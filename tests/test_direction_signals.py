"""Free direction context must never become technical or freshness evidence."""
import asyncio
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from xml.sax.saxutils import escape

import httpx
import config
from paradigms.models import candidate_from_dict
from paradigms.scoring import score_candidate
from paradigms.momentum import momentum_assessment
from paradigms.enrichment import EvidenceEnricher
from paradigms.evidence_association import support_revision
from reports.paradigm_generator import _compact_route_dossier, _ungrounded_url_violations, _route_fragment_key
from runtime_clock import research_window
from sources.direction_signal_source import (DirectionSignalClient, REGISTRY, topic_origins,
    cohort_query, parse_cohort, parse_capital_feed, parse_jobs, context_for_report)
from tests.test_paradigm_pipeline import candidate, paper

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


def registry():
    value = json.loads(REGISTRY.read_text())
    value["bounds"]["arxiv_topics"] = 1
    return value


def route():
    primary = paper()
    primary.title = "Efficient Diffusion Language Models"
    primary.summary = "A new diffusion language model training signal."
    primary.published_at = (NOW - timedelta(days=1)).isoformat()
    return candidate([primary])


def atom(*, total=2, duplicates=False, old=False):
    rows = []
    for index, days in enumerate((10, 40)):
        identifier = "2610.00001" if index == 0 or duplicates else "2609.00002"
        date = NOW - timedelta(days=90 if old else days)
        rows.append(f'<entry><id>https://arxiv.org/abs/{identifier}v2</id><published>{date.isoformat()}</published>'
                    f'<updated>{NOW.isoformat()}</updated><title>Diffusion Language Models {index}</title></entry>')
    return ('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
            f'<opensearch:totalResults>{total}</opensearch:totalResults>' + "".join(rows) + '</feed>')


def feed(title="扩散语言模型公司完成A轮融资", *, date="Sun, 01 Nov 2026 10:00:00 GMT", url="https://news.example/funding"):
    return f'<rss><channel><item><title>{escape(title)}</title><link>{escape(url)}</link><pubDate>{date}</pubDate><description>来自公开RSS的摘要。</description></item></channel></rss>'


def jobs():
    return {"jobs": [{"id": 1, "internal_job_id": 7, "title": "Research Engineer - Diffusion Language Models",
        "absolute_url": "https://job-boards.greenhouse.io/togetherai/jobs/1", "updated_at": NOW.isoformat(),
        "content": "Open research role; diffusion language models. No applicant data."}]}


