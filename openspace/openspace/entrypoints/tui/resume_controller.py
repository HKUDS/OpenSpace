from __future__ import annotations

from openspace.core.tui_bridge import TUIBridge
from openspace.protocol import CoreToTuiEvent
from openspace import OpenSpace

async def _send_bridge_notification(
    bridge: TUIBridge,
    level: str,
    title: str,
    message: str,
) -> None:
    await bridge.send(
        CoreToTuiEvent.NOTIFICATION.value,
        {
            "level": level,
            "title": title,
            "message": message,
        },
    )


async def _sync_runtime_status(
    openspace: OpenSpace,
    bridge: TUIBridge,
    extra: dict | None = None,
) -> None:
    payload = openspace.get_runtime_status()
    if extra:
        payload.update(extra)
    await bridge.send(CoreToTuiEvent.STATUS_UPDATE.value, payload)


async def _send_restored_todo_state(
    bridge: TUIBridge,
    restored: dict,
) -> None:
    runtime = restored.get("runtime")
    if not isinstance(runtime, dict):
        return
    todo_state = runtime.get("todo_state")
    if not isinstance(todo_state, dict):
        return
    session_id = restored.get("session_id")
    for todo_key, todos in todo_state.items():
        if not isinstance(todos, list):
            continue
        await bridge.send(
            CoreToTuiEvent.TODO_UPDATE.value,
            {
                "session_id": session_id,
                "todo_key": str(todo_key),
                "agent_id": "primary",
                "oldTodos": [],
                "newTodos": todos,
                "storedTodos": todos,
                "todos": todos,
                "all_done": len(todos) == 0,
                "verificationNudgeNeeded": False,
            },
        )


async def _send_bridge_command_result(
    bridge: TUIBridge,
    command: str,
    message: str | None = None,
    *,
    display: str = "system",
    clear_messages: bool = False,
    next_input: str | None = None,
    submit_next_input: bool = False,
) -> None:
    payload: dict[str, object] = {
        "command": command,
        "display": display,
        "clear_messages": clear_messages,
        "submit_next_input": submit_next_input,
    }
    if message is not None:
        payload["message"] = message
    if next_input is not None:
        payload["next_input"] = next_input
    await bridge.send(CoreToTuiEvent.COMMAND_RESULT.value, payload)


