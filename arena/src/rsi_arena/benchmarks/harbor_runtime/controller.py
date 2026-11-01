"""Pinned Harbor process boundary for one external measurement cell."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ContextManager, Iterator, Mapping, Protocol, Sequence

from .worktree_registry import WorktreeRegistryGuard
from .provider_broker import HOST_TMPDIR_ENV

APPROVED_HARBOR_CONTROLLER_COMMIT = "7db020ba5a5ceee918351dd8fc374d4d60bad442"
APPROVED_HARBOR_CONTROLLER_TREE = "03bea3ef9612a7381a2fa2f3955eaeb22324e9ff"
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_GENERAL_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_BASE_ENV_NAMES = ("PATH", "LANG", "LC_ALL", "DOCKER_HOST", "DOCKER_CONFIG", HOST_TMPDIR_ENV)
_SENSITIVE_KWARG_TOKENS = ("key", "token", "secret", "password", "credential")
TRUSTED_BUILD_NETWORK_OVERLAY_CONTENT = (
    b"services:\n  main:\n    build:\n      network: host\n"
)
TRUSTED_BUILD_NETWORK_OVERLAY_SHA256 = (
    "31c9dd6a4c3fbd82a13010b62e4e6298cf557c138345f7903aaaeafc197118ec"
)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_DOCKER_INSPECT_MISSING_RE = re.compile(
    r"(?:Error: |Error response from daemon: )"
    r"No such (?:object|container): (?P<object_id>\S+)"
)


class HarborRuntimeError(RuntimeError):
    """The trusted Harbor apparatus failed before producing a valid record."""


class HarborRuntimeUnavailable(HarborRuntimeError):
    """A required public Harbor integration has not been configured."""


class HarborTimeoutError(HarborRuntimeError):
    """The outer Harbor process exceeded its fixed cell deadline."""


class HarborAbortedError(HarborRuntimeError):
    """The cell was stopped because nobody will read its measurement."""


class AbortSignal(Protocol):
    """Read side of a caller-scoped request to stop in-flight cells."""

    def is_set(self) -> bool: ...


class _CellAborted(BaseException):
    """Internal signal that the wait ended for an abort, not for the cell."""


# How long a waiting cell may stay unaware that it was aborted. The wait is
# otherwise a single blocking join on a process whose deadline is measured in
# tens of minutes.
_ABORT_POLL_SECONDS = 1.0


class HarborCredentialBoundaryError(HarborRuntimeError):
    """A candidate-writable output contained or concealed a host credential."""


class ProgramCheckoutError(HarborRuntimeError):
    """A program id could not be materialized as one clean detached checkout."""


@dataclass(frozen=True, slots=True)
class TrustedBuildNetworkOverlaySpec:
    """Operator-attested build-only Compose overlay.

    The one accepted document changes Docker's build network only.  In
    particular, it cannot add a runtime ``network_mode`` or override Harbor's
    no-network policy for the candidate and verifier containers.
    """

    path: Path
    sha256: str = TRUSTED_BUILD_NETWORK_OVERLAY_SHA256

    def __post_init__(self) -> None:
        path = Path(self.path).expanduser()
        if not path.is_absolute():
            raise ValueError("trusted build-network overlay path must be absolute")
        if _HEX64.fullmatch(self.sha256) is None:
            raise ValueError(
                "trusted build-network overlay digest must be lowercase 64-hex"
            )
        if self.sha256 != TRUSTED_BUILD_NETWORK_OVERLAY_SHA256:
            raise ValueError("trusted build-network overlay digest is not approved")
        object.__setattr__(self, "path", path)


@dataclass(frozen=True, slots=True)
class TrustedBuildNetworkOverlayIdentity:
    source_path: Path
    compose_path: Path
    sha256: str

    def __post_init__(self) -> None:
        source = Path(self.source_path)
        compose = Path(self.compose_path)
        if not source.is_absolute() or not compose.is_absolute():
            raise ValueError("trusted build-network overlay paths must be absolute")
        if self.sha256 != TRUSTED_BUILD_NETWORK_OVERLAY_SHA256:
            raise ValueError("trusted build-network overlay identity is not approved")
        object.__setattr__(self, "source_path", source)
        object.__setattr__(self, "compose_path", compose)


@dataclass(frozen=True, slots=True)
class HarborControllerSpec:
    root: Path
    approved_commit: str = APPROVED_HARBOR_CONTROLLER_COMMIT
    approved_tree: str = APPROVED_HARBOR_CONTROLLER_TREE
    docker_executable: Path = Path("/usr/bin/docker")

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser()
        docker = Path(self.docker_executable).expanduser()
        if not root.is_absolute() or not docker.is_absolute():
            raise ValueError("Harbor and Docker paths must be absolute")
        if _HEX40.fullmatch(self.approved_commit) is None:
            raise ValueError("approved Harbor commit must be lowercase 40-hex")
        if _HEX40.fullmatch(self.approved_tree) is None:
            raise ValueError("approved Harbor tree must be lowercase 40-hex")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "docker_executable", docker)

    @property
    def executable(self) -> Path:
        return self.root / ".venv/bin/harbor"


@dataclass(frozen=True, slots=True)
class HarborControllerIdentity:
    root: Path
    executable: Path
    docker_executable: Path
    commit: str
    tree: str


@dataclass(frozen=True, slots=True)
class ProgramCheckout:
    program_id: str
    commit_sha: str
    tree_sha: str
    path: Path

    def __post_init__(self) -> None:
        if _HEX40.fullmatch(self.program_id) is None:
            raise ValueError("program_id must be lowercase 40-hex")
        if self.commit_sha != self.program_id:
            raise ValueError("program checkout commit differs from program_id")
        if _HEX40.fullmatch(self.tree_sha) is None:
            raise ValueError("program tree must be lowercase 40-hex")
        path = Path(self.path)
        if not path.is_absolute():
            raise ValueError("program checkout path must be absolute")
        object.__setattr__(self, "path", path)


@dataclass(frozen=True, slots=True)
class HarborAgentInvocation:
    """Name-only agent configuration; secret values never enter this record."""

    launcher_id: str
    agent_name: str
    model_name: str | None = None
    agent_kwargs: tuple[tuple[str, str], ...] = ()
    secret_env_names: tuple[str, ...] = ()
    apparatus_only: bool = False

    def __post_init__(self) -> None:
        for label, value in (
            ("launcher_id", self.launcher_id),
            ("agent_name", self.agent_name),
        ):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 512
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError(f"{label} is invalid")
        if self.model_name is not None and (
            not isinstance(self.model_name, str)
            or not self.model_name
            or len(self.model_name) > 512
            or any(ord(character) < 32 for character in self.model_name)
        ):
            raise ValueError("model_name is invalid")
        normalized_kwargs: list[tuple[str, str]] = []
        for item in self.agent_kwargs:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(isinstance(part, str) and part for part in item)
            ):
                raise ValueError("agent kwargs must be non-empty text pairs")
            key, value = item
            if key == "maximum_output_tokens":
                if not re.fullmatch(r"[1-9][0-9]{0,5}", value) or int(value) > 262_144:
                    raise ValueError("maximum_output_tokens must be a bounded integer")
            elif key == "maximum_total_output_tokens":
                if not re.fullmatch(r"[1-9][0-9]{0,7}", value) or int(value) > 16_777_216:
                    raise ValueError("maximum_total_output_tokens must be a bounded integer")
            elif any(token in key.lower() for token in _SENSITIVE_KWARG_TOKENS):
                raise ValueError("secret-bearing agent kwargs are forbidden")
            if any(ord(character) < 32 for character in key + value):
                raise ValueError("agent kwargs contain control characters")
            normalized_kwargs.append((key, value))
        if len({key for key, _ in normalized_kwargs}) != len(normalized_kwargs):
            raise ValueError("agent kwargs contain duplicate keys")
        names = tuple(self.secret_env_names)
        if len(names) != len(set(names)) or any(
            _ENV_NAME.fullmatch(name) is None for name in names
        ):
            raise ValueError("secret environment names are invalid or duplicated")
        object.__setattr__(self, "agent_kwargs", tuple(sorted(normalized_kwargs)))
        object.__setattr__(self, "secret_env_names", names)


class ProgramLauncher(Protocol):
    """Resolve one checkout and cell into a fixed Harbor agent invocation."""

    def invocation(
        self, *, checkout: ProgramCheckout, cell: Any
    ) -> HarborAgentInvocation: ...


class ProgramCheckoutResolver(Protocol):
    def checkout(self, program_id: str) -> ContextManager[ProgramCheckout]: ...


class UnavailableProgramLauncher:
    """Formal default until the isolated evolved-program bridge is wired."""

    def invocation(
        self, *, checkout: ProgramCheckout, cell: Any
    ) -> HarborAgentInvocation:
        del checkout, cell
        raise HarborRuntimeUnavailable(
            "formal program launcher is unavailable until the isolated "
            "candidate bridge is implemented"
        )


class OracleApparatusLauncher:
    """Provider-free apparatus probe; never a formal candidate launcher."""

    def invocation(
        self, *, checkout: ProgramCheckout, cell: Any
    ) -> HarborAgentInvocation:
        del checkout, cell
        return HarborAgentInvocation(
            launcher_id="harbor-oracle-apparatus",
            agent_name="oracle",
            apparatus_only=True,
        )


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _git_text(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
            env=_git_env(),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ProgramCheckoutError("trusted Git identity probe failed") from exc
    return result.stdout


def clean_git_identity(root: Path) -> tuple[str, str]:
    """Return commit/tree only for an ordinary clean, unmasked worktree."""

    resolved = Path(root).expanduser().resolve(strict=True)
    top = Path(_git_text(resolved, "rev-parse", "--show-toplevel").strip()).resolve()
    status = _git_text(resolved, "status", "--porcelain=v1", "--untracked-files=all")
    flags = _git_text(resolved, "ls-files", "-v", "-z", "--", ".")
    commit = _git_text(resolved, "rev-parse", "HEAD^{commit}").strip()
    tree = _git_text(resolved, "rev-parse", "HEAD^{tree}").strip()
    records = [record for record in flags.split("\0") if record]
    if (
        top != resolved
        or status.strip()
        or not records
        or any(not record.startswith("H ") for record in records)
        or _HEX40.fullmatch(commit) is None
        or _HEX40.fullmatch(tree) is None
    ):
        raise ProgramCheckoutError("Git worktree is not a clean ordinary checkout")
    return commit, tree


class GitProgramCheckoutResolver:
    """Materialize every program id as a fresh detached Git worktree."""

    def __init__(self, repository: str | Path, worktrees_root: str | Path) -> None:
        source = Path(repository).expanduser()
        target = Path(worktrees_root).expanduser()
        if not source.is_absolute() or not target.is_absolute():
            raise ValueError("program repository and worktrees_root must be absolute")
        self.repository = source.resolve(strict=True)
        self.worktrees_root = target
        if self.repository == target or self.repository in target.parents:
            raise ValueError("worktrees_root cannot be inside the program repository")
        top = _git_text(self.repository, "rev-parse", "--show-toplevel").strip()
        if Path(top).resolve() != self.repository:
            raise ValueError("program repository must be a Git top-level directory")
        self._registry = WorktreeRegistryGuard(self.repository, environment=_git_env())

    @contextmanager
    def checkout(self, program_id: str) -> Iterator[ProgramCheckout]:
        if _HEX40.fullmatch(program_id) is None:
            raise ProgramCheckoutError("program_id must be lowercase 40-hex")
        resolved_commit = _git_text(
            self.repository, "rev-parse", f"{program_id}^{{commit}}"
        ).strip()
        if resolved_commit != program_id:
            raise ProgramCheckoutError(
                "program_id does not resolve to its exact commit"
            )
        self.worktrees_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.worktrees_root.is_symlink() or not self.worktrees_root.is_dir():
            raise ProgramCheckoutError("worktrees_root must be a real directory")
        temporary = Path(
            tempfile.mkdtemp(prefix="rsi-arena-program-", dir=self.worktrees_root)
        )
        temporary.rmdir()
        added = False
        failed = False
        try:
            try:
                with self._registry.hold():
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(self.repository),
                            "-c",
                            "core.hooksPath=/dev/null",
                            "worktree",
                            "add",
                            "--detach",
                            str(temporary),
                            program_id,
                        ],
                        capture_output=True,
                        check=True,
                        timeout=120,
                        env=_git_env(),
                    )
            except (
                OSError,
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
            ) as exc:
                raise ProgramCheckoutError("fresh program checkout failed") from exc
            added = True
            commit, tree = clean_git_identity(temporary)
            if commit != program_id:
                raise ProgramCheckoutError("fresh checkout commit drifted")
            yield ProgramCheckout(program_id, commit, tree, temporary.resolve())
            post_commit, post_tree = clean_git_identity(temporary)
            if post_commit != commit or post_tree != tree:
                raise ProgramCheckoutError("program checkout changed during evaluation")
        except BaseException:
            failed = True
            raise
        finally:
            cleanup_ok = True
            if added:
                try:
                    with self._registry.hold():
                        result = subprocess.run(
                            [
                                "git",
                                "-C",
                                str(self.repository),
                                "worktree",
                                "remove",
                                "--force",
                                str(temporary),
                            ],
                            capture_output=True,
                            timeout=120,
                            env=_git_env(),
                        )
                    cleanup_ok = result.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    cleanup_ok = False
            if temporary.exists() and (not added or cleanup_ok):
                shutil.rmtree(temporary)
            if not cleanup_ok and not failed:
                raise ProgramCheckoutError("program checkout cleanup failed")


def verify_approved_controller(spec: HarborControllerSpec) -> HarborControllerIdentity:
    try:
        root_info = spec.root.lstat()
        root = spec.root.resolve(strict=True)
    except OSError as exc:
        raise HarborRuntimeError("trusted Harbor root is invalid") from exc
    if (
        stat.S_ISLNK(root_info.st_mode)
        or not stat.S_ISDIR(root_info.st_mode)
        or root != spec.root
    ):
        raise HarborRuntimeError("trusted Harbor root is invalid")
    raw_executable = root / ".venv/bin/harbor"
    try:
        executable_info = raw_executable.lstat()
        executable = raw_executable.resolve(strict=True)
    except OSError as exc:
        raise HarborRuntimeError("trusted Harbor executable is unavailable") from exc
    if (
        stat.S_ISLNK(executable_info.st_mode)
        or not stat.S_ISREG(executable_info.st_mode)
        or not os.access(executable, os.X_OK)
    ):
        raise HarborRuntimeError("trusted Harbor executable is unavailable")
    docker = spec.docker_executable.resolve(strict=True)
    if not docker.is_file() or not os.access(docker, os.X_OK):
        raise HarborRuntimeError("trusted Docker executable is unavailable")
    try:
        commit, tree = clean_git_identity(root)
    except ProgramCheckoutError as exc:
        raise HarborRuntimeError("trusted Harbor controller is not clean") from exc
    if commit != spec.approved_commit or tree != spec.approved_tree:
        raise HarborRuntimeError("trusted Harbor controller identity is not approved")
    return HarborControllerIdentity(root, executable, docker, commit, tree)


def _read_trusted_build_network_overlay(
    spec: TrustedBuildNetworkOverlaySpec,
) -> bytes:
    """Read the approved overlay without following or racing a symlink."""

    path = spec.path
    try:
        resolved = path.resolve(strict=True)
        path_info = path.lstat()
    except OSError as exc:
        raise HarborRuntimeError(
            "trusted build-network overlay is unavailable"
        ) from exc
    if resolved != path or stat.S_ISLNK(path_info.st_mode):
        raise HarborRuntimeError(
            "trusted build-network overlay path must be canonical and symlink-free"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HarborRuntimeError(
            "trusted build-network overlay cannot be opened"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise HarborRuntimeError(
                "trusted build-network overlay must be a regular file"
            )
        payload = bytearray()
        while chunk := os.read(descriptor, 4096):
            payload.extend(chunk)
            if len(payload) > len(TRUSTED_BUILD_NETWORK_OVERLAY_CONTENT):
                break
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, name) != getattr(after, name) for name in stable_fields):
        raise HarborRuntimeError("trusted build-network overlay changed while read")
    content = bytes(payload)
    digest = hashlib.sha256(content).hexdigest()
    if digest != spec.sha256 or content != TRUSTED_BUILD_NETWORK_OVERLAY_CONTENT:
        raise HarborRuntimeError(
            "trusted build-network overlay content is not approved"
        )
    return content


def materialize_trusted_build_network_overlay(
    spec: TrustedBuildNetworkOverlaySpec,
    runtime_dir: Path,
) -> TrustedBuildNetworkOverlayIdentity:
    """Snapshot the attested overlay into the trusted per-cell runtime."""

    content = _read_trusted_build_network_overlay(spec)
    target = runtime_dir / "trusted-build-network-host.yaml"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(target, flags, 0o400)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    except OSError as exc:
        raise HarborRuntimeError(
            "trusted build-network overlay snapshot could not be created"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return TrustedBuildNetworkOverlayIdentity(
        source_path=spec.path,
        compose_path=target.resolve(strict=True),
        sha256=spec.sha256,
    )


@dataclass(frozen=True, slots=True)
class HarborCellRequest:
    task_dir: Path
    task_name: str
    cell_id: str
    invocation: HarborAgentInvocation
    wall_timeout_seconds: float
    verifier_timeout_multiplier: float = 1.0
    verifier_env: tuple[tuple[str, str], ...] = ()
    verifier_import_path: str | None = None
    verifier_kwargs: tuple[tuple[str, str], ...] = ()
    agent_timeout_multiplier: float = 1.0

    def __post_init__(self) -> None:
        task_dir = Path(self.task_dir)
        if not task_dir.is_absolute():
            raise ValueError("task_dir must be absolute")
        for name in ("task_name", "cell_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty text")
        if (
            isinstance(self.wall_timeout_seconds, bool)
            or not isinstance(self.wall_timeout_seconds, (int, float))
            or not 0 < float(self.wall_timeout_seconds) <= 86_400
        ):
            raise ValueError("wall_timeout_seconds must be in (0, 86400]")
        if not isinstance(self.invocation, HarborAgentInvocation):
            raise TypeError("invocation must be a HarborAgentInvocation")
        if (type(self.agent_timeout_multiplier) not in (int,float)
                or not math.isfinite(self.agent_timeout_multiplier) or self.agent_timeout_multiplier <= 0):
            raise ValueError("agent_timeout_multiplier must be positive")
        if (
            isinstance(self.verifier_timeout_multiplier, bool)
            or not isinstance(self.verifier_timeout_multiplier, (int, float))
            or not math.isfinite(float(self.verifier_timeout_multiplier))
            or float(self.verifier_timeout_multiplier) <= 0.0
        ):
            raise ValueError("verifier_timeout_multiplier must be positive")
        object.__setattr__(self, "task_dir", task_dir)
        object.__setattr__(
            self, "wall_timeout_seconds", float(self.wall_timeout_seconds)
        )
        object.__setattr__(
            self,
            "verifier_timeout_multiplier",
            float(self.verifier_timeout_multiplier),
        )
        verifier_env = tuple(self.verifier_env)
        if len({name for name, _ in verifier_env}) != len(verifier_env) or any(
            not isinstance(name, str)
            or _GENERAL_ENV_NAME.fullmatch(name) is None
            or not isinstance(value, str)
            or not value
            or "\x00" in value
            for name, value in verifier_env
        ):
            raise ValueError("verifier_env must contain unique text pairs")
        object.__setattr__(self, "verifier_env", tuple(sorted(verifier_env)))
        import_path = self.verifier_import_path
        verifier_kwargs = tuple(self.verifier_kwargs)
        if import_path is None:
            if verifier_kwargs:
                raise ValueError("verifier kwargs require a verifier import path")
        elif (
            not isinstance(import_path, str)
            or not import_path
            or any(ord(character) < 32 for character in import_path)
            or len({name for name, _ in verifier_kwargs}) != len(verifier_kwargs)
            or any(
                not isinstance(name, str)
                or not name
                or not isinstance(value, str)
                or "\x00" in name + value
                for name, value in verifier_kwargs
            )
        ):
            raise ValueError("custom verifier binding is invalid")
        object.__setattr__(self, "verifier_kwargs", tuple(sorted(verifier_kwargs)))


@dataclass(frozen=True, slots=True)
class HarborRunResult:
    job_dir: Path
    not_before_ns: int
    controller: HarborControllerIdentity
    invocation: HarborAgentInvocation
    process_returncode: int = 0
    build_network_overlay: TrustedBuildNetworkOverlayIdentity | None = None


class CellPhysicalRuntime(Protocol):
    controller: HarborControllerSpec
    runtime_root: Path

    def run(
        self, request: HarborCellRequest, *, abort: AbortSignal | None = None
    ) -> HarborRunResult: ...


def build_harbor_command(
    executable: Path,
    request: HarborCellRequest,
    *,
    jobs_dir: Path,
    build_network_overlay: TrustedBuildNetworkOverlayIdentity | None = None,
) -> tuple[str, ...]:
    command = [
        str(executable),
        "run",
        "-p",
        str(request.task_dir),
        "--agent",
        request.invocation.agent_name,
        "--jobs-dir",
        str(jobs_dir),
        "--job-name",
        "cell",
        "-n",
        "1",
        "--max-retries",
        "0",
        "-q",
        "-y",
    ]
    if request.invocation.model_name is not None:
        command += ["-m", request.invocation.model_name]
    for key, value in request.invocation.agent_kwargs:
        # Harbor's CLI JSON-parses kwarg values.  Encode an explicit JSON
        # string so digit-only paths/ids do not silently become another type.
        command += ["--agent-kwarg", f"{key}={json.dumps(value)}"]
    for name in request.invocation.secret_env_names:
        command += ["--agent-env", f"{name}=${{{name}}}"]
    if request.agent_timeout_multiplier != 1.0:
        command += ["--agent-timeout-multiplier",str(request.agent_timeout_multiplier)]
    if request.verifier_timeout_multiplier != 1.0:
        command += [
            "--verifier-timeout-multiplier",
            str(request.verifier_timeout_multiplier),
        ]
    for name, value in request.verifier_env:
        command += ["--verifier-env", f"{name}={value}"]
    if request.verifier_import_path is not None:
        command += ["--verifier", request.verifier_import_path]
        for name, value in request.verifier_kwargs:
            command += ["--verifier-kwarg", f"{name}={value}"]
    if build_network_overlay is not None:
        if not isinstance(build_network_overlay, TrustedBuildNetworkOverlayIdentity):
            raise TypeError(
                "build_network_overlay must be a TrustedBuildNetworkOverlayIdentity"
            )
        _read_trusted_build_network_overlay(
            TrustedBuildNetworkOverlaySpec(
                build_network_overlay.compose_path,
                build_network_overlay.sha256,
            )
        )
        command += [
            "--extra-docker-compose",
            str(build_network_overlay.compose_path),
        ]
    return tuple(command)


def build_sanitized_environment(
    source: Mapping[str, str],
    *,
    home: Path,
    temporary: Path,
    secret_env_names: Sequence[str] = (),
    trusted_import_roots: Sequence[Path] = (),
) -> dict[str, str]:
    """Build a child environment from an allowlist, never ambient wholesale."""

    environment = {name: source[name] for name in _BASE_ENV_NAMES if name in source}
    environment.setdefault("PATH", "/usr/bin:/bin")
    environment["LANG"] = source.get("LANG", "C.UTF-8")
    environment["LC_ALL"] = source.get("LC_ALL", "C.UTF-8")
    environment["HOME"] = str(home)
    environment["TMPDIR"] = str(temporary)
    environment["PYTHONNOUSERSITE"] = "1"
    roots: list[str] = []
    for raw_root in trusted_import_roots:
        root = Path(raw_root)
        if (
            not root.is_absolute()
            or root.is_symlink()
            or not root.is_dir()
            or os.pathsep in str(root)
        ):
            raise HarborRuntimeError("trusted import root is invalid")
        roots.append(str(root.resolve(strict=True)))
    if roots:
        environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(roots))
    for name in secret_env_names:
        if _ENV_NAME.fullmatch(name) is None:
            raise HarborRuntimeError("configured secret environment name is invalid")
        value = source.get(name)
        if not isinstance(value, str) or not value or "\x00" in value:
            raise HarborRuntimeError(
                f"required host environment variable is missing: {name}"
            )
        environment[name] = value
    return environment


def _docker_env(docker_host: str = "") -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if docker_host:
        environment["DOCKER_HOST"] = docker_host
    return environment


def docker_container_inventory(
    docker: Path, *, docker_host: str = ""
) -> list[dict[str, object]]:
    listed = subprocess.run(
        [str(docker), "ps", "-aq", "--filter", "label=com.docker.compose.project"],
        capture_output=True,
        text=True,
        timeout=60,
        env=_docker_env(docker_host),
    )
    if listed.returncode != 0:
        raise HarborRuntimeError("could not inventory Compose containers")
    identifiers = [line for line in listed.stdout.splitlines() if line]
    if not identifiers:
        return []
    inspected = subprocess.run(
        [str(docker), "inspect", *identifiers],
        capture_output=True,
        text=True,
        timeout=120,
        env=_docker_env(docker_host),
    )
    disappeared_ids: set[str] = set()
    if inspected.returncode != 0:
        requested_ids = set(identifiers)
        errors = [
            line.strip() for line in inspected.stderr.splitlines() if line.strip()
        ]
        for error in errors:
            match = _DOCKER_INSPECT_MISSING_RE.fullmatch(error)
            if match is None or match.group("object_id") not in requested_ids:
                raise HarborRuntimeError("could not inspect Compose containers")
            disappeared_ids.add(match.group("object_id"))
        if not disappeared_ids:
            raise HarborRuntimeError("could not inspect Compose containers")
    output = inspected.stdout
    if not output.strip() and disappeared_ids == set(identifiers):
        output = "[]"
    try:
        rows = json.loads(output)
    except json.JSONDecodeError as exc:
        raise HarborRuntimeError("Docker inventory is invalid JSON") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise HarborRuntimeError("Docker inventory is malformed")
    if disappeared_ids and len(rows) != len(identifiers) - len(disappeared_ids):
        raise HarborRuntimeError("Docker inventory is malformed")
    return rows


def scoped_compose_container_ids(
    inventory: Sequence[Mapping[str, object]],
    cell_root: Path,
    *,
    running_only: bool,
) -> tuple[str, ...]:
    """Select Compose projects proven by a host mount below this cell only."""

    root = Path(cell_root).resolve()
    projects: set[str] = set()
    for row in inventory:
        mounts = row.get("Mounts")
        config = row.get("Config")
        labels = config.get("Labels") if isinstance(config, Mapping) else None
        if not isinstance(mounts, list) or not isinstance(labels, Mapping):
            continue
        project = labels.get("com.docker.compose.project")
        if not isinstance(project, str) or not project:
            continue
        for mount in mounts:
            source = mount.get("Source") if isinstance(mount, Mapping) else None
            if not isinstance(source, str) or not source:
                continue
            resolved = Path(source).resolve()
            if resolved == root or root in resolved.parents:
                projects.add(project)
                break
    selected: list[str] = []
    for row in inventory:
        config = row.get("Config")
        labels = config.get("Labels") if isinstance(config, Mapping) else None
        state = row.get("State")
        identifier = row.get("Id")
        if (
            not isinstance(labels, Mapping)
            or labels.get("com.docker.compose.project") not in projects
            or not isinstance(identifier, str)
            or not identifier
        ):
            continue
        if running_only and (
            not isinstance(state, Mapping) or state.get("Running") is not True
        ):
            continue
        selected.append(identifier)
    return tuple(sorted(set(selected)))


def cleanup_scoped_compose_cell(
    docker: Path,
    cell_root: Path,
    *,
    cleanup_grace_seconds: float,
    docker_host: str = "",
) -> None:
    def inventory() -> list[dict[str, object]]:
        return docker_container_inventory(docker, docker_host=docker_host)

    rows = inventory()
    scoped = scoped_compose_container_ids(rows, cell_root, running_only=False)
    running = scoped_compose_container_ids(rows, cell_root, running_only=True)
    failed = False
    if running:
        stopped = subprocess.run(
            [
                str(docker),
                "stop",
                "--time",
                str(max(1, int(cleanup_grace_seconds))),
                *running,
            ],
            capture_output=True,
            text=True,
            timeout=cleanup_grace_seconds + 60,
            env=_docker_env(docker_host),
        )
        failed = stopped.returncode != 0
    if scoped:
        removed = subprocess.run(
            [str(docker), "rm", "-f", *scoped],
            capture_output=True,
            text=True,
            timeout=cleanup_grace_seconds + 60,
            env=_docker_env(docker_host),
        )
        failed = failed or removed.returncode != 0
    remaining = scoped_compose_container_ids(inventory(), cell_root, running_only=False)
    if remaining or failed:
        raise HarborRuntimeError("scoped Harbor container cleanup failed")


def terminate_process_group_and_cleanup(
    process: subprocess.Popen[bytes],
    *,
    docker: Path,
    cell_root: Path,
    cleanup_grace_seconds: float,
    docker_host: str = "",
) -> None:
    try:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=cleanup_grace_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=30)
    finally:
        cleanup_scoped_compose_cell(
            docker,
            cell_root,
            cleanup_grace_seconds=cleanup_grace_seconds,
            docker_host=docker_host,
        )


def scrub_secret_literals(
    root: Path,
    values: Sequence[str],
    *,
    max_paths: int = 10_000,
    max_total_bytes: int = 512 * 1024 * 1024,
) -> None:
    """Delete scoped output containing a secret and fail without echoing it."""

    literals = tuple(value.encode("utf-8") for value in values if value)
    if not literals:
        return
    boundary = Path(root).resolve(strict=True)
    violation = False
    total = 0
    paths = tuple(boundary.rglob("*"))
    if len(paths) > max_paths:
        shutil.rmtree(boundary)
        raise HarborCredentialBoundaryError("credential scan exceeded its path budget")
    for path in paths:
        try:
            info = path.lstat()
            path.resolve(strict=True).relative_to(boundary)
        except (FileNotFoundError, OSError):
            continue
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
            shutil.rmtree(boundary)
            raise HarborCredentialBoundaryError(
                "credential scan exceeded its byte budget"
            )
        relative_name = (
            path.relative_to(boundary)
            .as_posix()
            .encode("utf-8", errors="surrogateescape")
        )
        leaked = any(literal in relative_name for literal in literals)
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            tails = {literal: b"" for literal in literals}
            while chunk := os.read(descriptor, 1024 * 1024):
                for literal in literals:
                    combined = tails[literal] + chunk
                    if literal in combined:
                        leaked = True
                    overlap = max(0, len(literal) - 1)
                    tails[literal] = combined[-overlap:] if overlap else b""
        except OSError:
            leaked = True
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if leaked:
            path.unlink(missing_ok=True)
            violation = True
    if violation:
        raise HarborCredentialBoundaryError(
            "candidate-writable output contained or concealed a credential"
        )


def _load_overlay_lock(path: Path, *, not_before_ns: int) -> Mapping[str, object]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise HarborRuntimeError("Harbor overlay provenance lock is missing") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > 4 * 1024 * 1024
        or info.st_mtime_ns < not_before_ns
    ):
        raise HarborRuntimeError("Harbor overlay provenance lock is invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarborRuntimeError(
            "Harbor overlay provenance lock is unreadable"
        ) from exc
    if not isinstance(value, Mapping):
        raise HarborRuntimeError("Harbor overlay provenance lock is malformed")
    return value


def _assert_overlay_trial_lock(
    value: Mapping[str, object],
    expected: TrustedBuildNetworkOverlayIdentity,
) -> None:
    environment = value.get("environment")
    if not isinstance(environment, Mapping) or environment.get(
        "extra_docker_compose"
    ) != [str(expected.compose_path)]:
        raise HarborRuntimeError(
            "Harbor lock did not bind the trusted build-network overlay path"
        )
    expected_lock = {
        "path": str(expected.compose_path),
        "digest": f"sha256:{expected.sha256}",
    }
    if value.get("extra_docker_compose") != [expected_lock]:
        raise HarborRuntimeError(
            "Harbor lock did not bind the trusted build-network overlay digest"
        )


def verify_build_network_overlay_provenance(
    job_dir: Path,
    *,
    expected: TrustedBuildNetworkOverlayIdentity,
    not_before_ns: int,
) -> None:
    """Require both Harbor job and trial locks to attest the overlay snapshot."""

    job_lock = _load_overlay_lock(job_dir / "lock.json", not_before_ns=not_before_ns)
    trials = job_lock.get("trials")
    if (
        not isinstance(trials, list)
        or len(trials) != 1
        or not isinstance(trials[0], Mapping)
    ):
        raise HarborRuntimeError("Harbor job lock does not contain exactly one trial")
    _assert_overlay_trial_lock(trials[0], expected)
    trial_locks = [
        child / "lock.json"
        for child in job_dir.iterdir()
        if child.is_dir() and (child / "result.json").is_file()
    ]
    if len(trial_locks) != 1:
        raise HarborRuntimeError(
            "Harbor overlay provenance does not contain exactly one completed trial"
        )
    trial_lock = _load_overlay_lock(trial_locks[0], not_before_ns=not_before_ns)
    _assert_overlay_trial_lock(trial_lock, expected)


class HarborRuntime:
    """Launch exactly one pinned Harbor job in an isolated host runtime root."""

    def __init__(
        self,
        controller: HarborControllerSpec,
        runtime_root: str | Path,
        *,
        environment: Mapping[str, str] | None = None,
        cleanup_grace_seconds: float = 30.0,
        allowed_secret_env_names: Sequence[str] = (),
        build_network_overlay: TrustedBuildNetworkOverlaySpec | None = None,
        trusted_import_roots: Sequence[str | Path] = (),
    ) -> None:
        root = Path(runtime_root).expanduser()
        if not root.is_absolute():
            raise ValueError("runtime_root must be absolute")
        if (
            isinstance(cleanup_grace_seconds, bool)
            or not isinstance(cleanup_grace_seconds, (int, float))
            or not 0 < float(cleanup_grace_seconds) <= 300
        ):
            raise ValueError("cleanup_grace_seconds must be in (0, 300]")
        self.controller = controller
        self.runtime_root = root
        self.environment = environment if environment is not None else os.environ
        self.cleanup_grace_seconds = float(cleanup_grace_seconds)
        if build_network_overlay is not None and not isinstance(
            build_network_overlay, TrustedBuildNetworkOverlaySpec
        ):
            raise TypeError(
                "build_network_overlay must be a TrustedBuildNetworkOverlaySpec"
            )
        self.build_network_overlay = build_network_overlay
        self.trusted_import_roots = tuple(
            Path(item).expanduser() for item in trusted_import_roots
        )
        allowed_names = tuple(allowed_secret_env_names)
        if len(allowed_names) != len(set(allowed_names)) or any(
            _ENV_NAME.fullmatch(name) is None for name in allowed_names
        ):
            raise ValueError("allowed secret environment names are invalid")
        self.allowed_secret_env_names = frozenset(allowed_names)

    @staticmethod
    def _await_exit(
        process: subprocess.Popen[bytes],
        *,
        timeout_seconds: float,
        abort: AbortSignal | None,
    ) -> None:
        """Join the cell without becoming deaf to an abort for its whole deadline."""

        if abort is None:
            process.wait(timeout=timeout_seconds)
            return
        deadline = time.monotonic() + timeout_seconds
        while True:
            if abort.is_set():
                raise _CellAborted
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("harbor", timeout_seconds)
            try:
                process.wait(timeout=min(remaining, _ABORT_POLL_SECONDS))
                return
            except subprocess.TimeoutExpired:
                continue

    def run(
        self, request: HarborCellRequest, *, abort: AbortSignal | None = None
    ) -> HarborRunResult:
        if not set(request.invocation.secret_env_names).issubset(
            self.allowed_secret_env_names
        ):
            raise HarborRuntimeError(
                "launcher requested an unconfigured secret environment name"
            )
        if abort is not None and abort.is_set():
            # A cell that is already abandoned must not buy a container.
            raise HarborAbortedError("Harbor cell was aborted before it started")
        identity = verify_approved_controller(self.controller)
        self.runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.runtime_root.is_symlink() or not self.runtime_root.is_dir():
            raise HarborRuntimeError("runtime_root must be a real directory")
        cell_root = Path(
            tempfile.mkdtemp(prefix="rsi-arena-cell-", dir=self.runtime_root)
        ).resolve()
        host_home = cell_root / ".runtime/home"
        host_tmp = cell_root / ".runtime/tmp"
        host_home.mkdir(parents=True, mode=0o700)
        host_tmp.mkdir(parents=True, mode=0o700)
        overlay_identity = (
            materialize_trusted_build_network_overlay(
                self.build_network_overlay, cell_root / ".runtime"
            )
            if self.build_network_overlay is not None
            else None
        )
        marker = cell_root / ".provenance-start"
        marker.write_text("fresh\n", encoding="ascii")
        not_before_ns = marker.stat().st_mtime_ns
        command = build_harbor_command(
            identity.executable,
            request,
            jobs_dir=cell_root,
            build_network_overlay=overlay_identity,
        )
        child_environment = build_sanitized_environment(
            self.environment,
            home=host_home,
            temporary=host_tmp,
            secret_env_names=request.invocation.secret_env_names,
            trusted_import_roots=self.trusted_import_roots,
        )
        secret_values = tuple(
            child_environment[name] for name in request.invocation.secret_env_names
        )
        docker_host = child_environment.get("DOCKER_HOST", "")
        process: subprocess.Popen[bytes] | None = None
        interrupted: BaseException | None = None
        timed_out = False
        aborted = False
        try:
            with (cell_root / "harbor.log").open("wb") as log:
                process = subprocess.Popen(
                    command,
                    cwd=str(cell_root / ".runtime"),
                    env=child_environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    self._await_exit(
                        process,
                        timeout_seconds=request.wall_timeout_seconds,
                        abort=abort,
                    )
                except subprocess.TimeoutExpired:
                    timed_out = True
                except _CellAborted:
                    aborted = True
                except BaseException as exc:
                    interrupted = exc
                if timed_out or aborted or interrupted is not None:
                    terminate_process_group_and_cleanup(
                        process,
                        docker=identity.docker_executable,
                        cell_root=cell_root / "cell",
                        cleanup_grace_seconds=self.cleanup_grace_seconds,
                        docker_host=docker_host,
                    )
        except (OSError, ValueError) as exc:
            raise HarborRuntimeError("Harbor process could not be launched") from exc
        finally:
            scrub_secret_literals(cell_root, secret_values)
        if interrupted is not None:
            raise interrupted
        if process is None:
            raise HarborRuntimeError("Harbor process was not created")
        if aborted:
            raise HarborAbortedError("Harbor cell was aborted before it finished")
        if timed_out:
            raise HarborTimeoutError("Harbor cell exceeded its outer deadline")
        cleanup_scoped_compose_cell(
            identity.docker_executable,
            cell_root / "cell",
            cleanup_grace_seconds=self.cleanup_grace_seconds,
            docker_host=docker_host,
        )
        job_dir = cell_root / "cell"
        if overlay_identity is not None:
            verify_build_network_overlay_provenance(
                job_dir,
                expected=overlay_identity,
                not_before_ns=not_before_ns,
            )
        return HarborRunResult(
            job_dir=job_dir,
            not_before_ns=not_before_ns,
            controller=identity,
            invocation=request.invocation,
            process_returncode=process.returncode,
            build_network_overlay=overlay_identity,
        )


__all__ = [
    "APPROVED_HARBOR_CONTROLLER_COMMIT",
    "APPROVED_HARBOR_CONTROLLER_TREE",
    "TRUSTED_BUILD_NETWORK_OVERLAY_CONTENT",
    "TRUSTED_BUILD_NETWORK_OVERLAY_SHA256",
    "AbortSignal",
    "CellPhysicalRuntime",
    "GitProgramCheckoutResolver",
    "HarborAbortedError",
    "HarborAgentInvocation",
    "HarborCellRequest",
    "HarborControllerIdentity",
    "HarborControllerSpec",
    "HarborCredentialBoundaryError",
    "HarborRunResult",
    "HarborRuntime",
    "HarborRuntimeError",
    "HarborRuntimeUnavailable",
    "HarborTimeoutError",
    "OracleApparatusLauncher",
    "ProgramCheckout",
    "ProgramCheckoutError",
    "ProgramCheckoutResolver",
    "ProgramLauncher",
    "TrustedBuildNetworkOverlayIdentity",
    "TrustedBuildNetworkOverlaySpec",
    "UnavailableProgramLauncher",
    "build_harbor_command",
    "build_sanitized_environment",
    "clean_git_identity",
    "cleanup_scoped_compose_cell",
    "docker_container_inventory",
    "materialize_trusted_build_network_overlay",
    "scoped_compose_container_ids",
    "scrub_secret_literals",
    "terminate_process_group_and_cleanup",
    "verify_approved_controller",
    "verify_build_network_overlay_provenance",
]
