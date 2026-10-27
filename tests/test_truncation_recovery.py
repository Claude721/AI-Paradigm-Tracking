"""Output-limit recovery: preserve work, not imaginary completed judgments."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import config
import research_archive as archive
from agents.llm_utils import (
    completed_content, parse_json_completion, readable_markdown_completion,
    recovery_fragment, repair_messages,
)
from paradigms.analyzer import ParadigmAnalyzer, ParadigmSynthesizer, ResearcherTrajectoryAnalyzer
from paradigms.models import ResearcherProfile
from reports.paradigm_generator import ParadigmReportGenerator, _route_draft_violations
from run_audit import run_audit
from tests import test_production_failure_regressions as fixture
from tests import test_research_archive as archive_fixture
from tests.test_paradigm_pipeline import rubric_answers
from tests.test_trajectory_contract import payload as person_payload
from tests.test_weekly_publication import prose


def response(content, reason="length"):
    if isinstance(content, dict):
        content = json.dumps(content, ensure_ascii=False)
    return NS(choices=[NS(finish_reason=reason, message=NS(content=content,
              reasoning_content="PRIVATE COT NEVER FORWARDED"))],
              usage=NS(prompt_tokens=100, completion_tokens=200, total_tokens=300))


def client_for(*responses):
    create = AsyncMock(side_effect=responses)
    return NS(chat=NS(completions=NS(create=create))), create


def extraction_payload():
    return {"hypotheses": [{"canonical_name": "状态预测机制", "problem_shift": "从像素转向状态",
        "mechanism": "学习状态转移", "innovation_types": ["architecture"],
        "rubric_answers": rubric_answers()}]}


class VisibleOutputTests(unittest.TestCase):
    def test_complete_length_json_is_usable_without_inventing_delimiters(self):
        for text in ('{"ok":true}', '```json\n{"ok":true}\n```', '```json\n{"ok":true}'):
            self.assertEqual(parse_json_completion(response(text)), {"ok": True})

    def test_incomplete_or_ambiguous_json_never_becomes_a_judgment(self):
        for text in ('', '{"name":"半句', '{"rows":[{"ok":true}]',
                     '{"ok":true} {"ok":false}', '{"a":1,"a":2}', '{"a":NaN}',
                     '{"outer":{"ok":true}', '[{"ok":true}]', '前缀 {"ok":true}'):
            with self.subTest(text=text), self.assertRaises(ValueError) as error:
                parse_json_completion(response(text))
            if text:
                self.assertNotIn(text, str(error.exception))

    def test_safety_and_tool_stops_are_not_length_recovery(self):
        for reason in ("content_filter", "tool_calls", "function_call", "", None):
            value = response('{"ok":true}', reason)
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                parse_json_completion(value)
            self.assertIsNone(recovery_fragment(value))

    def test_only_visible_text_is_forwarded_as_untrusted_draft(self):
        value = response('{"result":"原回答尚未结束')
        messages = repair_messages("原始证据与规则", recovery_fragment(value))
        self.assertEqual(messages[1], {"role": "assistant", "content": value.choices[0].message.content})
        self.assertIn("不可信草稿", messages[-1]["content"])
        self.assertNotIn("PRIVATE COT", json.dumps(messages))
        empty = repair_messages("原始证据", recovery_fragment(response("")))
        self.assertEqual([row["role"] for row in empty], ["user", "user"])

    def test_readable_markdown_is_retained_but_half_sentences_retry(self):
        text = "### 技术机制\n\n中文结论完整，并保留未知边界。"
        self.assertEqual(readable_markdown_completion(response(text)), text)
        for text in ("", "中文结论写到一半", "```python\nprint('unfinished')", "**中文结论：**"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                readable_markdown_completion(response(text))
        # Other callers must explicitly opt into a format-specific gate.
        with self.assertRaises(ValueError):
            completed_content(response("可读。"))


class StageRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        run_audit.reset()
        self.addCleanup(run_audit.reset)

    async def test_complete_truncated_synthesis_passes_real_contract_in_one_call(self):
        item = fixture.mixed_candidate()
        client, create = client_for(response(fixture.synthesis_payload([0])))
        await ParadigmSynthesizer(client=client, model="offline").run([item])
        self.assertEqual(create.await_count, 1)
        self.assertNotEqual(item.rubric_assessment["decision"], "incomplete")
        self.assertEqual(run_audit.token_totals()["llm_truncated_accepted_count"], 1)
        self.assertEqual(run_audit.token_totals()["llm_failed_count"], 0)

    async def test_broken_synthesis_reuses_visible_prefix_once_at_same_cap(self):
        item = fixture.mixed_candidate()
        fragment = '{"name":"已有技术分析","mental_model":'
        client, create = client_for(response(fragment), response(fixture.synthesis_payload([0]), "stop"))
        await ParadigmSynthesizer(client=client, model="offline").run([item])
        self.assertNotEqual(item.rubric_assessment["decision"], "incomplete")
        self.assertEqual(create.await_count, 2)
        repair = create.await_args_list[1].kwargs
        self.assertEqual(repair["messages"][1]["content"], fragment)
        self.assertIn(item.input_evidence[0].summary, repair["messages"][0]["content"])
        self.assertEqual([row.kwargs["max_tokens"] for row in create.await_args_list], [5600, 5600])
        totals = run_audit.token_totals()
        self.assertEqual(totals["llm_total_tokens"], 600)
        self.assertEqual(totals["llm_repair_needed_count"], 1)

    async def test_unreadable_and_empty_answers_get_deferred_repair_after_first_visits(self):
        for fragment in ('{"name":"半句', ''):
            with self.subTest(fragment=fragment):
                item = fixture.mixed_candidate()
                client, create = client_for(response(fragment), response(fixture.synthesis_payload([0])))
                synth = ParadigmSynthesizer(client=client, model="offline")
                synth.defer_retries = True
                await synth.run([item])
                self.assertEqual(create.await_count, 1)
                self.assertIn(item.key, synth._deferred_repairs)
                self.assertEqual(item.rubric_assessment["decision"], "incomplete")
                await synth.run([item])
                self.assertEqual(create.await_count, 2)
                self.assertNotIn(item.key, synth._deferred_repairs)
                self.assertNotEqual(item.rubric_assessment["decision"], "incomplete")

    async def test_repeated_truncation_stays_pending_and_cannot_loop_repairs(self):
        item = fixture.mixed_candidate()
        original = copy.deepcopy(item.mechanism)
        client, create = client_for(response('{"mechanism":"未核验'), response('still unreadable'))
        synth = ParadigmSynthesizer(client=client, model="offline")
        synth.defer_retries = True
        await synth.run([item])
        await synth.run([item])
        self.assertEqual(create.await_count, 2)
        self.assertNotIn(item.key, synth._deferred_repairs)
        self.assertEqual(item.rubric_assessment["decision"], "incomplete")
        self.assertEqual(item.mechanism, original)

    async def test_model_mode_change_cannot_reuse_a_previous_fragment(self):
        item = fixture.mixed_candidate()
        client, create = client_for(response('{"name":"OLD PRIVATE DRAFT'), response(fixture.synthesis_payload([0])))
        synth = ParadigmSynthesizer(client=client, model="offline")
        synth.defer_retries = True
        with patch.object(config, "MAIN_AGENT_REQUEST_OPTIONS", ""):
            await synth.run([item])
        with patch.object(config, "MAIN_AGENT_REQUEST_OPTIONS", '{"extra_body":{"reasoning_effort":"low"}}'):
            await synth.run([item])
        self.assertNotIn("OLD PRIVATE DRAFT", json.dumps(create.await_args_list[1].kwargs))
        self.assertEqual(len(create.await_args_list[1].kwargs["messages"]), 1)

    async def test_source_revision_change_cannot_reuse_a_previous_fragment(self):
        item = fixture.mixed_candidate()
        client, create = client_for(response('{"name":"OLD PRIVATE DRAFT'), response(fixture.synthesis_payload([0])))
        synth = ParadigmSynthesizer(client=client, model="offline")
        synth.defer_retries = True
        await synth.run([item])
        item.evidence[0].raw["document_excerpt"] = "更新的原文版本"
        await synth.run([item])
        self.assertNotIn("OLD PRIVATE DRAFT", json.dumps(create.await_args_list[1].kwargs))

    async def test_missing_rubric_is_not_waived_for_a_complete_length_json(self):
        item = fixture.mixed_candidate()
        invalid = {**fixture.synthesis_payload([0]), "rubric_answers": []}
        client, create = client_for(response(invalid), response(invalid))
        await ParadigmSynthesizer(client=client, model="offline").run([item])
        self.assertEqual(create.await_count, 2)
        self.assertEqual(item.rubric_assessment["decision"], "incomplete")

    async def test_healthy_peer_survives_a_twice_unreadable_candidate(self):
        healthy, bad = fixture.mixed_candidate(), fixture.mixed_candidate()
        bad.key = "separate-route"
        # Serial execution makes the fake response ordering deterministic.
        client, create = client_for(response(fixture.synthesis_payload([0])), response(""), response(""))
        await ParadigmSynthesizer(concurrency=1, client=client, model="offline").run([healthy, bad])
        self.assertNotEqual(healthy.rubric_assessment["decision"], "incomplete")
        self.assertEqual(bad.rubric_assessment["decision"], "incomplete")
        self.assertEqual(create.await_count, 3)

    async def test_sub_extraction_uses_complete_length_json_without_retry(self):
        client, create = client_for(response(extraction_payload()))
        result = await ParadigmAnalyzer(client=client, model="offline").extract(archive_fixture.origin())
        self.assertEqual(create.await_count, 1)
        self.assertEqual(result[0].rubric_assessment["decision"], "deep_dive")

    async def test_report_index_retries_broken_array_without_shrinking_mechanisms(self):
        seed = {"canonical_name": "机制一", "problem_shift": "问题变化", "mechanism": "状态转移",
                "innovation_types": ["architecture"]}
        first = '{"mechanisms":[' + json.dumps(seed, ensure_ascii=False) + ',{"canonical_name":"半句'
        second = {"mechanisms": [seed, {**seed, "canonical_name": "机制二"}]}
        client, create = client_for(response(first), response(second))
        analyzer = ParadigmAnalyzer(client=client, model="offline")
        values, error, _ = await analyzer._request_report_index(archive_fixture.origin(), "完整原始报告")
        self.assertEqual(error, "")
        self.assertEqual([row["canonical_name"] for row in values], ["机制一", "机制二"])
        self.assertEqual(create.await_args_list[1].kwargs["messages"][1]["content"], first)
        self.assertEqual([row.kwargs["max_tokens"] for row in create.await_args_list], [6000, 6000])

    async def test_batch_length_json_keeps_good_identity_and_marks_missing_peer_pending(self):
        one, two = archive_fixture.origin("1"), archive_fixture.origin("2")
        value = {"decisions": [{"fingerprint": one.fingerprint, "decision": "full_review", "reason": "有技术机制"},
                               {"fingerprint": "foreign", "decision": "screen_out", "reason": "外来对象"}]}
        client, create = client_for(response(value))
        result = await ParadigmAnalyzer(client=client, model="offline")._screen_uncached_origin_batch([one, two])
        self.assertEqual(set(result), {one.fingerprint})
        self.assertNotIn(two.fingerprint, result)
        self.assertEqual(create.await_count, 1)

    async def test_smoke_accepts_complete_json_at_output_cap_not_incomplete_prefix(self):
        from smokecheck import _check_models
        from tests.test_llm_policy import profile, MODEL
        client, create = client_for(response({"ok": True}))
        with patch("smokecheck.resolve_all", return_value=(profile(), profile(role="main"))), \
             patch("smokecheck.build_client", return_value=(client, MODEL)):
            result = await _check_models()
        self.assertEqual([row.status for row in result], ["passed", "skipped"])
        self.assertEqual(create.await_count, 1)

    async def test_trajectory_repairs_unreadable_length_answer_not_just_valid_json(self):
        profile = ResearcherProfile(name="Example Researcher", representative_works=[{"title": "Public work"}])
        item = fixture.mixed_candidate()
        item.researchers = [profile]
        fragment = '{"background_summary":"可核验背景'
        client, create = client_for(response(fragment), response(person_payload(), "stop"))
        await ResearcherTrajectoryAnalyzer(client=client, model="offline").run([item])
        self.assertEqual(create.await_count, 2)
        self.assertEqual(profile.attribution_analysis_status, "completed")
        self.assertEqual(create.await_args_list[1].kwargs["messages"][1]["content"], fragment)
        self.assertEqual(len(run_audit.llm_calls), 2)

    async def test_readable_length_prose_still_passes_real_route_gate(self):
        item = fixture.mixed_candidate()
        text = prose(item)
        client, create = client_for(response(text))
        with tempfile.TemporaryDirectory() as directory:
            generator = ParadigmReportGenerator(Path(directory), client=client, model="offline")
            value = await generator._draft_one_route("2026-10-21", item, index=1, total=1)
        self.assertEqual(value, text)
        self.assertFalse(_route_draft_violations(value, item))
        self.assertEqual(create.await_count, 1)

    async def test_half_sentence_retries_with_original_visible_prose(self):
        fragment = "### 技术路线\n\n正文写到半句"
        client, create = client_for(response(fragment), response("完整中文结论。", "stop"))
        with tempfile.TemporaryDirectory() as directory:
            generator = ParadigmReportGenerator(Path(directory), client=client, model="offline")
            value = await generator._request_markdown("原文约束", stage="weekly_route_draft", subject="offline",
                                                       temperature=0, max_tokens=2200)
        self.assertEqual(value, "完整中文结论。")
        self.assertEqual(create.await_args_list[1].kwargs["messages"][1]["content"], fragment)
        self.assertEqual([row.kwargs["max_tokens"] for row in create.await_args_list], [2200, 2200])

    async def test_readable_but_ungrounded_prose_cannot_bypass_route_gate(self):
        item = fixture.mixed_candidate()
        bad = prose(item) + "\n[猜测链接](https://unverified.example.invalid/)。"
        client, create = client_for(response(bad), response(bad))
        with tempfile.TemporaryDirectory() as directory:
            generator = ParadigmReportGenerator(Path(directory), client=client, model="offline")
            with self.assertRaises(ValueError):
                await generator._draft_one_route("2026-10-21", item, index=1, total=1)
        self.assertEqual(create.await_count, 2)


class TruncationArchiveTests(archive_fixture.ArchiveFixture, unittest.IsolatedAsyncioTestCase):
    async def test_raw_fragment_and_repair_remain_linked_without_private_cot(self):
        fragment = '{"hypotheses":[{"canonical_name":"已有分析'
        client, create = client_for(response(fragment), response(extraction_payload(), "stop"))
        await ParadigmAnalyzer(client=client, model="offline").extract(archive_fixture.origin())
        calls = sorted([archive._read(path) for path in (self.directory / "calls").glob("*.json")],
                       key=lambda row: row["started_at"])
        self.assertEqual(len(calls), 2)
        self.assertEqual([row["validation"]["status"] for row in calls], ["needs_repair", "passed"])
        self.assertEqual(calls[0]["response"]["content"], fragment)
        self.assertEqual(calls[0]["origin_refs"], calls[1]["origin_refs"])
        self.assertEqual(calls[1]["request"]["messages"][1]["content"], fragment)
        self.assertNotIn("PRIVATE COT", json.dumps(calls))

