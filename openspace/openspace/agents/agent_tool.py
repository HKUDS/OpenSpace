from __future__ import annotations

import asyncio
import copy
import os
import time
import uuid
from dataclasses import replace
from typing import Any, Mapping

from openspace.agents.agent_definitions import (
    AgentDefinition,
    AgentDefinitionRegistry,
    AgentDefinitionsResult,
    filter_agents_by_mcp_requirements,
)
from openspace.agents.agent_tool_utils import (
    AGENT_TOOL_NAME,
    AgentToolResult,
    filter_denied_agents,
    finalize_agent_result,
    format_agent_line,
    format_agent_tool_result,
    resolve_agent_tools,
    should_inject_agent_list_in_messages,
    tool_matches_name,
)
from openspace.agents.built_in_agents import BASH_TOOL_NAME, FILE_READ_TOOL_NAME
from openspace.agents.task_manager import AgentTask, TaskManager, TaskType
from openspace.grounding.core.permissions.types import PermissionAllow
from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class AgentTool(BaseTool):
    """Launch a subagent to handle a complex task.

    Implementation notes: ``tools/AgentTool/AgentTool.tsx``.  This OS port keeps the
    core engine path in-process: select an ``AgentDefinition``, filter the
    tool pool, run ``GroundingAgent.process()`` with ``pre_filtered_tools``,
    and map the result back into a single tool result for the parent model.
    """

    _name = AGENT_TOOL_NAME
    _description = "Launch a new agent"
    backend_type = BackendType.META
    _is_read_only = True
    _is_concurrency_safe = True
    max_result_size_chars = 100_000
    aliases = ["Task"]
    search_hint = "delegate work to a subagent"
    parameter_descriptions = {
        "name": (
            "Optional teammate name. When combined with team_name or an active "
            "TeamCreate team, launches an in-process teammate."
        ),
        "team_name": "Optional team name for teammate spawning.",
        "description": "A short (3-5 word) description of the task.",
        "prompt": "The task for the agent to perform.",
        "subagent_type": (
            "The type of specialized agent to use. If omitted, the "
            "general-purpose agent is used."
        ),
        "model": (
            "Optional model override for this agent. Takes precedence over "
            "the agent definition model. If omitted, inherits from the parent."
        ),
        "run_in_background": (
            "Set to true to run this agent in the background. A lightweight "
            "task handle and output file are returned."
        ),
    }

    def __init__(
        self,
        *,
        registry: AgentDefinitionRegistry | None = None,
        parent_agent: Any | None = None,
        grounding_client: Any | None = None,
        llm_client: Any | None = None,
        orchestrator: Any | None = None,
    ) -> None:
        self._registry = registry or AgentDefinitionRegistry()
        self._parent_agent = parent_agent
        self._grounding_client = grounding_client
        self._llm_client = llm_client
        self._orchestrator = orchestrator
        self._current_context: Any | None = None
        super().__init__(verbose=False, handle_errors=False)

    def set_context(self, context: Any) -> None:
        self._current_context = context

    def get_prompt(self, context: Any = None) -> str:
        active_agents = self._active_agents_for_prompt(context)
        list_via_attachment = should_inject_agent_list_in_messages()
        if list_via_attachment:
            agent_list = (
                "Available agent types are listed in <system-reminder> "
                "messages in the conversation."
            )
        else:
            agent_list = (
                "Available agent types and the tools they have access to:\n"
                + "\n".join(format_agent_line(agent) for agent in active_agents)
            )

        concurrency_note = (
            "- Launch multiple agents concurrently whenever possible by using "
            "a single assistant message with multiple Agent tool calls.\n"
        )
        return (
            "Launch a new agent to handle complex, multi-step tasks autonomously.\n\n"
            "The Agent tool launches specialized agents that work independently "
            "and return a single result message to you.\n\n"
            f"{agent_list}\n\n"
            "When using the Agent tool, specify a subagent_type parameter to "
            "select which agent type to use. If omitted, the general-purpose "
            "agent is used.\n\n"
            "When NOT to use the Agent tool:\n"
            f"- If you want to read a specific file path, use {FILE_READ_TOOL_NAME}.\n"
            "- If you are searching within a known file or a small set of files, "
            f"use {FILE_READ_TOOL_NAME} or grep directly.\n"
            "- Other tasks unrelated to the agent descriptions above.\n\n"
            "Usage notes:\n"
            "- Always include a short description (3-5 words) summarizing what "
            "the agent will do.\n"
            f"{concurrency_note}"
            "- Use foreground execution when you need the result before your "
            "next step. Use run_in_background=true only for independent work.\n"
            "- The agent starts without the parent conversation; provide a "
            "complete briefing with relevant paths, findings, and constraints.\n"
            "- The agent result is not shown to the user automatically; summarize "
            "it yourself when needed."
        )

    async def check_permissions(
        self,
        input: dict[str, Any],
        context: Any = None,
    ):
        return PermissionAllow(updated_input=input)

    async def _arun(
        self,
        prompt: str,
        description: str,
        name: str | None = None,
        team_name: str | None = None,
        subagent_type: str | None = None,
        model: str | None = None,
        run_in_background: bool = False,
    ) -> ToolResult:
        if not prompt or not str(prompt).strip():
            return ToolResult(
                status=ToolStatus.ERROR,
                error="Agent prompt is required",
                content="Error: Agent prompt is required",
            )

        context = self._current_context
        if context is None:
            return ToolResult(
                status=ToolStatus.ERROR,
                error="AgentTool was not given a ToolUseContext",
                content="Error: AgentTool was not given a ToolUseContext",
            )

        coordinator = getattr(context, "coordinator_mode", None) or getattr(
            self._parent_agent,
            "_coordinator_mode",
            None,
        )
        coordinator_enabled = bool(getattr(context, "coordinator_mode_enabled", False))
        requested_agent_type = subagent_type or "general-purpose"
        if coordinator_enabled and requested_agent_type == "worker":
            requested_agent_type = "general-purpose"

        agent_result = _coerce_agent_definitions_result(
            getattr(context, "agent_definitions", None),
            self._registry,
        )
        selected = self._select_agent(
            agent_result,
            requested_agent_type,
            context,
        )
        if isinstance(selected, ToolResult):
            return selected
        agent_def = selected
        worker_name = name or description or agent_def.agent_type
        if coordinator_enabled and coordinator is not None:
            agent_def = coordinator.prepare_worker_agent_definition(
                agent_def,
                worker_name=worker_name,
            )

        is_async = bool(run_in_background or agent_def.background or coordinator_enabled)
        available_tools = list(
            getattr(context, "coordinator_worker_tools", None)
            or getattr(context, "all_tools", None)
            or getattr(context, "tools", [])
        )
        resolved = resolve_agent_tools(
            agent_def,
            available_tools,
            is_async=is_async,
        )
        filtered_tools = resolved.resolved_tools
        if resolved.allowed_agent_types:
            # Nested Agent metadata is carried only for coordinator/team steps.
            logger.debug(
                "Agent %s resolved allowed nested agent types: %s",
                agent_def.agent_type,
                resolved.allowed_agent_types,
            )

        resolved_model = _resolve_agent_model(model, agent_def.model, context)
        task_description = description or agent_def.description or agent_def.when_to_use

        if (name or team_name) and not coordinator_enabled:
            if not name:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    error="name is required when using Agent as a teammate spawn.",
                    content="Error: name is required when using Agent as a teammate spawn.",
                )
            task_manager = _get_task_manager(context)
            resolved_team = team_name or getattr(task_manager, "active_team_name", None)
            if not resolved_team:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    error=(
                        "team_name is required for teammate spawn. Use "
                        "TeamCreate first or pass team_name."
                    ),
                    content=(
                        "Error: team_name is required for teammate spawn. Use "
                        "TeamCreate first or pass team_name."
                    ),
                )
            orchestrator = self._orchestrator or getattr(
                context,
                "multi_agent_orchestrator",
                None,
            )
            if orchestrator is None:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    error="Agent teammate spawn requires MultiAgentOrchestrator.",
                    content="Error: Agent teammate spawn requires MultiAgentOrchestrator.",
                )
            try:
                spawn_result = await orchestrator.spawn_teammate(
                    name=name,
                    prompt=prompt,
                    parent_context=context,
                    available_tools=list(
                        getattr(context, "coordinator_worker_tools", None)
                        or getattr(context, "all_tools", None)
                        or getattr(context, "tools", [])
                    ),
                    team_name=str(resolved_team),
                    agent_type=agent_def.agent_type,
                    model=resolved_model,
                    description=task_description,
                )
            except Exception as exc:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    error=str(exc),
                    content=f"Error: {exc}",
                )
            data = {"status": "teammate_spawned", **spawn_result.to_dict()}
            return ToolResult(
                status=ToolStatus.SUCCESS,
                content=_format_teammate_spawn_result(data),
                metadata={"tool": self.name, "agent_result": data},
            )

        if is_async:
            task_manager = _get_task_manager(context)
            if task_manager is None:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    error=(
                        "AgentTool requires a session-scoped TaskManager. "
                        "Initialize MultiAgentOrchestrator before launching "
                        "background agents."
                    ),
                    content=(
                        "Error: AgentTool requires a session-scoped "
                        "TaskManager for background agents."
                    ),
                )
            if coordinator_enabled:
                worker_team_name = team_name or getattr(
                    task_manager,
                    "active_team_name",
                    None,
                )
            else:
                worker_team_name = None

            async def runner(task: AgentTask) -> AgentToolResult:
                spawn_payload = {
                    "agent_id": task.agent_id,
                    "agent_type": agent_def.agent_type,
                    "team_name": worker_team_name,
                    "description": task_description,
                    "status": "running",
                    "background": True,
                    "task_id": task.id,
                    "parent_task_id": getattr(context, "task_id", None),
                    "session_id": getattr(context, "session_id", None),
                }
                await context.emit_event("agent_spawn", spawn_payload)
                await context.emit_event(
                    "agent_event",
                    {
                        "session_id": getattr(context, "session_id", None),
                        "agent_id": task.agent_id,
                        "event": "agent_spawn",
                        "payload": spawn_payload,
                    },
                )
                return await run_agent(
                    agent_def=agent_def,
                    prompt=prompt,
                    filtered_tools=filtered_tools,
                    allowed_agent_types=resolved.allowed_agent_types,
                    parent_context=context,
                    parent_agent=self._parent_agent,
                    grounding_client=self._grounding_client,
                    llm_client=self._llm_client,
                    resolved_model=resolved_model,
                    agent_id=task.agent_id,
                    task_description=task_description,
                    is_async_agent=True,
                    abort_event=task.abort_event,
                    message_source=task.inbox,
                )

            task = await task_manager.register_async_agent(
                runner=runner,
                prompt=prompt,
                description=task_description,
                agent_type=agent_def.agent_type,
                selected_agent=agent_def,
                model=resolved_model,
                parent_abort_event=getattr(context, "abort_event", None),
                parent_task_id=getattr(context, "task_id", None),
                parent_inbox=(
                    getattr(context, "coordinator_notification_queue", None)
                    if coordinator_enabled
                    else None
                ),
                task_type=(
                    TaskType.COORDINATOR_WORKER
                    if coordinator_enabled
                    else TaskType.LOCAL_AGENT
                ),
                team_name=worker_team_name,
            )
            can_read_output_file = any(
                tool_matches_name(tool, FILE_READ_TOOL_NAME)
                or tool_matches_name(tool, BASH_TOOL_NAME)
                for tool in getattr(context, "tools", [])
            )
            data = {
                "status": "async_launched",
                "agent_id": task.agent_id,
                "task_id": task.id,
                "agent_type": agent_def.agent_type,
                "description": task_description,
                "prompt": prompt,
                "output_file": task.output_file,
                "can_read_output_file": can_read_output_file,
            }
            return ToolResult(
                status=ToolStatus.SUCCESS,
                content=format_agent_tool_result(data),
                metadata={"tool": self.name, "agent_result": data},
            )

        agent_id = f"agent_{uuid.uuid4().hex[:12]}"
        spawn_payload = {
            "agent_id": agent_id,
            "agent_type": agent_def.agent_type,
            "description": task_description,
            "status": "starting",
            "background": False,
            "task_id": agent_id,
            "parent_task_id": getattr(context, "task_id", None),
            "session_id": getattr(context, "session_id", None),
        }
        await context.emit_event("agent_spawn", spawn_payload)
        await context.emit_event(
            "agent_event",
            {
                "session_id": getattr(context, "session_id", None),
                "agent_id": agent_id,
                "event": "agent_spawn",
                "payload": spawn_payload,
            },
        )
        result = await run_agent(
            agent_def=agent_def,
            prompt=prompt,
            filtered_tools=filtered_tools,
            allowed_agent_types=resolved.allowed_agent_types,
            parent_context=context,
            parent_agent=self._parent_agent,
            grounding_client=self._grounding_client,
            llm_client=self._llm_client,
            resolved_model=resolved_model,
            agent_id=agent_id,
            task_description=task_description,
        )
        return ToolResult(
            status=ToolStatus.SUCCESS if result.status == "completed" else ToolStatus.ERROR,
            content=format_agent_tool_result(result),
            error=None if result.status == "completed" else format_agent_tool_result(result),
            metadata={"tool": self.name, "agent_result": _agent_result_to_dict(result)},
        )

    def _active_agents_for_prompt(self, context: Any = None) -> list[AgentDefinition]:
        agent_result = _coerce_agent_definitions_result(
            getattr(context, "agent_definitions", None) if context is not None else None,
            self._registry,
        )
        agents = list(agent_result.active_agents)
        if context is not None:
            agents = filter_agents_by_mcp_requirements(
                agents,
                _mcp_servers_from_context_tools(context),
            )
            agents = filter_denied_agents(
                agents,
                getattr(context, "permission_context", None),
            )
        allowed = getattr(agent_result, "allowed_agent_types", None)
        if allowed:
            allowed_set = set(allowed)
            agents = [agent for agent in agents if agent.agent_type in allowed_set]
        return agents

    def _select_agent(
        self,
        agent_result: AgentDefinitionsResult,
        agent_type: str,
        context: Any,
    ) -> AgentDefinition | ToolResult:
        all_agents = list(agent_result.active_agents)
        allowed = set(agent_result.allowed_agent_types or [])
        if allowed:
            candidates = [agent for agent in all_agents if agent.agent_type in allowed]
        else:
            candidates = all_agents

        mcp_servers = _mcp_servers_from_context_tools(context)
        candidates = filter_agents_by_mcp_requirements(candidates, mcp_servers)
        candidates = filter_denied_agents(
            candidates,
            getattr(context, "permission_context", None),
        )

        for agent in candidates:
            if agent.agent_type == agent_type:
                return agent

        if any(agent.agent_type == agent_type for agent in all_agents):
            return ToolResult(
                status=ToolStatus.ERROR,
                error=f"Agent type '{agent_type}' is not available in this context.",
                content=f"Error: Agent type '{agent_type}' is not available in this context.",
            )
        available = ", ".join(agent.agent_type for agent in candidates) or "none"
        return ToolResult(
            status=ToolStatus.ERROR,
            error=f"Agent type '{agent_type}' not found. Available agents: {available}",
            content=f"Error: Agent type '{agent_type}' not found. Available agents: {available}",
        )


