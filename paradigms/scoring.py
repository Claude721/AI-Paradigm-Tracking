"""Technical eligibility and advisory public attention are separate judgments."""

from __future__ import annotations

import math

import config
from .models import (
    ORIGIN_EVIDENCE_TYPES,
    EvidenceType,
    ParadigmCandidate,
    is_verified_substantive_discussion,
    nonnegative_number,
    delivery_researcher_profiles,
)
from .reputation import resolve_organization, verified_priority_researcher
from .rubric import finalize_candidate_rubric, substantive_secondary
from .evidence_association import countable_evidence, supported_origins, has_independent_origins, profile_references_origin


def score_candidate(candidate: ParadigmCandidate) -> ParadigmCandidate:
    """用版本化 Rubric 计算最终决策；不再读取模型自报的数字分。"""
    candidate.rejection_reason = ""
    from .propagation import certify_diffusion
    certify_diffusion(candidate)
    _assess_publisher(candidate)
    candidate.momentum_score = _momentum_score(candidate)
    candidate.researcher_score = _researcher_score(candidate)
    candidate.volume_score = _volume_score(candidate)
    gate_passed, admission_reason = _admission_gate(candidate)
    candidate.admission_reason = admission_reason
    assessment = finalize_candidate_rubric(candidate)
    decision = assessment["decision"]
    if decision == "report" and not gate_passed:
        # Only unusable primary material is a veto here, not popularity.
        assessment["decision"] = "observe"
        assessment["decision_reason"] = admission_reason
        decision = "observe"
    if decision == "report":
        candidate.status = "reportable"
    elif decision == "incomplete":
        # 结构不完整或综合器失败是执行状态，不是研究结论。只有
        # pending_deep 会被下轮的完整增强/综合链路恢复；若写成一个独立
        # 状态，它反而会落入“历史讨论刷新”并永久失去重试机会。
        candidate.status = "pending_deep"
        candidate.rejection_reason = assessment["decision_reason"]
    elif decision == "observe":
        candidate.status = "observe"
        candidate.rejection_reason = assessment["decision_reason"]
    else:
        candidate.status = "rejected"
        candidate.rejection_reason = assessment["decision_reason"]
    return candidate


def is_reportable(candidate: ParadigmCandidate) -> bool:
    return (
        not candidate.rejection_reason
        and candidate.rubric_assessment.get("decision") == "report"
    )


def effective_solidity_score(candidate: ParadigmCandidate) -> float:
    evidence = [item for item in candidate.evidence if countable_evidence(candidate, item)]
    types = [item.evidence_type for item in evidence]
    independent_replications = sum(
        item.evidence_type == EvidenceType.INDEPENDENT_REPLICATION
        and item.raw.get("independence") == "independent"
        for item in evidence
    )
    implementations = sum(
        item.evidence_type == EvidenceType.IMPLEMENTATION
        and _counts_as_momentum_evidence(item, route_key=candidate.key)
        for item in evidence
    )
    origins = sum(item in ORIGIN_EVIDENCE_TYPES for item in types)
    review_replies = sum(
        int(_numeric_value(item.metrics.get("review_replies", 0)))
        for item in evidence
        if item.source == "openreview"
    )
    bonus = (
        min(independent_replications, 2) * 1.25
        + min(implementations, 2) * 0.25
        + min(max(origins - 1, 0), 2) * 0.5
        + (0.5 if review_replies > 0 else 0.0)
    )
    return min(candidate.solidity_score + bonus, 10.0)


def _momentum_score(candidate: ParadigmCandidate) -> float:
    """跨平台与独立响应优先；使用对数，避免大平台绝对量碾压早期信号。"""
    from .propagation import verified_diffusion
    countable = [
        item
        for item in candidate.evidence
        if countable_evidence(candidate, item) and (_counts_as_momentum_evidence(item, route_key=candidate.key)
                                                   or verified_diffusion(candidate, item))
    ]
    sources = {item.source for item in countable}
    types = {item.evidence_type for item in countable}
    independent = sum(
        1
        for item in countable
        if (
            item.evidence_type
            in {EvidenceType.INDEPENDENT_REPLICATION, EvidenceType.PRODUCT_ADOPTION}
            and item.raw.get("independence") == "independent"
            or (
                item.evidence_type == EvidenceType.IMPLEMENTATION
                and item.raw.get("independence") == "independent"
            )
            or is_verified_substantive_discussion(item, route_key=candidate.key)
        )
    )
    engagement = 0.0
    for item in countable:
        for key in (
            "citations",
            "upvotes",
            "comments",
            "stars",
            "forks",
            "likes",
            "retweets",
            "reposts",
            "replies",
            "score",
        ):
            engagement += nonnegative_number(item.metrics.get(key, 0))

    cross_platform = min(len(sources), 4) * 1.5
    evidence_diversity = min(len(types), 4) * 0.75
    independent_signal = min(independent, 4) * 0.75
    weak_volume_signal = min(math.log1p(engagement) / 3.0, 1.0)
    return min(cross_platform + evidence_diversity + independent_signal + weak_volume_signal, 10.0)


