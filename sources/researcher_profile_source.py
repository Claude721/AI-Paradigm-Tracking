"""通过 OpenAlex 与 ORCID 补齐研究者身份、机构、代表作和公开联系方式。"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

import httpx

import config
from paradigms.models import ResearcherProfile, TechnicalEvidence
from paradigms.reputation import resolve_organization

logger = logging.getLogger(__name__)
OPENALEX_AUTHORS = "https://api.openalex.org/authors"
OPENALEX_WORKS = "https://api.openalex.org/works"
ORCID_BASE = "https://pub.orcid.org/v3.0"


class ResearcherProfileClient:
    """只接收可回溯的公开资料；身份无法与当前论文对齐时不强行合并。"""

    def __init__(self, concurrency: int = 3):
        self.concurrency = max(concurrency, 1)
        self._semaphore = asyncio.Semaphore(self.concurrency)

    async def enrich(
        self,
        evidence: TechnicalEvidence,
        existing: list[ResearcherProfile],
        limit: int | None = None,
    ) -> list[ResearcherProfile]:
        limit = limit or config.PARADIGM_RESEARCHER_PROFILE_LIMIT
        profiles = _seed_profiles(evidence, existing, limit)

        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "AI-Paradigm-Radar/3.0"},
        ) as client:
            async def enrich_one(profile: ResearcherProfile) -> None:
                async with self._semaphore:
                    if config.OPENALEX_API_KEY:
                        try:
                            await self._openalex(client, profile, evidence)
                        except Exception as exc:
                            logger.warning("OpenAlex 人物增强失败 [%s]: %s", profile.name, exc)
                            _note(profile, f"OpenAlex 检索失败：{type(exc).__name__}")
                    else:
                        _note(profile, "OpenAlex 未配置，未执行作者身份检索")
                    orcid = profile.identifiers.get("orcid", "")
                    if orcid:
                        try:
                            await self._orcid(client, profile, orcid)
                        except Exception as exc:
                            logger.warning("ORCID 人物增强失败 [%s]: %s", profile.name, exc)
                            _note(profile, f"ORCID 检索失败：{type(exc).__name__}")
                    else:
                        _note(profile, "未获得可核验的 ORCID")
                    await self._homepage_contacts(client, profile)

            await asyncio.gather(*(enrich_one(profile) for profile in profiles))
        return profiles

    async def _openalex(
        self,
        client: httpx.AsyncClient,
        profile: ResearcherProfile,
        evidence: TechnicalEvidence,
    ) -> None:
        _note(profile, "已检索 OpenAlex Authors 并用当前论文题目核验身份")
        known_id = profile.identifiers.get("openalex", "")
        best = None
        if known_id:
            known_short_id = known_id.rstrip("/").rsplit("/", 1)[-1]
            response = await client.get(
                f"{OPENALEX_AUTHORS}/{known_short_id}",
                params={"api_key": config.OPENALEX_API_KEY},
            )
            if response.status_code < 400:
                best = response.json()
                _note(profile, "沿当前论文返回的 OpenAlex 作者 ID 直接核验")
        if best is None:
            response = await client.get(
                OPENALEX_AUTHORS,
                params={
                    "api_key": config.OPENALEX_API_KEY,
                    "search": profile.name,
                    "per-page": 5,
                },
            )
            if response.status_code >= 400:
                _note(profile, f"OpenAlex 作者检索返回 HTTP {response.status_code}")
                return
            matches = [
                item
                for item in response.json().get("results", [])
                if _name_similarity(profile.name, item.get("display_name", "")) >= 0.92
            ]
            best = await self._match_current_work(client, matches, evidence.title)
        if not best:
            _note(profile, "未找到能与当前论文可靠对齐的 OpenAlex 作者实体")
            return

        author_id = str(best.get("id", ""))
        short_id = author_id.rsplit("/", 1)[-1]
        if author_id:
            profile.identifiers["openalex"] = author_id
            profile.profile_urls["openalex"] = author_id
        ids = best.get("ids") or {}
        orcid = str(ids.get("orcid") or "").rsplit("/", 1)[-1]
        if orcid:
            profile.identifiers["orcid"] = orcid
            profile.profile_urls["orcid"] = f"https://orcid.org/{orcid}"
        institutions = best.get("last_known_institutions") or []
        if institutions:
            profile.current_affiliation = (
                institutions[0].get("display_name") or profile.current_affiliation
            )
        topics = [
            topic.get("display_name", "")
            for topic in (best.get("topics") or [])[:3]
            if topic.get("display_name")
        ]
        if topics and not profile.background_summary:
            profile.background_summary = (
                f"OpenAlex 将其近期研究聚合在{'、'.join(topics)}；"
                f"最近可确认机构为{profile.current_affiliation or '未公开'}。"
            )
        if not short_id:
            return
        works_response = await client.get(
            OPENALEX_WORKS,
            params={
                "api_key": config.OPENALEX_API_KEY,
                "filter": f"author.id:{short_id}",
                "sort": "cited_by_count:desc",
                "per-page": 12,
            },
        )
        if works_response.status_code >= 400:
            return
        works = []
        for work in works_response.json().get("results", []):
            title = work.get("display_name", "")
            if not title:
                continue
            works.append(
                {
                    "title": title,
                    "year": work.get("publication_year"),
                    "url": (work.get("primary_location") or {}).get(
                        "landing_page_url"
                    )
                    or work.get("doi")
                    or work.get("id", ""),
                    "venue": ((work.get("primary_location") or {}).get("source") or {}).get(
                        "display_name", ""
                    ),
                    "citations": work.get("cited_by_count", 0) or 0,
                }
            )
        if works:
            profile.representative_works = _merge_works(
                profile.representative_works, works
            )[:8]

    async def _match_current_work(
        self,
        client: httpx.AsyncClient,
        authors: list[dict],
        title: str,
    ) -> dict | None:
        for author in authors[:3]:
            short_id = str(author.get("id", "")).rsplit("/", 1)[-1]
            if not short_id:
                continue
            response = await client.get(
                OPENALEX_WORKS,
                params={
                    "api_key": config.OPENALEX_API_KEY,
                    "filter": f"author.id:{short_id}",
                    "search": title,
                    "per-page": 3,
                },
            )
            if response.status_code >= 400:
                continue
            if any(
                SequenceMatcher(
                    None,
                    title.lower(),
                    str(work.get("display_name", "")).lower(),
                ).ratio()
                >= 0.82
                for work in response.json().get("results", [])
            ):
                return author
        return None

    async def _orcid(
        self,
        client: httpx.AsyncClient,
        profile: ResearcherProfile,
        orcid: str,
    ) -> None:
        _note(profile, "已检索 ORCID 公开记录中的主页、任职与公开邮箱")
        response = await client.get(
            f"{ORCID_BASE}/{orcid}/record",
            headers={"Accept": "application/json"},
        )
        if response.status_code >= 400:
            _note(profile, f"ORCID 公开记录返回 HTTP {response.status_code}")
            return
        payload = response.json()
        person = payload.get("person") or {}
        researcher_urls = ((person.get("researcher-urls") or {}).get("researcher-url") or [])
        for entry in researcher_urls:
            url = str(((entry.get("url") or {}).get("value")) or "").strip()
            if not _public_http_url(url):
                continue
            label = _profile_label(url, str(entry.get("url-name") or ""))
            profile.profile_urls.setdefault(label, url)
        emails = ((person.get("emails") or {}).get("email") or [])
        for entry in emails:
            email = str(entry.get("email") or "").strip()
            if entry.get("visibility") == "PUBLIC" and _valid_email(email):
                profile.public_email = email
                profile.public_email_source = f"https://orcid.org/{orcid}"
                break
        employments = (
            ((payload.get("activities-summary") or {}).get("employments") or {}).get(
                "affiliation-group"
            )
            or []
        )
        for group in employments:
            summaries = group.get("summaries") or []
            if not summaries:
                continue
            organization = (
                ((summaries[0].get("employment-summary") or {}).get("organization") or {}).get(
                    "name", ""
                )
            )
            if organization:
                profile.current_affiliation = organization
                break
        educations = (
            ((payload.get("activities-summary") or {}).get("educations") or {}).get(
                "affiliation-group"
            )
            or []
        )
        for group in educations:
            summaries = group.get("summaries") or []
            if not summaries:
                continue
            organization = (
                ((summaries[0].get("education-summary") or {}).get("organization") or {}).get(
                    "name", ""
                )
            )
            if organization and organization not in profile.prior_affiliations:
                profile.prior_affiliations.append(organization)

    async def _homepage_contacts(
        self,
        client: httpx.AsyncClient,
        profile: ResearcherProfile,
    ) -> None:
        homepage = profile.profile_urls.get("homepage", "")
        if not _safe_public_url(homepage):
            if homepage:
                _note(profile, "个人主页地址未通过公共 URL 安全检查，未抓取")
            else:
                _note(profile, "公开学术资料未提供可核验个人主页")
            return
        _note(profile, "已检查公开个人主页中的邮箱、LinkedIn 与 GitHub 链接")
        try:
            response = await client.get(homepage)
            if response.status_code >= 400:
                _note(profile, f"个人主页返回 HTTP {response.status_code}")
                return
            content = html.unescape(response.text[:500_000])
        except Exception as exc:
            _note(profile, f"个人主页读取失败：{type(exc).__name__}")
            return
        mailto = re.search(
            r"href\s*=\s*['\"]mailto:([^?'\"#\s]+)", content, re.IGNORECASE
        )
        if mailto and _valid_email(mailto.group(1)):
            profile.public_email = mailto.group(1)
            profile.public_email_source = homepage
        for label, pattern in {
            "linkedin": r"https?://(?:[a-z]+\.)?linkedin\.com/[^'\"<>\s]+",
            "github": r"https?://github\.com/[^'\"<>\s]+",
            "google_scholar": r"https?://scholar\.google\.[^'\"<>\s]+",
        }.items():
            match = re.search(pattern, content, re.IGNORECASE)
            if match and _safe_public_url(match.group(0)):
                profile.profile_urls.setdefault(label, match.group(0).rstrip(".,);"))
        description = re.search(
            r"<meta[^>]+(?:name|property)\s*=\s*['\"](?:description|og:description)['\"][^>]+content\s*=\s*['\"]([^'\"]+)",
            content,
            re.IGNORECASE,
        ) or re.search(
            r"<meta[^>]+content\s*=\s*['\"]([^'\"]+)['\"][^>]+(?:name|property)\s*=\s*['\"](?:description|og:description)['\"]",
            content,
            re.IGNORECASE,
        )
        if description:
            bio = re.sub(r"\s+", " ", description.group(1)).strip()[:800]
            if len(bio) >= 40:
                profile.public_bio_excerpt = bio
                _note(profile, "已读取个人主页公开简介，用于核验教育与研究背景")


def _seed_profiles(
    evidence: TechnicalEvidence,
    existing: list[ResearcherProfile],
    limit: int,
) -> list[ResearcherProfile]:
    by_name = {profile.name.casefold(): profile for profile in existing}
    roles = {
        str(name): str(role)
        for name, role in (evidence.raw.get("author_roles") or {}).items()
        if name and role
    }
    profile_urls = {
        str(name): str(url)
        for name, url in (evidence.raw.get("author_profile_urls") or {}).items()
        if name and url
    }
    public_emails = {
        str(name): str(email)
        for name, email in (evidence.raw.get("author_public_emails") or {}).items()
        if name and _valid_email(str(email))
    }
    openalex_map = {
        str(name): str(identifier)
        for name, identifier in (evidence.raw.get("author_openalex_map") or {}).items()
        if name and identifier
    }
    affiliation_map = {
        str(name): [str(value) for value in values if value]
        for name, values in (evidence.raw.get("author_affiliations") or {}).items()
        if name and isinstance(values, list)
    }
    individual_authors = [
        name
        for name in evidence.authors
        if name and not _is_collective_author(name)
    ]
    has_collective_signature = len(individual_authors) != len(evidence.authors)
    selected: list[str] = []
    # 研究负责人选择是分层配额，不是论文作者列表的机械截断：
    # 先保留最可能主导方法的前三位，再保留末位/资深作者和名单中的长期
    # 前沿研究者，最后补入项目页明确标注的其他贡献角色。
    selected.extend(individual_authors[:3])
    if (
        len(individual_authors) > 1
        and not has_collective_signature
        and len(individual_authors) <= 50
    ):
        selected.append(individual_authors[-1])
    priority_names = {
        re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())
        for value in config.PRIORITY_RESEARCHERS
    }
    selected.extend(
        name
        for name in individual_authors
        if re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", name.casefold())
        in priority_names
    )
    selected.extend(name for name in roles if not _is_collective_author(name))
    selected = list(dict.fromkeys(name for name in selected if name))[:limit]

    for name in selected:
        if not name:
            continue
        index = evidence.authors.index(name) if name in evidence.authors else -1
        individual_index = (
            individual_authors.index(name) if name in individual_authors else -1
        )
        role = roles.get(name, "")
        if not role:
            if individual_index == 0 and index == 0:
                role = "第一作者"
            elif individual_index == 0:
                role = "第一位具名作者/贡献角色待核验"
            elif (
                individual_index == len(individual_authors) - 1
                and individual_index > 0
            ):
                role = "末位作者/资深作者线索"
            elif 0 < individual_index < 3:
                role = "前列作者/共同一作待核验"
            else:
                role = "共同作者"
        profile = by_name.setdefault(
            name.casefold(),
            ResearcherProfile(
                name=name,
                role=role,
                current_affiliation=evidence.organization,
            ),
        )
        if role and not profile.role:
            profile.role = role
        openalex_id = next(
            (
                identifier
                for author_name, identifier in openalex_map.items()
                if _name_similarity(name, author_name) >= 0.92
            ),
            "",
        )
        if openalex_id:
            profile.identifiers.setdefault("openalex", openalex_id)
            profile.profile_urls.setdefault("openalex", openalex_id)
        affiliations = next(
            (
                values
                for author_name, values in affiliation_map.items()
                if _name_similarity(name, author_name) >= 0.92
            ),
            [],
        )
        if affiliations:
            profile.current_affiliation = affiliations[0]
            for affiliation in affiliations[1:]:
                if affiliation not in profile.prior_affiliations:
                    profile.prior_affiliations.append(affiliation)
        homepage = next(
            (
                url
                for author_name, url in profile_urls.items()
                if _name_similarity(name, author_name) >= 0.92
                and _safe_public_url(url)
            ),
            "",
        )
        if homepage:
            label = _profile_label(homepage, "homepage")
            profile.profile_urls.setdefault(label, homepage)
            if label == "homepage":
                profile.profile_urls.setdefault("homepage", homepage)
            if "orcid.org" in urlparse(homepage).netloc.casefold():
                orcid = homepage.rstrip("/").rsplit("/", 1)[-1]
                if re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", orcid):
                    profile.identifiers.setdefault("orcid", orcid)
                    profile.profile_urls.setdefault("orcid", homepage)
            _note(profile, "已从论文官方 HTML 的作者链接获得公开主页")
        public_email = next(
            (
                email
                for author_name, email in public_emails.items()
                if _name_similarity(name, author_name) >= 0.92
            ),
            "",
        )
        if public_email:
            profile.public_email = public_email
            profile.public_email_source = str(
                (evidence.raw.get("project_urls") or [evidence.url])[0]
            )
            _note(profile, "已从论文官方项目页获得公开职业邮箱")
        _note(profile, "已从当前论文作者列表建立身份种子")
    return list(by_name.values())[:limit]


def _is_collective_author(name: str) -> bool:
    """团队/联盟署名用于机构归属，不能伪装成需要找联系方式的个人。"""
    normalized = re.sub(r"\s+", " ", name).strip().casefold()
    if resolve_organization(name) is not None:
        return True
    return bool(
        re.search(
            r"(?:\bteam\b|\bconsortium\b|\bcollaboration\b|研究团队|课题组)$",
            normalized,
        )
    )


def _note(profile: ResearcherProfile, value: str) -> None:
    if value and value not in profile.contact_search_notes:
        profile.contact_search_notes.append(value)


def _name_similarity(left: str, right: str) -> float:
    normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.casefold())
    return SequenceMatcher(None, normalize(left), normalize(right)).ratio()


def _public_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _safe_public_url(value: str) -> bool:
    if not _public_http_url(value):
        return False
    hostname = (urlparse(value).hostname or "").casefold()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return address.is_global


def _profile_label(url: str, supplied: str) -> str:
    host = urlparse(url).netloc.casefold()
    if "linkedin.com" in host:
        return "linkedin"
    if "github.com" in host:
        return "github"
    if "scholar.google" in host:
        return "google_scholar"
    label = re.sub(r"[^a-z0-9_]+", "_", supplied.casefold()).strip("_")
    return label or "homepage"


def _valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))


def _merge_works(existing: list[dict], new: list[dict]) -> list[dict]:
    by_title = {
        str(work.get("title", "")).casefold(): work
        for work in [*existing, *new]
        if work.get("title")
    }
    return sorted(
        by_title.values(),
        key=lambda work: (work.get("citations", 0) or 0, work.get("year", 0) or 0),
        reverse=True,
    )
