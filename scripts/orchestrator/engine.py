from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model_catalog import ModelCatalog
from .process_runner import CodexRunner
from .schemas import (
    ValidationError,
    apply_authority_ceiling,
    validate_plan,
    validate_result,
    validate_review,
)
from .state import JobStore, prune_runs
from .util import atomic_write_json, load_json, safe_name, uses_chatgpt_login, utc_now
from .workspace import Worktree, WorkspaceManager


@dataclass(frozen=True)
class Policy:
    name: str
    max_workers: int
    max_retries: int


POLICIES = {
    "economy": Policy("economy", max_workers=2, max_retries=0),
    "balanced": Policy("balanced", max_workers=3, max_retries=1),
    "quality": Policy("quality", max_workers=3, max_retries=2),
}


class OrchestrationError(RuntimeError):
    pass


class Orchestrator:
    def __init__(
        self,
        *,
        workspace: Path,
        run_root: Path,
        codex_command: list[str],
        policy_name: str = "balanced",
    ) -> None:
        if policy_name not in POLICIES:
            raise ValueError(f"unknown policy: {policy_name}")
        self.workspace = workspace.resolve()
        self.run_root = run_root.expanduser().resolve()
        self.codex_command = codex_command
        self.policy = POLICIES[policy_name]
        self.catalog = ModelCatalog.discover(codex_command)
        self.chatgpt_login = uses_chatgpt_login(codex_command)
        read_isolation = os.environ.get(
            "CODEX_ORCHESTRATOR_READ_ISOLATION",
            "snapshot" if os.name == "nt" else "native",
        ).lower()
        if read_isolation not in {"native", "snapshot"}:
            raise ValueError("CODEX_ORCHESTRATOR_READ_ISOLATION must be native or snapshot")
        self.read_isolation = read_isolation
        self._ultra_lock = threading.Lock()
        self._ultra_used = False

    def run(self, task_text: str, dry_run: bool = False) -> JobStore:
        if not task_text.strip():
            raise ValueError("task text cannot be empty")
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace does not exist: {self.workspace}")
        if str(__import__("os").environ.get("CODEX_AUTO_ORCHESTRATOR_WORKER", "")) == "1":
            raise OrchestrationError("recursive orchestrator invocation is disabled for workers")

        prune_runs(self.run_root)
        job_id = _new_job_id()
        store = JobStore.create(self.run_root, job_id, task_text, self.workspace, self.policy.name)
        workspace_manager = WorkspaceManager(self.workspace, job_id)
        runner = CodexRunner(self.codex_command, store, drop_codex_api_key=self.chatgpt_login)
        try:
            preflight = self._write_preflight(store, workspace_manager)
            print(f"[{job_id}] Sol Max is planning the task")
            plan = self._plan(task_text, preflight, runner, store)
            plan = apply_authority_ceiling(plan, task_text)
            plan = self._apply_workspace_policy(plan, workspace_manager)
            self._ultra_used = any(
                task["reasoning"] == "ultra"
                for wave in plan["waves"]
                for task in wave["tasks"]
            )
            plan_path = store.job_dir / "plan.json"
            atomic_write_json(plan_path, plan)
            store.set_artifact("plan", plan_path)
            if dry_run:
                store.transition("complete", "dry-run plan completed")
                self._write_report(store)
                return store

            store.transition("running", f"execution mode: {plan['execution_mode']}")
            results, task_worktrees = self._execute_plan(
                task_text,
                plan,
                runner,
                store,
                workspace_manager,
            )
            failures = [result for result in results.values() if result["status"] != "success"]
            if failures:
                messages = [f"{item['task_id']}: {item['status']} ({item['failure_kind']})" for item in failures]
                raise OrchestrationError("worker tasks did not pass: " + "; ".join(messages))

            store.transition("validating", "all worker results passed structural validation")
            review = self._review_and_integrate(
                task_text,
                plan,
                results,
                task_worktrees,
                runner,
                store,
                workspace_manager,
            )
            if not review["approved"] or review["status"] != "success":
                raise OrchestrationError("final review did not approve the integrated result")
            diff_issues = workspace_manager.diff_check()
            if diff_issues.strip():
                raise OrchestrationError(f"git diff --check failed: {diff_issues}")

            preserved = (
                workspace_manager.cleanup()
                if workspace_manager.worktrees or workspace_manager.read_snapshot
                else []
            )
            if preserved:
                raise OrchestrationError(
                    "isolated workspaces contain changes and were preserved: "
                    + ", ".join(preserved)
                )
            store.transition("complete", "review, integration, validation, and cleanup completed")
            self._write_report(store)
            print(f"[{job_id}] orchestration completed")
            return store
        except KeyboardInterrupt:
            store.request_cancel()
            store.transition("cancelled", "interrupted by user")
            self._write_report(store)
            return store
        except Exception as exc:
            store.add_blocker(str(exc))
            if workspace_manager.worktrees or workspace_manager.read_snapshot:
                try:
                    preserved = workspace_manager.cleanup()
                    if preserved:
                        store.add_blocker(
                            "isolated workspaces with changes were preserved: "
                            + ", ".join(preserved)
                        )
                except Exception as cleanup_exc:
                    store.add_blocker(f"worktree cleanup failed: {cleanup_exc}")
            current_status = store.read()["status"]
            if current_status not in {"cancelled", "complete", "blocked"}:
                if store.cancelled():
                    store.transition("cancelled", str(exc))
                else:
                    store.transition("blocked", str(exc))
            self._write_report(store)
            print(f"[{job_id}] {store.read()['status']}: {exc}")
            return store

    def _write_preflight(self, store: JobStore, workspace: WorkspaceManager) -> dict[str, Any]:
        info = workspace.info
        preflight = {
            "workspace": str(self.workspace),
            "model_catalog_source": self.catalog.source,
            "auth_mode": "chatgpt" if self.chatgpt_login else "configured-api-or-unknown",
            "models": self.catalog.prompt_summary(),
            "git": {
                "is_git": info.is_git,
                "root": str(info.root),
                "branch": info.branch,
                "head": info.head,
                "dirty": info.dirty,
                "porcelain": info.porcelain,
            },
            "policy": {
                "name": self.policy.name,
                "max_workers": self.policy.max_workers,
                "max_retries": self.policy.max_retries,
                "max_ultra_jobs": 1,
                "read_isolation": self.read_isolation,
            },
            "collected_at": utc_now(),
        }
        path = store.job_dir / "preflight.json"
        atomic_write_json(path, preflight)
        store.set_artifact("preflight", path)
        return preflight

    def _plan(
        self,
        task_text: str,
        preflight: dict[str, Any],
        runner: CodexRunner,
        store: JobStore,
    ) -> dict[str, Any]:
        planner_model = self.catalog.preferred_sol()
        self.catalog.require(planner_model, "max")
        schema = _schema_path("plan.schema.json")
        validation_feedback = ""
        last_error = "planner did not return a valid plan"
        for attempt in (1, 2):
            output = store.job_dir / f"plan-attempt-{attempt}.json"
            prompt = _planner_prompt(task_text, preflight, validation_feedback)
            outcome = runner.execute(
                key=f"planner-{attempt}",
                model=planner_model,
                reasoning="max",
                workspace=self.workspace,
                prompt=prompt,
                schema_path=schema,
                output_path=output,
                timeout_seconds=1800,
                read_only=True,
            )
            if outcome.cancelled:
                raise OrchestrationError("planning was cancelled")
            if outcome.timed_out:
                raise OrchestrationError("Sol Max planner timed out")
            if outcome.returncode != 0:
                raise OrchestrationError(outcome.stderr.strip() or "Sol Max planner process failed")
            if outcome.output is None:
                last_error = "planner produced no structured output"
                validation_feedback = last_error
                continue
            try:
                return validate_plan(outcome.output, self.catalog, self.policy.max_workers)
            except ValidationError as exc:
                last_error = str(exc)
                validation_feedback = f"The previous plan was rejected: {exc}. Return a corrected complete plan."
        raise OrchestrationError(last_error)

    def _apply_workspace_policy(
        self,
        plan: dict[str, Any],
        workspace: WorkspaceManager,
    ) -> dict[str, Any]:
        if workspace.info.is_git and not workspace.info.dirty:
            return plan
        if not any(
            task["access"] == "write"
            for wave in plan["waves"]
            for task in wave["tasks"]
        ):
            return plan
        adjusted = deepcopy(plan)
        waves: list[dict[str, Any]] = []
        serial_index = 0
        for wave in adjusted["waves"]:
            reads = [task for task in wave["tasks"] if task["access"] == "read"]
            writes = [task for task in wave["tasks"] if task["access"] == "write"]
            if reads:
                waves.append({"id": f"{safe_name(wave['id'])}-reads", "tasks": reads})
            for task in writes:
                serial_index += 1
                waves.append({"id": f"serial-write-{serial_index}", "tasks": [task]})
        adjusted["waves"] = waves
        adjusted["rationale"] += " Workspace policy serialized write tasks because the workspace is non-Git or dirty."
        return validate_plan(adjusted, self.catalog, self.policy.max_workers)

    def _execute_plan(
        self,
        task_text: str,
        plan: dict[str, Any],
        runner: CodexRunner,
        store: JobStore,
        workspace_manager: WorkspaceManager,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Worktree]]:
        results: dict[str, dict[str, Any]] = {}
        task_worktrees: dict[str, Worktree] = {}
        task_locations: dict[str, tuple[Path, bool, bool]] = {}
        read_snapshot = None
        if self.read_isolation == "snapshot" and any(
            task["access"] == "read"
            for wave in plan["waves"]
            for task in wave["tasks"]
        ):
            read_snapshot = workspace_manager.create_read_snapshot()
        use_isolated_writes = (
            plan["execution_mode"] == "orchestrated"
            and workspace_manager.info.is_git
            and not workspace_manager.info.dirty
        )
        for wave in plan["waves"]:
            if store.cancelled():
                raise OrchestrationError("job was cancelled")
            print(f"[{store.job_id}] running wave {wave['id']} with {len(wave['tasks'])} task(s)")
            prepared: list[tuple[dict[str, Any], Path, bool, bool]] = []
            for task in wave["tasks"]:
                unmet = [dependency for dependency in task["depends_on"] if results.get(dependency, {}).get("status") != "success"]
                if unmet:
                    result = _synthetic_result(task["id"], "blocked", "dependency did not succeed", "coordination", unmet)
                    results[task["id"]] = result
                    atomic_write_json(store.job_dir / f"result-{task['id']}.json", result)
                    continue
                isolated = use_isolated_writes and task["access"] == "write"
                if task["access"] == "read" and read_snapshot is not None:
                    task_workspace = read_snapshot.path
                    sandbox_read_only = False
                elif isolated:
                    worktree = workspace_manager.create_task_worktree(task["id"])
                    task_worktrees[task["id"]] = worktree
                    task_workspace = worktree.path
                    sandbox_read_only = False
                else:
                    task_workspace = workspace_manager.info.root
                    sandbox_read_only = task["access"] == "read"
                task_locations[task["id"]] = (task_workspace, isolated, sandbox_read_only)
                prepared.append((task, task_workspace, isolated, sandbox_read_only))

            with ThreadPoolExecutor(max_workers=min(self.policy.max_workers, max(1, len(prepared)))) as executor:
                futures = {
                    executor.submit(
                        self._run_task,
                        task_text,
                        plan,
                        task,
                        task_workspace,
                        isolated,
                        sandbox_read_only,
                        results,
                        runner,
                        store,
                    ): (task, isolated)
                    for task, task_workspace, isolated, sandbox_read_only in prepared
                }
                for future in as_completed(futures):
                    task, isolated = futures[future]
                    result = future.result()
                    results[task["id"]] = result
                    if isolated and result["status"] == "success":
                        workspace_manager.verify_clean(task_worktrees[task["id"]])

            coordination_failures = [
                task
                for task, _, _, _ in prepared
                if results.get(task["id"], {}).get("failure_kind") == "coordination"
            ]
            for task in coordination_failures:
                if self._claim_ultra():
                    recovery = deepcopy(task)
                    recovery["model"] = self.catalog.preferred_sol()
                    recovery["reasoning"] = "ultra"
                    self.catalog.require(recovery["model"], "ultra")
                    task_workspace, isolated, sandbox_read_only = task_locations[task["id"]]
                    result = self._run_task(
                        task_text,
                        plan,
                        recovery,
                        task_workspace,
                        isolated,
                        sandbox_read_only,
                        results,
                        runner,
                        store,
                        force_single_attempt=True,
                    )
                    results[task["id"]] = result
        return results, task_worktrees

    def _run_task(
        self,
        task_text: str,
        plan: dict[str, Any],
        task: dict[str, Any],
        task_workspace: Path,
        isolated: bool,
        sandbox_read_only: bool,
        prior_results: dict[str, dict[str, Any]],
        runner: CodexRunner,
        store: JobStore,
        force_single_attempt: bool = False,
        attempt_offset: int = 0,
    ) -> dict[str, Any]:
        current = deepcopy(task)
        attempts = 1 if force_single_attempt else self.policy.max_retries + 1
        previous: dict[str, Any] | None = None
        for attempt in range(1, attempts + 1):
            actual_attempt = attempt_offset + attempt
            store.set_task(
                task["id"],
                status="running",
                attempt=actual_attempt,
                model=current["model"],
                reasoning=current["reasoning"],
                workspace=str(task_workspace),
            )
            output_path = store.job_dir / f"result-{task['id']}-attempt-{actual_attempt}.json"
            prompt = _worker_prompt(
                task_text,
                plan,
                current,
                prior_results,
                previous,
                isolated,
                current["access"] == "read" and not sandbox_read_only,
            )
            outcome = runner.execute(
                key=f"worker-{task['id']}-{actual_attempt}",
                model=current["model"],
                reasoning=current["reasoning"],
                workspace=task_workspace,
                prompt=prompt,
                schema_path=_schema_path("result.schema.json"),
                output_path=output_path,
                timeout_seconds=current["timeout_seconds"],
                read_only=sandbox_read_only,
            )
            if outcome.cancelled:
                result = _synthetic_result(task["id"], "cancelled", "worker was cancelled", "cancelled")
            elif outcome.timed_out:
                result = _synthetic_result(task["id"], "failed", "worker timed out", "timeout")
            elif outcome.returncode != 0:
                detail = outcome.stderr.strip() or "worker produced no structured output"
                result = _synthetic_result(task["id"], "failed", detail, "environment")
            elif outcome.output is None:
                result = _synthetic_result(
                    task["id"],
                    "failed",
                    "worker produced malformed or missing structured output",
                    "implementation",
                )
            else:
                try:
                    result = validate_result(outcome.output, task["id"])
                except ValidationError as exc:
                    result = _synthetic_result(task["id"], "failed", str(exc), "implementation")
            previous = result
            if result["status"] == "success":
                canonical = store.job_dir / f"result-{task['id']}.json"
                atomic_write_json(canonical, result)
                store.set_artifact(f"result-{task['id']}", canonical)
                store.set_task(task["id"], status="success", result=str(canonical))
                return result
            if result["failure_kind"] not in {"implementation", "reasoning"} or attempt == attempts:
                break
            current = self._upgrade_task(current)

        canonical = store.job_dir / f"result-{task['id']}.json"
        atomic_write_json(canonical, previous)
        store.set_artifact(f"result-{task['id']}", canonical)
        store.set_task(task["id"], status=previous["status"], result=str(canonical))
        return previous

    def _upgrade_task(self, task: dict[str, Any]) -> dict[str, Any]:
        upgraded = deepcopy(task)
        order = ["low", "medium", "high", "xhigh", "max"]
        current = upgraded["reasoning"]
        if current in order and order.index(current) < len(order) - 1:
            candidate = order[order.index(current) + 1]
            if candidate in self.catalog.models[upgraded["model"]].efforts:
                upgraded["reasoning"] = candidate
                return upgraded
        if "terra" in upgraded["model"]:
            upgraded["model"] = self.catalog.preferred_sol()
            supported = self.catalog.models[upgraded["model"]].efforts
            upgraded["reasoning"] = current if current in supported and current != "ultra" else "high"
        return upgraded

    def _claim_ultra(self) -> bool:
        with self._ultra_lock:
            if self._ultra_used:
                return False
            self._ultra_used = True
            return True

    def _review_and_integrate(
        self,
        task_text: str,
        plan: dict[str, Any],
        results: dict[str, dict[str, Any]],
        task_worktrees: dict[str, Worktree],
        runner: CodexRunner,
        store: JobStore,
        workspace_manager: WorkspaceManager,
    ) -> dict[str, Any]:
        if not plan["final_review"]["required"] and not task_worktrees:
            review = {
                "approved": True,
                "status": "success",
                "summary": "Low-risk direct task passed worker validation; no separate model review was required.",
                "task_assessments": [
                    {
                        "task_id": task_id,
                        "status": "pass",
                        "findings": [],
                        "repair_instructions": [],
                    }
                    for task_id in results
                ],
                "findings": [],
                "changed_files": [],
                "tests": [],
                "integration_commit": "",
                "blockers": [],
                "merge_decision": "approve",
            }
            output_path = store.job_dir / "review.json"
            atomic_write_json(output_path, review)
            store.set_artifact("review", output_path)
            return review
        planned_tasks = {
            task["id"]: task
            for wave in plan["waves"]
            for task in wave["tasks"]
        }
        changed_branches = [
            worktree.branch
            for worktree in task_worktrees.values()
            if workspace_manager.head(worktree) != worktree.base_head
        ]
        integration: Worktree | None = None
        review_workspace = workspace_manager.info.root
        all_read = not any(task["access"] == "write" for task in planned_tasks.values())
        if task_worktrees:
            integration = workspace_manager.create_integration_worktree()
            review_workspace = integration.path
        elif all_read and workspace_manager.read_snapshot is not None:
            review_workspace = workspace_manager.read_snapshot.path

        review_config = plan["final_review"]
        canonical_review = store.job_dir / "review.json"
        review_read_only = all_read and workspace_manager.read_snapshot is None
        for review_attempt in (1, 2):
            store.transition("reviewing", f"fresh Sol Max reviewer pass {review_attempt}")
            prompt = _review_prompt(
                task_text,
                plan,
                results,
                changed_branches,
                bool(integration),
                all_read and workspace_manager.read_snapshot is not None,
            )
            attempt_output = store.job_dir / f"review-attempt-{review_attempt}.json"
            outcome = runner.execute(
                key=f"final-review-{review_attempt}",
                model=review_config["model"],
                reasoning=review_config["reasoning"],
                workspace=review_workspace,
                prompt=prompt,
                schema_path=_schema_path("review.schema.json"),
                output_path=attempt_output,
                timeout_seconds=3600,
                read_only=review_read_only,
            )
            if outcome.cancelled:
                raise OrchestrationError("final review was cancelled")
            if outcome.timed_out:
                raise OrchestrationError("final review timed out")
            if outcome.returncode != 0 or outcome.output is None:
                raise OrchestrationError(
                    outcome.stderr.strip() or "final review produced no structured output"
                )
            review = validate_review(outcome.output, set(planned_tasks))
            atomic_write_json(canonical_review, review)
            store.set_artifact("review", canonical_review)
            if review["approved"] and review["status"] == "success":
                if integration:
                    store.transition("integrating", "applying approved integration branch")
                    workspace_manager.verify_clean(integration)
                    applied_head = workspace_manager.apply_integration(integration)
                    review["integration_commit"] = applied_head
                    atomic_write_json(canonical_review, review)
                return review
            if review["merge_decision"] != "repair":
                return review
            if review_attempt == 2:
                review["status"] = "blocked"
                review["merge_decision"] = "block"
                review["blockers"].append("review repair budget exhausted")
                atomic_write_json(canonical_review, review)
                return review
            if integration:
                workspace_manager.verify_clean(integration)

            repair_assessments = [
                assessment
                for assessment in review["task_assessments"]
                if assessment["status"] == "repair"
            ]
            store.transition(
                "repairing",
                "targeted repair: "
                + ", ".join(assessment["task_id"] for assessment in repair_assessments),
            )
            repair_failures: list[str] = []
            for assessment in repair_assessments:
                task_id = assessment["task_id"]
                prior_attempt = int(store.read()["tasks"].get(task_id, {}).get("attempt", 1))
                if prior_attempt > 1:
                    repair_failures.append(f"{task_id}: repair budget already used")
                    continue
                repaired_task = deepcopy(planned_tasks[task_id])
                repaired_task["objective"] += "\n\nReview repair instructions:\n- " + "\n- ".join(
                    assessment["repair_instructions"]
                )
                target = task_worktrees.get(task_id)
                read_snapshot = (
                    workspace_manager.read_snapshot
                    if repaired_task["access"] == "read"
                    else None
                )
                repaired = self._run_task(
                    task_text,
                    plan,
                    repaired_task,
                    target.path
                    if target
                    else read_snapshot.path
                    if read_snapshot
                    else workspace_manager.info.root,
                    bool(target),
                    repaired_task["access"] == "read" and read_snapshot is None,
                    results,
                    runner,
                    store,
                    force_single_attempt=True,
                    attempt_offset=prior_attempt,
                )
                results[task_id] = repaired
                if repaired["status"] != "success":
                    repair_failures.append(
                        f"{task_id}: {repaired['status']} ({repaired['failure_kind']})"
                    )
                elif target:
                    workspace_manager.verify_clean(target)
            if repair_failures:
                review["status"] = "blocked"
                review["merge_decision"] = "block"
                review["blockers"].extend(repair_failures)
                atomic_write_json(canonical_review, review)
                return review
        raise OrchestrationError("review loop ended unexpectedly")

    def _write_report(self, store: JobStore) -> Path:
        state = store.read()
        plan_path = store.job_dir / "plan.json"
        plan = load_json(plan_path) if plan_path.is_file() else None
        lines = [
            f"# Orchestration report: {store.job_id}",
            "",
            f"- Status: `{state['status']}`",
            f"- Workspace: `{state['workspace']}`",
            f"- Policy: `{state['policy']}`",
            f"- Created: `{state['created_at']}`",
            f"- Updated: `{state['updated_at']}`",
        ]
        if plan:
            lines.extend(
                [
                    f"- Execution mode: `{plan['execution_mode']}`",
                    f"- Complexity/risk: `{plan['complexity']}` / `{plan['risk']}`",
                    "",
                    "## Routing",
                    "",
                ]
            )
            for wave in plan["waves"]:
                for task in wave["tasks"]:
                    lines.append(
                        f"- `{wave['id']}/{task['id']}`: `{task['model']}` + `{task['reasoning']}` ({task['access']})"
                    )
        lines.extend(["", "## Results", ""])
        for task_id, task_state in state["tasks"].items():
            lines.append(
                f"- `{task_id}`: `{task_state.get('status', 'unknown')}` via "
                f"`{task_state.get('model', 'unknown')}` + `{task_state.get('reasoning', 'unknown')}`"
            )
        if state["blockers"]:
            lines.extend(["", "## Blockers", ""])
            lines.extend(f"- {message}" for message in state["blockers"])
        invocation_records = []
        invocation_root = store.job_dir / "invocations"
        if invocation_root.is_dir():
            for evidence_path in sorted(invocation_root.glob("*/invocation.json")):
                invocation_records.append(load_json(evidence_path))
        if invocation_records:
            lines.extend(["", "## Model evidence", ""])
            for evidence in invocation_records:
                observed_models = ", ".join(evidence.get("observed_models", [])) or "unavailable"
                observed_reasoning = ", ".join(evidence.get("observed_reasoning", [])) or "unavailable"
                verdict = "verified" if evidence.get("actual_selection_verified") else "unverified"
                lines.append(
                    f"- `{evidence['key']}`: requested `{evidence['requested_model']}` + "
                    f"`{evidence['requested_reasoning']}`; observed `{observed_models}` + "
                    f"`{observed_reasoning}` — **{verdict}**"
                )
        lines.extend(["", "## Evidence", ""])
        lines.append(f"- State: `{store.state_path}`")
        lines.append(f"- Invocations: `{store.job_dir / 'invocations'}`")
        lines.append(f"- Structured artifacts: `{store.job_dir}`")
        report = store.job_dir / "report.md"
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        store.set_artifact("report", report)
        return report


