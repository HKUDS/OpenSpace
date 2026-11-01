"""Hook system for tool execution lifecycle and agent loop control.

The core registry is callback-based. Skill Protocol frontmatter hooks
(``command``, ``prompt``, ``http``, and ``agent``) are adapted into scoped
callbacks in ``skill_engine/protocol.py`` so they share this execution engine.

Hooks emit plain message dictionaries and runtime events rather than UI-specific
attachment objects. Permission decisions are resolved in
``tool_runtime.pipeline.execution`` via ``has_permissions_to_use_tool``; hook decisions
can still provide allow/deny signals before that layer runs.

Supported events include tool lifecycle hooks, permission hooks, compact hooks,
session lifecycle hooks, prompt-submit hooks, notification hooks, and teammate
task hooks. Some enum values are intentionally reserved for optional future
features such as file watchers, setup hooks, worktree hooks, and enterprise MCP
elicitation flows.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import shlex
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from openspace.services.tooling.context import active_skill_scope_payload

if TYPE_CHECKING:
    from openspace.services.tooling.context import ToolUseContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hook events — OpenSpace/entrypoints/sdk/coreSchemas.ts:355  HOOK_EVENTS
# ---------------------------------------------------------------------------

class HookEvent(str, Enum):
    """All hook lifecycle events.

    Implementation: ``HOOK_EVENTS`` in ``entrypoints/sdk/coreSchemas.ts:355``.
    OS retains events relevant to agent loop / tool execution / multi-agent.
    """

    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    PERMISSION_REQUEST = "PermissionRequest"
    PERMISSION_DENIED = "PermissionDenied"
    STOP = "Stop"
    SUBAGENT_STOP = "SubagentStop"
    STOP_FAILURE = "StopFailure"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    NOTIFICATION = "Notification"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    TEAMMATE_IDLE = "TeammateIdle"
    TASK_COMPLETED = "TaskCompleted"


def is_hook_event(value: str) -> bool:
    """Implementation: ``isHookEvent`` in ``types/hooks.ts:22``."""
    try:
        HookEvent(value)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Result types — OpenSpace/utils/hooks.ts:330-376
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class HookBlockingError:
    """Implementation: ``HookBlockingError`` in ``utils/hooks.ts:330``.

    Represents a hook that blocks tool execution or agent continuation.
    """

    blocking_error: str
    command: str = ""


@dataclass(slots=True)
class HookResult:
    """Single hook callback result.

    Implementation: ``HookResult`` in ``utils/hooks.ts:338-357``.
    Returned by each registered hook callback.

    OpenSpace field mapping:
        message              → message (dict | None)
        systemMessage        → system_message
        blockingError        → blocking_error
        outcome              → outcome
        preventContinuation  → prevent_continuation
        stopReason           → stop_reason
        permissionBehavior   → permission_behavior
        hookPermissionDecisionReason → hook_permission_decision_reason
        additionalContext    → additional_context
        initialUserMessage   → initial_user_message
        updatedInput         → updated_input
        updatedPermissions   → updated_permissions
        updatedMCPToolOutput → updated_tool_output (renamed: OS has RemoteTool, not just MCP)
        watchPaths           → watch_paths
        elicitationResponse  → elicitation_response
        elicitationResultResponse → elicitation_result_response
        retry                → retry
    """

    message: dict[str, Any] | None = None
    system_message: str | None = None
    blocking_error: HookBlockingError | None = None
    outcome: Literal["success", "blocking", "non_blocking_error", "cancelled"] = "success"
    prevent_continuation: bool = False
    stop_reason: str | None = None
    permission_behavior: Literal["ask", "deny", "allow", "passthrough"] | None = None
    hook_permission_decision_reason: str | None = None
    additional_context: str | None = None
    initial_user_message: str | None = None
    updated_input: dict[str, Any] | None = None
    updated_permissions: list[Any] | None = None
    updated_tool_output: Any | None = None
    watch_paths: list[str] | None = None
    elicitation_response: dict[str, Any] | None = None
    elicitation_result_response: dict[str, Any] | None = None
    retry: bool = False


@dataclass(slots=True)
class AggregatedHookResult:
    """Aggregated result from multiple hooks for a single event.

    Implementation: ``AggregatedHookResult`` in ``utils/hooks.ts:359-376``.
    Built by the hook execution engine, consumed by toolHooks / stopHooks.

    OpenSpace field mapping:
        message              → message
        blockingError        → blocking_error (singular in OpenSpace too)
        preventContinuation  → prevent_continuation
        stopReason           → stop_reason
        hookPermissionDecisionReason → hook_permission_decision_reason
        hookSource           → hook_source
        permissionBehavior   → permission_behavior
        additionalContexts   → additional_contexts (plural in OpenSpace)
        initialUserMessage   → initial_user_message
        updatedInput         → updated_input
        updatedPermissions   → updated_permissions
        updatedMCPToolOutput → updated_tool_output
        watchPaths           → watch_paths
        elicitationResponse  → elicitation_response
        elicitationResultResponse → elicitation_result_response
        retry                → retry
    """

    message: dict[str, Any] | None = None
    blocking_error: HookBlockingError | None = None
    prevent_continuation: bool = False
    stop_reason: str | None = None
    hook_permission_decision_reason: str | None = None
    hook_source: str | None = None
    permission_behavior: Literal["allow", "deny", "ask", "passthrough"] | None = None
    additional_contexts: list[str] | None = None
    initial_user_message: str | None = None
    updated_input: dict[str, Any] | None = None
    updated_permissions: list[Any] | None = None
    updated_tool_output: Any | None = None
    watch_paths: list[str] | None = None
    elicitation_response: dict[str, Any] | None = None
    elicitation_result_response: dict[str, Any] | None = None
    retry: bool = False


@dataclass(slots=True)
class CompactHookResult:
    """Return type of ``run_pre_compact_hooks`` / ``run_post_compact_hooks``.

    Implementation: return type of ``executePreCompactHooks`` / ``executePostCompactHooks``.
    """

    new_custom_instructions: str | None = None
    user_display_message: str | None = None


# ---------------------------------------------------------------------------
# Pre-tool hook yield types — OpenSpace/services/tools/toolHooks.ts:413-430
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PreToolHookYield:
    """Typed yield from ``run_pre_tool_use_hooks``.

    Implementation: the discriminated union yielded by ``runPreToolUseHooks`` in
    ``services/tools/toolHooks.ts:413-430``.

    OpenSpace yield types (discriminated by ``type`` field):
        'message'             → {message: MessageUpdateLazy}
        'hookPermissionResult' → {hookPermissionResult: PermissionResult}
        'hookUpdatedInput'    → {updatedInput: Record<string, unknown>}
        'preventContinuation' → {shouldPreventContinuation: boolean}
        'stopReason'          → {stopReason: string}
        'additionalContext'   → {message: MessageUpdateLazy}
        'stop'                → (no payload)
    """

    type: Literal[
        "message",
        "hook_permission_result",
        "hook_updated_input",
        "prevent_continuation",
        "stop_reason",
        "additional_context",
        "stop",
    ]
    message: dict[str, Any] | None = None
    hook_permission_result: dict[str, Any] | None = None
    updated_input: dict[str, Any] | None = None
    stop_reason: str | None = None


@dataclass(slots=True, frozen=True)
class PostToolHookRuntimeState:
    """Per-tool-call runtime state for PostToolUse hooks.

    This is an explicit producer/consumer contract between the tool execution
    pipeline and post-tool hooks. It is intentionally per-call data rather than
    mutable shared context state so concurrent tool execution cannot race.
    """

    tool_call: Any
    backend: str
    tool: Any | None = None
    execution_time_ms: float = 0.0
    is_last_tool_call_in_iteration: bool = False
    guidance: str | None = None
    task_complete_token: str | None = None


@dataclass(slots=True)
class LifecycleHookResult:
    """Result for non-tool lifecycle hooks such as SessionStart/UserPromptSubmit."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    blocking_errors: list[dict[str, Any]] = field(default_factory=list)
    prevent_continuation: bool = False
    stop_reason: str | None = None
    additional_contexts: list[str] = field(default_factory=list)
    initial_user_message: str | None = None
    watch_paths: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Hook registration — OS-only (OpenSpace uses settings + external scripts)
# ---------------------------------------------------------------------------

HookCallback = Callable[..., Awaitable[HookResult]]
"""Signature for hook callbacks.

Pre-tool:  callback(tool_name, tool_input, context) -> HookResult
Post-tool: callback(tool_name, tool_input, tool_result, context) -> HookResult
Failure:   callback(tool_name, tool_input, error, context) -> HookResult
Stop:      callback(messages, last_response, context) -> HookResult
Compact:   callback(compact_data, context) -> HookResult
"""


@dataclass(slots=True)
class HookRegistration:
    """A single registered hook.

    Implementation: Within ``getMatchingHooks`` (utils/hooks.ts:1555), OpenSpace matches
    hooks by event + tool_name (via ``matcher`` field in ``HookMatcher``).
    OS uses the same pattern with ``tool_name`` as optional filter.
    """

    event: HookEvent
    callback: HookCallback
    tool_name: str | None = None
    """None = global hook matching all tools.  Specified = match only that tool.
    Implementation: ``matcher`` field in ``HookMatcherSchema`` (schemas/hooks.ts:161).
    """
    priority: int = 100
    """Lower number = higher priority.  OpenSpace runs hooks in parallel then merges;
    OS runs in priority order (sequential for pre-hooks to allow short-circuit,
    all for post-hooks).  This is consistent with OpenSpace's merge semantics:
    deny > ask > allow for permission behaviors.
    """
    name: str = ""
    """Human-readable name for logging/debugging.
    Implementation: hookName constructed as ``{hookEvent}:{matcher}`` in
    ``executeHooks`` (utils/hooks.ts:1987).
    """
    once: bool = False
    """If True, remove after first execution.
    Implementation: ``once`` field in ``BashCommandHookSchema`` (schemas/hooks.ts:57).
    """


@dataclass(slots=True, frozen=True)
class ConfiguredHookSpec:
    """Hook loaded from settings/session/plugin-style configuration."""

    event: HookEvent
    matcher: str
    hook: Mapping[str, Any]
    source: str
    root: str | None = None
    priority: int = 100
    session_scoped: bool = False
    session_hook_id: str | None = None
    on_hook_success: Callable[..., Any] | None = None


_SESSION_HOOK_ID_KEY = "_openspace_session_hook_id"
_SESSION_HOOK_SUCCESS_KEY = "_openspace_on_hook_success"


