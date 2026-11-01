"""Isolated loader for an evolved Terminus-2 program checkout.

The trusted Harbor controller imports :class:`CandidateTerminus2Connector` from
this module.  Candidate code is never imported into that controller process.
Instead, the connector stages only ``src/harbor/agents/terminus_2`` from the
commit-bound checkout and executes it in a bubblewrap PID/filesystem namespace.
The candidate can interact with Harbor's task environment only through four
bounded RPC operations.  The controller, Docker authority, verifier files, Git
checkout, and the rest of the AgentPilot runtime are not mounted into the child.

This boundary deliberately does not claim authoritative provider cost: the
candidate-produced ``AgentContext`` is validated and retained as diagnostic
usage only.  Formal cost reconciliation remains a later release gate.
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
import signal
import stat
import subprocess
import sys
import tempfile
import traceback
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

if __package__:
    from .controller import (
        APPROVED_HARBOR_CONTROLLER_COMMIT,
        APPROVED_HARBOR_CONTROLLER_TREE,
    )
    from .controller import (
        HarborRuntimeError as _ProgramRuntimeBase,
    )
    from .controller import (
        scrub_secret_literals as _scrub_secret_literals,
    )
    from .provider_broker import (
        PROVIDER_DIAGNOSTICS_NAME,
        PROVIDER_TRIAL_DIAGNOSTICS_SCHEMA,
        WORKER_PROVIDER_BASE_URL,
        WORKER_PROVIDER_SOCKET,
        ProviderAccountingSnapshot,
        ProviderCallAccounting,
        ProviderReliabilitySnapshot,
        ScopedProviderBroker,
        UnixProviderRelay,
        host_temporary_directory,
        provider_wait_dominated_deadline,
    )
else:  # pragma: no cover - constants for the isolated standalone worker only.
    APPROVED_HARBOR_CONTROLLER_COMMIT = "7db020ba5a5ceee918351dd8fc374d4d60bad442"
    APPROVED_HARBOR_CONTROLLER_TREE = "03bea3ef9612a7381a2fa2f3955eaeb22324e9ff"
    _ProgramRuntimeBase = RuntimeError
    _scrub_secret_literals = None
    PROVIDER_TRIAL_DIAGNOSTICS_SCHEMA = "rsi-arena.provider-trial"
    from provider_broker import (  # type: ignore[no-redef]
        WORKER_PROVIDER_BASE_URL,
        WORKER_PROVIDER_SOCKET,
        UnixProviderRelay,
    )
AGENT_IMPORT_PATH = (
    "rsi_arena.benchmarks.harbor_runtime.program_connector:CandidateTerminus2Connector"
)
AGENT_NAME = "rsi-arena-program-isolated-terminus-2"
CONNECTOR_IDENTITY = "rsi-arena-program-connector"
BRIDGE_SCHEMA = "rsi-arena.program-agent-rpc"
# The broker writes these into its own 429 body and the isolated worker reads
# them back to tell one of our ceilings from an upstream refusal. The worker runs
# as a standalone script with no package, so it cannot import them from the
# broker; the copies are pinned equal by test instead.
BUDGET_EXHAUSTED_MARKERS = (
    "request budget exhausted",
    "provider budget exhausted",
    "provider settled-cost stop reached",
)
CANDIDATE_SUBTREE = PurePosixPath("src/harbor/agents/terminus_2")

_GUEST_HARBOR_ROOT = PurePosixPath("/opt/harbor")
_GUEST_BRIDGE_ROOT = PurePosixPath("/rsi-arena/bridge")
_GUEST_LOGS_ROOT = PurePosixPath("/rsi-arena/logs")
_GUEST_OUTPUT_ROOT = PurePosixPath("/rsi-arena/output")
_GUEST_INPUT = PurePosixPath("/rsi-arena/input.json")
_GUEST_RPC_ROOT = PurePosixPath("/rsi-arena/rpc")
_GUEST_RPC_SOCKET = _GUEST_RPC_ROOT / "environment.sock"
_GUEST_PROVIDER_ROOT = PurePosixPath("/rsi-arena/provider")
_GUEST_CREDENTIAL = PurePosixPath("/rsi-arena/provider-credential")
_CONTEXT_RESULT_NAME = "agent-context.json"
_WORKER_STATUS_NAME = "worker-status.json"
PROVIDER_DIAGNOSTICS_METADATA_KEY = "rsi_arena_program_provider"
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_RPC_METHODS = frozenset({"exec", "is_dir", "upload_file", "download_file"})
#: The only upstream this boundary will talk to.  Both halves of the bridge
#: refuse anything else, and callers assembling a launcher read it from here
#: rather than repeating the URL, so an endpoint change cannot leave one side
#: pointed somewhere the other rejects.
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
# LiteLLM's OpenRouter adapter does not support an experiment-specific key
# variable. The controller may read any explicitly configured host variable,
# but the isolated worker receives only a short-lived broker bearer and must
# expose that bearer under LiteLLM's canonical name.
_LITELLM_OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
_AGENT_CONTEXT_FIELDS = frozenset(
    {
        "n_input_tokens",
        "n_cache_tokens",
        "n_output_tokens",
        "cost_usd",
        "rollout_details",
        "metadata",
    }
)


class ProgramRuntimeError(_ProgramRuntimeBase):
    """The evolved-program boundary could not be established or completed."""


class ProgramWorkerStageError(ProgramRuntimeError):
    """A worker phase failed; carries only bounded type metadata, never payloads."""

    def __init__(self, stage: str, cause_type: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", stage) is None:
            raise ValueError("worker failure stage is invalid")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", cause_type) is None:
            raise ValueError("worker failure type is invalid")
        self.stage = stage
        self.cause_type = cause_type
        super().__init__(f"worker stage failed ({stage}:{cause_type})")


class CandidateOverlayError(ProgramRuntimeError):
    """The commit-bound Terminus-2 subtree is not safe to expose read-only."""


class ProgramBridgeUnavailable(ProgramRuntimeError):
    """A required trusted OS/Harbor primitive is absent or has drifted."""


class ProgramRpcError(ProgramRuntimeError):
    """The candidate sent a malformed or out-of-policy RPC request."""


class ProgramRpcLimitExceeded(ProgramRpcError):
    """A fixed RPC resource budget was exhausted."""


class CandidateContextError(ProgramRuntimeError):
    """The worker returned an invalid or credential-bearing AgentContext."""


def deadline_was_an_upstream_stall(
    snapshot: ProviderReliabilitySnapshot, *, worker_timeout_seconds: float
) -> bool:
    """Attribute only deadlines dominated by observed transport failures.

    The legacy function name is retained for compatibility. A slow or pending
    generation alone is not evidence of provider fault.
    """

    return provider_wait_dominated_deadline(
        snapshot,
        worker_timeout_seconds=worker_timeout_seconds,
    )


def _standalone_scrub_secret_literals(
    root: Path,
    values: Sequence[str],
    *,
    max_paths: int = 10_000,
    max_total_bytes: int = 512 * 1024 * 1024,
) -> None:
    """Worker-probe fallback for the package-level credential scrubber."""

    literals = tuple(value.encode("utf-8") for value in values if value)
    if not literals:
        return
    boundary = root.resolve(strict=True)
    paths = tuple(boundary.rglob("*"))
    if len(paths) > max_paths:
        raise ProgramRuntimeError("credential scan exceeded its path budget")
    violation = False
    total = 0
    for path in paths:
        info = path.lstat()
        try:
            path.resolve(strict=True).relative_to(boundary)
        except ValueError:
            violation = True
            continue
        if stat.S_ISDIR(info.st_mode):
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            path.unlink(missing_ok=True)
            violation = True
            continue
        total += info.st_size
        if total > max_total_bytes:
            raise ProgramRuntimeError("credential scan exceeded its byte budget")
        try:
            payload = path.read_bytes()
        except OSError:
            payload = b""
            violation = True
        if any(literal in payload for literal in literals):
            path.unlink(missing_ok=True)
            violation = True
    if violation:
        raise ProgramRuntimeError("candidate output contained a provider credential")


if _scrub_secret_literals is None:  # standalone pinned-Harbor probe/worker import
    _scrub_secret_literals = _standalone_scrub_secret_literals


try:  # Harbor is intentionally not an installation dependency of this package.
    from harbor.agents.base import BaseAgent as _HarborBaseAgent
except ModuleNotFoundError as exc:  # pragma: no cover - depends on host runtime.
    if exc.name and (exc.name == "harbor" or exc.name.startswith("harbor.")):

        class _HarborBaseAgent:  # type: ignore[no-redef]
            SUPPORTS_ATIF = False
            SUPPORTS_RESUME = False
            SUPPORTS_WINDOWS = False

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                del args, kwargs
                raise ProgramBridgeUnavailable(
                    "Harbor is not importable; the candidate connector must run "
                    "inside the approved Harbor virtual environment"
                )

    else:
        raise


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _validate_text(value: object, label: str, *, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or _CONTROL.search(value)
    ):
        raise ValueError(f"{label} must be bounded non-empty text")
    return value


def _validate_api_base(value: str | None) -> str | None:
    if value is None:
        return None
    url = _validate_text(value, "api_base", maximum=4096)
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("api_base must be an HTTP(S) URL without credentials")
    return url


def _is_own_budget_exhaustion(exc: BaseException) -> bool:
    """Say whether a failed call was refused by our broker's own ceiling.

    The worker only ever talks to the broker, so an upstream rate limit and one
    of our budgets arrive through the same client error. They are told apart by
    the marker the broker puts in its own 429 body, which is why that body is a
    named constant rather than an inline literal.
    """

    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        text = str(exc)
        if any(marker in text for marker in BUDGET_EXHAUSTED_MARKERS):
            return True
        exc = exc.__cause__ or exc.__context__  # type: ignore[assignment]
    return False


def _validate_temperature(value: Any) -> float | None:
    """Accept a solver sampling temperature, or None to send none at all.

    Text is accepted because the value crosses two boundaries that carry only
    strings: Harbor's agent kwargs and the bwrap worker's JSON input.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("temperature must be a number, not a boolean")
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError as exc:
            raise ValueError("temperature must be a number") from exc
    if not isinstance(value, (int, float)):
        raise ValueError("temperature must be a number")
    temperature = float(value)
    if not math.isfinite(temperature) or not 0.0 <= temperature <= 2.0:
        raise ValueError("temperature must be within [0, 2]")
    return temperature


