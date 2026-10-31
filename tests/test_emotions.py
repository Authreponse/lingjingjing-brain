"""task-6 表情包：选表情、触发词、imagegen 降级、artwork 关联、路由契约。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lingbrain.config import Settings
from lingbrain.db import Database
from lingbrain.emotions import (
    EMOTIONS,
    EMOTION_INDEX,
    EmotionLibrary,
    _ImageGenAdapter,
    build_emotions_router,
    choose_emotion,
    choose_self_portrait,
    detect_trigger,
    normalize_mood,
    recent_artworks,
    save_artwork,
)

TOKEN = "test-token"


def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, auth_token=TOKEN, heartbeat_seconds=3600)


def headers() -> dict[str, str]:
    return {"X-Ling-Token": TOKEN}


def make_db(tmp_path) -> Database:
    return Database(tmp_path / "emo.db")


class FakeResult:
    """模拟 task-5 generate() 的返回：既支持属性也支持映射键。"""

    def __init__(self, path, artwork_id="art-1", backend="mock", degraded=True, error=None):
        self.path = path
        self.artwork_id = artwork_id
        self.backend = backend
        self.degraded = degraded
        self.error = error


class FakeGenerator:
    """接口期望的正例：async generate(*, prompt, reason, mood)。"""

    def __init__(self, result_path="data/artwork/emo.png", artwork_id=None, raises=None):
        self.calls: list[dict] = []
        self.result_path = result_path
        self.artwork_id = artwork_id
        self.raises = raises
        self.backend = type("B", (), {"name": "mock"})()

    async def generate(self, *, prompt, reason, mood, emotion=None):
        self.calls.append({"prompt": prompt, "reason": reason, "mood": mood, "emotion": emotion})
        if self.raises is not None:
            raise self.raises
        return FakeResult(self.result_path, artwork_id=self.artwork_id)


class BudgetExceeded(Exception):
    pass


def adapter_with(generator, **exceptions) -> _ImageGenAdapter:
    adapter = _ImageGenAdapter(generator)
    adapter.export = {"module": object(), **exceptions}
    return adapter


# --- 目录与选择 ---------------------------------------------------------


def test_catalog_is_well_formed():
    ids = [item.id for item in EMOTIONS]
    assert len(ids) == len(set(ids)) >= 10
    for item in EMOTIONS:
        assert item.prompt.strip()
        assert item.emoji
        assert 0 <= item.energy_min <= item.energy_max <= 100


def test_choose_respects_mood_and_energy():
    assert choose_emotion("疲惫", 5).id == "sleepy"
    assert choose_emotion("睡眠", 0).id == "sleeping"
    assert choose_emotion("开心", 95).id == "joy_high"
    assert choose_emotion("饥饿", 30).id == "hungry"
    assert choose_emotion("沮丧", 20).id == "sad"


def test_choose_is_deterministic_and_supports_exclude():
    first = choose_emotion("平静", 50)
    assert choose_emotion("平静", 50).id == first.id
    second = choose_emotion("平静", 50, exclude=[first.id])
    assert second.id != first.id


def test_unknown_mood_still_chooses():
    assert choose_emotion("完全没听过的词", 50) is not None
    assert choose_emotion(None, None) is not None
    assert choose_emotion("平静", "坏数据") is not None


def test_explicit_prefer_wins():
    assert choose_emotion("平静", 80, prefer=["hungry"]).id == "hungry"


def test_normalize_mood():
    assert normalize_mood("高兴") == "开心"
    assert normalize_mood("累") == "疲惫"
    assert normalize_mood("") == "平静"


def test_self_portrait_candidates():
    portrait = choose_self_portrait("平静", 50)
    assert portrait.self_portrait_rank is not None
    low = choose_self_portrait("想睡", 3)
    assert low.id == "sleeping"


# --- 触发词 -------------------------------------------------------------


def test_self_portrait_triggers():
    for message in ("画个你自己", "画一下你自己", "给自己画个自画像", "你长什么样呀"):
        hit = detect_trigger(message)
        assert hit.kind == "self_portrait", message
        assert hit.matched and not hit.negated


def test_negated_trigger_does_not_fire():
    for message in ("别画你自己", "不要再画个你自己了吧", "不用画表情包"):
        hit = detect_trigger(message)
        assert hit.negated is True, message
        assert hit.matched is False, message


def test_emotion_triggers():
    hit = detect_trigger("来个表情包")
    assert hit.kind == "emotion" and hit.matched
    explicit = detect_trigger("画一个困了的表情包")
    assert explicit.emotion_id == "sleepy"


def test_plain_message_has_no_trigger():
    assert detect_trigger("今天下班有点晚") .kind == "none"
    assert detect_trigger("") .kind == "none"


# --- 降级路径 -----------------------------------------------------------


def test_render_degrades_when_imagegen_missing(tmp_path):
    db = make_db(tmp_path)
    try:
        library = EmotionLibrary(db, imagegen=_ImageGenAdapter(None))
        library.imagegen.export = {"module": None}
        result = __import__("asyncio").run(library.render(trigger="画个你自己"))
        assert result.ok is False
        assert result.degraded is True
        assert result.path is None
        assert "imagegen_missing" in result.error
        assert result.action == "self_portrait"
        payload = result.to_dict()
        assert payload["emotion"]["id"]
        assert payload["text"].endswith("（这次没画成）")
        # 失败也必须留痕，不能静默
        kinds = [item["kind"] for item in db.list_events(10)]
        assert "emotion.render" in kinds
    finally:
        db.close()


def test_render_degrades_when_backend_paused(tmp_path):
    db = make_db(tmp_path)
    try:
        gen = FakeGenerator()
        library = EmotionLibrary(db, imagegen=adapter_with(gen))
        library.imagegen.paused_reason = lambda: "连续 OOM"
        result = __import__("asyncio").run(library.render(emotion_id="joy_high"))
        assert result.ok is False and result.degraded is True
        assert "queue_paused" in result.error
        assert gen.calls == []  # 熔断期间不白跑生图
    finally:
        db.close()


def test_render_degrades_on_budget_exceeded(tmp_path):
    db = make_db(tmp_path)
    try:
        gen = FakeGenerator(raises=BudgetExceeded("今日预算已用完"))
        library = EmotionLibrary(db, imagegen=adapter_with(gen, BudgetExceeded=BudgetExceeded))
        result = __import__("asyncio").run(library.render(emotion_id="calm_neutral"))
        assert result.ok is False and result.degraded is True
        assert "budget_exceeded" in result.error
    finally:
        db.close()


def test_render_degrades_on_arbitrary_backend_error(tmp_path):
    db = make_db(tmp_path)
    try:
        gen = FakeGenerator(raises=RuntimeError("连接 ComfyUI 被拒"))
        library = EmotionLibrary(db, imagegen=adapter_with(gen))
        result = __import__("asyncio").run(library.render(emotion_id="calm_neutral"))
        assert result.ok is False and result.degraded is True
        assert "RuntimeError" in result.error
    finally:
        db.close()


def test_render_degrades_when_result_missing_path(tmp_path):
    db = make_db(tmp_path)
    try:
        class BadGen:
            async def generate(self, **kwargs):
                return FakeResult(None)

        library = EmotionLibrary(db, imagegen=adapter_with(BadGen()))
        result = __import__("asyncio").run(library.render(emotion_id="calm_neutral"))
        assert result.ok is False and result.degraded is True
        assert result.path is None
    finally:
        db.close()


# --- 正常路径 + artwork 关联 -------------------------------------------


def test_render_success_returns_path_and_artifact(tmp_path):
    db = make_db(tmp_path)
    try:
        gen = FakeGenerator(result_path="data/artwork/portrait_0007.png", artwork_id="art-7")
        library = EmotionLibrary(db, imagegen=adapter_with(gen))
        result = __import__("asyncio").run(library.render(trigger="画个你自己", reason="主人让我画自己"))
        assert result.ok is True
        assert result.path == "data/artwork/portrait_0007.png"
        assert result.artwork_id == "art-7"
        assert result.action == "self_portrait"
        assert gen.calls and gen.calls[0]["reason"] == "主人让我画自己"
        assert gen.calls[0]["prompt"] == result.prompt
        # imagegen 已经落库时，emotions 不应再重复插一行
        assert recent_artworks(db) == []
    finally:
        db.close()


def test_render_falls_back_to_local_artwork_insert(tmp_path):
    db = make_db(tmp_path)
    try:
        gen = FakeGenerator(result_path="data/artwork/emo_0001.png", artwork_id=None)
        library = EmotionLibrary(db, imagegen=adapter_with(gen))
        result = __import__("asyncio").run(library.render(emotion_id="joy_high"))
        assert result.ok is True and result.artwork_id
        rows = recent_artworks(db, limit=5)
        assert len(rows) == 1
        assert rows[0]["id"] == result.artwork_id
        assert rows[0]["path"] == "data/artwork/emo_0001.png"
        assert rows[0]["mood"] == result.mood
        assert rows[0]["reason"] == result.reason
    finally:
        db.close()


def test_save_artwork_matches_schema_columns(tmp_path):
    db = make_db(tmp_path)
    try:
        row = save_artwork(db, "a/b.png", "p", "r", "平静")
        assert row is not None and row["id"]
        stored = recent_artworks(db)
        assert stored[0]["prompt"] == "p" and stored[0]["created_at"]
    finally:
        db.close()


def test_render_uses_live_state(tmp_path):
    db = make_db(tmp_path)
    try:
        db.set_state({"mood": "疲惫", "energy": 4})
        gen = FakeGenerator()
        library = EmotionLibrary(db, imagegen=adapter_with(gen))
        result = __import__("asyncio").run(library.render())
        assert result.emotion.id == "sleepy"
        assert result.mood == "疲惫" and result.energy == 4
    finally:
        db.close()


def test_render_rejects_unknown_emotion_id(tmp_path):
    db = make_db(tmp_path)
    try:
        library = EmotionLibrary(db, imagegen=_ImageGenAdapter(None))
        try:
            __import__("asyncio").run(library.render(emotion_id="不存在"))
            raise AssertionError("未知表情应抛 KeyError")
        except KeyError:
            pass
    finally:
        db.close()


def test_sync_generate_is_supported(tmp_path):
    db = make_db(tmp_path)
    try:
        class SyncGen:
            def generate(self, *, prompt, reason, mood):
                return {"path": "data/artwork/sync.png", "artwork_id": "art-sync"}

        library = EmotionLibrary(db, imagegen=adapter_with(SyncGen()))
        result = __import__("asyncio").run(library.render(emotion_id="hug"))
        assert result.ok is True and result.artwork_id == "art-sync"
    finally:
        db.close()


# --- kind：主动发 vs 回复附带 -------------------------------------------

def test_render_kind_defaults_to_reply(tmp_path):
    db = make_db(tmp_path)
    try:
        gen = FakeGenerator()
        library = EmotionLibrary(db, imagegen=adapter_with(gen))
        result = __import__("asyncio").run(library.render(emotion_id="joy_high"))
        assert result.kind == "reply"
        assert result.to_dict()["kind"] == "reply"
        assert "回复附带" in result.reason
    finally:
        db.close()


def test_render_kind_proactive_reaches_event(tmp_path):
    db = make_db(tmp_path)
    try:
        library = EmotionLibrary(db, imagegen=_ImageGenAdapter(None))
        library.imagegen.export = {"module": None}
        result = __import__("asyncio").run(library.render(kind="proactive"))
        assert result.kind == "proactive"
        assert result.degraded is True  # 降级也要保留 kind
        event = db.list_events(1)[0]
        assert event["kind"] == "emotion.render"
        assert event["payload"]["kind"] == "proactive"
        assert "主动发" in result.reason
    finally:
        db.close()


def test_render_unknown_kind_falls_back_to_reply(tmp_path):
    db = make_db(tmp_path)
    try:
        library = EmotionLibrary(db, imagegen=_ImageGenAdapter(None))
        library.imagegen.export = {"module": None}
        result = __import__("asyncio").run(library.render(kind="whatever"))
        assert result.kind == "reply"
    finally:
        db.close()


def test_render_endpoint_kind_validation(tmp_path):
    db, client = make_client(tmp_path)
    try:
        ok = client.post(
            "/v1/emotions/render",
            headers=headers(),
            json={"emotion": "calm_neutral", "kind": "proactive"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["kind"] == "proactive"
        bad = client.post(
            "/v1/emotions/render",
            headers=headers(),
            json={"emotion": "calm_neutral", "kind": "随便"},
        )
        assert bad.status_code == 422
        listing = client.get("/v1/emotions", headers=headers()).json()
        assert listing["kinds"] == ["proactive", "reply"]
    finally:
        client.close()
        db.close()


def test_negation_suffix():
    hit = detect_trigger("画个你自己就算了")
    assert hit.negated is True and hit.matched is False
    assert detect_trigger("来个表情包吧").matched is True


# --- 路由契约（Lead 接线用） -------------------------------------------


def make_client(tmp_path, generator=None):
    db = make_db(tmp_path)
    app = FastAPI()
    app.include_router(build_emotions_router(settings(tmp_path), db, generator))
    return db, TestClient(app)


def test_list_endpoint_requires_auth_and_returns_catalog(tmp_path):
    db, client = make_client(tmp_path)
    try:
        assert client.get("/v1/emotions").status_code == 401
        response = client.get("/v1/emotions", headers=headers())
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) == len(EMOTIONS)
        assert set(body["items"][0]) >= {"id", "name", "emoji", "moods", "energy_range"}
        assert body["recommended"]["emotion"]["id"] in EMOTION_INDEX
        assert body["recommended"]["imagegen"]["available"] is True  # 本机自动装配 Mock 服务
        assert body["triggers"]["self_portrait"]
    finally:
        client.close()
        db.close()


def test_list_endpoint_query_overrides(tmp_path):
    db, client = make_client(tmp_path)
    try:
        response = client.get("/v1/emotions", headers=headers(), params={"mood": "开心", "energy": 99})
        assert response.status_code == 200
        assert response.json()["recommended"]["emotion"]["id"] == "joy_high"
    finally:
        client.close()
        db.close()


def test_render_endpoint_draws_with_auto_mock_service(tmp_path):
    """本机无 GPU/ComfyUI 时路由自己装配 MockBackend，应该真的画出一张可读的 PNG。"""
    db, client = make_client(tmp_path)
    try:
        response = client.post(
            "/v1/emotions/render", headers=headers(), json={"trigger": "画个你自己"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["action"] == "self_portrait"
        assert body["emotion"]["self_portrait"] is True
        assert body["backend"] == "mock"
        if body["degraded"] is False:
            assert body["path"] and body["artwork_id"]
            stored = tmp_path / "artwork" / body["path"]
            assert stored.is_file(), body
            assert stored.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
            # artwork 关联：库里那一行就是这次画的
            rows = recent_artworks(db, limit=5)
            assert rows[0]["id"] == body["artwork_id"]
            assert rows[0]["path"] == body["path"]
        else:
            # 生图链路不可用（如缺 Pillow）时必须优雅降级而不是 500
            assert body["path"] is None
            assert body["error"]
        assert body["kind"] == "reply"
        assert body["text"]
    finally:
        client.close()
        db.close()


class BrokenService:
    """模拟 imagegen 服务不可用：generate 直接抛错。"""

    backend_kind = "broken"

    class queue:
        paused = False
        pause_reason = None

    async def generate(self, prompt, **kwargs):
        raise RuntimeError("ComfyUI 不可达")


def test_render_endpoint_degrades_to_200_when_service_fails(tmp_path):
    db = make_db(tmp_path)
    app = FastAPI()
    app.include_router(
        build_emotions_router(settings(tmp_path), db, service=BrokenService())
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/emotions/render", headers=headers(), json={"trigger": "来个表情包"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["degraded"] is True
        assert body["path"] is None
        assert "backend_unavailable" in body["error"]
        assert body["backend"] == "broken"
    db.close()


def test_render_endpoint_success(tmp_path):
    db, client = make_client(tmp_path, FakeGenerator(result_path="data/artwork/x.png"))
    try:
        response = client.post(
            "/v1/emotions/render",
            headers=headers(),
            json={"emotion": "joy_high", "reason": "收到好消息"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["emotion"]["id"] == "joy_high"
        assert body["path"] == "data/artwork/x.png"
        assert body["reason"] == "收到好消息"
    finally:
        client.close()
        db.close()


def test_render_endpoint_validation(tmp_path):
    db, client = make_client(tmp_path)
    try:
        assert client.post("/v1/emotions/render", headers=headers(), json={}).status_code == 422
        assert client.post(
            "/v1/emotions/render", headers=headers(), json={"emotion": "不存在"}
        ).status_code == 422
        assert client.post(
            "/v1/emotions/render", headers=headers(), json={"emotion": "joy_high", "energy": 999}
        ).status_code == 422
        assert client.post(
            "/v1/emotions/render", headers=headers(), json={"trigger": "来个表情包"}
        ).status_code == 200
    finally:
        client.close()
        db.close()


# --- 与 task-5 真实 imagegen 的集成（MockBackend，无 GPU）-------------------


def _real_service(tmp_path, db, **kwargs):
    """用 imagegen.build_service 组装真实服务；模块不在就 skip，而不是失败。"""
    imagegen = pytest.importorskip("lingbrain.imagegen")
    from lingbrain.gpu_queue import GpuQueue

    kwargs.setdefault("backend_kind", "mock")
    kwargs.setdefault("artwork_root", tmp_path / "artwork")
    kwargs.setdefault("queue", GpuQueue())
    return imagegen.ImageGenerationService(db, **kwargs)


def test_integration_real_imagegen_draws_and_links_artwork(tmp_path):
    db = make_db(tmp_path)
    try:
        service = _real_service(tmp_path, db, daily_budget=10)
        library = EmotionLibrary(db, service=service)
        result = __import__("asyncio").run(
            library.render(trigger="画个你自己", kind="proactive")
        )
        assert result.ok is True, result.error
        assert result.degraded is False
        assert result.backend == "mock"
        assert result.artwork_id and result.path
        assert (tmp_path / "artwork" / result.path).is_file()
        # 1) artwork 表里正好一条，且就是这一张
        rows = recent_artworks(db, limit=5)
        assert len(rows) == 1
        assert rows[0]["id"] == result.artwork_id
        assert rows[0]["mood"] == result.mood
        # 2) imagegen 的 artwork.created 与 emotions 的 emotion.render 都能对上同一张
        events = db.list_events(20)
        created = [e for e in events if e["kind"] == "artwork.created"]
        rendered = [e for e in events if e["kind"] == "emotion.render"]
        assert len(created) == 1 and len(rendered) == 1
        assert rendered[0]["payload"]["artwork_id"] == result.artwork_id
        assert rendered[0]["payload"]["kind"] == "proactive"
        assert created[0]["payload"]["artwork_id"] == result.artwork_id
        # 3) 固定画风前缀由 imagegen 加上，回写回来的 prompt 里要能看见
        assert "灵鲸鲸固定画风" in rows[0]["prompt"]
    finally:
        db.close()


def test_integration_budget_exhausted_degrades(tmp_path):
    db = make_db(tmp_path)
    try:
        service = _real_service(tmp_path, db, daily_budget=0)
        library = EmotionLibrary(db, service=service)
        result = __import__("asyncio").run(library.render(emotion_id="joy_high"))
        assert result.ok is False
        assert result.degraded is True
        assert "budget_exceeded" in result.error
        assert recent_artworks(db) == []
    finally:
        db.close()


def test_integration_paused_queue_degrades_without_calling_backend(tmp_path):
    db = make_db(tmp_path)
    try:
        service = _real_service(tmp_path, db)
        service.queue.pause("显存连续不足，等待主人复核")
        library = EmotionLibrary(db, service=service)
        result = __import__("asyncio").run(library.render(emotion_id="calm_neutral"))
        assert result.ok is False and result.degraded is True
        assert "queue_paused" in result.error
        assert getattr(service.backend, "generate_calls", 0) == 0
        recommendation = library.recommend()
        # 队列已暂停时 recommend 必须把暂停原因透出去，且与队列自身状态一致
        assert recommendation["imagegen"]["paused"]
        assert recommendation["imagegen"]["paused"] == service.queue.status()["pause_reason"] or (
            service.queue.status()["pause_reason"] is not None
            and "GPU 队列" in recommendation["imagegen"]["paused"]
        )
    finally:
        db.close()


def test_module_imports_when_imagegen_absent(tmp_path, monkeypatch):
    """imagegen 缺失时要降级而不是 ImportError 崩掉（模拟 task-5 尚未交付）。"""

    def _load_unavailable(cls, generator=None):
        # 模拟 `from . import imagegen` 抛 ImportError 后适配器的落地状态
        adapter = cls(generator)
        adapter.export = {"module": None}
        return adapter

    monkeypatch.setattr(
        "lingbrain.emotions._ImageGenAdapter.load", classmethod(_load_unavailable)
    )
    db = make_db(tmp_path)
    try:
        library = EmotionLibrary(db)
        assert library.imagegen.available is False
        assert library.imagegen.backend_name == "unavailable"
        result = __import__("asyncio").run(library.render(trigger="画个你自己"))
        assert result.ok is False and result.degraded is True
        assert "imagegen_missing" in result.error
    finally:
        db.close()

