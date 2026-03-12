#!/usr/bin/env python3
"""
Asana Manager CLI

A zero-dependency command-line interface for managing Asana workspaces.
Supports projects, tasks, sections, tags, and team members with
human-friendly name resolution (case-insensitive, partial match).

Authentication:
    Set the ASANA_PAT environment variable with your Personal Access Token.
    Optionally set ASANA_WORKSPACE_GID to target a specific workspace.

Usage:
    python asana_api.py <command> [options]
    python asana_api.py --help
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

# ── Constants ─────────────────────────────────────────────────────────────────

API_BASE_URL = "https://app.asana.com/api/1.0"
AUDIT_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_log.jsonl")


# ── Exceptions ────────────────────────────────────────────────────────────────


class AsanaAPIError(Exception):
    """Raised when the Asana API returns an error response."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"HTTP {status_code}: {message}")


class ResolutionError(Exception):
    """Raised when a name cannot be resolved to a unique GID."""


# ── API Client ────────────────────────────────────────────────────────────────


class AsanaClient:
    """Lightweight client for the Asana REST API using only urllib."""

    def __init__(self, token: str) -> None:
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def request(self, method: str, endpoint: str, data: Optional[dict] = None) -> dict:
        """Send an authenticated request to the Asana API.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE).
            endpoint: API path relative to the base URL (e.g. "/tasks").
            data: Optional payload for write operations.

        Returns:
            Parsed JSON response as a dictionary.

        Raises:
            AsanaAPIError: If the API returns a non-2xx status code.
        """
        url = f"{API_BASE_URL}{endpoint}"
        body = json.dumps({"data": data}).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=self.headers, method=method)

        try:
            with urllib.request.urlopen(req) as resp:
                text = resp.read().decode()
                return json.loads(text) if text else {"data": {}}
        except urllib.error.HTTPError as exc:
            raise AsanaAPIError(exc.code, exc.read().decode()) from exc


# ── Name Resolution ───────────────────────────────────────────────────────────


class NameResolver:
    """Resolves human-friendly names to Asana GIDs with caching.

    Performs case-insensitive matching with partial-match support.
    Priority: exact match > unique partial match > error on ambiguity.
    """

    def __init__(self, client: AsanaClient, workspace_gid: str) -> None:
        self._client = client
        self._workspace_gid = workspace_gid
        self._project_cache: Optional[list[dict]] = None
        self._section_cache: dict[str, list[dict]] = {}

    def _fuzzy_match(
        self, items: list[dict], query: str, label: str = "item"
    ) -> str:
        """Match a query string against a list of named items.

        Args:
            items: List of dicts with "gid" and "name" keys.
            query: Search string to match against.
            label: Human-readable label for error messages.

        Returns:
            The GID of the matched item.

        Raises:
            ResolutionError: If no match or ambiguous match is found.
        """
        normalized = query.strip().lower()

        # Exact match takes priority
        for item in items:
            if item["name"].strip().lower() == normalized:
                return item["gid"]

        # Fall back to partial match
        matches = [i for i in items if normalized in i["name"].strip().lower()]

        if len(matches) == 1:
            return matches[0]["gid"]

        if len(matches) > 1:
            options = "\n".join(
                f'  - "{m["name"].strip()}" ({m["gid"]})' for m in matches
            )
            raise ResolutionError(
                f'"{query}" matches multiple {label}s:\n{options}'
            )

        raise ResolutionError(f'No {label} found matching "{query}".')

    def project(self, identifier: str) -> str:
        """Resolve a project name or GID to a GID."""
        if identifier.isdigit():
            return identifier

        if self._project_cache is None:
            endpoint = f"/workspaces/{self._workspace_gid}/projects"
            result = self._client.request("GET", f"{endpoint}?opt_fields=name,archived&limit=100")
            self._project_cache = result["data"]

        return self._fuzzy_match(self._project_cache, identifier, "project")

    def section(self, identifier: str, project_gid: str) -> str:
        """Resolve a section name or GID to a GID within a project."""
        if identifier.isdigit():
            return identifier

        if project_gid not in self._section_cache:
            result = self._client.request(
                "GET", f"/projects/{project_gid}/sections?opt_fields=name"
            )
            self._section_cache[project_gid] = result["data"]

        return self._fuzzy_match(
            self._section_cache[project_gid], identifier, "section"
        )

    def task(self, identifier: str, project_gid: Optional[str] = None) -> str:
        """Resolve a task name or GID to a GID.

        Numeric identifiers longer than 8 digits are treated as GIDs.
        Name resolution requires a project context.
        """
        if identifier.isdigit() and len(identifier) > 8:
            return identifier

        if not project_gid:
            raise ResolutionError("--project is required when using task names.")

        result = self._client.request(
            "GET", f"/projects/{project_gid}/tasks?opt_fields=name,completed&limit=100"
        )
        return self._fuzzy_match(result["data"], identifier, "task")


