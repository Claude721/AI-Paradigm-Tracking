"""Official Qwen serialized request contracts, entirely offline."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from openai import AsyncOpenAI

import config
from agents.llm_utils import ModelPolicyClient, ResolvedModel, build_client, request_policy, completed_content
from agents.sample_budget import SampleBudgetClient, BASE_URL, MODEL, PRICE_DATE
from database.paradigm_store import (_deep_checkpoint_input_signature,
                                     _trajectory_checkpoint_policy_signature)
from paradigms.analyzer import _stage_cache_signature
from paradigms.models import ParadigmCandidate


def profile(**overrides):
    values = dict(role="sub", provider="dashscope", provider_from="test", model=MODEL,
                  model_from="test", api_key="offline-test-only", api_key_from="test",
                  base_url=BASE_URL, base_url_from="test")
    return ResolvedModel(**(values | overrides))


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    async def serialized_request(self, *, thinking=False, budget=False, **params):
        captured = []
        def handle(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "offline", "object": "chat.completion", "created": 0, "model": MODEL,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "{}"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
                          "completion_tokens_details": {"reasoning_tokens": 0}}})
        sdk = AsyncOpenAI(api_key="offline-test-only", base_url=BASE_URL, max_retries=0,
                          http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        with patch.object(config, "LLM_ENABLE_THINKING", thinking):
            client = ModelPolicyClient(sdk, profile())
        try:
            with tempfile.TemporaryDirectory() as directory:
                if budget:
                    client = SampleBudgetClient(client, path=Path(directory) / "cost.json",
                                                budget_cny=19, verified_on=PRICE_DATE, now=PRICE_DATE)
                await client.chat.completions.create(model=MODEL,
                    messages=[{"role": "user", "content": "offline"}], **params)
        finally:
            await sdk.close()
        return captured[0]

    async def test_serialized_nonthinking_and_total_cap(self):
        body = await self.serialized_request(max_tokens=5000)
        self.assertIs(body["enable_thinking"], False)
        self.assertEqual(body["max_completion_tokens"], 5000)
        self.assertNotIn("max_tokens", body)

    async def test_explicit_thinking_still_has_combined_cap(self):
        body = await self.serialized_request(thinking=True, max_tokens=5000)
        self.assertIs(body["enable_thinking"], True)
        self.assertEqual(body["max_completion_tokens"], 5000)

    async def test_sample_admission_validates_transport_and_retains_policy(self):
        body = await self.serialized_request(budget=True, max_tokens=5000)
        self.assertIs(body["enable_thinking"], False)
        self.assertEqual(body["max_completion_tokens"], 5000)

    async def test_smoke_size_cap_uses_same_serialized_contract(self):
        body = await self.serialized_request(max_tokens=10)
        self.assertIs(body["enable_thinking"], False)
        self.assertEqual(body["max_completion_tokens"], 10)

    async def test_existing_combined_cap_and_extra_body_preserved(self):
        body = await self.serialized_request(max_completion_tokens=400,
                                             extra_body={"enable_thinking": False, "test_option": 1})
        self.assertEqual(body["max_completion_tokens"], 400)
        self.assertEqual(body["test_option"], 1)

    async def test_conflicting_mode_or_output_cap_refused_before_io(self):
        create = AsyncMock()
        raw = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(config, "LLM_ENABLE_THINKING", False):
            client = ModelPolicyClient(raw, profile())
        for args in ({"extra_body": {"enable_thinking": True}},
                     {"max_tokens": 50, "max_completion_tokens": 51}):
            with self.assertRaises(ValueError):
                await client.create(model=MODEL, **args)
        create.assert_not_awaited()

    async def test_unverified_provider_endpoint_model_and_override_not_modified(self):
        params = {"model": MODEL, "max_tokens": 50}
        for resolved in (profile(provider="openai"), profile(base_url="https://proxy.invalid/v1"),
                         profile(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1/evil"),
                         profile(model="qwen-unknown")):
            create = AsyncMock()
            client = ModelPolicyClient(SimpleNamespace(chat=SimpleNamespace(
                completions=SimpleNamespace(create=create))), resolved)
            await client.create(**params)
            create.assert_awaited_once_with(**params)
            self.assertEqual(request_policy(resolved), {})
        create = AsyncMock()
        client = ModelPolicyClient(SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create))), profile())
        await client.create(model="other-model", max_tokens=50)
        create.assert_awaited_once_with(model="other-model", max_tokens=50)

    async def test_builder_keeps_retry_zero_and_sdk_lifecycle(self):
        with patch("agents.llm_utils.resolve_model", return_value=profile()):
            client, model = build_client()
        try:
            self.assertEqual(model, MODEL)
            self.assertEqual(client.max_retries, 0)
            self.assertEqual(str(client.base_url).rstrip("/"), BASE_URL)
        finally:
            await client.close()
        self.assertTrue(client.is_closed())

    def test_mode_invalidates_all_three_model_checkpoints(self):
        candidate = ParadigmCandidate(key="offline", name="离线样例", thesis="", problem_shift="", mechanism="")
        with patch("agents.llm_utils.resolve_model", return_value=profile()), \
             patch("paradigms.analyzer.resolve_model", return_value=profile()):
            signatures = []
            for thinking in (False, True, False):
                with patch.object(config, "LLM_ENABLE_THINKING", thinking):
                    signatures.append((_stage_cache_signature("sub", MODEL, "prompt", "identity"),
                        _deep_checkpoint_input_signature(candidate), _trajectory_checkpoint_policy_signature()))
        self.assertEqual(signatures[0], signatures[2])
        for before, after in zip(signatures[0], signatures[1]):
            self.assertNotEqual(before, after)

    def test_invalid_mode_configuration_is_not_silently_defaulted(self):
        from healthcheck import _raw_environment_syntax_check
        with patch.dict("os.environ", {"LLM_ENABLE_THINKING": "flase"}):
            check = _raw_environment_syntax_check()
        self.assertEqual(check.status, "missing")
        self.assertIn("LLM_ENABLE_THINKING", str(check))

    def test_legal_json_or_markdown_prefix_is_not_a_completed_response(self):
        for content in ('{"hypotheses": [{}]}', '## 看似完整的标题\n足够可读的前缀。'):
            for reason in ("length", "content_filter", "tool_calls", "function_call", None, "", "private-body"):
                response = SimpleNamespace(choices=[SimpleNamespace(finish_reason=reason,
                    message=SimpleNamespace(content=content))])
                with self.assertRaises(ValueError) as captured:
                    completed_content(response)
                self.assertNotIn(content, str(captured.exception))
                self.assertNotIn("private-body", str(captured.exception))
        good = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                    message=SimpleNamespace(content="完整回答"))])
        self.assertEqual(completed_content(good), "完整回答")

    async def test_truncated_extraction_keeps_pending_and_accounts_confirmed_usage(self):
        from paradigms.analyzer import ParadigmAnalyzer
        from tests.test_research_service import origin
        from run_audit import run_audit
        run_audit.reset()
        self.addCleanup(run_audit.reset)
        response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=5000,
                                      total_tokens=6000), choices=[SimpleNamespace(finish_reason="length",
                                      message=SimpleNamespace(content='{"hypotheses": [{}]}'))])
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        items = await ParadigmAnalyzer(client=client, model="offline").run([origin(1)])
        self.assertEqual(create.await_count, 2)
        self.assertNotIn(items[0].rubric_assessment.get("decision"), {"deep_dive", "observe", "reject"})
        self.assertEqual(len(run_audit.llm_calls), 2)
        self.assertTrue(all(item["usage_reported"] and item["finish_reason"] == "length"
                            and item["status"] == "failed" for item in run_audit.llm_calls))

    async def test_truncated_report_is_not_delivered_or_double_accounted(self):
        from reports.paradigm_generator import ParadigmReportGenerator
        from run_audit import run_audit
        run_audit.reset()
        self.addCleanup(run_audit.reset)
        response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=2200,
                                      total_tokens=2300), choices=[SimpleNamespace(finish_reason="length",
                                      message=SimpleNamespace(content="## 完整外观\n中文结论。"))])
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with tempfile.TemporaryDirectory() as directory:
            generator = ParadigmReportGenerator(output_dir=Path(directory), client=client, model="offline")
            with self.assertRaises(ValueError):
                await generator._request_markdown("offline", stage="report_route", subject="离线路线",
                                                   temperature=0, max_tokens=2200)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(create.await_count, 2)
        self.assertEqual(len(run_audit.llm_calls), 2)
        self.assertTrue(all(item["status"] == "failed" and item["usage_reported"] for item in run_audit.llm_calls))

    async def test_smoke_same_label_different_credentials_or_endpoint_not_skipped(self):
        from smokecheck import _check_models
        good = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                               message=SimpleNamespace(content="OK"))], usage=SimpleNamespace(total_tokens=4))
        create_good = AsyncMock(return_value=good)
        create_bad = AsyncMock(side_effect=RuntimeError("endpoint contract failed"))
        clients = [SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call)))
                   for call in (create_good, create_bad)]
        for other in (profile(role="main", api_key="different-private-key"),
                      profile(role="main", base_url="https://other.invalid/v1"),
                      profile(role="main", provider="other")):
            with patch("smokecheck.resolve_all", return_value=(profile(), other)), \
                 patch("smokecheck.build_client", side_effect=[(item, MODEL) for item in clients]) as build:
                results = await _check_models()
            self.assertEqual([result.status for result in results], ["passed", "failed"])
            self.assertEqual(build.call_count, 2)
            self.assertNotIn("different-private-key", str(results))

    async def test_smoke_equivalent_roles_deduplicate_only_identical_capability(self):
        from smokecheck import _check_models
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                                   message=SimpleNamespace(content="OK"))], usage=SimpleNamespace(total_tokens=4))
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch("smokecheck.resolve_all", return_value=(profile(), profile(role="main", base_url=BASE_URL + "/"))), \
             patch("smokecheck.build_client", return_value=(client, MODEL)):
            results = await _check_models()
        self.assertEqual([result.status for result in results], ["passed", "skipped"])
        self.assertEqual(create.await_count, 1)
