"""BashTool path-constraint validation.

This module owns the per-subcommand path extraction and validation used by
:func:`bash_permissions.bash_tool_has_permission`. The canonical 34-command
:data:`PATH_EXTRACTORS` / :data:`COMMAND_OPERATION_TYPE` tables are reused
from :mod:`openspace.grounding.core.security.path_validation`; this module
focuses on the permission decision layer.

The lower-level path extractor tables live in
:mod:`openspace.grounding.core.security.path_validation`. This module adds the
permission decision layer: dangerous removal hard-denies, working-directory
checks, output-redirection checks, and rule suggestions for additional working
directories.

Tree-sitter-specific paths are intentionally not required. Every command goes
through :func:`validate_single_path_command`, and redirections come from the
shared shell parser.
"""
from __future__ import annotations

import os
import os.path
import re
from typing import Any, Callable, List, Optional, Tuple

from ..security.path_validation import (
    COMMAND_OPERATION_TYPE,
    PATH_EXTRACTORS,
    SUPPORTED_PATH_COMMANDS,
    FileOperationType,
    expand_tilde,
    is_dangerous_removal_path,
    strip_wrappers_from_argv as _canonical_strip_wrappers_from_argv,
)
from ..security.sed_validation import sed_command_is_allowed_by_allowlist
from ..security.shell_parser import (
    extract_output_redirections as _extract_output_redirections,
    split_command_segments,
    try_parse_shell_command,
)
from .types import (
    AddDirectoriesUpdate,
    AdditionalWorkingDirectory,
    DecisionReasonOther,
    DecisionReasonRule,
    DecisionReasonSafetyCheck,
    PermissionAsk,
    PermissionDeny,
    PermissionPassthrough,
    PermissionResult,
    PermissionRule,
    PermissionRuleValue,
    PermissionUpdate,
    ToolPermissionContext,
)


__all__ = [
    # OpenSpace re-exports
    "PATH_EXTRACTORS",
    "COMMAND_OPERATION_TYPE",
    "SUPPORTED_PATH_COMMANDS",
    "ACTION_VERBS",
    # strip_wrappers (local + canonical)
    "strip_wrappers_from_argv",
    # validation entry points
    "check_path_constraints",
    "create_path_checker",
    "validate_command_paths",
    "validate_single_path_command",
    "validate_output_redirections",
    "check_dangerous_removal_paths",
    # helpers
    "validate_path",
    "all_working_directories",
    "format_directory_list",
    "get_directory_for_path",
    "is_bash_tool_name",
]


# ════════════════════════════════════════════════════════════════════════
# §1  ACTION_VERBS and COMMAND_VALIDATOR
# ════════════════════════════════════════════════════════════════════════


# OpenSpace ``ACTION_VERBS`` (L513-550) — human-readable verb per path command,
# embedded in "was blocked. For security, OpenSpace may only …" asks.
ACTION_VERBS: dict[str, str] = {
    "cd": "change directories to",
    "ls": "list files in",
    "find": "search files in",
    "mkdir": "create directories in",
    "touch": "create or modify files in",
    "rm": "remove files from",
    "rmdir": "remove directories from",
    "mv": "move files to/from",
    "cp": "copy files to/from",
    "cat": "concatenate files from",
    "head": "read the beginning of files from",
    "tail": "read the end of files from",
    "sort": "sort contents of files from",
    "uniq": "filter duplicate lines from files in",
    "wc": "count lines/words/bytes in files from",
    "cut": "extract columns from files in",
    "paste": "merge files from",
    "column": "format files from",
    "tr": "transform text from files in",
    "file": "examine file types in",
    "stat": "read file stats from",
    "diff": "compare files from",
    "awk": "process text from files in",
    "strings": "extract strings from files in",
    "hexdump": "display hex dump of files from",
    "od": "display octal dump of files from",
    "base64": "encode/decode files from",
    "nl": "number lines in files from",
    "grep": "search for patterns in files from",
    "rg": "search for patterns in files from",
    "sed": "edit files in",
    "git": "access files with git from",
    "jq": "process JSON from files in",
    "sha256sum": "compute SHA-256 checksums for files in",
    "sha1sum": "compute SHA-1 checksums for files in",
    "md5sum": "compute MD5 checksums for files in",
}


