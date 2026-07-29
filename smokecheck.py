"""小成本真实连通性检查：不运行完整流水线，也不发送邮件。"""

from __future__ import annotations

import asyncio
import json
import smtplib
import ssl
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

import config
from agents.llm_utils import build_client, resolve_all


ARXIV_API = "https://export.arxiv.org/api/query"
HF_PAPERS_API = "https://huggingface.co/api/daily_papers"
SMOKE_CONTRACT_VERSION = "2026-07-30.1"


@dataclass
class SmokeResult:
    name: str
    status: str
    count: int = 0
    latency_ms: int = 0
    detail: str = ""
    blocking: bool = True
    failure_kind: str = ""


async def run_smoke_checks(
    *,
    include_llm: bool = True,
    include_smtp: bool = True,
    include_tavily: bool = True,
    write_json: bool = True,
) -> list[SmokeResult]:
    """顺序执行，避免 smoke test 自己触发平台并发限流。"""
    results: list[SmokeResult] = []

    if include_llm:
        results.extend(await _check_models())
    else:
        results.append(_skipped("Qwen 模型", "命令行要求跳过模型请求"))

    results.append(
        await _check(
            "arXiv",
            True,
            _arxiv,
            require_results=True,
            required_config=True,
            degrade_on_transient=True,
        )
    )
    results.append(
        await _check(
            "Hugging Face Daily Papers",
            True,
            _huggingface,
            require_results=True,
            blocking_on_failure=False,
        )
    )
    results.append(
        await _check(
            "Follow Builders",
            config.FOLLOW_BUILDERS_ENABLED,
            _follow_builders,
            require_results=True,
            skipped_detail="FOLLOW_BUILDERS_ENABLED=false",
            blocking_on_failure=False,
        )
    )
    results.append(
        await _check(
            "OpenAlex Works",
            bool(config.OPENALEX_API_KEY),
            _openalex_works,
            require_results=True,
            skipped_detail="未配置 OPENALEX_API_KEY",
            required_config=True,
            degrade_on_transient=True,
        )
    )
    results.append(
        await _check(
            "OpenAlex Authors",
            bool(config.OPENALEX_API_KEY),
            _openalex_authors,
            require_results=True,
            skipped_detail="未配置 OPENALEX_API_KEY",
            required_config=True,
            degrade_on_transient=True,
        )
    )
    results.append(
        await _check(
            "OpenReview",
            bool(config.OPENREVIEW_VENUES),
            _openreview,
            require_results=False,
            skipped_detail="未配置 OPENREVIEW_VENUES",
            blocking_on_failure=False,
        )
    )
    results.append(
        await _check(
            "官方研究页解析",
            bool(config.PRIORITY_RESEARCH_PAGES),
            _priority_pages,
            require_results=True,
            skipped_detail="没有高优先级官方研究页",
            required_config=True,
            degrade_on_transient=True,
        )
    )
    results.append(
        await _check(
            "研究 RSS/Atom",
            bool(config.RESEARCH_FEED_URLS),
            _research_feeds,
            require_results=True,
            skipped_detail="未配置 RESEARCH_FEED_URLS",
            blocking_on_failure=False,
        )
    )
    results.append(
        await _check(
            "GitHub Search",
            bool(config.GITHUB_TOKEN),
            _github,
            require_results=True,
            skipped_detail="未配置 GITHUB_TOKEN；不会匿名调用 Search API",
            required_config=True,
            degrade_on_transient=True,
        )
    )
    results.append(
        await _check(
            "Hacker News Algolia",
            True,
            _hackernews,
            require_results=True,
            blocking_on_failure=False,
        )
    )
    if include_tavily:
        results.append(
            await _check(
                "Tavily 社区网页索引",
                bool(config.TAVILY_SOCIAL_SEARCH_ENABLED and config.TAVILY_API_KEY),
                _tavily,
                require_results=True,
                skipped_detail="未启用或未配置 TAVILY_API_KEY",
            )
        )
    else:
        results.append(_skipped("Tavily 社区网页索引", "命令行要求跳过，未消耗 credit"))
    results.append(
        await _check(
            "Semantic Scholar",
            bool(
                config.SEMANTIC_SCHOLAR_ENABLED
                and config.SEMANTIC_SCHOLAR_API_KEY
            ),
            _semantic_scholar,
            require_results=True,
            skipped_detail="未显式启用或没有获批 Key；已确认不会匿名请求",
            blocking_on_failure=False,
        )
    )
    results.append(
        _skipped("Reddit 官方 API", "未获批或 OAuth 配置不完整；运行时正确跳过")
        if not _reddit_configured()
        else await _check(
            "Reddit 官方 API",
            True,
            _reddit,
            require_results=False,
            blocking_on_failure=False,
        )
    )
    results.append(
        _skipped("X Recent Search", "未配置 TWITTER_BEARER_TOKEN；运行时正确跳过")
        if not config.TWITTER_BEARER_TOKEN
        else await _check(
            "X Recent Search",
            True,
            _x_recent_search,
            require_results=False,
            blocking_on_failure=False,
        )
    )

    if include_smtp:
        results.append(
            await _check(
                "SMTP 登录（不发信）",
                _smtp_configured(),
                _smtp,
                require_results=False,
                skipped_detail="SMTP 配置不完整",
                required_config=True,
            )
        )
    else:
        results.append(_skipped("SMTP 登录（不发信）", "命令行要求跳过 SMTP"))

    if write_json:
        _write_results(results)
    return results


