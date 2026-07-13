---
status: active
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@v0.2.0"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Development

From the repository root:

```powershell
python -m compileall -q scripts tests
python -m unittest discover -s tests -v
python scripts\check_docs.py
git diff --check
```

The runtime uses only the Python standard library and supports Python 3.11+. Keep subprocess commands as argument arrays and pass task or instruction bodies through stdin or UTF-8 files.

Update runtime validators and their JSON Schemas together. Any model discovery, speed profile, controller, control queue, resume, process cleanup, worktree, external-action, or evidence change requires a deterministic fake-runner regression test.

Run the official Skill validator, plugin validator, and cachebuster after implementation validation. Reinstall from the personal marketplace; never edit generated marketplace entries manually. Start a new Codex task after reinstall so the new Skill metadata is loaded.

Before a real smoke run, use an isolated temporary workspace, a bounded prompt, and a dedicated profile. Confirm there is no non-terminal job in the same normalized workspace. Do not use fake evidence as a substitute for at least one release-time real session.
