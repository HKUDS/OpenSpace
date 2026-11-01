"""BashTool compound-command helpers.

The helpers in this module decompose a compound bash command into
pipe-separated segments and re-run the full permission pipeline on each
segment. They are called from :func:`bash_permissions.bash_tool_has_permission`
after the exact/prefix rule matchers have been exhausted.

Runtime boundaries:

- OpenSpace does not require tree-sitter for compound command checks; unsafe
  operators are detected with parser output plus conservative regex guards.
- The public helpers remain async-compatible because the surrounding
  permission pipeline awaits them, although the local work is synchronous.
"""
from __future__ import annotations

import re
from typing import Awaitable, Callable, List, Optional, Union

from ..security.bash_injection import bash_command_passes_injection_gate
from ..security.shell_parser import (
    extract_output_redirections,
    split_command_segments,
)
from .types import (
    DecisionReasonOther,
    DecisionReasonSubcommandResults,
    PermissionAllow,
    PermissionAsk,
    PermissionDeny,
    PermissionPassthrough,
    PermissionResult,
    PermissionUpdate,
    ToolPermissionContext,
)


__all__ = [
    "CommandIdentityCheckers",
    "check_command_operator_permissions",
    "segmented_command_permission_result",
    "build_segment_without_redirections",
]


class CommandIdentityCheckers:
    """Command identity callbacks used during compound-command checks."""

    __slots__ = ("is_normalized_cd_command", "is_normalized_git_command")

    def __init__(
        self,
        is_normalized_cd_command: Callable[[str], bool],
        is_normalized_git_command: Callable[[str], bool],
    ) -> None:
        self.is_normalized_cd_command = is_normalized_cd_command
        self.is_normalized_git_command = is_normalized_git_command


# Callable signature: ``(command, context) -> PermissionResult``.
# Matches the recursion entry point that the caller injects into
# :func:`check_command_operator_permissions`.
RecursePermissionFn = Callable[
    [str, str, Optional[str], ToolPermissionContext],
    "Union[PermissionResult, Awaitable[PermissionResult]]",
]


# ════════════════════════════════════════════════════════════════════════
#  build_segment_without_redirections
# ════════════════════════════════════════════════════════════════════════


def build_segment_without_redirections(segment_command: str) -> str:
    """Return *segment_command* without output redirection suffixes.

    Fast path: if the segment has no ``>`` operator, return it as-is.
    Otherwise delegate to :func:`extract_output_redirections` and return
    its ``command_without_redirections`` field.
    """
    if ">" not in segment_command:
        return segment_command

    result = extract_output_redirections(segment_command)
    return result.command_without_redirections or segment_command


# ════════════════════════════════════════════════════════════════════════
#  segmented_command_permission_result
# ════════════════════════════════════════════════════════════════════════


def _fmt_bash_prompt_message() -> str:
    """Return the plain permission prompt message used for bash asks."""
    return "OpenSpace needs permission to run a bash command"


async def segmented_command_permission_result(
    command: str,
    segments: List[str],
    recurse: RecursePermissionFn,
    checkers: CommandIdentityCheckers,
    context: ToolPermissionContext,
    cwd: str,
    description: Optional[str] = None,
) -> PermissionResult:
    """Merge per-segment permission results for a compound command.

    Evaluates each pipe-separated segment via *recurse*, then merges the
    per-segment decisions using the OpenSpace precedence rules:

        deny > ask > allow > passthrough

    If any segment denies → return :class:`PermissionDeny`.
    Else if all segments allow → :class:`PermissionAllow`.
    Else → :class:`PermissionAsk` with de-duplicated suggestions.

    Additional guards before per-segment evaluation:

    - Multiple ``cd`` across segments → ask.
    - ``cd`` + ``git`` across different segments → ask to
      prevent bare-repo fsmonitor bypass.
    """
    # §Multiple-cd guard.
    cd_count = sum(
        1
        for seg in segments
        if checkers.is_normalized_cd_command(seg.strip())
    )
    if cd_count > 1:
        reason = (
            "Multiple directory changes in one command "
            "require approval for clarity"
        )
        return PermissionAsk(
            message=reason,
            decision_reason=DecisionReasonOther(reason=reason),
        )

    # §cd+git cross-segment guard.
    has_cd = False
    has_git = False
    for seg in segments:
        for sub in split_command_segments(seg):
            trimmed = sub.strip()
            if checkers.is_normalized_cd_command(trimmed):
                has_cd = True
            if checkers.is_normalized_git_command(trimmed):
                has_git = True
    if has_cd and has_git:
        reason = (
            "Compound commands with cd and git require approval "
            "to prevent bare repository attacks"
        )
        return PermissionAsk(
            message=reason,
            decision_reason=DecisionReasonOther(reason=reason),
        )

    # §Per-segment evaluation.
    segment_results: dict[str, PermissionResult] = {}
    for seg in segments:
        trimmed = seg.strip()
        if not trimmed:
            continue
        res = recurse(trimmed, cwd, description, context)
        if hasattr(res, "__await__"):
            res = await res  # type: ignore[assignment,misc]
        segment_results[trimmed] = res  # type: ignore[assignment]

    # §Deny takes priority.
    denied_entry = next(
        (
            (k, v)
            for k, v in segment_results.items()
            if isinstance(v, PermissionDeny)
        ),
        None,
    )
    if denied_entry is not None:
        seg_cmd, seg_result = denied_entry
        message = (
            seg_result.message
            if isinstance(seg_result, PermissionDeny)
            else f"Permission denied for: {seg_cmd}"
        )
        return PermissionDeny(
            message=message,
            decision_reason=DecisionReasonSubcommandResults(
                reasons=dict(segment_results)
            ),
        )

    # §All-allowed.
    if segment_results and all(
        isinstance(v, PermissionAllow) for v in segment_results.values()
    ):
        return PermissionAllow(
            decision_reason=DecisionReasonSubcommandResults(
                reasons=dict(segment_results)
            ),
        )

    # §Collect suggestions from non-allow segments.
    suggestions: List[PermissionUpdate] = []
    for _, result in segment_results.items():
        if isinstance(result, PermissionAllow):
            continue
        seg_suggestions = getattr(result, "suggestions", None)
        if seg_suggestions:
            suggestions.extend(seg_suggestions)

    return PermissionAsk(
        message=_fmt_bash_prompt_message(),
        decision_reason=DecisionReasonSubcommandResults(
            reasons=dict(segment_results)
        ),
        suggestions=tuple(suggestions) if suggestions else None,
    )


