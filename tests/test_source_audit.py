"""Hermetic source acceptance and input-protocol regressions."""
from __future__ import annotations

import asyncio
import gzip
import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

import config
from runtime_clock import research_window
from paradigms.rubric import _decision_reason
from source_audit import Entry, SourceAudit, build_entries, safe_url, validate_url
from sources.curated_intelligence_source import _parse_feed
from sources.feed_contract import feed_nodes, feed_text, feed_link
from sources.follow_builders_source import FollowBuildersSource
from sources.hf_papers_source import HuggingFacePapersSource
from sources.priority_research_source import _discover_index_links, _discover_literal_publications, _publication_script_urls
from sources.research_feed_source import ResearchFeedSource

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
RSS = '<rss><channel><item><title>New world model architecture</title><link>https://lab.example/paper</link><pubDate>Thu, 08 Oct 2026 09:00:00 GMT</pubDate><description>Predictive action training.</description></item></channel></rss>'
ATOM = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>New world model architecture</title><link rel="self" href="/atom/item"/>
<link rel="alternate" href="/research/paper"/><author><name>Alex Chen</name><uri>https://author.example</uri></author>
<updated>2026-10-08T09:00:00Z</updated><summary>Predictive action training.</summary>
</entry></feed>'''


class FeedContractTests(unittest.TestCase):
    def test_html_and_non_feed_xml_are_not_successful_zero(self):
        for text in ('<html><body>Sign in</body></html>', '<error/>', '<rss/>'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                feed_nodes(text)

    def test_legal_empty_rss_and_atom_are_valid(self):
        self.assertEqual(feed_nodes('<rss><channel/></rss>'), [])
        self.assertEqual(feed_nodes('<feed xmlns="http://www.w3.org/2005/Atom"/>'), [])

    def test_missing_entry_identity_is_protocol_failure(self):
        with self.assertRaises(ValueError):
            feed_nodes('<rss><channel><item><title>Paper</title></item></channel></rss>')

    def test_atom_author_and_alternate_relative_link(self):
        node = feed_nodes(ATOM)[0]
        self.assertEqual(feed_text(node, 'author'), 'Alex Chen')
        self.assertEqual(feed_link(node, 'https://lab.example/feed.xml'), 'https://lab.example/research/paper')

    def test_updated_is_not_publication_for_research_or_kol_feed(self):
        record = {'name': 'Alex Chen', 'id': 'alex', 'homepage': 'https://author.example', 'origin_policy': 'concept_origin'}
        with research_window(NOW):
            paper = ResearchFeedSource()._parse(ATOM, 'https://lab.example/feed.xml')[0]
            essay = _parse_feed(ATOM, feed_url='https://lab.example/feed.xml', lookback_days=7, record=record, forum=None)[0][0]
        for item in (paper, essay):
            self.assertEqual(item.published_at, '')
            self.assertEqual(item.raw['date_basis'], 'unknown')
            self.assertEqual(item.raw['source_modified_at'], '2026-10-08T09:00:00Z')

    def test_json_ld_modified_cannot_create_recent_publication(self):
        links = _discover_index_links('<script type="application/ld+json">'+json.dumps({'headline':'New world model','url':'https://lab.example/research/model','dateModified':'2026-10-08'})+'</script>', 'https://lab.example/research')
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].published_at, '')

    def test_hf_error_object_and_incomplete_item_are_rejected(self):
        for payload in ({'error':'unavailable'}, [None], [{'paper':{}}]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                HuggingFacePapersSource()._parse(payload)

    def test_follow_builders_error_object_is_not_an_empty_feed(self):
        for payload in ({}, {'x':None}, {'x':['wrong']}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                FollowBuildersSource.validate_feed(payload, 'feed-x.json')
        FollowBuildersSource.validate_feed({'x':[]}, 'feed-x.json')

    def test_root_catalog_cannot_override_privacy_navigation_filter(self):
        links = _discover_index_links('<a href="/privacy-policy/">Privacy Policy</a><a href="/adaptive-memory/">Adaptive Memory Mechanism</a>', 'https://pub.sakana.ai/')
        self.assertEqual([link.title for link in links], ['Adaptive Memory Mechanism'])

    def test_publication_script_never_executes_code_or_infers_conference_date(self):
        script = 'dangerousFunction(); set([{title:"Adaptive World Model",projectLink:"/labs/gear/model/",paperLink:"https://arxiv.org/abs/2610.12345",conference:"ICML July 2026"}])'
        links = _discover_literal_publications(script, 'https://research.nvidia.com/labs/gear/publications/')
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].url, 'https://research.nvidia.com/labs/gear/model/')
        self.assertEqual(links[0].published_at, '')

    def test_script_allowlist_rejects_off_origin_and_unrelated_bundles(self):
        metadata = {'index_script_pattern': r'^/pages/publications-[a-z0-9]+\.js$'}
        html = '<script src="https://other.example/pages/publications-ab12.js"></script><script src="/pages/framework-ab12.js"></script><script src="/pages/publications-ab12.js"></script>'
        self.assertEqual(_publication_script_urls(html, 'https://lab.example/', metadata), ['https://lab.example/pages/publications-ab12.js'])
        self.assertEqual(_publication_script_urls(html, 'https://lab.example/', {}), [])

    def test_rubric_diagnostic_does_not_round_below_threshold_into_equality(self):
        reason = _decision_reason('incomplete', 5, 6, 11/13, .85, [])
        self.assertIn('84.62%', reason)
        self.assertIn('85.00%', reason)


class SourceAuditTests(unittest.IsolatedAsyncioTestCase):
    async def audit(self, entries, handler, **kwargs):
        # Even with production secrets present, the only network boundary is
        # this injected MockTransport, and the SQLite store is temporary.
        return await SourceAudit(transport=httpx.MockTransport(handler), **kwargs).run(entries, reference_time=NOW)

    async def test_nonempty_feed_uses_real_parser_and_sqlite_readback(self):
        def handler(request):
            return httpx.Response(200, text=RSS, request=request)
        result = await self.audit([Entry('rss', 'feed', 'https://lab.example/feed.xml')], handler)
        self.assertTrue(result['acceptance_complete'])
        self.assertFalse(result['campaign_coverage_complete'])
        self.assertEqual(result['entries'][0]['persisted'], 1)

    async def test_one_bad_entry_keeps_healthy_peers_and_manifest_denominator(self):
        def handler(request):
            return httpx.Response(403 if request.url.host == 'blocked.example' else 200, text=RSS, request=request)
        entries = [Entry('bad', 'feed', 'https://blocked.example/rss'), Entry('good', 'feed', 'https://lab.example/rss')]
        result = await self.audit(entries, handler)
        self.assertEqual(result['planned_entries'], 2)
        self.assertEqual(result['returned_entries'], 2)
        self.assertEqual(result['counts'], {'failed':1, 'passed':1})
        self.assertFalse(result['acceptance_complete'])
        self.assertEqual(result['entries'][1]['persisted'], 1)

    async def test_200_login_page_cannot_pass_as_zero(self):
        result = await self.audit([Entry('rss', 'feed', 'https://lab.example/rss')], lambda req:httpx.Response(200, text='<html>Login</html>', request=req))
        self.assertEqual(result['entries'][0]['status'], 'failed')

    async def test_gzip_response_is_not_decompressed_twice(self):
        result = await self.audit([Entry('rss', 'feed', 'https://lab.example/rss')], lambda req:httpx.Response(200, content=gzip.compress(RSS.encode()), headers={'content-encoding':'gzip'}, request=req))
        self.assertEqual(result['counts'], {'passed':1})

    async def test_request_limit_is_failure_not_healthy_zero(self):
        entries = [Entry('one', 'feed', 'https://one.example/rss'), Entry('two', 'feed', 'https://two.example/rss')]
        result = await self.audit(entries, lambda req:httpx.Response(200, text=RSS, request=req), max_requests=1)
        self.assertEqual(result['request_count'], 1)
        self.assertEqual(result['counts'], {'passed':1, 'not_executed':1})

    async def test_total_budget_accounts_for_unexecuted_rows(self):
        result = await self.audit([Entry('one','feed','https://one.example/rss')], lambda req:self.fail('Unexpected request'), budget=0)
        self.assertEqual(result['counts'], {'not_executed':1})
        self.assertEqual(result['returned_entries'], 1)

    async def test_rate_limit_stops_same_host_but_not_other_inputs(self):
        calls=[]
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(429,request=request) if request.url.host == 'limited.example' else httpx.Response(200,text=RSS,request=request)
        entries=[Entry('one','feed','https://limited.example/one'),Entry('two','feed','https://limited.example/two'),Entry('good','feed','https://healthy.example/feed')]
        result=await self.audit(entries,handler)
        self.assertEqual(len(calls),2)
        self.assertEqual(result['counts'],{'failed':1,'not_executed':1,'passed':1})

    async def test_malformed_url_does_not_cancel_healthy_peer(self):
        entries=[Entry('bad','feed','https://[bad'),Entry('good','feed','https://healthy.example/feed')]
        result=await self.audit(entries,lambda req:httpx.Response(200,text=RSS,request=req))
        self.assertEqual(result['counts'],{'failed':1,'passed':1})

    async def test_unknown_key_usage_is_not_silently_enabled(self):
        entry = Entry('paid', 'separate-acceptance', 'https://api.example/search', credential='TAVILY_API_KEY', deferred_reason='requires_separate_authorization')
        with patch.object(config, 'TAVILY_API_KEY', 'sensitive-test-key'):
            result = await self.audit([entry], lambda req:self.fail('Paid API was called'), include_authenticated=True)
        self.assertEqual(result['counts'], {'not_tested':1})
        self.assertNotIn('sensitive-test-key', json.dumps(result))

    async def test_optional_missing_credentials_and_disabled_are_declared(self):
        entries = [Entry('disabled','feed','https://one.example/rss',enabled=False), Entry('missing','github','https://api.github.com/orgs/test/repos',credential='GITHUB_TOKEN')]
        with patch.object(config, 'GITHUB_TOKEN', ''):
            result = await self.audit(entries, lambda req:self.fail('Unexpected request'))
        self.assertEqual(result['counts'], {'disabled':1, 'not_configured':1})
        self.assertNotIn('passed', result['counts'])

    async def test_secret_query_userinfo_and_exception_text_never_persist(self):
        def handler(request):
            raise RuntimeError('API failed with sensitive-test-key')
        entry = Entry('private','feed','https://user:sensitive-test-key@lab.example/rss?token=sensitive-test-key')
        result = await self.audit([entry], handler)
        self.assertNotIn('sensitive-test-key', json.dumps(result))
        self.assertEqual(result['counts'], {'failed':1})

    async def test_redirect_to_private_host_is_rejected(self):
        calls=[]
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={'location':'http://127.0.0.1/private'}, request=request)
        result = await self.audit([Entry('redirect','feed','https://lab.example/rss')], handler)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['counts'], {'failed':1})

    async def test_invalid_venue_cannot_pass_as_successful_zero(self):
        result = await self.audit([Entry('venue','openreview-group','https://api2.openreview.net/groups', {'id':'Unknown/2026'})], lambda req:httpx.Response(200, json={'groups':[]},request=req))
        self.assertEqual(result['counts'], {'failed':1})

    async def test_openreview_false_empty_page_cannot_pass_audit(self):
        entry = Entry('query', 'openreview', 'https://api2.openreview.net/notes/search', {'group':'ICLR.cc/2026/Conference', 'term':'world model'})
        result = await self.audit([entry], lambda req: httpx.Response(200, json={'notes':[], 'count':5}, request=req))
        self.assertEqual(result['counts'], {'failed':1})

    async def test_official_page_uses_production_parser_and_rejects_short_detail(self):
        def handler(request):
            body = '<a href="/research/model">New world model</a>' if request.url.path == '/research' else '<main><h1>Model</h1><p>Sign in</p></main>'
            return httpx.Response(200, text=body, request=request)
        result = await self.audit([Entry('official','official','https://lab.example/research')], handler)
        self.assertEqual(result['counts'], {'failed':1})
        self.assertEqual(result['entries'][0]['coverage']['detail_failures'], 1)

    async def test_dynamic_official_index_uses_registered_script_and_real_detail(self):
        index = 'https://research.nvidia.com/labs/gear/publications/'
        paths = []
        def handler(request):
            paths.append(request.url.path)
            if request.url.path.endswith('publications/'):
                text = '<script src="/labs/gear/_next/static/chunks/pages/publications-ab12.js"></script>'
            elif request.url.path.endswith('.js'):
                text = 'set([{title:"Adaptive World Model",projectLink:"/labs/gear/model/"}]);'
            else:
                text = '<title>Adaptive World Model</title><main>'+('Predictive action training and latent state learning. '*8)+'</main>'
            return httpx.Response(200, text=text, request=request)
        result = await self.audit([Entry('dynamic', 'official', index)], handler)
        self.assertEqual(result['counts'], {'passed': 1})
        self.assertEqual(len(paths), 3)
        self.assertEqual(result['entries'][0]['persisted'], 1)

    async def test_missing_dynamic_script_does_not_discard_healthy_official_peer(self):
        entries = [Entry('dynamic', 'official', 'https://research.nvidia.com/labs/gear/publications/'), Entry('healthy', 'official', 'https://lab.example/research')]
        def handler(request):
            if request.url.host == 'research.nvidia.com':
                text = '<html>Client rendering changed</html>'
            elif request.url.path == '/research':
                text = '<a href="/research/model">Adaptive world model</a>'
            else:
                text = '<title>Adaptive World Model</title><main>'+('Predictive action training. '*10)+'</main>'
            return httpx.Response(200, text=text, request=request)
        result = await self.audit(entries, handler)
        self.assertEqual(result['counts'], {'failed': 1, 'passed': 1})

    async def test_fixed_clock_required(self):
        with self.assertRaises(ValueError):
            await SourceAudit(transport=httpx.MockTransport(lambda req:self.fail('Network'))).run([], reference_time=datetime(2026,10,9))


class SourceAuditWiringTests(unittest.TestCase):
    def test_release_gate_removes_all_thought_source_variables(self):
        from scripts.offline_checks import _safe_environment
        values = {'LESSWRONG_SOURCE_ENABLED':'false', 'ALIGNMENT_FORUM_SOURCE_ENABLED':'false', 'KOL_SOURCE_ENABLED':'false', 'KOL_CUSTOM_FEED_URLS':'https://unexpected.example/feed'}
        with patch.dict(os.environ, values):
            env = _safe_environment()
        self.assertTrue(all(key not in env for key in values))

    def test_cli_rejects_malformed_source_switch_before_manifest_or_network(self):
        from scripts.source_audit import main
        with patch.dict(os.environ, {'KOL_SOURCE_ENABLED':'flase'}), patch('sys.argv', ['source_audit.py','--list']), patch('scripts.source_audit.build_entries') as entries, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 2)
        entries.assert_not_called()

    def test_manifest_keeps_all_configured_pages_and_feed_instances(self):
        with patch.object(config,'PRIORITY_RESEARCH_PAGES',['https://one.example','https://two.example']), patch.object(config,'RESEARCH_FEED_URLS',['https://three.example/rss']):
            entries = build_entries()
        self.assertEqual(sum(entry.kind == 'official' for entry in entries), 2)
        self.assertEqual(sum(entry.kind == 'feed' for entry in entries), 1)
        self.assertEqual(len({entry.key for entry in entries}), len(entries))

    def test_public_metadata_and_url_validation(self):
        self.assertEqual(safe_url('https://user:key@lab.example/feed?key=secret'), 'https://lab.example/feed')
        for value in ('file:///tmp/feed','http://localhost/rss','http://10.0.0.1/rss','https://u:p@lab.example/rss'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_url(value)

    def test_cloud_audit_mode_excludes_production_state_and_mail(self):
        workflow = Path('.github/workflows/weekly-radar.yml').read_text()
        self.assertIn('source_check_only:', workflow)
        self.assertIn('audit_args=(--include-authenticated', workflow)
        self.assertIn('python scripts/source_audit.py "${audit_args[@]}"', workflow)
        self.assertIn('if [ "$SOURCE_PLATFORM_CHECKS" = "true" ]; then audit_args+=(--include-platforms)', workflow)
        self.assertIn('logs/source_audit_latest.json', workflow)
        for name in ('恢复上一次去重状态','抓取、分析并在研究全部闭合后发送邮件','记录状态对应的代码版本','保存本期报告','任一生产步骤失败邮件提醒'):
            block = workflow.split('- name: '+name, 1)[1].split('- name:',1)[0]
            self.assertIn('!inputs.source_check_only', block)
        source_step = workflow.split('id: source_audit',1)[1].split('- name:',1)[0]
        self.assertNotIn('python main.py', source_step)
        self.assertIn('EMAIL_PUSH_ENABLED: "false"', source_step)


if __name__ == '__main__':
    unittest.main()
