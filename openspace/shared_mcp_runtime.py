from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from openspace.config.loader import get_agent_config
from openspace.grounding.backends.mcp.client import MCPClient
from openspace.host_detection import (
    build_grounding_config_path,
    build_llm_kwargs,
    load_runtime_env,
)
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

ServerKind = Literal["main", "evolution"]

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXPECTED_TOOL_NAMES: dict[ServerKind, tuple[str, ...]] = {
    "main": ("execute_task", "search_skills", "fix_skill", "upload_skill"),
    "evolution": ("evolve_from_context",),
}
_SERVER_MODULES: dict[ServerKind, str] = {
    "main": "openspace.mcp_server",
    "evolution": "openspace.evolution_mcp_server",
}


@dataclass(frozen=True)
class MCPDaemonIdentity:
    server_kind: ServerKind
    workspace: str
    resolved_model: str
    llm_kwargs_fingerprint: str
    backend_scope: tuple[str, ...]
    host_skill_dirs: tuple[str, ...]
    grounding_config_fingerprint: str
    instance_key: str
    state_dir: str

    @property
    def metadata_path(self) -> Path:
        return Path(self.state_dir) / f"{self.server_kind}-{self.instance_key}.json"

    @property
    def lock_path(self) -> Path:
        return Path(self.state_dir) / f"{self.server_kind}-{self.instance_key}.lock"

    @property
    def log_path(self) -> Path:
        return Path(self.state_dir) / f"{self.server_kind}-{self.instance_key}.log"


@dataclass(frozen=True)
class MCPDaemonRecord:
    server_kind: ServerKind
    instance_key: str
    pid: int
    port: int
    workspace: str
    resolved_model: str
    llm_kwargs_fingerprint: str
    backend_scope: list[str]
    host_skill_dirs: list[str]
    grounding_config_fingerprint: str
    started_at: float
    log_path: str
    ready: bool = False
    warmed: bool = False
    ready_at: float | None = None
    warmed_at: float | None = None
    warmup_error: str | None = None
    last_used_at: float | None = None
    active_requests: int = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"


class _FileLock:
    def __init__(self, path: Path):
        self._path = path
        self._handle = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a+", encoding="utf-8")
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self._handle:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _default_state_dir() -> Path:
    override = os.environ.get("OPENSPACE_MCP_DAEMON_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return (base / "openspace" / "mcp-daemons").resolve()


def _canonical_workspace() -> Path:
    workspace = Path(os.environ.get("OPENSPACE_WORKSPACE") or os.getcwd()).expanduser()
    workspace = workspace.resolve()
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip()).resolve()
    except Exception:
        pass
    return workspace


def _effective_backend_scope(server_kind: ServerKind) -> list[str]:
    raw = os.environ.get("OPENSPACE_BACKEND_SCOPE", "").strip()
    if raw:
        parts = [part.strip().lower() for part in raw.split(",") if part.strip()]
        return sorted(dict.fromkeys(parts))

    if server_kind == "evolution":
        return ["shell", "system"]

    agent_cfg = get_agent_config("GroundingAgent") or {}
    parts = agent_cfg.get("backend_scope") or ["gui", "shell", "mcp", "web", "system"]
    return sorted(dict.fromkeys(str(part).strip().lower() for part in parts if str(part).strip()))


def _effective_host_skill_dirs() -> list[str]:
    raw = os.environ.get("OPENSPACE_HOST_SKILL_DIRS", "").strip()
    if not raw:
        return []

    normalized: list[str] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        resolved = str(Path(item).expanduser().resolve())
        if resolved not in normalized:
            normalized.append(resolved)
    return normalized


