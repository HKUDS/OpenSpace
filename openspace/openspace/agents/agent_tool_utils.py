from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from openspace.agents.agent_definitions import (
    AgentDefinition,
    AgentSource,
    filter_agents_by_mcp_requirements,
)
from openspace.agents.built_in_agents import (
    AGENT_TOOL_NAME,
    BASH_TOOL_NAME,
    FILE_EDIT_TOOL_NAME,
    FILE_READ_TOOL_NAME,
    FILE_WRITE_TOOL_NAME,
    GLOB_TOOL_NAME,
    GREP_TOOL_NAME,
    LEGACY_AGENT_TOOL_NAME,
    NOTEBOOK_EDIT_TOOL_NAME,
    ONE_SHOT_BUILTIN_AGENT_TYPES,
    WEB_FETCH_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
)
from openspace.grounding.core.permissions.types import parse_rule_value
from openspace.grounding.core.tool.base import BaseTool
from openspace.llm.types import EMPTY_USAGE, TokenUsage, get_token_count_from_usage


TASK_OUTPUT_TOOL_NAME = "TaskOutput"
TASK_STOP_TOOL_NAME = "TaskStop"
TASK_GET_TOOL_NAME = "TaskGet"
TASK_LIST_TOOL_NAME = "TaskList"
SEND_MESSAGE_TOOL_NAME = "SendMessage"
TEAM_CREATE_TOOL_NAME = "TeamCreate"
TEAM_DELETE_TOOL_NAME = "TeamDelete"
EXIT_PLAN_MODE_TOOL_NAME = "ExitPlanMode"
ENTER_PLAN_MODE_TOOL_NAME = "EnterPlanMode"
ASK_USER_QUESTION_TOOL_NAME = "ask_user_question"
CONFIG_TOOL_NAME = "config"
TOOL_SEARCH_TOOL_NAME = "tool_search"
TODO_WRITE_TOOL_NAME = "todo_write"
SYNTHETIC_OUTPUT_TOOL_NAME = "synthetic_output"
ENTER_WORKTREE_TOOL_NAME = "enter_worktree"
EXIT_WORKTREE_TOOL_NAME = "exit_worktree"


ALL_AGENT_DISALLOWED_TOOLS = frozenset(
    {
        TASK_OUTPUT_TOOL_NAME,
        EXIT_PLAN_MODE_TOOL_NAME,
        ENTER_PLAN_MODE_TOOL_NAME,
        AGENT_TOOL_NAME,
        LEGACY_AGENT_TOOL_NAME,
        ASK_USER_QUESTION_TOOL_NAME,
        TASK_STOP_TOOL_NAME,
        TEAM_CREATE_TOOL_NAME,
        TEAM_DELETE_TOOL_NAME,
    }
)
"""OpenSpace constants/tools.ts::ALL_AGENT_DISALLOWED_TOOLS, mapped to OS names."""


CUSTOM_AGENT_DISALLOWED_TOOLS = ALL_AGENT_DISALLOWED_TOOLS
"""OpenSpace currently uses the same deny set for custom agents."""


ASYNC_AGENT_ALLOWED_TOOLS = frozenset(
    {
        FILE_READ_TOOL_NAME,
        WEB_SEARCH_TOOL_NAME,
        TODO_WRITE_TOOL_NAME,
        GREP_TOOL_NAME,
        WEB_FETCH_TOOL_NAME,
        GLOB_TOOL_NAME,
        BASH_TOOL_NAME,
        FILE_EDIT_TOOL_NAME,
        FILE_WRITE_TOOL_NAME,
        NOTEBOOK_EDIT_TOOL_NAME,
        "notebook_edit",
        SYNTHETIC_OUTPUT_TOOL_NAME,
        TOOL_SEARCH_TOOL_NAME,
        ENTER_WORKTREE_TOOL_NAME,
        EXIT_WORKTREE_TOOL_NAME,
        "ls",
        "read_file",
        "list_directory",
        "memory_log",
        "memory_write",
    }
)
"""OpenSpace constants/tools.ts::ASYNC_AGENT_ALLOWED_TOOLS, plus OS tool aliases."""


IN_PROCESS_TEAMMATE_ALLOWED_TOOLS = frozenset(
    {
        TASK_GET_TOOL_NAME,
        TASK_LIST_TOOL_NAME,
        SEND_MESSAGE_TOOL_NAME,
    }
)


