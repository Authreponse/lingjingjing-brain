"""M4 评论总结与分享队列。

只管三件事，别的都不做：

1. **发现队列**：把主人/她发现的东西攒成一批（1-3 条），排序 + 去重。
2. **分享文案**：每条只给「链接 + 一句她自己的评价」，不贴别人的评论原文。
3. **主动节奏**：每天最多 3 批；每次要主动开口前都问 state_machine.should_speak()，
   静默时段 / 睡着了 / 饿坏了就**不发**，并且把原因记下来（不隐藏降级与失败）。

红线（硬规则，写在代码里而不是靠自觉）：

- 绝不 @ 任何人：文案里出现 @ 或全角＠ 直接拒绝。
- 不回复他人评论、不发公共评论：没有任何「回复/评论」通道，批次本身就不是评论。
- 不碰密码和验证码：链接里不带凭据，文案里出现密码/验证码字样也拒绝。

state_machine.py 只读不改：本模块只调用它公开的 should_speak() / proactive_hint()。

状态落在 event 表（append-only，天然留痕），kind 前缀 share.：
share.batch_published / share.batch_blocked。不新建表，不动 db.py 的 SCHEMA。
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from .config import Settings
from .db import Database, utc_now

logger = logging.getLogger(__name__)

#: 事件 kind 前缀：状态变更史与 /v1/events 都靠它筛选。
SHARE_EVENT_PREFIX = "share."

#: 一批最少 / 最多几条。攒不够一条就没有分享。
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 3

#: 主动分享每天最多几批（立项书 v1：主动行为有额度，不能刷屏）。
DEFAULT_DAILY_BATCH_LIMIT = 3

#: 同一条链接滚出队列前的冷却期（天）。
DEFAULT_DEDUP_WINDOW_DAYS = 7

#: 评价长度上限：一句，就一句。
COMMENT_MAX_CHARS = 120


def _tz(minutes: int | None = None) -> timezone:
    # 与 state_machine 一致：固定偏移，不依赖 tzdata（Windows 常没装）。
    return timezone(timedelta(minutes=minutes if minutes is not None else 8 * 60))


def local_day(now: datetime | None = None, *, offset_minutes: int | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(_tz(offset_minutes)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 红线检查：这些规则必须能挡在发布之前
# ---------------------------------------------------------------------------


class SharePolicyError(ValueError):
    """触犯红线。调用方据此拒绝，**不要**捕获后继续发。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


#: @ 人的判定：半角 @ 和全角 ＠ 都算。
_AT_PATTERN = re.compile(r"@|＠")
_REPLY_WORDS = ("回复", "回评", "楼下", "楼主", "评论区见", "转发评论", "回他", "回她")
_SECRET_WORDS = ("密码", "验证码", "口令", "token=", "cookie=", "passwd", "password")


def _reject(code: str, message: str) -> None:
    raise SharePolicyError(code, message)


def assert_no_mention(text: str) -> None:
    """@ 任何人在 v1 都是红线，不做例外。"""
    if _AT_PATTERN.search(text or ""):
        _reject("mention_forbidden", "@ 任何人在 v1 都是红线，不能发")


def assert_no_comment_reply(text: str) -> None:
    """她只分享，不点评别人：文案里不能出现「回复某人 / 评论区回」这类语义。"""
    lowered = (text or "").lower()
    for word in _REPLY_WORDS:
        if word in lowered:
            _reject("comment_reply_forbidden", f"不回复任何人的评论（命中「{word}」）")


def assert_no_secrets(text: str) -> None:
    """不碰密码和验证码。链接与文案里都不许出现。"""
    lowered = (text or "").lower()
    for word in _SECRET_WORDS:
        if word in lowered:
            _reject("secret_forbidden", f"不碰密码和验证码（命中「{word}」）")


def assert_shareable(url: str, comment: str) -> None:
    """一条分享发出前的全部红线检查。任何一条不过就抛 SharePolicyError。"""
    if not url or not url.strip():
        _reject("empty_url", "没有链接就没有分享")
    if not comment or not comment.strip():
        _reject("empty_comment", "只发链接不说话等于扔垃圾，必须有一句她自己的评价")
    if len(comment.strip()) > COMMENT_MAX_CHARS:
        _reject("comment_too_long", f"评价只能一句，最多 {COMMENT_MAX_CHARS} 字")
    for text in (url, comment):
        assert_no_mention(text)
        assert_no_secrets(text)
    assert_no_comment_reply(comment)


