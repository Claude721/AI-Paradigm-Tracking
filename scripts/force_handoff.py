#!/usr/bin/env python3
"""Forward frozen deliveries to the force project's authenticated backend."""
import argparse
from contextlib import closing
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import zipfile

from force_delivery_contract import project_candidate, STAT_FIELDS


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch_sources(root, all_runs=False, skip_runs=()):
    from force_artifacts import GitHubArtifacts
    client = GitHubArtifacts(os.environ.get("GITHUB_REPOSITORY", "Claude721/AI-Paradigm-Tracking"))
    root.mkdir(parents=True, exist_ok=True)
    folders = []
    for run in reversed(client.runs(8)):
        if str(run["id"]) in skip_runs:
            continue
        artifacts = [item for item in client.artifacts(run["id"]) if not item["expired"]]
        names = {"state": f"paradigm-radar-v0-state-{run['id']}", "report": f"paradigm-radar-report-{run['id']}",
                 "audit": f"paradigm-radar-audit-{run['id']}"}
        selected = {kind: next((item for item in artifacts if item["name"] == name), None) for kind, name in names.items()}
        if not all(selected.values()):
            continue
        folder = Path(tempfile.mkdtemp(prefix="source-", dir=root))
        for kind, artifact in selected.items():
            archive = folder / (kind + ".zip")
            client.download(artifact, archive)
            with zipfile.ZipFile(archive) as bundle:
                seen = set()
                for item in bundle.infolist():
                    name = Path(item.filename).name
                    allowed = (kind == "state" and name in ("paradigm_radar.db", "state-meta.json") or
                               kind == "report" and re.fullmatch(r"paradigm_radar_\d{4}-\d{2}-\d{2}\.md", name) or
                               kind == "audit" and name == "run_audit_latest.json")
                    if not allowed or item.is_dir():
                        continue
                    if name in seen or item.file_size > 256 * 1024 * 1024:
                        raise ValueError("Ambiguous or oversized source member")
                    seen.add(name)
                    with bundle.open(item) as source, (folder / name).open("xb") as target:
                        shutil.copyfileobj(source, target)
        meta = json.loads((folder / "state-meta.json").read_text())
        if str(meta["run_id"]) != str(run["id"]) or meta["commit_sha"] != run["head_sha"]:
            raise ValueError("Fetched state provenance differs from its workflow run")
        folders.append(folder)
        if not all_runs:
            break
    return folders


def call_backend(url, token, path, payload=None):
    if urlsplit(url).scheme != "https":
        raise ValueError("Backend URL must use HTTPS")
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if payload is not None else None
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(3):
        try:
            request = urllib.request.Request(url.rstrip("/") + path, data=raw,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "User-Agent": "AI-Paradigm-Tracking-handoff"})
            with opener.open(request, timeout=40) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code < 500 and error.code != 429:
                raise RuntimeError(f"Backend HTTP {error.code}") from None
            if attempt == 2:
                raise RuntimeError(f"Backend HTTP {error.code}") from None
        except urllib.error.URLError:
            if attempt == 2:
                raise RuntimeError("Backend connection failed") from None
        time.sleep(attempt + 1)


def database_deliveries(database, source_meta, report_dir, audit):
    meta = json.loads(source_meta.read_text())
    stats = json.loads(audit.read_text()).get("pipeline_stats", {}) if audit.is_file() else {}
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM report_outbox WHERE status='delivered' ORDER BY created_at,delivery_key").fetchall()
    for row in rows:
        body = row["report_content"] or ""
        if not body.strip() and stats.get("delivery_key") == row["delivery_key"]:
            path = report_dir / Path(stats.get("report_path", "")).name
            if path.is_file():
                body = path.read_text(encoding="utf-8")
        raw_stats = json.loads(row["stats_json"])
        data = {"deliveryKey": row["delivery_key"], "reportDate": row["report_date"],
                "snapshotCreatedAt": row["created_at"], "deliveredAt": row["delivered_at"], "deliveryStatus": "delivered",
                "candidates": [project_candidate(candidate) for candidate in json.loads(row["candidate_payload_json"])],
                "stats": {key: value for key, value in raw_stats.items() if key in STAT_FIELDS}}
        yield {"schemaVersion": "force-radar-ingest/1", "deliveryKey": row["delivery_key"], "reportDate": row["report_date"],
               "source": {"project": "AI Paradigm Tracking", "runId": str(meta["run_id"]), "commitSha": meta["commit_sha"]},
               "report": body, "structured": data}


