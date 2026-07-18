"""通过标准 SMTP 发送技术范式雷达报告。"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

import config

logger = logging.getLogger(__name__)


async def send_report_email(report_path: Path, stats: dict) -> bool:
    """发送报告附件；必需投递模式下失败会让任务明确失败。"""
    if not config.EMAIL_PUSH_ENABLED:
        logger.info("邮件推送未启用，报告仅保存在本地")
        return False

    missing = []
    if not config.SMTP_HOST:
        missing.append("SMTP_HOST")
    if not config.SMTP_USERNAME:
        missing.append("SMTP_USERNAME")
    if not config.SMTP_PASSWORD:
        missing.append("SMTP_PASSWORD")
    if not config.SMTP_TO:
        missing.append("SMTP_TO")
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


def _send_sync(report_path: Path, stats: dict) -> None:
    content = report_path.read_text(encoding="utf-8")
    sender = config.SMTP_FROM or config.SMTP_USERNAME
    is_paradigm = report_path.stem.startswith("paradigm_radar_")
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
    if is_paradigm:
        body = (
            "AI 技术范式雷达本期报告已生成。\n\n"
            f"回看窗口：最近 {config.SOURCING_LOOKBACK_DAYS} 天\n"
            f"扫描论文/技术博客：{stats.get('origin_count', 0)}\n"
            f"首次捕捉范式：{stats.get('new_paradigms', 0)}\n"
            f"实质进展更新：{stats.get('updated_paradigms', 0)}\n\n"
            "完整证据、人物轨迹和公开专业联系方式见附件。"
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
