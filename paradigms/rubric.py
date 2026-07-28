"""版本化、可审计的技术范式 Rubric 计算引擎。

模型只负责为离散问题选择答案并给出可见证据；本模块负责校验答案、
确定性计分、生成阶段决策，以及补充不依赖模型主观判断的外部证据题。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import config

from .models import EvidenceType, ParadigmCandidate


MODEL_SOURCE = "model"
OBJECTIVE_SOURCE = "objective"


@lru_cache(maxsize=8)
def _load_path(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate_rubric(payload)
    return payload


def load_rubric(path: Path | str | None = None) -> dict[str, Any]:
    target = Path(path or config.PARADIGM_RUBRIC_PATH).expanduser().resolve()
    return _load_path(str(target))


def rubric_prompt(
    stage: str,
    innovation_types: Iterable[str] | None = None,
) -> str:
    """返回给 Agent 的紧凑 Rubric；不包含程序自动回答的客观题。"""
    rubric = load_rubric()
    requested_types = (
        [innovation_types]
        if isinstance(innovation_types, str)
        else innovation_types
    )
    selected_types = (
        normalize_innovation_types(requested_types, rubric)
        if requested_types is not None
        else list(rubric["type_criteria"])
    )
    payload = {
        "version": rubric["version"],
        "stage": stage,
        "instructions": (
            "先选择一个或多个 innovation_types，再只回答 common 和所选类型的题。"
            "每题必须使用该题 options 中的键；无法从材料确认时回答 unknown。"
            "evidence 写支持该选择的中文事实或实验，不得只写结论，不要输出任何数字分数。"
        ),
        "innovation_types": selected_types,
        "common_criteria": [
            _prompt_criterion(item)
            for item in rubric["common_criteria"]
            if stage in item["stages"]
        ],
        "type_criteria": {
            name: [
                _prompt_criterion(item)
                for item in criteria
                if stage in item["stages"]
            ]
            for name, criteria in rubric["type_criteria"].items()
            if name in selected_types
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def evaluate_rubric(
    *,
    stage: str,
    innovation_types: Iterable[str] | None,
    answers: Iterable[dict[str, Any]] | None,
    objective_answers: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """按 Rubric 计算结果；未回答或无证据的题按 unknown 计零分。"""
    rubric = load_rubric()
    selected_types = normalize_innovation_types(innovation_types, rubric)
    criteria = _applicable_criteria(rubric, stage, selected_types)
    if objective_answers is not None:
        criteria.extend(
            item
            for item in rubric.get("objective_criteria", [])
            if stage in item["stages"]
        )

    supplied = {
        str(item.get("criterion_id", "")).strip(): item
        for item in [*(answers or []), *(objective_answers or [])]
        if isinstance(item, dict) and str(item.get("criterion_id", "")).strip()
    }
    normalized_answers: list[dict[str, Any]] = []
    dimension_totals: dict[str, list[float]] = {}
    earned = 0.0
    maximum = 0.0
    supplied_count = 0
    evidenced_count = 0

    for criterion in criteria:
        criterion_id = criterion["id"]
        raw = supplied.get(criterion_id, {})
        option = str(raw.get("answer", "unknown")).strip()
        evidence = str(raw.get("evidence", "")).strip()
        allowed = criterion["options"]
        if option not in allowed:
            option = "unknown" if "unknown" in allowed else next(iter(allowed))
            evidence = ""
        if criterion_id in supplied:
            supplied_count += 1
        # 非 unknown 的模型判断必须给出可见依据；客观题由程序生成依据。
        if (
            option not in {"unknown", "none", "none_or_unsearched"}
            and not evidence
        ):
            option = "unknown" if "unknown" in allowed else min(
                allowed, key=allowed.get
            )
        if evidence:
            evidenced_count += 1

        weight = float(criterion["weight"])
        value = float(allowed[option])
        points = weight * value
        earned += points
        maximum += weight
        dimension = str(criterion["dimension"])
        bucket = dimension_totals.setdefault(dimension, [0.0, 0.0])
        bucket[0] += points
        bucket[1] += weight
        normalized_answers.append(
            {
                "criterion_id": criterion_id,
                "dimension": dimension,
                "question": criterion["question"],
                "answer": option,
                "evidence": evidence,
                "weight": weight,
                "value": value,
                "points": round(points, 3),
                "source": (
                    OBJECTIVE_SOURCE
                    if criterion_id
                    in {
                        item["id"]
                        for item in rubric.get("objective_criteria", [])
                    }
                    else MODEL_SOURCE
                ),
            }
        )

    score = round(100.0 * earned / maximum, 1) if maximum else 0.0
    answer_coverage = round(supplied_count / len(criteria), 3) if criteria else 0.0
    evidence_coverage = (
        round(evidenced_count / len(criteria), 3) if criteria else 0.0
    )
    dimension_scores = {
        dimension: round(100.0 * values[0] / values[1], 1)
        for dimension, values in dimension_totals.items()
        if values[1]
    }
    decision, threshold, minimum_coverage = _decision(
        rubric, stage, score, answer_coverage
    )
    blockers = _blockers(normalized_answers)
    return {
        "version": rubric["version"],
        "stage": stage,
        "innovation_types": selected_types,
        "answers": normalized_answers,
        "dimension_scores": dimension_scores,
        "score": score,
        "answer_coverage": answer_coverage,
        "evidence_coverage": evidence_coverage,
        "decision": decision,
        "threshold": threshold,
        "minimum_answer_coverage": minimum_coverage,
        "decision_reason": _decision_reason(
            decision, score, threshold, answer_coverage, minimum_coverage, blockers
        ),
        "blockers": blockers,
    }


def finalize_candidate_rubric(candidate: ParadigmCandidate) -> dict[str, Any]:
    """把综合 Agent 的技术题与程序计算的外部证据题合成最终 Rubric。"""
    technical = candidate.rubric_assessment or candidate.screening_rubric
    failure_reason = (
        str(technical.get("failure_reason", "")).strip()
        if isinstance(technical, dict)
        else ""
    )
    types = (
        technical.get("innovation_types")
        if isinstance(technical, dict)
        else candidate.innovation_types
    )
    technical_answers = [
        item
        for item in (
            technical.get("answers", []) if isinstance(technical, dict) else []
        )
        if item.get("source", MODEL_SOURCE) == MODEL_SOURCE
    ]
    assessment = evaluate_rubric(
        stage="final",
        innovation_types=types or candidate.innovation_types,
        answers=technical_answers,
        objective_answers=objective_answers(candidate),
    )
    if failure_reason:
        assessment["decision"] = "incomplete"
        assessment["failure_reason"] = failure_reason
        assessment["decision_reason"] = failure_reason
    candidate.rubric_assessment = assessment
    candidate.innovation_types = assessment["innovation_types"]
    candidate.total_score = assessment["score"]
    _inject_legacy_dimension_scores(candidate, assessment)
    return assessment


def objective_answers(candidate: ParadigmCandidate) -> list[dict[str, str]]:
    origins = [
        item
        for item in candidate.evidence
        if item.evidence_type
        in {EvidenceType.PRIMARY_PAPER, EvidenceType.TECHNICAL_BLOG}
    ]
    origin_organizations = {
        item.organization.casefold().strip()
        for item in origins
        if item.organization.strip()
    }
    types = {item.evidence_type for item in candidate.evidence}

    if candidate.is_formal_technical_report and candidate.publisher_tier == "established":
        publisher = "established_formal_release"
    elif candidate.publisher_tier == "established":
        publisher = "established_team"
    elif candidate.publisher_tier == "verified":
        publisher = "verified_team_or_researcher"
    else:
        publisher = "unknown"

    independent_implementation = any(
        item.evidence_type == EvidenceType.IMPLEMENTATION
        and item.raw.get("independence") == "independent"
        for item in candidate.evidence
    )
    official_implementation_uptake = any(
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
    if types & {EvidenceType.INDEPENDENT_REPLICATION, EvidenceType.PRODUCT_ADOPTION}:
        validation = "independent_replication_or_adoption"
    elif independent_implementation:
        validation = "independent_implementation"
    elif official_implementation_uptake:
        validation = "official_implementation_uptake"
    elif len(origins) >= 2 and len(origin_organizations) >= 2:
        validation = "multiple_primary_works"
    elif EvidenceType.IMPLEMENTATION in types:
        validation = "linked_implementation"
    elif origins:
        validation = "primary_claim_only"
    else:
        validation = "none"

    signal_count, engagement, sources, independent = substantive_secondary(candidate)
    indexed = any(
        item.raw.get("indexed_discovery_only") for item in candidate.evidence
    )
    if independent:
        discussion = "independent_high_signal"
    elif (
        signal_count >= config.PARADIGM_MIN_SUBSTANTIVE_DISCUSSIONS
        and len(sources) >= 2
    ):
        discussion = "multiple_substantive_sources"
    elif signal_count >= 1:
        discussion = "one_substantive_source"
    elif indexed:
        discussion = "indexed_hint_only"
    else:
        discussion = "none_or_unsearched"

    verified_profiles = [
        profile
        for profile in candidate.researchers
        if profile.profile_urls or profile.identifiers
    ]
    if any(
        profile.trajectory_consistency >= 7
        and len(profile.representative_works) >= 2
        for profile in verified_profiles
    ):
        researcher = "verified_continuous_trajectory"
    elif any(profile.representative_works for profile in verified_profiles):
        researcher = "verified_identity_and_history"
    elif verified_profiles:
        researcher = "identity_only"
    else:
        researcher = "unknown"

    if len(origins) >= 2 and len(origin_organizations) >= 2:
        convergence = "multiple_independent_origins"
    elif len(origins) >= 2:
        convergence = "multiple_related_origins"
    elif len(origins) == 1:
        convergence = "single_origin"
    else:
        convergence = "none"

    return [
        _objective("publisher_signal", publisher, candidate.publisher_evidence),
        _objective(
            "independent_validation",
            validation,
            [item.url for item in candidate.evidence if item.evidence_type != EvidenceType.COMMUNITY_DISCUSSION],
        ),
        _objective(
            "secondary_discussion",
            discussion,
            [
                f"{len(sources)} 个来源、{signal_count} 条实质信号、互动量 {int(engagement)}"
            ],
        ),
        _objective(
            "researcher_continuity",
            researcher,
            [
                profile.background_summary
                or profile.research_trajectory
                or profile.name
                for profile in candidate.researchers
            ],
        ),
        _objective(
            "route_convergence",
            convergence,
            [item.title for item in origins],
        ),
    ]


def substantive_secondary(
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


def normalize_innovation_types(
    values: Iterable[str] | None,
    rubric: dict[str, Any] | None = None,
) -> list[str]:
    definition = rubric or load_rubric()
    allowed = set(definition["type_criteria"])
    aliases = {
        "agent": "agent_action_loop",
        "agentic": "agent_action_loop",
        "learning": "learning_paradigm",
        "training": "learning_paradigm",
        "model_architecture": "architecture",
        "world_models": "world_model",
        "worldmodel": "world_model",
        "ai4s": "scientific_discovery",
        "science": "scientific_discovery",
    }
    result: list[str] = []
    for value in values or []:
        normalized = aliases.get(str(value).strip().casefold(), str(value).strip().casefold())
        if normalized in allowed and normalized not in result:
            result.append(normalized)
    return result or ["other"]


def legacy_dimension_scores(assessment: dict[str, Any]) -> dict[str, float]:
    dimensions = assessment.get("dimension_scores", {})
    return {
        "novelty_score": float(dimensions.get("novelty", 0.0)) / 10.0,
        "solidity_score": float(dimensions.get("solidity", 0.0)) / 10.0,
        "scope_score": float(dimensions.get("scope", 0.0)) / 10.0,
        "incremental_penalty": max(
            0.0, 10.0 - float(dimensions.get("novelty", 0.0)) / 10.0
        ),
    }


def _inject_legacy_dimension_scores(
    candidate: ParadigmCandidate, assessment: dict[str, Any]
) -> None:
    for name, value in legacy_dimension_scores(assessment).items():
        setattr(candidate, name, value)


def _applicable_criteria(
    rubric: dict[str, Any], stage: str, innovation_types: list[str]
) -> list[dict[str, Any]]:
    criteria = [
        item
        for item in rubric["common_criteria"]
        if stage in item["stages"]
    ]
    seen = {item["id"] for item in criteria}
    for innovation_type in innovation_types:
        for item in rubric["type_criteria"][innovation_type]:
            if stage not in item["stages"] or item["id"] in seen:
                continue
            criteria.append(item)
            seen.add(item["id"])
    return criteria


def _decision(
    rubric: dict[str, Any],
    stage: str,
    score: float,
    answer_coverage: float,
) -> tuple[str, float, float]:
    key = "deep_dive" if stage == "screening" else "report"
    rule = rubric["decisions"][key]
    threshold = float(rule["min_score"])
    minimum_coverage = float(rule.get("minimum_answer_coverage", 0.0))
    if answer_coverage < minimum_coverage:
        return "incomplete", threshold, minimum_coverage
    if score >= threshold:
        return key, threshold, minimum_coverage
    if score >= float(rubric["decisions"]["observe"]["min_score"]):
        return "observe", threshold, minimum_coverage
    return "reject", threshold, minimum_coverage


def _blockers(answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    weak = [
        item
        for item in answers
        if float(item["value"]) <= 0.2
        and item["source"] == MODEL_SOURCE
    ]
    weak.sort(key=lambda item: (-float(item["weight"]), item["criterion_id"]))
    return [
        {
            "criterion_id": item["criterion_id"],
            "question": item["question"],
            "answer": item["answer"],
            "evidence": item["evidence"],
        }
        for item in weak[:4]
    ]


def _decision_reason(
    decision: str,
    score: float,
    threshold: float,
    coverage: float,
    minimum_coverage: float,
    blockers: list[dict[str, Any]],
) -> str:
    if decision == "incomplete":
        return (
            f"Rubric 回答覆盖率 {coverage:.0%}，低于结构完整性要求 "
            f"{minimum_coverage:.0%}；应重试而不是据此淘汰"
        )
    if decision in {"deep_dive", "report"}:
        return f"Rubric 确定性得分 {score:.1f}，达到本阶段阈值 {threshold:.1f}"
    suffix = ""
    if blockers:
        suffix = "；主要未闭合问题：" + "、".join(
            item["question"] for item in blockers[:2]
        )
    return f"Rubric 确定性得分 {score:.1f}，未达到本阶段阈值 {threshold:.1f}{suffix}"


def _objective(
    criterion_id: str, answer: str, evidence: Iterable[str]
) -> dict[str, str]:
    facts = [str(item).strip() for item in evidence if str(item).strip()]
    return {
        "criterion_id": criterion_id,
        "answer": answer,
        "evidence": "；".join(facts[:4]) or "本轮没有可核验的对应证据",
    }


def _prompt_criterion(criterion: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": criterion["id"],
        "dimension": criterion["dimension"],
        "question": criterion["question"],
        "options": list(criterion["options"]),
    }


def _validate_rubric(payload: dict[str, Any]) -> None:
    required = {
        "version",
        "decisions",
        "common_criteria",
        "type_criteria",
        "objective_criteria",
    }
    missing = required - payload.keys()
    if missing:
        raise ValueError(f"Rubric 缺少字段: {sorted(missing)}")
    ids: set[str] = set()
    criteria = [
        *payload["common_criteria"],
        *payload["objective_criteria"],
        *(
            item
            for values in payload["type_criteria"].values()
            for item in values
        ),
    ]
    for item in criteria:
        for key in ("id", "dimension", "stages", "weight", "question", "options"):
            if key not in item:
                raise ValueError(f"Rubric criterion 缺少 {key}: {item}")
        if item["id"] in ids:
            raise ValueError(f"Rubric criterion id 重复: {item['id']}")
        ids.add(item["id"])
        if not item["options"]:
            raise ValueError(f"Rubric criterion 没有选项: {item['id']}")
