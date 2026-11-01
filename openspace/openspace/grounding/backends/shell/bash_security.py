"""Bash command safety heuristics for ``BashTool``.

This module contains cheap, synchronous checks that are useful before or after
a shell command runs: blocked sleep detection, destructive-command warnings,
exit-code interpretation for read-only tools, and auto-backgrounding policy.
The heavier read-only classifier and permission engine live in the core
security and permissions packages.
"""

from __future__ import annotations

import re
from typing import Callable

from openspace.grounding.core.security.shell_parser import (
    split_command_segments as _split_command_segments,
)

__all__ = [
    "split_command_segments",
    "detect_blocked_sleep_pattern",
    "get_destructive_command_warning",
    "interpret_command_result",
    "is_autobackgrounding_allowed",
    "DISALLOWED_AUTO_BACKGROUND_COMMANDS",
]


# ─────────────────────────────────────────────────────────────────────
#  Command splitting wrapper.
#
#  The parser lives in core so permissions, sandbox decisions, backgrounding
#  heuristics, and result interpretation all see the same subcommand list.
# ─────────────────────────────────────────────────────────────────────


def split_command_segments(command: str) -> list[str]:
    """Return subcommand fragments for *command*.

    Delegates to the core shell parser so backend safety helpers and core
    permission checks cannot diverge on quoted control characters.
    """
    return _split_command_segments(command)


# ─────────────────────────────────────────────────────────────────────
#  Blocked sleep detection
# ─────────────────────────────────────────────────────────────────────

_BLOCKED_SLEEP_RE = re.compile(r"^sleep\s+(\d+)\s*$")


def detect_blocked_sleep_pattern(command: str) -> str | None:
    """Detect leading ``sleep N`` where ``N >= 2`` seconds.

    Only the *first* subcommand is inspected (``sleep 5 && check`` matches;
    ``check && sleep 5`` does not).  Float durations (``sleep 0.5``) and
    ``sleep`` inside pipelines/subshells are allowed — those are legitimate
    pacing, not polls.

    Returns the problematic fragment (for the error message) or ``None``.
    """
    parts = split_command_segments(command)
    if not parts:
        return None
    first = parts[0].strip()
    m = _BLOCKED_SLEEP_RE.match(first)
    if not m:
        return None
    secs = int(m.group(1))
    if secs < 2:
        return None
    rest = " ".join(parts[1:]).strip()
    return f"sleep {secs} followed by: {rest}" if rest else f"standalone sleep {secs}"


# ─────────────────────────────────────────────────────────────────────
#  Destructive command warnings
# ─────────────────────────────────────────────────────────────────────

# TypeScript regex flags used in Implementation: default (case sensitive, single-line
# unless explicitly ``i``).  We preserve that by using Python ``re`` with
# the same flag set.  The ``\b`` word boundaries behave identically.
_DESTRUCTIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Git — data loss / hard to reverse
    (
        re.compile(r"\bgit\s+reset\s+--hard\b"),
        "Note: may discard uncommitted changes",
    ),
    (
        re.compile(r"\bgit\s+push\b[^;&|\n]*[ \t](--force|--force-with-lease|-f)\b"),
        "Note: may overwrite remote history",
    ),
    (
        re.compile(r"\bgit\s+clean\b(?![^;&|\n]*(?:-[a-zA-Z]*n|--dry-run))[^;&|\n]*-[a-zA-Z]*f"),
        "Note: may permanently delete untracked files",
    ),
    (
        re.compile(r"\bgit\s+checkout\s+(--\s+)?\.[ \t]*($|[;&|\n])"),
        "Note: may discard all working tree changes",
    ),
    (
        re.compile(r"\bgit\s+restore\s+(--\s+)?\.[ \t]*($|[;&|\n])"),
        "Note: may discard all working tree changes",
    ),
    (
        re.compile(r"\bgit\s+stash[ \t]+(drop|clear)\b"),
        "Note: may permanently remove stashed changes",
    ),
    (
        re.compile(r"\bgit\s+branch\s+(-D[ \t]|--delete\s+--force|--force\s+--delete)\b"),
        "Note: may force-delete a branch",
    ),
    # Git — safety bypass
    (
        re.compile(r"\bgit\s+(commit|push|merge)\b[^;&|\n]*--no-verify\b"),
        "Note: may skip safety hooks",
    ),
    (
        re.compile(r"\bgit\s+commit\b[^;&|\n]*--amend\b"),
        "Note: may rewrite the last commit",
    ),
    # File deletion. Dangerous removal paths are hard-denied by the path
    # validator; the patterns here are informational counterparts.
    (
        re.compile(
            r"(^|[;&|\n]\s*)rm\s+-[a-zA-Z]*[rR][a-zA-Z]*f|"
            r"(^|[;&|\n]\s*)rm\s+-[a-zA-Z]*f[a-zA-Z]*[rR]"
        ),
        "Note: may recursively force-remove files",
    ),
    (
        re.compile(r"(^|[;&|\n]\s*)rm\s+-[a-zA-Z]*[rR]"),
        "Note: may recursively remove files",
    ),
    (
        re.compile(r"(^|[;&|\n]\s*)rm\s+-[a-zA-Z]*f"),
        "Note: may force-remove files",
    ),
    # Database
    (
        re.compile(r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE|SCHEMA)\b", re.IGNORECASE),
        "Note: may drop or truncate database objects",
    ),
    (
        re.compile(r"\bDELETE\s+FROM\s+\w+[ \t]*(;|\"|'|\n|$)", re.IGNORECASE),
        "Note: may delete all rows from a database table",
    ),
    # Infrastructure
    (
        re.compile(r"\bkubectl\s+delete\b"),
        "Note: may delete Kubernetes resources",
    ),
    (
        re.compile(r"\bterraform\s+destroy\b"),
        "Note: may destroy Terraform infrastructure",
    ),
]


