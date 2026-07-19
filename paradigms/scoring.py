"""技术质量与发布者/社区势能联合准入的内部评分器。"""

from __future__ import annotations

import math

import config
from .models import EvidenceType, ParadigmCandidate


WEIGHTS = {
    "novelty": 20.0,
    "solidity": 20.0,
    "scope": 15.0,
    "momentum": 20.0,
    "researcher": 20.0,
    "volume": 5.0,
}


def score_candidate(candidate: ParadigmCandidate) -> ParadigmCandidate:
    """原地计算内部评分，并应用“技术 × 发布者 × 传播验证”联合门槛。"""
    candidate.rejection_reason = ""
    _assess_publisher(candidate)
    candidate.momentum_score = _momentum_score(candidate)
    candidate.researcher_score = _researcher_score(candidate)
    candidate.volume_score = _volume_score(candidate)

    candidate.total_score = round(
        WEIGHTS["novelty"] * _unit(candidate.novelty_score)
        + WEIGHTS["solidity"] * _unit(effective_solidity_score(candidate))
        + WEIGHTS["scope"] * _unit(candidate.scope_score)
        + WEIGHTS["momentum"] * _unit(candidate.momentum_score)
        + WEIGHTS["researcher"] * _unit(candidate.researcher_score)
        + WEIGHTS["volume"] * _unit(candidate.volume_score)
        - min(max(candidate.incremental_penalty, 0.0), 10.0) * 3.5,
        1,
    )
    candidate.total_score = max(0.0, min(candidate.total_score, 100.0))

    admission_ok, admission_reason = _admission_gate(candidate)
    candidate.admission_reason = admission_reason

    hard_rejected = False
    if not candidate.mechanism.strip() or not candidate.problem_shift.strip():
        candidate.rejection_reason = "缺少可辨认的新机制或问题边界变化"
        hard_rejected = True
    elif candidate.novelty_score < config.PARADIGM_MIN_NOVELTY:
        candidate.rejection_reason = "新颖性不足，仍是现有范式内的小改动"
        hard_rejected = True
    elif candidate.scope_score < config.PARADIGM_MIN_SCOPE:
        candidate.rejection_reason = "外延空间不足，只解决狭窄局部问题"
        hard_rejected = True
    elif candidate.incremental_penalty >= 7 and candidate.scope_score < 8:
        candidate.rejection_reason = "增量优化惩罚触发，缺乏能力边界迁移"
        hard_rejected = True
    elif not admission_ok:
        candidate.rejection_reason = admission_reason
    elif (
        candidate.total_score < config.PARADIGM_MIN_SCORE
        and not (
            candidate.is_formal_technical_report
            and candidate.publisher_tier == "established"
        )
    ):
        candidate.rejection_reason = "综合证据尚不足，进入观察池"

    if candidate.rejection_reason:
        # 技术边界不成立的工作直接淘汰；技术可能成立、但发布者或外部验证
        # 尚不足的工作留在观察池，等待未来一周的新讨论重新触发评估。
        candidate.status = "rejected" if hard_rejected else "observe"
    elif candidate.total_score >= 80:
        candidate.status = "breakout"
    elif candidate.total_score >= 70:
        candidate.status = "emerging"
    else:
        candidate.status = "watch"
    return candidate


def is_reportable(candidate: ParadigmCandidate) -> bool:
    return not candidate.rejection_reason and (
        candidate.total_score >= config.PARADIGM_MIN_SCORE
        or (
            candidate.is_formal_technical_report
            and candidate.publisher_tier == "established"
        )
    )


def _unit(value: float) -> float:
    return min(max(value, 0.0), 10.0) / 10.0


def effective_solidity_score(candidate: ParadigmCandidate) -> float:
    types = [item.evidence_type for item in candidate.evidence]
    independent_replications = types.count(EvidenceType.INDEPENDENT_REPLICATION)
    implementations = types.count(EvidenceType.IMPLEMENTATION)
    primary_papers = types.count(EvidenceType.PRIMARY_PAPER)
    review_replies = sum(
        int(item.metrics.get("review_replies", 0) or 0)
        for item in candidate.evidence
        if item.source == "openreview"
    )
    bonus = (
        min(independent_replications, 2) * 1.25
        + min(implementations, 2) * 0.25
        + min(max(primary_papers - 1, 0), 2) * 0.5
        + (0.5 if review_replies > 0 else 0.0)
    )
    return min(candidate.solidity_score + bonus, 10.0)


def _momentum_score(candidate: ParadigmCandidate) -> float:
    """跨平台与独立响应优先；使用对数，避免大平台绝对量碾压早期信号。"""
    countable = [
        item
        for item in candidate.evidence
        if not item.raw.get("indexed_discovery_only")
        and item.raw.get("relationship") != "author_self_release"
    ]
    sources = {item.source for item in countable}
    types = {item.evidence_type for item in countable}
    independent = sum(
        1
        for item in countable
        if item.evidence_type
        in {
            EvidenceType.INDEPENDENT_REPLICATION,
            EvidenceType.IMPLEMENTATION,
            EvidenceType.COMMUNITY_DISCUSSION,
            EvidenceType.SECONDARY_INTERPRETATION,
            EvidenceType.CITATION,
        }
        and item.source != "huggingface-papers"
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
            try:
                engagement += max(float(item.metrics.get(key, 0) or 0), 0.0)
            except (TypeError, ValueError):
                continue

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
        not item.raw.get("indexed_discovery_only") for item in candidate.evidence
    )
    return min(math.log2(countable + 1) * 2.5, 10.0)


