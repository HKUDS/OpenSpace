"""Model-call and model-response control flow for GroundingAgent turns."""

from __future__ import annotations

import copy
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from openspace.agents.turns import events as turn_events
from openspace.agents.turns.compaction_controller import persist_compacted_session_messages
from openspace.agents.turns import (
    bench_checker_guard,
    session_policy,
    stop_policy,
)
from openspace.agents.turns.context import TurnControllerContext
from openspace.llm.errors import (
    CannotRetryError,
    FallbackTriggeredError,
    PromptTooLongError,
    classify_api_error,
    get_error_message_for_user,
    is_abort_error,
)
from openspace.llm.types import ModelResponse
from openspace.services.conversation.compact import (
    build_post_compact_messages,
    compact_conversation,
    run_post_compact_cleanup,
)
from openspace.services.session.recovery import recover_conversation
from openspace.services.conversation.messages import (
    build_assistant_api_error_message,
    build_user_interruption_message,
)
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

_PENDING_ACTION_FINAL_RE = re.compile(
    r"(?ix)"
    r"\b(?:"
    r"let\s+me|"
    r"let['’]?s|"
    r"i(?:'|’)?ll|"
    r"i\s+will|"
    r"i\s+am\s+going\s+to|"
    r"i['’]?m\s+going\s+to|"
    r"we(?:'|’)?ll|"
    r"we\s+will|"
    r"next\s*,?\s+i(?:'|’)?ll|"
    r"next\s*,?\s+i\s+will|"
    r"now\s+i(?:'|’)?ll|"
    r"now\s+i\s+will"
    r")\s+"
    r"(?:"
    r"show|run|execute|check|verify|inspect|look|find|merge|"
    r"cherry[- ]?pick|apply|edit|write|create|modify|fix|update|"
    r"install|build|test|commit|resolve"
    r")\b"
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _require_tool_use_nudge_limit() -> int:
    raw = os.environ.get("OPENSPACE_REQUIRE_TOOL_USE_MAX_NUDGES", "2")
    try:
        return max(0, int(raw))
    except ValueError:
        return 2


def _bench_no_tool_final_nudge_limit() -> int:
    raw = os.environ.get("OPENSPACE_BENCH_NO_TOOL_FINAL_MAX_NUDGES", "2")
    try:
        return max(0, int(raw))
    except ValueError:
        return 2


def _bench_pending_action_final_nudge_limit() -> int:
    raw = os.environ.get("OPENSPACE_BENCH_PENDING_ACTION_FINAL_MAX_NUDGES", "2")
    try:
        return max(0, int(raw))
    except ValueError:
        return 2


def _should_force_tool_on_max_output_recovery() -> bool:
    return _env_bool("OPENSPACE_FORCE_TOOL_ON_MAX_OUTPUT_RECOVERY", False)


def _looks_like_pending_action_final(content: Any) -> bool:
    if not isinstance(content, str):
        return False
    text = " ".join(content.split())
    if not text:
        return False
    return bool(_PENDING_ACTION_FINAL_RE.search(text))


def _should_block_bench_pending_action_final(
    state: Any,
    assistant_content: Any,
    *,
    has_tool_calls: bool,
) -> bool:
    if not _env_bool("OPENSPACE_BENCH_PENDING_ACTION_FINAL_GUARD", False):
        return False
    if has_tool_calls or state.current_iteration >= state.max_iterations:
        return False
    return _looks_like_pending_action_final(assistant_content)


def _build_bench_pending_action_final_nudge_message(
    assistant_content: Any,
) -> dict[str, Any]:
    excerpt = ""
    if isinstance(assistant_content, str):
        excerpt = " ".join(assistant_content.split())[:360]
    suffix = f"\n\nPrevious message excerpt: {excerpt}" if excerpt else ""
    return {
        "role": "user",
        "content": (
            "Your last message described a pending action but did not call a "
            "tool. Continue now by using the shell/file tools to actually "
            "perform the pending action under /app. Do not provide a final "
            "response until the requested files are changed and a concrete "
            "check has been run or you have inspected why no checker exists."
            f"{suffix}"
        ),
        "_meta": {
            "type": "bench_pending_action_final_nudge",
            "is_meta": True,
        },
    }


def _should_block_bench_no_tool_final(state: Any) -> bool:
    if not _env_bool("OPENSPACE_BENCH_STRICT_NO_TOOL_FINAL", False):
        return False
    if state.current_iteration >= state.max_iterations:
        return False

    nudge_iteration = getattr(state, "bench_finalize_nudge_iteration", None)
    if nudge_iteration is None:
        return False

    last_tool_iteration = getattr(state, "bench_finalize_last_tool_iteration", None)
    return last_tool_iteration is None or last_tool_iteration <= nudge_iteration


def _bench_no_tool_final_fallback_model(state: Any) -> str | None:
    fallback_model = str(getattr(state, "effective_fallback_model", "") or "").strip()
    current_model = str(getattr(state, "effective_model", "") or "").strip()
    if not fallback_model or fallback_model == current_model:
        return None
    return fallback_model


def _build_bench_no_tool_final_nudge_message() -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            "Terminal-Bench finalization still needs a tool-backed fallback. "
            "Do not finish in chat yet. Call a shell/file tool now to inspect "
            "the latest artifact, run the visible checker if present, or write "
            "the corrected artifact under /app."
        ),
        "_meta": {
            "type": "bench_no_tool_final_nudge",
            "is_meta": True,
        },
    }


