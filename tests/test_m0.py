from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from lingbrain.app import create_app
from lingbrain.config import Settings
from lingbrain.db import Database

TOKEN = "test-token"


def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        auth_token=TOKEN,
        model_provider="echo",
        heartbeat_seconds=3600,
    )


def headers() -> dict[str, str]:
    return {"X-Ling-Token": TOKEN}


def test_health_requires_auth(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.get("/v1/health").status_code == 401
        response = client.get("/v1/health", headers=headers())
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["model_provider"] == "echo"


def test_chat_memory_search_and_restart(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            headers=headers(),
            json={"message": "我叫阿星，我喜欢蓝鲸", "client_id": "test"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["memories"]) == 2
        # 能量由状态机按真实流逝时间衰减，聊天本身不再写 energy。
        # 这里只断言状态可用，不断言具体数值（否则两套逻辑又耦合回去）。
        assert isinstance(body["state"]["energy"], int)
        assert 0 <= body["state"]["energy"] <= 100

        short_query = client.get(
            "/v1/memory/search", headers=headers(), params={"q": "蓝鲸"}
        )
        assert short_query.status_code == 200
        assert short_query.json()["items"][0]["content"] == "主人喜欢蓝鲸"

        fts_query = client.get(
            "/v1/memory/search", headers=headers(), params={"q": "喜欢蓝鲸"}
        )
        assert fts_query.status_code == 200, fts_query.text
        assert len(fts_query.json()["items"]) == 1

    with TestClient(create_app(settings(tmp_path))) as restarted:
        recalled = restarted.post(
            "/v1/chat",
            headers=headers(),
            json={"message": "你还记得我喜欢什么吗？", "client_id": "test"},
        )
        assert recalled.status_code == 200
        events = restarted.get("/v1/events", headers=headers()).json()["items"]
        kinds = [item["kind"] for item in events]
        assert "user.message" in kinds
        assert "assistant.message" in kinds
        memory = restarted.get(
            "/v1/memory/search", headers=headers(), params={"q": "阿星"}
        ).json()["items"]
        assert memory[0]["content"] == "主人希望被称呼为阿星"


def test_event_log_is_append_only(tmp_path):
    db = Database(tmp_path / "append-only.db")
    event = db.append_event("主人", "user.message", {"content": "你好"})
    try:
        try:
            with db.conn:
                db.conn.execute(
                    "UPDATE event SET actor='别人' WHERE id=?", (event["id"],)
                )
            raise AssertionError("事件日志不应允许更新")
        except sqlite3.IntegrityError as exc:
            assert "append-only" in str(exc)
    finally:
        db.close()


def test_websocket_chat(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        with client.websocket_connect(f"/v1/ws?token={TOKEN}") as socket:
            socket.send_json({"message": "你好", "client_id": "ws-test"})
            body = socket.receive_json()
            assert "我在" in body["reply"]
            # presence 由状态机决定（真实时钟可能落在静默时段），
            # 只断言它是合法取值，不断言"一定是 在"。
            assert body["state"]["presence"] in {
                "在", "睡了", "打盹", "静默", "离开",
            }


def test_blank_message_is_rejected(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.post(
            "/v1/chat", headers=headers(), json={"message": "   ", "client_id": "test"}
        )
        assert response.status_code == 422
