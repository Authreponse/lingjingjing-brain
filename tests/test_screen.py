"""M5 静态看屏测试（task-12）。

每条立项书 3.11.2 安全要求都有专门的测试，测试名里带编号：
  REQ1 静态一次一张 / REQ2 一键暂停立刻生效且持久 / REQ3 黑名单跳过并提示
  REQ4 默认关闭 / REQ5 Mock 后端 + 真截屏默认不启用 / REQ6 可见提示
  REQ7 只理解上下文、不替主人操作 / 以及事件留痕与 HTTP 路由契约。

测试不碰真实屏幕：只使用 MockScreenBackend / RealScreenBackend（后者恒不可用）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from lingbrain.db import Database
from lingbrain.screen import (
    DAILY_LIMIT,
    DEFAULT_BLACKLIST,
    EVENT_CAPTURE,
    EVENT_FAILED,
    EVENT_SKIPPED,
    ENABLED_KEY,
    PAUSE_KEY,
    BackendUnavailable,
    MockScreenBackend,
    RealScreenBackend,
    ScreenBlocked,
    ScreenDailyLimitReached,
    ScreenDisabled,
    ScreenError,
    ScreenPaused,
    ScreenRateLimited,
    ScreenService,
    WindowInfo,
    build_screen_service,
    check_blacklist,
    describe_hits,
)
from lingbrain.screen_routes import register_screen_routes

TOKEN = "test-token"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def make_db(tmp_path) -> Database:
    return Database(tmp_path / "screen.db")


def make_service(tmp_path, *, window=None, backend=None, **kwargs) -> ScreenService:
    db = make_db(tmp_path)
    backend = backend or MockScreenBackend(window=window)
    return ScreenService(db, data_dir=tmp_path, backend=backend, **kwargs)


def enabled_service(tmp_path, *, window=None, backend=None, **kwargs) -> ScreenService:
    service = make_service(tmp_path, window=window, backend=backend, **kwargs)
    service.enable()
    return service


@pytest.fixture
def service(tmp_path) -> ScreenService:
    svc = enabled_service(tmp_path)
    yield svc
    svc.db.close()


def event_kinds(service: ScreenService, limit: int = 100) -> list[str]:
    return [item["kind"] for item in service.history(limit=limit)]


# ---------------------------------------------------------------------------
# REQ1：静态看屏，一次一张，不是实时
# ---------------------------------------------------------------------------


def test_req1_capture_is_static_single_frame(service):
    result = service.capture()
    # 明确标记为静态、非实时，且只产出一张图
    payload = result.to_dict()
    assert payload["is_static"] is True
    assert payload["is_realtime"] is False
    assert Path(result.path).exists()
    assert service.backend.capture_count == 1
    # 产物目录下只有这一个文件，没有循环截图留下的多帧
    files = list((service.data_dir / "screen").glob("*.png"))
    assert len(files) == 1


def test_req1_back_to_back_capture_is_throttled(service):
    service.capture()
    with pytest.raises(ScreenRateLimited) as excinfo:
        service.capture()
    assert "不是实时" in str(excinfo.value)
    # 被拒的那次不应该真的再截一张
    assert service.backend.capture_count == 1


def test_req1_capture_allowed_again_after_cooldown(tmp_path):
    svc = enabled_service(tmp_path, cooldown_seconds=30)
    start = datetime.now(timezone.utc)
    svc.capture(now=start)
    later = start + timedelta(seconds=31)
    result = svc.capture(now=later)
    assert result.id
    assert svc.backend.capture_count == 2


def test_req1_daily_limit_is_enforced(tmp_path):
    svc = enabled_service(tmp_path, cooldown_seconds=0, daily_limit=2)
    start = datetime.now(timezone.utc)
    svc.capture(now=start)
    svc.capture(now=start + timedelta(seconds=1))
    with pytest.raises(ScreenDailyLimitReached):
        svc.capture(now=start + timedelta(seconds=2))
    assert svc.backend.capture_count == 2


# ---------------------------------------------------------------------------
# REQ2：一键暂停，立刻生效且持久
# ---------------------------------------------------------------------------


def test_req2_pause_takes_effect_immediately(service):
    service.capture()
    service.pause()
    with pytest.raises(ScreenPaused):
        service.capture()


def test_req2_pause_persists_across_restart(tmp_path):
    svc = enabled_service(tmp_path, cooldown_seconds=0)
    svc.capture()
    svc.pause()
    # 模拟重启：新 Database 实例读同一份库，暂停状态必须还在
    db2 = Database(tmp_path / "screen.db")
    fresh = ScreenService(db2, data_dir=tmp_path, backend=MockScreenBackend())
    assert fresh.is_paused() is True
    with pytest.raises(ScreenPaused):
        fresh.capture()


def test_req2_pause_survives_new_service_with_env_enabled(tmp_path, monkeypatch):
    svc = enabled_service(tmp_path)
    svc.pause()
    monkeypatch.setenv("LING_SCREEN_ENABLED", "1")
    db2 = Database(tmp_path / "screen.db")
    fresh = build_screen_service(db2, None)
    # 环境变量只能"允许开启"，不能绕过暂停
    assert fresh.is_paused() is True


def test_req2_no_capture_happens_while_paused(service):
    service.pause()
    before = service.backend.capture_count
    with pytest.raises(ScreenPaused):
        service.capture()
    assert service.backend.capture_count == before  # 后端一次都没被调用


def test_req2_resume_restores_capture(service, tmp_path):
    service.pause()
    service.resume()
    result = service.capture()
    assert result.id
    assert service.is_paused() is False


def test_req2_pause_event_is_recorded(service):
    service.pause()
    assert "screen.paused" in event_kinds(service)


# ---------------------------------------------------------------------------
# REQ3：黑名单命中自动跳过，并明确提示主人（不得静默跳过）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, expected_category",
    [
        ("Chrome — 登录密码", "password"),
        ("请输入密码", "password"),
        ("Password Manager", "password"),
        ("短信验证码 - 中国移动", "verification_code"),
        ("两步验证", "verification_code"),
        ("招商银行 网上银行", "banking"),
        ("支付宝 - 收银台", "banking"),
        ("Chrome — 无痕模式", "private_window"),
        ("私聊 - 张三", "messaging_private"),
    ],
)
def test_req3_blacklist_categories_are_detected(title, expected_category):
    hits = check_blacklist(WindowInfo(title=title))
    assert hits, f"{title!r} 应该命中黑名单"
    assert expected_category in [h.category for h in hits]


def test_req3_blacklist_does_not_touch_normal_windows():
    assert check_blacklist(WindowInfo(title="VS Code - lingbrain")) == []
    assert check_blacklist(WindowInfo(title="终端")) == []


def test_req3_private_flag_alone_blocks():
    hits = check_blacklist(WindowInfo(title="没关系的标题", is_private=True))
    assert [h.category for h in hits] == ["private_window"]


def test_req3_blocked_capture_raises_with_notice_and_never_captures(tmp_path):
    svc = enabled_service(tmp_path, window=WindowInfo(title="请输入密码"))
    with pytest.raises(ScreenBlocked) as excinfo:
        svc.capture()
    exc = excinfo.value
    # 明确提示：消息和 notice 都必须说明"没截"以及原因
    assert "没有截" in str(exc)
    assert exc.notice
    assert "密码" in exc.notice
    assert exc.categories
    # 关键：命中黑名单时后端一次都不能被调用，屏幕内容不能落盘
    assert svc.backend.capture_count == 0
    assert list((tmp_path / "screen").glob("*.png")) == []


def test_req3_skip_is_never_silent_it_writes_an_event(tmp_path):
    svc = enabled_service(tmp_path, window=WindowInfo(title="验证码"))
    with pytest.raises(ScreenBlocked):
        svc.capture()
    skipped = [e for e in svc.history(50) if e["kind"] == EVENT_SKIPPED]
    assert skipped, "黑名单跳过必须留下事件，不能静默"
    payload = skipped[0]["payload"]
    assert payload["reason"] == "blacklist"
    assert payload["captured"] is False
    assert payload["notice"]  # 事件里也要有给主人看的提示
    assert "验证码" in payload["window_title"]


def test_req3_describe_hits_is_human_readable():
    hits = check_blacklist(WindowInfo(title="网银 密码"))
    text = describe_hits(hits)
    assert "没有截" in text
    assert "密码" in text and "网银" in text


# ---------------------------------------------------------------------------
# REQ4：默认关闭，未开启时调用被拒绝
# ---------------------------------------------------------------------------


def test_req4_disabled_by_default(tmp_path):
    svc = make_service(tmp_path)
    assert svc.is_enabled() is False
    assert svc.status()["enabled"] is False
    assert "默认关闭" in svc.status()["notice"]


def test_req4_capture_rejected_when_disabled_without_touching_backend(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(ScreenDisabled) as excinfo:
        svc.capture()
    assert "默认关闭" in str(excinfo.value)
    assert svc.backend.capture_count == 0


def test_req4_enable_then_capture_works(tmp_path):
    svc = make_service(tmp_path)
    svc.enable()
    assert svc.is_enabled() is True
    assert svc.capture().id


def test_req4_disable_closes_the_gate_again(service):
    service.disable()
    with pytest.raises(ScreenDisabled):
        service.capture()


def test_req4_factory_default_is_disabled(tmp_path):
    db = make_db(tmp_path)
    svc = build_screen_service(db, None)
    assert svc.is_enabled() is False


# ---------------------------------------------------------------------------
# REQ5：后端先做 Mock；真截屏接口保留但默认不启用
# ---------------------------------------------------------------------------


def test_req5_mock_backend_produces_a_real_png(service):
    result = service.capture()
    data = Path(result.path).read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(data) > 500
    # 结果必须明确标注是 Mock，不能冒充真实验证
    assert result.to_dict()["mock"] is True
    assert result.to_dict()["backend"] == "mock"


def test_req5_real_backend_is_never_available(tmp_path):
    backend = RealScreenBackend()
    assert backend.available() is False
    svc = make_service(tmp_path, backend=backend)
    svc.enable()
    with pytest.raises(BackendUnavailable):
        svc.capture()
    assert svc.status()["backend_available"] is False


def test_req5_real_backend_cannot_be_used_even_if_flag_set(tmp_path):
    backend = RealScreenBackend(enabled_by_owner=True)
    assert backend.available() is False
    with pytest.raises(BackendUnavailable):
        backend.capture(tmp_path / "x.png")


def test_req5_backend_unavailable_is_logged_not_hidden(tmp_path):
    svc = make_service(tmp_path, backend=RealScreenBackend())
    svc.enable()
    with pytest.raises(BackendUnavailable):
        svc.capture()
    assert EVENT_SKIPPED in event_kinds(svc)


def test_req5_backend_failure_is_reported(tmp_path):
    svc = enabled_service(tmp_path, backend=MockScreenBackend(fail="模拟后端崩了"))
    with pytest.raises(ScreenError):
        svc.capture()
    failed = [e for e in svc.history(50) if e["kind"] == EVENT_FAILED]
    assert failed, "后端失败必须写 screen.failed 事件，不能静默"
    assert "模拟后端崩了" in failed[0]["payload"]["error"]


# ---------------------------------------------------------------------------
# REQ6：每次截屏都有可见提示
# ---------------------------------------------------------------------------


def test_req6_successful_capture_has_visible_notice(service):
    result = service.capture()
    assert result.notice
    assert "一眼" in result.notice or "静态" in result.notice
    assert result.to_dict()["notice"] == result.notice


def test_req6_status_always_explains_state(service):
    assert service.status()["notice"]
    service.pause()
    assert "暂停" in service.status()["notice"]


def test_req6_capture_event_carries_notice(service):
    service.capture()
    captured = [e for e in service.history(50) if e["kind"] == EVENT_CAPTURE]
    assert captured
    assert captured[0]["payload"]["notice"]


# ---------------------------------------------------------------------------
# REQ7：只用于理解上下文，不替主人操作
# ---------------------------------------------------------------------------


def test_req7_analyze_never_takes_action(service):
    result = service.capture()
    analysis = service.analyze(result.id)
    assert analysis["action_taken"] is None
    assert analysis["can_operate"] is False
    assert analysis["purpose"] == "理解上下文"


def test_req7_module_exposes_no_input_injection():
    import lingbrain.screen as screen_module

    source = Path(screen_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("pyautogui", "pynput", "keyboard.", "win32api", "SendInput", "click("):
        assert forbidden not in source, f"看屏模块不应包含输入注入能力：{forbidden}"


def test_req7_analyze_unknown_id_is_an_error(service):
    with pytest.raises(ScreenError):
        service.analyze("不存在的id")


# ---------------------------------------------------------------------------
# 事件留痕：截屏与跳过都写事件
# ---------------------------------------------------------------------------


def test_events_are_written_for_capture_and_skip(tmp_path):
    svc = enabled_service(tmp_path)
    svc.capture()
    svc.backend.window = WindowInfo(title="请输入密码")
    with pytest.raises(ScreenBlocked):
        svc.capture(now=datetime.now(timezone.utc) + timedelta(seconds=999))
    kinds = event_kinds(svc)
    assert EVENT_CAPTURE in kinds
    assert EVENT_SKIPPED in kinds


def test_history_only_returns_screen_events(service):
    service.db.append_event("主人", "user.message", {"content": "你好"})
    service.capture()
    assert all(e["kind"].startswith("screen.") for e in service.history(50))


def test_image_path_is_confined_to_managed_dir(service):
    result = service.capture()
    path = service.resolve_image(result.id)
    assert path == Path(result.path).resolve()
    service.db.append_event(
        "灵鲸鲸",
        EVENT_CAPTURE,
        {"id": "evil", "path": str(Path(service.data_dir) / ".." / "etc" / "passwd")},
    )
    with pytest.raises(ScreenError):
        service.resolve_image("evil")


# ---------------------------------------------------------------------------
# HTTP 路由契约
# ---------------------------------------------------------------------------


def build_app(tmp_path, *, window=None) -> tuple[FastAPI, ScreenService]:
    db = make_db(tmp_path)
    svc = ScreenService(db, data_dir=tmp_path, backend=MockScreenBackend(window=window))
    app = FastAPI()

    async def authorized(x_ling_token: str = Header(default="")) -> None:
        if x_ling_token != TOKEN:
            raise HTTPException(status_code=401, detail="认证失败")

    register_screen_routes(app, svc, authorized)
    return app, svc


def headers() -> dict[str, str]:
    return {"X-Ling-Token": TOKEN}


def test_routes_require_auth(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/v1/screen/status").status_code == 401
        assert client.post("/v1/screen/capture").status_code == 401
        assert client.post("/v1/screen/pause").status_code == 401


def test_route_capture_disabled_returns_403_with_notice(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/screen/capture", headers=headers())
        assert response.status_code == 403
        body = response.json()["detail"]
        assert body["error"] == "screen_disabled"
        assert "默认关闭" in body["notice"]


def test_route_full_flow(tmp_path):
    app, svc = build_app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/v1/screen/enable", headers=headers()).json()["enabled"] is True
        captured = client.post("/v1/screen/capture", headers=headers(), json={"client_id": "shell"})
        assert captured.status_code == 200, captured.text
        payload = captured.json()
        assert payload["mock"] is True
        assert payload["is_realtime"] is False
        assert payload["notice"]

        image = client.get(f"/v1/screen/{payload['id']}/image", headers=headers())
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"
        assert image.content.startswith(b"\x89PNG")

        analysis = client.get(f"/v1/screen/{payload['id']}/analyze", headers=headers())
        assert analysis.json()["action_taken"] is None

        history = client.get("/v1/screen/history", headers=headers()).json()
        assert history["items"]

        assert client.post("/v1/screen/pause", headers=headers()).json()["paused"] is True
        blocked_by_pause = client.post("/v1/screen/capture", headers=headers())
        assert blocked_by_pause.status_code == 423
        assert blocked_by_pause.json()["detail"]["error"] == "screen_paused"


def test_route_blacklist_block_returns_notice(tmp_path):
    app, _ = build_app(tmp_path, window=WindowInfo(title="请输入密码"))
    with TestClient(app) as client:
        client.post("/v1/screen/enable", headers=headers())
        response = client.post("/v1/screen/capture", headers=headers())
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["error"] == "screen_blocked"
        assert detail["skipped"] is True
        assert "密码" in detail["notice"]
        assert detail["categories"]


def test_route_resume_and_disable(tmp_path):
    app, svc = build_app(tmp_path)
    with TestClient(app) as client:
        client.post("/v1/screen/enable", headers=headers())
        client.post("/v1/screen/pause", headers=headers())
        assert client.post("/v1/screen/resume", headers=headers()).json()["paused"] is False
        assert client.post("/v1/screen/disable", headers=headers()).json()["enabled"] is False
        assert client.post("/v1/screen/capture", headers=headers()).status_code == 403


def test_route_status_reports_static_not_realtime(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        body = client.get("/v1/screen/status", headers=headers()).json()
        assert body["is_static"] is True
        assert body["is_realtime"] is False
        assert body["blacklist_categories"]