def print_smoke_results(results: list[SmokeResult]) -> None:
    labels = {"passed": "✓", "degraded": "△", "failed": "✗", "skipped": "–"}
    print(
        "\nAI 技术范式雷达 — 小成本真实 Smoke Test "
        f"(contract {SMOKE_CONTRACT_VERSION})\n"
    )
    for item in results:
        count = f"，结果={item.count}" if item.count else ""
        latency = f"，{item.latency_ms}ms" if item.latency_ms else ""
        failure_kind = (
            f"，failure_kind={item.failure_kind}" if item.failure_kind else ""
        )
        print(
            f"{labels.get(item.status, '?')} {item.name}: "
            f"{item.status}{count}{latency}{failure_kind}；{item.detail}"
        )
    failed = [item.name for item in results if item.status == "failed"]
    degraded = [item.name for item in results if item.status == "degraded"]
    print(
        f"\n结论：阻断失败 {len(failed)} 项；"
        f"非阻断降级 {len(degraded)} 项；"
        f"部署判定={'失败' if failed else '通过'}。"
    )
    print("明细已写入 logs/smoke_test_latest.json（不含任何密钥或响应正文）。\n")


def smoke_failed(results: list[SmokeResult]) -> bool:
    return any(item.status == "failed" for item in results)


async def _check(
    name: str,
    configured: bool,
    operation,
    *,
    require_results: bool,
    skipped_detail: str = "",
    required_config: bool = False,
    blocking_on_failure: bool = True,
    degrade_on_transient: bool = False,
) -> SmokeResult:
    if not configured:
        if required_config:
            return SmokeResult(
                name=name,
                status="failed",
                detail=skipped_detail or "必需配置缺失",
                blocking=True,
                failure_kind="configuration",
            )
        return _skipped(name, skipped_detail or "未配置")
    started = time.monotonic()
    failure_status = "failed" if blocking_on_failure else "degraded"
    try:
        count, detail = await asyncio.wait_for(
            operation(),
            timeout=config.SMOKE_CHECK_TIMEOUT_SECONDS,
        )
        latency = int((time.monotonic() - started) * 1000)
        if require_results and count <= 0:
            return SmokeResult(
                name=name,
                status=failure_status,
                count=0,
                latency_ms=latency,
                detail=detail or "接口可连接，但没有返回可用结果",
                blocking=blocking_on_failure,
                failure_kind="empty_response",
            )
        return SmokeResult(
            name,
            "passed",
            count,
            latency,
            detail,
            blocking=blocking_on_failure,
        )
    except Exception as exc:
        transient = degrade_on_transient and _is_transient_failure(exc)
        return SmokeResult(
            name=name,
            status="degraded" if transient else failure_status,
            latency_ms=int((time.monotonic() - started) * 1000),
            detail=_safe_error(exc),
            blocking=False if transient else blocking_on_failure,
            failure_kind=_failure_kind(exc),
        )


