from __future__ import annotations

from openspace.cli.slash_commands import SlashCommandContext, execute_slash_command
from openspace.core.tui_bridge import TUIBridge
from openspace import OpenSpace

from openspace.entrypoints.tui.resume_controller import (
    _send_bridge_command_result,
    _send_bridge_notification,
    _sync_runtime_status,
    handle_resume_event,
)
from openspace.entrypoints.tui.settings_controller import handle_settings_update

async def handle_slash_command(
    openspace: OpenSpace,
    bridge: TUIBridge,
    data: dict,
) -> str | None:
    command = str(data.get("command", "")).lstrip("/").lower()
    args = data.get("args") or []
    context = SlashCommandContext(
        openspace=openspace,
        bridge=bridge,
        handle_resume=lambda payload: handle_resume_event(openspace, bridge, payload),
        handle_settings_update=lambda payload: handle_settings_update(openspace, bridge, payload),
        send_notification=lambda level, title, message: _send_bridge_notification(
            bridge,
            level,
            title,
            message,
        ),
        send_command_result=lambda name, message=None, **kwargs: _send_bridge_command_result(
            bridge,
            name,
            message,
            **kwargs,
        ),
        sync_status=lambda extra=None: _sync_runtime_status(openspace, bridge, extra),
    )
    outcome = await execute_slash_command(
        context,
        command,
        args if isinstance(args, list) else [],
    )
    return outcome.session_id
