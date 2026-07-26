"""将论文级提取结果聚合成技术范式。"""

from __future__ import annotations

from collections import defaultdict

import config

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
    """在社区搜索和主模型综合前挡住模型自报的局部小改动。"""
    return not initial_gate_reason(item)


def initial_gate_reason(item: ParadigmExtraction) -> str:
    """返回未进入昂贵证据深挖的明确原因；空字符串表示通过。"""
    if not item.is_candidate:
        return item.rejection_reason or "机制抽取 Agent 判断不构成范式候选"
    formal_report = (
        item.evidence.raw.get("origin_kind") == "technical_report"
        and item.evidence.raw.get("publisher_tier") == "established"
    )
    if formal_report:
        return ""
    if item.novelty_score < config.PARADIGM_MIN_NOVELTY:
        return "新颖性没有跨过内部技术硬门槛"
    if item.scope_score < config.PARADIGM_MIN_SCOPE:
        return "技术外延过窄，仍是现有范式下的局部改进"
    if item.incremental_penalty >= 7 and item.scope_score < 8:
        return "增量改动惩罚过高，且没有足够大的能力边界变化"
    if not item.canonical_name:
        return "没有形成可归一的技术路线名称"
    if not item.mechanism:
        return "没有抽取出可复核的新机制"
    if not item.problem_shift:
        return "没有说明问题定义或能力边界发生了什么变化"
    return ""


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
        key=lambda item: item.novelty_score + item.solidity_score + item.scope_score,
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
        lineage_parent=lead.lineage_parent,
        lineage_path=[value for value in [lead.lineage_parent, lead.canonical_name] if value],
        keywords=sorted({word for item in items for word in item.keywords}),
        evidence=evidence,
        novelty_score=max(item.novelty_score for item in items),
        solidity_score=max(item.solidity_score for item in items),
        scope_score=max(item.scope_score for item in items),
        incremental_penalty=min(item.incremental_penalty for item in items),
    )
