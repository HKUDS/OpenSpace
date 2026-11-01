"""Permission engine — main orchestrator for tool permission checks.

This module implements :func:`has_permissions_to_use_tool`, the single
entry point invoked by the tool execution pipeline before any tool runs.
It composes:

1. Tool-level deny rules
2. Tool-level ask rules
3. Tool-specific checks
4. ``requires_user_interaction`` / ``rule``-backed / ``safetyCheck``
   escape hatches (bypass-immune)
5. ``bypassPermissions`` / ``plan+bypass`` short-circuit
6. Tool-level allow rules
7. ``passthrough → ask`` fallback
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

from .types import (
    DecisionReasonAsyncAgent,
    DecisionReasonMode,
    DecisionReasonOther,
    DecisionReasonRule,
    PERMISSION_RULE_SOURCES,
    PermissionAllow,
    PermissionAsk,
    PermissionBehavior,
    PermissionDecision,
    PermissionDeny,
    PermissionPassthrough,
    PermissionResult,
    PermissionRule,
    PermissionRuleSource,
    ToolPermissionContext,
    ToolPermissionRulesBySource,
    format_rule_value,
    parse_rule_value,
)

if TYPE_CHECKING:
    from openspace.grounding.core.tool.base import BaseTool
    from openspace.services.tooling.context import ToolUseContext


logger = logging.getLogger(__name__)


def deny_missing_permission_context(tool_name: str = "tool") -> PermissionDeny:
    """Fail closed when a runtime caller did not provide permission context."""

    return PermissionDeny(
        message=(
            f"{tool_name} cannot run because the tool runtime is missing "
            "permission context."
        ),
        decision_reason=DecisionReasonOther(reason="missing permission context"),
    )


# ════════════════════════════════════════════════════════════════════════
# §1  Rule extraction helpers — OpenSpace permissions.ts L122-372 equivalents
# ════════════════════════════════════════════════════════════════════════


def _iter_rules(rules_by_source: ToolPermissionRulesBySource) -> Iterable[tuple[PermissionRuleSource, str]]:
    """Iterate ``(source, raw_rule_string)`` pairs in OpenSpace's source order.

    Order matters for display — first-matching-wins uses this iteration.
    """
    for source in PERMISSION_RULE_SOURCES:
        for raw in rules_by_source.get(source, ()) or ():
            yield source, raw


def _build_rule(
    source: PermissionRuleSource,
    behavior: PermissionBehavior,
    raw: str,
) -> Optional[PermissionRule]:
    """Safely parse a raw rule string into a :class:`PermissionRule`."""
    try:
        value = parse_rule_value(raw)
    except ValueError:
        logger.warning("Dropping malformed permission rule %r from %s", raw, source)
        return None
    return PermissionRule(source=source, rule_behavior=behavior, rule_value=value)


def get_allow_rules(context: ToolPermissionContext) -> List[PermissionRule]:
    """OpenSpace :func:`getAllowRules` (L122-132)."""
    out: List[PermissionRule] = []
    for source, raw in _iter_rules(context.always_allow_rules):
        rule = _build_rule(source, "allow", raw)
        if rule is not None:
            out.append(rule)
    return out


def get_deny_rules(context: ToolPermissionContext) -> List[PermissionRule]:
    """OpenSpace :func:`getDenyRules` (L213)."""
    out: List[PermissionRule] = []
    for source, raw in _iter_rules(context.always_deny_rules):
        rule = _build_rule(source, "deny", raw)
        if rule is not None:
            out.append(rule)
    return out


def get_ask_rules(context: ToolPermissionContext) -> List[PermissionRule]:
    """OpenSpace :func:`getAskRules` (L223)."""
    out: List[PermissionRule] = []
    for source, raw in _iter_rules(context.always_ask_rules):
        rule = _build_rule(source, "ask", raw)
        if rule is not None:
            out.append(rule)
    return out


def _tool_level_rule(
    rules_by_source: ToolPermissionRulesBySource,
    behavior: PermissionBehavior,
    tool_name: str,
) -> Optional[PermissionRule]:
    """Find a **tool-level** rule (``rule_content is None``) matching ``tool_name``.

    Implementation: the inner loop of :func:`getDenyRuleForTool` /
    :func:`getAskRuleForTool` / :func:`toolAlwaysAllowedRule`.
    """
    for source, raw in _iter_rules(rules_by_source):
        rule = _build_rule(source, behavior, raw)
        if rule is None:
            continue
        v = rule.rule_value
        if v.tool_name == tool_name and v.rule_content is None:
            return rule
    return None


def get_deny_rule_for_tool(
    context: ToolPermissionContext, tool: "BaseTool"
) -> Optional[PermissionRule]:
    """OpenSpace :func:`getDenyRuleForTool` (L287)."""
    return _tool_level_rule(context.always_deny_rules, "deny", tool.name)


def get_ask_rule_for_tool(
    context: ToolPermissionContext, tool: "BaseTool"
) -> Optional[PermissionRule]:
    """Return the tool-level always-ask rule for *tool*, if configured."""
    return _tool_level_rule(context.always_ask_rules, "ask", tool.name)


def tool_always_allowed_rule(
    context: ToolPermissionContext, tool: "BaseTool"
) -> Optional[PermissionRule]:
    """Return the tool-level always-allow rule for *tool*, if configured."""
    return _tool_level_rule(context.always_allow_rules, "allow", tool.name)


def _tool_level_rule_for_name(
    rules_by_source: ToolPermissionRulesBySource,
    behavior: PermissionBehavior,
    tool_name: str,
) -> Optional[PermissionRule]:
    """Same as :func:`_tool_level_rule` but keyed by ``tool_name`` string.

    Used by :func:`check_rule_based_permissions` below where the tool
    object may not be available (e.g. from a spawned subagent).
    """
    return _tool_level_rule(rules_by_source, behavior, tool_name)


# ════════════════════════════════════════════════════════════════════════
# §2  Permission request message builder
# ════════════════════════════════════════════════════════════════════════


def create_permission_request_message(
    tool_name: str, decision_reason: Optional[Any] = None
) -> str:
    """Build the concise permission prompt title.

    Rendered into the TUI ask-dialog title.
    """
    if decision_reason is None:
        return f"Requesting permission to use {tool_name}"

    t = getattr(decision_reason, "type", None)
    if t == "hook":
        hook_name = getattr(decision_reason, "hook_name", "hook")
        reason = getattr(decision_reason, "reason", None)
        if reason:
            return f"Hook {hook_name!r} requires approval for {tool_name}: {reason}"
        return f"Hook {hook_name!r} requires approval for {tool_name}"
    if t == "rule":
        rule: PermissionRule = decision_reason.rule
        v = format_rule_value(rule.rule_value)
        return f"Rule {v} ({rule.rule_behavior}, from {rule.source}) requires approval for {tool_name}"
    if t == "mode":
        return f"Mode {decision_reason.mode!r} requires approval for {tool_name}"
    if t == "safetyCheck":
        return f"Safety check requires approval for {tool_name}: {decision_reason.reason}"
    if t == "workingDir":
        return f"Working-directory check requires approval for {tool_name}: {decision_reason.reason}"
    if t == "subcommandResults":
        return f"Compound command requires approval for {tool_name}"
    if t == "other":
        return f"Requesting permission to use {tool_name}: {decision_reason.reason}"
    return f"Requesting permission to use {tool_name}"


# ════════════════════════════════════════════════════════════════════════
# §3  Main entry — hasPermissionsToUseToolInner
# ════════════════════════════════════════════════════════════════════════


def _get_updated_input_or_fallback(
    result: PermissionResult, input_: Dict[str, Any]
) -> Dict[str, Any]:
    """OpenSpace :func:`getUpdatedInputOrFallback` (L1018).

    Returns ``result.updated_input`` if present, else the original input.
    """
    if isinstance(result, (PermissionAllow, PermissionAsk)):
        if result.updated_input is not None:
            return result.updated_input
    return input_


def _finalize_ask_decision(
    result: PermissionAsk,
    *,
    tool: "BaseTool",
    permission_context: ToolPermissionContext,
    runtime_context: "ToolUseContext",
) -> PermissionDecision:
    """Convert ask decisions that cannot be shown to terminal deny decisions."""

    if permission_context.mode == "dontAsk":
        return PermissionDeny(
            message=(
                f"{tool.name} would require user approval, but permission mode "
                "dontAsk forbids prompting."
            ),
            decision_reason=DecisionReasonMode(mode=permission_context.mode),
        )
    if runtime_context.is_async_agent:
        return PermissionDeny(
            message=(
                "Async agents cannot prompt for user permission. "
                f"The parent task must authorize {tool.name} explicitly."
            ),
            decision_reason=DecisionReasonAsyncAgent(
                reason=f"{tool.name} requires user approval"
            ),
        )
    if (
        permission_context.should_avoid_permission_prompts
        or not runtime_context.tui_available
    ):
        return PermissionDeny(
            message=(
                f"{tool.name} would require user approval, but no interactive "
                "TUI is available (headless mode). Configure an allow rule in "
                ".openspace/settings.local.json or run with --permission-mode "
                "acceptEdits/bypassPermissions as appropriate."
            ),
            decision_reason=DecisionReasonOther(reason="headless"),
        )
    return result


def _should_bypass_permissions(context: ToolPermissionContext) -> bool:
    """Return True when permission prompts should be bypassed."""
    if context.mode == "bypassPermissions":
        return True
    if context.mode == "plan" and context.is_bypass_permissions_mode_available:
        return True
    return False


def _validate_input_or_passthrough(
    tool: "BaseTool", input_: Dict[str, Any]
) -> Optional[str]:
    """Best-effort JSON-schema validation — OpenSpace ``tool.inputSchema.parse``.

    Returns ``None`` on success, or an error string (for logging) on
    failure.  Failures do not abort the permission check; OpenSpace logs and
    continues with a ``passthrough`` result.
    """
    try:
        tool.validate_parameters(input_)
    except Exception as e:  # pragma: no cover — schema layer errors
        return str(e)
    return None


async def has_permissions_to_use_tool(
    tool: "BaseTool",
    input_: Dict[str, Any],
    context: "ToolUseContext",
) -> PermissionDecision:
    """OpenSpace :func:`hasPermissionsToUseTool` → :func:`hasPermissionsToUseToolInner`
    (permissions.ts L473-1319).

    Main pipeline (OpenSpace-aligned — each numbered step mirrors OpenSpace comments):

    1a. Tool-level deny rule    → deny
    1b. Tool-level ask rule     → ask  (Bash sandbox auto-allow may override)
    1c. Run ``tool.check_permissions(input, context)``
    1d. Tool said deny          → deny
    1e. ``requires_user_interaction`` + ask → ask  (bypass-immune)
    1f. Tool said ask with rule-based reason → ask  (bypass-immune)
    1g. Tool said ask with safetyCheck reason → ask (bypass-immune)
    2a. Mode bypass active      → allow (mode reason)
    2b. Tool-level allow rule   → allow (rule reason)
    3.  Passthrough              → ask   (final fallback)

    Async subagent / headless handling:
      - When ``context.is_async_agent`` and the final decision is ``ask``,
        OpenSpace converts it to ``deny`` with ``DecisionReasonAsyncAgent`` —
        OpenSpace does the same because async subagents cannot prompt users.
      - When ``context.tui_available`` is ``False`` and the context's
        ``should_avoid_permission_prompts`` is ``True`` (headless CI), OpenSpace
        converts ``ask`` to ``deny`` with ``DecisionReasonOther("headless")``.
    """

    # Abort check.
    if context.is_aborted():
        raise asyncio.CancelledError("Tool permission check aborted")

    perm_ctx = context.permission_context
    if perm_ctx is None:
        logger.warning(
            "has_permissions_to_use_tool: context.permission_context is None; "
            "denying %s",
            tool.name,
        )
        return deny_missing_permission_context(tool.name)

    if perm_ctx.mode == "plan":
        try:
            is_read_only = bool(tool.is_read_only(input_))
        except Exception:
            is_read_only = False
        if not is_read_only and tool.name != "ExitPlanMode":
            from openspace.services.runtime_support.plan_mode import is_active_plan_file

            file_path = (
                input_.get("file_path")
                or input_.get("path")
                or input_.get("notebook_path")
            )
            if not (
                tool.name in {"edit", "write", "notebook_edit"}
                and isinstance(file_path, str)
                and is_active_plan_file(file_path)
            ):
                return PermissionDeny(
                    message=(
                        f"Cannot use {tool.name} in plan mode. Plan mode only "
                        "allows read-only tools, the plan file edit/write tools, "
                        "and ExitPlanMode."
                    ),
                    decision_reason=DecisionReasonMode(mode=perm_ctx.mode),
                )

    # --- 1a. Tool-level deny rule ---------------------------------------
    deny_rule = get_deny_rule_for_tool(perm_ctx, tool)
    if deny_rule is not None:
        return PermissionDeny(
            message=f"Permission to use {tool.name} has been denied.",
            decision_reason=DecisionReasonRule(rule=deny_rule),
        )

    # --- 1b. Tool-level ask rule ----------------------------------------
    # OpenSpace lets Bash sandbox auto-allow bypass a tool-level ask rule only when
    # the command will actually run inside the sandbox.
    ask_rule = get_ask_rule_for_tool(perm_ctx, tool)
    if ask_rule is not None:
        if tool.name == "bash":
            command = input_.get("command")
            if isinstance(command, str):
                from openspace.grounding.backends.shell.transport.local_connector import (
                    LocalShellConnector,
                )
                from openspace.grounding.core.permissions.bash_permissions import (
                    check_sandbox_auto_allow,
                )

                connector = getattr(tool, "_session", None)
                connector = getattr(connector, "connector", None)
                sandbox_result = check_sandbox_auto_allow(
                    command,
                    getattr(context, "cwd", "."),
                    perm_ctx,
                    tool.name,
                    "local" if isinstance(connector, LocalShellConnector) else "remote",
                    dangerously_disable_sandbox=bool(
                        input_.get("dangerously_disable_sandbox", False)
                    ),
                )
                if isinstance(sandbox_result, PermissionAllow):
                    return sandbox_result
        return _finalize_ask_decision(
            PermissionAsk(
                message=create_permission_request_message(tool.name),
                decision_reason=DecisionReasonRule(rule=ask_rule),
            ),
            tool=tool,
            permission_context=perm_ctx,
            runtime_context=context,
        )

    # --- 1c. Tool implementation's own check ----------------------------
    tool_result: PermissionResult
    _validate_input_or_passthrough(tool, input_)
    try:
        tool_result = await tool.check_permissions(input_, context)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # pragma: no cover — tool error path
        logger.exception("tool.check_permissions raised for %s: %s", tool.name, e)
        tool_result = PermissionPassthrough(
            message=create_permission_request_message(tool.name),
        )

    # --- 1d. Tool denied ------------------------------------------------
    if isinstance(tool_result, PermissionDeny):
        return tool_result

    # --- 1e. requires_user_interaction + ask → bypass-immune -----------
    if isinstance(tool_result, PermissionAsk) and tool.requires_user_interaction:
        return _finalize_ask_decision(
            tool_result,
            tool=tool,
            permission_context=perm_ctx,
            runtime_context=context,
        )

    # --- 1f. rule-based ask → bypass-immune ----------------------------
    if isinstance(tool_result, PermissionAsk):
        reason = tool_result.decision_reason
        if (
            reason is not None
            and getattr(reason, "type", None) == "rule"
            and getattr(reason.rule, "rule_behavior", None) == "ask"
        ):
            return _finalize_ask_decision(
                tool_result,
                tool=tool,
                permission_context=perm_ctx,
                runtime_context=context,
            )

    # --- 1g. safetyCheck ask → bypass-immune ---------------------------
    if isinstance(tool_result, PermissionAsk):
        reason = tool_result.decision_reason
        if reason is not None and getattr(reason, "type", None) == "safetyCheck":
            return _finalize_ask_decision(
                tool_result,
                tool=tool,
                permission_context=perm_ctx,
                runtime_context=context,
            )

    # --- 2a. Mode bypass -----------------------------------------------
    if _should_bypass_permissions(perm_ctx):
        return PermissionAllow(
            updated_input=_get_updated_input_or_fallback(tool_result, input_),
            decision_reason=DecisionReasonMode(mode=perm_ctx.mode),
        )

    # --- 2b. Tool-level allow rule -------------------------------------
    allow_rule = tool_always_allowed_rule(perm_ctx, tool)
    if allow_rule is not None:
        return PermissionAllow(
            updated_input=_get_updated_input_or_fallback(tool_result, input_),
            decision_reason=DecisionReasonRule(rule=allow_rule),
        )

    # --- 3. Passthrough → ask -------------------------------------------
    if isinstance(tool_result, PermissionPassthrough):
        result: PermissionDecision = PermissionAsk(
            message=create_permission_request_message(
                tool.name, tool_result.decision_reason
            ),
            decision_reason=tool_result.decision_reason,
            suggestions=tool_result.suggestions,
            blocked_path=tool_result.blocked_path,
            pending_classifier_check=tool_result.pending_classifier_check,
        )
    elif isinstance(tool_result, (PermissionAllow, PermissionAsk)):
        result = tool_result
    else:  # pragma: no cover — exhaustive
        result = PermissionAsk(
            message=create_permission_request_message(tool.name),
            decision_reason=DecisionReasonOther(
                reason=f"Unexpected permission result type: {type(tool_result).__name__}"
            ),
        )

    # --- Post-processing: prompts unavailable by mode/runtime ------------
    if isinstance(result, PermissionAsk):
        return _finalize_ask_decision(
            result,
            tool=tool,
            permission_context=perm_ctx,
            runtime_context=context,
        )

    return result


# ════════════════════════════════════════════════════════════════════════
# §4  Rule-based only entry
# ════════════════════════════════════════════════════════════════════════


async def check_rule_based_permissions(
    tool_name: str,
    input_: Dict[str, Any],
    context: ToolPermissionContext,
) -> PermissionResult:
    """Evaluate static permission rules and mode without running tool logic.

    This variant of :func:`has_permissions_to_use_tool` does not run the tool's
    own ``check_permissions`` and does not run any classifier.

    Used by the plan-mode tool filter, async subagent pre-flight checks,
    and the ``list_rules`` slash command.
    """
    # deny rule
    deny = _tool_level_rule_for_name(context.always_deny_rules, "deny", tool_name)
    if deny is not None:
        return PermissionDeny(
            message=f"Permission to use {tool_name} has been denied.",
            decision_reason=DecisionReasonRule(rule=deny),
        )
    # ask rule
    ask = _tool_level_rule_for_name(context.always_ask_rules, "ask", tool_name)
    if ask is not None:
        return PermissionAsk(
            message=create_permission_request_message(tool_name),
            decision_reason=DecisionReasonRule(rule=ask),
        )
    # mode bypass
    if _should_bypass_permissions(context):
        return PermissionAllow(
            updated_input=input_, decision_reason=DecisionReasonMode(mode=context.mode)
        )
    # allow rule
    allow = _tool_level_rule_for_name(context.always_allow_rules, "allow", tool_name)
    if allow is not None:
        return PermissionAllow(
            updated_input=input_, decision_reason=DecisionReasonRule(rule=allow)
        )
    # Fallthrough — no match, report passthrough (caller decides).
    return PermissionPassthrough(
        message=create_permission_request_message(tool_name),
    )


# ════════════════════════════════════════════════════════════════════════
# §5  Context application helpers
# ════════════════════════════════════════════════════════════════════════


def apply_permission_rules_to_permission_context(
    context: ToolPermissionContext,
    rules: Iterable[PermissionRule],
) -> ToolPermissionContext:
    """Apply permission rules to a copied permission context.

    Merge fresh rules into the existing context, grouped by behavior and
    source.  Returns a new immutable context.
    """
    allow: Dict[PermissionRuleSource, list[str]] = {
        s: list(v) for s, v in context.always_allow_rules.items()
    }
    deny: Dict[PermissionRuleSource, list[str]] = {
        s: list(v) for s, v in context.always_deny_rules.items()
    }
    ask: Dict[PermissionRuleSource, list[str]] = {
        s: list(v) for s, v in context.always_ask_rules.items()
    }

    for rule in rules:
        target = {"allow": allow, "deny": deny, "ask": ask}[rule.rule_behavior]
        bucket = target.setdefault(rule.source, [])
        raw = format_rule_value(rule.rule_value)
        if raw not in bucket:
            bucket.append(raw)

    return ToolPermissionContext(
        mode=context.mode,
        additional_working_directories=context.additional_working_directories,
        always_allow_rules={k: tuple(v) for k, v in allow.items()},
        always_deny_rules={k: tuple(v) for k, v in deny.items()},
        always_ask_rules={k: tuple(v) for k, v in ask.items()},
        is_bypass_permissions_mode_available=context.is_bypass_permissions_mode_available,
        stripped_dangerous_rules=context.stripped_dangerous_rules,
        should_avoid_permission_prompts=context.should_avoid_permission_prompts,
        await_automated_checks_before_dialog=context.await_automated_checks_before_dialog,
        pre_plan_mode=context.pre_plan_mode,
    )


# Needed for abort propagation above.  Imported late to avoid top-level
# circular issues on some Python versions.
import asyncio  # noqa: E402


__all__ = [
    "has_permissions_to_use_tool",
    "check_rule_based_permissions",
    "create_permission_request_message",
    "get_allow_rules",
    "get_deny_rules",
    "get_ask_rules",
    "get_deny_rule_for_tool",
    "get_ask_rule_for_tool",
    "tool_always_allowed_rule",
    "apply_permission_rules_to_permission_context",
]
