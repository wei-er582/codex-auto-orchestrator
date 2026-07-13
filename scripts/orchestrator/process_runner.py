from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .state import JobStore
from .util import atomic_write_json, load_json, safe_name, terminate_process_tree, utc_now


FAST_REJECTION = re.compile(r"(?i)(service[_ -]?tier|priority|fast).*(unsupported|unavailable|quota|reject|invalid|exhaust)")


@dataclass
class RunOutcome:
    returncode: int
    timed_out: bool
    cancelled: bool
    interrupted: bool
    interrupt_reason: str
    output: dict[str, Any] | None
    stderr: str
    evidence_path: Path
    session_id: str
    model_reasoning_verified: bool
    service_tier_acceptable: bool
    speed_fallback: bool = False


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
        service_tier: str = "default",
        speed_profile_name: str = "legacy-default",
        speed_policy_revision: int = 0,
        resume_session_id: str = "",
        interrupt_check: Callable[[], str] | None = None,
        allow_service_tier_retry: bool = True,
    ) -> RunOutcome:
        outcome = self._execute_once(
            key=key,
            model=model,
            reasoning=reasoning,
            service_tier=service_tier,
            speed_profile_name=speed_profile_name,
            speed_policy_revision=speed_policy_revision,
            workspace=workspace,
            prompt=prompt,
            schema_path=schema_path,
            output_path=output_path,
            timeout_seconds=timeout_seconds,
            read_only=read_only,
            resume_session_id=resume_session_id,
            interrupt_check=interrupt_check,
        )
        evidence = load_json(outcome.evidence_path)
        rejected = outcome.returncode != 0 and FAST_REJECTION.search(outcome.stderr)
        observed_degradation = (
            outcome.returncode == 0
            and bool(evidence.get("fast_degraded"))
            and read_only
        )
        if (
            service_tier == "priority"
            and allow_service_tier_retry
            and not outcome.cancelled
            and not outcome.interrupted
            and not outcome.timed_out
            and (rejected or observed_degradation)
        ):
            fallback = self._execute_once(
                key=f"{key}-speed-fallback",
                model=model,
                reasoning=reasoning,
                service_tier="default",
                speed_profile_name=speed_profile_name,
                speed_policy_revision=speed_policy_revision,
                workspace=workspace,
                prompt=prompt,
                schema_path=schema_path,
                output_path=output_path,
                timeout_seconds=timeout_seconds,
                read_only=read_only,
                resume_session_id="",
                interrupt_check=interrupt_check,
            )
            first_evidence = load_json(outcome.evidence_path)
            first_evidence["speed_fallback_to"] = str(fallback.evidence_path)
            first_evidence["speed_fallback_reason"] = (
                "backend rejected Fast" if rejected else "backend observed Standard for a Fast request"
            )
            atomic_write_json(outcome.evidence_path, first_evidence)
            return replace(fallback, speed_fallback=True)
        return outcome

    def _execute_once(
        self,
        *,
        key: str,
        model: str,
        reasoning: str,
        service_tier: str,
        speed_profile_name: str,
        speed_policy_revision: int,
        workspace: Path,
        prompt: str,
        schema_path: Path,
        output_path: Path,
        timeout_seconds: int,
        read_only: bool,
        resume_session_id: str,
        interrupt_check: Callable[[], str] | None,
    ) -> RunOutcome:
        invocation_dir = self.store.job_dir / "invocations" / safe_name(key)
        invocation_dir.mkdir(parents=True, exist_ok=True)
        events_path = invocation_dir / "events.jsonl"
        stderr_path = invocation_dir / "stderr.log"
        evidence_path = invocation_dir / "invocation.json"
        marker = str(evidence_path)
        command = self._command(
            model=model,
            reasoning=reasoning,
            service_tier=service_tier,
            workspace=workspace,
            schema_path=schema_path,
            output_path=output_path,
            read_only=read_only,
            resume_session_id=resume_session_id,
        )
        service_tier_request_verified = _command_requests_service_tier(command, service_tier)
        atomic_write_json(
            evidence_path,
            {
                "key": key,
                "requested_model": model,
                "requested_reasoning": reasoning,
                "requested_service_tier": service_tier,
                "speed_profile_name": speed_profile_name,
                "speed_policy_revision": speed_policy_revision,
                "workspace": str(workspace),
                "command": command,
                "resume_session_id": resume_session_id,
                "started_at": utc_now(),
                "observed_models": [],
                "observed_reasoning": [],
                "observed_service_tiers": [],
                "configured_service_tiers": [],
                "service_tier_request_verified": service_tier_request_verified,
                "returncode": None,
                "dropped_codex_api_key": self.drop_codex_api_key,
            },
        )

        environment = os.environ.copy()
        environment["CODEX_AUTO_ORCHESTRATOR_WORKER"] = "1"
        environment["CODEX_AUTO_ORCHESTRATOR_JOB"] = self.store.job_id
        environment["CODEX_AUTO_ORCHESTRATOR_INVOCATION"] = key
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
            bufsize=1,
        )
        self.store.register_process(key, process.pid, model, reasoning, service_tier, marker)
        if process.stdin is None or process.stdout is None or process.stderr is None:
            terminate_process_tree(process.pid)
            raise RuntimeError("failed to create Codex process streams")
        process.stdin.write(prompt)
        process.stdin.close()

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        thread_ids: set[str] = set()

        def read_stdout() -> None:
            with events_path.open("w", encoding="utf-8") as events:
                for line in process.stdout:
                    stdout_lines.append(line)
                    events.write(line)
                    events.flush()
                    thread_id = _thread_id_from_line(line)
                    if thread_id:
                        thread_ids.add(thread_id)
                        self.store.set_process_session(key, thread_id)

        def read_stderr() -> None:
            with stderr_path.open("w", encoding="utf-8") as errors:
                for line in process.stderr:
                    stderr_lines.append(line)
                    errors.write(line)
                    errors.flush()

        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        interrupted = False
        interrupt_reason = ""
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            if self.store.cancelled():
                terminate_process_tree(process.pid)
                break
            if interrupt_check:
                reason = interrupt_check()
                if reason:
                    interrupted = True
                    interrupt_reason = reason
                    terminate_process_tree(process.pid)
                    break
            if time.monotonic() >= deadline:
                timed_out = True
                terminate_process_tree(process.pid)
                break
            time.sleep(0.1)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            terminate_process_tree(process.pid)
            process.wait(timeout=15)
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        process.stdout.close()
        process.stderr.close()
        self.store.unregister_process(key)

        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        stream_models, stream_reasoning, stream_tiers, stream_threads = _extract_observed(stdout)
        thread_ids.update(stream_threads)
        session_models, session_reasoning, configured_tiers, session_files = _extract_session_observed(
            sorted(thread_ids)
        )
        observed_models = sorted(set(stream_models) | set(session_models))
        observed_reasoning = sorted(set(stream_reasoning) | set(session_reasoning))
        # Session thread settings prove what the CLI configured, not what the
        # backend ultimately served. Keep them separate from response evidence.
        observed_tiers = sorted(set(stream_tiers))
        evidence = load_json(evidence_path)
        model_reasoning_verified = model in observed_models and reasoning in observed_reasoning
        tier_evidence = _evaluate_service_tier_evidence(
            service_tier,
            observed_tiers,
            request_verified=service_tier_request_verified,
        )
        service_tier_verified = bool(tier_evidence["service_tier_verified"])
        service_tier_acceptable = bool(tier_evidence["service_tier_acceptable"])
        fast_degraded = bool(tier_evidence["fast_degraded"])
        all_settings_verified = model_reasoning_verified and service_tier_verified
        evidence.update(
            {
                "completed_at": utc_now(),
                "returncode": process.returncode,
                "timed_out": timed_out,
                "cancelled": self.store.cancelled(),
                "interrupted": interrupted,
                "interrupt_reason": interrupt_reason,
                "observed_models": observed_models,
                "observed_reasoning": observed_reasoning,
                "observed_service_tiers": observed_tiers,
                "configured_service_tiers": configured_tiers,
                "thread_ids": sorted(thread_ids),
                "session_evidence_files": session_files,
                "model_reasoning_verified": model_reasoning_verified,
                "service_tier_verified": service_tier_verified,
                "service_tier_acceptable": service_tier_acceptable,
                "service_tier_observation_status": tier_evidence[
                    "service_tier_observation_status"
                ],
                "service_tier_observation_note": tier_evidence[
                    "service_tier_observation_note"
                ],
                "actual_selection_verified": all_settings_verified,
                "all_settings_verified": all_settings_verified,
                "fast_degraded": fast_degraded,
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
            returncode=int(process.returncode or 0),
            timed_out=timed_out,
            cancelled=self.store.cancelled(),
            interrupted=interrupted,
            interrupt_reason=interrupt_reason,
            output=output,
            stderr=stderr,
            evidence_path=evidence_path,
            session_id=sorted(thread_ids)[-1] if thread_ids else resume_session_id,
            model_reasoning_verified=model_reasoning_verified,
            service_tier_acceptable=service_tier_acceptable,
        )

    def _command(
        self,
        *,
        model: str,
        reasoning: str,
        service_tier: str,
        workspace: Path,
        schema_path: Path,
        output_path: Path,
        read_only: bool,
        resume_session_id: str,
    ) -> list[str]:
        if resume_session_id:
            command = [
                *self.codex_command,
                "exec",
                "resume",
                "--disable",
                "plugins",
                "-m",
                model,
                "-c",
                f'model_reasoning_effort="{reasoning}"',
                "-c",
                f'service_tier="{service_tier}"',
                "-c",
                f'sandbox_mode="{"read-only" if read_only else "danger-full-access"}"',
                "--json",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path),
                "-o",
                str(output_path),
                resume_session_id,
                "-",
            ]
            return command
        command = [
            *self.codex_command,
            "exec",
            "--disable",
            "plugins",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning}"',
            "-c",
            f'service_tier="{service_tier}"',
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
        return command


