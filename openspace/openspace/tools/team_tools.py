"""Team and teammate messaging tools for OpenSpace multi-agent sessions.

Implementation notes:
- ``tools/TeamCreateTool/TeamCreateTool.ts``
- ``tools/TeamDeleteTool/TeamDeleteTool.ts``
- ``tools/SendMessageTool/SendMessageTool.ts``

OpenSpace keeps the engine branch in-process: no tmux/iTerm panes, disk
mailboxes, UDS bridge, or remote-control bridge are emulated here.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Mapping

from openspace.agents.task_manager import TaskManager, TaskStatus
from openspace.grounding.core.permissions import (
    PermissionAsk,
    deny_missing_permission_context,
)
from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus

TEAM_CREATE_TOOL_NAME = "TeamCreate"
TEAM_DELETE_TOOL_NAME = "TeamDelete"
SEND_MESSAGE_TOOL_NAME = "SendMessage"
TEAM_LEAD_NAME = "team-lead"


class _TeamTool(BaseTool):
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

    def _set_coordinator_enabled(self, enabled: bool) -> None:
        context = self._current_context
        if context is None:
            return
        _set_context_value(context, "coordinator_mode_enabled", bool(enabled))
        if enabled:
            if _get_context_value(context, "coordinator_notification_queue") is None:
                _set_context_value(
                    context,
                    "coordinator_notification_queue",
                    asyncio.Queue(),
                )
        else:
            _set_context_value(context, "coordinator_notification_queue", None)

    async def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        context = self._current_context
        emit_event = getattr(context, "emit_event", None)
        if emit_event is None:
            return
        try:
            result = emit_event(event_type, payload)
            if hasattr(result, "__await__"):
                await result
        except Exception:
            return

    async def _emit_agent_event(self, event: str, payload: dict[str, Any]) -> None:
        await self._emit_event(
            "agent_event",
            {
                "session_id": getattr(self._current_context, "session_id", None),
                "agent_id": str(
                    getattr(self._current_context, "agent_id", None) or "primary"
                ),
                "event": event,
                "payload": payload,
            },
        )

    async def _arun(self, **_: Any) -> ToolResult:
        raise NotImplementedError


class TeamCreateTool(_TeamTool):
    """Create a logical team for in-process teammates."""

    _name = TEAM_CREATE_TOOL_NAME
    _description = "Create a multi-agent team in the current session."
    _is_read_only = False
    _is_concurrency_safe = True
    search_hint = "create multi agent team swarm"
    parameter_descriptions = {
        "team_name": "Name for the team to create.",
        "description": "Optional team purpose or task summary.",
        "agent_type": "Optional team lead role name recorded in metadata.",
    }

    async def check_permissions(
        self,
        input: dict[str, Any],
        context: Any = None,
    ):
        if getattr(context, "permission_context", None) is None:
            return deny_missing_permission_context(self._name)
        return PermissionAsk(
            message="Create a multi-agent team and enter coordinator mode.",
            updated_input=dict(input or {}),
        )

    async def _arun(
        self,
        team_name: str,
        description: str = "",
        agent_type: str | None = None,
    ) -> ToolResult:
        manager = self._task_manager()
        if manager is None:
            return _error("TeamCreate requires a session-scoped TaskManager.")
        if not team_name or not str(team_name).strip():
            return _error("team_name is required for TeamCreate.")

        try:
            payload = manager.create_team(
                str(team_name).strip(),
                description=description,
                agent_type=agent_type or TEAM_LEAD_NAME,
                lead_agent_id=_format_lead_agent_id(str(team_name).strip()),
            )
        except ValueError as exc:
            return _error(str(exc))
        self._set_coordinator_enabled(True)
        payload["coordinator_mode_enabled"] = True

        team_update = {
            "action": "created",
            "status": "created",
            "team_name": payload.get("team_name"),
            "coordinator_mode_enabled": True,
            "description": payload.get("description"),
            "lead_agent_id": payload.get("lead_agent_id"),
            "lead_agent_type": payload.get("lead_agent_type"),
            "running_workers": 0,
            "total_workers": 0,
            "session_id": getattr(self._current_context, "session_id", None),
        }
        await self._emit_event("team_update", team_update)
        await self._emit_agent_event("team_update", team_update)

        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=json.dumps(payload, ensure_ascii=False),
            metadata={"tool": self.name, "team": payload},
        )


class TeamDeleteTool(_TeamTool):
    """Delete the current logical team after teammates have stopped."""

    _name = TEAM_DELETE_TOOL_NAME
    _description = "Delete the current team after active teammates have stopped."
    _is_read_only = False
    _is_concurrency_safe = True
    search_hint = "delete cleanup multi agent team"

    async def check_permissions(
        self,
        input: dict[str, Any],
        context: Any = None,
    ):
        if getattr(context, "permission_context", None) is None:
            return deny_missing_permission_context(self._name)
        return PermissionAsk(
            message="Delete the active multi-agent team and exit coordinator mode.",
            updated_input=dict(input or {}),
        )

    async def _arun(self) -> ToolResult:
        manager = self._task_manager()
        if manager is None:
            return _error("TeamDelete requires a session-scoped TaskManager.")

        team_name = manager.active_team_name
        if team_name:
            active_members = [
                task
                for task in manager.list_by_team(team_name)
                if task.status == TaskStatus.RUNNING
            ]
            if active_members:
                names = ", ".join(task.agent_type for task in active_members)
                payload = {
                    "success": False,
                    "message": (
                        f"Cannot cleanup team with {len(active_members)} active "
                        f"member(s): {names}. Use SendMessage with a "
                        "shutdown_request or TaskStop to terminate teammates first."
                    ),
                    "team_name": team_name,
                }
                team_update = {
                    "action": "delete_blocked",
                    "status": "blocked",
                    "team_name": team_name,
                    "coordinator_mode_enabled": True,
                    "running_workers": len(active_members),
                    "total_workers": len(manager.list_by_team(team_name)),
                    "session_id": getattr(self._current_context, "session_id", None),
                    "message": payload["message"],
                }
                await self._emit_event("team_update", team_update)
                await self._emit_agent_event("team_update", team_update)
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    content=json.dumps(payload, ensure_ascii=False),
                    metadata={"tool": self.name, "team_delete": payload},
                )

        payload = manager.delete_team()
        self._set_coordinator_enabled(False)
        payload["coordinator_mode_enabled"] = False
        team_update = {
            "action": "deleted",
            "status": "deleted" if payload.get("team_name") else "idle",
            "team_name": payload.get("team_name"),
            "coordinator_mode_enabled": False,
            "running_workers": 0,
            "total_workers": 0,
            "session_id": getattr(self._current_context, "session_id", None),
            "message": payload.get("message"),
        }
        await self._emit_event("team_update", team_update)
        await self._emit_agent_event("team_update", team_update)
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=json.dumps(payload, ensure_ascii=False),
            metadata={"tool": self.name, "team_delete": payload},
        )


class SendMessageTool(_TeamTool):
    """Queue a message into another agent's inbox."""

    _name = SEND_MESSAGE_TOOL_NAME
    _description = "Send a message to an agent teammate by name, task_id, or '*'."
    _is_read_only = True
    _is_concurrency_safe = True
    search_hint = "send message teammate worker agent"
    parameter_descriptions = {
        "to": "Recipient teammate name, task_id, agent_id, name@team, or '*' for broadcast.",
        "message": "Plain text or a structured message object.",
        "summary": "Optional short preview for plain text messages.",
        "task_id": "Optional explicit task ID recipient. Overrides to.",
        "to_agent": "Optional teammate/agent recipient alias. Overrides to.",
    }

    def is_read_only(self, input: dict[str, Any] | None = None) -> bool:
        message = (input or {}).get("message")
        return not (isinstance(message, Mapping) and message.get("type") == "shutdown_response")

    async def _arun(
        self,
        to: str | None = None,
        message: Any = "",
        summary: str | None = None,
        task_id: str | None = None,
        to_agent: str | None = None,
    ) -> ToolResult:
        manager = self._task_manager()
        if manager is None:
            return _error("SendMessage requires a session-scoped TaskManager.")

        target = task_id or to_agent or to
        if not target or not str(target).strip():
            return _error('to, to_agent, or task_id is required for SendMessage.')
        target = str(target).strip()

        payload = _message_payload(
            message,
            summary=summary,
            sender=str(getattr(self._current_context, "agent_type", None) or TEAM_LEAD_NAME),
        )
        if target == "*":
            count = await manager.broadcast_message(
                payload,
                team_name=manager.active_team_name,
                exclude_agent=str(getattr(self._current_context, "agent_type", "") or ""),
            )
            data = {
                "success": True,
                "message": f"Message broadcast to {count} teammate(s)",
                "recipients": count,
            }
            return _success(self.name, data, "send_message")

        delivered = await manager.send_message(target, payload)
        data = {
            "success": delivered,
            "message": (
                f"Message queued for delivery to {target} at its next agent loop."
                if delivered
                else f"No running task or teammate found for {target}."
            ),
            "target": target,
            "queued": delivered,
        }
        return _success(self.name, data, "send_message")


