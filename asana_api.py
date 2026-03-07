#!/usr/bin/env python3
"""
Asana Manager — A CLI tool for managing Asana tasks, projects, sections, and tags.

Authenticates via the ASANA_PAT environment variable (Personal Access Token).
Optionally set ASANA_WORKSPACE_GID to skip workspace auto-detection.

Supports both GIDs and human-friendly names for projects, sections, and tasks.

Examples:
  python asana_api.py create-task --project "MyProject" --section "To Do" --name "Fix bug"
  python asana_api.py list-tasks --project "MyProject" --section "In Progress"
  python asana_api.py list-tasks --project "MyProject"
  python asana_api.py archive-project --project "Old Project"
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

# --- Configuration ---
PAT = os.environ.get("ASANA_PAT", "")
BASE_URL = "https://app.asana.com/api/1.0"
WORKSPACE_GID = os.environ.get("ASANA_WORKSPACE_GID", "")

if not PAT:
    print("ERROR: ASANA_PAT environment variable is not set.", file=sys.stderr)
    print("Get a Personal Access Token from: https://app.asana.com/0/my-apps", file=sys.stderr)
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {PAT}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# --- Caches ---
_project_cache = None
_section_cache = {}

# --- Audit Log ---
AUDIT_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_log.jsonl")

def audit(action, details):
    """Append an entry to the audit log."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        **details,
    }
    try:
        with open(AUDIT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # Don't fail the command if logging fails


def api_request(method, endpoint, data=None):
    """Make an authenticated request to the Asana API."""
    url = f"{BASE_URL}{endpoint}"
    body = None
    if data is not None:
        body = json.dumps({"data": data}).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=HEADERS, method=method)

    try:
        with urllib.request.urlopen(req) as resp:
            response_body = resp.read().decode("utf-8")
            if response_body:
                return json.loads(response_body)
            return {"data": {}}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"ERROR {e.code}: {error_body}", file=sys.stderr)
        sys.exit(1)


def get_workspace_gid():
    """Get the user's workspace GID."""
    global WORKSPACE_GID
    if WORKSPACE_GID:
        return WORKSPACE_GID
    result = api_request("GET", "/users/me?opt_fields=workspaces.gid,workspaces.name")
    workspaces = result["data"]["workspaces"]
    if not workspaces:
        print("ERROR: No workspaces found", file=sys.stderr)
        sys.exit(1)
    WORKSPACE_GID = workspaces[0]["gid"]
    return WORKSPACE_GID


def resolve_project(identifier):
    """Resolve a project name or GID to a GID. Case-insensitive, partial match."""
    if identifier and identifier.isdigit():
        return identifier

    global _project_cache
    if _project_cache is None:
        workspace = get_workspace_gid()
        result = api_request("GET", f"/workspaces/{workspace}/projects?opt_fields=name,archived&limit=100")
        _project_cache = result["data"]

    query = identifier.strip().lower()

    # Exact match first (case-insensitive)
    for p in _project_cache:
        if p["name"].strip().lower() == query:
            return p["gid"]

    # Partial match (contains)
    matches = [p for p in _project_cache if query in p["name"].strip().lower()]
    if len(matches) == 1:
        return matches[0]["gid"]
    elif len(matches) > 1:
        names = [f'  - "{m["name"].strip()}" ({m["gid"]})' for m in matches]
        print(f'ERROR: "{identifier}" matches multiple projects:\n' + "\n".join(names), file=sys.stderr)
        sys.exit(1)

    print(f'ERROR: No project found matching "{identifier}". Run: asana list-projects', file=sys.stderr)
    sys.exit(1)


