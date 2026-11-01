"""Trusted Harbor agent bridge for evaluating an evolved Terminus-2.

The class exported by this module is intentionally importable by Harbor as a
custom agent::

    rsi_arena.benchmarks.harbor_runtime.agent_bridge:IsolatedTerminus2Agent

Harbor owns the task environment in the trusted process.  Candidate-controlled
Terminus-2 code runs in a separate bubblewrap PID/filesystem namespace and can
touch that environment only through a bounded JSONL RPC protocol.  In
particular, the candidate namespace never receives the task root, Docker
socket, RSI Arena database/run store, evaluator files, provenance digests, or
the artifact-envelope HMAC key.

The top-level Harbor import is optional so the protocol and codec primitives
can be unit-tested in RSI Arena's normal (Harbor-free) test environment.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

if __package__:
    from .provider_broker import (
        PROVIDER_DIAGNOSTICS_NAME,
        PROVIDER_TRIAL_DIAGNOSTICS_SCHEMA,
        WORKER_PROVIDER_SOCKET,
        ScopedProviderBroker,
        host_temporary_directory,
    )
else:  # pragma: no cover - standalone bubblewrap worker import
    from provider_broker import (  # type: ignore[no-redef]
        PROVIDER_DIAGNOSTICS_NAME,
        PROVIDER_TRIAL_DIAGNOSTICS_SCHEMA,
        WORKER_PROVIDER_SOCKET,
        ScopedProviderBroker,
        host_temporary_directory,
    )

try:  # Harbor is deliberately not an RSI Arena core dependency.
    from harbor.agents.base import BaseAgent as _HarborBaseAgent
except ModuleNotFoundError as exc:  # pragma: no cover - branch depends on env.
    if exc.name and (exc.name == "harbor" or exc.name.startswith("harbor.")):

        class _HarborBaseAgent:  # type: ignore[no-redef]
            """Import-only stand-in; construction fails with an actionable error."""

            SUPPORTS_ATIF = False
            SUPPORTS_RESUME = False
            SUPPORTS_WINDOWS = False

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise HarborBridgeUnavailable(
                    "Harbor is not importable. Run this agent from the trusted "
                    "Harbor environment and include the RSI Arena checkout on "
                    "PYTHONPATH."
                )

    else:
        raise


BRIDGE_SCHEMA = "rsi-arena.harbor-agent-bridge"
ENVELOPE_SCHEMA = "rsi-arena.artifact-envelope"
AGENT_IMPORT_PATH = (
    "rsi_arena.benchmarks.harbor_runtime.agent_bridge:IsolatedTerminus2Agent"
)
AGENT_NAME = "rsi-arena-isolated-terminus-2"

_GUEST_HARBOR_ROOT = PurePosixPath("/opt/harbor")
_GUEST_BRIDGE_ROOT = PurePosixPath("/rsi-arena/bridge")
_GUEST_LOGS_ROOT = PurePosixPath("/rsi-arena/logs")
_GUEST_INPUT = PurePosixPath("/rsi-arena/input.json")
_GUEST_RPC_DIR = PurePosixPath("/rsi-arena/rpc")
_GUEST_RPC_SOCKET = _GUEST_RPC_DIR / "environment.sock"
_GUEST_PROVIDER_DIR = PurePosixPath("/rsi-arena/provider")
_GUEST_CREDENTIAL = PurePosixPath("/rsi-arena/provider-credential")
_GUEST_CUSTOM_AGENT_ROOT = PurePosixPath("/rsi-arena/candidate-agent")
_GUEST_BUILTIN_AGENT_ROOT = _GUEST_HARBOR_ROOT / "src/harbor/agents/terminus_2"
_CONTEXT_RESULT_NAME = "agent-context.json"
_WORKER_STATUS_NAME = "worker-status.json"
_ENVELOPE_NAME = "rsi-arena-envelope.json"
_NONCE_RE = re.compile(r"^[A-Za-z0-9_.:-]{16,160}$")
_HEX_256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PYTHON_INNER_AGENT_REF = re.compile(
    r"python:(?P<module>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):"
    r"(?P<callable>[A-Za-z_]\w*)\Z"
)
_RPC_METHODS = frozenset({"exec", "is_dir", "upload_file", "download_file"})
_TRUSTED_CANDIDATE_KWARGS = frozenset(
    {
        "api_base",
        "llm_call_kwargs",
        "reasoning_effort",
        "reasoning_enabled",
        "temperature",
    }
)
_TIMEOUT_ANSWER_EXPORT_SECONDS = 30.0
_ANSWER_DOWNLOAD_PROBE_SOURCE = "/etc/hostname"
_ANSWER_DOWNLOAD_PROBE_MAX_BYTES = 4096
_MAX_TEXT_ANSWER_BYTES = 1024 * 1024
_AGENT_CONTEXT_KEYS = frozenset(
    {
        "n_input_tokens",
        "n_cache_tokens",
        "n_output_tokens",
        "cost_usd",
        "rollout_details",
        "metadata",
    }
)
CANDIDATE_RUNTIME_FAILURE_KEY = "rsi_arena_candidate_runtime_failure"
PROVIDER_ACCOUNTING_METADATA_KEY = "rsi_arena_provider_accounting"
_CANDIDATE_RUNTIME_FAILURE_PREFIX = "candidate_runtime_failure:"
_CANDIDATE_RUNTIME_REQUIRED_FIELDS = frozenset({"kind", "exception_type"})
_CANDIDATE_RUNTIME_FIELDS = _CANDIDATE_RUNTIME_REQUIRED_FIELDS | {"message"}
_CANDIDATE_DIAGNOSTIC_MAX_CHARS = 500


def _parse_python_inner_agent_ref(ref: object) -> tuple[str, str] | None:
    """Parse a custom reference without importing candidate-controlled code."""

    if not isinstance(ref, str):
        return None
    match = _PYTHON_INNER_AGENT_REF.fullmatch(ref)
    if match is None:
        return None
    return match.group("module"), match.group("callable")


class HarborBridgeError(RuntimeError):
    """Base class for fail-closed bridge errors."""


class HarborBridgeUnavailable(HarborBridgeError):
    """The trusted Harbor/bubblewrap runtime cannot enforce this bridge."""


class RpcProtocolError(HarborBridgeError):
    """A candidate sent a malformed or out-of-policy RPC frame."""


class RpcLimitExceeded(RpcProtocolError):
    """A bounded RPC resource was exhausted."""


class ArtifactValidationError(HarborBridgeError):
    """A candidate artifact did not satisfy the trusted codec."""


class _CandidateArtifactError(ArtifactValidationError):
    """A trusted channel worked, but the candidate artifact was unavailable."""


class _CandidateIntegerAnswerError(_CandidateArtifactError):
    """The trusted channel worked, but no valid candidate integer was available."""


class _CandidateTextAnswerError(_CandidateArtifactError):
    """The trusted channel worked, but no valid candidate text was available."""


class AgentContextValidationError(HarborBridgeError):
    """The worker returned an invalid or oversized AgentContext document."""


def _bounded_candidate_runtime_message(
    value: object, *, forbidden_literals: tuple[str, ...] = ()
) -> str:
    message = str(value)
    for literal in sorted(set(forbidden_literals), key=len, reverse=True):
        if literal:
            message = message.replace(literal, "[redacted]")
    message = " ".join(message.split())
    if len(message) > _CANDIDATE_DIAGNOSTIC_MAX_CHARS:
        message = message[: _CANDIDATE_DIAGNOSTIC_MAX_CHARS - 16] + "...[truncated]"
    return message


def validated_candidate_runtime_failure_marker(
    value: object, *, forbidden_literals: tuple[str, ...] = ()
) -> dict[str, str]:
    if not isinstance(value, Mapping) or frozenset(value) not in {
        _CANDIDATE_RUNTIME_REQUIRED_FIELDS,
        _CANDIDATE_RUNTIME_FIELDS,
    }:
        raise AgentContextValidationError("invalid candidate runtime failure marker")
    exception_type = value.get("exception_type")
    message = value.get("message")
    if not (
        value.get("kind") == "candidate_runtime_failure"
        and isinstance(exception_type, str)
        and exception_type.isidentifier()
        and len(exception_type) <= 128
        and (message is None or isinstance(message, str))
    ):
        raise AgentContextValidationError("invalid candidate runtime failure marker")
    marker = {
        "kind": "candidate_runtime_failure",
        "exception_type": exception_type,
    }
    if message is not None:
        bounded = _bounded_candidate_runtime_message(
            message, forbidden_literals=forbidden_literals
        )
        if bounded:
            marker["message"] = bounded
    return marker


def _innermost_exception_path(exc: BaseException) -> PurePosixPath | None:
    if isinstance(exc, SyntaxError):
        filename = exc.filename
        if isinstance(filename, str) and filename.startswith("/"):
            return PurePosixPath(filename)
    traceback = exc.__traceback__
    if traceback is None:
        return None
    while traceback.tb_next is not None:
        traceback = traceback.tb_next
    filename = traceback.tb_frame.f_code.co_filename
    if not isinstance(filename, str) or not filename.startswith("/"):
        return None
    return PurePosixPath(filename)


def candidate_owned_runtime_failure_marker(
    exc: BaseException,
    *,
    forbidden_literals: tuple[str, ...] = (),
) -> dict[str, str] | None:
    """Build the bounded marker at an already-proven candidate-owned boundary."""

    if not isinstance(exc, Exception):
        return None
    marker = {
        "kind": "candidate_runtime_failure",
        "exception_type": type(exc).__name__,
    }
    try:
        message = _bounded_candidate_runtime_message(
            exc, forbidden_literals=forbidden_literals
        )
    except Exception:
        message = ""
    if message:
        marker["message"] = message
    return validated_candidate_runtime_failure_marker(
        marker,
        forbidden_literals=forbidden_literals,
    )


def candidate_runtime_failure_marker(
    exc: BaseException,
    *,
    candidate_overlay: str | PurePosixPath,
    forbidden_literals: tuple[str, ...] = (),
) -> dict[str, str] | None:
    if not isinstance(exc, Exception):
        return None
    overlay = PurePosixPath(str(candidate_overlay))
    origin = _innermost_exception_path(exc)
    if origin is None or (origin != overlay and overlay not in origin.parents):
        return None
    return candidate_owned_runtime_failure_marker(
        exc,
        forbidden_literals=forbidden_literals,
    )


def candidate_runtime_failure_from_context(
    value: object, *, forbidden_literals: tuple[str, ...] = ()
) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    metadata = value.get("metadata")
    if (
        not isinstance(metadata, Mapping)
        or CANDIDATE_RUNTIME_FAILURE_KEY not in metadata
    ):
        return None
    return validated_candidate_runtime_failure_marker(
        metadata[CANDIDATE_RUNTIME_FAILURE_KEY],
        forbidden_literals=forbidden_literals,
    )


def candidate_runtime_failure_reason(marker: object) -> str:
    clean = validated_candidate_runtime_failure_marker(marker)
    return _CANDIDATE_RUNTIME_FAILURE_PREFIX + json.dumps(
        clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def candidate_runtime_failure_from_reason(value: object) -> dict[str, str] | None:
    if not isinstance(value, str) or not value.startswith(
        _CANDIDATE_RUNTIME_FAILURE_PREFIX
    ):
        return None
    try:
        decoded = json.loads(value[len(_CANDIDATE_RUNTIME_FAILURE_PREFIX) :])
    except json.JSONDecodeError as exc:
        raise AgentContextValidationError(
            "candidate runtime failure reason is malformed"
        ) from exc
    return validated_candidate_runtime_failure_marker(decoded)


@dataclass(frozen=True)
class RpcLimits:
    """Hard limits enforced by the trusted RPC server."""

    max_calls: int = 12_000
    max_request_bytes: int = 96 * 1024 * 1024
    max_response_bytes: int = 96 * 1024 * 1024
    max_transfer_bytes: int = 64 * 1024 * 1024
    max_total_wire_bytes: int = 512 * 1024 * 1024
    max_wall_seconds: float = 14_400.0
    max_exec_timeout_seconds: int = 900
    max_command_chars: int = 256 * 1024
    max_path_chars: int = 4096
    max_env_entries: int = 256
    max_env_value_chars: int = 32 * 1024

    def __post_init__(self) -> None:
        integer_fields = (
            self.max_calls,
            self.max_request_bytes,
            self.max_response_bytes,
            self.max_transfer_bytes,
            self.max_total_wire_bytes,
            self.max_exec_timeout_seconds,
            self.max_command_chars,
            self.max_path_chars,
            self.max_env_entries,
            self.max_env_value_chars,
        )
        if any(value <= 0 for value in integer_fields):
            raise ValueError("all RPC limits must be positive")
        if not math.isfinite(self.max_wall_seconds) or self.max_wall_seconds <= 0:
            raise ValueError("max_wall_seconds must be finite and positive")
        if self.max_transfer_bytes * 2 > self.max_request_bytes * 2:
            # A base64 transfer needs roughly 4/3 of the raw payload.  The
            # exact encoded frame is checked later; this catches absurd configs.
            raise ValueError("max_request_bytes is too small for transfer limit")
        if self.max_total_wire_bytes < max(
            self.max_request_bytes, self.max_response_bytes
        ):
            raise ValueError("total wire budget must cover one maximum frame")


@dataclass(frozen=True)
class ArtifactLimits:
    max_files: int = 20_000
    max_directories: int = 5_000
    max_total_bytes: int = 512 * 1024 * 1024
    max_file_bytes: int = 128 * 1024 * 1024
    max_path_chars: int = 1024
    max_integer_bytes: int = 16

    def __post_init__(self) -> None:
        if any(
            value <= 0
            for value in (
                self.max_files,
                self.max_directories,
                self.max_total_bytes,
                self.max_file_bytes,
                self.max_path_chars,
                self.max_integer_bytes,
            )
        ):
            raise ValueError("all artifact limits must be positive")


@dataclass
class _RpcBudget:
    limits: RpcLimits
    started_at: float = field(default_factory=time.monotonic)
    calls: int = 0
    wire_bytes: int = 0

    def consume(
        self,
        frame_bytes: int,
        *,
        call: bool = False,
        enforce_wall_deadline: bool = True,
    ) -> None:
        if frame_bytes < 0:
            raise RpcProtocolError("negative frame size")
        if (
            enforce_wall_deadline
            and time.monotonic() - self.started_at > self.limits.max_wall_seconds
        ):
            raise RpcLimitExceeded("RPC wall-clock budget exhausted")
        if call:
            self.calls += 1
            if self.calls > self.limits.max_calls:
                raise RpcLimitExceeded("RPC call budget exhausted")
        self.wire_bytes += frame_bytes
        if self.wire_bytes > self.limits.max_total_wire_bytes:
            raise RpcLimitExceeded("RPC aggregate byte budget exhausted")


@dataclass(frozen=True)
class CodecResult:
    payload_path: Path
    envelope_path: Path
    envelope: dict[str, Any]


class ArtifactCodec(Protocol):
    name: str
    payload_name: str

    async def export(
        self,
        environment: Any,
        *,
        source: str,
        convention_dir: Path,
        nonce: str,
        candidate_tree_digest: str,
        task_public_digest: str,
        bridge_identity_digest: str,
        hmac_key: bytes,
    ) -> CodecResult | None: ...


@dataclass(frozen=True)
class NativeStateCodec:
    """Leave the task environment intact for its upstream native verifier.

    This codec deliberately exports nothing.  Its only purpose is to reuse the
    fixed candidate-worker bridge for stateful Harbor tasks (notably generic
    Terminal-Bench) whose contract is "modify this container" rather than
    "return one replayable artifact".  The candidate process group is already
    dead when :meth:`export` is called, but the task container must remain
    running so Harbor can execute the task's original verifier against the
    resulting state.

    Consequently this mode establishes candidate/host filesystem separation,
    but it does *not* establish fresh-verifier or arbitrary reward-integrity
    isolation.  Callers must report those capabilities separately.
    """

    payload_name: str = ""
    name: str = "native_state"

    async def export(
        self,
        environment: Any,
        *,
        source: str,
        convention_dir: Path,
        nonce: str,
        candidate_tree_digest: str,
        task_public_digest: str,
        bridge_identity_digest: str,
        hmac_key: bytes,
    ) -> None:
        del (
            environment,
            source,
            convention_dir,
            nonce,
            candidate_tree_digest,
            task_public_digest,
            bridge_identity_digest,
            hmac_key,
        )


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _validate_nonce(value: str) -> str:
    if not isinstance(value, str) or not _NONCE_RE.fullmatch(value):
        raise HarborBridgeError(
            "nonce must be 16-160 ASCII letters, digits, '.', '_', ':', or '-'"
        )
    return value


def _validate_public_digest(label: str, value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise HarborBridgeError(f"{label} must be a non-empty bounded string")
    if _CONTROL_RE.search(value):
        raise HarborBridgeError(f"{label} contains control characters")
    return value


def _validate_payload_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 128
        or name in {".", "..", _ENVELOPE_NAME}
        or "/" in name
        or "\\" in name
        or _CONTROL_RE.search(name)
    ):
        raise ArtifactValidationError("invalid convention payload name")
    return name


def _sha256_file(path: Path) -> tuple[str, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ArtifactValidationError("payload must be one non-hardlinked regular file")
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_ino != info.st_ino:
            raise ArtifactValidationError("payload changed while being hashed")
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    finally:
        os.close(fd)
    return "sha256:" + digest.hexdigest(), size


def _exclusive_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_info = path.parent.lstat()
    if not stat.S_ISDIR(parent_info.st_mode):
        raise ArtifactValidationError(
            f"artifact parent is not a directory: {path.parent}"
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, mode)
    except FileExistsError as exc:
        raise ArtifactValidationError(
            f"candidate pre-positioned trusted artifact path: {path.name}"
        ) from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            path.unlink()
        except OSError:
            pass
        raise
    else:
        os.close(fd)


def _exclusive_copy(source: Path, destination: Path) -> None:
    source_info = source.lstat()
    if not stat.S_ISREG(source_info.st_mode) or source_info.st_nlink != 1:
        raise ArtifactValidationError("staged payload is not a safe regular file")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        out_fd = os.open(destination, flags, 0o600)
    except FileExistsError as exc:
        raise ArtifactValidationError(
            f"candidate pre-positioned trusted artifact path: {destination.name}"
        ) from exc
    in_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        while True:
            chunk = os.read(in_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(out_fd, view)
                view = view[written:]
        os.fsync(out_fd)
    except BaseException:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(in_fd)
        os.close(out_fd)


def write_signed_envelope(
    convention_dir: Path,
    *,
    payload_path: Path,
    nonce: str,
    codec: str,
    candidate_tree_digest: str,
    task_public_digest: str,
    bridge_identity_digest: str,
    hmac_key: bytes,
) -> tuple[Path, dict[str, Any]]:
    """Write a canonical, authenticated provenance envelope next to a payload."""

    convention_dir = Path(convention_dir)
    payload_path = Path(payload_path)
    if payload_path.parent != convention_dir:
        raise ArtifactValidationError("payload must be directly inside convention dir")
    payload_name = _validate_payload_name(payload_path.name)
    if not isinstance(hmac_key, bytes) or len(hmac_key) != 32:
        raise HarborBridgeError("artifact envelope requires a 32-byte HMAC key")
    unsigned: dict[str, Any] = {
        "schema": ENVELOPE_SCHEMA,
        "nonce": _validate_nonce(nonce),
        "codec": str(codec),
        "payload_name": payload_name,
        "payload_sha256": None,
        "payload_size_bytes": None,
        "candidate_tree_digest": _validate_public_digest(
            "candidate_tree_digest", candidate_tree_digest
        ),
        "task_public_digest": _validate_public_digest(
            "task_public_digest", task_public_digest
        ),
        "bridge_identity_digest": _validate_public_digest(
            "bridge_identity_digest", bridge_identity_digest
        ),
    }
    payload_digest, payload_size = _sha256_file(payload_path)
    unsigned["payload_sha256"] = payload_digest
    unsigned["payload_size_bytes"] = payload_size
    signature = hmac.new(hmac_key, _canonical_json_bytes(unsigned), hashlib.sha256)
    envelope = {**unsigned, "hmac_sha256": signature.hexdigest()}
    envelope_path = convention_dir / _ENVELOPE_NAME
    _exclusive_write(envelope_path, _canonical_json_bytes(envelope) + b"\n")
    return envelope_path, envelope


def verify_signed_envelope(
    envelope: Mapping[str, Any], hmac_key: bytes
) -> dict[str, Any]:
    """Pure verifier used by tests and downstream provenance code."""

    value = dict(envelope)
    signature = value.pop("hmac_sha256", None)
    if not isinstance(signature, str) or not _HEX_256_RE.fullmatch(signature):
        raise ArtifactValidationError("missing or malformed envelope HMAC")
    expected = hmac.new(hmac_key, _canonical_json_bytes(value), hashlib.sha256)
    if not hmac.compare_digest(signature.lower(), expected.hexdigest()):
        raise ArtifactValidationError("artifact envelope HMAC mismatch")
    if value.get("schema") != ENVELOPE_SCHEMA:
        raise ArtifactValidationError("unsupported artifact envelope schema")
    return value


def load_hmac_key_file(path: Path | str) -> bytes:
    """Read a root-owned/private 0600 file containing exactly 32 bytes as hex."""

    key_path = Path(path)
    info = key_path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise HarborBridgeUnavailable("envelope key file must be a regular file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise HarborBridgeUnavailable("envelope key file permissions must be 0600")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise HarborBridgeUnavailable("envelope key file must be owned by Harbor user")
    fd = os.open(key_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if opened.st_ino != info.st_ino or opened.st_dev != info.st_dev:
            raise HarborBridgeUnavailable("envelope key file changed while opening")
        raw = os.read(fd, 1024)
        if os.read(fd, 1):
            raise HarborBridgeUnavailable("envelope key file is oversized")
    finally:
        os.close(fd)
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise HarborBridgeUnavailable("envelope key file is not ASCII hex") from exc
    if not _HEX_256_RE.fullmatch(text):
        raise HarborBridgeUnavailable(
            "envelope key file must contain exactly 64 hexadecimal characters"
        )
    return bytes.fromhex(text)


async def _quiesce_environment(environment: Any) -> None:
    """Stop Harbor's main service before any trusted artifact download.

    A whole-environment ``stop(delete=False)`` is not equivalent for Harbor's
    Docker backend: it runs ``compose down`` and removes the container that
    ``docker cp`` needs.  ``stop_service('main')`` preserves both the stopped
    container and Harbor's later artifact/log/cleanup lifecycle.
    """

    try:
        await environment.stop_service("main")
    except Exception as exc:
        raise ArtifactValidationError(
            "task environment main service could not be quiesced before "
            "artifact download; this provider must support stop_service('main')"
        ) from exc


async def _probe_stopped_answer_download_channel(environment: Any) -> None:
    """Copy one tiny Docker-provided file through the stopped-container channel."""

    with tempfile.TemporaryDirectory(prefix="rsi-arena-answer-probe-") as temp:
        staged = Path(temp) / "hostname"
        await environment.download_file(_ANSWER_DOWNLOAD_PROBE_SOURCE, staged)
        info = staged.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > _ANSWER_DOWNLOAD_PROBE_MAX_BYTES
        ):
            raise ArtifactValidationError(
                "trusted answer download probe was not a bounded regular file"
            )


def _ensure_empty_convention_dir(path: Path) -> None:
    """Create/validate the host convention dir without following candidate links."""

    path = Path(path)
    # Harbor creates the trusted parent chain before the task starts.  It is
    # acceptable for the leaf not to exist in unit tests, but every existing
    # component from the nearest parent down must be a real directory.
    existing = path
    missing: list[Path] = []
    while not existing.exists():
        missing.append(existing)
        if existing.parent == existing:
            break
        existing = existing.parent
    if existing.exists() and not stat.S_ISDIR(existing.lstat().st_mode):
        raise ArtifactValidationError("artifact convention ancestor is not a directory")
    for component in reversed(missing):
        component.mkdir(mode=0o700)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ArtifactValidationError("artifact convention path is not a directory")
    for reserved in (_ENVELOPE_NAME,):
        if (path / reserved).exists() or (path / reserved).is_symlink():
            raise ArtifactValidationError(
                f"candidate pre-positioned trusted artifact path: {reserved}"
            )


@dataclass(frozen=True)
class IntegerAnswerCodec:
    """Export exactly one textual integer in the inclusive range 0..999."""

    limits: ArtifactLimits = field(default_factory=ArtifactLimits)
    payload_name: str = "answer.txt"
    name: str = "integer_answer"

    async def export(
        self,
        environment: Any,
        *,
        source: str,
        convention_dir: Path,
        nonce: str,
        candidate_tree_digest: str,
        task_public_digest: str,
        bridge_identity_digest: str,
        hmac_key: bytes,
    ) -> CodecResult:
        _validate_remote_path(source, RpcLimits(max_path_chars=4096))
        payload_name = _validate_payload_name(self.payload_name)
        with tempfile.TemporaryDirectory(prefix="rsi-arena-answer-") as temp:
            staged = Path(temp) / "candidate-answer"
            # Stopping first is deliberate: Docker cp works on stopped
            # containers and makes the artifact a race-free snapshot.  Other
            # providers must support the same property or fail closed.
            await _quiesce_environment(environment)
            try:
                await environment.download_file(source, staged)
            except Exception as exc:
                try:
                    await _probe_stopped_answer_download_channel(environment)
                except Exception as probe_exc:
                    raise ArtifactValidationError(
                        "trusted integer download channel failed after environment stop"
                    ) from probe_exc
                raise _CandidateIntegerAnswerError(
                    "candidate integer answer was unavailable"
                ) from exc
            info = staged.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > self.limits.max_integer_bytes
            ):
                raise _CandidateIntegerAnswerError(
                    "integer answer is not a bounded regular file"
                )
            raw = staged.read_bytes()
            try:
                text = raw.decode("ascii")
            except UnicodeDecodeError as exc:
                raise _CandidateIntegerAnswerError(
                    "integer answer must be ASCII"
                ) from exc
            # One line, digits only.  Leading zeroes are accepted and normalized
            # because AIME answers are conventionally rendered as three digits.
            if not re.fullmatch(r"[0-9]{1,3}(?:\n)?", text):
                raise _CandidateIntegerAnswerError(
                    "answer must contain only one 0-999 integer"
                )
            answer = int(text.rstrip("\n"), 10)
            if not 0 <= answer <= 999:
                raise _CandidateIntegerAnswerError("answer is outside 0-999")

            convention_dir = Path(convention_dir)
            _ensure_empty_convention_dir(convention_dir)
            payload_path = convention_dir / payload_name
            _exclusive_write(payload_path, f"{answer}\n".encode("ascii"))
            envelope_path, envelope = write_signed_envelope(
                convention_dir,
                payload_path=payload_path,
                nonce=nonce,
                codec=self.name,
                candidate_tree_digest=candidate_tree_digest,
                task_public_digest=task_public_digest,
                bridge_identity_digest=bridge_identity_digest,
                hmac_key=hmac_key,
            )
            return CodecResult(payload_path, envelope_path, envelope)


@dataclass(frozen=True)
class TextAnswerCodec:
    """Export one bounded, non-empty UTF-8 final answer."""

    max_bytes: int = _MAX_TEXT_ANSWER_BYTES
    payload_name: str = "final-answer.txt"
    name: str = "text_answer"

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or self.max_bytes <= 0
        ):
            raise ValueError("text answer byte limit must be positive")
        if self.payload_name != "final-answer.txt":
            raise ValueError("text answer payload name is fixed")

    async def export(
        self,
        environment: Any,
        *,
        source: str,
        convention_dir: Path,
        nonce: str,
        candidate_tree_digest: str,
        task_public_digest: str,
        bridge_identity_digest: str,
        hmac_key: bytes,
    ) -> CodecResult:
        _validate_remote_path(source, RpcLimits(max_path_chars=4096))
        with tempfile.TemporaryDirectory(prefix="rsi-arena-text-answer-") as temp:
            staged = Path(temp) / "candidate-answer"
            await _quiesce_environment(environment)
            try:
                await environment.download_file(source, staged)
            except Exception as exc:
                try:
                    await _probe_stopped_answer_download_channel(environment)
                except Exception as probe_exc:
                    raise ArtifactValidationError(
                        "trusted text download channel failed after environment stop"
                    ) from probe_exc
                raise _CandidateTextAnswerError(
                    "candidate final text was unavailable"
                ) from exc
            try:
                info = staged.lstat()
            except FileNotFoundError as exc:
                raise _CandidateTextAnswerError(
                    "candidate final text was unavailable"
                ) from exc
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > self.max_bytes
            ):
                raise _CandidateTextAnswerError(
                    "final text is not a bounded regular file"
                )
            raw = staged.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _CandidateTextAnswerError(
                    "final text must be valid UTF-8"
                ) from exc
            if not text.strip():
                raise _CandidateTextAnswerError("final text is empty")

            convention_dir = Path(convention_dir)
            _ensure_empty_convention_dir(convention_dir)
            payload_path = convention_dir / self.payload_name
            _exclusive_write(payload_path, raw)
            envelope_path, envelope = write_signed_envelope(
                convention_dir,
                payload_path=payload_path,
                nonce=nonce,
                codec=self.name,
                candidate_tree_digest=candidate_tree_digest,
                task_public_digest=task_public_digest,
                bridge_identity_digest=bridge_identity_digest,
                hmac_key=hmac_key,
            )
            return CodecResult(payload_path, envelope_path, envelope)


@dataclass(frozen=True)
class TreeSnapshotCodec:
    """Export a bounded worktree without replacing verifier-owned Git state."""

    limits: ArtifactLimits = field(default_factory=ArtifactLimits)
    payload_name: str = "tree.tar"
    name: str = "tree_snapshot"

    async def export(
        self,
        environment: Any,
        *,
        source: str,
        convention_dir: Path,
        nonce: str,
        candidate_tree_digest: str,
        task_public_digest: str,
        bridge_identity_digest: str,
        hmac_key: bytes,
    ) -> CodecResult:
        _validate_remote_path(source, RpcLimits(max_path_chars=4096))
        payload_name = _validate_payload_name(self.payload_name)
        with tempfile.TemporaryDirectory(prefix="rsi-arena-tree-") as temp:
            temp_root = Path(temp)
            staged_tree = temp_root / "tree"
            await _quiesce_environment(environment)
            try:
                await environment.download_dir(source, staged_tree)
            except Exception as exc:
                raise ArtifactValidationError(
                    "trusted tree download failed after environment stop"
                ) from exc
            # SWE verifier images contain a pristine /testbed/.git used by the
            # upstream grader.  Docker's directory download includes that
            # directory, but the candidate artifact must describe only the
            # mutable working tree.  Excluding it here, in trusted code after
            # the task container is stopped, avoids relying on candidate
            # cooperation and matches restore_tree's fail-closed contract.
            entries = _validate_snapshot_tree(
                staged_tree,
                self.limits,
                excluded_names=frozenset({".git"}),
                allow_safe_symlinks=True,
            )
            staged_tar = temp_root / "tree.tar"
            _write_deterministic_tar(
                staged_tree,
                staged_tar,
                entries,
                limits=self.limits,
                excluded_names=frozenset({".git"}),
            )

            convention_dir = Path(convention_dir)
            _ensure_empty_convention_dir(convention_dir)
            payload_path = convention_dir / payload_name
            _exclusive_copy(staged_tar, payload_path)
            envelope_path, envelope = write_signed_envelope(
                convention_dir,
                payload_path=payload_path,
                nonce=nonce,
                codec=self.name,
                candidate_tree_digest=candidate_tree_digest,
                task_public_digest=task_public_digest,
                bridge_identity_digest=bridge_identity_digest,
                hmac_key=hmac_key,
            )
            return CodecResult(payload_path, envelope_path, envelope)


def _validate_snapshot_tree(
    root: Path,
    limits: ArtifactLimits,
    *,
    excluded_names: frozenset[str] = frozenset(),
    allow_safe_symlinks: bool = False,
) -> list[tuple[str, Path, os.stat_result, str | None]]:
    if not root.exists() or not stat.S_ISDIR(root.lstat().st_mode):
        raise ArtifactValidationError("downloaded tree root is not a directory")
    root_resolved = root.resolve(strict=True)
    entries: list[tuple[str, Path, os.stat_result, str | None]] = []
    files = 0
    directories = 0
    total_bytes = 0
    stack: list[tuple[str, Path]] = [("", root)]
    while stack:
        relative_parent, directory = stack.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise ArtifactValidationError("cannot scan downloaded tree") from exc
        child_directories: list[tuple[str, Path]] = []
        for child in children:
            if child.name in excluded_names:
                continue
            if (
                not child.name
                or child.name in {".", ".."}
                or _CONTROL_RE.search(child.name)
            ):
                raise ArtifactValidationError("tree contains an invalid path component")
            relative = (
                f"{relative_parent}/{child.name}" if relative_parent else child.name
            )
            if len(relative) > limits.max_path_chars:
                raise ArtifactValidationError("tree path length limit exceeded")
            path = Path(child.path)
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                if not allow_safe_symlinks:
                    raise ArtifactValidationError("tree contains a symbolic link")
                link_target = _validated_tree_symlink_target(
                    root_resolved,
                    path,
                    limits,
                    excluded_names=excluded_names,
                )
                files += 1
                if files > limits.max_files:
                    raise ArtifactValidationError("tree file count limit exceeded")
                entries.append((relative, path, info, link_target))
            elif stat.S_ISDIR(info.st_mode):
                directories += 1
                if directories > limits.max_directories:
                    raise ArtifactValidationError("tree directory count limit exceeded")
                entries.append((relative, path, info, None))
                child_directories.append((relative, path))
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise ArtifactValidationError("tree contains a hard-linked file")
                files += 1
                total_bytes += info.st_size
                if files > limits.max_files:
                    raise ArtifactValidationError("tree file count limit exceeded")
                if info.st_size > limits.max_file_bytes:
                    raise ArtifactValidationError("tree per-file byte limit exceeded")
                if total_bytes > limits.max_total_bytes:
                    raise ArtifactValidationError("tree aggregate byte limit exceeded")
                entries.append((relative, path, info, None))
            else:
                raise ArtifactValidationError(
                    "tree contains a special filesystem entry"
                )
        # Reverse here because the stack is LIFO and output ordering is sorted
        # independently below.
        stack.extend(reversed(child_directories))
    return sorted(entries, key=lambda item: item[0])


def _validated_tree_symlink_target(
    root_resolved: Path,
    path: Path,
    limits: ArtifactLimits,
    *,
    excluded_names: frozenset[str],
) -> str:
    try:
        target = os.readlink(path)
    except OSError as exc:
        raise ArtifactValidationError("cannot read tree symbolic link") from exc
    if (
        not target
        or len(target) > limits.max_path_chars
        or "\\" in target
        or _CONTROL_RE.search(target)
        or PurePosixPath(target).is_absolute()
    ):
        raise ArtifactValidationError("tree contains an unsafe symbolic link")
    try:
        lexical_parts = list(
            path.parent.resolve(strict=True).relative_to(root_resolved).parts
        )
        for part in PurePosixPath(target).parts:
            if part in ("", "."):
                continue
            if part == "..":
                if not lexical_parts:
                    raise ValueError("symbolic link escapes the tree")
                lexical_parts.pop()
            else:
                lexical_parts.append(part)
        resolved = (path.parent / target).resolve(strict=True)
        relative_target = resolved.relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ArtifactValidationError("tree contains an unsafe symbolic link") from exc
    if any(part in excluded_names for part in (*lexical_parts, *relative_target.parts)):
        raise ArtifactValidationError("tree symbolic link targets excluded state")
    return target


def _write_deterministic_tar(
    root: Path,
    destination: Path,
    entries: list[tuple[str, Path, os.stat_result, str | None]],
    *,
    limits: ArtifactLimits,
    excluded_names: frozenset[str],
) -> None:
    root_resolved = root.resolve(strict=True)
    with tarfile.open(destination, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for relative, path, prior_info, link_target in entries:
            current = path.lstat()
            if (
                current.st_ino != prior_info.st_ino
                or current.st_dev != prior_info.st_dev
                or current.st_size != prior_info.st_size
                or stat.S_IFMT(current.st_mode) != stat.S_IFMT(prior_info.st_mode)
            ):
                raise ArtifactValidationError("tree changed while building snapshot")
            if stat.S_ISLNK(current.st_mode):
                current_target = _validated_tree_symlink_target(
                    root_resolved,
                    path,
                    limits,
                    excluded_names=excluded_names,
                )
                if current_target != link_target:
                    raise ArtifactValidationError(
                        "tree changed while building snapshot"
                    )
            info = tarfile.TarInfo(
                relative + ("/" if stat.S_ISDIR(current.st_mode) else "")
            )
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            if stat.S_ISDIR(current.st_mode):
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.size = 0
                archive.addfile(info)
            elif stat.S_ISLNK(current.st_mode):
                info.type = tarfile.SYMTYPE
                info.mode = 0o777
                info.size = 0
                info.linkname = link_target or ""
                archive.addfile(info)
            else:
                info.type = tarfile.REGTYPE
                info.mode = 0o755 if current.st_mode & 0o111 else 0o644
                info.size = current.st_size
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb", closefd=True) as source:
                    archive.addfile(info, source)


def _validate_remote_path(path: Any, limits: RpcLimits) -> str:
    if not isinstance(path, str) or not path or len(path) > limits.max_path_chars:
        raise RpcProtocolError("remote path must be a bounded non-empty string")
    if _CONTROL_RE.search(path) or "\\" in path:
        raise RpcProtocolError("remote path contains forbidden characters")
    pure = PurePosixPath(path)
    if not pure.is_absolute() or ".." in pure.parts:
        raise RpcProtocolError("remote path must be absolute and traversal-free")
    return path


def _validate_user(user: Any) -> str | int | None:
    if user is None:
        return None
    if isinstance(user, bool) or not isinstance(user, (str, int)):
        raise RpcProtocolError("user must be a string, integer, or null")
    if isinstance(user, str) and (
        not user or len(user) > 128 or _CONTROL_RE.search(user)
    ):
        raise RpcProtocolError("invalid user value")
    if isinstance(user, int) and not 0 <= user <= 2**31 - 1:
        raise RpcProtocolError("numeric user is out of range")
    return user


def _validate_exec_params(
    params: Mapping[str, Any], limits: RpcLimits
) -> dict[str, Any]:
    allowed = {"command", "cwd", "env", "timeout_sec", "user"}
    if set(params) - allowed:
        raise RpcProtocolError("exec contains unknown parameters")
    command = params.get("command")
    if (
        not isinstance(command, str)
        or not command
        or len(command) > limits.max_command_chars
        or "\x00" in command
    ):
        raise RpcProtocolError("exec command is empty, oversized, or contains NUL")
    result: dict[str, Any] = {"command": command}
    cwd = params.get("cwd")
    if cwd is not None:
        result["cwd"] = _validate_remote_path(cwd, limits)
    env = params.get("env")
    if env is not None:
        if not isinstance(env, dict) or len(env) > limits.max_env_entries:
            raise RpcProtocolError("exec env is not a bounded object")
        clean_env: dict[str, str] = {}
        for key, value in env.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 256
                or "=" in key
                or _CONTROL_RE.search(key)
                or not isinstance(value, str)
                or len(value) > limits.max_env_value_chars
                or "\x00" in value
            ):
                raise RpcProtocolError("exec env contains an invalid entry")
            clean_env[key] = value
        result["env"] = clean_env
    timeout = params.get("timeout_sec")
    if timeout is not None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
            or timeout > limits.max_exec_timeout_seconds
        ):
            raise RpcProtocolError("exec timeout is outside the bridge cap")
        result["timeout_sec"] = int(math.ceil(float(timeout)))
    result["user"] = _validate_user(params.get("user"))
    return result


def decode_rpc_request(
    frame: bytes,
    *,
    expected_nonce: str,
    limits: RpcLimits | None = None,
) -> dict[str, Any]:
    """Strictly parse one candidate JSONL request without invoking Harbor."""

    limits = limits or RpcLimits()
    if not frame.endswith(b"\n"):
        raise RpcProtocolError("RPC frame is not newline terminated")
    if len(frame) > limits.max_request_bytes:
        raise RpcLimitExceeded("RPC request frame is oversized")
    try:
        request = json.loads(frame)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RpcProtocolError("RPC frame is not valid UTF-8 JSON") from exc
    if not isinstance(request, dict):
        raise RpcProtocolError("RPC request must be an object")
    required = {"schema", "nonce", "id", "method", "params"}
    if set(request) != required:
        raise RpcProtocolError("RPC request has missing or unknown top-level fields")
    if request["schema"] != BRIDGE_SCHEMA:
        raise RpcProtocolError("RPC schema mismatch")
    if not hmac.compare_digest(str(request["nonce"]), expected_nonce):
        raise RpcProtocolError("RPC nonce mismatch")
    request_id = request["id"]
    if (
        isinstance(request_id, bool)
        or not isinstance(request_id, int)
        or not 0 < request_id < 2**63
    ):
        raise RpcProtocolError("RPC id must be a positive 63-bit integer")
    method = request["method"]
    if method not in _RPC_METHODS:
        raise RpcProtocolError("RPC method is not allowlisted")
    params = request["params"]
    if not isinstance(params, dict):
        raise RpcProtocolError("RPC params must be an object")

    if method == "exec":
        clean_params = _validate_exec_params(params, limits)
    elif method == "is_dir":
        if set(params) - {"path", "user"} or "path" not in params:
            raise RpcProtocolError("is_dir parameters are invalid")
        clean_params = {
            "path": _validate_remote_path(params["path"], limits),
            "user": _validate_user(params.get("user")),
        }
    elif method == "upload_file":
        if set(params) != {"target_path", "content_b64"}:
            raise RpcProtocolError("upload_file parameters are invalid")
        target = _validate_remote_path(params["target_path"], limits)
        encoded = params["content_b64"]
        if not isinstance(encoded, str) or len(encoded) > (
            limits.max_transfer_bytes * 4 // 3 + 8
        ):
            raise RpcLimitExceeded("upload_file payload is oversized")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RpcProtocolError("upload_file content is not strict base64") from exc
        if len(content) > limits.max_transfer_bytes:
            raise RpcLimitExceeded("upload_file decoded payload is oversized")
        clean_params = {"target_path": target, "content": content}
    else:  # download_file
        if set(params) != {"source_path"}:
            raise RpcProtocolError("download_file parameters are invalid")
        clean_params = {
            "source_path": _validate_remote_path(params["source_path"], limits)
        }
    return {"id": request_id, "method": method, "params": clean_params}


def _bounded_text(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", errors="ignore") + "\n[bridge truncated]"


class HarborRpcDispatcher:
    """Trusted dispatch from four typed RPCs to a BaseEnvironment instance."""

    def __init__(self, environment: Any, limits: RpcLimits | None = None) -> None:
        self.environment = environment
        self.limits = limits or RpcLimits()

    async def dispatch(self, request: Mapping[str, Any]) -> Any:
        method = request["method"]
        params = dict(request["params"])
        if method == "exec":
            timeout = params.get("timeout_sec", self.limits.max_exec_timeout_seconds)
            result = await asyncio.wait_for(
                self.environment.exec(**params),
                timeout=float(timeout) + 5.0,
            )
            return {
                "stdout": _bounded_text(
                    result.stdout, self.limits.max_response_bytes // 2
                ),
                "stderr": _bounded_text(
                    result.stderr, self.limits.max_response_bytes // 2
                ),
                "return_code": int(result.return_code),
            }
        if method == "is_dir":
            return bool(await self.environment.is_dir(**params))
        if method == "upload_file":
            content = params.pop("content")
            with tempfile.TemporaryDirectory(prefix="rsi-arena-rpc-upload-") as temp:
                source = Path(temp) / "upload"
                _exclusive_write(source, content)
                await self.environment.upload_file(source, params["target_path"])
            return {"size_bytes": len(content)}
        if method == "download_file":
            with tempfile.TemporaryDirectory(prefix="rsi-arena-rpc-download-") as temp:
                target = Path(temp) / "download"
                await self.environment.download_file(params["source_path"], target)
                info = target.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size > self.limits.max_transfer_bytes
                ):
                    raise RpcLimitExceeded(
                        "download_file result is not a bounded regular file"
                    )
                content = target.read_bytes()
            return {
                "size_bytes": len(content),
                "content_b64": base64.b64encode(content).decode("ascii"),
            }
        raise RpcProtocolError("unreachable RPC method")


def _reject_forbidden_rpc_literals(
    request: Mapping[str, Any], forbidden_literals: tuple[str, ...]
) -> None:
    """Keep explicit child-only credentials out of task-environment RPCs.

    This is exact-value defense in depth, not a claim to stop an arbitrary
    malicious program from encoding a credential.  The primary boundary is
    that BaseAgent.extra_env stays empty, so Harbor itself never injects these
    values into the task container.
    """

    if not forbidden_literals:
        return

    def visit(value: Any) -> bool:
        if isinstance(value, str):
            return any(secret in value for secret in forbidden_literals)
        if isinstance(value, bytes):
            return any(secret.encode("utf-8") in value for secret in forbidden_literals)
        if isinstance(value, Mapping):
            return any(visit(key) or visit(child) for key, child in value.items())
        if isinstance(value, (list, tuple)):
            return any(visit(child) for child in value)
        return False

    if visit(request.get("params")):
        raise RpcProtocolError(
            "RPC attempted to copy a child-only environment value into the task"
        )


class HarborRpcServer:
    """Single-client JSONL server tied to one worker nonce and budget."""

    def __init__(
        self,
        socket_path: Path,
        *,
        nonce: str,
        environment: Any,
        limits: RpcLimits | None = None,
        forbidden_literals: tuple[str, ...] = (),
    ) -> None:
        self.socket_path = Path(socket_path)
        self.nonce = _validate_nonce(nonce)
        self.limits = limits or RpcLimits()
        self.dispatcher = HarborRpcDispatcher(environment, self.limits)
        self.forbidden_literals = tuple(
            value
            for value in forbidden_literals
            if isinstance(value, str) and len(value) >= 4
        )
        self.budget = _RpcBudget(self.limits)
        self._server: asyncio.AbstractServer | None = None
        self._connections = 0

    async def __aenter__(self) -> HarborRpcServer:
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        self._server = await asyncio.start_unix_server(
            self._handle,
            path=str(self.socket_path),
            limit=self.limits.max_request_bytes + 1,
        )
        os.chmod(self.socket_path, 0o600)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._connections += 1
        if self._connections > 1:
            writer.close()
            await writer.wait_closed()
            return
        try:
            while True:
                try:
                    frame = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        await self._write_error(
                            writer, 0, RpcProtocolError("truncated RPC frame")
                        )
                    break
                except asyncio.LimitOverrunError:
                    await self._write_error(
                        writer, 0, RpcLimitExceeded("RPC frame is oversized")
                    )
                    break
                try:
                    self.budget.consume(len(frame), call=True)
                    request = decode_rpc_request(
                        frame, expected_nonce=self.nonce, limits=self.limits
                    )
                    _reject_forbidden_rpc_literals(request, self.forbidden_literals)
                    result = await self.dispatcher.dispatch(request)
                    response = {
                        "schema": BRIDGE_SCHEMA,
                        "id": request["id"],
                        "ok": True,
                        "result": result,
                    }
                    await self._write_response(writer, response)
                except Exception as exc:
                    request_id = request["id"] if "request" in locals() else 0
                    await self._write_error(writer, request_id, exc)
                    if isinstance(exc, RpcLimitExceeded):
                        break
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass

    async def _write_error(
        self, writer: asyncio.StreamWriter, request_id: int, exc: Exception
    ) -> None:
        message = _bounded_text(str(exc), 2048) or type(exc).__name__
        await self._write_response(
            writer,
            {
                "schema": BRIDGE_SCHEMA,
                "id": request_id,
                "ok": False,
                "error": {"type": type(exc).__name__, "message": message},
            },
            allow_after_wall_deadline=(
                isinstance(exc, RpcLimitExceeded)
                and message == "RPC wall-clock budget exhausted"
            ),
        )

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        response: Mapping[str, Any],
        *,
        allow_after_wall_deadline: bool = False,
    ) -> None:
        frame = _canonical_json_bytes(response) + b"\n"
        if len(frame) > self.limits.max_response_bytes:
            raise RpcLimitExceeded("RPC response frame is oversized")
        self.budget.consume(
            len(frame),
            enforce_wall_deadline=not allow_after_wall_deadline,
        )
        writer.write(frame)
        await writer.drain()


def validate_agent_context_payload(
    payload: Any, *, max_json_bytes: int = 64 * 1024 * 1024
) -> dict[str, Any]:
    """Validate the untrusted top-level AgentContext before Harbor sees it."""

    if not isinstance(payload, dict) or set(payload) - _AGENT_CONTEXT_KEYS:
        raise AgentContextValidationError("AgentContext has invalid top-level fields")
    try:
        encoded = _canonical_json_bytes(payload)
    except (TypeError, ValueError) as exc:
        raise AgentContextValidationError("AgentContext is not finite JSON") from exc
    if len(encoded) > max_json_bytes:
        raise AgentContextValidationError("AgentContext exceeds the byte limit")
    for key in ("n_input_tokens", "n_cache_tokens", "n_output_tokens"):
        value = payload.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise AgentContextValidationError(f"AgentContext {key} is invalid")
    cost = payload.get("cost_usd")
    if cost is not None and (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(float(cost))
        or cost < 0
    ):
        raise AgentContextValidationError("AgentContext cost_usd is invalid")
    rollouts = payload.get("rollout_details")
    if rollouts is not None and not isinstance(rollouts, list):
        raise AgentContextValidationError("AgentContext rollout_details must be a list")
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise AgentContextValidationError("AgentContext metadata must be an object")
    return dict(payload)


def _safe_load_json_file(path: Path, *, max_bytes: int) -> Any:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes:
        raise AgentContextValidationError("worker result is not a bounded regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        raw = bytearray()
        while len(raw) <= max_bytes:
            chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise AgentContextValidationError("worker result exceeds byte limit")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentContextValidationError("worker result is not UTF-8 JSON") from exc


def _trusted_python_metadata(trusted_harbor_root: Path) -> dict[str, Any]:
    python = trusted_harbor_root / ".venv" / "bin" / "python"
    if not python.exists():
        raise HarborBridgeUnavailable(
            f"trusted Harbor virtualenv Python is missing: {python}"
        )
    try:
        result = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                (
                    "import json,sys;print(json.dumps({"
                    "'base_prefix':sys.base_prefix,'executable':sys.executable,"
                    "'version':[sys.version_info[0],sys.version_info[1]]}))"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(result.stderr.strip())
        metadata = json.loads(result.stdout)
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise HarborBridgeUnavailable("trusted Harbor Python probe failed") from exc
    base_prefix = Path(metadata.get("base_prefix", ""))
    if not base_prefix.is_absolute() or not base_prefix.is_dir():
        raise HarborBridgeUnavailable("trusted Harbor Python has invalid base_prefix")
    major, minor = metadata.get("version", [None, None])
    if not isinstance(major, int) or not isinstance(minor, int):
        raise HarborBridgeUnavailable("trusted Harbor Python version probe is invalid")
    metadata["base_prefix"] = base_prefix
    # uv commonly creates the venv interpreter as an absolute symlink through
    # a version alias (for example ``cpython-3.13-linux-*``) while
    # ``sys.base_prefix`` resolves to the concrete patch-version directory.
    # Mount the concrete trusted runtime at the exact alias named by that
    # symlink so ``.venv/bin/python`` remains the executable exercised inside
    # the namespace.
    if python.is_symlink():
        raw_target = Path(os.readlink(python))
        if not raw_target.is_absolute() or len(raw_target.parents) < 2:
            raise HarborBridgeUnavailable("trusted Harbor Python symlink is invalid")
        guest_base_prefix = raw_target.parent.parent
    else:
        guest_base_prefix = base_prefix
    metadata["guest_base_prefix"] = guest_base_prefix
    metadata["guest_python"] = str(_GUEST_HARBOR_ROOT / ".venv/bin/python")
    metadata["site_packages"] = str(
        _GUEST_HARBOR_ROOT / f".venv/lib/python{major}.{minor}/site-packages"
    )
    return metadata


def _system_mount_args() -> list[str]:
    args: list[str] = []
    for raw in ("/usr", "/bin", "/lib", "/lib64", "/sbin"):
        path = Path(raw)
        if path.exists():
            args += ["--ro-bind", raw, raw]
    args += ["--dir", "/etc"]
    for raw in (
        "/etc/ssl",
        "/etc/ca-certificates",
        "/etc/pki",
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
        "/etc/passwd",
        "/etc/group",
        "/etc/localtime",
    ):
        if Path(raw).exists():
            args += ["--ro-bind", raw, raw]
    return args


def _mkdir_mount_parent_args(path: Path) -> list[str]:
    if not path.is_absolute():
        raise HarborBridgeUnavailable("Python base_prefix must be absolute")
    args: list[str] = []
    current = Path("/")
    for part in path.parts[1:-1]:
        current /= part
        args += ["--dir", str(current)]
    return args


def _resolve_bwrap(path: str | Path) -> Path:
    candidate = str(path)
    resolved = shutil.which(candidate)
    if resolved is None:
        raise HarborBridgeUnavailable(f"bubblewrap executable not found: {candidate}")
    return Path(resolved).resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _base_bwrap_argv(
    *,
    bwrap: Path,
    trusted_harbor_root: Path,
    python_metadata: Mapping[str, Any],
) -> list[str]:
    base_prefix = Path(python_metadata["base_prefix"])
    guest_base_prefix = Path(python_metadata["guest_base_prefix"])
    argv = [
        str(bwrap),
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-net",
        "--cap-drop",
        "ALL",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
    ]
    argv.extend(_system_mount_args())
    argv += [
        "--dir",
        "/opt",
        "--dir",
        "/rsi-arena",
        "--dir",
        str(_GUEST_BRIDGE_ROOT),
        "--dir",
        str(_GUEST_LOGS_ROOT),
        "--dir",
        str(_GUEST_RPC_DIR),
        "--dir",
        str(_GUEST_PROVIDER_DIR),
    ]
    argv.extend(_mkdir_mount_parent_args(guest_base_prefix))
    argv += [
        "--ro-bind",
        str(base_prefix),
        str(guest_base_prefix),
        "--dir",
        str(_GUEST_HARBOR_ROOT),
        "--dir",
        str(_GUEST_HARBOR_ROOT / "src"),
        "--ro-bind",
        str(trusted_harbor_root / ".venv"),
        str(_GUEST_HARBOR_ROOT / ".venv"),
        "--ro-bind",
        str(trusted_harbor_root / "src" / "harbor"),
        str(_GUEST_HARBOR_ROOT / "src" / "harbor"),
    ]
    return argv


def _empty_launcher_environment() -> dict[str, str]:
    """Environment for the host-side bwrap process itself.

    Bubblewrap 0.4 (used on the experiment host) has no ``--clearenv`` flag.
    Passing an explicit empty environment to subprocess is the equivalent
    fail-closed primitive; candidate/model variables are installed *inside*
    bwrap only through explicit ``--setenv`` arguments.
    """

    return {}


def _provider_worker_bwrap_args(
    *, provider_broker_module: Path, socket_parent: Path, credential_file: Path
) -> list[str]:
    """Mount only broker transport and a per-run bearer into the worker."""

    return [
        "--ro-bind",
        str(provider_broker_module),
        str(_GUEST_BRIDGE_ROOT / provider_broker_module.name),
        "--ro-bind",
        str(socket_parent),
        str(_GUEST_PROVIDER_DIR),
        "--ro-bind",
        str(credential_file),
        str(_GUEST_CREDENTIAL),
    ]


def harbor_agent_bridge_identity() -> str:
    """Return a deterministic identity for the shared bridge source bytes."""

    worker = Path(__file__).with_name("agent_worker.py").resolve()
    module = Path(__file__).resolve()
    provider_broker = Path(__file__).with_name("provider_broker.py").resolve()
    if not worker.is_file() or not module.is_file() or not provider_broker.is_file():
        raise HarborBridgeUnavailable("trusted Harbor bridge worker/module is missing")
    identity = {
        "schema": BRIDGE_SCHEMA,
        "worker_sha256": "sha256:" + hashlib.sha256(worker.read_bytes()).hexdigest(),
        "module_sha256": "sha256:" + hashlib.sha256(module.read_bytes()).hexdigest(),
        "provider_broker_sha256": "sha256:"
        + hashlib.sha256(provider_broker.read_bytes()).hexdigest(),
    }
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()


@dataclass(frozen=True)
class _StagedCandidate:
    mount_source: Path
    mount_target: PurePosixPath
    python_import_root: PurePosixPath
    source: Path | None


def _stage_candidate_agent(
    candidate_checkout: Path,
    stage_parent: Path,
    *,
    limits: ArtifactLimits,
    agent_ref: str,
) -> _StagedCandidate:
    checkout = candidate_checkout.resolve()
    custom = agent_ref != "builtin:terminus-2"
    if custom:
        parsed = _parse_python_inner_agent_ref(agent_ref)
        if parsed is None:
            raise HarborBridgeUnavailable("candidate inner-agent ref is unsupported")
        top_level = parsed[0].split(".", 1)[0]
        candidates: list[Path] = []
        for import_parent in (checkout / "src", checkout):
            package = import_parent / top_level
            module_file = import_parent / f"{top_level}.py"
            if package.is_dir():
                candidates.append(package)
            elif module_file.is_file():
                candidates.append(module_file)
        source = candidates[0] if candidates else None
        import_root = stage_parent / "python-root"
        import_root.mkdir(mode=0o700)
        if source is None:
            import_root.chmod(0o500)
            return _StagedCandidate(
                mount_source=import_root,
                mount_target=_GUEST_CUSTOM_AGENT_ROOT,
                python_import_root=_GUEST_CUSTOM_AGENT_ROOT,
                source=None,
            )
        destination = import_root / source.name
    else:
        source = checkout / "src" / "harbor" / "agents" / "terminus_2"
        if not source.is_dir():
            raise HarborBridgeUnavailable(
                "candidate checkout lacks src/harbor/agents/terminus_2"
            )
        import_root = stage_parent
        destination = stage_parent / "terminus_2"

    if source.is_file():
        info = source.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_size > limits.max_file_bytes
        ):
            raise HarborBridgeUnavailable(
                "custom agent module is not a bounded regular file"
            )
        source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            target_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o400,
            )
            try:
                copied = 0
                while chunk := os.read(source_fd, 1024 * 1024):
                    copied += len(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(target_fd, view)
                        view = view[written:]
                if copied != info.st_size:
                    raise HarborBridgeUnavailable(
                        "candidate inner agent changed while creating fixed overlay"
                    )
            finally:
                os.close(target_fd)
        finally:
            os.close(source_fd)
        import_root.chmod(0o500)
        return _StagedCandidate(
            mount_source=import_root,
            mount_target=_GUEST_CUSTOM_AGENT_ROOT,
            python_import_root=_GUEST_CUSTOM_AGENT_ROOT,
            source=source,
        )

    destination.mkdir(mode=0o700)
    entries = _validate_snapshot_tree(source, limits)
    for relative, path, info, _link_target in entries:
        target = destination.joinpath(*PurePosixPath(relative).parts)
        if stat.S_ISDIR(info.st_mode):
            target.mkdir(mode=0o700)
        else:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(source_fd)
                if (
                    opened.st_ino != info.st_ino
                    or opened.st_dev != info.st_dev
                    or opened.st_size != info.st_size
                    or not stat.S_ISREG(opened.st_mode)
                ):
                    raise HarborBridgeUnavailable(
                        "candidate inner agent changed while creating fixed overlay"
                    )
                target_fd = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o500 if info.st_mode & 0o111 else 0o400,
                )
                try:
                    copied = 0
                    while True:
                        chunk = os.read(source_fd, 1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(target_fd, view)
                            view = view[written:]
                    if copied != info.st_size:
                        raise HarborBridgeUnavailable(
                            "candidate inner agent changed while creating fixed overlay"
                        )
                finally:
                    os.close(target_fd)
            finally:
                os.close(source_fd)
    for directory in sorted(
        (path for path in destination.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        directory.chmod(0o500)
    destination.chmod(0o500)
    if custom:
        import_root.chmod(0o500)
        return _StagedCandidate(
            mount_source=import_root,
            mount_target=_GUEST_CUSTOM_AGENT_ROOT,
            python_import_root=_GUEST_CUSTOM_AGENT_ROOT,
            source=source,
        )
    return _StagedCandidate(
        mount_source=destination,
        mount_target=_GUEST_BUILTIN_AGENT_ROOT,
        python_import_root=_GUEST_HARBOR_ROOT / "src",
        source=source,
    )


def _sanitize_child_environment(extra_env: Mapping[str, Any]) -> dict[str, str]:
    # Candidate configuration has a typed JSON channel.  Accepting an ambient
    # Harbor environment here would create a second, unreviewed credential
    # delivery path and could silently reintroduce a long-lived provider key.
    if extra_env:
        raise HarborBridgeUnavailable(
            "isolated candidate workers do not accept ambient environment entries"
        )
    return {}


def _validated_credential_handle(path: Path | str) -> Path:
    """Lazily call the host utility without adding it to the bwrap mount."""

    try:
        from rsi_arena.benchmarks.harbor_runtime.execution import (
            HarborExecutionError,
            validate_credential_handle,
        )

        return validate_credential_handle(Path(path))
    except (OSError, HarborExecutionError) as exc:
        raise HarborBridgeUnavailable(str(exc)) from exc


def _scrub_candidate_credential_logs(
    root: Path, value: str, *, preserve_primary_failure: bool = False
) -> None:
    """Host-side exact-literal scrub after the candidate process group is gone."""

    try:
        from rsi_arena.benchmarks.harbor_runtime.execution import (
            HarborExecutionError,
            _CredentialLogScanBudgetExceeded,
            scrub_credential_literal_files,
        )

        scrub_credential_literal_files(root, Path(root).rglob("*"), value)
    except (OSError, HarborExecutionError) as exc:
        if preserve_primary_failure and isinstance(
            exc, _CredentialLogScanBudgetExceeded
        ):
            # This type is raised only after the scoped purge completes.
            return
        raise HarborBridgeError(
            "candidate-writable logs failed credential scrubbing"
        ) from exc


def _provider_credential_delivery_env(provider_name: str, source_env_name: str) -> str:
    """Return the provider SDK alias used only inside the trusted child."""

    if provider_name.casefold() == "openrouter":
        return "OPENROUTER_API_KEY"
    return source_env_name


def _terminus_provider_model_name(model_name: object, provider_name: object) -> str:
    model = str(model_name)
    if not model:
        raise HarborBridgeUnavailable("provider model name is empty")
    if (
        isinstance(provider_name, str)
        and provider_name.casefold() == "openrouter"
        and not model.startswith("openrouter/")
    ):
        return f"openrouter/{model}"
    return model


def _safe_provider_delivery(
    provider_name: object,
    base_url: object,
    base_url_env_name: object,
) -> tuple[str, str, str]:
    """Reject credential/control injection without validating an operator route."""

    provider = str(provider_name)
    url = str(base_url)
    env_name = str(base_url_env_name)
    parsed = urllib.parse.urlsplit(url)
    if (
        provider.casefold() != "openrouter"
        or _CONTROL_RE.search(provider)
        or _CONTROL_RE.search(url)
        or not env_name.isidentifier()
        or env_name.upper() != env_name
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or url != "https://openrouter.ai/api/v1"
    ):
        raise HarborBridgeUnavailable("provider delivery contains unsafe values")
    return provider, url, env_name


def _trusted_candidate_launch_kwargs(
    candidate_kwargs: Mapping[str, Any],
    *,
    provider_base_url: str | None,
    reasoning_effort: str | None = None,
    reasoning_enabled: bool | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    """Inject trusted Terminus constructor settings after rejecting conflicts."""

    conflicts = _TRUSTED_CANDIDATE_KWARGS.intersection(candidate_kwargs)
    if conflicts:
        names = ", ".join(sorted(conflicts))
        raise HarborBridgeUnavailable(
            f"candidate kwargs contain reserved trusted keys: {names}"
        )
    launch_kwargs = dict(candidate_kwargs)
    if provider_base_url is not None:
        launch_kwargs["api_base"] = provider_base_url
    if reasoning_effort is not None:
        if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
            raise HarborBridgeUnavailable("reasoning effort must be a non-empty string")
        launch_kwargs["reasoning_effort"] = reasoning_effort
    if reasoning_enabled is not None and not isinstance(reasoning_enabled, bool):
        raise HarborBridgeUnavailable("reasoning_enabled must be boolean")
    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(float(temperature))
    ):
        raise HarborBridgeUnavailable("temperature must be finite")
    reasoning: dict[str, Any] = {}
    if reasoning_effort is not None:
        reasoning["effort"] = reasoning_effort
    if reasoning_enabled is not None:
        reasoning["enabled"] = reasoning_enabled
        if reasoning_enabled:
            reasoning["exclude"] = False
    if reasoning:
        launch_kwargs["llm_call_kwargs"] = {"extra_body": {"reasoning": reasoning}}
    if temperature is not None:
        launch_kwargs["temperature"] = float(temperature)
    return launch_kwargs


def _codec_from_name(name: str, payload_name: str | None = None) -> ArtifactCodec:
    if name == "native_state":
        return NativeStateCodec()
    if name == "integer_answer":
        return IntegerAnswerCodec(payload_name=payload_name or "answer.txt")
    if name == "text_answer":
        if payload_name not in {None, "final-answer.txt"}:
            raise HarborBridgeUnavailable("text answer payload name is fixed")
        return TextAnswerCodec()
    if name == "tree_snapshot":
        return TreeSnapshotCodec(payload_name=payload_name or "tree.tar")
    raise HarborBridgeUnavailable(f"unsupported trusted artifact codec: {name!r}")


def harbor_cli_agent_args(
    *,
    agent_ref: str,
    agent_config: Mapping[str, object],
    benchmark_id: str,
    candidate_checkout: Path | str,
    trusted_harbor_root: Path | str,
    codec: str,
    source: str,
    nonce: str,
    candidate_tree_digest: str,
    task_public_digest: str,
    bridge_identity_digest: str,
    envelope_key_file: Path | str,
    worker_timeout_seconds: float,
    cleanup_grace_seconds: float,
    bwrap_path: Path | str = "bwrap",
    payload_name: str | None = None,
    credential_handle: Path | str | None = None,
    credential_env_name: str | None = None,
    provider_name: str | None = None,
    provider_base_url: str | None = None,
    provider_base_url_env_name: str | None = None,
    reasoning_effort: str | None = None,
    reasoning_enabled: bool | None = None,
    temperature: float | None = None,
    settled_cost_stop_usd: float | None = None,
) -> tuple[str, ...]:
    """Return the exact custom-agent portion to append to ``harbor run``."""

    if (
        agent_ref != "builtin:terminus-2"
        and _parse_python_inner_agent_ref(agent_ref) is None
    ):
        raise HarborBridgeUnavailable("candidate inner-agent ref is unsupported")
    if not isinstance(benchmark_id, str) or not benchmark_id:
        raise HarborBridgeUnavailable("benchmark_id is required")
    try:
        config_json = json.dumps(
            dict(agent_config),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise HarborBridgeUnavailable("inner-agent config is invalid") from exc
    worker_timeout = int(worker_timeout_seconds)
    cleanup_grace = float(cleanup_grace_seconds)
    if settled_cost_stop_usd is not None and (
        isinstance(settled_cost_stop_usd, bool)
        or not isinstance(settled_cost_stop_usd, (int, float))
        or not math.isfinite(float(settled_cost_stop_usd))
        or float(settled_cost_stop_usd) < 0.0
    ):
        raise HarborBridgeUnavailable(
            "settled provider cost stop must be finite and non-negative"
        )
    kwargs = {
        "agent_ref": agent_ref,
        "agent_config_json": config_json,
        "benchmark_id": benchmark_id,
        "candidate_checkout": str(Path(candidate_checkout).resolve()),
        "trusted_harbor_root": str(Path(trusted_harbor_root).resolve()),
        "codec": codec,
        "source": source,
        "nonce": _validate_nonce(nonce),
        "candidate_tree_digest": candidate_tree_digest,
        "task_public_digest": task_public_digest,
        "bridge_identity_digest": bridge_identity_digest,
        "envelope_key_file": str(Path(envelope_key_file).resolve()),
        "bwrap_path": str(bwrap_path),
        "worker_timeout_seconds": worker_timeout,
        "cleanup_grace_seconds": cleanup_grace,
    }
    if payload_name is not None:
        kwargs["payload_name"] = payload_name
    if (credential_handle is None) != (credential_env_name is None):
        raise HarborBridgeUnavailable(
            "credential handle and environment name must be supplied together"
        )
    resolved_credential_env_name = credential_env_name
    if credential_handle is not None and credential_env_name is not None:
        if (
            not credential_env_name.isidentifier()
            or credential_env_name.upper() != credential_env_name
        ):
            raise HarborBridgeUnavailable("credential environment name is invalid")
        handle = _validated_credential_handle(credential_handle)
        kwargs["credential_handle"] = str(handle)
    provider_values = (
        provider_name,
        provider_base_url,
        provider_base_url_env_name,
    )
    if any(value is not None for value in provider_values):
        if not all(value is not None for value in provider_values):
            raise HarborBridgeUnavailable(
                "provider name, base URL, and environment name must be supplied together"
            )
        resolved_provider, resolved_url, resolved_env = _safe_provider_delivery(
            provider_name,
            provider_base_url,
            provider_base_url_env_name,
        )
        if resolved_credential_env_name is not None:
            resolved_credential_env_name = _provider_credential_delivery_env(
                resolved_provider, resolved_credential_env_name
            )
        if resolved_env == resolved_credential_env_name:
            raise HarborBridgeUnavailable(
                "provider base URL and credential environment names must differ"
            )
        kwargs["provider_name"] = resolved_provider
        kwargs["provider_base_url"] = resolved_url
        kwargs["provider_base_url_env_name"] = resolved_env
    if credential_handle is not None and resolved_credential_env_name is not None:
        kwargs["credential_env_name"] = resolved_credential_env_name
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    if reasoning_enabled is not None:
        kwargs["reasoning_enabled"] = reasoning_enabled
    if temperature is not None:
        kwargs["temperature"] = temperature
    if settled_cost_stop_usd is not None:
        kwargs["settled_cost_stop_usd"] = float(settled_cost_stop_usd)
    argv: list[str] = ["--agent", AGENT_IMPORT_PATH]
    for key, value in kwargs.items():
        argv += ["--agent-kwarg", f"{key}={value}"]
    return tuple(argv)


class IsolatedTerminus2Agent(_HarborBaseAgent):
    """Trusted BaseAgent wrapper around candidate-controlled Terminus-2 code."""

    SUPPORTS_ATIF = True
    SUPPORTS_RESUME = False
    SUPPORTS_WINDOWS = False

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        agent_ref: str,
        agent_config_json: str | Mapping[str, object],
        benchmark_id: str,
        candidate_checkout: str,
        trusted_harbor_root: str,
        codec: str,
        source: str,
        nonce: str,
        candidate_tree_digest: str,
        task_public_digest: str,
        bridge_identity_digest: str,
        envelope_key_file: str,
        bwrap_path: str = "bwrap",
        payload_name: str | None = None,
        credential_handle: str | None = None,
        credential_env_name: str | None = None,
        provider_name: str | None = None,
        provider_base_url: str | None = None,
        provider_base_url_env_name: str | None = None,
        reasoning_effort: str | None = None,
        reasoning_enabled: bool | None = None,
        temperature: float | None = None,
        settled_cost_stop_usd: float | None = None,
        worker_timeout_seconds: int = 14_400,
        cleanup_grace_seconds: float,
        logger: Any | None = None,
        mcp_servers: list[Any] | None = None,
        skills_dir: str | None = None,
        extra_env: dict[str, str] | None = None,
        **candidate_kwargs: Any,
    ) -> None:
        # Deliberately do not give explicit model/API variables to BaseAgent:
        # Harbor wraps run() in scoped_exec_env(self.extra_env), which would
        # otherwise inject those secrets into the task container.  They belong
        # only to the candidate worker OS environment.
        super().__init__(logs_dir=logs_dir, model_name=model_name, logger=logger)
        if model_name is None:
            raise HarborBridgeUnavailable("model_name is required")
        if (
            agent_ref != "builtin:terminus-2"
            and _parse_python_inner_agent_ref(agent_ref) is None
        ):
            raise HarborBridgeUnavailable("candidate inner-agent ref is unsupported")
        if not isinstance(benchmark_id, str) or not benchmark_id:
            raise HarborBridgeUnavailable("benchmark_id is required")
        try:
            raw_agent_config = (
                json.loads(agent_config_json)
                if isinstance(agent_config_json, str)
                else dict(agent_config_json)
            )
            decoded_agent_config = json.loads(
                json.dumps(raw_agent_config, allow_nan=False)
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HarborBridgeUnavailable("inner-agent config is invalid") from exc
        if not isinstance(decoded_agent_config, dict):
            raise HarborBridgeUnavailable("inner-agent config must be an object")
        self._agent_ref = agent_ref
        self._agent_config = decoded_agent_config
        self._benchmark_id = benchmark_id
        if mcp_servers:
            raise HarborBridgeUnavailable(
                "MCP servers are not supported by the four-method fixed bridge"
            )
        if skills_dir is not None:
            candidate_kwargs.setdefault("skills_dir", skills_dir)
        _trusted_candidate_launch_kwargs(
            candidate_kwargs,
            provider_base_url=None,
            reasoning_effort=reasoning_effort,
            reasoning_enabled=reasoning_enabled,
            temperature=temperature,
        )
        self._child_extra_env = _sanitize_child_environment(extra_env or {})
        if (credential_handle is None) != (credential_env_name is None):
            raise HarborBridgeUnavailable(
                "credential handle and environment name must be supplied together"
            )
        self._credential_handle: Path | None = None
        self._credential_env_name: str | None = None
        self._credential_forbidden_literal: str | None = None
        if credential_handle is not None and credential_env_name is not None:
            if (
                not credential_env_name.isidentifier()
                or credential_env_name.upper() != credential_env_name
            ):
                raise HarborBridgeUnavailable("credential environment name is invalid")
            try:
                handle = _validated_credential_handle(credential_handle)
                raw_credential = handle.read_bytes()
            except (OSError, HarborBridgeUnavailable) as exc:
                raise HarborBridgeUnavailable(
                    "credential handle is unavailable"
                ) from exc
            if len(raw_credential) > 64 * 1024:
                raise HarborBridgeUnavailable("credential handle is oversized")
            try:
                credential_literal = raw_credential.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HarborBridgeUnavailable("credential handle is not UTF-8") from exc
            if not credential_literal or "\x00" in credential_literal:
                raise HarborBridgeUnavailable("credential handle value is invalid")
            self._credential_handle = handle
            self._credential_env_name = credential_env_name
            self._credential_forbidden_literal = credential_literal
        provider_values = (
            provider_name,
            provider_base_url,
            provider_base_url_env_name,
        )
        self._provider_name: str | None = None
        self._provider_base_url: str | None = None
        self._provider_base_url_env_name: str | None = None
        if any(value is not None for value in provider_values):
            if not all(value is not None for value in provider_values):
                raise HarborBridgeUnavailable(
                    "provider route name, URL, and environment must be complete"
                )
            (
                self._provider_name,
                self._provider_base_url,
                self._provider_base_url_env_name,
            ) = _safe_provider_delivery(
                provider_name,
                provider_base_url,
                provider_base_url_env_name,
            )
            if self._provider_base_url_env_name == self._credential_env_name:
                raise HarborBridgeUnavailable(
                    "provider base URL and credential environment names must differ"
                )
        self._candidate_checkout = Path(candidate_checkout).resolve()
        self._trusted_harbor_root = Path(trusted_harbor_root).resolve()
        self._codec = _codec_from_name(codec, payload_name)
        self._source = _validate_remote_path(source, RpcLimits())
        self._nonce = _validate_nonce(nonce)
        self._candidate_tree_digest = _validate_public_digest(
            "candidate_tree_digest", candidate_tree_digest
        )
        self._task_public_digest = _validate_public_digest(
            "task_public_digest", task_public_digest
        )
        self._bridge_identity_digest = _validate_public_digest(
            "bridge_identity_digest", bridge_identity_digest
        )
        raw_key_path = Path(envelope_key_file).expanduser()
        # Validate the caller-provided path before resolve() so a symlink
        # cannot be laundered into an apparently regular key file.
        self._hmac_key = load_hmac_key_file(raw_key_path)
        key_path = raw_key_path.resolve()
        candidate_visible_root = (
            self._candidate_checkout
            if self._agent_ref.startswith("python:")
            else self._candidate_checkout / "src" / "harbor" / "agents" / "terminus_2"
        )
        child_visible_roots = (
            self._trusted_harbor_root,
            candidate_visible_root,
            Path(self.logs_dir).resolve(),
        )
        if any(_is_within(key_path, root) for root in child_visible_roots):
            raise HarborBridgeUnavailable(
                "envelope key file overlaps a child-visible bwrap mount"
            )
        if self._credential_handle is not None and any(
            _is_within(self._credential_handle, root) for root in child_visible_roots
        ):
            raise HarborBridgeUnavailable(
                "credential handle overlaps a child-visible bwrap mount"
            )
        self._bwrap = _resolve_bwrap(bwrap_path)
        self._worker_timeout_seconds = int(worker_timeout_seconds)
        if self._worker_timeout_seconds <= 0:
            raise HarborBridgeUnavailable("worker_timeout_seconds must be positive")
        self._worker_deadline_reached = False
        self._cleanup_grace_seconds = float(cleanup_grace_seconds)
        if (
            not math.isfinite(self._cleanup_grace_seconds)
            or self._cleanup_grace_seconds <= 0
        ):
            raise HarborBridgeUnavailable("cleanup_grace_seconds must be positive")
        if settled_cost_stop_usd is not None and (
            isinstance(settled_cost_stop_usd, bool)
            or not isinstance(settled_cost_stop_usd, (int, float))
            or not math.isfinite(float(settled_cost_stop_usd))
            or float(settled_cost_stop_usd) < 0.0
        ):
            raise HarborBridgeUnavailable(
                "settled provider cost stop must be finite and non-negative"
            )
        self._settled_cost_stop_usd = (
            None if settled_cost_stop_usd is None else float(settled_cost_stop_usd)
        )
        if agent_ref == "builtin:terminus-2":
            overlap = set(decoded_agent_config) & set(candidate_kwargs)
            if overlap:
                raise HarborBridgeUnavailable(
                    "inner-agent config collides with Harbor agent kwargs: "
                    + ", ".join(sorted(overlap))
                )
            self._candidate_kwargs = {**decoded_agent_config, **candidate_kwargs}
        else:
            self._candidate_kwargs = dict(candidate_kwargs)
        self._reasoning_effort = reasoning_effort
        self._reasoning_enabled = reasoning_enabled
        self._temperature = temperature
        self._bridge_identity = harbor_agent_bridge_identity()
        if not hmac.compare_digest(self._bridge_identity, self._bridge_identity_digest):
            raise HarborBridgeUnavailable(
                "bridge_identity_digest does not match the shared bridge source"
            )

    @staticmethod
    def name() -> str:
        return AGENT_NAME

    def version(self) -> str:
        return self._bridge_identity

    async def setup(self, environment: Any) -> None:
        # setup and run must happen in one candidate process because Terminus-2
        # stores its tmux session in memory.  No task call crosses this method.
        del environment

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        try:
            payload = await self._run_candidate_worker(instruction, environment)
        except asyncio.CancelledError as agent_timeout:
            # Harbor's agent deadline cancels BaseAgent.run() and waits for the
            # cancellation to finish before raising its typed AgentTimeoutError.
            # _run_candidate_worker has already killed/waited for the candidate
            # process group and scrubbed credentials before cancellation reaches
            # this frame. Answer codecs can still freeze a bounded final value;
            # SWE snapshots use the package-owned cleanup allowance.
            if self._codec.name in {"integer_answer", "text_answer"}:
                try:
                    async with asyncio.timeout(_TIMEOUT_ANSWER_EXPORT_SECONDS):
                        await self._export_candidate_artifact(environment)
                except _CandidateArtifactError:
                    raise agent_timeout from None
                except Exception as exc:
                    if isinstance(exc, HarborBridgeError):
                        raise
                    raise HarborBridgeError(
                        "trusted answer artifact harvest failed at agent timeout"
                    ) from exc
            elif self._codec.name == "tree_snapshot":
                await self._export_tree_snapshot_within_cleanup_budget(environment)
            raise
        clean = validate_agent_context_payload(payload)
        candidate_failure = candidate_runtime_failure_from_context(clean)
        try:
            from harbor.models.agent.context import AgentContext

            validated = AgentContext.model_validate(clean)
        except Exception as exc:
            raise AgentContextValidationError(
                "worker result failed Harbor AgentContext validation"
            ) from exc
        for field_name in validated.model_fields:
            setattr(context, field_name, getattr(validated, field_name))

        if candidate_failure is not None:
            # The fixed worker proved a candidate-overlay exception before a
            # normal result existed. SWE still requires a stopped-tree artifact
            # so its independent patch collector and verifier remain authoritative.
            if self._codec.name == "tree_snapshot":
                await self._export_tree_snapshot_within_cleanup_budget(environment)
            return

        try:
            await self._export_candidate_artifact(environment)
        except _CandidateArtifactError as exc:
            metadata = dict(context.metadata or {})
            metadata[CANDIDATE_RUNTIME_FAILURE_KEY] = (
                candidate_owned_runtime_failure_marker(
                    exc,
                    forbidden_literals=(
                        (self._credential_forbidden_literal,)
                        if self._credential_forbidden_literal is not None
                        else ()
                    ),
                )
            )
            context.metadata = metadata

    async def _export_tree_snapshot_within_cleanup_budget(
        self, environment: Any
    ) -> None:
        async with asyncio.timeout(self._cleanup_grace_seconds):
            await self._export_candidate_artifact(environment)

    async def _export_candidate_artifact(self, environment: Any) -> None:
        """Export once after the candidate process group has been reaped."""

        # Quiesce the task container, then download/validate and only then
        # publish host-owned artifacts.
        convention_dir = (
            Path(self.logs_dir).resolve().parent / "artifacts" / "logs" / "artifacts"
        )
        await self._codec.export(
            environment,
            source=self._source,
            convention_dir=convention_dir,
            nonce=self._nonce,
            candidate_tree_digest=self._candidate_tree_digest,
            task_public_digest=self._task_public_digest,
            bridge_identity_digest=self._bridge_identity_digest,
            hmac_key=self._hmac_key,
        )

    async def _run_candidate_worker(
        self, instruction: str, environment: Any
    ) -> dict[str, Any]:
        if (
            self._provider_base_url is None
            or self._credential_forbidden_literal is None
        ):
            raise HarborBridgeUnavailable(
                "candidate inner agent requires the scoped provider broker"
            )
        broker_model = _terminus_provider_model_name(
            self.model_name, self._provider_name
        )
        broker = ScopedProviderBroker(
            provider_bearer=self._credential_forbidden_literal,
            api_base=self._provider_base_url,
            model_name=broker_model,
            timeout_seconds=min(float(self._worker_timeout_seconds), 120.0),
            settled_cost_stop_usd=self._settled_cost_stop_usd,
        )
        broker.start()
        ephemeral_bearer = broker.ephemeral_bearer
        self._worker_deadline_reached = False
        try:
            payload = await self._run_candidate_worker_with_broker(
                instruction,
                environment,
                broker=broker,
                ephemeral_bearer=ephemeral_bearer,
            )
            clean = validate_agent_context_payload(payload)
            accounting = broker.accounting_snapshot()
            clean["n_input_tokens"] = accounting.input_tokens
            clean["n_cache_tokens"] = accounting.cache_tokens
            clean["n_output_tokens"] = accounting.output_tokens
            clean["cost_usd"] = accounting.cost_usd
            metadata = dict(clean.get("metadata") or {})
            metadata[PROVIDER_ACCOUNTING_METADATA_KEY] = {
                "accounting": accounting.as_dict(),
                "reliability": broker.reliability_snapshot().as_dict(),
            }
            clean["metadata"] = metadata
            return validate_agent_context_payload(clean)
        finally:
            await asyncio.to_thread(broker.quiesce)
            reliability, accounting, calls = broker.trial_diagnostics_snapshot()
            diagnostics = {
                "schema": PROVIDER_TRIAL_DIAGNOSTICS_SCHEMA,
                "worker_deadline_reached": self._worker_deadline_reached,
                "worker_timeout_seconds": self._worker_timeout_seconds,
                "accounting": accounting.as_dict(),
                "calls": [call.as_dict() for call in calls],
                "provider": reliability.as_dict(),
            }
            destination = (
                Path(self.logs_dir).resolve().parent / PROVIDER_DIAGNOSTICS_NAME
            )
            try:
                _exclusive_write(destination, _canonical_json_bytes(diagnostics))
            except FileExistsError as exc:
                raise HarborBridgeError(
                    "trusted provider diagnostics were pre-positioned"
                ) from exc
            finally:
                broker.close()

    async def _run_candidate_worker_with_broker(
        self,
        instruction: str,
        environment: Any,
        *,
        broker: ScopedProviderBroker,
        ephemeral_bearer: str,
    ) -> dict[str, Any]:
        limits = RpcLimits(max_wall_seconds=float(self._worker_timeout_seconds))
        root = self._trusted_harbor_root
        python_metadata = _trusted_python_metadata(root)
        worker = Path(__file__).with_name("agent_worker.py").resolve()
        module = Path(__file__).resolve()
        provider_broker_module = (
            Path(__file__).with_name("provider_broker.py").resolve()
        )
        with host_temporary_directory("agent") as temp:
            temp_root = Path(temp)
            candidate_stage_parent = temp_root / "candidate"
            candidate_stage_parent.mkdir(mode=0o700)
            staged = _stage_candidate_agent(
                self._candidate_checkout,
                candidate_stage_parent,
                limits=ArtifactLimits(),
                agent_ref=self._agent_ref,
            )
            socket_parent = temp_root / "socket-parent"
            credential_file = temp_root / "provider-credential"
            _exclusive_write(credential_file, ephemeral_bearer.encode("utf-8"))
            launch_file = temp_root / "input.json"
            launch_candidate_kwargs = _trusted_candidate_launch_kwargs(
                self._candidate_kwargs,
                provider_base_url=broker.base_url,
                reasoning_effort=self._reasoning_effort,
                reasoning_enabled=self._reasoning_enabled,
                temperature=self._temperature,
            )
            launch_payload = {
                "schema": BRIDGE_SCHEMA,
                "agent_ref": self._agent_ref,
                "candidate_overlay": str(staged.mount_target),
                "agent_config": dict(self._agent_config),
                "benchmark_id": self._benchmark_id,
                "nonce": self._nonce,
                "instruction": instruction,
                "model_name": self.model_name,
                "candidate_kwargs": launch_candidate_kwargs,
                "trusted_api_base": broker.base_url,
                "provider_socket": WORKER_PROVIDER_SOCKET,
                "default_user": getattr(environment, "default_user", None),
                "context_result": str(_GUEST_LOGS_ROOT / _CONTEXT_RESULT_NAME),
                "worker_status": str(_GUEST_LOGS_ROOT / _WORKER_STATUS_NAME),
                "rpc_socket": str(_GUEST_RPC_SOCKET),
                "rpc_limits": vars(limits),
                "credential_env_name": "OPENROUTER_API_KEY",
                "credential_file": str(_GUEST_CREDENTIAL),
                "model_runtime": {
                    "model_name": self.model_name,
                    "provider_name": self._provider_name,
                    "provider_base_url": broker.base_url,
                    "provider_base_url_env_name": self._provider_base_url_env_name,
                    "credential_env_name": "OPENROUTER_API_KEY",
                    "reasoning_effort": self._reasoning_effort,
                    "reasoning_enabled": self._reasoning_enabled,
                    "temperature": self._temperature,
                    "agent_constructor_kwargs": launch_candidate_kwargs,
                },
            }
            _exclusive_write(launch_file, _canonical_json_bytes(launch_payload))
            context_path = Path(self.logs_dir) / _CONTEXT_RESULT_NAME
            status_path = Path(self.logs_dir) / _WORKER_STATUS_NAME
            for output in (context_path, status_path):
                if output.exists() or output.is_symlink():
                    raise HarborBridgeError(
                        f"candidate pre-positioned worker result: {output.name}"
                    )

            async with HarborRpcServer(
                socket_parent / _GUEST_RPC_SOCKET.name,
                nonce=self._nonce,
                environment=environment,
                limits=limits,
                forbidden_literals=tuple(
                    value for value in self._child_extra_env.values() if len(value) >= 4
                )
                + (
                    (self._credential_forbidden_literal,)
                    if self._credential_forbidden_literal is not None
                    and len(self._credential_forbidden_literal) >= 4
                    else ()
                )
                + (ephemeral_bearer,),
            ):
                argv = _base_bwrap_argv(
                    bwrap=self._bwrap,
                    trusted_harbor_root=root,
                    python_metadata=python_metadata,
                )
                if staged.mount_target == _GUEST_CUSTOM_AGENT_ROOT:
                    argv += [
                        "--dir",
                        str(_GUEST_CUSTOM_AGENT_ROOT),
                    ]
                argv += [
                    "--ro-bind",
                    str(staged.mount_source),
                    str(staged.mount_target),
                ]
                argv += [
                    "--ro-bind",
                    str(worker),
                    str(_GUEST_BRIDGE_ROOT / worker.name),
                    "--ro-bind",
                    str(module),
                    str(_GUEST_BRIDGE_ROOT / module.name),
                    "--ro-bind",
                    str(launch_file),
                    str(_GUEST_INPUT),
                    "--bind",
                    str(Path(self.logs_dir).resolve()),
                    str(_GUEST_LOGS_ROOT),
                    "--ro-bind",
                    str(socket_parent),
                    str(_GUEST_RPC_DIR),
                ]
                argv += _provider_worker_bwrap_args(
                    provider_broker_module=provider_broker_module,
                    socket_parent=broker.socket_path.parent,
                    credential_file=credential_file,
                )
                child_env = dict(self._child_extra_env)
                provider_env_name = getattr(self, "_provider_base_url_env_name", None)
                provider_base_url = broker.base_url
                if provider_env_name is not None:
                    if provider_base_url is None:
                        raise HarborBridgeUnavailable(
                            "provider route lost its resolved base URL"
                        )
                    child_env[provider_env_name] = provider_base_url
                child_env.update(
                    {
                        "HOME": "/tmp",
                        "TMPDIR": "/tmp",
                        "PATH": "/usr/local/bin:/usr/bin:/bin",
                        "PYTHONPATH": (
                            f"{staged.python_import_root}:"
                            f"{_GUEST_HARBOR_ROOT / 'src'}:"
                            f"{python_metadata['site_packages']}:{_GUEST_BRIDGE_ROOT}"
                        ),
                        "PYTHONNOUSERSITE": "1",
                    }
                )
                for key, value in child_env.items():
                    argv += ["--setenv", key, value]
                argv += [
                    "--chdir",
                    str(_GUEST_LOGS_ROOT),
                    "--",
                    str(python_metadata["guest_python"]),
                    str(_GUEST_BRIDGE_ROOT / worker.name),
                    str(_GUEST_INPUT),
                ]
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    # Candidate code controls both streams; piping them into
                    # communicate() would permit an unbounded host-memory DoS.
                    # The trusted worker writes a separately bounded status
                    # document for diagnostics.
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                    env=_empty_launcher_environment(),
                )
                cancelled = False
                try:
                    from .worker_deadline import wait_for_worker_deadline

                    self._worker_deadline_reached = await wait_for_worker_deadline(
                        process,
                        timeout_seconds=self._worker_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                finally:
                    # Kill the entire group even after the bwrap leader reports
                    # normal exit: candidate code may have forked a detached
                    # descendant before returning from Terminus.run().
                    _kill_process_group(process)
                    try:
                        if process.returncode is None:
                            await process.wait()
                    finally:
                        if self._credential_forbidden_literal is not None:
                            _scrub_candidate_credential_logs(
                                Path(self.logs_dir),
                                self._credential_forbidden_literal,
                                preserve_primary_failure=cancelled,
                            )
                        _scrub_candidate_credential_logs(
                            Path(self.logs_dir),
                            ephemeral_bearer,
                            preserve_primary_failure=cancelled,
                        )
                if process.returncode != 0 and not self._worker_deadline_reached:
                    detail = ""
                    if status_path.exists() and not status_path.is_symlink():
                        try:
                            failed_status = _safe_load_json_file(
                                status_path, max_bytes=64 * 1024
                            )
                            if isinstance(failed_status, dict):
                                detail = str(failed_status.get("message", ""))[:2048]
                        except (OSError, HarborBridgeError):
                            pass
                    raise HarborBridgeError(
                        f"isolated Terminus-2 worker failed ({process.returncode}): {detail}"
                    )

            if self._worker_deadline_reached:
                return {}
            status = _safe_load_json_file(status_path, max_bytes=64 * 1024)
            payload = _safe_load_json_file(context_path, max_bytes=64 * 1024 * 1024)
            forbidden_literals = (
                (self._credential_forbidden_literal,)
                if self._credential_forbidden_literal is not None
                else ()
            ) + (ephemeral_bearer,)
            context_failure = candidate_runtime_failure_from_context(
                payload, forbidden_literals=forbidden_literals
            )
            if status == {"ok": True}:
                if context_failure is not None:
                    raise HarborBridgeError(
                        "successful worker returned a reserved failure marker"
                    )
            elif (
                isinstance(status, dict)
                and set(status)
                == {
                    "ok",
                    CANDIDATE_RUNTIME_FAILURE_KEY,
                }
                and status.get("ok") is False
            ):
                status_failure = validated_candidate_runtime_failure_marker(
                    status[CANDIDATE_RUNTIME_FAILURE_KEY],
                    forbidden_literals=forbidden_literals,
                )
                raw_metadata = (
                    payload.get("metadata") if isinstance(payload, dict) else None
                )
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"metadata"}
                    or not isinstance(raw_metadata, dict)
                    or set(raw_metadata) != {CANDIDATE_RUNTIME_FAILURE_KEY}
                ):
                    raise HarborBridgeError(
                        "worker candidate failure context is not minimal"
                    )
                if context_failure != status_failure:
                    raise HarborBridgeError(
                        "worker candidate failure marker did not cross-check"
                    )
                clean_payload = dict(payload)
                clean_metadata = dict(clean_payload.get("metadata") or {})
                clean_metadata[CANDIDATE_RUNTIME_FAILURE_KEY] = status_failure
                clean_payload["metadata"] = clean_metadata
                payload = clean_payload
            else:
                raise HarborBridgeError("worker did not return a recognized status")
            if self._credential_forbidden_literal is not None:
                try:
                    _reject_forbidden_rpc_literals(
                        {"params": payload}, (self._credential_forbidden_literal,)
                    )
                except RpcProtocolError as exc:
                    raise HarborBridgeError(
                        "worker context contained a provider credential"
                    ) from exc
            return validate_agent_context_payload(payload)


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


__all__ = [
    "AGENT_IMPORT_PATH",
    "AGENT_NAME",
    "BRIDGE_SCHEMA",
    "ENVELOPE_SCHEMA",
    "ArtifactLimits",
    "ArtifactValidationError",
    "CodecResult",
    "HarborBridgeError",
    "HarborBridgeUnavailable",
    "HarborRpcDispatcher",
    "HarborRpcServer",
    "IntegerAnswerCodec",
    "IsolatedTerminus2Agent",
    "NativeStateCodec",
    "RpcLimitExceeded",
    "RpcLimits",
    "RpcProtocolError",
    "TextAnswerCodec",
    "TreeSnapshotCodec",
    "decode_rpc_request",
    "harbor_agent_bridge_identity",
    "harbor_cli_agent_args",
    "load_hmac_key_file",
    "validate_agent_context_payload",
    "verify_signed_envelope",
    "write_signed_envelope",
]
