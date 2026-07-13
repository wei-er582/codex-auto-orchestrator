---
status: active
owner: wei-er582
last_verified: 2026-07-13
verified_commit: "codex-auto-orchestrator@initial-implementation-2026-07-13"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Development

From the repository root:

```powershell
python -m compileall -q scripts tests
python -m unittest discover -s tests -v
python scripts\check_docs.py
```

The runtime intentionally uses only the Python standard library. Update all three output schemas and their manual validators together. Any process, retry, or worktree lifecycle change requires a regression test.

Use the official plugin cachebuster and reinstall flow after changing an installed build. Start a new Codex thread after reinstalling so the updated Skill metadata is loaded.
