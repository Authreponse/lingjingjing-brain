#!/usr/bin/env python3
"""Create verified SQLite backups with a bounded retention policy.

Python 3.11+, standard library only. The project root is derived from this
file's location, so the script works regardless of the caller's cwd.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "lingjingjing.db"
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "backups"
DEFAULT_RETENTION_DAYS = 14


def utc_iso(timestamp: float | None = None) -> str:
    """Return an unambiguous UTC timestamp."""
    moment = datetime.now(timezone.utc) if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_component(value: str, *, field: str = "label") -> str:
    """Validate a filename/version component instead of silently changing it."""
    value = value.strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        raise ValueError(f"{field} 须为 1-64 位字母、数字、点、下划线或连字符，且以字母或数字开头")
    return value


def quote_identifier(identifier: str) -> str:
    """Quote an SQLite identifier obtained from sqlite_master."""
    return '"' + identifier.replace('"', '""') + '"'


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing database read-only, including databases using WAL."""
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30.0)
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def table_row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        name: int(connection.execute(f"SELECT COUNT(*) FROM {quote_identifier(name)}").fetchone()[0])
        for name in names
    }


def inspect_database(path: Path, *, include_rows: bool = True) -> dict[str, Any]:
    """Return integrity, size, timestamp and optional per-table row counts."""
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"数据库不存在或不是普通文件: {path}")
    stat = path.stat()
    if stat.st_size <= 0:
        raise ValueError(f"数据库文件为空: {path}")
    connection = connect_readonly(path)
    try:
        integrity_rows = [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]
        integrity = "; ".join(integrity_rows)
        if integrity_rows != ["ok"]:
            raise ValueError(f"SQLite integrity_check 失败: {integrity}")
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        rows = table_row_counts(connection) if include_rows else None
    finally:
        connection.close()
    result: dict[str, Any] = {
        "path": str(path),
        "integrity_check": integrity,
        "page_count": page_count,
        "page_size": page_size,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "modified_at": utc_iso(stat.st_mtime),
    }
    if rows is not None:
        result["table_row_counts"] = rows
        result["total_user_table_rows"] = sum(rows.values())
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class BackupResult:
    path: Path
    created_at: str
    size_bytes: int
    sha256: str
    integrity_check: str
    page_count: int
    source_page_count: int
    retention_days: int
    pruned: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["path"] = str(self.path)
        value["pruned"] = list(self.pruned)
        return value


def _managed_backup_name(stem: str, name: str) -> bool:
    pattern = rf"^{re.escape(stem)}-\d{{8}}T\d{{6}}\.\d{{6}}Z-[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}\.db$"
    return re.fullmatch(pattern, name) is not None


def prune_backups(
    backup_dir: Path,
    *,
    stem: str,
    retention_days: int,
    keep: Path,
    now: float | None = None,
) -> tuple[str, ...]:
    """Prune only backups created by this tool, never arbitrary user files."""
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    pruned: list[str] = []
    for candidate in backup_dir.iterdir():
        if candidate == keep or not candidate.is_file() or not _managed_backup_name(stem, candidate.name):
            continue
        try:
            if candidate.stat().st_mtime >= cutoff:
                continue
            candidate.unlink()
            # An upgrade manifest belongs to the deleted snapshot.
            manifest = candidate.with_name(candidate.name + ".manifest.json")
            if manifest.is_file():
                manifest.unlink()
            pruned.append(str(candidate))
        except FileNotFoundError:
            continue
    return tuple(sorted(pruned))


def backup_database(
    database: Path,
    backup_dir: Path,
    *,
    label: str = "backup",
    retention_days: int = DEFAULT_RETENTION_DAYS,
    prune: bool = True,
) -> BackupResult:
    """Use sqlite3.Connection.backup and atomically publish a verified copy."""
    database = database.resolve()
    backup_dir = backup_dir.resolve()
    label = safe_component(label)
    if retention_days < 0:
        raise ValueError("retention_days 不能小于 0")
    if not database.is_file():
        raise ValueError(f"源数据库不存在或不是普通文件: {database}")
    if database.stat().st_size <= 0:
        raise ValueError(f"源数据库文件为空: {database}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    if database.parent == backup_dir:
        raise ValueError("备份目录不能与源数据库所在目录相同")

    stem = safe_component(database.stem, field="数据库文件名")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    filename = f"{stem}-{stamp}-{label}.db"
    destination = backup_dir / filename
    temporary = backup_dir / f".{filename}.{uuid4().hex}.tmp"
    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    try:
        source = connect_readonly(database)
        target = sqlite3.connect(str(temporary), timeout=30.0)
        source.backup(target)
        target.commit()
        integrity_rows = [row[0] for row in target.execute("PRAGMA integrity_check").fetchall()]
        if integrity_rows != ["ok"]:
            raise ValueError(f"备份 SQLite integrity_check 失败: {'; '.join(integrity_rows)}")
        backup_pages = int(target.execute("PRAGMA page_count").fetchone()[0])
        source_pages = int(source.execute("PRAGMA page_count").fetchone()[0])
        if backup_pages <= 0 or backup_pages != source_pages:
            raise ValueError(f"备份页数校验失败: source={source_pages}, backup={backup_pages}")
        target.close()
        target = None
        source.close()
        source = None
        os.replace(temporary, destination)
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        if temporary.exists():
            temporary.unlink()

    stat = destination.stat()
    if stat.st_size <= 0:
        destination.unlink(missing_ok=True)
        raise ValueError("备份发布失败：文件大小为 0")
    pruned = (
        prune_backups(backup_dir, stem=stem, retention_days=retention_days, keep=destination)
        if prune
        else ()
    )
    return BackupResult(
        path=destination,
        created_at=utc_iso(stat.st_mtime),
        size_bytes=stat.st_size,
        sha256=sha256_file(destination),
        integrity_check="ok",
        page_count=backup_pages,
        source_page_count=source_pages,
        retention_days=retention_days,
        pruned=pruned,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="用 SQLite backup API 创建带校验信息的备份")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help=f"默认: {DEFAULT_DATABASE}")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR, help=f"默认: {DEFAULT_BACKUP_DIR}")
    parser.add_argument("--label", default="backup", help="备份文件名标签（默认 backup）")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS, help="保留天数（默认 14）")
    parser.add_argument("--no-prune", action="store_true", help="本次不清理过期备份")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = backup_database(
            args.database,
            args.backup_dir,
            label=args.label,
            retention_days=args.retention_days,
            prune=not args.no_prune,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"备份失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "backup": result.as_dict()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
