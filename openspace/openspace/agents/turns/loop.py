"""Single-turn LLM/tool loop for GroundingAgent."""

from __future__ import annotations

import asyncio
import copy
import os
import time
from contextlib import nullcontext
from typing import Any

from openspace.agents.turns import compaction_controller
from openspace.agents.turns import events as turn_events
from openspace.agents.turns import model_call_controller
from openspace.agents.turns import stop_policy
from openspace.agents.turns import tool_turn_controller
from openspace.agents.turns.context import TurnControllerContext
from openspace.agents.turns.state import TurnState
from openspace.agents.turns.task_query import resolve_task_query
from openspace.services.memory.recall import start_relevant_memory_prefetch
from openspace.services.conversation.messages import extract_discovered_tool_names
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _maybe_build_bench_finalize_nudge(state: TurnState) -> dict[str, Any] | None:
    if not _env_bool("OPENSPACE_BENCH_FINALIZE_NUDGE_ENABLED"):
        return None

    max_nudges = max(0, _env_int("OPENSPACE_BENCH_FINALIZE_NUDGE_MAX", 1))
    if state.bench_finalize_nudge_count >= max_nudges:
        return None

    elapsed_s = time.monotonic() - state.started_at_monotonic
    after_sec = _env_int("OPENSPACE_BENCH_FINALIZE_NUDGE_AFTER_SEC", 0)
    after_iteration = _env_int("OPENSPACE_BENCH_FINALIZE_NUDGE_AFTER_ITERATION", 0)

    due_by_time = after_sec > 0 and elapsed_s >= after_sec
    due_by_iteration = (
        after_iteration > 0
        and state.current_iteration >= after_iteration
    )
    if not due_by_time and not due_by_iteration:
        return None

    state.bench_finalize_nudge_count += 1
    state.bench_finalize_nudge_iteration = state.current_iteration
    state.bench_finalize_nudge_monotonic = time.monotonic()
    return {
        "role": "user",
        "content": (
            "Terminal-Bench finalization checkpoint: the run budget is getting "
            "low. Immediately preserve the best current answer in the exact "
            "requested /app artifact path(s). If you have found a passing "
            "payload, script, data file, or command output, write it to the "
            "target artifact now. Run at most one quick verification against "
            "the visible checker or artifact parseability, then stop open-ended "
            "exploration and provide the final response."
        ),
        "_meta": {
            "type": "bench_finalize_nudge",
            "is_meta": True,
            "elapsed_s": round(elapsed_s, 3),
            "iteration": state.current_iteration,
        },
    }


def _bench_finalize_budget_exhausted(state: TurnState) -> tuple[bool, str]:
    if not _env_bool("OPENSPACE_BENCH_FINALIZE_NUDGE_ENABLED"):
        return False, ""
    if state.bench_finalize_nudge_count <= 0:
        return False, ""

    stop_after_iterations = _env_int(
        "OPENSPACE_BENCH_FINALIZE_STOP_AFTER_ITERATIONS",
        0,
    )
    if (
        stop_after_iterations > 0
        and state.bench_finalize_nudge_iteration is not None
        and state.current_iteration
        > state.bench_finalize_nudge_iteration + stop_after_iterations
    ):
        return (
            True,
            f"{stop_after_iterations} iterations after finalize nudge",
        )

    stop_after_sec = _env_int("OPENSPACE_BENCH_FINALIZE_STOP_AFTER_SEC", 0)
    if (
        stop_after_sec > 0
        and state.bench_finalize_nudge_monotonic is not None
        and time.monotonic() - state.bench_finalize_nudge_monotonic >= stop_after_sec
    ):
        return True, f"{stop_after_sec}s after finalize nudge"

    return False, ""


def _bench_checker_pass_budget_exhausted(state: TurnState) -> tuple[bool, str]:
    stop_after_iterations = _env_int(
        "OPENSPACE_BENCH_STOP_AFTER_CHECKER_PASS_ITERATIONS",
        0,
    )
    pass_iteration = getattr(state, "bench_visible_checker_pass_iteration", None)
    if (
        stop_after_iterations > 0
        and pass_iteration is not None
        and not bool(getattr(state, "bench_visible_checker_failed", False))
        and state.current_iteration > pass_iteration + stop_after_iterations
    ):
        return True, f"{stop_after_iterations} iterations after visible checker pass"
    return False, ""


