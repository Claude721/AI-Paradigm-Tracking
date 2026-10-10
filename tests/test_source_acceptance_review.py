"""Source fixes, opt-in authentication and repeated hermetic fault acceptance."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import config
from source_audit import Entry, SourceAudit, diagnose_entry
from scripts.review_source_audits import review
from sources.priority_research_source import _date_from_text, _discover_index_links, _discover_bibliographic_publications, _publication_module_urls, _discover_literal_publications

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
RSS = '<rss><channel><item><title>Adaptive predictive model</title><link>https://lab.example/paper</link><description>Predictive action training.</description></item></channel></rss>'


class SourceRepairTests(unittest.TestCase):
    def test_complete_card_date_not_year_or_invalid_date(self):
        self.assertEqual(_date_from_text('Image 03 August 2026 Energy Automating Chemical Reasoning'), '2026-08-03')
        self.assertEqual(_date_from_text('October 8, 2026 Adaptive Model'), '2026-10-08')
        self.assertEqual(_date_from_text('31 February 2026'), '')
        self.assertEqual(_date_from_text('ICML 2026'), '')

    def test_stepfun_registered_path_cards_not_generic_router_objects(self):
        text = '<script>'+json.dumps({'title':'New predictive learning', 'path':'/research/en/model', 'date':'2026-10-08'})+'</script>'
        self.assertEqual(_discover_index_links(text, 'https://lab.example'), [])
        links = _discover_index_links(text, 'https://chat.stepfun.com/research', source_url='https://chat.stepfun.com/research/en')
        self.assertEqual([(x.url,x.published_at) for x in links], [('https://chat.stepfun.com/research/en/model','2026-10-08')])
        self.assertEqual(_discover_index_links(text.replace('/research/en/model','/account/model'), 'https://chat.stepfun.com/research', source_url='https://chat.stepfun.com/research/en'), [])

    def test_crfm_catalog_internal_dated_path_includes_nonkeyword_title(self):
        links = _discover_index_links('<a href="/2026/10/08/helm.html">HELM Arabic Enterprise</a>', 'https://crfm.stanford.edu/blog.html', source_url='https://crfm.stanford.edu/')
        self.assertEqual(len(links),1)
        self.assertEqual(links[0].published_at,'')  # URL date is not publication metadata

    def test_registered_arc_module_ignores_other_bundles_and_signed_images(self):
        metadata={'publication_module_pattern':r'^/assets/Research-[a-zA-Z0-9_-]+\.js$'}
        text='["assets/Research-ab12.js","assets/Accounts-ab12.js","https://evil.example/assets/Research-ab12.js"]'
        self.assertEqual(_publication_module_urls(text,'https://arc.tencent.com/research',metadata),['https://arc.tencent.com/assets/Research-ab12.js'])
        cards='executeAnything();([{paper_titleEn:"Adaptive World Model",paper_pdf:"https://arxiv.org/pdf/2610.12345",paper_image:"https://cdn.example/image?Signature=private"}])'
        links=_discover_literal_publications(cards,'https://arc.tencent.com/research',schema='tencent_arc')
        self.assertEqual([(x.title,x.url,x.published_at) for x in links],[('Adaptive World Model','https://arxiv.org/abs/2610.12345','')])
        self.assertEqual(links[0].original_url,'https://arxiv.org/pdf/2610.12345')

    def test_bibliography_preserves_missing_primary_denominator_and_unknown_year(self):
        text='<body class="content-sidebar"><div class="content-sidebar-wrap"><main><p>A. Chen. (2026), &#8220;Adaptive predictive learning&#8221;, <em>arXiv:2610.12345</em></p><p>B. Lee. (2026), “Another mechanism”, Journal</p></main></div></body><footer><p>C. Smith. (2026), “Ignored navigation”, arXiv:2610.99999</p></footer>'
        links, unresolved=_discover_bibliographic_publications(text)
        self.assertEqual(unresolved,1)
        self.assertEqual([(x.url,x.published_at) for x in links],[('https://arxiv.org/abs/2610.12345','')])

    def test_403_and_missing_here_are_not_invalid_key_claims(self):
        self.assertEqual(diagnose_entry({'status':'failed','reason':'http_403','credential_required':True})['category'],'authenticated_access_denied')
        self.assertEqual(diagnose_entry({'status':'not_configured','credential_name':'OPENALEX_API_KEY'})['category'],'credential_missing_here')
        self.assertEqual(diagnose_entry({'status':'failed','http':[{'status':432}]})['category'],'billing_or_entitlement')

    def test_bibliography_year_only_excludes_disjoint_year_not_guessed_date(self):
        text='<p>A. Chen. (2025), “Old citation”, arXiv:2510.12345</p><p>B. Lee. (2026), “Same year unknown date”, arXiv:2601.12345</p><p>C. Smith. (2026), “Unresolved current year”, Journal</p>'
        diagnostics={}
        links,unresolved=_discover_bibliographic_publications(text,cutoff=NOW,diagnostics=diagnostics)
        self.assertEqual([(x.url,x.published_at) for x in links],[('https://arxiv.org/abs/2601.12345','')])
        self.assertEqual(unresolved,1)
        self.assertEqual(diagnostics['citation_records'],3)
        self.assertEqual(diagnostics['outside_window_by_year'],1)

    def test_adapter_policy_changes_discovery_receipt_not_other_sources(self):
        from paradigms.discovery import ParadigmDiscovery
        discovery=ParadigmDiscovery()
        before=discovery._plan_signatures()
        with patch('paradigms.discovery.source_record',return_value={'index_path':'/changed'}):
            after=discovery._plan_signatures()
        self.assertNotEqual(before['priority-research-page'],after['priority-research-page'])
        self.assertEqual(before['arxiv'],after['arxiv'])

    def test_unresolved_citation_cannot_hide_behind_completed_summary(self):
        from paradigms.completion import research_completion_violations, discovery_retry_sources
        frontier={'official_pages':{'total_pages':1,'checked_pages':1,'unresolved_citations':1}}
        self.assertTrue(research_completion_violations({'research_incomplete':False,'coverage_incomplete':False,'frontier_coverage':frontier}))
        self.assertIn('priority-research-page',discovery_retry_sources(frontier))


class AcceptanceFaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_50_fault_rounds_preserve_healthy_inputs_and_no_false_completion(self):
        # Pressure applies to OUR code with MockTransport, not provider servers.
        for index in range(50):
            def handler(request):
                if request.url.host == 'healthy.example':
                    return httpx.Response(200,text=RSS,request=request)
                case=index % 5
                if case==0: return httpx.Response(429,request=request)
                if case==1: return httpx.Response(403,request=request)
                if case==2: return httpx.Response(200,text='<html>Login</html>',request=request)
                if case==3: raise httpx.ConnectError('secret body must not leak',request=request)
                return httpx.Response(200,text='<rss><channel><item/></channel></rss>',request=request)
            result=await SourceAudit(transport=httpx.MockTransport(handler)).run([Entry('bad','feed','https://bad.example/rss'),Entry('good','feed','https://healthy.example/rss')],reference_time=NOW)
            self.assertEqual(result['counts'],{'failed':1,'passed':1})
            self.assertFalse(result['acceptance_complete'])
            self.assertEqual(result['entries'][1]['persisted'],1)
            self.assertNotIn('secret body must not leak',json.dumps(result))
            self.assertEqual(result['returned_entries'],2)

    async def test_platform_opt_in_is_minimal_and_redacted(self):
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx.Response(200,json={'results':[{'title':'Research','url':'https://lab.example/paper'}],'usage':{'credits':1}},request=request)
        entry=Entry('tavily','separate-acceptance','https://api.tavily.com/search',credential='TAVILY_API_KEY',deferred_reason='requires_separate_authorized_platform_acceptance')
        with patch.object(config,'TAVILY_API_KEY','private-key'):
            result=await SourceAudit(transport=httpx.MockTransport(handler),include_platforms=True).run([entry],reference_time=NOW)
        self.assertEqual(result['counts'],{'passed':1})
        self.assertEqual(len(requests),1)
        self.assertEqual(json.loads(requests[0].content)['search_depth'],'basic')
        self.assertEqual(result['entries'][0]['platform_usage_credits'],1)
        self.assertNotIn('private-key',json.dumps(result))

    async def test_missing_required_credentials_cannot_complete_acceptance(self):
        with patch.object(config,'GITHUB_TOKEN',''):
            result=await SourceAudit(transport=httpx.MockTransport(lambda request:self.fail('Network'))).run([Entry('gh','github','https://api.github.com/orgs/openai/repos',credential='GITHUB_TOKEN')],reference_time=NOW)
        self.assertFalse(result['acceptance_complete'])

    async def test_authentication_status_not_retried_and_no_secret_output(self):
        for status,category in [(401,'authentication_rejected'),(403,'authenticated_access_denied'),(429,'rate_limit_or_quota')]:
            entry=Entry('s2','github','https://api.github.com/orgs/openai/repos',credential='GITHUB_TOKEN')
            with patch.object(config,'GITHUB_TOKEN','private-key'):
                result=await SourceAudit(transport=httpx.MockTransport(lambda request:httpx.Response(status,request=request)),include_authenticated=True).run([entry],reference_time=NOW)
            self.assertEqual(result['request_count'],1)
            self.assertEqual(result['entries'][0]['diagnosis']['category'],category)
            self.assertNotIn('private-key',json.dumps(result))

    async def test_invalid_platform_zero_is_not_green(self):
        entry=Entry('x','separate-acceptance','https://api.x.com/2/tweets/search/recent',credential='TWITTER_BEARER_TOKEN',deferred_reason='requires_separate_authorized_platform_acceptance')
        with patch.object(config,'TWITTER_BEARER_TOKEN','private-key'):
            result=await SourceAudit(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'errors':[{'detail':'failure'}]},request=request)),include_platforms=True).run([entry],reference_time=NOW)
        self.assertEqual(result['counts'],{'failed':1})

    async def test_registered_arc_module_uses_bounded_fetch_and_keeps_identity(self):
        paths=[]
        def handler(request):
            paths.append(request.url.path)
            if request.url.path=='/research': text='<script src="/assets/index-ab12.js"></script>'
            elif request.url.path=='/assets/index-ab12.js': text='map(["assets/Research-ab12.js","assets/Account-ab12.js"])'
            elif request.url.path=='/assets/Research-ab12.js': text='[{paper_titleEn:"Adaptive World Model",paper_pdf:"https://arc.tencent.com/research/model"}]'
            else: text='<title>Adaptive World Model</title><main>'+('Mechanism and experimental evidence. '*10)+'</main>'
            return httpx.Response(200,text=text,request=request)
        result=await SourceAudit(transport=httpx.MockTransport(handler)).run([Entry('arc','official','https://arc.tencent.com/research')],reference_time=NOW)
        self.assertEqual(result['counts'],{'passed':1})
        self.assertEqual(paths,['/research','/assets/index-ab12.js','/assets/Research-ab12.js','/research/model'])

    async def test_unresolved_bibliography_keeps_source_gate_closed(self):
        def handler(request):
            return httpx.Response(200,text='<main><p>A. Chen. (2026), “Novel mechanism without primary ID”, Journal</p></main>',request=request)
        result=await SourceAudit(transport=httpx.MockTransport(handler)).run([Entry('nyu','official','https://wp.nyu.edu/cilvr/cilvr-group-publications/')],reference_time=NOW)
        self.assertEqual(result['counts'],{'failed':1})
        self.assertEqual(result['entries'][0]['coverage']['unresolved_citations'],1)
        self.assertFalse(result['acceptance_complete'])

    async def test_registered_catalog_route_preserves_declared_identity(self):
        paths=[]
        def handler(request):
            paths.append(request.url.path)
            if request.url.path=='/blog.html':
                text='<a href="/2026/10/08/helm.html">HELM Arabic Enterprise</a>'
            else:
                text='<title>HELM Arabic Enterprise</title><main>'+('Verified mechanism details. '*10)+'</main>'
            return httpx.Response(200,text=text,request=request)
        result=await SourceAudit(transport=httpx.MockTransport(handler)).run([Entry('crfm','official','https://crfm.stanford.edu/')],reference_time=NOW)
        self.assertEqual(paths,['/blog.html','/2026/10/08/helm.html'])
        self.assertEqual(result['entries'][0]['url'],'https://crfm.stanford.edu/')
        self.assertEqual(result['counts'],{'passed':1})


class ReviewTests(unittest.TestCase):
    def test_missing_or_changed_scope_not_silently_dropped(self):
        row={'key':'source','scope_hash':'old','url':'https://lab.example/rss?token=private','status':'failed','reason':'http_403'}
        newer={**row,'scope_hash':'new','status':'passed'}
        with tempfile.TemporaryDirectory() as directory:
            paths=[Path(directory)/name for name in ('a.json','b.json')]
            for path,value in zip(paths,[row,newer]):
                path.write_text(json.dumps({'planned_entries':1,'returned_entries':1,'entries':[value]}))
            result=review(paths)
        self.assertEqual(len(result['entries']),2)
        self.assertEqual(result['entries'][0]['absent_run_indices'],[1])
        self.assertFalse(result['long_term_stability_proven'])
        self.assertIsNone(result['entries'][0]['entry_seconds']['median'])
        self.assertNotIn('private',json.dumps(result))
