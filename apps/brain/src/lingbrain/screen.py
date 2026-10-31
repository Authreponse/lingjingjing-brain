"""M5 静态看屏：一次一张、可暂停、黑名单硬拦截、默认关闭。

严格对齐立项书 3.11.2 的安全要求，每一条在代码里都有独立执行点，
且在 tests/test_screen.py 里各有一条专门测试：

1. **静态，一次一张**（不是实时）—— capture() 每次只抓一帧，不启动定时循环。
2. **一键暂停立刻生效且持久** —— pause() 同步写 state 表，重启后仍然暂停。
3. **黑名单命中自动跳过并明确提示** —— 抛 ScreenBlocked，带 notice，写 screen.skipped。
4. **默认关闭** —— 未开启时抛 ScreenDisabled，且不调用后端。
5. **后端先做 Mock** —— MockScreenBackend 生成真实 png；RealScreenBackend 默认不启用。
6. **可见提示** —— 成功/跳过/失败都带 notice 并写事件。
7. **只理解上下文，不替主人操作** —— 没有任何鼠标键盘注入入口。

红线：不 @ 人、不发公共评论、不碰密码和验证码。
"""

from __future__ import annotations

import base64
import io
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from .db import Database

# ---------------------------------------------------------------------------
# 常数
# ---------------------------------------------------------------------------

PAUSE_KEY = "看屏暂停"
ENABLED_KEY = "看屏开启"
LAST_CAPTURE_KEY = "看屏最后截屏"

CAPTURE_COOLDOWN_SECONDS = 30.0
DAILY_LIMIT = 60
MAX_IMAGE_BYTES = 8 * 1024 * 1024

EVENT_CAPTURE = "screen.capture"
EVENT_SKIPPED = "screen.skipped"
EVENT_FAILED = "screen.failed"
EVENT_PAUSED = "screen.paused"
EVENT_RESUMED = "screen.resumed"
EVENT_ENABLED = "screen.enabled"
EVENT_DISABLED = "screen.disabled"
EVENT_KIND_PREFIX = "screen."

SCREEN_SUBDIR = "screen"

CATEGORY_LABELS = {
    "password": "密码",
    "verification_code": "验证码",
    "banking": "网银/支付",
    "private_window": "私密",
    "messaging_private": "私聊",
}

# ---------------------------------------------------------------------------
# 黑名单
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlacklistRule:
    """一条黑名单规则：命中窗口标题或进程名就整张跳过。"""

    category: str
    patterns: tuple[str, ...]
    reason: str

    def matches(self, haystack: str) -> bool:
        return any(re.search(pattern, haystack, re.IGNORECASE) for pattern in self.patterns)


#: 宁可多拦，不可漏拦：误拦只是少看一帧，漏拦是隐私事故。
DEFAULT_BLACKLIST: tuple[BlacklistRule, ...] = (
    BlacklistRule(
        category="password",
        patterns=(
            r"密码",
            r"口令",
            r"password",
            r"passwd",
            r"passphrase",
            r"密钥",
            r"私钥",
            r"secret",
            r"token",
            r"凭据",
            r"credential",
        ),
        reason="密码/密钥类窗口，不能截",
    ),
    BlacklistRule(
        category="verification_code",
        patterns=(
            r"验证码",
            r"校验码",
            r"动态码",
            r"短信验证",
            r"二次验证",
            r"两步验证",
            r"2fa",
            r"otp",
            r"verification code",
            r"one-time",
        ),
        reason="验证码类窗口，不能截",
    ),
    BlacklistRule(
        category="banking",
        patterns=(
            r"网银",
            r"网上银行",
            r"银行",
            r"支付",
            r"付款",
            r"转账",
            r"支付宝",
            r"微信支付",
            r"银行卡",
            r"信用卡",
            r"bank",
            r"payment",
            r"checkout",
            r"wallet",
            r"钱包",
        ),
        reason="网银/支付类窗口，不能截",
    ),
    BlacklistRule(
        category="private_window",
        patterns=(
            r"无痕",
            r"隐身模式",
            r"私密",
            r"隐私",
            r"incognito",
            r"private browsing",
            r"private window",
            r"vault",
            r"密码管理器",
            r"keychain",
        ),
        reason="私密/无痕窗口，不能截",
    ),
    BlacklistRule(
        category="messaging_private",
        patterns=(r"私聊", r"私信", r"密聊"),
        reason="私聊窗口，主人没说可以看",
    ),
)


