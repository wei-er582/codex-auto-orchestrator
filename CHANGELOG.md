---
status: active
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@v0.2.0"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Changelog

## 0.2.0 - 2026-07-14

- Added dynamic Sol/Terra Fast matrices, immutable job snapshots, named profiles, four built-in templates, first-use and model-catalog-change gates, and a secured one-shot local configuration page.
- Added text fallback, profile CLI management, explicit Standard overrides, runtime speed revisions, and Fast-to-Standard fallback evidence.
- Replaced foreground-only execution with per-job temporary background controllers, leases, heartbeats, atomic controls, pause/resume, steering, linked follow-ups, and crash-safe Codex session continuation.
- Upgraded state to version 2 with origin thread, parent job, desired status, checkpoints, plan revisions, speed revisions, workspace resources, control sequence, and external-action records; terminal v0.1 jobs remain readable.
- Extended clean-Git worktree isolation to Direct and Native Ultra writes, made integration idempotent, preserved integration work when the original branch advances, and authenticated process cleanup by PID birth identity plus job marker.
- Added hard runtime verification for model/reasoning, exact CLI Service Tier override proof, backend-tier observation when the CLI exposes it, and explicit `not_exposed` compatibility evidence when it does not; uncertain external writes are not mechanically retried.
- Expanded public commands, schemas, documentation, ADRs, fake-runner coverage, and real-release acceptance requirements.

## 0.1.2 - 2026-07-13

- Removed empty per-job temporary directories after clean snapshot and worktree cleanup.
- Added regression assertions for successful and cancelled orchestration cleanup.

## 0.1.1 - 2026-07-13

- Aligned structured-output schemas with runtime identifier validation.
- Accepted safe lowercase underscore identifiers such as `wave_1` while preserving filename and branch safety.
- Added planner guidance and regression coverage for wave, task, dependency, result, and review identifiers.

## 0.1.0 - 2026-07-13

- Added automatic Sol Max planning and model-aware Direct, Orchestrated, and Native Ultra execution.
- Added cross-process JSON state, session-backed model invocation evidence, cancellation, bounded retries, per-task review repair, Git worktree isolation, and safe integration.
- Added Windows read-only snapshot isolation with full content fingerprints and short-path Codex CLI selection.
- Added a personal Codex plugin and implicit Skill entrypoint.
