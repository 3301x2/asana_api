#!/usr/bin/env python3
"""
Asana Manager CLI — manage projects, tasks, sections, tags, and members.

Authenticates via ASANA_PAT env var. Optionally set ASANA_WORKSPACE_GID.
Supports both GIDs and human-friendly names (case-insensitive, partial match).

Usage:
  python asana_api.py create-task --project "MyProject" --section "To Do" --name "Fix bug"
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

# ── Config ────────────────────────────────────────────────────────────────────

PAT = os.environ.get("ASANA_PAT", "")
BASE_URL = "https://app.asana.com/api/1.0"
WORKSPACE_GID = os.environ.get("ASANA_WORKSPACE_GID", "")

if not PAT:
    print("ERROR: ASANA_PAT environment variable is not set.", file=sys.stderr)
    print("Get a token from: https://app.asana.com/0/my-apps", file=sys.stderr)
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {PAT}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

AUDIT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_log.jsonl")

# ── Caches ────────────────────────────────────────────────────────────────────

_project_cache = None
_section_cache = {}

# ── Helpers ───────────────────────────────────────────────────────────────────


def audit(action, details):
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "action": action, **details}
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def api(method, endpoint, data=None):
    url = f"{BASE_URL}{endpoint}"
    body = json.dumps({"data": data}).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            text = resp.read().decode()
            return json.loads(text) if text else {"data": {}}
    except urllib.error.HTTPError as e:
        print(f"ERROR {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


def out(data):
    print(json.dumps(data, indent=2))


def get_workspace():
    global WORKSPACE_GID
    if WORKSPACE_GID:
        return WORKSPACE_GID
    result = api("GET", "/users/me?opt_fields=workspaces.gid,workspaces.name")
    workspaces = result["data"]["workspaces"]
    if not workspaces:
        sys.exit("ERROR: No workspaces found")
    WORKSPACE_GID = workspaces[0]["gid"]
    return WORKSPACE_GID


# ── Name resolution ───────────────────────────────────────────────────────────


def _fuzzy_match(items, query, name_key="name", label="item"):
    """Case-insensitive exact then partial match. Returns matched item's GID."""
    q = query.strip().lower()
    for item in items:
        if item[name_key].strip().lower() == q:
            return item["gid"]
    matches = [i for i in items if q in i[name_key].strip().lower()]
    if len(matches) == 1:
        return matches[0]["gid"]
    if len(matches) > 1:
        lines = [f'  - "{m[name_key].strip()}" ({m["gid"]})' for m in matches]
        sys.exit(f'ERROR: "{query}" matches multiple {label}s:\n' + "\n".join(lines))
    return None


def resolve_project(identifier):
    if identifier and identifier.isdigit():
        return identifier
    global _project_cache
    if _project_cache is None:
        ws = get_workspace()
        _project_cache = api("GET", f"/workspaces/{ws}/projects?opt_fields=name,archived&limit=100")["data"]
    gid = _fuzzy_match(_project_cache, identifier, label="project")
    if not gid:
        sys.exit(f'ERROR: No project matching "{identifier}". Run: list-projects')
    return gid


def resolve_section(identifier, project_gid):
    if identifier and identifier.isdigit():
        return identifier
    if project_gid not in _section_cache:
        _section_cache[project_gid] = api("GET", f"/projects/{project_gid}/sections?opt_fields=name")["data"]
    sections = _section_cache[project_gid]
    gid = _fuzzy_match(sections, identifier, label="section")
    if not gid:
        avail = [f'  - "{s["name"].strip()}"' for s in sections]
        sys.exit(f'ERROR: No section matching "{identifier}". Available:\n' + "\n".join(avail))
    return gid


