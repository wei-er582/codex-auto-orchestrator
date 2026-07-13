from __future__ import annotations

import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .util import atomic_write_json, exclusive_file_lock, load_json, terminate_process_tree, utc_now


TERMINAL_STATES = {"complete", "blocked", "cancelled"}


class JobStore:
    def __init__(self, run_root: Path, job_id: str) -> None:
        self.run_root = run_root.resolve()
        self.job_id = job_id
        self.job_dir = self.run_root / job_id
        self.state_path = self.job_dir / "state.json"
        self.lock_path = self.job_dir / ".state.lock"
        self._lock = threading.RLock()

    @classmethod
    def create(cls, run_root: Path, job_id: str, task: str, workspace: Path, policy: str) -> "JobStore":
        store = cls(run_root, job_id)
        store.job_dir.mkdir(parents=True, exist_ok=False)
        atomic_write_json(
            store.state_path,
            {
                "job_id": job_id,
                "status": "planning",
                "task": task,
                "workspace": str(workspace.resolve()),
                "policy": policy,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "cancel_requested": False,
                "processes": {},
                "tasks": {},
                "history": [{"status": "planning", "at": utc_now(), "detail": "job created"}],
                "artifacts": {},
                "blockers": [],
            },
        )
        return store

    def read(self) -> dict[str, Any]:
        with self._lock:
            with exclusive_file_lock(self.lock_path):
                return load_json(self.state_path)

    def mutate(self, callback: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock:
            with exclusive_file_lock(self.lock_path):
                state = load_json(self.state_path)
                callback(state)
                state["updated_at"] = utc_now()
                atomic_write_json(self.state_path, state)
                return state

    def transition(self, status: str, detail: str = "") -> None:
        def update(state: dict[str, Any]) -> None:
            if state["status"] in TERMINAL_STATES and state["status"] != status:
                raise RuntimeError(f"cannot transition terminal job from {state['status']} to {status}")
            state["status"] = status
            state["history"].append({"status": status, "at": utc_now(), "detail": detail})

        self.mutate(update)

    def set_artifact(self, name: str, path: Path) -> None:
        self.mutate(lambda state: state["artifacts"].__setitem__(name, str(path)))

    def set_task(self, task_id: str, **values: Any) -> None:
        def update(state: dict[str, Any]) -> None:
            current = state["tasks"].setdefault(task_id, {})
            current.update(values)
            current["updated_at"] = utc_now()

        self.mutate(update)

    def register_process(self, key: str, pid: int, model: str, reasoning: str) -> None:
        def update(state: dict[str, Any]) -> None:
            state["processes"][key] = {
                "pid": pid,
                "model": model,
                "reasoning": reasoning,
                "started_at": utc_now(),
            }

        self.mutate(update)

    def unregister_process(self, key: str) -> None:
        self.mutate(lambda state: state["processes"].pop(key, None))

    def add_blocker(self, message: str) -> None:
        self.mutate(lambda state: state["blockers"].append(message))

    def cancelled(self) -> bool:
        return bool(self.read()["cancel_requested"])

    def request_cancel(self) -> list[int]:
        def update(state: dict[str, Any]) -> None:
            state["cancel_requested"] = True
            state["history"].append({"status": state["status"], "at": utc_now(), "detail": "cancellation requested"})

        state = self.mutate(update)
        pids = [int(process["pid"]) for process in state["processes"].values()]
        for pid in pids:
            terminate_process_tree(pid)
        return pids


def find_latest_job(run_root: Path) -> str:
    jobs = [path for path in run_root.iterdir() if path.is_dir() and (path / "state.json").is_file()]
    if not jobs:
        raise FileNotFoundError("no orchestrator jobs found")
    return max(jobs, key=lambda path: path.stat().st_mtime).name


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
        updated = datetime.fromisoformat(state["updated_at"])
        if state["status"] not in TERMINAL_STATES:
            continue
        if index >= max_jobs or updated < cutoff:
            shutil.rmtree(path)
            removed.append(path.name)
    return removed