def _validate_provider_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("provider_timeout_seconds must be a finite number")
    try:
        timeout = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("provider_timeout_seconds must be a finite number") from exc
    if not math.isfinite(timeout) or not 0 < timeout <= 86_400:
        raise ValueError("provider_timeout_seconds must be in (0, 86400]")
    return timeout


def _validate_provider_only(value: Any) -> str | None:
    if value is not None and (
        not isinstance(value, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9/_-]{0,127}(?:,[a-z0-9][a-z0-9/_-]{0,127}){0,7}", value) is None
    ):
        raise ValueError("provider_only must be comma-separated OpenRouter endpoint slugs")
    return value


def _validate_reasoning_effort(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "default",
    }:
        raise ValueError("reasoning_effort is unsupported")
    return value


if __package__:

    class _ScopedProviderBroker(ScopedProviderBroker):
        error_type = ProgramBridgeUnavailable


def _bounded_regular_file(
    path: Path, *, maximum: int, require_private_mode: bool = False
) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProgramRuntimeError("private runtime file is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > maximum
        or (require_private_mode and stat.S_IMODE(info.st_mode) & 0o077)
    ):
        raise ProgramRuntimeError("private runtime file has unsafe metadata")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
    finally:
        os.close(descriptor)
    if len(payload) > maximum:
        raise ProgramRuntimeError("private runtime file exceeds its byte cap")
    return bytes(payload)


def _exclusive_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class CandidateOverlayIdentity:
    root: Path
    digest: str
    file_count: int
    total_bytes: int

    def __post_init__(self) -> None:
        if not self.root.is_absolute() or _HEX64.fullmatch(self.digest) is None:
            raise ValueError("candidate overlay identity is invalid")
        if self.file_count < 2 or self.total_bytes <= 0:
            raise ValueError("candidate overlay identity is empty")


def _inspect_overlay_root(
    overlay_root: str | Path,
    *,
    max_files: int = 512,
    max_total_bytes: int = 16 * 1024 * 1024,
    max_file_bytes: int = 4 * 1024 * 1024,
) -> CandidateOverlayIdentity:
    overlay_raw = Path(overlay_root)
    try:
        root_info = overlay_raw.lstat()
    except OSError as exc:
        raise CandidateOverlayError("candidate Terminus-2 subtree is missing") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise CandidateOverlayError(
            "candidate Terminus-2 subtree must be a real directory"
        )
    root = overlay_raw.resolve(strict=True)

    entries: list[dict[str, object]] = []
    files = 0
    total = 0
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        relative = path.relative_to(root).as_posix()
        if len(relative) > 1024:
            raise CandidateOverlayError("candidate overlay path is oversized")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise CandidateOverlayError("candidate overlay contains a symlink")
        if stat.S_ISDIR(info.st_mode):
            entries.append({"path": relative, "kind": "directory"})
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CandidateOverlayError(
                "candidate overlay contains a special or linked file"
            )
        if info.st_size > max_file_bytes:
            raise CandidateOverlayError("candidate overlay file exceeds its byte cap")
        files += 1
        total += info.st_size
        if files > max_files or total > max_total_bytes:
            raise CandidateOverlayError("candidate overlay exceeds its aggregate cap")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            digest = hashlib.sha256()
            observed = 0
            while chunk := os.read(descriptor, 1024 * 1024):
                observed += len(chunk)
                digest.update(chunk)
        finally:
            os.close(descriptor)
        if observed != info.st_size:
            raise CandidateOverlayError("candidate overlay changed while being read")
        entries.append(
            {
                "path": relative,
                "kind": "file",
                "sha256": digest.hexdigest(),
                "executable": bool(info.st_mode & 0o111),
            }
        )
    required = {"__init__.py", "terminus_2.py"}
    regular_paths = {str(entry["path"]) for entry in entries if entry["kind"] == "file"}
    if not required.issubset(regular_paths):
        raise CandidateOverlayError(
            "candidate overlay omits the Terminus-2 entry module"
        )
    digest = hashlib.sha256(_canonical_bytes(entries)).hexdigest()
    return CandidateOverlayIdentity(root, digest, files, total)


def inspect_candidate_overlay(
    checkout_root: str | Path,
    *,
    max_files: int = 512,
    max_total_bytes: int = 16 * 1024 * 1024,
    max_file_bytes: int = 4 * 1024 * 1024,
) -> CandidateOverlayIdentity:
    """Hash the only candidate-owned tree that will be mounted in the worker."""

    checkout = Path(checkout_root).resolve(strict=True)
    overlay = _inspect_overlay_root(
        checkout.joinpath(*CANDIDATE_SUBTREE.parts),
        max_files=max_files,
        max_total_bytes=max_total_bytes,
        max_file_bytes=max_file_bytes,
    )
    try:
        overlay.root.relative_to(checkout)
    except ValueError as exc:
        raise CandidateOverlayError(
            "candidate Terminus-2 subtree escapes checkout"
        ) from exc
    return overlay


def stage_candidate_overlay(
    source: CandidateOverlayIdentity, destination: str | Path
) -> CandidateOverlayIdentity:
    """Copy validated regular files to a private tree and cross-check its digest."""

    target = Path(destination)
    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    for path in sorted(
        source.root.rglob("*"),
        key=lambda item: item.relative_to(source.root).as_posix(),
    ):
        relative = path.relative_to(source.root)
        info = path.lstat()
        output = target / relative
        if stat.S_ISDIR(info.st_mode):
            output.mkdir(mode=0o700)
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise CandidateOverlayError("candidate overlay changed during staging")
        payload = path.read_bytes()
        _exclusive_write(output, payload, mode=0o700 if info.st_mode & 0o111 else 0o600)
    staged = _inspect_overlay_root(target)
    if not hmac.compare_digest(staged.digest, source.digest):
        raise CandidateOverlayError("staged candidate overlay digest differs")
    return staged


