"""V4 聊天历史测试（task-19，契约第 1 节）。

GET /v1/chat/history：从 event 表读 user.message / assistant.message，
**按时间正序**返回，role 由 actor 映射（主人→user，灵鲸鲸→assistant），
支持 limit(1-500, 默认200) 与 before 向历史翻页。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from lingbrain.app import create_app
from lingbrain.config import Settings
from lingbrain.db import Database
from lingbrain.service import list_chat_history

TOKEN = "test-token"


def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path, auth_token=TOKEN, model_provider="echo", heartbeat_seconds=3600
    )


def headers() -> dict[str, str]:
    return {"X-Ling-Token": TOKEN}


def seed(db: Database, pairs: list[tuple[str, str]]) -> None:
    """直接写事件，避免依赖聊天链路。"""
    for user_text, reply in pairs:
        db.append_event("主人", "user.message", {"content": user_text, "client_id": "desktop"})
        db.append_event("灵鲸鲸", "assistant.message",
                        {"content": reply, "client_id": "desktop", "model": "echo"})


# ---------------------------------------------------------------------------
# 1. 基础形状与顺序
# ---------------------------------------------------------------------------


def test_history_is_chronological(tmp_path):
    db = Database(tmp_path / "h.db")
    seed(db, [("第一句", "答一"), ("第二句", "答二")])
    result = list_chat_history(db)
    contents = [item["content"] for item in result["items"]]
    # 正序：先 user 后 assistant，再下一轮
    assert contents == ["第一句", "答一", "第二句", "答二"]


def test_role_mapping_from_actor(tmp_path):
    db = Database(tmp_path / "h.db")
    seed(db, [("你好", "我在。")])
    items = list_chat_history(db)["items"]
    assert items[0]["role"] == "user"
    assert items[1]["role"] == "assistant"


def test_item_shape_matches_contract(tmp_path):
    db = Database(tmp_path / "h.db")
    seed(db, [("你好", "我在。")])
    items = list_chat_history(db)["items"]
    user, assistant = items
    for item in items:
        assert set(item) >= {"id", "ts", "role", "content", "client_id"}
        assert item["role"] in {"user", "assistant"}
    # 契约示例里 assistant 带 model
    assert assistant["model"] == "echo"
    # 用户消息没有 model 就不该冒出空字段
    assert "model" not in user


def test_system_events_are_excluded(tmp_path):
    db = Database(tmp_path / "h.db")
    seed(db, [("你好", "我在。")])
    db.append_event("system", "model.error", {"message": "炸了"})
    db.append_event("system", "state.changed", {"changed": {}})
    db.append_event("灵鲸鲸", "proactive.sent", {"text": "主动说一句"})
    items = list_chat_history(db)["items"]
    assert len(items) == 2
    assert {item["role"] for item in items} == {"user", "assistant"}


def test_has_more_false_when_everything_fits(tmp_path):
    db = Database(tmp_path / "h.db")
    seed(db, [("你好", "我在。")])
    assert list_chat_history(db)["has_more"] is False


# ---------------------------------------------------------------------------
# 2. limit 与 has_more
# ---------------------------------------------------------------------------


def test_limit_returns_most_recent_but_in_forward_order(tmp_path):
    db = Database(tmp_path / "h.db")
    seed(db, [("一", "答一"), ("二", "答二"), ("三", "答三")])
    result = list_chat_history(db, limit=2)
    # 取最近两条，但仍按正序（不是倒序）
    assert [i["content"] for i in result["items"]] == ["三", "答三"]
    assert result["has_more"] is True


def test_limit_is_clamped(tmp_path):
    db = Database(tmp_path / "h.db")
    seed(db, [("你好", "我在。")])
    # 超过上限按 500 处理，不报错
    assert len(list_chat_history(db, limit=10_000)["items"]) == 2


# ---------------------------------------------------------------------------
# 3. before 翻页
# ---------------------------------------------------------------------------


def test_before_pages_backwards(tmp_path):
    db = Database(tmp_path / "h.db")
    seed(db, [("一", "答一"), ("二", "答二"), ("三", "答三")])
    everything = list_chat_history(db)["items"]
    # 正序下标：0=一 1=答一 2=二 3=答二 4=三 5=答三
    # 用"三"（下标 4）的时间戳向前翻，应拿到它之前的 4 条
    pivot = everything[4]["ts"]
    page = list_chat_history(db, before=pivot)
    contents = [item["content"] for item in page["items"]]
    assert contents == ["一", "答一", "二", "答二"]
    # 翻出来的都必须严格早于 before
    assert all(item["ts"] < pivot for item in page["items"])


def test_before_with_oldest_ts_returns_empty(tmp_path):
    db = Database(tmp_path / "h.db")
    seed(db, [("你好", "我在。")])
    oldest = list_chat_history(db)["items"][0]["ts"]
    result = list_chat_history(db, before=oldest)
    assert result["items"] == []
    assert result["has_more"] is False


# ---------------------------------------------------------------------------
# 4. HTTP 路由
# ---------------------------------------------------------------------------


def test_route_requires_auth(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.get("/v1/chat/history").status_code == 401


def test_route_returns_history_after_chat(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        chat = client.post(
            "/v1/chat", headers=headers(),
            json={"message": "我叫阿星", "client_id": "shell"},
        )
        assert chat.status_code == 200, chat.text

        response = client.get("/v1/chat/history", headers=headers())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["has_more"] is False
        roles = [item["role"] for item in body["items"]]
        assert roles == ["user", "assistant"]
        assert body["items"][0]["content"] == "我叫阿星"
        assert body["items"][0]["client_id"] == "shell"
        assert body["items"][1]["model"]


def test_route_limit_and_before_params(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        for text in ("一", "二", "三"):
            client.post("/v1/chat", headers=headers(),
                        json={"message": text, "client_id": "c"})
        first_page = client.get("/v1/chat/history", headers=headers(),
                                params={"limit": 2}).json()
        assert len(first_page["items"]) == 2
        assert first_page["has_more"] is True

        pivot = first_page["items"][0]["ts"]
        earlier = client.get("/v1/chat/history", headers=headers(),
                             params={"before": pivot}).json()
        assert all(item["ts"] < pivot for item in earlier["items"])
        # 两页拼起来能覆盖全部 6 条
        assert len(earlier["items"]) + len(first_page["items"]) == 6


def test_route_rejects_out_of_range_limit(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.get("/v1/chat/history", headers=headers(),
                          params={"limit": 0}).status_code == 422
        assert client.get("/v1/chat/history", headers=headers(),
                          params={"limit": 501}).status_code == 422


def test_restart_keeps_history(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        client.post("/v1/chat", headers=headers(),
                    json={"message": "记住这句", "client_id": "c"})
    with TestClient(create_app(settings(tmp_path))) as restarted:
        items = restarted.get("/v1/chat/history", headers=headers()).json()["items"]
        assert items[0]["content"] == "记住这句"