@dataclass(slots=True)
class WindowInfo:
    """被看的那一扇窗。后端只提供元数据，不负责判断该不该截。"""

    title: str = ""
    process: str = ""
    is_private: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def haystack(self) -> str:
        parts = [self.title, self.process]
        parts.extend(str(v) for v in self.extra.values() if isinstance(v, str))
        return "\n".join(parts)


def check_blacklist(
    window: WindowInfo,
    rules: Iterable[BlacklistRule] = DEFAULT_BLACKLIST,
) -> list[BlacklistRule]:
    """返回命中的规则列表（空 = 可以截）。命中任何一条就整张跳过。"""
    hits: list[BlacklistRule] = []
    if window.is_private:
        hits.append(
            BlacklistRule(
                category="private_window",
                patterns=(),
                reason="后端标记为私密窗口，不能截",
            )
        )
    for rule in rules:
        if rule.matches(window.haystack) and rule not in hits:
            hits.append(rule)
    return hits


def describe_hits(hits: Iterable[BlacklistRule]) -> str:
    """把命中的规则翻译成给主人看的一句话。"""
    categories: list[str] = []
    reasons: list[str] = []
    for rule in hits:
        label = CATEGORY_LABELS.get(rule.category, rule.category)
        if label not in categories:
            categories.append(label)
        if rule.reason not in reasons:
            reasons.append(rule.reason)
    if not categories:
        return ""
    return f"这一屏没有截：检测到{ '、'.join(categories) }窗口（{ '；'.join(reasons) }）。"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ScreenError(Exception):
    """看屏相关基类异常。code 给路由层映射 HTTP 状态码。"""

    code = "screen_error"
    http_status = 400


class ScreenDisabled(ScreenError):
    """默认关闭：没显式开启就调用，直接拒绝且不碰后端。"""

    code = "screen_disabled"
    http_status = 403


class ScreenPaused(ScreenError):
    """主人按了暂停键。暂停后不再截屏。"""

    code = "screen_paused"
    http_status = 423


class ScreenBlocked(ScreenError):
    """黑名单命中：跳过，且必须带提示语（不静默）。"""

    code = "screen_blocked"
    http_status = 409

    def __init__(self, message: str, *, notice: str, hits: list[BlacklistRule]):
        super().__init__(message)
        self.notice = notice
        self.hits = hits
        self.categories = [CATEGORY_LABELS.get(r.category, r.category) for r in hits]


class ScreenRateLimited(ScreenError):
    """太频繁：静态看屏不是实时，连拍会被拒。"""

    code = "screen_rate_limited"
    http_status = 429


class ScreenDailyLimitReached(ScreenError):
    """今天已经截够张数了。"""

    code = "screen_daily_limit"
    http_status = 429


class BackendUnavailable(ScreenError):
    """后端不可用（包括"真截屏后端默认不启用"）。"""

    code = "screen_backend_unavailable"
    http_status = 503


class BackendFailed(ScreenError):
    """后端执行失败。不隐藏失败，原样上报。"""

    code = "screen_backend_failed"
    http_status = 502


# ---------------------------------------------------------------------------
# 后端
# ---------------------------------------------------------------------------


class ScreenBackend(Protocol):
    """截屏后端接口：只负责"抓一帧"，不做任何安全判断。

    安全判断全在 ScreenService 里，换后端也绕不过隐私规则。
    """

    name: str

    def available(self) -> bool: ...

    def current_window(self) -> WindowInfo: ...

    def capture(self, out_path: Path) -> bytes: ...


