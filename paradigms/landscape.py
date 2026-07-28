"""版本化 AI 前沿覆盖地图与可审计召回计划。"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import config


@lru_cache(maxsize=4)
def load_landscape(path: str = "") -> dict[str, Any]:
    target = Path(path) if path else config.FRONTIER_LANDSCAPE_PATH
    payload = json.loads(target.read_text(encoding="utf-8"))
    _validate_landscape(payload)
    return payload


def arxiv_query_plan(path: str = "") -> list[dict[str, Any]]:
    """按宏观技术栈合并查询，兼顾领域审计与 API 调用数量。"""
    landscape = load_landscape(path)
    groups: dict[str, list[dict[str, Any]]] = {}
    for domain in landscape["domains"]:
        groups.setdefault(domain["group"], []).append(domain)

    plan = []
    for group_id, domains in groups.items():
        phrases = _unique(
            phrase for domain in domains for phrase in domain["query_phrases"]
        )
        categories = _unique(
            category
            for domain in domains
            for category in domain["arxiv_categories"]
        )
        phrase_query = " OR ".join(f'all:"{_escape(phrase)}"' for phrase in phrases)
        category_query = " OR ".join(f"cat:{category}" for category in categories)
        plan.append(
            {
                "group": group_id,
                "domain_ids": [domain["id"] for domain in domains],
                "query": f"({phrase_query}) AND ({category_query})",
            }
        )
    return plan


def arxiv_priority_author_query_plan(
    names: Iterable[str],
    *,
    chunk_size: int = 16,
    path: str = "",
) -> list[dict[str, Any]]:
    """用重点研究者建立与术语无关的第二召回车道。

    新范式往往尚未使用覆盖地图里的既有名词，但重点研究者姓名相对稳定。这里
    只负责召回，人物身份和技术价值仍在后续独立核验，不能凭姓名直接入选。
    """

    normalized: dict[str, str] = {}
    for value in names:
        cleaned = re.sub(r"\s+", " ", str(value)).strip()
        key = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", cleaned.casefold())
        if cleaned and key:
            normalized.setdefault(key, cleaned)
    authors = list(normalized.values())
    if not authors:
        return []

    categories = _unique(
        category
        for domain in load_landscape(path)["domains"]
        for category in domain["arxiv_categories"]
    )
    category_query = " OR ".join(f"cat:{category}" for category in categories)
    size = max(int(chunk_size), 1)
    return [
        {
            "group": f"priority_researchers_{index // size + 1}",
            "query": (
                "("
                + " OR ".join(
                    f'au:"{_escape(name)}"' for name in authors[index : index + size]
                )
                + f") AND ({category_query})"
            ),
            "authors": authors[index : index + size],
        }
        for index in range(0, len(authors), size)
    ]


def openalex_search_plan(path: str = "") -> list[str]:
    """OpenAlex 搜索语法较宽松；每个领域保留一条独立检索以便去重。"""
    return [
        " OR ".join(f'"{phrase}"' for phrase in domain["query_phrases"])
        for domain in load_landscape(path)["domains"]
    ]


def classify_frontier_domains(*values: str, path: str = "") -> list[str]:
    text = " ".join(value for value in values if value).casefold()
    normalized = re.sub(r"[-_/]+", " ", text)
    matched = []
    for domain in load_landscape(path)["domains"]:
        if any(_contains_term(normalized, term) for term in domain["match_terms"]):
            matched.append(domain["id"])
    return matched


def coverage_report(
    evidence: Iterable[object],
    *,
    executed_groups: Iterable[str] = (),
    failed_groups: Iterable[str] = (),
    path: str = "",
) -> dict[str, Any]:
    """区分“查询成功但零命中”和“根本没有执行/执行失败”两类空白。"""
    landscape = load_landscape(path)
    counts = {domain["id"]: 0 for domain in landscape["domains"]}
    labels = {domain["id"]: domain["label"] for domain in landscape["domains"]}
    for item in evidence:
        raw = getattr(item, "raw", {}) or {}
        domains = raw.get("frontier_domains") or classify_frontier_domains(
            str(getattr(item, "title", "")),
            str(getattr(item, "summary", "")),
            " ".join(getattr(item, "keywords", []) or []),
            path=path,
        )
        for domain_id in set(domains):
            if domain_id in counts:
                counts[domain_id] += 1

    executed = set(executed_groups)
    failed = set(failed_groups)
    group_by_domain = {
        domain["id"]: domain["group"] for domain in landscape["domains"]
    }
    statuses = {}
    for domain_id, count in counts.items():
        group = group_by_domain[domain_id]
        if count > 0:
            status = "covered"
        elif group in failed:
            status = "query_failed"
        elif group not in executed:
            status = "not_executed"
        else:
            status = "searched_zero_hits"
        statuses[domain_id] = {
            "label": labels[domain_id],
            "group": group,
            "status": status,
            "hits": count,
        }
    return {
        "landscape_version": landscape["version"],
        "domains": statuses,
        "covered_domains": sum(value["hits"] > 0 for value in statuses.values()),
        "total_domains": len(statuses),
        "query_failures": sorted(failed),
    }


def _validate_landscape(payload: dict[str, Any]) -> None:
    if not payload.get("version"):
        raise ValueError("前沿覆盖地图缺少 version")
    domains = payload.get("domains")
    if not isinstance(domains, list) or not domains:
        raise ValueError("前沿覆盖地图没有 domains")
    required = set(payload.get("required_domain_ids") or [])
    ids = {str(domain.get("id", "")) for domain in domains}
    missing = required - ids
    if missing:
        raise ValueError(f"前沿覆盖地图缺少必需领域: {sorted(missing)}")
    for domain in domains:
        for field in (
            "id",
            "label",
            "group",
            "arxiv_categories",
            "query_phrases",
            "match_terms",
        ):
            if not domain.get(field):
                raise ValueError(f"领域 {domain.get('id', '?')} 缺少 {field}")


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _escape(value: str) -> str:
    return value.replace('"', " ")


def _contains_term(text: str, term: str) -> bool:
    """按完整英文词/短语匹配，避免 `agent` 错命中 `reagent` 等噪声。"""
    normalized_term = re.sub(r"[-_/]+", " ", term.casefold()).strip()
    if not normalized_term:
        return False
    pattern = re.escape(normalized_term).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", text))