def get_destructive_command_warning(command: str) -> str | None:
    """Return a destructive-command warning, or ``None``.

    Informational only — does NOT affect permission logic.  Consumed by
    permission prompts and logged when the tool is invoked via ``run_tool_use``.
    """
    for pattern, warning in _DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            return warning
    return None


# ─────────────────────────────────────────────────────────────────────
#  Command exit-code semantics
# ─────────────────────────────────────────────────────────────────────

CommandSemantic = Callable[[int, str, str], "InterpretResult"]


class InterpretResult:
    """Semantic interpretation of a command exit code.

    Mirrors the small ``{is_error, message}`` shape used by shell helpers.
    """

    __slots__ = ("is_error", "message")

    def __init__(self, is_error: bool, message: str | None = None):
        self.is_error = is_error
        self.message = message

    def __repr__(self) -> str:
        return f"InterpretResult(is_error={self.is_error}, message={self.message!r})"


def _default_semantic(exit_code: int, _stdout: str, _stderr: str) -> InterpretResult:
    """Default semantic: any non-zero exit code is an error."""
    if exit_code != 0:
        return InterpretResult(
            is_error=True,
            message=f"Command failed with exit code {exit_code}",
        )
    return InterpretResult(is_error=False)


def _grep_like_semantic(exit_code: int, _stdout: str, _stderr: str) -> InterpretResult:
    """OpenSpace grep / ripgrep: 0 matches, 1 = no matches, 2+ = error."""
    return InterpretResult(
        is_error=exit_code >= 2,
        message="No matches found" if exit_code == 1 else None,
    )


def _find_semantic(exit_code: int, _stdout: str, _stderr: str) -> InterpretResult:
    """OpenSpace find: 0 = success, 1 = partial (some dirs inaccessible), 2+ = error."""
    return InterpretResult(
        is_error=exit_code >= 2,
        message="Some directories were inaccessible" if exit_code == 1 else None,
    )


def _diff_semantic(exit_code: int, _stdout: str, _stderr: str) -> InterpretResult:
    """OpenSpace diff: 0 = no differences, 1 = differences found, 2+ = error."""
    return InterpretResult(
        is_error=exit_code >= 2,
        message="Files differ" if exit_code == 1 else None,
    )


def _test_semantic(exit_code: int, _stdout: str, _stderr: str) -> InterpretResult:
    """OpenSpace test / ``[``: 0 = true, 1 = false, 2+ = error."""
    return InterpretResult(
        is_error=exit_code >= 2,
        message="Condition is false" if exit_code == 1 else None,
    )


_COMMAND_SEMANTICS: dict[str, CommandSemantic] = {
    "grep": _grep_like_semantic,
    "rg": _grep_like_semantic,
    "find": _find_semantic,
    "diff": _diff_semantic,
    "test": _test_semantic,
    "[": _test_semantic,
}


def _extract_base_command(command: str) -> str:
    """OpenSpace ``extractBaseCommand`` — first whitespace-delimited token."""
    tokens = command.strip().split(None, 1)
    return tokens[0] if tokens else ""


def _heuristically_extract_base_command(command: str) -> str:
    """OpenSpace ``heuristicallyExtractBaseCommand`` — last subcommand's head.

    The last subcommand determines the pipeline exit code in ``set -o
    pipefail`` off (shell default), so OpenSpace takes the tail segment as the
    "effective" base command.
    """
    segments = split_command_segments(command)
    last = segments[-1] if segments else command
    return _extract_base_command(last)


def interpret_command_result(
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> InterpretResult:
    """Interpret a command result using command-specific exit-code rules.

    Implementation: ``BashTool/commandSemantics.ts`` ``interpretCommandResult``.

    Consumed by ``BashTool._arun`` (step 8.7).  Without this, a
    ``grep foo file`` with no matches would be surfaced to the model as a
    failed tool call, and ``diff a b`` on differing files would look like
    an error instead of a semantic signal.
    """
    base = _heuristically_extract_base_command(command)
    semantic = _COMMAND_SEMANTICS.get(base, _default_semantic)
    return semantic(exit_code, stdout, stderr)


# ─────────────────────────────────────────────────────────────────────
#  Auto-background allow-list — OpenSpace BashTool.tsx L220-221, L307-315
# ─────────────────────────────────────────────────────────────────────

# OpenSpace ``DISALLOWED_AUTO_BACKGROUND_COMMANDS`` — commands the model may
# not auto-background even when assistant-mode budget is exceeded.
# ``sleep`` is the only entry in OpenSpace; when a user explicitly passes
# ``run_in_background: true`` it is still honored.
DISALLOWED_AUTO_BACKGROUND_COMMANDS: tuple[str, ...] = ("sleep",)


def is_autobackgrounding_allowed(command: str) -> bool:
    """OpenSpace ``isAutobackgroundingAllowed`` (BashTool.tsx L307-315).

    Returns ``False`` iff the *first* subcommand matches
    :data:`DISALLOWED_AUTO_BACKGROUND_COMMANDS`.  Used by the background
    executor to decide whether to auto-detach a long-running command;
    the user's explicit ``run_in_background=True`` always wins.
    """
    segments = split_command_segments(command)
    if not segments:
        return True
    base = _extract_base_command(segments[0])
    if not base:
        return True
    return base not in DISALLOWED_AUTO_BACKGROUND_COMMANDS
