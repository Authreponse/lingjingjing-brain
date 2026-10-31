from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .config import Settings
from .db import Database, utc_now
from .emotions import build_emotions_router
from .imagegen import build_service as build_image_service
from .models import EchoModel, ModelUnavailable, OpenAICompatibleModel
from .api_image import register_image_routes
from .proactive import build_proactive_service
from .proactive_routes import create_proactive_router
from .schemas import ChatRequest, ChatResponse, HealthResponse
from .service import build_memory_extract_router
from .screen import build_screen_service
from .screen_routes import register_screen_routes
from .service import BrainService
from .share_routes import register_share_routes
from .speech_routes import build_router_from_settings as build_speech_routes
from .state_machine import build_state_machine
from .state_routes import bind_state_routes, create_state_router
from .video import build_observer
from .video_routes import create_video_router


def create_app(settings: Settings | None = None, model=None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.db_path)
    if model is None:
        if settings.model_provider == "echo":
            model = EchoModel()
        elif settings.model_provider == "openai_compatible":
            model = OpenAICompatibleModel(
                settings.model_base_url,
                settings.model_name,
                settings.model_api_key,
                settings.model_timeout_seconds,
            )
        else:
            raise ValueError(f"未知模型驱动：{settings.model_provider}")
    machine = build_state_machine(db, settings)
    # chat 返回的状态与 GET /v1/state 走同一个投影，避免两套数值打架。
    service = BrainService(db, model, state_source=machine.current_state)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async def beat():
            while True:
                await asyncio.sleep(settings.heartbeat_seconds)
                # M1：心跳改成推进状态机（能量/饱腹/疲劳随时间衰减、睡眠恢复）。
                machine.heartbeat(actor="system")

        task = asyncio.create_task(beat())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            db.close()

    app = FastAPI(title="灵鲸鲸大脑", version=__version__, lifespan=lifespan)
    app.state.db = db
    app.state.service = service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:1420",
            "http://127.0.0.1:1420",
            # vite preview 默认端口，用于本地预览验收（仅回环）
            "http://localhost:4173",
            "http://127.0.0.1:4173",
            "http://tauri.localhost",
            "https://tauri.localhost",
            "tauri://localhost",
        ],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Ling-Token"],
    )

    async def authorized(x_ling_token: str = Header(default="")) -> None:
        if not secrets.compare_digest(x_ling_token, settings.auth_token):
            raise HTTPException(status_code=401, detail="认证失败")

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        """根路径不是界面：只说明端口用途，避免误以为服务挂了。"""
        return {
            "service": "灵鲸鲸大脑（后端 API）",
            "note": "这里是 API，不是界面。界面在 http://127.0.0.1:1420",
            "ui_url": "http://127.0.0.1:1420",
            "health": "/v1/health（需要 X-Ling-Token）",
            "docs": "/docs",
        }

    @app.get("/v1/capabilities", dependencies=[Depends(authorized)])
    async def capabilities() -> dict:
        """哪些能力已接真实后端、哪些还留空。界面用它决定要不要显示"未接入"。"""
        report = settings.capability_report()
        report["screen"] = {
            "configured": False,
            "hint": "真实截屏需要主人单独授权后开放",
        }
        return {
            "items": report,
            "configured_count": sum(1 for v in report.values() if v.get("configured")),
            "total": len(report),
        }

    @app.get("/v1/health", response_model=HealthResponse, dependencies=[Depends(authorized)])
    async def health() -> HealthResponse:
        if db.integrity_check() != "ok":
            raise HTTPException(status_code=503, detail="数据库完整性检查失败")
        return HealthResponse(
            version=__version__,
            schema_version=1,
            model_provider=model.name,
            time=utc_now(),
        )

    async def get_state() -> dict:
        return machine.current_state()

    @app.post("/v1/chat", response_model=ChatResponse, dependencies=[Depends(authorized)])
    async def chat(request: ChatRequest) -> ChatResponse:
        try:
            return await service.chat(request.message, request.client_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ModelUnavailable as exc:
            db.append_event("system", "model.error", {"message": str(exc)})
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/v1/events", dependencies=[Depends(authorized)])
    async def events(limit: int = Query(default=50, ge=1, le=200)) -> dict:
        return {"items": db.list_events(limit)}

    @app.get("/v1/memory/search", dependencies=[Depends(authorized)])
    async def memory_search(
        q: str = Query(min_length=1, max_length=200),
        limit: int = Query(default=10, ge=1, le=50),
    ) -> dict:
        return {"items": db.search_memory(q, limit)}

    @app.websocket("/v1/ws")
    async def websocket_chat(socket: WebSocket) -> None:
        token = socket.query_params.get("token", socket.headers.get("x-ling-token", ""))
        if not secrets.compare_digest(token, settings.auth_token):
            await socket.close(code=4401)
            return
        await socket.accept()
        try:
            while True:
                data = await socket.receive_json()
                try:
                    request = ChatRequest(**data)
                    response = await service.chat(request.message, request.client_id)
                    await socket.send_json(response.model_dump())
                except Exception as exc:
                    await socket.send_json({"error": str(exc)})
        except WebSocketDisconnect:
            return

    # ---- M1：状态机路由（bind 必须在 include_router 之前，就地替换 GET /v1/state） ----
    app.add_api_route(
        "/v1/state",
        get_state,
        methods=["GET"],
        dependencies=[Depends(authorized)],
        name="get_state",
    )
    bind_state_routes(app, machine, authorize=authorized)
    app.include_router(create_state_router(machine, authorize=authorized))

    # ---- M2：生图服务（无 ComfyUI 时自动落 MockBackend）+ 路由 ----
    image_service = build_image_service(settings, db)
    register_image_routes(app, image_service, authorized)
    # 表情包复用同一个 GPU 队列与每日预算
    app.include_router(build_emotions_router(settings, db, service=image_service))

    # ---- M3：一级语音（默认 Mock 后端，接真模型只换实现） ----
    build_speech_routes(app, settings, db, authorized, chat=service.chat)

    # ---- M4：私下分享队列（只发链接+评价，绝不 @ 人） ----
    register_share_routes(app, settings, db, machine, authorized=authorized)

    # ---- 记忆抽取调试接口（默认 dry_run，不污染真实记忆） ----
    app.include_router(build_memory_extract_router(settings, service))

    # ---- 主动话题与打扰预算（每天≤3次、安静时段静默、静默时默认不发） ----
    proactive_service = build_proactive_service(db, machine, settings)
    app.include_router(create_proactive_router(proactive_service, authorize=authorized))

    # ---- M4：视频观察（材料视为不可信输入；直播与超长视频明确跳过） ----
    app.include_router(
        create_video_router(build_observer(db, settings), authorize=authorized)
    )

    # ---- M5：静态看屏（默认关闭，暂停键持久，黑名单截前拦截） ----
    register_screen_routes(app, build_screen_service(db, settings), authorized)

    return app
