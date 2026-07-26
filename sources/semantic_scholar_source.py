"""Semantic Scholar Academic Graph API：引用、作者与研究轨迹增强。"""

from __future__ import annotations

import asyncio
import logging
import time
from difflib import SequenceMatcher

import httpx

import config
from paradigms.models import EvidenceType, ResearcherProfile, TechnicalEvidence

logger = logging.getLogger(__name__)
S2_BASE = "https://api.semanticscholar.org/graph/v1"


class SemanticScholarClient:
    def __init__(self):
        headers = {"User-Agent": "AI-Paradigm-Radar/2.0"}
        if config.SEMANTIC_SCHOLAR_API_KEY:
            headers["x-api-key"] = config.SEMANTIC_SCHOLAR_API_KEY
        self.headers = headers
        # Semantic Scholar 新 API Key 的标准起始额度为 1 RPS。
        self._rate_lock = asyncio.Lock()
        self._last_request_at = 0.0

    @property
    def configured(self) -> bool:
        """只有显式启用且存在获批 Key 时才允许请求。"""
        return bool(
            config.SEMANTIC_SCHOLAR_ENABLED
            and config.SEMANTIC_SCHOLAR_API_KEY
        )

    async def enrich_paper(
        self, evidence: TechnicalEvidence
    ) -> tuple[TechnicalEvidence | None, list[ResearcherProfile]]:
        if not self.configured:
            return None, []
        params = {
            "query": evidence.title,
            "limit": 5,
            "fields": (
                "title,url,abstract,year,publicationDate,citationCount,"
                "influentialCitationCount,externalIds,authors,venue"
            ),
        }
        async with httpx.AsyncClient(timeout=30, headers=self.headers) as client:
            response = await self._get(client, f"{S2_BASE}/paper/search", params)
            response.raise_for_status()
            matches = response.json().get("data", [])
            paper = _best_title_match(evidence.title, matches)
            if not paper:
                return None, []
            profiles = await self._author_profiles(client, paper.get("authors", [])[:2])

        external = paper.get("externalIds") or {}
        citation_evidence = TechnicalEvidence(
            source="semantic-scholar",
            evidence_type=EvidenceType.CITATION,
            title=paper.get("title", evidence.title),
            url=paper.get("url", ""),
            summary=f"{paper.get('venue', '')} 学术图谱引用与作者信号",
            published_at=paper.get("publicationDate", ""),
            authors=[item.get("name", "") for item in paper.get("authors", [])],
            organization=paper.get("venue", ""),
            metrics={
                "citations": paper.get("citationCount", 0) or 0,
                "influential_citations": paper.get("influentialCitationCount", 0) or 0,
            },
            identifiers={
                "semantic_scholar": paper.get("paperId", ""),
                "doi": external.get("DOI", ""),
                "arxiv": external.get("ArXiv", ""),
            },
        )
        return citation_evidence, profiles

    async def _author_profiles(
        self, client: httpx.AsyncClient, authors: list[dict]
    ) -> list[ResearcherProfile]:
        profiles = []
        for index, author in enumerate(authors):
            author_id = author.get("authorId")
            if not author_id:
                continue
            details_response = await self._get(
                client,
                f"{S2_BASE}/author/{author_id}",
                {
                    "fields": (
                        "name,aliases,url,externalIds,affiliations,homepage,"
                        "paperCount,citationCount,hIndex"
                    )
                },
            )
            papers_response = await self._get(
                client,
                f"{S2_BASE}/author/{author_id}/papers",
                {
                    "limit": 20,
                    "fields": "title,year,url,venue,citationCount,externalIds",
                },
            )
            if details_response.status_code >= 400 or papers_response.status_code >= 400:
                continue
            details = details_response.json()
            papers = papers_response.json().get("data", [])
            external = details.get("externalIds") or {}
            profile_urls = {"semantic_scholar": details.get("url", "")}
            if details.get("homepage"):
                profile_urls["homepage"] = details["homepage"]
            orcid = external.get("ORCID", "")
            if orcid:
                profile_urls["orcid"] = f"https://orcid.org/{orcid}"
            profiles.append(
                ResearcherProfile(
                    name=details.get("name") or author.get("name", ""),
                    role="第一作者" if index == 0 else "共同作者",
                    current_affiliation=(details.get("affiliations") or [""])[0],
                    prior_affiliations=(details.get("affiliations") or [])[1:],
                    representative_works=[
                        {
                            "title": paper.get("title", ""),
                            "year": paper.get("year"),
                            "url": paper.get("url", ""),
                            "venue": paper.get("venue", ""),
                            "citations": paper.get("citationCount", 0) or 0,
                        }
                        for paper in sorted(
                            papers,
                            key=lambda value: (
                                value.get("year") or 0,
                                value.get("citationCount") or 0,
                            ),
                            reverse=True,
                        )[:8]
                    ],
                    profile_urls={k: v for k, v in profile_urls.items() if v},
                    identifiers={
                        "semantic_scholar": str(author_id),
                        **({"orcid": orcid} if orcid else {}),
                    },
                )
            )
        return profiles

    async def _get(
        self, client: httpx.AsyncClient, url: str, params: dict
    ) -> httpx.Response:
        """所有 S2 请求共享 1 RPS 节流，避免候选并发触发 429。"""
        async with self._rate_lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < 1.05:
                await asyncio.sleep(1.05 - elapsed)
            response = await client.get(url, params=params)
            self._last_request_at = time.monotonic()
            return response


def _best_title_match(title: str, papers: list[dict]) -> dict | None:
    scored = [
        (SequenceMatcher(None, title.lower(), paper.get("title", "").lower()).ratio(), paper)
        for paper in papers
    ]
    if not scored:
        return None
    score, paper = max(scored, key=lambda item: item[0])
    return paper if score >= 0.82 else None
