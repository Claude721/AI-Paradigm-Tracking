"""Bounded, resumable identity lookup, not a substitute for primary reading."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from urllib.parse import parse_qsl, urlsplit

import config
from runtime_clock import research_now


def normalized_title(value):
    return " ".join(re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", value).casefold()).split())


def author_aligned(author_text, names):
    tokens = normalized_title(author_text).split()
    for name in names:
        parts = normalized_title(name).split()
        if len(parts) < 2:
            continue
        if " ".join(parts) in " ".join(tokens):
            return True
        for index, token in enumerate(tokens):
            if token != parts[-1]:
                continue
            neighbors = tokens[max(0, index - 1):index] + tokens[index + 1:index + 2]
            if any(value == parts[0] or (len(value) == 1 and value == parts[0][0]) for value in neighbors):
                return True
    return False


def primary_url(value):
    if not isinstance(value, str):
        return ""
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.hostname not in {"arxiv.org", "doi.org"}
            or any(key.casefold() in {"expires", "signature"} or key.casefold().startswith("x-amz-")
                   for key, _ in parse_qsl(parsed.query))):
        return ""
    if parsed.hostname == "arxiv.org" and re.fullmatch(r"/abs/\d{4}\.\d{4,5}(?:v\d+)?", parsed.path):
        return value
    if parsed.hostname == "doi.org" and re.fullmatch(r"/10\.\d{4,9}/\S+", parsed.path):
        return value
    return ""


def match_work(record, payload):
    if (not isinstance(payload, dict) or not isinstance(payload.get("results"), list)
            or not isinstance(payload.get("meta"), dict)
            or type(payload["meta"].get("count")) is not int
            or payload["meta"]["count"] < len(payload["results"])):
        raise ValueError("Invalid bibliography search response")
    if payload["meta"]["count"] > len(payload["results"]):
        return {"status": "pending", "reason": "identity_search_truncated"}
    matches = {work.get("id"): work for work in payload["results"]
               if isinstance(work, dict) and isinstance(work.get("display_name"), str)
               and normalized_title(work["display_name"]) == normalized_title(record["title"])}
    if len(matches) != 1:
        return {"status": "pending", "reason": "ambiguous_identity" if matches else "identity_not_found"}
    work = next(iter(matches.values()))
    if not re.fullmatch(r"https://openalex.org/W\d+", str(work.get("id", ""))):
        return {"status": "pending", "reason": "invalid_work_identity"}
    if work.get("publication_year") != record["citation_year"]:
        return {"status": "pending", "reason": "citation_year_mismatch"}
    # Exact title alone is insufficient: corroborate a named citation author.
    names = [str((item.get("author") or {}).get("display_name", ""))
             for item in work.get("authorships", []) if isinstance(item, dict)]
    if not author_aligned(record.get("author_text", ""), names):
        return {"status": "pending", "reason": "author_identity_not_aligned"}
    locations = [work.get("primary_location") or {}, *(work.get("locations") or [])]
    urls = [primary_url(item.get("landing_page_url")) for item in locations if isinstance(item, dict)]
    urls.append(primary_url(work.get("doi")))
    url = next((value for value in urls if value), "")
    if not url:
        return {"status": "pending", "reason": "no_explicit_primary_url"}
    return {"status": "resolved_url", "url": url, "work_id": work["id"],
            "reason": "primary_date_and_content_verification_pending"}


class BibliographyResolver:
    def __init__(self, *, stage_cache=None, enabled=False, max_requests=8):
        self.stage_cache = stage_cache
        self.enabled = enabled
        self.max_requests = max_requests
        self.requests_used = 0

    def signature(self, index_url, lookback_days, key):
        return hashlib.sha256(json.dumps([1, index_url, lookback_days,
            research_now().isoformat(), key], ensure_ascii=False).encode()).hexdigest()

    async def resolve(self, client, index_url, records, *, lookback_days, explicit_records=()):
        manifest_key = self.signature(index_url, lookback_days, "manifest")
        old = self.stage_cache.cache_get("bibliography_manifest", manifest_key) if self.stage_cache else None
        if old is not None and not isinstance(old, dict):
            raise ValueError("Invalid bibliography manifest")
        manifest = {} if old is None else old.get("records")
        if not isinstance(manifest, dict):
            raise ValueError("Invalid bibliography manifest")
        manifest.update({record["key"]: record for record in records})
        # A parser repair may recover an explicit ID for a historically pending
        # citation. Rebind that existing task, never delete it or synthesize an
        # OpenAlex identity. New direct-ID citations use the normal detail queue.
        for record in explicit_records:
            if record.get("key") in manifest:
                if not primary_url(record.get("explicit_url")):
                    raise ValueError("Invalid explicit bibliography identifier")
                manifest[record["key"]] = dict(record)
        for key, record in manifest.items():
            if (not isinstance(record, dict) or record.get("key") != key
                    or not isinstance(record.get("title"), str)
                    or not isinstance(record.get("author_text"), str)
                    or type(record.get("citation_year")) is not int):
                raise ValueError("Invalid bibliography record identity")
        if self.stage_cache:
            self.stage_cache.cache_save("bibliography_manifest", manifest_key, {"records": manifest})
        rows = []
        circuit_open = False
        cached_results = {}
        for key in manifest:
            signature = self.signature(index_url, lookback_days, key)
            cached = self.stage_cache.cache_get("bibliography_identity", signature) if self.stage_cache else None
            if cached is not None and (not isinstance(cached, dict)
                    or type(cached.get("attempt_count", 0)) is not int or cached.get("attempt_count", 0) < 0):
                raise ValueError("Invalid bibliography lookup checkpoint")
            cached_results[key] = cached
        # A permanently ambiguous first record must not consume every run's
        # allowance before later records get their first lookup.
        ordered = sorted(manifest, key=lambda key: (cached_results[key] or {}).get("attempt_count", 0))
        for key in ordered:
            record = manifest[key]
            signature = self.signature(index_url, lookback_days, key)
            cached = cached_results[key]
            attempts = (cached or {}).get("attempt_count", 0)
            explicit = primary_url(record.get("explicit_url"))
            if explicit and not (cached and cached.get("status") == "primary_verified" and cached.get("url") == explicit):
                result = {"status": "resolved_url", "url": explicit,
                    "identity_basis": "explicit_source_identifier",
                    "reason": "primary_date_and_content_verification_pending"}
            elif cached is not None and cached.get("status") in {"resolved_url", "primary_verified"}:
                known_work = re.fullmatch(r"https://openalex.org/W\d+", str(cached.get("work_id", "")))
                explicit_identity = (cached.get("identity_basis") == "explicit_source_identifier"
                                     and explicit and cached.get("url") == explicit)
                if not primary_url(cached.get("url")) or not (known_work or explicit_identity):
                    raise ValueError("Invalid bibliography identity checkpoint")
                result = dict(cached)
                # A primary receipt is not reset merely by restarting. The
                # official adapter pairs it with the current-policy detail
                # checkpoint before trusting coverage or reusing its body.
            elif not self.enabled:
                result = {"status": "pending", "reason": "resolution_disabled"}
            elif not config.OPENALEX_API_KEY:
                result = {"status": "pending", "reason": "openalex_not_configured"}
            elif circuit_open:
                result = {"status": "pending", "reason": "provider_circuit_open"}
            elif self.requests_used >= self.max_requests:
                result = {"status": "pending", "reason": "identity_request_safety_limit"}
            elif not record.get("title") or not record.get("author_text"):
                result = {"status": "pending", "reason": "missing_citation_identity"}
            else:
                self.requests_used += 1
                attempts += 1
                result = {"status": "pending", "reason": "query_interrupted", "attempt_count": attempts}
                self.save(signature, result)
                response = None
                try:
                    response = await client.get("https://api.openalex.org/works", params={
                        "search": '"' + record["title"] + '"',
                        "per-page": 100, "select": "id,display_name,publication_year,authorships,doi,primary_location,locations"},
                        headers={"Authorization": "Bearer " + config.OPENALEX_API_KEY}, follow_redirects=False)
                    if response.status_code in {401, 403, 429}:
                        circuit_open = True
                    response.raise_for_status()
                    if len(response.content) > 2_000_000:
                        raise ValueError("Bibliography response exceeds size contract")
                    result = match_work(record, response.json())
                except Exception as exc:
                    result = {"status": "pending", "reason": f"http_{response.status_code}" if response is not None and response.status_code >= 400 else type(exc).__name__}
            result["attempt_count"] = attempts
            self.save(signature, result)
            rows.append({**record, **result, "input_signature": signature})
        return rows

    def save(self, signature, result):
        if self.stage_cache:
            self.stage_cache.cache_save("bibliography_identity", signature, result)

    def require_primary(self, row):
        if row.get("status") == "primary_verified":
            result = {key: row[key] for key in ("url", "work_id", "identity_basis", "attempt_count") if key in row}
            result.update(status="resolved_url", reason="primary_date_and_content_verification_pending")
            self.save(row["input_signature"], result)
            row.pop("primary_published_at", None)
            row.pop("primary_detail_contract", None)
            row.update(result)

    def verified(self, row, published_at, *, detail_contract):
        result = {key: row[key] for key in ("url", "work_id", "identity_basis", "attempt_count") if key in row}
        result.update(status="primary_verified", reason="primary_metadata_verified",
                      primary_published_at=published_at, primary_detail_contract=detail_contract)
        # The adapter has already durably saved the primary detail. Expose the
        # upstream acknowledgement only after this receipt save also succeeds.
        self.save(row["input_signature"], result)
        row.update(result)
