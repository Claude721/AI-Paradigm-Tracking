"""为范式补齐学术、复现、社区和人物证据。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from difflib import SequenceMatcher

from sources.paradigm_evidence_source import CommunityEvidenceClient
from sources.semantic_scholar_source import SemanticScholarClient
from sources.researcher_profile_source import ResearcherProfileClient
from sources.arxiv_document_source import ArxivDocumentClient
from runtime_clock import research_now

from .models import (
    EvidenceType,
    ParadigmCandidate,
    TechnicalEvidence,
    material_metric_signature,
    nonnegative_number,
    scrub_ephemeral_evidence,
    is_verified_substantive_discussion,
)
from .researcher_identity import merge_researcher_profiles

logger = logging.getLogger(__name__)


class EvidenceEnricher:
    def __init__(self, concurrency: int = 4):
        self.concurrency = max(concurrency, 1)
        self.semantic_scholar = SemanticScholarClient()
        self.community = CommunityEvidenceClient()
        self.researchers = ResearcherProfileClient()
        self.documents = ArxivDocumentClient()

    async def run(
        self,
        candidates: list[ParadigmCandidate],
        supporting: list[TechnicalEvidence],
    ) -> list[ParadigmCandidate]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def enrich_one(candidate: ParadigmCandidate) -> ParadigmCandidate:
            async with semaphore:
                self._attach_support(candidate, supporting)
                await self._external_enrichment(candidate)
                return candidate

        return await asyncio.gather(*(enrich_one(candidate) for candidate in candidates))

    async def hydrate_priority_origins(
        self, evidence: list[TechnicalEvidence]
    ) -> dict[str, int]:
        """在初筛前只补水高势能原点，避免摘要信息不足误杀关键节点。"""
        targets = [
            item
            for item in evidence
            if nonnegative_number(item.raw.get("origin_priority", 0)) >= 2
            or item.raw.get("explicit_seed")
        ]
        semaphore = asyncio.Semaphore(self.concurrency)

        async def hydrate_one(item: TechnicalEvidence) -> bool:
            async with semaphore:
                try:
                    coverage = await self.documents.hydrate(item)
                except Exception as exc:
                    logger.warning(
                        "高优先级原点正文补水异常 [%s]: %s",
                        item.title,
                        exc,
                    )
                    return False
                return bool(item.raw.get("document_excerpt")) and not any(
                    "失败" in value for value in coverage.values()
                )

        results = await asyncio.gather(*(hydrate_one(item) for item in targets))
        return {
            "priority_origin_targets": len(targets),
            "priority_origin_hydrated": sum(results),
            "priority_origin_hydration_failed": len(targets) - sum(results),
        }

    async def refresh(
        self,
        candidates: list[ParadigmCandidate],
        supporting: list[TechnicalEvidence],
    ) -> list[ParadigmCandidate]:
        """只保留本周出现新讨论/实现的历史候选，不重复跑学术身份接口。"""
        semaphore = asyncio.Semaphore(self.concurrency)

        async def refresh_one(candidate: ParadigmCandidate) -> ParadigmCandidate | None:
            async with semaphore:
                previous = {
                    item.fingerprint: copy.deepcopy(item)
                    for item in candidate.evidence
                }
                self._attach_support(candidate, supporting)
                try:
                    candidate.evidence.extend(await self.community.search(candidate))
                    candidate.community_coverage = {
                        **self.community.coverage(),
                        **candidate.community_coverage,
                    }
                except Exception as exc:
                    logger.warning("历史范式社区刷新失败 [%s]: %s", candidate.name, exc)
                    # CommunityEvidenceClient already isolates ordinary GitHub/
                    # HN/Tavily/Reddit/X failures and records their coverage.
                    # Reaching this boundary means the whole refresh operation
                    # failed structurally; returning None would falsely mean
                    # “checked successfully, no new uptake”.
                    raise RuntimeError(
                        f"社区刷新结构性失败: {type(exc).__name__}"
                    ) from exc
                candidate.evidence = _dedupe_evidence(
                    candidate.evidence, route_key=candidate.key
                )
                changed = False
                for item in candidate.evidence:
                    prior = previous.get(item.fingerprint)
                    if prior is None:
                        # A title-only discussion or same-name repository is
                        # a lead, not yet a reason to rewrite the route.
                        if _new_evidence_requires_synthesis(item):
                            changed = True
                        continue
                    if item.raw.get("indexed_discovery_only") and not is_verified_substantive_discussion(prior, route_key=candidate.key):
                        continue
                    if all(
                        nonnegative_number(item.metrics.get(key, 0))
                        == nonnegative_number(prior.metrics.get(key, 0))
                        for key in _UPTAKE_METRIC_KEYS
                    ):
                        continue
                    baseline = prior.raw.get("metric_baseline")
                    if not isinstance(baseline, dict):
                        baseline = prior.metrics
                    delta = _positive_metric_delta(item.metrics, baseline)
                    item.raw["metric_baseline"] = {
                        key: nonnegative_number(value)
                        for key, value in baseline.items()
                        if key in _UPTAKE_METRIC_KEYS
                    }
                    if _material_metric_growth(
                        item, prior, delta, route_key=candidate.key
                    ):
                        item.raw["metric_delta"] = delta
                        item.raw["metric_delta_observed_at"] = (
                            research_now().isoformat()
                        )
                        item.raw["metric_baseline"] = {
                            key: nonnegative_number(value)
                            for key, value in item.metrics.items()
                            if key in _UPTAKE_METRIC_KEYS
                        }
                        changed = True
                if not changed:
                    return None
                _attach_social_profiles(candidate)
                return candidate

        refreshed = await asyncio.gather(*(refresh_one(item) for item in candidates))
        return [item for item in refreshed if item is not None]

    async def _external_enrichment(self, candidate: ParadigmCandidate) -> None:
        lead = candidate.evidence[0]
        document_coverage = await self.documents.hydrate(lead)
        candidate.community_coverage = {
            **candidate.community_coverage,
            **document_coverage,
        }
        for repository in lead.raw.get("github_repositories", []) or []:
            candidate.evidence.append(
                _official_repository_evidence(lead, str(repository))
            )
        results = await asyncio.gather(
            self.semantic_scholar.enrich_paper(lead),
            self.community.search(candidate),
            return_exceptions=True,
        )
        scholarly, community = results
        if not isinstance(scholarly, Exception):
            citation_evidence, profiles = scholarly
            if citation_evidence and int(
                citation_evidence.metrics.get("citations", 0) or 0
            ) > 0:
                candidate.evidence.append(citation_evidence)
            candidate.researchers = _merge_profiles(candidate.researchers, profiles)
        else:
            logger.warning("Semantic Scholar 增强失败 [%s]: %s", candidate.name, scholarly)
        if not isinstance(community, Exception):
            candidate.evidence.extend(community)
            candidate.community_coverage = {
                **self.community.coverage(),
                **candidate.community_coverage,
            }
        else:
            logger.warning("社区证据增强失败 [%s]: %s", candidate.name, community)
            candidate.community_coverage = {
                **self.community.coverage(),
                **candidate.community_coverage,
                "community_runtime": (
                    f"社区证据总入口发生结构性异常：{type(community).__name__}"
                ),
            }
        candidate.evidence = _dedupe_evidence(
            candidate.evidence, route_key=candidate.key
        )
        # Semantic Scholar 只有在显式启用且配置获批 Key 时才调用。无论其是否
        # 启用，都用当前论文作者建立人物种子，并通过 OpenAlex/ORCID 补齐身份。
        candidate.researchers = await self.researchers.enrich(
            lead, candidate.researchers
        )
        _attach_social_profiles(candidate)

    @staticmethod
    def finalize(candidates: list[ParadigmCandidate]) -> list[ParadigmCandidate]:
        """分析完成后不持久化社区用户正文，只保留可复核链接和聚合指标。"""
        for candidate in candidates:
            for evidence in candidate.evidence:
                scrub_ephemeral_evidence(evidence)
        return candidates

    @staticmethod
    def _attach_support(
        candidate: ParadigmCandidate, supporting: list[TechnicalEvidence]
    ) -> None:
        candidate_ids = {
            str(value).lower()
            for item in candidate.evidence
            for value in item.identifiers.values()
            if value
        }
        titles = [item.title.lower() for item in candidate.evidence]
        for item in supporting:
            support_ids = {
                str(value).lower() for value in item.identifiers.values() if value
            }
            title_match = max(
                (SequenceMatcher(None, item.title.lower(), title).ratio() for title in titles),
                default=0.0,
            )
            if candidate_ids & support_ids or title_match >= 0.9:
                candidate.evidence.append(copy.deepcopy(item))
                repository = str(item.raw.get("github_repo", "")).strip()
                if repository:
                    candidate.evidence.append(
                        _official_repository_evidence(item, repository)
                    )
                continue
            if _lexically_related(candidate, item):
                candidate.evidence.append(copy.deepcopy(item))

    @staticmethod
    def supporting_signature(
        candidate: ParadigmCandidate, supporting: list[TechnicalEvidence]
    ) -> str:
        """Hash only support that this route would receive, without retaining text.

        A global batch hash would invalidate every deep checkpoint whenever an
        unrelated feed item appears. This uses the same conservative attachment
        rule as enrichment, then hashes a bounded factual projection. Temporary
        community bodies/authors never enter the durable signature.
        """
        if not supporting:
            return hashlib.sha256(b"[]").hexdigest()
        probe = copy.copy(candidate)
        probe.evidence = list(candidate.evidence)
        original_count = len(probe.evidence)
        EvidenceEnricher._attach_support(probe, supporting)
        relevant = []
        for item in probe.evidence[original_count:]:
            temporary = bool(item.raw.get("ephemeral_content"))
            relevant.append({
                "fingerprint": item.fingerprint,
                "title": "" if temporary else item.title,
                "url": item.url,
                "summary": "" if temporary else item.summary,
                "published_at": item.published_at,
                "authors": [] if temporary else item.authors,
                "metrics": material_metric_signature(item.metrics),
                "relationship": str(item.raw.get("relationship", "")),
                "independence": str(item.raw.get("independence", "")),
                "indexed_discovery_only": bool(item.raw.get("indexed_discovery_only")),
            })
        # Discovery sources can return the same supporting item in different
        # orders (or duplicate it). Neither should trigger another LLM run.
        records = {
            json.dumps(value, ensure_ascii=False, sort_keys=True)
            for value in relevant
        }
        encoded = json.dumps(sorted(records), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _merge_profiles(existing, new):
    return merge_researcher_profiles(existing, new)


def _lexically_related(
    candidate: ParadigmCandidate, evidence: TechnicalEvidence
) -> bool:
    """只为后续 LLM 审计提供小规模候选，不直接认定为趋势证据。"""
    haystack = f"{evidence.title} {evidence.summary}".casefold()
    phrases = [candidate.name, candidate.route_family, *candidate.keywords]
    exact = any(
        len(value.strip()) >= 7 and value.casefold() in haystack
        for value in phrases
        if value
    )
    tokens = {
        token.casefold()
        for value in phrases
        for token in value.replace("-", " ").split()
        if len(token) >= 5
    }
    overlap = sum(token in haystack for token in tokens)
    return exact or overlap >= 2


def _official_repository_evidence(
    source: TechnicalEvidence, repository: str
) -> TechnicalEvidence:
    url = repository if repository.startswith("http") else f"https://github.com/{repository}"
    full_name = repository.rstrip("/").removeprefix("https://github.com/")
    return TechnicalEvidence(
        source="github",
        evidence_type=EvidenceType.IMPLEMENTATION,
        title=full_name,
        url=url,
        summary="Hugging Face 论文元数据直接关联的代码仓库",
        published_at=source.published_at,
        authors=source.authors,
        metrics={"stars": source.metrics.get("stars", 0)},
        identifiers={"github": full_name},
        raw={
            "relationship": "paper_linked_repository",
            "independence": "official",
        },
    )


def _dedupe_evidence(
    items: list[TechnicalEvidence], *, route_key: str
) -> list[TechnicalEvidence]:
    by_fingerprint: dict[str, TechnicalEvidence] = {}
    for item in items:
        previous = by_fingerprint.get(item.fingerprint)
        if (
            previous is not None
            and not previous.raw.get("indexed_discovery_only")
            and item.raw.get("indexed_discovery_only")
        ):
            # A later search hit for the same URL cannot replace an already
            # verified evidence relationship and its original content.
            continue
        if previous is not None:
            for key in (
                "metric_baseline", "metric_delta", "metric_delta_observed_at"
            ):
                if key in previous.raw and key not in item.raw:
                    item.raw[key] = copy.deepcopy(previous.raw[key])
            if is_verified_substantive_discussion(
                previous, route_key=route_key
            ) and (
                previous.source == item.source
                and previous.url.rstrip("/") == item.url.rstrip("/")
                and item.raw.get("relationship")
                not in {"author_self_release", "publisher_self_release"}
                and item.raw.get("independence")
                not in {"author", "publisher", "official", "self"}
            ):
                for key in (
                    "relationship", "independence", "substantive_uptake",
                    "substantive_uptake_source",
                    "substantive_uptake_route_key",
                ):
                    item.raw[key] = previous.raw[key]
                if (
                    previous.raw.get("content_scrubbed")
                    and not item.summary.strip()
                ):
                    # An API may stop returning a post body. The prior audit
                    # remains attributable to this exact URL and route; its
                    # body was intentionally removed, not discredited.
                    item.raw["content_scrubbed"] = True
            elif (
                previous.evidence_type == EvidenceType.IMPLEMENTATION
                and previous.raw.get("independence")
                in {"independent", "official", "publisher"}
                and previous.source == item.source
                and previous.url.rstrip("/") == item.url.rstrip("/")
                and item.raw.get("independence") in {None, "unverified"}
            ):
                item.raw["independence"] = previous.raw["independence"]
                item.raw["relationship"] = previous.raw.get("relationship", "")
        by_fingerprint[item.fingerprint] = item
    return list(by_fingerprint.values())


_UPTAKE_METRIC_KEYS = frozenset({
    "citations", "influential_citations", "stars", "forks", "likes",
    "replies", "comments", "score", "upvotes", "retweets", "reposts",
})


def _positive_metric_delta(current: dict, baseline: dict) -> dict[str, float | int]:
    delta = {}
    for key in _UPTAKE_METRIC_KEYS:
        difference = nonnegative_number(current.get(key, 0)) - nonnegative_number(
            baseline.get(key, 0)
        )
        if difference > 0:
            delta[key] = int(difference) if difference.is_integer() else difference
    return delta


def _new_evidence_requires_synthesis(item: TechnicalEvidence) -> bool:
    if item.raw.get("indexed_discovery_only"):
        return False
    if item.evidence_type in {
        EvidenceType.COMMUNITY_DISCUSSION,
        EvidenceType.SECONDARY_INTERPRETATION,
    }:
        return len(item.summary.strip()) >= 40
    if item.evidence_type == EvidenceType.IMPLEMENTATION:
        return item.raw.get("independence") in {
            "independent", "official", "publisher"
        }
    if item.evidence_type in {
        EvidenceType.INDEPENDENT_REPLICATION,
        EvidenceType.PRODUCT_ADOPTION,
    }:
        return item.raw.get("independence") == "independent"
    if item.evidence_type == EvidenceType.CITATION:
        return nonnegative_number(item.metrics.get("citations", 0)) >= 3
    return True


def _material_metric_growth(
    item: TechnicalEvidence,
    prior: TechnicalEvidence,
    delta: dict[str, float | int],
    *,
    route_key: str,
) -> bool:
    amount = lambda key: nonnegative_number(delta.get(key, 0))
    if item.evidence_type == EvidenceType.CITATION:
        return amount("citations") >= 3 or amount("influential_citations") >= 1
    if item.evidence_type in {
        EvidenceType.IMPLEMENTATION,
        EvidenceType.INDEPENDENT_REPLICATION,
        EvidenceType.PRODUCT_ADOPTION,
    }:
        independence = item.raw.get("independence")
        if independence not in {"independent", "official", "publisher"}:
            return False
        crossed_official_adoption = bool(
            item.evidence_type == EvidenceType.IMPLEMENTATION
            and independence in {"official", "publisher"}
            and (
                nonnegative_number(prior.metrics.get("stars", 0)) < 50
                <= nonnegative_number(item.metrics.get("stars", 0))
                or nonnegative_number(prior.metrics.get("forks", 0)) < 3
                <= nonnegative_number(item.metrics.get("forks", 0))
            )
        )
        return crossed_official_adoption or amount("stars") >= 25 or amount("forks") >= 3
    if item.evidence_type in {
        EvidenceType.COMMUNITY_DISCUSSION,
        EvidenceType.SECONDARY_INTERPRETATION,
    } and is_verified_substantive_discussion(item, route_key=route_key):
        return (
            amount("comments") >= 5 or amount("replies") >= 5
            or amount("likes") >= 20 or amount("score") >= 20
        )
    return False


def _attach_social_profiles(candidate: ParadigmCandidate) -> None:
    """只有社交账号显示名与论文作者可靠对齐时，才补入公开身份线索。"""
    for evidence in candidate.evidence:
        profile_url = str(evidence.raw.get("social_profile_url", ""))
        social_name = str(evidence.raw.get("social_author_name", ""))
        bio = str(evidence.raw.get("social_bio", "")).strip()
        if not profile_url or not social_name:
            continue
        for profile in candidate.researchers:
            if not _same_person_name(profile.name, social_name):
                continue
            platform = str(evidence.raw.get("social_platform") or "x")
            profile_key = "xiaohongshu" if platform == "xiaohongshu" else "x"
            profile.profile_urls.setdefault(profile_key, profile_url)
            if bio and not profile.public_bio_excerpt:
                profile.public_bio_excerpt = bio
            platform_label = "小红书" if profile_key == "xiaohongshu" else "X"
            note = (
                f"已用工作标题搜索 {platform_label}，并将发布账号显示名与论文作者名核验一致"
            )
            if note not in profile.contact_search_notes:
                profile.contact_search_notes.append(note)


def _same_person_name(left: str, right: str) -> bool:
    normalize = lambda value: "".join(
        character for character in value.casefold() if character.isalnum()
    )
    left_value, right_value = normalize(left), normalize(right)
    if not left_value or not right_value:
        return False
    if left_value == right_value:
        return True
    if min(len(left_value), len(right_value)) < 6:
        return False
    return SequenceMatcher(None, left_value, right_value).ratio() >= 0.9
