---
status: active
owner: wei-er582
last_verified: 2026-07-13
verified_commit: "codex-auto-orchestrator@initial-implementation-2026-07-13"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Deployment

1. Run syntax, unit, integration, documentation, Skill, and plugin validation.
2. Update the plugin cachebuster and install `codex-auto-orchestrator@personal`.
3. Start a new Codex thread and verify the explicit `自动编排：` entrypoint.
4. Enable the managed global trigger only after the real smoke test passes.
5. Commit and push the GitHub repository.
6. Clone or fast-forward `/opt/codex-auto-orchestrator/current` and write its `REVISION` file.
7. Verify local HEAD, `origin/main`, and server `REVISION` are identical.

The server copy is a version mirror, not a running service. Do not retain release directories there.
