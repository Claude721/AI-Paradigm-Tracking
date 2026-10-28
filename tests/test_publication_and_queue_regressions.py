"""Offline shapes from the October audit; fixed clock, synthetic public data."""
import copy
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import config
from agents.paradigm_orchestrator import ParadigmOrchestrator
from database.paradigm_store import _deep_checkpoint_input_signature
from paradigms.analyzer import ParadigmSynthesizer
from paradigms.completion import require_completed_research
from paradigms.discovery import _merge_origins
from paradigms.models import EvidenceType, ParadigmCandidate, TechnicalEvidence, assess_candidate_freshness
from paradigms.search_receipts import record_search
from runtime_clock import research_window
from sources.official_repository_release_source import _primary_publication_date
from sources.priority_research_source import _ArticleParser, _discover_index_links
from sources.publication_dates import arxiv_identifier, arxiv_publication_dates, resolve_origin_dates
from tests import test_v0_delivery as fixture
from tests import test_execution_delivery as execution_fixture
from tests.test_production_failure_regressions import response, synthesis_payload
from tests.test_research_quality import material, NOW


LANDING = '''<meta name="citation_date" content="2026/10/20">
<meta property="article:modified_time" content="2026-10-25">
<div class="submission-history"><a>[v1]</a> Tue, 20 Oct 2026 14:41:46 UTC (1 KB)
<a>[v2]</a> Sun, 25 Oct 2026 06:57:49 UTC (2 KB)</div>'''


class PublicationDateTests(unittest.IsolatedAsyncioTestCase):
    def test_submission_history_has_priority_over_day_metadata_and_revision(self):
        dates = arxiv_publication_dates(LANDING)
        self.assertEqual(dates["published_at"], "2026-10-20T14:41:46+00:00")
        self.assertEqual(dates["modified_at"], "2026-10-25T06:57:49+00:00")
        self.assertEqual(_primary_publication_date("https://arxiv.org/abs/2610.00001", LANDING), dates["published_at"])

    def test_identifier_month_is_not_a_publication_date(self):
        self.assertEqual(_primary_publication_date("https://arxiv.org/abs/2610.00001", "<h1>Paper</h1>"), "")
        self.assertEqual(arxiv_publication_dates('<meta name="citation_date" content="2026/10">')["published_at"], "")

    def test_date_only_metadata_preserves_day_precision(self):
        dates = arxiv_publication_dates('<meta content="2026/10/22" name="citation_date">')
        self.assertEqual((dates["published_at"], dates["precision"]), ("2026-10-22", "day"))

    def test_day_overlapping_cutoff_is_unknown_precision_not_old(self):
        item = material(1)
        item.published_at = "2026-10-20"
        value = ParadigmCandidate(key="a", name="a", thesis="", problem_shift="", mechanism="", evidence=[item])
        result = assess_candidate_freshness(value, reference_time=datetime(2026, 10, 27, 8, 49, tzinfo=timezone.utc), window_days=7)
        self.assertEqual((result["decision"], result["classification"]), ("defer", "date_precision_unresolved"))

    def test_authoritative_first_date_wins_in_both_merge_orders(self):
        for reverse in (False, True):
            index = material(1, source="huggingface-papers")
            index.identifiers = {"arxiv": "2610.00001"}
            index.published_at = "2026-10-25T00:00:00Z"
            primary = copy.deepcopy(index)
            primary.source = "arxiv"
            primary.published_at = "2026-10-13T12:00:00Z"
            items = [index, primary] if not reverse else [primary, index]
            merged = _merge_origins(items)
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0].published_at, primary.published_at)
            self.assertEqual(merged[0].raw["publication_date_source"], "arxiv_api")

    def test_doi_bridge_deduplicates_without_identical_titles(self):
        a, b = material(1), material(2)
        a.identifiers = {"doi": "10.48550/arXiv.2610.00001"}
        b.identifiers = {"arxiv": "2610.00001"}
        self.assertEqual(arxiv_identifier(a), "2610.00001")
        self.assertEqual(len(_merge_origins([a, b])), 1)

    def test_external_query_or_citation_is_not_the_primary_work_identity(self):
        item = material(1)
        item.url = "https://lab.example/article?reference=https://arxiv.org/abs/2610.00001"
        self.assertEqual(arxiv_identifier(item), "")
        item.url = "https://arxiv.org/pdf/2610.00001v2.pdf"
        self.assertEqual(arxiv_identifier(item), "2610.00001")

    async def test_selected_index_date_is_corrected_before_revision_binding(self):
        a, b = material(1, source="openalex"), material(2, source="huggingface-papers")
        a.identifiers = b.identifiers = {"arxiv": "2610.00001"}
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=LANDING)))
        result = await resolve_origin_dates([a, b], client_factory=lambda **_: client)
        self.assertEqual(result, {"attempted": 1, "resolved": 1, "failed": 0})
        self.assertEqual(a.published_at, "2026-10-20T14:41:46+00:00")
        self.assertEqual(a.published_at, b.published_at)
        self.assertEqual(a.source_revision, "")

    async def test_date_failure_is_local_and_bound_inputs_are_never_mutated(self):
        a, b = material(1, source="huggingface-papers"), material(2, source="openalex")
        a.identifiers = {"arxiv": "2610.00001"}
        b.identifiers = {"arxiv": "2610.00002"}
        b.source_revision = "bound-version"
        before = copy.deepcopy(b.to_dict())
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
        result = await resolve_origin_dates([a, b], client_factory=lambda **_: client)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(b.to_dict(), before)
        value = ParadigmCandidate(key="a", name="a", thesis="", problem_shift="", mechanism="", evidence=[a])
        self.assertEqual(assess_candidate_freshness(value, reference_time=NOW, window_days=7)["decision"], "defer")


