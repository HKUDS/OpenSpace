"""Tool-turn control flow for GroundingAgent turns."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from openspace.agents.turns import (
    bench_checker_guard,
    session_policy,
    stop_policy,
)
from openspace.agents.turns.context import TurnControllerContext
from openspace.agents.turns.task_query import resolve_task_query
from openspace.services.conversation.attachments import (
    create_attachment_message,
    get_turn_attachment_messages_async,
)
from openspace.services.memory.openspace_md import consume_nested_memory_triggers
from openspace.services.memory.recall import consume_relevant_memory_prefetch
from openspace.services.conversation.messages import build_user_interruption_message
from openspace.services.tooling.results import enforce_tool_result_budget
from openspace.tool_runtime.orchestration import RunToolsResult, run_tools
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

@dataclass(slots=True)
class ToolRefreshResult:
    tools: list[Any]
    active_tools: list[Any]
    tool_set_signature: str
    preselection_debug_info: dict[str, Any] | None
    retrieved_tools_list: list[dict[str, Any]]


@dataclass(slots=True)
class ToolPreparationResult:
    messages: list[dict[str, Any]]
    active_tools: list[Any]


@dataclass(slots=True)
class ToolTurnResult:
    action: str
    messages: list[dict[str, Any]]
    tool_use_context: Any
    active_tools: list[Any]


async def refresh_tools_for_iteration(
    turn: TurnControllerContext,
    *,
    messages: list[dict[str, Any]],
    tools: list[Any],
    active_tools: list[Any],
    pre_filtered_tools: Any,
    use_fast_tool_policy: bool,
    tool_set_signature: str,
    preselection_debug_info: dict[str, Any] | None,
    retrieved_tools_list: list[dict[str, Any]],
) -> ToolRefreshResult:
    agent = turn.agent
    context = turn.context
    tool_use_context = turn.tool_use_context
    state = turn.state
    if not (
        agent.grounding_client
        and state.current_iteration > 1
        and pre_filtered_tools is None
        and not use_fast_tool_policy
    ):
        return ToolRefreshResult(
            tools=tools,
            active_tools=active_tools,
            tool_set_signature=tool_set_signature,
            preselection_debug_info=preselection_debug_info,
            retrieved_tools_list=retrieved_tools_list,
        )

    try:
        tool_retrieval_instruction = resolve_task_query(context, turn.instruction)
        refreshed_preselected = await agent._get_available_tools(
            tool_retrieval_instruction
        )
        refreshed = await agent._get_tool_universe(refreshed_preselected)
        refreshed = agent._with_memory_mode_tools(
            refreshed,
            tool_use_context.memory_mode,
        )
        refreshed_agent_definitions = agent._resolve_agent_definitions(
            context,
            refreshed,
        )
        if agent._should_append_agent_tools(context):
            refreshed = agent._with_agent_tool(
                refreshed,
                context=context,
                agent_definitions=refreshed_agent_definitions,
            )
        if tool_use_context.skills_disabled:
            refreshed_preselected = agent._without_skill_protocol_tools(
                refreshed_preselected
            )
            refreshed = agent._without_skill_protocol_tools(refreshed)
        if tool_use_context.coordinator_mode_enabled:
            coordinator = tool_use_context.coordinator_mode or agent._coordinator_mode
            if coordinator is not None:
                context["coordinator_worker_tools"] = list(refreshed)
                context["coordinator_worker_tools_context"] = (
                    coordinator.get_worker_tools_context(
                        refreshed,
                        context=context,
                    )
                )
                refreshed_preselected = coordinator.filter_coordinator_tools(
                    refreshed_preselected
                )
                refreshed = coordinator.filter_coordinator_tools(refreshed)
        refreshed_signature = agent._tool_set_signature(refreshed)
        if refreshed_signature != tool_set_signature:
            tools = refreshed
            tool_set_signature = refreshed_signature
            agent._last_tools = tools
            tool_use_context.discovered_tool_names.update(
                tool.name
                for tool in refreshed_preselected
                if getattr(tool, "is_deferred", False)
            )
            tool_use_context.all_tools = list(tools)
            configured_deferred_tool_names = context.get("policy_deferred_tool_names")
            if configured_deferred_tool_names is not None:
                tool_use_context.deferred_tool_names = {
                    str(name)
                    for name in configured_deferred_tool_names
                    if str(name) and str(name) not in tool_use_context.discovered_tool_names
                }
            else:
                tool_use_context.deferred_tool_names = set(
                    agent._deferred_tool_names(
                        tools,
                        discovered_tool_names=tool_use_context.discovered_tool_names,
                    )
                )
            active_tools = agent._build_active_tools(
                tools,
                discovered_tool_names=tool_use_context.discovered_tool_names,
                active_tool_names=context.get("active_tool_names"),
                deferred_tool_names=tool_use_context.deferred_tool_names,
            )
            active_tools = agent._filter_tools_for_permission_mode(
                active_tools,
                tool_use_context,
            )
            agent._sync_tool_use_context_runtime(
                tool_use_context,
                tools=active_tools,
                all_tools=tools,
            )
            tool_use_context.agent_definitions = refreshed_agent_definitions
            agent._bind_agent_tools_to_context(tools, tool_use_context)
            agent._bind_skill_tools_to_context(tools, tool_use_context)
            agent._append_skill_listing_delta(messages, tool_use_context)
            if agent.grounding_client:
                preselection_debug_info = (
                    agent.grounding_client.get_last_preselection_debug_info()
                )
            retrieved_tools_list = agent._build_retrieved_tools_list(
                tools,
                preselection_debug_info,
            )
            logger.info("Tools refreshed: %s tools available", len(tools))
    except Exception:
        pass

    return ToolRefreshResult(
        tools=tools,
        active_tools=active_tools,
        tool_set_signature=tool_set_signature,
        preselection_debug_info=preselection_debug_info,
        retrieved_tools_list=retrieved_tools_list,
    )


async def prepare_tools_for_model_call(
    turn: TurnControllerContext,
    *,
    messages: list[dict[str, Any]],
    tools: list[Any],
) -> ToolPreparationResult:
    agent = turn.agent
    context = turn.context
    tool_use_context = turn.tool_use_context
    state = turn.state
    configured_deferred_tool_names = context.get("policy_deferred_tool_names")
    if configured_deferred_tool_names is not None:
        policy_deferred_names = {str(name) for name in configured_deferred_tool_names}
        tool_use_context.deferred_tool_names = {
            name
            for name in policy_deferred_names
            if name and name not in tool_use_context.discovered_tool_names
        }
    else:
        tool_use_context.deferred_tool_names = set(
            agent._deferred_tool_names(
                tools,
                discovered_tool_names=tool_use_context.discovered_tool_names,
            )
        )
    active_tools = agent._build_active_tools(
        tools,
        discovered_tool_names=tool_use_context.discovered_tool_names,
        active_tool_names=context.get("active_tool_names"),
        deferred_tool_names=tool_use_context.deferred_tool_names,
    )
    active_tools = agent._filter_tools_for_permission_mode(
        active_tools,
        tool_use_context,
    )
    agent._sync_tool_use_context_runtime(
        tool_use_context,
        tools=active_tools,
        all_tools=tools,
        model=state.effective_model,
    )
    agent._bind_agent_tools_to_context(tools, tool_use_context)
    agent._bind_skill_tools_to_context(tools, tool_use_context)
    agent._refresh_primary_system_prompt(
        messages,
        cwd=context.get("workspace_dir"),
        deferred_tool_names=tool_use_context.deferred_tool_names,
        memory_mode=tool_use_context.memory_mode,
        skills_enabled=not tool_use_context.skills_disabled,
        skill_discovery_enabled=agent._has_discover_skills_tool(
            tool_use_context.tools
        ),
        permission_mode=tool_use_context.permission_mode,
        plan_file_path=tool_use_context.plan_file_path,
        response_style=tool_use_context.response_style,
        coordinator_mode=tool_use_context.coordinator_mode,
        coordinator_mode_enabled=tool_use_context.coordinator_mode_enabled,
    )
    turn_attachment_messages = await get_turn_attachment_messages_async(
        tool_use_context,
        messages,
        model=state.effective_model,
    )
    if turn_attachment_messages:
        messages.extend(turn_attachment_messages)
        agent._sync_tool_use_context_runtime(
            tool_use_context,
            messages=messages,
        )
    return ToolPreparationResult(messages=messages, active_tools=active_tools)


async def execute_tool_turn(
    turn: TurnControllerContext,
    *,
    messages: list[dict[str, Any]],
    tools: list[Any],
    active_tools: list[Any],
    model_response: Any,
    messages_input_snapshot: list[dict[str, Any]],
    pending_memory_prefetch: Any,
) -> ToolTurnResult:
    agent = turn.agent
    context = turn.context
    instruction = turn.instruction
    task_query = resolve_task_query(context, instruction)
    tool_use_context = turn.tool_use_context
    state = turn.state
    tools_result: RunToolsResult = await run_tools(
        tool_calls=model_response.tool_calls,
        tool_map=model_response.tool_map,
        context=tool_use_context,
        assistant_message=model_response.assistant_message,
    )

    state.all_tool_results.extend(
        agent._build_iteration_tool_results(
            tool_calls=model_response.tool_calls,
            tool_map=model_response.tool_map,
            result_messages=tools_result.messages,
            tools=tools,
        )
    )
    if model_response.tool_calls and state.max_output_tokens_recovery_count:
        logger.info(
            "Resetting max-output recovery count after tool execution "
            "(previous=%s)",
            state.max_output_tokens_recovery_count,
        )
        state.reset_max_output_recovery()
    bench_checker_guard.update_from_tool_turn(
        state,
        tool_calls=model_response.tool_calls,
        result_messages=tools_result.messages,
    )

    messages.extend(tools_result.messages)
    agent._sync_tool_use_context_runtime(tool_use_context, messages=messages)

    aggregate_tool_result_limit = context.get("max_tool_results_per_message_chars")
    try:
        aggregate_tool_result_limit = int(aggregate_tool_result_limit)
    except (TypeError, ValueError):
        aggregate_tool_result_limit = None
    budget_kwargs: dict[str, Any] = {}
    if aggregate_tool_result_limit and aggregate_tool_result_limit > 0:
        budget_kwargs["max_chars"] = aggregate_tool_result_limit

    messages = enforce_tool_result_budget(
        messages,
        results_dir=getattr(tool_use_context, "tool_results_dir", None),
        **budget_kwargs,
    )
    agent._sync_tool_use_context_runtime(tool_use_context, messages=messages)
    await session_policy.save_after_tool_result_budget(
        agent,
        tool_use_context,
        messages,
        model=state.effective_model,
    )

    if tools_result.updated_context is not None:
        tool_use_context = tools_result.updated_context
    context["response_style"] = tool_use_context.response_style
    context["task_manager"] = tool_use_context.task_manager
    context["coordinator_mode"] = tool_use_context.coordinator_mode
    context["coordinator_mode_enabled"] = bool(
        tool_use_context.coordinator_mode_enabled
    )
    context["coordinator_notification_queue"] = (
        tool_use_context.coordinator_notification_queue
    )
    context["coordinator_worker_tools"] = list(
        tool_use_context.coordinator_worker_tools or []
    )
    context_model = str(getattr(tool_use_context, "model", "") or "")
    if (
        context_model
        and context_model != state.effective_model
        and not tool_use_context.skill_model_override
    ):
        state.effective_model = context_model
        state.effective_fallback_model = getattr(
            agent._llm_client,
            "fallback_model",
            state.effective_fallback_model,
        )
    if tool_use_context.skill_model_override:
        state.effective_model = str(tool_use_context.skill_model_override)
    state.refresh_reasoning_effort(tool_use_context)
    active_tools = agent._filter_tools_for_permission_mode(
        active_tools,
        tool_use_context,
    )
    agent._sync_tool_use_context_runtime(
        tool_use_context,
        messages=messages,
        tools=active_tools,
        all_tools=tools,
        current_iteration=state.current_iteration,
        max_iterations=state.max_iterations,
        model=state.effective_model,
    )
    agent._bind_agent_tools_to_context(tools, tool_use_context)

    abort_stop_reason = stop_policy.abort_stop_reason(
        turn.abort_event,
        during_tool_use=True,
    )
    if abort_stop_reason:
        messages.append(build_user_interruption_message(tool_use=True))
        agent._sync_tool_use_context_runtime(tool_use_context, messages=messages)
        state.stop_reason_final = abort_stop_reason
        return ToolTurnResult(
            action="break",
            messages=messages,
            tool_use_context=tool_use_context,
            active_tools=active_tools,
        )

    if tools_result.prevent_continuation:
        state.stop_reason_final = tools_result.stop_reason or "hook_stopped"
        logger.info(
            "Tool hook prevented continuation: %s",
            state.stop_reason_final,
        )
        return ToolTurnResult(
            action="break",
            messages=messages,
            tool_use_context=tool_use_context,
            active_tools=active_tools,
        )

    nested_attachments = await consume_nested_memory_triggers(tool_use_context)
    if nested_attachments:
        nested_messages = [
            create_attachment_message(attachment)
            for attachment in nested_attachments
        ]
        messages.extend(nested_messages)
        agent._sync_tool_use_context_runtime(tool_use_context, messages=messages)
        await tool_use_context.emit_event(
            "nested_memory_consumed",
            {
                "iteration": state.current_iteration - 1,
                "attachment_count": len(nested_messages),
                "paths": [
                    str(attachment.get("path") or "")
                    for attachment in nested_attachments
                ],
            },
        )

    from openspace.skill_engine.protocol import consume_dynamic_skill_triggers

    dynamic_skill_messages = await consume_dynamic_skill_triggers(tool_use_context)
    if dynamic_skill_messages:
        messages.extend(dynamic_skill_messages)
        agent._sync_tool_use_context_runtime(tool_use_context, messages=messages)
        agent._append_skill_listing_delta(messages, tool_use_context)
        await tool_use_context.emit_event(
            "dynamic_skills_consumed",
            {
                "iteration": state.current_iteration - 1,
                "attachment_count": len(dynamic_skill_messages),
            },
        )

    discovery_query = await agent._build_post_tool_skill_discovery_query(
        task_query,
        messages,
        abort_event=turn.abort_event,
    )
    before_discovery_len = len(messages)
    await agent._append_skill_discovery_delta_async(
        messages,
        tool_use_context,
        query=discovery_query,
        source="post_tool_prefetch",
    )
    if len(messages) > before_discovery_len:
        await tool_use_context.emit_event(
            "skill_discovery_prefetch_consumed",
            {
                "iteration": state.current_iteration - 1,
                "attachment_count": len(messages) - before_discovery_len,
            },
        )

    memory_messages = await consume_relevant_memory_prefetch(
        pending_memory_prefetch,
        tool_use_context,
        iteration=state.current_iteration - 1,
    )
    if memory_messages:
        messages.extend(memory_messages)
        agent._sync_tool_use_context_runtime(tool_use_context, messages=messages)

    if state.compact_tracking.compacted:
        state.compact_tracking.turn_counter += 1

    delta_messages = messages[len(messages_input_snapshot):]
    response_metadata = {
        "has_tool_calls": bool(model_response.tool_calls),
        "tool_calls_count": len(model_response.tool_calls),
    }
    state.iteration_contexts.append(
        {
            "iteration": state.current_iteration,
            "messages_input": messages_input_snapshot,
            "messages_output": copy.deepcopy(messages),
            "response_metadata": response_metadata,
        }
    )
    from openspace.recording import RecordingManager

    await RecordingManager.record_iteration_context(
        iteration=state.current_iteration,
        delta_messages=copy.deepcopy(delta_messages),
        response_metadata=response_metadata,
    )

    await agent._emit(
        "iteration_end",
        {
            "iteration": state.current_iteration,
            "status": "continue",
            "tool_calls_count": len(model_response.tool_calls),
        },
    )

    if stop_policy.max_iterations_stop_reason(
        state.current_iteration,
        state.max_iterations,
    ):
        logger.warning("Reached max iterations (%s)", state.max_iterations)
        state.stop_reason_final = "max_turns"
        return ToolTurnResult(
            action="break",
            messages=messages,
            tool_use_context=tool_use_context,
            active_tools=active_tools,
        )

    return ToolTurnResult(
        action="continue",
        messages=messages,
        tool_use_context=tool_use_context,
        active_tools=active_tools,
    )
