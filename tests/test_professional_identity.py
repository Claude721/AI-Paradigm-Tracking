"""Synthetic identity evidence; no DNS, credentials, models, or real people."""
import asyncio
import copy
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import config
from paradigms.models import ResearcherProfile, candidate_from_dict
from paradigms.researcher_identity import bind_current_work, verify_contact, merge_researcher_profiles
from paradigms.evidence_association import profile_references_origin
from paradigms.enrichment import EvidenceEnricher, _attach_social_profiles
from paradigms.analyzer import ResearcherTrajectoryAnalyzer
from sources.professional_identity import profile_label, parse_person_page, public_url, page_status, validate_public_host
from sources.researcher_profile_source import ResearcherProfileClient, _seed_profiles, _name_similarity, _openalex_name_matches
from sources.semantic_scholar_source import SemanticScholarClient, _identity_paper_match
from runtime_clock import research_window
from tests.test_researcher_identity import origin
from tests.test_paradigm_pipeline import candidate

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)
HOME = 'https://research.example/people/alice/'


class IdentityRulesTests(unittest.TestCase):
    def test_unicode_names_preserved_and_empty_names_never_match(self):
        for a, b, expected in [('张三', '李四', 0), ('张三', '张三', 1), ('', '', 0),
                               ('---', '.', 0), ('王伟', '王维', 0), ('Ａｌｉｃｅ', 'Alice', 1)]:
            self.assertEqual(_name_similarity(a, b), expected)

    def test_academic_names_require_explicit_exact_name_or_provider_alias(self):
        self.assertFalse(_openalex_name_matches('Alice Chen',{'display_name':'Alice Cheng'}))
        self.assertTrue(_openalex_name_matches('Alice Chen',{'display_name':'A. Chen','display_name_alternatives':['Alice Chen']}))

    def test_author_metadata_stays_with_each_chinese_person(self):
        paper = origin('unicode', authors=['张三', '李四'])
        paper.raw = {'author_profile_urls': {'张三': 'https://research.example/zhang', '李四': 'https://research.example/li'},
                     'author_public_emails': {'张三': 'zhang@research.example', '李四': 'li@research.example'}}
        values = _seed_profiles(paper, [], 3)
        self.assertEqual({v.name: v.public_email for v in values}, {'张三': 'zhang@research.example', '李四': 'li@research.example'})
        self.assertTrue(all(v.identity_bindings and v.contact_provenance for v in values))

    def test_profile_registry_rejects_repository_org_index_and_lookalike_host(self):
        for url in ['https://github.com/alice/repo', 'https://github.com/orgs/example', 'https://github.com/settings',
                    'https://github.com.evil.example/alice', 'https://linkedin.com/company/example',
                    'https://huggingface.co/datasets', 'https://gitee.com/alice/repo']:
            self.assertEqual(profile_label(url), '', url)
        for url, label in [('https://github.com/alice', 'github'), ('https://huggingface.co/alice', 'huggingface'),
                           ('https://gitee.com/alice', 'gitee'), ('https://www.linkedin.com/in/alice/', 'linkedin')]:
            self.assertEqual(profile_label(url), label)
        self.assertEqual(profile_label(HOME, 'Personal Website'), 'homepage')

    def test_unsafe_and_temporary_urls_cannot_be_seeds(self):
        for url in ['http://127.0.0.1/user', 'https://localhost/user', 'https://u:p@research.example',
                    'https://research.example:22', 'https://research.example/?X-Amz-Signature=temporary',
                    'https://research.example/?api_key=not-a-real-key']:
            self.assertFalse(public_url(url), url)

    def test_unknown_links_are_not_public_contacts(self):
        paper = origin('paper', authors=['Alice Chen'])
        person = _seed_profiles(paper, [], 1)[0]
        person.profile_urls['github'] = 'https://github.com/same-name'
        person.public_email, person.public_email_source = 'guess@research.example', paper.url
        self.assertEqual(person.public_contacts, {})

    def test_page_parser_ignores_navigation_footer_repositories_and_unrelated_page(self):
        text = '''<title>Alice Chen</title><h1>Alice Chen</h1><nav><a href="https://github.com/other">GitHub</a></nav>
        <main><a href="https://github.com/alice/repo">Code</a><a href="https://github.com/alice">My GitHub</a>
        <a href="contact">Contact</a></main><footer><a href="mailto:team@research.example">Email</a></footer>'''
        parsed = parse_person_page(text, HOME, 'Alice Chen')
        self.assertEqual(parsed['links'], [('github', 'https://github.com/alice')])
        self.assertEqual(parsed['emails'], [])
        self.assertEqual(parsed['follow'], [HOME + 'contact'])
        self.assertFalse(parse_person_page('<h1>Other Person</h1><a href="mailto:other@research.example">Email</a>', HOME, 'Alice Chen')['owned'])

    def test_jsonld_same_as_and_multiauthor_directory_are_separate(self):
        text = '<script type="application/ld+json">' + json.dumps({'@type':'Person','name':'Alice Chen',
                'sameAs':['https://github.com/alice','https://github.com/alice/repo']}) + '</script>'
        self.assertEqual(parse_person_page(text, HOME, 'Alice Chen')['links'], [('github','https://github.com/alice')])
        directory = '<script type="application/ld+json">' + json.dumps({'@graph':[
            {'@type':'Person','name':'Alice Chen'}, {'@type':'Person','name':'Bob Lee'}]}) + '</script><a href="mailto:bob@research.example">Email</a>'
        self.assertFalse(parse_person_page(directory, HOME, 'Alice Chen')['owned'])

    def test_microdata_selects_only_the_named_person_section(self):
        text = '''<div itemtype="https://schema.org/Person"><b itemprop="name">Alice Chen</b>
        <a href="mailto:alice@research.example">Email</a></div>
        <div itemtype="https://schema.org/Person"><b itemprop="name">Bob Lee</b><a href="mailto:bob@research.example">Email</a></div>'''
        self.assertEqual(parse_person_page(text, HOME, 'Alice Chen')['emails'], ['alice@research.example'])

    def test_heading_does_not_override_other_structured_person(self):
        text = '<h1>Alice Chen</h1><script type="application/ld+json">' + json.dumps(
            {'@type': 'Person', 'name': 'Bob Lee'}) + '</script><a href="mailto:bob@research.example">Email</a>'
        self.assertFalse(parse_person_page(text, HOME, 'Alice Chen')['owned'])

    def test_hidden_script_text_is_not_a_biography(self):
        text = '<h1>Alice Chen</h1><p>Welcome to my home page.<script>I am a professor and researcher with an invented title.</script></p>'
        self.assertEqual(parse_person_page(text, HOME, 'Alice Chen')['bio'], '')

    def test_contact_proof_cannot_authorize_a_repository_or_secret_url(self):
        person = _seed_profiles(origin('paper', authors=['Alice Chen']), [], 1)[0]
        self.assertFalse(verify_contact(person, 'github', 'https://github.com/alice/repo', HOME, 'person_page_link'))
        self.assertFalse(verify_contact(person, 'homepage', HOME+'?token=not-real', HOME, 'person_page_link'))
        self.assertFalse(verify_contact(person, 'email', 'alice@research.example', HOME+'?api_key=not-real', 'person_page_email'))
        self.assertEqual(person.public_contacts, {})

    def test_explicit_orcid_links_seed_lookup_but_conflicting_ids_never_overwrite(self):
        person=_seed_profiles(origin('paper',authors=['Alice Chen']),[],1)[0]
        first='https://orcid.org/0000-0001-0002-0003'
        second='https://orcid.org/0000-0002-0003-0004'
        self.assertTrue(verify_contact(person,'orcid',first,HOME,'person_page_link'))
        self.assertEqual(person.identifiers['orcid'],'0000-0001-0002-0003')
        self.assertFalse(verify_contact(person,'orcid',second,'https://openalex.org/A1','academic_identity'))
        self.assertEqual(person.public_contacts['orcid'],first)
        self.assertEqual(person.identifiers['orcid'],'0000-0001-0002-0003')
        self.assertEqual(person.identity_observations[-1]['reason'],'ConflictingAcademicIdentity')

    def test_legacy_contacts_survive_merge_both_orders_without_inventing_current_job(self):
        legacy = ResearcherProfile(name='Alice Chen', identifiers={'orcid':'0000-0001-0002-0003'},
            current_affiliation='Undated Lab', profile_urls={'homepage':HOME})
        fresh = _seed_profiles(origin('paper', authors=['Alice Chen']), [], 1)[0]
        fresh.identifiers['orcid'] = '0000-0001-0002-0003'
        verify_contact(fresh, 'orcid', 'https://orcid.org/0000-0001-0002-0003', 'https://arxiv.org/abs/paper', 'work_author_link')
        for a, b in [(legacy, fresh), (fresh, legacy)]:
            result = merge_researcher_profiles([a], [b])[0]
            self.assertEqual(result.public_contacts['homepage'], HOME)
            self.assertEqual(result.contact_provenance['homepage']['relation'], 'historical_identity')
            self.assertEqual(result.current_affiliation, '')
            self.assertTrue(any(v['organization']=='Undated Lab' for v in result.affiliation_observations))
        self.assertEqual(legacy.current_affiliation, 'Undated Lab')

    def test_raw_id_mapping_cannot_merge_legacy_contacts(self):
        paper=origin('paper',authors=['Alice Chen'])
        paper.raw['author_openalex_map']={'Alice Chen':'https://openalex.org/A1'}
        fresh=_seed_profiles(paper,[],1)[0]
        old=ResearcherProfile(name='Alice Chen',identifiers={'openalex':'https://openalex.org/A1'},profile_urls={'homepage':HOME})
        result=merge_researcher_profiles([fresh],[old])
        self.assertEqual(len(result),2)
        self.assertEqual(result[0].public_contacts,{})

    def test_ambiguous_legacy_names_require_their_own_work_link(self):
        a,b=origin('a',authors=['Alice Chen']),origin('b',authors=['Alice Chen'])
        legacy=ResearcherProfile(name='Alice Chen',profile_urls={'homepage':HOME})
        fresh=_seed_profiles(b,[],1)[0]
        value=candidate([a,b]);value.researchers=[legacy,fresh]
        self.assertFalse(profile_references_origin(value,legacy))
        legacy.representative_works=[{'url':a.url}]
        self.assertTrue(profile_references_origin(value,legacy))

    def test_graph_identity_requires_exact_unique_work_or_agreeing_ids(self):
        paper = origin('Mechanism A', authors=['Alice Chen'])
        item = {'title':'Mechanism B', 'externalIds':{}}
        self.assertFalse(_identity_paper_match(paper, item, [item]))
        item['title'] = paper.title
        self.assertTrue(_identity_paper_match(paper, item, [item]))
        self.assertFalse(_identity_paper_match(paper, item, [item, item]))
        paper.identifiers['doi'] = '10.1000/example'
        item['externalIds']['DOI'] = '10.1000/different'
        self.assertFalse(_identity_paper_match(paper, item, [item]))

    def test_contact_follow_is_same_person_subtree_not_global_institution_contact(self):
        text = '<h1>Alice Chen</h1><a href="/contact">Institution contact</a><a href="contact">My contact</a>'
        self.assertEqual(parse_person_page(text, HOME, 'Alice Chen')['follow'], [HOME + 'contact'])

    def test_http_200_login_challenge_and_soft_404_are_not_person_pages(self):
        for title in ['Sign in', 'Page not found', 'Just a moment', '用户不存在']:
            response = httpx.Response(200, text=f'<html><h1>{title}</h1></html>', request=httpx.Request('GET', HOME))
            self.assertEqual(page_status(response)[0], 'blocked')
        for code in [401, 403, 429]:
            response = httpx.Response(code, text='<h1>Alice Chen</h1>', request=httpx.Request('GET', HOME))
            self.assertEqual(page_status(response)[0], 'blocked')
        response = httpx.Response(404, request=httpx.Request('GET', HOME))
        self.assertEqual(page_status(response)[0], 'not_found')

    def test_work_binding_checks_revision_and_does_not_lend_same_name(self):
        a, b = origin('a', authors=['Alice Chen']), origin('b', authors=['Alice Chen'])
        person = _seed_profiles(a, [], 1)[0]
        self.assertTrue(profile_references_origin(candidate([a]), person))
        self.assertFalse(profile_references_origin(candidate([b]), person))
        a.source_revision = 'changed'
        self.assertFalse(profile_references_origin(candidate([a]), person))

    def test_new_provenance_roundtrips_and_legacy_payloads_are_preserved(self):
        paper = origin('paper', authors=['Alice Chen'])
        person = _seed_profiles(paper, [], 1)[0]
        verify_contact(person, 'homepage', HOME, paper.url, 'work_author_link')
        value = candidate([paper]); value.researchers = [person]
        self.assertEqual(candidate_from_dict(value.to_dict()).to_dict(), value.to_dict())
        payload = value.to_dict(); payload['researchers'][0]['identity_bindings'] = ['invalid']
        with self.assertRaises(ValueError): candidate_from_dict(payload)
        legacy = ResearcherProfile(name='Alice Chen', profile_urls={'homepage':HOME})
        self.assertEqual(legacy.public_contacts, {'homepage':HOME})

    def test_partial_probe_states_can_be_checkpointed_before_summary(self):
        value=candidate([origin('paper',authors=['Alice Chen'])])
        value.researchers=_seed_profiles(value.evidence[0],[],1)
        for status in ['invalid','blocked','unverified','not_found','not_executed','partial']:
            value.researchers[0].contact_search_status=status
            self.assertEqual(candidate_from_dict(value.to_dict()).researchers[0].contact_search_status,status)

    def test_display_name_only_social_account_stays_an_unverified_lead(self):
        paper = origin('paper', authors=['Alice Chen'])
        value = candidate([paper]); value.researchers = _seed_profiles(paper, [], 1)
        social = copy.deepcopy(paper)
        social.raw = {'social_profile_url':'https://x.com/same-name', 'social_author_name':'Alice Chen', 'social_bio':'A famous researcher'}
        value.evidence.append(social)
        _attach_social_profiles(value)
        person = value.researchers[0]
        self.assertEqual(person.public_contacts, {})
        self.assertEqual(person.public_bio_excerpt, '')
        self.assertEqual(person.profile_leads[0]['status'], 'unverified')


class IdentityHTTPTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, handler):
        factory = lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
        return ResearcherProfileClient(client_factory=factory, host_validator=AsyncMock())

    async def test_homepage_first_contact_follow_and_cache_no_duplicate_queries(self):
        requests = []
        def handler(request):
            requests.append(str(request.url))
            if request.url.path.endswith('/contact'):
                return httpx.Response(200, text='<h1>Contact</h1><a href="mailto:alice@research.example">Email</a>')
            return httpx.Response(200, text='<h1>Alice Chen</h1><p>I am a doctoral student researching learning systems at Example Laboratory.</p><a href="contact">Contact</a><a href="https://github.com/alice">GitHub</a>')
        client = self.make_client(handler)
        paper = origin('paper', authors=['Alice Chen']); paper.raw['author_profile_urls'] = {'Alice Chen':HOME}
        with patch.object(config, 'OPENALEX_API_KEY', ''):
            first = await client.enrich(paper, [], limit=1)
            second = await client.enrich(paper, [], limit=1)
        self.assertEqual(requests, [HOME, HOME+'contact'])
        self.assertEqual(first[0].public_contacts['email'], 'alice@research.example')
        self.assertEqual(first[0].public_contacts, second[0].public_contacts)
        self.assertIn('doctoral student', first[0].public_bio_excerpt)

    async def test_provider_failure_not_overwritten_by_existing_healthy_contact(self):
        client = self.make_client(lambda r: httpx.Response(200, text='<h1>Alice Chen</h1>'))
        client._openalex = AsyncMock(side_effect=httpx.ConnectError('offline'))
        paper = origin('paper', authors=['Alice Chen']); paper.raw['author_profile_urls'] = {'Alice Chen':HOME}
        with patch.object(config, 'OPENALEX_API_KEY', 'offline-fixture'):
            person = (await client.enrich(paper, [], limit=1))[0]
        self.assertEqual(person.identity_search_status, 'partial')
        self.assertEqual(person.contact_search_status, 'partial')
        self.assertIn('homepage', person.public_contacts)
        self.assertTrue(any(row['provider']=='openalex' and row['status']=='failed' for row in person.identity_observations))

    async def test_unexpected_person_adapter_fault_does_not_cancel_healthy_peer(self):
        client=self.make_client(lambda r:httpx.Response(200,text='<h1>Bob Lee</h1>'))
        paper=origin('paper',authors=['Alice Chen','Bob Lee'])
        paper.raw['author_profile_urls']={'Bob Lee':'https://research.example/bob'}
        homepage=client._homepage_contacts
        async def visit(raw,person):
            if person.name=='Alice Chen': raise RuntimeError('synthetic adapter fault')
            await homepage(raw,person)
        client._homepage_contacts=visit
        with patch.object(config,'OPENALEX_API_KEY',''):
            people=await client.enrich(paper,[],limit=2)
        alice=next(p for p in people if p.name=='Alice Chen')
        bob=next(p for p in people if p.name=='Bob Lee')
        self.assertEqual(alice.identity_search_status,'failed')
        self.assertIn('homepage',bob.public_contacts)
        self.assertTrue(any(v['provider']=='homepage' and v['status']=='completed' for v in bob.identity_observations))

    async def test_blocked_page_does_not_generate_bio_or_mail(self):
        client = self.make_client(lambda r: httpx.Response(200, text='<title>Sign in</title><p>Alice Chen</p><a href="mailto:support@research.example">Email</a>'))
        paper = origin('paper', authors=['Alice Chen']); paper.raw['author_profile_urls']={'Alice Chen':HOME}
        with patch.object(config, 'OPENALEX_API_KEY', ''):
            person = (await client.enrich(paper, [], limit=1))[0]
        self.assertEqual(person.public_email, '')
        self.assertTrue(any(row['status']=='blocked' for row in person.identity_observations))

    async def test_redirect_is_checked_before_request_and_secrets_are_not_forwarded(self):
        requested = []
        client = self.make_client(lambda r: requested.append(str(r.url)) or httpx.Response(302, headers={'location':'http://127.0.0.1/private'}))
        client.host_validator = validate_public_host
        dns = [(None,None,None,None,('8.8.8.8',443))]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: requested.append(str(r.url)) or httpx.Response(302, headers={'location':'http://127.0.0.1/private'}))) as raw:
            with patch('sources.professional_identity.socket.getaddrinfo', return_value=dns):
                with self.assertRaises(ValueError): await client._person_page(raw, HOME)
        self.assertEqual(requested, [HOME])

    async def test_academic_key_is_not_sent_to_orcid_or_personal_pages(self):
        seen=[]
        def handler(request):
            seen.append((request.url.host,request.headers.get('Authorization')))
            if request.url.host=='api.openalex.org': return httpx.Response(200,json={'results':[]})
            if request.url.host=='pub.orcid.org': return httpx.Response(200,json={'person':{}})
            return httpx.Response(200,text='<h1>Alice Chen</h1>')
        client=self.make_client(handler)
        paper=origin('paper',authors=['Alice Chen']);paper.raw.update({
            'author_profile_urls':{'Alice Chen':'https://orcid.org/0000-0001-0002-0003'},
            'author_public_profiles':{'Alice Chen':{'homepage':HOME}}})
        with patch.object(config,'OPENALEX_API_KEY','offline-not-real'):
            await client.enrich(paper,[],limit=1)
        self.assertIn(('api.openalex.org','Bearer offline-not-real'),seen)
        self.assertIn(('pub.orcid.org',None),seen)
        self.assertIn(('research.example',None),seen)

    async def test_private_dns_and_login_platforms_are_not_fetched(self):
        for url in ['https://research.example', 'https://www.linkedin.com/in/alice/']:
            with patch('sources.professional_identity.socket.getaddrinfo', return_value=[(None,None,None,None,('127.0.0.1',443))]):
                with self.assertRaises(ValueError): await validate_public_host(url)

    async def test_contact_branch_failure_does_not_discard_healthy_branch(self):
        def handler(r):
            if r.url.path.endswith('/about'): raise httpx.ConnectError('offline')
            if r.url.path.endswith('/contact'): return httpx.Response(200,text='<h1>Contact</h1><a href="mailto:alice@research.example">Email</a>')
            return httpx.Response(200,text='<h1>Alice Chen</h1><a href="about">About</a><a href="contact">Contact</a>')
        client=self.make_client(handler)
        paper=origin('paper',authors=['Alice Chen']);paper.raw['author_profile_urls']={'Alice Chen':HOME}
        with patch.object(config,'OPENALEX_API_KEY',''):
            person=(await client.enrich(paper,[],limit=1))[0]
        self.assertEqual(person.public_email,'alice@research.example')
        self.assertEqual(person.contact_search_status,'partial')

    async def test_orcid_personal_website_and_employment_dates(self):
        person=ResearcherProfile(name='Alice Chen');bind_current_work(person,origin('paper'))
        body={'person':{'researcher-urls':{'researcher-url':[{'url-name':'Personal Website','url':{'value':HOME}}]}},
              'activities-summary':{'employments':{'affiliation-group':[
                  {'summaries':[{'employment-summary':{'organization':{'name':'Old Lab'},'end-date':{'year':{'value':'2020'}}}}]},
                  {'summaries':[{'employment-summary':{'organization':{'name':'Current Lab'},'start-date':{'year':{'value':'2025'}},
                     'last-modified-date':{'value':int(NOW.timestamp()*1000)}}}]}]}}}
        raw=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=body)))
        with research_window(NOW,observation_time=NOW):
            async with raw: await ResearcherProfileClient()._orcid(raw,person,'0000-0001-0002-0003')
        self.assertEqual(person.profile_urls['homepage'],HOME)
        self.assertEqual(person.current_affiliation,'Current Lab')
        self.assertIn('Old Lab',person.prior_affiliations)

    async def test_orcid_undated_employment_does_not_become_current_job(self):
        person=ResearcherProfile(name='Alice Chen')
        body={'person':{},'activities-summary':{'employments':{'affiliation-group':[{'summaries':[
            {'employment-summary':{'organization':{'name':'Uncertain Lab'}}}]}]}}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=body))) as raw:
            await ResearcherProfileClient()._orcid(raw,person,'0000-0001-0002-0003')
        self.assertEqual(person.current_affiliation,'')
        self.assertEqual(person.affiliation_observations[0]['organization'],'Uncertain Lab')

    async def test_openalex_multiple_people_matching_same_work_is_not_unique(self):
        client=ResearcherProfileClient();client._has_current_work=AsyncMock(return_value=True)
        self.assertIsNone(await client._match_current_work(None,[{'id':'A1'},{'id':'A2'}],'Current Work'))

    async def test_openalex_similar_title_is_not_current_work_proof(self):
        client=ResearcherProfileClient()
        client._openalex_get=AsyncMock(return_value=httpx.Response(200,json={'results':[{'display_name':'Mechanism A: A Large Model'}]}))
        self.assertFalse(await client._has_current_work(None,{'id':'https://openalex.org/A1'},'Mechanism B: A Large Model'))

    async def test_known_author_id_cannot_attach_a_differently_named_coauthor(self):
        client=ResearcherProfileClient()
        person=ResearcherProfile(name='Alice Chen',identifiers={'openalex':'https://openalex.org/A1'})
        client._openalex_get=AsyncMock(side_effect=[httpx.Response(200,json={'id':'https://openalex.org/A1','display_name':'Bob Lee'}),
            httpx.Response(200,json={'results':[]})])
        client._has_current_work=AsyncMock(return_value=True)
        await client._openalex(None,person,origin('paper',authors=['Alice Chen','Bob Lee']))
        client._has_current_work.assert_not_awaited()
        self.assertEqual(person.contact_provenance,{})

    async def test_orcid_later_undated_job_and_wrong_id_do_not_become_current(self):
        body={'person':{},'activities-summary':{'employments':{'affiliation-group':[{'summaries':[
            {'employment-summary':{'organization':{'name':'Dated Lab'},'start-date':{'year':{'value':'2025'}},
                'last-modified-date':{'value':int(NOW.timestamp()*1000)}}},
            {'employment-summary':{'organization':{'name':'Undated Lab'}}}]}]}}}
        person=ResearcherProfile(name='Alice Chen')
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=body))) as raw:
            with research_window(NOW,observation_time=NOW):
                await ResearcherProfileClient()._orcid(raw,person,'0000-0001-0002-0003')
                self.assertEqual(person.current_affiliation,'')
                body['orcid-identifier']={'path':'0000-0002-0003-0004'}
                with self.assertRaises(ValueError):
                    await ResearcherProfileClient()._orcid(raw,person,'0000-0001-0002-0003')

    async def test_page_size_limit_and_cancellation_have_honest_receipts(self):
        paper=origin('paper',authors=['Alice Chen']); paper.raw['author_profile_urls']={'Alice Chen':HOME}
        client=self.make_client(lambda r:httpx.Response(200,text='<h1>Alice Chen</h1>'+'x'*1_000_001))
        person=_seed_profiles(paper,[],1)[0]
        async with client.client_factory() as raw: await client._homepage_contacts(raw,person)
        self.assertEqual(person.identity_observations[-1]['reason'],'IdentityPageSizeLimit')
        client._person_page=AsyncMock(side_effect=asyncio.CancelledError())
        async with client.client_factory() as raw:
            with self.assertRaises(asyncio.CancelledError): await client._homepage_contacts(raw,person)
        self.assertEqual(person.identity_observations[-1]['reason'],'ProbeCancelled')

    async def test_graph_author_failure_keeps_healthy_peer_and_citation(self):
        paper=origin('Current Work',authors=['Alice Chen','Bob Lee'])
        paper.url='https://arxiv.org/abs/2611.00001'
        client=SemanticScholarClient()
        result={'title':paper.title,'url':'https://www.semanticscholar.org/paper/example','authors':[
            {'name':'Alice Chen','authorId':'1'},{'name':'Bob Lee','authorId':'2'}]}
        client._get=AsyncMock(return_value=httpx.Response(200,json={'data':[result]},request=httpx.Request('GET','https://api.semanticscholar.org')))
        async def people(raw,authors):
            if authors[0]['authorId']=='1': raise httpx.ConnectError('offline')
            return [ResearcherProfile(name='Bob Lee',profile_urls={'homepage':'https://research.example/bob'},identifiers={'semantic_scholar':'2'})]
        client._author_profiles=AsyncMock(side_effect=people)
        with patch.object(config,'SEMANTIC_SCHOLAR_ENABLED',True),patch.object(config,'SEMANTIC_SCHOLAR_API_KEY','offline'):
            citation,profiles=await client.enrich_paper(paper)
        self.assertIsNotNone(citation)
        self.assertEqual(len(profiles),2)
        self.assertEqual(profiles[0].identity_observations[0]['status'],'failed')
        self.assertIn('homepage',profiles[1].public_contacts)
        self.assertTrue(all(profile_references_origin(candidate([paper]),v) for v in profiles))

    async def test_graph_fuzzy_paper_does_not_query_people(self):
        paper=origin('Mechanism A: A Large Model',authors=['Alice Chen'])
        client=SemanticScholarClient()
        client._get=AsyncMock(return_value=httpx.Response(200,json={'data':[{'title':'Mechanism B: A Large Model','authors':[{'name':'Alice Chen','authorId':'1'}]}]},request=httpx.Request('GET','https://api.semanticscholar.org')))
        client._author_profiles=AsyncMock()
        with patch.object(config,'SEMANTIC_SCHOLAR_ENABLED',True),patch.object(config,'SEMANTIC_SCHOLAR_API_KEY','offline'):
            citation,profiles=await client.enrich_paper(paper)
        self.assertIsNotNone(citation)
        self.assertEqual(profiles,[])
        client._author_profiles.assert_not_awaited()

    async def test_graph_undated_affiliation_is_not_current_job(self):
        client=SemanticScholarClient()
        request=httpx.Request('GET','https://api.semanticscholar.org')
        client._get=AsyncMock(side_effect=[httpx.Response(200,json={'name':'Alice Chen','authorId':'1','affiliations':['Unknown Time Lab']},request=request),httpx.Response(200,json={'data':[]},request=request)])
        values=await client._author_profiles(None,[{'name':'Alice Chen','authorId':'1'}])
        self.assertEqual(values[0].current_affiliation,'')
        self.assertEqual(values[0].affiliation_observations[0]['organization'],'Unknown Time Lab')

    async def test_multi_origin_enrichment_tries_next_work_after_failure(self):
        a,b=origin('a',authors=['Alice Chen']),origin('b',authors=['Bob Lee'])
        value=candidate([a,b]);value.mechanism_origin_support=[]
        enricher=EvidenceEnricher()
        async def enrich(work,existing,**kwargs):
            if work is a: raise TimeoutError('unavailable')
            return _seed_profiles(work,existing,1)
        enricher.researchers.enrich=AsyncMock(side_effect=enrich)
        await enricher.enrich_people([value])
        self.assertEqual(enricher.researchers.enrich.await_count,2)
        bob=next(p for p in value.researchers if p.name=='Bob Lee')
        alice=next(p for p in value.researchers if p.name=='Alice Chen')
        self.assertEqual(alice.identity_observations[-1]['status'],'failed')
        self.assertTrue(profile_references_origin(candidate([a]),alice))
        self.assertFalse(any(v['provider']=='identity_runtime' for v in bob.identity_observations))

    async def test_bad_seed_metadata_fallback_does_not_abort_next_origin(self):
        a,b=origin('a',authors=['Alice Chen']),origin('b',authors=['Bob Lee'])
        a.raw['author_profile_urls']=['bad metadata']
        value=candidate([a,b]);value.mechanism_origin_support=[]
        enricher=EvidenceEnricher()
        async def enrich(work,existing,**kwargs):
            if work is a: raise ValueError('synthetic bad seed')
            return _seed_profiles(work,existing,1)
        enricher.researchers.enrich=AsyncMock(side_effect=enrich)
        await enricher.enrich_people([value])
        self.assertEqual(value.researchers[0].name,'Bob Lee')
        self.assertIn('researcher_seed',value.community_coverage)

    async def test_same_name_other_work_is_not_sent_to_person_model(self):
        a,b=origin('a',authors=['Alice Chen']),origin('b',authors=['Alice Chen'])
        person=_seed_profiles(a,[],1)[0]
        value=candidate([b]);value.researchers=[person]
        model=ResearcherTrajectoryAnalyzer(client=object(),model='offline')
        await model._analyze_one(value,person)
        self.assertEqual(person.attribution_analysis_status,'insufficient_material')
        person.public_bio_excerpt='I am a researcher; this bio belongs to another work.'
        await model._analyze_one(value,person)
        self.assertEqual(person.attribution_analysis_status,'insufficient_material')


if __name__ == '__main__': unittest.main()