# OpenSpace ``COMMAND_VALIDATOR`` (L596-601) — extra flag-safety checks beyond
# the generic ``filter_out_flags`` walker. ``mv``/``cp`` block *all* flags
# because flags like ``--target-directory=PATH`` bypass the positional
# extractor.
COMMAND_VALIDATOR: dict[str, Callable[[List[str]], bool]] = {
    "mv": lambda args: not any(a and a.startswith("-") for a in args),
    "cp": lambda args: not any(a and a.startswith("-") for a in args),
}


# OpenSpace permission-rule tool-name match (OpenSpace uses ``BashTool.name``).
# The OS canonical bash tool name is lowercase ``bash``; we also accept
# ``Bash`` for OpenSpace-authored rules (see types.parse_rule_value normalization).
_BASH_TOOL_NAMES = frozenset({"bash", "Bash"})


def is_bash_tool_name(tool_name: str) -> bool:
    """True if *tool_name* identifies the Bash tool (OS or OpenSpace naming)."""
    return tool_name in _BASH_TOOL_NAMES


# ════════════════════════════════════════════════════════════════════════
# §2  strip_wrappers_from_argv
# ════════════════════════════════════════════════════════════════════════


# OpenSpace pathValidation.ts ships the *canonical* stripWrappersFromArgv; the
# bashPermissions.ts copy at L678 is narrower and marked DEAD CODE. OS
# re-exports the canonical one under the same name, same as OpenSpace's prod
# consumer (see OpenSpace comment at L1152-1171).
def strip_wrappers_from_argv(argv: List[str]) -> List[str]:
    """OpenSpace ``stripWrappersFromArgv`` (L1263-1303).

    Iteratively strip ``time`` / ``nohup`` / ``timeout`` / ``nice`` /
    ``stdbuf`` / ``env`` wrapper prefixes from an argv list. Delegates to
    :func:`core.security.path_validation.strip_wrappers_from_argv` which
    already implements the full 6-wrapper walk.
    """
    return _canonical_strip_wrappers_from_argv(argv)


# ════════════════════════════════════════════════════════════════════════
# §3  validate_path — simplified port of OpenSpace utils/permissions/pathValidation.validatePath
# ════════════════════════════════════════════════════════════════════════


# OpenSpace ``SENSITIVE_PATH_PATTERNS`` — paths under these roots require explicit
# approval regardless of allow rules.  Ported from OpenSpace
# ``utils/permissions/pathValidation.ts`` sensitive-path check (see OpenSpace
# ``isClaudeConfigFilePath`` for the canonical list).  OS retains the
# same set plus ``.openspace/`` for the renamed config directory.
_SENSITIVE_BASENAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".ssh",
        ".aws",
        ".gnupg",
        ".docker",
        ".kube",
        ".claude",
        ".openspace",
    }
)


# OpenSpace block-device pattern — writes to ``/dev/sd*`` / ``/dev/nvme*`` /
# ``/dev/disk*`` are irrecoverable and always denied.
_BLOCK_DEVICE_RE = re.compile(
    r"^/dev/(sd[a-z]\d*|nvme\d+n\d+(?:p\d+)?|disk\d+(?:s\d+)?|hd[a-z]\d*|xvd[a-z]\d*)$"
)


def all_working_directories(
    context: ToolPermissionContext,
) -> List[str]:
    """OpenSpace ``allWorkingDirectories`` (utils/permissions/filesystem.ts).

    Returns every working directory the current permission context
    authorises, as absolute path strings.
    """
    return [
        wd.path if isinstance(wd, AdditionalWorkingDirectory) else str(wd)
        for wd in context.additional_working_directories.values()
    ]


def format_directory_list(dirs: List[str]) -> str:
    """OpenSpace ``formatDirectoryList`` (utils/permissions/pathValidation.ts)."""
    if not dirs:
        return "(none)"
    if len(dirs) == 1:
        return dirs[0]
    if len(dirs) == 2:
        return f"{dirs[0]} and {dirs[1]}"
    return ", ".join(dirs[:-1]) + f", and {dirs[-1]}"


