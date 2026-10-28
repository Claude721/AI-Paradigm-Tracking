"""Publication facts, not index/repository/revision dates.

Only public arXiv landing metadata is resolved here. No model calls, guessed
days from identifiers, or mutations of already-bound source revisions.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from .document_limits import bounded_document_get


def arxiv_identifier(evidence):
    values = [evidence.identifiers.get("arxiv", ""), evidence.identifiers.get("doi", ""), evidence.url]
    for value in values:
        value = str(value).strip()
        match = re.fullmatch(r"(?:arxiv:|10\.48550/arxiv\.)?(\d{4}\.\d{4,5})(?:v\d+)?", value, re.I)
        if not match:
            try:
                parsed = urlsplit(value)
                if parsed.hostname in {"arxiv.org", "export.arxiv.org"}:
                    match = re.fullmatch(r"/(?:abs|pdf|html)/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?/?", parsed.path, re.I)
                elif parsed.hostname in {"doi.org", "dx.doi.org"}:
                    match = re.fullmatch(r"/10\.48550/arxiv\.(\d{4}\.\d{4,5})(?:v\d+)?/?", parsed.path, re.I)
            except ValueError:
                continue
        if match:
            return match.group(1)
    return ""


class _LandingMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "meta":
            attrs = dict(attrs)
            key = (attrs.get("name") or attrs.get("property") or "").casefold()
            self.meta.setdefault(key, attrs.get("content") or "")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def arxiv_publication_dates(body):
    """Prefer v1 history over citation_date (often only day precision)."""
    parser = _LandingMetadata()
    parser.feed(body)
    text = " ".join(" ".join(parser.parts).split())
    versions = []
    for match in re.finditer(r"\[v(\d+)\]\s*([A-Za-z]{3},\s*\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}:\d{2}\s+(?:UTC|GMT))", text):
        try:
            date = parsedate_to_datetime(match.group(2).replace("UTC", "+0000"))
            versions.append((int(match.group(1)), date.astimezone(timezone.utc).isoformat()))
        except (ValueError, TypeError):
            continue
    first = next((date for version, date in versions if version == 1), "")
    precision = "second" if first else ""
    if not first:
        # This is arXiv's publication metadata, never dateModified or a related
        # article's <time>. A month-only value stays unknown.
        for key in ("citation_date", "citation_publication_date", "article:published_time"):
            value = parser.meta.get(key, "").strip().replace("/", "-")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.+)?", value):
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            first = value if len(value) == 10 else parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
            precision = "day" if len(value) == 10 else "second"
            break
    revised = max(versions, default=(0, ""))[1] if any(v > 1 for v, _ in versions) else ""
    return {"published_at": first, "modified_at": revised, "precision": precision}


def publication_date_rank(evidence):
    if not evidence.published_at:
        return 0
    source = evidence.raw.get("publication_date_source", "")
    if source == "arxiv_submission_history":
        return 5
    if evidence.source == "arxiv" or source == "arxiv_api":
        return 4
    if source == "arxiv_publication_metadata":
        return 3
    if evidence.source == "huggingface-papers":
        return 1
    return 2


async def resolve_origin_dates(origins, *, client_factory=None, concurrency=6):
    """Resolve selected index/repository paper dates BEFORE source observation.

    One landing GET per distinct work; failures remain explicit and local.
    Date failures do not discard available technical material or other work.
    """
    targets = {}
    for item in origins:
        identifier = arxiv_identifier(item)
        if identifier and not item.source_revision and publication_date_rank(item) < 4:
            item.raw["publication_date_status"] = "unresolved_pending"
            targets.setdefault(identifier, []).append(item)
    stats = {"attempted": len(targets), "resolved": 0, "failed": 0}
    semaphore = asyncio.Semaphore(concurrency)
    async with (client_factory or httpx.AsyncClient)(timeout=15, follow_redirects=True,
            headers={"User-Agent": "AI-Paradigm-Radar/3.3"}) as client:
        async def resolve(identifier, items):
            try:
                async with semaphore:
                    response = await bounded_document_get(client, f"https://arxiv.org/abs/{identifier}")
                response.raise_for_status()
                from paradigms.publication import nonresearch_page_reason
                final = urlsplit(str(response.url))
                if (final.hostname not in {"arxiv.org", "export.arxiv.org"}
                        or not re.fullmatch(r"/abs/" + re.escape(identifier) + r"(?:v\d+)?/?", final.path)
                        or nonresearch_page_reason(str(response.url), response.text)):
                    raise ValueError("Non-research publication metadata")
                dates = arxiv_publication_dates(response.text)
                if not dates["published_at"]:
                    raise ValueError("Missing original publication date")
                for item in items:
                    item.raw["source_published_at"] = item.raw.get("source_published_at", item.published_at)
                    item.published_at = dates["published_at"]
                    item.raw["publication_date_source"] = ("arxiv_submission_history" if dates["precision"] == "second" else "arxiv_publication_metadata")
                    item.raw["publication_date_precision"] = dates["precision"]
                    item.raw["publication_date_status"] = "verified"
                    if dates["modified_at"]:
                        item.raw["arxiv_last_revised_at"] = dates["modified_at"]
                stats["resolved"] += 1
            except Exception as exc:
                stats["failed"] += 1
                for item in items:
                    item.raw["publication_date_status"] = "unresolved_" + type(exc).__name__
        await asyncio.gather(*(resolve(key, items) for key, items in targets.items()))
    return stats
