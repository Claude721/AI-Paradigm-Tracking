"""Machine-readable community execution receipts; never research verdicts."""

from __future__ import annotations

import hashlib
import json
import re

import config
from runtime_clock import research_days, research_now


SEARCH_STATUSES = {"completed", "disabled", "not_configured", "failed", "not_executed"}


def record_search(candidate, provider, status, *, reason="", count=0, diagnostics=None):
    if status not in SEARCH_STATUSES:
        raise ValueError("Invalid community search status")
    candidate.community_search_receipts[provider] = {
        "status": status, "reason": reason, "result_count": count,
    }
    if diagnostics:
        for key, value in diagnostics.items():
            if key in {"raw_result_count", "retained_result_count", "filtered_result_count", "confirmed_credits"}:
                if type(value) is not int or value < 0:
                    raise ValueError("Invalid search diagnostic count")
            elif key in {"request_id", "query_kind"}:
                if not isinstance(value, str) or len(value) > 200:
                    raise ValueError("Invalid search diagnostic identity")
            else:
                raise ValueError("Unknown search diagnostic")
        candidate.community_search_receipts[provider].update(diagnostics)


def search_receipt_summary(candidate):
    """Keep execution hashes/history out of model context and editorial prose."""
    return {provider: {key: value.get(key) for key in ("status", "reason", "result_count")}
            for provider, value in candidate.community_search_receipts.items()}


def incomplete_searches(candidate):
    pending = [key for key, receipt in candidate.community_search_receipts.items()
               if receipt.get("status") not in {"completed", "disabled"}]
    for provider, text in candidate.community_coverage.items():
        if provider in candidate.community_search_receipts or not isinstance(text, str):
            continue
        explicit_failure = (
            re.match(r"^(?:已尝试|已发起).*(?:返回 HTTP [45]\d{2}|查询异常：)", text)
            or text.startswith("此前 GitHub Search 已鉴权/限流失败")
            or text.startswith("触发用户显式配置的 Tavily credit safety limit")
            or text.startswith("已配置 Reddit Data API，但本轮 OAuth Token 获取失败")
            or text.startswith("社区证据总入口发生结构性异常：")
        )
        if provider in {"github", "hackernews", "tavily_social_web", "reddit_official", "x_official", "community_runtime"} and explicit_failure:
            pending.append(provider)
    return pending


def blocking_searches(candidate, *, allow_partial=False):
    pending = incomplete_searches(candidate)
    if not allow_partial:
        return pending
    pending = [key for key in pending if candidate.community_search_receipts.get(key, {}).get("status") != "not_configured"]
    # A failed supplementary provider is a blind spot, not a veto. A complete
    # outage still cannot become a successful negative uptake judgment.
    if any(receipt.get("status") == "completed"
           for receipt in candidate.community_search_receipts.values()):
        return []
    return pending


def search_signature(candidate, provider):
    from paradigms.models import ORIGIN_EVIDENCE_TYPES
    # No credentials, response bodies, names of community users or query URLs.
    origins = sorted((item.fingerprint, item.source_revision, item.title)
                     for item in candidate.evidence
                     if item.evidence_type in ORIGIN_EVIDENCE_TYPES)
    settings = {
        "github": bool(config.GITHUB_TOKEN),
        "tavily_social_web": [config.TAVILY_SOCIAL_SEARCH_ENABLED,
                              bool(config.TAVILY_API_KEY), config.TAVILY_DISCOVERY_DOMAINS,
                              config.TAVILY_SOCIAL_MAX_RESULTS, config.SOURCING_LOOKBACK_DAYS],
        "reddit_official": [config.REDDIT_API_ACCESS_APPROVED, bool(config.REDDIT_CLIENT_ID),
                            bool(config.REDDIT_CLIENT_SECRET), bool(config.REDDIT_USER_AGENT)],
        "x_official": [config.KOL_X_SOURCE_ENABLED, bool(config.TWITTER_BEARER_TOKEN)],
    }
    payload = [2, provider, candidate.key, candidate.name, candidate.keywords,
               origins, research_now().isoformat(), research_days(), settings.get(provider)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def reusable_search(candidate, provider, signature):
    receipt = candidate.community_search_receipts.get(provider, {})
    if receipt.get("status") != "completed" or receipt.get("input_signature") != signature:
        return False
    fingerprints = receipt.get("evidence_fingerprints")
    if not isinstance(fingerprints, list) or len(fingerprints) != receipt.get("result_count"):
        return False
    evidence = {item.fingerprint: item for item in candidate.evidence}
    return all(key in evidence and (not evidence[key].raw.get("content_scrubbed")
                                   or evidence[key].raw.get("indexed_discovery_only") is True)
               for key in fingerprints)
