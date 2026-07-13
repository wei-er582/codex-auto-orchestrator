---
status: active
owner: wei-er582
last_verified: 2026-07-14
verified_commit: "codex-auto-orchestrator@v0.2.0"
applies_to: [codex-auto-orchestrator]
supersedes: []
---

# Testing

## Deterministic suite

The fake Codex runner covers:

- dynamic Sol/Terra discovery, version ranking, reasoning availability, schemas, topology, and concurrency;
- first setup before Planner, named profile CRUD, Unicode names, catalog changes, atomic locks, unsupported cells, and immutable job snapshots;
- Host, Origin, token, CSRF, request-size, timeout, page cancellation, and text fallback;
- Planner, Terra Worker, escalation, Ultra, and Reviewer speed lookup; Fast, Standard, observed degradation, and rejection fallback;
- background return, one-workspace exclusion, controller heartbeat/exit, safe immediate control, pause idle, replacement replanning, linked follow-up, and crash/session resume;
- timeout, cancellation, PID birth identity, v0.1 state compatibility, and authenticated orphan handling;
- Direct, parallel, and Ultra worktree isolation; dirty serialization; read-snapshot containment; idempotent integration; concurrent branch advance; selective repair; and cleanup;
- external-write uncertainty, hard model/reasoning verification, exact tier-override proof, and matched/degraded/not-exposed tier classification.

Run:

```powershell
python -m compileall -q scripts tests
python -m unittest discover -s tests -v
python scripts\check_docs.py
```

## Real Codex acceptance

Release acceptance must include bounded real calls for:

1. Sol Max + Max + Fast Planner.
2. Sol Max + Max + Standard Planner.
3. Terra ordinary reasoning + Fast Worker.
4. Terra ordinary reasoning + Standard Worker.
5. One interrupted session resumed by session ID.
6. A new Codex task that exercises first setup or default-profile use and an explicit per-job override.

For every call, inspect `invocation.json` and the referenced session rollout. `requested_model` and `requested_reasoning` must match observed thread settings and `model_reasoning_verified` must be true. `service_tier_request_verified` must also be true. When the CLI exposes a backend tier, require `matched` or an explicit `degraded` result and fallback. When it does not, require `service_tier_observation_status=not_exposed`; never reinterpret local thread settings as backend evidence.

Codex CLI 0.144.0 does not include `response.service_tier` in normal exec JSON/session evidence. For release diagnosis only, the optional `tests/tools/extract_mitm_tiers.py` addon can filter an operator-created mitmproxy capture down to host, path, model, and tier fields. It deliberately omits headers, prompts, auth material, and response text. The v0.2.0 sanitized result is preserved under `docs/releases/evidence/`.

Before release, confirm there are no active or resumable acceptance jobs, no extra worktrees or `codex-orch/*` branches, and no uncommitted repository changes beyond the intended release patch.