def _fingerprint_payload(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _grounding_config_fingerprint() -> str:
    config_path = build_grounding_config_path()
    if not config_path:
        return "none"

    path = Path(config_path)
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def compute_daemon_identity(server_kind: ServerKind) -> MCPDaemonIdentity:
    load_runtime_env()

    workspace = _canonical_workspace()
    env_model = os.environ.get("OPENSPACE_MODEL", "")
    resolved_model, llm_kwargs = build_llm_kwargs(env_model)
    backend_scope = _effective_backend_scope(server_kind)
    host_skill_dirs = _effective_host_skill_dirs()
    grounding_config_fingerprint = _grounding_config_fingerprint()
    llm_kwargs_fingerprint = _fingerprint_payload(llm_kwargs)

    key_payload = {
        "server_kind": server_kind,
        "workspace": str(workspace),
        "resolved_model": resolved_model,
        "llm_kwargs_fingerprint": llm_kwargs_fingerprint,
        "backend_scope": backend_scope,
        "host_skill_dirs": host_skill_dirs,
        "grounding_config_fingerprint": grounding_config_fingerprint,
    }

    return MCPDaemonIdentity(
        server_kind=server_kind,
        workspace=str(workspace),
        resolved_model=resolved_model,
        llm_kwargs_fingerprint=llm_kwargs_fingerprint,
        backend_scope=tuple(backend_scope),
        host_skill_dirs=tuple(host_skill_dirs),
        grounding_config_fingerprint=grounding_config_fingerprint,
        instance_key=_fingerprint_payload(key_payload)[:32],
        state_dir=str(_default_state_dir()),
    )


def _read_record(path: Path) -> MCPDaemonRecord | None:
    if not path.is_file():
        return None
    try:
        return MCPDaemonRecord(**json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        logger.warning("Failed to read daemon metadata %s: %s", path, exc)
        return None


def _write_record(path: Path, record: MCPDaemonRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(asdict(record), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _record_last_used_at(record: MCPDaemonRecord) -> float:
    return record.last_used_at or record.warmed_at or record.ready_at or record.started_at


def _max_daemons_per_kind() -> int:
    raw = os.environ.get("OPENSPACE_MCP_MAX_DAEMONS_PER_KIND", "").strip()
    if not raw:
        return 8
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid OPENSPACE_MCP_MAX_DAEMONS_PER_KIND=%r", raw)
        return 8


def _unlink_record_artifacts(record: MCPDaemonRecord, state_dir: str) -> None:
    metadata_path, lock_path = _metadata_paths(record.server_kind, record.instance_key, state_dir)
    with contextlib.suppress(FileNotFoundError):
        metadata_path.unlink()
    with contextlib.suppress(FileNotFoundError):
        lock_path.unlink()


def _collect_records(state_dir: str, server_kind: ServerKind) -> list[MCPDaemonRecord]:
    state_path = Path(state_dir)
    if not state_path.is_dir():
        return []

    records: list[MCPDaemonRecord] = []
    for metadata_path in sorted(state_path.glob(f"{server_kind}-*.json")):
        record = _read_record(metadata_path)
        if record is not None:
            records.append(record)
    return records


def _reap_state_dir_records(
    state_dir: str,
    server_kind: ServerKind,
    *,
    keep_instance_key: str | None = None,
) -> None:
    max_records = _max_daemons_per_kind()
    live_records: list[MCPDaemonRecord] = []

    for record in _collect_records(state_dir, server_kind):
        if not _pid_exists(record.pid) or not _pid_matches_server(record):
            _unlink_record_artifacts(record, state_dir)
            continue
        live_records.append(record)

    if max_records <= 0 or len(live_records) <= max_records:
        return

    remaining = len(live_records)
    for record in sorted(live_records, key=_record_last_used_at):
        if remaining <= max_records:
            break
        if keep_instance_key and record.instance_key == keep_instance_key:
            continue
        if record.active_requests > 0:
            continue

        logger.info(
            "Reaping %s daemon pid=%s instance_key=%s last_used_at=%.3f to keep fleet <= %s",
            record.server_kind,
            record.pid,
            record.instance_key,
            _record_last_used_at(record),
            max_records,
        )
        _terminate_record_process(record)
        _unlink_record_artifacts(record, state_dir)
        remaining -= 1


def _metadata_paths(
    server_kind: ServerKind,
    instance_key: str,
    state_dir: str,
) -> tuple[Path, Path]:
    state_path = Path(state_dir)
    return (
        state_path / f"{server_kind}-{instance_key}.json",
        state_path / f"{server_kind}-{instance_key}.lock",
    )


def update_current_daemon_status(
    server_kind: ServerKind,
    *,
    ready: bool | None = None,
    warmed: bool | None = None,
    warmup_error: str | None = None,
    touch: bool = False,
    active_delta: int = 0,
) -> MCPDaemonRecord | None:
    instance_key = os.environ.get("OPENSPACE_MCP_INSTANCE_KEY", "").strip()
    state_dir = os.environ.get("OPENSPACE_MCP_DAEMON_STATE_DIR", "").strip()
    if not instance_key or not state_dir:
        return None

    metadata_path, lock_path = _metadata_paths(server_kind, instance_key, state_dir)
    with _FileLock(lock_path):
        record = _read_record(metadata_path)
        if record is None:
            return None
        now = time.time()

        updates: dict[str, Any] = {}
        if ready is not None:
            updates["ready"] = ready
            if ready and record.ready_at is None:
                updates["ready_at"] = now
        if warmed is not None:
            updates["warmed"] = warmed
            if warmed and record.warmed_at is None:
                updates["warmed_at"] = now
        if warmup_error is not None:
            updates["warmup_error"] = warmup_error

        if active_delta:
            updates["active_requests"] = max(0, record.active_requests + active_delta)
            touch = True
        if touch:
            updates["last_used_at"] = now

        if not updates:
            return record

        updated = MCPDaemonRecord(
            **{
                **asdict(record),
                **updates,
            }
        )
        _write_record(metadata_path, updated)
        return updated


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _expected_process_marker(server_kind: ServerKind) -> str:
    return _SERVER_MODULES[server_kind]


def _pid_matches_server(record: MCPDaemonRecord) -> bool:
    if os.name == "nt":
        return _pid_exists(record.pid)
    try:
        proc = subprocess.run(
            ["ps", "-o", "command=", "-p", str(record.pid)],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return False
    command = proc.stdout.strip()
    return bool(command) and _expected_process_marker(record.server_kind) in command


def _terminate_record_process(record: MCPDaemonRecord) -> None:
    if not _pid_exists(record.pid) or not _pid_matches_server(record):
        return

    with contextlib.suppress(Exception):
        os.kill(record.pid, signal.SIGTERM)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not _pid_exists(record.pid):
            return
        time.sleep(0.1)
    with contextlib.suppress(Exception):
        os.kill(record.pid, signal.SIGKILL)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _spawn_daemon(identity: MCPDaemonIdentity, port: int) -> MCPDaemonRecord:
    env = os.environ.copy()
    env["OPENSPACE_MCP_DAEMON"] = "1"
    env["OPENSPACE_MCP_INSTANCE_KEY"] = identity.instance_key
    env["OPENSPACE_MCP_DAEMON_STATE_DIR"] = identity.state_dir
    env["OPENSPACE_WORKSPACE"] = identity.workspace
    env["OPENSPACE_MODEL"] = identity.resolved_model
    env["OPENSPACE_BACKEND_SCOPE"] = ",".join(identity.backend_scope)
    if identity.host_skill_dirs:
        env["OPENSPACE_HOST_SKILL_DIRS"] = ",".join(identity.host_skill_dirs)
    else:
        env.pop("OPENSPACE_HOST_SKILL_DIRS", None)

    log_path = identity.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab")

    popen_kwargs: dict[str, Any] = {
        "cwd": identity.workspace,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            _SERVER_MODULES[identity.server_kind],
            "--transport",
            "streamable-http",
            "--port",
            str(port),
        ],
        **popen_kwargs,
    )
    log_handle.close()
    started_at = time.time()
    return MCPDaemonRecord(
        server_kind=identity.server_kind,
        instance_key=identity.instance_key,
        pid=proc.pid,
        port=port,
        workspace=identity.workspace,
        resolved_model=identity.resolved_model,
        llm_kwargs_fingerprint=identity.llm_kwargs_fingerprint,
        backend_scope=list(identity.backend_scope),
        host_skill_dirs=list(identity.host_skill_dirs),
        grounding_config_fingerprint=identity.grounding_config_fingerprint,
        started_at=started_at,
        log_path=str(log_path),
        last_used_at=started_at,
    )


async def _probe_record(record: MCPDaemonRecord) -> bool:
    client = MCPClient(
        config={"mcpServers": {"daemon": {"url": record.url}}},
        timeout=5.0,
        sse_read_timeout=15.0,
        max_retries=1,
        retry_interval=0.1,
        check_dependencies=False,
    )
    try:
        session = await client.create_session("daemon", auto_initialize=True)
        if session is None:
            return False
        tools = await session.list_tools()
        actual = {tool.name for tool in tools}
        expected = set(_EXPECTED_TOOL_NAMES[record.server_kind])
        return actual == expected
    except Exception:
        return False
    finally:
        with contextlib.suppress(Exception):
            await client.close_all_sessions()


async def _wait_until_ready(record: MCPDaemonRecord, timeout_seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _pid_exists(record.pid) and await _probe_record(record):
            return True
        await asyncio.sleep(0.25)
    return False


async def ensure_daemon(server_kind: ServerKind) -> MCPDaemonRecord:
    identity = compute_daemon_identity(server_kind)
    identity.metadata_path.parent.mkdir(parents=True, exist_ok=True)

    with _FileLock(identity.lock_path):
        _reap_state_dir_records(
            identity.state_dir,
            server_kind,
            keep_instance_key=identity.instance_key,
        )
        existing = _read_record(identity.metadata_path)
        if existing and _pid_exists(existing.pid) and await _probe_record(existing):
            now = time.time()
            if not existing.ready or (server_kind != "main" and not existing.warmed):
                refreshed = MCPDaemonRecord(
                    **{
                        **asdict(existing),
                        "ready": True,
                        "ready_at": existing.ready_at or now,
                        "warmed": (existing.warmed or server_kind != "main"),
                        "warmed_at": (
                            existing.warmed_at
                            or (now if (existing.warmed or server_kind != "main") else None)
                        ),
                        "last_used_at": now,
                    }
                )
                _write_record(identity.metadata_path, refreshed)
                return refreshed or existing
            refreshed = MCPDaemonRecord(
                **{
                    **asdict(existing),
                    "last_used_at": now,
                }
            )
            _write_record(identity.metadata_path, refreshed)
            return refreshed

        if existing:
            _terminate_record_process(existing)
            _unlink_record_artifacts(existing, identity.state_dir)

        last_error: Exception | None = None
        for _ in range(3):
            record = _spawn_daemon(identity, _pick_free_port())
            _write_record(identity.metadata_path, record)
            if await _wait_until_ready(record):
                now = time.time()
                updated = MCPDaemonRecord(
                    **{
                        **asdict(record),
                        "ready": True,
                        "ready_at": now,
                        "warmed": (server_kind != "main"),
                        "warmed_at": (now if server_kind != "main" else None),
                        "last_used_at": now,
                    }
                )
                _write_record(identity.metadata_path, updated)
                return updated

            last_error = RuntimeError(
                f"Daemon for key={identity.instance_key} did not become ready"
            )
            _terminate_record_process(record)
            _unlink_record_artifacts(record, identity.state_dir)

        raise last_error or RuntimeError("Failed to start daemon")
