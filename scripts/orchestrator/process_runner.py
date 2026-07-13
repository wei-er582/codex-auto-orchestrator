from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import JobStore
from .util import atomic_write_json, load_json, safe_name, terminate_process_tree, utc_now


@dataclass
class RunOutcome:
    returncode: int
    timed_out: bool
    cancelled: bool
    output: dict[str, Any] | None
    stderr: str
    evidence_path: Path


class CodexRunner:
    def __init__(self, codex_command: list[str], store: JobStore, drop_codex_api_key: bool = False) -> None:
        self.codex_command = codex_command
        self.store = store
        self.drop_codex_api_key = drop_codex_api_key

    def execute(
        self,
        *,
        key: str,
        model: str,
        reasoning: str,
        workspace: Path,
        prompt: str,
        schema_path: Path,
        output_path: Path,
        timeout_seconds: int,
        read_only: bool = False,
    ) -> RunOutcome:
        invocation_dir = self.store.job_dir / "invocations" / safe_name(key)
        invocation_dir.mkdir(parents=True, exist_ok=True)
        events_path = invocation_dir / "events.jsonl"
        stderr_path = invocation_dir / "stderr.log"
        evidence_path = invocation_dir / "invocation.json"
        command = [
            *self.codex_command,
            "exec",
            "--disable",
            "plugins",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning}"',
            "-C",
            str(workspace),
            "--json",
            "--skip-git-repo-check",
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
        ]
        if read_only:
            command.extend(["-s", "read-only"])
        command.append("-")
        atomic_write_json(
            evidence_path,
            {
                "key": key,
                "requested_model": model,
                "requested_reasoning": reasoning,
                "workspace": str(workspace),
                "command": command,
                "started_at": utc_now(),
                "observed_models": [],
                "observed_reasoning": [],
                "returncode": None,
                "dropped_codex_api_key": self.drop_codex_api_key,
            },
        )

        environment = os.environ.copy()
        environment["CODEX_AUTO_ORCHESTRATOR_WORKER"] = "1"
        # A parent Codex process can inject CODEX_API_KEY even when the CLI is logged in
        # with ChatGPT. In that case the injected key overrides the stored login, so child
        # runs deliberately remove only that variable and keep every other auth setting.
        if self.drop_codex_api_key:
            environment.pop("CODEX_API_KEY", None)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=str(workspace),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        self.store.register_process(key, process.pid, model, reasoning)
        timed_out = False
        try:
            stdout, stderr = process.communicate(prompt, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_tree(process.pid)
            stdout, stderr = process.communicate()
        finally:
            self.store.unregister_process(key)

        events_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        observed_models, observed_reasoning, thread_ids = _extract_observed(stdout)
        session_models, session_reasoning, session_files = _extract_session_observed(thread_ids)
        observed_models = sorted(set(observed_models) | set(session_models))
        observed_reasoning = sorted(set(observed_reasoning) | set(session_reasoning))
        evidence = load_json(evidence_path)
        evidence.update(
            {
                "completed_at": utc_now(),
                "returncode": process.returncode,
                "timed_out": timed_out,
                "cancelled": self.store.cancelled(),
                "observed_models": observed_models,
                "observed_reasoning": observed_reasoning,
                "thread_ids": thread_ids,
                "session_evidence_files": session_files,
                "actual_selection_verified": (
                    model in observed_models and reasoning in observed_reasoning
                ),
            }
        )
        atomic_write_json(evidence_path, evidence)
        output = None
        if output_path.is_file():
            try:
                output = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                output = None
        return RunOutcome(
            returncode=process.returncode,
            timed_out=timed_out,
            cancelled=self.store.cancelled(),
            output=output,
            stderr=stderr,
            evidence_path=evidence_path,
        )


def _extract_observed(stream: str) -> tuple[list[str], list[str], list[str]]:
    models: set[str] = set()
    reasoning: set[str] = set()
    thread_ids: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "model" and isinstance(child, str):
                    models.add(child)
                elif key in {"reasoning_effort", "reasoning"} and isinstance(child, str):
                    reasoning.add(child)
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        walk(event)
        if isinstance(event, dict) and event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                thread_ids.add(thread_id)
    return sorted(models), sorted(reasoning), sorted(thread_ids)


def _extract_session_observed(
    thread_ids: list[str],
    session_root: Path | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Read the authoritative model selection from Codex turn_context records."""

    models: set[str] = set()
    reasoning: set[str] = set()
    files: list[str] = []
    root = session_root or (Path.home() / ".codex" / "sessions")
    if not root.is_dir():
        return [], [], []

    for thread_id in thread_ids:
        matches = sorted(root.glob(f"*/*/*/*{thread_id}.jsonl"), reverse=True)
        if not matches:
            continue
        session_file = matches[0]
        files.append(str(session_file))
        try:
            with session_file.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict) or event.get("type") != "turn_context":
                        continue
                    payload = event.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    model = payload.get("model")
                    effort = payload.get("effort")
                    if isinstance(model, str) and model:
                        models.add(model)
                    if isinstance(effort, str) and effort:
                        reasoning.add(effort)
        except OSError:
            continue
    return sorted(models), sorted(reasoning), files
