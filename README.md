---
status: active
owner: Samsung
last_verified: 2026-07-13
verified_commit: "codex-auto-orchestrator@initial-implementation-2026-07-13"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Codex Auto Orchestrator

Codex Auto Orchestrator turns one task into a verified execution loop: Sol Max plans, the runtime selects Direct, Orchestrated, or Native Ultra execution, workers run with explicit model settings, and a fresh Sol Max reviewer integrates the result. Review failures can target only the rejected task for one bounded repair before a new review pass.

## Requirements

- Python 3.11 or newer
- Codex CLI with an authenticated session
- Sol and Terra models exposed by `codex debug models`
- Git for isolated parallel write tasks

## Use

After installing the personal plugin, start a new Codex thread and write:

```text
自动编排：<complete task>
```

The managed global rule also invokes the Skill automatically for substantial tasks that need tools, while simple questions, translation, lightweight formatting, and one obvious command remain direct.

The direct command is:

```powershell
python scripts\orchestrate.py run --workspace C:\path\to\project --task "<complete task>"
```

Add `--dry-run` to stop after planning or `--json` to print the final state.

Inspect or stop a job with:

```powershell
python scripts\orchestrate.py status <job-id>
python scripts\orchestrate.py cancel <job-id>
python scripts\orchestrate.py report <job-id>
```

Run records live outside target repositories under `%USERPROFILE%\.codex\orchestrator\runs`. Each invocation records the requested model, the model and reasoning observed in the authoritative Codex `turn_context`, and an `actual_selection_verified` verdict. Temporary Git worktrees are removed after successful integration and preserved when uncommitted state needs inspection.

On Windows, executable read-only workers use a disposable workspace snapshot because standalone CLI read-only sandbox accounts are not reliable in every desktop session. Clean Git repositories use a detached worktree; dirty Git and non-Git workspaces use a private copy. The engine hashes the snapshot before and after execution, removes it only when unchanged, and never exposes writes to the original workspace.

## Documentation

- [Implemented capabilities](docs/product/FUNCTIONS.md)
- [Architecture](docs/architecture/OVERVIEW.md)
- [Configuration](docs/reference/CONFIGURATION.md)
- [Development and testing](docs/runbooks/DEVELOPMENT.md)
- [Deployment and rollback](docs/runbooks/DEPLOYMENT.md)
