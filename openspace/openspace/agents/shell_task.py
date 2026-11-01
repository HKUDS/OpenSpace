from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from typing import Any, Literal

from openspace.agents.task_manager import TaskStateBase, TaskStatus

LocalShellTaskKind = Literal["bash", "monitor"]
BACKGROUND_BASH_SUMMARY_PREFIX = "Background command "


@dataclass(slots=True)
class ShellTaskResult:
    code: int
    interrupted: bool = False


@dataclass(slots=True)
class LocalShellTaskState(TaskStateBase):
    command: str = ""
    result: ShellTaskResult | None = None
    completion_status_sent_in_attachment: bool = False
    shell_command: Any | None = None
    last_reported_total_lines: int = 0
    is_backgrounded: bool = True
    agent_id: str | None = None
    kind: LocalShellTaskKind = "bash"
    pid: int | None = None
    backgrounded_by_user: bool = False
    assistant_auto_backgrounded: bool = False
    notification_queue: asyncio.Queue[Any] | None = None
    finalizer_task: asyncio.Task[Any] | None = None
    stall_watchdog_task: asyncio.Task[Any] | None = None


_PROMPT_PATTERNS = (
    re.compile(r"\(y/n\)", re.I),
    re.compile(r"\[y/n\]", re.I),
    re.compile(r"\(yes/no\)", re.I),
    re.compile(r"\b(?:Do you|Would you|Shall I|Are you sure|Ready to)\b.*\? *$", re.I),
    re.compile(r"Press (any key|Enter)", re.I),
    re.compile(r"Continue\?", re.I),
    re.compile(r"Overwrite\?", re.I),
)


def is_local_shell_task(task: Any) -> bool:
    return isinstance(task, LocalShellTaskState) or getattr(task, "type", None) == "local_bash"


def is_background_task(task: Any) -> bool:
    status = getattr(task, "status", None)
    status_value = status.value if isinstance(status, TaskStatus) else str(status)
    if status_value not in {TaskStatus.PENDING.value, TaskStatus.RUNNING.value}:
        return False
    return getattr(task, "is_backgrounded", True) is not False


def looks_like_prompt(tail: str) -> bool:
    last_line = tail.rstrip().split("\n")[-1] if tail else ""
    return any(pattern.search(last_line) for pattern in _PROMPT_PATTERNS)


def build_shell_stall_notification_xml(task: LocalShellTaskState, tail: str) -> str:
    summary = (
        f'{BACKGROUND_BASH_SUMMARY_PREFIX}"{task.description}" appears to be '
        "waiting for interactive input"
    )
    parts = [
        "<task-notification>",
        f"<task-id>{task.id}</task-id>",
    ]
    if task.tool_use_id:
        parts.append(f"<tool-use-id>{task.tool_use_id}</tool-use-id>")
    if task.output_file:
        parts.append(f"<output-file>{task.output_file}</output-file>")
    parts.extend(
        [
            f"<summary>{html.escape(summary)}</summary>",
            "</task-notification>",
            "Last output:",
            tail.rstrip(),
            "",
            "The command is likely blocked on an interactive prompt. Kill this task "
            "and re-run with piped input (for example, `echo y | command`) or a "
            "non-interactive flag if one exists.",
        ]
    )
    return "\n".join(parts)


def build_shell_notification_xml(task: LocalShellTaskState) -> str:
    status = task.status.value if isinstance(task.status, TaskStatus) else str(task.status)
    exit_code = task.result.code if task.result is not None else None
    if task.kind == "monitor":
        if status == TaskStatus.COMPLETED.value:
            summary = f'Monitor "{task.description}" stream ended'
        elif status == TaskStatus.KILLED.value:
            summary = f'Monitor "{task.description}" was stopped'
        else:
            summary = f'Monitor "{task.description}" failed'
    else:
        if status == TaskStatus.COMPLETED.value:
            suffix = f" (exit code {exit_code})" if exit_code is not None else ""
            summary = f'{BACKGROUND_BASH_SUMMARY_PREFIX}"{task.description}" completed{suffix}'
        elif status == TaskStatus.KILLED.value:
            summary = f'{BACKGROUND_BASH_SUMMARY_PREFIX}"{task.description}" was stopped'
        else:
            suffix = f" (exit code {exit_code})" if exit_code is not None else ""
            summary = f'{BACKGROUND_BASH_SUMMARY_PREFIX}"{task.description}" failed{suffix}'

    parts = [
        "<task-notification>",
        f"<task-id>{task.id}</task-id>",
    ]
    if task.tool_use_id:
        parts.append(f"<tool-use-id>{task.tool_use_id}</tool-use-id>")
    if task.output_file:
        parts.append(f"<output-file>{task.output_file}</output-file>")
    parts.extend(
        [
            f"<status>{status}</status>",
            f"<summary>{summary}</summary>",
            "</task-notification>",
        ]
    )
    return "\n".join(parts)


__all__ = [
    "BACKGROUND_BASH_SUMMARY_PREFIX",
    "LocalShellTaskKind",
    "LocalShellTaskState",
    "ShellTaskResult",
    "build_shell_notification_xml",
    "build_shell_stall_notification_xml",
    "is_background_task",
    "is_local_shell_task",
    "looks_like_prompt",
]
