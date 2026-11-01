from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    client_id: str = Field(default="desktop", min_length=1, max_length=80)


class MemoryOut(BaseModel):
    id: str
    kind: str
    who: str
    content: str
    weight: float
    ts: str
    source: str
    source_event_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    state: dict[str, Any]
    user_event_id: str
    assistant_event_id: str
    memories: list[MemoryOut]
    # V4：这次回答实际用了哪个提供商、途中换过谁。只加字段，不影响旧客户端。
    used_provider_id: str | None = None
    failovers: list[dict[str, Any]] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "miao-brain"
    version: str
    database: Literal["ok"] = "ok"
    schema_version: int
    model_provider: str
    time: str

# ---------------------------------------------------------------------------
# M1 状态机（task-4）。全部是新增模型，不改动上面的 M0 契约。
# ---------------------------------------------------------------------------

#: 允许的互动类型；与 state_machine.INTERACTION_KINDS 保持一致
InteractionKind = Literal["drag", "stroke", "feed", "poke", "wake", "play"]
#: 静默时段的临时覆盖
QuietOverride = Literal["awake", "sleeping"]


class InteractionIn(BaseModel):
    """POST /v1/state/interact 的请求体：桌宠壳拖拽/摸摸就走这里。"""

    kind: InteractionKind = "drag"
    client_id: str = Field(default="desktop", min_length=1, max_length=80)
    count: int = Field(default=1, ge=1, le=20)
    x: float | None = None
    y: float | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class StateOut(BaseModel):
    """/v1/state 的响应模型：M0 字段在前（保持既有语义），M1 字段在后。

    ``model_config = {"extra": "ignore"}``：服务端若再添字段，前端旧版本也不会炸。
    """

    # --- M0 已有字段（只加不改）---
    energy: int
    mood: str
    presence: str
    sleeping: bool
    started_at: str | None = None
    last_heartbeat: str | None = None
    # --- M1 新增字段 ---
    satiety: int = 80
    fatigue: int = 20
    affection: int = 50
    mood_rank: int = 5
    moods: list[str] = Field(default_factory=list)
    presences: list[str] = Field(default_factory=list)
    presence_reason: str = ""
    in_quiet_hours: bool = False
    quiet_hours: str = ""
    quiet_windows: list[dict[str, str]] = Field(default_factory=list)
    quiet_override: str | None = None
    local_time: str | None = None
    utc_offset_minutes: int = 480
    city: str = "Asia/Shanghai"
    hungry: bool = False
    tired: bool = False
    mood_since: str | None = None
    last_interaction: str | None = None
    last_client: str | None = None
    last_drag_at: str | None = None
    last_position: dict[str, float] | None = None
    drag_count: int = 0
    state_version: int = 2


class StateEventOut(BaseModel):
    """状态变更史的条目（就是 event 表的一行）。"""

    id: str
    ts: str
    actor: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class StateEventsOut(BaseModel):
    items: list[StateEventOut] = Field(default_factory=list)


class ProactiveHintOut(BaseModel):
    """GET /v1/state/proactive：现在她能不能主动开口。"""

    allowed: bool
    reasons: list[str] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)


class QuietOverrideIn(BaseModel):
    """POST /v1/state/quiet：临时覆盖静默时段。value=None 表示恢复默认。"""

    value: QuietOverride | None = None