def get_directory_for_path(path: str) -> str:
    """OpenSpace ``getDirectoryForPath`` (utils/path.ts).

    Returns the directory containing *path*. If *path* is already a
    directory (ends with ``/``), returns *path* unchanged.
    """
    if path.endswith("/"):
        return path
    return os.path.dirname(path) or path


def _resolve_path(path: str, cwd: str) -> str:
    """Expand tilde + resolve to absolute. Does NOT resolve symlinks
    (matches OpenSpace — see :func:`check_dangerous_removal_paths` note).
    """
    cleaned = path.strip()
    # Strip surrounding single/double quotes (OpenSpace does this inline).
    if len(cleaned) >= 2 and cleaned[0] in "\"'" and cleaned[-1] == cleaned[0]:
        cleaned = cleaned[1:-1]
    expanded = expand_tilde(cleaned)
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(cwd, expanded))


def _path_inside(candidate: str, allowed: str) -> bool:
    """True iff *candidate* is *allowed* or is a descendant thereof.

    OpenSpace uses ``pathInAllowedWorkingPath`` with a prefix-plus-separator
    check to avoid ``/foo`` matching ``/foobar``.
    """
    c = os.path.normpath(candidate)
    a = os.path.normpath(allowed)
    if c == a:
        return True
    return c.startswith(a.rstrip(os.sep) + os.sep)


def _is_sensitive_path(abs_path: str) -> bool:
    """True if *abs_path* traverses a sensitive basename (``.git/``,
    ``.ssh/``, ``.openspace/`` etc.)."""
    parts = abs_path.replace("\\", "/").split("/")
    return any(p in _SENSITIVE_BASENAMES for p in parts)


def _is_block_device_path(abs_path: str) -> bool:
    return bool(_BLOCK_DEVICE_RE.match(abs_path.replace("\\", "/")))


def _matching_rule_for_path(
    context: ToolPermissionContext,
    tool_name: str,
    abs_path: str,
) -> Optional[PermissionRule]:
    """Return the first matching deny rule whose ``rule_content`` glob
    covers *abs_path*.  Minimal subset of OpenSpace's filesystem-rule matcher —
    just enough for ``checkPathConstraints`` to surface ``deny`` when a
    user installed a ``Read(/etc/**)`` deny rule.
    """
    rules_by_src = context.always_deny_rules
    if not rules_by_src:
        return None

    for source, rule_contents in rules_by_src.items():
        for rc in rule_contents:
            # Rules come formatted as "tool(content)"; also accept plain
            # content strings from older persisted settings.
            try:
                from .types import parse_rule_value

                rv = parse_rule_value(rc)
            except Exception:
                continue
            if rv.tool_name not in (tool_name, "bash", "Bash", "read", "edit", "write"):
                continue
            if rv.rule_content is None:
                # Tool-wide deny is handled by the rule matcher, so do not
                # reinterpret it as a path-specific deny here.
                # matching path-level.
                continue
            if _glob_match(rv.rule_content, abs_path):
                return PermissionRule(
                    source=source,
                    rule_behavior="deny",
                    rule_value=rv,
                )
    return None


def _glob_match(pattern: str, text: str) -> bool:
    """Minimal glob matcher supporting ``*`` and ``**``.

    OpenSpace uses ``minimatch``; OS uses a reduced form that handles the
    common cases ``**/*.py``, ``/tmp/**``, ``*.md``.
    """
    # Translate glob → regex
    regex = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                regex.append(".*")
                i += 2
                if i < len(pattern) and pattern[i] == "/":
                    i += 1
            else:
                regex.append("[^/]*")
                i += 1
        elif c == "?":
            regex.append("[^/]")
            i += 1
        elif c in r".+()[]^$|\\":
            regex.append("\\" + c)
            i += 1
        else:
            regex.append(c)
            i += 1
    try:
        return re.fullmatch("".join(regex), text) is not None
    except re.error:
        return False


class _ValidatePathResult:
    """OpenSpace ``validatePath`` return shape."""

    __slots__ = ("allowed", "resolved_path", "decision_reason")

    def __init__(
        self,
        allowed: bool,
        resolved_path: str,
        decision_reason: Optional[Any] = None,
    ) -> None:
        self.allowed = allowed
        self.resolved_path = resolved_path
        self.decision_reason = decision_reason


