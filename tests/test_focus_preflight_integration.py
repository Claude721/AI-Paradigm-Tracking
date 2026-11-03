"""Combined production paths: preflight queries, old evidence and isolation."""
import asyncio
import copy
import json
import sqlite3
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import config
from paradigms.enrichment import EvidenceEnricher
from paradigms.models import EvidenceType, candidate_from_dict, scrub_ephemeral_evidence
from paradigms.propagation import certify_diffusion, verified_diffusion
from paradigms.research_focus import assess
from paradigms.search_receipts import record_search
from runtime_clock import research_window
from sources.work_evidence_cache import WorkEvidenceCache, PROVIDERS, CHECKPOINT
from sources.paradigm_evidence_source import CommunityEvidenceClient
from sources.direction_signal_source import DirectionSignalClient
from tests.test_research_focus import focus_process
from tests.test_propagation_policy import route, post, NOW
from tests.test_brief_and_momentum import analysis
from paradigms.evidence_validation import certify_synthesis_discussion
from tests import test_v0_delivery as fixture
from tests import test_direction_signals as directions


def fake_community(*, error=None):
    async def search(candidate):
        for name in PROVIDERS:
            record_search(candidate, name, "disabled", reason="test_off")
        if error:
            record_search(candidate, "hackernews", "failed", reason=type(error).__name__)
            raise error
        item = post(route())
        item.title = candidate.evidence[0].title
        item.raw["referenced_url"] = candidate.evidence[0].url
        record_search(candidate, "hackernews", "completed", count=1)
        candidate.community_search_receipts["hackernews"]["evidence_fingerprints"] = [item.fingerprint]
        return [item]
    return SimpleNamespace(search=AsyncMock(side_effect=search))


class WorkQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clock = research_window(NOW); clock.__enter__(); self.addCleanup(clock.__exit__, None, None, None)
        for key, value in (("PARADIGM_RESEARCH_FOCUS_ENABLED", True), ("PARADIGM_DIRECTION_SIGNALS_ENABLED", False)):
            patcher = patch.object(config, key, value); patcher.start(); self.addCleanup(patcher.stop)

    async def test_concurrent_mechanisms_share_queries_but_not_technical_validation(self):
        community = fake_community(); cache = WorkEvidenceCache()
        first, second = route(age=1), route(age=1); second.key = "another-mechanism"
        await asyncio.gather(cache.attach(first, community), cache.attach(second, community))
        self.assertEqual(community.search.await_count, 1)
        self.assertTrue(verified_diffusion(first, first.evidence[-1]))
        self.assertTrue(verified_diffusion(second, second.evidence[-1]))
        self.assertNotEqual(first.evidence[-1].raw["diffusion_receipt"]["binding"], second.evidence[-1].raw["diffusion_receipt"]["binding"])
        self.assertFalse(second.evidence[-1].raw.get("substantive_uptake"))

    async def test_restart_uses_metadata_without_private_bodies_or_actor_names(self):
        community = fake_community(); first = route(age=1)
        await WorkEvidenceCache().attach(first, community)
        checkpoint = first.evidence[0].raw[CHECKPOINT]
        encoded = json.dumps(checkpoint)
        self.assertNotIn("Ordinary Member", encoded)
        self.assertNotIn("Worth reading!", encoded)
        second = route(age=1); second.key = "new-route"
        second.evidence[0].raw[CHECKPOINT] = copy.deepcopy(checkpoint)
        await WorkEvidenceCache().attach(second, community)
        self.assertEqual(community.search.await_count, 1)
        self.assertTrue(verified_diffusion(second, second.evidence[-1]))
        self.assertEqual(assess(second)["decision"], "research")
        self.assertEqual(candidate_from_dict(second.to_dict()).to_dict(), second.to_dict())

    async def test_source_window_and_settings_changes_do_not_reuse(self):
        community = fake_community(); cache = WorkEvidenceCache(); first = route(age=1)
        await cache.attach(first, community)
        revised = copy.deepcopy(first); revised.evidence[0].source_revision = "changed-source"
        revised.evidence = revised.evidence[:1]
        await cache.attach(revised, community)
        self.assertEqual(community.search.await_count, 2)
        with research_window(NOW+timedelta(days=1)):
            await cache.attach(copy.deepcopy(first), community)
        self.assertEqual(community.search.await_count, 3)
        with patch.object(config, "TAVILY_SOCIAL_SEARCH_ENABLED", not config.TAVILY_SOCIAL_SEARCH_ENABLED):
            await cache.attach(copy.deepcopy(first), community)
        self.assertEqual(community.search.await_count, 4)
        changed_window = copy.deepcopy(first); changed_window.evidence[0].raw["discovery_lookback_days"] = 90
        await cache.attach(changed_window, community)
        self.assertEqual(community.search.await_count, 5)

    async def test_failure_cached_in_process_but_retried_on_restart(self):
        community = fake_community(error=ValueError("bad provider response")); cache = WorkEvidenceCache()
        first = route(age=1)
        await cache.attach(first, community)
        await cache.attach(first, community)
        self.assertEqual(community.search.await_count, 1)
        self.assertEqual(first.community_search_receipts["hackernews"]["status"], "failed")
        await WorkEvidenceCache().attach(first, community)
        self.assertEqual(community.search.await_count, 2)

    async def test_preflight_deadline_starts_no_query_and_is_not_zero_results(self):
        community = fake_community(); item = route(age=1)
        await WorkEvidenceCache().attach(item, community, deadline=time.monotonic()-1)
        community.search.assert_not_awaited()
        self.assertEqual(item.community_search_receipts["community_preflight"]["status"], "not_executed")

    async def test_changed_or_excluded_work_cannot_inherit_a_visibility_receipt(self):
        community = fake_community(); cache = WorkEvidenceCache(); first = route(age=1)
        await cache.attach(first, community)
        from paradigms.propagation import transfer_work_visibility
        for change in ("source", "exclude"):
            other = copy.deepcopy(first); other.key = "other"
            if change == "source": other.evidence[0].source_revision = "new-source"
            else: other.evidence = other.evidence[1:]
            self.assertFalse(transfer_work_visibility(first, other, copy.deepcopy(first.evidence[-1])))

    async def test_full_body_in_memory_replaces_scrubbed_peer_for_main_without_query(self):
        community = fake_community(); cache = WorkEvidenceCache(); first = route(age=1)
        await cache.attach(first, community)
        second = copy.deepcopy(first)
        scrub_ephemeral_evidence(second.evidence[-1])
        await cache.attach(second, community)
        self.assertEqual(community.search.await_count, 1)
        self.assertEqual(second.evidence[-1].summary, "Worth reading!")

    async def test_old_route_retains_its_existing_bound_attention_when_discovery_has_none(self):
        process = focus_process(); item = route(age=1)
        item.evidence.append(post(item)); certify_diffusion(item)
        scrub_ephemeral_evidence(item.evidence[-1])
        kept, held, failed = await process._focus_before_deep([item], [], time.monotonic()+10, {}, persist=False)
        self.assertEqual(kept, [item]); self.assertFalse(held); self.assertFalse(failed)

    async def test_old_route_retains_substantive_association_and_excluded_evidence_stays_excluded(self):
        process = focus_process(); item = route(age=1)
        review = analysis(item); item.evidence.append(review)
        self.assertTrue(certify_synthesis_discussion(item, review))
        kept, held, failed = await process._focus_before_deep([item], [], time.monotonic()+10, {}, persist=False)
        self.assertEqual(kept, [item]); self.assertFalse(held); self.assertFalse(failed)
        other = route(2, age=1); other.excluded_evidence.append(post(other))
        kept, held, failed = await process._focus_before_deep([other], [], time.monotonic()+10, {}, persist=False)
        self.assertFalse(kept); self.assertEqual(held, [other]); self.assertFalse(failed)

    async def test_one_assessment_error_is_pending_not_watch_and_other_candidate_survives(self):
        process = focus_process(); bad, good = route(1, age=1), route(2, age=1)
        good.evidence.append(post(good))
        async def prepare(candidate, deadline):
            if candidate.key == bad.key: raise ValueError("single malformed candidate")
        process.enricher.preflight_community = prepare
        kept, watched, failed = await process._focus_before_deep([bad, good], [], time.monotonic()+10, {}, persist=False)
        self.assertEqual(kept, [good]); self.assertFalse(watched); self.assertEqual(failed, [bad])
        self.assertNotIn("research_focus", bad.evidence[0].raw)

    async def test_actual_storage_failure_is_not_swallowed_as_optional_focus_failure(self):
        process = focus_process(); item = route(age=1); item.status = "pending_deep"
        process.store = SimpleNamespace(save_enrichment_checkpoint=lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("disk database failure")))
        with self.assertRaises(sqlite3.OperationalError):
            await process._focus_before_deep([item], [], time.monotonic()+10, {})

    async def test_timeouts_are_unknown_not_storage_failures_in_all_focus_paths(self):
        process = focus_process(); item = route(age=1)
        process.analyzer.prefetch_origin_eligibility = AsyncMock(side_effect=TimeoutError())
        selected = await process._select_origin_scope(item.evidence, time.monotonic()+10, {}, supporting=[post(item)])
        self.assertEqual(selected, item.evidence)
        other = route(2, age=1); other.evidence.append(post(other))
        process.enricher.preflight_community = AsyncMock(side_effect=TimeoutError())
        kept, watched, failed = await process._focus_before_deep([other], [], time.monotonic()+10, {}, persist=False)
        self.assertEqual(kept, [other]); self.assertFalse(watched); self.assertFalse(failed)
        self.assertEqual(other.community_search_receipts["community_preflight"]["status"], "failed")

    async def test_tampered_source_checkpoint_cannot_suppress_new_queries(self):
        community = fake_community(); item = route(age=1)
        await WorkEvidenceCache().attach(item, community)
        item.evidence = item.evidence[:1]
        item.evidence[0].raw[CHECKPOINT]["signature"] = "foreign source or stale query"
        await WorkEvidenceCache().attach(item, community)
        self.assertEqual(community.search.await_count, 2)

    async def test_partial_restart_retries_failed_free_provider_not_completed_paid_query(self):
        calls = []; base_client = httpx.AsyncClient
        def handle(request):
            calls.append(request)
            if request.url.host == "api.tavily.com":
                return httpx.Response(200, json={"results": [], "usage": {"credits": 1}, "request_id": "synthetic"})
            if sum(call.url.host == "hn.algolia.com" for call in calls) == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"hits": [{"objectID": "7002", "title": request.url.params["query"],
                "url": "https://arxiv.org/abs/2609.00001", "created_at": (NOW-timedelta(days=1)).isoformat(),
                "author": "Public Reader", "points": 30, "num_comments": 2}]})
        def factory(**kwargs): return base_client(transport=httpx.MockTransport(handle), **kwargs)
        item = route(age=1)
        with patch.object(config, "TAVILY_SOCIAL_SEARCH_ENABLED", True), patch.object(config, "TAVILY_API_KEY", "synthetic-offline-key"), \
             patch.object(config, "TAVILY_MONTHLY_CREDIT_LIMIT", 0), patch("httpx.AsyncClient", side_effect=factory):
            await WorkEvidenceCache().attach(item, CommunityEvidenceClient())
            self.assertEqual(item.community_search_receipts["hackernews"]["status"], "failed")
            # A new process sees only a persisted metadata checkpoint.
            item = candidate_from_dict(item.to_dict())
            await WorkEvidenceCache().attach(item, CommunityEvidenceClient())
        self.assertEqual(sum(call.url.host == "api.tavily.com" for call in calls), 1)
        self.assertEqual(sum(call.url.host == "hn.algolia.com" for call in calls), 2)
        self.assertEqual(item.community_search_receipts["hackernews"]["status"], "completed")

    async def test_preflight_query_scope_is_not_multiplied_by_fourfold_cheap_pool(self):
        process = focus_process()
        process.enricher.preflight_community = AsyncMock(return_value=[])
        stats = {}; origins = [route(index, age=1).evidence[0] for index in range(1, 13)]
        with patch.object(config, "PARADIGM_RESEARCH_ORIGIN_LIMIT", 2):
            selected = await process._select_origin_scope(origins, time.monotonic()+100, stats)
        self.assertFalse(selected)
        self.assertEqual(stats["origin_scope_qualification"]["pool"], 8)
        self.assertEqual(process.enricher.preflight_community.await_count, 2)

    async def test_hydrated_official_link_keeps_cached_counters_without_query_or_replication_claim(self):
        from paradigms.models import TechnicalEvidence
        from paradigms.enrichment import _official_repository_evidence
        async def search(candidate):
            for name in PROVIDERS: record_search(candidate, name, "disabled")
            record_search(candidate, "github", "completed", count=1)
            return [TechnicalEvidence(source="github", evidence_type=EvidenceType.IMPLEMENTATION,
                title="team/method", url="https://github.com/team/method", summary="A work-related implementation",
                identifiers={"github": "team/method"}, metrics={"stars": 321, "forks": 11},
                raw={"independence": "unverified"})]
        community = SimpleNamespace(search=AsyncMock(side_effect=search)); cache = WorkEvidenceCache()
        await cache.attach(route(age=1), community)
        later = route(age=1); later.evidence[0].raw["github_repositories"] = ["https://github.com/team/method"]
        later.evidence.append(_official_repository_evidence(later.evidence[0], "https://github.com/team/method"))
        await cache.attach(later, community)
        self.assertEqual(community.search.await_count, 1)
        self.assertEqual(later.evidence[-1].metrics["stars"], 321)
        self.assertEqual(later.evidence[-1].raw["independence"], "official")


