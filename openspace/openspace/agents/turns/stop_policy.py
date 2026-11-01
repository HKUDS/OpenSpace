"""Stop and continuation decisions for GroundingAgent turns."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from typing import Any, Mapping

from openspace.llm.types import ModelResponse


MAX_OUTPUT_TOKENS_RECOVERY_LIMIT: int = 3
MAX_CONSECUTIVE_EMPTY: int = 5
_MAX_OUTPUT_TOKENS_RECOVERY_LIMIT_ENV = "OPENSPACE_MAX_OUTPUT_TOKENS_RECOVERY_LIMIT"


@dataclass(frozen=True, slots=True)
class EmptyResponseState:
    consecutive_empty: int
    should_stop: bool


def is_api_error_message(message: Mapping[str, Any] | None) -> bool:
    if not isinstance(message, Mapping):
        return False
    meta = message.get("_meta")
    return isinstance(meta, Mapping) and bool(meta.get("is_api_error_message"))


def model_error_stop_reason(stop_reason: str | None) -> str:
    if stop_reason == "length":
        return "max_output_tokens"
    if stop_reason in {"refusal", "content_filter"}:
        return str(stop_reason)
    return "model_error"


def get_model_response_followup_messages(
    model_response: ModelResponse,
) -> list[dict[str, Any]]:
    assistant_index = next(
        (
            index
            for index, message in enumerate(model_response.messages)
            if message is model_response.assistant_message
        ),
        -1,
    )
    if assistant_index < 0:
        assistant_role = model_response.assistant_message.get("role")
        assistant_content = model_response.assistant_message.get("content")
        assistant_tool_calls = model_response.assistant_message.get("tool_calls")
        for index in range(len(model_response.messages) - 1, -1, -1):
            message = model_response.messages[index]
            if not isinstance(message, dict):
                continue
            if message.get("role") != assistant_role:
                continue
            if message.get("content") != assistant_content:
                continue
            if message.get("tool_calls") != assistant_tool_calls:
                continue
            assistant_index = index
            break
    if assistant_index < 0:
        return []
    return [
        copy.deepcopy(message)
        for message in model_response.messages[assistant_index + 1 :]
        if isinstance(message, dict)
    ]


def build_max_output_tokens_recovery_message(
    attempt: int,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    resolved_limit = (
        max_output_tokens_recovery_limit()
        if limit is None
        else max(0, int(limit))
    )
    return {
        "role": "user",
        "content": (
            "Your response was truncated due to output length limits. "
            "Do not continue the long response verbatim. Keep your next chat "
            "message minimal. Your next response must contain a tool call "
            "when tools are available. Call a shell/file tool now to inspect "
            "files, write the best current artifact to the requested path, or "
            "run a verification command. Put substantial code, data, and "
            "analysis into files or commands instead of chat. "
            f"(recovery attempt {attempt}/{resolved_limit})"
        ),
        "_meta": {"type": "max_output_tokens_recovery", "is_meta": True},
    }


def max_output_tokens_recovery_limit(
    default: int = MAX_OUTPUT_TOKENS_RECOVERY_LIMIT,
) -> int:
    raw = os.environ.get(_MAX_OUTPUT_TOKENS_RECOVERY_LIMIT_ENV)
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def should_recover_max_output_tokens(
    *,
    stop_reason: str | None,
    has_tool_calls: bool,
    recovery_count: int,
    limit: int | None = None,
) -> bool:
    resolved_limit = (
        max_output_tokens_recovery_limit()
        if limit is None
        else max(0, int(limit))
    )
    return (
        stop_reason == "length"
        and not has_tool_calls
        and recovery_count < resolved_limit
    )


def is_abort_requested(abort_event: Any) -> bool:
    checker = getattr(abort_event, "is_set", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:
        return False


def abort_stop_reason(
    abort_event: Any,
    *,
    during_tool_use: bool = False,
    after_model_response: bool = False,
) -> str | None:
    if not is_abort_requested(abort_event):
        return None
    if during_tool_use:
        return "aborted_tools"
    if after_model_response:
        return "aborted_streaming"
    return "aborted"


def is_tool_call_only_response(
    *,
    assistant_content: Any,
    has_tool_calls: bool,
) -> bool:
    return bool(
        has_tool_calls
        and (
            assistant_content is None
            or not isinstance(assistant_content, str)
            or not assistant_content.strip()
        )
    )


def update_empty_response_state(
    *,
    assistant_content: Any,
    has_tool_calls: bool,
    consecutive_empty: int,
    max_consecutive_empty: int = MAX_CONSECUTIVE_EMPTY,
) -> EmptyResponseState:
    if (
        assistant_content
        and isinstance(assistant_content, str)
        and assistant_content.strip()
    ):
        return EmptyResponseState(consecutive_empty=0, should_stop=False)
    if has_tool_calls:
        return EmptyResponseState(consecutive_empty=0, should_stop=False)
    next_empty = consecutive_empty + 1
    return EmptyResponseState(
        consecutive_empty=next_empty,
        should_stop=next_empty >= max_consecutive_empty,
    )


def max_iterations_stop_reason(
    current_iteration: int,
    max_iterations: int,
) -> str | None:
    return "max_turns" if current_iteration >= max_iterations else None


__all__ = [
    "EmptyResponseState",
    "MAX_CONSECUTIVE_EMPTY",
    "MAX_OUTPUT_TOKENS_RECOVERY_LIMIT",
    "abort_stop_reason",
    "build_max_output_tokens_recovery_message",
    "get_model_response_followup_messages",
    "is_abort_requested",
    "is_api_error_message",
    "is_tool_call_only_response",
    "max_output_tokens_recovery_limit",
    "max_iterations_stop_reason",
    "model_error_stop_reason",
    "should_recover_max_output_tokens",
    "update_empty_response_state",
]
