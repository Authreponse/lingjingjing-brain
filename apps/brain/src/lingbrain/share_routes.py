"""M4 分享路由的注册入口。

功能其实都写在 share.py 里；这个文件只提供与生图路由同款的「注册」风格，
让 Lead 在 app.py 里可以任选一种接线方式，不用改 share.py：

    # 方式 A（推荐，与 emotions 一致的 include_router）
    from .share import build_share_router
    app.include_router(build_share_router(settings, db, machine))

    # 方式 B（与 api_image.register_image_routes 一致的注册风格）
    from .share_routes import register_share_routes
    register_share_routes(app, settings, db, machine)

两种方式的端点、认证、行为完全一样：认证沿用 app.py 注入的 authorized（或路由自带）。
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI

from .config import Settings
from .db import Database
from .share import ProactiveGate, ShareService, build_share_router


def build_share_service(
    settings: Settings,
    db: Database,
    gate: ProactiveGate | None = None,
) -> ShareService:
    """按 Settings 组装分享服务；gate 缺省就从 state_machine 借一个（只读）。"""
    active_gate = gate
    if active_gate is None:
        from .state_machine import build_state_machine

        active_gate = build_state_machine(db, settings)
    return ShareService(
        db,
        active_gate,
        utc_offset_minutes=int(getattr(settings, "state_utc_offset_minutes", 8 * 60) or 8 * 60),
    )


def register_share_routes(
    app: FastAPI,
    settings: Settings,
    db: Database,
    gate: ProactiveGate | None = None,
    *,
    service: ShareService | None = None,
    authorized: Callable[..., Any] | None = None,
) -> ShareService:
    """把 /v1/share/* 端点挂到 app 上，返回装配好的 ShareService（app.state.share_service）。

    authorized 传了就用 app.py 的认证依赖（与其它路由一致）；不传则由路由自带
    X-Ling-Token 校验。两条路径的认证强度相同。
    """
    share_service = service or build_share_service(settings, db, gate)
    app.state.share_service = share_service
    if authorized is None:
        app.include_router(build_share_router(settings, db, gate, service=share_service))
        return share_service
    # 已经装配好 service 时复用同一份状态，避免两套实例各算各的额度。
    router = build_share_router(settings, db, gate, service=share_service)
    _override_route_dependencies(router, authorized)
    app.include_router(router)
    return share_service


def _override_route_dependencies(router, authorized: Callable[..., Any]) -> None:
    """调用方传入自己的认证依赖时替换掉路由默认的 X-Ling-Token 校验。"""
    from fastapi import Depends

    for route in router.routes:
        existing = list(getattr(route, "dependencies", []) or [])
        route.dependencies = [Depends(authorized), *existing]


__all__ = ["build_share_service", "register_share_routes"]