async def _handle_resume_event(
    openspace: OpenSpace,
    bridge: TUIBridge,
    data: dict,
) -> str | None:
    action = str(data.get("action", "list"))
    session_id = data.get("session_id")

    if action == "list":
        page = int(data.get("page") or 0)
        page_size = int(data.get("page_size") or 20)
        all_projects = bool(data.get("all_projects") or data.get("show_all_projects"))
        discovered = await openspace.discover_sessions(
            page=page,
            page_size=page_size,
            all_projects=all_projects,
        )
        await bridge.send(
            CoreToTuiEvent.SESSION_LIST.value,
            discovered,
        )
        return None

    if action == "rewind":
        rewind_session_id = str(
            data.get("session_id") or openspace.current_session_id or ""
        ).strip()
        rewind_messages = data.get("messages")

        if not rewind_session_id:
            await _send_bridge_command_result(
                bridge,
                "rewind",
                "Rewind failed: missing session id",
            )
            await _send_bridge_notification(
                bridge,
                "warn",
                "Rewind",
                "Missing session id",
            )
            return None

        if not isinstance(rewind_messages, list):
            await _send_bridge_command_result(
                bridge,
                "rewind",
                "Rewind failed: missing transcript payload",
            )
            await _send_bridge_notification(
                bridge,
                "warn",
                "Rewind",
                "Missing rewind transcript payload",
            )
            return None

        try:
            restored = await openspace.rewind_session(
                rewind_session_id,
                [
                    message
                    for message in rewind_messages
                    if isinstance(message, dict)
                ],
            )
        except FileNotFoundError:
            await _send_bridge_command_result(
                bridge,
                "rewind",
                f"Rewind failed: session not found ({rewind_session_id})",
            )
            await _send_bridge_notification(
                bridge,
                "warn",
                "Rewind",
                f"Session not found: {rewind_session_id}",
            )
            return None
        await bridge.send(
            CoreToTuiEvent.SESSION_RESTORED.value,
            {
                "session_id": rewind_session_id,
                "title": restored.get("title"),
                "mode": restored.get("mode"),
                "metadata": restored.get("metadata", {}),
                "runtime": restored.get("runtime", {}),
                "messages": restored.get("messages", []),
                "cost": restored.get("cost_total"),
                "agent": restored.get("agent"),
                "standalone_agent_context": restored.get(
                    "standalone_agent_context"
                ),
                "worktree": restored.get("worktree"),
                "file_history_snapshots": restored.get(
                    "file_history_snapshots", []
                ),
                "content_replacements": restored.get(
                    "content_replacements", []
                ),
            },
        )
        await _send_restored_todo_state(bridge, restored)
        await _sync_runtime_status(openspace, bridge, {"phase": "rewound"})
        return rewind_session_id

    if not session_id:
        await _send_bridge_notification(
            bridge,
            "warn",
            "Resume",
            "Missing session id",
        )
        return None

    if action == "restore":
        try:
            restored = await openspace.restore_session(session_id)
        except FileNotFoundError:
            await _send_bridge_notification(
                bridge,
                "warn",
                "Resume",
                f"Session not found: {session_id}",
            )
            return None
        await bridge.send(
            CoreToTuiEvent.SESSION_RESTORED.value,
            {
                "session_id": session_id,
                "title": restored.get("title"),
                "mode": restored.get("mode"),
                "metadata": restored.get("metadata", {}),
                "runtime": restored.get("runtime", {}),
                "messages": restored.get("messages", []),
                "cost": restored.get("cost_total"),
                "agent": restored.get("agent"),
                "standalone_agent_context": restored.get(
                    "standalone_agent_context"
                ),
                "worktree": restored.get("worktree"),
                "file_history_snapshots": restored.get(
                    "file_history_snapshots", []
                ),
                "content_replacements": restored.get(
                    "content_replacements", []
                ),
            },
        )
        await _send_restored_todo_state(bridge, restored)
        await _sync_runtime_status(openspace, bridge)
        return session_id

    if action == "fork":
        try:
            restored = await openspace.fork_session(session_id)
        except FileNotFoundError:
            await _send_bridge_notification(
                bridge,
                "warn",
                "Resume",
                f"Session not found: {session_id}",
            )
            return None
        forked_session_id = restored["session_id"]
        await bridge.send(
            CoreToTuiEvent.SESSION_RESTORED.value,
            {
                "session_id": forked_session_id,
                "title": restored.get("title"),
                "mode": restored.get("mode"),
                "metadata": restored.get("metadata", {}),
                "runtime": restored.get("runtime", {}),
                "messages": restored.get("messages", []),
                "cost": restored.get("cost_total"),
                "agent": restored.get("agent"),
                "standalone_agent_context": restored.get(
                    "standalone_agent_context"
                ),
                "worktree": restored.get("worktree"),
                "file_history_snapshots": restored.get(
                    "file_history_snapshots", []
                ),
                "content_replacements": restored.get(
                    "content_replacements", []
                ),
            },
        )
        await _send_restored_todo_state(bridge, restored)
        await _sync_runtime_status(openspace, bridge)
        return forked_session_id

    await _send_bridge_notification(
        bridge,
        "warn",
        "Resume",
        f"Unknown resume action: {action}",
    )
    return None



handle_resume_event = _handle_resume_event
send_bridge_notification = _send_bridge_notification
send_bridge_command_result = _send_bridge_command_result
sync_runtime_status = _sync_runtime_status
send_restored_todo_state = _send_restored_todo_state
