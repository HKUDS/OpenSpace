"""Message construction helpers for GroundingAgent."""

from __future__ import annotations

import copy
from typing import Any, Iterable

from openspace.agents.turns.message_utils import (
    build_channel_context_message,
    normalize_external_history,
)
from openspace.prompts import GroundingAgentPrompts
from openspace.services.conversation.messages import build_agent_injection_message
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


def normalize_response_style(value: Any = None) -> str:
    raw = str(value or "").strip().lower()
    return "brief" if raw in {"brief", "concise", "short"} else "normal"


def apply_response_style_prompt(prompt: str, response_style: Any = None) -> str:
    if normalize_response_style(response_style) != "brief":
        return prompt
    return (
        f"{prompt}\n\n# Response Style\n"
        "Brief mode is active. Keep assistant text concise: answer directly, "
        "omit low-value exposition, and include only the details needed for "
        "the user to act or understand the result. Do not change tool usage "
        "or permission decisions because of this style."
    )


def skills_disabled_for_context(context: dict[str, Any]) -> bool:
    return bool(context.get("skills_disabled"))


def default_system_prompt(
    agent: Any,
    cwd: str | None = None,
    *,
    deferred_tool_names: Iterable[str] | None = None,
    memory_mode: str | None = None,
    skills_enabled: bool = True,
    skill_discovery_enabled: bool | None = None,
) -> str:
    model = getattr(getattr(agent, "_llm_client", None), "model", None)
    registry = getattr(agent, "_skill_registry", None)
    registry_has_skills = bool(registry and registry.list_skills())
    effective_skills_enabled = bool(skills_enabled and registry_has_skills)
    effective_discovery_enabled = bool(
        effective_skills_enabled
        and (
            getattr(agent, "_skill_discovery_enabled", True)
            if skill_discovery_enabled is None
            else skill_discovery_enabled
        )
    )
    return GroundingAgentPrompts.build_system_prompt(
        getattr(agent, "_backend_scope", []),
        cwd=cwd,
        model=model,
        deferred_tool_names=deferred_tool_names,
        memory_mode=memory_mode,
        skills_enabled=effective_skills_enabled,
        skill_discovery_enabled=effective_discovery_enabled,
    )


def current_system_prompt(
    agent: Any,
    cwd: str | None = None,
    *,
    deferred_tool_names: Iterable[str] | None = None,
    memory_mode: str | None = None,
    skills_enabled: bool = True,
    skill_discovery_enabled: bool | None = None,
    permission_mode: str | None = None,
    plan_file_path: str | None = None,
    response_style: str | None = None,
) -> str:
    custom_system_prompt = getattr(agent, "_custom_system_prompt", None)
    if custom_system_prompt is not None:
        return custom_system_prompt
    prompt = default_system_prompt(
        agent,
        cwd=cwd,
        deferred_tool_names=deferred_tool_names,
        memory_mode=memory_mode,
        skills_enabled=skills_enabled,
        skill_discovery_enabled=skill_discovery_enabled,
    )
    if permission_mode == "plan":
        prompt += (
            "\n\n# Plan Mode\n"
            "You are currently in plan mode. Do not make implementation "
            "changes. Use read-only tools to explore and write the plan to "
            "the plan file, then call ExitPlanMode for user approval."
        )
        if plan_file_path:
            prompt += f"\nPlan file: {plan_file_path}"
    return apply_response_style_prompt(prompt, response_style)


def coordinator_system_prompt(
    agent: Any,
    *,
    coordinator_mode: Any | None = None,
    coordinator_mode_enabled: bool | None = None,
) -> str | None:
    coordinator = coordinator_mode or getattr(agent, "_coordinator_mode", None)
    if coordinator is None:
        return None
    if coordinator_mode_enabled is not None:
        if not coordinator_mode_enabled:
            return None
        return coordinator.get_coordinator_system_prompt()
    if coordinator.is_enabled():
        return coordinator.get_coordinator_system_prompt()
    return None


def refresh_primary_system_prompt(
    agent: Any,
    messages: list[dict[str, Any]],
    *,
    cwd: str | None = None,
    deferred_tool_names: Iterable[str] | None = None,
    memory_mode: str | None = None,
    skills_enabled: bool = True,
    skill_discovery_enabled: bool | None = None,
    permission_mode: str | None = None,
    plan_file_path: str | None = None,
    response_style: str | None = None,
    coordinator_mode: Any | None = None,
    coordinator_mode_enabled: bool | None = None,
) -> None:
    if getattr(agent, "_custom_system_prompt", None) is not None:
        return
    coordinator_prompt = coordinator_system_prompt(
        agent,
        coordinator_mode=coordinator_mode,
        coordinator_mode_enabled=coordinator_mode_enabled,
    )
    if coordinator_prompt is not None:
        prompt = coordinator_prompt
    else:
        prompt = current_system_prompt(
            agent,
            cwd=cwd,
            deferred_tool_names=deferred_tool_names,
            memory_mode=memory_mode,
            skills_enabled=skills_enabled,
            skill_discovery_enabled=skill_discovery_enabled,
            permission_mode=permission_mode,
            plan_file_path=plan_file_path,
            response_style=response_style,
        )
    for message in messages:
        if message.get("role") == "system":
            message["content"] = prompt
            return