async def run_agent(
    *,
    agent_def: AgentDefinition,
    prompt: str,
    filtered_tools: list[BaseTool],
    parent_context: Any,
    parent_agent: Any | None,
    grounding_client: Any | None,
    llm_client: Any | None,
    resolved_model: str,
    agent_id: str | None = None,
    task_description: str | None = None,
    allowed_agent_types: list[str] | None = None,
    is_async_agent: bool = False,
    child_context_modifier: Any | None = None,
    abort_event: asyncio.Event | None = None,
    message_source: Any | None = None,
) -> AgentToolResult:
    """Run a foreground subagent with the same GroundingAgent engine."""

    from openspace.agents.grounding_agent import GroundingAgent

    start = time.time()
    agent_id = agent_id or f"agent_{uuid.uuid4().hex[:12]}"
    child_llm = _clone_llm_client(llm_client, resolved_model)

    system_prompt = agent_def.system_prompt(tool_use_context=parent_context)
    if agent_def.critical_system_reminder:
        system_prompt = f"{system_prompt}\n\n{agent_def.critical_system_reminder}"

    child = GroundingAgent(
        name=f"agent:{agent_def.agent_type}:{agent_id}",
        backend_scope=agent_def.backend_scope,
        llm_client=child_llm,
        grounding_client=grounding_client,
        recording_manager=getattr(parent_context, "recording_manager", None),
        system_prompt=system_prompt,
        max_iterations=agent_def.max_turns or getattr(parent_context, "max_iterations", 50) or 50,
        tool_retrieval_llm=getattr(parent_agent, "_tool_retrieval_llm", None),
        skill_selection_llm=getattr(parent_agent, "_skill_selection_llm", None),
    )
    child.set_runtime_event_sink(getattr(parent_context, "event_sink", None))
    if hasattr(parent_agent, "_skill_registry"):
        child.set_skill_registry(getattr(parent_agent, "_skill_registry", None))
    elif getattr(parent_context, "skill_registry", None) is not None:
        child.set_skill_registry(getattr(parent_context, "skill_registry", None))
    child._skill_store = getattr(parent_context, "skill_store", None)
    if parent_agent is not None:
        child._skill_store = getattr(parent_agent, "_skill_store", None)
        child.set_skill_protocol_settings(
            listing_enabled=getattr(parent_agent, "_skill_listing_enabled", True),
            discovery_enabled=getattr(parent_agent, "_skill_discovery_enabled", True),
            discovery_max_results=getattr(parent_agent, "_skill_discovery_max_results", 5),
            listing_budget_context_percent=getattr(
                parent_agent,
                "_skill_listing_budget_context_percent",
                0.01,
            ),
            listing_max_description_chars=getattr(
                parent_agent,
                "_skill_listing_max_description_chars",
                250,
            ),
            post_tool_query_builder_enabled=getattr(
                parent_agent,
                "_post_tool_query_builder_enabled",
                False,
            ),
            post_tool_query_builder_model=getattr(
                parent_agent,
                "_post_tool_query_builder_model",
                None,
            ),
            post_tool_query_builder_max_chars=getattr(
                parent_agent,
                "_post_tool_query_builder_max_chars",
                4000,
            ),
        )

    child_is_async_agent = bool(
        is_async_agent or getattr(parent_context, "is_async_agent", False)
    )
    parent_permission_context = getattr(parent_context, "permission_context", None)
    parent_base_permission_context = (
        getattr(parent_context, "base_permission_context", None)
        or parent_permission_context
    )
    parent_permission_mode = str(
        getattr(
            parent_permission_context,
            "mode",
            getattr(parent_context, "permission_mode", "default"),
        )
        or "default"
    )
    protected_parent_modes = {"acceptEdits", "bypassPermissions", "auto"}
    requested_permission_mode = agent_def.permission_mode
    effective_permission_mode = (
        parent_permission_mode
        if parent_permission_mode in protected_parent_modes
        else (requested_permission_mode or parent_permission_mode)
    )

    def _with_permission_mode(permission_context: Any | None) -> Any | None:
        if permission_context is None:
            return None
        if getattr(permission_context, "mode", None) == effective_permission_mode:
            return permission_context
        with_mode = getattr(permission_context, "with_mode", None)
        if callable(with_mode):
            try:
                return with_mode(effective_permission_mode)
            except Exception:
                return permission_context
        return permission_context

    child_context = {
        "instruction": prompt,
        "workspace_dir": getattr(parent_context, "cwd", "."),
        "agent_id": agent_id,
        "agent_type": agent_def.agent_type,
        "task_id": agent_id,
        "parent_task_id": (
            getattr(parent_context, "task_id", None)
            or getattr(parent_context, "parent_task_id", None)
        ),
        "session_id": getattr(parent_context, "session_id", None),
        "session_dir": getattr(parent_context, "session_dir", None),
        "memory_mode": getattr(parent_context, "memory_mode", "direct"),
        "pre_filtered_tools": list(filtered_tools),
        "all_tools": list(filtered_tools),
        "abort_event": abort_event or getattr(parent_context, "abort_event", None),
        "message_source": message_source,
        "read_file_state": copy.copy(getattr(parent_context, "read_file_state", {}) or {}),
        "todo_state": getattr(parent_context, "todo_state", None),
        "nested_memory_triggers": set(),
        "loaded_nested_memory_paths": set(),
        "dynamic_skill_path_triggers": set(),
        "sent_dynamic_skill_keys": set(
            getattr(parent_context, "sent_dynamic_skill_keys", set()) or set()
        ),
        "path_activated_skill_names": set(
            getattr(parent_context, "path_activated_skill_names", set()) or set()
        ),
        "sent_skill_names_by_agent": {
            str(agent): set(names or ())
            for agent, names in (
                getattr(parent_context, "sent_skill_names_by_agent", {}) or {}
            ).items()
        },
        "discovered_skill_names": set(
            getattr(parent_context, "discovered_skill_names", set()) or set()
        ),
        "invoked_skills_by_agent": {
            str(agent): list(records or [])
            for agent, records in (
                getattr(parent_context, "invoked_skills_by_agent", {}) or {}
            ).items()
        },
        "skill_listing_suppressed_once": bool(
            getattr(parent_context, "skill_listing_suppressed_once", False)
        ),
        "active_skill_scopes": _clone_active_skill_scopes_for_child(
            getattr(parent_context, "active_skill_scopes", {}) or {}
        ),
        "skill_model_override": getattr(parent_context, "skill_model_override", None),
        "skill_effort_override": getattr(parent_context, "skill_effort_override", None),
        "initial_tool_use_context_modifier": child_context_modifier,
        "permission_engine": getattr(parent_context, "permission_engine", None),
        "permission_mode": effective_permission_mode,
        "permission_context": _with_permission_mode(parent_permission_context),
        "base_permission_context": _with_permission_mode(parent_base_permission_context),
        "event_sink": getattr(parent_context, "event_sink", None),
        "recording_manager": getattr(parent_context, "recording_manager", None),
        "quality_manager": getattr(parent_context, "quality_manager", None),
        "cost_tracker": getattr(parent_context, "cost_tracker", None),
        "hook_registry": getattr(parent_context, "hook_registry", None),
        "tui_available": bool(getattr(parent_context, "tui_available", False))
        and not child_is_async_agent,
        "is_async_agent": child_is_async_agent,
        "max_iterations": agent_def.max_turns or getattr(parent_context, "max_iterations", 50) or 50,
        "task_description": task_description or prompt,
        "task_manager": getattr(parent_context, "task_manager", None),
        "multi_agent_orchestrator": getattr(parent_context, "multi_agent_orchestrator", None),
        "coordinator_mode": getattr(parent_context, "coordinator_mode", None),
        "coordinator_mode_enabled": bool(
            getattr(parent_context, "coordinator_mode_enabled", False)
        ),
        "coordinator_notification_queue": getattr(
            parent_context,
            "coordinator_notification_queue",
            None,
        ),
        "coordinator_worker_tools": list(
            getattr(parent_context, "coordinator_worker_tools", ()) or ()
        ),
        "agent_definitions": getattr(parent_context, "agent_definitions", None),
        "allowed_agent_types": allowed_agent_types or agent_def.allowed_agent_types,
        "session_storage": getattr(parent_context, "session_storage", None),
        "tool_results_dir": getattr(parent_context, "tool_results_dir", None),
        "file_history": getattr(parent_context, "file_history", None),
    }

    try:
        process_result = await child.process(child_context)
        try:
            from openspace.skill_engine.protocol import restore_skill_state_from_messages

            restore_skill_state_from_messages(
                process_result.get("messages") or [],
                parent_context,
            )
        except Exception:
            pass
        status = str(process_result.get("status") or "")
        if status not in {"success", "completed"}:
            messages = process_result.get("messages") or []
            if messages:
                try:
                    partial = finalize_agent_result(
                        messages=messages,
                        agent_id=agent_id,
                        agent_type=agent_def.agent_type,
                        prompt=prompt,
                        start_time=start,
                    )
                    partial.status = "completed" if status == "success" else "error"
                    return partial
                except Exception:
                    pass
            return AgentToolResult(
                agent_id=agent_id,
                agent_type=agent_def.agent_type,
                content=[{"type": "text", "text": str(process_result.get("response") or process_result.get("error") or status)}],
                total_tool_use_count=len(process_result.get("tool_executions") or []),
                total_duration_ms=max(0, int((time.time() - start) * 1000)),
                total_tokens=0,
                usage={},
                status="error",
                prompt=prompt,
            )

        return finalize_agent_result(
            messages=process_result.get("messages") or [],
            agent_id=agent_id,
            agent_type=agent_def.agent_type,
            prompt=prompt,
            start_time=start,
        )
    except asyncio.CancelledError:
        return AgentToolResult(
            agent_id=agent_id,
            agent_type=agent_def.agent_type,
            content=[{"type": "text", "text": "Subagent stopped."}],
            total_tool_use_count=0,
            total_duration_ms=max(0, int((time.time() - start) * 1000)),
            total_tokens=0,
            usage={},
            status="stopped",
            prompt=prompt,
        )
    except Exception as exc:
        return AgentToolResult(
            agent_id=agent_id,
            agent_type=agent_def.agent_type,
            content=[{"type": "text", "text": str(exc)}],
            total_tool_use_count=0,
            total_duration_ms=max(0, int((time.time() - start) * 1000)),
            total_tokens=0,
            usage={},
            status="error",
            prompt=prompt,
        )
    finally:
        _cleanup_agent_todos(parent_context, agent_id)


