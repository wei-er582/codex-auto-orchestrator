from __future__ import annotations

import http.client
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from orchestrator.model_catalog import ModelCatalog
from orchestrator.speed_profiles import ProfileStore, builtin_matrix
from orchestrator.speed_ui import MAX_REQUEST_BYTES, SpeedSetupServer
from orchestrator.state import JobStore


class SpeedUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.profiles = ProfileStore(self.base / "config.json")
        self.catalog = ModelCatalog.discover(
            [sys.executable, str(ROOT / "tests" / "fake_codex.py")]
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_first_setup_saves_named_default_without_task_content(self) -> None:
        server, thread = self._start(reason="first_setup")
        status, page = self._request(server, "GET", f"/?token={server.token}")
        self.assertEqual(status, 200)
        self.assertIn("Sol", page)
        self.assertIn("Terra", page)
        self.assertNotIn("secret task body", page)
        form = urllib.parse.urlencode(
            {
                "token": server.token,
                "csrf": server.csrf,
                "action": "save",
                "scope": "save-default",
                "profile_name": "日常开发",
                "fast__sol__max": "on",
            }
        )
        status, _ = self._request(
            server,
            "POST",
            "/",
            body=form,
            headers={"Origin": f"http://127.0.0.1:{server.port}"},
        )
        self.assertEqual(status, 200)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.profiles.read()["default_profile"], "日常开发")

    def test_rejects_bad_host_origin_csrf_and_oversized_request(self) -> None:
        server, thread = self._start(timeout=2)
        status, _ = self._request(server, "GET", f"/?token={server.token}", headers={"Host": "evil.invalid"})
        self.assertEqual(status, 403)
        form = urllib.parse.urlencode(
            {
                "token": server.token,
                "csrf": "wrong",
                "action": "save",
                "scope": "save-default",
                "profile_name": "x",
            }
        )
        status, _ = self._request(
            server,
            "POST",
            "/",
            body=form,
            headers={"Origin": f"http://127.0.0.1:{server.port}"},
        )
        self.assertEqual(status, 403)
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2)
        connection.putrequest("POST", "/")
        connection.putheader("Origin", f"http://127.0.0.1:{server.port}")
        connection.putheader("Content-Length", str(MAX_REQUEST_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        response.read()
        connection.close()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(server.result.status, "timeout")

    def test_job_customization_can_use_an_existing_profile_without_editing(self) -> None:
        self.profiles.save_profile(
            "日常开发", builtin_matrix("all-standard", self.catalog), self.catalog, set_default=True
        )
        job = JobStore.create(
            self.base / "runs",
            "job-use-profile",
            "task text",
            self.base,
            "balanced",
            initial_status="waiting_for_speed",
        )
        server, thread = self._start(
            reason="job_customization",
            selected_profile="日常开发",
            job_store=job,
        )
        form = urllib.parse.urlencode(
            {
                "token": server.token,
                "csrf": server.csrf,
                "action": "use-profile",
                "selected_profile": "all-fast",
            }
        )
        status, _ = self._request(
            server,
            "POST",
            "/",
            body=form,
            headers={"Origin": f"http://127.0.0.1:{server.port}"},
        )
        self.assertEqual(status, 200)
        thread.join(timeout=3)
        self.assertEqual(job.read_speed_policy()["profile_name"], "all-fast")
        self.assertEqual(job.read_speed_policy()["matrix"]["terra"]["medium"], "priority")

    def test_unsupported_fast_cells_are_disabled(self) -> None:
        payload = {
            "models": [
                {
                    "slug": item.slug,
                    "supported_reasoning_levels": [{"effort": effort} for effort in item.efforts],
                    "service_tiers": (
                        [{"id": "priority"}] if "sol" in item.slug else []
                    ),
                }
                for item in self.catalog.models.values()
            ]
        }
        catalog = ModelCatalog.from_payload(payload)
        server = SpeedSetupServer(
            catalog=catalog,
            profiles=self.profiles,
            selected_profile="balanced",
            reason="configure",
            token="test-token",
            csrf="test-csrf",
        )
        page = server._render()
        terra_max = re.search(
            r'<input type="checkbox" name="fast__terra__max"([^>]*)>', page
        )
        sol_max = re.search(r'<input type="checkbox" name="fast__sol__max"([^>]*)>', page)
        self.assertIn("disabled", terra_max.group(1))
        self.assertNotIn("disabled", sol_max.group(1))

    def test_profile_configuration_page_actions_cover_copy_rename_default_and_delete(self) -> None:
        self.profiles.save_profile(
            "日常开发", builtin_matrix("balanced", self.catalog), self.catalog, set_default=True
        )
        server = SpeedSetupServer(
            catalog=self.catalog,
            profiles=self.profiles,
            selected_profile="日常开发",
            reason="profile_configuration",
        )
        server._apply_form(
            {
                "action": ["copy-profile"],
                "selected_profile": ["all-fast"],
                "target_profile": ["赶时间"],
            }
        )
        server._apply_form(
            {
                "action": ["rename-profile"],
                "selected_profile": ["赶时间"],
                "target_profile": ["紧急任务"],
            }
        )
        server._apply_form(
            {"action": ["set-default-profile"], "selected_profile": ["紧急任务"]}
        )
        server._apply_form(
            {"action": ["delete-profile"], "selected_profile": ["日常开发"]}
        )
        config = self.profiles.read()
        self.assertEqual(config["default_profile"], "紧急任务")
        self.assertEqual(set(config["profiles"]), {"紧急任务"})

    def test_catalog_change_page_exposes_future_reasoning_cells_dynamically(self) -> None:
        self.profiles.save_profile(
            "日常开发", builtin_matrix("balanced", self.catalog), self.catalog, set_default=True
        )
        payload = {
            "models": [
                {
                    "slug": item.slug,
                    "supported_reasoning_levels": [
                        *({"effort": effort} for effort in item.efforts),
                        {"effort": "future-level"},
                    ],
                    "service_tiers": [{"id": "priority"}],
                }
                for item in self.catalog.models.values()
            ]
        }
        changed = ModelCatalog.from_payload(payload)
        page = SpeedSetupServer(
            catalog=changed,
            profiles=self.profiles,
            selected_profile="日常开发",
            reason="catalog_changed",
        )._render()
        self.assertIn("Future-Level", page)
        self.assertIn("新增", page)
        self.assertIn("<details>", page)

    def _start(
        self,
        *,
        reason: str = "configure",
        timeout: float = 5,
        selected_profile: str = "balanced",
        job_store: JobStore | None = None,
    ):
        server = SpeedSetupServer(
            catalog=self.catalog,
            profiles=self.profiles,
            entry_context={"model": "gpt-5.6-sol", "reasoning": "medium", "service_tier": "priority"},
            job_store=job_store,
            selected_profile=selected_profile,
            reason=reason,
            timeout_seconds=timeout,
            token="test-token",
            csrf="test-csrf",
        )
        thread = threading.Thread(target=lambda: server.serve(open_browser=False))
        thread.start()
        deadline = time.time() + 2
        while not server.port and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(server.port)
        return server, thread

    def _request(
        self,
        server: SpeedSetupServer,
        method: str,
        path: str,
        *,
        body: str = "",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2)
        request_headers = dict(headers or {})
        if method == "POST":
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        connection.request(method, path, body=body.encode("utf-8"), headers=request_headers)
        response = connection.getresponse()
        payload = response.read().decode("utf-8", errors="replace")
        status = response.status
        connection.close()
        return status, payload


if __name__ == "__main__":
    unittest.main()
