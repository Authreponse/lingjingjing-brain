#!/usr/bin/env python3
"""Create and validate a versioned pre-upgrade SQLite snapshot.

This script deliberately does not run application migrations. It provides the
freeze checklist, a recoverable snapshot, and a manifest for the upgrade owner.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any
from uuid import uuid4

from backup import (
    DEFAULT_BACKUP_DIR,
    DEFAULT_DATABASE,
    DEFAULT_RETENTION_DAYS,
    BackupResult,
    backup_database,
    inspect_database,
    safe_component,
    sha256_file,
    utc_iso,
)

FREEZE_RECOMMENDATIONS = (
    "冻结数据库写入：正常停止大脑服务、定时任务及其他写入方。",
    "确认当前应用版本、迁移步骤和回滚负责人，再开始升级。",
    "快照校验通过前不要执行迁移；升级后保留快照与清单以便回滚。",
)


class UpgradeValidationError(RuntimeError):
    def __init__(self, errors: list[str], manifest_path: Path):
        super().__init__("；".join(errors))
        self.errors = errors
        self.manifest_path = manifest_path


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_upgrade_snapshot(
    database: Path,
    backup_dir: Path,
    *,
    version: str,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict[str, Any]:
    version = safe_component(version, field="版本标签")
    database = database.resolve()
    backup_dir = backup_dir.resolve()
    source_before = inspect_database(database)
    snapshot: BackupResult = backup_database(
        database,
        backup_dir,
        label=f"pre-upgrade-{version}",
        retention_days=retention_days,
    )
    source_after = inspect_database(database)
    snapshot_info = inspect_database(snapshot.path)
    errors: list[str] = []
    checks: dict[str, Any] = {}

    source_rows = source_after["table_row_counts"]
    snapshot_rows = snapshot_info["table_row_counts"]
    rows_ok = source_rows == snapshot_rows
    checks["row_counts"] = {
        "ok": rows_ok,
        "source_total": sum(source_rows.values()),
        "snapshot_total": sum(snapshot_rows.values()),
        "source_by_table": source_rows,
        "snapshot_by_table": snapshot_rows,
    }
    if not rows_ok:
        errors.append("源库与快照的逐表行数不一致（升级冻结期间可能仍有写入）")

    expected_snapshot_size = snapshot_info["page_count"] * snapshot_info["page_size"]
    size_ok = (
        source_after["size_bytes"] > 0
        and snapshot_info["size_bytes"] > 0
        and snapshot_info["size_bytes"] == expected_snapshot_size
    )
    checks["file_sizes"] = {
        "ok": size_ok,
        "source_bytes": source_after["size_bytes"],
        "snapshot_bytes": snapshot_info["size_bytes"],
        "expected_snapshot_bytes": expected_snapshot_size,
        "note": "WAL 模式下源主文件可与快照大小不同；快照必须正好等于 page_count × page_size。",
    }
    if not size_ok:
        errors.append("文件大小校验失败")

    timestamp_ok = (
        snapshot_info["mtime_ns"] >= source_after["mtime_ns"]
        and source_before["mtime_ns"] == source_after["mtime_ns"]
        and source_before["size_bytes"] == source_after["size_bytes"]
    )
    checks["timestamps"] = {
        "ok": timestamp_ok,
        "source_before_mtime_ns": source_before["mtime_ns"],
        "source_after_mtime_ns": source_after["mtime_ns"],
        "snapshot_mtime_ns": snapshot_info["mtime_ns"],
        "source_before_modified_at": source_before["modified_at"],
        "source_after_modified_at": source_after["modified_at"],
        "snapshot_modified_at": snapshot_info["modified_at"],
    }
    if not timestamp_ok:
        errors.append("时间戳/源文件稳定性校验失败（快照早于源库，或源库在快照期间发生变化）")

    integrity_ok = source_after["integrity_check"] == "ok" and snapshot_info["integrity_check"] == "ok"
    checks["integrity"] = {"ok": integrity_ok}
    if not integrity_ok:
        errors.append("SQLite 完整性校验失败")

    digest = sha256_file(snapshot.path)
    manifest_path = snapshot.path.with_name(snapshot.path.name + ".manifest.json")
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "pre-upgrade-snapshot",
        "version": version,
        "snapshot_tag": snapshot.path.name,
        "created_at": utc_iso(),
        "freeze_recommendations": list(FREEZE_RECOMMENDATIONS),
        "validation": {"ok": not errors, "errors": errors, "checks": checks},
        "source": source_after,
        "snapshot": {**snapshot_info, "sha256": digest},
        "backup": snapshot.as_dict(),
    }
    write_json_atomic(manifest_path, manifest)
    if errors:
        raise UpgradeValidationError(errors, manifest_path)
    return {
        "version": version,
        "snapshot": snapshot.as_dict(),
        "manifest": str(manifest_path),
        "validation": manifest["validation"],
        "freeze_recommendations": list(FREEZE_RECOMMENDATIONS),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="创建带版本标签和严格校验的升级前快照（不执行迁移）")
    parser.add_argument("--version", required=True, help="目标版本标签，例如 v1.0.0")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help=f"默认: {DEFAULT_DATABASE}")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR, help=f"默认: {DEFAULT_BACKUP_DIR}")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS, help="保留天数（默认 14）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print("升级前冻结建议：", file=sys.stderr)
    for index, recommendation in enumerate(FREEZE_RECOMMENDATIONS, 1):
        print(f"  {index}. {recommendation}", file=sys.stderr)
    try:
        result = create_upgrade_snapshot(
            args.database,
            args.backup_dir,
            version=args.version,
            retention_days=args.retention_days,
        )
    except UpgradeValidationError as exc:
        print(f"升级前校验失败: {exc}", file=sys.stderr)
        print(f"失败清单（含快照定位）: {exc.manifest_path}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"升级前快照失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "upgrade_snapshot": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
