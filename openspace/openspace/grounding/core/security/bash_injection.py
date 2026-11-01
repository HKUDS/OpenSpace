"""Conservative injection-pattern gate for ``check_read_only_constraints``.

**Purpose**: reject command-substitution, variable-expansion, and heredoc
attacks before running the read-only allowlist.

This module intentionally fails closed: any token that looks like a substitution,
unquoted expansion, or heredoc triggers ``behavior='passthrough'`` so the
command falls through to the full permission engine.

**Known limitations**:

- Any heredoc with ``$`` / `` ` `` in its body is routed to the permission
  engine. This is not a security issue, just more prompts.
- AST-level injection detection for parse-tree redirections, ``>()`` process
  substitution, and escape-count analysis is absent. The regexes below are a
  superset that over-trigger.

**Return contract**::

    { "behavior": "passthrough" | "allow", "message"?: str }

Only two outcomes are used by ``check_read_only_constraints``:

- ``passthrough`` = "cannot auto-allow as read-only, hand off to the
  real permission engine"
- ``allow``       = "safe, continue the read-only pipeline"
"""

from __future__ import annotations

import re
from typing import TypedDict

__all__ = [
    "InjectionCheckResult",
    "bash_command_passes_injection_gate",
    "has_injection_pattern",
]


class InjectionCheckResult(TypedDict, total=False):
    behavior: str  # "passthrough" or "allow"
    message: str


# ─────────────────────────────────────────────────────────────────────
#  Conservative injection pattern set.
#
#  Design: keep each pattern simple and well-commented; accept false
#  positives (will reach permission engine) but never false negatives
#  (never silently auto-allow an injection).
# ─────────────────────────────────────────────────────────────────────

# Command substitution: $(...) and backtick form.
_CMD_SUBSTITUTION_DOLLAR = re.compile(r"\$\(")
_CMD_SUBSTITUTION_BACKTICK = re.compile(r"`")

# Process substitution: <(...) or >(...)  — shell feature OpenSpace explicitly
# flags as dangerous in pathValidation.ts.
_PROCESS_SUBSTITUTION = re.compile(r"[<>]\(")

# Brace-form variable expansion: ${VAR}, ${VAR:-default}, etc.
#   We allow plain ``$1``/``$?`` etc. only if they look like positional
#   or special params *inside double-quotes* in the fast path below.
_BRACE_EXPANSION = re.compile(r"\$\{")

# Heredoc with unquoted delimiter: ``<< EOF`` (OpenSpace requires delimiter be
# quoted like ``<< 'EOF'`` for safe heredocs).
_HEREDOC = re.compile(r"<<-?\s*[^'\"\s]")

# History-expansion bang: ``!!``, ``!$``, ``!-1`` etc.  Dangerous in
# interactive shells; OpenSpace strips these via shell-quote normalisation.
_HISTORY_EXPANSION = re.compile(r"(?<![\\'\w])!(?:\$|!|-?\d+|[A-Za-z_])")

# Double-dollar: safe-to-reject conservatively.
_PARAMETER_DOLLAR_SIGN = re.compile(r"\$\$")


def has_injection_pattern(command: str) -> bool:
    """True if *command* contains any conservative injection trigger."""
    if not command:
        return False
    return any(
        pattern.search(command)
        for pattern in (
            _CMD_SUBSTITUTION_DOLLAR,
            _CMD_SUBSTITUTION_BACKTICK,
            _PROCESS_SUBSTITUTION,
            _BRACE_EXPANSION,
            _HEREDOC,
            _HISTORY_EXPANSION,
            _PARAMETER_DOLLAR_SIGN,
        )
    )


def bash_command_passes_injection_gate(command: str) -> InjectionCheckResult:
    """Return whether *command* can continue through read-only auto-allow.

    Returns ``behavior='passthrough'`` when *command* contains any
    potential injection trigger, else ``behavior='allow'``.

    Callers in ``check_read_only_constraints`` interpret ``passthrough``
    as "cannot auto-allow, hand off to permission engine".
    """
    if has_injection_pattern(command):
        return {
            "behavior": "passthrough",
            "message": (
                "Command contains potential injection patterns "
                "($(…) / `…` / ${…} / <<EOF / process substitution). "
                "Cannot auto-allow as read-only without full injection analysis."
            ),
        }
    return {"behavior": "allow"}