async def _check_models() -> list[SmokeResult]:
    results = []
    seen: set[str] = set()
    for role, resolved in zip(("sub", "main"), resolve_all()):
        if resolved.label in seen:
            results.append(_skipped(f"Qwen {role}", f"与另一角色共用 {resolved.label}，不重复计费"))
            continue
        seen.add(resolved.label)
        started = time.monotonic()
        try:
            client, model = build_client(role)
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "只回复 OK"}],
                    temperature=0,
                    max_tokens=16,
                ),
                timeout=config.SMOKE_CHECK_TIMEOUT_SECONDS,
            )
            content = (response.choices[0].message.content or "").strip()
            usage = getattr(response, "usage", None)
            total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
            results.append(
                SmokeResult(
                    f"Qwen {role}",
                    "passed" if content else "failed",
                    1 if content else 0,
                    int((time.monotonic() - started) * 1000),
                    f"{resolved.label}；usage.total_tokens={total_tokens}",
                    failure_kind="" if content else "empty_response",
                )
            )
        except Exception as exc:
            results.append(
                SmokeResult(
                    f"Qwen {role}",
                    "failed",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    detail=_safe_error(exc),
                    failure_kind=_failure_kind(exc),
                )
            )
    return results


async def _arxiv() -> tuple[int, str]:
    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True,
        headers={
            "Accept": "application/atom+xml",
            "User-Agent": "AI-Paradigm-Radar/3.2",
        },
    ) as client:
        response = await client.get(
            ARXIV_API,
            params={
                # 精确查询一个长期稳定的公开记录；Smoke 不运行任何生产检索车道。
                "id_list": "1706.03762",
                "max_results": 1,
            },
        )
        response.raise_for_status()
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as exc:
        raise ValueError("arXiv 响应不是有效 Atom XML") from exc
    entries = root.findall("{http://www.w3.org/2005/Atom}entry")
    return len(entries), "单次精确 ID 请求；HTTP 与 Atom 响应契约正常"


async def _huggingface() -> tuple[int, str]:
    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True,
        headers={
            "Accept": "application/json",
            "User-Agent": "AI-Paradigm-Radar/3.2",
        },
    ) as client:
        response = await client.get(HF_PAPERS_API)
        response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("Hugging Face Daily Papers 响应不是列表")
    sample = ""
    if payload:
        paper = payload[0].get("paper") if isinstance(payload[0], dict) else {}
        if isinstance(paper, dict):
            sample = str(paper.get("title", ""))
    return len(payload), f"单次请求；示例={sample or '无标题'}"


async def _follow_builders() -> tuple[int, str]:
    base = config.FOLLOW_BUILDERS_FEED_URL.rstrip("/")
    if base.startswith("file://"):
        path = Path(base[7:]) / "feed-x.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        request_detail = "单次本地文件读取"
    else:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={
                "Accept": "application/json",
                "User-Agent": "AI-Paradigm-Radar/3.2",
            },
        ) as client:
            response = await client.get(f"{base}/feed-x.json")
            response.raise_for_status()
        payload = response.json()
        request_detail = "单次 feed-x.json 请求"
    if not isinstance(payload, dict) or not isinstance(payload.get("x"), list):
        raise ValueError("Follow Builders 响应缺少 x 列表")
    builders = payload["x"]
    tweets = sum(
        len(builder.get("tweets", []))
        for builder in builders
        if isinstance(builder, dict) and isinstance(builder.get("tweets", []), list)
    )
    return len(builders), f"{request_detail}；builders={len(builders)}；tweets={tweets}"


async def _openalex_works() -> tuple[int, str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).date()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://api.openalex.org/works",
            headers={"User-Agent": "AI-Paradigm-Radar/3.2"},
            params={
                "api_key": config.OPENALEX_API_KEY,
                "search": '"world model"',
                "filter": f"from_publication_date:{cutoff.isoformat()}",
                "sort": "relevance_score:desc,publication_date:desc",
                "per-page": 2,
            },
        )
        response.raise_for_status()
        payload = response.json()
    items = payload.get("results")
    if not isinstance(items, list):
        raise ValueError("OpenAlex Works 响应缺少 results 列表")
    sample = str(items[0].get("display_name", "")) if items else "无作品结果"
    return len(items), f"单次请求；示例={sample}"


async def _openalex_authors() -> tuple[int, str]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://api.openalex.org/authors",
            headers={"User-Agent": "AI-Paradigm-Radar/3.2"},
            params={
                "api_key": config.OPENALEX_API_KEY,
                "search": "Yann LeCun",
                "per-page": 2,
            },
        )
        response.raise_for_status()
        items = response.json().get("results")
    if not isinstance(items, list):
        raise ValueError("OpenAlex Authors 响应缺少 results 列表")
    sample = str(items[0].get("display_name", "")) if items else "无作者结果"
    return len(items), f"单次请求；示例={sample}"


