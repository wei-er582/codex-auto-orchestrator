#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orchestrator import __version__
from orchestrator.engine import Orchestrator, render_report
from orchestrator.state import JobStore, find_latest_job
from orchestrator.util import split_command


DEFAULT_RUN_ROOT = Path.home() / ".codex" / "orchestrator" / "runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Automatically route and execute Codex tasks")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="plan and execute a task")
    run.add_argument("--task", help="complete task text; stdin is used when omitted")
    run.add_argument("--task-file", type=Path, help="UTF-8 file containing the complete task")
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--policy", choices=["economy", "balanced", "quality"], default="balanced")
    run.add_argument("--dry-run", action="store_true", help="plan without executing workers")
    run.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    run.add_argument("--codex-command", help="override Codex executable for testing")
    run.add_argument("--json", action="store_true", help="print final state as JSON")

    for name in ("status", "cancel", "report"):
        command = subparsers.add_parser(name)
        command.add_argument("job_id", nargs="?", help="defaults to the most recently updated job")
        command.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        task = _read_task(args)
        orchestrator = Orchestrator(
            workspace=args.workspace,
            run_root=args.run_root,
            codex_command=split_command(args.codex_command),
            policy_name=args.policy,
        )
        store = orchestrator.run(task, dry_run=args.dry_run)
        state = store.read()
        if args.json:
            print(json.dumps(state, ensure_ascii=False, indent=2))
        else:
            print(f"job_id={store.job_id}")
            print(f"status={state['status']}")
            print(f"report={store.job_dir / 'report.md'}")
        return 0 if state["status"] == "complete" else 2

    run_root = args.run_root.expanduser().resolve()
    job_id = args.job_id or find_latest_job(run_root)
    store = JobStore(run_root, job_id)
    if not store.state_path.is_file():
        raise FileNotFoundError(f"job does not exist: {job_id}")
    if args.command == "status":
        print(json.dumps(store.read(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "cancel":
        pids = store.request_cancel()
        print(json.dumps({"job_id": job_id, "cancelled_pids": pids}, ensure_ascii=False, indent=2))
        return 0
    print(render_report(store))
    return 0


def _read_task(args: argparse.Namespace) -> str:
    sources = sum(bool(value) for value in (args.task, args.task_file))
    if sources > 1:
        raise ValueError("use only one of --task or --task-file")
    if args.task_file:
        return args.task_file.read_text(encoding="utf-8")
    if args.task:
        return args.task
    if sys.stdin.isatty():
        raise ValueError("provide --task, --task-file, or pipe task text on stdin")
    return sys.stdin.read()


if __name__ == "__main__":
    raise SystemExit(main())
