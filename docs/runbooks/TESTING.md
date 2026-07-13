---
status: active
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@initial-implementation-2026-07-13"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Testing

The fake Codex runner validates deterministic behavior without consuming model quota. It covers model discovery, schema and topology rejection, concurrency limits, Windows argument boundaries, model unavailability, malformed output, partial completion, routing, escalation, timeout, cross-process cancellation, parallel Git writes, dirty-worktree serialization, Windows read-snapshot write containment, targeted review repair, integration, and cleanup.

Release acceptance uses one real read-only dry-run through Sol Max and one bounded Direct execution in a temporary repository. For every real invocation, confirm that `requested_model` and `requested_reasoning` match the `observed_models` and `observed_reasoning` extracted from the session and that `actual_selection_verified` is true. Do not enable the global implicit rule until these smoke tests pass.

The 2026-07-13 acceptance jobs were `20260713T141109Z-1e9fae1b` for read-only planning, `20260713T141726Z-73961cd6` for Direct writing, `real-ultra-smoke-20260713T143319Z` for non-interactive Terra Ultra, and `20260713T144937Z-c662cc3d` for the final read-snapshot chain. The final job verified actual `gpt-5.6-sol + max` planning and `gpt-5.6-terra + low` execution, matched 53-file before/after hashes, and removed its snapshot.
