from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from orchestrator.control import ControlQueue
from orchestrator.state import JobStore, find_active_job
from orchestrator.util import atomic_write_json, process_identity, terminate_process_tree


class StateAndControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.workspace = self.base / "workspace with spaces"
        self.workspace.mkdir()
        self.runs = self.base / "runs"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_v2_state_contains_durable_control_fields(self) -> None:
        store = JobStore.create(
            self.runs,
            "job",
            "任务正文",
            self.workspace,
            "balanced",
            origin_thread_id="thread-1",
        )
        state = store.read()
        self.assertEqual(state["version"], 2)
        for field in (
            "origin_thread_id",
            "parent_job_id",
            "desired_status",
            "controller",
            "heartbeat_at",
            "checkpoint",
            "plan_revision",
            "speed_profile",
            "speed_policy_revision",
            "last_control_seq",
            "workspace_resources",
        ):
            self.assertIn(field, state)
        self.assertNotIn("task", state)
        self.assertEqual(store.task_text(), "任务正文")

    def test_control_priority_and_replace_coalescing(self) -> None:
        store = JobStore.create(self.runs, "job", "task", self.workspace, "balanced")
        queue = ControlQueue(store.control_path)
        first = queue.enqueue("steer", payload={"mode": "add"})
        replacement = queue.enqueue("steer", payload={"mode": "replace"})
        queue.enqueue("pause")
        queue.enqueue("cancel")
        pending = queue.pending()
        self.assertEqual(pending[0]["kind"], "cancel")
        self.assertNotIn(first["request_id"], [item["request_id"] for item in pending])
        self.assertIn(replacement["request_id"], [item["request_id"] for item in pending])

    def test_same_workspace_has_only_one_nonterminal_job(self) -> None:
        JobStore.create(
            self.runs,
            "job-a",
            "task",
            self.workspace,
            "balanced",
            origin_thread_id="thread-a",
        )
        self.assertEqual(find_active_job(self.runs, self.workspace), "job-a")
        self.assertEqual(
            find_active_job(self.runs, self.workspace, origin_thread_id="thread-a"), "job-a"
        )

    def test_cancel_does_not_kill_a_pid_when_identity_does_not_match(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            identity = process_identity(process.pid)
            self.assertIsNotNone(identity)
            store = JobStore.create(
                self.runs, "identity-job", "task", self.workspace, "balanced"
            )
            store.mutate(
                lambda state: state["processes"].__setitem__(
                    "forged",
                    {
                        "pid": process.pid,
                        "identity": {**identity, "created": "wrong-birth-marker"},
                        "marker": str(store.job_dir),
                    },
                )
            )
            self.assertEqual(store.request_cancel(), [])
            self.assertIsNone(process.poll())
        finally:
            terminate_process_tree(process.pid)
            process.wait(timeout=10)

    def test_v1_terminal_is_readable_and_nonterminal_is_interrupted(self) -> None:
        terminal = JobStore(self.runs, "terminal")
        terminal.job_dir.mkdir(parents=True)
        atomic_write_json(
            terminal.state_path,
            {
                "job_id": "terminal",
                "status": "complete",
                "task": "old",
                "workspace": str(self.workspace),
                "policy": "balanced",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "cancel_requested": False,
                "processes": {},
                "tasks": {},
                "history": [],
                "artifacts": {},
                "blockers": [],
            },
        )
        self.assertEqual(terminal.read()["status"], "complete")

        active = JobStore(self.runs, "active")
        active.job_dir.mkdir(parents=True)
        value = json.loads(terminal.state_path.read_text(encoding="utf-8"))
        value.update({"job_id": "active", "status": "running"})
        atomic_write_json(active.state_path, value)
        self.assertEqual(active.read()["status"], "interrupted")
        self.assertEqual(active.read()["checkpoint"]["phase"], "legacy-unrecoverable")


if __name__ == "__main__":
    unittest.main()
