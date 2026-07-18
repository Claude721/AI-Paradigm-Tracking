"""
Orchestrator — 多智能体流水线编排器（AgentSwarm 模式）

工作流程：
  Sourcing → QuantFilter → 增量闸门 → Triage → 专家分析（按类型路由）
  → Deep Analysis（综合专家意见）→ Critique（靶向追问）→ Store + LLM Report

核心思想：
  - 规则层（QuantFilter）做定量淘汰，并标注 project_type（tech/app）
  - 增量闸门避免重复 LLM 调用
  - 子 Agent（Triage）快速过滤噪声
  - 专家 Agent 组（按 tech/app 路由）提供多视角分析
  - 主 Agent（Deep Analysis）综合专家意见做最终判定
  - Critique Agent 对高分项目靶向追问，信息蒸馏
  - 报告生成器使用 LLM 叙事 + 模板混合
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import config
from sources.follow_builders_selfhost import run_feed_generator
from agents.sourcing_agent import SourcingAgent
from agents.quant_filter import QuantFilter
from agents.triage_agent import TriageAgent
from agents.expert_agent import ExpertAgent, ExpertOpinion
from agents.deep_agent import DeepAnalysisAgent, ScoredProject
from agents.critique_agent import CritiqueAgent, EnrichedProject
from database.store import ProjectStore
from reports.generator import ReportGenerator

logger = logging.getLogger(__name__)


def scored_to_dict(sp: ScoredProject) -> dict:
    """将 ScoredProject 转为字典以便存储"""
    return {
        "source": sp.raw.source,
        "name": sp.raw.name,
        "url": sp.raw.url,
        "score": sp.score,
        "one_liner": sp.one_liner,
        "innovation": sp.innovation,
        "key_design": sp.key_design,
        "risks": sp.risks,
        "ai_integration": sp.ai_integration,
        "founder_guess": sp.founder_guess,
        "category": sp.category,
        "reasoning": sp.reasoning,
        "stars": sp.raw.stars,
        "language": sp.raw.language,
        "topics": sp.raw.topics,
        "author": sp.raw.author,
        "created_at": sp.raw.created_at,
    }


def enriched_to_dict(ep: EnrichedProject) -> dict:
    """将 EnrichedProject 转为字典（含追问和 memo）"""
    d = scored_to_dict(ep.scored)
    d["critique_qa"] = ep.critique_qa
    d["mini_memo"] = ep.mini_memo
    return d


class Orchestrator:
    """多智能体流水线编排器"""

    def __init__(
        self,
        triage_concurrency: int = 8,
        deep_concurrency: int = 3,
        critique_concurrency: int = 3,
    ):
        self.sourcing_agent = SourcingAgent()
        self.quant_filter = QuantFilter()
        self.triage_agent = TriageAgent(concurrency=triage_concurrency)

        expert_concurrency = config.EXPERT_CONCURRENCY
        self.expert_tech = ExpertAgent(
            "expert_tech_landing",
            role=config.EXPERT_TECH_ROLE,
            concurrency=expert_concurrency,
        )
        self.expert_app_landing = ExpertAgent(
            "expert_app_landing",
            role=config.EXPERT_APP_LANDING_ROLE,
            concurrency=expert_concurrency,
        )
        self.expert_app_user = ExpertAgent(
            "expert_app_user",
            role=config.EXPERT_APP_USER_ROLE,
            concurrency=expert_concurrency,
        )

        self.deep_agent = DeepAnalysisAgent(concurrency=deep_concurrency)
        self.critique_agent = CritiqueAgent(concurrency=critique_concurrency)
        self.store = ProjectStore()
        self.report_gen = ReportGenerator()

    async def run(self) -> dict:
        start = datetime.now(timezone.utc)
        stats: dict = {}

        # ── Phase 0: Self-host feed generation (if enabled) ─
        if config.FOLLOW_BUILDERS_SELF_HOST:
            logger.info("=" * 60)
            logger.info("[Phase 0] Follow Builders 自建模式：生成最新 Feed...")
            logger.info("=" * 60)
            ok = await run_feed_generator()
            if ok:
                from pathlib import Path
                local_feed_dir = Path(config.BASE_DIR) / "social_media_sourcing" / "follow-builders"
                config.FOLLOW_BUILDERS_FEED_URL = f"file://{local_feed_dir}"
                logger.info(f"  Feed 已生成，Source 将从本地目录读取: {local_feed_dir}")
            else:
                logger.warning("  自建 Feed 生成失败，将回退到方案 A（读取 GitHub）")

        # ── Phase 1: Sourcing ─────────────────────────────
        logger.info("=" * 60)
        logger.info("[Phase 1/8] 信源并发获取中...")
        logger.info("=" * 60)
        raw_projects = await self.sourcing_agent.run()
        stats["raw_count"] = len(raw_projects)

        if not raw_projects:
            logger.warning("未获取到任何项目，Pipeline 终止")
            return stats

        # ── Phase 2: Quant Filter ─────────────────────────
        logger.info("")
        logger.info("=" * 60)
        logger.info("[Phase 2/8] 定量预过滤（LLM 前置）")
        logger.info("=" * 60)
        candidates = self.quant_filter.run(raw_projects)
        stats["quant_passed"] = len(candidates)
        if not candidates:
            logger.warning("定量预过滤后无项目，Pipeline 终止")
            return stats

        # ── Phase 3: 智能增量闸门 ─────────────────────────
        logger.info("")
        logger.info("=" * 60)
        logger.info("[Phase 3/8] 智能增量（URL+签名，LLM 前）")
        logger.info("=" * 60)
        deduped, inc_stats = self.store.plan_incremental_candidates(candidates)
        stats["inc_new"] = inc_stats["new"]
        stats["inc_changed"] = inc_stats["changed"]
        stats["inc_stale_reanalyze"] = inc_stats["stale_reanalyze"]
        stats["inc_unchanged_skip"] = inc_stats["unchanged_skip"]
        stats["db_dedup_passed"] = len(deduped)
        logger.info(
            "增量闸门完成：new=%s changed=%s stale=%s skip=%s -> 待 LLM %s",
            stats["inc_new"],
            stats["inc_changed"],
            stats["inc_stale_reanalyze"],
            stats["inc_unchanged_skip"],
            len(deduped),
        )
        if not deduped:
            logger.warning("增量闸门后无项目进入 LLM，Pipeline 终止")
            return stats

        # ── Phase 4: Triage (子 Agent) ────────────────────
        logger.info("")
        logger.info("=" * 60)
        logger.info(
            f"[Phase 4/8] 子 Agent 初筛（{stats['db_dedup_passed']} 个项目）"
        )
        logger.info("=" * 60)
        triaged = await self.triage_agent.run(deduped)
        stats["triage_passed"] = len(triaged)
        self.store.mark_incremental_analyzed(deduped)

        if not triaged:
            logger.warning("初筛后无项目通过阈值，Pipeline 终止")
            return stats

        # ── Phase 5: Expert Analysis (专家组) ─────────────
        logger.info("")
        logger.info("=" * 60)
        logger.info(
            f"[Phase 5/8] 专家 Agent 多视角分析（{len(triaged)} 个候选项目）"
        )
        logger.info("=" * 60)

        tech_triaged = [
            t for t in triaged
            if t.raw.extra.get("project_type", "tech") == "tech"
        ]
        app_triaged = [
            t for t in triaged
            if t.raw.extra.get("project_type", "tech") == "app"
        ]
        logger.info(
            f"  路由：tech={len(tech_triaged)} 个 → 技术落地专家 | "
            f"app={len(app_triaged)} 个 → 落地可行性专家 + 用户市场专家"
        )

        expert_tasks = []
        if tech_triaged:
            expert_tasks.append(self.expert_tech.run(tech_triaged))
        if app_triaged:
            expert_tasks.append(self.expert_app_landing.run(app_triaged))
            expert_tasks.append(self.expert_app_user.run(app_triaged))

        expert_results = await asyncio.gather(*expert_tasks, return_exceptions=True)

        merged_opinions: dict[str, list[tuple[str, str]]] = {}
        for result in expert_results:
            if isinstance(result, Exception):
                logger.warning(f"专家 Agent 执行异常: {result}")
                continue
            if isinstance(result, dict):
                for url_key, opinion in result.items():
                    merged_opinions.setdefault(url_key, []).append(
                        (opinion.expert_name, opinion.analysis)
                    )

        stats["expert_opinions_count"] = sum(
            len(v) for v in merged_opinions.values()
        )
        logger.info(
            f"专家分析完成：共收集 {stats['expert_opinions_count']} 份专家意见"
        )

        # ── Phase 6: Deep Analysis (主 Agent) ─────────────
        logger.info("")
        logger.info("=" * 60)
        logger.info(
            f"[Phase 6/8] 主 Agent 深度分析（{stats['triage_passed']} 个候选项目，综合专家意见）"
        )
        logger.info("=" * 60)
        self.deep_agent.set_expert_opinions(merged_opinions)
        scored = await self.deep_agent.run(triaged)
        stats["scored_count"] = len(scored)

        # ── Phase 7: Critique (靶向追问) ──────────────────
        logger.info("")
        logger.info("=" * 60)
        logger.info("[Phase 7/8] 靶向追问（Senior Partner 审阅）")
        logger.info("=" * 60)
        enriched = await self.critique_agent.run(scored)
        stats["critique_count"] = sum(
            1 for e in enriched if e.critique_qa
        )

        # ── Phase 8: Store + Report ───────────────────────
        logger.info("")
        logger.info("=" * 60)
        logger.info("[Phase 8/8] 存储 & 报告生成")
        logger.info("=" * 60)

        high_value = [
            e for e in enriched if e.score >= config.SCORE_THRESHOLD
        ]
        stats["high_value_count"] = len(high_value)

        storage_dicts = [scored_to_dict(e.scored) for e in high_value]
        stats["saved_count"] = self.store.save_batch(storage_dicts)

        today_projects = self.store.get_today_projects()
        enriched_map = {
            e.scored.raw.url.strip().lower().rstrip("/"): e
            for e in high_value
        }
        for p in today_projects:
            key = (p.get("url", "") or "").strip().lower().rstrip("/")
            ep = enriched_map.get(key)
            if ep:
                p["critique_qa"] = ep.critique_qa
                p["mini_memo"] = ep.mini_memo
                p["key_design"] = ep.scored.key_design
                p["risks"] = ep.scored.risks

        db_stats = self.store.get_stats()
        report_path = await self.report_gen.generate(
            today_projects, db_stats, stats
        )
        stats["report_path"] = str(report_path)

        # ── Summary ───────────────────────────────────────
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        stats["elapsed_seconds"] = elapsed

        logger.info("")
        logger.info("=" * 60)
        logger.info("Pipeline 执行完毕")
        logger.info(f"  信源获取        : {stats['raw_count']} 个原始项目")
        logger.info(f"  定量预过滤      : {stats['quant_passed']} 个候选项目")
        logger.info(
            f"  智能增量闸门    : new={stats['inc_new']} changed={stats['inc_changed']} "
            f"stale={stats['inc_stale_reanalyze']} skip={stats['inc_unchanged_skip']} "
            f"-> {stats['db_dedup_passed']}"
        )
        logger.info(
            f"  初筛通过        : {stats['triage_passed']} 个"
            f"（阈值 >={config.TRIAGE_THRESHOLD}）"
        )
        logger.info(f"  专家意见        : {stats.get('expert_opinions_count', 0)} 份")
        logger.info(f"  深度分析成功    : {stats['scored_count']} 个")
        logger.info(f"  靶向追问        : {stats['critique_count']} 个项目")
        logger.info(
            f"  高分入库        : {stats['high_value_count']} 个"
            f"（>={config.SCORE_THRESHOLD}）"
        )
        logger.info(f"  新增入库        : {stats['saved_count']} 个")
        logger.info(f"  报告路径        : {stats['report_path']}")
        logger.info(f"  总耗时          : {elapsed:.1f} 秒")
        logger.info("=" * 60)

        if high_value:
            logger.info("\nTop-5 项目预览:")
            for i, e in enumerate(high_value[:5], 1):
                logger.info(
                    f"  {i}. [{e.score}/10] {e.scored.raw.name} — {e.scored.one_liner}"
                )

        return stats