class HtmlInputTests(unittest.TestCase):
    def test_nested_hydration_card_is_discovered_without_executing_script(self):
        card = {"id": "example", "title": "Formal verification for real software", "href": "/research/formal-methods",
                "date": "Oct 22, 2026", "image": {"src": "/image.jpg", "alt": ""}}
        payload = 'self.__next_f.push(' + json.dumps([1, json.dumps({"resources": [card]})]) + ');'
        links = _discover_index_links("<script>" + payload + "</script>", "https://www.anthropic.com/research")
        self.assertEqual([(x.url, x.published_at) for x in links], [("https://www.anthropic.com/research/formal-methods", "2026-10-22")])

    def test_post_cards_do_not_replace_real_body_or_date(self):
        parser = _ArticleParser("https://research.ibm.com/blog/example")
        parser.feed('''<article data-testid="blog-post-card"><time datetime="2026-10-26"></time>Unrelated news</article>
            <main data-testid="blog-post"><h1>Mechanism</h1><time datetime="2026-08-12"></time>
            <p>Actual model interface and experiment.</p></main>''')
        self.assertIn("Actual model interface", parser.text)
        self.assertNotIn("Unrelated news", parser.text)
        self.assertEqual(parser.published_at, "2026-08-12")
        links = _discover_index_links('<article data-testid="blog-post-card"><a href="/blog/example">Research mechanism</a></article>', "https://research.ibm.com/blog")
        self.assertEqual(len(links), 1)


class AcquisitionQualificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_product_exclusions_do_not_consume_full_review_quota(self):
        process = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
        process.high_signal_discovery_lookback_days, process.ordinary_discovery_lookback_days = 30, 7
        values = [material(i) for i in range(12)]
        async def screen(items):
            return {x.fingerprint: ("screen_out" if x.title.endswith(("0", "1", "2", "3")) else "full_review", "explicit qualification") for x in items}
        process.analyzer = SimpleNamespace(enable_batch_prefilter=True, prefetch_origin_eligibility=screen)
        stats = {}
        with research_window(NOW), patch.object(config, "PARADIGM_RESEARCH_ORIGIN_LIMIT", 4):
            selected = await process._select_origin_scope(values, time.monotonic() + 1000, stats)
        self.assertEqual(len(selected), 4)
        self.assertTrue(all(not x.title.endswith(("0", "1", "2", "3")) for x in selected))
        self.assertTrue(all(not x.source_revision for x in values))
        self.assertEqual(stats["origin_scope_qualification"]["pool"], 12)

    async def test_failed_qualification_remains_unknown_not_technical_exclusion(self):
        process = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
        process.high_signal_discovery_lookback_days, process.ordinary_discovery_lookback_days = 30, 7
        process.analyzer = SimpleNamespace(enable_batch_prefilter=True, prefetch_origin_eligibility=AsyncMock(side_effect=ValueError("malformed")))
        stats = {}
        with research_window(NOW), patch.object(config, "PARADIGM_RESEARCH_ORIGIN_LIMIT", 4):
            selected = await process._select_origin_scope([material(i) for i in range(12)], time.monotonic() + 1000, stats)
        self.assertEqual(len(selected), 4)
        self.assertEqual(stats["origin_scope_qualification"]["screened_out"], 0)


class QueueBoundaryTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.V0DeliveryTests.setUp
    process = fixture.V0DeliveryTests.process
    distinct_process = execution_fixture.ExecutionDeliveryTests.distinct_process

    def campaign_with(self, pending):
        self.store.save_candidates([pending])
        campaign = self.store.campaigns.begin(reference_time=fixture.NOW, ordinary_days=7, high_signal_days=14,
            bootstrap=False, seeds=[], delivery_contract_version=5)
        self.store.campaigns.include(campaign.campaign_id, "deep", [(pending.key, _deep_checkpoint_input_signature(pending), pending.to_dict())])
        return campaign

    async def test_discovery_survives_failure_before_review_scope_is_frozen(self):
        process = self.process(accepted=True)
        process.delivery_contract_version = 5
        async def interrupt(*args):
            saved = self.store.campaigns.get(process.campaign.campaign_id)
            self.assertEqual(len(saved.discovery.origins), 1)
            raise RuntimeError("qualification interrupted")
        process._select_origin_scope = interrupt
        stats = await process.run(reference_time=fixture.NOW)
        self.assertTrue(stats["research_stage_interrupted"])
        self.assertEqual(stats["origin_count"], 1)
        self.assertEqual(len(self.store.campaigns.get(process.campaign.campaign_id).discovery.origins), 1)

    async def test_unavailable_upstream_does_not_block_other_structural_repairs(self):
        blocked_origin = fixture.origin(99)
        blocked_origin.source_revision = "unavailable-source-version"
        blocked = ParadigmCandidate(key="blocked", name="blocked", thesis="", problem_shift="", mechanism="", status="pending_deep", evidence=[blocked_origin])
        self.campaign_with(blocked)
        process, _ = self.distinct_process(3, fail=-1)
        process.delivery_contract_version = 5
        calls = []
        async def complete(**kwargs):
            repaired = "确定性校验失败：" in kwargs["messages"][0]["content"]
            calls.append(repaired)
            return response(synthesis_payload([0] if repaired else [999]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
        process.synthesizer = ParadigmSynthesizer(client=client, model="offline", allow_partial_sources=True)
        stats = await process.run(reference_time=fixture.NOW)
        self.assertEqual(calls, [False] * 3 + [True] * 3)
        self.assertEqual((stats["delivery_scope"]["deep_completed"], stats["delivery_scope"]["deep_unattempted"]), (3, 1))
        require_completed_research(stats, candidates=process.pending_delivery)

    async def test_changed_prerequisite_outside_initial_scope_has_a_real_review_path(self):
        old = self.store.observe_origins([fixture.origin(99)])[0][0]
        pending = ParadigmCandidate(key="historic", name="Historic hypothesis", thesis="", problem_shift="", mechanism="", status="pending_deep", evidence=[old])
        campaign = self.campaign_with(pending)
        changed = fixture.origin(99)
        changed.summary = "A revised original requiring a new review"
        self.store.observe_origins([changed])
        process, _ = self.distinct_process(3, fail=-1)
        process.delivery_contract_version = 5
        stats = await process.run(reference_time=fixture.NOW)
        self.assertEqual(stats["delivery_scope"]["deep_unattempted"], 0)
        saved = self.store.campaigns.get(campaign.campaign_id, load_discovery=False)
        self.assertIn(old.fingerprint, saved.stats["origin_dependency_scope"])
        self.assertNotIn(old.fingerprint, saved.stats["origin_scope"])
        self.assertEqual(stats["delivery_scope"]["origin_planned"], stats["delivery_scope"]["origin_selected"])
        self.assertTrue(self.store.candidate_inputs_current(self.store.load_candidate_snapshots(["historic"])[0]))
        require_completed_research(stats, candidates=process.pending_delivery)

    async def test_real_concurrent_write_still_fails_exact_checkpoint_compare(self):
        item = self.store.observe_origins([fixture.origin()])[0][0]
        original = ParadigmCandidate(key="cas", name="a", thesis="original", problem_shift="", mechanism="", status="pending_deep", evidence=[item])
        self.store.save_candidates([original])
        newer = copy.deepcopy(original)
        newer.thesis = "newer committed judgment"
        self.store.save_candidates([newer])
        progress = copy.deepcopy(original)
        record_search(progress, "hackernews", "completed")
        with self.assertRaisesRegex(ValueError, "candidate_snapshot_changed"):
            self.store.save_enrichment_checkpoint(original, progress)
        self.assertEqual(self.store.load_candidate_snapshots([original.key])[0].thesis, newer.thesis)

    async def test_failed_dependency_is_restored_without_reselecting_frozen_scope(self):
        old = self.store.observe_origins([fixture.origin(99)])[0][0]
        pending = ParadigmCandidate(key="historic", name="Historic hypothesis", thesis="", problem_shift="", mechanism="", status="pending_deep", evidence=[old])
        campaign = self.campaign_with(pending)
        changed = fixture.origin(99)
        changed.summary = "New source awaiting dependency review"
        self.store.observe_origins([changed])
        process, _ = self.distinct_process(3, fail=-1)
        process.delivery_contract_version = 5
        base = process.analyzer.run
        async def fail_dependency(items):
            return await base([x for x in items if x.fingerprint != old.fingerprint])
        process.analyzer.run = fail_dependency
        first = await process.run(reference_time=fixture.NOW)
        self.assertEqual(first["delivery_scope"]["deep_unattempted"], 1)
        require_completed_research(first, candidates=process.pending_delivery)
        frozen = list(self.store.campaigns.get(campaign.campaign_id, load_discovery=False).stats["origin_scope"])
        again, _ = self.distinct_process(3, fail=-1)
        again.delivery_contract_version = 5
        again._select_origin_scope = AsyncMock(side_effect=AssertionError("Cannot reselect frozen work"))
        second = await again.run(reference_time=fixture.NOW)
        self.assertEqual(second["delivery_scope"]["deep_unattempted"], 0)
        self.assertEqual(second["research_scope_origin_count"], 4)
        self.assertEqual(second["unselected_origin_count"], 0)
        self.assertEqual(self.store.campaigns.get(campaign.campaign_id, load_discovery=False).stats["origin_scope"], frozen)
        require_completed_research(second, candidates=again.pending_delivery)

    async def test_dependency_only_origin_work_is_not_omitted_from_run_audit(self):
        reused = self.store.observe_origins([fixture.origin(i) for i in (1, 2, 3)])[0]
        self.store.mark_evidence(reused, analyzed=True)
        old = self.store.observe_origins([fixture.origin(99)])[0][0]
        pending = ParadigmCandidate(key="historic", name="Historic hypothesis", thesis="", problem_shift="", mechanism="", status="pending_deep", evidence=[old])
        self.campaign_with(pending)
        changed = fixture.origin(99)
        changed.summary = "Only this upstream needs a new origin judgment"
        self.store.observe_origins([changed])
        process, _ = self.distinct_process(3, fail=-1)
        process.delivery_contract_version = 5
        stats = await process.run(reference_time=fixture.NOW)
        self.assertEqual(stats["dependency_origin_count"], 1)
        self.assertEqual(stats["analysis_completed_count"], 1)
        self.assertEqual(stats["planned_analysis_count"], 1)
        self.assertEqual(stats["delivery_scope"]["origin_completed"], 4)
        require_completed_research(stats, candidates=process.pending_delivery)

    async def test_queued_base_is_reloaded_before_paying_for_its_visit(self):
        item = self.store.observe_origins([fixture.origin(99)])[0][0]
        self.store.mark_evidence([item], analyzed=True)
        pending = ParadigmCandidate(key="queued", name="Queued hypothesis", thesis="old queue copy", problem_shift="", mechanism="", status="pending_deep", evidence=[item])
        self.campaign_with(pending)
        process, _ = self.distinct_process(3, fail=-1)
        process.delivery_contract_version = 5
        loader = self.store.load_pending_deep_candidates
        def load_old_then_commit_new(*args, **kwargs):
            rows = loader(*args, **kwargs)
            current = self.store.load_candidate_snapshots([pending.key])[0]
            current.thesis = "newly committed base"
            self.store.save_candidates([current])
            return rows
        self.store.load_pending_deep_candidates = load_old_then_commit_new
        base = process.synthesizer.run
        seen = []
        async def synthesize(items):
            for value in items:
                if value.key == pending.key:
                    seen.append(value.thesis)
            return await base(items)
        process.synthesizer.run = synthesize
        stats = await process.run(reference_time=fixture.NOW)
        self.assertEqual(seen, ["newly committed base"])
        self.assertEqual(stats["candidate_execution_deferred_count"], 0)
