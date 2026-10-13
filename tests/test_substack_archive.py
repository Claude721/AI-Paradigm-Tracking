import copy
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import config
from paradigms.models import EvidenceType
from research_watchlist import KOL_SOURCES
from runtime_clock import research_window
from sources.substack_archive import fetch_archive, public_payload, post_record
from sources.curated_intelligence_source import CuratedKOLSource
from source_audit import Entry, SourceAudit
from tests.test_meta_catalog import Cache

NOW=datetime(2026,10,12,tzinfo=timezone.utc)
ROOT='https://garymarcus.substack.com'
RECORD=next(item for item in KOL_SOURCES if item['id']=='gary-marcus')
ARCHIVE=ROOT+'/archive'
BODY='<p>A language model reasoning mechanism changes inference and training signals. '+('Learning and memory representation. '*10)+'</p>'


def post(number=1, *, date='2026-10-10T00:00:00Z', audience='everyone'):
    return {'id':number,'publication_id':888615,'title':'Language model reasoning architecture',
        'post_date':date,'canonical_url':ROOT+'/p/work-'+str(number),'audience':audience,
        'publishedBylines':[{'name':'Gary Marcus'}], 'description':'Language model reasoning architecture and training mechanism',
        'body_html':BODY}


def hydration(*, posts=None, detail=None, extra=None):
    value={'pub':{'id':888615},'base_url':ROOT,'publicationSettings':{'block_ai_crawlers':False}}
    if posts is not None: value['newPostsForArchive']={'pub':posts}
    if detail is not None: value['post']=detail
    value.update(extra or {})
    return '<script>window._preloads = JSON.parse('+json.dumps(json.dumps(value))+');</script>'


