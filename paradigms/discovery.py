"""论文/研究博客优先的范式发现层。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from runtime_clock import research_now
from urllib.parse import urlparse

import config
from research_watchlist import KOL_SOURCES, default_kol_x_accounts, source_record
from sources.arxiv_source import ArxivSource, TECHNICAL_REPORT_QUERY
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
from sources.official_detail_queue import official_detail_policy
from sources.research_feed_source import ResearchFeedSource

from .landscape import (
    arxiv_priority_author_query_plan, arxiv_query_plan,
    classify_frontier_domains, coverage_report,
)
from .publication import nonresearch_page_reason
from .models import (
    ORIGIN_EVIDENCE_TYPES,
    EvidenceType,
    TechnicalEvidence,
    nonnegative_number,
    _evidence_datetime,
    technical_evidence_from_dict,
    safe_public_contact_target,
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
        stage_cache=None,
        bounded: bool = False,
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
        self.bounded = bounded
        self.source_timeout_seconds = (
            config.PARADIGM_DISCOVERY_SOURCE_TIMEOUT_SECONDS
        )
        self.arxiv = ArxivSource(
            max_results=config.PARADIGM_DISCOVERY_SAFETY_LIMIT or None,
            lookback_days=self.broad_lookback_days,
            high_signal_lookback_days=self.high_signal_lookback_days,
            seed_arxiv_ids=config.PARADIGM_SEED_ARXIV_IDS,
            query_result_limit=config.PARADIGM_SOURCE_QUERY_LIMIT if bounded else None,
        )
        self.hf = HuggingFacePapersSource(
            lookback_days=self.broad_lookback_days
        )
        self.follow_builders = FollowBuildersSource()
        self.priority_pages = PriorityResearchPageSource(
            lookback_days=self.high_signal_lookback_days, stage_cache=stage_cache,
            per_page=(min(config.PRIORITY_RESEARCH_LINK_SAFETY_LIMIT or config.PARADIGM_OFFICIAL_DETAIL_LIMIT,
                          config.PARADIGM_OFFICIAL_DETAIL_LIMIT) if bounded else None),
            catalog_page_limit=config.PARADIGM_SOURCE_PAGE_LIMIT if bounded else None,
        )
        self.official_repositories = OfficialRepositoryReleaseSource(
            lookback_days=self.high_signal_lookback_days
        )
        self.openalex = OpenAlexSource(
            lookback_days=self.broad_lookback_days,
            max_pages=config.PARADIGM_SOURCE_PAGE_LIMIT if bounded else None,
        )
        self.openreview = OpenReviewSource(
            lookback_days=self.broad_lookback_days, stage_cache=stage_cache,
        )
        self.kol_feeds = CuratedKOLSource(
            lookback_days=self.high_signal_lookback_days, stage_cache=stage_cache,
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

    def _sources(self):
        return [
            self.arxiv,
            self.hf,
            self.follow_builders,
            *self.evidence_sources,
            self.official_repositories,
        ]

    def _plan_signatures(self, *, include_adapters=True):
        # Credentials and runtime/concurrency counters are deliberately absent.
        settings = {
            "research-blog": [config.RESEARCH_FEED_URLS],
            "curated-kol-feeds": [config.KOL_SOURCE_ENABLED, config.KOL_CUSTOM_FEED_URLS,
                [(item["id"], item.get("feed_urls", ()), item.get("origin_policy", "")) for item in KOL_SOURCES]],
            "high-signal-forums": [config.LESSWRONG_SOURCE_ENABLED, config.ALIGNMENT_FORUM_SOURCE_ENABLED, config.LESSWRONG_KARMA_THRESHOLD],
            "curated-kol-x": [config.KOL_X_SOURCE_ENABLED, config.TWITTER_WATCH_ACCOUNTS, default_kol_x_accounts()],
            "arxiv": [config.PARADIGM_PRIORITY_AUTHOR_SWEEP_ENABLED, arxiv_query_plan(),
                arxiv_priority_author_query_plan(config.PRIORITY_RESEARCHERS), TECHNICAL_REPORT_QUERY],
            "follow-builders": [config.FOLLOW_BUILDERS_ENABLED],
        }
        plans = {}
        for source in self._sources():
            value = {field: getattr(source, field) for field in ("lookback_days", "high_signal_lookback_days", "seed_arxiv_ids", "venues", "searches", "pages", "organizations", "feed_base_url") if hasattr(source, field)}
            if include_adapters:
                value["origin_input_policy"] = hashlib.sha256(Path(__file__).with_name("publication.py").read_bytes()).hexdigest()
                if isinstance(source, ResearchFeedSource):
                    root = Path(__file__).parent.parent / "sources"
                    value["html_body_policy"] = hashlib.sha256(b"".join(
                        (root / name).read_bytes() for name in ("priority_research_source.py", "research_feed_source.py"))).hexdigest()
            if isinstance(source, PriorityResearchPageSource) and include_adapters:
                # An adapter/catalog correction must invalidate its discovery
                # receipt; unchanged sources and durable task scope survive.
                value["catalog_adapters"] = [source_record(url) for url in source.pages]
                value["bibliography_resolution"] = config.PRIORITY_BIBLIOGRAPHY_RESOLUTION_ENABLED
                value["detail_parser_policy"] = official_detail_policy()
            if isinstance(source, HighSignalForumSource):
                value["feeds"] = source._feeds()
            if isinstance(source, CuratedKOLSource) and include_adapters:
                from sources.curated_intelligence_source import curated_feed_policy
                value["public_archive_adapters"] = [item.get("substack_public_archive") for item in KOL_SOURCES]
                value["feed_parser_policy"] = curated_feed_policy()
            value["settings"] = settings.get(source.source_name, [])
            plans[source.source_name] = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return plans

    async def run(self, *, source_names: set[str] | None = None) -> DiscoveryBatch:
        sources = self._sources()
        known = {source.source_name for source in sources}
        if source_names is not None and source_names - known:
            raise ValueError("不能续跑未知发现入口：" + ", ".join(sorted(source_names - known)))
        async def fetch(source):
            if source_names is not None and source.source_name not in source_names:
                return [], {"source": source.source_name, "status": "cached", "results": 0}
            return await self._bounded_fetch(source)
        fetched = await asyncio.gather(*(fetch(source) for source in sources))
        batches = [result for result, _ in fetched]
        source_health = {
            str(health["source"]): health for _, health in fetched
        }
        arxiv_raw, hf_raw, follow_raw, *native_and_repository_batches = batches
        native_batches = native_and_repository_batches[:-1]
        repository_batch = native_and_repository_batches[-1]

        origins = _convert_records(arxiv_raw, lambda item: [_raw_to_origin(item)], source_health[self.arxiv.source_name])
        # HF Daily Papers 可能覆盖 arXiv 查询词之外的重要论文，因此也作为候补原始论文。
        hf_evidence = _convert_records(hf_raw, lambda item: [_raw_to_origin(item), _hf_to_support(item)],
                                       source_health[self.hf.source_name])
        def convert_follow(item):
            blog = item.source == "follow-builders-blog"
            days = self.high_signal_lookback_days if blog else self.broad_lookback_days
            if not _within_lookback(item, days):
                return []
            return [_follow_builder_blog_to_origin(item) if blog else _follow_builder_to_support(item)]
        additional = [*hf_evidence, *_convert_records(follow_raw, convert_follow,
                                                       source_health[self.follow_builders.source_name])]
        for source, records in zip([*self.evidence_sources, self.official_repositories],
                                   [*native_batches, repository_batch]):
            additional.extend(_convert_records(records, lambda item: [item], source_health[source.source_name]))
        origins.extend(item for item in additional if item.evidence_type in ORIGIN_EVIDENCE_TYPES)
        supporting = [item for item in additional if item.evidence_type not in ORIGIN_EVIDENCE_TYPES]

        source_counts = {
            "arxiv": source_health[self.arxiv.source_name]["results"],
            "huggingface_daily_papers": source_health[self.hf.source_name]["results"],
            "follow_builders": source_health[self.follow_builders.source_name]["results"],
        }
        source_counts.update(
            {
                source.source_name: source_health[source.source_name]["results"]
                for source, batch in zip(self.evidence_sources, native_batches)
            }
        )
        source_counts[self.official_repositories.source_name] = source_health[self.official_repositories.source_name]["results"]

        # A resumed source can return today's feed even at an older business
        # clock. Do not let post-cutoff events expand the frozen campaign.
        origins = [item for item in origins if not (date := _evidence_datetime(item.published_at)) or date <= research_now() or item.raw.get("explicit_seed")]
        supporting = [item for item in supporting if not (date := _evidence_datetime(item.published_at)) or date <= research_now()]
        origin_input_exclusions, usable_origins = [], []
        for item in origins:
            reason = nonresearch_page_reason(item.url, item.summary, title=item.title)
            if reason:
                origin_input_exclusions.append({"fingerprint": item.fingerprint, "url": item.url,
                    "source": item.source, "reason": reason, "retryable": reason == "access_challenge"})
            else:
                usable_origins.append(item)
        origins = _merge_origins(usable_origins)
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
        coverage["origin_input_exclusions"] = origin_input_exclusions
        if self.bounded:
            coverage["acquisition_scope"] = {"kind": "bounded_recall",
                "arxiv_results_per_query": config.PARADIGM_SOURCE_QUERY_LIMIT,
                "openalex_pages_per_query": config.PARADIGM_SOURCE_PAGE_LIMIT,
                "official_details_per_entry": self.priority_pages.per_page,
                "exhaustive": False}
        coverage["source_plan_signatures"] = self._plan_signatures()
        coverage["source_scope_signatures"] = self._plan_signatures(include_adapters=False)
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

    def retry_sources(self, previous: DiscoveryBatch) -> set[str]:
        """Include obsolete positive receipts, not just recorded failures."""
        from paradigms.completion import discovery_retry_sources
        selected = discovery_retry_sources(previous.coverage)
        prior_plans = previous.coverage.get("source_plan_signatures") or {}
        if prior_plans:
            current_plans = self._plan_signatures()
            selected.update(name for name, value in (previous.coverage.get("source_health") or {}).items()
                            if value.get("status") not in {"not_configured", "disabled"}
                            and prior_plans.get(name) != current_plans.get(name))
        return selected

    async def retry(self, previous: DiscoveryBatch) -> DiscoveryBatch:
        """Retry failed capabilities at the original clock; retain healthy lanes.

        Partial receipts never become completed simply because the next process
        no longer has their source configured. A source set change is explicit
        incompatibility, not evidence that the original search was performed.
        """
        selected = self.retry_sources(previous)
        prior_plans = previous.coverage.get("source_plan_signatures") or {}
        if prior_plans:
            current_plans = self._plan_signatures()
            current_scopes = self._plan_signatures(include_adapters=False)
            prior_scopes = previous.coverage.get("source_scope_signatures") or {}
            for name in selected:
                if prior_plans.get(name) == current_plans.get(name):
                    continue
                same_scope = prior_scopes.get(name) == current_scopes.get(name) if name in prior_scopes else False
                if name not in prior_scopes and name == "priority-research-page":
                    # Legacy snapshots contain no separated scope hash. Only
                    # a complete explicit page manifest and frozen window can
                    # prove an adapter-only change. Counts alone never suffice.
                    pages = (previous.coverage.get("official_pages") or {}).get("pages")
                    windows = (previous.coverage.get("recall_windows") or {}).get("source_windows") or {}
                    same_scope = (isinstance(pages, dict) and set(pages) == set(self.priority_pages.pages)
                                  and windows.get("official_research_pages") == self.priority_pages.lookback_days)
                if not same_scope:
                    raise RuntimeError("未完成入口的召回范围发生变化；不能把换配置当作原批次覆盖完成")
        if not selected:
            return previous
        fresh = await self.run(source_names=selected)
        coverage = copy.deepcopy(previous.coverage)
        for name in selected:
            for field in ("source_plan_signatures", "source_scope_signatures"):
                signatures = fresh.coverage.get(field) or {}
                if name in signatures:
                    coverage.setdefault(field, {})[name] = signatures[name]
        for name in selected:
            current = fresh.coverage["source_health"][name]
            prior = (previous.coverage.get("source_health") or {}).get(name, {})
            if current.get("status") in {"not_configured", "disabled"} and prior.get("status") not in {"not_configured", "disabled"}:
                current = {**current, "status": "configuration_changed", "error": "original planned capability removed"}
            coverage.setdefault("source_health", {})[name] = current
        sections = {
            "arxiv": ("domains", "recall_lanes", "query_failures", "covered_domains", "total_domains", "landscape_version"),
            "priority-research-page": ("official_pages",),
            "official-repository-release": ("official_repositories",),
            "curated-kol-feeds": ("curated_kol_sources",),
            "high-signal-forums": ("high_signal_forums",),
            "curated-kol-x": ("curated_kol_x",),
        }
        for name in selected:
            for section in sections.get(name, ()):
                coverage[section] = fresh.coverage.get(section, {})
            if name in {"arxiv", "openalex", "openreview"}:
                coverage.setdefault("academic_indexes", {})[name] = fresh.coverage["academic_indexes"][name]
        aliases = {"huggingface-papers": "huggingface_daily_papers", "follow-builders": "follow_builders"}
        counts = dict(previous.source_counts)
        for name in selected:
            alias = aliases.get(name, name)
            counts[alias] = fresh.source_counts.get(alias, 0)
        return DiscoveryBatch(
            origins=_merge_origins([*previous.origins, *fresh.origins]),
            supporting=list({item.fingerprint: item for item in [*previous.supporting, *fresh.supporting]}.values()),
            source_counts=counts, coverage=coverage,
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
            # Only the official adapter exposes typed results committed to its
            # durable queue before cancellation. Keep them, never mark closed.
            results = list(getattr(source, "partial_results", [])) if isinstance(source, PriorityResearchPageSource) else []
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


def _convert_records(records, converter, health):
    """Contain adapter-record faults before merge, sorting or snapshot writes.

    A healthy HTTP/list response is not proof that each nested object is valid.
    Reject only that object (not a technical verdict), retaining healthy peers
    and transport failures without logging external payloads or credentials.
    """
    result, rejected = [], 0
    for record in records:
        try:
            values = [technical_evidence_from_dict(item.to_dict()) for item in converter(record)]
            for item in values:
                if (any(not isinstance(value, str) for value in [*item.authors, *item.keywords])
                        or any(not isinstance(key, str) or not isinstance(value, str)
                               for key, value in item.identifiers.items())):
                    raise ValueError("发现记录的署名/关键词/标识符结构无效")
                for field in ("frontier_domains", "selection_signals", "also_seen_on"):
                    value = item.raw.get(field)
                    if value is not None and (not isinstance(value, list)
                            or any(not isinstance(label, str) for label in value)):
                        raise ValueError("发现记录的合并元数据结构无效")
                    if field in item.raw and value is None:
                        item.raw[field] = []
                for field in ("origin_classification_reason", "document_format", "system_layer_count"):
                    if isinstance(item.raw.get(field), (dict, list)):
                        raise ValueError("发现记录的分类元数据结构无效")
                if item.url and not safe_public_contact_target("source", item.url):
                    raise ValueError("发现记录 URL 无效")
                item.fingerprint
                json.dumps(item.to_dict(), allow_nan=False)
        except (AttributeError, TypeError, ValueError, OverflowError, RecursionError):
            rejected += 1
            continue
        result.extend(values)
    if rejected:
        health.update(raw_results=len(records), results=len(records) - rejected, rejected_records=rejected)
        if health["status"] in {"completed", "completed_after_retry", "completed_with_warnings"}:
            health["status"] = "completed_with_warnings" if result else "query_failed"
        health["error_type"] = health.get("error_type") or "RecordContractError"
        logger.warning("[%s] 隔离 %s 条格式错误记录；其余材料继续，不形成技术否定", health["source"], rejected)
    return result


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
        published_at=str(item.created_at or ""),
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
        published_at=str(item.extra.get("hf_featured_at", item.created_at) or ""),
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
        published_at=str(item.created_at or ""),
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
        published_at=str(item.created_at or ""),
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
    return published >= research_now() - timedelta(
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
