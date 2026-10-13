"""Registered Meta publication cards and finite public pagination, as data only.

The live catalogue mixes recent and legacy records. Never stop at an old card
or assume that a page boundary is a publication-date boundary.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlparse

from runtime_clock import research_now

CATALOG_URL = "https://ai.meta.com/results/?content_types%5B0%5D=publication"
VERSION = 1
STAGE = "meta_publication_catalog"
PAGE_LIMIT = 128
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_EMPTY_MESSAGE = "Sorry, no results matched your search criteria. Please try adjusting your search criteria."


def _empty_receipt_valid(receipt, page):
    expected = {"resultsAreaId":"fb_ai_results_grid", "messageAreaId":"fb_ai_results_header_id",
        "contentType":"PUBLICATION", "page":page-1, "limit":12, "offset":0, "searchQuery":"",
        "filterId":None, "researchAreas":None, "filterTags":None, "years":None,
        "locations":None, "sortBy":None, "pinnedPosts":[], "blogPostsToExclude":[]}
    return (page > 1 and isinstance(receipt, dict) and all(key in receipt and receipt[key] == value
        for key, value in expected.items()) and type(receipt.get('page')) is int
        and type(receipt.get('limit')) is int and type(receipt.get('offset')) is int
        and type(receipt.get('unfilteredResultsHits')) is int and receipt['unfilteredResultsHits'] >= 0)


def _empty_receipt(blocks, page):
    # Decode only the registered initializer's JSON props object, as data.
    # Never evaluate its surrounding JavaScript or infer a filtered total
    # from unfilteredResultsHits (which includes other content types).
    matches = []
    for block in blocks:
        for match in re.finditer(r'(?<!\\)"props"\s*:\s*(?=\{)', block):
            try:
                value, _ = json.JSONDecoder().raw_decode(block, match.end())
            except (ValueError, RecursionError):
                continue
            if isinstance(value, dict) and value.get('resultsAreaId') == 'fb_ai_results_grid':
                matches.append(value)
    if len(matches) != 1 or not _empty_receipt_valid(matches[0], page):
        raise ValueError('Meta empty tail query identity is missing or ambiguous')
    return matches[0]


def publication_path(path):
    return bool(re.fullmatch(r"/research/publications/[^/?#\s\\]+/", path)
                and not re.search(r"%2f|%5c", path, re.I))


def catalogue_url(url, *, page):
    parsed = urlparse(url)
    values = parse_qsl(parsed.query, keep_blank_values=True)
    if (parsed.scheme != "https" or parsed.netloc != "ai.meta.com" or parsed.path != "/results/"
            or parsed.fragment or len(values) != len(dict(values))):
        raise ValueError("Meta catalogue URL contract changed")
    query = dict(values)
    if query != {"content_types[0]": "publication", **({"page": str(page)} if page > 1 else {})}:
        raise ValueError("Meta catalogue page identity mismatch")
    return url


class _Cards(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.current = None
        self.cards = []
        self.next_urls = []
        self.headings = []
        self.empty_messages = []
        self.data_blocks = []
        self.bad = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "div" and "data-ms" in attrs:
            try:
                data = json.loads(attrs["data-ms"])
            except ValueError:
                data = {}
            if data.get("creative") == "listview-card":
                if self.current is not None:
                    self.bad = True
                self.current = {"depth": len(self.stack), "titles": [], "dates": [], "urls": []}
        role = ("title" if tag == "h4" and {"_8w6a", "_8w61"} <= set(classes) else
                "date" if tag == "p" and ("_8wl0" in classes) else
                "empty_message" if tag == "h3" and {"_8w6a", "_8w6d"} <= set(classes) else
                "data" if tag == "script" and not attrs.get('src') else
                "heading" if tag == "h1" else "")
        if self.current is not None and tag == "a" and attrs.get("href"):
            self.current["urls"].append(attrs["href"])
        if tag == "a" and "next" in attrs.get("rel", "").split():
            self.next_urls.append(attrs.get("href", ""))
        if tag not in _VOID:
            self.stack.append((tag, role, []))

    def handle_data(self, text):
        for _, role, parts in self.stack:
            if role:
                parts.append(text)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if not self.stack or self.stack[-1][0] != tag:
            self.bad = True
            return
        _, role, parts = self.stack.pop()
        if role == 'data':
            self.data_blocks.append(''.join(parts))
        text = " ".join(" ".join(parts).split())
        if role == "heading":
            self.headings.append(text)
        if role == 'empty_message':
            self.empty_messages.append(text)
        if self.current is not None and role in {"title", "date"} and text:
            self.current[role + "s"].append(text)
        if self.current is not None and len(self.stack) == self.current["depth"]:
            self.cards.append(self.current)
            self.current = None


def decode_page(text, url, *, page):
    catalogue_url(url, page=page)
    parser = _Cards()
    parser.feed(text)
    if (not parser.bad and not parser.current and not parser.cards and not parser.headings
            and parser.empty_messages == [_EMPTY_MESSAGE] and not parser.next_urls):
        receipt = _empty_receipt(parser.data_blocks, page)
        return {'number':page, 'url':url, 'rows':[], 'next_url':'', 'empty_query_receipt':receipt}
    if parser.bad or parser.current or parser.headings != ["Publications"] or not parser.cards:
        raise ValueError("Registered Meta publication cards are missing or malformed")
    from sources.priority_research_source import _date_from_text
    rows = []
    for card in parser.cards:
        urls = {urljoin(url, href) for href in card["urls"]
                if publication_path(urlparse(urljoin(url, href)).path)}
        dates = {_date_from_text(value) for value in card["dates"]}
        if (len(card["titles"]) != 1 or len(card["titles"][0]) < 5
                or len(urls) != 1 or len(dates) != 1 or "" in dates):
            raise ValueError("Meta publication card identity/date is ambiguous")
        primary = urls.pop()
        if urlparse(primary).netloc != "ai.meta.com" or urlparse(primary).query or urlparse(primary).fragment:
            raise ValueError("Meta publication card left its registered origin")
        rows.append({"title": card["titles"][0], "url": primary, "published_at": dates.pop()})
    if len(parser.next_urls) > 1:
        raise ValueError("Meta publication page has ambiguous pagination")
    next_url = urljoin(url, parser.next_urls[0]) if parser.next_urls else ""
    if next_url:
        catalogue_url(next_url, page=page + 1)
    return {"number": page, "url": url, "rows": rows, "next_url": next_url}


async def fetch_catalog(get, *, index_url, lookback_days, stage_cache=None, sleeper=None, first_response=None, max_pages=None):
    """Commit each healthy page before advancing; preserve partial work on failure."""
    if max_pages is not None and (type(max_pages) is not int or not 1 <= max_pages <= PAGE_LIMIT):
        raise ValueError('Invalid Meta catalogue page sample limit')
    request_limit = PAGE_LIMIT if max_pages is None else max_pages
    scope = [VERSION, index_url, lookback_days, research_now().isoformat()]
    signature = hashlib.sha256(json.dumps(scope, ensure_ascii=False).encode()).hexdigest()
    saved = stage_cache.cache_get(STAGE, signature) if stage_cache else None
    pages = [] if saved is None else saved.get("pages") if isinstance(saved, dict) else None
    if not isinstance(pages, list):
        raise ValueError("Invalid Meta catalogue checkpoint")
    history = [] if saved is None else saved.get("failure_history", [])
    if (not isinstance(history, list) or (saved is not None and saved.get("scope", scope) != scope)
            or any(not isinstance(item, dict) or type(item.get("page")) is not int
                   or not isinstance(item.get("reason"), str) for item in history)):
        raise ValueError("Invalid Meta catalogue failure/scope checkpoint")
    expected, seen, conflicts, page_receipts = CATALOG_URL, {}, {}, set()

    def page_identity(rows):
        return json.dumps(sorted(rows, key=lambda row: (row['url'], row['title'], row['published_at'])), sort_keys=True)

    def remember(rows):
        for row in rows:
            original = seen.setdefault(row['url'], row)
            if original != row:
                variants = conflicts.setdefault(row['url'], [original])
                if row not in variants:
                    variants.append(row)
    for number, page in enumerate(pages, 1):
        if not isinstance(page, dict) or page.get("number") != number or page.get("url") != expected:
            raise ValueError("Meta catalogue checkpoint page identity mismatch")
        catalogue_url(expected, page=number)
        if not isinstance(page.get("rows"), list):
            raise ValueError("Invalid Meta catalogue checkpoint rows")
        if not page['rows'] and (number != len(pages) or page.get('next_url')
                or not _empty_receipt_valid(page.get('empty_query_receipt'), number)):
            raise ValueError('Invalid Meta catalogue checkpoint empty tail')
        for row in page["rows"]:
            if (not isinstance(row, dict) or set(row) != {"title", "url", "published_at"}
                    or not all(isinstance(value, str) for value in row.values())
                    or urlparse(row["url"]).netloc != "ai.meta.com"
                    or not publication_path(urlparse(row["url"]).path)
                    or urlparse(row["url"]).scheme != "https" or urlparse(row["url"]).query
                    or urlparse(row["url"]).fragment or not row["title"]
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["published_at"])):
                raise ValueError("Invalid Meta catalogue checkpoint publication")
            datetime.fromisoformat(row["published_at"])
        identity = page_identity(page['rows'])
        if identity in page_receipts:
            raise ValueError("Meta catalogue checkpoint repeats a whole page")
        page_receipts.add(identity)
        remember(page['rows'])
        expected = page.get("next_url")
        if not isinstance(expected, str) or (not expected and number != len(pages)):
            raise ValueError("Invalid Meta catalogue checkpoint pagination")
        if expected:
            catalogue_url(expected, page=number + 1)
    diagnostics = {"version": VERSION, "complete": False, "pagination_pending": 1,
        "pending_records": [], "pages_completed": len(pages), "pagination_requests": 0,
        "failure_history": history,
        "request_limit":request_limit, "selection_limit_origin":"production_safety_limit" if max_pages is None else "caller_sample",
        "coverage_boundary": "finite_catalogue_exhaustion_not_old_card_or_first_page"}
    while expected and diagnostics["pagination_requests"] < request_limit:
        try:
            if sleeper:
                await sleeper(1.0)
            diagnostics["pagination_requests"] += 1
            response = first_response if not pages and first_response is not None else await get(expected)
            response.raise_for_status()
            page = decode_page(response.text, str(response.url), page=len(pages) + 1)
            if str(response.url) != expected:
                raise ValueError("Meta catalogue redirected to a different page")
            identity = page_identity(page['rows'])
            # The live catalogue repeats some legacy cards across pages.
            # Preserve every card receipt. Conflicting publication facts must
            # be resolved by the primary detail, not an arbitrary winning card;
            # unrelated cards/pages can still make progress.
            if identity in page_receipts:
                raise ValueError("Meta catalogue pagination repeated a whole page")
            if stage_cache:
                stage_cache.cache_save(STAGE, signature, {"scope":scope,"pages": [*pages, page],"failure_history":history})
            pages.append(page)
            page_receipts.add(identity)
            remember(page['rows'])
            expected = page["next_url"]
            diagnostics["pages_completed"] = len(pages)
        except Exception as exc:
            diagnostics["reason"] = type(exc).__name__
            history.append({"page":len(pages)+1,"reason":type(exc).__name__})
            if stage_cache:
                try:
                    stage_cache.cache_save(STAGE,signature,{"scope":scope,"pages":pages,"failure_history":history})
                except Exception:
                    diagnostics["failure_history_save_failed"] = True
            break
    cutoff_day = (research_now() - timedelta(days=lookback_days)).date()
    outside_conflicts = [url for url, variants in conflicts.items()
        if all(datetime.fromisoformat(row['published_at']).date() < cutoff_day for row in variants)]
    diagnostics.update(complete=not bool(expected), pagination_pending=int(bool(expected)), next_url=expected,
        records=len(seen), card_receipts=sum(len(page["rows"]) for page in pages),
        repeated_card_receipts=sum(len(page["rows"]) for page in pages) - len(seen),
        conflicting_publications=[{'url':url, 'variants':variants,
            'resolution':'outside_window_by_all_explicit_date_bounds' if url in outside_conflicts
                         else 'primary_title_and_publication_date_required'} for url, variants in conflicts.items()],
        outside_window_conflicting_publications=outside_conflicts)
    if expected and diagnostics["pagination_requests"] >= request_limit:
        diagnostics["reason"] = "catalogue_request_safety_limit"
    # Unknown date forces the normal primary-detail queue to verify the work;
    # it cannot be discarded using either inconsistent catalogue timestamp.
    return [{**row, 'published_at':''} if url in conflicts else row for url, row in seen.items()], diagnostics