def validate_path(
    path: str,
    cwd: str,
    context: ToolPermissionContext,
    operation_type: FileOperationType,
) -> _ValidatePathResult:
    """OpenSpace ``validatePath`` (utils/permissions/pathValidation.ts).

    Resolve *path* then check:
      1. Deny-rule match (any ``Read``/``Bash``/``Edit``/``Write`` rule
         whose glob covers the resolved path) → ``decision_reason.rule``.
      2. Block-device write (``/dev/sda`` etc.) for write/create ops →
         safety check reason, not allowed.
      3. Sensitive path (``.git/``/``.ssh/``/``.openspace/``) traversal
         for write ops → safety check reason, not allowed.
      4. Path must sit inside cwd or an :class:`AdditionalWorkingDirectory`.

    Returns :class:`_ValidatePathResult` mirroring OpenSpace's tuple.
    """
    resolved = _resolve_path(path, cwd)

    # §1. Deny rules
    deny_rule = _matching_rule_for_path(context, "read", resolved)
    if deny_rule is not None:
        return _ValidatePathResult(
            allowed=False,
            resolved_path=resolved,
            decision_reason=DecisionReasonRule(rule=deny_rule),
        )

    # §2. Block device — only for write/create (reads of /dev/sda make
    # no practical sense but are not destructive).
    if operation_type in ("write", "create") and _is_block_device_path(resolved):
        return _ValidatePathResult(
            allowed=False,
            resolved_path=resolved,
            decision_reason=DecisionReasonSafetyCheck(
                reason=(
                    f"Write to block device {resolved!r} is blocked — "
                    "this would overwrite raw disk and is irrecoverable."
                ),
                classifier_approvable=False,
            ),
        )

    # §3. Sensitive-path traversal for write ops
    if operation_type in ("write", "create") and _is_sensitive_path(resolved):
        return _ValidatePathResult(
            allowed=False,
            resolved_path=resolved,
            decision_reason=DecisionReasonSafetyCheck(
                reason=(
                    f"Write to sensitive path {resolved!r} requires approval."
                ),
                classifier_approvable=True,
            ),
        )

    # §4. Working-directory containment
    allowed_dirs = [cwd] + all_working_directories(context)
    for d in allowed_dirs:
        if _path_inside(resolved, os.path.normpath(os.path.expanduser(d))):
            return _ValidatePathResult(allowed=True, resolved_path=resolved)

    # Explicit bypass mode permits ordinary paths outside the workspace.
    # Deny rules, block devices, sensitive writes, and dangerous removals are
    # handled above (or by the caller) and remain terminal.
    if (
        context.mode == "bypassPermissions"
        and context.is_bypass_permissions_mode_available
    ):
        return _ValidatePathResult(allowed=True, resolved_path=resolved)

    return _ValidatePathResult(
        allowed=False,
        resolved_path=resolved,
        decision_reason=DecisionReasonSafetyCheck(
            reason=(
                f"Path {resolved!r} is outside the allowed working directories."
            ),
            classifier_approvable=True,
        ),
    )


# ════════════════════════════════════════════════════════════════════════
# §4  check_dangerous_removal_paths
# ════════════════════════════════════════════════════════════════════════


def check_dangerous_removal_paths(
    command: str,
    args: List[str],
    cwd: str,
) -> PermissionResult:
    """Hard-deny catastrophic rm/rmdir targets.

    Truly catastrophic patterns such as ``rm /``, ``rm -rf /``, ``rm /*``,
    ``rm ~``, and ``rm C:/`` are denied instead of sent through approval.
    """
    if command not in ("rm", "rmdir"):
        return PermissionPassthrough(
            message=f"No dangerous removal check for {command}"
        )

    extractor = PATH_EXTRACTORS.get(command)
    if extractor is None:
        return PermissionPassthrough(
            message=f"No extractor for {command}"
        )

    for raw_path in extractor(args):
        # Implementation: `path.replace(/^['"]|['"]$/g, '')` + expandTilde. We do the
        # same in `_resolve_path` but need pre-abs for isDangerousRemovalPath.
        cleaned = raw_path.strip()
        if len(cleaned) >= 2 and cleaned[0] in "\"'" and cleaned[-1] == cleaned[0]:
            cleaned = cleaned[1:-1]
        expanded = expand_tilde(cleaned)
        absolute = (
            os.path.normpath(expanded)
            if os.path.isabs(expanded)
            else os.path.normpath(os.path.join(cwd, expanded))
        )
        if is_dangerous_removal_path(absolute):
            reason = (
                f"Dangerous {command} operation detected: {absolute!r}\n\n"
                "This command would remove a critical system directory "
                "and cannot be auto-allowed by permission rules."
            )
            return PermissionDeny(
                message=reason,
                decision_reason=DecisionReasonSafetyCheck(
                    reason=reason,
                    classifier_approvable=False,
                ),
            )

    return PermissionPassthrough(
        message=f"No dangerous removals detected for {command} command"
    )


