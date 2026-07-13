from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model_catalog import ModelCatalog
from .process_runner import CodexRunner
from .control import ControlQueue
from .schemas import (
    ValidationError,
    apply_authority_ceiling,
    validate_plan,
    validate_result,
    validate_review,
)
from .speed_profiles import ResolvedSpeedPolicy, builtin_matrix, normalize_matrix
from .state import JobStore, TERMINAL_STATES, prune_runs
from .util import atomic_write_json, load_json, safe_name, sha256_text, uses_chatgpt_login, utc_now
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


class JobCancelled(OrchestrationError):
    pass


class ControllerInterrupted(OrchestrationError):
    pass


class ReplanRequested(OrchestrationError):
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

    def create_job(
        self,
        task_text: str,
        *,
        origin_thread_id: str = "",
        parent_job_id: str = "",
        entry_context: dict[str, Any] | None = None,
        initial_status: str = "planning",
        controller_config: dict[str, Any] | None = None,
    ) -> JobStore:
        if not task_text.strip():
            raise ValueError("task text cannot be empty")
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace does not exist: {self.workspace}")
        if os.environ.get("CODEX_AUTO_ORCHESTRATOR_WORKER", "") == "1":
            raise OrchestrationError("recursive orchestrator invocation is disabled for workers")
        prune_runs(self.run_root)
        job_id = _new_job_id()
        return JobStore.create(
            self.run_root,
            job_id,
            task_text,
            self.workspace,
            self.policy.name,
            origin_thread_id=origin_thread_id,
            parent_job_id=parent_job_id,
            initial_status=initial_status,
            controller_config=controller_config,
            entry_context=entry_context,
        )

    def run(
        self,
        task_text: str,
        dry_run: bool = False,
        speed_policy: ResolvedSpeedPolicy | None = None,
    ) -> JobStore:
        store = self.create_job(task_text)
        if speed_policy is None:
            speed_policy = ResolvedSpeedPolicy(
                profile_name="balanced",
                matrix=builtin_matrix("balanced", self.catalog),
                model_bindings={
                    family: str(item["model"])
                    for family, item in self.catalog.speed_matrix_catalog().items()
                },
                catalog_fingerprint=self.catalog.fingerprint(),
                known_combinations=sorted(self.catalog.speed_combinations()),
                source="programmatic-default",
            )
        store.set_speed_policy(speed_policy.to_dict())
        return self.run_store(store, dry_run=dry_run)

    def run_store(self, store: JobStore, dry_run: bool = False) -> JobStore:
        task_text = store.task_text()
        if not store.speed_policy_path.is_file():
            raise OrchestrationError("speed policy must be resolved before the Sol Max planner starts")
        workspace_manager = WorkspaceManager(self.workspace, store.job_id, store=store)
        runner = CodexRunner(self.codex_command, store, drop_codex_api_key=self.chatgpt_login)
        try:
            preflight = self._write_preflight(store, workspace_manager)
            plan_path = store.job_dir / "plan.json"
            if plan_path.is_file() and int(store.read().get("plan_revision", 0)) > 0:
                plan = load_json(plan_path)
            else:
                print(f"[{store.job_id}] Sol Max is planning the task")
                store.transition("planning", "speed policy resolved; Sol Max planner starting")
                plan = self._plan(self._effective_task_text(store), preflight, runner, store)
                plan = apply_authority_ceiling(plan, task_text)
                plan = self._apply_workspace_policy(plan, workspace_manager)
                self._save_plan(store, plan)
            self._ultra_used = any(
                task["reasoning"] == "ultra"
                for wave in plan["waves"]
                for task in wave["tasks"]
            )
            if dry_run:
                store.transition("complete", "dry-run plan completed")
                self._write_report(store)
                return store
            while True:
                store.transition("running", f"execution mode: {plan['execution_mode']}")
                try:
                    results, task_worktrees = self._execute_plan(
                        self._effective_task_text(store),
                        plan,
                        runner,
                        store,
                        workspace_manager,
                    )
                    break
                except ReplanRequested:
                    store.transition("planning", "user replacement steering requested replanning")
                    preflight = self._write_preflight(store, workspace_manager)
                    plan = self._plan(self._effective_task_text(store), preflight, runner, store)
                    plan = apply_authority_ceiling(plan, task_text)
                    plan = self._apply_workspace_policy(plan, workspace_manager)
                    self._save_plan(store, plan)
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
            print(f"[{store.job_id}] orchestration completed")
            return store
        except KeyboardInterrupt:
            store.request_cancel()
            store.transition("cancelled", "interrupted by user")
            self._write_report(store)
            return store
        except JobCancelled as exc:
            if workspace_manager.worktrees or workspace_manager.read_snapshot:
                try:
                    preserved = workspace_manager.cleanup()
                    if preserved:
                        store.add_blocker(
                            "cancelled job preserved isolated workspaces with changes: "
                            + ", ".join(preserved)
                        )
                except Exception as cleanup_exc:
                    store.add_blocker(f"cancel cleanup failed: {cleanup_exc}")
            if store.read()["status"] not in TERMINAL_STATES:
                store.transition("cancelled", str(exc))
            self._write_report(store)
            return store
        except ControllerInterrupted as exc:
            if store.read()["status"] != "interrupted":
                store.transition("interrupted", str(exc))
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
            if current_status not in TERMINAL_STATES:
                if store.cancelled():
                    store.transition("cancelled", str(exc))
                else:
                    store.transition("blocked", str(exc))
            self._write_report(store)
            print(f"[{store.job_id}] {store.read()['status']}: {exc}")
            return store

    def _save_plan(self, store: JobStore, plan: dict[str, Any]) -> None:
        state = store.read()
        revision = int(state.get("plan_revision", 0)) + 1
        revision_path = store.job_dir / f"plan-revision-{revision}.json"
        canonical = store.job_dir / "plan.json"
        atomic_write_json(revision_path, plan)
        atomic_write_json(canonical, plan)
        store.set_artifact("plan", canonical)
        store.set_artifact(f"plan-revision-{revision}", revision_path)
        store.mutate(lambda current: current.__setitem__("plan_revision", revision))
        store.set_checkpoint(phase="planned", wave_index=0, safe=True)

    def _effective_task_text(self, store: JobStore) -> str:
        state = store.read()
        steering = [
            item.get("instruction", "")
            for item in state.get("steering", [])
            if item.get("status") == "applied" and item.get("instruction")
        ]
        if not steering:
            return store.task_text()
        return store.task_text() + "\n\nUser steering received during execution:\n- " + "\n- ".join(steering)

    def _speed_policy(self, store: JobStore) -> ResolvedSpeedPolicy:
        return ResolvedSpeedPolicy.from_dict(store.read_speed_policy())

    def _tier_for(self, store: JobStore, model: str, reasoning: str) -> tuple[str, str, int]:
        policy = self._speed_policy(store)
        return (
            policy.tier_for(self.catalog, model, reasoning),
            policy.profile_name,
            policy.revision,
        )

    def _write_preflight(self, store: JobStore, workspace: WorkspaceManager) -> dict[str, Any]:
        info = workspace.info
        speed_policy = store.read_speed_policy()
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
            "speed_policy": {
                "profile_name": speed_policy["profile_name"],
                "revision": speed_policy.get("revision", 1),
                "matrix": speed_policy["matrix"],
                "catalog_fingerprint": speed_policy["catalog_fingerprint"],
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
        service_tier, speed_profile, speed_revision = self._tier_for(
            store, planner_model, "max"
        )
        schema = store.job_dir / "plan-output.schema.json"
        atomic_write_json(schema, _plan_schema_for_catalog(self.catalog))
        store.set_artifact("plan-output-schema", schema)
        validation_feedback = ""
        last_error = "planner did not return a valid plan"
        for attempt in (1, 2):
            output = store.job_dir / f"plan-attempt-{attempt}.json"
            prompt = _planner_prompt(task_text, preflight, validation_feedback)
            outcome = runner.execute(
                key=f"planner-{attempt}",
                model=planner_model,
                reasoning="max",
                service_tier=service_tier,
                speed_profile_name=speed_profile,
                speed_policy_revision=speed_revision,
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
            if not outcome.model_reasoning_verified or not outcome.service_tier_acceptable:
                raise OrchestrationError(
                    "Sol Max planner runtime evidence rejected the requested model/reasoning "
                    "or could not account for the explicit service-tier request"
                )
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
        for wave in plan["waves"]:
            for task in wave["tasks"]:
                canonical = store.job_dir / f"result-{task['id']}.json"
                if canonical.is_file():
                    candidate = load_json(canonical)
                    if candidate.get("status") == "success":
                        results[task["id"]] = candidate
        task_worktrees: dict[str, Worktree] = {
            item.task_id: item
            for item in workspace_manager.worktrees
            if not item.integration and item.path.exists()
        }
        task_locations: dict[str, tuple[Path, bool, bool]] = {}
        read_snapshot = None
        if self.read_isolation == "snapshot" and any(
            task["access"] == "read"
            for wave in plan["waves"]
            for task in wave["tasks"]
        ):
            read_snapshot = workspace_manager.create_read_snapshot()
        use_isolated_writes = workspace_manager.info.is_git and not workspace_manager.info.dirty
        for wave_index, wave in enumerate(plan["waves"]):
            self._process_controls(store, boundary=f"before-wave:{wave['id']}")
            if store.cancelled():
                raise JobCancelled("job was cancelled")
            print(f"[{store.job_id}] running wave {wave['id']} with {len(wave['tasks'])} task(s)")
            prepared: list[tuple[dict[str, Any], Path, bool, bool]] = []
            for task in wave["tasks"]:
                if results.get(task["id"], {}).get("status") == "success":
                    continue
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
            store.set_checkpoint(
                phase="running",
                wave_index=wave_index + 1,
                wave_id=wave["id"],
                safe=True,
            )
            self._process_controls(store, boundary=f"after-wave:{wave['id']}")
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
        external_write_allowed = any(
            bool(plan["permissions"].get(name))
            for name in ("push", "deploy", "external_write")
        )
        action_fingerprint = sha256_text(
            json.dumps(
                {
                    "job_id": store.job_id,
                    "task_id": task["id"],
                    "objective": task["objective"],
                    "permissions": plan["permissions"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        prior_state = store.read()
        prior_action = prior_state.get("external_actions", {}).get(action_fingerprint, {})
        prior_task = prior_state.get("tasks", {}).get(task["id"], {})
        resumable_external = (
            prior_task.get("status") in {"running", "interrupted"}
            and bool(prior_task.get("session_id"))
        )
        if (
            external_write_allowed
            and prior_action.get("status") in {"started", "uncertain"}
            and not resumable_external
        ):
            verification = self._reconcile_external_action(
                task_text,
                plan,
                task,
                task_workspace,
                action_fingerprint,
                runner,
                store,
                phase="recovery",
            )
            if verification["verdict"] == "completed":
                result = _synthetic_result(
                    task["id"], "success", verification["summary"], "none"
                )
                result["tests"] = list(verification["evidence"])
                store.record_external_action(
                    action_fingerprint,
                    status="completed-reconciled",
                    reconciliation=verification,
                    reconciliation_required=False,
                )
                return self._persist_success_result(
                    store, task["id"], result, session_id=str(prior_task.get("session_id", ""))
                )
            if verification["verdict"] == "not_applied":
                store.record_external_action(
                    action_fingerprint,
                    status="verified-not-applied",
                    reconciliation=verification,
                    reconciliation_required=False,
                )
            else:
                result = _synthetic_result(
                    task["id"],
                    "blocked",
                    "an earlier external-write-capable invocation remains uncertain after read-only reconciliation",
                    "permission",
                    list(verification["evidence"]) or [verification["summary"]],
                )
                canonical = store.job_dir / f"result-{task['id']}.json"
                atomic_write_json(canonical, result)
                store.set_artifact(f"result-{task['id']}", canonical)
                store.set_task(task["id"], status="blocked", result=str(canonical))
                return result
        # An external write may have succeeded even when its final response was
        # lost. Never mechanically retry such an invocation under a new session.
        attempts = (
            1
            if force_single_attempt
            else 2
            if external_write_allowed
            else self.policy.max_retries + 1
        )
        previous: dict[str, Any] | None = None
        for attempt in range(1, attempts + 1):
            actual_attempt = attempt_offset + attempt
            service_tier, speed_profile, speed_revision = self._tier_for(
                store, current["model"], current["reasoning"]
            )
            prior_task_state = store.read()["tasks"].get(task["id"], {})
            resume_session_id = ""
            if (
                prior_task_state.get("status") in {"running", "interrupted"}
                and prior_task_state.get("workspace") == str(task_workspace)
                and prior_task_state.get("session_id")
            ):
                resume_session_id = str(prior_task_state["session_id"])
            invocation_key = f"worker-{task['id']}-{actual_attempt}"
            store.set_task(
                task["id"],
                status="running",
                attempt=actual_attempt,
                model=current["model"],
                reasoning=current["reasoning"],
                service_tier=service_tier,
                speed_profile=speed_profile,
                speed_policy_revision=speed_revision,
                workspace=str(task_workspace),
                invocation_key=invocation_key,
            )
            if external_write_allowed:
                store.record_external_action(
                    action_fingerprint,
                    status="started",
                    task_id=task["id"],
                    invocation_key=invocation_key,
                    permissions={
                        name: bool(plan["permissions"].get(name))
                        for name in ("push", "deploy", "external_write")
                    },
                    plan_revision=int(store.read().get("plan_revision", 0)),
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
            while True:
                outcome = runner.execute(
                    key=invocation_key if not resume_session_id else f"{invocation_key}-resume",
                    model=current["model"],
                    reasoning=current["reasoning"],
                    service_tier=service_tier,
                    speed_profile_name=speed_profile,
                    speed_policy_revision=speed_revision,
                    workspace=task_workspace,
                    prompt=prompt,
                    schema_path=_schema_path("result.schema.json"),
                    output_path=output_path,
                    timeout_seconds=current["timeout_seconds"],
                    read_only=sandbox_read_only,
                    resume_session_id=resume_session_id,
                    allow_service_tier_retry=not external_write_allowed,
                    interrupt_check=lambda: self._immediate_interrupt_reason(
                        store, current, safe_to_interrupt=(current["access"] == "read" or isolated)
                    ),
                )
                if not outcome.interrupted:
                    break
                store.set_task(
                    task["id"],
                    status="interrupted",
                    session_id=outcome.session_id,
                    interrupt_reason=outcome.interrupt_reason,
                )
                self._process_controls(store, boundary=f"interrupted:{task['id']}")
                service_tier, speed_profile, speed_revision = self._tier_for(
                    store, current["model"], current["reasoning"]
                )
                resume_session_id = outcome.session_id
                if not resume_session_id:
                    prompt += "\n\nThe prior invocation was safely interrupted before completion. Inspect existing work and continue."
            if outcome.cancelled:
                result = _synthetic_result(task["id"], "cancelled", "worker was cancelled", "cancelled")
            elif outcome.timed_out:
                result = _synthetic_result(task["id"], "failed", "worker timed out", "timeout")
            elif outcome.returncode != 0:
                detail = outcome.stderr.strip() or "worker produced no structured output"
                result = _synthetic_result(task["id"], "failed", detail, "environment")
            elif not outcome.model_reasoning_verified or not outcome.service_tier_acceptable:
                result = _synthetic_result(
                    task["id"],
                    "failed",
                    "runtime evidence rejected the requested model/reasoning or could not account "
                    "for the explicit service-tier request",
                    "environment",
                )
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
            if external_write_allowed and result["status"] != "success":
                store.record_external_action(
                    action_fingerprint,
                    status="uncertain",
                    session_id=outcome.session_id,
                    detail=result["summary"],
                    reconciliation_required=True,
                )
                verification = self._reconcile_external_action(
                    task_text,
                    plan,
                    task,
                    task_workspace,
                    action_fingerprint,
                    runner,
                    store,
                    phase=f"attempt-{actual_attempt}",
                )
                if verification["verdict"] == "completed":
                    result = _synthetic_result(
                        task["id"], "success", verification["summary"], "none"
                    )
                    result["tests"] = list(verification["evidence"])
                    store.record_external_action(
                        action_fingerprint,
                        status="completed-reconciled",
                        reconciliation=verification,
                        reconciliation_required=False,
                    )
                elif verification["verdict"] == "not_applied" and attempt < attempts:
                    store.record_external_action(
                        action_fingerprint,
                        status="verified-not-applied",
                        reconciliation=verification,
                        reconciliation_required=False,
                    )
                    previous = result
                    continue
                else:
                    result = _synthetic_result(
                        task["id"],
                        "blocked",
                        "external action outcome could not be safely completed or retried after read-only reconciliation",
                        "permission",
                        list(verification["evidence"]) or [verification["summary"]],
                    )
                    store.record_external_action(
                        action_fingerprint,
                        status="uncertain",
                        reconciliation=verification,
                        reconciliation_required=True,
                    )
            previous = result
            if result["status"] == "success":
                stored = self._persist_success_result(
                    store,
                    task["id"],
                    result,
                    session_id=outcome.session_id,
                    speed_fallback=outcome.speed_fallback,
                )
                if external_write_allowed:
                    store.record_external_action(
                        action_fingerprint,
                        status=(
                            "completed-reconciled"
                            if store.read()["external_actions"][action_fingerprint].get("status")
                            == "completed-reconciled"
                            else "completed"
                        ),
                        session_id=outcome.session_id,
                        result=str(store.job_dir / f"result-{task['id']}.json"),
                        reconciliation_required=False,
                    )
                return stored
            if result["failure_kind"] not in {"implementation", "reasoning"} or attempt == attempts:
                break
            current = self._upgrade_task(current)

        canonical = store.job_dir / f"result-{task['id']}.json"
        atomic_write_json(canonical, previous)
        store.set_artifact(f"result-{task['id']}", canonical)
        store.set_task(task["id"], status=previous["status"], result=str(canonical))
        return previous

    def _persist_success_result(
        self,
        store: JobStore,
        task_id: str,
        result: dict[str, Any],
        *,
        session_id: str = "",
        speed_fallback: bool = False,
    ) -> dict[str, Any]:
        canonical = store.job_dir / f"result-{task_id}.json"
        atomic_write_json(canonical, result)
        store.set_artifact(f"result-{task_id}", canonical)
        store.set_task(
            task_id,
            status="success",
            result=str(canonical),
            session_id=session_id,
            speed_fallback=speed_fallback,
        )
        return result

    def _reconcile_external_action(
        self,
        task_text: str,
        plan: dict[str, Any],
        task: dict[str, Any],
        workspace: Path,
        action_fingerprint: str,
        runner: CodexRunner,
        store: JobStore,
        *,
        phase: str,
    ) -> dict[str, Any]:
        model = self.catalog.preferred_sol()
        reasoning = "max"
        service_tier, speed_profile, speed_revision = self._tier_for(
            store, model, reasoning
        )
        output_path = store.job_dir / f"external-reconciliation-{safe_name(task['id'])}-{safe_name(phase)}.json"
        outcome = runner.execute(
            key=f"external-reconcile-{task['id']}-{phase}",
            model=model,
            reasoning=reasoning,
            service_tier=service_tier,
            speed_profile_name=speed_profile,
            speed_policy_revision=speed_revision,
            workspace=workspace,
            prompt=_external_reconciliation_prompt(
                task_text, plan, task, action_fingerprint
            ),
            schema_path=_schema_path("external-verification.schema.json"),
            output_path=output_path,
            timeout_seconds=min(int(task["timeout_seconds"]), 1800),
            read_only=True,
        )
        uncertain = {
            "version": 1,
            "task_id": task["id"],
            "verdict": "uncertain",
            "summary": "read-only external reconciliation did not produce verified evidence",
            "evidence": [],
        }
        if (
            outcome.cancelled
            or outcome.timed_out
            or outcome.returncode != 0
            or not outcome.model_reasoning_verified
            or not outcome.service_tier_acceptable
            or not isinstance(outcome.output, dict)
        ):
            verification = uncertain
        else:
            try:
                verification = _validate_external_verification(outcome.output, task["id"])
            except ValidationError:
                verification = uncertain
        store.record_external_action(
            action_fingerprint,
            reconciliation=verification,
            reconciliation_invocation=str(outcome.evidence_path),
        )
        return verification

    def _upgrade_task(self, task: dict[str, Any]) -> dict[str, Any]:
        upgraded = deepcopy(task)
        order = [
            effort
            for effort in self.catalog.models[upgraded["model"]].efforts
            if effort != "ultra"
        ]
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

    def _immediate_interrupt_reason(
        self,
        store: JobStore,
        task: dict[str, Any],
        *,
        safe_to_interrupt: bool,
    ) -> str:
        queue = ControlQueue(store.control_path)
        for request in queue.pending():
            if request["kind"] == "cancel":
                return f"cancel:{request['request_id']}"
            if request["boundary"] != "immediate":
                continue
            if not safe_to_interrupt:
                continue
            if request["kind"] in {"pause", "steer", "speed-change"}:
                return f"{request['kind']}:{request['request_id']}"
        return ""

    def _process_controls(self, store: JobStore, *, boundary: str) -> None:
        queue = ControlQueue(store.control_path)
        replan = False
        while True:
            pending = queue.pending()
            if not pending:
                break
            request = pending[0]
            kind = request["kind"]
            if kind == "cancel":
                queue.complete(request["request_id"], "applied", f"cancelled at {boundary}")
                store.set_last_control_seq(request["seq"])
                store.request_cancel()
                raise JobCancelled(f"cancellation applied at {boundary}")
            if kind == "pause":
                queue.complete(request["request_id"], "applied", f"paused at {boundary}")
                store.set_last_control_seq(request["seq"])
                self._wait_paused(store, boundary)
                continue
            if kind == "resume":
                queue.complete(request["request_id"], "applied", "job was already running")
                store.set_last_control_seq(request["seq"])
                continue
            if kind == "speed-change":
                self._apply_speed_control(store, request["payload"])
                queue.complete(request["request_id"], "applied", f"speed policy changed at {boundary}")
                store.set_last_control_seq(request["seq"])
                continue
            if kind == "steer":
                instruction = self._read_control_instruction(request["payload"])
                mode = str(request["payload"].get("mode", "add"))
                if mode not in {"add", "replace"}:
                    queue.complete(request["request_id"], "rejected", "steering mode must be add or replace")
                    store.set_last_control_seq(request["seq"])
                    continue
                store.record_steering(
                    {
                        "request_id": request["request_id"],
                        "instruction": instruction,
                        "mode": mode,
                        "status": "applied",
                        "boundary": boundary,
                        "at": utc_now(),
                    }
                )
                queue.complete(request["request_id"], "applied", f"steering applied at {boundary}")
                store.set_last_control_seq(request["seq"])
                replan = replan or mode == "replace"
                continue
        if replan:
            raise ReplanRequested("replacement steering requires a new Sol Max plan")

    def _wait_paused(self, store: JobStore, boundary: str) -> None:
        store.set_desired_status("paused")
        store.transition("paused", f"paused at {boundary}")
        idle_seconds = int(os.environ.get("CODEX_ORCHESTRATOR_PAUSE_IDLE_SECONDS", "1800"))
        deadline = time.monotonic() + idle_seconds
        queue = ControlQueue(store.control_path)
        while time.monotonic() < deadline:
            pending = queue.pending()
            if not pending:
                time.sleep(0.25)
                continue
            request = pending[0]
            if request["kind"] == "cancel":
                queue.complete(request["request_id"], "applied", "cancelled while paused")
                store.set_last_control_seq(request["seq"])
                store.request_cancel()
                raise JobCancelled("job cancelled while paused")
            if request["kind"] == "resume":
                queue.complete(request["request_id"], "applied", "job resumed")
                store.set_last_control_seq(request["seq"])
                store.set_desired_status("running")
                store.transition("running", "resume control applied")
                return
            if request["kind"] == "speed-change":
                self._apply_speed_control(store, request["payload"])
                queue.complete(request["request_id"], "applied", "speed changed while paused")
                store.set_last_control_seq(request["seq"])
                continue
            if request["kind"] == "steer":
                instruction = self._read_control_instruction(request["payload"])
                mode = str(request["payload"].get("mode", "add"))
                store.record_steering(
                    {
                        "request_id": request["request_id"],
                        "instruction": instruction,
                        "mode": mode,
                        "status": "applied",
                        "boundary": "paused",
                        "at": utc_now(),
                    }
                )
                queue.complete(request["request_id"], "applied", "steering recorded while paused")
                store.set_last_control_seq(request["seq"])
                continue
            queue.complete(request["request_id"], "superseded", "pause is already active")
            store.set_last_control_seq(request["seq"])
        store.set_desired_status("paused")
        store.transition("interrupted", "paused controller idle timeout; checkpoint retained")
        raise ControllerInterrupted("paused controller exited after idle timeout")

    def _apply_speed_control(self, store: JobStore, payload: dict[str, Any]) -> None:
        current = store.read_speed_policy()
        matrix_value = payload.get("matrix")
        if not isinstance(matrix_value, dict):
            raise OrchestrationError("speed control requires a complete matrix snapshot")
        matrix = normalize_matrix(matrix_value, self.catalog, require_complete=True)
        revision = int(current.get("revision", 1)) + 1
        updated = {
            **current,
            "profile_name": str(payload.get("profile_name") or current.get("profile_name") or "job-override"),
            "matrix": matrix,
            "source": "runtime-control",
            "revision": revision,
            "created_at": utc_now(),
        }
        store.set_speed_policy(updated)

    def _read_control_instruction(self, payload: dict[str, Any]) -> str:
        path_value = payload.get("instruction_file")
        expected_sha = payload.get("sha256")
        if not isinstance(path_value, str) or not isinstance(expected_sha, str):
            raise OrchestrationError("steering control requires an instruction file and checksum")
        path = Path(path_value)
        text = path.read_text(encoding="utf-8")
        if not text.strip() or sha256_text(text) != expected_sha:
            raise OrchestrationError("steering instruction is empty or its checksum changed")
        return text.strip()

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
        self._process_controls(store, boundary="before-review")
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

        review_config = deepcopy(plan["final_review"])
        if task_worktrees and not review_config["required"]:
            review_config.update(
                {"required": True, "model": self.catalog.preferred_sol(), "reasoning": "max"}
            )
        canonical_review = store.job_dir / "review.json"
        review_read_only = all_read and workspace_manager.read_snapshot is None
        for review_attempt in (1, 2):
            store.transition("reviewing", f"fresh Sol Max reviewer pass {review_attempt}")
            service_tier, speed_profile, speed_revision = self._tier_for(
                store, review_config["model"], review_config["reasoning"]
            )
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
                service_tier=service_tier,
                speed_profile_name=speed_profile,
                speed_policy_revision=speed_revision,
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
            if not outcome.model_reasoning_verified or not outcome.service_tier_acceptable:
                raise OrchestrationError(
                    "final reviewer runtime evidence rejected the requested model/reasoning "
                    "or could not account for the explicit service-tier request"
                )
            review = validate_review(outcome.output, set(planned_tasks))
            atomic_write_json(canonical_review, review)
            store.set_artifact("review", canonical_review)
            if review["approved"] and review["status"] == "success":
                if integration:
                    self._process_controls(store, boundary="before-integration")
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
            f"- Speed profile: `{state.get('speed_profile', 'unknown')}` revision `{state.get('speed_policy_revision', 0)}`",
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
        if state.get("external_actions"):
            lines.extend(["", "## External action reconciliation", ""])
            for fingerprint, record in state["external_actions"].items():
                verdict = record.get("reconciliation", {}).get("verdict", "not-needed")
                lines.append(
                    f"- `{fingerprint[:16]}` task `{record.get('task_id', 'unknown')}`: "
                    f"status `{record.get('status', 'unknown')}`, reconciliation `{verdict}`"
                )
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
                observed_tiers = ", ".join(evidence.get("observed_service_tiers", [])) or "unavailable"
                configured_tiers = ", ".join(evidence.get("configured_service_tiers", [])) or "unavailable"
                verdict = "verified" if evidence.get("model_reasoning_verified") else "unverified"
                request_verdict = (
                    "verified" if evidence.get("service_tier_request_verified") else "unverified"
                )
                observation_status = evidence.get("service_tier_observation_status", "unknown")
                speed_verdict = {
                    "matched": "verified",
                    "degraded": "degraded to Standard",
                    "not_exposed": "not exposed by CLI",
                    "mismatch": "mismatched",
                    "conflicting": "conflicting",
                }.get(observation_status, "unverified")
                fallback = " (Fast degraded to Standard)" if evidence.get("fast_degraded") or evidence.get("speed_fallback_to") else ""
                lines.append(
                    f"- `{evidence['key']}`: requested `{evidence['requested_model']}` + "
                    f"`{evidence['requested_reasoning']}` + `{evidence.get('requested_service_tier', 'unknown')}`; "
                    f"observed `{observed_models}` + `{observed_reasoning}` + `{observed_tiers}` — "
                    f"model/reasoning **{verdict}**, CLI tier override **{request_verdict}**, "
                    f"backend speed **{speed_verdict}**{fallback}; configured tier `{configured_tiers}`"
                )
            fast_count = sum(
                evidence.get("requested_service_tier") == "priority" for evidence in invocation_records
            )
            standard_count = sum(
                evidence.get("requested_service_tier") == "default" for evidence in invocation_records
            )
            observed_fast_count = sum(
                "priority" in evidence.get("observed_service_tiers", [])
                for evidence in invocation_records
            )
            observed_standard_count = sum(
                "default" in evidence.get("observed_service_tiers", [])
                for evidence in invocation_records
            )
            degraded_count = sum(
                bool(evidence.get("fast_degraded") or evidence.get("speed_fallback_to"))
                for evidence in invocation_records
            )
            unobserved_count = sum(
                evidence.get("service_tier_observation_status") == "not_exposed"
                for evidence in invocation_records
            )
            request_verified_count = sum(
                bool(evidence.get("service_tier_request_verified"))
                for evidence in invocation_records
            )
            model_reasoning_matched = all(
                evidence.get("model_reasoning_verified") for evidence in invocation_records
            )
            fully_matched = all(
                evidence.get("all_settings_verified")
                for evidence in invocation_records
            )
            lines.extend(
                [
                    "",
                    f"- Requested Fast calls: `{fast_count}`",
                    f"- Requested Standard calls: `{standard_count}`",
                    f"- Observed Fast calls: `{observed_fast_count}`",
                    f"- Observed Standard calls: `{observed_standard_count}`",
                    f"- Fast degradations: `{degraded_count}`",
                    f"- Backend tier not exposed by CLI: `{unobserved_count}`",
                    f"- Explicit CLI tier overrides verified: `{request_verified_count}/{len(invocation_records)}`",
                    f"- Every invocation matched requested model/reasoning: `{'yes' if model_reasoning_matched else 'no'}`",
                    f"- Every invocation independently verified all requested settings: `{'yes' if fully_matched else 'no'}`",
                ]
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


def _plan_schema_for_catalog(catalog: ModelCatalog) -> dict[str, Any]:
    schema = load_json(_schema_path("plan.schema.json"))
    efforts = sorted(
        {
            effort
            for model in catalog.models.values()
            for effort in model.efforts
        }
    )
    wave = schema["properties"]["waves"]["items"]["properties"]
    wave["tasks"]["items"]["properties"]["reasoning"]["enum"] = efforts
    schema["properties"]["final_review"]["properties"]["reasoning"]["enum"] = [
        effort for effort in efforts if effort != "ultra"
    ]
    return schema


def _planner_prompt(task_text: str, preflight: dict[str, Any], feedback: str) -> str:
    return f"""<codex-orchestrator-worker>
ROLE: planner

Plan the complete task without modifying any file or external system. Choose the execution mode before choosing workers. Return only the JSON object required by the supplied schema.

Original task:
{task_text}

Verified preflight:
{json.dumps(preflight, ensure_ascii=False, indent=2)}

Rules:
- The speed profile and any first-use or per-job selection gate were already resolved before this prompt. Treat wording about opening, waiting for, or choosing orchestrator speed as consumed control-plane metadata; never turn it into a task, dependency, acceptance criterion, or blocker.
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
- The orchestrator already resolved the speed policy before launching this call. Any original-task wording about waiting for or choosing orchestrator speed is already satisfied control-plane context and must not block execution.
- {delegation}
- {commit_rule}
- Respect allowed_paths and all applicable AGENTS.md files.
- Inspect current state before changing it; never discard unknown changes.
- Run the acceptance checks that are actually available and report unrun checks honestly.
- Never expand push, deployment, destructive cleanup, or external-write authority.
- Before any permitted external write, perform a read-only check, use an idempotent action fingerprint where the target supports one, and never repeat an action whose outcome is uncertain.
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
The orchestrator resolved all speed-selection gates before planning. Do not treat original wording about waiting for or choosing orchestrator speed as an unmet product requirement.

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


def _external_reconciliation_prompt(
    task_text: str,
    plan: dict[str, Any],
    task: dict[str, Any],
    action_fingerprint: str,
) -> str:
    return f"""<codex-orchestrator-worker>
ROLE: external action reconciler

The prior external-write-capable invocation ended without a trustworthy final result. Perform only read-only checks against the target system and return the supplied JSON schema.

Original task:
{task_text}

Assigned task:
{json.dumps(task, ensure_ascii=False, indent=2)}

Authorized permissions:
{json.dumps(plan['permissions'], ensure_ascii=False, indent=2)}

Action fingerprint: {action_fingerprint}

Rules:
- Do not create, update, delete, push, deploy, retry, or otherwise write anything.
- Inspect the target's current state and any locally available durable evidence.
- verdict=completed only when evidence proves the intended action and acceptance criteria already hold.
- verdict=not_applied only when evidence proves the action did not occur and retrying it would not duplicate an effect.
- Otherwise use verdict=uncertain.
- Include concise, non-secret evidence references. Do not invoke plugins, subagents, or another Codex session.
"""


def _validate_external_verification(raw: Any, task_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {
        "version",
        "task_id",
        "verdict",
        "summary",
        "evidence",
    }:
        raise ValidationError("invalid external reconciliation fields")
    if raw["version"] != 1 or raw["task_id"] != task_id:
        raise ValidationError("external reconciliation identity mismatch")
    if raw["verdict"] not in {"completed", "not_applied", "uncertain"}:
        raise ValidationError("invalid external reconciliation verdict")
    if not isinstance(raw["summary"], str) or not raw["summary"].strip():
        raise ValidationError("external reconciliation summary is empty")
    if not isinstance(raw["evidence"], list) or not all(
        isinstance(item, str) for item in raw["evidence"]
    ):
        raise ValidationError("external reconciliation evidence must be strings")
    return raw


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