class MockScreenBackend:
    """Mock 后端：生成真实可打开的测试图片，用于无真实桌面时的验收。

    它**不截真实屏幕**，产物里写明 MOCK，避免被当成真实验证。
    """

    name = "mock"

    def __init__(self, window: WindowInfo | None = None, fail: str | None = None):
        self.window = window or WindowInfo(title="灵鲸鲸 Mock 屏", process="mock")
        self.fail = fail
        self.capture_count = 0

    def available(self) -> bool:
        return True

    def current_window(self) -> WindowInfo:
        return self.window

    def capture(self, out_path: Path) -> bytes:
        if self.fail:
            raise BackendFailed(self.fail)
        self.capture_count += 1
        out_path.parent.mkdir(parents=True, exist_ok=True)
        data = _render_mock_png()
        out_path.write_bytes(data)
        return data


PNG_SIGNATURE_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1NdAAAADElEQVR4nGP4zwAAAwEBABjdjbAAAAAASUVORK5CYII="


def _render_mock_png(width: int = 640, height: int = 360) -> bytes:
    """用 Pillow 画一张带 MOCK 字样的测试图；没有 Pillow 时退化为最小合法 PNG。"""
    try:
        from PIL import Image, ImageDraw
    except Exception:  # pragma: no cover
        return base64.b64decode(PNG_SIGNATURE_B64)
    image = Image.new("RGB", (width, height), (24, 32, 48))
    draw = ImageDraw.Draw(image)
    draw.rectangle([16, 16, width - 16, 48], fill=(52, 66, 96))
    for i, color in enumerate([(70, 90, 130), (90, 110, 150), (110, 130, 170)]):
        y = 80 + i * 60
        draw.rectangle([32, y, width - 200, y + 36], fill=color)
    draw.text((width // 2 - 40, height - 40), "MOCK SCREEN", fill=(200, 220, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class RealScreenBackend:
    """真实截屏后端：**接口保留，默认不启用**。

    v1 权限矩阵里 screen/realtime_watch 是 deny（LJ-R008），真实截屏要主人显式复核。
    这里保留接口，但 available() 恒为 False，调用即抛 BackendUnavailable，
    不会偷偷截真屏幕，也不假装自己能用。
    """

    name = "real"

    def __init__(self, enabled_by_owner: bool = False):
        self.enabled_by_owner = bool(enabled_by_owner)

    def available(self) -> bool:
        return False

    def current_window(self) -> WindowInfo:
        raise BackendUnavailable(
            "真实截屏后端在 v1 默认不启用（权限矩阵 screen/realtime_watch = deny，LJ-R008）。"
            "当前只有 Mock 后端可用。"
        )

    def capture(self, out_path: Path) -> bytes:  # pragma: no cover
        raise BackendUnavailable(
            "真实截屏后端在 v1 默认不启用（权限矩阵 screen/realtime_watch = deny，LJ-R008）。"
        )


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CaptureResult:
    """一次成功截屏的结果。notice 是给主人看的可见提示。"""

    id: str
    path: str
    relative_path: str
    window: WindowInfo
    backend: str
    created_at: str
    notice: str
    mock: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "relative_path": self.relative_path,
            "window": {
                "title": self.window.title,
                "process": self.window.process,
                "is_private": self.window.is_private,
            },
            "backend": self.backend,
            "created_at": self.created_at,
            "notice": self.notice,
            "mock": self.mock,
            "is_static": True,
            "is_realtime": False,
            "action_taken": None,
        }


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class ScreenService:
    """静态看屏服务：所有安全要求收敛到这一个对象上。"""

    def __init__(
        self,
        db: Database,
        *,
        data_dir: Path | None = None,
        backend: ScreenBackend | None = None,
        blacklist: Iterable[BlacklistRule] | None = None,
        cooldown_seconds: float = CAPTURE_COOLDOWN_SECONDS,
        daily_limit: int = DAILY_LIMIT,
        enabled_by_env: bool = False,
    ) -> None:
        self.db = db
        self.data_dir = Path(data_dir) if data_dir is not None else db.path.parent
        self.backend = backend or MockScreenBackend()
        self.blacklist = tuple(blacklist) if blacklist is not None else DEFAULT_BLACKLIST
        self.cooldown_seconds = float(cooldown_seconds)
        self.daily_limit = int(daily_limit)
        self._lock = threading.RLock()
        if enabled_by_env:
            self._ensure_key(ENABLED_KEY, True)

    def _state(self) -> dict[str, Any]:
        return self.db.get_state()

    def _ensure_key(self, key: str, default: Any) -> None:
        if key not in self._state():
            self.db.set_state({key: default})

    def is_enabled(self) -> bool:
        """默认关闭：state 表没有这个键，或值为假，都算关闭。"""
        return bool(self._state().get(ENABLED_KEY, False))

    def is_paused(self) -> bool:
        """暂停状态从 state 表读，跨进程/重启都保留。"""
        return bool(self._state().get(PAUSE_KEY, False))

    def enable(self, *, by: str = "主人") -> dict[str, Any]:
        with self._lock:
            self.db.set_state({ENABLED_KEY: True})
            self.db.append_event("灵鲸鲸", EVENT_ENABLED, {"by": by})
        return self.status()

    def disable(self, *, by: str = "主人") -> dict[str, Any]:
        with self._lock:
            self.db.set_state({ENABLED_KEY: False})
            self.db.append_event("灵鲸鲸", EVENT_DISABLED, {"by": by})
        return self.status()

    def pause(self, *, by: str = "主人", reason: str = "主人按了暂停") -> dict[str, Any]:
        """一键暂停：同步落库 + 立即生效。"""
        with self._lock:
            self.db.set_state({PAUSE_KEY: True})
            self.db.append_event("灵鲸鲸", EVENT_PAUSED, {"by": by, "reason": reason})
        return self.status()

    def resume(self, *, by: str = "主人") -> dict[str, Any]:
        with self._lock:
            self.db.set_state({PAUSE_KEY: False})
            self.db.append_event("灵鲸鲸", EVENT_RESUMED, {"by": by})
        return self.status()

    def status(self) -> dict[str, Any]:
        state = self._state()
        return {
            "enabled": bool(state.get(ENABLED_KEY, False)),
            "paused": bool(state.get(PAUSE_KEY, False)),
            "backend": self.backend.name,
            "backend_available": self.backend.available(),
            "is_static": True,
            "is_realtime": False,
            "daily_limit": self.daily_limit,
            "captured_today": self._captured_today(),
            "last_capture_at": state.get(LAST_CAPTURE_KEY),
            "cooldown_seconds": self.cooldown_seconds,
            "blacklist_categories": sorted({rule.category for rule in self.blacklist}),
            "notice": self._status_notice(state),
        }

    def _status_notice(self, state: Mapping[str, Any]) -> str:
        if state.get(PAUSE_KEY):
            return "看屏已暂停：暂停期间不会截任何屏。"
        if not state.get(ENABLED_KEY):
            return "看屏默认关闭，调用会被拒绝。需要主人显式开启。"
        return "看屏已开启（静态，一次一张）。"

    def _captured_today(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        count = 0
        for event in self.db.list_events(limit=500):
            if event["kind"] != EVENT_CAPTURE:
                continue
            if str(event["ts"]).startswith(today):
                count += 1
        return count

    def _log_skipped(self, reason: str, *, notice: str = "", client_id: str = "desktop") -> None:
        self.db.append_event(
            "灵鲸鲸",
            EVENT_SKIPPED,
            {
                "reason": reason,
                "notice": notice or reason,
                "client_id": client_id,
                "captured": False,
            },
        )

    def capture(
        self,
        *,
        reason: str = "理解上下文",
        client_id: str = "desktop",
        now: datetime | None = None,
    ) -> CaptureResult:
        """截一帧（静态）。失败一律抛异常并写事件，不会静默返回空结果。

        顺序：暂停 → 是否开启 → 后端可用 → 节流 → 日限额 → 黑名单 → 抓图。
        黑名单在抓图之前判断：命中就根本不调用后端，屏幕内容不会落盘。
        """
        with self._lock:
            instant = now or datetime.now(timezone.utc)
            if self.is_paused():
                self._log_skipped("paused", notice="看屏已暂停：暂停期间不会截屏。", client_id=client_id)
                raise ScreenPaused("看屏已暂停：暂停期间不会截屏。")
            if not self.is_enabled():
                self._log_skipped("disabled", notice="看屏未开启", client_id=client_id)
                raise ScreenDisabled(
                    "看屏默认关闭。需要主人显式开启后才能截屏（POST /v1/screen/enable）。"
                )
            if not self.backend.available():
                self._log_skipped(
                    "backend_unavailable",
                    notice=f"后端 {self.backend.name} 不可用",
                    client_id=client_id,
                )
                raise BackendUnavailable(
                    f"截屏后端 {self.backend.name} 当前不可用；v1 只提供 Mock 后端。"
                )
            throttled = self._throttled(instant)
            if throttled:
                self._log_skipped("rate_limited", notice=throttled, client_id=client_id)
                raise ScreenRateLimited(throttled)
            if self._captured_today() >= self.daily_limit:
                self._log_skipped("daily_limit", notice="今日张数已用完", client_id=client_id)
                raise ScreenDailyLimitReached(
                    f"今天已经看了 {self.daily_limit} 张，达到上限；明天再继续。"
                )
            window = self.backend.current_window()
            hits = check_blacklist(window, self.blacklist)
            if hits:
                notice = describe_hits(hits)
                self.db.append_event(
                    "灵鲸鲸",
                    EVENT_SKIPPED,
                    {
                        "reason": "blacklist",
                        "categories": [CATEGORY_LABELS.get(h.category, h.category) for h in hits],
                        "category_keys": [h.category for h in hits],
                        "window_title": window.title,
                        "window_process": window.process,
                        "notice": notice,
                        "client_id": client_id,
                        "captured": False,
                    },
                )
                raise ScreenBlocked(notice, notice=notice, hits=hits)

            capture_id = str(uuid.uuid4())
            directory = self.data_dir / SCREEN_SUBDIR
            out_path = directory / f"{capture_id}.png"
            try:
                data = self.backend.capture(out_path)
            except ScreenError as exc:
                self.db.append_event(
                    "灵鲸鲸",
                    EVENT_FAILED,
                    {"error": str(exc), "code": exc.code, "backend": self.backend.name, "client_id": client_id},
                )
                raise
            except Exception as exc:
                self.db.append_event(
                    "灵鲸鲸",
                    EVENT_FAILED,
                    {"error": str(exc), "code": "screen_backend_failed", "backend": self.backend.name, "client_id": client_id},
                )
                raise BackendFailed(f"截屏失败：{exc}") from exc

            if len(data) > MAX_IMAGE_BYTES:
                out_path.unlink(missing_ok=True)
                self.db.append_event(
                    "灵鲸鲸", EVENT_FAILED, {"error": "产物过大", "code": "screen_image_too_large"}
                )
                raise BackendFailed("截屏产物超过大小上限，已丢弃。")

            created_at = instant.isoformat(timespec="milliseconds")
            title = window.title or "未命名窗口"
            notice = f"刚看了一眼屏幕：{title}（静态一帧，仅用于理解上下文）。"
            self.db.set_state({LAST_CAPTURE_KEY: created_at})
            self.db.append_event(
                "灵鲸鲸",
                EVENT_CAPTURE,
                {
                    "id": capture_id,
                    "path": str(out_path),
                    "window_title": window.title,
                    "window_process": window.process,
                    "backend": self.backend.name,
                    "mock": isinstance(self.backend, MockScreenBackend),
                    "reason": reason,
                    "client_id": client_id,
                    "notice": notice,
                    "bytes": len(data),
                },
            )
            return CaptureResult(
                id=capture_id,
                path=str(out_path),
                relative_path=f"{SCREEN_SUBDIR}/{out_path.name}",
                window=window,
                backend=self.backend.name,
                created_at=created_at,
                notice=notice,
                mock=isinstance(self.backend, MockScreenBackend),
            )

    def _throttled(self, instant: datetime) -> str:
        """返回节流原因；没被节流返回空串。"""
        last = self._state().get(LAST_CAPTURE_KEY)
        if not last:
            return ""
        try:
            previous = datetime.fromisoformat(str(last))
        except ValueError:
            return ""
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        elapsed = (instant - previous).total_seconds()
        if elapsed < self.cooldown_seconds:
            return (
                f"静态看屏不是实时：两次截屏至少间隔 {int(self.cooldown_seconds)} 秒，"
                f"请 {int(self.cooldown_seconds - elapsed) + 1} 秒后再试。"
            )
        return ""

    def analyze(self, capture_id: str, *, summary: str | None = None) -> dict[str, Any]:
        """把一帧变成"上下文描述"，供对话使用。不替主人操作。"""
        event = self._find_capture(capture_id)
        if event is None:
            raise ScreenError(f"找不到这次截屏：{capture_id}")
        payload = event["payload"]
        return {
            "id": capture_id,
            "captured_at": event["ts"],
            "window_title": payload.get("window_title", ""),
            "backend": payload.get("backend", ""),
            "mock": bool(payload.get("mock", False)),
            "summary": summary or payload.get("notice", ""),
            "purpose": "理解上下文",
            "action_taken": None,
            "can_operate": False,
        }

    def _find_capture(self, capture_id: str) -> dict[str, Any] | None:
        for event in self.db.list_events(limit=500):
            if event["kind"] == EVENT_CAPTURE and event["payload"].get("id") == capture_id:
                return event
        return None

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        """看屏相关事件（截屏/跳过/失败/暂停/开启），最新在前。"""
        rows = self.db.list_events(limit=max(limit * 4, 200))
        picked = [row for row in rows if row["kind"].startswith(EVENT_KIND_PREFIX)]
        return picked[: max(1, limit)]

    def resolve_image(self, capture_id: str) -> Path:
        """按 id 找到产物文件，并确认它在受管目录内（防路径穿越）。"""
        event = self._find_capture(capture_id)
        if event is None:
            raise ScreenError(f"找不到这次截屏：{capture_id}")
        raw = Path(str(event["payload"].get("path", "")))
        base = (self.data_dir / SCREEN_SUBDIR).resolve()
        try:
            resolved = raw.resolve()
        except OSError as exc:
            raise ScreenError("产物路径不可解析") from exc
        if base not in resolved.parents and resolved != base:
            raise ScreenError("产物路径不在受管目录内")
        if not resolved.is_file():
            raise ScreenError("产物文件缺失")
        return resolved


def build_screen_service(
    db: Database,
    settings: Any | None = None,
    *,
    backend: ScreenBackend | None = None,
) -> ScreenService:
    """从 Settings 构造服务。默认仍然是关闭。"""
    data_dir = getattr(settings, "data_dir", None) if settings is not None else None
    if data_dir is None:
        data_dir = db.path.parent
    enabled_by_env = os.getenv("LING_SCREEN_ENABLED", "0") not in {"0", "", "false", "False"}
    return ScreenService(
        db,
        data_dir=Path(data_dir),
        backend=backend,
        enabled_by_env=enabled_by_env,
    )


__all__ = [
    "ScreenService",
    "ScreenBackend",
    "MockScreenBackend",
    "RealScreenBackend",
    "BlacklistRule",
    "WindowInfo",
    "CaptureResult",
    "ScreenError",
    "ScreenDisabled",
    "ScreenPaused",
    "ScreenBlocked",
    "ScreenRateLimited",
    "ScreenDailyLimitReached",
    "BackendUnavailable",
    "BackendFailed",
    "build_screen_service",
    "check_blacklist",
    "describe_hits",
    "DEFAULT_BLACKLIST",
    "CATEGORY_LABELS",
    "EVENT_CAPTURE",
    "EVENT_SKIPPED",
    "EVENT_FAILED",
    "EVENT_PAUSED",
    "EVENT_RESUMED",
    "EVENT_ENABLED",
    "EVENT_DISABLED",
    "PAUSE_KEY",
    "ENABLED_KEY",
]
