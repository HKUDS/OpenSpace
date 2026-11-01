"""sed 2-pattern allowlist + dangerous-operation denylist.

Entry points:

- :func:`sed_command_is_allowed_by_allowlist` returns True iff the sed
  invocation matches exactly one of two safe shapes:

    **Pattern 1** (line printing): ``sed -n [-E|-r|-z] 'EXPR' [files...]``
    where EXPR is a semicolon-separated list of ``[N[,M]]p`` commands.

    **Pattern 2** (substitution): ``sed [-E|-r] 's/PAT/REPL/FLAGS' [files...]``
    where FLAGS is drawn from ``g p i I m M`` + optional single digit 1-9.
    When ``allow_file_writes=False`` (default) file arguments are
    forbidden; when True the ``-i`` / ``--in-place`` flag is also allowed.

  Even when the allowlist matches, a separate denylist check
  (:func:`_contains_dangerous_operations`) is applied as defence in
  depth — it rejects any sed expression containing non-ASCII bytes,
  newlines, braces, the ``e`` execute command, ``w`` write command,
  negation operators, step-address ``~`` syntax, or malformed
  substitutions.

- :func:`check_sed_constraints` loops over the pipeline's subcommands (via
  :func:`shell_parser.split_command_segments`) and returns an ``ask``
  :class:`~.bash_classifier.PermissionResult` as soon as any sed
  subcommand fails the allowlist.  ``passthrough`` when all sed calls
  are safe (or no sed is present).

- :func:`sed_additional_dangerous_callback` — the callback the
  :data:`readonly_commands.COMMAND_ALLOWLIST_LOCAL` ``sed`` entry points
  at.  Returns True (``dangerous``) when ``sed_command_is_allowed_by_allowlist``
  returns False.  This is the inverse contract: ``safeFlags`` check +
  callback must **both** pass for Layer 2 to allow.

Parser note: :func:`shell_parser.try_parse_shell_command` returns plain string
tokens. Unquoted glob strings such as ``*.log`` are still collected as
positional tokens and counted toward ``arg_count > 1``.

``acceptEdits`` mode plumbing is represented by ``allow_file_writes``. Callers
default it to ``False`` unless permission context explicitly allows file writes.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .shell_parser import split_command_segments, try_parse_shell_command


__all__ = [
    "sed_command_is_allowed_by_allowlist",
    "check_sed_constraints",
    "sed_additional_dangerous_callback",
    # Exported for focused unit coverage.
    "is_print_command",
    "is_line_printing_command",
    "extract_sed_expressions",
    "has_file_args",
]


_SED_PREFIX_RE = re.compile(r"^\s*sed\s+")


# ─────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────


def _validate_flags_against_allowlist(flags: list[str], allowed: list[str]) -> bool:
    """OpenSpace ``validateFlagsAgainstAllowlist`` (L13-35).

    Handles combined short flags (``-nE``) by checking every character.
    """
    for flag in flags:
        if flag.startswith("-") and not flag.startswith("--") and len(flag) > 2:
            for i in range(1, len(flag)):
                single = "-" + flag[i]
                if single not in allowed:
                    return False
        else:
            if flag not in allowed:
                return False
    return True


def is_print_command(cmd: str) -> bool:
    """OpenSpace ``isPrintCommand`` (L128-133).

    Strict allowlist: ``p`` / ``Np`` / ``N,Mp``.
    """
    if not cmd:
        return False
    return re.fullmatch(r"(?:\d+|\d+,\d+)?p", cmd) is not None


def _parse_tokens(without_sed: str) -> Optional[list[str]]:
    """Parse post-``sed `` portion with :mod:`shell_parser`.  None if parse fails."""
    result = try_parse_shell_command(without_sed)
    if not result.success:
        return None
    return list(result.tokens)


# ─────────────────────────────────────────────────────────────────────
#  is_line_printing_command — Pattern 1
# ─────────────────────────────────────────────────────────────────────


def is_line_printing_command(command: str, expressions: list[str]) -> bool:
    """OpenSpace ``isLinePrintingCommand``."""
    m = _SED_PREFIX_RE.match(command)
    if not m:
        return False
    without_sed = command[len(m.group(0)) :]
    tokens = _parse_tokens(without_sed)
    if tokens is None:
        return False

    flags = [t for t in tokens if t.startswith("-") and t != "--"]

    allowed_flags = [
        "-n",
        "--quiet",
        "--silent",
        "-E",
        "--regexp-extended",
        "-r",
        "-z",
        "--zero-terminated",
        "--posix",
    ]
    if not _validate_flags_against_allowlist(flags, allowed_flags):
        return False

    # Must have -n / --quiet / --silent OR a bundle containing 'n'
    has_n = False
    for flag in flags:
        if flag in ("-n", "--quiet", "--silent"):
            has_n = True
            break
        if flag.startswith("-") and not flag.startswith("--") and "n" in flag:
            has_n = True
            break

    if not has_n:
        return False

    if not expressions:
        return False

    for expr in expressions:
        for cmd in expr.split(";"):
            if not is_print_command(cmd.strip()):
                return False

    return True


# ─────────────────────────────────────────────────────────────────────
#  is_substitution_command — Pattern 2
# ─────────────────────────────────────────────────────────────────────


def _is_substitution_command(
    command: str,
    expressions: list[str],
    has_file_arguments: bool,
    *,
    allow_file_writes: bool = False,
) -> bool:
    """OpenSpace ``isSubstitutionCommand`` — strict ``s/PAT/REPL/FLAGS`` matcher."""
    if not allow_file_writes and has_file_arguments:
        return False

    m = _SED_PREFIX_RE.match(command)
    if not m:
        return False
    without_sed = command[len(m.group(0)) :]
    tokens = _parse_tokens(without_sed)
    if tokens is None:
        return False

    flags = [t for t in tokens if t.startswith("-") and t != "--"]

    allowed_flags = ["-E", "--regexp-extended", "-r", "--posix"]
    if allow_file_writes:
        allowed_flags.extend(["-i", "--in-place"])

    if not _validate_flags_against_allowlist(flags, allowed_flags):
        return False

    if len(expressions) != 1:
        return False

    expr = expressions[0].strip()
    if not expr.startswith("s"):
        return False

    # Only '/' delimiter allowed — rest of expr after 's/' walked for 2 unescaped '/'.
    if not expr.startswith("s/"):
        return False
    rest = expr[2:]

    delimiter_count = 0
    last_delim_pos = -1
    i = 0
    while i < len(rest):
        ch = rest[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "/":
            delimiter_count += 1
            last_delim_pos = i
        i += 1

    if delimiter_count != 2:
        return False

    expr_flags = rest[last_delim_pos + 1 :]

    # Allowed: any permutation of g p i I m M + optional single 1-9
    if not re.fullmatch(r"[gpimIM]*[1-9]?[gpimIM]*", expr_flags):
        return False

    return True


# ─────────────────────────────────────────────────────────────────────
#  extract_sed_expressions
# ─────────────────────────────────────────────────────────────────────


class _SedParseError(Exception):
    """Internal — indicates extract_sed_expressions could not parse."""


def extract_sed_expressions(command: str) -> list[str]:
    """OpenSpace ``extractSedExpressions``.  Raises on malformed syntax."""
    expressions: list[str] = []

    m = _SED_PREFIX_RE.match(command)
    if not m:
        return expressions

    without_sed = command[len(m.group(0)) :]

    if re.search(r"-e[wWe]", without_sed) or re.search(r"-w[eE]", without_sed):
        raise _SedParseError("Dangerous flag combination detected")

    tokens = _parse_tokens(without_sed)
    if tokens is None:
        raise _SedParseError("Malformed shell syntax")

    try:
        found_e = False
        found_expr = False
        i = 0
        while i < len(tokens):
            arg = tokens[i]

            if (arg == "-e" or arg == "--expression") and i + 1 < len(tokens):
                found_e = True
                expressions.append(tokens[i + 1])
                i += 2
                continue

            if arg.startswith("--expression="):
                found_e = True
                expressions.append(arg[len("--expression=") :])
                i += 1
                continue

            if arg.startswith("-e="):
                found_e = True
                expressions.append(arg[len("-e=") :])
                i += 1
                continue

            if arg.startswith("-"):
                i += 1
                continue

            if not found_e and not found_expr:
                expressions.append(arg)
                found_expr = True
                i += 1
                continue

            break
    except Exception as e:  # pragma: no cover - defensive mirror of OpenSpace try/catch
        raise _SedParseError(f"Failed to parse sed command: {e}")

    return expressions


# ─────────────────────────────────────────────────────────────────────
#  has_file_args
# ─────────────────────────────────────────────────────────────────────


def has_file_args(command: str) -> bool:
    """OpenSpace ``hasFileArgs`` — True if sed is invoked with ≥1 file operand.

    Returns True (conservative) when the command fails to parse.
    """
    m = _SED_PREFIX_RE.match(command)
    if not m:
        return False

    without_sed = command[len(m.group(0)) :]
    tokens = _parse_tokens(without_sed)
    if tokens is None:
        return True  # assume dangerous on parse failure (OpenSpace semantics)

    try:
        arg_count = 0
        has_e_flag = False

        i = 0
        while i < len(tokens):
            arg = tokens[i]

            if (arg == "-e" or arg == "--expression") and i + 1 < len(tokens):
                has_e_flag = True
                i += 2
                continue

            if arg.startswith("--expression="):
                has_e_flag = True
                i += 1
                continue

            if arg.startswith("-e="):
                has_e_flag = True
                i += 1
                continue

            if arg.startswith("-"):
                i += 1
                continue

            arg_count += 1

            if has_e_flag:
                return True

            if arg_count > 1:
                return True

            i += 1

        return False
    except Exception:  # pragma: no cover
        return True


# ─────────────────────────────────────────────────────────────────────
#  contains_dangerous_operations
# ─────────────────────────────────────────────────────────────────────


_NON_ASCII_RE = re.compile(r"[^\x01-\x7F]")
_STEP_ADDR_RE = re.compile(r"\d\s*~\s*\d|,\s*~\s*\d|\$\s*~\s*\d")
_COMMA_OFFSET_RE = re.compile(r",\s*[+-]")
_SED_BACKSLASH_RE = re.compile(r"s\\")
_ALT_DELIM_ESCAPE_RE = re.compile(r"\\[|#%@]")
_ESCAPED_SLASH_WW_RE = re.compile(r"\\/.*[wW]")
_SUSPICIOUS_SLASH_WEE_RE = re.compile(r"/[^/]*\s+[wWeE]")
_MALFORMED_SUBST_RE = re.compile(r"^s/(?!.*/[^/]*/[^/]*$).*$")
_S_NON_SLASH_DELIM_RE = re.compile(r"^s\.")  # starts with 's' + any char
_WW_EE_SUFFIX_RE = re.compile(r"[wWeE]$")
_PROPER_SUBST_RE = re.compile(r"^s([^\\\n]).*?\1.*?\1[^wWeE]*$")

# Write-command shapes (L569-577)
_W_WRITE_PATTERNS = [
    re.compile(r"^[wW]\s*\S+"),
    re.compile(r"^\d+\s*[wW]\s*\S+"),
    re.compile(r"^\$\s*[wW]\s*\S+"),
    re.compile(r"^/[^/]*/[IMim]*\s*[wW]\s*\S+"),
    re.compile(r"^\d+,\d+\s*[wW]\s*\S+"),
    re.compile(r"^\d+,\$\s*[wW]\s*\S+"),
    re.compile(r"^/[^/]*/[IMim]*,/[^/]*/[IMim]*\s*[wW]\s*\S+"),
]

# Execute-command shapes (L585-593)
_E_EXEC_PATTERNS = [
    re.compile(r"^e"),
    re.compile(r"^\d+\s*e"),
    re.compile(r"^\$\s*e"),
    re.compile(r"^/[^/]*/[IMim]*\s*e"),
    re.compile(r"^\d+,\d+\s*e"),
    re.compile(r"^\d+,\$\s*e"),
    re.compile(r"^/[^/]*/[IMim]*,/[^/]*/[IMim]*\s*e"),
]

_SUBST_WITH_FLAGS_RE = re.compile(r"s([^\\\n]).*?\1.*?\1(.*?)$")
_Y_CMD_RE = re.compile(r"y([^\\\n])")


def _contains_dangerous_operations(expression: str) -> bool:
    """OpenSpace ``containsDangerousOperations`` — conservative denylist."""
    cmd = expression.strip()
    if not cmd:
        return False

    if _NON_ASCII_RE.search(cmd):
        return True

    if "{" in cmd or "}" in cmd:
        return True

    if "\n" in cmd:
        return True

    # Reject `#` comments (not the delimiter form ``s#pattern#replacement#``)
    hash_idx = cmd.find("#")
    if hash_idx != -1 and not (hash_idx > 0 and cmd[hash_idx - 1] == "s"):
        return True

    # Negation
    if cmd.startswith("!") or re.search(r"[/\d$]!", cmd):
        return True

    if _STEP_ADDR_RE.search(cmd):
        return True

    if cmd.startswith(","):
        return True

    if _COMMA_OFFSET_RE.search(cmd):
        return True

    if _SED_BACKSLASH_RE.search(cmd) or _ALT_DELIM_ESCAPE_RE.search(cmd):
        return True

    if _ESCAPED_SLASH_WW_RE.search(cmd):
        return True

    if _SUSPICIOUS_SLASH_WEE_RE.search(cmd):
        return True

    # Malformed s/… — not fitting s/pat/repl/flags.
    if cmd.startswith("s/") and not re.fullmatch(r"s/[^/]*/[^/]*/[^/]*", cmd):
        return True

    # Paranoid: any `s<delim>…` ending in w/W/e/E that is not a properly
    # formed substitution.
    if _S_NON_SLASH_DELIM_RE.match(cmd) and _WW_EE_SUFFIX_RE.search(cmd):
        if not _PROPER_SUBST_RE.match(cmd):
            return True

    for pat in _W_WRITE_PATTERNS:
        if pat.match(cmd):
            return True

    for pat in _E_EXEC_PATTERNS:
        if pat.match(cmd):
            return True

    # Check substitution flags for w/W/e/E
    sub_m = _SUBST_WITH_FLAGS_RE.match(cmd)
    if sub_m:
        subst_flags = sub_m.group(2) or ""
        if "w" in subst_flags or "W" in subst_flags:
            return True
        if "e" in subst_flags or "E" in subst_flags:
            return True

    # Paranoid y (transliterate) with any w/W/e/E anywhere
    if _Y_CMD_RE.match(cmd):
        if re.search(r"[wWeE]", cmd):
            return True

    return False


# ─────────────────────────────────────────────────────────────────────
#  sed_command_is_allowed_by_allowlist
# ─────────────────────────────────────────────────────────────────────


def sed_command_is_allowed_by_allowlist(
    command: str, *, allow_file_writes: bool = False
) -> bool:
    """OpenSpace ``sedCommandIsAllowedByAllowlist``.

    Returns True iff the command matches **exactly one** safe pattern AND
    passes the denylist.  Conservative on any parse failure.
    """
    try:
        expressions = extract_sed_expressions(command)
    except _SedParseError:
        return False

    hf = has_file_args(command)

    is_pattern_1 = False
    is_pattern_2 = False

    if allow_file_writes:
        # only substitution allowed with file writes
        is_pattern_2 = _is_substitution_command(
            command, expressions, hf, allow_file_writes=True
        )
    else:
        is_pattern_1 = is_line_printing_command(command, expressions)
        is_pattern_2 = _is_substitution_command(command, expressions, hf)

    if not is_pattern_1 and not is_pattern_2:
        return False

    # Pattern 2 forbids `;`
    for expr in expressions:
        if is_pattern_2 and ";" in expr:
            return False

    for expr in expressions:
        if _contains_dangerous_operations(expr):
            return False

    return True


# ─────────────────────────────────────────────────────────────────────
#  check_sed_constraints
# ─────────────────────────────────────────────────────────────────────


def check_sed_constraints(
    command: str,
    *,
    mode: Optional[str] = None,
) -> dict[str, Any]:
    """OpenSpace ``checkSedConstraints``.

    Iterates over pipeline subcommands; for each ``sed`` invocation, calls
    :func:`sed_command_is_allowed_by_allowlist`.  Returns an ``ask``
    :class:`~.bash_classifier.PermissionResult` if any fails, else
    ``passthrough``.

    Parameters
    ----------
    mode:
        When ``"acceptEdits"``, ``allow_file_writes`` is set True (OpenSpace
        L658-659).  Any other value (or None) keeps the stricter default.
    """
    allow_file_writes = mode == "acceptEdits"

    for cmd in split_command_segments(command):
        trimmed = cmd.strip()
        base = trimmed.split()[0] if trimmed else ""
        if base != "sed":
            continue

        if not sed_command_is_allowed_by_allowlist(
            trimmed, allow_file_writes=allow_file_writes
        ):
            return {
                "behavior": "ask",
                "message": (
                    "sed command requires approval "
                    "(contains potentially dangerous operations)"
                ),
            }

    return {
        "behavior": "passthrough",
        "message": "No dangerous sed operations detected",
    }


# ─────────────────────────────────────────────────────────────────────
#  Bridge: additional_command_is_dangerous_callback for sed entry
# ─────────────────────────────────────────────────────────────────────


def sed_additional_dangerous_callback(raw_command: str, _args: list[str]) -> bool:
    """read-only validation real callback replacing the earlier read-only validation ``_sed_additional_callback`` stub.

    Returns True (dangerous) when the command does **not** match the
    safe 2-pattern allowlist.  Wired into
    :data:`readonly_commands.COMMAND_ALLOWLIST_LOCAL` via
    :func:`bash_classifier.register_shared_commands`.

    Note: ``raw_command`` is the full command string (OpenSpace passes the same
    value).  ``_args`` is unused — the sed validator re-tokenises the
    command internally for shell-quote parity.
    """
    return not sed_command_is_allowed_by_allowlist(raw_command)
