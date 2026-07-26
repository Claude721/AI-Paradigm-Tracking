"""使用 LLM 从论文/技术博客中抽取“范式假说”，不做热度先验。"""

from __future__ import annotations

import asyncio
import json
import logging

from agents.llm_utils import build_client, parse_json_object
from run_audit import run_audit
from skills.loader import SkillLoader

from .models import ParadigmExtraction, TechnicalEvidence
from .models import ParadigmCandidate, ResearcherProfile
from .rubric import (
    evaluate_rubric,
    legacy_dimension_scores,
    normalize_innovation_types,
    rubric_prompt,
)

logger = logging.getLogger(__name__)


class ParadigmAnalyzer:
    def __init__(self, concurrency: int = 6, client=None, model: str = ""):
        self.concurrency = max(concurrency, 1)
        self.client = client
        self.model = model
        self.skill_loader = SkillLoader()

    def _get_client(self):
        if self.client is None:
            self.client, self.model = build_client("sub")
        return self.client, self.model

    async def run(self, evidence: list[TechnicalEvidence]) -> list[ParadigmExtraction]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def guarded(item: TechnicalEvidence) -> list[ParadigmExtraction]:
            async with semaphore:
                return await self.extract(item)

        batches = await asyncio.gather(*(guarded(item) for item in evidence))
        return [extraction for batch in batches for extraction in batch]

    async def extract(self, evidence: TechnicalEvidence) -> list[ParadigmExtraction]:
        screening_material = evidence.summary
        document_excerpt = str(evidence.raw.get("document_excerpt", ""))
        if document_excerpt:
            screening_material = (
                f"{screening_material}\n\n[官方 HTML 正文节选]\n{document_excerpt}"
            )
        prompt = self.skill_loader.render(
            "paradigm_extraction",
            source=evidence.source,
            title=evidence.title,
            abstract=screening_material[
                : (
                    50_000
                    if evidence.raw.get("origin_kind") == "technical_report"
                    else 10_000
                )
            ],
            authors=", ".join(evidence.authors),
            organization=evidence.organization,
            identifiers=evidence.identifiers,
            origin_kind=evidence.raw.get("origin_kind", "research_paper"),
            frontier_domains=evidence.raw.get("frontier_domains", []),
            publisher_context={
                "organization": evidence.organization,
                "publisher_tier": evidence.raw.get("publisher_tier", "unknown"),
                "publisher_evidence": evidence.raw.get("publisher_evidence", ""),
            },
            rubric_definition=rubric_prompt("screening"),
        )
        last_error: Exception | None = None
        for attempt in range(2):
            response = None
            try:
                client, model = self._get_client()
                repair_note = (
                    ""
                    if attempt == 0
                    else "\n上一轮 JSON 或 Rubric 回答不完整。请重新输出完整 JSON，"
                    "确保 common 与所选 innovation_types 的每一道题都出现一次。"
                )
                response = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt + repair_note}],
                    temperature=0.1,
                    max_tokens=(
                        9000
                        if evidence.raw.get("origin_kind") == "technical_report"
                        else 5000
                    ),
                    response_format={"type": "json_object"},
                )
                payload = parse_json_object(
                    response.choices[0].message.content or "{}"
                )
                hypotheses = payload.get("hypotheses")
                payloads = hypotheses if isinstance(hypotheses, list) else [payload]
                parsed = [
                    self._from_payload(evidence, item)
                    for item in payloads[:6]
                    if isinstance(item, dict)
                ]
                if not parsed:
                    raise ValueError("结果为空")
                incomplete = [
                    item
                    for item in parsed
                    if item.rubric_assessment.get("decision") == "incomplete"
                ]
                if incomplete:
                    raise ValueError(
                        incomplete[0].rubric_assessment.get(
                            "decision_reason", "Rubric 回答不完整"
                        )
                    )
                run_audit.record_llm(
                    stage="paradigm_extraction",
                    role="sub",
                    model=model,
                    subject=evidence.title,
                    response=response,
                )
                return parsed
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "范式抽取第 %s 次失败 [%s]: %s",
                    attempt + 1,
                    evidence.title[:60],
                    exc,
                )
                run_audit.record_llm(
                    stage="paradigm_extraction",
                    role="sub",
                    model=self.model,
                    subject=f"{evidence.title} / attempt-{attempt + 1}",
                    response=response,
                    error=exc,
                )
        return [
            self._failed_extraction(
                evidence, f"抽取失败: {last_error or '未知错误'}"
            )
        ]

    @staticmethod
    def _failed_extraction(
        evidence: TechnicalEvidence, reason: str
    ) -> ParadigmExtraction:
        return ParadigmExtraction(
            evidence=evidence,
            is_candidate=False,
            canonical_name="",
            thesis="",
            problem_shift="",
            mechanism="",
            rejection_reason=reason,
        )

    @staticmethod
    def _from_payload(
        evidence: TechnicalEvidence, payload: dict
    ) -> ParadigmExtraction:
        raw_types = payload.get("innovation_types")
        if not isinstance(raw_types, list):
            raw_types = [payload.get("novelty_type", "other")]
        innovation_types = normalize_innovation_types(raw_types)
        assessment = evaluate_rubric(
            stage="screening",
            innovation_types=innovation_types,
            answers=payload.get("rubric_answers"),
        )
        scores = legacy_dimension_scores(assessment)
        is_candidate = assessment["decision"] == "deep_dive"
        return ParadigmExtraction(
            evidence=evidence,
            is_candidate=is_candidate,
            canonical_name=str(payload.get("canonical_name", "")).strip(),
            route_family=str(payload.get("route_family", "")).strip(),
            thesis=str(payload.get("thesis", "")).strip(),
            background=str(payload.get("background", "")).strip(),
            problem_shift=str(payload.get("problem_shift", "")).strip(),
            design_philosophy=str(payload.get("design_philosophy", "")).strip(),
            mechanism=str(payload.get("mechanism", "")).strip(),
            technical_explanation=str(
                payload.get("technical_explanation", "")
            ).strip(),
            application_value=str(payload.get("application_value", "")).strip(),
            why_now=str(payload.get("why_now", "")).strip(),
            novelty_type=innovation_types[0],
            innovation_types=innovation_types,
            lineage_parent=str(payload.get("lineage_parent", "")).strip(),
            keywords=_string_list(payload.get("keywords")),
            claimed_results=_string_list(payload.get("claimed_results")),
            rubric_assessment=assessment,
            novelty_score=scores["novelty_score"],
            solidity_score=scores["solidity_score"],
            scope_score=scores["scope_score"],
            incremental_penalty=scores["incremental_penalty"],
            rejection_reason=(
                "" if is_candidate else assessment["decision_reason"]
            ),
        )


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


