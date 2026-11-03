"""An evidence ledger for editorial attention, never technical eligibility.

One ordinary share is useful, not a trend. Multiple adapters for one URL are
one event, not multiple actors. Missing metrics stay missing. No API/model I/O.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit
import math

from .evidence_association import countable_evidence
from .models import (EvidenceType, _evidence_datetime, is_verified_substantive_discussion,
                     nonnegative_number, _canonical_material_url)
from .propagation import verified_diffusion
from runtime_clock import research_now


UPTAKE_TYPES = {EvidenceType.PEER_REVIEW, EvidenceType.INDEPENDENT_REPLICATION,
    EvidenceType.IMPLEMENTATION, EvidenceType.CITATION, EvidenceType.COMMUNITY_DISCUSSION,
    EvidenceType.SECONDARY_INTERPRETATION, EvidenceType.PRODUCT_ADOPTION}
METRICS = {"stars", "forks", "citations", "score", "upvotes", "comments", "likes",
           "retweets", "reposts", "replies"}


def momentum_evidence(candidate):
    """Keep qualified records, including modest/negative discussion evidence."""
    selected = []
    for item in candidate.evidence:
        if (not countable_evidence(candidate, item) or item.evidence_type not in UPTAKE_TYPES
                or item.raw.get("indexed_discovery_only")
                or not _canonical_material_url(item.url)):
            continue
        date = _evidence_datetime(item.published_at)
        if date and date > research_now():
            continue
        visible = verified_diffusion(candidate, item)
        if item.raw.get("relationship") == "author_self_release" and not visible:
            continue
        if item.evidence_type in {EvidenceType.COMMUNITY_DISCUSSION, EvidenceType.SECONDARY_INTERPRETATION}:
            if not visible and not is_verified_substantive_discussion(item, route_key=candidate.key):
                continue
        if item.evidence_type in {EvidenceType.INDEPENDENT_REPLICATION, EvidenceType.PRODUCT_ADOPTION}:
            if item.raw.get("independence") != "independent":
                continue
        if item.evidence_type == EvidenceType.IMPLEMENTATION:
            if item.raw.get("independence") not in {"independent", "official", "publisher"}:
                continue
        selected.append(item)
    return selected


def _event_url(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(),
                      parsed.path.rstrip("/"), parsed.query, ""))


def _kind(candidate, item):
    if (is_verified_substantive_discussion(item, route_key=candidate.key)
            or item.evidence_type in {EvidenceType.IMPLEMENTATION, EvidenceType.INDEPENDENT_REPLICATION,
                                      EvidenceType.PRODUCT_ADOPTION}
            and item.raw.get("independence") == "independent"):
        return "external_action"
    if verified_diffusion(candidate, item):
        return "publisher_exposure" if item.raw.get("relationship") == "author_self_release" else "public_attention"
    if item.evidence_type == EvidenceType.IMPLEMENTATION:
        return "publisher_activity"
    return "bibliographic_context"


def momentum_assessment(candidate, *, record_limit=8):
    """Source-bound action cards; neither fame nor raw search volume can rank.

    Direction context is separate from work-specific uptake and never changes
    this state. Field growth and actual hiring still need their own units.
    """
    groups = {}
    for item in momentum_evidence(candidate):
        key = _event_url(item.url)
        groups.setdefault(key, []).append(item)
    records = []
    priority = {"external_action": 0, "public_attention": 1, "publisher_activity": 2,
                "publisher_exposure": 3, "bibliographic_context": 4}
    for copies in groups.values():
        # Prefer an audited action over its API mirror, never add counters.
        item = min(copies, key=lambda value: priority[_kind(candidate, value)])
        metrics = {}
        for key in METRICS:
            values = [nonnegative_number(value.metrics[key]) for value in copies
                      if key in value.metrics and type(value.metrics[key]) in {int, float}
                      and math.isfinite(value.metrics[key]) and value.metrics[key] >= 0
                      and _kind(candidate, value) == _kind(candidate, item)]
            if values:
                metrics[key] = max(values)
        records.append({"kind": _kind(candidate, item), "url": item.url,
            "title": item.title, "actors": list(item.authors), "source": item.source,
            "published_at": item.published_at, "metrics": metrics,
            "meaning": "行动或可见度证据；不等于好评、规模化有效或整个方向已成潮流"})
    records.sort(key=lambda record: (priority[record["kind"]], record["url"]))
    kinds = {record["kind"] for record in records}
    state = "external_action" if "external_action" in kinds else "observed_attention" if "public_attention" in kinds else "early_signal"
    labels = {"external_action": "已有外部行动证据", "observed_attention": "已有可核验传播", "early_signal": "早期机制信号"}
    from sources.direction_signal_source import context_for_report
    direction = context_for_report(candidate)
    available = []
    for topic in direction.get("topics", []):
        if topic.get("media"): available.append("媒体融资报道")
        if topic.get("jobs"): available.append("公开招聘需求")
        if topic.get("paper_activity", {}).get("status") == "complete" and "current_count" in topic["paper_activity"]:
            available.append("同口径题名样本提交增量")
    return {"version": "action-ledger-v1", "role": "editorial_not_eligibility",
        "state": state, "label": labels[state],
        "scope": "当前工作及已核验的机制关联；不是全方向声量普查",
        "counts": {kind: sum(record["kind"] == kind for record in records) for kind in priority},
        "records": records[:max(record_limit, 0)],
        "overflow_record_count": max(len(records) - max(record_limit, 0), 0),
        "available_context_dimensions": sorted(set(available)),
        "unmeasured_dimensions": ["实际资本到位/全方向投入变化", "经历史消歧的人才入场"]
            + ([] if "同口径题名样本提交增量" in available else ["同口径论文提交增量"]),
        "boundary": "未系统测量不等于零；单条讨论不自动代表高势能，不能按URL数推断独立团队数"}


def editorial_priority(candidate):
    state = momentum_assessment(candidate, record_limit=0)["state"]
    return ({"external_action": 0, "observed_attention": 1, "early_signal": 2}[state],
            -nonnegative_number(candidate.total_score), candidate.key)