class ProgramAgentLauncher:
    """Bind one clean program checkout to the fixed isolated Harbor connector."""

    def __init__(
        self,
        *,
        trusted_harbor_root: str | Path,
        model_name: str,
        secret_env_name: str,
        bwrap_path: str | Path = "/usr/bin/bwrap",
        api_base: str | None = None,
        worker_timeout_seconds: int = 14_400,
        maximum_output_tokens: int = 65_536,
        maximum_total_output_tokens: int = 1_048_576,
        provider_timeout_seconds: float = 1_200.0,
        settled_cost_stop_usd: float | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        provider_only: str | None = None,
    ) -> None:
        root = Path(trusted_harbor_root).expanduser()
        bwrap = Path(bwrap_path).expanduser()
        if not root.is_absolute() or not bwrap.is_absolute():
            raise ValueError("trusted Harbor and bwrap paths must be absolute")
        model = _validate_text(model_name, "model_name", maximum=512)
        if model in {"oracle", "nop"}:
            raise ValueError(
                "formal program launcher cannot use an apparatus-only agent"
            )
        if _ENV_NAME.fullmatch(secret_env_name) is None:
            raise ValueError("secret_env_name must be a canonical environment name")
        if (
            isinstance(worker_timeout_seconds, bool)
            or not isinstance(worker_timeout_seconds, int)
            or not 0 < worker_timeout_seconds <= 86_400
        ):
            raise ValueError("worker_timeout_seconds must be in (0, 86400]")
        if settled_cost_stop_usd is not None and (
            isinstance(settled_cost_stop_usd, bool)
            or not isinstance(settled_cost_stop_usd, (int, float))
            or not math.isfinite(float(settled_cost_stop_usd))
            or float(settled_cost_stop_usd) < 0.0
        ):
            raise ValueError("settled_cost_stop_usd must be finite and non-negative")
        self.temperature = _validate_temperature(temperature)
        if (
            isinstance(maximum_output_tokens, bool)
            or not isinstance(maximum_output_tokens, int)
            or not 0 < maximum_output_tokens <= 262_144
        ):
            raise ValueError("maximum_output_tokens must be in (0, 262144]")
        self.maximum_output_tokens = maximum_output_tokens
        if (
            isinstance(maximum_total_output_tokens, bool)
            or not isinstance(maximum_total_output_tokens, int)
            or not maximum_output_tokens <= maximum_total_output_tokens <= 16_777_216
        ):
            raise ValueError("maximum_total_output_tokens must cover one call and be <= 16777216")
        self.maximum_total_output_tokens = maximum_total_output_tokens
        self.provider_timeout_seconds = _validate_provider_timeout(provider_timeout_seconds)
        self.reasoning_effort = _validate_reasoning_effort(reasoning_effort)
        self.provider_only = _validate_provider_only(provider_only)
        self.trusted_harbor_root = root
        self.model_name = model
        self.secret_env_name = secret_env_name
        self.bwrap_path = bwrap
        validated_api_base = _validate_api_base(api_base)
        if validated_api_base != OPENROUTER_API_BASE:
            raise ValueError(
                "program launcher api_base must equal the pinned OpenRouter endpoint"
            )
        self.api_base = validated_api_base
        self.worker_timeout_seconds = worker_timeout_seconds
        self.settled_cost_stop_usd = (
            None if settled_cost_stop_usd is None else float(settled_cost_stop_usd)
        )

    def invocation(self, *, checkout: Any, cell: Any) -> Any:
        """Return a secret-name-only invocation after revalidating the checkout."""

        from .controller import (
            HarborAgentInvocation,
            ProgramCheckout,
            clean_git_identity,
        )

        if not isinstance(checkout, ProgramCheckout):
            raise TypeError("checkout must be a ProgramCheckout")
        if not all(
            hasattr(cell, name)
            for name in ("cell_id", "task_id", "measurement_seed", "replicate")
        ):
            raise TypeError("cell must expose the shared evaluation coordinates")
        commit, tree = clean_git_identity(checkout.path)
        if (
            commit != checkout.program_id
            or commit != checkout.commit_sha
            or tree != checkout.tree_sha
        ):
            raise CandidateOverlayError("program checkout identity drifted")
        overlay = inspect_candidate_overlay(checkout.path)
        kwargs = [
            ("candidate_checkout", str(checkout.path)),
            ("candidate_commit", checkout.commit_sha),
            ("candidate_tree", checkout.tree_sha),
            ("candidate_overlay_digest", overlay.digest),
            ("trusted_harbor_root", str(self.trusted_harbor_root)),
            ("approved_harbor_commit", APPROVED_HARBOR_CONTROLLER_COMMIT),
            ("approved_harbor_tree", APPROVED_HARBOR_CONTROLLER_TREE),
            ("bwrap_path", str(self.bwrap_path)),
            ("worker_timeout_seconds", str(self.worker_timeout_seconds)),
            ("maximum_output_tokens", str(self.maximum_output_tokens)),
            ("maximum_total_output_tokens", str(self.maximum_total_output_tokens)),
            ("provider_timeout_seconds", repr(self.provider_timeout_seconds)),
        ]
        if self.api_base is not None:
            kwargs.append(("api_base", self.api_base))
        if self.temperature is not None:
            kwargs.append(("temperature", repr(self.temperature)))
        if self.provider_only is not None:
            kwargs.append(("provider_only", self.provider_only))
        if self.reasoning_effort is not None:
            kwargs.append(("reasoning_effort", self.reasoning_effort))
        if self.settled_cost_stop_usd is not None:
            kwargs.append(("settled_cost_stop_usd", repr(self.settled_cost_stop_usd)))
        return HarborAgentInvocation(
            launcher_id=(
                f"program-terminus2-rpc:{checkout.program_id[:12]}:"
                f"{overlay.digest[:12]}"
            ),
            agent_name=AGENT_IMPORT_PATH,
            model_name=self.model_name,
            agent_kwargs=tuple(kwargs),
            secret_env_names=(self.secret_env_name,),
            apparatus_only=False,
        )


@dataclass(frozen=True, slots=True)
class RpcLimits:
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
        integers = (
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
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in integers
        ):
            raise ValueError("RPC integer limits must be positive")
        if not math.isfinite(self.max_wall_seconds) or self.max_wall_seconds <= 0:
            raise ValueError("RPC wall limit must be finite and positive")
        if self.max_total_wire_bytes < max(
            self.max_request_bytes, self.max_response_bytes
        ):
            raise ValueError("RPC wire budget must cover one maximum frame")


@dataclass(slots=True)
class _RpcBudget:
    limits: RpcLimits
    started_at: float = field(default_factory=lambda: asyncio.get_running_loop().time())
    calls: int = 0
    wire_bytes: int = 0

    def consume(self, size: int, *, call: bool = False) -> None:
        if size < 0:
            raise ProgramRpcError("negative RPC frame size")
        if (
            asyncio.get_running_loop().time() - self.started_at
            > self.limits.max_wall_seconds
        ):
            raise ProgramRpcLimitExceeded("RPC wall-clock budget exhausted")
        if call:
            self.calls += 1
            if self.calls > self.limits.max_calls:
                raise ProgramRpcLimitExceeded("RPC call budget exhausted")
        self.wire_bytes += size
        if self.wire_bytes > self.limits.max_total_wire_bytes:
            raise ProgramRpcLimitExceeded("RPC aggregate byte budget exhausted")


def _remote_path(value: object, limits: RpcLimits) -> str:
    if not isinstance(value, str) or not value or len(value) > limits.max_path_chars:
        raise ProgramRpcError("remote path must be bounded non-empty text")
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or _CONTROL.search(value)
    ):
        raise ProgramRpcError("remote path must be absolute and traversal-free")
    return value


def _remote_user(value: object) -> str | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ProgramRpcError("remote user must be text, integer, or null")
    if isinstance(value, str) and (
        not value or len(value) > 128 or _CONTROL.search(value)
    ):
        raise ProgramRpcError("remote user text is invalid")
    if isinstance(value, int) and not 0 <= value < 2**31:
        raise ProgramRpcError("remote user integer is invalid")
    return value


def decode_rpc_request(
    frame: bytes, *, expected_nonce: str, limits: RpcLimits
) -> dict[str, Any]:
    if not frame.endswith(b"\n") or len(frame) > limits.max_request_bytes:
        raise ProgramRpcLimitExceeded("RPC request frame is invalid or oversized")
    try:
        request = json.loads(frame)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProgramRpcError("RPC request is not UTF-8 JSON") from exc
    if not isinstance(request, dict) or set(request) != {
        "schema",
        "nonce",
        "id",
        "method",
        "params",
    }:
        raise ProgramRpcError("RPC request has an invalid shape")
    if request["schema"] != BRIDGE_SCHEMA or not hmac.compare_digest(
        str(request["nonce"]), expected_nonce
    ):
        raise ProgramRpcError("RPC schema or nonce mismatch")
    request_id = request["id"]
    if (
        isinstance(request_id, bool)
        or not isinstance(request_id, int)
        or not 0 < request_id < 2**63
    ):
        raise ProgramRpcError("RPC id must be a positive 63-bit integer")
    method = request["method"]
    params = request["params"]
    if method not in _RPC_METHODS or not isinstance(params, dict):
        raise ProgramRpcError("RPC method or params are invalid")
    if method == "exec":
        if set(params) - {"command", "cwd", "env", "timeout_sec", "user"}:
            raise ProgramRpcError("exec contains unknown parameters")
        command = params.get("command")
        if (
            not isinstance(command, str)
            or not command
            or len(command) > limits.max_command_chars
            or "\0" in command
        ):
            raise ProgramRpcError("exec command is invalid")
        clean: dict[str, Any] = {
            "command": command,
            "user": _remote_user(params.get("user")),
        }
        if params.get("cwd") is not None:
            clean["cwd"] = _remote_path(params["cwd"], limits)
        environment = params.get("env")
        if environment is not None:
            if (
                not isinstance(environment, dict)
                or len(environment) > limits.max_env_entries
            ):
                raise ProgramRpcError("exec env is not a bounded object")
            clean_env: dict[str, str] = {}
            for key, value in environment.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or len(key) > 256
                    or "=" in key
                    or _CONTROL.search(key)
                    or not isinstance(value, str)
                    or len(value) > limits.max_env_value_chars
                    or "\0" in value
                ):
                    raise ProgramRpcError("exec env contains an invalid entry")
                clean_env[key] = value
            clean["env"] = clean_env
        timeout = params.get("timeout_sec")
        if timeout is not None:
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or not 0 < float(timeout) <= limits.max_exec_timeout_seconds
            ):
                raise ProgramRpcError("exec timeout exceeds the bridge cap")
            clean["timeout_sec"] = int(math.ceil(float(timeout)))
    elif method == "is_dir":
        if set(params) - {"path", "user"} or "path" not in params:
            raise ProgramRpcError("is_dir parameters are invalid")
        clean = {
            "path": _remote_path(params["path"], limits),
            "user": _remote_user(params.get("user")),
        }
    elif method == "upload_file":
        if set(params) != {"target_path", "content_b64"}:
            raise ProgramRpcError("upload_file parameters are invalid")
        encoded = params["content_b64"]
        if (
            not isinstance(encoded, str)
            or len(encoded) > limits.max_transfer_bytes * 4 // 3 + 8
        ):
            raise ProgramRpcLimitExceeded("upload_file payload is oversized")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProgramRpcError("upload_file content is not strict base64") from exc
        if len(content) > limits.max_transfer_bytes:
            raise ProgramRpcLimitExceeded("upload_file decoded payload is oversized")
        clean = {
            "target_path": _remote_path(params["target_path"], limits),
            "content": content,
        }
    else:
        if set(params) != {"source_path"}:
            raise ProgramRpcError("download_file parameters are invalid")
        clean = {"source_path": _remote_path(params["source_path"], limits)}
    return {"id": request_id, "method": method, "params": clean}


