"""Parallel tool orchestration.

Consecutive concurrency-safe tool calls run in parallel with an
``asyncio.Semaphore``; non-safe calls run serially. Parallel batch context
modifiers are applied in original tool-call order after the batch completes,
while serial batches apply context changes immediately.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

from openspace.grounding.core.tool.base import BaseTool
from openspace.tool_runtime.pipeline.execution import (
    ToolCallResult,
    find_tool_by_name,
    run_tool_use,
)
from openspace.services.tooling.context import ToolUseContext
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_MAX_TOOL_USE_CONCURRENCY: int = 10
"""Default maximum concurrent tool calls.
Overridable via ``OPENSPACE_MAX_TOOL_USE_CONCURRENCY`` env var."""


def _get_max_tool_use_concurrency() -> int:
    """Read the concurrency cap from env, with a safe default fallback."""
    raw = os.environ.get("OPENSPACE_MAX_TOOL_USE_CONCURRENCY", "")
    try:
        val = int(raw)
        if val > 0:
            return val
    except (ValueError, TypeError):
        pass
    return DEFAULT_MAX_TOOL_USE_CONCURRENCY


# ═══════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class ToolBatch:
    """A group of tool calls to execute together."""

    is_concurrency_safe: bool
    tool_calls: list[dict[str, Any]]


@dataclass
class RunToolsResult:
    """Aggregated result of running all tool calls in a turn.

    The agent loop appends returned messages and applies the optional final
    context value after all batches complete.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    """All tool result messages to append to conversation, in execution order."""

    updated_context: ToolUseContext | None = None
    """Final ToolUseContext after all context modifiers have been applied.
    ``None`` means no modifiers were applied (context unchanged)."""

    prevent_continuation: bool = False
    """If any ToolCallResult set ``prevent_continuation=True``, this is True."""

    stop_reason: str | None = None
    """First stop reason encountered (from hooks or tool)."""


# ═══════════════════════════════════════════════════════════════════════
# Tool-call partitioning
# ═══════════════════════════════════════════════════════════════════════

def partition_tool_calls(
    tool_calls: list[dict[str, Any]],
    tool_map: dict[str, BaseTool],
    context: ToolUseContext,
) -> list[ToolBatch]:
    """Partition tool calls into batches for mixed parallel/serial execution.

    Algorithm:
        1. For each tool call, determine ``is_concurrency_safe`` by looking
           up the tool and calling ``tool.is_concurrency_safe(parsed_input)``.
        2. Consecutive concurrency-safe calls are merged into a single batch.
        3. Each non-concurrency-safe call becomes its own batch.

    If the tool is not found or ``is_concurrency_safe()`` raises, the call
    is conservatively treated as non-concurrency-safe.

    Parameters
    ----------
    tool_calls : list[dict]
        OpenAI-format tool calls from ``ModelResponse.tool_calls``.
    tool_map : dict
        Map of tool names → BaseTool instances from ``ModelResponse.tool_map``.
    context : ToolUseContext
        Current turn context (provides ``context.tools`` for alias fallback).

    Returns
    -------
    list[ToolBatch]
        Ordered batches; concurrency-safe batches may contain multiple calls.
    """
    batches: list[ToolBatch] = []

    for tc in tool_calls:
        func = tc.get("function", {})
        tool_name = func.get("name", "")
        tool_input: dict[str, Any] = func.get("arguments", {})
        if isinstance(tool_input, str):
            import json
            try:
                tool_input = json.loads(tool_input)
            except (json.JSONDecodeError, TypeError):
                tool_input = {}

        # Look up tool — same resolution as run_tool_use (name → alias fallback)
        tool = tool_map.get(tool_name)
        if tool is None:
            tool = find_tool_by_name(list(tool_map.values()), tool_name)

        # Determine concurrency safety; exceptions fall back to serial execution.
        is_safe = False
        if tool is not None:
            try:
                is_safe = bool(tool.is_concurrency_safe(tool_input))
            except Exception:
                is_safe = False

        # Merge consecutive safe calls; non-safe always gets its own batch.
        if is_safe and batches and batches[-1].is_concurrency_safe:
            batches[-1].tool_calls.append(tc)
        else:
            batches.append(ToolBatch(is_concurrency_safe=is_safe, tool_calls=[tc]))

    return batches


# ═══════════════════════════════════════════════════════════════════════
# Tool execution orchestration
# ═══════════════════════════════════════════════════════════════════════

