"""Explicitly authorized, isolated one-paper/one-route real-model acceptance.

Never calls main/discovery/full workflow, restores production state, dispatches
continuations, creates an outbox or imports the email sender. Artifacts are test
evidence, NOT a formal weekly report or a successful production empty report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.sample_budget import SampleBudgetClient, SampleBudgetError, BASE_URL, MODEL


def isolate_environment():
    # No production Secrets/Variables may authorize additional paid services,
    # mail or resumes. We keep ONLY the explicitly budgeted model key.
    for key in list(os.environ):
        if key.startswith(("SMTP_", "EMAIL_", "TAVILY_", "TWITTER_", "REDDIT_",
                           "SEMANTIC_SCHOLAR_", "OPENALEX_", "MAIN_AGENT_", "SUB_AGENT_")):
            os.environ.pop(key, None)
    os.environ.update({"AI_RADAR_SKIP_DOTENV": "true", "EMAIL_PUSH_ENABLED": "false",
        "EMAIL_PUSH_REQUIRED": "false", "PARADIGM_AUTO_RESUME_ENABLED": "false",
        "KOL_X_SOURCE_ENABLED": "false", "TAVILY_SOCIAL_SEARCH_ENABLED": "false",
        "SEMANTIC_SCHOLAR_ENABLED": "false", "REDDIT_API_ACCESS_APPROVED": "false",
        "LLM_PROVIDER": "dashscope", "LLM_MODEL": MODEL, "LLM_BASE_URL": BASE_URL,
        "LLM_ENABLE_THINKING": "false"})


async def run_sample(arxiv_id: str, guarded, directory: Path, result: dict, *, reference_time=None):
    # Late imports ensure .env/production configuration cannot influence this
    # explicitly isolated test. Use real stage implementations, never mocks.
    import httpx
    from agents.paradigm_orchestrator import ParadigmOrchestrator
    from database.paradigm_store import ParadigmStore
    from paradigms.analyzer import ParadigmAnalyzer, ParadigmSynthesizer, ResearcherTrajectoryAnalyzer
    from paradigms.clustering import cluster_extractions
    from paradigms.discovery import _raw_to_origin
    from paradigms.enrichment import EvidenceEnricher
    from paradigms.scoring import score_candidate, is_reportable
    from reports.paradigm_generator import ParadigmReportGenerator, _report_input_violations
    from sources.arxiv_source import ArxivSource
    from sources.arxiv_document_source import ArxivDocumentClient
    from runtime_clock import research_window

    now = reference_time or datetime.now(timezone.utc)
    result["reference_time"] = now.isoformat()
    with research_window(now, lookback_days=7):
        source = ArxivSource(seed_arxiv_ids=[arxiv_id])
        async with httpx.AsyncClient(timeout=35) as client:
            rows = await source._fetch_seed_ids(client, [arxiv_id])
        if len(rows) != 1:
            raise ValueError("精确原点输入身份/基数未闭合")
        origin = _raw_to_origin(rows[0])
        if origin.identifiers.get("arxiv") != arxiv_id:
            raise ValueError("返回了非本次授权材料")
        result["primary"] = {"title": origin.title, "url": origin.url,
                             "published_at": origin.published_at}
        if origin.raw.get("origin_kind") == "technical_report":
            raise ValueError("单路线样本拒绝多机制 Technical Report；不能截断为一条冒充完成")
        await ArxivDocumentClient().hydrate(origin)
        if not origin.raw.get("document_excerpt"):
            raise ValueError("一手正文未读取，不支付摘要级质量验收")
        result["document_characters"] = len(origin.raw["document_excerpt"])
        store = ParadigmStore(directory / "sample.db")
        origins, _, checkpoint = store.observe_origins([origin])
        if checkpoint.rejected_count:
            raise ValueError("临时原点入库存在拒绝项")
        if len(origins) != 1:
            raise ValueError("临时原点入库身份/基数未闭合")
        values = await ParadigmAnalyzer(concurrency=1, client=guarded, model=MODEL).run(origins)
        if len(values) != 1 or values[0].evidence.fingerprint != origins[0].fingerprint:
            raise ValueError("模型返回多个机制或不匹配原点，停止而非截断")
        extraction = values[0]
        result["screening_decision"] = extraction.rubric_assessment.get("decision")
        result["screening_reason"] = extraction.rubric_assessment.get("decision_reason", "")
        if result["screening_decision"] not in {"deep_dive", "observe", "reject"}:
            raise ValueError("真实抽取/Rubric 未闭合")
        candidates = cluster_extractions(values)
        if not candidates:
            result["status"] = "screening_closed"
            result["quality_boundary"] = "原点筛选有结论；没有绕过 Rubric 测试后续写作，不能据此宣称非空报告质量通过。"
            return
        if len(candidates) != 1:
            raise ValueError("超出一条路线授权")
        candidate = candidates[0]
        candidate.status = "pending_deep"
        store.save_candidates(candidates)
        orchestrator = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
        orchestrator.store, orchestrator.deep_concurrency = store, 1
        orchestrator.enricher = EvidenceEnricher(concurrency=1, budget_store=store.provider_budgets)
        orchestrator.synthesizer = ParadigmSynthesizer(concurrency=1, client=guarded, model=MODEL)
        orchestrator.trajectory = ResearcherTrajectoryAnalyzer(concurrency=1, client=guarded,
                                                            model=MODEL, stage_cache=store.campaigns)
        completed, budget_pending, failed = await orchestrator._deep_analyze_in_batches(
            candidates, [], time.monotonic() + 900)
        if budget_pending or failed or len(completed) != 1:
            result["sample_execution_pending"] = len(budget_pending) + len(failed)
            raise ValueError("真实单路线研究存在执行待办；不生成阶段性报告")
        candidate = score_candidate(completed[0])
        result["route_name"] = candidate.name
        result["route_decision"] = candidate.rubric_assessment.get("decision")
        result["route_reason"] = candidate.rejection_reason or candidate.admission_reason
        result["explanation"] = candidate.technical_explanation
        result["discussion_judgment"] = candidate.trend_interpretation
        result["people_count"] = len(candidate.researchers)
        # Persist facts about the test boundary, not temporary community bodies
        # or a claim that absent/disabled platform coverage means no discussion.
        result["verification_summary"] = _verification_summary(candidate)
        if result["route_decision"] not in {"report", "observe", "reject"}:
            raise ValueError("最终 Rubric 未闭合")
        if not is_reportable(candidate):
            result["status"] = "route_judgment_closed"
            result["quality_boundary"] = "真实路线判断已完成但不符合交付准入；没有生成成功空周报，写作质量未验证。"
            return
        violations = _report_input_violations([candidate])
        if violations:
            result["input_violations"] = violations
            raise ValueError("单路线写作输入未闭合")
        generator = ParadigmReportGenerator(output_dir=directory, client=guarded, model=MODEL)
        draft = await generator._draft_one_route(now.date().isoformat(), candidate, index=1, total=1)
        # The actual production route-quality gate has run. Deliberately avoid
        # generate(): a one-route sample has NO weekly-wide coverage proof.
        result["route_test_draft"] = draft
        result["status"] = "route_quality_passed"
        result["quality_boundary"] = "仅单路线生产写作闸门通过；未验收整周召回、总编、生产状态、去重或 SMTP，不是正式报告。"


def _verification_summary(candidate):
    from paradigms.search_receipts import search_receipt_summary
    from paradigms.models import is_verified_substantive_discussion
    return {
        "publisher_tier": candidate.publisher_tier,
        "publisher_evidence_count": len(candidate.publisher_evidence),
        "verified_substantive_discussion_count": sum(
            is_verified_substantive_discussion(item, route_key=candidate.key) for item in candidate.evidence),
        "community_coverage": candidate.community_coverage,
        "search_receipts": search_receipt_summary(candidate),
        "contact_lookup_completed_count": sum(person.contact_lookup_completed for person in candidate.researchers),
    }


async def execute(args):
    isolate_environment()
    from agents.llm_utils import build_client, request_policy, resolve_model
    import config
    from run_audit import run_audit
    from runtime_provenance import runtime_provenance
    if not config.LLM_API_KEY or config.LLM_API_KEY in {"placeholder", "offline-test-only"}:
        raise SampleBudgetError("安全执行环境未配置真实模型 Key；不要在聊天中提供密钥")
    directory = Path("logs/one_route")
    directory.mkdir(parents=True, exist_ok=True)
    result = {"status": "failed", "runtime": runtime_provenance(), "arxiv_id": args.arxiv_id,
              "authorization": "最多一条路线；20 元内；无邮件、生产状态或自动续跑",
              "production_v0_accepted": False}
    result["model_request_policy"] = request_policy(resolve_model("sub"))
    raw, model = build_client("sub")
    try:
        guarded = SampleBudgetClient(raw, path=directory / "cost.json", budget_cny=args.budget_cny,
                                    verified_on=date.fromisoformat(args.pricing_verified_on), model=model)
        try:
            with tempfile.TemporaryDirectory(prefix="radar-one-route-") as temporary:
                await asyncio.wait_for(run_sample(args.arxiv_id, guarded, Path(temporary), result), 1200)
        except Exception as exc:
            result["error_type"] = type(exc).__name__
            # Do not persist HTTP/SDK exception bodies or credentials.
            result["failure_summary"] = "单路线验收未通过；查看已脱敏阶段审计，不发送报告。"
        finally:
            result["cost"] = guarded.summary()
            result["llm_calls"] = run_audit.llm_calls
            (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            run_audit.write({"sample_only": True, "production_v0_accepted": False},
                            status=result["status"], output_dir=directory)
        print(json.dumps({key: result.get(key) for key in ("status", "error_type", "screening_decision", "route_decision")}, ensure_ascii=False))
        print(json.dumps({"request_count": len(guarded.calls), "accounted_upper_cny": str(guarded.spent), "usage_unknown": guarded.unknown}, ensure_ascii=False))
        return 0 if result["status"] != "failed" else 1
    finally:
        await raw.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arxiv-id", required=True)
    parser.add_argument("--budget-cny", required=True)
    parser.add_argument("--pricing-verified-on", required=True)
    parser.add_argument("--live-authorized", action="store_true")
    args = parser.parse_args(argv)
    if not args.live_authorized or not re.fullmatch(r"\d{4}\.\d{4,5}", args.arxiv_id):
        parser.error("须明确授权真实模型并提供单个无版本 arXiv ID")
    return asyncio.run(execute(args))


if __name__ == "__main__":
    raise SystemExit(main())
