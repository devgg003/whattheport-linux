"""GitHub lookup for Vercel preview deployments and PR info via `gh` CLI."""
import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

@dataclass
class PreviewDeployment:
    url: str
    state: str  # "ready", "building", "failed"
    created_at: Optional[str] = None

@dataclass
class PullRequestInfo:
    number: int
    title: str
    url: str
    state: str

@dataclass
class GitHubResult:
    preview: Optional[PreviewDeployment] = None
    pull_request: Optional[PullRequestInfo] = None

class GitHubLookup:
    def __init__(self):
        self.gh_path = shutil.which("gh")
        self.results: Dict[str, GitHubResult] = {}
        self.fetched_at: Dict[str, float] = {}

    @property
    def is_available(self) -> bool:
        return self.gh_path is not None

    def get(self, root: Optional[str], branch: Optional[str]) -> Optional[GitHubResult]:
        if not root or not branch:
            return None
        key = f"{root}|{branch}"
        return self.results.get(key)

    def refresh(self, root: Optional[str], branch: Optional[str], wants_previews: bool, wants_prs: bool, max_age: float = 120.0):
        if not self.is_available or not root or not branch:
            return
        if not wants_previews and not wants_prs:
            return

        key = f"{root}|{branch}"
        now = time.time()
        if key in self.fetched_at and (now - self.fetched_at[key]) < max_age:
            return

        self.fetched_at[key] = now

        def _worker():
            preview = self._fetch_preview(root, branch) if wants_previews else None
            pr = self._fetch_pr(root, branch) if wants_prs else None
            self.results[key] = GitHubResult(preview=preview, pull_request=pr)

        threading.Thread(target=_worker, daemon=True).start()

    def _fetch_preview(self, root: str, branch: str) -> Optional[PreviewDeployment]:
        try:
            # Get branch commits
            res = subprocess.run(
                [self.gh_path, "api", f"repos/{{owner}}/{{repo}}/commits?sha={branch}&per_page=20", "--jq", ".[].sha"],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False
            )
            if res.returncode != 0 or not res.stdout:
                return None
            commits = [s.strip() for s in res.stdout.strip().splitlines() if s.strip()]
            if not commits:
                return None

            # Get deployments
            res = subprocess.run(
                [self.gh_path, "api", "repos/{owner}/{repo}/deployments?per_page=30"],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False
            )
            if res.returncode != 0 or not res.stdout:
                return None
            deployments = json.loads(res.stdout)
            previews = [d for d in deployments if "preview" in (d.get("environment") or "").lower()]

            commit_order = {sha: idx for idx, sha in enumerate(commits)}
            matches = []
            for d in previews:
                sha = d.get("sha")
                if sha in commit_order:
                    matches.append((commit_order[sha], d))
            if not matches:
                return None
            matches.sort(key=lambda m: m[0])
            best_dep = matches[0][1]

            dep_id = best_dep.get("id")
            if not dep_id:
                return None

            # Statuses
            res = subprocess.run(
                [self.gh_path, "api", f"repos/{{owner}}/{{repo}}/deployments/{dep_id}/statuses?per_page=1"],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False
            )
            if res.returncode != 0 or not res.stdout:
                return None
            statuses = json.loads(res.stdout)
            if not statuses:
                return None
            st = statuses[0]

            target_url = st.get("environment_url") or st.get("target_url")
            if not target_url:
                return None

            raw_state = st.get("state", "").lower()
            if raw_state == "success":
                state = "ready"
            elif raw_state in ("failure", "error"):
                state = "failed"
            else:
                state = "building"

            return PreviewDeployment(
                url=target_url,
                state=state,
                created_at=best_dep.get("created_at")
            )
        except Exception:
            return None

    def _fetch_pr(self, root: str, branch: str) -> Optional[PullRequestInfo]:
        try:
            res = subprocess.run(
                [self.gh_path, "pr", "list", "--head", branch, "--state", "all", "--limit", "1", "--json", "number,title,url,state"],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False
            )
            if res.returncode != 0 or not res.stdout:
                return None
            items = json.loads(res.stdout)
            if not items:
                return None
            first = items[0]
            return PullRequestInfo(
                number=int(first.get("number", 0)),
                title=first.get("title", ""),
                url=first.get("url", ""),
                state=first.get("state", "").capitalize()
            )
        except Exception:
            return None
