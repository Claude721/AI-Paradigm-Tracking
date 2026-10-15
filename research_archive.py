"""Private, append-only review provenance. Never consumed by research gates.

Requests are captured before the SDK call, visible answers before validation.
Human feedback lives in its own file and is never a model output target.
Plain JSON ZIPs are delivered only to the configured email recipients, not GitHub artifacts.
"""
from __future__ import annotations

import contextvars
import copy
from contextlib import contextmanager
import hashlib
import io
import json
import logging
import os
import re
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import config

SCHEMA_VERSION = 1
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
BUNDLE_MEMBER = "review-archive.json"
_HEX = re.compile(r"^[a-f0-9]{64}$")
_CALL = re.compile(r"^[a-f0-9]{32}$")
_context = contextvars.ContextVar("review_archive_call", default=None)
_bindings = contextvars.ContextVar("review_archive_cache_bindings", default={})
logger = logging.getLogger(__name__)


class ArchiveError(ValueError):
    """Fixed diagnostics only; never put source text or credentials in errors."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _invalid_constant(_value):
    raise ValueError("Non-finite JSON constant")


def _read(path):
    _no_symlinks(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _no_symlinks(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ArchiveError("Archive symlink is not allowed")


def _write(path, value):
    _no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.is_symlink():
        raise ArchiveError("Archive symlink is not allowed")
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sanitize(value):
    """Do not collect environment/config dumps, HTTP headers or private reasoning."""
    secrets = [str(item) for name in (
        "SUB_AGENT_LLM_API_KEY", "MAIN_AGENT_LLM_API_KEY", "OPENALEX_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY", "TAVILY_API_KEY", "GITHUB_TOKEN", "GH_TOKEN",
        "TWITTER_BEARER_TOKEN", "REDDIT_CLIENT_SECRET", "SMTP_PASSWORD",
        "SMTP_USERNAME",
    ) for item in (getattr(config, name, ""), os.getenv(name, "")) if item and len(str(item)) >= 6]

    def clean(item):
        if isinstance(item, dict):
            return {str(key): ("[REDACTED_SECRET]" if re.fullmatch(
                r"(?:[a-z0-9]+[_-])*(?:api[_-]?key|authorization|password|secret|access[_-]?token|refresh[_-]?token)", str(key), re.I
            ) else clean(part)) for key, part in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(part) for part in item]
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "[REDACTED_SECRET]").replace(quote(secret, safe=""), "[REDACTED_SECRET]")
            item = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[REDACTED_SECRET]@", item)
            item = re.sub(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED_SECRET]", item)
            return re.sub(r"(?i)([?&](?:api_key|access_token|token|secret|x-amz-signature|signature)=)[^&\s<>\"']+", r"\1[REDACTED_SECRET]", item)
        if item is None or type(item) in {bool, int, float}:
            return item
        raise ArchiveError("Archive payload must be JSON data")
    return clean(value)


def read_bundle(data):
    if len(data) > MAX_BUNDLE_BYTES * 2:
        raise ArchiveError("Review ZIP exceeds size limit")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            members = bundle.infolist()
            if len(members) != 1 or members[0].filename != BUNDLE_MEMBER or members[0].file_size > MAX_BUNDLE_BYTES:
                raise ArchiveError("Review ZIP must contain one bounded review-archive.json")
            payload = json.loads(bundle.read(members[0]), parse_constant=_invalid_constant)
    except (zipfile.BadZipFile, RuntimeError, UnicodeError, ValueError):
        raise ArchiveError("Invalid review ZIP or JSON; existing local data was not replaced") from None
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION or not isinstance(payload.get("files"), dict):
        raise ArchiveError("Unsupported archive schema")
    _validate_bundle(payload["files"])
    return payload["files"]


def _standards():
    from paradigms.rubric import load_rubric
    from runtime_provenance import runtime_provenance
    root = config.BASE_DIR
    skills = {str(path.relative_to(root)): path.read_text(encoding="utf-8")
              for path in sorted((root / "skills").glob("*/SKILL.md"))}
    settings = {name: value for name, value in vars(config).items()
                if name.startswith("PARADIGM_") and type(value) in {str, int, bool, float, list}}
    return sanitize({"schema_version": SCHEMA_VERSION, "rubric": load_rubric(),
        "skill_templates": skills, "research_settings": settings,
        "runtime": runtime_provenance(),
        "boundary": "Reference policies plus exact rendered messages in calls; not hidden provider instructions."})


def _origin_snapshot(evidence):
    return sanitize({key: value for key, value in {
        "fingerprint": evidence.fingerprint, "source_revision": evidence.source_revision,
        "source": evidence.source, "evidence_type": evidence.evidence_type.value,
        "title": evidence.title, "url": evidence.url, "published_at": evidence.published_at,
        "authors": evidence.authors, "organization": evidence.organization,
        "identifiers": evidence.identifiers, "summary": evidence.summary,
        "available_document_excerpt": evidence.raw.get("document_excerpt", ""),
        "origin_kind": evidence.raw.get("origin_kind", "research_paper"),
    }.items()})


def _identities(evidence):
    identities = []
    for namespace in ("arxiv", "doi", "openreview", "forum_post"):
        value = evidence.identifiers.get(namespace)
        if value:
            if namespace == "arxiv":
                value = re.sub(r"v\d+$", "", str(value))
            identities.append(namespace + ":" + (str(value).casefold() if namespace in {"arxiv", "doi"} else str(value)))
    for value in (evidence.raw.get("canonical_source_url"), evidence.url):
        if value:
            identities.append("url:" + str(value).rstrip("/"))
    if not identities:
        raise ArchiveError("Origin requires an explicit identifier or URL")
    return list(dict.fromkeys(sanitize(identities)))


class ArchiveStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        _no_symlinks(self.directory)
        if self.directory.is_symlink() or self.directory.resolve() in {config.BASE_DIR.resolve(), Path.home(), Path("/")}:
            raise ArchiveError("Archive requires a dedicated non-symlink directory")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        for folder in ("origins", "calls", "standards", "judgments"):
            _no_symlinks(self.directory / folder)
            (self.directory / folder).mkdir(mode=0o700, exist_ok=True)
        pending = self.directory / ".merge-pending"
        if pending.exists():
            files = _read(pending)
            # Local human edits always win, including edits made after an interrupted merge.
            feedback = self.directory / "feedback.json"
            if feedback.exists():
                current = _read(feedback)
                _validate_feedback(current)
                files["feedback.json"]["annotations"].update(current["annotations"])
            _validate_bundle(files)
            self._commit_merge(files)

    def register(self, evidence):
        identities = _identities(evidence)
        aliases_path = self.directory / "aliases.json"
        aliases = _read(aliases_path) if aliases_path.exists() else {"schema_version": 1, "identities": {}}
        known = {aliases["identities"][item] for item in identities if item in aliases["identities"]}
        if len(known) > 1:
            raise ArchiveError("Conflicting origin identities; human feedback requires explicit reconciliation")
        identity = identities[0]
        origin_id = next(iter(known)) if known else _digest(identity)
        snapshot = _origin_snapshot(evidence)
        version = _digest(snapshot)
        path = self.directory / "origins" / (origin_id + ".json")
        record = _read(path) if path.exists() else {
            "schema_version": SCHEMA_VERSION, "origin_id": origin_id, "identity": sanitize(identity),
            "versions": {}, "call_ids": [], "judgment_ids": [],
        }
        if version not in record["versions"]:
            record["versions"][version] = snapshot
            _write(path, record)
        # Read current feedback. NEVER rewrite a known origin's human fields.
        feedback_path = self.directory / "feedback.json"
        feedback = _read(feedback_path) if feedback_path.exists() else {"schema_version": SCHEMA_VERSION, "annotations": {}}
        if origin_id not in feedback["annotations"]:
            feedback["annotations"][origin_id] = {"title": snapshot["title"], "retain": None,
                "note": "", "revision": 0, "updated_at": None}
            _write(feedback_path, feedback)
        for item in identities:
            aliases["identities"][item] = origin_id
        _write(aliases_path, aliases)
        return {"origin_id": origin_id, "content_version": version, "source_revision": snapshot["source_revision"]}

    def references(self, context):
        from paradigms.models import ORIGIN_EVIDENCE_TYPES
        origins = list(context.get("origins", []))
        candidate = context.get("candidate")
        if candidate is not None:
            origins += [item for item in candidate.evidence if item.evidence_type in ORIGIN_EVIDENCE_TYPES]
        return list({ref["origin_id"] + ref["content_version"]: ref for ref in map(self.register, origins)}.values())

    def _link(self, references, field, object_id):
        for reference in references:
            path = self.directory / "origins" / (reference["origin_id"] + ".json")
            record = _read(path)
            if object_id not in record[field]:
                record[field].append(object_id)
                _write(path, record)

    def begin_call(self, *, run_id, stage, role, subject, request, context):
        references = self.references(context)
        standard = _standards()
        standard_id = _digest(standard)
        standard_path = self.directory / "standards" / (standard_id + ".json")
        if not standard_path.exists():
            _write(standard_path, standard)
        call_id = uuid.uuid4().hex
        signatures = list(context.get("cache_signatures", []))
        signatures += [_bindings.get()[item.fingerprint] for item in context.get("origins", []) if item.fingerprint in _bindings.get()]
        if stage in {"paradigm_synthesis", "paradigm_synthesis_repair"} and context.get("candidate") is not None:
            from database.paradigm_store import _deep_checkpoint_input_signature
            signatures.append(_deep_checkpoint_input_signature(context["candidate"]))
        # Capture only SDK model arguments, not client configuration or headers.
        retained = {name: request[name] for name in ("model", "messages", "temperature", "max_tokens", "max_completion_tokens", "response_format", "extra_body") if name in request}
        safe_request = sanitize(retained)
        from agents.llm_utils import resolve_model, request_policy
        resolved = resolve_model(role)
        record = {"schema_version": SCHEMA_VERSION, "call_id": call_id, "run_id": run_id,
            "stage": stage, "role": role, "subject": sanitize(subject), "origin_refs": references,
            "candidate_key": getattr(context.get("candidate"), "key", ""),
            "mechanism_id": context.get("mechanism_id", ""),
            "cache_signatures": sorted(set(signatures)),
            "standard_id": standard_id, "started_at": _now(), "completed_at": None,
            "provider": resolved.provider, "base_url": sanitize(resolved.base_url),
            "request": safe_request, "request_digest": _digest(safe_request),
            "redacted": safe_request != retained,
            "capture_boundary": "exact application messages; transport parameter policy recorded separately",
            "transport_policy": sanitize(request_policy(resolved)),
            "transport_status": "started", "response": None, "validation": {"status": "not_recorded"}}
        _write(self.directory / "calls" / (call_id + ".json"), record)
        self._link(references, "call_ids", call_id)
        return call_id

    def finish_call(self, call_id, *, response=None, error_type="", cancelled=False):
        path = self.directory / "calls" / (call_id + ".json")
        record = _read(path)
        record["completed_at"] = _now()
        record["transport_status"] = "cancelled_usage_unknown" if cancelled else "failed" if error_type else "returned"
        record["error_type"] = error_type
        if response is not None:
            choice = (getattr(response, "choices", None) or [None])[0]
            content = getattr(getattr(choice, "message", None), "content", None)
            if content is not None and not isinstance(content, str):
                content = None
            safe_content = sanitize(content)
            try:
                parsed = json.loads(safe_content, parse_constant=_invalid_constant) if safe_content else None
            except ValueError:
                parsed = None  # Preserve exact visible text, including invalid JSON.
            usage = getattr(response, "usage", None)
            record["response"] = {"content": safe_content, "parsed_json": parsed,
                "finish_reason": getattr(choice, "finish_reason", None),
                "usage": {name: getattr(usage, name, None) for name in ("prompt_tokens", "completion_tokens", "total_tokens")},
                "private_reasoning_retained": False}
            record["redacted"] = record["redacted"] or safe_content != content
        _write(path, record)

    def validation(self, call_id, error=None):
        path = self.directory / "calls" / (call_id + ".json")
        record = _read(path)
        if record["validation"]["status"] != "not_recorded":
            return
        record["validation"] = {"status": "failed" if error else "passed",
            "error_type": type(error).__name__ if isinstance(error, Exception) else "reported_failure" if error else ""}
        _write(path, record)

    def judgment(self, *, run_id, stage, context, result, cache_signature=""):
        # Serialization belongs inside the isolated retention operation.
        result = result() if callable(result) else result
        references = self.references(context)
        judgment_id = uuid.uuid4().hex
        record = {"schema_version": SCHEMA_VERSION, "judgment_id": judgment_id, "run_id": run_id,
            "stage": stage, "created_at": _now(), "origin_refs": references,
            "candidate_key": getattr(context.get("candidate"), "key", ""),
            "cache_signature": cache_signature, "result": sanitize(result),
            "actual_request_ids": []}
        for reference in references:
            origin = _read(self.directory / "origins" / (reference["origin_id"] + ".json"))
            for call_id in origin["call_ids"]:
                call = _read(self.directory / "calls" / (call_id + ".json"))
                exact_cache = cache_signature and cache_signature in call.get("cache_signatures", [])
                fresh_candidate = (not cache_signature and context.get("candidate") is not None
                    and call["run_id"] == run_id and call["candidate_key"] == record["candidate_key"])
                current = _context.get()
                fresh_extraction = (stage in {"extraction_validated", "origin_eligibility_validated"} and current and current["call_id"] == call_id)
                if exact_cache or fresh_candidate or fresh_extraction:
                    record["actual_request_ids"].append(call_id)
        record["actual_request_ids"] = sorted(set(record["actual_request_ids"]))
        record["input_provenance"] = "archived_request" if record["actual_request_ids"] else "no_exact_cached_request_available" if cache_signature else "deterministic_snapshot; consult linked origin reviews"
        _write(self.directory / "judgments" / (judgment_id + ".json"), record)
        self._link(references, "judgment_ids", judgment_id)

    def files(self):
        files = {}
        for path in sorted(self.directory.rglob("*.json")):
            relative = str(path.relative_to(self.directory))
            _validate_name(relative)
            files[relative] = _read(path)
        _validate_bundle(files)
        return files

    def export(self, destination, *, run_prefix=""):
        files = self.files()
        if run_prefix:
            files = _select_run(files, run_prefix)
        raw = json.dumps({"schema_version": SCHEMA_VERSION, "files": files}, ensure_ascii=False, allow_nan=False).encode()
        if len(raw) > MAX_BUNDLE_BYTES:
            raise ArchiveError("Review JSON exceeds size limit; do not truncate reviewed materials")
        destination = Path(destination)
        _no_symlinks(destination)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(destination, "xb") as handle:
            os.chmod(destination, 0o600)
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr(BUNDLE_MEMBER, raw)
        return files

    def merge(self, data):
        """Merge mailed runs locally; immutable model data and local feedback win."""
        incoming, files = read_bundle(data), self.files()
        aliases = files.setdefault("aliases.json", {"schema_version": 1, "identities": {}})["identities"]
        feedback = files.setdefault("feedback.json", {"schema_version": 1, "annotations": {}})["annotations"]
        remap = {}
        incoming_aliases = incoming.get("aliases.json", {}).get("identities", {})
        for name, row in incoming.items():
            if not name.startswith("origins/"):
                continue
            origin_id = row["origin_id"]
            identities = [key for key, target in incoming_aliases.items() if target == origin_id] + [row["identity"]]
            known = {aliases[key] for key in identities if key in aliases}
            if "origins/" + origin_id + ".json" in files:
                known.add(origin_id)
            if len(known) > 1:
                raise ArchiveError("Conflicting local origin identities; reconcile explicitly, never overwrite feedback")
            target = next(iter(known)) if known else origin_id
            remap[origin_id] = target
            prior = files.setdefault("origins/" + target + ".json", {**copy.deepcopy(row), "origin_id": target})
            prior["versions"].update(row["versions"])
            for field in ("call_ids", "judgment_ids"):
                prior[field] = list(dict.fromkeys([*prior[field], *row[field]]))
            if target not in feedback:
                feedback[target] = copy.deepcopy(incoming.get("feedback.json", {}).get("annotations", {}).get(origin_id,
                    {"title": next(iter(row["versions"].values()))["title"], "retain": None, "note": "", "revision": 0, "updated_at": None}))
            for identity in identities:
                aliases[identity] = target
        for name, value in incoming.items():
            if name.startswith(("calls/", "judgments/", "standards/")):
                value = copy.deepcopy(value)
                for ref in value.get("origin_refs", []):
                    if remap[ref["origin_id"]] != ref["origin_id"]:
                        ref.setdefault("imported_origin_id", ref["origin_id"])
                        ref["origin_id"] = remap[ref["origin_id"]]
                if name in files and files[name] != value:
                    raise ArchiveError("Conflicting immutable review record; existing model judgment was not replaced")
                files[name] = value
        runs = files.setdefault("capture-status.json", {"schema_version": 1, "runs": {}})["runs"]
        for run_id, row in incoming.get("capture-status.json", {}).get("runs", {}).items():
            if run_id in runs:
                row = {"errors": [dict(items) for items in dict.fromkeys(tuple(sorted(item.items()))
                    for item in [*runs[run_id]["errors"], *row["errors"]])],
                    "updated_at": max(runs[run_id]["updated_at"], row["updated_at"])}
            runs[run_id] = row
        _validate_bundle(files)
        _write(self.directory / ".merge-pending", files)
        self._commit_merge(files)

    def _commit_merge(self, files):
        for name, value in files.items():
            if name != "feedback.json":
                _write(self.directory / name, value)
        _write(self.directory / "feedback.json", files["feedback.json"])
        (self.directory / ".merge-pending").unlink()

    def import_feedback(self, payload):
        _validate_feedback(payload)
        path = self.directory / "feedback.json"
        current = _read(path)
        # Whole-patch validation before any write; stale imports never win.
        for origin_id, incoming in payload["annotations"].items():
            prior = current["annotations"].get(origin_id)
            if prior is None:
                raise ArchiveError("Feedback refers to an unknown origin")
            same = all(incoming.get(key) == prior.get(key) for key in ("retain", "note"))
            if not same and incoming["revision"] != prior["revision"]:
                raise ArchiveError("Stale feedback revision; export the latest feedback before editing")
        for origin_id, incoming in payload["annotations"].items():
            prior = current["annotations"][origin_id]
            if any(incoming.get(key) != prior.get(key) for key in ("retain", "note")):
                prior.update(retain=incoming["retain"], note=incoming["note"], revision=prior["revision"] + 1, updated_at=_now())
        _write(path, current)


def _validate_name(name):
    if name in {"feedback.json", "capture-status.json", "aliases.json"}:
        return
    parts = name.split("/")
    if len(parts) != 2 or parts[0] not in {"origins", "calls", "standards", "judgments"} or not parts[1].endswith(".json"):
        raise ArchiveError("Invalid archive member path")
    pattern = _HEX if parts[0] in {"origins", "standards"} else _CALL
    if not pattern.fullmatch(parts[1][:-5]):
        raise ArchiveError("Invalid archive member identity")


def _validate_feedback(value):
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION or not isinstance(value.get("annotations"), dict):
        raise ArchiveError("Invalid feedback JSON schema")
    for origin_id, row in value["annotations"].items():
        if (not isinstance(origin_id, str) or not _HEX.fullmatch(origin_id) or not isinstance(row, dict)
            or "retain" not in row
            or row.get("retain") is not None and type(row.get("retain")) is not bool
            or not isinstance(row.get("note"), str)
            or type(row.get("revision")) is not int or row["revision"] < 0):
            raise ArchiveError("Invalid human feedback; retain must be true/false/null with note and revision")


def _validate_bundle(files):
    if sanitize(files) != files:
        raise ArchiveError("Archive contains credentials; remove them before exporting or importing")
    for name, value in files.items():
        _validate_name(name)
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise ArchiveError("Invalid archive JSON schema")
        kind, _, filename = name.partition("/")
        key = filename.removesuffix(".json")
        if kind == "standards" and _digest(value) != key:
            raise ArchiveError("Research standard checksum mismatch")
        if kind == "origins":
            if value.get("origin_id") != key or not isinstance(value.get("versions"), dict):
                raise ArchiveError("Origin identity mismatch")
            if any(_digest(snapshot) != version for version, snapshot in value["versions"].items()):
                raise ArchiveError("Origin content checksum mismatch")
        if kind in {"calls", "judgments"}:
            field = "call_id" if kind == "calls" else "judgment_id"
            if value.get(field) != key:
                raise ArchiveError("Review identity mismatch")
            for ref in value.get("origin_refs", []):
                origin = files.get("origins/" + ref["origin_id"] + ".json", {})
                if ref["content_version"] not in origin.get("versions", {}):
                    raise ArchiveError("Review refers to missing origin content")
            if kind == "calls":
                if _digest(value.get("request")) != value.get("request_digest") or "standards/" + value.get("standard_id", "") + ".json" not in files:
                    raise ArchiveError("Review request or standard checksum mismatch")
            elif any("calls/" + call_id + ".json" not in files for call_id in value.get("actual_request_ids", [])):
                raise ArchiveError("Judgment refers to missing request")
    feedback = files.get("feedback.json", {"schema_version": 1, "annotations": {}})
    _validate_feedback(feedback)
    for origin_id in feedback["annotations"]:
        if "origins/" + origin_id + ".json" not in files:
            raise ArchiveError("Feedback refers to missing origin")
    for origin_id in files.get("aliases.json", {}).get("identities", {}).values():
        if "origins/" + origin_id + ".json" not in files:
            raise ArchiveError("Identity alias refers to missing origin")
    for name, origin in files.items():
        if name.startswith("origins/"):
            for field, folder in (("call_ids", "calls"), ("judgment_ids", "judgments")):
                if any(folder + "/" + key + ".json" not in files for key in origin.get(field, [])):
                    raise ArchiveError("Origin index refers to missing review")


def _select_run(files, prefix):
    selected = {name for name, row in files.items() if name.startswith(("calls/", "judgments/"))
                and row.get("run_id", "").startswith(prefix)}
    for name in list(selected):
        selected.update("calls/" + key + ".json" for key in files[name].get("actual_request_ids", []))
    origin_ids = {ref["origin_id"] for name in selected for ref in files[name].get("origin_refs", [])}
    standard_ids = {files[name]["standard_id"] for name in selected if name.startswith("calls/")}
    selected.update("standards/" + key + ".json" for key in standard_ids)
    result = {name: copy.deepcopy(files[name]) for name in selected}
    for origin_id in origin_ids:
        name = "origins/" + origin_id + ".json"
        result[name] = copy.deepcopy(files[name])
        versions = {ref["content_version"] for key in selected for ref in files[key].get("origin_refs", [])
                    if ref["origin_id"] == origin_id}
        result[name]["versions"] = {key: row for key, row in result[name]["versions"].items() if key in versions}
        for field, folder in (("call_ids", "calls"), ("judgment_ids", "judgments")):
            result[name][field] = [key for key in result[name][field] if folder + "/" + key + ".json" in selected]
    result["feedback.json"] = {"schema_version": 1, "annotations": {key: row for key, row in files.get("feedback.json", {}).get("annotations", {}).items() if key in origin_ids}}
    result["aliases.json"] = {"schema_version": 1, "identities": {key: target for key, target in files.get("aliases.json", {}).get("identities", {}).items() if target in origin_ids}}
    result["capture-status.json"] = {"schema_version": 1, "runs": {key: row for key, row in files.get("capture-status.json", {}).get("runs", {}).items() if key.startswith(prefix)}}
    _validate_bundle(result)
    return result


def restore_bundle(data, directory):
    files = read_bundle(data)
    directory = Path(directory)
    _no_symlinks(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ArchiveError("Restore requires an empty directory; never overwrite local human feedback")
    store = ArchiveStore(directory)
    for name, value in files.items():
        _write(directory / name, value)
    return store


class ArchiveRecorder:
    def __init__(self):
        self.reset()

    def reset(self):
        self.run_id = ":".join([os.getenv("GITHUB_RUN_ID", "local"), os.getenv("GITHUB_RUN_ATTEMPT", "1"), uuid.uuid4().hex])
        self._store = None
        self.errors = []
        _context.set(None)

    def _operation(self, method, *args, **kwargs):
        if not config.RESEARCH_ARCHIVE_ENABLED:
            return None
        try:
            if self._store is None:
                self._store = ArchiveStore(config.RESEARCH_ARCHIVE_DIR)
            return getattr(self._store, method)(*args, **kwargs)
        except Exception as exc:
            self.errors.append({"operation": method, "error_type": type(exc).__name__})
            logger.warning("Private review retention failed [%s/%s]; research semantics unchanged", method, type(exc).__name__)
            self.checkpoint()
            return None

    def begin(self, stage, role, subject, request, context):
        _context.set(None)
        if not context:
            return None  # Smoke/legacy/report calls not bound to origins are excluded.
        call_id = self._operation("begin_call", run_id=self.run_id, stage=stage, role=role, subject=subject, request=request, context=context)
        if call_id:
            _context.set({"call_id": call_id, "stage": stage, "role": role, "response_id": None, "error_id": None})
        return call_id

    def finish(self, call_id, response=None, error=None, cancelled=False):
        if call_id:
            current = _context.get()
            if current:
                current.update(response_id=id(response) if response is not None else None, error_id=id(error) if error is not None else None)
            self._operation("finish_call", call_id, response=response, error_type=type(error).__name__ if error is not None else "", cancelled=cancelled)

    def validate(self, stage, role, response, error):
        current = _context.get()
        if current and current["stage"] == stage and current["role"] == role and (
            response is not None and current["response_id"] == id(response)
            or isinstance(error, Exception) and current["error_id"] == id(error)
        ):
            self._operation("validation", current["call_id"], error)

    def judgment(self, stage, context, result, cache_signature=""):
        self._operation("judgment", run_id=self.run_id, stage=stage, context=context, result=result, cache_signature=cache_signature)

    def checkpoint(self):
        if config.RESEARCH_ARCHIVE_ENABLED:
            try:
                path = Path(config.RESEARCH_ARCHIVE_DIR) / "capture-status.json"
                status = _read(path) if path.exists() else {"schema_version": SCHEMA_VERSION, "runs": {}}
                status["runs"][self.run_id] = {"errors": self.errors, "updated_at": _now()}
                _write(path, status)
            except Exception:
                logger.warning("Private retention status checkpoint failed")

    def extraction(self, extraction):
        def result():
            from dataclasses import asdict
            decision = extraction.rubric_assessment.get("decision")
            return {"system_retention": True if decision == "deep_dive" else False if decision in {"observe", "reject"} else None,
                    "normalized_extraction": asdict(extraction)}
        self.judgment("extraction_validated", {"origins": [extraction.evidence]}, result)

    def eligibility(self, origins, decisions):
        for item in origins:
            self.judgment("origin_eligibility_validated", {"origins": [item]},
                lambda: {"decision": decisions[item.fingerprint][0], "reason": decisions[item.fingerprint][1]}
                if item.fingerprint in decisions else {"decision": None, "status": "missing_or_invalid_output"})

    @contextmanager
    def bind_cache(self, bindings):
        token = _bindings.set(bindings)
        try:
            yield
        finally:
            _bindings.reset(token)


review_archive = ArchiveRecorder()
