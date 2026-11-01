"""OpenClaw host-agent config reader.

Reads ``~/.openclaw/openclaw.json`` to auto-detect:
  - LLM provider credentials from env-style config blocks
    (``skills.entries.openspace.env`` and ``env.vars``)
  - Skill-level env block (``skills.entries.openspace.env``)
  - OpenAI API key for embedding generation

Config path resolution mirrors OpenClaw's own logic:
  1. ``OPENCLAW_CONFIG_PATH`` env var
  2. ``OPENCLAW_STATE_DIR/openclaw.json``
  3. ``~/.openclaw/openclaw.json`` (default)

Fallback legacy dirs: ``~/.clawdbot``, ``~/.moldbot``, ``~/.moltbot``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from openspace.host_detection.nanobot import PROVIDER_REGISTRY

logger = logging.getLogger("openspace.host_detection")

_STATE_DIRNAMES = [".openclaw", ".clawdbot", ".moldbot", ".moltbot"]
_CONFIG_FILENAMES = ["openclaw.json", "clawdbot.json", "moldbot.json", "moltbot.json"]
_PROVIDER_ENV_VARS: Dict[str, Dict[str, tuple[str, ...]]] = {
    "openrouter": {
        "api_key": ("OPENROUTER_API_KEY", "OR_API_KEY"),
        "api_base": ("OPENROUTER_API_BASE",),
    },
    "aihubmix": {
        "api_key": ("AIHUBMIX_API_KEY",),
        "api_base": ("AIHUBMIX_API_BASE",),
    },
    "siliconflow": {
        "api_key": ("SILICONFLOW_API_KEY",),
        "api_base": ("SILICONFLOW_API_BASE",),
    },
    "volcengine": {
        "api_key": ("VOLCENGINE_API_KEY", "ARK_API_KEY"),
        "api_base": ("VOLCENGINE_API_BASE", "ARK_API_BASE"),
    },
    "anthropic": {
        "api_key": ("ANTHROPIC_API_KEY",),
        "api_base": ("ANTHROPIC_API_BASE",),
    },
    "openai": {
        "api_key": ("OPENAI_API_KEY",),
        "api_base": ("OPENAI_BASE_URL", "OPENAI_API_BASE"),
    },
    "deepseek": {
        "api_key": ("DEEPSEEK_API_KEY",),
        "api_base": ("DEEPSEEK_API_BASE",),
    },
    "gemini": {
        "api_key": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "api_base": ("GEMINI_API_BASE", "GOOGLE_API_BASE"),
    },
    "zhipu": {
        "api_key": ("ZHIPU_API_KEY",),
        "api_base": ("ZHIPU_API_BASE",),
    },
    "dashscope": {
        "api_key": ("DASHSCOPE_API_KEY",),
        "api_base": ("DASHSCOPE_API_BASE",),
    },
    "moonshot": {
        "api_key": ("MOONSHOT_API_KEY",),
        "api_base": ("MOONSHOT_API_BASE",),
    },
    "minimax": {
        "api_key": ("MINIMAX_API_KEY",),
        "api_base": ("MINIMAX_API_BASE",),
    },
    "groq": {
        "api_key": ("GROQ_API_KEY",),
        "api_base": ("GROQ_API_BASE",),
    },
}


def _resolve_openclaw_config_path() -> Optional[Path]:
    """Find the OpenClaw config file on disk."""
    explicit = os.environ.get("OPENCLAW_CONFIG_PATH", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p
        return None

    state_dir = os.environ.get("OPENCLAW_STATE_DIR", "").strip()
    if state_dir:
        for fname in _CONFIG_FILENAMES:
            p = Path(state_dir) / fname
            if p.is_file():
                return p

    home = Path.home()
    for dirname in _STATE_DIRNAMES:
        for fname in _CONFIG_FILENAMES:
            p = home / dirname / fname
            if p.is_file():
                return p

    return None


def _load_openclaw_config() -> Optional[Dict[str, Any]]:
    """Load and parse the OpenClaw config file.  Returns None on failure."""
    config_path = _resolve_openclaw_config_path()
    if config_path is None:
        return None
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read OpenClaw config %s: %s", config_path, e)
        return None


def _coerce_env_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _pick_env(env_block: Dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = _coerce_env_value(env_block.get(name))
        if value:
            return value
    return ""


def _get_openclaw_env(skill_name: str = "openspace") -> Dict[str, Any]:
    """Merge OpenClaw top-level env vars with skill-level env overrides."""
    merged: Dict[str, Any] = {}
    data = _load_openclaw_config()
    if data and isinstance(data, dict):
        env_section = data.get("env", {})
        if isinstance(env_section, dict):
            vars_block = env_section.get("vars", {})
            if isinstance(vars_block, dict):
                merged.update(vars_block)
    merged.update(read_openclaw_skill_env(skill_name))
    return merged


def _extract_explicit_llm_kwargs(env_block: Dict[str, Any]) -> Dict[str, Any]:
    """Read OpenSpace-native LLM overrides from an env-like dict."""
    result: Dict[str, Any] = {}

    api_key = _coerce_env_value(env_block.get("OPENSPACE_LLM_API_KEY"))
    if api_key:
        result["api_key"] = api_key

    api_base = _coerce_env_value(env_block.get("OPENSPACE_LLM_API_BASE"))
    if api_base:
        result["api_base"] = api_base

    extra_headers_raw = _coerce_env_value(env_block.get("OPENSPACE_LLM_EXTRA_HEADERS"))
    if extra_headers_raw:
        try:
            headers = json.loads(extra_headers_raw)
            if isinstance(headers, dict):
                result["extra_headers"] = headers
        except json.JSONDecodeError:
            logger.warning(
                "Invalid JSON in OpenClaw OPENSPACE_LLM_EXTRA_HEADERS: %r",
                extra_headers_raw,
            )

    llm_config_raw = _coerce_env_value(env_block.get("OPENSPACE_LLM_CONFIG"))
    if llm_config_raw:
        try:
            llm_config = json.loads(llm_config_raw)
            if isinstance(llm_config, dict):
                result.update(llm_config)
        except json.JSONDecodeError:
            logger.warning(
                "Invalid JSON in OpenClaw OPENSPACE_LLM_CONFIG: %r",
                llm_config_raw,
            )

    return result


def _extract_provider_env(
    env_block: Dict[str, Any],
    provider: str,
    default_base: str = "",
) -> Optional[Dict[str, Any]]:
    spec = _PROVIDER_ENV_VARS.get(provider)
    if not spec:
        return None

    api_key = _pick_env(env_block, spec["api_key"])
    if not api_key:
        return None

    result: Dict[str, Any] = {"api_key": api_key}
    api_base = _pick_env(env_block, spec.get("api_base", ())) or default_base
    if api_base:
        result["api_base"] = api_base
    return result


def _match_provider_env(model: str, env_block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve provider-native env vars from OpenClaw config for a model."""
    model_lower = model.lower()
    model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
    normalized_prefix = model_prefix.replace("-", "_")

    for name, _keywords, default_base in PROVIDER_REGISTRY:
        if model_prefix and normalized_prefix == name:
            result = _extract_provider_env(env_block, name, default_base)
            if result:
                return result

    for name, keywords, default_base in PROVIDER_REGISTRY:
        if any(keyword in model_lower for keyword in keywords):
            result = _extract_provider_env(env_block, name, default_base)
            if result:
                return result

    for name, _keywords, default_base in PROVIDER_REGISTRY:
        result = _extract_provider_env(env_block, name, default_base)
        if result:
            return result

    return None


