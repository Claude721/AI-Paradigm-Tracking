"""Explicitly authorized, isolated recent-primary/one-route real acceptance.

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
from datetime import date, datetime, timedelta, timezone
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.sample_budget import SampleBudgetClient, SampleBudgetError, PriceContract


def isolate_environment(*, identity_checks=False):
    # No production Secrets/Variables may authorize additional paid services,
    # mail or resumes. Keep both explicitly budgeted model roles, nothing else.
    for key in list(os.environ):
        if key.startswith(("SMTP_", "EMAIL_", "TAVILY_", "TWITTER_", "REDDIT_",
                           "SEMANTIC_SCHOLAR_")) or (key.startswith("OPENALEX_") and not identity_checks) or key in {
                               "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY",
                               "DASHSCOPE_API_KEY", "SUB_AGENT_API_KEY", "MAIN_AGENT_API_KEY"}:
            os.environ.pop(key, None)
    os.environ.update({"AI_RADAR_SKIP_DOTENV": "true", "EMAIL_PUSH_ENABLED": "false",
        "EMAIL_PUSH_REQUIRED": "false", "PARADIGM_AUTO_RESUME_ENABLED": "false",
        "KOL_X_SOURCE_ENABLED": "false", "TAVILY_SOCIAL_SEARCH_ENABLED": "false",
        "SEMANTIC_SCHOLAR_ENABLED": "false", "REDDIT_API_ACCESS_APPROVED": "false",
        "LLM_ENABLE_THINKING": "false"})


def _require_recent_primary(origin, now, lookback_days):
    from paradigms.models import _evidence_datetime
    published = _evidence_datetime(origin.published_at)
    if not published or not now - timedelta(days=lookback_days) <= published <= now:
        raise ValueError("原始发布时间不在授权窗口，或无法核验；不支付模型、不回溯旧样本")


async def _official_origin(url, now, lookback_days):
    """One registered index + one linked article; never arbitrary URL input."""
    import httpx
    from research_watchlist import RESEARCH_SOURCES
    from sources.document_limits import bounded_document_get
    from sources.priority_research_source import (
        PriorityResearchPageSource, _discover_index_links,
    )
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in {None, 443}:
        raise ValueError("样本只能使用稳定的已登记官方 HTTPS 研究文章 URL")
    index = next((record["url"] for record in RESEARCH_SOURCES
                  if urlparse(record["url"]).hostname == parsed.hostname
                  and parsed.path.startswith(urlparse(record["url"]).path.rstrip("/") + "/")), "")
    if not index:
        raise ValueError("文章不属于已登记官方研究目录；不猜测发布者身份")
    async with httpx.AsyncClient(timeout=35, follow_redirects=False) as client:
        response = await bounded_document_get(client, index)
        response.raise_for_status()
        links = _discover_index_links(response.text, index)
        link = next((value for value in links if value.url == url and not value.fetch_url and not value.body_hint), None)
        if link is None:
            raise ValueError("官方索引未包含本次材料；不猜测或拼接原文链接")
        response = await bounded_document_get(client, url)
        response.raise_for_status()
    source = PriorityResearchPageSource(pages=[index], lookback_days=lookback_days)
    source.page_coverage[index] = {"detail_failures": 0, "detail_parsed": 0, "detail_outside_window": 0}
    origin = source._parse_detail(index, link, response, now - timedelta(days=lookback_days), {})
    if origin is None or origin.url != url:
        raise ValueError("官方正文、原始日期或输入身份未通过生产解析契约")
    _require_recent_primary(origin, now, lookback_days)
    origin.raw["document_excerpt"] = origin.summary
    origin.raw["document_source_url"] = origin.url
    return origin


async def run_sample(arxiv_id: str, guarded, directory: Path, result: dict, *, reference_time=None,
                     lookback_days=3, primary_url="", main_guarded=None, identity_requests=0):
    # Late imports ensure .env/production configuration cannot influence this
    # explicitly isolated test. Use real stage implementations, never mocks.
    import httpx
    from agents.paradigm_orchestrator import ParadigmOrchestrator
    from agents.llm_utils import resolve_model
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
    result["lookback_days"] = lookback_days
    result["window_start"] = (now - timedelta(days=lookback_days)).isoformat()
    with research_window(now, lookback_days=lookback_days):
        if primary_url:
            origin = await _official_origin(primary_url, now, lookback_days)
        else:
            source = ArxivSource(seed_arxiv_ids=[arxiv_id], reference_time=now)
            async with httpx.AsyncClient(timeout=35) as client:
                rows = await source._fetch_seed_ids(client, [arxiv_id])
            if len(rows) != 1:
                raise ValueError("精确原点输入身份/基数未闭合")
            origin = _raw_to_origin(rows[0])
            if origin.identifiers.get("arxiv") != arxiv_id:
                raise ValueError("返回了非本次授权材料")
        _require_recent_primary(origin, now, lookback_days)
        result["primary"] = {"title": origin.title, "url": origin.url,
                             "published_at": origin.published_at, "authors": origin.authors}
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
        values = await ParadigmAnalyzer(concurrency=1, client=guarded, model=resolve_model("sub").model).run(origins)
        if len(values) != 1 or values[0].evidence.fingerprint != origins[0].fingerprint:
            raise ValueError("模型返回多个机制或不匹配原点，停止而非截断")
        extraction = values[0]
        result["screening_explanation"] = getattr(extraction, "technical_explanation", "")
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
        if main_guarded is None:
            raise ValueError("MAIN 验收客户端未配置；不借用 SUB")
        candidate = candidates[0]
        candidate.status = "pending_deep"
        store.save_candidates(candidates)
        orchestrator = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
        orchestrator.delivery_contract_version = 2
        orchestrator.store, orchestrator.deep_concurrency = store, 1
        orchestrator.enricher = EvidenceEnricher(concurrency=1, budget_store=store.provider_budgets)
        orchestrator.enricher.researchers.openalex_request_limit = identity_requests
        orchestrator.enricher.allow_partial_sources = True
        main_model = resolve_model("main").model
        orchestrator.synthesizer = ParadigmSynthesizer(concurrency=1, client=main_guarded, model=main_model)
        orchestrator.trajectory = ResearcherTrajectoryAnalyzer(concurrency=1, client=main_guarded,
                                                            model=main_model, stage_cache=store.campaigns)
        completed, budget_pending, failed = await orchestrator._deep_analyze_in_batches(
            candidates, [], time.monotonic() + 900)
        result["identity_requests"] = orchestrator.enricher.researchers.openalex_request_count
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
        result["people"] = [{"name": person.name, "role": person.role,
                             "background": person.background_summary,
                             "trajectory": person.research_trajectory,
                             "contacts": person.public_contacts,
                             "contact_lookup_completed": person.contact_lookup_completed,
                             "search_notes": person.contact_search_notes} for person in candidate.researchers]
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
        generator = ParadigmReportGenerator(output_dir=directory, client=main_guarded, model=main_model)
        from runtime_clock import scheduled_date
        draft = await generator._draft_one_route(scheduled_date(now), candidate, index=1, total=1)
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
    isolate_environment(identity_checks=args.identity_requests > 0 and not args.models_only)
    from agents.llm_utils import build_client, request_policy, resolve_model
    from run_audit import run_audit
    from runtime_provenance import runtime_provenance
    profiles = {role: resolve_model(role) for role in ("sub", "main")}
    if any(not profile.api_key or profile.api_key in {"placeholder", "offline-test-only", "ollama"}
           for profile in profiles.values()):
        raise SampleBudgetError("MAIN/SUB 须分别配置真实模型 Key；不要在聊天中提供密钥")
    directory = Path("logs/one_route")
    directory.mkdir(parents=True, exist_ok=True)
    result = {"status": "failed", "runtime": runtime_provenance(), "arxiv_id": args.arxiv_id,
              "primary_url": args.primary_url,
              "authorization": "最多一条路线；20 元内；无邮件、生产状态或自动续跑",
              "production_v0_accepted": False}
    result["model_request_policy"] = {role: request_policy(profile) for role, profile in profiles.items()}
    clients = {}
    guarded = None
    try:
        prices = json.loads(args.price_contracts)
        if not isinstance(prices, dict) or set(prices) != {"sub", "main"}:
            raise SampleBudgetError("price_contracts 须包含 sub/main 两个价格契约")
        verified_on = date.fromisoformat(args.pricing_verified_on)
        for role in ("sub", "main"):
            clients[role], _ = build_client(role)
            contract = PriceContract.parse(prices[role], model=profiles[role].model,
                                          base_url=profiles[role].base_url, verified_on=verified_on)
            SampleBudgetClient._validate_transport(clients[role], contract)
        guarded = SampleBudgetClient(clients["sub"], path=directory / "cost.json", budget_cny=args.budget_cny,
                                    verified_on=verified_on, price_contract=prices["sub"],
                                    model=profiles["sub"].model, base_url=profiles["sub"].base_url)
        main_guarded = guarded.bind_client(clients["main"], role="main", model=profiles["main"].model,
                                          base_url=profiles["main"].base_url, price_contract=prices["main"])
        try:
            if args.models_only:
                from smokecheck import _check_models, smoke_failed
                transports = {"sub": guarded, "main": main_guarded}
                checks = await _check_models(client_factory=lambda role: (transports[role], profiles[role].model))
                result["smoke_results"] = [asdict(check) for check in checks]
                result["status"] = "failed" if smoke_failed(checks) else "model_smoke_passed"
            else:
                with tempfile.TemporaryDirectory(prefix="radar-one-route-") as temporary:
                    await asyncio.wait_for(run_sample(args.arxiv_id, guarded, Path(temporary), result,
                        primary_url=args.primary_url, lookback_days=args.lookback_days,
                        main_guarded=main_guarded, identity_requests=args.identity_requests), 1200)
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
            if result["status"] not in {"failed", "model_smoke_passed"}:
                _write_sample_review(result, directory)
        print(json.dumps({key: result.get(key) for key in ("status", "error_type", "screening_decision", "route_decision")}, ensure_ascii=False))
        print(json.dumps({"request_count": len(guarded.calls), "accounted_upper_cny": str(guarded.spent), "usage_unknown": guarded.unknown}, ensure_ascii=False))
        return 0 if result["status"] != "failed" else 1
    except Exception as exc:
        result.update(error_type=type(exc).__name__, failure_summary="配置/费用契约未通过，不发送报告。")
        if guarded is not None:
            result["cost"] = guarded.summary()
        (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}, ensure_ascii=False))
        return 1
    finally:
        for client in clients.values():
            await client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--arxiv-id", default="")
    selection.add_argument("--primary-url", default="")
    parser.add_argument("--lookback-days", type=int, choices=(3, 7), default=3)
    parser.add_argument("--budget-cny", required=True)
    parser.add_argument("--pricing-verified-on", required=True)
    parser.add_argument("--price-contracts", default=os.getenv("SAMPLE_PRICE_CONTRACTS", ""))
    parser.add_argument("--models-only", action="store_true")
    parser.add_argument("--identity-requests", type=int, choices=range(9), default=0)
    parser.add_argument("--live-authorized", action="store_true")
    args = parser.parse_args(argv)
    if (not args.live_authorized or (args.arxiv_id and not re.fullmatch(r"\d{4}\.\d{4,5}", args.arxiv_id))
        or (args.models_only and (args.arxiv_id or args.primary_url))
        or (not args.models_only and not (args.arxiv_id or args.primary_url))):
        parser.error("须明确授权并提供单个无版本 arXiv ID 或官方文章 URL")
    return asyncio.run(execute(args))


def _write_sample_review(result, directory):
    """Completed sample verdict, never a pending/phase memo or empty week report."""
    primary = result["primary"]
    decision = result.get("route_decision") or result.get("screening_decision")
    label = {"report": "符合路线准入", "observe": "保留观察，不作为新路线交付",
             "reject": "不符合路线准入"}.get(decision)
    if not label or result.get("status") not in {"screening_closed", "route_judgment_closed", "route_quality_passed"}:
        raise ValueError("未完成样本判断，不能生成研究结论")
    reason = result.get("route_reason") or result.get("screening_reason", "")
    if reason.startswith("Rubric 确定性得分"):
        reason = ("当前证据未达到路线准入要求。主要证据缺口：" + reason.split("；主要未闭合问题：", 1)[1]
                  if "；主要未闭合问题：" in reason else "当前机制证据未达到路线准入要求。")
    text = result.get("route_test_draft") or "\n\n".join(value for value in (
        reason,
        result.get("explanation") or result.get("screening_explanation"),
        "**讨论势能判断：**" + result["discussion_judgment"] if result.get("discussion_judgment") else "",
    ) if value)
    content = (f"# {result['lookback_days']} 天窗口研究样本结论\n\n"
        f"本次选定材料为[{primary['title']}]({primary['url']})，原始发布时间为 {primary['published_at']}。"
        f"对这份材料的判断是：{label}。\n\n{text}\n\n"
        "本结论仅覆盖这一份已完成判断的材料，不推断整个窗口没有重要技术，也不声称所有平台已经覆盖。"
        "这是独立研究测试，不是已发送的正式周报。\n")
    coverage = result.get("verification_summary", {}).get("community_coverage", {})
    blind = [value for key, value in coverage.items() if key in {"tavily_social_web", "reddit_official", "x_official"}]
    if blind:
        content += "\n本次传播检索边界：" + "；".join(blind) + "。不能据此断言未覆盖平台没有讨论。\n"
    (directory / "sample_review.md").write_text(content, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
