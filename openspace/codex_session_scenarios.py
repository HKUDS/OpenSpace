from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


OPENING_PREFIX = "先做一次 OpenSpace 预检，再开始当前任务。"
SESSION_STATUS_RE = re.compile(r"OpenSpace session:\s*(\S+)")
MACHINE_STATUS_RE = re.compile(r"OpenSpace machine:\s*(\S+)")
FALLBACK_LINE = "当前线程不依赖 OpenSpace 自动沉淀；我会先按本地文档、脚本或手动收尾路径继续。"


@dataclass(frozen=True)
class OpeningStatus:
    session_status: str
    machine_status: str
    fallback_present: bool
    text: str


@dataclass(frozen=True)
class ExecOutput:
    thread_id: str | None
    events: list[dict[str, Any]]
    agent_messages: list[str]
    opening: OpeningStatus | None


@dataclass(frozen=True)
class SessionFamily:
    thread_ids: set[str]
    session_files: set[Path]


@dataclass(frozen=True)
class RunResult:
    label: str
    exit_code: int
    thread_id: str | None
    opening: dict[str, Any] | None
    agent_messages: list[str]
    mcp_tool_calls: list[dict[str, Any]]
    session_files: list[str]
    thread_ids: list[str]
    stdout_path: str
    stderr_path: str


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    ok: bool
    details: dict[str, Any]


