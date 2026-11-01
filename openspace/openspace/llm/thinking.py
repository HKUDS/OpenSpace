"""Extended thinking configuration and request-parameter mapping.

This module mirrors the OpenSpace thinking split:

- ``ThinkingConfig`` is the runtime intent (adaptive / enabled / disabled).
- ``build_thinking_request_params`` maps that intent to provider/LiteLLM
  request fields and returns the fixed budget, if any, for retry handling.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import os
import re
from typing import Any, Literal

ThinkingType = Literal["adaptive", "enabled", "disabled"]

DEFAULT_MAX_OUTPUT_TOKENS = 8_192
DEFAULT_MAX_THINKING_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS - 1

@dataclass(frozen=True, slots=True)
class ThinkingConfig:
    """OpenSpace's provider-neutral thinking intent.

    The shape intentionally stays close to OpenSpace's TS union:
    ``{type:'adaptive'}``, ``{type:'enabled', budgetTokens}``,
    ``{type:'disabled'}``.
    """

    type: ThinkingType
    budget_tokens: int | None = None
    source: str = "default"

    @classmethod
    def adaptive(cls, *, source: str = "default") -> "ThinkingConfig":
        return cls(type="adaptive", budget_tokens=None, source=source)

    @classmethod
    def enabled(
        cls,
        budget_tokens: int,
        *,
        source: str = "default",
    ) -> "ThinkingConfig":
        return cls(
            type="enabled",
            budget_tokens=max(1, int(budget_tokens)),
            source=source,
        )

    @classmethod
    def disabled(cls, *, source: str = "default") -> "ThinkingConfig":
        return cls(type="disabled", budget_tokens=None, source=source)

    def to_wire_dict(self) -> dict[str, Any]:
        if self.type == "enabled":
            return {"type": "enabled", "budget_tokens": self.budget_tokens}
        return {"type": self.type}


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _csv_items(env_name: str) -> list[str]:
    raw = os.environ.get(env_name, "")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _canonical_model(model: str) -> str:
    return str(model or "").strip().lower()


def _model_tokens(model: str) -> set[str]:
    lowered = _canonical_model(model)
    tokens = {lowered}
    if "/" in lowered:
        tokens.add(lowered.rsplit("/", 1)[-1])
    return tokens


def _env_matches(model: str, env_name: str) -> bool:
    tokens = _model_tokens(model)
    lowered = _canonical_model(model)
    for item in _csv_items(env_name):
        if item in tokens or item in lowered:
            return True
    return False


def _is_anthropic_model(model: str) -> bool:
    return "claude" in _canonical_model(model)


def _is_openai_reasoning_model(model: str) -> bool:
    m = _canonical_model(model).rsplit("/", 1)[-1]
    return (
        m.startswith("o1")
        or m.startswith("o3")
        or m.startswith("o4")
        or m.startswith("gpt-5")
    )


def _is_gemini_thinking_model(model: str) -> bool:
    return "gemini-2.5" in _canonical_model(model)


def supports_thinking(model: str) -> bool:
    """Return whether a model is known to support thinking/reasoning.

    Unknown models default to false unless explicitly allowlisted.  This is
    stricter than OpenSpace first-party/foundry defaults because OpenSpace sends
    through multiple LiteLLM providers.
    """

    if _is_truthy(os.environ.get("OPENSPACE_DISABLE_THINKING")):
        return False
    if _env_matches(model, "OPENSPACE_NO_THINKING_MODELS"):
        return False
    if _env_matches(model, "OPENSPACE_THINKING_MODELS"):
        return True

    m = _canonical_model(model)
    if _is_anthropic_model(model):
        if "claude-3-" in m or "3-5" in m:
            return False
        return (
            "sonnet-4" in m
            or "opus-4" in m
            or "haiku-4" in m
            or "claude-4" in m
        )
    if _is_openai_reasoning_model(model):
        return True
    if _is_gemini_thinking_model(model):
        return True
    return False


def supports_adaptive_thinking(model: str) -> bool:
    if _is_truthy(os.environ.get("OPENSPACE_DISABLE_ADAPTIVE_THINKING")):
        return False
    if _env_matches(model, "OPENSPACE_ADAPTIVE_THINKING_MODELS"):
        return True
    if not _is_anthropic_model(model):
        return False
    m = _canonical_model(model).replace(".", "-")
    return "opus-4-6" in m or "sonnet-4-6" in m


def supports_thinking_with_tools(model: str) -> bool:
    if _env_matches(model, "OPENSPACE_NO_THINKING_WITH_TOOLS_MODELS"):
        return False
    if _env_matches(model, "OPENSPACE_THINKING_WITH_TOOLS_MODELS"):
        return True
    if not supports_thinking(model):
        return False
    if _is_anthropic_model(model):
        m = _canonical_model(model)
        return "sonnet-4" in m or "opus-4" in m or "haiku-4" in m or "claude-4" in m
    if _is_openai_reasoning_model(model):
        return True
    return False


def has_ultrathink_keyword(text: str | None) -> bool:
    return bool(text and re.search(r"\bultrathink\b", text, re.IGNORECASE))


def get_model_max_output_tokens(model: str) -> int:
    """Return a model's upper max-output limit, using OpenSpace's Claude table first."""

    env_override = os.environ.get("OPENSPACE_MAX_OUTPUT_TOKENS")
    if env_override:
        try:
            value = int(env_override)
            if value > 0:
                return value
        except ValueError:
            pass

    m = _canonical_model(model)
    if "opus-4-6" in m or "sonnet-4-6" in m:
        return 128_000
    if (
        "opus-4-5" in m
        or "sonnet-4" in m
        or "haiku-4" in m
        or "3-7-sonnet" in m
    ):
        return 64_000
    if "opus-4-1" in m or "opus-4" in m:
        return 32_000
    if "claude-3-sonnet" in m or "3-5-sonnet" in m or "3-5-haiku" in m:
        return 8_192
    if "claude-3-opus" in m or "claude-3-haiku" in m:
        return 4_096

    try:
        import litellm

        info = litellm.get_model_info(model)
        if info and isinstance(info.get("max_output_tokens"), int):
            return int(info["max_output_tokens"])
    except Exception:
        pass
    return DEFAULT_MAX_OUTPUT_TOKENS


def get_max_thinking_tokens_for_model(model: str) -> int:
    env_override = os.environ.get("OPENSPACE_MAX_THINKING_TOKENS")
    if env_override:
        try:
            value = int(env_override)
            if value > 0:
                return value
        except ValueError:
            pass
    return max(0, get_model_max_output_tokens(model) - 1)


def clamp_thinking_budget(
    requested: int,
    model: str,
    max_output_tokens: int,
) -> int:
    return max(
        0,
        min(
            int(requested),
            get_max_thinking_tokens_for_model(model),
            int(max_output_tokens) - 1,
        ),
    )


def _round_down_to_multiple(value: int, multiple: int = 256) -> int:
    if value <= 0:
        return 0
    return max(multiple, (value // multiple) * multiple)


def _effort_level(effort: str | int | None) -> str | None:
    from .effort import convert_effort_value_to_level, parse_effort_value

    parsed = parse_effort_value(effort)
    if parsed is None:
        return None
    return convert_effort_value_to_level(parsed).value


def effort_to_thinking_budget(effort: str | int | None, model: str) -> int:
    from .effort import effort_to_thinking_budget as _effort_to_thinking_budget

    return _effort_to_thinking_budget(effort, model)


def _coerce_explicit_config(
    explicit: ThinkingConfig | Mapping[str, Any] | None,
    *,
    source: str = "explicit",
) -> ThinkingConfig | None:
    if explicit is None:
        return None
    if isinstance(explicit, ThinkingConfig):
        return explicit
    config_type = str(explicit.get("type", "")).strip().lower()
    if config_type == "disabled":
        return ThinkingConfig.disabled(source=source)
    if config_type == "adaptive":
        return ThinkingConfig.adaptive(source=source)
    if config_type == "enabled":
        budget = explicit.get("budget_tokens", explicit.get("budgetTokens"))
        if budget is None:
            return ThinkingConfig.adaptive(source=source)
        try:
            return ThinkingConfig.enabled(int(budget), source=source)
        except (TypeError, ValueError):
            return ThinkingConfig.adaptive(source=source)
    return None


def get_thinking_config(
    model: str,
    effort: str | int | None,
    user_request: str | None = None,
    *,
    max_output_tokens: int | None = None,
    enable_thinking: bool = True,
    explicit: ThinkingConfig | Mapping[str, Any] | None = None,
    has_tools: bool = False,
) -> ThinkingConfig:
    explicit_config = _coerce_explicit_config(explicit)
    if explicit_config is not None:
        if explicit_config.type == "disabled":
            return explicit_config
        if not enable_thinking:
            return ThinkingConfig.disabled(source="disabled")
        if not supports_thinking(model):
            return ThinkingConfig.disabled(source="unsupported_model")
        if has_tools and not supports_thinking_with_tools(model):
            return ThinkingConfig.disabled(source="tools_unsupported")
        return explicit_config

    if not enable_thinking or effort is None:
        return ThinkingConfig.disabled(source="disabled")
    if not supports_thinking(model):
        return ThinkingConfig.disabled(source="unsupported_model")
    if has_tools and not supports_thinking_with_tools(model):
        return ThinkingConfig.disabled(source="tools_unsupported")

    effective_effort = effort
    source = "effort"
    if has_ultrathink_keyword(user_request):
        level = _effort_level(effective_effort)
        if level in {None, "low", "medium"}:
            effective_effort = "high"
            source = "ultrathink"

    if supports_adaptive_thinking(model):
        return ThinkingConfig.adaptive(source=source)

    requested = effort_to_thinking_budget(effective_effort, model)
    if max_output_tokens is not None:
        requested = clamp_thinking_budget(requested, model, max_output_tokens)
    if requested <= 0:
        return ThinkingConfig.disabled(source="budget_zero")
    return ThinkingConfig.enabled(requested, source=source)


def build_thinking_request_params(
    config: ThinkingConfig,
    model: str,
    *,
    effort: str | int | None = None,
    max_output_tokens: int,
) -> tuple[dict[str, Any], int]:
    """Map ``ThinkingConfig`` to LiteLLM params and fixed retry budget."""

    if config.type == "disabled" or not supports_thinking(model):
        return {}, 0

    level = _effort_level(effort) or "medium"
    if _is_openai_reasoning_model(model):
        return {"reasoning_effort": level}, 0

    if _is_anthropic_model(model):
        if config.type == "adaptive" and supports_adaptive_thinking(model):
            return {"thinking": {"type": "adaptive"}}, 0
        requested = (
            int(config.budget_tokens)
            if config.type == "enabled" and config.budget_tokens is not None
            else get_max_thinking_tokens_for_model(model)
        )
        budget = clamp_thinking_budget(requested, model, max_output_tokens)
        if budget <= 0:
            return {}, 0
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}, budget

    # Gemini/provider-specific thinking is intentionally not guessed here.
    return {}, 0


__all__ = [
    "ThinkingConfig",
    "ThinkingType",
    "build_thinking_request_params",
    "clamp_thinking_budget",
    "effort_to_thinking_budget",
    "get_max_thinking_tokens_for_model",
    "get_model_max_output_tokens",
    "get_thinking_config",
    "has_ultrathink_keyword",
    "supports_adaptive_thinking",
    "supports_thinking",
    "supports_thinking_with_tools",
]
