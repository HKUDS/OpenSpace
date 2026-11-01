"""Configuration for external coding workers, independent of their LLM provider."""

from __future__ import annotations

import json
import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .worker_sessions import validate_session_command

_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
BACKENDS = frozenset(
    {"codex", "claude_code", "hermes", "nanobot", "codebuddy", "dsh", "command"}
)


@dataclass(frozen=True, slots=True)
class WorkerProviderSettings:
    name: str
    model: str
    api_key_env: str
    base_url: str


@dataclass(frozen=True, slots=True)
class WorkerRuntimeSettings:
    backend: str
    mode: str
    command: tuple[str, ...]
    runtime_root: Path
    reservation_usd: float
    # New CLI integrations deliberately use the harness's execution policy.
    # An explicit host choice prevents silently downgrading a legacy bwrap run.
    sandbox_backend: str
    timeout_seconds: float = 1800.0
    max_prompt_bytes: int = 65536
    max_output_bytes: int = 2097152
    env_allowlist: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
    secret_env_names: tuple[str, ...] = ()
    provider: WorkerProviderSettings | None = None
    # For arbitrary harnesses, these map destination environment names to
    # {model}, {base_url}, or {api_key}; credentials never enter command argv.
    provider_env: tuple[tuple[str, str], ...] = ()
    # Custom wrappers must bind both values to their own session interface.
    session_env: tuple[tuple[str, str], ...] = ()


def parse_worker_settings(value) -> WorkerRuntimeSettings:
    field = "environment.settings.action_runtime.worker"
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a table")
    allowed = set(WorkerRuntimeSettings.__dataclass_fields__)
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown {field} keys: {sorted(unknown)}")
    required = {
        "backend",
        "mode",
        "command",
        "runtime_root",
        "reservation_usd",
        "sandbox_backend",
    }
    if missing := required - set(value):
        raise ValueError(f"missing {field} keys: {sorted(missing)}")
    backend, mode = value["backend"], value["mode"]
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise ValueError(f"{field}.backend must be one of {sorted(BACKENDS)}")
    if not isinstance(mode, str) or mode not in {"native", "provider"}:
        raise ValueError(f"{field}.mode must be native or provider")
    command = value["command"]
    if (
        not isinstance(command, (list, tuple))
        or not command
        or any(not isinstance(arg, str) or "\0" in arg or not arg for arg in command)
    ):
        raise ValueError(f"{field}.command must be a nonempty argv array")
    if not Path(command[0]).is_absolute():
        raise ValueError(f"{field}.command[0] must be an absolute executable path")
    validate_session_command(backend, tuple(command))
    tokens = {"{prompt}", "{prompt_file}", "{workspace}", "{session_id}", "{session_root}"}
    if mode == "provider":
        tokens |= {"{model}", "{base_url}"}
    for part in command:
        if "{api_key}" in part:
            raise ValueError(
                f"{field}: credentials must never appear in command arguments"
            )
        if re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", part) and part not in tokens:
            raise ValueError(f"{field}: placeholders must be supported whole arguments: {part!r}")
    if backend not in {"command", "dsh"} and any(part in tokens for part in command):
        raise ValueError(
            f"{field}: command placeholders are only for command/dsh backends"
        )
    root = value["runtime_root"]
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ValueError(f"{field}.runtime_root must be absolute")
    if value["sandbox_backend"] != "host":
        raise ValueError(
            f"{field}.sandbox_backend must explicitly be host; configure harness-native isolation"
        )

    def number(name, default=None, *, integer=False, maximum=None):
        item = value.get(name, default)
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            or item <= 0
            or integer
            and not isinstance(item, int)
            or maximum is not None
            and item > maximum
        ):
            raise ValueError(
                f"{field}.{name} must be a positive {'integer' if integer else 'number'}"
                + (f" <= {maximum}" if maximum else "")
            )
        return item

    def names(name, default):
        items = value.get(name, default)
        if (
            not isinstance(items, (list, tuple))
            or any(
                not isinstance(item, str) or not _ENV.fullmatch(item) for item in items
            )
            or len(items) != len(set(items))
        ):
            raise ValueError(f"{field}.{name} must contain unique environment names")
        return tuple(items)

    env = names(
        "env_allowlist",
        WorkerRuntimeSettings.__dataclass_fields__["env_allowlist"].default,
    )
    secrets = names("secret_env_names", ())
    if set(env) & set(secrets):
        raise ValueError(f"{field} ordinary and secret environment names overlap")
    session_mapping = value.get("session_env", {})
    if not isinstance(session_mapping, Mapping) or any(
        not isinstance(k, str) or not _ENV.fullmatch(k)
        or v not in ("{session_id}", "{session_root}")
        for k, v in session_mapping.items()
    ):
        raise ValueError(f"{field}.session_env maps names to session_id/session_root placeholders")
    if session_mapping and backend != "command":
        raise ValueError(f"{field}.session_env is only used by custom command workers")
    if set(session_mapping) & (set(env) | set(secrets)):
        raise ValueError(f"{field}.session_env must not overwrite inherited environment names")
    if backend == "command" and not {"{session_id}", "{session_root}"} <= (
        set(command) | set(session_mapping.values())
    ):
        raise ValueError(
            f"{field}: custom command must bind both {{session_id}} and {{session_root}} "
            "in command arguments or session_env, and use them for fresh context and memory"
        )
    provider = None
    mapping = value.get("provider_env", {})
    if not isinstance(mapping, Mapping) or any(
        not isinstance(k, str)
        or not _ENV.fullmatch(k)
        or not isinstance(v, str)
        or v not in {"{model}", "{base_url}", "{api_key}"}
        for k, v in mapping.items()
    ):
        raise ValueError(
            f"{field}.provider_env maps environment names to model/base_url/api_key placeholders"
        )
    if set(mapping) & set(session_mapping):
        raise ValueError(f"{field}: provider_env and session_env must not overlap")
    if mode == "native":
        if "provider" in value or mapping:
            raise ValueError(
                f"{field}: native mode must not override provider settings"
            )
    else:
        raw = value.get("provider")
        if not isinstance(raw, Mapping) or set(raw) - {
            "name",
            "model",
            "api_key_env",
            "base_url",
        }:
            raise ValueError(
                f"{field}.provider must contain name, model, api_key_env and optional base_url"
            )
        if any(
            not isinstance(raw.get(k), str) or not raw[k].strip() or "\0" in raw[k]
            for k in ("name", "model", "api_key_env")
        ):
            raise ValueError(f"{field}.provider requires name, model and api_key_env")
        if not _ENV.fullmatch(raw["api_key_env"]):
            raise ValueError(
                f"{field}.provider.api_key_env must be an environment name"
            )
        base_url = raw.get("base_url")
        if base_url is None and raw["name"] == "openrouter":
            base_url = (
                "https://openrouter.ai/api"
                if backend == "claude_code"
                else "https://openrouter.ai/api/v1"
            )
        if not isinstance(base_url, str):
            raise ValueError(
                f"{field}.provider.base_url is required for custom providers"
            )
        url = urlsplit(base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.netloc
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                f"{field}.provider.base_url must be an HTTP(S) endpoint without credentials"
            )
        provider = WorkerProviderSettings(
            raw["name"], raw["model"], raw["api_key_env"], base_url.rstrip("/")
        )
        if backend == "hermes" and (
            provider.name != "openrouter"
            or provider.base_url != "https://openrouter.ai/api/v1"
        ):
            raise ValueError(
                f"{field}: Hermes preset supports OpenRouter; use command/provider_env for other routes"
            )
        secrets = tuple(dict.fromkeys((*secrets, provider.api_key_env)))
        if set(env) & set(secrets):
            raise ValueError(
                f"{field}: provider credential must not be in env_allowlist"
            )
        if backend in {"command", "dsh"} and set(mapping.values()) != {
            "{model}",
            "{base_url}",
            "{api_key}",
        }:
            raise ValueError(
                f"{field}: {backend} provider mode requires explicit provider_env bindings"
            )
        if backend not in {"command", "dsh"} and mapping:
            raise ValueError(
                f"{field}: provider_env is only used by command and dsh backends"
            )
    return WorkerRuntimeSettings(
        backend=backend,
        mode=mode,
        command=tuple(command),
        runtime_root=Path(root),
        reservation_usd=float(number("reservation_usd")),
        sandbox_backend="host",
        timeout_seconds=float(number("timeout_seconds", 1800.0, maximum=86400)),
        max_prompt_bytes=number(
            "max_prompt_bytes", 65536, integer=True, maximum=1048576
        ),
        max_output_bytes=number(
            "max_output_bytes", 2097152, integer=True, maximum=67108864
        ),
        env_allowlist=env,
        secret_env_names=secrets,
        provider=provider,
        provider_env=tuple(sorted(mapping.items())),
        session_env=tuple(sorted(session_mapping.items())),
    )


