"""使用 LLM 从一手技术材料中抽取“范式假说”，不做热度先验。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import sqlite3
from dataclasses import fields

import config
from agents.llm_utils import build_client, parse_json_object, resolve_model
from run_audit import run_audit
from skills.loader import SkillLoader

from .models import (
    EvidenceType,
    ParadigmCandidate,
    ParadigmExtraction,
    ResearcherProfile,
    TechnicalEvidence,
    key_researcher_profiles,
)
from .evidence_validation import certify_synthesis_discussion
from .async_utils import gather_scoped
from .rubric import (
    evaluate_rubric,
    legacy_dimension_scores,
    normalize_innovation_types,
    rubric_prompt,
)

logger = logging.getLogger(__name__)


def _stage_cache_signature(role, model, prompt, identity):
    resolved = resolve_model(role)
    value = {"version": 1, "role": role, "model": model or resolved.model,
             "provider": resolved.provider, "base_url": resolved.base_url,
             "prompt": prompt, "identity": identity}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ParadigmAnalyzer:
    def __init__(
        self,
        concurrency: int = 6,
        client=None,
        model: str = "",
        *,
        enable_batch_prefilter: bool = False,
        technical_report_mechanism_slice: int = 0,
        stage_cache=None,
        checkpoint_callback=None,
    ):
        self.concurrency = max(concurrency, 1)
        self.client = client
        self.model = model
        self.enable_batch_prefilter = bool(enable_batch_prefilter)
        self.technical_report_mechanism_slice = max(
            int(technical_report_mechanism_slice or 0), 0
        )
        self.skill_loader = SkillLoader()
        self.stage_cache = stage_cache
        self.checkpoint_callback = checkpoint_callback

    def _get_client(self):
        if self.client is None:
            self.client, self.model = build_client("sub")
        return self.client, self.model

    async def run(self, evidence: list[TechnicalEvidence]) -> list[ParadigmExtraction]:
        # Preserve the legacy input-order contract for direct callers. The
        # orchestrator consumes iter_results and commits in completion order.
        finished = {}
        async for item, values in self.iter_results(evidence):
            finished[id(item)] = values
        return [value for item in evidence for value in finished.get(id(item), [])]

    async def iter_results(self, evidence: list[TechnicalEvidence]):
        """Yield finished origins without waiting for the slowest peer.

        The consumer commits each result before requesting another. Cancellation
        closes all outstanding requests; no task may keep spending after timeout.
        """
        semaphore = asyncio.Semaphore(self.concurrency)

        eligible = list(evidence)
        prefiltered: list[ParadigmExtraction] = []
        if self.enable_batch_prefilter:
            ordinary = [item for item in evidence if _should_prefilter_origin(item)]
            bypassed = [
                item for item in evidence if item.fingerprint not in {
                    value.fingerprint for value in ordinary
                }
            ]
            screened = await self._screen_origin_batch(ordinary)
            full_review = []
            for item in ordinary:
                decision = screened.get(item.fingerprint)
                if decision is None:
                    prefiltered.append(
                        self._failed_extraction(
                            item,
                            "原点资格预筛漏回或结构失败；保留待重试",
                        )
                    )
                    continue
                verdict, reason = decision
                item.raw["origin_eligibility_decision"] = verdict
                item.raw["origin_eligibility_reason"] = reason
                if verdict == "screen_out":
                    prefiltered.append(
                        self._eligibility_rejection(item, reason)
                    )
                else:
                    full_review.append(item)
            eligible = [*bypassed, *full_review]

        for value in prefiltered:
            yield value.evidence, [value]

        async def guarded(item: TechnicalEvidence) -> list[ParadigmExtraction]:
            async with semaphore:
                try:
                    values = await self.extract(item)
                except (sqlite3.DatabaseError, OSError):
                    raise
                except Exception as exc:
                    # ``extract`` already converts ordinary LLM/JSON failures
                    # into a retryable placeholder. This outer boundary catches
                    # programming/data edge cases so one malformed origin cannot
                    # cancel healthy peers in the streaming batch.
                    logger.exception(
                        "范式抽取发生未隔离异常 [%s]；仅保留该原点待重试",
                        item.title[:80],
                    )
                    return [
                        self._failed_extraction(
                            item,
                            f"抽取未隔离异常: {type(exc).__name__}",
                        )
                    ]
                if not values:
                    return [self._failed_extraction(item, "抽取结果意外为空")]
                return values

        async def with_identity(item):
            return item, await guarded(item)

        tasks = [asyncio.create_task(with_identity(item)) for item in eligible]
        try:
            for task in asyncio.as_completed(tasks):
                yield await task
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _screen_origin_batch(
        self,
        evidence: list[TechnicalEvidence],
    ) -> dict[str, tuple[str, str]]:
        """Reuse per-origin receipts even if later full reviews time out."""
        cache = getattr(self, "stage_cache", None)
        if cache is None:
            return await self._screen_uncached_origin_batch(evidence)
        signatures, decisions, missing = {}, {}, []
        for item in evidence:
            prompt = self.skill_loader.render("origin_eligibility", origin_records=json.dumps([{
                "fingerprint": item.fingerprint, "source": item.source[:80],
                "title": item.title[:300], "summary": item.summary[:2400],
                "origin_kind": str(item.raw.get("origin_kind", "research_paper")),
                "frontier_domains": item.raw.get("frontier_domains", []),
            }], ensure_ascii=False))
            signature = _stage_cache_signature("sub", self.model, prompt, item.source_revision)
            signatures[item.fingerprint] = signature
            result = cache.cache_get("origin_eligibility", signature)
            if result is not None and result.get("decision") in {"full_review", "screen_out"} and isinstance(result.get("reason"), str) and (result["decision"] != "screen_out" or result["reason"]):
                decisions[item.fingerprint] = (result["decision"], result["reason"])
            else:
                missing.append(item)
        fresh = await self._screen_uncached_origin_batch(missing)
        for fingerprint, (decision, reason) in fresh.items():
            cache.cache_save("origin_eligibility", signatures[fingerprint], {"decision": decision, "reason": reason})
        if decisions:
            run_audit.event("origin_eligibility_cache", "reused", f"复用 {len(decisions)} 条同版本资格预筛，不重做已确认判断")
        return {**decisions, **fresh}

    async def _screen_uncached_origin_batch(
        self, evidence: list[TechnicalEvidence],
    ) -> dict[str, tuple[str, str]]:
        """Conservatively identify records that clearly need no full Rubric.

        Every decision is keyed by the input fingerprint. Unknown identities,
        missing rows and malformed output deliberately stay pending rather than
        being converted into research rejections.
        """

        if not evidence:
            return {}
        records = [
            {
                "fingerprint": item.fingerprint,
                "source": item.source[:80],
                "title": item.title[:300],
                "summary": item.summary[:2400],
                "origin_kind": str(
                    item.raw.get("origin_kind", "research_paper")
                ),
                "frontier_domains": item.raw.get("frontier_domains", []),
            }
            for item in evidence
        ]
        prompt = self.skill_loader.render(
            "origin_eligibility",
            origin_records=json.dumps(records, ensure_ascii=False),
        )
        allowed = {item.fingerprint for item in evidence}
        last_error: Exception | None = None
        for attempt in range(2):
            response = None
            try:
                client, model = self._get_client()
                repair_note = (
                    ""
                    if attempt == 0
                    else "\n上一轮身份或 JSON 契约无效。逐条原样返回全部 fingerprint。"
                )
                response = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt + repair_note}],
                    temperature=0.0,
                    max_tokens=max(1200, min(4000, 450 * len(evidence))),
                    response_format={"type": "json_object"},
                )
                payload = parse_json_object(
                    response.choices[0].message.content or "{}"
                )
                rows = payload.get("decisions")
                if not isinstance(rows, list):
                    raise ValueError("缺少 decisions 数组")
                decisions: dict[str, tuple[str, str]] = {}
                seen, duplicate = set(), set()
                foreign = 0
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    fingerprint = str(row.get("fingerprint", "")).strip()
                    if fingerprint not in allowed:
                        foreign += 1
                        continue
                    if fingerprint in seen:
                        duplicate.add(fingerprint)
                        continue
                    seen.add(fingerprint)
                    verdict = str(row.get("decision", "")).strip()
                    reason = str(row.get("reason", "")).strip()
                    if verdict not in {"full_review", "screen_out"}:
                        continue
                    if verdict == "screen_out" and not reason:
                        continue
                    decisions[fingerprint] = (verdict, reason)
                for fingerprint in duplicate:
                    decisions.pop(fingerprint, None)
                if foreign:
                    run_audit.event(
                        "origin_eligibility_contract",
                        "warning",
                        f"丢弃 {foreign} 条不属于当前批次的预筛输出",
                    )
                missing = allowed - decisions.keys()
                if missing:
                    run_audit.event(
                        "origin_eligibility_contract",
                        "deferred",
                        f"资格预筛漏回 {len(missing)} 条输入；仅对应材料保留 pending",
                    )
                run_audit.record_llm(
                    stage="origin_eligibility",
                    role="sub",
                    model=model,
                    subject=f"{len(evidence)} origins",
                    response=response,
                )
                return decisions
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "原点资格预筛第 %s 次失败 [%s 条]: %s",
                    attempt + 1,
                    len(evidence),
                    exc,
                )
                run_audit.record_llm(
                    stage="origin_eligibility",
                    role="sub",
                    model=self.model,
                    subject=f"{len(evidence)} origins / attempt-{attempt + 1}",
                    response=response,
                    error=exc,
                )
        run_audit.event(
            "origin_eligibility_contract",
            "deferred",
            f"{len(evidence)} 条资格预筛失败并保留 pending："
            f"{type(last_error).__name__ if last_error else 'unknown'}",
        )
        return {}

    @staticmethod
    def _eligibility_rejection(
        evidence: TechnicalEvidence,
        reason: str,
    ) -> ParadigmExtraction:
        return ParadigmExtraction(
            evidence=evidence,
            is_candidate=False,
            canonical_name="",
            thesis="",
            problem_shift="",
            mechanism="",
            rejection_reason=reason,
            rubric_assessment={
                "version": "origin-eligibility-v1",
                "stage": "origin_eligibility",
                "decision": "reject",
                "decision_reason": reason,
                "answer_coverage": 1.0,
                "answers": [],
            },
        )

    async def extract(self, evidence: TechnicalEvidence) -> list[ParadigmExtraction]:
        if evidence.raw.get("origin_kind") == "technical_report":
            return await self._extract_technical_report(evidence)

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
            authors=_author_prompt_summary(evidence.authors),
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
                    for item in payloads
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

    async def _extract_technical_report(
        self,
        evidence: TechnicalEvidence,
    ) -> list[ParadigmExtraction]:
        """Index once, assess a bounded slice, and resume without recomputation.

        The mechanism count remains unlimited.  The slice only bounds one queue
        visit; completed mechanism results and the report index live in the
        pending evidence payload until every seed has a terminal assessment.
        """
        evidence.raw.pop("technical_report_partial_failure", None)
        evidence.raw.pop("technical_report_slice_pending", None)
        evidence.raw.pop("technical_report_last_run_failure", None)
        document_excerpt = str(evidence.raw.get("document_excerpt", ""))
        report_material = evidence.summary
        if document_excerpt:
            source_kind = str(
                evidence.raw.get("document_source_kind") or "official_document"
            )
            report_material = (
                f"{report_material}\n\n[官方报告正文节选：{source_kind}]\n"
                f"{document_excerpt}"
            )
        checkpoint_version = "technical-report-checkpoint-v1"
        cached_version = str(
            evidence.raw.get("technical_report_checkpoint_version", "")
        )
        cached_seeds = evidence.raw.get("technical_report_mechanism_seeds")
        cached_mechanisms = [
            item
            for item in (cached_seeds if isinstance(cached_seeds, list) else [])
            if isinstance(item, dict)
            and str(item.get("canonical_name", "")).strip()
            and str(item.get("problem_shift", "")).strip()
            and str(item.get("mechanism", "")).strip()
        ]
        if cached_version == checkpoint_version and cached_mechanisms:
            mechanisms = cached_mechanisms
            index_error = ""
            disposition_reason = ""
            run_audit.event(
                "technical_report_index",
                "checkpoint_reused",
                f"{evidence.title[:80]}：复用 {len(mechanisms)} 个机制种子",
            )
        else:
            index_prompt = self.skill_loader.render(
                "technical_report_index",
                source=evidence.source,
                title=evidence.title,
                report_material=report_material[:50_000],
                authors=_author_prompt_summary(evidence.authors),
                organization=evidence.organization,
                identifiers=evidence.identifiers,
                frontier_domains=evidence.raw.get("frontier_domains", []),
                publisher_context={
                    "organization": evidence.organization,
                    "publisher_tier": evidence.raw.get("publisher_tier", "unknown"),
                    "publisher_evidence": evidence.raw.get("publisher_evidence", ""),
                },
            )
            run_audit.event(
                "technical_report_index",
                "request_bounded",
                f"{evidence.title[:80]}：输入 {len(index_prompt)} 字符；"
                f"报告材料 {len(report_material[:50_000])} 字符",
            )
            mechanisms, index_error, disposition_reason = (
                await self._request_report_index(evidence, index_prompt)
            )
            if mechanisms:
                evidence.raw["technical_report_checkpoint_version"] = (
                    checkpoint_version
                )
                evidence.raw["technical_report_mechanism_seeds"] = mechanisms
                evidence.raw["technical_report_completed_mechanisms"] = {}
                evidence.raw["technical_report_mechanism_failure_counts"] = {}
                evidence.raw["technical_report_committed_candidates"] = {}
                if self.checkpoint_callback is not None:
                    evidence.raw["technical_report_slice_pending"] = True
                    self.checkpoint_callback(evidence, None, "")
        if not mechanisms:
            if not index_error and disposition_reason:
                return [
                    ParadigmExtraction(
                        evidence=evidence,
                        is_candidate=False,
                        canonical_name="",
                        thesis="",
                        problem_shift="",
                        mechanism="",
                        rejection_reason=disposition_reason,
                        rubric_assessment={
                            "decision": "reject",
                            "decision_reason": disposition_reason,
                            "answer_coverage": 1.0,
                            "answers": [],
                        },
                    )
                ]
            return [
                self._failed_extraction(
                    evidence,
                    f"Technical Report 机制索引失败: {index_error or '结果为空'}",
                )
            ]

        completed_payloads = evidence.raw.get(
            "technical_report_completed_mechanisms"
        )
        if not isinstance(completed_payloads, dict):
            completed_payloads = {}
        else:
            completed_payloads = {
                str(key): payload
                for key, payload in completed_payloads.items()
                if isinstance(payload, dict)
                and _report_extraction_from_payload(evidence, payload) is not None
            }
        failure_counts = evidence.raw.get(
            "technical_report_mechanism_failure_counts"
        )
        if not isinstance(failure_counts, dict):
            failure_counts = {}
        seeds = [
            (_report_mechanism_key(seed, index), index, seed)
            for index, seed in enumerate(mechanisms, 1)
        ]
        pending = [
            value for value in seeds if value[0] not in completed_payloads
        ]
        # A repeatedly malformed mechanism moves behind never-attempted peers,
        # so one bad section cannot starve the rest of the same report.
        pending.sort(
            key=lambda value: (
                int(failure_counts.get(value[0], 0) or 0),
                value[1],
            )
        )
        slice_limit = self.technical_report_mechanism_slice
        selected = pending[:slice_limit] if slice_limit else pending
        semaphore = asyncio.Semaphore(min(self.concurrency, 2))

        async def guarded(key: str, index: int, seed: dict):
            async with semaphore:
                extraction, error = await self._assess_report_mechanism(
                    evidence,
                    seed,
                    index=index,
                )
                return key, extraction, error

        tasks = [asyncio.create_task(guarded(key, index, seed)) for key, index, seed in selected]
        failures = []
        newly_completed: list[ParadigmExtraction] = []
        try:
            for task in asyncio.as_completed(tasks):
                key, extraction, error = await task
                if extraction is None:
                    failure_counts[key] = int(failure_counts.get(key, 0) or 0) + 1
                    failures.append(f"{key[:10]}: {error}")
                    continue
                extraction.mechanism_id = f"report-{evidence.fingerprint[:16]}-{key}"
                completed_payloads[key] = _report_extraction_payload(extraction)
                failure_counts.pop(key, None)
                newly_completed.append(extraction)
                if self.checkpoint_callback is not None:
                    evidence.raw["technical_report_completed_mechanisms"] = completed_payloads
                    evidence.raw["technical_report_mechanism_failure_counts"] = failure_counts
                    evidence.raw["technical_report_slice_pending"] = True
                    self.checkpoint_callback(evidence, extraction, key)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        evidence.raw["technical_report_completed_mechanisms"] = completed_payloads
        evidence.raw["technical_report_mechanism_failure_counts"] = failure_counts

        successful = []
        for key, _, _ in seeds:
            payload = completed_payloads.get(key)
            if not isinstance(payload, dict):
                continue
            restored = _report_extraction_from_payload(evidence, payload)
            if restored is not None:
                restored.mechanism_id = f"report-{evidence.fingerprint[:16]}-{key}"
                successful.append(restored)

        remaining = len(mechanisms) - len(successful)
        if failures:
            evidence.raw["technical_report_partial_failure"] = True
            evidence.raw["technical_report_last_run_failure"] = True
        if remaining:
            evidence.raw["technical_report_slice_pending"] = True
            if failures:
                reason = (
                    "Technical Report 部分机制评估失败；"
                    f"检查点已完成 {len(successful)}/{len(mechanisms)}，"
                    f"剩余 {remaining}；下次从检查点继续；本轮失败 "
                    + "；".join(failures)
                )
            else:
                reason = (
                    f"Technical Report 机制检查点未闭合：已完成 "
                    f"{len(successful)}/{len(mechanisms)}，剩余 {remaining}；"
                    "下次从检查点继续"
                )
            return [
                *newly_completed,
                self._failed_extraction(evidence, reason),
            ]
        evidence.raw.pop("technical_report_slice_pending", None)
        evidence.raw.pop("technical_report_partial_failure", None)
        evidence.raw.pop("technical_report_last_run_failure", None)
        if newly_completed:
            return newly_completed
        if successful:
            # Defensive recovery for a legacy checkpoint that had all mechanism
            # payloads but never committed the parent analyzed flag.
            return successful
        return [self._failed_extraction(evidence, "Technical Report 全部机制评估失败")]

    async def _request_report_index(
        self,
        evidence: TechnicalEvidence,
        prompt: str,
    ) -> tuple[list[dict], str, str]:
        last_error: Exception | None = None
        for attempt in range(2):
            response = None
            try:
                client, model = self._get_client()
                repair_note = (
                    ""
                    if attempt == 0
                    else "\n上一轮结构无效。请缩短每个字段，只返回一个合法 JSON 对象。"
                )
                response = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt + repair_note}],
                    temperature=0.1,
                    max_tokens=6000,
                    response_format={"type": "json_object"},
                )
                payload = parse_json_object(
                    response.choices[0].message.content or "{}"
                )
                raw_mechanisms = payload.get("mechanisms")
                if not isinstance(raw_mechanisms, list):
                    raise ValueError("缺少 mechanisms 数组")
                mechanisms = [
                    item
                    for item in raw_mechanisms
                    if isinstance(item, dict)
                    and str(item.get("canonical_name", "")).strip()
                    and str(item.get("problem_shift", "")).strip()
                    and str(item.get("mechanism", "")).strip()
                ]
                disposition = str(
                    payload.get("report_disposition", "")
                ).strip()
                disposition_reason = str(
                    payload.get("disposition_reason", "")
                ).strip()
                if (
                    not mechanisms
                    and disposition == "no_independent_mechanism"
                    and disposition_reason
                ):
                    run_audit.record_llm(
                        stage="technical_report_index",
                        role="sub",
                        model=model,
                        subject=evidence.title,
                        response=response,
                    )
                    return [], "", disposition_reason
                if not mechanisms:
                    raise ValueError("没有形成完整机制种子")
                run_audit.record_llm(
                    stage="technical_report_index",
                    role="sub",
                    model=model,
                    subject=evidence.title,
                    response=response,
                )
                return mechanisms, "", ""
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Technical Report 机制索引第 %s 次失败 [%s]: %s",
                    attempt + 1,
                    evidence.title[:60],
                    exc,
                )
                run_audit.record_llm(
                    stage="technical_report_index",
                    role="sub",
                    model=self.model,
                    subject=f"{evidence.title} / attempt-{attempt + 1}",
                    response=response,
                    error=exc,
                )
        return [], str(last_error or "未知错误"), ""

    async def _assess_report_mechanism(
        self,
        evidence: TechnicalEvidence,
        seed: dict,
        *,
        index: int,
    ) -> tuple[ParadigmExtraction | None, str]:
        prompt = self.skill_loader.render(
            "technical_report_mechanism",
            report_title=evidence.title,
            report_summary=evidence.summary[:3000],
            organization=evidence.organization,
            mechanism_seed=json.dumps(seed, ensure_ascii=False),
            rubric_definition=rubric_prompt(
                "screening",
                seed.get("innovation_types"),
            ),
        )
        run_audit.event(
            "technical_report_mechanism",
            "request_bounded",
            f"{evidence.title[:70]} / mechanism-{index}："
            f"输入 {len(prompt)} 字符；种子 "
            f"{len(json.dumps(seed, ensure_ascii=False))} 字符",
        )
        last_error: Exception | None = None
        for attempt in range(2):
            response = None
            try:
                client, model = self._get_client()
                repair_note = (
                    ""
                    if attempt == 0
                    else "\n上一轮 JSON 或 Rubric 不完整。只修正本机制，输出合法 JSON。"
                )
                response = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt + repair_note}],
                    temperature=0.1,
                    max_tokens=3500,
                    response_format={"type": "json_object"},
                )
                payload = parse_json_object(
                    response.choices[0].message.content or "{}"
                )
                assessment_payload = payload.get("assessment")
                if isinstance(assessment_payload, dict):
                    payload = assessment_payload
                merged = {
                    **seed,
                    "innovation_types": payload.get(
                        "innovation_types",
                        seed.get("innovation_types", ["other"]),
                    ),
                    "rubric_answers": payload.get("rubric_answers"),
                }
                for field_name in (
                    "thesis",
                    "background",
                    "problem_shift",
                    "design_philosophy",
                    "mechanism",
                    "technical_explanation",
                    "application_value",
                    "why_now",
                    "claimed_results",
                ):
                    value = payload.get(field_name)
                    if value not in (None, "", []):
                        merged[field_name] = value
                parsed = self._from_payload(evidence, merged)
                if parsed.rubric_assessment.get("decision") == "incomplete":
                    raise ValueError(
                        parsed.rubric_assessment.get(
                            "decision_reason",
                            "Rubric 回答不完整",
                        )
                    )
                run_audit.record_llm(
                    stage="technical_report_mechanism",
                    role="sub",
                    model=model,
                    subject=f"{evidence.title} / mechanism-{index}",
                    response=response,
                )
                return parsed, ""
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Technical Report 机制 %s 第 %s 次评估失败 [%s]: %s",
                    index,
                    attempt + 1,
                    evidence.title[:60],
                    exc,
                )
                run_audit.record_llm(
                    stage="technical_report_mechanism",
                    role="sub",
                    model=self.model,
                    subject=(
                        f"{evidence.title} / mechanism-{index} / "
                        f"attempt-{attempt + 1}"
                    ),
                    response=response,
                    error=exc,
                )
        return None, str(last_error or "未知错误")

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


def _should_prefilter_origin(evidence: TechnicalEvidence) -> bool:
    """Only ordinary papers use the cheap gate; high-signal origins bypass it."""

    raw = evidence.raw or {}
    try:
        origin_priority = int(raw.get("origin_priority", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        origin_priority = 0
    return bool(
        evidence.evidence_type == EvidenceType.PRIMARY_PAPER
        and raw.get("origin_kind", "research_paper") == "research_paper"
        and raw.get("origin_eligibility_decision") != "full_review"
        and not raw.get("explicit_seed")
        and origin_priority < 2
        and not raw.get("priority_researcher_match")
    )


def _report_mechanism_key(seed: dict, index: int) -> str:
    payload = json.dumps(
        {"index": index, "seed": seed},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _report_extraction_payload(extraction: ParadigmExtraction) -> dict:
    """Serialize only the mechanism judgment; evidence is the parent checkpoint."""

    return {
        field.name: getattr(extraction, field.name)
        for field in fields(ParadigmExtraction)
        if field.name != "evidence"
    }


def _report_extraction_from_payload(
    evidence: TechnicalEvidence,
    payload: dict,
) -> ParadigmExtraction | None:
    allowed = {
        field.name for field in fields(ParadigmExtraction) if field.name != "evidence"
    }
    values = {key: value for key, value in payload.items() if key in allowed}
    required = {"is_candidate", "canonical_name", "thesis", "problem_shift", "mechanism"}
    if not required.issubset(values):
        return None
    try:
        return ParadigmExtraction(evidence=evidence, **values)
    except (TypeError, ValueError):
        return None


def _author_prompt_summary(authors: list[str]) -> str:
    """大型系统报告保留关键署名结构，不把数百个人名灌进模型上下文。"""
    cleaned = list(dict.fromkeys(name.strip() for name in authors if name.strip()))
    if len(cleaned) <= 16:
        return ", ".join(cleaned)
    collective = [
        name
        for name in cleaned
        if name.casefold().endswith(
            (" team", " consortium", " collaboration")
        )
    ]
    priority_names = {
        "".join(character for character in value.casefold() if character.isalnum())
        for value in config.PRIORITY_RESEARCHERS
    }
    priority = [
        name
        for name in cleaned
        if "".join(
            character for character in name.casefold() if character.isalnum()
        )
        in priority_names
    ]
    visible = list(
        dict.fromkeys([*collective, *cleaned[:6], *priority, *cleaned[-2:]])
    )
    return f"{', '.join(visible)}（共 {len(cleaned)} 位作者，完整名单保留在证据中）"


class ResearcherTrajectoryAnalyzer:
    """用代表作验证研究连续性，不推测作者创业意愿。"""

    def __init__(self, concurrency: int = 4, client=None, model: str = "", *, stage_cache=None):
        self.concurrency = max(concurrency, 1)
        self.client = client
        self.model = model
        self.skill_loader = SkillLoader()
        self.stage_cache = stage_cache

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

        tasks = [
                asyncio.create_task(analyze(candidate, profile))
                for candidate in candidates
                for profile in key_researcher_profiles(
                    candidate.researchers,
                    config.PARADIGM_KEY_RESEARCHER_LIMIT,
                )
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
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
            signature = _stage_cache_signature("main", self.model, prompt, {
                "candidate": candidate.key, "person_identifiers": profile.identifiers,
                "origins": sorted((item.fingerprint, item.source_revision) for item in candidate.evidence if item.evidence_type in {EvidenceType.PRIMARY_PAPER, EvidenceType.TECHNICAL_BLOG, EvidenceType.CONCEPT_ESSAY, EvidenceType.ORIGINAL_IMPLEMENTATION}),
            })
            cache = getattr(self, "stage_cache", None)
            payload = cache.cache_get("researcher_trajectory", signature) if cache else None
            model = self.model or resolve_model("main").model
            if payload is None:
                client, model = self._get_client()
                response = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=1100,
                    response_format={"type": "json_object"},
                )
                payload = parse_json_object(response.choices[0].message.content or "{}")
            for key in ("background_summary", "trajectory_summary", "key_person_reason", "current_role_note"):
                if not isinstance(payload.get(key), str) or (key != "current_role_note" and not payload[key].strip()):
                    raise ValueError("人物分析 JSON 字段未闭合：" + key)
                if key != "current_role_note" and not re.search(r"[\u4e00-\u9fff]", payload[key]):
                    raise ValueError("人物解释字段必须使用中文：" + key)
            consistency = payload.get("trajectory_consistency")
            if isinstance(consistency, bool) or not isinstance(consistency, (int, float)) or not math.isfinite(consistency) or not 0 <= consistency <= 10:
                raise ValueError("人物连续性计数无效")
            if cache and response is not None:
                cache.cache_save("researcher_trajectory", signature, payload)
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
            if response is not None:
                run_audit.record_llm(
                    stage="researcher_trajectory", role="main", model=model,
                    subject=f"{candidate.name} / {profile.name}", response=response,
                )
            else:
                run_audit.event("researcher_trajectory_cache", "reused", f"复用 {profile.name} 同身份、同材料与规则版本的人物研究")
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
            # A failed person lookup is an execution failure, not a completed
            # profile. The orchestrator isolates this candidate and resumes
            # from its synthesized checkpoint without redoing technical work.
            raise RuntimeError("研究轨迹自动分析失败") from exc


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

        await gather_scoped(*(synthesize(item) for item in candidates))
        return candidates

    async def _synthesize_one(self, candidate: ParadigmCandidate) -> None:
        evidence_payload = _bounded_synthesis_evidence(candidate.evidence)
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
        run_audit.event(
            "paradigm_synthesis",
            "request_bounded",
            f"{candidate.name[:80]}：输入 {len(prompt)} 字符；"
            f"证据明细 {len(evidence_payload['records'])} 条，"
            f"折叠 {evidence_payload['overflow']['count']} 条",
        )
        visible_evidence_indices = {
            int(record["index"])
            for record in evidence_payload["records"]
            if not record.get("detail_deferred_due_to_context")
        }
        last_error: Exception | None = None
        partial_payload: dict[str, object] = {}
        validation_error = ""
        for attempt in range(2):
            response = None
            try:
                client, model = self._get_client()
                stage = "paradigm_synthesis"
                request_prompt = prompt
                if attempt and partial_payload:
                    stage = "paradigm_synthesis_repair"
                    request_prompt = self.skill_loader.render(
                        "paradigm_synthesis_repair",
                        provisional_name=candidate.name,
                        route_family=candidate.route_family,
                        validation_error=validation_error,
                        partial_payload=json.dumps(
                            {
                                key: partial_payload.get(key)
                                for key in (
                                    "mental_model",
                                    "innovation_types",
                                    "rubric_answers",
                                )
                                if key in partial_payload
                            },
                            ensure_ascii=False,
                        ),
                        rubric_definition=rubric_prompt("final"),
                    )
                elif attempt:
                    request_prompt += (
                        "\n上一轮不是合法 JSON。请重新输出完整 JSON，补齐所选"
                        " innovation_types 的全部 Rubric 题；心智模型必须先给主观察"
                        "坐标与低分辨率运行图，再用至少两个 resolution_ladder 节点"
                        "逐层纠偏和提高分辨率。"
                    )
                response = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": request_prompt}],
                    temperature=0.1,
                    max_tokens=5600,
                    response_format={"type": "json_object"},
                )
                payload = parse_json_object(
                    response.choices[0].message.content or "{}"
                )
                if partial_payload:
                    payload = _merge_structured_payload(partial_payload, payload)
                partial_payload = payload
                try:
                    self._apply_synthesis_payload(
                        candidate,
                        payload,
                        visible_evidence_indices=visible_evidence_indices,
                    )
                except Exception as exc:
                    validation_error = str(exc)
                    raise
                run_audit.record_llm(
                    stage=stage,
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
                    stage=(
                        "paradigm_synthesis_repair"
                        if attempt and partial_payload
                        else "paradigm_synthesis"
                    ),
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
        candidate: ParadigmCandidate,
        payload: dict[str, object],
        *,
        visible_evidence_indices: set[int],
    ) -> None:
        previous_mechanism = candidate.mechanism.strip()
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

        # The synthesis model may certify only a fully expanded, independently
        # sourced discussion as directly substantive for this mechanism.  The
        # delivery freshness gate consumes this explicit audit marker; generic
        # topic overlap can never create it by itself.
        for item in candidate.evidence:
            if item.raw.get("substantive_uptake_source") == "synthesis-v1":
                if (
                    item.raw.get("substantive_uptake_route_key") == candidate.key
                    and
                    item.raw.get("content_scrubbed")
                    and not item.summary.strip()
                    and candidate.mechanism.strip() == previous_mechanism
                ):
                    # The previous body was intentionally discarded after a
                    # completed audit. Do not erase its relation merely
                    # because a later refresh could not re-fetch that body;
                    # an explicit model exclusion below still removes it.
                    continue
                item.raw.pop("substantive_uptake", None)
                item.raw.pop("substantive_uptake_source", None)
                item.raw.pop("substantive_uptake_route_key", None)
        substantive_indices = {
            int(value)
            for value in payload.get("substantive_uptake_evidence_indices", [])
            if str(value).isdigit()
        }
        for index in substantive_indices & visible_evidence_indices:
            if not 0 <= index < len(candidate.evidence):
                continue
            certify_synthesis_discussion(candidate, candidate.evidence[index])

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


def _bounded_synthesis_evidence(
    evidence: list[TechnicalEvidence],
    *,
    char_budget: int = 48_000,
) -> dict[str, object]:
    """Represent evidence under a hard context budget without a research Top-K.

    Current primary materials and independent uptake are serialized first and
    with more detail.  If accumulated historical evidence exceeds the request
    budget, the remainder is represented by an explicit count/type/source
    ledger.  Nothing is silently reclassified as rejected or absent.
    """

    primary_types = {
        "primary_paper",
        "technical_blog",
        "concept_essay",
        "original_implementation",
    }
    uptake_types = {
        "independent_replication",
        "implementation",
        "community_discussion",
        "secondary_interpretation",
        "product_adoption",
        "peer_review",
        "citation",
    }

    def priority(pair: tuple[int, TechnicalEvidence]) -> tuple[int, int, int]:
        original_index, item = pair
        historical = bool(item.raw.get("historical"))
        evidence_type = item.evidence_type.value
        lane = (
            0
            if evidence_type in primary_types and not historical
            else 1
            if evidence_type in uptake_types and not historical
            else 2
            if evidence_type in primary_types
            else 3
        )
        return lane, int(historical), original_index

    ordered = sorted(enumerate(evidence), key=priority)
    records: list[dict[str, object]] = []
    used = 0
    overflow_items: list[TechnicalEvidence] = []
    for original_index, item in ordered:
        primary = item.evidence_type.value in primary_types
        affiliations = item.raw.get("affiliations", [])
        project_urls = item.raw.get("project_urls", [])
        author_roles = item.raw.get("author_roles", {})
        metrics = item.metrics if isinstance(item.metrics, dict) else {}
        compact_metrics = {
            str(key)[:80]: (
                value
                if isinstance(value, (int, float, bool)) or value is None
                else str(value)[:200]
            )
            for key, value in list(metrics.items())[:20]
        }
        detailed = {
            "index": original_index,
            "fingerprint": item.fingerprint,
            "type": item.evidence_type.value,
            "source": item.source[:120],
            "title": item.title[:400],
            "url": item.url[:1000],
            "summary": item.summary[: (2400 if primary else 700)],
            "document_excerpt": (
                str(item.raw.get("document_excerpt", ""))[:7000]
                if primary
                else ""
            ),
            "document_source_url": str(
                item.raw.get("document_source_url", "")
            )[:1000],
            "affiliations": (
                [str(value)[:300] for value in affiliations[:8]]
                if isinstance(affiliations, list)
                else []
            ),
            "project_urls": (
                [str(value)[:1000] for value in project_urls[:8]]
                if isinstance(project_urls, list)
                else []
            ),
            "author_roles": (
                {
                    str(key)[:120]: str(value)[:300]
                    for key, value in list(author_roles.items())[:12]
                }
                if isinstance(author_roles, dict)
                else {}
            ),
            "authors": [str(value) for value in item.authors[:12]],
            "metrics": compact_metrics,
            "historical": bool(item.raw.get("historical")),
            "relationship_hint": str(item.raw.get("relationship", ""))[:120],
        }
        encoded_length = len(json.dumps(detailed, ensure_ascii=False))
        if used + encoded_length <= char_budget:
            records.append(detailed)
            used += encoded_length
            continue

        compact = {
            "index": original_index,
            "fingerprint": item.fingerprint,
            "type": item.evidence_type.value,
            "source": item.source[:80],
            "title": item.title[:180],
            "url": item.url[:500],
            "metrics": compact_metrics,
            "historical": bool(item.raw.get("historical")),
            "relationship_hint": str(item.raw.get("relationship", ""))[:80],
            "detail_deferred_due_to_context": True,
        }
        compact_length = len(json.dumps(compact, ensure_ascii=False))
        if used + compact_length <= char_budget:
            records.append(compact)
            used += compact_length
        else:
            overflow_items.append(item)

    type_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    historical_count = 0
    for item in overflow_items:
        evidence_type = item.evidence_type.value
        type_counts[evidence_type] = type_counts.get(evidence_type, 0) + 1
        source = item.source[:80] or "unknown"
        source_counts[source] = source_counts.get(source, 0) + 1
        historical_count += int(bool(item.raw.get("historical")))
    if len(source_counts) > 40:
        ordered_sources = sorted(
            source_counts.items(),
            key=lambda pair: (-pair[1], pair[0]),
        )
        retained_sources = dict(ordered_sources[:40])
        retained_sources["__other_sources__"] = sum(
            count for _, count in ordered_sources[40:]
        )
        source_counts = retained_sources
    return {
        "records": records,
        "overflow": {
            "count": len(overflow_items),
            "historical_count": historical_count,
            "type_counts": type_counts,
            "source_counts": source_counts,
            "meaning": (
                "这些证据因上下文预算只保留统计，不代表被 Rubric 淘汰；"
                "不得据此断言没有更多讨论。"
                if overflow_items
                else "没有折叠证据"
            ),
        },
    }


def _merge_structured_payload(
    base: dict[str, object], patch: dict[str, object]
) -> dict[str, object]:
    """Merge a small validation repair without discarding valid first-pass work."""

    merged: dict[str, object] = dict(base)
    for key, value in patch.items():
        previous = merged.get(key)
        if isinstance(previous, dict) and isinstance(value, dict):
            merged[key] = _merge_structured_payload(previous, value)
        elif value not in (None, ""):
            merged[key] = value
    return merged


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
    status_aliases = {
        "source": "source_fact",
        "fact": "source_fact",
        "factual": "source_fact",
        "direct_evidence": "source_fact",
        "source_factual": "source_fact",
        "interpretation": "interpretive_compression",
        "interpretive": "interpretive_compression",
        "synthesis": "interpretive_compression",
        "compression": "interpretive_compression",
        "inferred": "inference",
        "reasoning": "inference",
        "deduction": "inference",
        "uncertain": "unknown",
        "unverified": "unknown",
        "not_available": "unknown",
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
        raw_status = re.sub(
            r"[^a-z0-9]+",
            "_",
            str(node["evidence_status"]).strip().casefold(),
        ).strip("_")
        normalized_status = status_aliases.get(raw_status, raw_status)
        node["evidence_status"] = normalized_status
        if normalized_status not in allowed_status:
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
