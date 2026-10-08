"""Public SDK request contract and unsorted pagination, without network."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

from runtime_clock import research_window
from sources.openreview_source import OpenReviewSource


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
            self.assertEqual(set(params), {"term", "content", "group", "source", "limit", "offset"})
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


if __name__ == "__main__":
    unittest.main()
