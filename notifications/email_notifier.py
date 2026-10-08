"""通过标准 SMTP 发送技术范式雷达报告。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

import config
from runtime_clock import scheduled_date
from paradigms.completion import require_completed_research

logger = logging.getLogger(__name__)

_WORKFLOW_STEPS = (
    ("PROVENANCE_STEP_OUTCOME", "provenance", "记录代码版本"),
    ("DEPENDENCIES_STEP_OUTCOME", "dependencies", "安装依赖"),
    ("OFFLINE_CHECKS_STEP_OUTCOME", "offline_checks", "离线回归"),
    ("DOCTOR_STEP_OUTCOME", "doctor", "配置体检"),
    ("RESTORE_STATE_STEP_OUTCOME", "restore_state", "恢复跨周状态"),
    ("SMOKE_STEP_OUTCOME", "smoke", "真实接口冒烟"),
    ("PIPELINE_STEP_OUTCOME", "pipeline", "研究与邮件主流程"),
    ("PREPARE_STATE_STEP_OUTCOME", "prepare_state", "准备状态制品"),
    ("UPLOAD_STATE_STEP_OUTCOME", "upload_state", "上传跨周状态"),
    ("UPLOAD_REPORT_STEP_OUTCOME", "upload_report", "上传报告制品"),
    ("CONTINUATION_STEP_OUTCOME", "continuation", "排队本周新研究"),
    ("UPLOAD_AUDIT_STEP_OUTCOME", "upload_audit", "上传审计制品"),
)


async def send_report_email(
    report_path: Path,
    stats: dict,
    *,
    delivery_key: str = "",
) -> bool:
    """发送报告附件；必需投递模式下失败会让任务明确失败。"""
    if report_path.stem.startswith("paradigm_radar_") or delivery_key or stats.get("pipeline_mode") == "paradigm":
        require_completed_research(stats)
    if not config.EMAIL_PUSH_ENABLED:
        logger.info("邮件推送未启用，报告仅保存在本地")
        return False
    missing = _missing_smtp_config()
    if missing:
        message = "邮件推送配置不完整，缺少: " + ", ".join(missing)
        if config.EMAIL_PUSH_REQUIRED:
            raise RuntimeError(message)
        logger.warning(message)
        return False

    try:
        await asyncio.to_thread(
            _send_sync, report_path, stats, delivery_key=delivery_key
        )
        logger.info("报告邮件已发送至 %s 个收件人", len(config.SMTP_TO))
        return True
    except Exception as exc:
        if config.EMAIL_PUSH_REQUIRED:
            raise RuntimeError(f"报告邮件发送失败: {exc}") from exc
        logger.warning("报告邮件发送失败，报告仍保存在本地: %s", exc)
        return False


async def send_failure_email(context: dict | None = None) -> bool:
    """发送独立于主流水线的失败提醒，供 Actions 的 always 步骤调用。"""
    if not config.EMAIL_PUSH_ENABLED:
        logger.info("邮件推送未启用，跳过任务失败提醒")
        return False
    missing = _missing_smtp_config()
    if missing:
        raise RuntimeError("失败提醒配置不完整，缺少: " + ", ".join(missing))
    payload = {
        "workflow": os.getenv("GITHUB_WORKFLOW", "AI 技术范式雷达"),
        "event": os.getenv("GITHUB_EVENT_NAME", "unknown"),
        "run_url": os.getenv("GITHUB_RUN_URL", ""),
        "run_id": os.getenv("GITHUB_RUN_ID", ""),
        "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT", ""),
        "commit_sha": os.getenv("GITHUB_SHA", ""),
        "step_outcome": os.getenv("PIPELINE_STEP_OUTCOME", "failure"),
        "audit_artifact": os.getenv("AUDIT_ARTIFACT_NAME", ""),
        **_local_failure_context(),
        **_workflow_failure_context(),
        **(context or {}),
    }
    await asyncio.to_thread(_send_failure_sync, payload)
    logger.info("任务失败提醒已发送至 %s 个收件人", len(config.SMTP_TO))
    return True


def _send_sync(
    report_path: Path,
    stats: dict,
    *,
    delivery_key: str = "",
) -> None:
    content = report_path.read_text(encoding="utf-8")
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > config.EMAIL_MAX_ATTACHMENT_BYTES:
        raise ValueError(
            "报告附件超过邮件安全上限: "
            f"{len(content_bytes)} > {config.EMAIL_MAX_ATTACHMENT_BYTES} bytes"
        )
    sender = config.SMTP_FROM or config.SMTP_USERNAME
    is_paradigm = bool(
        report_path.stem.startswith("paradigm_radar_")
        or delivery_key or stats.get("pipeline_mode") == "paradigm"
    )
    if is_paradigm:
        require_completed_research(stats, content=content)
    report_date = report_path.stem.removeprefix(
        "paradigm_radar_" if is_paradigm else "deal_flow_"
    )

    message = EmailMessage()
    if is_paradigm:
        message["Subject"] = (
            f"AI 技术范式雷达｜{report_date}｜"
            f"{stats.get('new_paradigms', 0)} 个新范式 + "
            f"{stats.get('updated_paradigms', 0)} 个进展"
        )
    else:
        message["Subject"] = (
            f"AI Sourcing 周报｜{report_date}｜"
            f"{stats.get('high_value_count', 0)} 个高价值项目"
        )
    message["From"] = sender
    message["To"] = ", ".join(config.SMTP_TO)
    if delivery_key:
        safe_key = re.sub(r"[^a-zA-Z0-9._-]", "", delivery_key)[:64]
        message["Message-ID"] = (
            f"<ai-paradigm-radar.{safe_key}@delivery.invalid>"
        )
        message["X-AI-Radar-Delivery-Key"] = delivery_key
    if is_paradigm:
        status_line = "AI 技术范式雷达本期研究与交付已完成。"
        body = (
            status_line
            + "\n\n"
            f"回看窗口：最近 {config.SOURCING_LOOKBACK_DAYS} 天\n"
            f"扫描论文/技术博客：{stats.get('origin_count', 0)}\n"
            f"首次捕捉范式：{stats.get('new_paradigms', 0)}\n"
            f"实质进展更新：{stats.get('updated_paradigms', 0)}\n\n"
            f"本轮完成机制抽取：{stats.get('analysis_completed_count', stats.get('analysis_count', 0))}/"
            f"{stats.get('planned_analysis_count', 0)}\n"
            "本期计划研究事务已全部闭合。\n\n"
            f"LLM 调用：{stats.get('llm_call_count', 0)} 次\n"
            f"LLM 合计 tokens：{stats.get('llm_total_tokens', 0)}\n\n"
            "完整证据、人物轨迹、公开专业联系方式和运行审计见附件。"
        )
    else:
        body = (
            "AI Sourcing 本期报告已生成。\n\n"
            f"回看窗口：最近 {config.SOURCING_LOOKBACK_DAYS} 天\n"
            f"原始项目：{stats.get('raw_count', 0)}\n"
            f"高价值项目：{stats.get('high_value_count', 0)}\n"
            f"新增入库：{stats.get('saved_count', 0)}\n\n"
            "完整 Markdown 报告见附件。"
        )
    message.set_content(body)
    message.add_attachment(
        content_bytes,
        maintype="text",
        subtype="markdown",
        filename=report_path.name,
    )
    for attachment in _audit_attachments(stats):
        subtype = "markdown" if attachment.suffix.casefold() == ".md" else "plain"
        message.add_attachment(
            attachment.read_bytes(),
            maintype="text",
            subtype=subtype,
            filename=attachment.name,
        )

    if config.SMTP_USE_SSL:
        with smtplib.SMTP_SSL(
            config.SMTP_HOST, config.SMTP_PORT, timeout=30
        ) as server:
            server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            server.send_message(message)
        return

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
        if config.SMTP_USE_STARTTLS:
            server.starttls()
        server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        server.send_message(message)


def _send_failure_sync(context: dict) -> None:
    sender = config.SMTP_FROM or config.SMTP_USERNAME
    date = scheduled_date()
    message = EmailMessage()
    message["Subject"] = f"[运行失败] AI 技术范式雷达｜{date}"
    message["From"] = sender
    message["To"] = ", ".join(config.SMTP_TO)
    run_url = str(context.get("run_url", "")).strip()
    audit_artifact = str(context.get("audit_artifact", "")).strip()
    failure_class = str(context.get("failure_class", "pipeline"))
    if failure_class == "preflight":
        opening = (
            "任务在正式研究前中止；本轮没有调用研究 API，也没有发送正式报告。"
        )
    elif failure_class == "post_delivery":
        opening = (
            "研究与邮件主流程已经成功，但后置状态或 artifact 保存失败；"
            "正式报告可能已经收到，请勿直接 reset_state。"
        )
    else:
        opening = "本期 AI 技术范式雷达未完成，因此没有确认发送正式报告。"
    lines = [
        opening,
        "",
        f"触发方式：{context.get('event', 'unknown')}",
        f"失败步骤：{context.get('failure_stage', 'unknown')}",
        f"研究与邮件主流程：{context.get('step_outcome', 'not-run')}",
        f"运行 ID：{context.get('run_id', '')}",
        f"重试序号：{context.get('run_attempt', '')}",
    ]
    if context.get("commit_sha"):
        lines.append(f"运行代码：{str(context['commit_sha'])[:12]}")
    if context.get("workflow_step_summary"):
        lines.append(f"步骤状态：{context['workflow_step_summary']}")
    if run_url:
        lines.extend([f"运行详情：{run_url}"])
    if audit_artifact:
        lines.extend([f"审计 artifact：{audit_artifact}"])
    if context.get("failure_detail"):
        lines.extend([f"可见失败原因：{context['failure_detail']}"])
    if context.get("outbox_status"):
        lines.extend(
            [
                f"交付 outbox：{context['outbox_status']}",
                f"待交付路线：{context.get('candidate_count', 0)} 条",
                f"已保存路线草稿：{context.get('fragment_count', 0)} 条",
            ]
        )
    if context.get("outbox_status"):
        action = (
            "研究检查点和待交付报告仍保留在 outbox。保持 reset_state=false "
            "重新运行，系统会复用候选快照和已保存路线草稿。"
        )
    elif failure_class == "preflight":
        action = (
            "本轮尚未进入研究阶段，修复离线回归或配置后重新运行即可；"
            "既有云端状态 artifact 没有被本次失败覆盖。"
        )
    elif failure_class == "post_delivery":
        action = (
            "先确认邮箱是否已收到报告，再检查状态 artifact；使用 "
            "reset_state=false 重试，避免丢失跨周去重状态。"
        )
    else:
        action = (
            "研究检查点会独立保存；保持 reset_state=false 重试。"
            "请结合失败步骤、运行日志与审计 artifact 判断是否从 outbox 续跑。"
        )
    lines.extend(["", action])
    message.set_content("\n".join(lines))
    _deliver_message(message)


def _local_failure_context() -> dict[str, object]:
    """Expose the last public failure boundary without leaking prompts/secrets."""

    context: dict[str, object] = {}
    try:
        if config.PARADIGM_DB_PATH.is_file():
            from database.paradigm_store import ParadigmStore

            store = ParadigmStore(config.PARADIGM_DB_PATH)
            job = store.load_pending_report_job()
            if job is not None:
                context.update(
                    {
                        "outbox_status": job.status,
                        "candidate_count": len(job.candidates),
                        "render_attempt_count": job.render_attempt_count,
                        "delivery_attempt_count": job.attempt_count,
                        "failure_kind": job.failure_kind,
                        "fragment_count": len(
                            store.load_report_fragments(job.delivery_key)
                        ),
                    }
                )
                if job.last_error:
                    context["failure_detail"] = job.last_error[:500]
    except Exception as exc:
        logger.warning("失败提醒读取 outbox 状态失败: %s", exc)

    try:
        audit_path = Path("logs/run_audit_latest.json")
        if audit_path.is_file():
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            failed_calls = [
                value
                for value in (audit.get("llm_calls") or [])
                if value.get("status") == "failed"
            ]
            if failed_calls:
                last = failed_calls[-1]
                context.setdefault("failure_stage", str(last.get("stage", "")))
                if last.get("error"):
                    # LLM 审计记录的是最内层请求异常，通常比 outbox 的业务层
                    # 包装错误更能直接说明是超时、限流还是响应结构问题。
                    context["failure_detail"] = str(last.get("error", ""))[:500]
            if not context.get("failure_stage"):
                failed_events = [
                    value
                    for value in (audit.get("events") or [])
                    if value.get("status") in {"failed", "render_failed", "send_failed"}
                ]
                if failed_events:
                    last = failed_events[-1]
                    context["failure_stage"] = str(last.get("stage", ""))
                    context.setdefault(
                        "failure_detail", str(last.get("detail", ""))[:500]
                    )
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("失败提醒读取审计摘要失败: %s", exc)
    return context


def _workflow_failure_context() -> dict[str, object]:
    """Identify the first failed Actions boundary instead of blaming pipeline."""

    outcomes: list[tuple[str, str, str]] = []
    for env_name, key, label in _WORKFLOW_STEPS:
        outcome = os.getenv(env_name, "").strip().lower() or "not-run"
        outcomes.append((key, label, outcome))
    failed = next(
        (item for item in outcomes if item[2] in {"failure", "cancelled"}),
        None,
    )
    if failed is None:
        return {}
    key, label, _ = failed
    pipeline_outcome = next(
        outcome for step_key, _, outcome in outcomes if step_key == "pipeline"
    )
    preflight_keys = {
        "provenance",
        "dependencies",
        "offline_checks",
        "restore_state",
        "doctor",
        "smoke",
    }
    failure_class = (
        "preflight"
        if key in preflight_keys
        else "post_delivery"
        if pipeline_outcome == "success" and key != "pipeline"
        else "pipeline"
    )
    visible = [
        f"{step_label}={outcome}"
        for _, step_label, outcome in outcomes
        if outcome not in {"not-run", "skipped"}
    ]
    context: dict[str, object] = {
        "failure_stage": label,
        "failure_class": failure_class,
        "workflow_step_summary": "；".join(visible),
    }
    detail = _workflow_log_detail(key)
    if detail:
        context["failure_detail"] = detail
    return context


def _workflow_log_detail(stage: str) -> str:
    if stage == "offline_checks":
        try:
            payload = json.loads(
                Path("logs/offline_checks.json").read_text(encoding="utf-8")
            )
            return _safe_public_text(str(payload.get("failure_summary", "")))
        except (OSError, ValueError, TypeError):
            return "离线回归未通过；详细失败用例见 offline_checks.log"
    if stage == "smoke":
        try:
            payload = json.loads(
                Path("logs/smoke_test_latest.json").read_text(encoding="utf-8")
            )
            failed = [
                f"{item.get('name')}={item.get('detail')}"
                for item in (payload.get("results") or [])
                if item.get("status") == "failed"
            ]
            return _safe_public_text("；".join(failed[:6]))
        except (OSError, ValueError, TypeError):
            return "真实接口冒烟未通过；详细结果见 smoke_test_latest.json"
    log_names = {
        "dependencies": "dependencies.log",
        "restore_state": "state_restore.log",
        "doctor": "doctor.log",
        "prepare_state": "state_prepare.log",
        "continuation": "continuation.log",
    }
    filename = log_names.get(stage, "")
    if not filename:
        return ""
    try:
        lines = [
            line.strip()
            for line in Path("logs", filename).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            if line.strip()
        ]
    except OSError:
        return ""
    if stage == "doctor":
        blocking = [line for line in lines if line.startswith("✗")]
        if blocking:
            return _safe_public_text("；".join(blocking[:6]))
    return _safe_public_text(" | ".join(lines[-6:]))


def _safe_public_text(value: str) -> str:
    result = value.replace("\n", " ").strip()
    result = re.sub(
        r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@",
        r"\1***@",
        result,
    )
    result = re.sub(
        r"(?i)([?&](?:api[_-]?key|token|password|secret)=)[^&\s]+",
        r"\1***",
        result,
    )
    for name in (
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
    ):
        secret = str(getattr(config, name, "") or "")
        if len(secret) >= 6:
            result = result.replace(secret, "***")
    return result[:800]


def _missing_smtp_config() -> list[str]:
    missing = []
    if not config.SMTP_HOST:
        missing.append("SMTP_HOST")
    if not config.SMTP_USERNAME:
        missing.append("SMTP_USERNAME")
    if not config.SMTP_PASSWORD:
        missing.append("SMTP_PASSWORD")
    if not config.SMTP_TO:
        missing.append("SMTP_TO")
    return missing


def _deliver_message(message: EmailMessage) -> None:
    if config.SMTP_USE_SSL:
        with smtplib.SMTP_SSL(
            config.SMTP_HOST, config.SMTP_PORT, timeout=30
        ) as server:
            server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            server.send_message(message)
        return

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
        if config.SMTP_USE_STARTTLS:
            server.starttls()
        server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        server.send_message(message)


def _audit_attachments(stats: dict) -> list[Path]:
    """只附加本轮显式登记且大小可控的审计文件。"""
    results = []
    for value in stats.get("audit_attachments", []):
        path = Path(str(value))
        try:
            if path.is_file() and path.stat().st_size <= 2_000_000:
                results.append(path)
        except OSError:
            continue
    return results