def read_openclaw_skill_env(skill_name: str = "openspace") -> Dict[str, str]:
    """Read ``skills.entries.<skill_name>.env`` from OpenClaw config.

    This is the OpenClaw equivalent of nanobot's
    ``tools.mcpServers.openspace.env``.

    Returns the env dict (empty if not found / parse error).
    """
    data = _load_openclaw_config()
    if data is None:
        return {}

    skills = data.get("skills", {})
    if not isinstance(skills, dict):
        return {}
    entries = skills.get("entries", {})
    if not isinstance(entries, dict):
        return {}
    skill_cfg = entries.get(skill_name, {})
    if not isinstance(skill_cfg, dict):
        return {}
    env_block = skill_cfg.get("env", {})
    return env_block if isinstance(env_block, dict) else {}


def get_openclaw_openai_api_key() -> Optional[str]:
    """Get OpenAI API key from OpenClaw config.

    Checks ``skills.entries.openspace.env.OPENAI_API_KEY`` first,
    then any top-level env vars in the config.

    Returns the key string, or None.
    """
    env = _get_openclaw_env("openspace")
    key = _coerce_env_value(env.get("OPENAI_API_KEY"))
    if key:
        logger.debug("Using OpenAI API key from OpenClaw skill env config")
        return key

    return None


def is_openclaw_host() -> bool:
    """Detect if the current environment is running under OpenClaw."""
    if os.environ.get("OPENCLAW_STATE_DIR") or os.environ.get("OPENCLAW_CONFIG_PATH"):
        return True
    return _resolve_openclaw_config_path() is not None


def try_read_openclaw_config(model: str) -> Optional[Dict[str, Any]]:
    """Read LLM credentials from OpenClaw's env-style config blocks."""
    env_block = _get_openclaw_env("openspace")
    if not env_block:
        return None

    explicit_kwargs = _extract_explicit_llm_kwargs(env_block)
    provider_kwargs = _match_provider_env(model or "", env_block)

    if not explicit_kwargs and not provider_kwargs:
        return None

    result: Dict[str, Any] = {}
    if provider_kwargs:
        result.update(provider_kwargs)
    if explicit_kwargs:
        result.update(explicit_kwargs)

    config_path = _resolve_openclaw_config_path()
    logger.info(
        "Auto-detected LLM credentials from OpenClaw config (%s), provider matched for model=%r",
        config_path,
        model,
    )
    return result


