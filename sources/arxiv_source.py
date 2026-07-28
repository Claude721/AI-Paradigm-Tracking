"""arXiv 信源：发现可能改变能力边界的最新 AI 技术工作。"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

import config
from paradigms.landscape import (
    arxiv_priority_author_query_plan,
    arxiv_query_plan,
    classify_frontier_domains,
)
from paradigms.publication import classify_publication
from paradigms.reputation import resolve_organization

from .base import BaseSource, RawProject

logger = logging.getLogger(__name__)

ARXIV_API = "https://export.arxiv.org/api/query"

ARXIV_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

# 保留公开常量供体检和测试读取；事实来源是版本化覆盖地图。
SEARCH_QUERIES = [item["query"] for item in arxiv_query_plan()]
TECHNICAL_REPORT_QUERY = (
    '(ti:"technical report" OR ti:"system report" OR ti:"research report" '
    'OR ti:"whitepaper" OR ti:"white paper" OR ti:"system card" '
    'OR ti:"model card" OR abs:"technical report" '
    'OR co:"technical report" OR co:"tech report" OR co:"system report" '
    'OR co:"whitepaper" OR co:"white paper" OR co:"system card" '
    'OR co:"model card") AND '
    "(cat:cs.AI OR cat:cs.CL OR cat:cs.LG OR cat:cs.CV OR cat:cs.RO "
    "OR cat:cs.DC OR cat:cs.AR OR cat:eess.SY OR cat:q-bio.BM "
    "OR cat:q-bio.GN OR cat:physics.comp-ph OR cat:cond-mat.mtrl-sci)"
)


class ArxivSource(BaseSource):
    source_name = "arxiv"

    def __init__(
        self,
        max_results: int | None = None,
        lookback_days: int = 3,
        seed_arxiv_ids: list[str] | None = None,
    ):
        self.max_results = max_results
        self.lookback_days = lookback_days
        self.seed_arxiv_ids = _normalize_seed_ids(
            seed_arxiv_ids
            if seed_arxiv_ids is not None
            else config.PARADIGM_SEED_ARXIV_IDS
        )
        self.executed_query_groups: set[str] = set()
        self.failed_query_groups: set[str] = set()
        self.executed_recall_lanes: set[str] = set()
        self.failed_recall_lanes: set[str] = set()
        self.recall_lane_hits: dict[str, int] = {}

    async def fetch(self) -> list[RawProject]:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            results: list[RawProject] = []
            for query_spec in arxiv_query_plan():
                if self.max_results and len(results) >= self.max_results:
                    break
                group = str(query_spec["group"])
                try:
                    batch = await self._fetch_query(
                        client,
                        str(query_spec["query"]),
                        query_group=group,
                        domain_ids=list(query_spec["domain_ids"]),
                        result_limit=(
                            self.max_results - len(results)
                            if self.max_results
                            else None
                        ),
                    )
                    results.extend(batch)
                    self.executed_query_groups.add(group)
                    self._record_lane(f"landscape:{group}", len(batch))
                except Exception as exc:
                    self.failed_query_groups.add(group)
                    self.failed_recall_lanes.add(f"landscape:{group}")
                    logger.warning("arXiv 覆盖查询失败 [%s]: %s", group, exc)

            if config.PARADIGM_PRIORITY_AUTHOR_SWEEP_ENABLED:
                for query_spec in arxiv_priority_author_query_plan(
                    config.PRIORITY_RESEARCHERS
                ):
                    lane = str(query_spec["group"])
                    try:
                        batch = await self._fetch_query(
                            client,
                            str(query_spec["query"]),
                            query_group="priority_researchers",
                            result_limit=(
                                max(self.max_results - len(results), 0)
                                if self.max_results
                                else None
                            ),
                        )
                        results.extend(batch)
                        self._record_lane(lane, len(batch))
                    except Exception as exc:
                        self.failed_recall_lanes.add(lane)
                        logger.warning("arXiv 重点研究者召回失败 [%s]: %s", lane, exc)

            try:
                batch = await self._fetch_query(
                    client,
                    TECHNICAL_REPORT_QUERY,
                    force_technical_report=True,
                    query_group="technical_reports",
                    result_limit=(
                        max(self.max_results - len(results), 0)
                        if self.max_results
                        else None
                    ),
                )
                results.extend(batch)
                self._record_lane("technical_documents", len(batch))
            except Exception as exc:
                self.failed_recall_lanes.add("technical_documents")
                logger.warning("arXiv Technical Report 专项查询失败: %s", exc)

            if self.seed_arxiv_ids:
                try:
                    batch = await self._fetch_seed_ids(client, self.seed_arxiv_ids)
                    results.extend(batch)
                    self._record_lane("explicit_seeds", len(batch))
                except Exception as exc:
                    self.failed_recall_lanes.add("explicit_seeds")
                    logger.warning("arXiv 精确补录查询失败: %s", exc)

        if self.failed_query_groups and not self.executed_query_groups:
            raise RuntimeError("arXiv 所有前沿覆盖查询均失败")
        deduped = list({item.url: item for item in results}.values())
        deduped.sort(
            key=lambda item: (
                int(item.extra.get("origin_priority", 0) or 0),
                item.created_at or "",
            ),
            reverse=True,
        )
        return deduped[: self.max_results] if self.max_results else deduped

    def recall_coverage(self) -> dict[str, dict[str, object]]:
        lanes = set(self.recall_lane_hits) | self.failed_recall_lanes
        return {
            lane: {
                "status": (
                    "query_failed"
                    if lane in self.failed_recall_lanes
                    else (
                        "searched_zero_hits"
                        if self.recall_lane_hits.get(lane, 0) == 0
                        else "covered"
                    )
                ),
                "hits": self.recall_lane_hits.get(lane, 0),
            }
            for lane in sorted(lanes)
        }

    def _record_lane(self, lane: str, hits: int) -> None:
        self.executed_recall_lanes.add(lane)
        self.recall_lane_hits[lane] = self.recall_lane_hits.get(lane, 0) + hits

    async def _fetch_query(
        self,
        client: httpx.AsyncClient,
        query: str,
        *,
        force_technical_report: bool = False,
        query_group: str = "",
        domain_ids: list[str] | None = None,
        result_limit: int | None = None,
        ignore_lookback: bool = False,
    ) -> list[RawProject]:
        """按日期窗口自适应翻页；max_results 仅作为显式 safety limit。"""
        if result_limit is not None and result_limit <= 0:
            return []
        page_size = min(result_limit, 100) if result_limit else 100
        start = 0
        results: list[RawProject] = []
        while True:
            remaining = (
                max(result_limit - len(results), 0)
                if result_limit
                else page_size
            )
            if result_limit and remaining <= 0:
                break
            request_size = min(page_size, remaining) if result_limit else page_size
            response = await self._request(
                client, query, request_size, start=start
            )
            results.extend(
                self._parse_atom_feed(
                    response.text,
                    force_technical_report=force_technical_report,
                    query_group=query_group,
                    domain_ids=domain_ids or [],
                    ignore_lookback=ignore_lookback,
                )
            )
            entry_count, reached_cutoff = self._page_state(
                response.text, ignore_lookback=ignore_lookback
            )
            if entry_count < request_size or reached_cutoff:
                break
            start += request_size
        if result_limit:
            return results[:result_limit]
        return results

    async def _fetch_seed_ids(
        self, client: httpx.AsyncClient, arxiv_ids: list[str]
    ) -> list[RawProject]:
        response = await self._get_with_retry(
            client,
            {
                "id_list": ",".join(arxiv_ids),
                "max_results": len(arxiv_ids),
            },
        )
        response.raise_for_status()
        return self._parse_atom_feed(
            response.text,
            query_group="explicit_seed",
            ignore_lookback=True,
        )

    async def _request(
        self,
        client: httpx.AsyncClient,
        query: str,
        max_results: int,
        *,
        start: int = 0,
    ) -> httpx.Response:
        return await self._get_with_retry(
            client,
            {
                "search_query": query,
                "start": start,
                "max_results": max_results,
                # 新提交在发布时也有 updated 时间；按 lastUpdatedDate 排序同时
                # 捕捉本周发布与旧路线的新版本，不再只盯首次提交日。
                "sortBy": "lastUpdatedDate",
                "sortOrder": "descending",
            },
        )

    async def _get_with_retry(
        self,
        client: httpx.AsyncClient,
        params: dict[str, object],
    ) -> httpx.Response:
        """arXiv export 偶发超时；做一次短退避重试，并保留最终故障给覆盖审计。"""
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await client.get(ARXIV_API, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                return response
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                retryable_status = (
                    isinstance(exc, httpx.HTTPStatusError)
                    and (
                        exc.response.status_code == 429
                        or exc.response.status_code >= 500
                    )
                )
                if attempt == 1 or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and not retryable_status
                ):
                    raise
                await asyncio.sleep(0.75)
        raise RuntimeError("arXiv 请求重试后仍失败") from last_error

    def _page_state(
        self, xml_text: str, *, ignore_lookback: bool = False
    ) -> tuple[int, bool]:
        root = ET.fromstring(xml_text)
        entries = root.findall("atom:entry", ARXIV_NS)
        if ignore_lookback:
            return len(entries), False
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        dates = []
        for entry in entries:
            latest = _latest_entry_date(entry)
            if latest is None:
                continue
            dates.append(latest)
        return len(entries), bool(dates and min(dates) < cutoff)

    def _parse_atom_feed(
        self,
        xml_text: str,
        force_technical_report: bool = False,
        *,
        query_group: str = "",
        domain_ids: list[str] | None = None,
        ignore_lookback: bool = False,
    ) -> list[RawProject]:
        root = ET.fromstring(xml_text)
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        results: list[RawProject] = []

        for entry in root.findall("atom:entry", ARXIV_NS):
            published_str = entry.findtext("atom:published", "", ARXIV_NS)
            updated_str = entry.findtext("atom:updated", "", ARXIV_NS)
            latest_date = _latest_entry_date(entry)
            if not ignore_lookback and latest_date and latest_date < cutoff:
                continue

            title = entry.findtext("atom:title", "", ARXIV_NS).strip().replace("\n", " ")
            summary = entry.findtext("atom:summary", "", ARXIV_NS).strip()

            link = ""
            for link_elem in entry.findall("atom:link", ARXIV_NS):
                if link_elem.get("type") == "text/html":
                    link = link_elem.get("href", "")
                    break
            if not link:
                id_text = entry.findtext("atom:id", "", ARXIV_NS)
                link = id_text

            authors = [
                a.findtext("atom:name", "", ARXIV_NS)
                for a in entry.findall("atom:author", ARXIV_NS)
            ]
            first_author = authors[0] if authors else ""

            categories = [
                c.get("term", "")
                for c in entry.findall("atom:category", ARXIV_NS)
            ]
            arxiv_comment = entry.findtext("arxiv:comment", "", ARXIV_NS).strip()
            matched_domains = classify_frontier_domains(
                title, summary, " ".join(categories)
            )
            # query group 记录的是“通过哪条发现通道被看到”，文本分类记录的是
            # “材料实际属于哪些领域”；二者分别用于接口审计和内容覆盖审计。
            declared_domains = list(domain_ids or [])
            priority_author = any(
                _normalized_name(author) in {
                    _normalized_name(value)
                    for value in config.PRIORITY_RESEARCHERS
                }
                for author in authors
            )
            classification = classify_publication(
                title=title,
                summary=summary,
                metadata=arxiv_comment,
                url=link,
                authors=authors,
                discovered_by_report_query=force_technical_report,
            )
            origin_kind = classification.origin_kind
            classification_reason = classification.reason
            is_report = origin_kind == "technical_report"
            team_organization = next(
                (
                    organization
                    for author in authors
                    if (organization := resolve_organization(author)) is not None
                ),
                None,
            )
            organization_name = (
                str(team_organization["name"]) if team_organization else ""
            )
            publisher_tier = (
                str(team_organization["tier"]) if team_organization else "unknown"
            )
            publisher_evidence = (
                f"arXiv 团队署名与内置机构别名精确匹配：{organization_name}"
                if team_organization
                else ""
            )

            results.append(
                RawProject(
                    source=self.source_name,
                    name=title,
                    url=link,
                    description=summary[:1500],
                    readme_summary=summary[:3000],
                    author=first_author,
                    topics=categories,
                    created_at=published_str,
                    extra={
                        "arxiv_id": link.rstrip("/").split("/")[-1],
                        "all_authors": authors,
                        "updated_at": updated_str,
                        "type": "paper",
                        "organization": organization_name,
                        "publisher_tier": publisher_tier,
                        "publisher_evidence": publisher_evidence,
                        "origin_kind": origin_kind,
                        "origin_priority": 3 if is_report else (2 if priority_author else 1),
                        "arxiv_comment": arxiv_comment,
                        "origin_classification_reason": classification_reason,
                        "document_format": classification.document_format,
                        "system_layer_count": classification.system_layer_count,
                        "query_group": query_group,
                        "query_domain_ids": declared_domains,
                        "frontier_domains": matched_domains or declared_domains,
                        "discovery_lookback_days": self.lookback_days,
                        "explicit_seed": query_group == "explicit_seed",
                    },
                )
            )

        return results


def _latest_entry_date(entry: ET.Element) -> datetime | None:
    values = [
        entry.findtext("atom:published", "", ARXIV_NS),
        entry.findtext("atom:updated", "", ARXIV_NS),
    ]
    dates = []
    for value in values:
        if not value:
            continue
        try:
            dates.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            continue
    return max(dates) if dates else None


def _normalize_seed_ids(values: list[str]) -> list[str]:
    normalized = []
    for value in values:
        arxiv_id = re.sub(
            r"^https?://arxiv\.org/(?:abs|pdf|html)/",
            "",
            value.strip(),
            flags=re.IGNORECASE,
        )
        arxiv_id = arxiv_id.removesuffix(".pdf").split("v", 1)[0]
        if re.fullmatch(r"\d{4}\.\d{4,5}", arxiv_id):
            normalized.append(arxiv_id)
    return list(dict.fromkeys(normalized))


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())


def _infer_origin_kind(
    *,
    title: str,
    summary: str,
    arxiv_comment: str,
    authors: list[str],
    forced: bool = False,
) -> tuple[str, str]:
    """兼容旧测试/调用；实际规则统一由 publication 模块维护。"""
    result = classify_publication(
        title=title,
        summary=summary,
        metadata=arxiv_comment,
        authors=authors,
        discovered_by_report_query=forced,
    )
    return result.origin_kind, result.reason
