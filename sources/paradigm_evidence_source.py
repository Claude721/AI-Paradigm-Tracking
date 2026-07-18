"""把 GitHub 与 Hacker News 从“候选生成器”降级为范式扩散证据。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

import config
from paradigms.models import EvidenceType, ParadigmCandidate, TechnicalEvidence

logger = logging.getLogger(__name__)


class CommunityEvidenceClient:
    async def search(self, candidate: ParadigmCandidate) -> list[TechnicalEvidence]:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            github, hn = await self._github(client, candidate), await self._hackernews(
                client, candidate
            )
        return github + hn

    async def _github(
        self, client: httpx.AsyncClient, candidate: ParadigmCandidate
    ) -> list[TechnicalEvidence]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "AI-Paradigm-Radar/2.0",
        }
        if config.GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
        primary_id = next(
            (
                value
                for item in candidate.evidence
                for key, value in item.identifiers.items()
                if key in {"arxiv", "doi"} and value
            ),
            "",
        )
        query_term = primary_id or candidate.name
        response = await client.get(
            "https://api.github.com/search/repositories",
            headers=headers,
            params={
                "q": f'"{query_term}" in:name,description,readme',
                "sort": "updated",
                "order": "desc",
                "per_page": 5,
            },
        )
        if response.status_code >= 400:
            logger.warning("GitHub 范式证据搜索失败: HTTP %s", response.status_code)
            return []
        results = []
        for repo in response.json().get("items", []):
            if not _is_relevant_repository(candidate, repo):
                continue
            results.append(
                TechnicalEvidence(
                    source="github",
                    evidence_type=EvidenceType.IMPLEMENTATION,
                    title=repo.get("full_name", ""),
                    url=repo.get("html_url", ""),
                    summary=repo.get("description") or "",
                    published_at=repo.get("created_at", ""),
                    authors=[(repo.get("owner") or {}).get("login", "")],
                    metrics={
                        "stars": repo.get("stargazers_count", 0) or 0,
                        "forks": repo.get("forks_count", 0) or 0,
                    },
                    identifiers={"github": repo.get("full_name", "")},
                    raw={
                        "updated_at": repo.get("updated_at", ""),
                        "relationship": "name_and_mechanism_match",
                    },
                )
            )
        return results

    async def _hackernews(
        self, client: httpx.AsyncClient, candidate: ParadigmCandidate
    ) -> list[TechnicalEvidence]:
        cutoff = int(
            (datetime.now(timezone.utc) - timedelta(days=config.SOURCING_LOOKBACK_DAYS)).timestamp()
        )
        query = candidate.name
        response = await client.get(
            "https://hn.algolia.com/api/v1/search",
            params={
                "query": query,
                "tags": "story",
                "numericFilters": f"created_at_i>{cutoff}",
                "hitsPerPage": 10,
            },
        )
        if response.status_code >= 400:
            return []
        results = []
        for hit in response.json().get("hits", []):
            if not _is_related(candidate, hit.get("title") or ""):
                continue
            object_id = hit.get("objectID", "")
            results.append(
                TechnicalEvidence(
                    source="hackernews",
                    evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
                    title=hit.get("title") or query,
                    url=f"https://news.ycombinator.com/item?id={quote(str(object_id))}",
                    summary=hit.get("story_text") or "",
                    published_at=hit.get("created_at", ""),
                    authors=[hit.get("author", "")],
                    metrics={
                        "score": hit.get("points", 0) or 0,
                        "comments": hit.get("num_comments", 0) or 0,
                    },
                    identifiers={"hackernews": str(object_id)},
                )
            )
        return results


def _is_related(candidate: ParadigmCandidate, title: str) -> bool:
    title_tokens = {
        token.lower().strip("-_/.,:()[]")
        for token in title.split()
        if len(token.strip("-_/.,:()[]")) >= 4
    }
    candidate_tokens = {
        token.lower().strip("-_/.,:()[]")
        for value in [candidate.name, *candidate.keywords]
        for token in value.split()
        if len(token.strip("-_/.,:()[]")) >= 4
    }
    required = 1 if len(candidate_tokens) <= 2 else 2
    return len(title_tokens & candidate_tokens) >= required


def _is_relevant_repository(candidate: ParadigmCandidate, repo: dict) -> bool:
    """宁可漏掉弱信号，也不把论文聚合仓库伪装成实现。"""
    full_name = str(repo.get("full_name", ""))
    description = str(repo.get("description") or "")
    text = f"{full_name} {description}".casefold()
    noise_markers = {
        "arxiv-daily",
        "arxiv_daily",
        "paper-daily",
        "paper_daily",
        "research-collection",
        "research_collection",
        "awesome-daily",
        "awesome_papers",
        "paper-list",
        "paper_list",
        "arxiv-radar",
        "rss-feed",
        "hfpaper",
    }
    if any(marker in text for marker in noise_markers):
        return False

    compact_repo = "".join(
        character
        for character in full_name.rsplit("/", 1)[-1].casefold()
        if character.isalnum()
    )
    compact_name = "".join(character for character in candidate.name.casefold() if character.isalnum())
    if min(len(compact_repo), len(compact_name)) >= 8 and (
        compact_name in compact_repo or compact_repo in compact_name
    ):
        return True

    candidate_tokens = {
        token.casefold().strip("-_/.,:()[]")
        for value in [candidate.name, candidate.route_family, *candidate.keywords]
        for token in value.split()
        if len(token.strip("-_/.,:()[]")) >= 5
    }
    overlap = {token for token in candidate_tokens if token in text}
    return len(overlap) >= 2
