"""Finite official catalogue recovery: no network, secrets or model calls."""
import asyncio
import copy
import json
import unittest
from datetime import datetime, timezone
from html import escape
from unittest.mock import AsyncMock, patch

import httpx
from runtime_clock import research_window
from sources.meta_catalog import CATALOG_URL, decode_page, fetch_catalog
from sources.priority_research_source import PriorityResearchPageSource, _discover_bibliographic_publications
from sources.bibliography_resolver import BibliographyResolver

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
ENTRY = "https://ai.meta.com/research/publications/"
NEXT = "https://ai.meta.com/results/?page=2&content_types%5B0%5D=publication"


def card(slug="adaptive-memory", date="October 10, 2026", title="Adaptive neural memory"):
    return ('<div data-ms="'+escape(json.dumps({"creative": "listview-card"}), quote=True)+'">'
        '<h4 class="_8w6a _8w60">NLP</h4><h4 class="_8w6a _8w61">'+escape(title)+'</h4>'
        '<p class="_8wl0">'+date+'</p><p class="_8wl0"></p>'
        '<a href="/research/publications/'+slug+'/">Read the Paper</a></div>')


def page(*cards, next_url=""):
    return '<h1>Publications</h1>'+''.join(cards)+(f'<a rel="next" href="{escape(next_url,quote=True)}">Next</a>' if next_url else '')


def empty_tail(number=2, **overrides):
    props = {'resultsAreaId':'fb_ai_results_grid','messageAreaId':'fb_ai_results_header_id',
        'contentType':'PUBLICATION','page':number-1,'limit':12,'offset':0,'searchQuery':'',
        'filterId':None,'researchAreas':None,'filterTags':None,'years':None,'locations':None,
        'sortBy':None,'pinnedPosts':[],'blogPostsToExclude':[],'unfilteredResultsHits':2939,**overrides}
    return ('<h3 class="_8w6a _8w6d">Sorry, no results matched your search criteria. '
        'Please try adjusting your search criteria.</h3><script>window.data='+json.dumps({'props':props})+';</script>')


class Cache:
    def __init__(self): self.rows = {}; self.fail = False
    def cache_get(self, stage, key): return copy.deepcopy(self.rows.get((stage,key)))
    def cache_save(self, stage, key, value):
        if self.fail: raise OSError("offline checkpoint failure")
        self.rows[(stage,key)] = copy.deepcopy(value)


