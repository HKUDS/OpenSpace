from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from openspace.codex_session_scenarios import (
    cleanup_session_artifacts,
    collect_session_family,
    parse_exec_output,
)


SERVER_SPECS: dict[str, dict[str, str]] = {
    "openspace": {
        "kind": "main",
        "expected_tool": "search_skills",
    },
    "openspace_evolution": {
        "kind": "evolution",
        "expected_tool": "evolve_from_context",
    },
}


def canonical_workspace(cwd: Path) -> Path:
    workspace = cwd.expanduser().resolve()
    proc = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip()).resolve()
    return workspace


def _resolve_command(command: str | None, *, config_path: Path) -> tuple[str | None, bool]:
    if not command:
        return None, False

    candidate = command.strip()
    if not candidate:
        return None, False

    if os.path.isabs(candidate):
        path = Path(candidate).expanduser().resolve()
    elif "/" in candidate:
        path = (config_path.parent / candidate).expanduser().resolve()
    else:
        resolved = shutil.which(candidate)
        path = Path(resolved).resolve() if resolved else None

    if path is None:
        return None, False
    return str(path), os.access(path, os.X_OK)


def _load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        return {}
    return tomllib.loads(config_path.read_text(encoding="utf-8"))


def _daemon_state_dir(codex_home: Path) -> Path:
    override = os.environ.get("OPENSPACE_MCP_DAEMON_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (codex_home / "state" / "openspace").resolve()


def _read_daemon_record(
    *,
    state_dir: Path,
    server_kind: str,
    workspace: Path,
) -> dict[str, Any] | None:
    if not state_dir.is_dir():
        return None

    for path in sorted(state_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("server_kind") != server_kind:
            continue
        if payload.get("workspace") != str(workspace):
            continue
        return {
            "path": str(path),
            "present": True,
            "ready": bool(payload.get("ready")),
            "warmed": bool(payload.get("warmed")),
            "pid": payload.get("pid"),
            "port": payload.get("port"),
        }
    return None


def _machine_status(server_statuses: list[str]) -> str:
    if server_statuses and all(status == "ready" for status in server_statuses):
        return "ready"
    if any(status == "ready" for status in server_statuses):
        return "partial"
    if any(status == "broken" for status in server_statuses):
        return "partial"
    return "missing"


def inspect_machine(
    *,
    cwd: Path,
    config_path: Path | None = None,
    codex_home: Path | None = None,
) -> dict[str, Any]:
    workspace = canonical_workspace(cwd)
    codex_home = (codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))).expanduser().resolve()
    config_path = (config_path or (codex_home / "config.toml")).expanduser().resolve()
    config = _load_config(config_path)
    servers_cfg = config.get("mcp_servers", {}) if isinstance(config, dict) else {}
    projects_cfg = config.get("projects", {}) if isinstance(config, dict) else {}
    project_entry = projects_cfg.get(str(workspace), {}) if isinstance(projects_cfg, dict) else {}
    trust_level = project_entry.get("trust_level") if isinstance(project_entry, dict) else None
    state_dir = _daemon_state_dir(codex_home)

    server_reports: dict[str, dict[str, Any]] = {}
    server_statuses: list[str] = []

    for server_name, spec in SERVER_SPECS.items():
        server_cfg = servers_cfg.get(server_name, {}) if isinstance(servers_cfg, dict) else {}
        configured = isinstance(server_cfg, dict) and bool(server_cfg)
        command = server_cfg.get("command") if configured else None
        resolved_command, executable = _resolve_command(command, config_path=config_path)
        daemon = _read_daemon_record(
            state_dir=state_dir,
            server_kind=spec["kind"],
            workspace=workspace,
        ) or {
            "path": None,
            "present": False,
            "ready": False,
            "warmed": False,
            "pid": None,
            "port": None,
        }

        if not configured:
            status = "missing"
        elif not resolved_command or not executable:
            status = "broken"
        else:
            status = "ready"

        server_statuses.append(status)
        server_reports[server_name] = {
            "status": status,
            "configured": configured,
            "command": command,
            "resolved_command": resolved_command,
            "executable": executable,
            "daemon": daemon,
        }

    repo_python = workspace / ".venv" / "bin" / "python"
    return {
        "status": _machine_status(server_statuses),
        "workspace": str(workspace),
        "codex_home": str(codex_home),
        "config_path": str(config_path),
        "project_trusted": trust_level == "trusted" if trust_level is not None else None,
        "repo_python": {
            "path": str(repo_python),
            "exists": repo_python.exists(),
            "executable": os.access(repo_python, os.X_OK),
        },
        "servers": server_reports,
    }


