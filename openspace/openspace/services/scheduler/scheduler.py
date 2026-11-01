from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .events import (
    ApprovalService,
    EventSink,
    NotificationService,
    emit_event,
)
from .models import (
    NotificationTarget,
    RunStatus,
    ScheduleDefinition,
    ScheduleKind,
    ScheduledRun,
    TaskKind,
    TaskPolicy,
    generate_run_id,
    generate_schedule_id,
    utc_now,
)
from .store import ScheduleStore


MAX_JOBS = 50
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_FAILURE_PAUSE_THRESHOLD = 5
DEFAULT_MIN_INTERVAL_SECONDS = 60

FIELD_RANGES = (
    (0, 59),
    (0, 23),
    (1, 31),
    (1, 12),
    (0, 6),
)


@dataclass(slots=True)
class CronFields:
    minute: list[int]
    hour: list[int]
    day_of_month: list[int]
    month: list[int]
    day_of_week: list[int]


@dataclass(slots=True)
class ScheduleCreateRequest:
    schedule: str
    schedule_kind: str
    prompt: str
    name: str = ""
    description: str = ""
    owner_user_id: str = "unknown"
    workspace_dir: str = "."
    session_id: str | None = None
    timezone: str = "local"
    recurring: bool = True
    task_kind: str = TaskKind.REMINDER.value
    task_payload: dict[str, Any] | None = None
    policy: TaskPolicy | None = None
    notification_target: NotificationTarget | None = None
    approval_required: bool | None = None


class ScheduleValidationError(ValueError):
    def __init__(self, message: str, error_code: int = 1) -> None:
        super().__init__(message)
        self.error_code = error_code


def _expand_field(field: str, min_value: int, max_value: int) -> list[int] | None:
    values: set[int] = set()
    for part in field.split(","):
        step_match = re.fullmatch(r"\*(?:/(\d+))?", part)
        if step_match:
            step = int(step_match.group(1) or "1")
            if step < 1:
                return None
            values.update(range(min_value, max_value + 1, step))
            continue

        range_match = re.fullmatch(r"(\d+)-(\d+)(?:/(\d+))?", part)
        if range_match:
            lo = int(range_match.group(1))
            hi = int(range_match.group(2))
            step = int(range_match.group(3) or "1")
            is_dow = min_value == 0 and max_value == 6
            effective_max = 7 if is_dow else max_value
            if lo > hi or step < 1 or lo < min_value or hi > effective_max:
                return None
            for value in range(lo, hi + 1, step):
                values.add(0 if is_dow and value == 7 else value)
            continue

        if re.fullmatch(r"\d+", part):
            value = int(part)
            if min_value == 0 and max_value == 6 and value == 7:
                value = 0
            if value < min_value or value > max_value:
                return None
            values.add(value)
            continue

        return None
    if not values:
        return None
    return sorted(values)


def parse_cron_expression(expr: str) -> CronFields | None:
    parts = str(expr or "").strip().split()
    if len(parts) != 5:
        return None
    expanded: list[list[int]] = []
    for part, (min_value, max_value) in zip(parts, FIELD_RANGES):
        values = _expand_field(part, min_value, max_value)
        if values is None:
            return None
        expanded.append(values)
    return CronFields(
        minute=expanded[0],
        hour=expanded[1],
        day_of_month=expanded[2],
        month=expanded[3],
        day_of_week=expanded[4],
    )


def _coerce_timezone(tz_name: str | None) -> timezone | ZoneInfo:
    if not tz_name or tz_name == "local":
        return datetime.now().astimezone().tzinfo or timezone.utc
    if tz_name.upper() == "UTC":
        return timezone.utc
    return ZoneInfo(tz_name)


def _parse_datetime(value: str, tz_name: str | None = None) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_coerce_timezone(tz_name))
    return dt.astimezone(timezone.utc)


def _matches_cron(fields: CronFields, candidate: datetime) -> bool:
    dom_wild = len(fields.day_of_month) == 31
    dow_wild = len(fields.day_of_week) == 7
    dow = (candidate.weekday() + 1) % 7
    if dom_wild and dow_wild:
        day_matches = True
    elif dom_wild:
        day_matches = dow in fields.day_of_week
    elif dow_wild:
        day_matches = candidate.day in fields.day_of_month
    else:
        day_matches = candidate.day in fields.day_of_month or dow in fields.day_of_week
    return (
        candidate.minute in fields.minute
        and candidate.hour in fields.hour
        and candidate.month in fields.month
        and day_matches
    )


