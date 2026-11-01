"""Session-save timing helpers for GroundingAgent turns."""

from __future__ import annotations

from typing import Any


async def save_after_assistant_response(
    agent: Any,
    tool_use_context: Any,
    messages: list[dict[str, Any]],
    *,
    usage: Any | None = None,
    model: str | None = None,
) -> None:
    await agent._save_session_turn(
        tool_use_context,
        messages,
        usage=usage,
        model=model,
    )


async def save_after_model_error(
    agent: Any,
    tool_use_context: Any,
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
) -> None:
    await agent._save_session_turn(tool_use_context, messages, model=model)


async def save_after_tool_result_budget(
    agent: Any,
    tool_use_context: Any,
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
) -> None:
    await agent._save_session_turn(tool_use_context, messages, model=model)
