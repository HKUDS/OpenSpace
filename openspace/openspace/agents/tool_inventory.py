"""Tool inventory assembly and visibility helpers for GroundingAgent."""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Iterable

from openspace.grounding.core.types import BackendType
from openspace.services.tooling.context import ToolUseContext
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


def tool_set_signature(tools: list[Any]) -> str:
    normalized_tools: list[dict[str, Any]] = []
    for tool in tools:
        schema = getattr(tool, "schema", None)
        runtime_info = getattr(tool, "runtime_info", None)
        backend = getattr(getattr(tool, "backend_type", None), "value", None)
        if runtime_info is not None:
            backend = getattr(getattr(runtime_info, "backend", None), "value", backend)

        normalized_tools.append(
            {
                "name": getattr(tool, "name", None),
                "aliases": list(getattr(tool, "aliases", []) or []),
                "description": getattr(tool, "description", None),
                "parameters": getattr(schema, "parameters", None),
                "backend": backend,
                "server_name": getattr(runtime_info, "server_name", None),
                "is_bound": bool(getattr(tool, "is_bound", False)),
                "is_deferred": bool(getattr(tool, "is_deferred", False)),
            }
        )

    normalized_tools.sort(
        key=lambda item: (
            str(item.get("name") or ""),
            str(item.get("backend") or ""),
            str(item.get("server_name") or ""),
        )
    )
    return json.dumps(normalized_tools, sort_keys=True, default=str)


def resolve_tui_available(agent: Any, context: dict[str, Any]) -> bool:
    explicit = context.get("tui_available")
    if explicit is not None:
        return bool(explicit)

    bridge = context.get("tui_bridge")
    if bridge is None:
        bridge = getattr(agent, "_tui_bridge", None)
    if bridge is None:
        return False

    proxy_owner = getattr(bridge, "_owner", None)
    if proxy_owner is not None and hasattr(proxy_owner, "_tui_bridge"):
        return bool(getattr(proxy_owner, "_tui_bridge"))

    bridge_owner = getattr(bridge, "__self__", None)
    if bridge_owner is not None and hasattr(bridge_owner, "_tui_bridge"):
        return bool(getattr(bridge_owner, "_tui_bridge"))

    return True


def resolve_async_agent(context: dict[str, Any]) -> bool:
    explicit = context.get("is_async_agent")
    if explicit is not None:
        return bool(explicit)
    return str(context.get("agent_id") or "primary") != "primary"


def resolve_permission_context(
    *,
    cwd: str,
    permission_mode: str | None,
    context: dict[str, Any],
) -> Any:
    explicit_permission_context = context.get("permission_context")
    if explicit_permission_context is not None:
        return explicit_permission_context

    try:
        from openspace.grounding.core.permissions.loader import (
            load_tool_permission_context,
        )

        return load_tool_permission_context(cwd, permission_mode)
    except Exception as exc:
        logger.error(
            "Failed to load tool permission context for %s (%s)",
            cwd,
            permission_mode,
            exc_info=True,
        )
        raise RuntimeError(
            "Failed to load tool permission settings for this workspace"
        ) from exc


def filter_tools_for_permission_mode(
    tools: list[Any],
    tool_use_context: ToolUseContext,
) -> list[Any]:
    if str(getattr(tool_use_context, "permission_mode", "") or "") != "plan":
        return list(tools)
    from openspace.agents.agent_tool_utils import EXIT_PLAN_MODE_TOOL_NAME
    from openspace.tool_runtime.pipeline.execution import (
        FILE_EDIT_TOOL_NAME,
        FILE_WRITE_TOOL_NAME,
        NOTEBOOK_EDIT_TOOL_NAME,
    )

    allowed_write_tools = {
        FILE_EDIT_TOOL_NAME,
        FILE_WRITE_TOOL_NAME,
        NOTEBOOK_EDIT_TOOL_NAME,
        EXIT_PLAN_MODE_TOOL_NAME,
    }
    filtered: list[Any] = []
    for tool in tools:
        name = str(getattr(tool, "name", "") or "")
        if name in allowed_write_tools:
            filtered.append(tool)
            continue
        try:
            if bool(tool.is_read_only({})):
                filtered.append(tool)
        except Exception:
            continue
    return filtered


