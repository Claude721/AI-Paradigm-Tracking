"""技术质量与发布者/社区势能联合准入的内部评分器。"""

from __future__ import annotations

import math

import config
from .models import EvidenceType, ParadigmCandidate
from .reputation import resolve_organization, verified_priority_researcher
from .rubric import finalize_candidate_rubric, substantive_secondary


def score_candidate(candidate: ParadigmCandidate) -> ParadigmCandidate:
    """用版本化 Rubric 计算最终决策；不再读取模型自报的数字分。"""
    candidate.rejection_reason = ""
    _assess_publisher(candidate)
    candidate.momentum_score = _momentum_score(candidate)
    candidate.researcher_score = _researcher_score(candidate)
    candidate.volume_score = _volume_score(candidate)
    gate_passed, admission_reason = _admission_gate(candidate)
    candidate.admission_reason = admission_reason
    assessment = finalize_candidate_rubric(candidate)
    decision = assessment["decision"]
    if decision == "report" and not gate_passed:
        # 技术 Rubric 与发布者/外部承接是两道不同的门。未知团队不能靠
        # 技术题高分绕过项目的联合准入边界。
        assessment["decision"] = "observe"
        assessment["decision_reason"] = admission_reason
        decision = "observe"
    if decision == "report":
        candidate.status = "reportable"
    elif decision == "incomplete":
        candidate.status = "rubric_incomplete"
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
        if (
            item.evidence_type
            in {
                EvidenceType.INDEPENDENT_REPLICATION,
                EvidenceType.PRODUCT_ADOPTION,
            }
            or (
                item.evidence_type == EvidenceType.IMPLEMENTATION
                and item.raw.get("independence") == "independent"
            )
            or (
                item.evidence_type
                in {
                    EvidenceType.COMMUNITY_DISCUSSION,
                    EvidenceType.SECONDARY_INTERPRETATION,
                }
                and item.source != "huggingface-papers"
                and item.raw.get("relationship") != "author_self_release"
            )
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
        elif item.raw.get("publisher_tier") == "verified":
            verified.append(
                str(item.raw.get("publisher_evidence") or item.organization or item.source)
            )
        organization = resolve_organization(item.organization)
        if organization and organization["tier"] == "established":
            established.append(str(organization["name"]))
        elif organization:
            verified.append(f"{organization['name']}：监测组织，尚不自动背书")
        elif item.organization:
            verified.append(item.organization)
    for profile in candidate.researchers:
        organization = resolve_organization(profile.current_affiliation)
        if organization and organization["tier"] == "established":
            established.append(str(organization["name"]))
        elif organization:
            verified.append(f"{organization['name']}：监测组织，研究者身份待联合核验")
        priority_researcher = verified_priority_researcher(profile)
        if priority_researcher:
            verified.append(
                f"{profile.name}：重点研究者身份已由公开 ID/主页核验；"
                f"长期方向为{priority_researcher['focus']}"
            )
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
    organization = resolve_organization(value)
    return bool(organization and organization["tier"] == "established")


def _admission_gate(candidate: ParadigmCandidate) -> tuple[bool, str]:
    signal_count, engagement, sources, independent = substantive_secondary(candidate)
    official_uptake = any(
        item.evidence_type == EvidenceType.IMPLEMENTATION
        and item.raw.get("independence") in {"official", None}
        and item.raw.get("relationship")
        in {"paper_linked_repository", "name_and_mechanism_match"}
        and (
            float(item.metrics.get("forks", 0) or 0) >= 3
            or float(item.metrics.get("stars", 0) or 0) >= 50
        )
        for item in candidate.evidence
    )
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
        independent
        or signal_count >= 1
        or engagement >= 10
        or official_uptake
    ):
        return True, "研究团队具有可核验势能，且已出现实质外部响应或代码承接"
    verified_people = sum(
        verified_priority_researcher(profile) is not None
        for profile in candidate.researchers
    )
    if candidate.publisher_tier == "verified" and official_uptake:
        if verified_people >= 1:
            return (
                True,
                "关键研究者身份已由公开主页/学术 ID 核验，且官方实现已出现实质代码承接",
            )
    if candidate.publisher_tier == "verified" and verified_people >= 2:
        return (
            True,
            "多位长期前沿研究者的身份与研究方向已核验；技术 Rubric 通过后进入解读",
        )
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
