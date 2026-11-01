from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


SCHEDULE_ID_ALPHABET = "0123456789abcdef"


class ScheduleKind(str, Enum):
    CRON = "cron"
    ONE_SHOT = "one_shot"


class TaskKind(str, Enum):
    REMINDER = "reminder"
    READ_ONLY_AGENT = "read_only_agent"


class RunStatus(str, Enum):
    DUE = "due"
    NOTIFIED = "notified"
    APPROVAL_REQUESTED = "approval_requested"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class NotificationTarget:
    """Provider-neutral notification destination.

    Channel adapters such as Feishu/TUI/CLI may interpret ``channel`` and
    ``identity``; the scheduler stores and forwards them without importing a
    channel SDK.
    """

    channel: str = "in_process"
    identity: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Any = None) -> "NotificationTarget":
        if isinstance(raw, NotificationTarget):
            return raw
        if isinstance(raw, str):
            return cls(channel=raw)
        if isinstance(raw, dict):
            metadata = raw.get("metadata")
            return cls(
                channel=str(raw.get("channel") or "in_process"),
                identity=(
                    str(raw.get("identity"))
                    if raw.get("identity") is not None
                    else None
                ),
                metadata=dict(metadata) if isinstance(metadata, dict) else {},
            )
        return cls()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TaskPolicy:
    task_kind: str = TaskKind.REMINDER.value
    approval_required: bool = False
    max_runtime_seconds: int = 300
    max_turns: int = 3
    read_only: bool = True
    allowed_tools: list[str] = field(default_factory=list)

    @classmethod
    def from_raw(
        cls,
        raw: Any = None,
        *,
        task_kind: str | None = None,
        approval_required: bool | None = None,
    ) -> "TaskPolicy":
        data = dict(raw) if isinstance(raw, dict) else {}
        kind = str(task_kind or data.get("task_kind") or data.get("kind") or TaskKind.REMINDER.value)
        needs_approval = bool(
            approval_required
            if approval_required is not None
            else data.get("approval_required", kind not in {TaskKind.REMINDER.value, TaskKind.READ_ONLY_AGENT.value})
        )
        return cls(
            task_kind=kind,
            approval_required=needs_approval,
            max_runtime_seconds=int(data.get("max_runtime_seconds") or 300),
            max_turns=int(data.get("max_turns") or 3),
            read_only=bool(data.get("read_only", kind == TaskKind.READ_ONLY_AGENT.value)),
            allowed_tools=[str(v) for v in data.get("allowed_tools") or []],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ScheduleDefinition:
    id: str
    name: str
    description: str
    owner_user_id: str
    workspace_dir: str
    session_id: str | None
    timezone: str
    schedule: str
    schedule_kind: str
    next_run_at: str | None
    task_kind: str
    task_payload: dict[str, Any]
    policy: TaskPolicy
    notification_target: NotificationTarget
    approval_required: bool
    enabled: bool
    created_at: str
    updated_at: str
    last_run_at: str | None = None
    last_result: dict[str, Any] | None = None
    failure_count: int = 0
    paused: bool = False
    run_history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ScheduleDefinition":
        return cls(
            id=str(raw["id"]),
            name=str(raw.get("name") or raw["id"]),
            description=str(raw.get("description") or ""),
            owner_user_id=str(raw.get("owner_user_id") or "unknown"),
            workspace_dir=str(raw.get("workspace_dir") or "."),
            session_id=(
                str(raw.get("session_id"))
                if raw.get("session_id") is not None
                else None
            ),
            timezone=str(raw.get("timezone") or "local"),
            schedule=str(raw.get("schedule") or ""),
            schedule_kind=str(raw.get("schedule_kind") or ScheduleKind.CRON.value),
            next_run_at=(
                str(raw.get("next_run_at"))
                if raw.get("next_run_at") is not None
                else None
            ),
            task_kind=str(raw.get("task_kind") or TaskKind.REMINDER.value),
            task_payload=dict(raw.get("task_payload") or {}),
            policy=TaskPolicy.from_raw(raw.get("policy"), task_kind=raw.get("task_kind")),
            notification_target=NotificationTarget.from_raw(raw.get("notification_target")),
            approval_required=bool(raw.get("approval_required", False)),
            enabled=bool(raw.get("enabled", True)),
            created_at=str(raw.get("created_at") or utc_now_iso()),
            updated_at=str(raw.get("updated_at") or utc_now_iso()),
            last_run_at=(
                str(raw.get("last_run_at"))
                if raw.get("last_run_at") is not None
                else None
            ),
            last_result=(
                dict(raw.get("last_result"))
                if isinstance(raw.get("last_result"), dict)
                else None
            ),
            failure_count=int(raw.get("failure_count") or 0),
            paused=bool(raw.get("paused", False)),
            run_history=[
                dict(item) for item in raw.get("run_history") or [] if isinstance(item, dict)
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["policy"] = self.policy.to_dict()
        data["notification_target"] = self.notification_target.to_dict()
        return data

    def visible_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        data["run_history"] = data["run_history"][-5:]
        return data


@dataclass(slots=True)
class ScheduledRun:
    id: str
    schedule_id: str
    due_at: str
    fired_at: str
    status: str
    task_kind: str
    task_payload: dict[str, Any]
    approval_required: bool
    notification_target: NotificationTarget
    task_id: str | None = None
    approval_request_id: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["notification_target"] = self.notification_target.to_dict()
        return data


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def generate_schedule_id() -> str:
    return "cron_" + "".join(secrets.choice(SCHEDULE_ID_ALPHABET) for _ in range(8))


def generate_run_id(schedule_id: str, due_at: str | None = None) -> str:
    digest = hashlib.sha256(f"{schedule_id}:{due_at or time.time_ns()}".encode()).hexdigest()
    return "run_" + digest[:12]


__all__ = [
    "NotificationTarget",
    "RunStatus",
    "ScheduleDefinition",
    "ScheduleKind",
    "ScheduledRun",
    "TaskKind",
    "TaskPolicy",
    "generate_run_id",
    "generate_schedule_id",
    "utc_now",
    "utc_now_iso",
]
