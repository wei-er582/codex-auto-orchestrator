---
status: active
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@initial-implementation-2026-07-13"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Rollback

Disable the managed global trigger first, run the global-rules sync and audit, and reinstall the last verified plugin commit from the personal marketplace. Start a new Codex thread before retesting.

Do not delete blocked job directories or non-clean worktrees during rollback. They may contain unique user changes and must be reviewed separately.