def resolve_task(identifier, project_gid=None):
    if identifier and identifier.isdigit() and len(identifier) > 8:
        return identifier
    if not project_gid:
        sys.exit("ERROR: --project is required when using task names.")
    tasks = api("GET", f"/projects/{project_gid}/tasks?opt_fields=name,completed&limit=100")["data"]
    gid = _fuzzy_match(tasks, identifier, label="task")
    if not gid:
        sys.exit(f'ERROR: No task matching "{identifier}" in project.')
    return gid


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_create_task(args):
    ws = get_workspace()
    project_gid = resolve_project(args.project)
    data = {"name": args.name, "workspace": ws}

    if args.section:
        section_gid = resolve_section(args.section, project_gid)
        data["memberships"] = [{"project": project_gid, "section": section_gid}]
    else:
        data["projects"] = [project_gid]

    for field, attr in [("notes", "notes"), ("assignee", "assignee"),
                        ("due_on", "due_on"), ("start_on", "start_on")]:
        val = getattr(args, attr, None)
        if val:
            data[field] = val

    task = api("POST", "/tasks", data)["data"]
    out({"status": "created", "task_gid": task["gid"], "name": task["name"],
         "url": f"https://app.asana.com/0/0/{task['gid']}/f"})
    audit("create-task", {"task_gid": task["gid"], "name": task["name"],
                          "project": args.project, "section": getattr(args, "section", None)})


def cmd_move_task(args):
    project_gid = resolve_project(args.project) if args.project else None
    task_gid = resolve_task(args.task, project_gid) if project_gid else args.task
    section_gid = resolve_section(args.section, project_gid) if project_gid else args.section

    data = {"task": task_gid}
    if getattr(args, "insert_before", None):
        data["insert_before"] = args.insert_before
    if getattr(args, "insert_after", None):
        data["insert_after"] = args.insert_after

    api("POST", f"/sections/{section_gid}/addTask", data)
    out({"status": "moved", "task_gid": task_gid, "to_section": section_gid})
    audit("move-task", {"task_gid": task_gid, "to_section": section_gid})


def cmd_update_task(args):
    project_gid = resolve_project(args.project) if args.project else None
    task_gid = resolve_task(args.task, project_gid) if project_gid else args.task

    data = {}
    for field, attr in [("name", "name"), ("notes", "notes"),
                        ("assignee", "assignee"), ("due_on", "due_on")]:
        val = getattr(args, attr, None)
        if val:
            data[field] = val

    if not data:
        sys.exit("ERROR: Provide at least one of --name, --notes, --assignee, --due-on")

    task = api("PUT", f"/tasks/{task_gid}", data)["data"]
    out({"status": "updated", "task_gid": task["gid"], "name": task["name"]})
    audit("update-task", {"task_gid": task["gid"], "name": task["name"], "changes": data})


def cmd_complete_task(args):
    project_gid = resolve_project(args.project) if args.project else None
    task_gid = resolve_task(args.task, project_gid) if project_gid else args.task
    task = api("PUT", f"/tasks/{task_gid}", {"completed": True})["data"]
    out({"status": "completed", "task_gid": task["gid"], "name": task["name"]})
    audit("complete-task", {"task_gid": task["gid"], "name": task["name"]})


def cmd_delete_task(args):
    project_gid = resolve_project(args.project) if args.project else None
    task_gid = resolve_task(args.task, project_gid) if project_gid else args.task
    api("DELETE", f"/tasks/{task_gid}")
    out({"status": "deleted", "task_gid": task_gid})
    audit("delete-task", {"task_gid": task_gid})


def cmd_list_tasks(args):
    opt = "name,completed,assignee.name,due_on,tags.name"

    if args.project and args.section:
        project_gid = resolve_project(args.project)
        section_gid = resolve_section(args.section, project_gid)
        tasks = api("GET", f"/sections/{section_gid}/tasks?opt_fields={opt}&limit=100")["data"]
        out({"tasks": [_fmt_task(t) for t in tasks]})

    elif args.project:
        project_gid = resolve_project(args.project)
        sections = api("GET", f"/projects/{project_gid}/sections?opt_fields=name")["data"]
        all_tasks = []
        for sec in sections:
            sec_tasks = api("GET", f"/sections/{sec['gid']}/tasks?opt_fields={opt}&limit=100")["data"]
            for t in sec_tasks:
                all_tasks.append({**_fmt_task(t), "section": sec["name"]})
        out({"tasks": all_tasks})

    elif args.section:
        tasks = api("GET", f"/sections/{args.section}/tasks?opt_fields={opt}&limit=100")["data"]
        out({"tasks": [_fmt_task(t) for t in tasks]})

    else:
        sys.exit("ERROR: Provide --project and/or --section")


