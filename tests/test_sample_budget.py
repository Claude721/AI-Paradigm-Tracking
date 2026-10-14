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

from agents.sample_budget import SampleBudgetClient, SampleBudgetError
from tests.model_contract_fixture import MODEL, BASE_URL, PRICE_DATE, price_contract


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
                                  verified_on=kwargs.pop("verified_on", PRICE_DATE), now=kwargs.pop("now", PRICE_DATE),
                                  model=kwargs.pop("model", MODEL), base_url=kwargs.pop("base_url", BASE_URL),
                                  price_contract=kwargs.pop("price_contract", price_contract()), **kwargs)

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

    async def test_main_sub_transports_share_one_total_budget_and_use_own_client(self):
        self.create.return_value = response(prompt=1_000_000, output=5000)
        other_create = AsyncMock(return_value=response(prompt=1_000_000, output=5000))
        other = SimpleNamespace(max_retries=0, base_url=BASE_URL,
                                chat=SimpleNamespace(completions=SimpleNamespace(create=other_create)))
        shared = self.client()
        main = shared.bind_client(other, model=MODEL, base_url=BASE_URL, role="main", price_contract=price_contract())
        await self.request(shared)
        await self.request(main.chat.completions)
        await self.request(shared)
        with self.assertRaises(SampleBudgetError):
            await self.request(main.chat.completions)
        self.assertEqual((self.create.await_count, other_create.await_count), (2, 1))
        self.assertEqual(shared.spent, Decimal("18.36"))
        self.assertEqual([row["role"] for row in shared.calls], ["sub", "main", "sub"])

    async def test_main_unknown_usage_blocks_sub_without_new_spend(self):
        other_create = AsyncMock(side_effect=TimeoutError("private"))
        other = SimpleNamespace(max_retries=0, base_url=BASE_URL,
                                chat=SimpleNamespace(completions=SimpleNamespace(create=other_create)))
        shared = self.client()
        main = shared.bind_client(other, model=MODEL, base_url=BASE_URL, role="main", price_contract=price_contract())
        with self.assertRaises(SampleBudgetError):
            await self.request(main.chat.completions)
        with self.assertRaises(SampleBudgetError):
            await self.request(shared)
        self.create.assert_not_awaited()
        self.assertEqual(other_create.await_count, 1)
        self.assertTrue(shared.unknown)

    async def test_unverified_main_model_is_rejected_before_either_role_spends(self):
        shared = self.client()
        with self.assertRaises(SampleBudgetError):
            shared.bind_client(self.raw, model="another-model", base_url=BASE_URL, role="main", price_contract=price_contract())
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
                                            verified_on=PRICE_DATE, now=PRICE_DATE, model=MODEL,
                                            base_url=BASE_URL, price_contract=price_contract())
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

    async def test_different_roles_models_prices_and_hosts_use_one_shared_ledger(self):
        other_model, endpoint = "any-compatible-model", "https://other.invalid/v1"
        other_create = AsyncMock(return_value=response(model=other_model))
        other = SimpleNamespace(max_retries=0, base_url=endpoint,
                                chat=SimpleNamespace(completions=SimpleNamespace(create=other_create)))
        shared = self.client()
        contract = price_contract(model=other_model, base_url=endpoint, response_models=[other_model],
                                  input_price_cny="12", output_price_cny="36", output_limit="max_tokens")
        main = shared.bind_client(other, model=other_model, base_url=endpoint, role="main", price_contract=contract)
        await self.request(shared)
        await main.chat.completions.create(model=other_model, messages=[{"role":"user", "content":"offline"}], max_tokens=128)
        self.assertEqual(shared.spent, Decimal("0.024"))
        self.assertEqual(other_create.call_args.kwargs["max_tokens"], 128)
        self.assertNotIn("max_completion_tokens", other_create.call_args.kwargs)
        self.assertEqual(shared.summary()["price_contracts"]["main"]["model"], other_model)

    async def test_price_date_is_supplied_configuration_not_code_calendar_pin(self):
        next_day = date(2027, 1, 1)
        client = self.client(verified_on=next_day, now=next_day,
                             price_contract=price_contract(verified_on=next_day.isoformat()))
        await self.request(client)
        self.assertEqual(self.create.await_count, 1)

    def test_bad_price_contract_rejected_without_provider_io(self):
        for change in ({"input_price_cny":"NaN"}, {"output_price_cny":"-1"}, {"input_limit":True},
                       {"completion_includes_reasoning":False}, {"response_models":[]},
                       {"pricing_url":"https://user:private@example.org/"}, {"extra_field":1}):
            with self.subTest(change=change), self.assertRaises(SampleBudgetError):
                self.client(price_contract=price_contract(**change))
        self.create.assert_not_awaited()


