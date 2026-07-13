---
status: active
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@initial-implementation-2026-07-13"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Architecture

```text
Skill or CLI
    |
    v
Sol Max read-only planner -- plan schema -- policy validator
    |
    +--> Direct worker
    +--> Bounded parallel workers and Git worktrees
    +--> One native Sol/Terra Ultra worker
    |
    v
Fresh Sol Max reviewer/integrator -- per-task review schema
    |                                  |
    +-- one targeted task repair <-----+
    |
    +-- approved safe fast-forward
    |
    v
State, invocation evidence, report, and cleanup
```

The Python engine is the authority for execution state and safety policy. Model-generated plans cannot exceed current model availability, concurrency, Ultra, workspace, or permission ceilings.

Each Codex subprocess receives task text through stdin and uses an argument array, so shell interpolation cannot reinterpret user input. Child processes disable plugins and carry `CODEX_AUTO_ORCHESTRATOR_WORKER=1` to prevent recursive orchestration.

For a clean Git repository, each parallel write task receives a branch and worktree at the baseline revision. A fresh integration branch merges approved worker commits, runs final review, and advances the original branch only when its HEAD and cleanliness still match the baseline. Unknown modifications stop integration and remain preserved.

On Windows, read-only executors and reviewers use one shared disposable snapshot rather than the standalone CLI read-only sandbox. A clean Git repository uses a detached worktree; dirty Git and non-Git workspaces are copied. The engine fingerprints every non-Git-metadata file before execution and blocks while preserving evidence if any content changes. The original workspace is never the write target for a read task.

Runtime evidence is stored outside target projects. `state.json` is atomically replaced under a cross-process file lock, so a separate `cancel` process cannot lose concurrent PID or task updates. Active child PIDs remain visible to cancellation, which terminates their process trees before clean worktrees and branches are removed.

The JSON event stream identifies each Codex thread. The runner resolves that thread to its session rollout and reads only authoritative `turn_context` records to verify the actual model and reasoning effort. Plan labels alone are never treated as routing proof.

See [ADR 0002](adr/0002-windows-read-snapshot-isolation.md) for the Windows isolation decision.