async def _openreview() -> tuple[int, str]:
    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True,
        headers={
            "Accept": "application/json",
            "User-Agent": "AI-Paradigm-Radar/3.2",
        },
    ) as client:
        response = await client.get(
            "https://api2.openreview.net/notes/search",
            params={
                "query": "world model",
                "venueid": config.OPENREVIEW_VENUES[0],
                "limit": 1,
                "offset": 0,
                "sort": "tmdate:desc",
                "details": "replyCount",
            },
        )
        response.raise_for_status()
        notes = response.json().get("notes")
    if not isinstance(notes, list):
        raise ValueError("OpenReview 响应缺少 notes 列表")
    return len(notes), "单次请求；HTTP 与 notes 响应契约正常"


async def _priority_pages() -> tuple[int, str]:
    preferred_hosts = {
        "www.moonshot.ai",
        "qwenlm.github.io",
        "deepmind.google",
    }
    pages = [
        page
        for page in config.PRIORITY_RESEARCH_PAGES
        if (urlparse(page).hostname or "") in preferred_hosts
    ][:1]
    if not pages:
        pages = config.PRIORITY_RESEARCH_PAGES[:1]
    page = pages[0]
    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True,
        headers={"User-Agent": "AI-Paradigm-Radar/3.2"},
    ) as client:
        response = await client.get(page)
        response.raise_for_status()
    body = response.text.strip()
    if len(body) < 200 or not any(
        marker in body.casefold() for marker in ("<html", "<a ", "__next_data__")
    ):
        raise ValueError("官方研究页响应不像可解析的 HTML")
    return 1, f"单次索引页请求；host={urlparse(page).hostname or 'unknown'}"


async def _research_feeds() -> tuple[int, str]:
    feed_url = config.RESEARCH_FEED_URLS[0]
    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True,
        headers={
            "Accept": "application/rss+xml, application/atom+xml, application/xml",
            "User-Agent": "AI-Paradigm-Radar/3.2",
        },
    ) as client:
        response = await client.get(feed_url)
        response.raise_for_status()
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as exc:
        raise ValueError("研究 Feed 响应不是有效 XML") from exc
    nodes = root.findall(".//item") or root.findall(
        "{http://www.w3.org/2005/Atom}entry"
    )
    return len(nodes), f"单次 Feed 请求；host={urlparse(feed_url).hostname or 'unknown'}"


async def _github() -> tuple[int, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "AI-Paradigm-Radar/3.2",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://api.github.com/search/repositories",
            headers=headers,
            params={"q": "qwen in:name,description", "per_page": 2},
        )
        response.raise_for_status()
        items = response.json().get("items")
    if not isinstance(items, list):
        raise ValueError("GitHub Search 响应缺少 items 列表")
    remaining = response.headers.get("x-ratelimit-remaining", "unknown")
    return len(items), (
        f"单次 Search 请求；search_remaining={remaining}；"
        f"示例={items[0].get('full_name', '') if items else '无'}"
    )


async def _hackernews() -> tuple[int, str]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://hn.algolia.com/api/v1/search",
            params={"query": "world model", "tags": "story", "hitsPerPage": 3},
        )
        response.raise_for_status()
        items = response.json().get("hits", [])
    return len(items), f"示例={items[0].get('title', '') if items else '无'}"


async def _tavily() -> tuple[int, str]:
    domains = [
        domain
        for domain in config.TAVILY_SOCIAL_SEARCH_DOMAINS
        if domain in {"x.com", "twitter.com", "reddit.com"}
    ] or ["reddit.com", "x.com"]
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            headers={
                "Authorization": f"Bearer {config.TAVILY_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "query": '"V-JEPA 2" AI',
                "search_depth": "basic",
                "max_results": 3,
                "topic": "general",
                "time_range": "year",
                "include_domains": domains,
                "include_answer": False,
                "include_raw_content": False,
                "include_usage": True,
            },
        )
        response.raise_for_status()
        payload = response.json()
    items = payload.get("results", [])
    usage = payload.get("usage") or {}
    return len(items), f"本次仅 1 个 basic 请求；usage={usage}"


async def _semantic_scholar() -> tuple[int, str]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            headers={
                "x-api-key": config.SEMANTIC_SCHOLAR_API_KEY,
                "User-Agent": "AI-Paradigm-Radar/3.2",
            },
            params={
                "query": "Attention Is All You Need",
                "limit": 1,
                "fields": "title,year,url",
            },
        )
        response.raise_for_status()
        items = response.json().get("data", [])
    return len(items), f"示例={items[0].get('title', '') if items else '无'}"


