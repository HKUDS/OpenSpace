from __future__ import annotations

import asyncio
import json
import inspect
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Union

import openspace.agents.skill_context as agent_skill_context
import openspace.agents.tool_inventory as agent_tool_inventory
from openspace.agents.base import BaseAgent
from openspace.agents.turns import message_builder as agent_message_builder
from openspace.agents.turns import stop_policy as agent_stop_policy
from openspace.grounding.core.types import BackendType
from openspace.llm.types import ModelResponse
from openspace.services.tooling.context import ToolUseContext
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace.llm import LLMClient
    from openspace.grounding.core.grounding_client import GroundingClient
    from openspace.recording import RecordingManager
    from openspace.skill_engine import SkillRegistry

logger = Logger.get_logger(__name__)

# OpenSpace query.ts L164 — cap on max_output_tokens recovery attempts
MAX_OUTPUT_TOKENS_RECOVERY_LIMIT: int = 3


class GroundingAgent(BaseAgent):

    def __init__(
        self,
        name: str = "GroundingAgent",
        backend_scope: Optional[List[str]] = None,
        llm_client: Optional[LLMClient] = None,
        grounding_client: Optional[GroundingClient] = None,
        recording_manager: Optional[RecordingManager] = None,
        system_prompt: Optional[str] = None,
        max_iterations: int = 15,
        tool_retrieval_llm: Optional[LLMClient] = None,
        skill_selection_llm: Optional[LLMClient] = None,
        enable_turn0_llm_skill_selector: bool = True,
    ) -> None:
        """
        Initialize the Grounding Agent.
        
        Args:
            name: Agent name
            backend_scope: List of backends this agent can access (None = all available)
            llm_client: LLM client for reasoning
            grounding_client: GroundingClient for tool execution
            recording_manager: RecordingManager for recording execution
            system_prompt: Custom system prompt
            max_iterations: Maximum LLM reasoning iterations for self-correction
            tool_retrieval_llm: LLM client for tool retrieval filter (None = use llm_client)
        """
        super().__init__(
            name=name,
            backend_scope=backend_scope or ["gui", "shell", "mcp", "web", "meta"],
            llm_client=llm_client,
            grounding_client=grounding_client,
            recording_manager=recording_manager
        )
       
        # Skill registry for Skill Protocol tools.
        self._skill_registry: Optional["SkillRegistry"] = None
        self._skill_discovery_enabled: bool = True
        self._skill_listing_enabled: bool = True
        self._skill_discovery_max_results: int = 5
        self._skill_listing_budget_context_percent: float = 0.01
        self._skill_listing_max_description_chars: int = 250
        self._post_tool_query_builder_enabled: bool = False
        self._post_tool_query_builder_model: Optional[str] = None
        self._post_tool_query_builder_max_chars: int = 4000

        self._custom_system_prompt = system_prompt
        self._system_prompt = system_prompt or self._default_system_prompt()
        self._max_iterations = max_iterations
        self._tool_retrieval_llm = tool_retrieval_llm
        self._skill_selection_llm = skill_selection_llm
        self._enable_turn0_llm_skill_selector = bool(enable_turn0_llm_skill_selector)
        
        # TUI bridge for event streaming (set by OpenSpaceRuntime).
        self._tui_bridge: Optional[Any] = None
        self._runtime_event_sink: Optional[Any] = None

        # Tools from the last execution (available for post-execution analysis)
        self._last_tools: List = []

        # Hook registry for tool execution lifecycle (step 3.1)
        from openspace.services.tooling.hooks import HookRegistry, setup_default_hooks
        self._hook_registry = HookRegistry()
        setup_default_hooks(self._hook_registry)

        # OpenSpace loadedNestedMemoryPaths is session-scoped: it dedupes nested
        # instruction attachments even if read_file_state churns.
        self._loaded_nested_memory_paths: set[str] = set()

        # Loaded lazily so importing GroundingAgent does not force the
        # AgentTool module during base agent construction.
        self._agent_definition_registry: Any | None = None
        self._multi_agent_orchestrator: Any | None = None
        self._coordinator_mode: Any | None = None

        logger.info(f"Grounding Agent initialized: {name}")
        logger.info(f"Backend scope: {self._backend_scope}")
        logger.info(f"Max iterations: {self._max_iterations}")
        if tool_retrieval_llm:
            logger.info(f"Tool retrieval model: {tool_retrieval_llm.model}")

    async def _emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """Send an event via TUI bridge if available. Failures are silently swallowed."""
        if self._tui_bridge is None:
            return
        try:
            await self._tui_bridge.send(event_type, data)
        except Exception:
            pass

    def set_tui_bridge(self, bridge: Optional[Any]) -> None:
        """Attach the UI event bridge used for foreground event streaming."""
        self._tui_bridge = bridge

    def set_runtime_event_sink(self, sink: Optional[Any]) -> None:
        """Attach an internal runtime event sink used by background orchestration."""
        self._runtime_event_sink = sink

    async def _emit_runtime_event(
        self,
        event_type: str,
        data: Dict[str, Any],
    ) -> None:
        if self._runtime_event_sink is None:
            return
        try:
            result = self._runtime_event_sink(event_type, data)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug(
                "GroundingAgent runtime sink failed for %s",
                event_type,
                exc_info=True,
            )

    def set_skill_registry(self, registry: Optional["SkillRegistry"]) -> None:
        """Attach a SkillRegistry so the agent can offer Skill Protocol tools."""
        self._skill_registry = registry
        if registry:
            count = len(registry.list_skills())
            logger.info(f"Skill registry attached ({count} skill(s) available)")

    def set_skill_protocol_settings(
        self,
        *,
        listing_enabled: bool = True,
        discovery_enabled: bool = True,
        discovery_max_results: int = 5,
        listing_budget_context_percent: float = 0.01,
        listing_max_description_chars: int = 250,
        post_tool_query_builder_enabled: bool = False,
        post_tool_query_builder_model: Optional[str] = None,
        post_tool_query_builder_max_chars: int = 4000,
    ) -> None:
        self._skill_listing_enabled = bool(listing_enabled)
        self._skill_discovery_enabled = bool(discovery_enabled)
        self._skill_discovery_max_results = max(1, min(int(discovery_max_results or 5), 20))
        self._skill_listing_budget_context_percent = max(
            0.0,
            float(listing_budget_context_percent),
        )
        self._skill_listing_max_description_chars = max(
            20,
            int(listing_max_description_chars or 250),
        )
        self._post_tool_query_builder_enabled = bool(post_tool_query_builder_enabled)
        self._post_tool_query_builder_model = post_tool_query_builder_model
        self._post_tool_query_builder_max_chars = int(post_tool_query_builder_max_chars or 4000)

    def _tool_set_signature(self, tools: List[Any]) -> str:
        return agent_tool_inventory.tool_set_signature(tools)
    def _resolve_tui_available(self, context: Dict[str, Any]) -> bool:
        return agent_tool_inventory.resolve_tui_available(self, context)
    def _resolve_async_agent(self, context: Dict[str, Any]) -> bool:
        return agent_tool_inventory.resolve_async_agent(context)
    def _resolve_permission_context(
        self,
        *,
        cwd: str,
        permission_mode: str | None,
        context: Dict[str, Any],
    ) -> Any:
        return agent_tool_inventory.resolve_permission_context(
            cwd=cwd,
            permission_mode=permission_mode,
            context=context,
        )
    def _sync_tool_use_context_runtime(
        self,
        tool_use_context: ToolUseContext,
        *,
        messages: List[Dict[str, Any]] | None = None,
        tools: List[Any] | None = None,
        all_tools: List[Any] | None = None,
        current_iteration: int | None = None,
        max_iterations: int | None = None,
        model: str | None = None,
    ) -> ToolUseContext:
        if messages is not None:
            tool_use_context.messages = messages
        if tools is not None:
            tool_use_context.tools = list(tools)
        if all_tools is not None:
            tool_use_context.all_tools = list(all_tools)
        if current_iteration is not None:
            tool_use_context.current_iteration = current_iteration
        if max_iterations is not None:
            tool_use_context.max_iterations = max_iterations
        if model is not None:
            tool_use_context.model = str(model)
        return tool_use_context

    @staticmethod
    def _normalize_response_style(value: Any = None) -> str:
        return agent_message_builder.normalize_response_style(value)
    @staticmethod
    def _apply_response_style_prompt(prompt: str, response_style: Any = None) -> str:
        return agent_message_builder.apply_response_style_prompt(prompt, response_style)
    @staticmethod
    def _filter_tools_for_permission_mode(
        tools: List[Any],
        tool_use_context: ToolUseContext,
    ) -> List[Any]:
        return agent_tool_inventory.filter_tools_for_permission_mode(
            tools,
            tool_use_context,
        )
    def _append_todo_write_tool(self, tools: list[Any]) -> None:
        agent_tool_inventory.append_todo_write_tool(tools)
    def _append_sleep_and_brief_tools(self, tools: list[Any]) -> None:
        agent_tool_inventory.append_sleep_and_brief_tools(tools)
    def _append_schedule_cron_tools(self, tools: list[Any]) -> None:
        agent_tool_inventory.append_schedule_cron_tools(tools)
    def _get_agent_definition_registry(self) -> Any:
        return agent_tool_inventory.get_agent_definition_registry(self)
    def _resolve_agent_definitions(
        self,
        context: Dict[str, Any],
        tools: List[Any],
    ) -> Any:
        return agent_tool_inventory.resolve_agent_definitions(self, context, tools)
    def _with_agent_tool(
        self,
        tools: List[Any],
        *,
        context: Dict[str, Any],
        agent_definitions: Any,
    ) -> List[Any]:
        return agent_tool_inventory.with_agent_tool(
            self,
            tools,
            context=context,
            agent_definitions=agent_definitions,
        )
    def _should_append_agent_tools(self, context: Dict[str, Any]) -> bool:
        return agent_tool_inventory.should_append_agent_tools(self, context)
    def _append_config_tool(self, tools: list[Any]) -> None:
        agent_tool_inventory.append_config_tool(tools)
    def _append_lsp_tool(self, tools: list[Any]) -> None:
        agent_tool_inventory.append_lsp_tool(tools)
    def _append_plan_mode_tools(self, tools: list[Any]) -> None:
        agent_tool_inventory.append_plan_mode_tools(tools)
    def _append_ask_user_question_tool(self, tools: list[Any]) -> None:
        agent_tool_inventory.append_ask_user_question_tool(tools)
    def _append_multi_agent_control_tools(self, tools: list[Any]) -> None:
        agent_tool_inventory.append_multi_agent_control_tools(tools)
    def _bind_agent_tools_to_context(
        self,
        tools: Iterable[Any],
        tool_use_context: ToolUseContext,
    ) -> None:
        agent_tool_inventory.bind_agent_tools_to_context(tools, tool_use_context)
    def _bind_skill_tools_to_context(
        self,
        tools: Iterable[Any],
        tool_use_context: ToolUseContext,
    ) -> None:
        agent_skill_context.bind_skill_tools_to_context(tools, tool_use_context)
    def _append_skill_listing_delta(
        self,
        messages: List[Dict[str, Any]],
        tool_use_context: ToolUseContext,
    ) -> None:
        agent_skill_context.append_skill_listing_delta(self, messages, tool_use_context)
    def _append_skill_discovery_delta(
        self,
        messages: List[Dict[str, Any]],
        tool_use_context: ToolUseContext,
        *,
        query: str,
        source: str,
    ) -> None:
        agent_skill_context.append_skill_discovery_delta(
            self,
            messages,
            tool_use_context,
            query=query,
            source=source,
        )
    async def _append_skill_discovery_delta_async(
        self,
        messages: List[Dict[str, Any]],
        tool_use_context: ToolUseContext,
        *,
        query: str,
        source: str,
    ) -> None:
        await agent_skill_context.append_skill_discovery_delta_async(
            self,
            messages,
            tool_use_context,
            query=query,
            source=source,
        )
    @staticmethod
    def _has_skill_tool(tools: Iterable[Any]) -> bool:
        return agent_skill_context.has_skill_tool(tools)
    @staticmethod
    def _has_discover_skills_tool(tools: Iterable[Any]) -> bool:
        return agent_skill_context.has_discover_skills_tool(tools)
    @staticmethod
    def _skill_discovery_query_from_recent_messages(
        instruction: str,
        messages: List[Dict[str, Any]],
    ) -> str:
        return agent_skill_context.skill_discovery_query_from_recent_messages(
            instruction,
            messages,
        )
    async def _build_post_tool_skill_discovery_query(
        self,
        instruction: str,
        messages: List[Dict[str, Any]],
        *,
        abort_event: asyncio.Event | None = None,
    ) -> str:
        return await agent_skill_context.build_post_tool_skill_discovery_query(
            self,
            instruction,
            messages,
            abort_event=abort_event,
        )
    def _append_agent_listing_delta(
        self,
        messages: List[Dict[str, Any]],
        tool_use_context: ToolUseContext,
    ) -> None:
        agent_skill_context.append_agent_listing_delta(messages, tool_use_context)
    @staticmethod
    def _mcp_servers_from_tools(tools: Iterable[Any]) -> list[str]:
        return agent_tool_inventory.mcp_servers_from_tools(tools)
    @staticmethod
    def _is_api_error_message(message: Dict[str, Any] | None) -> bool:
        return agent_stop_policy.is_api_error_message(message)
    @staticmethod
    def _model_error_stop_reason(stop_reason: str | None) -> str:
        return agent_stop_policy.model_error_stop_reason(stop_reason)
    @staticmethod
    def _get_model_response_followup_messages(
        model_response: ModelResponse,
    ) -> list[dict[str, Any]]:
        return agent_stop_policy.get_model_response_followup_messages(model_response)
    def _find_tool_by_name(
        self,
        tool_name: str,
        *,
        tool_map: Dict[str, Any] | None = None,
        tools: List[Any] | None = None,
    ) -> Any | None:
        return agent_tool_inventory.find_tool_by_name(
            tool_name,
            tool_map=tool_map,
            tools=tools,
        )
    def _build_iteration_tool_results(
        self,
        *,
        tool_calls: list[dict[str, Any]],
        tool_map: dict[str, Any],
        result_messages: list[dict[str, Any]],
        tools: List[Any],
    ) -> list[dict[str, Any]]:
        return agent_tool_inventory.build_iteration_tool_results(
            tool_calls=tool_calls,
            tool_map=tool_map,
            result_messages=result_messages,
            tools=tools,
        )
    def _create_tool_use_context(
        self,
        *,
        tools: List[Any],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
    ) -> ToolUseContext:
        """Assemble turn-scoped runtime context for upcoming tool pipeline work."""

        abort_event = context.get("abort_event")
        if abort_event is not None and not isinstance(abort_event, asyncio.Event):
            logger.debug("Ignoring non-asyncio abort_event on tool use context")
            abort_event = None

        permission_engine = context.get(
            "permission_engine",
            getattr(self, "_permission_engine", None),
        )
        hook_registry = context.get(
            "hook_registry",
            getattr(self, "_hook_registry", None),
        )

        quality_manager = None
        if self.grounding_client is not None:
            quality_manager = getattr(self.grounding_client, "quality_manager", None)

        read_file_state = context.get("read_file_state")
        if not isinstance(read_file_state, dict):
            read_file_state = {}
        nested_memory_triggers = context.get("nested_memory_triggers")
        if not isinstance(nested_memory_triggers, set):
            nested_memory_triggers = set()
        loaded_nested_memory_paths = context.get("loaded_nested_memory_paths")
        if not isinstance(loaded_nested_memory_paths, set):
            loaded_nested_memory_paths = self._loaded_nested_memory_paths
        nested_memory_source_paths = context.get("nested_memory_source_paths")
        if not isinstance(nested_memory_source_paths, set):
            nested_memory_source_paths = set()
        todo_state = context.get("todo_state")
        if not isinstance(todo_state, dict):
            todo_state = {}
            context["todo_state"] = todo_state

        event_sink = context.get("event_sink")
        if event_sink is None and self._llm_client is not None:
            event_sink = getattr(self._llm_client, "_event_callback", None)

        recording_manager = context.get("recording_manager", self._recording_manager)
        quality_manager = context.get("quality_manager", quality_manager)
        cwd = str(context.get("workspace_dir") or ".")
        raw_permission_mode = context.get("permission_mode")
        permission_mode = (
            str(raw_permission_mode)
            if raw_permission_mode is not None
            else None
        )

        from openspace.grounding.core.tool.base import DEFAULT_MAX_RESULT_SIZE_CHARS
        max_result_size_chars = context.get("max_result_size_chars", DEFAULT_MAX_RESULT_SIZE_CHARS)
        try:
            max_result_size_chars = int(max_result_size_chars)
        except (TypeError, ValueError):
            max_result_size_chars = DEFAULT_MAX_RESULT_SIZE_CHARS

        tool_results_token_count = context.get("tool_results_token_count", 0)
        try:
            tool_results_token_count = int(tool_results_token_count)
        except (TypeError, ValueError):
            tool_results_token_count = 0

        permission_context = self._resolve_permission_context(
            cwd=cwd,
            permission_mode=permission_mode,
            context=context,
        )
        effective_permission_mode = str(
            getattr(permission_context, "mode", permission_mode or "default")
        )
        pre_plan_mode = getattr(permission_context, "pre_plan_mode", None)
        session_id = context.get("session_id")
        agent_id_value = context.get("agent_id", "primary")
        plan_file_path = context.get("plan_file_path")
        if effective_permission_mode == "plan" or plan_file_path:
            try:
                from openspace.services.runtime_support.plan_mode import get_plan_file_path

                plan_file_path = str(
                    get_plan_file_path(
                        str(session_id) if session_id is not None else None,
                        str(agent_id_value) if agent_id_value is not None else None,
                    )
                )
            except Exception:
                plan_file_path = str(plan_file_path) if plan_file_path else None
        else:
            plan_file_path = None

        try:
            from openspace.services.memory.daily_log import get_memory_mode

            memory_mode = get_memory_mode(
                str(context["memory_mode"]) if context.get("memory_mode") is not None else None
            )
        except Exception:
            memory_mode = str(context.get("memory_mode") or "direct")

        tool_use_context = ToolUseContext(
            tools=list(tools),
            all_tools=list(context.get("all_tools") or tools),
            model=str(getattr(self._llm_client, "model", "unknown")),
            llm_client=self._llm_client,
            cwd=cwd,
            original_cwd=str(context.get("original_cwd") or cwd),
            agent_id=str(context.get("agent_id") or "primary"),
            agent_type=(
                str(context.get("agent_type"))
                if context.get("agent_type") is not None
                else None
            ),
            max_result_size_chars=max_result_size_chars,
            abort_event=abort_event,
            messages=messages,
            read_file_state=read_file_state,
            nested_memory_triggers=nested_memory_triggers,
            loaded_nested_memory_paths=loaded_nested_memory_paths,
            nested_memory_source_paths=nested_memory_source_paths,
            tool_results_token_count=tool_results_token_count,
            permission_engine=permission_engine,
            permission_mode=effective_permission_mode,
            pre_plan_mode=str(pre_plan_mode) if pre_plan_mode else None,
            plan_file_path=plan_file_path,
            plan_mode_exit_pending=bool(context.get("plan_mode_exit_pending", False)),
            plan_mode_exited_in_session=bool(context.get("plan_mode_exited_in_session", False)),
            permission_context=permission_context,
            base_permission_context=context.get("base_permission_context") or permission_context,
            event_sink=event_sink,
            recording_manager=recording_manager,
            quality_manager=quality_manager,
            quality_recorded_tool_use_ids=set(
                context.get("quality_recorded_tool_use_ids") or ()
            ),
            cost_tracker=context.get("cost_tracker"),
            scheduler=context.get("scheduler"),
            ensure_scheduler=context.get("ensure_scheduler"),
            notification_service=context.get("notification_service"),
            approval_service=context.get("approval_service"),
            lsp_manager=context.get("lsp_manager"),
            diagnostic_tracker=context.get("diagnostic_tracker"),
            hook_registry=hook_registry,
            async_rewake_queue=context.get("async_rewake_queue"),
            channel_context=dict(context.get("channel_context") or {}),
            http_hook_allowed_urls=context.get("http_hook_allowed_urls"),
            http_hook_allowed_env_vars=context.get("http_hook_allowed_env_vars"),
            tui_available=self._resolve_tui_available(context),
            is_async_agent=self._resolve_async_agent(context),
            parent_task_id=(
                str(context.get("parent_task_id"))
                if context.get("parent_task_id") is not None
                else None
            ),
            task_manager=context.get("task_manager"),
            multi_agent_orchestrator=context.get("multi_agent_orchestrator")
            or self._multi_agent_orchestrator,
            todo_state=todo_state,
            coordinator_mode=context.get("coordinator_mode") or self._coordinator_mode,
            coordinator_mode_enabled=bool(context.get("coordinator_mode_enabled", False)),
            coordinator_notification_queue=context.get("coordinator_notification_queue"),
            coordinator_worker_tools=list(context.get("coordinator_worker_tools") or ()),
            task_id=(
                str(context.get("task_id"))
                if context.get("task_id") is not None
                else None
            ),
            task_description=str(
                context.get("task_query")
                or context.get("tool_retrieval_query")
                or context.get("instruction")
                or ""
            ),
            current_iteration=int(context.get("current_iteration") or 0),
            max_iterations=int(context.get("max_iterations") or self._max_iterations),
            agent_definitions=context.get("agent_definitions"),
            allowed_agent_types=context.get("allowed_agent_types"),
            session_id=(
                str(context.get("session_id"))
                if context.get("session_id") is not None
                else None
            ),
            session_dir=(
                str(context.get("session_dir"))
                if context.get("session_dir") is not None
                else None
            ),
            tool_results_dir=(
                str(context.get("tool_results_dir"))
                if context.get("tool_results_dir") is not None
                else (
                    str(context["session_storage"].tool_results_dir)
                    if context.get("session_storage") is not None
                    and hasattr(context.get("session_storage"), "tool_results_dir")
                    else None
                )
            ),
            session_storage=context.get("session_storage"),
            file_history=context.get("file_history"),
            memory_mode=memory_mode,
            response_style=self._normalize_response_style(context.get("response_style")),
            append_system_message=context.get("append_system_message"),
            capability_profile=str(context.get("capability_profile") or ""),
            backend_scope=tuple(str(v) for v in self._backend_scope),
            deferred_tool_names=set(context.get("deferred_tool_names") or ()),
            discovered_tool_names=set(context.get("discovered_tool_names") or ()),
            tool_schema_cache_telemetry_enabled=bool(
                context.get("tool_schema_cache_telemetry", True)
            ),
            mcp_clients=list(context.get("mcp_clients") or ()),
            skill_registry=self._skill_registry,
            skill_store=getattr(self, "_skill_store", None),
            sent_skill_names_by_agent=context.get("sent_skill_names_by_agent") or {},
            discovered_skill_names=set(context.get("discovered_skill_names") or ()),
            skill_metadata_only_discovery=bool(
                context.get("skill_metadata_only_discovery", False)
            ),
            invoked_skills_by_agent=context.get("invoked_skills_by_agent") or {},
            skill_listing_suppressed_once=bool(
                context.get("skill_listing_suppressed_once", False)
            ),
            active_skill_scopes=context.get("active_skill_scopes") or {},
            skill_model_override=context.get("skill_model_override"),
            skill_effort_override=context.get("skill_effort_override"),
            dynamic_skill_path_triggers=set(
                context.get("dynamic_skill_path_triggers") or ()
            ),
            sent_dynamic_skill_keys=set(context.get("sent_dynamic_skill_keys") or ()),
            path_activated_skill_names=set(
                context.get("path_activated_skill_names") or ()
            ),
            skills_disabled=self._skills_disabled_for_context(context),
        )
        if tool_use_context.active_skill_scopes:
            tool_use_context.rebuild_skill_permission_context()
        return tool_use_context

    async def process(self, context: Dict[str, Any]) -> Dict[str, Any]:
        from openspace.agents.turns.loop import run_grounding_turn

        return await run_grounding_turn(self, context)

    # ── Helper methods for process() ────────────────────────────────────

    def _build_session_capability_state_payload(
        self,
        context: Dict[str, Any],
        tool_use_context: ToolUseContext,
        *,
        active_tools: List[Any],
        profile_name: Any,
    ) -> Dict[str, Any]:
        from openspace.services.runtime_support.low_latency import SessionCapabilityState

        previous = SessionCapabilityState.from_mapping(
            context.get("session_capability_state")
        )
        visible_skill_names: set[str] = set()
        for names in tool_use_context.sent_skill_names_by_agent.values():
            visible_skill_names.update(str(name) for name in names if name)
        active_skill_ids: set[str] = {
            str(scope.skill_id)
            for scope in tool_use_context.active_skill_scopes.values()
            if getattr(scope, "skill_id", None)
        }
        for records in tool_use_context.invoked_skills_by_agent.values():
            for record in records:
                skill_id = getattr(record, "skill_id", None)
                if skill_id:
                    active_skill_ids.add(str(skill_id))

        updated = previous.merge_turn(
            profile_name=str(profile_name or context.get("capability_profile") or ""),
            discovered_tool_names=tool_use_context.discovered_tool_names,
            active_tool_names=[
                str(getattr(tool, "name", "") or "")
                for tool in active_tools
                if getattr(tool, "name", None)
            ],
            deferred_tool_names=tool_use_context.deferred_tool_names,
            visible_skill_names=visible_skill_names,
            discovered_skill_names=tool_use_context.discovered_skill_names,
            active_skill_ids=active_skill_ids,
            last_intent_classification=str(
                context.get("capability_profile") or profile_name or ""
            ),
            reason=str(context.get("active_tool_policy_reason") or "turn"),
        )
        payload = updated.to_dict()
        context["session_capability_state"] = payload
        context["discovered_tool_names"] = set(updated.discovered_tool_names)
        context["discovered_skill_names"] = set(updated.discovered_skill_names)
        context["sent_skill_names_by_agent"] = {
            "primary": set(updated.visible_skill_names)
        }
        return payload

    @staticmethod
    def _summarize_tool_schema_cache_events(
        tool_use_context: ToolUseContext,
    ) -> Dict[str, Any]:
        events = list(getattr(tool_use_context, "tool_schema_cache_events", []) or [])
        if not events:
            return {}
        hits = sum(1 for event in events if event.get("cache_hit"))
        misses = len(events) - hits
        latest = events[-1]
        total_render_ms = sum(
            float(event.get("render_duration_ms") or 0.0)
            for event in events
        )
        return {
            "events": len(events),
            "hits": hits,
            "misses": misses,
            "hit_rate": hits / len(events),
            "active_schema_count": latest.get("active_schema_count", 0),
            "all_tools_count": latest.get("all_tools_count", 0),
            "deferred_tools_count": latest.get("deferred_tools_count", 0),
            "discovered_tools_count": latest.get("discovered_tools_count", 0),
            "model": latest.get("model"),
            "profile": latest.get("profile"),
            "permission_mode": latest.get("permission_mode"),
            "backend_scope": latest.get("backend_scope") or [],
            "render_duration_ms": total_render_ms,
        }

    async def _save_session_turn(
        self,
        tool_use_context: ToolUseContext,
        messages: List[Dict[str, Any]],
        *,
        usage: Any | None = None,
        model: str | None = None,
    ) -> None:
        storage = getattr(tool_use_context, "session_storage", None)
        if storage is None:
            return
        save_turn = getattr(storage, "save_turn", None)
        if save_turn is None:
            return
        try:
            metadata_patch = self._session_turn_metadata_patch(tool_use_context)
            result = save_turn(
                messages,
                usage,
                model=model,
                metadata_patch=metadata_patch,
            )
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug("Session turn persistence failed", exc_info=True)

    @staticmethod
    def _session_turn_metadata_patch(tool_use_context: ToolUseContext) -> dict[str, Any] | None:
        task_id = getattr(tool_use_context, "task_id", None)
        parent_task_id = getattr(tool_use_context, "parent_task_id", None)
        raw_agent_id = getattr(tool_use_context, "agent_id", None)
        if not task_id and not parent_task_id and not raw_agent_id:
            return None
        agent_id = raw_agent_id or "primary"
        runtime: dict[str, Any] = {}
        patch: dict[str, Any] = {}
        if task_id:
            patch["last_task_id"] = str(task_id)
            runtime["active_task_id"] = str(task_id)
        if parent_task_id:
            patch["parent_task_id"] = str(parent_task_id)
            runtime["parent_task_id"] = str(parent_task_id)
        if agent_id:
            patch["agent_id"] = str(agent_id)
            runtime["agent_id"] = str(agent_id)
        if runtime:
            patch["runtime"] = runtime
        return patch or None

    async def _drain_messages(
        self,
        source: Union[asyncio.Queue, Callable, Any],
    ) -> List[Dict[str, Any]]:
        """Consume all pending external messages and return as LLM messages.

        Implementation: ``getCommandsByMaxPriority`` + ``getAttachmentMessages``
        in ``query.ts`` L1566-1590.

        OS simplification: FIFO Queue instead of priority queue (OpenSpace has
        max-priority scheduling across command/attachment/etc.).
        """
        drained: List[Dict[str, Any]] = []
        if isinstance(source, asyncio.Queue):
            while not source.empty():
                try:
                    msg = source.get_nowait()
                    drained.append(self._format_injected_message(msg))
                except asyncio.QueueEmpty:
                    break
        elif callable(source):
            try:
                pending = source()
                if inspect.isawaitable(pending):
                    pending = await pending
                if pending:
                    for msg in pending:
                        drained.append(self._format_injected_message(msg))
            except Exception:
                pass
        return drained

    @staticmethod
    def _format_injected_message(msg: Any) -> Dict[str, Any]:
        return agent_message_builder.format_injected_message(msg)
    def _build_retrieved_tools_list(
        self,
        tools: List,
        preselection_debug_info: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        return agent_tool_inventory.build_retrieved_tools_list(
            tools,
            preselection_debug_info,
        )
    def _default_system_prompt(
        self,
        cwd: Optional[str] = None,
        *,
        deferred_tool_names: Optional[Iterable[str]] = None,
        memory_mode: Optional[str] = None,
        skills_enabled: bool = True,
        skill_discovery_enabled: Optional[bool] = None,
    ) -> str:
        return agent_message_builder.default_system_prompt(
            self,
            cwd=cwd,
            deferred_tool_names=deferred_tool_names,
            memory_mode=memory_mode,
            skills_enabled=skills_enabled,
            skill_discovery_enabled=skill_discovery_enabled,
        )
    def _current_system_prompt(
        self,
        cwd: Optional[str] = None,
        *,
        deferred_tool_names: Optional[Iterable[str]] = None,
        memory_mode: Optional[str] = None,
        skills_enabled: bool = True,
        skill_discovery_enabled: Optional[bool] = None,
        permission_mode: Optional[str] = None,
        plan_file_path: Optional[str] = None,
        response_style: Optional[str] = None,
    ) -> str:
        return agent_message_builder.current_system_prompt(
            self,
            cwd=cwd,
            deferred_tool_names=deferred_tool_names,
            memory_mode=memory_mode,
            skills_enabled=skills_enabled,
            skill_discovery_enabled=skill_discovery_enabled,
            permission_mode=permission_mode,
            plan_file_path=plan_file_path,
            response_style=response_style,
        )
    @staticmethod
    def _resolve_memory_mode(value: Any = None) -> str:
        return agent_tool_inventory.resolve_memory_mode(value)
    @staticmethod
    def _with_memory_mode_tools(tools: List[Any], memory_mode: str) -> List[Any]:
        return agent_tool_inventory.with_memory_mode_tools(tools, memory_mode)
    @staticmethod
    def _skills_disabled_for_context(context: Dict[str, Any]) -> bool:
        return agent_tool_inventory.skills_disabled_for_context(context)
    @staticmethod
    def _without_skill_protocol_tools(tools: Iterable[Any]) -> List[Any]:
        return agent_tool_inventory.without_skill_protocol_tools(tools)
    @staticmethod
    def _deferred_tool_names(
        tools: List[Any],
        *,
        discovered_tool_names: Iterable[str] = (),
    ) -> list[str]:
        return agent_tool_inventory.deferred_tool_names(
            tools,
            discovered_tool_names=discovered_tool_names,
        )
    @staticmethod
    def _build_active_tools(
        tools: List[Any],
        *,
        discovered_tool_names: Iterable[str] = (),
        active_tool_names: Optional[Iterable[str]] = None,
        deferred_tool_names: Optional[Iterable[str]] = None,
    ) -> List[Any]:
        return agent_tool_inventory.build_active_tools(
            tools,
            discovered_tool_names=discovered_tool_names,
            active_tool_names=active_tool_names,
            deferred_tool_names=deferred_tool_names,
        )
    def _refresh_primary_system_prompt(
        self,
        messages: List[Dict[str, Any]],
        *,
        cwd: Optional[str] = None,
        deferred_tool_names: Optional[Iterable[str]] = None,
        memory_mode: Optional[str] = None,
        skills_enabled: bool = True,
        skill_discovery_enabled: Optional[bool] = None,
        permission_mode: Optional[str] = None,
        plan_file_path: Optional[str] = None,
        response_style: Optional[str] = None,
        coordinator_mode: Any | None = None,
        coordinator_mode_enabled: bool | None = None,
    ) -> None:
        agent_message_builder.refresh_primary_system_prompt(
            self,
            messages,
            cwd=cwd,
            deferred_tool_names=deferred_tool_names,
            memory_mode=memory_mode,
            skills_enabled=skills_enabled,
            skill_discovery_enabled=skill_discovery_enabled,
            permission_mode=permission_mode,
            plan_file_path=plan_file_path,
            response_style=response_style,
            coordinator_mode=coordinator_mode,
            coordinator_mode_enabled=coordinator_mode_enabled,
        )
    def _refresh_system_messages_after_compact(
        self,
        messages: List[Dict[str, Any]],
        *,
        cwd: Optional[str] = None,
        deferred_tool_names: Optional[Iterable[str]] = None,
        memory_mode: Optional[str] = None,
        skills_enabled: bool = True,
        skill_discovery_enabled: Optional[bool] = None,
        permission_mode: Optional[str] = None,
        plan_file_path: Optional[str] = None,
        response_style: Optional[str] = None,
        coordinator_mode: Any | None = None,
        coordinator_mode_enabled: bool | None = None,
    ) -> List[Dict[str, Any]]:
        return agent_message_builder.refresh_system_messages_after_compact(
            self,
            messages,
            cwd=cwd,
            deferred_tool_names=deferred_tool_names,
            memory_mode=memory_mode,
            skills_enabled=skills_enabled,
            skill_discovery_enabled=skill_discovery_enabled,
            permission_mode=permission_mode,
            plan_file_path=plan_file_path,
            response_style=response_style,
            coordinator_mode=coordinator_mode,
            coordinator_mode_enabled=coordinator_mode_enabled,
        )
    def construct_messages(
        self,
        context: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        return agent_message_builder.construct_messages(self, context)
    def _scoped_tool_backends(self) -> list[BackendType]:
        return agent_tool_inventory.scoped_tool_backends(self)
    def _with_skill_protocol_tools(
        self,
        tools: List[Any],
        backends: list[BackendType],
    ) -> List[Any]:
        return agent_tool_inventory.with_skill_protocol_tools(self, tools, backends)
    async def _get_tools_without_auto_preselection(self) -> List[Any]:
        return await agent_tool_inventory.get_tools_without_auto_preselection(self)
    async def _get_available_tools(self, task_description: Optional[str]) -> List:
        return await agent_tool_inventory.get_available_tools(self, task_description)
    async def _get_tool_universe(self, preselected_tools: List[Any]) -> List[Any]:
        return await agent_tool_inventory.get_tool_universe(self, preselected_tools)
    async def _load_all_tools(self, grounding_client: "GroundingClient") -> List:
        return await agent_tool_inventory.load_all_tools(self, grounding_client)
    def _get_workspace_path(self, context: Dict[str, Any]) -> Optional[str]:
        """
        Get workspace directory path from context.
        """
        return context.get("workspace_dir")
    
    def _scan_workspace_files(
        self,
        workspace_path: str,
        recent_threshold: int = 600 # seconds
    ) -> Dict[str, Any]:
        """
        Scan workspace directory and collect file information.
        
        Args:
            workspace_path: Path to workspace directory
            recent_threshold: Threshold in seconds for recent files
            
        Returns:
            Dictionary with file information:
                - files: List of all filenames
                - file_details: Dict mapping filename to file info (size, modified, age_seconds)
                - recent_files: List of recently modified filenames
        """
        import os
        import time
        
        result = {
            "files": [],
            "file_details": {},
            "recent_files": []
        }
        
        if not workspace_path or not os.path.exists(workspace_path):
            return result
        
        # Recording system files to exclude from workspace scanning
        excluded_files = {"metadata.json", "traj.jsonl"}
        
        try:
            current_time = time.time()
            
            for filename in os.listdir(workspace_path):
                filepath = os.path.join(workspace_path, filename)
                if os.path.isfile(filepath) and filename not in excluded_files:
                    result["files"].append(filename)
                    
                    # Get file stats
                    stat = os.stat(filepath)
                    file_info = {
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                        "age_seconds": current_time - stat.st_mtime
                    }
                    result["file_details"][filename] = file_info
                    
                    # Track recently created/modified files
                    if file_info["age_seconds"] < recent_threshold:
                        result["recent_files"].append(filename)
            
            result["files"] = sorted(result["files"])
        
        except Exception as e:
            logger.debug(f"Error scanning workspace files: {e}")
        
        return result
    
    async def _check_workspace_artifacts(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Check workspace directory for existing artifacts that might be relevant to the task.
        Enhanced to detect if task might already be completed.
        """
        import re
        
        workspace_info = {"has_files": False, "files": [], "file_details": {}, "recent_files": []}
        
        try:
            # Get workspace path
            workspace_path = self._get_workspace_path(context)
            
            # Scan workspace files
            scan_result = self._scan_workspace_files(workspace_path, recent_threshold=600)
            
            if scan_result["files"]:
                workspace_info["has_files"] = True
                workspace_info["files"] = scan_result["files"]
                workspace_info["file_details"] = scan_result["file_details"]
                workspace_info["recent_files"] = scan_result["recent_files"]
                
                logger.info(f"Grounding Agent: Found {len(scan_result['files'])} existing files in workspace "
                           f"({len(scan_result['recent_files'])} recent)")
                
                # Check if instruction mentions specific filenames
                instruction = context.get("instruction", "")
                if instruction:
                    # Look for potential file references in instruction
                    potential_outputs = []
                    # Match common file patterns: filename.ext, "filename", 'filename'
                    file_patterns = re.findall(r'["\']?([a-zA-Z0-9_\-]+\.[a-zA-Z0-9]+)["\']?', instruction)
                    for pattern in file_patterns:
                        if pattern in scan_result["files"]:
                            potential_outputs.append(pattern)
                    
                    if potential_outputs:
                        workspace_info["matching_files"] = potential_outputs
                        logger.info(f"Grounding Agent: Found {len(potential_outputs)} files matching task: {potential_outputs}")
        
        except Exception as e:
            logger.debug(f"Could not check workspace artifacts: {e}")
        
        return workspace_info
    
    async def _build_final_result(
        self,
        instruction: str,
        messages: List[Dict],
        all_tool_results: List[Dict],
        iterations: int,
        max_iterations: int,
        iteration_contexts: List[Dict] = None,
        retrieved_tools_list: List[Dict] = None,
        preselection_debug_info: Dict[str, Any] = None,
        stop_reason: str | None = None,
    ) -> Dict[str, Any]:
        """Build final execution result.

        OpenSpace-aligned completion logic: "no tool calls = completed".
        ``stop_reason`` is set by the main loop (``completed``, ``max_turns``,
        ``aborted``, ``model_error``, etc.).
        """
        tool_executions = self._format_tool_executions(all_tool_results)
        last_assistant_message = self._extract_last_assistant_message_entry(messages)
        effective_stop_reason = stop_reason
        if (
            self._is_api_error_message(last_assistant_message)
            and effective_stop_reason in (None, "completed", "stop_hook_prevented")
        ):
            effective_stop_reason = "model_error"

        is_success = effective_stop_reason in (
            "completed",
            "stop_hook_prevented",
            "bench_finalize_budget",
        )

        skill_ids = self._extract_skill_ids_from_messages(messages)

        result = {
            "instruction": instruction,
            "step": self.step,
            "iterations": iterations,
            "tool_executions": tool_executions,
            "messages": messages,
            "iteration_contexts": iteration_contexts or [],
            "retrieved_tools_list": retrieved_tools_list or [],
            "preselection_debug_info": preselection_debug_info,
            "active_skills": skill_ids,
            "keep_session": True,
            "stop_reason": effective_stop_reason,
        }

        result["response"] = (
            last_assistant_message.get("content", "")
            if last_assistant_message is not None
            else ""
        )

        if is_success:
            result["status"] = "success"
        elif effective_stop_reason in ("max_turns", "empty_response"):
            result["status"] = "incomplete"
            if effective_stop_reason == "empty_response":
                result["warning"] = (
                    f"Task stopped after {iterations} consecutive empty "
                    "model responses. The model did not produce content or "
                    "tool calls, so the task is incomplete."
                )
            else:
                result["warning"] = (
                    f"Task reached max iterations ({max_iterations}). "
                    "This may indicate the task needs more steps or clarification."
                )
        else:
            result["status"] = effective_stop_reason or "incomplete"
            result["error"] = (
                result.get("response")
                or result.get("warning")
                or result["status"]
            )

        return result

    @staticmethod
    def _extract_skill_ids_from_messages(messages: List[Dict]) -> List[str]:
        return agent_skill_context.extract_skill_ids_from_messages(messages)
    @staticmethod
    def _extract_tool_call_info(tool_call: Any) -> tuple[str, dict[str, Any]]:
        tool_name = "unknown"
        arguments: dict[str, Any] = {}

        if tool_call is None:
            return tool_name, arguments

        if hasattr(tool_call, "function"):
            tool_name = getattr(tool_call.function, "name", "unknown")
            args_raw = getattr(tool_call.function, "arguments", "{}")
        elif isinstance(tool_call, dict):
            function = tool_call.get("function", {})
            tool_name = function.get("name", "unknown")
            args_raw = function.get("arguments", "{}")
        else:
            return tool_name, arguments

        if isinstance(args_raw, str):
            try:
                arguments = json.loads(args_raw) if args_raw.strip() else {}
            except json.JSONDecodeError:
                arguments = {}
        elif isinstance(args_raw, dict):
            arguments = args_raw

        return tool_name, arguments
    
    def _format_tool_executions(self, all_tool_results: List[Dict]) -> List[Dict]:
        executions = []
        for tr in all_tool_results:
            tool_result_obj = tr.get("result")
            tool_call = tr.get("tool_call")
            status = tr.get("status", "unknown")
            if hasattr(tool_result_obj, "status"):
                status_obj = tool_result_obj.status
                status = getattr(status_obj, "value", status_obj)

            tool_name, arguments = self._extract_tool_call_info(tool_call)
            if tr.get("tool_name"):
                tool_name = tr["tool_name"]

            content = tr.get("content")
            error = tr.get("error")
            execution_time = tr.get("execution_time")
            metadata = tr.get("metadata", {})
            if hasattr(tool_result_obj, "content"):
                content = tool_result_obj.content
            if hasattr(tool_result_obj, "error"):
                error = tool_result_obj.error
            if hasattr(tool_result_obj, "execution_time"):
                execution_time = tool_result_obj.execution_time
            if hasattr(tool_result_obj, "metadata"):
                metadata = tool_result_obj.metadata

            executions.append({
                "tool_name": tool_name,
                "arguments": arguments,
                "backend": tr.get("backend"),
                "server_name": tr.get("server_name"),
                "status": status,
                "content": content,
                "error": error,
                "execution_time": execution_time,
                "metadata": metadata or {},
            })
        return executions
    
    # _check_task_completion removed — OpenSpace-aligned: "no tool calls = completed".
    # Stop reason is determined by the main loop, not by a magic token.

    def _extract_last_assistant_message_entry(
        self,
        messages: List[Dict],
    ) -> Dict[str, Any] | None:
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                return msg
        return None

    def _extract_last_assistant_message(self, messages: List[Dict]) -> str:
        message = self._extract_last_assistant_message_entry(messages)
        if message is None:
            return ""
        return message.get("content", "")

    async def _record_agent_execution(
        self,
        result: Dict[str, Any],
        instruction: str
    ) -> None:
        """
        Record agent execution to recording manager.
        
        Args:
            result: Execution result
            instruction: Original instruction
        """
        if not self._recording_manager:
            return
        
        # Extract tool execution summary
        tool_summary = []
        if result.get("tool_executions"):
            for exec_info in result["tool_executions"]:
                tool_summary.append({
                    "tool": exec_info.get("tool_name", "unknown"),
                    "backend": exec_info.get("backend", "unknown"),
                    "status": exec_info.get("status", "unknown"),
                })
        
        await self._recording_manager.record_agent_action(
            agent_name=self.name,
            action_type="execute",
            input_data={"instruction": instruction},
            reasoning={
                "response": result.get("response", ""),
                "tools_selected": tool_summary,
            },
            output_data={
                "status": result.get("status", "unknown"),
                "iterations": result.get("iterations", 0),
                "num_tool_executions": len(result.get("tool_executions", [])),
            },
            metadata={
                "step": self.step,
                "instruction": instruction,
            }
        )