class DirectionProtocolTests(unittest.TestCase):
    def test_capital_is_reported_event_not_closed_deal_and_requires_real_date(self):
        source = registry()["media_feeds"][0]
        result = parse_capital_feed(feed(), source, now=NOW)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "reported_capital_event")
        for date in ("", "Sun, 01 Nov 2020 10:00:00 GMT", "Sun, 01 Nov 2027 10:00:00 GMT"):
            self.assertEqual(parse_capital_feed(feed(date=date), source, now=NOW), [])
        self.assertEqual(parse_capital_feed(feed(title="New diffusion research raises questions"), source, now=NOW), [])

    def test_http_200_html_and_bad_schema_are_not_empty_success(self):
        with self.assertRaises(ValueError):
            parse_capital_feed('<html><body>Subscribe</body></html>', registry()["media_feeds"][0], now=NOW)
        for value in ({"error": "blocked"}, {"jobs": [{}]}, []):
            with self.assertRaises(ValueError):
                parse_jobs(value, registry()["job_boards"][0])

    def test_hiring_dedupes_locations_and_never_treats_modified_as_first_published(self):
        value = jobs()
        second = copy.deepcopy(value["jobs"][0]); second["id"] = 2
        value["jobs"].append(second)
        value["jobs"].append({"id": 3, "title": "Recruiting Coordinator", "absolute_url": "https://jobs.example/3"})
        result = parse_jobs(value, registry()["job_boards"][0])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["published_at"], "")
        self.assertEqual(result[0]["kind"], "hiring_intent")

    def test_equal_utc_windows_first_dates_and_recomputable_members(self):
        topic = registry()["specialized_topics"][0]
        query, start, middle, end = cohort_query(topic, now=NOW, days=28)
        self.assertEqual(middle - start, end - middle)
        result = parse_cohort(atom(), topic, now=NOW, days=28, query=query)
        self.assertEqual((result["current_count"], result["previous_count"]), (1, 1))
        self.assertEqual(len(result["members"]), 2)
        self.assertEqual(result["status"], "complete")
        self.assertIn("submittedDate", query)

    def test_member_cap_duplicates_old_first_dates_and_error_feeds_do_not_claim_growth(self):
        topic = registry()["specialized_topics"][0]
        query, *_ = cohort_query(topic, now=NOW, days=28)
        value = parse_cohort(atom(total=2000), topic, now=NOW, days=28, query=query)
        self.assertEqual(value["status"], "incomplete_member_limit")
        self.assertIsNone(value["current_count"])
        for text in (atom(duplicates=True), atom(old=True), '<error>Busy</error>'):
            with self.assertRaises(ValueError):
                parse_cohort(text, topic, now=NOW, days=28, query=query)

    def test_specialized_topics_do_not_borrow_broad_multimodal_counts(self):
        value = route()
        self.assertEqual(set(topic_origins(value, registry())), {"diffusion_language"})


