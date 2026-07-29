from __future__ import annotations

import ast
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import config
from smokecheck import (
    SmokeResult,
    _arxiv,
    _check,
    _check_models,
    _follow_builders,
    _github,
    _huggingface,
    _openalex_works,
    _openreview,
    _priority_pages,
    _research_feeds,
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
    def test_smoke_module_does_not_import_production_sources(self) -> None:
        tree = ast.parse(Path("smokecheck.py").read_text(encoding="utf-8"))
        source_imports = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("sources")
        ]
        self.assertEqual(source_imports, [])

    def test_arxiv_smoke_uses_exactly_one_stable_id_request(self) -> None:
        response = httpx.Response(
            200,
            text=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<feed xmlns="http://www.w3.org/2005/Atom">'
                "<entry><id>http://arxiv.org/abs/1706.03762</id></entry>"
                "</feed>"
            ),
            request=httpx.Request("GET", "https://export.arxiv.org/api/query"),
        )
        context, client = _client_context(response)
        with patch("smokecheck.httpx.AsyncClient", return_value=context):
            count, detail = asyncio.run(_arxiv())

        self.assertEqual(count, 1)
        self.assertIn("单次精确 ID 请求", detail)
        self.assertEqual(client.get.await_count, 1)
        params = client.get.await_args.kwargs["params"]
        self.assertEqual(params, {"id_list": "1706.03762", "max_results": 1})
        self.assertNotIn("search_query", params)

    def test_public_source_transient_limit_is_degraded_not_blocking(self) -> None:
        request = httpx.Request("GET", "https://export.arxiv.org/api/query")
        response = httpx.Response(429, request=request)

        async def rate_limited():
            raise httpx.HTTPStatusError(
                "rate limited",
                request=request,
                response=response,
            )

        result = asyncio.run(
            _check(
                "arXiv",
                True,
                rate_limited,
                require_results=True,
                required_config=True,
                degrade_on_transient=True,
            )
        )

        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.failure_kind, "transient_availability")
        self.assertFalse(result.blocking)
        self.assertFalse(smoke_failed([result]))

    def test_public_source_contract_error_still_blocks(self) -> None:
        request = httpx.Request("GET", "https://export.arxiv.org/api/query")
        response = httpx.Response(404, request=request)

        async def missing_contract():
            raise httpx.HTTPStatusError(
                "missing",
                request=request,
                response=response,
            )

        result = asyncio.run(
            _check(
                "arXiv",
                True,
                missing_contract,
                require_results=True,
                required_config=True,
                degrade_on_transient=True,
            )
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_kind, "http_contract")
        self.assertTrue(result.blocking)

    def test_huggingface_smoke_uses_exactly_one_request(self) -> None:
        response = httpx.Response(
            200,
            json=[{"paper": {"title": "A Paper"}}],
            request=httpx.Request(
                "GET", "https://huggingface.co/api/daily_papers"
            ),
        )
        context, client = _client_context(response)
        with patch("smokecheck.httpx.AsyncClient", return_value=context):
            count, detail = asyncio.run(_huggingface())

        self.assertEqual(count, 1)
        self.assertIn("单次请求", detail)
        self.assertEqual(client.get.await_count, 1)

    def test_follow_builders_smoke_reads_only_one_feed(self) -> None:
        response = httpx.Response(
            200,
            json={"x": [{"name": "Builder", "tweets": [{"text": "hello"}]}]},
            request=httpx.Request(
                "GET", "https://example.com/feed-x.json"
            ),
        )
        context, client = _client_context(response)
        with (
            patch.object(
                config,
                "FOLLOW_BUILDERS_FEED_URL",
                "https://example.com",
            ),
            patch("smokecheck.httpx.AsyncClient", return_value=context),
        ):
            count, detail = asyncio.run(_follow_builders())

        self.assertEqual(count, 1)
        self.assertIn("tweets=1", detail)
        self.assertEqual(client.get.await_count, 1)
        self.assertEqual(
            client.get.await_args.args[0],
            "https://example.com/feed-x.json",
        )

    def test_follow_builders_smoke_supports_local_feed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            feed = Path(directory) / "feed-x.json"
            feed.write_text(
                json.dumps({"x": [{"tweets": []}]}),
                encoding="utf-8",
            )
            with patch.object(
                config,
                "FOLLOW_BUILDERS_FEED_URL",
                f"file://{directory}",
            ):
                count, detail = asyncio.run(_follow_builders())

        self.assertEqual(count, 1)
        self.assertIn("单次本地文件读取", detail)

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

    def test_priority_page_smoke_fetches_only_one_index_page(self) -> None:
        response = httpx.Response(
            200,
            text="<html><body>" + '<a href="/paper">Paper</a>' * 20 + "</body></html>",
            request=httpx.Request("GET", "https://deepmind.google/research/"),
        )
        context, client = _client_context(response)
        with (
            patch.object(
                config,
                "PRIORITY_RESEARCH_PAGES",
                [
                    "https://deepmind.google/research/",
                    "https://www.moonshot.ai/",
                ],
            ),
            patch("smokecheck.httpx.AsyncClient", return_value=context),
        ):
            count, detail = asyncio.run(_priority_pages())

        self.assertEqual(count, 1)
        self.assertIn("单次索引页请求", detail)
        self.assertEqual(client.get.await_count, 1)

    def test_research_feed_smoke_fetches_only_first_feed(self) -> None:
        response = httpx.Response(
            200,
            text="<rss><channel><item><title>Paper</title></item></channel></rss>",
            request=httpx.Request("GET", "https://example.com/feed.xml"),
        )
        context, client = _client_context(response)
        with (
            patch.object(
                config,
                "RESEARCH_FEED_URLS",
                [
                    "https://example.com/feed.xml",
                    "https://example.org/feed.xml",
                ],
            ),
            patch("smokecheck.httpx.AsyncClient", return_value=context),
        ):
            count, detail = asyncio.run(_research_feeds())

        self.assertEqual(count, 1)
        self.assertIn("单次 Feed 请求", detail)
        self.assertEqual(client.get.await_count, 1)
        self.assertEqual(
            client.get.await_args.args[0],
            "https://example.com/feed.xml",
        )

    def test_github_smoke_does_not_make_separate_rate_limit_request(self) -> None:
        response = httpx.Response(
            200,
            json={"items": [{"full_name": "QwenLM/Qwen"}]},
            headers={"x-ratelimit-remaining": "29"},
            request=httpx.Request(
                "GET", "https://api.github.com/search/repositories"
            ),
        )
        context, client = _client_context(response)
        with (
            patch.object(config, "GITHUB_TOKEN", "configured"),
            patch("smokecheck.httpx.AsyncClient", return_value=context),
        ):
            count, detail = asyncio.run(_github())

        self.assertEqual(count, 1)
        self.assertIn("单次 Search 请求", detail)
        self.assertIn("search_remaining=29", detail)
        self.assertEqual(client.get.await_count, 1)

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
        self.assertEqual(result.failure_kind, "transient_availability")

    def test_llm_probe_uses_smoke_deadline_not_business_timeout(self) -> None:
        async def too_slow(**kwargs):
            await asyncio.sleep(0.1)
            return SimpleNamespace(choices=[])

        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(side_effect=too_slow))
            )
        )
        resolved = SimpleNamespace(label="dashscope/qwen3.7-plus")
        with (
            patch("smokecheck.resolve_all", return_value=[resolved, resolved]),
            patch("smokecheck.build_client", return_value=(client, "qwen3.7-plus")),
            patch.object(config, "SMOKE_CHECK_TIMEOUT_SECONDS", 0.01),
        ):
            results = asyncio.run(_check_models())

        self.assertEqual(results[0].status, "failed")
        self.assertEqual(results[0].failure_kind, "transient_availability")
        self.assertIn("Timeout", results[0].detail)
        self.assertEqual(results[1].status, "skipped")

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