def render_report(store: JobStore) -> str:
    report = store.job_dir / "report.md"
    if report.is_file():
        return report.read_text(encoding="utf-8")
    state = store.read()
    return json.dumps(state, ensure_ascii=False, indent=2)


def _new_job_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _schema_path(name: str) -> Path:
    return Path(__file__).resolve().parent.parent / "schemas" / name


def _planner_prompt(task_text: str, preflight: dict[str, Any], feedback: str) -> str:
    return f"""<codex-orchestrator-worker>
ROLE: planner

Plan the complete task without modifying any file or external system. Choose the execution mode before choosing workers. Return only the JSON object required by the supplied schema.

Original task:
{task_text}

Verified preflight:
{json.dumps(preflight, ensure_ascii=False, indent=2)}

Rules:
- direct: exactly one non-Ultra task for one coherent execution path.
- orchestrated: at least two independent non-Ultra tasks, with no more than {preflight['policy']['max_workers']} tasks per wave. Dependencies must point only to earlier waves.
- native-ultra: exactly one task using Sol or Terra with reasoning ultra. Use this only for strong coupling, shared global state, or continuous replanning.
- Terra handles routine implementation, search, tests, docs, and mechanical work. Sol handles core code, architecture, novel failures, and high risk.
- Ultra is native automatic delegation, not a generic retry level.
- Mark access=read unless the task must change local or external state.
- Use allowed_paths to narrow write scope. Use an empty array only when scope cannot be narrowed.
- final_review must use a non-Ultra model. Require it for orchestrated, native-ultra, or medium-and-higher risk work.
- Set permissions true only when the original task explicitly authorizes them.
- Every wave ID, task ID, and dependency ID must be a lowercase ASCII identifier matching ^[a-z0-9][a-z0-9_-]{{0,63}}$.
- Each task needs concrete acceptance criteria and a timeout from 60 to 14400 seconds.
- Do not invoke plugins, other Codex CLI sessions, or subagents while planning.

Correction feedback:
{feedback or 'none'}
"""