class ResearcherTrajectoryAnalyzer:
    """用代表作验证研究连续性，不推测作者创业意愿。"""

    def __init__(self, concurrency: int = 4, client=None, model: str = ""):
        self.concurrency = max(concurrency, 1)
        self.client = client
        self.model = model
        self.skill_loader = SkillLoader()

    def _get_client(self):
        if self.client is None:
            self.client, self.model = build_client("main")
        return self.client, self.model

    async def run(
        self, candidates: list[ParadigmCandidate]
    ) -> list[ParadigmCandidate]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def analyze(candidate, profile):
            async with semaphore:
                await self._analyze_one(candidate, profile)

        await asyncio.gather(
            *(
                analyze(candidate, profile)
                for candidate in candidates
                for profile in candidate.researchers[:3]
            )
        )
        return candidates

    async def _analyze_one(
        self, candidate: ParadigmCandidate, profile: ResearcherProfile
    ) -> None:
        if not profile.representative_works and not profile.public_bio_excerpt:
            profile.research_trajectory = "公开学术资料不足，暂不判断研究连续性。"
            return
        prompt = self.skill_loader.render(
            "researcher_trajectory",
            paradigm_name=candidate.name,
            mechanism=candidate.mechanism,
            keywords=", ".join(candidate.keywords),
            researcher_name=profile.name,
            affiliation=profile.current_affiliation,
            works=profile.representative_works,
            contacts=profile.public_contacts,
            search_notes=profile.contact_search_notes,
            public_bio=profile.public_bio_excerpt,
            prior_affiliations=profile.prior_affiliations,
        )
        response = None
        try:
            client, model = self._get_client()
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=1100,
                response_format={"type": "json_object"},
            )
            payload = parse_json_object(response.choices[0].message.content or "{}")
            background = str(payload.get("background_summary", "")).strip()
            if background:
                profile.background_summary = background
            profile.research_trajectory = str(
                payload.get("trajectory_summary", "")
            ).strip()
            profile.key_person_reason = str(
                payload.get("key_person_reason", "")
            ).strip()
            try:
                profile.trajectory_consistency = min(
                    max(float(payload.get("trajectory_consistency", 0)), 0.0),
                    10.0,
                )
            except (TypeError, ValueError):
                profile.trajectory_consistency = 0.0
            note = str(payload.get("current_role_note", "")).strip()
            if note:
                profile.research_trajectory = (
                    f"{profile.research_trajectory} 当前状态：{note}"
                ).strip()
            run_audit.record_llm(
                stage="researcher_trajectory",
                role="main",
                model=model,
                subject=f"{candidate.name} / {profile.name}",
                response=response,
            )
        except Exception as exc:
            logger.warning("研究轨迹分析失败 [%s]: %s", profile.name, exc)
            run_audit.record_llm(
                stage="researcher_trajectory",
                role="main",
                model=self.model,
                subject=f"{candidate.name} / {profile.name}",
                response=response,
                error=exc,
            )
            profile.research_trajectory = "研究轨迹自动分析失败；保留代表作供人工复核。"


