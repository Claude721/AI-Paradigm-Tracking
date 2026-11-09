"""通过 OpenAlex 与 ORCID 补齐研究者身份、机构、代表作和公开联系方式。"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx

import config
from paradigms.models import (
    EvidenceType,
    ResearcherProfile,
    TechnicalEvidence,
    plausible_researcher_name,
)
from paradigms.researcher_identity import (
    merge_researcher_profiles,
    same_verified_researcher,
    bind_current_work, normalized_person_name, person_name_similarity, record_identity_observation, verify_contact,
)
from sources.professional_identity import (profile_label, public_url, validate_public_host,
                                           page_status, parse_person_page, RULE_VERSION, IdentityProbeError)
from runtime_clock import observation_now
from paradigms.reputation import resolve_organization
from paradigms.async_utils import gather_scoped
from paradigms.authorship import source_authorship

logger = logging.getLogger(__name__)
OPENALEX_AUTHORS = "https://api.openalex.org/authors"
OPENALEX_WORKS = "https://api.openalex.org/works"
ORCID_BASE = "https://pub.orcid.org/v3.0"


class ResearcherProfileClient:
    """只接收可回溯的公开资料；身份无法与当前论文对齐时不强行合并。"""

    def __init__(self, concurrency: int = 3, *, openalex_request_limit: int | None = None,
                 client_factory=None, host_validator=None):
        self.concurrency = max(concurrency, 1)
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self.openalex_request_limit = openalex_request_limit
        self.openalex_request_count = 0
        self.client_factory = client_factory or httpx.AsyncClient
        self.host_validator = host_validator or validate_public_host
        self._page_cache, self._page_locks = {}, {}

    async def enrich(
        self,
        evidence: TechnicalEvidence,
        existing: list[ResearcherProfile],
        limit: int | None = None,
    ) -> list[ResearcherProfile]:
        limit = limit or min(
            config.PARADIGM_RESEARCHER_PROFILE_LIMIT,
            config.PARADIGM_KEY_RESEARCHER_LIMIT,
        )
        active_count = len(_seed_profiles(evidence, [], limit))
        profiles = _seed_profiles(evidence, existing, limit)

        async with self.client_factory(
            timeout=30,
            follow_redirects=False,
            headers={"User-Agent": "AI-Paradigm-Radar/3.0"},
        ) as client:
            async def enrich_one(profile: ResearcherProfile) -> None:
                async with self._semaphore:
                    # Explicit work-supplied pages get the first chance, before
                    # a slow optional academic directory consumes the phase.
                    initial_homepage = profile.profile_urls.get("homepage", "")
                    await self._homepage_contacts(client, profile)
                    profile.identity_search_status = "not_configured"
                    if config.OPENALEX_API_KEY:
                        try:
                            await self._openalex(client, profile, evidence)
                            matched = any("当前论文题目与 OpenAlex 作者实体交叉核验通过" in note for note in profile.contact_search_notes)
                            profile.identity_search_status = "completed" if matched else "not_found"
                            record_identity_observation(profile, "openalex", profile.identity_search_status,
                                                        url=OPENALEX_AUTHORS, reason="CurrentWorkAligned" if matched else "NoUniqueCurrentWorkMatch")
                        except Exception as exc:
                            profile.identity_search_status = "failed"
                            record_identity_observation(profile, "openalex", "failed", url=OPENALEX_AUTHORS, reason=type(exc).__name__)
                            logger.warning("OpenAlex 人物增强失败 [%s]: %s", profile.name, type(exc).__name__)
                            _note(profile, f"OpenAlex 检索失败：{type(exc).__name__}")
                    else:
                        record_identity_observation(profile, "openalex", "not_configured", url=OPENALEX_AUTHORS)
                        _note(profile, "OpenAlex 未配置，未执行作者身份检索")
                    orcid = profile.identifiers.get("orcid", "")
                    if orcid:
                        try:
                            await self._orcid(client, profile, orcid)
                            record_identity_observation(profile, "orcid", "completed", url=f"https://orcid.org/{orcid}")
                        except Exception as exc:
                            record_identity_observation(profile, "orcid", "failed", url=f"https://orcid.org/{orcid}", reason=type(exc).__name__)
                            logger.warning("ORCID 人物增强失败 [%s]: %s", profile.name, type(exc).__name__)
                            _note(profile, f"ORCID 检索失败：{type(exc).__name__}")
                    else:
                        _note(profile, "未获得可核验的 ORCID")
                    if profile.profile_urls.get("homepage", "") != initial_homepage:
                        await self._homepage_contacts(client, profile)
                    outcomes = [row["status"] for row in profile.identity_observations]
                    faults = any(value in {"failed", "blocked", "invalid", "unverified"} for value in outcomes)
                    if profile.public_contacts:
                        profile.identity_search_status = "partial" if faults else "completed"
                        profile.contact_search_status = "partial" if faults else "completed"
                    elif faults:
                        profile.contact_search_status = "failed"
                    elif "completed" in outcomes or "not_found" in outcomes:
                        profile.contact_search_status = "not_found"
                    else:
                        profile.contact_search_status = "not_configured"

            async def guarded_enrich(profile):
                try:
                    await enrich_one(profile)
                except asyncio.CancelledError:
                    record_identity_observation(profile, "identity_runtime", "not_executed", reason="PersonVisitInterrupted")
                    raise
                except Exception as exc:
                    # An adapter/programming fault belongs to this person;
                    # gather_scoped must not cancel a healthy peer's lookup.
                    status = "partial" if profile.public_contacts else "failed"
                    profile.identity_search_status = profile.contact_search_status = status
                    record_identity_observation(profile, "identity_runtime", "failed", reason=type(exc).__name__)
                    _note(profile, "人物补充异常，其他人物独立继续：" + type(exc).__name__)

            # A same-name historical profile without a shared identifier is
            # kept for the route, but must not be re-identified against this
            # different paper or inherit its contact evidence.
            await gather_scoped(
                *(guarded_enrich(profile) for profile in profiles[:active_count])
            )
        active = profiles[:active_count]
        historical = profiles[active_count:]
        resolved = []
        for profile in active:
            if _current_work_identity_confirmed(profile):
                matches = [
                    item for item in historical
                    if same_verified_researcher(profile, item)
                ]
                if matches:
                    profile = merge_researcher_profiles([profile], matches)[0]
                    historical = [
                        item for item in historical
                        if item not in matches
                    ]
            resolved.append(profile)
        return [*resolved, *historical]

    async def _openalex(
        self,
        client: httpx.AsyncClient,
        profile: ResearcherProfile,
        evidence: TechnicalEvidence,
    ) -> None:
        _note(profile, "已检索 OpenAlex Authors，并尝试用当前论文题目对齐身份")
        known_id = profile.identifiers.get("openalex", "")
        best = None
        if known_id:
            known_short_id = known_id.rstrip("/").rsplit("/", 1)[-1]
            if not re.fullmatch(r"A\d+", known_short_id):
                raise ValueError("InvalidOpenAlexAuthorID")
            response = await self._openalex_get(client,
                f"{OPENALEX_AUTHORS}/{known_short_id}",
                params={},
            )
            if response.status_code < 400:
                known_author = response.json()
                if (_openalex_name_matches(profile.name, known_author)
                    and str(known_author.get("id", "")).rstrip("/").rsplit("/", 1)[-1] == known_short_id
                    and await self._has_current_work(
                    client,
                    known_author,
                    evidence.title,
                )):
                    best = known_author
                    _note(profile, "当前论文题目与 OpenAlex 作者实体交叉核验通过")
                else:
                    _note(
                        profile,
                        "当前论文返回的 OpenAlex 作者 ID 未能与当前论文题目对齐",
                    )
        if best is None:
            response = await self._openalex_get(client,
                OPENALEX_AUTHORS,
                params={
                    "search": profile.name,
                    "per-page": 5,
                },
            )
            if response.status_code >= 400:
                _note(profile, f"OpenAlex 作者检索返回 HTTP {response.status_code}")
                response.raise_for_status()
            matches = [
                item
                for item in _openalex_results(response)
                if _openalex_name_matches(profile.name, item)
            ]
            best = await self._match_current_work(client, matches, evidence.title)
            if best is not None:
                _note(profile, "当前论文题目与 OpenAlex 作者实体交叉核验通过")
        if not best:
            _note(profile, "未找到能与当前论文可靠对齐的 OpenAlex 作者实体")
            return

        author_id = str(best.get("id", ""))
        if not re.fullmatch(r"https://openalex\.org/A\d+", author_id):
            raise ValueError("InvalidOpenAlexAuthorIdentity")
        short_id = author_id.rsplit("/", 1)[-1]
        if author_id:
            if not verify_contact(profile, "openalex", author_id, evidence.url, "academic_identity"):
                return
            profile.identifiers["openalex"] = author_id
        ids = best.get("ids") or {}
        orcid = str(ids.get("orcid") or "").rsplit("/", 1)[-1]
        if orcid and not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", orcid):
            raise ValueError("InvalidORCIDIdentifier")
        if orcid:
            verify_contact(profile, "orcid", f"https://orcid.org/{orcid}", author_id, "academic_identity")
        institutions = best.get("last_known_institutions") or []
        if institutions:
            for institution in institutions:
                row = {"organization": institution.get("display_name", ""), "source_url": author_id,
                       "kind": "last_known_institution", "observed_at": observation_now().isoformat()}
                if row not in profile.affiliation_observations:
                    profile.affiliation_observations.append(row)
        topics = [
            topic.get("display_name", "")
            for topic in (best.get("topics") or [])[:3]
            if topic.get("display_name")
        ]
        if topics and not profile.background_summary:
            profile.background_summary = (
                f"OpenAlex 将其近期研究聚合在{'、'.join(topics)}；"
                "图谱最近机构仅作历史线索，不能据此确认当前任职。"
            )
        if not short_id:
            return
        works_response = await self._openalex_get(client,
            OPENALEX_WORKS,
            params={
                "filter": f"author.id:{short_id}",
                "sort": "cited_by_count:desc",
                "per-page": 12,
            },
        )
        if works_response.status_code >= 400:
            return
        works = []
        for work in _openalex_results(works_response):
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
        if len(authors) > 3:
            return None  # The unexamined tail cannot establish uniqueness.
        matches = []
        for author in authors[:3]:
            if await self._has_current_work(client, author, title):
                matches.append(author)
        return matches[0] if len(matches) == 1 else None

    async def _has_current_work(
        self,
        client: httpx.AsyncClient,
        author: dict,
        title: str,
    ) -> bool:
        short_id = str(author.get("id", "")).rsplit("/", 1)[-1]
        if not short_id or not title.strip():
            return False
        response = await self._openalex_get(client,
            OPENALEX_WORKS,
            params={
                "filter": f"author.id:{short_id}",
                "search": title,
                "per-page": 3,
            },
        )
        if response.status_code >= 400:
            response.raise_for_status()
        return any(normalized_person_name(title) == normalized_person_name(work.get("display_name", ""))
                   for work in _openalex_results(response))

    async def _openalex_get(self, client, url, *, params):
        # Never put a production Secret in URLs, exception text or redirected
        # requests. ORCID/homepage requests do not inherit this header.
        if (self.openalex_request_limit is not None
            and self.openalex_request_count >= self.openalex_request_limit):
            raise RuntimeError("OpenAlex 有界身份查询上限已到；不继续发请求")
        self.openalex_request_count += 1  # Before await: shared by concurrent people.
        return await client.get(url, params=params,
            headers={"Authorization": f"Bearer {config.OPENALEX_API_KEY}"}, follow_redirects=False)

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
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("person"), dict):
            raise ValueError("InvalidORCIDRecord")
        returned_id = ((payload.get("orcid-identifier") or {}).get("path") or "")
        if returned_id and returned_id != orcid:
            raise ValueError("MismatchedORCIDRecord")
        person = payload.get("person") or {}
        researcher_urls = ((person.get("researcher-urls") or {}).get("researcher-url") or [])
        for entry in researcher_urls:
            url = str(((entry.get("url") or {}).get("value")) or "").strip()
            if not _public_http_url(url):
                continue
            label = _profile_label(url, str(entry.get("url-name") or ""))
            if label:
                verify_contact(profile, label, url, f"https://orcid.org/{orcid}", "academic_identity")
        emails = ((person.get("emails") or {}).get("email") or [])
        for entry in emails:
            email = str(entry.get("email") or "").strip()
            if entry.get("visibility") == "PUBLIC" and _valid_email(email):
                verify_contact(profile, "email", email, f"https://orcid.org/{orcid}", "academic_identity")
                break
        employments = (
            ((payload.get("activities-summary") or {}).get("employments") or {}).get(
                "affiliation-group"
            )
            or []
        )
        ongoing, dated_current = set(), set()
        for group in employments:
            summaries = group.get("summaries") or []
            if not summaries:
                continue
            for entry in summaries:
                employment = entry.get("employment-summary") or {}
                organization = (employment.get("organization") or {}).get("name", "")
                if not organization:
                    continue
                row = {"organization": organization, "source_url": f"https://orcid.org/{orcid}",
                       "kind": "employment", "start_date": employment.get("start-date"),
                       "end_date": employment.get("end-date"), "observed_at": observation_now().isoformat()}
                if row not in profile.affiliation_observations:
                    profile.affiliation_observations.append(row)
                if employment.get("end-date"):
                    if organization not in profile.prior_affiliations:
                        profile.prior_affiliations.append(organization)
                else:
                    ongoing.add(organization)
                    if _employment_current(employment):
                        dated_current.add(organization)
        # Decide after reading every entry: a later undated job can also make
        # the apparent current employer ambiguous. Ordering is not evidence.
        profile.current_affiliation = next(iter(dated_current)) if len(ongoing) == len(dated_current) == 1 else ""
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
            record_identity_observation(profile, "homepage", "not_executed", reason="NoPublicPersonPage")
            return
        _note(profile, "已检查公开个人主页中的邮箱、LinkedIn 与 GitHub 链接")
        queue, seen = [(homepage, homepage, 0)], set()
        while queue and len(seen) < 3:
            target, parent, depth = queue.pop(0)
            if target in seen:
                continue
            seen.add(target)
            record_identity_observation(profile, "homepage", "not_executed", url=target, reason="ProbeScheduled", parent_url=parent)
            try:
                page = await self._person_page(client, target)
                status, reason = page["status"], page["reason"]
                inherited = (depth > 0 and urlparse(target).netloc == urlparse(parent).netloc
                             and urlparse(target).path.startswith(urlparse(parent).path.rstrip("/") + "/"))
                parsed = parse_person_page(page.get("text", ""), page["url"], profile.name,
                                           inherited_owner=inherited and page["url"] == target) if status == "completed" else {}
                if status == "completed" and not parsed.get("owned"):
                    status, reason = "unverified", "NoUniquePersonSection"
                record_identity_observation(profile, "homepage", status, url=target, reason=reason,
                                            parent_url=parent, adapter_version=RULE_VERSION,
                                            http_status=page["http_status"], final_url=page["url"],
                                            body_sha256=parsed.get("body_sha256", ""))
                profile.contact_search_status = "completed" if status == "completed" else status
                if status != "completed":
                    continue
                if parsed.get("bio"):
                    profile.public_bio_excerpt = parsed["bio"]
                    _note(profile, "已读取个人主页公开职业简介段落，用于核验教育与研究背景")
                for label, url in parsed["links"]:
                    verify_contact(profile, label, url, page["url"], "person_page_link")
                # A directory can contain other people's mailboxes. Only a
                # unique email on a person-owned page is attributable.
                if len(parsed["emails"]) == 1:
                    verify_contact(profile, "email", parsed["emails"][0], page["url"], "person_page_email")
                for url in parsed["leads"]:
                    lead = {"url": url, "source_url": page["url"], "status": "unverified", "reason": "NotAPersonContactLink"}
                    if lead not in profile.profile_leads:
                        profile.profile_leads.append(lead)
                if depth < 1:
                    queue.extend((url, page["url"], depth + 1) for url in parsed["follow"][:2])
            except asyncio.CancelledError:
                record_identity_observation(profile, "homepage", "failed", url=target, reason="ProbeCancelled")
                raise
            except (OSError, ValueError, httpx.HTTPError) as exc:
                record_identity_observation(profile, "homepage", "failed", url=target,
                                            reason=exc.reason if isinstance(exc, IdentityProbeError) else type(exc).__name__)
                profile.contact_search_status = "failed"
                _note(profile, f"个人主页读取失败：{type(exc).__name__}")

    async def _person_page(self, client, url):
        # One process cache, keyed by exact public URL and parser version.
        key = (RULE_VERSION, url)
        async with self._page_locks.setdefault(key, asyncio.Lock()):
            if key in self._page_cache:
                value = self._page_cache[key]
                if isinstance(value, Exception):
                    if isinstance(value, IdentityProbeError):
                        raise IdentityProbeError(value.reason)
                    raise IdentityProbeError("PreviousIdentityPageFailure")
                return copy.deepcopy(value)
            current = url
            try:
                for _ in range(3):
                    await self.host_validator(current)
                    async with client.stream("GET", current) as stream:
                        chunks, size = [], 0
                        async for chunk in stream.aiter_bytes():
                            size += len(chunk)
                            if size > 1_000_000:
                                raise IdentityProbeError("IdentityPageSizeLimit")
                            chunks.append(chunk)
                        response = httpx.Response(stream.status_code, content=b"".join(chunks),
                                                  headers={k: v for k, v in stream.headers.items()
                                                           if k.lower() not in {"content-encoding", "content-length"}},
                                                  request=stream.request)
                    if response.is_redirect:
                        next_url = urljoin(current, response.headers.get("location", ""))
                        if urlparse(current).scheme == "https" and urlparse(next_url).scheme != "https":
                            raise IdentityProbeError("IdentityRedirectDowngrade")
                        current = next_url
                        continue
                    if len(response.content) > 1_000_000:
                        raise IdentityProbeError("IdentityPageSizeLimit")
                    status, reason = page_status(response)
                    value = {"status": status, "reason": reason, "url": current,
                             "http_status": response.status_code, "text": response.text if status == "completed" else ""}
                    self._page_cache[key] = value
                    return copy.deepcopy(value)
                raise IdentityProbeError("IdentityRedirectLimit")
            except Exception as exc:
                self._page_cache[key] = IdentityProbeError(exc.reason) if isinstance(exc, IdentityProbeError) else ValueError(type(exc).__name__)
                raise


def _openalex_results(response):
    payload = response.json()
    if (not isinstance(payload, dict) or payload.get("error")
        or not isinstance(payload.get("results"), list)
        or any(not isinstance(item, dict) for item in payload["results"])):
        raise ValueError("OpenAlex results contract is invalid; not a completed no-match search")
    return payload["results"]


def _openalex_name_matches(name, author):
    if not isinstance(author, dict):
        return False
    alternatives = author.get("display_name_alternatives") or []
    alternatives = alternatives if isinstance(alternatives, list) else []
    expected = normalized_person_name(name)
    return bool(expected and any(isinstance(value, str) and normalized_person_name(value) == expected
                                for value in [author.get("display_name", ""), *alternatives[:20]]))


def _seed_profiles(
    evidence: TechnicalEvidence,
    existing: list[ResearcherProfile],
    limit: int,
) -> list[ResearcherProfile]:
    # Current-work author metadata must not silently inherit a same-name
    # historical person's verified contact or identifier. Build fresh seeds,
    # then reconcile only on a shared public identity claim.
    by_name: dict[str, ResearcherProfile] = {}
    authorship = source_authorship(evidence)
    roles = {fact.name: fact.role for fact in authorship}
    profile_urls = {
        str(name): str(url)
        for name, url in (evidence.raw.get("author_profile_urls") or {}).items()
        if name and url
    }
    curated_public_profiles = {
        str(name): {
            str(label): str(url)
            for label, url in values.items()
            if label and url
        }
        for name, values in (
            evidence.raw.get("author_public_profiles") or {}
        ).items()
        if name and isinstance(values, dict)
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
    affiliation_map = {fact.name: list(fact.affiliations) for fact in authorship}
    individual_authors = [fact.name for fact in authorship]
    has_collective_signature = len(individual_authors) != len(evidence.authors)
    selected: list[str] = []
    # 研究负责人选择是分层配额，不是论文作者列表的机械截断：第一位具名
    # 作者、明确通讯/负责人、重点研究者和末位资深作者各占一层；其余共同
    # 一作只在仍有配额时补入。完整作者名单仍保留在 evidence.authors。
    selected.extend(individual_authors[:1])
    selected.extend(
        name
        for name, role in roles.items()
        if any(
            marker in role.casefold()
            for marker in (
                "通讯",
                "corresponding",
                "lead",
                "负责人",
                "资深",
            )
        )
    )
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
    if (
        len(individual_authors) > 1
        and not has_collective_signature
        and len(individual_authors) <= 50
    ):
        selected.append(individual_authors[-1])
    selected.extend(
        name
        for name, role in roles.items()
        if any(
            marker in role.casefold()
            for marker in ("第一作者", "共同第一", "共同一作")
        )
    )
    selected.extend(individual_authors[1:3])
    # Source facts include ordinary coauthors too. Do not turn their presence
    # into a new lookup quota or select a collective report's last author just
    # because every byline now has a deterministic display role.
    selected = list(
        dict.fromkeys(name for name in selected if plausible_researcher_name(name))
    )[:limit]

    for name in selected:
        if not name:
            continue
        index = evidence.authors.index(name) if name in evidence.authors else -1
        individual_index = (
            individual_authors.index(name) if name in individual_authors else -1
        )
        role = roles.get(name, "")
        if not role:
            if evidence.evidence_type != EvidenceType.PRIMARY_PAPER:
                role = "公开署名作者/贡献角色未披露"
            elif individual_index == 0 and index == 0:
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
        normalized_name = re.sub(
            r"[^a-z0-9\u4e00-\u9fff]", "", name.casefold()
        )
        if normalized_name in priority_names and "重点研究者" not in role:
            role = f"{role}/重点研究者"
        profile = by_name.setdefault(
            name.casefold(),
            ResearcherProfile(
                name=name,
                role=role,
                # Paper affiliation and publisher are not current employment.
                # Only the optional identity lookup may establish that field.
                current_affiliation="",
            ),
        )
        bind_current_work(profile, evidence)
        if role and not profile.role:
            profile.role = role
        curated = _author_map_value(name, curated_public_profiles) or {}
        if curated:
            for label, url in curated.items():
                kind = _profile_label(url, label)
                if kind:
                    verify_contact(profile, kind, url, evidence.url, "curated_author")
            note = "已检查公开个人主页与手工核验的 KOL 目录，并记录职业联系入口"
            if note not in profile.contact_search_notes:
                profile.contact_search_notes.append(note)
        openalex_id = _author_map_value(name, openalex_map) or ""
        if openalex_id:
            profile.identifiers.setdefault("openalex", openalex_id)
            profile.profile_urls.setdefault("openalex", openalex_id)
        affiliations = _author_map_value(name, affiliation_map) or []
        if affiliations:
            _note(profile, "本工作署名机构（不代表当前任职）：" + "；".join(affiliations))
        homepage = _author_map_value(name, profile_urls) or ""
        if homepage:
            label = _profile_label(homepage, "homepage")
            if label:
                verify_contact(profile, label, homepage, evidence.url, "work_author_link")
            if (urlparse(homepage).hostname or "").casefold() == "orcid.org":
                orcid = homepage.rstrip("/").rsplit("/", 1)[-1]
                if re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", orcid):
                    profile.identifiers.setdefault("orcid", orcid)
                    profile.profile_urls.setdefault("orcid", homepage)
            if label:
                _note(profile, "已从论文官方 HTML 的作者链接获得公开主页")
        public_email = _author_map_value(name, public_emails) or ""
        if public_email:
            verify_contact(profile, "email", public_email, evidence.url, "work_author_link")
            _note(profile, "已从论文官方项目页获得公开职业邮箱")
        _note(profile, "已从当前论文作者列表建立身份种子" if evidence.evidence_type == EvidenceType.PRIMARY_PAPER
              else "已从当前原创材料的公开署名建立身份种子；发布方不等于任职机构")
    current = list(by_name.values())
    historical = [
        profile for profile in existing
        if plausible_researcher_name(profile.name)
    ]
    # Do not join a new author's unconfirmed OpenAlex mapping to a previous
    # person's contacts before the current-paper work check has run.
    profiles = [*current, *historical]
    selected_names = {name.casefold() for name in selected}
    relevant = [
        profile for profile in profiles
        if profile.name.casefold() in selected_names
    ]
    other = [
        profile for profile in profiles
        if profile.name.casefold() not in selected_names
    ]
    # ``limit`` bounds current-paper lookups, not the durable route dossier.
    # Historical people may be distinct or may be the route's earlier lead;
    # silently truncating them would erase verified contacts across weeks.
    return [*relevant, *other]


def _current_work_identity_confirmed(profile: ResearcherProfile) -> bool:
    return any(
        "当前论文题目与 OpenAlex 作者实体交叉核验通过" in note
        or "已从论文官方 HTML 的作者链接获得公开主页" in note
        for note in profile.contact_search_notes
    )


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
    return person_name_similarity(left, right)


def _author_map_value(name, mapping):
    matches = [value for key, value in mapping.items() if _name_similarity(name, key) >= 0.92]
    return matches[0] if len(matches) == 1 else None


def _employment_current(employment):
    """A dated, recently updated open employment claim; not a guessed job."""
    try:
        start = int(((employment.get("start-date") or {}).get("year") or {}).get("value", ""))
        modified = int((employment.get("last-modified-date") or {}).get("value", 0)) / 1000
        now = observation_now()
        return 1900 <= start <= now.year and 0 <= now.timestamp() - modified <= 365 * 86400
    except (TypeError, ValueError, OverflowError):
        return False


def _public_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _safe_public_url(value: str) -> bool:
    return public_url(value)


def _profile_label(url: str, supplied: str) -> str:
    return profile_label(url, supplied)


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
