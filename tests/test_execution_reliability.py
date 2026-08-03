from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import config
from agents.paradigm_orchestrator import (
    ParadigmOrchestrator,
    _execution_deadlines,
    _origin_analysis_priority,
    _origin_execution_order,
)
from database.paradigm_store import ParadigmStore
from database.state_migration import migrate_state
from notifications.email_notifier import send_failure_email
from paradigms.models import EvidenceType, ParadigmExtraction, TechnicalEvidence
from reports.paradigm_generator import ParadigmReportGenerator


def _origin(title: str, *, priority: int = 1) -> TechnicalEvidence:
    return TechnicalEvidence(
        source="arxiv",
        evidence_type=EvidenceType.PRIMARY_PAPER,
        title=title,
        url=f"https://arxiv.org/abs/{title}",
        published_at="2026-08-01T00:00:00Z",
        raw={"origin_priority": priority, "origin_kind": "research_paper"},
    )


def _rejected_extraction(evidence: TechnicalEvidence) -> ParadigmExtraction:
    return ParadigmExtraction(
        evidence=evidence,
        is_candidate=False,
        canonical_name="",
        thesis="",
        problem_shift="",
        mechanism="",
        rejection_reason="Rubric 未通过",
        rubric_assessment={"decision": "reject", "answer_coverage": 1.0},
    )


