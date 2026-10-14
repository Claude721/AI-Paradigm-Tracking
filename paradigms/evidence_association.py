"""Work identity, not topical similarity, authorizes factual support.

Discovery may keep broad leads. They must never lend a different work's
publisher, repository adoption or implementation identity to this route.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .models import ORIGIN_EVIDENCE_TYPES, EvidenceType

VERSION = 2
WORK_ID_KEYS = frozenset({"arxiv", "doi", "openreview"})


def _normal(value):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(value).casefold())


def _ids(evidence):
    return _work_ids(evidence.identifiers)


def _work_ids(identifiers):
    return {(key, re.sub(r"v\d+$", "", str(value).casefold()) if key == "arxiv" else str(value).casefold() if key == "doi" else str(value))
            for key, value in identifiers.items() if key in WORK_ID_KEYS and value}


def references_origin(candidate, evidence):
    origins = [item for item in candidate.evidence if item.evidence_type in ORIGIN_EVIDENCE_TYPES]
    ids = _ids(evidence)
    text = _normal(evidence.title + " " + evidence.summary)
    for origin in origins:
        if ids & _ids(origin):
            return True
        title = _normal(origin.title)
        if len(title) >= 20 and title in text:
            return True
        # Require an actual stable reference, never two generic keyword hits.
        if origin.url and origin.url in evidence.summary:
            return True
        for key, value in _ids(origin):
            if key == "arxiv" and re.search(r"(?<!\d)" + re.escape(value) + r"(?:v\d+)?(?!\d)", evidence.summary, re.I):
                return True
            if key == "doi" and value in evidence.summary.casefold():
                return True
    return False


def countable_evidence(candidate, evidence):
    if evidence.raw.get("association_status") == "unverified":
        return False
    if evidence.evidence_type in {EvidenceType.IMPLEMENTATION, EvidenceType.INDEPENDENT_REPLICATION}:
        parsed = urlparse(evidence.url)
        parts = [part.casefold() for part in parsed.path.split("/") if part]
        repository = parts[1] if parsed.hostname in {"github.com", "www.github.com"} and len(parts) >= 2 else ""
        if (re.search(r"^(?:awesome|cookbook|examples?|tutorials?|docs|website)(?:[-_]|$)|(?:arxiv|paper)[-_]daily|(?:paper|reading)[-_]list|research[-_]collection|rss[-_]feed", repository)
            or re.search(r"\b(?:curated list of|collection of papers|awesome list|daily papers|reading list)\b", evidence.summary[:3000], re.I)):
            return False  # Even an exact paper citation does not make a list code.
    if evidence.source == "official-repository-release" and evidence.evidence_type not in ORIGIN_EVIDENCE_TYPES:
        # Includes old saved snapshots which predate association receipts.
        return references_origin(candidate, evidence)
    if evidence.raw.get("relationship") == "paper_linked_repository":
        parsed = urlparse(evidence.url)
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.hostname not in {"github.com", "www.github.com"} or len(parts) != 2:
            return False  # issue/wiki/navigation links are not implementations.
        if parts[0].casefold() == "arxiv" or parts[1].casefold() == "latexml":
            return False  # document-rendering infrastructure, not paper code.
        origins = [item for item in candidate.evidence if item.evidence_type in ORIGIN_EVIDENCE_TYPES]
        linked_ids = evidence.raw.get("origin_work_ids", {})
        linked_fingerprint = evidence.raw.get("origin_fingerprint")
        if evidence.raw.get("association_version") == VERSION:
            return any(linked_fingerprint == item.fingerprint or (
                isinstance(linked_ids, dict) and _ids(item) & _work_ids(linked_ids)
            ) for item in origins)
        # Old HTML snapshots mixed actual code with dependency/navigation
        # links. Only explicit same-work metadata can authorize reuse.
        return any(str(item.raw.get("github_repo", "")).rstrip("/").removeprefix("https://github.com/").casefold()
                   == "/".join(parts).casefold() for item in origins)
    return True
