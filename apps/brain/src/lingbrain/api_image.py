"""M2 生图 HTTP 路由：POST /v1/image/generate 与 GET /v1/artwork。

由 app.py 在 create_app 内调用 register_image_routes(app, service, authorized) 挂载，
本模块不改 app.py 的既有五个固定契约。认证沿用 X-Ling-Token（app.py 注入的依赖）。
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .gpu_queue import QueuePaused
from .imagegen import (
    ALLOWED_SIZES,
    ArtworkPathError,
    BudgetExhausted,
    ImageGenerationError,
    ImageGenerationService,
)


class ImageGenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1200)
    reason: str = Field(min_length=1, max_length=400)
    mood: str | None = Field(default=None, max_length=40)
    size: int = 1024
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    client_id: str = Field(default="desktop", min_length=1, max_length=80)

    def validate_size(self) -> None:
        if self.size not in ALLOWED_SIZES:
            raise ValueError(f"size 只接受 {sorted(ALLOWED_SIZES)}")


def _error_payload(code: str, detail: str, status: int) -> dict[str, Any]:
    return {"error": code, "detail": detail, "status": status}


def register_image_routes(
    app: FastAPI,
    service: ImageGenerationService,
    authorized: Callable[..., Any],
) -> None:
    app.state.image_service = service
    guard = [Depends(authorized)]

    @app.post("/v1/image/generate", dependencies=guard)
    async def image_generate(request: ImageGenerateRequest) -> dict:
        try:
            request.validate_size()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            result = await service.generate(
                request.prompt,
                reason=request.reason,
                mood=request.mood,
                size=request.size,
                seed=request.seed,
                requested_by=request.client_id,
            )
        except BudgetExhausted as exc:
            raise HTTPException(
                status_code=429,
                detail=_error_payload("image_budget_exhausted", str(exc), 429),
            ) from exc
        except QueuePaused as exc:
            raise HTTPException(
                status_code=503,
                detail=_error_payload("gpu_queue_paused", str(exc), 503),
            ) from exc
        except ImageGenerationError as exc:
            paused = service.queue.paused
            raise HTTPException(
                status_code=503,
                detail=_error_payload(
                    "gpu_queue_paused" if paused else "image_generation_failed",
                    str(exc),
                    503,
                ),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return result.to_dict()

    @app.get("/v1/artwork", dependencies=guard)
    async def list_artwork(
        limit: int = Query(default=50, ge=1, le=200),
        mood: str | None = Query(default=None, max_length=40),
        day: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ) -> dict:
        items = service.list_artwork(limit, mood=mood, day=day)
        return {"items": [item.to_dict() for item in items], "limit": limit}

    @app.get("/v1/artwork/{artwork_id}", dependencies=guard)
    async def get_artwork(artwork_id: str) -> dict:
        artwork = service.store.get(artwork_id)
        if artwork is None:
            raise HTTPException(status_code=404, detail="找不到这张作品")
        return artwork.to_dict()

    @app.get("/v1/image/status", dependencies=guard)
    async def image_status() -> dict:
        return service.status()

    @app.get("/v1/artwork/{artwork_id}/file", dependencies=guard)
    async def get_artwork_file(artwork_id: str):
        artwork = service.store.get(artwork_id)
        if artwork is None:
            raise HTTPException(status_code=404, detail="找不到这张作品")
        try:
            path = service.store.resolve(artwork.path)
        except ArtworkPathError as exc:
            raise HTTPException(status_code=409, detail="产物路径不在受管目录内") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="产物文件缺失")
        return FileResponse(path, media_type="image/png", filename=path.name)

    @app.post("/v1/image/resume", dependencies=guard)
    async def resume_queue() -> dict:
        """主人复核后手动恢复 GPU 队列（LJ-R011 的恢复路径）。"""
        service.queue.resume()
        return {"resumed": True, "queue": service.queue.status()}
