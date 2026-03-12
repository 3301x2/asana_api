# Asana Manager CLI

A zero-dependency Python CLI for managing Asana projects, tasks, sections, tags, and team members — all from the terminal.

Uses only the Python standard library (`urllib`, `argparse`, `json`) and the [Asana REST API](https://developers.asana.com/reference/rest-api-reference).

## Features

- **Projects** — create, list, archive, unarchive, delete
- **Tasks** — create, update, move, complete, delete, list (by section or entire project)
- **Sections** — create, list, rename, delete
- **Tags** — create, add/remove from tasks, list, delete
- **Members** — list workspace/project members, add/remove workspace members, add/remove project members
- **Name Resolution** — use human-friendly names instead of GIDs for projects, sections, and tasks (case-insensitive, partial match)
- **Audit Log** — every write operation is logged to `audit_log.jsonl`

## Setup

### 1. Get an Asana Personal Access Token

Go to [https://app.asana.com/0/my-apps](https://app.asana.com/0/my-apps) and create a Personal Access Token.

### 2. Set Environment Variables

```bash
export ASANA_PAT="your_personal_access_token"

# Optional: pin a specific workspace (auto-detected if not set)
export ASANA_WORKSPACE_GID="your_workspace_gid"
```

Or copy the example file:

```bash
cp .env.example .env
# Edit .env with your token, then:
source .env
```

### 3. Run

```bash
python asana_api.py <command> [options]
```

## Commands

### Projects

```bash
python asana_api.py list-projects
python asana_api.py list-projects --archived
python asana_api.py create-project --name "New Project" --layout board --notes "Description"
python asana_api.py archive-project --project "Old Project"
python asana_api.py unarchive-project --project "Old Project"
python asana_api.py delete-project --project "Old Project" --confirm
```

### Sections

```bash
python asana_api.py list-sections --project "MyProject"
python asana_api.py create-section --project "MyProject" --name "Backlog"
python asana_api.py rename-section --project "MyProject" --section "Backlog" --name "Archive"
python asana_api.py delete-section --project "MyProject" --section "Archive"
```

### Tasks

```bash
python asana_api.py create-task --project "MyProject" --section "To Do" --name "Fix bug" --notes "Details" --due-on 2025-12-31
python asana_api.py list-tasks --project "MyProject"
python asana_api.py list-tasks --project "MyProject" --section "In Progress"
python asana_api.py update-task --task "Fix bug" --project "MyProject" --name "Fix critical bug"
python asana_api.py move-task --task "Fix bug" --project "MyProject" --section "Done"
python asana_api.py complete-task --task "Fix bug" --project "MyProject"
python asana_api.py delete-task --task "Fix bug" --project "MyProject"
```

### Tags

```bash
python asana_api.py list-tags
python asana_api.py create-tag --name "urgent" --color dark-red
python asana_api.py add-tag --task <task_gid> --tag <tag_gid>
python asana_api.py remove-tag --task <task_gid> --tag <tag_gid>
python asana_api.py delete-tag --tag <tag_gid>
```

### Members

```bash
python asana_api.py list-members
python asana_api.py list-members --project "MyProject"
python asana_api.py add-member --email user@example.com
python asana_api.py remove-member --user "John Doe"
python asana_api.py add-project-member --project "MyProject" --user user@example.com
python asana_api.py remove-project-member --project "MyProject" --user user@example.com
```

### Audit Log

```bash
python asana_api.py audit-log
python asana_api.py audit-log --last 10
python asana_api.py audit-log --action create-task
python asana_api.py audit-log --since 2025-01-01
python asana_api.py audit-log --clear
```

## Name Resolution

You can use project, section, and task **names** instead of GIDs everywhere. The CLI performs case-insensitive matching with partial match support:

```bash
# These all work:
python asana_api.py list-tasks --project "MyProject"
python asana_api.py list-tasks --project "myproject"
python asana_api.py list-tasks --project "my"  # if unique partial match
```

When `--project` is provided, task names can be used with `--task` as well.

## Automation: GitHub-to-Asana Sync

Automatically create Asana boards and tickets from your GitHub commit history using GitHub Actions.

### What it does

- **Weekly sync (Fridays)**: Scans all your GitHub repos for new commits, groups them into logical tasks, and creates Asana tickets in the matching board
- **New repo detection (Mondays)**: Detects repos without Asana boards and auto-creates them with standard sections (Done, In Progress, Backlog)
- **Manual trigger**: Run either workflow on-demand from the Actions tab

### Setup

1. Go to your `asana_api` repo on GitHub → Settings → Secrets and variables → Actions

2. Add these **secrets**:
   - `ASANA_PAT` — Your Asana Personal Access Token
   - `ASANA_WORKSPACE_GID` — Your Asana workspace GID
   - `GH_TOKEN` — A GitHub PAT with `repo` scope (to read private repos)
   - `GITHUB_OWNER` — Your GitHub username (e.g. `3301x2`)

3. (Optional) Add a **variable** `SKIP_REPOS` with comma-separated repo names to exclude

4. Push this repo to GitHub — the workflows will activate automatically

### Running locally

```bash
export ASANA_PAT="your_token"
export ASANA_WORKSPACE_GID="your_workspace_gid"
export GH_TOKEN="your_github_token"
export GITHUB_OWNER="your_github_username"

python3 sync_commits.py
```

### How commits become tasks

- Conventional commits (`feat:`, `fix:`, `docs:`, etc.) are categorized automatically
- Commits with the same prefix and similar message are grouped into a single task
- Duplicate task names are skipped (safe to re-run)
- New tasks land in the **Backlog** section
- Sync state is persisted between runs to avoid re-processing

## Requirements

- Python 3.7+
- No external dependencies

## License

MIT
