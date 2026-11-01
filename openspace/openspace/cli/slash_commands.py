from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from openspace.grounding.core.permissions import (
    build_permission_rules_snapshot,
    set_session_permission_mode,
)
from openspace.protocol import CoreToTuiEvent
from openspace.llm.effort import (
    EFFORT_LEVELS,
    get_displayed_effort_level,
    get_effort_env_state,
    get_effort_level_description,
    get_effort_value_description,
    is_effort_level,
)
from openspace.protocol.slash_commands import get_core_slash_commands


@dataclass(slots=True)
class SlashCommandSpec:
    name: str
    summary: str
    usage: str
    aliases: tuple[str, ...] = ()


@dataclass(slots=True)
class SlashCommandOutcome:
    session_id: str | None = None


@dataclass(slots=True)
class SlashCommandContext:
    openspace: object
    bridge: object
    handle_resume: Callable[[dict], Awaitable[str | None]]
    handle_settings_update: Callable[[dict], Awaitable[None]]
    send_notification: Callable[[str, str, str], Awaitable[None]]
    send_command_result: Callable[..., Awaitable[None]]
    sync_status: Callable[[dict | None], Awaitable[None]]


COMMAND_SPECS: tuple[SlashCommandSpec, ...] = tuple(
    SlashCommandSpec(
        command.name,
        command.summary,
        command.usage,
        aliases=command.aliases,
    )
    for command in get_core_slash_commands()
)

def get_core_command_specs() -> tuple[SlashCommandSpec, ...]:
    return COMMAND_SPECS


def _format_effort_status(openspace: Any) -> str:
    getter = getattr(openspace, "get_reasoning_effort", None)
    value = getter() if callable(getter) else None
    model = str(getattr(getattr(openspace, "config", None), "llm_model", "") or "")
    env_state, env_value, _ = get_effort_env_state()
    effective_value = None if env_state == "auto" else (env_value or value)
    if effective_value is not None:
        description = get_effort_value_description(effective_value)
        display_value = getattr(effective_value, "value", effective_value)
        return f"Current effort level: {display_value} ({description})"
    level = get_displayed_effort_level(model, value)
    return f"Effort level: auto (currently {level.value})"


def _format_effort_help() -> str:
    lines = [
        "Usage: /effort [low|medium|high|max|auto]",
        "",
        "Effort levels:",
    ]
    lines.extend(
        f"- {name}: {get_effort_level_description(name)}"
        for name in EFFORT_LEVELS
    )
    lines.append("- auto: use the model default")
    return "\n".join(lines)


def _format_summary_command_result(result: Any) -> str:
    if isinstance(result, dict):
        message = result.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        status = str(result.get("status") or "").strip()
        memory_path = str(result.get("memory_path") or "").strip()
        if status == "completed" and memory_path:
            return f"Session memory updated at {memory_path}"
        if status:
            return f"Summary {status}."
    return str(result)


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in (None, "text", "status"):
                text = block.get("text") or block.get("message") or ""
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
                continue
            if block_type == "field":
                label = block.get("label") or ""
                value = block.get("value") or ""
                rendered = f"{label}: {value}".strip(": ")
                if rendered:
                    parts.append(rendered)
                continue
            if block_type == "tool_use":
                summary = block.get("summary") or block.get("result") or block.get("error")
                if isinstance(summary, str) and summary.strip():
                    parts.append(summary.strip())
        return "\n".join(part for part in parts if part).strip()
    text = message.get("text")
    return text.strip() if isinstance(text, str) else ""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _resolve_project_path(openspace: Any) -> str:
    metadata = getattr(openspace, "current_session_metadata", None)
    if isinstance(metadata, dict):
        project_path = metadata.get("project_path")
        if isinstance(project_path, str) and project_path.strip():
            return project_path
    return os.getcwd()


async def _load_active_session(openspace: Any) -> tuple[str | None, dict[str, Any] | None]:
    session_id = getattr(openspace, "current_session_id", None)
    if not isinstance(session_id, str) or not session_id:
        return None, None
    restored = await openspace.load_session_snapshot(session_id)
    return session_id, restored