def _assess_publisher(candidate: ParadigmCandidate) -> None:
    established = []
    verified = []
    candidate.is_formal_technical_report = any(
        item.raw.get("origin_kind") == "technical_report"
        or "technical report" in item.title.casefold()
        for item in candidate.evidence
        if item.evidence_type
        in {EvidenceType.PRIMARY_PAPER, EvidenceType.TECHNICAL_BLOG}
    )
    for item in candidate.evidence:
        if item.raw.get("publisher_tier") == "established":
            established.append(
                str(item.raw.get("publisher_evidence") or item.organization or item.source)
            )
        if _matches_established_organization(item.organization):
            established.append(item.organization)
        elif item.organization:
            verified.append(item.organization)
    for profile in candidate.researchers:
        if _matches_established_organization(profile.current_affiliation):
            established.append(profile.current_affiliation)
        elif (
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
    normalized = value.casefold().strip()
    return bool(normalized) and any(
        organization.casefold() in normalized
        or normalized in organization.casefold()
        for organization in config.ESTABLISHED_RESEARCH_ORGANIZATIONS
        if organization.strip()
    )


def _admission_gate(candidate: ParadigmCandidate) -> tuple[bool, str]:
    signal_count, engagement, sources, independent = _substantive_secondary(candidate)
    official_release = any(
        item.raw.get("publisher_tier") == "established"
        and item.raw.get("origin_kind")
        in {"technical_report", "official_research", "official_model_release"}
        for item in candidate.evidence
    )
    if candidate.is_formal_technical_report and candidate.publisher_tier == "established":
        return True, "已核验的前沿组织发布正式 Technical Report，进入优先解读"
    if candidate.publisher_tier == "established" and official_release:
        return True, "已核验的前沿组织通过官方研究入口发布，进入优先解读"
    if candidate.publisher_tier == "established" and (
        independent or signal_count >= 1 or engagement >= 10
    ):
        return True, "研究团队具有可核验势能，且已出现实质外部响应"
    strong_secondary = independent or (
        signal_count >= config.PARADIGM_MIN_SUBSTANTIVE_DISCUSSIONS
        and len(sources) >= 2
    ) or engagement >= config.PARADIGM_MIN_SECONDARY_ENGAGEMENT
    if strong_secondary:
        return True, "发布者势能尚未充分核验，但已有足够的独立讨论或承接"
    if candidate.publisher_tier == "established":
        return False, "研究团队背景较强，但尚无外部承接，暂留观察池"
    if candidate.publisher_tier == "verified":
        return False, "研究者身份可核验，但尚无足够二次讨论或独立承接，进入观察池"
    return False, "发布者背景未核验且缺少实质二次讨论，暂不占用周报篇幅"


def _substantive_secondary(
    candidate: ParadigmCandidate,
) -> tuple[int, float, set[str], bool]:
    signals = []
    engagement = 0.0
    sources: set[str] = set()
    independent = False
    for item in candidate.evidence:
        qualifies = False
        if item.evidence_type in {
            EvidenceType.INDEPENDENT_REPLICATION,
            EvidenceType.PRODUCT_ADOPTION,
        }:
            qualifies = True
            independent = True
        elif (
            item.evidence_type
            in {EvidenceType.COMMUNITY_DISCUSSION, EvidenceType.SECONDARY_INTERPRETATION}
            and item.source != "huggingface-papers"
            and item.raw.get("relationship") != "author_self_release"
            and not item.raw.get("indexed_discovery_only")
        ):
            qualifies = True
            if (
                item.source == "x-title-search"
                and float(item.metrics.get("author_followers", 0) or 0) >= 10_000
            ):
                independent = True
        elif (
            item.evidence_type == EvidenceType.CITATION
            and float(item.metrics.get("citations", 0) or 0) >= 3
        ):
            qualifies = True
        elif (
            item.evidence_type == EvidenceType.IMPLEMENTATION
            and item.raw.get("independence") == "independent"
        ):
            qualifies = True
            independent = True
        elif item.evidence_type == EvidenceType.IMPLEMENTATION and (
            float(item.metrics.get("forks", 0) or 0) >= 3
            or float(item.metrics.get("stars", 0) or 0) >= 50
        ):
            # 一个真实相关仓库出现明显 fork/star 承接，本身已是社区采用信号；
            # 仍不把它误写成“独立复现”。
            qualifies = True
        if not qualifies:
            continue
        signals.append(item)
        sources.add(item.source)
        for key in (
            "likes",
            "retweets",
            "reposts",
            "comments",
            "replies",
            "score",
            "stars",
            "forks",
            "citations",
        ):
            try:
                engagement += max(float(item.metrics.get(key, 0) or 0), 0.0)
            except (TypeError, ValueError):
                continue
    return len(signals), engagement, sources, independent
