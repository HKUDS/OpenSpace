"""Side-query helpers for lightweight auxiliary LLM work.

This module provides one isolated async side loop that can run either a single
side query or a bounded tool-using auxiliary agent.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import ToolResult, ToolStatus
from openspace.llm.types import ModelResponse, TokenUsage
from openspace.services.conversation.content_blocks import extract_text_from_content
from openspace.services.conversation.messages import build_tool_result_message
from openspace.tool_runtime.pipeline.execution import find_tool_by_name
from openspace.tool_runtime.orchestration import RunToolsResult, run_tools
from openspace.services.tooling.context import ToolUseContext

SideQueryEventSink = Callable[[str, dict[str, Any]], Awaitable[None] | None]
SideQueryToolGate = Callable[
    [BaseTool | None, Mapping[str, Any]],
    Awaitable[Mapping[str, Any]] | Mapping[str, Any],
]
SideQueryMessageCallback = Callable[
    [dict[str, Any], "SideQueryContext"],
    Awaitable[None] | None,
]

_UNSET = object()


@dataclass(slots=True)
class SideQueryAbortController:
    """Independent abort handle for a side query.

    OpenSpace exposes the child ``asyncio.Event`` directly so callers or
    TaskManager integration can stop the side query without mutating the parent
    context's abort signal.
    """

    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    parent_abort_event: asyncio.Event | None = None
    reason: str | None = None
    _mirror_task: asyncio.Task[Any] | None = field(default=None, init=False, repr=False)

    def start(self) -> None:
        if self.parent_abort_event is None:
            return
        if self.parent_abort_event.is_set():
            self.abort("parent_aborted")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._mirror_task = loop.create_task(self._mirror_parent_abort())

    def abort(self, reason: str = "aborted") -> None:
        self.reason = reason
        self.abort_event.set()

    def is_aborted(self) -> bool:
        return self.abort_event.is_set() or bool(
            self.parent_abort_event is not None and self.parent_abort_event.is_set()
        )

    async def close(self) -> None:
        if self._mirror_task is None:
            return
        self._mirror_task.cancel()
        try:
            await self._mirror_task
        except asyncio.CancelledError:
            pass
        finally:
            self._mirror_task = None

    async def _mirror_parent_abort(self) -> None:
        assert self.parent_abort_event is not None
        await self.parent_abort_event.wait()
        self.abort("parent_aborted")


@dataclass(slots=True)
class SideQueryContext:
    """Runtime envelope for a side-query execution."""

    llm_client: Any
    messages: list[dict[str, Any]]
    tools: list[BaseTool] = field(default_factory=list)
    model: str | None = None
    parent_context: ToolUseContext | None = None
    tool_context: ToolUseContext | None = None
    abort_controller: SideQueryAbortController = field(
        default_factory=SideQueryAbortController,
    )
    query_source: str = "side_query"
    fork_label: str = "side_query"
    max_turns: int = 1
    max_tokens: int | None = None
    temperature: float | None = None
    response_format: Any | None = None
    tool_choice: str = "auto"
    denied_result_type: str = "side_query_tool_denied"
    call_model_kwargs: dict[str, Any] = field(default_factory=dict)
    event_sink: SideQueryEventSink | None = None

    def is_aborted(self) -> bool:
        return self.abort_controller.is_aborted()

    async def emit_event(self, event_type: str, data: Mapping[str, Any]) -> None:
        if self.event_sink is None:
            return
        try:
            result = self.event_sink(event_type, dict(data))
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass


@dataclass(slots=True)
class SideQueryResult:
    """Result of an OS side-query run."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    assistant_messages: list[dict[str, Any]] = field(default_factory=list)
    tool_result_messages: list[dict[str, Any]] = field(default_factory=list)
    conversation_messages: list[dict[str, Any]] = field(default_factory=list)
    total_usage: TokenUsage = field(default_factory=TokenUsage)
    turn_count: int = 0
    stop_reason: str | None = None
    effective_model: str | None = None
    duration_ms: float = 0.0
    aborted: bool = False

    @property
    def text(self) -> str:
        return extract_side_query_text(self.messages)

    def as_task_result(self) -> dict[str, Any]:
        return {
            "status": "cancelled" if self.aborted else "completed",
            "content": self.text,
            "turn_count": self.turn_count,
            "input_tokens": self.total_usage.input_tokens,
            "output_tokens": self.total_usage.output_tokens,
            "duration_ms": self.duration_ms,
        }