def _build_bench_no_tool_final_model_fallback_message(
    fallback_model: str,
) -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            "Terminal-Bench finalization did not produce a tool-backed answer "
            "after the retry budget. Continue with the configured fallback "
            f"model ({fallback_model}) and call a shell/file tool before "
            "finishing."
        ),
        "_meta": {
            "type": "bench_no_tool_final_model_fallback",
            "is_meta": True,
            "fallback_model": fallback_model,
        },
    }


def _append_max_output_recovery_message(
    messages: list[dict[str, Any]],
    state: Any,
) -> None:
    state.max_output_tokens_recovery_count += 1
    if _should_force_tool_on_max_output_recovery():
        state.force_tool_choice_next_call = True
    messages.append(
        stop_policy.build_max_output_tokens_recovery_message(
            state.max_output_tokens_recovery_count
        )
    )


def _drop_no_tool_length_content(model_response: ModelResponse) -> None:
    if model_response.stop_reason != "length" or model_response.tool_calls:
        return
    assistant_message = model_response.assistant_message
    content = assistant_message.get("content")
    if not content:
        return
    original_chars = len(content) if isinstance(content, str) else None
    meta = dict(assistant_message.get("_meta") or {})
    meta["truncated_content_omitted"] = True
    if original_chars is not None:
        meta["original_content_chars"] = original_chars
    assistant_message["_meta"] = meta
    assistant_message["content"] = ""
    if original_chars is None:
        logger.info("Dropped length-truncated no-tool assistant content")
    else:
        logger.info(
            "Dropped %s chars of length-truncated no-tool assistant content",
            original_chars,
        )


def _tool_name(tool: Any) -> str:
    schema = getattr(tool, "schema", None)
    name = getattr(schema, "name", None)
    if isinstance(name, str) and name:
        return name
    name = getattr(tool, "name", None)
    if isinstance(name, str) and name:
        return name
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name:
                return name
        name = tool.get("name")
        if isinstance(name, str) and name:
            return name
    return type(tool).__name__


def _usage_summary(usage: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cost",
    ):
        value = getattr(usage, key, None)
        if value not in (None, 0, 0.0):
            summary[key] = value
    return summary


def _response_metadata(
    model_response: ModelResponse,
    *,
    has_tool_calls: bool,
    outcome: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "has_tool_calls": has_tool_calls,
        "tool_calls_count": len(model_response.tool_calls),
        "stop_reason": model_response.stop_reason,
    }
    if outcome:
        metadata["outcome"] = outcome
    usage = _usage_summary(model_response.usage)
    if usage:
        metadata["usage"] = usage
    if model_response.effective_model:
        metadata["effective_model"] = model_response.effective_model
    return metadata