def append_todo_write_tool(tools: list[Any]) -> None:
    from openspace.agents.agent_tool_utils import TODO_WRITE_TOOL_NAME, tool_matches_name
    from openspace.tools.todo_tool import TodoWriteTool

    if any(tool_matches_name(tool, TODO_WRITE_TOOL_NAME) for tool in tools):
        return
    tool = TodoWriteTool()
    if not tool.is_enabled():
        return
    tool.bind_runtime_info(backend=BackendType.META, session_name="agent")
    tools.append(tool)


def append_sleep_and_brief_tools(tools: list[Any]) -> None:
    from openspace.agents.agent_tool_utils import tool_matches_name
    from openspace.tools.brief_tool import BRIEF_TOOL_NAME, BriefTool
    from openspace.tools.sleep_tool import SLEEP_TOOL_NAME, SleepTool

    for name, tool_cls in (
        (SLEEP_TOOL_NAME, SleepTool),
        (BRIEF_TOOL_NAME, BriefTool),
    ):
        if any(tool_matches_name(tool, name) for tool in tools):
            continue
        tool = tool_cls()
        tool.bind_runtime_info(backend=BackendType.META, session_name="agent")
        tools.append(tool)


def append_schedule_cron_tools(tools: list[Any]) -> None:
    from openspace.agents.agent_tool_utils import tool_matches_name
    from openspace.tools.schedule_cron_tool import (
        SCHEDULE_CRON_CREATE_TOOL_NAME,
        SCHEDULE_CRON_DELETE_TOOL_NAME,
        SCHEDULE_CRON_LIST_TOOL_NAME,
        ScheduleCronCreateTool,
        ScheduleCronDeleteTool,
        ScheduleCronListTool,
    )

    for name, tool_cls in (
        (SCHEDULE_CRON_CREATE_TOOL_NAME, ScheduleCronCreateTool),
        (SCHEDULE_CRON_DELETE_TOOL_NAME, ScheduleCronDeleteTool),
        (SCHEDULE_CRON_LIST_TOOL_NAME, ScheduleCronListTool),
    ):
        if any(tool_matches_name(tool, name) for tool in tools):
            continue
        tool = tool_cls()
        tool.bind_runtime_info(backend=BackendType.META, session_name="agent")
        tools.append(tool)


def get_agent_definition_registry(agent: Any) -> Any:
    if getattr(agent, "_agent_definition_registry", None) is None:
        from openspace.agents.agent_definitions import AgentDefinitionRegistry

        agent._agent_definition_registry = AgentDefinitionRegistry()
    return agent._agent_definition_registry


def resolve_agent_definitions(
    agent: Any,
    context: dict[str, Any],
    tools: list[Any],
) -> Any:
    existing = context.get("agent_definitions")
    if existing is not None:
        return existing

    registry = get_agent_definition_registry(agent)
    workspace_dir = context.get("workspace_dir")
    if workspace_dir:
        try:
            from openspace.agents.agent_definitions import AgentSource

            registry.load_custom_agents(
                os.path.join(str(workspace_dir), ".openspace", "agents"),
                source=AgentSource.PROJECT_SETTINGS,
            )
        except Exception:
            logger.debug("Failed to load project agent definitions", exc_info=True)

    return registry.result(
        allowed_agent_types=context.get("allowed_agent_types"),
        available_mcp_servers=mcp_servers_from_tools(tools),
    )


def with_agent_tool(
    agent: Any,
    tools: list[Any],
    *,
    context: dict[str, Any],
    agent_definitions: Any,
) -> list[Any]:
    scope_names = {
        str(getattr(item, "value", item))
        for item in getattr(agent, "_backend_scope", [])
    }
    if "meta" not in scope_names:
        return list(tools)

    from openspace.agents.agent_tool_utils import AGENT_TOOL_NAME, tool_matches_name

    result = list(tools)

    if not any(tool_matches_name(tool, AGENT_TOOL_NAME) for tool in result):
        from openspace.agents.agent_tool import build_agent_tool

        tool = build_agent_tool(
            registry=get_agent_definition_registry(agent),
            parent_agent=agent,
            grounding_client=agent.grounding_client,
            llm_client=getattr(agent, "_llm_client", None),
            orchestrator=getattr(agent, "_multi_agent_orchestrator", None),
        )
        result.append(tool)
    append_multi_agent_control_tools(result)
    append_ask_user_question_tool(result)
    append_todo_write_tool(result)
    append_plan_mode_tools(result)
    append_config_tool(result)
    append_lsp_tool(result)
    append_sleep_and_brief_tools(result)
    append_schedule_cron_tools(result)
    context["agent_definitions"] = agent_definitions
    return result


