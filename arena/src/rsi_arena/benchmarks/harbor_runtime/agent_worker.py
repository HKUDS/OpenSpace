"""Entrypoint for candidate Terminus-2 inside the fixed Harbor bridge.

This file is mounted read-only by :mod:`agent_bridge`.  It intentionally
contains no HMAC/provenance logic and receives no private evaluation metadata.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

try:
    from .agent_bridge import (
        BRIDGE_SCHEMA,
        CANDIDATE_RUNTIME_FAILURE_KEY,
        RpcLimits,
        candidate_owned_runtime_failure_marker,
        validate_agent_context_payload,
    )
    from .provider_broker import (
        WORKER_PROVIDER_BASE_URL,
        WORKER_PROVIDER_SOCKET,
        UnixProviderRelay,
    )
except ImportError:  # pragma: no cover - standalone bubblewrap worker
    from agent_bridge import (
        BRIDGE_SCHEMA,
        CANDIDATE_RUNTIME_FAILURE_KEY,
        RpcLimits,
        candidate_owned_runtime_failure_marker,
        validate_agent_context_payload,
    )
    from provider_broker import (  # type: ignore[no-redef]
        WORKER_PROVIDER_BASE_URL,
        WORKER_PROVIDER_SOCKET,
        UnixProviderRelay,
    )


class WorkerProtocolError(RuntimeError):
    pass


class CandidateInnerAgentRuntimeError(RuntimeError):
    """Candidate-selected module/callable cannot execute this task."""


class CandidateCommandTimeout(RuntimeError):
    pass


_COMMAND_TIMEOUT_RE = re.compile(r"Command timed out after [0-9]+(?:\.[0-9]+)? seconds")
_CANDIDATE_OVERLAY = Path("/opt/harbor/src/harbor/agents/terminus_2")
_CUSTOM_AGENT_OVERLAY = Path("/rsi-arena/candidate-agent")
_PYTHON_INNER_AGENT_REF = re.compile(
    r"python:(?P<module>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):"
    r"(?P<callable>[A-Za-z_]\w*)\Z"
)


def _terminus_model_name(model_name: object, provider_name: object) -> str:
    """Translate a trusted route into Terminus/LiteLLM's model spelling."""

    if not isinstance(model_name, str) or not model_name:
        raise WorkerProtocolError("Terminus model name is invalid")
    if (
        isinstance(provider_name, str)
        and provider_name.casefold() == "openrouter"
        and not model_name.casefold().startswith("openrouter/")
    ):
        return f"openrouter/{model_name}"
    return model_name


@dataclass(frozen=True)
class AgentExecutionContext:
    """Minimal stable context supplied to an explicitly selected agent loop."""

    benchmark_id: str
    instruction: str
    environment: Any


def _parse_python_inner_agent_ref(ref: object) -> tuple[str, str] | None:
    if not isinstance(ref, str):
        return None
    match = _PYTHON_INNER_AGENT_REF.fullmatch(ref)
    if match is None:
        return None
    return match.group("module"), match.group("callable")


