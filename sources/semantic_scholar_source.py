"""Semantic Scholar Academic Graph API：引用、作者与研究轨迹增强。"""

from __future__ import annotations

import asyncio
import logging
import time
from difflib import SequenceMatcher

import httpx

import config
from paradigms.models import EvidenceType, ResearcherProfile, TechnicalEvidence
from paradigms.researcher_identity import (bind_current_work, normalized_person_name,
                                          record_identity_observation, verify_contact)
from runtime_clock import observation_now
from sources.professional_identity import profile_label

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
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("Invalid Semantic Scholar search response")
            matches = payload["data"]
            if any(not isinstance(item, dict) or not isinstance(item.get("title"), str) for item in matches):
                raise ValueError("Invalid Semantic Scholar paper record")
            paper = _best_title_match(evidence.title, matches)
            if not paper:
                return None, []
            authors = paper.get("authors") or []
            if not isinstance(authors, list) or any(not isinstance(author, dict) for author in authors):
                raise ValueError("Invalid Semantic Scholar authors")
            profiles = []
            if _identity_paper_match(evidence, paper, matches):
                for author in authors[:2]:
                    name = str(author.get("name") or "")
                    normalized = normalized_person_name(name)
                    if (not normalized or normalized not in {normalized_person_name(n) for n in evidence.authors}
                            or sum(normalized_person_name(a.get("name", "")) == normalized for a in authors) != 1):
                        continue
                    # Author API faults are local: keep healthy peers and the
                    # citation result rather than losing the whole response.
                    try:
                        people = await self._author_profiles(client, [author])
                    except Exception as exc:
                        person = ResearcherProfile(name=name, identity_search_status="failed", contact_search_status="failed")
                        bind_current_work(person, evidence)
                        record_identity_observation(person, "semantic_scholar", "failed", url=paper.get("url", ""), reason=type(exc).__name__)
                        profiles.append(person)
                        continue
                    for person in people:
                        bind_current_work(person, evidence)
                        person.role = "原文署名作者（学术图谱对齐）"
                        for label, url in list(person.profile_urls.items()):
                            kind = profile_label(url, label)
                            if kind:
                                verify_contact(person, kind, url, person.profile_urls.get("semantic_scholar") or evidence.url, "academic_identity")
                        person.identity_search_status = "completed"
                        person.contact_search_status = "completed" if person.public_contacts else "not_found"
                        record_identity_observation(person, "semantic_scholar", "completed", url=paper.get("url", ""), reason="CurrentWorkAligned")
                        profiles.append(person)

        external = paper.get("externalIds") or {}
        citation_evidence = TechnicalEvidence(
            source="semantic-scholar",
            evidence_type=EvidenceType.CITATION,
            title=paper.get("title", evidence.title),
            url=paper.get("url", ""),
            summary=f"{paper.get('venue', '')} 学术图谱引用与作者信号",
            published_at=str(paper.get("publicationDate") or ""),
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
            details_response.raise_for_status()
            papers_response.raise_for_status()
            details = details_response.json()
            payload = papers_response.json()
            if (not isinstance(details, dict) or not isinstance(details.get("name"), str)
                    or str(details.get("authorId", "")) != str(author_id)
                    or normalized_person_name(details.get("name", "")) != normalized_person_name(author.get("name", ""))
                    or not isinstance(payload, dict) or not isinstance(payload.get("data"), list)
                    or any(not isinstance(item, dict) for item in payload["data"])):
                raise ValueError("Invalid Semantic Scholar author response")
            papers = payload["data"]
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
                    affiliation_observations=[{"organization": organization,
                        "source_url": details.get("url", ""), "kind": "undated_graph_affiliation",
                        "observed_at": observation_now().isoformat()}
                        for organization in details.get("affiliations", []) or [] if isinstance(organization, str) and organization],
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


def _identity_paper_match(evidence, paper, matches):
    """Citation fuzzy recall is not permission to attach a person's identity."""
    external = paper.get("externalIds") or {}
    if not isinstance(external, dict):
        return False
    overlaps = []
    for key, remote in (("doi", "DOI"), ("arxiv", "ArXiv")):
        local, returned = str(evidence.identifiers.get(key) or ""), str(external.get(remote) or "")
        if local and returned:
            normalize = lambda value: value.casefold().removeprefix("https://doi.org/").removeprefix("http://doi.org/")
            overlaps.append(normalize(local) == normalize(returned))
    if overlaps:
        return all(overlaps)
    title = normalized_person_name(evidence.title)
    return bool(title and title == normalized_person_name(paper.get("title", ""))
                and sum(normalized_person_name(item.get("title", "")) == title for item in matches) == 1)