def resolve_section(section_identifier, project_gid):
    """Resolve a section name or GID to a GID within a project."""
    if section_identifier and section_identifier.isdigit():
        return section_identifier

    if project_gid not in _section_cache:
        result = api_request("GET", f"/projects/{project_gid}/sections?opt_fields=name")
        _section_cache[project_gid] = result["data"]

    sections = _section_cache[project_gid]
    query = section_identifier.strip().lower()

    # Exact match first
    for s in sections:
        if s["name"].strip().lower() == query:
            return s["gid"]

    # Partial match
    matches = [s for s in sections if query in s["name"].strip().lower()]
    if len(matches) == 1:
        return matches[0]["gid"]
    elif len(matches) > 1:
        names = [f'  - "{s["name"].strip()}" ({s["gid"]})' for s in matches]
        print(f'ERROR: "{section_identifier}" matches multiple sections:\n' + "\n".join(names), file=sys.stderr)
        sys.exit(1)

    available = [f'  - "{s["name"].strip()}"' for s in sections]
    print(f'ERROR: No section found matching "{section_identifier}". Available:\n' + "\n".join(available), file=sys.stderr)
    sys.exit(1)


def resolve_task(task_identifier, project_gid=None):
    """Resolve a task name or GID to a GID. Requires --project for name resolution."""
    if task_identifier and task_identifier.isdigit() and len(task_identifier) > 8:
        return task_identifier

    if not project_gid:
        print('ERROR: --project is required when using task names instead of GIDs.', file=sys.stderr)
        sys.exit(1)

    # Get all tasks across all sections of this project
    result = api_request("GET", f"/projects/{project_gid}/tasks?opt_fields=name,completed&limit=100")
    tasks = result["data"]

    query = task_identifier.strip().lower()

    # Exact match first (case-insensitive)
    for t in tasks:
        if t["name"].strip().lower() == query:
            return t["gid"]

    # Partial match (contains)
    matches = [t for t in tasks if query in t["name"].strip().lower()]
    if len(matches) == 1:
        return matches[0]["gid"]
    elif len(matches) > 1:
        names = [f'  - "{m["name"].strip()}" ({m["gid"]})' for m in matches[:10]]
        print(f'ERROR: "{task_identifier}" matches multiple tasks:\n' + "\n".join(names), file=sys.stderr)
        sys.exit(1)

    print(f'ERROR: No task found matching "{task_identifier}" in project.', file=sys.stderr)
    sys.exit(1)


def create_task(args):
    """Create a task in a specific project and section."""
    workspace = get_workspace_gid()
    project_gid = resolve_project(args.project)
    data = {"name": args.name, "workspace": workspace}

    if args.section:
        section_gid = resolve_section(args.section, project_gid)
        data["memberships"] = [
            {"project": project_gid, "section": section_gid}
        ]
    else:
        data["projects"] = [project_gid]

    if args.notes:
        data["notes"] = args.notes
    if args.assignee:
        data["assignee"] = args.assignee
    if args.due_on:
        data["due_on"] = args.due_on
    if args.start_on:
        data["start_on"] = args.start_on

    result = api_request("POST", "/tasks", data)
    task = result["data"]
    print(json.dumps({
        "status": "created",
        "task_gid": task["gid"],
        "name": task["name"],
        "url": f"https://app.asana.com/0/0/{task['gid']}/f"
    }, indent=2))
    audit("create-task", {"task_gid": task["gid"], "name": task["name"], "project": args.project, "section": getattr(args, "section", None)})
    return task


def move_task(args):
    """Move a task to a different section."""
    project_gid = resolve_project(args.project) if args.project else None
    task_gid = resolve_task(args.task, project_gid) if project_gid else args.task
    section_gid = resolve_section(args.section, project_gid) if project_gid else args.section

    data = {"task": task_gid}
    if hasattr(args, "insert_before") and args.insert_before:
        data["insert_before"] = args.insert_before
    if hasattr(args, "insert_after") and args.insert_after:
        data["insert_after"] = args.insert_after

    result = api_request("POST", f"/sections/{section_gid}/addTask", data)
    print(json.dumps({
        "status": "moved",
        "task_gid": task_gid,
        "to_section": section_gid
    }, indent=2))
    audit("move-task", {"task_gid": task_gid, "to_section": section_gid, "project": getattr(args, "project", None)})


def create_tag(args):
    """Create a new tag in the workspace."""
    workspace = get_workspace_gid()
    data = {"name": args.name, "workspace": workspace}
    if args.color:
        data["color"] = args.color

    result = api_request("POST", "/tags", data)
    tag = result["data"]
    print(json.dumps({
        "status": "created",
        "tag_gid": tag["gid"],
        "name": tag["name"]
    }, indent=2))
    audit("create-tag", {"tag_gid": tag["gid"], "name": tag["name"]})
    return tag