def compute_next_cron_run(
    cron: str,
    from_dt: datetime | None = None,
    *,
    tz_name: str | None = None,
) -> datetime | None:
    fields = parse_cron_expression(cron)
    if fields is None:
        return None
    tz = _coerce_timezone(tz_name)
    base = (from_dt or utc_now()).astimezone(tz)
    candidate = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if candidate.month not in fields.month:
            month = candidate.month + 1
            year = candidate.year
            if month > 12:
                month = 1
                year += 1
            candidate = candidate.replace(year=year, month=month, day=1, hour=0, minute=0)
            continue
        if not _matches_cron(fields, candidate):
            candidate = candidate + timedelta(minutes=1)
            continue
        return candidate.astimezone(timezone.utc)
    return None


def cron_to_human(cron: str) -> str:
    parts = str(cron or "").strip().split()
    if len(parts) != 5:
        return cron
    minute, hour, day_of_month, month, day_of_week = parts
    every_min = re.fullmatch(r"\*/(\d+)", minute)
    if every_min and hour == day_of_month == month == day_of_week == "*":
        value = int(every_min.group(1))
        return "Every minute" if value == 1 else f"Every {value} minutes"
    if re.fullmatch(r"\d+", minute) and hour == "*" and day_of_month == month == day_of_week == "*":
        value = int(minute)
        return "Every hour" if value == 0 else f"Every hour at :{value:02d}"
    every_hour = re.fullmatch(r"\*/(\d+)", hour)
    if re.fullmatch(r"\d+", minute) and every_hour and day_of_month == month == day_of_week == "*":
        value = int(every_hour.group(1))
        suffix = "" if int(minute) == 0 else f" at :{int(minute):02d}"
        return f"Every hour{suffix}" if value == 1 else f"Every {value} hours{suffix}"
    if not re.fullmatch(r"\d+", minute) or not re.fullmatch(r"\d+", hour):
        return cron
    m = int(minute)
    h = int(hour)
    display_time = datetime(2000, 1, 1, h, m).strftime("%I:%M %p").lstrip("0")
    if day_of_month == month == day_of_week == "*":
        return f"Every day at {display_time}"
    day_names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    if day_of_month == "*" and month == "*" and re.fullmatch(r"\d", day_of_week):
        return f"Every {day_names[int(day_of_week) % 7]} at {display_time}"
    if day_of_month == "*" and month == "*" and day_of_week == "1-5":
        return f"Weekdays at {display_time}"
    return cron


def next_run_for_schedule(
    schedule: str,
    schedule_kind: str,
    *,
    from_dt: datetime | None = None,
    tz_name: str | None = None,
) -> datetime | None:
    if schedule_kind == ScheduleKind.ONE_SHOT.value:
        run_at = _parse_datetime(schedule, tz_name)
        return run_at if run_at > (from_dt or utc_now()) else None
    return compute_next_cron_run(schedule, from_dt, tz_name=tz_name)


