"""Trusted local search service for BrowseComp-Plus task workers.

The injected BM25 backend and tokenizer live only in this owner.  Each task
execution receives a distinct Unix-socket endpoint whose server-side retriever
owns the authoritative trajectory for that execution.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .retrieval import (
    BM25SearchBackend,
    FixedCorpusBM25Retriever,
    RetrievalTrajectory,
    Tokenizer,
)

_MAX_REQUEST_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_SOCKET_PATH_BYTE_LIMIT = 103
_CLIENT_SOCKET_TIMEOUT_SECONDS = 300.0
_SERVER_JOIN_TIMEOUT_SECONDS = 10.0


class SearchTransportError(RuntimeError):
    """The package-local search service or its wire contract failed."""

    def __init__(
        self,
        message: str,
        *,
        trajectory: RetrievalTrajectory | None = None,
    ) -> None:
        super().__init__(message)
        self.trajectory = trajectory


def _frame(value: object, maximum: int) -> bytes:
    try:
        payload = (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise SearchTransportError(
            "search transport payload is not finite JSON"
        ) from exc
    if len(payload) > maximum:
        raise SearchTransportError("search transport frame exceeds its byte limit")
    return payload


def _read_frame(connection: socket.socket, maximum: int) -> object:
    payload = bytearray()
    while True:
        chunk = connection.recv(min(64 * 1024, maximum + 1 - len(payload)))
        if not chunk:
            raise SearchTransportError("search transport frame ended early")
        payload.extend(chunk)
        if len(payload) > maximum:
            raise SearchTransportError("search transport frame exceeds its byte limit")
        newline = payload.find(b"\n")
        if newline >= 0:
            if newline != len(payload) - 1:
                raise SearchTransportError(
                    "search transport accepts one frame per call"
                )
            break
    try:
        return json.loads(bytes(payload[:-1]))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SearchTransportError("search transport frame is not valid JSON") from exc


class BrowseCompSearchClient:
    """Candidate-side client containing only one task-local endpoint."""

    __slots__ = ("_endpoint",)

    def __init__(self, endpoint: str | os.PathLike[str]) -> None:
        path = Path(endpoint).absolute()
        if not str(path):
            raise ValueError("search endpoint must be non-empty")
        self._endpoint = path

    @property
    def endpoint(self) -> str:
        return str(self._endpoint)

    def search(self, query: str) -> tuple[dict[str, str | None], ...]:
        request = _frame({"method": "search", "query": query}, _MAX_REQUEST_BYTES)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(_CLIENT_SOCKET_TIMEOUT_SECONDS)
        try:
            connection.connect(str(self._endpoint))
            connection.sendall(request)
            raw = _read_frame(connection, _MAX_RESPONSE_BYTES)
        except (OSError, SearchTransportError) as exc:
            raise SearchTransportError(
                "task-local search endpoint is unavailable"
            ) from exc
        finally:
            connection.close()
        if not isinstance(raw, dict) or set(raw) not in (
            {"ok", "results"},
            {"ok", "error"},
        ):
            raise SearchTransportError("search service returned an invalid response")
        if raw.get("ok") is not True:
            raise SearchTransportError("trusted search service rejected the call")
        results = raw.get("results")
        if not isinstance(results, list) or len(results) > 5:
            raise SearchTransportError("search service returned an invalid result list")
        copied: list[dict[str, str | None]] = []
        for result in results:
            if (
                not isinstance(result, dict)
                or set(result) != {"docid", "snippet", "url"}
                or not isinstance(result.get("docid"), str)
                or not isinstance(result.get("snippet"), str)
                or result.get("url") is not None
                and not isinstance(result.get("url"), str)
            ):
                raise SearchTransportError("search service returned an invalid result")
            copied.append(
                {
                    "docid": result["docid"],
                    "snippet": result["snippet"],
                    "url": result["url"],
                }
            )
        return tuple(copied)


@dataclass(frozen=True)
class TrustedSearchSession:
    """Trusted handle; workers receive only its endpoint string."""

    session_id: str
    endpoint: Path


class _TaskSearchServer:
    def __init__(
        self,
        *,
        root: Path,
        backend: BM25SearchBackend,
        tokenizer: Tokenizer,
        backend_gate: threading.Semaphore,
    ) -> None:
        self.root = root
        self.endpoint = root / "search.sock"
        if len(os.fsencode(str(self.endpoint))) > _SOCKET_PATH_BYTE_LIMIT:
            raise SearchTransportError("task-local Unix socket path is too long")
        root.mkdir(mode=0o700, parents=False)
        self._retriever = FixedCorpusBM25Retriever(
            backend=backend,
            tokenizer=tokenizer,
        )
        self._backend_gate = backend_gate
        self._stop = threading.Event()
        self._trusted_failure: BaseException | None = None
        self._freeze_lock = threading.Lock()
        self._frozen_trajectory: RetrievalTrajectory | None = None
        self._cleanup_complete = False
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._listener.bind(str(self.endpoint))
            os.chmod(self.endpoint, 0o600)
            self._listener.listen(16)
            self._listener.settimeout(0.1)
        except BaseException:
            self._listener.close()
            self.endpoint.unlink(missing_ok=True)
            self.root.rmdir()
            raise
        self._thread = threading.Thread(
            target=self._serve,
            name=f"browsecomp-search-{root.name}",
            daemon=True,
        )
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                try:
                    request = _read_frame(connection, _MAX_REQUEST_BYTES)
                    if (
                        not isinstance(request, dict)
                        or set(request) != {"method", "query"}
                        or request.get("method") != "search"
                        or not isinstance(request.get("query"), str)
                        or not request["query"].strip()
                    ):
                        raise SearchTransportError("invalid search request")
                except SearchTransportError:
                    response: object = {
                        "ok": False,
                        "error": "invalid search request",
                    }
                else:
                    try:
                        with self._backend_gate:
                            results = self._retriever.search(request["query"])
                        response = {
                            "ok": True,
                            "results": [result.as_dict() for result in results],
                        }
                    except Exception as exc:  # noqa: BLE001 - backend errors are isolated
                        self._trusted_failure = exc
                        response = {
                            "ok": False,
                            "error": "trusted search service failed",
                        }
                try:
                    connection.sendall(_frame(response, _MAX_RESPONSE_BYTES))
                except (OSError, SearchTransportError):
                    pass

    def freeze(self) -> RetrievalTrajectory:
        with self._freeze_lock:
            if not self._cleanup_complete:
                self._stop.set()
                self._listener.close()
                self._thread.join(timeout=_SERVER_JOIN_TIMEOUT_SECONDS)
                if self._thread.is_alive():
                    raise SearchTransportError("task-local search service did not stop")
                self._frozen_trajectory = self._retriever.trajectory
                self.endpoint.unlink(missing_ok=True)
                self.root.rmdir()
                self._cleanup_complete = True
            if self._trusted_failure:
                raise SearchTransportError(
                    "trusted search backend failed",
                    trajectory=self._frozen_trajectory,
                ) from (self._trusted_failure)
            assert self._frozen_trajectory is not None
            return self._frozen_trajectory

    @property
    def cleanup_complete(self) -> bool:
        with self._freeze_lock:
            return self._cleanup_complete


class BrowseCompRetrievalService:
    """Run-scoped trusted backend with isolated task-scoped UDS sessions."""

    def __init__(
        self,
        *,
        root: str | os.PathLike[str],
        backend: BM25SearchBackend,
        tokenizer: Tokenizer,
        retrieval_concurrency: int = 1,
        socket_parent: str | os.PathLike[str] | None = None,
    ) -> None:
        if (
            isinstance(retrieval_concurrency, bool)
            or not isinstance(retrieval_concurrency, int)
            or retrieval_concurrency < 1
        ):
            raise ValueError("retrieval concurrency must be a positive integer")
        supplied = Path(root).expanduser().absolute()
        supplied.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = supplied.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("retrieval service root must be a real directory")
        self._root = supplied.resolve(strict=True)
        endpoint_parent = (
            Path(socket_parent if socket_parent is not None else "/tmp")
            .expanduser()
            .absolute()
        )
        endpoint_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        endpoint_parent = endpoint_parent.resolve(strict=True)
        endpoint_info = endpoint_parent.lstat()
        if stat.S_ISLNK(endpoint_info.st_mode) or not stat.S_ISDIR(
            endpoint_info.st_mode
        ):
            raise ValueError("retrieval socket parent must be a real directory")
        longest_endpoint = endpoint_parent / "bc-000000" / "s-000000" / "search.sock"
        if len(os.fsencode(str(longest_endpoint))) > _SOCKET_PATH_BYTE_LIMIT:
            raise ValueError(
                "retrieval socket parent is too long for task-local Unix sockets"
            )
        socket_root: Path | None = None
        for _attempt in range(32):
            candidate = endpoint_parent / f"bc-{uuid.uuid4().hex[:6]}"
            try:
                candidate.mkdir(mode=0o700)
            except FileExistsError:
                continue
            socket_root = candidate
            break
        if socket_root is None:
            raise SearchTransportError("could not allocate retrieval socket namespace")
        self._socket_root = socket_root
        self._backend = backend
        self._tokenizer = tokenizer
        self._backend_gate = threading.BoundedSemaphore(retrieval_concurrency)
        self._lock = threading.RLock()
        self._sessions: dict[str, tuple[TrustedSearchSession, _TaskSearchServer]] = {}
        self._closed = False
        self._physical_closed = False

    @property
    def active_session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def open_task_session(self) -> TrustedSearchSession:
        with self._lock:
            if self._closed:
                raise SearchTransportError("retrieval service is closed")
            server: _TaskSearchServer | None = None
            for _attempt in range(32):
                session_id = uuid.uuid4().hex
                try:
                    server = _TaskSearchServer(
                        root=self._socket_root / f"s-{session_id[:6]}",
                        backend=self._backend,
                        tokenizer=self._tokenizer,
                        backend_gate=self._backend_gate,
                    )
                except FileExistsError:
                    continue
                break
            if server is None:
                raise SearchTransportError("could not allocate task search session")
            handle = TrustedSearchSession(session_id, server.endpoint)
            self._sessions[session_id] = (handle, server)
            return handle

    def freeze_task_session(self, handle: TrustedSearchSession) -> RetrievalTrajectory:
        if not isinstance(handle, TrustedSearchSession):
            raise TypeError("search session handle is invalid")
        with self._lock:
            owned = self._sessions.get(handle.session_id)
            if owned is None or owned[0] is not handle:
                raise SearchTransportError("search session is not service-owned")
        try:
            trajectory = owned[1].freeze()
        except BaseException:
            if owned[1].cleanup_complete:
                with self._lock:
                    if self._sessions.get(handle.session_id) is owned:
                        self._sessions.pop(handle.session_id)
            raise
        with self._lock:
            if self._sessions.get(handle.session_id) is owned:
                self._sessions.pop(handle.session_id)
        return trajectory

    def close(self) -> None:
        with self._lock:
            self._closed = True
            sessions = tuple(self._sessions.values())
        first_error: BaseException | None = None
        for handle, server in sessions:
            try:
                server.freeze()
            except BaseException as exc:  # noqa: BLE001 - freeze must finish cleanup
                if first_error is None:
                    first_error = exc
                if not server.cleanup_complete:
                    continue
            with self._lock:
                owned = self._sessions.get(handle.session_id)
                if owned is not None and owned[0] is handle and owned[1] is server:
                    self._sessions.pop(handle.session_id)
        with self._lock:
            has_sessions = bool(self._sessions)
        if has_sessions:
            if first_error is not None:
                raise first_error
            return
        try:
            self._socket_root.rmdir()
        except OSError:
            pass
        try:
            self._root.rmdir()
        except OSError:
            pass
        if not self._physical_closed:
            for owner in (self._backend, self._tokenizer):
                close = getattr(owner, "close", None)
                if not callable(close):
                    continue
                try:
                    close()
                except BaseException as exc:  # noqa: BLE001 - close every owner
                    if first_error is None:
                        first_error = exc
            self._physical_closed = True
        if first_error is not None:
            raise first_error

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = [
    "BrowseCompRetrievalService",
    "BrowseCompSearchClient",
    "SearchTransportError",
    "TrustedSearchSession",
]
