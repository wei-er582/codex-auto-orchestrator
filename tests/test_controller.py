from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from orchestrator.model_catalog import ModelCatalog
from orchestrator.speed_profiles import ProfileStore, builtin_matrix
from orchestrator.state import JobStore, TERMINAL_STATES
from orchestrator.util import process_identity, terminate_process_tree


class ControllerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.workspace = self.base / "workspace with spaces"
        self.workspace.mkdir()
        self.runs = self.base / "runs"
        self.config = self.base / "config.json"
        self.fake = [sys.executable, str(ROOT / "tests" / "fake_codex.py")]
        catalog = ModelCatalog.discover(self.fake)
        self.catalog = catalog
        ProfileStore(self.config).save_profile(
            "日常开发", builtin_matrix("balanced", catalog), catalog, set_default=True
        )

    def tearDown(self) -> None:
        if self.runs.is_dir():
            for state_path in self.runs.glob("*/state.json"):
                store = JobStore(self.runs, state_path.parent.name)
                try:
                    state = store.read()
                    if state["status"] not in TERMINAL_STATES:
                        store.request_cancel()
                        pid = int(state.get("controller", {}).get("pid", 0) or 0)
                        terminate_process_tree(pid)
                    else:
                        deadline = time.time() + 3
                        while (
                            store.read().get("controller", {}).get("status") != "exited"
                            and time.time() < deadline
                        ):
                            time.sleep(0.05)
                except Exception:
                    pass
        self.temporary.cleanup()

    def test_background_controller_returns_immediately_and_exits_on_completion(self) -> None:
        store = self._start("MODE=direct inspect")
        final = self._wait_for(store, {"complete"})
        self.assertEqual(final["status"], "complete")
        final = self._wait_for_controller_exit(store)
        self.assertEqual(final["controller"]["status"], "exited")
        self.assertTrue(final["heartbeat_at"])

    def test_profiles_list_requires_no_dummy_name(self) -> None:
        completed = self._cli(
            "profiles",
            "list",
            "--config",
            str(self.config),
            "--codex-command",
            subprocess.list2cmdline(self.fake),
            "--json",
        )
        values = json.loads(completed.stdout)
        self.assertTrue(any(item["name"] == "日常开发" for item in values))

    def test_first_start_waits_for_speed_setup_before_planner(self) -> None:
        config = self.base / "first-use-config.json"
        task_file = self.base / "first-use-task.txt"
        task_file.write_text("MODE=direct inspect", encoding="utf-8")
        completed = self._cli(
            "start",
            "--workspace",
            str(self.workspace),
            "--task-file",
            str(task_file),
            "--run-root",
            str(self.runs),
            "--config",
            str(config),
            "--codex-command",
            subprocess.list2cmdline(self.fake),
            "--no-browser",
            "--json",
        )
        payload = json.loads(completed.stdout)
        store = JobStore(self.runs, payload["job_id"])
        self.assertEqual(store.read()["status"], "waiting_for_speed")
        self.assertFalse((store.job_dir / "invocations").exists())
        url = payload["setup_url"]
        page = self._read_url_with_retry(url)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        parsed = urllib.parse.urlparse(url)
        token = urllib.parse.parse_qs(parsed.query)["token"][0]
        form = urllib.parse.urlencode(
            {
                "token": token,
                "csrf": csrf,
                "action": "save",
                "scope": "save-default",
                "profile_name": "首次配置",
                "fast__sol__max": "on",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{parsed.scheme}://{parsed.netloc}/",
            data=form,
            method="POST",
            headers={
                "Origin": f"{parsed.scheme}://{parsed.netloc}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
        final = self._wait_for(store, {"complete"}, timeout=30)
        self.assertEqual(final["speed_profile"], "首次配置")
        self.assertTrue((store.job_dir / "invocations" / "planner-1").is_dir())

    def test_cancel_waiting_speed_setup_stops_page_without_starting_planner(self) -> None:
        config = self.base / "cancel-setup-config.json"
        task_file = self.base / "cancel-setup-task.txt"
        task_file.write_text("MODE=direct inspect", encoding="utf-8")
        completed = self._cli(
            "start",
            "--workspace",
            str(self.workspace),
            "--task-file",
            str(task_file),
            "--run-root",
            str(self.runs),
            "--config",
            str(config),
            "--codex-command",
            subprocess.list2cmdline(self.fake),
            "--no-browser",
            "--json",
        )
        payload = json.loads(completed.stdout)
        store = JobStore(self.runs, payload["job_id"])
        setup_pid = int(store.read()["controller"]["setup_pid"])
        self._cli("cancel", store.job_id, "--run-root", str(self.runs))
        state = self._wait_for(store, {"cancelled"})
        deadline = time.time() + 5
        while process_identity(setup_pid) is not None and time.time() < deadline:
            time.sleep(0.05)
        self.assertIsNone(process_identity(setup_pid))
        self.assertFalse((store.job_dir / "invocations").exists())
        self.assertEqual(state["status"], "cancelled")

    def test_first_setup_can_complete_through_text_fallback(self) -> None:
        config = self.base / "text-setup-config.json"
        task_file = self.base / "text-setup-task.txt"
        task_file.write_text("MODE=direct inspect", encoding="utf-8")
        completed = self._cli(
            "start",
            "--workspace",
            str(self.workspace),
            "--task-file",
            str(task_file),
            "--run-root",
            str(self.runs),
            "--config",
            str(config),
            "--codex-command",
            subprocess.list2cmdline(self.fake),
            "--no-browser",
            "--json",
        )
        payload = json.loads(completed.stdout)
        store = JobStore(self.runs, payload["job_id"])
        matrix_file = self.base / "text-speed.txt"
        matrix_file.write_text(
            "Sol Fast = max, ultra\nTerra Fast = ultra\n", encoding="utf-8"
        )
        self._cli(
            "speed",
            store.job_id,
            "--text-file",
            str(matrix_file),
            "--save-profile",
            "文本配置",
            "--set-default",
            "--run-root",
            str(self.runs),
            "--config",
            str(config),
            "--codex-command",
            subprocess.list2cmdline(self.fake),
        )
        final = self._wait_for(store, {"complete"}, timeout=30)
        self.assertEqual(final["speed_profile"], "文本配置")
        self.assertEqual(ProfileStore(config).read()["default_profile"], "文本配置")

    def test_pause_and_resume_continue_same_job(self) -> None:
        store = self._start("MODE=direct FAKE_SLEEP")
        self._wait_for_process(store)
        self._cli("pause", store.job_id, "--immediate", "--run-root", str(self.runs))
        self._wait_for(store, {"paused"})
        self._cli(
            "resume",
            store.job_id,
            "--run-root",
            str(self.runs),
            "--config",
            str(self.config),
        )
        final = self._wait_for(store, {"complete"}, timeout=30)
        self.assertEqual(final["status"], "complete")
        self.assertEqual(final["tasks"]["direct-main"]["attempt"], 1)
        self.assertTrue(
            list((store.job_dir / "invocations").glob("worker-direct-main-1-resume*"))
        )

    def test_paused_controller_exits_after_idle_timeout_and_keeps_checkpoint(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_ORCHESTRATOR_PAUSE_IDLE_SECONDS": "1"}):
            store = self._start("MODE=direct FAKE_SLEEP")
            self._wait_for_process(store)
            self._cli("pause", store.job_id, "--immediate", "--run-root", str(self.runs))
            self._wait_for(store, {"interrupted"}, timeout=10)
        state = self._wait_for_controller_exit(store)
        self.assertEqual(state["desired_status"], "paused")
        self.assertTrue(state["checkpoint"]["safe"])

    @unittest.skipUnless(os.name == "nt", "Windows crash test validates process-tree identity behavior")
    def test_crashed_controller_resumes_saved_codex_session(self) -> None:
        store = self._start("MODE=direct FAKE_SLEEP")
        state = self._wait_for_process(store, require_session=True)
        controller_pid = int(state["controller"]["pid"])
        terminate_process_tree(controller_pid)
        deadline = time.time() + 5
        while time.time() < deadline:
            if process_identity(controller_pid) is None:
                break
            time.sleep(0.1)
        store.mutate(lambda value: value.__setitem__("heartbeat_at", "2000-01-01T00:00:00+00:00"))
        self._cli(
            "resume",
            store.job_id,
            "--run-root",
            str(self.runs),
            "--config",
            str(self.config),
        )
        final = self._wait_for(store, {"complete"}, timeout=30)
        self.assertEqual(final["status"], "complete")
        evidence_paths = list(
            (store.job_dir / "invocations").glob("worker-direct-main-1-resume*/invocation.json")
        )
        self.assertTrue(evidence_paths)
        evidence = json.loads(evidence_paths[-1].read_text(encoding="utf-8"))
        self.assertTrue(evidence["resume_session_id"])
        temp_root = Path(tempfile.gettempdir()) / "codex-auto-orchestrator" / store.job_id.lower()
        self.assertFalse(temp_root.exists())

    def test_completed_followup_creates_linked_job(self) -> None:
        parent = self._start("MODE=direct inspect")
        self._wait_for(parent, {"complete"})
        followup_file = self.base / "followup.txt"
        followup_file.write_text("MODE=direct inspect followup", encoding="utf-8")
        completed = self._cli(
            "followup",
            parent.job_id,
            "--task-file",
            str(followup_file),
            "--run-root",
            str(self.runs),
            "--config",
            str(self.config),
            "--codex-command",
            subprocess.list2cmdline(self.fake),
        )
        child_id = next(
            line.split("=", 1)[1] for line in completed.stdout.splitlines() if line.startswith("job_id=")
        )
        child = JobStore(self.runs, child_id)
        final = self._wait_for(child, {"complete"})
        self.assertEqual(final["parent_job_id"], parent.job_id)

    def test_second_job_in_same_workspace_is_rejected_while_first_is_active(self) -> None:
        first = self._start("MODE=direct FAKE_SLEEP")
        self._wait_for_process(first)
        task_file = self.base / "conflicting-task.txt"
        task_file.write_text("MODE=direct inspect", encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "orchestrate.py"),
                "start",
                "--workspace",
                str(self.workspace),
                "--task-file",
                str(task_file),
                "--run-root",
                str(self.runs),
                "--config",
                str(self.config),
                "--codex-command",
                subprocess.list2cmdline(self.fake),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("workspace already has a non-terminal", completed.stderr)
        self._cli("cancel", first.job_id, "--run-root", str(self.runs))
        self._wait_for(first, {"cancelled"})

    def test_runtime_speed_change_affects_only_later_calls(self) -> None:
        store = self._start("MODE=two-waves inspect")
        self._wait_for_process(store)
        matrix_file = self.base / "all-fast.json"
        matrix_file.write_text(
            json.dumps(builtin_matrix("all-fast", self.catalog)), encoding="utf-8"
        )
        self._cli(
            "speed",
            store.job_id,
            "--matrix-file",
            str(matrix_file),
            "--run-root",
            str(self.runs),
            "--config",
            str(self.config),
        )
        final = self._wait_for(store, {"complete"}, timeout=30)
        self.assertEqual(final["speed_policy_revision"], 2)
        first = json.loads(
            (store.job_dir / "invocations" / "worker-first-1" / "invocation.json").read_text(
                encoding="utf-8"
            )
        )
        second = json.loads(
            (store.job_dir / "invocations" / "worker-second-1" / "invocation.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(first["requested_service_tier"], "default")
        self.assertEqual(second["requested_service_tier"], "priority")
        self.assertEqual(second["speed_policy_revision"], 2)

    def test_immediate_speed_change_safely_resumes_a_local_worker(self) -> None:
        store = self._start("MODE=direct FAKE_SLEEP")
        self._wait_for_process(store, require_session=True)
        matrix_file = self.base / "immediate-all-fast.json"
        matrix_file.write_text(
            json.dumps(builtin_matrix("all-fast", self.catalog)), encoding="utf-8"
        )
        self._cli(
            "speed",
            store.job_id,
            "--matrix-file",
            str(matrix_file),
            "--immediate",
            "--run-root",
            str(self.runs),
            "--config",
            str(self.config),
        )
        final = self._wait_for(store, {"complete"}, timeout=30)
        evidence_paths = list(
            (store.job_dir / "invocations").glob(
                "worker-direct-main-1-resume*/invocation.json"
            )
        )
        self.assertTrue(evidence_paths)
        evidence = json.loads(evidence_paths[-1].read_text(encoding="utf-8"))
        self.assertEqual(evidence["requested_service_tier"], "priority")
        self.assertEqual(final["tasks"]["direct-main"]["attempt"], 1)

    def test_running_job_speed_snapshot_does_not_drift_with_global_profile(self) -> None:
        store = self._start("MODE=two-waves inspect")
        self._wait_for_process(store)
        ProfileStore(self.config).save_profile(
            "日常开发",
            builtin_matrix("all-fast", self.catalog),
            self.catalog,
            overwrite=True,
        )
        final = self._wait_for(store, {"complete"}, timeout=30)
        self.assertEqual(final["speed_policy_revision"], 1)
        second = json.loads(
            (store.job_dir / "invocations" / "worker-second-1" / "invocation.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(second["requested_service_tier"], "default")

    def test_replacement_steering_replans_at_safe_wave_boundary(self) -> None:
        store = self._start("MODE=two-waves inspect")
        self._wait_for_process(store)
        instruction = self.base / "replace.txt"
        instruction.write_text("MODE=ultra coordinate the replacement objective", encoding="utf-8")
        self._cli(
            "steer",
            store.job_id,
            "--instruction-file",
            str(instruction),
            "--mode",
            "replace",
            "--run-root",
            str(self.runs),
        )
        final = self._wait_for(store, {"complete"}, timeout=30)
        self.assertEqual(final["plan_revision"], 2)
        plan = json.loads((store.job_dir / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["execution_mode"], "native-ultra")
        self.assertTrue(final["steering"])

    def _start(self, task: str) -> JobStore:
        task_file = self.base / f"task-{time.time_ns()}.txt"
        task_file.write_text(task, encoding="utf-8")
        completed = self._cli(
            "start",
            "--workspace",
            str(self.workspace),
            "--task-file",
            str(task_file),
            "--run-root",
            str(self.runs),
            "--config",
            str(self.config),
            "--codex-command",
            subprocess.list2cmdline(self.fake),
            "--json",
        )
        payload = json.loads(completed.stdout)
        return JobStore(self.runs, payload["job_id"])

    def _cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "orchestrate.py"), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=20,
        )

    def _wait_for(
        self, store: JobStore, statuses: set[str], *, timeout: float = 20
    ) -> dict:
        deadline = time.time() + timeout
        state = store.read()
        while state["status"] not in statuses and time.time() < deadline:
            if state["status"] in TERMINAL_STATES and state["status"] not in statuses:
                self.fail(f"job stopped in {state['status']}: {state['blockers']}")
            time.sleep(0.1)
            state = store.read()
        self.assertIn(state["status"], statuses, state)
        return state

    def _wait_for_process(self, store: JobStore, *, require_session: bool = False) -> dict:
        deadline = time.time() + 15
        state = store.read()
        while time.time() < deadline:
            processes = state.get("processes", {})
            worker = next((value for key, value in processes.items() if key.startswith("worker-")), None)
            if worker and (not require_session or worker.get("session_id")):
                return state
            time.sleep(0.1)
            state = store.read()
        self.fail(f"worker process did not start: {state}")

    def _wait_for_controller_exit(self, store: JobStore, *, timeout: float = 5) -> dict:
        deadline = time.time() + timeout
        state = store.read()
        while state.get("controller", {}).get("status") != "exited" and time.time() < deadline:
            time.sleep(0.05)
            state = store.read()
        self.assertEqual(state.get("controller", {}).get("status"), "exited", state)
        return state

    def _read_url_with_retry(self, url: str) -> str:
        deadline = time.time() + 5
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    return response.read().decode("utf-8")
            except Exception as exc:
                last_error = exc
                time.sleep(0.1)
        raise AssertionError(f"speed setup page did not start: {last_error}")


if __name__ == "__main__":
    unittest.main()
