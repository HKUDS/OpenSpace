"""Session token usage and cost tracking.

OpenSpace keeps cost accounting as an instance service with per-model usage,
cache token accounting, unknown-model marking, session snapshot/restore, and
formatted /cost output.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from openspace.llm.types import TokenUsage
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)
_SLOTS_KW = {"slots": True} if sys.version_info >= (3, 10) else {}


@dataclass(**_SLOTS_KW)
class ModelCosts:
    input_tokens: float
    output_tokens: float
    prompt_cache_write_tokens: float
    prompt_cache_read_tokens: float
    web_search_requests: float = 0.01


@dataclass(**_SLOTS_KW)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reasoning_tokens: int = 0
    web_search_requests: int = 0
    cost_usd: float = 0.0
    context_window: int = 0
    max_output_tokens: int = 0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ModelUsage":
        return cls(
            input_tokens=int(raw.get("input_tokens") or raw.get("inputTokens") or 0),
            output_tokens=int(raw.get("output_tokens") or raw.get("outputTokens") or 0),
            cache_read_input_tokens=int(
                raw.get("cache_read_input_tokens")
                or raw.get("cacheReadInputTokens")
                or 0
            ),
            cache_creation_input_tokens=int(
                raw.get("cache_creation_input_tokens")
                or raw.get("cacheCreationInputTokens")
                or 0
            ),
            reasoning_tokens=int(raw.get("reasoning_tokens") or raw.get("reasoningTokens") or 0),
            web_search_requests=int(raw.get("web_search_requests") or raw.get("webSearchRequests") or 0),
            cost_usd=float(raw.get("cost_usd") or raw.get("costUSD") or raw.get("cost") or 0.0),
            context_window=int(raw.get("context_window") or raw.get("contextWindow") or 0),
            max_output_tokens=int(raw.get("max_output_tokens") or raw.get("maxOutputTokens") or 0),
        )

    def to_camel_case_dict(self) -> dict[str, int | float]:
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "cacheReadInputTokens": self.cache_read_input_tokens,
            "cacheCreationInputTokens": self.cache_creation_input_tokens,
            "reasoningTokens": self.reasoning_tokens,
            "webSearchRequests": self.web_search_requests,
            "costUSD": self.cost_usd,
            "contextWindow": self.context_window,
            "maxOutputTokens": self.max_output_tokens,
        }

    def to_os_dict(self) -> dict[str, int | float]:
        data = asdict(self)
        data["cost"] = self.cost_usd
        return data


COST_TIER_3_15 = ModelCosts(3.0, 15.0, 3.75, 0.3)
COST_TIER_15_75 = ModelCosts(15.0, 75.0, 18.75, 1.5)
COST_TIER_5_25 = ModelCosts(5.0, 25.0, 6.25, 0.5)
COST_HAIKU_35 = ModelCosts(0.8, 4.0, 1.0, 0.08)
COST_HAIKU_45 = ModelCosts(1.0, 5.0, 1.25, 0.1)
DEFAULT_UNKNOWN_MODEL_COST = COST_TIER_5_25

# Compatibility table: existing tests import MODEL_PRICING and destructure
# (input_rate, output_rate).  Keep it as a two-tuple table while MODEL_COSTS
# carries OpenSpace's cache/web-search rates.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-20250514": (3.00, 15.00),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022": (0.80, 4.00),
    "claude-3-opus-20240229": (15.00, 75.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "o3-mini": (1.10, 4.40),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
}

MODEL_COSTS: dict[str, ModelCosts] = {
    "claude-3-5-haiku-20241022": COST_HAIKU_35,
    "claude-haiku-4-5": COST_HAIKU_45,
    "claude-3-5-sonnet-20241022": COST_TIER_3_15,
    "claude-3-7-sonnet": COST_TIER_3_15,
    "claude-sonnet-4-20250514": COST_TIER_3_15,
    "claude-sonnet-4-5": COST_TIER_3_15,
    "claude-sonnet-4.5": COST_TIER_3_15,
    "claude-sonnet-4-6": COST_TIER_3_15,
    "claude-3-opus-20240229": COST_TIER_15_75,
    "claude-opus-4": COST_TIER_15_75,
    "claude-opus-4-1": COST_TIER_15_75,
    "claude-opus-4-5": COST_TIER_5_25,
    "claude-opus-4-6": COST_TIER_5_25,
}

for _model, (_input, _output) in MODEL_PRICING.items():
    MODEL_COSTS.setdefault(
        _model,
        ModelCosts(
            input_tokens=_input,
            output_tokens=_output,
            prompt_cache_write_tokens=_input,
            prompt_cache_read_tokens=_input,
            web_search_requests=0.0,
        ),
    )

_SESSIONS_DIR = Path.home() / ".openspace" / "sessions"


def _sessions_dir() -> Path:
    return _SESSIONS_DIR


def _get_context_window(model: str) -> int:
    try:
        from openspace.services.conversation.compact import get_effective_context_window_size

        return int(get_effective_context_window_size(model))
    except Exception:
        return 200_000


def canonical_model_name(model: str) -> str:
    normalized = str(model or "unknown").strip()
    for prefix in (
        "openrouter/anthropic/",
        "openrouter/openai/",
        "openrouter/",
        "anthropic/",
        "openai/",
        "bedrock/",
        "vertex_ai/",
    ):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    return normalized.lower()


def get_model_costs(model: str) -> tuple[ModelCosts, bool]:
    canonical = canonical_model_name(model)
    costs = MODEL_COSTS.get(canonical)
    if costs is None:
        return DEFAULT_UNKNOWN_MODEL_COST, True
    return costs, False


def _tokens_to_usd(costs: ModelCosts, usage: TokenUsage, web_search_requests: int = 0) -> float:
    return (
        (usage.input_tokens / 1_000_000) * costs.input_tokens
        + (usage.output_tokens / 1_000_000) * costs.output_tokens
        + (usage.cache_read_input_tokens / 1_000_000) * costs.prompt_cache_read_tokens
        + (usage.cache_creation_input_tokens / 1_000_000) * costs.prompt_cache_write_tokens
        + web_search_requests * costs.web_search_requests
    )


def get_model_cost(model: str, usage: TokenUsage) -> float:
    cost, _unknown = get_model_cost_with_unknown(model, usage)
    return cost


def get_model_cost_with_unknown(model: str, usage: TokenUsage) -> tuple[float, bool]:
    provider_cost = float(usage.cost or 0.0)
    if provider_cost <= 0 and usage.cost_details.upstream_inference_cost > 0:
        provider_cost = float(usage.cost_details.upstream_inference_cost)
    if provider_cost > 0:
        return provider_cost, False
    costs, unknown = get_model_costs(model)
    web_search_requests = int(getattr(usage, "web_search_requests", 0) or 0)
    return _tokens_to_usd(costs, usage, web_search_requests), unknown


def format_cost(usd: float, max_decimal_places: int = 4) -> str:
    return f"${usd:.2f}" if usd > 0.5 else f"${usd:.{max_decimal_places}f}"


def _format_number(value: int | float) -> str:
    return f"{int(value):,}"


def format_total_cost(tracker: "CostTracker") -> str:
    cost_display = format_cost(tracker.get_total())
    if tracker.has_unknown_model_cost():
        cost_display += " (costs may be inaccurate due to usage of unknown models)"

    lines = [f"Total cost:            {cost_display}"]
    if tracker.total_api_duration_ms:
        lines.append(f"Total duration (API):  {tracker.total_api_duration_ms / 1000:.1f}s")

    usage_by_model = tracker.get_model_usage()
    if not usage_by_model:
        lines.append("Usage:                 0 input, 0 output, 0 cache read, 0 cache write")
        return "\n".join(lines)

    lines.append("Usage by model:")
    for model, usage in usage_by_model.items():
        usage_string = (
            f"  {_format_number(usage.input_tokens)} input, "
            f"{_format_number(usage.output_tokens)} output, "
            f"{_format_number(usage.cache_read_input_tokens)} cache read, "
            f"{_format_number(usage.cache_creation_input_tokens)} cache write"
        )
        if usage.reasoning_tokens:
            usage_string += f", {_format_number(usage.reasoning_tokens)} reasoning"
        if usage.web_search_requests:
            usage_string += f", {_format_number(usage.web_search_requests)} web search"
        usage_string += f" ({format_cost(usage.cost_usd)})"
        lines.append(f"{(model + ':').rjust(21)}{usage_string}")
    return "\n".join(lines)


class CostTracker:
    """Accumulates token usage across models and computes USD cost."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._model_usage: dict[str, ModelUsage] = {}
        self._total_cost_usd: float = 0.0
        self.total_api_duration_ms: int = 0
        self.total_api_duration_without_retries_ms: int = 0
        self.total_tool_duration_ms: int = 0
        self.total_lines_added: int = 0
        self.total_lines_removed: int = 0
        self.last_duration_ms: int | None = None
        self._has_unknown_model_cost: bool = False

    async def add_usage(
        self,
        model: str,
        usage: TokenUsage,
        *,
        duration_ms: int | None = None,
    ) -> float:
        async with self._lock:
            return self._add_usage_locked(model, usage, duration_ms=duration_ms)

    async def add_token_counts(self, model: str, input_tokens: int, output_tokens: int) -> float:
        return await self.add_usage(
            model,
            TokenUsage(
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
            ),
        )

    def _add_usage_locked(
        self,
        model: str,
        usage: TokenUsage,
        *,
        duration_ms: int | None = None,
    ) -> float:
        cost, unknown = get_model_cost_with_unknown(model, usage)
        if unknown:
            self._has_unknown_model_cost = True

        entry = self._model_usage.setdefault(
            model,
            ModelUsage(
                context_window=_get_context_window(model),
                max_output_tokens=0,
            ),
        )
        entry.input_tokens += int(usage.input_tokens or 0)
        entry.output_tokens += int(usage.output_tokens or 0)
        entry.cache_read_input_tokens += int(usage.cache_read_input_tokens or 0)
        entry.cache_creation_input_tokens += int(usage.cache_creation_input_tokens or 0)
        entry.reasoning_tokens += int(usage.reasoning_tokens or 0)
        entry.web_search_requests += int(getattr(usage, "web_search_requests", 0) or 0)
        entry.cost_usd += cost
        entry.context_window = entry.context_window or _get_context_window(model)

        self._total_cost_usd += cost
        if duration_ms is not None:
            self.total_api_duration_ms += int(duration_ms)
            self.last_duration_ms = int(duration_ms)
        return cost

    def add_lines_changed(self, added: int, removed: int) -> None:
        self.total_lines_added += int(added or 0)
        self.total_lines_removed += int(removed or 0)

    def set_has_unknown_model_cost(self) -> None:
        self._has_unknown_model_cost = True

    def has_unknown_model_cost(self) -> bool:
        return self._has_unknown_model_cost

    def get_total(self) -> float:
        return self._total_cost_usd

    def get_model_usage(self) -> dict[str, ModelUsage]:
        return dict(self._model_usage)

    def get_usage_for_model(self, model: str) -> ModelUsage | None:
        return self._model_usage.get(model)

    def get_breakdown(self) -> dict[str, dict[str, Any]]:
        return {
            model: usage.to_os_dict()
            for model, usage in self._model_usage.items()
        }

    def get_total_input_tokens(self) -> int:
        return sum(usage.input_tokens for usage in self._model_usage.values())

    def get_total_output_tokens(self) -> int:
        return sum(usage.output_tokens for usage in self._model_usage.values())

    def get_total_cache_read_input_tokens(self) -> int:
        return sum(usage.cache_read_input_tokens for usage in self._model_usage.values())

    def get_total_cache_creation_input_tokens(self) -> int:
        return sum(usage.cache_creation_input_tokens for usage in self._model_usage.values())

    def get_total_reasoning_tokens(self) -> int:
        return sum(usage.reasoning_tokens for usage in self._model_usage.values())

    def snapshot(self) -> dict[str, Any]:
        return {
            "usage": self.get_breakdown(),
            "model_usage": {
                model: usage.to_camel_case_dict()
                for model, usage in self._model_usage.items()
            },
            "total_cost": self.get_total(),
            "totalCostUSD": self.get_total(),
            "totalAPIDuration": self.total_api_duration_ms,
            "totalAPIDurationWithoutRetries": self.total_api_duration_without_retries_ms,
            "totalToolDuration": self.total_tool_duration_ms,
            "totalLinesAdded": self.total_lines_added,
            "totalLinesRemoved": self.total_lines_removed,
            "lastDuration": self.last_duration_ms,
            "hasUnknownModelCost": self._has_unknown_model_cost,
        }

    def restore(self, snapshot: Mapping[str, Any] | None) -> None:
        self._model_usage = {}
        self._total_cost_usd = 0.0
        self.total_api_duration_ms = 0
        self.total_api_duration_without_retries_ms = 0
        self.total_tool_duration_ms = 0
        self.total_lines_added = 0
        self.total_lines_removed = 0
        self.last_duration_ms = None
        self._has_unknown_model_cost = False

        if not isinstance(snapshot, Mapping):
            return

        raw_usage = snapshot.get("model_usage") or snapshot.get("modelUsage") or snapshot.get("usage")
        if isinstance(raw_usage, Mapping):
            for model, raw in raw_usage.items():
                if isinstance(raw, Mapping):
                    self._model_usage[str(model)] = ModelUsage.from_mapping(raw)

        total = snapshot.get("total_cost")
        if not isinstance(total, (int, float)):
            total = snapshot.get("totalCostUSD")
        self._total_cost_usd = (
            float(total)
            if isinstance(total, (int, float))
            else sum(usage.cost_usd for usage in self._model_usage.values())
        )
        self.total_api_duration_ms = int(snapshot.get("totalAPIDuration") or 0)
        self.total_api_duration_without_retries_ms = int(
            snapshot.get("totalAPIDurationWithoutRetries") or 0
        )
        self.total_tool_duration_ms = int(snapshot.get("totalToolDuration") or 0)
        self.total_lines_added = int(snapshot.get("totalLinesAdded") or 0)
        self.total_lines_removed = int(snapshot.get("totalLinesRemoved") or 0)
        last_duration = snapshot.get("lastDuration")
        self.last_duration_ms = int(last_duration) if isinstance(last_duration, (int, float)) else None
        self._has_unknown_model_cost = bool(snapshot.get("hasUnknownModelCost") or False)

    async def save(self, session_id: str) -> None:
        path = _sessions_dir() / f"{session_id}.cost.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.snapshot(), indent=2), encoding="utf-8")
        logger.debug("Cost data saved to %s", path)

    async def load(self, session_id: str) -> None:
        path = _sessions_dir() / f"{session_id}.cost.json"
        if not path.exists():
            logger.debug("No cost data found for session %s", session_id)
            return
        self.restore(json.loads(path.read_text(encoding="utf-8")))
        logger.debug("Cost data loaded from %s", path)

    @staticmethod
    def format_cost(usd: float) -> str:
        return format_cost(usd)

    def print_summary(self) -> None:
        logger.info(format_total_cost(self))
