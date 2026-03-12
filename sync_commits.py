#!/usr/bin/env python3
"""
GitHub-to-Asana Sync

Scans all repositories under a GitHub account, identifies new commits
since the last sync, groups them into logical tasks, and creates
corresponding Asana tickets. Automatically creates Asana boards
for repositories that don't have one yet.

Designed to run as a GitHub Action on a weekly schedule or manually.

Environment variables:
    ASANA_PAT           Asana Personal Access Token
    ASANA_WORKSPACE_GID Target Asana workspace GID
    GH_TOKEN            GitHub PAT with 'repo' scope
    GITHUB_OWNER        GitHub username to scan

Optional:
    SYNC_STATE_FILE     Path to persistent state file (default: .sync_state.json)
    SKIP_REPOS          Comma-separated repository names to exclude
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

# ── Constants ─────────────────────────────────────────────────────────────────

ASANA_API_BASE = "https://app.asana.com/api/1.0"
GITHUB_API_BASE = "https://api.github.com"

DEFAULT_BOARD_SECTIONS = ["Done", "In Progress", "Backlog"]

CONVENTIONAL_PREFIXES = {
    "feat": "Feature",
    "fix": "Bug Fix",
    "docs": "Documentation",
    "chore": "Chore",
    "refactor": "Refactor",
    "test": "Testing",
    "style": "Style",
    "perf": "Performance",
    "ci": "CI/CD",
    "build": "Build",
}


# ── Configuration ─────────────────────────────────────────────────────────────


class Config:
    """Validated configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.asana_token = os.environ.get("ASANA_PAT", "")
        self.workspace_gid = os.environ.get("ASANA_WORKSPACE_GID", "")
        self.github_token = os.environ.get("GH_TOKEN", "")
        self.github_owner = os.environ.get("GITHUB_OWNER", "")
        self.state_path = os.environ.get(
            "SYNC_STATE_FILE",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sync_state.json"),
        )
        self.skip_repos = {
            name.strip()
            for name in os.environ.get("SKIP_REPOS", "").split(",")
            if name.strip()
        }

    def validate(self) -> None:
        """Exit with an error if any required variable is missing."""
        required = {
            "ASANA_PAT": self.asana_token,
            "ASANA_WORKSPACE_GID": self.workspace_gid,
            "GH_TOKEN": self.github_token,
            "GITHUB_OWNER": self.github_owner,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            sys.exit(f"Error: missing environment variables: {', '.join(missing)}")


# ── HTTP Clients ──────────────────────────────────────────────────────────────


def _http_request(
    url: str,
    headers: dict[str, str],
    method: str = "GET",
    body: Optional[bytes] = None,
) -> Optional[Any]:
    """Send an HTTP request and return the parsed JSON response.

    Returns None on HTTP errors instead of raising, allowing callers
    to handle failures gracefully during batch operations.
    """
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            text = resp.read().decode()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode()}", file=sys.stderr)
        return None


class AsanaClient:
    """Minimal Asana API client."""

    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def request(self, method: str, endpoint: str, data: Optional[dict] = None) -> Optional[Any]:
        body = json.dumps({"data": data}).encode() if data is not None else None
        result = _http_request(f"{ASANA_API_BASE}{endpoint}", self._headers, method, body)
        if result and "data" in result:
            return result["data"]
        return None


class GitHubClient:
    """Minimal GitHub API client with automatic pagination."""

    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get(self, endpoint: str) -> Optional[Any]:
        """Fetch a single resource."""
        return _http_request(f"{GITHUB_API_BASE}{endpoint}", self._headers)

    def get_all(self, endpoint: str) -> list[dict]:
        """Fetch all pages of a paginated list endpoint."""
        separator = "&" if "?" in endpoint else "?"
        items: list[dict] = []
        page = 1

        while True:
            url = f"{GITHUB_API_BASE}{endpoint}{separator}page={page}&per_page=100"
            batch = _http_request(url, self._headers)
            if not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1

        return items


# ── Sync State ────────────────────────────────────────────────────────────────


