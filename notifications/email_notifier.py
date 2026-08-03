"""通过标准 SMTP 发送技术范式雷达报告。"""

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import config

logger = logging.getLogger(__name__)


async def send_report_email(report_path: Path, stats: dict) -> bool:
    """发送报告附件；必需投递模式下失败会让任务明确失败。"""
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
        await asyncio.to_thread(_send_sync, report_path, stats)
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
        "step_outcome": os.getenv("PIPELINE_STEP_OUTCOME", "failure"),
        "audit_artifact": os.getenv("AUDIT_ARTIFACT_NAME", ""),
        **(context or {}),
    }
    await asyncio.to_thread(_send_failure_sync, payload)
    logger.info("任务失败提醒已发送至 %s 个收件人", len(config.SMTP_TO))
    return True


def _send_sync(report_path: Path, stats: dict) -> None:
    content = report_path.read_text(encoding="utf-8")
    sender = config.SMTP_FROM or config.SMTP_USERNAME
    is_paradigm = report_path.stem.startswith("paradigm_radar_")
    report_date = report_path.stem.removeprefix(
        "paradigm_radar_" if is_paradigm else "deal_flow_"
    )

    message = EmailMessage()
    if is_paradigm:
        progress = "｜覆盖进行中" if stats.get("run_incomplete") else ""
        message["Subject"] = (
            f"AI 技术范式雷达｜{report_date}｜"
            f"{stats.get('new_paradigms', 0)} 个新范式 + "
            f"{stats.get('updated_paradigms', 0)} 个进展{progress}"
        )
    else:
        message["Subject"] = (
            f"AI Sourcing 周报｜{report_date}｜"
            f"{stats.get('high_value_count', 0)} 个高价值项目"
        )
    message["From"] = sender
    message["To"] = ", ".join(config.SMTP_TO)
    if is_paradigm:
        body = (
            "AI 技术范式雷达本期报告已生成。\n\n"
            f"回看窗口：最近 {config.SOURCING_LOOKBACK_DAYS} 天\n"
            f"扫描论文/技术博客：{stats.get('origin_count', 0)}\n"
            f"首次捕捉范式：{stats.get('new_paradigms', 0)}\n"
            f"实质进展更新：{stats.get('updated_paradigms', 0)}\n\n"
            f"本轮完成机制抽取：{stats.get('analysis_completed_count', stats.get('analysis_count', 0))}/"
            f"{stats.get('planned_analysis_count', 0)}\n"
            f"留待后续运行：{stats.get('pending_work_count', 0)} 项\n\n"
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
        content.encode("utf-8"),
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
    date = datetime.now().astimezone().strftime("%Y-%m-%d")
    message = EmailMessage()
    message["Subject"] = f"[运行失败] AI 技术范式雷达｜{date}"
    message["From"] = sender
    message["To"] = ", ".join(config.SMTP_TO)
    run_url = str(context.get("run_url", "")).strip()
    audit_artifact = str(context.get("audit_artifact", "")).strip()
    lines = [
        "本期 AI 技术范式雷达未完成，因此没有发送正式报告。",
        "",
        f"触发方式：{context.get('event', 'unknown')}",
        f"主流程结果：{context.get('step_outcome', 'failure')}",
        f"运行 ID：{context.get('run_id', '')}",
        f"重试序号：{context.get('run_attempt', '')}",
    ]
    if run_url:
        lines.extend([f"运行详情：{run_url}"])
    if audit_artifact:
        lines.extend([f"审计 artifact：{audit_artifact}"])
    lines.extend(
        [
            "",
            "去重状态只会在完整报告和邮件成功后发布；"
            "本次失败不会被误标为已交付。请查看运行日志与审计 artifact 后重试。",
        ]
    )
    message.set_content("\n".join(lines))
    _deliver_message(message)


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
