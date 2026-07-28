"""用多种独立信号判断研究材料类型，避免把固定标题后缀当成唯一事实来源。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse


_EXPLICIT_REPORT_MARKERS = (
    "technical report",
    "tech report",
    "technical-report",
    "tech-report",
    "system report",
    "system-report",
    "research report",
    "research-report",
    "full report",
    "full-report",
    "architecture report",
    "methodology report",
    "white paper",
    "whitepaper",
    "system card",
    "model card",
    "技术报告",
    "系统报告",
    "研究报告",
    "完整报告",
    "白皮书",
    "模型卡",
    "系统卡",
)

_RELEASE_MARKERS = (
    "introducing",
    "we introduce",
    "we present",
    "we release",
    "release",
    "released",
    "launching",
    "announcing",
    "open-weight",
    "open weight",
    "model weights",
    "模型发布",
    "正式发布",
    "开源模型",
    "开放权重",
)

_MODEL_MARKERS = (
    "language model",
    "foundation model",
    "multimodal model",
    "vision-language",
    "vision language",
    "world model",
    "robot policy",
    "model weights",
    "mixture-of-experts",
    "mixture of experts",
    "大语言模型",
    "基础模型",
    "多模态模型",
    "世界模型",
    "机器人策略",
)

# 这里只用于判断“一份材料是否覆盖完整系统生命周期”，而不用于判断技术是否
# 新颖。必须跨多个层面同时命中，单个宽泛词不能把普通论文升级为系统报告。
_SYSTEM_LAYER_MARKERS: tuple[tuple[str, ...], ...] = (
    ("architecture", "attention", "expert", "backbone", "架构", "注意力"),
    (
        "pre-training",
        "pretraining",
        "training data",
        "data mixture",
        "预训练",
        "训练数据",
    ),
    (
        "post-training",
        "post training",
        "reinforcement learning",
        "alignment",
        "后训练",
        "强化学习",
        "对齐",
    ),
    (
        "infrastructure",
        "deployment",
        "serving",
        "co-design",
        "inference system",
        "基础设施",
        "部署",
        "推理系统",
    ),
    ("evaluation", "benchmark", "capability", "评测", "基准", "能力"),
    (
        "parameter",
        "context window",
        "context length",
        "model weights",
        "参数",
        "上下文",
        "模型权重",
    ),
)


@dataclass(frozen=True)
class PublicationClassification:
    origin_kind: str
    reason: str
    document_format: str
    system_layer_count: int


def classify_publication(
    *,
    title: str,
    url: str = "",
    summary: str = "",
    metadata: str = "",
    authors: list[str] | tuple[str, ...] = (),
    official: bool = False,
    discovered_by_report_query: bool = False,
) -> PublicationClassification:
    """综合显式元数据、文档结构、发布上下文与团队规模判断材料类型。

    `discovered_by_report_query` 只记录召回车道，绝不强制分类。搜索词可能只是
    命中了摘要里的参考文献或比较对象，若直接强制升级会产生大量假报告。
    """

    path = unquote(urlparse(url).path).casefold()
    explicit_text = _normalize(f"{title} {metadata} {path}")
    full_text = _normalize(f"{title} {summary}")
    explicit_marker = next(
        (marker for marker in _EXPLICIT_REPORT_MARKERS if marker in explicit_text),
        "",
    )
    layer_count = sum(
        any(marker in full_text for marker in markers)
        for markers in _SYSTEM_LAYER_MARKERS
    )
    release_signal = any(marker in full_text for marker in _RELEASE_MARKERS)
    model_signal = any(marker in full_text for marker in _MODEL_MARKERS)
    team_authorship = len(authors) >= 20 or any(
        _normalize(value).strip().endswith(" team") for value in authors
    )
    document_like = path.endswith(".pdf") or any(
        marker in path
        for marker in (
            "/report/",
            "/reports/",
            "/paper/",
            "/papers/",
            "/publication/",
            "/publications/",
            "whitepaper",
            "system-card",
            "model-card",
        )
    )

    if explicit_marker:
        return PublicationClassification(
            origin_kind="technical_report",
            reason=f"explicit_document_metadata:{explicit_marker}",
            document_format=_document_format(explicit_marker),
            system_layer_count=layer_count,
        )

    # 官方入口中的长篇系统材料可能使用品牌化标题，完全不写 report。此时只有
    # 文档形态、模型发布语义和至少四个系统层面同时成立才拆成多机制报告。
    if (
        official
        and document_like
        and release_signal
        and model_signal
        and layer_count >= 4
        and len(summary) >= 4_000
    ):
        return PublicationClassification(
            origin_kind="technical_report",
            reason="official_document_with_system_scope",
            document_format="system_report",
            system_layer_count=layer_count,
        )

    # arXiv 等学术入口没有可靠 owner 元数据时，使用“大型协作 + 正式发布 +
    # 多层系统贡献”的保守组合信号。任何一个单独信号都不足以升级。
    if release_signal and model_signal and team_authorship and layer_count >= 4:
        return PublicationClassification(
            origin_kind="technical_report",
            reason="inferred_system_scope_report",
            document_format="system_report",
            system_layer_count=layer_count,
        )

    if official and (release_signal or model_signal):
        return PublicationClassification(
            origin_kind="official_model_release",
            reason="official_release_context",
            document_format="release",
            system_layer_count=layer_count,
        )
    if official:
        return PublicationClassification(
            origin_kind="official_research",
            reason="official_research_entry",
            document_format="article",
            system_layer_count=layer_count,
        )
    return PublicationClassification(
        origin_kind="research_paper",
        reason=(
            "report_query_unconfirmed"
            if discovered_by_report_query
            else "ordinary_research_paper"
        ),
        document_format="paper",
        system_layer_count=layer_count,
    )


def report_query_terms() -> tuple[str, ...]:
    """供不同发现源共享可维护的显式文档词表；它不是唯一召回车道。"""

    return _EXPLICIT_REPORT_MARKERS


def looks_like_linked_research_document(label: str, url: str) -> bool:
    """识别官方文章中链接的完整报告/论文，不依赖文章标题本身。"""

    text = _normalize(f"{label} {unquote(urlparse(url).path)}")
    if urlparse(url).path.casefold().endswith(".pdf"):
        return True
    markers = (
        *_EXPLICIT_REPORT_MARKERS,
        "paper",
        "read paper",
        "full paper",
        "download paper",
        "论文",
        "阅读论文",
    )
    return any(marker in text for marker in markers)


def _document_format(marker: str) -> str:
    if "system card" in marker or "系统卡" in marker:
        return "system_card"
    if "model card" in marker or "模型卡" in marker:
        return "model_card"
    if "white" in marker or "白皮书" in marker:
        return "whitepaper"
    return "technical_report"


def _normalize(value: str) -> str:
    lowered = value.casefold()
    lowered = re.sub(r"[-_/]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()
