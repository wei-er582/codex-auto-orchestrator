from __future__ import annotations

import json
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
        },
    ]
}


def argument(name: str, default: str = "") -> str:
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except ValueError:
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
    reasoning_value = argument("-c")
    reasoning = reasoning_value.split('"')[1] if '"' in reasoning_value else reasoning_value.split("=")[-1]
    if "FAKE_SLEEP" in prompt and "ROLE: executor" in prompt:
        time.sleep(30)

    if "ROLE: planner" in prompt and "FAKE_MALFORMED_PLAN" in prompt:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("{malformed", encoding="utf-8")
        print(json.dumps({"type": "thread.started", "model": model, "reasoning_effort": reasoning}))
        return 0
    if "ROLE: executor" in prompt and "FAKE_MALFORMED_RESULT" in prompt:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("{malformed", encoding="utf-8")
        print(json.dumps({"type": "thread.started", "model": model, "reasoning_effort": reasoning}))
        return 0

    if "ROLE: planner" in prompt:
        payload = make_plan(prompt)
    elif "ROLE: final reviewer and integrator" in prompt:
        payload = make_review(prompt, workspace)
    else:
        payload = make_result(prompt, workspace)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload), encoding="utf-8")
    print(json.dumps({"type": "thread.started", "model": model, "reasoning_effort": reasoning}))
    print(json.dumps({"type": "turn.completed", "model": model, "reasoning_effort": reasoning, "usage": {"input_tokens": 1, "output_tokens": 1}}))
    return 0


def make_plan(prompt: str) -> dict:
    review = {
        "required": False,
        "model": "gpt-5.6-sol",
        "reasoning": "max",
        "acceptance": ["Verify the complete result"],
    }
    permissions = {"commit": True, "push": False, "deploy": False, "external_write": False}

    if "MODE=write-parallel" in prompt:
        tasks = [task("write-a", "WRITE_FILE A", "write"), task("write-b", "WRITE_FILE B", "write")]
        return plan("orchestrated", "S3", "medium", [{"id": "write-wave", "tasks": tasks}], review | {"required": True}, permissions)
    if "MODE=parallel" in prompt:
        tasks = [task("inspect-a", "Inspect A", "read"), task("inspect-b", "Inspect B", "read")]
        return plan("orchestrated", "S3", "medium", [{"id": "inspect-wave", "tasks": tasks}], review | {"required": True}, permissions)
    if "MODE=ultra" in prompt:
        ultra = task("ultra-main", "Coordinate the coupled task", "write")
        ultra["model"] = "gpt-5.6-sol"
        ultra["reasoning"] = "ultra"
        return plan("native-ultra", "S4", "high", [{"id": "ultra-wave", "tasks": [ultra]}], review | {"required": True}, permissions)
    if "MODE=missing-model" in prompt:
        direct = task("missing-model", "Inspect with unavailable model", "read")
        direct["model"] = "gpt-model-does-not-exist"
        return plan("direct", "S1", "low", [{"id": "missing-wave", "tasks": [direct]}], review, permissions)
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


def extract_json(prompt: str, start: str, end: str):
    pattern = re.escape(start) + r"\s*(.*?)\s*" + re.escape(end)
    match = re.search(pattern, prompt, re.DOTALL)
    if not match:
        raise RuntimeError(f"unable to extract JSON between {start!r} and {end!r}")
    return json.loads(match.group(1))


if __name__ == "__main__":
    raise SystemExit(main())
