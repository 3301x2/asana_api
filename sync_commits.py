#!/usr/bin/env python3
"""
Sync GitHub commits to Asana boards.

Scans all repos under a GitHub account, groups new commits into tasks,
and creates them on matching Asana boards. Auto-creates boards for new repos.

Environment variables:
  ASANA_PAT           - Asana Personal Access Token
  ASANA_WORKSPACE_GID - Asana workspace GID
  GH_TOKEN            - GitHub PAT (with repo scope)
  GITHUB_OWNER        - GitHub username

Optional:
  SYNC_STATE_FILE     - Path to sync state JSON (default: .sync_state.json)
  SKIP_REPOS          - Comma-separated repo names to skip
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────

ASANA_PAT = os.environ.get("ASANA_PAT", "")
ASANA_BASE = "https://app.asana.com/api/1.0"
WORKSPACE_GID = os.environ.get("ASANA_WORKSPACE_GID", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_OWNER = os.environ.get("GITHUB_OWNER", "")
STATE_PATH = os.environ.get(
    "SYNC_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sync_state.json"),
)
SKIP_REPOS = {r.strip() for r in os.environ.get("SKIP_REPOS", "").split(",") if r.strip()}

SECTIONS = ["Done", "In Progress", "Backlog"]

PREFIXES = {
    "feat": "Feature", "fix": "Bug Fix", "docs": "Documentation",
    "chore": "Chore", "refactor": "Refactor", "test": "Testing",
    "style": "Style", "perf": "Performance", "ci": "CI/CD", "build": "Build",
}


# ── Validation ────────────────────────────────────────────────────────────────

def validate_env():
    required = {"ASANA_PAT": ASANA_PAT, "ASANA_WORKSPACE_GID": WORKSPACE_GID,
                "GH_TOKEN": GH_TOKEN, "GITHUB_OWNER": GH_OWNER}
    missing = [k for k, v in required.items() if not v]
    if missing:
        sys.exit(f"ERROR: Missing env vars: {', '.join(missing)}")


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _request(url, headers, method="GET", body=None):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            text = r.read().decode()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        return None


def asana(method, endpoint, data=None):
    headers = {"Authorization": f"Bearer {ASANA_PAT}",
               "Content-Type": "application/json", "Accept": "application/json"}
    body = json.dumps({"data": data}).encode() if data else None
    result = _request(f"{ASANA_BASE}{endpoint}", headers, method, body)
    return result.get("data") if result else None


def github(endpoint):
    headers = {"Authorization": f"Bearer {GH_TOKEN}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    sep = "&" if "?" in endpoint else "?"
    items, page = [], 1
    while True:
        batch = _request(f"https://api.github.com{endpoint}{sep}page={page}&per_page=100", headers)
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return items


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"repos": {}}


def save_state(state):
    state["last_sync"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


# ── Asana helpers ─────────────────────────────────────────────────────────────

def get_projects():
    projects = asana("GET", f"/workspaces/{WORKSPACE_GID}/projects?opt_fields=name,archived&limit=100")
    return [p for p in (projects or []) if not p.get("archived")]


def find_project(projects, name):
    lo = name.strip().lower()
    return next((p for p in projects if p["name"].strip().lower() == lo), None)


def create_project(name, notes=""):
    project = asana("POST", "/projects", {
        "name": name, "workspace": WORKSPACE_GID,
        "default_view": "board", **({"notes": notes} if notes else {}),
    })
    if not project:
        return None
    print(f"  + Project: {name}")
    for section in reversed(SECTIONS):
        asana("POST", f"/projects/{project['gid']}/sections", {"name": section})
    return project


def get_sections(project_gid):
    return asana("GET", f"/projects/{project_gid}/sections?opt_fields=name") or []


def get_task_names(project_gid):
    tasks = asana("GET", f"/projects/{project_gid}/tasks?opt_fields=name&limit=100")
    return {t["name"].strip().lower() for t in (tasks or [])}


def find_section(sections, name):
    lo = name.lower()
    return next((s for s in sections if s["name"].strip().lower() == lo), None)


def create_task(project_gid, section_gid, name, notes="", due_on=None):
    data = {"name": name, "workspace": WORKSPACE_GID, "notes": notes}
    if section_gid:
        data["memberships"] = [{"project": project_gid, "section": section_gid}]
    else:
        data["projects"] = [project_gid]
    if due_on:
        data["due_on"] = due_on
    result = asana("POST", "/tasks", data)
    if result:
        print(f"    + {name}")
    return result


# ── GitHub helpers ────────────────────────────────────────────────────────────

def get_repos():
    repos = github("/user/repos?affiliation=owner&sort=updated")
    return [r for r in repos if r["owner"]["login"].lower() == GH_OWNER.lower()]


def get_commits(repo_name, since=None, branch=None):
    params = []
    if since:
        params.append(f"since={since}")
    if branch:
        params.append(f"sha={branch}")
    qs = "&".join(params)
    return github(f"/repos/{GH_OWNER}/{repo_name}/commits?{qs}")


def get_default_branch(repo_name):
    repo = _request(
        f"https://api.github.com/repos/{GH_OWNER}/{repo_name}",
        {"Authorization": f"Bearer {GH_TOKEN}",
         "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"},
    )
    return repo.get("default_branch", "main") if repo else "main"


# ── Commit grouping ──────────────────────────────────────────────────────────

def parse_prefix(message):
    msg = message.strip().lower()
    for prefix in PREFIXES:
        if msg.startswith(f"{prefix}:") or msg.startswith(f"{prefix}("):
            return prefix
    return None


def group_commits(commits):
    """Group commits into logical tasks by prefix or individually."""
    groups = {}
    for c in commits:
        msg = c["commit"]["message"].split("\n")[0].strip()
        date = c["commit"]["committer"]["date"][:10]
        sha = c["sha"][:7]
        prefix = parse_prefix(msg)

        if prefix:
            clean = msg.split(":", 1)[-1].strip() if ":" in msg else msg
            key = f"{prefix}:{clean.lower()[:50]}"
        else:
            clean = msg
            key = f"standalone:{sha}"

        if key not in groups:
            groups[key] = {"name": clean, "lines": [], "date": date}

        groups[key]["lines"].append(f"- [{sha}] {msg} ({date})")
        if date > groups[key]["date"]:
            groups[key]["date"] = date

    tasks = [
        {"name": g["name"][:100], "notes": "\n".join(g["lines"]), "date": g["date"]}
        for g in groups.values()
    ]
    tasks.sort(key=lambda t: t["date"], reverse=True)
    return tasks


# ── Sync ──────────────────────────────────────────────────────────────────────

def sync_repo(repo, projects, state):
    name = repo["name"]
    if name in SKIP_REPOS:
        return

    print(f"\n--- {name} ---")
    repo_state = state["repos"].get(name, {})

    # Find or create Asana project
    project = find_project(projects, name)
    if not project:
        project = find_project(projects, name.replace("-", " ").title())
    if not project:
        desc = repo.get("description") or ""
        lang = repo.get("language") or "Unknown"
        notes = f"{desc}\n\nGitHub: https://github.com/{repo['full_name']}\nLanguage: {lang}".strip()
        project = create_project(name, notes)
        if not project:
            print(f"  ERROR: Failed to create project")
            return
        projects.append(project)

    project_gid = project["gid"]
    repo_state["project_gid"] = project_gid

    # Fetch new commits
    branch = get_default_branch(name)
    commits = get_commits(name, since=repo_state.get("last_sync_date"), branch=branch)
    if not commits:
        print(f"  No new commits")
        state["repos"][name] = repo_state
        return

    # Filter already-synced
    last_sha = repo_state.get("last_sha")
    if last_sha:
        filtered = []
        for c in commits:
            if c["sha"] == last_sha:
                break
            filtered.append(c)
        commits = filtered

    if not commits:
        print(f"  Up to date")
        state["repos"][name] = repo_state
        return

    print(f"  {len(commits)} new commit(s)")
    tasks = group_commits(commits)

    # Get sections and existing task names
    sections = get_sections(project_gid)
    existing = get_task_names(project_gid)
    backlog = find_section(sections, "Backlog") or (sections[-1] if sections else None)
    backlog_gid = backlog["gid"] if backlog else None

    created = skipped = 0
    for t in tasks:
        if t["name"].strip().lower() in existing:
            skipped += 1
            continue
        create_task(project_gid, backlog_gid, t["name"], t["notes"], t["date"])
        created += 1

    print(f"  {created} created, {skipped} skipped (duplicates)")
    repo_state["last_sha"] = commits[0]["sha"]
    repo_state["last_sync_date"] = datetime.now(timezone.utc).isoformat()
    state["repos"][name] = repo_state


def main():
    validate_env()
    print(f"Sync started: {datetime.now(timezone.utc).isoformat()}")
    print(f"Owner: {GH_OWNER} | Workspace: {WORKSPACE_GID}\n")

    state = load_state()
    repos = get_repos()
    print(f"Repos: {len(repos)}")

    projects = get_projects()
    print(f"Asana projects: {len(projects)}")

    for repo in repos:
        try:
            sync_repo(repo, projects, state)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)

    save_state(state)
    print(f"\nDone. State saved to {STATE_PATH}")


if __name__ == "__main__":
    main()
