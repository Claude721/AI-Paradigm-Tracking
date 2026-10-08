"""Public code provenance, available before installing project dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parent
_SOURCE_DIRS = (
    "agents", "database", "notifications", "paradigms", "reports", "scripts",
    "sources", "skills", "rubrics", "taxonomy", "tests", ".github/workflows",
)
_SOURCE_FILES = (
    "main.py", "config.py", "runtime_clock.py", "runtime_provenance.py",
    "run_audit.py", "healthcheck.py", "smokecheck.py", "research_watchlist.py",
    "requirements.txt",
)
_DATA_SUFFIXES = {
    "skills": {".md"}, "rubrics": {".json"}, "taxonomy": {".json"},
    ".github/workflows": {".yml", ".yaml"},
}


def source_fingerprint(root: Path) -> str:
    """Hash executable code and policies; never read secrets or generated data."""
    files = {
        root / name for name in _SOURCE_FILES
        if (root / name).is_file() and not (root / name).is_symlink()
    }
    for name in _SOURCE_DIRS:
        directory = root / name
        if not directory.is_dir():
            continue
        suffixes = _DATA_SUFFIXES.get(name, {".py"})
        for path in directory.rglob("*"):
            relative = path.relative_to(root)
            if (
                path.is_file() and not path.is_symlink()
                and path.suffix in suffixes
                and not {"output", "__pycache__", ".git"}.intersection(relative.parts)
            ):
                files.add(path)
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@lru_cache(maxsize=1)
def runtime_provenance() -> dict:
    def git(*args):
        try:
            result = subprocess.run(
                ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    sha = git("rev-parse", "HEAD")
    expected = os.getenv("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
        sha = ""
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", expected):
        expected = ""
    return {
        "commit_sha": sha or expected,
        "workflow_sha": expected,
        "worktree_dirty": bool(git("status", "--porcelain")) if sha else None,
        "source_fingerprint": source_fingerprint(ROOT),
        "python_version": platform.python_version(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Record code identity before preflight")
    parser.add_argument("--output", type=Path, default=Path("logs/runtime_provenance.json"))
    args = parser.parse_args()
    payload = runtime_provenance()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    line = (
        f"运行代码 {payload['commit_sha'] or 'unavailable'}；"
        f"源码指纹 {payload['source_fingerprint']}；"
        f"工作区含未提交修改 {payload['worktree_dirty']}；"
        f"Python {payload['python_version']}"
    )
    print(line)
    summary = os.getenv("GITHUB_STEP_SUMMARY", "")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