class SyncState:
    """Manages persistent sync state between runs.

    State is stored as JSON with per-repo tracking of the last synced
    commit SHA and timestamp.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict[str, Any] = {"repos": {}}

    def load(self) -> None:
        if os.path.exists(self._path):
            with open(self._path) as f:
                self._data = json.load(f)

    def save(self) -> None:
        self._data["last_sync"] = datetime.now(timezone.utc).isoformat()
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    def get_repo(self, name: str) -> dict:
        return self._data["repos"].get(name, {})

    def set_repo(self, name: str, state: dict) -> None:
        self._data["repos"][name] = state


# ── Commit Processing ────────────────────────────────────────────────────────


def _parse_conventional_prefix(message: str) -> Optional[str]:
    """Extract the conventional commit prefix (feat, fix, etc.) if present."""
    normalized = message.strip().lower()
    for prefix in CONVENTIONAL_PREFIXES:
        if normalized.startswith(f"{prefix}:") or normalized.startswith(f"{prefix}("):
            return prefix
    return None


def group_commits_into_tasks(commits: list[dict]) -> list[dict]:
    """Group a list of commits into logical task entries.

    Commits using conventional commit prefixes with similar messages are
    merged into a single task. Non-conventional commits remain individual.

    Returns:
        Sorted list (newest first) of dicts with keys: name, notes, date.
    """
    groups: dict[str, dict] = {}

    for commit in commits:
        message = commit["commit"]["message"].split("\n")[0].strip()
        date = commit["commit"]["committer"]["date"][:10]
        sha_short = commit["sha"][:7]
        prefix = _parse_conventional_prefix(message)

        if prefix:
            clean_name = message.split(":", 1)[-1].strip() if ":" in message else message
            group_key = f"{prefix}:{clean_name.lower()[:50]}"
        else:
            clean_name = message
            group_key = f"standalone:{sha_short}"

        if group_key not in groups:
            groups[group_key] = {"name": clean_name, "commit_lines": [], "date": date}

        groups[group_key]["commit_lines"].append(f"- [{sha_short}] {message} ({date})")

        if date > groups[group_key]["date"]:
            groups[group_key]["date"] = date

    tasks = [
        {
            "name": group["name"][:100],
            "notes": "\n".join(group["commit_lines"]),
            "date": group["date"],
        }
        for group in groups.values()
    ]
    tasks.sort(key=lambda t: t["date"], reverse=True)
    return tasks


# ── Sync Engine ───────────────────────────────────────────────────────────────


class SyncEngine:
    """Orchestrates the GitHub-to-Asana synchronization."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._asana = AsanaClient(config.asana_token)
        self._github = GitHubClient(config.github_token)
        self._state = SyncState(config.state_path)
        self._asana_projects: list[dict] = []

    def run(self) -> None:
        """Execute the full sync: discover repos, sync commits, create boards."""
        self._state.load()

        repos = self._fetch_repos()
        print(f"Repos: {len(repos)}")

        self._asana_projects = self._fetch_asana_projects()
        print(f"Asana projects: {len(self._asana_projects)}")

        for repo in repos:
            name = repo["name"]
            if name in self._config.skip_repos:
                continue
            try:
                self._sync_repo(repo)
            except Exception as exc:
                print(f"  Error syncing {name}: {exc}", file=sys.stderr)

        self._state.save()
        print(f"\nSync complete. State saved to {self._config.state_path}")

    # ── Private helpers ──

    def _fetch_repos(self) -> list[dict]:
        repos = self._github.get_all("/user/repos?affiliation=owner&sort=updated")
        owner = self._config.github_owner.lower()
        return [r for r in repos if r["owner"]["login"].lower() == owner]

    def _fetch_asana_projects(self) -> list[dict]:
        workspace = self._config.workspace_gid
        projects = self._asana.request(
            "GET", f"/workspaces/{workspace}/projects?opt_fields=name,archived&limit=100"
        )
        return [p for p in (projects or []) if not p.get("archived")]

    def _find_asana_project(self, name: str) -> Optional[dict]:
        """Find an Asana project by exact name (case-insensitive)."""
        normalized = name.strip().lower()
        for project in self._asana_projects:
            if project["name"].strip().lower() == normalized:
                return project
        return None

    def _create_asana_board(self, name: str, notes: str = "") -> Optional[dict]:
        """Create a new board-style Asana project with standard sections."""
        data: dict[str, str] = {
            "name": name,
            "workspace": self._config.workspace_gid,
            "default_view": "board",
        }
        if notes:
            data["notes"] = notes

        project = self._asana.request("POST", "/projects", data)
        if not project:
            return None

        print(f"  + Board: {name}")

        # Create sections in reverse so they display in the expected order
        for section_name in reversed(DEFAULT_BOARD_SECTIONS):
            self._asana.request(
                "POST", f"/projects/{project['gid']}/sections", {"name": section_name}
            )

        return project

    def _sync_repo(self, repo: dict) -> None:
        """Sync a single repository's commits to its Asana board."""
        name = repo["name"]
        print(f"\n--- {name} ---")

        repo_state = self._state.get_repo(name)

        # Find or create the corresponding Asana board
        project = self._find_asana_project(name)
        if not project:
            project = self._find_asana_project(name.replace("-", " ").title())

        if not project:
            description = repo.get("description") or ""
            language = repo.get("language") or "Unknown"
            notes = (
                f"{description}\n\n"
                f"GitHub: https://github.com/{repo['full_name']}\n"
                f"Language: {language}"
            ).strip()

            project = self._create_asana_board(name, notes)
            if not project:
                print("  Failed to create board")
                return
            self._asana_projects.append(project)

        project_gid = project["gid"]
        repo_state["project_gid"] = project_gid

        # Fetch commits since last sync
        default_branch = self._get_default_branch(name)
        commits = self._get_commits(
            name, since=repo_state.get("last_sync_date"), branch=default_branch
        )

        if not commits:
            print("  No new commits")
            self._state.set_repo(name, repo_state)
            return

        # Filter out already-synced commits
        last_sha = repo_state.get("last_sha")
        if last_sha:
            new_commits = []
            for commit in commits:
                if commit["sha"] == last_sha:
                    break
                new_commits.append(commit)
            commits = new_commits

        if not commits:
            print("  Up to date")
            self._state.set_repo(name, repo_state)
            return

        print(f"  {len(commits)} new commit(s)")

        # Group commits and create tasks
        tasks = group_commits_into_tasks(commits)
        self._create_tasks_from_commits(project_gid, tasks)

        # Update sync state
        repo_state["last_sha"] = commits[0]["sha"]
        repo_state["last_sync_date"] = datetime.now(timezone.utc).isoformat()
        self._state.set_repo(name, repo_state)

    def _create_tasks_from_commits(self, project_gid: str, tasks: list[dict]) -> None:
        """Create Asana tasks, skipping duplicates."""
        sections = self._asana.request(
            "GET", f"/projects/{project_gid}/sections?opt_fields=name"
        ) or []

        existing_names = set()
        existing = self._asana.request(
            "GET", f"/projects/{project_gid}/tasks?opt_fields=name&limit=100"
        )
        if existing:
            existing_names = {t["name"].strip().lower() for t in existing}

        # Find the Backlog section for new tasks
        backlog = next(
            (s for s in sections if s["name"].strip().lower() == "backlog"),
            sections[-1] if sections else None,
        )
        backlog_gid = backlog["gid"] if backlog else None

        created = skipped = 0
        for task in tasks:
            if task["name"].strip().lower() in existing_names:
                skipped += 1
                continue

            data: dict[str, Any] = {
                "name": task["name"],
                "workspace": self._config.workspace_gid,
                "notes": task["notes"],
            }
            if backlog_gid:
                data["memberships"] = [{"project": project_gid, "section": backlog_gid}]
            else:
                data["projects"] = [project_gid]
            if task.get("date"):
                data["due_on"] = task["date"]

            if self._asana.request("POST", "/tasks", data):
                print(f"    + {task['name']}")
                created += 1

        print(f"  {created} created, {skipped} skipped (duplicates)")

    def _get_default_branch(self, repo_name: str) -> str:
        owner = self._config.github_owner
        repo = self._github.get(f"/repos/{owner}/{repo_name}")
        return repo.get("default_branch", "main") if repo else "main"

    def _get_commits(
        self,
        repo_name: str,
        since: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> list[dict]:
        owner = self._config.github_owner
        params = []
        if since:
            params.append(f"since={since}")
        if branch:
            params.append(f"sha={branch}")
        query_string = "&".join(params)
        return self._github.get_all(f"/repos/{owner}/{repo_name}/commits?{query_string}")


# ── Entry Point ───────────────────────────────────────────────────────────────


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Sync GitHub commits to Asana boards.")
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="Only create boards for repos that don't have one (skip commit sync).",
    )
    parser.add_argument(
        "--repo",
        help="Sync a specific repo by name instead of all repos.",
    )
    args = parser.parse_args()

    config = Config()
    config.validate()

    print(f"Sync started: {datetime.now(timezone.utc).isoformat()}")
    print(f"Owner: {config.github_owner} | Workspace: {config.workspace_gid}")
    if args.new_only:
        print("Mode: new repos only")
    if args.repo:
        print(f"Target: {args.repo}")
    print()

    engine = SyncEngine(config)

    if args.repo or args.new_only:
        engine._state.load()
        repos = engine._fetch_repos()
        engine._asana_projects = engine._fetch_asana_projects()
        project_names = {p["name"].strip().lower() for p in engine._asana_projects}

        for repo in repos:
            name = repo["name"]
            if args.repo and name != args.repo:
                continue
            if name in config.skip_repos:
                continue
            if args.new_only:
                if name.lower() in project_names:
                    continue
                alt = name.replace("-", " ").title().lower()
                if alt in project_names:
                    continue
                print(f"New repo detected: {name}")
            engine._sync_repo(repo)

        engine._state.save()
        print(f"\nDone. State saved to {config.state_path}")
    else:
        engine.run()


if __name__ == "__main__":
    main()
