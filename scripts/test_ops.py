#!/usr/bin/env python3
"""Local tests; all temporary files stay below scripts/ and are cleaned up."""
from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlparse
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backup
import restore
import upgrade
import vertical_slice_check as vertical


def create_database(path: Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS "odd table" ("value" TEXT)')
        connection.execute('DELETE FROM "odd table"')
        connection.executemany('INSERT INTO "odd table" VALUES (?)', [(f"记忆-{index}",) for index in range(rows)])
        connection.commit()
    finally:
        connection.close()


def row_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute('SELECT COUNT(*) FROM "odd table"').fetchone()[0])
    finally:
        connection.close()


class MockBrainHandler(BaseHTTPRequestHandler):
    events: list[dict] = []
    memories: list[dict] = []
    expected_token = vertical.DEFAULT_TOKEN

    def log_message(self, format: str, *args) -> None:
        pass

    def send_json(self, payload, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self) -> bool:
        if self.headers.get("X-Ling-Token") == self.expected_token:
            return True
        self.send_json({"error": "unauthorized"}, 401)
        return False

    def do_GET(self) -> None:
        if not self.authorized():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/v1/health":
            self.send_json({"status": "ok"})
        elif parsed.path == "/v1/events":
            self.send_json({"events": self.events})
        elif parsed.path == "/v1/state":
            self.send_json({"status": "ready", "event_count": len(self.events)})
        elif parsed.path == "/v1/memory/search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            matches = []
            for item in self.memories:
                content = item["memory"]["content"]
                if query in content or (query == "喜欢" and "喜欢" in content):
                    matches.append(item)
            # Deliberately nest actual memory content to exercise robust parsing.
            self.send_json({"data": {"memories": matches}})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self.authorized():
            return
        if urlparse(self.path).path != "/v1/chat":
            self.send_json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.events.append({"type": "chat.completed", **payload})
        self.memories.append({"memory": {"content": payload["message"]}})
        self.send_json({"data": {"reply": "好的，我会记住你喜欢桂花乌龙。"}})


class TemporaryTreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".ops-test-", dir=SCRIPT_DIR)
        self.root = Path(self.temporary.name)
        self.database = self.root / "data" / "lingjingjing.db"
        self.backups = self.root / "backups"
        create_database(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()


class DatabaseOperationsTest(TemporaryTreeTest):
    def test_backup_verifies_and_prunes_only_managed_files(self) -> None:
        old = backup.backup_database(self.database, self.backups, label="old")
        arbitrary = self.backups / "do-not-delete.db"
        shutil.copyfile(self.database, arbitrary)
        old_time = time.time() - 20 * 86400
        os.utime(old.path, (old_time, old_time))
        os.utime(arbitrary, (old_time, old_time))

        current = backup.backup_database(self.database, self.backups, label="current")
        self.assertFalse(old.path.exists())
        self.assertTrue(arbitrary.exists())
        self.assertIn(str(old.path), current.pruned)
        self.assertEqual(current.integrity_check, "ok")
        self.assertEqual(current.page_count, current.source_page_count)
        self.assertEqual(len(current.sha256), 64)
        self.assertEqual(backup.inspect_database(current.path)["table_row_counts"], {"odd table": 3})

    def test_restore_refuses_overwrite_then_force_makes_safety_backup(self) -> None:
        original = backup.backup_database(self.database, self.backups, label="original")
        create_database(self.database, rows=5)
        with self.assertRaises(PermissionError):
            restore.restore_database(original.path, self.database, safety_backup_dir=self.backups)
        self.assertEqual(row_count(self.database), 5)

        result = restore.restore_database(
            original.path,
            self.database,
            force=True,
            safety_backup_dir=self.backups,
        )
        self.assertEqual(row_count(self.database), 3)
        self.assertIsNotNone(result["safety_backup"])
        safety = Path(result["safety_backup"]["path"])
        self.assertTrue(safety.is_file())
        self.assertEqual(row_count(safety), 5)

    def test_upgrade_writes_validated_version_manifest(self) -> None:
        result = upgrade.create_upgrade_snapshot(self.database, self.backups, version="v1.2.3")
        self.assertTrue(result["validation"]["ok"])
        self.assertIn("pre-upgrade-v1.2.3", result["snapshot"]["path"])
        manifest_path = Path(result["manifest"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "v1.2.3")
        self.assertTrue(manifest["validation"]["checks"]["row_counts"]["ok"])
        self.assertTrue(manifest["validation"]["checks"]["file_sizes"]["ok"])
        self.assertTrue(manifest["validation"]["checks"]["timestamps"]["ok"])

    def test_upgrade_timestamp_failure_is_explicit(self) -> None:
        real_backup = upgrade.backup_database

        def change_source_after_snapshot(*args, **kwargs):
            result = real_backup(*args, **kwargs)
            future = time.time() + 3600
            os.utime(self.database, (future, future))
            return result

        with mock.patch.object(upgrade, "backup_database", side_effect=change_source_after_snapshot):
            with self.assertRaises(upgrade.UpgradeValidationError) as caught:
                upgrade.create_upgrade_snapshot(self.database, self.backups, version="v1-bad")
        self.assertTrue(caught.exception.manifest_path.is_file())
        manifest = json.loads(caught.exception.manifest_path.read_text(encoding="utf-8"))
        self.assertFalse(manifest["validation"]["ok"])
        self.assertFalse(manifest["validation"]["checks"]["timestamps"]["ok"])

    def test_upgrade_cli_validation_failure_returns_nonzero(self) -> None:
        with mock.patch.object(upgrade, "create_upgrade_snapshot", side_effect=ValueError("bad snapshot")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(upgrade.main(["--version", "v1"]), 0)


class VerticalSliceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockBrainHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        MockBrainHandler.events = []
        MockBrainHandler.memories = []

    def run_vertical(self, *extra: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = vertical.main([
                "--base-url", self.base_url,
                "--timeout", "2",
                "--settle-timeout", "2",
                *extra,
            ])
        return status, output.getvalue()

    def test_core_slice_and_transparent_skips(self) -> None:
        status, output = self.run_vertical("--skip-restart")
        self.assertEqual(status, 0, output)
        for name in ("健康检查", "中文聊天", "事件落盘", "中文记忆检索"):
            self.assertIn(f"[PASS] {name}", output)
        self.assertIn("[SKIP] 重开持久性", output)
        self.assertIn("[SKIP] 显存检查", output)
        self.assertIn("[SKIP] Tauri 检查", output)
        self.assertNotIn("[PASS] 显存检查", output)
        self.assertNotIn("[PASS] Tauri 检查", output)

    def test_optional_command_checks_pre_restart_memory(self) -> None:
        command = f'"{sys.executable}" -c "print(\'restart requested\')"'
        status, output = self.run_vertical("--command", command, "--restart-timeout", "10")
        self.assertEqual(status, 0, output)
        self.assertIn("[PASS] 重开持久性", output)

    def test_auth_failure_is_nonzero(self) -> None:
        status, output = self.run_vertical("--skip-restart", "--token", "wrong")
        self.assertNotEqual(status, 0)
        self.assertIn("[FAIL] 健康检查", output)


if __name__ == "__main__":
    unittest.main()
