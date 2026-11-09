"""Export published reports and their frozen evidence without research or mail."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import zipfile

SCHEMA = "paradigm-weekly-delivery/1"
CANDIDATE_FIELDS = ("key", "name", "mechanism", "problem_shift", "route_family",
                    "lineage_parent", "lineage_path", "status", "report_kind",
                    "mechanism_origin_support")
EVIDENCE_FIELDS = ("source", "evidence_type", "title", "summary", "url", "published_at",
                   "authors", "identifiers", "metrics", "source_revision")
RAW_FIELDS = ("publication_date_precision", "metric_delta_observed_at", "indexed_discovery_only",
              "association_status", "relationship", "independence")
STAT_FIELDS = ("coverage_incomplete", "research_incomplete", "reference_time", "observation_time",
               "execution_complete", "delivery_contract_version", "research_health")


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def project_candidate(raw):
    value = {key: raw[key] for key in CANDIDATE_FIELDS if key in raw}
    value["evidence"] = []
    for evidence in raw.get("evidence", []):
        item = {key: evidence[key] for key in EVIDENCE_FIELDS if key in evidence}
        item["raw"] = {key: evidence["raw"][key] for key in RAW_FIELDS if key in (evidence.get("raw") or {})}
        value["evidence"].append(item)
    value["community_search_receipts"] = {}
    value["excluded_evidence"] = [{} for _ in raw.get("excluded_evidence", [])]
    for platform, receipt in (raw.get("community_search_receipts") or {}).items():
        if not isinstance(receipt, dict):
            continue
        item = {key: receipt[key] for key in ("status", "result_count", "input_signature") if key in receipt}
        item["failure_history"] = [{} for _ in receipt.get("failure_history", [])]
        value["community_search_receipts"][platform] = item
    return value


def export(database, output, delivery_key=None, source_meta=None, report_body=None):
    database = Path(database).resolve()
    if not database.is_file():
        raise ValueError("Source state database is missing")
    for suffix in ("-wal", "-journal"):
        journal = Path(str(database) + suffix)
        if journal.exists() and journal.stat().st_size:
            raise ValueError("Use a checkpointed source database")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        query = "SELECT * FROM report_outbox WHERE status='delivered'"
        params = ()
        if delivery_key is not None:
            query += " AND delivery_key=?"
            params = (delivery_key,)
        rows = conn.execute(query + " ORDER BY created_at,delivery_key", params).fetchall()
    source_meta = source_meta or {}
    files = {}
    manifest = {"schemaVersion": SCHEMA, "source": {
        "project": "AI Paradigm Tracking", "stateRunId": source_meta.get("run_id") or os.getenv("GITHUB_RUN_ID", "local"),
        "stateCommitSha": source_meta.get("commit_sha") or os.getenv("GITHUB_SHA", "local"),
    }, "exporter": {"runId": os.getenv("GITHUB_RUN_ID", "local"), "commitSha": os.getenv("GITHUB_SHA", "local"),
                   "adapterSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "deliveries": [], "populationComplete": False}
    for row in rows:
        key = row["delivery_key"]
        stem = "deliveries/" + hashlib.sha256(key.encode()).hexdigest()
        data = {"deliveryKey": key, "reportDate": row["report_date"], "snapshotCreatedAt": row["created_at"],
        "deliveredAt": row["delivered_at"] if "delivered_at" in row.keys() else None, "deliveryStatus": "delivered",
                "candidates": [project_candidate(candidate) for candidate in json.loads(row["candidate_payload_json"])],
                "stats": {k: v for k, v in json.loads(row["stats_json"]).items() if k in STAT_FIELDS}}
        body = row["report_content"] or ""
        binding = "retained-outbox" if body.strip() else "missing"
        if not body.strip() and report_body is not None and delivery_key == key:
            body = report_body
            binding = "same-run-delivery-receipt" if body.strip() else "missing"
        entry = {"deliveryKey": key, "reportDate": row["report_date"], "reportBinding": binding,
                 "data": stem + "/evidence.json", "report": stem + "/report.md" if body.strip() else None}
        files[entry["data"]] = encode(data)
        if entry["report"]:
            files[entry["report"]] = body.encode("utf-8")
        manifest["deliveries"].append(entry)
    manifest["files"] = [{"path": path, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
                         for path, body in sorted(files.items())]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", encode(manifest))
        for path, body in sorted(files.items()):
            bundle.writestr(path, body)
    return manifest


def restore(bundle, output):
    """Validate a local handoff bundle and adapt it to the existing bridge."""
    infos = bundle.infolist()
    names = [entry.filename for entry in infos if not entry.is_dir()]
    if len(names) != len(set(names)) or sum(entry.file_size for entry in infos) > 256 * 1024 * 1024:
        raise ValueError("Duplicate or oversized delivery archive")
    manifest = json.loads(bundle.read("manifest.json"))
    if manifest.get("schemaVersion") != SCHEMA:
        raise ValueError("Unknown weekly delivery contract")
    files = {}
    for item in manifest["files"]:
        path = item["path"]
        if path in files:
            raise ValueError("Duplicate delivery manifest file")
        raw = bundle.read(path)
        if len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("Delivery file hash mismatch")
        files[path] = raw
    expected = set()
    values = []
    bindings = {}
    for entry in manifest["deliveries"]:
        key = entry["deliveryKey"]
        if key in bindings:
            raise ValueError("Duplicate delivery identity")
        stem = "deliveries/" + hashlib.sha256(key.encode()).hexdigest()
        if entry["data"] != stem + "/evidence.json" or entry["report"] not in (None, stem + "/report.md"):
            raise ValueError("Unexpected delivery file layout")
        if entry["reportBinding"] not in ("retained-outbox", "same-run-delivery-receipt", "missing"):
            raise ValueError("Unknown report binding provenance")
        if bool(entry["report"]) == (entry["reportBinding"] == "missing"):
            raise ValueError("Report body and binding status differ")
        expected.add(entry["data"])
        data = json.loads(files[entry["data"]])
        if data["deliveryKey"] != key or data["reportDate"] != entry["reportDate"] or data["deliveryStatus"] != "delivered":
            raise ValueError("Delivery data and manifest identity differ")
        body = ""
        if entry["report"]:
            expected.add(entry["report"])
            body = files[entry["report"]].decode("utf-8")
            if not body.strip():
                raise ValueError("Delivery report is empty")
        bindings[key] = entry["reportBinding"]
        values.append((key, data["reportDate"], "delivered", json.dumps(data["candidates"], ensure_ascii=False),
                       json.dumps(data["stats"], ensure_ascii=False), data["snapshotCreatedAt"], body))
    if set(files) != expected or set(names) != expected | {"manifest.json"}:
        raise ValueError("Delivery archive contains unreferenced files")
    with sqlite3.connect(output / "paradigm_radar.db") as conn:
        conn.execute("CREATE TABLE report_outbox (delivery_key TEXT PRIMARY KEY, report_date, status, "
                     "candidate_payload_json, stats_json, created_at, report_content)")
        conn.executemany("INSERT INTO report_outbox VALUES (?,?,?,?,?,?,?)", values)
    meta = {"run_id": str(manifest["source"]["stateRunId"]),
            "commit_sha": manifest["source"]["stateCommitSha"], "_format": SCHEMA,
            "_report_bindings": bindings}
    (output / "state-meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta
