"""Query-level stop hook orchestration.

Implementation: ``query/stopHooks.ts``.

This module owns end-of-turn orchestration.  ``services.tool_hooks`` owns the
lower-level HookRegistry and typed hook callbacks.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from openspace.services.conversation.content_blocks import extract_text_from_content
from openspace.services.conversation.messages import (
    build_stop_hook_summary_message,
    get_assistant_message_text,
    get_last_assistant_message,
)
from openspace.services.tooling.hooks import (
    HookBlockingError,
    HookEvent,
    get_stop_hook_message,
    get_task_completed_hook_message,
    get_teammate_idle_hook_message,
)

if TYPE_CHECKING:
    from openspace.services.tooling.context import ToolUseContext

logger = logging.getLogger(__name__)

_MAIN_AGENT_IDS = {"", "primary", "main"}


@dataclass(slots=True)
class StopHookResult:
    """Result from the query-level stop hook orchestrator."""

    blocking_errors: list[dict[str, Any]] = field(default_factory=list)
    prevent_continuation: bool = False
    yielded_messages: list[dict[str, Any]] = field(default_factory=list)


async def handle_stop_hooks(
    messages: list[dict[str, Any]],
    last_response: Any,
    context: ToolUseContext,
    *,
    stop_hook_active: bool = False,
) -> StopHookResult:
    """Run OpenSpace end-of-turn stop chain.

    Return shape mirrors OpenSpace ``StopHookResult``:
    ``blocking_errors`` are hidden user messages for the next model turn;
    ``prevent_continuation`` means the query loop should stop immediately.
    """

    started_at = time.monotonic()
    result = StopHookResult()

    try:
        await _schedule_background_work(context)

        hook_registry = context.hook_registry
        hook_event = _stop_hook_event_for_context(context)

        if hook_registry is None:
            await _emit_cost_snapshot(context, reason="no_stop_hooks")
            return result
        if not hook_registry.has_hook_for_event(hook_event):
            teammate_result = await _run_teammate_hooks(context)
            result.yielded_messages.extend(teammate_result.yielded_messages)
            if teammate_result.prevent_continuation or teammate_result.blocking_errors:
                return teammate_result
            await _emit_cost_snapshot(context, reason="no_stop_hooks")
            return result

        abort_event = context.abort_event
        hook_count = 0
        hook_errors: list[str] = []
        hook_infos: list[dict[str, Any]] = []
        prevented_continuation = False
        stop_reason = ""
        has_output = False
        hook_tool_use_id: str | None = None
        last_assistant_text = _last_assistant_text(messages)

        async for agg in hook_registry.execute_hooks(
            hook_event,
            hook_kwargs={
                "messages": messages,
                "last_response": last_response,
                "context": context,
                "stop_hook_active": stop_hook_active,
                "last_assistant_message": last_assistant_text,
                "agent_id": _agent_id_for_hooks(context),
                "agent_type": getattr(context, "agent_type", None) or "",
            },
            context=context,
            abort_event=abort_event,
        ):
            hook_count += 1

            if agg.message:
                result.yielded_messages.append(agg.message)
                await _emit_hook_message(context, agg.message)
                info = _hook_info_from_message(agg.message)
                if info:
                    hook_infos.append(info)
                maybe_tool_use_id = _tool_use_id_from_message(agg.message)
                if maybe_tool_use_id:
                    hook_tool_use_id = maybe_tool_use_id
                if _message_has_hook_output(agg.message):
                    has_output = True
                error_text = _hook_error_from_message(agg.message)
                if error_text:
                    hook_errors.append(error_text)
                    has_output = True

            if agg.blocking_error:
                user_msg = _blocking_error_message(hook_event, agg.blocking_error)
                result.blocking_errors.append(user_msg)
                result.yielded_messages.append(user_msg)
                hook_errors.append(agg.blocking_error.blocking_error)
                has_output = True
                await _emit_hook_message(context, user_msg)

            if agg.prevent_continuation:
                prevented_continuation = True
                stop_reason = agg.stop_reason or f"{hook_event.value} hook prevented continuation"
                stopped = _hook_stopped_continuation_message(
                    hook_event=hook_event,
                    stop_reason=stop_reason,
                    tool_use_id=hook_tool_use_id,
                )
                result.yielded_messages.append(stopped)
                await _emit_hook_message(context, stopped)

            if abort_event is not None and abort_event.is_set():
                interrupted = {
                    "role": "user",
                    "content": "User interrupted stop hook execution.",
                    "_meta": {"type": "user_interruption", "tool_use": False},
                }
                result.yielded_messages.append(interrupted)
                await _emit_hook_message(context, interrupted)
                result.prevent_continuation = True
                result.blocking_errors.clear()
                await _emit_cost_snapshot(context, reason="aborted")
                return result

        if hook_count > 0:
            summary = build_stop_hook_summary_message(
                hook_count=hook_count,
                hook_infos=hook_infos,
                hook_errors=hook_errors,
                prevented_continuation=prevented_continuation,
                stop_reason=stop_reason or None,
                has_output=has_output,
                level="info",
                tool_use_id=hook_tool_use_id,
                hook_label=hook_event.value,
                total_duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            result.yielded_messages.append(summary)
            await context.emit_event(
                "stop_hook_summary",
                {
                    "summary": summary,
                    "hook_count": hook_count,
                    "hook_event": hook_event.value,
                    "errors": hook_errors,
                    "prevented_continuation": prevented_continuation,
                },
            )

        if prevented_continuation:
            result.prevent_continuation = True
            result.blocking_errors.clear()
            await _emit_cost_snapshot(context, reason="prevent_continuation")
            return result

        if result.blocking_errors:
            await _emit_cost_snapshot(context, reason="blocking_error")
            return result

        teammate_result = await _run_teammate_hooks(context)
        result.yielded_messages.extend(teammate_result.yielded_messages)
        if teammate_result.prevent_continuation or teammate_result.blocking_errors:
            return teammate_result

        await _emit_cost_snapshot(context, reason="completed")
        return result
    except Exception as exc:
        logger.debug("Stop hook orchestration failed", exc_info=True)
        warning = {
            "role": "system",
            "content": f"Stop hook failed: {exc}",
            "_meta": {"type": "stop_hook_error", "level": "warning"},
        }
        result.yielded_messages.append(warning)
        await _emit_hook_message(context, warning)
        await _emit_cost_snapshot(context, reason="error")
        return result


async def _schedule_background_work(context: ToolUseContext) -> None:
    """Schedule OpenSpace stop-hook background work before registry guard."""

    if _is_bare_mode():
        return

    # PromptSuggestion/speculation is intentionally not implemented yet.
    # OpenSpace runs it first; OS has no prompt-suggestion AppState/UI flow today.

    await _schedule_session_memory(context)
    await _schedule_extract_memories(context)
    await _schedule_auto_dream(context)


async def _schedule_session_memory(context: ToolUseContext) -> None:
    try:
        from openspace.services.runtime_support.background import schedule_session_memory

        await schedule_session_memory(
            context,
            getattr(context, "append_system_message", None),
        )
    except RuntimeError:
        pass
    except Exception:
        logger.debug("Failed to schedule session memory extraction", exc_info=True)


async def _schedule_extract_memories(context: ToolUseContext) -> None:
    try:
        from openspace.services.runtime_support.background import schedule_extract_memories

        await schedule_extract_memories(
            context,
            getattr(context, "append_system_message", None),
        )
    except RuntimeError:
        pass
    except Exception:
        logger.debug("Failed to schedule memory extraction", exc_info=True)


async def _schedule_auto_dream(context: ToolUseContext) -> None:
    try:
        from openspace.services.runtime_support.background import schedule_auto_dream

        await schedule_auto_dream(
            context,
            getattr(context, "append_system_message", None),
        )
    except RuntimeError:
        pass
    except Exception:
        logger.debug("Failed to schedule auto dream", exc_info=True)


def _stop_hook_event_for_context(context: ToolUseContext) -> HookEvent:
    return HookEvent.SUBAGENT_STOP if _is_subagent_context(context) else HookEvent.STOP


def _is_subagent_context(context: ToolUseContext) -> bool:
    agent_id = _agent_id_for_hooks(context)
    if agent_id and agent_id not in _MAIN_AGENT_IDS:
        return True
    return bool(getattr(context, "parent_task_id", None) or getattr(context, "is_async_agent", False))


def _agent_id_for_hooks(context: ToolUseContext) -> str:
    value = getattr(context, "agent_id", "") or ""
    return str(value)


def _last_assistant_text(messages: list[dict[str, Any]]) -> str | None:
    last = get_last_assistant_message(messages)
    if last is None:
        return None
    text = get_assistant_message_text(last)
    if text:
        return text
    extracted = extract_text_from_content(last.get("content"))
    return extracted.strip() or None


def _blocking_error_message(
    hook_event: HookEvent,
    blocking_error: HookBlockingError,
) -> dict[str, Any]:
    if hook_event == HookEvent.TASK_COMPLETED:
        content = get_task_completed_hook_message(blocking_error)
    elif hook_event == HookEvent.TEAMMATE_IDLE:
        content = get_teammate_idle_hook_message(blocking_error)
    else:
        content = get_stop_hook_message(blocking_error)
    return {
        "role": "user",
        "content": content,
        "_meta": {
            "is_meta": True,
            "type": "stop_hook_blocking",
            "hook_event": hook_event.value,
        },
    }


def _hook_stopped_continuation_message(
    *,
    hook_event: HookEvent,
    stop_reason: str,
    tool_use_id: str | None,
) -> dict[str, Any]:
    return {
        "role": "system",
        "content": stop_reason,
        "_meta": {
            "type": "hook_stopped_continuation",
            "hook_name": hook_event.value,
            "hook_event": hook_event.value,
            "tool_use_id": tool_use_id,
            "message": stop_reason,
        },
    }


async def _emit_hook_message(
    context: ToolUseContext,
    message: dict[str, Any],
) -> None:
    await context.emit_event("stop_hook_message", {"message": message})


def _hook_info_from_message(message: Mapping[str, Any]) -> dict[str, Any] | None:
    meta = message.get("_meta")
    if not isinstance(meta, Mapping):
        return None
    command = meta.get("command") or meta.get("hook_name")
    if command is None:
        return None
    info: dict[str, Any] = {"command": str(command)}
    if meta.get("prompt_text") is not None:
        info["promptText"] = str(meta.get("prompt_text"))
    if meta.get("duration_ms") is not None:
        try:
            info["durationMs"] = int(meta.get("duration_ms"))  # type: ignore[arg-type]
        except Exception:
            pass
    return info


def _tool_use_id_from_message(message: Mapping[str, Any]) -> str | None:
    meta = message.get("_meta")
    if isinstance(meta, Mapping):
        value = meta.get("tool_use_id") or meta.get("toolUseID")
        if value:
            return str(value)
    value = message.get("tool_use_id") or message.get("toolUseID")
    return str(value) if value else None


def _hook_error_from_message(message: Mapping[str, Any]) -> str | None:
    meta = message.get("_meta")
    if not isinstance(meta, Mapping):
        return None
    msg_type = str(meta.get("type") or "")
    if msg_type not in {
        "hook_non_blocking_error",
        "hook_error_during_execution",
        "hook_blocking_error",
    }:
        return None
    return str(meta.get("error") or meta.get("blocking_error") or message.get("content") or "")


def _message_has_hook_output(message: Mapping[str, Any]) -> bool:
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return True
    meta = message.get("_meta")
    if isinstance(meta, Mapping):
        return any(bool(str(meta.get(k) or "").strip()) for k in ("stdout", "stderr", "error"))
    return False


async def _run_teammate_hooks(context: ToolUseContext) -> StopHookResult:
    """Run OpenSpace teammate TaskCompleted/TeammateIdle branches when available.

    The branch is intentionally no-op unless an in-process TaskManager team is
    active. It consumes real OS teammate task state instead of inventing a fake
    OpenSpace team store.
    """

    hook_registry = getattr(context, "hook_registry", None)
    manager = getattr(context, "task_manager", None)
    team_name = getattr(manager, "active_team_name", None)
    if hook_registry is None or manager is None or not team_name:
        return StopHookResult()

    try:
        team_tasks = list(manager.list_by_team(team_name))
    except Exception:
        logger.debug("Failed to list teammate tasks", exc_info=True)
        return StopHookResult()

    result = StopHookResult()

    if hook_registry.has_hook_for_event(HookEvent.TASK_COMPLETED):
        for task in team_tasks:
            if not _is_terminal_task(task) or bool(getattr(task, "notified", False)):
                continue
            task_result = await _run_teammate_hook_event(
                context,
                HookEvent.TASK_COMPLETED,
                task=task,
                team_name=str(team_name),
            )
            result.yielded_messages.extend(task_result.yielded_messages)
            if task_result.prevent_continuation or task_result.blocking_errors:
                return task_result
            try:
                task.notified = True
            except Exception:
                pass

    has_running = any(_is_running_task(task) for task in team_tasks)
    idle_notified = _team_idle_notified_set(manager)
    if has_running:
        idle_notified.discard(str(team_name))
    elif (
        team_tasks
        and str(team_name) not in idle_notified
        and hook_registry.has_hook_for_event(HookEvent.TEAMMATE_IDLE)
    ):
        idle_result = await _run_teammate_hook_event(
            context,
            HookEvent.TEAMMATE_IDLE,
            task=None,
            team_name=str(team_name),
        )
        result.yielded_messages.extend(idle_result.yielded_messages)
        if idle_result.prevent_continuation or idle_result.blocking_errors:
            return idle_result
        idle_notified.add(str(team_name))

    return result


async def _run_teammate_hook_event(
    context: ToolUseContext,
    hook_event: HookEvent,
    *,
    task: Any | None,
    team_name: str,
) -> StopHookResult:
    hook_registry = context.hook_registry
    result = StopHookResult()
    hook_tool_use_id: str | None = None
    abort_event = getattr(context, "abort_event", None)

    async for agg in hook_registry.execute_hooks(
        hook_event,
        hook_kwargs={
            "context": context,
            "agent_id": _agent_id_for_hooks(context),
            "team_name": team_name,
            "task": task,
            "task_id": getattr(task, "id", None),
            "task_status": str(getattr(task, "status", "") or ""),
            "task_result": getattr(task, "result", None),
            "task_error": getattr(task, "error", None),
        },
        context=context,
        abort_event=abort_event,
    ):
        if agg.message:
            result.yielded_messages.append(agg.message)
            await _emit_hook_message(context, agg.message)
            maybe_tool_use_id = _tool_use_id_from_message(agg.message)
            if maybe_tool_use_id:
                hook_tool_use_id = maybe_tool_use_id

        if agg.blocking_error:
            user_msg = _blocking_error_message(hook_event, agg.blocking_error)
            result.blocking_errors.append(user_msg)
            result.yielded_messages.append(user_msg)
            await _emit_hook_message(context, user_msg)

        if agg.prevent_continuation:
            result.prevent_continuation = True
            result.blocking_errors.clear()
            stop_reason = agg.stop_reason or f"{hook_event.value} hook prevented continuation"
            stopped = _hook_stopped_continuation_message(
                hook_event=hook_event,
                stop_reason=stop_reason,
                tool_use_id=hook_tool_use_id,
            )
            result.yielded_messages.append(stopped)
            await _emit_hook_message(context, stopped)

    return result


def _is_terminal_task(task: Any) -> bool:
    try:
        from openspace.agents.task_manager import is_terminal_task_status

        return is_terminal_task_status(getattr(task, "status", ""))
    except Exception:
        return str(getattr(task, "status", "")).lower() in {
            "completed",
            "failed",
            "killed",
        }


def _is_running_task(task: Any) -> bool:
    status = getattr(task, "status", "")
    value = getattr(status, "value", status)
    return str(value).lower() == "running"


def _team_idle_notified_set(manager: Any) -> set[str]:
    value = getattr(manager, "_teammate_idle_hook_notified", None)
    if not isinstance(value, set):
        value = set()
        setattr(manager, "_teammate_idle_hook_notified", value)
    return value


async def _emit_cost_snapshot(context: ToolUseContext, *, reason: str) -> None:
    tracker = getattr(context, "cost_tracker", None)
    if tracker is None:
        return

    payload: dict[str, Any] = {"reason": reason}
    try:
        payload["total_cost_usd"] = float(tracker.get_total())
        payload["cost_usd"] = float(tracker.get_total())
    except Exception:
        pass
    try:
        payload["unknown_model_cost"] = bool(tracker.has_unknown_model_cost())
    except Exception:
        pass
    try:
        payload["snapshot"] = tracker.snapshot()
    except Exception:
        pass
    await context.emit_event("cost_summary", payload)


def _is_bare_mode() -> bool:
    return _env_truthy("OPENSPACE_BARE") or _env_truthy("OPENSPACE_SIMPLE")


def _env_truthy(name: str) -> bool:
    value = os.environ.get(name)
    if value is None:
        return False
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


__all__ = ["StopHookResult", "handle_stop_hooks"]
