from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .model_catalog import ModelCatalog


class ValidationError(ValueError):
    pass


REQUIRED_RESULT_FIELDS = {
    "task_id",
    "status",
    "summary",
    "changed_files",
    "tests",
    "commit",
    "artifacts",
    "blockers",
    "uncertainties",
    "failure_kind",
}

REQUIRED_REVIEW_FIELDS = {
    "approved",
    "status",
    "summary",
    "task_assessments",
    "findings",
    "changed_files",
    "tests",
    "integration_commit",
    "blockers",
    "merge_decision",
}


def validate_plan(raw: Any, catalog: ModelCatalog, max_workers: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("plan must be an object")
    plan = deepcopy(raw)
    required = {
        "version",
        "summary",
        "complexity",
        "risk",
        "execution_mode",
        "rationale",
        "waves",
        "final_review",
        "permissions",
    }
    _exact_fields(plan, required, "plan")
    if plan["version"] != 1:
        raise ValidationError("unsupported plan version")
    if plan["execution_mode"] not in {"direct", "orchestrated", "native-ultra"}:
        raise ValidationError("invalid execution mode")
    if plan["complexity"] not in {"S1", "S2", "S3", "S4"}:
        raise ValidationError("invalid complexity")
    if plan["risk"] not in {"low", "medium", "high", "critical"}:
        raise ValidationError("invalid risk")
    _require_nonempty_string(plan["summary"], "plan summary")
    _require_nonempty_string(plan["rationale"], "plan rationale")
    if not isinstance(plan["waves"], list) or not plan["waves"]:
        raise ValidationError("plan must contain at least one wave")

    seen_tasks: set[str] = set()
    prior_tasks: set[str] = set()
    all_tasks: list[dict[str, Any]] = []
    for wave in plan["waves"]:
        _exact_fields(wave, {"id", "tasks"}, "wave")
        if not _valid_id(wave["id"]):
            raise ValidationError(f"invalid wave id: {wave['id']!r}")
        if not isinstance(wave["tasks"], list) or not wave["tasks"]:
            raise ValidationError(f"wave {wave['id']} has no tasks")
        if len(wave["tasks"]) > max_workers:
            raise ValidationError(f"wave {wave['id']} exceeds max concurrency {max_workers}")
        wave_ids: set[str] = set()
        for task in wave["tasks"]:
            required_task = {
                "id",
                "title",
                "objective",
                "model",
                "reasoning",
                "depends_on",
                "access",
                "allowed_paths",
                "acceptance",
                "timeout_seconds",
            }
            _exact_fields(task, required_task, f"task {task.get('id', '<unknown>')}")
            task_id = task["id"]
            if not _valid_id(task_id):
                raise ValidationError(f"invalid task id: {task_id!r}")
            if task_id in seen_tasks:
                raise ValidationError(f"duplicate task id: {task_id}")
            if task["access"] not in {"read", "write"}:
                raise ValidationError(f"invalid access for {task_id}")
            _require_nonempty_string(task["title"], f"title for {task_id}")
            _require_nonempty_string(task["objective"], f"objective for {task_id}")
            if not _string_array(task["depends_on"]):
                raise ValidationError(f"dependencies for {task_id} must be a string array")
            if (
                len(task["depends_on"]) != len(set(task["depends_on"]))
                or not set(task["depends_on"]).issubset(prior_tasks)
            ):
                raise ValidationError(f"dependencies for {task_id} must reference prior waves")
            if not _string_array(task["allowed_paths"]):
                raise ValidationError(f"allowed paths for {task_id} must be a string array")
            if not _string_array(task["acceptance"]) or not task["acceptance"]:
                raise ValidationError(f"task {task_id} needs acceptance criteria")
            if not isinstance(task["timeout_seconds"], int) or not 60 <= task["timeout_seconds"] <= 14400:
                raise ValidationError(f"invalid timeout for {task_id}")
            _require_model(catalog, task["model"], task["reasoning"])
            seen_tasks.add(task_id)
            wave_ids.add(task_id)
            all_tasks.append(task)
        prior_tasks.update(wave_ids)

    mode = plan["execution_mode"]
    ultra_tasks = [task for task in all_tasks if task["reasoning"] == "ultra"]
    if mode == "direct" and (len(all_tasks) != 1 or ultra_tasks):
        raise ValidationError("direct mode requires exactly one non-Ultra task")
    if mode == "orchestrated" and (len(all_tasks) < 2 or ultra_tasks):
        raise ValidationError("orchestrated mode requires at least two non-Ultra tasks")
    if mode == "native-ultra" and (len(all_tasks) != 1 or len(ultra_tasks) != 1):
        raise ValidationError("native-ultra mode requires exactly one Ultra task")

    review = plan["final_review"]
    _exact_fields(review, {"required", "model", "reasoning", "acceptance"}, "final_review")
    if not isinstance(review["required"], bool):
        raise ValidationError("final_review required must be boolean")
    if not _string_array(review["acceptance"]):
        raise ValidationError("final_review acceptance must be a string array")
    _require_model(catalog, review["model"], review["reasoning"])
    if review["reasoning"] == "ultra":
        raise ValidationError("final review cannot recursively use Ultra")
    if mode != "direct" or plan["risk"] != "low":
        review["required"] = True
    if review["required"]:
        review["model"] = catalog.preferred_sol()
        review["reasoning"] = "max"
        _require_model(catalog, review["model"], review["reasoning"])

    permissions = plan["permissions"]
    _exact_fields(permissions, {"commit", "push", "deploy", "external_write"}, "permissions")
    if not all(isinstance(value, bool) for value in permissions.values()):
        raise ValidationError("permission values must be booleans")
    return plan


def validate_result(raw: Any, expected_task_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("worker result must be an object")
    _exact_fields(raw, REQUIRED_RESULT_FIELDS, "worker result")
    if raw["task_id"] != expected_task_id:
        raise ValidationError("worker result task id does not match")
    if raw["status"] not in {"success", "failed", "blocked", "cancelled"}:
        raise ValidationError("invalid worker status")
    if raw["failure_kind"] not in {
        "none",
        "environment",
        "permission",
        "implementation",
        "reasoning",
        "coordination",
        "timeout",
        "cancelled",
    }:
        raise ValidationError("invalid failure kind")
    _require_nonempty_string(raw["summary"], "worker result summary")
    if not isinstance(raw["commit"], str):
        raise ValidationError("commit must be a string")
    for field in ("changed_files", "tests", "artifacts", "blockers", "uncertainties"):
        if not isinstance(raw[field], list) or not all(isinstance(value, str) for value in raw[field]):
            raise ValidationError(f"{field} must be a string array")
    return raw


def validate_review(raw: Any, expected_task_ids: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("review must be an object")
    _exact_fields(raw, REQUIRED_REVIEW_FIELDS, "review")
    if raw["status"] not in {"success", "failed", "blocked"}:
        raise ValidationError("invalid review status")
    if not isinstance(raw["approved"], bool):
        raise ValidationError("approved must be boolean")
    _require_nonempty_string(raw["summary"], "review summary")
    for field in ("findings", "changed_files", "tests", "blockers"):
        if not _string_array(raw[field]):
            raise ValidationError(f"{field} must be a string array")
    if not isinstance(raw["integration_commit"], str):
        raise ValidationError("integration_commit must be a string")
    if not isinstance(raw["task_assessments"], list):
        raise ValidationError("task_assessments must be an array")
    assessment_ids: set[str] = set()
    repair_count = 0
    for assessment in raw["task_assessments"]:
        _exact_fields(
            assessment,
            {"task_id", "status", "findings", "repair_instructions"},
            "task assessment",
        )
        task_id = assessment["task_id"]
        if not _valid_id(task_id) or task_id in assessment_ids:
            raise ValidationError(f"invalid or duplicate assessment task id: {task_id!r}")
        if assessment["status"] not in {"pass", "repair", "blocked"}:
            raise ValidationError(f"invalid assessment status for {task_id}")
        if not _string_array(assessment["findings"]) or not _string_array(
            assessment["repair_instructions"]
        ):
            raise ValidationError(f"assessment arrays for {task_id} must contain strings")
        if assessment["status"] == "repair":
            repair_count += 1
            if not assessment["repair_instructions"]:
                raise ValidationError(f"repair assessment for {task_id} needs instructions")
        assessment_ids.add(task_id)
    if expected_task_ids is not None and assessment_ids != expected_task_ids:
        raise ValidationError("review must assess every planned task exactly once")
    decision = raw["merge_decision"]
    if decision not in {"approve", "repair", "block"}:
        raise ValidationError("invalid merge decision")
    if raw["approved"] != (decision == "approve"):
        raise ValidationError("approved and merge_decision disagree")
    if decision == "approve" and raw["status"] != "success":
        raise ValidationError("approved review must have success status")
    if decision == "repair" and repair_count == 0:
        raise ValidationError("repair decision requires at least one repair assessment")
    return raw


def apply_authority_ceiling(plan: dict[str, Any], task_text: str) -> dict[str, Any]:
    text = task_text.lower()
    ceilings = {
        "commit": any(token in text for token in ("implement", "修改", "创建", "修复", "提交", "build", "完成")),
        "push": any(token in text for token in ("push", "github", "三端同步", "推送")),
        "deploy": any(token in text for token in ("deploy", "部署", "上线", "服务器镜像", "三端同步")),
        "external_write": any(token in text for token in ("cloudflare", " cf ", "服务器", "github", "部署", "推送", "外部")),
    }
    for name, ceiling in ceilings.items():
        plan["permissions"][name] = bool(plan["permissions"][name] and ceiling)
    return plan


def _exact_fields(value: dict[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object")
    missing = expected - set(value)
    extra = set(value) - expected
    if missing or extra:
        raise ValidationError(f"{label} fields differ: missing={sorted(missing)}, extra={sorted(extra)}")


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value) is not None


def _string_array(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _require_nonempty_string(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty string")


def _require_model(catalog: ModelCatalog, model: Any, reasoning: Any) -> None:
    if not isinstance(model, str) or not isinstance(reasoning, str):
        raise ValidationError("model and reasoning must be strings")
    try:
        catalog.require(model, reasoning)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
