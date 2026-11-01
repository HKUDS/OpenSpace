"""Filesystem permissions.

The module owns dangerous-path checks, working-directory checks, internal-path
rules, and permission suggestions. Runtime-specific internal path checks are
gated behind ``_INTERNAL_PATH_PREDICATES`` so callers can wire concrete
implementations without touching the core control flow.

The local runtime does not invoke an LLM classifier from filesystem permission
checks. ``classifier_approvable`` is still surfaced on
:class:`DecisionReasonSafetyCheck` so callers can render or persist the same
decision shape without changing the permission result.
"""
from __future__ import annotations

import fnmatch
import os
import posixpath
import re
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

try:
    # Re-use the UNC detector the bash classifier already owns so the
    # two stay aligned.
    from ..security.flag_validation import (
        contains_vulnerable_unc_path as _contains_vulnerable_unc_path,
    )
except Exception:  # pragma: no cover - safety net for circular imports
    def _contains_vulnerable_unc_path(
        path_or_command: str, *, force_check: bool = False
    ) -> bool:
        return False

from .types import (
    AddDirectoriesUpdate,
    AddRulesUpdate,
    DecisionReasonMode,
    DecisionReasonOther,
    DecisionReasonRule,
    DecisionReasonSafetyCheck,
    DecisionReasonWorkingDir,
    PermissionAllow,
    PermissionAsk,
    PermissionBehavior,
    PermissionDecisionReason,
    PermissionDeny,
    PermissionPassthrough,
    PermissionResult,
    PermissionRule,
    PermissionRuleSource,
    PermissionRuleValue,
    PermissionUpdate,
    ToolPermissionContext,
    parse_rule_value,
)

__all__ = [
    # constants
    "DANGEROUS_FILES",
    "DANGEROUS_DIRECTORIES",
    "SENSITIVE_FILES",
    "SENSITIVE_DIRECTORY_SEGMENTS",
    "SENSITIVE_FILE_GLOBS",
    "FILE_EDIT_TOOL_NAME",
    "FILE_WRITE_TOOL_NAME",
    "FILE_READ_TOOL_NAME",
    "EDIT_RULE_TOOL_NAMES",
    "READ_RULE_TOOL_NAMES",
    # path helpers
    "to_posix_path",
    "normalize_case_for_comparison",
    "expand_path",
    "contains_path_traversal",
    "relative_path",
    "has_suspicious_windows_path_pattern",
    # settings & sensitive-path predicates
    "is_openspace_settings_path",
    "is_sensitive_path",
    "is_dangerous_file_path_to_auto_edit",
    # working directories
    "all_working_directories",
    "path_in_working_path",
    "path_in_allowed_working_path",
    # rule matching
    "matching_rule_for_input",
    "normalize_patterns_to_path",
    "get_file_read_ignore_patterns",
    # safety & suggestions
    "check_path_safety_for_auto_edit",
    "generate_suggestions",
    # internal path carve-outs
    "check_editable_internal_path",
    "check_readable_internal_path",
    "register_internal_path_predicate",
    # main entry points
    "check_read_permission_for_tool",
    "check_write_permission_for_tool",
]


# ════════════════════════════════════════════════════════════════════════
# §0  Tool-name constants (OpenSpace canonicalisation, os lowercase rename)
# ════════════════════════════════════════════════════════════════════════

# OpenSpace ``FILE_EDIT_TOOL_NAME = 'Edit'`` — the rule tool_name used when
# storing/matching edit permissions.  OS uses lowercase per runtime constraints.6.
FILE_EDIT_TOOL_NAME: str = "edit"
FILE_WRITE_TOOL_NAME: str = "write"
FILE_READ_TOOL_NAME: str = "read"

# OS introduces ``write`` as a distinct tool alongside ``edit`` (OpenSpace had
# only ``Edit``).  When matching edit-category rules we honour both so
# a user-authored ``write(/tmp/**)`` rule still takes effect.  Read-side
# we also accept ``grep``/``glob``/``ls`` so operator-facing "grant read
# to this folder" rules apply across the read-family tools uniformly.
EDIT_RULE_TOOL_NAMES: Tuple[str, ...] = (FILE_EDIT_TOOL_NAME, FILE_WRITE_TOOL_NAME)
READ_RULE_TOOL_NAMES: Tuple[str, ...] = (
    FILE_READ_TOOL_NAME,
    "grep",
    "glob",
    "ls",
)


# ════════════════════════════════════════════════════════════════════════
# §1  Dangerous & sensitive path constants  (OpenSpace filesystem.ts L57-L79)
# ════════════════════════════════════════════════════════════════════════

# OpenSpace ``DANGEROUS_FILES``. ``.claude.json`` is renamed ``.openspace.json``
# per the OpenSpace→os .claude/.openspace rebrand.  Everything else is unchanged.
DANGEROUS_FILES: FrozenSet[str] = frozenset(
    {
        ".gitconfig",
        ".gitmodules",
        ".bashrc",
        ".bash_profile",
        ".zshrc",
        ".zprofile",
        ".profile",
        ".ripgreprc",
        ".mcp.json",
        ".openspace.json",  # Implementation: '.claude.json'
    }
)

# OpenSpace ``DANGEROUS_DIRECTORIES``. ``.claude`` → ``.openspace``.
DANGEROUS_DIRECTORIES: FrozenSet[str] = frozenset(
    {
        ".git",
        ".vscode",
        ".idea",
        ".openspace",  # Implementation: '.claude'
    }
)

# Extended sensitive files — force an ``ask`` even when the user has
# enabled ``bypassPermissions`` / ``acceptEdits``.  Matched as the
# *final* path component of a path (case-insensitive).  This is OS-
# specific hardening requested by the user contract; OpenSpace lets the
# dangerous-files set cover this.
SENSITIVE_FILES: FrozenSet[str] = frozenset(
    {
        ".env",
        ".mcp.json",
        ".bashrc",
        ".zshrc",
        ".bash_profile",
        ".zprofile",
        ".profile",
        ".gitconfig",
        ".credential",
        ".credentials",
        ".secret",
        ".secrets",
        ".token",
        ".tokens",
        ".api_key",
        ".apikey",
        ".envrc",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "private_key",
        "private-key",
        "key",
        "certificate",
        "cert",
        "credential",
        "credentials",
        "credentials.json",
        "secret",
        "secrets",
        "token",
        "tokens",
        "token.json",
        "api_key",
        "apikey",
    }
)

# Glob-shaped sensitive filenames (e.g. ``.env.production``).  Stored as
# ``fnmatch``-style patterns evaluated case-insensitively against the
# final path component.
SENSITIVE_FILE_GLOBS: Tuple[str, ...] = (
    ".env.*",
    "*.pem",
    "*.key",
    "*.crt",
    "*.cert",
    "*.cer",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "private_key.*",
    "private-key.*",
    "*_private_key.*",
    "*-private-key.*",
    "*_key.*",
    "*-key.*",
    "certificate.*",
    "cert.*",
    "credential.*",
    "credentials.*",
    "secret.*",
    "secrets.*",
    "token.*",
    "tokens.*",
    "api_key.*",
    "apikey.*",
)

