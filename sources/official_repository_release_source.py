"""Discover recent releases from verified research organizations on GitHub.

A repository linked to a paper/technical page preserves that external origin.
A repository-native mechanism may seed an implementation origin when its README
actually specifies a technical intervention. Neither path bypasses the rubric.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urlparse

import httpx

import config
from paradigms.models import EvidenceType, TechnicalEvidence, nonnegative_number
from paradigms.publication import (
    classify_publication,
    looks_like_linked_research_document,
)
from research_watchlist import (
    OFFICIAL_GITHUB_ORGANIZATIONS,
    RESEARCH_SOURCES,
    organization_record,
    organization_tier,
)

logger = logging.getLogger(__name__)


class OfficialRepositoryReleaseSource:
    """Watch first-party repositories without equating a repo with a paradigm."""

    source_name = "official-repository-release"

    def __init__(
        self,
        lookback_days: int = 30,
        organizations: tuple[dict, ...] | list[dict] | None = None,
        concurrency: int = 6,
    ) -> None:
        self.lookback_days = max(lookback_days, 1)
        self.organizations = list(
            OFFICIAL_GITHUB_ORGANIZATIONS
            if organizations is None
            else organizations
        )
        self.concurrency = max(concurrency, 1)
        self._coverage: dict[str, object] = {}

    async def safe_fetch(self) -> list[TechnicalEvidence]:
        if not config.GITHUB_TOKEN:
            self.fetch_status = "not_configured"
            self.fetch_error = ""
            self._coverage = {
                "status": "not_configured",
                "configured_organizations": len(self.organizations),
                "checked_organizations": 0,
                "failed_organizations": [],
                "repository_page_failures": [],
                "recent_repositories": 0,
                "linked_primary_origins": 0,
                "repository_native_origins": 0,
                "repository_only_releases": [],
                "unverified_primary_releases": [],
                "external_primary_targets": 0,
                "external_primary_validation_failures": 0,
            }
            return []
        try:
            results = await self.fetch()
            failed = self._coverage.get("failed_organizations") or []
            page_failures = self._coverage.get("repository_page_failures") or []
            configured = int(self._coverage.get("configured_organizations", 0) or 0)
            checked = int(self._coverage.get("checked_organizations", 0) or 0)
            primary_targets = int(
                self._coverage.get("external_primary_targets", 0) or 0
            )
            primary_failures = int(
                self._coverage.get("external_primary_validation_failures", 0)
                or 0
            )
            if configured and checked == 0:
                self.fetch_status = "query_failed"
                self.fetch_error = "AllOrganizationQueriesFailed"
            elif configured and checked / configured < 0.8:
                self.fetch_status = "partial"
                self.fetch_error = "MaterialOrganizationCoverageLoss"
            elif page_failures:
                # Once a full page inside the active window has been read, a
                # failure on the next page is a known cardinality loss for that
                # organization. Its absolute share of a large watchlist is
                # irrelevant: one busy lab can publish most releases in a week.
                self.fetch_status = "partial"
                self.fetch_error = "MaterialRepositoryPaginationLoss"
            elif primary_targets and primary_failures / primary_targets >= 0.5:
                self.fetch_status = "partial"
                self.fetch_error = "MaterialPrimaryValidationLoss"
            elif failed or primary_failures:
                # A renamed/deleted optional organization must remain visible in
                # audit, but one stale watchlist entry must not permanently turn
                # every otherwise complete weekly run into research_incomplete.
                self.fetch_status = "completed_with_warnings"
                self.fetch_error = (
                    "MinorOrganizationFailures"
                    if failed
                    else (
                        "MinorPrimaryValidationFailures"
                    )
                )
            else:
                self.fetch_status = "completed"
                self.fetch_error = ""
            self._coverage["status"] = self.fetch_status
            return results
        except Exception as exc:
            self.fetch_status = "query_failed"
            self.fetch_error = type(exc).__name__
            logger.exception("[official-repository-release] 获取失败")
            return []

    async def fetch(self) -> list[TechnicalEvidence]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "AI-Paradigm-Radar/3.2",
        }
        semaphore = asyncio.Semaphore(self.concurrency)

        async def get_repositories(client: httpx.AsyncClient, item: dict):
            login = str(item["login"])
            repositories: list[dict] = []
            page = 1
            while True:
                try:
                    async with semaphore:
                        response = await client.get(
                            f"https://api.github.com/orgs/{login}/repos",
                            params={
                                "type": "public",
                                "sort": "created",
                                "direction": "desc",
                                "per_page": 100,
                                "page": page,
                            },
                            headers=headers,
                        )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, list):
                        raise ValueError("GitHub repository response is not a list")
                except Exception as exc:
                    if repositories:
                        return (
                            item,
                            repositories,
                            page - 1,
                            f"page_{page}:{type(exc).__name__}",
                        )
                    raise

                repositories.extend(payload)
                parsed_dates = [
                    value
                    for value in (
                        _parse_date(str(repository.get("created_at", "")))
                        for repository in payload
                    )
                    if value is not None
                ]
                # GitHub guarantees descending creation order for this query.
                # A short page or the first window-external repository closes
                # the scan without a hidden per-organization Top-K.
                if len(payload) < 100 or any(value < cutoff for value in parsed_dates):
                    return item, repositories, page, ""
                page += 1

        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            raw_batches = await asyncio.gather(
                *(get_repositories(client, item) for item in self.organizations),
                return_exceptions=True,
            )
            failures: list[str] = []
            page_failures: list[str] = []
            pages_by_organization: dict[str, int] = {}
            recent: list[tuple[dict, dict]] = []
            checked = 0
            for configured, result in zip(self.organizations, raw_batches):
                if isinstance(result, Exception):
                    failures.append(
                        f"{configured['login']}:{type(result).__name__}"
                    )
                    continue
                checked += 1
                item, repositories, pages, page_error = result
                pages_by_organization[str(item["login"])] = int(pages)
                if page_error:
                    page_failures.append(f"{item['login']}:{page_error}")
                for repository in repositories:
                    created = _parse_date(str(repository.get("created_at", "")))
                    if created and created < cutoff:
                        continue
                    if repository.get("fork") or repository.get("archived"):
                        continue
                    if created:
                        recent.append((item, repository))

            async def get_readme(item: dict, repository: dict):
                full_name = str(repository.get("full_name", ""))
                if not full_name:
                    return item, repository, ""
                try:
                    async with semaphore:
                        response = await client.get(
                            f"https://api.github.com/repos/{full_name}/readme",
                            headers={**headers, "Accept": "application/vnd.github.raw+json"},
                        )
                    response.raise_for_status()
                    return item, repository, response.text
                except Exception as exc:
                    logger.info("官方仓库 README 读取失败 %s: %s", full_name, exc)
                    return item, repository, ""

            repository_details = await asyncio.gather(
                *(get_readme(item, repository) for item, repository in recent)
            )

            primary_targets = {
                _linked_primary_material(
                    str(item["owner"]),
                    readme,
                    str(repository.get("homepage", "")),
                )
                for item, repository, readme in repository_details
            }
            primary_targets.discard("")

            async def verify_primary(
                url: str,
            ) -> tuple[str, bool, str, str, str]:
                try:
                    async with semaphore:
                        response = await client.get(
                            url,
                            headers={
                                "User-Agent": "AI-Paradigm-Radar/3.2",
                                "Range": "bytes=0-4095",
                            },
                        )
                    response.raise_for_status()
                    return (
                        url,
                        True,
                        "",
                        _primary_publication_date(url, response.text),
                        response.text[:16_000],
                    )
                except Exception as exc:
                    return url, False, type(exc).__name__, "", ""

            validation = await asyncio.gather(
                *(verify_primary(url) for url in sorted(primary_targets))
            )
            verified_primary_urls = {
                url for url, valid, _, _, _ in validation if valid
            }
            unverified_primary_urls = {
                url: error
                for url, valid, error, _, _ in validation
                if not valid
            }
            primary_publication_dates = {
                url: published
                for url, valid, _, published, _ in validation
                if valid and published
            }
            primary_excerpts = {
                url: excerpt
                for url, valid, _, _, excerpt in validation
                if valid and excerpt
            }

        results: list[TechnicalEvidence] = []
        repository_only: list[str] = []
        repository_origin_rejections: list[str] = []
        unverified_primary: list[str] = []
        origin_count = 0
        native_origin_count = 0
        for configured, repository, readme in repository_details:
            owner_id = str(configured["owner"])
            owner = organization_record(owner_id) or {}
            organization = str(owner.get("name", configured["login"]))
            repository_url = str(repository.get("html_url", ""))
            full_name = str(repository.get("full_name", repository.get("name", "")))
            title = str(repository.get("name", full_name))
            created_at = str(repository.get("created_at", ""))
            metrics = {
                "stars": int(nonnegative_number(repository.get("stargazers_count", 0))),
                "forks": int(nonnegative_number(repository.get("forks_count", 0))),
            }
            support = TechnicalEvidence(
                source=self.source_name,
                evidence_type=EvidenceType.IMPLEMENTATION,
                title=f"{full_name} 官方发布仓库",
                url=repository_url,
                summary=(readme or str(repository.get("description", "")))[:4000],
                published_at=created_at,
                organization=organization,
                metrics=metrics,
                keywords=list(repository.get("topics") or []),
                raw={
                    "relationship": "official_release_repository",
                    "independence": "publisher",
                    "verified_github_owner": str(configured["login"]),
                    "repository_created_at": created_at,
                },
            )
            results.append(support)

            primary_url = _linked_primary_material(
                owner_id,
                readme,
                str(repository.get("homepage", "")),
            )
            if not primary_url:
                repository_only.append(full_name)
                qualifies, reason = _repository_origin_qualification(
                    repository, readme
                )
                if qualifies:
                    results.append(
                        _repository_native_origin(
                            configured=configured,
                            repository=repository,
                            readme=readme,
                            organization=organization,
                            publisher_tier=(
                                "established"
                                if organization_tier(owner_id) == "established"
                                else "verified"
                            ),
                            reason=reason,
                        )
                    )
                    native_origin_count += 1
                else:
                    repository_origin_rejections.append(f"{full_name}:{reason}")
                continue
            if primary_url not in verified_primary_urls:
                unverified_primary.append(
                    f"{full_name}:{unverified_primary_urls.get(primary_url, 'unverified')}"
                )
                qualifies, reason = _repository_origin_qualification(
                    repository, readme
                )
                if qualifies:
                    results.append(
                        _repository_native_origin(
                            configured=configured,
                            repository=repository,
                            readme=readme,
                            organization=organization,
                            publisher_tier=(
                                "established"
                                if organization_tier(owner_id) == "established"
                                else "verified"
                            ),
                            reason=(
                                f"{reason}；外链本轮不可核验，因此仅以仓库内机制为原点"
                            ),
                        )
                    )
                    native_origin_count += 1
                else:
                    repository_origin_rejections.append(f"{full_name}:{reason}")
                continue
            material_summary = "\n".join(
                value
                for value in (
                    readme,
                    primary_excerpts.get(primary_url, ""),
                    str(repository.get("description", "")),
                )
                if value
            )[:16_000]
            classification = classify_publication(
                title=title,
                url=primary_url,
                summary=material_summary,
                metadata=str(repository.get("description", "")),
                official=True,
            )
            arxiv_match = re.search(
                r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?",
                primary_url,
                re.IGNORECASE,
            )
            primary_host = (
                urlparse(primary_url).hostname or ""
            ).casefold().removeprefix("www.")
            is_paper = bool(arxiv_match or primary_host == "doi.org")
            identifiers = {}
            if arxiv_match:
                identifiers["arxiv"] = arxiv_match.group(1)
            results.append(
                TechnicalEvidence(
                    source=self.source_name,
                    evidence_type=(
                        EvidenceType.PRIMARY_PAPER
                        if is_paper
                        else EvidenceType.TECHNICAL_BLOG
                    ),
                    title=title,
                    url=primary_url,
                    summary=material_summary,
                    # 仓库创建时间是发布事件时间，不是它所链接论文/博客的
                    # 发布时间。只有从外部材料本身解析到的日期才能填这里。
                    published_at=primary_publication_dates.get(primary_url, ""),
                    organization=organization,
                    identifiers=identifiers,
                    keywords=list(repository.get("topics") or []),
                    raw={
                        "origin_kind": (
                            "research_paper" if is_paper else classification.origin_kind
                        ),
                        "origin_classification_reason": (
                            "官方新仓库链接到独立可打开的一手论文/技术页；"
                            "GitHub 仓库本身仍只作为支持证据"
                        ),
                        "origin_priority": 3,
                        "publisher_tier": (
                            "established"
                            if organization_tier(owner_id) == "established"
                            else "verified"
                        ),
                        "publisher_evidence": (
                            f"内置核验的官方 GitHub 组织 {configured['login']}，"
                            f"外部一手材料 {primary_url}"
                        ),
                        "canonical_source_url": primary_url,
                        "github_repositories": [repository_url],
                        "release_event_url": repository_url,
                        "release_event_at": created_at,
                        "date_basis": (
                            "external_primary_published_at"
                            if primary_publication_dates.get(primary_url)
                            else "external_primary_date_unknown"
                        ),
                        "source_owner_id": owner_id,
                    },
                )
            )
            origin_count += 1

        self._coverage = {
            "status": "partial" if failures else "completed",
            "configured_organizations": len(self.organizations),
            "checked_organizations": checked,
            "failed_organizations": failures,
            "repository_page_failures": page_failures,
            "repository_pages_by_organization": pages_by_organization,
            "recent_repositories": len(repository_details),
            "linked_primary_origins": origin_count,
            "repository_native_origins": native_origin_count,
            "external_primary_targets": len(primary_targets),
            "external_primary_validation_failures": len(unverified_primary_urls),
            "repository_only_releases": repository_only[:30],
            "repository_origin_rejections": repository_origin_rejections[:30],
            "unverified_primary_releases": unverified_primary[:30],
        }
        return list({item.fingerprint: item for item in results}.values())

    def coverage(self) -> dict[str, object]:
        return dict(self._coverage)


def _repository_origin_qualification(
    repository: dict,
    readme: str,
) -> tuple[bool, str]:
    """Conservatively distinguish a native technical mechanism from repo noise."""

    name = str(repository.get("name", "")).casefold()
    description = str(repository.get("description", ""))
    topics = " ".join(str(value) for value in repository.get("topics", ()) or ())
    body = re.sub(r"<[^>]+>", " ", readme or "")
    body = re.sub(r"\s+", " ", body).strip()
    noise_names = (
        "awesome", "cookbook", "examples", "example", "tutorial", "website",
        "docs", "documentation", "leaderboard", "paper-list", "reading-list",
    )
    if any(token in name for token in noise_names):
        return False, "仓库名称表现为列表、示例、文档或聚合入口"
    noise_phrases = (
        "curated list of", "collection of papers", "paper list",
        "awesome list", "daily papers", "reading list",
    )
    if any(phrase in body[:3000].casefold() for phrase in noise_phrases):
        return False, "README 表明它是聚合/论文列表而非原创机制"
    if len(body) < 800:
        return False, "README 少于 800 字符，无法复核技术 intervention"

    combined = f" {name} {description} {topics} {body[:12_000]} ".casefold()
    ai_anchors = (
        "artificial intelligence", "machine learning", "language model", "llm",
        "neural network", "transformer", "agent", "reasoning", "world model",
        "robot", "multimodal", "diffusion", "reinforcement learning",
        "人工智能", "机器学习", "大模型", "智能体", "推理", "世界模型",
        "机器人", "多模态", "强化学习",
    )
    mechanism_markers = (
        "architecture", "algorithm", "training", "inference", "objective",
        "loss", "memory", "search", "representation", "protocol", "pipeline",
        "optimization", "fine-tun", "pretrain", "benchmark", "evaluation",
        "架构", "算法", "训练", "推理", "目标函数", "损失", "记忆", "搜索",
        "表征", "优化", "评测",
    )
    if not any(term in combined for term in ai_anchors):
        return False, "缺少 AI 技术对象"
    mechanism_hits = sum(term in combined for term in mechanism_markers)
    if mechanism_hits < 2:
        return False, "缺少足够的机制、训练或信息流描述"
    if not re.search(r"(?:^|\s)#{1,4}\s+", readme, re.MULTILINE):
        return False, "README 缺少可审计的技术结构"
    return True, "官方新仓库以结构化 README 首次给出可复核技术机制"


def _repository_native_origin(
    *,
    configured: dict,
    repository: dict,
    readme: str,
    organization: str,
    publisher_tier: str,
    reason: str,
) -> TechnicalEvidence:
    repository_url = str(repository.get("html_url", ""))
    full_name = str(repository.get("full_name", repository.get("name", "")))
    created_at = str(repository.get("created_at", ""))
    return TechnicalEvidence(
        source="official-repository-release",
        evidence_type=EvidenceType.ORIGINAL_IMPLEMENTATION,
        title=str(repository.get("name", full_name)),
        url=repository_url,
        summary=(readme or str(repository.get("description", "")))[:16_000],
        published_at=created_at,
        organization=organization,
        metrics={
            "stars": int(nonnegative_number(repository.get("stargazers_count", 0))),
            "forks": int(nonnegative_number(repository.get("forks_count", 0))),
        },
        identifiers={"github": full_name},
        keywords=list(repository.get("topics") or []),
        raw={
            "origin_kind": "official_open_source_release",
            "origin_priority": 2,
            "origin_classification_reason": reason,
            "publisher_tier": publisher_tier,
            "publisher_evidence": (
                f"内置核验的官方 GitHub 组织 {configured['login']}"
            ),
            "canonical_source_url": repository_url,
            "github_repositories": [repository_url],
            "release_event_url": repository_url,
            "release_event_at": created_at,
            "date_basis": "repository_created_at",
            "source_owner_id": str(configured["owner"]),
            "relationship": "publisher_original_implementation",
            "independence": "publisher",
        },
    )


def _linked_primary_material(owner_id: str, readme: str, homepage: str) -> str:
    allowed_hosts = {"arxiv.org", "doi.org"}
    for source in RESEARCH_SOURCES:
        if source.get("owner") != owner_id:
            continue
        host = (urlparse(str(source.get("url", ""))).hostname or "").casefold()
        if host:
            allowed_hosts.add(host.removeprefix("www."))
        allowed_hosts.update(
            str(value).casefold().removeprefix("www.")
            for value in source.get("allowed_domains", ())
            if str(value).casefold() != "github.com"
        )

    def allowed(value: str) -> bool:
        parsed = urlparse(value)
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        return bool(
            host
            and host != "github.com"
            and any(
                host == allowed or host.endswith(f".{allowed}")
                for allowed in allowed_hosts
            )
        )

    homepage_value = homepage.strip().rstrip(".,);]>\"'")
    homepage_value = homepage_value if allowed(homepage_value) else ""
    labeled: list[tuple[str, str]] = re.findall(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)", readme or ""
    )
    labeled.extend(
        (label, url)
        for url, label in re.findall(
            r'<a[^>]+href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>',
            readme or "",
            re.IGNORECASE | re.DOTALL,
        )
    )
    explicit = [
        value.rstrip(".,);]>\"'")
        for label, value in labeled
        if allowed(value) and looks_like_linked_research_document(label, value)
    ]
    all_urls = [
        value.rstrip(".,);]>\"'")
        for value in re.findall(r"https?://[^\s<>()\[\]]+", readme or "")
        if allowed(value.rstrip(".,);]>\"'"))
    ]

    def dedupe(values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

    explicit = dedupe(explicit)
    all_urls = dedupe(all_urls)
    if explicit:
        # A README may cite many papers. Only an explicitly labelled paper/report
        # link may outrank the repository's declared homepage.
        return sorted(
            explicit,
            key=lambda value: (
                (urlparse(value).hostname or "").casefold().removeprefix("www.")
                not in {"arxiv.org", "doi.org"},
                all_urls.index(value) if value in all_urls else len(all_urls),
            ),
        )[0]
    if homepage_value:
        return homepage_value
    # A single bare external URL is a common compact README convention. Multiple
    # unlabeled links are ambiguous references and must not seed the wrong paper.
    return all_urls[0] if len(all_urls) == 1 else ""


def _primary_publication_date(url: str, body: str) -> str:
    """Read a material date without borrowing the surrounding release event."""

    text = unescape(body or "")[:200_000]
    patterns = (
        r'["\']datePublished["\']\s*[:=]\s*["\']([^"\']+)',
        r'(?:article:published_time|citation_publication_date|citation_date)["\']?\s+(?:content=)?["\']([^"\']+)',
        r'<time[^>]+datetime=["\']([^"\']+)',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        parsed = _parse_date(match.group(1).strip())
        if parsed:
            return parsed.isoformat()

    arxiv = re.search(
        r"arxiv\.org/(?:abs|pdf)/([0-9]{2})([0-9]{2})\.[0-9]{4,5}(?:v\d+)?",
        url,
        re.IGNORECASE,
    )
    if arxiv:
        year = 2000 + int(arxiv.group(1))
        month = int(arxiv.group(2))
        if 1 <= month <= 12:
            # arXiv identifiers expose month but not day. First-of-month is a
            # conservative, stable approximation used only for window checks.
            return datetime(year, month, 1, tzinfo=timezone.utc).isoformat()
    return ""


def _parse_date(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for pattern in ("%Y/%m/%d", "%B %d, %Y", "%b %d, %Y"):
            try:
                parsed = datetime.strptime(value.strip(), pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
