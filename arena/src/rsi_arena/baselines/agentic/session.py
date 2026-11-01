"""Internal method-session lifecycle seam for the Arena agentic runner.

The Arena runner owns campaign admission, wall/cost policy, and closeout.  A
method session owns only its native session/process intervals, prompt
continuation, raw-output usage normalization, and native cleanup.  Codex and
Claude remain deliberately separate adapters: their flags, homes, auth files,
session identities, and usage formats are not a generic provider taxonomy.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol

from rsi_arena.baselines._sandbox import (
    HarnessSandbox,
    SandboxLaunch,
    SandboxPolicy,
    atomic_private_copy,
    create_harness_sandbox,
    validate_sandbox_environment,
    write_sandbox_audit,
)
from rsi_arena.common import UsageLedger

RESUME_PROMPT = "Continue improving the implementation for one more iteration."
ARENA_RESPONSE_COMPLETION_GRACE_S = 60.0

_BASE_ENV = ("PATH", "HOME", "LANG", "TERM", "USER", "SHELL", "TMPDIR")
_GUEST_CLI_HOME = "/rsi-arena/config"
_GUEST_AGENTIC_TOOLS = "/rsi-arena/visible/0-agentic-tools"


@dataclass(frozen=True)
class CliMethodSessionConfig:
    """Only the resolved values needed by the existing CLI adapters."""

    default_model: str
    cli_path: str
    worktree: Path
    run_dir: Path
    trusted_dir: Path
    model: Optional[str]
    reasoning_effort: Optional[str]
    provider: str
    provider_base_url: Optional[str]
    provider_env_key: Optional[str]
    provider_name: Optional[str]
    input_cost_per_million: float
    output_cost_per_million: float
    max_resumes: int
    min_segment_s: float
    env_passthrough: tuple[str, ...]
    env_overrides: tuple[tuple[str, str], ...]
    extra_cli_args: tuple[str, ...]
    auth_file: Optional[Path]
    sandbox_backend: str
    bwrap_path: str
    sandbox_network: str
    allow_unsafe_host: bool
    run_kind: str
    session_id: Optional[str]
    resume_segment_ordinal: Optional[int]


@dataclass(frozen=True)
class MethodIntervalControl:
    """Arena-owned policy projected into one method-native interval."""

    wall_deadline: float
    attempt_cap_exhausted: Callable[[], bool]
    completion_grace_s: float = ARENA_RESPONSE_COMPLETION_GRACE_S


@dataclass(frozen=True)
class _MethodClientCapabilities:
    """Narrow public-client setup capabilities projected by the runner."""

    install: Callable[..., Path]
    client_environment: Callable[[], Mapping[str, str]]
    sandbox_mounts: Callable[[], tuple[tuple[Path, Path], ...]]
    bind_native_context: Optional[Callable[..., None]] = None


@dataclass(frozen=True)
class MethodIntervalResult:
    """Normalized facts from one method-native prompt interval."""

    command_summary: Optional[str]
    exit_code: Optional[int]
    elapsed_s: float
    cost_usd: Optional[float]
    continuation: bool
    timed_out: bool
    session_id: Optional[str] = None
    interval_id: Optional[str] = None
    raw_output_path: Optional[Path] = None

    def segment_record(self) -> dict[str, object]:
        return {
            "cmd": self.command_summary,
            "exit_code": self.exit_code,
            "elapsed_s": self.elapsed_s,
            "cost_usd": self.cost_usd,
            "resume": self.continuation,
            "session_id": self.session_id,
            "segment_id": self.interval_id,
        }


class MethodSession(Protocol):
    """Minimal lifecycle owned by one benchmark-neutral native method."""

    run_id: Optional[str]

    def bind(self, *, run_id: str) -> None: ...

    def prepare(self, capabilities: _MethodClientCapabilities) -> None: ...

    def reset_usage(self) -> None: ...

    def persist_identity(self, trusted_dir: Path) -> None: ...

    def run_prompt_interval(
        self,
        *,
        initial_prompt: str,
        raw_output_dir: Path,
        control: MethodIntervalControl,
    ) -> MethodIntervalResult: ...

    def interval_failure_reason(
        self, interval: MethodIntervalResult
    ) -> Optional[str]: ...

    def continuation_stop_reason(self, resume_count: int) -> Optional[str]: ...

    def current_search_cost_status(self) -> str: ...

    def search_cost_summary(self, spent_usd: float) -> tuple[Optional[float], str]: ...

    def finalize_session_usage(
        self, *, run_started_at: object, run_ended_at: object
    ) -> None: ...

    def close(self) -> None: ...


class MethodSessionAdapter:
    """Convenience defaults for injected SDK/fake adapters.

    Non-CLI adapters need only implement ``run_prompt_interval`` and any
    native preparation/cleanup they actually require.
    """

    def __init__(
        self,
        *,
        run_id: Optional[str] = None,
    ) -> None:
        self.run_id = run_id

    def bind(self, *, run_id: str) -> None:
        if self.run_id is not None and self.run_id != run_id:
            raise ValueError("method session run identity disagrees with runner")
        self.run_id = run_id

    def prepare(self, capabilities: _MethodClientCapabilities) -> None:
        del capabilities

    def reset_usage(self) -> None:
        return None

    def persist_identity(self, trusted_dir: Path) -> None:
        del trusted_dir

    def run_prompt_interval(
        self,
        *,
        initial_prompt: str,
        raw_output_dir: Path,
        control: MethodIntervalControl,
    ) -> MethodIntervalResult:
        raise NotImplementedError

    def interval_failure_reason(self, interval: MethodIntervalResult) -> Optional[str]:
        del interval
        return None

    def continuation_stop_reason(self, resume_count: int) -> Optional[str]:
        del resume_count
        return None

    def current_search_cost_status(self) -> str:
        return "missing"

    def search_cost_summary(self, spent_usd: float) -> tuple[Optional[float], str]:
        del spent_usd
        return None, self.current_search_cost_status()

    def finalize_session_usage(
        self, *, run_started_at: object, run_ended_at: object
    ) -> None:
        del run_started_at, run_ended_at

    def close(self) -> None:
        return None


@dataclass(frozen=True)
class PreparedCliSandbox:
    env: dict[str, str]
    secret_names: tuple[str, ...]
    sandbox: HarnessSandbox
    policy: SandboxPolicy
    executable: Path
    cli_home: Path
    config_env_var: str
    tools_bin: str
    credential_file_mounted: bool


class _CliMethodSession(MethodSessionAdapter):
    """Shared physical CLI process mechanics, not shared method semantics."""

    config_env_var: str
    credential_name: str

    def __init__(
        self,
        config: CliMethodSessionConfig,
        *,
        ledger: Optional[UsageLedger],
    ) -> None:
        super().__init__()
        self.config = config
        self.ledger = ledger
        self._runtime: Optional[PreparedCliSandbox] = None
        self._client_capabilities: Optional[_MethodClientCapabilities] = None
        self._resume_existing_session = config.resume_segment_ordinal is not None
        self._interval_index = config.resume_segment_ordinal or 0
        self._active_process: Optional[subprocess.Popen] = None
        self._saw_search_usage = False
        self._search_cost_unknown = False
        self._observed_session_usage_lower_bound = False

    def prepare(self, capabilities: _MethodClientCapabilities) -> None:
        self._client_capabilities = capabilities
        self._runtime = _prepare_cli_sandbox(
            self.config,
            session=self,
            capabilities=capabilities,
        )

    def prepare_version_launch(self) -> SandboxLaunch:
        self._runtime = _prepare_cli_sandbox(
            self.config,
            session=self,
            capabilities=None,
        )
        return self._prepare_launch([self.config.cli_path, "--version"])

    def reset_usage(self) -> None:
        self._saw_search_usage = False
        self._search_cost_unknown = False
        self._observed_session_usage_lower_bound = False

    def install_home(
        self,
        cli_home: Path,
        *,
        hook_path: Path,
        hook_command: str,
        secrets: Mapping[str, str],
    ) -> None:
        raise NotImplementedError

    def copy_auth_sidecars(self, source: Path, cli_home: Path) -> None:
        del source, cli_home

    def next_command(
        self, initial_prompt: str, *, continuation: Optional[bool] = None
    ) -> tuple[list[str], bool]:
        raise NotImplementedError

    def bind_child_context(self, child_env: dict[str, str]) -> None:
        del child_env

    def normalize_raw_output(self, raw_output_path: Path) -> float:
        raise NotImplementedError

    def native_session_id(self) -> Optional[str]:
        return None

    def native_interval_id(self) -> Optional[str]:
        return None

    def _prepare_launch(self, command: list[str]) -> SandboxLaunch:
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("method session is not prepared")
        launch = runtime.sandbox.prepare(
            command,
            executable=runtime.executable,
            worktree=self.config.worktree,
            config_dir=runtime.cli_home,
            config_env_var=runtime.config_env_var,
            env=runtime.env,
            policy=runtime.policy,
        )
        child_env = dict(launch.env)
        child_env.pop("RSI_ARENA_DEVELOPMENT_PATHS", None)
        child_env["XDG_CACHE_HOME"] = str(
            Path(child_env[runtime.config_env_var]) / "cache"
        )
        self.bind_child_context(child_env)
        child_env["PATH"] = (
            f"{runtime.tools_bin}:"
            f"{child_env.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"
        )
        return SandboxLaunch(
            argv=launch.argv,
            cwd=launch.cwd,
            env=child_env,
            audit=launch.audit,
        )

    def run_prompt_interval(
        self,
        *,
        initial_prompt: str,
        raw_output_dir: Path,
        control: MethodIntervalControl,
    ) -> MethodIntervalResult:
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("method session is not prepared")
        command, continuation = self.next_command(initial_prompt)
        launch = self._prepare_launch(command)
        if self._interval_index == 0:
            write_sandbox_audit(
                self.config.trusted_dir / "sandbox.json",
                launch,
                injected_secret_names=runtime.secret_names,
                credential_file_mounted=runtime.credential_file_mounted,
            )
        raw_output_dir.mkdir(parents=True, exist_ok=True)
        raw_output_path = raw_output_dir / f"segment-{self._interval_index:03d}.jsonl"
        started = time.monotonic()
        timed_out = False
        process: Optional[subprocess.Popen] = None
        try:
            with raw_output_path.open("wb") as raw_output:
                process = subprocess.Popen(
                    list(launch.argv),
                    cwd=str(launch.cwd),
                    env=launch.env,
                    stdout=raw_output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                self._active_process = process
                while process.poll() is None:
                    if control.attempt_cap_exhausted():
                        _await_completion(
                            process,
                            wall_deadline=control.wall_deadline,
                            completion_grace_s=control.completion_grace_s,
                        )
                        break
                    remaining = control.wall_deadline - time.monotonic()
                    if remaining <= 0:
                        _kill_tree(process)
                        process.wait()
                        timed_out = True
                        break
                    try:
                        process.wait(timeout=min(0.05, remaining))
                    except subprocess.TimeoutExpired:
                        continue
                raw_output.flush()
                os.fsync(raw_output.fileno())
        finally:
            self._active_process = None
        elapsed = time.monotonic() - started
        cost_usd = self.normalize_raw_output(raw_output_path)
        result = MethodIntervalResult(
            command_summary=shlex.join(command[:3]),
            exit_code=process.returncode if process is not None else None,
            elapsed_s=elapsed,
            cost_usd=cost_usd,
            continuation=continuation,
            timed_out=timed_out,
            session_id=self.native_session_id(),
            interval_id=self.native_interval_id(),
            raw_output_path=raw_output_path,
        )
        self._interval_index += 1
        return result

    def search_cost_summary(self, spent_usd: float) -> tuple[Optional[float], str]:
        status = self.current_search_cost_status()
        return (
            (
                spent_usd
                if self._saw_search_usage and not self._search_cost_unknown
                else None
            ),
            status,
        )

    def close(self) -> None:
        process = self._active_process
        if process is not None and process.poll() is None:
            _kill_tree(process)
            process.wait()
        self._active_process = None


def _prepare_cli_sandbox(
    config: CliMethodSessionConfig,
    *,
    session: _CliMethodSession,
    capabilities: Optional[_MethodClientCapabilities],
) -> PreparedCliSandbox:
    if config.provider not in {"native", "openrouter", "openai_compatible"}:
        raise ValueError(f"unknown agentic provider {config.provider!r}")
    if config.provider != "native" and config.auth_file is not None:
        raise ValueError(
            "custom-provider agentic runs must not also mount native CLI auth"
        )
    worktree = config.worktree.expanduser().resolve(strict=True)
    run_dir = config.run_dir.expanduser().resolve(strict=True)
    trusted_dir = config.trusted_dir.expanduser().resolve(strict=True)
    if run_dir == worktree or _is_within(run_dir, worktree):
        raise ValueError("agentic CLI run_dir must be outside the candidate worktree")
    if trusted_dir == worktree or _is_within(trusted_dir, worktree):
        raise ValueError(
            "agentic CLI trusted_dir must be outside the candidate worktree"
        )

    cli_home = (
        _preserve_private_directory(run_dir / "cli-home")
        if config.resume_segment_ordinal is not None
        else _recreate_private_directory(run_dir / "cli-home")
    )
    private_home = _recreate_private_directory(run_dir / "home")
    private_tmp = _recreate_private_directory(run_dir / "tmp")
    private_cache = _recreate_private_directory(cli_home / "cache")
    tools_dir = _recreate_private_directory(run_dir / "agentic-tools")
    host_hook = tools_dir / "stop-hook.sh"
    is_host = config.sandbox_backend.strip().lower() == "host"
    hook_command = str(host_hook) if is_host else f"{_GUEST_AGENTIC_TOOLS}/stop-hook.sh"
    secrets = {
        name: os.environ[name] for name in config.env_passthrough if name in os.environ
    }
    for name, value in config.env_overrides:
        if name in secrets and secrets[name] != value:
            raise ValueError(f"conflicting agentic environment value: {name}")
        secrets[name] = value
    session.install_home(
        cli_home,
        hook_path=host_hook,
        hook_command=hook_command,
        secrets=secrets,
    )

    credential_file_mounted = config.auth_file is not None
    if config.auth_file is not None:
        source = config.auth_file.expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"agentic CLI auth file not found: {source}")
        atomic_private_copy(source, cli_home / session.credential_name)
        session.copy_auth_sidecars(source, cli_home)

    executable = _resolve_executable(config.cli_path)
    env = {name: os.environ[name] for name in _BASE_ENV if name in os.environ}
    policy = SandboxPolicy(
        network=config.sandbox_network,
        env_allowlist=tuple(sorted(env)),
        secret_names=tuple(sorted(secrets)),
    )
    validate_sandbox_environment(policy, env, secrets)
    env.update(secrets)
    env["HOME"] = str(private_home)
    env["TMPDIR"] = str(private_tmp)
    env["XDG_CACHE_HOME"] = str(private_cache)
    env[session.config_env_var] = str(cli_home)

    if capabilities is not None:
        arena_ipc_dir = cli_home / "arena-ipc"
        guest_arena_ipc = (
            arena_ipc_dir if is_host else Path(_GUEST_CLI_HOME) / "arena-ipc"
        )
        capabilities.install(
            tools_dir,
            arena_ipc_dir,
            guest_ipc_dir=guest_arena_ipc,
        )
        client_environment = dict(capabilities.client_environment())
        if set(client_environment) & set(secrets):
            raise ValueError("Arena client environment overlaps secrets")
        env.update(client_environment)
        public_env = {name: value for name, value in env.items() if name not in secrets}
        policy = SandboxPolicy(
            network=config.sandbox_network,
            env_allowlist=tuple(sorted(public_env)),
            secret_names=tuple(sorted(secrets)),
        )
        validate_sandbox_environment(policy, public_env, secrets)
    sandbox = create_harness_sandbox(
        config.sandbox_backend,
        bwrap_path=config.bwrap_path,
        visible_paths=(tools_dir,),
        mutable_mounts=(
            capabilities.sandbox_mounts() if capabilities is not None else ()
        ),
        allow_unsafe_host=config.allow_unsafe_host,
    )
    return PreparedCliSandbox(
        env=env,
        secret_names=tuple(sorted(secrets)),
        sandbox=sandbox,
        policy=policy,
        executable=executable,
        cli_home=cli_home,
        config_env_var=session.config_env_var,
        tools_bin=str(tools_dir) if is_host else _GUEST_AGENTIC_TOOLS,
        credential_file_mounted=credential_file_mounted,
    )


def _kill_tree(process: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


def _await_completion(
    process: subprocess.Popen,
    *,
    wall_deadline: float,
    completion_grace_s: float,
) -> None:
    timeout = min(
        completion_grace_s,
        max(0.0, wall_deadline - time.monotonic()),
    )
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        process.wait()


def _resolve_executable(value: str) -> Path:
    expanded = Path(value).expanduser()
    if expanded.is_absolute() or expanded.parent != Path("."):
        return expanded.resolve(strict=True)
    resolved = shutil.which(value)
    if resolved is None:
        raise FileNotFoundError(f"agentic CLI not found: {value}")
    return Path(resolved).resolve(strict=True)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _recreate_private_directory(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            shutil.rmtree(path)
        else:
            path.unlink()
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _preserve_private_directory(path: Path) -> Path:
    """Reuse one run-private CLI home without widening shared resume policy."""

    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700)
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("resumed agentic CLI home is not a directory")
    path.chmod(0o700)
    return path


__all__ = [
    "ARENA_RESPONSE_COMPLETION_GRACE_S",
    "CliMethodSessionConfig",
    "MethodIntervalControl",
    "MethodIntervalResult",
    "MethodSession",
    "MethodSessionAdapter",
    "PreparedCliSandbox",
    "RESUME_PROMPT",
]
