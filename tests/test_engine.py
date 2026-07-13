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

from orchestrator.engine import Orchestrator, _planner_prompt
from orchestrator.speed_profiles import ResolvedSpeedPolicy, builtin_matrix
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

    def test_planner_knows_speed_selection_gate_is_already_satisfied(self) -> None:
        prompt = _planner_prompt(
            "本轮自定义速度；先停在 waiting_for_speed，再读取 README.txt",
            {"policy": {"max_workers": 3}},
            "",
        )
        self.assertIn("already resolved before this prompt", prompt)
        self.assertIn("never turn it into a task", prompt)

    def speed_policy(self, name: str) -> ResolvedSpeedPolicy:
        orchestrator = self.orchestrator()
        return ResolvedSpeedPolicy(
            profile_name=name,
            matrix=builtin_matrix(name, orchestrator.catalog),
            model_bindings={
                family: str(item["model"])
                for family, item in orchestrator.catalog.speed_matrix_catalog().items()
            },
            catalog_fingerprint=orchestrator.catalog.fingerprint(),
            known_combinations=sorted(orchestrator.catalog.speed_combinations()),
            source="test",
        )

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
        self.assertEqual(evidence["requested_service_tier"], "default")
        self.assertEqual(evidence["observed_service_tiers"], ["default"])
        self.assertTrue(evidence["service_tier_verified"])
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
        self.assertEqual(evidence["requested_service_tier"], "priority")
        self.assertTrue(evidence["actual_selection_verified"])

    def test_planner_and_reviewer_read_sol_max_speed_cell(self) -> None:
        store = self.orchestrator().run(
            "MODE=parallel inspect",
            speed_policy=self.speed_policy("all-standard"),
        )
        for key in ("planner-1", "final-review-1"):
            evidence = json.loads(
                (store.job_dir / "invocations" / key / "invocation.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(evidence["requested_model"], "gpt-5.6-sol")
            self.assertEqual(evidence["requested_reasoning"], "max")
            self.assertEqual(evidence["requested_service_tier"], "default")

    def test_terra_worker_uses_fast_when_its_cell_is_enabled(self) -> None:
        store = self.orchestrator().run(
            "MODE=direct inspect",
            speed_policy=self.speed_policy("all-fast"),
        )
        evidence = json.loads(
            (store.job_dir / "invocations" / "worker-direct-main-1" / "invocation.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(evidence["requested_service_tier"], "priority")
        self.assertTrue(evidence["service_tier_verified"])

    def test_hidden_backend_tier_is_reported_without_blocking_verified_request(self) -> None:
        store = self.orchestrator().run(
            "MODE=direct FAKE_HIDE_TIER",
            speed_policy=self.speed_policy("all-fast"),
        )
        self.assertEqual(store.read()["status"], "complete", store.read()["blockers"])
        evidence = json.loads(
            (store.job_dir / "invocations" / "worker-direct-main-1" / "invocation.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(evidence["service_tier_request_verified"])
        self.assertEqual(evidence["service_tier_observation_status"], "not_exposed")
        self.assertTrue(evidence["service_tier_acceptable"])
        self.assertFalse(evidence["service_tier_verified"])
        self.assertFalse(evidence["all_settings_verified"])
        report = (store.job_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("backend speed **not exposed by CLI**", report)

    def test_fast_rejection_falls_back_to_standard_without_model_escalation(self) -> None:
        store = self.orchestrator().run("MODE=direct FAKE_FAST_REJECT")
        self.assertEqual(store.read()["status"], "complete", store.read()["blockers"])
        first = json.loads(
            (store.job_dir / "invocations" / "planner-1" / "invocation.json").read_text(
                encoding="utf-8"
            )
        )
        fallback = json.loads(
            (store.job_dir / "invocations" / "planner-1-speed-fallback" / "invocation.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(first["requested_service_tier"], "priority")
        self.assertEqual(fallback["requested_service_tier"], "default")
        self.assertEqual(first["requested_model"], fallback["requested_model"])
        self.assertEqual(first["requested_reasoning"], fallback["requested_reasoning"])

    def test_observed_fast_degradation_retries_read_only_call_as_standard(self) -> None:
        store = self.orchestrator().run("MODE=direct FAKE_FAST_DEGRADE")
        self.assertEqual(store.read()["status"], "complete", store.read()["blockers"])
        first = json.loads(
            (store.job_dir / "invocations" / "planner-1" / "invocation.json").read_text(
                encoding="utf-8"
            )
        )
        fallback = json.loads(
            (
                store.job_dir
                / "invocations"
                / "planner-1-speed-fallback"
                / "invocation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertTrue(first["fast_degraded"])
        self.assertIn("observed Standard", first["speed_fallback_reason"])
        self.assertEqual(fallback["requested_service_tier"], "default")

    def test_direct_clean_git_write_uses_isolated_worktree(self) -> None:
        self.init_git()
        store = self.orchestrator().run("MODE=write-direct implement direct file")
        self.assertEqual(store.read()["status"], "complete", store.read()["blockers"])
        self.assertTrue((self.workspace / "direct-write.txt").is_file())
        evidence = json.loads(
            (store.job_dir / "invocations" / "worker-direct-write-1" / "invocation.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotEqual(evidence["workspace"], str(self.workspace))

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

    def test_uncertain_external_write_is_not_mechanically_retried(self) -> None:
        store = self.orchestrator().run("MODE=external FAKE_MALFORMED_RESULT external write")
        state = store.read()
        self.assertEqual(state["status"], "blocked")
        invocations = list((store.job_dir / "invocations").glob("worker-external-main-*"))
        self.assertEqual(len(invocations), 1)
        records = list(state["external_actions"].values())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "uncertain")
        self.assertTrue(records[0]["reconciliation_required"])
        self.assertTrue(
            list((store.job_dir / "invocations").glob("external-reconcile-external-main-*"))
        )

    def test_external_action_verified_not_applied_is_retried_once(self) -> None:
        store = self.orchestrator().run(
            "MODE=external FAKE_EXTERNAL_FAIL_ONCE external write"
        )
        state = store.read()
        self.assertEqual(state["status"], "complete", state["blockers"])
        invocations = list((store.job_dir / "invocations").glob("worker-external-main-*"))
        self.assertEqual(len(invocations), 2)
        record = next(iter(state["external_actions"].values()))
        self.assertEqual(record["status"], "completed")
        self.assertFalse(record["reconciliation_required"])

    def test_external_action_verified_completed_is_not_repeated(self) -> None:
        store = self.orchestrator().run(
            "MODE=external FAKE_MALFORMED_RESULT FAKE_EXTERNAL_COMPLETED external write"
        )
        state = store.read()
        self.assertEqual(state["status"], "complete", state["blockers"])
        invocations = list((store.job_dir / "invocations").glob("worker-external-main-*"))
        self.assertEqual(len(invocations), 1)
        record = next(iter(state["external_actions"].values()))
        self.assertEqual(record["status"], "completed-reconciled")

    def test_malformed_plan_is_rejected_after_schema_retry(self) -> None:
        store = self.orchestrator().run("FAKE_MALFORMED_PLAN")
        self.assertEqual(store.read()["status"], "blocked")
        invocations = list((store.job_dir / "invocations").glob("planner-*"))
        self.assertEqual(len(invocations), 2)

    def test_unavailable_model_plan_is_rejected(self) -> None:
        store = self.orchestrator().run("MODE=missing-model")
        self.assertEqual(store.read()["status"], "blocked")
        self.assertIn("model is not available", " ".join(store.read()["blockers"]))

    def test_runtime_model_mismatch_blocks_instead_of_trusting_plan_text(self) -> None:
        store = self.orchestrator().run("MODE=direct FAKE_WRONG_MODEL")
        self.assertEqual(store.read()["status"], "blocked")
        self.assertIn("runtime evidence", " ".join(store.read()["blockers"]))

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
        store = self.orchestrator().run(
            "MODE=direct FAIL_IMPLEMENTATION",
            speed_policy=self.speed_policy("all-fast"),
        )
        self.assertEqual(store.read()["status"], "complete", store.read()["blockers"])
        invocations = sorted((store.job_dir / "invocations").glob("worker-direct-main-*"))
        self.assertEqual(len(invocations), 2)
        second = json.loads((invocations[1] / "invocation.json").read_text(encoding="utf-8"))
        self.assertEqual(second["requested_reasoning"], "high")
        self.assertEqual(second["requested_service_tier"], "priority")

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
