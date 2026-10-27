"""Input hygiene is not a relaxed Rubric or a source-wide publication veto."""
import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import httpx

from database.paradigm_store import ParadigmStore
from paradigms.analyzer import ParadigmAnalyzer, ParadigmSynthesizer
from paradigms.discovery import ParadigmDiscovery
from paradigms.publication import nonresearch_page_reason
from runtime_clock import research_window
from run_audit import run_audit
from sources.arxiv_document_source import ArxivDocumentClient
from sources.official_detail_queue import OfficialDetailQueue
from sources.priority_research_source import PriorityResearchPageSource, _ArticleParser, _ResearchLink
from sources.research_feed_source import ResearchFeedSource
from tests.test_production_failure_regressions import mixed_candidate
from tests.test_research_archive import origin

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
INDEX = "https://lab.example/research"
CLIENT = httpx.AsyncClient


class PagePurposeTests(unittest.TestCase):
    def test_recruitment_accounts_and_challenges_have_distinct_reasons(self):
        for url, body, title, expected in (
            ("https://lab.example/research/careers/", "", "Research Careers", "recruitment_page"),
            ("https://lab.example/jobs/research-scientist", "", "Research scientist", "recruitment_page"),
            ("https://lab.example/research/new", "<title>Verifying your browser</title>", "", "access_challenge"),
            ("https://openreview.net/forum?id=one", "", "Verifying your browser", "access_challenge"),
            ("https://lab.example/new", "<h1>Verify you are human</h1>", "", "access_challenge"),
            ("https://lab.example/new", "<title>Sign in | Lab</title>", "", "account_or_policy_page"),
            ("https://dashboard.lab.example/api-keys", "", "API keys", "account_or_policy_page"),
        ):
            with self.subTest(url=url, title=title):
                self.assertEqual(nonresearch_page_reason(url, body, title=title), expected)

    def test_topics_and_quoted_challenges_do_not_blacklist_real_articles(self):
        for url, title in (
            ("https://lab.example/research/privacy", "Differential privacy for language models"),
            ("https://lab.example/blog/futures-of-work", "AI mechanisms for future work"),
            ("https://lab.example/papers/verifying-browsers", "Verifying your browser: a formal model"),
            ("https://lab.example/research/api-key-representation", "Representation learning"),
        ):
            with self.subTest(url=url):
                self.assertEqual(nonresearch_page_reason(url,
                    '<title>' + title + '</title><nav>Login Careers</nav><article><p>'
                    'We study the message "Verifying your browser".</p></article>', title=title), "")


class ArticleBodyTests(unittest.TestCase):
    def test_body_is_not_page_shell_and_article_header_keeps_metadata(self):
        parser = _ArticleParser("https://lab.example/research/new")
        parser.feed('''<title>Browser display title</title><div>OUTSIDE NAVIGATION</div><time datetime="2020-01-01"></time>
            <div class="navigation_menu"><a href="/other.pdf">WRONG REPORT</a></div>
            <main><div>MAIN SHELL<a href="/shell.pdf">WRONG SHELL REPORT</a></div><article><header><h1>Research mechanism</h1>
            <time datetime="2026-10-20"></time><a href="/report.pdf">Full report</a></header>
            <p>ACTUAL TRAINING SIGNAL</p><section class="related-posts">RELATED NOISE</section>
            <div role="dialog">COOKIE NOISE</div><p hidden>HIDDEN NOISE</p>
            <form>NEWSLETTER FORM</form><p>Real limitations remain.</p>
            <footer>Article author and acknowledgements.</footer></article></main><footer>GLOBAL FOOTER</footer>''')
        self.assertIn("ACTUAL TRAINING SIGNAL", parser.text)
        self.assertIn("Real limitations remain.", parser.text)
        self.assertIn("Research mechanism", parser.text)
        self.assertIn("Article author and acknowledgements.", parser.text)
        for noise in ("Browser display title", "OUTSIDE NAVIGATION", "MAIN SHELL", "RELATED NOISE",
                      "COOKIE NOISE", "HIDDEN NOISE", "NEWSLETTER FORM", "GLOBAL FOOTER"):
            self.assertNotIn(noise, parser.text)
        self.assertEqual(parser.published_at, "2026-10-20")
        self.assertEqual(parser.links, [("Full report", "https://lab.example/report.pdf")])

    def test_simple_academic_page_and_wordpress_layout_keep_body(self):
        for html in (
            '<h1>Research</h1><p>Plain academic mechanism and limitations.</p>',
            '<body class="content-sidebar"><div class="content-sidebar-wrap"><main>'
            '<p>Plain academic mechanism and limitations.</p></main></div></body>',
            '<div role="main"><p>Plain academic mechanism and limitations.</p></div>',
        ):
            parser = _ArticleParser()
            parser.feed(html)
            self.assertIn("academic mechanism and limitations", parser.text)

    def test_plain_research_words_are_never_a_body_denylist(self):
        parser = _ArticleParser()
        parser.feed('<article><p>Privacy, work, careers, browser challenges and API keys are research topics.</p></article>')
        self.assertIn("Privacy, work, careers, browser challenges and API keys", parser.text)


class InputExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        run_audit.reset()
        self.addCleanup(run_audit.reset)

    async def test_input_audit_has_real_json_location_without_raw_content(self):
        exclusions = [{"fingerprint": "one", "url": INDEX + "/careers", "source": "official",
                       "reason": "recruitment_page", "retryable": False}]
        with tempfile.TemporaryDirectory() as directory:
            result = run_audit.write({"frontier_coverage": {"origin_input_exclusions": exclusions}},
                                     output_dir=directory)
            data = json.loads(Path(result["audit_json_path"]).read_text())
            text = Path(result["audit_markdown_path"]).read_text()
        self.assertEqual(data["pipeline_stats"]["frontier_coverage"]["origin_input_exclusions"], exclusions)
        self.assertIn("输入内容诊断", text)
        self.assertIn("pipeline_stats.frontier_coverage.origin_input_exclusions", text)
        self.assertNotIn("prompt", json.dumps(data["pipeline_stats"]))

    async def test_bad_origins_use_no_model_and_challenge_is_not_technical_reject(self):
        career, challenge = origin("1"), origin("2")
        career.url, career.title = "https://lab.example/research/careers/", "Research Careers"
        challenge.title = "Verifying your browser"
        create = AsyncMock(side_effect=AssertionError("No model call is allowed"))
        client = NS(chat=NS(completions=NS(create=create)))
        analyzer = ParadigmAnalyzer(client=client, model="offline", enable_batch_prefilter=True)
        values = await analyzer.run([career, challenge])
        self.assertEqual(values[0].rubric_assessment["decision"], "reject")
        self.assertNotIn(values[1].rubric_assessment.get("decision"), {"report", "reject", "observe", "deep_dive"})
        self.assertEqual(values[0].rubric_assessment["stage"], "origin_eligibility")
        create.assert_not_awaited()

    async def test_old_pending_challenge_and_nonresearch_candidate_do_not_spend(self):
        value = mixed_candidate()
        value.evidence[0].title = "Verifying your browser"
        before = copy.deepcopy(value.input_evidence)
        create = AsyncMock(side_effect=AssertionError("No model call is allowed"))
        client = NS(chat=NS(completions=NS(create=create)))
        await ParadigmSynthesizer(client=client, model="offline").run([value])
        self.assertEqual(value.rubric_assessment["decision"], "incomplete")
        self.assertEqual(value.input_evidence, before)
        create.assert_not_awaited()

    async def test_discovery_filters_all_native_entry_types_with_audit_reasons(self):
        clean, career, challenge = origin("1"), origin("2"), origin("3")
        career.url = "https://lab.example/research/careers/"
        challenge.title = "Verifying your browser"
        discovery = ParadigmDiscovery(lookback_days=7)
        async def fetch(source):
            values = [clean, career, challenge] if isinstance(source, ResearchFeedSource) else []
            return values, {"source": source.source_name, "status": "completed", "results": len(values)}
        discovery._bounded_fetch = fetch
        with research_window(NOW):
            batch = await discovery.run()
        self.assertEqual([row.fingerprint for row in batch.origins], [clean.fingerprint])
        reasons = batch.coverage["origin_input_exclusions"]
        self.assertEqual({row["reason"] for row in reasons}, {"recruitment_page", "access_challenge"})
        self.assertEqual([row["retryable"] for row in reasons], [False, True])

    async def test_official_details_keep_healthy_peers_and_durable_nonresearch_exclusions(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/research":
                return httpx.Response(200, text='<a href="/research/careers/">Research Careers</a>'
                    '<a href="/research/good">Training Mechanism</a><a href="/research/challenge">Another Mechanism</a>'
                    '<a href="/research/redirect">Research Update</a>')
            if request.url.path == "/research/redirect":
                return httpx.Response(302, headers={"Location": "/jobs/researcher"})
            if request.url.path == "/jobs/researcher":
                return httpx.Response(200, text='<title>Research scientist</title><p>Join us</p>')
            if request.url.path == "/research/challenge":
                return httpx.Response(200, text='<title>Verifying your browser</title><main>' + 'Enable JavaScript. ' * 30 + '</main>')
            if request.url.path == "/research/good":
                return httpx.Response(200, text='<title>Training Mechanism</title><main><article>'
                    '<time datetime="2026-10-20"></time><p>' + 'Real training signal and experimental limitations. ' * 10 + '</p></article></main>')
            raise AssertionError(str(request.url))
        def factory(**kwargs):
            return CLIENT(transport=httpx.MockTransport(handler), **kwargs)
        with tempfile.TemporaryDirectory() as directory, research_window(NOW):
            cache = ParadigmStore(Path(directory) / "state.db").campaigns
            for _ in range(2):
                source = PriorityResearchPageSource(pages=[INDEX], per_page=0, stage_cache=cache, client_factory=factory)
                values = await source.safe_fetch()
                self.assertEqual([row.title for row in values], ["Training Mechanism"])
                self.assertEqual(source.fetch_status, "partial")
                page = source.coverage()["pages"][INDEX]
                self.assertEqual(page["detail_failures"], 1)
                self.assertEqual(page["detail_pending"], 1)
                self.assertEqual(page["detail_exclusions"], [{"url": "https://lab.example/research/redirect", "reason": "recruitment_page"}])
                self.assertTrue(any(row["reason"] == "recruitment_page" for row in page["index_exclusions"]))
            self.assertEqual(calls.count("/research/good"), 1)
            self.assertEqual(calls.count("/research/challenge"), 2)
            self.assertEqual(calls.count("/research/redirect"), 1)
            self.assertNotIn("/research/careers/", calls)

    async def test_access_challenge_cannot_be_forged_as_completed_exclusion(self):
        with research_window(NOW):
            queue = OfficialDetailQueue(INDEX, 7)
        link = _ResearchLink(title="Report", url=INDEX + "/report")
        queue.merge([link])
        with self.assertRaises(ValueError):
            queue.complete(link, None, outside=False, exclusion_reason="access_challenge")
        self.assertEqual(queue.pending_count, 1)

    async def test_200_challenge_header_on_index_is_not_a_healthy_catalog(self):
        requests = []
        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(200, headers={"cf-mitigated": "challenge"},
                                  text='<a href="/research/paper">Research mechanism</a>')
        with research_window(NOW):
            source = PriorityResearchPageSource(pages=[INDEX], per_page=0,
                client_factory=lambda **kwargs: CLIENT(transport=httpx.MockTransport(handler), **kwargs))
            self.assertEqual(await source.safe_fetch(), [])
        self.assertEqual(requests, [INDEX])
        self.assertEqual(source.coverage()["pages"][INDEX]["error"], "access_challenge")
        self.assertEqual(source.coverage()["pages"][INDEX]["status"], "request_failed")

    async def test_linked_html_failover_uses_clean_real_body_not_challenge(self):
        value = origin()
        value.identifiers = {}
        value.raw = {"linked_research_documents": [{"url": "https://lab.example/challenge"}, {"url": "https://lab.example/report"}]}
        def handler(request):
            if request.url.path == "/challenge":
                return httpx.Response(200, text='<title>Verifying your browser</title>')
            return httpx.Response(200, text='<div>PAGE SHELL</div><article><p>Real mechanism with limits.</p></article>')
        with patch("sources.arxiv_document_source.httpx.AsyncClient",
                   side_effect=lambda **kwargs: CLIENT(transport=httpx.MockTransport(handler), **kwargs)):
            await ArxivDocumentClient().hydrate(value)
        self.assertEqual(value.raw["document_excerpt"], "Real mechanism with limits.")
        self.assertEqual(value.raw["document_source_url"], "https://lab.example/report")

    async def test_html_feed_summary_uses_body_not_ui(self):
        xml = '''<rss><channel><item><title>Learning a new mechanism</title><link>https://lab.example/new</link>
            <description><![CDATA[<nav>MENU NOISE</nav><main><p>Actual technical mechanism and limits.</p></main>]]></description>
            <pubDate>Tue, 20 Oct 2026 00:00:00 GMT</pubDate></item></channel></rss>'''
        with research_window(NOW):
            values = ResearchFeedSource()._parse(xml, "https://lab.example/feed")
        self.assertEqual(values[0].summary, "Actual technical mechanism and limits.")

