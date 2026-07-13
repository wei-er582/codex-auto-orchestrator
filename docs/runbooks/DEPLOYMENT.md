---
status: active
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@v0.2.0"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Deployment

1. Run compile, full unit/integration suite, documentation audit, Skill validation, plugin validation, and `git diff --check`.
2. Run bounded real Sol Fast/Standard, Terra Fast/Standard, and session-resume acceptance. Verify observed model/reasoning and the exact CLI Service Tier override. Verify the backend tier only where the runtime exposes it; otherwise require an explicit `not_exposed` result and optionally preserve sanitized release-only transport evidence.
3. Confirm there is no active, waiting, paused, or interrupted job that could be affected by reinstall.
4. Set the source manifest to `0.2.0`, then run the official plugin cachebuster. Do not edit the personal marketplace entry manually.
5. Reinstall `codex-auto-orchestrator@personal` and start a new Codex task to validate explicit entry, first/default speed setup, and per-job override.
6. Commit the plugin repository, create annotated tag `v0.2.0`, and push the public GitHub repository.
7. Fast-forward `/opt/codex-auto-orchestrator/current` to the release commit and write the exact commit to `REVISION`. The server is a mirror, not a service.
8. Commit and push the private global-rules repository, synchronize the global `AGENTS.md` entry, and mirror `/opt/codex-global-rules/current`.
9. Prove plugin local HEAD, GitHub HEAD, and server `REVISION` match. Prove the same independently for global rules.

Keep only one `current` checkout and the necessary revision record on the server. Do not retain release trees or run a controller there.
