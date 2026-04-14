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


def test_update_current_daemon_status_tracks_last_used_and_active_requests(monkeypatch, tmp_path) -> None:
    identity = _build_identity(tmp_path)
    metadata_path = identity.metadata_path
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    shared_mcp_runtime._write_record(metadata_path, _build_record(identity))

    monkeypatch.setenv("OPENSPACE_MCP_INSTANCE_KEY", identity.instance_key)
    monkeypatch.setenv("OPENSPACE_MCP_DAEMON_STATE_DIR", identity.state_dir)

    monkeypatch.setattr(shared_mcp_runtime.time, "time", lambda: 111.0)
    started = shared_mcp_runtime.update_current_daemon_status(
        "main",
        touch=True,
        active_delta=1,
    )
    assert started is not None
    assert started.active_requests == 1
    assert started.last_used_at == 111.0

    monkeypatch.setattr(shared_mcp_runtime.time, "time", lambda: 114.5)
    finished = shared_mcp_runtime.update_current_daemon_status(
        "main",
        touch=True,
        active_delta=-1,
    )
    assert finished is not None
    assert finished.active_requests == 0
    assert finished.last_used_at == 114.5


def test_reap_state_dir_records_limits_live_daemons_per_kind(monkeypatch, tmp_path) -> None:
    records = []
    for index, last_used_at in enumerate((10.0, 20.0, 30.0), start=1):
        identity = shared_mcp_runtime.MCPDaemonIdentity(
            server_kind="main",
            workspace=f"/tmp/workspace-{index}",
            resolved_model="unit-model",
            llm_kwargs_fingerprint=f"llm-{index}",
            backend_scope=("shell",),
            host_skill_dirs=(f"/tmp/skills-{index}",),
            grounding_config_fingerprint=f"cfg-{index}",
            instance_key=f"key-{index}",
            state_dir=str(tmp_path),
        )
        record = shared_mcp_runtime.MCPDaemonRecord(
            server_kind="main",
            instance_key=identity.instance_key,
            pid=4000 + index,
            port=56000 + index,
            workspace=identity.workspace,
            resolved_model=identity.resolved_model,
            llm_kwargs_fingerprint=identity.llm_kwargs_fingerprint,
            backend_scope=list(identity.backend_scope),
            host_skill_dirs=list(identity.host_skill_dirs),
            grounding_config_fingerprint=identity.grounding_config_fingerprint,
            started_at=1.0,
            log_path=str(tmp_path / f"{identity.instance_key}.log"),
            ready=True,
            warmed=True,
            last_used_at=last_used_at,
            active_requests=0,
        )
        shared_mcp_runtime._write_record(identity.metadata_path, record)
        records.append(record)

    reaped: list[str] = []
    monkeypatch.setattr(shared_mcp_runtime, "_max_daemons_per_kind", lambda: 2)
    monkeypatch.setattr(shared_mcp_runtime, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(shared_mcp_runtime, "_pid_matches_server", lambda record: True)
    monkeypatch.setattr(
        shared_mcp_runtime,
        "_terminate_record_process",
        lambda record: reaped.append(record.instance_key),
    )

    shared_mcp_runtime._reap_state_dir_records(
        str(tmp_path),
        "main",
        keep_instance_key="key-3",
    )

    assert reaped == ["key-1"]
    assert not (tmp_path / "main-key-1.json").exists()
    assert (tmp_path / "main-key-2.json").exists()
    assert (tmp_path / "main-key-3.json").exists()
