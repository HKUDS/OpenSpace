"""Helpers for public/direct tool invocation entrypoints.

Agent turns build a rich ``ToolUseContext`` in ``GroundingAgent``.  Public
facades such as ``GroundingClient.invoke_tool()``, ``BaseSession.call_tool()``,
and slash-command helpers still need the same permission and hook wiring when
they call ``run_tool_use()`` outside a full agent turn.
"""

from __future__ import annotations

import os
from typing import Any, Sequence

from openspace.grounding.core.tool.base import BaseTool
from openspace.services.tooling.context import ReadFileEntry, ToolUseContext


def build_direct_tool_use_context(
    *,
    tools: Sequence[BaseTool],
    model: str,
    agent_id: str,
    cwd: str | None = None,
    all_tools: Sequence[BaseTool] | None = None,
    recording_manager: Any | None = None,
    quality_manager: Any | None = None,
    permission_context: Any | None = None,
    permission_mode: str | None = None,
    hook_registry: Any | None = None,
    read_file_state: dict[str, ReadFileEntry] | None = None,
    messages: list[dict[str, Any]] | None = None,
    event_sink: Any | None = None,
    tool_results_dir: str | None = None,
    session_id: str | None = None,
    tui_available: bool = False,
) -> ToolUseContext:
    """Build a full pipeline context for non-agent tool entrypoints."""

    tool_list = list(tools)
    resolved_cwd = str(cwd or _derive_cwd(tool_list) or os.getcwd())

    if permission_context is None:
        from openspace.grounding.core.permissions.loader import (
            load_tool_permission_context,
        )

        permission_context = load_tool_permission_context(
            resolved_cwd,
            permission_mode,
        )

    if hook_registry is None:
        from openspace.services.tooling.hooks import HookRegistry, setup_default_hooks

        hook_registry = HookRegistry()
        setup_default_hooks(hook_registry)

    if quality_manager is None:
        quality_manager = _derive_quality_manager(tool_list)
    if recording_manager is None:
        recording_manager = _derive_recording_manager(tool_list)

    return ToolUseContext(
        tools=tool_list,
        all_tools=list(all_tools or tool_list),
        model=model,
        cwd=resolved_cwd,
        agent_id=agent_id,
        messages=messages if messages is not None else [],
        read_file_state=read_file_state if read_file_state is not None else {},
        recording_manager=recording_manager,
        quality_manager=quality_manager,
        hook_registry=hook_registry,
        permission_context=permission_context,
        permission_mode=getattr(permission_context, "mode", permission_mode or "default"),
        event_sink=event_sink,
        tool_results_dir=tool_results_dir,
        session_id=session_id,
        tui_available=tui_available,
    )


def _derive_quality_manager(tools: Sequence[BaseTool]) -> Any | None:
    client = _derive_grounding_client(tools)
    if client is not None:
        manager = getattr(client, "quality_manager", None)
        if manager is not None:
            return manager

    try:
        from openspace.grounding.core.quality import get_quality_manager

        return get_quality_manager()
    except Exception:
        return None


def _derive_recording_manager(tools: Sequence[BaseTool]) -> Any | None:
    client = _derive_grounding_client(tools)
    if client is None:
        return None
    return getattr(client, "recording_manager", None)


def _derive_cwd(tools: Sequence[BaseTool]) -> str | None:
    for tool in tools:
        for obj in (
            tool,
            getattr(tool, "_session", None),
            getattr(tool, "connector", None),
        ):
            if obj is None:
                continue
            for attr in (
                "default_working_dir",
                "_default_working_dir",
                "workspace_dir",
                "working_dir",
                "cwd",
            ):
                value = getattr(obj, attr, None)
                if isinstance(value, os.PathLike):
                    return os.fspath(value)
                if isinstance(value, str) and value:
                    return value
    return None


def _derive_grounding_client(tools: Sequence[BaseTool]) -> Any | None:
    for tool in tools:
        runtime_info = getattr(tool, "runtime_info", None)
        client = getattr(runtime_info, "grounding_client", None)
        if client is not None:
            return client

        client = getattr(tool, "client", None)
        if client is not None:
            return client
    return None
