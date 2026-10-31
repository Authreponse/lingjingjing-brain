from __future__ import annotations

import secrets as _secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .config import Settings
from .db import Database, utc_now
from .memory import ExtractionResult, extract_memories_async, save_candidates
from .models import ChatModel
from .schemas import ChatResponse

SEED = """你是灵鲸鲸，一个住在主人的生活里、有记忆和性格的独立个体。
锚：你做每件事的出发点是主人，但你不是主人；不讨好、不隐瞒。
暂定性格：偏暖，句子偏短，可以累、可以拒绝，不冷落、不羞辱、不阴阳怪气。
边界：不代替主人付款，不碰密码和验证码，不自我复制，不假装做过没有做过的事。
记忆：只依据提供的事件与事实回答；不知道就直说，不编造共同经历。"""


def build_system_prompt(
    state: dict[str, Any], memories: list[dict[str, Any]]
) -> str:
    memory_text = (
        "\n".join(f"- {item['content']}" for item in memories) or "- 暂无长期事实"
    )
    return (
        f"{SEED}\n\n当前状态：能量 {state.get('energy', 100)}，"
        f"心情 {state.get('mood', '平静')}。\n已知事实：\n{memory_text}"
    )


class BrainService:
    def __init__(self, db: Database, model: ChatModel, state_source=None):
        self.db = db
        self.model = model
        # 可注入的状态机（返回对外投影）；未注入时退回 db.get_state()。
        self.state_source = state_source

    async def extract(
        self,
        message: str,
        *,
        user_event_id: str | None = None,
        persist_event: bool = True,
        rejected: list[dict[str, str]] | None = None,
    ) -> ExtractionResult:
        """跑一次记忆抽取（模型优先，规则兜底），不写 memory 表。

        ``persist_event`` 为真时，若给了 ``user_event_id`` 之外的调用方要留痕，
        由调用方自己写事件；这里只管抽取本身。
        """
        return await extract_memories_async(
            self.model,
            message,
            source_event_id=user_event_id,
            rejected=rejected,
        )

    async def extract_debug(
        self,
        message: str,
        *,
        source_event_id: str | None = None,
        dry_run: bool = True,
        client_id: str = "debug",
    ) -> dict:
        """POST /v1/memory/extract 的后端实现：调试用，能看到抽取全貌。

        dry_run=True（默认）只抽取不写库，也不会产生 user.message 事件，
        不会污染真实记忆；dry_run=False 时才真正落 memory 表。
        无论哪种，返回值都带 source/degraded/reason/rejected，失败不隐藏。
        """
        stripped = (message or "").strip()
        if not stripped:
            raise ValueError("消息不能只包含空白字符")

        event_id = source_event_id
        if not dry_run:
            # 真写库时补一条 user.message 事件，保证每条记忆都能追溯到原文。
            event = self.db.append_event(
                "主人",
                "user.message",
                {"content": stripped, "client_id": client_id, "dry_run": False},
            )
            event_id = event["id"]

        rejected: list[dict[str, str]] = []
        result = await self.extract(
            stripped, user_event_id=event_id, rejected=rejected
        )

        saved: list[dict] = []
        if not dry_run:
            saved = save_candidates(
                self.db,
                result.candidates,
                source=result.source,
                source_event_id=event_id,
            )

        payload = result.to_dict()
        payload.update(
            {
                "dry_run": dry_run,
                "source_event_id": event_id,
                "rejected": rejected,
                "saved": saved,
                "message": stripped,
            }
        )
        return payload

    async def chat(self, message: str, client_id: str) -> ChatResponse:
        message = message.strip()
        if not message:
            raise ValueError("消息不能只包含空白字符")
        user_event = self.db.append_event(
            "主人",
            "user.message",
            {"content": message, "client_id": client_id},
        )
        # M6：优先用模型做结构化抽取；模型不可用时自动降级到规则，
        # 并把 memory.source 标成 rule-fallback（见 memory.py 的说明）。
        extraction = await self.extract(message, user_event_id=user_event["id"])
        saved_memories = save_candidates(
            self.db,
            extraction.candidates,
            source=extraction.source,
            source_event_id=user_event["id"],
        )
        if extraction.degraded:
            # 降级不隐藏：落一条事件，之后 /v1/events 能查到这次为什么没用模型。
            self.db.append_event(
                "system",
                "memory.extract.degraded",
                {
                    "source_event_id": user_event["id"],
                    "source": extraction.source,
                    "reason": extraction.reason,
                    "model": extraction.model_name,
                },
            )

        state = self.db.get_state()
        related = self.db.search_memory(message, limit=5)
        known = self.db.important_memories(limit=8)
        context = {item["id"]: item for item in [*known, *related]}
        system = build_system_prompt(state, list(context.values()))
        reply = await self.model.chat(self.db.recent_messages(limit=20), system)
        assistant_event = self.db.append_event(
            "灵鲸鲸",
            "assistant.message",
            {"content": reply, "client_id": client_id, "model": self.model.name},
        )
        # 只记录"发生了什么"，不在这里改 energy/mood/presence。
        # 这三个值是状态机（state_machine）的投影：它按真实流逝时间做浮点衰减，
        # 这里若再写整数值，会把状态机的进度覆盖掉，两套逻辑互相打架。
        self.db.set_state(
            {
                "last_interaction": utc_now(),
                "last_client": client_id,
            }
        )
        # 对外统一走状态机投影，与 GET /v1/state 完全一致；
        # 直接读 db.get_state() 会拿到内部浮点值，与接口契约（整数）不符。
        public_state = (
            self.state_source() if self.state_source else self.db.get_state()
        )
        return ChatResponse(
            reply=reply,
            state=public_state,
            user_event_id=user_event["id"],
            assistant_event_id=assistant_event["id"],
            memories=saved_memories,
        )