class CombinedProductionTests(unittest.IsolatedAsyncioTestCase):
    process = fixture.V0DeliveryTests.process

    def setUp(self):
        fixture.V0DeliveryTests.setUp(self)
        for key in ("PARADIGM_RESEARCH_FOCUS_ENABLED", "PARADIGM_DIRECTION_SIGNALS_ENABLED"):
            patcher = patch.object(config, key, True); patcher.start(); self.addCleanup(patcher.stop)

    def network(self, *, fail_hn=False):
        calls = []
        base_client = httpx.AsyncClient
        def handle(request):
            calls.append(request)
            if request.url.host == "hn.algolia.com":
                return httpx.Response(503) if fail_hn else httpx.Response(200, json={"hits": [{
                    "objectID": "7001", "title": request.url.params["query"], "url": "https://arxiv.org/abs/2609.00001",
                    "created_at": (NOW-timedelta(days=1)).isoformat(), "author": "Actual Public Reader",
                    "points": 100, "num_comments": 30, "story_text": "Public body must not persist."}]})
            if request.url.host == "export.arxiv.org": return httpx.Response(200, text=directions.atom())
            if request.url.host == "boards-api.greenhouse.io": return httpx.Response(200, json=directions.jobs())
            return httpx.Response(200, text=directions.feed())
        def factory(**kwargs):
            return base_client(transport=httpx.MockTransport(handle), **kwargs)
        return factory, calls

    async def test_default_flags_query_before_extraction_and_main_reuses_it(self):
        item = route(age=1); item.evidence[0].title = "Efficient Diffusion Language Models"
        process = self.process(fixture.batch(item.evidence), accepted=True); process.delivery_contract_version = 5
        factory, calls = self.network()
        process.enricher.direction_signals = DirectionSignalClient(client_factory=factory, registry=directions.registry())
        process.enricher.preflight_community = EvidenceEnricher.preflight_community.__get__(process.enricher)
        process.enricher.secondary_pages = SimpleNamespace(read=AsyncMock())
        original_analyze = process.analyzer.run
        async def analyze(items):
            self.assertTrue(any(call.url.host == "hn.algolia.com" for call in calls), "query must precede full extraction")
            return await original_analyze(items)
        process.analyzer.run = analyze
        original_enrich = process.enricher.run
        async def enrich(items, supporting):
            for candidate in items:
                await process.enricher.preflight_community(candidate)
            return await original_enrich(items, supporting)
        process.enricher.run = enrich
        with patch("httpx.AsyncClient", side_effect=factory):
            result = await process.run(reference_time=NOW)
        self.assertEqual(len(process.pending_delivery), 1, result)
        self.assertEqual(sum(call.url.host == "hn.algolia.com" for call in calls), 1)
        self.assertEqual(result["research_focus"]["research"], 1)
        self.assertIn(CHECKPOINT, process.pending_delivery[0].evidence[0].raw)

    async def test_default_flags_query_failure_unknown_still_delivers_weekly_receipt(self):
        process = self.process(fixture.batch(route(age=1).evidence), accepted=True); process.delivery_contract_version = 5
        factory, calls = self.network(fail_hn=True)
        process.enricher.direction_signals = DirectionSignalClient(client_factory=factory, registry=directions.registry())
        process.enricher.preflight_community = EvidenceEnricher.preflight_community.__get__(process.enricher)
        process.enricher.secondary_pages = SimpleNamespace(read=AsyncMock())
        with patch("httpx.AsyncClient", side_effect=factory):
            result = await process.run(reference_time=NOW)
        self.assertTrue(result["delivery_ready"], result)
        self.assertFalse(process.pending_delivery)
        saved = self.store.load_pending_origins()[0]
        self.assertEqual(saved.raw[CHECKPOINT]["receipts"]["hackernews"]["status"], "failed")
        self.assertNotEqual(saved.raw["research_focus"]["decision"], "reject")

    async def test_one_focus_cas_conflict_does_not_stop_healthy_main_or_overwrite_newer_snapshot(self):
        pending = route(1, age=1); pending.status = "pending_deep"
        pending.evidence, _, _ = self.store.observe_origins(pending.evidence)
        self.store.save_candidates([pending])
        healthy = route(2, age=1)
        batch = fixture.batch(healthy.evidence); batch.supporting = [post(healthy)]
        process = self.process(batch, accepted=True); process.delivery_contract_version = 5
        process.enricher.direction_signals = SimpleNamespace(attach=AsyncMock())
        original = self.store.save_enrichment_checkpoint
        newer = []
        def conflict(before, after):
            if before.key == pending.key:
                changed = copy.deepcopy(before); changed.admission_reason = "Newer foreign snapshot must survive"
                self.store.save_candidates([changed]); newer.append(changed.to_dict())
                raise ValueError("增强检查点与当前候选或一手版本冲突：candidate_snapshot_changed")
            return original(before, after)
        self.store.save_enrichment_checkpoint = conflict
        result = await process.run(reference_time=NOW)
        self.assertFalse(result.get("research_stage_interrupted"), result)
        self.assertEqual(len(process.pending_delivery), 1, result)
        self.assertEqual(result["candidate_focus_failure_count"], 1, result)
        self.assertEqual(result["delivery_scope"]["deep_failed"], 1, result)
        self.assertEqual(self.store.load_candidate_snapshots([pending.key])[0].to_dict(), newer[-1])
