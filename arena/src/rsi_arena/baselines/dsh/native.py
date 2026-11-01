"""Pinned DeepSeek Harness SDK session adapter for Arena agentic runs.

The production JSON-RPC transport belongs to the official rc6 SDK.  This
module only projects one SDK ``Session`` onto the existing ``MethodSession``
seam, prepares run-private sandbox state, and records observable notifications
and usage.  Arena admission, controller, recorder, and closeout remain outside
the adapter.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Optional

from rsi_arena.baselines._sandbox import (
    HarnessSandbox,
    SandboxLaunch,
    SandboxPolicy,
    atomic_private_copy,
    create_harness_sandbox,
    ensure_private_config_directory,
    validate_sandbox_environment,
    write_sandbox_audit,
)
from rsi_arena.baselines.agentic.session import (
    RESUME_PROMPT,
    MethodIntervalControl,
    MethodIntervalResult,
    MethodSessionAdapter,
    _MethodClientCapabilities,
)
from rsi_arena.common import UsageLedger
from rsi_arena.run_layout import (
    AppendOnlyJsonl,
    atomic_write_bytes,
    atomic_write_json,
    file_digest,
)

if TYPE_CHECKING:
    from deepseek_harness import DeepSeekHarness, Notification, Session

DSH_SDK_VERSION = "0.1.0rc6"
DSH_RUNTIME_VERSION = "0.1.0rc6"
DSH_SDK_WHEEL_SHA256 = (
    "8a05421be4298196cf94383e0a3164b020f5f5977a8d30019cc5add64cb208eb"
)
DSH_RUNTIME_LINUX_X86_64_WHEEL_SHA256 = (
    "d7261d3bdadfa8d10ab03fd06c6bbc66a182ae27d39892a0eb7c2ce9d63a5448"
)
DSH_OFFICIAL_PROVIDER = "deepseek-official"
DSH_API_BASE_URL_ENV = "DEEPSEEK_BASE_URL"
DSH_CREDENTIAL_ENV = "DEEPSEEK_API_KEY"
DSH_MODEL_ENV = "DEEPSEEK_MODEL"
DSH_CORDIS_PATH = Path(__file__).with_name("cordis.yml")

_BASE_ENV = ("PATH", "LANG", "TERM", "USER", "SHELL")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_GUEST_DSH_HOME = Path("/rsi-arena/config")
_GUEST_TOOLS = "/rsi-arena/visible/0-agentic-tools"
_MAX_TIMER_DELAY_MS = 2_147_483_647
_BASH_CONFIG_ANCHOR = (
    "- id: bash\n"
    "  name: '@deepseek-ai/dsh-bash-local'\n"
    "  config:\n"
    "    cwd: !!js process.env.DSH_CWD ?? process.cwd()\n"
)
_DEEPSEEK_CONFIG_ANCHOR = (
    "- id: llm-deepseek\n  name: '@deepseek-ai/dsh-llm-deepseek'\n"
)


@dataclass(frozen=True)
class DshSdkSessionAuthority:
    """Trusted, benchmark-neutral authority for one official SDK session."""

    provider: str
    model: str
    base_url: str
    api_base_url_env: str
    credential_env: str
    max_tokens: Optional[int]
    bash_command_timeout_ms: int
    reasoning_effort: Optional[str] = None


@dataclass(frozen=True)
class DshSdkSessionConfig:
    """Resolved, benchmark-neutral values for one pinned DSH session."""

    worktree: Path
    run_dir: Path
    trusted_dir: Path
    cordis_path: Path
    provider: str
    model: str
    max_tokens: Optional[int]
    bash_command_timeout_ms: int
    reasoning_effort: Optional[str] = None
    credential_env: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    sandbox_backend: str = "bwrap"
    bwrap_path: str = "bwrap"
    sandbox_network: str = "open"
    allow_unsafe_host: bool = False
    session_id: Optional[str] = None
    request_timeout_seconds: Optional[float] = None
    shutdown_timeout_seconds: float = 0.25
    # Tests may point the official SDK at a fake child. Production leaves this
    # unset and runs the pinned bundled executable through HarnessSandbox.
    launch_args_override: Optional[tuple[str, ...]] = None

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("DSH provider and model must come from trusted config")
        if self.max_tokens is not None and (
            isinstance(self.max_tokens, bool) or self.max_tokens <= 0
        ):
            raise ValueError("DSH max_tokens must be positive when configured")
        if self.reasoning_effort is not None and (
            not isinstance(self.reasoning_effort, str)
            or not self.reasoning_effort.strip()
        ):
            raise ValueError("DSH reasoning_effort must be non-empty when configured")
        if (
            isinstance(self.bash_command_timeout_ms, bool)
            or not isinstance(self.bash_command_timeout_ms, int)
            or not 0 < self.bash_command_timeout_ms <= _MAX_TIMER_DELAY_MS
        ):
            raise ValueError(
                "DSH Bash command timeout must be a positive timer-safe integer"
            )
        if self.shutdown_timeout_seconds <= 0:
            raise ValueError("DSH shutdown timeout must be positive")
        names = [*self.credential_env, *(name for name, _ in self.environment)]
        invalid = [name for name in names if _ENVIRONMENT_NAME.fullmatch(name) is None]
        if invalid:
            raise ValueError(f"invalid DSH environment name: {invalid[0]!r}")
        if len(names) != len(set(names)):
            raise ValueError("DSH environment names must be unique")
        if self.launch_args_override is not None and not self.launch_args_override:
            raise ValueError("DSH launch_args_override cannot be empty")


def official_dsh_identity() -> dict[str, object]:
    """Verify and return the exact installed SDK/runtime identity."""

    from deepseek_harness_runtime import bundled_runtime_path

    sdk = importlib.metadata.distribution("deepseek-harness-sdk")
    runtime = importlib.metadata.distribution("deepseek-harness-runtime-bin")
    if sdk.version != DSH_SDK_VERSION:
        raise RuntimeError(
            f"deepseek-harness-sdk must be {DSH_SDK_VERSION}, got {sdk.version}"
        )
    if runtime.version != DSH_RUNTIME_VERSION:
        raise RuntimeError(
            "deepseek-harness-runtime-bin must be "
            f"{DSH_RUNTIME_VERSION}, got {runtime.version}"
        )
    required = "deepseek-harness-runtime-bin==0.1.0rc6"
    if required not in (sdk.requires or []):
        raise RuntimeError("the pinned SDK does not exactly bind its rc6 runtime")
    runtime_file = bundled_runtime_path().resolve(strict=True)
    return {
        "sdk_distribution": "deepseek-harness-sdk",
        "sdk_version": sdk.version,
        "sdk_wheel_sha256": DSH_SDK_WHEEL_SHA256,
        "runtime_distribution": "deepseek-harness-runtime-bin",
        "runtime_version": runtime.version,
        "runtime_wheel_sha256_linux_x86_64": (DSH_RUNTIME_LINUX_X86_64_WHEEL_SHA256),
        "runtime_file_sha256": file_digest(runtime_file),
    }


def resolve_dsh_sdk_session_authority(
    *,
    provider: str,
    model: str,
    api_base_url_env: str,
    credential_env: str,
    base_url: Optional[str],
    max_tokens: object,
    wall_seconds: object,
    reasoning_effort: object = None,
) -> DshSdkSessionAuthority:
    """Resolve trusted run config before benchmark execution is composed."""

    if provider not in {DSH_OFFICIAL_PROVIDER, "openrouter"}:
        raise ValueError("DSH SDK authority requires a supported provider route")
    resolved_base_url = os.environ.get(api_base_url_env, "").strip().rstrip("/")
    if base_url != resolved_base_url:
        raise ValueError("DSH base URL authority changed after resolution")
    milliseconds = float(wall_seconds) * 1000
    if not milliseconds.is_integer():
        raise ValueError("DSH Bash timeout must resolve to whole milliseconds")
    return DshSdkSessionAuthority(
        provider=provider,
        model=model,
        base_url=str(base_url),
        api_base_url_env=api_base_url_env,
        credential_env=credential_env,
        max_tokens=int(max_tokens) if max_tokens is not None else None,
        reasoning_effort=(
            str(reasoning_effort) if reasoning_effort is not None else None
        ),
        bash_command_timeout_ms=int(milliseconds),
    )


def project_dsh_sdk_environment(
    authority: DshSdkSessionAuthority,
) -> dict[str, str]:
    """Project the resolved credential alias and model to official SDK names."""

    credential = os.environ.get(authority.credential_env, "")
    if not credential:
        raise ValueError("DSH credential environment is missing")
    return {
        DSH_CREDENTIAL_ENV: credential,
        DSH_MODEL_ENV: authority.model,
    }


class DshSdkSession(MethodSessionAdapter):
    """One official SDK harness, child process, and session ID per Arena run."""

    def __init__(
        self,
        config: DshSdkSessionConfig,
        *,
        ledger: Optional[UsageLedger],
    ) -> None:
        super().__init__()
        self.config = config
        self.ledger = ledger
        self.session_id = config.session_id
        self._official_identity = official_dsh_identity()
        self._cordis_template_digest = file_digest(
            config.cordis_path.resolve(strict=True)
        )
        self._cordis_digest: Optional[str] = None
        self._sandbox: Optional[HarnessSandbox] = None
        self._sandbox_launch: Optional[SandboxLaunch] = None
        self._harness: Optional[DeepSeekHarness] = None
        self._session: Optional[Session] = None
        self._identity_path: Optional[Path] = None
        self._interval_index = 0
        self._active = False
        self._closed = False
        self._usage_lock = threading.Lock()
        self._seen_messages: set[tuple[str, str]] = set()
        self._stderr_snapshot: tuple[str, ...] = ()
        self._stderr_path = (
            config.trusted_dir / "trajectory" / "raw" / "dsh-runtime.stderr.log"
        )

    @property
    def sandbox_launch(self) -> Optional[SandboxLaunch]:
        """Prepared inner launch, exposed for no-execution wiring tests."""

        return self._sandbox_launch

    def bind(self, *, run_id: str) -> None:
        super().bind(run_id=run_id)
        if self.session_id is None:
            self.session_id = f"dsh-{run_id}"
        if not self.session_id.strip():
            raise ValueError("DSH session identity must be non-empty")

    def reset_usage(self) -> None:
        with self._usage_lock:
            self._seen_messages.clear()

    def persist_identity(self, trusted_dir: Path) -> None:
        trusted = Path(trusted_dir).resolve(strict=True)
        configured = self.config.trusted_dir.resolve(strict=True)
        if trusted != configured:
            raise ValueError("DSH trusted identity path disagrees with session config")
        self._identity_path = trusted / "trajectory" / "dsh-native-identity.json"
        self._write_identity()

    def prepare(self, capabilities: _MethodClientCapabilities) -> None:
        from deepseek_harness import DeepSeekHarness
        from deepseek_harness_runtime import bundled_runtime_path

        if self.run_id is None or self.session_id is None:
            raise RuntimeError("DSH method session must be bound before prepare")
        if self._harness is not None:
            raise RuntimeError("DSH method session is already prepared")

        worktree = self.config.worktree.resolve(strict=True)
        run_dir = self.config.run_dir.resolve(strict=True)
        trusted_dir = self.config.trusted_dir.resolve(strict=True)
        if _overlap(worktree, run_dir) or _overlap(worktree, trusted_dir):
            raise ValueError(
                "DSH mutable/trusted state must stay outside the candidate"
            )

        dsh_home = _private_directory(run_dir / "dsh-home")
        private_home = _private_directory(dsh_home / "home")
        private_tmp = _private_directory(dsh_home / "tmp")
        private_cache = _private_directory(dsh_home / "cache")
        sessions = _private_directory(dsh_home / "sessions")
        tools_root = _private_directory(run_dir / "agentic-tools")
        tools_dir = Path(tempfile.mkdtemp(prefix="launch-", dir=tools_root))
        tools_dir.chmod(0o700)
        arena_ipc_dir = _private_directory(dsh_home / "arena-ipc")
        cordis = dsh_home / "cordis.yml"
        atomic_private_copy(self.config.cordis_path, cordis)
        if file_digest(cordis) != self._cordis_template_digest:
            raise RuntimeError(
                "run-private DSH Cordis copy changed during installation"
            )
        _project_bash_command_timeout(
            cordis, timeout_ms=self.config.bash_command_timeout_ms
        )
        _project_reasoning_effort(cordis, reasoning_effort=self.config.reasoning_effort)
        self._cordis_digest = file_digest(cordis)

        sandboxed = self.config.sandbox_backend.strip().lower() in {
            "bwrap",
            "bubblewrap",
        }
        guest_ipc = _GUEST_DSH_HOME / "arena-ipc" if sandboxed else arena_ipc_dir
        capabilities.install(tools_dir, arena_ipc_dir, guest_ipc_dir=guest_ipc)

        secrets = _credential_values(self.config.credential_env)
        public_env = {
            name: os.environ[name] for name in _BASE_ENV if name in os.environ
        }
        public_env.update(dict(self.config.environment))
        client_env = dict(capabilities.client_environment())
        if set(client_env) & set(secrets):
            raise ValueError("Arena client environment overlaps DSH credentials")
        public_env.update(client_env)

        if sandboxed:
            child_home = _GUEST_DSH_HOME / "home"
            child_tmp = _GUEST_DSH_HOME / "tmp"
            child_cache = _GUEST_DSH_HOME / "cache"
            child_sessions = _GUEST_DSH_HOME / "sessions"
            child_cordis = _GUEST_DSH_HOME / "cordis.yml"
            sdk_cwd = "/workspace"
            tools_bin = _GUEST_TOOLS
        else:
            child_home = private_home
            child_tmp = private_tmp
            child_cache = private_cache
            child_sessions = sessions
            child_cordis = cordis
            sdk_cwd = str(worktree)
            tools_bin = str(tools_dir)
        public_env.update(
            {
                "HOME": str(child_home),
                "TMPDIR": str(child_tmp),
                "XDG_CACHE_HOME": str(child_cache),
                "DSH_HOME": str(_GUEST_DSH_HOME if sandboxed else dsh_home),
                "DSH_SESSION_ROOT": str(child_sessions),
                "DSH_CORDIS_CONFIG": str(child_cordis),
                "DSH_CWD": sdk_cwd,
            }
        )
        policy = SandboxPolicy(
            network=self.config.sandbox_network,
            env_allowlist=tuple(sorted(public_env)),
            secret_names=tuple(sorted(secrets)),
        )
        validate_sandbox_environment(policy, public_env, secrets)
        child_env = dict(public_env)
        child_env.update(secrets)

        runtime_argv = self.config.launch_args_override
        executable = (
            Path(runtime_argv[0]).resolve(strict=True)
            if runtime_argv is not None
            else bundled_runtime_path().resolve(strict=True)
        )
        command = list(runtime_argv or (str(executable),))
        # The fake JSON-RPC bwrap golden uses this exact interpreter.  Mount
        # its self-contained prefix so the guest does not fall back to a
        # different system Python when the fake invokes the Arena CLI.
        override_tool_root = (
            Path(sys.base_prefix).resolve(strict=True)
            if runtime_argv is not None
            and sandboxed
            and executable == Path(sys.executable).resolve(strict=True)
            else None
        )
        self._sandbox = create_harness_sandbox(
            self.config.sandbox_backend,
            bwrap_path=self.config.bwrap_path,
            visible_paths=(tools_dir,),
            mutable_mounts=capabilities.sandbox_mounts(),
            cli_tool_root=override_tool_root,
            allow_unsafe_host=self.config.allow_unsafe_host,
        )
        launch = self._sandbox.prepare(
            command,
            executable=executable,
            worktree=worktree,
            config_dir=dsh_home,
            config_env_var="DSH_HOME",
            env=child_env,
            policy=policy,
        )
        final_env = dict(launch.env)
        runtime_bin = "/rsi-arena/tool/bin" if override_tool_root is not None else None
        final_env.update(
            {
                "HOME": str(child_home),
                "TMPDIR": str(child_tmp),
                "XDG_CACHE_HOME": str(child_cache),
                "DSH_HOME": str(_GUEST_DSH_HOME if sandboxed else dsh_home),
                "DSH_SESSION_ROOT": str(child_sessions),
                "DSH_CORDIS_CONFIG": str(child_cordis),
                "DSH_CWD": sdk_cwd,
                "PATH": ":".join(
                    part
                    for part in (
                        tools_bin,
                        runtime_bin,
                        "/usr/local/bin",
                        "/usr/bin",
                        "/bin",
                    )
                    if part is not None
                ),
            }
        )
        launch = SandboxLaunch(
            argv=launch.argv,
            cwd=launch.cwd,
            env=final_env,
            audit=launch.audit,
        )
        self._sandbox_launch = launch
        write_sandbox_audit(
            trusted_dir / "sandbox.json",
            launch,
            injected_secret_names=tuple(sorted(secrets)),
            credential_file_mounted=False,
        )

        launcher = dsh_home / "launch-runtime.sh"
        _write_environment_launcher(launcher, launch)
        self._harness = DeepSeekHarness(
            provider=self.config.provider,
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            cwd=sdk_cwd,
            runtime_cwd=str(worktree),
            session_root=str(child_sessions),
            cordis=str(child_cordis),
            env=launch.env,
            launch_args_override=(str(launcher),),
            request_timeout_seconds=self.config.request_timeout_seconds,
            shutdown_timeout_seconds=self.config.shutdown_timeout_seconds,
        )
        self._write_identity()

    def run_prompt_interval(
        self,
        *,
        initial_prompt: str,
        raw_output_dir: Path,
        control: MethodIntervalControl,
    ) -> MethodIntervalResult:
        harness = self._harness
        if harness is None or self.session_id is None:
            raise RuntimeError("DSH method session is not prepared")
        if self._closed:
            raise RuntimeError("DSH method session is closed")
        if self._active:
            raise RuntimeError("concurrent DSH prompt intervals are unsupported")

        interval_index = self._interval_index
        interval_id = f"dsh-{interval_index:03d}"
        continuation = interval_index > 0
        prompt = RESUME_PROMPT if continuation else initial_prompt
        started = time.monotonic()
        raw_output_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_output_dir / f"{interval_id}.notifications.jsonl"
        writer = AppendOnlyJsonl(raw_path)
        error_box: list[BaseException] = []

        def on_notification(notification: Notification) -> None:
            writer.append(
                {"method": notification.method, "params": notification.payload}
            )
            self._project_usage(notification, interval_id=interval_id)

        def invoke() -> None:
            try:
                session.run(
                    prompt,
                    on_notification=on_notification,
                )
            except BaseException as exc:
                error_box.append(exc)

        stopped_for_cap = False
        cap_deadline: Optional[float] = None
        timed_out = False
        native_process = None
        self._active = True
        thread: Optional[threading.Thread] = None
        try:
            if self._session is None:
                # bwrap's parent-death signal follows the thread that creates
                # the runtime.  Keep Popen and initialize on the durable runner
                # thread; only the later Session.run call uses a worker.
                harness.client.start()
                native_process = harness.client._proc
                remaining = control.wall_deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    harness.close()
                else:
                    configured_timeout = harness.client.config.request_timeout_seconds
                    initialize_wall_limited = (
                        configured_timeout is None or remaining <= configured_timeout
                    )
                    harness.client.config.request_timeout_seconds = (
                        remaining
                        if configured_timeout is None
                        else min(configured_timeout, remaining)
                    )
                    try:
                        self._session = harness.start_session(self.session_id)
                    except TimeoutError:
                        if not initialize_wall_limited:
                            raise
                        timed_out = True
                    finally:
                        # The wall-derived timeout applies only to initialize;
                        # Session.run remains under the outer wall supervisor.
                        harness.client.config.request_timeout_seconds = (
                            configured_timeout
                        )
                    if not timed_out and time.monotonic() >= control.wall_deadline:
                        timed_out = True
                        harness.close()

            if not timed_out:
                session = self._session
                if session is None:
                    raise RuntimeError(
                        "DSH SDK session initialization returned no session"
                    )
                thread = threading.Thread(
                    target=invoke,
                    name=f"dsh-session-run-{interval_index}",
                    daemon=True,
                )
                thread.start()
                while thread.is_alive():
                    self._persist_stderr()
                    if control.attempt_cap_exhausted():
                        stopped_for_cap = True
                        if cap_deadline is None:
                            cap_deadline = time.monotonic() + control.completion_grace_s
                        if time.monotonic() >= cap_deadline:
                            native_process = harness.client._proc
                            harness.close()
                            break
                    remaining = control.wall_deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        native_process = harness.client._proc
                        harness.close()
                        break
                    wait_for = min(0.05, remaining)
                    if cap_deadline is not None:
                        wait_for = min(
                            wait_for,
                            max(0.0, cap_deadline - time.monotonic()),
                        )
                    thread.join(timeout=wait_for)
                thread.join(timeout=self.config.shutdown_timeout_seconds + 1.0)
                if thread.is_alive():
                    raise RuntimeError(
                        "DSH SDK run thread did not stop with its runtime"
                    )
        finally:
            self._active = False
            self._persist_stderr()
            writer.close()

        if error_box and not (stopped_for_cap or timed_out):
            raise error_box[0]
        elapsed = time.monotonic() - started
        result = MethodIntervalResult(
            command_summary="deepseek-harness-sdk Session.run",
            exit_code=(
                native_process.returncode if native_process is not None else None
            ),
            elapsed_s=elapsed,
            cost_usd=None,
            continuation=continuation,
            timed_out=timed_out,
            session_id=self.session_id,
            interval_id=interval_id,
            raw_output_path=raw_path,
        )
        self._interval_index += 1
        return result

    def current_search_cost_status(self) -> str:
        # rc6 messages expose tokens but no provider dollar cost.
        return "missing"

    def search_cost_summary(self, spent_usd: float) -> tuple[None, str]:
        del spent_usd
        return None, "missing"

    def close(self) -> None:
        harness = self._harness
        if harness is not None:
            harness.close()
        self._persist_stderr()
        self._closed = True

    def _project_usage(self, notification: Notification, *, interval_id: str) -> None:
        if notification.method != "session.event":
            return
        session_id = notification.payload.get("sessionId")
        event = notification.payload.get("event")
        if not isinstance(session_id, str) or not isinstance(event, Mapping):
            return
        if event.get("type") != "assistant/message":
            return
        data = event.get("data")
        if not isinstance(data, Mapping):
            return
        message = data.get("message")
        if not isinstance(message, Mapping):
            return
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            return
        identity = (session_id, message_id)
        with self._usage_lock:
            if identity in self._seen_messages:
                return
            self._seen_messages.add(identity)

        usage = data.get("usage")
        projected = (
            {
                target: value
                for source, target in (
                    ("inputTokens", "tokens_in"),
                    ("outputTokens", "tokens_out"),
                    ("cacheReadTokens", "cache_read_tokens"),
                    ("cacheWriteTokens", "cache_write_tokens"),
                    ("reasoningTokens", "thinking_tokens"),
                )
                if (value := _token_value(usage.get(source))) is not None
            }
            if isinstance(usage, Mapping)
            else {}
        )
        if self.ledger is None:
            raise RuntimeError("DSH usage requires a bound usage ledger")
        source = message.get("source")
        source = source if isinstance(source, Mapping) else {}
        provider = source.get("provider")
        model = source.get("model")
        tokens_in = projected.get("tokens_in")
        tokens_out = projected.get("tokens_out")
        token_status = (
            "reported"
            if tokens_in is not None and tokens_out is not None
            else "unverified"
            if projected
            else "missing"
        )
        self.ledger.record(
            "solver-agent",
            model=str(model) if isinstance(model, str) and model else self.config.model,
            provider=(
                str(provider)
                if isinstance(provider, str) and provider
                else self.config.provider
            ),
            call_id=message_id,
            session_id=session_id,
            segment_id=interval_id,
            message_id=message_id,
            usage_scope="message_leaf",
            token_source="cli" if projected else "unknown",
            token_status=token_status,
            cost_usd=None,
            provider_reported_cost=None,
            cost_source="unknown",
            cost_status="missing",
            cache_status=(
                "hit"
                if projected.get("cache_read_tokens", 0)
                else "miss_or_unreported"
                if projected
                else "missing"
            ),
            **projected,
        )

    def _persist_stderr(self) -> None:
        harness = self._harness
        if harness is None:
            return
        snapshot = tuple(harness.client._stderr_lines)
        overlap = min(len(self._stderr_snapshot), len(snapshot))
        while overlap and self._stderr_snapshot[-overlap:] != snapshot[:overlap]:
            overlap -= 1
        new_lines = snapshot[overlap:]
        self._stderr_snapshot = snapshot
        if not new_lines:
            return
        self._stderr_path.parent.mkdir(parents=True, exist_ok=True)
        with self._stderr_path.open("a", encoding="utf-8") as stream:
            for line in new_lines:
                stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _write_identity(self) -> None:
        if self._identity_path is None or self.session_id is None:
            return
        sandbox_identity = (
            self._sandbox.identity_payload() if self._sandbox is not None else None
        )
        atomic_write_json(
            self._identity_path,
            {
                "schema": "rsi-arena-dsh-native-identity",
                **self._official_identity,
                "cordis_template_sha256": self._cordis_template_digest,
                "cordis_sha256": self._cordis_digest,
                "bash_command_timeout_ms": self.config.bash_command_timeout_ms,
                "session_id": self.session_id,
                "provider": self.config.provider,
                "model": self.config.model,
                "max_tokens": self.config.max_tokens,
                "reasoning_effort": self.config.reasoning_effort,
                "credential_env_names": sorted(self.config.credential_env),
                "runtime_environment_names": sorted(
                    name for name, _ in self.config.environment
                ),
                "runtime_launch_kind": (
                    "test_override"
                    if self.config.launch_args_override is not None
                    else "official_bundled"
                ),
                "sandbox": sandbox_identity,
            },
        )


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return ensure_private_config_directory(path)


def _project_bash_command_timeout(path: Path, *, timeout_ms: int) -> None:
    """Freeze the trusted experiment lifetime into one run-private Cordis."""

    source = path.read_text(encoding="utf-8")
    if source.count(_BASH_CONFIG_ANCHOR) != 1:
        raise RuntimeError("DSH Cordis Bash configuration anchor drifted")
    resolved = source.replace(
        _BASH_CONFIG_ANCHOR,
        _BASH_CONFIG_ANCHOR
        + f"    timeoutMs: {timeout_ms}\n"
        + f"    maxTimeoutMs: {timeout_ms}\n",
    )
    atomic_write_bytes(path, resolved.encode("utf-8"))


def _project_reasoning_effort(path: Path, *, reasoning_effort: Optional[str]) -> None:
    """Freeze an optional operator-selected DeepSeek effort into Cordis."""

    if reasoning_effort is None:
        return
    source = path.read_text(encoding="utf-8")
    if source.count(_DEEPSEEK_CONFIG_ANCHOR) != 1:
        raise RuntimeError("DSH Cordis DeepSeek configuration anchor drifted")
    resolved = source.replace(
        _DEEPSEEK_CONFIG_ANCHOR,
        _DEEPSEEK_CONFIG_ANCHOR
        + "  config:\n"
        + f"    reasoningEffort: {json.dumps(reasoning_effort)}\n",
    )
    atomic_write_bytes(path, resolved.encode("utf-8"))


def _credential_values(names: tuple[str, ...]) -> dict[str, str]:
    missing = [name for name in names if name not in os.environ]
    if missing:
        raise RuntimeError(f"missing trusted DSH credential environment: {missing[0]}")
    return {name: os.environ[name] for name in names}


def _write_environment_launcher(path: Path, launch: SandboxLaunch) -> None:
    """Whitelist environment names before the official SDK-owned subprocess.

    rc6 intentionally merges its supplied environment over ``os.environ``.
    This launcher prevents ambient host variables from reaching the sandboxed
    runtime without placing any environment value in argv, script, or audit.
    """

    env_bin = shutil.which("env")
    if env_bin is None:
        raise FileNotFoundError("the DSH environment launcher requires /usr/bin/env")
    assignments = [f'  "{name}=${{{name}-}}" \\\n' for name in sorted(launch.env)]
    command = " ".join(shlex.quote(argument) for argument in launch.argv)
    source = "#!/bin/sh\nexec " + shlex.quote(env_bin) + " -i \\\n"
    source += "".join(assignments)
    source += "  " + command + "\n"
    atomic_write_bytes(path, source.encode("utf-8"))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def _token_value(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


__all__ = [
    "DSH_API_BASE_URL_ENV",
    "DSH_CORDIS_PATH",
    "DSH_CREDENTIAL_ENV",
    "DSH_MODEL_ENV",
    "DSH_OFFICIAL_PROVIDER",
    "DSH_RUNTIME_LINUX_X86_64_WHEEL_SHA256",
    "DSH_RUNTIME_VERSION",
    "DSH_SDK_VERSION",
    "DSH_SDK_WHEEL_SHA256",
    "DshSdkSession",
    "DshSdkSessionAuthority",
    "DshSdkSessionConfig",
    "official_dsh_identity",
    "project_dsh_sdk_environment",
    "resolve_dsh_sdk_session_authority",
]