class DirectionCollectionTests(unittest.IsolatedAsyncioTestCase):
    def client(self, *, broken_media=False, bad_atom=False):
        calls = []
        def handle(request):
            calls.append(request)
            host = request.url.host
            if host == "export.arxiv.org":
                return httpx.Response(200, text='<error>Busy</error>' if bad_atom else atom())
            if host == "boards-api.greenhouse.io":
                return httpx.Response(200, json=jobs())
            if host == "techcrunch.com" and broken_media:
                return httpx.Response(200, text='<html>Not a feed</html>')
            return httpx.Response(200, text=feed(url=f'https://{host}/funding'))
        def factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handle), **kwargs)
        return DirectionSignalClient(client_factory=factory, registry=registry()), calls

    async def test_sources_shared_between_candidates_and_no_paid_calls_or_private_jobs_retention(self):
        client, calls = self.client()
        first, second = route(), route(); second.key += "-second"
        with research_window(NOW):
            await client.attach([first, second])
            await client.attach([first])
            context = context_for_report(first)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(request.method == "GET" for request in calls))
        self.assertTrue(all("authorization" not in request.headers for request in calls))
        self.assertEqual(context["topics"][0]["paper_activity"]["current_count"], 1)
        self.assertEqual(context["topics"][0]["jobs"][0]["published_at"], "")
        self.assertNotIn("summary", first.direction_context["topics"][0]["jobs"][0])
        self.assertNotIn("applicant", json.dumps(first.direction_context))
        self.assertIsNot(first.direction_context, second.direction_context)

    async def test_one_source_failure_and_failed_cohort_preserve_other_context(self):
        client, _ = self.client(broken_media=True, bad_atom=True)
        value = route()
        with research_window(NOW):
            await client.attach([value])
            result = context_for_report(value)
        self.assertTrue(result["source_health"]["TechCrunch"].startswith("failed_"))
        self.assertEqual(len(result["topics"][0]["media"]), 1)
        self.assertTrue(result["topics"][0]["jobs"])
        self.assertNotIn("current_count", result["topics"][0]["paper_activity"])

    async def test_evidence_exclusion_foreign_route_source_change_and_old_context_do_not_lend_facts_or_urls(self):
        client, _ = self.client()
        value = route()
        with research_window(NOW):
            await client.attach([value])
            for kind in ("route", "source", "exclude", "query_version"):
                changed = copy.deepcopy(value)
                if kind == "route": changed.key += "foreign"
                if kind == "source": changed.evidence[0].source_revision = "new"
                if kind == "exclude": changed.evidence = []
                if kind == "query_version": changed.direction_context["version"] = "wrong"
                self.assertEqual(context_for_report(changed), {}, kind)
            link = value.direction_context["topics"][0]["media"][0]["url"]
            self.assertEqual(_ungrounded_url_violations(f'[source]({link})', [value]), [])
            other = copy.deepcopy(value); other.key += "foreign"
            self.assertTrue(_ungrounded_url_violations(f'[source]({link})', [other]))
        with research_window(NOW + timedelta(days=8)):
            self.assertEqual(context_for_report(value)["as_of"], NOW.isoformat())
            self.assertEqual(_ungrounded_url_violations(f'[source]({link})', [value]), [])

    async def test_edited_counts_are_recounted_instead_of_trusted(self):
        client, _ = self.client()
        value = route()
        with research_window(NOW):
            await client.attach([value])
            value.direction_context["topics"][0]["paper_activity"]["current_count"] = 9000
            self.assertNotIn("current_count", context_for_report(value)["topics"][0]["paper_activity"])

    async def test_context_roundtrip_and_write_cache_without_changing_technical_eligibility_or_work_momentum(self):
        client, _ = self.client()
        value = route()
        with research_window(NOW):
            score_candidate(value)
            before = (value.rubric_assessment, value.status, value.total_score, value.report_signature)
            key = _route_fragment_key(value)
            await client.attach([value])
            score_candidate(value)
            self.assertEqual((value.rubric_assessment, value.status, value.total_score, value.report_signature), before)
            self.assertEqual(momentum_assessment(value)["state"], "early_signal")
            self.assertIn("同口径题名样本提交增量", momentum_assessment(value)["available_context_dimensions"])
            self.assertNotEqual(_route_fragment_key(value), key)
            restored = candidate_from_dict(json.loads(json.dumps(value.to_dict())))
            self.assertEqual(context_for_report(restored), context_for_report(value))
            self.assertIn("direction_context", _compact_route_dossier(value))

    async def test_phase_timeout_is_unknown_not_global_research_failure(self):
        client, _ = self.client()
        value = route()
        timeout = asyncio.timeout
        async def slow(*args): await asyncio.sleep(1)
        with research_window(NOW), patch.object(client, "_get", side_effect=slow), patch(
                "sources.direction_signal_source.asyncio.timeout", side_effect=lambda _: timeout(.005)):
            await client.attach([value])
            self.assertEqual(value.direction_context["source_health"]["phase"], "time_budget_interrupted")
            self.assertTrue(value.evidence)

    async def test_enrichment_wrapper_calls_optional_context_but_never_opens_it_when_disabled(self):
        value = route()
        runner = EvidenceEnricher()
        runner._external_enrichment = AsyncMock()
        runner.direction_signals = SimpleNamespace(attach=AsyncMock())
        with patch.object(config, "PARADIGM_DIRECTION_SIGNALS_ENABLED", True):
            self.assertEqual(await runner.run([value], []), [value])
        runner.direction_signals.attach.assert_awaited_once_with([value])
        runner.direction_signals.attach.reset_mock()
        with patch.object(config, "PARADIGM_DIRECTION_SIGNALS_ENABLED", False):
            await runner.run([value], [])
        runner.direction_signals.attach.assert_not_awaited()

    async def test_present_hiring_snapshot_is_not_backdated_to_a_resumed_old_run(self):
        client, _ = self.client()
        value = route()
        with research_window(NOW, observation_time=NOW + timedelta(days=8)):
            await client.attach([value])
            context = context_for_report(value)
        self.assertEqual(context["topics"][0]["jobs"], [])
        self.assertEqual(context["source_health"]["Together AI"], "current_jobs_not_historical_snapshot")


if __name__ == '__main__':
    unittest.main()
