from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import signal
import subprocess
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_name(value: str, limit: int = 64) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (normalized or "item")[:limit].rstrip("-")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextmanager
def exclusive_file_lock(path: Path):
    """Serialize state mutations across threads and separate CLI processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def try_exclusive_file_lock(path: Path):
    """Acquire a non-blocking one-byte lease and yield False when another owner holds it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    acquired = False
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    acquired = False
            yield acquired
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_workspace(path: Path) -> str:
    resolved = str(path.expanduser().resolve())
    return os.path.normcase(resolved) if os.name == "nt" else resolved


def split_command(command: str | None) -> list[str]:
    if command:
        return _split_command_text(command)
    override = os.environ.get("CODEX_ORCHESTRATOR_CODEX_COMMAND")
    if override:
        return _split_command_text(override)
    if os.name == "nt":
        # The WinGet Node prefix can push native helper paths past MAX_PATH. Prefer
        # the standard per-user npm prefix when that Codex installation exists.
        app_data = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        short_install = app_data / "npm" / "codex.cmd"
        if short_install.is_file():
            return [str(short_install)]
        return ["codex.cmd"]
    return ["codex"]


def _split_command_text(command: str) -> list[str]:
    parts = shlex.split(command, posix=os.name != "nt")
    if os.name != "nt":
        return parts
    return [
        part[1:-1]
        if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"}
        else part
        for part in parts
    ]


def run_text(arguments: list[str], cwd: Path | None = None, check: bool = True) -> str:
    completed = subprocess.run(
        arguments,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"command failed ({completed.returncode}): {detail}")
    return completed.stdout.strip()


def uses_chatgpt_login(codex_command: list[str]) -> bool:
    completed = subprocess.run(
        [*codex_command, "login", "status"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    combined = f"{completed.stdout}\n{completed.stderr}".lower()
    return completed.returncode == 0 and "chatgpt" in combined


def terminate_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return


def process_identity(pid: int) -> dict[str, Any] | None:
    """Return stable-enough process evidence before any cross-process termination."""

    if pid <= 0:
        return None
    if os.name == "nt":
        script = (
            "$p=Get-CimInstance Win32_Process -Filter \"ProcessId="
            + str(pid)
            + "\" -ErrorAction SilentlyContinue;"
            "if($p){[pscustomobject]@{pid=[int]$p.ProcessId;created=[string]$p.CreationDate;"
            "command=[string]$p.CommandLine}|ConvertTo-Json -Compress}"
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    proc = Path("/proc") / str(pid)
    try:
        stat = (proc / "stat").read_text(encoding="utf-8", errors="replace").split()
        command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return None
    return {"pid": pid, "created": stat[21] if len(stat) > 21 else "", "command": command}


def same_process_identity(expected: dict[str, Any] | None, current: dict[str, Any] | None) -> bool:
    if not expected or not current:
        return False
    return (
        int(expected.get("pid", -1)) == int(current.get("pid", -2))
        and bool(expected.get("created"))
        and expected.get("created") == current.get("created")
    )