def _worker_prompt(
    task_text: str,
    plan: dict[str, Any],
    task: dict[str, Any],
    prior_results: dict[str, dict[str, Any]],
    previous: dict[str, Any] | None,
    isolated: bool,
    isolated_read_snapshot: bool,
) -> str:
    delegation = (
        "Native task delegation is allowed, but do not invoke this plugin or launch external Codex CLI sessions."
        if task["reasoning"] == "ultra"
        else "Do not delegate or launch other agents; complete only this bounded task."
    )
    if isolated_read_snapshot:
        commit_rule = (
            "This current directory is a disposable read snapshot. Do not modify it, do not access "
            "the original workspace or absolute local paths outside this snapshot, and treat allowed_paths "
            "as logical paths relative to the current directory."
        )
    elif isolated:
        commit_rule = "This is an isolated Git worktree. Commit all intentional changes to the current branch and leave the worktree clean."
    else:
        commit_rule = "Do not commit, push, or deploy unless the permissions below explicitly allow it."
    dependencies = {key: prior_results[key] for key in task["depends_on"] if key in prior_results}
    return f"""<codex-orchestrator-worker>
ROLE: executor

Complete the assigned task and return only the JSON object required by the supplied schema.

Original user task:
{task_text}

Overall plan summary: {plan['summary']}
Permissions: {json.dumps(plan['permissions'], ensure_ascii=False)}
Assigned task:
{json.dumps(task, ensure_ascii=False, indent=2)}

Dependency results:
{json.dumps(dependencies, ensure_ascii=False, indent=2)}

Previous failed attempt:
{json.dumps(previous, ensure_ascii=False, indent=2) if previous else 'none'}

Execution rules:
- {delegation}
- {commit_rule}
- Respect allowed_paths and all applicable AGENTS.md files.
- Inspect current state before changing it; never discard unknown changes.
- Run the acceptance checks that are actually available and report unrun checks honestly.
- Never expand push, deployment, destructive cleanup, or external-write authority.
- Set failure_kind precisely. Environment and permission failures are not reasoning failures.
"""