# ════════════════════════════════════════════════════════════════════════
# §5  validate_command_paths
# ════════════════════════════════════════════════════════════════════════


def validate_command_paths(
    command: str,
    args: List[str],
    cwd: str,
    context: ToolPermissionContext,
    compound_command_has_cd: bool = False,
    operation_type_override: Optional[FileOperationType] = None,
) -> PermissionResult:
    """OpenSpace ``validateCommandPaths`` (L603-701)."""
    extractor = PATH_EXTRACTORS.get(command)
    if extractor is None:
        return PermissionPassthrough(
            message=f"Command {command!r} has no path extractor"
        )

    paths = extractor(args)
    operation_type = operation_type_override or COMMAND_OPERATION_TYPE[command]

    # §COMMAND_VALIDATOR — mv/cp with any flag → ask.
    validator = COMMAND_VALIDATOR.get(command)
    if validator is not None and not validator(args):
        reason = (
            f"{command} with flags requires manual approval to ensure "
            "path safety. For security, the tool cannot automatically "
            f"validate {command} commands that use flags, as some flags "
            "like --target-directory=PATH can bypass path validation."
        )
        return PermissionAsk(
            message=reason,
            decision_reason=DecisionReasonOther(reason=reason),
        )

    # §cd + write compound.
    if compound_command_has_cd and operation_type != "read":
        reason = (
            "Commands that change directories and perform write operations "
            "require explicit approval to ensure paths are evaluated correctly. "
            "For security, the tool cannot automatically determine the final "
            "working directory when 'cd' is used in compound commands."
        )
        return PermissionAsk(
            message=reason,
            decision_reason=DecisionReasonOther(
                reason=(
                    "Compound command contains cd with write operation — "
                    "manual approval required to prevent path resolution bypass"
                )
            ),
        )

    # §Per-path validation.
    for raw_path in paths:
        result = validate_path(raw_path, cwd, context, operation_type)
        if result.allowed:
            continue

        working_dirs = all_working_directories(context)
        dir_list_str = format_directory_list(working_dirs or [cwd])
        dr = result.decision_reason

        if isinstance(dr, (DecisionReasonOther, DecisionReasonSafetyCheck)):
            message = dr.reason
        else:
            message = (
                f"{command} in {result.resolved_path!r} was blocked. "
                f"For security, the tool may only "
                f"{ACTION_VERBS.get(command, 'access files in')} "
                f"the allowed working directories for this session: {dir_list_str}."
            )

        if isinstance(dr, DecisionReasonRule):
            return PermissionDeny(
                message=message,
                decision_reason=dr,
            )

        return PermissionAsk(
            message=message,
            blocked_path=result.resolved_path,
            decision_reason=dr or DecisionReasonOther(reason=message),
        )

    return PermissionPassthrough(
        message=f"Path validation passed for {command} command"
    )


# ════════════════════════════════════════════════════════════════════════
# §6  create_path_checker
# ════════════════════════════════════════════════════════════════════════


PathCheckerFn = Callable[
    [List[str], str, ToolPermissionContext, bool],
    PermissionResult,
]