async def run_side_query(
    prompt: str | None,
    tools: Sequence[BaseTool] | None = None,
    model: str | None = None,
    *,
    parent_context: ToolUseContext | None = None,
    llm_client: Any | None = None,
    messages: Sequence[Mapping[str, Any]] | None = None,
    system: str | None = None,
    max_turns: int = 1,
    max_tokens: int | None = None,
    temperature: float | None = None,
    response_format: Any | None = None,
    tool_choice: str = "auto",
    denied_result_type: str = "side_query_tool_denied",
    can_use_tool: SideQueryToolGate | None = None,
    query_source: str = "side_query",
    fork_label: str = "side_query",
    agent_type: str | None = None,
    abort_controller: SideQueryAbortController | None = None,
    abort_event: asyncio.Event | None = None,
    parent_abort_event: asyncio.Event | None | object = _UNSET,
    event_sink: SideQueryEventSink | None = None,
    on_message: SideQueryMessageCallback | None = None,
    call_model_kwargs: Mapping[str, Any] | None = None,
    read_file_state: Mapping[str, Any] | None = None,
    permission_context: Any | object = _UNSET,
    hook_registry: Any | None = None,
    tui_available: bool = False,
    is_async_agent: bool = True,
) -> SideQueryResult:
    """Run a lightweight model call or bounded tool-using side agent.

    ``prompt`` is appended as a user message after ``messages``.  With
    ``tools=None`` this runs a single model call.  With tools and ``max_turns``
    greater than one it runs a bounded auxiliary loop with isolated context,
    tool gating, and usage accumulation.
    """

    resolved_client = llm_client or getattr(parent_context, "llm_client", None)
    if resolved_client is None or not hasattr(resolved_client, "call_model"):
        raise ValueError("run_side_query requires an llm_client with call_model()")

    initial_messages = _build_initial_messages(messages, system, prompt)
    side_tools = list(tools or [])

    if abort_controller is None:
        if abort_event is None:
            abort_event = asyncio.Event()
        if parent_abort_event is _UNSET:
            parent_abort = getattr(parent_context, "abort_event", None)
        else:
            parent_abort = parent_abort_event
        abort_controller = SideQueryAbortController(
            abort_event=abort_event,
            parent_abort_event=parent_abort if isinstance(parent_abort, asyncio.Event) else None,
        )

    resolved_event_sink = event_sink or getattr(parent_context, "event_sink", None)
    side_context = SideQueryContext(
        llm_client=resolved_client,
        messages=initial_messages,
        tools=side_tools,
        model=model or getattr(parent_context, "model", None) or getattr(resolved_client, "model", None),
        parent_context=parent_context,
        abort_controller=abort_controller,
        query_source=query_source,
        fork_label=fork_label,
        max_turns=max(1, int(max_turns or 1)),
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
        tool_choice=tool_choice,
        denied_result_type=denied_result_type,
        call_model_kwargs=dict(call_model_kwargs or {}),
        event_sink=resolved_event_sink,
    )
    side_context.tool_context = _build_side_tool_context(
        side_context,
        agent_type=agent_type or fork_label,
        read_file_state=read_file_state,
        permission_context=permission_context,
        hook_registry=hook_registry,
        can_use_tool=can_use_tool,
        tui_available=tui_available,
        is_async_agent=is_async_agent,
    )

    start = time.time()
    output_messages: list[dict[str, Any]] = []
    total_usage = TokenUsage()
    turn_count = 0
    stop_reason: str | None = None
    effective_model = side_context.model

    abort_controller.start()
    await side_context.emit_event(
        "side_query_start",
        {
            "query_source": query_source,
            "fork_label": fork_label,
            "tool_count": len(side_tools),
            "max_turns": side_context.max_turns,
            "model": effective_model,
        },
    )
    try:
        for _turn_index in range(side_context.max_turns):
            if side_context.is_aborted():
                break

            model_response = await _call_side_model(side_context, effective_model)
            effective_model = model_response.effective_model or effective_model
            stop_reason = model_response.stop_reason
            total_usage = _accumulate_usage(total_usage, model_response.usage)
            assistant_message = model_response.assistant_message
            side_context.messages.append(assistant_message)
            output_messages.append(assistant_message)
            turn_count += 1
            await _notify_message(on_message, assistant_message, side_context)

            if not model_response.tool_calls or not side_tools:
                break

            tool_result = await _run_side_query_tool_calls(
                model_response.tool_calls,
                model_response.tool_map,
                side_context.tool_context,
                can_use_tool,
                assistant_message=assistant_message,
                query_source=query_source,
                denied_result_type=side_context.denied_result_type,
            )
            side_context.messages.extend(tool_result.messages)
            output_messages.extend(tool_result.messages)
            for message in tool_result.messages:
                await _notify_message(on_message, message, side_context)
            if tool_result.updated_context is not None:
                side_context.tool_context = tool_result.updated_context
                side_context.tool_context.messages = side_context.messages
            if tool_result.prevent_continuation:
                stop_reason = tool_result.stop_reason or "tool_prevented_continuation"
                break

        aborted = side_context.is_aborted()
        duration_ms = (time.time() - start) * 1000
        result = SideQueryResult(
            messages=output_messages,
            assistant_messages=[
                message for message in output_messages if message.get("role") == "assistant"
            ],
            tool_result_messages=[
                message for message in output_messages if message.get("role") == "tool"
            ],
            conversation_messages=list(side_context.messages),
            total_usage=total_usage,
            turn_count=turn_count,
            stop_reason=stop_reason,
            effective_model=effective_model,
            duration_ms=duration_ms,
            aborted=aborted,
        )
        await side_context.emit_event(
            "side_query_complete",
            {
                "query_source": query_source,
                "fork_label": fork_label,
                "turn_count": turn_count,
                "input_tokens": total_usage.input_tokens,
                "output_tokens": total_usage.output_tokens,
                "duration_ms": duration_ms,
                "aborted": aborted,
            },
        )
        return result
    except asyncio.CancelledError:
        abort_controller.abort("cancelled")
        await side_context.emit_event(
            "side_query_cancelled",
            {"query_source": query_source, "fork_label": fork_label},
        )
        raise
    except Exception as exc:
        await side_context.emit_event(
            "side_query_error",
            {
                "query_source": query_source,
                "fork_label": fork_label,
                "error": str(exc),
                "duration_ms": (time.time() - start) * 1000,
            },
        )
        raise
    finally:
        await abort_controller.close()