def add_tag(args):
    """Add a tag to a task."""
    data = {"tag": args.tag}
    result = api_request("POST", f"/tasks/{args.task}/addTag", data)
    print(json.dumps({
        "status": "tagged",
        "task_gid": args.task,
        "tag_gid": args.tag
    }, indent=2))
    audit("add-tag", {"task_gid": args.task, "tag_gid": args.tag})


def remove_tag(args):
    """Remove a tag from a task."""
    data = {"tag": args.tag}
    result = api_request("POST", f"/tasks/{args.task}/removeTag", data)
    print(json.dumps({
        "status": "untagged",
        "task_gid": args.task,
        "tag_gid": args.tag
    }, indent=2))
    audit("remove-tag", {"task_gid": args.task, "tag_gid": args.tag})


def list_tags(args):
    """List all tags in the workspace."""
    workspace = get_workspace_gid()
    result = api_request("GET", f"/workspaces/{workspace}/tags?opt_fields=name,color&limit=100")
    tags = result["data"]
    print(json.dumps({"tags": [{"gid": t["gid"], "name": t["name"], "color": t.get("color")} for t in tags]}, indent=2))


def delete_tag(args):
    """Delete a tag."""
    result = api_request("DELETE", f"/tags/{args.tag}")
    print(json.dumps({"status": "deleted", "tag_gid": args.tag}, indent=2))
    audit("delete-tag", {"tag_gid": args.tag})


def update_task(args):
    """Update a task's name, notes, assignee, or due date."""
    project_gid = resolve_project(args.project) if args.project else None
    task_gid = resolve_task(args.task, project_gid) if project_gid else args.task

    data = {}
    if args.name:
        data["name"] = args.name
    if args.notes:
        data["notes"] = args.notes
    if args.assignee:
        data["assignee"] = args.assignee
    if args.due_on:
        data["due_on"] = args.due_on

    if not data:
        print("ERROR: Provide at least one of --name, --notes, --assignee, --due-on", file=sys.stderr)
        sys.exit(1)

    result = api_request("PUT", f"/tasks/{task_gid}", data)
    task = result["data"]
    print(json.dumps({
        "status": "updated",
        "task_gid": task["gid"],
        "name": task["name"]
    }, indent=2))
    audit("update-task", {"task_gid": task["gid"], "name": task["name"], "changes": data})


def list_sections(args):
    """List sections in a project."""
    project_gid = resolve_project(args.project)
    result = api_request("GET", f"/projects/{project_gid}/sections?opt_fields=name")
    sections = result["data"]
    print(json.dumps({"sections": [{"gid": s["gid"], "name": s["name"]} for s in sections]}, indent=2))


def list_tasks(args):
    """List tasks in a section or all tasks in a project."""
    if args.project and args.section:
        project_gid = resolve_project(args.project)
        section_gid = resolve_section(args.section, project_gid)
    elif args.section:
        section_gid = args.section
    elif args.project:
        # List all tasks across all sections in the project
        project_gid = resolve_project(args.project)
        result = api_request("GET", f"/projects/{project_gid}/sections?opt_fields=name")
        sections = result["data"]
        all_tasks = []
        opt = "name,completed,assignee.name,due_on,tags.name"
        for section in sections:
            sec_result = api_request("GET", f"/sections/{section['gid']}/tasks?opt_fields={opt}&limit=100")
            for t in sec_result["data"]:
                all_tasks.append({
                    "gid": t["gid"],
                    "name": t["name"],
                    "section": section["name"],
                    "completed": t.get("completed", False),
                    "assignee": t.get("assignee", {}).get("name") if t.get("assignee") else None,
                    "due_on": t.get("due_on"),
                    "tags": [tag["name"] for tag in t.get("tags", [])]
                })
        print(json.dumps({"tasks": all_tasks}, indent=2))
        return
    else:
        print("ERROR: Provide --section (and optionally --project), or just --project to list all tasks", file=sys.stderr)
        sys.exit(1)

    opt = "name,completed,assignee.name,due_on,tags.name"
    result = api_request("GET", f"/sections/{section_gid}/tasks?opt_fields={opt}&limit=100")
    tasks = result["data"]
    print(json.dumps({"tasks": [{
        "gid": t["gid"],
        "name": t["name"],
        "completed": t.get("completed", False),
        "assignee": t.get("assignee", {}).get("name") if t.get("assignee") else None,
        "due_on": t.get("due_on"),
        "tags": [tag["name"] for tag in t.get("tags", [])]
    } for t in tasks]}, indent=2))