# ── Audit Log ─────────────────────────────────────────────────────────────────


def write_audit_entry(action: str, details: dict[str, Any]) -> None:
    """Append a timestamped entry to the JSONL audit log.

    Silently skips if the log file cannot be written (e.g. read-only filesystem).
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        **details,
    }
    try:
        with open(AUDIT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# ── Output Helpers ────────────────────────────────────────────────────────────


def _print_json(data: Any) -> None:
    """Pretty-print a JSON-serializable object to stdout."""
    print(json.dumps(data, indent=2))


def _format_task(raw: dict) -> dict:
    """Normalize an Asana task response into a consistent output format."""
    return {
        "gid": raw["gid"],
        "name": raw["name"],
        "completed": raw.get("completed", False),
        "assignee": (raw.get("assignee") or {}).get("name"),
        "due_on": raw.get("due_on"),
        "tags": [tag["name"] for tag in raw.get("tags", [])],
    }


# ── Command Handlers ─────────────────────────────────────────────────────────
#
# Each handler receives the parsed CLI args and operates through the shared
# `client` and `resolver` instances initialized in main().

client: AsanaClient
resolver: NameResolver
workspace_gid: str


def _resolve_task_with_project(args: argparse.Namespace) -> tuple[str, Optional[str]]:
    """Common pattern: resolve task GID, optionally via project context."""
    project_gid = resolver.project(args.project) if args.project else None
    task_gid = resolver.task(args.task, project_gid) if project_gid else args.task
    return task_gid, project_gid


# -- Tasks --

def cmd_create_task(args: argparse.Namespace) -> None:
    project_gid = resolver.project(args.project)
    data: dict[str, Any] = {"name": args.name, "workspace": workspace_gid}

    if args.section:
        section_gid = resolver.section(args.section, project_gid)
        data["memberships"] = [{"project": project_gid, "section": section_gid}]
    else:
        data["projects"] = [project_gid]

    for api_field, arg_name in [("notes", "notes"), ("assignee", "assignee"),
                                ("due_on", "due_on"), ("start_on", "start_on")]:
        value = getattr(args, arg_name, None)
        if value:
            data[api_field] = value

    task = client.request("POST", "/tasks", data)["data"]
    _print_json({
        "status": "created",
        "task_gid": task["gid"],
        "name": task["name"],
        "url": f"https://app.asana.com/0/0/{task['gid']}/f",
    })
    write_audit_entry("create-task", {
        "task_gid": task["gid"], "name": task["name"],
        "project": args.project, "section": getattr(args, "section", None),
    })


def cmd_update_task(args: argparse.Namespace) -> None:
    task_gid, _ = _resolve_task_with_project(args)

    data = {}
    for api_field, arg_name in [("name", "name"), ("notes", "notes"),
                                ("assignee", "assignee"), ("due_on", "due_on")]:
        value = getattr(args, arg_name, None)
        if value:
            data[api_field] = value

    if not data:
        sys.exit("Error: provide at least one of --name, --notes, --assignee, --due-on")

    task = client.request("PUT", f"/tasks/{task_gid}", data)["data"]
    _print_json({"status": "updated", "task_gid": task["gid"], "name": task["name"]})
    write_audit_entry("update-task", {
        "task_gid": task["gid"], "name": task["name"], "changes": data,
    })


def cmd_move_task(args: argparse.Namespace) -> None:
    project_gid = resolver.project(args.project) if args.project else None
    task_gid = resolver.task(args.task, project_gid) if project_gid else args.task
    section_gid = resolver.section(args.section, project_gid) if project_gid else args.section

    data: dict[str, str] = {"task": task_gid}
    if getattr(args, "insert_before", None):
        data["insert_before"] = args.insert_before
    if getattr(args, "insert_after", None):
        data["insert_after"] = args.insert_after

    client.request("POST", f"/sections/{section_gid}/addTask", data)
    _print_json({"status": "moved", "task_gid": task_gid, "to_section": section_gid})
    write_audit_entry("move-task", {"task_gid": task_gid, "to_section": section_gid})


def cmd_complete_task(args: argparse.Namespace) -> None:
    task_gid, _ = _resolve_task_with_project(args)
    task = client.request("PUT", f"/tasks/{task_gid}", {"completed": True})["data"]
    _print_json({"status": "completed", "task_gid": task["gid"], "name": task["name"]})
    write_audit_entry("complete-task", {"task_gid": task["gid"], "name": task["name"]})


def cmd_delete_task(args: argparse.Namespace) -> None:
    task_gid, _ = _resolve_task_with_project(args)
    client.request("DELETE", f"/tasks/{task_gid}")
    _print_json({"status": "deleted", "task_gid": task_gid})
    write_audit_entry("delete-task", {"task_gid": task_gid})


def cmd_list_tasks(args: argparse.Namespace) -> None:
    fields = "name,completed,assignee.name,due_on,tags.name"

    if args.project and args.section:
        project_gid = resolver.project(args.project)
        section_gid = resolver.section(args.section, project_gid)
        result = client.request("GET", f"/sections/{section_gid}/tasks?opt_fields={fields}&limit=100")
        _print_json({"tasks": [_format_task(t) for t in result["data"]]})

    elif args.project:
        project_gid = resolver.project(args.project)
        sections = client.request("GET", f"/projects/{project_gid}/sections?opt_fields=name")["data"]
        all_tasks = []
        for sec in sections:
            result = client.request("GET", f"/sections/{sec['gid']}/tasks?opt_fields={fields}&limit=100")
            for task in result["data"]:
                all_tasks.append({**_format_task(task), "section": sec["name"]})
        _print_json({"tasks": all_tasks})

    elif args.section:
        result = client.request("GET", f"/sections/{args.section}/tasks?opt_fields={fields}&limit=100")
        _print_json({"tasks": [_format_task(t) for t in result["data"]]})

    else:
        sys.exit("Error: provide --project and/or --section")


# -- Tags --

def cmd_create_tag(args: argparse.Namespace) -> None:
    data: dict[str, str] = {"name": args.name, "workspace": workspace_gid}
    if args.color:
        data["color"] = args.color
    tag = client.request("POST", "/tags", data)["data"]
    _print_json({"status": "created", "tag_gid": tag["gid"], "name": tag["name"]})
    write_audit_entry("create-tag", {"tag_gid": tag["gid"], "name": tag["name"]})


def cmd_add_tag(args: argparse.Namespace) -> None:
    client.request("POST", f"/tasks/{args.task}/addTag", {"tag": args.tag})
    _print_json({"status": "tagged", "task_gid": args.task, "tag_gid": args.tag})
    write_audit_entry("add-tag", {"task_gid": args.task, "tag_gid": args.tag})


def cmd_remove_tag(args: argparse.Namespace) -> None:
    client.request("POST", f"/tasks/{args.task}/removeTag", {"tag": args.tag})
    _print_json({"status": "untagged", "task_gid": args.task, "tag_gid": args.tag})
    write_audit_entry("remove-tag", {"task_gid": args.task, "tag_gid": args.tag})


def cmd_list_tags(args: argparse.Namespace) -> None:
    result = client.request("GET", f"/workspaces/{workspace_gid}/tags?opt_fields=name,color&limit=100")
    _print_json({
        "tags": [{"gid": t["gid"], "name": t["name"], "color": t.get("color")} for t in result["data"]],
    })


def cmd_delete_tag(args: argparse.Namespace) -> None:
    client.request("DELETE", f"/tags/{args.tag}")
    _print_json({"status": "deleted", "tag_gid": args.tag})
    write_audit_entry("delete-tag", {"tag_gid": args.tag})


# -- Sections --

def cmd_create_section(args: argparse.Namespace) -> None:
    project_gid = resolver.project(args.project)
    section = client.request("POST", f"/projects/{project_gid}/sections", {"name": args.name})["data"]
    _print_json({"status": "created", "section_gid": section["gid"], "name": section["name"]})
    write_audit_entry("create-section", {
        "section_gid": section["gid"], "name": section["name"], "project": args.project,
    })


def cmd_list_sections(args: argparse.Namespace) -> None:
    project_gid = resolver.project(args.project)
    result = client.request("GET", f"/projects/{project_gid}/sections?opt_fields=name")
    _print_json({"sections": [{"gid": s["gid"], "name": s["name"]} for s in result["data"]]})


def cmd_rename_section(args: argparse.Namespace) -> None:
    project_gid = resolver.project(args.project)
    section_gid = resolver.section(args.section, project_gid)
    section = client.request("PUT", f"/sections/{section_gid}", {"name": args.name})["data"]
    _print_json({"status": "renamed", "section_gid": section["gid"], "name": section["name"]})
    write_audit_entry("rename-section", {
        "section_gid": section["gid"], "old_name": args.section, "new_name": args.name,
    })


def cmd_delete_section(args: argparse.Namespace) -> None:
    project_gid = resolver.project(args.project)
    section_gid = resolver.section(args.section, project_gid)
    client.request("DELETE", f"/sections/{section_gid}")
    _print_json({"status": "deleted", "section_gid": section_gid, "name": args.section})
    write_audit_entry("delete-section", {
        "section_gid": section_gid, "name": args.section, "project": args.project,
    })


# -- Projects --

def cmd_create_project(args: argparse.Namespace) -> None:
    data: dict[str, str] = {
        "name": args.name,
        "workspace": workspace_gid,
        "default_view": args.layout or "board",
    }
    if args.notes:
        data["notes"] = args.notes
    project = client.request("POST", "/projects", data)["data"]
    _print_json({
        "status": "created",
        "project_gid": project["gid"],
        "name": project["name"],
        "url": f"https://app.asana.com/0/{project['gid']}",
    })
    write_audit_entry("create-project", {"project_gid": project["gid"], "name": project["name"]})


def cmd_list_projects(args: argparse.Namespace) -> None:
    fields = "name,owner.name,archived,due_on"
    result = client.request("GET", f"/workspaces/{workspace_gid}/projects?opt_fields={fields}&limit=100")
    projects = result["data"]

    if args.archived:
        projects = [p for p in projects if p.get("archived")]
    elif not args.all:
        projects = [p for p in projects if not p.get("archived")]

    _print_json({
        "projects": [{
            "gid": p["gid"],
            "name": p["name"],
            "owner": (p.get("owner") or {}).get("name"),
            "archived": p.get("archived", False),
            "due_on": p.get("due_on"),
        } for p in projects],
    })


def cmd_archive_project(args: argparse.Namespace) -> None:
    gid = resolver.project(args.project)
    project = client.request("PUT", f"/projects/{gid}", {"archived": True})["data"]
    _print_json({"status": "archived", "project_gid": project["gid"], "name": project["name"]})
    write_audit_entry("archive-project", {"project_gid": project["gid"], "name": project["name"]})


def cmd_unarchive_project(args: argparse.Namespace) -> None:
    gid = resolver.project(args.project)
    project = client.request("PUT", f"/projects/{gid}", {"archived": False})["data"]
    _print_json({"status": "unarchived", "project_gid": project["gid"], "name": project["name"]})
    write_audit_entry("unarchive-project", {"project_gid": project["gid"], "name": project["name"]})


def cmd_delete_project(args: argparse.Namespace) -> None:
    if not args.confirm:
        sys.exit("Error: pass --confirm to permanently delete. This cannot be undone.")
    gid = resolver.project(args.project)
    client.request("DELETE", f"/projects/{gid}")
    _print_json({"status": "deleted", "project_gid": gid})
    write_audit_entry("delete-project", {"project_gid": gid})


# -- Members --

def cmd_list_members(args: argparse.Namespace) -> None:
    if args.project:
        gid = resolver.project(args.project)
        result = client.request("GET", f"/projects/{gid}/members?opt_fields=name,email")
        scope = f'project "{args.project}"'
    else:
        result = client.request("GET", f"/workspaces/{workspace_gid}/users?opt_fields=name,email")
        scope = "workspace"

    members = result["data"]
    _print_json({
        "members": [
            {"gid": m["gid"], "name": m.get("name", ""), "email": m.get("email", "")}
            for m in members
        ],
        "count": len(members),
        "scope": scope,
    })


def cmd_add_member(args: argparse.Namespace) -> None:
    client.request("POST", f"/workspaces/{workspace_gid}/addUser", {"user": args.email})
    _print_json({"status": "added", "email": args.email})
    write_audit_entry("add-member", {"email": args.email})


def cmd_remove_member(args: argparse.Namespace) -> None:
    identifier = args.user.strip()

    if "@" in identifier:
        user_ref = identifier
    else:
        result = client.request("GET", f"/workspaces/{workspace_gid}/users?opt_fields=name,email")
        users = result["data"]
        query = identifier.lower()

        # Try exact match on name or email
        match = next(
            (u for u in users
             if u.get("name", "").strip().lower() == query
             or u.get("email", "").strip().lower() == query),
            None,
        )

        # Fall back to partial match on name
        if not match:
            matches = [u for u in users if query in u.get("name", "").strip().lower()]
            if len(matches) == 1:
                match = matches[0]
            elif len(matches) > 1:
                options = "\n".join(f'  - {m["name"]} ({m.get("email", "")})' for m in matches)
                sys.exit(f'Error: "{identifier}" matches multiple users:\n{options}')

        if not match:
            sys.exit(f'Error: no user found matching "{identifier}". Run: list-members')

        user_ref = match["gid"]
        print(f'Removing {match["name"]} ({match.get("email", "")})...')

    client.request("POST", f"/workspaces/{workspace_gid}/removeUser", {"user": user_ref})
    _print_json({"status": "removed", "user": identifier})
    write_audit_entry("remove-member", {"user": identifier, "resolved_to": str(user_ref)})


def cmd_add_project_member(args: argparse.Namespace) -> None:
    gid = resolver.project(args.project)
    client.request("POST", f"/projects/{gid}/addMembers", {"members": [args.user]})
    _print_json({"status": "added", "user": args.user, "project": args.project})
    write_audit_entry("add-project-member", {"user": args.user, "project": args.project})


def cmd_remove_project_member(args: argparse.Namespace) -> None:
    gid = resolver.project(args.project)
    client.request("POST", f"/projects/{gid}/removeMembers", {"members": [args.user]})
    _print_json({"status": "removed", "user": args.user, "project": args.project})
    write_audit_entry("remove-project-member", {"user": args.user, "project": args.project})


# -- Audit Log --

def cmd_audit_log(args: argparse.Namespace) -> None:
    if not os.path.exists(AUDIT_LOG_PATH):
        print("No audit log yet. It will be created on the first write operation.")
        return

    with open(AUDIT_LOG_PATH) as f:
        entries = [json.loads(line) for line in f if line.strip()]

    if args.action:
        entries = [e for e in entries if args.action in e.get("action", "")]
    if args.since:
        entries = [e for e in entries if e.get("timestamp", "") >= args.since]
    if args.last:
        entries = entries[-args.last:]

    if args.clear:
        os.remove(AUDIT_LOG_PATH)
        print(f"Audit log cleared ({len(entries)} entries removed).")
        return

    if not entries:
        print("No matching entries.")
        return

    for entry in entries:
        timestamp = entry.pop("timestamp", "?")
        action = entry.pop("action", "?")
        details = ", ".join(f"{k}={v}" for k, v in entry.items())
        print(f"[{timestamp}] {action}: {details}")


# ── CLI Definition ────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="asana",
        description="Asana Manager CLI — manage your workspace from the terminal.",
        epilog=(
            "Examples:\n"
            '  asana create-task --project "MaestroOS" --name "Fix auth bug"\n'
            '  asana list-tasks --project "MaestroOS" --section "In Progress"\n'
            '  asana archive-project --project "Old Project"\n'
            "\n"
            "All identifiers accept names (case-insensitive, partial match) or GIDs."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── Tasks ──

    p = sub.add_parser("create-task", help="Create a new task")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.add_argument("--section", help="Target section name or GID")
    p.add_argument("--name", required=True, help="Task name")
    p.add_argument("--notes", default="", help="Task description")
    p.add_argument("--assignee", default="me", help="Assignee (default: me)")
    p.add_argument("--due-on", help="Due date (YYYY-MM-DD)")
    p.add_argument("--start-on", help="Start date (YYYY-MM-DD)")
    p.set_defaults(func=cmd_create_task)

    p = sub.add_parser("update-task", help="Update task properties")
    p.add_argument("--task", required=True, help="Task name or GID")
    p.add_argument("--project", help="Project context for name resolution")
    p.add_argument("--name", help="New name")
    p.add_argument("--notes", help="New description")
    p.add_argument("--assignee", help="New assignee")
    p.add_argument("--due-on", help="New due date (YYYY-MM-DD)")
    p.set_defaults(func=cmd_update_task)

    p = sub.add_parser("move-task", help="Move a task to a different section")
    p.add_argument("--task", required=True, help="Task name or GID")
    p.add_argument("--section", required=True, help="Target section name or GID")
    p.add_argument("--project", help="Project context for name resolution")
    p.add_argument("--insert-before", help="Place before this task GID")
    p.add_argument("--insert-after", help="Place after this task GID")
    p.set_defaults(func=cmd_move_task)

    p = sub.add_parser("complete-task", help="Mark a task as complete")
    p.add_argument("--task", required=True, help="Task name or GID")
    p.add_argument("--project", help="Project context for name resolution")
    p.set_defaults(func=cmd_complete_task)

    p = sub.add_parser("delete-task", help="Delete a task permanently")
    p.add_argument("--task", required=True, help="Task name or GID")
    p.add_argument("--project", help="Project context for name resolution")
    p.set_defaults(func=cmd_delete_task)

    p = sub.add_parser("list-tasks", help="List tasks in a project or section")
    p.add_argument("--project", help="Project name or GID")
    p.add_argument("--section", help="Section name or GID")
    p.set_defaults(func=cmd_list_tasks)

    # ── Tags ──

    p = sub.add_parser("create-tag", help="Create a new tag")
    p.add_argument("--name", required=True, help="Tag name")
    p.add_argument("--color", help="Tag color (e.g. dark-red, light-green)")
    p.set_defaults(func=cmd_create_tag)

    p = sub.add_parser("add-tag", help="Add a tag to a task")
    p.add_argument("--task", required=True, help="Task GID")
    p.add_argument("--tag", required=True, help="Tag GID")
    p.set_defaults(func=cmd_add_tag)

    p = sub.add_parser("remove-tag", help="Remove a tag from a task")
    p.add_argument("--task", required=True, help="Task GID")
    p.add_argument("--tag", required=True, help="Tag GID")
    p.set_defaults(func=cmd_remove_tag)

    p = sub.add_parser("list-tags", help="List all tags in the workspace")
    p.set_defaults(func=cmd_list_tags)

    p = sub.add_parser("delete-tag", help="Delete a tag")
    p.add_argument("--tag", required=True, help="Tag GID")
    p.set_defaults(func=cmd_delete_tag)

    # ── Sections ──

    p = sub.add_parser("create-section", help="Create a new section")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.add_argument("--name", required=True, help="Section name")
    p.set_defaults(func=cmd_create_section)

    p = sub.add_parser("list-sections", help="List sections in a project")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.set_defaults(func=cmd_list_sections)

    p = sub.add_parser("rename-section", help="Rename a section")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.add_argument("--section", required=True, help="Current section name or GID")
    p.add_argument("--name", required=True, help="New section name")
    p.set_defaults(func=cmd_rename_section)

    p = sub.add_parser("delete-section", help="Delete a section")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.add_argument("--section", required=True, help="Section name or GID")
    p.set_defaults(func=cmd_delete_section)

    # ── Projects ──

    p = sub.add_parser("create-project", help="Create a new project")
    p.add_argument("--name", required=True, help="Project name")
    p.add_argument("--layout", default="board", choices=["board", "list", "calendar"],
                   help="View layout (default: board)")
    p.add_argument("--notes", default="", help="Project description")
    p.set_defaults(func=cmd_create_project)

    p = sub.add_parser("list-projects", help="List projects in the workspace")
    p.add_argument("--archived", action="store_true", help="Show only archived projects")
    p.add_argument("--all", action="store_true", help="Show both active and archived")
    p.set_defaults(func=cmd_list_projects)

    p = sub.add_parser("archive-project", help="Archive a project")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.set_defaults(func=cmd_archive_project)

    p = sub.add_parser("unarchive-project", help="Restore an archived project")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.set_defaults(func=cmd_unarchive_project)

    p = sub.add_parser("delete-project", help="Permanently delete a project")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.add_argument("--confirm", action="store_true", help="Required to confirm deletion")
    p.set_defaults(func=cmd_delete_project)

    # ── Members ──

    p = sub.add_parser("list-members", help="List workspace or project members")
    p.add_argument("--project", help="Scope to a specific project")
    p.set_defaults(func=cmd_list_members)

    p = sub.add_parser("add-member", help="Invite a user to the workspace")
    p.add_argument("--email", required=True, help="User's email address")
    p.set_defaults(func=cmd_add_member)

    p = sub.add_parser("remove-member", help="Remove a user from the workspace")
    p.add_argument("--user", required=True, help="Email, name, or GID")
    p.set_defaults(func=cmd_remove_member)

    p = sub.add_parser("add-project-member", help="Add a user to a project")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.add_argument("--user", required=True, help="Email or GID")
    p.set_defaults(func=cmd_add_project_member)

    p = sub.add_parser("remove-project-member", help="Remove a user from a project")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.add_argument("--user", required=True, help="Email or GID")
    p.set_defaults(func=cmd_remove_project_member)

    # ── Audit Log ──

    p = sub.add_parser("audit-log", help="View the operation audit log")
    p.add_argument("--last", type=int, help="Show only the last N entries")
    p.add_argument("--action", help="Filter by action type (e.g. create-task)")
    p.add_argument("--since", help="Show entries since date (YYYY-MM-DD)")
    p.add_argument("--clear", action="store_true", help="Clear the entire log")
    p.set_defaults(func=cmd_audit_log)

    return parser


# ── Entry Point ───────────────────────────────────────────────────────────────


def main() -> None:
    global client, resolver, workspace_gid

    token = os.environ.get("ASANA_PAT", "")
    if not token:
        print("Error: ASANA_PAT environment variable is not set.", file=sys.stderr)
        print("Get a token at: https://app.asana.com/0/my-apps", file=sys.stderr)
        sys.exit(1)

    client = AsanaClient(token)

    # Resolve workspace: use env var or auto-detect from the user's account
    workspace_gid = os.environ.get("ASANA_WORKSPACE_GID", "")
    if not workspace_gid:
        result = client.request("GET", "/users/me?opt_fields=workspaces.gid,workspaces.name")
        workspaces = result["data"]["workspaces"]
        if not workspaces:
            sys.exit("Error: no workspaces found on this Asana account.")
        workspace_gid = workspaces[0]["gid"]

    resolver = NameResolver(client, workspace_gid)

    parser = _build_parser()
    args = parser.parse_args()

    try:
        args.func(args)
    except AsanaAPIError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except ResolutionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