def should_append_agent_tools(agent: Any, context: dict[str, Any]) -> bool:
    explicit = context.get("agent_tools_enabled")
    if explicit is not None:
        return bool(explicit)
    grounding_client = agent.grounding_client
    return grounding_client is not None and hasattr(grounding_client, "list_tools")


def append_config_tool(tools: list[Any]) -> None:
    from openspace.agents.agent_tool_utils import CONFIG_TOOL_NAME, tool_matches_name
    from openspace.tools.config_tool import ConfigTool

    if any(tool_matches_name(tool, CONFIG_TOOL_NAME) for tool in tools):
        return
    tool = ConfigTool()
    if not tool.is_enabled():
        return
    tool.bind_runtime_info(backend=BackendType.META, session_name="agent")
    tools.append(tool)


def append_lsp_tool(tools: list[Any]) -> None:
    from openspace.agents.agent_tool_utils import tool_matches_name
    from openspace.tools.lsp_tool import LSP_TOOL_NAME, LSPTool

    if any(tool_matches_name(tool, LSP_TOOL_NAME) for tool in tools):
        return
    tool = LSPTool()
    tool.bind_runtime_info(backend=BackendType.META, session_name="agent")
    tools.append(tool)


def append_plan_mode_tools(tools: list[Any]) -> None:
    from openspace.agents.agent_tool_utils import (
        ENTER_PLAN_MODE_TOOL_NAME,
        EXIT_PLAN_MODE_TOOL_NAME,
        tool_matches_name,
    )
    from openspace.tools.plan_mode_tools import EnterPlanModeTool, ExitPlanModeTool

    existing = list(tools)
    for name, tool_cls in (
        (EXIT_PLAN_MODE_TOOL_NAME, ExitPlanModeTool),
        (ENTER_PLAN_MODE_TOOL_NAME, EnterPlanModeTool),
    ):
        if any(tool_matches_name(tool, name) for tool in existing):
            continue
        tool = tool_cls()
        tool.bind_runtime_info(backend=BackendType.META, session_name="agent")
        tools.append(tool)
        existing.append(tool)


def append_ask_user_question_tool(tools: list[Any]) -> None:
    from openspace.agents.agent_tool_utils import ASK_USER_QUESTION_TOOL_NAME, tool_matches_name
    from openspace.tools.ask_user_tool import AskUserQuestionTool

    if any(tool_matches_name(tool, ASK_USER_QUESTION_TOOL_NAME) for tool in tools):
        return
    tool = AskUserQuestionTool()
    tool.bind_runtime_info(backend=BackendType.META, session_name="agent")
    tools.append(tool)


def append_multi_agent_control_tools(tools: list[Any]) -> None:
    from openspace.agents.agent_tool_utils import tool_matches_name
    from openspace.tools.task_tools import (
        TASK_GET_TOOL_NAME,
        TASK_LIST_TOOL_NAME,
        TASK_STOP_TOOL_NAME,
        TaskGetTool,
        TaskListTool,
        TaskStopTool,
    )
    from openspace.tools.team_tools import (
        SEND_MESSAGE_TOOL_NAME,
        TEAM_CREATE_TOOL_NAME,
        TEAM_DELETE_TOOL_NAME,
        SendMessageTool,
        TeamCreateTool,
        TeamDeleteTool,
    )

    existing = list(tools)
    for name, tool_cls in (
        (TASK_GET_TOOL_NAME, TaskGetTool),
        (TASK_LIST_TOOL_NAME, TaskListTool),
        (TASK_STOP_TOOL_NAME, TaskStopTool),
        (SEND_MESSAGE_TOOL_NAME, SendMessageTool),
        (TEAM_CREATE_TOOL_NAME, TeamCreateTool),
        (TEAM_DELETE_TOOL_NAME, TeamDeleteTool),
    ):
        if any(tool_matches_name(tool, name) for tool in existing):
            continue
        tool = tool_cls()
        tool.bind_runtime_info(backend=BackendType.META, session_name="agent")
        tools.append(tool)
        existing.append(tool)