def delete_task(args):
    """Delete a task."""
    project_gid = resolve_project(args.project) if args.project else None
    task_gid = resolve_task(args.task, project_gid) if project_gid else args.task
    result = api_request("DELETE", f"/tasks/{task_gid}")
    print(json.dumps({"status": "deleted", "task_gid": task_gid}, indent=2))
    audit("delete-task", {"task_gid": task_gid})


def complete_task(args):
    """Mark a task as complete."""
    project_gid = resolve_project(args.project) if args.project else None
    task_gid = resolve_task(args.task, project_gid) if project_gid else args.task
    data = {"completed": True}
    result = api_request("PUT", f"/tasks/{task_gid}", data)
    task = result["data"]
    print(json.dumps({
        "status": "completed",
        "task_gid": task["gid"],
        "name": task["name"]
    }, indent=2))
    audit("complete-task", {"task_gid": task["gid"], "name": task["name"]})


def create_project(args):
    """Create a new project in the workspace."""
    workspace = get_workspace_gid()
    data = {
        "name": args.name,
        "workspace": workspace,
        "default_view": args.layout or "board",
    }
    if args.notes:
        data["notes"] = args.notes

    result = api_request("POST", "/projects", data)
    project = result["data"]
    print(json.dumps({
        "status": "created",
        "project_gid": project["gid"],
        "name": project["name"],
        "url": f"https://app.asana.com/0/{project['gid']}"
    }, indent=2))
    audit("create-project", {"project_gid": project["gid"], "name": project["name"]})
    return project


def create_section(args):
    """Create a new section in a project."""
    project_gid = resolve_project(args.project)
    data = {"name": args.name}

    result = api_request("POST", f"/projects/{project_gid}/sections", data)
    section = result["data"]
    print(json.dumps({
        "status": "created",
        "section_gid": section["gid"],
        "name": section["name"]
    }, indent=2))
    audit("create-section", {"section_gid": section["gid"], "name": section["name"], "project": args.project})
    return section


def list_projects(args):
    """List all projects in the workspace."""
    workspace = get_workspace_gid()
    opt = "name,owner.name,archived,due_on"
    result = api_request("GET", f"/workspaces/{workspace}/projects?opt_fields={opt}&limit=100")
    projects = result["data"]

    if args.archived:
        projects = [p for p in projects if p.get("archived")]
    elif not args.all:
        projects = [p for p in projects if not p.get("archived")]

    print(json.dumps({"projects": [{
        "gid": p["gid"],
        "name": p["name"],
        "owner": p.get("owner", {}).get("name") if p.get("owner") else None,
        "archived": p.get("archived", False),
        "due_on": p.get("due_on"),
    } for p in projects]}, indent=2))


def archive_project(args):
    """Archive a project."""
    project_gid = resolve_project(args.project)
    data = {"archived": True}
    result = api_request("PUT", f"/projects/{project_gid}", data)
    project = result["data"]
    print(json.dumps({
        "status": "archived",
        "project_gid": project["gid"],
        "name": project["name"]
    }, indent=2))
    audit("archive-project", {"project_gid": project["gid"], "name": project["name"]})


def unarchive_project(args):
    """Unarchive a project."""
    project_gid = resolve_project(args.project)
    data = {"archived": False}
    result = api_request("PUT", f"/projects/{project_gid}", data)
    project = result["data"]
    print(json.dumps({
        "status": "unarchived",
        "project_gid": project["gid"],
        "name": project["name"]
    }, indent=2))
    audit("unarchive-project", {"project_gid": project["gid"], "name": project["name"]})


