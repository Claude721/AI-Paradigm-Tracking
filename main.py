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
import sys
from contextlib import contextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import config
from runtime_clock import scheduled_date


def _redacted_log_text(value: str) -> str:
    secret_names = (
        "LLM_API_KEY",
        "SUB_AGENT_API_KEY",
        "MAIN_AGENT_API_KEY",
        "OPENALEX_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY",
        "GITHUB_TOKEN",
        "TWITTER_BEARER_TOKEN",
        "TAVILY_API_KEY",
        "REDDIT_CLIENT_SECRET",
        "SMTP_PASSWORD",
    )
    result = value
    for name in secret_names:
        secret = str(getattr(config, name, "") or "")
        if len(secret) < 6:
            continue
        result = result.replace(secret, "***")
        encoded = quote(secret, safe="")
        if encoded != secret:
            result = result.replace(encoded, "***")
    return result


class _SecretRedactingFormatter(logging.Formatter):
    """Redact configured secrets from messages and formatted tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return _redacted_log_text(super().format(record))


def setup_logging() -> None:
    log_formatter = _SecretRedactingFormatter(
        "%(asctime)s | %(levelname)-7s | %(name)-25s | %(message)s",
        datefmt="%H:%M:%S"
    )
    
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(log_formatter)
    handlers = [stream_handler]
    
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


@contextmanager
def _pipeline_lock():
    """Prevent two local processes from mutating one SQLite/outbox concurrently."""

    db_path = (
        config.DB_PATH
        if config.PIPELINE_MODE == "legacy"
        else config.PARADIGM_DB_PATH
    )
    lock_path = db_path.with_suffix(db_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    lock_backend = ""
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_backend = "fcntl"
        except ImportError:
            import msvcrt

            handle.seek(0)
            if not handle.read(1):
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            lock_backend = "msvcrt"
        except BlockingIOError as exc:
            raise RuntimeError("已有另一份 AI Radar 流水线正在运行") from exc
        except OSError as exc:
            raise RuntimeError("已有另一份 AI Radar 流水线正在运行") from exc
        yield
    finally:
        try:
            if lock_backend == "fcntl":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif lock_backend == "msvcrt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        handle.close()


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


async def _deliver_paradigm_job(store, generator, job, *, recovered: bool) -> dict:
    """渲染并投递一个持久化 outbox 任务，不重新执行研究。"""

    from notifications.email_notifier import send_report_email
    from run_audit import run_audit

    stats = dict(job.stats)
    stats["delivery_key"] = job.delivery_key
    stats["delivery_recovered"] = recovered
    if job.status == "delivered":
        stats["delivery_duplicate_skipped"] = True
        run_audit.event(
            "report_outbox",
            "duplicate_skipped",
            f"交付任务 {job.delivery_key[:12]} 已完成，不重复发送邮件",
        )
        stats.update(run_audit.write(stats, status="duplicate_skipped"))
        stats["email_sent"] = False
        return stats

    report_path = generator.output_dir / job.report_name
    report_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = bool(job.report_content)
    if rendered:
        report_path.write_text(job.report_content, encoding="utf-8")
        run_audit.event(
            "report_outbox",
            "reused",
            f"复用已通过质量门槛的报告制品 {job.delivery_key[:12]}",
        )
    else:
        try:
            route_fragments = store.load_report_fragments(job.delivery_key)
            if route_fragments:
                run_audit.event(
                    "report_outbox",
                    "fragments_restored",
                    f"恢复 {len(route_fragments)} 条已通过质量闸门的路线草稿",
                )
            report_path = await asyncio.wait_for(
                generator.generate(
                    job.candidates,
                    stats,
                    report_date=job.report_date,
                    route_fragments=route_fragments,
                    save_route_fragment=lambda fragment_key, content: (
                        store.save_report_fragment(
                            job.delivery_key,
                            fragment_key,
                            content,
                        )
                    ),
                ),
                timeout=config.PARADIGM_REPORT_TIMEOUT_SECONDS,
            )
            store.save_rendered_report(
                job.delivery_key,
                report_path.read_text(encoding="utf-8"),
            )
            rendered = True
        except Exception as exc:
            store.record_delivery_failure(
                job.delivery_key, exc, rendered=False
            )
            run_audit.event(
                "report_outbox",
                "render_failed",
                f"报告任务 {job.delivery_key[:12]} 渲染失败，已保留候选快照",
            )
            raise

    stats["report_path"] = str(report_path)
    audit_summary = run_audit.write(
        stats,
        status=(
            "recovered_delivery"
            if recovered
            else "completed_with_backlog"
            if stats.get("run_incomplete")
            else "completed"
        ),
    )
    stats.update(audit_summary)
    stats["audit_attachments"] = [
        audit_summary["audit_markdown_path"],
        "logs/current_run.log",
    ]

    if not config.EMAIL_PUSH_ENABLED:
        store.mark_delivery_delivered(job.delivery_key, report_path)
        stats["email_sent"] = False
        return stats

    store.begin_delivery_attempt(job.delivery_key)
    try:
        email_sent = await send_report_email(
            report_path,
            stats,
            delivery_key=job.delivery_key,
        )
    except Exception as exc:
        store.record_delivery_failure(job.delivery_key, exc, rendered=rendered)
        run_audit.event(
            "report_outbox",
            "send_failed",
            f"报告任务 {job.delivery_key[:12]} 邮件失败，已保留报告制品",
        )
        raise
    stats["email_sent"] = email_sent
    if email_sent:
        store.mark_delivery_delivered(job.delivery_key, report_path)
    else:
        store.record_delivery_failure(
            job.delivery_key,
            "SMTP 未确认发送，保留为待交付",
            rendered=rendered,
        )
    return stats


async def _run_pipeline_once() -> dict:
    """执行一次流水线；研究检查点与正式交付使用独立状态。"""

    from run_audit import run_audit

    run_audit.reset()
    _check_env()
    _print_model_banner()
    if config.PIPELINE_MODE == "legacy":
        from agents.orchestrator import Orchestrator

        orchestrator = Orchestrator()
    else:
        from agents.paradigm_orchestrator import ParadigmOrchestrator
        from reports.paradigm_generator import ParadigmReportGenerator

        orchestrator = ParadigmOrchestrator()
        generator = ParadigmReportGenerator()
        pending_job = orchestrator.store.load_pending_report_job()
        if pending_job is not None:
            logger.warning(
                "发现未完成交付 %s（状态=%s），本次只复用研究结果完成报告/邮件",
                pending_job.delivery_key[:12],
                pending_job.status,
            )
            result = await _deliver_paradigm_job(
                orchestrator.store,
                generator,
                pending_job,
                recovered=True,
            )
            result["recovered_delivery_only"] = True
            run_audit.event(
                "report_outbox",
                "recovered",
                f"已完成历史待交付任务 {pending_job.delivery_key[:12]}；"
                "为避免恢复耗时与新研究叠加触发云端硬超时，本次不再启动新研究",
            )
            return result

    stats = await orchestrator.run()
    if config.PIPELINE_MODE != "legacy":
        report_date = scheduled_date()
        job = orchestrator.store.enqueue_report(
            orchestrator.pending_delivery,
            stats,
            report_date=report_date,
        )
        return await _deliver_paradigm_job(
            orchestrator.store,
            generator,
            job,
            recovered=False,
        )

    report_path = stats.get("report_path")
    if not report_path:
        report_path = await orchestrator.report_gen.generate(
            orchestrator.store.get_today_projects(),
            orchestrator.store.get_stats(),
            stats,
        )
        stats["report_path"] = str(report_path)

    audit_summary = run_audit.write(stats)
    stats.update(audit_summary)
    stats["audit_attachments"] = [
        audit_summary["audit_markdown_path"],
        "logs/current_run.log",
    ]
    from notifications.email_notifier import send_report_email

    stats["email_sent"] = await send_report_email(Path(report_path), stats)
    return stats


async def run_pipeline() -> dict:
    """执行流水线；失败时保留研究检查点和未完成交付供下次续跑。"""

    try:
        with _pipeline_lock():
            return await _run_pipeline_once()
    except Exception:
        logger.exception("本次任务失败；保留已完成研究检查点供下次续跑")
        from run_audit import run_audit

        try:
            run_audit.event(
                "pipeline",
                "failed",
                "本轮失败；研究检查点未回滚，未成功发送的报告仍处于待交付状态",
            )
            run_audit.write(
                run_audit.last_stats
                or {"pipeline_mode": config.PIPELINE_MODE},
                status="failed",
            )
        except Exception:
            # 审计属于故障证据，写盘失败必须可见，但不能覆盖原始业务异常。
            logger.exception("失败审计写盘失败")
        raise


async def regenerate_report() -> dict:
    """续投待交付任务，或基于最近已交付候选重新生成报告。"""
    with _pipeline_lock():
        return await _regenerate_report_once()


async def _regenerate_report_once() -> dict:
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
        pending_job = store.load_pending_report_job()
        if pending_job is not None:
            logger.info(
                "--report 发现待交付任务 %s，优先复用研究快照/报告制品续投",
                pending_job.delivery_key[:12],
            )
            return await _deliver_paradigm_job(
                store, generator, pending_job, recovered=True
            )
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
        help="优先续投失败报告；否则不拉取新数据，重生成最近报告并发送邮件",
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
        from healthcheck import blocking_checks, print_checks

        checks = print_checks()
        if blocking_checks(checks):
            raise SystemExit(1)
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
