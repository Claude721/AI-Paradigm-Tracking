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
        limit: int = 2,
    ) -> list[ResearcherProfile]:
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


def _seed_profiles(
    evidence: TechnicalEvidence,
    existing: list[ResearcherProfile],
    limit: int,
) -> list[ResearcherProfile]:
    by_name = {profile.name.casefold(): profile for profile in existing}
    for index, name in enumerate(evidence.authors[:limit]):
        if not name:
            continue
        profile = by_name.setdefault(
            name.casefold(),
            ResearcherProfile(
                name=name,
                role="第一作者" if index == 0 else "共同作者",
                current_affiliation=evidence.organization,
            ),
        )
        _note(profile, "已从当前论文作者列表建立身份种子")
    return list(by_name.values())[:limit]


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
