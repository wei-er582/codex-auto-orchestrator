from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from orchestrator.engine import Orchestrator
from orchestrator.state import JobStore, find_latest_job


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.run_root = self.base / "runs"
        self.fake = [sys.executable, str(ROOT / "tests" / "fake_codex.py")]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def orchestrator(self) -> Orchestrator:
        return Orchestrator(
            workspace=self.workspace,
            run_root=self.run_root,
            codex_command=self.fake,
            policy_name="balanced",
        )

    def init_git(self) -> None:
        subprocess.run(["git", "init", "-b", "main", str(self.workspace)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.workspace), "config", "user.name", "Test User"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "config", "user.email", "test@example.com"], check=True)
        seed = self.workspace / "seed.txt"
        seed.write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "seed.txt"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-m", "test: seed"], check=True, stdout=subprocess.DEVNULL)

    def test_direct_flow(self) -> None:
        store = self.orchestrator().run("MODE=direct inspect")
        self.assertEqual(store.read()["status"], "complete")
        self.assertFalse(_job_temp_root(store).exists())
        plan = json.loads((store.job_dir / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["execution_mode"], "direct")
        if os.name == "nt":
            evidence = json.loads(
                (store.job_dir / "invocations" / "worker-direct-main-1" / "invocation.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn("-s", evidence["command"])
            self.assertNotEqual(evidence["workspace"], str(self.workspace))

    def test_parallel_flow_records_model_evidence(self) -> None:
        store = self.orchestrator().run("MODE=parallel inspect")
        self.assertEqual(store.read()["status"], "complete")
        evidence = json.loads(
            (store.job_dir / "invocations" / "worker-inspect-a-1" / "invocation.json").read_text(encoding="utf-8")
        )
        self.assertEqual(evidence["requested_model"], "gpt-5.6-terra")
        self.assertEqual(evidence["observed_models"], ["gpt-5.6-terra"])
        self.assertTrue(evidence["actual_selection_verified"])
        report = (store.job_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("**verified**", report)

    def test_native_ultra_is_real_invocation(self) -> None:
        store = self.orchestrator().run("MODE=ultra coordinate")
        self.assertEqual(store.read()["status"], "complete")
        evidence = json.loads(
            (store.job_dir / "invocations" / "worker-ultra-main-1" / "invocation.json").read_text(encoding="utf-8")
        )
        self.assertEqual(evidence["requested_reasoning"], "ultra")
        self.assertEqual(evidence["observed_reasoning"], ["ultra"])
        self.assertTrue(evidence["actual_selection_verified"])

    def test_parallel_writes_merge_and_cleanup(self) -> None:
        self.init_git()
        store = self.orchestrator().run("MODE=write-parallel implement files")
        self.assertEqual(store.read()["status"], "complete", store.read()["blockers"])
        self.assertTrue((self.workspace / "write-a.txt").is_file())
        self.assertTrue((self.workspace / "write-b.txt").is_file())
        worktree_lines = subprocess.check_output(
            ["git", "-C", str(self.workspace), "worktree", "list", "--porcelain"], text=True
        )
        self.assertEqual(worktree_lines.count("worktree "), 1)
        self.assertFalse(_job_temp_root(store).exists())
        status = subprocess.check_output(["git", "-C", str(self.workspace), "status", "--porcelain"], text=True)
        self.assertEqual(status, "")

    def test_review_repairs_only_the_rejected_task(self) -> None:
        self.init_git()
        store = self.orchestrator().run("MODE=write-parallel MODE=review-repair implement files")
        self.assertEqual(store.read()["status"], "complete", store.read()["blockers"])
        self.assertTrue((self.workspace / "write-a-repair.txt").is_file())
        invocations = store.job_dir / "invocations"
        self.assertEqual(len(list(invocations.glob("worker-write-a-*"))), 2)
        self.assertEqual(len(list(invocations.glob("worker-write-b-*"))), 1)
        review = json.loads((store.job_dir / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(review["merge_decision"], "approve")
        history = [entry["status"] for entry in store.read()["history"]]
        self.assertIn("repairing", history)

    def test_dirty_workspace_serializes_writes_and_preserves_user_file(self) -> None:
        self.init_git()
        user_file = self.workspace / "user-untracked.txt"
        user_file.write_text("keep me\n", encoding="utf-8")
        store = self.orchestrator().run("MODE=write-parallel implement files")
        self.assertEqual(store.read()["status"], "complete", store.read()["blockers"])
        self.assertEqual(user_file.read_text(encoding="utf-8"), "keep me\n")
        plan = json.loads((store.job_dir / "plan.json").read_text(encoding="utf-8"))
        self.assertTrue(all(len(wave["tasks"]) == 1 for wave in plan["waves"]))

    def test_environment_failure_is_not_retried(self) -> None:
        store = self.orchestrator().run("MODE=direct FAIL_ENVIRONMENT")
        self.assertEqual(store.read()["status"], "blocked")
        invocations = list((store.job_dir / "invocations").glob("worker-direct-main-*"))
        self.assertEqual(len(invocations), 1)

    def test_malformed_worker_output_is_retried_once(self) -> None:
        store = self.orchestrator().run("MODE=direct FAKE_MALFORMED_RESULT")
        self.assertEqual(store.read()["status"], "blocked")
        invocations = list((store.job_dir / "invocations").glob("worker-direct-main-*"))
        self.assertEqual(len(invocations), 2)

    def test_malformed_plan_is_rejected_after_schema_retry(self) -> None:
        store = self.orchestrator().run("FAKE_MALFORMED_PLAN")
        self.assertEqual(store.read()["status"], "blocked")
        invocations = list((store.job_dir / "invocations").glob("planner-*"))
        self.assertEqual(len(invocations), 2)

    def test_unavailable_model_plan_is_rejected(self) -> None:
        store = self.orchestrator().run("MODE=missing-model")
        self.assertEqual(store.read()["status"], "blocked")
        self.assertIn("model is not available", " ".join(store.read()["blockers"]))

    def test_partial_parallel_completion_is_preserved(self) -> None:
        store = self.orchestrator().run("MODE=parallel FAKE_PARTIAL")
        self.assertEqual(store.read()["status"], "blocked")
        self.assertEqual(store.read()["tasks"]["inspect-a"]["status"], "success")
        self.assertEqual(store.read()["tasks"]["inspect-b"]["status"], "failed")

    def test_windows_command_keeps_spaced_workspace_as_one_argument(self) -> None:
        spaced = self.base / "workspace with spaces"
        spaced.mkdir()
        orchestrator = Orchestrator(
            workspace=spaced,
            run_root=self.run_root,
            codex_command=self.fake,
            policy_name="balanced",
        )
        store = orchestrator.run("MODE=direct inspect")
        evidence = json.loads(
            (store.job_dir / "invocations" / "planner-1" / "invocation.json").read_text(
                encoding="utf-8"
            )
        )
        command = evidence["command"]
        self.assertEqual(command[command.index("-C") + 1], str(spaced))

    def test_cancelled_parallel_write_removes_clean_worktrees_and_branches(self) -> None:
        self.init_git()
        holder = {}

        def run() -> None:
            holder["store"] = self.orchestrator().run("MODE=write-parallel FAKE_SLEEP")

        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.time() + 10
        store = None
        while time.time() < deadline:
            try:
                job_id = find_latest_job(self.run_root)
                candidate = JobStore(self.run_root, job_id)
                process_keys = candidate.read()["processes"]
                if any(key.startswith("worker-") for key in process_keys):
                    store = candidate
                    break
            except (FileNotFoundError, OSError):
                pass
            time.sleep(0.05)
        self.assertIsNotNone(store)
        store.request_cancel()
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(holder["store"].read()["status"], "cancelled")
        worktrees = subprocess.check_output(
            ["git", "-C", str(self.workspace), "worktree", "list", "--porcelain"],
            text=True,
        )
        self.assertEqual(worktrees.count("worktree "), 1)
        branches = subprocess.check_output(
            ["git", "-C", str(self.workspace), "branch", "--list", "codex-orch/*"],
            text=True,
        )
        self.assertEqual(branches.strip(), "")
        self.assertFalse(_job_temp_root(holder["store"]).exists())

    def test_implementation_failure_retries_at_higher_reasoning(self) -> None:
        store = self.orchestrator().run("MODE=direct FAIL_IMPLEMENTATION")
        self.assertEqual(store.read()["status"], "complete", store.read()["blockers"])
        invocations = sorted((store.job_dir / "invocations").glob("worker-direct-main-*"))
        self.assertEqual(len(invocations), 2)
        second = json.loads((invocations[1] / "invocation.json").read_text(encoding="utf-8"))
        self.assertEqual(second["requested_reasoning"], "high")

    @unittest.skipUnless(os.name == "nt", "Windows uses snapshot isolation for read workers")
    def test_read_worker_write_violation_never_touches_original_workspace(self) -> None:
        store = self.orchestrator().run("MODE=direct READ_WRITE_VIOLATION")
        self.assertEqual(store.read()["status"], "blocked")
        self.assertFalse((self.workspace / "unauthorized.txt").exists())
        snapshot_root = Path(tempfile.gettempdir()) / "codex-auto-orchestrator" / store.job_id
        self.assertTrue((snapshot_root / "read-snapshot" / "unauthorized.txt").is_file())
        self.assertIn("isolated workspaces contain changes", " ".join(store.read()["blockers"]))
        shutil.rmtree(snapshot_root)
        try:
            snapshot_root.parent.rmdir()
        except OSError:
            pass


def _job_temp_root(store: JobStore) -> Path:
    return Path(tempfile.gettempdir()) / "codex-auto-orchestrator" / store.job_id


if __name__ == "__main__":
    unittest.main()
