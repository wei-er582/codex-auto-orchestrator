---
status: active
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@v0.2.0"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Rollback

1. Stop or explicitly cancel all active v0.2 jobs. Do not reinstall while a controller still owns a job lease.
2. Preserve `~/.codex/orchestrator/runs` and every blocked or changed worktree. Version 0.2 evidence must remain available after rollback.
3. Disable the managed automatic trigger, synchronize and audit global rules, then restore the v0.1.2-compatible rule revision.
4. Reinstall the v0.1.2 plugin through the personal marketplace workflow and start a new Codex task before testing.
5. Restore the public repository/server mirror only if the rollback is intended to change the published default, then verify all three revisions.

Do not feed non-terminal version 2 state to v0.1.2. Finish or cancel it first. Terminal evidence is data, not disposable cache.