def extract_side_query_text(
    messages: Sequence[Mapping[str, Any]],
    default: str = "",
) -> str:
    """Extract assistant text across all side-query assistant messages."""

    parts: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        text = extract_text_from_content(message.get("content"))
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip() or default


def _build_initial_messages(
    messages: Sequence[Mapping[str, Any]] | None,
    system: str | None,
    prompt: str | None,
) -> list[dict[str, Any]]:
    built = [dict(message) for message in (messages or [])]
    if system:
        built.append({"role": "system", "content": system})
    if prompt is not None:
        built.append({"role": "user", "content": prompt})
    return built


def _build_side_tool_context(
    side_context: SideQueryContext,
    *,
    agent_type: str,
    read_file_state: Mapping[str, Any] | None,
    permission_context: Any | object,
    hook_registry: Any | None,
    can_use_tool: SideQueryToolGate | None,
    tui_available: bool,
    is_async_agent: bool,
) -> ToolUseContext:
    parent = side_context.parent_context
    del can_use_tool
    parent_permission_context = getattr(parent, "permission_context", None)
    if permission_context is _UNSET or permission_context is None:
        resolved_permission_context = parent_permission_context
    else:
        resolved_permission_context = permission_context
    cwd = str(getattr(parent, "cwd", "") or ".")
    if resolved_permission_context is None:
        from openspace.grounding.core.permissions.loader import (
            load_tool_permission_context,
        )

        resolved_permission_context = load_tool_permission_context(
            cwd,
            getattr(parent, "permission_mode", None),
        )
    resolved_hook_registry = (
        hook_registry
        if hook_registry is not None
        else getattr(parent, "hook_registry", None)
    )
    if resolved_hook_registry is None:
        from openspace.services.tooling.hooks import HookRegistry, setup_default_hooks

        resolved_hook_registry = HookRegistry()
        setup_default_hooks(resolved_hook_registry)

    resolved_read_file_state = (
        dict(read_file_state)
        if read_file_state is not None
        else dict(getattr(parent, "read_file_state", {}) or {})
    )
    parent_agent_id = str(getattr(parent, "agent_id", "side_query") or "side_query")
    return ToolUseContext(
        tools=list(side_context.tools),
        all_tools=list(side_context.tools),
        model=str(side_context.model or getattr(parent, "model", "unknown") or "unknown"),
        llm_client=side_context.llm_client,
        cwd=cwd,
        original_cwd=str(getattr(parent, "original_cwd", None) or cwd),
        agent_id=f"{parent_agent_id}:{agent_type}",
        agent_type=agent_type,
        max_result_size_chars=int(getattr(parent, "max_result_size_chars", 50_000) or 50_000),
        abort_event=side_context.abort_controller.abort_event,
        messages=side_context.messages,
        read_file_state=resolved_read_file_state,
        tool_results_token_count=0,
        permission_engine=getattr(parent, "permission_engine", None),
        permission_mode=str(getattr(parent, "permission_mode", "default") or "default"),
        permission_context=resolved_permission_context,
        hook_registry=resolved_hook_registry,
        tui_available=tui_available,
        is_async_agent=is_async_agent,
        event_sink=side_context.event_sink,
        recording_manager=None,
        quality_manager=None,
        parent_task_id=getattr(parent, "parent_task_id", None),
        task_description=str(getattr(parent, "task_description", "") or ""),
        current_iteration=0,
        max_iterations=side_context.max_turns,
        task_manager=getattr(parent, "task_manager", None),
        session_id=getattr(parent, "session_id", None),
        session_dir=getattr(parent, "session_dir", None),
        tool_results_dir=getattr(parent, "tool_results_dir", None),
        session_storage=getattr(parent, "session_storage", None),
        file_history=getattr(parent, "file_history", None),
        memory_mode=str(getattr(parent, "memory_mode", "direct") or "direct"),
        append_system_message=getattr(parent, "append_system_message", None),
        backend_scope=tuple(getattr(parent, "backend_scope", ()) or ()),
        background_task_ids=dict(getattr(parent, "background_task_ids", {}) or {}),
        skill_registry=getattr(parent, "skill_registry", None),
        skill_store=getattr(parent, "skill_store", None),
        skills_disabled=bool(getattr(parent, "skills_disabled", False)),
    )