def _fmt_task(t):
    return {
        "gid": t["gid"], "name": t["name"],
        "completed": t.get("completed", False),
        "assignee": (t.get("assignee") or {}).get("name"),
        "due_on": t.get("due_on"),
        "tags": [tag["name"] for tag in t.get("tags", [])],
    }


# ── Tags ──────────────────────────────────────────────────────────────────────


def cmd_create_tag(args):
    ws = get_workspace()
    data = {"name": args.name, "workspace": ws}
    if args.color:
        data["color"] = args.color
    tag = api("POST", "/tags", data)["data"]
    out({"status": "created", "tag_gid": tag["gid"], "name": tag["name"]})
    audit("create-tag", {"tag_gid": tag["gid"], "name": tag["name"]})


def cmd_add_tag(args):
    api("POST", f"/tasks/{args.task}/addTag", {"tag": args.tag})
    out({"status": "tagged", "task_gid": args.task, "tag_gid": args.tag})
    audit("add-tag", {"task_gid": args.task, "tag_gid": args.tag})


def cmd_remove_tag(args):
    api("POST", f"/tasks/{args.task}/removeTag", {"tag": args.tag})
    out({"status": "untagged", "task_gid": args.task, "tag_gid": args.tag})
    audit("remove-tag", {"task_gid": args.task, "tag_gid": args.tag})


def cmd_list_tags(args):
    ws = get_workspace()
    tags = api("GET", f"/workspaces/{ws}/tags?opt_fields=name,color&limit=100")["data"]
    out({"tags": [{"gid": t["gid"], "name": t["name"], "color": t.get("color")} for t in tags]})


def cmd_delete_tag(args):
    api("DELETE", f"/tags/{args.tag}")
    out({"status": "deleted", "tag_gid": args.tag})
    audit("delete-tag", {"tag_gid": args.tag})


# ── Sections ──────────────────────────────────────────────────────────────────


def cmd_create_section(args):
    project_gid = resolve_project(args.project)
    section = api("POST", f"/projects/{project_gid}/sections", {"name": args.name})["data"]
    out({"status": "created", "section_gid": section["gid"], "name": section["name"]})
    audit("create-section", {"section_gid": section["gid"], "name": section["name"], "project": args.project})


def cmd_list_sections(args):
    project_gid = resolve_project(args.project)
    sections = api("GET", f"/projects/{project_gid}/sections?opt_fields=name")["data"]
    out({"sections": [{"gid": s["gid"], "name": s["name"]} for s in sections]})


def cmd_rename_section(args):
    project_gid = resolve_project(args.project)
    section_gid = resolve_section(args.section, project_gid)
    section = api("PUT", f"/sections/{section_gid}", {"name": args.name})["data"]
    out({"status": "renamed", "section_gid": section["gid"], "name": section["name"]})
    audit("rename-section", {"section_gid": section["gid"], "old_name": args.section, "new_name": args.name})


def cmd_delete_section(args):
    project_gid = resolve_project(args.project)
    section_gid = resolve_section(args.section, project_gid)
    api("DELETE", f"/sections/{section_gid}")
    out({"status": "deleted", "section_gid": section_gid, "name": args.section})
    audit("delete-section", {"section_gid": section_gid, "name": args.section, "project": args.project})


# ── Projects ──────────────────────────────────────────────────────────────────