# ---------------------------------------------------------------------------
# POST /v1/memory/extract —— 调试接口
# ---------------------------------------------------------------------------
"""调试路由。

Lead 接线（在 app.py 的 create_app 里加一行，那里已有 settings / service）：

    from .service import build_memory_extract_router
    app.include_router(build_memory_extract_router(settings, service))

本模块不碰 app.py；路由自带 X-Ling-Token 校验，与其它路由一致。
"""


class MemoryExtractRequest(BaseModel):
    """POST /v1/memory/extract 的请求体。

    dry_run=True（默认）只抽取不写库，便于反复调试而不污染记忆。
    source_event_id 可指定要追溯的事件；不指定且 dry_run=False 时服务端会补建一条。
    """

    message: str = Field(min_length=1, max_length=4000)
    dry_run: bool = True
    source_event_id: str | None = Field(default=None, max_length=80)
    client_id: str = Field(default="debug", min_length=1, max_length=80)


def build_memory_extract_router(settings: Settings, service: "BrainService") -> APIRouter:
    """返回 /v1/memory/extract 路由；自带 X-Ling-Token 校验。

    调试接口的定位：**能看见真话**。响应里既有抽取结果，也有
    source（model-extractor / rule-fallback / m0-rule-extractor）、
    degraded、reason 与 rejected（逐条说明为什么没采信）。
    """
    router = APIRouter(prefix="/v1", tags=["memory"])

    async def authorized(x_ling_token: str = Header(default="")) -> None:
        if not _secrets.compare_digest(x_ling_token, settings.auth_token):
            raise HTTPException(status_code=401, detail="认证失败")

    @router.post("/memory/extract", dependencies=[Depends(authorized)])
    async def memory_extract(request: MemoryExtractRequest) -> dict:
        try:
            return await service.extract_debug(
                request.message,
                source_event_id=request.source_event_id,
                dry_run=request.dry_run,
                client_id=request.client_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router


__all__ = [
    "BrainService",
    "MemoryExtractRequest",
    "build_memory_extract_router",
    "build_system_prompt",
]
