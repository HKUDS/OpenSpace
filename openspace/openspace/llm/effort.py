"""Effort level parsing, model capability checks, and API parameter mapping.

- string levels are ``low | medium | high | max``;
- ``auto`` / ``unset`` intentionally means "send no explicit effort";
- ``max`` is downgraded to ``high`` when the selected model does not support it;
- numeric effort is treated as an internal/session-local override only.

Provider-specific wire mapping is isolated in ``build_effort_request_params``
instead of leaking request fields through the rest of the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from typing import Any, Literal


class EffortLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


EFFORT_LEVELS: tuple[str, ...] = tuple(level.value for level in EffortLevel)
EffortValue = EffortLevel | int
EffortEnvState = Literal["absent", "auto", "value", "invalid"]

EFFORT_BUDGET_FRACTIONS: dict[EffortLevel, float] = {
    EffortLevel.LOW: 0.15,
    EffortLevel.MEDIUM: 0.35,
    EffortLevel.HIGH: 0.70,
    EffortLevel.MAX: 1.00,
}


@dataclass(frozen=True, slots=True)
class EffortConfig:
    level: EffortLevel
    api_effort: EffortLevel | None = None
    thinking_budget_tokens: int | None = None
    source: str = "default"
    applied_value: EffortValue | None = None


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _canonical_model(model: str) -> str:
    return str(model or "").strip().lower().replace(".", "-")


def _model_tokens(model: str) -> set[str]:
    lowered = _canonical_model(model)
    tokens = {lowered}
    if "/" in lowered:
        tokens.add(lowered.rsplit("/", 1)[-1])
    return tokens


def _csv_items(env_name: str) -> list[str]:
    raw = os.environ.get(env_name, "")
    return [item.strip().lower().replace(".", "-") for item in raw.split(",") if item.strip()]


def _env_matches(model: str, env_name: str) -> bool:
    tokens = _model_tokens(model)
    lowered = _canonical_model(model)
    return any(item in tokens or item in lowered for item in _csv_items(env_name))


def _is_openai_reasoning_model(model: str) -> bool:
    m = _canonical_model(model).rsplit("/", 1)[-1]
    return (
        m.startswith("o1")
        or m.startswith("o3")
        or m.startswith("o4")
        or m.startswith("gpt-5")
    )


def _is_anthropic_effort_model(model: str) -> bool:
    m = _canonical_model(model)
    return "opus-4-6" in m or "sonnet-4-6" in m


def _numeric_effort_enabled() -> bool:
    return os.environ.get("USER_TYPE") == "ant" or _is_truthy(
        os.environ.get("OPENSPACE_ALLOW_NUMERIC_EFFORT")
    )


def is_effort_level(value: str) -> bool:
    return str(value).strip().lower() in EFFORT_LEVELS


def is_valid_numeric_effort(value: int | float) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_effort_value(value: object) -> EffortValue | None:
    if value is None:
        return None
    if isinstance(value, EffortLevel):
        return value
    if is_valid_numeric_effort(value):  # type: ignore[arg-type]
        return int(value)  # type: ignore[arg-type]

    text = str(value).strip().lower()
    if text in {"", "auto", "unset", "none"}:
        return None
    if is_effort_level(text):
        return EffortLevel(text)
    try:
        numeric = int(text, 10)
    except (TypeError, ValueError):
        return None
    return numeric if is_valid_numeric_effort(numeric) else None


def to_persistable_effort(value: EffortValue | str | None) -> EffortLevel | None:
    parsed = parse_effort_value(value)
    if parsed in {EffortLevel.LOW, EffortLevel.MEDIUM, EffortLevel.HIGH}:
        return parsed  # type: ignore[return-value]
    if parsed == EffortLevel.MAX and _numeric_effort_enabled():
        return EffortLevel.MAX
    return None


def resolve_picker_effort_persistence(
    picked: EffortLevel | str | None,
    model_default: EffortLevel | str,
    prior_persisted: EffortLevel | str | None,
    toggled_in_picker: bool,
) -> EffortLevel | None:
    picked_level = parse_effort_value(picked)
    default_level = convert_effort_value_to_level(parse_effort_value(model_default) or EffortLevel.HIGH)
    prior_level = to_persistable_effort(prior_persisted)
    had_explicit = prior_level is not None or toggled_in_picker
    if picked_level is None:
        return None
    picked_display = convert_effort_value_to_level(picked_level)
    if had_explicit or picked_display != default_level:
        return to_persistable_effort(picked_display)
    return None


def _raw_effort_env() -> str | None:
    return os.environ.get("OPENSPACE_EFFORT_LEVEL")


def get_effort_env_override() -> EffortValue | None:
    state, value = _get_effort_env_state()
    return value if state == "value" else None


def get_effort_env_state() -> tuple[EffortEnvState, EffortValue | None, str | None]:
    state, value = _get_effort_env_state()
    return state, value, _raw_effort_env()


def _get_effort_env_state() -> tuple[EffortEnvState, EffortValue | None]:
    raw = _raw_effort_env()
    if raw is None:
        return "absent", None
    normalized = raw.strip().lower()
    if normalized in {"unset", "auto"}:
        return "auto", None
    parsed = parse_effort_value(normalized)
    if parsed is None:
        return "invalid", None
    return "value", parsed


def model_supports_effort(model: str) -> bool:
    if _is_truthy(os.environ.get("OPENSPACE_ALWAYS_ENABLE_EFFORT")):
        return True
    if _env_matches(model, "OPENSPACE_NO_EFFORT_MODELS"):
        return False
    if _env_matches(model, "OPENSPACE_EFFORT_MODELS"):
        return True
    if _is_anthropic_effort_model(model):
        return True
    if _is_openai_reasoning_model(model):
        return True
    return False


def model_supports_max_effort(model: str) -> bool:
    if _env_matches(model, "OPENSPACE_MAX_EFFORT_MODELS"):
        return True
    if _env_matches(model, "OPENSPACE_NO_MAX_EFFORT_MODELS"):
        return False
    return "opus-4-6" in _canonical_model(model)


def get_default_effort_for_model(model: str) -> EffortLevel | None:
    env_default = os.environ.get("OPENSPACE_DEFAULT_EFFORT_LEVEL")
    if env_default is not None:
        if env_default.strip().lower() in {"", "auto", "unset", "none"}:
            return None
        parsed = parse_effort_value(env_default)
        if parsed is not None:
            level = convert_effort_value_to_level(parsed)
            if level == EffortLevel.MAX and not model_supports_max_effort(model):
                return EffortLevel.HIGH
            return level

    # Keep a conservative product recommendation for Opus 4.6 and avoid adding
    # defaults for other effort-capable models.
    if "opus-4-6" in _canonical_model(model):
        return EffortLevel.MEDIUM
    return None


def resolve_applied_effort(
    model: str,
    requested: str | int | EffortLevel | None,
) -> EffortValue | None:
    env_state, env_value = _get_effort_env_state()
    if env_state == "auto":
        return None
    if env_state == "value":
        resolved: EffortValue | None = env_value
    else:
        resolved = parse_effort_value(requested) or get_default_effort_for_model(model)

    if resolved is None:
        return None
    if isinstance(resolved, int) and not _numeric_effort_enabled():
        return EffortLevel.HIGH
    if resolved == EffortLevel.MAX and not model_supports_max_effort(model):
        return EffortLevel.HIGH
    return resolved


def convert_effort_value_to_level(value: EffortValue | str) -> EffortLevel:
    parsed = parse_effort_value(value)
    if isinstance(parsed, EffortLevel):
        return parsed
    if isinstance(parsed, int) and _numeric_effort_enabled():
        if parsed <= 50:
            return EffortLevel.LOW
        if parsed <= 85:
            return EffortLevel.MEDIUM
        if parsed <= 100:
            return EffortLevel.HIGH
        return EffortLevel.MAX
    return EffortLevel.HIGH


def get_displayed_effort_level(
    model: str,
    requested: str | int | EffortLevel | None,
) -> EffortLevel:
    return convert_effort_value_to_level(
        resolve_applied_effort(model, requested) or EffortLevel.HIGH
    )


def get_effort_suffix(
    model: str,
    effort_value: str | int | EffortLevel | None,
) -> str:
    if parse_effort_value(effort_value) is None:
        return ""
    resolved = resolve_applied_effort(model, effort_value)
    if resolved is None:
        return ""
    return f" with {convert_effort_value_to_level(resolved).value} effort"


def get_effort_level_description(level: EffortLevel | str) -> str:
    parsed = convert_effort_value_to_level(level)
    if parsed == EffortLevel.LOW:
        return "Quick, straightforward implementation with minimal overhead"
    if parsed == EffortLevel.MEDIUM:
        return "Balanced approach with standard implementation and testing"
    if parsed == EffortLevel.HIGH:
        return "Comprehensive implementation with extensive testing and documentation"
    return "Maximum capability with deepest reasoning (Opus 4.6 only)"


def get_effort_value_description(value: EffortValue | str) -> str:
    parsed = parse_effort_value(value)
    if isinstance(parsed, int) and _numeric_effort_enabled():
        return f"[INTERNAL] Numeric effort value of {parsed}"
    if parsed is not None:
        return get_effort_level_description(convert_effort_value_to_level(parsed))
    return get_effort_level_description(EffortLevel.MEDIUM)


def _round_down_to_multiple(value: int, multiple: int = 256) -> int:
    if value <= 0:
        return 0
    return max(multiple, (value // multiple) * multiple)


def effort_to_thinking_budget(level: str | int | EffortLevel | None, model: str) -> int:
    parsed = parse_effort_value(level)
    if isinstance(parsed, int) and parsed > 0 and _numeric_effort_enabled():
        return int(parsed)
    effort_level = convert_effort_value_to_level(parsed or EffortLevel.MEDIUM)
    fraction = EFFORT_BUDGET_FRACTIONS[effort_level]
    from .thinking import get_max_thinking_tokens_for_model

    return _round_down_to_multiple(
        int(get_max_thinking_tokens_for_model(model) * fraction)
    )


def get_effort_config(
    model: str,
    level: str | int | EffortLevel | None,
) -> EffortConfig:
    applied = resolve_applied_effort(model, level)
    if applied is None:
        return EffortConfig(
            level=get_displayed_effort_level(model, level),
            source="auto",
            applied_value=None,
        )

    display_level = convert_effort_value_to_level(applied)
    source = "env" if _get_effort_env_state()[0] == "value" else "explicit"
    if parse_effort_value(level) is None and _get_effort_env_state()[0] != "value":
        source = "model_default"

    if model_supports_effort(model):
        return EffortConfig(
            level=display_level,
            api_effort=display_level,
            source=source,
            applied_value=applied,
        )

    from .thinking import supports_thinking

    if supports_thinking(model):
        return EffortConfig(
            level=display_level,
            thinking_budget_tokens=effort_to_thinking_budget(applied, model),
            source=source,
            applied_value=applied,
        )

    return EffortConfig(level=display_level, source=source, applied_value=applied)


def build_effort_request_params(config: EffortConfig, model: str) -> dict[str, Any]:
    if config.api_effort is None:
        return {}
    effort = config.api_effort.value
    if _is_openai_reasoning_model(model):
        return {"reasoning_effort": effort}
    if _is_anthropic_effort_model(model):
        return {"extra_body": {"output_config": {"effort": effort}}}
    return {"reasoning_effort": effort}


__all__ = [
    "EFFORT_BUDGET_FRACTIONS",
    "EFFORT_LEVELS",
    "EffortConfig",
    "EffortLevel",
    "EffortValue",
    "build_effort_request_params",
    "convert_effort_value_to_level",
    "effort_to_thinking_budget",
    "get_default_effort_for_model",
    "get_displayed_effort_level",
    "get_effort_env_override",
    "get_effort_env_state",
    "get_effort_level_description",
    "get_effort_suffix",
    "get_effort_value_description",
    "get_effort_config",
    "is_effort_level",
    "is_valid_numeric_effort",
    "model_supports_effort",
    "model_supports_max_effort",
    "parse_effort_value",
    "resolve_applied_effort",
    "resolve_picker_effort_persistence",
    "to_persistable_effort",
]
