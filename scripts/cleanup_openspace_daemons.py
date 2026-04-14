#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


PROCESS_MARKERS = (
    "openspace.mcp_server",
    "openspace.evolution_mcp_server",
)
STATE_FILE_PREFIXES = ("main-", "evolution-")
STATE_FILE_SUFFIXES = (".json", ".lock", ".log")


@dataclass
class ManagedProcess:
    pid: int
    command: str
    source: str
    state_dir: str | None = None
    record_file: str | None = None


def default_state_dirs() -> list[Path]:
    env_override = os.environ.get("OPENSPACE_MCP_DAEMON_STATE_DIR", "").strip()
    candidates = [
        Path(env_override).expanduser().resolve() if env_override else None,
        Path.home() / ".codex" / "state" / "openspace",
        Path.home() / ".codex-openspace" / "state" / "openspace",
        Path.home() / "Library" / "Application Support" / "openspace" / "mcp-daemons",
    ]

    result: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        if path is None:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def is_target_command(command: str) -> bool:
    return any(marker in command for marker in PROCESS_MARKERS)


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def command_for_pid(pid: int) -> str:
    if pid <= 0:
        return ""
    proc = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def collect_metadata_processes(state_dir: Path) -> tuple[list[ManagedProcess], list[dict[str, object]], list[str]]:
    managed: list[ManagedProcess] = []
    metadata_rows: list[dict[str, object]] = []
    warnings: list[str] = []

    if not state_dir.is_dir():
        return managed, metadata_rows, warnings

    for metadata_path in sorted(state_dir.glob("*.json")):
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"failed to read {metadata_path}: {exc}")
            continue

        pid = int(payload.get("pid") or 0)
        command = command_for_pid(pid) if pid_exists(pid) else ""
        live_target = bool(command and is_target_command(command))
        metadata_rows.append(
            {
                "state_dir": str(state_dir),
                "record_file": metadata_path.name,
                "pid": pid,
                "port": payload.get("port"),
                "workspace": payload.get("workspace"),
                "server_kind": payload.get("server_kind"),
                "live_target": live_target,
                "command": command,
            }
        )
        if live_target:
            managed.append(
                ManagedProcess(
                    pid=pid,
                    command=command,
                    source="metadata",
                    state_dir=str(state_dir),
                    record_file=metadata_path.name,
                )
            )

    return managed, metadata_rows, warnings


def collect_orphan_processes(excluded_pids: Iterable[int]) -> list[ManagedProcess]:
    excluded = set(excluded_pids)
    proc = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        check=False,
        capture_output=True,
        text=True,
    )

    results: list[ManagedProcess] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            pid_text, command = stripped.split(None, 1)
        except ValueError:
            continue
        pid = int(pid_text)
        if pid in excluded:
            continue
        if not is_target_command(command):
            continue
        results.append(ManagedProcess(pid=pid, command=command, source="orphan-scan"))
    return results


def terminate_process(pid: int, timeout_seconds: float) -> str:
    if not pid_exists(pid):
        return "already-exited"

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not pid_exists(pid):
            return "terminated"
        time.sleep(0.1)

    if pid_exists(pid):
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if not pid_exists(pid):
                return "killed"
            time.sleep(0.05)
    return "kill-sent"


def removable_state_files(state_dir: Path, keep_logs: bool) -> list[Path]:
    if not state_dir.is_dir():
        return []

    removable: list[Path] = []
    for path in sorted(state_dir.iterdir()):
        if not path.is_file():
            continue
        if not path.name.startswith(STATE_FILE_PREFIXES):
            continue
        if not path.name.endswith(STATE_FILE_SUFFIXES):
            continue
        if keep_logs and path.suffix == ".log":
            continue
        removable.append(path)
    return removable


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clean OpenSpace MCP daemon processes and state files")
    parser.add_argument(
        "--state-dir",
        action="append",
        default=[],
        help="Additional state directory to clean. Can be passed multiple times.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be terminated/removed without changing anything.",
    )
    parser.add_argument(
        "--keep-logs",
        action="store_true",
        help="Keep *.log files in state dirs while removing json/lock artifacts.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=3.0,
        help="How long to wait after SIGTERM before SIGKILL.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the final report as JSON.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    state_dirs = default_state_dirs()
    for raw in args.state_dir:
        state_dirs.append(Path(raw).expanduser().resolve())

    deduped_state_dirs: list[Path] = []
    seen_state_dirs: set[str] = set()
    for path in state_dirs:
        key = str(path)
        if key in seen_state_dirs:
            continue
        seen_state_dirs.add(key)
        deduped_state_dirs.append(path)

    metadata_processes: list[ManagedProcess] = []
    metadata_rows: list[dict[str, object]] = []
    warnings: list[str] = []
    for state_dir in deduped_state_dirs:
        managed, rows, row_warnings = collect_metadata_processes(state_dir)
        metadata_processes.extend(managed)
        metadata_rows.extend(rows)
        warnings.extend(row_warnings)

    orphan_processes = collect_orphan_processes(proc.pid for proc in metadata_processes)

    unique_processes: dict[int, ManagedProcess] = {}
    for proc in [*metadata_processes, *orphan_processes]:
        unique_processes.setdefault(proc.pid, proc)

    process_actions: list[dict[str, object]] = []
    for proc in sorted(unique_processes.values(), key=lambda item: item.pid):
        action = "would-terminate" if args.dry_run else terminate_process(proc.pid, args.timeout_seconds)
        process_actions.append(
            {
                **asdict(proc),
                "action": action,
            }
        )

    file_actions: list[dict[str, object]] = []
    for state_dir in deduped_state_dirs:
        for path in removable_state_files(state_dir, keep_logs=args.keep_logs):
            action = "would-remove"
            if not args.dry_run:
                path.unlink(missing_ok=True)
                action = "removed"
            file_actions.append(
                {
                    "state_dir": str(state_dir),
                    "path": str(path),
                    "action": action,
                }
            )

    report = {
        "state_dirs": [str(path) for path in deduped_state_dirs],
        "metadata_records": metadata_rows,
        "process_actions": process_actions,
        "file_actions": file_actions,
        "warnings": warnings,
        "dry_run": args.dry_run,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("OpenSpace daemon cleanup")
    print(f"dry run: {'yes' if args.dry_run else 'no'}")
    print("state dirs:")
    for path in report["state_dirs"]:
        print(f"- {path}")

    print("\nprocesses:")
    if process_actions:
        for item in process_actions:
            print(
                f"- pid={item['pid']} source={item['source']} action={item['action']} "
                f"record={item.get('record_file') or '-'}"
            )
    else:
        print("- none")

    print("\nfiles:")
    if file_actions:
        for item in file_actions:
            print(f"- {item['action']}: {item['path']}")
    else:
        print("- none")

    if warnings:
        print("\nwarnings:")
        for warning in warnings:
            print(f"- {warning}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
