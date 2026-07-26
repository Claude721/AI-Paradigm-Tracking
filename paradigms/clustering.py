"""将论文级提取结果聚合成技术范式。"""

from __future__ import annotations

from collections import defaultdict

from .models import ParadigmCandidate, ParadigmExtraction, normalize_paradigm_name


def cluster_extractions(extractions: list[ParadigmExtraction]) -> list[ParadigmCandidate]:
    accepted = [item for item in extractions if _passes_initial_gate(item)]
    groups: dict[str, list[ParadigmExtraction]] = defaultdict(list)

    for item in accepted:
        key = item.normalized_key
        best_key = _find_similar_group(item, groups)
        groups[best_key or key].append(item)

    return [_build_candidate(key, items) for key, items in groups.items()]


def _passes_initial_gate(item: ParadigmExtraction) -> bool:
    """只让版本化 Rubric 判定为 deep_dive 的机制进入昂贵证据深挖。"""
    return not initial_gate_reason(item)


def initial_gate_reason(item: ParadigmExtraction) -> str:
    """返回未进入昂贵证据深挖的明确原因；空字符串表示通过。"""
    assessment = item.rubric_assessment
    if not assessment:
        return "缺少版本化 Rubric 结果，不能据模型主观分数进入深挖"
    decision = assessment.get("decision")
    priority_review = (
        decision == "observe"
        and _has_review_priority(item)
    )
    if decision != "deep_dive" and not priority_review:
        return str(
            assessment.get("decision_reason")
            or item.rejection_reason
            or "Rubric 未判定进入深挖"
        )
    if not item.canonical_name:
        return "抽取结构不完整：没有形成可归一的技术路线名称"
    if not item.mechanism:
        return "抽取结构不完整：没有抽取出可复核的新机制"
    if not item.problem_shift:
        return "抽取结构不完整：没有说明问题定义或能力边界发生了什么变化"
    return ""


def is_priority_review(item: ParadigmExtraction) -> bool:
    """高势能但初筛边界不确定的材料进入复核，不代表自动通过最终门槛。"""
    return (
        item.rubric_assessment.get("decision") == "observe"
        and _has_review_priority(item)
        and not initial_gate_reason(item)
    )


def _has_review_priority(item: ParadigmExtraction) -> bool:
    return (
        int(item.evidence.raw.get("origin_priority", 0) or 0) >= 2
        or bool(item.evidence.raw.get("explicit_seed"))
    )


def _find_similar_group(
    item: ParadigmExtraction,
    groups: dict[str, list[ParadigmExtraction]],
) -> str | None:
    item_words = _keyword_set(item)
    item_parent = normalize_paradigm_name(item.lineage_parent)
    for key, members in groups.items():
        representative = members[0]
        if item_parent and normalize_paradigm_name(representative.lineage_parent) not in {"", item_parent}:
            continue
        other_words = _keyword_set(representative)
        union = item_words | other_words
        overlap = len(item_words & other_words) / len(union) if union else 0.0
        if item.normalized_key == key or overlap >= 0.55:
            return key
    return None


def _keyword_set(item: ParadigmExtraction) -> set[str]:
    values = item.keywords or item.canonical_name.split()
    return {
        normalize_paradigm_name(value)
        for value in values
        if len(normalize_paradigm_name(value)) >= 2
    }


def _build_candidate(
    key: str, items: list[ParadigmExtraction]
) -> ParadigmCandidate:
    lead = max(
        items,
        key=lambda item: float(item.rubric_assessment.get("score", 0.0)),
    )
    evidence = []
    seen = set()
    for item in items:
        if item.evidence.fingerprint not in seen:
            evidence.append(item.evidence)
            seen.add(item.evidence.fingerprint)

    return ParadigmCandidate(
        key=key,
        name=lead.canonical_name,
        route_family=lead.route_family,
        thesis=lead.thesis,
        background=lead.background,
        problem_shift=lead.problem_shift,
        design_philosophy=lead.design_philosophy,
        mechanism=lead.mechanism,
        technical_explanation=lead.technical_explanation,
        application_value=lead.application_value,
        why_now=lead.why_now,
        novelty_type=lead.novelty_type,
        innovation_types=sorted(
            {
                innovation_type
                for item in items
                for innovation_type in item.innovation_types
            }
        ),
        lineage_parent=lead.lineage_parent,
        lineage_path=[value for value in [lead.lineage_parent, lead.canonical_name] if value],
        keywords=sorted({word for item in items for word in item.keywords}),
        evidence=evidence,
        novelty_score=max(item.novelty_score for item in items),
        solidity_score=max(item.solidity_score for item in items),
        scope_score=max(item.scope_score for item in items),
        incremental_penalty=min(item.incremental_penalty for item in items),
        screening_rubric=lead.rubric_assessment,
        total_score=float(lead.rubric_assessment.get("score", 0.0)),
    )