def _thread_id_from_line(line: str) -> str:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return ""
    if isinstance(event, dict) and event.get("type") == "thread.started":
        value = event.get("thread_id")
        return value if isinstance(value, str) else ""
    return ""


def _extract_observed(stream: str) -> tuple[list[str], list[str], list[str], list[str]]:
    models: set[str] = set()
    reasoning: set[str] = set()
    service_tiers: set[str] = set()
    thread_ids: set[str] = set()

    def walk(value: Any, *, collect_service_tier: bool = False) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "model" and isinstance(child, str):
                    models.add(child)
                elif key in {"reasoning_effort", "reasoning", "effort"} and isinstance(child, str):
                    reasoning.add(child)
                elif key == "service_tier" and collect_service_tier and isinstance(child, str):
                    service_tiers.add(child)
                else:
                    walk(child, collect_service_tier=collect_service_tier)
        elif isinstance(value, list):
            for child in value:
                walk(child, collect_service_tier=collect_service_tier)

    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type") if isinstance(event, dict) else None
        # Local thread settings only demonstrate the requested configuration.
        # A tier is backend observation only when a completion-style response
        # exposes it. Codex CLI 0.144.0 does not currently do so.
        collect_service_tier = event_type in {
            "turn.completed",
            "response.completed",
            "response.done",
            "response.failed",
        }
        walk(event, collect_service_tier=collect_service_tier)
        if isinstance(event, dict) and event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                thread_ids.add(thread_id)
    return sorted(models), sorted(reasoning), sorted(service_tiers), sorted(thread_ids)


