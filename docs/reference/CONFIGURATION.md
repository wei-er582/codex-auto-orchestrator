---
status: active
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@v0.2.0"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Configuration

## Runtime settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `--policy` | `balanced` | `economy`, `balanced`, or `quality` retry and concurrency policy |
| `--run-root` | `%USERPROFILE%\.codex\orchestrator\runs` | Durable state and evidence outside target repositories |
| `--config` | `%USERPROFILE%\.codex\orchestrator\config.json` | Named speed profiles and explicit prompt policy |
| `--speed-profile` | saved default | Select another profile for a new job |
| `--custom-speed` | off | Open per-job speed selection before planning |
| `--codex-command` | short npm Codex path on Windows, otherwise `codex` | Test or alternate CLI executable |
| `CODEX_ORCHESTRATOR_CODEX_COMMAND` | unset | Environment-level Codex override |
| `CODEX_ORCHESTRATOR_READ_ISOLATION` | `snapshot` on Windows, `native` elsewhere | Executable read-task isolation |
| `CODEX_ORCHESTRATOR_PAUSE_IDLE_SECONDS` | `1800` | Controller exit delay while paused |
| `CODEX_AUTO_ORCHESTRATOR_WORKER` | managed internally | Recursive-orchestration guard |

Balanced mode permits three ordinary workers, one implementation/reasoning retry per step, and one exclusive Ultra per job. Environment, permission, timeout, cancellation, and external uncertainty do not escalate reasoning.

## Speed configuration

The profile file has `version: 1`, `prompt_policy: explicit`, one saved default name, and any number of named profiles. Each profile records:

- a complete `sol` and `terra` matrix;
- `default` or `priority` for every currently supported reasoning effort;
- known model-version/effort combinations;
- the catalog fingerprint and timestamps.

Built-in templates cannot be overwritten or deleted:

| Template | Behavior |
| --- | --- |
| `balanced` | Fast for Sol Max, Sol Ultra, and Terra Ultra only |
| `all-fast` | Fast for every supported cell |
| `all-standard` | Standard for every cell |
| `follow-entry` | Use the entry thread's Service Tier for all supported cells when the surface exposes it; otherwise record `default-unavailable` and use Standard |

First setup must save a named user profile and make it default. A model-version or reasoning change is detected by combination identity, not just by the family name. New cells default to Standard in the form and require explicit confirmation before planning.

The one-shot page validates loopback Host, loopback Origin, random URL token, random CSRF value, maximum request size, and the dynamic matrix schema. It shows entry settings only as context and never displays the task body.

## Job snapshot and evidence

`speed-policy.json` is immutable until an explicit runtime speed control. Every revision stores the selected profile, complete matrix, model bindings, catalog fingerprint, source, and revision number. A global profile edit cannot drift into an active job.

Every subprocess explicitly receives:

```text
model
model_reasoning_effort
service_tier
```

`priority` is Fast and `default` is Standard. A Fast rejection is retried once as Standard. A successful read-only Fast call observed as Standard is also retried once explicitly as Standard; write calls already completed at Standard are not duplicated and are reported as degraded.

## Authentication and platform details

The runtime inherits the authenticated Codex session. If `codex login status` confirms ChatGPT login, child processes remove an inherited `CODEX_API_KEY` so it cannot silently override that login. API-key-only installations keep the variable.

On Windows the runtime prefers `%APPDATA%\npm\codex.cmd` when available. This avoids longer WinGet helper paths. Snapshot isolation uses a detached worktree for clean Git or a private copy for dirty/non-Git workspaces, then verifies a complete content digest before cleanup.
