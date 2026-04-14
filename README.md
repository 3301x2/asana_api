# Asana CLI

A zero-dependency Python CLI for managing Asana workspaces from the terminal. Built entirely on the Python standard library and the [Asana REST API](https://developers.asana.com/reference/rest-api-reference).

Includes a GitHub Actions integration that automatically syncs commit history to Asana boards on a weekly schedule.

## Features

| Category         | Operations                                                          |
|------------------|---------------------------------------------------------------------|
| **Projects**     | Create, get, update, list, archive, unarchive, delete, duplicate    |
| **Tasks**        | Create, get, update, move, complete, uncomplete, delete, list, search |
| **Subtasks**     | Add, list                                                           |
| **Comments**     | Add, list                                                           |
| **Dependencies** | Add, remove                                                         |
| **Bulk Ops**     | Bulk complete, bulk move                                            |
| **Sections**     | Create, list, rename, delete                                        |
| **Tags**         | Create, add to / remove from tasks, list, delete                    |
| **Members**      | List, add, remove (workspace and project scope)                     |
| **Audit Log**    | Queryable JSONL log of every write operation                         |
| **Sync**         | Automated GitHub commit-to-Asana ticket pipeline                    |

### Name Resolution

All identifiers accept human-friendly names instead of GIDs. The CLI performs **case-insensitive matching** with **partial match** support:

```bash
asana list-tasks --project "MaestroOS"     # exact match
asana list-tasks --project "maestroos"     # case-insensitive
asana list-tasks --project "maestro"       # partial (if unique)
```

When `--project` is provided, `--task` and `--section` also accept names.

---

## Quick Start

### 1. Get an Asana Personal Access Token

Create one at [app.asana.com/0/my-apps](https://app.asana.com/0/my-apps).

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your token
source .env
```

Or export directly:

```bash
export ASANA_PAT="your_personal_access_token"
export ASANA_WORKSPACE_GID="your_workspace_gid"  # optional, auto-detected
```

### 3. Run

```bash
python3 asana_cli.py <command> [options]
```

---

## Command Reference

### Projects

```bash
python3 asana_cli.py create-project --name "New Project" --layout board --notes "Description"
python3 asana_cli.py get-project --project "MyProject"
python3 asana_cli.py update-project --project "MyProject" --name "New Name" --color dark-blue
python3 asana_cli.py list-projects
python3 asana_cli.py list-projects --archived
python3 asana_cli.py archive-project --project "Old Project"
python3 asana_cli.py unarchive-project --project "Old Project"
python3 asana_cli.py delete-project --project "Old Project" --confirm
python3 asana_cli.py duplicate-project --project "Template" --name "New From Template"
```

### Sections

```bash
python3 asana_cli.py create-section --project "MyProject" --name "Backlog"
python3 asana_cli.py list-sections --project "MyProject"
python3 asana_cli.py rename-section --project "MyProject" --section "Backlog" --name "Archive"
python3 asana_cli.py delete-section --project "MyProject" --section "Archive"
```

### Tasks

```bash
python3 asana_cli.py create-task --project "MyProject" --section "To Do" \
    --name "Fix bug" --notes "Details" --due-on 2025-12-31
python3 asana_cli.py get-task --task <task_gid>
python3 asana_cli.py list-tasks --project "MyProject"
python3 asana_cli.py list-tasks --project "MyProject" --section "In Progress"
python3 asana_cli.py search-tasks --query "auth" --project "MyProject"
python3 asana_cli.py update-task --task "Fix bug" --project "MyProject" --name "Fix critical bug"
python3 asana_cli.py move-task --task "Fix bug" --project "MyProject" --section "Done"
python3 asana_cli.py complete-task --task "Fix bug" --project "MyProject"
python3 asana_cli.py uncomplete-task --task "Fix bug" --project "MyProject"
python3 asana_cli.py delete-task --task "Fix bug" --project "MyProject"
```

### Subtasks

```bash
python3 asana_cli.py add-subtask --task <task_gid> --name "Step 1" --notes "Details"
python3 asana_cli.py list-subtasks --task <task_gid>
```

### Comments

```bash
python3 asana_cli.py add-comment --task <task_gid> --text "Looks good, merging."
python3 asana_cli.py list-comments --task <task_gid>
```

### Dependencies

```bash
python3 asana_cli.py add-dependency --task <task_gid> --depends-on <other_task_gid>
python3 asana_cli.py remove-dependency --task <task_gid> --depends-on <other_task_gid>
```

### Bulk Operations

```bash
python3 asana_cli.py bulk-complete --tasks "gid1,gid2,gid3"
python3 asana_cli.py bulk-move --tasks "gid1,gid2,gid3" --section "Done" --project "MyProject"
```

### Tags

```bash
python3 asana_cli.py create-tag --name "urgent" --color dark-red
python3 asana_cli.py list-tags
python3 asana_cli.py add-tag --task <task_gid> --tag <tag_gid>
python3 asana_cli.py remove-tag --task <task_gid> --tag <tag_gid>
python3 asana_cli.py delete-tag --tag <tag_gid>
```

### Members

```bash
python3 asana_cli.py list-members
python3 asana_cli.py list-members --project "MyProject"
python3 asana_cli.py add-member --email user@example.com
python3 asana_cli.py remove-member --user "John Doe"
python3 asana_cli.py add-project-member --project "MyProject" --user user@example.com
python3 asana_cli.py remove-project-member --project "MyProject" --user user@example.com
```

### Audit Log

Every write operation is logged to `audit_log.jsonl` with timestamps.

```bash
python3 asana_cli.py audit-log
python3 asana_cli.py audit-log --last 10
python3 asana_cli.py audit-log --action create-task
python3 asana_cli.py audit-log --since 2025-01-01
python3 asana_cli.py audit-log --clear
```

All output is structured JSON, making it easy to pipe into `jq` or other tools.

---

## GitHub-to-Asana Sync

Automatically create Asana boards and tickets from your GitHub commit history.

### How It Works

1. **Weekly sync** (Fridays at 7pm SAST) — scans all your GitHub repos for new commits, groups them into logical tasks using [Conventional Commits](https://www.conventionalcommits.org/) prefixes, and creates Asana tickets on the matching board.

2. **New repo detection** (Mondays) — identifies repos without Asana boards and creates them with standard sections (`Done`, `In Progress`, `Backlog`).

3. **Manual trigger** — both workflows can be run on-demand from the GitHub Actions tab.

### Commit Grouping

| Prefix       | Category      | Example                            |
|--------------|---------------|------------------------------------|
| `feat:`      | Feature       | `feat: add user authentication`    |
| `fix:`       | Bug Fix       | `fix: resolve login redirect`      |
| `docs:`      | Documentation | `docs: update API reference`       |
| `refactor:`  | Refactor      | `refactor: extract auth module`    |
| `test:`      | Testing       | `test: add unit tests for parser`  |
| `chore:`     | Chore         | `chore: update dependencies`       |
| `perf:`      | Performance   | `perf: optimize database queries`  |
| `ci:`        | CI/CD         | `ci: add deployment workflow`      |

Commits with the same prefix and similar message are merged into a single task. Non-conventional commits are tracked individually. Duplicate task names are skipped, making the sync safe to re-run.

### Setup

1. Push this repo to GitHub.

2. Add the following **secrets** under **Settings > Secrets and variables > Actions**:

   | Secret               | Description                                  |
   |----------------------|----------------------------------------------|
   | `ASANA_PAT`          | Asana Personal Access Token                  |
   | `ASANA_WORKSPACE_GID`| Target Asana workspace GID                   |
   | `GH_TOKEN`           | GitHub PAT with `repo` scope                 |
   | `GITHUB_OWNER`       | Your GitHub username                         |

3. *(Optional)* Add a repository **variable** `SKIP_REPOS` with comma-separated repo names to exclude.

### Running Locally

```bash
export ASANA_PAT="your_asana_token"
export ASANA_WORKSPACE_GID="your_workspace_gid"
export GH_TOKEN="your_github_token"
export GITHUB_OWNER="your_github_username"

python3 sync_commits.py
```

---

## Project Structure

```
asana_api/
├── asana_cli.py                     # CLI tool (40 commands)
├── sync_commits.py                  # GitHub-to-Asana sync engine
├── .github/
│   └── workflows/
│       ├── weekly-sync.yml          # Friday commit sync
│       └── new-repo-sync.yml       # Monday new-repo detection
├── .env.example                     # Environment variable template
├── .gitignore
├── LICENSE
└── README.md
```

## Requirements

- Python 3.7+
- No external dependencies

## License

[MIT](LICENSE)