class ParadigmSynthesizer:
    """在论文聚类和外部增强之后，形成证据化的范式级结论。"""

    def __init__(self, concurrency: int = 3, client=None, model: str = ""):
        self.concurrency = max(concurrency, 1)
        self.client = client
        self.model = model
        self.skill_loader = SkillLoader()

    def _get_client(self):
        if self.client is None:
            self.client, self.model = build_client("main")
        return self.client, self.model

    async def run(
        self, candidates: list[ParadigmCandidate]
    ) -> list[ParadigmCandidate]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def synthesize(item: ParadigmCandidate):
            async with semaphore:
                await self._synthesize_one(item)

        await asyncio.gather(*(synthesize(item) for item in candidates))
        return candidates

    async def _synthesize_one(self, candidate: ParadigmCandidate) -> None:
        ordered_evidence = sorted(
            enumerate(candidate.evidence),
            key=lambda pair: bool(pair[1].raw.get("historical")),
        )
        evidence_payload = [
            {
                "index": original_index,
                "fingerprint": item.fingerprint,
                "type": item.evidence_type.value,
                "source": item.source,
                "title": item.title,
                "url": item.url,
                # 原点材料需要保留足够的机制细节，社区证据只保留支持
                # 相关性判断所需的短摘要。该扩容只发生在通过技术门槛
                # 的少量候选上，不增加全量论文抽取成本。
                "summary": item.summary[
                    : (
                        2400
                        if item.evidence_type.value
                        in {"primary_paper", "technical_blog"}
                        else 700
                    )
                ],
                "document_excerpt": str(
                    item.raw.get("document_excerpt", "")
                )[:12_000],
                "document_source_url": item.raw.get(
                    "document_source_url", ""
                ),
                "affiliations": item.raw.get("affiliations", []),
                "project_urls": item.raw.get("project_urls", []),
                "author_roles": item.raw.get("author_roles", {}),
                "authors": item.authors,
                "metrics": item.metrics,
                "historical": bool(item.raw.get("historical")),
                "relationship_hint": item.raw.get("relationship", ""),
            }
            for original_index, item in ordered_evidence[:24]
        ]
        prompt = self.skill_loader.render(
            "paradigm_synthesis",
            provisional_name=candidate.name,
            route_family=candidate.route_family,
            provisional_thesis=candidate.thesis,
            background=candidate.background,
            problem_shift=candidate.problem_shift,
            design_philosophy=candidate.design_philosophy,
            mechanism=candidate.mechanism,
            technical_explanation=candidate.technical_explanation,
            mental_model=json.dumps(candidate.mental_model, ensure_ascii=False),
            innovation_types=json.dumps(
                candidate.innovation_types or [candidate.novelty_type or "other"],
                ensure_ascii=False,
            ),
            screening_rubric=json.dumps(
                candidate.screening_rubric, ensure_ascii=False
            ),
            rubric_definition=rubric_prompt("final"),
            mental_model_method=self.skill_loader.load("technical-mental-model"),
            lineage_parent=candidate.lineage_parent,
            evidence=json.dumps(evidence_payload, ensure_ascii=False),
        )
        last_error: Exception | None = None
        for attempt in range(2):
            response = None
            try:
                client, model = self._get_client()
                repair_note = (
                    ""
                    if attempt == 0
                    else "\n上一轮综合结果不完整。请重新输出完整 JSON，补齐所选"
                    " innovation_types 的全部 Rubric 题；心智模型必须先给主观察"
                    "坐标与低分辨率运行图，再用至少两个 resolution_ladder 节点"
                    "逐层纠偏和提高分辨率。"
                )
                response = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt + repair_note}],
                    temperature=0.1,
                    max_tokens=5600,
                    response_format={"type": "json_object"},
                )
                payload = parse_json_object(
                    response.choices[0].message.content or "{}"
                )
                self._apply_synthesis_payload(candidate, payload)
                run_audit.record_llm(
                    stage="paradigm_synthesis",
                    role="main",
                    model=model,
                    subject=candidate.name,
                    response=response,
                )
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "范式综合第 %s 次失败 [%s]: %s",
                    attempt + 1,
                    candidate.name,
                    exc,
                )
                run_audit.record_llm(
                    stage="paradigm_synthesis",
                    role="main",
                    model=self.model,
                    subject=f"{candidate.name} / attempt-{attempt + 1}",
                    response=response,
                    error=exc,
                )
        failure = evaluate_rubric(
            stage="final",
            innovation_types=candidate.innovation_types
            or [candidate.novelty_type or "other"],
            answers=[],
        )
        failure["failure_reason"] = f"范式综合失败: {last_error or '未知错误'}"
        failure["decision_reason"] = failure["failure_reason"]
        candidate.rubric_assessment = failure

    @staticmethod
    def _apply_synthesis_payload(
        candidate: ParadigmCandidate, payload: dict[str, object]
    ) -> None:
        for field_name in (
            "name",
            "route_family",
            "thesis",
            "background",
            "problem_shift",
            "design_philosophy",
            "mechanism",
            "technical_explanation",
            "application_value",
            "why_now",
            "evidence_assessment",
            "secondary_discussion_summary",
            "trend_interpretation",
            "marketing_overclaim_risk",
        ):
            value = str(payload.get(field_name, "")).strip()
            if value:
                setattr(candidate, field_name, value)
        questions = _string_list(payload.get("open_questions"))
        if questions:
            candidate.open_questions = questions
        lineage = _string_list(payload.get("lineage_path"))
        if lineage:
            candidate.lineage_path = lineage
        momentum = _string_list(payload.get("objective_momentum_signals"))
        if momentum:
            candidate.objective_momentum_signals = momentum
        mental_model = payload.get("mental_model")
        if isinstance(mental_model, dict):
            candidate.mental_model = {
                str(key): value
                for key, value in mental_model.items()
                if value not in ("", [], {}, None)
            }
        _validate_mental_model(candidate.mental_model)

        raw_types = payload.get("innovation_types")
        if not isinstance(raw_types, list):
            raw_types = candidate.innovation_types or [
                candidate.novelty_type or "other"
            ]
        assessment = evaluate_rubric(
            stage="final",
            innovation_types=raw_types,
            answers=payload.get("rubric_answers"),
        )
        candidate.innovation_types = assessment["innovation_types"]
        candidate.novelty_type = candidate.innovation_types[0]
        candidate.rubric_assessment = assessment
        if assessment["decision"] == "incomplete":
            raise ValueError(assessment["decision_reason"])

        excluded = {
            int(value)
            for value in payload.get("excluded_evidence_indices", [])
            if str(value).isdigit()
        }
        if excluded:
            candidate.evidence = [
                item
                for index, item in enumerate(candidate.evidence)
                if index not in excluded
            ]


