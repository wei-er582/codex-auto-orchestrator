---
status: active
owner: Samsung
last_verified: 2026-07-13
verified_commit: "codex-auto-orchestrator@initial-implementation-2026-07-13"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Changelog

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