def _iter_json_events(output: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _extract_agent_message(event: dict[str, Any]) -> str | None:
    if event.get("type") != "item.completed":
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    if item.get("type") != "agent_message":
        return None
    text = item.get("text")
    return text if isinstance(text, str) else None


def _extract_mcp_tool_call(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("type") != "item.completed":
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    if item.get("type") != "mcp_tool_call":
        return None
    return {
        "server": item.get("server"),
        "tool": item.get("tool"),
        "status": item.get("status"),
        "error": item.get("error"),
    }


def _parse_opening(text: str) -> OpeningStatus | None:
    if not text.startswith(OPENING_PREFIX):
        return None
    session_match = SESSION_STATUS_RE.search(text)
    machine_match = MACHINE_STATUS_RE.search(text)
    if session_match is None or machine_match is None:
        return None
    return OpeningStatus(
        session_status=session_match.group(1),
        machine_status=machine_match.group(1),
        fallback_present=FALLBACK_LINE in text,
        text=text,
    )


def parse_exec_output(output: str) -> ExecOutput:
    events = _iter_json_events(output)
    thread_id: str | None = None
    agent_messages: list[str] = []
    opening: OpeningStatus | None = None

    for event in events:
        if event.get("type") == "thread.started" and thread_id is None:
            candidate = event.get("thread_id")
            if isinstance(candidate, str):
                thread_id = candidate
        agent_message = _extract_agent_message(event)
        if agent_message is not None:
            agent_messages.append(agent_message)
            if opening is None:
                opening = _parse_opening(agent_message)

    return ExecOutput(
        thread_id=thread_id,
        events=events,
        agent_messages=agent_messages,
        opening=opening,
    )


def _read_session_meta(path: Path) -> dict[str, Any] | None:
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
    except Exception:
        return None
    try:
        payload = json.loads(first_line)
    except json.JSONDecodeError:
        return None
    if payload.get("type") != "session_meta":
        return None
    session_payload = payload.get("payload")
    return session_payload if isinstance(session_payload, dict) else None


def collect_session_family(thread_id: str, sessions_root: Path) -> SessionFamily:
    metas: list[tuple[Path, dict[str, Any]]] = []
    for path in sessions_root.glob("**/*.jsonl"):
        meta = _read_session_meta(path)
        if meta is not None:
            metas.append((path, meta))

    thread_ids: set[str] = {thread_id}
    session_files: set[Path] = set()
    changed = True

    while changed:
        changed = False
        for path, meta in metas:
            current_id = meta.get("id")
            if isinstance(current_id, str) and current_id in thread_ids:
                if path not in session_files:
                    session_files.add(path)
                    changed = True

            source = meta.get("source")
            if not isinstance(source, dict):
                continue
            subagent = source.get("subagent")
            if not isinstance(subagent, dict):
                continue
            thread_spawn = subagent.get("thread_spawn")
            if not isinstance(thread_spawn, dict):
                continue
            parent_thread_id = thread_spawn.get("parent_thread_id")
            if (
                isinstance(parent_thread_id, str)
                and parent_thread_id in thread_ids
                and isinstance(current_id, str)
                and current_id not in thread_ids
            ):
                thread_ids.add(current_id)
                session_files.add(path)
                changed = True

    return SessionFamily(thread_ids=thread_ids, session_files=session_files)


def cleanup_session_artifacts(
    *,
    thread_ids: set[str],
    session_files: set[Path],
    session_index_path: Path,
) -> None:
    for path in session_files:
        path.unlink(missing_ok=True)

    if not session_index_path.exists():
        return

    kept_lines: list[str] = []
    for line in session_index_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            kept_lines.append(line)
            continue

        payload_thread_id = payload.get("thread_id")
        payload_session_file = payload.get("session_file")
        if payload_thread_id in thread_ids:
            continue
        if isinstance(payload_session_file, str) and Path(payload_session_file) in session_files:
            continue
        kept_lines.append(line)

    new_content = "\n".join(kept_lines)
    if new_content:
        new_content += "\n"
    session_index_path.write_text(new_content, encoding="utf-8")


def snapshot_daemons(state_dir: Path, workspace: str) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    if not state_dir.exists():
        return snapshot

    for path in state_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("workspace") != workspace:
            continue
        server_kind = payload.get("server_kind")
        if not isinstance(server_kind, str):
            continue
        snapshot[server_kind] = payload
    return snapshot


def kill_daemons_in_state_dir(state_dir: Path) -> None:
    if not state_dir.exists():
        return
    for path in state_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = payload.get("pid")
        if not isinstance(pid, int):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
        else:
            deadline = time.time() + 2.0
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, AttributeError):
                    pass
    shutil.rmtree(state_dir, ignore_errors=True)


def run_exec_session(
    *,
    label: str,
    prompt: str,
    cwd: Path,
    extra_configs: list[str] | None = None,
    timeout_seconds: int = 180,
    codex_binary: str = "codex",
    sessions_root: Path | None = None,
    session_index_path: Path | None = None,
    output_dir: Path | None = None,
) -> RunResult:
    sessions_root = sessions_root or (Path.home() / ".codex" / "sessions")
    session_index_path = session_index_path or (Path.home() / ".codex" / "session_index.jsonl")
    output_dir = output_dir or Path(tempfile.mkdtemp(prefix="openspace-codex-session-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = output_dir / f"{label}.stdout.log"
    stderr_path = output_dir / f"{label}.stderr.log"

    command = [
        codex_binary,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        str(cwd),
    ]
    for item in extra_configs or []:
        command.extend(["-c", item])
    command.append(prompt)

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")

    parsed = parse_exec_output(completed.stdout)
    session_family = (
        collect_session_family(parsed.thread_id, sessions_root)
        if parsed.thread_id
        else SessionFamily(thread_ids=set(), session_files=set())
    )
    mcp_tool_calls = [
        call
        for event in parsed.events
        if (call := _extract_mcp_tool_call(event)) is not None
    ]

    return RunResult(
        label=label,
        exit_code=completed.returncode,
        thread_id=parsed.thread_id,
        opening=asdict(parsed.opening) if parsed.opening else None,
        agent_messages=parsed.agent_messages,
        mcp_tool_calls=mcp_tool_calls,
        session_files=sorted(str(path) for path in session_family.session_files),
        thread_ids=sorted(session_family.thread_ids),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )


def _session_env_overrides(state_dir: Path, workspace: Path) -> list[str]:
    return [
        f'mcp_servers.openspace.env.OPENSPACE_MCP_DAEMON_STATE_DIR="{state_dir}"',
        f'mcp_servers.openspace_evolution.env.OPENSPACE_MCP_DAEMON_STATE_DIR="{state_dir}"',
        'mcp_servers.openspace.env.OPENSPACE_MCP_PROXY_MODE="daemon"',
        'mcp_servers.openspace_evolution.env.OPENSPACE_MCP_PROXY_MODE="daemon"',
        f'mcp_servers.openspace.env.OPENSPACE_WORKSPACE="{workspace}"',
        f'mcp_servers.openspace_evolution.env.OPENSPACE_WORKSPACE="{workspace}"',
    ]


def _assert(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def _scenario_cold_start(base_dir: Path, cwd: Path) -> ScenarioResult:
    state_dir = base_dir / "cold-start-state"
    run = run_exec_session(
        label="cold-start",
        prompt=(
            "这是 OpenSpace 冷启动预检测试。严格按 TMP 的 AGENTS.md 做开场预检，"
            "然后只输出一句“cold-start done”。不要修改任何文件，也不要使用子代理。"
        ),
        cwd=cwd,
        extra_configs=_session_env_overrides(state_dir, cwd),
        output_dir=base_dir / "cold-start-output",
    )

    errors: list[str] = []
    opening = run.opening or {}
    _assert(run.exit_code == 0, f"cold-start exit_code={run.exit_code}", errors)
    _assert(opening.get("session_status") == "ready", f"cold-start session_status={opening.get('session_status')}", errors)
    _assert(opening.get("machine_status") == "ready", f"cold-start machine_status={opening.get('machine_status')}", errors)
    _assert(opening.get("fallback_present") is False, "cold-start unexpectedly printed fallback line", errors)
    _assert(run.agent_messages[-1:] == ["cold-start done"], f"cold-start final message={run.agent_messages[-1:]}", errors)

    return ScenarioResult(
        name="cold-start-preflight",
        ok=not errors,
        details={"errors": errors, "run": asdict(run), "state_dirs": [str(state_dir)]},
    )


def _scenario_warm_reuse(base_dir: Path, cwd: Path) -> ScenarioResult:
    state_dir = base_dir / "warm-reuse-state"
    extra_configs = _session_env_overrides(state_dir, cwd)
    prompt = (
        "这是 OpenSpace warm-session reuse 测试。先完成 TMP 的开场预检，"
        "然后调用 openspace 的 search_skills 工具，参数用 query='OpenSpace MCP 健康检查'、"
        "source='local'、limit=1、auto_import=false。最后只输出一句“warm-session done”。"
        "不要修改任何文件，也不要使用子代理。"
    )

    first = run_exec_session(
        label="warm-reuse-first",
        prompt=prompt,
        cwd=cwd,
        extra_configs=extra_configs,
        output_dir=base_dir / "warm-reuse-output",
    )
    first_snapshot = snapshot_daemons(state_dir, str(cwd))

    second = run_exec_session(
        label="warm-reuse-second",
        prompt=prompt,
        cwd=cwd,
        extra_configs=extra_configs,
        output_dir=base_dir / "warm-reuse-output",
    )
    second_snapshot = snapshot_daemons(state_dir, str(cwd))

    errors: list[str] = []
    for label, run in (("first", first), ("second", second)):
        opening = run.opening or {}
        _assert(run.exit_code == 0, f"warm-reuse {label} exit_code={run.exit_code}", errors)
        _assert(opening.get("session_status") == "ready", f"warm-reuse {label} session_status={opening.get('session_status')}", errors)
        _assert(any(call.get("server") == "openspace" and call.get("tool") == "search_skills" for call in run.mcp_tool_calls), f"warm-reuse {label} missing openspace.search_skills call", errors)
        _assert(run.agent_messages[-1:] == ["warm-session done"], f"warm-reuse {label} final message={run.agent_messages[-1:]}", errors)

    _assert("main" in first_snapshot, "warm-reuse first run did not create main daemon metadata", errors)
    _assert("main" in second_snapshot, "warm-reuse second run did not create main daemon metadata", errors)
    if "main" in first_snapshot and "main" in second_snapshot:
        _assert(
            first_snapshot["main"].get("pid") == second_snapshot["main"].get("pid"),
            f"warm-reuse main pid changed: {first_snapshot['main'].get('pid')} -> {second_snapshot['main'].get('pid')}",
            errors,
        )
        _assert(
            first_snapshot["main"].get("port") == second_snapshot["main"].get("port"),
            f"warm-reuse main port changed: {first_snapshot['main'].get('port')} -> {second_snapshot['main'].get('port')}",
            errors,
        )

    return ScenarioResult(
        name="warm-session-reuse",
        ok=not errors,
        details={
            "errors": errors,
            "first": asdict(first),
            "second": asdict(second),
            "first_snapshot": first_snapshot,
            "second_snapshot": second_snapshot,
            "state_dirs": [str(state_dir)],
        },
    )


def _scenario_unhealthy_fallback(base_dir: Path, cwd: Path) -> ScenarioResult:
    state_dir = base_dir / "unhealthy-state"
    missing_command = base_dir / "missing-openspace-command"
    run = run_exec_session(
        label="unhealthy-fallback",
        prompt=(
            "这是 OpenSpace unhealthy-session fallback 测试。严格按 TMP 的 AGENTS.md 做开场预检，"
            "然后只输出一句“unhealthy-session done”。不要修改任何文件，也不要使用子代理。"
        ),
        cwd=cwd,
        extra_configs=_session_env_overrides(state_dir, cwd)
        + [
            f'mcp_servers.openspace.command="{missing_command}"',
            f'mcp_servers.openspace_evolution.command="{missing_command}"',
        ],
        output_dir=base_dir / "unhealthy-output",
    )

    errors: list[str] = []
    opening = run.opening or {}
    _assert(run.exit_code == 0, f"unhealthy fallback exit_code={run.exit_code}", errors)
    _assert(
        opening.get("session_status") in {"exposed-but-unhealthy", "unknown"},
        f"unhealthy fallback session_status={opening.get('session_status')}",
        errors,
    )
    _assert(opening.get("machine_status") == "ready", f"unhealthy fallback machine_status={opening.get('machine_status')}", errors)
    _assert(opening.get("fallback_present") is True, "unhealthy fallback missing fallback line", errors)
    _assert(run.agent_messages[-1:] == ["unhealthy-session done"], f"unhealthy fallback final message={run.agent_messages[-1:]}", errors)

    return ScenarioResult(
        name="unhealthy-session-fallback",
        ok=not errors,
        details={"errors": errors, "run": asdict(run), "state_dirs": [str(state_dir)]},
    )


def _scenario_agent_team(base_dir: Path, cwd: Path) -> ScenarioResult:
    state_dir = base_dir / "agent-team-state"
    run = run_exec_session(
        label="agent-team",
        prompt=(
            "这是 OpenSpace agent-team 测试。先完成 TMP 的开场预检，然后使用 agent team，"
            "至少启动两个只读子代理，分别检查 TMP 仓库里的 AGENTS.md 和 "
            "scripts/check_openspace_mcp_preflight.py 与 OpenSpace 预检相关的内容。"
            "父线程最后只输出一句“agent-team done”。不要修改任何文件。"
        ),
        cwd=cwd,
        extra_configs=_session_env_overrides(state_dir, cwd),
        timeout_seconds=240,
        output_dir=base_dir / "agent-team-output",
    )

    errors: list[str] = []
    opening = run.opening or {}
    _assert(run.exit_code == 0, f"agent-team exit_code={run.exit_code}", errors)
    _assert(opening.get("session_status") == "ready", f"agent-team session_status={opening.get('session_status')}", errors)
    _assert(len(run.thread_ids) >= 3, f"agent-team expected parent + >=2 child threads, got {run.thread_ids}", errors)
    _assert(run.agent_messages[-1:] == ["agent-team done"], f"agent-team final message={run.agent_messages[-1:]}", errors)

    return ScenarioResult(
        name="agent-team-split",
        ok=not errors,
        details={"errors": errors, "run": asdict(run), "state_dirs": [str(state_dir)]},
    )


def _cleanup_run_artifacts(result: ScenarioResult, session_index_path: Path) -> None:
    details = result.details
    candidate_runs: list[dict[str, Any]] = []
    if "run" in details and isinstance(details["run"], dict):
        candidate_runs.append(details["run"])
    for key in ("first", "second"):
        if key in details and isinstance(details[key], dict):
            candidate_runs.append(details[key])

    session_files: set[Path] = set()
    thread_ids: set[str] = set()
    for run in candidate_runs:
        for item in run.get("session_files", []):
            session_files.add(Path(item))
        for item in run.get("thread_ids", []):
            thread_ids.add(item)

    cleanup_session_artifacts(
        thread_ids=thread_ids,
        session_files=session_files,
        session_index_path=session_index_path,
    )


def run_scenarios(*, cwd: Path, cleanup: bool = True) -> dict[str, Any]:
    base_dir = Path(tempfile.mkdtemp(prefix="openspace-codex-scenarios-"))
    session_index_path = Path.home() / ".codex" / "session_index.jsonl"
    started_at = time.time()

    scenario_steps = [
        ("cold-start-preflight", _scenario_cold_start),
        ("warm-session-reuse", _scenario_warm_reuse),
        ("unhealthy-session-fallback", _scenario_unhealthy_fallback),
        ("agent-team-split", _scenario_agent_team),
    ]
    results: list[ScenarioResult] = []

    try:
        for scenario_name, scenario_fn in scenario_steps:
            try:
                results.append(scenario_fn(base_dir, cwd))
            except Exception as exc:  # noqa: BLE001
                results.append(
                    ScenarioResult(
                        name=scenario_name,
                        ok=False,
                        details={"errors": [f"{type(exc).__name__}: {exc}"]},
                    )
                )
    finally:
        if cleanup:
            for result in results:
                _cleanup_run_artifacts(result, session_index_path)
            for state_dir in base_dir.glob("*-state"):
                kill_daemons_in_state_dir(state_dir)
            shutil.rmtree(base_dir, ignore_errors=True)

    return {
        "cwd": str(cwd),
        "started_at": started_at,
        "cleanup": cleanup,
        "all_ok": all(item.ok for item in results),
        "results": [asdict(item) for item in results],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real Codex OpenSpace session scenarios")
    parser.add_argument(
        "--cwd",
        type=Path,
        default=Path("/Users/admin/PycharmProjects/TMP"),
        help="Working directory for real session scenarios",
    )
    parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="Keep generated session files, daemon state, and command logs",
    )
    args = parser.parse_args()

    summary = run_scenarios(cwd=args.cwd.resolve(), cleanup=not args.keep_artifacts)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
