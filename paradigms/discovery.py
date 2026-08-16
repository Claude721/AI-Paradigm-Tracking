"""论文/研究博客优先的范式发现层。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import config
from sources.arxiv_source import ArxivSource
from sources.base import RawProject
from sources.curated_intelligence_source import (
    CuratedKOLSource,
    CuratedKOLXSource,
    HighSignalForumSource,
)
from sources.hf_papers_source import HuggingFacePapersSource
from sources.follow_builders_source import FollowBuildersSource
from sources.openalex_source import OpenAlexSource
from sources.openreview_source import OpenReviewSource
from sources.official_repository_release_source import (
    OfficialRepositoryReleaseSource,
)
from sources.priority_research_source import PriorityResearchPageSource
from sources.research_feed_source import ResearchFeedSource

from .landscape import classify_frontier_domains, coverage_report
from .models import (
    ORIGIN_EVIDENCE_TYPES,
    EvidenceType,
    TechnicalEvidence,
    nonnegative_number,
)

logger = logging.getLogger(__name__)


@dataclass
class DiscoveryBatch:
    origins: list[TechnicalEvidence]
    supporting: list[TechnicalEvidence]
    source_counts: dict[str, int]
    coverage: dict


class ParadigmDiscovery:
    """发现论文、正式技术页、原创思想文章与原生代码机制。

    普通领域召回只扫本次任务窗口；较长回补窗口仅用于正式报告、
    重点研究者、官方研究入口和高信号思想源。这是召回车道的语义分工，
    不是 Top-K；所有原点后续仍走同一技术与势能闸门。
    """

    def __init__(
        self,
        lookback_days: int | None = None,
        *,
        broad_lookback_days: int | None = None,
        high_signal_lookback_days: int | None = None,
    ):
        # lookback_days 保留为旧调用方式；新编排器显式传入两层窗口。
        legacy = lookback_days or config.SOURCING_LOOKBACK_DAYS
        self.broad_lookback_days = max(broad_lookback_days or legacy, 1)
        self.high_signal_lookback_days = max(
            high_signal_lookback_days or legacy,
            self.broad_lookback_days,
        )
        # 保持兼容：旧属性表示本轮最长发现窗口。
        self.lookback_days = self.high_signal_lookback_days
        self.source_timeout_seconds = (
            config.PARADIGM_DISCOVERY_SOURCE_TIMEOUT_SECONDS
        )
        self.arxiv = ArxivSource(
            max_results=config.PARADIGM_DISCOVERY_SAFETY_LIMIT or None,
            lookback_days=self.broad_lookback_days,
            high_signal_lookback_days=self.high_signal_lookback_days,
            seed_arxiv_ids=config.PARADIGM_SEED_ARXIV_IDS,
        )
        self.hf = HuggingFacePapersSource(
            lookback_days=self.broad_lookback_days
        )
        self.follow_builders = FollowBuildersSource()
        self.priority_pages = PriorityResearchPageSource(
            lookback_days=self.high_signal_lookback_days
        )
        self.official_repositories = OfficialRepositoryReleaseSource(
            lookback_days=self.high_signal_lookback_days
        )
        self.openalex = OpenAlexSource(
            lookback_days=self.broad_lookback_days
        )
        self.openreview = OpenReviewSource(
            lookback_days=self.broad_lookback_days
        )
        self.kol_feeds = CuratedKOLSource(
            lookback_days=self.high_signal_lookback_days
        )
        self.high_signal_forums = HighSignalForumSource(
            lookback_days=self.high_signal_lookback_days
        )
        self.kol_x = CuratedKOLXSource(
            lookback_days=self.broad_lookback_days
        )
        self.evidence_sources = [
            self.openalex,
            self.openreview,
            ResearchFeedSource(lookback_days=self.high_signal_lookback_days),
            self.priority_pages,
            self.kol_feeds,
            self.high_signal_forums,
            self.kol_x,
        ]

    async def run(self) -> DiscoveryBatch:
        sources = [
            self.arxiv,
            self.hf,
            self.follow_builders,
            *self.evidence_sources,
            self.official_repositories,
        ]
        fetched = await asyncio.gather(
            *(self._bounded_fetch(source) for source in sources),
        )
        batches = [result for result, _ in fetched]
        source_health = {
            str(health["source"]): health for _, health in fetched
        }
        arxiv_raw, hf_raw, follow_raw, *native_and_repository_batches = batches
        native_batches = native_and_repository_batches[:-1]
        repository_batch = native_and_repository_batches[-1]

        origins = [_raw_to_origin(item) for item in arxiv_raw]
        supporting = [_hf_to_support(item) for item in hf_raw]
        # HF Daily Papers 可能覆盖 arXiv 查询词之外的重要论文，因此也作为候补原始论文。
        origins.extend(_raw_to_origin(item) for item in hf_raw)
        origins.extend(
            _follow_builder_blog_to_origin(item)
            for item in follow_raw
            if item.source == "follow-builders-blog"
            and _within_lookback(item, self.high_signal_lookback_days)
        )
        supporting.extend(
            _follow_builder_to_support(item)
            for item in follow_raw
            if item.source != "follow-builders-blog"
            and _within_lookback(item, self.broad_lookback_days)
        )
        for batch in native_batches:
            origins.extend(
                item for item in batch if item.evidence_type in ORIGIN_EVIDENCE_TYPES
            )
            supporting.extend(
                item for item in batch if item.evidence_type not in ORIGIN_EVIDENCE_TYPES
            )
        origins.extend(
            item
            for item in repository_batch
            if item.evidence_type in ORIGIN_EVIDENCE_TYPES
        )
        supporting.extend(
            item
            for item in repository_batch
            if item.evidence_type not in ORIGIN_EVIDENCE_TYPES
        )

        source_counts = {
            "arxiv": len(arxiv_raw),
            "huggingface_daily_papers": len(hf_raw),
            "follow_builders": len(follow_raw),
        }
        source_counts.update(
            {
                source.source_name: len(batch)
                for source, batch in zip(self.evidence_sources, native_batches)
            }
        )
        source_counts[self.official_repositories.source_name] = len(
            repository_batch
        )

        origins = _merge_origins(origins)
        for item in origins:
            if not item.raw.get("frontier_domains"):
                item.raw["frontier_domains"] = classify_frontier_domains(
                    item.title,
                    item.summary,
                    " ".join(item.keywords),
                )
        origins = sorted(
            origins,
            key=lambda item: (
                int(nonnegative_number(item.raw.get("origin_priority", 0))),
                item.published_at or "",
            ),
            reverse=True,
        )
        # ``PARADIGM_DISCOVERY_SAFETY_LIMIT`` can stop further arXiv transport
        # lanes, and those unexecuted lanes are explicitly marked incomplete.
        # It must not slice the already fetched cross-source result here: doing
        # so would discard known origins before the durable pending checkpoint
        # and repeatedly starve the same tail on every retry.
        logger.info(
            "范式发现完成：%s 条一手机制原点，%s 条平台/解读支持信号",
            len(origins),
            len(supporting),
        )
        logger.info(
            "发现窗口与信源拆分：普通车道=%s天，高信号回补=%s天；%s",
            self.broad_lookback_days,
            self.high_signal_lookback_days,
            ", ".join(
                f"{name}={count}"
                for name, count in sorted(source_counts.items())
            ),
        )
        coverage = coverage_report(
            origins,
            executed_groups=self.arxiv.executed_query_groups,
            failed_groups=self.arxiv.failed_query_groups,
        )
        coverage["recall_lanes"] = self.arxiv.recall_coverage()
        coverage["source_health"] = source_health
        coverage["official_pages"] = self.priority_pages.coverage()
        coverage["official_repositories"] = self.official_repositories.coverage()
        coverage["curated_kol_sources"] = self.kol_feeds.coverage()
        coverage["high_signal_forums"] = self.high_signal_forums.coverage()
        coverage["curated_kol_x"] = self.kol_x.coverage()
        coverage["academic_indexes"] = {
            "arxiv": self.arxiv.coverage(),
            "openalex": self.openalex.coverage(),
            "openreview": self.openreview.coverage(),
        }
        coverage["recall_windows"] = {
            "ordinary_origins_days": self.broad_lookback_days,
            "high_signal_backfill_days": self.high_signal_lookback_days,
            "source_windows": {
                "arxiv_landscape": self.broad_lookback_days,
                "arxiv_technical_documents": self.high_signal_lookback_days,
                "arxiv_priority_researchers": self.high_signal_lookback_days,
                "arxiv_explicit_seeds": "exact_unbounded",
                "huggingface_daily_papers": self.broad_lookback_days,
                "openalex": self.broad_lookback_days,
                "openreview_submission_created_at": self.broad_lookback_days,
                "official_research_pages": self.high_signal_lookback_days,
                "official_repository_release_events": self.high_signal_lookback_days,
                "research_feeds": self.high_signal_lookback_days,
                "curated_kol_feeds": self.high_signal_lookback_days,
                "lesswrong_alignment_forum": self.high_signal_lookback_days,
                "curated_kol_x_recent_search": min(self.broad_lookback_days, 7),
            },
        }
        return DiscoveryBatch(
            origins=origins,
            supporting=supporting,
            source_counts=source_counts,
            coverage=coverage,
        )

    async def _bounded_fetch(self, source) -> tuple[list, dict[str, object]]:
        """Isolate a stalled discovery source without disguising it as zero hits."""

        name = str(getattr(source, "source_name", type(source).__name__))
        started = time.monotonic()
        try:
            results = await asyncio.wait_for(
                source.safe_fetch(),
                timeout=self.source_timeout_seconds,
            )
            if not isinstance(results, list):
                raise TypeError(
                    f"{name} 返回 {type(results).__name__}，预期 list"
                )
            status = str(getattr(source, "fetch_status", "completed"))
            error = str(getattr(source, "fetch_error", ""))
        except TimeoutError:
            status = "timed_out"
            error = "TimeoutError"
            results = []
            logger.error(
                "[%s] 发现源超过 %ss 墙上时间，已取消；本轮覆盖不完整",
                name,
                self.source_timeout_seconds,
            )
        except Exception as exc:
            status = "query_failed"
            error = type(exc).__name__
            results = []
            logger.exception(
                "[%s] 发现源越过内部保护抛出异常；本轮覆盖不完整",
                name,
            )
        return results, {
            "source": name,
            "status": status,
            "error_type": error,
            "results": len(results),
            "elapsed_seconds": round(max(time.monotonic() - started, 0.0), 3),
            "timeout_seconds": self.source_timeout_seconds,
        }


def _raw_to_origin(item: RawProject) -> TechnicalEvidence:
    arxiv_id = _normalize_arxiv_id(item.extra.get("arxiv_id", "") or item.url)
    identifiers = {"arxiv": arxiv_id} if arxiv_id else {}
    canonical_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else item.url
    raw = dict(item.extra)
    if canonical_url.rstrip("/") != item.url.rstrip("/"):
        raw["discovery_url"] = item.url
    metrics = {
        key: item.extra.get(key, 0)
        for key in ("upvotes", "github_stars", "num_comments")
        if item.extra.get(key) is not None
    }
    return TechnicalEvidence(
        source=item.source,
        evidence_type=EvidenceType.PRIMARY_PAPER,
        title=item.name,
        url=canonical_url,
        summary=item.readme_summary or item.description,
        published_at=item.created_at,
        authors=item.extra.get("all_authors", []) or ([item.author] if item.author else []),
        organization=item.extra.get("organization", ""),
        metrics=metrics,
        identifiers=identifiers,
        keywords=item.topics,
        raw=raw,
    )


def _hf_to_support(item: RawProject) -> TechnicalEvidence:
    return TechnicalEvidence(
        source="huggingface-papers",
        evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
        title=item.name,
        url=item.url,
        summary="Hugging Face Daily Papers 的收藏、评论与代码仓库信号",
        published_at=item.created_at,
        authors=item.extra.get("all_authors", []),
        organization=item.extra.get("organization", ""),
        metrics={
            "upvotes": item.extra.get("upvotes", 0),
            "comments": item.extra.get("num_comments", 0),
            "stars": item.extra.get("github_stars", 0),
        },
        identifiers={"arxiv": _normalize_arxiv_id(item.extra.get("arxiv_id", ""))},
        raw={"github_repo": item.extra.get("github_repo", "")},
    )


def _follow_builder_blog_to_origin(item: RawProject) -> TechnicalEvidence:
    return TechnicalEvidence(
        source=item.source,
        evidence_type=EvidenceType.TECHNICAL_BLOG,
        title=item.name,
        url=item.url,
        summary=item.readme_summary or item.description,
        published_at=item.created_at,
        authors=[item.author] if item.author else [],
        organization=str(item.extra.get("blog_name", "")),
        raw=item.extra,
    )


def _follow_builder_to_support(item: RawProject) -> TechnicalEvidence:
    metrics = {
        key: item.extra.get(key, 0)
        for key in ("likes", "retweets", "replies")
        if item.extra.get(key) is not None
    }
    raw = {**item.extra, "relationship": "kol_or_podcast_candidate"}
    if item.source == "follow-builders-x":
        handle = ""
        match = re.search(r"@([A-Za-z0-9_]+)", item.author)
        if match:
            handle = match.group(1)
        raw.update(
            {
                "social_author_name": item.author.split(" (@", 1)[0].strip(),
                "social_bio": str(item.extra.get("builder_bio", "")),
                "social_profile_url": f"https://x.com/{handle}" if handle else "",
            }
        )
    return TechnicalEvidence(
        source=item.source,
        evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
        title=item.name,
        url=item.url,
        summary=item.readme_summary or item.description,
        published_at=item.created_at,
        authors=[item.author] if item.author else [],
        metrics=metrics,
        raw=raw,
    )


def _normalize_arxiv_id(value: str) -> str:
    match = re.search(r"(\d{4}\.\d{4,5})(?:v\d+)?", str(value))
    return match.group(1) if match else ""


def _within_lookback(item: RawProject, lookback_days: int) -> bool:
    if not item.created_at:
        return True
    try:
        published = datetime.fromisoformat(
            item.created_at.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except ValueError:
        return True
    return published >= datetime.now(timezone.utc) - timedelta(
        days=max(lookback_days, 1)
    )


def _merge_origins(items: list[TechnicalEvidence]) -> list[TechnicalEvidence]:
    by_key: dict[str, TechnicalEvidence] = {}
    title_keys: dict[str, str] = {}
    for item in items:
        key = item.identifiers.get("doi") or item.identifiers.get("arxiv") or item.fingerprint
        title_key = _safe_title_dedupe_key(item.title)
        if title_key and title_key in title_keys:
            key = title_keys[title_key]
        if key not in by_key:
            by_key[key] = item
            if title_key:
                title_keys[title_key] = key
            continue
        current = by_key[key]
        if len(item.summary) > len(current.summary):
            current.summary = item.summary
        if _primary_url_rank(item.url) > _primary_url_rank(current.url):
            current.url = item.url
        current.metrics.update(item.metrics)
        current.identifiers.update(item.identifiers)
        _merge_origin_raw(current.raw, item.raw)
        if item.organization and not current.organization:
            current.organization = item.organization
        current.authors = list(dict.fromkeys(current.authors + item.authors))
        current.keywords = list(dict.fromkeys(current.keywords + item.keywords))
        if item.source not in current.raw.setdefault("also_seen_on", []):
            current.raw["also_seen_on"].append(item.source)
    return list(by_key.values())


def _safe_title_dedupe_key(title: str) -> str:
    """Use title-only merging only when the title is sufficiently specific."""

    parts = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", title.casefold())
    key = "".join(parts)
    if len(key) < 20 or len(parts) < 3:
        return ""
    if key in {
        "technicalreport",
        "researchreport",
        "systemcard",
        "modelcard",
    }:
        return ""
    return key


def _primary_url_rank(url: str) -> int:
    """Prefer direct first-party/academic records over discovery aggregators."""

    parsed = urlparse(str(url))
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    path = parsed.path.casefold()
    if host == "arxiv.org" and path.startswith(("/abs/", "/pdf/", "/html/")):
        return 5
    if host == "doi.org" or host == "openreview.net":
        return 5
    if host in {"huggingface.co", "openalex.org", "api.openalex.org"}:
        return 1
    return 4 if parsed.scheme in {"http", "https"} and host else 0


def _merge_origin_raw(current: dict, incoming: dict) -> None:
    """Merge provenance without allowing a weaker index to downgrade identity."""

    priority = max(
        int(nonnegative_number(current.get("origin_priority", 0))),
        int(nonnegative_number(incoming.get("origin_priority", 0))),
    )
    tier_rank = {"unknown": 0, "verified": 1, "established": 2}
    current_tier = str(current.get("publisher_tier", "unknown"))
    incoming_tier = str(incoming.get("publisher_tier", "unknown"))
    best_tier = max(
        (current_tier, incoming_tier),
        key=lambda value: tier_rank.get(value, 0),
    )
    kind_rank = {
        "": 0,
        "research_paper": 1,
        "official_research": 2,
        "official_model_release": 3,
        "technical_report": 4,
    }
    current_kind = str(current.get("origin_kind", ""))
    incoming_kind = str(incoming.get("origin_kind", ""))
    best_kind_metadata = (
        current
        if kind_rank.get(current_kind, 0) >= kind_rank.get(incoming_kind, 0)
        else incoming
    )
    best_kind = max(
        (current_kind, incoming_kind),
        key=lambda value: kind_rank.get(value, 0),
    )
    publisher_evidence = str(current.get("publisher_evidence", ""))
    if tier_rank.get(incoming_tier, 0) > tier_rank.get(
        current_tier, 0
    ) or (
        not publisher_evidence
        and tier_rank.get(incoming_tier, 0) == tier_rank.get(best_tier, 0)
    ):
        publisher_evidence = str(incoming.get("publisher_evidence", ""))
    frontier_domains = list(
        dict.fromkeys(
            [
                *(current.get("frontier_domains") or []),
                *(incoming.get("frontier_domains") or []),
            ]
        )
    )
    explicit_seed = bool(current.get("explicit_seed")) or bool(
        incoming.get("explicit_seed")
    )
    priority_researcher = bool(
        current.get("priority_researcher_match")
    ) or bool(incoming.get("priority_researcher_match"))
    selection_signals = list(
        dict.fromkeys(
            [
                *(current.get("selection_signals") or []),
                *(incoming.get("selection_signals") or []),
            ]
        )
    )

    current.update(incoming)
    current["origin_priority"] = priority
    if best_tier:
        current["publisher_tier"] = best_tier
    if publisher_evidence:
        current["publisher_evidence"] = publisher_evidence
    if best_kind:
        current["origin_kind"] = best_kind
        for key in (
            "origin_classification_reason",
            "document_format",
            "system_layer_count",
        ):
            value = best_kind_metadata.get(key)
            if value not in {None, ""}:
                current[key] = value
    current["frontier_domains"] = frontier_domains
    current["explicit_seed"] = explicit_seed
    current["priority_researcher_match"] = priority_researcher
    if selection_signals:
        current["selection_signals"] = selection_signals