def delete_project(args):
    """Delete a project permanently."""
    project_gid = resolve_project(args.project)
    if not args.confirm:
        print("ERROR: Pass --confirm to permanently delete this project. This cannot be undone.", file=sys.stderr)
        sys.exit(1)
    result = api_request("DELETE", f"/projects/{project_gid}")
    print(json.dumps({"status": "deleted", "project_gid": project_gid}, indent=2))
    audit("delete-project", {"project_gid": project_gid})


def list_members(args):
    """List all members in the workspace, or in a specific project."""
    if args.project:
        project_gid = resolve_project(args.project)
        result = api_request("GET", f"/projects/{project_gid}/members?opt_fields=name,email")
        label = f'project "{args.project}"'
    else:
        workspace = get_workspace_gid()
        result = api_request("GET", f"/workspaces/{workspace}/users?opt_fields=name,email")
        label = "workspace"

    members = result["data"]
    print(json.dumps({"members": [{
        "gid": m["gid"],
        "name": m.get("name", ""),
        "email": m.get("email", ""),
    } for m in members], "count": len(members), "scope": label}, indent=2))


def delete_section(args):
    """Delete a section from a project."""
    project_gid = resolve_project(args.project)
    section_gid = resolve_section(args.section, project_gid)
    result = api_request("DELETE", f"/sections/{section_gid}")
    print(json.dumps({"status": "deleted", "section_gid": section_gid, "name": args.section}, indent=2))
    audit("delete-section", {"section_gid": section_gid, "name": args.section, "project": args.project})


def rename_section(args):
    """Rename a section in a project."""
    project_gid = resolve_project(args.project)
    section_gid = resolve_section(args.section, project_gid)
    data = {"name": args.name}
    result = api_request("PUT", f"/sections/{section_gid}", data)
    section = result["data"]
    print(json.dumps({"status": "renamed", "section_gid": section["gid"], "name": section["name"]}, indent=2))
    audit("rename-section", {"section_gid": section["gid"], "old_name": args.section, "new_name": args.name})


def add_project_member(args):
    """Add a user to a project."""
    project_gid = resolve_project(args.project)
    data = {"members": [args.user]}
    result = api_request("POST", f"/projects/{project_gid}/addMembers", data)
    print(json.dumps({"status": "added", "user": args.user, "project": args.project}, indent=2))
    audit("add-project-member", {"user": args.user, "project": args.project, "project_gid": project_gid})


def remove_project_member(args):
    """Remove a user from a project."""
    project_gid = resolve_project(args.project)
    data = {"members": [args.user]}
    result = api_request("POST", f"/projects/{project_gid}/removeMembers", data)
    print(json.dumps({"status": "removed", "user": args.user, "project": args.project}, indent=2))
    audit("remove-project-member", {"user": args.user, "project": args.project, "project_gid": project_gid})


def add_member(args):
    """Add a user to the workspace by email."""
    workspace = get_workspace_gid()
    data = {"user": args.email}
    result = api_request("POST", f"/workspaces/{workspace}/addUser", data)
    print(json.dumps({
        "status": "added",
        "email": args.email,
        "workspace": workspace
    }, indent=2))
    audit("add-member", {"email": args.email})


def remove_member(args):
    """Remove a user from the workspace by email or name."""
    workspace = get_workspace_gid()

    # Resolve email: if it looks like an email, use directly; otherwise search by name
    identifier = args.user.strip()
    if "@" in identifier:
        user_ref = identifier
    else:
        # Search workspace users by name
        result = api_request("GET", f"/workspaces/{workspace}/users?opt_fields=name,email")
        users = result["data"]
        query = identifier.lower()

        # Exact match first
        match = None
        for u in users:
            if u.get("name", "").strip().lower() == query or u.get("email", "").strip().lower() == query:
                match = u
                break

        # Partial match
        if not match:
            matches = [u for u in users if query in u.get("name", "").strip().lower()]
            if len(matches) == 1:
                match = matches[0]
            elif len(matches) > 1:
                names = [f'  - {m["name"]} ({m.get("email", "no email")})' for m in matches]
                print(f'ERROR: "{identifier}" matches multiple users:\n' + "\n".join(names), file=sys.stderr)
                sys.exit(1)

        if not match:
            print(f'ERROR: No user found matching "{identifier}". Run: asana list-members', file=sys.stderr)
            sys.exit(1)

        user_ref = match["gid"]
        print(f'Removing {match["name"]} ({match.get("email", "")})...')

    data = {"user": user_ref}
    result = api_request("POST", f"/workspaces/{workspace}/removeUser", data)
    print(json.dumps({
        "status": "removed",
        "user": identifier,
        "workspace": workspace
    }, indent=2))
    audit("remove-member", {"user": identifier, "resolved_to": str(user_ref)})