def _validate_mental_model(mental_model: dict[str, object]) -> None:
    """拒绝模块清单式降级稿，要求同一运行图上的递进式理解。"""
    required_text = (
        "observation_axis",
        "low_resolution_model",
        "decisive_intervention",
        "minimal_simulation",
        "counterfactual_and_boundary",
    )
    missing = [
        name
        for name in required_text
        if not str(mental_model.get(name, "")).strip()
    ]
    if missing:
        raise ValueError(
            "技术心智模型缺少关键部件: "
            + "、".join(missing)
            + "；应重试而不是生成模块清单式降级档案"
        )

    ladder = mental_model.get("resolution_ladder")
    if not isinstance(ladder, list) or not 2 <= len(ladder) <= 6:
        raise ValueError(
            "resolution_ladder 必须包含 2 到 6 个真正改变整体理解的下钻节点"
        )
    allowed_status = {
        "source_fact",
        "interpretive_compression",
        "inference",
        "unknown",
    }
    for index, node in enumerate(ladder, start=1):
        if not isinstance(node, dict):
            raise ValueError(f"resolution_ladder 第 {index} 项不是结构化节点")
        missing_fields = [
            field
            for field in ("question", "answer", "evidence_status", "model_update")
            if not str(node.get(field, "")).strip()
        ]
        if missing_fields:
            raise ValueError(
                f"resolution_ladder 第 {index} 项缺少: "
                + "、".join(missing_fields)
            )
        if str(node["evidence_status"]).strip() not in allowed_status:
            raise ValueError(
                f"resolution_ladder 第 {index} 项 evidence_status 无效"
            )

    training = mental_model.get("training_causal_chain")
    runtime = mental_model.get("runtime_causal_chain")
    has_training = isinstance(training, list) and any(
        str(item).strip() for item in training
    )
    has_runtime = isinstance(runtime, list) and any(
        str(item).strip() for item in runtime
    )
    if not has_training and not has_runtime:
        raise ValueError("技术心智模型没有闭合训练或运行侧的任何一条因果链")
