"""Flag validation, UNC path detection, and unquoted-expansion detection.

These utilities are the bedrock the ``bash_classifier`` dispatcher stands
on.  They are intentionally stateless — no I/O, no globals — so they can
be unit-tested in isolation.
"""

from __future__ import annotations

import platform as _platform_mod
import re
from typing import Optional, Sequence, TypedDict

from .readonly_commands import CommandConfig, FlagArgType

__all__ = [
    "FLAG_PATTERN",
    "ValidateFlagsOptions",
    "validate_flag_argument",
    "validate_flags",
    "contains_vulnerable_unc_path",
    "contains_unquoted_expansion",
]


# ─────────────────────────────────────────────────────────────────────
#  FLAG_PATTERN
# ─────────────────────────────────────────────────────────────────────

FLAG_PATTERN = re.compile(r"^-[a-zA-Z0-9_-]")


# ─────────────────────────────────────────────────────────────────────
#  validate_flag_argument
# ─────────────────────────────────────────────────────────────────────


def validate_flag_argument(value: str, arg_type: FlagArgType) -> bool:
    """Validate a flag argument against a declared argument type."""
    if arg_type == "none":
        # OpenSpace returns false here defensively (should never be called for 'none').
        return False
    if arg_type == "number":
        return bool(re.fullmatch(r"\d+", value))
    if arg_type == "string":
        # Any string including empty is valid for string-typed args.
        return True
    if arg_type == "char":
        return len(value) == 1
    if arg_type == "{}":
        return value == "{}"
    if arg_type == "EOF":
        return value == "EOF"
    return False


# ─────────────────────────────────────────────────────────────────────
#  validate_flags
# ─────────────────────────────────────────────────────────────────────


class ValidateFlagsOptions(TypedDict, total=False):
    """Options for flag validation."""

    commandName: str
    rawCommand: str
    xargsTargetCommands: Sequence[str]


def validate_flags(
    tokens: list[str],
    start_index: int,
    config: CommandConfig,
    options: Optional[ValidateFlagsOptions] = None,
) -> bool:
    """Verify every flag in *tokens[start_index:]* against *config*['safeFlags'].

    Returns True iff every flag is known-safe.  Does **not** invoke the
    ``additional_command_is_dangerous_callback`` — callers do that
    separately (see ``is_command_safe_via_flag_parsing``).

    Security-critical details:

    1. ``-E=`` triggers ``has_equals=True`` with empty ``inline_value``;
       we honour the explicit empty value and do NOT consume the next
       token (prevents xargs ``-E= EOF echo`` code-exec).
    2. Bundled short flags (``-nr``) must ALL be type ``"none"``; any
       arg-taking flag in a bundle is rejected to avoid GNU getopt
       parser-differential (``-rI`` in xargs).
    3. ``--`` is honoured only if ``respects_double_dash`` is not
       explicitly False (pyright doesn't respect ``--``).
    4. ``string``-typed args starting with ``-`` are rejected, except
       git ``--sort`` (allows ``-refname`` for reverse sort).
    5. git numeric shorthand ``-<N>`` is accepted (as ``-n <N>``).
    6. grep/rg attached numeric args (``-A20``) are accepted.
    """
    opts = options or {}
    safe_flags = config.get("safeFlags", {})
    respects_dd = config.get("respects_double_dash", True)
    xargs_targets = opts.get("xargsTargetCommands")
    command_name = opts.get("commandName")

    i = start_index
    n = len(tokens)

    while i < n:
        token = tokens[i]
        if not token:
            i += 1
            continue

        # ───── xargs target-command detection ─────
        if (
            xargs_targets is not None
            and command_name == "xargs"
            and (not token.startswith("-") or token == "--")
        ):
            if token == "--" and i + 1 < n:
                i += 1
                token = tokens[i]
            if token and token in xargs_targets:
                break
            return False

        # ───── `--` end-of-options ─────
        if token == "--":
            if respects_dd is not False:
                i += 1
                break
            i += 1
            continue

        # ───── flag-shaped token ─────
        if token.startswith("-") and len(token) > 1 and FLAG_PATTERN.match(token):
            has_equals = "=" in token
            if has_equals:
                flag, _, inline_value = token.partition("=")
            else:
                flag, inline_value = token, ""

            if not flag:
                return False

            flag_arg_type = safe_flags.get(flag)

            if flag_arg_type is None:
                # git numeric shorthand: -5 == -n 5
                if command_name == "git" and re.fullmatch(r"-\d+", flag):
                    i += 1
                    continue

                # grep/rg attached numeric: -A20
                if (
                    command_name in ("grep", "rg")
                    and flag.startswith("-")
                    and not flag.startswith("--")
                    and len(flag) > 2
                ):
                    potential_flag = flag[:2]
                    potential_value = flag[2:]
                    pf_type = safe_flags.get(potential_flag)
                    if pf_type and re.fullmatch(r"\d+", potential_value):
                        if pf_type in ("number", "string"):
                            if validate_flag_argument(potential_value, pf_type):
                                i += 1
                                continue
                            return False

                # Bundled single-letter short flags (-nr)
                if (
                    flag.startswith("-")
                    and not flag.startswith("--")
                    and len(flag) > 2
                ):
                    for j in range(1, len(flag)):
                        single = "-" + flag[j]
                        ft = safe_flags.get(single)
                        if ft is None:
                            return False
                        if ft != "none":
                            # Arg-taking flag in a bundle — reject (GNU getopt
                            # would consume the NEXT token, parser differential).
                            return False
                    i += 1
                    continue

                return False  # unknown flag

            # ───── known flag: argument handling ─────
            if flag_arg_type == "none":
                if has_equals:
                    return False  # flag=value but flag takes no arg
                i += 1
                continue

            # flag takes an argument
            if has_equals:
                arg_value: str = inline_value
                i += 1
            else:
                # consume next token
                if i + 1 >= n:
                    return False
                nxt = tokens[i + 1]
                if (
                    nxt
                    and nxt.startswith("-")
                    and len(nxt) > 1
                    and FLAG_PATTERN.match(nxt)
                ):
                    return False  # missing argument
                arg_value = nxt or ""
                i += 2

            # Defense-in-depth: reject string-args starting with '-'
            if flag_arg_type == "string" and arg_value.startswith("-"):
                # Exception: git --sort allows -prefix for reverse sort
                if (
                    flag == "--sort"
                    and command_name == "git"
                    and re.match(r"^-[a-zA-Z]", arg_value)
                ):
                    pass
                else:
                    return False

            if not validate_flag_argument(arg_value, flag_arg_type):
                return False

        else:
            # Non-flag positional (paths, rev specs) — allowed.
            i += 1

    return True