def _clone_active_skill_scopes_for_child(scopes: Mapping[str, Any]) -> dict[str, Any]:
    """Copy parent skill scopes without sharing hook registrations.

    Child agents reuse the same HookRegistry object as the parent. If they hold
    the parent's scope objects directly, child cleanup unregisters parent hooks.
    """

    cloned: dict[str, Any] = {}
    for key, scope in scopes.items():
        try:
            cloned[str(key)] = replace(scope, hook_registrations=[])
        except Exception:
            cloned[str(key)] = copy.copy(scope)
            try:
                cloned[str(key)].hook_registrations = []
            except Exception:
                pass
    return cloned


def _cleanup_agent_todos(parent_context: Any, agent_id: str) -> None:
    """Clean up subagent-specific todo entries."""

    todo_state = getattr(parent_context, "todo_state", None)
    if isinstance(todo_state, dict):
        todo_state.pop(agent_id, None)


def build_agent_tool(
    *,
    registry: AgentDefinitionRegistry,
    parent_agent: Any,
    grounding_client: Any,
    llm_client: Any,
    orchestrator: Any | None = None,
) -> AgentTool:
    tool = AgentTool(
        registry=registry,
        parent_agent=parent_agent,
        grounding_client=grounding_client,
        llm_client=llm_client,
        orchestrator=orchestrator,
    )
    tool.bind_runtime_info(
        backend=BackendType.META,
        session_name="agent",
    )
    return tool


