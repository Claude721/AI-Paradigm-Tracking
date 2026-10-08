"""Multi-window orchestrator replay with real SQLite and no external I/O."""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
from agents.paradigm_orchestrator import ParadigmOrchestrator
from database.paradigm_store import ParadigmStore
from paradigms.discovery import DiscoveryBatch
from paradigms.enrichment import EvidenceEnricher
from paradigms.models import (
    EvidenceType, ParadigmCandidate, ParadigmExtraction, TechnicalEvidence,
    assess_candidate_freshness,
)
from run_audit import run_audit
from runtime_clock import research_window


def _origin(index: int) -> TechnicalEvidence:
    return TechnicalEvidence(
        source="arxiv",
        evidence_type=EvidenceType.PRIMARY_PAPER,
        title=f"Ordinary mechanism study {index}",
        url=f"https://arxiv.org/abs/2609.{index:05d}",
        published_at="2026-09-03T00:00:00+00:00",
    )


def _negative(item: TechnicalEvidence) -> ParadigmExtraction:
    return ParadigmExtraction(
        evidence=item,
        is_candidate=False,
        canonical_name=f"Incremental study {item.title}",
        thesis="仅在既有方法内调整局部组件",
        problem_shift="问题边界没有变化",
        mechanism="局部参数调整",
        route_family="incremental optimization",
        rubric_assessment={
            "decision": "reject", "answer_coverage": 1.0, "score": 20,
        },
    )


class OfflineReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_origin_reaches_deep_stage_with_large_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "state.db")
            old = [_origin(index) for index in range(1, 401)]
            for item in old:
                item.published_at = "2026-08-01T00:00:00+00:00"
            current = _origin(99999)
            current.published_at = "2026-09-22T00:00:00+00:00"
            analyzed = []
            deep_seen = []

            async def analyze(group):
                analyzed.extend(item.title for item in group)
                return [ParadigmExtraction(
                    evidence=item, is_candidate=True,
                    canonical_name="Current mechanism",
                    route_family="Current technical route",
                    thesis="A new training interface",
                    problem_shift="Training state changes",
                    mechanism="Update state from feedback",
                    rubric_assessment={
                        "decision": "deep_dive", "answer_coverage": 1.0,
                        "score": 80,
                    },
                ) for item in group]

            async def synthesize(values):
                deep_seen.extend(item.key for item in values)
                for item in values:
                    item.rubric_assessment = {
                        "decision": "observe", "answer_coverage": 1.0,
                        "score": 60,
                    }
                return values

            orchestrator = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
            orchestrator.store = store
            orchestrator.bootstrap_mode = False
            orchestrator.ordinary_discovery_lookback_days = 7
            orchestrator.high_signal_discovery_lookback_days = 30
            orchestrator.discovery_lookback_days = 30
            orchestrator.discovery = SimpleNamespace(run=AsyncMock(return_value=DiscoveryBatch(
                origins=[*old, current], supporting=[],
                source_counts={"arxiv": 401},
                coverage={"covered_domains": 0, "total_domains": 0},
            )))
            orchestrator.analyzer = SimpleNamespace(run=analyze)
            orchestrator.enricher = SimpleNamespace(
                hydrate_priority_origins=AsyncMock(return_value={}),
                run=AsyncMock(side_effect=lambda values, _: values),
                finalize=EvidenceEnricher.finalize,
            )
            orchestrator.synthesizer = SimpleNamespace(run=synthesize)
            orchestrator.trajectory = SimpleNamespace(
                run=AsyncMock(side_effect=lambda values: values)
            )
            orchestrator.pending_delivery = []
            run_audit.reset()
            with patch.object(config, "PARADIGM_ANALYSIS_SAFETY_LIMIT", 1):
                result = await orchestrator.run(
                    reference_time=datetime(2026, 9, 23, tzinfo=timezone.utc)
                )
            self.assertEqual(analyzed, [current.title])
            self.assertEqual(deep_seen, ["current-mechanism"])
            self.assertEqual(result["current_window_origin_planned_count"], 1)
            self.assertEqual(result["current_window_origin_completed_count"], 1)
            self.assertEqual(result["deep_candidate_count"], 1)
            self.assertEqual(result["current_window_deep_stage_returned_count"], 1)
            self.assertEqual(result["work_queue_after"]["pending_origin_count"], 400)

    async def test_observed_metric_growth_expires_after_window(self) -> None:
        primary = _origin(1)
        primary.published_at = "2025-01-01T00:00:00+00:00"
        implementation = TechnicalEvidence(
            source="github",
            evidence_type=EvidenceType.IMPLEMENTATION,
            title="Independent implementation",
            url="https://github.com/independent/lab",
            published_at="2025-02-01T00:00:00+00:00",
            metrics={"stars": 10},
            raw={"independence": "independent"},
        )
        newer = copy.deepcopy(implementation)
        newer.metrics["stars"] = 45
        candidate = ParadigmCandidate(
            key="route", name="Route", thesis="", problem_shift="",
            mechanism="", evidence=[primary, implementation],
        )
        enricher = EvidenceEnricher.__new__(EvidenceEnricher)
        enricher.concurrency = 1
        enricher.community = SimpleNamespace(
            search=AsyncMock(return_value=[newer]), coverage=lambda: {},
        )
        observed_at = datetime(2026, 9, 4, tzinfo=timezone.utc)
        with research_window(observed_at):
            changed = await enricher.refresh([candidate], [])
        self.assertEqual(len(changed), 1)
        updated = next(
            item for item in candidate.evidence
            if item.fingerprint == implementation.fingerprint
        )
        self.assertEqual(updated.raw["metric_delta"], {"stars": 35})
        self.assertEqual(
            updated.raw["metric_delta_observed_at"], observed_at.isoformat()
        )
        self.assertEqual(
            assess_candidate_freshness(
                candidate, reference_time=observed_at, window_days=7
            )["decision"],
            "include",
        )
        self.assertEqual(
            assess_candidate_freshness(
                candidate,
                reference_time=datetime(2026, 9, 18, tzinfo=timezone.utc),
                window_days=7,
            )["decision"],
            "defer",
        )

    async def test_index_only_refresh_retains_lead_without_resynthesis(self) -> None:
        lead = TechnicalEvidence(
            source="tavily-web",
            evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
            title="Unverified related page",
            url="https://example.org/indexed-lead",
            raw={"indexed_discovery_only": True},
        )
        candidate = ParadigmCandidate(
            key="route", name="Route", thesis="", problem_shift="",
            mechanism="", evidence=[_origin(1)],
        )
        enricher = EvidenceEnricher.__new__(EvidenceEnricher)
        enricher.concurrency = 1
        enricher.community = SimpleNamespace(
            search=AsyncMock(return_value=[lead]),
            coverage=lambda: {"web_index": "partial"},
        )
        changed = await enricher.refresh([candidate], [])
        self.assertEqual(changed, [])
        self.assertTrue(any(
            item.fingerprint == lead.fingerprint for item in candidate.evidence
        ))

        verified = copy.deepcopy(lead)
        verified.raw = {
            "independence": "independent",
            "relationship": "independent_mechanism_analysis",
            "substantive_uptake": True,
        }
        verified.summary = "Verified route-specific mechanism analysis"
        candidate.evidence = [_origin(1), verified]
        unchanged = await enricher.refresh([candidate], [])
        self.assertEqual(unchanged, [])
        retained = next(
            item for item in candidate.evidence
            if item.fingerprint == lead.fingerprint
        )
        self.assertEqual(retained.summary, verified.summary)
        self.assertTrue(retained.raw["substantive_uptake"])

    async def test_verified_discussion_survives_repeat_search_and_small_growth(self) -> None:
        primary = _origin(1)
        primary.published_at = "2025-01-01T00:00:00+00:00"
        verified = TechnicalEvidence(
            source="reddit", evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
            title="Reddit discussion", url="https://www.reddit.com/r/Local/comments/abc/",
            summary="", metrics={"score": 40},
            raw={
                "relationship": "independent_mechanism_analysis",
                "independence": "independent",
                "substantive_uptake": True,
                "substantive_uptake_source": "synthesis-v1",
                "substantive_uptake_route_key": "route",
                "ephemeral_content": True,
            },
        )
        repeated = copy.deepcopy(verified)
        repeated.summary = "A detailed public discussion of the mechanism and its limits."
        repeated.metrics["score"] = 41
        repeated.raw = {"relationship": "official_reddit_exact_work_search",
                        "ephemeral_content": True}
        candidate = ParadigmCandidate(
            key="route", name="Route", thesis="", problem_shift="",
            mechanism="", evidence=[primary, verified],
        )
        enricher = EvidenceEnricher.__new__(EvidenceEnricher)
        enricher.concurrency = 1
        enricher.community = SimpleNamespace(
            search=AsyncMock(return_value=[repeated]), coverage=lambda: {},
        )
        changed = await enricher.refresh([candidate], [])
        self.assertEqual(changed, [])
        retained = next(item for item in candidate.evidence if item.source == "reddit")
        self.assertTrue(retained.raw["substantive_uptake"])
        self.assertEqual(retained.raw["substantive_uptake_source"], "synthesis-v1")
        self.assertEqual(retained.raw["metric_baseline"]["score"], 40)

    async def test_metric_growth_accumulates_from_stable_baseline(self) -> None:
        primary = _origin(1)
        primary.published_at = "2025-01-01T00:00:00+00:00"
        implementation = TechnicalEvidence(
            source="github", evidence_type=EvidenceType.IMPLEMENTATION,
            title="Independent implementation",
            url="https://github.com/independent/lab",
            published_at="2025-02-01T00:00:00+00:00",
            metrics={"stars": 10}, raw={"independence": "independent"},
        )
        candidate = ParadigmCandidate(
            key="route", name="Route", thesis="", problem_shift="",
            mechanism="", evidence=[primary, implementation],
        )
        next_week = copy.deepcopy(implementation)
        next_week.metrics["stars"] = 20
        third_week = copy.deepcopy(implementation)
        third_week.metrics["stars"] = 37
        enricher = EvidenceEnricher.__new__(EvidenceEnricher)
        enricher.concurrency = 1
        enricher.community = SimpleNamespace(
            search=AsyncMock(side_effect=[[next_week], [third_week]]),
            coverage=lambda: {},
        )
        with research_window(datetime(2026, 9, 4, tzinfo=timezone.utc)):
            first = await enricher.refresh([candidate], [])
        self.assertEqual(first, [])
        current = next(item for item in candidate.evidence if item.source == "github")
        self.assertEqual(current.raw["metric_baseline"]["stars"], 10)
        with research_window(datetime(2026, 9, 11, tzinfo=timezone.utc)):
            second = await enricher.refresh([candidate], [])
        self.assertEqual(len(second), 1)
        current = next(item for item in candidate.evidence if item.source == "github")
        self.assertEqual(current.raw["metric_delta"], {"stars": 27})
        self.assertEqual(current.raw["metric_baseline"]["stars"], 37)
        self.assertEqual(
            assess_candidate_freshness(
                candidate,
                reference_time=datetime(2026, 9, 11, tzinfo=timezone.utc),
                window_days=7,
            )["decision"],
            "include",
        )

    async def test_material_metric_event_changes_report_signature_inside_bucket(self) -> None:
        implementation = TechnicalEvidence(
            source="github", evidence_type=EvidenceType.IMPLEMENTATION,
            title="Implementation", url="https://github.com/independent/lab",
            metrics={"stars": 110}, raw={"independence": "independent"},
        )
        candidate = ParadigmCandidate(
            key="route", name="Route", thesis="", problem_shift="",
            mechanism="same mechanism", evidence=[_origin(1), implementation],
        )
        first_signature = candidate.report_signature
        implementation.metrics["stars"] = 135
        implementation.raw["metric_delta"] = {"stars": 25}
        implementation.raw["metric_delta_observed_at"] = (
            "2026-09-11T00:00:00+00:00"
        )
        self.assertNotEqual(candidate.report_signature, first_signature)
        material_signature = candidate.report_signature
        self.assertEqual(candidate.report_signature, material_signature)

        tiny = copy.deepcopy(candidate)
        tiny.evidence[1].raw["metric_delta"] = {"stars": 1}
        self.assertEqual(tiny.report_signature, first_signature)

        unverified = copy.deepcopy(candidate)
        unverified.evidence[1].raw["independence"] = "unverified"
        unverified.evidence[1].raw["metric_delta"] = {"stars": 25}
        unverified_without_event = copy.deepcopy(unverified)
        unverified_without_event.evidence[1].raw.pop("metric_delta")
        unverified_without_event.evidence[1].raw.pop("metric_delta_observed_at")
        self.assertEqual(
            unverified.report_signature, unverified_without_event.report_signature
        )

    async def test_official_repository_growth_is_update_not_independent_replication(self) -> None:
        primary = _origin(1)
        primary.published_at = "2025-01-01T00:00:00+00:00"
        official = TechnicalEvidence(
            source="github", evidence_type=EvidenceType.IMPLEMENTATION,
            title="Official implementation", url="https://github.com/lab/route",
            published_at="2025-02-01T00:00:00+00:00",
            metrics={"stars": 135},
            raw={
                "relationship": "paper_linked_repository",
                "independence": "official",
                "metric_delta": {"stars": 25},
                "metric_delta_observed_at": "2026-09-11T00:00:00+00:00",
            },
        )
        candidate = ParadigmCandidate(
            key="route", name="Route", thesis="", problem_shift="",
            mechanism="", evidence=[primary, official],
        )
        freshness = assess_candidate_freshness(
            candidate,
            reference_time=datetime(2026, 9, 11, tzinfo=timezone.utc),
            window_days=7,
        )
        self.assertEqual(freshness["decision"], "include")
        self.assertIn("官方实现采用指标", freshness["current_uptake_evidence"][0]["qualification_reason"])

    async def test_title_only_hit_and_unverified_metric_jump_do_not_resynthesize(self) -> None:
        candidate = ParadigmCandidate(
            key="route", name="Route", thesis="", problem_shift="",
            mechanism="", evidence=[_origin(1)],
        )
        lead = TechnicalEvidence(
            source="hackernews", evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
            title="Similar title", url="https://news.ycombinator.com/item?id=9",
            metrics={"score": 2},
            raw={"relationship": "exact_or_mechanism_title_match"},
        )
        bigger = copy.deepcopy(lead)
        bigger.metrics["score"] = 200
        enricher = EvidenceEnricher.__new__(EvidenceEnricher)
        enricher.concurrency = 1
        enricher.community = SimpleNamespace(
            search=AsyncMock(side_effect=[[lead], [bigger]]),
            coverage=lambda: {},
        )
        self.assertEqual(await enricher.refresh([candidate], []), [])
        self.assertEqual(await enricher.refresh([candidate], []), [])
        self.assertEqual(enricher.community.search.await_count, 2)

    async def test_three_windows_defer_resume_and_deduplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "state.db")
            extracted: list[str] = []

            async def analyze(group):
                extracted.extend(item.fingerprint for item in group)
                return [_negative(item) for item in group]

            orchestrator = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
            orchestrator.store = store
            orchestrator.bootstrap_mode = False
            orchestrator.ordinary_discovery_lookback_days = 7
            orchestrator.high_signal_discovery_lookback_days = 30
            orchestrator.discovery_lookback_days = 30
            orchestrator.analyzer = SimpleNamespace(run=analyze)
            orchestrator.enricher = SimpleNamespace(
                hydrate_priority_origins=AsyncMock(return_value={}),
                finalize=EvidenceEnricher.finalize,
            )
            orchestrator.pending_delivery = []

            def batch():
                return DiscoveryBatch(
                    origins=[copy.deepcopy(_origin(index)) for index in range(4)],
                    supporting=[],
                    source_counts={"arxiv": 4},
                    coverage={"covered_domains": 0, "total_domains": 0},
                )

            orchestrator.discovery = SimpleNamespace(run=AsyncMock(side_effect=[
                batch(), batch(), batch(),
            ]))
            dates = [
                datetime(2026, 9, day, tzinfo=timezone.utc)
                for day in (4, 11, 18)
            ]
            results = []
            for index, reference in enumerate(dates):
                run_audit.reset()
                with patch.object(
                    config, "PARADIGM_ANALYSIS_SAFETY_LIMIT", 1 if index == 0 else 0
                ):
                    results.append(await orchestrator.run(reference_time=reference))

            first, second, third = results
            self.assertEqual(first["result_kind"], "research_blocked")
            self.assertEqual(first["work_queue_after"]["pending_origin_count"], 3)
            self.assertEqual(first["pending_queue_net_change"], 3)
            self.assertEqual(second["result_kind"], "complete_no_signal")
            self.assertEqual(second["analysis_count"], 3)
            self.assertEqual(second["work_queue_after"]["pending_total_count"], 0)
            self.assertEqual(second["pending_queue_net_change"], -3)
            self.assertEqual(third["analysis_count"], 0)
            self.assertEqual(third["pending_queue_net_change"], 0)
            self.assertEqual(len(extracted), 4)
            self.assertEqual(len(set(extracted)), 4)
            self.assertEqual(store.load_pending_origins(), [])
            self.assertEqual(store.load_pending_deep_candidates(), [])
