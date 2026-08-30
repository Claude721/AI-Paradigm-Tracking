"""RSS-first high-signal forums and manually verified AI thinker sources.

These sources may seed a mechanism hypothesis. They do not bypass the technical
rubric, identity verification, independent uptake, or report quality gates.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urlencode

import httpx

import config
from paradigms.models import EvidenceType, TechnicalEvidence, nonnegative_number
from research_watchlist import KOL_SOURCES, KOL_SOURCE_VERSION

logger = logging.getLogger(__name__)


_AI_ANCHORS = (
    "artificial intelligence", "machine learning", "language model", "llm",
    "neural network", "transformer", "agent", "reinforcement learning",
    "reasoning", "world model", "robot", "embodied", "multimodal", "vla",
    "diffusion", "interpretability", "alignment", "superintelligence",
    "recursive self-improvement", "self-improving", " ai ", "agi", "rsi",
    "人工智能", "机器学习", "语言模型", "大模型", "智能体", "强化学习",
    "推理", "世界模型", "机器人", "具身", "多模态", "扩散模型",
)
_MECHANISM_MARKERS = (
    "architecture", "algorithm", "training", "learning", "objective", "loss",
    "inference", "search", "memory", "representation", "mechanism", "method",
    "experiment", "benchmark", "scaling", "optimization", "feedback",
    "recursive", "self-improving", "paradigm", "protocol", "compute",
    "架构", "算法", "训练", "学习", "目标函数", "损失", "推理", "搜索",
    "记忆", "表征", "机制", "方法", "实验", "评测", "扩展", "优化",
    "反馈", "递归", "自我改进", "范式", "计算",
)
_TITLE_TECHNICAL_MARKERS = (
    "architecture", "algorithm", "training", "learning", "inference",
    "reasoning", "agent", "model", "neural", "transformer", "memory",
    "representation", "interpretability", "alignment", "benchmark",
    "evaluation", "world model", "reinforcement", "diffusion", "robot",
    "self-improvement", "scaling", "架构", "算法", "训练", "学习", "推理",
    "智能体", "模型", "神经网络", "记忆", "表征", "可解释", "对齐",
    "评测", "世界模型", "强化学习", "扩散", "机器人", "自我改进",
)
_NON_TECHNICAL_TITLE_MARKERS = (
    "weekly links", "weeknotes", "newsletter", "new release of", "changelog",
    "timeline of", "my students", "executive could control", "regulation",
    "regulatory", "election", "politics", "policy proposal", "governance of",
    "job opening", "hiring", "conference notes", "book review", "podcast",
    "本周链接", "周报", "招聘", "政策", "监管", "选举", "读书笔记",
)


class CuratedKOLSource:
    """Read verified personal feeds; do not guess missing feed endpoints."""

    source_name = "curated-kol-feeds"

    def __init__(self, lookback_days: int = 30) -> None:
        self.lookback_days = max(lookback_days, 1)
        self._coverage: dict[str, object] = {}

    async def safe_fetch(self) -> list[TechnicalEvidence]:
        if not config.KOL_SOURCE_ENABLED:
            self.fetch_status = "disabled"
            self.fetch_error = ""
            self._coverage = {"status": "disabled", "catalog_version": KOL_SOURCE_VERSION}
            return []
        try:
            results = await self.fetch()
            configured = int(self._coverage.get("configured_feeds", 0) or 0)
            completed = int(self._coverage.get("completed_feeds", 0) or 0)
            failed = list(self._coverage.get("failed_feeds", []) or [])
            if configured and completed == 0:
                self.fetch_status = "query_failed"
                self.fetch_error = "AllFeedsFailed"
            elif configured and completed / configured < 0.6:
                self.fetch_status = "partial"
                self.fetch_error = "MaterialFeedCoverageLoss"
            elif failed:
                self.fetch_status = "completed_with_warnings"
                self.fetch_error = "MinorFeedFailures"
            else:
                self.fetch_status = "completed"
                self.fetch_error = ""
            self._coverage["status"] = self.fetch_status
            return results
        except Exception as exc:
            self.fetch_status = "query_failed"
            self.fetch_error = type(exc).__name__
            logger.exception("[curated-kol-feeds] 获取失败")
            return []

    async def fetch(self) -> list[TechnicalEvidence]:
        records = [
            (item, str(url))
            for item in KOL_SOURCES
            for url in item.get("feed_urls", ())
        ]
        records.extend(
            (
                {
                    "id": f"custom-{index}",
                    "name": "Custom KOL feed",
                    "aliases": (),
                    "focus": "用户自定义信源，身份与原点资格未内置核验",
                    "homepage": url,
                    "origin_policy": "secondary_only",
                },
                url,
            )
            for index, url in enumerate(config.KOL_CUSTOM_FEED_URLS)
        )
        async with httpx.AsyncClient(
            timeout=25,
            follow_redirects=True,
            headers={"User-Agent": "AI-Paradigm-Radar/3.3 (public RSS reader)"},
        ) as client:
            responses = await asyncio.gather(
                *(client.get(url) for _, url in records),
                return_exceptions=True,
            )

        items: list[TechnicalEvidence] = []
        failures: list[str] = []
        completed = 0
        filtered = 0
        for (record, feed_url), response in zip(records, responses):
            if isinstance(response, Exception):
                failures.append(f"{record['id']}:{type(response).__name__}")
                continue
            try:
                response.raise_for_status()
                parsed, rejected = _parse_feed(
                    response.text,
                    feed_url=feed_url,
                    lookback_days=self.lookback_days,
                    record=record,
                    forum=None,
                )
                items.extend(parsed)
                filtered += rejected
                completed += 1
            except Exception as exc:
                failures.append(f"{record['id']}:{type(exc).__name__}")
                logger.warning("KOL Feed 解析失败 %s: %s", feed_url, exc)
        deduplicated = list({item.fingerprint: item for item in items}.values())
        self._coverage = {
            "catalog_version": KOL_SOURCE_VERSION,
            "directory_entries": len(KOL_SOURCES),
            "configured_feeds": len(records),
            "completed_feeds": completed,
            "failed_feeds": failures,
            "relevance_filtered_items": filtered,
            "returned_items": len(deduplicated),
            "x_directory_entries": sum(bool(item.get("x_handle")) for item in KOL_SOURCES),
        }
        return deduplicated

    def coverage(self) -> dict[str, object]:
        return dict(self._coverage)


class HighSignalForumSource:
    """Read LessWrong/Alignment Forum official public feeds."""

    source_name = "high-signal-forums"

    def __init__(self, lookback_days: int = 30) -> None:
        self.lookback_days = max(lookback_days, 1)
        self._coverage: dict[str, object] = {}

    def _feeds(self) -> list[dict[str, str | int]]:
        feeds: list[dict[str, str | int]] = []
        if config.LESSWRONG_SOURCE_ENABLED:
            threshold = config.LESSWRONG_KARMA_THRESHOLD
            feeds.extend(
                [
                    {
                        "forum": "lesswrong",
                        "view": "frontpage",
                        "selection": f"frontpage_karma_gte_{threshold}",
                        "karma_lower_bound": threshold,
                        "url": "https://www.lesswrong.com/feed.xml?"
                        + urlencode({"view": "frontpage", "karmaThreshold": threshold}),
                    },
                    {
                        "forum": "lesswrong",
                        "view": "curated",
                        "selection": "curated",
                        "karma_lower_bound": 0,
                        "url": "https://www.lesswrong.com/feed.xml?view=curated",
                    },
                ]
            )
        if config.ALIGNMENT_FORUM_SOURCE_ENABLED:
            threshold = config.LESSWRONG_KARMA_THRESHOLD
            feeds.append(
                {
                    "forum": "alignment-forum",
                    "view": "frontpage",
                    "selection": f"frontpage_karma_gte_{threshold}",
                    "karma_lower_bound": threshold,
                    "url": "https://www.alignmentforum.org/feed.xml?"
                    + urlencode({"view": "frontpage", "karmaThreshold": threshold}),
                }
            )
        return feeds

    async def safe_fetch(self) -> list[TechnicalEvidence]:
        feeds = self._feeds()
        if not feeds:
            self.fetch_status = "disabled"
            self.fetch_error = ""
            self._coverage = {"status": "disabled", "configured_feeds": 0}
            return []
        try:
            results = await self.fetch()
            completed = int(self._coverage.get("completed_feeds", 0) or 0)
            failed = list(self._coverage.get("failed_feeds", []) or [])
            if completed == 0:
                self.fetch_status = "query_failed"
                self.fetch_error = "AllFeedsFailed"
            elif completed / len(feeds) < (2 / 3):
                self.fetch_status = "partial"
                self.fetch_error = "MaterialForumCoverageLoss"
            elif failed:
                self.fetch_status = "completed_with_warnings"
                self.fetch_error = "MinorForumFeedFailures"
            else:
                self.fetch_status = "completed"
                self.fetch_error = ""
            self._coverage["status"] = self.fetch_status
            return results
        except Exception as exc:
            self.fetch_status = "query_failed"
            self.fetch_error = type(exc).__name__
            logger.exception("[high-signal-forums] 获取失败")
            return []

    async def fetch(self) -> list[TechnicalEvidence]:
        feeds = self._feeds()
        async with httpx.AsyncClient(
            timeout=25,
            follow_redirects=True,
            headers={"User-Agent": "AI-Paradigm-Radar/3.3 (public RSS reader)"},
        ) as client:
            responses = await asyncio.gather(
                *(client.get(str(item["url"])) for item in feeds),
                return_exceptions=True,
            )
        items: list[TechnicalEvidence] = []
        failures: list[str] = []
        completed = 0
        filtered = 0
        for feed, response in zip(feeds, responses):
            if isinstance(response, Exception):
                failures.append(f"{feed['forum']}:{feed['view']}:{type(response).__name__}")
                continue
            try:
                response.raise_for_status()
                parsed, rejected = _parse_feed(
                    response.text,
                    feed_url=str(feed["url"]),
                    lookback_days=self.lookback_days,
                    record=None,
                    forum=feed,
                )
                items.extend(parsed)
                filtered += rejected
                completed += 1
            except Exception as exc:
                failures.append(f"{feed['forum']}:{feed['view']}:{type(exc).__name__}")
                logger.warning("论坛 Feed 解析失败 %s: %s", feed["url"], exc)
        deduplicated = list({item.fingerprint: item for item in items}.values())
        self._coverage = {
            "configured_feeds": len(feeds),
            "completed_feeds": completed,
            "failed_feeds": failures,
            "relevance_filtered_items": filtered,
            "returned_items": len(deduplicated),
            "metric_boundary": (
                "RSS 不提供精确 karma；frontpage 只记录配置的 karma 下限，"
                "curated 只记录编辑精选状态"
            ),
        }
        return deduplicated

    def coverage(self) -> dict[str, object]:
        return dict(self._coverage)


class CuratedKOLXSource:
    """Optionally read recent posts from known handles via the official X API."""

    source_name = "curated-kol-x"

    def __init__(self, lookback_days: int = 7) -> None:
        self.lookback_days = max(lookback_days, 1)
        self._coverage: dict[str, object] = {}

    async def safe_fetch(self) -> list[TechnicalEvidence]:
        if not config.KOL_X_SOURCE_ENABLED or not config.TWITTER_BEARER_TOKEN:
            self.fetch_status = "not_configured"
            self.fetch_error = ""
            self._coverage = {
                "status": "not_configured",
                "configured_handles": len(config.TWITTER_WATCH_ACCOUNTS),
                "reason": "KOL_X_SOURCE_ENABLED=false or missing TWITTER_BEARER_TOKEN",
            }
            return []
        try:
            results = await self.fetch()
            failed = list(self._coverage.get("failed_batches", []) or [])
            completed = int(self._coverage.get("completed_batches", 0) or 0)
            configured = int(self._coverage.get("configured_batches", 0) or 0)
            if configured and completed == 0:
                self.fetch_status = "query_failed"
                self.fetch_error = "AllAccountBatchesFailed"
            elif configured and completed / configured < 0.75:
                self.fetch_status = "partial"
                self.fetch_error = "MaterialAccountCoverageLoss"
            elif failed:
                self.fetch_status = "completed_with_warnings"
                self.fetch_error = "MinorAccountBatchFailures"
            else:
                self.fetch_status = "completed"
                self.fetch_error = ""
            self._coverage["status"] = self.fetch_status
            return results
        except Exception as exc:
            self.fetch_status = "query_failed"
            self.fetch_error = type(exc).__name__
            logger.exception("[curated-kol-x] 获取失败")
            return []

    async def fetch(self) -> list[TechnicalEvidence]:
        records_by_handle = {
            str(record.get("x_handle", "")).casefold(): record
            for record in KOL_SOURCES
            if str(record.get("x_handle", "")).strip()
        }
        for value in config.TWITTER_WATCH_ACCOUNTS:
            records_by_handle.setdefault(
                value.casefold(),
                {
                    "id": f"custom-x-{value.casefold()}",
                    "name": value,
                    "homepage": f"https://x.com/{value}",
                    "origin_policy": "secondary_only",
                },
            )
        handles = list(dict.fromkeys(config.TWITTER_WATCH_ACCOUNTS))
        batches = [handles[index : index + 8] for index in range(0, len(handles), 8)]
        headers = {"Authorization": f"Bearer {config.TWITTER_BEARER_TOKEN}"}
        start_time = (
            datetime.now(timezone.utc) - timedelta(days=min(self.lookback_days, 7))
        ).isoformat(timespec="seconds").replace("+00:00", "Z")

        async def request_batch(client: httpx.AsyncClient, batch: list[str]):
            query = "(" + " OR ".join(f"from:{handle}" for handle in batch) + ")"
            query += " -is:retweet -is:reply"
            response = await client.get(
                "https://api.x.com/2/tweets/search/recent",
                headers=headers,
                params={
                    "query": query,
                    "start_time": start_time,
                    "max_results": 100,
                    "tweet.fields": "created_at,author_id,public_metrics",
                    "expansions": "author_id",
                    "user.fields": "name,username,description,public_metrics,verified",
                },
            )
            response.raise_for_status()
            return batch, response.json()

        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            payloads = await asyncio.gather(
                *(request_batch(client, batch) for batch in batches),
                return_exceptions=True,
            )

        items: list[TechnicalEvidence] = []
        failures: list[str] = []
        filtered = 0
        completed = 0
        for batch, payload in zip(batches, payloads):
            if isinstance(payload, Exception):
                failures.append(f"{','.join(batch)}:{type(payload).__name__}")
                continue
            completed += 1
            _, data = payload
            users = {
                str(user.get("id", "")): user
                for user in (data.get("includes") or {}).get("users", [])
            }
            for post in data.get("data", []) or []:
                user = users.get(str(post.get("author_id", "")), {})
                username = str(user.get("username", ""))
                record = records_by_handle.get(username.casefold())
                text = str(post.get("text", "")).strip()
                if not record or not _looks_like_short_concept(text):
                    filtered += 1
                    continue
                evidence_type = (
                    EvidenceType.CONCEPT_ESSAY
                    if record.get("origin_policy") == "concept_origin"
                    else EvidenceType.SECONDARY_INTERPRETATION
                )
                post_id = str(post.get("id", ""))
                post_url = f"https://x.com/{username}/status/{post_id}"
                public_metrics = post.get("public_metrics") or {}
                homepage = str(record.get("homepage", ""))
                author = str(user.get("name") or record.get("name", username))
                origin = evidence_type == EvidenceType.CONCEPT_ESSAY
                items.append(
                    TechnicalEvidence(
                        source=self.source_name,
                        evidence_type=evidence_type,
                        title=text[:180],
                        url=post_url,
                        summary=text,
                        published_at=str(post.get("created_at") or ""),
                        authors=[author],
                        organization=str(record.get("name", "")),
                        metrics={
                            "likes": int(nonnegative_number(public_metrics.get("like_count", 0))),
                            "retweets": int(nonnegative_number(public_metrics.get("retweet_count", 0))),
                            "replies": int(nonnegative_number(public_metrics.get("reply_count", 0))),
                        },
                        identifiers={"x": post_id},
                        raw={
                            "origin_kind": "kol_short_concept" if origin else "kol_commentary",
                            "origin_priority": 2 if origin else 0,
                            "publisher_tier": "verified" if origin else "unknown",
                            "publisher_evidence": (
                                f"手工核验 KOL 目录 {KOL_SOURCE_VERSION}：{homepage}"
                                if origin
                                else ""
                            ),
                            "commentator_tier": "" if origin else "verified",
                            "canonical_source_url": post_url,
                            "source_catalog_id": record.get("id"),
                            "source_catalog_version": KOL_SOURCE_VERSION,
                            "priority_researcher_match": origin,
                            "relationship": (
                                "author_original_post" if origin else "independent_commentary"
                            ),
                            "independence": "author" if origin else "independent",
                            "author_roles": {author: "短文作者/机制提出者"},
                            "author_public_profiles": {
                                author: {
                                    "homepage": homepage,
                                    "x": f"https://x.com/{username}",
                                }
                            },
                            "social_platform": "x",
                        },
                    )
                )
        deduplicated = list({item.fingerprint: item for item in items}.values())
        self._coverage = {
            "configured_handles": len(handles),
            "configured_batches": len(batches),
            "completed_batches": completed,
            "failed_batches": failures,
            "relevance_filtered_posts": filtered,
            "returned_items": len(deduplicated),
            "window_days": min(self.lookback_days, 7),
            "coverage_boundary": "X recent search only; requires an eligible official API plan",
        }
        return deduplicated

    def coverage(self) -> dict[str, object]:
        return dict(self._coverage)


def _parse_feed(
    xml_text: str,
    *,
    feed_url: str,
    lookback_days: int,
    record: dict | None,
    forum: dict | None,
) -> tuple[list[TechnicalEvidence], int]:
    root = ET.fromstring(xml_text)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(lookback_days, 1))
    nodes = root.findall(".//item") or root.findall(
        "{http://www.w3.org/2005/Atom}entry"
    )
    results: list[TechnicalEvidence] = []
    filtered = 0
    for node in nodes:
        title = _text(node, "title")
        link = _link(node)
        body = (
            _text(node, "encoded")
            or _text(node, "content")
            or _text(node, "description")
            or _text(node, "summary")
        )
        summary = _plain_text(body)[:16_000]
        published_text = (
            _text(node, "pubDate")
            or _text(node, "published")
            or _text(node, "updated")
        )
        published = _parse_date(published_text)
        if published and published < cutoff:
            continue
        if not title or not link or not _looks_like_frontier_ai(title, summary):
            filtered += 1
            continue
        author = _text(node, "creator") or _text(node, "author")
        if record is not None:
            evidence_type = (
                EvidenceType.CONCEPT_ESSAY
                if record.get("origin_policy") == "concept_origin"
                else EvidenceType.SECONDARY_INTERPRETATION
            )
            name = str(record.get("name", author))
            author = author or name
            homepage = str(record.get("homepage", ""))
            x_handle = str(record.get("x_handle", ""))
            profiles = {"homepage": homepage}
            if x_handle:
                profiles["x"] = f"https://x.com/{x_handle}"
            raw = {
                "origin_kind": "kol_concept_essay",
                "origin_priority": 2,
                "publisher_tier": (
                    "verified"
                    if evidence_type == EvidenceType.CONCEPT_ESSAY
                    else "unknown"
                ),
                "publisher_evidence": (
                    f"手工核验的 KOL 目录 {KOL_SOURCE_VERSION}：{name} / {homepage}"
                    if evidence_type == EvidenceType.CONCEPT_ESSAY
                    else ""
                ),
                "commentator_tier": (
                    "verified"
                    if evidence_type == EvidenceType.SECONDARY_INTERPRETATION
                    else ""
                ),
                "commentator_evidence": (
                    f"手工核验的 KOL 目录 {KOL_SOURCE_VERSION}：{name} / {homepage}"
                    if evidence_type == EvidenceType.SECONDARY_INTERPRETATION
                    else ""
                ),
                "canonical_source_url": link,
                "source_catalog_id": record.get("id"),
                "source_catalog_version": KOL_SOURCE_VERSION,
                "origin_policy": record.get("origin_policy"),
                "priority_researcher_match": (
                    evidence_type == EvidenceType.CONCEPT_ESSAY
                ),
                "author_roles": {author: "文章作者/机制提出者"},
                "author_public_profiles": {author: profiles},
                "author_profile_urls": {author: homepage},
                "relationship": (
                    "author_original_essay"
                    if evidence_type == EvidenceType.CONCEPT_ESSAY
                    else "independent_commentary"
                ),
                "independence": (
                    "author" if evidence_type == EvidenceType.CONCEPT_ESSAY else "independent"
                ),
            }
            organization = name
            source = "curated-kol-blog"
            metrics: dict[str, int] = {}
            identifiers: dict[str, str] = {}
        else:
            assert forum is not None
            evidence_type = EvidenceType.CONCEPT_ESSAY
            lower_bound = int(forum.get("karma_lower_bound", 0) or 0)
            metrics = {"karma_lower_bound": lower_bound} if lower_bound else {}
            known_author = _kol_record_for_author(author)
            author_profiles: dict[str, dict[str, str]] = {}
            if known_author and author:
                profiles = {"homepage": str(known_author.get("homepage", ""))}
                if known_author.get("x_handle"):
                    profiles["x"] = f"https://x.com/{known_author['x_handle']}"
                author_profiles[author] = profiles
            raw = {
                "origin_kind": "community_concept_essay",
                "origin_priority": 1,
                "publisher_tier": "verified" if known_author else "unknown",
                "publisher_evidence": (
                    f"论坛作者与手工 KOL 目录 {KOL_SOURCE_VERSION} 完整姓名匹配："
                    f"{known_author['name']} / {known_author['homepage']}"
                    if known_author
                    else ""
                ),
                "priority_researcher_match": bool(known_author),
                "canonical_source_url": link,
                "forum": forum["forum"],
                "forum_view": forum["view"],
                "selection_signal": forum["selection"],
                "selection_signals": [
                    f"{forum['forum']}:{forum['selection']}"
                ],
                "metric_boundary": "selection lower bound, not exact post karma",
                "relationship": "author_original_essay",
                "independence": "author",
                "author_roles": {author: "论坛文章作者/机制提出者"} if author else {},
                "author_public_profiles": author_profiles,
            }
            organization = str(forum["forum"])
            source = str(forum["forum"])
            post_id_match = re.search(r"/posts/([^/?#]+)", link)
            identifiers = (
                {"forum_post": post_id_match.group(1)}
                if post_id_match
                else {}
            )
        results.append(
            TechnicalEvidence(
                source=source,
                evidence_type=evidence_type,
                title=title,
                url=link,
                summary=summary,
                published_at=published.isoformat() if published else published_text,
                authors=[author] if author else [],
                organization=organization,
                metrics=metrics,
                identifiers=identifiers,
                raw=raw,
            )
        )
    return results, filtered


def _looks_like_frontier_ai(title: str, summary: str) -> bool:
    title_value = f" {title} ".casefold()
    if any(term in title_value for term in _NON_TECHNICAL_TITLE_MARKERS):
        return False
    body_value = f" {summary[:5000]} ".casefold()
    combined = title_value + body_value
    anchor_hits = sum(term in combined for term in _AI_ANCHORS)
    mechanism_hits = sum(term in combined for term in _MECHANISM_MARKERS)
    title_is_technical = any(
        term in title_value for term in _TITLE_TECHNICAL_MARKERS
    )
    # A technical title still needs a mechanism-bearing body. An unfamiliar
    # coined term may pass without a known title keyword only when the article
    # repeatedly grounds it in both AI objects and executable mechanisms.
    if title_is_technical:
        return anchor_hits >= 1 and mechanism_hits >= 2
    return anchor_hits >= 2 and mechanism_hits >= 5


def _looks_like_short_concept(text: str) -> bool:
    value = re.sub(r"https?://\S+", " ", text or "").strip()
    if len(value) < 80:
        return False
    lowered = f" {value} ".casefold()
    if any(term in lowered for term in _NON_TECHNICAL_TITLE_MARKERS):
        return False
    return any(term in lowered for term in _AI_ANCHORS) and any(
        term in lowered for term in _MECHANISM_MARKERS
    )


def _kol_record_for_author(author: str) -> dict | None:
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", author.casefold())
    if not normalized:
        return None
    for record in KOL_SOURCES:
        aliases = {
            re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(value).casefold())
            for value in record.get("aliases", ())
        }
        if normalized in aliases:
            return record
    return None


def _plain_text(value: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", value or "", flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _text(node: ET.Element, local_name: str) -> str:
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1] != local_name:
            continue
        return "".join(child.itertext()).strip()
    return ""


def _link(node: ET.Element) -> str:
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1] != "link":
            continue
        rel = str(child.get("rel", "alternate"))
        if rel not in {"", "alternate"}:
            continue
        if child.get("href"):
            return str(child.get("href", "")).strip()
        if child.text:
            return child.text.strip()
    return ""


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
