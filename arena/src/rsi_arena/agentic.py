"""Generic agentic-CLI lifecycle for one run-scoped Arena service."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

from rsi_arena.client import ARENA_CLIENT_STATE_ENV, ARENA_SOCKET_ENV
from rsi_arena.dto import ArenaRequest, ArenaResponse
from rsi_arena.manifests import SampleSlot

from .service import ArenaService
from .transport import ArenaUnixServer

_COMMAND = "rsi-arena"
_LIBRARY = ".rsi-arena-lib"
_A_OPERATION_LOCK = "a-operation.lock"
_LAUNCHER = """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / __LIBRARY__))
os.environ[__SOCKET_ENV__] = __SOCKET_PATH__
os.environ[__STATE_ENV__] = __STATE_PATH__

from rsi_arena.agentic_cli import main

raise SystemExit(main())
"""


class TrustedAttemptBoundary:
    """Turn an ID-only fresh attempt intent into trusted frozen slots."""

    def __init__(
        self,
        service: ArenaService,
        slot_provider: Callable[[str], Mapping[str, Sequence[SampleSlot]]],
    ) -> None:
        self._service = service
        self._slot_provider = slot_provider
        self.layout = service.layout

    def handle_request(self, request: ArenaRequest) -> ArenaResponse:
        if request.op != "attempt.start":
            return self._service.handle_request(request)
        if set(request.payload) != {"attempt_id"}:
            return ArenaResponse.failure(
                request.request_id,
                kind="protocol_error",
                summary="fresh attempt slots are selected by the trusted runtime",
            )
        attempt_id = request.payload["attempt_id"]
        if not isinstance(attempt_id, str):
            return self._service.handle_request(request)
        if self._service.campaign_controller.attempt(attempt_id) is not None:
            return self._service.handle_request(request)
        slots = self._slot_provider(attempt_id)
        trusted = ArenaRequest(
            request.request_id,
            "attempt.start",
            {
                "attempt_id": attempt_id,
                "kind": "single",
                "benchmark_slots": {
                    benchmark_id: [slot.as_dict() for slot in values]
                    for benchmark_id, values in slots.items()
                },
            },
        )
        return self._service.handle_request(trusted)

    def record_response_published(
        self, request: ArenaRequest, response: ArenaResponse
    ) -> None:
        self._service.record_response_published(request, response)


class ArenaAgenticRunner:
    """Install the canonical client and forward its local UDS transport."""

    def __init__(
        self,
        service: ArenaService,
        *,
        request_service: object | None = None,
    ) -> None:
        self.service = service
        self.session_spine = service.campaign_controller
        self._server = ArenaUnixServer(request_service or service)
        self._installed_command: Path | None = None
        self._sandbox_mounts: tuple[tuple[Path, Path], ...] = ()
        self._client_environment: Mapping[str, str] = MappingProxyType({})

    @property
    def handles_submit(self) -> bool:
        return True

    @property
    def installed_command(self) -> Path | None:
        return self._installed_command

    @property
    def sandbox_mounts(self) -> tuple[tuple[Path, Path], ...]:
        """Run-private writable mounts needed by the local UDS transport."""

        return self._sandbox_mounts

    @property
    def client_environment(self) -> Mapping[str, str]:
        return self._client_environment

    @property
    def structured_terminal_delivered(self) -> bool:
        return self.service.terminal_response_delivered

    @property
    def attempt_cap_exhausted(self) -> bool:
        return self.service.attempt_cap_exhausted

    @property
    def last_submission_result(self) -> Mapping[str, object] | None:
        result = self.service.last_submission_result
        return MappingProxyType(dict(result)) if result is not None else None

    @property
    def trusted_last_submission_result(self) -> Mapping[str, object] | None:
        result = self.service.trusted_last_submission_result
        return MappingProxyType(dict(result)) if result is not None else None

    def install(
        self,
        bin_dir: Path,
        ipc_dir: Path,
        *,
        guest_ipc_dir: Path | str | None = None,
    ) -> Path:
        if self._installed_command is not None:
            raise RuntimeError("Arena agentic runner is already installed")
        bin_dir = Path(bin_dir).resolve(strict=True)
        ipc_dir = Path(ipc_dir)
        ipc_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        ipc_dir = ipc_dir.resolve(strict=True)
        workspace = self.service.workspace.resolve(strict=True)
        if _overlap(bin_dir, workspace) or _overlap(ipc_dir, workspace):
            raise RuntimeError("Arena client state entered the candidate workspace")

        host_ipc = self._server.socket_path.parent
        _ensure_private_directory(host_ipc.parent)
        _ensure_private_directory(host_ipc)
        host_state = host_ipc / "client-state"
        _ensure_private_directory(host_state)
        a_lock = host_state / _A_OPERATION_LOCK
        public_development_root = (
            self.service.public_workspace / ".rsi-arena" / "development"
        )
        a_lock.write_text(
            f"{public_development_root}\n\n",
            encoding="utf-8",
        )
        a_lock.chmod(0o600)
        guest_requested = Path(guest_ipc_dir or host_ipc)
        sandboxed = guest_requested != ipc_dir
        guest_ipc = guest_requested if sandboxed else host_ipc
        self._sandbox_mounts = ((host_ipc, guest_requested),) if sandboxed else ()
        self._client_environment = MappingProxyType(
            {
                ARENA_SOCKET_ENV: str(guest_ipc / "arena.sock"),
                ARENA_CLIENT_STATE_ENV: str(guest_ipc / "client-state"),
            }
        )

        library = bin_dir / _LIBRARY
        library.mkdir(mode=0o700)
        _copy_client_library(library)
        command = bin_dir / _COMMAND
        command.write_text(
            _LAUNCHER.replace("__LIBRARY__", repr(_LIBRARY))
            .replace("__SOCKET_ENV__", repr(ARENA_SOCKET_ENV))
            .replace("__SOCKET_PATH__", repr(str(guest_ipc / "arena.sock")))
            .replace("__STATE_ENV__", repr(ARENA_CLIENT_STATE_ENV))
            .replace("__STATE_PATH__", repr(str(guest_ipc / "client-state"))),
            encoding="utf-8",
        )
        command.chmod(0o555)
        self._installed_command = command
        return command

    def start(self) -> "ArenaAgenticRunner":
        if self._installed_command is None:
            raise RuntimeError("Arena agentic runner must be installed before start")
        self._server.start()
        return self

    def stop(self) -> None:
        self._server.close()

    def stop_admission(self) -> None:
        self._server.stop_admission()


def _copy_client_library(destination: Path) -> None:
    arena_package = Path(__file__).resolve().parent
    files = {
        "rsi_arena/__init__.py": arena_package / "__init__.py",
        "rsi_arena/dto.py": arena_package / "dto.py",
        "rsi_arena/transport.py": arena_package / "transport.py",
        "rsi_arena/agentic_cli.py": arena_package / "agentic_cli.py",
        "rsi_arena/baseline_registry.py": arena_package / "baseline_registry.py",
        "rsi_arena/client.py": arena_package / "client.py",
        "rsi_arena/cli.py": arena_package / "cli.py",
    }
    for relative, source in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = source.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Arena client source is not a regular file")
        shutil.copyfile(source, target)
        target.chmod(0o444)


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeError("Arena run-private directory is invalid")


__all__ = ["ArenaAgenticRunner", "TrustedAttemptBoundary"]
