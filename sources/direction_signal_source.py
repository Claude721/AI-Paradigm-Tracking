"""Free advisory context: media capital reports, hiring intent, arXiv cohorts.

No paid search, model, account, browser or application submission. Each source
is optional. These records never enter TechnicalEvidence, Rubric or freshness.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs

import httpx

from paradigms.models import ORIGIN_EVIDENCE_TYPES, _evidence_datetime, _canonical_material_url
from paradigms.evidence_association import countable_evidence, support_revision
from paradigms.landscape import load_landscape
from runtime_clock import research_now, observation_now, research_days
from sources.arxiv_source import ArxivSource, ARXIV_API, ARXIV_NS, _validated_atom_root
from sources.document_limits import bounded_document_get
from sources.feed_contract import feed_nodes, feed_text, feed_link

REGISTRY = Path(__file__).resolve().parents[1] / "taxonomy/direction_channels.json"
_CAPITAL = re.compile(r"融资|领投|募资|投资轮|完成.{0,16}轮|\brais(?:e[ds]?|ing)\s+[$€£\d]|\bfunding\b|\bseries\s+[a-f]\b|\bseed round\b", re.I)
_TECH_JOB = re.compile(r"engineer|scientist|research|算法|研究|工程师|机器学习", re.I)


def topics(registry):
    base = [{**domain, "media_terms": [*domain["query_phrases"], *registry["chinese_terms"].get(domain["id"], [])]}
            for domain in load_landscape()["domains"]]
    return [*registry["specialized_topics"], *base]


def _matches(text, terms):
    text = re.sub(r"[-_/]+", " ", text).casefold()
    terms = [re.sub(r"[-_/]+", " ", term).casefold() for term in terms]
    return any(bool(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text))
               if term.isascii() else term in text for term in terms)


def topic_origins(candidate, registry):
    result = {}
    for origin in candidate.evidence:
        if origin.evidence_type not in ORIGIN_EVIDENCE_TYPES or not countable_evidence(candidate, origin):
            continue
        text = " ".join((origin.title, origin.summary, " ".join(origin.keywords)))
        matched = [topic for topic in topics(registry) if _matches(text, topic["media_terms"])]
        specific = [topic for topic in matched if topic in registry["specialized_topics"]]
        for topic in specific or matched:
            result.setdefault(topic["id"], {})[origin.fingerprint] = support_revision(origin)
    return result


def _plain(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))).strip()


def parse_capital_feed(text, source, *, now, window_days=7):
    records = []
    for node in feed_nodes(text):
        title = feed_text(node, "title")
        summary = _plain(feed_text(node, "description") or feed_text(node, "summary") or feed_text(node, "encoded"))
        raw_date = feed_text(node, "pubDate") or feed_text(node, "published")
        date = _evidence_datetime(raw_date)
        if not date:
            from email.utils import parsedate_to_datetime
            try: date = parsedate_to_datetime(raw_date)
            except (TypeError, ValueError, OverflowError): continue
        if date.tzinfo is None:
            continue
        url = feed_link(node, source["url"])
        if (_CAPITAL.search(title + " " + summary[:300]) and _canonical_material_url(url)
                and now - timedelta(days=window_days) <= date <= now):
            records.append({"kind": "reported_capital_event", "scope": "方向或组织背景，不证明当前机制承接",
                "title": title, "summary": summary[:800], "url": url,
                "published_at": date.isoformat(), "source": source["name"],
                "verification": "媒体报道，未在本阶段核对交割/金额或独立性；转载不重复计融资"})
    return list({record["url"]: record for record in records}.values())


def parse_jobs(payload, board):
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ValueError("Job board schema invalid")
    records = {}
    for job in payload["jobs"]:
        if not isinstance(job, dict) or not job.get("id") or not isinstance(job.get("title"), str):
            raise ValueError("Job board record invalid")
        title = job["title"]
        url = str(job.get("absolute_url") or "")
        if not _TECH_JOB.search(title) or not _canonical_material_url(url):
            continue
        key = str(job.get("internal_job_id") or job["id"])
        records.setdefault(key, {"kind": "hiring_intent", "title": title, "url": url,
            "summary": _plain(job.get("content"))[:3000], "organization": board["name"],
            "job_id": key, "published_at": "", "source": board["name"] + " 官方招聘",
            "verification": "当前开放招聘意向，不等于已入职或本期新增；updated_at不作发布日期"})
    return list(records.values())


def cohort_query(topic, *, now, days):
    end = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    middle, start = end - timedelta(days=days), end - timedelta(days=days * 2)
    phrases = " OR ".join('ti:"' + value.replace('"', '') + '"' for value in topic["query_phrases"])
    query = f'({phrases}) AND submittedDate:[{start:%Y%m%d%H%M} TO {end - timedelta(minutes=1):%Y%m%d%H%M}]'
    return query, start, middle, end


def parse_cohort(text, topic, *, now, days, query):
    root = _validated_atom_root(text)
    raw_total = root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
    if raw_total is None or not raw_total.isdigit():
        raise ValueError("arXiv cohort total missing")
    _, start, middle, end = cohort_query(topic, now=now, days=days)
    members = {}
    for entry in root.findall("atom:entry", ARXIV_NS):
        identifier = entry.findtext("atom:id", "", ARXIV_NS).rsplit("/abs/", 1)[-1]
        identifier = re.sub(r"v\d+$", "", identifier)
        date = _evidence_datetime(entry.findtext("atom:published", "", ARXIV_NS))
        if not re.fullmatch(r"\d{4}\.\d{4,5}", identifier) or not date or not start <= date < end:
            raise ValueError("arXiv cohort identity/date mismatch")
        if identifier in members:
            raise ValueError("arXiv cohort repeats a work identity")
        members[identifier] = {"arxiv": identifier, "published_at": date.isoformat()}
    complete = len(members) == int(raw_total)
    current = sum(_evidence_datetime(item["published_at"]) >= middle for item in members.values())
    previous = len(members) - current
    return {"kind": "paper_activity", "topic_id": topic["id"], "topic": topic["label"],
        "status": "complete" if complete else "incomplete_member_limit",
        "url": ARXIV_API + "?" + urlencode({"search_query": query, "max_results": 300, "sortBy": "submittedDate", "sortOrder": "descending"}),
        "query": query, "windows": {"previous_start": start.isoformat(), "current_start": middle.isoformat(), "end_exclusive": end.isoformat()},
        "previous_count": previous if complete else None, "current_count": current if complete else None,
        "members": list(members.values()), "index_total": int(raw_total),
        "verification": f"固定题名检索样本、UTC等长{days}日窗口、首提交日及基础ID去重；非全领域普查，不据署名推算人才入场"}


class DirectionSignalClient:
    def __init__(self, *, client_factory=None, registry=None):
        self.registry = registry or json.loads(REGISTRY.read_text(encoding="utf-8"))
        self.client_factory = client_factory or httpx.AsyncClient
        self.arxiv = ArxivSource()
        self._lock = asyncio.Lock()
        self._loaded = False
        self._media, self._jobs, self._cohorts, self._health = [], [], {}, {}

    async def _get(self, client, source):
        response = await bounded_document_get(client, source["url"])
        response.raise_for_status()
        if response.is_redirect:
            raise ValueError("Unreviewed source redirect")
        return response

    async def attach(self, candidates):
        if not candidates:
            return
        now = research_now()
        async with self._lock:
            try:
                async with asyncio.timeout(self.registry["bounds"]["phase_seconds"]):
                    async with self.client_factory(timeout=8, follow_redirects=False,
                        headers={"User-Agent": "AI-Paradigm-Radar/4.1 (public direction context)"}) as client:
                        if not self._loaded:
                            # Once per process, including failures; no repeated
                            # attempts multiplied by the number of candidates.
                            self._loaded = True
                            self._health.update({source["name"]: "not_executed" for source in
                                [*self.registry["media_feeds"], *self.registry["job_boards"]]})
                            for source in [*self.registry["media_feeds"], *self.registry["job_boards"]]:
                                try:
                                    response = await self._get(client, source)
                                    if source in self.registry["media_feeds"]:
                                        self._media += parse_capital_feed(response.text, source, now=now, window_days=research_days())
                                    else:
                                        observed = observation_now()
                                        if observed > now + timedelta(days=1):
                                            self._health[source["name"]] = "current_jobs_not_historical_snapshot"
                                            continue
                                        self._jobs += [{**record, "observed_at": observed.isoformat()}
                                                       for record in parse_jobs(response.json(), source)]
                                    self._health[source["name"]] = "read_latest_slice_not_exhaustive"
                                except Exception as exc:
                                    self._health[source["name"]] = "failed_" + type(exc).__name__
                        wanted = {key for candidate in candidates for key in topic_origins(candidate, self.registry)}
                        for topic in topics(self.registry):
                            if topic["id"] not in wanted or topic["id"] in self._cohorts:
                                continue
                            if len(self._cohorts) >= self.registry["bounds"]["arxiv_topics"]:
                                break  # Provider bound, not a research Top-K.
                            self._cohorts[topic["id"]] = {"status": "not_completed"}
                            try:
                                days = self.registry["bounds"]["comparison_days"]
                                query, *_ = cohort_query(topic, now=now, days=days)
                                response = await self.arxiv._get_with_retry(client, {"search_query": query,
                                    "max_results": self.registry["bounds"]["arxiv_members"], "start": 0,
                                    "sortBy": "submittedDate", "sortOrder": "descending"})
                                self._cohorts[topic["id"]] = parse_cohort(response.text, topic, now=now, days=days, query=query)
                                self._cohorts[topic["id"]]["url"] = str(response.request.url)
                            except Exception as exc:
                                self._cohorts[topic["id"]] = {"status": "failed_" + type(exc).__name__}
            except TimeoutError:
                self._health["phase"] = "time_budget_interrupted"
            except Exception as exc:
                self._health["phase"] = "failed_" + type(exc).__name__
            for candidate in candidates:
                bindings = topic_origins(candidate, self.registry)
                contexts = []
                for topic in topics(self.registry):
                    if topic["id"] not in bindings:
                        continue
                    media = [record for record in self._media if _matches(record["title"] + " " + record["summary"], topic["media_terms"])]
                    jobs = [record for record in self._jobs if _matches(record["title"] + " " + record["summary"], topic["media_terms"])]
                    # Keep public metadata and the actual matching terms, not
                    # copied articles, full job descriptions or applicant data.
                    def metadata(records):
                        return [{key: value for key, value in record.items() if key != "summary"} |
                            {"matched_terms": [term for term in topic["media_terms"]
                             if _matches(record["title"] + " " + record.get("summary", ""), [term])]}
                            for record in records]
                    contexts.append({"topic_id": topic["id"], "topic": topic["label"], "origins": bindings[topic["id"]],
                        "media": metadata(media), "jobs": metadata(jobs), "paper_activity": self._cohorts.get(topic["id"], {"status": "not_executed_provider_bound"})})
                candidate.direction_context = {"version": self.registry["version"], "route_key": candidate.key,
                    "observed_at": observation_now().isoformat(), "research_reference_time": now.isoformat(),
                    "role": "direction_background_not_mechanism_uptake", "source_health": dict(self._health), "topics": contexts}
            from run_audit import run_audit
            run_audit.event("direction_context", "collected", f"免费方向背景：{len(candidates)} 条候选；"
                f"媒体/招聘入口 {len(self._health)} 项状态；题名队列 {len(self._cohorts)} 项；不影响技术准入")


def context_for_report(candidate):
    """Only still-bound context can lend URLs or prose to this publication."""
    value = candidate.direction_context
    if not isinstance(value, dict) or value.get("route_key") != candidate.key:
        return {}
    reference = _evidence_datetime(value.get("research_reference_time"))
    # Provenance does not expire with the news window: an old frozen outbox
    # can cite its dated snapshot. Writers must preserve that as-of date.
    if not reference or reference > research_now():
        return {}
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    if value.get("version") != registry["version"]:
        return {}
    known_topics = {topic["id"]: topic for topic in topics(registry)}
    authorized = topic_origins(candidate, registry)
    current = {origin.fingerprint: support_revision(origin) for origin in candidate.evidence
               if origin.evidence_type in ORIGIN_EVIDENCE_TYPES and countable_evidence(candidate, origin)}
    selected = []
    for item in value.get("topics", []) if isinstance(value.get("topics"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("origins"), dict):
            continue
        if item.get("topic_id") not in authorized or item.get("topic_id") not in known_topics:
            continue
        if not any(current.get(key) == revision for key, revision in item["origins"].items()):
            continue
        raw_activity = item.get("paper_activity") if isinstance(item.get("paper_activity"), dict) else {}
        activity = {key: raw_activity.get(key) for key in
                    ("status", "url", "query", "windows", "previous_count", "current_count", "verification")}
        if not _cohort_receipt_valid(raw_activity, known_topics[item["topic_id"]], reference, registry):
            activity = {"status": activity["status"], "meaning": "未得到完整可复算成员，不作增减判断"}
        def records(kind):
            values = item.get(kind, [])
            return [record for record in values if isinstance(record, dict)
                    and _canonical_material_url(str(record.get("url") or ""))][:3] if isinstance(values, list) else []
        selected.append({"topic": str(item.get("topic", "")), "topic_id": item["topic_id"],
                         "selection_scope": "specific_direction" if any(t["id"] == item["topic_id"] for t in registry["specialized_topics"]) else "broad_domain",
                         "media": records("media"), "jobs": records("jobs"),
                         "paper_activity": activity})
    return {"role": "direction_background_not_mechanism_uptake", "topics": selected[:2],
            "as_of": reference.isoformat(),
            "overflow_topic_count": max(len(selected) - 2, 0),
            "source_health": value.get("source_health", {}), "observed_at": value.get("observed_at", ""),
            "boundary": "媒体报道非交割确认；招聘非入职/新增；同口径论文样本非整个行业。不得改变技术资格或机制承接结论"} if selected else {}


def _cohort_receipt_valid(value, topic, reference, registry):
    """Recount restored metadata instead of trusting editable headline counts."""
    try:
        days = registry["bounds"]["comparison_days"]
        query, start, middle, end = cohort_query(topic, now=reference, days=days)
        url = urlsplit(value.get("url", ""))
        if (value.get("status") != "complete" or value.get("query") != query
                or not _canonical_material_url(value.get("url", ""))
                or url.scheme != "https" or url.hostname != "export.arxiv.org" or url.path != "/api/query"
                or parse_qs(url.query).get("search_query") != [query]):
            return False
        members = value.get("members")
        if not isinstance(members, list) or len(members) > registry["bounds"]["arxiv_members"]:
            return False
        identities, dates = set(), []
        for member in members:
            if not isinstance(member, dict) or not re.fullmatch(r"\d{4}\.\d{4,5}", str(member.get("arxiv", ""))):
                return False
            date = _evidence_datetime(member.get("published_at"))
            if not date or not start <= date < end or member["arxiv"] in identities:
                return False
            identities.add(member["arxiv"])
            dates.append(date)
        current = sum(date >= middle for date in dates)
        if any(type(value.get(key)) is not int for key in ("index_total", "current_count", "previous_count")):
            return False
        return (value["index_total"] == len(dates) and value["current_count"] == current
                and value["previous_count"] == len(dates) - current
                and value.get("windows") == {"previous_start": start.isoformat(), "current_start": middle.isoformat(), "end_exclusive": end.isoformat()})
    except (TypeError, ValueError, KeyError):
        return False
