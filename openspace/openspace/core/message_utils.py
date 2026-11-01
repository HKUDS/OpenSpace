"""
Message filtering and rendering utilities.
"""
from __future__ import annotations

from typing import Any

NON_RENDERABLE_TYPES: set[str] = {
    "tool_use_metadata",
    "thinking_trace",
    "system_annotation",
    "cache_control",
    "token_count",
    "debug_info",
}


def is_renderable(attachment_type: str) -> bool:
    return attachment_type not in NON_RENDERABLE_TYPES


def _has_renderable_content(message: dict[str, Any]) -> bool:
    """Return True if the message carries at least one piece of
    user-visible content (text, tool result, image, etc.)."""
    content = message.get("content")
    if content is None:
        return False
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            is_renderable(block.get("type", "")) if isinstance(block, dict) else bool(block)
            for block in content
        )
    return True


def filter_for_display(
    messages: list[dict[str, Any]],
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return up to *limit* most-recent messages suitable for display.

    System messages are always retained.  Messages whose content consists
    entirely of non-renderable blocks are dropped.
    """
    system: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []

    for msg in messages:
        if msg.get("role") == "system":
            system.append(msg)
        elif _has_renderable_content(msg):
            rest.append(msg)

    tail = rest[-limit:] if len(rest) > limit else rest
    return system + tail
