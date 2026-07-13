from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .engine import Orchestrator
from .state import JobStore, TERMINAL_STATES
from .util import (
    process_identity,
    same_process_identity,
    terminate_process_tree,
    try_exclusive_file_lock,
    utc_now,
)


HEARTBEAT_SECONDS = 2.0


class JobController:
    def __init__(self, store: JobStore) -> None:
        self.store = store
        self.lease_path = store.job_dir / ".controller.lock"
        self._stop = threading.Event()

    def run(self) -> int:
        with try_exclusive_file_lock(self.lease_path) as acquired:
            if not acquired:
                raise RuntimeError(f"another controller already owns job {self.store.job_id}")
            state = self.store.read()
            if state["status"] in TERMINAL_STATES:
                return 0
            self._reconcile_orphan_processes(state)
            identity = process_identity(os.getpid())
            self.store.set_controller(
                pid=os.getpid(),
                identity=identity,
                status="starting",
                started_at=utc_now(),
            )
            heartbeat = threading.Thread(
                target=self._heartbeat_loop,
                args=(identity,),
                daemon=True,
            )
            heartbeat.start()
            try:
                state = self.store.read()
                if state["status"] == "interrupted":
                    self.store.transition("running", "controller resumed from durable checkpoint")
                config = state.get("controller", {})
                codex_command = config.get("codex_command")
                if not isinstance(codex_command, list) or not all(
                    isinstance(item, str) for item in codex_command
                ):
                    raise RuntimeError("controller state has no valid Codex command")
                orchestrator = Orchestrator(
                    workspace=Path(state["workspace"]),
                    run_root=self.store.run_root,
                    codex_command=codex_command,
                    policy_name=str(state["policy"]),
                )
                result = orchestrator.run_store(
                    self.store,
                    dry_run=bool(config.get("dry_run", False)),
                )
                return 0 if result.read()["status"] == "complete" else 2
            finally:
                self._stop.set()
                heartbeat.join(timeout=HEARTBEAT_SECONDS * 2)
                self.store.set_controller(status="exited", exited_at=utc_now())

    def _heartbeat_loop(self, identity: dict[str, Any] | None) -> None:
        while not self._stop.is_set():
            try:
                self.store.heartbeat(os.getpid(), identity)
            except (OSError, ValueError):
                return
            self._stop.wait(HEARTBEAT_SECONDS)

    def _reconcile_orphan_processes(self, state: dict[str, Any]) -> None:
        marker = str(self.store.job_dir).lower()
        for key, record in list(state.get("processes", {}).items()):
            pid = int(record.get("pid", 0))
            current = process_identity(pid)
            if not same_process_identity(record.get("identity"), current):
                self.store.unregister_process(key)
                continue
            if marker not in str((current or {}).get("command", "")).lower():
                continue
            terminate_process_tree(pid)
            session_id = str(record.get("session_id", ""))
            for task_id, task in state.get("tasks", {}).items():
                if task.get("invocation_key") == key:
                    self.store.set_task(
                        task_id,
                        status="interrupted",
                        session_id=session_id,
                        interrupt_reason="controller recovery terminated an authenticated orphan",
                    )
            self.store.unregister_process(key)


def launch_controller(script_path: Path, store: JobStore) -> int:
    log_path = store.job_dir / "controller.log"
    command = [
        sys.executable,
        str(script_path),
        "_controller",
        store.job_id,
        "--run-root",
        str(store.run_root),
    ]
    creationflags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(Path(store.read()["workspace"])),
            close_fds=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    store.set_controller(
        pid=process.pid,
        identity=process_identity(process.pid),
        status="launched",
        log=str(log_path),
        command=command,
        launched_at=utc_now(),
    )
    return process.pid