def _bounded_text(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= maximum:
        return text
    return encoded[:maximum].decode("utf-8", errors="ignore") + "\n[bridge truncated]"


def _contains_literal(value: object, literals: Sequence[str]) -> bool:
    if isinstance(value, str):
        return any(literal in value for literal in literals if literal)
    if isinstance(value, bytes):
        return any(literal.encode("utf-8") in value for literal in literals if literal)
    if isinstance(value, Mapping):
        return any(
            _contains_literal(key, literals) or _contains_literal(child, literals)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_literal(child, literals) for child in value)
    return False


class HarborRpcDispatcher:
    def __init__(self, environment: Any, limits: RpcLimits) -> None:
        self.environment = environment
        self.limits = limits

    async def dispatch(self, request: Mapping[str, Any]) -> Any:
        method = request["method"]
        params = dict(request["params"])
        if method == "exec":
            timeout = params.get("timeout_sec", self.limits.max_exec_timeout_seconds)
            try:
                result = await asyncio.wait_for(
                    self.environment.exec(**params), timeout=float(timeout) + 5.0
                )
            except (TimeoutError, RuntimeError) as error:
                # Harbor raises RuntimeError for a timed-out docker exec.
                # This is a command outcome, not a broken RPC connection.
                # Do not retry it: the command may already have side effects.
                if not isinstance(error, TimeoutError) and not re.fullmatch(
                    r'Command timed out after [0-9.]+ seconds', str(error)
                ):
                    raise
                return {'stdout':'', 'stderr':f'Command timed out after {timeout} seconds', 'return_code':124}
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
            with tempfile.TemporaryDirectory(prefix="rsi-arena-upload-") as raw:
                source = Path(raw) / "upload"
                _exclusive_write(source, content)
                await self.environment.upload_file(source, params["target_path"])
            return {"size_bytes": len(content)}
        if method == "download_file":
            with tempfile.TemporaryDirectory(prefix="rsi-arena-download-") as raw:
                target = Path(raw) / "download"
                await self.environment.download_file(params["source_path"], target)
                content = _bounded_regular_file(
                    target, maximum=self.limits.max_transfer_bytes
                )
            return {
                "size_bytes": len(content),
                "content_b64": base64.b64encode(content).decode("ascii"),
            }
        raise ProgramRpcError("unreachable RPC method")


class HarborRpcServer:
    def __init__(
        self,
        socket_path: Path,
        *,
        nonce: str,
        environment: Any,
        limits: RpcLimits,
        forbidden_literals: Sequence[str],
        error_journal: Path | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.nonce = nonce
        self.limits = limits
        self.dispatcher = HarborRpcDispatcher(environment, limits)
        self.forbidden_literals = tuple(
            value for value in forbidden_literals if len(value) >= 4
        )
        self.budget: _RpcBudget | None = None
        self.server: asyncio.AbstractServer | None = None
        self.connections = 0
        self.error_journal = error_journal
        self.recorded_errors = 0

    def record_error(self, error: BaseException, request=None) -> None:
        """Trusted host diagnostics, outside candidate-writable agent logs."""
        if self.error_journal is None or self.recorded_errors >= 64:
            return
        details = ''.join(traceback.format_exception(error))[-16000:]
        for literal in self.forbidden_literals:
            details = details.replace(literal, '[redacted]')
        record = {'schema':'rsi-arena.rpc-error.v1', 'exception_type':type(error).__name__,
                  'method':request.get('method') if request else None,
                  'request_id':request.get('id') if request else None, 'traceback':details}
        try:
            fd = os.open(self.error_journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab') as stream:
                stream.write(_canonical_bytes(record)+b'\n')
                stream.flush()
                os.fsync(stream.fileno())
            self.recorded_errors += 1
        except OSError:
            pass  # Diagnostics failure must not replace the original error.

    async def __aenter__(self) -> "HarborRpcServer":
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.budget = _RpcBudget(self.limits)
        self.server = await asyncio.start_unix_server(
            self._handle,
            path=str(self.socket_path),
            limit=self.limits.max_request_bytes + 1,
        )
        os.chmod(self.socket_path, 0o600)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        self.socket_path.unlink(missing_ok=True)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        if self.connections > 1:
            writer.close()
            await writer.wait_closed()
            return
        try:
            while True:
                try:
                    frame = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError:
                    break
                except asyncio.LimitOverrunError:
                    await self._respond(
                        writer,
                        {
                            "schema": BRIDGE_SCHEMA,
                            "id": 0,
                            "ok": False,
                            "error": {
                                "type": "ProgramRpcLimitExceeded",
                                "message": "RPC frame is oversized",
                            },
                        },
                    )
                    break
                request: dict[str, Any] | None = None
                try:
                    assert self.budget is not None
                    self.budget.consume(len(frame), call=True)
                    request = decode_rpc_request(
                        frame, expected_nonce=self.nonce, limits=self.limits
                    )
                    if _contains_literal(request["params"], self.forbidden_literals):
                        raise ProgramRpcError(
                            "RPC attempted to copy a child-only credential into the task"
                        )
                    result = await self.dispatcher.dispatch(request)
                    response = {
                        "schema": BRIDGE_SCHEMA,
                        "id": request["id"],
                        "ok": True,
                        "result": result,
                    }
                except Exception as error:
                    self.record_error(error, request)
                    response = {
                        "schema": BRIDGE_SCHEMA,
                        "id": request["id"] if request is not None else 0,
                        "ok": False,
                        "error": {
                            "type": type(error).__name__,
                            "message": _bounded_text(error, 2048),
                        },
                    }
                try:
                    await self._respond(writer, response)
                except Exception as error:
                    self.record_error(error, request)
                    raise
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionError):
                pass

    async def _respond(
        self, writer: asyncio.StreamWriter, response: Mapping[str, Any]
    ) -> None:
        frame = _canonical_bytes(response) + b"\n"
        if len(frame) > self.limits.max_response_bytes:
            raise ProgramRpcLimitExceeded("RPC response frame is oversized")
        assert self.budget is not None
        self.budget.consume(len(frame))
        writer.write(frame)
        await writer.drain()


def validate_agent_context(
    value: object, *, forbidden_literals: Sequence[str] = ()
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - _AGENT_CONTEXT_FIELDS:
        raise CandidateContextError("candidate AgentContext has invalid fields")
    if _contains_literal(value, forbidden_literals):
        raise CandidateContextError(
            "candidate AgentContext contains a provider credential"
        )
    for name in ("n_input_tokens", "n_cache_tokens", "n_output_tokens"):
        item = value.get(name)
        if item is not None and (
            isinstance(item, bool) or not isinstance(item, int) or item < 0
        ):
            raise CandidateContextError(f"candidate AgentContext {name} is invalid")
    cost = value.get("cost_usd")
    if cost is not None and (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(float(cost))
        or cost < 0
    ):
        raise CandidateContextError("candidate AgentContext cost_usd is invalid")
    if value.get("rollout_details") is not None and not isinstance(
        value["rollout_details"], list
    ):
        raise CandidateContextError("candidate AgentContext rollout_details is invalid")
    if value.get("metadata") is not None and not isinstance(value["metadata"], dict):
        raise CandidateContextError("candidate AgentContext metadata is invalid")
    try:
        encoded = _canonical_bytes(value)
    except (TypeError, ValueError) as exc:
        raise CandidateContextError(
            "candidate AgentContext is not finite JSON"
        ) from exc
    if len(encoded) > 64 * 1024 * 1024:
        raise CandidateContextError("candidate AgentContext exceeds the byte cap")
    return dict(value)


def attach_provider_diagnostics(
    context: object,
    snapshot: ProviderReliabilitySnapshot,
    accounting: ProviderAccountingSnapshot,
    *,
    forbidden_literals: Sequence[str] = (),
) -> dict[str, Any]:
    """Bind trusted broker telemetry into an otherwise candidate context."""

    validated = validate_agent_context(
        context,
        forbidden_literals=forbidden_literals,
    )
    if not isinstance(snapshot, ProviderReliabilitySnapshot):
        raise TypeError("provider reliability snapshot is required")
    if not isinstance(accounting, ProviderAccountingSnapshot):
        raise TypeError("provider accounting snapshot is required")
    metadata = dict(validated.get("metadata") or {})
    # This key is trusted and reserved.  Candidate output is overwritten so a
    # consumer can never mistake self-reported figures for broker observations.
    metadata[PROVIDER_DIAGNOSTICS_METADATA_KEY] = {
        "accounting": accounting.as_dict(),
        "reliability": snapshot.as_dict(),
    }
    validated["n_input_tokens"] = accounting.input_tokens
    validated["n_cache_tokens"] = accounting.cache_tokens
    validated["n_output_tokens"] = accounting.output_tokens
    validated["cost_usd"] = accounting.cost_usd
    validated["metadata"] = metadata
    return validate_agent_context(
        validated,
        forbidden_literals=forbidden_literals,
    )


def _git_identity(root: Path) -> tuple[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    def git(*arguments: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *arguments],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProgramBridgeUnavailable("trusted Git identity probe failed") from exc
        return result.stdout.strip()

    top = Path(git("rev-parse", "--show-toplevel")).resolve()
    commit = git("rev-parse", "HEAD^{commit}")
    tree = git("rev-parse", "HEAD^{tree}")
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    flags = git("ls-files", "-v", "--", ".").splitlines()
    if (
        top != root
        or status
        or not flags
        or any(not row.startswith("H ") for row in flags)
    ):
        raise ProgramBridgeUnavailable(
            "trusted Harbor checkout is not clean and ordinary"
        )
    return commit, tree


def _python_metadata(trusted_harbor_root: Path) -> dict[str, str]:
    python = trusted_harbor_root / ".venv/bin/python"
    try:
        result = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "import json,sys;print(json.dumps({'base_prefix':sys.base_prefix,'version':[sys.version_info[0],sys.version_info[1]]}))",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ProgramBridgeUnavailable("approved Harbor Python probe failed") from exc
    base_prefix = Path(payload.get("base_prefix", ""))
    version = payload.get("version")
    if (
        not base_prefix.is_absolute()
        or not base_prefix.is_dir()
        or not isinstance(version, list)
        or len(version) != 2
        or any(not isinstance(item, int) for item in version)
    ):
        raise ProgramBridgeUnavailable("approved Harbor Python metadata is invalid")
    if python.is_symlink():
        raw_target = Path(os.readlink(python))
        if not raw_target.is_absolute() or len(raw_target.parents) < 2:
            raise ProgramBridgeUnavailable("approved Harbor Python symlink is invalid")
        guest_base_prefix = raw_target.parent.parent
    else:
        guest_base_prefix = base_prefix
    major, minor = version
    return {
        "base_prefix": str(base_prefix),
        "guest_base_prefix": str(guest_base_prefix),
        "guest_python": str(_GUEST_HARBOR_ROOT / ".venv/bin/python"),
        "site_packages": str(
            _GUEST_HARBOR_ROOT / f".venv/lib/python{major}.{minor}/site-packages"
        ),
    }


def _mount_parent_args(path: Path) -> list[str]:
    if not path.is_absolute():
        raise ProgramBridgeUnavailable("Python base prefix must be absolute")
    arguments: list[str] = []
    current = Path("/")
    for part in path.parts[1:-1]:
        current /= part
        arguments += ["--dir", str(current)]
    return arguments


def build_bwrap_command(
    *,
    bwrap: Path,
    trusted_harbor_root: Path,
    python_metadata: Mapping[str, str],
    staged_overlay: Path,
    bridge_module: Path,
    provider_broker_module: Path,
    input_file: Path,
    socket_parent: Path,
    provider_socket_parent: Path,
    logs_dir: Path,
    output_dir: Path,
    credential_file: Path,
    api_base: str | None,
) -> tuple[str, ...]:
    """Build an argv whose only candidate-checkout mount is ``staged_overlay``."""

    arguments = [
        str(bwrap),
        "--die-with-parent",
        "--new-session",
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
    for raw in ("/usr", "/bin", "/lib", "/lib64", "/sbin"):
        if Path(raw).exists():
            arguments += ["--ro-bind", raw, raw]
    arguments += ["--dir", "/etc"]
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
            arguments += ["--ro-bind", raw, raw]
    guest_base = Path(python_metadata["guest_base_prefix"])
    arguments += [
        "--dir",
        "/opt",
        "--dir",
        "/rsi-arena",
        "--dir",
        str(_GUEST_BRIDGE_ROOT),
        "--dir",
        str(_GUEST_HARBOR_ROOT),
        "--dir",
        str(_GUEST_HARBOR_ROOT / "src"),
        "--dir",
        str(_GUEST_PROVIDER_ROOT),
    ]
    arguments += _mount_parent_args(guest_base)
    arguments += [
        "--ro-bind",
        python_metadata["base_prefix"],
        str(guest_base),
        "--ro-bind",
        str(trusted_harbor_root / ".venv"),
        str(_GUEST_HARBOR_ROOT / ".venv"),
        "--ro-bind",
        str(trusted_harbor_root / "src/harbor"),
        str(_GUEST_HARBOR_ROOT / "src/harbor"),
        "--ro-bind",
        str(staged_overlay),
        str(_GUEST_HARBOR_ROOT / CANDIDATE_SUBTREE),
        "--ro-bind",
        str(bridge_module),
        str(_GUEST_BRIDGE_ROOT / bridge_module.name),
        "--ro-bind",
        str(provider_broker_module),
        str(_GUEST_BRIDGE_ROOT / provider_broker_module.name),
        "--ro-bind",
        str(input_file),
        str(_GUEST_INPUT),
        "--ro-bind",
        str(socket_parent),
        str(_GUEST_RPC_ROOT),
        "--ro-bind",
        str(provider_socket_parent),
        str(_GUEST_PROVIDER_ROOT),
        "--bind",
        str(logs_dir),
        str(_GUEST_LOGS_ROOT),
        "--bind",
        str(output_dir),
        str(_GUEST_OUTPUT_ROOT),
        "--ro-bind",
        str(credential_file),
        str(_GUEST_CREDENTIAL),
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "PATH",
        "/usr/local/bin:/usr/bin:/bin",
        "--setenv",
        "PYTHONPATH",
        f"{_GUEST_HARBOR_ROOT / 'src'}:{python_metadata['site_packages']}:{_GUEST_BRIDGE_ROOT}",
        "--setenv",
        "PYTHONNOUSERSITE",
        "1",
    ]
    if api_base is not None:
        arguments += [
            "--setenv",
            "RSI_ARENA_AGENTPILOT_API_BASE",
            api_base,
        ]
    arguments += [
        "--chdir",
        str(_GUEST_LOGS_ROOT),
        "--",
        python_metadata["guest_python"],
        str(_GUEST_BRIDGE_ROOT / bridge_module.name),
        "--worker",
        str(_GUEST_INPUT),
    ]
    return tuple(arguments)


class CandidateTerminus2Connector(_HarborBaseAgent):
    """Trusted Harbor agent wrapper; candidate Python runs only inside bwrap."""

    SUPPORTS_ATIF = True
    SUPPORTS_RESUME = False
    SUPPORTS_WINDOWS = False

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        candidate_checkout: str,
        candidate_commit: str,
        candidate_tree: str,
        candidate_overlay_digest: str,
        trusted_harbor_root: str,
        approved_harbor_commit: str,
        approved_harbor_tree: str,
        bwrap_path: str,
        worker_timeout_seconds: str | int,
        maximum_output_tokens: str | int = 65_536,
        maximum_total_output_tokens: str | int = 1_048_576,
        provider_timeout_seconds: str | float = 1_200.0,
        settled_cost_stop_usd: str | float | None = None,
        api_base: str | None = None,
        temperature: str | float | None = None,
        reasoning_effort: str | None = None,
        provider_only: str | None = None,
        logger: Any | None = None,
        mcp_servers: list[Any] | None = None,
        skills_dir: str | None = None,
        extra_env: dict[str, str] | None = None,
        trial_paths: Any | None = None,
        agent_timeout_sec: Any | None = None,
        **unknown: Any,
    ) -> None:
        del trial_paths, agent_timeout_sec
        super().__init__(
            logs_dir=logs_dir, model_name=model_name, logger=logger, extra_env=None
        )
        if unknown:
            raise ProgramBridgeUnavailable(
                "custom connector received unknown agent kwargs"
            )
        if mcp_servers or skills_dir is not None:
            raise ProgramBridgeUnavailable(
                "MCP servers and skills mounts are not supported"
            )
        if model_name is None or model_name in {"oracle", "nop"}:
            raise ProgramBridgeUnavailable(
                "a real model is required by the program connector"
            )
        if (
            approved_harbor_commit != APPROVED_HARBOR_CONTROLLER_COMMIT
            or approved_harbor_tree != APPROVED_HARBOR_CONTROLLER_TREE
        ):
            raise ProgramBridgeUnavailable(
                "launcher supplied an unapproved Harbor identity"
            )
        if (
            _HEX40.fullmatch(candidate_commit) is None
            or _HEX40.fullmatch(candidate_tree) is None
        ):
            raise ProgramBridgeUnavailable("candidate commit/tree identity is invalid")
        if _HEX64.fullmatch(candidate_overlay_digest) is None:
            raise ProgramBridgeUnavailable("candidate overlay digest is invalid")
        try:
            timeout = int(worker_timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise ProgramBridgeUnavailable("worker timeout is invalid") from exc
        if not 0 < timeout <= 86_400:
            raise ProgramBridgeUnavailable("worker timeout is outside the fixed cap")
        try:
            settled_cost_stop = (
                None if settled_cost_stop_usd is None else float(settled_cost_stop_usd)
            )
        except (TypeError, ValueError) as exc:
            raise ProgramBridgeUnavailable(
                "provider settled-cost stop is invalid"
            ) from exc
        if settled_cost_stop is not None and (
            not math.isfinite(settled_cost_stop) or settled_cost_stop < 0.0
        ):
            raise ProgramBridgeUnavailable("provider settled-cost stop is invalid")
        try:
            self._temperature = _validate_temperature(temperature)
        except ValueError as exc:
            raise ProgramBridgeUnavailable("solver temperature is invalid") from exc
        try:
            self._reasoning_effort = _validate_reasoning_effort(reasoning_effort)
            self._provider_only = _validate_provider_only(provider_only)
        except ValueError as exc:
            raise ProgramBridgeUnavailable(
                "solver reasoning effort is invalid"
            ) from exc
        secrets = dict(extra_env or {})
        if len(secrets) != 1:
            raise ProgramBridgeUnavailable(
                "connector requires exactly one named credential"
            )
        credential_name, credential_value = next(iter(secrets.items()))
        if (
            _ENV_NAME.fullmatch(credential_name) is None
            or not credential_value
            or "\0" in credential_value
        ):
            raise ProgramBridgeUnavailable("connector credential input is invalid")

        root = Path(trusted_harbor_root).resolve(strict=True)
        if root.is_symlink() or not root.is_dir():
            raise ProgramBridgeUnavailable("trusted Harbor root is invalid")
        commit, tree = _git_identity(root)
        if (
            commit != APPROVED_HARBOR_CONTROLLER_COMMIT
            or tree != APPROVED_HARBOR_CONTROLLER_TREE
        ):
            raise ProgramBridgeUnavailable("trusted Harbor controller identity drifted")
        checkout_raw = Path(candidate_checkout)
        if checkout_raw.is_symlink():
            raise ProgramBridgeUnavailable("candidate checkout cannot be a symlink")
        checkout = checkout_raw.resolve(strict=True)
        checkout_commit, checkout_tree = _git_identity(checkout)
        if checkout_commit != candidate_commit or checkout_tree != candidate_tree:
            raise ProgramBridgeUnavailable("candidate checkout commit/tree drifted")
        overlay = inspect_candidate_overlay(checkout)
        if not hmac.compare_digest(overlay.digest, candidate_overlay_digest):
            raise ProgramBridgeUnavailable(
                "candidate overlay changed after launch binding"
            )
        bwrap = Path(bwrap_path).resolve(strict=True)
        if not bwrap.is_file() or not os.access(bwrap, os.X_OK):
            raise ProgramBridgeUnavailable(
                "approved bubblewrap executable is unavailable"
            )

        self._trusted_harbor_root = root
        self._candidate_checkout = checkout
        self._candidate_commit = candidate_commit
        self._candidate_tree = candidate_tree
        self._overlay = overlay
        self._bwrap = bwrap
        if (
            isinstance(maximum_output_tokens, bool)
            or not re.fullmatch(r"[1-9][0-9]*", str(maximum_output_tokens))
            or not 0 < int(maximum_output_tokens) <= 262_144
        ):
            raise ValueError("maximum_output_tokens must be in (0, 262144]")
        self._maximum_output_tokens = int(maximum_output_tokens)
        if (
            isinstance(maximum_total_output_tokens, bool)
            or not re.fullmatch(r"[1-9][0-9]*", str(maximum_total_output_tokens))
            or not self._maximum_output_tokens <= int(maximum_total_output_tokens) <= 16_777_216
        ):
            raise ValueError("maximum_total_output_tokens must cover one call and be <= 16777216")
        self._maximum_total_output_tokens = int(maximum_total_output_tokens)
        self._provider_timeout_seconds = _validate_provider_timeout(provider_timeout_seconds)
        self._worker_timeout_seconds = timeout
        self._settled_cost_stop_usd = settled_cost_stop
        self._credential_name = credential_name
        self._credential_value = credential_value
        self._worker_deadline_reached = False
        validated_api_base = _validate_api_base(api_base)
        if validated_api_base != OPENROUTER_API_BASE:
            raise ProgramBridgeUnavailable(
                "connector api_base must equal the pinned OpenRouter endpoint"
            )
        self._api_base = validated_api_base
        self._setup_environment: Any | None = None

    @staticmethod
    def name() -> str:
        return AGENT_NAME

    def version(self) -> str:
        # Harbor's BaseAgent contract requires a package-style version string.
        return "1.0.0"

    async def setup(self, environment: Any) -> None:
        required = ("exec", "is_dir", "upload_file", "download_file")
        if any(not callable(getattr(environment, name, None)) for name in required):
            raise ProgramBridgeUnavailable(
                "Harbor environment lacks the fixed RPC surface"
            )
        self._setup_environment = environment

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        if self._setup_environment is not environment:
            raise ProgramBridgeUnavailable(
                "connector run used another Harbor environment"
            )
        payload = await self._run_worker(instruction, environment)
        try:
            from harbor.models.agent.context import AgentContext

            validated = AgentContext.model_validate(payload)
        except Exception as exc:
            raise CandidateContextError(
                "candidate context failed Harbor validation"
            ) from exc
        for field_name in validated.model_fields:
            setattr(context, field_name, getattr(validated, field_name))

    async def _run_worker(self, instruction: str, environment: Any) -> dict[str, Any]:
        if self._api_base is None:
            raise ProgramBridgeUnavailable(
                "candidate provider requires an explicit broker upstream"
            )
        broker = _ScopedProviderBroker(
            provider_bearer=self._credential_value,
            api_base=self._api_base,
            model_name=self.model_name,
            timeout_seconds=min(float(self._worker_timeout_seconds), self._provider_timeout_seconds),
            **({"provider_only": self._provider_only} if self._provider_only is not None else {}),
            settled_cost_stop_usd=self._settled_cost_stop_usd,
            maximum_output_tokens=self._maximum_output_tokens,
            maximum_total_output_tokens=self._maximum_total_output_tokens,
            response_artifact_dir=Path(self.logs_dir).resolve(strict=True).parent
            / "provider-responses",
        )
        broker.start()
        ephemeral_bearer = broker.ephemeral_bearer
        self._worker_deadline_reached = False
        try:
            return await self._run_worker_with_broker(
                instruction,
                environment,
                broker=broker,
                ephemeral_bearer=ephemeral_bearer,
            )
        finally:
            await asyncio.to_thread(broker.quiesce)
            snapshot, accounting, calls = broker.trial_diagnostics_snapshot()
            try:
                self._persist_provider_diagnostics(
                    snapshot,
                    accounting,
                    calls,
                    worker_deadline_reached=self._worker_deadline_reached,
                )
            finally:
                broker.close()

    def _persist_provider_diagnostics(
        self,
        snapshot: ProviderReliabilitySnapshot,
        accounting: ProviderAccountingSnapshot,
        calls: tuple[ProviderCallAccounting, ...],
        *,
        worker_deadline_reached: bool,
    ) -> None:
        """Write trusted, content-free provider telemetry into the trial logs."""

        logs_dir = Path(self.logs_dir).resolve(strict=True)
        # Only logs_dir itself is mounted into the worker.  Persist in its trial
        # parent so candidate code cannot pre-create, replace, or spoof this
        # trusted record even while the provider handler is still unwinding.
        destination = logs_dir.parent / PROVIDER_DIAGNOSTICS_NAME
        _exclusive_write(
            destination,
            _canonical_bytes(
                {
                    "schema": PROVIDER_TRIAL_DIAGNOSTICS_SCHEMA,
                    "worker_deadline_reached": worker_deadline_reached,
                    "worker_timeout_seconds": self._worker_timeout_seconds,
                    "configured_max_output_tokens": self._maximum_output_tokens,
                    "configured_max_total_output_tokens": self._maximum_total_output_tokens,
                    "configured_provider_timeout_seconds": self._provider_timeout_seconds,
                    "cost_stop_enabled": self._settled_cost_stop_usd is not None,
                    **({"configured_provider_only": self._provider_only} if self._provider_only is not None else {}),
                    "model_name": self.model_name,
                    "accounting": accounting.as_dict(),
                    "calls": [call.as_dict() for call in calls],
                    "provider": snapshot.as_dict(),
                }
            ),
        )

    async def _run_worker_with_broker(
        self,
        instruction: str,
        environment: Any,
        *,
        broker: _ScopedProviderBroker,
        ephemeral_bearer: str,
    ) -> dict[str, Any]:
        limits = RpcLimits(max_wall_seconds=float(self._worker_timeout_seconds))
        module = Path(__file__).resolve(strict=True)
        provider_broker_module = module.with_name("provider_broker.py")
        python_metadata = _python_metadata(self._trusted_harbor_root)
        with host_temporary_directory("program") as raw:
            root = Path(raw)
            staged = stage_candidate_overlay(self._overlay, root / "candidate-overlay")
            input_file = root / "input.json"
            output_dir = root / "output"
            output_dir.mkdir(mode=0o700)
            socket_parent = root / "rpc"
            credential_file = root / "credential"
            _exclusive_write(credential_file, ephemeral_bearer.encode("utf-8"))
            nonce = hashlib.sha256(os.urandom(32)).hexdigest()
            payload = {
                "schema": BRIDGE_SCHEMA,
                "nonce": nonce,
                "instruction": instruction,
                "model_name": self.model_name,
                "api_base": broker.base_url,
                "provider_socket": WORKER_PROVIDER_SOCKET,
                "temperature": self._temperature,
                "reasoning_effort": self._reasoning_effort,
                "maximum_output_tokens": self._maximum_output_tokens,
                "provider_timeout_seconds": min(
                    float(self._worker_timeout_seconds), self._provider_timeout_seconds
                ),
                "default_user": getattr(environment, "default_user", None),
                "rpc_socket": str(_GUEST_RPC_SOCKET),
                "rpc_limits": asdict(limits),
                "credential_env_name": self._credential_name,
                "credential_file": str(_GUEST_CREDENTIAL),
                "context_result": str(_GUEST_OUTPUT_ROOT / _CONTEXT_RESULT_NAME),
                "worker_status": str(_GUEST_OUTPUT_ROOT / _WORKER_STATUS_NAME),
                "candidate_overlay_digest": staged.digest,
            }
            _exclusive_write(input_file, _canonical_bytes(payload))
            logs_dir = Path(self.logs_dir).resolve(strict=True)
            async with HarborRpcServer(
                socket_parent / _GUEST_RPC_SOCKET.name,
                nonce=nonce,
                environment=environment,
                limits=limits,
                forbidden_literals=(self._credential_value, ephemeral_bearer),
                error_journal=logs_dir.parent/'rpc-errors.private.jsonl',
            ):
                command = build_bwrap_command(
                    bwrap=self._bwrap,
                    trusted_harbor_root=self._trusted_harbor_root,
                    python_metadata=python_metadata,
                    staged_overlay=staged.root,
                    bridge_module=module,
                    provider_broker_module=provider_broker_module,
                    input_file=input_file,
                    socket_parent=socket_parent,
                    provider_socket_parent=broker.socket_path.parent,
                    logs_dir=logs_dir,
                    output_dir=output_dir,
                    credential_file=credential_file,
                    api_base=broker.base_url,
                )
                joined_command = "\0".join(command)
                if self._credential_value in joined_command:
                    raise ProgramBridgeUnavailable("credential entered the worker argv")
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                    env={},
                )
                try:
                    from .worker_deadline import wait_for_worker_deadline

                    self._worker_deadline_reached = await wait_for_worker_deadline(
                        process,
                        timeout_seconds=self._worker_timeout_seconds,
                    )
                    if self._worker_deadline_reached:
                        # This is a measurement deadline, not an execution
                        # exception.  Freeze the environment, return a minimal
                        # context, and let Harbor continue to artifact collection
                        # and the separate verifier.  Trusted trial telemetry
                        # carries the timeout/stall distinction downstream.
                        pass
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    if process.returncode is None:
                        await asyncio.shield(process.wait())
                    _scrub_secret_literals(
                        logs_dir, (self._credential_value, ephemeral_bearer)
                    )
            if self._worker_deadline_reached:
                return attach_provider_diagnostics(
                    {},
                    broker.reliability_snapshot(),
                    broker.accounting_snapshot(),
                    forbidden_literals=(self._credential_value, ephemeral_bearer),
                )
            status_path = output_dir / _WORKER_STATUS_NAME
            context_path = output_dir / _CONTEXT_RESULT_NAME
            try:
                status = json.loads(
                    _bounded_regular_file(
                        status_path,
                        maximum=64 * 1024,
                        require_private_mode=True,
                    )
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CandidateContextError(
                    "candidate worker output is not UTF-8 JSON"
                ) from exc
            # Exact shapes rather than a lenient read: the worker either finished
            # its turn or it did not, and the only difference between these two
            # is whether it ran out of its own allowance on the way.
            finished = (
                {"ok": True, "spent_its_allowance": False},
                {"ok": True, "spent_its_allowance": True},
            )
            if process.returncode != 0 or status not in finished:
                failure_detail = output_dir/'worker-error.json'
                if failure_detail.exists() and not failure_detail.is_symlink():
                    try:
                        detail = _bounded_regular_file(failure_detail, maximum=32768, require_private_mode=True)
                        for literal in (self._credential_value, ephemeral_bearer):
                            detail = detail.replace(literal.encode(), b'[redacted]')
                        _exclusive_write(logs_dir.parent/'worker-error.private.json', detail)
                    except (OSError, ProgramRuntimeError):
                        pass
                error_type = (
                    status.get("error_type") if isinstance(status, dict) else None
                )
                if not isinstance(error_type, str) or not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]{0,127}", error_type
                ):
                    error_type = "unknown"
                error_stage = (
                    status.get("error_stage") if isinstance(status, dict) else None
                )
                if not isinstance(error_stage, str) or not re.fullmatch(
                    r"[a-z][a-z0-9_]{0,63}", error_stage
                ):
                    error_stage = "unknown"
                cause_type = (
                    status.get("cause_type") if isinstance(status, dict) else None
                )
                if not isinstance(cause_type, str) or not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]{0,127}", cause_type
                ):
                    cause_type = "unknown"
                raise ProgramRuntimeError(
                    "isolated candidate worker failed "
                    f"({error_type}:{error_stage}:{cause_type})"
                )
            try:
                context_payload = json.loads(
                    _bounded_regular_file(
                        context_path,
                        maximum=64 * 1024 * 1024,
                        require_private_mode=True,
                    )
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CandidateContextError(
                    "candidate worker context is not UTF-8 JSON"
                ) from exc
            validated_context = validate_agent_context(
                context_payload,
                forbidden_literals=(self._credential_value, ephemeral_bearer),
            )
            return attach_provider_diagnostics(
                validated_context,
                broker.reliability_snapshot(),
                broker.accounting_snapshot(),
                forbidden_literals=(self._credential_value, ephemeral_bearer),
            )


class _RpcClient:
    def __init__(self, socket_path: str, nonce: str, limits: RpcLimits) -> None:
        self.socket_path = socket_path
        self.nonce = nonce
        self.limits = limits
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.next_id = 1
        self.calls = 0
        self.wire_bytes = 0
        self.lock = asyncio.Lock()
        self.abandoned_ids: set[int] = set()

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        async with self.lock:
            if self.reader is None or self.writer is None:
                self.reader, self.writer = await asyncio.open_unix_connection(
                    self.socket_path,
                    limit=self.limits.max_response_bytes + 1,
                )
            request_id = self.next_id
            self.next_id += 1
            self.calls += 1
            if self.calls > self.limits.max_calls:
                raise ProgramRpcLimitExceeded("worker RPC call cap exceeded")
            frame = (
                _canonical_bytes(
                    {
                        "schema": BRIDGE_SCHEMA,
                        "nonce": self.nonce,
                        "id": request_id,
                        "method": method,
                        "params": params,
                    }
                )
                + b"\n"
            )
            if len(frame) > self.limits.max_request_bytes:
                raise ProgramRpcLimitExceeded("worker RPC request cap exceeded")
            self.wire_bytes += len(frame)
            self.writer.write(frame)
            try:
                await self.writer.drain()
                while True:
                    response_frame = await self.reader.readuntil(b"\n")
                    self.wire_bytes += len(response_frame)
                    if self.wire_bytes > self.limits.max_total_wire_bytes:
                        raise ProgramRpcLimitExceeded("worker RPC wire cap exceeded")
                    response = json.loads(response_frame)
                    if (isinstance(response, dict) and response.get('schema') == BRIDGE_SCHEMA
                            and type(response.get('id')) is int and response['id'] in self.abandoned_ids):
                        self.abandoned_ids.remove(response['id'])
                        continue  # Cancelled caller, not a cancelled/retried remote command.
                    break
            except asyncio.CancelledError:
                self.abandoned_ids.add(request_id)
                raise
            if (
                not isinstance(response, dict)
                or response.get("schema") != BRIDGE_SCHEMA
                or response.get("id") != request_id
                or not isinstance(response.get("ok"), bool)
            ):
                raise ProgramRpcError("worker received a malformed RPC response")
            if not response["ok"]:
                error = response.get("error")
                message = (
                    error.get("message") if isinstance(error, dict) else "RPC failed"
                )
                raise ProgramRpcError(str(message))
            return response.get("result")


class _RemoteEnvironment:
    def __init__(
        self, client: _RpcClient, *, default_user: str | int | None, limits: RpcLimits
    ) -> None:
        self.client = client
        self.default_user = default_user
        self.limits = limits
        self.session_id = "rsi-arena-program-candidate"
        self.trial_paths = SimpleNamespace(agent_dir=Path(_GUEST_LOGS_ROOT))

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> Any:
        params: dict[str, Any] = {"command": command}
        for name, value in (
            ("cwd", cwd),
            ("env", env),
            ("timeout_sec", timeout_sec),
            ("user", user),
        ):
            if value is not None:
                params[name] = value
        result = await self.client.call("exec", params)
        from harbor.environments.base import ExecResult

        return ExecResult.model_validate(result)

    async def is_dir(self, path: str, user: str | int | None = None) -> bool:
        params: dict[str, Any] = {"path": path}
        if user is not None:
            params["user"] = user
        return bool(await self.client.call("is_dir", params))

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        source = Path(source_path)
        info = source.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > self.limits.max_transfer_bytes
        ):
            raise ProgramRpcLimitExceeded("worker upload source is invalid")
        content = source.read_bytes()
        await self.client.call(
            "upload_file",
            {
                "target_path": target_path,
                "content_b64": base64.b64encode(content).decode("ascii"),
            },
        )

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        result = await self.client.call("download_file", {"source_path": source_path})
        if not isinstance(result, dict) or set(result) != {"size_bytes", "content_b64"}:
            raise ProgramRpcError("worker download response is malformed")
        try:
            content = base64.b64decode(result["content_b64"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProgramRpcError(
                "worker download response is not strict base64"
            ) from exc
        if (
            len(content) != result["size_bytes"]
            or len(content) > self.limits.max_transfer_bytes
        ):
            raise ProgramRpcLimitExceeded("worker download response is oversized")
        _exclusive_write(Path(target_path), content)


def _load_worker_input(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            _bounded_regular_file(
                path,
                maximum=8 * 1024 * 1024,
                require_private_mode=True,
            )
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProgramRuntimeError("worker input is unreadable JSON") from exc
    required = {
        "schema",
        "nonce",
        "instruction",
        "model_name",
        "api_base",
        "provider_socket",
        "temperature",
        "reasoning_effort",
        "maximum_output_tokens",
        "provider_timeout_seconds",
        "default_user",
        "rpc_socket",
        "rpc_limits",
        "credential_env_name",
        "credential_file",
        "context_result",
        "worker_status",
        "candidate_overlay_digest",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or payload.get("schema") != BRIDGE_SCHEMA
    ):
        raise ProgramRuntimeError("worker input has an invalid shape")
    if not isinstance(payload["instruction"], str):
        raise ProgramRuntimeError("worker instruction must be text")
    _validate_text(payload["model_name"], "worker model", maximum=512)
    if payload["api_base"] != WORKER_PROVIDER_BASE_URL:
        raise ProgramRuntimeError("worker provider route is not isolated")
    if payload["provider_socket"] != WORKER_PROVIDER_SOCKET:
        raise ProgramRuntimeError("worker provider socket is invalid")
    try:
        _validate_temperature(payload["temperature"])
    except ValueError as exc:
        raise ProgramRuntimeError("worker temperature is invalid") from exc
    try:
        _validate_reasoning_effort(payload["reasoning_effort"])
    except ValueError as exc:
        raise ProgramRuntimeError("worker reasoning effort is invalid") from exc
    maximum_output_tokens = payload["maximum_output_tokens"]
    if (
        isinstance(maximum_output_tokens, bool)
        or not isinstance(maximum_output_tokens, int)
        or not 1 <= maximum_output_tokens <= 262_144
    ):
        raise ProgramRuntimeError("worker output token limit is invalid")
    try:
        _validate_provider_timeout(payload["provider_timeout_seconds"])
    except ValueError as exc:
        raise ProgramRuntimeError("worker provider timeout is invalid") from exc
    if _ENV_NAME.fullmatch(str(payload["credential_env_name"])) is None:
        raise ProgramRuntimeError("worker credential environment name is invalid")
    if payload["credential_file"] != str(_GUEST_CREDENTIAL):
        raise ProgramRuntimeError("worker credential path is invalid")
    if _HEX64.fullmatch(str(payload["candidate_overlay_digest"])) is None:
        raise ProgramRuntimeError("worker candidate digest is invalid")
    return payload


def _worker_solver_kwargs(config: Mapping[str, Any]) -> dict[str, Any]:
    """Pin the effective limits and OpenRouter reasoning at the model boundary."""

    call_kwargs: dict[str, Any] = {
        "max_tokens": config["maximum_output_tokens"],
        "timeout": config["provider_timeout_seconds"],
        # LiteLLM's OpenRouter adapter otherwise forwards the top-level
        # reasoning_effort parameter, which is not the OpenRouter wire contract.
        "reasoning_effort": None,
    }
    effort = config["reasoning_effort"]
    if effort not in (None, "default"):
        call_kwargs["extra_body"] = {
            "reasoning": {"enabled": False} if effort == "none" else {"effort": effort}
        }
    kwargs: dict[str, Any] = {
        "llm_call_kwargs": call_kwargs,
        "reasoning_effort": None,
    }
    if config["api_base"] is not None:
        kwargs["api_base"] = config["api_base"]
    if config["temperature"] is not None:
        kwargs["temperature"] = float(config["temperature"])
    return kwargs


async def _worker_run(config: dict[str, Any]) -> None:
    limits = RpcLimits(**config["rpc_limits"])
    client = _RpcClient(config["rpc_socket"], config["nonce"], limits)
    credential_name = config["credential_env_name"]
    credential = _bounded_regular_file(
        Path(config["credential_file"]),
        maximum=64 * 1024,
        require_private_mode=True,
    )
    try:
        credential_text = credential.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProgramRuntimeError("worker credential is not UTF-8") from exc
    if not credential_text or "\0" in credential_text:
        raise ProgramRuntimeError("worker credential is empty or invalid")
    os.environ[credential_name] = credential_text
    os.environ[_LITELLM_OPENROUTER_KEY_ENV] = credential_text
    environment = _RemoteEnvironment(
        client, default_user=config["default_user"], limits=limits
    )
    stage = "candidate_import"
    try:
        try:
            # The candidate overlay is the only object mounted at this package path.
            from harbor.agents.base import BaseAgent
            from harbor.agents.terminus_2.terminus_2 import Terminus2
            from harbor.llms.base import OutputLengthExceededError
            from harbor.models.agent.context import AgentContext

            stage = "candidate_type"
            if not isinstance(Terminus2, type) or not issubclass(Terminus2, BaseAgent):
                raise ProgramRuntimeError(
                    "candidate Terminus2 must implement the pinned Harbor BaseAgent API"
                )
            stage = "candidate_origin"
            module = sys.modules.get(Terminus2.__module__)
            origin = Path(
                getattr(getattr(module, "__spec__", None), "origin", "")
            ).resolve()
            overlay = Path(_GUEST_HARBOR_ROOT / CANDIDATE_SUBTREE)
            if origin != overlay / "terminus_2.py":
                raise ProgramRuntimeError(
                    "Terminus-2 did not resolve from the candidate overlay"
                )
            kwargs = _worker_solver_kwargs(config)
            stage = "agent_construct"
            agent = Terminus2(
                logs_dir=Path(_GUEST_LOGS_ROOT),
                model_name=config["model_name"],
                **kwargs,
            )
            context = AgentContext()
            stage = "agent_setup"
            await agent.setup(environment)
            stage = "agent_run"
            spent_its_allowance = False
            try:
                await agent.run(config["instruction"], environment, context)
            except BaseException as exc:
                if not (
                    _is_own_budget_exhaustion(exc)
                    or isinstance(exc, OutputLengthExceededError)
                ):
                    raise
                # Exhausted bounded length recovery is also a solver failure,
                # not an apparatus fault that may exclude a difficult task.
                # The cell's allowance is part of the measurement, not a fault:
                # an agent that cannot answer within a generous budget has failed
                # the task. Ending the turn here hands the container to the
                # verifier, which grades whatever is actually in it, instead of
                # voiding a comparison over the hardest tasks in the census.
                spent_its_allowance = True
            stage = "context_finalize"
            agent.populate_context_post_run(context)
            payload = validate_agent_context(
                context.model_dump(mode="json"), forbidden_literals=(credential_text,)
            )
            stage = "context_write"
            _exclusive_write(Path(config["context_result"]), _canonical_bytes(payload))
            _exclusive_write(
                Path(config["worker_status"]),
                _canonical_bytes(
                    {"ok": True, "spent_its_allowance": spent_its_allowance}
                ),
            )
        except BaseException as exc:
            # Persist the causal traceback before the stage wrapper strips it.
            # This remains diagnostic data, never an admission verdict.
            try:
                detail = ''.join(traceback.format_exception(exc))[-24000:]
                detail = detail.replace(credential_text, '[redacted]')
                _exclusive_write(Path(config['worker_status']).parent/'worker-error.json',
                    _canonical_bytes({'stage':stage, 'exception_type':type(exc).__name__,
                                      'traceback':detail}))
            except Exception:
                pass
            if isinstance(exc, ProgramWorkerStageError):
                raise
            raise ProgramWorkerStageError(stage, type(exc).__name__) from exc
    finally:
        await client.close()
        os.environ.pop(credential_name, None)
        os.environ.pop(_LITELLM_OPENROUTER_KEY_ENV, None)


def _worker_main(path: Path) -> int:
    config = _load_worker_input(path)
    relay = UnixProviderRelay(config["provider_socket"])
    try:
        relay.start()
        asyncio.run(_worker_run(config))
    except BaseException as exc:
        status = Path(config["worker_status"])
        if not status.exists() and not status.is_symlink():
            try:
                failure = {
                    "ok": False,
                    "error_type": type(exc).__name__,
                }
                if isinstance(exc, ProgramWorkerStageError):
                    failure["error_stage"] = exc.stage
                    failure["cause_type"] = exc.cause_type
                _exclusive_write(
                    status,
                    _canonical_bytes(failure),
                )
            except Exception:
                pass
        raise
    finally:
        relay.close()
    return 0


def _main(arguments: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if len(args) == 2 and args[0] == "--worker":
        return _worker_main(Path(args[1]))
    raise SystemExit("program connector must be launched by the Harbor controller")


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "AGENT_IMPORT_PATH",
    "AGENT_NAME",
    "APPROVED_HARBOR_CONTROLLER_COMMIT",
    "APPROVED_HARBOR_CONTROLLER_TREE",
    "OPENROUTER_API_BASE",
    "PROVIDER_DIAGNOSTICS_METADATA_KEY",
    "PROVIDER_DIAGNOSTICS_NAME",
    "CandidateContextError",
    "CandidateOverlayError",
    "CandidateOverlayIdentity",
    "CandidateTerminus2Connector",
    "ProgramAgentLauncher",
    "ProgramBridgeUnavailable",
    "ProgramRpcError",
    "ProgramRpcLimitExceeded",
    "ProgramRuntimeError",
    "RpcLimits",
    "attach_provider_diagnostics",
    "build_bwrap_command",
    "deadline_was_an_upstream_stall",
    "decode_rpc_request",
    "inspect_candidate_overlay",
    "stage_candidate_overlay",
    "validate_agent_context",
]
