"""SleepTool - safe wait primitive for OpenSpace."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Mapping

from openspace.grounding.core.permissions.types import PermissionAllow
from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolSchema, ToolStatus


SLEEP_TOOL_NAME = "sleep"
SLEEP_TOOL_ALIAS = "Sleep"
DEFAULT_SLEEP_MAX_MS = 30_000
HARD_SLEEP_MAX_MS = 5 * 60 * 1000

DESCRIPTION = "Wait for a specified duration"
PROMPT = """Wait for a specified duration. The user can interrupt the sleep at any time.

Use this when the user tells you to sleep or rest, when you have nothing to do, or when you're waiting for something.

Prefer this over `bash(sleep ...)` because it does not hold a shell process."""


def _configured_max_ms() -> int:
    raw = os.environ.get("OPENSPACE_SLEEP_MAX_MS")
    if raw is None:
        return DEFAULT_SLEEP_MAX_MS
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_SLEEP_MAX_MS
    return max(1, min(value, HARD_SLEEP_MAX_MS))


def normalize_sleep_duration_ms(input_data: Mapping[str, Any]) -> tuple[int | None, str | None]:
    has_duration_ms = input_data.get("duration_ms") is not None
    has_seconds = input_data.get("seconds") is not None
    if has_duration_ms == has_seconds:
        return None, "Provide exactly one of duration_ms or seconds."

    raw = input_data.get("duration_ms") if has_duration_ms else input_data.get("seconds")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, "Sleep duration must be a positive number."
    if value <= 0:
        return None, "Sleep duration must be positive."

    requested_ms = int(round(value if has_duration_ms else value * 1000))
    max_ms = _configured_max_ms()
    if requested_ms > max_ms:
        return None, f"Sleep duration exceeds the configured limit of {max_ms} ms."
    return requested_ms, None


def make_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "duration_ms": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": "Duration to wait in milliseconds.",
            },
            "seconds": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": "Duration to wait in seconds.",
            },
            "reason": {
                "type": "string",
                "description": "Optional short reason for waiting.",
            },
        },
        "additionalProperties": False,
    }


class SleepTool(BaseTool):
    _name = SLEEP_TOOL_NAME
    _description = DESCRIPTION
    backend_type = BackendType.META
    aliases = [SLEEP_TOOL_ALIAS, "SleepTool"]
    _is_read_only = True
    _is_concurrency_safe = True
    should_defer = True
    search_hint = "wait pause delay timer"
    max_result_size_chars = 100_000

    def __init__(self) -> None:
        self._current_context: Any | None = None
        super().__init__(
            schema=ToolSchema(
                name=self._name,
                description=DESCRIPTION,
                parameters=make_input_schema(),
                backend_type=self.backend_type,
            )
        )

    def set_context(self, context: Any) -> None:
        self._current_context = context

    def get_prompt(self, context: Any = None) -> str:
        return PROMPT

    async def validate_input(self, input: dict[str, Any], context: Any = None) -> str | None:
        _, error = normalize_sleep_duration_ms(input)
        return error

    async def check_permissions(self, input: dict[str, Any], context: Any = None) -> PermissionAllow:
        return PermissionAllow(updated_input=dict(input))

    async def _arun(
        self,
        duration_ms: float | int | None = None,
        seconds: float | int | None = None,
        reason: str | None = None,
    ) -> ToolResult:
        requested_ms, error = normalize_sleep_duration_ms(
            {"duration_ms": duration_ms, "seconds": seconds}
        )
        if error or requested_ms is None:
            return ToolResult(status=ToolStatus.ERROR, content=error or "Invalid sleep duration.", error=error)

        started = time.monotonic()
        abort_event = getattr(self._current_context, "abort_event", None)
        try:
            if isinstance(abort_event, asyncio.Event):
                await asyncio.wait_for(abort_event.wait(), timeout=requested_ms / 1000)
                aborted = True
            else:
                await asyncio.sleep(requested_ms / 1000)
                aborted = False
        except asyncio.TimeoutError:
            aborted = False

        waited_ms = int(round((time.monotonic() - started) * 1000))
        data = {
            "requested_ms": requested_ms,
            "waited_ms": waited_ms,
            "aborted": aborted,
            "reason": reason or "",
            "max_ms": _configured_max_ms(),
        }
        if aborted:
            content = f"Sleep interrupted after {waited_ms} ms."
        else:
            content = f"Slept for {waited_ms} ms."
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=content,
            metadata={"tool": self.name, "data": data},
        )


__all__ = [
    "SLEEP_TOOL_ALIAS",
    "DEFAULT_SLEEP_MAX_MS",
    "HARD_SLEEP_MAX_MS",
    "SLEEP_TOOL_NAME",
    "SleepTool",
    "normalize_sleep_duration_ms",
]
