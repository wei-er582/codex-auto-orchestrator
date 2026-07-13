---
status: active
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@v0.2.0"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Architecture

```text
Skill / CLI entry
    |
    +-- classify new task or control for an existing workspace job
    |
    +-- discover Sol/Terra catalog
    +-- resolve named speed profile or wait before any model call
    |
    v
Per-job temporary controller -- lease -- heartbeat -- checkpoint -- control queue
    |
    v
Sol Max read-only planner -- output schema -- runtime plan validator
    |
    +--> Direct worker in one isolated location
    +--> Dependency-ordered ordinary worker waves, max 3
    +--> One exclusive native Sol/Terra Ultra worker
    |
    +--> pause / steer / speed revision / safe resume at boundaries
    |
    v
Fresh Sol Max reviewer and integrator -- targeted repair at most once
    |
    +--> idempotent approved fast-forward or preserved integration branch
    |
    v
Observed model/reasoning + requested/observable tier evidence -- report -- safe cleanup
```

The Python engine is the authority for state and safety. A model-generated plan cannot exceed current model availability, supported reasoning, concurrency, Ultra, workspace, or original permission ceilings.

## Speed before planning

The catalog is discovered before any model invocation. A saved profile is compared with the current model-version/effort combinations. First use or a new combination changes state to `waiting_for_speed`; the Planner cannot start without a complete `speed-policy.json`. The snapshot records model bindings, matrix, catalog fingerprint, known combinations, source, and revision.

Every call performs a fresh lookup by actual model family and reasoning. Thus a Terra High worker, a Sol XHigh retry, Sol Ultra, and the Sol Max Reviewer can use different Service Tiers. A runtime update replaces only the job snapshot at a safe boundary and increments its revision.

## Controller and durable recovery

`start` creates state first, then launches one detached controller. The controller owns a non-blocking file lease and updates its PID birth identity and heartbeat. `control.json` is an atomic queue ordered as cancel, pause, steering/speed, then resume. The engine checkpoints completed waves and saves a session ID as soon as `thread.started` appears.

After a controller crash, a new controller authenticates leftover processes by both birth identity and job-specific command marker. A compatible task resumes with `codex exec resume`. If no session exists, only an intact isolated worktree with no uncertain external action may be continued in a fresh worker. Paused controllers exit after the configured idle interval but keep the same `job_id` and checkpoint.

## Process and evidence boundary

Task text and steering instructions travel through stdin or UTF-8 files. Subprocess commands are argument arrays. Children disable plugins and carry `CODEX_AUTO_ORCHESTRATOR_WORKER=1` to block recursive entry.

Each call explicitly passes model, `model_reasoning_effort`, and `service_tier`. The JSON stream yields the thread ID; the runner reads `turn_context` and local thread settings from the session rollout. Successful work requires observed model and reasoning to match. The runner separately proves the exact CLI tier override, records a backend match or Fast-to-Standard degradation when a completion event exposes it, and otherwise marks the backend tier `not_exposed`.

This separation is deliberate. Codex CLI 0.144.0 forwards `service_tier` in the Responses request but discards `response.service_tier` while reducing `response.completed` to response ID, usage, and end-turn state. Therefore local thread settings prove configuration, not the backend result. Release-only sanitized WebSocket inspection can audit that boundary, but the plugin does not bundle a TLS interception proxy or fabricate an observation. Plan labels are never proof.

## Workspace boundary

All writes in a clean Git repository use job-scoped worktrees, including Direct and Native Ultra. An integration worktree combines approved commits. The original branch advances only when clean and still at the captured baseline; repeated application is idempotent. A concurrent branch advance preserves the integration worktree instead of forcing a merge.

Dirty Git and non-Git workspaces serialize writes. Windows executable read tasks use a detached worktree or private copy with a deterministic content digest. Any read-snapshot mutation blocks cleanup and preserves evidence while leaving the original workspace unchanged.

## External actions

Original user authority is intersected with the plan. Push, deployment, and external writes register a stable action fingerprint. An external-capable invocation is not mechanically retried under a new session when the result is uncertain. A separate read-only Sol Max reconciliation classifies the target as completed, not applied, or still uncertain; only proven `not_applied` may be retried once.

See [ADR 0002](adr/0002-windows-read-snapshot-isolation.md), [ADR 0003](adr/0003-speed-policy-before-planning.md), and [ADR 0004](adr/0004-temporary-controller-and-durable-controls.md).