class ScheduleScheduler:
    """OpenSpace long-horizon schedule runtime."""

    def __init__(
        self,
        store: ScheduleStore,
        *,
        event_sink: EventSink | None = None,
        notification_service: NotificationService | None = None,
        approval_service: ApprovalService | None = None,
        task_manager: Any | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        failure_pause_threshold: int = DEFAULT_FAILURE_PAUSE_THRESHOLD,
        min_interval_seconds: int = DEFAULT_MIN_INTERVAL_SECONDS,
        clock: Any | None = None,
    ) -> None:
        self.store = store
        self.event_sink = event_sink
        self.notification_service = notification_service or NotificationService(event_sink)
        self.approval_service = approval_service or ApprovalService(event_sink)
        self.task_manager = task_manager
        self.poll_interval_seconds = poll_interval_seconds
        self.failure_pause_threshold = failure_pause_threshold
        self.min_interval_seconds = min_interval_seconds
        self.clock = clock
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._in_flight: set[str] = set()

    def now(self) -> datetime:
        if self.clock is not None:
            value = self.clock()
            if isinstance(value, datetime):
                return value.astimezone(timezone.utc)
        return utc_now()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stopped.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        while not self._stopped.is_set():
            await self.check_due()
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    def create_schedule(self, request: ScheduleCreateRequest) -> ScheduleDefinition:
        schedules = self.store.load()
        if len(schedules) >= MAX_JOBS:
            raise ScheduleValidationError(f"Too many scheduled jobs (max {MAX_JOBS}). Cancel one first.", 3)

        schedule_kind = request.schedule_kind
        if schedule_kind not in {ScheduleKind.CRON.value, ScheduleKind.ONE_SHOT.value}:
            raise ScheduleValidationError("schedule_kind must be cron or one_shot.", 1)
        supported_task_kinds = {item.value for item in TaskKind}
        if request.task_kind not in supported_task_kinds:
            raise ScheduleValidationError(
                f"task_kind must be one of: {', '.join(sorted(supported_task_kinds))}.",
                1,
            )
        next_run = next_run_for_schedule(
            request.schedule,
            schedule_kind,
            from_dt=self.now(),
            tz_name=request.timezone,
        )
        if next_run is None:
            if schedule_kind == ScheduleKind.CRON.value:
                if parse_cron_expression(request.schedule) is None:
                    raise ScheduleValidationError(
                        f"Invalid cron expression '{request.schedule}'. Expected 5 fields: M H DoM Mon DoW.",
                        1,
                    )
                raise ScheduleValidationError(
                    f"Cron expression '{request.schedule}' does not match any calendar date in the next year.",
                    2,
                )
            raise ScheduleValidationError("One-shot run_at must be in the future.", 2)

        policy = request.policy or TaskPolicy.from_raw(
            None,
            task_kind=request.task_kind,
            approval_required=request.approval_required,
        )
        if policy.task_kind not in supported_task_kinds:
            raise ScheduleValidationError(
                f"policy.task_kind must be one of: {', '.join(sorted(supported_task_kinds))}.",
                1,
            )
        approval_required = policy.approval_required or bool(request.approval_required)

        now_iso = self.now().isoformat()
        schedule = ScheduleDefinition(
            id=generate_schedule_id(),
            name=request.name.strip() or request.prompt[:60] or "Scheduled task",
            description=request.description,
            owner_user_id=request.owner_user_id,
            workspace_dir=request.workspace_dir,
            session_id=request.session_id,
            timezone=request.timezone,
            schedule=request.schedule,
            schedule_kind=schedule_kind,
            next_run_at=next_run.isoformat(),
            task_kind=policy.task_kind,
            task_payload=request.task_payload or {"prompt": request.prompt},
            policy=policy,
            notification_target=request.notification_target or NotificationTarget(),
            approval_required=approval_required,
            enabled=True,
            created_at=now_iso,
            updated_at=now_iso,
        )
        schedules.append(schedule)
        self.store.save(schedules)
        return schedule

    def delete_schedule(self, schedule_id: str) -> bool:
        return self.store.delete(schedule_id)

    def list_schedules(
        self,
        *,
        workspace_dir: str | None = None,
        session_id: str | None = None,
        owner_user_id: str | None = None,
    ) -> list[ScheduleDefinition]:
        schedules = self.store.load()
        return [
            schedule
            for schedule in schedules
            if (workspace_dir is None or schedule.workspace_dir == workspace_dir)
            and (session_id is None or schedule.session_id == session_id)
            and (owner_user_id is None or schedule.owner_user_id == owner_user_id)
        ]

    async def check_due(self) -> list[ScheduledRun]:
        now = self.now()
        schedules = self.store.load()
        updated: list[ScheduleDefinition] = []
        runs: list[ScheduledRun] = []
        changed = False

        for schedule in schedules:
            if not schedule.enabled or schedule.paused or not schedule.next_run_at:
                updated.append(schedule)
                continue
            try:
                due_at = _parse_datetime(schedule.next_run_at)
            except Exception:
                schedule.paused = True
                schedule.last_result = {"status": RunStatus.FAILED.value, "error": "invalid next_run_at"}
                changed = True
                updated.append(schedule)
                continue
            if due_at > now or schedule.id in self._in_flight:
                updated.append(schedule)
                continue

            self._in_flight.add(schedule.id)
            try:
                run = await self._fire_schedule(schedule, due_at)
                runs.append(run)
                schedule.last_run_at = run.fired_at
                schedule.last_result = run.to_dict()
                schedule.failure_count = 0 if run.status in {RunStatus.NOTIFIED.value, RunStatus.APPROVAL_REQUESTED.value, RunStatus.STARTED.value} else schedule.failure_count + 1
                schedule.run_history.append(run.to_dict())
                schedule.run_history = schedule.run_history[-20:]
                schedule.updated_at = now.isoformat()
                next_run = None
                if schedule.schedule_kind == ScheduleKind.CRON.value:
                    next_run = next_run_for_schedule(
                        schedule.schedule,
                        schedule.schedule_kind,
                        from_dt=now,
                        tz_name=schedule.timezone,
                    )
                if next_run is not None:
                    schedule.next_run_at = next_run.isoformat()
                else:
                    schedule.enabled = False
                    schedule.next_run_at = None
                if schedule.failure_count >= self.failure_pause_threshold:
                    schedule.paused = True
                changed = True
            finally:
                self._in_flight.discard(schedule.id)
            updated.append(schedule)

        if changed:
            self.store.save(updated)
        return runs

    async def _fire_schedule(self, schedule: ScheduleDefinition, due_at: datetime) -> ScheduledRun:
        run = ScheduledRun(
            id=generate_run_id(schedule.id, due_at.isoformat()),
            schedule_id=schedule.id,
            due_at=due_at.isoformat(),
            fired_at=self.now().isoformat(),
            status=RunStatus.DUE.value,
            task_kind=schedule.task_kind,
            task_payload=dict(schedule.task_payload),
            approval_required=schedule.approval_required,
            notification_target=schedule.notification_target,
        )
        await emit_event(
            self.event_sink,
            "cron_due",
            {"run": run.to_dict(), "schedule": schedule.visible_dict()},
        )

        if schedule.approval_required:
            request = await self.approval_service.request_approval(
                run,
                schedule,
                reason="Scheduled task requires approval before execution.",
            )
            run.approval_request_id = request.id
            run.status = RunStatus.APPROVAL_REQUESTED.value
            return run

        if schedule.task_kind == TaskKind.REMINDER.value:
            await self.notification_service.notify(run, schedule)
            run.status = RunStatus.NOTIFIED.value
            return run

        if schedule.task_kind == TaskKind.READ_ONLY_AGENT.value:
            task_id = await self._start_read_only_agent(run, schedule)
            if task_id:
                run.task_id = task_id
                run.status = RunStatus.STARTED.value
            else:
                run.status = RunStatus.APPROVAL_REQUESTED.value
                request = await self.approval_service.request_approval(
                    run,
                    schedule,
                    reason="No read-only background runtime is attached; queued for approval/control.",
                )
                run.approval_request_id = request.id
            return run

        run.status = RunStatus.SKIPPED.value
        run.error = f"Unsupported task_kind: {schedule.task_kind}"
        return run

    async def _start_read_only_agent(
        self,
        run: ScheduledRun,
        schedule: ScheduleDefinition,
    ) -> str | None:
        if self.task_manager is None:
            return None
        runner = getattr(self.task_manager, "register_async_agent", None)
        if runner is None:
            return None
        prompt = str(run.task_payload.get("prompt") or "")
        try:
            task = await runner(
                prompt=prompt,
                description=schedule.name,
                agent_type="general-purpose",
                model=None,
                selected_agent=None,
                tool_use_id=run.id,
                parent_task_id=None,
            )
            task_id = getattr(task, "id", None) or getattr(task, "task_id", None)
            await emit_event(
                self.event_sink,
                "cron_task_started",
                {"run": run.to_dict(), "schedule": schedule.visible_dict(), "task_id": task_id},
            )
            return str(task_id) if task_id else None
        except TypeError:
            return None
        except Exception as exc:
            await emit_event(
                self.event_sink,
                "cron_task_failed",
                {
                    "run": run.to_dict(),
                    "schedule": schedule.visible_dict(),
                    "error": str(exc),
                },
            )
            return None


