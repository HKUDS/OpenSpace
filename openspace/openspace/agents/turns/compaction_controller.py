"""Compaction and token-budget decisions for GroundingAgent turns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openspace.agents.turns import events as turn_events
from openspace.services.conversation.compact import (
    AutoCompactTracking,
    auto_compact_if_needed,
    build_post_compact_messages,
    calculate_token_warning_state,
    run_post_compact_cleanup,
    time_based_microcompact,
    token_count_with_estimation,
)
from openspace.services.runtime_support.budget import parse_token_budget
from openspace.services.tooling.context import ToolUseContext
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

@dataclass(slots=True)
class MicroCompactDecision:
    messages: list[dict[str, Any]]
    was_cleared: bool
    event_data: dict[str, Any] | None = None


@dataclass(slots=True)
class AutoCompactDecision:
    messages: list[dict[str, Any]]
    was_compacted: bool
    reset_max_output_recovery: bool = False
    consecutive_failures: int = 0


def resolve_turn_token_budget(
    context: dict[str, Any],
    instruction: str,
) -> int | None:
    raw_token_budget = (
        context.get("token_budget")
        if context.get("token_budget") is not None
        else context.get("task_budget")
    )
    if isinstance(raw_token_budget, dict):
        raw_token_budget = raw_token_budget.get("total")
    if isinstance(raw_token_budget, (int, float)) and raw_token_budget > 0:
        return int(raw_token_budget)
    return parse_token_budget(str(instruction))


async def maybe_time_based_microcompact(
    messages: list[dict[str, Any]],
    tool_use_context: ToolUseContext,
    *,
    query_source: str = "main_thread",
) -> MicroCompactDecision:
    mc_result = time_based_microcompact(messages, query_source=query_source)
    if mc_result.was_cleared:
        if mc_result.event_data is not None:
            await tool_use_context.emit_event(
                "time_based_microcompact",
                mc_result.event_data,
            )
        return MicroCompactDecision(
            messages=mc_result.messages,
            was_cleared=True,
            event_data=mc_result.event_data,
        )
    return MicroCompactDecision(messages=messages, was_cleared=False)


async def emit_token_warning(
    messages: list[dict[str, Any]],
    tool_use_context: ToolUseContext,
    *,
    model: str,
) -> None:
    try:
        token_count = token_count_with_estimation(messages)
        token_warning = calculate_token_warning_state(token_count, model)
        await tool_use_context.emit_event(
            "token_warning",
            turn_events.token_warning_payload(
                token_count=token_count,
                model=model,
                token_warning=token_warning,
            ),
        )
    except Exception:
        logger.debug("Failed to emit token_warning", exc_info=True)


async def maybe_auto_compact(
    agent: Any,
    messages: list[dict[str, Any]],
    tool_use_context: ToolUseContext,
    *,
    model: str,
    tracking: AutoCompactTracking,
    cwd: str | None = None,
) -> AutoCompactDecision:
    compact_result = await auto_compact_if_needed(
        messages,
        getattr(agent, "_llm_client", None),
        tool_use_context,
        model=model,
        tracking=tracking,
        hook_registry=getattr(agent, "_hook_registry", None),
    )
    if compact_result.was_compacted and compact_result.compaction_result:
        post_compact_msgs = build_post_compact_messages(compact_result.compaction_result)
        run_post_compact_cleanup(tool_use_context)
        system_msgs = agent._refresh_system_messages_after_compact(
            messages,
            cwd=cwd,
            deferred_tool_names=tool_use_context.deferred_tool_names,
            memory_mode=tool_use_context.memory_mode,
            skills_enabled=not tool_use_context.skills_disabled,
            skill_discovery_enabled=agent._has_discover_skills_tool(
                tool_use_context.tools
            ),
            permission_mode=tool_use_context.permission_mode,
            plan_file_path=tool_use_context.plan_file_path,
            response_style=tool_use_context.response_style,
            coordinator_mode=tool_use_context.coordinator_mode,
            coordinator_mode_enabled=tool_use_context.coordinator_mode_enabled,
        )
        tracking.compacted = True
        tracking.turn_counter = 0
        tracking.consecutive_failures = 0
        compacted_messages = system_msgs + post_compact_msgs
        await persist_compacted_session_messages(
            agent,
            tool_use_context,
            compacted_messages,
            model=model,
        )
        logger.info("Auto compact succeeded, messages replaced")
        return AutoCompactDecision(
            messages=compacted_messages,
            was_compacted=True,
            reset_max_output_recovery=True,
        )
    if compact_result.consecutive_failures > 0:
        tracking.consecutive_failures = compact_result.consecutive_failures
    return AutoCompactDecision(
        messages=messages,
        was_compacted=False,
        consecutive_failures=compact_result.consecutive_failures,
    )


async def persist_compacted_session_messages(
    agent: Any,
    tool_use_context: ToolUseContext,
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
) -> None:
    storage = getattr(tool_use_context, "session_storage", None)
    replace_messages = getattr(storage, "replace_messages", None)
    if replace_messages is None:
        return
    metadata_patch = None
    metadata_builder = getattr(agent, "_session_turn_metadata_patch", None)
    if callable(metadata_builder):
        metadata_patch = metadata_builder(tool_use_context)
    try:
        result = replace_messages(
            messages,
            model=model,
            metadata_patch=metadata_patch,
        )
        if hasattr(result, "__await__"):
            await result
    except Exception:
        logger.debug("Failed to persist compacted session messages", exc_info=True)


__all__ = [
    "AutoCompactDecision",
    "AutoCompactTracking",
    "MicroCompactDecision",
    "emit_token_warning",
    "maybe_auto_compact",
    "maybe_time_based_microcompact",
    "persist_compacted_session_messages",
    "resolve_turn_token_budget",
]
