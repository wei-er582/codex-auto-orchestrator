---
status: accepted
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@v0.2.0"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# ADR 0002: isolate executable Windows read tasks with snapshots

## Context

Real Codex CLI smoke tests showed two independent failures for standalone `-s read-only` execution on Windows: a WinGet npm helper path exceeded the process-launch path limit, and the shorter installation still could not create a sandbox-user logon session. Tool-free structured calls worked, but read workers that launched PowerShell did not.

Running those workers with the inherited unrestricted sandbox against the original workspace would make a plan's `access=read` declaration unenforceable and could damage unknown dirty state.

## Decision

Keep the Sol Max planner in native read-only mode because it receives complete preflight context and is prohibited from launching tools. On Windows, run executable read workers and all-read reviewers in one disposable snapshot:

- clean Git workspace: detached worktree at the captured baseline commit;
- dirty Git or non-Git workspace: private filesystem copy;
- all snapshot content outside Git metadata: deterministic SHA-256 fingerprint before and after execution;
- unchanged snapshot: remove immediately;
- changed snapshot: block the job, preserve the snapshot as evidence, and leave the original workspace untouched.

Child prompts prohibit access to original absolute paths and treat planned allowed paths as mappings inside the snapshot. Linux and macOS continue to use the native read-only sandbox by default. `CODEX_ORCHESTRATOR_READ_ISOLATION` can explicitly select either strategy.

## Consequences

Read tasks no longer depend on the unstable standalone Windows sandbox account and cannot write into the original workspace through their current working directory. Dirty and non-Git workspaces pay a copy and hashing cost. Snapshot isolation protects the scoped workspace but does not pretend to revoke every OS-level capability from an unrestricted child, so prompt boundaries, original authority ceilings, and post-run verification remain mandatory.