class SampleIsolationTests(unittest.IsolatedAsyncioTestCase):
    def test_verification_summary_preserves_disabled_scope_not_zero_discussion(self):
        from scripts.one_route_check import _verification_summary
        from paradigms.models import ParadigmCandidate, ResearcherProfile
        candidate = ParadigmCandidate(key="sample", name="离线样例", thesis="", problem_shift="", mechanism="")
        candidate.community_coverage = {"x_official": "未开启；未作平台全量检索"}
        candidate.community_search_receipts = {"x_official": {"status":"disabled", "reason":"not_enabled", "result_count":0}}
        candidate.researchers = [ResearcherProfile(name="Alex Chen", contact_search_notes=["未配置身份接口"])]
        summary = _verification_summary(candidate)
        self.assertEqual(summary["publisher_tier"], "unknown")
        self.assertEqual(summary["contact_lookup_completed_count"], 0)
        self.assertEqual(summary["search_receipts"]["x_official"]["status"], "disabled")
        self.assertIn("未开启", summary["community_coverage"]["x_official"])

    def test_configuration_cannot_enable_platforms_mail_or_resumes(self):
        from scripts.one_route_check import isolate_environment
        import os
        with patch.dict(os.environ, {"SMTP_PASSWORD":"private", "TAVILY_API_KEY":"private",
                                    "OPENALEX_API_KEY":"private", "MAIN_AGENT_API_KEY":"old-private",
                                    "SUB_AGENT_LLM_API_KEY":"authorized-sub-key", "MAIN_AGENT_LLM_API_KEY":"authorized-main-key",
                                    "LLM_API_KEY":"old-private", "LLM_ENABLE_THINKING":"true",
                                    "EMAIL_PUSH_ENABLED":"true"}, clear=True):
            isolate_environment()
            self.assertEqual(os.environ["SUB_AGENT_LLM_API_KEY"], "authorized-sub-key")
            self.assertEqual(os.environ["MAIN_AGENT_LLM_API_KEY"], "authorized-main-key")
            for key in ("SMTP_PASSWORD", "TAVILY_API_KEY", "OPENALEX_API_KEY", "MAIN_AGENT_API_KEY", "LLM_API_KEY"):
                self.assertNotIn(key, os.environ)
            self.assertEqual(os.environ["EMAIL_PUSH_ENABLED"], "false")
            self.assertEqual(os.environ["PARADIGM_AUTO_RESUME_ENABLED"], "false")
            self.assertEqual(os.environ["LLM_ENABLE_THINKING"], "false")

    def test_sample_isolation_does_not_invent_missing_models(self):
        from scripts.one_route_check import isolate_environment
        import os
        with patch.dict(os.environ, {}, clear=True):
            isolate_environment()
            for role in ("MAIN_AGENT", "SUB_AGENT"):
                for field in ("PROVIDER", "MODEL", "BASE_URL", "LLM_API_KEY"):
                    self.assertNotIn(role + "_" + field, os.environ)

    def test_explicit_identity_scope_keeps_only_identity_key_not_other_paid_services(self):
        from scripts.one_route_check import isolate_environment
        import os
        with patch.dict(os.environ, {"OPENALEX_API_KEY":"identity-key", "TAVILY_API_KEY":"private",
                                    "SMTP_PASSWORD":"private"}, clear=True):
            isolate_environment(identity_checks=True)
            self.assertEqual(os.environ["OPENALEX_API_KEY"], "identity-key")
            self.assertNotIn("TAVILY_API_KEY", os.environ)
            self.assertNotIn("SMTP_PASSWORD", os.environ)

    async def test_identity_api_limit_is_shared_before_parallel_awaits(self):
        from sources.researcher_profile_source import ResearcherProfileClient
        profile_client = ResearcherProfileClient(openalex_request_limit=2)
        get = AsyncMock(return_value=SimpleNamespace(status_code=200))
        raw = SimpleNamespace(get=get)
        values = await asyncio.gather(*(profile_client._openalex_get(raw, "https://api.openalex.org/authors",
                        params={}) for _ in range(5)), return_exceptions=True)
        self.assertEqual(get.await_count, 2)
        self.assertEqual(profile_client.openalex_request_count, 2)
        self.assertEqual(sum(isinstance(value, RuntimeError) for value in values), 3)

    async def test_real_sqlite_origin_identity_uses_actual_stage_contract(self):
        from scripts.one_route_check import run_sample
        from sources.base import RawProject
        row = RawProject(source="arxiv", name="Mechanism sample", url="https://arxiv.org/abs/2501.00663",
                         description="Source facts", created_at="2026-10-10T12:00:00Z",
                         extra={"arxiv_id":"2501.00663", "all_authors":["Alex Chen"]})
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
        for forbidden in ("main.py", "SMTP_PASSWORD", "TAVILY_API_KEY", "actions: write", "schedule:", "restore_state", "sample.db"):
            self.assertNotIn(forbidden, text)
        self.assertIn("if: inputs.live_authorized", text)
        self.assertIn("--live-authorized", text)
        self.assertIn('EMAIL_PUSH_ENABLED: "false"', text)
        self.assertIn("!inputs.models_only && secrets.OPENALEX_API_KEY", text)
