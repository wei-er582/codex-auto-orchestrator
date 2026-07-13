#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from orchestrator import __version__
from orchestrator.control import ControlQueue
from orchestrator.controller import JobController, launch_controller
from orchestrator.engine import Orchestrator, render_report
from orchestrator.entry_context import discover_entry_context
from orchestrator.model_catalog import ModelCatalog
from orchestrator.speed_profiles import (
    ProfileStore,
    ResolvedSpeedPolicy,
    SpeedConfigurationError,
    SpeedSetupRequired,
    format_matrix,
    normalize_matrix,
    parse_matrix_text,
)
from orchestrator.speed_ui import SpeedSetupServer
from orchestrator.state import (
    JobStore,
    TERMINAL_STATES,
    find_active_job,
    find_latest_job,
    heartbeat_stale,
)
from orchestrator.util import (
    atomic_write_text,
    exclusive_file_lock,
    normalize_workspace,
    process_identity,
    same_process_identity,
    sha256_text,
    split_command,
    terminate_process_tree,
    utc_now,
)


DEFAULT_RUN_ROOT = Path.home() / ".codex" / "orchestrator" / "runs"
DEFAULT_CONFIG = Path.home() / ".codex" / "orchestrator" / "config.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Automatically route and execute Codex tasks")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("run", "plan and execute in the foreground"),
        ("start", "start a temporary background controller"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        _add_task_source(command, allow_inline=name == "run")
        command.add_argument("--workspace", type=Path, default=Path.cwd())
        command.add_argument("--policy", choices=["economy", "balanced", "quality"], default="balanced")
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
        command.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        command.add_argument("--codex-command", help="override Codex executable for testing")
        command.add_argument("--speed-profile")
        command.add_argument("--custom-speed", action="store_true")
        command.add_argument("--entry-model")
        command.add_argument("--entry-reasoning")
        command.add_argument("--entry-service-tier")
        command.add_argument("--parent-job-id", default="")
        command.add_argument("--no-browser", action="store_true")
        command.add_argument("--json", action="store_true")

    profiles = subparsers.add_parser("profiles", help="manage named speed profiles")
    profile_commands = profiles.add_subparsers(dest="profile_command", required=True)
    for name in ("list", "show", "configure", "copy", "rename", "set-default", "delete"):
        command = profile_commands.add_parser(name)
        if name in {"show", "configure"}:
            command.add_argument("name", nargs="?")
        elif name != "list":
            command.add_argument("name")
        if name in {"copy", "rename"}:
            command.add_argument("target")
        command.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        command.add_argument("--codex-command")
        command.add_argument("--entry-service-tier", default="default")
        command.add_argument("--no-browser", action="store_true")
        command.add_argument("--json", action="store_true")

    speed = subparsers.add_parser("speed", help="change speed for later calls in an active job")
    speed.add_argument("job_id", nargs="?")
    speed.add_argument("--profile")
    speed.add_argument("--matrix-file", type=Path)
    speed.add_argument("--text-file", type=Path)
    speed.add_argument("--scope", choices=["job"], default="job")
    speed.add_argument("--save-profile")
    speed.add_argument("--set-default", action="store_true")
    speed.add_argument("--overwrite-profile", action="store_true")
    speed.add_argument("--immediate", action="store_true")
    speed.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    speed.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    speed.add_argument("--codex-command")

    steer = subparsers.add_parser("steer", help="guide or replace an active job objective")
    steer.add_argument("job_id", nargs="?")
    steer.add_argument("--instruction-file", type=Path)
    steer.add_argument("--mode", choices=["add", "replace"], default="add")
    steer.add_argument("--immediate", action="store_true")
    steer.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)

    pause = subparsers.add_parser("pause")
    pause.add_argument("job_id", nargs="?")
    pause.add_argument("--immediate", action="store_true")
    pause.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)

    resume = subparsers.add_parser("resume")
    resume.add_argument("job_id", nargs="?")
    resume.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    resume.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    resume.add_argument("--no-browser", action="store_true")

    followup = subparsers.add_parser("followup")
    followup.add_argument("job_id", nargs="?")
    _add_task_source(followup, allow_inline=False)
    followup.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    followup.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    followup.add_argument("--codex-command")
    followup.add_argument("--no-browser", action="store_true")

    for name in ("status", "cancel", "report"):
        command = subparsers.add_parser(name)
        command.add_argument("job_id", nargs="?")
        command.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)

    internal = subparsers.add_parser("_controller")
    internal.add_argument("job_id")
    internal.add_argument("--run-root", type=Path, required=True)

    speed_ui = subparsers.add_parser("_speed-ui")
    speed_ui.add_argument("job_id")
    speed_ui.add_argument("--run-root", type=Path, required=True)
    speed_ui.add_argument("--config", type=Path, required=True)
    speed_ui.add_argument("--port", type=int, required=True)
    speed_ui.add_argument("--token", required=True)
    speed_ui.add_argument("--csrf", required=True)
    speed_ui.add_argument("--reason", required=True)
    speed_ui.add_argument("--selected-profile", default="balanced")
    speed_ui.add_argument("--timeout", type=int, default=600)
    speed_ui.add_argument("--no-browser", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        if args.command in {"run", "start"}:
            return _handle_start(args, background=args.command == "start")
        if args.command == "profiles":
            return _handle_profiles(args)
        if args.command == "speed":
            return _handle_speed(args)
        if args.command == "steer":
            return _handle_steer(args)
        if args.command == "pause":
            return _handle_pause(args)
        if args.command == "resume":
            return _handle_resume(args)
        if args.command == "followup":
            return _handle_followup(args)
        if args.command in {"status", "cancel", "report"}:
            return _handle_job_command(args)
        if args.command == "_controller":
            return JobController(JobStore(args.run_root, args.job_id)).run()
        if args.command == "_speed-ui":
            return _handle_speed_ui(args)
    except (FileNotFoundError, RuntimeError, ValueError, SpeedConfigurationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _handle_start(args: argparse.Namespace, *, background: bool) -> int:
    task = _read_task(args)
    codex_command = split_command(args.codex_command)
    entry = discover_entry_context(
        model=args.entry_model,
        reasoning=args.entry_reasoning,
        service_tier=args.entry_service_tier,
    )
    orchestrator = Orchestrator(
        workspace=args.workspace,
        run_root=args.run_root,
        codex_command=codex_command,
        policy_name=args.policy,
    )
    profiles = ProfileStore(args.config)
    selected = args.speed_profile
    setup_reason = "job_customization" if args.custom_speed else ""
    speed_policy: ResolvedSpeedPolicy | None = None
    if not args.custom_speed:
        try:
            speed_policy = profiles.resolve(
                orchestrator.catalog,
                selected,
                entry_service_tier=entry["service_tier"],
            )
        except SpeedSetupRequired as exc:
            setup_reason = exc.reason
            selected = selected or profiles.read().get("default_profile") or "balanced"
    workspace_key = normalize_workspace(args.workspace)
    start_lock = args.run_root.expanduser().resolve() / ".workspace-locks" / (
        sha256_text(workspace_key) + ".lock"
    )
    with exclusive_file_lock(start_lock):
        active = find_active_job(
            args.run_root,
            args.workspace,
            origin_thread_id=entry["thread_id"],
        )
        if active:
            raise RuntimeError(
                f"workspace already has a non-terminal orchestration job: {active}; use steer, pause, resume, or cancel"
            )
        store = orchestrator.create_job(
            task,
            origin_thread_id=entry["thread_id"],
            parent_job_id=args.parent_job_id,
            entry_context=entry,
            initial_status="waiting_for_speed" if speed_policy is None else "planning",
            controller_config={
                "codex_command": codex_command,
                "dry_run": bool(args.dry_run),
                "config_path": str(args.config.expanduser().resolve()),
                "script_path": str(Path(__file__).resolve()),
            },
        )
    if speed_policy is not None:
        store.set_speed_policy(speed_policy.to_dict())
        if background:
            launch_controller(Path(__file__).resolve(), store)
            return _print_start(store, args.json)
        result = orchestrator.run_store(store, dry_run=args.dry_run)
        return _print_finish(result, args.json)

    if background:
        url = _launch_speed_ui(
            store,
            config_path=args.config,
            reason=setup_reason,
            selected_profile=selected or "balanced",
            open_browser=not args.no_browser,
        )
        store.set_controller(setup_url=url, setup_reason=setup_reason, status="waiting_for_speed")
        return _print_start(store, args.json, setup_url=url)

    server = SpeedSetupServer(
        catalog=orchestrator.catalog,
        profiles=profiles,
        entry_context=entry,
        job_store=store,
        selected_profile=selected or "balanced",
        reason=setup_reason,
    )
    result = server.serve(open_browser=not args.no_browser)
    if result.status != "saved":
        if result.status == "cancelled":
            store.transition("cancelled", "speed setup cancelled")
        return 2
    return _print_finish(orchestrator.run_store(store, dry_run=args.dry_run), args.json)


def _handle_profiles(args: argparse.Namespace) -> int:
    codex_command = split_command(args.codex_command)
    catalog = ModelCatalog.discover(codex_command)
    profiles = ProfileStore(args.config)
    command = args.profile_command
    if command == "list":
        values = profiles.list_profiles(catalog, args.entry_service_tier)
        if args.json:
            print(json.dumps(values, ensure_ascii=False, indent=2))
        else:
            for item in values:
                flags = ["built-in" if item["builtin"] else "saved"]
                if item["default"]:
                    flags.append("default")
                print(f"{item['name']} ({', '.join(flags)})")
        return 0
    if command == "show":
        name = args.name or profiles.read().get("default_profile")
        if not name:
            raise SpeedSetupRequired("first_setup")
        matrix = profiles.profile_matrix(name, catalog, entry_service_tier=args.entry_service_tier)
        if args.json:
            print(json.dumps({"name": name, "matrix": matrix}, ensure_ascii=False, indent=2))
        else:
            print(f"Current profile: {name}\n\n{format_matrix(matrix)}")
        return 0
    if command == "configure":
        current_config = profiles.read()
        server = SpeedSetupServer(
            catalog=catalog,
            profiles=profiles,
            entry_context={"service_tier": args.entry_service_tier},
            selected_profile=args.name or current_config.get("default_profile") or "balanced",
            reason="profile_configuration" if current_config.get("default_profile") else "first_setup",
        )
        result = server.serve(open_browser=not args.no_browser)
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
        return 0 if result.status == "saved" else 2
    if command == "copy":
        profiles.copy_profile(args.name, args.target, catalog, entry_service_tier=args.entry_service_tier)
    elif command == "rename":
        profiles.rename_profile(args.name, args.target)
    elif command == "set-default":
        profiles.set_default(args.name)
    elif command == "delete":
        profiles.delete_profile(args.name)
    print(json.dumps(profiles.read(), ensure_ascii=False, indent=2))
    return 0


def _handle_speed(args: argparse.Namespace) -> int:
    store = _job_store(args.run_root, args.job_id)
    state = store.read()
    if state["status"] in TERMINAL_STATES:
        raise RuntimeError("cannot change speed on a terminal job")
    codex_command = (
        split_command(args.codex_command)
        if args.codex_command
        else list(state.get("controller", {}).get("codex_command", []))
    )
    if not codex_command:
        codex_command = split_command(None)
    orchestrator = Orchestrator(
        workspace=Path(state["workspace"]),
        run_root=store.run_root,
        codex_command=codex_command,
        policy_name=state["policy"],
    )
    sources = sum(bool(value) for value in (args.profile, args.matrix_file, args.text_file))
    if sources != 1:
        raise ValueError("choose exactly one of --profile, --matrix-file, or --text-file")
    profiles = ProfileStore(args.config)
    resolved: ResolvedSpeedPolicy | None = None
    if args.profile:
        if args.save_profile:
            raise ValueError("--profile and --save-profile cannot be combined")
        resolved = profiles.resolve(
            orchestrator.catalog,
            args.profile,
            entry_service_tier=state.get("entry_context", {}).get("service_tier", "default"),
        )
        matrix = resolved.matrix
        profile_name = resolved.profile_name
    elif args.text_file:
        matrix = parse_matrix_text(args.text_file.read_text(encoding="utf-8"), orchestrator.catalog)
        profile_name = "job-override"
    else:
        raw = json.loads(args.matrix_file.read_text(encoding="utf-8"))
        matrix = normalize_matrix(raw.get("matrix", raw), orchestrator.catalog, require_complete=True)
        profile_name = str(raw.get("profile_name", "job-override")) if isinstance(raw, dict) else "job-override"
    if args.save_profile:
        profiles.save_profile(
            args.save_profile,
            matrix,
            orchestrator.catalog,
            set_default=bool(args.set_default),
            overwrite=bool(args.overwrite_profile),
        )
        resolved = profiles.resolve(
            orchestrator.catalog,
            args.save_profile,
            entry_service_tier=state.get("entry_context", {}).get("service_tier", "default"),
        )
        matrix = resolved.matrix
        profile_name = resolved.profile_name
    elif args.set_default or args.overwrite_profile:
        raise ValueError("--set-default and --overwrite-profile require --save-profile")

    if resolved is None:
        resolved = ResolvedSpeedPolicy(
            profile_name=profile_name,
            matrix=matrix,
            model_bindings={
                family: str(item["model"])
                for family, item in orchestrator.catalog.speed_matrix_catalog().items()
            },
            catalog_fingerprint=orchestrator.catalog.fingerprint(),
            known_combinations=sorted(orchestrator.catalog.speed_combinations()),
            source="job-override",
        )

    if state["status"] == "waiting_for_speed":
        reason = str(state.get("controller", {}).get("setup_reason", "first_setup"))
        selected = str(state.get("controller", {}).get("selected_profile", ""))
        if reason == "first_setup" and not (args.save_profile and args.set_default):
            raise ValueError("first setup text fallback must save a named profile and set it as default")
        if reason == "catalog_changed" and not (
            args.save_profile == selected and args.overwrite_profile
        ):
            raise ValueError("catalog change text fallback must update the selected named profile")
        _stop_speed_setup(store)
        store.set_speed_policy(resolved.to_dict())
        store.set_desired_status("running")
        store.set_checkpoint(phase="speed-configured", safe=True)
        store.transition("planning", "speed configured through text fallback before model invocation")
        launch_controller(Path(__file__).resolve(), store)
        print(
            json.dumps(
                {
                    "job_id": store.job_id,
                    "status": "planning",
                    "profile_name": resolved.profile_name,
                    "matrix": resolved.matrix,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    request = ControlQueue(store.control_path).enqueue(
        "speed-change",
        payload={"matrix": matrix, "profile_name": profile_name},
        source_thread_id=os.environ.get("CODEX_THREAD_ID", ""),
        boundary="immediate" if args.immediate else "safe",
    )
    print(json.dumps(request, ensure_ascii=False, indent=2))
    return 0


def _handle_steer(args: argparse.Namespace) -> int:
    store = _job_store(args.run_root, args.job_id)
    if store.read()["status"] in TERMINAL_STATES:
        raise RuntimeError("terminal jobs use followup instead of steer")
    instruction = (
        args.instruction_file.read_text(encoding="utf-8")
        if args.instruction_file
        else _read_stdin_required("provide --instruction-file or pipe steering text on stdin")
    )
    controls_dir = store.job_dir / "controls"
    instruction_path = controls_dir / f"steer-{uuid.uuid4().hex}.txt"
    atomic_write_text(instruction_path, instruction)
    request = ControlQueue(store.control_path).enqueue(
        "steer",
        payload={
            "instruction_file": str(instruction_path),
            "sha256": sha256_text(instruction),
            "mode": args.mode,
        },
        source_thread_id=os.environ.get("CODEX_THREAD_ID", ""),
        boundary="immediate" if args.immediate else "safe",
    )
    print(json.dumps(request, ensure_ascii=False, indent=2))
    return 0


def _handle_pause(args: argparse.Namespace) -> int:
    store = _job_store(args.run_root, args.job_id)
    request = ControlQueue(store.control_path).enqueue(
        "pause",
        source_thread_id=os.environ.get("CODEX_THREAD_ID", ""),
        boundary="immediate" if args.immediate else "safe",
    )
    store.set_desired_status("paused")
    print(json.dumps(request, ensure_ascii=False, indent=2))
    return 0


def _handle_resume(args: argparse.Namespace) -> int:
    store = _job_store(args.run_root, args.job_id)
    state = store.read()
    if state["status"] in TERMINAL_STATES:
        raise RuntimeError("terminal jobs cannot be resumed; use followup")
    if state["status"] == "waiting_for_speed":
        _stop_speed_setup(store)
        reason = str(state.get("controller", {}).get("setup_reason", "first_setup"))
        selected = str(state.get("speed_profile") or "balanced")
        url = _launch_speed_ui(
            store,
            config_path=args.config,
            reason=reason,
            selected_profile=selected,
            open_browser=not args.no_browser,
        )
        print(json.dumps({"job_id": store.job_id, "status": "waiting_for_speed", "setup_url": url}, ensure_ascii=False, indent=2))
        return 0
    request = ControlQueue(store.control_path).enqueue(
        "resume",
        source_thread_id=os.environ.get("CODEX_THREAD_ID", ""),
    )
    store.set_desired_status("running")
    if state["status"] == "interrupted" or _controller_is_stale(state):
        launch_controller(Path(__file__).resolve(), store)
    print(json.dumps(request, ensure_ascii=False, indent=2))
    return 0


def _handle_followup(args: argparse.Namespace) -> int:
    parent = _job_store(args.run_root, args.job_id)
    state = parent.read()
    task = _read_task(args)
    if state["status"] not in TERMINAL_STATES:
        temporary = parent.job_dir / "controls" / f"followup-{uuid.uuid4().hex}.txt"
        atomic_write_text(temporary, task)
        request = ControlQueue(parent.control_path).enqueue(
            "steer",
            payload={"instruction_file": str(temporary), "sha256": sha256_text(task), "mode": "add"},
            source_thread_id=os.environ.get("CODEX_THREAD_ID", ""),
        )
        print(json.dumps(request, ensure_ascii=False, indent=2))
        return 0
    start_args = argparse.Namespace(
        task=None,
        task_file=args.task_file,
        workspace=Path(state["workspace"]),
        policy=state["policy"],
        dry_run=False,
        run_root=args.run_root,
        config=args.config,
        codex_command=args.codex_command,
        speed_profile=None,
        custom_speed=False,
        entry_model=None,
        entry_reasoning=None,
        entry_service_tier=None,
        parent_job_id=parent.job_id,
        no_browser=args.no_browser,
        json=False,
    )
    if args.task_file is None:
        followup_file = parent.job_dir / "controls" / f"new-job-{uuid.uuid4().hex}.txt"
        atomic_write_text(followup_file, task)
        start_args.task_file = followup_file
    return _handle_start(start_args, background=True)


def _handle_job_command(args: argparse.Namespace) -> int:
    store = _job_store(args.run_root, args.job_id)
    if args.command == "status":
        state = store.read()
        if state["status"] not in TERMINAL_STATES and state["status"] not in {"waiting_for_speed", "paused", "interrupted"} and _controller_is_stale(state):
            store.transition("interrupted", "controller heartbeat and process identity are stale")
            state = store.read()
        print(json.dumps(state, ensure_ascii=False, indent=2))
        if state["status"] == "waiting_for_speed":
            print("\n" + _speed_fallback_text(store))
        return 0
    if args.command == "cancel":
        request = ControlQueue(store.control_path).enqueue(
            "cancel", source_thread_id=os.environ.get("CODEX_THREAD_ID", ""), boundary="immediate"
        )
        pids = store.request_cancel()
        if store.read()["status"] == "waiting_for_speed":
            setup_pid = _stop_speed_setup(store)
            if setup_pid:
                pids.append(setup_pid)
            store.transition("cancelled", "cancelled before model planning")
        print(json.dumps({"job_id": store.job_id, "request": request, "cancelled_pids": pids}, ensure_ascii=False, indent=2))
        return 0
    print(render_report(store))
    return 0


def _handle_speed_ui(args: argparse.Namespace) -> int:
    store = JobStore(args.run_root, args.job_id)
    state = store.read()
    config = state.get("controller", {})
    orchestrator = Orchestrator(
        workspace=Path(state["workspace"]),
        run_root=store.run_root,
        codex_command=list(config["codex_command"]),
        policy_name=state["policy"],
    )
    server = SpeedSetupServer(
        catalog=orchestrator.catalog,
        profiles=ProfileStore(args.config),
        entry_context=state.get("entry_context", {}),
        job_store=store,
        selected_profile=args.selected_profile,
        reason=args.reason,
        timeout_seconds=args.timeout,
        port=args.port,
        token=args.token,
        csrf=args.csrf,
    )
    result = server.serve(open_browser=not args.no_browser)
    if result.status == "saved":
        store.transition("planning", "speed setup completed before model invocation")
        launch_controller(Path(__file__).resolve(), store)
        return 0
    if result.status == "cancelled":
        store.transition("cancelled", "speed setup cancelled")
        return 0
    store.set_controller(status="waiting_for_speed", setup_expired_at=utc_now())
    return 2


def _launch_speed_ui(
    store: JobStore,
    *,
    config_path: Path,
    reason: str,
    selected_profile: str,
    open_browser: bool,
) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_speed-ui",
        store.job_id,
        "--run-root",
        str(store.run_root),
        "--config",
        str(config_path.expanduser().resolve()),
        "--port",
        str(port),
        "--token",
        token,
        "--csrf",
        csrf,
        "--reason",
        reason,
        "--selected-profile",
        selected_profile,
    ]
    if not open_browser:
        command.append("--no-browser")
    creationflags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    log_path = store.job_dir / "speed-setup.log"
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    url = f"http://127.0.0.1:{port}/?token={token}"
    store.set_controller(
        setup_pid=process.pid,
        setup_identity=process_identity(process.pid),
        setup_url=url,
        setup_log=str(log_path),
        setup_reason=reason,
        selected_profile=selected_profile,
    )
    return url


def _stop_speed_setup(store: JobStore) -> int:
    state = store.read()
    controller = state.get("controller", {})
    pid = int(controller.get("setup_pid", 0) or 0)
    expected = controller.get("setup_identity")
    current = process_identity(pid)
    command = str((current or {}).get("command", ""))
    if (
        pid > 0
        and same_process_identity(expected, current)
        and store.job_id.lower() in command.lower()
        and "_speed-ui" in command.lower()
    ):
        terminate_process_tree(pid)
        store.set_controller(setup_status="stopped", setup_stopped_at=utc_now())
        return pid
    return 0


def _speed_fallback_text(store: JobStore) -> str:
    state = store.read()
    config = state.get("controller", {})
    try:
        orchestrator = Orchestrator(
            workspace=Path(state["workspace"]),
            run_root=store.run_root,
            codex_command=list(config["codex_command"]),
            policy_name=state["policy"],
        )
        profiles = ProfileStore(Path(config.get("config_path", DEFAULT_CONFIG)))
        name = config.get("selected_profile") or profiles.read().get("default_profile") or "balanced"
        matrix = profiles.profile_matrix(
            name,
            orchestrator.catalog,
            entry_service_tier=state.get("entry_context", {}).get("service_tier", "default"),
        )
        return (
            f"Current profile: {name}\n\n{format_matrix(matrix)}\n\n"
            "Reply with 'use default' or provide a complete Sol Fast / Terra Fast matrix; the Skill will pass it through a UTF-8 file."
        )
    except Exception as exc:
        return f"Speed setup is waiting. Local matrix fallback could not be rendered: {exc}"


def _controller_is_stale(state: dict[str, Any]) -> bool:
    if not heartbeat_stale(state):
        return False
    controller = state.get("controller", {})
    pid = int(controller.get("pid", 0) or 0)
    return not same_process_identity(controller.get("identity"), process_identity(pid))


def _job_store(run_root: Path, job_id: str | None) -> JobStore:
    root = run_root.expanduser().resolve()
    selected = job_id or find_latest_job(root)
    store = JobStore(root, selected)
    if not store.state_path.is_file():
        raise FileNotFoundError(f"job does not exist: {selected}")
    return store


def _print_start(store: JobStore, as_json: bool, setup_url: str = "") -> int:
    state = store.read()
    value = {"job_id": store.job_id, "status": state["status"], "report": str(store.job_dir / "report.md")}
    if setup_url:
        value["setup_url"] = setup_url
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        for key, item in value.items():
            print(f"{key}={item}")
    return 0


def _print_finish(store: JobStore, as_json: bool) -> int:
    state = store.read()
    if as_json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        print(f"job_id={store.job_id}")
        print(f"status={state['status']}")
        print(f"report={store.job_dir / 'report.md'}")
    return 0 if state["status"] == "complete" else 2


def _add_task_source(parser: argparse.ArgumentParser, *, allow_inline: bool) -> None:
    if allow_inline:
        parser.add_argument("--task", help="compatibility only; prefer --task-file or stdin")
    parser.add_argument("--task-file", type=Path, help="UTF-8 file containing the complete task")


def _read_task(args: argparse.Namespace) -> str:
    inline = getattr(args, "task", None)
    task_file = getattr(args, "task_file", None)
    if inline and task_file:
        raise ValueError("use only one task source")
    if task_file:
        return task_file.read_text(encoding="utf-8")
    if inline:
        return inline
    return _read_stdin_required("provide --task-file or pipe task text on stdin")


def _read_stdin_required(message: str) -> str:
    if sys.stdin.isatty():
        raise ValueError(message)
    value = sys.stdin.read()
    if not value.strip():
        raise ValueError(message)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