# ---------------------------------------------------------------------------
# 链接规范化与去重键
# ---------------------------------------------------------------------------

_TRACKING_PREFIXES = (
    "utm_",
    "spm",
    "vd_source",
    "share_source",
    "share_medium",
    "unique_k",
    "from_spmid",
)


def _video_id(parts) -> str | None:
    """B 站视频：BV 号大小写统一后再比，av 号同理。"""
    match = re.search(r"/video/(BV[0-9A-Za-z]{10})", parts.path, re.IGNORECASE)
    if match:
        return "BV:" + match.group(1).upper()
    match = re.search(r"/video/av(\d+)", parts.path, re.IGNORECASE)
    if match:
        return "AV:" + match.group(1)
    return None


def normalize_url(url: str) -> str:
    """去掉跟踪参数、统一主机名与末尾斜杠，让「同一条」落到同一个键上。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    kept: list[str] = []
    if parts.query:
        for pair in parts.query.split("&"):
            if not pair:
                continue
            key = pair.split("=", 1)[0].lower()
            if any(key.startswith(prefix) for prefix in _TRACKING_PREFIXES):
                continue
            kept.append(pair)
    query = "&".join(sorted(kept))
    return urlunsplit((scheme, netloc, path, query, ""))


def dedup_key(url: str, title: str = "") -> str:
    """去重键：认得出 B 站视频就用视频号，否则用规范化链接（空链接才退回标题）。"""
    normalized = normalize_url(url)
    identifier = _video_id(urlsplit(normalized))
    if identifier:
        return identifier
    base = normalized or (title or "").strip()
    if not base:
        return ""
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 一条发现 / 一批分享
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Discovery:
    """主人或她发现的一条内容。评价是**她自己的**，不是别人评论的转述。"""

    url: str
    title: str = ""
    comment: str = ""
    source: str = "bilibili"
    discovered_at: str = ""
    score: float = 0.0
    key: str = ""

    def __post_init__(self) -> None:
        self.url = (self.url or "").strip()
        self.title = (self.title or "").strip()
        self.comment = (self.comment or "").strip()
        if not self.discovered_at:
            self.discovered_at = utc_now()
        if not self.key:
            self.key = dedup_key(self.url, self.title)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "comment": self.comment,
            "source": self.source,
            "discovered_at": self.discovered_at,
            "score": self.score,
            "key": self.key,
        }


@dataclass(slots=True)
class ShareItem:
    """一条已通过红线检查的分享项：只有链接 + 一句评价。"""

    url: str
    comment: str
    title: str = ""
    key: str = ""
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "comment": self.comment,
            "title": self.title,
            "key": self.key,
            "score": self.score,
        }

    def to_text(self) -> str:
        """一条的最终文案：链接 + 一句她自己的评价。标题只用来定位，不含他人评论。"""
        head = f"{self.title}\n" if self.title else ""
        return f"{head}{self.url}\n我的评价：{self.comment}"


@dataclass(slots=True)
class Batch:
    """一批 1-3 条。批次 = 一次主动分享，**不是**一条评论。"""

    id: str
    day: str
    items: list[ShareItem] = field(default_factory=list)
    created_at: str = ""
    event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "day": self.day,
            "created_at": self.created_at,
            "event_id": self.event_id,
            "size": len(self.items),
            "items": [item.to_dict() for item in self.items],
            "text": self.to_text(),
        }

    def to_text(self) -> str:
        return "\n\n".join(item.to_text() for item in self.items)


def _desc_timestamp(value: str) -> str:
    """把时间戳按字符取补，让「越新越靠前」能塞进升序排序。"""
    return "".join(chr(0x10FFFF - ord(ch)) for ch in (value or ""))


def order_discoveries(
    discoveries: Iterable[Discovery],
    *,
    not_before: datetime | None = None,
) -> list[Discovery]:
    """排序 + 去重。

    排序：分数高的先；同分时新的先；再同则按 key 稳定排序（结果可复现）。
    去重：同一条链接（或同一个 B 站视频）只留最先出现的那条。
    not_before 只保留这条时间之后发现的（冷却期外的旧发现会被丢掉）。
    """
    seen: set[str] = set()
    picked: list[Discovery] = []
    for item in discoveries:
        if not item.url or not item.key:
            continue
        if not_before is not None and item.discovered_at:
            try:
                moment = datetime.fromisoformat(item.discovered_at)
            except ValueError:
                moment = None
            if moment is not None and moment < not_before:
                continue
        if item.key in seen:
            continue
        seen.add(item.key)
        picked.append(item)
    picked.sort(key=lambda d: (-d.score, _desc_timestamp(d.discovered_at), d.key))
    return picked


def build_batch(
    discoveries: Sequence[Discovery],
    *,
    limit: int = MAX_BATCH_SIZE,
    not_before: datetime | None = None,
    rejected: list[dict[str, str]] | None = None,
) -> list[ShareItem]:
    """从排序后的发现里挑出这一批。攒不满 MIN_BATCH_SIZE 就返回空列表。

    任何一条过不了红线检查（@、密码、回复语义、空评价、超长）就跳过；
    若传入 rejected 列表，会把被拒原因记进去 —— **不静默吞掉失败**。
    """
    ordered = order_discoveries(discoveries, not_before=not_before)
    ceiling = max(MIN_BATCH_SIZE, min(limit, MAX_BATCH_SIZE))
    items: list[ShareItem] = []
    for found in ordered:
        try:
            assert_shareable(found.url, found.comment)
        except SharePolicyError as exc:
            logger.warning("分享项被红线拒绝（%s）：%s", exc.code, found.url)
            if rejected is not None:
                rejected.append(
                    {"url": found.url, "code": exc.code, "message": exc.message}
                )
            continue
        items.append(
            ShareItem(
                url=normalize_url(found.url),
                comment=found.comment,
                title=found.title,
                key=found.key,
                score=found.score,
            )
        )
        if len(items) >= ceiling:
            break
    return items


# ---------------------------------------------------------------------------
# 状态机闸门：只读地借 should_speak / proactive_hint
# ---------------------------------------------------------------------------


class ProactiveGate(Protocol):
    """只需要这两个方法，方便测试替换。state_machine.StateMachine 天然满足。"""

    def should_speak(self, now: datetime | None = None) -> bool: ...

    def proactive_hint(self, now: datetime | None = None) -> dict[str, Any]: ...


class _FallbackGate:
    """没有状态机时的保守闸门：默认**不主动**，并把原因写清楚。

    保守方向是有意的：拿不准的时候不发，比发出去打扰主人更好。
    """

    def should_speak(self, now: datetime | None = None) -> bool:
        return False

    def proactive_hint(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "allowed": False,
            "reasons": ["状态机不可用，无法判断静默时段与睡眠，按不主动处理"],
            "state": {},
        }


# ---------------------------------------------------------------------------
# 主服务
# ---------------------------------------------------------------------------


class ShareService:
    """分享队列。所有写操作都落 event 表，状态靠重放事件得到（不建新表）。"""

    def __init__(
        self,
        db: Database,
        gate: ProactiveGate | None = None,
        *,
        daily_batch_limit: int = DEFAULT_DAILY_BATCH_LIMIT,
        dedup_window_days: int = DEFAULT_DEDUP_WINDOW_DAYS,
        utc_offset_minutes: int = 8 * 60,
    ):
        if daily_batch_limit < 0:
            raise ValueError("每日主动批次数不能为负")
        if dedup_window_days < 0:
            raise ValueError("去重冷却期不能为负")
        self.db = db
        self.gate = gate if gate is not None else _FallbackGate()
        self.daily_batch_limit = daily_batch_limit
        self.dedup_window_days = dedup_window_days
        self.utc_offset_minutes = utc_offset_minutes

    # ---- 读：从事件重放 -------------------------------------------------

    def _share_events(self, limit: int = 400) -> list[dict[str, Any]]:
        rows = self.db.list_events(limit=limit)
        return [row for row in rows if str(row["kind"]).startswith(SHARE_EVENT_PREFIX)]

    def published_today(self, now: datetime | None = None) -> int:
        """今天（本地日）已经主动发出去几批。"""
        today = local_day(now, offset_minutes=self.utc_offset_minutes)
        return sum(
            1
            for event in self._share_events()
            if event["kind"] == "share.batch_published"
            and event["payload"].get("day") == today
        )

    def recent_keys(self, now: datetime | None = None) -> set[str]:
        """冷却期内发过的去重键，用来避免同一条反复推。"""
        moment = now or datetime.now(timezone.utc)
        floor = moment - timedelta(days=self.dedup_window_days)
        keys: set[str] = set()
        for event in self._share_events():
            if event["kind"] != "share.batch_published":
                continue
            try:
                ts = datetime.fromisoformat(event["ts"])
            except ValueError:
                continue
            if ts < floor:
                continue
            for item in event["payload"].get("items", []):
                if item.get("key"):
                    keys.add(item["key"])
        return keys

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        """最近发生过的分享相关事件（发布 / 被拦），新的在前。"""
        return self._share_events(limit=max(limit, 50))[:limit]

    def _hint(self, now: datetime | None = None) -> dict[str, Any]:
        try:
            return self.gate.proactive_hint(now) or {}
        except Exception as exc:  # noqa: BLE001 - 状态机坏了就按不主动处理
            logger.warning("proactive_hint 调用失败，按不主动处理：%s", exc)
            return {"allowed": False, "reasons": [f"状态机调用失败：{exc}"], "state": {}}

    def can_publish(self, now: datetime | None = None) -> tuple[bool, list[str]]:
        """能不能主动发。返回 (允许?, 原因列表)；允许时原因列表为空。"""
        reasons: list[str] = []
        hint = self._hint(now)
        if not hint.get("allowed"):
            reasons.extend(hint.get("reasons") or ["当前不适合主动开口"])
        # should_speak() 是「静默时段不主动」的唯一真相来源，这里再问一次做交叉校验。
        try:
            if not self.gate.should_speak(now) and not reasons:
                reasons.append("should_speak() 判定现在不该主动")
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"should_speak() 调用失败：{exc}")
        used = self.published_today(now)
        if used >= self.daily_batch_limit:
            reasons.append(f"今天已经分享 {used} 批，上限 {self.daily_batch_limit} 批")
        return (not reasons, reasons)

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        hint = self._hint(now)
        used = self.published_today(now)
        allowed, reasons = self.can_publish(now)
        return {
            "day": local_day(now, offset_minutes=self.utc_offset_minutes),
            "published_today": used,
            "daily_batch_limit": self.daily_batch_limit,
            "remaining": max(0, self.daily_batch_limit - used),
            "allowed": allowed,
            "blocked_reasons": reasons,
            "state": hint.get("state") or {},
            "dedup_window_days": self.dedup_window_days,
            "batch_size": [MIN_BATCH_SIZE, MAX_BATCH_SIZE],
            "policies": {
                "mention_allowed": False,
                "reply_to_comment_allowed": False,
                "public_comment_allowed": False,
                "secrets_allowed": False,
            },
        }

    # ---- 写：发布 -------------------------------------------------------

    def publish(
        self,
        discoveries: Sequence[Discovery],
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """尝试发一批。

        force 只跳过额度与状态机闸门（主人当场要看时用），**绝不跳过任何红线**。
        被拦时写 share.batch_blocked 事件并返回 published=False 与原因 —— 不静默丢弃。
        """
        moment = now or datetime.now(timezone.utc)
        if not force:
            allowed, reasons = self.can_publish(moment)
            if not allowed:
                self._record_blocked(moment, discoveries, reasons)
                return {
                    "published": False,
                    "reason": "blocked",
                    "reasons": reasons,
                    "rejected": [],
                    "items": [],
                    "batch": None,
                }

        rejected: list[dict[str, str]] = []
        items = build_batch(
            discoveries,
            limit=MAX_BATCH_SIZE,
            not_before=moment - timedelta(days=self.dedup_window_days),
            rejected=rejected,
        )
        # 冷却期内发过的链接不再重复发（第二轮去重，跨批次）。
        sent = self.recent_keys(moment)
        duplicated = [item for item in items if item.key in sent]
        items = [item for item in items if item.key not in sent]
        if len(items) < MIN_BATCH_SIZE:
            reason = "队列里没有能发的发现（重复、冷却期内已发、或过不了红线）"
            reasons = [reason, *([f"{len(rejected)} 条过不了红线"] if rejected else [])]
            self._record_blocked(moment, discoveries, reasons)
            return {
                "published": False,
                "reason": "empty",
                "reasons": reasons,
                "rejected": rejected,
                "items": [],
                "batch": None,
            }

        batch = Batch(
            id=f"share-{moment.strftime('%Y%m%dT%H%M%S')}-{items[0].key[:6]}",
            day=local_day(moment, offset_minutes=self.utc_offset_minutes),
            items=items,
            created_at=utc_now(),
        )
        event = self.db.append_event(
            "灵鲸鲸",
            "share.batch_published",
            {
                "batch_id": batch.id,
                "day": batch.day,
                "items": [item.to_dict() for item in batch.items],
                "text": batch.to_text(),
                "forced": bool(force),
                "duplicates_skipped": len(duplicated),
                "rejected": rejected,
                "gate": self._hint(moment),
            },
        )
        batch.event_id = event["id"]
        return {
            "published": True,
            "reason": "ok",
            "reasons": [],
            "rejected": rejected,
            "items": [item.to_dict() for item in batch.items],
            "batch": batch.to_dict(),
        }

    def _record_blocked(
        self,
        moment: datetime,
        discoveries: Sequence[Discovery],
        reasons: list[str],
    ) -> None:
        """被拦也要留痕：主人之后能从 /v1/share/history 看到「为什么没发」。"""
        try:
            self.db.append_event(
                "灵鲸鲸",
                "share.batch_blocked",
                {
                    "day": local_day(moment, offset_minutes=self.utc_offset_minutes),
                    "reasons": list(reasons),
                    "candidate_count": len(list(discoveries)),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("share.batch_blocked 事件写入失败：%s", exc)


# ---------------------------------------------------------------------------
# HTTP 模型与路由
# ---------------------------------------------------------------------------


class DiscoveryIn(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=200)
    comment: str = Field(default="", max_length=COMMENT_MAX_CHARS)
    source: str = Field(default="bilibili", max_length=40)
    discovered_at: str | None = None
    score: float = Field(default=0.0, ge=0, le=100)

    @field_validator("url")
    @classmethod
    def _looks_like_url(cls, value: str) -> str:
        value = (value or "").strip()
        if "://" not in value:
            raise ValueError("链接必须带协议头（http/https）")
        return value

    @field_validator("comment")
    @classmethod
    def _comment_red_lines(cls, value: str) -> str:
        value = (value or "").strip()
        if value:
            # 入场即拒：带着 @ / 密码字样的东西根本进不了队列。
            assert_no_mention(value)
            assert_no_secrets(value)
            assert_no_comment_reply(value)
        return value


class PublishRequest(BaseModel):
    discoveries: list[DiscoveryIn] = Field(default_factory=list, max_length=20)
    force: bool = False


class RawDiscoveryIn(BaseModel):
    """红线自检专用：不在这里拦截，好让 /share/guard 把每一处问题都报出来。"""

    url: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=200)
    comment: str = Field(default="", max_length=400)
    source: str = Field(default="bilibili", max_length=40)
    discovered_at: str | None = None
    score: float = Field(default=0.0, ge=0, le=100)


class GuardRequest(BaseModel):
    discoveries: list[RawDiscoveryIn] = Field(default_factory=list, max_length=20)


def _to_discovery(item: Mapping[str, Any]) -> Discovery:
    allowed = ("url", "title", "comment", "source", "discovered_at", "score", "key")
    return Discovery(**{k: v for k, v in item.items() if k in allowed})


def build_share_router(
    settings: Settings,
    db: Database | None = None,
    gate: ProactiveGate | None = None,
    *,
    service: ShareService | None = None,
) -> APIRouter:
    """返回挂在 /v1 下的分享路由；自带 X-Ling-Token 校验，不依赖 app.py 的依赖。

    Lead 接线（在 app.py 的 create_app 里加一行）：

        app.include_router(build_share_router(settings, db, machine))

    machine 就是 app.py 已有的 build_state_machine(db, settings) 结果。
    本模块只**读**它的 should_speak() / proactive_hint()，不改 state_machine.py。
    不传 gate 就自己构造一个；构造失败按「不主动」保守处理（并写进 blocked_reasons）。
    """
    database = db if db is not None else Database(settings.db_path)
    share_service = service
    if share_service is None:
        active_gate = gate
        if active_gate is None:
            try:
                from .state_machine import build_state_machine

                active_gate = build_state_machine(database, settings)
            except Exception as exc:  # noqa: BLE001 - 状态机不可用就保守不主动
                logger.warning("分享路由未装配状态机，按不主动处理：%s", exc)
                active_gate = None
        share_service = ShareService(
            database,
            active_gate,
            utc_offset_minutes=int(
                getattr(settings, "state_utc_offset_minutes", 8 * 60) or 8 * 60
            ),
        )
    svc = share_service
    router = APIRouter(prefix="/v1", tags=["share"])

    async def authorized(x_ling_token: str = Header(default="")) -> None:
        if not secrets.compare_digest(x_ling_token, settings.auth_token):
            raise HTTPException(status_code=401, detail="认证失败")

    @router.get("/share/status", dependencies=[Depends(authorized)])
    async def share_status() -> dict:
        return svc.status()

    @router.get("/share/history", dependencies=[Depends(authorized)])
    async def share_history(limit: int = 20) -> dict:
        return {"items": svc.history(max(1, min(int(limit), 100)))}

    @router.post("/share/preview", dependencies=[Depends(authorized)])
    async def share_preview(request: PublishRequest) -> dict:
        """只看会发什么，不落任何状态，方便主人先审一眼。"""
        discoveries = [_to_discovery(item.model_dump(exclude_none=True)) for item in request.discoveries]
        rejected: list[dict[str, str]] = []
        items = build_batch(discoveries, limit=MAX_BATCH_SIZE, rejected=rejected)
        allowed, reasons = svc.can_publish()
        return {
            "allowed": allowed,
            "reasons": reasons,
            "rejected": rejected,
            "items": [item.to_dict() for item in items],
            "text": "\n\n".join(item.to_text() for item in items),
            "policies": svc.status()["policies"],
        }

    @router.post("/share/publish", dependencies=[Depends(authorized)])
    async def share_publish(request: PublishRequest) -> dict:
        discoveries = [_to_discovery(item.model_dump(exclude_none=True)) for item in request.discoveries]
        return svc.publish(discoveries, force=request.force)

    @router.post("/share/guard", dependencies=[Depends(authorized)])
    async def share_guard(request: GuardRequest) -> dict:
        """红线自检：只回答「这些能不能发」，不落任何状态。任何一条不过就 422。"""
        problems: list[dict[str, str]] = []
        for item in request.discoveries:
            try:
                assert_shareable(item.url, item.comment)
            except SharePolicyError as exc:
                problems.append({"url": item.url, "code": exc.code, "message": exc.message})
        if problems:
            raise HTTPException(
                status_code=422,
                detail={"error": "share_policy_violation", "problems": problems},
            )
        return {
            "ok": True,
            "policy": "v1：不@人、不回复评论、不发公共评论、不碰密码验证码",
        }

    return router


__all__ = [
    "Batch",
    "COMMENT_MAX_CHARS",
    "DEFAULT_DAILY_BATCH_LIMIT",
    "DEFAULT_DEDUP_WINDOW_DAYS",
    "Discovery",
    "DiscoveryIn",
    "GuardRequest",
    "MAX_BATCH_SIZE",
    "MIN_BATCH_SIZE",
    "ProactiveGate",
    "PublishRequest",
    "RawDiscoveryIn",
    "SHARE_EVENT_PREFIX",
    "ShareItem",
    "SharePolicyError",
    "ShareService",
    "assert_no_comment_reply",
    "assert_no_mention",
    "assert_no_secrets",
    "assert_shareable",
    "build_batch",
    "build_share_router",
    "dedup_key",
    "local_day",
    "normalize_url",
    "order_discoveries",
]