# ════════════════════════════════════════════════════════════════════════
# §3  _is_unsafe_compound_command — OpenSpace utils/bash/commands.ts
# ════════════════════════════════════════════════════════════════════════


# OpenSpace ``isUnsafeCompoundCommand_DEPRECATED`` — detects subshell ``(cmd)``
# and command-group ``{ cmd; }`` structures that can't be pipe-split
# safely. OS uses a regex heuristic because we have no tree-sitter AST
# (per runtime constraints).
_UNSAFE_COMPOUND_PATTERNS = (
    # Subshell: unescaped '(' at the start or after an operator,
    # excluding `$(` (command substitution — checked elsewhere).
    re.compile(r"(?<!\$)\((?!\s*\))"),
    # Brace group: ``{ cmd; }`` with separation whitespace.
    re.compile(r"\{\s+[^}]+;\s*\}"),
)


def _is_unsafe_compound_command(command: str) -> bool:
    """OpenSpace ``isUnsafeCompoundCommand_DEPRECATED`` (utils/bash/commands.ts)."""
    for pattern in _UNSAFE_COMPOUND_PATTERNS:
        if pattern.search(command):
            return True
    return False


def _get_pipe_segments(command: str) -> List[str]:
    """Return command segments using the shared quote-aware shell parser."""
    return split_command_segments(command)


# ════════════════════════════════════════════════════════════════════════
# §4  check_command_operator_permissions
# ════════════════════════════════════════════════════════════════════════


async def check_command_operator_permissions(
    command: str,
    recurse: RecursePermissionFn,
    checkers: CommandIdentityCheckers,
    context: ToolPermissionContext,
    cwd: str,
    description: Optional[str] = None,
) -> PermissionResult:
    """OpenSpace ``checkCommandOperatorPermissions`` (L181-202) +
    ``bashToolCheckCommandOperatorPermissions`` (L208-265).

    Decision tree:

    1. Unsafe compound structure (subshell / brace group) → ask.
    2. Single-segment pipeline (or no pipes) → :class:`PermissionPassthrough`
       so the caller's normal flow continues.
    3. Multi-segment pipeline → strip redirections per segment and hand
       off to :func:`segmented_command_permission_result`.
    """
    # §1. Unsafe compound.
    if _is_unsafe_compound_command(command):
        safety = bash_command_passes_injection_gate(command)
        reason = (
            safety.get("message")
            if safety.get("behavior") != "allow"
            else "This command uses shell operators that require approval for safety"
        )
        return PermissionAsk(
            message=reason or "",
            decision_reason=DecisionReasonOther(reason=reason or ""),
        )

    # §2. Single segment → passthrough.
    pipe_segments = _get_pipe_segments(command)
    if len(pipe_segments) <= 1:
        return PermissionPassthrough(
            message="No pipes found in command"
        )

    # §3. Multi-segment → strip redirections then segment.
    segments = [
        build_segment_without_redirections(seg) for seg in pipe_segments
    ]
    return await segmented_command_permission_result(
        command=command,
        segments=segments,
        recurse=recurse,
        checkers=checkers,
        context=context,
        cwd=cwd,
        description=description,
    )
