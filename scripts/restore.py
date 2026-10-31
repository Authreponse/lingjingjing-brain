#!/usr/bin/env python3
"""Restore a verified SQLite backup without accidental overwrite."""
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
    backup_database,
    connect_readonly,
    inspect_database,
)


def sidecar_paths(database: Path) -> tuple[Path, Path]:
    return Path(str(database) + "-wal"), Path(str(database) + "-shm")


def restore_database(
    backup: Path,
    database: Path,
    *,
    force: bool = False,
    safety_backup_dir: Path = DEFAULT_BACKUP_DIR,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict[str, Any]:
    backup = backup.resolve()
    database = database.resolve()
    safety_backup_dir = safety_backup_dir.resolve()
    if not backup.is_file():
        raise ValueError(f"恢复源不存在或不是普通文件: {backup}")
    if backup == database:
        raise ValueError("恢复源与目标是同一个文件，已拒绝")
    if database.exists() and not database.is_file():
        raise ValueError(f"目标存在但不是普通文件: {database}")
    if database.exists() and not force:
        raise PermissionError(f"目标数据库已存在，默认拒绝覆盖；确认后显式添加 --force: {database}")

    # Validate the source before creating any target-side changes.
    source_info = inspect_database(backup)
    safety = None
    if database.exists():
        # A clean service shutdown normally removes WAL/SHM. Refusing stale or
        # live sidecars prevents old WAL frames being replayed onto the restore.
        sidecars = [str(path) for path in sidecar_paths(database) if path.exists()]
        if sidecars:
            raise ValueError("目标旁存在 WAL/SHM，无法安全替换；请先正常停止服务并完成检查点: " + ", ".join(sidecars))
        # This must complete successfully before the atomic overwrite below.
        safety = backup_database(
            database,
            safety_backup_dir,
            label="pre-restore",
            retention_days=retention_days,
        )
    else:
        stale_sidecars = [str(path) for path in sidecar_paths(database) if path.exists()]
        if stale_sidecars:
            raise ValueError("目标虽不存在但存在遗留 WAL/SHM，已拒绝恢复: " + ", ".join(stale_sidecars))

    database.parent.mkdir(parents=True, exist_ok=True)
    temporary = database.parent / f".{database.name}.{uuid4().hex}.restore.tmp"
    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    try:
        # The backup API also makes restore consistent for a WAL-mode source.
        source = connect_readonly(backup)
        target = sqlite3.connect(str(temporary), timeout=30.0)
        source.backup(target)
        target.commit()
        target.close()
        target = None
        source.close()
        source = None
        staged_info = inspect_database(temporary)
        if staged_info["table_row_counts"] != source_info["table_row_counts"]:
            raise ValueError("暂存恢复文件行数与备份不一致，已取消替换")
        os.replace(temporary, database)
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        if temporary.exists():
            temporary.unlink()

    restored_info = inspect_database(database)
    if restored_info["table_row_counts"] != source_info["table_row_counts"]:
        raise ValueError("恢复后行数校验失败；原库安全备份仍保留，请立即检查")
    return {
        "restored_from": str(backup),
        "database": str(database),
        "forced_overwrite": bool(safety),
        "safety_backup": safety.as_dict() if safety else None,
        "verification": restored_info,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从校验过的 SQLite 备份恢复；覆盖必须显式 --force")
    parser.add_argument("backup", type=Path, help="用于恢复的备份 .db 文件")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help=f"目标数据库（默认 {DEFAULT_DATABASE}）")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR, help="覆盖前安全备份目录")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS, help="安全备份保留天数（默认 14）")
    parser.add_argument("--force", action="store_true", help="允许覆盖；覆盖前自动创建安全备份")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = restore_database(
            args.backup,
            args.database,
            force=args.force,
            safety_backup_dir=args.backup_dir,
            retention_days=args.retention_days,
        )
    except (OSError, sqlite3.Error, ValueError, PermissionError) as exc:
        print(f"恢复失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "restore": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
