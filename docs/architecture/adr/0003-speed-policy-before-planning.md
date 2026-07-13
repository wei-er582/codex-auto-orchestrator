---
status: accepted
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@v0.2.0"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# ADR 0003: resolve a complete speed policy before planning

## Context

The entry agent has already been launched with the user's current UI speed, but Planner, Worker, Ultra, repair, and Reviewer are new Codex sessions. Choosing their speed after planning would make the Planner itself inconsistent and would allow a later global setting to change an active job.

## Decision

Discover the current Sol/Terra catalog first and resolve a complete family/effort matrix before any model call. Save it as an immutable job snapshot. First use and newly discovered combinations block in `waiting_for_speed`. Ordinary jobs use the named default without prompting; explicit per-job customization uses a secured one-shot loopback page or strict text fallback.

Use `priority` for Fast and `default` for Standard. Look up each call independently after any model or reasoning escalation. A runtime speed control creates a new job-only revision at a safe boundary.

## Consequences

- Planner speed is deterministic and auditable.
- Global edits cannot drift into running work.
- New model cells require one explicit choice before quota is used.
- Fast failure can degrade independently of reasoning escalation.
- The profile store and UI must track dynamic catalogs rather than hard-coded version names.