def _load_input(path: Path, maximum: int = 8 * 1024 * 1024) -> dict[str, Any]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise WorkerProtocolError("worker input is not a bounded regular file")
    raw = path.read_bytes()
    if len(raw) > maximum:
        raise WorkerProtocolError("worker input exceeds byte limit")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema") != BRIDGE_SCHEMA:
        raise WorkerProtocolError("worker input schema mismatch")
    required = {
        "schema",
        "agent_ref",
        "candidate_overlay",
        "agent_config",
        "benchmark_id",
        "nonce",
        "instruction",
        "model_name",
        "candidate_kwargs",
        "trusted_api_base",
        "default_user",
        "context_result",
        "worker_status",
        "rpc_socket",
        "rpc_limits",
        "credential_env_name",
        "credential_file",
        "model_runtime",
        "provider_socket",
    }
    if set(value) != required:
        raise WorkerProtocolError("worker input has missing or unknown fields")
    if not isinstance(value["instruction"], str):
        raise WorkerProtocolError("instruction must be a string")
    if (
        value["agent_ref"] != "builtin:terminus-2"
        and _parse_python_inner_agent_ref(value["agent_ref"]) is None
    ):
        raise WorkerProtocolError("agent_ref is unsupported")
    expected_overlay = (
        _CANDIDATE_OVERLAY
        if value["agent_ref"] == "builtin:terminus-2"
        else _CUSTOM_AGENT_OVERLAY
    )
    if value["candidate_overlay"] != str(expected_overlay):
        raise WorkerProtocolError("candidate overlay does not match agent_ref")
    if not isinstance(value["agent_config"], dict):
        raise WorkerProtocolError("agent_config must be an object")
    if not isinstance(value["benchmark_id"], str) or not value["benchmark_id"]:
        raise WorkerProtocolError("benchmark_id must be a non-empty string")
    if not isinstance(value["model_name"], str) or not value["model_name"]:
        raise WorkerProtocolError("model_name must be a non-empty string")
    if not isinstance(value["candidate_kwargs"], dict):
        raise WorkerProtocolError("candidate_kwargs must be an object")
    if (
        not isinstance(value["model_runtime"], dict)
        or value["model_runtime"].get("model_name") != value["model_name"]
    ):
        raise WorkerProtocolError("model_runtime is invalid")
    candidate_api_base = value["candidate_kwargs"].get("api_base")
    trusted_api_base = value["trusted_api_base"]
    if trusted_api_base is None:
        if "api_base" in value["candidate_kwargs"]:
            raise WorkerProtocolError("api_base is a reserved trusted setting")
    else:
        if trusted_api_base != WORKER_PROVIDER_BASE_URL:
            raise WorkerProtocolError("trusted api_base is not the isolated relay")
        if candidate_api_base != trusted_api_base:
            raise WorkerProtocolError("candidate api_base disagrees with trusted route")
        if value["provider_socket"] != WORKER_PROVIDER_SOCKET:
            raise WorkerProtocolError("provider broker socket is invalid")
    if trusted_api_base is None and value["provider_socket"] is not None:
        raise WorkerProtocolError("provider socket requires a trusted route")
    credential_env_name = value["credential_env_name"]
    credential_file = value["credential_file"]
    if (credential_env_name is None) != (credential_file is None):
        raise WorkerProtocolError("credential input pair is incomplete")
    if credential_env_name is not None and (
        not isinstance(credential_env_name, str)
        or not credential_env_name.isidentifier()
        or credential_env_name.upper() != credential_env_name
        or credential_file != "/rsi-arena/provider-credential"
    ):
        raise WorkerProtocolError("credential input is invalid")
    return value


def _install_credential(config: dict[str, Any]) -> str | None:
    env_name = config["credential_env_name"]
    if env_name is None:
        return None
    path = Path(config["credential_file"])
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_size > 64 * 1024
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise WorkerProtocolError("credential file is not a private regular file")
    raw = path.read_bytes()
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkerProtocolError("credential file is not UTF-8") from exc
    if not value or "\x00" in value:
        raise WorkerProtocolError("credential value is invalid")
    os.environ[env_name] = value
    return value


