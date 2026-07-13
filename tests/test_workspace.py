from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from orchestrator.workspace import WorkspaceManager


class WorkspaceManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.job_suffix = uuid.uuid4().hex[:10]
        self.test_worktrees: list[Path] = []
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main", str(self.root)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "Test User"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.email", "test@example.com"],
            check=True,
        )
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        self._commit("seed.txt", "test: seed")

    def tearDown(self) -> None:
        job_roots: set[Path] = set()
        for path in self.test_worktrees:
            job_roots.add(path.parent)
            if path.exists() and self.root.exists():
                subprocess.run(
                    ["git", "-C", str(self.root), "worktree", "remove", "--force", str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        for job_root in job_roots:
            try:
                job_root.rmdir()
                job_root.parent.rmdir()
            except OSError:
                pass
        self.temporary.cleanup()

    def _commit(self, path: str, message: str) -> None:
        subprocess.run(["git", "-C", str(self.root), "add", path], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", message],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def test_integration_is_idempotent_when_approved_head_is_already_applied(self) -> None:
        manager = WorkspaceManager(self.root, f"idempotent-{self.job_suffix}")
        integration = manager.create_integration_worktree()
        self.test_worktrees.append(integration.path)
        target = integration.path / "result.txt"
        target.write_text("done\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(integration.path), "add", target.name], check=True)
        subprocess.run(
            ["git", "-C", str(integration.path), "commit", "-m", "test: result"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        first = manager.apply_integration(integration)
        second = manager.apply_integration(integration)
        self.assertEqual(first, second)
        self.assertEqual(manager.cleanup(), [])

    def test_integration_stops_when_original_branch_advanced_elsewhere(self) -> None:
        manager = WorkspaceManager(self.root, f"advanced-{self.job_suffix}")
        integration = manager.create_integration_worktree()
        self.test_worktrees.append(integration.path)
        target = integration.path / "integration.txt"
        target.write_text("integration\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(integration.path), "add", target.name], check=True)
        subprocess.run(
            ["git", "-C", str(integration.path), "commit", "-m", "test: integration"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        (self.root / "other.txt").write_text("other\n", encoding="utf-8")
        self._commit("other.txt", "test: concurrent advance")
        with self.assertRaisesRegex(RuntimeError, "original branch advanced"):
            manager.apply_integration(integration)
        self.assertTrue(integration.path.exists())
        self.assertEqual(manager.cleanup(), [str(integration.path)])
        self.assertTrue(integration.path.exists())

    def test_non_git_workspace_fingerprint_detects_file_changes(self) -> None:
        non_git = Path(self.temporary.name) / "plain"
        non_git.mkdir()
        manager = WorkspaceManager(non_git, "fingerprint-job")
        before = manager.workspace_fingerprint()
        (non_git / "file.txt").write_text("changed\n", encoding="utf-8")
        after = manager.workspace_fingerprint()
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
