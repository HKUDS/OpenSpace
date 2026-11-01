"""Agent-facing CLI surface for an Arena agentic experiment."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from rsi_arena.dto import ArenaProtocolError

from .cli import _add_run_set_parser, _render
from .cli import main as _public_main
from .client import (
    ARENA_CLIENT_STATE_ENV,
    ArenaClient,
    ArenaClientError,
    ArenaClientStateError,
    ArenaRequestError,
)

_A_OPERATION_LOCK = "a-operation.lock"
_A_WAIT_HEARTBEAT_SECONDS = 60.0


def _is_plural_development_terminal(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"benchmark_results"}:
        return False
    results = value.get("benchmark_results")
    if not isinstance(results, list) or not results:
        return False
    benchmark_ids: list[str] = []
    for result in results:
        if (
            not isinstance(result, Mapping)
            or set(result)
            != {
                "benchmark_id",
                "outcome",
                "development_evaluation_id",
                "evidence_path",
            }
            or not isinstance(result.get("benchmark_id"), str)
            or not result.get("benchmark_id")
            or result.get("outcome") not in {"measured", "inconclusive"}
            or not isinstance(result.get("development_evaluation_id"), str)
            or not result.get("development_evaluation_id")
            or (
                result.get("evidence_path") is not None
                and not isinstance(result.get("evidence_path"), str)
            )
        ):
            return False
        benchmark_ids.append(str(result["benchmark_id"]))
    return len(set(benchmark_ids)) == len(benchmark_ids)


def _json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit compact JSON")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rsi-arena")
    commands = parser.add_subparsers(dest="command", required=True)

    context = commands.add_parser("context")
    _json_option(context)

    development = commands.add_parser("a")
    a_commands = development.add_subparsers(dest="a_command", required=True)
    _add_run_set_parser(a_commands, complete_only=True)

    submit = commands.add_parser("submit")
    submit.add_argument("--occurrence-key")
    _json_option(submit)

    result = commands.add_parser("result")
    result.add_argument("--occurrence-key", required=True)
    result.add_argument("--wait", action="store_true", required=True)
    _json_option(result)

    return parser


def _read_a_lock_state(
    descriptor: int,
) -> tuple[Path, dict[str, object] | None]:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        fields = os.read(descriptor, 4096).decode("utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ArenaClientStateError("Arena A admission state is unavailable") from exc
    if len(fields) != 2 or not fields[0]:
        raise ArenaClientStateError("Arena A admission state is invalid")
    if not fields[1]:
        return Path(fields[0]), None
    try:
        completed = json.loads(fields[1])
    except json.JSONDecodeError as exc:
        raise ArenaClientStateError("Arena A admission state is invalid") from exc
    terminal = completed.get("result") if isinstance(completed, Mapping) else None
    if (
        not isinstance(completed, dict)
        or set(completed) != {"state", "result"}
        or completed.get("state") != "TERMINAL"
        or not _is_plural_development_terminal(terminal)
    ):
        raise ArenaClientStateError("Arena A admission state is invalid")
    return Path(fields[0]), completed


def _write_a_lock_state(
    descriptor: int,
    evidence_root: Path,
    completed: Mapping[str, object] | None,
) -> None:
    encoded = (
        json.dumps(
            dict(completed),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if completed is not None
        else ""
    )
    payload = f"{evidence_root}\n{encoded}\n".encode("utf-8")
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, payload)
    except OSError as exc:
        raise ArenaClientStateError("Arena A admission state update failed") from exc


@contextmanager
def _a_waiting_heartbeat() -> Iterator[None]:
    """Keep long foreground A waits visibly distinct from a hung client."""

    stopped = threading.Event()
    started_at = time.monotonic()
    print(
        "[rsi-arena] waiting in the foreground for the synchronous A "
        "operation to return terminal JSON",
        file=sys.stderr,
        flush=True,
    )

    def heartbeat() -> None:
        while not stopped.wait(_A_WAIT_HEARTBEAT_SECONDS):
            elapsed = max(0.0, time.monotonic() - started_at)
            print(
                "[rsi-arena] client still waiting for the A operation "
                f"terminal JSON; elapsed={elapsed:.0f}s",
                file=sys.stderr,
                flush=True,
            )

    worker = threading.Thread(
        target=heartbeat,
        name="rsi-arena-a-wait-heartbeat",
        daemon=True,
    )
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()


@contextmanager
def _agentic_a_admission() -> Iterator[tuple[int, bool]]:
    state = os.environ.get(ARENA_CLIENT_STATE_ENV)
    if not state:
        raise ArenaClientStateError("Arena client environment is incomplete")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(Path(state) / _A_OPERATION_LOCK, flags, 0o600)
    except OSError as exc:
        raise ArenaClientStateError("Arena A admission lock failed") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ArenaClientStateError("Arena A admission lock is invalid")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield descriptor, True
        else:
            evidence_root, _completed = _read_a_lock_state(descriptor)
            _write_a_lock_state(descriptor, evidence_root, None)
            yield descriptor, False
    finally:
        os.close(descriptor)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(arguments)
    if args.command == "a" and args.a_command == "run-set":
        try:
            with _a_waiting_heartbeat():
                with _agentic_a_admission() as (descriptor, contended):
                    evidence_root, completed = _read_a_lock_state(descriptor)
                    if contended:
                        if completed is None:
                            raise ArenaClientStateError(
                                "in-flight A completed without one new public result"
                            )
                        public = completed
                    else:
                        client = ArenaClient.from_environment()
                        public = client.development.run_set(all=True).as_dict()
                        terminal = public.get("result")
                        if public.get("state") != "TERMINAL" or not (
                            _is_plural_development_terminal(terminal)
                        ):
                            raise ArenaClientStateError(
                                "in-flight A public result changed identity"
                            )
                        client.context()
                        _write_a_lock_state(descriptor, evidence_root, public)
            _render(public, compact=args.json)
            return 0
        except ArenaRequestError as exc:
            _render(
                {
                    "status": exc.status,
                    "error": {"kind": exc.kind, "summary": exc.summary},
                },
                compact=args.json,
            )
            return 2
        except (ArenaClientError, ArenaProtocolError, ValueError) as exc:
            _render(
                {
                    "status": "ERROR",
                    "error": {"kind": "client_error", "summary": str(exc)},
                },
                compact=args.json,
            )
            return 2
    return _public_main(arguments)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
