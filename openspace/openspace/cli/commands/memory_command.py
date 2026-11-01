"""Backend implementation of the ``/memory`` slash command.

OpenSpace's command is a local React dialog.  OpenSpace's core command surface is
headless, so this module exposes the same state transitions as text commands:
clear caches, preload/list memory files, create the selected file, and launch
an external editor when one is configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openspace.tools.memory_tools import (
    format_memory_targets,
    get_relative_memory_path,
    list_memory_targets,
    open_memory_target,
    resolve_memory_target,
)


@dataclass(slots=True)
class MemoryCommandResult:
    message: str
    display: str = "system"


def build_memory_selector_payload(
    *,
    openspace: Any | None = None,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Build the TUI selector payload for ``/memory``.

    This mirrors OpenSpace's ``MemoryFileSelector`` data model, but the actual target
    discovery stays in Python so the TUI does not duplicate OpenSpace memory
    path and auto-memory rules.
    """

    project_path = Path(cwd or _resolve_project_path(openspace)).expanduser().resolve()
    targets = list_memory_targets(cwd=project_path)
    return {
        "cwd": str(project_path),
        "targets": [
            {
                "label": target.label,
                "path": str(target.path),
                "kind": target.kind,
                "description": target.description,
                "exists": target.exists,
                "is_folder": target.is_folder,
                "display_path": get_relative_memory_path(target.path, cwd=project_path),
            }
            for target in targets
        ],
    }


async def execute_memory_command(
    args: list[str],
    *,
    openspace: Any | None = None,
    cwd: str | Path | None = None,
    launch_editor: bool = True,
) -> MemoryCommandResult:
    """Execute ``/memory``.

    Supported forms:
    - ``/memory`` or ``/memory list``: list editable memory files/folders.
    - ``/memory edit [user|project|local|auto|folder|<path>]``: create/open.
    - ``/memory read [filename]``: print an auto-memory topic or ``MEMORY.md``.
    - ``/memory logs [all]``: print daily-log entries.
    """

    project_path = Path(cwd or _resolve_project_path(openspace)).expanduser().resolve()
    if not args or args[0].lower() in {"list", "ls"}:
        targets = list_memory_targets(cwd=project_path)
        return MemoryCommandResult(format_memory_targets(targets, cwd=project_path))

    action = args[0].lower()
    if action in {"edit", "open"}:
        selector = " ".join(args[1:]).strip() or "user"
        try:
            target = resolve_memory_target(selector, cwd=project_path)
            message, opened = open_memory_target(
                target,
                cwd=project_path,
                launch_editor=launch_editor,
            )
        except Exception as exc:  # noqa: BLE001 - mirrors OpenSpace's catch-all UI branch
            return MemoryCommandResult(f"Error opening memory file: {exc}", display="system")
        _clear_memory_caches()
        if target.is_folder:
            return MemoryCommandResult(message, display="system")
        if not opened:
            message += "\n\nEditor was not launched; the file is ready to edit."
        return MemoryCommandResult(message, display="system")

    if action == "read":
        filename = " ".join(args[1:]).strip() or "MEMORY.md"
        from openspace.tools.memory_tools import MemoryReadTool
        from openspace.tool_runtime.direct_context import build_direct_tool_use_context
        from openspace.tool_runtime.pipeline.execution import (
            run_tool_use,
            tool_call_result_to_tool_result,
        )

        tool = MemoryReadTool()
        tool_call = {
            "id": "memory-command-read",
            "type": "function",
            "function": {"name": tool.schema.name, "arguments": {"filename": filename}},
        }
        tool_context = build_direct_tool_use_context(
            tools=[tool],
            all_tools=[tool],
            model="memory-command",
            cwd=str(project_path),
            agent_id="memory-command",
            read_file_state={},
            tui_available=False,
        )
        result = tool_call_result_to_tool_result(
            await run_tool_use(tool_call, {tool.schema.name: tool}, tool_context)
        )
        if result.is_error:
            return MemoryCommandResult(str(result.content or result.error), display="system")
        return MemoryCommandResult(str(result.content), display="system")

    if action == "logs":
        from openspace.services.memory import format_daily_log_entries, get_auto_mem_path

        include_consolidated = any(arg.lower() in {"all", "--all"} for arg in args[1:])
        memory_dir = get_auto_mem_path(cwd=project_path)
        return MemoryCommandResult(
            format_daily_log_entries(
                memory_dir,
                include_consolidated=include_consolidated,
            ),
            display="system",
        )

    if action in {"path", "show"}:
        selector = " ".join(args[1:]).strip() or "user"
        try:
            target = resolve_memory_target(selector, cwd=project_path)
        except Exception as exc:  # noqa: BLE001 - command boundary
            return MemoryCommandResult(f"Error resolving memory file: {exc}", display="system")
        return MemoryCommandResult(
            get_relative_memory_path(target.path, cwd=project_path),
            display="system",
        )

    return MemoryCommandResult(
        "Usage: /memory [list] | /memory edit [user|project|local|auto|folder|<listed-path>] | /memory read [filename] | /memory logs [all]",
        display="system",
    )


def _resolve_project_path(openspace: Any | None) -> str:
    metadata = getattr(openspace, "current_session_metadata", None)
    if isinstance(metadata, dict):
        for key in ("project_path", "workspace_dir", "cwd"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return str(Path.cwd())


def _clear_memory_caches() -> None:
    try:
        from openspace.services.memory import clear_memory_file_caches
        from openspace.prompts.grounding_agent_prompts import clear_system_prompt_sections

        clear_memory_file_caches()
        clear_system_prompt_sections()
    except Exception:
        return