COORDINATOR_MODE_ALLOWED_TOOLS = frozenset(
    {
        AGENT_TOOL_NAME,
        TASK_STOP_TOOL_NAME,
        SEND_MESSAGE_TOOL_NAME,
        SYNTHETIC_OUTPUT_TOOL_NAME,
        TASK_GET_TOOL_NAME,
        TASK_LIST_TOOL_NAME,
    }
)


@dataclass(slots=True)
class ResolvedAgentTools:
    has_wildcard: bool
    valid_tools: list[str]
    invalid_tools: list[str]
    resolved_tools: list[BaseTool]
    allowed_agent_types: list[str] | None = None


@dataclass(slots=True)
class AgentToolResult:
    agent_id: str
    agent_type: str | None
    content: list[dict[str, str]]
    total_tool_use_count: int
    total_duration_ms: int
    total_tokens: int
    usage: dict[str, Any]
    status: str = "completed"
    prompt: str = ""


def tool_matches_name(tool: BaseTool, name: str) -> bool:
    if tool.name == name:
        return True
    return name in set(getattr(tool, "aliases", []) or ())


def filter_tools_for_agent(
    tools: Sequence[BaseTool],
    agent_definition: AgentDefinition | None = None,
    *,
    is_built_in: bool | None = None,
    is_async: bool = False,
    is_main_thread: bool = False,
    permission_mode: str | None = None,
    is_teammate: bool = False,
    is_coordinator: bool = False,
) -> list[BaseTool]:
    """Filter a tool pool using OpenSpace's subagent rules.

    Implementation notes: ``tools/AgentTool/agentToolUtils.ts::filterToolsForAgent``.
    OpenSpace adds the ``agent_definition.is_read_only`` pass so Explore/Plan
    cannot receive write tools even when a future OS-only tool forgets to add
    itself to the agent deny list.
    """

    if is_main_thread:
        return list(tools)

    if is_built_in is None:
        source = agent_definition.source if agent_definition else AgentSource.CUSTOM
        is_built_in = _source_value(source) == AgentSource.BUILT_IN.value

    filtered: list[BaseTool] = []
    for tool in tools:
        name = tool.name

        if is_coordinator:
            if name in COORDINATOR_MODE_ALLOWED_TOOLS:
                filtered.append(tool)
            continue

        # Implementation: MCP tools are allowed for all agents regardless of the generic
        # subagent deny list.
        if name.startswith("mcp__"):
            filtered.append(tool)
            continue

        if name == EXIT_PLAN_MODE_TOOL_NAME and permission_mode == "plan":
            filtered.append(tool)
            continue

        if name in ALL_AGENT_DISALLOWED_TOOLS:
            continue
        if not is_built_in and name in CUSTOM_AGENT_DISALLOWED_TOOLS:
            continue

        if is_async and name not in ASYNC_AGENT_ALLOWED_TOOLS:
            if is_teammate and (
                name == AGENT_TOOL_NAME or name in IN_PROCESS_TEAMMATE_ALLOWED_TOOLS
            ):
                filtered.append(tool)
            continue

        filtered.append(tool)

    if agent_definition and agent_definition.is_read_only:
        filtered = [tool for tool in filtered if _tool_is_read_only(tool)]

    return filtered


