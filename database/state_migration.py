"""Validate and migrate persisted paradigm-radar SQLite state.

GitHub Actions artifacts may outlive the code revision that created them.  The
state schema is therefore migrated by the application instead of being
discarded merely because the metadata version changed.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import config


MIN_COMPATIBLE_STATE_SCHEMA_VERSION = 1

_REQUIRED_TABLES = {
    "evidence_state",
    "paradigms",
    "report_deliveries",
    "paradigm_evidence",
}
_REQUIRED_COLUMNS = {
    "evidence_state": {
        "fingerprint",
        "content_signature",
        "source",
        "evidence_type",
        "url",
        "payload_json",
        "first_seen_at",
        "last_seen_at",
        "last_analyzed_at",
    },
    "paradigms": {
        "paradigm_key",
        "name",
        "status",
        "total_score",
        "payload_json",
        "first_seen_at",
        "last_seen_at",
        "last_reported_signature",
        "last_reported_at",
    },
    "report_deliveries": {
        "id",
        "paradigm_key",
        "report_signature",
        "report_path",
        "payload_json",
        "delivered_at",
        "report_kind",
    },
    "paradigm_evidence": {
        "paradigm_key",
        "fingerprint",
        "first_linked_at",
    },
    "radar_meta": {"key", "value", "updated_at"},
}


def migrate_state(
    db_path: Path | str,
    source_version: int | str | None = None,
) -> int:
    """Validate an artifact and apply all backwards-compatible migrations.

    Version 1 and version 2 share the same core tables.  Version 2 adds
    ``radar_meta`` via ``CREATE TABLE IF NOT EXISTS``, so opening the database
    with :class:`ParadigmStore` is the migration.  Future versions must extend
    this function before raising ``PARADIGM_STATE_SCHEMA_VERSION``.
    """

    path = Path(db_path)
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"状态数据库不存在或为空: {path}")

    version = _normalize_version(source_version)
    if not (
        MIN_COMPATIBLE_STATE_SCHEMA_VERSION
        <= version
        <= config.PARADIGM_STATE_SCHEMA_VERSION
    ):
        raise ValueError(
            "状态 schema 不在兼容范围: "
            f"artifact={version}, supported="
            f"{MIN_COMPATIBLE_STATE_SCHEMA_VERSION}-"
            f"{config.PARADIGM_STATE_SCHEMA_VERSION}"
        )

    _validate_sqlite(path, require_current=False)

    # Imported lazily so the module can print the current version without
    # opening or creating the configured production database.
    from database.paradigm_store import ParadigmStore

    ParadigmStore(path)
    _validate_sqlite(path, require_current=True)
    return config.PARADIGM_STATE_SCHEMA_VERSION


def _normalize_version(value: int | str | None) -> int:
    # Artifacts created before schema metadata was introduced are the known
    # version-1 layout.  Missing metadata must not silently mean "current".
    if value is None or str(value).strip() == "":
        return MIN_COMPATIBLE_STATE_SCHEMA_VERSION
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"无法识别状态 schema 版本: {value}") from exc


def _validate_sqlite(path: Path, *, require_current: bool) -> None:
    try:
        with sqlite3.connect(path) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if not integrity or str(integrity[0]).casefold() != "ok":
                raise ValueError(f"SQLite 完整性检查失败: {integrity}")
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            columns = {
                table: {
                    str(row[1])
                    for row in connection.execute(
                        f'PRAGMA table_info("{table}")'
                    ).fetchall()
                }
                for table in tables
                if table in _REQUIRED_COLUMNS
            }
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"不是可恢复的 SQLite 状态数据库: {exc}") from exc

    required = set(_REQUIRED_TABLES)
    if require_current:
        required.add("radar_meta")
    missing = required - tables
    if missing:
        raise ValueError(f"状态数据库缺少必需表: {sorted(missing)}")
    for table in required:
        missing_columns = _REQUIRED_COLUMNS[table] - columns.get(table, set())
        if missing_columns:
            raise ValueError(
                f"状态数据库表 {table} 缺少字段: {sorted(missing_columns)}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移并校验 AI Radar 状态数据库")
    parser.add_argument("db_path", nargs="?", type=Path)
    parser.add_argument("--from-version", default="")
    parser.add_argument("--print-current-version", action="store_true")
    args = parser.parse_args()
    if args.print_current_version:
        print(config.PARADIGM_STATE_SCHEMA_VERSION)
        return
    if args.db_path is None:
        parser.error("db_path is required unless --print-current-version is used")
    version = migrate_state(args.db_path, args.from_version)
    print(f"状态数据库已迁移并通过校验: schema {version}")


if __name__ == "__main__":
    main()