class CardTests(unittest.TestCase):
    def test_actual_empty_tail_requires_exact_publication_query_identity(self):
        value=decode_page(empty_tail(),NEXT,page=2)
        self.assertEqual(value['rows'], []); self.assertEqual(value['next_url'], '')
        self.assertEqual(value['empty_query_receipt']['unfilteredResultsHits'],2939)
        for overrides in ({'page':0},{'contentType':'BLOG'},{'years':[2026]},{'searchQuery':'something'},
                          {'limit':True},{'unfilteredResultsHits':'2939'}):
            with self.assertRaises(ValueError):decode_page(empty_tail(**overrides),NEXT,page=2)

    def test_first_empty_or_untyped_ambiguous_message_is_not_successful_zero(self):
        for value,url,number in ((empty_tail(1),CATALOG_URL,1), (empty_tail()+'<script>window.other='+json.dumps({'props':{'resultsAreaId':'fb_ai_results_grid'}})+'</script>',NEXT,2),
            (empty_tail().split('<script>')[0],NEXT,2), (empty_tail().replace('"props"', '"notProps"'),NEXT,2)):
            with self.assertRaises(ValueError):decode_page(value,url,page=number)
    def test_bound_title_date_url_and_ignore_category_empty_mobile_date(self):
        value = decode_page(page(card(), next_url=NEXT), CATALOG_URL, page=1)
        self.assertEqual(value['rows'], [{'title':'Adaptive neural memory',
            'url':'https://ai.meta.com/research/publications/adaptive-memory/', 'published_at':'2026-10-10'}])
        self.assertEqual(value['next_url'], NEXT)

    def test_no_execution_or_generic_anchors_as_catalogue(self):
        for value in ('<script>executeAnything()</script>', '<h1>Publications</h1><a href="/research/publications/adaptive-memory/">Paper</a>'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_page(value, CATALOG_URL, page=1)

    def test_missing_or_ambiguous_card_field_fails_closed(self):
        values = [card(date='2026'), card(date='February 31, 2026'), card(title=''),
            card().replace('</div>', '<p class="_8wl0">October 09, 2026</p></div>'),
            card().replace('</div>', '<h4 class="_8w6a _8w61">Another title</h4></div>'),
            card().replace('</div>', '<a href="/research/publications/another/">Paper</a></div>'),
            card().replace('href="/research/', 'href="https://evil.example/research/')]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_page(page(value), CATALOG_URL, page=1)

    def test_next_page_cannot_skip_repeat_filter_or_leave_origin(self):
        for url in (CATALOG_URL, NEXT.replace('page=2','page=3'), NEXT+'&research_area=vision',
                    NEXT.replace('ai.meta.com','evil.example'), NEXT+'&page=2', NEXT+'#fragment'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                decode_page(page(card(),next_url=url), CATALOG_URL,page=1)

    def test_duplicate_cards_keep_receipts_but_multiple_next_links_fail(self):
        self.assertEqual(len(decode_page(page(card(), card()), CATALOG_URL, page=1)['rows']), 2)
        with self.assertRaises(ValueError):
            decode_page(page(card(),next_url=NEXT)+'<a rel="next" href="'+NEXT+'">Next</a>',CATALOG_URL,page=1)

    def test_explicit_legacy_slug_case_is_preserved_not_normalized(self):
        value=decode_page(page(card('From-Patches-to-Pictures-PaQ-2-PiQ')),CATALOG_URL,page=1)
        self.assertTrue(value['rows'][0]['url'].endswith('/From-Patches-to-Pictures-PaQ-2-PiQ/'))
        value = decode_page(page(card('wav2vec-2.0')), CATALOG_URL, page=1)
        self.assertTrue(value['rows'][0]['url'].endswith('/wav2vec-2.0/'))


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, handler, cache=None, **kwargs):
        async def get(url): return httpx.Response(200,text=handler(url),request=httpx.Request('GET',url))
        with research_window(NOW):
            return await fetch_catalog(get,index_url=ENTRY,lookback_days=7,stage_cache=cache,sleeper=AsyncMock(),**kwargs)

    async def test_old_cards_and_old_first_page_never_hide_later_new_work(self):
        calls=[]
        def handler(url):
            calls.append(url)
            return page(card('old',date='October 10, 2019'),next_url=NEXT) if url==CATALOG_URL else page(card())
        rows, diag = await self.read(handler)
        self.assertEqual(len(rows),2)
        self.assertEqual(calls,[CATALOG_URL,NEXT])
        self.assertTrue(diag['complete'])

    async def test_verified_empty_tail_commits_and_resumes_without_http(self):
        cache = Cache()
        rows,diag=await self.read(lambda url:page(card(),next_url=NEXT) if url==CATALOG_URL else empty_tail(),cache)
        self.assertTrue(diag['complete']);self.assertEqual(len(rows),1);self.assertEqual(diag['pages_completed'],2)
        restored,diag=await self.read(lambda url:self.fail('closed empty tail re-requested'),cache)
        self.assertEqual(restored,rows);self.assertTrue(diag['complete']);self.assertEqual(diag['pagination_requests'],0)

    async def test_explicit_page_sample_keeps_production_tail_pending(self):
        rows,diag=await self.read(lambda url:page(card(),next_url=NEXT),max_pages=1)
        self.assertEqual(len(rows),1);self.assertFalse(diag['complete'])
        self.assertEqual(diag['pagination_pending'],1);self.assertEqual(diag['selection_limit_origin'],'caller_sample')
        for limit in (0,True,129,'1'):
            with self.assertRaises(ValueError):await self.read(lambda url:'unused',max_pages=limit)

    async def test_corrupt_empty_tail_receipt_cannot_clear_pagination(self):
        cache=Cache();await self.read(lambda url:page(card(),next_url=NEXT) if url==CATALOG_URL else empty_tail(),cache)
        next(iter(cache.rows.values()))['pages'][-1]['empty_query_receipt']['contentType']='BLOG'
        with self.assertRaises(ValueError):await self.read(lambda url:'unused',cache)

    async def test_failed_later_page_preserves_healthy_snapshot_and_retries_only_pending(self):
        cache=Cache(); calls=[]
        def fail(url):
            calls.append(url)
            return page(card(),next_url=NEXT) if url==CATALOG_URL else '<html>challenge</html>'
        rows, diag = await self.read(fail,cache)
        self.assertEqual(len(rows),1); self.assertFalse(diag['complete']); self.assertEqual(diag['next_url'],NEXT)
        rows, diag = await self.read(lambda url: calls.append(url) or page(card('second')),cache)
        self.assertEqual(calls,[CATALOG_URL,NEXT,NEXT]); self.assertEqual(len(rows),2); self.assertTrue(diag['complete'])
        rows, diag = await self.read(lambda url: self.fail('completed pages were requested'),cache)
        self.assertTrue(diag['complete']); self.assertEqual(diag['pagination_requests'],0)

    async def test_save_failure_does_not_acknowledge_page_or_advance(self):
        cache=Cache(); cache.fail=True
        rows,diag=await self.read(lambda url:page(card(),next_url=NEXT),cache)
        self.assertEqual(rows,[]); self.assertEqual(diag['pages_completed'],0); self.assertEqual(diag['next_url'],CATALOG_URL)
        self.assertFalse(cache.rows)

    async def test_repeated_payload_on_new_page_is_not_finite_completion(self):
        rows,diag=await self.read(lambda url:page(card(),next_url=NEXT) if url==CATALOG_URL else page(card()))
        self.assertEqual(len(rows),1); self.assertFalse(diag['complete'])

    async def test_identical_legacy_overlap_keeps_page_receipts_and_unique_facts(self):
        cache = Cache()
        rows, diag = await self.read(lambda url: page(card(), next_url=NEXT) if url == CATALOG_URL
            else page(card(), card('second')), cache)
        self.assertTrue(diag['complete']); self.assertEqual(len(rows), 2)
        self.assertEqual(diag['card_receipts'], 3); self.assertEqual(diag['repeated_card_receipts'], 1)
        restored, diag = await self.read(lambda url: self.fail('healthy pages re-requested'), cache)
        self.assertEqual(restored, rows); self.assertEqual(diag['pagination_requests'], 0)

    async def test_conflicting_legacy_fact_keeps_all_receipts_and_requires_primary_date(self):
        cache = Cache()
        rows, diag = await self.read(lambda url: page(card(), next_url=NEXT) if url == CATALOG_URL
            else page(card(date='October 10, 2024'), card('second')), cache)
        self.assertTrue(diag['complete']); self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['published_at'], '')
        self.assertEqual(len(diag['conflicting_publications'][0]['variants']), 2)
        restored, diag = await self.read(lambda url: self.fail('healthy page re-requested'), cache)
        self.assertEqual(restored, rows); self.assertEqual(diag['pagination_requests'], 0)

    async def test_live_same_page_legacy_duplicate_is_not_a_missing_or_foreign_work(self):
        cache = Cache()
        rows, diag = await self.read(lambda url: page(card(), card(date='October 10, 2024'), card('second')), cache)
        self.assertEqual(len(rows), 2); self.assertTrue(diag['complete'])
        self.assertEqual(diag['card_receipts'], 3); self.assertEqual(diag['repeated_card_receipts'], 1)
        self.assertEqual(rows[0]['published_at'], '')
        restored, diag = await self.read(lambda url: self.fail('completed page requested'), cache)
        self.assertEqual(restored, rows)

    async def test_later_subset_of_known_works_is_not_identical_page_or_exhaustion(self):
        third = NEXT.replace('page=2', 'page=3')
        def handler(url):
            if url == CATALOG_URL: return page(card(), card('second'), next_url=NEXT)
            if url == NEXT: return page(card(), next_url=third)
            return page(card('third'))
        rows, diag = await self.read(handler)
        self.assertEqual(len(rows), 3); self.assertTrue(diag['complete'])
        self.assertEqual(diag['pages_completed'], 3); self.assertEqual(diag['card_receipts'], 4)

    async def test_redirect_and_corrupt_checkpoint_fail_closed(self):
        response=httpx.Response(200,text=page(card()),request=httpx.Request('GET',NEXT))
        rows,diag=await self.read(lambda url:'unused',first_response=response)
        self.assertEqual(rows,[]); self.assertFalse(diag['complete'])
        cache=Cache(); await self.read(lambda url:page(card()),cache)
        next(iter(cache.rows.values()))['pages'][0]['rows'][0]['url']='https://evil.example/research/publications/work/'
        with self.assertRaises(ValueError): await self.read(lambda url:'unused',cache)

    async def test_original_entry_and_failure_ledger_survive_equivalent_catalogue(self):
        async def handler(request):
            if str(request.url)==ENTRY: return httpx.Response(403,request=request)
            return httpx.Response(200,text=page(card(date='October 10, 2019')),request=request)
        factory=lambda **kwargs:httpx.AsyncClient(transport=httpx.MockTransport(handler),**kwargs)
        source=PriorityResearchPageSource(pages=[ENTRY],client_factory=factory,catalog_sleeper=AsyncMock())
        with research_window(NOW): self.assertEqual(await source.safe_fetch(),[])
        receipt=source.coverage()['pages'][ENTRY]
        self.assertEqual(receipt['index_attempts'][0]['status'],403)
        self.assertEqual(receipt['resolved_index_url'],CATALOG_URL)
        self.assertEqual(receipt['status'],'no_recent_links'); self.assertEqual(source.fetch_status,'completed')

    async def test_pending_catalogue_cannot_become_successful_empty_source(self):
        async def handler(request): return httpx.Response(200,text='empty noncontent',request=request)
        source=PriorityResearchPageSource(pages=[ENTRY],client_factory=lambda **kwargs:httpx.AsyncClient(
            transport=httpx.MockTransport(handler),**kwargs),catalog_sleeper=AsyncMock())
        with research_window(NOW): await source.safe_fetch()
        self.assertNotEqual(source.fetch_status,'completed')
        self.assertGreater(source.coverage()['detail_failures'],0)

    async def test_slow_meta_catalogue_cannot_starve_committed_peer_detail(self):
        from tests.test_official_detail_queue import INDEX, index, article
        blocked = asyncio.Event(); calls = []
        async def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, text=index('a') if str(request.url) == INDEX else
                article('a', date='2026-10-10'))
        async def stall(*args, **kwargs):
            blocked.set(); await asyncio.Future()
        source = PriorityResearchPageSource(pages=[ENTRY, INDEX], lookback_days=7, stage_cache=Cache(),
            client_factory=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs))
        with research_window(NOW), patch('sources.meta_catalog.fetch_catalog', stall):
            task = asyncio.create_task(source.safe_fetch())
            await asyncio.wait_for(blocked.wait(), 1)
            self.assertIn(INDEX + '/a', calls)
            self.assertEqual(len(source.partial_results), 1)
            self.assertEqual(source.page_coverage[INDEX]['detail_pending'], 0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task

    async def test_conflicting_catalogue_date_is_resolved_by_actual_primary_not_a_winning_card(self):
        async def handler(request):
            url = str(request.url)
            if url == ENTRY: text = 'legacy non-content index'
            elif url == CATALOG_URL:
                text = page(card(), card(date='October 10, 2024'), card('second', title='Second adaptive neural memory'))
            else:
                title = 'Adaptive neural memory' if url.endswith('/adaptive-memory/') else 'Second adaptive neural memory'
                date = '2024-10-10' if url.endswith('/adaptive-memory/') else '2026-10-10'
                text = f'<meta property="article:published_time" content="{date}"><article><h1>{title}</h1><p>'+('A model training and inference mechanism. '*15)+'</p></article>'
            return httpx.Response(200, text=text, request=request)
        source = PriorityResearchPageSource(pages=[ENTRY], per_page=0, lookback_days=7, stage_cache=Cache(),
            catalog_sleeper=AsyncMock(), client_factory=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs))
        with research_window(NOW): values = await source.safe_fetch()
        self.assertEqual(len(values), 1); self.assertTrue(values[0].url.endswith('/second/'))
        self.assertEqual(source.fetch_status, 'completed')
        self.assertEqual(source.page_coverage[ENTRY]['detail_outside_window'], 1)

    async def test_audit_first_page_sample_never_claims_production_pagination_closed(self):
        from source_audit import Entry, SourceAudit
        async def handler(request):
            if str(request.url)==ENTRY:return httpx.Response(403,request=request)
            if str(request.url)==CATALOG_URL:return httpx.Response(200,text=page(card(),next_url=NEXT),request=request)
            return httpx.Response(200,text='<meta property="article:published_time" content="2026-10-10"><article><h1>Adaptive neural memory</h1><p>'+('A public model training and inference mechanism. '*12)+'</p></article>',request=request)
        result=await SourceAudit(transport=httpx.MockTransport(handler),sleeper=AsyncMock()).run(
            [Entry('official:meta','official',ENTRY)],reference_time=NOW)
        row=result['entries'][0]
        self.assertEqual(row['status'],'passed');self.assertEqual(row['persisted'],1)
        self.assertTrue(row['coverage']['catalog_sample_complete'])
        self.assertFalse(row['coverage']['catalog_diagnostics']['complete'])
        self.assertEqual(row['coverage']['catalog_diagnostics']['pagination_pending'],1)
        self.assertFalse(result['campaign_coverage_complete'])

    async def test_all_explicit_conflicting_dates_outside_window_have_a_bound_not_a_guessed_date(self):
        rows, diag = await self.read(lambda url: page(card(date='October 10, 2023'), card(date='October 10, 2024')))
        self.assertEqual(rows[0]['published_at'], '')
        self.assertEqual(diag['outside_window_conflicting_publications'], [rows[0]['url']])
        self.assertEqual(diag['conflicting_publications'][0]['resolution'], 'outside_window_by_all_explicit_date_bounds')
        _, diag = await self.read(lambda url: page(card(date='October 5, 2026'), card(date='October 10, 2024')))
        self.assertEqual(diag['outside_window_conflicting_publications'], [])