def resolve_agent_tools(
    agent_definition: AgentDefinition,
    available_tools: Sequence[BaseTool],
    *,
    is_async: bool = False,
    is_main_thread: bool = False,
    is_teammate: bool = False,
    is_coordinator: bool = False,
) -> ResolvedAgentTools:
    """Resolve an ``AgentDefinition.tools`` spec against available tools.

    Mirrors OpenSpace ``resolveAgentTools``: wildcard means all filtered tools;
    explicit specs are validated, deduped, and ``Agent(type,type)`` is parsed
    as allowed-agent-type metadata even when the Agent tool is not handed to
    the subagent.
    """

    filtered_available = filter_tools_for_agent(
        available_tools,
        agent_definition,
        is_async=is_async,
        is_main_thread=is_main_thread,
        is_teammate=is_teammate,
        is_coordinator=is_coordinator,
        permission_mode=agent_definition.permission_mode,
    )

    disallowed = {
        _parse_tool_spec(spec)[0]
        for spec in (agent_definition.disallowed_tools or [])
    }
    allowed_available = [
        tool for tool in filtered_available
        if tool.name not in disallowed
        and not any(alias in disallowed for alias in (getattr(tool, "aliases", []) or ()))
    ]

    agent_tools = agent_definition.tools
    has_wildcard = agent_tools == "*" or agent_tools is None
    if has_wildcard:
        return ResolvedAgentTools(
            has_wildcard=True,
            valid_tools=[],
            invalid_tools=[],
            resolved_tools=allowed_available,
        )

    available_by_name = {tool.name: tool for tool in allowed_available}
    for tool in allowed_available:
        for alias in getattr(tool, "aliases", []) or ():
            available_by_name.setdefault(alias, tool)
    resolved: list[BaseTool] = []
    seen_tools: set[int] = set()
    valid_tools: list[str] = []
    invalid_tools: list[str] = []
    allowed_agent_types: list[str] | None = None

    for tool_spec in agent_tools:
        tool_name, rule_content = _parse_tool_spec(tool_spec)
        if tool_name == AGENT_TOOL_NAME:
            if rule_content:
                allowed_agent_types = [
                    item.strip()
                    for item in rule_content.split(",")
                    if item.strip()
                ]
            if not is_main_thread:
                valid_tools.append(tool_spec)
                continue

        tool = available_by_name.get(tool_name)
        if tool is None:
            invalid_tools.append(tool_spec)
            continue
        valid_tools.append(tool_spec)
        identity = id(tool)
        if identity not in seen_tools:
            resolved.append(tool)
            seen_tools.add(identity)

    return ResolvedAgentTools(
        has_wildcard=False,
        valid_tools=valid_tools,
        invalid_tools=invalid_tools,
        resolved_tools=resolved,
        allowed_agent_types=allowed_agent_types,
    )


def count_tool_uses(messages: Sequence[Mapping[str, Any]]) -> int:
    count = 0
    for message in messages:
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            count += len(tool_calls)
            continue
        content = message.get("content")
        if isinstance(content, list):
            count += sum(1 for block in content if block.get("type") == "tool_use")
    return count


def finalize_agent_result(
    *,
    messages: Sequence[Mapping[str, Any]],
    agent_id: str,
    agent_type: str | None,
    prompt: str,
    start_time: float,
) -> AgentToolResult:
    last_assistant = _last_assistant_message(messages)
    if last_assistant is None:
        raise RuntimeError("No assistant messages found")

    content_blocks = _assistant_text_blocks(last_assistant)
    if not content_blocks:
        for message in reversed(messages):
            if message.get("role") != "assistant":
                continue
            content_blocks = _assistant_text_blocks(message)
            if content_blocks:
                break

    usage = _usage_from_message(last_assistant)
    total_tokens = int(usage.get("total_tokens") or 0)
    if total_tokens <= 0:
        total_tokens = int(usage.get("input_tokens") or 0) + int(
            usage.get("output_tokens") or 0
        )

    return AgentToolResult(
        agent_id=agent_id,
        agent_type=agent_type,
        content=content_blocks,
        total_tool_use_count=count_tool_uses(messages),
        total_duration_ms=max(0, int((time.time() - start_time) * 1000)),
        total_tokens=total_tokens,
        usage=usage,
        prompt=prompt,
    )


def format_agent_line(agent: AgentDefinition) -> str:
    return f"- {agent.agent_type}: {agent.when_to_use} (Tools: {_tools_description(agent)})"


