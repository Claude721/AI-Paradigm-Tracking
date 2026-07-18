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
