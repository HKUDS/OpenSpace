from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from openspace.grounding.core.tool import BaseTool

_SLOTS_KW = {"slots": True} if sys.version_info >= (3, 10) else {}

MessageDict = dict[str, Any]
ToolCallDict = dict[str, Any]


# ---------------------------------------------------------------------------
# Token detail sub-structures
#
# Wire format: ``response.usage.completion_tokens_details`` etc.
# Not all providers populate every field.  Fields used in arithmetic
# default to ``0``; purely informational fields default to ``None``.
# ---------------------------------------------------------------------------


@dataclass(**_SLOTS_KW)
class OutputTokensDetails:
    """Breakdown of output (completion) tokens.

    Wire key: ``usage.completion_tokens_details``.
    """

    reasoning_tokens: int = 0
    audio_tokens: int = 0
    image_tokens: int = 0
    accepted_prediction_tokens: Optional[int] = None
    rejected_prediction_tokens: Optional[int] = None
    text_tokens: Optional[int] = None
    video_tokens: Optional[int] = None


@dataclass(**_SLOTS_KW)
class InputTokensDetails:
    """Breakdown of input (prompt) tokens.

    Wire key: ``usage.prompt_tokens_details``.
    """

    cached_tokens: int = 0
    cache_write_tokens: int = 0
    audio_tokens: int = 0
    image_tokens: Optional[int] = None
    video_tokens: Optional[int] = None


@dataclass(**_SLOTS_KW)
class CostDetails:
    """Per-call cost breakdown.  Only populated when the request goes
    through a billing-aware proxy like OpenRouter; ``0.0`` otherwise.

    Wire key: ``usage.cost_details``.
    """

    upstream_inference_cost: float = 0.0
    upstream_inference_prompt_cost: float = 0.0
    upstream_inference_completions_cost: float = 0.0


@dataclass(**_SLOTS_KW)
class TokenUsage:
    """Token counts and cost for a single LLM call."""

    # Core counts (always present from every provider)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    # Derived counts (from nested details; 0 when not applicable)
    reasoning_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    # Full detail sub-structures
    output_details: OutputTokensDetails = field(
        default_factory=OutputTokensDetails,
    )
    input_details: InputTokensDetails = field(
        default_factory=InputTokensDetails,
    )

    # Cost (only from billing-aware proxies like OpenRouter)
    cost: float = 0.0
    cost_details: CostDetails = field(default_factory=CostDetails)

    # Server-side paid tool usage (Implementation: usage.server_tool_use.web_search_requests).
    web_search_requests: int = 0


EMPTY_USAGE: TokenUsage = TokenUsage()


def get_token_count_from_usage(usage: TokenUsage) -> int:
    """Total context window tokens consumed by one API call.

    Prefers ``total_tokens`` (directly from API response) when available,
    otherwise computes from individual counts.
    """
    if usage.total_tokens > 0:
        return usage.total_tokens
    return (
        usage.input_tokens
        + usage.cache_creation_input_tokens
        + usage.cache_read_input_tokens
        + usage.output_tokens
    )


def get_current_usage(messages: list[MessageDict]) -> dict[str, int] | None:
    """Walk backward through *messages* to find the most recent assistant
    message carrying ``_meta.usage`` and return its core token counts.
    """
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        meta = msg.get("_meta", {})
        u = meta.get("usage")
        if u is None:
            continue
        return {
            "input_tokens": u.get("input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
            "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
            "reasoning_tokens": u.get("reasoning_tokens", 0),
            "total_tokens": u.get("total_tokens", 0),
        }
    return None


def token_usage_from_dict(raw: dict[str, Any] | None) -> TokenUsage:
    """Build a ``TokenUsage`` from a raw dict (typically ``response.usage.model_dump()``).

    Nested detail dicts (``completion_tokens_details``,
    ``prompt_tokens_details``) are read for cache and reasoning counts.
    Missing keys safely fall back to ``0`` / ``None``.
    """
    if not raw:
        return TokenUsage()

    # Nested detail dicts (may be absent or None) 
    ctd = raw.get("completion_tokens_details") or {}
    ptd = raw.get("prompt_tokens_details") or {}
    cd = raw.get("cost_details") or {}
    stu = raw.get("server_tool_use") or {}

    # Core counts
    input_tokens = raw.get("input_tokens") or raw.get("prompt_tokens") or 0
    output_tokens = raw.get("output_tokens") or raw.get("completion_tokens") or 0
    total_tokens = raw.get("total_tokens") or 0

    # Derived counts from nested details 
    # Top-level keys (our _meta format) take priority over nested ones (API wire format).
    cache_read = (
        raw.get("cache_read_input_tokens")
        or ptd.get("cached_tokens")
        or 0
    )
    cache_write = (
        raw.get("cache_creation_input_tokens")
        or ptd.get("cache_write_tokens")
        or 0
    )
    reasoning = ctd.get("reasoning_tokens") or 0

    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        reasoning_tokens=reasoning,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
        output_details=OutputTokensDetails(
            reasoning_tokens=reasoning,
            audio_tokens=ctd.get("audio_tokens") or 0,
            image_tokens=ctd.get("image_tokens") or 0,
            accepted_prediction_tokens=ctd.get("accepted_prediction_tokens"),
            rejected_prediction_tokens=ctd.get("rejected_prediction_tokens"),
            text_tokens=ctd.get("text_tokens"),
            video_tokens=ctd.get("video_tokens"),
        ),
        input_details=InputTokensDetails(
            cached_tokens=cache_read,
            cache_write_tokens=cache_write,
            audio_tokens=ptd.get("audio_tokens") or 0,
            image_tokens=ptd.get("image_tokens"),
            video_tokens=ptd.get("video_tokens"),
        ),
        cost=raw.get("cost") or 0.0,
        cost_details=CostDetails(
            upstream_inference_cost=cd.get("upstream_inference_cost") or 0.0,
            upstream_inference_prompt_cost=cd.get("upstream_inference_prompt_cost") or 0.0,
            upstream_inference_completions_cost=cd.get("upstream_inference_completions_cost") or 0.0,
        ),
        web_search_requests=stu.get("web_search_requests") or raw.get("web_search_requests") or 0,
    )


@dataclass(**_SLOTS_KW)
class ModelResponse:
    """Standardized output for a single ``call_model()`` invocation.

    ``GroundingAgent.process()`` consumes this to drive the agent loop.
    """

    assistant_message: MessageDict
    """OpenAI-format assistant message dict."""

    tool_calls: list[ToolCallDict]
    """OpenAI format: ``[{"id": str, "type": "function", "function": {"name": str, "arguments": str|dict}}, ...]``.
    Same format as ``assistant_message["tool_calls"]``."""

    tool_map: dict[str, BaseTool]
    """LLM tool name → BaseTool instance."""

    stop_reason: str | None
    """Maps from ``choice.finish_reason``.  Common values:
    ``'stop'``, ``'length'``, ``'tool_calls'``, ``'content_filter'``.
    Anthropic via LiteLLM may also produce ``'refusal'``."""

    usage: TokenUsage
    """Token counts and cost for this turn."""

    messages: list[MessageDict]
    """Input messages + assistant_message.  Do NOT use this to overwrite
    the caller's message list — append ``assistant_message`` only."""

    effective_model: str | None = None
    """The model that actually produced this response after any request-local
    fallback handling."""
