"""Query-level background housekeeping.

Background tasks are routed through TaskManager and the TUI
``agent_task_update`` surface so startup, drain, and cancellation behavior stays
observable.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from openspace.agents.task_manager import (
    StopTaskError,
    TaskManager,
    TaskType,
    generate_task_id,
)
from openspace.services.memory.background import (
    MemoryDrainResult,
    drain_pending_memory_tasks,
)
from openspace.services.memory.task_scope import maybe_memory_task_scope_key

logger = logging.getLogger(__name__)

BackgroundEventSink = Callable[[str, dict[str, Any]], Awaitable[None] | None]

DEFAULT_IDLE_GRACE_S = 60.0
DEFAULT_SLOW_OP_DELAY_S = 10 * 60.0
DEFAULT_RECURRING_CLEANUP_INTERVAL_S = 24 * 60 * 60.0


@dataclass(slots=True)
class BackgroundTaskRecord:
    task_id: str
    name: str
    source: str
    task_type: str
    description: str
    task: asyncio.Task[Any]
    scope_key: str | None
    session_id: str | None = None
    parent_task_id: str | None = None
    root_task_id: str | None = None
    event_sink: BackgroundEventSink | None = None
    task_manager: TaskManager | None = None
    started_at: float = field(default_factory=lambda: time.time() * 1000)
    status: str = "running"


@dataclass(slots=True)
class BackgroundHousekeepingState:
    started: bool = False
    started_at: float = 0.0
    last_interaction_at: float = field(default_factory=time.time)
    idle_grace_s: float = DEFAULT_IDLE_GRACE_S
    slow_op_delay_s: float = DEFAULT_SLOW_OP_DELAY_S
    recurring_cleanup_interval_s: float = DEFAULT_RECURRING_CLEANUP_INTERVAL_S
    slow_ops_task: asyncio.Task[Any] | None = None
    recurring_task: asyncio.Task[Any] | None = None


@dataclass(frozen=True, slots=True)
class BackgroundDrainResult:
    timeout_s: float
    memory: MemoryDrainResult
    tracked_pending: int = 0

    @property
    def pending_count(self) -> int:
        return self.memory.pending_count + self.tracked_pending

    @property
    def timed_out(self) -> bool:
        return self.pending_count > 0

    def as_event_payload(self) -> dict[str, Any]:
        payload = self.memory.as_event_payload()
        payload["tracked_pending"] = self.tracked_pending
        payload["pending_count"] = self.pending_count
        return payload


@dataclass(slots=True)
class BackgroundQueueStats:
    source: str
    queued_count: int = 0
    running_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    cancelled_count: int = 0
    backlog_warning_count: int = 0
    latest_error: str | None = None
    latest_queue_lag_ms: float = 0.0
    latest_backlog_count: int = 0
    latest_started_at_ms: float = 0.0
    latest_finished_at_ms: float = 0.0

    def to_dict(self, *, backlog_warning_threshold: int | None = None) -> dict[str, Any]:
        backlogged = (
            backlog_warning_threshold is not None
            and backlog_warning_threshold > 0
            and self.queued_count >= backlog_warning_threshold
        )
        return {
            "source": self.source,
            "queued_count": self.queued_count,
            "running_count": self.running_count,
            "completed_count": self.completed_count,
            "failed_count": self.failed_count,
            "cancelled_count": self.cancelled_count,
            "backlog_warning_count": self.backlog_warning_count,
            "backlogged": backlogged,
            "latest_error": self.latest_error,
            "latest_queue_lag_ms": self.latest_queue_lag_ms,
            "latest_backlog_count": self.latest_backlog_count,
            "latest_started_at_ms": self.latest_started_at_ms,
            "latest_finished_at_ms": self.latest_finished_at_ms,
        }


class BackgroundSupervisor:
    """Small explicit supervisor around existing fire-and-forget tasks."""

    def __init__(
        self,
        *,
        max_concurrency_per_source: int = 1,
        backlog_warning_threshold: int | None = 50,
        event_sink: BackgroundEventSink | None = None,
    ) -> None:
        self.max_concurrency_per_source = max(1, int(max_concurrency_per_source))
        self.backlog_warning_threshold = (
            None
            if backlog_warning_threshold is None
            else max(0, int(backlog_warning_threshold))
        )
        self._event_sink = event_sink
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._stats: dict[str, BackgroundQueueStats] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    def submit(
        self,
        *,
        source: str,
        name: str,
        coro_factory: Callable[[], Awaitable[Any] | Any],
        context: Any | None = None,
        description: str = "",
        task_type: TaskType | str = "background",
        timeout_s: float | None = None,
        task_id: str | None = None,
    ) -> asyncio.Task[Any]:
        source_name = str(source or "background")
        resolved_task_id = task_id or generate_task_id(task_type)
        queued_at_ms = time.time() * 1000
        stats = self._stats_for(source_name)
        stats.queued_count += 1
        sink = self._event_sink or _event_sink_from_context(context)
        self._maybe_emit_backlog_warning(
            sink=sink,
            stats=stats,
            source=source_name,
            name=name,
            task_id=resolved_task_id,
            description=description,
        )

        async def _runner() -> Any:
            started = False
            try:
                semaphore = self._semaphore_for(source_name)
                async with semaphore:
                    started = True
                    stats.queued_count = max(0, stats.queued_count - 1)
                    stats.running_count += 1
                    stats.latest_started_at_ms = time.time() * 1000
                    stats.latest_queue_lag_ms = max(
                        0.0,
                        stats.latest_started_at_ms - queued_at_ms,
                    )
                    await _emit_event(
                        sink,
                        "background_lag",
                        {
                            "task_id": resolved_task_id,
                            "source": source_name,
                            "name": name,
                            "description": description,
                            "queue_lag_ms": stats.latest_queue_lag_ms,
                            "queued_count": stats.queued_count,
                            "running_count": stats.running_count,
                        },
                    )
                    await _emit_event(
                        sink,
                        "background.queue_lag",
                        {
                            "task_id": resolved_task_id,
                            "source": source_name,
                            "name": name,
                            "description": description,
                            "queue_lag_ms": stats.latest_queue_lag_ms,
                            "queued_count": stats.queued_count,
                            "running_count": stats.running_count,
                        },
                    )
                    try:
                        result = coro_factory()
                        if inspect.isawaitable(result):
                            awaitable = result
                        else:
                            async def _constant() -> Any:
                                return result

                            awaitable = _constant()
                        if timeout_s is not None:
                            result = await asyncio.wait_for(
                                awaitable,
                                timeout=max(0.0, float(timeout_s)),
                            )
                        else:
                            result = await awaitable
                    except asyncio.CancelledError:
                        stats.cancelled_count += 1
                        stats.latest_finished_at_ms = time.time() * 1000
                        await _emit_event(
                            sink,
                            "background_task_cancelled",
                            {
                                "task_id": resolved_task_id,
                                "source": source_name,
                                "name": name,
                            },
                        )
                        raise
                    except Exception as exc:
                        stats.failed_count += 1
                        stats.latest_error = str(exc)
                        stats.latest_finished_at_ms = time.time() * 1000
                        await _emit_event(
                            sink,
                            "background_task_failed",
                            {
                                "task_id": resolved_task_id,
                                "source": source_name,
                                "name": name,
                                "error": str(exc),
                            },
                        )
                        return {"error": str(exc)}
                    else:
                        stats.completed_count += 1
                        stats.latest_error = None
                        stats.latest_finished_at_ms = time.time() * 1000
                        await _emit_event(
                            sink,
                            "background_task_completed",
                            {
                                "task_id": resolved_task_id,
                                "source": source_name,
                                "name": name,
                            },
                        )
                        return result
                    finally:
                        stats.running_count = max(0, stats.running_count - 1)
            except asyncio.CancelledError:
                if not started:
                    stats.queued_count = max(0, stats.queued_count - 1)
                    stats.cancelled_count += 1
                    stats.latest_finished_at_ms = time.time() * 1000
                    await _emit_event(
                        sink,
                        "background_task_cancelled",
                        {
                            "task_id": resolved_task_id,
                            "source": source_name,
                            "name": name,
                        },
                    )
                raise
            finally:
                current = asyncio.current_task()
                if current is not None:
                    self._tasks.discard(current)

        task = asyncio.create_task(
            _runner(),
            name=f"openspace-background-{source_name}-{resolved_task_id}",
        )
        self._tasks.add(task)
        return task

    async def drain(self, *, timeout_s: float = 3.0) -> int:
        pending = [task for task in self._tasks if not task.done()]
        if not pending:
            return 0
        done, still_pending = await asyncio.wait(
            pending,
            timeout=max(0.0, float(timeout_s)),
        )
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("Background supervisor task failed during drain", exc_info=True)
        return len(still_pending)

    async def cancel_all(self, *, reason: str = "cancelled") -> int:
        count = 0
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
                count += 1
        if count:
            await _emit_event(
                self._event_sink,
                "background_supervisor_cancel_all",
                {"reason": reason, "count": count},
            )
        return count

    def status(self) -> dict[str, Any]:
        threshold = self.backlog_warning_threshold
        return {
            "tasks": sum(1 for task in self._tasks if not task.done()),
            "backlog_warning_threshold": threshold,
            "backlogged_sources": [
                source
                for source, stats in sorted(self._stats.items())
                if threshold is not None
                and threshold > 0
                and stats.queued_count >= threshold
            ],
            "queues": {
                source: stats.to_dict(backlog_warning_threshold=threshold)
                for source, stats in sorted(self._stats.items())
            },
        }

    def source_backlogged(self, source: str) -> bool:
        threshold = self.backlog_warning_threshold
        if threshold is None or threshold <= 0:
            return False
        stats = self._stats.get(str(source or "background"))
        return bool(stats is not None and stats.queued_count >= threshold)

    def _maybe_emit_backlog_warning(
        self,
        *,
        sink: BackgroundEventSink | None,
        stats: BackgroundQueueStats,
        source: str,
        name: str,
        task_id: str,
        description: str,
    ) -> None:
        threshold = self.backlog_warning_threshold
        if threshold is None or threshold <= 0 or stats.queued_count < threshold:
            return
        stats.backlog_warning_count += 1
        stats.latest_backlog_count = stats.queued_count
        asyncio.create_task(
            _emit_event(
                sink,
                "background_backlog_high",
                {
                    "task_id": task_id,
                    "source": source,
                    "name": name,
                    "description": description,
                    "queued_count": stats.queued_count,
                    "running_count": stats.running_count,
                    "threshold": threshold,
                },
            ),
            name=f"openspace-background-backlog-{source}-{task_id}",
        )

    def _stats_for(self, source: str) -> BackgroundQueueStats:
        stats = self._stats.get(source)
        if stats is None:
            stats = BackgroundQueueStats(source=source)
            self._stats[source] = stats
        return stats

    def _semaphore_for(self, source: str) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(source)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self.max_concurrency_per_source)
            self._semaphores[source] = semaphore
        return semaphore


_state = BackgroundHousekeepingState()
_tasks: dict[str, BackgroundTaskRecord] = {}
_supervisor = BackgroundSupervisor()


def get_background_supervisor() -> BackgroundSupervisor:
    return _supervisor


def start_background_housekeeping(
    context: Any | None = None,
    *,
    event_sink: BackgroundEventSink | None = None,
    idle_grace_s: float = DEFAULT_IDLE_GRACE_S,
    slow_op_delay_s: float = DEFAULT_SLOW_OP_DELAY_S,
    recurring_cleanup_interval_s: float = DEFAULT_RECURRING_CLEANUP_INTERVAL_S,
) -> BackgroundHousekeepingState:
    """Initialize background runners and idle cleanup timers.

    OpenSpace returns ``void`` and exposes no cleanup handle.  OS returns the state for
    tests while keeping the module-level singleton behavior.
    """

    if _state.started:
        record_interaction()
        return _state

    from openspace.services.memory.extract import init_extract_memories
    from openspace.services.memory.dream import init_auto_dream
    from openspace.services.memory.session_memory import init_session_memory

    init_session_memory()
    init_extract_memories()
    init_auto_dream()

    _state.started = True
    _state.started_at = time.time()
    _state.last_interaction_at = time.time()
    _state.idle_grace_s = max(0.0, float(idle_grace_s))
    _state.slow_op_delay_s = max(0.0, float(slow_op_delay_s))
    _state.recurring_cleanup_interval_s = max(0.0, float(recurring_cleanup_interval_s))

    sink = event_sink or _event_sink_from_context(context)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        _state.slow_ops_task = loop.create_task(_run_delayed_idle_cleanup(sink))
        if os.environ.get("USER_TYPE") == "ant":
            _state.recurring_task = loop.create_task(_run_recurring_cleanup(sink))
    return _state


async def run_startup_evolution_recovery(
    recovery: Any,
    *,
    event_sink: BackgroundEventSink | None = None,
) -> Any | None:
    """Run evolution startup recovery without letting it break runtime init."""

    try:
        run = getattr(recovery, "run_startup_recovery", None)
        if not callable(run):
            return None
        result = run()
        payload = (
            result.to_dict()
            if hasattr(result, "to_dict") and callable(result.to_dict)
            else {"result": result}
        )
        await _emit_event(event_sink, "evolution_recovery_complete", payload)
        return result
    except Exception as exc:
        logger.debug("Evolution startup recovery failed", exc_info=True)
        await _emit_event(
            event_sink,
            "evolution_recovery_failed",
            {"error": str(exc)},
        )
        return None


def record_interaction() -> None:
    """Mark recent user activity for OpenSpace idle cleanup deferral."""

    _state.last_interaction_at = time.time()


async def stop_background_housekeeping(
    context: Any | None = None,
    *,
    timeout_s: float = 10.0,
    event_sink: BackgroundEventSink | None = None,
    cancel_pending: bool = True,
) -> BackgroundDrainResult:
    """Drain registered housekeeping work and stop timers."""

    _cancel_timer(_state.slow_ops_task)
    _cancel_timer(_state.recurring_task)
    _state.slow_ops_task = None
    _state.recurring_task = None

    sink = event_sink or _event_sink_from_context(context)
    result = await drain_background_tasks(
        context,
        timeout_s=timeout_s,
        event_sink=sink,
    )
    if cancel_pending and result.pending_count:
        await cancel_all_background_tasks(context=context, reason="shutdown")
    _state.started = False
    return result


async def schedule_session_memory(
    context: Any,
    append_system_message: Any | None = None,
) -> asyncio.Task[Any] | None:
    from openspace.services.memory.session_memory import (
        should_schedule_session_memory,
        submit_session_memory_extraction,
    )

    if not should_schedule_session_memory(context):
        return None
    task_id = _reserve_task_id(context, "session_memory", TaskType.SESSION_MEMORY)
    task = submit_session_memory_extraction(context, append_system_message)
    await track_background_task(
        context,
        task,
        name="Session Memory",
        source="session_memory",
        task_type=TaskType.SESSION_MEMORY,
        description="Background session memory extraction",
        task_id=task_id,
    )
    return task


async def schedule_extract_memories(
    context: Any,
    append_system_message: Any | None = None,
) -> asyncio.Task[Any] | None:
    from openspace.services.memory.extract import (
        should_schedule_extract_memories,
        submit_extract_memories,
    )

    if not should_schedule_extract_memories(context):
        return None
    task_id = _reserve_task_id(context, "extract_memories", TaskType.MEMORY_EXTRACT)
    task = submit_extract_memories(context, append_system_message)
    await track_background_task(
        context,
        task,
        name="Memory Extract",
        source="extract_memories",
        task_type=TaskType.MEMORY_EXTRACT,
        description="Background memory extraction",
        task_id=task_id,
    )
    return task


async def schedule_auto_dream(
    context: Any,
    append_system_message: Any | None = None,
) -> asyncio.Task[Any] | None:
    from openspace.services.memory.dream import (
        should_schedule_auto_dream,
        submit_auto_dream,
    )

    if not should_schedule_auto_dream(context):
        return None
    task_id = _reserve_task_id(context, "auto_dream", TaskType.DREAM)
    task = submit_auto_dream(context, append_system_message)
    await track_background_task(
        context,
        task,
        name="Memory Dream",
        source="auto_dream",
        task_type=TaskType.DREAM,
        description="Memory consolidation",
        task_id=task_id,
    )
    return task


async def track_background_task(
    context: Any,
    task: asyncio.Task[Any] | None,
    *,
    name: str,
    source: str,
    task_type: TaskType | str,
    description: str,
    task_id: str | None = None,
) -> BackgroundTaskRecord | None:
    """Track a submitted task in context, global registry, and TaskManager."""

    if task is None or not isinstance(task, asyncio.Task):
        return None

    resolved_task_id = task_id or _reserve_task_id(context, source, task_type)
    scope_key = maybe_memory_task_scope_key(context)
    task_manager = _task_manager_from_context(context)
    event_sink = _event_sink_from_context(context)

    record = BackgroundTaskRecord(
        task_id=resolved_task_id,
        name=name,
        source=source,
        task_type=str(getattr(task_type, "value", task_type)),
        description=description,
        task=task,
        scope_key=scope_key,
        session_id=_none_or_str(_context_value(context, "session_id")),
        parent_task_id=(
            _none_or_str(_context_value(context, "task_id"))
            or _none_or_str(_context_value(context, "parent_task_id"))
        ),
        root_task_id=_none_or_str(_context_value(context, "task_id")),
        event_sink=event_sink,
        task_manager=task_manager,
    )
    _tasks[resolved_task_id] = record
    _add_context_task(context, task)

    if task_manager is not None:
        await task_manager.register_external_task(
            runner_task=task,
            description=description,
            task_type=task_type,
            agent_type="memory",
            agent_id=resolved_task_id,
            parent_task_id=(
                _context_value(context, "task_id")
                or _context_value(context, "parent_task_id")
            ),
            is_backgrounded=True,
        )
    else:
        await _emit_task_update(record, "task_started")

        def _done(done: asyncio.Task[Any]) -> None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.create_task(_finalize_unmanaged_record(record, done))

        task.add_done_callback(_done)

    def _cleanup(done: asyncio.Task[Any]) -> None:
        _discard_context_task(context, done)
        if done.cancelled():
            record.status = "killed"
        elif done.exception() is not None:
            record.status = "failed"
        else:
            record.status = "completed"

    task.add_done_callback(_cleanup)
    return record


async def drain_background_tasks(
    context: Any | None = None,
    *,
    timeout_s: float = 3.0,
    event_sink: BackgroundEventSink | None = None,
) -> BackgroundDrainResult:
    """Drain low-level memory tasks plus registered housekeeping records."""

    timeout_s = max(0.0, float(timeout_s))
    deadline = time.monotonic() + timeout_s
    sink = event_sink or _event_sink_from_context(context)

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    memory_result = await drain_pending_memory_tasks(
        timeout_s=remaining(),
        event_sink=sink,
        context=context,
    )
    tracked_pending = await _drain_tracked_records(
        context,
        timeout_s=remaining(),
    )
    result = BackgroundDrainResult(
        timeout_s=timeout_s,
        memory=memory_result,
        tracked_pending=tracked_pending,
    )
    if result.timed_out:
        await _emit_event(
            sink,
            "background_housekeeping_drain_timeout",
            result.as_event_payload(),
        )
    return result


async def cancel_background_task(task_id: str, *, reason: str = "cancelled") -> bool:
    record = _tasks.get(task_id)
    if record is None:
        return False
    if record.task.done():
        return False
    record.status = "killed"
    if record.task_manager is not None:
        try:
            await record.task_manager.stop_task_or_raise(task_id)
            return True
        except StopTaskError:
            pass
    record.task.cancel()
    await _emit_task_update(record, "task_stopped", {"reason": reason})
    return True


async def cancel_all_background_tasks(
    *,
    context: Any | None = None,
    reason: str = "cancelled",
) -> int:
    scope_key = maybe_memory_task_scope_key(context)
    count = 0
    for record in list(_tasks.values()):
        if record.task.done():
            continue
        if scope_key is not None and record.scope_key != scope_key:
            continue
        if await cancel_background_task(record.task_id, reason=reason):
            count += 1
    return count


async def _run_delayed_idle_cleanup(
    event_sink: BackgroundEventSink | None,
) -> None:
    try:
        while True:
            await asyncio.sleep(_state.slow_op_delay_s)
            if time.time() - _state.last_interaction_at < _state.idle_grace_s:
                continue
            await _emit_event(
                event_sink,
                "background_housekeeping_idle",
                {"idle_for_s": time.time() - _state.last_interaction_at},
            )
            await _emit_event(
                event_sink,
                "background_housekeeping_cleanup_complete",
                {
                    "cleanup": "os_retention_policy",
                    "skipped": True,
                    "reason": "no_openspace_retention_policy_configured",
                },
            )
            return
    except asyncio.CancelledError:
        return


async def _run_recurring_cleanup(event_sink: BackgroundEventSink | None) -> None:
    try:
        while True:
            await asyncio.sleep(_state.recurring_cleanup_interval_s)
            await _emit_event(
                event_sink,
                "background_housekeeping_recurring_cleanup",
                {"skipped": True, "reason": "anthropic_package_cleanup_not_applicable"},
            )
    except asyncio.CancelledError:
        return


async def _drain_tracked_records(context: Any | None, *, timeout_s: float) -> int:
    scope_key = maybe_memory_task_scope_key(context)
    pending = [
        record.task
        for record in _tasks.values()
        if not record.task.done()
        and (scope_key is None or record.scope_key == scope_key)
    ]
    if not pending:
        return 0
    done, still_pending = await asyncio.wait(pending, timeout=max(0.0, timeout_s))
    for task in done:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Background housekeeping task failed during drain", exc_info=True)
    return len(still_pending)


async def _finalize_unmanaged_record(
    record: BackgroundTaskRecord,
    done: asyncio.Task[Any],
) -> None:
    if done.cancelled():
        record.status = "killed"
        await _emit_task_update(record, "task_stopped")
        return
    try:
        result = done.result()
        error = _mapping_get(result, "error")
        if error:
            record.status = "failed"
            await _emit_task_update(record, "task_failed", {"error": str(error)})
        else:
            record.status = "completed"
            await _emit_task_update(record, "task_completed")
    except Exception as exc:
        record.status = "failed"
        await _emit_task_update(record, "task_failed", {"error": str(exc)})


async def _emit_task_update(
    record: BackgroundTaskRecord,
    event_type: str,
    extra: Mapping[str, Any] | None = None,
) -> None:
    status = {
        "task_started": "running",
        "task_completed": "completed",
        "task_failed": "failed",
        "task_stopped": "killed",
    }.get(event_type, record.status)
    payload = {
        "task_id": record.task_id,
        "session_id": record.session_id,
        "parent_task_id": record.parent_task_id,
        "source_task_id": record.root_task_id,
        "agent_id": "memory",
        "agent_type": "memory",
        "task_type": record.task_type,
        "description": record.description,
        "current_operation": record.description,
        "status": status,
        "is_backgrounded": True,
        "start_time": record.started_at,
    }
    if status != "running":
        payload["end_time"] = time.time() * 1000
    if extra:
        payload.update(dict(extra))
    await _emit_event(record.event_sink, event_type, payload)
    await _emit_event(record.event_sink, "agent_task_update", payload)


def _reserve_task_id(
    context: Any,
    source: str,
    task_type: TaskType | str,
) -> str:
    existing = _background_task_ids(context).get(source)
    if existing:
        return existing
    task_id = generate_task_id(task_type)
    _background_task_ids(context)[source] = task_id
    return task_id


def _background_task_ids(context: Any) -> dict[str, str]:
    if isinstance(context, Mapping):
        value = context.get("background_task_ids")
        if isinstance(value, dict):
            return value
        value = {}
        try:
            context["background_task_ids"] = value  # type: ignore[index]
        except Exception:
            pass
        return value
    value = getattr(context, "background_task_ids", None)
    if isinstance(value, dict):
        return value
    return {}


def _add_context_task(context: Any, task: asyncio.Task[Any]) -> None:
    tasks = _context_value(context, "background_hook_tasks")
    if isinstance(tasks, set):
        tasks.add(task)


def _discard_context_task(context: Any, task: asyncio.Task[Any]) -> None:
    tasks = _context_value(context, "background_hook_tasks")
    if isinstance(tasks, set):
        tasks.discard(task)


def _task_manager_from_context(context: Any) -> TaskManager | None:
    manager = _context_value(context, "task_manager")
    return manager if isinstance(manager, TaskManager) else None


def _event_sink_from_context(context: Any | None) -> BackgroundEventSink | None:
    if context is None:
        return None
    sink = _context_value(context, "event_sink")
    return sink if callable(sink) else None


def _context_value(context: Any, key: str) -> Any:
    if isinstance(context, Mapping):
        return context.get(key)
    return getattr(context, key, None)


def _none_or_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _mapping_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


async def _emit_event(
    event_sink: BackgroundEventSink | None,
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
        logger.debug("Background housekeeping event sink failed", exc_info=True)


def _cancel_timer(task: asyncio.Task[Any] | None) -> None:
    if task is not None and not task.done():
        task.cancel()


__all__ = [
    "BackgroundDrainResult",
    "BackgroundHousekeepingState",
    "BackgroundTaskRecord",
    "cancel_all_background_tasks",
    "cancel_background_task",
    "drain_background_tasks",
    "record_interaction",
    "run_startup_evolution_recovery",
    "schedule_auto_dream",
    "schedule_extract_memories",
    "schedule_session_memory",
    "start_background_housekeeping",
    "stop_background_housekeeping",
    "track_background_task",
]
