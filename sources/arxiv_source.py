"""arXiv 信源：发现可能改变能力边界的最新 AI 技术工作。"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

from .base import BaseSource, RawProject

logger = logging.getLogger(__name__)

ARXIV_API = "https://export.arxiv.org/api/query"

ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}

SEARCH_QUERIES = [
    'all:"foundation model"',
    'all:"world model"',
    'all:"reasoning model"',
    'all:"agentic learning"',
    'all:"reinforcement learning" AND all:reasoning',
    'all:"vision language action"',
    'all:"embodied intelligence"',
    'all:"self-supervised" AND all:video',
    'all:"continual learning"',
    'all:"test-time learning"',
    'all:"multimodal learning"',
    'all:"synthetic data"',
    'all:"model architecture"',
    'all:"in-context learning"',
]


class ArxivSource(BaseSource):
    source_name = "arxiv"

    def __init__(self, max_results: int = 30, lookback_days: int = 3):
        self.max_results = max_results
        self.lookback_days = lookback_days

    async def fetch(self) -> list[RawProject]:
        query = " OR ".join(f"({q})" for q in SEARCH_QUERIES)
        # arXiv API 用 cat: 限定类目
        full_query = (
            f"({query}) AND (cat:cs.AI OR cat:cs.CL OR cat:cs.LG "
            "OR cat:cs.CV OR cat:cs.RO OR cat:cs.NE OR cat:stat.ML)"
        )

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(
                ARXIV_API,
                params={
                    "search_query": full_query,
                    "start": 0,
                    "max_results": self.max_results,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                },
            )
            resp.raise_for_status()

        return self._parse_atom_feed(resp.text)

    def _parse_atom_feed(self, xml_text: str) -> list[RawProject]:
        root = ET.fromstring(xml_text)
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        results: list[RawProject] = []

        for entry in root.findall("atom:entry", ARXIV_NS):
            published_str = entry.findtext("atom:published", "", ARXIV_NS)
            if published_str:
                pub_date = datetime.fromisoformat(
                    published_str.replace("Z", "+00:00")
                )
                if pub_date < cutoff:
                    continue

            title = entry.findtext("atom:title", "", ARXIV_NS).strip().replace("\n", " ")
            summary = entry.findtext("atom:summary", "", ARXIV_NS).strip()

            link = ""
            for link_elem in entry.findall("atom:link", ARXIV_NS):
                if link_elem.get("type") == "text/html":
                    link = link_elem.get("href", "")
                    break
            if not link:
                id_text = entry.findtext("atom:id", "", ARXIV_NS)
                link = id_text

            authors = [
                a.findtext("atom:name", "", ARXIV_NS)
                for a in entry.findall("atom:author", ARXIV_NS)
            ]
            first_author = authors[0] if authors else ""

            categories = [
                c.get("term", "")
                for c in entry.findall("atom:category", ARXIV_NS)
            ]

            results.append(
                RawProject(
                    source=self.source_name,
                    name=title,
                    url=link,
                    description=summary[:1500],
                    readme_summary=summary[:3000],
                    author=first_author,
                    topics=categories,
                    created_at=published_str,
                    extra={
                        "arxiv_id": link.rstrip("/").split("/")[-1],
                        "all_authors": authors,
                        "type": "paper",
                    },
                )
            )

        return results