def _review_prompt(
    task_text: str,
    plan: dict[str, Any],
    results: dict[str, dict[str, Any]],
    branches: list[str],
    isolated_integration: bool,
    isolated_read_snapshot: bool,
) -> str:
    if isolated_read_snapshot:
        integration_rules = (
            "Review only the current disposable read snapshot. Do not modify it and do not access "
            "the original workspace or absolute local paths outside this snapshot."
        )
    elif isolated_integration:
        integration_rules = "Merge each listed worker branch into the current integration branch in the listed order. Resolve conflicts by the original task and acceptance criteria. Commit all integration fixes and leave the worktree clean."
    else:
        integration_rules = "Review the current workspace in place. Fix only defects required to satisfy the original task and acceptance criteria."
    return f"""<codex-orchestrator-worker>
ROLE: final reviewer and integrator

Independently verify the complete outcome. Return only the JSON object required by the supplied schema.

Original task:
{task_text}

Plan:
{json.dumps(plan, ensure_ascii=False, indent=2)}

Worker results:
{json.dumps(results, ensure_ascii=False, indent=2)}

Worker branches:
{json.dumps(branches, ensure_ascii=False)}

Rules:
- {integration_rules}
- Do not invoke the orchestration plugin, launch external Codex sessions, or use Ultra.
- Inspect actual diffs and current files; do not trust worker summaries alone.
- Run relevant tests, documentation checks, and git diff --check.
- Return exactly one task_assessments entry for every planned task.
- Fix safe integration-only defects yourself. If a specific worker must be redone, mark only that task as repair and provide concrete repair_instructions.
- Set merge_decision to approve only when every task passes, repair only when at least one task needs targeted rework, or block when repair cannot make the result acceptable.
- Do not approve while a material finding or required test failure remains.
- Do not push, deploy, or write to external systems unless the original permissions explicitly allow it.
- If approved, status must be success. If unresolved, return failed or blocked with concrete blockers.
"""


def _synthetic_result(
    task_id: str,
    status: str,
    summary: str,
    failure_kind: str,
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "changed_files": [],
        "tests": [],
        "commit": "",
        "artifacts": [],
        "blockers": blockers or ([summary] if status != "success" else []),
        "uncertainties": [],
        "failure_kind": failure_kind,
    }