def summarize_session_probe(
    *,
    exit_code: int | None,
    mcp_tool_calls: list[dict[str, Any]],
    error: str | None = None,
) -> dict[str, Any]:
    server_reports: dict[str, dict[str, Any]] = {}

    for server_name, spec in SERVER_SPECS.items():
        matches = [
            call for call in mcp_tool_calls
            if call.get("server") == server_name and call.get("tool") == spec["expected_tool"]
        ]
        server_reports[server_name] = {
            "observed": bool(matches),
            "calls": matches,
        }

    observed_count = sum(1 for item in server_reports.values() if item["observed"])
    if error:
        status = "probe-failed"
    elif observed_count == len(SERVER_SPECS) and exit_code == 0:
        status = "ready"
    elif observed_count:
        status = "partial"
    elif exit_code == 0:
        status = "missing"
    else:
        status = "probe-failed"

    return {
        "status": status,
        "exit_code": exit_code,
        "error": error,
        "servers": server_reports,
        "mcp_tool_calls": mcp_tool_calls,
    }


def _extract_mcp_tool_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
            continue
        calls.append(
            {
                "server": item.get("server"),
                "tool": item.get("tool"),
                "status": item.get("status"),
                "error": item.get("error"),
            }
        )
    return calls


def _cleanup_probe_artifacts(thread_id: str | None, session_index_path: Path) -> list[str]:
    if not thread_id:
        return []
    family = collect_session_family(thread_id, session_index_path.parent / "sessions")
    cleanup_session_artifacts(
        thread_ids=family.thread_ids,
        session_files=family.session_files,
        session_index_path=session_index_path,
    )
    return sorted(str(path) for path in family.session_files)


def _session_probe_prompt(workspace: Path) -> str:
    file_paths = [str(workspace / "scripts" / "check_openspace_mcp_preflight.py")]
    return (
        "这是 OpenSpace MCP health check。不要修改任何文件，也不要使用子代理。"
        "先调用 openspace 的 search_skills 工具，参数用 "
        "query='OpenSpace health check'、source='local'、limit=1、auto_import=false。"
        "再调用 openspace_evolution 的 evolve_from_context 工具，参数用 "
        f"task='OpenSpace session probe'、summary='Zero-capture smoke test. Do not modify code. Do not create skills.'、"
        f"workspace_dir='{workspace}'、max_skills=0、file_paths={file_paths}。"
        "最后只输出一句“openspace-preflight done”。"
    )


def probe_session(
    *,
    cwd: Path,
    codex_home: Path | None = None,
    codex_binary: str = "codex",
    timeout_seconds: int = 180,
    keep_artifacts: bool = False,
) -> dict[str, Any]:
    workspace = canonical_workspace(cwd)
    codex_home = (codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))).expanduser().resolve()
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    output_dir = Path(tempfile.mkdtemp(prefix="openspace-mcp-preflight-"))
    stdout_path = output_dir / "session.stdout.log"
    stderr_path = output_dir / "session.stderr.log"
    session_index_path = codex_home / "session_index.jsonl"

    command_path = shutil.which(codex_binary) if os.sep not in codex_binary else codex_binary
    if not command_path:
        return {
            "status": "probe-failed",
            "exit_code": None,
            "error": f"Unable to resolve codex binary: {codex_binary}",
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "session_files": [],
            "servers": {
                server_name: {"observed": False, "calls": []}
                for server_name in SERVER_SPECS
            },
            "mcp_tool_calls": [],
        }

    command = [
        command_path,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        str(workspace),
        _session_probe_prompt(workspace),
    ]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            env=env,
        )
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        parsed = parse_exec_output(completed.stdout)
        tool_calls = _extract_mcp_tool_calls(parsed.events)
        report = summarize_session_probe(exit_code=completed.returncode, mcp_tool_calls=tool_calls)
        report.update(
            {
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "thread_id": parsed.thread_id,
                "agent_messages": parsed.agent_messages,
                "session_files": [],
            }
        )
        if not keep_artifacts:
            _cleanup_probe_artifacts(parsed.thread_id, session_index_path)
            report["session_files"] = []
            report["stdout_path"] = None
            report["stderr_path"] = None
            shutil.rmtree(output_dir, ignore_errors=True)
        return report
    except subprocess.TimeoutExpired as exc:
        stdout_path.write_text(exc.stdout or "", encoding="utf-8")
        stderr_path.write_text(exc.stderr or "", encoding="utf-8")
        return {
            **summarize_session_probe(exit_code=None, mcp_tool_calls=[], error=f"Timed out after {timeout_seconds}s"),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "session_files": [],
        }


