from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import config
import main as app_main
from agents.paradigm_orchestrator import (
    ParadigmOrchestrator,
    _delivery_primary_source_ready,
    _delivery_profile_ready,
    _execution_deadlines,
    _origin_analysis_priority,
    _origin_execution_order,
)
from database.paradigm_store import ParadigmStore
from database.state_migration import migrate_state
from notifications.email_notifier import send_failure_email
from paradigms.models import (
    EvidenceType,
    ParadigmCandidate,
    ParadigmExtraction,
    ResearcherProfile,
    TechnicalEvidence,
)
from reports.paradigm_generator import ParadigmReportGenerator
from run_audit import run_audit


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
            self.assertIn("report_outbox", tables)
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
            pending = store.load_pending_report_job()
            self.assertIsNotNone(pending)
            self.assertEqual(pending.candidates[0].key, candidate.key)
            self.assertEqual(store.load_pending_origins(), [])

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
        self.assertIn("PARADIGM_KEY_RESEARCHER_LIMIT", workflow)
        self.assertIn("运行离线回归测试", workflow)
        self.assertIn("--notify-failure", workflow)
        self.assertIn("always() && failure()", workflow)
        self.assertIn("拒绝静默从空状态启动", workflow)
        self.assertIn("steps.prepare-state.outputs.available == 'true'", workflow)
        self.assertNotIn('if [ "$state_schema" !=', workflow)


if __name__ == "__main__":
    unittest.main()