def refresh_system_messages_after_compact(
    agent: Any,
    messages: list[dict[str, Any]],
    *,
    cwd: str | None = None,
    deferred_tool_names: Iterable[str] | None = None,
    memory_mode: str | None = None,
    skills_enabled: bool = True,
    skill_discovery_enabled: bool | None = None,
    permission_mode: str | None = None,
    plan_file_path: str | None = None,
    response_style: str | None = None,
    coordinator_mode: Any | None = None,
    coordinator_mode_enabled: bool | None = None,
) -> list[dict[str, Any]]:
    system_msgs = [
        copy.deepcopy(message)
        for message in messages
        if message.get("role") == "system"
    ]
    if system_msgs and getattr(agent, "_custom_system_prompt", None) is None:
        coordinator_prompt = coordinator_system_prompt(
            agent,
            coordinator_mode=coordinator_mode,
            coordinator_mode_enabled=coordinator_mode_enabled,
        )
        if coordinator_prompt is not None:
            system_msgs[0]["content"] = coordinator_prompt
        else:
            system_msgs[0]["content"] = current_system_prompt(
                agent,
                cwd=cwd,
                deferred_tool_names=deferred_tool_names,
                memory_mode=memory_mode,
                skills_enabled=skills_enabled,
                skill_discovery_enabled=skill_discovery_enabled,
                permission_mode=permission_mode,
                plan_file_path=plan_file_path,
                response_style=response_style,
            )
    return system_msgs


def construct_messages(
    agent: Any,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    workspace_dir = context.get("workspace_dir")
    coordinator = context.get("coordinator_mode") or getattr(agent, "_coordinator_mode", None)
    if context.get("coordinator_mode_enabled") and coordinator is not None:
        primary_system_prompt = coordinator.get_coordinator_system_prompt()
    else:
        skills_enabled = bool(
            not skills_disabled_for_context(context)
            and context.get("skill_tool_available", True)
        )
        skill_discovery_enabled = bool(
            skills_enabled
            and context.get(
                "discover_skills_tool_available",
                getattr(agent, "_skill_discovery_enabled", True),
            )
        )
        primary_system_prompt = current_system_prompt(
            agent,
            cwd=workspace_dir,
            deferred_tool_names=context.get("deferred_tool_names"),
            memory_mode=context.get("memory_mode"),
            skills_enabled=skills_enabled,
            skill_discovery_enabled=skill_discovery_enabled,
            permission_mode=context.get("permission_mode"),
            plan_file_path=context.get("plan_file_path"),
            response_style=context.get("response_style"),
        )
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": primary_system_prompt,
        }
    ]

    instruction = context.get("instruction", "")
    if not instruction:
        raise ValueError("context must contain 'instruction' field")

    if workspace_dir:
        messages.append(
            {
                "role": "system",
                "content": GroundingAgentPrompts.workspace_directory(workspace_dir),
            }
        )

    workspace_artifacts = context.get("workspace_artifacts")
    if workspace_artifacts and workspace_artifacts.get("has_files"):
        files = workspace_artifacts.get("files", [])
        matching_files = workspace_artifacts.get("matching_files", [])
        recent_files = workspace_artifacts.get("recent_files", [])

        if matching_files:
            artifact_msg = GroundingAgentPrompts.workspace_matching_files(matching_files)
        elif len(recent_files) >= 2:
            artifact_msg = GroundingAgentPrompts.workspace_recent_files(
                total_files=len(files),
                recent_files=recent_files,
            )
        else:
            artifact_msg = GroundingAgentPrompts.workspace_file_list(files)

        messages.append({"role": "system", "content": artifact_msg})

    channel_context_msg = build_channel_context_message(context.get("channel_context"))
    if channel_context_msg:
        messages.append({"role": "system", "content": channel_context_msg})

    hook_contexts = context.get("hook_additional_contexts")
    if isinstance(hook_contexts, list):
        for item in hook_contexts:
            if item:
                messages.append(
                    {
                        "role": "system",
                        "content": f"Hook additional context:\n{item}",
                        "_meta": {"type": "hook_additional_context"},
                    }
                )

    worker_tools_context = context.get("coordinator_worker_tools_context")
    if isinstance(worker_tools_context, dict):
        for value in worker_tools_context.values():
            if value:
                messages.append({"role": "system", "content": str(value)})

    external_history = normalize_external_history(
        context.get("conversation_history")
    )
    if external_history:
        messages.extend(external_history)
        logger.info(
            "Injected %d external conversation message(s)",
            len(external_history),
        )

    initial_user_message = context.get("session_start_initial_user_message")
    if isinstance(initial_user_message, str) and initial_user_message.strip():
        messages.append(
            {
                "role": "user",
                "content": initial_user_message.strip(),
                "_meta": {"type": "session_start_initial_user_message"},
            }
        )

    messages.append({"role": "user", "content": instruction})
    return messages


def format_injected_message(msg: Any) -> dict[str, Any]:
    if isinstance(msg, dict) and "role" in msg:
        return msg
    if isinstance(msg, dict):
        msg_type = msg.get("type", "message")
        sender = msg.get("from", "unknown")
        content = msg.get("content", "")
        return build_agent_injection_message(
            from_agent=str(sender),
            content=str(content),
            message_type=str(msg_type),
        )
    return build_agent_injection_message(
        from_agent="unknown",
        content=str(msg),
        message_type="message",
    )


__all__ = [
    "apply_response_style_prompt",
    "construct_messages",
    "coordinator_system_prompt",
    "current_system_prompt",
    "default_system_prompt",
    "format_injected_message",
    "normalize_response_style",
    "refresh_primary_system_prompt",
    "refresh_system_messages_after_compact",
    "skills_disabled_for_context",
]
