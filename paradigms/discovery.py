"""论文/研究博客优先的范式发现层。"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import config
from sources.arxiv_source import ArxivSource
from sources.base import RawProject
from sources.hf_papers_source import HuggingFacePapersSource
from sources.openalex_source import OpenAlexSource
from sources.openreview_source import OpenReviewSource
from sources.research_feed_source import ResearchFeedSource

from .models import EvidenceType, TechnicalEvidence

logger = logging.getLogger(__name__)


@dataclass
class DiscoveryBatch:
    origins: list[TechnicalEvidence]
    supporting: list[TechnicalEvidence]


class ParadigmDiscovery:
    """发现源只允许论文和技术博客；热榜/社区只作为支持证据。"""

    def __init__(self):
        lookback = config.SOURCING_LOOKBACK_DAYS
        self.arxiv = ArxivSource(
            max_results=config.PARADIGM_MAX_DISCOVERY_ITEMS,
            lookback_days=lookback,
        )
        self.hf = HuggingFacePapersSource(lookback_days=lookback)
        self.evidence_sources = [
            OpenAlexSource(lookback_days=lookback),
            OpenReviewSource(lookback_days=lookback),
            ResearchFeedSource(lookback_days=lookback),
        ]

    async def run(self) -> DiscoveryBatch:
        arxiv_raw, hf_raw, *native_batches = await asyncio.gather(
            self.arxiv.safe_fetch(),
            self.hf.safe_fetch(),
            *(source.safe_fetch() for source in self.evidence_sources),
        )

        origins = [_raw_to_origin(item) for item in arxiv_raw]
        supporting = [_hf_to_support(item) for item in hf_raw]
        # HF Daily Papers 可能覆盖 arXiv 查询词之外的重要论文，因此也作为候补原始论文。
        origins.extend(_raw_to_origin(item) for item in hf_raw)
        for batch in native_batches:
            origins.extend(batch)

        origins = _merge_origins(origins)
        origins = sorted(
            origins,
            key=lambda item: item.published_at or "",
            reverse=True,
        )[: config.PARADIGM_MAX_DISCOVERY_ITEMS]
        logger.info(
            "范式发现完成：%s 条原始论文/博客，%s 条平台支持信号",
            len(origins),
            len(supporting),
        )
        return DiscoveryBatch(origins=origins, supporting=supporting)


def _raw_to_origin(item: RawProject) -> TechnicalEvidence:
    arxiv_id = _normalize_arxiv_id(item.extra.get("arxiv_id", "") or item.url)
    identifiers = {"arxiv": arxiv_id} if arxiv_id else {}
    metrics = {
        key: item.extra.get(key, 0)
        for key in ("upvotes", "github_stars", "num_comments")
        if item.extra.get(key) is not None
    }
    return TechnicalEvidence(
        source=item.source,
        evidence_type=EvidenceType.PRIMARY_PAPER,
        title=item.name,
        url=item.url,
        summary=item.readme_summary or item.description,
        published_at=item.created_at,
        authors=item.extra.get("all_authors", []) or ([item.author] if item.author else []),
        organization=item.extra.get("organization", ""),
        metrics=metrics,
        identifiers=identifiers,
        keywords=item.topics,
        raw=item.extra,
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


def _normalize_arxiv_id(value: str) -> str:
    match = re.search(r"(\d{4}\.\d{4,5})(?:v\d+)?", str(value))
    return match.group(1) if match else ""


def _merge_origins(items: list[TechnicalEvidence]) -> list[TechnicalEvidence]:
    by_key: dict[str, TechnicalEvidence] = {}
    for item in items:
        key = item.identifiers.get("doi") or item.identifiers.get("arxiv") or item.fingerprint
        if key not in by_key:
            by_key[key] = item
            continue
        current = by_key[key]
        if len(item.summary) > len(current.summary):
            current.summary = item.summary
            current.url = item.url or current.url
        current.metrics.update(item.metrics)
        current.identifiers.update(item.identifiers)
        current.raw.update(item.raw)
        current.authors = list(dict.fromkeys(current.authors + item.authors))
        current.keywords = list(dict.fromkeys(current.keywords + item.keywords))
        if item.source not in current.raw.setdefault("also_seen_on", []):
            current.raw["also_seen_on"].append(item.source)
    return list(by_key.values())
