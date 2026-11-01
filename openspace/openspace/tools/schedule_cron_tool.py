from __future__ import annotations

import inspect
from typing import Any, Mapping

from openspace.grounding.core.permissions.types import PermissionAllow, PermissionAsk
from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolSchema, ToolStatus
from openspace.services.scheduler import (
    NotificationTarget,
    ScheduleCreateRequest,
    ScheduleKind,
    ScheduleScheduler,
    ScheduleValidationError,
    TaskKind,
    TaskPolicy,
    cron_to_human,
    create_scheduler_for_workspace,
    next_run_for_schedule,
    schedule_summary,
)


SCHEDULE_CRON_CREATE_TOOL_NAME = "schedule_cron_create"
SCHEDULE_CRON_DELETE_TOOL_NAME = "schedule_cron_delete"
SCHEDULE_CRON_LIST_TOOL_NAME = "schedule_cron_list"
CRON_CREATE_TOOL_ALIAS = "CronCreate"
CRON_DELETE_TOOL_ALIAS = "CronDelete"
CRON_LIST_TOOL_ALIAS = "CronList"


def _is_scheduler(value: Any) -> bool:
    return isinstance(value, ScheduleScheduler) or (
        value is not None
        and callable(getattr(value, "create_schedule", None))
        and callable(getattr(value, "delete_schedule", None))
        and callable(getattr(value, "list_schedules", None))
    )


def _attach_scheduler(context: Any, scheduler: Any) -> None:
    if context is None:
        return
    try:
        context.scheduler = scheduler
        context.notification_service = getattr(scheduler, "notification_service", None)
        context.approval_service = getattr(scheduler, "approval_service", None)
    except Exception:
        pass


async def _scheduler_from_context(
    context: Any = None,
    *,
    ensure: bool = False,
    start_if_created: bool = False,
) -> Any:
    scheduler = getattr(context, "scheduler", None) if context is not None else None
    if _is_scheduler(scheduler):
        return scheduler
    if ensure and context is not None:
        ensure_scheduler = getattr(context, "ensure_scheduler", None)
        if callable(ensure_scheduler):
            try:
                scheduler = ensure_scheduler(context)
            except TypeError as exc:
                try:
                    scheduler = ensure_scheduler()
                except TypeError:
                    raise exc
            if inspect.isawaitable(scheduler):
                scheduler = await scheduler
            if _is_scheduler(scheduler):
                _attach_scheduler(context, scheduler)
                return scheduler
    cwd = str(getattr(context, "cwd", None) or ".")
    event_sink = getattr(context, "event_sink", None)
    task_manager = getattr(context, "task_manager", None)
    scheduler = create_scheduler_for_workspace(
        cwd,
        event_sink=event_sink,
        task_manager=task_manager,
    )
    if start_if_created:
        await scheduler.start()
        _attach_scheduler(context, scheduler)
    return scheduler


def _input_get(input_data: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in input_data and input_data.get(name) is not None:
            return input_data.get(name)
    return None


def _default_notification_target(context: Any = None) -> NotificationTarget:
    channel_context = getattr(context, "channel_context", None)
    if isinstance(channel_context, dict) and channel_context.get("platform"):
        metadata = {
            key: value
            for key, value in channel_context.items()
            if key not in {"platform", "chat_id"}
        }
        return NotificationTarget(
            channel=str(channel_context.get("platform") or "in_process"),
            identity=(
                str(channel_context.get("chat_id"))
                if channel_context.get("chat_id") is not None
                else None
            ),
            metadata=metadata,
        )
    return NotificationTarget()


def _create_parameters_schema() -> dict[str, Any]:
    task_kinds = [item.value for item in TaskKind]
    return {
        "type": "object",
        "properties": {
            "cron": {
                "type": "string",
                "description": "Standard 5-field cron expression in local time: M H DoM Mon DoW.",
            },
            "run_at": {
                "type": "string",
                "description": "ISO datetime for a one-shot schedule. Naive values use timezone.",
            },
            "prompt": {
                "type": "string",
                "description": "Prompt, reminder text, or read-only task instruction to trigger.",
            },
            "recurring": {
                "type": "boolean",
                "description": "For cron schedules, true means repeat until deleted; false means fire once.",
            },
            "name": {"type": "string"},
            "description": {"type": "string"},
            "timezone": {"type": "string", "description": "IANA timezone name or UTC/local."},
            "task_kind": {"type": "string", "enum": task_kinds},
            "policy": {"type": "object", "additionalProperties": True},
            "notification_target": {"type": "object", "additionalProperties": True},
            "approval_required": {"type": "boolean"},
            "durable": {
                "type": "boolean",
                "description": "Accepted for older transcripts. OpenSpace schedules are always persisted.",
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    }


def _delete_parameters_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Schedule ID returned by schedule_cron_create."},
        },
        "required": ["id"],
        "additionalProperties": False,
    }


def _list_parameters_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "include_disabled": {"type": "boolean", "default": False},
            "workspace_only": {"type": "boolean", "default": True},
            "session_only": {"type": "boolean", "default": False},
        },
        "additionalProperties": False,
    }