class HookRegistry:
    """Code-level hook registration and execution.

    Hook specs from settings, project hook files, plugins, sessions, and skills
    are resolved into callback registrations before execution.
    """

    def __init__(self) -> None:
        self._hooks: list[HookRegistration] = []
        self._once_removal_queue: list[HookRegistration] = []

    # ── Registration API ─────────────────────────────────────────────

    def register(
        self,
        event: HookEvent,
        callback: HookCallback,
        *,
        tool_name: str | None = None,
        priority: int = 100,
        name: str = "",
        once: bool = False,
    ) -> HookRegistration:
        """Register a hook callback for a specific event.

        Implementation: ``registerFrontmatterHooks`` / ``getRegisteredHooks``
        in ``utils/hooks.ts`` + ``bootstrap/state.ts``.

        Returns the registration object (can be passed to ``unregister``).
        """
        reg = HookRegistration(
            event=event,
            callback=callback,
            tool_name=tool_name,
            priority=priority,
            name=name,
            once=once,
        )
        self._hooks.append(reg)
        self._hooks.sort(key=lambda h: h.priority)
        return reg

    def register_pre_tool(
        self,
        callback: HookCallback,
        *,
        tool_name: str | None = None,
        priority: int = 100,
        name: str = "",
        once: bool = False,
    ) -> HookRegistration:
        """Convenience: register a PreToolUse hook."""
        return self.register(
            HookEvent.PRE_TOOL_USE, callback,
            tool_name=tool_name, priority=priority, name=name, once=once,
        )

    def register_post_tool(
        self,
        callback: HookCallback,
        *,
        tool_name: str | None = None,
        priority: int = 100,
        name: str = "",
        once: bool = False,
    ) -> HookRegistration:
        """Convenience: register a PostToolUse hook."""
        return self.register(
            HookEvent.POST_TOOL_USE, callback,
            tool_name=tool_name, priority=priority, name=name, once=once,
        )

    def register_stop_hook(
        self,
        callback: HookCallback,
        *,
        priority: int = 100,
        name: str = "",
        once: bool = False,
    ) -> HookRegistration:
        """Convenience: register a Stop hook."""
        return self.register(
            HookEvent.STOP, callback,
            priority=priority, name=name, once=once,
        )

    def unregister(self, registration: HookRegistration) -> bool:
        """Remove a previously registered hook.  Returns True if found."""
        try:
            self._hooks.remove(registration)
            return True
        except ValueError:
            return False

    def clear(self, event: HookEvent | None = None) -> int:
        """Remove all hooks, optionally filtered by event.  Returns count removed.

        Implementation: ``clearSessionHooks`` in ``utils/hooks/sessionHooks.ts``.
        """
        if event is None:
            count = len(self._hooks)
            self._hooks.clear()
            return count
        before = len(self._hooks)
        self._hooks = [h for h in self._hooks if h.event != event]
        return before - len(self._hooks)

    def has_hook_for_event(
        self,
        event: HookEvent,
        tool_name: str | None = None,
    ) -> bool:
        """Check if any hook is registered for the given event.

        Implementation: ``hasHookForEvent`` in ``utils/hooks.ts`` (~L1490).
        Used for fast-path short-circuit before constructing hook input.
        """
        return any(
            h.event == event
            and (h.tool_name is None or h.tool_name == tool_name)
            for h in self._hooks
        )

    # ── Matching ─────────────────────────────────────────────────────

    def _match_hooks(
        self,
        event: HookEvent,
        tool_name: str | None = None,
    ) -> list[HookRegistration]:
        """Return matching hooks sorted by priority.

        Implementation: ``getMatchingHooks`` in ``utils/hooks.ts:1555``.
        OpenSpace matching logic: for tool events, ``matchQuery = toolName``
        matched against hook's ``matcher`` field (exact or regex).
        OS simplified: exact ``tool_name`` match or global (None).
        """
        matches = [
            h for h in self._hooks
            if h.event == event
            and (h.tool_name is None or h.tool_name == tool_name)
        ]
        return matches  # already sorted by priority from register()

    def _drain_once_hooks(self) -> None:
        """Remove hooks marked ``once`` that were queued for removal."""
        for reg in self._once_removal_queue:
            try:
                self._hooks.remove(reg)
            except ValueError:
                pass
        self._once_removal_queue.clear()

    # ── Core execution engine ────────────────────────────────────────

    async def execute_hooks(
        self,
        event: HookEvent,
        tool_name: str | None = None,
        *,
        hook_kwargs: dict[str, Any] | None = None,
        context: ToolUseContext | None = None,
        abort_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[AggregatedHookResult, None]:
        """Core hook execution engine.

        Execution pattern:
            1. Find matching hooks.
            2. Stop early if the abort event is set.
            3. Emit start/progress events through the runtime context.
            4. Run matching callbacks by priority.
            5. Aggregate each callback result into ``AggregatedHookResult``.

        OS difference from Implementation: OpenSpace runs hooks in parallel then merges. OS runs
        hooks sequentially by priority for PreToolUse (need short-circuit on
        block/deny) and parallel for PostToolUse (no short-circuit needed).
        This matches OpenSpace's semantic: for PreToolUse, OpenSpace's merge applies
        deny > ask > allow, which sequential evaluation with short-circuit
        achieves naturally.
        """
        kwargs = hook_kwargs or {}
        matching = self._match_hooks(event, tool_name)
        if not matching:
            return

        if abort_event is not None and abort_event.is_set():
            return

        hook_name = f"{event.value}:{tool_name}" if tool_name else event.value

        if context is not None:
            await context.emit_event("hook_start", {
                "hook_event": event.value,
                "hook_name": hook_name,
                "hook_count": len(matching),
            })

        is_pre_tool = event == HookEvent.PRE_TOOL_USE

        try:
            for reg in matching:
                if abort_event is not None and abort_event.is_set():
                    yield AggregatedHookResult(
                        message=_make_hook_cancelled_message(hook_name, event),
                    )
                    return

                try:
                    single = await reg.callback(**kwargs)
                except Exception as exc:
                    logger.warning(
                        "Hook '%s' (%s) failed for %s: %s",
                        reg.name, event.value, tool_name, exc,
                    )
                    if is_pre_tool:
                        hook_label = reg.name or hook_name
                        yield AggregatedHookResult(
                            message=_make_hook_error_message(
                                hook_label, event, str(exc),
                            ),
                            blocking_error=HookBlockingError(
                                blocking_error=(
                                    f"Hook {hook_label} failed during pre-tool validation: {exc}"
                                ),
                            ),
                        )
                        return

                    yield AggregatedHookResult(
                        message=_make_hook_error_message(
                            reg.name or hook_name, event, str(exc),
                        ),
                    )
                    continue

                if reg.once:
                    self._once_removal_queue.append(reg)

                agg = _aggregate_single(single, reg.name or hook_name, event)
                yield agg

                if event == HookEvent.POST_TOOL_USE and agg.updated_tool_output is not None:
                    kwargs = dict(kwargs)
                    kwargs["tool_result"] = agg.updated_tool_output

                if is_pre_tool and (agg.blocking_error or agg.permission_behavior == "deny"):
                    break
        finally:
            self._drain_once_hooks()

            if context is not None:
                await context.emit_event("hook_complete", {
                    "hook_event": event.value,
                    "hook_name": hook_name,
                })


# ---------------------------------------------------------------------------
# Tool hook execution functions — OpenSpace/services/tools/toolHooks.ts
# ---------------------------------------------------------------------------

async def run_pre_tool_use_hooks(
    hook_registry: HookRegistry | None,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str,
    context: ToolUseContext | None = None,
) -> AsyncGenerator[PreToolHookYield, None]:
    """Execute PreToolUse hooks and yield typed results.

    Implementation: ``runPreToolUseHooks`` in ``services/tools/toolHooks.ts:413``.

    OpenSpace yield types → OS PreToolHookYield types:
        'message'              → type="message"
        'hookPermissionResult' → type="hook_permission_result"
        'hookUpdatedInput'     → type="hook_updated_input"
        'preventContinuation'  → type="prevent_continuation"
        'stopReason'           → type="stop_reason"
        'additionalContext'    → type="additional_context"
        'stop'                 → type="stop"

    OpenSpace branching logic per yielded AggregatedHookResult:
        1. result.message → yield {type: 'message', message}
        2. result.blockingError → yield {type: 'hookPermissionResult',
           hookPermissionResult: {behavior: 'deny', message: denialMessage, ...}}
        3. result.preventContinuation → yield {type: 'preventContinuation'}
           + optional {type: 'stopReason'}
        4. result.permissionBehavior defined:
           - 'allow' → yield {type: 'hookPermissionResult', ..., behavior: 'allow',
                        updatedInput: result.updatedInput}
           - 'ask' → yield {type: 'hookPermissionResult', ..., behavior: 'ask',
                     message: reason}
           - 'deny' → yield {type: 'hookPermissionResult', ..., behavior: 'deny',
                      message: reason}
        5. result.updatedInput without permissionBehavior → yield passthrough
           {type: 'hookUpdatedInput', updatedInput}
        6. result.additionalContexts → yield {type: 'additionalContext', message}
        7. aborted → yield cancelled message + {type: 'stop'}
    """
    if hook_registry is None or not hook_registry.has_hook_for_event(
        HookEvent.PRE_TOOL_USE, tool_name
    ):
        return

    abort_event = context.abort_event if context else None

    async for result in hook_registry.execute_hooks(
        HookEvent.PRE_TOOL_USE,
        tool_name,
        hook_kwargs={
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_use_id": tool_use_id,
            "context": context,
        },
        context=context,
        abort_event=abort_event,
    ):
        if result.message:
            yield PreToolHookYield(type="message", message=result.message)

        if result.blocking_error:
            denial_message = get_pre_tool_hook_blocking_message(
                f"PreToolUse:{tool_name}", result.blocking_error,
            )
            yield PreToolHookYield(
                type="hook_permission_result",
                hook_permission_result={
                    "behavior": "deny",
                    "message": denial_message,
                    "decision_reason": {
                        "type": "hook",
                        "hook_name": f"PreToolUse:{tool_name}",
                        "reason": denial_message,
                    },
                },
            )

        if result.prevent_continuation:
            yield PreToolHookYield(type="prevent_continuation")
            if result.stop_reason:
                yield PreToolHookYield(
                    type="stop_reason", stop_reason=result.stop_reason,
                )

        if result.permission_behavior is not None:
            hook_name = f"PreToolUse:{tool_name}"
            reason = result.hook_permission_decision_reason
            decision_reason = {
                "type": "hook",
                "hook_name": hook_name,
                "hook_source": result.hook_source,
                "reason": reason,
            }
            if result.permission_behavior == "allow":
                yield PreToolHookYield(
                    type="hook_permission_result",
                    hook_permission_result={
                        "behavior": "allow",
                        "updated_input": result.updated_input,
                        "decision_reason": decision_reason,
                    },
                )
            elif result.permission_behavior == "ask":
                yield PreToolHookYield(
                    type="hook_permission_result",
                    hook_permission_result={
                        "behavior": "ask",
                        "updated_input": result.updated_input,
                        "message": reason or f"Hook {hook_name} asks for permission",
                        "decision_reason": decision_reason,
                    },
                )
            elif result.permission_behavior == "deny":
                yield PreToolHookYield(
                    type="hook_permission_result",
                    hook_permission_result={
                        "behavior": "deny",
                        "message": reason or f"Hook {hook_name} denied this tool",
                        "decision_reason": decision_reason,
                    },
                )

        if result.updated_input and result.permission_behavior is None:
            yield PreToolHookYield(
                type="hook_updated_input",
                updated_input=result.updated_input,
            )

        if result.additional_contexts:
            yield PreToolHookYield(
                type="additional_context",
                message={
                    "role": "system",
                    "content": "\n".join(result.additional_contexts),
                    "_meta": {
                        "type": "hook_additional_context",
                        "hook_name": f"PreToolUse:{tool_name}",
                        "hook_event": "PreToolUse",
                    },
                },
            )

    if abort_event is not None and abort_event.is_set():
        yield PreToolHookYield(
            type="message",
            message=_make_hook_cancelled_message(
                f"PreToolUse:{tool_name}", HookEvent.PRE_TOOL_USE,
            ),
        )
        yield PreToolHookYield(type="stop")


async def run_post_tool_use_hooks(
    hook_registry: HookRegistry | None,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: Any,
    tool_use_id: str,
    context: ToolUseContext | None = None,
    *,
    post_tool_hook_state: PostToolHookRuntimeState | None = None,
) -> AsyncGenerator[AggregatedHookResult, None]:
    """Execute PostToolUse hooks.

    Implementation: ``runPostToolUseHooks`` in ``services/tools/toolHooks.ts:44``.

    OpenSpace branching per AggregatedHookResult:
        1. hook_cancelled attachment → yield cancelled message
        2. result.message (skip hook_blocking_error duplicates) → yield
        3. result.blockingError → yield hook_blocking_error attachment
        4. result.preventContinuation → yield hook_stopped_continuation + return
        5. result.additionalContexts → yield hook_additional_context attachment
        6. result.updatedMCPToolOutput (MCP only) → yield {updatedMCPToolOutput}

    ``post_tool_hook_state`` is the explicit per-call runtime contract used by
    production post-tool hooks. Callers should pass it directly rather than
    mutating ``ToolUseContext``.
    """
    if hook_registry is None or not hook_registry.has_hook_for_event(
        HookEvent.POST_TOOL_USE, tool_name
    ):
        return

    abort_event = context.abort_event if context else None

    async for result in hook_registry.execute_hooks(
        HookEvent.POST_TOOL_USE,
        tool_name,
        hook_kwargs={
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_result": tool_result,
            "tool_use_id": tool_use_id,
            "context": context,
            "post_tool_hook_state": post_tool_hook_state,
        },
        context=context,
        abort_event=abort_event,
    ):
        if result.message:
            yield result

        if result.blocking_error:
            yield AggregatedHookResult(
                message={
                    "role": "system",
                    "content": f"Hook blocking error: {result.blocking_error.blocking_error}",
                    "_meta": {
                        "type": "hook_blocking_error",
                        "hook_name": f"PostToolUse:{tool_name}",
                        "tool_use_id": tool_use_id,
                        "hook_event": "PostToolUse",
                        "blocking_error": result.blocking_error.blocking_error,
                    },
                },
            )

        if result.prevent_continuation:
            yield AggregatedHookResult(
                prevent_continuation=True,
                stop_reason=result.stop_reason or "Execution stopped by PostToolUse hook",
                message={
                    "role": "system",
                    "content": result.stop_reason or "Execution stopped by PostToolUse hook",
                    "_meta": {
                        "type": "hook_stopped_continuation",
                        "hook_name": f"PostToolUse:{tool_name}",
                        "hook_event": "PostToolUse",
                    },
                },
            )
            return

        if result.additional_contexts:
            yield AggregatedHookResult(
                additional_contexts=result.additional_contexts,
                message={
                    "role": "system",
                    "content": "\n".join(result.additional_contexts),
                    "_meta": {
                        "type": "hook_additional_context",
                        "hook_name": f"PostToolUse:{tool_name}",
                        "hook_event": "PostToolUse",
                    },
                },
            )

        if result.updated_tool_output is not None:
            yield AggregatedHookResult(updated_tool_output=result.updated_tool_output)


async def run_post_tool_use_failure_hooks(
    hook_registry: HookRegistry | None,
    tool_name: str,
    tool_input: dict[str, Any],
    error: str,
    tool_use_id: str,
    is_interrupt: bool = False,
    context: ToolUseContext | None = None,
) -> AsyncGenerator[AggregatedHookResult, None]:
    """Execute PostToolUseFailure hooks.

    Implementation: ``runPostToolUseFailureHooks`` in
    ``services/tools/toolHooks.ts:185``.

    Same branching as ``run_post_tool_use_hooks`` minus
    ``preventContinuation`` and ``updatedMCPToolOutput``.
    """
    if hook_registry is None or not hook_registry.has_hook_for_event(
        HookEvent.POST_TOOL_USE_FAILURE, tool_name
    ):
        return

    abort_event = context.abort_event if context else None

    async for result in hook_registry.execute_hooks(
        HookEvent.POST_TOOL_USE_FAILURE,
        tool_name,
        hook_kwargs={
            "tool_name": tool_name,
            "tool_input": tool_input,
            "error": error,
            "tool_use_id": tool_use_id,
            "is_interrupt": is_interrupt,
            "context": context,
        },
        context=context,
        abort_event=abort_event,
    ):
        if result.message:
            yield result

        if result.blocking_error:
            yield AggregatedHookResult(
                message={
                    "role": "system",
                    "content": f"Hook blocking error: {result.blocking_error.blocking_error}",
                    "_meta": {
                        "type": "hook_blocking_error",
                        "hook_name": f"PostToolUseFailure:{tool_name}",
                        "tool_use_id": tool_use_id,
                        "hook_event": "PostToolUseFailure",
                        "blocking_error": result.blocking_error.blocking_error,
                    },
                },
            )

        if result.additional_contexts:
            yield AggregatedHookResult(
                additional_contexts=result.additional_contexts,
                message={
                    "role": "system",
                    "content": "\n".join(result.additional_contexts),
                    "_meta": {
                        "type": "hook_additional_context",
                        "hook_name": f"PostToolUseFailure:{tool_name}",
                        "hook_event": "PostToolUseFailure",
                    },
                },
            )


# ---------------------------------------------------------------------------
# Permission hook resolution — OpenSpace/services/tools/toolHooks.ts:300-412
# ---------------------------------------------------------------------------

async def resolve_hook_permission_decision(
    hook_permission_result: dict[str, Any] | None,
    tool_name: str,
    tool_input: dict[str, Any],
    context: ToolUseContext,
    check_permission_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Resolve a PreToolUse hook's permission result into a final decision.

    Implementation: ``resolveHookPermissionDecision`` in
    ``services/tools/toolHooks.ts:300-412``.

    OpenSpace branching logic:
        hookResult.behavior == 'allow':
            → requiresInteraction && !interactionSatisfied → canUseTool
            → requireCanUseTool → canUseTool
            → checkRuleBasedPermissions:
                null → approve (hook allow takes effect)
                deny → deny (rule overrides hook)
                ask  → canUseTool (rule requires prompt)
        hookResult.behavior == 'deny':
            → deny
        hookResult.behavior == 'ask' or undefined:
            → canUseTool(forceDecision if 'ask')

    Permission hooks delegate to ``check_permission_fn`` when available so
    normal settings/rule checks still run after hook decisions.
    """
    if hook_permission_result is None:
        if check_permission_fn is not None:
            return await check_permission_fn(tool_name, tool_input, context)
        return {"behavior": "allow", "input": tool_input}

    behavior = hook_permission_result.get("behavior")
    updated_input = hook_permission_result.get("updated_input", tool_input)

    if behavior == "allow":
        if check_permission_fn is not None:
            pe = context.permission_engine
            if pe is not None and hasattr(pe, "check_rule_based"):
                rule_check = await pe.check_rule_based(tool_name, updated_input, context)
                if rule_check is not None:
                    if rule_check.get("behavior") == "deny":
                        return {"behavior": "deny", "input": updated_input, **rule_check}
                    if rule_check.get("behavior") == "ask":
                        return await check_permission_fn(
                            tool_name, updated_input, context,
                        )
        return {"behavior": "allow", "input": updated_input}

    if behavior == "deny":
        return {
            "behavior": "deny",
            "input": tool_input,
            "message": hook_permission_result.get("message", "Denied by hook"),
        }

    # 'ask' or no behavior → normal permission flow
    if check_permission_fn is not None:
        force_decision = hook_permission_result if behavior == "ask" else None
        return await check_permission_fn(
            tool_name,
            updated_input if behavior == "ask" else tool_input,
            context,
            force_decision=force_decision,
        )
    return {"behavior": "allow", "input": tool_input}


# ---------------------------------------------------------------------------
# Compact hooks — OpenSpace/utils/hooks.ts executePreCompactHooks / executePostCompactHooks
# ---------------------------------------------------------------------------

async def run_pre_compact_hooks(
    hook_registry: HookRegistry | None,
    compact_data: dict[str, Any],
    context: ToolUseContext | None = None,
) -> CompactHookResult:
    """Execute PreCompact hooks.

    Implementation: ``executePreCompactHooks`` in ``utils/hooks.ts`` (~L4100).
    OpenSpace returns: ``{newCustomInstructions?, userDisplayMessage?}``
    """
    result = CompactHookResult()
    if hook_registry is None or not hook_registry.has_hook_for_event(
        HookEvent.PRE_COMPACT
    ):
        return result

    async for agg in hook_registry.execute_hooks(
        HookEvent.PRE_COMPACT,
        hook_kwargs={"compact_data": compact_data, "context": context},
        context=context,
    ):
        if agg.additional_contexts:
            result.new_custom_instructions = "\n".join(agg.additional_contexts)
        if agg.message and isinstance(agg.message.get("content"), str):
            if result.user_display_message:
                result.user_display_message += "\n" + agg.message["content"]
            else:
                result.user_display_message = agg.message["content"]

    return result


async def run_post_compact_hooks(
    hook_registry: HookRegistry | None,
    compact_data: dict[str, Any],
    context: ToolUseContext | None = None,
) -> CompactHookResult:
    """Execute PostCompact hooks.

    Implementation: ``executePostCompactHooks`` in ``utils/hooks.ts`` (~L4150).
    OpenSpace returns: ``{userDisplayMessage?}``
    """
    result = CompactHookResult()
    if hook_registry is None or not hook_registry.has_hook_for_event(
        HookEvent.POST_COMPACT
    ):
        return result

    async for agg in hook_registry.execute_hooks(
        HookEvent.POST_COMPACT,
        hook_kwargs={"compact_data": compact_data, "context": context},
        context=context,
    ):
        if agg.message and isinstance(agg.message.get("content"), str):
            if result.user_display_message:
                result.user_display_message += "\n" + agg.message["content"]
            else:
                result.user_display_message = agg.message["content"]

    return result


# ---------------------------------------------------------------------------
# Notification / misc hooks — OpenSpace/utils/hooks.ts executeNotificationHooks etc.
# ---------------------------------------------------------------------------

async def run_notification_hooks(
    hook_registry: HookRegistry | None,
    message: str,
    *,
    title: str | None = None,
    notification_type: str = "info",
    context: ToolUseContext | None = None,
) -> None:
    """Execute Notification hooks (fire-and-forget).

    Implementation: ``executeNotificationHooks`` in ``utils/hooks.ts:3570``.
    """
    if hook_registry is None or not hook_registry.has_hook_for_event(
        HookEvent.NOTIFICATION
    ):
        return

    async for _ in hook_registry.execute_hooks(
        HookEvent.NOTIFICATION,
        hook_kwargs={
            "message": message,
            "title": title,
            "notification_type": notification_type,
            "context": context,
        },
        context=context,
    ):
        pass  # fire-and-forget: consume but don't act on results


async def run_session_start_hooks(
    hook_registry: HookRegistry | None,
    *,
    source: Literal["startup", "resume", "clear", "compact"] = "startup",
    context: ToolUseContext | None = None,
    session_id: str | None = None,
    agent_type: str | None = None,
    model: str | None = None,
) -> LifecycleHookResult:
    """Execute SessionStart hooks before the first model call.

    Implementation: ``processSessionStartHooks`` / ``executeSessionStartHooks``.
    Blocking errors are reported as messages but do not abort the session, which
    matches OpenSpace's SessionStart behavior.
    """
    result = LifecycleHookResult()
    if hook_registry is None or not hook_registry.has_hook_for_event(
        HookEvent.SESSION_START, source
    ):
        return result

    async for agg in hook_registry.execute_hooks(
        HookEvent.SESSION_START,
        source,
        hook_kwargs={
            "source": source,
            "session_id": session_id,
            "agent_type": agent_type,
            "model": model,
            "context": context,
        },
        context=context,
    ):
        _collect_lifecycle_hook_result(
            result,
            agg,
            hook_name="SessionStart",
            blocking_is_terminal=False,
        )

    return result


async def run_user_prompt_submit_hooks(
    hook_registry: HookRegistry | None,
    prompt: str,
    *,
    context: ToolUseContext | None = None,
    permission_mode: str | None = None,
) -> LifecycleHookResult:
    """Execute UserPromptSubmit hooks after slash parsing and before LLM input."""
    result = LifecycleHookResult()
    if hook_registry is None or not hook_registry.has_hook_for_event(
        HookEvent.USER_PROMPT_SUBMIT
    ):
        return result

    async for agg in hook_registry.execute_hooks(
        HookEvent.USER_PROMPT_SUBMIT,
        hook_kwargs={
            "prompt": prompt,
            "permission_mode": permission_mode,
            "context": context,
        },
        context=context,
    ):
        _collect_lifecycle_hook_result(
            result,
            agg,
            hook_name="UserPromptSubmit",
            blocking_is_terminal=True,
        )
        if result.blocking_errors or result.prevent_continuation:
            break

    return result


def _collect_lifecycle_hook_result(
    result: LifecycleHookResult,
    agg: AggregatedHookResult,
    *,
    hook_name: str,
    blocking_is_terminal: bool,
) -> None:
    if agg.message:
        result.messages.append(agg.message)
    if agg.additional_contexts:
        result.additional_contexts.extend(str(item) for item in agg.additional_contexts)
    if agg.initial_user_message:
        result.initial_user_message = agg.initial_user_message
    if agg.watch_paths:
        result.watch_paths.extend(str(path) for path in agg.watch_paths)

    if agg.blocking_error:
        result.blocking_errors.append(
            {
                "role": "system",
                "content": _format_lifecycle_blocking_error(hook_name, agg.blocking_error),
                "_meta": {
                    "type": "hook_blocking_error",
                    "hook_event": hook_name,
                    "command": agg.blocking_error.command,
                },
            }
        )
        if blocking_is_terminal:
            result.prevent_continuation = True
            result.stop_reason = agg.stop_reason or agg.blocking_error.blocking_error
    if agg.prevent_continuation:
        result.prevent_continuation = True
        result.stop_reason = agg.stop_reason or result.stop_reason


def _format_lifecycle_blocking_error(
    hook_name: str,
    blocking_error: HookBlockingError,
) -> str:
    command = f' from command: "{blocking_error.command}"' if blocking_error.command else ""
    return f"{hook_name} hook blocking error{command}: {blocking_error.blocking_error}"


async def run_session_end_hooks(
    hook_registry: HookRegistry | None,
    reason: str,
    context: ToolUseContext | None = None,
) -> None:
    """Execute SessionEnd hooks for active session-scoped callbacks."""
    if hook_registry is None or not hook_registry.has_hook_for_event(
        HookEvent.SESSION_END
    ):
        return

    async for _ in hook_registry.execute_hooks(
        HookEvent.SESSION_END,
        hook_kwargs={
            "reason": reason,
            "context": context,
        },
        context=context,
    ):
        pass


async def run_stop_failure_hooks(
    hook_registry: HookRegistry | None,
    error: Any,
    *,
    error_details: str | None = None,
    last_assistant_message: str | None = None,
    context: ToolUseContext | None = None,
) -> None:
    """Execute StopFailure hooks after agent-loop failure."""
    if hook_registry is None or not hook_registry.has_hook_for_event(
        HookEvent.STOP_FAILURE
    ):
        return

    async for _ in hook_registry.execute_hooks(
        HookEvent.STOP_FAILURE,
        hook_kwargs={
            "error": error,
            "error_details": error_details,
            "last_assistant_message": last_assistant_message,
            "context": context,
        },
        context=context,
    ):
        pass


async def run_permission_denied_hooks(
    hook_registry: HookRegistry | None,
    tool_name: str,
    tool_input: dict[str, Any],
    reason: str,
    tool_use_id: str,
    context: ToolUseContext | None = None,
) -> bool:
    """Execute PermissionDenied hooks and report whether the model may retry.

    Implementation: ``executePermissionDeniedHooks`` in ``utils/hooks.ts:3529``.
    """
    if hook_registry is None or not hook_registry.has_hook_for_event(
        HookEvent.PERMISSION_DENIED, tool_name,
    ):
        return False

    hook_says_retry = False
    async for result in hook_registry.execute_hooks(
        HookEvent.PERMISSION_DENIED,
        tool_name,
        hook_kwargs={
            "tool_name": tool_name,
            "tool_input": tool_input,
            "reason": reason,
            "tool_use_id": tool_use_id,
            "context": context,
        },
        context=context,
    ):
        if result.retry:
            hook_says_retry = True

    return hook_says_retry


# ---------------------------------------------------------------------------
# Message helpers — OpenSpace/utils/hooks.ts getPreToolHookBlockingMessage etc.
# ---------------------------------------------------------------------------

def get_pre_tool_hook_blocking_message(
    hook_name: str,
    blocking_error: HookBlockingError,
) -> str:
    """Format a blocking error message for a PreToolUse hook.

    Implementation: ``getPreToolHookBlockingMessage`` in ``utils/hooks.ts``.
    OpenSpace format: ``"Hook {hookName} blocked: {blockingError}"``
    """
    return (
        f"Hook {hook_name} blocked this action: "
        f"{blocking_error.blocking_error}"
    )


def get_stop_hook_message(blocking_error: HookBlockingError) -> str:
    """Format a blocking error message for a Stop hook.

    Implementation: ``getStopHookMessage`` in ``utils/hooks.ts``.
    """
    return f"Stop hook feedback:\n{blocking_error.blocking_error}"


def get_teammate_idle_hook_message(blocking_error: HookBlockingError) -> str:
    """Implementation: ``getTeammateIdleHookMessage`` in ``utils/hooks.ts``."""
    return f"TeammateIdle hook feedback:\n{blocking_error.blocking_error}"


def get_task_completed_hook_message(blocking_error: HookBlockingError) -> str:
    """Implementation: ``getTaskCompletedHookMessage`` in ``utils/hooks.ts``."""
    return f"TaskCompleted hook feedback:\n{blocking_error.blocking_error}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _aggregate_single(
    single: HookResult,
    hook_name: str,
    event: HookEvent,
) -> AggregatedHookResult:
    """Convert a single HookResult to AggregatedHookResult.

    Implementation: The aggregation logic inside ``executeHooks`` generator loop
    (``utils/hooks.ts`` ~L2143-2200) that merges individual hook results.

    OpenSpace aggregation rules:
        - permissionBehavior merges as deny > ask > allow > passthrough
        - blockingError takes the first one
        - additionalContexts accumulate
        - updatedInput takes the last one
    """
    agg = AggregatedHookResult(
        blocking_error=single.blocking_error,
        prevent_continuation=single.prevent_continuation,
        stop_reason=single.stop_reason,
        hook_permission_decision_reason=single.hook_permission_decision_reason,
        permission_behavior=single.permission_behavior,
        initial_user_message=single.initial_user_message,
        updated_input=single.updated_input,
        updated_permissions=single.updated_permissions,
        updated_tool_output=single.updated_tool_output,
        watch_paths=single.watch_paths,
        elicitation_response=single.elicitation_response,
        elicitation_result_response=single.elicitation_result_response,
        retry=single.retry,
    )

    if single.additional_context:
        agg.additional_contexts = [single.additional_context]

    if single.message:
        agg.message = single.message
    elif single.system_message:
        agg.message = {
            "role": "system",
            "content": single.system_message,
            "_meta": {
                "type": "hook_system_message",
                "hook_name": hook_name,
                "hook_event": event.value,
            },
        }
    elif single.blocking_error:
        agg.message = {
            "role": "system",
            "content": f"Hook blocked: {single.blocking_error.blocking_error}",
            "_meta": {
                "type": "hook_blocking_error",
                "hook_name": hook_name,
                "hook_event": event.value,
                "blocking_error": single.blocking_error.blocking_error,
            },
        }

    return agg


def _make_hook_cancelled_message(
    hook_name: str,
    event: HookEvent,
) -> dict[str, Any]:
    """Create a cancellation message for UI.

    Implementation: ``createAttachmentMessage({type: 'hook_cancelled', ...})``
    in ``services/tools/toolHooks.ts``.
    """
    return {
        "role": "system",
        "content": f"Hook {hook_name} was cancelled",
        "_meta": {
            "type": "hook_cancelled",
            "hook_name": hook_name,
            "hook_event": event.value,
        },
    }


def _make_hook_error_message(
    hook_name: str,
    event: HookEvent,
    error: str,
) -> dict[str, Any]:
    """Create an error message for a failed hook.

    Implementation: ``createAttachmentMessage({type: 'hook_error_during_execution', ...})``
    in ``services/tools/toolHooks.ts``.
    """
    return {
        "role": "system",
        "content": f"Hook {hook_name} error: {error}",
        "_meta": {
            "type": "hook_error_during_execution",
            "hook_name": hook_name,
            "hook_event": event.value,
            "error": error,
        },
    }


# ---------------------------------------------------------------------------
# Built-in hooks — registered during initialization
# ---------------------------------------------------------------------------

def create_max_tokens_recovery_hook() -> HookCallback:
    """Create a Stop hook that handles max_tokens truncation.

    Implementation: Part of ``handleStopHooks`` in ``query/stopHooks.ts``,
    specifically the logic that detects ``stop_reason === 'length'`` and
    injects a recovery message.

    In OpenSpace this is inline in the query loop (``query.ts``).
    OS moves it to a hook for cleaner separation.
    """

    async def _max_tokens_recovery(
        messages: list[dict[str, Any]],
        last_response: Any,
        context: ToolUseContext,
        **_: Any,
    ) -> HookResult:
        stop_reason = getattr(last_response, "stop_reason", None)
        if stop_reason == "length":
            return HookResult(
                prevent_continuation=False,
                system_message=(
                    "Your response was truncated due to output length limits. "
                    "Please continue where you left off."
                ),
                outcome="success",
            )
        return HookResult()

    return _max_tokens_recovery


def create_permission_check_hook() -> HookCallback:
    """Create a legacy PreToolUse adapter for external permission engines.

    The canonical permission path is the tool runtime's
    ``_resolve_permissions()`` call into ``has_permissions_to_use_tool``.
    This adapter is not registered by default; callers that still inject a
    custom ``context.permission_engine`` may opt in explicitly.
    """

    async def _permission_check(
        tool_name: str = "",
        tool_input: dict[str, Any] | None = None,
        tool_use_id: str = "",
        context: ToolUseContext | None = None,
        **_: Any,
    ) -> HookResult:
        if context is None or context.permission_engine is None:
            return HookResult()

        pe = context.permission_engine
        if hasattr(pe, "check_permissions"):
            try:
                decision = await pe.check_permissions(tool_name, tool_input or {}, context)
                behavior = decision.get("behavior", "allow") if isinstance(decision, dict) else "allow"
                if behavior == "deny":
                    return HookResult(
                        permission_behavior="deny",
                        hook_permission_decision_reason=decision.get("message", "Denied by permission engine"),
                        outcome="blocking",
                    )
                if behavior == "ask":
                    return HookResult(
                        permission_behavior="ask",
                        hook_permission_decision_reason=decision.get("message"),
                    )
            except Exception as exc:
                logger.warning("Permission check hook error: %s", exc)

        return HookResult()

    return _permission_check


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _tool_result_success(tool_result: Any) -> bool:
    if hasattr(tool_result, "is_success"):
        return bool(getattr(tool_result, "is_success"))
    status = getattr(tool_result, "status", None)
    if status is None:
        return True
    return str(status).lower().endswith("success")


def _tool_result_record_payload(tool_result: Any) -> Any:
    if not _tool_result_success(tool_result):
        error = getattr(tool_result, "error", None)
        if error:
            return error
    return getattr(tool_result, "content", tool_result)


def _tool_result_metadata(tool_result: Any) -> dict[str, Any] | None:
    metadata = getattr(tool_result, "metadata", None)
    return metadata if isinstance(metadata, dict) else None


def _callable_accepts_var_kwargs(callback: Callable[..., Any]) -> bool:
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return False
    return any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        for param in signature.parameters.values()
    )


def create_recording_hook() -> HookCallback:
    """Create a PostToolUse hook that records tool calls to recording_manager.

    DEC-020 §四 hook (2): Recording hook (PostToolUse, priority=200).
    Replaces the recording logic previously hardcoded in BaseTool._auto_record_execution.

    Implementation: OpenSpace records tool calls in ``toolExecution.ts`` ``runToolUse``
    after tool execution completes.  OS extracts recording to a hook.
    """

    async def _recording(
        tool_name: str = "",
        tool_input: dict[str, Any] | None = None,
        tool_result: Any = None,
        tool_use_id: str = "",
        context: ToolUseContext | None = None,
        post_tool_hook_state: PostToolHookRuntimeState | None = None,
        **_: Any,
    ) -> HookResult:
        if context is None or context.recording_manager is None:
            return HookResult()

        rm = context.recording_manager
        runtime_state = _get_post_tool_hook_state(post_tool_hook_state)
        backend = runtime_state.backend if runtime_state is not None else "unknown"
        server_name = None
        tool = runtime_state.tool if runtime_state is not None else None
        runtime_info = getattr(tool, "_runtime_info", None)
        if runtime_info is not None:
            server_name = getattr(runtime_info, "server_name", None)

        try:
            if hasattr(rm, "record_tool_execution"):
                await _await_if_needed(rm.record_tool_execution(
                    tool_name=tool_name,
                    backend=backend,
                    parameters=tool_input or {},
                    result=_tool_result_record_payload(tool_result),
                    server_name=server_name,
                    is_success=_tool_result_success(tool_result),
                    metadata={
                        **(_tool_result_metadata(tool_result) or {}),
                        "tool_use_id": tool_use_id,
                    },
                ))
        except Exception as exc:
            logger.warning("Recording hook error (non-fatal): %s", exc)

        return HookResult()

    return _recording


def create_quality_hook() -> HookCallback:
    """Create a PostToolUse hook that updates ToolQualityManager statistics.

    DEC-020 §四 hook (3): Quality hook (PostToolUse, priority=210).
    Feeds tool execution outcomes into quality tracking for evolution triggers.
    """

    async def _quality_tracking(
        tool_name: str = "",
        tool_input: dict[str, Any] | None = None,
        tool_result: Any = None,
        tool_use_id: str = "",
        context: ToolUseContext | None = None,
        post_tool_hook_state: PostToolHookRuntimeState | None = None,
        **_: Any,
    ) -> HookResult:
        if context is None or context.quality_manager is None:
            return HookResult()

        qm = context.quality_manager
        recorded_ids = getattr(context, "quality_recorded_tool_use_ids", None)
        if isinstance(recorded_ids, set) and tool_use_id in recorded_ids:
            return HookResult()
        runtime_state = _get_post_tool_hook_state(post_tool_hook_state)
        tool = runtime_state.tool if runtime_state is not None else None
        execution_time_ms = (
            runtime_state.execution_time_ms if runtime_state is not None else 0.0
        )
        quality_record = None
        try:
            if hasattr(qm, "record_execution"):
                if tool is not None and _is_tool_result_like(tool_result):
                    await _await_if_needed(
                        qm.record_execution(tool, tool_result, execution_time_ms)
                    )
                    get_record = getattr(qm, "get_record", None)
                    if callable(get_record):
                        quality_record = get_record(tool)
                    if isinstance(recorded_ids, set):
                        recorded_ids.add(tool_use_id)
                elif _callable_accepts_var_kwargs(qm.record_execution):
                    await _await_if_needed(qm.record_execution(
                        tool_name=tool_name,
                        success=_tool_result_success(tool_result),
                        result=tool_result,
                        tool_use_id=tool_use_id,
                    ))
                    if isinstance(recorded_ids, set):
                        recorded_ids.add(tool_use_id)
                else:
                    logger.debug(
                        "Quality hook skipped for %s: missing tool/runtime ToolResult",
                        tool_name,
                    )
        except Exception as exc:
            logger.warning("Quality hook error (non-fatal): %s", exc)
        if quality_record is not None:
            await _emit_tool_quality_evidence(
                context,
                qm,
                quality_record,
                tool_use_id=tool_use_id,
                execution_time_ms=execution_time_ms,
            )

        return HookResult()

    return _quality_tracking


async def _emit_tool_quality_evidence(
    context: ToolUseContext,
    quality_manager: Any,
    record: Any,
    *,
    tool_use_id: str,
    execution_time_ms: float,
    source: str = "quality_tracking_hook",
) -> None:
    payload = _tool_quality_evidence_payload(
        record,
        quality_manager=quality_manager,
        tool_use_id=tool_use_id,
        execution_time_ms=execution_time_ms,
        source=source,
    )
    if not payload:
        return
    payload.update(
        {
            "session_id": getattr(context, "session_id", None),
            "task_id": getattr(context, "task_id", None),
            "agent_id": getattr(context, "agent_id", None),
            "parent_task_id": getattr(context, "parent_task_id", None),
            "current_iteration": getattr(context, "current_iteration", None),
        }
    )
    payload.update(active_skill_scope_payload(context))
    await context.emit_event("tool_quality_recorded", payload)


def _tool_quality_evidence_payload(
    record: Any,
    *,
    quality_manager: Any,
    tool_use_id: str,
    execution_time_ms: float,
    source: str = "quality_tracking_hook",
) -> dict[str, Any] | None:
    tool_key = getattr(record, "tool_key", None)
    if not tool_key:
        return None
    payload = {
        "tool_key": tool_key,
        "backend": getattr(record, "backend", None),
        "server": getattr(record, "server", None),
        "tool_name": getattr(record, "tool_name", None),
        "total_calls": getattr(record, "total_calls", None),
        "success_count": getattr(record, "success_count", None),
        "recent_success_rate": getattr(record, "recent_success_rate", None),
        "last_updated": _isoformat_or_none(getattr(record, "last_updated", None)),
        "tool_use_id": tool_use_id,
        "execution_time_ms": execution_time_ms,
        "source": source,
    }
    history: list[dict[str, Any]] = []
    store = getattr(quality_manager, "_store", None)
    if store is not None and hasattr(store, "load_recent_history"):
        try:
            history = list(store.load_recent_history(str(tool_key), limit=5))
            if history:
                _annotate_current_quality_history_item(
                    history,
                    index=0,
                    tool_use_id=tool_use_id,
                    source=source,
                )
        except Exception:
            history = []
    if not history:
        for item in list(getattr(record, "recent_executions", []) or [])[-5:]:
            history.append(
                {
                    "timestamp": _isoformat_or_none(getattr(item, "timestamp", None)),
                    "success": getattr(item, "success", None),
                    "execution_time_ms": getattr(item, "execution_time_ms", None),
                    "error_message": getattr(item, "error_message", None),
                }
            )
        if history:
            _annotate_current_quality_history_item(
                history,
                index=len(history) - 1,
                tool_use_id=tool_use_id,
                source=source,
            )
    payload["history"] = history
    return payload


def _annotate_current_quality_history_item(
    history: list[dict[str, Any]],
    *,
    index: int,
    tool_use_id: str,
    source: str,
) -> None:
    if not history or not tool_use_id:
        return
    try:
        item = history[index]
    except IndexError:
        return
    if not isinstance(item, dict):
        return
    item.setdefault("tool_use_id", tool_use_id)
    item.setdefault("source", source)


def _isoformat_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _get_post_tool_hook_state(
    post_tool_hook_state: PostToolHookRuntimeState | None,
) -> PostToolHookRuntimeState | None:
    """Return optional producer-side runtime state for post-tool hooks.

    The runtime state is passed explicitly per hook invocation. Post-tool hooks
    should not read per-call state from ``ToolUseContext``.
    """
    return post_tool_hook_state


def _normalize_visual_tool_call(
    runtime_state: PostToolHookRuntimeState,
) -> Any:
    """Return a GUI VisualAnalysisHook-compatible tool call object."""
    runtime_tool_call = runtime_state.tool_call
    if hasattr(runtime_tool_call, "function"):
        return runtime_tool_call
    if isinstance(runtime_tool_call, Mapping):
        function = runtime_tool_call.get("function")
        if isinstance(function, Mapping):
            return SimpleNamespace(
                id=runtime_tool_call.get("id", ""),
                function=SimpleNamespace(
                    name=function.get("name", ""),
                    arguments=function.get("arguments", {}),
                ),
            )
    raise TypeError("post_tool_hook_state.tool_call must expose function arguments")


def _is_tool_result_like(value: Any) -> bool:
    """Return whether ``value`` looks like a mutable tool result object."""
    return all(
        hasattr(value, attr)
        for attr in ("status", "content", "metadata", "error", "execution_time")
    )


def create_visual_analysis_hook(visual_hook: Any | None = None) -> HookCallback:
    """Create a PostToolUse hook for GUI visual analysis.

    DEC-020 §四 hook (4) / DEC-022 §二: GUI Visual Analysis hook
    (PostToolUse, GUI tools only, priority=50).

    Replaces the old agent-owned visual-analysis callback path.
    Runtime dependencies come from ``ToolUseContext`` and GUI backend config.

    Implementation: OpenSpace does not have a separate visual analysis hook — it's
    an OS-specific feature (DEC-022).
    """
    if visual_hook is None:
        from openspace.grounding.backends.gui.hooks import VisualAnalysisHook

        visual_hook = VisualAnalysisHook()

    async def _visual_analysis(
        tool_name: str = "",
        tool_input: dict[str, Any] | None = None,
        tool_result: Any = None,
        tool_use_id: str = "",
        context: ToolUseContext | None = None,
        post_tool_hook_state: PostToolHookRuntimeState | None = None,
        **_: Any,
    ) -> HookResult:
        runtime_state = _get_post_tool_hook_state(post_tool_hook_state)
        if runtime_state is None or runtime_state.backend != "gui":
            return HookResult()

        if not _is_tool_result_like(tool_result):
            return HookResult()

        if not hasattr(visual_hook, "analyze_tool_result"):
            return HookResult()

        analyzed_result = await visual_hook.analyze_tool_result(
            result=tool_result,
            tool_name=tool_name,
            tool_call=_normalize_visual_tool_call(runtime_state),
            backend=runtime_state.backend,
            task_description=(
                getattr(context, "task_description", "")
            ),
            context=context,
        )
        if analyzed_result is None:
            return HookResult()
        return HookResult(updated_tool_output=analyzed_result)

    return _visual_analysis


def add_session_hook(
    context: ToolUseContext,
    event: HookEvent | str,
    matcher: str,
    hook: Mapping[str, Any],
    *,
    hook_id: str | None = None,
    once: bool | None = None,
    on_hook_success: Callable[..., Any] | None = None,
) -> str:
    """Add a OpenSpace JSON hook spec scoped to one ``ToolUseContext``.

    Returns the generated hook id so callers can remove one hook without
    clearing the entire event.
    """

    hook_event = event if isinstance(event, HookEvent) else HookEvent(str(event))
    session_hook_id = hook_id or f"session-hook-{uuid.uuid4().hex}"
    hook_payload = dict(hook)
    hook_payload[_SESSION_HOOK_ID_KEY] = session_hook_id
    if once is not None:
        hook_payload["once"] = bool(once)
    if on_hook_success is not None:
        hook_payload[_SESSION_HOOK_SUCCESS_KEY] = on_hook_success
    _append_session_hook_payload(context, hook_event, matcher, hook_payload)
    return session_hook_id


def add_session_function_hook(
    context: ToolUseContext,
    event: HookEvent | str,
    matcher: str,
    callback: Callable[..., Any],
    error_message: str,
    *,
    hook_id: str | None = None,
    timeout: float | None = None,
    once: bool = False,
    on_hook_success: Callable[..., Any] | None = None,
) -> str:
    """Add an in-memory function hook scoped to one ``ToolUseContext``.

    Function hooks receive the current message list and should return ``True``
    to pass or ``False`` to block with ``error_message``.
    """

    if not callable(callback):
        raise TypeError("callback must be callable")
    hook_event = event if isinstance(event, HookEvent) else HookEvent(str(event))
    session_hook_id = hook_id or f"function-hook-{uuid.uuid4().hex}"
    hook: dict[str, Any] = {
        "type": "function",
        "id": session_hook_id,
        "callback": callback,
        "errorMessage": error_message,
        _SESSION_HOOK_ID_KEY: session_hook_id,
    }
    if timeout is not None:
        hook["timeout"] = timeout
    if once:
        hook["once"] = True
    if on_hook_success is not None:
        hook[_SESSION_HOOK_SUCCESS_KEY] = on_hook_success
    _append_session_hook_payload(context, hook_event, matcher, hook)
    return session_hook_id


def remove_session_hook(
    context: ToolUseContext,
    event: HookEvent | str,
    hook_or_id: Mapping[str, Any] | str,
    *,
    matcher: str | None = None,
) -> int:
    """Remove one session hook by generated id or OpenSpace hook identity."""

    hook_event = event if isinstance(event, HookEvent) else HookEvent(str(event))
    return _remove_session_hook_from_hooks_map(
        context.session_hooks,
        hook_event,
        hook_or_id,
        matcher=matcher,
    )


def _remove_session_hook_from_hooks_map(
    session_hooks: Any,
    event: HookEvent,
    hook_or_id: Mapping[str, Any] | str,
    *,
    matcher: str | None = None,
) -> int:
    if not isinstance(session_hooks, dict):
        return 0
    event_hooks = session_hooks.get(event.value)
    if not isinstance(event_hooks, list):
        return 0

    removed = 0
    updated_matchers: list[dict[str, Any]] = []
    for matcher_config in event_hooks:
        if not isinstance(matcher_config, Mapping):
            continue
        matcher_value = str(matcher_config.get("matcher") or "")
        hooks = matcher_config.get("hooks")
        if not isinstance(hooks, Sequence) or isinstance(
            hooks,
            (str, bytes, bytearray),
        ):
            continue
        retained: list[Any] = []
        for hook in hooks:
            should_remove = False
            if matcher is None or matcher == matcher_value:
                should_remove = _session_hook_matches_remove_target(hook, hook_or_id)
            if should_remove:
                removed += 1
            else:
                retained.append(hook)
        if retained:
            updated = dict(matcher_config)
            updated["hooks"] = retained
            updated_matchers.append(updated)

    if updated_matchers:
        session_hooks[event.value] = updated_matchers
    else:
        session_hooks.pop(event.value, None)
    return removed


def _remove_channel_context_session_hook(
    context: ToolUseContext,
    event: HookEvent,
    hook_or_id: Mapping[str, Any] | str,
    *,
    matcher: str | None = None,
) -> int:
    channel_context = getattr(context, "channel_context", None)
    if not isinstance(channel_context, dict):
        return 0
    raw_session_hooks = channel_context.get("session_hooks")
    if isinstance(raw_session_hooks, dict):
        return _remove_session_hook_from_hooks_map(
            raw_session_hooks,
            event,
            hook_or_id,
            matcher=matcher,
        )
    if isinstance(raw_session_hooks, Mapping):
        copied = dict(raw_session_hooks)
        removed = _remove_session_hook_from_hooks_map(
            copied,
            event,
            hook_or_id,
            matcher=matcher,
        )
        if removed:
            channel_context["session_hooks"] = copied
        return removed
    return 0


def remove_session_function_hook(
    context: ToolUseContext,
    event: HookEvent | str,
    hook_id: str,
) -> int:
    """Remove one session function hook by id."""

    return remove_session_hook(context, event, hook_id)


def clear_session_hooks(
    context: ToolUseContext,
    event: HookEvent | str | None = None,
) -> int:
    """Clear OpenSpace session hooks from a runtime context."""

    if event is None:
        count = sum(
            _count_session_hook_payloads(items)
            for items in context.session_hooks.values()
        )
        context.session_hooks.clear()
        return count
    hook_event = event if isinstance(event, HookEvent) else HookEvent(str(event))
    return _count_session_hook_payloads(
        context.session_hooks.pop(hook_event.value, [])
    )


def _append_session_hook_payload(
    context: ToolUseContext,
    event: HookEvent,
    matcher: str,
    hook: Mapping[str, Any],
) -> None:
    event_hooks = context.session_hooks.setdefault(event.value, [])
    matcher_value = str(matcher or "")
    for matcher_config in event_hooks:
        if not isinstance(matcher_config, dict):
            continue
        if str(matcher_config.get("matcher") or "") != matcher_value:
            continue
        hooks = matcher_config.setdefault("hooks", [])
        if isinstance(hooks, list):
            hooks.append(dict(hook))
            return
    event_hooks.append({"matcher": matcher_value, "hooks": [dict(hook)]})


def _session_hook_matches_remove_target(
    hook: Any,
    hook_or_id: Mapping[str, Any] | str,
) -> bool:
    if not isinstance(hook, Mapping):
        return False
    if isinstance(hook_or_id, str):
        return str(hook.get(_SESSION_HOOK_ID_KEY) or hook.get("id") or "") == hook_or_id
    return _session_hook_identity_equal(hook, hook_or_id)


def _session_hook_identity_equal(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    left_type = str(left.get("type") or _infer_hook_type(left))
    right_type = str(right.get("type") or _infer_hook_type(right))
    if left_type != right_type:
        return False
    if str(left.get("if") or "") != str(right.get("if") or ""):
        return False
    if left_type == "command":
        return (
            str(left.get("command") or "") == str(right.get("command") or "")
            and str(left.get("shell") or "bash") == str(right.get("shell") or "bash")
        )
    if left_type in {"prompt", "agent"}:
        return str(left.get("prompt") or "") == str(right.get("prompt") or "")
    if left_type == "http":
        return str(left.get("url") or "") == str(right.get("url") or "")
    if left_type == "function":
        return bool(left.get("id")) and left.get("id") == right.get("id")
    return False


def _count_session_hook_payloads(matchers: Any) -> int:
    if not isinstance(matchers, Sequence) or isinstance(
        matchers,
        (str, bytes, bytearray),
    ):
        return 0
    count = 0
    for matcher_config in matchers:
        if not isinstance(matcher_config, Mapping):
            continue
        hooks = matcher_config.get("hooks")
        if isinstance(hooks, Sequence) and not isinstance(
            hooks,
            (str, bytes, bytearray),
        ):
            count += len(hooks)
        elif _is_hook_payload(matcher_config):
            count += 1
    return count


def _register_configured_hook_dispatchers(registry: HookRegistry) -> None:
    if getattr(registry, "_openspace_configured_hook_dispatchers", False):
        return
    setattr(registry, "_openspace_configured_hook_dispatchers", True)
    for event in HookEvent:
        registry.register(
            event,
            _configured_hook_callback(event),
            priority=80,
            name=f"configured_hooks:{event.value}",
        )


def _configured_hook_callback(event: HookEvent) -> HookCallback:
    async def _callback(
        tool_name: str = "",
        tool_input: dict[str, Any] | None = None,
        tool_use_id: str = "",
        context: ToolUseContext | None = None,
        **kwargs: Any,
    ) -> HookResult:
        if context is None:
            return HookResult()
        return await _run_configured_hooks(
            event=event,
            tool_name=tool_name,
            tool_input=tool_input or {},
            tool_use_id=tool_use_id,
            context=context,
            kwargs=kwargs,
        )

    return _callback


async def _run_configured_hooks(
    *,
    event: HookEvent,
    tool_name: str,
    tool_input: Mapping[str, Any],
    tool_use_id: str,
    context: ToolUseContext,
    kwargs: Mapping[str, Any],
) -> HookResult:
    if tool_input.get("_skill_hook_command") is True:
        return HookResult()

    specs = _load_configured_hook_specs(event, context)
    if not specs:
        return HookResult()

    merged = HookResult()
    ran = False
    for spec in specs:
        if not _configured_hook_matches(
            spec,
            tool_name=tool_name,
            tool_input=tool_input,
            kwargs=kwargs,
        ):
            continue
        ran = True
        result = await _execute_configured_hook_spec(
            spec,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_use_id=tool_use_id,
            context=context,
            kwargs=kwargs,
        )
        if result.outcome == "success":
            await _handle_session_hook_success(spec, result, context)
        merged = _merge_configured_hook_result(merged, result)
        if merged.blocking_error or merged.permission_behavior == "deny":
            return merged
    return merged if ran else HookResult()


def _load_configured_hook_specs(
    event: HookEvent,
    context: ToolUseContext,
) -> list[ConfiguredHookSpec]:
    cwd = Path(getattr(context, "cwd", "") or os.getcwd()).expanduser()
    specs: list[ConfiguredHookSpec] = []
    specs.extend(_settings_hook_specs(event, cwd))
    specs.extend(_hooks_directory_specs(event, cwd))
    specs.extend(_session_hook_specs(event, context))
    specs.extend(_plugin_hook_specs(event, cwd))
    return sorted(specs, key=lambda spec: spec.priority)


def _settings_hook_specs(event: HookEvent, cwd: Path) -> list[ConfiguredHookSpec]:
    specs: list[ConfiguredHookSpec] = []
    try:
        from openspace.services.runtime_support.settings import (
            get_project_root,
            get_settings_with_errors,
        )

        snapshot = get_settings_with_errors(cwd)
        specs.extend(
            _normalise_hooks_config(
                snapshot.raw.get("hooks"),
                event=event,
                source="settings",
                root=snapshot.project_root,
                priority=100,
                session_scoped=False,
            )
        )
        project_root = get_project_root(cwd)
    except Exception:
        logger.debug("Failed to load OpenSpace settings hooks", exc_info=True)
        project_root = cwd

    return specs


def _hooks_directory_specs(event: HookEvent, cwd: Path) -> list[ConfiguredHookSpec]:
    try:
        from openspace.services.runtime_support.settings import get_project_root

        project_root = get_project_root(cwd)
    except Exception:
        project_root = cwd

    openspace_home = Path(
        os.environ.get("OPENSPACE_CONFIG_HOME") or Path.home() / ".openspace"
    )
    dirs = (
        (project_root / ".openspace" / "hooks", "openspace:projectHooks", 110),
        (openspace_home / "hooks", "openspace:userHooks", 112),
    )
    specs: list[ConfiguredHookSpec] = []
    for directory, source, priority in dirs:
        for path in _iter_hook_json_files(directory):
            data = _read_json_mapping(path)
            if data is None:
                continue
            config = (
                data.get("hooks")
                if isinstance(data.get("hooks"), Mapping)
                else data
            )
            specs.extend(
                _normalise_hooks_config(
                    config,
                    event=event,
                    source=f"{source}:{path.name}",
                    root=str(path.parent),
                    priority=priority,
                    session_scoped=False,
                )
            )
    return specs


def _session_hook_specs(
    event: HookEvent,
    context: ToolUseContext,
) -> list[ConfiguredHookSpec]:
    specs: list[ConfiguredHookSpec] = []
    session_hooks = getattr(context, "session_hooks", None)
    if isinstance(session_hooks, Mapping):
        specs.extend(
            _normalise_hooks_config(
                {event.value: session_hooks.get(event.value, [])},
                event=event,
                source="session",
                root=getattr(context, "cwd", None),
                priority=130,
                session_scoped=True,
            )
        )
    channel_hooks = getattr(context, "channel_context", {}).get("session_hooks")
    if isinstance(channel_hooks, Mapping):
        specs.extend(
            _normalise_hooks_config(
                {event.value: channel_hooks.get(event.value, [])},
                event=event,
                source="session:channel_context",
                root=getattr(context, "cwd", None),
                priority=131,
                session_scoped=True,
            )
        )
    return specs


def _plugin_hook_specs(event: HookEvent, cwd: Path) -> list[ConfiguredHookSpec]:
    specs: list[ConfiguredHookSpec] = []
    for config, plugin_root, plugin_name, label in _iter_plugin_hook_configs(cwd):
        specs.extend(
            _normalise_hooks_config(
                config,
                event=event,
                source=f"plugin:{plugin_name}:{label}",
                root=str(plugin_root),
                priority=120,
                session_scoped=False,
            )
        )
    return specs


def _normalise_hooks_config(
    config: Any,
    *,
    event: HookEvent,
    source: str,
    root: str | None,
    priority: int,
    session_scoped: bool,
) -> list[ConfiguredHookSpec]:
    if not isinstance(config, Mapping):
        return []
    raw_matchers = config.get(event.value)
    if raw_matchers is None:
        return []
    if isinstance(raw_matchers, Mapping):
        raw_matchers = [raw_matchers]
    if not isinstance(raw_matchers, Sequence) or isinstance(
        raw_matchers,
        (str, bytes, bytearray),
    ):
        return []

    specs: list[ConfiguredHookSpec] = []
    for matcher_config in raw_matchers:
        if not isinstance(matcher_config, Mapping):
            continue
        matcher = str(matcher_config.get("matcher") or "").strip()
        raw_hooks = matcher_config.get("hooks")
        if raw_hooks is None and _is_hook_payload(matcher_config):
            raw_hooks = [matcher_config]
        if isinstance(raw_hooks, Mapping):
            raw_hooks = [raw_hooks]
        if not isinstance(raw_hooks, Sequence) or isinstance(
            raw_hooks,
            (str, bytes, bytearray),
        ):
            continue
        for raw_hook in raw_hooks:
            if not isinstance(raw_hook, Mapping):
                continue
            hook = dict(raw_hook)
            hook.pop("matcher", None)
            hook.pop("hooks", None)
            if not _is_hook_payload(hook):
                continue
            hook.setdefault("type", _infer_hook_type(hook))
            specs.append(
                ConfiguredHookSpec(
                    event=event,
                    matcher=matcher,
                    hook=hook,
                    source=source,
                    root=root,
                    priority=priority,
                    session_scoped=session_scoped,
                    session_hook_id=(
                        str(hook.get(_SESSION_HOOK_ID_KEY) or hook.get("id") or "")
                        or None
                    ),
                    on_hook_success=(
                        hook.get(_SESSION_HOOK_SUCCESS_KEY)
                        if callable(hook.get(_SESSION_HOOK_SUCCESS_KEY))
                        else None
                    ),
                )
            )
    return specs


def _is_hook_payload(value: Mapping[str, Any]) -> bool:
    return any(key in value for key in ("command", "prompt", "url", "type"))


def _infer_hook_type(hook: Mapping[str, Any]) -> str:
    if hook.get("url"):
        return "http"
    if hook.get("prompt"):
        return "prompt"
    return "command"


def _configured_hook_matches(
    spec: ConfiguredHookSpec,
    *,
    tool_name: str,
    tool_input: Mapping[str, Any],
    kwargs: Mapping[str, Any],
) -> bool:
    try:
        from openspace.skill_engine.protocol import (
            _skill_hook_condition_matches,
            _skill_hook_matcher_matches,
        )

        if not _skill_hook_matcher_matches(
            spec.matcher,
            event=spec.event.value,
            tool_name=tool_name,
            tool_input=tool_input,
            kwargs=kwargs,
        ):
            return False
        condition = spec.hook.get("if")
        return not condition or _skill_hook_condition_matches(
            condition,
            tool_name,
            tool_input,
        )
    except Exception:
        logger.debug(
            "Configured hook matcher failed for %s",
            spec.source,
            exc_info=True,
        )
        return False


async def _execute_configured_hook_spec(
    spec: ConfiguredHookSpec,
    *,
    tool_name: str,
    tool_input: Mapping[str, Any],
    tool_use_id: str,
    context: ToolUseContext,
    kwargs: Mapping[str, Any],
) -> HookResult:
    hook_type = str(spec.hook.get("type") or "command")
    if hook_type == "command":
        return await _execute_configured_command_hook(
            spec,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_use_id=tool_use_id,
            context=context,
            kwargs=kwargs,
        )
    if hook_type == "function":
        return await _execute_configured_function_hook(
            spec,
            context=context,
        )

    meta = SimpleNamespace(
        name=spec.source,
        path=Path(spec.root or getattr(context, "cwd", ".") or ".") / "HOOKS",
        shell=spec.hook.get("shell"),
        model=None,
        allowed_tools=[],
        loaded_from="configured_hook",
    )
    try:
        if hook_type == "prompt":
            from openspace.skill_engine.protocol import _execute_skill_prompt_hook

            return await _execute_skill_prompt_hook(
                meta=meta,
                event=spec.event.value,
                hook=spec.hook,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_use_id=tool_use_id,
                context=context,
                kwargs=kwargs,
            )
        if hook_type == "http":
            from openspace.skill_engine.protocol import _execute_skill_http_hook

            return await _execute_skill_http_hook(
                meta=meta,
                event=spec.event.value,
                hook=spec.hook,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_use_id=tool_use_id,
                context=context,
                kwargs=kwargs,
            )
        if hook_type == "agent":
            from openspace.skill_engine.protocol import _execute_skill_agent_hook

            return await _execute_skill_agent_hook(
                meta=meta,
                event=spec.event.value,
                hook=spec.hook,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_use_id=tool_use_id,
                context=context,
                kwargs=kwargs,
            )
    except Exception as exc:
        return HookResult(
            blocking_error=HookBlockingError(
                f"Configured hook {spec.source} failed: {exc}",
                command=str(
                    spec.hook.get("command")
                    or spec.hook.get("prompt")
                    or spec.hook.get("url")
                    or ""
                ),
            ),
            outcome="blocking",
        )

    return HookResult(
        blocking_error=HookBlockingError(
            f"Configured hook {spec.source} uses unsupported hook type {hook_type!r}",
            command=str(spec.hook),
        ),
        outcome="blocking",
    )


async def _execute_configured_command_hook(
    spec: ConfiguredHookSpec,
    *,
    tool_name: str,
    tool_input: Mapping[str, Any],
    tool_use_id: str,
    context: ToolUseContext,
    kwargs: Mapping[str, Any],
) -> HookResult:
    from openspace.grounding.core.types import ToolStatus
    from openspace.services.conversation.content_blocks import extract_text_from_content
    from openspace.skill_engine.protocol import (
        _execute_skill_hook_command_via_runtime,
        _find_skill_prompt_shell_tool,
        _hook_result_from_ok_json,
        _looks_like_hook_json,
        _skill_hook_payload,
    )

    command = str(spec.hook.get("command") or "").strip()
    if not command:
        return HookResult()

    shell_tool = _find_skill_prompt_shell_tool(
        context,
        str(spec.hook.get("shell") or "bash").strip().lower() or None,
    )
    if shell_tool is None:
        return HookResult(
            blocking_error=HookBlockingError(
                "Configured command hook requires bash/powershell tool",
                command=command,
            ),
            outcome="blocking",
        )

    payload = _skill_hook_payload(
        event=spec.event.value,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_use_id=tool_use_id,
        context=context,
        kwargs=kwargs,
    )
    payload_json = json.dumps(payload, ensure_ascii=False)
    if "$ARGUMENTS" in command:
        rendered = command.replace("$ARGUMENTS", shlex.quote(payload_json))
    else:
        rendered = f"printf %s {shlex.quote(payload_json)} | {command}"
    if spec.root:
        rendered = (
            f"export OPENSPACE_PLUGIN_ROOT={shlex.quote(spec.root)}; "
            f"{rendered}"
        )

    result = await _execute_skill_hook_command_via_runtime(
        shell_tool=shell_tool,
        command=rendered,
        context=context,
        timeout=int(spec.hook.get("timeout") or 30),
        description=str(
            spec.hook.get("statusMessage")
            or f"Configured hook: {spec.source}"
        ),
    )
    output = extract_text_from_content(result.content).strip()
    metadata = getattr(result, "metadata", None) or {}
    try:
        exit_code = int(metadata.get("exit_code"))
    except (TypeError, ValueError):
        exit_code = None

    label = f"Configured command hook `{spec.source}` ({spec.event.value})"
    if result.status == ToolStatus.SUCCESS:
        if _looks_like_hook_json(output):
            return _hook_result_from_ok_json(
                output,
                hook_label=label,
                command=command,
                event=spec.event.value,
            )
        return HookResult()

    message = output or result.error or f"Hook command exited with code {exit_code}"
    if exit_code == 2:
        return HookResult(
            blocking_error=HookBlockingError(message, command=command),
            permission_behavior="deny" if spec.event == HookEvent.PRE_TOOL_USE else None,
            hook_permission_decision_reason=message,
            prevent_continuation=spec.event not in {
                HookEvent.PRE_TOOL_USE,
                HookEvent.PERMISSION_REQUEST,
            },
            stop_reason=message,
            outcome="blocking",
        )

    await context.emit_event(
        "hook_non_blocking_error",
        {
            "hook_event": spec.event.value,
            "hook_source": spec.source,
            "exit_code": exit_code,
            "message": message,
        },
    )
    return HookResult(outcome="non_blocking_error")


async def _execute_configured_function_hook(
    spec: ConfiguredHookSpec,
    *,
    context: ToolUseContext,
) -> HookResult:
    callback = spec.hook.get("callback")
    if not callable(callback):
        return HookResult(
            blocking_error=HookBlockingError(
                f"Configured function hook {spec.source} requires a callable callback",
                command="function",
            ),
            outcome="blocking",
        )

    abort_event = getattr(context, "abort_event", None)
    if abort_event is not None and abort_event.is_set():
        return HookResult(outcome="cancelled")

    messages = list(getattr(context, "messages", []) or [])
    timeout_seconds = _configured_function_hook_timeout(spec.hook)
    hook_name = f"{spec.source}:{spec.event.value}:function"
    try:
        call = _call_function_hook_callback(callback, messages, abort_event)
        passed = (
            await asyncio.wait_for(call, timeout=timeout_seconds)
            if timeout_seconds > 0
            else await call
        )
    except TimeoutError:
        return HookResult(outcome="cancelled")
    except Exception as exc:
        logger.debug("Function hook %s failed", hook_name, exc_info=True)
        return HookResult(
            message=_make_hook_error_message(hook_name, spec.event, str(exc)),
            outcome="non_blocking_error",
        )

    if passed:
        return HookResult(outcome="success")

    error_message = str(
        spec.hook.get("errorMessage")
        or spec.hook.get("error_message")
        or "Function hook blocked execution"
    )
    return HookResult(
        blocking_error=HookBlockingError(error_message, command="function"),
        permission_behavior="deny" if spec.event == HookEvent.PRE_TOOL_USE else None,
        hook_permission_decision_reason=error_message,
        prevent_continuation=spec.event not in {
            HookEvent.PRE_TOOL_USE,
            HookEvent.PERMISSION_REQUEST,
        },
        stop_reason=error_message,
        outcome="blocking",
    )


async def _call_function_hook_callback(
    callback: Callable[..., Any],
    messages: list[dict[str, Any]],
    abort_event: asyncio.Event | None,
) -> bool:
    args = _positional_args_for_callback(callback, messages, abort_event)
    result = callback(*args)
    if inspect.isawaitable(result):
        result = await result
    return bool(result)


def _configured_function_hook_timeout(hook: Mapping[str, Any]) -> float:
    raw = hook.get("timeout")
    if raw is None:
        return 5.0
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return 5.0
    return max(0.0, timeout)


async def _handle_session_hook_success(
    spec: ConfiguredHookSpec,
    result: HookResult,
    context: ToolUseContext,
) -> None:
    if not spec.session_scoped:
        return

    hook_payload = _public_session_hook_payload(spec.hook)
    aggregated = _aggregate_single(
        result,
        f"{spec.source}:{spec.event.value}",
        spec.event,
    )

    if spec.on_hook_success is not None:
        try:
            args = _positional_args_for_callback(
                spec.on_hook_success,
                hook_payload,
                aggregated,
            )
            maybe = spec.on_hook_success(*args)
            if inspect.isawaitable(maybe):
                await maybe
        except Exception:
            logger.debug("Session hook success callback failed", exc_info=True)

    if spec.hook.get("once") is True:
        target: Mapping[str, Any] | str = (
            spec.session_hook_id if spec.session_hook_id else hook_payload
        )
        if spec.source == "session:channel_context":
            _remove_channel_context_session_hook(
                context,
                spec.event,
                target,
                matcher=spec.matcher,
            )
        else:
            remove_session_hook(
                context,
                spec.event,
                target,
                matcher=spec.matcher,
            )


def _public_session_hook_payload(hook: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(hook)
    payload.pop(_SESSION_HOOK_ID_KEY, None)
    payload.pop(_SESSION_HOOK_SUCCESS_KEY, None)
    return payload


def _positional_args_for_callback(
    callback: Callable[..., Any],
    *args: Any,
) -> tuple[Any, ...]:
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return tuple(args)

    positional_count = 0
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            return tuple(args)
        if parameter.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            positional_count += 1
    return tuple(args[:positional_count])


def _merge_configured_hook_result(
    current: HookResult,
    incoming: HookResult,
) -> HookResult:
    if incoming.blocking_error or incoming.permission_behavior == "deny":
        return incoming

    permission_behavior = _stronger_permission_behavior(
        current.permission_behavior,
        incoming.permission_behavior,
    )
    updated_input = _merged_configured_updated_input(current, incoming)
    additional_context = "\n".join(
        part
        for part in (current.additional_context, incoming.additional_context)
        if part
    ) or None
    system_message = "\n".join(
        part
        for part in (current.system_message, incoming.system_message)
        if part
    ) or None
    return HookResult(
        message=incoming.message or current.message,
        system_message=system_message,
        prevent_continuation=(
            current.prevent_continuation or incoming.prevent_continuation
        ),
        stop_reason=incoming.stop_reason or current.stop_reason,
        permission_behavior=permission_behavior,
        hook_permission_decision_reason=(
            incoming.hook_permission_decision_reason
            or current.hook_permission_decision_reason
        ),
        additional_context=additional_context,
        initial_user_message=(
            incoming.initial_user_message or current.initial_user_message
        ),
        updated_input=updated_input,
        updated_permissions=incoming.updated_permissions or current.updated_permissions,
        updated_tool_output=(
            incoming.updated_tool_output
            if incoming.updated_tool_output is not None
            else current.updated_tool_output
        ),
        watch_paths=incoming.watch_paths or current.watch_paths,
        elicitation_response=incoming.elicitation_response or current.elicitation_response,
        elicitation_result_response=(
            incoming.elicitation_result_response
            or current.elicitation_result_response
        ),
        retry=current.retry or incoming.retry,
    )


def _stronger_permission_behavior(
    left: Literal["ask", "deny", "allow", "passthrough"] | None,
    right: Literal["ask", "deny", "allow", "passthrough"] | None,
) -> Literal["ask", "deny", "allow", "passthrough"] | None:
    rank = {None: 0, "passthrough": 1, "allow": 2, "ask": 3, "deny": 4}
    return right if rank.get(right, 0) > rank.get(left, 0) else left


def _configured_permission_rank(
    behavior: Literal["ask", "deny", "allow", "passthrough"] | None,
) -> int:
    return {None: 0, "passthrough": 1, "allow": 2, "ask": 3, "deny": 4}.get(
        behavior,
        0,
    )


def _merged_configured_updated_input(
    current: HookResult,
    incoming: HookResult,
) -> dict[str, Any] | None:
    current_rank = _configured_permission_rank(current.permission_behavior)
    incoming_rank = _configured_permission_rank(incoming.permission_behavior)

    if incoming_rank > current_rank:
        return incoming.updated_input
    if current_rank > incoming_rank:
        return current.updated_input
    return incoming.updated_input or current.updated_input


def _read_json_mapping(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("Failed to read hook config %s", path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def _iter_hook_json_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    paths: list[Path] = []
    primary = directory / "hooks.json"
    if primary.is_file():
        paths.append(primary)
    for path in sorted(directory.glob("*.json")):
        if path != primary:
            paths.append(path)
    return paths


def _iter_plugin_hook_configs(
    cwd: Path,
) -> list[tuple[Mapping[str, Any], Path, str, str]]:
    roots = _plugin_search_roots(cwd)
    seen_roots: set[Path] = set()
    found: list[tuple[Mapping[str, Any], Path, str, str]] = []
    for root in roots:
        if not root.exists():
            continue
        candidates = [root] if _looks_like_plugin_root(root) else []
        try:
            for manifest_path in root.rglob("plugin.json"):
                if len(candidates) > 200:
                    break
                candidates.append(manifest_path.parent)
            for hooks_path in root.rglob("hooks.json"):
                if len(candidates) > 200:
                    break
                plugin_root = hooks_path.parent.parent
                if _looks_like_plugin_root(plugin_root):
                    candidates.append(plugin_root)
        except OSError:
            continue
        for plugin_root in candidates:
            try:
                resolved_root = plugin_root.resolve()
            except OSError:
                resolved_root = plugin_root.absolute()
            if resolved_root in seen_roots:
                continue
            seen_roots.add(resolved_root)
            found.extend(_plugin_hook_configs_for_root(plugin_root))
    return found


def _plugin_hook_configs_for_root(
    plugin_root: Path,
) -> list[tuple[Mapping[str, Any], Path, str, str]]:
    manifest = _read_json_mapping(plugin_root / "plugin.json") or {}
    plugin_name = _plugin_name(plugin_root, manifest)
    loaded_files: set[Path] = set()
    configs: list[tuple[Mapping[str, Any], Path, str, str]] = []

    standard = plugin_root / "hooks" / "hooks.json"
    standard_config = _read_plugin_hooks_file(standard)
    if standard_config is not None:
        configs.append((standard_config, plugin_root, plugin_name, "hooks.json"))
        try:
            loaded_files.add(standard.resolve())
        except OSError:
            loaded_files.add(standard.absolute())

    hooks_spec = manifest.get("hooks")
    if hooks_spec is None:
        return configs
    hook_items = hooks_spec if isinstance(hooks_spec, list) else [hooks_spec]
    for index, hook_spec in enumerate(hook_items):
        if isinstance(hook_spec, str):
            hook_file = plugin_root / hook_spec
            try:
                key = hook_file.resolve()
            except OSError:
                key = hook_file.absolute()
            if key in loaded_files:
                continue
            config = _read_plugin_hooks_file(hook_file)
            if config is None:
                continue
            loaded_files.add(key)
            configs.append((config, plugin_root, plugin_name, hook_spec))
        elif isinstance(hook_spec, Mapping):
            configs.append(
                (
                    dict(hook_spec),
                    plugin_root,
                    plugin_name,
                    f"manifest:{index}",
                )
            )
    return configs


def _read_plugin_hooks_file(path: Path) -> Mapping[str, Any] | None:
    data = _read_json_mapping(path)
    if data is None:
        return None
    hooks = data.get("hooks")
    if isinstance(hooks, Mapping):
        return dict(hooks)
    return data


def _plugin_search_roots(cwd: Path) -> list[Path]:
    try:
        from openspace.services.runtime_support.settings import get_project_root

        project_root = get_project_root(cwd)
    except Exception:
        project_root = cwd
    roots: list[Path] = []
    raw = os.environ.get("OPENSPACE_PLUGIN_PATHS")
    if raw:
        roots.extend(
            Path(item).expanduser()
            for item in raw.split(os.pathsep)
            if item
        )
    roots.extend(
        [
            project_root / ".agents" / "plugins",
            project_root / ".openspace" / "plugins",
            Path.home() / ".openspace" / "plugins",
        ]
    )
    deduped: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            key = root.expanduser().resolve()
        except OSError:
            key = root.expanduser().absolute()
        if key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped


def _looks_like_plugin_root(path: Path) -> bool:
    return (path / "plugin.json").is_file() or (
        path / "hooks" / "hooks.json"
    ).is_file()


def _plugin_name(
    plugin_root: Path,
    manifest: Mapping[str, Any] | None = None,
) -> str:
    if manifest is None:
        manifest = _read_json_mapping(plugin_root / "plugin.json")
    if manifest is not None and manifest.get("name"):
        return str(manifest["name"])
    return plugin_root.name


def setup_default_hooks(
    registry: HookRegistry,
    *,
    include_configured_hooks: bool = True,
) -> None:
    """Register built-in hooks per DEC-020 §四/§五 and EXECUTION_CHECKLIST 3.1.

    Called during ``GroundingAgent.__init__``.
    All hooks get their runtime dependencies from ``ToolUseContext`` fields,
    so registration can happen before the context is constructed.

    Built-in hooks registered:
        (1) visual_analysis     — PostToolUse, priority=50   → DEC-022, GUI tools only
        (2) recording           — PostToolUse, priority=200  → records to recording_manager
        (3) quality_tracking    — PostToolUse, priority=210  → feeds ToolQualityManager
        (4) max_tokens_recovery — Stop,        priority=10   → detects model truncation
        (5) configured_hooks    — all events, priority=80    → settings/.openspace/session/plugin
    """
    if include_configured_hooks:
        _register_configured_hook_dispatchers(registry)

    # (1) Visual analysis — PostToolUse, GUI tools only, runs early
    registry.register_post_tool(
        create_visual_analysis_hook(),
        priority=50,
        name="visual_analysis",
    )

    # (2) Recording — PostToolUse, low priority (after business logic hooks)
    registry.register_post_tool(
        create_recording_hook(),
        priority=200,
        name="recording",
    )

    # (3) Quality tracking — PostToolUse, lowest priority
    registry.register_post_tool(
        create_quality_hook(),
        priority=210,
        name="quality_tracking",
    )

    # (4) Max tokens recovery — Stop hook
    registry.register_stop_hook(
        create_max_tokens_recovery_hook(),
        priority=10,
        name="max_tokens_recovery",
    )
