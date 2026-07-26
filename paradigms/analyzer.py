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
        prompt = self.skill_loader.render(
            "paradigm_extraction",
            source=evidence.source,
            title=evidence.title,
            abstract=evidence.summary[
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
            publisher_context={
                "organization": evidence.organization,
                "publisher_tier": evidence.raw.get("publisher_tier", "unknown"),
                "publisher_evidence": evidence.raw.get("publisher_evidence", ""),
            },
        )
        response = None
        try:
            client, model = self._get_client()
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=(
                    6200
                    if evidence.raw.get("origin_kind") == "technical_report"
                    else 3000
                ),
                response_format={"type": "json_object"},
            )
            payload = parse_json_object(response.choices[0].message.content or "{}")
            hypotheses = payload.get("hypotheses")
            payloads = hypotheses if isinstance(hypotheses, list) else [payload]
            parsed = [
                self._from_payload(evidence, item)
                for item in payloads[:6]
                if isinstance(item, dict)
            ]
            run_audit.record_llm(
                stage="paradigm_extraction",
                role="sub",
                model=model,
                subject=evidence.title,
                response=response,
            )
            return parsed or [self._failed_extraction(evidence, "抽取失败: 结果为空")]
        except Exception as exc:
            logger.warning("范式抽取失败 [%s]: %s", evidence.title[:60], exc)
            run_audit.record_llm(
                stage="paradigm_extraction",
                role="sub",
                model=self.model,
                subject=evidence.title,
                response=response,
                error=exc,
            )
            return [self._failed_extraction(evidence, f"抽取失败: {exc}")]

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
        def number(name: str) -> float:
            try:
                return min(max(float(payload.get(name, 0)), 0.0), 10.0)
            except (TypeError, ValueError):
                return 0.0

        candidate_value = payload.get("is_candidate", False)
        is_candidate = candidate_value is True or str(candidate_value).lower() == "true"
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
            novelty_type=str(payload.get("novelty_type", "")).strip(),
            lineage_parent=str(payload.get("lineage_parent", "")).strip(),
            keywords=_string_list(payload.get("keywords")),
            claimed_results=_string_list(payload.get("claimed_results")),
            novelty_score=number("novelty_score"),
            solidity_score=number("solidity_score"),
            scope_score=number("scope_score"),
            incremental_penalty=number("incremental_penalty"),
            rejection_reason=str(payload.get("rejection_reason", "")).strip(),
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
            lineage_parent=candidate.lineage_parent,
            evidence=json.dumps(evidence_payload, ensure_ascii=False),
        )
        response = None
        try:
            client, model = self._get_client()
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=3800,
                response_format={"type": "json_object"},
            )
            payload = parse_json_object(response.choices[0].message.content or "{}")
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
            for payload_name, attribute in (
                ("scope_reassessment", "scope_score"),
                ("solidity_reassessment", "solidity_score"),
            ):
                try:
                    reassessed = min(
                        max(float(payload.get(payload_name)), 0.0), 10.0
                    )
                except (TypeError, ValueError):
                    continue
                setattr(candidate, attribute, min(getattr(candidate, attribute), reassessed))
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
            run_audit.record_llm(
                stage="paradigm_synthesis",
                role="main",
                model=model,
                subject=candidate.name,
                response=response,
            )
        except Exception as exc:
            logger.warning("范式综合失败 [%s]: %s", candidate.name, exc)
            run_audit.record_llm(
                stage="paradigm_synthesis",
                role="main",
                model=self.model,
                subject=candidate.name,
                response=response,
                error=exc,
            )