def cmd_create_project(args):
    ws = get_workspace()
    data = {"name": args.name, "workspace": ws, "default_view": args.layout or "board"}
    if args.notes:
        data["notes"] = args.notes
    project = api("POST", "/projects", data)["data"]
    out({"status": "created", "project_gid": project["gid"], "name": project["name"],
         "url": f"https://app.asana.com/0/{project['gid']}"})
    audit("create-project", {"project_gid": project["gid"], "name": project["name"]})


def cmd_list_projects(args):
    ws = get_workspace()
    projects = api("GET", f"/workspaces/{ws}/projects?opt_fields=name,owner.name,archived,due_on&limit=100")["data"]
    if args.archived:
        projects = [p for p in projects if p.get("archived")]
    elif not args.all:
        projects = [p for p in projects if not p.get("archived")]
    out({"projects": [{
        "gid": p["gid"], "name": p["name"],
        "owner": (p.get("owner") or {}).get("name"),
        "archived": p.get("archived", False), "due_on": p.get("due_on"),
    } for p in projects]})


def cmd_archive_project(args):
    gid = resolve_project(args.project)
    project = api("PUT", f"/projects/{gid}", {"archived": True})["data"]
    out({"status": "archived", "project_gid": project["gid"], "name": project["name"]})
    audit("archive-project", {"project_gid": project["gid"], "name": project["name"]})


def cmd_unarchive_project(args):
    gid = resolve_project(args.project)
    project = api("PUT", f"/projects/{gid}", {"archived": False})["data"]
    out({"status": "unarchived", "project_gid": project["gid"], "name": project["name"]})
    audit("unarchive-project", {"project_gid": project["gid"], "name": project["name"]})


def cmd_delete_project(args):
    gid = resolve_project(args.project)
    if not args.confirm:
        sys.exit("ERROR: Pass --confirm to permanently delete. This cannot be undone.")
    api("DELETE", f"/projects/{gid}")
    out({"status": "deleted", "project_gid": gid})
    audit("delete-project", {"project_gid": gid})


# ── Members ───────────────────────────────────────────────────────────────────


def cmd_list_members(args):
    if args.project:
        gid = resolve_project(args.project)
        members = api("GET", f"/projects/{gid}/members?opt_fields=name,email")["data"]
        label = f'project "{args.project}"'
    else:
        ws = get_workspace()
        members = api("GET", f"/workspaces/{ws}/users?opt_fields=name,email")["data"]
        label = "workspace"
    out({"members": [{"gid": m["gid"], "name": m.get("name", ""), "email": m.get("email", "")}
                     for m in members], "count": len(members), "scope": label})


def cmd_add_member(args):
    ws = get_workspace()
    api("POST", f"/workspaces/{ws}/addUser", {"user": args.email})
    out({"status": "added", "email": args.email, "workspace": ws})
    audit("add-member", {"email": args.email})


def cmd_remove_member(args):
    ws = get_workspace()
    identifier = args.user.strip()

    if "@" in identifier:
        user_ref = identifier
    else:
        users = api("GET", f"/workspaces/{ws}/users?opt_fields=name,email")["data"]
        q = identifier.lower()
        match = next((u for u in users if u.get("name", "").strip().lower() == q
                      or u.get("email", "").strip().lower() == q), None)
        if not match:
            matches = [u for u in users if q in u.get("name", "").strip().lower()]
            if len(matches) == 1:
                match = matches[0]
            elif len(matches) > 1:
                lines = [f'  - {m["name"]} ({m.get("email", "")})' for m in matches]
                sys.exit(f'ERROR: "{identifier}" matches multiple users:\n' + "\n".join(lines))
        if not match:
            sys.exit(f'ERROR: No user matching "{identifier}". Run: list-members')
        user_ref = match["gid"]
        print(f'Removing {match["name"]} ({match.get("email", "")})...')

    api("POST", f"/workspaces/{ws}/removeUser", {"user": user_ref})
    out({"status": "removed", "user": identifier, "workspace": ws})
    audit("remove-member", {"user": identifier, "resolved_to": str(user_ref)})


