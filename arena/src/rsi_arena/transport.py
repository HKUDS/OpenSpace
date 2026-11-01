"""Run-private Unix-domain-socket transport for :mod:`rsi_arena`."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import math
import os
import socket
import stat
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .dto import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    ArenaProtocolError,
    ArenaRequest,
    ArenaResponse,
)

if TYPE_CHECKING:
    from .service import ArenaService

DEFAULT_FRAME_READ_DEADLINE_SECONDS = 5.0
_SOCKET_PATH_LIMIT = 100


class ArenaTransportError(RuntimeError):
    """The run-private local transport cannot be started safely."""


class ArenaUnixTransport:
    """Strict one-request/one-response client for a run-private Arena socket."""

    def __init__(
        self,
        socket_path: Path,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("Arena client timeout must be finite and positive")
        self.socket_path = Path(socket_path)
        self.timeout_seconds = (
            float(timeout_seconds) if timeout_seconds is not None else None
        )

    def request(self, request: ArenaRequest) -> ArenaResponse:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout_seconds is not None:
            connection.settimeout(self.timeout_seconds)
        try:
            connection.connect(str(self.socket_path))
            connection.sendall(request.to_bytes())
            return ArenaResponse.from_bytes(self._read_frame(connection))
        except ArenaProtocolError:
            raise
        except OSError as exc:
            raise ArenaTransportError("Arena request transport failed") from exc
        finally:
            connection.close()

    @staticmethod
    def _read_frame(connection: socket.socket) -> bytes:
        data = bytearray()
        newline: int | None = None
        while True:
            try:
                chunk = connection.recv(min(8192, MAX_RESPONSE_BYTES + 1 - len(data)))
            except socket.timeout as exc:
                raise ArenaTransportError("Arena response timed out") from exc
            if not chunk:
                if newline is None:
                    raise ArenaTransportError(
                        "Arena response frame is not newline terminated"
                    )
                return bytes(data[:newline])
            data.extend(chunk)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ArenaProtocolError("response frame exceeds its byte boundary")
            if newline is None:
                found = data.find(b"\n")
                newline = found if found >= 0 else None
            if newline is not None:
                if data[newline + 1 :].strip():
                    raise ArenaProtocolError(
                        "connection contains multiple response frames"
                    )


class ArenaUnixServer:
    """One-request-per-connection synchronous JSON transport."""

    def __init__(
        self,
        service: ArenaService,
        *,
        frame_read_deadline_seconds: float = DEFAULT_FRAME_READ_DEADLINE_SECONDS,
    ) -> None:
        if (
            isinstance(frame_read_deadline_seconds, bool)
            or not isinstance(frame_read_deadline_seconds, (int, float))
            or not math.isfinite(float(frame_read_deadline_seconds))
            or frame_read_deadline_seconds <= 0
        ):
            raise ValueError("Arena frame-read deadline must be finite and positive")
        self.service = service
        self.frame_read_deadline_seconds = float(frame_read_deadline_seconds)
        digest = hashlib.sha256(
            f"{service.layout.run_id}\0{service.layout.method}".encode()
        ).hexdigest()[:24]
        self.socket_path = (
            Path("/tmp") / f"rsi-arena-{os.getuid()}" / digest / "arena.sock"
        )
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connection_lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._workers: set[threading.Thread] = set()
        self._socket_identity: tuple[int, int] | None = None
        self._lifecycle_lock_fd: int | None = None

    def start(self) -> "ArenaUnixServer":
        if self._listener is not None:
            raise ArenaTransportError("Arena UDS server is already running")
        self._prepare_socket_directory()
        self._acquire_lifecycle_lock()
        listener: socket.socket | None = None
        try:
            self._remove_stale_socket()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            info = self.socket_path.lstat()
            if not stat.S_ISSOCK(info.st_mode):
                raise ArenaTransportError("Arena endpoint is not a Unix socket")
            self._socket_identity = (info.st_dev, info.st_ino)
            listener.listen(8)
            listener.settimeout(0.2)
            self._listener = listener
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._serve,
                name=f"arena-uds-{self.service.layout.run_id}",
                daemon=True,
            )
            self._thread.start()
        except BaseException:
            if listener is not None:
                listener.close()
            self._listener = None
            self._thread = None
            try:
                self._unlink_owned_socket()
            finally:
                self._release_lifecycle_lock()
            raise
        return self

    def _prepare_socket_directory(self) -> None:
        root = self.socket_path.parent.parent
        directory = self.socket_path.parent
        for path in (root, directory):
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = path.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise ArenaTransportError("Arena socket directory is not private")
        if len(os.fsencode(self.socket_path)) > _SOCKET_PATH_LIMIT:
            raise ArenaTransportError("Arena socket path exceeds the UDS limit")

    def _acquire_lifecycle_lock(self) -> None:
        lock_path = self.socket_path.parent / "arena.lock"
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise ArenaTransportError("Arena lifecycle lock is unavailable") from exc
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ArenaTransportError("Arena lifecycle lock is not private")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ArenaTransportError(
                    "Arena endpoint lifecycle is already owned"
                ) from exc
        except BaseException:
            os.close(descriptor)
            raise
        self._lifecycle_lock_fd = descriptor

    def _release_lifecycle_lock(self) -> None:
        descriptor, self._lifecycle_lock_fd = self._lifecycle_lock_fd, None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _remove_stale_socket(self) -> None:
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode):
            raise ArenaTransportError(
                "Arena endpoint already exists and is not a socket"
            )
        self._assert_existing_socket_is_stale()
        self.socket_path.unlink()

    def _assert_existing_socket_is_stale(self) -> None:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.1)
        try:
            probe.connect(str(self.socket_path))
        except OSError as exc:
            if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise ArenaTransportError(
                    "Arena endpoint ownership is ambiguous"
                ) from exc
        else:
            raise ArenaTransportError("Arena endpoint is already active")
        finally:
            probe.close()

    def _serve(self) -> None:
        listener = self._listener
        assert listener is not None
        while not self._stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            with self._connection_lock:
                if self._stop.is_set():
                    connection.close()
                    return
                self._connections.add(connection)
                worker = threading.Thread(
                    target=self._handle_connection,
                    args=(connection,),
                    name="arena-uds-request",
                    daemon=True,
                )
                self._workers.add(worker)
            worker.start()

    def _handle_connection(self, connection: socket.socket) -> None:
        try:
            request: ArenaRequest | None = None
            try:
                request = ArenaRequest.from_bytes(self._read_frame(connection))
                if self._stop.is_set():
                    return
                response = self.service.handle_request(request)
            except ArenaProtocolError as exc:
                response = ArenaResponse.failure(
                    "invalid-request",
                    kind="protocol_error",
                    summary=str(exc),
                )
            except Exception:
                response = ArenaResponse.failure(
                    "invalid-request",
                    kind="transport_error",
                    summary="Arena transport failed",
                )
            if not self._stop.is_set():
                try:
                    connection.sendall(response.to_bytes())
                except (ArenaProtocolError, OSError):
                    pass
                else:
                    if request is not None:
                        self.service.record_response_published(request, response)
        finally:
            with self._connection_lock:
                self._connections.discard(connection)
                self._workers.discard(threading.current_thread())
            connection.close()

    def _read_frame(self, connection: socket.socket) -> bytes:
        data = bytearray()
        deadline = time.monotonic() + self.frame_read_deadline_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ArenaProtocolError("request frame timed out")
            connection.settimeout(remaining)
            try:
                chunk = connection.recv(min(8192, MAX_REQUEST_BYTES + 1 - len(data)))
            except socket.timeout as exc:
                raise ArenaProtocolError("request frame timed out") from exc
            if not chunk:
                raise ArenaProtocolError("request frame is not newline terminated")
            data.extend(chunk)
            if len(data) > MAX_REQUEST_BYTES:
                raise ArenaProtocolError("request frame exceeds its byte boundary")
            newline = data.find(b"\n")
            if newline >= 0:
                if data[newline + 1 :].strip():
                    raise ArenaProtocolError(
                        "connection contains multiple request frames"
                    )
                return bytes(data[:newline])

    def close(self) -> None:
        thread = self._thread
        if thread is threading.current_thread():
            raise ArenaTransportError("Arena UDS server cannot close itself")
        listener = self._listener
        if listener is None:
            self._release_lifecycle_lock()
            return
        self.stop_admission()
        with self._connection_lock:
            connections = tuple(self._connections)
            workers = tuple(self._workers)
            for connection in connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        if thread is not None:
            thread.join()
        for worker in workers:
            worker.join()
        self._listener = None
        self._thread = None
        try:
            self._unlink_owned_socket()
        finally:
            self._release_lifecycle_lock()

    def stop_admission(self) -> None:
        """Stop accepting requests without interrupting the active dispatch."""

        listener = self._listener
        if listener is None:
            return
        self._stop.set()
        listener.close()

    def _unlink_owned_socket(self) -> None:
        identity, self._socket_identity = self._socket_identity, None
        if identity is None:
            return
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == identity:
            self.socket_path.unlink()

    def __enter__(self) -> "ArenaUnixServer":
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = ["ArenaTransportError", "ArenaUnixServer", "ArenaUnixTransport"]
