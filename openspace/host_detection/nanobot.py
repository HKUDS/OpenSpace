"""Nanobot host-agent config reader.

Reads ``~/.nanobot/config.json`` to auto-detect:
  - LLM provider credentials (``providers.*``)
  - MCP env block for the ``openspace`` server
  - Default model and forced provider settings

Provider keyword → config field mapping mirrors ``nanobot/providers/registry.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("openspace.host_detection")

PROVIDER_REGISTRY: List[tuple] = [
    # Gateways
    ("openrouter",  ("openrouter",),                  "https://openrouter.ai/api/v1"),
    ("aihubmix",    ("aihubmix",),                    "https://aihubmix.com/v1"),
    ("siliconflow", ("siliconflow",),                 "https://api.siliconflow.cn/v1"),
    ("volcengine",  ("volcengine", "volces", "ark"),  "https://ark.cn-beijing.volces.com/api/v3"),
    # Standard providers
    ("anthropic",   ("anthropic", "claude"),           ""),
    ("openai",      ("openai", "gpt"),                 ""),
    ("deepseek",    ("deepseek",),                     ""),
    ("gemini",      ("gemini",),                       ""),
    ("zhipu",       ("zhipu", "glm", "zai"),           ""),
    ("dashscope",   ("qwen", "dashscope"),             ""),
    ("moonshot",    ("moonshot", "kimi"),               "https://api.moonshot.ai/v1"),
    ("minimax",     ("minimax",),                      "https://api.minimaxi.com/v1"),
    ("groq",        ("groq",),                         ""),
]

NANOBOT_CONFIG_PATH = Path.home() / ".nanobot" / "config.json"


def _load_nanobot_config() -> Optional[Dict[str, Any]]:
    """Load and parse ``~/.nanobot/config.json``.  Returns None on failure."""
    if not NANOBOT_CONFIG_PATH.is_file():
        return None
    try:
        with open(NANOBOT_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read nanobot config %s: %s", NANOBOT_CONFIG_PATH, e)
        return None


def match_provider(
    providers: Dict[str, Any],
    model: str,
    forced_provider: str = "auto",
) -> Optional[Dict[str, Any]]:
    """Match a provider config dict from nanobot's ``providers`` section.

    Resolution order:
      1. ``forced_provider`` (if not "auto") → use that config field directly.
      2. Prefix match: model's first ``/``-separated segment == config field name.
      3. Keyword match: iterate ``PROVIDER_REGISTRY`` by priority.
      4. Fallback: first provider with a non-empty ``apiKey``.

    Returns:
        ``{"api_key": ..., "api_base": ..., "extra_headers": ...}``
        (litellm-compatible), or None.
    """
    def _extract(prov_dict: Dict[str, Any], default_base: str = "") -> Optional[Dict[str, Any]]:
        api_key = prov_dict.get("apiKey") or prov_dict.get("api_key") or ""
        if not api_key:
            return None
        result: Dict[str, Any] = {"api_key": api_key}
        api_base = prov_dict.get("apiBase") or prov_dict.get("api_base") or default_base
        if api_base:
            result["api_base"] = api_base
        extra = prov_dict.get("extraHeaders") or prov_dict.get("extra_headers")
        if extra and isinstance(extra, dict):
            result["extra_headers"] = extra
        return result

    model_lower = model.lower()
    model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
    normalized_prefix = model_prefix.replace("-", "_")

    # 1. Forced provider
    if forced_provider and forced_provider != "auto":
        p = providers.get(forced_provider)
        if p and isinstance(p, dict):
            # Look up the default api_base for this provider from the registry
            forced_default_base = ""
            for _name, _kws, _base in PROVIDER_REGISTRY:
                if _name == forced_provider:
                    forced_default_base = _base
                    break
            return _extract(p, forced_default_base)

    # 2. Prefix match
    for name, _kws, default_base in PROVIDER_REGISTRY:
        if model_prefix and normalized_prefix == name:
            p = providers.get(name)
            if p and isinstance(p, dict):
                result = _extract(p, default_base)
                if result:
                    return result

    # 3. Keyword match
    for name, keywords, default_base in PROVIDER_REGISTRY:
        if any(kw in model_lower for kw in keywords):
            p = providers.get(name)
            if p and isinstance(p, dict):
                result = _extract(p, default_base)
                if result:
                    return result

    # 4. Fallback: first provider with an api_key
    for name, _kws, default_base in PROVIDER_REGISTRY:
        p = providers.get(name)
        if p and isinstance(p, dict):
            result = _extract(p, default_base)
            if result:
                return result

    return None


def try_read_nanobot_config(model: str) -> Optional[Dict[str, Any]]:
    """Read LLM credentials from ``~/.nanobot/config.json``.

    Returns litellm kwargs dict (``api_key``, ``api_base``, ``extra_headers``),
    or None.  May include a ``"_model"`` key with the nanobot default model.
    """
    data = _load_nanobot_config()
    if data is None:
        return None

    providers = data.get("providers", {})
    if not isinstance(providers, dict):
        return None

    agents = data.get("agents", {})
    defaults = agents.get("defaults", {}) if isinstance(agents, dict) else {}
    nanobot_model = defaults.get("model", "") if isinstance(defaults, dict) else ""
    forced_provider = defaults.get("provider", "auto") if isinstance(defaults, dict) else "auto"

    match_model = model or nanobot_model or ""
    result = match_provider(providers, match_model, forced_provider)

    if result and nanobot_model:
        result["_model"] = nanobot_model
    if result and forced_provider and forced_provider != "auto":
        result["_forced_provider"] = forced_provider

    if result:
        logger.info(
            "Auto-detected LLM credentials from nanobot config (%s), "
            "provider matched for model=%r",
            NANOBOT_CONFIG_PATH, match_model,
        )

    return result


def read_nanobot_mcp_env() -> Dict[str, str]:
    """Read ``tools.mcpServers.openspace.env`` from nanobot config.

    Returns the env dict (empty if not found / parse error).
    """
    data = _load_nanobot_config()
    if data is None:
        return {}

    tools = data.get("tools", {})
    if not isinstance(tools, dict):
        return {}
    mcp_servers = tools.get("mcpServers") or tools.get("mcp_servers") or {}
    if not isinstance(mcp_servers, dict):
        return {}
    openspace_cfg = mcp_servers.get("openspace", {})
    if not isinstance(openspace_cfg, dict):
        return {}
    env_block = openspace_cfg.get("env", {})
    return env_block if isinstance(env_block, dict) else {}


def get_openai_api_key() -> Optional[str]:
    """Get OpenAI API key for embedding generation.

    Resolution:
      1. ``OPENAI_API_KEY`` env var
      2. nanobot config ``providers.openai.apiKey``
      3. None
    """
    import os
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key

    data = _load_nanobot_config()
    if data:
        providers = data.get("providers", {})
        if isinstance(providers, dict):
            openai_cfg = providers.get("openai", {})
            if isinstance(openai_cfg, dict):
                api_key = openai_cfg.get("apiKey")
                if api_key:
                    logger.debug("Using OpenAI API key from nanobot config for embeddings")
                    return api_key
    return None

