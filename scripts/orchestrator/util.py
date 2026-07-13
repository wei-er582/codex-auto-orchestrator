from __future__ import annotations

import json
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


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def split_command(command: str | None) -> list[str]:
    if command:
        return shlex.split(command, posix=os.name != "nt")
    override = os.environ.get("CODEX_ORCHESTRATOR_CODEX_COMMAND")
    if override:
        return shlex.split(override, posix=os.name != "nt")
    if os.name == "nt":
        # The WinGet Node prefix can push native helper paths past MAX_PATH. Prefer
        # the standard per-user npm prefix when that Codex installation exists.
        app_data = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        short_install = app_data / "npm" / "codex.cmd"
        if short_install.is_file():
            return [str(short_install)]
        return ["codex.cmd"]
    return ["codex"]


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