def cmd_add_project_member(args):
    gid = resolve_project(args.project)
    api("POST", f"/projects/{gid}/addMembers", {"members": [args.user]})
    out({"status": "added", "user": args.user, "project": args.project})
    audit("add-project-member", {"user": args.user, "project": args.project})


def cmd_remove_project_member(args):
    gid = resolve_project(args.project)
    api("POST", f"/projects/{gid}/removeMembers", {"members": [args.user]})
    out({"status": "removed", "user": args.user, "project": args.project})
    audit("remove-project-member", {"user": args.user, "project": args.project})


# ── Audit log ─────────────────────────────────────────────────────────────────


def cmd_audit_log(args):
    if not os.path.exists(AUDIT_LOG):
        print("No audit log yet.")
        return

    with open(AUDIT_LOG) as f:
        entries = [json.loads(line) for line in f if line.strip()]

    if args.action:
        entries = [e for e in entries if args.action in e.get("action", "")]
    if args.since:
        entries = [e for e in entries if e.get("timestamp", "") >= args.since]
    if args.last:
        entries = entries[-args.last:]

    if args.clear:
        os.remove(AUDIT_LOG)
        print(f"Audit log cleared ({len(entries)} entries).")
        return

    if not entries:
        print("No matching entries.")
        return

    for e in entries:
        ts, action = e.pop("timestamp", "?"), e.pop("action", "?")
        details = ", ".join(f"{k}={v}" for k, v in e.items())
        print(f"[{ts}] {action}: {details}")


# ── CLI parser ────────────────────────────────────────────────────────────────


def _add_common(p, project=False, section=False, task=False):
    if project:
        p.add_argument("--project", required=True, help="Project name or GID")
    if section:
        p.add_argument("--section", required=True, help="Section name or GID")
    if task:
        p.add_argument("--task", required=True, help="Task name (requires --project) or GID")