async def _call_side_model(
    context: SideQueryContext,
    effective_model: str | None,
) -> ModelResponse:
    kwargs: dict[str, Any] = {
        "messages": context.messages,
        "tools": list(context.tools) if context.tools else None,
        "abort_event": context.abort_controller.abort_event,
        "tool_choice": context.tool_choice,
        "tool_prompt_context": context.tool_context,
        **context.call_model_kwargs,
    }
    kwargs.setdefault("emit_events", False)
    if effective_model:
        kwargs["model"] = effective_model
    if context.max_tokens is not None:
        kwargs["max_tokens"] = context.max_tokens
    if context.temperature is not None:
        kwargs["temperature"] = context.temperature
    if context.response_format is not None:
        kwargs["response_format"] = context.response_format

    call_model = (
        getattr(context.llm_client, "call_model_with_fallback", None)
        or context.llm_client.call_model
    )
    raw_response = await call_model(**kwargs)
    return _coerce_model_response(raw_response)


def _coerce_model_response(raw_response: Any) -> ModelResponse:
    if isinstance(raw_response, ModelResponse):
        return raw_response
    assistant_message = getattr(raw_response, "assistant_message", None)
    if not isinstance(assistant_message, dict):
        assistant_message = {"role": "assistant", "content": ""}
    tool_calls = getattr(raw_response, "tool_calls", None)
    if tool_calls is None:
        tool_calls = assistant_message.get("tool_calls") or []
    tool_map = getattr(raw_response, "tool_map", None) or {}
    usage = getattr(raw_response, "usage", None)
    if not isinstance(usage, TokenUsage):
        usage = TokenUsage()
    return ModelResponse(
        assistant_message=assistant_message,
        tool_calls=list(tool_calls or []),
        tool_map=dict(tool_map),
        stop_reason=getattr(raw_response, "stop_reason", None),
        usage=usage,
        messages=list(getattr(raw_response, "messages", []) or [assistant_message]),
        effective_model=getattr(raw_response, "effective_model", None),
    )


