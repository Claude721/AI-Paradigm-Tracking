"""Route-level checks for secondary evidence admitted by synthesis.

Search results are leads. A source-specific record becomes substantive only
after its full body is visible to synthesis and the returned index is checked
against the current route and a distinct public actor.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .models import (
    ORIGIN_EVIDENCE_TYPES,
    EvidenceType,
    ParadigmCandidate,
    TechnicalEvidence,
)


_DIRECT_DISCUSSION_SOURCES = {
    "reddit": ("reddit.com",),
    "hackernews": ("news.ycombinator.com",),
    "x-title-search": ("x.com",),
}


def certify_synthesis_discussion(
    candidate: ParadigmCandidate, item: TechnicalEvidence
) -> bool:
    """Accept an expanded model-audited record only within a hard boundary.

    This function is called only for a visible evidence index explicitly
    selected by the synthesis model. It does not infer substantive analysis
    from a title, engagement counter, API search hit, or author reputation.
    """
    raw = item.raw or {}
    if (
        item.evidence_type not in {
            EvidenceType.COMMUNITY_DISCUSSION,
            EvidenceType.SECONDARY_INTERPRETATION,
        }
        or raw.get("indexed_discovery_only")
        or str(raw.get("relationship", "")).casefold()
        in {"author_self_release", "publisher_self_release"}
        or str(raw.get("independence", "")).casefold()
        in {"author", "publisher", "official", "self"}
    ):
        return False
    # Empty/short story text is a link announcement, not an inspected
    # discussion. The model must have seen the actual public analysis body.
    body = re.sub(r"<[^>]+>", " ", item.summary or "").strip()
    if len(body) < 40:
        return False
    if _same_actor_as_origin(candidate, item):
        return False

    if item.source in _DIRECT_DISCUSSION_SOURCES:
        parsed = urlparse(item.url)
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        if (
            parsed.scheme != "https"
            or host not in _DIRECT_DISCUSSION_SOURCES[item.source]
            or not _is_direct_discussion_url(item.source, parsed.path, parsed.query)
        ):
            return False
        if not any(str(author).strip() for author in item.authors):
            return False
    else:
        # Curated essays may arrive with a preliminary independent label,
        # but still need a directly attributable public article rather than
        # a copied search snippet or an unowned URL.
        parsed = urlparse(item.url)
        if (
            item.source not in {"curated-kol-blog", "curated-kol-x"}
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not any(str(author).strip() for author in item.authors)
        ):
            return False
        if raw.get("independence") != "independent":
            return False
        if raw.get("relationship") != "independent_commentary":
            return False

    raw["independence"] = "independent"
    raw["relationship"] = "independent_mechanism_analysis"
    raw["substantive_uptake"] = True
    raw["substantive_uptake_source"] = "synthesis-v1"
    raw["substantive_uptake_route_key"] = candidate.key
    return True


def _is_direct_discussion_url(source: str, path: str, query: str) -> bool:
    if source == "reddit":
        return bool(re.search(r"/comments/[a-z0-9]+(?:/|$)", path, re.I))
    if source == "hackernews":
        return path == "/item" and bool(re.search(r"(?:^|&)id=\d+(?:&|$)", query))
    if source == "x-title-search":
        return bool(re.fullmatch(r"/[^/]+/status/\d+/?", path))
    return False


def _same_actor_as_origin(
    candidate: ParadigmCandidate, item: TechnicalEvidence
) -> bool:
    actors = {
        _actor_key(value)
        for origin in candidate.evidence
        if origin.evidence_type in ORIGIN_EVIDENCE_TYPES
        for value in [*origin.authors, origin.organization]
        if _actor_key(value)
    }
    discussion_actors = {
        _actor_key(value)
        for value in [*item.authors, item.raw.get("social_author_name", "")]
        if _actor_key(value)
    }
    return bool(actors & discussion_actors)


def _actor_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())
