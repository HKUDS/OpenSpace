"""Backend implementation of the ``/dream`` slash command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class DreamCommandResult:
    message: str
    display: str = "system"


async def execute_dream_command(
    args: list[str],
    *,
    openspace: Any,
) -> DreamCommandResult:
    """Execute ``/dream`` by running manual memory consolidation."""

    runner = getattr(openspace, "run_manual_dream", None)
    if runner is None:
        return DreamCommandResult("Dream runtime is not available.")

    logs_mode = False
    remaining: list[str] = []
    for arg in args:
        if arg == "--logs":
            logs_mode = True
            continue
        remaining.append(arg)
    extra_context = " ".join(remaining).strip()
    if logs_mode:
        result = await runner(extra_context, logs_mode=True)
    else:
        result = await runner(extra_context)
    if not isinstance(result, dict):
        return DreamCommandResult("Dream runtime returned an unexpected result.")

    return DreamCommandResult(
        str(result.get("message") or "Dream completed."),
        display="system",
    )
