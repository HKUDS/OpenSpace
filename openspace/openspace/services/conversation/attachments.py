"""Model-visible attachment messages.

OpenSpace stores attachment payloads in ``_meta.attachment`` and renders them
as provider-neutral ``role=user`` system-reminder text. This module is the
single attachment envelope/formatter used by compact, memory, skills, and the
agent loop.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from openspace.grounding.core.types import BackendType, ToolStatus
from openspace.services.runtime_support.plan_mode import (
    PLAN_MODE_ATTACHMENT_TURNS_BETWEEN_ATTACHMENTS,
    PLAN_MODE_FULL_REMINDER_EVERY_N_ATTACHMENTS,
)
TODO_REMINDER_TURNS_SINCE_WRITE = 10
TODO_REMINDER_TURNS_BETWEEN_REMINDERS = 10

POST_COMPACT_MAX_FILES_TO_RESTORE = 5
POST_COMPACT_TOKEN_BUDGET = 50_000
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000
POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000
POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000

CLAUDE_IN_CHROME_MCP_SERVER_NAME = "claude-in-chrome"
CHROME_TOOL_SEARCH_INSTRUCTIONS = """**IMPORTANT: Before using any chrome browser tools, you MUST first load them using ToolSearch.**

Chrome browser tools are MCP tools that require loading before use. Before calling any mcp__claude-in-chrome__* tool:
1. Use ToolSearch with `select:mcp__claude-in-chrome__<tool_name>` to load the specific tool
2. Then call the tool