def _manual_compact_messages(
    messages: list[dict[str, Any]],
    instructions: str | None,
    *,
    keep_head: int = 4,
    keep_tail: int = 8,
) -> list[dict[str, Any]]:
    if len(messages) <= keep_head + keep_tail + 1:
        return list(messages)

    head = list(messages[:keep_head])
    tail = list(messages[-keep_tail:])
    middle = messages[keep_head:-keep_tail]
    omitted_count = len(middle)

    previews: list[str] = []
    for message in middle:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        text = _message_text(message)
        if text:
            previews.append(_truncate(text.replace("\n", " "), 120))
        if len(previews) >= 3:
            break

    summary_lines = [
        "Manual compact summary.",
        f"Omitted {omitted_count} middle messages from the active transcript.",
    ]
    if instructions:
        summary_lines.append(f"Instructions: {instructions}")
    if previews:
        summary_lines.append("Representative user prompts:")
        summary_lines.extend(f"- {preview}" for preview in previews)

    head.append(
        {
            "role": "system",
            "content": "\n".join(summary_lines),
            "meta": {
                "compacted": True,
                "omitted_count": omitted_count,
            },
        }
    )
    head.extend(tail)
    invoked_attachment = _invoked_skills_attachment_from_messages(messages)
    if invoked_attachment is not None and invoked_attachment not in head:
        head.append(invoked_attachment)
    return head


