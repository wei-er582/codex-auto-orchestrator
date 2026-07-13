from __future__ import annotations

import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .util import (
    atomic_write_json,
    atomic_write_text,
    exclusive_file_lock,
    load_json,
    normalize_workspace,
    process_identity,
    same_process_identity,
    sha256_text,
    terminate_process_tree,
    utc_now,
)


STATE_VERSION = 2
TERMINAL_STATES = {"complete", "blocked", "cancelled"}
PAUSED_STATES = {"waiting_for_speed", "paused", "interrupted"}
ACTIVE_STATES = {
    "planning",
    "running",
    "validating",
    "reviewing",
    "repairing",
    "integrating",
    *PAUSED_STATES,
}


class JobStore:
    def __init__(self, run_root: Path, job_id: str) -> None:
        self.run_root = run_root.expanduser().resolve()
        self.job_id = job_id
        self.job_dir = self.run_root / job_id
        self.state_path = self.job_dir / "state.json"
        self.task_path = self.job_dir / "task.txt"
        self.control_path = self.job_dir / "control.json"
        self.speed_policy_path = self.job_dir / "speed-policy.json"
        self.lock_path = self.job_dir / ".state.lock"
        self._lock = threading.RLock()

    @classmethod
    def create(
        cls,
        run_root: Path,
        job_id: str,
        task: str,
        workspace: Path,
        policy: str,
        *,
        origin_thread_id: str = "",
        parent_job_id: str = "",
        initial_status: str = "planning",
        controller_config: dict[str, Any] | None = None,
        entry_context: dict[str, Any] | None = None,
    ) -> "JobStore":
        if initial_status not in ACTIVE_STATES:
            raise ValueError(f"invalid initial status: {initial_status}")
        store = cls(run_root, job_id)
        store.job_dir.mkdir(parents=True, exist_ok=False)
        atomic_write_text(store.task_path, task)
        now = utc_now()
        state = {
            "version": STATE_VERSION,
            "job_id": job_id,
            "status": initial_status,
            "desired_status": "running",
            "task_path": str(store.task_path),
            "task_sha256": sha256_text(task),
            "workspace": str(workspace.expanduser().resolve()),
            "workspace_key": normalize_workspace(workspace),
            "policy": policy,
            "origin_thread_id": origin_thread_id,
            "parent_job_id": parent_job_id,
            "created_at": now,
            "updated_at": now,
            "heartbeat_at": "",
            "cancel_requested": False,
            "controller": controller_config or {},
            "checkpoint": {"phase": "created", "wave_index": 0, "safe": True},
            "plan_revision": 0,
            "speed_profile": "",
            "speed_policy_revision": 0,
            "last_control_seq": 0,
            "workspace_resources": {"worktrees": [], "read_snapshot": None},
            "processes": {},
            "tasks": {},
            "history": [{"status": initial_status, "at": now, "detail": "job created"}],
            "artifacts": {"task": str(store.task_path)},
            "blockers": [],
            "steering": [],
            "external_actions": {},
            "entry_context": entry_context or {},
        }
        atomic_write_json(store.state_path, state)
        atomic_write_json(store.control_path, {"version": 1, "next_seq": 1, "requests": []})
        return store

    def read(self) -> dict[str, Any]:
        with self._lock:
            with exclusive_file_lock(self.lock_path):
                return _normalize_state(load_json(self.state_path), self)

    def mutate(self, callback: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock:
            with exclusive_file_lock(self.lock_path):
                state = _normalize_state(load_json(self.state_path), self)
                callback(state)
                state["version"] = STATE_VERSION
                state["updated_at"] = utc_now()
                atomic_write_json(self.state_path, state)
                return state

    def task_text(self) -> str:
        state = self.read()
        path = Path(state["task_path"])
        text = path.read_text(encoding="utf-8")
        if sha256_text(text) != state["task_sha256"]:
            raise RuntimeError("task file checksum does not match state")
        return text

    def transition(self, status: str, detail: str = "") -> None:
        if status not in ACTIVE_STATES | TERMINAL_STATES:
            raise ValueError(f"invalid job status: {status}")

        def update(state: dict[str, Any]) -> None:
            if state["status"] in TERMINAL_STATES and state["status"] != status:
                raise RuntimeError(f"cannot transition terminal job from {state['status']} to {status}")
            state["status"] = status
            state["history"].append({"status": status, "at": utc_now(), "detail": detail})

        self.mutate(update)

    def set_desired_status(self, status: str) -> None:
        self.mutate(lambda state: state.__setitem__("desired_status", status))

    def set_checkpoint(self, **values: Any) -> None:
        def update(state: dict[str, Any]) -> None:
            state["checkpoint"].update(values)
            state["checkpoint"]["at"] = utc_now()

        self.mutate(update)

    def set_artifact(self, name: str, path: Path) -> None:
        self.mutate(lambda state: state["artifacts"].__setitem__(name, str(path)))

    def set_task(self, task_id: str, **values: Any) -> None:
        def update(state: dict[str, Any]) -> None:
            current = state["tasks"].setdefault(task_id, {})
            current.update(values)
            current["updated_at"] = utc_now()

        self.mutate(update)

    def register_process(
        self,
        key: str,
        pid: int,
        model: str,
        reasoning: str,
        service_tier: str,
        marker: str,
    ) -> None:
        identity = process_identity(pid)
        deadline = time.monotonic() + 1.0
        while identity is None and time.monotonic() < deadline:
            time.sleep(0.05)
            identity = process_identity(pid)

        def update(state: dict[str, Any]) -> None:
            state["processes"][key] = {
                "pid": pid,
                "model": model,
                "reasoning": reasoning,
                "service_tier": service_tier,
                "marker": marker,
                "identity": identity,
                "started_at": utc_now(),
                "session_id": "",
            }

        self.mutate(update)

    def set_process_session(self, key: str, session_id: str) -> None:
        def update(state: dict[str, Any]) -> None:
            if key in state["processes"]:
                state["processes"][key]["session_id"] = session_id
            for task in state["tasks"].values():
                if task.get("invocation_key") == key:
                    task["session_id"] = session_id

        self.mutate(update)

    def unregister_process(self, key: str) -> None:
        self.mutate(lambda state: state["processes"].pop(key, None))

    def set_controller(self, **values: Any) -> None:
        def update(state: dict[str, Any]) -> None:
            state["controller"].update(values)

        self.mutate(update)

    def heartbeat(self, pid: int, identity: dict[str, Any] | None) -> None:
        def update(state: dict[str, Any]) -> None:
            state["heartbeat_at"] = utc_now()
            state["controller"].update(
                {"pid": pid, "identity": identity, "status": "running", "heartbeat_at": state["heartbeat_at"]}
            )

        self.mutate(update)

    def set_speed_policy(self, policy: dict[str, Any]) -> None:
        atomic_write_json(self.speed_policy_path, policy)

        def update(state: dict[str, Any]) -> None:
            state["speed_profile"] = policy["profile_name"]
            state["speed_policy_revision"] = int(policy.get("revision", 1))
            state["artifacts"]["speed_policy"] = str(self.speed_policy_path)

        self.mutate(update)

    def read_speed_policy(self) -> dict[str, Any]:
        if not self.speed_policy_path.is_file():
            raise FileNotFoundError("job speed-policy.json is missing")
        return load_json(self.speed_policy_path)

    def set_workspace_resources(self, resources: dict[str, Any]) -> None:
        self.mutate(lambda state: state.__setitem__("workspace_resources", resources))

    def record_steering(self, value: dict[str, Any]) -> None:
        self.mutate(lambda state: state["steering"].append(value))

    def set_last_control_seq(self, sequence: int) -> None:
        self.mutate(lambda state: state.__setitem__("last_control_seq", int(sequence)))

    def add_blocker(self, message: str) -> None:
        self.mutate(lambda state: state["blockers"].append(message))

    def cancelled(self) -> bool:
        return bool(self.read()["cancel_requested"])

    def request_cancel(self) -> list[int]:
        def update(state: dict[str, Any]) -> None:
            state["cancel_requested"] = True
            state["desired_status"] = "cancelled"
            state["history"].append(
                {"status": state["status"], "at": utc_now(), "detail": "cancellation requested"}
            )

        state = self.mutate(update)
        killed: list[int] = []
        marker = str(self.job_dir)
        for process in state["processes"].values():
            pid = int(process.get("pid", 0))
            current = process_identity(pid)
            expected = process.get("identity")
            command = str((current or {}).get("command", ""))
            if same_process_identity(expected, current) and marker.lower() in command.lower():
                terminate_process_tree(pid)
                killed.append(pid)
        return killed

    def record_external_action(self, fingerprint: str, **values: Any) -> None:
        def update(state: dict[str, Any]) -> None:
            record = state["external_actions"].setdefault(fingerprint, {})
            record.update(values)
            record["updated_at"] = utc_now()

        self.mutate(update)


def find_latest_job(run_root: Path) -> str:
    jobs = [
        path
        for path in run_root.expanduser().resolve().iterdir()
        if path.is_dir() and (path / "state.json").is_file()
    ]
    if not jobs:
        raise FileNotFoundError("no orchestrator jobs found")
    return max(jobs, key=lambda path: path.stat().st_mtime).name


def find_active_job(
    run_root: Path,
    workspace: Path,
    *,
    origin_thread_id: str = "",
) -> str | None:
    root = run_root.expanduser().resolve()
    if not root.is_dir():
        return None
    workspace_key = normalize_workspace(workspace)
    candidates: list[tuple[str, str]] = []
    for path in root.iterdir():
        state_path = path / "state.json"
        if not state_path.is_file():
            continue
        try:
            state = load_json(state_path)
        except (OSError, ValueError):
            continue
        if state.get("status") in TERMINAL_STATES:
            continue
        state_key = state.get("workspace_key") or normalize_workspace(Path(state.get("workspace", ".")))
        if state_key != workspace_key:
            continue
        if origin_thread_id and state.get("origin_thread_id") == origin_thread_id:
            return path.name
        candidates.append((str(state.get("updated_at", "")), path.name))
    return max(candidates)[1] if candidates else None


def heartbeat_stale(state: dict[str, Any], stale_seconds: int = 15) -> bool:
    heartbeat = state.get("heartbeat_at")
    if not heartbeat:
        return True
    try:
        value = datetime.fromisoformat(str(heartbeat))
    except ValueError:
        return True
    return datetime.now(timezone.utc) - value > timedelta(seconds=stale_seconds)


def prune_runs(run_root: Path, max_age_days: int = 14, max_jobs: int = 20) -> list[str]:
    if not run_root.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    jobs = sorted(
        [path for path in run_root.iterdir() if path.is_dir() and (path / "state.json").is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed: list[str] = []
    for index, path in enumerate(jobs):
        state = load_json(path / "state.json")
        try:
            updated = datetime.fromisoformat(state["updated_at"])
        except (KeyError, ValueError):
            continue
        if state.get("status") not in TERMINAL_STATES:
            continue
        if index >= max_jobs or updated < cutoff:
            shutil.rmtree(path)
            removed.append(path.name)
    return removed


def _normalize_state(state: dict[str, Any], store: JobStore) -> dict[str, Any]:
    if int(state.get("version", 1)) >= STATE_VERSION:
        return state
    # v0.1 terminal runs remain reportable. Non-terminal v1 runs have no durable
    # checkpoint, so they are marked interrupted instead of falsely resumed.
    task = str(state.pop("task", ""))
    if task and not store.task_path.is_file():
        atomic_write_text(store.task_path, task)
    state.update(
        {
            "version": STATE_VERSION,
            "desired_status": state.get("status", "interrupted"),
            "task_path": str(store.task_path),
            "task_sha256": sha256_text(task),
            "workspace_key": normalize_workspace(Path(state.get("workspace", "."))),
            "origin_thread_id": "",
            "parent_job_id": "",
            "heartbeat_at": "",
            "controller": {},
            "checkpoint": {"phase": "legacy-terminal" if state.get("status") in TERMINAL_STATES else "legacy-unrecoverable", "wave_index": 0, "safe": True},
            "plan_revision": 1 if (store.job_dir / "plan.json").is_file() else 0,
            "speed_profile": "legacy-default",
            "speed_policy_revision": 0,
            "last_control_seq": 0,
            "workspace_resources": {"worktrees": [], "read_snapshot": None},
            "steering": [],
            "external_actions": {},
            "entry_context": {},
        }
    )
    if state.get("status") not in TERMINAL_STATES:
        state["status"] = "interrupted"
        state["desired_status"] = "paused"
    return state
