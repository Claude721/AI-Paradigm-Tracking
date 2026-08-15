"""零网络、零密钥泄露的配置体检。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config
from agents.llm_utils import resolve_all
from paradigms.landscape import load_landscape
from paradigms.rubric import load_rubric


@dataclass
class Check:
    name: str
    role: str
    status: str
    note: str


def collect_checks() -> list[Check]:
    sub, main = resolve_all()
    checks = [
        _raw_environment_syntax_check(),
        _source_endpoint_syntax_check(),
        _model_check("论文范式抽取模型", sub),
        _model_check("范式综合/人物模型", main),
        _rubric_check(),
        _landscape_check(),
        Check("arXiv", "论文发现", "ready", "官方 API，无需 Key；本检查未发请求"),
        Check(
            "arXiv HTML / 官方 PDF / 项目页",
            "深挖正文与人物入口",
            "ready",
            "高优先级原点在初筛前读取，HTML 不可用时回退官方 PDF；"
            "其他候选在深挖时读取；无需 Key",
        ),
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
            "ready"
            if config.SEMANTIC_SCHOLAR_ENABLED
            and config.SEMANTIC_SCHOLAR_API_KEY
            else "degraded",
            "已显式启用并配置 Key"
            if config.SEMANTIC_SCHOLAR_ENABLED
            and config.SEMANTIC_SCHOLAR_API_KEY
            else "未启用或未配置获批 Key；运行时完全跳过，不会匿名请求",
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
            "ready" if config.RESEARCH_FEED_URLS else "degraded",
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
            "重点研究者无术语召回",
            "新术语发现冗余",
            (
                "ready"
                if config.PARADIGM_PRIORITY_AUTHOR_SWEEP_ENABLED
                else "degraded"
            ),
            (
                "已启用独立 arXiv 作者车道；姓名只提高召回，不自动背书"
                if config.PARADIGM_PRIORITY_AUTHOR_SWEEP_ENABLED
                else "已关闭；新术语只能依赖领域词、官方入口与策展源"
            ),
        ),
        Check(
            "GitHub",
            "实现/复现证据",
            "ready" if config.GITHUB_TOKEN else "degraded",
            "Token 已配置；仍需用 --smoke-test 验证是否有效"
            if config.GITHUB_TOKEN
            else "未配置时跳过 Search API，不会匿名调用",
        ),
        Check(
            "Follow Builders",
            "KOL/播客/博客辅助信号",
            "ready" if config.FOLLOW_BUILDERS_ENABLED else "degraded",
            "公共 JSON Feed 已启用"
            if config.FOLLOW_BUILDERS_ENABLED
            else "已关闭；不会读取 KOL、播客与博客 Feed",
        ),
        Check("Hacker News Algolia", "社区讨论", "ready", "无需 Key；本检查未发请求"),
        Check(
            "Tavily 跨站公开索引",
            "社区页面/独立技术博客发现",
            "ready" if config.TAVILY_API_KEY else "degraded",
            (
                f"已配置；credit safety limit="
                f"{config.TAVILY_REQUEST_SAFETY_LIMIT or '不限制'}；"
                f"域名限制={config.TAVILY_DISCOVERY_DOMAINS or '无'}；"
                "只作为部分索引线索，不代表平台总声量"
                if config.TAVILY_API_KEY
                else "未配置 TAVILY_API_KEY，运行时跳过跨站公开索引"
            ),
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
        Check(
            "真实接口 Smoke 契约",
            "云端部署验收",
            "ready",
            f"逐项总时限 {config.SMOKE_CHECK_TIMEOUT_SECONDS}s；"
            "单端点使用最小请求，多入口最多 failover 5 次；"
            "临时可用性与契约失败分账",
        ),
        _execution_budget_check(),
        _email_check(),
        _schedule_check(),
    ]
    return checks


_BOOLEAN_ENVIRONMENT_KEYS = (
    "SEMANTIC_SCHOLAR_ENABLED",
    "TAVILY_SOCIAL_SEARCH_ENABLED",
    "REDDIT_API_ACCESS_APPROVED",
    "FOLLOW_BUILDERS_ENABLED",
    "PARADIGM_ALLOW_UPDATES",
    "PARADIGM_PRIORITY_AUTHOR_SWEEP_ENABLED",
    "EMAIL_PUSH_ENABLED",
    "EMAIL_PUSH_REQUIRED",
    "SMTP_USE_SSL",
    "SMTP_USE_STARTTLS",
)

_INTEGER_ENVIRONMENT_RANGES: dict[str, tuple[int, int | None]] = {
    "LLM_REQUEST_TIMEOUT_SECONDS": (30, None),
    "SMOKE_CHECK_TIMEOUT_SECONDS": (5, 120),
    "TAVILY_REQUEST_SAFETY_LIMIT": (0, None),
    "PRIORITY_RESEARCH_LINK_SAFETY_LIMIT": (0, None),
    "PRIORITY_RESEARCH_CONCURRENCY": (1, 12),
    "PARADIGM_DISCOVERY_SAFETY_LIMIT": (0, None),
    "PARADIGM_ANALYSIS_SAFETY_LIMIT": (0, None),
    "PARADIGM_DEEP_SAFETY_LIMIT": (0, None),
    "PARADIGM_REPORT_SAFETY_LIMIT": (0, None),
    "PARADIGM_REFRESH_SAFETY_LIMIT": (0, None),
    "PARADIGM_MIN_SUBSTANTIVE_DISCUSSIONS": (1, None),
    "PARADIGM_MIN_SECONDARY_ENGAGEMENT": (1, None),
    "SOURCING_LOOKBACK_DAYS": (1, None),
    "PARADIGM_RECALL_OVERLAP_DAYS": (1, None),
    "PARADIGM_BOOTSTRAP_LOOKBACK_DAYS": (1, None),
    "PARADIGM_RESEARCHER_PROFILE_LIMIT": (3, 10),
    "PARADIGM_KEY_RESEARCHER_LIMIT": (1, 6),
    "PARADIGM_RUN_BUDGET_SECONDS": (0, None),
    "PARADIGM_STAGE_RESERVE_SECONDS": (60, None),
    "PARADIGM_DISCOVERY_SOURCE_TIMEOUT_SECONDS": (60, None),
    "PARADIGM_REPORT_TIMEOUT_SECONDS": (60, None),
    "PARADIGM_REPORT_REQUEST_TIMEOUT_SECONDS": (30, None),
    "PARADIGM_REPORT_ROUTE_CONCURRENCY": (1, 4),
    "PARADIGM_ANALYSIS_BATCH_SIZE": (1, 24),
    "PARADIGM_DEEP_BATCH_SIZE": (1, 6),
    "SCHEDULE_HOUR": (0, 23),
    "SCHEDULE_MINUTE": (0, 59),
    "EMAIL_MAX_ATTACHMENT_BYTES": (1_000_000, None),
    "SMTP_PORT": (1, 65535),
}


def _raw_environment_syntax_check() -> Check:
    """Reject malformed Variables instead of silently using parser defaults."""

    invalid: list[str] = []
    allowed_booleans = {
        "0",
        "1",
        "false",
        "true",
        "no",
        "yes",
        "n",
        "y",
        "off",
        "on",
    }
    for name in _BOOLEAN_ENVIRONMENT_KEYS:
        raw = os.getenv(name)
        if raw is not None and raw.strip().casefold() not in allowed_booleans:
            invalid.append(name)
    for name, allowed in {
        "PIPELINE_MODE": {"legacy", "paradigm"},
        "RESEARCH_WATCHLIST_MODE": {"merge", "replace"},
    }.items():
        raw = os.getenv(name)
        if raw is not None and raw.strip().casefold() not in allowed:
            invalid.append(name)
    for name, (minimum, maximum) in _INTEGER_ENVIRONMENT_RANGES.items():
        raw = os.getenv(name)
        if raw is None:
            continue
        try:
            value = int(raw.strip())
        except ValueError:
            invalid.append(name)
            continue
        if value < minimum or (maximum is not None and value > maximum):
            invalid.append(name)
    return Check(
        "环境变量语法",
        "配置解析",
        "missing" if invalid else "ready",
        (
            "以下 Variable 值无效，程序不会再静默回退默认值："
            + "、".join(sorted(set(invalid)))
            if invalid
            else "关键布尔、整数与范围配置语法有效"
        ),
    )


def _source_endpoint_syntax_check() -> Check:
    """Reject malformed endpoint Variables before a real HTTP client sees them."""

    invalid: list[str] = []

    def valid_http(value: str) -> bool:
        parsed = urlparse(value.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)

    if any(not valid_http(value) for value in config.PRIORITY_RESEARCH_PAGES):
        invalid.append("PRIORITY_RESEARCH_PAGES")
    if any(not valid_http(value) for value in config.RESEARCH_FEED_URLS):
        invalid.append("RESEARCH_FEED_URLS")
    follow_base = config.FOLLOW_BUILDERS_FEED_URL.strip()
    if config.FOLLOW_BUILDERS_ENABLED:
        parsed_follow = urlparse(follow_base)
        follow_valid = valid_http(follow_base) or (
            parsed_follow.scheme == "file" and bool(parsed_follow.path)
        )
        if not follow_valid:
            invalid.append("FOLLOW_BUILDERS_FEED_URL")
    if any(
        "/" not in value
        or value.startswith(("http://", "https://"))
        or any(character.isspace() for character in value)
        for value in config.OPENREVIEW_VENUES
    ):
        invalid.append("OPENREVIEW_VENUES")
    return Check(
        "信源入口语法",
        "配置解析",
        "missing" if invalid else "ready",
        (
            "以下入口配置格式无效：" + "、".join(sorted(set(invalid)))
            if invalid
            else "官方页面、Feed、Follow Builders 与 OpenReview venue 格式有效"
        ),
    )


def _rubric_check() -> Check:
    try:
        rubric = load_rubric()
        deep = rubric["decisions"]["deep_dive"]["min_score"]
        report = rubric["decisions"]["report"]["min_score"]
        return Check(
            "技术范式 Rubric",
            "可审计研究决策",
            "ready",
            f"版本 {rubric['version']}；{len(rubric['common_criteria'])} 道 common 题；"
            f"{len(rubric['type_criteria'])} 类创新量表；深挖/报告阈值 {deep}/{report}；"
            f"实质讨论/互动边界 {config.PARADIGM_MIN_SUBSTANTIVE_DISCUSSIONS}/"
            f"{config.PARADIGM_MIN_SECONDARY_ENGAGEMENT}",
        )
    except Exception as exc:
        return Check(
            "技术范式 Rubric",
            "可审计研究决策",
            "missing",
            f"Rubric 无法加载：{exc}",
        )


def _landscape_check() -> Check:
    try:
        landscape = load_landscape()
        domains = landscape["domains"]
        return Check(
            "AI 前沿覆盖地图",
            "产业/技术栈召回审计",
            "ready",
            f"版本 {landscape['version']}；{len(domains)} 个必查领域；"
            f"周更重叠 {config.PARADIGM_RECALL_OVERLAP_DAYS} 天；"
            f"空状态/地图升级回看 {config.PARADIGM_BOOTSTRAP_LOOKBACK_DAYS} 天",
        )
    except Exception as exc:
        return Check(
            "AI 前沿覆盖地图",
            "产业/技术栈召回审计",
            "missing",
            f"覆盖地图无法加载：{exc}",
        )


def blocking_checks(checks: list[Check] | None = None) -> list[Check]:
    """Return configuration defects that make a production run unsafe."""

    values = collect_checks() if checks is None else checks
    return [item for item in values if item.status == "missing"]


def print_checks() -> list[Check]:
    labels = {"ready": "✓", "degraded": "△", "warning": "△", "missing": "✗"}
    checks = collect_checks()
    print("\nAI 技术范式雷达 — 静态体检（不会请求任何外部 API）\n")
    for item in checks:
        print(f"{labels[item.status]} {item.name} [{item.role}]：{item.note}")
    print()
    return checks


def _model_check(name, resolved) -> Check:
    missing = resolved.provider != "ollama" and resolved.api_key == "placeholder"
    model_ok = resolved.model.startswith("qwen3.7")
    status = "ready" if not missing and model_ok else "missing"
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
        return Check("SMTP 邮件", "交付", "degraded", "未启用；报告仅保存本地")
    if config.SMTP_USE_SSL and config.SMTP_USE_STARTTLS:
        return Check(
            "SMTP 邮件",
            "交付",
            "missing",
            "SMTP_USE_SSL 与 SMTP_USE_STARTTLS 不能同时开启",
        )
    if not 1 <= config.SMTP_PORT <= 65535:
        return Check("SMTP 邮件", "交付", "missing", "SMTP_PORT 超出有效范围")
    return Check(
        "SMTP 邮件",
        "交付",
        "ready" if complete else "missing",
        "配置完整" if complete else "已启用但配置不完整",
    )


def _execution_budget_check() -> Check:
    budget = config.PARADIGM_RUN_BUDGET_SECONDS
    if budget == 0:
        in_github_actions = os.getenv("GITHUB_ACTIONS", "").casefold() == "true"
        return Check(
            "可续跑时间预算",
            "云端可靠性",
            "missing" if in_github_actions else "warning",
            (
                "GitHub Actions 不允许禁用软预算；否则任务可能在保存状态、"
                "发送失败提醒前被 90 分钟 job 硬取消"
                if in_github_actions
                else "软预算已禁用；本地可用，但 90 分钟 GitHub job 可能被硬取消"
            ),
        )
    report_budget = config.PARADIGM_REPORT_TIMEOUT_SECONDS
    combined = budget + report_budget
    request_budget = config.PARADIGM_REPORT_REQUEST_TIMEOUT_SECONDS
    discovery_budget = config.PARADIGM_DISCOVERY_SOURCE_TIMEOUT_SECONDS
    stage_reserve = config.PARADIGM_STAGE_RESERVE_SECONDS
    safe_budget = (
        combined <= 4800
        and request_budget < report_budget
        and discovery_budget < budget
        and stage_reserve < budget
    )
    status = "ready" if safe_budget else "missing"
    return Check(
        "可续跑时间预算",
        "云端可靠性",
        status,
        f"研究 {budget}s + 报告 {report_budget}s = {combined}s；"
        f"报告单请求 {request_budget}s，并发 "
        f"{config.PARADIGM_REPORT_ROUTE_CONCURRENCY}；"
        f"发现单源 {discovery_budget}s；阶段预留 {stage_reserve}s；"
        f"抽取/深挖批次 {config.PARADIGM_ANALYSIS_BATCH_SIZE}/"
        f"{config.PARADIGM_DEEP_BATCH_SIZE}；"
        + (
            "为 90 分钟 Actions 的安装、测试、邮件和 artifact 保留约 10 分钟"
            if status == "ready"
            else "总预算或单请求配置过大，可能来不及发送邮件和保存 artifact"
        ),
    )


def _schedule_check() -> Check:
    valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    if config.SCHEDULE_DAY_OF_WEEK.casefold() not in valid_days:
        return Check("周任务", "调度", "missing", "SCHEDULE_DAY_OF_WEEK 无效")
    if not 0 <= config.SCHEDULE_HOUR <= 23:
        return Check("周任务", "调度", "missing", "SCHEDULE_HOUR 超出 0–23")
    if not 0 <= config.SCHEDULE_MINUTE <= 59:
        return Check("周任务", "调度", "missing", "SCHEDULE_MINUTE 超出 0–59")
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