# Extended sensitive directory segments.  If *any* path component
# matches, force ``ask``.
SENSITIVE_DIRECTORY_SEGMENTS: FrozenSet[str] = frozenset(
    {
        ".git",
        ".openspace",
        ".ssh",
        ".aws",
        ".gcloud",
        ".azure",
    }
)


# ════════════════════════════════════════════════════════════════════════
# §2  Path normalisation helpers  (OpenSpace filesystem.ts L90-L192 + path.ts)
# ════════════════════════════════════════════════════════════════════════


def normalize_case_for_comparison(path: str) -> str:
    """OpenSpace ``normalizeCaseForComparison`` (filesystem.ts L90).

    Always lowercase regardless of platform to harden against
    case-insensitive filesystems (``.cLauDe/SeTtings.LoCaL.json``).
    """
    return path.lower()


def to_posix_path(path: str) -> str:
    """OpenSpace ``toPosixPath`` (filesystem.ts L187).

    Convert backslashes to forward slashes.  On Linux/macOS this is a
    no-op; on Windows it matches OpenSpace's ``windowsPathToPosixPath``.
    """
    if not path:
        return path
    # Drop ``\\?\`` / ``\\.\`` long-path prefixes so they don't leak into
    # gitignore-style pattern matching.  OpenSpace's ``windowsPathToPosixPath``
    # does the same.
    stripped = path
    for prefix in ("\\\\?\\", "\\\\.\\"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    return stripped.replace("\\", "/")


def expand_path(path: str) -> str:
    """OpenSpace ``expandPath`` — expand ``~`` and make absolute.

    OpenSpace normalises case on case-insensitive filesystems elsewhere; here
    we only resolve the tilde and make the path absolute.  We avoid
    ``os.path.realpath`` because OpenSpace checks both the unresolved and the
    symlink-resolved form separately (see
    :func:`_paths_for_permission_check`).
    """
    if not path:
        return path
    if path.startswith("~"):
        path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    # Normalise redundant separators but DO NOT follow symlinks.
    return os.path.normpath(path)


def contains_path_traversal(relative: str) -> bool:
    """OpenSpace ``containsPathTraversal`` — relative path starts with ``..``."""
    if not relative:
        return False
    posix = to_posix_path(relative)
    if posix == "..":
        return True
    if posix.startswith("../"):
        return True
    # Embedded traversal (``a/../b``) is OK for gitignore matching but
    # still rejected by OpenSpace for membership checks — mirror that here.
    parts = posix.split("/")
    return ".." in parts and parts[0] == ".."


def relative_path(from_path: str, to_path: str) -> str:
    """OpenSpace ``relativePath`` (filesystem.ts L170) — POSIX-style relpath."""
    posix_from = to_posix_path(from_path)
    posix_to = to_posix_path(to_path)
    try:
        return posixpath.relpath(posix_to, posix_from)
    except ValueError:
        # Different drive letters on Windows — treat as outside.
        return posixpath.join("..", posix_to)


def _paths_for_permission_check(path: str) -> Tuple[str, ...]:
    """OpenSpace ``getPathsForPermissionCheck`` — original + symlink-resolved.

    OpenSpace derives up to 3 canonical forms to prevent symlink-based
    bypasses: the raw input, the lexically normalised absolute form,
    and the ``realpath`` form (symlinks followed).  OS preserves the
    same fan-out but tolerates errors resolving symlinks (e.g. when
    the target does not yet exist on the filesystem — common for
    ``write``).
    """
    out: List[str] = []
    if path:
        out.append(path)
    try:
        expanded = expand_path(path)
        if expanded and expanded not in out:
            out.append(expanded)
    except Exception:
        expanded = path
    try:
        real = os.path.realpath(expanded)
        if real and real not in out:
            out.append(real)
    except Exception:
        pass
    return tuple(out)


# ════════════════════════════════════════════════════════════════════════
# §3  Suspicious Windows path patterns  (OpenSpace filesystem.ts L537-L602)
# ════════════════════════════════════════════════════════════════════════

_SHORT_NAME_RE = re.compile(r"~\d")
_TRAILING_DOTSPACE_RE = re.compile(r"[.\s]+$")
_DOS_DEVICE_RE = re.compile(
    r"\.(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE
)
_TRIPLE_DOT_RE = re.compile(r"(^|/|\\)\.{3,}(/|\\|$)")


def _current_platform() -> str:
    """OpenSpace ``getPlatform`` — collapse to macos/linux/windows/wsl."""
    plat = os.sys.platform  # type: ignore[attr-defined]
    if plat.startswith("win"):
        return "windows"
    if plat == "darwin":
        return "macos"
    # crude WSL sniff (``/proc/version`` mentions Microsoft)
    try:
        with open("/proc/version", "rb") as f:
            if b"microsoft" in f.read().lower():
                return "wsl"
    except Exception:
        pass
    return "linux"


def has_suspicious_windows_path_pattern(path: str) -> bool:
    """OpenSpace ``hasSuspiciousWindowsPathPattern`` (filesystem.ts L537).

    Detects patterns that could bypass string-level path checks on
    Windows/NTFS:

    - NTFS Alternate Data Streams (``file.txt::$DATA``) — Windows/WSL
    - 8.3 short names (``CLAUDE~1``)
    - Long-path prefixes (``\\\\?\\C:\\``, ``\\\\.\\``, ``//?/``, ``//./``)
    - Trailing dots/spaces (``.git.``, ``.bashrc ``) that Windows
      silently strips during resolution
    - DOS device names (``.git.CON``)
    - 3+ consecutive dots used as a path component (``.../x``)
    - UNC paths (delegated to :func:`contains_vulnerable_unc_path`)
    """
    if not path:
        return False

    platform = _current_platform()

    # NTFS ADS — only interpreted by the Windows kernel.  On non-WSL
    # Linux/macOS, colons are valid filename characters.
    if platform in ("windows", "wsl"):
        colon_idx = path.find(":", 2)
        if colon_idx != -1:
            return True

    if _SHORT_NAME_RE.search(path):
        return True

    if (
        path.startswith("\\\\?\\")
        or path.startswith("\\\\.\\")
        or path.startswith("//?/")
        or path.startswith("//./")
    ):
        return True

    if _TRAILING_DOTSPACE_RE.search(path):
        return True

    if _DOS_DEVICE_RE.search(path):
        return True

    if _TRIPLE_DOT_RE.search(path):
        return True

    if _contains_vulnerable_unc_path(path, force_check=True):
        return True

    return False


# ════════════════════════════════════════════════════════════════════════
# §4  Settings / sensitive-path detection
# ════════════════════════════════════════════════════════════════════════


def is_openspace_settings_path(path: str) -> bool:
    """OpenSpace ``isClaudeSettingsPath`` (filesystem.ts L200).

    Match any ``.openspace/settings.json`` or
    ``.openspace/settings.local.json`` regardless of project — these are
    always sensitive.
    """
    if not path:
        return False
    expanded = expand_path(path)
    lower = normalize_case_for_comparison(expanded)
    posix = to_posix_path(lower)
    return (
        posix.endswith("/.openspace/settings.json")
        or posix.endswith("/.openspace/settings.local.json")
    )


def _match_sensitive_filename(filename: str) -> bool:
    """True iff the final path component is in the OS sensitive list."""
    if not filename:
        return False
    lower = normalize_case_for_comparison(filename)
    if lower in {name.lower() for name in SENSITIVE_FILES}:
        return True
    # Glob-shaped sensitive names (``.env.production``).
    for glob_pat in SENSITIVE_FILE_GLOBS:
        if fnmatch.fnmatchcase(lower, glob_pat.lower()):
            return True
    return False


def _has_sensitive_directory_segment(path: str) -> bool:
    """True iff any path component matches the sensitive-dir list."""
    if not path:
        return False
    expanded = expand_path(path)
    segments = re.split(r"[\\/]+", expanded)
    sensitive = {seg.lower() for seg in SENSITIVE_DIRECTORY_SEGMENTS}
    for seg in segments:
        if seg and seg.lower() in sensitive:
            return True
    return False


def is_sensitive_path(path: str) -> bool:
    """OS-specific: True iff ``path`` is in the user-declared sensitive list.

    The sensitive list is a superset of OpenSpace's ``DANGEROUS_*`` and is
    enforced even in ``bypassPermissions`` / ``acceptEdits`` modes.
    """
    if not path:
        return False
    expanded = expand_path(path)
    basename = os.path.basename(expanded)
    if _match_sensitive_filename(basename):
        return True
    if _has_sensitive_directory_segment(expanded):
        return True
    # ``settings.json`` is only sensitive when under ``.openspace/``.
    if normalize_case_for_comparison(basename) in {"settings.json", "settings.local.json"}:
        if is_openspace_settings_path(expanded):
            return True
    return False


def is_dangerous_file_path_to_auto_edit(path: str) -> bool:
    """OpenSpace ``isDangerousFilePathToAutoEdit`` (filesystem.ts L435).

    Triggers ``ask`` for paths that live under ``DANGEROUS_DIRECTORIES``
    or whose basename is in ``DANGEROUS_FILES``.  Matches OpenSpace
    case-insensitively and skips ``.openspace/worktrees`` as a
    structural carve-out (Implementation: ``.claude/worktrees``).
    """
    if not path:
        return False

    # Defence-in-depth UNC check (Implementation: block any \\ or // prefix up
    # front — catches patterns that slip past the regex UNC detector).
    if path.startswith("\\\\") or path.startswith("//"):
        return True

    expanded = expand_path(path)
    segments = re.split(r"[\\/]+", expanded)
    dangerous_dirs_lower = {d.lower() for d in DANGEROUS_DIRECTORIES}

    for i, segment in enumerate(segments):
        if not segment:
            continue
        segment_lower = segment.lower()
        if segment_lower in dangerous_dirs_lower:
            # OpenSpace carve-out: ``.openspace/worktrees/`` (Implementation: ``.claude/worktrees``)
            # is a structural path used by the harness itself.  Skip the
            # ``.openspace`` segment when it's followed by ``worktrees``.
            if segment_lower == ".openspace":
                nxt = segments[i + 1] if i + 1 < len(segments) else ""
                if nxt and nxt.lower() == "worktrees":
                    continue
            return True

    filename = segments[-1] if segments else ""
    if filename:
        filename_lower = filename.lower()
        for dangerous in DANGEROUS_FILES:
            if dangerous.lower() == filename_lower:
                return True

    return False


# ════════════════════════════════════════════════════════════════════════
# §5  Working directory membership
# ════════════════════════════════════════════════════════════════════════


def all_working_directories(context: ToolPermissionContext) -> Set[str]:
    """OpenSpace ``allWorkingDirectories`` (filesystem.ts L667).

    OpenSpace returns ``new Set([getOriginalCwd(), ...ctx.additionalWorkingDirectories.keys()])``.
    In OS the original cwd is stored inside
    ``context.additional_working_directories`` (see
    ``ToolPermissionContext.default``) so we just return its keys.
    Callers that want to inject a process-wide cwd on top of the
    context should pre-populate it there.
    """
    return set(context.additional_working_directories.keys())


def _macos_symlink_unalias(p: str) -> str:
    """Collapse macOS ``/private/var`` → ``/var`` etc. (OpenSpace filesystem.ts L716)."""
    if p.startswith("/private/var/"):
        return "/var/" + p[len("/private/var/"):]
    if p == "/private/tmp" or p.startswith("/private/tmp/"):
        return "/tmp" + p[len("/private/tmp"):]
    return p


def path_in_working_path(path: str, working_path: str) -> bool:
    """OpenSpace ``pathInWorkingPath`` (filesystem.ts L709).

    Case-insensitive membership check robust to macOS ``/private/var``
    and ``/private/tmp`` symlinks.
    """
    if not path or not working_path:
        return False

    abs_path = _macos_symlink_unalias(expand_path(path))
    abs_wp = _macos_symlink_unalias(expand_path(working_path))

    # Normalise for case-insensitive comparison.
    ln_path = normalize_case_for_comparison(abs_path)
    ln_wp = normalize_case_for_comparison(abs_wp)

    rel = relative_path(ln_wp, ln_path)

    if rel == "" or rel == ".":
        return True
    if contains_path_traversal(rel):
        return False
    return not posixpath.isabs(rel)


def path_in_allowed_working_path(
    path: str,
    context: ToolPermissionContext,
    precomputed_paths_to_check: Optional[Sequence[str]] = None,
) -> bool:
    """OpenSpace ``pathInAllowedWorkingPath`` (filesystem.ts L683)."""
    paths_to_check = (
        tuple(precomputed_paths_to_check)
        if precomputed_paths_to_check is not None
        else _paths_for_permission_check(path)
    )

    # Expand each working directory to both its lexical and symlink
    # forms so comparisons are symmetric on macOS / WSL.
    working_paths: List[str] = []
    for wp in all_working_directories(context):
        for resolved in _paths_for_permission_check(wp):
            if resolved not in working_paths:
                working_paths.append(resolved)

    if not working_paths:
        return False

    # Every resolved input form must be inside *some* working dir.
    for p in paths_to_check:
        if not any(path_in_working_path(p, wp) for wp in working_paths):
            return False
    return True


# ════════════════════════════════════════════════════════════════════════
# §6  Rule lookup helpers
# ════════════════════════════════════════════════════════════════════════


def _iter_rules_for_behavior(
    context: ToolPermissionContext, behavior: PermissionBehavior
) -> Iterable[PermissionRule]:
    """Flatten ``context.always_*_rules`` → iterable of :class:`PermissionRule`.

    Parses the stored ``toolName(ruleContent)`` strings back into
    ``PermissionRuleValue`` and attaches the originating source.
    Malformed entries are silently dropped (same as OpenSpace's parseRule).
    """
    if behavior == "allow":
        by_source = context.always_allow_rules
    elif behavior == "deny":
        by_source = context.always_deny_rules
    else:
        by_source = context.always_ask_rules

    for source, raw_rules in by_source.items():
        for raw in raw_rules or ():
            try:
                value = parse_rule_value(raw)
            except ValueError:
                continue
            yield PermissionRule(
                source=source, rule_behavior=behavior, rule_value=value
            )


def _rules_for_tool_and_behavior(
    context: ToolPermissionContext,
    tool_names: Sequence[str],
    behavior: PermissionBehavior,
) -> List[PermissionRule]:
    """All rules whose ``rule_value.tool_name`` is in ``tool_names``."""
    tool_set = set(tool_names)
    return [
        rule
        for rule in _iter_rules_for_behavior(context, behavior)
        if rule.rule_value.tool_name in tool_set
    ]


# --- gitignore-ish matcher (OpenSpace uses the ``ignore`` npm package) ------

def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-style glob to a regex.

    Supported metachars::

        **   → any depth (including zero segments)
        *    → any char except ``/``
        ?    → any single char except ``/``
        [..] → character class (pass-through)

    A leading ``/`` anchors the pattern to the start of the relative
    path; otherwise the pattern matches if it occurs at *any* depth
    (gitignore semantics).
    """
    anchored = pattern.startswith("/")
    body = pattern[1:] if anchored else pattern

    # Walk char-by-char to emit regex tokens.
    out: List[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "*":
            # Check for `**`
            if i + 1 < n and body[i + 1] == "*":
                # Handle `**/`, `/**`, `/**/`, or bare `**`.
                if i + 2 < n and body[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if ch == "?":
            out.append("[^/]")
            i += 1
            continue
        if ch == "[":
            # Pass the char class through with minimal escaping.
            end = body.find("]", i + 1)
            if end == -1:
                out.append(re.escape(ch))
                i += 1
                continue
            cls = body[i : end + 1]
            out.append(cls)
            i = end + 1
            continue
        out.append(re.escape(ch))
        i += 1

    body_regex = "".join(out)

    if anchored:
        # Must match from the start; allow a trailing ``/…`` so that
        # ``/dir`` matches ``dir/file`` too (gitignore dir semantics).
        full = f"^{body_regex}(?:/.*)?$"
    else:
        # Match the pattern at any depth — either as the entire string
        # or as a suffix starting after a ``/``.
        full = f"^(?:.*/)?{body_regex}(?:/.*)?$"
    return re.compile(full)


def _gitignore_test(rel_path: str, pattern: str) -> bool:
    """True iff ``rel_path`` is ignored by ``pattern`` (OpenSpace ``ig.test``)."""
    if not pattern or not rel_path:
        return False
    posix_path = to_posix_path(rel_path)
    while posix_path.startswith("./"):
        posix_path = posix_path[2:]
    posix_path = posix_path.lstrip("/")
    regex = _glob_to_regex(pattern)
    return bool(regex.match(posix_path))


# --- patternWithRoot / getPatternsByRoot (OpenSpace filesystem.ts L853-L953) -

def _root_path_for_source(
    context: ToolPermissionContext, source: PermissionRuleSource
) -> str:
    """OpenSpace ``rootPathForSource`` (filesystem.ts L746).

    OS cannot ask the settings loader for the on-disk root of each
    source (see 20.x Settings — not yet landed).  We fall back to the
    *first* additional working directory (OpenSpace uses ``getOriginalCwd()``)
    for session/cliArg/command sources, and the same for
    user/project/local settings since we don't track disk roots yet.
    """
    wds = list(context.additional_working_directories.keys())
    return wds[0] if wds else os.getcwd()


def _pattern_with_root(
    context: ToolPermissionContext, pattern: str, source: PermissionRuleSource
) -> Tuple[str, Optional[str]]:
    """OpenSpace ``patternWithRoot`` (filesystem.ts L853).

    Returns ``(relativePattern, root)`` where ``root=None`` means the
    pattern may match anywhere.
    """
    DIR_SEP = "/"

    if pattern.startswith(DIR_SEP + DIR_SEP):
        # ``//abs/path/**``  → root = ``/``, pattern = abs path.
        without_double = pattern[1:]
        return without_double, DIR_SEP

    if pattern.startswith("~" + DIR_SEP):
        home = os.path.expanduser("~")
        return pattern[1:], home

    if pattern.startswith(DIR_SEP):
        return pattern, _root_path_for_source(context, source)

    # No explicit root → anchor-free pattern; strip leading ``./``.
    normalised = pattern
    if pattern.startswith("." + DIR_SEP):
        normalised = pattern[2:]
    return normalised, None


def _get_patterns_by_root(
    context: ToolPermissionContext,
    tool_names: Sequence[str],
    behavior: PermissionBehavior,
) -> Dict[Optional[str], Dict[str, PermissionRule]]:
    """OpenSpace ``getPatternsByRoot`` (filesystem.ts L919).

    Returns ``{root: {relativePattern: rule}}``.  ``root=None`` means
    "pattern can match at any depth".
    """
    rules = _rules_for_tool_and_behavior(context, tool_names, behavior)
    result: Dict[Optional[str], Dict[str, PermissionRule]] = {}
    for rule in rules:
        content = rule.rule_value.rule_content
        if not content:
            # Tool-wide rule with no path pattern — store under the
            # null root with a catch-all ``**``.
            entry = result.setdefault(None, {})
            entry["**"] = rule
            continue
        relative_pattern, root = _pattern_with_root(context, content, rule.source)
        entry = result.setdefault(root, {})
        entry[relative_pattern] = rule
    return result


def matching_rule_for_input(
    tool_name: str,
    path: str,
    context: ToolPermissionContext,
    behavior: PermissionBehavior,
) -> Optional[PermissionRule]:
    """OpenSpace ``matchingRuleForInput`` (filesystem.ts L955).

    Returns the first :class:`PermissionRule` that matches ``path`` at
    ``behavior`` for the given ``tool_name`` category (``'read'`` →
    read-family tools; ``'edit'``/``'write'`` → edit-family tools).

    OpenSpace signature::

        matchingRuleForInput(path, ctx, toolType, behavior)
          where toolType ∈ {'read','edit'}

    We accept the actual tool name (``'read'``/``'grep'``/… or
    ``'edit'``/``'write'``) and broaden internally.
    """
    if tool_name in EDIT_RULE_TOOL_NAMES:
        tool_names: Sequence[str] = EDIT_RULE_TOOL_NAMES
    elif tool_name in READ_RULE_TOOL_NAMES:
        tool_names = READ_RULE_TOOL_NAMES
    else:
        # Unknown tool — match only rules with the exact tool name.
        tool_names = (tool_name,)

    abs_path = to_posix_path(expand_path(path))
    if not abs_path:
        return None

    patterns_by_root = _get_patterns_by_root(context, tool_names, behavior)
    # Deterministic iteration: explicit roots first (longest first so
    # nested roots win), then the anchor-free bucket.
    explicit_roots = sorted(
        (r for r in patterns_by_root.keys() if r is not None),
        key=lambda r: len(r or ""),
        reverse=True,
    )

    cwd_fallback = (
        next(iter(context.additional_working_directories.keys()), os.getcwd())
    )

    for root in explicit_roots + [None]:
        pattern_map = patterns_by_root.get(root) or {}
        if not pattern_map:
            continue
        if root is None:
            ref_root = cwd_fallback
        else:
            ref_root = root
        rel = relative_path(ref_root, abs_path)
        if rel.startswith("../") or rel == "..":
            continue
        if not rel:
            continue
        for pattern, rule in pattern_map.items():
            # OpenSpace strips trailing ``/**`` before feeding to ``ignore()``;
            # we keep both forms to preserve OpenSpace's dual-lookup behaviour.
            check_pattern = pattern
            if check_pattern.endswith("/**"):
                check_pattern = check_pattern[:-3]
            if _gitignore_test(rel, check_pattern) or _gitignore_test(rel, pattern):
                return rule
    return None


# ════════════════════════════════════════════════════════════════════════
# §7  Pattern normalisation helpers
# ════════════════════════════════════════════════════════════════════════


def _normalize_pattern_to_path(
    *,
    pattern_root: str,
    pattern: str,
    root_path: str,
) -> Optional[str]:
    """OpenSpace ``normalizePatternToPath`` (filesystem.ts L765)."""
    DIR_SEP = "/"
    full_pattern = posixpath.join(pattern_root, pattern)
    if pattern_root == root_path:
        return DIR_SEP + pattern.lstrip(DIR_SEP)
    if full_pattern.startswith(root_path + DIR_SEP):
        relative_part = full_pattern[len(root_path):]
        return DIR_SEP + relative_part.lstrip(DIR_SEP)
    # Pattern is either outside the reference root or nested under a
    # sibling.
    try:
        rel = posixpath.relpath(pattern_root, root_path)
    except ValueError:
        return None
    if not rel or rel == ".." or rel.startswith(".." + DIR_SEP):
        return None
    relative_pattern = posixpath.join(rel, pattern)
    return DIR_SEP + relative_pattern.lstrip(DIR_SEP)


def normalize_patterns_to_path(
    patterns_by_root: Mapping[Optional[str], Sequence[str]],
    root: str,
) -> List[str]:
    """OpenSpace ``normalizePatternsToPath`` (filesystem.ts L800)."""
    result: List[str] = []
    seen: Set[str] = set()

    for pat in patterns_by_root.get(None, ()) or ():
        if pat not in seen:
            seen.add(pat)
            result.append(pat)

    for pattern_root, patterns in patterns_by_root.items():
        if pattern_root is None:
            continue
        for pattern in patterns or ():
            normalised = _normalize_pattern_to_path(
                pattern_root=pattern_root, pattern=pattern, root_path=root
            )
            if normalised and normalised not in seen:
                seen.add(normalised)
                result.append(normalised)
    return result


def get_file_read_ignore_patterns(
    context: ToolPermissionContext,
) -> Dict[Optional[str], List[str]]:
    """OpenSpace ``getFileReadIgnorePatterns`` (filesystem.ts L837).

    Aggregates all file-read deny patterns grouped by their root.  Used
    by GrepTool/GlobTool to hide files matched by read-deny rules.
    """
    patterns_by_root = _get_patterns_by_root(
        context, READ_RULE_TOOL_NAMES, "deny"
    )
    out: Dict[Optional[str], List[str]] = {}
    for root, pattern_map in patterns_by_root.items():
        out[root] = list(pattern_map.keys())
    return out


# ════════════════════════════════════════════════════════════════════════
# §8  Internal-path allow-lists
# ════════════════════════════════════════════════════════════════════════

# OpenSpace weaves in half a dozen session-lifecycle predicates
# (``isSessionPlanFile``, ``isScratchpadPath``, ``isAgentMemoryPath``,
# ``isAutoMemPath``, ``getToolResultsDir``, ``getBundledSkillsRoot``,
# ``getProjectTempDir``, ``join(getClaudeConfigHomeDir(),'tasks')``,
# ``join(getClaudeConfigHomeDir(),'teams')`` …).  OS has not yet
# ported the supporting runtime state (step 17.x session storage,
# 15.x agent memory, 23.x scratchpad, etc.), so this module exposes a
# registration API that downstream modules can populate without
# touching the control flow here.

InternalPathPredicate = Callable[[str], bool]
_INTERNAL_EDITABLE_PREDICATES: List[Tuple[str, InternalPathPredicate]] = []
_INTERNAL_READABLE_PREDICATES: List[Tuple[str, InternalPathPredicate]] = []


def register_internal_path_predicate(
    *,
    category: Literal["editable", "readable"],
    reason: str,
    predicate: InternalPathPredicate,
) -> None:
    """Register a carve-out for :func:`check_editable_internal_path` or
    :func:`check_readable_internal_path`.

    Example (future 17.1 session store wiring)::

        register_internal_path_predicate(
            category="editable",
            reason="Plan files for current session are allowed for writing",
            predicate=is_session_plan_file,
        )

    ``predicate`` receives an absolute, normalised path.
    """
    entry = (reason, predicate)
    if category == "editable":
        _INTERNAL_EDITABLE_PREDICATES.append(entry)
    elif category == "readable":
        _INTERNAL_READABLE_PREDICATES.append(entry)
    else:
        raise ValueError(
            f"register_internal_path_predicate: unknown category {category!r}"
        )


def check_editable_internal_path(
    absolute_path: str,
    input_payload: Optional[Mapping[str, Any]] = None,
) -> PermissionResult:
    """OpenSpace ``checkEditableInternalPath`` (filesystem.ts L1479).

    OpenSpace branches (session plan file, scratchpad, job dir under
    ``TEMPLATES`` feature, agent memory, auto-mem, preview
    ``launch.json``) are delegated to predicates registered via
    :func:`register_internal_path_predicate`.  When no predicate has
    been registered (the default until 15.x/17.1/23.x land) the
    function returns :class:`PermissionPassthrough`, matching OpenSpace's
    ``{ behavior: 'passthrough' }`` fall-through.
    """
    if not absolute_path:
        return PermissionPassthrough(message="")
    normalized = expand_path(absolute_path)
    for reason, predicate in _INTERNAL_EDITABLE_PREDICATES:
        try:
            if predicate(normalized):
                return PermissionAllow(
                    updated_input=dict(input_payload) if input_payload else None,
                    decision_reason=DecisionReasonOther(reason=reason),
                )
        except Exception:  # pragma: no cover - defensive
            continue
    return PermissionPassthrough(message="")


def check_readable_internal_path(
    absolute_path: str,
    input_payload: Optional[Mapping[str, Any]] = None,
) -> PermissionResult:
    """OpenSpace ``checkReadableInternalPath`` (filesystem.ts L1611).

    See :func:`check_editable_internal_path` for the registration
    semantics.  OpenSpace checks session-memory, project dir, plan file,
    tool-results, scratchpad, project-temp, agent memory, auto-mem,
    tasks dir, teams dir, bundled-skills root — all delegated to
    registered predicates in OS.
    """
    if not absolute_path:
        return PermissionPassthrough(message="")
    normalized = expand_path(absolute_path)
    for reason, predicate in _INTERNAL_READABLE_PREDICATES:
        try:
            if predicate(normalized):
                return PermissionAllow(
                    updated_input=dict(input_payload) if input_payload else None,
                    decision_reason=DecisionReasonOther(reason=reason),
                )
        except Exception:  # pragma: no cover - defensive
            continue
    return PermissionPassthrough(message="")


# ════════════════════════════════════════════════════════════════════════
# §9  Safety check (OpenSpace checkPathSafetyForAutoEdit L620-L665)
# ════════════════════════════════════════════════════════════════════════


_SafetyReturn = Optional[Tuple[str, bool]]


def check_path_safety_for_auto_edit(
    path: str,
    context: Optional[ToolPermissionContext] = None,
    precomputed_paths_to_check: Optional[Sequence[str]] = None,
) -> _SafetyReturn:
    """OpenSpace ``checkPathSafetyForAutoEdit`` (filesystem.ts L620).

    Returns ``None`` when the path is safe, or
    ``(reason_message, classifier_approvable)`` when not.

    Mirrors OpenSpace's three-step cascade and layers OS's sensitive-path set
    on top so it fires in ``bypassPermissions`` / ``acceptEdits`` modes.

    The ``context`` parameter is accepted for API symmetry with the
    rest of the module but not consulted directly — the check is
    purely path-based.  ``precomputed_paths_to_check`` lets callers
    reuse the original + symlink-resolved fan-out computed by
    :func:`_paths_for_permission_check`.
    """
    paths_to_check = (
        tuple(precomputed_paths_to_check)
        if precomputed_paths_to_check is not None
        else _paths_for_permission_check(path)
    )

    # OpenSpace step 1 — suspicious Windows path patterns → classifier cannot
    # approve (path canonicalisation risks).
    for p in paths_to_check:
        if has_suspicious_windows_path_pattern(p):
            return (
                f"OpenSpace requested permissions to write to {path}, which "
                "contains a suspicious Windows path pattern that requires "
                "manual approval.",
                False,
            )

    # OS-extension — user-declared sensitive paths force an ask even
    # under bypassPermissions (per user spec).  Classifier-approvable
    # because the content (not the shape) is what's sensitive.
    for p in paths_to_check:
        if is_sensitive_path(p):
            return (
                f"OpenSpace requested permissions to write to {path}, which "
                "is a sensitive path (credentials, shell config, or "
                "OpenSpace settings) — manual approval required.",
                True,
            )

    # OpenSpace step 2 — OpenSpace config files (.openspace/settings.json,
    # .openspace/commands/, .openspace/agents/, .openspace/skills/).
    # Detected via :func:`is_openspace_settings_path` plus the broader
    # ``.openspace/*`` segment check done by sensitive-paths above;
    # keep the explicit message for UX parity with OpenSpace.
    for p in paths_to_check:
        if is_openspace_settings_path(p):
            return (
                f"OpenSpace requested permissions to write to {path}, but "
                "you haven't granted it yet.",
                True,
            )

    # OpenSpace step 3 — generic dangerous files/dirs
    # (.git/, .vscode/, .idea/, .openspace/ + shell configs).
    for p in paths_to_check:
        if is_dangerous_file_path_to_auto_edit(p):
            return (
                f"OpenSpace requested permissions to edit {path} which is "
                "a sensitive file.",
                True,
            )

    return None


# ════════════════════════════════════════════════════════════════════════
# §10  Suggestions for ask decisions (OpenSpace generateSuggestions L1414)
# ════════════════════════════════════════════════════════════════════════


def _directory_for_path(path: str) -> str:
    """OpenSpace ``getDirectoryForPath`` — parent if file, self if dir."""
    expanded = expand_path(path)
    if os.path.isdir(expanded):
        return expanded
    return os.path.dirname(expanded) or expanded


def _create_read_rule_suggestion(
    directory: str, destination: str = "session"
) -> Optional[AddRulesUpdate]:
    """OpenSpace ``createReadRuleSuggestion`` (PermissionUpdate.ts).

    Wraps a directory in a ``read(<dir>/**)`` addRules update.
    """
    if not directory:
        return None
    posix_dir = to_posix_path(directory).rstrip("/")
    if not posix_dir:
        return None
    pattern = f"{posix_dir}/**"
    # OpenSpace anchors absolute patterns with a leading ``//`` so they resolve
    # relative to the filesystem root in ``patternWithRoot``.  Follow
    # suit so round-tripping works.
    if pattern.startswith("/") and not pattern.startswith("//"):
        pattern = "/" + pattern
    return AddRulesUpdate(
        destination=destination,  # type: ignore[arg-type]
        rules=(
            PermissionRuleValue(
                tool_name=FILE_READ_TOOL_NAME, rule_content=pattern
            ),
        ),
        behavior="allow",
    )


def _create_edit_rule_suggestion(
    directory: str, destination: str = "session"
) -> Optional[AddRulesUpdate]:
    """OS-side helper mirroring ``createReadRuleSuggestion`` for edits."""
    if not directory:
        return None
    posix_dir = to_posix_path(directory).rstrip("/")
    if not posix_dir:
        return None
    pattern = f"{posix_dir}/**"
    if pattern.startswith("/") and not pattern.startswith("//"):
        pattern = "/" + pattern
    return AddRulesUpdate(
        destination=destination,  # type: ignore[arg-type]
        rules=(
            PermissionRuleValue(
                tool_name=FILE_EDIT_TOOL_NAME, rule_content=pattern
            ),
        ),
        behavior="allow",
    )


def generate_suggestions(
    tool_name: str,
    path: str,
    context: ToolPermissionContext,
    precomputed_paths_to_check: Optional[Sequence[str]] = None,
    operation_type: Optional[Literal["read", "write", "create"]] = None,
) -> Tuple[PermissionUpdate, ...]:
    """OpenSpace ``generateSuggestions`` (filesystem.ts L1414).

    Emits the ``PermissionUpdate`` list the UI surfaces as "always
    allow" shortcuts.  Typically:

    - For reads outside working dirs: one ``addRules(read, <dir>/**)``
      per canonical path form.
    - For writes/creates: ``setMode=acceptEdits`` (when it would be an
      upgrade) plus, if outside working dirs, ``addDirectories([<dir>])``.
    - For reads inside working dirs: ``setMode=acceptEdits`` (no-op
      for reads today but preserved for parity).
    """
    # Derive operation type from tool_name if the caller didn't specify.
    if operation_type is None:
        if tool_name in EDIT_RULE_TOOL_NAMES:
            operation_type = "write"
        else:
            operation_type = "read"

    is_outside = not path_in_allowed_working_path(
        path, context, precomputed_paths_to_check
    )

    # Reads outside the working directory get one addRules(read,…) per
    # canonical dir form so subsequent checks against either the
    # symlink or the resolved path pass.
    if operation_type == "read" and is_outside:
        dir_path = _directory_for_path(path)
        dirs_to_add = _paths_for_permission_check(dir_path)
        suggestions: List[PermissionUpdate] = []
        for d in dirs_to_add:
            sugg = _create_read_rule_suggestion(d, "session")
            if sugg is not None:
                suggestions.append(sugg)
        return tuple(suggestions)

    # ``setMode: acceptEdits`` only suggested from modes where it's an
    # upgrade (Implementation: default / plan).  bypassPermissions / acceptEdits
    # already grant ≥ these rights; dontAsk is a deny-default mode
    # where silently downgrading would surprise the user.
    should_suggest_accept = context.mode in ("default", "plan")

    if operation_type in ("write", "create"):
        updates: List[PermissionUpdate] = []
        if should_suggest_accept:
            from .types import SetModeUpdate  # local import to avoid cycles

            updates.append(
                SetModeUpdate(destination="session", mode="acceptEdits")
            )
        if is_outside:
            dir_path = _directory_for_path(path)
            dirs_to_add = _paths_for_permission_check(dir_path)
            if dirs_to_add:
                updates.append(
                    AddDirectoriesUpdate(
                        destination="session",
                        directories=tuple(dirs_to_add),
                    )
                )
        return tuple(updates)

    # Reads inside working dirs → only the mode upgrade (when applicable).
    if should_suggest_accept:
        from .types import SetModeUpdate

        return (SetModeUpdate(destination="session", mode="acceptEdits"),)
    return ()


# ════════════════════════════════════════════════════════════════════════
# §11  Top-level entry points
# ════════════════════════════════════════════════════════════════════════


def _ask_unc(path: str, operation: str) -> PermissionAsk:
    """OpenSpace step 1 in checkReadPermissionForTool — defence-in-depth UNC."""
    return PermissionAsk(
        message=(
            f"OpenSpace requested permissions to {operation} from {path}, "
            "which appears to be a UNC path that could access network resources."
        ),
        blocked_path=path,
        decision_reason=DecisionReasonOther(
            reason="UNC path detected (defense-in-depth check)"
        ),
    )


def _ask_suspicious_windows(path: str, operation: str) -> PermissionAsk:
    """OpenSpace step 2 in checkReadPermissionForTool — suspicious win patterns."""
    return PermissionAsk(
        message=(
            f"OpenSpace requested permissions to {operation} from {path}, "
            "which contains a suspicious Windows path pattern that requires "
            "manual approval."
        ),
        blocked_path=path,
        decision_reason=DecisionReasonSafetyCheck(
            reason=(
                "Path contains suspicious Windows-specific patterns "
                "(alternate data streams, short names, long path prefixes, "
                "or three or more consecutive dots) that require manual "
                "verification"
            ),
            classifier_approvable=False,
        ),
    )


def check_read_permission_for_tool(
    tool_name: str,
    input_path: str,
    context: ToolPermissionContext,
    internal_read_roots: Optional[Sequence[str]] = None,
) -> PermissionResult:
    """OpenSpace ``checkReadPermissionForTool`` (filesystem.ts L1030).

    Faithful port of OpenSpace's 12-step cascade.  Callers pass the final
    path (resolved from ``tool.getPath(input)`` upstream) rather than
    a ``Tool`` instance.

    Returns a :class:`PermissionAllow` / :class:`PermissionAsk` /
    :class:`PermissionDeny` — never :class:`PermissionPassthrough`.
    """
    if not input_path:
        return PermissionAsk(
            message=(
                f"OpenSpace requested permissions to use {tool_name}, but "
                "you haven't granted it yet."
            )
        )

    paths_to_check = _paths_for_permission_check(input_path)

    # OpenSpace 1. Defence-in-depth UNC block.
    for p in paths_to_check:
        if p.startswith("\\\\") or p.startswith("//"):
            return _ask_unc(input_path, "read")

    # OpenSpace 2. Suspicious Windows patterns.
    for p in paths_to_check:
        if has_suspicious_windows_path_pattern(p):
            return _ask_suspicious_windows(input_path, "read")

    # OpenSpace 3. Read-specific deny rules (on every canonical path form).
    # Explicit deny rules stay terminal even for paths that would otherwise
    # trigger the sensitive-path ask below.
    for p in paths_to_check:
        deny_rule = matching_rule_for_input(FILE_READ_TOOL_NAME, p, context, "deny")
        if deny_rule is not None:
            return PermissionDeny(
                message=f"Permission to read {input_path} has been denied.",
                decision_reason=DecisionReasonRule(rule=deny_rule),
            )

    # Runtime-owned output is safe to read even when its session directory
    # lives below the otherwise-sensitive .openspace tree. Keep explicit deny
    # rules above this carve-out.
    for root in internal_read_roots or ():
        if root and all(path_in_working_path(p, root) for p in paths_to_check):
            return PermissionAllow(
                updated_input=None,
                decision_reason=DecisionReasonOther(
                    reason="Path is inside a runtime-owned readable directory"
                ),
            )

    # OS-extension: runtime-owned internal files, such as background task
    # stdout under the session task directory, must remain readable even when
    # their parent lives under ``.openspace``. Keep explicit deny rules above.
    internal = check_readable_internal_path(expand_path(input_path), None)
    if not isinstance(internal, PermissionPassthrough):
        return internal

    # OS-extension — sensitive paths force an ask even when the user
    # has installed a permissive read rule.
    for p in paths_to_check:
        if is_sensitive_path(p):
            return PermissionAsk(
                message=(
                    f"OpenSpace requested permissions to read from {input_path}, "
                    "which is a sensitive path (credentials, shell config, or "
                    "OpenSpace settings) — manual approval required."
                ),
                blocked_path=input_path,
                decision_reason=DecisionReasonSafetyCheck(
                    reason=(
                        "Path matches the OpenSpace sensitive-path list "
                        "(credentials / config / secrets)"
                    ),
                    classifier_approvable=True,
                ),
            )

    # OpenSpace 4. Read-specific ask rules.
    for p in paths_to_check:
        ask_rule = matching_rule_for_input(FILE_READ_TOOL_NAME, p, context, "ask")
        if ask_rule is not None:
            return PermissionAsk(
                message=(
                    f"OpenSpace requested permissions to read from {input_path}, "
                    "but you haven't granted it yet."
                ),
                blocked_path=input_path,
                decision_reason=DecisionReasonRule(rule=ask_rule),
            )

    # OpenSpace 5. Edit access implies read access (but only when no
    # read-specific ask/deny rule matched — preserved above).
    edit_result = check_write_permission_for_tool(
        # Use the read tool name in downstream messaging when we're
        # synthesising a write-check from a read-check — but delegate
        # rule matching to edit-category rules by passing the caller's
        # original tool name context via the write helper.
        tool_name="edit",
        input_path=input_path,
        context=context,
        precomputed_paths_to_check=paths_to_check,
    )
    if isinstance(edit_result, PermissionAllow):
        return edit_result

    # OpenSpace 6. Allow reads in working directories.
    if path_in_allowed_working_path(input_path, context, paths_to_check):
        return PermissionAllow(
            updated_input=None,
            decision_reason=DecisionReasonWorkingDir(
                reason="Path is inside an allowed working directory"
            ),
        )

    # OpenSpace 7. Allow internal harness paths.
    internal = check_readable_internal_path(expand_path(input_path), None)
    if not isinstance(internal, PermissionPassthrough):
        return internal

    # OpenSpace 8. Read-allow rules (user-granted).
    allow_rule = matching_rule_for_input(
        FILE_READ_TOOL_NAME, input_path, context, "allow"
    )
    if allow_rule is not None:
        return PermissionAllow(
            updated_input=None,
            decision_reason=DecisionReasonRule(rule=allow_rule),
        )

    # OpenSpace 12. Default → ask with suggestions.
    return PermissionAsk(
        message=(
            f"OpenSpace requested permissions to read from {input_path}, but "
            "you haven't granted it yet."
        ),
        blocked_path=input_path,
        suggestions=generate_suggestions(
            tool_name=FILE_READ_TOOL_NAME,
            path=input_path,
            context=context,
            precomputed_paths_to_check=paths_to_check,
            operation_type="read",
        ),
        decision_reason=DecisionReasonWorkingDir(
            reason="Path is outside allowed working directories"
        ),
    )


def check_write_permission_for_tool(
    tool_name: str,
    input_path: str,
    context: ToolPermissionContext,
    precomputed_paths_to_check: Optional[Sequence[str]] = None,
) -> PermissionResult:
    """OpenSpace ``checkWritePermissionForTool`` (filesystem.ts L1205).

    Faithful port of OpenSpace's 5-step cascade, with OS extensions:

    - ``plan`` mode → immediate :class:`PermissionDeny` (OS plan mode
      is strictly read-only; OpenSpace's ``plan`` defers to the normal
      pipeline).
    - ``bypassPermissions`` → allow *unless* the path is in the
      OS sensitive list (sensitive-paths always force ask).
    - ``acceptEdits`` + inside working dir + not sensitive → allow.
    """
    if not input_path:
        return PermissionAsk(
            message=(
                f"OpenSpace requested permissions to use {tool_name}, but "
                "you haven't granted it yet."
            )
        )

    # OS extension — plan mode disallows all writes outright.
    if context.mode == "plan":
        return PermissionDeny(
            message=(
                "Plan mode is read-only; OpenSpace cannot edit "
                f"{input_path} while the session is in plan mode."
            ),
            decision_reason=DecisionReasonMode(mode="plan"),
        )

    paths_to_check = (
        tuple(precomputed_paths_to_check)
        if precomputed_paths_to_check is not None
        else _paths_for_permission_check(input_path)
    )

    # OpenSpace 1. Edit-specific deny rules.
    for p in paths_to_check:
        deny_rule = matching_rule_for_input(
            FILE_EDIT_TOOL_NAME, p, context, "deny"
        )
        if deny_rule is not None:
            return PermissionDeny(
                message=f"Permission to edit {input_path} has been denied.",
                decision_reason=DecisionReasonRule(rule=deny_rule),
            )

    # OpenSpace 1.5. Internal editable paths (plan files, scratchpad, etc.).
    # Must come BEFORE safety checks because ``.openspace`` is a
    # dangerous directory but the harness legitimately writes under it.
    absolute_for_edit = expand_path(input_path)
    internal_edit = check_editable_internal_path(absolute_for_edit, None)
    if not isinstance(internal_edit, PermissionPassthrough):
        return internal_edit

    # OpenSpace 1.7. Comprehensive safety validations.
    # Must come BEFORE allow-rule / mode / bypass checks so the user
    # can't accidentally grant write access to sensitive files.
    safety = check_path_safety_for_auto_edit(
        input_path, context, paths_to_check
    )
    if safety is not None:
        message, classifier_approvable = safety
        return PermissionAsk(
            message=message,
            blocked_path=input_path,
            suggestions=generate_suggestions(
                tool_name=FILE_EDIT_TOOL_NAME,
                path=input_path,
                context=context,
                precomputed_paths_to_check=paths_to_check,
                operation_type="write",
            ),
            decision_reason=DecisionReasonSafetyCheck(
                reason=message,
                classifier_approvable=classifier_approvable,
            ),
        )

    # OpenSpace 2. Edit-specific ask rules.
    for p in paths_to_check:
        ask_rule = matching_rule_for_input(
            FILE_EDIT_TOOL_NAME, p, context, "ask"
        )
        if ask_rule is not None:
            return PermissionAsk(
                message=(
                    f"OpenSpace requested permissions to write to {input_path}, "
                    "but you haven't granted it yet."
                ),
                blocked_path=input_path,
                decision_reason=DecisionReasonRule(rule=ask_rule),
            )

    in_working_dir = path_in_allowed_working_path(
        input_path, context, paths_to_check
    )

    # OS extension — bypassPermissions allows everything that passed
    # the safety cascade above.
    if context.mode == "bypassPermissions":
        return PermissionAllow(
            updated_input=None,
            decision_reason=DecisionReasonMode(mode="bypassPermissions"),
        )

    # OpenSpace 3. acceptEdits + in working dir → allow.
    if context.mode == "acceptEdits" and in_working_dir:
        return PermissionAllow(
            updated_input=None,
            decision_reason=DecisionReasonMode(mode="acceptEdits"),
        )

    # OpenSpace 4. Edit-allow rules.
    allow_rule = matching_rule_for_input(
        FILE_EDIT_TOOL_NAME, input_path, context, "allow"
    )
    if allow_rule is not None:
        return PermissionAllow(
            updated_input=None,
            decision_reason=DecisionReasonRule(rule=allow_rule),
        )

    # OpenSpace 5. Default → ask.
    ask_reason: Optional[PermissionDecisionReason] = None
    if not in_working_dir:
        ask_reason = DecisionReasonWorkingDir(
            reason="Path is outside allowed working directories"
        )
    return PermissionAsk(
        message=(
            f"OpenSpace requested permissions to write to {input_path}, but "
            "you haven't granted it yet."
        ),
        blocked_path=input_path,
        suggestions=generate_suggestions(
            tool_name=FILE_EDIT_TOOL_NAME,
            path=input_path,
            context=context,
            precomputed_paths_to_check=paths_to_check,
            operation_type="write",
        ),
        decision_reason=ask_reason,
    )