def create_path_checker(
    command: str,
    operation_type_override: Optional[FileOperationType] = None,
) -> PathCheckerFn:
    """OpenSpace ``createPathChecker`` (L703-784).

    Returns a closure that:
      1. Runs :func:`validate_command_paths`.
      2. If that returns ``deny`` → returns as-is (explicit rule wins).
      3. For ``rm``/``rmdir`` runs :func:`check_dangerous_removal_paths`
         on top.
      4. On ``ask``, attaches :class:`AddDirectoriesUpdate` and/or
         ``setMode: acceptEdits`` suggestions.
    """

    def _checker(
        args: List[str],
        cwd: str,
        context: ToolPermissionContext,
        compound_command_has_cd: bool = False,
    ) -> PermissionResult:
        result = validate_command_paths(
            command=command,
            args=args,
            cwd=cwd,
            context=context,
            compound_command_has_cd=compound_command_has_cd,
            operation_type_override=operation_type_override,
        )

        if isinstance(result, PermissionDeny):
            return result

        # Dangerous-rm check.
        if command in ("rm", "rmdir"):
            danger = check_dangerous_removal_paths(command, args, cwd)
            if not isinstance(danger, PermissionPassthrough):
                return danger

        if isinstance(result, PermissionPassthrough):
            return result

        if isinstance(result, PermissionAsk):
            operation_type = operation_type_override or COMMAND_OPERATION_TYPE[command]
            suggestions: List[PermissionUpdate] = []

            if result.blocked_path:
                dir_path = get_directory_for_path(result.blocked_path)
                if operation_type == "read":
                    # OpenSpace ``createReadRuleSuggestion`` — only if dir exists.
                    try:
                        dir_exists = os.path.isdir(dir_path)
                    except OSError:
                        dir_exists = False
                    if dir_exists:
                        from .types import AddRulesUpdate

                        suggestions.append(
                            AddRulesUpdate(
                                destination="session",
                                rules=(
                                    PermissionRuleValue(
                                        tool_name="read",
                                        rule_content=os.path.join(dir_path, "**"),
                                    ),
                                ),
                                behavior="allow",
                            )
                        )
                else:
                    suggestions.append(
                        AddDirectoriesUpdate(
                            destination="session",
                            directories=(dir_path,),
                        )
                    )

            if operation_type in ("write", "create"):
                from .types import SetModeUpdate

                suggestions.append(
                    SetModeUpdate(
                        destination="session",
                        mode="acceptEdits",
                    )
                )

            return PermissionAsk(
                message=result.message,
                decision_reason=result.decision_reason,
                updated_input=result.updated_input,
                suggestions=tuple(suggestions) if suggestions else None,
                blocked_path=result.blocked_path,
                metadata=result.metadata,
            )

        return result

    return _checker


# ════════════════════════════════════════════════════════════════════════
# §7  validate_single_path_command
# ════════════════════════════════════════════════════════════════════════


def validate_single_path_command(
    cmd: str,
    cwd: str,
    context: ToolPermissionContext,
    compound_command_has_cd: bool = False,
) -> PermissionResult:
    """OpenSpace ``validateSinglePathCommand`` (L834-880)."""
    from .bash_permissions import strip_safe_wrappers  # local import avoids cycle

    stripped_cmd = strip_safe_wrappers(cmd)
    parse_result = try_parse_shell_command(stripped_cmd)
    if not parse_result.success:
        return PermissionPassthrough(
            message="Empty or unparseable command — no paths to validate"
        )

    extracted_args = [t for t in parse_result.tokens if isinstance(t, str)]
    if not extracted_args:
        return PermissionPassthrough(
            message="Empty command — no paths to validate"
        )

    base_cmd, *args = extracted_args
    if base_cmd not in SUPPORTED_PATH_COMMANDS:
        return PermissionPassthrough(
            message=f"Command {base_cmd!r} is not a path-restricted command"
        )

    # §sed read-only override.
    operation_type_override: Optional[FileOperationType] = None
    if base_cmd == "sed" and sed_command_is_allowed_by_allowlist(stripped_cmd):
        operation_type_override = "read"

    checker = create_path_checker(base_cmd, operation_type_override)
    return checker(list(args), cwd, context, compound_command_has_cd)


# ════════════════════════════════════════════════════════════════════════
# §8  validate_output_redirections
# ════════════════════════════════════════════════════════════════════════