def main():
    parser = argparse.ArgumentParser(
        description="Asana Manager CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Tasks
    p = sub.add_parser("create-task", help="Create a task")
    _add_common(p, project=True)
    p.add_argument("--section", help="Section name or GID")
    p.add_argument("--name", required=True)
    p.add_argument("--notes", default="")
    p.add_argument("--assignee", default="me")
    p.add_argument("--due-on", help="YYYY-MM-DD")
    p.add_argument("--start-on", help="YYYY-MM-DD")
    p.set_defaults(func=cmd_create_task)

    p = sub.add_parser("move-task", help="Move a task to a section")
    p.add_argument("--task", required=True, help="Task name or GID")
    p.add_argument("--section", required=True, help="Target section name or GID")
    p.add_argument("--project", help="Project name or GID")
    p.add_argument("--insert-before")
    p.add_argument("--insert-after")
    p.set_defaults(func=cmd_move_task)

    p = sub.add_parser("update-task", help="Update a task")
    p.add_argument("--task", required=True, help="Task name or GID")
    p.add_argument("--project", help="Project name or GID")
    p.add_argument("--name")
    p.add_argument("--notes")
    p.add_argument("--assignee")
    p.add_argument("--due-on", help="YYYY-MM-DD")
    p.set_defaults(func=cmd_update_task)

    p = sub.add_parser("complete-task", help="Mark a task complete")
    p.add_argument("--task", required=True, help="Task name or GID")
    p.add_argument("--project", help="Project name or GID")
    p.set_defaults(func=cmd_complete_task)

    p = sub.add_parser("delete-task", help="Delete a task")
    p.add_argument("--task", required=True, help="Task name or GID")
    p.add_argument("--project", help="Project name or GID")
    p.set_defaults(func=cmd_delete_task)

    p = sub.add_parser("list-tasks", help="List tasks")
    p.add_argument("--section", help="Section name or GID")
    p.add_argument("--project", help="Project name or GID")
    p.set_defaults(func=cmd_list_tasks)

    # Tags
    p = sub.add_parser("create-tag", help="Create a tag")
    p.add_argument("--name", required=True)
    p.add_argument("--color", help="e.g. dark-red, dark-blue, light-green")
    p.set_defaults(func=cmd_create_tag)

    p = sub.add_parser("add-tag", help="Tag a task")
    p.add_argument("--task", required=True)
    p.add_argument("--tag", required=True)
    p.set_defaults(func=cmd_add_tag)

    p = sub.add_parser("remove-tag", help="Untag a task")
    p.add_argument("--task", required=True)
    p.add_argument("--tag", required=True)
    p.set_defaults(func=cmd_remove_tag)

    p = sub.add_parser("list-tags", help="List all tags")
    p.set_defaults(func=cmd_list_tags)

    p = sub.add_parser("delete-tag", help="Delete a tag")
    p.add_argument("--tag", required=True)
    p.set_defaults(func=cmd_delete_tag)

    # Sections
    p = sub.add_parser("create-section", help="Create a section")
    _add_common(p, project=True)
    p.add_argument("--name", required=True, help="Section name")
    p.set_defaults(func=cmd_create_section)

    p = sub.add_parser("list-sections", help="List sections")
    _add_common(p, project=True)
    p.set_defaults(func=cmd_list_sections)

    p = sub.add_parser("rename-section", help="Rename a section")
    _add_common(p, project=True, section=True)
    p.add_argument("--name", required=True, help="New name")
    p.set_defaults(func=cmd_rename_section)

    p = sub.add_parser("delete-section", help="Delete a section")
    _add_common(p, project=True, section=True)
    p.set_defaults(func=cmd_delete_section)

    # Projects
    p = sub.add_parser("create-project", help="Create a project")
    p.add_argument("--name", required=True)
    p.add_argument("--layout", default="board", help="board, list, or calendar")
    p.add_argument("--notes", default="")
    p.set_defaults(func=cmd_create_project)

    p = sub.add_parser("list-projects", help="List projects")
    p.add_argument("--archived", action="store_true", help="Show only archived")
    p.add_argument("--all", action="store_true", help="Show active + archived")
    p.set_defaults(func=cmd_list_projects)

    p = sub.add_parser("archive-project", help="Archive a project")
    _add_common(p, project=True)
    p.set_defaults(func=cmd_archive_project)

    p = sub.add_parser("unarchive-project", help="Unarchive a project")
    _add_common(p, project=True)
    p.set_defaults(func=cmd_unarchive_project)

    p = sub.add_parser("delete-project", help="Delete a project permanently")
    _add_common(p, project=True)
    p.add_argument("--confirm", action="store_true", help="Required for deletion")
    p.set_defaults(func=cmd_delete_project)

    # Members
    p = sub.add_parser("list-members", help="List workspace or project members")
    p.add_argument("--project", help="Project name or GID")
    p.set_defaults(func=cmd_list_members)

    p = sub.add_parser("add-member", help="Add user to workspace by email")
    p.add_argument("--email", required=True)
    p.set_defaults(func=cmd_add_member)

    p = sub.add_parser("remove-member", help="Remove user from workspace")
    p.add_argument("--user", required=True, help="Email, name, or GID")
    p.set_defaults(func=cmd_remove_member)

    p = sub.add_parser("add-project-member", help="Add user to a project")
    _add_common(p, project=True)
    p.add_argument("--user", required=True, help="Email or GID")
    p.set_defaults(func=cmd_add_project_member)

    p = sub.add_parser("remove-project-member", help="Remove user from a project")
    _add_common(p, project=True)
    p.add_argument("--user", required=True, help="Email or GID")
    p.set_defaults(func=cmd_remove_project_member)

    # Audit log
    p = sub.add_parser("audit-log", help="View audit log")
    p.add_argument("--last", type=int, help="Show last N entries")
    p.add_argument("--action", help="Filter by action")
    p.add_argument("--since", help="Entries since YYYY-MM-DD")
    p.add_argument("--clear", action="store_true", help="Clear the log")
    p.set_defaults(func=cmd_audit_log)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
