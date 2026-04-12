from __future__ import annotations

import threading
import time
from pathlib import Path

from openspace import shared_mcp_runtime


def _build_identity(state_dir: Path) -> shared_mcp_runtime.MCPDaemonIdentity:
    return shared_mcp_runtime.MCPDaemonIdentity(
        server_kind="main",
        workspace="/Users/admin/PycharmProjects/openspace",
        resolved_model="unit-model",
        llm_kwargs_fingerprint="llm-fingerprint",
        backend_scope=("shell", "mcp"),
        host_skill_dirs=("/tmp/unit-skills",),
        grounding_config_fingerprint="grounding-fingerprint",
        instance_key="unit-instance-key",
        state_dir=str(state_dir),
    )


def _build_record(identity: shared_mcp_runtime.MCPDaemonIdentity) -> shared_mcp_runtime.MCPDaemonRecord:
    return shared_mcp_runtime.MCPDaemonRecord(
        server_kind=identity.server_kind,
        instance_key=identity.instance_key,
        pid=4242,
        port=56789,
        workspace=identity.workspace,
        resolved_model=identity.resolved_model,
        llm_kwargs_fingerprint=identity.llm_kwargs_fingerprint,
        backend_scope=list(identity.backend_scope),
        host_skill_dirs=list(identity.host_skill_dirs),
        grounding_config_fingerprint=identity.grounding_config_fingerprint,
        started_at=100.0,
        log_path=str(Path(identity.state_dir) / "main-unit-instance-key.log"),
        ready=False,
        warmed=False,
    )


def test_daemon_metadata_round_trip_includes_ready_and_warmed(tmp_path) -> None:
    identity = _build_identity(tmp_path)
    record = _build_record(identity)
    metadata_path = identity.metadata_path
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    shared_mcp_runtime._write_record(metadata_path, record)

    assert metadata_path.is_file()

    initial = shared_mcp_runtime._read_record(metadata_path)
    assert initial is not None
    assert initial.ready is False
    assert initial.warmed is False

    assert initial.server_kind == "main"
    assert initial.instance_key == identity.instance_key


def test_update_current_daemon_status_marks_ready_then_warmed_for_main_daemon(monkeypatch, tmp_path) -> None:
    identity = _build_identity(tmp_path)
    metadata_path = identity.metadata_path
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    shared_mcp_runtime._write_record(metadata_path, _build_record(identity))

    monkeypatch.setenv("OPENSPACE_MCP_INSTANCE_KEY", identity.instance_key)
    monkeypatch.setenv("OPENSPACE_MCP_DAEMON_STATE_DIR", identity.state_dir)

    monkeypatch.setattr(shared_mcp_runtime.time, "time", lambda: 101.0)
    ready_record = shared_mcp_runtime.update_current_daemon_status("main", ready=True)
    assert ready_record is not None
    assert ready_record.ready is True
    assert ready_record.warmed is False
    assert ready_record.ready_at == 101.0
    assert ready_record.warmed_at is None

    monkeypatch.setattr(shared_mcp_runtime.time, "time", lambda: 107.5)
    warmed_record = shared_mcp_runtime.update_current_daemon_status("main", warmed=True)
    assert warmed_record is not None
    assert warmed_record.ready is True
    assert warmed_record.warmed is True
    assert warmed_record.ready_at == 101.0
    assert warmed_record.warmed_at == 107.5

    reloaded = shared_mcp_runtime._read_record(metadata_path)
    assert reloaded is not None
    assert reloaded.ready is True
    assert reloaded.warmed is True
    assert reloaded.ready_at == 101.0
    assert reloaded.warmed_at == 107.5


def test_spawn_daemon_exports_metadata_env_for_background_updates(monkeypatch, tmp_path) -> None:
    identity = _build_identity(tmp_path)
    captured: dict[str, object] = {}

    class _FakeProcess:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs["env"]
            self.pid = 9898

    monkeypatch.setattr(shared_mcp_runtime.subprocess, "Popen", _FakeProcess)

    record = shared_mcp_runtime._spawn_daemon(identity, 45678)

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OPENSPACE_MCP_DAEMON"] == "1"
    assert env["OPENSPACE_MCP_INSTANCE_KEY"] == identity.instance_key
    assert env["OPENSPACE_MCP_DAEMON_STATE_DIR"] == identity.state_dir
    assert record.instance_key == identity.instance_key
    assert record.port == 45678


def test_update_current_daemon_status_timestamps_after_lock_wait(monkeypatch, tmp_path) -> None:
    identity = _build_identity(tmp_path)
    metadata_path = identity.metadata_path
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    shared_mcp_runtime._write_record(metadata_path, _build_record(identity))

    monkeypatch.setenv("OPENSPACE_MCP_INSTANCE_KEY", identity.instance_key)
    monkeypatch.setenv("OPENSPACE_MCP_DAEMON_STATE_DIR", identity.state_dir)

    current_time = {"value": 101.0}
    monkeypatch.setattr(shared_mcp_runtime.time, "time", lambda: current_time["value"])

    result_holder: dict[str, shared_mcp_runtime.MCPDaemonRecord | None] = {}

    with shared_mcp_runtime._FileLock(identity.lock_path):
        worker = threading.Thread(
            target=lambda: result_holder.setdefault(
                "record",
                shared_mcp_runtime.update_current_daemon_status("main", warmed=True),
            ),
            daemon=True,
        )
        worker.start()
        time.sleep(0.1)
        current_time["value"] = 107.5

    worker.join(timeout=2.0)

    updated = result_holder.get("record")
    assert updated is not None
    assert updated.warmed is True
    assert updated.warmed_at == 107.5