def _message_payload(message: Any, *, summary: str | None, sender: str) -> dict[str, Any]:
    if isinstance(message, Mapping):
        payload = dict(message)
        payload.setdefault("summary", summary)
        payload.setdefault("from", sender)
        payload.setdefault("timestamp", time.time())
        return payload
    text = str(message)
    return {
        "type": "message",
        "from": sender,
        "content": text,
        "text": text,
        "summary": summary,
        "timestamp": time.time(),
    }


def _get_context_value(context: Any, key: str, default: Any = None) -> Any:
    if isinstance(context, dict):
        return context.get(key, default)
    return getattr(context, key, default)


def _set_context_value(context: Any, key: str, value: Any) -> None:
    if isinstance(context, dict):
        context[key] = value
        return
    try:
        setattr(context, key, value)
    except Exception:
        return


def _format_lead_agent_id(team_name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in team_name)
    return f"{TEAM_LEAD_NAME}@{cleaned.strip('-') or 'team'}"


def _success(tool_name: str, data: dict[str, Any], key: str) -> ToolResult:
    return ToolResult(
        status=ToolStatus.SUCCESS,
        content=json.dumps(data, ensure_ascii=False),
        metadata={"tool": tool_name, key: data},
    )


def _error(message: str) -> ToolResult:
    return ToolResult(
        status=ToolStatus.ERROR,
        content=f"Error: {message}",
        error=message,
        metadata={"tool": "team_tools"},
    )


__all__ = [
    "SEND_MESSAGE_TOOL_NAME",
    "TEAM_CREATE_TOOL_NAME",
    "TEAM_DELETE_TOOL_NAME",
    "SendMessageTool",
    "TeamCreateTool",
    "TeamDeleteTool",
]