class PayloadTests(unittest.TestCase):
    def test_live_semicolon_free_literal_is_supported_without_executing_suffix(self):
        literal = hydration(posts=[]).replace(');</script>', ')\n</script>')
        self.assertEqual(public_payload(literal, root=ROOT, publication_id=888615)['pub']['id'], 888615)
        with self.assertRaises(ValueError):
            public_payload(literal.replace('\n</script>', ';executeAnything()</script>'), root=ROOT, publication_id=888615)

    def test_public_json_literal_is_data_not_javascript(self):
        self.assertEqual(public_payload(hydration(posts=[]),root=ROOT,publication_id=888615)['pub']['id'],888615)
        for value in ('<script>executeAnything()</script>', hydration(posts=[])*2,
            hydration(posts=[]).replace('JSON.parse(', 'evaluate(')):
            with self.assertRaises(ValueError): public_payload(value,root=ROOT,publication_id=888615)

    def test_publisher_or_ai_crawler_denial_is_not_bypassed(self):
        for extra in ({'pub':{'id':573100}},{'base_url':'https://evil.example'},
                      {'publicationSettings':{'block_ai_crawlers':True}},{'publicationSettings':{}}):
            with self.assertRaises(ValueError): public_payload(hydration(posts=[],extra=extra),root=ROOT,publication_id=888615)

    def test_date_title_url_author_audience_must_close(self):
        for extra in ({'post_date':'2026-10-10'},{'canonical_url':ROOT+'/p/work?Signature=private'},
            {'canonical_url':'https://evil.example/p/work'},{'title':''},{'publication_id':573100},
            {'publishedBylines':[{'name':'Different Person'}]},{'audience':'unknown'},{'description':['bad']}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                post_record({**post(),**extra},root=ROOT,publication_id=888615,aliases=('Gary Marcus',))

    def test_date_modified_does_not_replace_publication(self):
        value=post_record({**post(date='2024-01-01T00:00:00Z'),'dateModified':NOW.isoformat()},
            root=ROOT,publication_id=888615,aliases=('Gary Marcus',))
        self.assertEqual(value['published_at'],'2024-01-01T00:00:00+00:00')


class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def run_archive(self, handler, cache=None, record=RECORD, **kwargs):
        calls=[]
        async def get(url):
            calls.append(url)
            value=handler(url)
            if isinstance(value,httpx.Response): return value
            return httpx.Response(200,text=value,request=httpx.Request('GET',url))
        with research_window(NOW):
            result=await fetch_archive(get,record=record,feed_url=ROOT+'/feed',lookback_days=7,
                stage_cache=cache,sleeper=AsyncMock(),**kwargs)
        return result,calls

    def handler(self, url):
        return hydration(posts=[post()]) if url==ARCHIVE else hydration(detail=post())

    async def test_body_identity_and_secondary_policy_are_preserved(self):
        (items,filtered,diag),calls=await self.run_archive(self.handler)
        self.assertTrue(diag['complete']); self.assertEqual(len(items),1)
        self.assertEqual(calls,[ARCHIVE,post()['canonical_url']])
        self.assertEqual(items[0].evidence_type,EvidenceType.SECONDARY_INTERPRETATION)
        self.assertEqual(items[0].authors,['Gary Marcus']); self.assertFalse(items[0].raw['full_text_verified'])
        self.assertIn('rendered_body',items[0].raw['public_text_boundary'])

    async def test_paid_preview_stays_public_and_never_unlocks_body(self):
        (items,_,diag),calls=await self.run_archive(lambda url:hydration(posts=[post(audience='only_paid')]))
        self.assertTrue(diag['complete']); self.assertEqual(calls,[ARCHIVE]); self.assertEqual(len(items),1)
        self.assertEqual(items[0].raw['public_text_boundary'],'public_preview_not_full_text')

    async def test_missing_or_mismatched_body_remains_pending(self):
        for wrong in ({**post(),'canonical_url':ROOT+'/p/different'}, {**post(),'body_html':None},
                      {**post(),'post_date':'2026-10-09T00:00:00Z'}):
            with self.subTest(wrong=wrong):
                (items,_,diag),_=await self.run_archive(lambda url:hydration(posts=[post()]) if url==ARCHIVE else hydration(detail=wrong))
                self.assertEqual(items,[]); self.assertFalse(diag['complete']); self.assertEqual(diag['pending_public_posts'],1)

    async def test_missing_archive_byline_requires_exact_public_detail_author(self):
        missing = {**post(), 'publishedBylines': []}
        def handler(url):
            return hydration(posts=[missing]) if url == ARCHIVE else '<meta name="author" content="Gary Marcus">'+hydration(detail=missing)
        cache = Cache()
        (rows, _, diag), calls = await self.run_archive(handler, cache)
        self.assertTrue(diag['complete']); self.assertEqual(rows[0].authors, ['Gary Marcus'])
        self.assertIn('html_author', rows[0].raw['author_identity_basis'])
        (restored, _, diag), calls = await self.run_archive(lambda url: self.fail('verified body re-requested'), cache)
        self.assertEqual(calls, []); self.assertEqual(restored[0].authors, ['Gary Marcus'])

    async def test_missing_or_ambiguous_public_author_only_holds_that_post_pending(self):
        missing = {**post(1), 'publishedBylines': []}
        for metadata in ('', '<meta name="author" content="Gary Marcus"><meta name="author" content="Different Person">'):
            def handler(url):
                if url == ARCHIVE: return hydration(posts=[missing, post(2)])
                return (metadata+hydration(detail=missing)) if url.endswith('1') else hydration(detail=post(2))
            (rows, _, diag), _ = await self.run_archive(handler)
            self.assertEqual(len(rows), 1); self.assertFalse(diag['complete'])
            self.assertEqual(diag['pending_public_posts'], 1); self.assertEqual(diag['failed_public_posts'], 1)

    async def test_healthy_peer_survives_failure_and_only_failed_body_retries(self):
        cache=Cache()
        def first(url):
            if url==ARCHIVE: return hydration(posts=[post(1),post(2)])
            if url.endswith('2'): return httpx.Response(503,request=httpx.Request('GET',url))
            return hydration(detail=post(1))
        (items,_,diag),_=await self.run_archive(first,cache)
        self.assertEqual(len(items),1); self.assertFalse(diag['complete'])
        (items,_,diag),calls=await self.run_archive(lambda url:hydration(detail=post(2)),cache)
        self.assertEqual(calls,[post(2)['canonical_url']]); self.assertEqual(len(items),2)
        self.assertTrue(diag['complete']); self.assertEqual(diag['failure_history'][0]['stage'],'public_post')

    async def test_duplicate_out_of_order_or_unknown_archive_is_not_empty_success(self):
        for values in ([post(),post()], [post(1,date='2026-10-09T00:00:00Z'),post(2)], [{'error':'unavailable'}]):
            (items,_,diag),_=await self.run_archive(lambda url:hydration(posts=values))
            self.assertEqual(items,[]); self.assertFalse(diag['complete'])

    async def test_old_publication_is_filtered_without_body_request(self):
        (items,_,diag),calls=await self.run_archive(lambda url:hydration(posts=[post(date='2024-01-01T00:00:00Z')]))
        self.assertEqual(items,[]); self.assertTrue(diag['complete']); self.assertEqual(calls,[ARCHIVE])

    async def test_complete_empty_catalogue_is_valid_zero(self):
        (items,_,diag),calls=await self.run_archive(lambda url:hydration(posts=[]))
        self.assertEqual(items,[]); self.assertTrue(diag['complete']); self.assertEqual(calls,[ARCHIVE])

    async def test_nonregistered_or_concept_origin_cannot_gain_fallback(self):
        for value in ({**RECORD,'substack_public_archive':{}},{**RECORD,'origin_policy':'concept_origin'}):
            with self.assertRaises(ValueError): await self.run_archive(self.handler,record=value)

    async def test_changed_publisher_registration_cannot_reuse_healthy_catalogue(self):
        cache = Cache(); await self.run_archive(self.handler, cache)
        changed = {**RECORD, 'substack_public_archive': {**RECORD['substack_public_archive'], 'publication_id': 573100}}
        (rows, _, diag), calls = await self.run_archive(self.handler, cache, record=changed)
        self.assertEqual(rows, []); self.assertFalse(diag['complete']); self.assertEqual(calls, [ARCHIVE])

    async def test_caller_sample_does_not_close_unvisited_production_posts(self):
        def handler(url): return hydration(posts=[post(1),post(2)]) if url==ARCHIVE else hydration(detail=post(int(url.rsplit('-',1)[-1])))
        (items,_,diag),calls=await self.run_archive(handler,detail_limit=1)
        self.assertEqual(len(items),1); self.assertEqual(len(calls),2)
        self.assertTrue(diag['catalogue_complete']); self.assertFalse(diag['complete'])
        self.assertEqual(diag['pending_public_posts'],1); self.assertEqual(diag['failed_public_posts'],0)

    async def test_save_failure_does_not_advance_or_acknowledge_catalogue(self):
        cache=Cache(); cache.fail=True
        with self.assertRaises(OSError): await self.run_archive(self.handler,cache)
        self.assertFalse(cache.rows)

    async def test_corrupt_catalogue_cursor_or_author_receipt_cannot_close(self):
        for corruption in ('cursor', 'author', 'type'):
            cache = Cache()
            await self.run_archive(self.handler, cache)
            if corruption == 'cursor':
                next(value for (stage, _), value in cache.rows.items() if stage == 'substack_archive_catalog')['offset'] = 12
                with self.assertRaises(ValueError): await self.run_archive(self.handler, cache)
            else:
                value = next(value for (stage, _), value in cache.rows.items() if stage == 'substack_public_post')
                value['evidence'][0]['authors' if corruption == 'author' else 'evidence_type'] = (
                    ['Different Person'] if corruption == 'author' else 'concept_essay')
                (items, _, diag), _ = await self.run_archive(self.handler, cache)
                self.assertEqual(items, []); self.assertFalse(diag['complete'])

    async def test_canonical_json_key_order_does_not_break_durable_paging_or_result_order(self):
        class SortedCache(Cache):
            def cache_save(self, stage, key, value):
                super().cache_save(stage, key, json.loads(json.dumps(value, sort_keys=True)))
        cache = SortedCache()
        posts = [post(12), post(2, date='2026-10-09T00:00:00Z'), post(3, date='2026-10-08T00:00:00Z')]
        def handler(url):
            return hydration(posts=posts) if url == ARCHIVE else hydration(detail=next(p for p in posts if p['canonical_url'] == url))
        (rows, _, diag), _ = await self.run_archive(handler, cache)
        (restored, _, diag), calls = await self.run_archive(lambda url: self.fail('completed post requested'), cache)
        self.assertEqual(calls, []); self.assertTrue(diag['complete'])
        self.assertEqual([r.to_dict() for r in rows], [r.to_dict() for r in restored])
        next(value for (stage, _), value in cache.rows.items() if stage == 'substack_archive_catalog')['last_date'] = NOW.isoformat()
        with self.assertRaises(ValueError): await self.run_archive(handler, cache)

    async def test_pagination_uses_new_sort_and_actual_offset_until_window_closes(self):
        first=[post(i,date='2026-10-10T00:00:00Z',audience='only_paid') for i in range(1,13)]
        def handler(url):
            if url==ARCHIVE: return hydration(posts=first)
            return httpx.Response(200,json=[post(13,date='2024-01-01T00:00:00Z')],request=httpx.Request('GET',url))
        (items,_,diag),calls=await self.run_archive(handler)
        self.assertEqual(len(items),12); self.assertTrue(diag['complete'])
        self.assertIn('sort=new&offset=12&limit=12',calls[-1])


class FeedAndAuditTests(unittest.IsolatedAsyncioTestCase):
    def handler(self, request):
        if request.url.path=='/feed': return httpx.Response(403,request=request)
        return httpx.Response(200,text=hydration(posts=[post()]) if request.url.path=='/archive' else hydration(detail=post()),request=request)

    async def test_feed_failure_kept_after_equivalent_archive_completes(self):
        source=CuratedKOLSource(lookback_days=7,sleeper=AsyncMock(),client_factory=lambda **kwargs:
            httpx.AsyncClient(transport=httpx.MockTransport(self.handler),**kwargs))
        with research_window(NOW),patch('sources.curated_intelligence_source.KOL_SOURCES',(RECORD,)),patch.object(config,'KOL_CUSTOM_FEED_URLS',[]):
            items=await source.fetch()
        self.assertEqual(len(items),1); self.assertEqual(source.coverage()['completed_feeds'],1)
        self.assertEqual(source.coverage()['failed_feeds'],[])
        self.assertEqual(source.coverage()['feed_receipts'][ROOT+'/feed']['attempts'][0]['status'],403)

    async def test_audit_uses_same_production_fallback_and_original_manifest_row(self):
        result=await SourceAudit(transport=httpx.MockTransport(self.handler),sleeper=AsyncMock()).run(
            [Entry('kol:gary-marcus:0','kol',ROOT+'/feed',metadata={'record':RECORD})],reference_time=NOW)
        row=result['entries'][0]
        self.assertEqual(row['status'],'passed'); self.assertEqual(row['url'],ROOT+'/feed')
        self.assertEqual(row['persisted'],1); self.assertEqual(row['coverage']['attempts'][0]['status'],403)
        self.assertFalse(result['campaign_coverage_complete'])

    async def test_failed_fallback_is_not_greenified_or_invalid_key(self):
        result=await SourceAudit(transport=httpx.MockTransport(lambda request:httpx.Response(403,request=request)),sleeper=AsyncMock()).run(
            [Entry('kol:gary-marcus:0','kol',ROOT+'/feed',metadata={'record':RECORD})],reference_time=NOW)
        self.assertEqual(result['entries'][0]['status'],'failed'); self.assertFalse(result['acceptance_complete'])