CREATE_PROMPT = """Schedule a prompt or reminder to trigger in the future.

OpenSpace schedules are persisted in the workspace `.openspace/scheduled_tasks.json` file and are owned by the current workspace/session. Use `cron` for recurring schedules and `run_at` for one-shot reminders. Cron syntax is the standard 5-field form: minute hour day-of-month month day-of-week.

Task policy:
- `reminder` sends a notification/runtime event at the due time.
- `read_only_agent` may start a restricted background agent only when a runtime TaskManager is attached; otherwise it creates a pending approval/control record.

Avoid `:00` and `:30` when the user's request is approximate; pick a nearby off-minute to reduce synchronized load. Returns a schedule ID for schedule_cron_delete."""


class _BaseScheduleCronTool(BaseTool):
    backend_type = BackendType.META
    should_defer = True
    max_result_size_chars = 100_000

    def __init__(self, *, schema: ToolSchema) -> None:
        self._current_context: Any | None = None
        super().__init__(schema=schema)

    def set_context(self, context: Any) -> None:
        self._current_context = context

    async def check_permissions(self, input: dict[str, Any], context: Any = None) -> PermissionAllow | PermissionAsk:
        return PermissionAllow(updated_input=dict(input))

    async def _arun(self, **kwargs: Any) -> ToolResult:
        return ToolResult(
            status=ToolStatus.ERROR,
            content="Schedule cron base tool cannot be executed directly.",
            error="Schedule cron base tool cannot be executed directly.",
        )


class ScheduleCronCreateTool(_BaseScheduleCronTool):
    _name = SCHEDULE_CRON_CREATE_TOOL_NAME
    _description = "Schedule a future reminder or read-only agent task."
    aliases = [CRON_CREATE_TOOL_ALIAS, "ScheduleCronCreate", "CronCreateTool"]
    search_hint = "schedule cron reminder future recurring task"

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name=self._name,
                description=self._description,
                parameters=_create_parameters_schema(),
                backend_type=self.backend_type,
            )
        )

    def get_prompt(self, context: Any = None) -> str:
        return CREATE_PROMPT

    async def validate_input(self, input: dict[str, Any], context: Any = None) -> str | None:
        has_cron = bool(str(input.get("cron") or "").strip())
        has_run_at = bool(str(input.get("run_at") or "").strip())
        if has_cron == has_run_at:
            return "Provide exactly one of cron or run_at."
        if not str(input.get("prompt") or "").strip():
            return "prompt is required."
        schedule_kind = ScheduleKind.CRON.value if has_cron else ScheduleKind.ONE_SHOT.value
        schedule = str(input.get("cron") if has_cron else input.get("run_at"))
        try:
            if next_run_for_schedule(
                schedule,
                schedule_kind,
                tz_name=str(input.get("timezone") or "local"),
            ) is None:
                return "Schedule does not produce a future run time."
        except Exception as exc:
            return str(exc)
        return None

    async def _arun(
        self,
        prompt: str,
        cron: str | None = None,
        run_at: str | None = None,
        recurring: bool | None = None,
        name: str | None = None,
        description: str | None = None,
        timezone: str | None = None,
        task_kind: str | None = None,
        policy: dict[str, Any] | None = None,
        notification_target: dict[str, Any] | str | None = None,
        approval_required: bool | None = None,
        durable: bool | None = None,
    ) -> ToolResult:
        has_cron = bool(str(cron or "").strip())
        schedule_kind = ScheduleKind.CRON.value if has_cron else ScheduleKind.ONE_SHOT.value
        schedule = str(cron if has_cron else run_at or "").strip()
        context = self._current_context
        scheduler = await _scheduler_from_context(
            context,
            ensure=True,
            start_if_created=True,
        )
        owner = str(getattr(context, "agent_id", None) or "primary")
        workspace_dir = str(getattr(context, "cwd", None) or ".")
        session_id = getattr(context, "session_id", None)
        try:
            schedule_def = scheduler.create_schedule(
                ScheduleCreateRequest(
                    schedule=schedule,
                    schedule_kind=schedule_kind,
                    prompt=prompt,
                    name=name or "",
                    description=description or "",
                    owner_user_id=owner,
                    workspace_dir=workspace_dir,
                    session_id=str(session_id) if session_id is not None else None,
                    timezone=timezone or "local",
                    recurring=True if recurring is None else bool(recurring),
                    task_kind=task_kind or TaskKind.REMINDER.value,
                    policy=TaskPolicy.from_raw(
                        policy,
                        task_kind=task_kind,
                        approval_required=approval_required,
                    ),
                    notification_target=(
                        NotificationTarget.from_raw(notification_target)
                        if notification_target is not None
                        else _default_notification_target(context)
                    ),
                    approval_required=approval_required,
                )
            )
        except ScheduleValidationError as exc:
            return ToolResult(status=ToolStatus.ERROR, content=str(exc), error=str(exc))

        data = schedule_def.visible_dict()
        data["durable"] = True
        data["durable_requested"] = durable
        human = cron_to_human(schedule_def.schedule) if schedule_kind == ScheduleKind.CRON.value else f"Once at {schedule_def.schedule}"
        content = (
            f"Scheduled {schedule_def.task_kind} job {schedule_def.id} ({human}). "
            "Persisted to .openspace/scheduled_tasks.json. "
            f"Next run: {schedule_def.next_run_at}."
        )
        if schedule_def.approval_required:
            content += " This schedule requires approval before execution."
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=content,
            metadata={"tool": self.name, "data": data},
        )


