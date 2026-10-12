"""Cloud source audit regressions; no real credentials, providers or mail."""
import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import config
from source_audit import Entry, SourceAudit, diagnostic_rows, build_entries, diagnose_entry
from sources.openreview_source import OpenReviewSource
from sources.request_policy import retry_after_seconds
from sources.priority_research_source import _discover_bibliographic_publications, _discover_modelbest_catalog
from runtime_clock import research_window

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
RSS = '<rss><channel/></rss>'


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay
        await asyncio.sleep(0)


def nuxt_page(cards, *, more=False):
    heap = [["ShallowReactive", 1], None]

    def add(value):
        index = len(heap)
        heap.append(None)
        if isinstance(value, dict):
            value = {key: add(item) for key, item in value.items()}
        elif isinstance(value, list):
            value = [add(item) for item in value]
        heap[index] = value
        return index

    catalog = {f"news-cate-{category}-1-30": {"code": 1, "data": {"list": cards if category == 7 else [], "hasMore": more if category == 7 else False}}
               for category in range(7, 14)}
    catalog["home-blog-model-data"] = {"link": "https://unrelated.example/product"}
    heap[1] = {"data": add(catalog)}
    return '<script type="application/json" id="__NUXT_DATA__">'+json.dumps(heap)+'</script>'


