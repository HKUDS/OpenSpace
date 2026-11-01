"""Shared Harbor runner primitive for the frozen native mini-swe loop.

Benchmark packages own task-environment and artifact-boundary policy.  This
module only consumes the explicit artifact source supplied by that package and
otherwise lets Harbor's task environment choose the execution working
directory.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import stat
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .agent_bridge import TreeSnapshotCodec, load_hmac_key_file

AGENT_IMPORT_PATH = (
    "rsi_arena.benchmarks.harbor_runtime.frozen_mini_agent:HarborFrozenMiniAgent"
)
AGENT_NAME = "rsi-arena-frozen-mini-swe"
GUEST_CANDIDATE_ROOT = Path("/opt/rsi-arena/frozen-mini")
CREDENTIAL_HANDLE_ENV = "RSI_ARENA_PROVIDER_CREDENTIAL_FILE"


class HarborFrozenMiniAgentError(RuntimeError):
    """The shared frozen mini runner failed its trusted Harbor boundary."""


try:  # Harbor is intentionally not an RSI Arena package dependency.
    from harbor.agents.installed.base import BaseInstalledAgent as _BaseInstalledAgent
except ModuleNotFoundError as exc:  # pragma: no cover - depends on runtime env.
    if exc.name and (exc.name == "harbor" or exc.name.startswith("harbor.")):

        class _BaseInstalledAgent:  # type: ignore[no-redef]
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                raise HarborFrozenMiniAgentError(
                    "HarborFrozenMiniAgent must be constructed by trusted Harbor"
                )

    else:
        raise


def _safe_model_environment(
    extra_env: Mapping[str, str], credential_env_name: str
) -> Path:
    if (
        not credential_env_name.isidentifier()
        or credential_env_name.upper() != credential_env_name
    ):
        raise HarborFrozenMiniAgentError("credential environment name is invalid")
    if set(extra_env) != {CREDENTIAL_HANDLE_ENV}:
        raise HarborFrozenMiniAgentError(
            "Harbor agent received unexpected environment names"
        )
    value = extra_env.get(CREDENTIAL_HANDLE_ENV)
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise HarborFrozenMiniAgentError(
            "Harbor agent credential handle was not delivered"
        )
    handle = Path(value)
    try:
        info = handle.lstat()
    except OSError as exc:
        raise HarborFrozenMiniAgentError(
            "Harbor agent credential handle is unavailable"
        ) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
        raise HarborFrozenMiniAgentError("Harbor agent credential handle is unsafe")
    return handle


class HarborFrozenMiniAgent(_BaseInstalledAgent):
    """Trusted Harbor wrapper that runs only the frozen native mini loop."""

    SUPPORTS_ATIF = False
    SUPPORTS_RESUME = False
    SUPPORTS_WINDOWS = False

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        candidate_tree_digest: str,
        task_public_digest: str,
        backend_digest: str,
        nonce: str,
        envelope_key_file: str,
        api_base_url: str,
        credential_env_name: str,
        model_revision: str,
        artifact_source: str,
        artifact_payload_name: str,
        worker_timeout_seconds: int = 3000,
        candidate_harvest_grace_seconds: int = 60,
        logger: Any | None = None,
        mcp_servers: list[Any] | None = None,
        skills_dir: str | None = None,
        extra_env: dict[str, str] | None = None,
        **_kwargs: object,
    ) -> None:
        # Keep credentials out of BaseAgent.extra_env.  Harbor otherwise scopes
        # them around setup as well as run and may inject them more broadly.
        super().__init__(logs_dir=logs_dir, model_name=model_name, logger=logger)
        if model_name is None or not model_name.strip():
            raise HarborFrozenMiniAgentError("model name is required")
        if mcp_servers or skills_dir is not None:
            raise HarborFrozenMiniAgentError(
                "MCP/skills are outside the frozen mini runner contract"
            )
        parsed = urlparse(str(api_base_url))
        if parsed.scheme != "https" or not parsed.netloc or parsed.username:
            raise HarborFrozenMiniAgentError(
                "model base URL must be a non-credential HTTPS URL"
            )
        if not str(model_revision).strip():
            raise HarborFrozenMiniAgentError("model revision is required")
        resolved_artifact_source = str(artifact_source)
        if (
            not resolved_artifact_source
            or not Path(resolved_artifact_source).is_absolute()
        ):
            raise HarborFrozenMiniAgentError("artifact source must be an absolute path")
        resolved_payload_name = str(artifact_payload_name)
        if (
            not resolved_payload_name
            or Path(resolved_payload_name).name != resolved_payload_name
            or resolved_payload_name in {".", ".."}
        ):
            raise HarborFrozenMiniAgentError("artifact payload name is invalid")
        self._credential_handle_path = _safe_model_environment(
            extra_env or {}, str(credential_env_name)
        )
        self._credential_env_name = str(credential_env_name)
        self._api_base_url = str(api_base_url).rstrip("/")
        self._model_revision = str(model_revision)
        self._candidate_tree_digest = str(candidate_tree_digest)
        self._task_public_digest = str(task_public_digest)
        self._backend_digest = str(backend_digest)
        self._nonce = str(nonce)
        self._artifact_source = resolved_artifact_source
        self._artifact_payload_name = resolved_payload_name
        self._hmac_key = load_hmac_key_file(envelope_key_file)
        self._timeout = int(worker_timeout_seconds)
        if self._timeout < 1:
            raise HarborFrozenMiniAgentError("worker timeout must be positive")
        self._harvest_grace_seconds = int(candidate_harvest_grace_seconds)
        if self._harvest_grace_seconds < 1:
            raise HarborFrozenMiniAgentError("candidate harvest grace must be positive")

    @staticmethod
    def name() -> str:
        return AGENT_NAME

    def get_version_command(self) -> str | None:
        return None

    def version(self) -> str:
        return "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    async def install(self, environment: Any) -> None:
        await self.ensure_system_dependencies(
            environment,
            (
                "curl",
                "bash",
                "build_tools",
                "coreutils",
                "git",
                "python3",
                "python_pip",
            ),
        )
        upstream = GUEST_CANDIDATE_ROOT / "src/harbor/agents/mini_swe_agent/upstream"
        install_source = '"$HOME/.cache/rsi-arena-mini-swe-agent-source"'
        command = (
            "set -euo pipefail; "
            "if ! command -v uv >/dev/null 2>&1; then "
            "curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh; fi; "
            'if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; fi; '
            'export PATH="$HOME/.local/bin:$PATH"; '
            f"test -r {shlex.quote(str(upstream / 'pyproject.toml'))}; "
            f"rm -rf -- {install_source}; "
            f"mkdir -p -- {install_source}; "
            f"cp -a -- {shlex.quote(str(upstream))}/. {install_source}/; "
            f"chmod -R u+w -- {install_source}; "
            f"uv tool install --force {install_source} "
            "--with 'litellm[proxy]'"
        )
        result = await self.exec_as_agent(
            environment, command=command, timeout_sec=min(self._timeout, 900)
        )
        if result.return_code != 0:
            raise HarborFrozenMiniAgentError(
                "frozen mini dependency installation failed"
            )

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        upstream = GUEST_CANDIDATE_ROOT / "src/harbor/agents/mini_swe_agent/upstream"
        output = Path("/logs/agent/mini-swe-agent.trajectory.json")
        stdout = Path("/logs/agent/mini-swe-agent.stdout.log")
        config_host = Path(self.logs_dir) / "model-delivery.yaml"
        config_host.write_text(
            __import__("yaml").safe_dump(
                {
                    "model": {
                        "model_class": "litellm",
                        "model_name": self.model_name,
                        "model_kwargs": {"api_base": self._api_base_url},
                        "cost_tracking": "ignore_errors",
                    },
                    "environment": {
                        "environment_class": "local",
                        "timeout": 30,
                        "env": {
                            "PAGER": "cat",
                            "MANPAGER": "cat",
                            "PIP_PROGRESS_BAR": "off",
                        },
                    },
                    "agent": {
                        "mode": "yolo",
                        "confirm_exit": False,
                        "cost_limit": 0,
                        "step_limit": 250,
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        config_host.chmod(0o600)
        tool_python = Path("$HOME/.local/share/uv/tools/mini-swe-agent/bin/python")
        guest_credential = Path(
            "/tmp/rsi-arena-provider-credential-" + uuid.uuid4().hex
        )
        native_pgid = Path("/tmp/rsi-arena-native-pgid-" + uuid.uuid4().hex)
        native_launcher = (
            "import os,sys; "
            "os.setsid(); "
            f"pg=open({str(native_pgid)!r},'w'); "
            "pg.write(str(os.getpid())); pg.close(); "
            f"fd=os.open({str(stdout)!r},os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600); "
            "os.dup2(fd,1); os.dup2(fd,2); os.close(fd); "
            "os.execv(sys.executable,[sys.executable,'-m',"
            "'minisweagent.run.mini',*sys.argv[1:]])"
        )
        native_command = (
            "set -uo pipefail; "
            "export MSWEA_CONFIGURED=true; "
            f'export {self._credential_env_name}="$(cat '
            f'{shlex.quote(str(guest_credential))})"; '
            f"rm -f {shlex.quote(str(guest_credential))}; "
            f"test -r {shlex.quote(str(upstream / 'src/minisweagent/run/mini.py'))}; "
            "command -v timeout >/dev/null; "
            f"export PYTHONPATH={shlex.quote(str(upstream / 'src'))}; "
            "timeout --foreground --signal=TERM --kill-after=5s "
            f"{self._timeout}s "
            f'"{tool_python}" -c {shlex.quote(native_launcher)} '
            "--yolo --exit-immediately "
            f"--task {shlex.quote(str(instruction))} "
            f"--output {shlex.quote(str(output))} "
            f"--config {shlex.quote(str(upstream / 'src/minisweagent/config/mini.yaml'))} "
            "--config /logs/agent/model-delivery.yaml </dev/null"
        )
        stop_native_group = (
            "set -u; "
            f"if [ -e {shlex.quote(str(native_pgid))} ]; then "
            f'pgid="$(cat -- {shlex.quote(str(native_pgid))})"; '
            "case \"$pgid\" in ''|*[!0-9]*) exit 70;; esac; "
            'kill -TERM -- "-$pgid" 2>/dev/null || true; '
            "for attempt in 1 2 3 4 5 6 7 8 9 10; do "
            'if ! kill -0 -- "-$pgid" 2>/dev/null; then break; fi; '
            "sleep 0.2; done; "
            'kill -KILL -- "-$pgid" 2>/dev/null || true; '
            f"rm -f -- {shlex.quote(str(native_pgid))}; "
            "fi"
        )
        # Passing ``env=...`` would place the credential in Docker's process
        # argv (``docker compose exec -e KEY=value``).  Upload a short-lived
        # root-only file instead, remove it in the launching shell before the
        # native Python process starts, and never place the value in a command,
        # Harbor log, or persistent task artifact.
        host_credential = self._credential_handle_path
        native_result: object | None = None
        native_exception_type: str | None = None
        try:
            try:
                await environment.upload_file(host_credential, str(guest_credential))
            finally:
                host_credential.unlink(missing_ok=True)
            permission = await environment.exec(
                command=f"chmod 600 {shlex.quote(str(guest_credential))}",
                user="root",
                timeout_sec=30,
            )
            if permission.return_code != 0:
                raise HarborFrozenMiniAgentError("provider credential staging failed")
            try:
                native_result = await environment.exec(
                    command=native_command,
                    timeout_sec=self._timeout + min(10, self._harvest_grace_seconds),
                )
            except Exception as exc:
                native_exception_type = type(exc).__name__
        finally:
            try:
                stopped = await environment.exec(
                    command=stop_native_group,
                    user="root",
                    timeout_sec=10,
                )
                if stopped.return_code != 0:
                    raise HarborFrozenMiniAgentError(
                        "native candidate process group could not be stopped"
                    )
            finally:
                credential_cleanup = await environment.exec(
                    command=f"rm -f {shlex.quote(str(guest_credential))}",
                    user="root",
                    timeout_sec=30,
                )
                if credential_cleanup.return_code != 0:
                    raise HarborFrozenMiniAgentError(
                        "provider credential cleanup failed"
                    )

        raw_return_code = getattr(native_result, "return_code", None)
        native_return_code = (
            int(raw_return_code)
            if isinstance(raw_return_code, int)
            and not isinstance(raw_return_code, bool)
            else None
        )
        trajectory: Mapping[str, object] | None = None
        try:
            loaded_trajectory = json.loads(
                (Path(self.logs_dir) / output.name).read_text(encoding="utf-8")
            )
            if not isinstance(loaded_trajectory, Mapping):
                raise ValueError("trajectory is not an object")
            trajectory = loaded_trajectory
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        raw_info = (trajectory or {}).get("info")
        info = raw_info if isinstance(raw_info, Mapping) else {}
        input_tokens = output_tokens = cache_tokens = 0
        token_rows = 0
        raw_messages = (trajectory or {}).get("messages")
        messages = raw_messages if isinstance(raw_messages, list) else []
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            extra = message.get("extra")
            response = extra.get("response") if isinstance(extra, dict) else None
            usage = response.get("usage") if isinstance(response, dict) else None
            if isinstance(usage, dict):
                prompt = usage.get("prompt_tokens")
                completion = usage.get("completion_tokens")
                if (
                    isinstance(prompt, int)
                    and not isinstance(prompt, bool)
                    and prompt >= 0
                    and isinstance(completion, int)
                    and not isinstance(completion, bool)
                    and completion >= 0
                ):
                    input_tokens += prompt
                    output_tokens += completion
                    details = usage.get("prompt_tokens_details")
                    if isinstance(details, dict):
                        cached = details.get("cached_tokens")
                        if (
                            isinstance(cached, int)
                            and not isinstance(cached, bool)
                            and cached >= 0
                        ):
                            cache_tokens += cached
                    token_rows += 1
        raw_model_stats = info.get("model_stats")
        model_stats = raw_model_stats if isinstance(raw_model_stats, Mapping) else {}
        raw_calls = model_stats.get("api_calls")
        calls = (
            int(raw_calls)
            if isinstance(raw_calls, int) and not isinstance(raw_calls, bool)
            else 0
        )
        context.n_input_tokens = input_tokens if token_rows == calls and calls else None
        context.n_output_tokens = (
            output_tokens if token_rows == calls and calls else None
        )
        context.n_cache_tokens = cache_tokens if token_rows == calls and calls else None
        # mini's per-message ``cost`` is a trajectory-side estimate, not a
        # provider-reported charge.  Preserve the existing one-task semantics:
        # tokens may be reported while provider cost remains explicitly null.
        context.cost_usd = None
        context.metadata = {
            "schema": "swe-harbor-agent",
            "candidate_tree_digest": self._candidate_tree_digest,
            "model_revision": self._model_revision,
            "credential_env_name": self._credential_env_name,
            "credential_value_recorded": False,
            "native_entrypoint": "python -m minisweagent.run.mini",
            "native_timeout_seconds": self._timeout,
            "candidate_harvest_grace_seconds": self._harvest_grace_seconds,
            "native_execution": {
                "return_code": native_return_code,
                "exception_type": native_exception_type,
                "trajectory_exit_status": info.get("exit_status"),
            },
        }

        convention_dir = (
            Path(self.logs_dir).resolve().parent / "artifacts" / "logs" / "artifacts"
        )
        await TreeSnapshotCodec(payload_name=self._artifact_payload_name).export(
            environment,
            source=self._artifact_source,
            convention_dir=convention_dir,
            nonce=self._nonce,
            candidate_tree_digest=self._candidate_tree_digest,
            task_public_digest=self._task_public_digest,
            bridge_identity_digest=self._backend_digest,
            hmac_key=self._hmac_key,
        )


__all__ = [
    "AGENT_IMPORT_PATH",
    "AGENT_NAME",
    "CREDENTIAL_HANDLE_ENV",
    "GUEST_CANDIDATE_ROOT",
    "HarborFrozenMiniAgent",
    "HarborFrozenMiniAgentError",
]