def _extract_session_observed(
    thread_ids: list[str],
    session_root: Path | None = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Read model/reasoning evidence and locally configured tier from a session."""

    models: set[str] = set()
    reasoning: set[str] = set()
    service_tiers: set[str] = set()
    files: list[str] = []
    root = session_root or (Path.home() / ".codex" / "sessions")
    if not root.is_dir():
        return [], [], [], []

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
                    if not isinstance(event, dict):
                        continue
                    payload = event.get("payload")
                    if event.get("type") == "turn_context" and isinstance(payload, dict):
                        model = payload.get("model")
                        effort = payload.get("effort")
                        if isinstance(model, str) and model:
                            models.add(model)
                        if isinstance(effort, str) and effort:
                            reasoning.add(effort)
                    elif (
                        event.get("type") == "event_msg"
                        and isinstance(payload, dict)
                        and payload.get("type") == "thread_settings_applied"
                    ):
                        settings = payload.get("thread_settings")
                        if not isinstance(settings, dict):
                            continue
                        model = settings.get("model")
                        effort = settings.get("reasoning_effort")
                        tier = settings.get("service_tier")
                        if isinstance(model, str) and model:
                            models.add(model)
                        if isinstance(effort, str) and effort:
                            reasoning.add(effort)
                        if isinstance(tier, str) and tier:
                            service_tiers.add(tier)
        except OSError:
            continue
    return sorted(models), sorted(reasoning), sorted(service_tiers), files


def _command_requests_service_tier(command: list[str], requested: str) -> bool:
    """Verify that the exact service-tier override was passed to the CLI."""

    expected = f'service_tier="{requested}"'
    return any(
        command[index] == "-c" and command[index + 1] == expected
        for index in range(len(command) - 1)
    )


def _evaluate_service_tier_evidence(
    requested: str,
    observed: list[str],
    *,
    request_verified: bool,
) -> dict[str, Any]:
    """Classify backend-tier evidence without inventing an observation.

    Codex CLI 0.144.0 forwards the explicit override but omits the backend's
    response tier from its JSON and session records. In that compatibility
    case execution may continue because the request is proven, while
    ``service_tier_verified`` remains false and the report says ``not_exposed``.
    """

    tiers = sorted(set(observed))
    if tiers == [requested]:
        return {
            "service_tier_verified": True,
            "service_tier_acceptable": True,
            "service_tier_observation_status": "matched",
            "service_tier_observation_note": "backend Service Tier matched the explicit request",
            "fast_degraded": False,
        }
    if requested == "priority" and tiers == ["default"]:
        return {
            "service_tier_verified": False,
            "service_tier_acceptable": True,
            "service_tier_observation_status": "degraded",
            "service_tier_observation_note": "backend served Standard for an explicit Fast request",
            "fast_degraded": True,
        }
    if not tiers:
        return {
            "service_tier_verified": False,
            "service_tier_acceptable": request_verified,
            "service_tier_observation_status": "not_exposed",
            "service_tier_observation_note": (
                "Codex CLI did not expose the backend Service Tier; the exact CLI override "
                + ("was verified" if request_verified else "could not be verified")
            ),
            "fast_degraded": False,
        }
    status = "conflicting" if len(tiers) > 1 else "mismatch"
    return {
        "service_tier_verified": False,
        "service_tier_acceptable": False,
        "service_tier_observation_status": status,
        "service_tier_observation_note": (
            f"backend Service Tier evidence {tiers!r} did not match request {requested!r}"
        ),
        "fast_degraded": requested == "priority" and "default" in tiers,
    }
