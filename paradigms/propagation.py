"""Public attention is not independent technical validation or eligibility.

Index snippets can wake research, but cannot certify that a public post was
read. Direct API posts get a small source/route/version-bound receipt which
survives removal of temporary community text. No network or model calls here.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from runtime_clock import research_now
from .models import (ORIGIN_EVIDENCE_TYPES, EvidenceType, _evidence_datetime,
                     safe_public_contact_target, material_metric_signature)
from .evidence_association import countable_evidence, mechanism_digest, support_revision


DISCUSSION_TYPES = {EvidenceType.COMMUNITY_DISCUSSION, EvidenceType.SECONDARY_INTERPRETATION}


def work_reference(candidate, evidence):
    from sources.social_web_search_source import work_reference_reason
    return work_reference_reason(candidate, " ".join((evidence.title, evidence.summary, evidence.url,
                                str(evidence.raw.get("referenced_url", "")))))


def _origin_versions(candidate):
    return {x.fingerprint: support_revision(x) for x in candidate.evidence
            if x.evidence_type in ORIGIN_EVIDENCE_TYPES and countable_evidence(candidate, x)}


def _binding_digest(candidate, origins, *, legacy=False):
    binding = {"route_key": candidate.key, "origins": sorted(origins.items())}
    if legacy:
        binding["mechanism"] = mechanism_digest(candidate)
    return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()


def _direct_post(item):
    if item.raw.get("indexed_discovery_only") or not safe_public_contact_target("source", item.url):
        return False
    parsed = urlsplit(item.url)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if item.source == "hackernews":
        return host == "news.ycombinator.com" and parsed.path == "/item" and str(parse_qs(parsed.query).get("id", [""])[0]).isdigit()
    if item.source == "reddit":
        return (host == "reddit.com" or host.endswith(".reddit.com")) and "/comments/" in parsed.path
    if item.source == "x-title-search":
        return host in {"x.com", "twitter.com"} and "/status/" in parsed.path
    return False


def verified_diffusion(candidate, item):
    """A source-verified same-work post, not a claim of independent adoption."""
    if item.evidence_type not in DISCUSSION_TYPES or not countable_evidence(candidate, item) or not _direct_post(item):
        return False
    receipt = item.raw.get("diffusion_receipt", {})
    if (not isinstance(receipt, dict) or receipt.get("fingerprint") != item.fingerprint
            or receipt.get("url") != item.url or receipt.get("published_at") != item.published_at
            or not _evidence_datetime(item.published_at)):
        return False
    current = _origin_versions(candidate)
    if receipt.get("version") == 1:
        # Old receipts remain valid only under their original, stricter binding.
        # An already-invalid hash cannot establish a new work association.
        return bool(current) and receipt.get("binding") == _binding_digest(candidate, current, legacy=True)
    origins = receipt.get("origins")
    return (receipt.get("version") == 2 and isinstance(origins, dict) and bool(origins)
            and all(isinstance(key, str) and isinstance(revision, str) and revision
                    and current.get(key) == revision for key, revision in origins.items())
            and receipt.get("binding") == _binding_digest(candidate, origins))


def certify_diffusion(candidate):
    """Check work identity while text is available; never infer fame/quality."""
    for item in candidate.evidence:
        if item.evidence_type not in DISCUSSION_TYPES or not countable_evidence(candidate, item):
            continue
        valid = verified_diffusion(candidate, item)
        if valid and item.raw["diffusion_receipt"]["version"] == 2:
            continue
        if not _direct_post(item) or not _evidence_datetime(item.published_at):
            continue
        matches = {}
        reason = ""
        for origin in candidate.evidence:
            if origin.evidence_type not in ORIGIN_EVIDENCE_TYPES or not countable_evidence(candidate, origin):
                continue
            match = work_reference(SimpleNamespace(evidence=[origin]), item)
            if match in {"exact_work_title", "canonical_work_url", "stable_work_id"}:
                matches[origin.fingerprint] = support_revision(origin)
                reason = match
        if not matches and valid:
            # A valid v1 audit may have intentionally discarded the post body.
            # Keep all its original work dependencies when upgrading, not a
            # guessed subset. Do this before MAIN can paraphrase the mechanism.
            matches = _origin_versions(candidate)
            reason = item.raw["diffusion_receipt"].get("association", "legacy_work_audit")
        if not matches:
            continue
        item.raw["diffusion_receipt"] = {"version": 2, "origins": matches,
            "binding": _binding_digest(candidate, matches),
            "fingerprint": item.fingerprint, "url": item.url, "published_at": item.published_at,
            "association": reason, "meaning": "public_attention_not_technical_validation"}


def diffusion_items(candidate):
    return [item for item in candidate.evidence if verified_diffusion(candidate, item)]


def propagation_summary(candidate):
    attention = diffusion_items(candidate)
    return {"role": "advisory_not_eligibility", "verified_public_post_count": len(attention),
        "indexed_lead_count": sum(bool(x.raw.get("indexed_discovery_only")) for x in candidate.evidence),
        "meaning": "工作层面被看见，不等于每个子机制被讨论、独立复现或技术结论获验证",
        "public_posts": [{"url": x.url, "source": x.source, "published_at": x.published_at,
                          "metrics": x.metrics, "publisher_self_release": x.raw.get("relationship") == "author_self_release"}
                         for x in attention]}


def event_signature(item):
    return hashlib.sha256(json.dumps([item.fingerprint, item.published_at,
                         material_metric_signature(item.metrics)], sort_keys=True).encode()).hexdigest()


def recent_related_events(candidate, supporting, *, reference_time=None, window_days=7):
    now = research_now(reference_time)
    cutoff = now - timedelta(days=max(window_days, 1))
    existing = {event_signature(x) for x in candidate.evidence}
    return [x for x in supporting if x.evidence_type not in ORIGIN_EVIDENCE_TYPES
            and (date := _evidence_datetime(x.published_at)) and cutoff <= date <= now
            and safe_public_contact_target("source", x.url) and event_signature(x) not in existing
            and work_reference(candidate, x)]


def history_recall_plan(candidates, supporting, *, limit, reference_time=None, window_days=7):
    """New events bypass the background polling count, never the Rubric.

    Input candidates arrive in durable least-recently-checked order. Pending
    research and initial observations remain eligible for bounded background
    retries. Technical rejects are revisited only on a new related event.
    """
    events, background = [], []
    for item in candidates:
        if recent_related_events(item, supporting, reference_time=reference_time, window_days=window_days):
            events.append(item)
        elif item.status != "rejected":
            background.append(item)
    return events + background[:max(limit, 0)], {"event_recalled_routes": len(events),
        "background_selected": min(len(background), max(limit, 0)),
        "background_unselected": max(len(background) - max(limit, 0), 0)}


