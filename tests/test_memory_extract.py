"""task-15 记忆理解升级：模型结构化抽取、规则兜底、可追溯、不写疑问句/引用。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lingbrain.config import Settings
from lingbrain.db import Database
from lingbrain.memory import (
    MEMORY_KINDS,
    SOURCE_MODEL,
    SOURCE_RULE_FALLBACK,
    extract_memories,
    extract_memories_async,
    is_checkable_fact,
    normalize_candidates,
    parse_model_output,
    save_candidates,
)
from lingbrain.models import EchoModel, ModelUnavailable
from lingbrain.service import BrainService, build_memory_extract_router

TOKEN = "test-token"


def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, auth_token=TOKEN, heartbeat_seconds=3600)


def headers() -> dict[str, str]:
    return {"X-Ling-Token": TOKEN}


def make_db(tmp_path) -> Database:
    return Database(tmp_path / "mem.db")


class ScriptedModel:
    """返回固定文本的模型替身，用来模拟模型输出的各种形状。"""

    name = "scripted"

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def chat(self, messages, system):
        self.calls.append((messages, system))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def run(coro):
    return asyncio.run(coro)


# --- 规则快速路径保持可用 -------------------------------------------------


def test_rule_extraction_still_works():
    items = extract_memories("我叫阿星，我喜欢蓝鲸，我讨厌熬夜")
    kinds = {item.kind for item in items}
    assert "profile" in kinds and "preference" in kinds
    assert any("阿星" in item.content for item in items)


def test_rule_extraction_rejects_questions():
    assert extract_memories("你还记得我喜欢什么吗？") == []
    assert extract_memories("我喜欢什么？") == []


# --- 可核性：疑问/引用/假设 ----------------------------------------------


def test_question_is_not_checkable():
    assert is_checkable_fact("主人喜欢什么") is False
    assert is_checkable_fact("明天是什么天气") is False


def test_quote_is_not_checkable():
    assert is_checkable_fact("\u201c我喜欢猫\u201d") is False
    assert is_checkable_fact("\u300c他说的\u300d") is False


def test_hypothetical_is_not_checkable():
    assert is_checkable_fact("如果我明天不加班") is False
    assert is_checkable_fact("据说主人喜欢猫") is False
    assert is_checkable_fact("也许主人喜欢猫") is False


def test_plain_fact_is_checkable():
    assert is_checkable_fact("主人喜欢蓝鲸") is True
    assert is_checkable_fact("主人养了一只叫豆豆的猫") is True


# --- 模型输出解析 -------------------------------------------------------


def test_parse_plain_json_array():
    items = parse_model_output(
        '[{"kind":"preference","who":"主人","content":"主人喜欢蓝鲸","weight":0.8}]'
    )
    assert items[0]["kind"] == "preference"


def test_parse_tolerates_code_fence():
    raw = "```json\n[{\"kind\":\"profile\",\"content\":\"主人希望被称呼为阿星\"}]\n```"
    assert parse_model_output(raw)[0]["kind"] == "profile"


def test_parse_tolerates_surrounding_chat():
    raw = "好的，结果如下：\n[{\"kind\":\"event\",\"content\":\"主人今天加班\"}]\n希望有帮助"
    assert parse_model_output(raw)[0]["kind"] == "event"


def test_parse_tolerates_wrapper_object():
    raw = '{"memories": [{"kind": "event", "content": "主人今天加班"}]}'
    assert parse_model_output(raw)[0]["kind"] == "event"


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_model_output("我无法回答这个问题")
    with pytest.raises(ValueError):
        parse_model_output("")


# --- 模型抽取：四类 kind 与过滤 ------------------------------------------


def test_model_extraction_supports_four_kinds():
    raw = (
        '['
        '{"kind":"preference","who":"主人","content":"主人喜欢蓝鲸","weight":0.9},'
        '{"kind":"event","who":"主人","content":"主人今天加班到十点","weight":0.4},'
        '{"kind":"profile","who":"主人","content":"主人住在杭州","weight":0.9},'
        '{"kind":"relation","who":"主人","content":"主人养了一只叫豆豆的猫","weight":0.7}'
        ']'
    )
    message = "我喜欢蓝鲸，今天加班到十点，我住在杭州，我养了一只叫豆豆的猫"
    result = run(extract_memories_async(ScriptedModel(raw), message))
    assert result.degraded is False
    assert result.source == SOURCE_MODEL
    assert {c.kind for c in result.candidates} == set(MEMORY_KINDS)


def test_model_candidate_with_unknown_kind_is_rejected():
    raw = '[{"kind":"gossip","content":"主人喜欢蓝鲸"}]'
    rejected = []
    result = run(
        extract_memories_async(ScriptedModel(raw), "我喜欢蓝鲸", rejected=rejected)
    )
    assert result.candidates == []
    assert rejected and rejected[0]["code"] == "bad_kind"


def test_model_cannot_write_question_as_fact():
    raw = '[{"kind":"preference","content":"主人喜欢什么"}]'
    result = run(extract_memories_async(ScriptedModel(raw), "我喜欢什么？"))
    assert result.candidates == []


def test_model_cannot_invent_ungrounded_content():
    raw = '[{"kind":"profile","content":"主人住在巴黎开跑车"}]'
    result = run(extract_memories_async(ScriptedModel(raw), "我喜欢蓝鲸"))
    assert result.candidates == []


def test_normalize_weight_out_of_range_falls_back():
    items = [{"kind": "preference", "content": "主人喜欢蓝鲸", "weight": 99}]
    out = normalize_candidates(items, "我喜欢蓝鲸")
    assert 0 <= out[0].weight <= 1


def test_empty_array_is_trusted_not_degraded():
    result = run(extract_memories_async(ScriptedModel("[]"), "今天天气不错"))
    assert result.candidates == []
    assert result.degraded is False
    assert result.source == SOURCE_MODEL


# --- 降级：模型不可用 -> rule-fallback -----------------------------------


def test_model_exception_degrades_to_rule_fallback():
    model = ScriptedModel(ModelUnavailable("模型挂了"))
    result = run(extract_memories_async(model, "我喜欢蓝鲸"))
    assert result.degraded is True
    assert result.source == SOURCE_RULE_FALLBACK
    assert "模型调用失败" in result.reason
    assert any(c.content == "主人喜欢蓝鲸" for c in result.candidates)


def test_model_garbage_degrades_to_rule_fallback():
    result = run(extract_memories_async(ScriptedModel("我觉得你说得对"), "我喜欢蓝鲸"))
    assert result.degraded is True
    assert result.source == SOURCE_RULE_FALLBACK
    assert "不可解析" in result.reason


def test_model_without_chat_degrades():
    class Dumb:
        name = "dumb"

    result = run(extract_memories_async(Dumb(), "我喜欢蓝鲸"))
    assert result.degraded is True
    assert result.source == SOURCE_RULE_FALLBACK


def test_echo_model_degrades_because_output_is_not_json():
    """echo 模型返回对话文本而不是 JSON，必须老老实实降级。"""
    result = run(extract_memories_async(EchoModel(), "我喜欢蓝鲸"))
    assert result.degraded is True
    assert result.source == SOURCE_RULE_FALLBACK


# --- 可追溯：source_event_id --------------------------------------------


def test_candidates_carry_source_event_id():
    model = ScriptedModel('[{"kind":"preference","content":"主人喜欢蓝鲸"}]')
    result = run(extract_memories_async(model, "我喜欢蓝鲸", source_event_id="evt-42"))
    assert result.candidates[0].source_event_id == "evt-42"


def test_fallback_candidates_carry_source_event_id():
    model = ScriptedModel(ModelUnavailable("挂了"))
    result = run(extract_memories_async(model, "我喜欢蓝鲸", source_event_id="evt-7"))
    assert all(c.source_event_id == "evt-7" for c in result.candidates)


def test_saved_memory_traces_back_to_event(tmp_path):
    db = make_db(tmp_path)
    try:
        event = db.append_event("主人", "user.message", {"content": "我喜欢蓝鲸"})
        model = ScriptedModel('[{"kind":"preference","content":"主人喜欢蓝鲸"}]')
        result = run(extract_memories_async(model, "我喜欢蓝鲸", source_event_id=event["id"]))
        saved = save_candidates(db, result.candidates, source=result.source, source_event_id=event["id"])
        assert len(saved) == 1
        assert saved[0]["source_event_id"] == event["id"]
        assert saved[0]["source"] == SOURCE_MODEL
        events = db.list_events(5)
        assert any(e["id"] == event["id"] for e in events)
    finally:
        db.close()


# --- service.chat 集成 ---------------------------------------------------


def test_chat_records_model_source(tmp_path):
    db = make_db(tmp_path)
    try:
        model = ScriptedModel('[{"kind":"preference","content":"主人喜欢蓝鲸"}]')
        service = BrainService(db, model)
        response = run(service.chat("我喜欢蓝鲸", "test"))
        assert len(response.memories) == 1
        assert response.memories[0].source == SOURCE_MODEL
        assert response.memories[0].source_event_id == response.user_event_id
    finally:
        db.close()


class ExtractFailsChatWorks:
    """抽取时抛错、闲聊时正常：验证降级只影响记忆，不影响回话。"""

    name = "flaky"

    def __init__(self):
        self.extract_calls = 0

    async def chat(self, messages, system):
        # 抽取调用带的是抽取系统提示；闲聊调用带的是人设提示。
        if "记忆整理员" in (system or ""):
            self.extract_calls += 1
            raise ModelUnavailable("抽取模型不可用")
        return "我在。"


def test_chat_degrades_and_marks_rule_fallback(tmp_path):
    """抽取失败时：记忆照写但标 rule-fallback，且详情落事件；对话不受影响。"""
    db = make_db(tmp_path)
    try:
        model = ExtractFailsChatWorks()
        service = BrainService(db, model)
        response = run(service.chat("我喜欢蓝鲸", "test"))
        assert model.extract_calls == 1
        assert response.reply == "我在。"
        assert len(response.memories) == 1
        assert response.memories[0].source == SOURCE_RULE_FALLBACK
        degraded = [e for e in db.list_events(20) if e["kind"] == "memory.extract.degraded"]
        assert degraded
        assert degraded[0]["payload"]["source"] == SOURCE_RULE_FALLBACK
        assert degraded[0]["payload"]["reason"]
        assert degraded[0]["payload"]["source_event_id"] == response.user_event_id
    finally:
        db.close()


def test_chat_propagates_model_unavailable_for_reply(tmp_path):
    """模型整体不可用时，chat 仍要报错给上层（既有的 503 行为不能吞掉）。"""
    db = make_db(tmp_path)
    try:
        service = BrainService(db, ScriptedModel(ModelUnavailable("模型不可用")))
        with pytest.raises(ModelUnavailable):
            run(service.chat("我喜欢蓝鲸", "test"))
        # 即使对话失败，降级事实也已留痕
        degraded = [e for e in db.list_events(20) if e["kind"] == "memory.extract.degraded"]
        assert degraded
    finally:
        db.close()


def test_chat_with_echo_model_falls_back(tmp_path):
    db = make_db(tmp_path)
    try:
        service = BrainService(db, EchoModel())
        response = run(service.chat("我叫阿星，我喜欢蓝鲸", "test"))
        sources = {m.source for m in response.memories}
        assert sources == {SOURCE_RULE_FALLBACK}
    finally:
        db.close()


def test_chat_question_writes_no_memory(tmp_path):
    db = make_db(tmp_path)
    try:
        model = ScriptedModel('[{"kind":"preference","content":"主人喜欢什么"}]')
        service = BrainService(db, model)
        response = run(service.chat("你还记得我喜欢什么吗？", "test"))
        assert response.memories == []
    finally:
        db.close()


# --- POST /v1/memory/extract 调试接口 -----------------------------------


def make_client(tmp_path, model):
    db = make_db(tmp_path)
    service = BrainService(db, model)
    app = FastAPI()
    app.include_router(build_memory_extract_router(settings(tmp_path), service))
    return db, TestClient(app)


def test_extract_endpoint_requires_auth(tmp_path):
    db, client = make_client(tmp_path, ScriptedModel("[]"))
    try:
        assert client.post("/v1/memory/extract", json={"message": "你好"}).status_code == 401
    finally:
        client.close()
        db.close()


def test_extract_endpoint_dry_run_writes_nothing(tmp_path):
    model = ScriptedModel('[{"kind":"preference","content":"主人喜欢蓝鲸"}]')
    db, client = make_client(tmp_path, model)
    try:
        before_events = len(db.list_events(50))
        response = client.post(
            "/v1/memory/extract", headers=headers(), json={"message": "我喜欢蓝鲸"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["dry_run"] is True
        assert body["source"] == SOURCE_MODEL
        assert body["degraded"] is False
        assert body["count"] == 1
        assert body["candidates"][0]["content"] == "主人喜欢蓝鲸"
        assert body["saved"] == []
        assert len(db.list_events(50)) == before_events
        assert db.important_memories(5) == []
    finally:
        client.close()
        db.close()


def test_extract_endpoint_dry_run_false_persists_with_trace(tmp_path):
    model = ScriptedModel('[{"kind":"preference","content":"主人喜欢蓝鲸"}]')
    db, client = make_client(tmp_path, model)
    try:
        response = client.post(
            "/v1/memory/extract",
            headers=headers(),
            json={"message": "我喜欢蓝鲸", "dry_run": False},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["dry_run"] is False
        assert len(body["saved"]) == 1
        event_id = body["source_event_id"]
        assert event_id
        assert body["saved"][0]["source_event_id"] == event_id
        assert any(e["id"] == event_id for e in db.list_events(20))
    finally:
        client.close()
        db.close()


def test_extract_endpoint_shows_degraded_reason(tmp_path):
    db, client = make_client(tmp_path, ScriptedModel(ModelUnavailable("挂了")))
    try:
        response = client.post(
            "/v1/memory/extract", headers=headers(), json={"message": "我喜欢蓝鲸"}
        )
        body = response.json()
        assert body["degraded"] is True
        assert body["source"] == SOURCE_RULE_FALLBACK
        assert "模型调用失败" in body["reason"]
        assert body["count"] == 1
    finally:
        client.close()
        db.close()


def test_extract_endpoint_reports_rejected_items(tmp_path):
    raw = '[{"kind":"gossip","content":"主人喜欢蓝鲸"},{"kind":"preference","content":"主人喜欢什么"}]'
    db, client = make_client(tmp_path, ScriptedModel(raw))
    try:
        response = client.post(
            "/v1/memory/extract", headers=headers(), json={"message": "我喜欢蓝鲸"}
        )
        body = response.json()
        assert body["count"] == 0
        codes = {item["code"] for item in body["rejected"]}
        assert "bad_kind" in codes
    finally:
        client.close()
        db.close()


def test_extract_endpoint_rejects_blank_message(tmp_path):
    db, client = make_client(tmp_path, ScriptedModel("[]"))
    try:
        response = client.post("/v1/memory/extract", headers=headers(), json={"message": "   "})
        assert response.status_code == 422
    finally:
        client.close()
        db.close()
