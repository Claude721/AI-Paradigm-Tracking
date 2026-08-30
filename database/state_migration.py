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
        "last_refresh_attempt_at",
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
        "delivery_key",
    },
    "report_outbox": {
        "delivery_key",
        "report_date",
        "report_name",
        "status",
        "candidate_payload_json",
        "stats_json",
        "report_content",
        "attempt_count",
        "render_attempt_count",
        "last_error",
        "failure_kind",
        "created_at",
        "updated_at",
        "delivered_at",
        "quarantined_at",
    },
    "report_render_fragments": {
        "delivery_key",
        "fragment_key",
        "content",
        "created_at",
        "updated_at",
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

    Version 2 added ``radar_meta``; version 3 added the durable report outbox and
    a delivery identifier; version 4 added resumable route-level report fragments;
    version 5 adds fair refresh scheduling plus domain-payload validation;
    version 6 adds render-attempt accounting and quarantined delivery recovery.
    All additions are backwards-compatible, so opening
    the database with :class:`ParadigmStore` performs the migration.  Future
    versions must extend this function before raising the schema version.
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
    _validate_domain_payloads(path)
    _quarantine_incompatible_outbox(path)
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
        required.update(
            {"radar_meta", "report_outbox", "report_render_fragments"}
        )
    missing = required - tables
    if missing:
        raise ValueError(f"状态数据库缺少必需表: {sorted(missing)}")
    for table in required:
        expected_columns = set(_REQUIRED_COLUMNS[table])
        if not require_current and table == "report_deliveries":
            expected_columns.discard("delivery_key")
        if not require_current and table == "paradigms":
            expected_columns.discard("last_refresh_attempt_at")
        missing_columns = expected_columns - columns.get(table, set())
        if missing_columns:
            raise ValueError(
                f"状态数据库表 {table} 缺少字段: {sorted(missing_columns)}"
            )


def _validate_domain_payloads(path: Path) -> None:
    """Reject structurally valid SQLite files with corrupt domain JSON.

    GitHub recovery walks immutable snapshots from newest to oldest. Raising
    here lets it fall back before the production pipeline spends tokens or
    sends mail with a state file that can never be deserialized.
    """

    import json

    from paradigms.models import candidate_from_dict, technical_evidence_from_dict

    try:
        with sqlite3.connect(path) as connection:
            for fingerprint, payload_json in connection.execute(
                "SELECT fingerprint, payload_json FROM evidence_state"
            ):
                evidence = technical_evidence_from_dict(json.loads(payload_json))
                if evidence.fingerprint != str(fingerprint):
                    raise ValueError(
                        f"证据 fingerprint 与 payload 不一致: {fingerprint}"
                    )
            for paradigm_key, payload_json in connection.execute(
                "SELECT paradigm_key, payload_json FROM paradigms"
            ):
                candidate = candidate_from_dict(json.loads(payload_json))
                if candidate.key != str(paradigm_key):
                    raise ValueError(
                        f"范式 key 与 payload 不一致: {paradigm_key}"
                    )
            for delivery_key, status, candidates_json, stats_json, content in (
                connection.execute(
                    """
                    SELECT delivery_key, status, candidate_payload_json,
                           stats_json, report_content
                    FROM report_outbox
                    """
                )
            ):
                if str(status) not in {
                    "pending_render",
                    "rendered",
                    "sending",
                    "delivered",
                    "quarantined",
                }:
                    raise ValueError(
                        f"交付任务状态不可识别: {delivery_key}={status}"
                    )
                payloads = json.loads(candidates_json)
                stats = json.loads(stats_json)
                if not isinstance(payloads, list) or not isinstance(stats, dict):
                    raise ValueError(f"交付任务 JSON 结构损坏: {delivery_key}")
                for payload in payloads:
                    candidate_from_dict(payload)
                if str(status) in {"rendered", "sending"} and not str(content):
                    raise ValueError(
                        f"交付任务状态为 {status} 但没有报告制品: {delivery_key}"
                    )
            for delivery_id, paradigm_key, payload_json in connection.execute(
                "SELECT id, paradigm_key, payload_json FROM report_deliveries"
            ):
                candidate = candidate_from_dict(json.loads(payload_json))
                if candidate.key != str(paradigm_key):
                    raise ValueError(
                        "历史交付中的范式 key 与 payload 不一致: "
                        f"delivery={delivery_id}"
                    )
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        raise ValueError(f"状态数据库包含不可恢复的领域 JSON: {exc}") from exc


def _quarantine_incompatible_outbox(path: Path) -> None:
    """Move old, structurally valid but no-longer-deliverable jobs aside.

    Report contracts evolve more quickly than the SQLite schema.  A frozen
    snapshot that lacks today's verified-person or primary-source fields cannot
    be repaired by rerunning the renderer, so restoring it as the active head of
    queue would block every weekly run.  Keep the row for audit and atomically
    return its candidates to ``pending_deep`` instead.
    """

    from database.paradigm_store import ParadigmStore
    from reports.paradigm_generator import _report_input_violations

    store = ParadigmStore(path)
    with sqlite3.connect(path) as connection:
        keys = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT delivery_key FROM report_outbox
                WHERE status IN ('pending_render', 'rendered', 'sending')
                ORDER BY created_at ASC
                """
            )
        ]
    for delivery_key in keys:
        job = store.get_report_job(delivery_key)
        if job is None:
            continue
        violations = _report_input_violations(job.candidates)
        if not violations:
            continue
        store.quarantine_report_job(
            delivery_key,
            "状态升级后交付输入不再满足当前契约：" + "；".join(violations),
            failure_kind="state_contract_migration",
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
