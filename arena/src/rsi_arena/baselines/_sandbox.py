"""Outer OS sandbox for untrusted coding CLIs.

Codex' ``--sandbox`` flag and Claude Code's permission mode are useful
defence in depth, but neither is Arena's primary trust boundary.  This module
provides the small outer boundary used by both adapters:

* one writable candidate worktree;
* one writable, per-run CLI config directory;
* optional explicitly supplied read-only development packs;
* a minimal read-only system toolchain;
* no ambient host home, sibling worktrees, run store, database socket, or
  hidden benchmark directories in the mount namespace (the explicitly copied
  per-run CLI auth/config remains visible to the whole CLI tool domain);
* a fresh network namespace for the ``offline`` policy.

The production default is fail-closed bubblewrap.  ``host`` exists only for
explicitly marked unsafe tests/development; there is deliberately no silent
fallback when namespaces are unavailable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional, Protocol

from rsi_arena.contracts import canonical_digest

SANDBOX_ADAPTER = "bwrap"
_WORKSPACE = "/workspace"
_CONFIG = "/rsi-arena/config"
_HOME = "/rsi-arena/home"
_GIT_ROOT = "/rsi-arena/git"
_VISIBLE_ROOT = PurePosixPath("/rsi-arena/visible")
_BIN_ROOT = PurePosixPath("/rsi-arena/bin")
_TOOL_ROOT = PurePosixPath("/rsi-arena/tool")


@dataclass(frozen=True)
class SandboxPolicy:
    network: str = "offline"
    env_allowlist: tuple[str, ...] = ("PATH", "HOME", "LANG", "TERM")
    secret_names: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "network": self.network,
                "env_allowlist": sorted(self.env_allowlist),
                "secret_names": sorted(self.secret_names),
            }
        )


class SandboxError(RuntimeError):
    """The requested isolation policy cannot be enforced."""


class SandboxUnavailable(SandboxError):
    """The selected backend is missing or unusable on this host."""


class UnsupportedSandboxPolicy(SandboxError):
    """The backend cannot truthfully implement the requested policy."""


def ensure_private_config_directory(path: Path | str) -> Path:
    """Create or validate a candidate-writable CLI home without following it.

    A reused CLI home may contain candidate-created entries, but its mount
    root itself must remain a user-owned 0700 directory.  Callers that write
    trusted auth/config files into it must use :func:`atomic_private_copy` so
    an entry-level symlink cannot redirect a host write.
    """

    directory = Path(path)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        entry = os.lstat(directory)
    except FileNotFoundError as exc:
        raise SandboxError("private CLI config directory disappeared") from exc
    if not stat.S_ISDIR(entry.st_mode):
        raise SandboxError("private CLI config path must be a non-symlink directory")
    if entry.st_uid != os.geteuid():
        raise SandboxError("private CLI config directory has the wrong owner")
    if stat.S_IMODE(entry.st_mode) != 0o700:
        raise SandboxError("private CLI config directory permissions must be 0700")
    return directory


def atomic_private_copy(source: Path | str, destination: Path | str) -> None:
    """Atomically replace one private config file without following target links."""

    source_path = Path(source).expanduser().resolve(strict=True)
    destination_path = Path(destination)
    parent = ensure_private_config_directory(destination_path.parent)
    source_info = os.lstat(source_path)
    if not stat.S_ISREG(source_info.st_mode):
        raise SandboxError("private config source must be a regular file")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.rsi-arena-", dir=str(parent)
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with source_path.open("rb") as source_stream:
            before = os.fstat(source_stream.fileno())
            with os.fdopen(descriptor, "wb", closefd=True) as target_stream:
                descriptor = -1
                shutil.copyfileobj(source_stream, target_stream)
                target_stream.flush()
                os.fsync(target_stream.fileno())
            after = os.fstat(source_stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise SandboxError("private config source changed while copying")
        # os.replace replaces a symlink/hardlink directory entry itself; it
        # never follows the old destination to a host target.
        os.replace(temporary, destination_path)
        installed = os.lstat(destination_path)
        if (
            not stat.S_ISREG(installed.st_mode)
            or installed.st_uid != os.geteuid()
            or stat.S_IMODE(installed.st_mode) != 0o600
        ):
            raise SandboxError("installed private config file is not safe")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class SandboxLaunch:
    """Fully materialized subprocess launch plus its public audit record."""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    audit: dict


class HarnessSandbox(Protocol):
    def identity_payload(self) -> dict: ...

    def prepare(
        self,
        argv: list[str],
        *,
        executable: Path,
        worktree: Path,
        config_dir: Path,
        config_env_var: str,
        env: dict[str, str],
        policy: SandboxPolicy,
    ) -> SandboxLaunch: ...


def create_harness_sandbox(
    backend: str = "bwrap",
    *,
    bwrap_path: str = "bwrap",
    visible_paths: Iterable[Path | str] = (),
    mutable_mounts: Iterable[tuple[Path | str, Path | str]] = (),
    cli_tool_root: Optional[Path | str] = None,
    allow_unsafe_host: bool = False,
) -> HarnessSandbox:
    """Create a sandbox without silently weakening the requested backend."""

    name = backend.strip().lower()
    if name in {"bwrap", "bubblewrap"}:
        return BubblewrapSandbox(
            bwrap_path=bwrap_path,
            visible_paths=visible_paths,
            mutable_mounts=mutable_mounts,
            cli_tool_root=cli_tool_root,
        )
    if name == "host":
        if not allow_unsafe_host:
            raise SandboxUnavailable(
                "host harness execution is unsafe and may only be enabled with "
                "allow_unsafe_host=True"
            )
        return UnsafeHostSandbox(
            visible_paths=visible_paths,
            mutable_mounts=mutable_mounts,
        )
    raise ValueError(f"unknown harness sandbox backend: {backend!r}")


def validate_sandbox_environment(
    policy: SandboxPolicy,
    request_env: dict[str, str],
    injected_secrets: Iterable[str],
) -> None:
    """Fail closed when the concrete environment exceeds the frozen policy."""

    env_names = {str(name) for name in request_env}
    allowed_env = set(policy.env_allowlist)
    unexpected = sorted(env_names - allowed_env)
    if unexpected:
        raise UnsupportedSandboxPolicy(
            "request environment contains names outside SandboxPolicy.env_allowlist: "
            + ", ".join(unexpected)
        )
    secret_names = {str(name) for name in injected_secrets}
    declared_secrets = set(policy.secret_names)
    undeclared = sorted(secret_names - declared_secrets)
    if undeclared:
        raise UnsupportedSandboxPolicy(
            "harness injects secrets not declared by SandboxPolicy.secret_names: "
            + ", ".join(undeclared)
        )
    overlap = sorted(env_names & secret_names)
    if overlap:
        raise UnsupportedSandboxPolicy(
            "secret names must use the explicit harness secret channel, not "
            "request.env_allowlist: " + ", ".join(overlap)
        )


class UnsafeHostSandbox:
    """Explicit test/development escape hatch; never claims isolation."""

    def __init__(
        self,
        *,
        visible_paths: Iterable[Path | str] = (),
        mutable_mounts: Iterable[tuple[Path | str, Path | str]] = (),
    ) -> None:
        self._visible_paths = tuple(_resolve_existing(p) for p in visible_paths)
        self._visible_digest = _visible_content_digest(self._visible_paths)
        if tuple(mutable_mounts):
            raise SandboxUnavailable("host sandbox cannot materialize guest mounts")

    def identity_payload(self) -> dict:
        return {
            "backend": "host",
            "backend_version": "unsafe-host",
            "filesystem_isolation": False,
            "pid_isolation": False,
            "network_modes_supported": ["offline", "allowlist", "open"],
            "network_modes_enforced": [],
            "auth_exposed_to_cli_domain": True,
            "environment_secrets_exposed_to_cli_domain": True,
            "secret_isolation_from_cli_tools": False,
            "visible_mount_digest": self._visible_digest,
            "unsafe": True,
        }

    def prepare(
        self,
        argv: list[str],
        *,
        executable: Path,
        worktree: Path,
        config_dir: Path,
        config_env_var: str,
        env: dict[str, str],
        policy: SandboxPolicy,
    ) -> SandboxLaunch:
        del executable, config_dir, config_env_var
        self._assert_visible_paths_unchanged()
        audit = {
            **self.identity_payload(),
            "policy_digest": policy.digest,
            "declared_secret_names": sorted(policy.secret_names),
            "network_requested": policy.network,
            "network_enforced": False,
            "network_shared_with_host": True,
            "egress_exfiltration_possible": True,
            "writable_guest_paths": [str(_resolve_worktree(worktree))],
            "read_only_guest_paths": [str(p) for p in self._visible_paths],
        }
        return SandboxLaunch(
            argv=tuple(argv),
            cwd=_resolve_worktree(worktree),
            env=dict(env),
            audit=audit,
        )

    def _assert_visible_paths_unchanged(self) -> None:
        if _visible_content_digest(self._visible_paths) != self._visible_digest:
            raise SandboxError(
                "an explicit development pack changed after sandbox identity was frozen"
            )


class BubblewrapSandbox:
    """Small bubblewrap mount/network namespace for a coding CLI."""

    def __init__(
        self,
        *,
        bwrap_path: str = "bwrap",
        visible_paths: Iterable[Path | str] = (),
        mutable_mounts: Iterable[tuple[Path | str, Path | str]] = (),
        cli_tool_root: Optional[Path | str] = None,
    ) -> None:
        resolved = shutil.which(bwrap_path)
        if resolved is None:
            raise SandboxUnavailable(f"bubblewrap executable not found: {bwrap_path}")
        self._bwrap = Path(resolved).resolve()
        self._visible_paths = tuple(_resolve_existing(p) for p in visible_paths)
        self._visible_digest = _visible_content_digest(self._visible_paths)
        self._mutable_mounts = tuple(
            (_resolve_existing(source), _guest_config_path(destination))
            for source, destination in mutable_mounts
        )
        self._cli_tool_root = (
            _resolve_existing(cli_tool_root) if cli_tool_root is not None else None
        )
        self._version = self._probe()
        self._offline_available, self._offline_error = self._probe_offline()

    def _probe(self) -> str:
        """Exercise the namespaces shared by every production network mode."""

        version = subprocess.run(
            [str(self._bwrap), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if version.returncode != 0:
            raise SandboxUnavailable(
                f"bubblewrap version probe failed: {version.stderr.strip()}"
            )
        argv = [
            str(self._bwrap),
            "--die-with-parent",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--cap-drop",
            "ALL",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
        ]
        argv.extend(_system_mount_args())
        argv.append("/usr/bin/true")
        probe = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout).strip()
            raise SandboxUnavailable(
                "bubblewrap/user/network namespaces are unavailable"
                + (f": {detail}" if detail else "")
            )
        return version.stdout.strip() or version.stderr.strip() or "unknown"

    def _probe_offline(self) -> tuple[bool, str]:
        """Probe the additional network namespace required by offline mode.

        An ``open`` run deliberately shares host networking and must not be
        rejected merely because a nested/container host forbids network
        namespaces.  Offline requests still fail closed in :meth:`prepare`.
        """

        argv = [
            str(self._bwrap),
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
        ]
        argv.extend(_system_mount_args())
        argv.append("/usr/bin/true")
        probe = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        detail = (probe.stderr or probe.stdout).strip()
        return probe.returncode == 0, detail

    def identity_payload(self) -> dict:
        return {
            "backend": "bubblewrap",
            "backend_version": f"{SANDBOX_ADAPTER}:{self._version}",
            "filesystem_isolation": True,
            "pid_isolation": True,
            # Bubblewrap can deny all networking, but it cannot implement a
            # hostname allowlist by itself.  Claim only what is enforced.
            "network_modes_supported": (
                ["offline", "open"] if self._offline_available else ["open"]
            ),
            "network_modes_enforced": (["offline"] if self._offline_available else []),
            # Codex/Claude and their shell/tool calls share this namespace.
            # The copied auth file cannot be hidden from a command launched by
            # the CLI; strong secret separation requires a host-side model
            # proxy and no credential mount.
            "auth_exposed_to_cli_domain": True,
            "environment_secrets_exposed_to_cli_domain": True,
            "secret_isolation_from_cli_tools": False,
            "unsafe": False,
            "visible_mount_count": len(self._visible_paths),
            "visible_mount_digest": self._visible_digest,
            "run_private_mutable_mount_count": len(self._mutable_mounts),
        }

    def prepare(
        self,
        argv: list[str],
        *,
        executable: Path,
        worktree: Path,
        config_dir: Path,
        config_env_var: str,
        env: dict[str, str],
        policy: SandboxPolicy,
    ) -> SandboxLaunch:
        network = policy.network.strip().lower()
        if network not in {"offline", "open", "allowlist"}:
            raise UnsupportedSandboxPolicy(
                f"unknown sandbox network policy: {policy.network!r}"
            )
        if network == "allowlist":
            raise UnsupportedSandboxPolicy(
                "bubblewrap cannot enforce a hostname allowlist; configure a "
                "metered model proxy backend rather than silently granting open egress"
            )
        if network == "offline" and not self._offline_available:
            detail = self._offline_error
            raise SandboxUnavailable(
                "bubblewrap network namespace is unavailable for offline policy"
                + (f": {detail}" if detail else "")
            )

        wt = _resolve_worktree(worktree)
        cfg = _resolve_existing(config_dir)
        cli = _resolve_existing(executable)
        self._assert_visible_paths_unchanged()
        if _is_within(cli, wt):
            raise SandboxError(
                "the configured coding CLI is inside the mutable candidate worktree"
            )

        wrapped = [
            str(self._bwrap),
            "--die-with-parent",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
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
        if network == "offline":
            wrapped.append("--unshare-net")
        wrapped.extend(_system_mount_args())
        wrapped += [
            "--dir",
            "/rsi-arena",
            "--dir",
            str(_BIN_ROOT),
            "--dir",
            _HOME,
            "--dir",
            str(_VISIBLE_ROOT),
            "--dir",
            _WORKSPACE,
            "--bind",
            str(wt),
            _WORKSPACE,
            "--bind",
            str(cfg),
            _CONFIG,
        ]
        mutable_guest_paths: list[str] = []
        for source, destination in self._mutable_mounts:
            if _is_within(source, wt) or source == wt:
                raise SandboxError(
                    "run-private mutable mount overlaps the candidate worktree"
                )
            wrapped += ["--bind", str(source), str(destination)]
            mutable_guest_paths.append(str(destination))

        isolated_git = _prepare_isolated_git(wt, cfg)
        if isolated_git is not None:
            git_dir, pointer = isolated_git
            dotgit = wt / ".git"
            if dotgit.is_file():
                # Hide the linked-worktree pointer, which contains an absolute
                # path into the control repository, behind a pointer to the
                # per-run private metadata.  Both sources live outside the
                # candidate-writable CLI config bind and are mounted read-only.
                wrapped += ["--ro-bind", str(git_dir), _GIT_ROOT]
                wrapped += ["--ro-bind", str(pointer), f"{_WORKSPACE}/.git"]
            elif dotgit.is_dir():
                wrapped += ["--ro-bind", str(git_dir), f"{_WORKSPACE}/.git"]

        guest_cli, tool_mounts = self._tool_mounts(cli)
        for source, destination in tool_mounts:
            wrapped += ["--ro-bind", str(source), destination]

        visible_guest_paths: list[str] = []
        for index, source in enumerate(self._visible_paths):
            safe_name = _safe_component(source.name or "pack")
            destination = str(_VISIBLE_ROOT / f"{index}-{safe_name}")
            wrapped += ["--ro-bind", str(source), destination]
            visible_guest_paths.append(destination)

        child_env = _sanitize_child_env(env)
        child_env["HOME"] = _HOME
        child_env["TMPDIR"] = "/tmp"
        child_env[config_env_var] = _CONFIG
        child_env["PATH"] = "/rsi-arena/bin:/usr/local/bin:/usr/bin:/bin"
        if visible_guest_paths:
            child_env["RSI_ARENA_DEVELOPMENT_PATHS"] = os.pathsep.join(
                visible_guest_paths
            )
        else:
            child_env.pop("RSI_ARENA_DEVELOPMENT_PATHS", None)

        wrapped += ["--chdir", _WORKSPACE, "--", guest_cli, *argv[1:]]
        audit = {
            **self.identity_payload(),
            "policy_digest": policy.digest,
            "declared_secret_names": sorted(policy.secret_names),
            "network_requested": network,
            "network_enforced": network == "offline",
            "network_shared_with_host": network == "open",
            "egress_exfiltration_possible": network == "open",
            "writable_guest_paths": [
                _WORKSPACE,
                _CONFIG,
                "/tmp",
                *mutable_guest_paths,
            ],
            "read_only_guest_paths": visible_guest_paths,
            "system_profile": "minimal-ro-system",
            "isolated_git_metadata": isolated_git is not None,
            "isolated_git_metadata_read_only": isolated_git is not None,
            "git_common_dir_exposed": False,
            # Candidate-owned files can themselves contain arbitrary strings;
            # the mount policy does not claim to redact their contents.
            "host_paths_may_appear_in_candidate_files": True,
        }
        return SandboxLaunch(argv=tuple(wrapped), cwd=wt, env=child_env, audit=audit)

    def _assert_visible_paths_unchanged(self) -> None:
        if _visible_content_digest(self._visible_paths) != self._visible_digest:
            raise SandboxError(
                "an explicit development pack changed after sandbox identity was frozen"
            )

    def _tool_mounts(self, executable: Path) -> tuple[str, list[tuple[Path, str]]]:
        """Map only the configured CLI/package, never its surrounding home.

        Native binaries and simple scripts are mounted as one file.  A Node
        package may import files relative to its entrypoint; when a nearby
        ``package.json`` exists we mount that package root, still excluding
        sibling packages and the rest of the operator's home directory.
        ``cli_tool_root`` is the explicit escape hatch for other layouts.
        """

        system_path = _system_guest_path(executable)
        if system_path is not None:
            return system_path, []

        root = self._cli_tool_root
        if root is not None:
            if not _is_within(executable, root):
                raise SandboxError(
                    f"CLI {executable} is outside configured cli_tool_root {root}"
                )
        elif _looks_like_node_script(executable):
            root = _nearest_package_root(executable)

        mounts: list[tuple[Path, str]] = []
        if root is not None:
            relative = executable.relative_to(root)
            mounts.append((root, str(_TOOL_ROOT)))
            guest_cli = str(_TOOL_ROOT / PurePosixPath(relative.as_posix()))
        else:
            guest_cli = str(_BIN_ROOT / "coding-cli")
            mounts.append((executable, guest_cli))

        interpreter = _external_env_interpreter(executable)
        if interpreter is not None and _system_guest_path(interpreter) is None:
            name = _safe_component(interpreter.name)
            guest_interpreter = str(_BIN_ROOT / name)
            if all(destination != guest_interpreter for _, destination in mounts):
                mounts.append((interpreter, guest_interpreter))
        return guest_cli, mounts


def write_sandbox_audit(
    path: Path,
    launch: SandboxLaunch,
    *,
    injected_secret_names: Iterable[str] = (),
    credential_file_mounted: bool = False,
) -> None:
    """Persist capability truth without host paths or credential values."""

    audit = dict(launch.audit)
    audit["injected_secret_names"] = sorted(str(n) for n in injected_secret_names)
    audit["credential_file_mounted"] = bool(credential_file_mounted)
    Path(path).write_text(
        json.dumps(audit, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _system_mount_args() -> list[str]:
    args: list[str] = []
    for path in ("/usr", "/bin", "/lib", "/lib64", "/sbin"):
        if Path(path).exists():
            args += ["--ro-bind", path, path]

    # Do not expose all of /etc: it may contain service credentials.  These
    # files/directories are enough for TLS, DNS and normal user lookup.
    args += ["--dir", "/etc"]
    for path in (
        "/etc/ssl",
        "/etc/ca-certificates",
        "/etc/pki",
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
        "/etc/passwd",
        "/etc/group",
        "/etc/localtime",
        "/etc/gitconfig",
    ):
        if Path(path).exists():
            args += ["--ro-bind", path, path]
    return args


def _sanitize_child_env(env: dict[str, str]) -> dict[str, str]:
    result = {str(key): str(value) for key, value in env.items()}
    # Loader/shell injection variables are never required for the coding CLI
    # and can turn an allowlisted environment into code execution before the
    # requested binary starts.
    for key in (
        "BASH_ENV",
        "ENV",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PYTHONHOME",
        "PYTHONPATH",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "SSH_AUTH_SOCK",
        "DOCKER_HOST",
        "CONTAINER_HOST",
    ):
        result.pop(key, None)
    return result


def _visible_content_digest(paths: Iterable[Path]) -> str:
    """Content-address explicit read-only packs without recording host paths."""

    roots: list[dict] = []
    for root in paths:
        if root.is_symlink():
            roots.append({"kind": "symlink", "target": os.readlink(root)})
            continue
        if root.is_file():
            roots.append(
                {
                    "kind": "file",
                    "sha256": hashlib.sha256(root.read_bytes()).hexdigest(),
                    "mode": root.stat().st_mode & 0o777,
                }
            )
            continue
        entries: list[dict] = []
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirnames.sort()
            filenames.sort()
            for name in dirnames:
                item = current_path / name
                relative = item.relative_to(root).as_posix()
                if item.is_symlink():
                    entries.append(
                        {
                            "path": relative,
                            "kind": "symlink",
                            "target": os.readlink(item),
                        }
                    )
            for name in filenames:
                item = current_path / name
                relative = item.relative_to(root).as_posix()
                if item.is_symlink():
                    entries.append(
                        {
                            "path": relative,
                            "kind": "symlink",
                            "target": os.readlink(item),
                        }
                    )
                else:
                    entries.append(
                        {
                            "path": relative,
                            "kind": "file",
                            "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
                            "mode": item.stat().st_mode & 0o777,
                        }
                    )
        roots.append({"kind": "directory", "entries": entries})
    return canonical_digest(roots)


def _prepare_isolated_git(
    worktree: Path, config_dir: Path
) -> Optional[tuple[Path, Path]]:
    """Give coding tools useful git status/diff without the control repo.

    A normal ``git worktree`` stores an absolute pointer to the shared common
    directory.  Mounting that directory would expose refs and objects from
    other candidates.  Instead, snapshot the current candidate tree into a
    tiny per-run Git directory and over-mount only the root ``.git`` marker in
    the sandbox.  The authoritative diff is still captured by the trusted
    orchestrator after the CLI exits.

    The CLI config directory is deliberately candidate-writable, so it must
    never contain a future mount source.  Otherwise one segment can replace a
    source with a symlink and make the trusted parent follow/mount an arbitrary
    host path in the next segment.  Persistent Git state therefore lives in a
    host-only sibling directory and is always mounted read-only.
    """

    dotgit = worktree / ".git"
    try:
        dotgit_stat = os.lstat(dotgit)
    except FileNotFoundError:
        return None
    if not (stat.S_ISREG(dotgit_stat.st_mode) or stat.S_ISDIR(dotgit_stat.st_mode)):
        raise SandboxError("candidate .git marker must be a regular file or directory")
    if dotgit_stat.st_uid != os.geteuid():
        raise SandboxError("candidate .git marker is not owned by the sandbox user")

    git = shutil.which("git")
    if git is None:
        raise SandboxUnavailable("git is required to isolate linked-worktree metadata")

    # Resolve the state name from trusted host values only.  In particular,
    # never inspect config_dir/isolated-git* left by a previous candidate.
    parent = config_dir.parent
    _assert_trusted_state_directory(parent, label="CLI transcript directory")
    if _is_within(parent, worktree) or _is_within(config_dir, worktree):
        raise SandboxError("trusted sandbox state may not live inside the worktree")
    worktree_key = hashlib.sha256(os.fsencode(str(worktree))).hexdigest()[:16]
    state_root = parent / (
        f".rsi-arena-sandbox-{_safe_component(config_dir.name)}-{worktree_key}"
    )

    try:
        os.lstat(state_root)
    except FileNotFoundError:
        _initialize_isolated_git_state(
            git=git,
            worktree=worktree,
            state_root=state_root,
            parent=parent,
            dotgit_kind="file" if stat.S_ISREG(dotgit_stat.st_mode) else "directory",
        )

    _assert_trusted_state_directory(state_root, label="trusted sandbox state")
    manifest = state_root / "manifest.json"
    git_dir = state_root / "git"
    pointer = state_root / "git-pointer"
    _assert_trusted_state_directory(git_dir, label="isolated Git metadata")
    _assert_owned_regular_file(pointer, label="isolated Git pointer")
    _assert_owned_regular_file(manifest, label="isolated Git manifest")

    expected_manifest = _isolated_git_manifest(
        worktree,
        "file" if stat.S_ISREG(dotgit_stat.st_mode) else "directory",
    )
    actual_manifest = _read_nofollow_text(manifest, limit=16_384)
    try:
        decoded_manifest = json.loads(actual_manifest)
    except (json.JSONDecodeError, ValueError) as exc:
        raise SandboxError("isolated Git manifest is invalid") from exc
    if decoded_manifest != expected_manifest:
        raise SandboxError("isolated Git state does not match this worktree")
    if _read_nofollow_text(pointer, limit=1_024) != f"gitdir: {_GIT_ROOT}\n":
        raise SandboxError("isolated Git pointer has unexpected content")

    return git_dir, pointer


def _initialize_isolated_git_state(
    *,
    git: str,
    worktree: Path,
    state_root: Path,
    parent: Path,
    dotgit_kind: str,
) -> None:
    """Build trusted Git state off to the side, then publish it atomically."""

    staging = Path(
        tempfile.mkdtemp(prefix=".rsi-arena-sandbox-staging-", dir=str(parent))
    )
    staging.chmod(0o700)
    git_dir = staging / "git"
    pointer = staging / "git-pointer"
    manifest = staging / "manifest.json"
    try:
        git_dir.mkdir(mode=0o700)
        commands = [
            [git, f"--git-dir={git_dir}", "init", "--quiet"],
            [
                git,
                f"--git-dir={git_dir}",
                "config",
                "core.bare",
                "false",
            ],
            [
                git,
                f"--git-dir={git_dir}",
                "config",
                "core.worktree",
                _WORKSPACE,
            ],
            [
                git,
                f"--git-dir={git_dir}",
                f"--work-tree={worktree}",
                "add",
                "-A",
            ],
            [
                git,
                "-c",
                "user.name=rsi-arena-sandbox",
                "-c",
                "user.email=rsi-arena-sandbox@localhost",
                f"--git-dir={git_dir}",
                f"--work-tree={worktree}",
                "commit",
                "--quiet",
                "--allow-empty",
                "-m",
                "sandbox baseline",
            ],
        ]
        git_env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            # Git must not consume candidate-controlled config while building
            # the trusted baseline.
            "HOME": str(staging),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
        for command in commands:
            completed = subprocess.run(
                command,
                env=git_env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise SandboxError(
                    "failed to create isolated Git metadata"
                    + (f": {detail}" if detail else "")
                )

        _write_new_nofollow(pointer, f"gitdir: {_GIT_ROOT}\n")
        _write_new_nofollow(
            manifest,
            json.dumps(
                _isolated_git_manifest(worktree, dotgit_kind),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        try:
            os.rename(staging, state_root)
        except FileExistsError:
            # A concurrent trusted launcher won the race.  Its state is
            # validated by the caller before use; never merge directories.
            shutil.rmtree(staging)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _isolated_git_manifest(worktree: Path, dotgit_kind: str) -> dict:
    worktree_stat = os.stat(worktree, follow_symlinks=False)
    return {
        "worktree_dev": worktree_stat.st_dev,
        "worktree_ino": worktree_stat.st_ino,
        "dotgit_kind": dotgit_kind,
    }


def _assert_trusted_state_directory(path: Path, *, label: str) -> None:
    try:
        entry = os.lstat(path)
    except FileNotFoundError as exc:
        raise SandboxError(f"{label} does not exist") from exc
    if not stat.S_ISDIR(entry.st_mode):
        raise SandboxError(f"{label} must be a non-symlink directory")
    if entry.st_uid != os.geteuid():
        raise SandboxError(f"{label} is not owned by the sandbox user")
    if entry.st_mode & 0o022:
        raise SandboxError(f"{label} must not be group/world writable")


def _assert_owned_regular_file(path: Path, *, label: str) -> None:
    try:
        entry = os.lstat(path)
    except FileNotFoundError as exc:
        raise SandboxError(f"{label} does not exist") from exc
    if not stat.S_ISREG(entry.st_mode):
        raise SandboxError(f"{label} must be a non-symlink regular file")
    if entry.st_uid != os.geteuid():
        raise SandboxError(f"{label} is not owned by the sandbox user")
    if entry.st_mode & 0o022:
        raise SandboxError(f"{label} must not be group/world writable")


def _write_new_nofollow(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        data = content.encode("utf-8")
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_nofollow_text(path: Path, *, limit: int) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise SandboxError("trusted sandbox state file is invalid")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise SandboxError("trusted sandbox state changed while being read")
        data = b"".join(chunks)
        if len(data) > limit:
            raise SandboxError("trusted sandbox state file exceeds its size limit")
        return data.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise SandboxError("trusted sandbox state is not UTF-8") from exc
    finally:
        os.close(descriptor)


def _resolve_existing(path: Path | str) -> Path:
    resolved = Path(path).expanduser().resolve(strict=True)
    return resolved


def _guest_config_path(path: Path | str) -> PurePosixPath:
    guest = PurePosixPath(str(path))
    config = PurePosixPath(_CONFIG)
    try:
        relative = guest.relative_to(config)
    except ValueError as exc:
        raise SandboxError(
            "run-private mutable mount must stay under the CLI config root"
        ) from exc
    if not relative.parts:
        raise SandboxError("run-private mutable mount cannot replace the config root")
    if any(part == ".." for part in relative.parts):
        raise SandboxError("run-private mutable mount guest path is invalid")
    return guest


def _resolve_worktree(path: Path | str) -> Path:
    resolved = _resolve_existing(path)
    if not resolved.is_dir():
        raise SandboxError(f"candidate worktree is not a directory: {resolved}")
    if resolved == Path("/"):
        raise SandboxError("refusing to expose the filesystem root as a worktree")
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _system_guest_path(path: Path) -> Optional[str]:
    for root in (
        Path("/usr"),
        Path("/bin"),
        Path("/lib"),
        Path("/lib64"),
        Path("/sbin"),
    ):
        if _is_within(path, root):
            return str(path)
    return None


def _safe_component(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    return safe or "item"


def _looks_like_node_script(path: Path) -> bool:
    first = _read_shebang(path)
    return first.startswith("#!") and ("node" in first or "deno" in first)


def _nearest_package_root(path: Path) -> Optional[Path]:
    parent = path.parent
    for candidate in (parent, *list(parent.parents)[:5]):
        if (candidate / "package.json").is_file():
            return candidate
    return None


def _external_env_interpreter(path: Path) -> Optional[Path]:
    """Resolve ``#!/usr/bin/env NAME`` when NAME itself lives outside /usr."""

    first = _read_shebang(path).strip()
    prefix = "#!/usr/bin/env "
    if not first.startswith(prefix):
        return None
    words = first[len(prefix) :].split()
    if not words:
        return None
    name = words[-1] if words[0] == "-S" else words[0]
    resolved = shutil.which(name)
    return Path(resolved).resolve() if resolved else None


def _read_shebang(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            return stream.readline(512).decode("utf-8", errors="ignore")
    except OSError:
        return ""