def create_scheduler_for_workspace(
    workspace_dir: str | os.PathLike[str],
    *,
    event_sink: EventSink | None = None,
    task_manager: Any | None = None,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> ScheduleScheduler:
    return ScheduleScheduler(
        ScheduleStore.for_workspace(workspace_dir),
        event_sink=event_sink,
        task_manager=task_manager,
        poll_interval_seconds=poll_interval_seconds,
    )


def ensure_schedule_owner(value: Any) -> str:
    raw = str(value or "").strip()
    return raw or "unknown"


def schedule_summary(schedule: ScheduleDefinition) -> str:
    if schedule.schedule_kind == ScheduleKind.CRON.value:
        human = cron_to_human(schedule.schedule)
    else:
        human = f"Once at {schedule.schedule}"
    state = "paused" if schedule.paused else ("enabled" if schedule.enabled else "disabled")
    return f"{schedule.id} - {human} [{state}] {schedule.task_kind}: {schedule.name}"


__all__ = [
    "MAX_JOBS",
    "ScheduleCreateRequest",
    "ScheduleScheduler",
    "ScheduleValidationError",
    "compute_next_cron_run",
    "create_scheduler_for_workspace",
    "cron_to_human",
    "ensure_schedule_owner",
    "next_run_for_schedule",
    "parse_cron_expression",
    "schedule_summary",
]
