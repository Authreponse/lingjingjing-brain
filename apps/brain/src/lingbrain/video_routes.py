"""灵鲸鲸 M4 视频观察 HTTP 路由（可挂载，不改 app.py 已有契约）。

接线方式（Lead 在 app.py 里做）：

    from .video import build_observer
    from .video_routes import create_video_router
    observer = build_observer(db, settings)   # 没配真实源就自动用 MockSource
    app.include_router(create_video_router(observer, authorize=authorized))

端点：

* GET  /v1/video/search      搜索（只读元数据 + 准入预览，不下载材料）
* POST /v1/video/observe     观察：准入 -> 取字幕/评论/关键帧 -> 写 video.observed + voice
* GET  /v1/video/status      来源、关键帧能力、降级统计、上限（明确标注 mock）
* GET  /v1/video/voices      她的观点列表

失败语义（不隐藏降级）：

* 来源不可用 -> 503 + detail.error=source_unavailable，带 reason 与给人看的 hint；
* 风控 / 限频 / 需要登录 -> 503，reason 分别是 risk_controlled / rate_limited / login_required；
* 准入拒绝（直播/超长/红线）-> 200 + admitted=false + reason，并写 video.skipped 事件；
* 单步证据取不到（字幕/评论/关键帧）-> 仍然 200，但在 degraded / degraded_reasons 里逐条说明；
* 参数不合法 / 重复观察 -> 422。

红线：本模块只有读操作，不存在点赞、发评论、@ 人、私信任何接口。
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .video import (
    DEGRADED_REASONS,
    MAX_DURATION_SECONDS,
    SourceUnavailable,
    VideoObserver,
)


class ObserveRequest(BaseModel):
    bvid: str = Field(min_length=1, max_length=64)
    want_subtitles: bool = True
    want_comments: bool = True
    #: 关键帧：None 跟随 observer 默认（settings.video_frames）；True/False 强制开关
    want_frames: bool | None = None
    #: 主人的提问/议题；给出时会用它作为观点 topic，否则用视频标题。
    claim: str | None = Field(default=None, max_length=200)
    #: 她自己的一句话立场；不给时用观察归纳出的第一印象。
    stance: str | None = Field(default=None, max_length=500)
    record_voice: bool = True


def _source_error(exc: SourceUnavailable) -> HTTPException:
    detail = {
        "error": "source_unavailable",
        "reason": exc.reason,
        "hint": DEGRADED_REASONS.get(exc.reason, ""),
        "retryable": exc.retryable,
        "detail": f"视频来源不可用，已降级；没有伪造任何观察结果。（{exc}）",
    }
    if exc.status_code is not None:
        detail["upstream_http_status"] = exc.status_code
    if exc.code is not None:
        detail["upstream_code"] = exc.code
    return HTTPException(status_code=503, detail=detail)


def create_video_router(
    observer: VideoObserver,
    *,
    authorize: Callable[..., Any] | None = None,
) -> APIRouter:
    """构造视频路由；authorize 传 app.py 里的依赖函数即复用 X-Ling-Token 校验。"""
    dependencies = [Depends(authorize)] if authorize is not None else []
    router = APIRouter(dependencies=dependencies, tags=["video"])

    @router.get("/v1/video/search")
    async def video_search(
        q: str = Query(min_length=1, max_length=100),
        limit: int = Query(default=10, ge=1, le=50),
    ) -> dict[str, Any]:
        try:
            return await observer.search(q, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SourceUnavailable as exc:
            observer.record_degraded("search", exc)
            raise _source_error(exc) from exc

    @router.post("/v1/video/observe")
    async def video_observe(request: ObserveRequest) -> dict[str, Any]:
        try:
            observation = await observer.observe(
                request.bvid,
                want_subtitles=request.want_subtitles,
                want_comments=request.want_comments,
                want_frames=request.want_frames,
                claim=request.claim,
                stance=request.stance,
                record_voice=request.record_voice,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SourceUnavailable as exc:
            observer.record_degraded("observe", exc)
            raise _source_error(exc) from exc
        return observation.to_dict()

    @router.get("/v1/video/status")
    async def video_status() -> dict[str, Any]:
        extractor = getattr(observer, "frame_extractor", None)
        frames = extractor.capability() if extractor is not None else {
            "available": False,
            "ffmpeg": None,
            "reason": "frame_unavailable",
            "hint": "未配置抽帧器",
        }
        source = observer.source
        stats = getattr(source, "stats", None)
        return {
            "source": source.name,
            "mock": observer.is_mock,
            "mock_notice": (
                "当前是 MockSource：数据是示例素材，不是真实平台内容，不能当作实机验收。"
                if observer.is_mock
                else "当前是真实来源的只读 HTTP 客户端（未登录态，只走公开只读接口）。"
            ),
            "max_duration_seconds": MAX_DURATION_SECONDS,
            "comment_limit": observer.comment_limit,
            "denied_topics": list(observer.denied_topics),
            "observed": len(observer.observations(limit=200)),
            "frames": frames,
            "want_frames": bool(getattr(observer, "want_frames", False)),
            "request_stats": dict(stats) if isinstance(stats, dict) else None,
            "degraded_reasons": dict(DEGRADED_REASONS),
            "capabilities": {
                "search": True,
                "metadata": True,
                "subtitles": True,
                "comments": True,
                "frames": bool(frames.get("available")),
                "public_actions": False,
            },
        }

    @router.get("/v1/video/voices")
    async def video_voices(
        limit: int = Query(default=50, ge=1, le=200),
        status: str | None = Query(default=None, max_length=32),
    ) -> dict[str, Any]:
        try:
            items = observer.list_voices(limit=limit, status=status)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"items": items}

    return router


__all__ = ["ObserveRequest", "create_video_router"]
