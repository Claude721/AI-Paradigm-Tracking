"""零网络、零密钥泄露的配置体检。"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config
from agents.llm_utils import resolve_all


@dataclass
class Check:
    name: str
    role: str
    status: str
    note: str


def collect_checks() -> list[Check]:
    sub, main = resolve_all()
    checks = [
        _model_check("论文范式抽取模型", sub),
        _model_check("范式综合/人物模型", main),
        Check("arXiv", "论文发现", "ready", "官方 API，无需 Key；本检查未发请求"),
        Check(
            "Hugging Face Daily Papers",
            "论文社区信号",
            "warning",
            "无需 Key，但属于未版本化站内接口",
        ),
        Check(
            "OpenAlex",
            "论文/引用/机构",
            "ready" if config.OPENALEX_API_KEY else "missing",
            "已配置 Key" if config.OPENALEX_API_KEY else "缺少 OPENALEX_API_KEY，运行时跳过",
        ),
        Check(
            "Semantic Scholar",
            "引用/作者/代表作",
            "ready" if config.SEMANTIC_SCHOLAR_API_KEY else "degraded",
            "已配置 Key" if config.SEMANTIC_SCHOLAR_API_KEY else "可无 Key 低频使用，但限流与稳定性较弱",
        ),
        Check(
            "OpenReview",
            "投稿/评审回复",
            "ready" if config.OPENREVIEW_VENUES else "missing",
            f"已配置 {len(config.OPENREVIEW_VENUES)} 个 venue；需要按年份人工核对",
        ),
        Check(
            "官方研究 RSS/Atom",
            "研究博客发现",
            "ready" if config.RESEARCH_FEED_URLS else "missing",
            f"已配置 {len(config.RESEARCH_FEED_URLS)} 个 Feed" if config.RESEARCH_FEED_URLS else "尚未配置 RESEARCH_FEED_URLS",
        ),
        Check(
            "高优先级官方研究页面",
            "Technical Report/官方发布",
            "ready" if config.PRIORITY_RESEARCH_PAGES else "missing",
            f"已配置 {len(config.PRIORITY_RESEARCH_PAGES)} 个官方入口",
        ),
        Check(
            "前沿研究组织名单",
            "发布者势能核验",
            "ready" if config.ESTABLISHED_RESEARCH_ORGANIZATIONS else "missing",
            f"已配置 {len(config.ESTABLISHED_RESEARCH_ORGANIZATIONS)} 个已建立组织别名；"
            f"模式为 {config.RESEARCH_WATCHLIST_MODE}",
        ),
        Check(
            "监测研究组织名单",
            "广覆盖但不自动背书",
            "ready" if config.MONITORED_RESEARCH_ORGANIZATIONS else "missing",
            f"已配置 {len(config.MONITORED_RESEARCH_ORGANIZATIONS)} 个监测组织别名",
        ),
        Check(
            "重点研究者名单",
            "人物身份与长期轨迹核验",
            "ready" if config.PRIORITY_RESEARCHERS else "missing",
            f"已配置 {len(config.PRIORITY_RESEARCHERS)} 个姓名别名；"
            "必须再有公开 ID 或主页才生效",
        ),
        Check(
            "GitHub",
            "实现/复现证据",
            "ready" if config.GITHUB_TOKEN else "degraded",
            "Token 已配置" if config.GITHUB_TOKEN else "无 Token 时限额较低",
        ),
        Check("Hacker News Algolia", "社区讨论", "ready", "无需 Key；本检查未发请求"),
        Check(
            "Tavily 跨站公开索引",
            "X/Reddit/小红书讨论发现",
            "ready" if config.TAVILY_API_KEY else "degraded",
            "已配置；只作为部分索引线索，不代表平台总声量"
            if config.TAVILY_API_KEY
            else "未配置 TAVILY_API_KEY，运行时跳过跨站公开索引",
        ),
        Check(
            "Reddit 官方 Data API",
            "帖子、评论与互动量",
            "ready"
            if (
                config.REDDIT_API_ACCESS_APPROVED
                and config.REDDIT_CLIENT_ID
                and config.REDDIT_CLIENT_SECRET
                and config.REDDIT_USER_AGENT
            )
            else "degraded",
            "已确认批准并完成 OAuth 配置"
            if (
                config.REDDIT_API_ACCESS_APPROVED
                and config.REDDIT_CLIENT_ID
                and config.REDDIT_CLIENT_SECRET
                and config.REDDIT_USER_AGENT
            )
            else "未确认 Reddit 批准或 OAuth 配置不完整；不会调用官方 API",
        ),
        Check(
            "X 精确标题搜索",
            "作者身份/KOL 二次解读",
            "ready" if config.TWITTER_BEARER_TOKEN else "degraded",
            "Bearer Token 已配置"
            if config.TWITTER_BEARER_TOKEN
            else "未配置时自动跳过，不影响主流程",
        ),
        _email_check(),
        _schedule_check(),
    ]
    return checks


def print_checks() -> None:
    labels = {"ready": "✓", "degraded": "△", "warning": "△", "missing": "✗"}
    print("\nAI 技术范式雷达 — 静态体检（不会请求任何外部 API）\n")
    for item in collect_checks():
        print(f"{labels[item.status]} {item.name} [{item.role}]：{item.note}")
    print()


def _model_check(name, resolved) -> Check:
    missing = resolved.provider != "ollama" and resolved.api_key == "placeholder"
    model_ok = resolved.model.startswith("qwen3.7")
    status = "ready" if not missing and model_ok else "missing" if missing else "warning"
    note = f"{resolved.provider}/{resolved.model}；" + (
        "Key 未配置" if missing else "配置完整" if model_ok else "不属于 qwen3.7 系列"
    )
    return Check(name, "LLM", status, note)


def _email_check() -> Check:
    complete = bool(
        config.SMTP_HOST
        and config.SMTP_USERNAME
        and config.SMTP_PASSWORD
        and config.SMTP_TO
    )
    if not config.EMAIL_PUSH_ENABLED:
        return Check("SMTP 邮件", "交付", "missing", "未启用；报告仅保存本地")
    return Check(
        "SMTP 邮件",
        "交付",
        "ready" if complete else "missing",
        "配置完整" if complete else "已启用但配置不完整",
    )


def _schedule_check() -> Check:
    try:
        ZoneInfo(config.SCHEDULE_TIMEZONE)
        return Check(
            "周任务",
            "调度",
            "ready",
            f"每周 {config.SCHEDULE_DAY_OF_WEEK} {config.SCHEDULE_HOUR:02d}:{config.SCHEDULE_MINUTE:02d} ({config.SCHEDULE_TIMEZONE})",
        )
    except ZoneInfoNotFoundError:
        return Check("周任务", "调度", "missing", "时区名称无效")
