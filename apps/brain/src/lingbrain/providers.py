"""V4 模型提供商注册表与并发路由。

设计边界（很重要）
------------------
本模块**只做配置与选择**，不重复实现任何 HTTP 调用：

* 真正发请求的是 :mod:`model_providers`（echo / openai_compatible / ollama 等），
  这里通过 :func:`lingbrain.model_providers.create_provider` 按配置造实例。
* 连通性测试调用提供商自己的 ``list_models()`` / ``health_check()``，
  不在本模块里手写 /models、/api/tags 的请求。

安全性
------
* **api_key 只写不读**：数据库里存着，但 :meth:`Provider.out` 永远只给
  ``has_api_key`` 布尔值，任何响应都不会带出密钥。
* base_url 只允许 http/https；指向云元数据地址（169.254.169.254 等）直接拒绝，
  内网非回环地址仅告警（返回 warnings，不阻断）。
* 名称唯一、数量上限 20、内置 echo 不可删。

路由
----
* ``single``：所有任务都走 active_provider_id；失败直接抛错（路由层映射 503）。
* ``concurrent``：最多 4 个启用，按能力分配
  - chat    → 已探活且延迟最低的、具备 chat 能力者
  - extract → 同上（更看重便宜/快，用同一套延迟排序）
  调用失败时按顺序故障转移到下一个候选，并把每次失败记进 ``failovers``。

能力范围（已按 Lead 拍板收敛）
------------------------------
本注册表**只管理 LLM 提供商**，能力枚举固定为 ``chat`` / ``extract``。
生图（imagegen.py）与语音（speech.py）保持各自的后端，不经过这里，
因此不接受、也不展示 image/tts 能力 —— 免得配置界面暗示"选了就能接管生图"。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

from .db import Database, utc_now
from .model_providers import (
    EchoProvider,
    ModelError,
    ModelProvider,
    ModelUnavailable,
    ProviderConfig,
    create_provider,
)

logger = logging.getLogger(__name__)

#: 提供商数量上限（契约第 2 节）
MAX_PROVIDERS = 20
#: concurrent 模式最多启用几个（契约：limits.max_enabled = 4）
MAX_ENABLED = 4
#: 连通性测试超时（契约：8s）
TEST_TIMEOUT_SECONDS = 8.0

MODES = ("single", "concurrent")
KINDS = ("openai_compatible", "ollama")
#: 本注册表只管 LLM 能力；生图/语音走各自后端，不在此登记。
CAPABILITIES = ("chat", "extract")
TASK_TYPES = CAPABILITIES
STATUS_VALUES = ("ok", "down", "unknown", "unchecked")

#: 内置提供商 id（固定，方便迁移与识别）
BUILTIN_ECHO_ID = "builtin-echo"
BUILTIN_ECHO_NAME = "内置 echo（离线验收）"


class ProviderError(Exception):
    """提供商配置错误。http_status 由路由层使用。"""

    code = "provider_error"
    http_status = 400

    def __init__(self, message: str, *, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = list(problems or [])


class ProviderNotFound(ProviderError):
    code = "provider_not_found"
    http_status = 404


class ProviderConflict(ProviderError):
    code = "provider_conflict"
    http_status = 409


class ProviderForbidden(ProviderError):
    code = "provider_forbidden"
    http_status = 403


class ProviderLimitExceeded(ProviderError):
    code = "provider_limit"
    http_status = 409


class ProviderValidationError(ProviderError):
    code = "provider_invalid"
    http_status = 422


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Provider:
    """一个提供商配置。``api_key`` 在内存里存在，但绝不进 API 响应。"""

    id: str
    name: str
    kind: str
    base_url: str
    model: str
    api_key: str = ""
    enabled: bool = True
    capabilities: list[str] = field(default_factory=lambda: ["chat"])
    status: str = "unchecked"
    is_builtin: bool = False
    last_checked_at: str | None = None
    latency_ms: int | None = None
    last_error: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def out(self) -> dict[str, Any]:
        """契约第 2 节的对外形状。**不含 api_key**。"""
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "base_url": self.base_url,
            "model": self.model,
            "has_api_key": self.has_api_key,
            "enabled": self.enabled,
            "capabilities": list(self.capabilities),
            "status": self.status,
            "is_builtin": self.is_builtin,
            "last_checked_at": self.last_checked_at,
            "latency_ms": self.latency_ms,
            "last_error": self.last_error,
        }

    def to_provider_config(self, *, timeout_seconds: float = 60.0) -> ProviderConfig:
        return ProviderConfig(
            name=self.kind,
            base_url=self.base_url,
            model=self.model,
            api_key=self.api_key,
            timeout_seconds=timeout_seconds,
        )


@dataclass(slots=True)
class Routing:
    mode: str = "single"
    active_provider_id: str | None = None
    enabled_ids: list[str] = field(default_factory=list)
    assignments: dict[str, str] = field(default_factory=dict)

    def out(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "active_provider_id": self.active_provider_id,
            "enabled_ids": list(self.enabled_ids),
            "assignments": dict(self.assignments),
        }


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


_METADATA_HOSTS = {
    "169.254.169.254",  # AWS / GCP / Azure / OpenStack 通用元数据地址
    "metadata.google.internal",
    "metadata",
    "100.100.100.200",  # 阿里云
}


def _is_metadata_address(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").strip().lower()
    if not host:
        return False
    if host in _METADATA_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address in ipaddress.ip_network("169.254.0.0/16")


def _is_loopback(host: str) -> bool:
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_base_url(base_url: str) -> list[str]:
    """校验 base_url，返回警告列表（不通过则抛 ProviderValidationError）。"""
    text = (base_url or "").strip()
    if not text:
        raise ProviderValidationError("base_url 不能为空")
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"}:
        raise ProviderValidationError("base_url 必须以 http:// 或 https:// 开头")
    if not parts.hostname:
        raise ProviderValidationError("base_url 缺少主机名")
    if _is_metadata_address(text):
        raise ProviderValidationError(
            "base_url 不能指向云元数据地址（169.254.x.x / metadata），这是安全隐患"
        )
    warnings: list[str] = []
    host = parts.hostname or ""
    if not _is_loopback(host):
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and address.is_private:
            warnings.append(f"{host} 是内网地址，请确认确实是你自己的模型服务")
        elif address is not None and address.is_link_local:
            warnings.append(f"{host} 是链路本地地址")
    return warnings


def normalize_capabilities(values: Iterable[str] | None) -> list[str]:
    if not values:
        return ["chat"]
    seen: list[str] = []
    for value in values:
        item = str(value).strip().lower()
        if not item:
            continue
        if item not in CAPABILITIES:
            raise ProviderValidationError(
                f"未知能力 {item!r}，只能是 {', '.join(CAPABILITIES)}"
            )
        if item not in seen:
            seen.append(item)
    if not seen:
        raise ProviderValidationError("capabilities 至少要有 1 项")
    return seen


def validate_name(name: str) -> str:
    text = (name or "").strip()
    if not 1 <= len(text) <= 40:
        raise ProviderValidationError("名称长度必须是 1-40 个字符")
    return text


def validate_kind(kind: str) -> str:
    text = (kind or "").strip().lower()
    if text not in KINDS:
        raise ProviderValidationError(f"kind 只能是 {' / '.join(KINDS)}")
    return text


def validate_model(model: str) -> str:
    text = (model or "").strip()
    if not text:
        raise ProviderValidationError("model 不能为空")
    if len(text) > 120:
        raise ProviderValidationError("model 名字太长（上限 120）")
    return text


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


class ProviderRegistry:
    """provider 表的读写 + 路由配置。所有写操作立刻落库。"""

    def __init__(self, db: Database):
        self.db = db
        self.ensure_builtin()

    # -- 内置 ------------------------------------------------------------

    def ensure_builtin(self) -> Provider:
        """保证内置 echo 一定存在（幂等）。"""
        existing = self.get(BUILTIN_ECHO_ID)
        if existing is not None:
            return existing
        now = utc_now()
        with self.db._lock, self.db.conn:
            self.db.conn.execute(
                """
                INSERT OR IGNORE INTO provider(
                    id, name, kind, base_url, model, api_key, enabled,
                    capabilities, status, is_builtin, last_checked_at,
                    latency_ms, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    BUILTIN_ECHO_ID,
                    BUILTIN_ECHO_NAME,
                    "echo",
                    "",
                    "echo",
                    "",
                    1,
                    json.dumps(["chat", "extract"]),
                    # 内置 echo 从没做过连通性测试，如实标 unknown，
                    # 免得凭 latiency=0 在能力分配里压过真实测过的提供商。
                    "unknown",
                    1,
                    None,
                    None,
                    None,
                    now,
                    now,
                ),
            )
        created = self.get(BUILTIN_ECHO_ID)
        if created is None:  # pragma: no cover - 理论不可达
            raise ProviderError("内置提供商初始化失败")
        return created

    # -- 读 --------------------------------------------------------------

    @staticmethod
    def _row(row: Any) -> Provider:
        return Provider(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            base_url=row["base_url"],
            model=row["model"],
            api_key=row["api_key"] or "",
            enabled=bool(row["enabled"]),
            capabilities=json.loads(row["capabilities"] or '["chat"]'),
            status=row["status"] or "unchecked",
            is_builtin=bool(row["is_builtin"]),
            last_checked_at=row["last_checked_at"],
            latency_ms=row["latency_ms"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list(self) -> list[Provider]:
        with self.db._lock:
            rows = self.db.conn.execute(
                "SELECT * FROM provider ORDER BY is_builtin DESC, created_at ASC"
            ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, provider_id: str) -> Provider | None:
        with self.db._lock:
            row = self.db.conn.execute(
                "SELECT * FROM provider WHERE id=?", (provider_id,)
            ).fetchone()
        return None if row is None else self._row(row)

    def require(self, provider_id: str) -> Provider:
        provider = self.get(provider_id)
        if provider is None:
            raise ProviderNotFound(f"找不到提供商：{provider_id}")
        return provider

    def count(self) -> int:
        with self.db._lock:
            return int(
                self.db.conn.execute("SELECT COUNT(*) FROM provider").fetchone()[0]
            )

    def _name_taken(self, name: str, *, exclude_id: str | None = None) -> bool:
        with self.db._lock:
            row = self.db.conn.execute(
                "SELECT id FROM provider WHERE name=?", (name,)
            ).fetchone()
        return row is not None and row["id"] != exclude_id

    # -- 写 --------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        kind: str,
        base_url: str,
        model: str,
        api_key: str = "",
        capabilities: Iterable[str] | None = None,
        enabled: bool = True,
    ) -> tuple[Provider, list[str]]:
        """新建自定义提供商。返回 (provider, warnings)。"""
        if self.count() >= MAX_PROVIDERS:
            raise ProviderLimitExceeded(
                f"最多只能有 {MAX_PROVIDERS} 个提供商，请先删除不用的"
            )
        name = validate_name(name)
        kind = validate_kind(kind)
        model = validate_model(model)
        warnings = validate_base_url(base_url)
        caps = normalize_capabilities(capabilities)
        if self._name_taken(name):
            raise ProviderConflict(f"名称 {name!r} 已经用过了，换一个")

        provider_id = str(uuid.uuid4())
        now = utc_now()
        with self.db._lock, self.db.conn:
            self.db.conn.execute(
                """
                INSERT INTO provider(
                    id, name, kind, base_url, model, api_key, enabled,
                    capabilities, status, is_builtin, last_checked_at,
                    latency_ms, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider_id,
                    name,
                    kind,
                    base_url.strip(),
                    model,
                    api_key or "",
                    1 if enabled else 0,
                    json.dumps(caps),
                    "unchecked",
                    0,
                    None,
                    None,
                    None,
                    now,
                    now,
                ),
            )
        return self.require(provider_id), warnings

    def update(
        self,
        provider_id: str,
        *,
        name: str | None = None,
        kind: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        api_key: Any = ...,
        capabilities: Iterable[str] | None = None,
        enabled: bool | None = None,
    ) -> tuple[Provider, list[str]]:
        """更新提供商。

        ``api_key`` 的三种语义（契约）：
        * 不传 / 传空字符串 ``""`` → 不修改
        * 传 ``None``（JSON null）→ 清除
        * 传非空字符串 → 覆盖
        """
        provider = self.require(provider_id)
        warnings: list[str] = []
        fields: dict[str, Any] = {}

        if name is not None:
            name = validate_name(name)
            if self._name_taken(name, exclude_id=provider_id):
                raise ProviderConflict(f"名称 {name!r} 已经用过了，换一个")
            fields["name"] = name
        if kind is not None:
            fields["kind"] = validate_kind(kind)
        if base_url is not None:
            warnings = validate_base_url(base_url)
            fields["base_url"] = base_url.strip()
        if model is not None:
            fields["model"] = validate_model(model)
        if capabilities is not None:
            fields["capabilities"] = json.dumps(normalize_capabilities(capabilities))
        if enabled is not None:
            fields["enabled"] = 1 if enabled else 0
        if api_key is not ...:
            if api_key is None:
                fields["api_key"] = ""
            elif str(api_key).strip():
                fields["api_key"] = str(api_key).strip()
            # 空字符串 = 不修改，什么也不做

        if fields:
            fields["updated_at"] = utc_now()
            # 改了连接参数就重算状态，避免显示过期的 ok
            if {"base_url", "kind", "model", "api_key"} & set(fields):
                fields["status"] = "unchecked"
            columns = ", ".join(f"{key}=?" for key in fields)
            with self.db._lock, self.db.conn:
                self.db.conn.execute(
                    f"UPDATE provider SET {columns} WHERE id=?",
                    (*fields.values(), provider_id),
                )
        return self.require(provider_id), warnings

    def delete(self, provider_id: str) -> None:
        provider = self.require(provider_id)
        if provider.is_builtin:
            raise ProviderForbidden("内置提供商不能删除")
        with self.db._lock, self.db.conn:
            self.db.conn.execute("DELETE FROM provider WHERE id=?", (provider_id,))
            # 同时把它从路由配置里摘掉，避免悬空引用
            routing = self.routing()
            changed = False
            if routing.active_provider_id == provider_id:
                routing.active_provider_id = None
                changed = True
            if provider_id in routing.enabled_ids:
                routing.enabled_ids = [i for i in routing.enabled_ids if i != provider_id]
                changed = True
            for task, assigned in list(routing.assignments.items()):
                if assigned == provider_id:
                    routing.assignments.pop(task)
                    changed = True
            if changed:
                self._write_routing(routing)

    # -- 路由配置 --------------------------------------------------------

    def routing(self) -> Routing:
        with self.db._lock:
            row = self.db.conn.execute(
                "SELECT * FROM routing WHERE id='default'"
            ).fetchone()
        if row is None:
            return Routing()
        return Routing(
            mode=row["mode"],
            active_provider_id=row["active_provider_id"],
            enabled_ids=json.loads(row["enabled_ids"] or "[]"),
            assignments=json.loads(row["assignments"] or "{}"),
        )

    def _write_routing(self, routing: Routing) -> None:
        with self.db._lock, self.db.conn:
            self.db.conn.execute(
                """
                INSERT INTO routing(id, mode, active_provider_id, enabled_ids, assignments, updated_at)
                VALUES ('default', ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    mode=excluded.mode,
                    active_provider_id=excluded.active_provider_id,
                    enabled_ids=excluded.enabled_ids,
                    assignments=excluded.assignments,
                    updated_at=excluded.updated_at
                """,
                (
                    routing.mode,
                    routing.active_provider_id,
                    json.dumps(routing.enabled_ids),
                    json.dumps(routing.assignments),
                    utc_now(),
                ),
            )

    def set_routing(
        self,
        *,
        mode: str,
        active_provider_id: str | None = None,
        enabled_ids: Sequence[str] | None = None,
    ) -> Routing:
        """保存路由配置并重新计算 assignments。校验失败抛 ProviderValidationError。"""
        mode = (mode or "").strip().lower()
        if mode not in MODES:
            raise ProviderValidationError("mode 只能是 single 或 concurrent")

        if mode == "single":
            if not active_provider_id:
                raise ProviderValidationError("single 模式必须指定 active_provider_id")
            provider = self.get(active_provider_id)
            if provider is None:
                raise ProviderValidationError(
                    f"找不到提供商：{active_provider_id}"
                )
            if not provider.enabled:
                raise ProviderValidationError(
                    f"提供商 {provider.name!r} 已被停用，请先启用"
                )
            routing = Routing(
                mode="single",
                active_provider_id=active_provider_id,
                enabled_ids=[active_provider_id],
                assignments={task: active_provider_id for task in TASK_TYPES},
            )
            self._write_routing(routing)
            return routing

        # concurrent
        ids = [str(i) for i in (enabled_ids or [])]
        if not 1 <= len(ids) <= MAX_ENABLED:
            raise ProviderValidationError(
                f"concurrent 模式需要启用 1-{MAX_ENABLED} 个提供商，收到 {len(ids)} 个"
            )
        if len(set(ids)) != len(ids):
            raise ProviderValidationError("enabled_ids 里有重复项")
        providers: list[Provider] = []
        for provider_id in ids:
            provider = self.get(provider_id)
            if provider is None:
                raise ProviderValidationError(f"找不到提供商：{provider_id}")
            if not provider.enabled:
                raise ProviderValidationError(
                    f"提供商 {provider.name!r} 已被停用，请先启用"
                )
            providers.append(provider)
        routing = Routing(
            mode="concurrent",
            active_provider_id=None,
            enabled_ids=ids,
            assignments=assign_capabilities(providers),
        )
        self._write_routing(routing)
        return routing

    # -- 连通性测试 ------------------------------------------------------

    async def test(self, provider_id: str, *, timeout: float = TEST_TIMEOUT_SECONDS) -> dict[str, Any]:
        """连通性测试：走提供商自己的 list_models()，超时可配（默认 8s）。"""
        provider = self.require(provider_id)
        started = time.perf_counter()
        models: list[str] = []
        error: str | None = None
        ok = False
        try:
            instance = build_provider_instance(provider, timeout_seconds=timeout)
            models = await asyncio.wait_for(instance.list_models(), timeout=timeout)
            ok = True
        except asyncio.TimeoutError:
            error = f"超时（>{timeout:g}s）"
        except ModelError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - 任何异常都要如实上报
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = int((time.perf_counter() - started) * 1000)
        self._record_test(provider_id, ok=ok, latency_ms=latency_ms, error=error)
        return {
            "ok": ok,
            "latency_ms": latency_ms,
            "models": list(models or []),
            "error": error,
        }

    def _record_test(
        self, provider_id: str, *, ok: bool, latency_ms: int, error: str | None
    ) -> None:
        now = utc_now()
        with self.db._lock, self.db.conn:
            self.db.conn.execute(
                """
                UPDATE provider SET status=?, latency_ms=?, last_error=?,
                    last_checked_at=?, updated_at=? WHERE id=?
                """,
                (
                    "ok" if ok else "down",
                    latency_ms,
                    error,
                    now,
                    now,
                    provider_id,
                ),
            )

    def mark_status(self, provider_id: str, status: str, *, error: str | None = None) -> None:
        """记录一次调用结果（故障转移时用），不改变配置。"""
        if status not in STATUS_VALUES:
            return
        now = utc_now()
        with self.db._lock, self.db.conn:
            self.db.conn.execute(
                "UPDATE provider SET status=?, last_error=?, last_checked_at=?, updated_at=? WHERE id=?",
                (status, error, now, now, provider_id),
            )


def build_provider_instance(provider: Provider, *, timeout_seconds: float = 60.0) -> ModelProvider:
    """按注册表里的一行造出真正的提供商实例（复用 model_providers 的工厂）。"""
    if provider.kind == "echo":
        return EchoProvider(
            ProviderConfig(name="echo", base_url="", model=provider.model or "echo")
        )
    return create_provider(provider.to_provider_config(timeout_seconds=timeout_seconds))


# ---------------------------------------------------------------------------
# 能力分配
# ---------------------------------------------------------------------------


def _latency_key(provider: Provider) -> tuple[int, int]:
    """排序用：先按状态（ok 优先），再按延迟（None 视为很慢）。"""
    status_rank = 0 if provider.status == "ok" else 1
    latency = provider.latency_ms if provider.latency_ms is not None else 10**9
    return (status_rank, latency)


def assign_capabilities(providers: Sequence[Provider]) -> dict[str, str]:
    """按任务类型给 concurrent 模式分配提供商。

    * chat    → 具备 chat 能力里**最快响应**的（已探活优先）
    * extract → 同上（更快更便宜的先上）

    只分配 LLM 任务；image/tts 属于各自的后端（imagegen/speech），
    不在这里出现。返回 {task: provider_id}，只包含分配成功的任务。
    """
    assignments: dict[str, str] = {}
    ordered = sorted(providers, key=_latency_key)
    for task in TASK_TYPES:
        candidates = [p for p in ordered if task in p.capabilities]
        if candidates:
            assignments[task] = candidates[0].id
    return assignments


# ---------------------------------------------------------------------------
# 路由：选提供商 + 故障转移
# ---------------------------------------------------------------------------


class ProviderRouter:
    """把"任务要谁来做"翻译成具体提供商，并负责故障转移。"""

    def __init__(self, registry: ProviderRegistry, *, default: ModelProvider | None = None):
        self.registry = registry
        self.default = default

    def candidates(self, task: str = "chat") -> list[Provider]:
        """按当前路由配置列出该任务的候选提供商（有序，第一个优先）。"""
        routing = self.registry.routing()
        if routing.mode == "single":
            if not routing.active_provider_id:
                return []
            provider = self.registry.get(routing.active_provider_id)
            return [] if provider is None else [provider]

        # concurrent
        assigned_id = routing.assignments.get(task)
        ordered: list[Provider] = []
        if assigned_id:
            provider = self.registry.get(assigned_id)
            if provider is not None:
                ordered.append(provider)
        for provider_id in routing.enabled_ids:
            if provider_id == assigned_id:
                continue
            provider = self.registry.get(provider_id)
            if provider is None:
                continue
            # 故障转移只考虑能力对得上的
            if task in provider.capabilities:
                ordered.append(provider)
        return ordered

    def resolve(self, task: str = "chat") -> ModelProvider:
        """拿到该任务的**首选**提供商实例。无可用时抛 ModelUnavailable。"""
        candidates = self.candidates(task)
        if not candidates:
            raise ModelUnavailable(
                f"没有可用的提供商来处理 {task} 任务，请先在设置里配置路由"
            )
        return build_provider_instance(candidates[0])

    async def chat_with_failover(
        self,
        messages: list[dict[str, str]],
        system: str,
        *,
        task: str = "chat",
    ) -> tuple[str, Provider, list[dict[str, Any]]]:
        """执行 chat，concurrent 模式下失败自动换下一个。

        返回 (回复, 实际使用的提供商, 故障转移记录)。
        single 模式下失败直接抛出（路由层映射 503），不静默降级。
        """
        routing = self.registry.routing()
        candidates = self.candidates(task)
        if not candidates:
            if self.default is not None:
                # 还没配置路由时退回启动时的默认模型，保证老库里升级后仍能用。
                reply = await self.default.chat(messages, system)
                return reply, _SyntheticProvider(self.default), []
            raise ModelUnavailable(
                "还没有配置可用的模型提供商，请先在设置里选一个"
            )

        failovers: list[dict[str, Any]] = []
        if routing.mode == "single":
            provider = candidates[0]
            instance = build_provider_instance(provider)
            try:
                reply = await instance.chat(messages, system)
            except Exception as exc:  # noqa: BLE001
                # single 模式不静默降级：全部归一成 ModelUnavailable，
                # 让路由层稳定地映射成 503，而不是漏成 500。
                self.registry.mark_status(provider.id, "down", error=str(exc))
                code = getattr(exc, "code", type(exc).__name__)
                raise ModelUnavailable(
                    f"提供商 {provider.name!r} 调用失败（{code}）：{exc}"
                ) from exc
            self.registry.mark_status(provider.id, "ok", error=None)
            return reply, provider, failovers

        last_error: Exception | None = None
        for provider in candidates:
            instance = build_provider_instance(provider)
            try:
                reply = await instance.chat(messages, system)
            except Exception as exc:  # noqa: BLE001 - 任何失败都记下并换下一个
                last_error = exc
                self.registry.mark_status(provider.id, "down", error=str(exc))
                failovers.append(
                    {
                        "provider_id": provider.id,
                        "provider_name": provider.name,
                        "error": str(exc),
                        "error_code": getattr(exc, "code", type(exc).__name__),
                    }
                )
                continue
            if failovers:
                self.registry.mark_status(provider.id, "ok", error=None)
            return reply, provider, failovers

        detail = "; ".join(f"{f['provider_name']}: {f['error']}" for f in failovers)
        raise ModelUnavailable(f"所有提供商都失败了（{detail}）")


@dataclass(slots=True)
class _SyntheticProvider:
    """未配置路由时用于包装默认模型，只提供 out() 需要的字段。"""

    inner: ModelProvider

    @property
    def id(self) -> str:
        return "default"

    @property
    def name(self) -> str:
        return getattr(self.inner, "name", "default")

    def out(self) -> dict[str, Any]:
        return {
            "id": "default",
            "name": self.name,
            "kind": "builtin",
            "base_url": "",
            "model": self.name,
            "has_api_key": False,
            "enabled": True,
            "capabilities": ["chat"],
            "status": "unknown",
            "is_builtin": True,
            "last_checked_at": None,
            "latency_ms": None,
            "last_error": None,
        }


# ---------------------------------------------------------------------------
# runtime.json（契约第 4 节）
# ---------------------------------------------------------------------------


def runtime_path(data_dir: Path) -> Path:
    return Path(data_dir) / "runtime.json"


def write_runtime(
    data_dir: Path, *, port: int, token: str, pid: int | None = None
) -> Path:
    """后端启动时写 data/runtime.json，供 launcher 轮询。"""
    import os

    path = runtime_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "port": int(port),
        "token": token,
        "pid": int(pid if pid is not None else os.getpid()),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # 原子替换：launcher 不会读到半个文件
    try:
        os.chmod(path, 0o600)  # 里面有令牌，只给当前用户读
    except OSError:  # pragma: no cover - Windows 上可能不支持
        pass
    return path


def remove_runtime(data_dir: Path) -> None:
    """退出时删除 runtime.json。不存在也不算错。"""
    try:
        runtime_path(data_dir).unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover
        logger.warning("删除 runtime.json 失败：%s", exc)


__all__ = [
    "Provider",
    "Routing",
    "ProviderRegistry",
    "ProviderRouter",
    "ProviderError",
    "ProviderNotFound",
    "ProviderConflict",
    "ProviderForbidden",
    "ProviderLimitExceeded",
    "ProviderValidationError",
    "assign_capabilities",
    "build_provider_instance",
    "validate_base_url",
    "normalize_capabilities",
    "write_runtime",
    "remove_runtime",
    "runtime_path",
    "MAX_PROVIDERS",
    "MAX_ENABLED",
    "MODES",
    "KINDS",
    "CAPABILITIES",
    "TASK_TYPES",
    "BUILTIN_ECHO_ID",
    "TEST_TIMEOUT_SECONDS",
]
