---
status: active
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@v0.2.0"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# ADR 0001: Plugin with Skill and deterministic Python orchestration

## Context

A prompt or Skill can recommend a model but cannot by itself prove that separate Codex runs used the selected model and reasoning level. Native Ultra provides automatic delegation but does not replace transparent cross-model routing for independent tasks.

## Decision

Package the workflow as a personal plugin. Use a concise Skill for discovery and a dependency-free Python process controller for model discovery, planning, validation, dispatch, monitoring, retries, Git isolation, review, and cleanup.

Support three mutually exclusive execution shapes: Direct, Orchestrated, and Native Ultra. Do not add MCP or Hooks. Version 0.2 adds temporary per-job controllers and a one-shot loopback setup page without introducing a resident service.

## Consequences

- Invocation arguments and JSONL events provide auditable routing evidence.
- Safety rules remain deterministic even when a planner produces a poor plan.
- The runtime depends on a local Codex CLI and authenticated session.
- Live controls and speed profiles reuse the same deterministic execution engine and durable state.
