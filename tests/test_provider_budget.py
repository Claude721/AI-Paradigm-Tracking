"""Offline credit safety, migration and end-to-end enrichment contracts."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import config
from database.paradigm_store import ParadigmStore
from database.state_migration import migrate_state
from paradigms.enrichment import EvidenceEnricher
from paradigms.search_receipts import incomplete_searches
from sources.social_web_search_source import SocialWebSearchClient
from sources.semantic_scholar_source import SemanticScholarClient
from tests.test_input_closure import candidate


def usage(credits=0):
    return {"account": {"plan_usage": credits, "paygo_usage": 0}}


class CreditStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "radar.db"
        self.store = ParadigmStore(self.path)
        self.budget = self.store.provider_budgets
        self.budget.observe("tavily", "2026-10", 5, through=0)

    def test_reservation_survives_crash_restart_without_refund(self):
        identity = self.budget.reserve("tavily", "2026-10", limit=6)
        self.assertIsNotNone(identity)
        restored = ParadigmStore(self.path).provider_budgets
        self.assertEqual(restored.used("tavily", "2026-10"), 6)
        self.assertIsNone(restored.reserve("tavily", "2026-10", limit=6))
        restored.settle(identity)
        self.assertEqual(restored.used("tavily", "2026-10"), 6)

    def test_confirmed_usage_idempotence_and_no_silent_refund(self):
        identity = self.budget.reserve("tavily", "2026-10", limit=10)
        self.budget.settle(identity, credits=2)
        self.budget.settle(identity, credits=2)
        self.assertEqual(self.budget.used("tavily", "2026-10"), 7)
        for value in (None, 0, 1):
            with self.assertRaises(ValueError):
                self.budget.settle(identity, credits=value)

    def test_account_reconciliation_never_double_counts_completed_requests(self):
        identity = self.budget.reserve("tavily", "2026-10", limit=10)
        self.budget.settle(identity, credits=1)
        self.budget.observe("tavily", "2026-10", 6, through=identity)
        self.assertEqual(self.budget.used("tavily", "2026-10"), 6)
        self.budget.observe("tavily", "2026-10", 8, through=identity)
        self.assertEqual(self.budget.used("tavily", "2026-10"), 8)
        self.budget.observe("tavily", "2026-10", 4, through=identity)
        self.assertEqual(self.budget.used("tavily", "2026-10"), 8)

    def test_inflight_during_usage_read_is_still_counted(self):
        through = self.budget.watermark("tavily", "2026-10")
        self.budget.reserve("tavily", "2026-10", limit=10)
        self.budget.observe("tavily", "2026-10", 8, through=through)
        self.assertEqual(self.budget.used("tavily", "2026-10"), 9)

    def test_processes_share_atomic_cap(self):
        def reserve(_):
            return ParadigmStore(self.path).provider_budgets.reserve("tavily", "2026-10", limit=8)
        with ThreadPoolExecutor(max_workers=4) as executor:
            identities = list(executor.map(reserve, range(12)))
        self.assertEqual(sum(identity is not None for identity in identities), 3)
        self.assertEqual(self.budget.used("tavily", "2026-10"), 8)

    def test_new_month_keeps_old_history_and_needs_fresh_observation(self):
        self.budget.reserve("tavily", "2026-10", limit=10)
        with self.assertRaises(ValueError):
            self.budget.reserve("tavily", "2026-11", limit=10)
        self.budget.observe("tavily", "2026-11", 0, through=0)
        self.budget.reserve("tavily", "2026-11", limit=10)
        self.assertEqual((self.budget.used("tavily", "2026-10"), self.budget.used("tavily", "2026-11")), (6, 1))

    def test_v8_upgrade_preserves_routes_and_v9_requires_budget_tables(self):
        item = candidate()
        self.store.save_candidates([item])
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TABLE provider_credit_requests")
            conn.execute("DROP TABLE provider_credit_periods")
        with self.assertRaises(ValueError):
            migrate_state(self.path, 9)
        self.assertEqual(migrate_state(self.path, 8), config.PARADIGM_STATE_SCHEMA_VERSION)
        self.assertEqual(ParadigmStore(self.path).load_pending_deep_candidates()[0].key, item.key)

    def test_corrupt_budget_payload_cannot_restore_as_healthy(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE provider_credit_periods SET observed_credits=-1")
        with self.assertRaises(ValueError):
            migrate_state(self.path, 9)

    def test_missing_or_foreign_observation_watermark_is_rejected(self):
        with self.assertRaises(ValueError):
            self.budget.observe("tavily", "2026-10", 6, through=9999)
        identity = self.budget.reserve("tavily", "2026-10", limit=10)
        with self.assertRaises(ValueError):
            self.budget.observe("tavily", "2026-11", 0, through=identity)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE provider_credit_periods SET observed_through=9999")
        with self.assertRaises(ValueError):
            migrate_state(self.path, 9)


class CreditClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "radar.db"
        self.budget = ParadigmStore(self.path).provider_budgets
        self.settings = patch.multiple(config, TAVILY_SOCIAL_SEARCH_ENABLED=True,
            TAVILY_API_KEY="offline-only-key", TAVILY_MONTHLY_CREDIT_LIMIT=2,
            TAVILY_REQUEST_SAFETY_LIMIT=0)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.clock = patch("sources.social_web_search_source.billing_period", return_value="2026-10")
        self.clock.start()
        self.addCleanup(self.clock.stop)

    async def test_resumed_client_checks_account_once_and_preserves_cap(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            self.assertNotIn("offline-only-key", str(request.url))
            return httpx.Response(200, json=usage(calls.count("/search")) if request.url.path == "/usage" else {"results": [], "usage": {"credits": 1}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            first = SocialWebSearchClient(budget_store=self.budget)
            await first.search(http, candidate("a"))
            await first.search(http, candidate("b"))
            self.assertEqual(calls.count("/usage"), 1)
            second = SocialWebSearchClient(budget_store=ParadigmStore(self.path).provider_budgets)
            item = candidate("c")
            await second.search(http, item)
        self.assertEqual(calls.count("/search"), 2)
        self.assertEqual(item.community_search_receipts["tavily_social_web"]["reason"], "monthly_credit_limit")
        self.assertEqual(self.budget.used("tavily", "2026-10"), 2)

    async def test_unknown_account_usage_never_sends_search(self):
        for data in ({}, {"account": {"plan_usage": True, "paygo_usage": 0}}, {"account": {"plan_usage": 0}}):
            calls = []
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: (calls.append(request.url.path), httpx.Response(200, json=data))[1])) as http:
                search = SocialWebSearchClient(budget_store=self.budget)
                item = candidate()
                await search.search(http, item)
                await search.search(http, candidate("b"))
            self.assertEqual(calls, ["/usage"])
            self.assertIn("usage_check_", item.community_search_receipts["tavily_social_web"]["reason"])

    async def test_unknown_charge_stops_peer_and_survives_restore(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json=usage() if request.url.path == "/usage" else {"results": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            search = SocialWebSearchClient(budget_store=self.budget)
            with self.assertRaises(ValueError):
                await search.search(http, candidate())
            item = candidate("peer")
            await search.search(http, item)
        self.assertEqual(calls, ["/usage", "/search"])
        self.assertEqual(item.community_search_receipts["tavily_social_web"]["reason"], "unconfirmed_request_usage")
        self.assertEqual(ParadigmStore(self.path).provider_budgets.used("tavily", "2026-10"), 1)

    async def test_cancellation_reserves_credit_before_io(self):
        entered = asyncio.Event()
        async def handler(request):
            if request.url.path == "/usage":
                return httpx.Response(200, json=usage())
            self.assertEqual(self.budget.used("tavily", "2026-10"), 1)
            entered.set()
            await asyncio.Future()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            search = SocialWebSearchClient(budget_store=self.budget)
            task = asyncio.create_task(search.search(http, candidate()))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.budget.used("tavily", "2026-10"), 1)

    async def test_no_durable_store_fails_closed_without_network(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: self.fail("network must not execute"))) as http:
            item = candidate()
            await SocialWebSearchClient().search(http, item)
        self.assertEqual(item.community_search_receipts["tavily_social_web"]["reason"], "durable_budget_store_missing")

    async def test_usage_auth_error_retains_http_diagnosis_without_spending_search_credit(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(401)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            item = candidate()
            await SocialWebSearchClient(budget_store=self.budget).search(http, item)
        self.assertEqual(calls, ["/usage"])
        self.assertEqual(item.community_search_receipts["tavily_social_web"]["reason"], "usage_http_401")

    async def test_search_auth_error_retains_http_failure_and_unknown_reservation(self):
        def handler(request):
            return httpx.Response(200, json=usage()) if request.url.path == "/usage" else httpx.Response(401)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            item = candidate()
            self.assertEqual(await SocialWebSearchClient(budget_store=self.budget).search(http, item), [])
        self.assertEqual(item.community_search_receipts["tavily_social_web"]["reason"], "http_401")
        self.assertEqual(self.budget.used("tavily", "2026-10"), 1)


class ScholarlyClosureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = patch.multiple(config, SEMANTIC_SCHOLAR_ENABLED=True, SEMANTIC_SCHOLAR_API_KEY="approved-offline")
        self.settings.start()
        self.addCleanup(self.settings.stop)

    async def test_enabled_scholarly_failure_is_not_successful_zero(self):
        for error in (RuntimeError("private detail"), None):
            item = candidate()
            enricher = EvidenceEnricher()
            enricher.documents.hydrate = AsyncMock(return_value={})
            enricher.community.search = AsyncMock(return_value=[])
            enricher.semantic_scholar.enrich_paper = AsyncMock(side_effect=error, return_value=(None, []))
            enricher.researchers.enrich = AsyncMock(return_value=[])
            await enricher.run([item], [])
            self.assertEqual(incomplete_searches(item), ["semantic_scholar"] if error else [])
            enricher.researchers.enrich.assert_not_awaited()
            if not error:
                await enricher.enrich_people([item])
            self.assertEqual(enricher.researchers.enrich.await_count, 0 if error else 1)
            self.assertNotIn("private detail", json.dumps(item.to_dict()))

    async def test_enabled_missing_key_retains_task_without_request(self):
        item = candidate()
        enricher = EvidenceEnricher()
        enricher.documents.hydrate = AsyncMock(return_value={})
        enricher.community.search = AsyncMock(return_value=[])
        enricher.semantic_scholar.enrich_paper = AsyncMock()
        enricher.researchers.enrich = AsyncMock()
        with patch.object(config, "SEMANTIC_SCHOLAR_API_KEY", ""):
            await enricher.run([item], [])
        self.assertEqual(incomplete_searches(item), ["semantic_scholar"])
        enricher.semantic_scholar.enrich_paper.assert_not_awaited()
        enricher.researchers.enrich.assert_not_awaited()

    async def test_search_schema_error_cannot_become_zero_match(self):
        response = httpx.Response(200, json={"error": "ignored body"}, request=httpx.Request("GET", "https://api.semanticscholar.org/graph/v1/paper/search"))
        client = SemanticScholarClient()
        client._get = AsyncMock(return_value=response)
        with self.assertRaises(ValueError):
            await client.enrich_paper(candidate().evidence[0])
