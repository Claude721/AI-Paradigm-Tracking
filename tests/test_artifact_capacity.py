"""General regressions derived from run 37899070924; no real DB/API/clock samples."""
import asyncio
import copy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from database.paradigm_store import _deep_checkpoint_input_signature
from paradigms.analyzer import ParadigmAnalyzer, _bounded_synthesis_evidence
from paradigms.models import ParadigmCandidate
from run_audit import RunAudit
from runtime_clock import research_window
from tests import test_research_service as service_fixtures

origin = service_fixtures.origin


class CapacityDependencyTests(unittest.IsolatedAsyncioTestCase):
    setUp = service_fixtures.ResearchServiceTests.setUp
    analyze = service_fixtures.ResearchServiceTests.analyze
    enrich = service_fixtures.ResearchServiceTests.enrich
    synthesize = service_fixtures.ResearchServiceTests.synthesize
    trajectory = service_fixtures.ResearchServiceTests.trajectory
    service = service_fixtures.ResearchServiceTests.service

    async def test_stale_refresh_redirects_without_paid_calls_and_preserves_parent(self):
        old = self.store.observe_origins([origin(1)])[0][0]
        historical = ParadigmCandidate(key='history', name='历史路线', thesis='', problem_shift='', mechanism='', status='observe', evidence=[old])
        self.store.save_candidates([historical])
        revised = copy.deepcopy(old)
        revised.summary = 'The primary material changed.'
        revised = self.store.observe_origins([revised])[0][0]
        with research_window(datetime(2026, 10, 8, tzinfo=timezone.utc)):
            campaign = self.store.campaigns.begin(reference_time=datetime(2026,10,8,tzinfo=timezone.utc), ordinary_days=7, high_signal_days=30, bootstrap=False, seeds=[])
            self.orchestrator.campaign = campaign
            self.orchestrator.enricher.refresh = AsyncMock(return_value=[])
            result = await self.service([])
            ledger = self.store.campaigns.reconcile(campaign.campaign_id)
            self.assertEqual(result['refresh_input_redirected_count'], 1)
            self.assertEqual(len(result['input_deferred_candidates']), 1)
            self.assertEqual((ledger['refresh_planned'], ledger['refresh_pending'], ledger['deep_pending']), (1,1,1))
            self.orchestrator.enricher.refresh.assert_not_awaited()
            self.assertEqual(self.events, [])
            # Closing the revised origin wakes a real hypothesis, never a fake
            # empty result. Only a downstream completed verdict closes parent.
            self.store.mark_evidence([revised], analyzed=True)
            rebased = self.store.rebase_analyzed_candidate_inputs('history')
            self.assertIsNotNone(rebased)
            self.assertEqual(rebased.evidence[0].source_revision, revised.source_revision)
            self.store.campaigns.include(campaign.campaign_id, 'deep', [(rebased.key, _deep_checkpoint_input_signature(rebased), rebased.to_dict())])
            rebased.status = 'observe'
            self.store.save_candidates([rebased])
            closed = self.store.campaigns.reconcile(campaign.campaign_id)
            self.assertEqual(closed['pending_total_count'], 0)
            self.assertEqual(closed['refresh_completed'], 1)

    async def test_interleaved_protected_origins_do_not_fragment_eligibility_calls(self):
        items = [origin(index) for index in range(55)]
        for index,item in enumerate(items):
            if index % 4 == 0:
                item.raw['origin_priority'] = 2
        items = self.store.observe_origins(items)[0]
        calls = []
        async def prefetch(group):
            calls.append([item.fingerprint for item in group])
            return {item.fingerprint: ('full_review', '需完整机制判断') for item in group}
        self.orchestrator.analyzer = SimpleNamespace(run=self.analyze, enable_batch_prefilter=True, stage_cache=object(), prefetch_origin_eligibility=prefetch)
        result = await self.service(items, seconds=1000)
        self.assertEqual([len(group) for group in calls], [24,17])
        self.assertEqual(len(result['origin_result'][6]), 55)
        self.assertEqual([event[1] for event in self.events if event[0]=='origin'], [item.title for item in items])

    async def test_bad_prefetched_identity_is_not_paid_for_twice_in_same_visit(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"decisions":[]}'))], usage=None)
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        analyzer = ParadigmAnalyzer(client=client, model='offline', stage_cache=self.store.campaigns)
        items = self.store.observe_origins([origin(1)])[0]
        self.assertEqual(await analyzer.prefetch_origin_eligibility(items), {})
        self.assertEqual(await analyzer._screen_origin_batch(items), {})
        self.assertEqual(create.await_count, 1)
        analyzer._eligibility_prefetch_failures.clear()  # next campaign process may retry
        self.assertEqual(await analyzer._screen_origin_batch(items), {})
        self.assertEqual(create.await_count, 2)

    def test_evidence_compaction_retains_identity_metrics_and_zeroes(self):
        item = origin(1)
        item.metrics = {'stars':0,'verified':False}
        result = _bounded_synthesis_evidence([item])
        record = result['records'][0]
        self.assertNotIn('document_excerpt', record)
        self.assertNotIn('author_roles', record)
        self.assertEqual(record['index'], 0)
        self.assertEqual(record['url'], item.url)
        self.assertEqual(record['metrics'], item.metrics)
        self.assertEqual(result['overflow']['count'], 0)


class RequestAccountingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.audit = RunAudit()

    async def test_cancelled_request_is_unknown_usage_and_cancellation_propagates(self):
        started = asyncio.Event()
        async def call(**kwargs):
            started.set()
            await asyncio.Event().wait()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call)))
        task = asyncio.create_task(self.audit.chat_completion(client, stage='origin_eligibility', role='sub', subject='batch', model='offline'))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.audit.token_totals()['llm_unreported_usage_count'], 1)
        self.assertEqual(self.audit.token_totals()['llm_call_count'], 1)
        self.assertIn('usage_unknown', self.audit.llm_calls[0]['error'])
        self.assertIsNotNone(self.audit.llm_calls[0]['elapsed_seconds'])

    async def test_success_is_recorded_once_by_validator_with_exact_sdk_parameters(self):
        response = SimpleNamespace(usage=SimpleNamespace(total_tokens=9,prompt_tokens=5,completion_tokens=4), choices=[])
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        params = {'model':'offline', 'messages':[{'role':'user','content':'sensitive-private-prompt'}], 'max_tokens':100}
        with patch('run_audit.time.monotonic', side_effect=[10,12]):
            result = await self.audit.chat_completion(client, stage='synthesis', role='main', subject='route', **params)
        self.assertEqual(self.audit.llm_calls, [])
        self.audit.record_llm(stage='synthesis',role='main',model='offline',subject='route',response=result)
        create.assert_awaited_once_with(**params)
        self.assertEqual(self.audit.token_totals()['llm_call_count'], 1)
        self.assertEqual(self.audit.llm_calls[0]['elapsed_seconds'], 2)
        self.assertNotIn('sensitive-private-prompt', str(self.audit.llm_calls))

    async def test_ordinary_failure_keeps_single_record_and_request_timing(self):
        error = RuntimeError('service unavailable')
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=error))))
        with patch('run_audit.time.monotonic', side_effect=[10,13]):
            with self.assertRaises(RuntimeError):
                await self.audit.chat_completion(client, stage='synthesis', role='main', subject='route', model='offline')
        self.audit.record_llm(stage='synthesis',role='main',model='offline',subject='route',error=error)
        self.assertEqual(self.audit.token_totals()['llm_call_count'], 1)
        self.assertEqual(self.audit.llm_calls[0]['elapsed_seconds'], 3)
