from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


MODELS = {
    "models": [
        {
            "slug": "gpt-5.6-sol",
            "display_name": "GPT-5.6-Sol",
            "description": "Latest frontier agentic coding model.",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [
                {"effort": effort, "description": effort}
                for effort in ("low", "medium", "high", "xhigh", "max", "ultra")
            ],
            "multi_agent_version": "v2",
            "service_tiers": [{"id": "priority", "name": "Fast", "description": "fake fast"}],
        },
        {
            "slug": "gpt-5.6-terra",
            "display_name": "GPT-5.6-Terra",
            "description": "Balanced agentic coding model for everyday work.",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [
                {"effort": effort, "description": effort}
                for effort in ("low", "medium", "high", "xhigh", "max", "ultra")
            ],
            "multi_agent_version": "v2",
            "service_tiers": [{"id": "priority", "name": "Fast", "description": "fake fast"}],
        },
    ]
}


def argument(name: str, default: str = "") -> str:
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except ValueError:
        return default


def config_value(name: str, default: str = "") -> str:
    for index, value in enumerate(sys.argv[:-1]):
        if value != "-c":
            continue
        candidate = sys.argv[index + 1]
        if candidate.startswith(name + "="):
            raw = candidate.split("=", 1)[1]
            return raw.strip('"')
    return default


