"""M6 主动话题 HTTP 路由（由 app.py 挂载，本模块不改 app.py 的既有契约）。

接线方式（Lead 在 app.py 里做）::

    from .proactive import build_proactive_service
    from .proactive_routes import create_proactive_router

    gate = getattr(app.state, "machine", None)      # state_machine.StateMachine
    service = build_proactive_service(db, gate, settings)
    app.include_router(create_proactive_router(service, authorize=authorized))

端点::

    GET  /v1/proactive/status   今日已用/剩余/下次可用时间/当前是否允许/下一条话题
    POST /v1/proactive/tick     手动做一次判断（调试用）：能发就发，不能发写明原因
    GET  /v1/proactive/history  最近的主动事件（发出/被拦/关闭）

原则：**不隐藏失败**。tick 即便没发出去，也返回 ``sent=false`` + ``blocked_reasons``，
调用方不应把"没发"当成"发了"。
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .proactive import ProactiveService


class TickRequest(BaseModel):
    """/v1/proactive/tick 的请求体（都可不传）。"""

    client_id: str = Field(default="debug", min_length=1, max_length=80)


def create_proactive_router(
    service: ProactiveService,
    *,
    authorize: Callable[..., Any] | None = None,
) -> APIRouter:
    """构造主动话题路由。``authorize`` 传 app.py 里的认证依赖函数。"""
    dependencies = [Depends(authorize)] if authorize is not None else []
    router = APIRouter(dependencies=dependencies, tags=["proactive"])

    @router.get("/v1/proactive/status")
    async def proactive_status() -> dict:
        """今日已用/剩余/下次可用时间，以及现在能不能主动。"""
        return service.status()

    @router.post("/v1/proactive/tick")
    async def proactive_tick(request: TickRequest | None = None) -> dict:
        """手动触发一次判断（调试用）。

        能发就发一条并返回 ``sent=true``；不能发也返回 200，
        但 ``sent=false`` 且 ``blocked_reasons`` 写明为什么 —— 失败不隐藏。
        """
        request = request or TickRequest()
        return service.tick(client_id=request.client_id)

    @router.get("/v1/proactive/history")
    async def proactive_history(
        limit: int = Query(default=20, ge=1, le=200),
    ) -> dict:
        return {"items": service.history(limit=limit), "limit": limit}

    return router


__all__ = ["create_proactive_router", "TickRequest"]