def build_report(
    *,
    cwd: Path,
    config_path: Path | None = None,
    codex_home: Path | None = None,
    probe_session_enabled: bool = False,
    codex_binary: str = "codex",
    timeout_seconds: int = 180,
    keep_artifacts: bool = False,
) -> dict[str, Any]:
    machine = inspect_machine(cwd=cwd, config_path=config_path, codex_home=codex_home)
    if probe_session_enabled:
        session = probe_session(
            cwd=Path(machine["workspace"]),
            codex_home=Path(machine["codex_home"]),
            codex_binary=codex_binary,
            timeout_seconds=timeout_seconds,
            keep_artifacts=keep_artifacts,
        )
    else:
        session = {
            "status": "not-probed",
            "exit_code": None,
            "error": None,
            "servers": {
                server_name: {"observed": False, "calls": []}
                for server_name in SERVER_SPECS
            },
            "mcp_tool_calls": [],
        }
    return {
        "workspace": machine["workspace"],
        "machine": machine,
        "session": session,
    }


def format_text_report(report: dict[str, Any]) -> str:
    machine = report["machine"]
    session = report["session"]
    lines = [
        "OpenSpace MCP preflight",
        f"Workspace: {report['workspace']}",
        f"Machine: {machine['status']}",
        f"Session: {session['status']}",
        "",
        "Machine details:",
        f"- config: {machine['config_path']}",
        f"- CODEX_HOME: {machine['codex_home']}",
        f"- project trusted: {machine['project_trusted']}",
        f"- repo python: {machine['repo_python']['path']} (exists={machine['repo_python']['exists']}, executable={machine['repo_python']['executable']})",
    ]
    for server_name, server in machine["servers"].items():
        daemon = server["daemon"]
        lines.append(
            f"- {server_name}: status={server['status']}, configured={server['configured']}, "
            f"resolved_command={server['resolved_command']}, daemon_present={daemon['present']}, daemon_ready={daemon['ready']}"
        )

    lines.extend(["", "Session details:"])
    if session["status"] == "not-probed":
        lines.append("- probe disabled; rerun with --probe-session to verify real Codex tool exposure")
    else:
        lines.append(f"- exit_code: {session['exit_code']}")
        lines.append(f"- error: {session['error']}")
        for server_name, server in session["servers"].items():
            lines.append(f"- {server_name}: observed={server['observed']}")
        if session.get("stdout_path"):
            lines.append(f"- stdout log: {session['stdout_path']}")
        if session.get("stderr_path"):
            lines.append(f"- stderr log: {session['stderr_path']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check OpenSpace MCP machine/session health")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Workspace or nested repo directory to inspect")
    parser.add_argument("--codex-home", type=Path, default=None, help="Override CODEX_HOME for config/session probing")
    parser.add_argument("--config-path", type=Path, default=None, help="Override config.toml path")
    parser.add_argument("--probe-session", action="store_true", help="Run a real Codex session smoke test")
    parser.add_argument("--codex-binary", default="codex", help="Codex CLI binary to use for session probes")
    parser.add_argument("--timeout-seconds", type=int, default=180, help="Timeout for the session probe")
    parser.add_argument("--keep-artifacts", action="store_true", help="Keep session logs and temporary artifacts")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args(argv)

    report = build_report(
        cwd=args.cwd.resolve(),
        config_path=args.config_path,
        codex_home=args.codex_home,
        probe_session_enabled=args.probe_session,
        codex_binary=args.codex_binary,
        timeout_seconds=args.timeout_seconds,
        keep_artifacts=args.keep_artifacts,
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_text_report(report))

    machine_ok = report["machine"]["status"] == "ready"
    session_status = report["session"]["status"]
    session_ok = session_status in {"ready", "not-probed"}
    return 0 if machine_ok and session_ok else 1


__all__ = [
    "build_report",
    "canonical_workspace",
    "format_text_report",
    "inspect_machine",
    "main",
    "probe_session",
    "summarize_session_probe",
]
