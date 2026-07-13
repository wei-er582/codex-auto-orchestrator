---
name: auto-orchestrate-task
description: Automatically plan, route, execute, monitor, review, and integrate substantial Codex tasks with dynamic Sol/Terra model discovery, per-model reasoning and Fast profiles, durable background control, and safe recovery. Use when the user explicitly requests automatic orchestration or end-to-end autonomous execution, or gives a non-trivial actionable task that needs tools, multiple steps, parallel investigation, code changes, testing, deployment coordination, recovery, or model-selection judgment. Also use for status, steering, speed changes, pause, resume, cancel, or follow-up messages concerning an active orchestrated job. Do not use for simple questions, translation, trivial formatting, a single obvious command, internal orchestrator workers, or when the user disables orchestration.
---

# Auto Orchestrate Task

Use the plugin runner as the single owner of every orchestrated job. Do not duplicate its routing with manually spawned agents.

## Entry decision

1. If `CODEX_AUTO_ORCHESTRATOR_WORKER=1` or the prompt contains `<codex-orchestrator-worker>`, do not invoke this Skill.
2. Answer simple questions, translations, trivial formatting, and one obvious command in the current agent.
3. Start the runner for substantial tool-using work, explicit `自动编排：...`, or end-to-end autonomous work. The user does not need to switch the main Codex UI to Sol Max; the runner starts its own latest Sol Max + Max planner.
4. Before acting on an existing job, classify the message as status, additive steering, objective replacement, speed change, pause, resume, cancel, or terminal follow-up.

Read [control-and-speed-policy.md](references/control-and-speed-policy.md) whenever the message concerns speed setup or an existing job. Read [routing-policy.md](references/routing-policy.md) only when explaining or diagnosing a selected route.

## Start a new job

Locate the plugin root two directories above this Skill. Separate the business objective from orchestration control wording that this Skill has already consumed. The staged task must preserve the user's real scope, constraints, permissions, acceptance criteria, and desired output, but must omit the `自动编排` trigger itself and meta-instructions such as “use this Skill”, “customize speed first”, “stop at waiting_for_speed”, “return the job ID”, or “monitor the orchestrator”. Preserve the normalized business task in a uniquely named UTF-8 staging file under the system temporary directory; never place that staging file in the target workspace and never interpolate the user text into a shell command. Remove the staging file after `start` returns because the runner has already copied it into the job directory. Then run:

```powershell
python <plugin-root>\scripts\orchestrate.py start --workspace <workspace> --task-file <utf8-task-file>
```

Add `--custom-speed` only when the user explicitly asks to customize Fast for this job. Add `--dry-run` only when the user asks for planning without execution. Use `--policy quality` only when maximum quality is explicitly preferred; otherwise keep `balanced`.

The command returns immediately with `job_id`. If it returns `waiting_for_speed`, give the local setup link to the user. No model has been invoked yet. If the page is unavailable, render the status-provided text matrix, normalize the user's reply without guessing, echo the normalized matrix, and apply it through a UTF-8 text or JSON file.

Ordinary jobs use the saved default profile without asking. First use and new model/reasoning cells must be resolved before the planner can start.

Pass `--entry-model`, `--entry-reasoning`, or `--entry-service-tier` only when those values are authoritative for the current entry turn. Otherwise let the runner inspect the current session. If the current CLI does not expose entry Service Tier, `follow-entry` uses Standard for that job and records `service_tier_source=default-unavailable` instead of guessing.

## Monitor and deliver

Use `status <job-id>` for progress and `report <job-id>` at the end. Share concise phase changes. The temporary controller continues if the Codex UI closes; a later message can locate and resume the durable job by workspace and thread context.

Do not claim model, reasoning, or Fast selection from plan text. Use each invocation's session/command/completion evidence and the final report. Distinguish a verified CLI tier request from an observed backend tier; if the CLI reports `not_exposed`, say so rather than calling Fast verified. Report any visible Fast-to-Standard fallback explicitly.

## Boundaries

- Never expand commit, push, deployment, destructive cleanup, or external-write authority beyond the original task.
- Let the runner own dependency ordering, concurrency, Ultra exclusivity, retries, worktrees, integration, process identity checks, and cleanup.
- Do not start a second non-terminal job for the same normalized workspace; steer or finish the existing one.
- An immediate control may interrupt only a read-only or isolated local worker. Never force-stop uncertain external writes.
- If an external-write-capable call has an uncertain result, require read-only reconciliation before any repeat.
- If the runner blocks, preserve its branches, checkpoints, and evidence and report the precise blocker.