For example, to get tab context:
1. First: ToolSearch with query "select:mcp__claude-in-chrome__tabs_context_mcp"
2. Then: Call mcp__claude-in-chrome__tabs_context_mcp"""


def create_attachment_message(attachment: Mapping[str, Any]) -> dict[str, Any]:
    """Create a provider-neutral OpenSpace attachment message."""

    safe_attachment = _json_safe_attachment(dict(attachment))
    return {
        "role": "user",
        "content": format_attachment_for_model(safe_attachment),
        "_meta": {
            "type": "attachment",
            "attachment_type": safe_attachment.get("type"),
            "attachment": safe_attachment,
            "uuid": str(uuid4()),
            "timestamp": time.time(),
        },
    }


def format_attachment_for_model(attachment: Mapping[str, Any]) -> str:
    attachment_type = attachment.get("type")

    if attachment_type == "file":
        filename = str(attachment.get("filename") or "")
        content = _content_to_text(attachment.get("content"))
        note = ""
        if attachment.get("truncated"):
            note = (
                f"\n\nNote: The file {filename} was too large and has been "
                "truncated. Use the read tool to read more of the file if needed."
            )
        return (
            "<system-reminder>\n"
            f"Contents of {filename}:\n\n{content}{note}"
            "\n</system-reminder>"
        )

    if attachment_type == "compact_file_reference":
        filename = str(attachment.get("filename") or "")
        return (
            "<system-reminder>\n"
            f"Note: {filename} was read before the last conversation was "
            "summarized, but the contents are too large to include. Use the "
            "read tool if you need to access it."
            "\n</system-reminder>"
        )

    if attachment_type == "nested_memory":
        content = attachment.get("content")
        if isinstance(content, Mapping):
            path = str(content.get("path") or attachment.get("path") or "")
            body = str(content.get("content") or "")
        else:
            path = str(attachment.get("path") or "")
            body = str(content or "")
        return (
            "<system-reminder>\n"
            f"Contents of {path}:\n\n{body}"
            "\n</system-reminder>"
        )

    if attachment_type == "relevant_memories":
        chunks: list[str] = []
        for memory in attachment.get("memories") or []:
            if not isinstance(memory, Mapping):
                continue
            header = str(memory.get("header") or memory.get("path") or "memory")
            chunks.append(f"{header}\n{memory.get('content') or ''}")
        return (
            "<system-reminder>\n"
            + ("Relevant memories:\n\n" + "\n\n---\n\n".join(chunks) if chunks else "No relevant memories.")
            + "\n</system-reminder>"
        )

    if attachment_type == "deferred_tools_delta":
        added = [str(line) for line in (attachment.get("addedLines") or [])]
        removed = [str(name) for name in (attachment.get("removedNames") or [])]
        parts: list[str] = []
        if added:
            parts.append(
                "The following deferred tools are now available via `tool_search`:\n"
                + "\n".join(added)
            )
        if removed:
            parts.append(
                "The following deferred tools are no longer available:\n"
                + "\n".join(removed)
            )
        if not parts:
            parts.append("Deferred tool availability did not change.")
        return "<system-reminder>\n" + "\n\n".join(parts) + "\n</system-reminder>"

    if attachment_type == "agent_listing_delta":
        added = [str(line) for line in (attachment.get("addedLines") or [])]
        removed = [str(name) for name in (attachment.get("removedTypes") or [])]
        parts: list[str] = []
        if added:
            heading = (
                "Available agent types for the Agent tool:"
                if attachment.get("isInitial")
                else "New agent types are now available for the Agent tool:"
            )
            parts.append(heading + "\n" + "\n".join(added))
        if removed:
            parts.append(
                "The following agent types are no longer available:\n"
                + "\n".join(f"- {name}" for name in removed)
            )
        if attachment.get("isInitial") and attachment.get("showConcurrencyNote"):
            parts.append(
                "Launch multiple agents concurrently whenever possible, to "
                "maximize performance; to do that, use a single message with "
                "multiple tool uses."
            )
        return "<system-reminder>\n" + "\n\n".join(parts) + "\n</system-reminder>"

    if attachment_type == "mcp_instructions_delta":
        added = [str(block) for block in (attachment.get("addedBlocks") or [])]
        removed = [str(name) for name in (attachment.get("removedNames") or [])]
        parts: list[str] = []
        if added:
            parts.append(
                "# MCP Server Instructions\n\n"
                "The following MCP servers have provided instructions for how "
                "to use their tools and resources:\n\n"
                + "\n\n".join(added)
            )
        if removed:
            parts.append(
                "The following MCP server instructions no longer apply:\n"
                + "\n".join(removed)
            )
        return "<system-reminder>\n" + "\n\n".join(parts) + "\n</system-reminder>"

    if attachment_type == "todo_reminder":
        todo_items = [
            f"{index + 1}. [{todo.get('status')}] {todo.get('content')}"
            for index, todo in enumerate(attachment.get("content") or [])
            if isinstance(todo, Mapping)
        ]
        message = (
            "The TodoWrite tool hasn't been used recently. If you're working "
            "on tasks that would benefit from tracking progress, consider "
            "using the TodoWrite tool to track progress. Also consider "
            "cleaning up the todo list if has become stale and no longer "
            "matches what you are working on. Only use it if it's relevant "
            "to the current work. This is just a gentle reminder - ignore if "
            "not applicable. Make sure that you NEVER mention this reminder "
            "to the user\n"
        )
        if todo_items:
            message += "\n\nHere are the existing contents of your todo list:\n\n["
            message += "\n".join(todo_items) + "]"
        return "<system-reminder>\n" + message + "\n</system-reminder>"

    if attachment_type == "task_reminder":
        task_items = [
            f"#{task.get('id')}. [{task.get('status')}] {task.get('subject')}"
            for task in (attachment.get("content") or [])
            if isinstance(task, Mapping)
        ]
        message = (
            "The task tools haven't been used recently. If you're working on "
            "tasks that would benefit from tracking progress, consider using "
            "TaskCreate to add new tasks and TaskUpdate to update task status. "
            "Only use these if relevant to the current work. This is just a "
            "gentle reminder - ignore if not applicable. Make sure that you "
            "NEVER mention this reminder to the user\n"
        )
        if task_items:
            message += "\n\nHere are the existing tasks:\n\n" + "\n".join(task_items)
        return "<system-reminder>\n" + message + "\n</system-reminder>"

    if attachment_type == "diagnostics":
        lines = ["New diagnostics were reported after recent file changes:"]
        for file in attachment.get("files") or []:
            if isinstance(file, Mapping):
                uri = str(file.get("uri") or "")
                diagnostics = file.get("diagnostics") or []
            else:
                uri = str(getattr(file, "uri", "") or "")
                diagnostics = getattr(file, "diagnostics", []) or []
            if not diagnostics:
                continue
            lines.append(f"\n{uri}:")
            for diagnostic in diagnostics:
                if isinstance(diagnostic, Mapping):
                    severity = str(diagnostic.get("severity") or "Error")
                    message = str(diagnostic.get("message") or "")
                    range_value = diagnostic.get("range") or {}
                    source = diagnostic.get("source")
                    code = diagnostic.get("code")
                else:
                    severity = str(getattr(diagnostic, "severity", "Error"))
                    message = str(getattr(diagnostic, "message", ""))
                    range_value = getattr(diagnostic, "range", {}) or {}
                    source = getattr(diagnostic, "source", None)
                    code = getattr(diagnostic, "code", None)
                start = range_value.get("start", {}) if isinstance(range_value, Mapping) else {}
                location = f"{int(start.get('line', 0)) + 1}:{int(start.get('character', 0)) + 1}"
                suffix = (f" [{code}]" if code else "") + (f" ({source})" if source else "")
                lines.append(f"- {severity} at {location}: {message}{suffix}")
        return (
            "<system-reminder>\n"
            + "\n".join(lines)
            + "\nUse the diagnostics to guide fixes when relevant. Do not mention this reminder directly."
            + "\n</system-reminder>"
        )

    if attachment_type == "skill_listing":
        content = str(attachment.get("content") or "").strip()
        if not content:
            return "<system-reminder>No new skills are available.</system-reminder>"
        heading = "Available skills:" if attachment.get("isInitial") else "Additional skills are now available:"
        return (
            "<system-reminder>\n"
            f"{heading}\n{content}\n\n"
            "If one of these skills matches the task, call the Skill tool "
            "with that skill name before applying it."
            "\n</system-reminder>"
        )

    if attachment_type == "skill_discovery":
        skills = [s for s in (attachment.get("skills") or []) if isinstance(s, Mapping)]
        if not skills:
            return "<system-reminder>No relevant skills were discovered.</system-reminder>"
        lines = ["Skills relevant to your task:"]
        for skill in skills:
            name = str(skill.get("name") or "")
            desc = str(skill.get("description") or "")
            lines.append(f"- {name}: {desc}" if desc else f"- {name}")
        lines.append("Use the Skill tool to load full instructions before applying a skill.")
        return "<system-reminder>\n" + "\n".join(lines) + "\n</system-reminder>"

    if attachment_type == "dynamic_skill":
        skill_names = [str(name) for name in (attachment.get("skillNames") or [])]
        skill_dir = str(attachment.get("displayPath") or attachment.get("skillDir") or "")
        lines = [
            f"Skills were found near the files you touched: {skill_dir}",
            *[f"- {name}" for name in skill_names],
            "Use the Skill tool to load one if it is relevant.",
        ]
        return "<system-reminder>\n" + "\n".join(lines) + "\n</system-reminder>"

    if attachment_type == "skill_state":
        return (
            "<system-reminder>"
            "Skill protocol state restored after compaction."
            "</system-reminder>"
        )

    if attachment_type == "invoked_skills":
        skills = [s for s in (attachment.get("skills") or []) if isinstance(s, Mapping)]
        if not skills:
            return ""
        skills_content = "\n\n---\n\n".join(
            f"### Skill: {skill.get('name')}\nPath: {skill.get('path')}\n\n{skill.get('content') or ''}"
            for skill in skills
        )
        return (
            "<system-reminder>\n"
            "The following skills were invoked in this session. Continue to "
            f"follow these guidelines:\n\n{skills_content}"
            "\n</system-reminder>"
        )

    if attachment_type == "invoked_skill_content":
        name = str(attachment.get("name") or "")
        content = str(attachment.get("content") or "")
        return (
            "<system-reminder>\n"
            f"Skill `{name}` has been loaded. Follow these instructions for "
            f"the current task when relevant.\n\n{content}"
            "\n</system-reminder>"
        )

    if attachment_type == "plan_file_reference":
        return (
            "<system-reminder>\n"
            f"A plan file exists from plan mode at: {attachment.get('planFilePath')}\n\n"
            f"Plan contents:\n\n{attachment.get('planContent')}\n\n"
            "If this plan is relevant to the current work and not already "
            "complete, continue working on it."
            "\n</system-reminder>"
        )

    if attachment_type == "plan_mode":
        mode = "subagent" if attachment.get("isSubAgent") else "main agent"
        plan_path = attachment.get("planFilePath")
        if attachment.get("reminderType") == "compact":
            body = (
                f"You are still in plan mode for the {mode}. Continue to avoid "
                "making changes until plan mode has been exited."
            )
        else:
            body = (
                f"You are in plan mode for the {mode}. Your task is to explore, "
                "ask clarifying questions if needed, and write an implementation "
                "plan before making changes.\n\n"
                "Rules:\n"
                "- Do NOT make code or file changes except editing the plan file.\n"
                "- Use read-only tools to inspect the codebase.\n"
                "- Write the plan to the plan file.\n"
                "- When ready for approval, use ExitPlanMode.\n"
            )
            if plan_path:
                body += f"\nPlan file: {plan_path}"
        return (
            "<system-reminder>\n"
            + body
            + "\n</system-reminder>"
        )

    if attachment_type == "plan_mode_reentry":
        return (
            "<system-reminder>\n"
            "You have re-entered plan mode. Continue exploring and updating the "
            "plan file, but do not make implementation changes until the plan is approved."
            "\n</system-reminder>"
        )

    if attachment_type == "plan_mode_exit":
        return (
            "<system-reminder>\n"
            "Plan mode has been exited. You may now implement the approved plan."
            "\n</system-reminder>"
        )

    if attachment_type == "verify_plan_reminder":
        return (
            "<system-reminder>\n"
            "Before continuing implementation, verify that your next steps still "
            "match the approved plan. If the plan is stale or wrong, re-enter "
            "plan mode instead of silently diverging."
            "\n</system-reminder>"
        )

    if attachment_type == "task_status":
        status = str(attachment.get("status") or "")
        display_status = "stopped" if status == "killed" else status
        description = str(attachment.get("description") or "")
        task_id = str(attachment.get("taskId") or "")
        if status == "killed":
            text = f'Task "{description}" ({task_id}) was stopped by the user.'
        elif status == "running":
            parts = [f'Background agent "{description}" ({task_id}) is still running.']
            if attachment.get("deltaSummary"):
                parts.append(f"Progress: {attachment.get('deltaSummary')}")
            output_path = attachment.get("outputFilePath")
            if output_path:
                parts.append(
                    "Do NOT spawn a duplicate. You will be notified when it "
                    f"completes. You can read partial output at {output_path}."
                )
            else:
                parts.append("Do NOT spawn a duplicate. You will be notified when it completes.")
            text = " ".join(parts)
        else:
            parts = [
                f"Task {task_id}",
                f"(type: {attachment.get('taskType')})",
                f"(status: {display_status})",
                f"(description: {description})",
            ]
            if attachment.get("deltaSummary"):
                parts.append(f"Delta: {attachment.get('deltaSummary')}")
            if attachment.get("outputFilePath"):
                parts.append(f"Read the output file to retrieve the result: {attachment.get('outputFilePath')}")
            text = " ".join(parts)
        return "<system-reminder>\n" + text + "\n</system-reminder>"

    return f"<system-reminder>{json.dumps(dict(attachment), ensure_ascii=False, default=str)}</system-reminder>"


def get_deferred_tools_delta_attachment(
    tools: Sequence[Any],
    model: str | None,
    existing_messages: Sequence[Mapping[str, Any]] | None = None,
    *,
    scan_context: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Provider-neutral OpenSpace ``getDeferredToolsDeltaAttachment`` equivalent."""

    del model  # OS intentionally does not gate this on Anthropic tool_reference.
    pool_names = {
        str(getattr(tool, "name", ""))
        for tool in tools
        if getattr(tool, "name", None)
    }
    current = {
        str(getattr(tool, "name", ""))
        for tool in tools
        if getattr(tool, "is_deferred", False) and getattr(tool, "name", None)
    }

    announced = _scan_announced_names(
        existing_messages or [],
        "deferred_tools_delta",
        added_key="addedNames",
        removed_key="removedNames",
    )
    added = sorted(current - announced)
    removed = sorted(
        name for name in announced
        if name not in current and name not in pool_names
    )
    if not added and not removed:
        return []

    attachment: dict[str, Any] = {
        "type": "deferred_tools_delta",
        "addedNames": added,
        "addedLines": added,
        "removedNames": removed,
    }
    if scan_context:
        attachment["scanContext"] = dict(scan_context)
    return [attachment]


