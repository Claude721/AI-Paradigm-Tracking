"""Closed-only delivery, including older snapshots and alternate entry points."""

from __future__ import annotations

import json
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import main
from database.paradigm_store import ParadigmStore
from database.state_migration import migrate_state
from notifications.email_notifier import _send_sync, send_report_email
from paradigms.completion import (
    ResearchNotCompleteError, _PENDING_COUNTS, require_completed_research,
)
from paradigms.models import EvidenceType, ParadigmCandidate, TechnicalEvidence
from reports.paradigm_generator import ParadigmReportGenerator
from run_audit import run_audit
from tests.completion_fixtures import COMPLETED_RESEARCH


class CompletionContractTests(unittest.TestCase):
    def test_absent_proof_cannot_certify_an_empty_result(self):
        for stats in ({}, {"high_value_count": 0}, {"pending_work_count": 0}):
            with self.subTest(stats=stats), self.assertRaises(ResearchNotCompleteError):
                require_completed_research(stats)

    def test_false_summary_flags_cannot_hide_any_deferred_ledger(self):
        for key in _PENDING_COUNTS:
            with self.subTest(key=key), self.assertRaises(ResearchNotCompleteError):
                require_completed_research({**COMPLETED_RESEARCH, key: 1})
        with self.assertRaises(ResearchNotCompleteError):
            require_completed_research({**COMPLETED_RESEARCH, "run_incomplete": True})

    def test_invalid_counts_and_flags_are_not_completion_proof(self):
        for value in ("0", -1, float("nan"), float("inf"), True, None):
            with self.subTest(value=value), self.assertRaises(ResearchNotCompleteError):
                require_completed_research({**COMPLETED_RESEARCH, "pending_work_count": value})
        with self.assertRaises(ResearchNotCompleteError):
            require_completed_research({**COMPLETED_RESEARCH, "research_incomplete": "false"})

    def test_planned_counts_and_durable_queues_must_close(self):
        for detail in (
            {"planned_analysis_count": 3, "analysis_completed_count": 2},
            {"planned_deep_candidate_count": 3, "deep_candidate_count": 2},
            {"work_queue_after": {"pending_deep_count": 1}},
        ):
            with self.subTest(detail=detail), self.assertRaises(ResearchNotCompleteError):
                require_completed_research({**COMPLETED_RESEARCH, **detail})

    def test_coverage_ledger_cannot_be_hidden_by_false_flags(self):
        for frontier in (
            {"domains": {"ai": {"status": "query_failed"}}},
            {"recall_lanes": {"authors": {"status": "not_executed_budget"}}},
            {"academic_indexes": {"arxiv": {"status": "partial"}}},
            {"source_health": {"feed": {"status": "timed_out"}}},
            {"official_pages": {"total_pages": 2, "checked_pages": 1}},
            {"official_repositories": {"repository_page_failures": ["fixture"]}},
        ):
            with self.subTest(frontier=frontier), self.assertRaises(ResearchNotCompleteError):
                require_completed_research({**COMPLETED_RESEARCH, "frontier_coverage": frontier})

    def test_configured_successful_zero_hits_are_a_closed_result(self):
        require_completed_research({
            **COMPLETED_RESEARCH,
            "planned_analysis_count": 0, "analysis_completed_count": 0,
            "frontier_coverage": {
                "recall_lanes": {"landscape": {"status": "searched_zero_hits"}},
                "academic_indexes": {"optional": {"status": "not_configured"}},
                "source_health": {"optional": {"status": "disabled"}},
            },
        })
        content = ParadigmReportGenerator._empty_report("2026-10-08", dict(COMPLETED_RESEARCH))
        self.assertIn("本期没有可交付的新路线", content)
        self.assertNotIn("阶段性", content)
        self.assertNotIn("待续跑", content)

    def test_unknown_coverage_status_is_not_completion_proof(self):
        with self.assertRaises(ResearchNotCompleteError):
            require_completed_research({
                **COMPLETED_RESEARCH,
                "frontier_coverage": {"source_health": {"fixture": {"status": "unknown"}}},
            })


class CompletionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.store = ParadigmStore(self.path / "state.db")
        run_audit.reset()

    async def test_generator_rejects_before_editor_or_file_write(self):
        editor = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())))
        generator = ParadigmReportGenerator(self.path, client=editor)
        with self.assertRaises(ResearchNotCompleteError):
            await generator.generate([], {**COMPLETED_RESEARCH, "pending_work_count": 1})
        editor.chat.completions.create.assert_not_awaited()
        self.assertEqual(list(self.path.glob("*.md")), [])

    async def test_incomplete_pipeline_fails_without_outbox_or_delivery(self):
        orchestrator = SimpleNamespace(
            store=self.store, pending_delivery=[],
            run=AsyncMock(return_value={**COMPLETED_RESEARCH, "pending_work_count": 12}),
        )
        delivery = AsyncMock()
        with (
            patch.object(config, "PIPELINE_MODE", "paradigm"),
            patch("main._check_env"), patch("main._print_model_banner"),
            patch("agents.paradigm_orchestrator.ParadigmOrchestrator", return_value=orchestrator),
            patch("reports.paradigm_generator.ParadigmReportGenerator"),
            patch("main._deliver_paradigm_job", delivery),
            patch("main._write_pipeline_result") as marker,
            patch.object(run_audit, "write", return_value={}),
        ):
            with self.assertRaises(ResearchNotCompleteError):
                await main._run_pipeline_once()
        delivery.assert_not_awaited()
        self.assertIsNone(self.store.load_pending_report_job())
        self.assertEqual(marker.call_args.args[0]["result_kind"], "research_blocked")
        self.assertFalse(marker.call_args.args[0]["email_sent"])

    async def test_actual_queue_blocks_forged_closed_summary_atomically(self):
        self.store.observe_origins([TechnicalEvidence(
            source="arxiv", evidence_type=EvidenceType.PRIMARY_PAPER,
            title="Unfinished origin", url="https://arxiv.org/abs/2610.00001",
        )])
        with self.assertRaises(ResearchNotCompleteError):
            self.store.enqueue_report([], dict(COMPLETED_RESEARCH), report_date="2026-10-08")
        self.assertIsNone(self.store.load_pending_report_job())
        self.assertEqual(len(self.store.load_pending_origins()), 1)

    async def test_pending_candidate_itself_blocks_even_without_durable_queue(self):
        candidate = ParadigmCandidate(
            key="pending", name="Pending", thesis="", problem_shift="", mechanism="",
            status="pending_deep",
        )
        with self.assertRaises(ResearchNotCompleteError):
            self.store.enqueue_report([candidate], dict(COMPLETED_RESEARCH), report_date="2026-10-08")
        generator = ParadigmReportGenerator(self.path, client=False)
        with self.assertRaises(ResearchNotCompleteError):
            await generator.generate([candidate], dict(COMPLETED_RESEARCH))
        self.assertEqual(list(self.path.glob("*.md")), [])

    async def test_source_revision_conflict_blocks_a_ready_snapshot(self):
        source = self.store.observe_origins([TechnicalEvidence(
            source="arxiv", evidence_type=EvidenceType.PRIMARY_PAPER,
            title="Revised paper", url="https://arxiv.org/abs/2610.00001",
        )])[0][0]
        self.store.mark_evidence([source], analyzed=True)
        candidate = ParadigmCandidate(
            key="revised", name="Revised", thesis="", problem_shift="", mechanism="",
            status="observe", evidence=[source],
        )
        self.store.save_candidates([candidate])
        changed = copy.deepcopy(source)
        changed.summary = "A different upstream revision"
        updated = self.store.observe_origins([changed])[0]
        self.store.mark_evidence(updated, analyzed=True)
        with self.assertRaises(ResearchNotCompleteError):
            self.store.enqueue_report([candidate], dict(COMPLETED_RESEARCH), report_date="2026-10-08")
        self.assertIsNone(self.store.load_pending_report_job())

    def old_partial_job(self, *, content="", stats=None):
        candidate = ParadigmCandidate(
            key="route", name="Route", thesis="假说", problem_shift="边界",
            mechanism="old input", status="observe",
        )
        self.store.save_candidates([candidate])
        job = self.store.enqueue_report([candidate], dict(COMPLETED_RESEARCH), report_date="2026-10-08")
        # Emulate a pre-contract cloud snapshot, not an accepted new enqueue.
        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute(
                "UPDATE report_outbox SET stats_json=?, report_content=?, status=? WHERE delivery_key=?",
                (json.dumps(stats or {**COMPLETED_RESEARCH, "pending_work_count": 3}), content,
                 "rendered" if content else "pending_render", job.delivery_key),
            )
        return self.store.get_report_job(job.delivery_key)

    async def test_recovered_partial_report_cannot_send_or_overwrite_newer_research(self):
        job = self.old_partial_job(content="# 本期运行状态 Memo")
        newer = job.candidates[0]
        newer.mechanism = "newer durable input"
        newer.status = "pending_deep"
        self.store.save_candidates([newer])
        original = self.store.load_candidate_snapshots({newer.key})[0].to_dict()
        generator = SimpleNamespace(output_dir=self.path, generate=AsyncMock())
        with (
            patch("notifications.email_notifier.send_report_email", AsyncMock()) as send,
            patch.object(run_audit, "write", return_value={}),
        ):
            result = await main._deliver_paradigm_job(self.store, generator, job, recovered=True)
        self.assertTrue(result["delivery_quarantined"])
        generator.generate.assert_not_awaited()
        send.assert_not_awaited()
        self.assertEqual(self.store.load_candidate_snapshots({newer.key})[0].to_dict(), original)
        self.assertIsNone(self.store.load_pending_report_job())
        self.assertEqual(self.store.stats()["deliveries"], 0)

    async def test_old_partial_content_is_rejected_even_with_closed_metadata(self):
        job = self.old_partial_job(content="> **阶段性研究范围：** 仍有待办", stats=dict(COMPLETED_RESEARCH))
        generator = SimpleNamespace(output_dir=self.path, generate=AsyncMock())
        with patch.object(run_audit, "write", return_value={}):
            result = await main._deliver_paradigm_job(self.store, generator, job, recovered=True)
        self.assertEqual(result["delivery_failure_kind"], "research_not_complete")
        self.assertFalse(result["email_sent"])

    async def test_state_migration_retires_partial_job_but_keeps_research(self):
        job = self.old_partial_job()
        prior = self.store.load_candidate_snapshots({"route"})[0].to_dict()
        migrate_state(self.store.db_path, config.PARADIGM_STATE_SCHEMA_VERSION)
        self.assertEqual(self.store.get_report_job(job.delivery_key).status, "quarantined")
        self.assertEqual(self.store.load_candidate_snapshots({"route"})[0].to_dict(), prior)

    async def test_partial_job_cannot_advance_delivery_signatures(self):
        job = self.old_partial_job()
        with self.assertRaises(ResearchNotCompleteError):
            self.store.mark_delivery_delivered(job.delivery_key, self.path / job.report_name)
        self.assertEqual(self.store.stats()["deliveries"], 0)
        self.assertEqual(len(self.store.prepare_report(job.candidates)), 1)

    async def test_smtp_boundary_rejects_partial_metadata_or_content(self):
        report = self.path / "paradigm_radar_2026-10-08.md"
        report.write_text("# 本期运行状态 Memo", encoding="utf-8")
        with patch("notifications.email_notifier.smtplib.SMTP_SSL") as smtp:
            with self.assertRaises(ResearchNotCompleteError):
                _send_sync(report, dict(COMPLETED_RESEARCH))
            with self.assertRaises(ResearchNotCompleteError):
                await send_report_email(report, {**COMPLETED_RESEARCH, "pending_work_count": 1})
            smtp.assert_not_called()

    async def test_report_command_uses_original_completed_snapshot_and_date(self):
        closed = self.store.enqueue_report([], dict(COMPLETED_RESEARCH), report_date="2026-09-30")
        self.store.mark_delivery_delivered(closed.delivery_key, self.path / closed.report_name)
        partial = self.old_partial_job()
        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute("UPDATE report_outbox SET status='delivered', delivered_at='2099' WHERE delivery_key=?", (partial.delivery_key,))
        report = self.path / closed.report_name
        report.write_text("# 完整历史结论", encoding="utf-8")
        generator = SimpleNamespace(generate=AsyncMock(return_value=report))
        with (
            patch.object(config, "PIPELINE_MODE", "paradigm"),
            patch("database.paradigm_store.ParadigmStore", return_value=self.store),
            patch("reports.paradigm_generator.ParadigmReportGenerator", return_value=generator),
            patch("notifications.email_notifier.send_report_email", AsyncMock(return_value=False)),
            patch.object(run_audit, "write", return_value={"audit_markdown_path": str(self.path / "audit.md")}),
        ):
            await main._regenerate_report_once()
        self.assertEqual(generator.generate.call_args.kwargs["report_date"], "2026-09-30")

    async def test_report_command_without_completed_history_cannot_invent_empty_result(self):
        generator = SimpleNamespace(generate=AsyncMock())
        with (
            patch.object(config, "PIPELINE_MODE", "paradigm"),
            patch("database.paradigm_store.ParadigmStore", return_value=self.store),
            patch("reports.paradigm_generator.ParadigmReportGenerator", return_value=generator),
            patch("notifications.email_notifier.send_report_email", AsyncMock()) as send,
        ):
            with self.assertRaisesRegex(RuntimeError, "没有已闭合"):
                await main._regenerate_report_once()
        generator.generate.assert_not_awaited()
        send.assert_not_awaited()

    async def test_report_command_refuses_legacy_partial_job(self):
        job = self.old_partial_job()
        generator = SimpleNamespace(output_dir=self.path, generate=AsyncMock())
        with (
            patch.object(config, "PIPELINE_MODE", "paradigm"),
            patch("database.paradigm_store.ParadigmStore", return_value=self.store),
            patch("reports.paradigm_generator.ParadigmReportGenerator", return_value=generator),
            patch("notifications.email_notifier.send_report_email", AsyncMock()) as send,
            patch.object(run_audit, "write", return_value={}),
        ):
            with self.assertRaisesRegex(RuntimeError, "请先完成正式研究"):
                await main._regenerate_report_once()
        self.assertEqual(self.store.get_report_job(job.delivery_key).status, "quarantined")
        generator.generate.assert_not_awaited()
        send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
