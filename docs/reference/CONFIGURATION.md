---
status: active
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@initial-implementation-2026-07-13"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `--policy` | `balanced` | Select `economy`, `balanced`, or `quality` retry and concurrency limits |
| `--run-root` | `%USERPROFILE%\.codex\orchestrator\runs` | Store job state and evidence outside target repositories |
| `--codex-command` | short per-user npm Codex path when present, otherwise `codex.cmd` on Windows | Override the executable for tests or alternate installations |
| `CODEX_ORCHESTRATOR_CODEX_COMMAND` | unset | Environment-level executable override |
| `CODEX_AUTO_ORCHESTRATOR_WORKER` | managed internally | Prevent recursive orchestration in child processes |
| `CODEX_ORCHESTRATOR_READ_ISOLATION` | `snapshot` on Windows, `native` elsewhere | Select disposable snapshot or native CLI read-only sandbox for executable read tasks |

Balanced mode allows three ordinary workers, one retry for implementation or reasoning failures, and one exclusive Ultra execution. Environment, permission, timeout, and cancellation failures do not escalate reasoning.

The runtime inherits Codex authentication and the user's normal sandbox configuration. The planner always overrides its sandbox to read-only. No secret values are stored in project configuration or invocation evidence.

When `codex login status` confirms a stored ChatGPT login, child processes remove an inherited `CODEX_API_KEY` variable because it would otherwise override that login. API-key-authenticated CLI installations keep the variable unchanged.

On Windows the runtime prefers `%APPDATA%\npm\codex.cmd` when present. This avoids the WinGet Node prefix making native sandbox-helper paths longer than the Windows process-launch limit.

`snapshot` isolation runs executable read tasks in a detached Git worktree or private filesystem copy and verifies a full content digest before cleanup. `native` passes `-s read-only` directly to Codex and should be selected only where that sandbox has been validated.