async def _reddit() -> tuple[int, str]:
    async with httpx.AsyncClient(timeout=20) as client:
        token_response = await client.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET),
            headers={"User-Agent": config.REDDIT_USER_AGENT},
            data={"grant_type": "client_credentials"},
        )
        token_response.raise_for_status()
        token = str(token_response.json().get("access_token", ""))
        if not token:
            return 0, "OAuth 响应没有 access_token"
        response = await client.get(
            "https://oauth.reddit.com/search",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": config.REDDIT_USER_AGENT,
            },
            params={"q": '"V-JEPA 2"', "limit": 3, "sort": "relevance"},
        )
        response.raise_for_status()
        items = ((response.json().get("data") or {}).get("children") or [])
    return len(items), "OAuth 与搜索接口均可连接"


async def _x_recent_search() -> tuple[int, str]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://api.x.com/2/tweets/search/recent",
            headers={"Authorization": f"Bearer {config.TWITTER_BEARER_TOKEN}"},
            params={"query": '"V-JEPA 2" -is:retweet', "max_results": 10},
        )
        response.raise_for_status()
        items = response.json().get("data", [])
    return len(items), "Recent Search 鉴权通过"


async def _smtp() -> tuple[int, str]:
    await asyncio.to_thread(_smtp_login)
    return 1, "已建立连接并完成登录；没有发送邮件"


def _smtp_login() -> None:
    context = ssl.create_default_context()
    if config.SMTP_USE_SSL:
        with smtplib.SMTP_SSL(
            config.SMTP_HOST,
            config.SMTP_PORT,
            timeout=20,
            context=context,
        ) as server:
            server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        return
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as server:
        if config.SMTP_USE_STARTTLS:
            server.starttls(context=context)
        server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)


def _smtp_configured() -> bool:
    return bool(
        config.SMTP_HOST
        and config.SMTP_PORT
        and config.SMTP_USERNAME
        and config.SMTP_PASSWORD
    )


def _reddit_configured() -> bool:
    return bool(
        config.REDDIT_API_ACCESS_APPROVED
        and config.REDDIT_CLIENT_ID
        and config.REDDIT_CLIENT_SECRET
        and config.REDDIT_USER_AGENT
    )


def _skipped(name: str, detail: str) -> SmokeResult:
    return SmokeResult(
        name=name,
        status="skipped",
        detail=detail,
        blocking=False,
    )


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        host = urlparse(str(response.url)).hostname or "unknown"
        if response.status_code == 401 and host == "api.github.com":
            return (
                "HTTP 401 (api.github.com)：Token 无效、已撤销或粘贴错误；"
                "只读 Search 不需要额外仓库权限"
            )
        if response.status_code == 401 and host == "api.tavily.com":
            return "HTTP 401 (api.tavily.com)：TAVILY_API_KEY 无效"
        if response.status_code == 429:
            return (
                f"HTTP 429 ({host})：第三方接口临时限流；"
                "最小请求已停止，不继续生产级分页"
            )
        return f"HTTP {response.status_code} ({host})"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return (
            f"TimeoutError：单项超过 {config.SMOKE_CHECK_TIMEOUT_SECONDS}s，"
            "已停止该接口检查"
        )
    return f"{type(exc).__name__}: {str(exc)[:240]}"


def _is_transient_failure(exc: Exception) -> bool:
    """只把平台临时不可用降级；鉴权、404 和响应契约变化仍然阻断。"""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status in {408, 425, 429} or status >= 500
    return isinstance(
        exc,
        (
            TimeoutError,
            httpx.TimeoutException,
            httpx.TransportError,
        ),
    )


def _failure_kind(exc: Exception) -> str:
    if _is_transient_failure(exc):
        return "transient_availability"
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code in {401, 403}:
            return "authentication_or_permission"
        return "http_contract"
    if isinstance(exc, (ValueError, json.JSONDecodeError, ET.ParseError)):
        return "response_contract"
    return "runtime"


def _write_results(results: list[SmokeResult]) -> None:
    path = Path("logs/smoke_test_latest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "contract_version": SMOKE_CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "passed": sum(item.status == "passed" for item in results),
            "degraded": sum(item.status == "degraded" for item in results),
            "failed": sum(item.status == "failed" for item in results),
            "skipped": sum(item.status == "skipped" for item in results),
            "deployment_ready": not smoke_failed(results),
        },
        "results": [asdict(item) for item in results],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
