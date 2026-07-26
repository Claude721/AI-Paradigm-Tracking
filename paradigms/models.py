"""技术范式雷达的领域模型。

与旧版 ``RawProject`` 不同，这里的最小单位是“证据”，最终聚合单位是
“技术范式”。GitHub 仓库、社区帖子只作为证据，不能单独定义一个范式。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EvidenceType(str, Enum):
    PRIMARY_PAPER = "primary_paper"
    TECHNICAL_BLOG = "technical_blog"
    PEER_REVIEW = "peer_review"
    INDEPENDENT_REPLICATION = "independent_replication"
    IMPLEMENTATION = "implementation"
    CITATION = "citation"
    COMMUNITY_DISCUSSION = "community_discussion"
    SECONDARY_INTERPRETATION = "secondary_interpretation"
    PRODUCT_ADOPTION = "product_adoption"


@dataclass
class TechnicalEvidence:
    source: str
    evidence_type: EvidenceType
    title: str
    url: str
    summary: str = ""
    published_at: str = ""
    authors: list[str] = field(default_factory=list)
    organization: str = ""
    metrics: dict[str, float | int | str] = field(default_factory=dict)
    identifiers: dict[str, str] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """跨周稳定去重键：优先学术标识，最后才使用 URL。"""
        stable_id = (
            self.identifiers.get("doi")
            or self.identifiers.get("arxiv")
            or self.identifiers.get("openalex")
            or self.identifiers.get("semantic_scholar")
            or self.url.strip().lower().rstrip("/")
            or self.title.strip().lower()
        )
        payload = f"{self.evidence_type.value}:{stable_id}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evidence_type"] = self.evidence_type.value
        return result


@dataclass
class ResearcherProfile:
    name: str
    role: str = ""
    current_affiliation: str = ""
    prior_affiliations: list[str] = field(default_factory=list)
    research_trajectory: str = ""
    trajectory_consistency: float = 0.0
    representative_works: list[dict[str, Any]] = field(default_factory=list)
    profile_urls: dict[str, str] = field(default_factory=dict)
    public_email: str = ""
    public_email_source: str = ""
    identifiers: dict[str, str] = field(default_factory=dict)
    background_summary: str = ""
    public_bio_excerpt: str = ""
    key_person_reason: str = ""
    contact_search_notes: list[str] = field(default_factory=list)

    @property
    def public_contacts(self) -> dict[str, str]:
        """只输出已由公开来源返回的联系方式，不猜测邮箱。"""
        contacts = dict(self.profile_urls)
        if self.public_email and self.public_email_source:
            contacts["email"] = self.public_email
        return contacts


@dataclass
class ParadigmExtraction:
    evidence: TechnicalEvidence
    is_candidate: bool
    canonical_name: str
    thesis: str
    problem_shift: str
    mechanism: str
    route_family: str = ""
    background: str = ""
    design_philosophy: str = ""
    technical_explanation: str = ""
    application_value: str = ""
    why_now: str = ""
    evidence_assessment: str = ""
    trend_interpretation: str = ""
    open_questions: list[str] = field(default_factory=list)
    novelty_type: str = ""
    innovation_types: list[str] = field(default_factory=list)
    lineage_parent: str = ""
    keywords: list[str] = field(default_factory=list)
    claimed_results: list[str] = field(default_factory=list)
    # Rubric 是技术筛选的事实来源；以下 0-10 字段仅保留为兼容性派生值，
    # 不再接受模型直接打分，也不再作为独立硬门槛。
    rubric_assessment: dict[str, Any] = field(default_factory=dict)
    novelty_score: float = 0.0
    solidity_score: float = 0.0
    scope_score: float = 0.0
    incremental_penalty: float = 0.0
    rejection_reason: str = ""

    @property
    def normalized_key(self) -> str:
        text = self.canonical_name or self.mechanism or self.evidence.title
        return normalize_paradigm_name(text)


@dataclass
class ParadigmCandidate:
    key: str
    name: str
    thesis: str
    problem_shift: str
    mechanism: str
    why_now: str = ""
    evidence_assessment: str = ""
    trend_interpretation: str = ""
    open_questions: list[str] = field(default_factory=list)
    route_family: str = ""
    background: str = ""
    design_philosophy: str = ""
    technical_explanation: str = ""
    # 只在通过论文级技术门槛、进入深挖阶段后生成。它是总编辑的内部
    # 认知脚手架，不是最终报告里的固定栏目。
    mental_model: dict[str, Any] = field(default_factory=dict)
    application_value: str = ""
    secondary_discussion_summary: str = ""
    objective_momentum_signals: list[str] = field(default_factory=list)
    community_coverage: dict[str, str] = field(default_factory=dict)
    publisher_tier: str = "unknown"
    publisher_evidence: list[str] = field(default_factory=list)
    admission_reason: str = ""
    is_formal_technical_report: bool = False
    marketing_overclaim_risk: str = ""
    novelty_type: str = ""
    innovation_types: list[str] = field(default_factory=list)
    lineage_parent: str = ""
    lineage_path: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    evidence: list[TechnicalEvidence] = field(default_factory=list)
    researchers: list[ResearcherProfile] = field(default_factory=list)
    novelty_score: float = 0.0
    solidity_score: float = 0.0
    scope_score: float = 0.0
    momentum_score: float = 0.0
    researcher_score: float = 0.0
    volume_score: float = 0.0
    incremental_penalty: float = 0.0
    # screening_rubric 决定是否进入外部深挖；rubric_assessment 在综合后
    # 加入客观发布者/承接题，决定最终是否进入报告。
    screening_rubric: dict[str, Any] = field(default_factory=dict)
    rubric_assessment: dict[str, Any] = field(default_factory=dict)
    total_score: float = 0.0
    status: str = "watch"
    report_kind: str = "new"
    rejection_reason: str = ""

    @property
    def evidence_sources(self) -> set[str]:
        return {item.source for item in self.evidence}

    @property
    def effective_solidity_score(self) -> float:
        """由评分器注入的证据加成不会污染论文原始扎实度评分。"""
        from .scoring import effective_solidity_score

        return effective_solidity_score(self)

    @property
    def report_signature(self) -> str:
        payload = {
            "key": self.key,
            "evidence": sorted(
                (
                    item.fingerprint,
                    material_metric_signature(item.metrics),
                    str(item.raw.get("relationship", "")),
                    str(item.raw.get("independence", "")),
                )
                for item in self.evidence
            ),
            "mechanism": self.mechanism.strip(),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            **{
                key: value
                for key, value in asdict(self).items()
                if key not in {"evidence", "researchers"}
            },
            "evidence": [item.to_dict() for item in self.evidence],
            "researchers": [asdict(item) for item in self.researchers],
        }


def normalize_paradigm_name(text: str) -> str:
    """保留技术词与数字，移除容易造成伪差异的标点。"""
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text.lower()).strip("-")
    value = re.sub(r"-(framework|method|approach|model|models|system)$", "", value)
    return value[:120]


def candidate_from_dict(payload: dict[str, Any]) -> ParadigmCandidate:
    """从数据库 JSON 恢复候选，供“仅重新生成报告”模式使用。"""
    evidence = [technical_evidence_from_dict(item) for item in payload.get("evidence", [])]
    researchers = [
        ResearcherProfile(**item) for item in payload.get("researchers", [])
    ]
    fields = {
        key: value
        for key, value in payload.items()
        if key not in {"evidence", "researchers"}
    }
    return ParadigmCandidate(**fields, evidence=evidence, researchers=researchers)


def technical_evidence_from_dict(payload: dict[str, Any]) -> TechnicalEvidence:
    return TechnicalEvidence(
        **{
            **payload,
            "evidence_type": EvidenceType(payload["evidence_type"]),
        }
    )


def material_metric_signature(
    metrics: dict[str, float | int | str]
) -> tuple[tuple[str, int], ...]:
    """只在互动量跨越有意义量级时触发路线更新，避免每个 +1 都重发。"""
    tracked = (
        "citations",
        "upvotes",
        "comments",
        "stars",
        "forks",
        "likes",
        "retweets",
        "reposts",
        "replies",
        "score",
    )
    return tuple(
        (key, _metric_bucket(metrics.get(key, 0)))
        for key in tracked
        if _numeric(metrics.get(key, 0)) > 0
    )


def _metric_bucket(value: object) -> int:
    number = _numeric(value)
    thresholds = (1, 3, 10, 25, 50, 100, 250, 500, 1000, 5000, 10_000)
    return sum(number >= threshold for threshold in thresholds)


def _numeric(value: object) -> float:
    try:
        return max(float(value or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0
