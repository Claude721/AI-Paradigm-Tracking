"""Public SDK request contract and unsorted pagination, without network."""

from __future__ import annotations

import unittest
import asyncio
import tempfile
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, AsyncMock

import httpx

from runtime_clock import research_window
from sources.openreview_source import OpenReviewSource
from database.paradigm_store import ParadigmStore


VENUE = "ICLR.cc/2026/Conference"
NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def paper(note_id, *, current=True, venue=VENUE, forum=None):
    value = {
        "id": note_id,
        "cdate": int(datetime(2026, 10, 7 if current else 1, tzinfo=timezone.utc).timestamp() * 1000),
        "content": {
            "title": {"value": "Learning a World Model"},
            "abstract": {"value": "A predictive world model for control."},
            "venueid": {"value": venue},
        },
    }
    if not current:
        value["cdate"] = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    if forum:
        value["forum"] = forum
    return value


class OpenReviewContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_optional_venue_is_not_a_failed_planned_query(self):
        source = OpenReviewSource(venues=[])
        self.assertEqual(await source.safe_fetch(), [])
        self.assertEqual(source.coverage()["status"], "not_configured")
        self.assertEqual(source.coverage()["queries"], 0)

    async def fetch(self, handler, *, limit=1):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = OpenReviewSource(venues=[VENUE], searches=["world model"], limit=limit)
        with (
            research_window(NOW),
            patch("sources.openreview_source.httpx.AsyncClient", return_value=client),
        ):
            result = await source.safe_fetch()
        return source, result

    async def test_sdk_keys_and_old_first_page_do_not_drop_later_recent_paper(self):
        offsets = []

        def handler(request):
            params = dict(request.url.params)
            self.assertEqual(set(params), {"term", "content", "group", "source", "count", "limit", "offset"})
            self.assertEqual(params["source"], "forum")
            self.assertEqual(params["count"], "true")
            self.assertEqual(params["term"], "world model")
            self.assertEqual(params["group"], VENUE)
            offset = int(params["offset"])
            offsets.append(offset)
            pages = [[paper("old", current=False)], [paper("recent")], []]
            return httpx.Response(200, json={"notes": pages[offset]}, request=request)

        source, result = await self.fetch(handler)
        self.assertEqual(offsets, [0, 1, 2])
        self.assertEqual([item.identifiers["openreview"] for item in result], ["recent"])
        self.assertEqual(source.coverage()["status"], "completed")
        self.assertEqual(result[0].metrics, {})  # absent reply counts are not measured zeroes

    async def test_foreign_venue_and_replies_do_not_become_primary_origins(self):
        def handler(request):
            return httpx.Response(200, json={"notes": [
                paper("foreign", venue="Other.cc/2026/Conference"),
                paper("reply", forum="original-paper"),
                paper("ours"),
            ]}, request=request)

        source, result = await self.fetch(handler, limit=50)
        self.assertEqual([item.identifiers["openreview"] for item in result], ["ours"])
        self.assertEqual(source.relevance_filtered_count, 2)

    async def test_repeated_page_is_failure_not_successful_empty_coverage(self):
        def handler(request):
            return httpx.Response(200, json={"notes": [paper("same-old", current=False)]}, request=request)

        source, result = await self.fetch(handler)
        self.assertEqual(result, [])
        self.assertEqual(source.request_count, 2)
        self.assertEqual(source.failed_queries, 1)
        self.assertEqual(source.coverage()["status"], "query_failed")

    async def test_malformed_notes_payload_is_not_successful_zero_hits(self):
        for payload in ({"notes": None}, {}, {"error": "service unavailable"}, []):
            with self.subTest(payload=payload):
                def handler(request):
                    return httpx.Response(200, json=payload, request=request)

                source, result = await self.fetch(handler)
                self.assertEqual(result, [])
                self.assertEqual(source.completed_queries, 0)
                self.assertEqual(source.failed_queries, 1)

    async def test_rate_limit_preserves_healthy_page_and_resumes_same_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / 'audit.db')
            first_offsets, second_offsets = [], []
            def first(request):
                offset = int(request.url.params['offset'])
                first_offsets.append(offset)
                return httpx.Response(200, json={'notes':[paper('healthy')]}, request=request) if offset == 0 else httpx.Response(429, headers={'Retry-After':'120'}, request=request)
            client = httpx.AsyncClient(transport=httpx.MockTransport(first))
            source = OpenReviewSource(venues=[VENUE], searches=['world model'], limit=1, stage_cache=store.campaigns)
            with research_window(NOW), patch('sources.openreview_source.httpx.AsyncClient', return_value=client), patch('sources.openreview_source.asyncio.sleep', new_callable=AsyncMock):
                values = await source.safe_fetch()
            self.assertEqual([value.identifiers['openreview'] for value in values], ['healthy'])
            self.assertEqual(first_offsets, [0,1])  # respect long retry-after; no early third call
            self.assertEqual(source.completed_queries, 0)
            self.assertEqual(source.failed_queries, 1)
            def second(request):
                second_offsets.append(int(request.url.params['offset']))
                return httpx.Response(200, json={'notes':[]}, request=request)
            for index in range(2):
                client = httpx.AsyncClient(transport=httpx.MockTransport(second))
                resumed = OpenReviewSource(venues=[VENUE], searches=['world model'], limit=1, stage_cache=store.campaigns)
                with research_window(NOW), patch('sources.openreview_source.httpx.AsyncClient', return_value=client), patch('sources.openreview_source.asyncio.sleep', new_callable=AsyncMock):
                    values = await resumed.safe_fetch()
                self.assertEqual([value.identifiers['openreview'] for value in values], ['healthy'])
                self.assertEqual(resumed.completed_queries, 1)
            self.assertEqual(second_offsets, [1])  # completed zero-hit tail also has a receipt

    async def test_cancelled_page_can_resume_without_refetching_committed_page(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / 'audit.db')
            waiting = asyncio.Event()
            async def first(request):
                if int(request.url.params['offset']) == 0:
                    return httpx.Response(200,json={'notes':[paper('healthy')]},request=request)
                waiting.set()
                await asyncio.Event().wait()
            client = httpx.AsyncClient(transport=httpx.MockTransport(first))
            source = OpenReviewSource(venues=[VENUE], searches=['world model'], limit=1, stage_cache=store.campaigns)
            with research_window(NOW), patch('sources.openreview_source.httpx.AsyncClient', return_value=client), patch('sources.openreview_source.asyncio.sleep', new_callable=AsyncMock):
                task = asyncio.create_task(source.fetch())
                await asyncio.wait_for(waiting.wait(),1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            offsets=[]
            def second(request):
                offsets.append(int(request.url.params['offset']))
                return httpx.Response(200,json={'notes':[]},request=request)
            client = httpx.AsyncClient(transport=httpx.MockTransport(second))
            resumed = OpenReviewSource(venues=[VENUE], searches=['world model'], limit=1, stage_cache=store.campaigns)
            with research_window(NOW), patch('sources.openreview_source.httpx.AsyncClient', return_value=client), patch('sources.openreview_source.asyncio.sleep', new_callable=AsyncMock):
                values = await resumed.safe_fetch()
            self.assertEqual(offsets,[1])
            self.assertEqual(len(values),1)

    async def test_changed_window_cannot_reuse_previous_query_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ParadigmStore(Path(directory)/'audit.db')
            calls=[]
            def handler(request):
                calls.append(int(request.url.params['offset']))
                return httpx.Response(200,json={'notes':[]},request=request)
            for now in (NOW,datetime(2026,10,15,tzinfo=timezone.utc)):
                client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
                source=OpenReviewSource(venues=[VENUE],searches=['world model'],stage_cache=store.campaigns)
                with research_window(now),patch('sources.openreview_source.httpx.AsyncClient',return_value=client):
                    await source.fetch()
            self.assertEqual(calls,[0,0])

    async def test_server_count_closes_exact_full_final_page(self):
        def handler(request):
            self.assertEqual(int(request.url.params['offset']),0)
            return httpx.Response(200,json={'notes':[paper('ours')],'count':1},request=request)
        source, result=await self.fetch(handler,limit=1)
        self.assertEqual(len(result),1)
        self.assertEqual(source.request_count,1)

    async def test_inconsistent_count_is_not_completed_coverage(self):
        def handler(request):
            return httpx.Response(200,json={'notes':[paper('ours')],'count':0},request=request)
        source,result=await self.fetch(handler)
        self.assertEqual(source.completed_queries,0)
        self.assertEqual(source.failed_queries,1)

    async def test_short_page_with_more_declared_results_keeps_paging(self):
        offsets = []
        def handler(request):
            offset = int(request.url.params['offset'])
            offsets.append(offset)
            return httpx.Response(200, json={'notes': [paper(str(offset))], 'count': 2}, request=request)
        source, result = await self.fetch(handler, limit=50)
        self.assertEqual(offsets, [0, 1])
        self.assertEqual(len(result), 2)
        self.assertEqual(source.completed_queries, 1)

    async def test_empty_tail_before_declared_count_is_failure(self):
        source, result = await self.fetch(lambda request: httpx.Response(200, json={'notes': [], 'count': 2}, request=request))
        self.assertEqual(result, [])
        self.assertEqual(source.completed_queries, 0)
        self.assertEqual(source.failed_queries, 1)


if __name__ == "__main__":
    unittest.main()
