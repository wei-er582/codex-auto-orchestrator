---
status: active
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@v0.2.0"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Implemented capabilities

| Capability | Entry point | Verification |
| --- | --- | --- |
| Sol Max structured planning after speed resolution | `start` / `run` | Planner evidence and `plan.json` revisions |
| Direct, Orchestrated, and Native Ultra routing | Plan validator and engine | Fake-runner route and exclusivity tests |
| Dynamic Sol/Terra and reasoning discovery | `codex debug models` with cache fallback | Catalog, version ranking, and missing-family tests |
| Complete per-cell Fast policy | Profile store and `speed-policy.json` | Planner, Worker, escalation, Ultra, Reviewer, exposed follow-entry, and unavailable-tier fallback tests |
| Named profiles and first-use gate | `profiles` and local matrix page | CRUD, Unicode, catalog-change, concurrency, and no-planner-before-setup tests |
| Secured one-shot local UI | `127.0.0.1` random port | Host, Origin, token, CSRF, size, timeout, unsupported-cell, and cancel tests |
| Text configuration fallback | `speed --text-file` | Normalization, unknown-effort rejection, first-default save, and snapshot tests |
| Temporary background controller | `start` and `_controller` | Immediate return, lease, heartbeat, terminal exit, and paused idle tests |
| Atomic live controls | `steer`, `speed`, `pause`, `resume`, `cancel` | Priority, safe-boundary, immediate-interrupt, and plan/speed revision tests |
| Session and worktree recovery | `resume` | Crash, authenticated orphan cleanup, `codex exec resume`, and same-attempt tests |
| Linked terminal follow-up | `followup` | `parent_job_id` integration test |
| Clean-Git write isolation for every mode | Workspace manager | Direct, parallel, Ultra, merge, cleanup, and branch-advance tests |
| Dirty and non-Git protection | Workspace policy and fingerprints | Serialized writes, user-file preservation, and digest tests |
| Independent review and targeted repair | Fresh Sol Max review | Per-task assessment and one-task repair tests |
| External-write uncertainty guard | Action ledger and read-only Sol Max reconciler | Completed, proven-not-applied retry, and still-uncertain blocking tests |
| Selection and tier evidence | Session, command, and completion evidence | Model mismatch blocking; exact tier override; matched, degraded, and not-exposed tests |
| Version 2 durable state | `state.json` / `control.json` | v2 field, atomic lock, PID identity, and v0.1 compatibility tests |
| Human-readable audit report | `report` | Requested/observed values, profile revision, Fast counts, and degradation list |
| Personal Codex Skill entrypoint | `auto-orchestrate-task` | Official Skill and plugin validators |

## Intentional boundaries

- The plugin has no permanent daemon, hosted UI, MCP server, or Hook.
- Native Ultra is one exclusive invocation per job and cannot recursively start another outer orchestrator.
- A profile update never mutates a running job snapshot unless the user explicitly sends a runtime speed control.
- An uncertain external write blocks automatic repetition until read-only reconciliation establishes the external state.
- Changed or unmerged isolation work is preserved rather than force-removed.
- Backend Service Tier is marked verified only when a runtime completion exposes it. Codex CLI 0.144.0 normally does not, so reports preserve the distinction between a proven request and an observed backend result.