async def _run_side_query_tool_calls(
    tool_calls: list[dict[str, Any]],
    tool_map: dict[str, BaseTool],
    context: ToolUseContext,
    can_use_tool: SideQueryToolGate | None,
    *,
    assistant_message: dict[str, Any],
    query_source: str,
    denied_result_type: str,
) -> RunToolsResult:
    if can_use_tool is None:
        return await run_tools(
            tool_calls,
            tool_map,
            context,
            assistant_message=assistant_message,
        )

    final = RunToolsResult()
    pending_allowed: list[dict[str, Any]] = []

    async def flush_allowed() -> None:
        nonlocal final, pending_allowed, context
        if not pending_allowed:
            return
        batch = await run_tools(
            pending_allowed,
            tool_map,
            context,
            assistant_message=assistant_message,
        )
        final.messages.extend(batch.messages)
        final.prevent_continuation = final.prevent_continuation or batch.prevent_continuation
        final.stop_reason = final.stop_reason or batch.stop_reason
        if batch.updated_context is not None:
            context = batch.updated_context
            final.updated_context = batch.updated_context
        pending_allowed = []

    for call in tool_calls:
        tool_name = _tool_call_name(call)
        tool_input = _tool_call_input(call)
        tool = tool_map.get(tool_name) or find_tool_by_name(
            list(tool_map.values()),
            tool_name,
        )
        decision = await _call_tool_gate(can_use_tool, tool, tool_input)
        if decision.get("behavior") == "allow":
            updated_input = decision.get("updated_input")
            if isinstance(updated_input, dict):
                call = _replace_tool_call_input(call, updated_input)
            pending_allowed.append(call)
            continue

        await flush_allowed()
        final.messages.append(
            _build_denied_tool_result(
                tool_use_id=str(call.get("id") or ""),
                tool_name=tool_name or "unknown",
                message=str(decision.get("message") or "Denied by side-query tool gate."),
                query_source=query_source,
                denied_result_type=denied_result_type,
            )
        )

    await flush_allowed()
    return final


async def _call_tool_gate(
    can_use_tool: SideQueryToolGate,
    tool: BaseTool | None,
    tool_input: Mapping[str, Any],
) -> Mapping[str, Any]:
    decision = can_use_tool(tool, tool_input)
    if inspect.isawaitable(decision):
        decision = await decision
    if not isinstance(decision, Mapping):
        return {"behavior": "deny", "message": "Invalid side-query tool gate decision."}
    return decision


def _build_denied_tool_result(
    *,
    tool_use_id: str,
    tool_name: str,
    message: str,
    query_source: str,
    denied_result_type: str,
) -> dict[str, Any]:
    return build_tool_result_message(
        result=ToolResult(
            status=ToolStatus.ERROR,
            content=f"Error: {message}",
            error=message,
            metadata={"type": denied_result_type, "query_source": query_source},
        ),
        tool_call_id=tool_use_id,
        tool_name=tool_name,
    )


def _tool_call_name(tool_call: Mapping[str, Any]) -> str:
    function = tool_call.get("function")
    if isinstance(function, Mapping):
        name = function.get("name")
        if isinstance(name, str):
            return name
    name = tool_call.get("name")
    return name if isinstance(name, str) else ""


def _tool_call_input(tool_call: Mapping[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function")
    raw: Any = None
    if isinstance(function, Mapping):
        raw = function.get("arguments")
    elif "input" in tool_call:
        raw = tool_call.get("input")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _replace_tool_call_input(
    tool_call: Mapping[str, Any],
    updated_input: dict[str, Any],
) -> dict[str, Any]:
    cloned = dict(tool_call)
    function = cloned.get("function")
    if isinstance(function, Mapping):
        cloned["function"] = {
            **dict(function),
            "arguments": json.dumps(updated_input),
        }
    else:
        cloned["input"] = dict(updated_input)
    return cloned


async def _notify_message(
    callback: SideQueryMessageCallback | None,
    message: dict[str, Any],
    context: SideQueryContext,
) -> None:
    if callback is None:
        return
    result = callback(message, context)
    if inspect.isawaitable(result):
        await result


def _accumulate_usage(current: TokenUsage, update: TokenUsage | None) -> TokenUsage:
    if update is None:
        return current
    return TokenUsage(
        input_tokens=current.input_tokens + update.input_tokens,
        output_tokens=current.output_tokens + update.output_tokens,
        cache_creation_input_tokens=(
            current.cache_creation_input_tokens + update.cache_creation_input_tokens
        ),
        cache_read_input_tokens=current.cache_read_input_tokens + update.cache_read_input_tokens,
        total_tokens=current.total_tokens + update.total_tokens,
        reasoning_tokens=current.reasoning_tokens + update.reasoning_tokens,
        cost=current.cost + update.cost,
        web_search_requests=current.web_search_requests + update.web_search_requests,
    )


__all__ = [
    "SideQueryAbortController",
    "SideQueryContext",
    "SideQueryResult",
    "SideQueryToolGate",
    "extract_side_query_text",
    "run_side_query",
]