def _append_lifecycle_hook_contexts(
    context: dict[str, Any],
    hook_result: Any,
) -> None:
    additional_contexts = getattr(hook_result, "additional_contexts", None)
    if additional_contexts:
        target = context.setdefault("hook_additional_contexts", [])
        if isinstance(target, list):
            target.extend(str(item) for item in additional_contexts if item)
    initial_user_message = getattr(hook_result, "initial_user_message", None)
    if initial_user_message:
        context["session_start_initial_user_message"] = str(initial_user_message)
    watch_paths = getattr(hook_result, "watch_paths", None)
    if watch_paths:
        target = context.setdefault("hook_watch_paths", [])
        if isinstance(target, list):
            target.extend(str(path) for path in watch_paths if path)


def _reserve_session_start_hooks(
    agent: Any,
    *,
    session_id: str | None,
    source: str,
) -> bool:
    key = f"{session_id or 'anonymous'}:{source}"
    seen = getattr(agent, "_session_start_hooks_seen", None)
    if not isinstance(seen, set):
        seen = set()
        setattr(agent, "_session_start_hooks_seen", seen)
    if key in seen:
        return False
    seen.add(key)
    return True


async def _emit_lifecycle_hook_messages(agent: Any, hook_result: Any) -> None:
    for message in getattr(hook_result, "messages", []) or []:
        await agent._emit_runtime_event(
            "hook_message",
            {"message": message},
        )
    for message in getattr(hook_result, "blocking_errors", []) or []:
        await agent._emit_runtime_event(
            "hook_message",
            {"message": message},
        )