def should_inject_agent_list_in_messages() -> bool:
    raw = os.environ.get("OPENSPACE_AGENT_LIST_IN_MESSAGES")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def get_agent_listing_delta_attachment(
    tool_use_context: Any,
    messages: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Provider-neutral OpenSpace ``agent_listing_delta`` attachment.

    The filtering mirrors AgentTool's prompt path: AgentTool must be active,
    agent MCP requirements must be met, deny rules are applied, then
    ``allowed_agent_types`` narrows the list.
    """

    if not should_inject_agent_list_in_messages():
        return []

    tools = list(getattr(tool_use_context, "tools", []) or [])
    if not any(tool_matches_name(tool, AGENT_TOOL_NAME) for tool in tools):
        return []

    agent_result = getattr(tool_use_context, "agent_definitions", None)
    if agent_result is None:
        return []

    active_agents = list(getattr(agent_result, "active_agents", []) or [])
    allowed_agent_types = getattr(agent_result, "allowed_agent_types", None)
    if allowed_agent_types is None:
        allowed_agent_types = getattr(tool_use_context, "allowed_agent_types", None)

    mcp_servers = _mcp_servers_from_tools(tools)
    filtered = filter_agents_by_mcp_requirements(active_agents, mcp_servers)
    filtered = filter_denied_agents(
        filtered,
        getattr(tool_use_context, "permission_context", None),
    )
    if allowed_agent_types:
        allowed = set(allowed_agent_types)
        filtered = [agent for agent in filtered if agent.agent_type in allowed]

    announced = _scan_announced_agent_types(messages or [])
    current = {agent.agent_type for agent in filtered}
    added = [agent for agent in filtered if agent.agent_type not in announced]
    removed = sorted(agent_type for agent_type in announced if agent_type not in current)
    if not added and not removed:
        return []

    added.sort(key=lambda agent: agent.agent_type)
    return [
        {
            "type": "agent_listing_delta",
            "addedTypes": [agent.agent_type for agent in added],
            "addedLines": [format_agent_line(agent) for agent in added],
            "removedTypes": removed,
            "isInitial": not announced,
            "showConcurrencyNote": True,
        }
    ]


def filter_denied_agents(
    agents: Iterable[AgentDefinition],
    permission_context: Any | None,
) -> list[AgentDefinition]:
    denied = _denied_agent_types(permission_context)
    if not denied:
        return list(agents)
    return [agent for agent in agents if agent.agent_type not in denied]


def format_agent_tool_result(result: AgentToolResult | Mapping[str, Any]) -> str:
    data = _result_to_mapping(result)
    status = data.get("status")
    if status == "async_launched":
        agent_id = str(data.get("agent_id") or data.get("agentId") or "")
        output_file = str(data.get("output_file") or data.get("outputFile") or "")
        can_read = bool(data.get("can_read_output_file") or data.get("canReadOutputFile"))
        text = (
            "Async agent launched successfully.\n"
            f"agentId: {agent_id} (internal ID - do not mention to user.)\n"
            "The agent is working in the background. You will be notified "
            "via runtime events when it completes."
        )
        task_id = str(data.get("task_id") or data.get("taskId") or "")
        if task_id:
            text += (
                f"\ntask_id: {task_id}\n"
                "Use TaskGet(task_id=...) to check status/output, TaskList to "
                "list background tasks, TaskStop(task_id=...) to stop it, and "
                "SendMessage(to_agent/task_id, message) to send follow-up "
                "instructions."
            )
        if can_read and output_file:
            text += (
                "\nDo not duplicate this agent's work. Work on non-overlapping "
                "tasks or briefly tell the user what you launched and end your response.\n"
                f"output_file: {output_file}"
            )
        else:
            text += "\nBriefly tell the user what you launched and end your response."
        return text

    if status == "completed":
        content = list(data.get("content") or [])
        if not content:
            content = [{"type": "text", "text": "(Subagent completed but returned no output.)"}]
        body = "\n".join(str(block.get("text", "")) for block in content if isinstance(block, Mapping))
        agent_type = data.get("agent_type") or data.get("agentType")
        if agent_type in ONE_SHOT_BUILTIN_AGENT_TYPES:
            return body
        trailer = (
            f"agentId: {data.get('agent_id') or data.get('agentId')} "
            "(use SendMessage with this id after Task/Team tools are available)\n"
            f"<usage>total_tokens: {int(data.get('total_tokens') or data.get('totalTokens') or 0)}\n"
            f"tool_uses: {int(data.get('total_tool_use_count') or data.get('totalToolUseCount') or 0)}\n"
            f"duration_ms: {int(data.get('total_duration_ms') or data.get('totalDurationMs') or 0)}</usage>"
        )
        return f"{body}\n{trailer}" if body else trailer

    if status in {"error", "stopped"}:
        return str(data.get("error") or status)

    raise RuntimeError(f"Unexpected agent tool result status: {status}")


def token_usage_to_dict(usage: TokenUsage | None) -> dict[str, Any]:
    usage = usage or EMPTY_USAGE
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "total_tokens": get_token_count_from_usage(usage),
        "cost": usage.cost,
    }


def _parse_tool_spec(tool_spec: str) -> tuple[str, str | None]:
    try:
        parsed = parse_rule_value(tool_spec)
        return parsed.tool_name, parsed.rule_content
    except Exception:
        return str(tool_spec), None


def _source_value(source: AgentSource | str) -> str:
    return source.value if isinstance(source, AgentSource) else str(source)


def _tool_is_read_only(tool: BaseTool) -> bool:
    try:
        return bool(tool.is_read_only({}))
    except Exception:
        return False


def _tools_description(agent: AgentDefinition) -> str:
    tools = agent.tools
    disallowed = agent.disallowed_tools or []
    has_allow = isinstance(tools, list) and bool(tools)
    has_deny = bool(disallowed)
    if has_allow and has_deny:
        deny = set(disallowed)
        effective = [tool for tool in tools if tool not in deny]
        return ", ".join(effective) if effective else "None"
    if has_allow:
        return ", ".join(tools)
    if has_deny:
        return f"All tools except {', '.join(disallowed)}"
    return "All tools"


def _mcp_servers_from_tools(tools: Iterable[BaseTool]) -> list[str]:
    servers: set[str] = set()
    for tool in tools:
        name = getattr(tool, "name", "")
        if name.startswith("mcp__"):
            parts = name.split("__")
            if len(parts) >= 3 and parts[1]:
                servers.add(parts[1])
                continue
        runtime = getattr(tool, "runtime_info", None)
        server_name = getattr(runtime, "server_name", None) if runtime else None
        if server_name:
            servers.add(str(server_name))
    return sorted(servers)


def _scan_announced_agent_types(messages: Sequence[Mapping[str, Any]]) -> set[str]:
    announced: set[str] = set()
    for message in messages:
        meta = message.get("_meta")
        if not isinstance(meta, Mapping):
            continue
        attachment = meta.get("attachment")
        if not isinstance(attachment, Mapping):
            continue
        if attachment.get("type") != "agent_listing_delta":
            continue
        for name in attachment.get("addedTypes") or []:
            announced.add(str(name))
        for name in attachment.get("removedTypes") or []:
            announced.discard(str(name))
    return announced


def _denied_agent_types(permission_context: Any | None) -> set[str]:
    denied: set[str] = set()
    rules_by_source = getattr(permission_context, "always_deny_rules", None)
    if not isinstance(rules_by_source, Mapping):
        return denied
    for rules in rules_by_source.values():
        for raw in rules or ():
            try:
                parsed = parse_rule_value(str(raw))
            except Exception:
                continue
            if parsed.tool_name != AGENT_TOOL_NAME:
                continue
            if parsed.rule_content:
                denied.add(parsed.rule_content)
    return denied


def _last_assistant_message(
    messages: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            return message
    return None


def _assistant_text_blocks(message: Mapping[str, Any]) -> list[dict[str, str]]:
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
        return [{"type": "text", "text": text}] if text else []
    if isinstance(content, list):
        out: list[dict[str, str]] = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                text = str(block.get("text", "")).strip()
                if text:
                    out.append({"type": "text", "text": text})
        return out
    return []


def _usage_from_message(message: Mapping[str, Any]) -> dict[str, Any]:
    meta = message.get("_meta")
    if isinstance(meta, Mapping):
        usage = meta.get("usage")
        if isinstance(usage, Mapping):
            return dict(usage)
    return token_usage_to_dict(None)


def _result_to_mapping(result: AgentToolResult | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(result, AgentToolResult):
        return {
            "status": result.status,
            "agent_id": result.agent_id,
            "agent_type": result.agent_type,
            "content": result.content,
            "total_tool_use_count": result.total_tool_use_count,
            "total_duration_ms": result.total_duration_ms,
            "total_tokens": result.total_tokens,
            "usage": result.usage,
            "prompt": result.prompt,
        }
    return dict(result)


__all__ = [
    "AGENT_TOOL_NAME",
    "ALL_AGENT_DISALLOWED_TOOLS",
    "ASYNC_AGENT_ALLOWED_TOOLS",
    "AgentToolResult",
    "COORDINATOR_MODE_ALLOWED_TOOLS",
    "CUSTOM_AGENT_DISALLOWED_TOOLS",
    "IN_PROCESS_TEAMMATE_ALLOWED_TOOLS",
    "ResolvedAgentTools",
    "filter_denied_agents",
    "filter_tools_for_agent",
    "finalize_agent_result",
    "format_agent_line",
    "format_agent_tool_result",
    "get_agent_listing_delta_attachment",
    "resolve_agent_tools",
    "should_inject_agent_list_in_messages",
    "tool_matches_name",
]