def _exclusive_json(path: Path, value: Any, maximum: int) -> None:
    data = (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if len(data) > maximum:
        raise WorkerProtocolError("worker output exceeds byte limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


class RpcClient:
    def __init__(self, socket_path: str, nonce: str, limits: RpcLimits) -> None:
        self.socket_path = socket_path
        self.nonce = nonce
        self.limits = limits
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 1
        self._wire_bytes = 0
        self._calls = 0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except (ConnectionError, OSError) as exc:
                raise WorkerProtocolError("RPC transport failed while closing") from exc

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        async with self._lock:
            if self._reader is None or self._writer is None:
                try:
                    self._reader, self._writer = await asyncio.open_unix_connection(
                        self.socket_path,
                        limit=self.limits.max_response_bytes + 1,
                    )
                except (OSError, ValueError) as exc:
                    raise WorkerProtocolError(
                        "RPC transport connection failed"
                    ) from exc
            request_id = self._next_id
            self._next_id += 1
            self._calls += 1
            if self._calls > self.limits.max_calls:
                raise WorkerProtocolError("local RPC call cap exceeded")
            request = {
                "schema": BRIDGE_SCHEMA,
                "nonce": self.nonce,
                "id": request_id,
                "method": method,
                "params": params,
            }
            frame = (
                json.dumps(
                    request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            if len(frame) > self.limits.max_request_bytes:
                raise WorkerProtocolError("local RPC request cap exceeded")
            self._wire_bytes += len(frame)
            try:
                self._writer.write(frame)
                await self._writer.drain()
                response_frame = await self._reader.readuntil(b"\n")
            except asyncio.LimitOverrunError as exc:
                raise WorkerProtocolError("RPC response cap exceeded") from exc
            except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
                raise WorkerProtocolError("RPC transport failed") from exc
            self._wire_bytes += len(response_frame)
            if self._wire_bytes > self.limits.max_total_wire_bytes:
                raise WorkerProtocolError("local RPC aggregate cap exceeded")
            try:
                response = json.loads(response_frame)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise WorkerProtocolError("malformed RPC response JSON") from exc
            if (
                not isinstance(response, dict)
                or response.get("schema") != BRIDGE_SCHEMA
                or response.get("id") != request_id
                or not isinstance(response.get("ok"), bool)
            ):
                raise WorkerProtocolError("malformed or mismatched RPC response")
            if not response["ok"]:
                error = response.get("error")
                message = (
                    error.get("message") if isinstance(error, dict) else "RPC failed"
                )
                if (
                    method == "exec"
                    and isinstance(error, dict)
                    and error.get("type") == "RuntimeError"
                    and isinstance(message, str)
                    and _COMMAND_TIMEOUT_RE.fullmatch(message)
                ):
                    raise CandidateCommandTimeout(message)
                raise WorkerProtocolError(str(message))
            return response.get("result")


class RemoteEnvironment:
    """The small BaseEnvironment-shaped surface used by Terminus-2."""

    def __init__(
        self,
        client: RpcClient,
        *,
        default_user: str | int | None,
        limits: RpcLimits,
    ) -> None:
        self._client = client
        self._limits = limits
        self.default_user = default_user
        self.trial_paths = SimpleNamespace(agent_dir=Path("/rsi-arena/logs"))

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> Any:
        params: dict[str, Any] = {"command": command}
        if cwd is not None:
            params["cwd"] = cwd
        if env is not None:
            params["env"] = env
        if timeout_sec is not None:
            params["timeout_sec"] = timeout_sec
        if user is not None:
            params["user"] = user
        result = await self._client.call("exec", params)
        from harbor.environments.base import ExecResult

        try:
            return ExecResult.model_validate(result)
        except Exception as exc:
            raise WorkerProtocolError("malformed exec result") from exc

    async def is_dir(self, path: str, user: str | int | None = None) -> bool:
        params: dict[str, Any] = {"path": path}
        if user is not None:
            params["user"] = user
        result = await self._client.call("is_dir", params)
        if not isinstance(result, bool):
            raise WorkerProtocolError("malformed is_dir result")
        return result

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        source = Path(source_path)
        info = source.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > self._limits.max_transfer_bytes
        ):
            raise WorkerProtocolError("upload source is not a bounded regular file")
        content = source.read_bytes()
        await self._client.call(
            "upload_file",
            {
                "target_path": target_path,
                "content_b64": base64.b64encode(content).decode("ascii"),
            },
        )

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        result = await self._client.call("download_file", {"source_path": source_path})
        if not isinstance(result, dict) or set(result) != {"size_bytes", "content_b64"}:
            raise WorkerProtocolError("malformed download_file result")
        size_bytes = result["size_bytes"]
        content_b64 = result["content_b64"]
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
            or not isinstance(content_b64, str)
        ):
            raise WorkerProtocolError("malformed download_file result")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except Exception as exc:
            raise WorkerProtocolError(
                "download_file result is not strict base64"
            ) from exc
        if len(content) != size_bytes or len(content) > self._limits.max_transfer_bytes:
            raise WorkerProtocolError("download_file size mismatch")
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target, flags, 0o600)
        try:
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                view = view[written:]
        finally:
            os.close(fd)


def _write_candidate_failure(config: dict[str, Any], marker: dict[str, str]) -> None:
    payload = {"metadata": {CANDIDATE_RUNTIME_FAILURE_KEY: marker}}
    _exclusive_json(Path(config["context_result"]), payload, 64 * 1024 * 1024)
    _exclusive_json(
        Path(config["worker_status"]),
        {"ok": False, CANDIDATE_RUNTIME_FAILURE_KEY: marker},
        64 * 1024,
    )


async def _run_builtin_terminus(
    *,
    context: AgentExecutionContext,
    model_runtime: MappingProxyType,
    config: MappingProxyType,
) -> dict[str, Any]:
    """Adapt native Terminus-2 to the shared inner-agent call contract."""

    del config
    from harbor.agents.terminus_2.terminus_2 import Terminus2
    from harbor.models.agent.context import AgentContext

    module = sys.modules.get(Terminus2.__module__)
    module_file = Path(getattr(module, "__file__", "")).resolve()
    # ``_load_input`` has already bound a builtin run to this exact mount.  The
    # entrypoint ``config`` is intentionally only the candidate-authored agent
    # config and must not be overloaded with trusted launcher coordinates.
    if _CANDIDATE_OVERLAY not in module_file.parents:
        raise WorkerProtocolError("Terminus-2 did not load from candidate overlay")
    harbor_context = AgentContext()
    constructor_kwargs = model_runtime.get("agent_constructor_kwargs")
    if not isinstance(constructor_kwargs, dict):
        raise WorkerProtocolError("Terminus model runtime is incomplete")
    agent = Terminus2(
        logs_dir=Path("/rsi-arena/logs"),
        model_name=_terminus_model_name(
            model_runtime["model_name"], model_runtime.get("provider_name")
        ),
        **constructor_kwargs,
    )
    await agent.setup(context.environment)
    await agent.run(context.instruction, context.environment, harbor_context)
    agent.populate_context_post_run(harbor_context)
    return harbor_context.model_dump(mode="json")


async def _run(config: dict[str, Any]) -> None:
    limits = RpcLimits(**config["rpc_limits"])
    client = RpcClient(config["rpc_socket"], config["nonce"], limits)
    environment = RemoteEnvironment(
        client,
        default_user=config["default_user"],
        limits=limits,
    )
    credential_env_name = config["credential_env_name"]
    forbidden = (
        (os.environ[credential_env_name],)
        if credential_env_name is not None and credential_env_name in os.environ
        else ()
    )
    try:
        try:
            execution_context = AgentExecutionContext(
                benchmark_id=config["benchmark_id"],
                instruction=config["instruction"],
                environment=environment,
            )
            model_runtime = MappingProxyType(dict(config["model_runtime"]))
            entrypoint_config = MappingProxyType(dict(config["agent_config"]))
            if config["agent_ref"] == "builtin:terminus-2":
                entrypoint = _run_builtin_terminus
            else:
                parsed = _parse_python_inner_agent_ref(config["agent_ref"])
                if parsed is None:
                    raise WorkerProtocolError("agent_ref is unsupported")
                module_name, callable_name = parsed
                module = __import__(module_name, fromlist=[callable_name])
                module_file = Path(getattr(module, "__file__", "")).resolve()
                candidate_overlay = Path(config["candidate_overlay"])
                if (
                    module_file != candidate_overlay
                    and candidate_overlay not in module_file.parents
                ):
                    raise CandidateInnerAgentRuntimeError(
                        "custom agent did not load from candidate overlay"
                    )
                entrypoint = getattr(module, callable_name, None)
                if not callable(entrypoint):
                    raise CandidateInnerAgentRuntimeError(
                        "custom inner-agent callable is unavailable"
                    )
                try:
                    inspect.signature(entrypoint).bind(
                        context=object(), model_runtime=object(), config=object()
                    )
                except (TypeError, ValueError) as exc:
                    raise CandidateInnerAgentRuntimeError(
                        "custom inner-agent callable must accept context, "
                        "model_runtime, and config"
                    ) from exc
            raw_payload = entrypoint(
                context=execution_context,
                model_runtime=model_runtime,
                config=entrypoint_config,
            )
            if inspect.isawaitable(raw_payload):
                raw_payload = await raw_payload
            payload = validate_agent_context_payload(raw_payload)
        except MemoryError:
            raise
        except WorkerProtocolError:
            raise
        except Exception as exc:
            marker = candidate_owned_runtime_failure_marker(
                exc, forbidden_literals=forbidden
            )
            if marker is None:
                raise
            _write_candidate_failure(config, marker)
            return
        _exclusive_json(Path(config["context_result"]), payload, 64 * 1024 * 1024)
        _exclusive_json(Path(config["worker_status"]), {"ok": True}, 64 * 1024)
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        raise SystemExit("usage: agent_worker.py INPUT_JSON")
    config = _load_input(Path(arguments[0]))
    credential_literal: str | None = None
    relay: UnixProviderRelay | None = None
    try:
        credential_literal = _install_credential(config)
        if config["provider_socket"] is not None:
            relay = UnixProviderRelay(config["provider_socket"])
            relay.start()
        asyncio.run(_run(config))
    except BaseException as exc:
        status = Path(config["worker_status"])
        if not status.exists() and not status.is_symlink():
            try:
                _exclusive_json(
                    status,
                    {
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "message": (
                            str(exc).replace(credential_literal, "[redacted]")[:2048]
                            if credential_literal
                            else str(exc)[:2048]
                        ),
                    },
                    64 * 1024,
                )
            except Exception:
                pass
        raise
    finally:
        if relay is not None:
            relay.close()
        if config["credential_env_name"] is not None:
            os.environ.pop(config["credential_env_name"], None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
