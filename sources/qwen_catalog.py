"""Registered finite public Qwen research collections; no JS execution.

The official research component concatenates page_config research.research-list
and article/retrieval (qwen_ai, en-US), then paginates that array in the browser.
Reader-facing URLs follow the verified frontend's /blog?id=<article.path>
protocol, not model guesses. Both landing and matching article API are checked.
"""

import hashlib
import re
from urllib.parse import urlencode, urlparse, parse_qs


async def fetch_catalog(get, *, cutoff, reference_time):
    from sources.priority_research_source import _ArticleParser, _parse_date

    async def read(url, maximum):
        response = await get(url)
        response.raise_for_status()
        if str(response.url) != url or len(response.content) > maximum:
            raise ValueError("Qwen catalog origin/size contract failed")
        return response.json()

    historical = await read("https://qwen.ai/api/page_config?code=research.research-list", 2_000_000)
    payload = await read("https://qwen.ai/api/v2/article/retrieval?type=qwen_ai&language=en-US", 10_000_000)
    if (not isinstance(historical, list) or not isinstance(payload, dict) or payload.get("success") is not True
            or not isinstance(payload.get("data"), dict) or not isinstance(payload["data"].get("articles"), list)):
        raise ValueError("Invalid Qwen public catalog schema")
    data, articles = payload["data"], payload["data"]["articles"]
    # This is a registered non-paged collection. A future server pagination
    # contract must not silently turn its first page into completed coverage.
    if any(value.get(key) not in (None, False, "", 0) for value in (payload, data) for key in ("has_more", "hasMore", "next_cursor", "nextCursor")):
        raise ValueError("Qwen catalog introduced unhandled pagination")
    for key in ("total", "count"):
        if key in data and (type(data[key]) is not int or data[key] != len(articles)):
            raise ValueError("Qwen catalog cardinality mismatch")
    if len(historical) > 5000 or len(articles) > 5000:
        raise ValueError("Qwen catalog record limit")

    links, pending, outside, conflicts, identities = {}, {}, 0, 0, set()
    paths = {}

    def defer(identity, reason):
        key = hashlib.sha256(str(identity).encode()).hexdigest()
        pending[key] = {"key": key, "status": "pending", "reason": reason}

    def date(value):
        # A year/dateModified/createTime cannot establish publication time.
        return _parse_date(value) if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}(?:T|$)", value) else None

    def normalize(value):
        return re.sub(r"\W+", "", re.sub(r"\s*\|\s*Qwen\s*$", "", value, flags=re.I)).casefold()

    for index, row in enumerate(articles):
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
                or not isinstance(row.get("path"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,160}", row["path"])
                or not isinstance(row.get("title"), str) or not row["title"].strip()
                or row.get("type") != "qwen_ai" or row.get("language") != "en-US"):
            defer(f"article:{index}", "invalid_record_identity")
            continue
        identity = row["id"]
        if identity in identities or row["path"] in paths:
            defer(identity, "duplicate_record_identity")
            continue
        identities.add(identity)
        paths[row["path"]] = row
        extra = row.get("extra")
        published = date(extra.get("date")) if isinstance(extra, dict) else None
        if not published:
            defer(identity, "unknown_publication_date")
            continue
        if not isinstance(row.get("content"), str):
            defer(identity, "article_content_missing")
            continue
        article = _ArticleParser("https://qwen.ai/research")
        article.feed(row["content"])
        body_date = date(article.published_at)
        if normalize(article.title) != normalize(row["title"]):
            defer(identity, "article_metadata_not_aligned")
            continue
        if body_date and body_date != published:
            conflicts += 1  # Current official component displays extra.date,
            # not the historical Hugo HTML's embedded metadata. Detail API
            # must independently return the same date before source closure.
        if published < cutoff or published > reference_time:
            outside += 1
            continue
        # Registered frontend protocol, not a guessed slug route: converter
        # assigns id=article.path and toBlogPage emits /blog?id=<id>. Landing
        # and detail API are both verified by the normal source detail stage.
        url = "https://qwen.ai/blog?" + urlencode({"id": row["path"]})
        fetch_url = "https://qwen.ai/api/v2/article/?" + urlencode({"language": "en-US", "path": row["path"], "type": "qwen_ai"})
        links[url] = {"title": row["title"].strip(), "url": url, "published_at": published.isoformat(), "fetch_url": fetch_url}

    old_ids = set()
    for index, row in enumerate(historical):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            defer(f"historical:{index}", "invalid_record_identity")
            continue
        identity = row["id"]
        if identity in old_ids:
            defer("historical:" + identity, "duplicate_record_identity")
            continue
        old_ids.add(identity)
        published = date(row.get("date"))
        if not published:
            defer("historical:" + identity, "unknown_publication_date")
        elif published < cutoff or published > reference_time:
            outside += 1
        elif identity not in paths:
            # tokenLinks is JSON data, not a guessed qwen.ai/blog/{id} landing.
            defer("historical:" + identity, "current_research_has_no_verified_article")
        elif not isinstance(paths[identity].get("extra"), dict) or date(paths[identity]["extra"].get("date")) != published:
            defer("historical:" + identity, "collection_publication_date_conflict")
    return list(links.values()), {"kind": "qwen_public_collections_v1", "records": len(articles) + len(historical),
        "article_records": len(articles), "historical_records": len(historical), "outside_window": outside,
        "resolved_links": len(links), "embedded_date_conflicts": conflicts,
        "resolved_links_are_detail_tasks": True, "pending_records": list(pending.values()), "complete": not pending}


def decode_article(response, link):
    """Verify the current article, never substitute another API search result."""
    from sources.priority_research_source import _parse_date
    response.raise_for_status()
    if str(response.url) != link.fetch_url or len(response.content) > 2_000_000:
        raise ValueError("Qwen article origin/size contract failed")
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) and payload.get("success") is True else None
    expected = parse_qs(urlparse(link.url).query).get("id", [])
    if (not isinstance(data, dict) or expected != [data.get("path")]
            or data.get("type") != "qwen_ai" or data.get("language") != "en-US"
            or data.get("title") != link.title or not isinstance(data.get("content"), str)
            or not isinstance(data.get("extra"), dict)
            or _parse_date(data["extra"].get("date", "")) != _parse_date(link.published_at)):
        raise ValueError("Qwen detail does not match current catalog identity/date")
    return data["content"], data["extra"]["date"]
