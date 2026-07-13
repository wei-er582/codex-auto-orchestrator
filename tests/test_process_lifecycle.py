from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from orchestrator.process_runner import CodexRunner
from orchestrator.state import JobStore


class ProcessLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.store = JobStore.create(self.base / "runs", "job", "test", self.workspace, "balanced")
        self.runner = CodexRunner([sys.executable, str(ROOT / "tests" / "fake_codex.py")], self.store)
        self.schema = ROOT / "scripts" / "schemas" / "result.schema.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_timeout_terminates_process_and_unregisters_pid(self) -> None:
        outcome = self.runner.execute(
            key="timeout",
            model="gpt-5.6-terra",
            reasoning="medium",
            workspace=self.workspace,
            prompt="<codex-orchestrator-worker>\nROLE: executor\nFAKE_SLEEP",
            schema_path=self.schema,
            output_path=self.store.job_dir / "timeout.json",
            timeout_seconds=1,
            read_only=True,
        )
        self.assertTrue(outcome.timed_out)
        self.assertEqual(self.store.read()["processes"], {})

    def test_cancel_terminates_running_process(self) -> None:
        result = {}

        def run() -> None:
            result["outcome"] = self.runner.execute(
                key="cancel",
                model="gpt-5.6-terra",
                reasoning="medium",
                workspace=self.workspace,
                prompt="<codex-orchestrator-worker>\nROLE: executor\nFAKE_SLEEP",
                schema_path=self.schema,
                output_path=self.store.job_dir / "cancel.json",
                timeout_seconds=60,
                read_only=True,
            )

        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and not self.store.read()["processes"]:
            time.sleep(0.05)
        self.assertTrue(self.store.read()["processes"])
        self.store.request_cancel()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result["outcome"].cancelled)
        self.assertEqual(self.store.read()["processes"], {})
