"""不以热度优先的技术范式评分器。"""

from __future__ import annotations

import math

import config
from .models import EvidenceType, ParadigmCandidate


WEIGHTS = {
    "novelty": 25.0,
    "solidity": 25.0,
    "scope": 20.0,
    "momentum": 15.0,
    "researcher": 10.0,
    "volume": 5.0,
}


def score_candidate(candidate: ParadigmCandidate) -> ParadigmCandidate:
    """原地计算 0-100 分，并应用“无病呻吟”硬门槛。"""
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

    if not candidate.mechanism.strip() or not candidate.problem_shift.strip():
        candidate.rejection_reason = "缺少可辨认的新机制或问题边界变化"
    elif candidate.novelty_score < config.PARADIGM_MIN_NOVELTY:
        candidate.rejection_reason = "新颖性不足，仍是现有范式内的小改动"
    elif candidate.scope_score < config.PARADIGM_MIN_SCOPE:
        candidate.rejection_reason = "外延空间不足，只解决狭窄局部问题"
    elif candidate.incremental_penalty >= 7 and candidate.scope_score < 8:
        candidate.rejection_reason = "增量优化惩罚触发，缺乏能力边界迁移"
    elif candidate.total_score < config.PARADIGM_MIN_SCORE:
        candidate.rejection_reason = "综合证据尚不足，进入观察池"

    if candidate.rejection_reason:
        candidate.status = "rejected" if candidate.total_score < 50 else "observe"
    elif candidate.total_score >= 80:
        candidate.status = "breakout"
    elif candidate.total_score >= 70:
        candidate.status = "emerging"
    else:
        candidate.status = "watch"
    return candidate


def is_reportable(candidate: ParadigmCandidate) -> bool:
    return not candidate.rejection_reason and candidate.total_score >= config.PARADIGM_MIN_SCORE


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
    sources = candidate.evidence_sources
    types = {item.evidence_type for item in candidate.evidence}
    independent = sum(
        1
        for item in candidate.evidence
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
    for item in candidate.evidence:
        for key in ("citations", "upvotes", "comments", "stars", "score"):
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
        return 0.0
    consistency = max(
        (profile.trajectory_consistency for profile in candidate.researchers),
        default=0.0,
    )
    history_bonus = min(
        max((len(profile.representative_works) for profile in candidate.researchers), default=0),
        4,
    ) * 0.5
    identity_bonus = 1.0 if any(p.identifiers for p in candidate.researchers) else 0.0
    return min(consistency + history_bonus + identity_bonus, 10.0)


def _volume_score(candidate: ParadigmCandidate) -> float:
    # 绝对量最多贡献总分 5%，只作佐证，不作门槛。
    return min(math.log2(len(candidate.evidence) + 1) * 2.5, 10.0)
