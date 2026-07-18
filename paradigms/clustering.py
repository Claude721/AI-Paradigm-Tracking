"""将论文级提取结果聚合成技术范式。"""

from __future__ import annotations

from collections import defaultdict

from .models import ParadigmCandidate, ParadigmExtraction, normalize_paradigm_name


def cluster_extractions(extractions: list[ParadigmExtraction]) -> list[ParadigmCandidate]:
    accepted = [item for item in extractions if item.is_candidate]
    groups: dict[str, list[ParadigmExtraction]] = defaultdict(list)

    for item in accepted:
        key = item.normalized_key
        best_key = _find_similar_group(item, groups)
        groups[best_key or key].append(item)

    return [_build_candidate(key, items) for key, items in groups.items()]


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
        thesis=lead.thesis,
        problem_shift=lead.problem_shift,
        mechanism=lead.mechanism,
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
