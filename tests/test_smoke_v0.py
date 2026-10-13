"""Hermetic tests for bounded real smoke, not production research acceptance."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import smokecheck


class V0ModelSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, content='{"ok":true}', *, finish="stop", usage=None, error=None):
        response = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content))],
            usage=usage,
        )
        create = AsyncMock(return_value=response, side_effect=error)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        profile = SimpleNamespace(provider="dashscope", model="offline", label="dashscope/offline",
                                  base_url="https://offline.invalid/v1", api_key="private-offline-key")
        with patch("smokecheck.resolve_all", return_value=(profile, profile)), \
                patch("smokecheck.build_client", return_value=(client, "offline")):
            results = await smokecheck._check_models()
        self.assertEqual(create.await_count, 1)
        self.assertEqual(results[1].status, "skipped")
        self.assertNotIn(profile.api_key, str(results))
        return results[0], create

    async def test_exact_json_contract_and_bounded_request(self):
        result, create = await self.probe(usage=SimpleNamespace(
            prompt_tokens=12, completion_tokens=5, total_tokens=17))
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.usage, {"reported": True, "prompt_tokens": 12,
                                      "completion_tokens": 5, "total_tokens": 17})
        self.assertEqual(create.await_args.kwargs["max_tokens"], 64)
        self.assertEqual(create.await_args.kwargs["response_format"], {"type": "json_object"})

    async def test_wrong_contracts_are_not_success(self):
        for text in ("OK", '{"ok":false}', '{"ok":1}', '{"ok":true,"extra":1}',
                     '{"ok":', '[{"ok":true}]', '```json\n{"ok":true}\n```'):
            with self.subTest(text=text):
                result, _ = await self.probe(text)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.failure_kind, "response_contract")
                self.assertTrue(smokecheck.smoke_failed([result]))

    async def test_missing_or_inconsistent_usage_is_unknown_not_zero(self):
        for usage in (None, SimpleNamespace(total_tokens=17),
                      SimpleNamespace(prompt_tokens=12, completion_tokens=5, total_tokens=18),
                      SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                      SimpleNamespace(prompt_tokens=True, completion_tokens=5, total_tokens=6)):
            with self.subTest(usage=usage):
                result, _ = await self.probe(usage=usage)
                self.assertEqual(result.status, "passed")
                self.assertEqual(result.usage, {"reported": False})
                self.assertIn("unknown", result.detail)

    async def test_truncated_json_keeps_usage_but_fails(self):
        result, _ = await self.probe(finish="length", usage=SimpleNamespace(
            prompt_tokens=12, completion_tokens=64, total_tokens=76))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_kind, "response_contract")
        self.assertEqual(result.usage["total_tokens"], 76)

    async def test_sdk_error_never_logs_response_body_or_credentials(self):
        error = RuntimeError("response-body private-offline-key")
        result, _ = await self.probe(error=error)
        self.assertEqual(result.status, "failed")
        self.assertNotIn("response-body", str(asdict(result)))
        self.assertEqual(result.usage, {"reported": False})

    async def test_sdk_authentication_status_is_actionable(self):
        error = RuntimeError("private response body")
        error.status_code = 401
        result, _ = await self.probe(error=error)
        self.assertEqual(result.failure_kind, "authentication_or_permission")
        self.assertIn("401", result.detail)
        self.assertNotIn("private response", result.detail)


class V0InputSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_retains_outage_but_healthy_capabilities_continue(self):
        settings = {
            "PARADIGM_MIN_PRIMARY_SOURCES": 2, "OPENALEX_API_KEY": "",
            "OPENREVIEW_VENUES": [], "PRIORITY_RESEARCH_PAGES": ["https://offline.invalid"],
            "RESEARCH_FEED_URLS": [], "LESSWRONG_SOURCE_ENABLED": False,
            "ALIGNMENT_FORUM_SOURCE_ENABLED": False, "KOL_SOURCE_ENABLED": False,
            "FOLLOW_BUILDERS_ENABLED": False, "GITHUB_TOKEN": "",
            "SEMANTIC_SCHOLAR_ENABLED": False, "SEMANTIC_SCHOLAR_API_KEY": "",
            "REDDIT_API_ACCESS_APPROVED": False, "KOL_X_SOURCE_ENABLED": False,
        }
        with ExitStack() as stack:
            stack.enter_context(patch.multiple(config, **settings))
            for name in ("_arxiv", "_huggingface", "_hackernews"):
                stack.enter_context(patch("smokecheck." + name, new=AsyncMock(return_value=(1, "offline"))))
            stack.enter_context(patch("smokecheck._priority_pages", new=AsyncMock(side_effect=ValueError("HTML unavailable"))))
            tavily = stack.enter_context(patch("smokecheck._tavily", new=AsyncMock()))
            smtp = stack.enter_context(patch("smokecheck._smtp", new=AsyncMock()))
            results = await smokecheck.run_smoke_checks(
                include_llm=False, include_smtp=False, include_tavily=False, write_json=False)
        self.assertFalse(smokecheck.smoke_failed(results))
        official = next(item for item in results if item.name == "官方研究页解析")
        self.assertEqual(official.status, "degraded")
        self.assertEqual(official.failure_kind, "response_contract")
        self.assertFalse(official.blocking)
        self.assertEqual(results[-1].count, 2)
        tavily.assert_not_awaited()
        smtp.assert_not_awaited()

    async def test_auxiliary_search_does_not_replace_origin_capability(self):
        with patch.object(config, "PARADIGM_MIN_PRIMARY_SOURCES", 2):
            summary = smokecheck._primary_capability_summary([
                smokecheck.SmokeResult("arXiv", "passed"),
                smokecheck.SmokeResult("GitHub Search", "passed"),
                smokecheck.SmokeResult("OpenAlex Authors", "passed"),
                smokecheck.SmokeResult("Hacker News Algolia", "passed"),
            ])
        self.assertEqual(summary.count, 1)
        self.assertEqual(summary.failure_kind, "insufficient_input_capabilities")
        self.assertTrue(smokecheck.smoke_failed([summary]))

    async def test_same_capability_not_double_counted_and_openreview_counts(self):
        with patch.object(config, "PARADIGM_MIN_PRIMARY_SOURCES", 2):
            results = [smokecheck.SmokeResult("arXiv", "passed")] * 2
            self.assertEqual(smokecheck._primary_capability_summary(results).status, "failed")
            results.append(smokecheck.SmokeResult("OpenReview", "passed", 0))
            self.assertEqual(smokecheck._primary_capability_summary(results).status, "passed")


class V0SmokeBoundaryTests(unittest.TestCase):
    def test_artifact_does_not_claim_production_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "smoke.json"
            with patch("smokecheck.Path", return_value=target):
                smokecheck._write_results([smokecheck.SmokeResult("offline", "passed")])
            payload = json.loads(target.read_text())
        self.assertTrue(payload["summary"]["smoke_passed"])
        self.assertIs(payload["production_v0_accepted"], False)

    def test_cloud_smoke_explicitly_skips_paid_platforms_and_smtp(self):
        workflow = Path(".github/workflows/weekly-radar.yml").read_text()
        step = workflow.split("      - name: 小成本真实接口冒烟\n", 1)[1].split("      - name:", 1)[0]
        self.assertIn("inputs.smoke_only && !inputs.source_check_only", step)
        self.assertIn("--smoke-skip-smtp --smoke-skip-tavily", step)
        for flag in ("EMAIL_PUSH_ENABLED", "EMAIL_PUSH_REQUIRED", "TAVILY_SOCIAL_SEARCH_ENABLED",
                     "KOL_X_SOURCE_ENABLED", "SEMANTIC_SCHOLAR_ENABLED", "REDDIT_API_ACCESS_APPROVED"):
            self.assertIn(flag + ': "false"', step)
        for key in ("SMTP_PASSWORD", "TAVILY_API_KEY", "TWITTER_BEARER_TOKEN",
                    "SEMANTIC_SCHOLAR_API_KEY", "REDDIT_CLIENT_SECRET"):
            self.assertIn(key + ': ""', step)


if __name__ == "__main__":
    unittest.main()
