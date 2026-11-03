"""Offline-only repair of an approved failed delivery artifact into a new copy.

No in-place repair, production secrets, model/search/email calls, or dispatch.
The historical renderer is read from a verified ancestor commit solely to
recompute exact draft cache keys, never to execute a research pipeline.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _historical_bindings(database, source_ref, delivery_key, digest):
    if not re.fullmatch(r"[0-9a-f]{40}", source_ref):
        raise ValueError("原提交必须是完整SHA，不接受任意路径或分支表达式")
    subprocess.run(["git", "merge-base", "--is-ancestor", source_ref, "HEAD"], cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    archive = subprocess.check_output(["git", "archive", "--format=tar", source_ref], cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix="radar-historical-binding-") as directory:
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            for member in bundle.getmembers():
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                    # Git's export metadata/symlinks are not needed for imports.
                    continue
                bundle.extract(member, directory, filter="data")
        program = """import json,sqlite3,sys,socket
def no_network(*args,**kwargs):
 raise RuntimeError('historical cache binding cannot access the network')
socket.socket.connect=no_network
socket.create_connection=no_network
from datetime import datetime
from paradigms.models import candidate_from_dict
from reports.paradigm_generator import _route_fragment_key
from runtime_clock import research_window
connection=sqlite3.connect(sys.argv[1],uri=True)
payload,stats=connection.execute('SELECT candidate_payload_json,stats_json FROM report_outbox WHERE delivery_key=?',(sys.argv[2],)).fetchone()
stats=json.loads(stats)
reference=datetime.fromisoformat(stats['research_campaign']['reference_time'])
days=connection.execute('SELECT ordinary_days FROM research_campaigns WHERE campaign_id=?',(stats['research_campaign']['campaign_id'],)).fetchone()[0]
with research_window(reference,observation_time=reference,lookback_days=days):
 print(json.dumps({v['key']:_route_fragment_key(candidate_from_dict(v)) for v in json.loads(payload)}))
"""
        # This subprocess inherits only the already-hermetic script environment.
        process = subprocess.run([sys.executable, "-c", program,
                                  database.resolve().as_uri() + "?mode=ro", delivery_key],
                                 cwd=directory, env=os.environ.copy(), text=True, timeout=90, capture_output=True)
        if process.returncode:
            raise ValueError("原提交草稿绑定计算失败；停止恢复，不补买模型或搜索调用")
    return {"snapshot_digest": digest, "draft_keys": json.loads(process.stdout), "source_commit": source_ref}


def _run(args):
    if (os.environ.get("AI_RADAR_SKIP_DOTENV") != "true"
            or os.environ.get("HTTP_PROXY") != "http://127.0.0.1:9"):
        raise ValueError("恢复只能在隔离、拒绝网络的环境执行")
    source = args.state_zip.resolve(strict=True)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", args.expected_sha256) or digest != args.expected_sha256:
        raise ValueError("状态ZIP摘要与已批准来源不一致")
    if args.output_dir.is_symlink() or args.output_dir.exists():
        raise ValueError("输出目录必须尚不存在；不覆盖原状态、备份或已有恢复结果")
    from database.paradigm_store import ParadigmStore
    from paradigms.publication_recovery import recover_publication, snapshot_digest
    with tempfile.TemporaryDirectory(prefix="radar-delivery-recovery-") as temporary:
        workspace = Path(temporary)
        with zipfile.ZipFile(source) as bundle:
            if (bundle.namelist().count("paradigm_radar.db") != 1
                    or bundle.namelist().count("state-meta.json") != 1):
                raise ValueError("状态制品缺少唯一数据库或版本元数据")
            bundle.extract("paradigm_radar.db", workspace)
            metadata = json.loads(bundle.read("state-meta.json"))
        source_ref = str(metadata.get("commit_sha", ""))
        if args.expected_source_commit and source_ref != args.expected_source_commit:
            raise ValueError("状态提交与GitHub原运行提交不一致")
        database = workspace / "paradigm_radar.db"
        with sqlite3.connect(database) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("原数据库完整性检查失败")
        store = ParadigmStore(database)
        delivery_key = args.delivery_key
        if not delivery_key:
            with store._connect() as connection:
                rows = connection.execute("SELECT delivery_key FROM report_outbox WHERE status='quarantined' "
                                          "AND failure_kind='report_quality_contract' ORDER BY created_at DESC").fetchall()
            jobs = [store.get_report_job(row[0]) for row in rows]
            eligible = [job for job in jobs if job.stats.get("delivery_contract_version") == 5 and job.candidates]
            if len(eligible) != 1:
                raise ValueError("不能唯一确定失败交付，请显式指定delivery-key")
            delivery_key = eligible[0].delivery_key
        job = store.get_report_job(delivery_key)
        if job is None:
            raise ValueError("未找到指定交付")
        bindings = _historical_bindings(database, source_ref, delivery_key, snapshot_digest(job))
        result = recover_publication(store, delivery_key, bindings, apply=args.apply)
        args.output_dir.mkdir(parents=True)
        # Public diagnostic output is metadata only; original error details
        # remain in the private state journal, not the workflow summary.
        summary = {key: value for key, value in result.items() if key not in {
            "original_last_error", "restore_keys", "excluded", "draft_excluded"}}
        summary.update(source_zip_sha256=digest, source_commit=source_ref,
                       excluded_count=len(result.get("excluded", {})),
                       draft_excluded_count=len(result.get("draft_excluded", {})),
                       restored_count=result.get("restored_count", 0), email_sent=False, model_calls=0)
        (args.output_dir / "recovery_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        if args.apply:
            with sqlite3.connect(database) as original, sqlite3.connect(args.output_dir / "paradigm_radar.db") as output:
                original.backup(output)
                if output.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("恢复数据库完整性检查失败")
            from runtime_provenance import runtime_provenance
            metadata.update(commit_sha=runtime_provenance()["commit_sha"], run_id=os.environ.get("GITHUB_RUN_ID", ""),
                            delivery_recovery={"parent_key": delivery_key, "replacement_key": result["replacement_key"],
                                               "source_commit": source_ref, "source_zip_sha256": digest})
            (args.output_dir / "state-meta.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-zip", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-source-commit", default="")
    parser.add_argument("--delivery-key", default="")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="仅生成修复后的新状态副本；不发邮件")
    parser.add_argument("--isolated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.isolated:
        from scripts.offline_checks import _safe_environment
        command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--isolated"]
        environment = _safe_environment()
        # Artifact-download credentials must not reach historical code.
        for key in list(environment):
            if any(word in key.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
                environment.pop(key, None)
        environment.pop("PYTHONPATH", None)
        raise SystemExit(subprocess.run(command, cwd=ROOT, env=environment).returncode)
    _run(args)


if __name__ == "__main__":
    main()
