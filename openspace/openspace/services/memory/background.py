"""Shared drain boundary for memory background work."""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from openspace.services.memory.dream import drain_pending_auto_dream
from openspace.services.memory.extract import drain_pending_extraction
from openspace.services.memory.session_memory import drain_pending_session_memory
from openspace.services.memory.task_scope import maybe_memory_task_scope_key

logger = logging.getLogger(__name__)

MemoryDrainEventSink = Callable[[str, dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class MemoryDrainResult:
    """Summary of memory background work still pending after a drain."""

    timeout_s: float
    session_id: str | None = None
    scope_key: str | None = None
    session_memory_pending: int = 0
    extraction_pending: int = 0
    auto_dream_pending: int = 0

    @property
    def pending_count(self) -> int:
        return (
            self.session_memory_pending
            + self.extraction_pending
            + self.auto_dream_pending
        )

    @property
    def timed_out(self) -> bool:
        return self.pending_count > 0

    def as_event_payload(self) -> dict[str, Any]:
        payload = {
            "timeout_s": self.timeout_s,
            "pending_count": self.pending_count,
            "session_memory_pending": self.session_memory_pending,
            "extraction_pending": self.extraction_pending,
            "auto_dream_pending": self.auto_dream_pending,
        }
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        if self.scope_key is not None:
            payload["scope_key"] = self.scope_key
        return payload


async def _emit_timeout_event(
    event_sink: MemoryDrainEventSink | None,
    payload: dict[str, Any],
) -> None:
    if event_sink is None:
        return
    try:
        result = event_sink("memory_background_drain_timeout", payload)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.debug("Memory drain timeout event sink failed", exc_info=True)


async def drain_pending_memory_tasks(
    timeout_s: float = 3.0,
    *,
    event_sink: MemoryDrainEventSink | None = None,
    session_id: str | None = None,
    context: Any | None = None,
) -> MemoryDrainResult:
    """Drain already-submitted memory tasks without scheduling new work.

    Passing ``context`` scopes the drain to that session/scope.  ``context=None``
    remains a global drain for compatibility; prefer
    ``drain_all_pending_memory_tasks`` when a global shutdown drain is intended.
    """

    timeout_s = max(0.0, float(timeout_s))
    session_id = _resolve_session_id(session_id, context)
    scope_key = _resolve_scope_key(session_id, context)
    deadline = time.monotonic() + timeout_s

    def remaining_timeout() -> float:
        return max(0.0, deadline - time.monotonic())

    result = MemoryDrainResult(
        timeout_s=timeout_s,
        session_id=session_id,
        scope_key=scope_key,
        session_memory_pending=await drain_pending_session_memory(
            timeout_s=remaining_timeout(),
            context=context,
            scope_key=scope_key,
        ),
        extraction_pending=await drain_pending_extraction(
            timeout_s=remaining_timeout(),
            context=context,
            scope_key=scope_key,
        ),
        auto_dream_pending=await drain_pending_auto_dream(
            timeout_s=remaining_timeout(),
            context=context,
            scope_key=scope_key,
        ),
    )
    if result.timed_out:
        payload = result.as_event_payload()
        logger.warning(
            "Timed out draining memory background tasks after %.2fs: "
            "%d pending (session_memory=%d, extraction=%d, auto_dream=%d)",
            timeout_s,
            result.pending_count,
            result.session_memory_pending,
            result.extraction_pending,
            result.auto_dream_pending,
        )
        await _emit_timeout_event(event_sink, payload)
    return result


async def drain_all_pending_memory_tasks(
    timeout_s: float = 3.0,
    *,
    event_sink: MemoryDrainEventSink | None = None,
) -> MemoryDrainResult:
    """Explicitly drain memory tasks across all scopes for process teardown."""

    return await drain_pending_memory_tasks(
        timeout_s=timeout_s,
        event_sink=event_sink,
        context=None,
    )


def _resolve_session_id(session_id: str | None, context: Any | None) -> str | None:
    if session_id is not None:
        value = str(session_id).strip()
        return value or None
    if context is None:
        return None
    if isinstance(context, Mapping):
        value = context.get("session_id")
    else:
        value = getattr(context, "session_id", None)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _resolve_scope_key(session_id: str | None, context: Any | None) -> str | None:
    scope_key = maybe_memory_task_scope_key(context)
    if scope_key is not None:
        return scope_key
    if session_id is not None:
        return f"session_id:{session_id}"
    return None


__all__ = [
    "MemoryDrainEventSink",
    "MemoryDrainResult",
    "drain_all_pending_memory_tasks",
    "drain_pending_memory_tasks",
]