def bind_agent_tools_to_context(
    tools: Iterable[Any],
    tool_use_context: ToolUseContext,
) -> None:
    from openspace.agents.agent_tool_utils import (
        AGENT_TOOL_NAME,
        CONFIG_TOOL_NAME,
        ENTER_PLAN_MODE_TOOL_NAME,
        EXIT_PLAN_MODE_TOOL_NAME,
        SEND_MESSAGE_TOOL_NAME,
        TASK_GET_TOOL_NAME,
        TASK_LIST_TOOL_NAME,
        TASK_STOP_TOOL_NAME,
        TODO_WRITE_TOOL_NAME,
        tool_matches_name,
    )
    from openspace.tools.brief_tool import BRIEF_TOOL_NAME
    from openspace.tools.lsp_tool import LSP_TOOL_NAME
    from openspace.tools.schedule_cron_tool import (
        SCHEDULE_CRON_CREATE_TOOL_NAME,
        SCHEDULE_CRON_DELETE_TOOL_NAME,
        SCHEDULE_CRON_LIST_TOOL_NAME,
    )
    from openspace.tools.sleep_tool import SLEEP_TOOL_NAME
    from openspace.tools.team_tools import TEAM_CREATE_TOOL_NAME, TEAM_DELETE_TOOL_NAME

    for tool in tools:
        if not (
            tool_matches_name(tool, AGENT_TOOL_NAME)
            or tool_matches_name(tool, CONFIG_TOOL_NAME)
            or tool_matches_name(tool, TASK_GET_TOOL_NAME)
            or tool_matches_name(tool, TASK_LIST_TOOL_NAME)
            or tool_matches_name(tool, TASK_STOP_TOOL_NAME)
            or tool_matches_name(tool, SEND_MESSAGE_TOOL_NAME)
            or tool_matches_name(tool, TEAM_CREATE_TOOL_NAME)
            or tool_matches_name(tool, TEAM_DELETE_TOOL_NAME)
            or tool_matches_name(tool, TODO_WRITE_TOOL_NAME)
            or tool_matches_name(tool, SLEEP_TOOL_NAME)
            or tool_matches_name(tool, BRIEF_TOOL_NAME)
            or tool_matches_name(tool, SCHEDULE_CRON_CREATE_TOOL_NAME)
            or tool_matches_name(tool, SCHEDULE_CRON_DELETE_TOOL_NAME)
            or tool_matches_name(tool, SCHEDULE_CRON_LIST_TOOL_NAME)
            or tool_matches_name(tool, LSP_TOOL_NAME)
            or tool_matches_name(tool, ENTER_PLAN_MODE_TOOL_NAME)
            or tool_matches_name(tool, EXIT_PLAN_MODE_TOOL_NAME)
        ):
            continue
        setter = getattr(tool, "set_context", None)
        if callable(setter):
            setter(tool_use_context)


def mcp_servers_from_tools(tools: Iterable[Any]) -> list[str]:
    servers: set[str] = set()
    for tool in tools:
        name = str(getattr(tool, "name", "") or "")
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


def find_tool_by_name(
    tool_name: str,
    *,
    tool_map: dict[str, Any] | None = None,
    tools: list[Any] | None = None,
) -> Any | None:
    if tool_map and tool_name in tool_map:
        return tool_map[tool_name]

    for tool in tools or []:
        aliases = getattr(tool, "aliases", []) or []
        if getattr(tool, "name", None) == tool_name or tool_name in aliases:
            return tool
    return None


