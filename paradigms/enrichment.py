"""为范式补齐学术、复现、社区和人物证据。"""

from __future__ import annotations

import asyncio
import logging
from difflib import SequenceMatcher

from sources.paradigm_evidence_source import CommunityEvidenceClient
from sources.semantic_scholar_source import SemanticScholarClient
from sources.researcher_profile_source import ResearcherProfileClient

from .models import EvidenceType, ParadigmCandidate, TechnicalEvidence

logger = logging.getLogger(__name__)


class EvidenceEnricher:
    def __init__(self, concurrency: int = 4):
        self.concurrency = max(concurrency, 1)
        self.semantic_scholar = SemanticScholarClient()
        self.community = CommunityEvidenceClient()
        self.researchers = ResearcherProfileClient()

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

    async def refresh(
        self,
        candidates: list[ParadigmCandidate],
        supporting: list[TechnicalEvidence],
    ) -> list[ParadigmCandidate]:
        """只保留本周出现新讨论/实现的历史候选，不重复跑学术身份接口。"""
        semaphore = asyncio.Semaphore(self.concurrency)

        async def refresh_one(candidate: ParadigmCandidate) -> ParadigmCandidate | None:
            async with semaphore:
                previous = {item.fingerprint for item in candidate.evidence}
                self._attach_support(candidate, supporting)
                try:
                    candidate.evidence.extend(await self.community.search(candidate))
                    candidate.community_coverage = self.community.coverage()
                except Exception as exc:
                    logger.warning("历史范式社区刷新失败 [%s]: %s", candidate.name, exc)
                candidate.evidence = _dedupe_evidence(candidate.evidence)
                if not any(item.fingerprint not in previous for item in candidate.evidence):
                    return None
                _attach_social_profiles(candidate)
                return candidate

        refreshed = await asyncio.gather(*(refresh_one(item) for item in candidates))
        return [item for item in refreshed if item is not None]

    async def _external_enrichment(self, candidate: ParadigmCandidate) -> None:
        lead = candidate.evidence[0]
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
            candidate.community_coverage = self.community.coverage()
        else:
            logger.warning("社区证据增强失败 [%s]: %s", candidate.name, community)
        candidate.evidence = list(
            {item.fingerprint: item for item in candidate.evidence}.values()
        )
        # Semantic Scholar 可匿名但经常受共享限流影响。无论其是否成功，
        # 都用当前论文作者建立人物种子，并通过 OpenAlex/ORCID 补齐身份。
        candidate.researchers = await self.researchers.enrich(
            lead, candidate.researchers
        )
        _attach_social_profiles(candidate)

    @staticmethod
    def finalize(candidates: list[ParadigmCandidate]) -> list[ParadigmCandidate]:
        """分析完成后不持久化社区用户正文，只保留可复核链接和聚合指标。"""
        for candidate in candidates:
            for evidence in candidate.evidence:
                if not evidence.raw.get("ephemeral_content"):
                    continue
                platform = str(evidence.raw.get("social_platform") or evidence.source)
                labels = {
                    "reddit": "Reddit 公开讨论",
                    "tavily-reddit": "Reddit 公开索引线索",
                    "x": "X 公开索引线索",
                    "tavily-x": "X 公开索引线索",
                    "xiaohongshu": "小红书公开索引线索",
                    "tavily-xiaohongshu": "小红书公开索引线索",
                }
                stable_id = next(iter(evidence.identifiers.values()), "")
                evidence.title = labels.get(platform, "社区公开讨论")
                if stable_id:
                    evidence.title = f"{evidence.title}（{stable_id}）"
                evidence.summary = ""
                evidence.authors = []
                for key in (
                    "social_author_name",
                    "social_bio",
                    "tavily_request_id",
                ):
                    evidence.raw.pop(key, None)
                evidence.raw["content_scrubbed"] = True
        return candidates

    @staticmethod
    def _attach_support(
        candidate: ParadigmCandidate, supporting: list[TechnicalEvidence]
    ) -> None:
        candidate_ids = {
            value.lower()
            for item in candidate.evidence
            for value in item.identifiers.values()
            if value
        }
        titles = [item.title.lower() for item in candidate.evidence]
        for item in supporting:
            support_ids = {value.lower() for value in item.identifiers.values() if value}
            title_match = max(
                (SequenceMatcher(None, item.title.lower(), title).ratio() for title in titles),
                default=0.0,
            )
            if candidate_ids & support_ids or title_match >= 0.9:
                candidate.evidence.append(item)
                repository = str(item.raw.get("github_repo", "")).strip()
                if repository:
                    candidate.evidence.append(
                        _official_repository_evidence(item, repository)
                    )
                continue
            if _lexically_related(candidate, item):
                candidate.evidence.append(item)


def _merge_profiles(existing, new):
    by_name = {profile.name.lower(): profile for profile in existing}
    for profile in new:
        by_name[profile.name.lower()] = profile
    return list(by_name.values())


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
        raw={"relationship": "paper_linked_repository"},
    )


def _dedupe_evidence(items: list[TechnicalEvidence]) -> list[TechnicalEvidence]:
    return list({item.fingerprint: item for item in items}.values())


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