def _get_task_manager(context: Any) -> TaskManager | None:
    manager = getattr(context, "task_manager", None)
    if isinstance(manager, TaskManager):
        return manager
    return None


def _coerce_agent_definitions_result(
    value: Any,
    registry: AgentDefinitionRegistry,
) -> AgentDefinitionsResult:
    if isinstance(value, AgentDefinitionsResult):
        return value
    allowed = getattr(value, "allowed_agent_types", None) if value is not None else None
    return registry.result(allowed_agent_types=allowed)


def _resolve_agent_model(
    model_arg: str | None,
    agent_model: str | None,
    context: Any,
) -> str:
    parent_model = str(getattr(context, "model", None) or "")
    for candidate in (model_arg, agent_model, parent_model):
        if not candidate or str(candidate) == "inherit":
            continue
        return _resolve_model_alias(str(candidate), parent_model)
    return parent_model or "unknown"


def _resolve_model_alias(model: str, parent_model: str) -> str:
    alias = model.strip().lower()
    if alias not in {"sonnet", "opus", "haiku"}:
        return model
    env_name = f"OPENSPACE_AGENT_MODEL_{alias.upper()}"
    configured = os.environ.get(env_name)
    if configured and configured.strip():
        return configured.strip()
    return parent_model or model


def _clone_llm_client(llm_client: Any, model: str) -> Any:
    if llm_client is None:
        return None
    try:
        clone = copy.copy(llm_client)
        clone.model = model
        return clone
    except Exception:
        return llm_client


def _mcp_servers_from_context_tools(context: Any) -> list[str]:
    servers: set[str] = set()
    for tool in getattr(context, "all_tools", None) or getattr(context, "tools", []):
        name = getattr(tool, "name", "")
        if name.startswith("mcp__"):
            parts = name.split("__")
            if len(parts) >= 3 and parts[1]:
                servers.add(parts[1])
    return sorted(servers)


def _agent_result_to_dict(result: AgentToolResult | Mapping[str, Any]) -> dict[str, Any]:
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


def _format_teammate_spawn_result(data: Mapping[str, Any]) -> str:
    return (
        "Teammate spawned successfully.\n"
        f"name: {data.get('name')}\n"
        f"team_name: {data.get('team_name')}\n"
        f"agent_id: {data.get('agent_id')}\n"
        f"task_id: {data.get('task_id')}\n"
        "Use SendMessage(to_agent/name/task_id, message) to send instructions, "
        "TaskGet(task_id=...) to read output, TaskList to inspect tasks, and "
        "TaskStop(task_id=...) to stop it."
    )


__all__ = [
    "AgentTool",
    "build_agent_tool",
    "run_agent",
]
