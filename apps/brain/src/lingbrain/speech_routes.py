"""灵鲸鲸 M3 一级语音 HTTP 路由（由 app.py 调 register_speech_routes 挂载）。

本模块不改 app.py 的既有契约，只提供：

- ``POST /v1/speech/asr``         音频 -> 转写（原始字节上传，不依赖 python-multipart）
- ``POST /v1/speech/tts``         文本 -> WAV 音频（返回元信息 + 音频地址）
- ``POST /v1/speech/voice-turn``  一级语音：音频 -> ASR -> chat -> 文本+语音回复
- ``GET  /v1/speech/audio/{id}``  取受管目录里的音频文件（Range 被忽略，整段返回）
- ``GET  /v1/speech/status``      后端与上限（明确标 mock / real_model=false）
- ``GET  /v1/speech/records``     最近语音留痕（JSON sidecar）

为什么不用 multipart：当前 venv 没有 ``python-multipart``，装了才存在的依赖会让
"/v1/speech/asr 本来能跑"变成"看环境脸色"。所以 ASR / voice-turn 收原始音频字节，
文件名与来源走查询参数（``filename=按住说话.wav&client_id=desktop``）。
接真实前端时如果换成 multipart，只需要在这里加一个 ``UploadFile`` 版本，
校验、落盘、错误语义全部复用 ``speech.SpeechService``。

错误一律是 ``{"status": "error", "error": {"code", "http_status", "detail"}}``，
状态码由 ``SpeechError.http_status`` 决定（409/413/415/422/503/504），绝不 500。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .speech import (
    AudioPathError,
    AudioTooLarge,
    SpeechError,
    SpeechService,
    build_speech_service,
)

#: 前端一直按住不放时，单条音频的硬上限（服务层还会按 speech_max_upload_bytes 再卡一次）。
HARD_UPLOAD_LIMIT_BYTES = 64 * 1024 * 1024

#: 允许的音频 MIME（宽松：前端有时给 application/octet-stream）。
_MIME_OK = re.compile(
    r"^(audio/[\w.+-]+|application/octet-stream|binary/octet-stream|\s*)$", re.IGNORECASE
)

#: 文件名只保留安全字符，防止被塞进日志/响应时带控制符。
_FILENAME_OK = re.compile(r"^[\w\u4e00-\u9fff\-.]+$")


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    client_id: str = Field(default="desktop", min_length=1, max_length=80)
    voice: str | None = Field(default=None, max_length=40)


def _query_filename(request: Request) -> str | None:
    value = request.query_params.get("filename")
    if value and _FILENAME_OK.match(value):
        return value
    return None


def _error_response(exc: SpeechError) -> dict[str, Any]:
    return {"status": "error", "error": exc.to_dict(), "notice": exc.detail}


async def _read_audio_body(request: Request) -> tuple[bytes, str | None]:
    """读原始上传体：先看 Content-Length 挡超大请求，再整体读入。"""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > HARD_UPLOAD_LIMIT_BYTES:
        raise HTTPException(status_code=413, detail=_error_response(_too_large(declared)))
    body = await request.body()
    if len(body) > HARD_UPLOAD_LIMIT_BYTES:
        raise HTTPException(status_code=413, detail=_error_response(_too_large(str(len(body)))))
    return body, request.headers.get("content-type")


def _require_audio_content_type(request: Request) -> str:
    """请求体必须是音频。不是就 415，并且错误体里的 http_status 必须自洽（不是默认的 422）。"""
    content_type = request.headers.get("content-type", "")
    if _MIME_OK.match(content_type or ""):
        return content_type
    exc = SpeechError("Content-Type 不是音频；请用 audio/wav 提交原始字节")
    exc.code = "unsupported_media_type"
    exc.http_status = 415
    raise HTTPException(status_code=415, detail=_error_response(exc))


def _too_large(declared: str) -> SpeechError:
    return AudioTooLarge(f"请求体 {declared} 字节，超过单条上限 {HARD_UPLOAD_LIMIT_BYTES} 字节")


def register_speech_routes(
    app: FastAPI,
    service: SpeechService,
    authorized: Callable[..., Any],
) -> APIRouter:
    """挂载 M3 语音路由。``authorized`` 传 app.py 里那个 X-Ling-Token 依赖。"""
    app.state.speech_service = service
    router = APIRouter(dependencies=[Depends(authorized)], tags=["speech"])

    @router.post("/v1/speech/asr")
    async def speech_asr(request: Request) -> dict:
        """按住说话的音频字节 -> 转写。文件名用来做确定性 Mock 文本。"""
        content_type = _require_audio_content_type(request)
        try:
            body, _ = await _read_audio_body(request)
            result = await service.transcribe_upload(
                body,
                filename=_query_filename(request),
                client_id=request.query_params.get("client_id", "desktop"),
                source=request.query_params.get("source", "hold_to_talk"),
                content_type=content_type,
            )
        except SpeechError as exc:
            raise HTTPException(status_code=exc.http_status, detail=_error_response(exc)) from exc
        return result

    @router.post("/v1/speech/tts")
    async def speech_tts(request: TtsRequest) -> dict:
        """文本 -> 语音。返回元信息与 audio_url，音频字节走 /v1/speech/audio/{id}。"""
        try:
            return await service.synthesize(
                request.text, source="api", client_id=request.client_id
            )
        except SpeechError as exc:
            raise HTTPException(status_code=exc.http_status, detail=_error_response(exc)) from exc

    @router.post("/v1/speech/voice-turn")
    async def speech_voice_turn(request: Request) -> dict:
        """一级语音完整链路：音频 -> ASR -> chat -> 文本+语音回复。

        ``status`` 不是 ``"ok"`` 就是明确降级（``asr_failed`` / ``chat_failed`` /
        ``tts_degraded`` 等），HTTP 状态码按失败环节给：ASR 失败沿用它的 4xx/5xx，
        chat 失败 503，仅 TTS 失败时返回 200 并在 ``status``/``notice`` 里说明。
        """
        content_type = _require_audio_content_type(request)
        try:
            body, _ = await _read_audio_body(request)
            result = await service.voice_turn(
                body,
                filename=_query_filename(request),
                client_id=request.query_params.get("client_id", "desktop"),
                content_type=content_type,
            )
        except SpeechError as exc:
            raise HTTPException(status_code=exc.http_status, detail=_error_response(exc)) from exc
        if result["status"] == "ok" or result["status"] == "tts_degraded":
            return result
        error = result.get("error") or {}
        raise HTTPException(status_code=int(error.get("http_status") or 503), detail=result)

    @router.get("/v1/speech/audio/{record_id}")
    async def speech_audio(record_id: str) -> FileResponse:
        try:
            path, record = service.open_audio(record_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="找不到这段音频") from exc
        except AudioPathError as exc:
            raise HTTPException(status_code=409, detail="音频路径不在受管目录内") from exc
        media_type = record.get("content_type") or "audio/wav"
        return FileResponse(path, media_type=media_type, filename=path.name)

    @router.get("/v1/speech/status")
    async def speech_status() -> dict:
        return service.status()

    @router.get("/v1/speech/records")
    async def speech_records(
        limit: int = Query(default=20, ge=1, le=200),
        kind: str | None = Query(default=None, pattern="^(input|output)$"),
    ) -> dict:
        return {"items": service.list_records(limit, kind=kind), "limit": limit}

    app.include_router(router)
    return router


def build_router_from_settings(
    app: FastAPI,
    settings: Any,
    db: Any,
    authorized: Callable[..., Any],
    *,
    chat: Any = None,
) -> SpeechService:
    """app.py 一行接入：组装服务（默认 Mock）并把路由挂上。"""
    service = build_speech_service(settings, db, chat=chat)
    register_speech_routes(app, service, authorized)
    return service


__all__ = [
    "HARD_UPLOAD_LIMIT_BYTES",
    "TtsRequest",
    "build_router_from_settings",
    "register_speech_routes",
]