class ScheduleCronDeleteTool(_BaseScheduleCronTool):
    _name = SCHEDULE_CRON_DELETE_TOOL_NAME
    _description = "Cancel a scheduled cron job by ID."
    aliases = [CRON_DELETE_TOOL_ALIAS, "ScheduleCronDelete", "CronDeleteTool"]
    search_hint = "cancel delete scheduled cron job"

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name=self._name,
                description=self._description,
                parameters=_delete_parameters_schema(),
                backend_type=self.backend_type,
            )
        )

    def get_prompt(self, context: Any = None) -> str:
        return "Cancel a schedule previously created with schedule_cron_create."

    async def validate_input(self, input: dict[str, Any], context: Any = None) -> str | None:
        schedule_id = str(input.get("id") or "").strip()
        if not schedule_id:
            return "id is required."
        scheduler = await _scheduler_from_context(context or self._current_context)
        if scheduler.store.get(schedule_id) is None:
            return f"No scheduled job with id '{schedule_id}'"
        return None

    async def _arun(self, id: str) -> ToolResult:
        scheduler = await _scheduler_from_context(self._current_context)
        deleted = scheduler.delete_schedule(id)
        if not deleted:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"No scheduled job with id '{id}'",
                error=f"No scheduled job with id '{id}'",
            )
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=f"Cancelled job {id}.",
            metadata={"tool": self.name, "data": {"id": id}},
        )


class ScheduleCronListTool(_BaseScheduleCronTool):
    _name = SCHEDULE_CRON_LIST_TOOL_NAME
    _description = "List scheduled cron jobs for the current workspace/session."
    aliases = [CRON_LIST_TOOL_ALIAS, "ScheduleCronList", "CronListTool"]
    search_hint = "list scheduled cron jobs reminders"
    _is_read_only = True
    _is_concurrency_safe = True

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name=self._name,
                description=self._description,
                parameters=_list_parameters_schema(),
                backend_type=self.backend_type,
            )
        )

    def get_prompt(self, context: Any = None) -> str:
        return "List schedules created by schedule_cron_create."

    async def _arun(
        self,
        include_disabled: bool = False,
        workspace_only: bool = True,
        session_only: bool = False,
    ) -> ToolResult:
        context = self._current_context
        scheduler = await _scheduler_from_context(context)
        workspace_dir = str(getattr(context, "cwd", None) or ".") if workspace_only else None
        session_id = getattr(context, "session_id", None) if session_only else None
        schedules = scheduler.list_schedules(
            workspace_dir=workspace_dir,
            session_id=str(session_id) if session_id is not None else None,
        )
        if not include_disabled:
            schedules = [schedule for schedule in schedules if schedule.enabled and not schedule.paused]
        jobs = [schedule.visible_dict() for schedule in schedules]
        content = "\n".join(schedule_summary(schedule) for schedule in schedules) if schedules else "No scheduled jobs."
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=content,
            metadata={"tool": self.name, "data": {"jobs": jobs}},
        )


__all__ = [
    "CRON_CREATE_TOOL_ALIAS",
    "CRON_DELETE_TOOL_ALIAS",
    "CRON_LIST_TOOL_ALIAS",
    "SCHEDULE_CRON_CREATE_TOOL_NAME",
    "SCHEDULE_CRON_DELETE_TOOL_NAME",
    "SCHEDULE_CRON_LIST_TOOL_NAME",
    "ScheduleCronCreateTool",
    "ScheduleCronDeleteTool",
    "ScheduleCronListTool",
]