def pool_deliveries(index):
    value = json.loads(index.read_text())
    for record in value["reports"]:
        delivery_zip = Path(record["intakeReceipt"]).parents[1] / "delivery.zip"
        with zipfile.ZipFile(delivery_zip) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
            entry = next(item for item in manifest["deliveries"] if item["deliveryKey"] == record["deliveryKey"])
            data = json.loads(bundle.read(entry["data"]))
            body = bundle.read(entry["report"]).decode("utf-8")
        yield {"schemaVersion": "force-radar-ingest/1", "deliveryKey": data["deliveryKey"], "reportDate": data["reportDate"],
               "source": {"project": "AI Paradigm Tracking", "runId": manifest["source"]["stateRunId"], "commitSha": manifest["source"]["stateCommitSha"]},
               "report": body, "structured": data}
    for gap in value["sourceGaps"]:
        if gap["status"] != "source-pair-unavailable":
            continue
        with zipfile.ZipFile(Path(gap["directory"]) / "report.zip") as bundle:
            for name in bundle.namelist():
                match = re.fullmatch(r"(?:.*/)?paradigm_radar_(\d{4}-\d{2}-\d{2})\.md", name)
                if not match:
                    continue
                yield {"schemaVersion": "force-radar-ingest/1", "deliveryKey": f"unpaired-run-{gap['runId']}-{match[1]}",
                       "reportDate": match[1], "source": {"project": "AI Paradigm Tracking", "runId": str(gap["runId"]),
                       "missingArtifacts": gap["missingArtifacts"]}, "report": bundle.read(name).decode("utf-8"), "structured": None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("FORCE_SITE_URL"))
    parser.add_argument("--pool-index", type=Path)
    parser.add_argument("--fetch-latest", action="store_true")
    parser.add_argument("--fetch-all", action="store_true")
    parser.add_argument("--database", type=Path, default=Path("database/paradigm_radar.db"))
    parser.add_argument("--source-meta", type=Path, default=Path("database/state-meta.json"))
    parser.add_argument("--report-dir", type=Path, default=Path("reports/output"))
    parser.add_argument("--audit", type=Path, default=Path("logs/run_audit_latest.json"))
    args = parser.parse_args()
    token = os.environ.get("FORCE_SITE_INGEST_TOKEN")
    if not args.url:
        parser.error("FORCE_SITE_URL or --url is required")
    if not token:
        if not sys.stdin.isatty():
            raise ValueError("Backend ingestion credential is unavailable")
        token = getpass.getpass("Backend ingest token (hidden): ")
    current = call_backend(args.url, token, "/api/radar/status")
    known = {row["id"] for row in current["reports"]}
    if args.fetch_latest or args.fetch_all:
        skip = {str(row["sourceRunId"]) for row in current["reports"] if row["status"] == "collected"} if args.fetch_all else set()
        folders = fetch_sources(Path(".private/force-handoff"), args.fetch_all, skip)
        inputs = (payload for folder in folders for payload in database_deliveries(
            folder / "paradigm_radar.db", folder / "state-meta.json", folder, folder / "run_audit_latest.json"))
    else:
        inputs = pool_deliveries(args.pool_index) if args.pool_index else database_deliveries(args.database, args.source_meta, args.report_dir, args.audit)
    result = []
    for payload in inputs:
        key = payload["deliveryKey"]
        if not payload["report"].strip():
            identity = hashlib.sha256(("AI Paradigm Tracking\n" + key).encode()).hexdigest()
            result.append({"deliveryKey": key, "id": identity, "status": "already-collected" if identity in known else "missing-original"})
            continue
        try:
            saved = call_backend(args.url, token, "/api/radar/ingest", payload)
            known.add(saved["record"]["id"])
            result.append({"deliveryKey": key, "status": saved["status"], "id": saved["record"]["id"]})
        except (RuntimeError, ValueError) as error:
            result.append({"deliveryKey": key, "status": "failed", "reason": str(error)})
    for row in result:
        if row["status"] == "missing-original" and row["id"] in known:
            row["status"] = "recovered-in-same-batch"
    print(json.dumps({"results": result}, ensure_ascii=False))
    return int(any(row["status"] in ("failed", "missing-original") for row in result))


if __name__ == "__main__":
    raise SystemExit(main())