async def _record_iteration_without_tool_execution(
    turn: TurnControllerContext,
    *,
    messages_input_snapshot: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    model_response: ModelResponse,
    response_metadata: dict[str, Any],
    outcome: str,
) -> None:
    """Persist model-only iterations that never reach tool execution.

    Tool turns are recorded by ``tool_turn_controller`` after tool results are
    appended. Length-truncated, empty, stop-hook, and final no-tool responses
    used to leave gaps in ``conversations.jsonl``; recording them here keeps the
    trace complete without duplicating normal tool iterations.
    """

    metadata = dict(response_metadata)
    metadata["outcome"] = outcome
    metadata["tool_execution"] = False
    delta_messages = copy.deepcopy(messages[len(messages_input_snapshot):])
    turn.state.iteration_contexts.append(
        {
            "iteration": turn.state.current_iteration,
            "messages_input": messages_input_snapshot,
            "messages_output": copy.deepcopy(messages),
            "response_metadata": metadata,
        }
    )
    try:
        from openspace.recording import RecordingManager

        await RecordingManager.record_iteration_context(
            iteration=turn.state.current_iteration,
            delta_messages=delta_messages,
            response_metadata=metadata,
            extra={
                "source": "model_response_controller",
                "outcome": outcome,
            },
        )
    except Exception as exc:
        logger.debug("Failed to record model-only iteration: %s", exc)


@dataclass(slots=True)
class ModelCallResult:
    action: str
    messages: list[dict[str, Any]]
    model_response: ModelResponse | None = None


@dataclass(slots=True)
class ModelResponseResult:
    action: str
    messages: list[dict[str, Any]]
    has_tool_calls: bool = False
    assistant_content: Any = ""


