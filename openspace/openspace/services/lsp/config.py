from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from openspace.services.lsp.types import LSPServerConfig
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


def _config_home() -> Path:
    return Path(os.environ.get("OPENSPACE_CONFIG_HOME") or Path.home() / ".openspace")


def _coerce_server_config(raw: Mapping[str, Any], *, cwd: str | None) -> LSPServerConfig | None:
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        return None

    args = raw.get("args") or []
    if not isinstance(args, list):
        args = []

    extension_to_language = (
        raw.get("extensionToLanguage")
        or raw.get("extension_to_language")
        or raw.get("languages")
        or {}
    )
    if not isinstance(extension_to_language, Mapping) or not extension_to_language:
        return None

    env = raw.get("env") or {}
    if not isinstance(env, Mapping):
        env = {}

    workspace_folder = raw.get("workspaceFolder") or raw.get("workspace_folder") or cwd
    if workspace_folder is not None:
        workspace_folder = str(Path(str(workspace_folder)).expanduser())

    initialization_options = raw.get("initializationOptions") or raw.get("initialization_options") or {}
    if not isinstance(initialization_options, Mapping):
        initialization_options = {}

    def _int(name: str, default: int) -> int:
        value = raw.get(name)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    return LSPServerConfig(
        command=command,
        args=[str(arg) for arg in args],
        extension_to_language={
            str(ext).lower(): str(language)
            for ext, language in extension_to_language.items()
            if str(ext)
        },
        env={str(key): str(value) for key, value in env.items()},
        workspace_folder=workspace_folder,
        initialization_options=dict(initialization_options),
        startup_timeout_ms=_int("startupTimeout", _int("startup_timeout_ms", 10_000)),
        request_timeout_ms=_int("requestTimeout", _int("request_timeout_ms", 10_000)),
        max_restarts=_int("maxRestarts", _int("max_restarts", 3)),
    )


def _read_json_file(path: Path) -> Mapping[str, Any] | None:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, Mapping) else None
    except Exception as exc:
        logger.debug("Failed to load LSP config from %s: %s", path, exc)
        return None


def _iter_config_payloads(cwd: str | None) -> list[Mapping[str, Any]]:
    payloads: list[Mapping[str, Any]] = []

    env_config = os.environ.get("OPENSPACE_LSP_SERVERS")
    if env_config:
        try:
            parsed = json.loads(env_config)
            if isinstance(parsed, Mapping):
                payloads.append(parsed)
        except Exception as exc:
            logger.debug("Failed to parse OPENSPACE_LSP_SERVERS: %s", exc)

    explicit_path = os.environ.get("OPENSPACE_LSP_CONFIG")
    if explicit_path:
        payload = _read_json_file(Path(explicit_path).expanduser())
        if payload is not None:
            payloads.append(payload)

    for path in (
        _config_home() / "lsp.json",
        Path(cwd or os.getcwd()) / ".openspace" / "lsp.json",
    ):
        payload = _read_json_file(path)
        if payload is not None:
            payloads.append(payload)

    return payloads


def get_all_lsp_servers(cwd: str | None = None) -> dict[str, LSPServerConfig]:
    """Load all explicitly configured language servers.

    OpenSpace loads LSP servers from explicit config files and env JSON. Missing
    or bad config is non-fatal and returns an empty map.
    """

    servers: dict[str, LSPServerConfig] = {}
    for payload in _iter_config_payloads(cwd):
        raw_servers = payload.get("servers") if "servers" in payload else payload
        if not isinstance(raw_servers, Mapping):
            continue
        for name, raw_config in raw_servers.items():
            if not isinstance(raw_config, Mapping):
                continue
            config = _coerce_server_config(raw_config, cwd=cwd)
            if config is None:
                logger.debug("Skipping invalid LSP server config: %s", name)
                continue
            servers[str(name)] = config
    return servers
