"""
Agent configuration generation and validation utilities.
"""
from __future__ import annotations

import json
import re
from typing import Any

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

VALID_AGENT_TYPES = {"grounding", "conversational", "code", "research", "custom"}
MAX_DESCRIPTION_LENGTH = 500

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_DEFAULT_TEMPLATE: dict[str, Any] = {
    "name": "new_agent",
    "type": "custom",
    "description": "",
    "tools": [],
    "system_prompt": "",
    "max_iterations": 10,
}

_GENERATE_PROMPT = (
    "Generate a JSON agent configuration based on the following description.\n"
    "Return ONLY valid JSON with these keys: "
    "name (str), type (one of {types}), description (str, ≤{max_len} chars), "
    "tools (list[str]), system_prompt (str), max_iterations (int).\n\n"
    "Description: {description}"
)


class AgentValidationError(Exception):
    """Raised when an agent configuration fails validation."""


async def generate(
    description: str,
    llm_client: Any = None,
) -> dict[str, Any]:
    """Generate an agent configuration dict from a natural-language description.

    If *llm_client* is provided, calls it to produce the config; otherwise
    returns a basic template populated with *description*.
    """
    if llm_client is not None:
        return await _generate_via_llm(description, llm_client)
    return _generate_template(description)


def validate(agent_config: dict[str, Any]) -> list[str]:
    """Validate *agent_config* and return a list of error strings (empty = valid)."""
    errors: list[str] = []

    for field in ("name", "type", "description"):
        if field not in agent_config:
            errors.append(f"Missing required field: {field}")

    name = agent_config.get("name", "")
    if isinstance(name, str):
        if not name:
            errors.append("Field 'name' must not be empty")
        elif not _NAME_RE.match(name):
            errors.append(
                "Field 'name' may only contain alphanumeric characters, "
                "underscores, and hyphens"
            )

    agent_type = agent_config.get("type")
    if agent_type is not None and agent_type not in VALID_AGENT_TYPES:
        errors.append(
            f"Invalid agent type {agent_type!r}; "
            f"must be one of {sorted(VALID_AGENT_TYPES)}"
        )

    desc = agent_config.get("description", "")
    if isinstance(desc, str) and len(desc) > MAX_DESCRIPTION_LENGTH:
        errors.append(
            f"Description too long ({len(desc)} chars); "
            f"maximum is {MAX_DESCRIPTION_LENGTH}"
        )

    tools = agent_config.get("tools")
    if tools is not None and not isinstance(tools, list):
        errors.append("Field 'tools' must be a list")

    return errors


# ── Internal helpers ──────────────────────────────────────────────


def _generate_template(description: str) -> dict[str, Any]:
    config = dict(_DEFAULT_TEMPLATE)
    config["description"] = description[:MAX_DESCRIPTION_LENGTH]
    slug = re.sub(r"[^a-z0-9]+", "_", description.lower())[:40].strip("_")
    config["name"] = slug or "new_agent"
    return config


async def _generate_via_llm(
    description: str,
    llm_client: Any,
) -> dict[str, Any]:
    prompt = _GENERATE_PROMPT.format(
        types=", ".join(sorted(VALID_AGENT_TYPES)),
        max_len=MAX_DESCRIPTION_LENGTH,
        description=description,
    )

    try:
        call_model = getattr(
            llm_client,
            "call_model_with_fallback",
            llm_client.call_model,
        )
        response = await call_model(
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.assistant_message.get("content", "")
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError("No JSON object found in LLM response")
        config = json.loads(text[start:end])
    except Exception:
        logger.warning("LLM generation failed, falling back to template")
        return _generate_template(description)

    errors = validate(config)
    if errors:
        logger.warning("LLM-generated config has issues: %s", errors)
        template = _generate_template(description)
        for key in ("name", "type", "tools", "system_prompt", "max_iterations"):
            if key not in config or key in [
                e.split("'")[1] for e in errors if "'" in e
            ]:
                config[key] = template[key]

    return config
