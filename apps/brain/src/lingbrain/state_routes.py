"""灵鲸鲸 M1 状态外显接口（FastAPI 路由，由 app.py 挂载）。

本模块**不改动** app.py 里已有的任何路由；它只是提供一个可挂载的路由工厂，
以及一个"安全替换 /v1/state"的辅助函数。接线方式（Lead 在 app.py 里做）：

    from .state_machine import build_state_machine
    from .state_routes import create_state_router, bind_state_routes

    machine = build_state_machine(db, settings)
    app.state.machine = machine
    # 1) 把既有 GET /v1/state 就地替换成"先推进时间再返回全量字段"的版本；
    #    契约不变，只加字段。这一步必须在 include_router 之前做。
    bind_state_routes(app, machine, authorize=authorized)
    # 2) 挂上新增的状态路由（/v1/state/events、/v1/state/interact 等）。
    app.include_router(create_state_router(machine, authorize=authorized))
    # 3) 心跳：把 lifespan 里的 db.heartbeat() 换成 machine.heartbeat()。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from fastapi import APIRouter, Depends, Query
from fastapi.dependencies.utils import get_dependant, get_parameterless_sub_dependant

from .schemas import (
    InteractionIn,
    ProactiveHintOut,
    QuietOverrideIn,
    StateEventOut,
    StateEventsOut,
    StateOut,
)
from .state_machine import StateMachine


def _ok(state: dict[str, Any]) -> dict[str, Any]:
    """统一包一层：``{"state": {...}}``。保留外层是为了以后加字段不破坏契约。"""
    return {"state": state}


def create_state_router(
    machine: StateMachine,
    *,
    authorize: Callable[..., Any] | None = None,
) -> APIRouter:
    """构造状态路由。

    ``authorize`` 传 app.py 里的那个 FastAPI 依赖函数（``async def authorized(...)``）；
    传入后所有新路由都会挂 ``Depends(authorize)``，与既有接口走同一套 X-Ling-Token 校验。
    """
    dependencies = [Depends(authorize)] if authorize is not None else []
    router = APIRouter(dependencies=dependencies, tags=["state"])

    # -- 1. 状态变更史 ----------------------------------------------------
    @router.get("/v1/state/events", response_model=StateEventsOut)
    async def state_events(
        limit: int = Query(default=50, ge=1, le=200),
        kinds: str | None = Query(
            default=None,
            description="逗号分隔的 kind 前缀，默认 state.（即全部状态事件）",
        ),
    ) -> StateEventsOut:
        """状态变更史：最新在前。默认只回 state.* 事件，不影响 /v1/events。"""
        prefixes: tuple[str, ...] | None = None
        if kinds:
            parsed: Iterable[str] = tuple(
                part.strip() for part in kinds.split(",") if part.strip()
            )
            if parsed:
                prefixes = tuple(parsed)
        items = machine.history(limit=limit, kinds=prefixes)
        return StateEventsOut(items=[StateEventOut(**item) for item in items])

    # -- 2. 互动（拖拽 / 摸摸 / 投喂 / 戳）--------------------------------
    @router.post("/v1/state/interact", response_model=StateOut)
    async def state_interact(request: InteractionIn) -> StateOut:
        """桌宠壳的拖拽、双击、投喂都走这里；返回互动之后的最新状态。"""
        state = machine.interact(
            request.kind,
            client_id=request.client_id,
            count=request.count,
            detail=request.detail,
            position={"x": request.x, "y": request.y}
            if (request.x is not None or request.y is not None)
            else None,
        )
        return StateOut(**state)

    @router.post("/v1/state/drag", response_model=StateOut)
    async def state_drag(request: InteractionIn | None = None) -> StateOut:
        """``POST /v1/state/interact {"kind":"drag"}`` 的别名，方便前端直接调。"""
        request = request or InteractionIn()
        state = machine.drag(
            client_id=request.client_id,
            x=request.x,
            y=request.y,
            detail=request.detail,
        )
        return StateOut(**state)

    # -- 3. 睡觉 / 叫醒 / 静默覆盖 ---------------------------------------
    @router.post("/v1/state/sleep", response_model=StateOut)
    async def state_sleep() -> StateOut:
        return StateOut(**machine.sleep())

    @router.post("/v1/state/wake", response_model=StateOut)
    async def state_wake() -> StateOut:
        """叫醒：同时把静默时段临时覆盖成"在场"（她应声了）。"""
        return StateOut(**machine.wake())

    @router.post("/v1/state/quiet", response_model=StateOut)
    async def state_quiet(request: QuietOverrideIn) -> StateOut:
        """临时覆盖静默时段：awake 强行在场 / sleeping 强制睡 / null 恢复默认。"""
        return StateOut(**machine.set_quiet_override(request.value))

    # -- 4. 主动行为闸门 --------------------------------------------------
    @router.get("/v1/state/proactive", response_model=ProactiveHintOut)
    async def state_proactive() -> ProactiveHintOut:
        """现在她能不能主动开口（静默时段、睡着、太饿太累都会返回 false + 理由）。"""
        return ProactiveHintOut(**machine.proactive_hint())

    # -- 5. 手动推进（联调用）---------------------------------------------
    @router.post("/v1/state/tick", response_model=StateOut)
    async def state_tick() -> StateOut:
        """手动推进一次衰减（等价于一次心跳），联调和运维排障时方便。"""
        return StateOut(**machine.heartbeat(actor="manual"))

    return router


def bind_state_routes(
    app: Any,
    machine: StateMachine,
    *,
    authorize: Callable[..., Any] | None = None,
) -> Callable[..., Any]:
    """把既有的 ``GET /v1/state`` 就地替换成状态机版本。

    **必须在 ``include_router(...)`` 之前调用**：FastAPI 的路由表是"先注册先匹配"，
    这里删掉原路由再插回同路径/同方法的新路由，保证 ``/v1/state`` 的 URL、
    方法、认证头、响应结构（一个 dict）全部不变，只是字段变多。

    返回新的处理函数，方便测试直接调用。
    """
    if authorize is None:
        raise ValueError("bind_state_routes 需要 app.py 里的 authorize 依赖函数")

    async def get_state_rich() -> dict:
        # 老行为等价于 db.get_state()；新行为先按流逝时间推进，再返回全量字段。
        return machine.current_state()

    # 就地改写已有的 GET /v1/state，而不是重建整张路由表。
    # 早期实现用 app.router.routes[:] = keep 整体替换，会把调用前
    # include 进来的其它路由（表情、语音、分享等）全部丢掉。
    replaced = 0
    for route in app.router.routes:
        if getattr(route, "path", None) == "/v1/state" and "GET" in getattr(
            route, "methods", set()
        ):
            route.endpoint = get_state_rich
            route.dependant = get_dependant(path="/v1/state", call=get_state_rich)
            route.dependant.dependencies.append(
                get_parameterless_sub_dependant(
                    depends=Depends(authorize), path="/v1/state"
                )
            )
            replaced += 1
    if replaced == 0:
        app.add_api_route(
            "/v1/state",
            get_state_rich,
            methods=["GET"],
            dependencies=[Depends(authorize)],
            name="get_state",
        )
    app.state.machine = machine
    return get_state_rich


__all__ = ["create_state_router", "bind_state_routes"]
