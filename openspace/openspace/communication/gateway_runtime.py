from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

_GATEWAY_KIND = "openspace-communication-gateway"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _process_start_time(pid: int) -> Optional[int]:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        return int(stat_path.read_text(encoding="utf-8").split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        return None


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _build_process_record() -> dict[str, Any]:
    pid = os.getpid()
    return {
        "pid": pid,
        "kind": _GATEWAY_KIND,
        "argv": list(sys.argv),
        "start_time": _process_start_time(pid),
    }


def _record_matches_live_process(record: dict[str, Any]) -> bool:
    try:
        pid = int(record["pid"])
    except (KeyError, TypeError, ValueError):
        return False
    if not _is_pid_alive(pid):
        return False

    recorded_start_time = record.get("start_time")
    live_start_time = _process_start_time(pid)
    if recorded_start_time is None or live_start_time is None:
        return True
    return live_start_time == recorded_start_time


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class LockConflictError(RuntimeError):
    def __init__(self, scope: str, identity: str, record: Optional[dict[str, Any]] = None):
        super().__init__(f"Communication gateway lock is already held for {scope}:{identity}")
        self.scope = scope
        self.identity = identity
        self.record = record or {}


@dataclass
class ScopedRuntimeLock:
    path: Path
    record: dict[str, Any]
    released: bool = False

    @classmethod
    def acquire(
        cls,
        *,
        locks_dir: Path,
        scope: str,
        identity: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "ScopedRuntimeLock":
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_path = locks_dir / f"{scope}-{_scope_hash(identity)}.lock"
        record = {
            **_build_process_record(),
            "scope": scope,
            "identity": identity,
            "metadata": metadata or {},
            "created_at": _utcnow_iso(),
            "updated_at": _utcnow_iso(),
        }

        while True:
            existing = _read_json(lock_path)
            if existing is not None:
                if _record_matches_live_process(existing):
                    raise LockConflictError(scope, identity, existing)
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise RuntimeError(f"Failed to remove stale gateway lock {lock_path}: {exc}") from exc
            elif lock_path.exists():
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise RuntimeError(
                        f"Failed to remove malformed gateway lock {lock_path}: {exc}"
                    ) from exc
                continue

            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue

            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(record, handle, ensure_ascii=False, indent=2)
            except Exception:
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            return cls(path=lock_path, record=record)

    def release(self) -> None:
        if self.released:
            return
        try:
            current = _read_json(self.path)
            if current and current.get("pid") == self.record.get("pid"):
                self.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to release communication gateway lock %s: %s", self.path, exc)
        finally:
            self.released = True


class GatewayRuntimeTracker:
    def __init__(self, status_path: Path):
        self.status_path = status_path

    def write_status(
        self,
        *,
        gateway_state: str,
        platforms: dict[str, dict[str, Any]],
        fatal_error: Optional[str] = None,
        exit_reason: Optional[str] = None,
        config_path: Optional[str] = None,
        sessions: Optional[int] = None,
    ) -> None:
        payload = {
            **_build_process_record(),
            "gateway_state": gateway_state,
            "fatal_error": fatal_error,
            "exit_reason": exit_reason,
            "config_path": config_path,
            "platforms": platforms,
            "sessions": sessions,
            "updated_at": _utcnow_iso(),
        }
        _write_json(self.status_path, payload)

    def read_status(self) -> Optional[dict[str, Any]]:
        return _read_json(self.status_path)


class ScopedLockManager:
    def __init__(self, locks_dir: Path):
        self.locks_dir = locks_dir

    def acquire(
        self,
        scope: str,
        identity: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "ScopedLock":
        return ScopedRuntimeLock.acquire(
            locks_dir=self.locks_dir,
            scope=scope,
            identity=identity,
            metadata=metadata,
        )

    @staticmethod
    def release(lock: "ScopedLock") -> None:
        lock.release()


class RuntimeStatusStore:
    def __init__(self, status_path: Path):
        self._tracker = GatewayRuntimeTracker(status_path)

    def write(
        self,
        *,
        gateway_state: str,
        platforms: dict[str, dict[str, Any]],
        fatal_error: Optional[str] = None,
        exit_reason: Optional[str] = None,
        config_path: Optional[str] = None,
        sessions: Optional[int] = None,
    ) -> None:
        self._tracker.write_status(
            gateway_state=gateway_state,
            platforms=platforms,
            fatal_error=fatal_error,
            exit_reason=exit_reason,
            config_path=config_path,
            sessions=sessions,
        )

    def read(self) -> Optional[dict[str, Any]]:
        return self._tracker.read_status()


ScopedLock = ScopedRuntimeLock
