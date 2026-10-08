"""技术范式雷达的领域模型。

与旧版 ``RawProject`` 不同，这里的最小单位是“证据”，最终聚合单位是
“技术范式”。论文、原创技术文章与原生实现都只能先提出机制假说；是否形成
路线仍由技术 Rubric、发布者身份和外部承接共同决定。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from runtime_clock import research_now

class EvidenceType(str, Enum):
    PRIMARY_PAPER = "primary_paper"
    TECHNICAL_BLOG = "technical_blog"
    CONCEPT_ESSAY = "concept_essay"
    ORIGINAL_IMPLEMENTATION = "original_implementation"
    PEER_REVIEW = "peer_review"
    INDEPENDENT_REPLICATION = "independent_replication"
    IMPLEMENTATION = "implementation"
    CITATION = "citation"
    COMMUNITY_DISCUSSION = "community_discussion"
    SECONDARY_INTERPRETATION = "secondary_interpretation"
    PRODUCT_ADOPTION = "product_adoption"


# 能够提出一个新机制假说的“一手原点”。这是统一的领域契约，避免发现、
# 评分、持久化和报告各自维护一套不一致的论文/博客白名单。
ORIGIN_EVIDENCE_TYPES = frozenset(
    {
        EvidenceType.PRIMARY_PAPER,
        EvidenceType.TECHNICAL_BLOG,
        EvidenceType.CONCEPT_ESSAY,
        EvidenceType.ORIGINAL_IMPLEMENTATION,
    }
)


def nonnegative_number(value: object, default: float = 0.0) -> float:
    """Coerce untrusted counters without allowing NaN/Infinity downstream."""

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    return max(number, 0.0)


def safe_public_contact_target(label: str, target: str) -> str:
    """过滤旧状态或外部资料中不安全/格式异常的职业联系入口。"""

    value = str(target).strip()
    if label == "email":
        return (
            value
            if re.fullmatch(
                r"[^@\s<>()[\]]+@[^@\s<>()[\]]+\.[^@\s<>()[\]]+",
                value,
            )
            else ""
        )
    if any(character.isspace() for character in value) or any(
        character in value for character in "<>\\"
    ):
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    hostname = parsed.hostname.casefold()
    if parsed.username or parsed.password:
        return ""
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
        ".local"
    ):
        return ""
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return value
    return value if address.is_global else ""


_SIGNED_URL_QUERY_KEYS = {
    "expires",
    "policy",
    "signature",
    "x-amz-algorithm",
    "x-amz-credential",
    "x-amz-date",
    "x-amz-expires",
    "x-amz-security-token",
    "x-amz-signature",
    "x-amz-signedheaders",
}


def _canonical_material_url(value: str) -> str:
    """Canonicalize durable material links and reject expiring object URLs."""

    safe = safe_public_contact_target("source", value)
    if not safe:
        return ""
    parsed = urlsplit(safe)
    hostname = (parsed.hostname or "").casefold()
    # Hugging Face may redirect a stable resolve URL to a short-lived Xet/S3
    # download. Such a URL works during the run but is guaranteed to rot later.
    if (
        hostname.endswith(".cdn.hf.co")
        or hostname in {"cdn-lfs.huggingface.co", "cdn-lfs-us-1.huggingface.co"}
        or hostname.endswith(".xethub.hf.co")
    ):
        return ""
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query_keys = {key.casefold() for key, _ in query_pairs}
    if query_keys & _SIGNED_URL_QUERY_KEYS:
        return ""
    path = parsed.path
    if hostname == "huggingface.co" and "/resolve/" in path:
        path = path.replace("/resolve/", "/blob/", 1)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            path,
            urlencode(query_pairs, doseq=True),
            "",
        )
    )


def primary_material_url(evidence: "TechnicalEvidence") -> str:
    """Return a direct, public first-party/academic material URL.

    Bibliographic and paper-discovery pages are useful for recall but are not
    the original work the user must be able to open from the report.
    """

    # Fetchers may follow a stable public URL to a signed CDN object. Preserve
    # their explicitly recorded canonical input URL instead of reporting the
    # transient response URL.
    value = _canonical_material_url(
        str(evidence.raw.get("canonical_source_url", ""))
    ) or _canonical_material_url(evidence.url)
    if not value:
        return ""
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").casefold().removeprefix("www.")
    if hostname in {"openalex.org", "api.openalex.org"}:
        doi = str(evidence.identifiers.get("doi", "")).strip()
        arxiv_id = str(evidence.identifiers.get("arxiv", "")).strip()
        if doi:
            return f"https://doi.org/{doi.removeprefix('https://doi.org/')}"
        if arxiv_id:
            versionless = re.sub(r"v\d+$", "", arxiv_id)
            return f"https://arxiv.org/abs/{versionless}"
        return ""
    if hostname == "huggingface.co" and parsed.path.startswith("/papers/"):
        arxiv_id = str(evidence.identifiers.get("arxiv", "")).strip()
        versionless = re.sub(r"v\d+$", "", arxiv_id)
        return (
            f"https://arxiv.org/abs/{versionless}"
            if arxiv_id
            else ""
        )
    return value


def _evidence_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _material_uptake_delta(
    evidence: "TechnicalEvidence",
    *,
    reference_time: datetime | None = None,
    window_days: int = 30,
) -> bool:
    """Require a dated observation and a visible magnitude change.

    A persisted delta without an observation time is legacy context, not a
    perpetual event that can reactivate an old route every subsequent week.
    """

    delta = evidence.raw.get("metric_delta")
    if not isinstance(delta, dict):
        return False
    observed = _evidence_datetime(evidence.raw.get("metric_delta_observed_at"))
    now = research_now(reference_time)
    if not observed or not now - timedelta(days=max(window_days, 1)) <= observed <= now + timedelta(days=1):
        return False

    return _material_metric_delta_amount(evidence)


def _material_metric_delta_amount(evidence: "TechnicalEvidence") -> bool:
    delta = evidence.raw.get("metric_delta")
    if not isinstance(delta, dict):
        return False

    def amount(name: str) -> float:
        return nonnegative_number(delta.get(name, 0))

    if evidence.evidence_type == EvidenceType.CITATION:
        return amount("citations") >= 3 or amount("influential_citations") >= 1
    if evidence.evidence_type in {
        EvidenceType.IMPLEMENTATION,
        EvidenceType.INDEPENDENT_REPLICATION,
        EvidenceType.PRODUCT_ADOPTION,
    }:
        return amount("stars") >= 25 or amount("forks") >= 3
    if evidence.evidence_type in {
        EvidenceType.COMMUNITY_DISCUSSION,
        EvidenceType.SECONDARY_INTERPRETATION,
    }:
        return (
            amount("comments") >= 5 or amount("replies") >= 5
            or amount("likes") >= 20 or amount("score") >= 20
        )
    return False


def material_metric_event_signature(
    evidence: "TechnicalEvidence", *, route_key: str
) -> tuple:
    """Stable event identity even when two counts lie in the same bucket."""
    if evidence.evidence_type in {
        EvidenceType.COMMUNITY_DISCUSSION,
        EvidenceType.SECONDARY_INTERPRETATION,
    } and not is_verified_substantive_discussion(evidence, route_key=route_key):
        return ()
    if evidence.evidence_type in {
        EvidenceType.IMPLEMENTATION,
        EvidenceType.INDEPENDENT_REPLICATION,
        EvidenceType.PRODUCT_ADOPTION,
    } and evidence.raw.get("independence") not in {
        "independent", "official", "publisher"
    }:
        return ()
    if not _material_metric_delta_amount(evidence):
        return ()
    observed_at = str(evidence.raw.get("metric_delta_observed_at", ""))
    if not _evidence_datetime(observed_at):
        return ()
    delta = evidence.raw["metric_delta"]
    return (
        observed_at,
        tuple(sorted(
            (str(key), nonnegative_number(value))
            for key, value in delta.items()
            if nonnegative_number(value) > 0
        )),
    )


DISCUSSION_RELATIONSHIPS = frozenset({
    "independent_commentary",
    "independent_discussion",
    "independent_analysis",
    "mechanism_discussion",
    "independent_mechanism_analysis",
})


def is_verified_substantive_discussion(
    evidence: "TechnicalEvidence", *, route_key: str
) -> bool:
    raw = evidence.raw or {}
    return bool(
        evidence.evidence_type in {
            EvidenceType.COMMUNITY_DISCUSSION,
            EvidenceType.SECONDARY_INTERPRETATION,
        }
        and not raw.get("indexed_discovery_only")
        and raw.get("substantive_uptake") is True
        and raw.get("substantive_uptake_source") == "synthesis-v1"
        and bool(route_key)
        and raw.get("substantive_uptake_route_key") == route_key
        and str(raw.get("independence", "")).casefold() == "independent"
        and str(raw.get("relationship", "")).casefold()
        in DISCUSSION_RELATIONSHIPS
    )


def _qualifying_current_uptake(
    evidence: "TechnicalEvidence",
    *,
    route_key: str,
    reference_time: datetime | None = None,
    window_days: int = 30,
) -> tuple[bool, str]:
    """Return whether one secondary record can reactivate an old route.

    Search hits and generic recent commentary are intentionally insufficient.
    A discussion source must have completed an upstream substantive-link audit;
    otherwise the safe V0 behavior is to keep the route in observation.
    """

    raw = evidence.raw or {}
    if raw.get("indexed_discovery_only"):
        return False, "仅为搜索索引命中"
    relationship = str(raw.get("relationship", "")).casefold()
    independence = str(raw.get("independence", "")).casefold()
    if evidence.evidence_type == EvidenceType.IMPLEMENTATION and independence in {
        "official", "publisher"
    }:
        if _material_uptake_delta(
            evidence, reference_time=reference_time, window_days=window_days
        ):
            return True, "官方实现采用指标出现量级变化；不等于独立复现"
        return False, "官方实现自身活动或微小指标变化不足以构成本期进展"
    if relationship in {
        "author_self_release",
        "official_release_repository",
        "publisher_self_release",
        "paper_linked_repository",
        "publisher_original_implementation",
    } or independence in {"author", "publisher", "official", "self"}:
        return False, "发布者/作者自身活动不属于独立承接"

    evidence_type = evidence.evidence_type
    if evidence_type in {
        EvidenceType.INDEPENDENT_REPLICATION,
        EvidenceType.PRODUCT_ADOPTION,
    }:
        return (
            (True, "独立复现或产品采用")
            if independence == "independent"
            else (False, "复现/采用的独立性未核验")
        )
    if evidence_type == EvidenceType.IMPLEMENTATION:
        return (
            (True, "独立实现")
            if independence == "independent"
            else (False, "实现的独立性未核验")
        )
    if evidence_type == EvidenceType.PEER_REVIEW:
        return (
            (True, "独立同行评议")
            if independence == "independent"
            else (False, "评议独立性未核验")
        )
    if evidence_type == EvidenceType.CITATION:
        return (
            (True, "引用指标出现可核验量级变化")
            if _material_uptake_delta(
                evidence, reference_time=reference_time, window_days=window_days
            )
            else (False, "引用总量或微小变化不足以构成本期进展")
        )
    if evidence_type in {
        EvidenceType.COMMUNITY_DISCUSSION,
        EvidenceType.SECONDARY_INTERPRETATION,
    }:
        if independence != "independent":
            return False, "讨论者独立性未核验"
        if raw.get("substantive_uptake") is not True:
            return False, "尚未核验为直接讨论本机制的实质承接"
        if relationship not in DISCUSSION_RELATIONSHIPS:
            return False, "讨论与本机制的关系类型未闭合"
        return (
            (True, "独立且直接关联本机制的实质讨论")
            if is_verified_substantive_discussion(evidence, route_key=route_key)
            else (False, "讨论核验状态未闭合")
        )
    return False, "证据类型不构成历史路线更新"


def assess_candidate_freshness(
    candidate: "ParadigmCandidate",
    *,
    reference_time: datetime | None = None,
    window_days: int = 30,
) -> dict[str, Any]:
    """Separate a newly published route from an old route with new uptake.

    A clean database only means "not seen by this installation". It must never
    turn an old, undated official page into a current-week breakthrough.
    """

    now = research_now(reference_time)
    cutoff = now - timedelta(days=max(window_days, 1))
    primary_types = ORIGIN_EVIDENCE_TYPES
    uptake_types = {
        EvidenceType.PEER_REVIEW,
        EvidenceType.INDEPENDENT_REPLICATION,
        EvidenceType.IMPLEMENTATION,
        EvidenceType.CITATION,
        EvidenceType.COMMUNITY_DISCUSSION,
        EvidenceType.SECONDARY_INTERPRETATION,
        EvidenceType.PRODUCT_ADOPTION,
    }
    dated_primary: list[tuple[datetime, TechnicalEvidence]] = []
    undated_primary: list[TechnicalEvidence] = []
    current_uptake: list[TechnicalEvidence] = []
    for evidence in candidate.evidence:
        if evidence.evidence_type in primary_types:
            published = _evidence_datetime(evidence.published_at)
            if published:
                dated_primary.append((published, evidence))
            else:
                undated_primary.append(evidence)
            continue
        if evidence.evidence_type not in uptake_types:
            continue
        published = _evidence_datetime(evidence.published_at)
        qualifying, _ = _qualifying_current_uptake(
            evidence, route_key=candidate.key,
            reference_time=now, window_days=window_days
        )
        current_date = bool(
            published and cutoff <= published <= now + timedelta(days=1)
        )
        if qualifying and (
            current_date
            or _material_uptake_delta(
                evidence, reference_time=now, window_days=window_days
            )
        ):
            current_uptake.append(evidence)

    recent_primary = [
        evidence
        for published, evidence in dated_primary
        if cutoff <= published <= now + timedelta(days=1)
    ]
    latest_primary = max((value for value, _ in dated_primary), default=None)
    if recent_primary:
        classification = "recent_primary"
        decision = "include"
        reason = "存在窗口内可核验发布日期的一手材料"
    elif current_uptake:
        classification = "historical_reactivated"
        decision = "include"
        reason = "一手材料不是本期新发布，但存在窗口内独立承接或可核验指标增量"
    elif dated_primary:
        classification = "historical_without_current_uptake"
        decision = "defer"
        reason = "一手材料早于本期窗口，且没有窗口内独立承接"
    else:
        classification = "unknown_date_without_current_uptake"
        decision = "defer"
        reason = "一手材料发布日期不可核验，且没有窗口内独立承接"
    return {
        "version": "freshness-v2",
        "classification": classification,
        "decision": decision,
        "reason": reason,
        "window_days": max(window_days, 1),
        "latest_primary_published_at": (
            latest_primary.isoformat() if latest_primary else ""
        ),
        "recent_primary_urls": [
            primary_material_url(value) for value in recent_primary
            if primary_material_url(value)
        ],
        "current_uptake_urls": [
            value.url for value in current_uptake
            if safe_public_contact_target("source", value.url)
        ],
        "current_uptake_evidence": [
            {
                "title": value.title,
                "url": value.url,
                "published_at": value.published_at,
                "evidence_type": value.evidence_type.value,
                "relationship": str(value.raw.get("relationship", "")),
                "independence": str(value.raw.get("independence", "")),
                "metric_delta": value.raw.get("metric_delta", {}),
                "qualification_reason": _qualifying_current_uptake(
                    value, route_key=candidate.key,
                    reference_time=now, window_days=window_days
                )[1],
            }
            for value in current_uptake
            if safe_public_contact_target("source", value.url)
        ],
        "undated_primary_count": len(undated_primary),
    }


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
    # Bound by persistence before hydration; empty on fresh source observations.
    source_revision: str = ""

    def __post_init__(self) -> None:
        """Normalize nullable external fields before they reach persistence.

        Several upstream JSON APIs use ``null`` for an unknown publication
        date even when the field itself is present.  Dataclass annotations do
        not enforce runtime types, so without this boundary ``None`` can be
        serialized into SQLite and only fail when the pending queue is read
        back.  Missing optional values are losslessly represented by the
        domain defaults; non-null structural type errors remain visible to the
        persistence validator.
        """

        for name in (
            "source",
            "title",
            "url",
            "summary",
            "published_at",
            "organization",
        ):
            if getattr(self, name) is None:
                setattr(self, name, "")
        for name in ("authors", "keywords"):
            if getattr(self, name) is None:
                setattr(self, name, [])
        for name in ("metrics", "identifiers", "raw"):
            if getattr(self, name) is None:
                setattr(self, name, {})

    @property
    def fingerprint(self) -> str:
        """跨周稳定去重键：优先学术标识，最后才使用 URL。"""
        stable_id = (
            self.identifiers.get("doi")
            or self.identifiers.get("arxiv")
            or self.identifiers.get("openalex")
            or self.identifiers.get("semantic_scholar")
            or self.identifiers.get("forum_post")
            or self.url.strip().lower().rstrip("/")
            or self.title.strip().lower()
        )
        payload = f"{self.evidence_type.value}:{stable_id}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evidence_type"] = self.evidence_type.value
        return result


EPHEMERAL_PERSISTABLE_RAW_KEYS = frozenset({
    "relationship", "independence", "indexed_discovery_only",
    "metrics_unavailable", "coverage", "ephemeral_content",
    "social_platform", "social_profile_url", "subreddit",
    "retention_policy", "substantive_uptake", "substantive_uptake_source",
    "substantive_uptake_route_key",
    "historical", "content_scrubbed", "metric_delta", "metric_baseline",
    "metric_delta_observed_at",
})
_EPHEMERAL_METRIC_DELTA_KEYS = frozenset({
    "citations", "influential_citations", "stars", "forks", "likes",
    "replies", "comments", "score", "upvotes", "retweets", "reposts",
})


def scrub_ephemeral_evidence(evidence: TechnicalEvidence) -> None:
    """Remove user-authored content before any research-state persistence."""
    if not evidence.raw.get("ephemeral_content"):
        return
    platform = str(evidence.raw.get("social_platform") or evidence.source)
    labels = {
        "reddit": "Reddit 公开讨论",
        "tavily-reddit": "Reddit 公开索引线索",
        "x": "X 公开索引线索",
        "tavily-x": "X 公开索引线索",
        "xiaohongshu": "小红书公开索引线索",
        "tavily-xiaohongshu": "小红书公开索引线索",
        "web": "独立技术网页索引线索",
        "tavily-web": "独立技术网页索引线索",
    }
    stable_id = next(iter(evidence.identifiers.values()), "")
    evidence.title = labels.get(platform, "社区公开讨论")
    if stable_id:
        evidence.title = f"{evidence.title}（{stable_id}）"
    evidence.summary = ""
    evidence.authors = []
    evidence.raw = {
        key: value
        for key, value in evidence.raw.items()
        if key in EPHEMERAL_PERSISTABLE_RAW_KEYS
    }
    delta = evidence.raw.get("metric_delta")
    if isinstance(delta, dict):
        evidence.raw["metric_delta"] = {
            key: number
            for key, value in delta.items()
            if key in _EPHEMERAL_METRIC_DELTA_KEYS
            if (number := nonnegative_number(value)) > 0
        }
    else:
        evidence.raw.pop("metric_delta", None)
    baseline = evidence.raw.get("metric_baseline")
    if isinstance(baseline, dict):
        evidence.raw["metric_baseline"] = {
            key: nonnegative_number(value)
            for key, value in baseline.items()
            if key in _EPHEMERAL_METRIC_DELTA_KEYS
        }
    else:
        evidence.raw.pop("metric_baseline", None)
    observed = _evidence_datetime(evidence.raw.get("metric_delta_observed_at"))
    if observed:
        evidence.raw["metric_delta_observed_at"] = observed.isoformat()
    else:
        evidence.raw.pop("metric_delta_observed_at", None)
    evidence.raw["content_scrubbed"] = True


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
        contacts = {
            label: safe
            for label, value in self.profile_urls.items()
            if (safe := safe_public_contact_target(label, value))
        }
        if self.public_email and self.public_email_source:
            email = safe_public_contact_target("email", self.public_email)
            if email:
                contacts["email"] = email
        return contacts

    @property
    def contact_lookup_completed(self) -> bool:
        """身份种子不等于完成过公开联系入口检索。"""

        if self.public_contacts:
            return True
        completed_markers = (
            "已检索",
            "检索失败",
            "检索返回",
            "已检查公开个人主页",
            "个人主页返回",
            "个人主页读取失败",
            "交叉核验通过",
        )
        return any(
            any(marker in note for marker in completed_markers)
            and "未配置" not in note
            and "未执行" not in note
            for note in self.contact_search_notes
        )


def plausible_researcher_name(name: str) -> bool:
    """Reject layout fragments, URLs and joined contacts before identity work.

    Author metadata is assembled from HTML, PDF text and third-party indexes.  A
    malformed author token must never become a person merely because a later
    lookup happened to return something.  This is deliberately a syntax gate,
    not an ethnicity-specific name dictionary.
    """

    value = re.sub(r"\s+", " ", str(name or "")).strip()
    if not 2 <= len(value) <= 100 or not any(char.isalpha() for char in value):
        return False
    if any(char.isdigit() or ord(char) < 32 for char in value):
        return False
    if any(token in value.casefold() for token in ("http://", "https://", "mailto:")):
        return False
    if any(char in value for char in "@{}[]<>\\/|=_:;\n\r\t"):
        return False
    if len(value.split()) > 10:
        return False
    return all(
        char.isalpha()
        or char.isspace()
        or char in "-.'’,·"
        for char in value
    )


def key_researcher_profiles(
    profiles: list[ResearcherProfile],
    limit: int = 3,
) -> list[ResearcherProfile]:
    """选择需要承担路线归因的人物，而不是机械要求每位合作者过闸门。

    一手材料明确标注的一作/通讯/负责人优先，其次是末位资深作者和已经
    形成关键人物判断的人。若元数据没有角色，至少保留第一位具名作者。
    这只影响人物核验与报告篇幅，不会改变完整作者名单或技术 Rubric。
    """

    named = [
        profile for profile in profiles if plausible_researcher_name(profile.name)
    ]
    if not named:
        return []
    target = max(int(limit or 0), 1)
    primary_markers = (
        "第一作者",
        "共同第一",
        "共同一作",
        "first author",
        "lead author",
        "通讯",
        "corresponding",
        "负责人",
        "project lead",
        "重点研究者",
        "priority researcher",
    )
    senior_markers = (
        "末位",
        "资深",
        "senior",
        "principal investigator",
        r"\bpi\b",
    )

    def matches(profile: ResearcherProfile, markers: tuple[str, ...]) -> bool:
        role = profile.role.casefold()
        return any(
            re.search(marker, role) if marker.startswith("\\b") else marker in role
            for marker in markers
        )

    selected: list[ResearcherProfile] = []

    def add(profile: ResearcherProfile) -> None:
        if (
            len(selected) < target
            and profile not in selected
        ):
            selected.append(profile)

    # 每类先取一位，避免多位“前列作者/共同一作待核验”挤掉明确的资深作者。
    primary = next((item for item in named if matches(item, primary_markers)), None)
    senior = next((item for item in named if matches(item, senior_markers)), None)
    if primary:
        add(primary)
    if senior:
        add(senior)
    for profile in named:
        if profile.key_person_reason.strip():
            add(profile)
    for profile in named:
        if matches(profile, primary_markers) or matches(profile, senior_markers):
            add(profile)
    if not selected:
        add(named[0])
    return selected


def delivery_researcher_profiles(
    profiles: list[ResearcherProfile],
    limit: int = 3,
) -> list[ResearcherProfile]:
    """Return only people safe enough to attribute in a public report.

    An OpenAlex name hit by itself is not identity proof.  Delivery requires a
    usable background, a completed public-contact search, and either a direct
    professional identity anchor or an explicit current-work alignment note.
    Unready profiles remain in the candidate for future enrichment.
    """

    direct_labels = {
        "homepage",
        "orcid",
        "google_scholar",
        "semantic_scholar",
        "linkedin",
        "github",
    }
    ready: list[ResearcherProfile] = []
    for profile in key_researcher_profiles(profiles, limit):
        has_background = bool(
            profile.current_affiliation
            or profile.background_summary
            or profile.prior_affiliations
            or profile.research_trajectory
            or profile.key_person_reason
        )
        direct_identity = bool(
            direct_labels.intersection(profile.public_contacts)
            or (
                profile.public_email
                and profile.public_email_source
                and "email" in profile.public_contacts
            )
        )
        current_work_aligned = any(
            ("当前论文" in note or "当前工作" in note)
            and (
                "交叉核验通过" in note
                or "身份对齐通过" in note
                or ("已与当前" in note and "对齐" in note)
            )
            for note in profile.contact_search_notes
        )
        if (
            plausible_researcher_name(profile.name)
            and has_background
            and profile.contact_lookup_completed
            and (direct_identity or current_work_aligned)
        ):
            ready.append(profile)
    return ready


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
    # 新材料与“第一次进入本地数据库”必须分开。该字段只用于交付时效审计，
    # 不改变技术 Rubric 的结论。
    freshness_assessment: dict[str, Any] = field(default_factory=dict)
    total_score: float = 0.0
    status: str = "watch"
    report_kind: str = "new"
    rejection_reason: str = ""
    # 运行异常不是研究结论。跨轮保留计数只用于执行公平性和审计，避免
    # 一个反复触发外部异常的候选长期占据同优先级队首。
    execution_failure_count: int = 0
    last_execution_failure_at: str = ""
    # Only a fully closed synthesis may be reused. The input signature is
    # derived from source-owned origin revisions, not transient community text.
    deep_checkpoint_stage: str = ""
    deep_checkpoint_input_signature: str = ""
    deep_checkpoint_support_signature: str = ""
    deep_checkpoint_created_at: str = ""
    deep_checkpoint_trajectory_signature: str = ""
    deep_checkpoint_synthesis_rubric: dict[str, Any] = field(default_factory=dict)

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
                    material_metric_event_signature(item, route_key=self.key),
                    str(item.raw.get("relationship", "")) + (
                        ":route-certified"
                        if is_verified_substantive_discussion(
                            item, route_key=self.key
                        )
                        else ""
                    ),
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


def verified_organization_attribution(
    candidate: ParadigmCandidate,
) -> dict[str, str]:
    """为未披露自然人贡献结构的正式组织发布保留可核验归因。

    不把组织虚构成人物，也不允许未知网页借此绕过人物契约。只有已建立
    发布者，或已核验组织的正式技术报告，且存在安全一手 URL 时才成立。
    """

    official_organization_release = any(
        evidence.evidence_type
        in {
            EvidenceType.TECHNICAL_BLOG,
            EvidenceType.CONCEPT_ESSAY,
            EvidenceType.ORIGINAL_IMPLEMENTATION,
        }
        and str(evidence.raw.get("origin_kind", "")).startswith("official_")
        for evidence in candidate.evidence
    )
    if candidate.publisher_tier == "established":
        publisher_ready = bool(
            candidate.is_formal_technical_report or official_organization_release
        )
    else:
        publisher_ready = bool(
            candidate.publisher_tier == "verified"
            and candidate.is_formal_technical_report
        )
    if not publisher_ready:
        return {}
    for evidence in candidate.evidence:
        if evidence.evidence_type not in ORIGIN_EVIDENCE_TYPES:
            continue
        organization = evidence.organization.strip()
        source_url = primary_material_url(evidence)
        if organization and source_url:
            return {"name": organization, "source_url": source_url}
    return {}


def normalize_paradigm_name(text: str) -> str:
    """保留技术词与数字，移除容易造成伪差异的标点。"""
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text.lower()).strip("-")
    value = re.sub(r"-(framework|method|approach|model|models|system)$", "", value)
    return value[:120]


def candidate_from_dict(payload: dict[str, Any]) -> ParadigmCandidate:
    """从数据库 JSON 恢复候选，供“仅重新生成报告”模式使用。"""
    if not isinstance(payload, dict):
        raise ValueError("候选状态 payload 必须是 JSON object")
    _validate_payload_types(
        payload,
        "候选状态",
        strings={
            "key", "name", "thesis", "problem_shift", "mechanism", "why_now",
            "evidence_assessment", "trend_interpretation", "route_family",
            "background", "design_philosophy", "technical_explanation",
            "application_value", "secondary_discussion_summary", "publisher_tier",
            "admission_reason", "marketing_overclaim_risk", "novelty_type",
            "lineage_parent", "status", "report_kind", "rejection_reason",
            "last_execution_failure_at", "deep_checkpoint_stage",
            "deep_checkpoint_input_signature", "deep_checkpoint_support_signature",
            "deep_checkpoint_created_at", "deep_checkpoint_trajectory_signature",
        },
        lists={
            "open_questions", "objective_momentum_signals", "publisher_evidence",
            "innovation_types", "lineage_path", "keywords", "evidence",
            "researchers",
        },
        mappings={
            "mental_model", "community_coverage", "screening_rubric",
            "rubric_assessment", "freshness_assessment",
            "deep_checkpoint_synthesis_rubric",
        },
        numbers={
            "novelty_score", "solidity_score", "scope_score", "momentum_score",
            "researcher_score", "volume_score", "incremental_penalty",
            "total_score", "execution_failure_count",
        },
    )
    raw_evidence = payload.get("evidence", [])
    raw_researchers = payload.get("researchers", [])
    if not isinstance(raw_evidence, list) or not isinstance(raw_researchers, list):
        raise ValueError("候选状态中的 evidence/researchers 必须是数组")
    evidence = [technical_evidence_from_dict(item) for item in raw_evidence]
    researchers = [
        _researcher_profile_from_dict(item)
        for item in raw_researchers
    ]
    known = {value.name for value in fields(ParadigmCandidate)}
    payload_fields = {
        key: value
        for key, value in payload.items()
        if key in known and key not in {"evidence", "researchers"}
    }
    return ParadigmCandidate(
        **payload_fields,
        evidence=evidence,
        researchers=researchers,
    )


def technical_evidence_from_dict(payload: dict[str, Any]) -> TechnicalEvidence:
    if not isinstance(payload, dict):
        raise ValueError("证据状态 payload 必须是 JSON object")
    _validate_payload_types(
        payload,
        "证据状态",
        strings={
            "source", "evidence_type", "title", "url", "summary",
            "published_at", "organization", "source_revision",
        },
        lists={"authors", "keywords"},
        mappings={"metrics", "identifiers", "raw"},
    )
    values = _known_dataclass_fields(TechnicalEvidence, payload)
    if "evidence_type" not in values:
        raise ValueError("证据状态缺少 evidence_type")
    values["evidence_type"] = EvidenceType(values["evidence_type"])
    return TechnicalEvidence(**values)


def _researcher_profile_from_dict(payload: dict[str, Any]) -> ResearcherProfile:
    _validate_payload_types(
        payload,
        "研究者状态",
        strings={
            "name", "role", "current_affiliation", "research_trajectory",
            "public_email", "public_email_source", "background_summary",
            "public_bio_excerpt", "key_person_reason",
        },
        lists={
            "prior_affiliations", "representative_works", "contact_search_notes",
        },
        mappings={"profile_urls", "identifiers"},
        numbers={"trajectory_consistency"},
    )
    return ResearcherProfile(
        **_known_dataclass_fields(ResearcherProfile, payload)
    )


def _validate_payload_types(
    payload: dict[str, Any],
    label: str,
    *,
    strings: set[str] | None = None,
    lists: set[str] | None = None,
    mappings: set[str] | None = None,
    numbers: set[str] | None = None,
) -> None:
    """Fail closed on domain-shape corruption while allowing retired fields."""

    if not isinstance(payload, dict):
        raise ValueError(f"{label} payload 必须是 JSON object")
    expectations = (
        (strings or set(), str, "字符串"),
        (lists or set(), list, "数组"),
        (mappings or set(), dict, "object"),
        (numbers or set(), (int, float), "数字"),
    )
    for names, expected, expected_label in expectations:
        for name in names:
            if name in payload and not isinstance(payload[name], expected):
                raise ValueError(
                    f"{label}字段 {name} 必须是{expected_label}"
                )
    for name in numbers or set():
        if name in payload and not math.isfinite(float(payload[name])):
            raise ValueError(f"{label}字段 {name} 必须是有限数字")


def _known_dataclass_fields(model, payload: dict[str, Any]) -> dict[str, Any]:
    """Ignore retired JSON fields while keeping malformed rows fail-closed.

    State artifacts can outlive a code revision. Additive dataclass changes are
    handled by defaults, and retired fields should not make a healthy older
    artifact unrestorable. Structural type errors still raise so migration can
    fall back to an older immutable snapshot instead of hiding corruption.
    """

    if not isinstance(payload, dict):
        raise ValueError(f"{model.__name__} payload 必须是 JSON object")
    known = {value.name for value in fields(model)}
    return {key: value for key, value in payload.items() if key in known}


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
    return nonnegative_number(value)
