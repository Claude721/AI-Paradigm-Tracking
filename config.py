"""
全局配置管理 — 从 .env 文件加载所有环境变量

快速配置: python main.py --setup
查看状态: python main.py --status

┌──────────────────────────────────────────────────────────┐
│              模型配置优先级（从高到低）                      │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  子Agent(初筛)                主Agent(深度分析)            │
│  ┌──────────────┐            ┌──────────────┐            │
│  │SUB_AGENT_*   │            │MAIN_AGENT_*  │  ← 最高优先 │
│  └──────┬───────┘            └──────┬───────┘            │
│         │ 未设置时                    │ 未设置时             │
│         ▼                           ▼                    │
│  ┌──────────────────────────────────────────┐            │
│  │  LLM_PROVIDER / LLM_MODEL / LLM_API_KEY │  ← 全局配置 │
│  └──────────────────┬───────────────────────┘            │
│                     │ 未设置时                             │
│                     ▼                                    │
│  ┌──────────────────────────────────────────┐            │
│  │  Provider 内置默认值                       │  ← 兜底    │
│  │  ollama → localhost:11434 / qwen3:14b    │            │
│  │  dashscope → aliyun URL / qwen3.7-plus   │            │
│  │  volcengine → volces URL / doubao-pro-32k│            │
│  └──────────────────────────────────────────┘            │
│                                                          │
│  示例：                                                   │
│  · 全用 Ollama:   只设 LLM_PROVIDER=ollama              │
│  · 全用云端:      设 LLM_PROVIDER + LLM_API_KEY          │
│  · 混合部署:      全局 ollama + MAIN_AGENT 设 dashscope  │
│                                                          │
└──────────────────────────────────────────────────────────┘
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from research_watchlist import (
    default_monitored_organization_aliases,
    default_organization_aliases,
    default_priority_pages,
    default_researcher_aliases,
)

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return float(val.strip())
    except ValueError:
        return default


def _env_csv(name: str) -> list[str]:
    return [value.strip() for value in os.getenv(name, "").split(",") if value.strip()]


def _merge_watchlist(defaults: list[str], custom: list[str], mode: str) -> list[str]:
    values = custom if mode == "replace" else [*defaults, *custom]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result

# ── 项目路径 ──────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "database" / "deal_sourcing.db"
PARADIGM_DB_PATH = BASE_DIR / "database" / "paradigm_radar.db"
REPORTS_DIR = BASE_DIR / "reports" / "output"
_landscape_value = os.getenv("FRONTIER_LANDSCAPE_PATH", "").strip()
_landscape_path = Path(_landscape_value).expanduser() if _landscape_value else None
FRONTIER_LANDSCAPE_PATH = (
    (
        _landscape_path
        if _landscape_path.is_absolute()
        else BASE_DIR / _landscape_path
    )
    if _landscape_path
    else BASE_DIR / "taxonomy" / "frontier_landscape.json"
)
_rubric_value = os.getenv("PARADIGM_RUBRIC_PATH", "").strip()
_rubric_path = Path(_rubric_value).expanduser() if _rubric_value else None
PARADIGM_RUBRIC_PATH = (
    (_rubric_path if _rubric_path.is_absolute() else BASE_DIR / _rubric_path)
    if _rubric_path
    else BASE_DIR / "rubrics" / "paradigm_rubric.json"
)

# ── 全局 LLM 配置（子/主 Agent 的共同 fallback） ─────────
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "")
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "")
LLM_MODEL: str = os.getenv("LLM_MODEL", "")
LLM_REQUEST_TIMEOUT_SECONDS: int = max(
    30,
    _env_int("LLM_REQUEST_TIMEOUT_SECONDS", 180),
)
# 每个真实接口冒烟的总时限。冒烟只验证最小请求和响应契约，不允许生产
# 分页逻辑或第三方服务异常无界拖住整轮部署验收。
SMOKE_CHECK_TIMEOUT_SECONDS: int = max(
    5,
    min(_env_int("SMOKE_CHECK_TIMEOUT_SECONDS", 30), 120),
)

# Ollama 便捷配置（仅当 provider=ollama 时作为 LLM_MODEL/LLM_BASE_URL 的补充）
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "")

# ── 子 Agent 配置（初筛 Triage，留空继承全局） ────────────
SUB_AGENT_PROVIDER: str = os.getenv("SUB_AGENT_PROVIDER", "")
SUB_AGENT_MODEL: str = os.getenv("SUB_AGENT_MODEL", "")
SUB_AGENT_API_KEY: str = os.getenv("SUB_AGENT_API_KEY", "")
SUB_AGENT_BASE_URL: str = os.getenv("SUB_AGENT_BASE_URL", "")

# ── 主 Agent 配置（深度分析，留空继承全局） ───────────────
MAIN_AGENT_PROVIDER: str = os.getenv("MAIN_AGENT_PROVIDER", "")
MAIN_AGENT_MODEL: str = os.getenv("MAIN_AGENT_MODEL", "")
MAIN_AGENT_API_KEY: str = os.getenv("MAIN_AGENT_API_KEY", "")
MAIN_AGENT_BASE_URL: str = os.getenv("MAIN_AGENT_BASE_URL", "")

# Triage 阶段的分数阈值（>= 此分数才进入深度分析）
TRIAGE_THRESHOLD: int = _env_int("TRIAGE_THRESHOLD", 5)

# ── 外部 API Token ───────────────────────────────────────
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
PRODUCTHUNT_TOKEN: str = os.getenv("PRODUCTHUNT_TOKEN", "")
TWITTER_BEARER_TOKEN: str = os.getenv("TWITTER_BEARER_TOKEN", "")
TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")
OPENALEX_API_KEY: str = os.getenv("OPENALEX_API_KEY", "")
SEMANTIC_SCHOLAR_API_KEY: str = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")

# Semantic Scholar 不再匿名调用。未获批 API Key 时必须保持关闭，避免共享匿名
# 额度在候选并发增强时连续触发 429。
SEMANTIC_SCHOLAR_ENABLED: bool = _env_bool(
    "SEMANTIC_SCHOLAR_ENABLED", False
)

# Tavily 免费层作为 X / Reddit / 小红书的公开网页索引兜底。它只能证明
# “搜索引擎发现了这些页面”，不能替代平台官方的完整互动量。
TAVILY_SOCIAL_SEARCH_ENABLED: bool = _env_bool(
    "TAVILY_SOCIAL_SEARCH_ENABLED", True
)
TAVILY_SOCIAL_SEARCH_DOMAINS: list[str] = [
    value.strip().casefold()
    for value in os.getenv(
        "TAVILY_SOCIAL_SEARCH_DOMAINS",
        "x.com,twitter.com,reddit.com,xiaohongshu.com",
    ).split(",")
    if value.strip()
]
TAVILY_SOCIAL_MAX_RESULTS: int = max(
    1, min(_env_int("TAVILY_SOCIAL_MAX_RESULTS", 12), 20)
)
# 同一个 Tavily 请求默认同时发现社区页面和独立技术博客。留空表示不按域名
# 限制搜索；结果仍只是索引线索，必须经直接来源核验后才能计入独立承接。
TAVILY_DISCOVERY_DOMAINS: list[str] = [
    value.strip().casefold()
    for value in os.getenv("TAVILY_DISCOVERY_DOMAINS", "").split(",")
    if value.strip()
]
# Tavily 默认覆盖全部通过 Rubric 的深挖候选。非零值是用户显式设置的
# credit safety limit，不参与候选排序或研究去留。
TAVILY_REQUEST_SAFETY_LIMIT: int = max(
    0, _env_int("TAVILY_REQUEST_SAFETY_LIMIT", 0)
)

# Reddit 官方 Data API 必须先获得 Reddit 批准并使用 OAuth。商业用途还需
# 单独获得许可，因此即使填了密钥，也只有显式确认批准后才会调用。
REDDIT_API_ACCESS_APPROVED: bool = _env_bool(
    "REDDIT_API_ACCESS_APPROVED", False
)
REDDIT_CLIENT_ID: str = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET: str = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT: str = os.getenv("REDDIT_USER_AGENT", "")

# OpenReview 的 venue id 可按年份调整；留空时跳过该信源。
OPENREVIEW_VENUES: list[str] = [
    value.strip()
    for value in os.getenv(
        "OPENREVIEW_VENUES",
        "ICLR.cc/2026/Conference,NeurIPS.cc/2026/Conference",
    ).split(",")
    if value.strip()
]

# 官方研究博客/RSS。格式为逗号分隔 URL；默认不猜测不稳定的 feed 地址。
RESEARCH_FEED_URLS: list[str] = [
    value.strip()
    for value in os.getenv("RESEARCH_FEED_URLS", "").split(",")
    if value.strip()
]

# 内置名单版本化保存在 research_watchlist.py。环境变量默认只追加少量自定义
# 项，避免 GitHub 中的旧 Variable 阻止仓库名单随版本更新；replace 可显式替换。
RESEARCH_WATCHLIST_MODE: str = os.getenv(
    "RESEARCH_WATCHLIST_MODE", "merge"
).strip().casefold()
if RESEARCH_WATCHLIST_MODE not in {"merge", "replace"}:
    RESEARCH_WATCHLIST_MODE = "merge"

# 重点页面只控制主动召回；页面能否构成发布者背书由内置 owner/tier 元数据决定。
PRIORITY_RESEARCH_PAGES: list[str] = _merge_watchlist(
    default_priority_pages(),
    _env_csv("PRIORITY_RESEARCH_PAGES"),
    RESEARCH_WATCHLIST_MODE,
)
PRIORITY_RESEARCH_LINK_SAFETY_LIMIT: int = max(
    0, _env_int("PRIORITY_RESEARCH_LINK_SAFETY_LIMIT", 0)
)
PRIORITY_RESEARCH_CONCURRENCY: int = max(
    1, min(_env_int("PRIORITY_RESEARCH_CONCURRENCY", 6), 12)
)

# “已建立”与“监测”严格分层。别名只做实体精确归一，不使用任意子串匹配；
# 不登记整所大学，也不加入 Seed、GLM、AI Lab 等歧义裸词。
ESTABLISHED_RESEARCH_ORGANIZATIONS: list[str] = _merge_watchlist(
    default_organization_aliases(),
    _env_csv("ESTABLISHED_RESEARCH_ORGANIZATIONS"),
    RESEARCH_WATCHLIST_MODE,
)
MONITORED_RESEARCH_ORGANIZATIONS: list[str] = _merge_watchlist(
    default_monitored_organization_aliases(),
    _env_csv("MONITORED_RESEARCH_ORGANIZATIONS"),
    RESEARCH_WATCHLIST_MODE,
)

# 重点研究者必须再由公开个人主页、ORCID/OpenAlex 等身份信息核验；姓名本身
# 只提高背景核验优先级，不会让工作越过技术或外部承接门槛。
PRIORITY_RESEARCHERS: list[str] = _merge_watchlist(
    default_researcher_aliases(),
    _env_csv("PRIORITY_RESEARCHERS"),
    RESEARCH_WATCHLIST_MODE,
)

# ── Twitter 追踪账号（逗号分隔） ─────────────────────────
_tw_accounts = os.getenv("TWITTER_WATCH_ACCOUNTS", "")
TWITTER_WATCH_ACCOUNTS: list[str] = [
    a.strip().lstrip("@") for a in _tw_accounts.split(",") if a.strip()
]

# ── 微信公众号信源（we-mp-rss 本地服务）──────────────────
WECHAT_SOURCE_ENABLED: bool = _env_bool("WECHAT_SOURCE_ENABLED", False)
WECHAT_RSS_BASE_URL: str = os.getenv("WECHAT_RSS_BASE_URL", "http://localhost:8001")
WECHAT_RSS_TOKEN: str = os.getenv("WECHAT_RSS_TOKEN", "")

# ── Follow Builders 二次传播信源（KOL / Podcast / Blog）─
# 方案 A（默认）：读取原作者 GitHub 仓库的 JSON Feed，零成本
# 方案 B（自建）：本地运行抓取脚本，需配置 API Key
FOLLOW_BUILDERS_ENABLED: bool = _env_bool("FOLLOW_BUILDERS_ENABLED", True)
FOLLOW_BUILDERS_FEED_URL: str = os.getenv(
    "FOLLOW_BUILDERS_FEED_URL",
    "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main",
)

# 方案 B 自建配置（默认关闭）
FOLLOW_BUILDERS_SELF_HOST: bool = _env_bool("FOLLOW_BUILDERS_SELF_HOST", False)
FOLLOW_BUILDERS_X_BEARER_TOKEN: str = os.getenv("FOLLOW_BUILDERS_X_BEARER_TOKEN", "")
FOLLOW_BUILDERS_SUPADATA_API_KEY: str = os.getenv("FOLLOW_BUILDERS_SUPADATA_API_KEY", "")

# ── 业务参数 ─────────────────────────────────────────────
SCORE_THRESHOLD: int = _env_int("SCORE_THRESHOLD", 7)
STAR_THRESHOLD: int = _env_int("STAR_THRESHOLD", 50)

# 默认启用技术范式雷达；legacy 可回退到原有项目型 Deal Flow。
PIPELINE_MODE: str = os.getenv("PIPELINE_MODE", "paradigm").strip().lower()

# 技术筛选唯一依据是版本化 Rubric。模型回答离散问题并给出证据，程序按
# rubrics/paradigm_rubric.json 确定性计分；不再读取模型自报的 0-10 主观分。
#
# 以下 safety limit 只用于用户主动设置的运行熔断，0 表示不限制。它们绝不
# 参与候选排序或研究去留；触发时必须在审计中明确标为“未完成”，不能写成
# “未通过筛选”。旧版 PARADIGM_MAX_* 环境变量不再读取，避免 GitHub 中
# 遗留的 100/30/16 继续影响新逻辑。
PARADIGM_DISCOVERY_SAFETY_LIMIT: int = max(
    0, _env_int("PARADIGM_DISCOVERY_SAFETY_LIMIT", 0)
)
PARADIGM_ANALYSIS_SAFETY_LIMIT: int = max(
    0, _env_int("PARADIGM_ANALYSIS_SAFETY_LIMIT", 0)
)
PARADIGM_DEEP_SAFETY_LIMIT: int = max(
    0, _env_int("PARADIGM_DEEP_SAFETY_LIMIT", 0)
)
PARADIGM_REPORT_SAFETY_LIMIT: int = max(
    0, _env_int("PARADIGM_REPORT_SAFETY_LIMIT", 0)
)
PARADIGM_ALLOW_UPDATES: bool = _env_bool("PARADIGM_ALLOW_UPDATES", True)
# 重点研究者 arXiv 车道与领域关键词车道独立运行。姓名只提高召回优先级，
# 后续仍需公开身份、技术 Rubric 和外部承接核验。
PARADIGM_PRIORITY_AUTHOR_SWEEP_ENABLED: bool = _env_bool(
    "PARADIGM_PRIORITY_AUTHOR_SWEEP_ENABLED", True
)
PARADIGM_REFRESH_SAFETY_LIMIT: int = max(
    0, _env_int("PARADIGM_REFRESH_SAFETY_LIMIT", 0)
)
PARADIGM_MIN_SUBSTANTIVE_DISCUSSIONS: int = _env_int(
    "PARADIGM_MIN_SUBSTANTIVE_DISCUSSIONS", 2
)
PARADIGM_MIN_SECONDARY_ENGAGEMENT: int = _env_int(
    "PARADIGM_MIN_SECONDARY_ENGAGEMENT", 50
)

# 周报/月度回顾的抓取时间窗口
SOURCING_LOOKBACK_DAYS: int = max(_env_int("SOURCING_LOOKBACK_DAYS", 7), 1)
# 定时周更保留一个月重叠发现窗口。数据库负责按证据指纹去重，因此扫描 30 天
# 不会重复分析已处理论文，却能覆盖索引晚到、官方文章稍后补挂完整报告、Actions
# 延迟或某条召回车道短期失败。手动 60/90 天窗口不受缩短。
PARADIGM_RECALL_OVERLAP_DAYS: int = max(
    SOURCING_LOOKBACK_DAYS,
    _env_int("PARADIGM_RECALL_OVERLAP_DAYS", 30),
)
# 正常周更只看本周新增；数据库为空或用户显式 reset 时使用较长冷启动窗口，
# 以免项目第一次上线时错过最近形成、但已超出 7/30 天的关键节点。
PARADIGM_BOOTSTRAP_LOOKBACK_DAYS: int = max(
    PARADIGM_RECALL_OVERLAP_DAYS,
    _env_int("PARADIGM_BOOTSTRAP_LOOKBACK_DAYS", 60),
)
# 可选的精确论文补录通道，用逗号分隔 arXiv ID。它主要用于审计、回补与
# 黄金样例复查，不是用论文白名单替代领域召回。
PARADIGM_SEED_ARXIV_IDS: list[str] = _env_csv("PARADIGM_SEED_ARXIV_IDS")
PARADIGM_RESEARCHER_PROFILE_LIMIT: int = max(
    3, min(_env_int("PARADIGM_RESEARCHER_PROFILE_LIMIT", 6), 10)
)
PARADIGM_STATE_SCHEMA_VERSION: int = 2

# 云端任务必须在 GitHub 的硬超时之前主动收尾。该预算只决定本轮执行到
# backlog 的哪个位置，不参与 Rubric、排序分数或研究去留；未处理项会持久化
# 并在后续运行继续。设为 0 可在有外部进程监管的本地环境禁用软预算。
PARADIGM_RUN_BUDGET_SECONDS: int = max(
    0, _env_int("PARADIGM_RUN_BUDGET_SECONDS", 3900)
)
# 每个后续阶段预留的时间：原点机制抽取会为深挖和最终报告各预留一份，
# 深挖/历史刷新会为最终报告预留一份。
PARADIGM_STAGE_RESERVE_SECONDS: int = max(
    60, _env_int("PARADIGM_STAGE_RESERVE_SECONDS", 600)
)
# 批次大小只是可取消、可检查点的执行粒度，不是候选数量上限。
PARADIGM_ANALYSIS_BATCH_SIZE: int = max(
    1, min(_env_int("PARADIGM_ANALYSIS_BATCH_SIZE", 6), 24)
)
PARADIGM_DEEP_BATCH_SIZE: int = max(
    1, min(_env_int("PARADIGM_DEEP_BATCH_SIZE", 1), 6)
)

# 定时任务（默认每周五 09:15，Asia/Shanghai；与 GitHub Actions 一致）
SCHEDULE_DAY_OF_WEEK: str = os.getenv("SCHEDULE_DAY_OF_WEEK", "fri")
SCHEDULE_HOUR: int = _env_int("SCHEDULE_HOUR", 9)
SCHEDULE_MINUTE: int = _env_int("SCHEDULE_MINUTE", 15)
SCHEDULE_TIMEZONE: str = os.getenv("SCHEDULE_TIMEZONE", "Asia/Shanghai")

# 报告邮件推送（可选；未启用时只生成本地 Markdown）
EMAIL_PUSH_ENABLED: bool = _env_bool("EMAIL_PUSH_ENABLED", False)
# 开启邮件后默认将投递视为必需步骤。发送失败时让进程返回失败，避免云端
# 调度看似成功、实际没有收到报告。
EMAIL_PUSH_REQUIRED: bool = _env_bool("EMAIL_PUSH_REQUIRED", True)
SMTP_HOST: str = os.getenv("SMTP_HOST", "")
SMTP_PORT: int = _env_int("SMTP_PORT", 465)
SMTP_USERNAME: str = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM: str = os.getenv("SMTP_FROM", "")
SMTP_TO: list[str] = [
    address.strip()
    for address in os.getenv("SMTP_TO", "").split(",")
    if address.strip()
]
SMTP_USE_SSL: bool = _env_bool("SMTP_USE_SSL", True)
SMTP_USE_STARTTLS: bool = _env_bool("SMTP_USE_STARTTLS", False)

# 定量预过滤（LLM 前置）配置
QUANT_FILTER_ENABLED: bool = _env_bool("QUANT_FILTER_ENABLED", True)
QUANT_MIN_SCORE: float = _env_float("QUANT_MIN_SCORE", 38.0)
QUANT_MAX_CANDIDATES: int = _env_int("QUANT_MAX_CANDIDATES", 140)
QUANT_MIN_TEXT_LEN: int = _env_int("QUANT_MIN_TEXT_LEN", 20)

# 评分权重（总和建议为 1.0）
QUANT_W_FRESHNESS: float = _env_float("QUANT_W_FRESHNESS", 0.45)
QUANT_W_MOMENTUM: float = _env_float("QUANT_W_MOMENTUM", 0.35)
QUANT_W_ENGAGEMENT: float = _env_float("QUANT_W_ENGAGEMENT", 0.20)

# 按信源定制化：最低 quant 分（留空表示继承 QUANT_MIN_SCORE）
QUANT_SRC_MIN_GITHUB: float = _env_float("QUANT_SRC_MIN_GITHUB", 42.0)
QUANT_SRC_MIN_GITHUB_TRENDING: float = _env_float("QUANT_SRC_MIN_GITHUB_TRENDING", 40.0)
QUANT_SRC_MIN_HUGGINGFACE_MODEL: float = _env_float("QUANT_SRC_MIN_HUGGINGFACE_MODEL", 36.0)
QUANT_SRC_MIN_HUGGINGFACE_SPACE: float = _env_float("QUANT_SRC_MIN_HUGGINGFACE_SPACE", 34.0)
QUANT_SRC_MIN_HUGGINGFACE_PAPERS: float = _env_float("QUANT_SRC_MIN_HUGGINGFACE_PAPERS", 40.0)
QUANT_SRC_MIN_ARXIV: float = _env_float("QUANT_SRC_MIN_ARXIV", 44.0)
QUANT_SRC_MIN_PRODUCTHUNT: float = _env_float("QUANT_SRC_MIN_PRODUCTHUNT", 24.0)
QUANT_SRC_MIN_HACKERNEWS: float = _env_float("QUANT_SRC_MIN_HACKERNEWS", 26.0)
QUANT_SRC_MIN_TWITTER: float = _env_float("QUANT_SRC_MIN_TWITTER", 30.0)
QUANT_SRC_MIN_WECHAT: float = _env_float("QUANT_SRC_MIN_WECHAT", 20.0)
QUANT_SRC_MIN_FOLLOW_BUILDERS_X: float = _env_float("QUANT_SRC_MIN_FOLLOW_BUILDERS_X", 20.0)
QUANT_SRC_MIN_FOLLOW_BUILDERS_PODCAST: float = _env_float("QUANT_SRC_MIN_FOLLOW_BUILDERS_PODCAST", 18.0)
QUANT_SRC_MIN_FOLLOW_BUILDERS_BLOG: float = _env_float("QUANT_SRC_MIN_FOLLOW_BUILDERS_BLOG", 18.0)

# 按信源配额（0=不限制）
QUANT_SRC_MAX_GITHUB: int = _env_int("QUANT_SRC_MAX_GITHUB", 60)
QUANT_SRC_MAX_GITHUB_TRENDING: int = _env_int("QUANT_SRC_MAX_GITHUB_TRENDING", 30)
QUANT_SRC_MAX_HUGGINGFACE_MODEL: int = _env_int("QUANT_SRC_MAX_HUGGINGFACE_MODEL", 25)
QUANT_SRC_MAX_HUGGINGFACE_SPACE: int = _env_int("QUANT_SRC_MAX_HUGGINGFACE_SPACE", 20)
QUANT_SRC_MAX_HUGGINGFACE_PAPERS: int = _env_int("QUANT_SRC_MAX_HUGGINGFACE_PAPERS", 20)
QUANT_SRC_MAX_ARXIV: int = _env_int("QUANT_SRC_MAX_ARXIV", 15)
QUANT_SRC_MAX_HACKERNEWS: int = _env_int("QUANT_SRC_MAX_HACKERNEWS", 20)
QUANT_SRC_MAX_PRODUCTHUNT: int = _env_int("QUANT_SRC_MAX_PRODUCTHUNT", 20)
QUANT_SRC_MAX_TWITTER: int = _env_int("QUANT_SRC_MAX_TWITTER", 15)
QUANT_SRC_MAX_WECHAT: int = _env_int("QUANT_SRC_MAX_WECHAT", 30)
QUANT_SRC_MAX_FOLLOW_BUILDERS_X: int = _env_int("QUANT_SRC_MAX_FOLLOW_BUILDERS_X", 25)
QUANT_SRC_MAX_FOLLOW_BUILDERS_PODCAST: int = _env_int("QUANT_SRC_MAX_FOLLOW_BUILDERS_PODCAST", 10)
QUANT_SRC_MAX_FOLLOW_BUILDERS_BLOG: int = _env_int("QUANT_SRC_MAX_FOLLOW_BUILDERS_BLOG", 10)

# 按信源偏置（可正可负，直接加到 quant_score）
QUANT_BIAS_GITHUB: float = _env_float("QUANT_BIAS_GITHUB", -3.0)
QUANT_BIAS_GITHUB_TRENDING: float = _env_float("QUANT_BIAS_GITHUB_TRENDING", -1.0)
QUANT_BIAS_HUGGINGFACE_MODEL: float = _env_float("QUANT_BIAS_HUGGINGFACE_MODEL", 2.0)
QUANT_BIAS_HUGGINGFACE_SPACE: float = _env_float("QUANT_BIAS_HUGGINGFACE_SPACE", 3.0)
QUANT_BIAS_HUGGINGFACE_PAPERS: float = _env_float("QUANT_BIAS_HUGGINGFACE_PAPERS", -1.0)
QUANT_BIAS_ARXIV: float = _env_float("QUANT_BIAS_ARXIV", -2.0)
QUANT_BIAS_HACKERNEWS: float = _env_float("QUANT_BIAS_HACKERNEWS", 8.0)
QUANT_BIAS_PRODUCTHUNT: float = _env_float("QUANT_BIAS_PRODUCTHUNT", 10.0)
QUANT_BIAS_TWITTER: float = _env_float("QUANT_BIAS_TWITTER", 3.0)
QUANT_BIAS_WECHAT: float = _env_float("QUANT_BIAS_WECHAT", 8.0)
QUANT_BIAS_FOLLOW_BUILDERS_X: float = _env_float("QUANT_BIAS_FOLLOW_BUILDERS_X", 12.0)
QUANT_BIAS_FOLLOW_BUILDERS_PODCAST: float = _env_float("QUANT_BIAS_FOLLOW_BUILDERS_PODCAST", 15.0)
QUANT_BIAS_FOLLOW_BUILDERS_BLOG: float = _env_float("QUANT_BIAS_FOLLOW_BUILDERS_BLOG", 14.0)

# 智能增量：签名未变化时，N 天后可强制重分析（0 表示关闭）
INCREMENTAL_REANALYZE_DAYS: int = _env_int("INCREMENTAL_REANALYZE_DAYS", 14)
# 势能爆发阈值：内容未变但 Star 增长达到此绝对值或倍率时，重新纳入分析
INCREMENTAL_MIN_STAR_BURST: int = _env_int("INCREMENTAL_MIN_STAR_BURST", 500)
INCREMENTAL_MIN_GROWTH_RATIO: float = _env_float("INCREMENTAL_MIN_GROWTH_RATIO", 0.5)

# ── 专家 Agent 配置 ──────────────────────────────────────
# 专家 Agent 可选择使用 sub 或 main 模型（默认 sub 以节省成本）
EXPERT_TECH_ROLE: str = os.getenv("EXPERT_TECH_ROLE", "sub")
EXPERT_APP_LANDING_ROLE: str = os.getenv("EXPERT_APP_LANDING_ROLE", "sub")
EXPERT_APP_USER_ROLE: str = os.getenv("EXPERT_APP_USER_ROLE", "sub")
EXPERT_CONCURRENCY: int = _env_int("EXPERT_CONCURRENCY", 5)

# ── 靶向追问（Critique）─────────────────────────────────
CRITIQUE_ENABLED: bool = _env_bool("CRITIQUE_ENABLED", True)
CRITIQUE_MIN_SCORE: int = _env_int("CRITIQUE_MIN_SCORE", 8)

# ── 报告 LLM 叙事 ────────────────────────────────────────
REPORT_LLM_ENABLED: bool = _env_bool("REPORT_LLM_ENABLED", True)
REPORT_MEMO_MIN_SCORE: int = _env_int("REPORT_MEMO_MIN_SCORE", 8)

AI_TOPICS: list[str] = [
    "llm", "agent", "large-language-model", "generative-ai",
    "rag", "fine-tuning", "multimodal", "text-generation",
    "reasoning", "ai-agent", "mcp", "vlm", "image-generation", "video-generation",
    "agentskill", "agentic", "agentic-ai", "agentic-ai-framework", "agentic-ai-agent", "agentic-ai-mcp", "agentic-ai-vlm", "agentic-ai-image-generation", "agentic-ai-video-generation",
]

# ── 日志级别 ─────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
