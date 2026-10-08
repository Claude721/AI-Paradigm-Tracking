"""Offline regression tests for the first V0 reconstruction batches."""
from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agents.paradigm_orchestrator import ParadigmOrchestrator, _commit_origin_analysis_checkpoint
from database.paradigm_store import ParadigmStore
from database.state_migration import migrate_state
from paradigms.models import EvidenceType, ParadigmCandidate, TechnicalEvidence, assess_candidate_freshness
from runtime_clock import research_now, research_window, scheduled_date
from sources.arxiv_source import ArxivSource
from sources.official_repository_release_source import OfficialRepositoryReleaseSource


def paper(name="one"):
    return TechnicalEvidence(
        source="arxiv", evidence_type=EvidenceType.PRIMARY_PAPER,
        title=name, url=f"https://arxiv.org/abs/{name}", summary="Original abstract",
        published_at="2026-09-10T00:00:00+00:00", identifiers={"arxiv": name},
    )


def route(evidence):
    return ParadigmCandidate(key="route", name="机制", thesis="假说", problem_shift="瓶颈",
                             mechanism="机制变化", evidence=[evidence])


def progress(item, completed):
    item.raw.update({
        "origin_kind": "technical_report",
        "technical_report_checkpoint_version": "technical-report-checkpoint-v1",
        "technical_report_mechanism_seeds": [
            {"canonical_name": "one"}, {"canonical_name": "two"}, {"canonical_name": "three"},
        ],
        "technical_report_completed_mechanisms": {
            key: {"mechanism": key} for key in completed
        },
        "technical_report_slice_pending": True,
    })
    return item


class ResearchClockTests(unittest.TestCase):
    def test_frozen_clock_drives_sources_freshness_and_report_date(self):
        for year in (2026, 2030):
            reference = datetime(year, 9, 11, 23, 59, tzinfo=timezone.utc)
            with self.subTest(year=year), research_window(reference):
                origin = paper()
                origin.published_at = f"{year}-09-10T00:00:00+00:00"
                self.assertEqual(ArxivSource()._now(), reference)
                self.assertEqual(assess_candidate_freshness(route(origin), window_days=7)["decision"], "include")
                self.assertEqual(scheduled_date(research_now(), timezone_name="Asia/Shanghai"), f"{year}-09-12")

    def test_naive_reference_rejected(self):
        with self.assertRaises(ValueError):
            with research_window(datetime(2026, 9, 11)):
                pass
        with self.assertRaises(ValueError):
            OfficialRepositoryReleaseSource(reference_time=datetime(2026, 9, 11))

    def test_nested_context_restores_after_exception(self):
        first = datetime(2026, 9, 4, tzinfo=timezone.utc)
        with research_window(first):
            with self.assertRaises(RuntimeError):
                with research_window(datetime(2026, 9, 11, tzinfo=timezone.utc)):
                    raise RuntimeError("cancelled")
            self.assertEqual(research_now(), first)

    def test_async_runs_do_not_share_business_clock(self):
        async def worker(day):
            value = datetime(2026, 9, day, tzinfo=timezone.utc)
            with research_window(value):
                await asyncio.sleep(0)
                return research_now()

        async def run():
            return await asyncio.gather(worker(4), worker(11))

        self.assertEqual([value.day for value in asyncio.run(run())], [4, 11])

    def test_production_orchestrator_enters_frozen_context(self):
        orchestrator = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
        async def body():
            await asyncio.sleep(0)
            return {"time": research_now()}
        orchestrator._run_research = body
        reference = datetime(2026, 9, 4, tzinfo=timezone.utc)
        self.assertEqual(asyncio.run(orchestrator.run(reference_time=reference))["time"], reference)


class OriginStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "fixture.db"
        self.store = ParadigmStore(self.path)

    def observe(self, item=None):
        selected, _, checkpoint = self.store.observe_origins([item or paper()])
        self.assertEqual(checkpoint.rejected_count, 0)
        return selected[0]

    def state(self):
        with sqlite3.connect(self.path) as conn:
            return conn.execute("SELECT source_signature, source_payload_json, checkpoint_json, baseline_status FROM origin_research_state").fetchone()

    def view(self):
        with sqlite3.connect(self.path) as conn:
            return json.loads(conn.execute("SELECT payload_json FROM evidence_state").fetchone()[0])

    def test_hydration_does_not_reopen_completed_original(self):
        item = self.observe()
        item.organization = "Institution found during hydration"
        item.raw["document_excerpt"] = "Long document"
        self.store.mark_evidence([item], analyzed=True)
        selected, stats, _ = self.store.observe_origins([paper()])
        self.assertEqual(selected, [])
        self.assertEqual(stats["unchanged_skip"], 1)
        self.assertEqual(json.loads(self.state()[1])["organization"], "")
        self.assertEqual(self.view()["organization"], item.organization)
        self.assertEqual(self.view()["raw"]["document_excerpt"], "Long document")

    def test_research_enrichment_cannot_advance_origin_progress(self):
        item = self.observe()
        candidate_view = progress(copy.deepcopy(item), ["one", "two"])
        self.store.mark_evidence([candidate_view], enrichment_only=True)
        self.assertEqual(json.loads(self.state()[2]), {})
        self.assertNotIn("technical_report_completed_mechanisms", self.view()["raw"])
        self.assertEqual(len(self.store.load_pending_origins()), 1)

    def test_old_progress_cannot_remove_completed_mechanisms(self):
        item = self.observe()
        newer = progress(copy.deepcopy(item), ["one", "two"])
        self.store.mark_evidence([newer])
        older = progress(copy.deepcopy(item), ["one"])
        older.raw["technical_report_completed_mechanisms"]["one"] = {"mechanism": "stale text"}
        self.store.mark_evidence([older])
        completed = json.loads(self.state()[2])["technical_report_completed_mechanisms"]
        self.assertEqual(set(completed), {"one", "two"})
        self.assertEqual(completed["one"]["mechanism"], "one")
        self.assertEqual(self.view()["raw"]["technical_report_completed_mechanisms"], completed)

    def test_enrichment_cannot_replace_newer_checkpoint(self):
        item = self.observe()
        self.store.mark_evidence([progress(copy.deepcopy(item), ["one", "two"])])
        stale = progress(copy.deepcopy(item), ["one"])
        stale.organization = "Enriched"
        self.store.mark_evidence([stale], enrichment_only=True)
        self.assertEqual(set(self.view()["raw"]["technical_report_completed_mechanisms"]), {"one", "two"})

    def test_only_execution_can_mark_an_origin_complete(self):
        item = self.observe()
        for mode in ("enrichment_only", "source_observation"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.store.mark_evidence([item], analyzed=True, **{mode: True})
        self.assertEqual(len(self.store.load_pending_origins()), 1)

    def test_bound_callback_cannot_recreate_missing_source_state(self):
        item = self.observe()
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM origin_research_state")
        result = self.store.mark_evidence([item], analyzed=True)
        self.assertEqual(result.stale_revision_count, 1)
        self.assertIsNone(self.state())

    def test_old_snapshot_does_not_reduce_attempt_count(self):
        item = self.observe()
        item.raw["analysis_failure_count"] = 3
        self.store.mark_evidence([item])
        item.raw["analysis_failure_count"] = 1
        self.store.mark_evidence([item])
        self.assertEqual(json.loads(self.state()[2])["analysis_failure_count"], 3)

    def test_revision_change_invalidates_only_that_origin(self):
        item = self.observe()
        self.store.mark_evidence([progress(item, ["one"])], analyzed=True)
        updated = paper()
        updated.summary = "New technical intervention"
        selected, stats, _ = self.store.observe_origins([updated])
        self.assertEqual(stats["changed"], 1)
        self.assertNotEqual(selected[0].source_revision, item.source_revision)
        self.assertEqual(json.loads(self.state()[2]), {})
        self.assertEqual(len(self.store.load_pending_origins()), 1)

    def test_old_revision_callback_cannot_mark_new_revision_done(self):
        old = self.observe()
        fresh = paper()
        fresh.raw["updated_at"] = "2026-09-11T00:00:00Z"
        self.store.observe_origins([fresh])
        before = self.state()
        result = self.store.mark_evidence([old], analyzed=True)
        self.assertEqual(result.stale_revision_count, 1)
        self.assertEqual(result.written_count, 0)
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.store.load_pending_origins()), 1)

    def test_bad_checkpoint_isolated_from_healthy_peer(self):
        items, _, _ = self.store.observe_origins([paper("bad"), paper("good")])
        items[0].raw["technical_report_completed_mechanisms"] = []
        result = self.store.mark_evidence(items, analyzed=True)
        self.assertEqual(result.rejected_count, 1)
        self.assertEqual([item.title for item in result.accepted], ["good"])
        self.assertEqual([item.title for item in self.store.load_pending_origins()], ["bad"])
        self.assertEqual(migrate_state(self.path, 7), 7)

    def test_incompatible_index_does_not_replace_progress(self):
        item = self.observe()
        self.store.mark_evidence([progress(item, ["one"])])
        before = self.state()
        changed = copy.deepcopy(item)
        changed.raw["technical_report_mechanism_seeds"] = [{"canonical_name": "other"}]
        result = self.store.mark_evidence([changed])
        self.assertEqual(result.rejected_count, 1)
        self.assertEqual(self.state(), before)

    def test_source_update_can_add_linked_report_with_same_abstract(self):
        item = self.observe()
        self.store.mark_evidence([item], analyzed=True)
        revised = paper()
        revised.raw["linked_research_documents"] = [{"title": "Full Report", "url": "https://example.org/report.pdf"}]
        selected, stats, _ = self.store.observe_origins([revised])
        self.assertEqual(len(selected), 1)
        self.assertEqual(stats["changed"], 1)

    def test_transaction_failure_rolls_back_candidate_and_completion(self):
        item = self.observe()
        original = self.store.mark_evidence
        def fail_after_write(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("interrupted before commit")
        with patch.object(self.store, "mark_evidence", side_effect=fail_after_write):
            with self.assertRaises(RuntimeError):
                _commit_origin_analysis_checkpoint(self.store, [route(item)], [item])
        self.assertEqual(len(self.store.load_pending_origins()), 1)
        self.assertEqual(self.store.load_pending_deep_candidates(), [])

    def test_stale_checkpoint_rolls_back_candidate_insertion(self):
        old = self.observe()
        new = paper()
        new.summary = "Changed"
        self.store.observe_origins([new])
        with self.assertRaises(ValueError):
            _commit_origin_analysis_checkpoint(self.store, [route(old)], [old])
        self.assertEqual(self.store.load_pending_deep_candidates(), [])
        self.assertEqual(len(self.store.load_pending_origins()), 1)

    def test_partial_report_and_downstream_candidate_commit_together(self):
        item = progress(self.observe(), ["one"])
        _commit_origin_analysis_checkpoint(self.store, [route(item)], [], [item])
        self.assertEqual(len(self.store.load_pending_deep_candidates()), 1)
        self.assertEqual(len(self.store.load_pending_origins()), 1)
        self.assertEqual(set(json.loads(self.state()[2])["technical_report_completed_mechanisms"]), {"one"})

    def test_v6_migration_preserves_completed_result_and_reconciles_once(self):
        original = paper()
        original.organization = "Legacy hydrated institution"
        self.store.mark_evidence([original], analyzed=True)
        self.store.save_candidates([route(original)])
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TABLE origin_research_state")
            payload = json.loads(conn.execute("SELECT payload_json FROM evidence_state").fetchone()[0])
            payload.pop("source_revision", None)
            conn.execute("UPDATE evidence_state SET payload_json=?", (json.dumps(payload),))
        self.assertEqual(migrate_state(self.path, 6), 7)
        self.store = ParadigmStore(self.path)
        self.assertEqual(self.state()[3], "legacy_unverified")
        selected, stats, _ = self.store.observe_origins([paper()])
        self.assertEqual(selected, [])
        self.assertEqual(stats["unchanged_skip"], 1)
        self.assertEqual(self.state()[3], "observed")
        changed = paper()
        changed.organization = "Genuinely changed source affiliation"
        self.assertEqual(self.store.observe_origins([changed])[1]["changed"], 1)

    def test_v7_corruption_is_rejected_not_reconstructed_from_enrichment(self):
        self.observe()
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM origin_research_state")
        with self.assertRaisesRegex(ValueError, "缺少来源版本"):
            migrate_state(self.path, 7)

    def test_corrupt_checkpoint_mapping_is_rejected_during_restore(self):
        self.observe()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE origin_research_state SET checkpoint_json=?", ('{"technical_report_completed_mechanisms": []}',))
        with self.assertRaises(ValueError):
            migrate_state(self.path, 7)

    def test_three_week_replay_does_not_reopen_unchanged_originals(self):
        for day in (4, 11, 18):
            with research_window(datetime(2026, 9, day, tzinfo=timezone.utc)):
                selected, stats, _ = self.store.observe_origins([paper()])
                if day == 4:
                    selected[0].organization = "Hydrated"
                    self.store.mark_evidence(selected, analyzed=True)
                    self.assertEqual(stats["new"], 1)
                else:
                    self.assertEqual(selected, [])
                    self.assertEqual(stats["unchanged_skip"], 1)
                self.store = ParadigmStore(self.path)

    def test_observation_failure_does_not_drop_healthy_source(self):
        bad = paper("bad")
        bad.summary = 42
        selected, _, checkpoint = self.store.observe_origins([bad, paper("good")])
        self.assertEqual([item.title for item in selected], ["good"])
        self.assertEqual(checkpoint.rejected_count, 1)

    def test_supporting_evidence_keeps_legacy_persistence_contract(self):
        item = TechnicalEvidence(source="hn", evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
                                 title="Discussion", url="https://example.org/discussion")
        result = self.store.mark_evidence([item], enrichment_only=True)
        self.assertEqual(result.written_count, 1)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM origin_research_state").fetchone()[0], 0)

    def test_reopening_store_does_not_change_origin_state(self):
        self.observe()
        before = self.state()
        ParadigmStore(self.path)
        self.assertEqual(self.state(), before)


if __name__ == "__main__":
    unittest.main()