class PolicyTests(unittest.TestCase):
    def test_retry_after_delta_date_and_invalid_values(self):
        self.assertEqual(retry_after_seconds('37'), 37)
        self.assertEqual(retry_after_seconds('Sat, 10 Oct 2026 00:00:37 GMT', now=NOW), 37)
        self.assertEqual(retry_after_seconds('Sat, 10 Oct 2026 00:00:00 GMT', now=NOW + timedelta(seconds=1)), 0)
        for value in ('', 'nan', 'inf', 'unknown'):
            self.assertIsNone(retry_after_seconds(value))
        self.assertEqual(retry_after_seconds('120'), 120)  # never truncate a long wait

    def test_workflow_exports_controls_and_defaults_x_off(self):
        workflow = Path('.github/workflows/weekly-radar.yml').read_text()
        self.assertIn("KOL_X_SOURCE_ENABLED: ${{ vars.KOL_X_SOURCE_ENABLED || 'false' }}", workflow)
        self.assertIn("TAVILY_SOCIAL_SEARCH_ENABLED: ${{ vars.TAVILY_SOCIAL_SEARCH_ENABLED || 'true' }}", workflow)
        self.assertIn('TAVILY_SOCIAL_SEARCH_DOMAINS: ${{ vars.TAVILY_SOCIAL_SEARCH_DOMAINS', workflow)

    def test_disabled_x_remains_in_manifest_not_passed_or_missing(self):
        with patch.object(config, 'KOL_X_SOURCE_ENABLED', False), patch.object(config, 'TWITTER_BEARER_TOKEN', ''):
            entries = [entry for entry in build_entries() if entry.key == 'x']
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0].enabled)

    def test_200_small_noncontent_is_not_certain_parser_bug(self):
        row = {'status':'failed', 'coverage':{'status':'parse_zero_links'},
               'http':[{'status':200, 'bytes':45}], 'index_diagnostics':{'anchors':0, 'scripts':[]}}
        self.assertEqual(diagnose_entry(row)['category'], 'noncontent_index_response')

    def test_diagnostics_remove_query_credentials_and_exception_bodies(self):
        result = {'entries':[{'key':'bad', 'url':'https://lab.example/index?api_key=private', 'status':'failed',
                              'reason':'http_403', 'http':[{'status':403}], 'exception':'sensitive-body'}]}
        rendered = json.dumps(list(diagnostic_rows(result)))
        self.assertNotIn('private', rendered)
        self.assertNotIn('sensitive-body', rendered)
        self.assertIn('403', rendered)

    def test_cli_failure_prints_per_entry_and_publishes_summary(self):
        from scripts.source_audit import main
        row = {'key':'x', 'url':'https://api.x.com/2/tweets/search/recent', 'status':'not_configured',
               'reason':'credential_not_configured', 'credential_name':'TWITTER_BEARER_TOKEN',
               'missing_credentials':['TWITTER_BEARER_TOKEN'], 'parsed':0, 'persisted':0, 'http':[]}
        result = {'entries':[row], 'reference_time':NOW.isoformat(), 'boundary':'fixture',
                  'planned_entries':1, 'returned_entries':1, 'counts':{'not_configured':1},
                  'request_count':0, 'elapsed_seconds':0, 'acceptance_complete':False}
        with tempfile.TemporaryDirectory() as directory:
            output, summary = Path(directory)/'output', Path(directory)/'summary.md'
            stream = io.StringIO()
            with patch('scripts.source_audit.configuration_syntax_checks', return_value=[]), \
                 patch('scripts.source_audit.build_entries', return_value=[]), \
                 patch('scripts.source_audit.SourceAudit') as audit, \
                 patch.dict(os.environ, {'GITHUB_STEP_SUMMARY':str(summary)}), \
                 patch('sys.argv', ['source_audit.py','--output-dir',str(output)]), redirect_stdout(stream):
                audit.return_value.run = AsyncMock(return_value=result)
                self.assertEqual(main(), 1)
            self.assertIn('TWITTER_BEARER_TOKEN', stream.getvalue())
            self.assertIn('credential_missing_here', summary.read_text())
            self.assertTrue((output/'source_audit_latest.json').exists())

    def test_bibliography_resolves_explicit_doi_and_tracks_pending_identity(self):
        text = '<p>A. Chen. (2026), “Known primary work”, doi:10.1234/mechanism</p><p>B. Lee. (2026), “Unknown primary work”, Journal</p>'
        first, second = {}, {}
        links, count = _discover_bibliographic_publications(text, cutoff=NOW, diagnostics=first)
        _discover_bibliographic_publications(text, cutoff=NOW, diagnostics=second)
        self.assertEqual([(link.url, link.published_at) for link in links], [('https://doi.org/10.1234/mechanism','')])
        self.assertEqual(count, 1)
        self.assertEqual(first['pending_records'], second['pending_records'])
        self.assertEqual(first['pending_records'][0]['date_basis'], 'year_only_not_publication_date')

    def test_nuxt_catalog_uses_explicit_url_publish_time_not_product_data(self):
        page = nuxt_page([{'id':1, 'title':'New mechanism', 'link':'https://modelbest.cn/news/work',
                           'publishTime':'2026-10-08T00:00:00Z', 'updateTime':'2026-10-09',
                           'thumbnail':'https://cdn.example/image?Signature=private'}])
        links, diag = _discover_modelbest_catalog(page, cutoff=NOW-timedelta(days=7))
        self.assertEqual([(link.title,link.url,link.published_at) for link in links],
                         [('New mechanism','https://modelbest.cn/news/work','2026-10-08T00:00:00+00:00')])
        self.assertEqual(diag['resolved_links'], 1)
        self.assertEqual(diag['pending_records'], [])

    def test_nuxt_old_publication_modified_recently_is_not_new(self):
        links, diag = _discover_modelbest_catalog(nuxt_page([{'id':1, 'title':'Old work', 'link':'',
                                                             'publishTime':'2024-10-08', 'updateTime':'2026-10-09'}]), cutoff=NOW-timedelta(days=7))
        self.assertEqual(links, [])
        self.assertEqual(diag['outside_window'], 1)
        self.assertEqual(diag['pending_records'], [])

    def test_nuxt_missing_or_temporary_url_and_pagination_are_pending(self):
        for link in ('', 'javascript:void(0)', 'https://cdn.example/file?X-Amz-Signature=private'):
            links, diag = _discover_modelbest_catalog(nuxt_page([{'id':1, 'title':'Unknown primary', 'link':link,
                                                                 'publishTime':'0001-01-01T00:00:00Z', 'createTime':'2024-10-08'}], more=True), cutoff=NOW)
            self.assertEqual(links, [])
            self.assertEqual(len(diag['pending_records']), 1)
            self.assertEqual(diag['pagination_pending'], 1)

    def test_nuxt_corrupt_heap_is_rejected_not_executed(self):
        for heap in ([{'data':0}], [['ShallowReactive',0]], [{'data':1}, {'news-cate-7-1-30':True}]):
            with self.assertRaises(ValueError):
                _discover_modelbest_catalog('<script id="__NUXT_DATA__">'+json.dumps(heap)+'</script>')


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def audit(self, handler, *, peers=True, **kwargs):
        clock = Clock()
        entries = [Entry('first','openreview','https://api2.openreview.net/notes/search',{'term':'one'})]
        if peers:
            entries += [Entry('second','openreview','https://api2.openreview.net/notes/search',{'term':'two'}),
                        Entry('healthy','feed','https://healthy.example/rss')]
        result = await SourceAudit(transport=httpx.MockTransport(lambda request:handler(request, clock)),
                                   clock=clock, sleeper=clock.sleep, **kwargs).run(entries, reference_time=NOW)
        return result, clock

    async def test_audit_37_second_recovery_and_peers_keep_failure_ledger(self):
        observed = []
        def handler(request, clock):
            observed.append((request.url.host,clock()))
            if request.url.host != 'api2.openreview.net':
                return httpx.Response(200,text=RSS,request=request)
            return (httpx.Response(429,headers={'Retry-After':'37'},request=request) if len([x for x in observed if x[0]=='api2.openreview.net'])==1
                    else httpx.Response(200,json={'notes':[],'count':0},request=request))
        result, clock = await self.audit(handler)
        self.assertEqual(result['counts'], {'passed':3})
        self.assertEqual(result['request_count'], 4)
        self.assertEqual([event['status'] for event in result['entries'][0]['http']], [429,200])
        self.assertTrue(result['entries'][0]['rate_limit_recovered'])
        times = [time for host,time in observed if host=='api2.openreview.net']
        self.assertGreaterEqual(times[1]-times[0], 37)
        self.assertTrue(any(host=='healthy.example' and time < times[1] for host,time in observed))

    async def test_audit_persistent_rate_limit_stops_after_one_recovery(self):
        result, clock = await self.audit(lambda request,clock:httpx.Response(429,headers={'Retry-After':'37'},request=request)
                                         if request.url.host=='api2.openreview.net' else httpx.Response(200,text=RSS,request=request))
        self.assertEqual(sum(row['requests'] for row in result['entries']), result['request_count'])
        self.assertEqual(result['request_count'], 3)
        self.assertFalse(result['acceptance_complete'])
        self.assertEqual(result['entries'][2]['status'], 'passed')

    async def test_audit_long_wait_or_challenge_never_retries(self):
        for headers in ({'Retry-After':'120'}, {'Retry-After':'37','cf-mitigated':'challenge'}):
            result, clock = await self.audit(lambda request,clock:httpx.Response(429,headers=headers,request=request)
                                             if request.url.host=='api2.openreview.net' else httpx.Response(200,text=RSS,request=request))
            self.assertEqual(result['counts'], {'failed':1,'not_executed':1,'passed':1})
            self.assertEqual(result['request_count'], 2)

    async def test_attempted_retry_exhausting_budget_is_not_never_executed(self):
        result, _ = await self.audit(lambda request,clock:httpx.Response(429,headers={'Retry-After':'37'},request=request), peers=False, max_requests=1)
        self.assertEqual(result['counts'], {'failed':1})
        self.assertEqual(result['request_count'], 1)
        self.assertEqual(result['entries'][0]['http'][0]['status'], 429)

    async def test_200_after_retry_must_still_pass_response_contract(self):
        count = 0
        def handler(request, clock):
            nonlocal count
            count += 1
            return httpx.Response(429,headers={'Retry-After':'1'},request=request) if count==1 else httpx.Response(200,json={'error':'invalid body'},request=request)
        result, _ = await self.audit(handler, peers=False)
        self.assertEqual(result['counts'], {'failed':1})

    async def test_production_queries_share_cooldown(self):
        clock, calls = Clock(), []
        def handler(request):
            calls.append(clock())
            return httpx.Response(429,headers={'Retry-After':'37'},request=request) if len(calls)==1 else httpx.Response(200,json={'notes':[],'count':0},request=request)
        source = OpenReviewSource(venues=['ICLR.cc/2026/Conference'],searches=['one','two'],concurrency=2,clock=clock,sleeper=clock.sleep)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with research_window(NOW), patch('sources.openreview_source.httpx.AsyncClient', return_value=client):
            await source.fetch()
        self.assertGreaterEqual(calls[1]-calls[0], 37)
        self.assertEqual(source.completed_queries, 2)
        self.assertEqual(source.rate_limited_requests, 1)

    async def test_production_open_circuit_blocks_waiting_peer(self):
        clock, calls = Clock(), []
        def handler(request):
            calls.append(clock())
            return httpx.Response(429,headers={'Retry-After':'120'},request=request)
        source = OpenReviewSource(venues=['ICLR.cc/2026/Conference'],searches=['one','two'],concurrency=2,clock=clock,sleeper=clock.sleep)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with research_window(NOW), patch('sources.openreview_source.httpx.AsyncClient', return_value=client):
            await source.fetch()
        self.assertEqual(len(calls), 1)
        self.assertEqual(source.failed_queries, 1)
        self.assertEqual(source.not_executed_queries, 1)

    async def test_production_challenge_is_not_an_ordinary_retryable_429(self):
        clock = Clock()
        source = OpenReviewSource(clock=clock,sleeper=clock.sleep)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(429,headers={'Retry-After':'37','cf-mitigated':'challenge'},request=request))) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await source._get_with_backoff(client, {'term':'one'})
        self.assertEqual(source.request_count, 1)
        self.assertTrue(source._circuit_open)
        self.assertFalse(any(wait >= 37 for wait in clock.waits))

    async def test_x_off_blocks_both_origin_and_community_calls_with_token_present(self):
        from sources.curated_intelligence_source import CuratedKOLXSource
        from sources.paradigm_evidence_source import CommunityEvidenceClient
        from sources.twitter_source import TwitterSource
        from paradigms.models import ParadigmCandidate
        client = AsyncMock()
        candidate = ParadigmCandidate(key='fixture',name='fixture',thesis='fixture',problem_shift='fixture',mechanism='fixture')
        with patch.object(config,'KOL_X_SOURCE_ENABLED',False), patch.object(config,'TWITTER_BEARER_TOKEN','private'):
            self.assertEqual(await CuratedKOLXSource().fetch(), [])
            self.assertEqual(await CommunityEvidenceClient()._x_title_search(client,candidate), [])
            self.assertEqual(await TwitterSource().fetch(), [])
        client.get.assert_not_awaited()
        self.assertIn('未执行',candidate.community_coverage['x_official'])

    async def test_smoke_x_off_does_not_probe_even_with_token_present(self):
        from smokecheck import run_smoke_checks, SmokeResult
        with patch.object(config,'KOL_X_SOURCE_ENABLED',False), patch.object(config,'TWITTER_BEARER_TOKEN','private'), \
             patch('smokecheck._check',new=AsyncMock(return_value=SmokeResult('fixture','passed'))) as check:
            results = await run_smoke_checks(include_llm=False,include_smtp=False,include_tavily=False,write_json=False)
        self.assertFalse(any(call.args[0]=='X Recent Search' for call in check.await_args_list))
        x = next(result for result in results if result.name=='X Recent Search')
        self.assertEqual(x.status, 'skipped')

    async def test_qwen_migration_keeps_original_manifest_identity(self):
        calls=[]
        def handler(request):
            calls.append(str(request.url))
            if request.url.path == '/api/page_config':
                return httpx.Response(200, json=[], request=request)
            if request.url.path == '/api/v2/article/retrieval':
                return httpx.Response(200, json={'success': True, 'data': {'articles': []}}, request=request)
            text = ('<a href="/research/mechanism">Adaptive world model</a>' if request.url.path=='/research'
                    else '<title>Adaptive world model</title><main>'+('Predictive mechanism with controlled experiments. '*10)+'</main>')
            return httpx.Response(200,text=text,request=request)
        result = await SourceAudit(transport=httpx.MockTransport(handler)).run([Entry('qwen','official','https://qwenlm.github.io/publication/')], reference_time=NOW)
        self.assertEqual(result['counts'], {'passed':1})
        self.assertEqual(result['entries'][0]['url'], 'https://qwenlm.github.io/publication/')
        self.assertEqual(calls[0], 'https://qwen.ai/research')

    async def test_modelbest_truncated_catalog_preserves_healthy_peer(self):
        def handler(request):
            if request.url.host=='healthy.example':
                return httpx.Response(200,text=RSS,request=request)
            if request.url.path=='/':
                text = nuxt_page([{'id':1, 'title':'Adaptive predictive mechanism','link':'https://modelbest.cn/news/work', 'publishTime':'2026-10-08'}], more=True)
            else:
                text = '<title>Adaptive predictive mechanism</title><main>'+('Mechanism and experimental evidence. '*10)+'</main>'
            return httpx.Response(200,text=text,request=request)
        result = await SourceAudit(transport=httpx.MockTransport(handler)).run([Entry('modelbest','official','https://modelbest.cn/'),Entry('good','feed','https://healthy.example/rss')], reference_time=NOW)
        self.assertEqual(result['counts'], {'failed':1,'passed':1})
        self.assertEqual(result['entries'][0]['coverage']['catalog_diagnostics']['pagination_pending'], 1)

    async def test_production_cancelled_wait_preserves_shared_deadline(self):
        clock, calls = Clock(), []
        waiting = asyncio.Event()
        async def sleep(delay):
            if delay >= 37 and not waiting.is_set():
                waiting.set()
                await asyncio.Event().wait()
            await clock.sleep(delay)
        def handler(request):
            calls.append(clock())
            return httpx.Response(429,headers={'Retry-After':'37'},request=request) if len(calls)==1 else httpx.Response(200,json={'notes':[],'count':0},request=request)
        source = OpenReviewSource(clock=clock,sleeper=sleep)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(source._get_with_backoff(client, {'term':'one'}))
            await asyncio.wait_for(waiting.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await source._get_with_backoff(client, {'term':'two'})
        self.assertGreaterEqual(calls[1]-calls[0], 37)

    async def test_modelbest_complete_mock_catalog_uses_detail_and_sqlite(self):
        def handler(request):
            text = (nuxt_page([{'id':1, 'title':'Adaptive predictive mechanism','link':'https://modelbest.cn/news/work', 'publishTime':'2026-10-08'}]) if request.url.path=='/'
                    else '<title>Adaptive predictive mechanism</title><main>'+('Mechanism and experimental evidence. '*10)+'</main>')
            return httpx.Response(200,text=text,request=request)
        result = await SourceAudit(transport=httpx.MockTransport(handler)).run([Entry('modelbest','official','https://modelbest.cn/')], reference_time=NOW)
        self.assertEqual(result['counts'], {'passed':1})
        self.assertEqual(result['entries'][0]['persisted'], 1)

    async def test_official_coverage_locator_keeps_query_secrets_out_of_audit(self):
        def handler(request):
            text = ('<a href="/research/work">Adaptive world model</a>' if request.url.path=='/research'
                    else '<title>Adaptive world model</title><main>'+('Mechanism and experimental evidence. '*10)+'</main>')
            return httpx.Response(200,text=text,request=request)
        result = await SourceAudit(transport=httpx.MockTransport(handler)).run([Entry('safe','official','https://lab.example/research?api_key=private-query')],reference_time=NOW)
        self.assertEqual(result['counts'], {'passed':1})
        self.assertNotIn('private-query', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
