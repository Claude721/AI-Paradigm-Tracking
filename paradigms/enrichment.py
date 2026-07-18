"""为范式补齐学术、复现、社区和人物证据。"""

from __future__ import annotations

import asyncio
import logging
from difflib import SequenceMatcher

from sources.paradigm_evidence_source import CommunityEvidenceClient
from sources.semantic_scholar_source import SemanticScholarClient

from .models import ParadigmCandidate, TechnicalEvidence

logger = logging.getLogger(__name__)


class EvidenceEnricher:
    def __init__(self, concurrency: int = 4):
        self.concurrency = max(concurrency, 1)
        self.semantic_scholar = SemanticScholarClient()
        self.community = CommunityEvidenceClient()

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


def _merge_profiles(existing, new):
    by_name = {profile.name.lower(): profile for profile in existing}
    for profile in new:
        by_name[profile.name.lower()] = profile
    return list(by_name.values())
