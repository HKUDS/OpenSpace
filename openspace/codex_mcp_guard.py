from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


HOST_MARKER = "codex app-server"
OPENSPACE_PROXY_MARKER = "openspace.mcp_proxy"
COMPUTER_USE_MARKER = "SkyComputerUseClient mcp"
DEFAULT_STALE_AGE_SECONDS = 60 * 60
DEFAULT_WARN_TOTAL_COUNT = 24
DEFAULT_THRESHOLD_TOTAL_COUNT = 48
DEFAULT_WARN_STALE_COUNT = 8
DEFAULT_THRESHOLD_STALE_COUNT = 16
DEFAULT_TAIL_LIMIT = 20


@dataclass(frozen=True)
class ManagedChild:
    pid: int
    ppid: int
    kind: str
    age_seconds: int
    command: str
    is_descendant: bool
    mode: str | None = None


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_guard_dir() -> Path:
    return repo_root() / "logs" / "codex_mcp_guard"


def parse_elapsed_seconds(value: str) -> int:
    raw = (value or "").strip()
    if not raw:
        return 0
    if "-" in raw:
        days_text, time_text = raw.split("-", 1)
        days = int(days_text)
        return days * 86400 + parse_elapsed_seconds(time_text)

    parts = [int(item) for item in raw.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"Unsupported elapsed time format: {value!r}")


def classify_command(command: str) -> str | None:
    if HOST_MARKER in command:
        return "codex_app_server"
    if OPENSPACE_PROXY_MARKER in command and "--kind main" in command:
        return "openspace_main"
    if OPENSPACE_PROXY_MARKER in command and "--kind evolution" in command:
        return "openspace_evolution"
    if COMPUTER_USE_MARKER in command:
        return "computer_use_mcp"
    return None


def collect_process_rows() -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,etime=,command="],
        check=False,
        capture_output=True,
        text=True,
    )
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 3)
        if len(parts) != 4:
            continue
        pid_text, ppid_text, etime, command = parts
        rows.append(
            {
                "pid": int(pid_text),
                "ppid": int(ppid_text),
                "etime": etime,
                "command": command,
            }
        )
    return rows


