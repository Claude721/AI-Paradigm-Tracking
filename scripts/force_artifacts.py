"""Read GitHub Actions artifacts using existing local authentication."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.parse
import urllib.request


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ValueError("Artifact redirect must use HTTPS")
        redirected = super().redirect_request(request, response, code, message, headers, url)
        if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(request.full_url).netloc:
            redirected.remove_header("Authorization")
        return redirected


class GitHubArtifacts:
    def __init__(self, repo):
        parts = repo.split("/")
        if len(parts) != 2 or any(not part or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in part) for part in parts):
            raise ValueError("Expected GitHub owner/repository")
        self.repo = repo
        self.base = "https://api.github.com/repos/" + repo
        self.opener = urllib.request.build_opener(SafeRedirect())
        self.token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            result = subprocess.run(
                ["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                text=True, capture_output=True, timeout=15,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
            )
            values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            self.token = values.get("password")
        if not self.token:
            raise ValueError("GitHub credential unavailable")

    def open(self, path):
        request = urllib.request.Request(self.base + path, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json", "User-Agent": "AI-force-analysis-weekly-intake",
        })
        return self.opener.open(request, timeout=60)

    def get(self, path):
        with self.open(path) as response:
            return json.load(response)

    def runs(self, limit):
        result = []
        page = 1
        while len(result) < limit:
            data = self.get(f"/actions/workflows/weekly-radar.yml/runs?status=completed&per_page=100&page={page}")
            rows = data["workflow_runs"]
            result.extend(rows[:limit - len(result)])
            if len(rows) < 100:
                break
            page += 1
        return list(reversed(result))

    def artifacts(self, run_id):
        result = []
        page = 1
        while True:
            data = self.get(f"/actions/runs/{int(run_id)}/artifacts?per_page=100&page={page}")
            rows = data["artifacts"]
            result.extend(rows)
            if len(rows) < 100:
                return result
            page += 1

    def download(self, artifact, destination):
        expected = artifact.get("digest")
        checksum = hashlib.sha256()
        size = 0
        with self.open(f"/actions/artifacts/{int(artifact['id'])}/zip") as response, Path(destination).open("xb") as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 512 * 1024 * 1024:
                    raise ValueError("Artifact exceeds the download limit")
                checksum.update(chunk)
                output.write(chunk)
        actual = "sha256:" + checksum.hexdigest()
        if expected and expected != actual:
            Path(destination).unlink()
            raise ValueError("GitHub artifact digest mismatch")
        return {"id": artifact["id"], "name": artifact["name"], "sha256": checksum.hexdigest(),
                "githubDigestVerified": bool(expected), "sizeBytes": size}
