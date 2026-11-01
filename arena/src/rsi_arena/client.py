"""Narrow Python client for one run-scoped RSI Arena service."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from rsi_arena.dto import (
    ArenaProtocolError,
    ArenaRequest,
    ArenaResponse,
    bounded_opaque_id,
)
from rsi_arena.transport import (
    ArenaTransportError,
    ArenaUnixTransport,
)

if TYPE_CHECKING:
    from rsi_arena.manifests import SampleSlot
    from rsi_arena.service import ArenaService

ARENA_SOCKET_ENV = "RSI_ARENA_SOCKET_PATH"
ARENA_CLIENT_STATE_ENV = "RSI_ARENA_CLIENT_STATE_DIR"
_PENDING_SCHEMA = "rsi-arena-pending-single-attempt"


class ArenaClientError(RuntimeError):
    """Base error raised by the public Arena client."""


class ArenaClientStateError(ArenaClientError):
    """Run-private pending occurrence state is invalid or conflicting."""


class ArenaClientTransportError(ArenaClientError):
    """The local service request did not complete at the transport boundary."""


class ArenaRequestError(ArenaClientError):
    """The service rejected a well-formed Arena request."""

    def __init__(self, response: ArenaResponse) -> None:
        assert response.error is not None
        assert response.status is not None
        self.kind = response.error.kind
        self.summary = response.error.summary
        self.status = response.status
        super().__init__(f"{self.kind}: {self.summary}")


class ArenaResult(Mapping):
    """Immutable public projection with mapping and attribute access."""

    def __init__(self, payload: Mapping[str, object]) -> None:
        self._payload = MappingProxyType(dict(payload))

    def __getitem__(self, key: str) -> object:
        return self._payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._payload)

    def __len__(self) -> int:
        return len(self._payload)

    def __getattr__(self, name: str) -> object:
        try:
            return self._payload[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    @property
    def state(self) -> str:
        return str(self._payload["state"])

    @property
    def result(self) -> "ArenaResult | None":
        value = self._payload.get("result")
        return ArenaResult(value) if isinstance(value, Mapping) else None

    @property
    def outcome(self) -> str:
        value = self._payload.get("outcome")
        if value is None and self.result is not None:
            return self.result.outcome
        return str(value)

    @property
    def aggregate_score(self) -> float | None:
        value = self._payload.get("aggregate_score")
        if value is None and self.result is not None:
            return self.result.aggregate_score
        return float(value) if value is not None else None

    def as_dict(self) -> dict[str, object]:
        return dict(self._payload)


class _Transport(Protocol):
    def request(self, request: ArenaRequest) -> ArenaResponse: ...


class _DirectTransport:
    def __init__(self, service: ArenaService) -> None:
        self.service = service

    def request(self, request: ArenaRequest) -> ArenaResponse:
        response = self.service.handle_request(request)
        if response.request_id != request.request_id:
            raise ArenaTransportError("Arena response identity mismatch")
        self.service.record_response_published(request, response)
        return response


class _PendingOccurrenceStore:
    def __init__(self, state_dir: Path) -> None:
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = self.root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArenaClientStateError("Arena client state is not a directory")
        os.chmod(self.root, 0o700)
        self.path = self.root / "pending-occurrence.json"
        self.lock_path = self.root / "pending-occurrence.lock"

    @contextmanager
    def locked(self) -> Iterator[None]:
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise ArenaClientStateError("Arena client state lock failed") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ArenaClientStateError("Arena client state lock is invalid")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def read(self) -> tuple[str, str] | None:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ArenaClientStateError("pending occurrence state is invalid")
        if info.st_size > 4096:
            raise ArenaClientStateError("pending occurrence state is invalid")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArenaClientStateError("pending occurrence state is invalid") from exc
        if not isinstance(payload, Mapping) or set(payload) != {
            "schema",
            "attempt_id",
            "occurrence_key",
        }:
            raise ArenaClientStateError("pending occurrence state is invalid")
        if payload["schema"] != _PENDING_SCHEMA:
            raise ArenaClientStateError("pending occurrence state is invalid")
        try:
            return (
                bounded_opaque_id(payload["attempt_id"], "attempt_id"),
                bounded_opaque_id(payload["occurrence_key"], "occurrence_key"),
            )
        except ArenaProtocolError as exc:
            raise ArenaClientStateError("pending occurrence state is invalid") from exc

    def write(self, attempt_id: str, occurrence_key: str) -> None:
        payload = (
            json.dumps(
                {
                    "schema": _PENDING_SCHEMA,
                    "attempt_id": attempt_id,
                    "occurrence_key": occurrence_key,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        temporary = self.root / f".pending-{uuid.uuid4().hex}.tmp"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temporary, flags, 0o600)
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        else:
            os.close(descriptor)
        os.replace(temporary, self.path)
        self._fsync_root()

    def clear(self, attempt_id: str, occurrence_key: str) -> None:
        current = self.read()
        if current != (attempt_id, occurrence_key):
            return
        self.path.unlink()
        self._fsync_root()

    def _fsync_root(self) -> None:
        descriptor = os.open(
            self.root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class _ArenaDevelopmentClient:
    def __init__(self, client: "ArenaClient") -> None:
        self._client = client

    def run(
        self,
        task_ids_by_benchmark: Mapping[str, str],
        *,
        candidate_workspace: str | None = None,
    ) -> ArenaResult:
        if not isinstance(task_ids_by_benchmark, Mapping) or not task_ids_by_benchmark:
            raise ValueError("development.run requires a benchmark-to-task-ID mapping")
        benchmark_ids = list(task_ids_by_benchmark)
        if any(
            not isinstance(benchmark_id, str)
            or not benchmark_id
            or not isinstance(task_ids_by_benchmark[benchmark_id], str)
            or not task_ids_by_benchmark[benchmark_id]
            for benchmark_id in benchmark_ids
        ):
            raise ValueError("development.run benchmark/task IDs are invalid")
        payload: dict[str, object] = {
            "benchmark_ids": benchmark_ids,
            "task_ids_by_benchmark": dict(task_ids_by_benchmark),
        }
        if candidate_workspace is not None:
            if not isinstance(candidate_workspace, str) or not candidate_workspace:
                raise ValueError("candidate_workspace must be a non-empty string")
            payload["candidate_workspace"] = candidate_workspace
        return self._client._request("development.run", payload)

    def run_set(
        self,
        task_ids_by_benchmark: Mapping[str, Sequence[str]] | None = None,
        *,
        all: bool = False,
        candidate_workspace: str | None = None,
    ) -> ArenaResult:
        if candidate_workspace is not None and (
            not isinstance(candidate_workspace, str) or not candidate_workspace
        ):
            raise ValueError("candidate_workspace must be a non-empty string")
        if all:
            if task_ids_by_benchmark is not None:
                raise ValueError(
                    "development.run_set accepts a benchmark mapping or all=True, not both"
                )
            payload: dict[str, object] = {"all": True}
            if candidate_workspace is not None:
                payload["candidate_workspace"] = candidate_workspace
            return self._client._request("development.run_set", payload)
        if not isinstance(task_ids_by_benchmark, Mapping) or not task_ids_by_benchmark:
            raise ValueError(
                "development.run_set requires a benchmark-to-task-IDs mapping"
            )
        benchmark_ids = list(task_ids_by_benchmark)
        values: dict[str, list[str]] = {}
        for benchmark_id in benchmark_ids:
            task_ids = task_ids_by_benchmark[benchmark_id]
            if (
                not isinstance(benchmark_id, str)
                or not benchmark_id
                or isinstance(task_ids, (str, bytes))
            ):
                raise ValueError("development.run_set benchmark/tasks are invalid")
            selected = list(task_ids)
            if any(
                not isinstance(task_id, str) or not task_id for task_id in selected
            ) or len(set(selected)) != len(selected):
                raise ValueError("development.run_set task IDs are invalid")
            values[benchmark_id] = selected
        if not any(values.values()):
            raise ValueError("development.run_set requires at least one task")
        payload = {
            "benchmark_ids": benchmark_ids,
            "task_ids_by_benchmark": values,
        }
        if candidate_workspace is not None:
            payload["candidate_workspace"] = candidate_workspace
        return self._client._request("development.run_set", payload)


class ArenaClient:
    """CLI/SDK façade that never owns campaign lifecycle or evaluation state."""

    def __init__(self, transport: _Transport, *, client_state_dir: Path) -> None:
        self._transport = transport
        self._pending = _PendingOccurrenceStore(client_state_dir)
        self.development = _ArenaDevelopmentClient(self)

    @classmethod
    def from_service(
        cls,
        service: ArenaService,
        *,
        client_state_dir: Path | None = None,
    ) -> "ArenaClient":
        state_dir = client_state_dir or (
            service.layout.method_root / "state" / "arena-client"
        )
        return cls(_DirectTransport(service), client_state_dir=state_dir)

    @classmethod
    def from_socket(
        cls,
        socket_path: Path,
        *,
        client_state_dir: Path,
        timeout_seconds: float | None = None,
    ) -> "ArenaClient":
        return cls(
            ArenaUnixTransport(socket_path, timeout_seconds=timeout_seconds),
            client_state_dir=client_state_dir,
        )

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "ArenaClient":
        values = os.environ if environ is None else environ
        socket_path = values.get(ARENA_SOCKET_ENV)
        state_dir = values.get(ARENA_CLIENT_STATE_ENV)
        if not socket_path or not state_dir:
            raise ArenaClientStateError("Arena client environment is incomplete")
        return cls.from_socket(
            Path(socket_path),
            client_state_dir=Path(state_dir),
        )

    def context(self) -> ArenaResult:
        response = self._request("context", {})
        if response.state != "TERMINAL" or response.result is None:
            raise ArenaClientTransportError("Arena context response is invalid")
        return response.result

    def start_attempt(
        self,
        attempt_id: str,
        *,
        kind: str | None = None,
        benchmark_slots: Mapping[str, Sequence[SampleSlot]] | None = None,
    ) -> ArenaResult:
        """Start with explicit slots, or replay an already durable attempt by ID."""

        key = bounded_opaque_id(attempt_id, "attempt_id")
        if benchmark_slots is None and kind is None:
            return self._request("attempt.start", {"attempt_id": key})
        if benchmark_slots is None or kind not in {"single", "paired"}:
            raise ValueError("fresh attempt requires kind and benchmark_slots")
        return self._request(
            "attempt.start",
            {
                "attempt_id": key,
                "kind": kind,
                "benchmark_slots": {
                    benchmark: [slot.as_dict() for slot in slots]
                    for benchmark, slots in benchmark_slots.items()
                },
            },
        )

    def transition_attempt(self, attempt_id: str, decision: str) -> ArenaResult:
        key = bounded_opaque_id(attempt_id, "attempt_id")
        if decision not in {"enter", "skip"}:
            raise ValueError("decision must be enter or skip")
        return self._request(
            "attempt.transition", {"attempt_id": key, "decision": decision}
        )

    def submit(
        self,
        *,
        attempt_id: str,
        occurrence_key: str,
        role: str,
        candidate_workspace: str | None = None,
        parent_candidate_id: str | None = None,
    ) -> ArenaResult:
        attempt = bounded_opaque_id(attempt_id, "attempt_id")
        occurrence = bounded_opaque_id(occurrence_key, "occurrence_key")
        if role not in {"candidate", "control", "challenger"}:
            raise ValueError("candidate role is invalid")
        payload: dict[str, object] = {
            "attempt_id": attempt,
            "occurrence_key": occurrence,
            "role": role,
        }
        if candidate_workspace is not None:
            if not isinstance(candidate_workspace, str) or not candidate_workspace:
                raise ValueError("candidate_workspace must be a non-empty string")
            payload["candidate_workspace"] = candidate_workspace
        if parent_candidate_id is not None:
            payload["parent_candidate_id"] = bounded_opaque_id(
                parent_candidate_id, "parent_candidate_id"
            )
        return self._request("submit", payload)

    def seal_attempt(self, attempt_id: str) -> ArenaResult:
        key = bounded_opaque_id(attempt_id, "attempt_id")
        return self._request("attempt.seal", {"attempt_id": key})

    def attempt_result(self, attempt_id: str, *, wait: bool = True) -> ArenaResult:
        key = bounded_opaque_id(attempt_id, "attempt_id")
        if not isinstance(wait, bool):
            raise ValueError("wait must be boolean")
        return self._request("attempt.result", {"attempt_id": key, "wait": wait})

    def result(self, occurrence_key: str, *, wait: bool = True) -> ArenaResult:
        """Read one caller-known occurrence terminal without listing attempts."""

        key = bounded_opaque_id(occurrence_key, "occurrence_key")
        if wait is not True:
            raise ValueError("wait must be true")
        response = self._request("result", {"occurrence_key": key, "wait": True})
        if response.state == "TERMINAL":
            with self._pending.locked():
                pending = self._pending.read()
                if pending is not None and pending[1] == key:
                    self._pending.clear(*pending)
        return response

    def submit_once(self, occurrence_key: str | None = None) -> ArenaResult:
        """Send an ID-only intent; fresh attempts require a trusted adapter."""

        requested_occurrence = (
            bounded_opaque_id(occurrence_key, "occurrence_key")
            if occurrence_key is not None
            else None
        )
        with self._pending.locked():
            pending = self._pending.read()
            if pending is not None and requested_occurrence not in {None, pending[1]}:
                raise ArenaClientStateError("a different occurrence is already pending")
            attempt_id = pending[0] if pending else f"attempt-{uuid.uuid4().hex}"
            key = (
                pending[1]
                if pending
                else (requested_occurrence or f"submit-{uuid.uuid4().hex}")
            )
            if pending is None:
                self._pending.write(attempt_id, key)

        started = self.start_attempt(attempt_id)
        if started.state == "PENDING":
            self.transition_attempt(attempt_id, "enter")
        result = self.submit(
            attempt_id=attempt_id,
            occurrence_key=key,
            role="candidate",
        )
        sealed = self.seal_attempt(attempt_id)
        if sealed.state != "TERMINAL":
            self.attempt_result(attempt_id, wait=True)
        if result.state == "TERMINAL":
            with self._pending.locked():
                self._pending.clear(attempt_id, key)
        return result

    def _request(self, op: str, payload: Mapping[str, object]) -> ArenaResult:
        request = ArenaRequest(f"request-{uuid.uuid4().hex}", op, payload)
        try:
            response = self._transport.request(request)
        except (ArenaProtocolError, ArenaTransportError, OSError) as exc:
            raise ArenaClientTransportError("Arena request did not complete") from exc
        if response.request_id != request.request_id:
            raise ArenaClientTransportError("Arena response identity mismatch")
        if not response.ok:
            raise ArenaRequestError(response)
        assert response.state is not None
        return ArenaResult(
            {
                "state": response.state,
                "result": (
                    dict(response.result) if response.result is not None else None
                ),
            }
        )


__all__ = [
    "ARENA_CLIENT_STATE_ENV",
    "ARENA_SOCKET_ENV",
    "ArenaClient",
    "ArenaClientError",
    "ArenaClientStateError",
    "ArenaClientTransportError",
    "ArenaRequestError",
    "ArenaResult",
]
