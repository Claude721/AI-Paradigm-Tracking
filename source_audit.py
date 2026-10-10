"""Bounded, per-entry input acceptance. Never runs research, LLM or SMTP.

Samples are deliberately not campaign coverage receipts. Response bodies live
only in memory; persistence round-trips use an isolated temporary SQLite store.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import time
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse, urlunparse, urljoin

import httpx

import config
from database.paradigm_store import ParadigmStore
from paradigms.discovery import _raw_to_origin, _hf_to_support, _follow_builder_to_support, _follow_builder_blog_to_origin
from paradigms.landscape import arxiv_query_plan, arxiv_priority_author_query_plan
from paradigms.models import TechnicalEvidence, technical_evidence_from_dict, _evidence_datetime
from research_watchlist import KOL_SOURCES, OFFICIAL_GITHUB_ORGANIZATIONS
from runtime_clock import research_window
from runtime_provenance import runtime_provenance, source_fingerprint, ROOT as PROVENANCE_ROOT
from sources.arxiv_source import ArxivSource, ARXIV_API, TECHNICAL_REPORT_QUERY
from sources.curated_intelligence_source import HighSignalForumSource, _parse_feed
from sources.follow_builders_source import FollowBuildersSource
from sources.hf_papers_source import HuggingFacePapersSource, HF_PAPERS_API
from sources.openalex_source import OpenAlexSource, OPENALEX_WORKS_API
from sources.openreview_source import OpenReviewSource, OPENREVIEW_SEARCH_API, DEFAULT_SEARCHES, _note_matches_venue, _note_matches_query, _search_page
from sources.priority_research_source import PriorityResearchPageSource, _AnchorParser, _discover_index_links
from sources.research_feed_source import ResearchFeedSource

CONTRACT_VERSION = 2


def safe_url(value: str) -> str:
    """Never persist query values, userinfo, signed URLs or credentials."""
    try:
        parsed = urlparse(value)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        return urlunparse((parsed.scheme, host, parsed.path, "", "", ""))
    except ValueError:
        return "invalid-url"


def validate_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTP(S) URLs without userinfo are supported")
    if parsed.hostname.casefold() in {"localhost", "localhost.localdomain"} or parsed.hostname.endswith(".local"):
        raise ValueError("Local endpoints are not permitted")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("Private network endpoints are not permitted")


@dataclass
class Entry:
    key: str
    kind: str
    url: str
    params: dict = field(default_factory=dict, repr=False)
    metadata: dict = field(default_factory=dict, repr=False)
    enabled: bool = True
    credential: str = ""
    deferred_reason: str = ""

    def public(self) -> dict:
        return {"key": self.key, "kind": self.kind, "url": safe_url(self.url),
                "enabled": self.enabled, "credential_required": bool(self.credential),
                "credential_name": self.credential,
                "scope_hash": hashlib.sha256(json.dumps([self.kind, self.url, self.params], sort_keys=True).encode()).hexdigest()}


def build_entries() -> list[Entry]:
    """One manifest row for EVERY enabled configured page/feed/query/owner."""
    entries = [Entry(f"official:{i}", "official", url) for i, url in enumerate(config.PRIORITY_RESEARCH_PAGES)]
    for record in KOL_SOURCES:
        entries.extend(Entry(f"kol:{record['id']}:{i}", "kol", url, metadata={"record": record}, enabled=config.KOL_SOURCE_ENABLED)
                       for i, url in enumerate(record.get("feed_urls", ())))
    for i, url in enumerate(config.KOL_CUSTOM_FEED_URLS):
        record = {"id": f"custom-{i}", "name": "Custom KOL feed", "homepage": url, "origin_policy": "secondary_only"}
        entries.append(Entry(f"kol:custom:{i}", "kol", url, metadata={"record": record}, enabled=config.KOL_SOURCE_ENABLED))
    entries.extend(Entry(f"research-feed:{i}", "feed", url) for i, url in enumerate(config.RESEARCH_FEED_URLS))
    entries.extend(Entry(f"forum:{i}", "forum", str(feed["url"]), metadata={"forum": feed})
                   for i, feed in enumerate(HighSignalForumSource()._feeds()))
    source = FollowBuildersSource()
    for filename in ("feed-x.json", "feed-podcasts.json", "feed-blogs.json"):
        entries.append(Entry(f"follow:{filename}", "follow", f"{source.feed_base_url}/{filename}",
                             metadata={"filename": filename}, enabled=config.FOLLOW_BUILDERS_ENABLED))
    entries.append(Entry("hf:daily-papers", "hf", HF_PAPERS_API))
    entries.append(Entry("arxiv:exact-id", "arxiv", ARXIV_API, {"id_list": "1706.03762", "max_results": 1}, metadata={"ignore_lookback": True}))
    plans = [(item["query"], "domain") for item in arxiv_query_plan()]
    if config.PARADIGM_PRIORITY_AUTHOR_SWEEP_ENABLED:
        plans.extend((item["query"], "author") for item in arxiv_priority_author_query_plan(config.PRIORITY_RESEARCHERS))
    plans.append((TECHNICAL_REPORT_QUERY, "report"))
    for i, (query, lane) in enumerate(plans):
        entries.append(Entry(f"arxiv:{lane}:{i}", "arxiv", ARXIV_API,
                             {"search_query": query, "max_results": 2, "sortBy": "submittedDate", "sortOrder": "descending"}))
    for i, venue in enumerate(config.OPENREVIEW_VENUES):
        entries.append(Entry(f"openreview:venue:{i}", "openreview-group", "https://api2.openreview.net/groups", {"id": venue}))
        for j, query in enumerate(DEFAULT_SEARCHES):
            entries.append(Entry(f"openreview:{i}:{j}", "openreview", OPENREVIEW_SEARCH_API,
                                 {"term": query, "content": "all", "group": venue, "source": "forum", "count": True, "limit": 5, "offset": 0}))
    for i, query in enumerate(OpenAlexSource().searches):
        entries.append(Entry(f"openalex:works:{i}", "openalex", OPENALEX_WORKS_API,
                             {"search": query, "per-page": 2, "cursor": "*"}, credential="OPENALEX_API_KEY"))
    entries.append(Entry("openalex:authors", "openalex-authors", "https://api.openalex.org/authors", {"search": "Yoshua Bengio", "per-page": 1}, credential="OPENALEX_API_KEY"))
    for record in OFFICIAL_GITHUB_ORGANIZATIONS:
        entries.append(Entry(f"github:org:{record['login']}", "github", f"https://api.github.com/orgs/{record['login']}/repos",
                             {"type": "public", "sort": "created", "direction": "desc", "per_page": 2}, credential="GITHUB_TOKEN"))
    entries.append(Entry("github:search", "github-search", "https://api.github.com/search/repositories", {"q": '"1706.03762" in:readme', "per_page": 1}, credential="GITHUB_TOKEN"))
    entries.append(Entry("hn:search", "hn", "https://hn.algolia.com/api/v1/search", {"query": "transformer", "tags": "story", "hitsPerPage": 1}))
    entries.append(Entry("orcid:search", "orcid", "https://pub.orcid.org/v3.0/search/", {"q": 'family-name:Bengio AND given-names:Yoshua', "rows": 1}))
    # These may incur paid credits / require special platform approval. Keep
    # them in the manifest, but NEVER silently spend or claim they passed.
    for key, url, enabled, credential in (
        ("tavily", "https://api.tavily.com/search", config.TAVILY_SOCIAL_SEARCH_ENABLED, "TAVILY_API_KEY"),
        ("x", "https://api.x.com/2/tweets/search/recent", config.KOL_X_SOURCE_ENABLED, "TWITTER_BEARER_TOKEN"),
        ("reddit", "https://oauth.reddit.com/search", config.REDDIT_API_ACCESS_APPROVED, "REDDIT_CLIENT_SECRET"),
        ("semantic-scholar", "https://api.semanticscholar.org/graph/v1/paper/search", config.SEMANTIC_SCHOLAR_ENABLED, "SEMANTIC_SCHOLAR_API_KEY"),
    ):
        entries.append(Entry(key, "separate-acceptance", url, enabled=enabled, credential=credential,
                             metadata={"required_credentials": ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT"]} if key == "reddit" else {},
                             deferred_reason="requires_separate_authorized_platform_acceptance"))
    if len({entry.key for entry in entries}) != len(entries):
        raise ValueError("Duplicate source acceptance identity")
    return entries


class AuditLimit(RuntimeError):
    pass


class RecordingTransport(httpx.AsyncBaseTransport):
    """Shared hard request limit, per-entry limit, per-host pacing and size cap."""
    def __init__(self, inner, runner, row):
        self.inner, self.runner, self.row = inner, runner, row

    async def handle_async_request(self, request):
        try:
            validate_url(str(request.url))
        except ValueError:
            self.row["blocked_destination"] = True
            raise
        host = request.url.host
        lock = self.runner.host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            if host in self.runner.blocked_hosts:
                self.row["transport_limit"] = "provider_rate_limited"
                raise AuditLimit("provider_rate_limited")
            if self.runner.request_count >= self.runner.max_requests or self.row["requests"] >= 6:
                self.row["transport_limit"] = "request_limit"
                raise AuditLimit("request_limit")
            spacing = 3.1 if host == "export.arxiv.org" else 1.0
            await asyncio.sleep(max(0, spacing - (time.monotonic() - self.runner.host_started.get(host, 0))))
            # Other hosts can use the shared allowance while this one sleeps.
            # Recheck immediately before charging/sending, with no await gap.
            if self.runner.request_count >= self.runner.max_requests:
                self.row["transport_limit"] = "request_limit"
                raise AuditLimit("request_limit")
            self.runner.host_started[host] = time.monotonic()
            self.runner.request_count += 1
            self.row["requests"] += 1
            sent = time.monotonic()
            event = {"url": safe_url(str(request.url)), "status": None, "bytes": 0}
            self.row["http"].append(event)
            try:
                response = await self.inner.handle_async_request(request)
            except BaseException as exc:
                event.update(error_type=type(exc).__name__, elapsed_seconds=round(time.monotonic() - sent, 3))
                raise
            if response.status_code == 429 or (response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"):
                self.runner.blocked_hosts.add(host)
            chunks, length = [], 0
            event.update(status=response.status_code,
                         retry_after_present=bool(response.headers.get("retry-after")),
                         rate_limit_exhausted=response.headers.get("x-ratelimit-remaining") == "0",
                         access_challenge=bool(response.headers.get("x-vercel-mitigated") == "challenge" or response.headers.get("cf-mitigated") == "challenge"))
            retry_after = response.headers.get("retry-after", "")
            if re.fullmatch(r"\d+(?:\.\d+)?", retry_after):
                event["retry_after_seconds"] = min(float(retry_after), 86400)
            try:
                async for chunk in response.aiter_bytes():
                    length += len(chunk)
                    if length > 10_000_000:
                        self.row["transport_limit"] = "response_size_limit"
                        raise AuditLimit("response_size_limit")
                    chunks.append(chunk)
            finally:
                event["bytes"] = length
                event["elapsed_seconds"] = round(time.monotonic() - sent, 3)
                await response.aclose()
        headers = dict(response.headers)
        # aiter_bytes already decoded HTTP compression. Do not ask the caller
        # to decompress the same payload again (many public sites send gzip).
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)
        decoded = httpx.Response(response.status_code, headers=headers, content=b"".join(chunks), request=request)
        if self.row["kind"] == "official" and response.status_code == 200 and "index_diagnostics" not in self.row:
            parser = _AnchorParser()
            parser.feed(decoded.text)
            links = _discover_index_links(decoded.text, str(request.url))
            self.row["index_diagnostics"] = {
                "anchors": len(parser.anchors), "recognized_research_links": len(links),
                "anchor_sample": [{"title": title[:100], "url": safe_url(url)} for title, url in parser.anchors[:18]],
                "research_sample": [{"title": item.title[:100], "url": safe_url(item.url), "published_at": item.published_at} for item in links[:5]],
                "embedded_frames": [safe_url(urljoin(str(request.url), value)) for value in re.findall(r'<iframe[^>]+src=["\']([^"\']+)', decoded.text, re.I)[:5]],
                "scripts": [safe_url(urljoin(str(request.url), value)) for value in re.findall(r'<script[^>]+src=["\']([^"\']+)', decoded.text, re.I)[:5]],
            }
        return decoded

    async def aclose(self):
        # The run owns the shared pool; closing a per-entry client must not
        # disrupt healthy peer probes.
        pass


class SourceAudit:
    def __init__(self, *, transport=None, include_authenticated=False, include_platforms=False, timeout=45, budget=900, max_requests=300, concurrency=3):
        self.transport = transport or httpx.AsyncHTTPTransport()
        self.include_authenticated = include_authenticated
        # Explicit opt-in: one minimal request per enabled platform (Reddit
        # additionally needs OAuth). Never implied by include_authenticated.
        self.include_platforms = include_platforms
        self.timeout, self.budget, self.max_requests, self.concurrency = timeout, budget, max_requests, concurrency
        self.request_count = 0
        self.host_locks, self.host_started = {}, {}
        self.blocked_hosts = set()

    async def run(self, entries=None, *, reference_time=None):
        started = time.monotonic()
        initial_runtime = {**runtime_provenance(), "source_fingerprint": source_fingerprint(PROVENANCE_ROOT)}
        reference_time = reference_time or datetime.now(timezone.utc)
        if reference_time.tzinfo is None:
            raise ValueError("Audit reference clock must have a timezone")
        entries = list(build_entries() if entries is None else entries)
        if len({entry.key for entry in entries}) != len(entries):
            raise ValueError("Duplicate input acceptance identity")
        semaphore = asyncio.Semaphore(self.concurrency)
        with TemporaryDirectory(prefix="radar-source-audit-") as directory, research_window(reference_time):
            store = ParadigmStore(Path(directory) / "audit.db")
            async def run_one(entry):
                row = {**entry.public(), "status": "pending", "requests": 0, "http": [], "parsed": 0, "persisted": 0}
                if not entry.enabled:
                    row.update(status="disabled", reason="disabled_by_configuration")
                    return row
                required = entry.metadata.get("required_credentials", [entry.credential] if entry.credential else [])
                missing = [name for name in required if not getattr(config, name, "")]
                if missing:
                    row.update(status="not_configured", reason="credential_not_configured", missing_credentials=missing)
                    return row
                platform_allowed = entry.kind == "separate-acceptance" and self.include_platforms
                if (entry.deferred_reason and not platform_allowed) or (entry.credential and not self.include_authenticated and not platform_allowed):
                    row.update(status="not_tested", reason=entry.deferred_reason or "authenticated_checks_not_enabled")
                    return row
                async with semaphore:
                    remaining = self.budget - (time.monotonic() - started)
                    if remaining <= 0:
                        row.update(status="not_executed", reason="total_budget")
                        return row
                    entry_started = time.monotonic()
                    try:
                        row["input_url_checked"] = False
                        validate_url(entry.url)
                        row["input_url_checked"] = True
                        evidence, details = await asyncio.wait_for(self.probe(entry, row), min(self.timeout, remaining))
                        row.update(details)
                        row["parsed"] = len(evidence)
                        self.roundtrip(store, evidence)
                        row["persisted"] = len({item.fingerprint for item in evidence})
                        row["status"] = "passed"
                    except asyncio.TimeoutError:
                        row.update(status="failed", reason="timeout")
                    except httpx.HTTPStatusError as exc:
                        row.update(status="failed", reason=f"http_{exc.response.status_code}")
                    except Exception as exc:
                        # Never log exception text: HTTP errors can contain API
                        # keys, signed query strings or external response bodies.
                        reason = "bounded_request_limit" if isinstance(exc, AuditLimit) else type(exc).__name__
                        if isinstance(exc, ValueError) and row.get("coverage"):
                            reason = "production_parser_rejection"
                        if row["http"] and isinstance(row["http"][-1]["status"], int) and row["http"][-1]["status"] >= 400:
                            reason = f"http_{row['http'][-1]['status']}"
                        row.update(status="failed", reason=reason)
                        if row.get("transport_limit"):
                            row.update(status="not_executed" if row["transport_limit"] != "response_size_limit" else "not_tested", reason=row["transport_limit"])
                    row["elapsed_seconds"] = round(time.monotonic() - entry_started, 3)
                    return row
            try:
                rows = await asyncio.gather(*(run_one(entry) for entry in entries))
                for row in rows:
                    row["diagnosis"] = diagnose_entry(row)
            finally:
                await self.transport.aclose()
        counts = dict(Counter(row["status"] for row in rows))
        code_changed = initial_runtime.get("source_fingerprint") != source_fingerprint(PROVENANCE_ROOT)
        completed = len(rows) == len(entries) and not any(row["status"] in {"failed", "not_tested", "not_executed", "not_configured", "pending"} for row in rows)
        return {"contract_version": CONTRACT_VERSION, "reference_time": reference_time.isoformat(),
                "runtime_provenance": initial_runtime, "code_changed_during_run": code_changed, "input_manifest_hash": hashlib.sha256(json.dumps([entry.public() for entry in entries], sort_keys=True).encode()).hexdigest(),
                "planned_entries": len(entries), "returned_entries": len(rows), "counts": counts,
                "request_count": self.request_count, "elapsed_seconds": round(time.monotonic() - started, 3),
                "acceptance_complete": completed and not code_changed, "campaign_coverage_complete": False,
                "boundary": "逐入口有界样本；不证明完整分页、研究判断或生产 V0；不修改生产状态、不调用模型、不发送邮件。",
                "entries": rows}

    def client(self, row, **kwargs):
        kwargs.setdefault("timeout", 20)
        kwargs.setdefault("follow_redirects", True)
        kwargs.setdefault("headers", {"User-Agent": "AI-Paradigm-Radar/3.4 (bounded source acceptance)", "Accept": "application/json,application/atom+xml,application/rss+xml,text/html;q=0.9,*/*;q=0.8"})
        return httpx.AsyncClient(**kwargs, transport=RecordingTransport(self.transport, self, row))

    async def probe(self, entry, row):
        if entry.kind == "separate-acceptance":
            return await self.probe_platform(entry, row)
        if entry.kind == "official":
            source = PriorityResearchPageSource(pages=[entry.url], per_page=1, lookback_days=60,
                                               client_factory=lambda **kwargs: self.client(row, **kwargs))
            evidence = await source.fetch()
            coverage = source.coverage()
            page = coverage["pages"].get(entry.url, {})
            row["coverage"] = page
            if coverage["request_failed"] or coverage["parse_zero_links"] or coverage["detail_failures"] or coverage.get("unresolved_citations"):
                raise ValueError("Official index/detail failed its production parser")
            if page.get("status") == "parsed" and not evidence and not page.get("detail_outside_window"):
                raise ValueError("Selected official detail produced no verifiable evidence")
            return evidence, {"coverage": page, "sample_boundary": "最多一条详情；不证明分页与全部发布覆盖"}
        params = dict(entry.params)
        headers = {}
        if entry.credential == "OPENALEX_API_KEY":
            params["api_key"] = config.OPENALEX_API_KEY
        if entry.credential == "GITHUB_TOKEN":
            headers = {"Authorization": f"Bearer {config.GITHUB_TOKEN}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if entry.kind == "orcid":
            headers["Accept"] = "application/json"
        async with self.client(row) as client:
            response = await client.get(entry.url, params=params, headers=headers)
            response.raise_for_status()
        if entry.kind in {"kol", "forum"}:
            evidence, filtered = _parse_feed(response.text, feed_url=entry.url, lookback_days=60,
                                            record=entry.metadata.get("record"), forum=entry.metadata.get("forum"))
            return evidence, {"filtered": filtered}
        if entry.kind == "feed":
            return ResearchFeedSource(lookback_days=60)._parse(response.text, entry.url), {}
        if entry.kind == "arxiv":
            raw = ArxivSource(lookback_days=60)._parse_atom_feed(response.text, ignore_lookback=entry.metadata.get("ignore_lookback", False))
            return [_raw_to_origin(item) for item in raw], {"sample_boundary": "单页，最多两条；不证明生产查询分页闭合"}
        payload = response.json()
        if entry.kind == "follow":
            source = FollowBuildersSource()
            filename = entry.metadata["filename"]
            source.validate_feed(payload, filename)
            parser = {"feed-x.json": source._parse_x_feed, "feed-podcasts.json": source._parse_podcast_feed, "feed-blogs.json": source._parse_blog_feed}[filename]
            raw = parser(payload)
            converter = _follow_builder_blog_to_origin if filename == "feed-blogs.json" else _follow_builder_to_support
            return [converter(item) for item in raw], {"raw_records": len(raw)}
        if entry.kind == "hf":
            raw = HuggingFacePapersSource(lookback_days=60)._parse(payload)
            return [_raw_to_origin(item) for item in raw] + [_hf_to_support(item) for item in raw], {}
        if entry.kind in {"openalex", "openalex-authors"}:
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list) or not isinstance(payload.get("meta"), dict):
                raise ValueError("OpenAlex results/meta contract failed")
            if entry.kind == "openalex":
                return OpenAlexSource(lookback_days=60)._parse(payload["results"]), {"sample_boundary": "单页；未验收后续 cursor"}
            if any(not item.get("id") or not item.get("display_name") for item in payload["results"]):
                raise ValueError("OpenAlex author identity is missing")
            return [], {"protocol_records": len(payload["results"]), "sample_boundary": "身份 API 协议；不合并同名人物"}
        if entry.kind in {"openreview", "openreview-group"}:
            field = "notes" if entry.kind == "openreview" else "groups"
            if not isinstance(payload, dict) or not isinstance(payload.get(field), list):
                raise ValueError("OpenReview response contract failed")
            if entry.kind == "openreview-group" and not any(item.get("id") == entry.params["id"] for item in payload[field]):
                raise ValueError("Configured OpenReview venue does not exist publicly")
            evidence = []
            if entry.kind == "openreview":
                page, _ = _search_page(payload, int(entry.params.get("offset", 0)))
                matching = [note for note in page if _note_matches_venue(note, entry.params["group"]) and _note_matches_query(note, entry.params["term"])]
                evidence = OpenReviewSource(lookback_days=60)._parse_notes(matching)
            return evidence, {"protocol_records": len(payload[field]), "sample_boundary": "公开 group/首个搜索页；不证明完整分页"}
        if entry.kind in {"github", "github-search"}:
            items = payload if entry.kind == "github" else payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list) or any(not isinstance(item, dict) or not item.get("full_name") or not item.get("html_url") for item in items):
                raise ValueError("GitHub repository contract failed")
            return [], {"protocol_records": len(items), "sample_boundary": "组织列表/Search 协议；README、外部原点与全分页另需验收"}
        if entry.kind == "hn":
            if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
                raise ValueError("HN search hits contract failed")
            return [], {"protocol_records": len(payload["hits"])}
        if entry.kind == "orcid":
            if not isinstance(payload, dict) or "num-found" not in payload:
                raise ValueError("ORCID search contract failed")
            return [], {"protocol_records": int(payload.get("num-found") or 0)}
        raise ValueError("Unknown source acceptance kind")

    async def probe_platform(self, entry, row):
        if not self.include_platforms:
            raise ValueError("Platform acceptance is not authorized")
        async with self.client(row) as client:
            if entry.key == "tavily":
                response = await client.post(entry.url, headers={"Authorization": f"Bearer {config.TAVILY_API_KEY}"}, json={
                    "query": "artificial intelligence research discussion", "search_depth": "basic",
                    "max_results": 1, "include_answer": False, "include_raw_content": False, "include_usage": True,
                })
            elif entry.key == "semantic-scholar":
                response = await client.get(entry.url, headers={"x-api-key": config.SEMANTIC_SCHOLAR_API_KEY}, params={
                    "query": "Attention Is All You Need", "limit": 1, "fields": "title,year,url",
                })
            elif entry.key == "x":
                response = await client.get(entry.url, headers={"Authorization": f"Bearer {config.TWITTER_BEARER_TOKEN}"}, params={
                    "query": '"artificial intelligence" -is:retweet', "max_results": 10,
                })
            elif entry.key == "reddit":
                if not config.REDDIT_API_ACCESS_APPROVED:
                    raise ValueError("Reddit API approval must be explicitly configured")
                token_response = await client.post("https://www.reddit.com/api/v1/access_token",
                    auth=(config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET),
                    headers={"User-Agent": config.REDDIT_USER_AGENT}, data={"grant_type": "client_credentials"})
                token_response.raise_for_status()
                token = token_response.json().get("access_token")
                if not isinstance(token, str) or not token.strip():
                    raise ValueError("OAuth token contract failed")
                response = await client.get(entry.url, headers={"Authorization": f"Bearer {token}", "User-Agent": config.REDDIT_USER_AGENT},
                    params={"q": "artificial intelligence research", "limit": 1, "sort": "relevance"})
            else:
                raise ValueError("Unknown platform acceptance identity")
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Platform response must be an object")
        if entry.key == "tavily":
            items = payload.get("results")
            credits = (payload.get("usage") or {}).get("credits") if isinstance(payload.get("usage"), dict) else None
            row["platform_usage_credits"] = credits if type(credits) is int and credits >= 0 else None
        elif entry.key == "semantic-scholar":
            items = payload.get("data")
        elif entry.key == "reddit":
            items = payload.get("data", {}).get("children") if isinstance(payload.get("data"), dict) else None
        else:
            # X may omit data for a valid empty response, but must explicitly
            # confirm result_count=0; errors are not successful zero results.
            meta = payload.get("meta")
            if payload.get("errors") or not isinstance(meta, dict) or type(meta.get("result_count")) is not int:
                raise ValueError("X search meta/errors contract failed")
            items = payload.get("data", [] if meta["result_count"] == 0 else None)
            if not isinstance(items, list) or len(items) != meta["result_count"]:
                raise ValueError("X search identity/cardinality failed")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("Platform result list contract failed")
        for item in items:
            if entry.key == "tavily":
                if not isinstance(item.get("title"), str) or not item["title"].strip() or not isinstance(item.get("url"), str):
                    raise ValueError("Tavily result identity failed")
                validate_url(item["url"])
            elif entry.key == "semantic-scholar" and (not item.get("paperId") or not item.get("title")):
                raise ValueError("Semantic Scholar identity failed")
            elif entry.key == "x" and (not item.get("id") or not isinstance(item.get("text"), str)):
                raise ValueError("X post identity failed")
            elif entry.key == "reddit" and (not isinstance(item.get("data"), dict) or not item["data"].get("id") or not item["data"].get("title")):
                raise ValueError("Reddit post identity failed")
        return [], {"protocol_records": len(items), "sample_boundary": "显式授权的单次最小平台鉴权/协议样本；不调用模型、不证明平台覆盖或独立承接。"}

    @staticmethod
    def roundtrip(store, evidence):
        # Check input/output identity AND cardinality, including supports that
        # are not returned by load_pending_origins. No production DB is used.
        unique = {item.fingerprint: item for item in evidence}
        for item in unique.values():
            validate_url(item.url)
            if not item.title.strip():
                raise ValueError("Evidence title is empty")
            if item.published_at and not _evidence_datetime(item.published_at):
                raise ValueError("Evidence publication date is invalid")
        result = store.mark_evidence(list(unique.values()), source_observation=True)
        if result.rejected_count or {item.fingerprint for item in result.accepted} != set(unique):
            raise ValueError("Evidence persistence input/output identity mismatch")
        with store._connect() as connection:
            for key in unique:
                row = connection.execute("SELECT payload_json FROM evidence_state WHERE fingerprint=?", (key,)).fetchone()
                if row is None or technical_evidence_from_dict(json.loads(row[0])).fingerprint != key:
                    raise ValueError("Evidence persistence read-back failed")


def render_summary(result):
    lines = ["# 逐入口信源验收", "", f"参考时钟：{result['reference_time']}",
             f"入口：{result['returned_entries']}/{result['planned_entries']}；状态：{result['counts']}；请求：{result['request_count']}",
             "", result["boundary"], "", "| 入口 | 状态 | 原因/归因 | 解析/回读 |", "|---|---|---|---|"]
    for row in result["entries"]:
        diagnosis = row.get("diagnosis") or diagnose_entry(row)
        lines.append(f"| {row['key']} | {row['status']} | {row.get('reason', '')} / {diagnosis['category']} | {row['parsed']}/{row['persisted']} |")
    return "\n".join(lines) + "\n"


def diagnose_entry(row):
    """Conservative attribution from observations, never from exception bodies.

    A 403 is not proof of an invalid key; missing local credentials say nothing
    about GitHub Secrets. Keep upstream failure and unexecuted rows separate.
    """
    status, reason = row.get("status"), row.get("reason", "")
    events = row.get("http", [])
    last = events[-1] if events else {}
    code = last.get("status")
    key = row.get("credential_name", "")
    if status == "disabled":
        category, action = "declared_disabled", "配置关闭，未测试；不能计作通过。"
    elif status == "not_configured":
        category, action = "credential_missing_here", f"当前检查进程未注入 {key or '对应凭据'}；在 Runner 验证，不能据此判断远端 Key 无效。"
    elif status == "not_executed":
        category, action = "not_executed", "因预算或同主机限流未执行，保留入口；等待冷却后补验。"
    elif status == "not_tested":
        category, action = "separate_acceptance_needed", "此样本未验收；核对授权或响应大小边界后单独检查。"
    elif status == "passed":
        category, action = "bounded_sample_passed", "协议/解析样本通过；不证明全分页、长期可用性或本期没有更新。"
    elif row.get("input_url_checked") is False or row.get("blocked_destination"):
        category, action = "unsafe_or_invalid_input_url", "修正配置地址或不安全重定向；不放宽私网/凭据 URL 限制。"
    elif last.get("access_challenge"):
        category, action = "provider_access_challenge", "提供方访问挑战，不是 API Key 问题；核对公开官方等价入口，不绕过挑战。"
    elif code == 401 or reason == "http_401":
        category, action = "authentication_rejected", f"核对 {key or '接口鉴权配置'} 的有效性及注入；只读检查失败不自动轮换密钥。"
    elif code == 429 or reason == "http_429" or last.get("rate_limit_exhausted"):
        category, action = "rate_limit_or_quota", "遵守 Retry-After/配额重置，检查账户额度与请求节奏；不要反复重打或直接判 Key 无效。"
    elif code in {402, 432, 433} or reason in {"http_402", "http_432", "http_433"}:
        category, action = "billing_or_entitlement", "核对账户额度/套餐权限；不自动增加付费额度。"
    elif code == 403 or reason == "http_403":
        category = "authenticated_access_denied" if row.get("credential_required") else "public_access_denied"
        action = "核对授权范围/套餐和云端 IP 访问策略；403 本身不足以证明 Key 无效。" if row.get("credential_required") else "公开页面被拒绝；不需要轮换 API Key，核对 Runner 及官方等价入口。"
    elif code == 404 or reason == "http_404":
        category, action = "endpoint_missing_or_moved", "核对官方目录迁移；带鉴权接口也可能隐藏无权访问资源，不能一概判地址不存在。"
    elif isinstance(code, int) and code >= 500:
        category, action = "provider_server_error", "提供方返回服务端错误；有界退避后补验，不按 API Key 失效处理。"
    elif code in {400, 422}:
        category, action = "request_parameters_rejected", "检查官方接口参数和版本契约；不靠轮换 Key 修复错误请求。"
    elif code == 202:
        category, action = "unexpected_http_202", "返回异步/挑战响应而非研究正文；不能按成功空结果交付。"
    elif reason == "timeout" or any(event.get("error_type") in {"ReadTimeout", "ConnectTimeout", "PoolTimeout", "WriteTimeout"} for event in events):
        category, action = "network_or_response_timeout", "检查连接、响应耗时和入口预算；超时不等于鉴权失败。"
    elif any(event.get("error_type") in {"ConnectError", "ProxyError", "RemoteProtocolError"} for event in events):
        category, action = "network_transport_failure", "检查 DNS、代理、TLS 与远端连接；仅凭该错误不能区分本地网络和提供方故障。"
    elif (row.get("coverage") or {}).get("unresolved_citations"):
        category, action = "bibliography_primary_links_unresolved", "部分书目没有可核验的一手标识/URL；已识别原点保留，不把未解析书目记为覆盖完成。"
    elif (row.get("coverage") or {}).get("status") == "parse_zero_links":
        category, action = "index_structure_or_wrong_catalog", "200 但没有研究条目：检查主页/迁移页/动态数据或解析器，不能当成没有发布。"
    elif (row.get("coverage") or {}).get("detail_failures"):
        category, action = "detail_or_index_data_contract", "索引可读，但详情或注册数据失败；逐请求定位正文/文件格式，不放宽质量闸门。"
    else:
        category, action = "response_or_persistence_contract", "核对响应结构、身份、日期与入库回读；保留失败记录及健康同伴。"
    return {"category": category, "action": action, "credential_name": key,
            "boundary": "观测归因；不猜测提供方内部故障，不证明长期稳定性。"}
