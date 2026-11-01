from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 2 = V4 提供商注册表与路由配置（provider / routing 两张新表）
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_ts ON event(ts);
CREATE INDEX IF NOT EXISTS idx_event_kind_ts ON event(kind, ts);
CREATE TRIGGER IF NOT EXISTS event_no_update
BEFORE UPDATE ON event BEGIN SELECT RAISE(ABORT, 'event is append-only'); END;
CREATE TRIGGER IF NOT EXISTS event_no_delete
BEFORE DELETE ON event BEGIN SELECT RAISE(ABORT, 'event is append-only'); END;

CREATE TABLE IF NOT EXISTS memory (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    who TEXT NOT NULL,
    content TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0.5,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    source_event_id TEXT REFERENCES event(id)
);
CREATE INDEX IF NOT EXISTS idx_memory_content ON memory(content);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content,
    content='memory',
    content_rowid='rowid',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
    INSERT INTO memory_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intent (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    due_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS autobiography (
    id TEXT PRIMARY KEY,
    week TEXT NOT NULL,
    change TEXT NOT NULL,
    evidence_ids TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voice (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    topic TEXT NOT NULL,
    source_url TEXT,
    stance TEXT NOT NULL,
    mood TEXT,
    basis_ids TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artwork (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    prompt TEXT NOT NULL,
    reason TEXT NOT NULL,
    mood TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preference (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    polarity TEXT NOT NULL,
    strength REAL NOT NULL,
    evidence_ids TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS letter (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    "from" TEXT NOT NULL,
    "to" TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handover (
    id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    content TEXT NOT NULL,
    evidence_ids TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- V4：模型提供商注册表。api_key 只写不读，接口层永不返回它。
CREATE TABLE IF NOT EXISTS provider (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    capabilities TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unchecked',
    is_builtin INTEGER NOT NULL DEFAULT 0,
    last_checked_at TEXT,
    latency_ms INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_name ON provider(name);
-- V4：路由配置。单行表（id 固定为 'default'），整体读写。
CREATE TABLE IF NOT EXISTS routing (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    active_provider_id TEXT,
    enabled_ids TEXT NOT NULL,
    assignments TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=5000")
            # 迁移策略：SCHEMA 全部是 CREATE ... IF NOT EXISTS，天然幂等。
            # 每次打开都跑一遍，旧库（version=1）会自动补出 V4 的 provider/routing 表，
            # 新库则一次建好 —— 两条路径走同一份 DDL，不存在"只升级部分环境"的分叉。
            self.conn.executescript(SCHEMA)
            for version in range(1, SCHEMA_VERSION + 1):
                self.conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utc_now()),
                )
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._ensure_initial_state()

    def _ensure_initial_state(self) -> None:
        initial = {
            "energy": 100,
            "mood": "平静",
            "presence": "在",
            "sleeping": False,
            "started_at": utc_now(),
            "last_heartbeat": utc_now(),
        }
        with self._lock, self.conn:
            for key, value in initial.items():
                self.conn.execute(
                    "INSERT OR IGNORE INTO state(key, value, updated_at) VALUES (?, ?, ?)",
                    (key, _json(value), utc_now()),
                )

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "ts": row["ts"],
            "actor": row["actor"],
            "kind": row["kind"],
            "payload": json.loads(row["payload"]),
        }

    @staticmethod
    def _memory(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "who": row["who"],
            "content": row["content"],
            "weight": row["weight"],
            "ts": row["ts"],
            "source": row["source"],
            "source_event_id": row["source_event_id"],
        }

    def append_event(
        self, actor: str, kind: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        event = {
            "id": str(uuid.uuid4()),
            "ts": utc_now(),
            "actor": actor,
            "kind": kind,
            "payload": payload,
        }
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO event(id, ts, actor, kind, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    event["id"],
                    event["ts"],
                    actor,
                    kind,
                    _json(payload),
                ),
            )
        return event

    def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM event ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._event(row) for row in rows]

    def recent_messages(self, limit: int = 20) -> list[dict[str, str]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT actor, payload FROM event
                WHERE kind IN ('user.message', 'assistant.message')
                ORDER BY ts DESC, rowid DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        messages = []
        for row in reversed(rows):
            payload = json.loads(row["payload"])
            messages.append(
                {
                    "role": "user" if row["actor"] == "主人" else "assistant",
                    "content": payload.get("content", ""),
                }
            )
        return messages

    def add_memory(
        self,
        kind: str,
        who: str,
        content: str,
        weight: float,
        source: str,
        source_event_id: str | None,
    ) -> dict[str, Any]:
        with self._lock, self.conn:
            existing = self.conn.execute(
                "SELECT * FROM memory WHERE kind=? AND who=? AND content=?",
                (kind, who, content),
            ).fetchone()
            if existing:
                return self._memory(existing)
            memory_id = str(uuid.uuid4())
            ts = utc_now()
            self.conn.execute(
                """
                INSERT INTO memory(id, kind, who, content, weight, ts, source, source_event_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (memory_id, kind, who, content, weight, ts, source, source_event_id),
            )
            row = self.conn.execute(
                "SELECT * FROM memory WHERE id=?", (memory_id,)
            ).fetchone()
        return self._memory(row)

    def search_memory(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        with self._lock:
            if len(query) < 3:
                rows = self.conn.execute(
                    "SELECT * FROM memory WHERE content LIKE ? ORDER BY weight DESC, ts DESC LIMIT ?",
                    (f"%{query}%", limit),
                ).fetchall()
            else:
                phrase = '"' + query.replace('"', '""') + '"'
                rows = self.conn.execute(
                    """
                    SELECT m.* FROM memory_fts f
                    JOIN memory m ON m.rowid=f.rowid
                    WHERE memory_fts MATCH ?
                    ORDER BY m.weight DESC, m.ts DESC LIMIT ?
                    """,
                    (phrase, limit),
                ).fetchall()
        return [self._memory(row) for row in rows]

    def important_memories(self, limit: int = 8) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM memory ORDER BY weight DESC, ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._memory(row) for row in rows]

    def set_state(self, values: dict[str, Any]) -> None:
        with self._lock, self.conn:
            for key, value in values.items():
                self.conn.execute(
                    """
                    INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (key, _json(value), utc_now()),
                )

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            rows = self.conn.execute("SELECT key, value FROM state").fetchall()
        return {row["key"]: json.loads(row["value"]) for row in rows}

    def heartbeat(self) -> dict[str, Any]:
        state = self.get_state()
        energy = min(100, int(state.get("energy", 100)) + 1)
        self.set_state({"last_heartbeat": utc_now(), "energy": energy})
        return self.get_state()

    def integrity_check(self) -> str:
        with self._lock:
            return self.conn.execute("PRAGMA integrity_check").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
