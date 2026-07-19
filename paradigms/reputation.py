"""前沿组织、重点研究者与官方入口的保守实体核验。"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

import config
from research_watchlist import (
    ORGANIZATIONS,
    RESEARCHERS,
    organization_record,
    organization_tier,
    source_record,
)


_PUBLIC_RESEARCH_HOSTS = {
    "arxiv.org",
    "openreview.net",
    "github.com",
    "huggingface.co",
}


def normalize_entity_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    normalized = re.sub(r"[^\w\u3400-\u9fff]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _segments(value: str) -> set[str]:
    raw_parts = re.split(r"[,;|\n]+", value or "")
    normalized = {normalize_entity_name(part) for part in raw_parts}
    normalized.add(normalize_entity_name(value))
    return {part for part in normalized if part}


def exact_alias_match(value: str, alias: str) -> bool:
    """只匹配完整机构字段/分段，杜绝 FAIR、1X、Seed 一类子串误伤。"""
    normalized_alias = normalize_entity_name(alias)
    return bool(normalized_alias) and normalized_alias in _segments(value)


def resolve_organization(value: str) -> dict | None:
    for item in ORGANIZATIONS:
        names = (item["name"], *item["aliases"])
        if any(exact_alias_match(value, alias) for alias in names):
            return {**item, "tier": organization_tier(item["id"])}

    # 兼容用户在环境变量中追加的精确别名；没有结构化 owner 关系，不能用于
    # 给 Priority 页面自动继承发布者身份。
    for alias in config.ESTABLISHED_RESEARCH_ORGANIZATIONS:
        if exact_alias_match(value, alias):
            return {
                "id": "legacy-custom-established",
                "name": alias,
                "aliases": (alias,),
                "kind": "custom",
                "tier": "established",
            }
    for alias in config.MONITORED_RESEARCH_ORGANIZATIONS:
        if exact_alias_match(value, alias):
            return {
                "id": "legacy-custom-monitored",
                "name": alias,
                "aliases": (alias,),
                "kind": "custom",
                "tier": "monitored",
            }
    return None


def verified_priority_researcher(profile: object) -> dict | None:
    name = str(getattr(profile, "name", "") or "")
    if not name:
        return None
    has_public_identity = bool(
        getattr(profile, "identifiers", None) or getattr(profile, "profile_urls", None)
    )
    if not has_public_identity:
        return None
    if not any(exact_alias_match(name, alias) for alias in config.PRIORITY_RESEARCHERS):
        return None
    for item in RESEARCHERS:
        if any(exact_alias_match(name, alias) for alias in item["aliases"]):
            return item
    return {"name": name, "aliases": (name,), "focus": "用户追加的重点研究者"}


def source_identity(index_url: str) -> tuple[dict | None, dict | None, str]:
    source = source_record(index_url)
    if not source:
        return None, None, "verified"
    owner = organization_record(source["owner"])
    return source, owner, str(source.get("tier") or "verified")


def source_link_allowed(index_url: str, detail_url: str) -> bool:
    source, _, _ = source_identity(index_url)
    index_host = urlparse(index_url).hostname or ""
    detail_host = urlparse(detail_url).hostname or ""
    if not detail_host:
        return False
    allowed = {index_host.casefold().removeprefix("www.")}
    if source:
        allowed.update(
            str(host).casefold().removeprefix("www.")
            for host in source.get("allowed_domains", ())
        )
        allowed.update(_PUBLIC_RESEARCH_HOSTS)
    normalized_detail = detail_host.casefold().removeprefix("www.")
    return any(
        normalized_detail == host or normalized_detail.endswith(f".{host}")
        for host in allowed
        if host
    )
