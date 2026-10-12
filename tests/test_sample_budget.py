"""Hermetic cost admission; no keys, network, mail or production state."""
import asyncio
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.sample_budget import SampleBudgetClient, SampleBudgetError, MODEL, BASE_URL, PRICE_DATE


def response(prompt=1000, output=100, *, total=None, model=MODEL, reasoning=0):
    return SimpleNamespace(model=model, usage=SimpleNamespace(prompt_tokens=prompt,
        completion_tokens=output, total_tokens=prompt + output if total is None else total,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning)))


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "cost.json"
        self.create = AsyncMock(return_value=response())
        self.raw = SimpleNamespace(max_retries=0, base_url=BASE_URL,
                                  chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))

    def client(self, **kwargs):
        return SampleBudgetClient(self.raw, path=self.path, budget_cny=kwargs.pop("budget_cny", 20),
                                  verified_on=kwargs.pop("verified_on", PRICE_DATE), now=kwargs.pop("now", PRICE_DATE), **kwargs)

    async def request(self, client, **extra):
        return await client.create(model=MODEL, messages=[{"role": "user", "content": "test"}],
                                   max_tokens=5000, **extra)

    async def test_reservation_saved_before_io_and_complete_output_cap(self):
        async def inspect(**kwargs):
            saved = json.loads(self.path.read_text())
            self.assertEqual(saved["requests"][0]["status"], "reserved")
            self.assertEqual(Decimal(saved["accounted_upper_cny"]), Decimal("6.12024"))
            self.assertEqual(kwargs["max_completion_tokens"], 5000)
            self.assertNotIn("max_tokens", kwargs)
            return response()
        self.create.side_effect = inspect
        client = self.client()
        await self.request(client)
        self.assertEqual(client.spent, Decimal("0.0084"))
        self.assertNotIn("test", self.path.read_text())

    async def test_retries_each_pay_admission_and_stop_before_budget(self):
        self.create.return_value = response(prompt=1_000_000, output=5000)
        client = self.client()
        for _ in range(3):
            await self.request(client)
        with self.assertRaises(SampleBudgetError):
            await self.request(client)
        self.assertEqual(self.create.await_count, 3)
        self.assertEqual(client.spent, Decimal("18.36"))

    async def test_small_budget_refuses_even_first_request(self):
        with self.assertRaises(SampleBudgetError):
            await self.request(self.client(budget_cny=1))
        self.create.assert_not_awaited()

    async def test_timeout_reserves_and_never_implicitly_retries(self):
        self.create.side_effect = TimeoutError("sensitive text")
        client = self.client()
        for _ in range(2):
            with self.assertRaises(SampleBudgetError):
                await self.request(client)
        self.assertEqual(self.create.await_count, 1)
        self.assertTrue(client.unknown)
        self.assertEqual(client.spent, Decimal("6.12024"))
        self.assertNotIn("sensitive text", self.path.read_text())

    async def test_bad_or_missing_usage_stops_and_keeps_reservation(self):
        for value in (SimpleNamespace(model=MODEL, usage=None), response(total=5),
                      response(prompt=True), response(output=5011), response(model="other"),
                      response(reasoning=101), response(prompt=1_000_001)):
            with self.subTest(value=value):
                path = Path(self.temporary.name) / f"cost-{len(list(Path(self.temporary.name).iterdir()))}.json"
                self.create.return_value = value
                client = SampleBudgetClient(self.raw, path=path, budget_cny=20,
                                            verified_on=PRICE_DATE, now=PRICE_DATE)
                with self.assertRaises(SampleBudgetError):
                    await self.request(client)
                self.assertTrue(client.unknown)
                self.assertEqual(client.spent, Decimal("6.12024"))

    async def test_cancel_then_queued_peer_cannot_spend(self):
        entered = asyncio.Event()
        async def waiting(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.create.side_effect = waiting
        client = self.client()
        first = asyncio.create_task(self.request(client))
        await entered.wait()
        second = asyncio.create_task(self.request(client))
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        with self.assertRaises(SampleBudgetError):
            await second
        self.assertEqual(self.create.await_count, 1)
        self.assertTrue(client.unknown)

    async def test_reservation_save_failure_does_not_call_provider(self):
        client = self.client()
        with patch.object(client, "_save", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                await self.request(client)
        self.create.assert_not_awaited()
        self.assertTrue(client.unknown)

    async def test_final_save_failure_cannot_refund_or_continue(self):
        client = self.client()
        original = client._save
        calls = 0
        def save():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk")
            original()
        with patch.object(client, "_save", side_effect=save):
            with self.assertRaises(SampleBudgetError):
                await self.request(client)
        self.assertEqual(client.spent, Decimal("6.12024"))
        self.assertTrue(client.unknown)

    async def test_addons_or_multimodal_cannot_bypass_cost_contract(self):
        client = self.client()
        for extra in ({"tools": []}, {"stream": True}, {"extra_body": {"enable_search": True}}):
            with self.assertRaises(SampleBudgetError):
                await self.request(client, **extra)
        with self.assertRaises(SampleBudgetError):
            await client.create(model=MODEL, messages=[{"role":"user","content":[]}], max_tokens=5000)
        self.create.assert_not_awaited()

    async def test_request_count_is_bounded_independently_of_cost(self):
        client = self.client()
        for _ in range(24):
            await self.request(client)
        with self.assertRaises(SampleBudgetError):
            await self.request(client)
        self.assertEqual(self.create.await_count, 24)

    def test_invalid_budget_provider_price_or_sdk_retries_refuse(self):
        for budget in (0, -1, 21, "NaN", "Infinity", "bad", True):
            with self.assertRaises(SampleBudgetError):
                self.client(budget_cny=budget)
        for kwargs in ({"model":"deepseek-chat"}, {"base_url":"https://proxy.example/v1"},
                       {"verified_on":date(2026,10,11)}, {"now":date(2026,10,13)}):
            with self.assertRaises(SampleBudgetError):
                self.client(**kwargs)
        self.raw.max_retries = 2
        with self.assertRaises(SampleBudgetError):
            self.client()

    def test_existing_journal_never_reset_by_second_process(self):
        self.client()
        before = self.path.read_text()
        with self.assertRaises(FileExistsError):
            self.client()
        self.assertEqual(self.path.read_text(), before)


class SampleIsolationTests(unittest.IsolatedAsyncioTestCase):
    def test_configuration_cannot_enable_platforms_mail_or_resumes(self):
        from scripts.one_route_check import isolate_environment
        import os
        with patch.dict(os.environ, {"SMTP_PASSWORD":"private", "TAVILY_API_KEY":"private",
                                    "OPENALEX_API_KEY":"private", "MAIN_AGENT_API_KEY":"private",
                                    "LLM_API_KEY":"authorized-model-key", "LLM_ENABLE_THINKING":"true",
                                    "EMAIL_PUSH_ENABLED":"true"}, clear=True):
            isolate_environment()
            self.assertEqual(os.environ["LLM_API_KEY"], "authorized-model-key")
            for key in ("SMTP_PASSWORD", "TAVILY_API_KEY", "OPENALEX_API_KEY", "MAIN_AGENT_API_KEY"):
                self.assertNotIn(key, os.environ)
            self.assertEqual(os.environ["EMAIL_PUSH_ENABLED"], "false")
            self.assertEqual(os.environ["PARADIGM_AUTO_RESUME_ENABLED"], "false")
            self.assertEqual(os.environ["LLM_ENABLE_THINKING"], "false")

    async def test_real_sqlite_origin_identity_uses_actual_stage_contract(self):
        from scripts.one_route_check import run_sample
        from sources.base import RawProject
        row = RawProject(source="arxiv", name="Mechanism sample", url="https://arxiv.org/abs/2501.00663",
                         description="Source facts", extra={"arxiv_id":"2501.00663", "all_authors":["Alex Chen"]})
        async def hydrate(origin):
            origin.raw["document_excerpt"] = "Verified source body " * 50
            return {"primary_document":"read"}
        async def analyze(origins):
            return [SimpleNamespace(evidence=origins[0], rubric_assessment={"decision":"reject", "decision_reason":"No route-level intervention"})]
        with tempfile.TemporaryDirectory() as temporary, \
             patch("sources.arxiv_source.ArxivSource._fetch_seed_ids", new=AsyncMock(return_value=[row])), \
             patch("sources.arxiv_document_source.ArxivDocumentClient.hydrate", new=AsyncMock(side_effect=hydrate)), \
             patch("paradigms.analyzer.ParadigmAnalyzer.run", new=AsyncMock(side_effect=analyze)), \
             patch("paradigms.clustering.cluster_extractions", return_value=[]):
            result = {"status":"failed"}
            await run_sample("2501.00663", None, Path(temporary), result,
                             reference_time=datetime(2026,10,12,tzinfo=timezone.utc))
            self.assertEqual(result["status"], "screening_closed")
            self.assertEqual(result["primary"]["url"], "https://arxiv.org/abs/2501.00663")
            self.assertNotIn("route_test_draft", result)

    def test_workflow_never_injects_mail_extra_paid_keys_or_production_state(self):
        text = (Path(__file__).resolve().parents[1] / ".github/workflows/one-route-acceptance.yml").read_text()
        for forbidden in ("main.py", "SMTP_PASSWORD", "OPENALEX_API_KEY", "TAVILY_API_KEY", "actions: write", "schedule:", "restore_state", "sample.db"):
            self.assertNotIn(forbidden, text)
        self.assertIn("if: inputs.live_authorized", text)
        self.assertIn("--live-authorized", text)
        self.assertIn('EMAIL_PUSH_ENABLED: "false"', text)
