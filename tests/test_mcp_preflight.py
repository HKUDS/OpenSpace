from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

from openspace import mcp_preflight
from openspace.mcp_preflight import inspect_machine, probe_session, summarize_session_probe


def _write_executable(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_inspect_machine_reports_ready_for_both_servers(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    repo_python = workspace / ".venv" / "bin" / "python"
    repo_python.parent.mkdir(parents=True)
    _write_executable(repo_python)

    codex_home = tmp_path / ".codex"
    state_dir = codex_home / "state" / "openspace"
    state_dir.mkdir(parents=True)

    main_cmd = tmp_path / "openspace-global-mcp"
    evolution_cmd = tmp_path / "openspace-evolution-global-mcp"
    _write_executable(main_cmd)
    _write_executable(evolution_cmd)

    config_path = codex_home / "config.toml"
    config_path.write_text(
        f"""
[projects."{workspace.resolve()}"]
trust_level = "trusted"

[mcp_servers.openspace]
command = "{main_cmd}"
args = []

[mcp_servers.openspace_evolution]
command = "{evolution_cmd}"
args = []
""".strip()
        + "\n",
        encoding="utf-8",
    )

    (state_dir / "main-test.json").write_text(
        json.dumps(
            {
                "server_kind": "main",
                "workspace": str(workspace.resolve()),
                "ready": True,
                "warmed": True,
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "evolution-test.json").write_text(
        json.dumps(
            {
                "server_kind": "evolution",
                "workspace": str(workspace.resolve()),
                "ready": True,
                "warmed": False,
            }
        ),
        encoding="utf-8",
    )

    report = inspect_machine(cwd=workspace, config_path=config_path, codex_home=codex_home)

    assert report["status"] == "ready"
    assert report["project_trusted"] is True
    assert report["repo_python"]["exists"] is True
    assert report["servers"]["openspace"]["status"] == "ready"
    assert report["servers"]["openspace"]["daemon"]["present"] is True
    assert report["servers"]["openspace_evolution"]["status"] == "ready"
    assert report["servers"]["openspace_evolution"]["daemon"]["ready"] is True


def test_inspect_machine_reports_partial_when_server_is_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    repo_python = workspace / ".venv" / "bin" / "python"
    repo_python.parent.mkdir(parents=True)
    _write_executable(repo_python)

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()

    main_cmd = tmp_path / "openspace-global-mcp"
    _write_executable(main_cmd)

    config_path = codex_home / "config.toml"
    config_path.write_text(
        f"""
[mcp_servers.openspace]
command = "{main_cmd}"
args = []
""".strip()
        + "\n",
        encoding="utf-8",
    )

    report = inspect_machine(cwd=workspace, config_path=config_path, codex_home=codex_home)

    assert report["status"] == "partial"
    assert report["servers"]["openspace"]["status"] == "ready"
    assert report["servers"]["openspace_evolution"]["status"] == "missing"


def test_summarize_session_probe_reports_ready_when_both_tools_are_observed() -> None:
    report = summarize_session_probe(
        exit_code=0,
        mcp_tool_calls=[
            {"server": "openspace", "tool": "search_skills", "status": "completed"},
            {
                "server": "openspace_evolution",
                "tool": "evolve_from_context",
                "status": "completed",
            },
        ],
    )

    assert report["status"] == "ready"
    assert report["servers"]["openspace"]["observed"] is True
    assert report["servers"]["openspace_evolution"]["observed"] is True


def test_summarize_session_probe_reports_partial_when_only_one_tool_is_observed() -> None:
    report = summarize_session_probe(
        exit_code=0,
        mcp_tool_calls=[
            {"server": "openspace", "tool": "search_skills", "status": "completed"},
        ],
    )

    assert report["status"] == "partial"
    assert report["servers"]["openspace"]["observed"] is True
    assert report["servers"]["openspace_evolution"]["observed"] is False


def test_probe_session_omits_artifact_paths_after_cleanup(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    monkeypatch.setattr(mcp_preflight, "canonical_workspace", lambda cwd: workspace)
    monkeypatch.setattr(mcp_preflight.shutil, "which", lambda binary: "/usr/bin/codex")

    class _Completed:
        returncode = 0
        stdout = "{}\n"
        stderr = ""

    monkeypatch.setattr(mcp_preflight.subprocess, "run", lambda *args, **kwargs: _Completed())
    monkeypatch.setattr(
        mcp_preflight,
        "parse_exec_output",
        lambda output: SimpleNamespace(
            thread_id="thread-123",
            events=[
                {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "server": "openspace",
                        "tool": "search_skills",
                        "status": "completed",
                        "error": None,
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "server": "openspace_evolution",
                        "tool": "evolve_from_context",
                        "status": "completed",
                        "error": None,
                    },
                },
            ],
            agent_messages=["openspace-preflight done"],
        ),
    )
    monkeypatch.setattr(
        mcp_preflight,
        "collect_session_family",
        lambda thread_id, sessions_root: SimpleNamespace(thread_ids={"thread-123"}, session_files=set()),
    )
    monkeypatch.setattr(mcp_preflight, "cleanup_session_artifacts", lambda **kwargs: None)

    report = probe_session(cwd=workspace, codex_home=tmp_path / ".codex", keep_artifacts=False)

    assert report["status"] == "ready"
    assert report["stdout_path"] is None
    assert report["stderr_path"] is None
    assert report["session_files"] == []
