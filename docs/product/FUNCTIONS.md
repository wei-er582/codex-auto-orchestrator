---
status: active
owner: Samsung
last_verified: 2026-07-13
verified_commit: "codex-auto-orchestrator@initial-implementation-2026-07-13"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Implemented capabilities

## Implemented

| Capability | Entry point | Verification |
| --- | --- | --- |
| Sol Max structured planning | `orchestrate.py run` | Planner invocation evidence and `plan.json` |
| Direct, Orchestrated, Native Ultra routing | Plan validator and engine | Unit and fake-runner integration tests |
| Dynamic model discovery | `codex debug models` with cache fallback | Model catalog tests |
| Parallel read and isolated write workers | Wave executor and Git worktrees | Parallel integration tests |
| Windows read-only snapshot isolation | Detached read worktree or private copy plus content digest | Write-violation containment and real read smoke |
| Failure classification and bounded retry | Worker execution loop | Environment, malformed-output, model-unavailable, and partial-completion tests |
| Independent review, targeted repair, and integration | Fresh Sol Max review sessions | Per-task assessment, selective repair, and Git merge tests |
| Status, cancellation, and reports | CLI subcommands and cross-process state lock | State, timeout, process-tree, branch, and worktree cleanup tests |
| Actual model verification | Exec events plus Codex session `turn_context` | Real Sol Max and Terra smoke evidence |
| Personal Codex Skill entrypoint | `auto-orchestrate-task` | Skill and plugin validators |

## Intentional limitations

- Version 0.1.0 does not expose MCP controls or a graphical task tree.
- Native Ultra is exclusive within its wave; recursive external orchestration is blocked.
- Dirty or non-Git workspaces serialize write tasks instead of attempting unsafe parallel merging.
- A failed run removes clean temporary worktrees but preserves non-clean worktrees for inspection rather than discarding unique changes.
- Snapshot isolation adds copy and hashing cost for dirty or non-Git Windows workspaces.
