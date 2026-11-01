"""Bash command classifier — main entry ``check_read_only_constraints``.

**Layer topology**:

    Layer 1 — destructive heuristics  (bash_security.get_destructive_command_warning)
    Layer 2 — COMMAND_ALLOWLIST flag parsing (is_command_safe_via_flag_parsing)
    Layer 3 — READONLY_COMMAND_REGEXES       (is_command_read_only fallback)
    Layer 4 — compound-command safety checks
    Layer 5 — permission engine              (caller)

**Public entry points**:

- :func:`check_read_only_constraints` — the one callers import.
- :func:`is_command_read_only` — per-subcommand helper (exported for tests).
- :func:`is_command_safe_via_flag_parsing` — exposed for tests.

**Implemented guards**:

- ``_extract_write_paths_from_subcommand`` now delegates to
  :func:`path_validation.extract_write_paths_from_subcommand` for
  git-internal-path write detection.
- ``register_shared_commands(build_shared_allowlist())`` is invoked at
  import time, merging ``GIT_READ_ONLY_COMMANDS``/
  ``GH_READ_ONLY_COMMANDS``/ ``DOCKER_READ_ONLY_COMMANDS``/
  ``RIPGREP_READ_ONLY_COMMANDS``/ ``PYRIGHT_READ_ONLY_COMMANDS`` from
  :mod:`readonly_shared_flags` into the central allowlist.

**Return contract**::

    {"behavior": "allow" | "passthrough" | "ask", "message"?: str, "updated_input"?: dict}
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional, TypedDict

from .bash_injection import bash_command_passes_injection_gate
from .flag_validation import (
    contains_unquoted_expansion,
    contains_vulnerable_unc_path,
    validate_flags,
)
from .path_validation import (
    extract_write_paths_from_subcommand as _path_extract_write_paths,
)
from .readonly_commands import (
    COMMAND_ALLOWLIST_LOCAL,
    GIT_INTERNAL_PATTERNS,
    READONLY_COMMAND_REGEXES,
    SAFE_TARGET_COMMANDS_FOR_XARGS,
    CommandConfig,
)
from .readonly_shared_flags import build_shared_allowlist
from .shell_parser import (
    command_has_any_cd,
    command_has_any_git,
    extract_output_redirections,
    split_command_segments,
    try_parse_shell_command,
)

__all__ = [
    "PermissionResult",
    "check_read_only_constraints",
    "is_command_read_only",
    "is_command_safe_via_flag_parsing",
    "get_command_allowlist",
    "register_shared_commands",
]


class PermissionResult(TypedDict, total=False):
    """OpenSpace ``PermissionResult`` — the shape every entry point returns."""

    behavior: str  # 'allow' | 'passthrough' | 'ask'
    message: str
    updated_input: dict[str, Any]


# ─────────────────────────────────────────────────────────────────────
#  COMMAND_ALLOWLIST registry
# ─────────────────────────────────────────────────────────────────────

_COMMAND_ALLOWLIST: dict[str, CommandConfig] = dict(COMMAND_ALLOWLIST_LOCAL)


def get_command_allowlist() -> dict[str, CommandConfig]:
    """OpenSpace ``getCommandAllowlist`` — returns the merged table.

    Returns a snapshot (dict view) so callers can iterate safely.
    """
    return _COMMAND_ALLOWLIST


def register_shared_commands(shared: dict[str, CommandConfig]) -> None:
    """Merge *shared* into the allowlist.

    Idempotent. Module initialization uses this to inject
    ``GIT_READ_ONLY_COMMANDS`` / ``GH_READ_ONLY_COMMANDS`` /
    ``DOCKER_READ_ONLY_COMMANDS`` / ``RIPGREP_READ_ONLY_COMMANDS`` /
    ``PYRIGHT_READ_ONLY_COMMANDS``.
    """
    _COMMAND_ALLOWLIST.update(shared)


# Merge shared tables exactly once at import.
register_shared_commands(build_shared_allowlist())


# ─────────────────────────────────────────────────────────────────────
#  command_writes_to_git_internal_paths
# ─────────────────────────────────────────────────────────────────────


def _is_git_internal_path(path: str) -> bool:
    """OpenSpace ``isGitInternalPath`` (L1781-1784)."""
    normalized = re.sub(r"^\.?/", "", path)
    return any(p.match(normalized) for p in GIT_INTERNAL_PATTERNS)


def _extract_write_paths_from_subcommand(subcommand: str) -> list[str]:
    """OpenSpace ``extractWritePathsFromSubcommand`` (L1795-1823).

    Thin delegation to :func:`path_validation.extract_write_paths_from_subcommand`
    which handles tokenisation, wrapper stripping, and the
    ``COMMAND_OPERATION_TYPE`` / ``NON_CREATING_WRITE_COMMANDS`` gating.
    """
    return _path_extract_write_paths(subcommand)


def _command_writes_to_git_internal_paths(command: str) -> bool:
    """Return whether a command writes to git internal repository paths."""
    for subcmd in split_command_segments(command):
        trimmed = subcmd.strip()

        for p in _extract_write_paths_from_subcommand(trimmed):
            if _is_git_internal_path(p):
                return True

        result = extract_output_redirections(trimmed)
        for redir in result.redirections:
            if _is_git_internal_path(redir.target):
                return True

    return False


# ─────────────────────────────────────────────────────────────────────
#  Sandbox / cwd filesystem guards
# ─────────────────────────────────────────────────────────────────────


def _resolve_path(value: str | Path | None) -> Path | None:
    try:
        return Path(value or os.getcwd()).expanduser().resolve(strict=False)
    except Exception:
        return None


def _sandbox_enabled(cwd: str | Path | None = None) -> bool:
    """OpenSpace ``SandboxManager.isSandboxingEnabled()``.

    Stage 25 wires this to the local process sandbox manager.  Fail closed to
    the sandbox-off classifier path if settings or dependency loading fails.
    """
    try:
        from openspace.services.sandbox import get_process_sandbox_manager

        return get_process_sandbox_manager(cwd=cwd).is_sandboxing_enabled()
    except Exception:
        return False


def _cwd_differs_from_original(
    cwd: str | Path | None = None,
    original_cwd: str | Path | None = None,
) -> bool:
    """OpenSpace ``getCwd() !== getOriginalCwd()``.

    A missing original cwd means the caller has no drift signal, so this guard
    stays inactive.  When both paths are available, compare normalized
    filesystem paths to avoid false positives from relative path spelling.
    """
    if cwd is None or original_cwd is None:
        return False
    current = _resolve_path(cwd)
    original = _resolve_path(original_cwd)
    if current is None or original is None:
        return False
    return current != original


def _is_current_directory_bare_git_repo(cwd: str | Path | None = None) -> bool:
    """Return whether *cwd* looks like a bare git repository root.

    A bare repository exposes ``HEAD``, ``objects/``, and ``refs/`` directly
    in the current directory instead of under a worktree ``.git`` directory.
    Git commands in that shape are not auto-allowed as read-only because they
    can operate on repository internals even when the command text looks safe.
    """
    path = _resolve_path(cwd)
    if path is None:
        return False
    try:
        if (path / ".git").exists():
            return False
        return (
            (path / "HEAD").is_file()
            and (path / "objects").is_dir()
            and (path / "refs").is_dir()
        )
    except OSError:
        return False


# ─────────────────────────────────────────────────────────────────────
#  isCommandSafeViaFlagParsing — OpenSpace readOnlyValidation.ts L1246-1408
# ─────────────────────────────────────────────────────────────────────


def is_command_safe_via_flag_parsing(command: str) -> bool:
    """OpenSpace ``isCommandSafeViaFlagParsing`` — Layer 2.

    1. Parse the command (shell_parser).
    2. If there are operators (redirect / pipe / &&) → False.
       (OpenSpace relies on ``hasOperators`` in shell-quote output; our
       bashlex wrapper strips redirects from the token list, so we
       check explicitly via :func:`extract_output_redirections` and
       compound-split below.)
    3. Match ``tokens[0..]`` against the merged COMMAND_ALLOWLIST
       (multi-word support via space-split).
    4. ``git ls-remote`` URL / variable rejection.
    5. Token-level ``$`` and ``{,}`` rejection (parser-differential defence).
    6. :func:`validate_flags` walk.
    7. Optional ``commandConfig.regex`` check.
    8. Optional backtick rejection.
    9. grep/rg newline rejection.
    10. ``additional_command_is_dangerous_callback``.
    """
    parse_result = try_parse_shell_command(command)
    if not parse_result.success:
        return False

    tokens = parse_result.tokens
    if not tokens:
        return False

    # Reject any redirection — OpenSpace's shell-quote returns them as operator
    # tokens which trigger ``hasOperators`` rejection at L1266-1269.
    # bashlex drops them from `tokens`, so we detect separately.
    redir_result = extract_output_redirections(command)
    if redir_result.redirections or redir_result.has_dangerous_redirection:
        return False

    # Reject compound operators (|, &&, ||, ;) — a single-command allowlist
    # entry must not span multiple commands.
    subcmds = split_command_segments(command)
    if len(subcmds) > 1:
        return False

    # Find matching command config (multi-word prefix match, longest wins).
    command_config: Optional[CommandConfig] = None
    command_tokens_count = 0
    allowlist = get_command_allowlist()
    # Sort multi-word patterns longest first so "git stash list" beats "git stash".
    for pattern in sorted(allowlist, key=lambda k: -len(k.split())):
        cmd_tokens = pattern.split(" ")
        if len(tokens) >= len(cmd_tokens) and all(
            tokens[i] == cmd_tokens[i] for i in range(len(cmd_tokens))
        ):
            command_config = allowlist[pattern]
            command_tokens_count = len(cmd_tokens)
            break

    if command_config is None:
        return False  # not in allowlist

    # ── git ls-remote URL guard ──
    if tokens[0] == "git" and len(tokens) > 1 and tokens[1] == "ls-remote":
        for t in tokens[2:]:
            if t and not t.startswith("-"):
                if "://" in t or "@" in t or ":" in t or "$" in t:
                    return False

    # ── `$` and `{,}`/{..} token rejection ──
    for t in tokens[command_tokens_count:]:
        if not t:
            continue
        if "$" in t:
            return False
        if "{" in t and ("," in t or ".." in t):
            return False

    # ── Flag validation ──
    opts: dict[str, Any] = {"commandName": tokens[0], "rawCommand": command}
    if tokens[0] == "xargs":
        opts["xargsTargetCommands"] = SAFE_TARGET_COMMANDS_FOR_XARGS
    if not validate_flags(tokens, command_tokens_count, command_config, opts):
        return False

    # ── Config-level regex ──
    regex = command_config.get("regex")
    if regex is not None and not regex.match(command):
        return False

    # ── Backtick guard for non-regex configs ──
    if regex is None and "`" in command:
        return False

    # ── grep/rg newline guard ──
    if regex is None and tokens[0] in ("rg", "grep"):
        if "\n" in command or "\r" in command:
            return False

    # ── Additional callback ──
    cb = command_config.get("additional_command_is_dangerous_callback")
    if cb is not None:
        args_after_cmd = list(tokens[command_tokens_count:])
        if cb(command, args_after_cmd):
            return False

    return True


# ─────────────────────────────────────────────────────────────────────
#  isCommandReadOnly — OpenSpace readOnlyValidation.ts L1678-1752
# ─────────────────────────────────────────────────────────────────────


_GIT_DASH_C_RE = re.compile(r"\s-c[\s=]")
_GIT_EXEC_PATH_RE = re.compile(r"\s--exec-path[\s=]")
_GIT_CONFIG_ENV_RE = re.compile(r"\s--config-env[\s=]")


def is_command_read_only(command: str) -> bool:
    """OpenSpace ``isCommandReadOnly`` — Layer 2+3 combined gate.

    Algorithm:
      1. Trim trailing ``2>&1``.
      2. :func:`contains_vulnerable_unc_path` → False.
      3. :func:`contains_unquoted_expansion` → False.
      4. :func:`is_command_safe_via_flag_parsing` → True.
      5. Iterate READONLY_COMMAND_REGEXES; on match, reject a few git
         sub-flag patterns (``-c``, ``--exec-path``, ``--config-env``).
    """
    if not command:
        return False

    test_command = command.strip()
    if test_command.endswith(" 2>&1"):
        test_command = test_command[:-5].strip()

    if contains_vulnerable_unc_path(test_command):
        return False

    if contains_unquoted_expansion(test_command):
        return False

    if is_command_safe_via_flag_parsing(test_command):
        return True

    for regex in READONLY_COMMAND_REGEXES:
        if regex.match(test_command):
            if "git" in test_command:
                if _GIT_DASH_C_RE.search(test_command):
                    return False
                if _GIT_EXEC_PATH_RE.search(test_command):
                    return False
                if _GIT_CONFIG_ENV_RE.search(test_command):
                    return False
            return True

    return False


# ─────────────────────────────────────────────────────────────────────
#  check_read_only_constraints
# ─────────────────────────────────────────────────────────────────────


def _passthrough(message: str) -> PermissionResult:
    return {"behavior": "passthrough", "message": message}


def _ask(message: str) -> PermissionResult:
    return {"behavior": "ask", "message": message}


def check_read_only_constraints(
    command: str, *, compound_command_has_cd: Optional[bool] = None,
    cwd: str | Path | None = None,
    original_cwd: str | Path | None = None,
    sandbox_enabled: bool | None = None,
    input_data: Optional[dict[str, Any]] = None,
) -> PermissionResult:
    """Return whether a bash command is read-only enough to bypass prompts.

    The caller passes the command string directly and may pass ``input_data`` for
    the ``updated_input`` returned on success.

    ``compound_command_has_cd`` defaults to ``None`` and is computed internally
    via :func:`shell_parser.command_has_any_cd`. Callers that already have the
    value may pass it to skip recomputation.
    """
    if compound_command_has_cd is None:
        compound_command_has_cd = command_has_any_cd(command)

    # 1. Parseability — bail out if we can't tokenise
    parse_result = try_parse_shell_command(command)
    if not parse_result.success:
        return _passthrough(
            "Command cannot be parsed, requires further permission checks"
        )

    # 2. Injection gate.
    injection_check = bash_command_passes_injection_gate(command)
    if injection_check.get("behavior") != "allow":
        return _passthrough(
            "Command is not read-only, requires further permission checks"
        )

    # 3. Windows UNC paths.
    if contains_vulnerable_unc_path(command):
        return _ask(
            "Command contains Windows UNC path that could be vulnerable to WebDAV attacks"
        )

    # 4. Has-git detection.
    has_git = command_has_any_git(command)

    # 5. cd + git compound.
    if compound_command_has_cd and has_git:
        return _passthrough(
            "Compound commands with cd and git require permission checks for enhanced security"
        )

    # 6. Bare-repo in cwd.
    if has_git and _is_current_directory_bare_git_repo(cwd):
        return _passthrough(
            "Git commands in directories with bare repository structure "
            "require permission checks for enhanced security"
        )

    # 7. Compound writes to git-internal paths.
    if has_git and _command_writes_to_git_internal_paths(command):
        return _passthrough(
            "Compound commands that create git internal files and run git "
            "require permission checks for enhanced security"
        )

    # 8. Sandbox + cwd drift.
    if (
        has_git
        and (_sandbox_enabled(cwd) if sandbox_enabled is None else sandbox_enabled)
        and _cwd_differs_from_original(cwd, original_cwd)
    ):
        return _passthrough(
            "Git commands outside the original working directory require "
            "permission checks when sandbox is enabled"
        )

    # 9. All subcommands read-only?
    all_ro = True
    for subcmd in split_command_segments(command):
        if bash_command_passes_injection_gate(subcmd).get("behavior") != "allow":
            all_ro = False
            break
        if not is_command_read_only(subcmd):
            all_ro = False
            break

    if all_ro:
        result: PermissionResult = {"behavior": "allow"}
        if input_data is not None:
            result["updated_input"] = dict(input_data)
        return result

    return _passthrough(
        "Command is not read-only, requires further permission checks"
    )
