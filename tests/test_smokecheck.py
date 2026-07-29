from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import config
from smokecheck import (
    SmokeResult,
    _check,
    _openalex_works,
    _openreview,
    smoke_failed,
)


def _client_context(response: httpx.Response) -> tuple[MagicMock, MagicMock]:
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    return context, client


class SmokeCheckContractTests(unittest.TestCase):
    def test_openalex_smoke_uses_exactly_one_request_without_cursor(self) -> None:
        response = httpx.Response(
            200,
            json={"results": [{"display_name": "A World Model"}]},
            request=httpx.Request("GET", "https://api.openalex.org/works"),
        )
        context, client = _client_context(response)
        with (
            patch.object(config, "OPENALEX_API_KEY", "configured"),
            patch("smokecheck.httpx.AsyncClient", return_value=context),
        ):
            count, detail = asyncio.run(_openalex_works())

        self.assertEqual(count, 1)
        self.assertIn("单次请求", detail)
        self.assertEqual(client.get.await_count, 1)
        params = client.get.await_args.kwargs["params"]
        self.assertEqual(params["per-page"], 2)
        self.assertNotIn("cursor", params)

    def test_openreview_smoke_uses_exactly_one_request_and_accepts_zero_hits(
        self,
    ) -> None:
        response = httpx.Response(
            200,
            json={"notes": []},
            request=httpx.Request(
                "GET", "https://api2.openreview.net/notes/search"
            ),
        )
        context, client = _client_context(response)
        with (
            patch.object(
                config,
                "OPENREVIEW_VENUES",
                ["ICLR.cc/2026/Conference"],
            ),
            patch("smokecheck.httpx.AsyncClient", return_value=context),
        ):
            count, detail = asyncio.run(_openreview())

        self.assertEqual(count, 0)
        self.assertIn("响应契约正常", detail)
        self.assertEqual(client.get.await_count, 1)
        params = client.get.await_args.kwargs["params"]
        self.assertEqual(params["limit"], 1)
        self.assertEqual(params["offset"], 0)

    def test_optional_source_rate_limit_is_visible_but_not_blocking(self) -> None:
        request = httpx.Request("GET", "https://api2.openreview.net/notes/search")
        response = httpx.Response(429, request=request)

        async def rate_limited():
            raise httpx.HTTPStatusError(
                "rate limited",
                request=request,
                response=response,
            )

        result = asyncio.run(
            _check(
                "OpenReview",
                True,
                rate_limited,
                require_results=False,
                blocking_on_failure=False,
            )
        )

        self.assertEqual(result.status, "degraded")
        self.assertFalse(result.blocking)
        self.assertFalse(smoke_failed([result]))

    def test_required_configuration_missing_blocks_deployment(self) -> None:
        result = asyncio.run(
            _check(
                "OpenAlex Works",
                False,
                AsyncMock(),
                require_results=True,
                required_config=True,
                skipped_detail="未配置 OPENALEX_API_KEY",
            )
        )

        self.assertEqual(result.status, "failed")
        self.assertTrue(result.blocking)
        self.assertTrue(smoke_failed([result]))

    def test_per_check_deadline_stops_unbounded_operation(self) -> None:
        async def too_slow():
            await asyncio.sleep(0.1)
            return 1, "late"

        with patch.object(config, "SMOKE_CHECK_TIMEOUT_SECONDS", 0.01):
            result = asyncio.run(
                _check(
                    "slow",
                    True,
                    too_slow,
                    require_results=True,
                )
            )

        self.assertEqual(result.status, "failed")
        self.assertIn("Timeout", result.detail)

    def test_only_blocking_failures_make_smoke_fail(self) -> None:
        results = [
            SmokeResult("optional", "degraded", blocking=False),
            SmokeResult("ok", "passed"),
            SmokeResult("skip", "skipped", blocking=False),
        ]
        self.assertFalse(smoke_failed(results))
        self.assertTrue(
            smoke_failed([*results, SmokeResult("required", "failed")])
        )


if __name__ == "__main__":
    unittest.main()
