---
status: active
owner: Samsung
last_verified: 2026-07-13
verified_commit: "codex-auto-orchestrator@initial-implementation-2026-07-13"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# ADR 0001: Plugin with Skill and deterministic Python orchestration

## Context

A prompt or Skill can recommend a model but cannot by itself prove that separate Codex runs used the selected model and reasoning level. Native Ultra provides automatic delegation but does not replace transparent cross-model routing for independent tasks.

## Decision

Package the workflow as a personal plugin. Use a concise Skill for discovery and a dependency-free Python process controller for model discovery, planning, validation, dispatch, monitoring, retries, Git isolation, review, and cleanup.

Support three mutually exclusive execution shapes: Direct, Orchestrated, and Native Ultra. Do not add MCP or Hooks in version 0.1.0.

## Consequences

- Invocation arguments and JSONL events provide auditable routing evidence.
- Safety rules remain deterministic even when a planner produces a poor plan.
- The runtime depends on a local Codex CLI and authenticated session.
- UI-level live controls can be added later without replacing the execution engine.
