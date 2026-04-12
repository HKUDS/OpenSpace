from __future__ import annotations

import asyncio
from pathlib import Path

from openspace import mcp_proxy
from openspace import shared_mcp_runtime
from openspace.mcp_tool_registration import register_main_tools


def test_proxy_mode_defaults_follow_split_rollout(monkeypatch) -> None:
    monkeypatch.delenv("OPENSPACE_MCP_PROXY_MODE", raising=False)

    assert mcp_proxy._proxy_mode_for("main") == "daemon"
    assert mcp_proxy._proxy_mode_for("evolution") == "daemon"

    monkeypatch.setenv("OPENSPACE_MCP_PROXY_MODE", "daemon")
    assert mcp_proxy._proxy_mode_for("main") == "daemon"
    assert mcp_proxy._proxy_mode_for("evolution") == "daemon"


def test_proxy_registration_is_lazy(monkeypatch) -> None:
    async def _fail_if_called(server_kind):
        raise AssertionError(f"ensure_daemon should not run during tool registration ({server_kind})")

    monkeypatch.setattr(mcp_proxy, "ensure_daemon", _fail_if_called)

    mcp = mcp_proxy._build_fastmcp("main")
    register_main_tools(mcp, mcp_proxy._MainProxyImplementation())

    assert {tool.name for tool in mcp._tool_manager.list_tools()} == {
        "execute_task",
        "search_skills",
        "fix_skill",
        "upload_skill",
    }


def test_compute_daemon_identity_normalizes_repo_workspace(monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    nested_path = repo_root / "openspace"

    monkeypatch.setattr(
        shared_mcp_runtime,
        "build_llm_kwargs",
        lambda model: ("resolved-model", {"api_base": "http://unit.test/v1"}),
    )
    monkeypatch.setattr(shared_mcp_runtime, "build_grounding_config_path", lambda: None)
    monkeypatch.setattr(
        shared_mcp_runtime,
        "get_agent_config",
        lambda name: {"backend_scope": ["mcp", "shell"]},
    )
    monkeypatch.delenv("OPENSPACE_BACKEND_SCOPE", raising=False)
    monkeypatch.delenv("OPENSPACE_HOST_SKILL_DIRS", raising=False)

    monkeypatch.setenv("OPENSPACE_WORKSPACE", str(repo_root))
    root_identity = shared_mcp_runtime.compute_daemon_identity("main")

    monkeypatch.setenv("OPENSPACE_WORKSPACE", str(nested_path))
    nested_identity = shared_mcp_runtime.compute_daemon_identity("main")

    assert root_identity.workspace == str(repo_root.resolve())
    assert nested_identity.workspace == str(repo_root.resolve())
    assert root_identity.instance_key == nested_identity.instance_key


def test_compute_daemon_identity_changes_when_skill_dirs_change(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        shared_mcp_runtime,
        "build_llm_kwargs",
        lambda model: ("resolved-model", {"api_base": "http://unit.test/v1"}),
    )
    monkeypatch.setattr(shared_mcp_runtime, "build_grounding_config_path", lambda: None)
    monkeypatch.setattr(
        shared_mcp_runtime,
        "get_agent_config",
        lambda name: {"backend_scope": ["shell", "mcp"]},
    )
    monkeypatch.setenv("OPENSPACE_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("OPENSPACE_BACKEND_SCOPE", raising=False)

    first = tmp_path / "skills-a"
    second = tmp_path / "skills-b"
    first.mkdir()
    second.mkdir()

    monkeypatch.setenv("OPENSPACE_HOST_SKILL_DIRS", str(first))
    first_identity = shared_mcp_runtime.compute_daemon_identity("main")

    monkeypatch.setenv("OPENSPACE_HOST_SKILL_DIRS", f"{first},{second}")
    second_identity = shared_mcp_runtime.compute_daemon_identity("main")

    assert first_identity.host_skill_dirs == (str(first.resolve()),)
    assert second_identity.host_skill_dirs == (
        str(first.resolve()),
        str(second.resolve()),
    )
    assert first_identity.instance_key != second_identity.instance_key


async def _ready_probe(record):
    return True


def test_ensure_daemon_marks_main_ready_but_not_warmed(monkeypatch, tmp_path) -> None:
    identity = shared_mcp_runtime.MCPDaemonIdentity(
        server_kind="main",
        workspace=str(tmp_path),
        resolved_model="model",
        llm_kwargs_fingerprint="llm",
        backend_scope=("shell",),
        host_skill_dirs=(str(tmp_path),),
        grounding_config_fingerprint="cfg",
        instance_key="main-key",
        state_dir=str(tmp_path),
    )

    monkeypatch.setattr(shared_mcp_runtime, "compute_daemon_identity", lambda kind: identity)
    monkeypatch.setattr(shared_mcp_runtime, "_pick_free_port", lambda: 12345)
    monkeypatch.setattr(shared_mcp_runtime, "_spawn_daemon", lambda ident, port: shared_mcp_runtime.MCPDaemonRecord(
        server_kind="main",
        instance_key=ident.instance_key,
        pid=4321,
        port=port,
        workspace=ident.workspace,
        resolved_model=ident.resolved_model,
        llm_kwargs_fingerprint=ident.llm_kwargs_fingerprint,
        backend_scope=list(ident.backend_scope),
        host_skill_dirs=list(ident.host_skill_dirs),
        grounding_config_fingerprint=ident.grounding_config_fingerprint,
        started_at=1.0,
        log_path=str(identity.log_path),
    ))
    monkeypatch.setattr(shared_mcp_runtime, "_wait_until_ready", _ready_probe)
    monkeypatch.setattr(shared_mcp_runtime, "_pid_exists", lambda pid: True)

    record = asyncio.run(shared_mcp_runtime.ensure_daemon("main"))

    assert record.ready is True
    assert record.warmed is False
    assert record.ready_at is not None
    assert record.warmed_at is None


def test_update_current_daemon_status_marks_warmed(monkeypatch, tmp_path) -> None:
    metadata_path = tmp_path / "main-key.json"
    lock_path = tmp_path / "main-key.lock"
    record = shared_mcp_runtime.MCPDaemonRecord(
        server_kind="main",
        instance_key="key",
        pid=4321,
        port=12345,
        workspace=str(tmp_path),
        resolved_model="model",
        llm_kwargs_fingerprint="llm",
        backend_scope=["shell"],
        host_skill_dirs=[str(tmp_path)],
        grounding_config_fingerprint="cfg",
        started_at=1.0,
        log_path=str(tmp_path / "main-key.log"),
        ready=True,
        warmed=False,
        ready_at=2.0,
    )
    shared_mcp_runtime._write_record(metadata_path, record)
    lock_path.touch()

    monkeypatch.setenv("OPENSPACE_MCP_INSTANCE_KEY", "key")
    monkeypatch.setenv("OPENSPACE_MCP_DAEMON_STATE_DIR", str(tmp_path))

    updated = shared_mcp_runtime.update_current_daemon_status(
        "main",
        warmed=True,
        warmup_error=None,
    )

    assert updated is not None
    assert updated.ready is True
    assert updated.warmed is True
    assert updated.warmed_at is not None
