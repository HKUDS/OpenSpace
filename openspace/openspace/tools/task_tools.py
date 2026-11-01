"""Task control tools for OpenSpace background agents.

Task get/list/stop names map to the TaskManager runtime because session
checklist writes are handled by ``TodoWriteTool`` until a write-capable
task-list tool family exists.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from typing import Any

from openspace.agents.task_manager import (
    StopTaskError,
    TaskManager,
    TaskOutput,
    TaskOutputResult,
    TaskStatus,
    is_terminal_task_status,
)
from openspace.grounding.core.permissions import (
    PermissionAsk,
    deny_missing_permission_context,
)
from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus

TASK_GET_TOOL_NAME = "TaskGet"
TASK_LIST_TOOL_NAME = "TaskList"
TASK_STOP_TOOL_NAME = "TaskStop"


class _TaskTool(BaseTool):
    backend_type = BackendType.META
    max_result_size_chars = 100_000
    should_defer = True

    def __init__(self) -> None:
        self._current_context: Any | None = None
        super().__init__(verbose=False, handle_errors=False)

    def set_context(self, context: Any) -> None:
        self._current_context = context

    def _task_manager(self) -> TaskManager | None:
        manager = getattr(self._current_context, "task_manager", None)
        return manager if isinstance(manager, TaskManager) else None

    async def _arun(self, **_: Any) -> ToolResult:
        raise NotImplementedError


class TaskGetTool(_TaskTool):
    """Retrieve status and output for a background task."""

    _name = TASK_GET_TOOL_NAME
    _description = "Get a background task's status and output by task_id."
    _is_read_only = True
    _is_concurrency_safe = True
    aliases = ["TaskOutput", "AgentOutputTool", "BashOutputTool"]
    search_hint = "read output logs from background task"
    parameter_descriptions = {
        "task_id": "The ID of the background task to inspect.",
        "block": "Whether to wait for task completion. Defaults to true.",
        "timeout": "Maximum wait time in milliseconds when block=true.",
    }

    async def _arun(
        self,
        task_id: str,
        block: bool = True,
        timeout: int = 30_000,
    ) -> ToolResult:
        manager = self._task_manager()
        if manager is None:
            return _error("TaskGet requires a session-scoped TaskManager.")
        if not task_id:
            return _error("Task ID is required.")
        try:
            timeout_ms = max(0, min(int(timeout), 600_000))
        except (TypeError, ValueError):
            timeout_ms = 30_000

        try:
            result = await manager.get_task_output(
                task_id,
                block=bool(block),
                timeout_ms=timeout_ms,
                abort_event=getattr(self._current_context, "abort_event", None),
            )
        except KeyError:
            return _error(f"No task found with ID: {task_id}")

        data = _task_output_result_to_dict(result)
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=_format_task_get_result(result),
            metadata={"tool": self.name, "task_result": data},
        )


class TaskListTool(_TaskTool):
    """List background tasks in the current OpenSpace session."""

    _name = TASK_LIST_TOOL_NAME
    _description = "List background tasks for the current session."
    _is_read_only = True
    _is_concurrency_safe = True
    search_hint = "list running background tasks"
    parameter_descriptions = {
        "status": "Optional status filter: pending, running, completed, failed, or killed.",
        "task_type": "Optional task type filter, for example local_bash or local_agent.",
        "include_bash": "Whether to include local_bash shell tasks. Defaults to true.",
        "team_name": "Optional team name filter.",
        "include_completed": "Whether to include terminal tasks. Defaults to true.",
    }

    async def _arun(
        self,
        status: str | None = None,
        task_type: str | None = None,
        include_bash: bool = True,
        team_name: str | None = None,
        include_completed: bool = True,
    ) -> ToolResult:
        manager = self._task_manager()
        if manager is None:
            return _error("TaskList requires a session-scoped TaskManager.")

        tasks = manager.list_all()
        if status:
            wanted = str(status)
            tasks = [task for task in tasks if task.status.value == wanted]
        if task_type:
            wanted_type = str(task_type)
            tasks = [task for task in tasks if task.type.value == wanted_type]
        if not include_bash:
            tasks = [task for task in tasks if task.type.value != "local_bash"]
        if team_name:
            tasks = [task for task in tasks if getattr(task, "team_name", None) == team_name]
        if not include_completed:
            tasks = [task for task in tasks if not is_terminal_task_status(task.status)]

        payload = {
            "tasks": [_task_summary(task) for task in tasks],
            "total": len(tasks),
            "running": sum(1 for task in tasks if task.status == TaskStatus.RUNNING),
        }
        lines = [
            f"{item['task_id']} [{item['status']}] {item['description']}"
            for item in payload["tasks"]
        ]
        content = "\n".join(lines) if lines else "No background tasks found"
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=content,
            metadata={"tool": self.name, "task_list": payload},
        )


class TaskStopTool(_TaskTool):
    """Stop a running background task."""

    _name = TASK_STOP_TOOL_NAME
    _description = "Stop a running background task by task_id."
    _is_read_only = False
    _is_concurrency_safe = True
    search_hint = "stop kill background task"
    parameter_descriptions = {
        "task_id": "The ID of the background task to stop.",
        "signal": "Signal to send for shell tasks: TERM or KILL. Defaults to TERM.",
    }

    async def check_permissions(
        self,
        input: dict[str, Any],
        context: Any = None,
    ):
        if getattr(context, "permission_context", None) is None:
            return deny_missing_permission_context(self._name)
        normalized = dict(input or {})
        signal = str(normalized.get("signal") or "TERM").upper()
        task_id = str(normalized.get("task_id") or "").strip()
        target = f" {task_id}" if task_id else ""
        return PermissionAsk(
            message=f"Stop background task{target} with signal {signal}.",
            updated_input=normalized,
        )

    async def _arun(
        self,
        task_id: str | None = None,
        signal: str = "TERM",
    ) -> ToolResult:
        manager = self._task_manager()
        if manager is None:
            return _error("TaskStop requires a session-scoped TaskManager.")
        if not task_id:
            return _error("Missing required parameter: task_id", code="missing_task_id")
        normalized_signal = str(signal or "TERM").upper()
        if normalized_signal not in {"TERM", "KILL"}:
            return _error(
                "Invalid signal. Use TERM or KILL.",
                code="invalid_signal",
            )

        try:
            result = await manager.stop_task_or_raise(
                task_id,
                signal_name=normalized_signal,
            )
        except StopTaskError as exc:
            return _error(str(exc), code=exc.code)

        payload = {
            "message": f"Successfully stopped task: {result['task_id']} ({result.get('command')})",
            "task_id": result["task_id"],
            "task_type": result["task_type"],
            "command": result.get("command"),
        }
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=json.dumps(payload, ensure_ascii=False),
            metadata={"tool": self.name, "task_stop": payload},
        )


def _task_summary(task: Any) -> dict[str, Any]:
    data = {
        "task_id": task.id,
        "task_type": task.type.value,
        "status": task.status.value,
        "state": task.status.value,
        "description": task.description,
        "output_file": task.output_file,
        "output_path": task.output_file,
        "start_time": task.start_time,
        "end_time": task.end_time,
        "duration_ms": (
            (task.end_time or time.time() * 1000) - task.start_time
            if task.start_time
            else None
        ),
    }
    if task.type.value == "local_bash":
        result = getattr(task, "result", None)
        data.update(
            {
                "agent_id": getattr(task, "agent_id", None),
                "command": getattr(task, "command", ""),
                "kind": getattr(task, "kind", "bash"),
                "pid": getattr(task, "pid", None),
                "exit_code": getattr(result, "code", None),
                "interrupted": getattr(result, "interrupted", None),
                "is_backgrounded": getattr(task, "is_backgrounded", True),
                "backgrounded_by_user": getattr(task, "backgrounded_by_user", False),
                "assistant_auto_backgrounded": getattr(
                    task, "assistant_auto_backgrounded", False
                ),
            }
        )
        return data
    data.update(
        {
            "agent_id": getattr(task, "agent_id", None),
            "agent_type": getattr(task, "agent_type", None),
            "team_name": getattr(task, "team_name", None),
            "parent_task_id": getattr(task, "parent_task_id", None),
        }
    )
    return data


def _task_output_result_to_dict(result: TaskOutputResult) -> dict[str, Any]:
    return {
        "retrieval_status": result.retrieval_status,
        "task": _task_output_to_dict(result.task) if result.task else None,
    }


def _task_output_to_dict(output: TaskOutput) -> dict[str, Any]:
    if is_dataclass(output):
        data = asdict(output)
    else:
        data = dict(getattr(output, "__dict__", {}))
    if "exit_code" in data:
        data["exitCode"] = data.pop("exit_code")
    if data.get("output_file") is not None:
        data["output_path"] = data["output_file"]
    if data.get("status") is not None:
        data["state"] = data["status"]
    return data


def _format_task_get_result(result: TaskOutputResult) -> str:
    parts = [f"<retrieval_status>{result.retrieval_status}</retrieval_status>"]
    task = result.task
    if task is None:
        return "\n\n".join(parts)
    parts.extend(
        [
            f"<task_id>{task.task_id}</task_id>",
            f"<task_type>{task.task_type}</task_type>",
            f"<status>{task.status}</status>",
            f"<state>{task.status}</state>",
        ]
    )
    if task.command:
        parts.append(f"<command>{task.command}</command>")
    if task.output_file:
        parts.append(f"<output_file>{task.output_file}</output_file>")
        parts.append(f"<output_path>{task.output_file}</output_path>")
    if task.pid is not None:
        parts.append(f"<pid>{task.pid}</pid>")
    if task.is_backgrounded is not None:
        parts.append(
            f"<is_backgrounded>{str(task.is_backgrounded).lower()}</is_backgrounded>"
        )
    if task.kind:
        parts.append(f"<kind>{task.kind}</kind>")
    if task.backgrounded_by_user is not None:
        parts.append(
            "<backgrounded_by_user>"
            f"{str(task.backgrounded_by_user).lower()}"
            "</backgrounded_by_user>"
        )
    if task.assistant_auto_backgrounded is not None:
        parts.append(
            "<assistant_auto_backgrounded>"
            f"{str(task.assistant_auto_backgrounded).lower()}"
            "</assistant_auto_backgrounded>"
        )
    if task.duration_ms is not None:
        parts.append(f"<duration_ms>{int(task.duration_ms)}</duration_ms>")
    if task.exit_code is not None:
        parts.append(f"<exit_code>{task.exit_code}</exit_code>")
    if task.interrupted is not None:
        parts.append(f"<interrupted>{str(task.interrupted).lower()}</interrupted>")
    if task.output_tail and task.output_tail.strip() and task.output_tail != task.output:
        parts.append(f"<output_tail>\n{task.output_tail.rstrip()}\n</output_tail>")
    if task.output and task.output.strip():
        parts.append(f"<output>\n{task.output.rstrip()}\n</output>")
    if task.error:
        parts.append(f"<error>{task.error}</error>")
    return "\n\n".join(parts)


def _error(message: str, *, code: str | None = None) -> ToolResult:
    metadata: dict[str, Any] = {"tool": "task_tools"}
    if code:
        metadata["code"] = code
    return ToolResult(
        status=ToolStatus.ERROR,
        content=f"Error: {message}",
        error=message,
        metadata=metadata,
    )


__all__ = [
    "TASK_GET_TOOL_NAME",
    "TASK_LIST_TOOL_NAME",
    "TASK_STOP_TOOL_NAME",
    "TaskGetTool",
    "TaskListTool",
    "TaskStopTool",
]
