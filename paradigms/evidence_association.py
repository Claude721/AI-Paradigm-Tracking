"""Work identity, not topical similarity, authorizes factual support.

Discovery may keep broad leads. They must never lend a different work's
publisher, repository adoption or implementation identity to this route.
"""
from __future__ import annotations

import re
import hashlib
import json
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
        words = re.findall(r"[^\W_]+", origin.title, re.UNICODE)
        if len(title) >= 20 and words and re.search(r"(?<!\w)" + r"[\W_]*".join(
                re.escape(word) for word in words) + r"(?!\w)", evidence.title + " " + evidence.summary, re.I):
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
    if evidence.evidence_type in ORIGIN_EVIDENCE_TYPES and candidate.mechanism_origin_support:
        receipt = candidate.mechanism_origin_support
        if (receipt.get("route_key") != candidate.key or
            receipt.get("mechanism_digest") != mechanism_digest(candidate) or
            receipt.get("origins", {}).get(evidence.fingerprint) != support_revision(evidence)):
            return False
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


def mechanism_digest(candidate):
    return hashlib.sha256(candidate.mechanism.strip().encode()).hexdigest()


def support_revision(evidence):
    # Unobserved/legacy objects can have no store revision yet. Their actual
    # public material still needs a content-bound receipt, never an empty tag.
    return evidence.source_revision or hashlib.sha256(json.dumps({
        "url": evidence.url, "title": evidence.title, "summary": evidence.summary,
        "authors": evidence.authors, "identifiers": evidence.identifiers,
    }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def distinct_origins(evidence):
    """An arXiv mirror or second adapter is still one original work."""
    groups = []
    for item in evidence:
        if item.evidence_type not in ORIGIN_EVIDENCE_TYPES:
            continue
        tokens = {("id", key, value) for key, value in _ids(item)}
        url = item.url.rstrip("/")
        if url:
            tokens.add(("url", url))
            parsed = urlparse(url)
            if parsed.hostname in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
                match = re.match(r"/(?:abs|pdf|html)/([0-9]{4}\.[0-9]{4,5})(?:v[0-9]+)?(?:\.pdf)?$", parsed.path)
                if match:
                    tokens.add(("id", "arxiv", match[1]))
        tokens.add(("fingerprint", item.fingerprint))
        matches = [index for index, (_, known) in enumerate(groups) if known & tokens]
        if not matches:
            groups.append((item, tokens))
            continue
        # A later DOI+arXiv bridge can prove that two earlier records were the
        # same work; merge the whole identity component, not just the new row.
        canonical = groups[matches[0]][0]
        for index in reversed(matches):
            tokens.update(groups.pop(index)[1])
        groups.insert(matches[0], (canonical, tokens))
    return [item for item, _ in groups]


def supported_origins(candidate):
    origins = distinct_origins(item for item in candidate.evidence if countable_evidence(candidate, item))
    # Legacy/unjudged co-clusters have not proved convergence. Keep one primary
    # claim, never manufacture multi-origin uptake from topical grouping.
    return origins if candidate.mechanism_origin_support else origins[:1]


def has_independent_origins(origins):
    """Different affiliations do not make the same authors independent."""
    from .reputation import resolve_organization
    def publisher(item):
        known = resolve_organization(item.organization)
        return _normal(known["name"] if known else item.organization)
    for index, left in enumerate(origins):
        for right in origins[index + 1:]:
            if not publisher(left) or not publisher(right) or publisher(left) == publisher(right):
                continue
            if {_normal(name) for name in left.authors} & {_normal(name) for name in right.authors}:
                continue
            return True
    return False


def profile_references_origin(candidate, profile):
    """A verified person is not automatically an author of this mechanism."""
    for origin in candidate.evidence:
        if origin.evidence_type not in ORIGIN_EVIDENCE_TYPES or not countable_evidence(candidate, origin):
            continue
        if any(_normal(profile.name) == _normal(name) for name in origin.authors):
            return True
        for work in profile.representative_works:
            if origin.url and work.get("url") == origin.url:
                return True
            if len(_normal(origin.title)) >= 20 and _normal(work.get("title", "")) == _normal(origin.title):
                return True
    return False
