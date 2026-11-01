"""Thin ``rsi-arena`` command line wrapper around :class:`ArenaClient`."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from rsi_arena.dto import ArenaProtocolError

from .baseline_registry import BASELINE_REGISTRY
from .client import ArenaClient, ArenaClientError, ArenaRequestError


def _json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit compact JSON")


def _add_run_set_parser(
    subparsers: argparse._SubParsersAction,
    *,
    complete_only: bool,
) -> None:
    """Install the shared development-set grammar for host and staged CLIs."""

    run_set = subparsers.add_parser(
        "run-set",
        help="evaluate a selected development set",
    )
    if not complete_only:
        run_set.add_argument(
            "benchmark_task_sets", nargs="*", metavar="BENCHMARK=TASK_ID,..."
        )
    run_set.add_argument("--all", action="store_true", required=complete_only)
    _json_option(run_set)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rsi-arena")
    commands = parser.add_subparsers(dest="command", required=True)

    context = commands.add_parser("context")
    _json_option(context)

    development = commands.add_parser("a")
    a_commands = development.add_subparsers(dest="a_command", required=True)
    run = a_commands.add_parser("run")
    run.add_argument("benchmark_tasks", nargs="+", metavar="BENCHMARK=TASK_ID")
    _json_option(run)
    _add_run_set_parser(a_commands, complete_only=False)

    submit = commands.add_parser("submit")
    submit.add_argument("--occurrence-key")
    _json_option(submit)

    result = commands.add_parser("result")
    result.add_argument("--occurrence-key", required=True)
    result.add_argument("--wait", action="store_true", required=True)
    _json_option(result)

    launch = commands.add_parser("launch", help="launch a generic command experiment")
    source = launch.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--experiment-toml",
        type=Path,
        help="human-authored experiment.toml",
    )
    source.add_argument(
        "--resume",
        action="store_true",
        help="read the write-once experiment from RUN_ROOT",
    )
    launch.add_argument("--run-root", type=Path, required=True)
    launch.add_argument(
        "--workspace-root",
        type=Path,
        help="new-run workspace template; resume restores the stored workspace",
    )

    baseline = commands.add_parser(
        "baseline", help="launch a repository-supported native baseline"
    )
    baseline_commands = baseline.add_subparsers(dest="baseline_command", required=True)
    baseline_launch = baseline_commands.add_parser("launch")
    baseline_launch.add_argument("--baseline", choices=BASELINE_REGISTRY, required=True)
    baseline_launch.add_argument("--experiment-toml", type=Path, required=True)
    baseline_launch.add_argument("--run-root", type=Path, required=True)
    baseline_launch.add_argument("--workspace-root", type=Path, required=True)
    baseline_resume = baseline_commands.add_parser("resume")
    baseline_resume.add_argument("--baseline", choices=BASELINE_REGISTRY, required=True)
    baseline_resume.add_argument("--run-root", type=Path, required=True)
    return parser


def _render(payload: dict[str, object], *, compact: bool) -> None:
    print(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":") if compact else None,
            indent=None if compact else 2,
            ensure_ascii=False,
        )
    )


def _benchmark_tasks(
    values: Sequence[str], *, multiple: bool
) -> dict[str, str] | dict[str, list[str]]:
    result: dict[str, str] | dict[str, list[str]] = {}
    for value in values:
        benchmark_id, separator, raw_tasks = value.partition("=")
        if not separator or not benchmark_id or not raw_tasks or benchmark_id in result:
            raise ValueError("development task mapping is invalid")
        if multiple:
            task_ids = raw_tasks.split(",")
            if not all(task_ids) or len(set(task_ids)) != len(task_ids):
                raise ValueError("development task mapping is invalid")
            result[benchmark_id] = task_ids  # type: ignore[assignment]
        else:
            if "," in raw_tasks:
                raise ValueError("development.run accepts one task per benchmark")
            result[benchmark_id] = raw_tasks  # type: ignore[assignment]
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "a" and args.a_command == "run-set":
        if args.all == bool(args.benchmark_task_sets):
            parser.error("a run-set requires BENCHMARK=TASK_ID,... or --all")
    if args.command == "launch":
        from rsi_arena.common import SnapshotAcquisitionError
        from rsi_arena.experiment import load_experiment_toml
        from rsi_arena.launcher import launch_command, resume_command

        try:
            if args.resume:
                if args.workspace_root is not None:
                    parser.error("launch --resume does not accept --workspace-root")
                completed = resume_command(
                    run_root=args.run_root,
                )
            else:
                if args.workspace_root is None:
                    parser.error("launch --experiment-toml requires --workspace-root")
                source = load_experiment_toml(args.experiment_toml)
                completed = launch_command(
                    source,
                    run_root=args.run_root,
                    workspace_root=args.workspace_root,
                )
            return int(completed.returncode)
        except (OSError, ValueError, SnapshotAcquisitionError) as exc:
            _render(
                {
                    "status": "ERROR",
                    "error": {"kind": "launch_error", "summary": str(exc)},
                },
                compact=True,
            )
            return 2
    if args.command == "baseline":
        from rsi_arena.baseline_dispatch import launch_baseline, resume_baseline
        from rsi_arena.experiment import load_experiment_toml

        try:
            if args.baseline_command == "launch":
                launch_baseline(
                    args.baseline,
                    load_experiment_toml(args.experiment_toml),
                    run_root=args.run_root,
                    workspace_root=args.workspace_root,
                )
            else:
                resume_baseline(args.baseline, run_root=args.run_root)
            return 0
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            _render(
                {
                    "status": "ERROR",
                    "error": {"kind": "baseline_launch_error", "summary": str(exc)},
                },
                compact=True,
            )
            return 2
    try:
        client = ArenaClient.from_environment()
        if args.command == "context":
            response = client.context()
        elif args.command == "a" and args.a_command == "run":
            response = client.development.run(
                _benchmark_tasks(args.benchmark_tasks, multiple=False)
            )
        elif args.command == "a" and args.a_command == "run-set":
            response = (
                client.development.run_set(all=True)
                if args.all
                else client.development.run_set(
                    _benchmark_tasks(args.benchmark_task_sets, multiple=True)
                )
            )
        elif args.command == "submit":
            response = client.submit_once(occurrence_key=args.occurrence_key)
        elif args.command == "result":
            response = client.result(args.occurrence_key, wait=args.wait)
        else:  # pragma: no cover - argparse owns the command grammar
            parser.error("unsupported command")
        _render(response.as_dict(), compact=args.json)
        return 0
    except ArenaRequestError as exc:
        _render(
            {
                "status": exc.status,
                "error": {"kind": exc.kind, "summary": exc.summary},
            },
            compact=args.json,
        )
    except (ArenaClientError, ArenaProtocolError, ValueError) as exc:
        _render(
            {
                "status": "ERROR",
                "error": {"kind": "client_error", "summary": str(exc)},
            },
            compact=args.json,
        )
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