def _invoked_skills_attachment_from_messages(
    messages: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Retain loaded skill bodies when manual /compact truncates the middle."""

    skills: dict[str, dict[str, Any]] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        meta = message.get("_meta")
        if not isinstance(meta, dict):
            continue
        attachment = meta.get("attachment")
        if not isinstance(attachment, dict):
            continue

        if attachment.get("type") == "invoked_skill_content":
            skill_id = str(attachment.get("skill_id") or attachment.get("name") or "").strip()
            if not skill_id:
                continue
            skills[skill_id] = _copy_invoked_skill_attachment_fields(attachment)
            continue

        if attachment.get("type") == "invoked_skills":
            for item in attachment.get("skills") or []:
                if not isinstance(item, dict):
                    continue
                skill_id = str(item.get("skill_id") or item.get("name") or "").strip()
                content = str(item.get("content") or "")
                if not skill_id or not content:
                    continue
                skills[skill_id] = _copy_invoked_skill_attachment_fields(item)

    retained = [skill for skill in skills.values() if skill.get("content")]
    if not retained:
        return None

    from openspace.services.conversation.compact import create_attachment_message

    return create_attachment_message({"type": "invoked_skills", "skills": retained})


def _copy_invoked_skill_attachment_fields(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "skill_id": str(item.get("skill_id") or item.get("skillId") or ""),
        "name": str(item.get("name") or ""),
        "path": str(item.get("path") or ""),
        "content": str(item.get("content") or ""),
        "agent_id": str(item.get("agent_id") or item.get("agentId") or ""),
        "allowed_tools": [
            str(tool)
            for tool in item.get("allowed_tools", item.get("allowedTools", []))
            if str(tool).strip()
        ],
        "model": str(item.get("model")).strip() if item.get("model") else None,
        "effort": str(item.get("effort")).strip() if item.get("effort") else None,
        "execution_context": str(
            item.get("execution_context") or item.get("executionContext") or "inline"
        ),
    }


def _format_tools_output(tools: list[Any], filter_text: str | None = None) -> str:
    rows: list[str] = []
    query = filter_text.lower().strip() if filter_text else ""

    for tool in tools:
        schema = getattr(tool, "schema", None)
        runtime_info = getattr(tool, "runtime_info", None)
        backend = getattr(getattr(runtime_info, "backend", None), "value", None)
        server_name = getattr(runtime_info, "server_name", None)
        name = getattr(schema, "name", getattr(tool, "name", "unknown"))
        description = getattr(schema, "description", "")
        haystack = " ".join(
            part for part in (str(name), str(description), str(backend), str(server_name)) if part
        ).lower()
        if query and query not in haystack:
            continue
        source = str(backend or "unknown")
        if server_name:
            source = f"{source}@{server_name}"
        rows.append(f"- {name} [{source}] {description}".rstrip())

    if not rows:
        return "No tools matched the current filter."
    return "\n".join(rows)


def _format_skills_output(skills: list[Any], filter_text: str | None = None) -> str:
    rows: list[str] = []
    query = filter_text.lower().strip() if filter_text else ""

    for skill in skills:
        name = getattr(skill, "name", "unknown")
        skill_id = getattr(skill, "skill_id", "")
        description = getattr(skill, "description", "")
        category = getattr(getattr(skill, "category", None), "value", getattr(skill, "category", ""))
        haystack = " ".join(str(part) for part in (name, skill_id, description, category)).lower()
        if query and query not in haystack:
            continue
        detail = f" ({category})" if category else ""
        rows.append(f"- {name}{detail} — {skill_id}: {description}".rstrip(": "))

    if not rows:
        return "No skills matched the current filter."
    return "\n".join(rows)


_HIGH_RISK_SKILL_OVERLAY_FIELDS = {
    "allowed-tools",
    "disable-model-invocation",
    "user-invocable",
    "model",
    "effort",
    "hooks",
    "context",
    "agent",
    "shell",
}


def _format_skill_overlay_output(
    rows: list[dict[str, Any]],
    skill_id_filter: str | None = None,
) -> str:
    if skill_id_filter:
        rows = [
            row for row in rows
            if str(row.get("skill_id") or "") == skill_id_filter
        ]
        if not rows:
            return f"No runtime overlay found for {skill_id_filter}."

    pending = [
        row for row in rows
        if isinstance(row.get("suggested"), dict) and row["suggested"]
    ]
    approved_rows = [
        row for row in rows
        if isinstance(row.get("approved"), dict) and row["approved"]
    ]
    if not pending and not (skill_id_filter and approved_rows):
        return "No suggested runtime overlays are pending approval."

    lines = ["Runtime overlay review:"]
    for row in pending:
        skill_id = str(row.get("skill_id") or "")
        lines.append(f"- {skill_id}")
        path = str(row.get("path") or "")
        if path:
            lines.append(f"  file: {path}")
        suggested = row.get("suggested") if isinstance(row.get("suggested"), dict) else {}
        suggested_meta = (
            row.get("suggested_meta")
            if isinstance(row.get("suggested_meta"), dict)
            else {}
        )
        for field in sorted(suggested):
            meta = suggested_meta.get(field) if isinstance(suggested_meta, dict) else {}
            risk = _overlay_field_risk(field, meta)
            lines.append(
                f"  - {field} [{risk}]: {_format_overlay_value(suggested[field])}"
            )
            rationale = meta.get("rationale") if isinstance(meta, dict) else None
            if isinstance(rationale, str) and rationale.strip():
                lines.append(f"    rationale: {_truncate(rationale.strip(), 180)}")
            source = meta.get("source") if isinstance(meta, dict) else None
            if isinstance(source, str) and source.strip():
                lines.append(f"    source: {source.strip()}")
            lines.append(f"    approve: /skills overlay approve {skill_id} {field}")
            lines.append(f"    reject: /skills overlay reject {skill_id} {field}")

    if skill_id_filter and approved_rows:
        for row in approved_rows:
            approved = row.get("approved") if isinstance(row.get("approved"), dict) else {}
            if not approved:
                continue
            lines.append("")
            lines.append(f"Approved runtime overlay fields for {skill_id_filter}:")
            approved_meta = (
                row.get("approved_meta")
                if isinstance(row.get("approved_meta"), dict)
                else {}
            )
            for field in sorted(approved):
                meta = approved_meta.get(field) if isinstance(approved_meta, dict) else {}
                risk = _overlay_field_risk(field, meta)
                lines.append(
                    f"- {field} [{risk}]: {_format_overlay_value(approved[field])}"
                )
    return "\n".join(lines)


def _overlay_field_risk(field: str, meta: Any) -> str:
    if isinstance(meta, dict):
        raw = str(meta.get("risk") or "").strip().lower()
        if raw in {"high", "low"}:
            return raw.upper()
    return "HIGH" if field in _HIGH_RISK_SKILL_OVERLAY_FIELDS else "LOW"


def _format_overlay_value(value: Any, *, limit: int = 240) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = " ".join(text.split())
    return _truncate(text, limit)


def _format_permissions_output(openspace: Any) -> str:
    cwd = getattr(getattr(openspace, "config", None), "workspace_dir", None) or os.getcwd()
    try:
        rules = build_permission_rules_snapshot(str(cwd))
    except Exception as exc:
        return f"Permission settings are unavailable: {exc}"
    mode = rules.get("mode", {}).get("current", "default")
    session_rules = rules.get("session", {})
    persistent_rules = rules.get("persistent", {})

    lines = [f"Mode: {mode}"]
    lines.append("")
    lines.append("Session rules:")
    if session_rules:
        lines.extend(f"- {pattern}: {decision}" for pattern, decision in sorted(session_rules.items()))
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Persistent rules:")
    if persistent_rules:
        lines.extend(f"- {pattern}: {decision}" for pattern, decision in sorted(persistent_rules.items()))
    else:
        lines.append("- none")

    return "\n".join(lines)


def _sandbox_usage() -> str:
    return (
        "Usage: /sandbox [status|doctor|enable [auto-allow|regular]|disable|"
        "toggle|exclude <pattern>|unexclude <pattern>|auto-allow <on|off>|"
        "allow-unsandboxed <on|off>]"
    )


def _strip_wrapping_quotes(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


def _parse_on_off(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"on", "true", "yes", "1", "enable", "enabled"}:
        return True
    if normalized in {"off", "false", "no", "0", "disable", "disabled"}:
        return False
    return None


async def _send_sandbox_status_update(
    context: SlashCommandContext,
    payload: dict[str, Any],
) -> None:
    await context.sync_status({"sandbox": payload})


async def _execute_sandbox_command(
    context: SlashCommandContext,
    args: list[str],
) -> SlashCommandOutcome:
    from openspace.services.runtime_support.settings import get_settings_path_for_source
    from openspace.services.sandbox import (
        add_to_excluded_commands,
        build_sandbox_status,
        format_sandbox_doctor,
        format_sandbox_status,
        get_process_sandbox_manager,
        remove_from_excluded_commands,
    )

    cwd = _resolve_project_path(context.openspace)
    manager = get_process_sandbox_manager(cwd=cwd)
    subcommand = (args[0].lower() if args else "status")
    local_settings_path = get_settings_path_for_source("localSettings", cwd)

    async def send_current(message: str | None = None) -> SlashCommandOutcome:
        payload = build_sandbox_status(manager)
        if message:
            rendered = f"{message}\n\n{format_sandbox_status(payload)}"
        else:
            rendered = format_sandbox_status(payload)
        await _send_sandbox_status_update(context, payload)
        await context.send_command_result("sandbox", rendered)
        return SlashCommandOutcome()

    if subcommand in {"status", "show", "config"}:
        return await send_current()

    if subcommand == "doctor":
        payload = build_sandbox_status(manager)
        await _send_sandbox_status_update(context, payload)
        await context.send_command_result("sandbox", format_sandbox_doctor(payload))
        return SlashCommandOutcome()

    if subcommand in {"help", "-h", "--help"}:
        await context.send_command_result("sandbox", _sandbox_usage())
        return SlashCommandOutcome()

    if manager.are_settings_locked_by_policy():
        await context.send_command_result(
            "sandbox",
            "Sandbox settings are locked by a higher-priority settings source.",
        )
        return SlashCommandOutcome()

    if subcommand == "toggle":
        current = manager.is_enabled_in_settings()
        manager.set_sandbox_settings(
            enabled=not current,
            auto_allow_bash_if_sandboxed=(
                manager.is_auto_allow_bash_if_sandboxed_enabled()
                if not current
                else False
            ),
        )
        target = "enabled" if not current else "disabled"
        return await send_current(f"Sandbox {target} in local settings.")

    if subcommand == "enable":
        mode = (args[1].lower() if len(args) > 1 else "").strip()
        auto_allow = manager.is_auto_allow_bash_if_sandboxed_enabled()
        if mode in {"auto", "auto-allow", "auto_allow"}:
            auto_allow = True
        elif mode in {"regular", "permission", "permissions"}:
            auto_allow = False
        elif mode:
            await context.send_command_result(
                "sandbox",
                "Invalid sandbox enable mode. Use: /sandbox enable [auto-allow|regular]",
            )
            return SlashCommandOutcome()
        manager.set_sandbox_settings(
            enabled=True,
            auto_allow_bash_if_sandboxed=auto_allow,
        )
        return await send_current("Sandbox enabled in local settings.")

    if subcommand == "disable":
        manager.set_sandbox_settings(
            enabled=False,
            auto_allow_bash_if_sandboxed=False,
        )
        return await send_current("Sandbox disabled in local settings.")

    if subcommand in {"auto-allow", "auto_allow"}:
        value = _parse_on_off(args[1] if len(args) > 1 else None)
        if value is None:
            await context.send_command_result(
                "sandbox",
                "Usage: /sandbox auto-allow <on|off>",
            )
            return SlashCommandOutcome()
        manager.set_sandbox_settings(auto_allow_bash_if_sandboxed=value)
        return await send_current(
            f"autoAllowBashIfSandboxed set to {'true' if value else 'false'}."
        )

    if subcommand in {"allow-unsandboxed", "allow_unsandboxed", "overrides"}:
        raw_value = args[1] if len(args) > 1 else None
        if raw_value in {"open", "opened"}:
            value = True
        elif raw_value in {"closed", "strict"}:
            value = False
        else:
            value = _parse_on_off(raw_value)
        if value is None:
            await context.send_command_result(
                "sandbox",
                "Usage: /sandbox allow-unsandboxed <on|off> (or /sandbox overrides open|closed)",
            )
            return SlashCommandOutcome()
        manager.set_sandbox_settings(allow_unsandboxed_commands=value)
        return await send_current(
            f"allowUnsandboxedCommands set to {'true' if value else 'false'}."
        )

    if subcommand == "exclude":
        if len(args) < 2:
            await context.send_command_result(
                "sandbox",
                "Usage: /sandbox exclude <command-pattern>",
            )
            return SlashCommandOutcome()
        pattern = _strip_wrapping_quotes(" ".join(args[1:]))
        added_pattern = add_to_excluded_commands(pattern, cwd=cwd)
        manager.refresh_config()
        location = str(local_settings_path) if local_settings_path else "local settings"
        return await send_current(
            f"Added sandbox exclusion `{added_pattern}` to {location}."
        )

    if subcommand in {"unexclude", "remove-exclude", "remove_exclude"}:
        if len(args) < 2:
            await context.send_command_result(
                "sandbox",
                "Usage: /sandbox unexclude <command-pattern>",
            )
            return SlashCommandOutcome()
        pattern = _strip_wrapping_quotes(" ".join(args[1:]))
        removed = remove_from_excluded_commands(pattern, cwd=cwd)
        manager.refresh_config()
        if removed:
            location = str(local_settings_path) if local_settings_path else "local settings"
            return await send_current(
                f"Removed sandbox exclusion `{pattern}` from {location}."
            )
        return await send_current(f"Sandbox exclusion `{pattern}` was not present.")

    await context.send_command_result(
        "sandbox",
        f"Unknown sandbox subcommand: {subcommand}\n{_sandbox_usage()}",
    )
    return SlashCommandOutcome()


def _render_messages_as_text(messages: list[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "system")).upper()
        text = _message_text(message)
        if text:
            rendered.append(f"{role}: {text}")
    return "\n\n".join(rendered).strip() + "\n"


def _render_messages_as_markdown(messages: list[dict[str, Any]], session_id: str) -> str:
    rendered = [f"# OpenSpace Session `{session_id}`", ""]
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "system")).capitalize()
        text = _message_text(message)
        if not text:
            continue
        rendered.append(f"## {role}")
        rendered.append("")
        rendered.append(text)
        rendered.append("")
    return "\n".join(rendered).rstrip() + "\n"


async def _run_git_diff(project_path: str, path_arg: str | None) -> tuple[int, str, str]:
    argv = ["git", "-C", project_path, "diff"]
    if path_arg:
        argv.extend(["--", path_arg])

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


def _parse_settings_value(raw: str) -> Any:
    text = raw.strip()
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    if text.lower() in {"null", "none", "unset"}:
        return None
    try:
        return json.loads(text)
    except Exception:
        return raw


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def execute_slash_command(
    context: SlashCommandContext,
    command: str,
    args: list[str] | None = None,
) -> SlashCommandOutcome:
    args = list(args or [])
    normalized = command.lstrip("/").lower()

    if normalized == "doctor":
        from openspace.core.doctor import Doctor

        doctor = Doctor(
            tui_bridge=context.bridge,
            openspace=context.openspace,
        )
        await doctor.run_all()
        await context.send_command_result(
            "doctor",
            "Doctor run completed",
        )
        return SlashCommandOutcome()

    if normalized == "resume":
        if args:
            session_id = await context.handle_resume(
                {"action": "restore", "session_id": args[0]}
            )
            if session_id:
                await context.send_command_result(
                    "resume",
                    f"Restored session {session_id}",
                )
            return SlashCommandOutcome(session_id=session_id)

        await context.handle_resume({"action": "list"})
        await context.send_command_result(
            "resume",
            "Listed resumable sessions",
            display="skip",
        )
        return SlashCommandOutcome()

    if normalized == "load":
        if not args:
            await context.send_notification(
                "warn",
                "Load",
                "Usage: /load <session-id>",
            )
            return SlashCommandOutcome()
        session_id = await context.handle_resume(
            {"action": "restore", "session_id": args[0]}
        )
        if session_id:
            await context.send_command_result(
                "load",
                f"Loaded session {session_id}",
            )
        return SlashCommandOutcome(session_id=session_id)

    if normalized == "cost":
        from openspace.services.runtime_support.cost import format_total_cost

        await context.send_command_result(
            "cost",
            format_total_cost(context.openspace.cost_tracker),
        )
        return SlashCommandOutcome()

    if normalized == "effort":
        if not args or args[0].lower() in {"current", "status"}:
            await context.send_command_result(
                "effort",
                _format_effort_status(context.openspace),
            )
            return SlashCommandOutcome()

        requested = args[0].lower()
        if requested in {"help", "-h", "--help"}:
            await context.send_command_result("effort", _format_effort_help())
            return SlashCommandOutcome()

        if requested not in {"auto", "unset"} and not is_effort_level(requested):
            await context.send_command_result(
                "effort",
                f"Invalid argument: {args[0]}. Valid options are: low, medium, high, max, auto",
            )
            return SlashCommandOutcome()

        updater = getattr(context.openspace, "update_reasoning_effort", None)
        if not callable(updater):
            await context.send_command_result(
                "effort",
                "Reasoning effort runtime is not available.",
            )
            return SlashCommandOutcome()

        next_value = None if requested in {"auto", "unset"} else requested
        updater(next_value)
        await context.sync_status({
            "reasoning_effort": next_value or "auto",
            "effort": next_value or "auto",
        })
        if next_value is None:
            env_state, env_value, env_raw = get_effort_env_state()
            if env_state == "value":
                env_display = getattr(env_value, "value", env_value)
                await context.send_command_result(
                    "effort",
                    (
                        "Effort level set to auto, but "
                        f"effort env={env_raw} still controls this session "
                        f"({env_display})"
                    ),
                )
            else:
                await context.send_command_result("effort", "Effort level set to auto")
        else:
            description = get_effort_level_description(next_value)
            env_state, env_value, env_raw = get_effort_env_state()
            prefix = ""
            if env_state in {"auto", "value"}:
                env_display = getattr(env_value, "value", env_value)
                if env_state == "auto" or env_display != next_value:
                    prefix = (
                        f"Not applied this session: effort env={env_raw} "
                        "overrides runtime effort. "
                    )
            await context.send_command_result(
                "effort",
                f"{prefix}Set effort level to {next_value}: {description}",
            )
        return SlashCommandOutcome()

    if normalized == "model":
        if args:
            model_name = args[0]
            context.openspace.update_main_loop_model(model_name)
            await context.sync_status({"model": model_name})
            await context.send_command_result(
                "model",
                f"Model set to {model_name}",
            )
            return SlashCommandOutcome()

        current_model = context.openspace.config.llm_model
        await context.send_command_result(
            "model",
            f"Current model: {current_model}",
        )
        return SlashCommandOutcome()

    if normalized == "plan":
        cwd = (
            getattr(getattr(context.openspace, "config", None), "workspace_dir", None)
            or os.getcwd()
        )
        set_session_permission_mode("plan", str(cwd))
        session_id = getattr(context.openspace, "current_session_id", None)
        plan_path = None
        try:
            from openspace.services.runtime_support.plan_mode import get_plan_file_path

            plan_path = str(get_plan_file_path(session_id, "primary"))
        except Exception:
            plan_path = None
        status: dict[str, Any] = {"permission_mode": "plan"}
        if plan_path:
            status["plan_file_path"] = plan_path
        await context.sync_status(status)
        message = (
            "Entered plan mode. The next agent turn will be restricted to "
            "read-only tools plus the plan file and ExitPlanMode."
        )
        if plan_path:
            message += f"\nPlan file: {plan_path}"
        await context.send_command_result("plan", message)
        return SlashCommandOutcome(session_id=session_id)

    if normalized == "save":
        session_name = args[0] if args else None
        saved = await context.openspace.save_current_session(session_name)
        label = saved.get("name") or saved["session_id"]
        await context.send_command_result(
            "save",
            f"Saved session {label}",
        )
        await context.sync_status(None)
        return SlashCommandOutcome(session_id=saved["session_id"])

    if normalized in {"config", "settings"}:
        from openspace.services.runtime_support.settings import (
            get_setting,
            get_settings_with_errors,
            update_setting,
        )

        cwd = getattr(context.openspace.config, "workspace_dir", None) or os.getcwd()
        if len(args) >= 2:
            value = _parse_settings_value(" ".join(args[1:]))
            snapshot = update_setting(args[0], value, cwd=cwd, source="userSettings")
            await context.handle_settings_update(
                {"key": args[0], "value": value, "persisted": True}
            )
            await context.send_command_result(
                "config",
                f"Updated setting {args[0]} = {_json_compact(get_setting(args[0], cwd=cwd, refresh=True))}",
            )
            return SlashCommandOutcome()

        if len(args) == 1:
            await context.send_command_result(
                "config",
                f"{args[0]} = {_json_compact(get_setting(args[0], cwd=cwd, refresh=True))}",
            )
            return SlashCommandOutcome()

        try:
            snapshot = get_settings_with_errors(cwd, refresh=True)
            from openspace.tools.config_tool import get_grouped_keys

            groups = get_grouped_keys()
            error_lines = ""
            if snapshot.errors:
                error_lines = "\n\nValidation errors:\n" + "\n".join(
                    f"- {err.key_path or '<root>'}: {err.message}"
                    for err in snapshot.errors[:5]
                )
            message = (
                "Configurable settings:\n"
                f"Stable engine: {', '.join(groups['stable_engine'])}\n"
                f"Stable UI: {', '.join(groups['stable_ui'])}\n"
                f"Experimental: {', '.join(groups['experimental'])}\n\n"
                f"Effective model: {_json_compact(snapshot.raw.get('model'))}\n"
                f"Permission mode: {_json_compact(snapshot.raw.get('permissions', {}).get('defaultMode'))}"
                f"{error_lines}"
            )
        except Exception:
            message = "Usage: /config [key] [value]"
        await context.send_command_result("config", message)
        return SlashCommandOutcome()

    if normalized == "compact":
        session_id, restored = await _load_active_session(context.openspace)
        if not session_id or not restored:
            await context.send_notification(
                "warn",
                "Compact",
                "No active session to compact",
            )
            return SlashCommandOutcome()

        messages = restored.get("messages", [])
        if not isinstance(messages, list) or len(messages) == 0:
            await context.send_command_result("compact", "Nothing to compact")
            return SlashCommandOutcome(session_id=session_id)

        instructions = " ".join(args).strip() or None
        await context.bridge.send(
            CoreToTuiEvent.COMPACT_START.value,
            {"reason": instructions or "Manual compact"},
        )
        compacted_messages = _manual_compact_messages(messages, instructions)

        await context.openspace.save_compacted_session(session_id, compacted_messages)

        await context.bridge.send(
            CoreToTuiEvent.COMPACT_COMPLETE.value,
            {
                "reason": instructions or "Manual compact",
                "messages": compacted_messages,
            },
        )
        await context.sync_status({"phase": "compacted"})
        await context.send_command_result(
            "compact",
            f"Compacted session from {len(messages)} to {len(compacted_messages)} messages",
            display="skip",
        )
        return SlashCommandOutcome(session_id=session_id)

    if normalized == "summary":
        runner = getattr(context.openspace, "run_manual_summary", None)
        if not callable(runner):
            await context.send_command_result(
                "summary",
                "Session memory summary runtime is not available.",
            )
            return SlashCommandOutcome()

        result = await runner()
        session_id = None
        if isinstance(result, dict):
            raw_session_id = result.get("session_id")
            session_id = str(raw_session_id) if raw_session_id else None
        await context.send_command_result(
            "summary",
            _format_summary_command_result(result),
            display="system",
        )
        return SlashCommandOutcome(session_id=session_id)

    if normalized == "tools":
        grounding_client = context.openspace.get_grounding_client()
        if grounding_client is None:
            await context.send_command_result(
                "tools",
                "Grounding client is not available.",
            )
            return SlashCommandOutcome()

        tools = await grounding_client.list_tools(use_cache=True)
        await context.send_command_result(
            "tools",
            _format_tools_output(tools, args[0] if args else None),
        )
        return SlashCommandOutcome()

    if normalized == "skills":
        registry = context.openspace.get_skill_registry()
        if registry is None:
            await context.send_command_result(
                "skills",
                "Skill registry is not initialized.",
            )
            return SlashCommandOutcome()

        if args and args[0] == "overlay":
            action = args[1] if len(args) > 1 else "review"
            if action == "review":
                skill_id_filter = args[2] if len(args) > 2 else None
                await context.send_command_result(
                    "skills",
                    _format_skill_overlay_output(
                        registry.list_runtime_overlays(),
                        skill_id_filter,
                    ),
                )
                return SlashCommandOutcome()

            if action in {"approve", "reject"}:
                if len(args) < 3:
                    await context.send_command_result(
                        "skills",
                        "Usage: /skills overlay approve|reject <skill_id> [field ...]",
                    )
                    return SlashCommandOutcome()
                skill_id = args[2]
                fields = args[3:] or None
                if action == "approve":
                    changed = registry.approve_runtime_overlay(skill_id, fields)
                    store = context.openspace.get_skill_store()
                    if store is not None and changed:
                        try:
                            await store.record_skill_event(
                                skill_id,
                                "field_approved",
                                source="slash_skills_overlay",
                                metadata={"fields": changed},
                            )
                        except Exception:
                            pass
                    verb = "Approved"
                else:
                    changed = registry.reject_runtime_overlay(skill_id, fields)
                    store = context.openspace.get_skill_store()
                    if store is not None and changed:
                        try:
                            await store.record_skill_event(
                                skill_id,
                                "field_rejected",
                                source="slash_skills_overlay",
                                metadata={"fields": changed},
                            )
                        except Exception:
                            pass
                    verb = "Rejected"
                if changed:
                    await context.send_command_result(
                        "skills",
                        f"{verb} runtime overlay field(s) for {skill_id}: "
                        + ", ".join(changed),
                    )
                else:
                    await context.send_command_result(
                        "skills",
                        f"No matching suggested runtime overlay fields for {skill_id}.",
                    )
                return SlashCommandOutcome()

            await context.send_command_result(
                "skills",
                "Usage: /skills overlay review [skill_id] | approve|reject <skill_id> [field ...]",
            )
            return SlashCommandOutcome()

        await context.send_command_result(
            "skills",
            _format_skills_output(registry.list_skills(), args[0] if args else None),
        )
        return SlashCommandOutcome()

    if normalized == "review":
        target = args[0] if args else "."
        review_prompt = (
            f"Review the current changes in `{target}`. "
            "Prioritize bugs, risks, regressions, and missing tests. "
            "Present findings first with concrete file references."
        )
        await context.send_command_result(
            "review",
            f"Starting review for {target}",
            display="skip",
            next_input=review_prompt,
            submit_next_input=True,
        )
        return SlashCommandOutcome()

    if normalized == "diff":
        project_path = _resolve_project_path(context.openspace)
        path_arg = args[0] if args else None
        returncode, stdout, stderr = await _run_git_diff(project_path, path_arg)
        if returncode != 0:
            message = stderr.strip() or "git diff failed"
            await context.send_command_result("diff", message)
            return SlashCommandOutcome()
        rendered = stdout.strip()
        if not rendered:
            rendered = "No diff found."
        await context.send_command_result("diff", _truncate(rendered, 20000))
        return SlashCommandOutcome()

    if normalized == "export":
        session_id, restored = await _load_active_session(context.openspace)
        if not session_id or not restored:
            await context.send_notification(
                "warn",
                "Export",
                "No active session to export",
            )
            return SlashCommandOutcome()

        format_type = (args[0] if args else "md").lower()
        suffix_by_format = {"md": ".md", "markdown": ".md", "json": ".json", "txt": ".txt"}
        if format_type not in suffix_by_format:
            await context.send_command_result(
                "export",
                "Usage: /export [md|json|txt] [path]",
            )
            return SlashCommandOutcome(session_id=session_id)

        output_path = (
            Path(args[1]).expanduser()
            if len(args) >= 2
            else Path.cwd() / f"openspace-session-{session_id}{suffix_by_format[format_type]}"
        )
        messages = restored.get("messages", [])
        if not isinstance(messages, list):
            messages = []

        if format_type == "json":
            payload = json.dumps(messages, ensure_ascii=False, indent=2) + "\n"
        elif format_type == "txt":
            payload = _render_messages_as_text(messages)
        else:
            payload = _render_messages_as_markdown(messages, session_id)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload, encoding="utf-8")
        await context.send_command_result(
            "export",
            f"Exported session to {output_path}",
        )
        return SlashCommandOutcome(session_id=session_id)

    if normalized == "init":
        output_path = Path(_resolve_project_path(context.openspace)) / "OPENSPACE.md"
        if output_path.exists():
            await context.send_command_result(
                "init",
                f"{output_path.name} already exists at {output_path}",
            )
            return SlashCommandOutcome()

        template = "\n".join(
            [
                "# OpenSpace Project Context",
                "",
                "## Overview",
                "- Describe the project and its primary goal.",
                "",
                "## Important Paths",
                "- List the directories and files the agent should know first.",
                "",
                "## Constraints",
                "- Record runtime, security, style, or deployment constraints.",
                "",
                "## Recommended Workflows",
                "- Note repeatable commands, test flows, and review expectations.",
                "",
            ]
        )
        output_path.write_text(template, encoding="utf-8")
        await context.send_command_result(
            "init",
            f"Created {output_path}",
        )
        return SlashCommandOutcome()

    if normalized == "memory":
        from openspace.cli.commands.memory_command import (
            build_memory_selector_payload,
            execute_memory_command,
        )

        if not args:
            await context.bridge.send(
                CoreToTuiEvent.MEMORY_SELECTOR.value,
                build_memory_selector_payload(
                    openspace=context.openspace,
                    cwd=_resolve_project_path(context.openspace),
                ),
            )
            return SlashCommandOutcome()

        result = await execute_memory_command(
            args,
            openspace=context.openspace,
            cwd=_resolve_project_path(context.openspace),
        )
        await context.send_command_result(
            "memory",
            result.message,
            display=result.display,
        )
        return SlashCommandOutcome()

    if normalized == "dream":
        from openspace.cli.commands.dream_command import execute_dream_command

        result = await execute_dream_command(
            args,
            openspace=context.openspace,
        )
        await context.send_command_result(
            "dream",
            result.message,
            display=result.display,
        )
        return SlashCommandOutcome()

    if normalized == "permissions":
        await context.send_command_result(
            "permissions",
            _format_permissions_output(context.openspace),
        )
        return SlashCommandOutcome()

    if normalized == "sandbox":
        return await _execute_sandbox_command(context, args)

    await context.send_notification(
        "warn",
        "Slash Command",
        f"Unsupported slash command: /{normalized}",
    )
    return SlashCommandOutcome()