def build_iteration_tool_results(
    *,
    tool_calls: list[dict[str, Any]],
    tool_map: dict[str, Any],
    result_messages: list[dict[str, Any]],
    tools: list[Any],
) -> list[dict[str, Any]]:
    tool_result_by_id: dict[str, dict[str, Any]] = {}
    for message in result_messages:
        if message.get("role") != "tool":
            continue
        tool_call_id = str(message.get("tool_call_id") or "")
        if not tool_call_id:
            continue
        tool_result_by_id.setdefault(tool_call_id, message)

    iteration_results: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        tool_call_id = str(tool_call.get("id") or "")
        tool_name = str((tool_call.get("function") or {}).get("name") or "unknown")
        result_message = tool_result_by_id.get(tool_call_id, {})
        result_meta = result_message.get("_meta")
        if not isinstance(result_meta, dict):
            result_meta = {}

        tool_obj = find_tool_by_name(tool_name, tool_map=tool_map, tools=tools)
        runtime_info = getattr(tool_obj, "runtime_info", None)
        backend = getattr(
            getattr(runtime_info, "backend", None),
            "value",
            getattr(getattr(tool_obj, "backend_type", None), "value", None),
        )
        status = str(result_meta.get("status") or "unknown")
        content = result_message.get("content")
        error = None
        if status in {"error", "denied", "cancelled"} and isinstance(content, str):
            error = content.removeprefix("Error: ").strip() or content

        iteration_results.append(
            {
                "tool_call": copy.deepcopy(tool_call),
                "tool_name": tool_name,
                "backend": backend,
                "server_name": getattr(runtime_info, "server_name", None),
                "status": status,
                "content": content,
                "error": error,
                "execution_time": result_meta.get("execution_time"),
                "metadata": copy.deepcopy(result_meta.get("tool_result_metadata") or {}),
            }
        )

    return iteration_results


def resolve_memory_mode(value: Any = None) -> str:
    try:
        from openspace.services.memory.daily_log import get_memory_mode

        return get_memory_mode(str(value) if value is not None else None)
    except Exception:
        raw = str(value or "").strip().lower()
        return "daily_log" if raw in {"daily_log", "daily-log", "dailylog", "logs"} else "direct"


def with_memory_mode_tools(tools: list[Any], memory_mode: str) -> list[Any]:
    if memory_mode != "daily_log":
        return list(tools)
    selected = list(tools)
    if any(getattr(tool, "name", None) == "memory_log" for tool in selected):
        return selected
    try:
        from openspace.services.memory.daily_log import MemoryLogTool

        tool = MemoryLogTool()
        tool.bind_runtime_info(
            backend=BackendType.SHELL,
            session_name="memory",
        )
        selected.append(tool)
    except Exception:
        logger.debug("Could not add memory_log tool for daily_log mode", exc_info=True)
    return selected


def skills_disabled_for_context(context: dict[str, Any]) -> bool:
    return bool(context.get("skills_disabled"))


def without_skill_protocol_tools(tools: Iterable[Any]) -> list[Any]:
    try:
        from openspace.skill_engine.protocol import (
            DISCOVER_SKILLS_TOOL_NAME,
            SKILL_TOOL_NAME,
            tool_matches_name as skill_tool_matches_name,
        )
    except Exception:
        return list(tools)

    filtered: list[Any] = []
    for tool in tools:
        if (
            skill_tool_matches_name(tool, SKILL_TOOL_NAME)
            or skill_tool_matches_name(tool, DISCOVER_SKILLS_TOOL_NAME)
        ):
            continue
        filtered.append(tool)
    return filtered


def deferred_tool_names(
    tools: list[Any],
    *,
    discovered_tool_names: Iterable[str] = (),
) -> list[str]:
    discovered = {str(name) for name in discovered_tool_names}
    return sorted(
        {
            tool.name
            for tool in tools
            if getattr(tool, "is_deferred", False)
            and getattr(tool, "name", None)
            and tool.name not in discovered
        }
    )