async def run_grounding_turn(agent: Any, context: dict[str, Any]) -> dict[str, Any]:
    """OpenSpace external orchestration loop.

    Uses ``call_model()`` for LLM calls and ``run_tools()`` for tool execution.
    Implementation: ``query.ts`` ``queryLoop`` (L241-1729).

    Flow per iteration:
        1. abort check
        2. drain external messages (multi-agent inbox)
        3. auto_compact_if_needed (LLM summary compression)
        4. call_model (pure LLM, no tool execution)
        5. handle errors: PTL → compact retry, Fallback → model switch
        6. no tool_calls → stop hooks → break/continue
        7. run_tools (parallel/serial orchestration)
        8. enforce tool result budget
        9. prevent_continuation check
       10. iteration guidance injection → continue
    """
    self = agent
    instruction = context.get("instruction", "")
    if not instruction:
        logger.error("Grounding Agent: No instruction provided")
        return {"error": "No instruction provided", "status": "error"}

    self._current_instruction = instruction
    tool_retrieval_instruction = resolve_task_query(context, instruction)

    logger.info(f"Grounding Agent: Processing instruction at step {self.step}")
    agent_id = str(context.get("agent_id") or "primary")
    await self._emit_runtime_event(
        "agent_start",
        turn_events.agent_start_payload(
            self,
            context,
            agent_id=agent_id,
            instruction=instruction,
        ),
    )

    memory_mode = self._resolve_memory_mode(context.get("memory_mode"))
    context["memory_mode"] = memory_mode
    skills_disabled = self._skills_disabled_for_context(context)
    low_latency_profiler = context.get("low_latency_profiler")

    def _latency_span(name: str, **metadata: Any):
        if low_latency_profiler is None:
            return nullcontext()
        span = getattr(low_latency_profiler, "span", None)
        if not callable(span):
            return nullcontext()
        return span(name, **metadata)

    # ── Phase 0: Pre-loop setup (workspace, tools, messages) ──────

    with _latency_span("turn.workspace_scan"):
        workspace_info = await self._check_workspace_artifacts(context)
    if workspace_info["has_files"]:
        context["workspace_artifacts"] = workspace_info
        logger.info(f"Workspace has {len(workspace_info['files'])} existing files: {workspace_info['files']}")

    from openspace.services.runtime_support.low_latency import (
        SessionCapabilityState,
        build_active_tool_policy,
        classify_capability_profile,
    )

    session_capability_state_enabled = bool(
        context.get("session_capability_state_enabled", True)
    )
    session_capability_state = (
        SessionCapabilityState.from_mapping(
            context.get("session_capability_state")
        )
        if session_capability_state_enabled
        else SessionCapabilityState()
    )
    if (
        session_capability_state_enabled
        and session_capability_state.visible_skill_names
        and not context.get("sent_skill_names_by_agent")
    ):
        context["sent_skill_names_by_agent"] = {
            "primary": set(session_capability_state.visible_skill_names)
        }

    requested_profile = str(context.get("capability_profile") or "batch_full")
    classified_profile = classify_capability_profile(
        instruction,
        default_profile=requested_profile,
    )
    context["capability_profile"] = classified_profile.name
    if low_latency_profiler is not None:
        try:
            low_latency_profiler.profile = classified_profile.name
        except Exception:
            pass
    use_fast_tool_policy = bool(
        context.get("low_latency_enabled")
        and context.get("fast_tool_policy_enabled")
        and classified_profile.name == "interactive_fast"
        and context.get("disable_fast_auto_preselection", True)
    )
    active_tool_policy = None

    pre_filtered_tools = context.get("pre_filtered_tools")
    if pre_filtered_tools is not None:
        preselected_tools = list(pre_filtered_tools)
        tools = list(context.get("all_tools") or preselected_tools)
        tools = self._with_memory_mode_tools(tools, memory_mode)
        agent_definitions = self._resolve_agent_definitions(context, tools)
    elif use_fast_tool_policy:
        with _latency_span("turn.tool_universe", policy="hard_allowlist"):
            tools = await self._get_tools_without_auto_preselection()
        tools = self._with_memory_mode_tools(tools, memory_mode)
        agent_definitions = self._resolve_agent_definitions(context, tools)
        if self._should_append_agent_tools(context):
            tools = self._with_agent_tool(
                tools,
                context=context,
                agent_definitions=agent_definitions,
            )
        active_tool_policy = build_active_tool_policy(
            profile_name=classified_profile.name,
            instruction=tool_retrieval_instruction,
            tools=tools,
            hard_active_tool_limit=context.get("hard_active_tool_limit"),
        )
        preselected_tools = [
            tool
            for tool in tools
            if getattr(tool, "name", None) in active_tool_policy.active_tool_names
        ]
    else:
        with _latency_span("turn.tool_preselection"):
            preselected_tools = await self._get_available_tools(
                tool_retrieval_instruction
            )
        with _latency_span("turn.tool_universe"):
            tools = await self._get_tool_universe(preselected_tools)
        tools = self._with_memory_mode_tools(tools, memory_mode)
        agent_definitions = self._resolve_agent_definitions(context, tools)
        if self._should_append_agent_tools(context):
            tools = self._with_agent_tool(
                tools,
                context=context,
                agent_definitions=agent_definitions,
            )
    if skills_disabled:
        preselected_tools = self._without_skill_protocol_tools(preselected_tools)
        tools = self._without_skill_protocol_tools(tools)
    coordinator = context.get("coordinator_mode") or self._coordinator_mode
    coordinator_enabled = bool(
        pre_filtered_tools is None
        and coordinator is not None
        and coordinator.is_enabled(context)
    )
    context["coordinator_mode_enabled"] = coordinator_enabled
    if coordinator_enabled:
        context["coordinator_worker_tools"] = list(tools)
        context["coordinator_worker_tools_context"] = coordinator.get_worker_tools_context(
            tools,
            context=context,
        )
        notification_queue = context.get("coordinator_notification_queue")
        if not isinstance(notification_queue, asyncio.Queue):
            notification_queue = asyncio.Queue()
            context["coordinator_notification_queue"] = notification_queue
        context.setdefault("message_source", notification_queue)
        tools = coordinator.filter_coordinator_tools(tools)
        preselected_tools = coordinator.filter_coordinator_tools(preselected_tools)
    context["agent_definitions"] = agent_definitions
    self._last_tools = tools
    tool_set_signature = self._tool_set_signature(tools)
    configured_active_tool_names = context.get("active_tool_names")
    hard_active_tool_names = (
        {str(name) for name in configured_active_tool_names if str(name)}
        if configured_active_tool_names is not None
        else None
    )
    discovered_tool_names = set(context.get("discovered_tool_names") or ())
    if session_capability_state_enabled:
        discovered_tool_names.update(
            session_capability_state.discovered_tool_names
        )
    discovered_tool_names.update(
        extract_discovered_tool_names(context.get("conversation_history") or [])
    )
    if active_tool_policy is None:
        if hard_active_tool_names is None:
            discovered_tool_names.update(
                tool.name
                for tool in preselected_tools
                if getattr(tool, "is_deferred", False)
            )
        configured_deferred_tool_names = context.get("policy_deferred_tool_names")
        if configured_deferred_tool_names is not None:
            deferred_tool_names = sorted(
                str(name)
                for name in configured_deferred_tool_names
                if str(name) and str(name) not in discovered_tool_names
            )
        else:
            deferred_tool_names = self._deferred_tool_names(
                tools,
                discovered_tool_names=discovered_tool_names,
            )
        active_tool_names = hard_active_tool_names
    else:
        deferred_tool_names = sorted(
            name
            for name in active_tool_policy.deferred_tool_names
            if name not in discovered_tool_names
        )
        active_tool_names = set(active_tool_policy.active_tool_names)
        context["active_tool_policy_reason"] = active_tool_policy.reason
        context["policy_deferred_tool_names"] = set(
            active_tool_policy.deferred_tool_names
        )
    context["deferred_tool_names"] = deferred_tool_names
    context["discovered_tool_names"] = discovered_tool_names
    context["all_tools"] = tools
    if active_tool_names is not None:
        context["active_tool_names"] = set(active_tool_names)
    active_tools = self._build_active_tools(
        tools,
        discovered_tool_names=discovered_tool_names,
        active_tool_names=active_tool_names,
        deferred_tool_names=deferred_tool_names,
    )
    context["skill_tool_available"] = bool(
        not skills_disabled and self._has_skill_tool(active_tools)
    )
    context["discover_skills_tool_available"] = bool(
        not skills_disabled and self._has_discover_skills_tool(active_tools)
    )
    marker = getattr(low_latency_profiler, "mark", None)
    if callable(marker):
        marker(
            "turn.schema_build",
            active_schema_count=len(active_tools),
            all_tools_count=len(tools),
            deferred_tools_count=len(deferred_tool_names),
        )

    preselection_debug_info = None
    if self.grounding_client and pre_filtered_tools is None:
        preselection_debug_info = self.grounding_client.get_last_preselection_debug_info()

    retrieved_tools_list = self._build_retrieved_tools_list(tools, preselection_debug_info)

    if self._recording_manager:
        from openspace.recording import RecordingManager
        await RecordingManager.record_retrieved_tools(
            task_instruction=tool_retrieval_instruction,
            tools=tools,
            preselection_debug_info=preselection_debug_info,
        )

    max_iterations = context.get("max_iterations", self._max_iterations)
    abort_event: asyncio.Event | None = context.get("abort_event")
    if abort_event is not None and not isinstance(abort_event, asyncio.Event):
        abort_event = None
    message_source = context.get("message_source")
    async_rewake_queue = context.get("async_rewake_queue")
    if not isinstance(async_rewake_queue, asyncio.Queue):
        async_rewake_queue = asyncio.Queue()
        context["async_rewake_queue"] = async_rewake_queue

    if context.get("permission_mode") == "plan" and not context.get("plan_file_path"):
        try:
            from openspace.services.runtime_support.plan_mode import get_plan_file_path

            context["plan_file_path"] = str(
                get_plan_file_path(context.get("session_id"), context.get("agent_id", "primary"))
            )
        except Exception:
            pass

    with _latency_span("turn.message_build"):
        messages = self.construct_messages(context)

    try:
        tool_use_context = self._create_tool_use_context(
            tools=active_tools,
            messages=messages,
            context=context,
        )
    except Exception as e:
        logger.error(f"Grounding Agent: Execution failed: {e}")
        await self._emit_runtime_event(
            "agent_error",
            turn_events.agent_error_payload(
                self,
                context,
                agent_id=agent_id,
                error=e,
            ),
        )
        self.increment_step()
        return {
            "error": str(e),
            "status": "error",
            "instruction": instruction,
            "iteration": 0,
        }

    self._sync_tool_use_context_runtime(
        tool_use_context,
        messages=messages,
        tools=active_tools,
        all_tools=tools,
        current_iteration=0,
        max_iterations=max_iterations,
        model=getattr(self._llm_client, "model", "unknown"),
    )
    initial_context_modifier = context.get("initial_tool_use_context_modifier")
    if callable(initial_context_modifier):
        tool_use_context = initial_context_modifier(tool_use_context)
    active_tools = self._filter_tools_for_permission_mode(active_tools, tool_use_context)
    self._sync_tool_use_context_runtime(tool_use_context, tools=active_tools)
    self._bind_agent_tools_to_context(tools, tool_use_context)
    self._bind_skill_tools_to_context(tools, tool_use_context)

    try:
        from openspace.services.tooling.hooks import (
            run_session_end_hooks,
            run_session_start_hooks,
            run_user_prompt_submit_hooks,
        )

        raw_session_start_source = context.get("session_start_source")
        should_run_session_start = (
            raw_session_start_source is not None
            or not tool_use_context.session_id
        )
        if should_run_session_start:
            session_start_source = str(raw_session_start_source or "startup")
            if session_start_source not in {"startup", "resume", "clear", "compact"}:
                session_start_source = "startup"
            if _reserve_session_start_hooks(
                self,
                session_id=(
                    tool_use_context.session_id
                    or str(context.get("task_id") or id(context))
                ),
                source=session_start_source,
            ):
                session_start_result = await run_session_start_hooks(
                    tool_use_context.hook_registry,
                    source=session_start_source,  # type: ignore[arg-type]
                    context=tool_use_context,
                    session_id=tool_use_context.session_id,
                    agent_type=tool_use_context.agent_type,
                    model=tool_use_context.model,
                )
                _append_lifecycle_hook_contexts(context, session_start_result)
                await _emit_lifecycle_hook_messages(self, session_start_result)
                with _latency_span("turn.message_build.lifecycle"):
                    messages = self.construct_messages(context)
                self._sync_tool_use_context_runtime(
                    tool_use_context,
                    messages=messages,
                )

        prompt_submit_result = await run_user_prompt_submit_hooks(
            tool_use_context.hook_registry,
            str(instruction),
            context=tool_use_context,
            permission_mode=tool_use_context.permission_mode,
        )
        _append_lifecycle_hook_contexts(context, prompt_submit_result)
        await _emit_lifecycle_hook_messages(self, prompt_submit_result)
        if (
            prompt_submit_result.blocking_errors
            or prompt_submit_result.prevent_continuation
        ):
            reason = (
                prompt_submit_result.stop_reason
                or "UserPromptSubmit hook blocked execution"
            )
            await run_session_end_hooks(
                tool_use_context.hook_registry,
                reason,
                context=tool_use_context,
            )
            self.increment_step()
            messages = list(prompt_submit_result.blocking_errors)
            if not messages:
                messages.append(
                    {
                        "role": "system",
                        "content": reason,
                        "_meta": {
                            "type": "hook_blocking_error",
                            "hook_event": "UserPromptSubmit",
                        },
                    }
                )
            return {
                "status": "blocked",
                "response": reason,
                "stop_reason": "user_prompt_submit_hook_blocked",
                "instruction": instruction,
                "iteration": 0,
                "iterations": 0,
                "tool_executions": [],
                "messages": messages,
            }
    except Exception as e:
        logger.error(f"Grounding Agent: Lifecycle hooks failed: {e}")
        await self._emit_runtime_event(
            "agent_error",
            turn_events.agent_error_payload(
                self,
                context,
                agent_id=agent_id,
                error=e,
            ),
        )
        self.increment_step()
        return {
            "error": str(e),
            "status": "error",
            "instruction": instruction,
            "iteration": 0,
        }

    with _latency_span("turn.message_build"):
        messages = self.construct_messages(context)

    self._sync_tool_use_context_runtime(tool_use_context, messages=messages)
    self._append_agent_listing_delta(messages, tool_use_context)
    if self._skill_registry:
        from openspace.skill_engine.protocol import restore_skill_state_from_messages

        restored = restore_skill_state_from_messages(messages, tool_use_context)
        if any(restored.values()):
            logger.info("Restored skill protocol state from transcript: %s", restored)
    with _latency_span("turn.skill_listing"):
        self._append_skill_listing_delta(messages, tool_use_context)
    with _latency_span("turn.skill_discovery"):
        await self._append_skill_discovery_delta_async(
            messages,
            tool_use_context,
            query=tool_retrieval_instruction,
            source="turn0_prefetch",
        )

    from openspace.recording import RecordingManager
    await RecordingManager.record_conversation_setup(
        setup_messages=copy.deepcopy(messages),
        tools=active_tools,
    )

    # ── Phase 1: Main loop (OpenSpace queryLoop while(true)) ─────────────

    state = TurnState.from_agent_context(
        self,
        context,
        str(instruction),
        tool_use_context,
        max_iterations=max_iterations,
    )
    turn_context = TurnControllerContext(
        agent=self,
        context=context,
        tool_use_context=tool_use_context,
        state=state,
        abort_event=abort_event,
        instruction=str(instruction),
        agent_id=agent_id,
        low_latency_profiler=low_latency_profiler,
        latency_span=_latency_span,
    )

    # OpenSpace query.ts starts relevant memory prefetch once per user turn before
    # the loop.  The handle is only consumed after tools finish and only if
    # it has settled, so the recall side query never blocks the main turn.
    pending_memory_prefetch = start_relevant_memory_prefetch(
        messages,
        tool_use_context,
        llm_client=self._tool_retrieval_llm or self._llm_client,
        enabled=context.get("memory_recall_enabled"),
        model=context.get("memory_recall_model"),
    )

    try:
        while state.current_iteration < state.max_iterations:
            current_iteration = state.begin_iteration()
            logger.info(
                "Grounding Agent: Iteration %s/%s",
                current_iteration,
                state.max_iterations,
            )
            self._sync_tool_use_context_runtime(
                tool_use_context,
                messages=messages,
                tools=active_tools,
                all_tools=tools,
                current_iteration=current_iteration,
                max_iterations=state.max_iterations,
                model=state.effective_model,
            )

            await self._emit(
                "iteration_start",
                turn_events.iteration_start_payload(
                    current_iteration,
                    state.max_iterations,
                ),
            )
            await self._emit_runtime_event(
                "agent_progress",
                turn_events.agent_progress_payload(
                    self,
                    context,
                    agent_id=agent_id,
                    iteration=current_iteration,
                    max_iterations=state.max_iterations,
                ),
            )

            # ── 2a. Abort check (Implementation: abortController.signal.aborted) ─
            abort_stop_reason = stop_policy.abort_stop_reason(abort_event)
            if abort_stop_reason:
                logger.info("Agent aborted by external signal before call_model")
                from openspace.services.conversation.messages import build_user_interruption_message

                messages.append(build_user_interruption_message(tool_use=False))
                state.stop_reason_final = abort_stop_reason
                break

            bench_budget_exhausted, bench_budget_reason = (
                _bench_finalize_budget_exhausted(state)
            )
            if bench_budget_exhausted:
                state.stop_reason_final = "bench_finalize_budget"
                logger.info(
                    "Bench finalize budget exhausted: %s",
                    bench_budget_reason,
                )
                break

            checker_pass_budget_exhausted, checker_pass_budget_reason = (
                _bench_checker_pass_budget_exhausted(state)
            )
            if checker_pass_budget_exhausted:
                state.stop_reason_final = "bench_checker_pass_budget"
                logger.info(
                    "Bench checker-pass budget exhausted: %s",
                    checker_pass_budget_reason,
                )
                break

            # ── 2b. Drain external messages (Implementation: getCommandsByMaxPriority) ─
            rewake_injected = await self._drain_messages(async_rewake_queue)
            if rewake_injected:
                messages.extend(rewake_injected)
                self._sync_tool_use_context_runtime(
                    tool_use_context,
                    messages=messages,
                )

            if message_source:
                injected = await self._drain_messages(message_source)
                if injected:
                    messages.extend(injected)
                    self._sync_tool_use_context_runtime(
                        tool_use_context,
                        messages=messages,
                    )

            bench_finalize_nudge = _maybe_build_bench_finalize_nudge(state)
            if bench_finalize_nudge:
                messages.append(bench_finalize_nudge)
                self._sync_tool_use_context_runtime(
                    tool_use_context,
                    messages=messages,
                )
                logger.info(
                    "Bench finalize nudge %s injected at iteration %s",
                    state.bench_finalize_nudge_count,
                    current_iteration,
                )

            # ── 2b½. Time-based microcompact (Implementation: microcompactMessages
            #    → maybeTimeBasedMicrocompact, runs BEFORE autoCompact).
            #    Pre-processing layer: content-clear old tool results when
            #    gap since last assistant exceeds threshold. No LLM call.
            mc_decision = await compaction_controller.maybe_time_based_microcompact(
                messages,
                tool_use_context,
                query_source="main_thread",
            )
            if mc_decision.was_cleared:
                messages = mc_decision.messages
                self._sync_tool_use_context_runtime(
                    tool_use_context,
                    messages=messages,
                )

            await compaction_controller.emit_token_warning(
                messages,
                tool_use_context,
                model=state.effective_model,
            )

            # ── 2c. Auto compact (Implementation: deps.autocompact) ──────────────
            compact_decision = await compaction_controller.maybe_auto_compact(
                self,
                messages,
                tool_use_context,
                model=state.effective_model,
                tracking=state.compact_tracking,
                cwd=context.get("workspace_dir"),
            )
            if compact_decision.was_compacted:
                messages = compact_decision.messages
                self._sync_tool_use_context_runtime(
                    tool_use_context,
                    messages=messages,
                )
                if compact_decision.reset_max_output_recovery:
                    state.reset_max_output_recovery()

            refresh = await tool_turn_controller.refresh_tools_for_iteration(
                turn_context,
                messages=messages,
                tools=tools,
                active_tools=active_tools,
                pre_filtered_tools=pre_filtered_tools,
                use_fast_tool_policy=use_fast_tool_policy,
                tool_set_signature=tool_set_signature,
                preselection_debug_info=preselection_debug_info,
                retrieved_tools_list=retrieved_tools_list,
            )
            tools = refresh.tools
            active_tools = refresh.active_tools
            tool_set_signature = refresh.tool_set_signature
            preselection_debug_info = refresh.preselection_debug_info
            retrieved_tools_list = refresh.retrieved_tools_list

            preparation = await tool_turn_controller.prepare_tools_for_model_call(
                turn_context,
                messages=messages,
                tools=tools,
            )
            messages = preparation.messages
            active_tools = preparation.active_tools
            messages_input_snapshot = copy.deepcopy(messages)

            model_call = await model_call_controller.call_model_with_recovery(
                turn_context,
                messages=messages,
                active_tools=active_tools,
            )
            messages = model_call.messages
            if model_call.action == "retry":
                continue
            if model_call.action == "break":
                break
            response = await model_call_controller.handle_model_response(
                turn_context,
                model_response=model_call.model_response,
                messages=messages,
            )
            messages = response.messages
            if response.action == "continue":
                continue
            if response.action == "break":
                break

            tool_turn = await tool_turn_controller.execute_tool_turn(
                turn_context,
                messages=messages,
                tools=tools,
                active_tools=active_tools,
                model_response=model_call.model_response,
                messages_input_snapshot=messages_input_snapshot,
                pending_memory_prefetch=pending_memory_prefetch,
            )
            messages = tool_turn.messages
            tool_use_context = tool_turn.tool_use_context
            turn_context.tool_use_context = tool_use_context
            active_tools = tool_turn.active_tools
            if tool_turn.action == "break":
                break

            continue  # next iteration

        # ── Phase 4: Result assembly ──────────────────────────────────

        await self._emit(
            "status_update",
            turn_events.status_update_payload(
                total_iterations=state.current_iteration,
                total_tool_calls=len(state.all_tool_results),
            ),
        )

        result = await self._build_final_result(
            instruction=instruction,
            messages=messages,
            all_tool_results=state.all_tool_results,
            iterations=state.current_iteration,
            max_iterations=state.max_iterations,
            iteration_contexts=state.iteration_contexts,
            retrieved_tools_list=retrieved_tools_list,
            preselection_debug_info=preselection_debug_info,
            stop_reason=state.stop_reason_final,
        )

        result["permission_mode"] = tool_use_context.permission_mode
        result["pre_plan_mode"] = tool_use_context.pre_plan_mode
        result["plan_file_path"] = tool_use_context.plan_file_path
        result["plan_mode_exited_in_session"] = tool_use_context.plan_mode_exited_in_session
        if session_capability_state_enabled:
            result["session_capability_state"] = (
                self._build_session_capability_state_payload(
                    context,
                    tool_use_context,
                    active_tools=active_tools,
                    profile_name=context.get("capability_profile"),
                )
            )
        schema_cache_telemetry = self._summarize_tool_schema_cache_events(
            tool_use_context
        )
        if schema_cache_telemetry:
            result["tool_schema_cache_telemetry"] = schema_cache_telemetry
            marker = getattr(low_latency_profiler, "mark", None)
            if callable(marker):
                marker("turn.schema_cache", **schema_cache_telemetry)

        if self._recording_manager:
            await self._record_agent_execution(result, instruction)

        try:
            from openspace.services.tooling.hooks import run_session_end_hooks

            await run_session_end_hooks(
                tool_use_context.hook_registry,
                state.stop_reason_final or str(result.get("status") or "completed"),
                context=tool_use_context,
            )
        except Exception:
            logger.debug("SessionEnd hooks failed", exc_info=True)

        self.increment_step()

        logger.info(
            "Grounding Agent: Execution completed: %s (reason=%s)",
            result.get("status"),
            state.stop_reason_final,
        )
        await self._emit_runtime_event(
            "agent_complete",
            turn_events.agent_complete_payload(
                self,
                context,
                agent_id=agent_id,
                result=result,
                iterations=state.current_iteration,
                tool_calls=len(state.all_tool_results),
            ),
        )
        return result

    except Exception as e:
        logger.error(f"Grounding Agent: Execution failed: {e}")
        try:
            from openspace.services.tooling.hooks import (
                run_session_end_hooks,
                run_stop_failure_hooks,
            )

            await run_stop_failure_hooks(
                tool_use_context.hook_registry,
                e,
                error_details=str(e),
                last_assistant_message=self._extract_last_assistant_message(messages),
                context=tool_use_context,
            )
            await run_session_end_hooks(
                tool_use_context.hook_registry,
                "error",
                context=tool_use_context,
            )
        except Exception:
            logger.debug("Failure lifecycle hooks failed", exc_info=True)
        await self._emit_runtime_event(
            "agent_error",
            turn_events.agent_error_payload(
                self,
                context,
                agent_id=agent_id,
                error=e,
            ),
        )
        result = {
            "error": str(e),
            "status": "error",
            "instruction": instruction,
            "iteration": state.current_iteration,
        }
        self.increment_step()
        return result

    finally:
        try:
            tool_use_context.deactivate_all_skill_scopes()
        except Exception:
            logger.debug("Failed to deactivate skill scopes", exc_info=True)
        if pending_memory_prefetch is not None:
            pending_memory_prefetch.cancel()
