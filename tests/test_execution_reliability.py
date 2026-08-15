from __future__ import annotations

import asyncio
import configparser
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import config
import main as app_main
import setup_env
from agents.paradigm_orchestrator import (
    ParadigmOrchestrator,
    _delivery_primary_source_ready,
    _delivery_profile_ready,
    _commit_origin_analysis_checkpoint,
    _commit_landscape_checkpoint_if_complete,
    _execution_deadlines,
    _origin_analysis_priority,
    _origin_execution_order,
)
from database.paradigm_store import ParadigmStore
from database.state_migration import migrate_state
from healthcheck import (
    _email_check,
    _execution_budget_check,
    _model_check,
    _raw_environment_syntax_check,
    _schedule_check,
    _source_endpoint_syntax_check,
    blocking_checks,
)
from notifications.email_notifier import (
    _safe_public_text,
    _workflow_failure_context,
    send_failure_email,
)
from paradigms.models import (
    EvidenceType,
    ParadigmCandidate,
    ParadigmExtraction,
    ResearcherProfile,
    TechnicalEvidence,
)
from paradigms.discovery import ParadigmDiscovery
from reports.paradigm_generator import ParadigmReportGenerator
from run_audit import run_audit
from sources.research_feed_source import ResearchFeedSource
from sources.follow_builders_source import FollowBuildersSource


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
    def test_incomplete_recall_cannot_advance_landscape_baseline(self) -> None:
        store = Mock()
        advanced = _commit_landscape_checkpoint_if_complete(
            store,
            landscape_coverage_incomplete=True,
        )
        self.assertFalse(advanced)
        store.mark_landscape_version.assert_not_called()

        advanced = _commit_landscape_checkpoint_if_complete(
            store,
            landscape_coverage_incomplete=False,
        )
        self.assertTrue(advanced)
        store.mark_landscape_version.assert_called_once_with()

    def test_setup_writes_private_env_file_with_current_time_defaults(self) -> None:
        defaults = {
            key: default
            for _, items in setup_env.SECTIONS
            for key, _, _, default, _ in items
        }
        self.assertEqual(defaults["PARADIGM_RECALL_OVERLAP_DAYS"], "30")
        self.assertEqual(defaults["SCHEDULE_MINUTE"], "15")
        self.assertEqual(
            defaults["PARADIGM_DISCOVERY_SOURCE_TIMEOUT_SECONDS"], "600"
        )
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            with patch.object(setup_env, "ENV_PATH", env_path):
                setup_env._write_env({})
            content = env_path.read_text(encoding="utf-8")
            self.assertIn("PARADIGM_DISCOVERY_SOURCE_TIMEOUT_SECONDS=", content)
            self.assertIn("SCHEDULE_MINUTE=", content)
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)

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
            self.assertIn("report_outbox", tables)
            self.assertIn("report_render_fragments", tables)
            with sqlite3.connect(database) as connection:
                delivery_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(report_deliveries)"
                    )
                }
            self.assertIn("delivery_key", delivery_columns)

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

        extractions, attempted, failed, deferred, _, completed = result
        self.assertEqual(len(extractions), 1)
        self.assertEqual(attempted, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(deferred, [second])
        self.assertEqual(completed, [first])
        orchestrator.store.mark_evidence.assert_not_called()

    def test_unexpected_origin_batch_failure_isolated_and_kept_pending(
        self,
    ) -> None:
        first, second = _origin("2608.00011"), _origin("2608.00012")
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
            run=AsyncMock(
                side_effect=[
                    RuntimeError("one malformed upstream response"),
                    [_rejected_extraction(second)],
                ]
            )
        )
        orchestrator.store = SimpleNamespace(mark_evidence=Mock())

        with (
            patch.object(config, "PARADIGM_ANALYSIS_BATCH_SIZE", 1),
            patch(
                "agents.paradigm_orchestrator._remaining_seconds",
                side_effect=[5.0, 5.0],
            ),
        ):
            result = asyncio.run(
                orchestrator._analyze_origins_in_batches(
                    [first, second], deadline=123.0
                )
            )

        extractions, attempted, failed, deferred, _, completed = result
        self.assertEqual([item.evidence for item in extractions], [second])
        self.assertEqual(attempted, 2)
        self.assertEqual(failed, 1)
        self.assertEqual(deferred, [])
        self.assertEqual(completed, [second])
        self.assertEqual(first.raw["analysis_failure_count"], 1)
        orchestrator.store.mark_evidence.assert_called_once_with(
            [first], analyzed=False
        )

    def test_unexpected_deep_failure_does_not_abort_peer_candidate(self) -> None:
        first = ParadigmCandidate(
            key="broken-route",
            name="Broken route",
            thesis="test",
            problem_shift="test",
            mechanism="test",
            evidence=[_origin("2608.00013")],
        )
        second = ParadigmCandidate(
            key="working-route",
            name="Working route",
            thesis="test",
            problem_shift="test",
            mechanism="test",
            evidence=[_origin("2608.00014")],
        )
        second.execution_failure_count = 2
        second.last_execution_failure_at = "2026-08-14T00:00:00Z"
        orchestrator = object.__new__(ParadigmOrchestrator)

        async def enrich(values, _supporting):
            if values[0].key == first.key:
                raise RuntimeError("profile endpoint broke")
            return values

        orchestrator.enricher = SimpleNamespace(run=enrich)
        orchestrator.synthesizer = SimpleNamespace(
            run=AsyncMock(side_effect=lambda values: values)
        )
        orchestrator.trajectory = SimpleNamespace(
            run=AsyncMock(side_effect=lambda values: values)
        )

        with (
            patch.object(config, "PARADIGM_DEEP_BATCH_SIZE", 1),
            patch(
                "agents.paradigm_orchestrator._remaining_seconds",
                side_effect=[5.0, 5.0],
            ),
        ):
            completed, budget_deferred, execution_deferred = asyncio.run(
                orchestrator._deep_analyze_in_batches(
                    [first, second], [], deadline=123.0
                )
            )

        self.assertEqual([item.key for item in completed], [second.key])
        self.assertEqual(budget_deferred, [])
        self.assertEqual(execution_deferred, [first])
        self.assertEqual(first.status, "pending_deep")
        self.assertEqual(first.execution_failure_count, 1)
        self.assertTrue(first.last_execution_failure_at)
        self.assertEqual(completed[0].execution_failure_count, 0)
        self.assertEqual(completed[0].last_execution_failure_at, "")

    def test_refresh_failure_keeps_original_snapshot_and_continues(self) -> None:
        first = ParadigmCandidate(
            key="broken-refresh",
            name="Broken refresh",
            thesis="original",
            problem_shift="test",
            mechanism="test",
            evidence=[_origin("2608.00015")],
        )
        second = ParadigmCandidate(
            key="working-refresh",
            name="Working refresh",
            thesis="original",
            problem_shift="test",
            mechanism="test",
            evidence=[_origin("2608.00016")],
        )
        second.execution_failure_count = 2
        second.last_execution_failure_at = "2026-08-14T00:00:00Z"
        orchestrator = object.__new__(ParadigmOrchestrator)

        async def refresh(values, _supporting):
            values[0].thesis = "mutated-copy"
            if values[0].key == first.key:
                raise RuntimeError("community endpoint broke")
            return values

        orchestrator.enricher = SimpleNamespace(refresh=refresh)
        orchestrator.synthesizer = SimpleNamespace(
            run=AsyncMock(side_effect=lambda values: values)
        )

        with (
            patch.object(config, "PARADIGM_DEEP_BATCH_SIZE", 1),
            patch(
                "agents.paradigm_orchestrator._remaining_seconds",
                side_effect=[5.0, 5.0],
            ),
        ):
            refreshed, budget_deferred, execution_deferred, attempted = (
                asyncio.run(
                    orchestrator._refresh_in_batches(
                        [first, second], [], deadline=123.0
                    )
                )
            )

        self.assertEqual(first.thesis, "original")
        self.assertEqual(refreshed[0].key, second.key)
        self.assertEqual(refreshed[0].thesis, "mutated-copy")
        self.assertEqual(budget_deferred, [])
        self.assertEqual(execution_deferred, [first])
        self.assertEqual(attempted, 2)
        self.assertEqual(first.execution_failure_count, 1)
        self.assertEqual(refreshed[0].execution_failure_count, 0)
        self.assertEqual(refreshed[0].last_execution_failure_at, "")

    def test_origin_is_not_marked_analyzed_before_candidate_checkpoint(self) -> None:
        store = SimpleNamespace(
            save_candidates=Mock(side_effect=RuntimeError("disk full")),
            mark_evidence=Mock(),
        )
        candidate = ParadigmCandidate(
            key="checkpoint-route",
            name="Checkpoint route",
            thesis="能力边界变化",
            problem_shift="问题发生变化",
            mechanism="新的机制",
            evidence=[_origin("2608.00010")],
        )

        with self.assertRaisesRegex(RuntimeError, "disk full"):
            _commit_origin_analysis_checkpoint(
                store,
                [candidate],
                candidate.evidence,
            )

        self.assertEqual(candidate.status, "pending_deep")
        store.mark_evidence.assert_not_called()

        store.save_candidates.side_effect = None
        _commit_origin_analysis_checkpoint(
            store,
            [candidate],
            candidate.evidence,
        )
        store.mark_evidence.assert_called_once_with(
            candidate.evidence,
            analyzed=True,
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

    def test_refresh_failure_metadata_survives_candidate_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            candidate = ParadigmCandidate(
                key="refresh-retry-route",
                name="Refresh retry route",
                thesis="原始判断",
                problem_shift="问题边界",
                mechanism="机制",
                evidence=[_origin("2608.00017")],
                status="watch",
                execution_failure_count=2,
                last_execution_failure_at="2026-08-15T00:00:00Z",
            )

            store.save_candidates([candidate])
            restored = store.load_refresh_candidates(limit=0)

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].execution_failure_count, 2)
        self.assertEqual(
            restored[0].last_execution_failure_at,
            "2026-08-15T00:00:00Z",
        )

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

    def test_new_revision_or_linked_report_reopens_unchanged_abstract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            original = _origin("2608.00009")
            original.summary = "unchanged abstract"
            original.raw["updated_at"] = "2026-08-01T00:00:00Z"
            store.mark_evidence([original], analyzed=True)

            revised = _origin("2608.00009")
            revised.summary = original.summary
            revised.raw.update(
                {
                    "updated_at": "2026-08-14T00:00:00Z",
                    "linked_research_documents": [
                        {
                            "title": "Full technical report",
                            "url": "https://lab.example/report.pdf",
                        }
                    ],
                }
            )
            planned, stats = store.plan_origins([revised])

        self.assertEqual(planned, [revised])
        self.assertEqual(stats["changed"], 1)

    def test_report_outbox_preserves_research_and_commits_delivery_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            evidence = _origin("2608.00005")
            candidate = ParadigmCandidate(
                key="durable-route",
                name="Durable route",
                thesis="改变能力边界",
                problem_shift="从旧问题转向新问题",
                mechanism="新的训练接口",
                evidence=[evidence],
            )
            store.mark_evidence([evidence], analyzed=True)
            store.save_candidates([candidate])
            job = store.enqueue_report(
                [candidate],
                {"origin_count": 1, "new_paradigms": 1},
                report_date="2026-08-09",
            )

            store.record_delivery_failure(
                job.delivery_key, "renderer failed", rendered=False
            )
            store.save_report_fragment(
                job.delivery_key,
                "route:durable-route:signature",
                "### 已验证路线草稿",
            )
            pending = store.load_pending_report_job()
            self.assertIsNotNone(pending)
            self.assertEqual(pending.candidates[0].key, candidate.key)
            self.assertEqual(store.load_pending_origins(), [])
            self.assertEqual(
                len(store.load_report_fragments(job.delivery_key)), 1
            )

            store.save_rendered_report(job.delivery_key, "# validated report")
            store.begin_delivery_attempt(job.delivery_key)
            store.mark_delivery_delivered(
                job.delivery_key, Path("reports/output/report.md")
            )

            self.assertIsNone(store.load_pending_report_job())
            with sqlite3.connect(store.db_path) as connection:
                reported = connection.execute(
                    "SELECT last_reported_signature FROM paradigms "
                    "WHERE paradigm_key=?",
                    (candidate.key,),
                ).fetchone()[0]
                outbox_status = connection.execute(
                    "SELECT status FROM report_outbox WHERE delivery_key=?",
                    (job.delivery_key,),
                ).fetchone()[0]
                report_content = connection.execute(
                    "SELECT report_content FROM report_outbox WHERE delivery_key=?",
                    (job.delivery_key,),
                ).fetchone()[0]
            self.assertEqual(reported, candidate.report_signature)
            self.assertEqual(outbox_status, "delivered")
            self.assertEqual(report_content, "")
            self.assertEqual(store.load_report_fragments(job.delivery_key), {})

    def test_report_timeout_resumes_from_route_fragment_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            candidate = ParadigmCandidate(
                key="route-checkpoint",
                name="Route checkpoint",
                thesis="改变能力边界",
                problem_shift="从旧问题转向新问题",
                mechanism="新的训练接口",
                evidence=[_origin("2608.00009")],
            )
            store.save_candidates([candidate])
            job = store.enqueue_report(
                [candidate], {"new_paradigms": 1}, report_date="2026-08-09"
            )
            report_path = Path(directory) / job.report_name

            async def fail_after_fragment(*_args, **kwargs):
                kwargs["save_route_fragment"](
                    "route:route-checkpoint:abc",
                    "### 已完成且通过闸门的路线草稿",
                )
                try:
                    raise TimeoutError("editorial frame timed out")
                except TimeoutError as exc:
                    raise RuntimeError("report rendering failed") from exc

            failing_generator = SimpleNamespace(
                output_dir=Path(directory),
                generate=AsyncMock(side_effect=fail_after_fragment),
            )
            with (
                patch.object(config, "EMAIL_PUSH_ENABLED", False),
                patch.object(config, "PARADIGM_REPORT_TIMEOUT_SECONDS", 30),
            ):
                with self.assertRaisesRegex(RuntimeError, "report rendering"):
                    asyncio.run(
                        app_main._deliver_paradigm_job(
                            store, failing_generator, job, recovered=False
                        )
                    )

            pending = store.load_pending_report_job()
            self.assertIsNotNone(pending)
            self.assertIn("RuntimeError: report rendering failed", pending.last_error)
            self.assertIn("TimeoutError: editorial frame timed out", pending.last_error)
            self.assertEqual(
                store.load_report_fragments(job.delivery_key),
                {
                    "route:route-checkpoint:abc": (
                        "### 已完成且通过闸门的路线草稿"
                    )
                },
            )

            async def resume_from_fragment(*_args, **kwargs):
                self.assertIn(
                    "route:route-checkpoint:abc", kwargs["route_fragments"]
                )
                report_path.write_text("# resumed report", encoding="utf-8")
                return report_path

            recovered_generator = SimpleNamespace(
                output_dir=Path(directory),
                generate=AsyncMock(side_effect=resume_from_fragment),
            )
            audit_result = {
                "audit_markdown_path": str(Path(directory) / "audit.md"),
                "audit_json_path": str(Path(directory) / "audit.json"),
            }
            with (
                patch.object(config, "EMAIL_PUSH_ENABLED", False),
                patch.object(run_audit, "write", return_value=audit_result),
            ):
                asyncio.run(
                    app_main._deliver_paradigm_job(
                        store, recovered_generator, pending, recovered=True
                    )
                )

            self.assertIsNone(store.load_pending_report_job())
            self.assertEqual(store.load_report_fragments(job.delivery_key), {})

    def test_recovered_outbox_delivery_does_not_start_fresh_research(self) -> None:
        pending = SimpleNamespace(
            delivery_key="a" * 64,
            status="pending_render",
        )
        store = SimpleNamespace(load_pending_report_job=Mock(return_value=pending))
        orchestrator = SimpleNamespace(store=store, run=AsyncMock())
        generator = SimpleNamespace()
        delivery = AsyncMock(return_value={"email_sent": True})

        with (
            patch.object(config, "PIPELINE_MODE", "paradigm"),
            patch("main._check_env"),
            patch("main._print_model_banner"),
            patch(
                "agents.paradigm_orchestrator.ParadigmOrchestrator",
                return_value=orchestrator,
            ),
            patch(
                "reports.paradigm_generator.ParadigmReportGenerator",
                return_value=generator,
            ),
            patch("main._deliver_paradigm_job", new=delivery),
        ):
            result = asyncio.run(app_main._run_pipeline_once())

        self.assertTrue(result["recovered_delivery_only"])
        delivery.assert_awaited_once_with(
            store, generator, pending, recovered=True
        )
        orchestrator.run.assert_not_awaited()

    def test_email_failure_reuses_validated_report_without_rerunning_research(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            candidate = ParadigmCandidate(
                key="retry-route",
                name="Retry route",
                thesis="改变能力边界",
                problem_shift="从旧问题转向新问题",
                mechanism="新的训练接口",
                evidence=[_origin("2608.00006")],
            )
            store.save_candidates([candidate])
            job = store.enqueue_report(
                [candidate], {"new_paradigms": 1}, report_date="2026-08-09"
            )
            report_path = Path(directory) / job.report_name

            async def render(*_args, **_kwargs):
                report_path.write_text("# validated report", encoding="utf-8")
                return report_path

            generator = SimpleNamespace(
                output_dir=Path(directory), generate=AsyncMock(side_effect=render)
            )
            audit_result = {
                "audit_markdown_path": str(Path(directory) / "audit.md"),
                "audit_json_path": str(Path(directory) / "audit.json"),
            }
            with (
                patch.object(config, "EMAIL_PUSH_ENABLED", True),
                patch.object(config, "PARADIGM_REPORT_TIMEOUT_SECONDS", 30),
                patch.object(run_audit, "write", return_value=audit_result),
                patch(
                    "notifications.email_notifier.send_report_email",
                    new=AsyncMock(side_effect=RuntimeError("smtp unavailable")),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "smtp unavailable"):
                    asyncio.run(
                        app_main._deliver_paradigm_job(
                            store, generator, job, recovered=False
                        )
                    )

            pending = store.load_pending_report_job()
            self.assertIsNotNone(pending)
            self.assertEqual(pending.status, "rendered")
            self.assertEqual(pending.report_content, "# validated report")
            self.assertEqual(generator.generate.await_count, 1)

            with (
                patch.object(config, "EMAIL_PUSH_ENABLED", True),
                patch.object(run_audit, "write", return_value=audit_result),
                patch(
                    "notifications.email_notifier.send_report_email",
                    new=AsyncMock(return_value=True),
                ) as sender,
            ):
                asyncio.run(
                    app_main._deliver_paradigm_job(
                        store, generator, pending, recovered=True
                    )
                )

            self.assertEqual(generator.generate.await_count, 1)
            sender.assert_awaited_once()
            self.assertIsNone(store.load_pending_report_job())

    def test_empty_outbox_key_changes_when_coverage_materially_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            first = store.enqueue_report(
                [],
                {"origin_count": 0, "run_incomplete": True, "pending_work_count": 5},
                report_date="2026-08-09",
            )
            duplicate = store.enqueue_report(
                [],
                {"origin_count": 0, "run_incomplete": True, "pending_work_count": 5},
                report_date="2026-08-09",
            )
            completed = store.enqueue_report(
                [],
                {"origin_count": 10, "run_incomplete": False, "pending_work_count": 0},
                report_date="2026-08-09",
            )

        self.assertEqual(first.delivery_key, duplicate.delivery_key)
        self.assertNotEqual(first.delivery_key, completed.delivery_key)

    def test_identity_seed_alone_does_not_satisfy_person_delivery_contract(self) -> None:
        item = ParadigmCandidate(
            key="person-contract",
            name="Person contract",
            thesis="改变能力边界",
            problem_shift="新的研究问题",
            mechanism="新的训练接口",
            researchers=[
                ResearcherProfile(
                    name="A. Researcher",
                    current_affiliation="Example Lab",
                    contact_search_notes=["已从当前论文作者列表建立身份种子"],
                )
            ],
        )
        self.assertFalse(_delivery_profile_ready(item))
        item.researchers[0].contact_search_notes.append(
            "已检索 OpenAlex Authors 并用当前论文题目核验身份"
        )
        self.assertTrue(_delivery_profile_ready(item))

    def test_non_key_coauthor_does_not_block_person_delivery_contract(self) -> None:
        item = ParadigmCandidate(
            key="key-person-contract",
            name="Key person contract",
            thesis="改变能力边界",
            problem_shift="新的研究问题",
            mechanism="新的训练接口",
            researchers=[
                ResearcherProfile(
                    name="Lead Researcher",
                    role="第一作者",
                    current_affiliation="Example Lab",
                    contact_search_notes=["已检索 OpenAlex Authors 并核验身份"],
                ),
                ResearcherProfile(
                    name="Contributing Researcher",
                    role="共同作者",
                ),
                ResearcherProfile(
                    name="Senior Researcher",
                    role="末位作者/资深作者线索",
                    current_affiliation="Example University",
                    contact_search_notes=["已检索 OpenAlex Authors 并核验身份"],
                ),
            ],
        )
        self.assertTrue(_delivery_profile_ready(item))

    def test_verified_team_report_can_use_organization_attribution(self) -> None:
        item = ParadigmCandidate(
            key="team-report",
            name="Team technical report",
            thesis="改变系统能力边界",
            problem_shift="新的系统问题",
            mechanism="新的系统机制",
            publisher_tier="established",
            is_formal_technical_report=True,
            evidence=[
                TechnicalEvidence(
                    source="arxiv",
                    evidence_type=EvidenceType.PRIMARY_PAPER,
                    title="Team technical report",
                    url="https://arxiv.org/abs/2608.00008",
                    authors=["Example Research Team"],
                    organization="Example Research Lab",
                )
            ],
        )
        self.assertTrue(_delivery_profile_ready(item))

        item.publisher_tier = "unknown"
        self.assertFalse(_delivery_profile_ready(item))

    def test_primary_source_delivery_contract_rejects_local_or_missing_links(self) -> None:
        item = ParadigmCandidate(
            key="source-contract",
            name="Source contract",
            thesis="改变能力边界",
            problem_shift="新的研究问题",
            mechanism="新的训练接口",
            evidence=[
                TechnicalEvidence(
                    source="paper",
                    evidence_type=EvidenceType.PRIMARY_PAPER,
                    title="Unsafe source",
                    url="http://127.0.0.1/private",
                )
            ],
        )
        self.assertFalse(_delivery_primary_source_ready(item))
        item.evidence[0].url = "https://api.openalex.org/works/W123"
        self.assertFalse(_delivery_primary_source_ready(item))
        item.evidence[0].url = "https://arxiv.org/abs/2608.00007"
        self.assertTrue(_delivery_primary_source_ready(item))

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
            run, origin, deep, reserve = _execution_deadlines(100.0, 1600.0)
        self.assertEqual((run, origin, deep, reserve), (4000.0, 3400.0, 4000.0, 600))

    def test_doctor_checks_combined_research_and_delivery_budget(self) -> None:
        with (
            patch.object(config, "PARADIGM_RUN_BUDGET_SECONDS", 3600),
            patch.object(config, "PARADIGM_REPORT_TIMEOUT_SECONDS", 1200),
            patch.object(
                config, "PARADIGM_REPORT_REQUEST_TIMEOUT_SECONDS", 360
            ),
            patch.object(config, "PARADIGM_REPORT_ROUTE_CONCURRENCY", 2),
        ):
            self.assertEqual(_execution_budget_check().status, "ready")
        with (
            patch.object(config, "PARADIGM_RUN_BUDGET_SECONDS", 3900),
            patch.object(config, "PARADIGM_REPORT_TIMEOUT_SECONDS", 1200),
            patch.object(
                config, "PARADIGM_REPORT_REQUEST_TIMEOUT_SECONDS", 360
            ),
        ):
            unsafe = _execution_budget_check()
            self.assertEqual(unsafe.status, "missing")
            self.assertEqual(blocking_checks([unsafe]), [unsafe])

    def test_doctor_rejects_disabled_soft_budget_in_github_actions(self) -> None:
        with (
            patch.object(config, "PARADIGM_RUN_BUDGET_SECONDS", 0),
            patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}, clear=False),
        ):
            check = _execution_budget_check()
        self.assertEqual(check.status, "missing")
        self.assertIn("不允许禁用软预算", check.note)

        with (
            patch.object(config, "PARADIGM_RUN_BUDGET_SECONDS", 0),
            patch.dict("os.environ", {"GITHUB_ACTIONS": ""}, clear=False),
        ):
            self.assertEqual(_execution_budget_check().status, "warning")

    def test_doctor_blocks_model_outside_qwen37_series(self) -> None:
        resolved = SimpleNamespace(
            provider="dashscope",
            model="qwen3-max",
            api_key="configured",
        )
        check = _model_check("fixture", resolved)
        self.assertEqual(check.status, "missing")
        self.assertEqual(blocking_checks([check]), [check])

    def test_doctor_blocks_invalid_schedule_and_smtp_modes(self) -> None:
        with patch.object(config, "SCHEDULE_HOUR", 27):
            self.assertEqual(_schedule_check().status, "missing")
        with (
            patch.object(config, "EMAIL_PUSH_ENABLED", True),
            patch.object(config, "SMTP_USE_SSL", True),
            patch.object(config, "SMTP_USE_STARTTLS", True),
        ):
            self.assertEqual(_email_check().status, "missing")

    def test_doctor_rejects_malformed_variables_instead_of_defaulting(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "PARADIGM_RUN_BUDGET_SECONDS": "three-thousand",
                "SMTP_USE_SSL": "flase",
                "PARADIGM_REPORT_ROUTE_CONCURRENCY": "99",
                "RESEARCH_WATCHLIST_MODE": "overwrite",
            },
            clear=False,
        ):
            check = _raw_environment_syntax_check()
        self.assertEqual(check.status, "missing")
        self.assertIn("PARADIGM_RUN_BUDGET_SECONDS", check.note)
        self.assertIn("SMTP_USE_SSL", check.note)
        self.assertIn("PARADIGM_REPORT_ROUTE_CONCURRENCY", check.note)
        self.assertIn("RESEARCH_WATCHLIST_MODE", check.note)

    def test_doctor_rejects_malformed_source_endpoints(self) -> None:
        with (
            patch.object(config, "PRIORITY_RESEARCH_PAGES", ["not-a-url"]),
            patch.object(
                config,
                "RESEARCH_FEED_URLS",
                ["https://valid.example/feed"],
            ),
            patch.object(config, "FOLLOW_BUILDERS_ENABLED", True),
            patch.object(
                config,
                "FOLLOW_BUILDERS_FEED_URL",
                "https://feeds.example/base",
            ),
            patch.object(
                config,
                "OPENREVIEW_VENUES",
                ["ICLR.cc/2026/Conference"],
            ),
        ):
            check = _source_endpoint_syntax_check()

        self.assertEqual(check.status, "missing")
        self.assertIn("PRIORITY_RESEARCH_PAGES", check.note)

    def test_doctor_accepts_local_follow_builders_and_http_sources(self) -> None:
        with (
            patch.object(
                config,
                "PRIORITY_RESEARCH_PAGES",
                ["https://lab.example/research"],
            ),
            patch.object(
                config,
                "RESEARCH_FEED_URLS",
                ["https://lab.example/feed.xml"],
            ),
            patch.object(config, "FOLLOW_BUILDERS_ENABLED", True),
            patch.object(
                config,
                "FOLLOW_BUILDERS_FEED_URL",
                "file:///tmp/feeds",
            ),
            patch.object(
                config,
                "OPENREVIEW_VENUES",
                ["ICLR.cc/2026/Conference"],
            ),
        ):
            check = _source_endpoint_syntax_check()

        self.assertEqual(check.status, "ready")

    def test_slow_discovery_cannot_consume_the_entire_origin_stage(self) -> None:
        with (
            patch.object(config, "PARADIGM_RUN_BUDGET_SECONDS", 3900),
            patch.object(config, "PARADIGM_STAGE_RESERVE_SECONDS", 1200),
        ):
            run, origin, deep, reserve = _execution_deadlines(100.0, 2500.0)
        self.assertEqual(run, deep)
        self.assertEqual(reserve, 750)
        self.assertGreater(origin, 2500.0)
        self.assertLess(origin, deep)

    def test_stalled_discovery_source_is_visible_instead_of_zero_hits(self) -> None:
        class StalledSource:
            source_name = "stalled-fixture"

            async def safe_fetch(self):
                await asyncio.sleep(1)
                return [object()]

        discovery = ParadigmDiscovery(lookback_days=7)
        discovery.source_timeout_seconds = 0.01
        results, health = asyncio.run(
            discovery._bounded_fetch(StalledSource())
        )

        self.assertEqual(results, [])
        self.assertEqual(health["status"], "timed_out")
        self.assertEqual(health["error_type"], "TimeoutError")

    def test_unprotected_discovery_exception_is_isolated(self) -> None:
        class BrokenSource:
            source_name = "broken-fixture"

            async def safe_fetch(self):
                raise RuntimeError("fixture failure")

        discovery = ParadigmDiscovery(lookback_days=7)
        results, health = asyncio.run(discovery._bounded_fetch(BrokenSource()))

        self.assertEqual(results, [])
        self.assertEqual(health["status"], "query_failed")
        self.assertEqual(health["error_type"], "RuntimeError")

    def test_feed_internal_failures_cannot_masquerade_as_completed(self) -> None:
        source = ResearchFeedSource(lookback_days=7)
        source.completed_feeds = 1
        source.failed_feeds = 1
        with (
            patch.object(config, "RESEARCH_FEED_URLS", ["a", "b"]),
            patch.object(source, "fetch", AsyncMock(return_value=[])),
        ):
            self.assertEqual(asyncio.run(source.safe_fetch()), [])
        self.assertEqual(source.fetch_status, "partial")
        self.assertEqual(source.fetch_error, "PartialFeedFailure")

        source.completed_feeds = 0
        source.failed_feeds = 2
        with (
            patch.object(config, "RESEARCH_FEED_URLS", ["a", "b"]),
            patch.object(source, "fetch", AsyncMock(return_value=[])),
        ):
            self.assertEqual(asyncio.run(source.safe_fetch()), [])
        self.assertEqual(source.fetch_status, "query_failed")
        self.assertEqual(source.fetch_error, "AllFeedsFailed")

    def test_follow_builders_partial_failure_keeps_results_and_health(self) -> None:
        source = FollowBuildersSource()
        feed = {
            "x": [
                {
                    "name": "Builder",
                    "handle": "builder",
                    "tweets": [{"text": "new training method"}],
                }
            ]
        }
        with (
            patch.object(config, "FOLLOW_BUILDERS_ENABLED", True),
            patch.object(
                source,
                "_fetch_json",
                AsyncMock(side_effect=[feed, OSError("temporary"), None]),
            ),
        ):
            results = asyncio.run(source.safe_fetch())

        self.assertEqual(len(results), 1)
        self.assertEqual(source.fetch_status, "partial")
        self.assertEqual(source.fetch_error, "PartialFeedFailure")

    def test_follow_builders_all_failures_are_not_zero_hits(self) -> None:
        source = FollowBuildersSource()
        with (
            patch.object(config, "FOLLOW_BUILDERS_ENABLED", True),
            patch.object(
                source,
                "_fetch_json",
                AsyncMock(side_effect=OSError("unavailable")),
            ),
        ):
            self.assertEqual(asyncio.run(source.safe_fetch()), [])

        self.assertEqual(source.fetch_status, "query_failed")
        self.assertEqual(source.fetch_error, "AllFeedsFailed")

    def test_follow_builders_all_missing_feeds_are_not_successful_zero_hits(
        self,
    ) -> None:
        source = FollowBuildersSource()
        with (
            patch.object(config, "FOLLOW_BUILDERS_ENABLED", True),
            patch.object(
                source,
                "_fetch_json",
                AsyncMock(return_value=None),
            ),
        ):
            self.assertEqual(asyncio.run(source.safe_fetch()), [])

        self.assertEqual(source.fetch_status, "query_failed")
        self.assertEqual(source.fetch_error, "AllFeedsMissing")
        self.assertEqual(source.missing_feeds, 3)

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

    def test_empty_report_discloses_timed_out_discovery_source(self) -> None:
        content = ParadigmReportGenerator._empty_report(
            "2026-08-15",
            {
                "origin_count": 0,
                "recall_coverage_incomplete": True,
                "frontier_coverage": {
                    "source_health": {
                        "huggingface-papers": {
                            "status": "timed_out",
                            "results": 0,
                        }
                    }
                },
            },
        )
        self.assertIn("发现源：huggingface-papers=timed_out", content)
        self.assertIn("召回覆盖未闭合", content)

    def test_empty_report_discloses_partially_failed_discovery_source(self) -> None:
        content = ParadigmReportGenerator._empty_report(
            "2026-08-15",
            {
                "origin_count": 0,
                "recall_coverage_incomplete": True,
                "frontier_coverage": {
                    "source_health": {
                        "research-blog": {
                            "status": "partial",
                            "results": 3,
                        }
                    }
                },
            },
        )
        self.assertIn("发现源：research-blog=partial", content)
        self.assertIn("召回覆盖未闭合", content)

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

    def test_workflow_failure_context_identifies_preflight_test_failure(self) -> None:
        outcomes = {
            "DEPENDENCIES_STEP_OUTCOME": "success",
            "OFFLINE_CHECKS_STEP_OUTCOME": "failure",
            "RESTORE_STATE_STEP_OUTCOME": "skipped",
            "DOCTOR_STEP_OUTCOME": "skipped",
            "SMOKE_STEP_OUTCOME": "skipped",
            "PIPELINE_STEP_OUTCOME": "skipped",
            "PREPARE_STATE_STEP_OUTCOME": "success",
            "UPLOAD_STATE_STEP_OUTCOME": "skipped",
            "UPLOAD_REPORT_STEP_OUTCOME": "skipped",
            "UPLOAD_AUDIT_STEP_OUTCOME": "success",
        }
        with (
            patch.dict("os.environ", outcomes, clear=False),
            patch(
                "notifications.email_notifier._workflow_log_detail",
                return_value="offline contract failed",
            ),
        ):
            context = _workflow_failure_context()

        self.assertEqual(context["failure_stage"], "离线回归")
        self.assertEqual(context["failure_class"], "preflight")
        self.assertEqual(context["failure_detail"], "offline contract failed")
        self.assertIn("离线回归=failure", context["workflow_step_summary"])
        self.assertNotIn("研究与邮件主流程", context["workflow_step_summary"])

    def test_failure_log_summary_redacts_generic_url_credentials(self) -> None:
        sanitized = _safe_public_text(
            "https://build-user:private-pass@packages.example/simple"
            "?token=secret-value"
        )
        self.assertNotIn("private-pass", sanitized)
        self.assertNotIn("secret-value", sanitized)
        self.assertIn("https://***@packages.example", sanitized)

    def test_workflow_has_migration_soft_budget_and_failure_alert(self) -> None:
        workflow = Path(".github/workflows/weekly-radar.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("database.state_migration", workflow)
        self.assertIn("PARADIGM_RUN_BUDGET_SECONDS", workflow)
        self.assertIn("PARADIGM_KEY_RESEARCHER_LIMIT", workflow)
        self.assertIn("PARADIGM_REPORT_REQUEST_TIMEOUT_SECONDS", workflow)
        self.assertIn("PARADIGM_REPORT_ROUTE_CONCURRENCY", workflow)
        self.assertIn("运行离线回归测试", workflow)
        self.assertIn("python scripts/offline_checks.py", workflow)
        self.assertIn("logs/offline_checks.json", workflow)
        self.assertIn("--notify-failure", workflow)
        self.assertIn("always() && failure()", workflow)
        self.assertIn("拒绝静默从空状态启动", workflow)
        self.assertIn("steps.prepare_state.outputs.available == 'true'", workflow)
        self.assertIn("OFFLINE_CHECKS_STEP_OUTCOME", workflow)
        self.assertIn("vars.RESEARCH_WATCHLIST_MODE || 'merge'", workflow)
        self.assertIn("vars.REDDIT_API_ACCESS_APPROVED || 'false'", workflow)
        self.assertIn("vars.PRIORITY_RESEARCH_CONCURRENCY || '6'", workflow)
        self.assertNotIn("overwrite: true", workflow)
        self.assertIn(
            "name: paradigm-radar-state-${{ github.run_id }}",
            workflow,
        )
        self.assertIn('startswith("paradigm-radar-state-")', workflow)
        self.assertIn("gh api --paginate", workflow)
        self.assertIn("submodules: false", workflow)
        self.assertIn("尝试上一份快照", workflow)
        self.assertIn("所有未过期状态快照均不可用", workflow)
        self.assertNotIn('if [ "$state_schema" !=', workflow)

    def test_gitmodules_have_complete_unique_records(self) -> None:
        parser = configparser.ConfigParser()
        loaded = parser.read(".gitmodules", encoding="utf-8")
        self.assertEqual(loaded, [".gitmodules"])
        paths = []
        for section in parser.sections():
            self.assertTrue(section.startswith("submodule "))
            path = parser.get(section, "path", fallback="").strip()
            url = parser.get(section, "url", fallback="").strip()
            self.assertTrue(path, f"{section} 缺少 path")
            self.assertRegex(url, r"^https://github\.com/[^/]+/[^/]+(?:\.git)?$")
            paths.append(path)
        self.assertEqual(len(paths), len(set(paths)))


if __name__ == "__main__":
    unittest.main()
