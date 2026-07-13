# Active-job control and speed policy

## Find the job

Prefer the `job_id` already returned in the conversation. Otherwise inspect the latest state for the current normalized workspace and `CODEX_THREAD_ID`. Never guess between multiple unrelated workspaces.

```powershell
python <plugin-root>\scripts\orchestrate.py status [<job-id>]
```

## Classify the user's message

| User intent | Command | Effect |
| --- | --- | --- |
| Ask progress or what is running | `status` | Read-only status; do not alter execution |
| Add a constraint or clarification | `steer --mode add` | Applies at the next safe boundary |
| Replace the objective or direction | `steer --mode replace` | Sol Max replans and increments `plan_revision` |
| Change Fast for later calls | `speed` | Creates a new immutable speed-policy revision |
| Pause | `pause` | Pauses at a safe boundary unless `--immediate` is explicitly requested and safe |
| Continue | `resume` | Reuses the same `job_id`, checkpoint, worktree, and session when compatible |
| Cancel | `cancel` | Authenticates and terminates only matching job processes, then cleans safe resources |
| Message after a terminal job | `followup` | Creates a new linked job with `parent_job_id` |

Pass steering and follow-up text through UTF-8 files:

```powershell
python <plugin-root>\scripts\orchestrate.py steer <job-id> --instruction-file <file> --mode add
python <plugin-root>\scripts\orchestrate.py followup <job-id> --task-file <file>
python <plugin-root>\scripts\orchestrate.py pause <job-id>
python <plugin-root>\scripts\orchestrate.py resume <job-id>
python <plugin-root>\scripts\orchestrate.py cancel <job-id>
```

Use `--immediate` only when the user explicitly requests immediate behavior. An active invocation keeps its launch-time speed unless a safe immediate interruption is allowed; otherwise the new revision begins at the next boundary.

Speed-selection language belongs to the control plane. Consume phrases such as “本轮自定义速度”, “先选择 Fast”, or “停在 waiting_for_speed” when choosing CLI flags and interacting with the user; do not copy those phrases into the Planner's business-task text or Worker acceptance criteria. Preserve all non-orchestration requirements unchanged.

## Speed profiles

The global configuration is `~/.codex/orchestrator/config.json`. Built-ins are `balanced`, `all-fast`, `all-standard`, and `follow-entry`; first setup still requires a saved, named user profile as the default.

```powershell
python <plugin-root>\scripts\orchestrate.py profiles list
python <plugin-root>\scripts\orchestrate.py profiles show <name>
python <plugin-root>\scripts\orchestrate.py profiles configure [<name>]
python <plugin-root>\scripts\orchestrate.py profiles copy <source> <target>
python <plugin-root>\scripts\orchestrate.py profiles rename <source> <target>
python <plugin-root>\scripts\orchestrate.py profiles set-default <name>
python <plugin-root>\scripts\orchestrate.py profiles delete <name>
```

For an active job, use a named profile or a complete matrix file:

```powershell
python <plugin-root>\scripts\orchestrate.py speed <job-id> --profile <name>
python <plugin-root>\scripts\orchestrate.py speed <job-id> --matrix-file <json> --scope job
```

For the text fallback, accept only explicit known efforts and echo the normalized matrix before applying it. The canonical file format is:

```text
Sol Fast = xhigh, max, ultra
Terra Fast = high, xhigh, max, ultra
```

To complete first setup through text, save and set the named default in the same call:

```powershell
python <plugin-root>\scripts\orchestrate.py speed <job-id> --text-file <file> --save-profile <name> --set-default
```

To update a profile after new catalog cells appear, use `--save-profile <current-name> --overwrite-profile`. Add `--set-default` only when the user asks to change the default.

`priority` means Fast and `default` means Standard. A saved job snapshot does not drift when the global profile later changes. `follow-entry` reads the current entry thread settings for each new job. If the current Codex surface exposes model/reasoning but not Service Tier, it records `service_tier_source=default-unavailable` and safely uses Standard rather than guessing that Fast was enabled.

## Recovery rules

1. If a saved Codex session, workspace, and objective remain compatible, resume it.
2. If the session cannot resume but an isolated worktree is intact and no external result is uncertain, continue from its diff in a new worker.
3. If the user replaces direction, replan with Sol Max.
4. Authenticate PID birth identity and job markers before terminating a leftover process.
5. Never repeat an uncertain push, deploy, or external write; verify the external state read-only first.