class CitationTests(unittest.TestCase):
    def parse(self,text):
        diag={}; links,count=_discover_bibliographic_publications(text,cutoff=NOW,diagnostics=diag)
        return links,count,diag

    def test_nested_quotes_keep_entire_title_not_prefix(self):
        links,count,diag=self.parse('<p>A. Chen. (2026), “A model named "Alpha" with adaptive memory”, arXiv:2610.12345</p>')
        self.assertEqual(count,0)
        self.assertEqual(links[0].title,'A model named "Alpha" with adaptive memory')

    def test_real_nyu_stray_inner_quote_does_not_lose_explicit_primary(self):
        links,count,diag=self.parse('<p>A. Fábrega. (2026), “” AI Watermarking”: Bridging Policy Discourse and Technical Capabilities”, arXiv:2606.28331</p>')
        self.assertEqual(count,0); self.assertIn('Bridging Policy',links[0].title)
        self.assertEqual(links[0].url,'https://arxiv.org/abs/2606.28331'); self.assertEqual(links[0].published_at,'')

    def test_ambiguous_quote_endpoints_remain_pending_with_stable_identity(self):
        value='<p>A. Chen. (2026), “Adaptive memory”, “Journal title”, arXiv:2610.12345</p>'
        links,count,diag=self.parse(value)
        self.assertEqual(links,[]); self.assertEqual(count,1); self.assertEqual(diag['pending_records'][0]['title'],'')
        self.assertEqual(diag['pending_records'],self.parse(value)[2]['pending_records'])


class CitationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_parser_repair_rebinds_old_pending_without_search_or_dropping_task(self):
        value='<p>A. Fábrega. (2026), “” AI Watermarking”: Bridging Policy Discourse and Technical Capabilities”, arXiv:2606.28331</p>'
        diag={}; _discover_bibliographic_publications(value,cutoff=NOW,diagnostics=diag)
        repaired=diag['explicit_records'][0]
        old={key:item for key,item in repaired.items() if key!='explicit_url'}; old['title']=''
        cache=Cache(); client=AsyncMock(); resolver=BibliographyResolver(stage_cache=cache,enabled=False)
        with research_window(NOW):
            first=await resolver.resolve(client,ENTRY,[old],lookback_days=7)
            rows=await resolver.resolve(client,ENTRY,[],lookback_days=7,explicit_records=[repaired])
            self.assertEqual(first[0]['status'],'pending'); self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['key'],old['key']); self.assertEqual(rows[0]['title'],repaired['title'])
            self.assertEqual(rows[0]['status'],'resolved_url'); self.assertNotIn('work_id',rows[0])
            resolver.verified(rows[0],'2026-10-10T00:00:00+00:00',detail_contract='offline')
            reused=await resolver.resolve(client,ENTRY,[],lookback_days=7,explicit_records=[repaired])
            self.assertEqual(reused[0]['status'],'primary_verified')
            resolver.require_primary(reused[0])
            self.assertEqual(reused[0]['status'],'resolved_url')
        client.get.assert_not_awaited()

    async def test_wrong_explicit_url_rejected_and_unseen_direct_ids_not_added_to_lookup(self):
        resolver=BibliographyResolver(stage_cache=Cache(),enabled=False); client=AsyncMock()
        old={'key':'one','title':'Adaptive Memory','author_text':'A. Chen','citation_year':2026}
        with research_window(NOW):
            await resolver.resolve(client,ENTRY,[old],lookback_days=7)
            with self.assertRaises(ValueError):
                await resolver.resolve(client,ENTRY,[],lookback_days=7,explicit_records=[{**old,'explicit_url':'https://evil.example/work'}])
            rows=await resolver.resolve(client,ENTRY,[],lookback_days=7,explicit_records=[
                {**old,'key':'unseen','explicit_url':'https://arxiv.org/abs/2610.12345'}])
        self.assertEqual([r['key'] for r in rows],['one']); self.assertEqual(rows[0]['status'],'pending')
        client.get.assert_not_awaited()