def build_active_tools(
    tools: list[Any],
    *,
    discovered_tool_names: Iterable[str] = (),
    active_tool_names: Iterable[str] | None = None,
    deferred_tool_names: Iterable[str] | None = None,
) -> list[Any]:
    from openspace.grounding.core.tool_discovery import (
        TOOL_DISCOVERY_TOOL_NAME,
        ToolSearchTool,
    )

    discovered = {str(name) for name in discovered_tool_names}
    hard_active = (
        {str(name) for name in active_tool_names}
        if active_tool_names is not None
        else None
    )
    hard_deferred = {str(name) for name in (deferred_tool_names or ())}

    if hard_active is None:
        active = [
            tool
            for tool in tools
            if not getattr(tool, "is_deferred", False)
            or getattr(tool, "name", None) in discovered
        ]
        remaining_deferred = [
            tool
            for tool in tools
            if getattr(tool, "is_deferred", False)
            and getattr(tool, "name", None) not in discovered
        ]
    else:
        active = [
            tool
            for tool in tools
            if (
                getattr(tool, "name", None) in hard_active
                or getattr(tool, "name", None) in discovered
            )
        ]
        remaining_deferred = [
            tool
            for tool in tools
            if (
                getattr(tool, "name", None) in hard_deferred
                and getattr(tool, "name", None) not in discovered
            )
        ]
    active_names = {getattr(tool, "name", "") for tool in active}
    if remaining_deferred and TOOL_DISCOVERY_TOOL_NAME not in active_names:
        active.append(ToolSearchTool(all_tools=tools))
    return active


def scoped_tool_backends(agent: Any) -> list[BackendType]:
    return [BackendType(name) for name in getattr(agent, "_backend_scope", [])]


def with_skill_protocol_tools(
    agent: Any,
    tools: list[Any],
    backends: list[BackendType],
) -> list[Any]:
    registry = getattr(agent, "_skill_registry", None)
    if not (registry and registry.list_skills()):
        return tools

    from openspace.skill_engine.protocol import (
        DISCOVER_SKILLS_TOOL_NAME,
        SKILL_TOOL_NAME,
        DiscoverSkillsTool,
        SkillDiscoveryService,
        SkillTool,
    )

    existing_names = {getattr(tool, "name", "") for tool in tools}
    if SKILL_TOOL_NAME not in existing_names:
        skill_tool = SkillTool(
            registry,
            skill_store=getattr(agent, "_skill_store", None),
        )
        skill_tool.bind_runtime_info(
            backend=BackendType.META,
            session_name="internal",
        )
        tools.append(skill_tool)
        existing_names.add(SKILL_TOOL_NAME)
    if (
        getattr(agent, "_skill_discovery_enabled", True)
        and DISCOVER_SKILLS_TOOL_NAME not in existing_names
    ):
        discover_tool = DiscoverSkillsTool(
            SkillDiscoveryService(
                registry,
                store=getattr(agent, "_skill_store", None),
                llm_client=(
                    getattr(agent, "_skill_selection_llm", None)
                    or getattr(agent, "_tool_retrieval_llm", None)
                    or getattr(agent, "_llm_client", None)
                ),
            )
        )
        discover_tool.bind_runtime_info(
            backend=BackendType.META,
            session_name="internal",
        )
        tools.append(discover_tool)
        existing_names.add(DISCOVER_SKILLS_TOOL_NAME)

    logger.info("Added Skill Protocol tools")
    return tools


async def get_tools_without_auto_preselection(agent: Any) -> list[Any]:
    grounding_client = agent.grounding_client
    if not grounding_client:
        return []
    backends = scoped_tool_backends(agent)
    tools = await grounding_client.list_tools(
        backend=backends,
        use_cache=True,
    )
    return with_skill_protocol_tools(agent, list(tools), backends)


async def get_available_tools(agent: Any, task_description: str | None) -> list[Any]:
    grounding_client = agent.grounding_client
    if not grounding_client:
        return []

    backends = scoped_tool_backends(agent)

    try:
        retrieval_llm = getattr(agent, "_tool_retrieval_llm", None) or getattr(agent, "_llm_client", None)
        tools = await grounding_client.get_tools_with_auto_preselection(
            task_description=task_description,
            backend=backends,
            use_cache=True,
            llm_callable=retrieval_llm,
        )
        logger.info(
            "GroundingAgent selected %d tools (auto-preselection preload) from %d backends",
            len(tools),
            len(backends),
        )
    except Exception as e:
        logger.warning("Auto-search tools failed, falling back to full list: %s", e)
        tools = await load_all_tools(agent, grounding_client)

    return with_skill_protocol_tools(agent, tools, backends)


