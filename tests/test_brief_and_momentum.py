"""Short writing, action-based reading order, and free secondary lead checks."""
import copy
import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from paradigms.evidence_validation import certify_synthesis_discussion
from paradigms.models import EvidenceType, TechnicalEvidence, is_verified_substantive_discussion, scrub_ephemeral_evidence
from paradigms.momentum import editorial_priority, momentum_assessment
from paradigms.propagation import certify_diffusion
from paradigms.scoring import score_candidate, is_reportable
from paradigms.enrichment import EvidenceEnricher
from paradigms.analyzer import _bounded_synthesis_evidence
from reports.paradigm_generator import (ParadigmReportGenerator, _compact_route_dossier,
    _group_route_drafts, _route_fragment_key, _route_draft_violations)
from skills.loader import SkillLoader
from sources.secondary_page_source import SecondaryPageReader, _public_host
from tests.test_propagation_policy import route, post, NOW
from runtime_clock import research_window
from run_audit import run_audit


def analysis(value):
    return TechnicalEvidence(source="curated-kol-blog", evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
        title="A substantive review", url="https://review.example/review", authors=["Bob Reviewer"],
        summary="Detailed analysis of this work's assumptions, training signals and experimental limitations.",
        published_at="2026-10-25T00:00:00+00:00", raw={"independence": "independent", "relationship": "independent_commentary"})


class MomentumBriefTests(unittest.TestCase):
    def setUp(self):
        clock = research_window(NOW)
        clock.__enter__()
        self.addCleanup(clock.__exit__, None, None, None)

    def test_low_attention_remains_publishable_and_is_explicitly_early(self):
        item = route(age=1)
        score_candidate(item)
        self.assertTrue(is_reportable(item))
        self.assertEqual(momentum_assessment(item)["state"], "early_signal")
        self.assertEqual(len(momentum_assessment(item)["unmeasured_dimensions"]), 3)

    def test_one_ordinary_share_is_attention_not_technical_action(self):
        item = route(age=1)
        item.evidence.append(post(item))
        certify_diffusion(item)
        report = momentum_assessment(item)
        self.assertEqual(report["state"], "observed_attention")
        self.assertEqual(report["counts"]["external_action"], 0)
        self.assertNotIn("author_followers", report["records"][0]["metrics"])

    def test_fame_search_results_and_official_stars_do_not_manufacture_external_action(self):
        item = route(age=1)
        item.publisher_tier = "established"
        item.evidence.extend([post(item, indexed=True), TechnicalEvidence(source="github",
            evidence_type=EvidenceType.IMPLEMENTATION, title="Official code", url="https://github.com/team/method",
            metrics={"stars": 1000000}, raw={"independence": "official"})])
        self.assertEqual(momentum_assessment(item)["state"], "early_signal")

    def test_author_self_exposure_does_not_count_as_outside_attention(self):
        item = route(age=1)
        share = post(item)
        share.raw["relationship"] = "author_self_release"
        item.evidence.append(share)
        certify_diffusion(item)
        data = momentum_assessment(item)
        self.assertEqual(data["state"], "early_signal")
        self.assertEqual(data["counts"]["publisher_exposure"], 1)

    def test_mirrors_of_the_same_event_not_summed_missing_counters_not_zero(self):
        item = route(age=1)
        share = post(item)
        share.metrics = {"score": 9, "author_followers": 999999}
        mirror = copy.deepcopy(share)
        mirror.metrics = {"score": 12, "comments": float("nan")}
        item.evidence += [share, mirror]
        certify_diffusion(item)
        data = momentum_assessment(item)
        self.assertEqual(data["counts"]["public_attention"], 1)
        self.assertEqual(data["records"][0]["metrics"], {"score": 12})

    def test_action_first_keeps_every_route_without_changing_technical_scores(self):
        quiet, shared, acted = [route(i, age=1) for i in (1, 2, 3)]
        quiet.total_score = 100
        shared.evidence.append(post(shared))
        certify_diffusion(shared)
        review = analysis(acted)
        acted.evidence.append(review)
        self.assertTrue(certify_synthesis_discussion(acted, review))
        before = [copy.deepcopy(item.to_dict()) for item in (quiet, shared, acted)]
        ordered = sorted([quiet, shared, acted], key=editorial_priority)
        self.assertEqual(ordered, [acted, shared, quiet])
        self.assertEqual([item.to_dict() for item in (quiet, shared, acted)], before)
        grouped = "\n".join(_group_route_drafts([quiet, shared, acted], ["QUIET", "SHARED", "ACTED"]))
        self.assertLess(grouped.index("ACTED"), grouped.index("SHARED"))
        self.assertLess(grouped.index("SHARED"), grouped.index("QUIET"))
        for body in ("QUIET", "SHARED", "ACTED"):
            self.assertEqual(grouped.count(body), 1)

    def test_fake_old_popularity_prose_is_not_unlocked_by_one_valid_review(self):
        item = route(age=1)
        review = analysis(item)
        item.evidence.append(review)
        certify_synthesis_discussion(item, review)
        item.objective_momentum_signals = ["资本涌入，论文暴增100倍"]
        item.trend_interpretation = "已成为全行业潮流"
        data = _compact_route_dossier(item)
        self.assertNotIn("资本涌入", str(data["objective_momentum_signals"]))
        self.assertNotIn("全行业潮流", data["trend_interpretation"])
        self.assertEqual(data["momentum_assessment"]["state"], "external_action")

    def test_overflow_and_old_signals_do_not_invent_missing_coverage(self):
        item = route(age=1)
        for number in range(12):
            value = post(item)
            value.url = f"https://news.ycombinator.com/item?id={number}"
            item.evidence.append(value)
        certify_diffusion(item)
        data = momentum_assessment(item, record_limit=3)
        self.assertEqual((len(data["records"]), data["overflow_record_count"]), (3, 9))
        self.assertEqual(data["counts"]["public_attention"], 12)

    def test_future_and_signed_events_cannot_lend_momentum(self):
        item = route(age=1)
        item.evidence.append(post(item, age=-1))
        value = analysis(item)
        value.url += "?Signature=temporary"
        item.evidence.append(value)
        certify_synthesis_discussion(item, value)
        certify_diffusion(item)
        self.assertEqual(momentum_assessment(item)["state"], "early_signal")

    def test_runtime_skill_composition_and_cache_follow_method_changes(self):
        method = SkillLoader().mental_model_method()
        self.assertLess(method.index("paper-mental-model"), method.index("# 技术心智模型"))
        item = route(age=1)
        key = _route_fragment_key(item)
        with patch.object(SkillLoader, "mental_model_method", return_value=method + " new method"):
            self.assertNotEqual(_route_fragment_key(item), key)


