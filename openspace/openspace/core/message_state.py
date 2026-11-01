"""
Message state — maintains the conversation message list from stream events.
"""
from __future__ import annotations

import copy
import time
from typing import Any, Optional

from openspace.protocol import StreamEvent
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

_TOMBSTONE = "__tombstone__"


class MessageState:
    """Converts incoming StreamEvents into a structured message list for display."""

    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []
        self._pending_tools: dict[str, dict[str, Any]] = {}
        self._compact_buffer: list[dict[str, Any]] | None = None
        self._complete = False
        self._token_usage: dict[str, int] = {"input": 0, "output": 0}

    @property
    def messages(self) -> list[dict[str, Any]]:
        return list(self._messages)

    @property
    def is_complete(self) -> bool:
        return self._complete

    @property
    def token_usage(self) -> dict[str, int]:
        return dict(self._token_usage)

    # ── Public API ────────────────────────────────────────────────

    def handle_event(self, event: StreamEvent) -> None:
        handler_name = f"_handle_{event.type}"
        handler = getattr(self, handler_name, None)
        if handler is None:
            logger.debug("MessageState: no handler for event type %r", event.type)
            return
        try:
            handler(event.data)
        except Exception:
            logger.exception("Error handling event %r", event.type)

    def remove_tombstones(self) -> None:
        self._messages = [
            m for m in self._messages if m.get("_status") != _TOMBSTONE
        ]

    def clear(self) -> None:
        self._messages.clear()
        self._pending_tools.clear()
        self._compact_buffer = None
        self._complete = False
        self._token_usage = {"input": 0, "output": 0}

    def get_display_messages(self, limit: int = 200) -> list[dict[str, Any]]:
        visible = [
            m for m in self._messages if m.get("_status") != _TOMBSTONE
        ]
        if limit and len(visible) > limit:
            visible = visible[-limit:]
        return copy.deepcopy(visible)

    # ── LLM handlers ─────────────────────────────────────────────

    def _handle_llm_start(self, data: dict[str, Any]) -> None:
        self._messages.append({
            "role": "assistant",
            "content": "",
            "type": "llm",
            "_status": "streaming",
            "_started_at": time.time(),
            "model": data.get("model", ""),
        })

    def _handle_llm_token(self, data: dict[str, Any]) -> None:
        token = data.get("token", "")
        if not token:
            return
        last = self._find_last_assistant()
        if last is not None:
            last["content"] += token

    def _handle_llm_complete(self, data: dict[str, Any]) -> None:
        last = self._find_last_assistant()
        if last is None:
            return
        last["_status"] = "complete"
        last["_finished_at"] = time.time()
        if "usage" in data:
            usage = data["usage"]
            self._token_usage["input"] += usage.get("input_tokens", 0)
            self._token_usage["output"] += usage.get("output_tokens", 0)
        if "stop_reason" in data:
            last["stop_reason"] = data["stop_reason"]

    # ── Tool handlers ────────────────────────────────────────────

    def _handle_tool_start(self, data: dict[str, Any]) -> None:
        tool_use_id = data.get("tool_use_id", "")
        msg: dict[str, Any] = {
            "role": "tool",
            "type": "tool_use",
            "tool_use_id": tool_use_id,
            "tool_name": data.get("tool_name", ""),
            "tool_input": data.get("tool_input", {}),
            "content": "",
            "_status": "running",
            "_started_at": time.time(),
        }
        self._messages.append(msg)
        if tool_use_id:
            self._pending_tools[tool_use_id] = msg

    def _handle_tool_progress(self, data: dict[str, Any]) -> None:
        tool_use_id = data.get("tool_use_id", "")
        pending = self._pending_tools.get(tool_use_id)
        if pending is None:
            return
        pending["content"] = data.get("content", pending["content"])
        if "progress" in data:
            pending["_progress"] = data["progress"]

    def _handle_tool_complete(self, data: dict[str, Any]) -> None:
        tool_use_id = data.get("tool_use_id", "")
        pending = self._pending_tools.pop(tool_use_id, None)
        if pending is None:
            self._messages.append({
                "role": "tool",
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": data.get("output", ""),
                "_status": "complete",
                "_finished_at": time.time(),
            })
            return
        pending["content"] = data.get("output", pending.get("content", ""))
        pending["_status"] = "complete"
        pending["_finished_at"] = time.time()
        pending["type"] = "tool_result"

    def _handle_tool_error(self, data: dict[str, Any]) -> None:
        tool_use_id = data.get("tool_use_id", "")
        pending = self._pending_tools.pop(tool_use_id, None)
        if pending is None:
            self._messages.append({
                "role": "tool",
                "type": "tool_error",
                "tool_use_id": tool_use_id,
                "content": data.get("error", "Unknown error"),
                "_status": "error",
            })
            return
        pending["content"] = data.get("error", "Unknown error")
        pending["_status"] = "error"
        pending["type"] = "tool_error"

    # ── Compact handlers ─────────────────────────────────────────

    def _handle_compact_start(self, data: dict[str, Any]) -> None:
        self._compact_buffer = copy.deepcopy(self._messages)
        logger.debug("Compact started, buffered %d messages", len(self._messages))

    def _handle_compact_complete(self, data: dict[str, Any]) -> None:
        compacted = data.get("messages")
        if compacted is not None:
            self._messages = compacted
        self._compact_buffer = None
        logger.debug("Compact complete, now %d messages", len(self._messages))

    # ── Task handlers ────────────────────────────────────────────

    def _handle_task_complete(self, data: dict[str, Any]) -> None:
        self._complete = True
        self._pending_tools.clear()

    # ── Internal helpers ─────────────────────────────────────────

    def _find_last_assistant(self) -> Optional[dict[str, Any]]:
        for msg in reversed(self._messages):
            if msg.get("role") == "assistant" and msg.get("type") == "llm":
                return msg
        return None
