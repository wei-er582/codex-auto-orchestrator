---
name: auto-orchestrate-task
description: Automatically plan, route, execute, monitor, review, and integrate substantial Codex tasks using the locally available Sol and Terra models and their supported reasoning levels. Use when a user explicitly requests automatic orchestration or end-to-end autonomous execution, or gives a non-trivial actionable task that needs tools, multiple steps, parallel investigation, code changes, testing, deployment coordination, or model-selection judgment. Do not use for simple questions, translation, trivial formatting, a single obvious command, tasks explicitly marked as an internal orchestrator worker, or when the user explicitly disables orchestration.
---

# Auto Orchestrate Task

Use the deterministic plugin runner for the whole task. Do not reproduce its routing logic in the conversation.

## Start a task

1. Preserve the user's complete request and the active workspace path.
2. Locate the plugin root two levels above this skill directory.
3. Run:

```powershell
python <plugin-root>\scripts\orchestrate.py run --workspace <workspace> --task <complete-user-request>
```

4. Poll the yielded process until it finishes. Share concise progress from the runner, but do not manually spawn competing agents for the same task.
5. Read the final report path printed by the runner and summarize the actual result, tests, model routing, and any blocker.

Use `--dry-run` only when the user asks for a plan without execution. Use `--policy quality` only when the user explicitly prioritizes maximum quality over quota; otherwise keep `balanced`.

## Inspect or stop a task

```powershell
python <plugin-root>\scripts\orchestrate.py status <job-id>
python <plugin-root>\scripts\orchestrate.py cancel <job-id>
python <plugin-root>\scripts\orchestrate.py report <job-id>
```

## Boundaries

- Never invoke this skill when `CODEX_AUTO_ORCHESTRATOR_WORKER=1` or the prompt contains `<codex-orchestrator-worker>`.
- Let the runner enforce model availability, dependency order, concurrency, Ultra exclusivity, dirty-worktree protection, retries, and cleanup.
- Do not claim a model switch from plan text alone; use the invocation evidence and JSONL event log produced by the runner.
- Do not expand task authority. Push, deployment, destructive cleanup, or external writes remain allowed only when the original request authorizes them.
- If the runner reports `blocked`, preserve its worktrees and evidence and report the precise blocker instead of improvising destructive recovery.

Read [routing-policy.md](references/routing-policy.md) only when explaining why a route was selected or diagnosing an orchestration result.