def proxy_mode_for_pid(pid: int) -> str | None:
    proc = subprocess.run(
        ["ps", "eww", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    text = proc.stdout
    if "OPENSPACE_MCP_PROXY_MODE=direct" in text:
        return "direct"
    if "OPENSPACE_MCP_PROXY_MODE=daemon" in text:
        return "daemon"
    return None


def pick_host_pid(process_rows: list[dict[str, Any]], requested_host_pid: int | None = None) -> tuple[int | None, list[int]]:
    candidates = sorted(
        int(row["pid"])
        for row in process_rows
        if classify_command(str(row["command"])) == "codex_app_server"
    )
    if requested_host_pid is not None:
        return (requested_host_pid if requested_host_pid in candidates else None), candidates
    if not candidates:
        return None, []
    return candidates[-1], candidates


def descendant_pid_set(process_rows: list[dict[str, Any]], host_pid: int | None) -> set[int]:
    if host_pid is None:
        return set()
    children_by_parent: dict[int, list[int]] = {}
    for row in process_rows:
        children_by_parent.setdefault(int(row["ppid"]), []).append(int(row["pid"]))

    descendants: set[int] = set()
    stack = [host_pid]
    while stack:
        current = stack.pop()
        for child in children_by_parent.get(current, []):
            if child in descendants:
                continue
            descendants.add(child)
            stack.append(child)
    return descendants


def evaluate_status(
    *,
    total_count: int,
    stale_count: int,
    warn_total_count: int,
    threshold_total_count: int,
    warn_stale_count: int,
    threshold_stale_count: int,
) -> str:
    if total_count >= threshold_total_count or stale_count >= threshold_stale_count:
        return "threshold_exceeded"
    if total_count >= warn_total_count or stale_count >= warn_stale_count:
        return "warning"
    return "ok"


def build_snapshot(
    process_rows: list[dict[str, Any]],
    now_ts: int,
    *,
    requested_host_pid: int | None = None,
    stale_age_seconds: int = DEFAULT_STALE_AGE_SECONDS,
    warn_total_count: int = DEFAULT_WARN_TOTAL_COUNT,
    threshold_total_count: int = DEFAULT_THRESHOLD_TOTAL_COUNT,
    warn_stale_count: int = DEFAULT_WARN_STALE_COUNT,
    threshold_stale_count: int = DEFAULT_THRESHOLD_STALE_COUNT,
    proxy_mode_lookup: Callable[[int], str | None] | None = None,
) -> dict[str, Any]:
    host_pid, host_candidates = pick_host_pid(process_rows, requested_host_pid=requested_host_pid)
    descendants = descendant_pid_set(process_rows, host_pid)

    host_row = next((row for row in process_rows if int(row["pid"]) == host_pid), None)
    targets: list[ManagedChild] = []
    outside_host_total = 0

    counts = {
        "openspace_main": 0,
        "openspace_evolution": 0,
        "computer_use_mcp": 0,
        "targets_total": 0,
    }
    stale = {
        "openspace_main": 0,
        "openspace_evolution": 0,
        "computer_use_mcp": 0,
        "total": 0,
        "oldest_age_seconds": 0,
    }

    for row in process_rows:
        kind = classify_command(str(row["command"]))
        if kind not in {"openspace_main", "openspace_evolution", "computer_use_mcp"}:
            continue

        pid = int(row["pid"])
        age_seconds = parse_elapsed_seconds(str(row["etime"]))
        is_descendant = pid in descendants
        if not is_descendant:
            outside_host_total += 1
            continue

        mode = proxy_mode_lookup(pid) if proxy_mode_lookup and kind.startswith("openspace_") else None
        child = ManagedChild(
            pid=pid,
            ppid=int(row["ppid"]),
            kind=kind,
            age_seconds=age_seconds,
            command=str(row["command"]),
            is_descendant=is_descendant,
            mode=mode,
        )
        targets.append(child)
        counts[kind] += 1
        counts["targets_total"] += 1
        stale["oldest_age_seconds"] = max(stale["oldest_age_seconds"], age_seconds)
        if age_seconds >= stale_age_seconds:
            stale[kind] += 1
            stale["total"] += 1

    status = evaluate_status(
        total_count=counts["targets_total"],
        stale_count=stale["total"],
        warn_total_count=warn_total_count,
        threshold_total_count=threshold_total_count,
        warn_stale_count=warn_stale_count,
        threshold_stale_count=threshold_stale_count,
    )

    if host_pid is None:
        assessment = "no_codex_host_found"
    elif counts["targets_total"] == 0:
        assessment = "no_target_processes"
    elif stale["total"] > 0:
        assessment = "host_lifecycle_leak_suspected"
    elif status in {"warning", "threshold_exceeded"}:
        assessment = "high_live_session_footprint"
    else:
        assessment = "within_threshold"

    return {
        "sampled_at": now_ts,
        "status": status,
        "assessment": assessment,
        "host": {
            "pid": host_pid,
            "command": host_row["command"] if host_row else None,
            "candidate_pids": host_candidates,
        },
        "thresholds": {
            "stale_age_seconds": stale_age_seconds,
            "warn_total_count": warn_total_count,
            "threshold_total_count": threshold_total_count,
            "warn_stale_count": warn_stale_count,
            "threshold_stale_count": threshold_stale_count,
        },
        "counts": counts,
        "stale": stale,
        "outside_host_total": outside_host_total,
        "targets": [asdict(item) for item in sorted(targets, key=lambda item: (-item.age_seconds, item.pid))],
    }


def select_cleanup_candidates(
    candidates: list[ManagedChild],
    *,
    age_threshold_seconds: int,
    include_kinds: set[str],
) -> list[ManagedChild]:
    return sorted(
        [
            item
            for item in candidates
            if item.is_descendant and item.kind in include_kinds and item.age_seconds >= age_threshold_seconds
        ],
        key=lambda item: (-item.age_seconds, item.pid),
    )


def record_snapshot(guard_dir: Path, snapshot: dict[str, Any], *, event_type: str) -> None:
    guard_dir.mkdir(parents=True, exist_ok=True)
    state_path = guard_dir / "state.json"
    events_path = guard_dir / "events.jsonl"
    samples_path = guard_dir / "samples.jsonl"

    state_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    event = {
        "ts": int(time.time()),
        "type": event_type,
        "status": snapshot.get("status"),
        "assessment": snapshot.get("assessment"),
        "counts": snapshot.get("counts", {}),
        "stale": snapshot.get("stale", {}),
    }
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    with samples_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")


def terminate_process(pid: int, timeout_seconds: float) -> str:
    try:
        os.kill(pid, 0)
    except OSError:
        return "already-exited"

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return "terminated"
        time.sleep(0.1)

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return "terminated"
    return "killed"


def current_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    return build_snapshot(
        collect_process_rows(),
        int(time.time()),
        requested_host_pid=args.host_pid,
        stale_age_seconds=args.stale_age_seconds,
        warn_total_count=args.warn_total_count,
        threshold_total_count=args.threshold_total_count,
        warn_stale_count=args.warn_stale_count,
        threshold_stale_count=args.threshold_stale_count,
        proxy_mode_lookup=proxy_mode_for_pid,
    )


def read_state(guard_dir: Path) -> dict[str, Any] | None:
    state_path = guard_dir / "state.json"
    if not state_path.is_file():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def render_text(snapshot: dict[str, Any]) -> str:
    counts = snapshot["counts"]
    stale = snapshot["stale"]
    host = snapshot["host"]
    lines = [
        "Codex MCP Guard",
        f"status: {snapshot['status']}",
        f"assessment: {snapshot['assessment']}",
        f"host pid: {host.get('pid')}",
        f"openspace main: {counts['openspace_main']}",
        f"openspace evolution: {counts['openspace_evolution']}",
        f"computer use mcp: {counts['computer_use_mcp']}",
        f"targets total: {counts['targets_total']}",
        f"stale total: {stale['total']}",
        f"oldest age seconds: {stale['oldest_age_seconds']}",
        f"outside host total: {snapshot['outside_host_total']}",
    ]
    return "\n".join(lines)


def print_snapshot(snapshot: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(render_text(snapshot))


def command_status(args: argparse.Namespace) -> int:
    guard_dir = Path(args.state_dir).expanduser().resolve()
    snapshot = read_state(guard_dir)
    if snapshot is None:
        snapshot = current_snapshot(args)
        record_snapshot(guard_dir, snapshot, event_type="status-bootstrap")
    print_snapshot(snapshot, as_json=args.json)
    return 0


def command_check(args: argparse.Namespace) -> int:
    guard_dir = Path(args.state_dir).expanduser().resolve()
    snapshot = current_snapshot(args)
    record_snapshot(guard_dir, snapshot, event_type="check")
    print_snapshot(snapshot, as_json=args.json)
    return 0


def command_clean(args: argparse.Namespace) -> int:
    guard_dir = Path(args.state_dir).expanduser().resolve()
    snapshot = current_snapshot(args)
    candidates = select_cleanup_candidates(
        [ManagedChild(**item) for item in snapshot["targets"]],
        age_threshold_seconds=args.stale_age_seconds,
        include_kinds={"openspace_main", "openspace_evolution", "computer_use_mcp"},
    )

    cleanup_allowed = snapshot["status"] == "threshold_exceeded" or args.force
    actions: list[dict[str, Any]] = []
    if cleanup_allowed:
        for item in candidates:
            action = "would-terminate" if args.dry_run else terminate_process(item.pid, args.timeout_seconds)
            actions.append({"pid": item.pid, "kind": item.kind, "action": action})

    result = {
        "cleanup_allowed": cleanup_allowed,
        "dry_run": args.dry_run,
        "candidates": [asdict(item) for item in candidates],
        "actions": actions,
        "snapshot": snapshot,
    }
    record_snapshot(guard_dir, snapshot, event_type="clean")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(snapshot))
        print(f"\ncleanup allowed: {'yes' if cleanup_allowed else 'no'}")
        if not cleanup_allowed:
            print("no cleanup performed because thresholds were not exceeded; use --force to override.")
        elif not candidates:
            print("no cleanup candidates matched the allowlist and age threshold.")
        else:
            for item in actions:
                print(f"- pid={item['pid']} kind={item['kind']} action={item['action']}")
    return 0


def command_tail(args: argparse.Namespace) -> int:
    events_path = Path(args.state_dir).expanduser().resolve() / "events.jsonl"
    if not events_path.is_file():
        print("No events recorded yet.")
        return 0
    lines = events_path.read_text(encoding="utf-8").splitlines()
    for line in lines[-args.limit :]:
        print(line)
    return 0


def command_daemon(args: argparse.Namespace) -> int:
    guard_dir = Path(args.state_dir).expanduser().resolve()
    iteration = 0
    while True:
        snapshot = current_snapshot(args)
        record_snapshot(guard_dir, snapshot, event_type="daemon")
        iteration += 1
        if args.iterations and iteration >= args.iterations:
            print_snapshot(snapshot, as_json=args.json)
            return 0
        time.sleep(args.interval_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Diagnose and clean stale Codex Desktop MCP child processes")
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--state-dir", default=str(default_guard_dir()))
    shared.add_argument("--host-pid", type=int, default=None)
    shared.add_argument("--stale-age-seconds", type=int, default=DEFAULT_STALE_AGE_SECONDS)
    shared.add_argument("--warn-total-count", type=int, default=DEFAULT_WARN_TOTAL_COUNT)
    shared.add_argument("--threshold-total-count", type=int, default=DEFAULT_THRESHOLD_TOTAL_COUNT)
    shared.add_argument("--warn-stale-count", type=int, default=DEFAULT_WARN_STALE_COUNT)
    shared.add_argument("--threshold-stale-count", type=int, default=DEFAULT_THRESHOLD_STALE_COUNT)
    shared.add_argument("--json", action="store_true")

    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = True

    subparsers.add_parser("status", parents=[shared])
    subparsers.add_parser("check", parents=[shared])

    clean_parser = subparsers.add_parser("clean", parents=[shared])
    clean_parser.add_argument("--dry-run", action="store_true")
    clean_parser.add_argument("--force", action="store_true")
    clean_parser.add_argument("--timeout-seconds", type=float, default=3.0)

    tail_parser = subparsers.add_parser("tail", parents=[shared])
    tail_parser.add_argument("--limit", type=int, default=DEFAULT_TAIL_LIMIT)

    daemon_parser = subparsers.add_parser("daemon", parents=[shared])
    daemon_parser.add_argument("--interval-seconds", type=int, default=30)
    daemon_parser.add_argument("--iterations", type=int, default=0)

    subparsers.add_parser("help")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "help":
        parser.print_help(sys.stdout)
        return 0
    if args.command == "status":
        return command_status(args)
    if args.command == "check":
        return command_check(args)
    if args.command == "clean":
        return command_clean(args)
    if args.command == "tail":
        return command_tail(args)
    if args.command == "daemon":
        return command_daemon(args)
    parser.error(f"Unknown command: {args.command}")
    return 2


__all__ = [
    "ManagedChild",
    "build_snapshot",
    "build_parser",
    "classify_command",
    "default_guard_dir",
    "main",
    "parse_elapsed_seconds",
    "record_snapshot",
    "select_cleanup_candidates",
]