# ─────────────────────────────────────────────────────────────────────
#  containsVulnerableUncPath
# ─────────────────────────────────────────────────────────────────────


def _get_platform() -> str:
    """OpenSpace ``getPlatform`` equivalent.

    Returns 'windows', 'darwin', or 'linux' (lower-case).  os simplifies
    OpenSpace's env-var-aware detection since our codebase never ships a UNC
    path outside Windows test fixtures.
    """
    sys_name = _platform_mod.system().lower()
    if sys_name == "windows":
        return "windows"
    if sys_name == "darwin":
        return "darwin"
    return "linux"


_BACKSLASH_UNC_RE = re.compile(
    r"\\\\[^\s\\/]+(?:@(?:\d+|ssl))?(?:[\\/]|$|\s)", re.IGNORECASE
)
_FORWARD_SLASH_UNC_RE = re.compile(
    r"(?<!:)//[^\s\\/]+(?:@(?:\d+|ssl))?(?:[\\/]|$|\s)", re.IGNORECASE
)
_MIXED_SLASH_UNC_RE = re.compile(r"/\\{2,}[^\s\\/]")
_REVERSE_MIXED_UNC_RE = re.compile(r"\\{2,}/[^\s\\/]")
_WEBDAV_SSL_PORT_RE_1 = re.compile(r"@SSL@\d+", re.IGNORECASE)
_WEBDAV_SSL_PORT_RE_2 = re.compile(r"@\d+@SSL", re.IGNORECASE)
_DAVWWWROOT_RE = re.compile(r"DavWWWRoot", re.IGNORECASE)
_UNC_IPV4_BS_RE = re.compile(r"^\\\\(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})[\\/]")
_UNC_IPV4_FS_RE = re.compile(r"^//(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})[\\/]")
_UNC_IPV6_BS_RE = re.compile(r"^\\\\(\[[\da-fA-F:]+\])[\\/]")
_UNC_IPV6_FS_RE = re.compile(r"^//(\[[\da-fA-F:]+\])[\\/]")


def contains_vulnerable_unc_path(
    path_or_command: str, *, force_check: bool = False
) -> bool:
    """OpenSpace ``containsVulnerableUncPath``.

    Only active on Windows by default.  Pass ``force_check=True`` to
    run the check regardless of platform (useful for tests).
    """
    if not force_check and _get_platform() != "windows":
        return False

    if _BACKSLASH_UNC_RE.search(path_or_command):
        return True
    if _FORWARD_SLASH_UNC_RE.search(path_or_command):
        return True
    if _MIXED_SLASH_UNC_RE.search(path_or_command):
        return True
    if _REVERSE_MIXED_UNC_RE.search(path_or_command):
        return True
    if _WEBDAV_SSL_PORT_RE_1.search(path_or_command):
        return True
    if _WEBDAV_SSL_PORT_RE_2.search(path_or_command):
        return True
    if _DAVWWWROOT_RE.search(path_or_command):
        return True
    if _UNC_IPV4_BS_RE.search(path_or_command):
        return True
    if _UNC_IPV4_FS_RE.search(path_or_command):
        return True
    if _UNC_IPV6_BS_RE.search(path_or_command):
        return True
    if _UNC_IPV6_FS_RE.search(path_or_command):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────
#  containsUnquotedExpansion — OpenSpace readOnlyValidation.ts L1600-1669
# ─────────────────────────────────────────────────────────────────────


_VAR_NAME_FIRST_CHAR = re.compile(r"[A-Za-z_@*#?!$0-9-]")
_GLOB_CHAR = re.compile(r"[?*\[\]]")


def contains_unquoted_expansion(command: str) -> bool:
    """OpenSpace ``containsUnquotedExpansion`` — glob + ``$VAR`` outside single-quotes.

    Matches `` `$` `` followed by a variable-name/special-parameter
    character, OR unquoted glob characters (``?``, ``*``, ``[``, ``]``).
    Ignores content inside single-quoted strings (bash literal).
    ``${...}`` and ``$(...)`` are NOT matched here — those are caught by
    the conservative injection gate.
    """
    in_single = False
    in_double = False
    escaped = False

    i = 0
    while i < len(command):
        ch = command[i]

        if escaped:
            escaped = False
            i += 1
            continue

        # Backslash escapes only OUTSIDE single quotes (bash literal inside '').
        if ch == "\\" and not in_single:
            escaped = True
            i += 1
            continue

        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue

        if in_single:
            i += 1
            continue

        # `$` followed by variable-name char — expands inside DQ and unquoted.
        if ch == "$":
            if i + 1 < len(command) and _VAR_NAME_FIRST_CHAR.match(command[i + 1]):
                return True

        # Glob only expands unquoted (literal inside "" and '').
        if not in_double and _GLOB_CHAR.match(ch):
            return True

        i += 1

    return False
