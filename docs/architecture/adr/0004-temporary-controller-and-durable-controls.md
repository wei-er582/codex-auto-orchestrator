---
status: accepted
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@v0.2.0"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# ADR 0004: temporary per-job controllers with durable controls

## Context

A foreground command cannot survive a closed Codex UI, accept later steering safely, or distinguish an interrupted worker from a failed attempt. A permanent service would add installation, security, and lifecycle cost that is unnecessary for personal local orchestration.

## Decision

Launch one hidden controller per job. Protect it with a non-blocking lease, update a two-second heartbeat, save checkpoints and session IDs, and exit at terminal state or after a paused idle timeout. Store controls in an atomic queue with explicit priority and safe/immediate boundaries.

Resume a compatible Codex session first. Continue from an isolated worktree only when session recovery is unavailable and no external result is uncertain. Authenticate leftover processes with both PID birth identity and a job command marker before terminating them.

## Consequences

- Closing the Codex UI does not cancel active work.
- Pause, resume, steering, speed revision, and terminal follow-up have deterministic semantics.
- Interrupted safe workers do not spend retry budget.
- No resident service or listening control port exists outside the short-lived setup page.
- State version 2 and v0.1 compatibility logic are required.