def load_worker_config(path: str | Path) -> WorkerRuntimeSettings:
    """Read a standalone worker file using the same contract as runtime TOMLs."""
    with Path(path).expanduser().open("rb") as source:
        document = tomllib.load(source)
    if set(document) != {"worker"}:
        raise ValueError("worker config must contain only the [worker] table")
    return parse_worker_settings(document["worker"])


def render_worker_settings(settings: WorkerRuntimeSettings) -> str:
    """Render a validated worker and its nested provider tables for a runtime."""
    table = "environment.settings.action_runtime.worker"
    values = asdict(settings)
    provider = values.pop("provider")
    provider_env = dict(values.pop("provider_env"))
    session_env = dict(values.pop("session_env"))
    values["runtime_root"] = str(settings.runtime_root)

    def render(name, items):
        return "\n".join(
            [f"[{name}]"]
            + [
                f"{key} = {json.dumps(value, ensure_ascii=False)}"
                for key, value in items.items()
            ]
        )

    blocks = [render(table, values)]
    if provider is not None:
        blocks.append(render(f"{table}.provider", provider))
    if provider_env:
        blocks.append(render(f"{table}.provider_env", provider_env))
    if session_env:
        blocks.append(render(f"{table}.session_env", session_env))
    return "\n\n".join(blocks)


def worker_settings(runtime):
    """The selected settings, including unchanged legacy Codex configurations."""
    return getattr(runtime, "worker", None) or runtime.codex


def worker_reservation(runtime) -> float:
    settings = worker_settings(runtime)
    if isinstance(settings, WorkerRuntimeSettings):
        return settings.reservation_usd
    return float(settings.settled_cost_stop_usd) * 2


def worker_identity(runtime) -> dict:
    """Preserve legacy fingerprints so saved builds are still replayable."""
    settings = worker_settings(runtime)
    return {
        "worker" if isinstance(settings, WorkerRuntimeSettings) else "codex": settings
    }