def get_agent_listing_delta_attachment(
    context: Any,
    messages: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    from openspace.agents.agent_tool_utils import get_agent_listing_delta_attachment as impl

    return impl(context, messages)


def get_mcp_instructions_delta_attachment(
    mcp_clients: Iterable[Any] | None,
    tools: Sequence[Any],
    model: str | None,
    messages: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """OpenSpace ``getMcpInstructionsDeltaAttachment`` for OS MCP runtime state."""

    del model  # OS uses provider-neutral text attachments.
    sources = _collect_mcp_instruction_sources(mcp_clients, tools)
    connected_names = {source["name"] for source in sources}
    blocks_by_name = {
        source["name"]: f"## {source['name']}\n{source['instructions']}"
        for source in sources
        if source.get("instructions")
    }

    if (
        CLAUDE_IN_CHROME_MCP_SERVER_NAME in connected_names
        and _is_tool_search_tool_available(tools)
    ):
        existing = blocks_by_name.get(CLAUDE_IN_CHROME_MCP_SERVER_NAME)
        chrome_block = (
            f"## {CLAUDE_IN_CHROME_MCP_SERVER_NAME}\n"
            f"{CHROME_TOOL_SEARCH_INSTRUCTIONS}"
        )
        blocks_by_name[CLAUDE_IN_CHROME_MCP_SERVER_NAME] = (
            f"{existing}\n\n{CHROME_TOOL_SEARCH_INSTRUCTIONS}"
            if existing
            else chrome_block
        )

    announced = _scan_announced_names(
        messages or [],
        "mcp_instructions_delta",
        added_key="addedNames",
        removed_key="removedNames",
    )
    added = sorted(
        ({"name": name, "block": block} for name, block in blocks_by_name.items() if name not in announced),
        key=lambda item: item["name"],
    )
    removed = sorted(name for name in announced if name not in connected_names)
    if not added and not removed:
        return []
    return [
        {
            "type": "mcp_instructions_delta",
            "addedNames": [item["name"] for item in added],
            "addedBlocks": [item["block"] for item in added],
            "removedNames": removed,
        }
    ]


def get_todo_reminder_turn_counts(
    messages: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    last_todo_seen = False
    last_reminder_seen = False
    turns_since_write = 0
    turns_since_reminder = 0

    for message in reversed(messages):
        if message.get("role") == "assistant":
            if _is_thinking_only_message(message):
                continue
            if not last_todo_seen and _assistant_used_tool(message, {"TodoWrite", "todo_write"}):
                last_todo_seen = True
            if not last_todo_seen:
                turns_since_write += 1
            if not last_reminder_seen:
                turns_since_reminder += 1
        elif not last_reminder_seen and _attachment_type(message) == "todo_reminder":
            last_reminder_seen = True

        if last_todo_seen and last_reminder_seen:
            break

    return {
        "turnsSinceLastTodoWrite": turns_since_write,
        "turnsSinceLastReminder": turns_since_reminder,
    }


def get_todo_reminder_attachments(
    messages: Sequence[Mapping[str, Any]] | None,
    context: Any,
) -> list[dict[str, Any]]:
    try:
        from openspace.services.runtime_support.settings import get_setting

        cwd = getattr(context, "cwd", None)
        if not bool(get_setting("todoFeatureEnabled", True, cwd=cwd)):
            return []
        if not bool(get_setting("attachments.todoReminderEnabled", True, cwd=cwd)):
            return []
    except Exception:
        pass

    tools = list(getattr(context, "tools", []) or [])
    if not any(_tool_matches_name(tool, "todo_write") or _tool_matches_name(tool, "TodoWrite") for tool in tools):
        return []
    if any(
        _tool_matches_name(tool, "brief")
        or _tool_matches_name(tool, "Brief")
        or _tool_matches_name(tool, "SendUserMessage")
        for tool in tools
    ):
        return []
    if not messages:
        return []

    counts = get_todo_reminder_turn_counts(messages)
    if (
        counts["turnsSinceLastTodoWrite"] < TODO_REMINDER_TURNS_SINCE_WRITE
        or counts["turnsSinceLastReminder"] < TODO_REMINDER_TURNS_BETWEEN_REMINDERS
    ):
        return []

    try:
        from openspace.tools.todo_tool import get_todo_key

        todo_key = get_todo_key(context)
    except Exception:
        todo_key = str(getattr(context, "agent_id", None) or getattr(context, "session_id", None) or "primary")
    state = getattr(context, "todo_state", {}) or {}
    todos = list(state.get(todo_key, []) or [])
    return [
        {
            "type": "todo_reminder",
            "content": todos,
            "itemCount": len(todos),
        }
    ]


def get_task_reminder_attachments(
    messages: Sequence[Mapping[str, Any]] | None,
    context: Any,
) -> list[dict[str, Any]]:
    # OpenSpace enables this only for its write-capable task-list tools. OpenSpace's
    # current TaskGet/List tools are the async-agent registry, not todo tools.
    del messages, context
    return []


async def get_diagnostic_attachments(context: Any) -> list[dict[str, Any]]:
    tools = list(getattr(context, "tools", []) or [])
    if not any(_tool_matches_name(tool, "bash") or _tool_matches_name(tool, "Bash") for tool in tools):
        return []

    files = []
    tracker = getattr(context, "diagnostic_tracker", None)
    if tracker is not None:
        try:
            files.extend(await tracker.get_new_diagnostics())
        except Exception:
            pass

    try:
        from openspace.services.lsp.diagnostic_registry import check_for_lsp_diagnostics, clear_all_lsp_diagnostics

        for diagnostic_set in check_for_lsp_diagnostics():
            files.extend(diagnostic_set.get("files", []))  # type: ignore[arg-type]
        if files:
            clear_all_lsp_diagnostics()
    except Exception:
        pass

    if not files:
        return []
    return [{"type": "diagnostics", "files": files, "isNew": True}]


def get_turn_attachment_messages(
    context: Any,
    messages: Sequence[Mapping[str, Any]],
    *,
    model: str | None = None,
) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    attachments.extend(
        get_deferred_tools_delta_attachment(
            getattr(context, "all_tools", []) or getattr(context, "tools", []) or [],
            model or getattr(context, "model", None),
            messages,
            scan_context={"callSite": "attachments_main"},
        )
    )
    attachments.extend(get_agent_listing_delta_attachment(context, messages))
    attachments.extend(
        get_mcp_instructions_delta_attachment(
            getattr(context, "mcp_clients", None),
            getattr(context, "all_tools", []) or getattr(context, "tools", []) or [],
            model or getattr(context, "model", None),
            messages,
        )
    )
    attachments.extend(get_todo_reminder_attachments(messages, context))
    attachments.extend(get_plan_mode_attachments(context, messages))
    plan_exit = get_plan_mode_exit_attachment(context)
    if plan_exit is not None:
        attachments.append(plan_exit)
    verify_plan = get_verify_plan_reminder_attachment(context, messages)
    if verify_plan is not None:
        attachments.append(verify_plan)
    return [create_attachment_message(attachment) for attachment in attachments]


async def get_turn_attachment_messages_async(
    context: Any,
    messages: Sequence[Mapping[str, Any]],
    *,
    model: str | None = None,
) -> list[dict[str, Any]]:
    base = get_turn_attachment_messages(context, messages, model=model)
    diagnostics = await get_diagnostic_attachments(context)
    if not diagnostics:
        return base
    return base + [create_attachment_message(attachment) for attachment in diagnostics]


async def create_post_compact_attachments(
    context: Any,
    *,
    effective_model: str | None,
    messages_to_keep: Sequence[Mapping[str, Any]] | None,
    full_compact: bool,
    pre_compact_read_file_state: Mapping[str, Any] | None = None,
    pre_compact_nested_memory_source_paths: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Rebuild OpenSpace post-compact attachments in the same broad order."""

    read_state = getattr(context, "read_file_state", None)
    snapshot = dict(pre_compact_read_file_state or (read_state if isinstance(read_state, dict) else {}))
    source_paths = {
        str(path)
        for path in (pre_compact_nested_memory_source_paths or getattr(context, "nested_memory_source_paths", set()) or set())
        if str(path)
    }
    source_paths.update(str(path) for path in snapshot.keys() if str(path))

    if isinstance(read_state, dict):
        read_state.clear()
    loaded = getattr(context, "loaded_nested_memory_paths", None)
    if isinstance(loaded, set):
        loaded.clear()
    triggers = getattr(context, "nested_memory_triggers", None)
    if isinstance(triggers, set):
        triggers.clear()

    preserved = list(messages_to_keep or [])
    attachments: list[dict[str, Any]] = []
    attachments.extend(
        await create_post_compact_file_attachments(
            snapshot,
            context,
            POST_COMPACT_MAX_FILES_TO_RESTORE,
            preserved_messages=preserved,
        )
    )
    attachments.extend(await create_async_agent_attachments_if_needed(context))

    plan_attachment = create_plan_attachment_if_needed(getattr(context, "agent_id", None), context=context)
    if plan_attachment is not None:
        attachments.append(plan_attachment)

    plan_mode_attachment = create_plan_mode_attachment_if_needed(context)
    if plan_mode_attachment is not None:
        attachments.append(plan_mode_attachment)

    skill_state = create_skill_state_attachment_if_needed(context)
    if skill_state is not None:
        attachments.append(skill_state)
    invoked_skills = create_invoked_skills_attachment_if_needed(context)
    if invoked_skills is not None:
        attachments.append(invoked_skills)
    attachments.extend(create_dynamic_skill_attachments_if_needed(context, delta_history=[] if full_compact else preserved))

    delta_history = [] if full_compact else preserved
    for attachment in get_deferred_tools_delta_attachment(
        getattr(context, "all_tools", []) or getattr(context, "tools", []) or [],
        effective_model,
        delta_history,
        scan_context={"callSite": "compact_full" if full_compact else "compact_partial"},
    ):
        attachments.append(create_attachment_message(attachment))
    for attachment in get_agent_listing_delta_attachment(context, delta_history):
        attachments.append(create_attachment_message(attachment))
    for attachment in get_mcp_instructions_delta_attachment(
        getattr(context, "mcp_clients", None),
        getattr(context, "all_tools", []) or getattr(context, "tools", []) or [],
        effective_model,
        delta_history,
    ):
        attachments.append(create_attachment_message(attachment))

    for attachment in get_post_compact_nested_memory_attachments(context, source_paths):
        attachments.append(create_attachment_message(attachment))
    if isinstance(triggers, set):
        triggers.clear()
    return attachments


async def create_post_compact_file_attachments(
    read_file_state: Mapping[str, Any],
    context: Any,
    max_files: int = POST_COMPACT_MAX_FILES_TO_RESTORE,
    *,
    preserved_messages: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    preserved_read_paths = _collect_read_tool_file_paths(preserved_messages or [])
    recent_files = sorted(
        (
            (str(path), _read_state_timestamp(entry))
            for path, entry in read_file_state.items()
            if str(path)
            and not _should_exclude_from_post_compact_restore(str(path), context)
            and str(Path(str(path)).expanduser().resolve()) not in preserved_read_paths
        ),
        key=lambda item: item[1],
        reverse=True,
    )[:max_files]

    used_tokens = 0
    messages: list[dict[str, Any]] = []
    for filename, _timestamp in recent_files:
        attachment = await generate_file_attachment(filename, context, mode="compact")
        if attachment is None:
            continue
        message = create_attachment_message(attachment)
        tokens = _rough_token_count(json.dumps(message, ensure_ascii=False, default=str))
        if used_tokens + tokens > POST_COMPACT_TOKEN_BUDGET:
            continue
        used_tokens += tokens
        messages.append(message)
    return messages


async def generate_file_attachment(
    filename: str,
    context: Any,
    *,
    mode: str = "compact",
) -> dict[str, Any] | None:
    path = Path(filename).expanduser()
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.exists() or not resolved.is_file():
        return None
    if _is_read_denied(str(resolved), context):
        return None

    from openspace.grounding.backends.shell.file_tools import ReadFileTool
    from openspace.tool_runtime.pipeline.execution import (
        run_tool_use,
        tool_call_result_to_tool_result,
    )

    reader = ReadFileTool()
    reader.set_context(context)
    tool_use_id = f"compact_read_{abs(hash(str(resolved))) & 0xffffffff:x}"
    pipeline_result = await run_tool_use(
        {
            "id": tool_use_id,
            "type": "function",
            "function": {
                "name": reader.name,
                "arguments": {
                    "file_path": str(resolved),
                    "offset": 1,
                    "limit": None,
                },
            },
        },
        {reader.name: reader},
        context,
        assistant_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": tool_use_id,
                    "type": "function",
                    "function": {
                        "name": reader.name,
                        "arguments": {
                            "file_path": str(resolved),
                            "offset": 1,
                            "limit": None,
                        },
                    },
                }
            ],
            "_meta": {
                "type": "synthetic_tool_call",
                "source": "post_compact_file_attachment",
            },
        },
    )
    result = tool_call_result_to_tool_result(pipeline_result)
    if result.status != ToolStatus.SUCCESS:
        content = str(result.content or result.error or "")
        if mode == "compact" and ("exceeds" in content or "too large" in content.lower()):
            return {
                "type": "compact_file_reference",
                "filename": str(resolved),
                "displayPath": _display_path(resolved, context),
            }
        return None

    content = result.content
    tokens = _rough_token_count(_content_to_text(content))
    if mode == "compact" and tokens > POST_COMPACT_MAX_TOKENS_PER_FILE:
        _drop_read_file_state(context, str(resolved))
        return {
            "type": "compact_file_reference",
            "filename": str(resolved),
            "displayPath": _display_path(resolved, context),
        }

    return {
        "type": "file",
        "filename": str(resolved),
        "content": content,
        "metadata": dict(result.metadata or {}),
        "displayPath": _display_path(resolved, context),
    }


def create_plan_attachment_if_needed(agent_id: str | None = None, *, context: Any | None = None) -> dict[str, Any] | None:
    del agent_id
    plan_path = getattr(context, "plan_file_path", None) if context is not None else None
    if not plan_path:
        return None
    try:
        content = Path(str(plan_path)).expanduser().read_text(encoding="utf-8")
    except OSError:
        return None
    if not content.strip():
        return None
    return create_attachment_message(
        {
            "type": "plan_file_reference",
            "planFilePath": str(Path(str(plan_path)).expanduser()),
            "planContent": content,
        }
    )


def create_plan_mode_attachment_if_needed(context: Any) -> dict[str, Any] | None:
    if str(getattr(context, "permission_mode", "") or "").lower() != "plan":
        return None
    return create_attachment_message(
        {
            "type": "plan_mode",
            "reminderType": "full",
            "isSubAgent": bool(getattr(context, "agent_id", None) and getattr(context, "agent_id", None) != "primary"),
            "planFilePath": getattr(context, "plan_file_path", None),
            "planExists": bool(getattr(context, "plan_file_path", None)),
        }
    )


def get_plan_mode_attachments(
    context: Any,
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if str(getattr(context, "permission_mode", "") or "").lower() != "plan":
        return []
    existing_count = sum(
        1
        for message in messages
        if (
            isinstance(message, Mapping)
            and isinstance(message.get("_meta"), Mapping)
            and message["_meta"].get("attachment_type")
            in {"plan_mode", "plan_mode_reentry"}
        )
    )
    turns_since = _turns_since_attachment(messages, {"plan_mode", "plan_mode_reentry"})
    if existing_count and turns_since < PLAN_MODE_ATTACHMENT_TURNS_BETWEEN_ATTACHMENTS:
        return []
    reminder_type = (
        "full"
        if existing_count % PLAN_MODE_FULL_REMINDER_EVERY_N_ATTACHMENTS == 0
        else "compact"
    )
    return [
        {
            "type": "plan_mode",
            "reminderType": reminder_type,
            "isSubAgent": bool(
                getattr(context, "agent_id", None)
                and getattr(context, "agent_id", None) != "primary"
            ),
            "planFilePath": getattr(context, "plan_file_path", None),
            "planExists": bool(getattr(context, "plan_file_path", None)),
        }
    ]


def get_plan_mode_exit_attachment(context: Any) -> dict[str, Any] | None:
    if not bool(getattr(context, "plan_mode_exit_pending", False)):
        return None
    setattr(context, "plan_mode_exit_pending", False)
    return {"type": "plan_mode_exit"}


def get_verify_plan_reminder_attachment(
    context: Any,
    messages: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if str(getattr(context, "permission_mode", "") or "").lower() == "plan":
        return None
    if not bool(getattr(context, "plan_mode_exited_in_session", False)):
        return None
    if _turns_since_attachment(messages, {"plan_mode_exit", "verify_plan_reminder"}) < 10:
        return None
    return {"type": "verify_plan_reminder"}


def _turns_since_attachment(
    messages: Sequence[Mapping[str, Any]],
    attachment_types: set[str],
) -> int:
    turns = 0
    for message in reversed(messages):
        if (
            isinstance(message, Mapping)
            and isinstance(message.get("_meta"), Mapping)
            and message["_meta"].get("attachment_type") in attachment_types
        ):
            return turns
        if isinstance(message, Mapping) and message.get("role") == "assistant":
            turns += 1
    return 10**9


def create_invoked_skills_attachment_if_needed(context: Any) -> dict[str, Any] | None:
    records_by_agent = getattr(context, "invoked_skills_by_agent", None)
    if not isinstance(records_by_agent, Mapping):
        return None

    current_agent_id = str(getattr(context, "agent_id", None) or "primary")
    records = list(records_by_agent.get(current_agent_id, ()) or ())
    if not records:
        return None

    max_chars_per_skill = POST_COMPACT_MAX_TOKENS_PER_SKILL * 4
    total_budget_chars = POST_COMPACT_SKILLS_TOKEN_BUDGET * 4
    used_chars = 0
    skills: list[dict[str, Any]] = []
    records.sort(key=lambda record: getattr(record, "invoked_at", 0.0), reverse=True)
    for record in records:
        content = str(getattr(record, "content", "") or "")
        if not content:
            continue
        truncated = False
        if len(content) > max_chars_per_skill:
            content = (
                content[:max_chars_per_skill]
                + "\n\n[Skill content truncated during compaction.]"
            )
            truncated = True
        if used_chars + len(content) > total_budget_chars:
            continue
        used_chars += len(content)
        item: dict[str, Any] = {
            "skill_id": str(getattr(record, "skill_id", "") or ""),
            "name": str(getattr(record, "name", "") or ""),
            "path": str(getattr(record, "path", "") or ""),
            "content": content,
            "agent_id": current_agent_id,
            "allowed_tools": [
                str(tool)
                for tool in (getattr(record, "allowed_tools", None) or [])
                if str(tool).strip()
            ],
            "model": getattr(record, "model", None),
            "effort": getattr(record, "effort", None),
            "execution_context": str(getattr(record, "execution_context", None) or "inline"),
        }
        if truncated:
            item["truncated"] = "true"
        skills.append(item)

    if not skills:
        return None
    return create_attachment_message({"type": "invoked_skills", "skills": skills})


def create_skill_state_attachment_if_needed(context: Any) -> dict[str, Any] | None:
    sent_by_agent = {
        str(agent): sorted(str(name) for name in names if str(name).strip())
        for agent, names in (getattr(context, "sent_skill_names_by_agent", {}) or {}).items()
        if names
    }
    discovered = sorted(
        str(name)
        for name in (getattr(context, "discovered_skill_names", set()) or set())
        if str(name).strip()
    )
    sent_dynamic = sorted(
        str(key)
        for key in (getattr(context, "sent_dynamic_skill_keys", set()) or set())
        if str(key).strip()
    )
    path_activated = sorted(
        str(name)
        for name in (getattr(context, "path_activated_skill_names", set()) or set())
        if str(name).strip()
    )
    if not any([sent_by_agent, discovered, sent_dynamic, path_activated]):
        return None
    return create_attachment_message(
        {
            "type": "skill_state",
            "sentSkillNamesByAgent": sent_by_agent,
            "discoveredSkillNames": discovered,
            "sentDynamicSkillKeys": sent_dynamic,
            "pathActivatedSkillNames": path_activated,
        }
    )


def create_dynamic_skill_attachments_if_needed(
    context: Any,
    delta_history: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Re-emit model-visible dynamic skill notices after compaction."""

    already_sent = _scan_dynamic_skill_keys(delta_history or [])
    messages: list[dict[str, Any]] = []
    for key in sorted(str(k) for k in (getattr(context, "sent_dynamic_skill_keys", set()) or set()) if str(k).strip()):
        if key in already_sent:
            continue
        skill_dir, names = _parse_dynamic_skill_key(key)
        if not skill_dir or not names:
            continue
        messages.append(
            create_attachment_message(
                {
                    "type": "dynamic_skill",
                    "skillDir": skill_dir,
                    "displayPath": skill_dir,
                    "skillNames": names,
                }
            )
        )
    return messages


async def create_async_agent_attachments_if_needed(context: Any) -> list[dict[str, Any]]:
    task_manager = getattr(context, "task_manager", None)
    if task_manager is None or not hasattr(task_manager, "list_all"):
        return []
    try:
        tasks = list(task_manager.list_all())
    except Exception:
        return []

    messages: list[dict[str, Any]] = []
    current_agent_id = str(getattr(context, "agent_id", "") or "")
    for task in tasks:
        if getattr(task, "retrieved", False):
            continue
        if str(getattr(task, "status", "")) == "pending":
            continue
        if str(getattr(task, "agent_id", "") or getattr(task, "id", "")) == current_agent_id:
            continue
        task_type = str(getattr(getattr(task, "type", None), "value", getattr(task, "type", "")))
        if task_type not in {"local_agent", "in_process_teammate", "coordinator_worker"}:
            continue
        progress = getattr(task, "progress", None)
        status = str(getattr(getattr(task, "status", None), "value", getattr(task, "status", "")))
        delta = getattr(progress, "summary", None) if status == "running" else (getattr(task, "error", None) or _extract_task_result_text(getattr(task, "result", None)))
        messages.append(
            create_attachment_message(
                {
                    "type": "task_status",
                    "taskId": str(getattr(task, "id", "")),
                    "taskType": task_type,
                    "description": str(getattr(task, "description", "") or ""),
                    "status": status,
                    "deltaSummary": delta,
                    "outputFilePath": str(getattr(task, "output_file", "") or ""),
                }
            )
        )
    return messages


def get_post_compact_nested_memory_attachments(
    context: Any,
    source_paths: Iterable[str],
) -> list[dict[str, Any]]:
    from openspace.services.memory.openspace_md import (
        get_nested_memory_attachments_for_file,
        is_memory_file_path,
    )

    attachments: list[dict[str, Any]] = []
    loaded = getattr(context, "loaded_nested_memory_paths", None)
    if isinstance(loaded, set):
        loaded.clear()
    for path in sorted({str(path) for path in source_paths if str(path)}):
        if is_memory_file_path(path):
            continue
        attachments.extend(
            get_nested_memory_attachments_for_file(
                path,
                context,
                ignore_read_file_state=True,
            )
        )
    return attachments


def _scan_announced_names(
    messages: Sequence[Mapping[str, Any]],
    attachment_type: str,
    *,
    added_key: str,
    removed_key: str,
) -> set[str]:
    announced: set[str] = set()
    for message in messages:
        attachment = _attachment_payload(message)
        if not isinstance(attachment, Mapping) or attachment.get("type") != attachment_type:
            continue
        added = attachment.get(added_key)
        if isinstance(added, Sequence) and not isinstance(added, (str, bytes, bytearray)):
            announced.update(str(name) for name in added if name)
        removed = attachment.get(removed_key)
        if isinstance(removed, Sequence) and not isinstance(removed, (str, bytes, bytearray)):
            announced.difference_update(str(name) for name in removed if name)
    return announced


def _attachment_payload(message: Mapping[str, Any]) -> Mapping[str, Any] | None:
    meta = message.get("_meta")
    if isinstance(meta, Mapping):
        attachment = meta.get("attachment")
        if isinstance(attachment, Mapping):
            return attachment
    attachment = message.get("attachment")
    if isinstance(attachment, Mapping):
        return attachment
    return None


def _attachment_type(message: Mapping[str, Any]) -> str | None:
    attachment = _attachment_payload(message)
    return str(attachment.get("type")) if isinstance(attachment, Mapping) else None


def _collect_mcp_instruction_sources(
    mcp_clients: Iterable[Any] | None,
    tools: Sequence[Any],
) -> list[dict[str, str]]:
    sources: dict[str, str] = {}
    for client in mcp_clients or ():
        name = _extract_mcp_name(client)
        instructions = _extract_mcp_instructions(client)
        if name:
            sources[name] = instructions

    for tool in tools:
        runtime = getattr(tool, "runtime_info", None)
        if runtime is None or getattr(runtime, "backend", None) != BackendType.MCP:
            continue
        name = str(getattr(runtime, "server_name", "") or "")
        if not name:
            continue
        instructions = _extract_mcp_instructions_from_runtime(runtime)
        if instructions:
            sources.setdefault(name, instructions)
        else:
            sources.setdefault(name, "")

    return [{"name": name, "instructions": instructions} for name, instructions in sorted(sources.items())]


def _extract_mcp_name(value: Any) -> str:
    if isinstance(value, Mapping):
        raw = value.get("name") or value.get("serverName") or value.get("server_name") or value.get("session_name") or value.get("sessionId") or value.get("session_id") or ""
    else:
        raw = (
            getattr(value, "name", None)
            or getattr(value, "server_name", None)
            or getattr(value, "serverName", None)
            or getattr(value, "session_name", None)
            or getattr(value, "session_id", None)
            or ""
        )
    name = str(raw)
    if name.startswith("mcp-") and len(name) > 4:
        return name[4:]
    return name


def _extract_mcp_instructions(value: Any) -> str:
    if isinstance(value, Mapping):
        raw = value.get("instructions")
        return str(raw).strip() if raw else ""
    raw = getattr(value, "instructions", None)
    if raw:
        return str(raw).strip()
    return _extract_instructions_from_session_info(getattr(value, "session_info", None))


def _extract_mcp_instructions_from_runtime(runtime: Any) -> str:
    client = getattr(runtime, "grounding_client", None)
    session_name = getattr(runtime, "session_name", None)
    if client is None or not session_name:
        return ""
    try:
        session = getattr(client, "_sessions", {}).get(session_name)
    except Exception:
        session = None
    if session is None:
        return ""
    return _extract_instructions_from_session_info(getattr(session, "session_info", None))


def _extract_instructions_from_session_info(session_info: Any) -> str:
    if isinstance(session_info, Mapping):
        raw = session_info.get("instructions")
        return str(raw).strip() if raw else ""
    raw = getattr(session_info, "instructions", None)
    return str(raw).strip() if raw else ""


def _is_tool_search_tool_available(tools: Sequence[Any]) -> bool:
    return any(_tool_matches_name(tool, "tool_search") or _tool_matches_name(tool, "ToolSearch") for tool in tools)


def _tool_matches_name(tool: Any, name: str) -> bool:
    if str(getattr(tool, "name", "") or "") == name:
        return True
    return name in {str(alias) for alias in (getattr(tool, "aliases", None) or [])}


def _assistant_used_tool(message: Mapping[str, Any], names: set[str]) -> bool:
    for call in message.get("tool_calls") or []:
        if not isinstance(call, Mapping):
            continue
        fn = call.get("function")
        name = fn.get("name") if isinstance(fn, Mapping) else call.get("name")
        if str(name) in names:
            return True
    content = message.get("content")
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_use" and str(block.get("name")) in names:
                return True
    return False


def _is_thinking_only_message(message: Mapping[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
        return False
    blocks = [block for block in content if isinstance(block, Mapping)]
    return bool(blocks) and all(str(block.get("type")) == "thinking" for block in blocks)


def _collect_read_tool_file_paths(messages: Sequence[Mapping[str, Any]]) -> set[str]:
    stub_ids: set[str] = set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = _content_to_text(message.get("content"))
        if content.startswith("File unchanged since last read"):
            tool_call_id = message.get("tool_call_id")
            if tool_call_id:
                stub_ids.add(str(tool_call_id))

    paths: set[str] = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_use_id, tool_name, tool_input in _iter_assistant_tool_uses(message):
            if tool_use_id in stub_ids or tool_name not in {"read", "Read", "FileRead"}:
                continue
            raw_path = tool_input.get("file_path") or tool_input.get("path")
            if not raw_path:
                continue
            try:
                paths.add(str(Path(str(raw_path)).expanduser().resolve()))
            except OSError:
                paths.add(str(raw_path))
    return paths


def _iter_assistant_tool_uses(message: Mapping[str, Any]) -> Iterable[tuple[str, str, Mapping[str, Any]]]:
    for call in message.get("tool_calls") or []:
        if not isinstance(call, Mapping):
            continue
        tool_use_id = str(call.get("id") or "")
        fn = call.get("function")
        if isinstance(fn, Mapping):
            name = str(fn.get("name") or "")
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
            elif isinstance(raw_args, Mapping):
                args = raw_args
            else:
                args = {}
            yield tool_use_id, name, args
    content = message.get("content")
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                continue
            raw_input = block.get("input")
            yield str(block.get("id") or ""), str(block.get("name") or ""), raw_input if isinstance(raw_input, Mapping) else {}


def _should_exclude_from_post_compact_restore(filename: str, context: Any) -> bool:
    try:
        from openspace.services.memory.openspace_md import is_memory_file_path

        if is_memory_file_path(filename):
            return True
    except Exception:
        pass
    plan_path = getattr(context, "plan_file_path", None)
    if plan_path:
        try:
            return Path(filename).expanduser().resolve() == Path(str(plan_path)).expanduser().resolve()
        except OSError:
            return False
    return False


def _read_state_timestamp(entry: Any) -> float:
    if isinstance(entry, Mapping):
        raw = entry.get("timestamp", 0)
    else:
        raw = getattr(entry, "timestamp", 0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _is_read_denied(filename: str, context: Any) -> bool:
    permission_context = getattr(context, "permission_context", None)
    if permission_context is None:
        return True
    try:
        from openspace.grounding.core.permissions import check_read_permission_for_tool

        result = check_read_permission_for_tool("read", filename, permission_context)
        return str(getattr(result, "behavior", "")) == "deny"
    except Exception:
        return True


def _drop_read_file_state(context: Any, filename: str) -> None:
    read_state = getattr(context, "read_file_state", None)
    if not isinstance(read_state, dict):
        return
    read_state.pop(filename, None)
    try:
        read_state.pop(str(Path(filename).expanduser().resolve()), None)
    except OSError:
        pass


def _display_path(path: Path, context: Any) -> str:
    try:
        cwd = Path(str(getattr(context, "cwd", os.getcwd()))).expanduser().resolve()
        return str(path.relative_to(cwd))
    except Exception:
        return str(path)


def _rough_token_count(text: str) -> int:
    return max(1, len(text) // 4)


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif block.get("type") in {"image", "image_url"}:
                    parts.append("[image]")
                elif block.get("type") == "document":
                    parts.append("[document]")
                else:
                    parts.append(json.dumps(dict(block), ensure_ascii=False, default=str))
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)
    return json.dumps(content, ensure_ascii=False, default=str)


def _json_safe_attachment(attachment: Mapping[str, Any]) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(k): convert(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [convert(v) for v in value]
        if isinstance(value, Path):
            return str(value)
        return value

    return convert(dict(attachment))


def _scan_dynamic_skill_keys(messages: Sequence[Mapping[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for message in messages:
        attachment = _attachment_payload(message)
        if not isinstance(attachment, Mapping):
            continue
        if attachment.get("type") != "dynamic_skill":
            continue
        skill_dir = str(attachment.get("skillDir") or attachment.get("displayPath") or "")
        names = sorted(str(name) for name in (attachment.get("skillNames") or []) if str(name).strip())
        if skill_dir and names:
            keys.add(f"{skill_dir}:{','.join(names)}")
    return keys


def _parse_dynamic_skill_key(key: str) -> tuple[str, list[str]]:
    if ":" not in key:
        return "", []
    skill_dir, raw_names = key.rsplit(":", 1)
    names = sorted(name.strip() for name in raw_names.split(",") if name.strip())
    return skill_dir, names


def _extract_task_result_text(result: Any) -> str | None:
    if isinstance(result, Mapping):
        for key in ("result", "content", "summary", "output"):
            value = result.get(key)
            if value:
                return _content_to_text(value)
    return None


__all__ = [
    "create_attachment_message",
    "format_attachment_for_model",
    "get_deferred_tools_delta_attachment",
    "get_agent_listing_delta_attachment",
    "get_mcp_instructions_delta_attachment",
    "get_todo_reminder_turn_counts",
    "get_todo_reminder_attachments",
    "get_diagnostic_attachments",
    "get_task_reminder_attachments",
    "get_turn_attachment_messages",
    "get_turn_attachment_messages_async",
    "create_post_compact_attachments",
    "create_post_compact_file_attachments",
    "generate_file_attachment",
    "create_plan_attachment_if_needed",
    "create_plan_mode_attachment_if_needed",
    "get_plan_mode_attachments",
    "get_plan_mode_exit_attachment",
    "get_verify_plan_reminder_attachment",
    "create_invoked_skills_attachment_if_needed",
    "create_skill_state_attachment_if_needed",
    "create_dynamic_skill_attachments_if_needed",
    "create_async_agent_attachments_if_needed",
    "get_post_compact_nested_memory_attachments",
]
