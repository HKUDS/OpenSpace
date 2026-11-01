from __future__ import annotations

import asyncio
import inspect
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class TaskStatus(str, Enum):
    """OpenSpace ``TaskStatus`` values from ``Task.ts``."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


class TaskType(str, Enum):
    """OpenSpace task types plus the OS coordinator-worker specialisation."""

    LOCAL_BASH = "local_bash"
    LOCAL_AGENT = "local_agent"
    IN_PROCESS_TEAMMATE = "in_process_teammate"
    DREAM = "dream"
    COORDINATOR_WORKER = "coordinator_worker"
    SESSION_MEMORY = "session_memory"
    MEMORY_EXTRACT = "memory_extract"


TASK_ID_PREFIXES: dict[str, str] = {
    TaskType.LOCAL_BASH.value: "b",
    TaskType.LOCAL_AGENT.value: "a",
    TaskType.IN_PROCESS_TEAMMATE.value: "t",
    TaskType.DREAM.value: "d",
    TaskType.COORDINATOR_WORKER.value: "c",
    TaskType.SESSION_MEMORY.value: "s",
    TaskType.MEMORY_EXTRACT.value: "e",
}

TASK_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
DEFAULT_TASK_OUTPUT_DIR = Path(tempfile.gettempdir()) / "openspace-agent-tasks"
_REGISTERED_INTERNAL_OUTPUT_DIRS: set[str] = set()


def _register_internal_output_dir(output_dir: Path) -> None:
    """Allow read access to TaskManager-owned output files.

    Background shell/agent output lives outside the workspace in normal
    sessions. The directory may be under ``~/.openspace``, which is otherwise
    treated as sensitive. This narrow internal carve-out keeps the model able
    to inspect outputs it just created without opening broader config access.
    """
    try:
        root = output_dir.expanduser().resolve()
    except OSError:
        root = output_dir.expanduser().absolute()
    key = str(root)
    if key in _REGISTERED_INTERNAL_OUTPUT_DIRS:
        return
    _REGISTERED_INTERNAL_OUTPUT_DIRS.add(key)

    try:
        from openspace.grounding.core.permissions import register_internal_path_predicate
    except Exception:
        logger.debug("Could not register internal task output dir", exc_info=True)
        return

    def _is_task_output_path(path: str, *, _root: Path = root) -> bool:
        try:
            candidate = Path(path).expanduser().resolve()
        except OSError:
            candidate = Path(path).expanduser().absolute()
        try:
            return candidate.is_relative_to(_root) and candidate.is_file()
        except AttributeError:  # pragma: no cover - Python < 3.9 fallback
            try:
                candidate.relative_to(_root)
                return candidate.is_file()
            except ValueError:
                return False

    register_internal_path_predicate(
        category="readable",
        reason="TaskManager-owned background task output is readable",
        predicate=_is_task_output_path,
    )


def is_terminal_task_status(status: TaskStatus | str) -> bool:
    status_value = status.value if isinstance(status, TaskStatus) else str(status)
    return status_value in {
        TaskStatus.COMPLETED.value,
        TaskStatus.FAILED.value,
        TaskStatus.KILLED.value,
    }


def generate_task_id(task_type: TaskType | str) -> str:
    """Generate OpenSpace compact task IDs (prefix + 8 base36 chars)."""

    task_type_value = str(getattr(task_type, "value", task_type))
    prefix = TASK_ID_PREFIXES.get(task_type_value, "x")
    return prefix + "".join(secrets.choice(TASK_ID_ALPHABET) for _ in range(8))


def get_task_output_path(task_id: str, output_dir: str | Path | None = None) -> Path:
    """Stable output path for a task ID.

    OpenSpace stores this via ``utils/task/diskOutput.ts::getTaskOutputPath``.
    OpenSpace keeps the same one-file-per-task contract but uses a portable
    temp directory until the session store step gives tasks a persisted home.
    """

    root = Path(output_dir) if output_dir is not None else DEFAULT_TASK_OUTPUT_DIR
    return root / f"{task_id}.out"


@dataclass(slots=True)
class TaskStateBase:
    id: str
    type: TaskType
    status: TaskStatus
    description: str
    tool_use_id: str | None = None
    start_time: float = field(default_factory=lambda: time.time() * 1000)
    end_time: float | None = None
    total_paused_ms: float | None = None
    output_file: str = ""
    output_offset: int = 0
    notified: bool = False


@dataclass(slots=True)
class ToolActivity:
    tool_name: str
    input: dict[str, Any]
    activity_description: str | None = None
    is_search: bool | None = None
    is_read: bool | None = None


@dataclass(slots=True)
class AgentProgress:
    tool_use_count: int = 0
    token_count: int = 0
    last_activity: ToolActivity | None = None
    recent_activities: list[ToolActivity] = field(default_factory=list)
    summary: str | None = None


@dataclass(slots=True)
class AgentTask(TaskStateBase):
    """Unified runtime state for local-agent and teammate tasks."""

    agent_id: str = ""
    prompt: str = ""
    agent_type: str = "general-purpose"
    model: str | None = None
    selected_agent: Any | None = None
    abort_event: asyncio.Event | None = None
    asyncio_task: asyncio.Task[Any] | None = None
    error: str | None = None
    result: Any | None = None
    progress: AgentProgress | None = None
    retrieved: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_reported_tool_count: int = 0
    last_reported_token_count: int = 0
    is_backgrounded: bool = True
    pending_messages: list[Any] = field(default_factory=list)
    inbox: asyncio.Queue[Any] | None = None
    retain: bool = False
    disk_loaded: bool = False
    evict_after: float | None = None
    team_name: str | None = None
    parent_task_id: str | None = None
    parent_inbox: asyncio.Queue[Any] | None = None
    shutdown_requested: bool = False


@dataclass(slots=True)
class TaskOutput:
    task_id: str
    task_type: str
    status: str
    description: str
    output: str
    output_tail: str | None = None
    output_file: str | None = None
    command: str | None = None
    pid: int | None = None
    interrupted: bool | None = None
    exit_code: int | None = None
    is_backgrounded: bool | None = None
    kind: str | None = None
    backgrounded_by_user: bool | None = None
    assistant_auto_backgrounded: bool | None = None
    duration_ms: float | None = None
    error: str | None = None
    prompt: str | None = None
    result: str | None = None


@dataclass(slots=True)
class TaskOutputResult:
    retrieval_status: str
    task: TaskOutput | None


class StopTaskError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


TaskRunner = Callable[[AgentTask], Awaitable[Any]]
EventSink = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class TaskManager:
    """Unified lifecycle manager for background agent tasks.

    OpenSpace spreads this behavior across ``Task.ts``, ``tasks.ts``,
    ``LocalAgentTask.tsx``, ``InProcessTeammateTask`` and
    ``utils/task/framework.ts``.  OpenSpace keeps it backend-neutral so Agent,
    teammate and future shell tasks share one registry.
    """

    def __init__(
        self,
        *,
        event_sink: EventSink | None = None,
        output_dir: str | Path | None = None,
    ) -> None:
        self._tasks: dict[str, Any] = {}
        self._agent_name_registry: dict[str, str] = {}
        self._event_sink = event_sink
        self._output_dir = Path(output_dir) if output_dir is not None else DEFAULT_TASK_OUTPUT_DIR
        self._output_dir.mkdir(parents=True, exist_ok=True)
        _register_internal_output_dir(self._output_dir)
        self._active_team_name: str | None = None
        self._team_metadata: dict[str, Any] | None = None

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def active_team_name(self) -> str | None:
        return self._active_team_name

    @property
    def team_metadata(self) -> dict[str, Any] | None:
        return dict(self._team_metadata) if self._team_metadata else None

    def create_team(
        self,
        team_name: str,
        *,
        description: str = "",
        agent_type: str = "team-lead",
        lead_agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a session-local team context for in-process teammates.

        OpenSpace persists a team file and task-list directory. OpenSpace keeps the
        runtime state in this session-scoped manager because 12.4 deliberately
        chose in-process queues over mailbox files.
        """

        if self._active_team_name:
            raise ValueError(
                f'Already leading team "{self._active_team_name}". '
                "Use TeamDelete before creating a new team."
            )
        final_name = str(team_name).strip()
        self._active_team_name = final_name
        self._team_metadata = {
            "team_name": final_name,
            "description": description,
            "lead_agent_id": lead_agent_id or f"team-lead@{final_name}",
            "lead_agent_type": agent_type,
            "created_at": time.time() * 1000,
        }
        self.register_alias("team-lead", self._team_metadata["lead_agent_id"])
        return dict(self._team_metadata)

    def delete_team(self) -> dict[str, Any]:
        team_name = self._active_team_name
        self._active_team_name = None
        self._team_metadata = None
        if team_name:
            aliases_to_drop = [
                alias
                for alias, task_id in self._agent_name_registry.items()
                if alias.endswith(f"@{team_name}") or task_id.endswith(f"@{team_name}")
            ]
            for alias in aliases_to_drop:
                self._agent_name_registry.pop(alias, None)
        return {
            "success": True,
            "message": (
                f'Cleaned up team "{team_name}"'
                if team_name
                else "No team name found, nothing to clean up"
            ),
            "team_name": team_name,
        }

    def set_event_sink(self, sink: EventSink | None) -> None:
        self._event_sink = sink

    def create_task_state_base(
        self,
        task_id: str,
        task_type: TaskType | str,
        description: str,
        *,
        tool_use_id: str | None = None,
    ) -> TaskStateBase:
        task_type_enum = _coerce_task_type(task_type)
        return TaskStateBase(
            id=task_id,
            type=task_type_enum,
            status=TaskStatus.PENDING,
            description=description,
            tool_use_id=tool_use_id,
            output_file=str(get_task_output_path(task_id, self._output_dir)),
        )

    async def register_async_agent(
        self,
        *,
        runner: TaskRunner,
        prompt: str,
        description: str,
        agent_type: str = "general-purpose",
        selected_agent: Any | None = None,
        model: str | None = None,
        task_type: TaskType | str = TaskType.LOCAL_AGENT,
        team_name: str | None = None,
        parent_task_id: str | None = None,
        parent_abort_event: asyncio.Event | None = None,
        parent_inbox: asyncio.Queue[Any] | None = None,
        tool_use_id: str | None = None,
        is_backgrounded: bool = True,
        agent_id: str | None = None,
    ) -> AgentTask:
        task_type_enum = _coerce_task_type(task_type)
        task_id = agent_id or generate_task_id(task_type_enum)
        base = self.create_task_state_base(
            task_id,
            task_type_enum,
            description,
            tool_use_id=tool_use_id,
        )
        task = AgentTask(
            id=base.id,
            type=base.type,
            status=base.status,
            description=base.description,
            tool_use_id=base.tool_use_id,
            start_time=base.start_time,
            end_time=base.end_time,
            total_paused_ms=base.total_paused_ms,
            output_file=base.output_file,
            output_offset=base.output_offset,
            notified=base.notified,
            agent_id=task_id,
            prompt=prompt,
            agent_type=agent_type,
            model=model,
            selected_agent=selected_agent,
            abort_event=asyncio.Event(),
            inbox=asyncio.Queue(),
            team_name=team_name,
            parent_task_id=parent_task_id,
            parent_inbox=parent_inbox,
            is_backgrounded=is_backgrounded,
        )
        self._register_task(task)
        self._write_task_output(task, {"status": "running", **self._task_event_payload(task)})

        task.asyncio_task = asyncio.create_task(
            self._run_agent_task(task, runner, parent_abort_event=parent_abort_event)
        )
        task.status = TaskStatus.RUNNING
        await self._emit_task_event("task_started", task)
        return task

    async def register_foreground_agent(
        self,
        *,
        runner: TaskRunner,
        prompt: str,
        description: str,
        agent_type: str = "general-purpose",
        selected_agent: Any | None = None,
        model: str | None = None,
        parent_abort_event: asyncio.Event | None = None,
        tool_use_id: str | None = None,
        agent_id: str | None = None,
    ) -> AgentTask:
        task = await self.register_async_agent(
            runner=runner,
            prompt=prompt,
            description=description,
            agent_type=agent_type,
            selected_agent=selected_agent,
            model=model,
            task_type=TaskType.LOCAL_AGENT,
            parent_abort_event=parent_abort_event,
            tool_use_id=tool_use_id,
            is_backgrounded=False,
            agent_id=agent_id,
        )
        if task.asyncio_task is not None:
            await task.asyncio_task
        return task

    async def register_external_task(
        self,
        *,
        runner_task: asyncio.Task[Any],
        description: str,
        task_type: TaskType | str = TaskType.LOCAL_AGENT,
        agent_type: str = "background",
        agent_id: str | None = None,
        model: str | None = None,
        prompt: str = "",
        parent_task_id: str | None = None,
        tool_use_id: str | None = None,
        is_backgrounded: bool = True,
    ) -> AgentTask:
        """Register an already-created asyncio task in the OpenSpace task registry.

        Memory housekeeping tasks are scheduled by their own services so they
        can preserve OpenSpace's fire-and-forget semantics.  This method gives those
        tasks the same TaskGet/TaskList/TaskStop and TUI lifecycle surface as
        regular background agents.
        """

        task_type_enum = _coerce_task_type(task_type)
        task_id = agent_id or generate_task_id(task_type_enum)
        base = self.create_task_state_base(
            task_id,
            task_type_enum,
            description,
            tool_use_id=tool_use_id,
        )
        task = AgentTask(
            id=base.id,
            type=base.type,
            status=TaskStatus.RUNNING,
            description=base.description,
            tool_use_id=base.tool_use_id,
            start_time=base.start_time,
            end_time=base.end_time,
            total_paused_ms=base.total_paused_ms,
            output_file=base.output_file,
            output_offset=base.output_offset,
            notified=base.notified,
            agent_id=task_id,
            prompt=prompt,
            agent_type=agent_type,
            model=model,
            asyncio_task=runner_task,
            is_backgrounded=is_backgrounded,
            parent_task_id=parent_task_id,
        )
        self._register_task(task)
        self._write_task_output(task, {"status": "running", **self._task_event_payload(task)})
        await self._emit_task_event("task_started", task)

        def _done(done: asyncio.Task[Any]) -> None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.create_task(self._finalize_external_task(task, done))

        runner_task.add_done_callback(_done)
        return task

    async def spawn_local_shell_task(
        self,
        *,
        command: str,
        description: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        conda_env: str | None = None,
        argv: list[str] | None = None,
        cleanup_callbacks: list[Any] | None = None,
        output_transform: Any | None = None,
        tool_use_id: str | None = None,
        agent_id: str | None = None,
        is_backgrounded: bool = True,
        backgrounded_by_user: bool = False,
        assistant_auto_backgrounded: bool = False,
        notification_queue: asyncio.Queue[Any] | None = None,
        kind: str = "bash",
    ) -> Any:
        """Spawn and register a OpenSpace ``local_bash`` task.

        Unlike ``register_external_task``, this owns the subprocess handle so
        TaskStop can kill the process group and the output file remains the raw
        merged shell stream.
        """

        from openspace.grounding.backends.shell.shell_command_handle import (
            BackgroundShellHandle,
        )

        task_id = generate_task_id(TaskType.LOCAL_BASH)
        output_path = get_task_output_path(task_id, self._output_dir)
        shell_command = await BackgroundShellHandle.spawn(
            command,
            task_id=task_id,
            output_path=output_path,
            cwd=cwd,
            env=env,
            conda_env=conda_env,
            argv=argv,
            cleanup_callbacks=cleanup_callbacks,
            output_transform=output_transform,
        )
        return await self.register_local_shell_task(
            command=command,
            description=description,
            shell_command=shell_command,
            task_id=task_id,
            tool_use_id=tool_use_id,
            agent_id=agent_id,
            is_backgrounded=is_backgrounded,
            backgrounded_by_user=backgrounded_by_user,
            assistant_auto_backgrounded=assistant_auto_backgrounded,
            notification_queue=notification_queue,
            kind=kind,
            finalize_on_completion=True,
        )

    async def register_local_shell_task(
        self,
        *,
        command: str,
        description: str,
        shell_command: Any,
        task_id: str | None = None,
        tool_use_id: str | None = None,
        agent_id: str | None = None,
        is_backgrounded: bool = True,
        backgrounded_by_user: bool = False,
        assistant_auto_backgrounded: bool = False,
        notification_queue: asyncio.Queue[Any] | None = None,
        kind: str = "bash",
        finalize_on_completion: bool = True,
    ) -> Any:
        """Register an already-started local shell command.

        24.2 needs this because foreground commands start immediately but only
        become TaskManager-visible after the OpenSpace 2s progress threshold.  If the
        command is later backgrounded, the same process handle is flipped in
        place rather than respawned.
        """

        from openspace.agents.shell_task import LocalShellTaskState

        final_task_id = task_id or generate_task_id(TaskType.LOCAL_BASH)
        if is_backgrounded:
            shell_command.background(final_task_id)
        output_path = getattr(shell_command, "output_path", None)
        if output_path is None:
            output_path = get_task_output_path(final_task_id, self._output_dir)
        base = self.create_task_state_base(
            final_task_id,
            TaskType.LOCAL_BASH,
            description,
            tool_use_id=tool_use_id,
        )
        task = LocalShellTaskState(
            id=base.id,
            type=base.type,
            status=TaskStatus.RUNNING,
            description=base.description,
            tool_use_id=base.tool_use_id,
            start_time=base.start_time,
            end_time=base.end_time,
            total_paused_ms=base.total_paused_ms,
            output_file=str(output_path),
            output_offset=base.output_offset,
            notified=base.notified,
            command=command,
            shell_command=shell_command,
            is_backgrounded=is_backgrounded,
            agent_id=agent_id,
            kind="monitor" if kind == "monitor" else "bash",
            pid=shell_command.pid,
            backgrounded_by_user=backgrounded_by_user,
            assistant_auto_backgrounded=assistant_auto_backgrounded,
            notification_queue=notification_queue,
        )
        self._register_task(task)
        await self._emit_task_event("task_started", task)
        if is_backgrounded:
            self._ensure_shell_stall_watchdog(task)
        if finalize_on_completion:
            self._ensure_shell_finalizer(task)
        return task

    def _ensure_shell_finalizer(self, task: Any) -> None:
        if getattr(task, "finalizer_task", None) is not None:
            return
        task.finalizer_task = asyncio.create_task(self._finalize_shell_task(task))

    async def background_existing_foreground_shell_task(
        self,
        task_id: str,
        *,
        backgrounded_by_user: bool = False,
        assistant_auto_backgrounded: bool = False,
    ) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.type != TaskType.LOCAL_BASH:
            return False
        if task.status != TaskStatus.RUNNING:
            return False
        shell_command = getattr(task, "shell_command", None)
        if shell_command is None:
            return False
        shell_command.background(task.id)
        task.is_backgrounded = True
        task.backgrounded_by_user = bool(
            task.backgrounded_by_user or backgrounded_by_user
        )
        task.assistant_auto_backgrounded = bool(
            task.assistant_auto_backgrounded or assistant_auto_backgrounded
        )
        self._ensure_shell_stall_watchdog(task)
        self._ensure_shell_finalizer(task)
        await self._emit_task_event("agent_task_update", task)
        return True

    async def background_all_foreground_tasks(self) -> list[str]:
        backgrounded: list[str] = []
        for task_id, task in list(self._tasks.items()):
            if (
                task.type == TaskType.LOCAL_BASH
                and task.status == TaskStatus.RUNNING
                and getattr(task, "is_backgrounded", True) is False
            ):
                did_background = await self.background_existing_foreground_shell_task(
                    task_id,
                    backgrounded_by_user=True,
                )
                if did_background:
                    backgrounded.append(task_id)
        return backgrounded

    def mark_task_notified(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task is not None:
            task.notified = True

    async def unregister_foreground_shell_task(
        self,
        task_id: str,
        *,
        result: Any | None = None,
    ) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.type != TaskType.LOCAL_BASH:
            return False
        if getattr(task, "is_backgrounded", True):
            return False
        if result is not None:
            task.result = result
            task.status = (
                TaskStatus.COMPLETED
                if getattr(result, "code", 1) == 0
                else TaskStatus.FAILED
            )
        else:
            task.status = TaskStatus.COMPLETED
        task.end_time = time.time() * 1000
        task.notified = True
        await self._emit_task_event(
            "task_completed"
            if task.status == TaskStatus.COMPLETED
            else "task_failed",
            task,
        )
        self._tasks.pop(task_id, None)
        return True

    def _ensure_shell_stall_watchdog(self, task: Any) -> None:
        if getattr(task, "kind", "bash") == "monitor":
            return
        if getattr(task, "stall_watchdog_task", None) is not None:
            return
        task.stall_watchdog_task = asyncio.create_task(
            self._watch_shell_stall(task)
        )

    async def _watch_shell_stall(self, task: Any) -> None:
        interval_ms = _env_int("OPENSPACE_SHELL_STALL_INTERVAL_MS", 5_000)
        threshold_ms = _env_int("OPENSPACE_SHELL_STALL_THRESHOLD_MS", 45_000)
        tail_bytes = _env_int("OPENSPACE_SHELL_STALL_TAIL_BYTES", 1024)
        last_size = 0
        last_growth = time.time() * 1000
        try:
            while task.status == TaskStatus.RUNNING and getattr(
                task, "is_backgrounded", False
            ):
                await asyncio.sleep(max(0.01, interval_ms / 1000))
                try:
                    size = Path(task.output_file).stat().st_size
                except OSError:
                    continue
                if size > last_size:
                    last_size = size
                    last_growth = time.time() * 1000
                    continue
                if time.time() * 1000 - last_growth < threshold_ms:
                    continue
                tail = _tail_text_file(task.output_file, tail_bytes)
                from openspace.agents.shell_task import (
                    build_shell_stall_notification_xml,
                    looks_like_prompt,
                )

                if not looks_like_prompt(tail):
                    last_growth = time.time() * 1000
                    continue
                queue = getattr(task, "notification_queue", None)
                if queue is not None:
                    queue.put_nowait(
                        {
                            "type": "notification",
                            "content": build_shell_stall_notification_xml(task, tail),
                        }
                    )
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Shell stall watchdog failed for %s", task.id, exc_info=True)

    def _register_task(self, task: Any) -> None:
        self._tasks[task.id] = task
        agent_type = getattr(task, "agent_type", None)
        if agent_type:
            self._agent_name_registry[agent_type] = task.id
        team_name = getattr(task, "team_name", None)
        if agent_type and team_name:
            self._agent_name_registry[f"{agent_type}@{team_name}"] = task.id

    def register_alias(self, alias: str, task_id: str) -> None:
        """Register a lookup alias for SendMessage-style routing.

        OpenSpace teammates can be addressed by human name, agent id, or
        ``name@team``. Task IDs stay canonical; aliases only point at existing
        tasks and are intentionally session-local through the owning
        ``TaskManager``.
        """

        if alias and (task_id in self._tasks or str(alias) == "team-lead"):
            self._agent_name_registry[str(alias)] = task_id

    async def stop_task(self, task_id: str) -> bool:
        try:
            await self.stop_task_or_raise(task_id)
            return True
        except StopTaskError:
            return False

    async def stop_task_or_raise(
        self,
        task_id: str,
        *,
        signal_name: str = "TERM",
    ) -> dict[str, Any]:
        task = self._tasks.get(task_id)
        if task is None:
            raise StopTaskError(f"No task found with ID: {task_id}", "not_found")
        if task.status != TaskStatus.RUNNING:
            raise StopTaskError(
                f"Task {task_id} is not running (status: {task.status.value})",
                "not_running",
            )

        if task.type == TaskType.LOCAL_BASH:
            shell_command = getattr(task, "shell_command", None)
            if shell_command is not None:
                await shell_command.kill(signal_name)
                await shell_command.cleanup()
            watchdog = getattr(task, "stall_watchdog_task", None)
            if watchdog is not None and not watchdog.done():
                watchdog.cancel()
            task.status = TaskStatus.KILLED
            task.end_time = time.time() * 1000
            task.notified = True
            task.shell_command = None
            await self._emit_task_event("task_stopped", task)
            return {
                "task_id": task.id,
                "task_type": task.type.value,
                "command": getattr(task, "command", task.description),
            }

        task.abort_event.set() if task.abort_event is not None else None
        if task.asyncio_task is not None and not task.asyncio_task.done():
            task.asyncio_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task.asyncio_task), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        task.status = TaskStatus.KILLED
        task.end_time = time.time() * 1000
        task.notified = True
        self._write_task_output(task, self._serialize_task(task))
        if task.asyncio_task is None:
            await self._emit_task_event("task_stopped", task)
        return {
            "task_id": task.id,
            "task_type": task.type.value,
            "command": task.description,
        }

    async def stop_all(self) -> int:
        count = 0
        for task_id, task in list(self._tasks.items()):
            if task.status == TaskStatus.RUNNING and await self.stop_task(task_id):
                count += 1
        return count

    def get_task(self, task_id: str) -> Any | None:
        return self._tasks.get(task_id)

    def find_by_name(self, agent_name: str) -> AgentTask | None:
        task_id = self._agent_name_registry.get(agent_name)
        return self._tasks.get(task_id) if task_id else None

    def find_by_agent_id(self, agent_id: str) -> AgentTask | None:
        return self._tasks.get(agent_id)

    def list_running(self) -> list[Any]:
        return [task for task in self._tasks.values() if task.status == TaskStatus.RUNNING]

    def list_all(self) -> list[Any]:
        return list(self._tasks.values())

    def list_by_team(self, team_name: str) -> list[AgentTask]:
        return [
            task
            for task in self._tasks.values()
            if getattr(task, "team_name", None) == team_name
        ]

    async def send_message(self, to_agent: str, message: Any) -> bool:
        task = self.find_by_name(to_agent) or self.find_by_agent_id(to_agent)
        if task is None or is_terminal_task_status(task.status):
            return False
        inbox = getattr(task, "inbox", None)
        if inbox is not None:
            await inbox.put(message)
        else:
            pending_messages = getattr(task, "pending_messages", None)
            if isinstance(pending_messages, list):
                pending_messages.append(message)
            else:
                return False
        return True

    async def broadcast_message(
        self,
        message: Any,
        *,
        team_name: str | None = None,
        exclude_agent: str | None = None,
    ) -> int:
        count = 0
        targets = self.list_by_team(team_name) if team_name else self.list_running()
        for task in targets:
            if exclude_agent and getattr(task, "agent_type", None) == exclude_agent:
                continue
            if await self.send_message(task.id, message):
                count += 1
        return count

    def enqueue_notification(self, task: AgentTask | str, notification: str) -> bool:
        target = self.get_task(task) if isinstance(task, str) else task
        if target is None or is_terminal_task_status(target.status):
            return False
        xml_notification = (
            "<task-notification>\n"
            f"{notification}\n"
            "</task-notification>"
        )
        pending_messages = getattr(target, "pending_messages", None)
        if isinstance(pending_messages, list):
            pending_messages.append({"type": "notification", "content": xml_notification})
        inbox = getattr(target, "inbox", None)
        if inbox is not None:
            try:
                inbox.put_nowait({"type": "notification", "content": xml_notification})
            except asyncio.QueueFull:
                return False
        return True

    async def wait_for_task_completion(
        self,
        task_id: str,
        *,
        timeout_ms: int = 30_000,
        abort_event: asyncio.Event | None = None,
    ) -> AgentTask | None:
        start = time.time() * 1000
        while time.time() * 1000 - start < timeout_ms:
            if abort_event is not None and abort_event.is_set():
                raise asyncio.CancelledError()
            task = self.get_task(task_id)
            if task is None:
                return None
            if is_terminal_task_status(task.status):
                return task
            await asyncio.sleep(0.1)
        return self.get_task(task_id)

    async def get_task_output(
        self,
        task_id: str,
        *,
        block: bool = True,
        timeout_ms: int = 30_000,
        abort_event: asyncio.Event | None = None,
    ) -> TaskOutputResult:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(f"No task found with ID: {task_id}")

        if not block:
            status = "success" if is_terminal_task_status(task.status) else "not_ready"
            if status == "success":
                task.notified = True
            return TaskOutputResult(status, self._task_output_data(task))

        completed = await self.wait_for_task_completion(
            task_id,
            timeout_ms=timeout_ms,
            abort_event=abort_event,
        )
        if completed is None:
            return TaskOutputResult("timeout", None)
        if not is_terminal_task_status(completed.status):
            return TaskOutputResult("timeout", self._task_output_data(completed))
        completed.notified = True
        return TaskOutputResult("success", self._task_output_data(completed))

    async def _run_agent_task(
        self,
        task: AgentTask,
        runner: TaskRunner,
        *,
        parent_abort_event: asyncio.Event | None = None,
    ) -> None:
        parent_watcher: asyncio.Task[Any] | None = None
        if parent_abort_event is not None and task.abort_event is not None:
            parent_watcher = asyncio.create_task(
                self._mirror_parent_abort(parent_abort_event, task.abort_event)
            )
        try:
            result = await runner(task)
            task.result = result
            result_status = str(_mapping_get(result, "status", "completed"))
            task.status = (
                TaskStatus.COMPLETED
                if result_status in {"completed", "success"}
                else TaskStatus.FAILED
            )
        except asyncio.CancelledError:
            task.status = TaskStatus.KILLED
            task.error = "Task was stopped."
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            logger.debug("Agent task %s failed", task.id, exc_info=True)
        finally:
            if parent_watcher is not None:
                parent_watcher.cancel()
            task.end_time = time.time() * 1000
            self._write_task_output(task, self._serialize_task(task))
            event = (
                "task_completed"
                if task.status == TaskStatus.COMPLETED
                else "task_failed"
                if task.status == TaskStatus.FAILED
                else "task_stopped"
            )
            await self._emit_task_event(event, task)
            if task.parent_task_id:
                parent = self.get_task(task.parent_task_id)
                if parent is not None and parent.status == TaskStatus.RUNNING:
                    self.enqueue_notification(parent, _build_task_notification_body(task))
            if task.parent_inbox is not None:
                await task.parent_inbox.put(
                    {
                        "type": "notification",
                        "content": _build_task_notification_xml(task),
                    }
                )

    async def _finalize_external_task(
        self,
        task: AgentTask,
        done: asyncio.Task[Any],
    ) -> None:
        try:
            result = done.result()
            task.result = result
            result_status = str(_mapping_get(result, "status", "") or "").lower()
            result_error = _mapping_get(result, "error", None)
            if task.status == TaskStatus.KILLED:
                pass
            elif result_error:
                task.status = TaskStatus.FAILED
                task.error = str(result_error)
            elif result_status in {"failed", "error"}:
                task.status = TaskStatus.FAILED
            elif result_status in {"killed", "cancelled", "canceled"}:
                task.status = TaskStatus.KILLED
            else:
                task.status = TaskStatus.COMPLETED
        except asyncio.CancelledError:
            task.status = TaskStatus.KILLED
            task.error = task.error or "Task was stopped."
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            logger.debug("External task %s failed", task.id, exc_info=True)
        finally:
            task.end_time = time.time() * 1000
            self._write_task_output(task, self._serialize_task(task))
            event = (
                "task_completed"
                if task.status == TaskStatus.COMPLETED
                else "task_failed"
                if task.status == TaskStatus.FAILED
                else "task_stopped"
            )
            await self._emit_task_event(event, task)

    async def _finalize_shell_task(self, task: Any) -> None:
        shell_command = getattr(task, "shell_command", None)
        try:
            if shell_command is None:
                return
            result = await shell_command.result
            if task.status == TaskStatus.KILLED:
                return
            task.result = result
            task.status = TaskStatus.COMPLETED if result.code == 0 else TaskStatus.FAILED
        except asyncio.CancelledError:
            task.status = TaskStatus.KILLED
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.result = None
            logger.debug("Local shell task %s failed", task.id, exc_info=True)
            try:
                with open(task.output_file, "ab") as output_file:
                    output_file.write(f"\n[openspace] shell task failed: {exc}\n".encode())
            except Exception:
                pass
        finally:
            task.end_time = time.time() * 1000
            if shell_command is not None:
                try:
                    await shell_command.cleanup()
                except Exception:
                    pass
            watchdog = getattr(task, "stall_watchdog_task", None)
            if watchdog is not None and not watchdog.done():
                watchdog.cancel()
            task.shell_command = None
            if task.status == TaskStatus.KILLED and task.notified:
                return
            if not task.notified:
                from openspace.agents.shell_task import build_shell_notification_xml

                task.notified = True
                queue = getattr(task, "notification_queue", None)
                if queue is not None:
                    try:
                        queue.put_nowait(
                            {
                                "type": "notification",
                                "content": build_shell_notification_xml(task),
                            }
                        )
                    except asyncio.QueueFull:
                        task.notified = False
            event = (
                "task_completed"
                if task.status == TaskStatus.COMPLETED
                else "task_failed"
                if task.status == TaskStatus.FAILED
                else "task_stopped"
            )
            await self._emit_task_event(event, task)

    async def _mirror_parent_abort(
        self,
        parent: asyncio.Event,
        child: asyncio.Event,
    ) -> None:
        await parent.wait()
        child.set()

    def _task_output_data(self, task: Any) -> TaskOutput:
        if task.type == TaskType.LOCAL_BASH:
            is_terminal = is_terminal_task_status(task.status)
            output_tail = _tail_text_file(task.output_file, 8192)
            output = _read_text_file(task.output_file) if is_terminal else output_tail
            result = getattr(task, "result", None)
            return TaskOutput(
                task_id=task.id,
                task_type=task.type.value,
                status=task.status.value,
                description=task.description,
                output=output,
                output_tail=output_tail,
                output_file=task.output_file,
                command=task.command,
                pid=task.pid,
                interrupted=getattr(result, "interrupted", None),
                exit_code=getattr(result, "code", None),
                is_backgrounded=task.is_backgrounded,
                kind=task.kind,
                backgrounded_by_user=task.backgrounded_by_user,
                assistant_auto_backgrounded=task.assistant_auto_backgrounded,
                duration_ms=(
                    (task.end_time or time.time() * 1000) - task.start_time
                    if task.start_time
                    else None
                ),
                error=None if task.status != TaskStatus.FAILED else "Shell command failed",
                result=output,
            )
        output = _read_text_file(task.output_file)
        result_text = _extract_result_text(task.result)
        if result_text:
            output = result_text
        return TaskOutput(
            task_id=task.id,
            task_type=task.type.value,
            status=task.status.value,
            description=task.description,
            output=output,
            error=task.error,
            prompt=task.prompt,
            result=result_text or output,
        )

    def _serialize_task(self, task: Any) -> dict[str, Any]:
        payload = self._task_event_payload(task)
        if task.type == TaskType.LOCAL_BASH:
            result = getattr(task, "result", None)
            payload.update(
                {
                    "status": task.status.value,
                    "command": task.command,
                    "exit_code": getattr(result, "code", None),
                    "interrupted": getattr(result, "interrupted", None),
                }
            )
            return payload
        payload.update(
            {
                "status": task.status.value,
                "prompt": task.prompt,
                "error": task.error,
                "result": _json_safe_result(task.result),
            }
        )
        return payload

    def _task_event_payload(self, task: Any) -> dict[str, Any]:
        if task.type == TaskType.LOCAL_BASH:
            result = getattr(task, "result", None)
            return {
                "task_id": task.id,
                "agent_id": task.agent_id,
                "description": task.description,
                "command": task.command,
                "task_type": task.type.value,
                "output_file": task.output_file,
                "output_tail": _tail_text_file(task.output_file, 2048),
                "is_backgrounded": task.is_backgrounded,
                "backgrounded_by_user": task.backgrounded_by_user,
                "assistant_auto_backgrounded": task.assistant_auto_backgrounded,
                "kind": task.kind,
                "start_time": task.start_time,
                "end_time": task.end_time,
                "elapsed_ms": (
                    (task.end_time or time.time() * 1000) - task.start_time
                    if task.start_time
                    else None
                ),
                "current_operation": task.description or task.command,
                "pid": task.pid,
                "exit_code": getattr(result, "code", None),
                "interrupted": getattr(result, "interrupted", None),
                "progress": {},
            }
        progress = _progress_payload(task.progress)
        return {
            "task_id": task.id,
            "agent_id": task.agent_id,
            "agent_type": task.agent_type,
            "description": task.description,
            "task_type": task.type.value,
            "team_name": task.team_name,
            "parent_task_id": task.parent_task_id,
            "output_file": task.output_file,
            "model": task.model,
            "is_backgrounded": task.is_backgrounded,
            "start_time": task.start_time,
            "end_time": task.end_time,
            "elapsed_ms": (
                (task.end_time or time.time() * 1000) - task.start_time
                if task.start_time
                else None
            ),
            "current_operation": progress.get("current_operation") or task.description,
            "progress": progress,
        }

    def _write_task_output(self, task: AgentTask, payload: Mapping[str, Any]) -> None:
        path = Path(task.output_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    async def _emit_task_event(self, event_type: str, task: Any) -> None:
        if self._event_sink is None:
            return
        data = {"status": task.status.value, **self._task_event_payload(task)}
        try:
            result = self._event_sink(event_type, data)
            if inspect.isawaitable(result):
                await result
            agent_result = self._event_sink(
                "agent_event",
                {
                    "agent_id": getattr(task, "agent_id", None) or task.id,
                    "event": "agent_task_update",
                    "payload": data,
                },
            )
            if inspect.isawaitable(agent_result):
                await agent_result
            update_result = self._event_sink("agent_task_update", data)
            if inspect.isawaitable(update_result):
                await update_result
        except Exception:
            logger.debug("Task event sink failed for %s", event_type, exc_info=True)


def _coerce_task_type(value: TaskType | str) -> TaskType:
    if isinstance(value, TaskType):
        return value
    raw = str(value)
    try:
        return TaskType(raw)
    except ValueError:
        return TaskType.LOCAL_AGENT


def _mapping_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _json_safe_result(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, Mapping):
        return dict(result)
    if hasattr(result, "__dataclass_fields__"):
        return {
            field_name: getattr(result, field_name)
            for field_name in getattr(result, "__dataclass_fields__", {})
        }
    if hasattr(result, "__dict__"):
        return dict(getattr(result, "__dict__"))
    return str(result)


def _progress_payload(progress: AgentProgress | None) -> dict[str, Any]:
    if progress is None:
        return {}
    last_activity = progress.last_activity
    recent = [_activity_payload(activity) for activity in progress.recent_activities]
    current_operation = (
        last_activity.activity_description
        if last_activity and last_activity.activity_description
        else last_activity.tool_name
        if last_activity
        else progress.summary
    )
    return {
        "tool_use_count": progress.tool_use_count,
        "token_count": progress.token_count,
        "summary": progress.summary,
        "current_operation": current_operation,
        "last_activity": _activity_payload(last_activity) if last_activity else None,
        "recent_activities": recent,
    }


def _activity_payload(activity: ToolActivity | None) -> dict[str, Any] | None:
    if activity is None:
        return None
    return {
        "tool_name": activity.tool_name,
        "input": dict(activity.input),
        "activity_description": activity.activity_description,
        "is_search": activity.is_search,
        "is_read": activity.is_read,
    }


def _extract_result_text(result: Any) -> str:
    if result is None:
        return ""
    content = _mapping_get(result, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(part for part in parts if part)
    return ""


def _read_text_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError:
        return Path(path).read_text(encoding="utf-8", errors="replace")


def _tail_text_file(path: str, max_bytes: int) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes), os.SEEK_SET)
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _build_task_notification_body(task: AgentTask) -> str:
    output = _extract_result_text(task.result) or task.error or _read_text_file(task.output_file)
    status = task.status.value
    if task.status == TaskStatus.COMPLETED:
        summary = f'Agent "{task.description}" completed'
    elif task.status == TaskStatus.FAILED:
        summary = f'Agent "{task.description}" failed: {task.error or "unknown error"}'
    elif task.status == TaskStatus.KILLED:
        summary = f'Agent "{task.description}" was stopped'
    else:
        summary = f'Agent "{task.description}" {status}'

    parts = [
        f"<task-id>{task.agent_id or task.id}</task-id>",
        f"<status>{status}</status>",
        f"<summary>{summary}</summary>",
    ]
    if output:
        parts.append(f"<result>{str(output).strip()}</result>")
    return "\n".join(parts)


def _build_task_notification_xml(task: AgentTask) -> str:
    return (
        "<task-notification>\n"
        f"{_build_task_notification_body(task)}\n"
        "</task-notification>"
    )


__all__ = [
    "AgentProgress",
    "AgentTask",
    "TaskManager",
    "TaskOutput",
    "TaskOutputResult",
    "TaskStateBase",
    "TaskStatus",
    "TaskType",
    "ToolActivity",
    "StopTaskError",
    "generate_task_id",
    "get_task_output_path",
    "is_terminal_task_status",
]
