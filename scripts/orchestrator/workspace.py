from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import run_text, safe_name


@dataclass(frozen=True)
class GitInfo:
    is_git: bool
    root: Path
    branch: str
    head: str
    dirty: bool
    porcelain: str


@dataclass(frozen=True)
class Worktree:
    task_id: str
    path: Path
    branch: str
    base_head: str
    integration: bool = False


@dataclass(frozen=True)
class ReadSnapshot:
    path: Path
    managed_worktree: bool
    baseline_digest: str


class WorkspaceManager:
    def __init__(self, workspace: Path, job_id: str, store: Any | None = None) -> None:
        self.requested_workspace = workspace.resolve()
        self.info = self.inspect(self.requested_workspace)
        self.job_id = safe_name(job_id, 48)
        self.temp_root = Path(tempfile.gettempdir()) / "codex-auto-orchestrator" / self.job_id
        self.worktrees: list[Worktree] = []
        self.read_snapshot: ReadSnapshot | None = None
        self._protected_branches: set[str] = set()
        self.store = store
        if store is not None:
            self._restore(store.read().get("workspace_resources", {}))

    @staticmethod
    def inspect(workspace: Path) -> GitInfo:
        try:
            root_text = run_text(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"])
        except RuntimeError:
            return GitInfo(False, workspace.resolve(), "", "", False, "")
        root = Path(root_text).resolve()
        head = run_text(["git", "-C", str(root), "rev-parse", "HEAD"])
        branch = run_text(["git", "-C", str(root), "branch", "--show-current"], check=False)
        porcelain = run_text(["git", "-C", str(root), "status", "--porcelain=v1"], check=False)
        return GitInfo(True, root, branch, head, bool(porcelain.strip()), porcelain)

    def create_task_worktree(self, task_id: str) -> Worktree:
        if not self.info.is_git or self.info.dirty:
            raise RuntimeError("isolated worktrees require a clean Git workspace")
        self.temp_root.mkdir(parents=True, exist_ok=True)
        normalized = safe_name(task_id, 40)
        for existing in self.worktrees:
            if existing.task_id == task_id and existing.path.exists():
                return existing
        path = self.temp_root / normalized
        branch = f"codex-orch/{self.job_id}/{normalized}"
        run_text(
            ["git", "-C", str(self.info.root), "worktree", "add", "-b", branch, str(path), self.info.head]
        )
        worktree = Worktree(task_id, path, branch, self.info.head)
        self.worktrees.append(worktree)
        self._persist()
        return worktree

    def create_integration_worktree(self) -> Worktree:
        if not self.info.is_git or self.info.dirty:
            raise RuntimeError("integration worktree requires a clean Git workspace")
        for existing in self.worktrees:
            if existing.integration and existing.path.exists():
                return existing
        self.temp_root.mkdir(parents=True, exist_ok=True)
        path = self.temp_root / "integration"
        branch = f"codex-orch/{self.job_id}/integration"
        run_text(
            ["git", "-C", str(self.info.root), "worktree", "add", "-b", branch, str(path), self.info.head]
        )
        worktree = Worktree("integration", path, branch, self.info.head, integration=True)
        self.worktrees.append(worktree)
        self._persist()
        return worktree

    def create_read_snapshot(self) -> ReadSnapshot:
        if self.read_snapshot is not None and self.read_snapshot.path.exists():
            return self.read_snapshot
        if self.read_snapshot is not None:
            # A missing persisted snapshot cannot contain user changes. Clear the
            # stale registry entry before rebuilding it from the current baseline.
            self.read_snapshot = None
            self._persist()
        self.temp_root.mkdir(parents=True, exist_ok=True)
        path = self.temp_root / "read-snapshot"
        if self.info.is_git and not self.info.dirty:
            run_text(
                ["git", "-C", str(self.info.root), "worktree", "add", "--detach", str(path), self.info.head]
            )
            managed_worktree = True
        else:
            shutil.copytree(self.info.root, path, symlinks=True)
            managed_worktree = False
        self.read_snapshot = ReadSnapshot(path, managed_worktree, _tree_digest(path))
        self._persist()
        return self.read_snapshot

    def verify_clean(self, worktree: Worktree) -> None:
        status = run_text(["git", "-C", str(worktree.path), "status", "--porcelain=v1"], check=False)
        if status.strip():
            raise RuntimeError(f"worktree {worktree.task_id} has uncommitted changes and was preserved")

    def head(self, worktree: Worktree) -> str:
        return run_text(["git", "-C", str(worktree.path), "rev-parse", "HEAD"])

    def apply_integration(self, integration: Worktree) -> str:
        current = self.inspect(self.info.root)
        if current.dirty:
            self._protected_branches.add(integration.branch)
            raise RuntimeError("original workspace changed during orchestration; integration was preserved")
        self.verify_clean(integration)
        integration_head = self.head(integration)
        if current.head == integration_head:
            return current.head
        if current.head != self.info.head:
            completed = subprocess.run(
                ["git", "-C", str(self.info.root), "merge-base", "--is-ancestor", integration_head, current.head],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if completed.returncode == 0:
                return current.head
            self._protected_branches.add(integration.branch)
            raise RuntimeError("original branch advanced during orchestration; integration was preserved")
        run_text(["git", "-C", str(self.info.root), "merge", "--ff-only", integration.branch])
        return run_text(["git", "-C", str(self.info.root), "rev-parse", "HEAD"])

    def diff_check(self, workspace: Path | None = None) -> str:
        if not self.info.is_git:
            return ""
        return run_text(["git", "-C", str(workspace or self.info.root), "diff", "--check"], check=False)

    def cleanup(self) -> list[str]:
        preserved: list[str] = []
        for worktree in reversed(self.worktrees):
            if worktree.branch in self._protected_branches:
                preserved.append(str(worktree.path))
                continue
            try:
                self.verify_clean(worktree)
            except RuntimeError:
                preserved.append(str(worktree.path))
                continue
            run_text(["git", "-C", str(self.info.root), "worktree", "remove", str(worktree.path)], check=False)
            self._remove_empty_managed_path(worktree.path)
            if worktree.path.exists():
                preserved.append(str(worktree.path))
        run_text(["git", "-C", str(self.info.root), "worktree", "prune"], check=False)
        for worktree in self.worktrees:
            if str(worktree.path) in preserved:
                continue
            run_text(["git", "-C", str(self.info.root), "branch", "-d", worktree.branch], check=False)
        if self.read_snapshot is not None:
            snapshot = self.read_snapshot
            if _tree_digest(snapshot.path) != snapshot.baseline_digest:
                preserved.append(str(snapshot.path))
            elif snapshot.managed_worktree:
                run_text(
                    ["git", "-C", str(self.info.root), "worktree", "remove", str(snapshot.path)],
                    check=False,
                )
                run_text(["git", "-C", str(self.info.root), "worktree", "prune"], check=False)
                self._remove_empty_managed_path(snapshot.path)
                if snapshot.path.exists():
                    preserved.append(str(snapshot.path))
            else:
                resolved = snapshot.path.resolve()
                temp_root = self.temp_root.resolve()
                if resolved.parent != temp_root:
                    raise RuntimeError(f"refusing to remove read snapshot outside temp root: {resolved}")
                shutil.rmtree(resolved)
        self._remove_empty_temp_root(preserved)
        self._persist()
        return preserved

    def workspace_fingerprint(self) -> dict[str, str]:
        if self.info.is_git:
            current = self.inspect(self.info.root)
            diff = run_text(["git", "-C", str(self.info.root), "diff", "--binary"], check=False)
            return {
                "head": current.head,
                "porcelain": current.porcelain,
                "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
            }
        return {"tree_sha256": _tree_digest(self.info.root)}

    def _restore(self, resources: dict[str, Any]) -> None:
        for item in resources.get("worktrees", []) if isinstance(resources, dict) else []:
            try:
                worktree = Worktree(
                    task_id=str(item["task_id"]),
                    path=Path(item["path"]),
                    branch=str(item["branch"]),
                    base_head=str(item["base_head"]),
                    integration=bool(item.get("integration", False)),
                )
            except (KeyError, TypeError):
                continue
            self.worktrees.append(worktree)
        snapshot = resources.get("read_snapshot") if isinstance(resources, dict) else None
        if isinstance(snapshot, dict):
            try:
                self.read_snapshot = ReadSnapshot(
                    Path(snapshot["path"]),
                    bool(snapshot["managed_worktree"]),
                    str(snapshot["baseline_digest"]),
                )
            except (KeyError, TypeError):
                self.read_snapshot = None

    def _persist(self) -> None:
        if self.store is None:
            return
        resources = {
            "worktrees": [
                {
                    "task_id": item.task_id,
                    "path": str(item.path),
                    "branch": item.branch,
                    "base_head": item.base_head,
                    "integration": item.integration,
                }
                for item in self.worktrees
                if item.path.exists()
            ],
            "read_snapshot": (
                {
                    "path": str(self.read_snapshot.path),
                    "managed_worktree": self.read_snapshot.managed_worktree,
                    "baseline_digest": self.read_snapshot.baseline_digest,
                }
                if self.read_snapshot is not None and self.read_snapshot.path.exists()
                else None
            ),
        }
        self.store.set_workspace_resources(resources)

    def _remove_empty_temp_root(self, preserved: list[str]) -> None:
        orchestrator_temp = (Path(tempfile.gettempdir()) / "codex-auto-orchestrator").resolve()
        resolved = self.temp_root.resolve()
        if resolved.parent != orchestrator_temp:
            raise RuntimeError(f"refusing to remove unexpected job temp root: {resolved}")
        if resolved.exists():
            if any(resolved.iterdir()):
                if not preserved:
                    preserved.append(str(resolved))
                return
            resolved.rmdir()
        try:
            orchestrator_temp.rmdir()
        except OSError:
            # Other concurrent or preserved jobs may still own the shared parent.
            pass

    def _remove_empty_managed_path(self, path: Path) -> None:
        if not path.exists():
            return
        resolved = path.resolve()
        temp_root = self.temp_root.resolve()
        if resolved.parent != temp_root:
            raise RuntimeError(f"refusing to remove managed path outside job temp root: {resolved}")
        try:
            resolved.rmdir()
        except OSError:
            # Non-empty content is evidence and will be preserved by the caller.
            pass


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == ".git":
            continue
        digest.update(relative.as_posix().encode("utf-8", errors="surrogatepass"))
        if path.is_symlink():
            digest.update(b"L")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogatepass"))
        elif path.is_file():
            digest.update(b"F")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        elif path.is_dir():
            digest.update(b"D")
    return digest.hexdigest()
