"""Normalized internal runtime event structures.

These helpers keep the background runtime/orchestrator boundary typed and
avoid passing ad-hoc dicts around the core layer.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any

RUNTIME_AGENT_EVENT_TYPES: set[str] = {
    "agent_start",
    "agent_progress",
    "agent_output",
    "agent_error",
    "agent_complete",
    "agent_spawn",
    "agent_task_update",
    "agent_task_complete",
}

RUNTIME_BACKGROUND_EVENT_TYPES: set[str] = {
    "background_session_update",
    "team_update",
    "todo_update",
    "task_started",
    "task_completed",
    "task_failed",
    "task_stopped",
    "background_housekeeping_idle",
    "background_housekeeping_cleanup_complete",
    "background_housekeeping_recurring_cleanup",
}

RUNTIME_EVENT_TYPES: set[str] = (
    RUNTIME_AGENT_EVENT_TYPES | RUNTIME_BACKGROUND_EVENT_TYPES
)


@dataclass
class AgentRuntimeEvent:
    """Normalized event emitted by background orchestration layers."""

    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    agent_id: str | None = None
    timestamp: float = field(default_factory=lambda: time.time() * 1000)
    source: str = "runtime"

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "payload": copy.deepcopy(self.payload),
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "timestamp": self.timestamp,
            "source": self.source,
        }


def coerce_runtime_event(
    event_type: str | AgentRuntimeEvent | dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> AgentRuntimeEvent:
    """Coerce runtime event input into a normalized event object."""

    if isinstance(event_type, AgentRuntimeEvent):
        return event_type

    if isinstance(event_type, dict):
        raw = copy.deepcopy(event_type)
        normalized_type = str(
            raw.get("event_type")
            or raw.get("type")
            or raw.get("kind")
            or ""
        ).strip()
        if not normalized_type:
            raise ValueError("Runtime event payload is missing an event type")
        raw_payload = raw.get("payload")
        if raw_payload is None:
            raw_payload = {
                key: value
                for key, value in raw.items()
                if key not in {"event_type", "type", "kind", "timestamp"}
            }
        return AgentRuntimeEvent(
            event_type=normalized_type,
            payload=normalize_runtime_payload(raw_payload),
            session_id=_optional_str(raw.get("session_id")),
            agent_id=_optional_str(raw.get("agent_id")),
            timestamp=float(raw.get("timestamp", time.time() * 1000)),
            source=str(raw.get("source") or "runtime"),
        )

    normalized_type = str(event_type).strip()
    if not normalized_type:
        raise ValueError("Runtime event type is required")
    return AgentRuntimeEvent(
        event_type=normalized_type,
        payload=normalize_runtime_payload(payload),
    )


def normalize_runtime_payload(payload: Any) -> dict[str, Any]:
    """Return a deep-copied dict payload for runtime event transport."""

    if payload is None:
        return {}
    if isinstance(payload, dict):
        return copy.deepcopy(payload)
    return {"value": copy.deepcopy(payload)}


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    value_str = str(value).strip()
    return value_str or None
