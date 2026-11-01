from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .models import ScheduledRun


EventSink = Callable[[str, dict[str, Any]], Awaitable[None] | None]


@dataclass(slots=True)
class NotificationResult:
    delivered: bool
    channel: str
    message: str = ""
    error: str | None = None


@dataclass(slots=True)
class ApprovalRequest:
    id: str
    schedule_id: str
    run_id: str
    task_kind: str
    prompt: str
    status: str = "pending"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "schedule_id": self.schedule_id,
            "run_id": self.run_id,
            "task_kind": self.task_kind,
            "prompt": self.prompt,
            "status": self.status,
            "reason": self.reason,
        }


class NotificationService:
    """Provider-neutral notification facade for scheduled runs."""

    def __init__(self, event_sink: EventSink | None = None) -> None:
        self.event_sink = event_sink
        self.notifications: list[dict[str, Any]] = []

    async def notify(self, run: ScheduledRun, schedule: Any) -> NotificationResult:
        payload = {
            "run": run.to_dict(),
            "schedule": schedule.visible_dict() if hasattr(schedule, "visible_dict") else {},
        }
        self.notifications.append(payload)
        await emit_event(self.event_sink, "cron_notification", payload)
        return NotificationResult(
            delivered=True,
            channel=run.notification_target.channel,
            message="notification emitted",
        )


class ApprovalService:
    """Minimal approval bridge.

    The scheduler creates pending approval requests for unsafe task kinds. A
    channel adapter or TUI can later resolve them; 23.2 deliberately does not
    execute shell/write work without such a resolution.
    """

    def __init__(self, event_sink: EventSink | None = None) -> None:
        self.event_sink = event_sink
        self.requests: dict[str, ApprovalRequest] = {}

    async def request_approval(
        self,
        run: ScheduledRun,
        schedule: Any,
        *,
        reason: str,
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            id="approval_" + uuid.uuid4().hex[:12],
            schedule_id=run.schedule_id,
            run_id=run.id,
            task_kind=run.task_kind,
            prompt=str(run.task_payload.get("prompt") or ""),
            reason=reason,
        )
        self.requests[request.id] = request
        await emit_event(
            self.event_sink,
            "cron_approval_requested",
            {
                "approval": request.to_dict(),
                "run": run.to_dict(),
                "schedule": schedule.visible_dict() if hasattr(schedule, "visible_dict") else {},
            },
        )
        return request


async def emit_event(
    event_sink: EventSink | None,
    event_type: str,
    data: dict[str, Any],
) -> None:
    if event_sink is None:
        return
    try:
        result = event_sink(event_type, data)
        if inspect.isawaitable(result):
            await result
    except Exception:
        pass


__all__ = [
    "ApprovalRequest",
    "ApprovalService",
    "EventSink",
    "NotificationResult",
    "NotificationService",
    "emit_event",
]
