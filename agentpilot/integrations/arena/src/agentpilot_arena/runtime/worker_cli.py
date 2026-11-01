"""Run a complete external harness; model configuration is an optional override.

This transport owns neither an agent loop nor inference. Native mode preserves
the selected CLI's authentication and model configuration. Provider mode only
translates configuration into that CLI's supported interface.
"""

from __future__ import annotations

import json
import re
import os
import selectors
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from .action_runner import HarnessResult
from .worker_settings import WorkerRuntimeSettings
from .worker_sessions import (
    CONTEXT_ENV, SESSION_CONTRACT, dsh_patch, hermes_home, nanobot_config, session_scope,
    session_values, validate_session_command,
)


class ExternalCliHarness:
    def __init__(
        self,
        config: WorkerRuntimeSettings,
        *,
        environment: Mapping[str, str],
        prompt_builder: Callable,
    ):
        if not isinstance(config, WorkerRuntimeSettings):
            raise TypeError("worker config must be WorkerRuntimeSettings")
        if not isinstance(environment, Mapping) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in environment.items()
        ):
            raise TypeError("worker environment must be a text mapping")
        self.config = config
        validate_session_command(config.backend, config.command)
        if config.backend == "command" and not {"{session_id}", "{session_root}"} <= (
            set(config.command) | {value for _, value in config.session_env}
        ):
            raise ValueError("custom command must bind {session_id} and {session_root}")
        self.prompt_builder = prompt_builder
        self.environment = {
            k: environment[k]
            for k in (*config.env_allowlist, *config.secret_env_names)
            if k in environment
        }
        executable = Path(config.command[0])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("worker command must start with an executable file")
        for name in config.secret_env_names:
            if not self.environment.get(name):
                raise ValueError(f"required worker credential {name!r} is missing")
        root = config.runtime_root
        if root.is_symlink():
            raise ValueError("worker runtime_root must not be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise ValueError(
                "worker runtime_root must be private and owned by this user"
            )

    def _prompt(self, action, diagnostic=None, rework_budget=0):
        return self.prompt_builder(action, diagnostic, rework_budget)

    def _launch(self, prompt: bytes, workspace: Path, private: Path):
        cfg = self.config
        env = {key: value for key, value in self.environment.items() if key not in CONTEXT_ENV}
        env.update(LANG=env.get("LANG", "C.UTF-8"), LC_ALL=env.get("LC_ALL", "C.UTF-8"))
        provider = cfg.provider
        prompt_path = private / "prompt.txt"
        prompt_path.write_bytes(prompt)
        prompt_path.chmod(0o600)
        replacements = {
            "{prompt}": prompt.decode("utf-8"),
            "{prompt_file}": str(prompt_path),
            "{workspace}": str(workspace),
            **session_values(private),
        }
        env["AGENTPILOT_WORKER_SESSION_ID"] = replacements["{session_id}"]
        env["AGENTPILOT_WORKER_SESSION_ROOT"] = replacements["{session_root}"]
        if provider:
            replacements.update(
                {"{model}": provider.model, "{base_url}": provider.base_url}
            )
        argv = []
        for part in cfg.command:
            if part == "{api_key}":
                raise ValueError("worker credentials must never be passed in argv")
            if re.fullmatch(r"\{[A-Za-z_][A-Za-z0-9_]*\}", part) and part not in replacements:
                raise ValueError(f"unsupported worker command placeholder {part}")
            argv.append(replacements.get(part, part))
        stdin = prompt
        if cfg.backend in {"command", "dsh"}:
            if cfg.backend == "dsh":
                # DSH's headless profile creates a random session, but its store
                # must also be private so session-query tools cannot see old runs.
                selected = None
                for index, arg in enumerate(argv):
                    if arg == "--profile" and index + 1 < len(argv):
                        selected = argv[index + 1]
                    elif arg.startswith("--profile="):
                        selected = arg.split("=", 1)[1]
                if selected not in {None, "headless"}:
                    raise ValueError("dsh worker requires the headless profile for fresh sessions")
                if selected is None:
                    argv += ["--profile", "headless"]
                argv += ["--patch", str(dsh_patch(private))]
                if not any(token in cfg.command for token in ("{prompt}", "{prompt_file}")):
                    argv.append(prompt.decode("utf-8"))
                stdin = b""
            if any(token in cfg.command for token in ("{prompt}", "{prompt_file}")):
                stdin = b""
            if provider:
                values = {
                    "{model}": provider.model,
                    "{base_url}": provider.base_url,
                    "{api_key}": env[provider.api_key_env],
                }
                for name, template in cfg.provider_env:
                    env[name] = values[template]
            for name, template in cfg.session_env:
                env[name] = replacements[template]
            # This contract is also available to user-owned wrapper scripts.
            env["RSI_WORKER_WORKSPACE"] = str(workspace)
            env["RSI_WORKER_PROMPT_FILE"] = str(prompt_path)
            return argv, env, stdin

        if cfg.backend == "codex":
            argv += ["exec", "--json", "--sandbox", "workspace-write", "--ephemeral",
                     "--disable", "memories", "-c", 'history.persistence="none"']
            if provider:
                key_name = "AGENTPILOT_WORKER_PROVIDER_KEY"
                env[key_name] = env[provider.api_key_env]
                overrides = {
                    "model_provider": "agentpilot_worker",
                    "model": provider.model,
                    "model_providers.agentpilot_worker.name": provider.name,
                    "model_providers.agentpilot_worker.base_url": provider.base_url,
                    "model_providers.agentpilot_worker.wire_api": "responses",
                    "model_providers.agentpilot_worker.env_key": key_name,
                    "model_providers.agentpilot_worker.requires_openai_auth": False,
                    "shell_environment_policy.inherit": "none",
                    "shell_environment_policy.include_only": [
                        "PATH",
                        "HOME",
                        "LANG",
                        "LC_ALL",
                    ],
                }
                for name, value in overrides.items():
                    argv += ["-c", f"{name}={json.dumps(value)}"]
            argv += ["-"]
        elif cfg.backend in {"claude_code", "codebuddy"}:
            argv += ["-p", "--output-format", "json", "--no-session-persistence",
                     "--session-id", replacements["{session_id}"]]
            prefix = "CLAUDE_CODE" if cfg.backend == "claude_code" else "CODEBUDDY"
            env[prefix + "_DISABLE_AUTO_MEMORY"] = "1"
            if cfg.backend == "claude_code":
                env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] = "1"
            if provider:
                if cfg.backend == "claude_code":
                    env.update(
                        ANTHROPIC_BASE_URL=provider.base_url,
                        ANTHROPIC_AUTH_TOKEN=env[provider.api_key_env],
                        ANTHROPIC_API_KEY="",
                    )
                    # Auxiliary roles must not silently use another model.
                    for name in (
                        "ANTHROPIC_DEFAULT_OPUS_MODEL",
                        "ANTHROPIC_DEFAULT_SONNET_MODEL",
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                        "CLAUDE_CODE_SUBAGENT_MODEL",
                    ):
                        env[name] = provider.model
                else:
                    env.update(
                        CODEBUDDY_BASE_URL=provider.base_url,
                        CODEBUDDY_API_KEY=env[provider.api_key_env],
                        CODEBUDDY_MODEL=provider.model,
                        CODEBUDDY_BIG_SLOW_MODEL=provider.model,
                        CODEBUDDY_SMALL_FAST_MODEL=provider.model,
                        CODEBUDDY_CODE_SUBAGENT_MODEL=provider.model,
                    )
                argv += ["--model", provider.model]
        elif cfg.backend == "hermes":
            hermes_home(env, workspace, provider)
            argv += ["chat", "--oneshot", "--query-file", "-"]
            if provider:
                if (
                    provider.name != "openrouter"
                    or provider.base_url != "https://openrouter.ai/api/v1"
                ):
                    raise ValueError(
                        "Hermes preset supports OpenRouter; use command/provider_env for other routes"
                    )
                env["OPENROUTER_API_KEY"] = env[provider.api_key_env]
                argv += ["--provider", "openrouter", "--model", provider.model]
        elif cfg.backend == "nanobot":
            path = nanobot_config(env, private, workspace, provider, cfg.command)
            argv += ["agent", "--workspace", str(workspace), "--no-markdown",
                     "--config", str(path), "--session", "cli:" + replacements["{session_id}"]]
            argv += ["-m", prompt.decode("utf-8")]
            stdin = b""
        return argv, env, stdin

    @staticmethod
    def _kill(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @staticmethod
    def _feed(pipe, data):
        try:
            pipe.write(data)
            pipe.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            pipe.close()

    @staticmethod
    def _events(stdout):
        usage = {}
        reported_cost = None
        failed = False
        # Only CLI envelopes are parsed, never assistant message text.
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") in {"result", "agentpilot.worker.result"}:
                failed = event.get("is_error") is True or event.get("success") is False
                value = event.get("total_cost_usd", event.get("cost_usd"))
                if isinstance(value, (float, int)) and not isinstance(value, bool):
                    import math

                    if math.isfinite(value) and value >= 0:
                        reported_cost = value
            if event.get("type") == "turn.completed":
                failed = False
            elif event.get("type") in {"turn.failed", "error"}:
                failed = True
            if isinstance(event.get("usage"), dict):
                for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
                    value = event["usage"].get(key)
                    if type(value) is int and value >= 0:
                        usage[key] = value
        return usage, reported_cost, failed

    def run(self, action, workspace, *, diagnostic=None, rework_budget=0, capture_output=False):
        worktree = workspace.path.resolve(strict=True)
        private_root = self.config.runtime_root.resolve()
        if (
            private_root == worktree
            or private_root in worktree.parents
            or worktree in private_root.parents
        ):
            raise ValueError(
                "worker runtime directory must be disjoint from its checkout"
            )
        prompt = self._prompt(action, diagnostic, rework_budget)
        if len(prompt) > self.config.max_prompt_bytes:
            raise ValueError("worker prompt exceeds its configured allowance")
        secrets = [self.environment[k].encode() for k in self.config.secret_env_names]
        if any(secret in prompt for secret in secrets):
            raise ValueError("worker prompt contains a configured credential")
        started = time.monotonic()
        process = None
        with tempfile.TemporaryDirectory(
            prefix="worker-", dir=self.config.runtime_root
        ) as directory, session_scope():
            argv, env, stdin = self._launch(prompt, worktree, Path(directory))
            if any(
                secret in argument.encode() for secret in secrets for argument in argv
            ):
                raise ValueError("worker command contains a configured credential")
            out, err = bytearray(), bytearray()
            reason = None
            writer = None
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=workspace.path,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                writer = threading.Thread(
                    target=self._feed, args=(process.stdin, stdin), daemon=True
                )
                writer.start()
                with selectors.DefaultSelector() as selector:
                    for pipe, target in ((process.stdout, out), (process.stderr, err)):
                        os.set_blocking(pipe.fileno(), False)
                        selector.register(pipe, selectors.EVENT_READ, target)
                    while selector.get_map():
                        remaining = self.config.timeout_seconds - (
                            time.monotonic() - started
                        )
                        if remaining <= 0:
                            reason = "timeout"
                            break
                        for key, _ in selector.select(min(remaining, 0.1)):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            room = self.config.max_output_bytes - len(out) - len(err)
                            key.data.extend(chunk[: max(0, room)])
                            if len(chunk) > room:
                                reason = "output_limit"
                                break
                        if reason:
                            break
                    if reason:
                        self._kill(process)
                    try:
                        process.wait(
                            timeout=max(
                                0.01,
                                self.config.timeout_seconds
                                - (time.monotonic() - started),
                            )
                        )
                    except subprocess.TimeoutExpired:
                        reason = "timeout"
                        self._kill(process)
                        process.wait()
                leak = any(secret in out or secret in err for secret in secrets)
                usage, reported_cost, failed = (
                    ({}, None, True) if leak else self._events(out)
                )
                reason = "credential_in_output" if leak else reason
                if failed and reason is None:
                    reason = "harness_reported_failure"
                return HarnessResult(
                    diagnostics={
                        "connector": "external-cli",
                        "backend": self.config.backend,
                        "mode": self.config.mode,
                        "session": {"contract": SESSION_CONTRACT, "isolated": True,
                                    "invocation_id": env["AGENTPILOT_WORKER_SESSION_ID"]},
                        "exit_code": process.returncode if not reason else -1,
                        "error_kind": reason,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "usage": usage,
                        "harness_reported_cost_usd": reported_cost,
                        "accounting": {
                            "mode": "external",
                            "actual_cost_usd": None,
                            "budget_policy": "retain_reservation",
                        },
                        "sandbox": {"backend": "host", "harness_policy": True},
                        **({"stdout": out.decode("utf-8", errors="replace")} if capture_output and not leak else {}),
                    },
                    # A CLI estimate/subscription quota is not a provider invoice.
                    cost_usd=None,
                )
            finally:
                if process is not None:
                    self._kill(process)
                    process.wait()
                    for pipe in (process.stdout, process.stderr):
                        pipe.close()
                if writer is not None:
                    writer.join(timeout=1)
