"""LLM credential and grounding config resolution.

Resolves the model name and litellm kwargs for OpenSpace's LLM client,
and assembles grounding config from env-var overrides.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Dict, Optional

logger = logging.getLogger("openspace.host_detection")


def build_llm_kwargs(model: str) -> tuple[str, Dict[str, Any]]:
    """Build litellm kwargs and resolve model for OpenSpace's LLM client.

    Resolution order (highest → lowest priority):

    Tier 1 — Explicit ``OPENSPACE_LLM_*`` env vars::

        OPENSPACE_LLM_API_KEY         → litellm ``api_key``
        OPENSPACE_LLM_API_BASE        → litellm ``api_base``
        OPENSPACE_LLM_EXTRA_HEADERS   → litellm ``extra_headers`` (JSON string)
        OPENSPACE_LLM_CONFIG          → arbitrary litellm kwargs (JSON string)

    Tier 2 — Auto-detect from host agent config file::

        ~/.nanobot/config.json      → providers.{matched}.apiKey / apiBase

    Tier 3 — Provider-native env vars inherited from the parent process
    (e.g. ``OPENROUTER_API_KEY``).  Read by litellm automatically.

    Returns:
        ``(resolved_model, llm_kwargs_dict)``
    """
    from openspace.host_detection.nanobot import try_read_nanobot_config

    kwargs: Dict[str, Any] = {}
    resolved_model = model
    source = "inherited env"

    # --- Tier 2: auto-detect from host config (filled first, may be overridden) ---
    host_config = try_read_nanobot_config(model)
    if host_config:
        host_model = host_config.pop("_model", None)
        forced_provider = host_config.pop("_forced_provider", None)
        if not resolved_model and host_model:
            resolved_model = host_model
        # If the host config forces a gateway provider (e.g. openrouter)
        # and the model name doesn't already carry that prefix, prepend
        # it so that litellm uses the correct request format (OpenAI-
        # compatible for gateways vs native for direct providers).
        # Skip when the user explicitly provided a model (via OPENSPACE_MODEL
        # or --model) AND explicit OPENSPACE_LLM_* overrides — the user knows
        # exactly which endpoint they want to hit.
        _GATEWAY_PROVIDERS = {"openrouter", "aihubmix", "siliconflow"}
        _has_explicit_llm_override = bool(
            os.environ.get("OPENSPACE_LLM_API_BASE")
            or os.environ.get("OPENSPACE_LLM_API_KEY")
        )
        if (
            forced_provider
            and forced_provider in _GATEWAY_PROVIDERS
            and resolved_model
            and not resolved_model.lower().startswith(f"{forced_provider}/")
            and not (model and _has_explicit_llm_override)
        ):
            resolved_model = f"{forced_provider}/{resolved_model}"
            logger.info(
                "Prepended gateway prefix: model=%r (forced_provider=%s)",
                resolved_model, forced_provider,
            )
        kwargs.update(host_config)
        source = "nanobot config"

    # --- Tier 1: explicit env vars override everything ---
    api_key = os.environ.get("OPENSPACE_LLM_API_KEY")
    if api_key:
        kwargs["api_key"] = api_key
        source = "OPENSPACE_LLM_* env"

    api_base = os.environ.get("OPENSPACE_LLM_API_BASE")
    if api_base:
        kwargs["api_base"] = api_base

    extra_headers_raw = os.environ.get("OPENSPACE_LLM_EXTRA_HEADERS")
    if extra_headers_raw:
        try:
            headers = json.loads(extra_headers_raw)
            if isinstance(headers, dict):
                kwargs["extra_headers"] = headers
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in OPENSPACE_LLM_EXTRA_HEADERS: %r", extra_headers_raw)

    llm_config_raw = os.environ.get("OPENSPACE_LLM_CONFIG")
    if llm_config_raw:
        try:
            llm_config = json.loads(llm_config_raw)
            if isinstance(llm_config, dict):
                kwargs.update(llm_config)
                source = "OPENSPACE_LLM_CONFIG env"
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in OPENSPACE_LLM_CONFIG: %r", llm_config_raw)

    # Default model fallback
    if not resolved_model:
        resolved_model = "openrouter/anthropic/claude-sonnet-4.5"

    # Provider-specific adjustments for litellm routing
    if resolved_model and "minimax" in resolved_model.lower():
        final_key = kwargs.get("api_key")
        final_base = kwargs.get("api_base", "")

        if final_key:
            os.environ.setdefault("MINIMAX_API_KEY", final_key)
        if final_base:
            os.environ.setdefault("MINIMAX_API_BASE", final_base)

        # api.minimaxi.com (domestic) is OpenAI-compatible but does not fully
        # support litellm's minimax-specific request transformations (e.g. tool
        # calling format).  Switch to the generic openai/ prefix so litellm
        # sends standard OpenAI-format requests that minimaxi.com accepts.
        if (
            resolved_model.lower().startswith("minimax/")
            and "minimaxi.com" in final_base
        ):
            original = resolved_model
            resolved_model = "openai/" + resolved_model.split("/", 1)[1]
            logger.info(
                "Switched model prefix for minimaxi.com compat: %s -> %s",
                original, resolved_model,
            )

    if kwargs:
        safe = {
            k: (v[:8] + "..." if k == "api_key" and isinstance(v, str) and len(v) > 8 else v)
            for k, v in kwargs.items()
        }
        logger.info("LLM kwargs resolved (source=%s): %s", source, safe)

    return resolved_model, kwargs


def build_grounding_config_path() -> Optional[str]:
    """Resolve grounding config: inline JSON > file path > None.

    Supports:
      * ``OPENSPACE_CONFIG_JSON``  — inline JSON string (written to a temp file)
      * ``OPENSPACE_CONFIG_PATH``  — path to a JSON config file

    Granular env-var overrides (``OPENSPACE_SHELL_*``, ``OPENSPACE_SKILLS_*``,
    etc.) are merged before writing.

    Returns:
        Path to the resolved config file, or None.
    """
    config_json_raw = os.environ.get("OPENSPACE_CONFIG_JSON", "").strip()
    overrides: Dict[str, Any] = {}
    if config_json_raw:
        try:
            overrides = json.loads(config_json_raw)
            if not isinstance(overrides, dict):
                logger.warning("OPENSPACE_CONFIG_JSON is not a dict, ignoring")
                overrides = {}
            else:
                logger.info("Loaded inline config from OPENSPACE_CONFIG_JSON")
        except json.JSONDecodeError as e:
            logger.warning("Invalid JSON in OPENSPACE_CONFIG_JSON: %s", e)

    # --- Granular env-var overrides ---
    conda_env = os.environ.get("OPENSPACE_SHELL_CONDA_ENV", "").strip()
    if conda_env:
        overrides.setdefault("shell", {})["conda_env"] = conda_env

    shell_wd = os.environ.get("OPENSPACE_SHELL_WORKING_DIR", "").strip()
    if shell_wd:
        overrides.setdefault("shell", {})["working_dir"] = shell_wd

    skills_dirs_raw = os.environ.get("OPENSPACE_SKILLS_DIRS", "").strip()
    if skills_dirs_raw:
        dirs = [d.strip() for d in skills_dirs_raw.split(",") if d.strip()]
        if dirs:
            overrides.setdefault("skills", {})["skill_dirs"] = dirs

    mcp_servers_raw = os.environ.get("OPENSPACE_MCP_SERVERS_JSON", "").strip()
    if mcp_servers_raw:
        try:
            servers = json.loads(mcp_servers_raw)
            if isinstance(servers, dict):
                overrides["mcpServers"] = servers
        except json.JSONDecodeError as e:
            logger.warning("Invalid JSON in OPENSPACE_MCP_SERVERS_JSON: %s", e)

    log_level = os.environ.get("OPENSPACE_LOG_LEVEL", "").strip().upper()
    if log_level and log_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        overrides["log_level"] = log_level

    if overrides:
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="openspace_cfg_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(overrides, f, ensure_ascii=False)
            logger.info(
                "Grounding config overrides written to %s (%d keys)",
                tmp_path, len(overrides),
            )
            return tmp_path
        except Exception as e:
            logger.warning("Failed to write config overrides: %s", e)

    return os.environ.get("OPENSPACE_CONFIG_PATH")

