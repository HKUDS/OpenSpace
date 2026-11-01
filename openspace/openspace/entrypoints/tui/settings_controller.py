from __future__ import annotations

import logging
import os

from openspace.core.tui_bridge import TUIBridge
from openspace.grounding.core.permissions import set_session_permission_mode
from openspace.protocol import CoreToTuiEvent
from openspace import OpenSpace

from openspace.entrypoints.tui.resume_controller import (
    _send_bridge_notification,
    _sync_runtime_status,
)

logger = logging.getLogger(__name__)


async def _send_settings_update(
    bridge: TUIBridge,
    key: str,
    value: object,
) -> None:
    await bridge.send(
        CoreToTuiEvent.SETTINGS_UPDATE.value,
        {"key": key, "value": value},
    )


async def _handle_settings_update(
    openspace: OpenSpace,
    bridge: TUIBridge,
    data: dict,
) -> None:
    key = data.get("key")
    value = data.get("value")

    if not isinstance(key, str) or not key:
        return

    cwd = openspace.config.workspace_dir or os.getcwd()
    runtime_permission_keys = {"permissionMode", "toolPermissionContext.mode"}

    if key not in runtime_permission_keys:
        try:
            from openspace.services.runtime_support.settings import get_setting, update_setting

            if not data.get("persisted"):
                update_setting(key, value, cwd=cwd, source="userSettings")
            value = get_setting(key, value, cwd=cwd, refresh=True)
            await _send_settings_update(bridge, key, value)
        except Exception as exc:
            logger.debug("Failed to persist settings update %s=%r", key, value, exc_info=True)
            await _send_bridge_notification(
                bridge,
                "error",
                "Settings",
                f"Failed to update {key}: {exc}",
            )
            try:
                from openspace.services.runtime_support.settings import get_setting

                current_value = get_setting(key, None, cwd=cwd, refresh=True)
                await _send_settings_update(bridge, key, current_value)
            except Exception:
                logger.debug("Failed to send settings rollback for %s", key, exc_info=True)
            return

    if key == "model" and isinstance(value, str) and value:
        openspace.update_main_loop_model(value)
        await _sync_runtime_status(openspace, bridge)
        return

    if key == "alwaysThinkingEnabled" and isinstance(value, bool):
        openspace.update_thinking_enabled(value)
        await _sync_runtime_status(openspace, bridge)
        return

    if key in ("permissionMode", "toolPermissionContext.mode", "permissions.defaultMode") and isinstance(value, str):
        try:
            set_session_permission_mode(
                value,
                cwd,
            )
        except ValueError:
            await _send_bridge_notification(
                bridge,
                "warning",
                "Permissions",
                f"Unsupported permission mode: {value}",
            )
            return
        logger.info("TUI permission mode updated: %s", value)
        await _send_bridge_notification(
            bridge,
            "info",
            "Permissions",
            f"Permission mode set to {value}",
        )
        return

    logger.debug("Ignoring settings update from TUI: %s=%r", key, value)



handle_settings_update = _handle_settings_update