def web_lead(value, number=1):
    return TechnicalEvidence(source="tavily-web", evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
        title=value.evidence[0].title, url=f"https://analysis.example/post-{number}", summary="Search snippet only.",
        published_at="2026-10-25", raw={"indexed_discovery_only": True, "ephemeral_content": True})


class SecondaryReaderTests(unittest.IsolatedAsyncioTestCase):
    def page(self, value, *, author="Bob Reviewer", title=None, date=""):
        return (f'<html><head><meta name="author" content="{author}">'
                f'<meta property="article:published_time" content="{date}"></head><body><article>'
                f'<h1>{title or value.evidence[0].title}</h1>' +
                "The method changes the information flow and training objective; the claimed gains remain uncertain. " * 55 +
                '</article></body></html>')

    def reader(self, responses):
        calls = []
        def handle(request):
            calls.append(str(request.url))
            return responses(request)
        def factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handle), **kwargs)
        return SecondaryPageReader(client_factory=factory), calls

    async def test_real_html_read_is_not_automatic_uptake_and_enters_existing_synthesis(self):
        value = route(age=1)
        lead = web_lead(value)
        lead.raw.pop("ephemeral_content")  # Legacy leads must not retain third-party bodies publicly.
        value.evidence.append(lead)
        reader, calls = self.reader(lambda req: httpx.Response(200, text=self.page(value), headers={"content-type": "text/html"}))
        with patch("sources.secondary_page_source._public_host", AsyncMock()):
            await reader.read(value)
        self.assertEqual(len(calls), 1)
        self.assertEqual(lead.source, "read-web-article")
        self.assertEqual(lead.published_at, "")
        self.assertFalse(is_verified_substantive_discussion(lead, route_key=value.key))
        material = _bounded_synthesis_evidence(value.evidence, candidate=value)
        self.assertEqual(next(row for row in material["records"] if row["index"] == 1)["summary"], lead.summary)
        self.assertGreater(len(lead.summary), 700)
        self.assertTrue(certify_synthesis_discussion(value, lead))
        self.assertEqual(len(lead.raw["index_summary"]), len("Search snippet only."))
        scrub_ephemeral_evidence(lead)
        self.assertEqual(lead.summary, "")
        self.assertNotIn("index_summary", lead.raw)
        self.assertNotIn("secondary_read_receipt", lead.raw)
        self.assertTrue(is_verified_substantive_discussion(lead, route_key=value.key))

    async def test_access_failures_stay_index_leads_and_only_two_attempts(self):
        value = route(age=1)
        value.evidence += [web_lead(value, i) for i in range(5)]
        reader, calls = self.reader(lambda req: httpx.Response(403))
        before = copy.deepcopy(value.evidence)
        with patch("sources.secondary_page_source._public_host", AsyncMock()):
            await reader.read(value)
        self.assertEqual(len(calls), 2)
        self.assertEqual([(item.summary, item.source, item.published_at) for item in value.evidence],
                         [(item.summary, item.source, item.published_at) for item in before])
        self.assertTrue(all(item.raw.get("indexed_discovery_only") for item in value.evidence[1:]))
        self.assertTrue(value.evidence[1].raw["secondary_read_status"].startswith("failed_"))

    async def test_unrelated_challenge_anonymous_and_self_authored_pages_are_not_uptake(self):
        for kind in ("unrelated", "challenge", "anonymous", "self"):
            value = route(age=1)
            lead = web_lead(value)
            value.evidence.append(lead)
            body = self.page(value, title="A different unrelated study" if kind == "unrelated" else None,
                             author="" if kind == "anonymous" else value.evidence[0].authors[0] if kind == "self" else "Bob")
            reader, _ = self.reader(lambda req: httpx.Response(200, text=body,
                headers={"content-type": "text/html", **({"cf-mitigated": "challenge"} if kind == "challenge" else {})}))
            with patch("sources.secondary_page_source._public_host", AsyncMock()):
                await reader.read(value)
            self.assertFalse(certify_synthesis_discussion(value, lead), kind)
            if kind != "self":
                self.assertTrue(lead.raw["indexed_discovery_only"], kind)

    async def test_receipt_cannot_authorize_foreign_route_or_changed_body_or_origin(self):
        value = route(age=1)
        lead = web_lead(value)
        value.evidence.append(lead)
        reader, _ = self.reader(lambda req: httpx.Response(200, text=self.page(value), headers={"content-type": "text/html"}))
        with patch("sources.secondary_page_source._public_host", AsyncMock()):
            await reader.read(value)
        for kind in ("route", "body", "origin"):
            other = copy.deepcopy(value)
            if kind == "route": other.key += "foreign"
            if kind == "body": other.evidence[1].summary += " changed"
            if kind == "origin": other.evidence[0].source_revision = "new-version"
            self.assertFalse(certify_synthesis_discussion(other, other.evidence[1]), kind)

    async def test_download_cache_is_shared_not_recharged_or_aliased(self):
        first = route(age=1)
        second = copy.deepcopy(first)
        first.evidence.append(web_lead(first))
        second.evidence.append(web_lead(second))
        reader, calls = self.reader(lambda req: httpx.Response(200, text=self.page(first), headers={"content-type": "text/html"}))
        with patch("sources.secondary_page_source._public_host", AsyncMock()):
            await reader.read(first)
            await reader.read(second)
        self.assertEqual(len(calls), 1)
        self.assertIsNot(first.evidence[1], second.evidence[1])

    async def test_redirect_and_dns_local_targets_are_rejected_before_http(self):
        value = route(age=1)
        value.evidence.append(web_lead(value))
        reader, calls = self.reader(lambda req: httpx.Response(302, headers={"location": "https://127.0.0.1/private"}))
        async def check(url):
            if "127.0.0.1" in url:
                raise ValueError("not public")
        with patch("sources.secondary_page_source._public_host", side_effect=check):
            await reader.read(value)
        self.assertEqual(len(calls), 1)
        for url in ("http://public.example", "https://user:pass@public.example", "https://public.example:8443",
                    "https://public.example/post?Signature=x", "https://www.reddit.com/comments/123"):
            with self.assertRaises(ValueError):
                await _public_host(url)
        with patch("sources.secondary_page_source.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("10.0.0.1", 443))]):
            with self.assertRaises(ValueError):
                await _public_host("https://public.example")

    async def test_optional_deadline_leaves_unknown_lead_and_returns_normally(self):
        value = route(age=1)
        value.evidence.append(web_lead(value))
        reader = SecondaryPageReader()
        timeout = asyncio.timeout
        async def slow(url):
            await asyncio.sleep(1)
        with patch.object(reader, "_page", side_effect=slow), patch(
                "sources.secondary_page_source.asyncio.timeout", side_effect=lambda _: timeout(.005)):
            await reader.read(value)
        self.assertTrue(value.evidence[1].raw["indexed_discovery_only"])
        self.assertEqual(value.evidence[1].raw["secondary_read_status"], "time_budget_interrupted")

    async def test_old_index_upgraded_to_actual_body_is_revisited_without_metric_change(self):
        value = route(age=25)
        value.evidence.append(web_lead(value))
        enricher = EvidenceEnricher()
        enricher.allow_partial_sources = True
        enricher.community = SimpleNamespace(search=AsyncMock(return_value=[]), coverage=lambda: {})
        page = {"title": value.evidence[0].title, "text": self.page(value), "url": value.evidence[1].url,
                "authors": ["Bob Reviewer"], "published_at": "2026-10-25"}
        with research_window(NOW), patch.object(enricher.secondary_pages, "_page", AsyncMock(return_value=page)):
            changed = await enricher.refresh([value], [])
        self.assertEqual(changed, [value])
        self.assertEqual(value.evidence[1].source, "read-web-article")
        self.assertFalse(is_verified_substantive_discussion(value.evidence[1], route_key=value.key))

    async def test_future_article_metadata_cannot_be_borrowed_as_current_event(self):
        value = route(age=1)
        value.evidence.append(web_lead(value))
        reader, _ = self.reader(lambda req: httpx.Response(200, text=self.page(value, date="2027-01-01"),
                                                        headers={"content-type": "text/html"}))
        with research_window(NOW), patch("sources.secondary_page_source._public_host", AsyncMock()):
            await reader.read(value)
        self.assertTrue(value.evidence[1].raw["indexed_discovery_only"])
        self.assertEqual(value.evidence[1].raw["secondary_read_status"], "future_date")


class ShortWritingTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_mechanism_passes_existing_route_safety_gate_in_one_mock_call(self):
        run_audit.reset()
        self.addCleanup(run_audit.reset)
        value = route(age=1)
        text = ("### 让状态表示直接服务行动选择\n\n"
            "旧方案预测下一帧，行动选择还要从像素中反推状态变化。新方法把变化压成可行动的中间表示，"
            "用视频中的前后状态作为学习信号；运行时先预测这种变化，再交给控制接口。关键改动落在"
            "预测对象，减少了控制器从画面重新提取变化的负担。\n\n"
            "例如作为教学示意，机器人先选择开门造成的状态变化，再由控制器生成实际动作。"
            "这不等于模型已经知道执行所需的全部物理条件。当前证据仍限于原文的受控对照，"
            "长时程与新场景的控制收益尚待验证。"
            f"[查看原文]({value.evidence[0].url})。\n\n"
            "**讨论势能判断：** 本轮仅确认一手团队提出机制，尚无可核验的外部承接。"
            "后续关注独立控制实验能否使用这种表示，并报告真实任务完成率；未覆盖的平台不计作零讨论。")
        self.assertTrue(200 <= len(re.findall(r"[\u4e00-\u9fff]", text)) <= 450)
        self.assertEqual(_route_draft_violations(text, value), [])
        call = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=text))]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call)))
        with tempfile.TemporaryDirectory() as folder, research_window(NOW):
            result = await ParadigmReportGenerator(folder, client=client, model="offline")._draft_one_route(
                "2026-10-26", value, index=1, total=1)
        self.assertEqual(result, text)
        self.assertEqual(call.await_count, 1)
        self.assertEqual(call.await_args.kwargs["max_tokens"], 1600)
        prompt = call.await_args.kwargs["messages"][0]["content"]
        self.assertIn("paper-mental-model", prompt)
        self.assertIn("方向势能：看谁在用行动下注", prompt)
        self.assertIn('"momentum_assessment"', prompt)
        self.assertNotIn("## 六、对话模式", prompt)


if __name__ == "__main__":
    unittest.main()