async def get_tool_universe(agent: Any, preselected_tools: list[Any]) -> list[Any]:
    grounding_client = agent.grounding_client
    if not grounding_client:
        return list(preselected_tools)

    backends = [BackendType(name) for name in getattr(agent, "_backend_scope", [])]
    try:
        tools = await grounding_client.list_tools(
            backend=backends,
            use_cache=True,
        )
    except Exception as exc:
        logger.debug("Failed to load full tool universe; using preselected tools: %s", exc)
        tools = list(preselected_tools)

    seen = {getattr(tool, "name", "") for tool in tools}
    for tool in preselected_tools:
        name = getattr(tool, "name", "")
        if name and name not in seen:
            tools.append(tool)
            seen.add(name)
    return tools


async def load_all_tools(agent: Any, grounding_client: Any) -> list[Any]:
    all_tools: list[Any] = []
    for backend_name in getattr(agent, "_backend_scope", []):
        try:
            backend_type = BackendType(backend_name)
            tools = await grounding_client.list_tools(backend=backend_type)
            all_tools.extend(tools)
            logger.debug("Retrieved %d tools from backend: %s", len(tools), backend_name)
        except Exception as e:
            logger.debug("Could not get tools from %s: %s", backend_name, e)

    logger.info(
        "GroundingAgent fallback retrieved %d tools from %d backends",
        len(all_tools),
        len(getattr(agent, "_backend_scope", [])),
    )
    return all_tools


def build_retrieved_tools_list(
    tools: list[Any],
    preselection_debug_info: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    retrieved: list[dict[str, Any]] = []
    for tool in tools:
        tool_info: dict[str, Any] = {
            "name": getattr(tool, "name", str(tool)),
            "description": getattr(tool, "description", ""),
        }
        runtime_info = getattr(tool, "_runtime_info", None)
        if runtime_info and hasattr(runtime_info, "backend"):
            tool_info["backend"] = (
                runtime_info.backend.value
                if hasattr(runtime_info.backend, "value")
                else str(runtime_info.backend)
            )
            tool_info["server_name"] = runtime_info.server_name
        elif hasattr(tool, "backend_type"):
            tool_info["backend"] = (
                tool.backend_type.value
                if hasattr(tool.backend_type, "value")
                else str(tool.backend_type)
            )
        if preselection_debug_info and preselection_debug_info.get("tool_scores"):
            for score_info in preselection_debug_info["tool_scores"]:
                if score_info["name"] == tool_info["name"]:
                    tool_info["similarity_score"] = score_info["score"]
                    if "quality_adjusted_score" in score_info:
                        tool_info["quality_adjusted_score"] = score_info[
                            "quality_adjusted_score"
                        ]
                    if "quality_penalty" in score_info:
                        tool_info["quality_penalty"] = score_info["quality_penalty"]
                    break
        retrieved.append(tool_info)
    return retrieved


__all__ = [
    "append_ask_user_question_tool",
    "append_config_tool",
    "append_lsp_tool",
    "append_multi_agent_control_tools",
    "append_plan_mode_tools",
    "append_schedule_cron_tools",
    "append_sleep_and_brief_tools",
    "append_todo_write_tool",
    "bind_agent_tools_to_context",
    "build_active_tools",
    "build_iteration_tool_results",
    "build_retrieved_tools_list",
    "deferred_tool_names",
    "filter_tools_for_permission_mode",
    "find_tool_by_name",
    "get_agent_definition_registry",
    "get_available_tools",
    "get_tool_universe",
    "get_tools_without_auto_preselection",
    "load_all_tools",
    "mcp_servers_from_tools",
    "resolve_agent_definitions",
    "resolve_async_agent",
    "resolve_memory_mode",
    "resolve_permission_context",
    "resolve_tui_available",
    "scoped_tool_backends",
    "should_append_agent_tools",
    "skills_disabled_for_context",
    "tool_set_signature",
    "with_agent_tool",
    "with_memory_mode_tools",
    "with_skill_protocol_tools",
    "without_skill_protocol_tools",
]