def validate_output_redirections(
    redirections: List[Tuple[str, str]],
    cwd: str,
    context: ToolPermissionContext,
    compound_command_has_cd: bool = False,
) -> PermissionResult:
    """OpenSpace ``validateOutputRedirections`` (L924-1003).

    *redirections* is a list of ``(target, operator)`` tuples where
    operator is ``'>'`` or ``'>>'``.
    """
    if compound_command_has_cd and redirections:
        reason = (
            "Commands that change directories and write via output redirection "
            "require explicit approval to ensure paths are evaluated correctly."
        )
        return PermissionAsk(
            message=reason,
            decision_reason=DecisionReasonOther(
                reason=(
                    "Compound command contains cd with output redirection — "
                    "manual approval required to prevent path resolution bypass"
                )
            ),
        )

    for target, _operator in redirections:
        if target == "/dev/null":
            continue

        result = validate_path(target, cwd, context, "create")
        if result.allowed:
            continue

        working_dirs = all_working_directories(context) or [cwd]
        dir_list_str = format_directory_list(working_dirs)
        dr = result.decision_reason

        if isinstance(dr, DecisionReasonRule):
            message = (
                f"Output redirection to {result.resolved_path!r} "
                "was blocked by a deny rule."
            )
            return PermissionDeny(
                message=message,
                decision_reason=dr,
            )

        if isinstance(dr, (DecisionReasonOther, DecisionReasonSafetyCheck)):
            message = dr.reason
        else:
            message = (
                f"Output redirection to {result.resolved_path!r} was blocked. "
                f"For security, the tool may only write to files in the "
                f"allowed working directories for this session: {dir_list_str}."
            )

        return PermissionAsk(
            message=message,
            blocked_path=result.resolved_path,
            decision_reason=dr or DecisionReasonOther(reason=message),
            suggestions=(
                AddDirectoriesUpdate(
                    destination="session",
                    directories=(get_directory_for_path(result.resolved_path),),
                ),
            ),
        )

    return PermissionPassthrough(
        message="No unsafe redirections found"
    )


# ════════════════════════════════════════════════════════════════════════
# §9  check_path_constraints
# ════════════════════════════════════════════════════════════════════════


# OpenSpace process-substitution detector (L1028): ``>(cmd)`` / ``<(cmd)`` /
# combined ``>>>(cmd)`` patterns.
_PROCESS_SUBSTITUTION_RE = re.compile(r">>\s*>\s*\(|>\s*>\s*\(|<\s*\(")


def check_path_constraints(
    command: str,
    cwd: str,
    context: ToolPermissionContext,
    compound_command_has_cd: bool = False,
) -> PermissionResult:
    """OpenSpace ``checkPathConstraints`` (L1013-1109).

    Validation order (1:1 with OpenSpace):

    1. Process substitution detected → ask.
    2. Extract output redirections; if any target has shell expansion →
       ask.
    3. Validate redirection targets → bail on deny/ask.
    4. For each subcommand (``splitCommandSegments``), run
       :func:`validate_single_path_command`; bail on deny/ask.
    5. Fall through to :class:`PermissionPassthrough`.
    """
    # §1
    if _PROCESS_SUBSTITUTION_RE.search(command):
        reason = (
            "Process substitution (>(...) or <(...)) can execute arbitrary "
            "commands and requires manual approval"
        )
        return PermissionAsk(
            message=reason,
            decision_reason=DecisionReasonOther(
                reason="Process substitution requires manual approval"
            ),
        )

    # §2-3
    redir = _extract_output_redirections(command)
    if redir.has_dangerous_redirection:
        reason = "Shell expansion syntax in paths requires manual approval"
        return PermissionAsk(
            message=reason,
            decision_reason=DecisionReasonOther(reason=reason),
        )

    redirection_tuples = [(r.target, r.operator) for r in redir.redirections]
    redir_result = validate_output_redirections(
        redirection_tuples,
        cwd,
        context,
        compound_command_has_cd,
    )
    if not isinstance(redir_result, PermissionPassthrough):
        return redir_result

    # §4
    for sub in split_command_segments(command):
        sub_result = validate_single_path_command(
            sub,
            cwd,
            context,
            compound_command_has_cd,
        )
        if isinstance(sub_result, (PermissionAsk, PermissionDeny)):
            return sub_result

    # §5
    return PermissionPassthrough(
        message="All path commands validated successfully"
    )
