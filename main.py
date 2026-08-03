"""
AI Deal Sourcing Agent - 一键运行入口
AI 技术范式捕捉与关键人物追踪系统

Usage:
    python main.py              # 立即执行一次完整的 sourcing pipeline
    python main.py --setup      # 交互式配置环境变量（首次使用推荐）
    python main.py --status     # 查看当前配置状态
    python main.py --schedule   # 启动定时任务模式（默认每周五 09:00）
    python main.py --report     # 仅重新生成今日报告（不重新拉取数据）
    python main.py --doctor     # 零网络静态配置检查
    python main.py --smoke-test # 小成本真实接口检查，不发送邮件
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
import tempfile
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import config


def setup_logging() -> None:
    log_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-25s | %(message)s",
        datefmt="%H:%M:%S"
    )
    
    handlers = [logging.StreamHandler(sys.stdout)]
    
    # 添加滚动文件日志支持
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        filename=log_dir / "sourcing.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8"
    )
    file_handler.setFormatter(log_formatter)
    handlers.append(file_handler)

    # 每次命令单独覆盖一份本轮日志，便于邮件附件和 Actions 审计；滚动日志
    # 仍保留 30 天用于本机连续排查。
    current_file_handler = logging.FileHandler(
        filename=log_dir / "current_run.log",
        mode="w",
        encoding="utf-8",
    )
    current_file_handler.setFormatter(log_formatter)
    handlers.append(current_file_handler)

    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        handlers=handlers,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


logger = logging.getLogger("main")


def _check_env() -> None:
    """启动前检查关键环境变量"""
    from agents.llm_utils import resolve_all

    resolved_models = resolve_all()
    missing_roles = [
        model.role
        for model in resolved_models
        if model.provider != "ollama" and model.api_key == "placeholder"
    ]
    if missing_roles:
        logger.warning(
            "以下 Agent 的 API Key 未配置，分析将失败: %s。"
            "请在 .env 中设置对应 API Key，或运行 python main.py --setup",
            ", ".join(missing_roles),
        )
    if not config.GITHUB_TOKEN:
        logger.info(
            "GITHUB_TOKEN 未配置，技术范式流水线会跳过 GitHub Search，"
            "不会匿名消耗共享限额"
        )


def _print_model_banner() -> None:
    """启动时打印模型解析摘要"""
    from agents.llm_utils import resolve_all
    sub, main = resolve_all()
    logger.info("模型配置解析完成:")
    for line in sub.summary_lines():
        logger.info(line)
    for line in main.summary_lines():
        logger.info(line)
    if sub.label == main.label:
        logger.info(f"  (子Agent 与 主Agent 使用同一模型: {sub.label})")


async def _run_pipeline_once() -> dict:
    """执行一次流水线；由 run_pipeline 负责失败回滚。"""
    from run_audit import run_audit

    run_audit.reset()
    _check_env()
    _print_model_banner()
    if config.PIPELINE_MODE == "legacy":
        from agents.orchestrator import Orchestrator
        orchestrator = Orchestrator()
    else:
        from agents.paradigm_orchestrator import ParadigmOrchestrator
        orchestrator = ParadigmOrchestrator()
    stats = await orchestrator.run()
    report_path = stats.get("report_path")
    if not report_path:
        # 即使本周没有新项目，也生成并推送一份空报告，便于确认定时任务正常执行。
        if config.PIPELINE_MODE == "legacy":
            report_path = await orchestrator.report_gen.generate(
                orchestrator.store.get_today_projects(),
                orchestrator.store.get_stats(),
                stats,
            )
        else:
            report_path = await orchestrator.report_gen.generate([], stats)
        stats["report_path"] = str(report_path)

    audit_summary = run_audit.write(
        stats,
        status=(
            "completed_with_backlog"
            if stats.get("run_incomplete")
            else "completed"
        ),
    )
    stats.update(audit_summary)
    stats["audit_attachments"] = [
        audit_summary["audit_markdown_path"],
        "logs/current_run.log",
    ]

    from notifications.email_notifier import send_report_email
    email_sent = await send_report_email(Path(report_path), stats)
    stats["email_sent"] = email_sent
    if config.PIPELINE_MODE != "legacy":
        # 开启邮件时，只有实际发送成功才算交付；关闭邮件时仍保留原有的
        # “生成本地报告即交付”语义。
        if email_sent or not config.EMAIL_PUSH_ENABLED:
            orchestrator.store.mark_reported(
                orchestrator.pending_delivery, Path(report_path)
            )
    return stats


async def run_pipeline() -> dict:
    """执行流水线；失败时回滚本次状态，保证修复后可以完整重试。"""
    db_path = (
        config.DB_PATH
        if config.PIPELINE_MODE == "legacy"
        else config.PARADIGM_DB_PATH
    )
    existed_before = db_path.exists()
    with tempfile.TemporaryDirectory(prefix="ai-sourcing-state-") as directory:
        backup_path = Path(directory) / db_path.name
        if existed_before:
            shutil.copy2(db_path, backup_path)
        try:
            return await _run_pipeline_once()
        except Exception:
            logger.exception("本次任务失败，正在回滚本次数据库状态")
            from run_audit import run_audit

            run_audit.event("pipeline", "failed", "本轮失败并回滚数据库状态")
            run_audit.write(
                run_audit.last_stats
                or {"pipeline_mode": config.PIPELINE_MODE},
                status="failed",
            )
            if existed_before:
                shutil.copy2(backup_path, db_path)
            else:
                db_path.unlink(missing_ok=True)
            raise


async def regenerate_report() -> dict:
    """仅重新生成报告（不拉取新数据）"""
    from run_audit import run_audit

    run_audit.reset()
    if config.PIPELINE_MODE == "legacy":
        from database.store import ProjectStore
        from reports.generator import ReportGenerator
        store = ProjectStore()
        generator = ReportGenerator()
        stats = store.get_stats()
        report_path = await generator.generate(
            store.get_today_projects(), stats
        )
    else:
        from database.paradigm_store import ParadigmStore
        from reports.paradigm_generator import ParadigmReportGenerator
        store = ParadigmStore()
        generator = ParadigmReportGenerator()
        candidates = store.latest_reported_candidates()
        stats = store.stats()
        stats["new_paradigms"] = sum(
            item.report_kind == "new" for item in candidates
        )
        stats["updated_paradigms"] = sum(
            item.report_kind == "update" for item in candidates
        )
        report_path = await generator.generate(candidates, stats)
    logger.info(f"报告已重新生成: {report_path}")
    from notifications.email_notifier import send_report_email
    stats["report_path"] = str(report_path)
    audit_summary = run_audit.write(stats)
    stats.update(audit_summary)
    stats["audit_attachments"] = [
        audit_summary["audit_markdown_path"],
        "logs/current_run.log",
    ]
    stats["email_sent"] = await send_report_email(Path(report_path), stats)
    return stats


async def _run_scheduler() -> None:
    """在当前事件循环中启动每周定时任务。"""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    _check_env()
    timezone = ZoneInfo(config.SCHEDULE_TIMEZONE)
    logger.info(
        "定时任务启动: 每周 %s %02d:%02d (%s)，回看 %s 天",
        config.SCHEDULE_DAY_OF_WEEK,
        config.SCHEDULE_HOUR,
        config.SCHEDULE_MINUTE,
        config.SCHEDULE_TIMEZONE,
        config.SOURCING_LOOKBACK_DAYS,
    )
    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(
        run_pipeline,
        "cron",
        day_of_week=config.SCHEDULE_DAY_OF_WEEK,
        hour=config.SCHEDULE_HOUR,
        minute=config.SCHEDULE_MINUTE,
        id="weekly_sourcing",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)


def run_scheduler() -> None:
    """启动定时任务模式。"""
    try:
        asyncio.run(_run_scheduler())
    except (KeyboardInterrupt, SystemExit):
        logger.info("定时任务已停止")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Paradigm Radar — 技术范式捕捉与关键人物追踪系统"
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="交互式配置环境变量（首次使用推荐）",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="查看当前环境变量配置状态",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="启动定时任务模式（默认每周五 09:00 自动执行）",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="不拉取新数据，重新生成最近报告并按配置发送邮件",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="只做配置体检，不请求任何外部 API",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="小成本真实检查各接口；不会运行完整流水线或发送邮件",
    )
    parser.add_argument(
        "--smoke-skip-llm",
        action="store_true",
        help="smoke test 中跳过 Qwen 请求",
    )
    parser.add_argument(
        "--smoke-skip-smtp",
        action="store_true",
        help="smoke test 中跳过 SMTP 连接与登录",
    )
    parser.add_argument(
        "--smoke-skip-tavily",
        action="store_true",
        help="smoke test 中跳过 Tavily，避免重复消耗 basic request credit",
    )
    parser.add_argument(
        "--notify-failure",
        action="store_true",
        help="仅发送云端任务失败提醒；不运行研究流水线",
    )
    args = parser.parse_args()

    if args.setup:
        from setup_env import run_setup
        run_setup()
    elif args.status:
        from setup_env import _print_status
        _print_status()
    elif args.schedule:
        setup_logging()
        run_scheduler()
    elif args.report:
        setup_logging()
        asyncio.run(regenerate_report())
    elif args.doctor:
        from healthcheck import print_checks
        print_checks()
    elif args.smoke_test:
        setup_logging()
        from smokecheck import print_smoke_results, run_smoke_checks, smoke_failed

        results = asyncio.run(
            run_smoke_checks(
                include_llm=not args.smoke_skip_llm,
                include_smtp=not args.smoke_skip_smtp,
                include_tavily=not args.smoke_skip_tavily,
            )
        )
        print_smoke_results(results)
        if smoke_failed(results):
            raise SystemExit(1)
    elif args.notify_failure:
        from notifications.email_notifier import send_failure_email

        asyncio.run(send_failure_email())
    else:
        setup_logging()
        asyncio.run(run_pipeline())


if __name__ == "__main__":
    main()