def main() -> int:
    if sys.argv[1:3] == ["debug", "models"]:
        print(json.dumps(MODELS))
        return 0
    if sys.argv[1:3] == ["login", "status"]:
        print("Logged in using ChatGPT", file=sys.stderr)
        return 0
    if len(sys.argv) < 2 or sys.argv[1] != "exec":
        print("unsupported fake command", file=sys.stderr)
        return 2

    prompt = sys.stdin.read()
    output_path = Path(argument("-o"))
    workspace = Path(argument("-C", str(Path.cwd())))
    model = argument("-m")
    reasoning = config_value("model_reasoning_effort", "medium")
    requested_tier = config_value("service_tier", "default")
    observed_tier = "default" if "FAKE_FAST_DEGRADE" in prompt else requested_tier
    observed_model = "gpt-5.6-terra" if "FAKE_WRONG_MODEL" in prompt else model
    resume = len(sys.argv) > 2 and sys.argv[2] == "resume"
    thread_id = _resume_session_id() if resume else f"fake-{os.getpid()}"
    _record_invocation(model, reasoning, requested_tier, resume, thread_id)
    # The settings event represents the local request; the completion event
    # below represents what the backend actually served.
    emit_settings(observed_model, reasoning, requested_tier, thread_id)
    if "FAKE_FAST_REJECT" in prompt and requested_tier == "priority":
        print("service_tier priority unavailable: fake quota exhausted", file=sys.stderr)
        return 3
    if "FAKE_SLEEP" in prompt and "ROLE: executor" in prompt and not resume:
        time.sleep(30)

    if "ROLE: planner" in prompt and "FAKE_MALFORMED_PLAN" in prompt:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("{malformed", encoding="utf-8")
        return 0
    if "ROLE: executor" in prompt and "FAKE_MALFORMED_RESULT" in prompt:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("{malformed", encoding="utf-8")
        return 0
    if (
        "ROLE: executor" in prompt
        and "FAKE_EXTERNAL_FAIL_ONCE" in prompt
        and "attempt-1" in output_path.name
    ):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("{malformed", encoding="utf-8")
        return 0

    if "ROLE: planner" in prompt:
        payload = make_plan(prompt)
    elif "ROLE: external action reconciler" in prompt:
        payload = make_external_verification(prompt)
    elif "ROLE: final reviewer and integrator" in prompt:
        payload = make_review(prompt, workspace)
    else:
        payload = make_result(prompt, workspace)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload), encoding="utf-8")
    completion = {
        "type": "turn.completed",
        "model": observed_model,
        "reasoning_effort": reasoning,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    if "FAKE_HIDE_TIER" not in prompt:
        completion["service_tier"] = observed_tier
    print(json.dumps(completion))
    return 0


def make_plan(prompt: str) -> dict:
    review = {
        "required": False,
        "model": "gpt-5.6-sol",
        "reasoning": "max",
        "acceptance": ["Verify the complete result"],
    }
    permissions = {"commit": True, "push": False, "deploy": False, "external_write": False}

    if "User steering received during execution:" in prompt and "MODE=ultra" in prompt:
        ultra = task("ultra-main", "Coordinate the coupled task", "write")
        ultra["model"] = "gpt-5.6-sol"
        ultra["reasoning"] = "ultra"
        return plan("native-ultra", "S4", "high", [{"id": "ultra-wave", "tasks": [ultra]}], review | {"required": True}, permissions)

    if "MODE=write-parallel" in prompt:
        tasks = [task("write-a", "WRITE_FILE A", "write"), task("write-b", "WRITE_FILE B", "write")]
        return plan("orchestrated", "S3", "medium", [{"id": "write-wave", "tasks": tasks}], review | {"required": True}, permissions)
    if "MODE=parallel" in prompt:
        tasks = [task("inspect-a", "Inspect A", "read"), task("inspect-b", "Inspect B", "read")]
        return plan("orchestrated", "S3", "medium", [{"id": "inspect-wave", "tasks": tasks}], review | {"required": True}, permissions)
    if "MODE=two-waves" in prompt:
        first = task("first", "FIRST_DELAY", "read")
        second = task("second", "Inspect after speed change", "read")
        second["depends_on"] = ["first"]
        return plan(
            "orchestrated",
            "S3",
            "medium",
            [{"id": "first-wave", "tasks": [first]}, {"id": "second-wave", "tasks": [second]}],
            review | {"required": True},
            permissions,
        )
    if "MODE=ultra" in prompt:
        ultra = task("ultra-main", "Coordinate the coupled task", "write")
        ultra["model"] = "gpt-5.6-sol"
        ultra["reasoning"] = "ultra"
        return plan("native-ultra", "S4", "high", [{"id": "ultra-wave", "tasks": [ultra]}], review | {"required": True}, permissions)
    if "MODE=missing-model" in prompt:
        direct = task("missing-model", "Inspect with unavailable model", "read")
        direct["model"] = "gpt-model-does-not-exist"
        return plan("direct", "S1", "low", [{"id": "missing-wave", "tasks": [direct]}], review, permissions)
    if "MODE=write-direct" in prompt:
        direct = task("direct-write", "WRITE_FILE DIRECT", "write")
        return plan("direct", "S1", "low", [{"id": "direct-write-wave", "tasks": [direct]}], review, permissions)
    if "MODE=external" in prompt:
        direct = task("external-main", "Perform the authorized external action", "write")
        external_permissions = permissions | {"external_write": True}
        return plan(
            "direct",
            "S2",
            "medium",
            [{"id": "external-wave", "tasks": [direct]}],
            review | {"required": True},
            external_permissions,
        )
    objective = "Inspect the task"
    access = "read"
    if "FAIL_ENVIRONMENT" in prompt:
        objective = "FAIL_ENVIRONMENT"
    elif "FAIL_IMPLEMENTATION" in prompt:
        objective = "FAIL_IMPLEMENTATION"
    elif "READ_WRITE_VIOLATION" in prompt:
        objective = "READ_WRITE_VIOLATION"
    direct = task("direct-main", objective, access)
    return plan("direct", "S1", "low", [{"id": "direct-wave", "tasks": [direct]}], review, permissions)


def task(task_id: str, objective: str, access: str) -> dict:
    return {
        "id": task_id,
        "title": task_id,
        "objective": objective,
        "model": "gpt-5.6-terra",
        "reasoning": "medium",
        "depends_on": [],
        "access": access,
        "allowed_paths": [],
        "acceptance": [f"Complete {task_id}"],
        "timeout_seconds": 120,
    }


def plan(mode: str, complexity: str, risk: str, waves: list[dict], review: dict, permissions: dict) -> dict:
    return {
        "version": 1,
        "summary": f"Fake {mode} plan",
        "complexity": complexity,
        "risk": risk,
        "execution_mode": mode,
        "rationale": "Deterministic fake plan for integration testing.",
        "waves": waves,
        "final_review": review,
        "permissions": permissions,
    }


def make_result(prompt: str, workspace: Path) -> dict:
    assigned = extract_json(prompt, "Assigned task:", "Dependency results:")
    task_id = assigned["id"]
    if assigned["objective"] == "FAIL_ENVIRONMENT":
        return result(task_id, "failed", "environment unavailable", "environment")
    if assigned["objective"] == "FIRST_DELAY":
        time.sleep(2)
    if "FAKE_PARTIAL" in prompt and task_id == "inspect-b":
        return result(task_id, "failed", "partial worker failed", "environment")
    if assigned["objective"] == "FAIL_IMPLEMENTATION" and "Previous failed attempt:\nnone" in prompt:
        return result(task_id, "failed", "first implementation failed", "implementation")
    if assigned["objective"] == "READ_WRITE_VIOLATION":
        target = workspace / "unauthorized.txt"
        target.write_text("must stay isolated\n", encoding="utf-8")
        payload = result(task_id, "success", "fake worker wrote inside snapshot", "none")
        payload["changed_files"] = [target.name]
        payload["blockers"] = []
        return payload

    changed: list[str] = []
    commit = ""
    repaired = "Review repair instructions:" in assigned["objective"]
    if assigned["objective"].startswith("WRITE_FILE"):
        target = workspace / (f"{task_id}-repair.txt" if repaired else f"{task_id}.txt")
        target.write_text(
            f"{'repaired' if repaired else 'generated'} by {task_id}\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(workspace), "add", target.name], check=True)
        subprocess.run(
            ["git", "-C", str(workspace), "commit", "-m", f"test: complete {task_id}"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        commit = subprocess.check_output(["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True).strip()
        changed.append(target.name)
    return {
        "task_id": task_id,
        "status": "success",
        "summary": f"{'repaired' if repaired else 'completed'} {task_id}",
        "changed_files": changed,
        "tests": ["fake acceptance passed"],
        "commit": commit,
        "artifacts": [],
        "blockers": [],
        "uncertainties": [],
        "failure_kind": "none",
    }


def result(task_id: str, status: str, summary: str, failure_kind: str) -> dict:
    return {
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "changed_files": [],
        "tests": [],
        "commit": "",
        "artifacts": [],
        "blockers": [summary],
        "uncertainties": [],
        "failure_kind": failure_kind,
    }


def make_review(prompt: str, workspace: Path) -> dict:
    results = extract_json(prompt, "Worker results:", "Worker branches:")
    branches = extract_json(prompt, "Worker branches:", "Rules:")
    for branch in branches:
        subprocess.run(
            ["git", "-C", str(workspace), "merge", "--no-edit", branch],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    commit = ""
    if (workspace / ".git").exists() or subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--git-dir"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0:
        commit = subprocess.check_output(["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True).strip()
    task_ids = sorted(results)
    if "MODE=review-repair" in prompt and not results.get("write-a", {}).get("summary", "").startswith("repaired"):
        assessments = [
            {
                "task_id": task_id,
                "status": "repair" if task_id == "write-a" else "pass",
                "findings": ["targeted fake defect"] if task_id == "write-a" else [],
                "repair_instructions": ["create the repair proof"] if task_id == "write-a" else [],
            }
            for task_id in task_ids
        ]
        return {
            "approved": False,
            "status": "failed",
            "summary": "one task needs targeted repair",
            "task_assessments": assessments,
            "findings": ["write-a needs repair"],
            "changed_files": [],
            "tests": ["fake review detected a defect"],
            "integration_commit": commit,
            "blockers": [],
            "merge_decision": "repair",
        }
    return {
        "approved": True,
        "status": "success",
        "summary": "fake review approved",
        "task_assessments": [
            {
                "task_id": task_id,
                "status": "pass",
                "findings": [],
                "repair_instructions": [],
            }
            for task_id in task_ids
        ],
        "findings": [],
        "changed_files": [],
        "tests": ["fake review passed"],
        "integration_commit": commit,
        "blockers": [],
        "merge_decision": "approve",
    }


def make_external_verification(prompt: str) -> dict:
    assigned = extract_json(prompt, "Assigned task:", "Authorized permissions:")
    if "FAKE_EXTERNAL_COMPLETED" in prompt:
        verdict = "completed"
        summary = "read-only evidence proves the external action already completed"
    elif "FAKE_EXTERNAL_FAIL_ONCE" in prompt:
        verdict = "not_applied"
        summary = "read-only evidence proves the first action was not applied"
    else:
        verdict = "uncertain"
        summary = "read-only evidence cannot determine the external outcome"
    return {
        "version": 1,
        "task_id": assigned["id"],
        "verdict": verdict,
        "summary": summary,
        "evidence": [f"fake external verification: {verdict}"],
    }


def extract_json(prompt: str, start: str, end: str):
    pattern = re.escape(start) + r"\s*(.*?)\s*" + re.escape(end)
    match = re.search(pattern, prompt, re.DOTALL)
    if not match:
        raise RuntimeError(f"unable to extract JSON between {start!r} and {end!r}")
    return json.loads(match.group(1))


def emit_settings(model: str, reasoning: str, service_tier: str, thread_id: str) -> None:
    print(json.dumps({"type": "thread.started", "thread_id": thread_id}), flush=True)
    print(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "thread_settings_applied",
                    "thread_settings": {
                        "model": model,
                        "reasoning_effort": reasoning,
                        "service_tier": service_tier,
                    },
                },
            }
        ),
        flush=True,
    )


def _resume_session_id() -> str:
    try:
        marker = sys.argv.index("resume")
    except ValueError:
        return f"fake-{os.getpid()}"
    options_with_values = {"-c", "-m", "--output-schema", "-o", "-i", "--disable", "--enable"}
    index = marker + 1
    while index < len(sys.argv):
        value = sys.argv[index]
        if value in options_with_values:
            index += 2
            continue
        if value.startswith("-"):
            index += 1
            continue
        return value
    return f"fake-{os.getpid()}"


def _record_invocation(
    model: str, reasoning: str, service_tier: str, resume: bool, thread_id: str
) -> None:
    path = os.environ.get("FAKE_CODEX_LOG")
    if not path:
        return
    record = {
        "model": model,
        "reasoning": reasoning,
        "service_tier": service_tier,
        "resume": resume,
        "thread_id": thread_id,
        "argv": sys.argv[1:],
    }
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