def show_audit_log(args):
    """Display the audit log, optionally filtered."""
    if not os.path.exists(AUDIT_LOG_PATH):
        print("No audit log found yet. It will be created on the next write operation.")
        return

    entries = []
    with open(AUDIT_LOG_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    # Filter by action
    if args.action:
        entries = [e for e in entries if args.action in e.get("action", "")]

    # Filter by date
    if args.since:
        entries = [e for e in entries if e.get("timestamp", "") >= args.since]

    # Show last N
    if args.last:
        entries = entries[-args.last:]

    if args.clear:
        os.remove(AUDIT_LOG_PATH)
        print(f"Audit log cleared ({len(entries)} entries removed).")
        return

    if not entries:
        print("No matching entries.")
        return

    for e in entries:
        ts = e.pop("timestamp", "?")
        action = e.pop("action", "?")
        details = ", ".join(f"{k}={v}" for k, v in e.items())
        print(f"[{ts}] {action}: {details}")


def main():
    parser = argparse.ArgumentParser(
        description="Asana Manager CLI",
        epilog='Projects and sections accept names or GIDs. Examples:\n'
               '  asana create-task --project "MaestroOS" --section "To Do" --name "Fix bug"\n'
               '  asana list-tasks --project "MaestroOS" --section "In Progress"\n'
               '  asana list-tasks --project "MaestroOS"  (all tasks)\n'
               '  asana archive-project --project "Enigma Forge"',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # create-task
    p = subparsers.add_parser("create-task", help="Create a task")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.add_argument("--section", help="Section name or GID")
    p.add_argument("--name", required=True)
    p.add_argument("--notes", default="")
    p.add_argument("--assignee", default="me")
    p.add_argument("--due-on", help="YYYY-MM-DD")
    p.add_argument("--start-on", help="YYYY-MM-DD")
    p.set_defaults(func=create_task)

    # move-task
    p = subparsers.add_parser("move-task", help="Move a task to a section")
    p.add_argument("--task", required=True, help="Task name (requires --project) or GID")
    p.add_argument("--section", required=True, help="Section name (requires --project) or GID")
    p.add_argument("--project", help="Project name or GID (enables name lookup)")
    p.add_argument("--insert-before")
    p.add_argument("--insert-after")
    p.set_defaults(func=move_task)

    # update-task
    p = subparsers.add_parser("update-task", help="Update a task")
    p.add_argument("--task", required=True, help="Task name (requires --project) or GID")
    p.add_argument("--project", help="Project name or GID (enables name lookup)")
    p.add_argument("--name")
    p.add_argument("--notes")
    p.add_argument("--assignee")
    p.add_argument("--due-on", help="YYYY-MM-DD")
    p.set_defaults(func=update_task)

    # create-tag
    p = subparsers.add_parser("create-tag", help="Create a tag")
    p.add_argument("--name", required=True)
    p.add_argument("--color", default=None, help="e.g. dark-red, dark-blue, light-green, none")
    p.set_defaults(func=create_tag)

    # add-tag
    p = subparsers.add_parser("add-tag", help="Tag a task")
    p.add_argument("--task", required=True)
    p.add_argument("--tag", required=True)
    p.set_defaults(func=add_tag)

    # remove-tag
    p = subparsers.add_parser("remove-tag", help="Untag a task")
    p.add_argument("--task", required=True)
    p.add_argument("--tag", required=True)
    p.set_defaults(func=remove_tag)

    # list-tags
    p = subparsers.add_parser("list-tags", help="List all tags")
    p.set_defaults(func=list_tags)

    # delete-tag
    p = subparsers.add_parser("delete-tag", help="Delete a tag")
    p.add_argument("--tag", required=True)
    p.set_defaults(func=delete_tag)

    # list-sections
    p = subparsers.add_parser("list-sections", help="List sections in a project")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.set_defaults(func=list_sections)

    # list-tasks
    p = subparsers.add_parser("list-tasks", help="List tasks")
    p.add_argument("--section", help="Section name (requires --project) or GID")
    p.add_argument("--project", help="Project name or GID (list all tasks if no section)")
    p.set_defaults(func=list_tasks)

    # delete-task
    p = subparsers.add_parser("delete-task", help="Delete a task")
    p.add_argument("--task", required=True, help="Task name (requires --project) or GID")
    p.add_argument("--project", help="Project name or GID (enables name lookup)")
    p.set_defaults(func=delete_task)

    # complete-task
    p = subparsers.add_parser("complete-task", help="Mark a task complete")
    p.add_argument("--task", required=True, help="Task name (requires --project) or GID")
    p.add_argument("--project", help="Project name or GID (enables name lookup)")
    p.set_defaults(func=complete_task)

    # list-projects
    p = subparsers.add_parser("list-projects", help="List all projects")
    p.add_argument("--archived", action="store_true", help="Show only archived")
    p.add_argument("--all", action="store_true", help="Show active + archived")
    p.set_defaults(func=list_projects)

    # create-project
    p = subparsers.add_parser("create-project", help="Create a new project")
    p.add_argument("--name", required=True, help="Project name")
    p.add_argument("--layout", default="board", help="View layout: board, list, calendar (default: board)")
    p.add_argument("--notes", default="", help="Project description")
    p.set_defaults(func=create_project)

    # create-section
    p = subparsers.add_parser("create-section", help="Create a section in a project")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.add_argument("--name", required=True, help="Section name")
    p.set_defaults(func=create_section)

    # archive-project
    p = subparsers.add_parser("archive-project", help="Archive a project")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.set_defaults(func=archive_project)

    # unarchive-project
    p = subparsers.add_parser("unarchive-project", help="Unarchive a project")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.set_defaults(func=unarchive_project)

    # delete-project
    p = subparsers.add_parser("delete-project", help="Permanently delete a project")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.add_argument("--confirm", action="store_true", help="Required to confirm deletion")
    p.set_defaults(func=delete_project)

    # list-members
    p = subparsers.add_parser("list-members", help="List workspace or project members")
    p.add_argument("--project", help="Project name or GID (omit for workspace members)")
    p.set_defaults(func=list_members)

    # delete-section
    p = subparsers.add_parser("delete-section", help="Delete a section from a project")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.add_argument("--section", required=True, help="Section name or GID")
    p.set_defaults(func=delete_section)

    # rename-section
    p = subparsers.add_parser("rename-section", help="Rename a section")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.add_argument("--section", required=True, help="Current section name or GID")
    p.add_argument("--name", required=True, help="New section name")
    p.set_defaults(func=rename_section)

    # add-project-member
    p = subparsers.add_parser("add-project-member", help="Add a user to a project")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.add_argument("--user", required=True, help="User email or GID")
    p.set_defaults(func=add_project_member)

    # remove-project-member
    p = subparsers.add_parser("remove-project-member", help="Remove a user from a project")
    p.add_argument("--project", required=True, help="Project name or GID")
    p.add_argument("--user", required=True, help="User email or GID")
    p.set_defaults(func=remove_project_member)

    # add-member
    p = subparsers.add_parser("add-member", help="Add a user to the workspace by email")
    p.add_argument("--email", required=True, help="User's email address")
    p.set_defaults(func=add_member)

    # remove-member
    p = subparsers.add_parser("remove-member", help="Remove a user from the workspace")
    p.add_argument("--user", required=True, help="User's email, name, or GID")
    p.set_defaults(func=remove_member)

    # audit-log
    p = subparsers.add_parser("audit-log", help="View the audit log of all changes")
    p.add_argument("--last", type=int, help="Show last N entries")
    p.add_argument("--action", help="Filter by action (e.g. create-task, remove-member)")
    p.add_argument("--since", help="Show entries since date (YYYY-MM-DD)")
    p.add_argument("--clear", action="store_true", help="Clear the audit log")
    p.set_defaults(func=show_audit_log)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