async def run_tools(
    tool_calls: list[dict[str, Any]],
    tool_map: dict[str, BaseTool],
    context: ToolUseContext,
    *,
    assistant_message: dict[str, Any] | None = None,
) -> RunToolsResult:
    """Execute all tool calls with parallel/serial batching.

    Orchestration:
        1. ``partition_tool_calls`` groups calls into batches.
        2. For each batch:
           - **concurrency-safe**: ``asyncio.gather`` with semaphore cap.
             Context modifiers are **queued** and applied in block order
             after the whole batch completes.
           - **non-safe**: executed serially; context modifiers applied
             **immediately**.
        3. All messages are collected in execution order.
        4. If any result sets ``prevent_continuation``, the flag propagates.

    Parameters
    ----------
    tool_calls : list[dict]
        OpenAI-format tool calls from ``ModelResponse.tool_calls``.
    tool_map : dict
        Map of tool names → BaseTool instances from ``ModelResponse.tool_map``.
    context : ToolUseContext
        Turn-scoped runtime context.
    assistant_message : dict, optional
        The full assistant message containing these tool calls.

    Returns
    -------
    RunToolsResult
        Aggregated messages, updated context, and continuation flag.
    """
    if not tool_calls:
        return RunToolsResult()

    batches = partition_tool_calls(tool_calls, tool_map, context)
    current_context = context
    context_was_modified = False
    tool_result_messages: list[dict[str, Any]] = []
    followup_messages: list[dict[str, Any]] = []
    should_prevent_continuation = False
    first_stop_reason: str | None = None
    concurrency_cap = _get_max_tool_use_concurrency()

    for batch in batches:
        if current_context.is_aborted():
            break

        if batch.is_concurrency_safe:
            # ── Concurrent batch ─────────────────────────────────────
            # Context modifiers are queued, not applied immediately.
            batch_results = await _run_batch_concurrently(
                batch.tool_calls,
                tool_map,
                current_context,
                assistant_message=assistant_message,
                concurrency_cap=concurrency_cap,
            )

            # Collect messages from all results (order matches tool_calls order)
            for result in batch_results:
                _collect_result_messages(
                    result.messages,
                    tool_result_messages,
                    followup_messages,
                )
                if result.prevent_continuation:
                    should_prevent_continuation = True
                    if first_stop_reason is None:
                        first_stop_reason = result.stop_reason

            # Apply queued context modifiers in block order.
            for tc, result in zip(batch.tool_calls, batch_results):
                if result.context_modifier is not None:
                    current_context = result.context_modifier(current_context)
                    context_was_modified = True

        else:
            # ── Serial batch ─────────────────────────────────────────
            # Run one at a time and apply context updates immediately.
            for tc in batch.tool_calls:
                if current_context.is_aborted():
                    break

                result = await run_tool_use(
                    tc,
                    tool_map,
                    current_context,
                    assistant_message=assistant_message,
                )

                _collect_result_messages(
                    result.messages,
                    tool_result_messages,
                    followup_messages,
                )

                # Apply context modifier immediately.
                if result.context_modifier is not None:
                    current_context = result.context_modifier(current_context)
                    context_was_modified = True

                if result.prevent_continuation:
                    should_prevent_continuation = True
                    if first_stop_reason is None:
                        first_stop_reason = result.stop_reason

    return RunToolsResult(
        # OpenAI-style tool pairing requires all tool results for an assistant
        # turn to be contiguous. Image/content followups stay after tool results.
        messages=tool_result_messages + followup_messages,
        updated_context=current_context if context_was_modified else None,
        prevent_continuation=should_prevent_continuation,
        stop_reason=first_stop_reason,
    )


# ═══════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════

def _is_tool_result_message(message: dict[str, Any]) -> bool:
    return message.get("role") == "tool"


def _collect_result_messages(
    messages: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
    followups: list[dict[str, Any]],
) -> None:
    """Split tool results from attachments/hook messages.

    The final conversation append order must keep all role=tool messages
    immediately after the assistant tool_calls message; otherwise OpenAI-style
    pairing repair can treat later tool results as missing/orphaned.
    """
    for message in messages:
        if _is_tool_result_message(message):
            tool_results.append(message)
        else:
            followups.append(message)

async def _run_batch_concurrently(
    tool_calls: list[dict[str, Any]],
    tool_map: dict[str, BaseTool],
    context: ToolUseContext,
    *,
    assistant_message: dict[str, Any] | None = None,
    concurrency_cap: int = DEFAULT_MAX_TOOL_USE_CONCURRENCY,
) -> list[ToolCallResult]:
    """Run a batch of concurrency-safe tool calls in parallel.

    Results are returned in the **same order** as ``tool_calls`` to preserve
    deterministic message ordering (asyncio.gather preserves input order).
    """
    if not tool_calls:
        return []

    if len(tool_calls) == 1:
        result = await run_tool_use(
            tool_calls[0],
            tool_map,
            context,
            assistant_message=assistant_message,
        )
        return [result]

    semaphore = asyncio.Semaphore(concurrency_cap)

    async def _run_one(tc: dict[str, Any]) -> ToolCallResult:
        async with semaphore:
            return await run_tool_use(
                tc,
                tool_map,
                context,
                assistant_message=assistant_message,
            )

    results = await asyncio.gather(
        *[_run_one(tc) for tc in tool_calls],
        return_exceptions=False,
    )
    return list(results)