def _researcher_score(candidate: ParadigmCandidate) -> float:
    if not candidate.researchers:
        return 4.0 if candidate.publisher_tier == "established" else 0.0
    consistency = max(
        (profile.trajectory_consistency for profile in candidate.researchers),
        default=0.0,
    )
    history_bonus = min(
        max((len(profile.representative_works) for profile in candidate.researchers), default=0),
        4,
    ) * 0.5
    identity_bonus = 1.0 if any(p.identifiers for p in candidate.researchers) else 0.0
    publisher_bonus = 4.0 if candidate.publisher_tier == "established" else 2.0
    return min(consistency + history_bonus + identity_bonus + publisher_bonus, 10.0)


def _volume_score(candidate: ParadigmCandidate) -> float:
    # 绝对量最多贡献总分 5%，只作佐证，不作门槛。
    countable = sum(
        countable_evidence(candidate, item) and _counts_as_momentum_evidence(item, route_key=candidate.key)
        for item in candidate.evidence
    )
    return min(math.log2(countable + 1) * 2.5, 10.0)


def _assess_publisher(candidate: ParadigmCandidate) -> None:
    established = []
    verified = []
    candidate.is_formal_technical_report = any(
        item.raw.get("origin_kind") == "technical_report"
        for item in candidate.evidence
        if item.evidence_type in ORIGIN_EVIDENCE_TYPES and countable_evidence(candidate, item)
    )
    for item in candidate.evidence:
        if item.evidence_type not in ORIGIN_EVIDENCE_TYPES or not countable_evidence(candidate, item):
            continue  # A commentator/implementer is not the origin publisher.
        if item.raw.get("publisher_tier") == "established":
            established.append(
                str(item.raw.get("publisher_evidence") or item.organization or item.source)
            )
        elif item.raw.get("publisher_tier") == "verified":
            verified.append(
                str(item.raw.get("publisher_evidence") or item.organization or item.source)
            )
        organization = resolve_organization(item.organization)
        if organization and organization["tier"] == "established":
            established.append(str(organization["name"]))
        elif organization:
            verified.append(f"{organization['name']}：监测组织，尚不自动背书")
        # A free-text affiliation alone is not a verified publisher anchor.
    ready_profiles = delivery_researcher_profiles(candidate.researchers)
    for profile in candidate.researchers:
        if not profile_references_origin(candidate, profile):
            continue
        priority_researcher = verified_priority_researcher(profile)
        if priority_researcher:
            verified.append(f"{profile.name}：重点研究者身份已由公开 ID/主页核验；长期方向为{priority_researcher['focus']}")
        if profile not in ready_profiles:
            continue  # A model-supplied affiliation cannot certify a publisher.
        organization = resolve_organization(profile.current_affiliation)
        if organization and organization["tier"] == "established":
            established.append(str(organization["name"]))
        elif organization:
            verified.append(f"{organization['name']}：监测组织，研究者身份待联合核验")
        if not priority_researcher and (
            profile.current_affiliation
            and profile.trajectory_consistency >= 7
            and len(profile.representative_works) >= 3
            and (profile.profile_urls or profile.identifiers)
        ):
            verified.append(
                f"{profile.name}：{profile.current_affiliation}，研究轨迹已核验"
            )
    candidate.publisher_evidence = list(
        dict.fromkeys(value for value in [*established, *verified] if value)
    )
    if established:
        candidate.publisher_tier = "established"
    elif verified:
        candidate.publisher_tier = "verified"
    else:
        candidate.publisher_tier = "unknown"


def _matches_established_organization(value: str) -> bool:
    organization = resolve_organization(value)
    return bool(organization and organization["tier"] == "established")


def _admission_gate(candidate: ParadigmCandidate) -> tuple[bool, str]:
    from .publication import is_nonresearch_page
    primary = [item for item in candidate.evidence if item.evidence_type in ORIGIN_EVIDENCE_TYPES]
    if primary and all(is_nonresearch_page(item.url) or is_nonresearch_page(str(item.raw.get("canonical_source_url", ""))) for item in primary):
        return False, "原点链接均为账户、登录或政策页面，不能作为技术事实来源，保留观察"
    from .propagation import diffusion_items
    attention = len(diffusion_items(candidate))
    return True, (f"已核验 {attention} 条当前工作公开传播记录；传播与发布者势能仅作辅助，不代替技术验证"
                  if attention else "当前传播证据有限或覆盖不足；不因低声量、未知机构或缺少 KOL 转发否决技术资格")


def _numeric_value(value: object) -> float:
    """External metrics and restored legacy JSON must not crash admission."""
    return nonnegative_number(value)


def _counts_as_momentum_evidence(item, *, route_key: str) -> bool:
    if item.raw.get("indexed_discovery_only"):
        return False
    if item.raw.get("relationship") == "author_self_release":
        return False
    if item.evidence_type in {
        EvidenceType.COMMUNITY_DISCUSSION,
        EvidenceType.SECONDARY_INTERPRETATION,
    }:
        return is_verified_substantive_discussion(item, route_key=route_key)
    if item.evidence_type in {
        EvidenceType.INDEPENDENT_REPLICATION,
        EvidenceType.PRODUCT_ADOPTION,
    }:
        return item.raw.get("independence") == "independent"
    if item.evidence_type == EvidenceType.IMPLEMENTATION:
        return item.raw.get("independence") in {
            "independent", "official", "publisher"
        }
    return True