class ExecutionReliabilityTests(unittest.TestCase):
    def test_legacy_state_is_migrated_instead_of_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE evidence_state (
                        fingerprint TEXT PRIMARY KEY,
                        content_signature TEXT NOT NULL,
                        source TEXT NOT NULL,
                        evidence_type TEXT NOT NULL,
                        url TEXT,
                        payload_json TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        last_analyzed_at TEXT
                    );
                    CREATE TABLE paradigms (
                        paradigm_key TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        status TEXT NOT NULL,
                        total_score REAL NOT NULL,
                        payload_json TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        last_reported_signature TEXT,
                        last_reported_at TEXT
                    );
                    CREATE TABLE report_deliveries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        paradigm_key TEXT NOT NULL,
                        report_signature TEXT NOT NULL,
                        report_path TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        delivered_at TEXT NOT NULL,
                        report_kind TEXT NOT NULL,
                        UNIQUE(paradigm_key, report_signature)
                    );
                    CREATE TABLE paradigm_evidence (
                        paradigm_key TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        first_linked_at TEXT NOT NULL,
                        PRIMARY KEY(paradigm_key, fingerprint)
                    );
                    """
                )

            version = migrate_state(database, source_version="")

            with sqlite3.connect(database) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertEqual(version, config.PARADIGM_STATE_SCHEMA_VERSION)
            self.assertIn("radar_meta", tables)

    def test_future_state_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.db"
            database.write_bytes(b"not opened because version is rejected first")
            with self.assertRaisesRegex(ValueError, "兼容范围"):
                migrate_state(
                    database,
                    source_version=config.PARADIGM_STATE_SCHEMA_VERSION + 1,
                )

    def test_origin_batches_checkpoint_and_leave_remainder_pending(self) -> None:
        first, second = _origin("2608.00001"), _origin("2608.00002")
        orchestrator = object.__new__(ParadigmOrchestrator)
        orchestrator.enricher = SimpleNamespace(
            hydrate_priority_origins=AsyncMock(
                return_value={
                    "priority_origin_targets": 0,
                    "priority_origin_hydrated": 0,
                    "priority_origin_hydration_failed": 0,
                }
            )
        )
        orchestrator.analyzer = SimpleNamespace(
            run=AsyncMock(return_value=[_rejected_extraction(first)])
        )
        orchestrator.store = SimpleNamespace(mark_evidence=Mock())

        with (
            patch.object(config, "PARADIGM_ANALYSIS_BATCH_SIZE", 1),
            patch(
                "agents.paradigm_orchestrator._remaining_seconds",
                side_effect=[5.0, 0.0],
            ),
        ):
            result = asyncio.run(
                orchestrator._analyze_origins_in_batches(
                    [first, second], deadline=123.0
                )
            )

        extractions, attempted, failed, deferred, _ = result
        self.assertEqual(len(extractions), 1)
        self.assertEqual(attempted, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(deferred, [second])
        orchestrator.store.mark_evidence.assert_called_once_with(
            [first], analyzed=True
        )

    def test_repeated_failure_metadata_survives_rediscovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            pending = _origin("2608.00003")
            pending.raw["analysis_failure_count"] = 2
            store.mark_evidence([pending], analyzed=False)
            rediscovered = _origin("2608.00003")

            planned, _ = store.plan_origins([rediscovered])

        self.assertEqual(planned[0].raw["analysis_failure_count"], 2)

    def test_changed_content_deferred_by_budget_remains_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            original = _origin("2608.00004")
            original.summary = "version one"
            store.mark_evidence([original], analyzed=True)
            changed = _origin("2608.00004")
            changed.summary = "version two with a new mechanism"
            planned, _ = store.plan_origins([changed])
            store.mark_evidence(planned, analyzed=False)

            backlog = store.load_pending_origins()

        self.assertEqual([item.summary for item in backlog], [changed.summary])

    def test_priority_order_is_operational_not_a_truncation(self) -> None:
        ordinary = _origin("ordinary", priority=1)
        report = _origin("report", priority=3)
        report.raw.update(
            {
                "origin_kind": "technical_report",
                "publisher_tier": "established",
            }
        )
        self.assertEqual(
            sorted(
                [ordinary, report],
                key=_origin_analysis_priority,
                reverse=True,
            ),
            [report, ordinary],
        )

    def test_new_origins_and_fifo_backlog_are_interleaved(self) -> None:
        pending_one = _origin("pending-1")
        pending_two = _origin("pending-2")
        new_one = _origin("new-1")
        new_two = _origin("new-2")
        ordered = _origin_execution_order(
            [pending_one, pending_two], [new_one, new_two]
        )
        self.assertEqual(
            ordered,
            [new_one, pending_one, new_two, pending_two],
        )

    def test_repeated_structural_failure_does_not_starve_peer_reports(self) -> None:
        failing = _origin("failing-report", priority=3)
        failing.raw.update(
            {
                "origin_kind": "technical_report",
                "analysis_failure_count": 2,
            }
        )
        untried = _origin("untried-report", priority=3)
        untried.raw["origin_kind"] = "technical_report"
        ordered = _origin_execution_order([failing, untried], [])
        self.assertEqual(ordered, [untried, failing])

    def test_execution_budget_reserves_deep_and_report_time(self) -> None:
        with (
            patch.object(config, "PARADIGM_RUN_BUDGET_SECONDS", 3900),
            patch.object(config, "PARADIGM_STAGE_RESERVE_SECONDS", 600),
        ):
            run, origin, deep, reserve = _execution_deadlines(100.0)
        self.assertEqual((run, origin, deep, reserve), (4000.0, 2800.0, 3400.0, 600))

    def test_empty_report_discloses_runtime_backlog(self) -> None:
        content = ParadigmReportGenerator._empty_report(
            "2026-08-03",
            {
                "origin_count": 100,
                "planned_analysis_count": 100,
                "analysis_count": 12,
                "analysis_deferred_count": 88,
                "candidate_deferred_count": 0,
                "refresh_deferred_count": 0,
                "pending_work_count": 88,
            },
        )
        self.assertIn("尚未完成研究判断的执行积压", content)
        self.assertIn("不能解释为近期没有新范式", content)
        self.assertNotIn("100 篇论文、Technical Report 与官方技术博客，但没有材料", content)

    def test_failure_notification_does_not_require_a_report(self) -> None:
        with (
            patch.object(config, "EMAIL_PUSH_ENABLED", True),
            patch.object(config, "SMTP_HOST", "smtp.example.com"),
            patch.object(config, "SMTP_PORT", 465),
            patch.object(config, "SMTP_USERNAME", "sender@example.com"),
            patch.object(config, "SMTP_PASSWORD", "app-password"),
            patch.object(config, "SMTP_FROM", "sender@example.com"),
            patch.object(config, "SMTP_TO", ["receiver@example.com"]),
            patch.object(config, "SMTP_USE_SSL", True),
            patch("notifications.email_notifier.smtplib.SMTP_SSL") as smtp,
        ):
            sent = asyncio.run(
                send_failure_email(
                    {
                        "event": "schedule",
                        "run_id": "82990689675",
                        "run_url": "https://github.example/run/82990689675",
                    }
                )
            )

        self.assertTrue(sent)
        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertIn("[运行失败]", message["Subject"])
        self.assertIn("82990689675", message.get_content())
        self.assertFalse(message.is_multipart())

    def test_workflow_has_migration_soft_budget_and_failure_alert(self) -> None:
        workflow = Path(".github/workflows/weekly-radar.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("database.state_migration", workflow)
        self.assertIn("PARADIGM_RUN_BUDGET_SECONDS", workflow)
        self.assertIn("--notify-failure", workflow)
        self.assertIn("steps.pipeline.outcome != 'success'", workflow)
        self.assertNotIn('if [ "$state_schema" !=', workflow)


if __name__ == "__main__":
    unittest.main()
