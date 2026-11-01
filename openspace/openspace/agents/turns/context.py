"""Shared runtime context for GroundingAgent turn controllers."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(slots=True)
class TurnControllerContext:
    """Stable per-turn references shared by extracted controllers."""

    agent: Any
    context: dict[str, Any]
    tool_use_context: Any
    state: Any
    abort_event: Any = None
    instruction: str = ""
    agent_id: str = "primary"
    low_latency_profiler: Any = None
    latency_span: Callable[..., Any] | None = None

    def span(self, name: str, **metadata: Any) -> Any:
        if self.latency_span is None:
            return nullcontext()
        return self.latency_span(name, **metadata)
