"""M5 静态看屏 HTTP 路由（由 app.py 挂载，本模块不改 app.py 的既有契约）。

接线方式（Lead 在 app.py 里做）::

    from .screen import build_screen_service
    from .screen_routes import register_screen_routes

    register_screen_routes(app, build_screen_service(db, settings), authorized)

端点一览（全部要求 X-Ling-Token）::

    GET  /v1/screen/status     开关、暂停、后端、今日张数、黑名单类别
    POST /v1/screen/enable     显式开启（默认关闭）
    POST /v1/screen/disable    关闭
    POST /v1/screen/pause      一键暂停（立刻生效且持久）
    POST /v1/screen/resume     解除暂停
    POST /v1/screen/capture    静态截一帧（一次一张）
    GET  /v1/screen/history    截屏/跳过/失败事件
    GET  /v1/screen/{id}/image 读回产物图片
    POST /v1/screen/{id}/analyze 把一帧变成上下文描述（不替主人操作）

错误不隐藏：拒绝与降级都用明确的 HTTP 状态码 + code + notice 返回，
前端可以据此直接给主人弹提示。
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .screen import (
    BackendFailed,
    BackendUnavailable,
    ScreenBlocked,
    ScreenDailyLimitReached,
    ScreenDisabled,
    ScreenError,
    ScreenPaused,
    ScreenRateLimited,
    ScreenService,
)


class CaptureRequest(BaseModel):
    """POST /v1/screen/capture 的请求体（都可不传）。"""

    reason: str = Field(default="理解上下文", max_length=200)
    client_id: str = Field(default="desktop", min_length=1, max_length=80)


class AnalyzeRequest(BaseModel):
    summary: str | None = Field(default=None, max_length=2000)


def _error(code: str, detail: str, status: int, **extra: Any) -> dict[str, Any]:
    """统一错误体：不带堆栈、不带令牌。notice 是给主人看的可见提示。"""
    payload: dict[str, Any] = {"error": code, "detail": detail, "status": status}
    payload.update(extra)
    return payload


def _raise_screen_error(exc: ScreenError) -> None:
    """把服务层异常映射成 HTTP 错误；黑名单命中时把提示语一并带出去。"""
    extra: dict[str, Any] = {"notice": str(exc)}
    if isinstance(exc, ScreenBlocked):
        extra["categories"] = exc.categories
        extra["skipped"] = True
    raise HTTPException(
        status_code=exc.http_status,
        detail=_error(exc.code, str(exc), exc.http_status, **extra),
    ) from exc


def register_screen_routes(
    app: FastAPI,
    service: ScreenService,
    authorized: Callable[..., Any],
) -> None:
    """把看屏路由挂到既有 app 上；认证沿用 app.py 注入的 authorized 依赖。"""
    app.state.screen_service = service
    guard = [Depends(authorized)]

    @app.get("/v1/screen/status", dependencies=guard)
    async def screen_status() -> dict:
        return service.status()

    @app.post("/v1/screen/enable", dependencies=guard)
    async def screen_enable() -> dict:
        """显式开启。默认关闭，必须由主人主动开启。"""
        return service.enable()

    @app.post("/v1/screen/disable", dependencies=guard)
    async def screen_disable() -> dict:
        return service.disable()

    @app.post("/v1/screen/pause", dependencies=guard)
    async def screen_pause() -> dict:
        """一键暂停：同步落库，暂停后不再截屏。"""
        return service.pause()

    @app.post("/v1/screen/resume", dependencies=guard)
    async def screen_resume() -> dict:
        return service.resume()

    @app.post("/v1/screen/capture", dependencies=guard)
    async def screen_capture(request: CaptureRequest | None = None) -> dict:
        """静态截一帧。暂停/未开启/命中黑名单都会返回明确错误 + notice。"""
        request = request or CaptureRequest()
        try:
            result = service.capture(reason=request.reason, client_id=request.client_id)
        except ScreenError as exc:
            _raise_screen_error(exc)
            raise  # pragma: no cover - _raise_screen_error 一定会抛
        return result.to_dict()

    @app.get("/v1/screen/history", dependencies=guard)
    async def screen_history(
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict:
        items = service.history(limit=limit)
        return {"items": items, "limit": limit}

    @app.get("/v1/screen/{capture_id}/analyze", dependencies=guard)
    async def screen_analyze_get(capture_id: str) -> dict:
        try:
            return service.analyze(capture_id)
        except ScreenError as exc:
            _raise_screen_error(exc)
            raise  # pragma: no cover

    @app.post("/v1/screen/{capture_id}/analyze", dependencies=guard)
    async def screen_analyze(capture_id: str, request: AnalyzeRequest | None = None) -> dict:
        request = request or AnalyzeRequest()
        try:
            return service.analyze(capture_id, summary=request.summary)
        except ScreenError as exc:
            _raise_screen_error(exc)
            raise  # pragma: no cover

    @app.get("/v1/screen/{capture_id}/image", dependencies=guard)
    async def screen_image(capture_id: str):
        try:
            path = service.resolve_image(capture_id)
        except ScreenError as exc:
            _raise_screen_error(exc)
            raise  # pragma: no cover
        return FileResponse(path, media_type="image/png", filename=path.name)


__all__ = ["register_screen_routes", "CaptureRequest", "AnalyzeRequest"]