async def call_model_with_recovery(
    turn: TurnControllerContext,
    *,
    messages: list[dict[str, Any]],
    active_tools: list[Any],
) -> ModelCallResult:
    """Call the LLM and handle retryable model-side failures."""

    agent = turn.agent
    context = turn.context
    tool_use_context = turn.tool_use_context
    state = turn.state
    state.refresh_reasoning_effort(tool_use_context)
    model_response: ModelResponse | None = None
    try:
        if _env_bool("OPENSPACE_DEBUG_TOOL_CALLS"):
            tool_line = (
                f"OPENSPACE_DEBUG active_tools={len(active_tools)} "
                f"names={', '.join(_tool_name(tool) for tool in active_tools[:40])}"
            )
            print(tool_line, flush=True)
            logger.info(tool_line)
        marker = getattr(turn.low_latency_profiler, "mark", None)
        if callable(marker):
            if state.current_iteration == 1:
                marker("first_model_request", iteration=state.current_iteration)
            marker("llm.request_start", iteration=state.current_iteration)
        force_tool_choice = (
            bool(state.force_tool_choice_next_call)
            and bool(active_tools)
            and bool(context.get("auto_execute", True))
        )
        state.force_tool_choice_next_call = False
        with turn.span("llm.request", iteration=state.current_iteration):
            model_response = await agent._llm_client.call_model(
                messages=messages,
                tools=active_tools if context.get("auto_execute", True) else None,
                abort_event=turn.abort_event,
                model=state.effective_model,
                fallback_model=state.effective_fallback_model,
                reasoning_effort=state.effective_reasoning_effort,
                tool_prompt_context=tool_use_context,
                tool_choice="required" if force_tool_choice else "auto",
            )
        if callable(marker) and model_response is not None:
            marker(
                "llm.first_chunk",
                iteration=state.current_iteration,
                streaming=True,
            )
        return ModelCallResult(
            action="response",
            messages=messages,
            model_response=model_response,
        )

    except PromptTooLongError as ptl_err:
        logger.warning("Prompt too long, attempting compact: %s", ptl_err)
        await tool_use_context.emit_event(
            "compact_start",
            {"trigger": "prompt_too_long"},
        )
        try:
            compaction = await compact_conversation(
                messages,
                agent._llm_client,
                tool_use_context,
                is_auto_compact=True,
                hook_registry=agent._hook_registry,
                model=state.effective_model,
                emit_lifecycle_events=False,
            )
            post_msgs = build_post_compact_messages(compaction)
            run_post_compact_cleanup(tool_use_context)
            system_msgs = agent._refresh_system_messages_after_compact(
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
            messages = system_msgs + post_msgs
            await persist_compacted_session_messages(
                agent,
                tool_use_context,
                messages,
                model=state.effective_model,
            )
            agent._sync_tool_use_context_runtime(
                tool_use_context,
                messages=messages,
            )
            state.compact_tracking.compacted = True
            state.compact_tracking.consecutive_failures = 0
            await tool_use_context.emit_event("compact_complete", {"success": True})
            logger.info("PTL recovery compact succeeded, retrying call_model")
            return ModelCallResult(action="retry", messages=messages)
        except Exception as compact_err:
            state.compact_tracking.consecutive_failures += 1
            logger.warning(
                "PTL compact failed (%s), stopping without local truncation",
                state.compact_tracking.consecutive_failures,
            )
            await tool_use_context.emit_event(
                "compact_complete",
                {
                    "success": False,
                    "error": str(compact_err),
                },
            )
            error_msg = get_error_message_for_user(
                ptl_err,
                state.effective_model,
            )
            messages.append(
                build_assistant_api_error_message(
                    error_msg,
                    error_details=(
                        f"{ptl_err}; compact_error={compact_err}"
                    ),
                )
            )
            agent._sync_tool_use_context_runtime(
                tool_use_context,
                messages=messages,
            )
            state.stop_reason_final = "prompt_too_long"
            return ModelCallResult(action="break", messages=messages)

    except FallbackTriggeredError as fb_err:
        logger.warning(
            "Fallback triggered: %s -> %s (task-local switch only, shared "
            "LLMClient unchanged)",
            fb_err.original_model,
            fb_err.fallback_model,
        )
        current_model = state.effective_model
        fallback_model = str(fb_err.fallback_model or "").strip()
        if not fallback_model or fallback_model == current_model:
            messages.append(
                build_assistant_api_error_message(
                    get_error_message_for_user(
                        fb_err,
                        current_model or "unknown",
                    ),
                    error_details=str(fb_err),
                )
            )
            agent._sync_tool_use_context_runtime(
                tool_use_context,
                messages=messages,
            )
            state.stop_reason_final = "model_error"
            return ModelCallResult(action="break", messages=messages)

        state.switch_to_fallback(fallback_model)
        agent._sync_tool_use_context_runtime(
            tool_use_context,
            model=state.effective_model,
        )
        return ModelCallResult(action="retry", messages=messages)

    except CannotRetryError as cr_err:
        recovery = recover_conversation(messages, cr_err)
        messages = recovery.messages
        agent._sync_tool_use_context_runtime(
            tool_use_context,
            messages=messages,
        )
        if recovery.should_retry and state.conversation_recovery_retry_count < 1:
            state.conversation_recovery_retry_count += 1
            await tool_use_context.emit_event(
                "conversation_recovery",
                {
                    "reason": recovery.reason,
                    "retry": True,
                    "attempt": state.conversation_recovery_retry_count,
                    "dropped_messages": recovery.dropped_messages,
                    "inserted_synthetic_results": (
                        recovery.inserted_synthetic_results
                    ),
                    "error": classify_api_error(cr_err.original_error or cr_err),
                },
            )
            logger.info(
                "Conversation recovery retrying last turn after %s",
                classify_api_error(cr_err.original_error or cr_err),
            )
            return ModelCallResult(action="retry", messages=messages)

        error_msg = get_error_message_for_user(
            cr_err.original_error or cr_err,
            state.effective_model,
        )
        messages.append(
            build_assistant_api_error_message(
                error_msg,
                error_details=str(cr_err),
            )
        )
        agent._sync_tool_use_context_runtime(tool_use_context, messages=messages)
        await session_policy.save_after_model_error(
            agent,
            tool_use_context,
            messages,
            model=state.effective_model,
        )
        state.stop_reason_final = "model_error"
        return ModelCallResult(action="break", messages=messages)

    except Exception as api_err:
        if is_abort_error(api_err):
            messages.append(build_user_interruption_message(tool_use=False))
            agent._sync_tool_use_context_runtime(
                tool_use_context,
                messages=messages,
            )
            state.stop_reason_final = "aborted"
            return ModelCallResult(action="break", messages=messages)
        recovery = recover_conversation(messages, api_err)
        messages = recovery.messages
        agent._sync_tool_use_context_runtime(
            tool_use_context,
            messages=messages,
        )
        if recovery.should_retry and state.conversation_recovery_retry_count < 1:
            state.conversation_recovery_retry_count += 1
            await tool_use_context.emit_event(
                "conversation_recovery",
                {
                    "reason": recovery.reason,
                    "retry": True,
                    "attempt": state.conversation_recovery_retry_count,
                    "dropped_messages": recovery.dropped_messages,
                    "inserted_synthetic_results": (
                        recovery.inserted_synthetic_results
                    ),
                    "error": classify_api_error(api_err),
                },
            )
            logger.info(
                "Conversation recovery retrying last turn after %s",
                classify_api_error(api_err),
            )
            return ModelCallResult(action="retry", messages=messages)
        error_msg = get_error_message_for_user(api_err, state.effective_model)
        messages.append(
            build_assistant_api_error_message(
                error_msg,
                error_details=str(api_err),
            )
        )
        agent._sync_tool_use_context_runtime(tool_use_context, messages=messages)
        await session_policy.save_after_model_error(
            agent,
            tool_use_context,
            messages,
            model=state.effective_model,
        )
        state.stop_reason_final = "model_error"
        logger.error(
            "call_model failed: %s - %s",
            classify_api_error(api_err),
            api_err,
        )
        return ModelCallResult(action="break", messages=messages)


async def handle_model_response(
    turn: TurnControllerContext,
    *,
    model_response: ModelResponse | None,
    messages: list[dict[str, Any]],
) -> ModelResponseResult:
    """Append and classify a model response before any tool execution."""

    agent = turn.agent
    context = turn.context
    tool_use_context = turn.tool_use_context
    state = turn.state
    if model_response is None:
        state.consecutive_empty += 1
        if state.current_iteration >= state.max_iterations:
            state.stop_reason_final = "max_turns"
            return ModelResponseResult(action="break", messages=messages)
        if state.consecutive_empty >= state.max_consecutive_empty:
            state.stop_reason_final = "empty_response"
            return ModelResponseResult(action="break", messages=messages)
        return ModelResponseResult(action="continue", messages=messages)

    _drop_no_tool_length_content(model_response)
    messages_input_snapshot = copy.deepcopy(messages)
    messages.append(model_response.assistant_message)
    state.budget_tracker.record_usage(model_response.usage)
    agent._sync_tool_use_context_runtime(tool_use_context, messages=messages)
    await session_policy.save_after_assistant_response(
        agent,
        tool_use_context,
        messages,
        usage=model_response.usage,
        model=state.effective_model,
    )

    assistant_content = model_response.assistant_message.get("content", "")
    has_tool_calls = bool(model_response.tool_calls)
    if has_tool_calls and state.bench_finalize_nudge_count > 0:
        state.bench_finalize_last_tool_iteration = state.current_iteration
        state.bench_finalize_last_tool_monotonic = time.monotonic()
    has_assistant_text = (
        assistant_content
        and isinstance(assistant_content, str)
        and assistant_content.strip()
    )
    base_response_metadata = _response_metadata(
        model_response,
        has_tool_calls=has_tool_calls,
    )

    async def finish_without_tool_execution(
        action: str,
        outcome: str,
    ) -> ModelResponseResult:
        await _record_iteration_without_tool_execution(
            turn,
            messages_input_snapshot=messages_input_snapshot,
            messages=messages,
            model_response=model_response,
            response_metadata=base_response_metadata,
            outcome=outcome,
        )
        return ModelResponseResult(
            action=action,
            messages=messages,
            has_tool_calls=has_tool_calls,
            assistant_content=assistant_content,
        )

    abort_stop_reason = stop_policy.abort_stop_reason(
        turn.abort_event,
        after_model_response=True,
    )
    if abort_stop_reason:
        messages.append(build_user_interruption_message(tool_use=False))
        agent._sync_tool_use_context_runtime(tool_use_context, messages=messages)
        state.stop_reason_final = abort_stop_reason
        return await finish_without_tool_execution("break", abort_stop_reason)

    response_followups = agent._get_model_response_followup_messages(
        model_response
    )
    has_model_api_error = agent._is_api_error_message(
        model_response.assistant_message
    ) or any(agent._is_api_error_message(message) for message in response_followups)

    if (
        not has_assistant_text
        and not has_tool_calls
        and model_response.stop_reason == "length"
    ):
        if state.current_iteration >= state.max_iterations:
            state.consecutive_empty += 1
            state.stop_reason_final = "max_turns"
            return await finish_without_tool_execution("break", "max_turns")
        if stop_policy.should_recover_max_output_tokens(
            stop_reason=model_response.stop_reason,
            has_tool_calls=has_tool_calls,
            recovery_count=state.max_output_tokens_recovery_count,
        ):
            _append_max_output_recovery_message(messages, state)
            agent._sync_tool_use_context_runtime(
                tool_use_context,
                messages=messages,
            )
            logger.info(
                "max_output_tokens recovery %s",
                state.max_output_tokens_recovery_count,
            )
            return await finish_without_tool_execution(
                "continue",
                "max_output_tokens_recovery",
            )
        logger.warning("max_output_tokens recovery limit reached")
        state.stop_reason_final = "max_output_tokens"
        return await finish_without_tool_execution("break", "max_output_tokens")

    if has_assistant_text:
        state.consecutive_empty = 0
        await agent._emit_runtime_event(
            "agent_output",
            turn_events.agent_output_payload(
                agent,
                context,
                agent_id=turn.agent_id,
                content=assistant_content,
                iteration=state.current_iteration,
                tool_calls_count=len(model_response.tool_calls),
            ),
        )
    elif not has_tool_calls:
        state.consecutive_empty += 1
        logger.warning(
            "Empty response %s/%s",
            state.consecutive_empty,
            state.max_consecutive_empty,
        )
        if state.current_iteration >= state.max_iterations:
            state.stop_reason_final = "max_turns"
            return await finish_without_tool_execution("break", "max_turns")
        if state.consecutive_empty >= state.max_consecutive_empty:
            logger.error("Too many consecutive empty responses")
            state.stop_reason_final = "empty_response"
            return await finish_without_tool_execution("break", "empty_response")
        return await finish_without_tool_execution("continue", "empty_response")
    elif stop_policy.is_tool_call_only_response(
        assistant_content=assistant_content,
        has_tool_calls=has_tool_calls,
    ):
        state.consecutive_empty = 0
    else:
        state.consecutive_empty = 0

    if has_model_api_error:
        if stop_policy.should_recover_max_output_tokens(
            stop_reason=model_response.stop_reason,
            has_tool_calls=has_tool_calls,
            recovery_count=state.max_output_tokens_recovery_count,
        ):
            _append_max_output_recovery_message(messages, state)
            agent._sync_tool_use_context_runtime(
                tool_use_context,
                messages=messages,
            )
            logger.info(
                "max_output_tokens recovery %s",
                state.max_output_tokens_recovery_count,
            )
            return await finish_without_tool_execution(
                "continue",
                "max_output_tokens_recovery",
            )

        if response_followups:
            messages.extend(response_followups)
            agent._sync_tool_use_context_runtime(
                tool_use_context,
                messages=messages,
            )
        state.stop_reason_final = agent._model_error_stop_reason(
            model_response.stop_reason
        )
        return await finish_without_tool_execution(
            "break",
            state.stop_reason_final,
        )

    if not has_tool_calls:
        if model_response.stop_reason == "error":
            state.stop_reason_final = "model_error"
            return await finish_without_tool_execution("break", "model_error")

        if model_response.stop_reason == "length":
            if stop_policy.should_recover_max_output_tokens(
                stop_reason=model_response.stop_reason,
                has_tool_calls=has_tool_calls,
                recovery_count=state.max_output_tokens_recovery_count,
            ):
                _append_max_output_recovery_message(messages, state)
                agent._sync_tool_use_context_runtime(
                    tool_use_context,
                    messages=messages,
                )
                logger.info(
                    "max_output_tokens recovery %s",
                    state.max_output_tokens_recovery_count,
                )
                return await finish_without_tool_execution(
                    "continue",
                    "max_output_tokens_recovery",
                )
            logger.warning("max_output_tokens recovery limit reached")
            state.stop_reason_final = "max_output_tokens"
            return await finish_without_tool_execution("break", "max_output_tokens")

        if (
            _env_bool("OPENSPACE_REQUIRE_TOOL_USE")
            and not state.all_tool_results
            and state.current_iteration < state.max_iterations
        ):
            nudge_count = int(context.get("require_tool_use_nudges", 0) or 0)
            if nudge_count < _require_tool_use_nudge_limit():
                context["require_tool_use_nudges"] = nudge_count + 1
                state.force_tool_choice_next_call = True
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You haleted inside the container. Use the available "
                            "shelve not used any tools yet. This task must be "
                            "compl/file tools now to inspect the workspace and "
                            "make the required changes; do not provide a final "
                            "answer until after tool execution."
                        ),
                        "_meta": {
                            "type": "require_tool_use_nudge",
                            "is_meta": True,
                        },
                    }
                )
                agent._sync_tool_use_context_runtime(
                    tool_use_context,
                    messages=messages,
                )
                logger.info(
                    "Require-tool-use nudge %s/%s after no-tool response",
                    nudge_count + 1,
                    _require_tool_use_nudge_limit(),
                )
                return await finish_without_tool_execution(
                    "continue",
                    "require_tool_use_nudge",
                )

        if bench_checker_guard.should_block_final(state):
            nudge_count = int(context.get("bench_checker_failure_nudges", 0) or 0)
            if (
                state.current_iteration < state.max_iterations
                and nudge_count < bench_checker_guard.nudge_limit()
            ):
                context["bench_checker_failure_nudges"] = nudge_count + 1
                state.force_tool_choice_next_call = True
                messages.append(bench_checker_guard.build_nudge_message(state))
                agent._sync_tool_use_context_runtime(
                    tool_use_context,
                    messages=messages,
                )
                logger.info(
                    "Bench checker failure nudge %s/%s",
                    nudge_count + 1,
                    bench_checker_guard.nudge_limit(),
                )
                return await finish_without_tool_execution(
                    "continue",
                    "bench_checker_failure_nudge",
                )
            state.stop_reason_final = "bench_visible_checker_failed"
            logger.warning(
                "Stopping after unresolved visible checker failure: %s",
                bench_checker_guard.summarize_failure(state),
            )
            return await finish_without_tool_execution(
                "break",
                "bench_visible_checker_failed",
            )

        if _should_block_bench_pending_action_final(
            state,
            assistant_content,
            has_tool_calls=has_tool_calls,
        ):
            nudge_count = int(
                context.get("bench_pending_action_final_nudges", 0) or 0
            )
            if nudge_count < _bench_pending_action_final_nudge_limit():
                context["bench_pending_action_final_nudges"] = nudge_count + 1
                state.force_tool_choice_next_call = True
                messages.append(
                    _build_bench_pending_action_final_nudge_message(
                        assistant_content
                    )
                )
                agent._sync_tool_use_context_runtime(
                    tool_use_context,
                    messages=messages,
                )
                logger.info(
                    "Bench pending-action final nudge %s/%s",
                    nudge_count + 1,
                    _bench_pending_action_final_nudge_limit(),
                )
                return await finish_without_tool_execution(
                    "continue",
                    "bench_pending_action_final_nudge",
                )
            logger.warning(
                "Terminal-Bench pending-action final guard exhausted"
            )

        if _should_block_bench_no_tool_final(state):
            nudge_count = int(context.get("bench_no_tool_final_nudges", 0) or 0)
            if nudge_count < _bench_no_tool_final_nudge_limit():
                context["bench_no_tool_final_nudges"] = nudge_count + 1
                state.force_tool_choice_next_call = True
                messages.append(_build_bench_no_tool_final_nudge_message())
                agent._sync_tool_use_context_runtime(
                    tool_use_context,
                    messages=messages,
                )
                logger.info(
                    "Bench no-tool final nudge %s/%s",
                    nudge_count + 1,
                    _bench_no_tool_final_nudge_limit(),
                )
                return await finish_without_tool_execution(
                    "continue",
                    "bench_no_tool_final_nudge",
                )

            fallback_model = _bench_no_tool_final_fallback_model(state)
            if fallback_model:
                state.switch_to_fallback(fallback_model)
                state.force_tool_choice_next_call = True
                context["bench_no_tool_final_model_fallback"] = fallback_model
                messages.append(
                    _build_bench_no_tool_final_model_fallback_message(
                        fallback_model
                    )
                )
                agent._sync_tool_use_context_runtime(
                    tool_use_context,
                    messages=messages,
                    model=state.effective_model,
                )
                logger.info(
                    "Bench no-tool final switched to fallback model %s",
                    fallback_model,
                )
                return await finish_without_tool_execution(
                    "continue",
                    "bench_no_tool_final_model_fallback",
                )

            state.stop_reason_final = "bench_no_tool_final_unresolved"
            logger.warning(
                "Stopping after Terminal-Bench finalization produced no "
                "tool-backed fallback"
            )
            return await finish_without_tool_execution(
                "break",
                "bench_no_tool_final_unresolved",
            )

        from openspace.services.tooling.stop import handle_stop_hooks

        stop_hook_result = await handle_stop_hooks(
            messages=messages,
            last_response=model_response,
            context=tool_use_context,
        )
        if stop_hook_result.blocking_errors:
            messages.extend(stop_hook_result.blocking_errors)
            agent._sync_tool_use_context_runtime(
                tool_use_context,
                messages=messages,
            )
            logger.info(
                "Stop hook blocking - continuing with injected messages"
            )
            return await finish_without_tool_execution(
                "continue",
                "stop_hook_blocking",
            )
        if stop_hook_result.prevent_continuation:
            state.stop_reason_final = "stop_hook_prevented"
            return await finish_without_tool_execution(
                "break",
                "stop_hook_prevented",
            )

        budget_decision = state.budget_tracker.check(
            agent_id=None if turn.agent_id == "primary" else turn.agent_id,
            budget=state.current_turn_token_budget,
        )
        if budget_decision.action == "continue":
            await tool_use_context.emit_event(
                "token_budget_continue",
                {
                    "continuation_count": budget_decision.continuation_count,
                    "pct": budget_decision.pct,
                    "turn_tokens": budget_decision.turn_tokens,
                    "budget": budget_decision.budget,
                },
            )
            messages.append(
                {
                    "role": "user",
                    "content": budget_decision.nudge_message or "",
                    "_meta": {
                        "type": "token_budget_continuation",
                        "is_meta": True,
                    },
                }
            )
            agent._sync_tool_use_context_runtime(
                tool_use_context,
                messages=messages,
            )
            state.reset_max_output_recovery()
            logger.info(
                "Token budget continuation #%s: %s%% (%s / %s)",
                budget_decision.continuation_count,
                budget_decision.pct,
                budget_decision.turn_tokens,
                budget_decision.budget,
            )
            return await finish_without_tool_execution(
                "continue",
                "token_budget_continue",
            )

        if budget_decision.completion_event is not None:
            event_payload = budget_decision.completion_event.to_dict()
            await tool_use_context.emit_event(
                "token_budget_completed",
                event_payload,
            )
            if budget_decision.completion_event.diminishing_returns:
                logger.info(
                    "Token budget early stop: diminishing returns at %s%%",
                    budget_decision.completion_event.pct,
                )

        state.stop_reason_final = "completed"
        return await finish_without_tool_execution("break", "completed")

    return ModelResponseResult(
        action="tools",
        messages=messages,
        has_tool_calls=True,
        assistant_content=assistant_content,
    )
