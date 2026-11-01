"""Search and navigation tools for local shell workspaces.

``GrepTool`` searches file contents with the system ``rg`` binary. ``GlobTool``
uses ``rg --files`` for path discovery. ``ListDirTool`` is a direct filesystem
listing helper for compact directory inspection.

Read-deny permission rules are translated to ripgrep ``--glob !...``
exclusions and results are re-filtered against canonical paths.
"""
from __future__ import annotations

import asyncio
import bisect
import fnmatch
import os
import re
import shutil
import stat
import time
from typing import Any, TYPE_CHECKING

from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace.grounding.backends.shell.session import ShellSession
    from openspace.services.tooling.context import ToolUseContext

logger = Logger.get_logger(__name__)


# =====================================================================
# Constants
# =====================================================================

GREP_TOOL_NAME = "grep"
GLOB_TOOL_NAME = "glob"
LIST_DIR_TOOL_NAME = "ls"

VCS_DIRECTORIES_TO_EXCLUDE = (".git", ".svn", ".hg", ".bzr", ".jj", ".sl")
"""VCS directories excluded from grep to reduce noise."""

DEFAULT_HEAD_LIMIT = 250
"""Cap on grep results when head_limit is unspecified.

Unbounded content-mode greps can fill up to the 20KB persist threshold.
250 is generous enough for exploratory searches while preventing context bloat.
Pass head_limit=0 explicitly for unlimited.
"""

DEFAULT_GLOB_LIMIT = 100
"""Cap on glob results."""

DEFAULT_RG_TIMEOUT = 20
"""Ripgrep process timeout in seconds."""

MAX_COLUMNS = 500
"""Maximum ripgrep output line length to prevent base64/minified clutter."""

FILE_NOT_FOUND_CWD_NOTE = (
    "Note: file paths are relative to the current working directory:"
)
"""Note included when a missing path may be cwd-relative."""


# =====================================================================
# Ripgrep subprocess invocation
# =====================================================================

def _find_rg() -> str:
    """Locate the ripgrep binary.  Returns 'rg' if found on PATH."""
    rg_path = shutil.which("rg")
    if rg_path:
        return rg_path
    return "rg"


async def _run_ripgrep(
    args: list[str],
    target: str,
    *,
    cwd: str | None = None,
    timeout: float = DEFAULT_RG_TIMEOUT,
    abort_event: asyncio.Event | None = None,
) -> list[str]:
    """Run ripgrep and return stdout lines.

    Returns:
        List of non-empty stdout lines (stripped of trailing ``\\r``).

    Raises:
        FileNotFoundError: rg binary not found.
        TimeoutError: process exceeded *timeout* seconds.
        RuntimeError: rg exited with code ≠ 0 or 1 and produced no output.
    """
    rg_cmd = _find_rg()
    full_args = [rg_cmd, *args, target]

    proc = await asyncio.create_subprocess_exec(
        *full_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(
            f"Ripgrep search timed out after {timeout} seconds. "
            "The search may have matched files but did not complete in time. "
            "Try searching a more specific path or pattern."
        )

    returncode = proc.returncode

    # exit 0 = matches found, exit 1 = no matches — both are success
    if returncode in (0, 1):
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        return [
            line.replace("\r", "")
            for line in stdout.strip().split("\n")
            if line.strip()
        ]

    stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

        # If there is partial stdout, return it instead of dropping matches.
    if stdout_bytes and stdout_bytes.strip():
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        lines = [
            line.replace("\r", "")
            for line in stdout.strip().split("\n")
            if line.strip()
        ]
        if lines:
            # Drop last line if it may be incomplete (timeout / buffer overflow)
            return lines[:-1] if len(lines) > 1 else lines

    raise RuntimeError(
        f"ripgrep exited with code {returncode}: {stderr[:500]}"
    )


# =====================================================================
# Path utilities (shared by Grep + Glob)
# =====================================================================

def _get_cwd(
    session: ShellSession | None,
    context: Any | None = None,
) -> str:
    """Resolve the effective working directory for search/navigation tools.

    Precedence: ``context.cwd`` > ``session.default_working_dir`` > ``os.getcwd()``.

    This mirrors ``file_tools._resolve_tool_working_dir`` so that a search
    result emitted by grep/glob/ls can be handed back to read/edit/write
    without silently switching to a different root.  Mixing these roots
    previously let an agent "search one file and read another", and the
    permission check would also evaluate paths against a different base.
    """
    context_cwd = getattr(context, "cwd", None)
    if isinstance(context_cwd, str) and context_cwd.strip():
        return context_cwd
    session_cwd = getattr(session, "default_working_dir", None) if session else None
    if isinstance(session_cwd, str) and session_cwd.strip():
        return session_cwd
    return os.getcwd()


def _expand_path(path: str, cwd: str) -> str:
    """Expand ~ and resolve relative paths against *cwd*."""
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    return os.path.normpath(expanded)


def _to_relative_path(abs_path: str, cwd: str) -> str:
    """Convert absolute path to relative (saves tokens in results)."""
    try:
        rel = os.path.relpath(abs_path, cwd)
        if rel.startswith(".."):
            return abs_path
        return rel
    except ValueError:
        return abs_path


def _suggest_path_under_cwd(requested_path: str, cwd: str) -> str | None:
    """Detect a missing cwd prefix and suggest the existing workspace path."""
    cwd_parent = os.path.dirname(cwd)
    if not requested_path.startswith(cwd_parent):
        return None
    relative_to_parent = os.path.relpath(requested_path, cwd_parent)
    parts = relative_to_parent.split(os.sep)
    if len(parts) < 2:
        return None
    candidate = os.path.join(cwd, *parts[1:])
    if os.path.exists(candidate):
        return candidate
    return None


def _is_unc_path(path: str) -> bool:
    """SECURITY: Check for UNC paths to prevent NTLM credential leaks."""
    return path.startswith("\\\\") or path.startswith("//")


def _resolve_ripgrep_target(path: str) -> tuple[str, str]:
    """Return ``(execution_root, target)`` for a ripgrep invocation."""
    normalized = os.path.normpath(path)
    if os.path.isdir(normalized):
        return normalized, "."
    parent = os.path.dirname(normalized) or os.path.sep
    target = os.path.basename(normalized) or "."
    return parent, target


def _resolve_glob_search(
    pattern: str,
    path: str | None,
    cwd: str,
) -> tuple[str, str]:
    """Return the actual ``(search_dir, search_pattern)`` for GlobTool."""
    search_dir = _expand_path(path, cwd) if path else cwd
    search_pattern = pattern

    if os.path.isabs(pattern):
        base_dir, rel_pattern = _extract_glob_base_directory(pattern)
        if base_dir:
            search_dir = os.path.normpath(base_dir)
            search_pattern = rel_pattern

    return search_dir, search_pattern


def _has_glob_special_chars(pattern: str) -> bool:
    return re.search(r"[*?\[{]", pattern) is not None


def _glob_permission_check_path(pattern: str, path: str | None, cwd: str) -> str:
    """Return the concrete path GlobTool must authorize before execution."""
    search_dir, _ = _resolve_glob_search(pattern, path, cwd)
    if os.path.isabs(pattern) and not _has_glob_special_chars(pattern):
        return os.path.normpath(pattern)
    return search_dir


def _resolve_list_dir_path(path: str | None, cwd: str) -> str:
    """Resolve the directory path used by ``ListDirTool``."""
    return _expand_path(path or ".", cwd)


def _resolve_search_result_path(path_text: str, execution_root: str) -> str:
    """Convert a ripgrep-emitted path to an absolute path."""
    candidate = path_text.strip()
    if not candidate or candidate == "--":
        return ""
    if os.path.isabs(candidate):
        return os.path.normpath(candidate)
    if candidate == ".":
        return execution_root
    if candidate.startswith("./"):
        candidate = candidate[2:]
    if candidate.startswith(f".{os.sep}"):
        candidate = candidate[2:]
    return os.path.normpath(os.path.join(execution_root, candidate))


def _extract_result_path_prefix(
    line: str,
    execution_root: str,
    *,
    output_mode: str = "content",
) -> tuple[str, int] | None:
    """Best-effort parse of the file path prefix in a ripgrep output line."""
    candidates = _result_path_prefix_candidates(
        line,
        execution_root,
        output_mode=output_mode,
    )

    existing_match: tuple[str, int] | None = None
    for candidate in candidates:
        # Ripgrep result prefixes identify files, not directories.  A denied
        # directory named like a short separator prefix (``public`` in
        # ``public-1-visible.txt``) must not override the longer file prefix.
        if candidate[0] and os.path.isfile(candidate[0]):
            existing_match = candidate
    if existing_match is not None:
        return existing_match

    if candidates:
        return candidates[-1]

    return None


def _result_path_prefix_candidates(
    line: str,
    execution_root: str,
    *,
    output_mode: str = "content",
) -> list[tuple[str, int]]:
    """Return plausible ripgrep-emitted path prefixes for a result line.

    Deleted files cannot be disambiguated with ``exists()``.  We keep plausible
    prefixes in short-to-long order so callers can prefer an existing file, and
    otherwise fall back to the longest filename-like prefix.
    """
    candidates: list[tuple[str, int]] = []
    seen_indexes: set[int] = set()

    def add(index: int) -> None:
        if index <= 0 or index in seen_indexes:
            return
        path = _resolve_search_result_path(line[:index], execution_root)
        if not path:
            return
        seen_indexes.add(index)
        candidates.append((path, index))

    if output_mode == "count":
        colon_index = line.rfind(":")
        if colon_index > 0 and line[colon_index + 1:].isdigit():
            add(colon_index)
        return candidates

    if output_mode != "content":
        return candidates

    for index, sep in enumerate(line):
        if sep not in (":", "-"):
            continue
        number_start = index + 1
        if number_start >= len(line) or not line[number_start].isdigit():
            continue
        number_end = number_start + 1
        while number_end < len(line) and line[number_end].isdigit():
            number_end += 1
        if number_end < len(line) and line[number_end] == sep:
            add(index)

    if candidates:
        return candidates

    for index, char in enumerate(line):
        if char == ":":
            add(index)

    return candidates


def _get_permission_ignore_patterns(
    execution_root: str,
    permission_context: ToolUseContext | Any | None,
) -> list[str]:
    """Return read-deny globs normalized to the ripgrep execution root."""
    actual_permission_context = getattr(permission_context, "permission_context", permission_context)
    if actual_permission_context is None:
        return []

    from openspace.grounding.core.permissions.filesystem import (
        get_file_read_ignore_patterns,
        normalize_patterns_to_path,
        to_posix_path,
    )

    patterns_by_root = get_file_read_ignore_patterns(actual_permission_context)
    return normalize_patterns_to_path(
        patterns_by_root,
        to_posix_path(os.path.normpath(execution_root)),
    )


def _append_permission_ignore_globs(
    args: list[str],
    execution_root: str,
    permission_context: ToolUseContext | Any | None,
) -> None:
    """Append ripgrep ``--glob !...`` exclusions derived from deny rules."""
    for pattern in _get_permission_ignore_patterns(execution_root, permission_context):
        args.extend(["--glob", f"!{pattern}"])


def _python_glob_files(
    search_dir: str,
    pattern: str,
    permission_context: ToolUseContext | Any | None,
    tool_name: str,
) -> list[str]:
    """Fallback for GlobTool when ripgrep is unavailable in minimal containers."""
    matches: list[str] = []
    normalized_pattern = pattern.replace("\\", "/")
    permission_ignores = _get_permission_ignore_patterns(
        search_dir,
        permission_context,
    )

    for root, dirnames, filenames in os.walk(search_dir):
        rel_root = os.path.relpath(root, search_dir)
        rel_root_posix = "" if rel_root == "." else rel_root.replace(os.sep, "/")
        kept_dirs: list[str] = []
        for dirname in dirnames:
            rel_dir = f"{rel_root_posix}/{dirname}" if rel_root_posix else dirname
            if any(fnmatch.fnmatch(rel_dir, ignore) for ignore in permission_ignores):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            rel_path = f"{rel_root_posix}/{filename}" if rel_root_posix else filename
            if any(fnmatch.fnmatch(rel_path, ignore) for ignore in permission_ignores):
                continue
            if not (
                fnmatch.fnmatch(rel_path, normalized_pattern)
                or fnmatch.fnmatch(filename, normalized_pattern)
            ):
                continue
            absolute_path = os.path.normpath(os.path.join(search_dir, rel_path))
            if _has_read_permission(
                absolute_path,
                tool_name,
                permission_context,
            ):
                matches.append(absolute_path)

    matches.sort(key=lambda item: (os.path.getmtime(item), item))
    return matches


_FALLBACK_TYPE_GLOBS = {
    "c": ("*.c", "*.h"),
    "cpp": ("*.cc", "*.cpp", "*.cxx", "*.hpp", "*.hh", "*.hxx"),
    "go": ("*.go",),
    "java": ("*.java",),
    "js": ("*.js", "*.jsx", "*.mjs", "*.cjs"),
    "json": ("*.json",),
    "javascript": ("*.js", "*.jsx", "*.mjs", "*.cjs"),
    "md": ("*.md", "*.markdown"),
    "py": ("*.py", "*.pyi"),
    "python": ("*.py", "*.pyi"),
    "rb": ("*.rb",),
    "rs": ("*.rs",),
    "rust": ("*.rs",),
    "sh": ("*.sh", "*.bash", "*.zsh"),
    "toml": ("*.toml",),
    "ts": ("*.ts", "*.tsx"),
    "tsx": ("*.tsx",),
    "typescript": ("*.ts", "*.tsx"),
    "txt": ("*.txt",),
    "yaml": ("*.yaml", "*.yml"),
}


def _coerce_nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _expand_simple_brace_glob(pattern: str) -> list[str]:
    """Expand simple ripgrep-style brace globs such as ``*.{ts,tsx}``."""
    start = pattern.find("{")
    end = pattern.find("}", start + 1)
    if start < 0 or end < 0:
        return [pattern]
    choices = [item.strip() for item in pattern[start + 1:end].split(",")]
    if not choices:
        return [pattern]
    prefix = pattern[:start]
    suffix = pattern[end + 1:]
    return [f"{prefix}{choice}{suffix}" for choice in choices if choice]


def _expand_fallback_globs(
    glob_patterns: list[str],
    type_filter: str | None,
) -> list[str]:
    patterns: list[str] = []
    for pattern in glob_patterns:
        if pattern:
            patterns.extend(_expand_simple_brace_glob(pattern))
    if type_filter:
        patterns.extend(_FALLBACK_TYPE_GLOBS.get(type_filter.lower(), ()))
    return patterns


def _matches_path_glob(rel_path: str, basename: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/")
    return (
        fnmatch.fnmatch(rel_path, normalized)
        or fnmatch.fnmatch(basename, normalized)
    )


def _matches_any_path_glob(rel_path: str, patterns: list[str]) -> bool:
    basename = os.path.basename(rel_path)
    return any(_matches_path_glob(rel_path, basename, pattern) for pattern in patterns)


def _line_start_offsets(text: str) -> list[int]:
    offsets = [0]
    for match in re.finditer("\n", text):
        offsets.append(match.end())
    return offsets


def _line_for_offset(offsets: list[int], offset: int) -> int:
    return max(1, bisect.bisect_right(offsets, offset))


def _context_line_numbers(
    match_lines: list[int],
    total_lines: int,
    before: int,
    after: int,
) -> list[int]:
    selected: set[int] = set()
    for line_no in match_lines:
        start = max(1, line_no - before)
        end = min(total_lines, line_no + after)
        selected.update(range(start, end + 1))
    return sorted(selected)


def _python_grep_files(
    *,
    execution_root: str,
    target: str,
    pattern: str,
    output_mode: str,
    glob_patterns: list[str],
    type_filter: str | None,
    case_insensitive: bool,
    show_line_numbers: bool,
    context_before: int,
    context_after: int,
    multiline: bool,
    permission_context: ToolUseContext | Any | None,
    tool_name: str,
) -> list[str]:
    """Small grep fallback for minimal containers without ripgrep.

    The fallback intentionally implements the common grep surface rather than
    every ripgrep flag: regex search, glob/type filters, files/count/content
    modes, line numbers, and nearby context. Ripgrep remains the fast path.
    """
    flags = re.IGNORECASE | (re.DOTALL if multiline else 0)
    regex = re.compile(pattern, flags)
    target_path = os.path.normpath(os.path.join(execution_root, target))
    effective_globs = _expand_fallback_globs(glob_patterns, type_filter)
    permission_ignores = _get_permission_ignore_patterns(
        execution_root,
        permission_context,
    )
    files: list[str] = []

    def include_file(path: str) -> bool:
        rel_path = _to_relative_path(path, execution_root).replace(os.sep, "/")
        if permission_ignores and _matches_any_path_glob(rel_path, permission_ignores):
            return False
        if effective_globs and not _matches_any_path_glob(rel_path, effective_globs):
            return False
        if any(part in VCS_DIRECTORIES_TO_EXCLUDE for part in rel_path.split("/")):
            return False
        return _has_read_permission(path, tool_name, permission_context)

    if os.path.isfile(target_path):
        if include_file(target_path):
            files.append(target_path)
    else:
        walk_root = target_path if os.path.isdir(target_path) else execution_root
        for root, dirnames, filenames in os.walk(walk_root):
            rel_root = _to_relative_path(root, execution_root).replace(os.sep, "/")
            kept_dirs: list[str] = []
            for dirname in dirnames:
                if dirname in VCS_DIRECTORIES_TO_EXCLUDE:
                    continue
                rel_dir = dirname if rel_root == "." else f"{rel_root}/{dirname}"
                if permission_ignores and _matches_any_path_glob(rel_dir, permission_ignores):
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs
            for filename in filenames:
                candidate = os.path.join(root, filename)
                if include_file(candidate):
                    files.append(candidate)

    results: list[str] = []
    for file_path in sorted(files):
        rel_path = _to_relative_path(file_path, execution_root)
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue

        lines = text.splitlines()
        if multiline:
            offsets = _line_start_offsets(text)
            matched_line_numbers = sorted(
                {
                    _line_for_offset(offsets, match.start())
                    for match in regex.finditer(text)
                }
            )
        else:
            matched_line_numbers = [
                line_no
                for line_no, line in enumerate(lines, start=1)
                if regex.search(line)
            ]

        if not matched_line_numbers:
            continue
        if output_mode == "files_with_matches":
            results.append(rel_path)
        elif output_mode == "count":
            results.append(f"{rel_path}:{len(matched_line_numbers)}")
        else:
            context_line_numbers = _context_line_numbers(
                matched_line_numbers,
                len(lines),
                context_before,
                context_after,
            )
            previous_line_no: int | None = None
            match_line_set = set(matched_line_numbers)
            for line_no in context_line_numbers:
                if previous_line_no is not None and line_no > previous_line_no + 1:
                    results.append("--")
                previous_line_no = line_no
                if line_no < 1 or line_no > len(lines):
                    continue
                line = lines[line_no - 1].rstrip("\n\r")[:MAX_COLUMNS]
                separator = ":" if line_no in match_line_set else "-"
                if show_line_numbers:
                    results.append(f"{rel_path}{separator}{line_no}{separator}{line}")
                else:
                    results.append(f"{rel_path}:{line}")
    return results


def _add_permission_path_candidate(candidates: list[str], path: str) -> None:
    """Add path plus macOS /private aliases used by permission checks."""
    if not path or path in candidates:
        return
    candidates.append(path)
    if path.startswith("/private/var/"):
        alias = "/var/" + path[len("/private/var/"):]
    elif path == "/private/tmp" or path.startswith("/private/tmp/"):
        alias = "/tmp" + path[len("/private/tmp"):]
    else:
        alias = ""
    if alias and alias not in candidates:
        candidates.append(alias)


def _read_permission_path_candidates(path: str) -> list[str]:
    """Return lexical, absolute, realpath, and macOS alias forms for ``path``."""
    paths_to_check: list[str] = []
    for candidate in (
        path,
        os.path.abspath(os.path.expanduser(path)) if path else "",
        os.path.realpath(path) if path else "",
    ):
        _add_permission_path_candidate(paths_to_check, candidate)
    return paths_to_check


def _has_read_permission(
    path: str,
    tool_name: str,
    permission_context: ToolUseContext | Any | None,
) -> bool:
    """True only when the full read-permission cascade allows ``path``."""
    actual_permission_context = getattr(permission_context, "permission_context", permission_context)
    if actual_permission_context is None:
        return False

    from openspace.grounding.core.permissions import (
        PermissionAllow,
        check_read_permission_for_tool,
    )

    paths_to_check = _read_permission_path_candidates(path)
    if not paths_to_check:
        return False

    return all(
        isinstance(
            check_read_permission_for_tool(
                tool_name=tool_name,
                input_path=candidate,
                context=actual_permission_context,
            ),
            PermissionAllow,
        )
        for candidate in paths_to_check
    )


def _is_read_denied(
    path: str,
    tool_name: str,
    permission_context: ToolUseContext | Any | None,
) -> bool:
    """Backward-compatible wrapper: deny means anything not explicitly allowed."""
    return not _has_read_permission(path, tool_name, permission_context)


def _filter_grep_results_by_permissions(
    results: list[str],
    *,
    output_mode: str,
    execution_root: str,
    permission_context: ToolUseContext | Any | None,
    tool_name: str,
) -> list[str]:
    """Drop grep results whose backing file is not read-allowed."""
    actual_permission_context = getattr(permission_context, "permission_context", permission_context)
    if actual_permission_context is None:
        return []

    filtered: list[str] = []
    for line in results:
        if line == "--":
            filtered.append(line)
            continue

        if output_mode == "files_with_matches":
            absolute_path = _resolve_search_result_path(line, execution_root)
            if absolute_path and not _has_read_permission(
                absolute_path, tool_name, actual_permission_context
            ):
                continue
            filtered.append(line)
            continue

        parsed = _extract_result_path_prefix(
            line,
            execution_root,
            output_mode=output_mode,
        )
        if parsed is None:
            continue

        absolute_path, _ = parsed
        if not _has_read_permission(absolute_path, tool_name, actual_permission_context):
            continue
        filtered.append(line)

    if output_mode != "content":
        return filtered

    cleaned: list[str] = []
    for line in filtered:
        if line != "--":
            cleaned.append(line)
            continue
        if not cleaned or cleaned[-1] == "--":
            continue
        cleaned.append(line)
    if cleaned and cleaned[-1] == "--":
        cleaned.pop()
    return cleaned


def _filter_file_results_by_permissions(
    results: list[str],
    *,
    execution_root: str,
    permission_context: ToolUseContext | Any | None,
    tool_name: str,
) -> list[str]:
    """Drop file-path results whose backing file is not read-allowed."""
    actual_permission_context = getattr(permission_context, "permission_context", permission_context)
    if actual_permission_context is None:
        return []

    filtered: list[str] = []
    for line in results:
        absolute_path = _resolve_search_result_path(line, execution_root)
        if absolute_path and not _has_read_permission(
            absolute_path, tool_name, actual_permission_context
        ):
            continue
        filtered.append(line)
    return filtered


# =====================================================================
# applyHeadLimit pagination helper
# =====================================================================

def _apply_head_limit(
    items: list,
    limit: int | None,
    offset: int = 0,
) -> tuple[list, int | None]:
    """Apply head_limit and offset pagination.

    Returns:
        (sliced_items, applied_limit_or_None)
        applied_limit is set only when truncation actually occurred,
        so the model knows there may be more results and can paginate.
    """
    # Explicit 0 = unlimited escape hatch
    if limit == 0:
        return items[offset:], None

    effective_limit = limit if limit is not None else DEFAULT_HEAD_LIMIT
    sliced = items[offset: offset + effective_limit]
    was_truncated = (len(items) - offset) > effective_limit
    return sliced, (effective_limit if was_truncated else None)


def _format_limit_info(
    applied_limit: int | None,
    applied_offset: int | None,
) -> str:
    """Format limit/offset information for display."""
    parts: list[str] = []
    if applied_limit is not None:
        parts.append(f"limit: {applied_limit}")
    if applied_offset:
        parts.append(f"offset: {applied_offset}")
    return ", ".join(parts)


# =====================================================================
# Glob base directory extraction
# =====================================================================

def _extract_glob_base_directory(pattern: str) -> tuple[str, str]:
    """Extract the static base directory from a glob pattern.

    The base directory is everything before the first glob special char
    (``*``, ``?``, ``[``, ``{``).

    Returns:
        (base_dir, relative_pattern)
    """
    glob_chars = re.search(r"[*?\[{]", pattern)

    if not glob_chars:
        # No glob characters — literal path
        return os.path.dirname(pattern), os.path.basename(pattern)

    # Get everything before the first glob character
    static_prefix = pattern[: glob_chars.start()]

    # Find the last path separator in the static prefix
    last_sep = max(static_prefix.rfind("/"), static_prefix.rfind(os.sep))

    if last_sep == -1:
        # No separator before the glob — relative to cwd
        return "", pattern

    base_dir = static_prefix[:last_sep]
    relative_pattern = pattern[last_sep + 1:]

    # Handle root directory (e.g. /*.txt)
    if base_dir == "" and last_sep == 0:
        base_dir = "/"

    return base_dir, relative_pattern


# =====================================================================
# GrepTool
# =====================================================================

_GREP_DESCRIPTION = """\
A powerful search tool built on ripgrep

Usage:
- Supports full regex syntax (e.g., "log.*Error", "function\\s+\\w+")
- Filter files with glob parameter (e.g., "*.js", "*.{ts,tsx}") or type parameter (e.g., "js", "py", "rust")
- Output modes: "content" shows matching lines (default), "files_with_matches" shows only file paths, "count" shows match counts
- Pattern syntax: Uses ripgrep (not grep) - literal braces need escaping (use interface\\{\\} to find interface{} in Go code)
- Multiline matching: By default patterns match within single lines only. For cross-line patterns like struct \\{[\\s\\S]*?field, use multiline: true
- Results are capped to several thousand output lines for responsiveness; when truncation occurs, the results report "at least" counts, but are otherwise accurate.\
"""

_GREP_PROMPT = _GREP_DESCRIPTION


class GrepTool(BaseTool):
    """Ripgrep-based code search."""

    _name = GREP_TOOL_NAME
    _description = _GREP_DESCRIPTION
    backend_type = BackendType.SHELL

    _is_read_only = True
    _is_concurrency_safe = True
    max_result_size_chars = 20_000
    search_hint = "search file contents with regex (ripgrep)"
    parameter_descriptions = {
        "pattern": "The regular expression pattern to search for in file contents",
        "path": "File or directory to search in (rg PATH). Defaults to current working directory.",
        "glob": 'Glob pattern to filter files (e.g. "*.js", "*.{ts,tsx}") - maps to rg --glob',
        "output_mode": 'Output mode: "content" shows matching lines (supports -A/-B/-C context, -n line numbers, head_limit), "files_with_matches" shows file paths (supports head_limit), "count" shows match counts (supports head_limit). Defaults to "files_with_matches".',
        "-B": 'Number of lines to show before each match (rg -B). Requires output_mode: "content", ignored otherwise.',
        "-A": 'Number of lines to show after each match (rg -A). Requires output_mode: "content", ignored otherwise.',
        "-C": "Alias for context.",
        "context": 'Number of lines to show before and after each match (rg -C). Requires output_mode: "content", ignored otherwise.',
        "-n": 'Show line numbers in output (rg -n). Requires output_mode: "content", ignored otherwise. Defaults to true.',
        "-i": "Case insensitive search (rg -i)",
        "head_limit": 'Limit output to first N lines/entries, equivalent to "| head -N". Works across all output modes: content (limits output lines), files_with_matches (limits file paths), count (limits count entries). Defaults to 250 when unspecified. Pass 0 for unlimited (use sparingly — large result sets waste context).',
        "offset": 'Skip first N lines/entries before applying head_limit, equivalent to "| tail -n +N | head -N". Works across all output modes. Defaults to 0.',
        "multiline": "Enable multiline mode where . matches newlines and patterns can span lines (rg -U --multiline-dotall). Default: false.",
        "type": "Optional ripgrep file type filter such as py, js, rust, or go.",
    }

    def __init__(self, session: ShellSession | None = None):
        self._session = session
        self._current_context: ToolUseContext | None = None
        super().__init__()

    def get_prompt(self) -> str:
        return _GREP_PROMPT

    def set_context(self, context: ToolUseContext) -> None:
        self._current_context = context

    # ------------------------------------------------------------------
    # Permission checks
    # ------------------------------------------------------------------
    async def check_permissions(self, input: dict, context: Any):
        """Read-permission check on the search root."""
        from openspace.grounding.core.permissions import (
            check_read_permission_for_tool,
            deny_missing_permission_context,
        )

        perm_ctx = getattr(context, "permission_context", None)
        if perm_ctx is None:
            return deny_missing_permission_context(self._name)

        cwd = _get_cwd(self._session, context)
        path = input.get("path") or cwd
        absolute_path = _expand_path(path, cwd)
        return check_read_permission_for_tool(
            tool_name=self._name,
            input_path=absolute_path,
            context=perm_ctx,
        )

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------
    async def validate_input(self, input: dict, context: Any = None) -> str | None:
        """Validate the path parameter if provided.

        Checks path existence, UNC bypass, and cwd-relative suggestions.
        """
        pattern = input.get("pattern")
        if not pattern:
            return "Missing required parameter: pattern"

        path = input.get("path")
        if not path:
            return None

        cwd = _get_cwd(self._session, context or self._current_context)
        absolute_path = _expand_path(path, cwd)

        # SECURITY: Skip filesystem operations for UNC paths
        if _is_unc_path(absolute_path):
            return None

        if not os.path.exists(absolute_path):
            cwd_suggestion = _suggest_path_under_cwd(absolute_path, cwd)
            message = (
                f"Path does not exist: {path}. "
                f"{FILE_NOT_FOUND_CWD_NOTE} {cwd}."
            )
            if cwd_suggestion:
                message += f" Did you mean {cwd_suggestion}?"
            return message

        return None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def _arun(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        output_mode: str = "files_with_matches",
        context: int | None = None,
        head_limit: int | None = None,
        offset: int = 0,
        multiline: bool = False,
        type: str | None = None,
        **kwargs,
    ) -> ToolResult:
        """Execute ripgrep search.

        Parameters match the GrepTool input schema. Additional rg flags
        (``-A``, ``-B``, ``-C``, ``-n``, ``-i``) are passed
        through **kwargs to allow flexible invocation.
        """
        context_before: int | None = kwargs.get("-B") or kwargs.get("context_before")
        context_after: int | None = kwargs.get("-A") or kwargs.get("context_after")
        context_c: int | None = kwargs.get("-C") or kwargs.get("context_c")
        show_line_numbers: bool = kwargs.get("-n", kwargs.get("show_line_numbers", True))
        case_insensitive: bool = kwargs.get("-i", kwargs.get("case_insensitive", False))
        fallback_context_before = 0
        fallback_context_after = 0
        if output_mode == "content":
            if context is not None:
                fallback_context_before = fallback_context_after = _coerce_nonnegative_int(context)
            elif context_c is not None:
                fallback_context_before = fallback_context_after = _coerce_nonnegative_int(context_c)
            else:
                fallback_context_before = _coerce_nonnegative_int(context_before)
                fallback_context_after = _coerce_nonnegative_int(context_after)

        cwd = _get_cwd(self._session, self._current_context)
        absolute_path = _expand_path(path, cwd) if path else cwd
        execution_root, target = _resolve_ripgrep_target(absolute_path)
        permission_context = getattr(self._current_context, "permission_context", None)

        # ── Build ripgrep arguments ──

        args: list[str] = ["--hidden"]

        # Exclude VCS directories
        for vcs_dir in VCS_DIRECTORIES_TO_EXCLUDE:
            args.extend(["--glob", f"!{vcs_dir}"])

        # Limit line length
        args.extend(["--max-columns", str(MAX_COLUMNS)])

        # Multiline mode
        if multiline:
            args.extend(["-U", "--multiline-dotall"])

        # Case insensitive
        if case_insensitive:
            args.append("-i")

        # Output mode flags
        if output_mode == "files_with_matches":
            args.append("-l")
        elif output_mode == "count":
            args.append("-c")
            args.append("-H")

        # Line numbers (content mode only)
        if show_line_numbers and output_mode == "content":
            args.append("-n")
        if output_mode == "content":
            args.append("-H")

        # Context flags (content mode only, -C/context takes precedence)
        if output_mode == "content":
            if context is not None:
                args.extend(["-C", str(context)])
            elif context_c is not None:
                args.extend(["-C", str(context_c)])
            else:
                if context_before is not None:
                    args.extend(["-B", str(context_before)])
                if context_after is not None:
                    args.extend(["-A", str(context_after)])

        # Pattern (use -e if starts with dash)
        if pattern.startswith("-"):
            args.extend(["-e", pattern])
        else:
            args.append(pattern)

        # Type filter
        if type:
            args.extend(["--type", type])

        # Glob filters: split on whitespace, then commas unless braces are used.
        glob_patterns: list[str] = []
        if glob:
            raw_patterns = glob.split()
            for raw in raw_patterns:
                if "{" in raw and "}" in raw:
                    glob_patterns.append(raw)
                else:
                    glob_patterns.extend(p for p in raw.split(",") if p)
            for gp in glob_patterns:
                if gp:
                    args.extend(["--glob", gp])

        _append_permission_ignore_globs(args, execution_root, permission_context)
        # This runtime has no plugin-cache path exclusions to add.

        # ── Execute ripgrep ──
        try:
            results = await _run_ripgrep(args, target, cwd=execution_root)
        except FileNotFoundError:
            try:
                results = _python_grep_files(
                    execution_root=execution_root,
                    target=target,
                    pattern=pattern,
                    output_mode=output_mode,
                    glob_patterns=glob_patterns,
                    type_filter=type,
                    case_insensitive=case_insensitive,
                    show_line_numbers=show_line_numbers,
                    context_before=fallback_context_before,
                    context_after=fallback_context_after,
                    multiline=multiline,
                    permission_context=permission_context,
                    tool_name=self._name,
                )
            except re.error as e:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    error=f"Invalid regular expression: {e}",
                )
            except OSError as e:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    error=f"Python grep fallback failed: {e}",
                )
            logger.info(
                "ripgrep not found; used Python grep fallback for %s",
                pattern,
            )
        except TimeoutError as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                error=str(e),
            )
        except RuntimeError as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                error=str(e),
            )

        results = _filter_grep_results_by_permissions(
            results,
            output_mode=output_mode,
            execution_root=execution_root,
            permission_context=permission_context,
            tool_name=self._name,
        )

        # ── Format results by output_mode ──

        if output_mode == "content":
            return self._format_content_mode(
                results, head_limit, offset, cwd, execution_root,
            )

        if output_mode == "count":
            return self._format_count_mode(
                results, head_limit, offset, cwd, execution_root,
            )

        # files_with_matches (default)
        return await self._format_files_mode(
            results, head_limit, offset, cwd, execution_root,
        )

    # ------------------------------------------------------------------
    # Result formatting helpers
    # ------------------------------------------------------------------

    def _format_content_mode(
        self,
        results: list[str],
        head_limit: int | None,
        offset: int,
        cwd: str,
        execution_root: str,
    ) -> ToolResult:
        """Format content mode output (matching lines)."""
        limited, applied_limit = _apply_head_limit(results, head_limit, offset)

        # Relativize paths in each line (format: /abs/path:line_content)
        final_lines: list[str] = []
        for line in limited:
            parsed = _extract_result_path_prefix(
                line,
                execution_root,
                output_mode="content",
            )
            if parsed is not None:
                file_path, split_idx = parsed
                rest = line[split_idx:]
                final_lines.append(_to_relative_path(file_path, cwd) + rest)
            else:
                final_lines.append(line)

        content = "\n".join(final_lines) if final_lines else "No matches found"

        applied_offset = offset if offset > 0 else None
        limit_info = _format_limit_info(applied_limit, applied_offset)
        if limit_info:
            content += f"\n\n[Showing results with pagination = {limit_info}]"

        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=content,
            metadata={
                "mode": "content",
                "num_lines": len(final_lines),
                **({"applied_limit": applied_limit} if applied_limit else {}),
                **({"applied_offset": offset} if offset > 0 else {}),
            },
        )

    def _format_count_mode(
        self,
        results: list[str],
        head_limit: int | None,
        offset: int,
        cwd: str,
        execution_root: str,
    ) -> ToolResult:
        """Format count mode output (filename:count per line)."""
        limited, applied_limit = _apply_head_limit(results, head_limit, offset)

        # Relativize and parse counts
        final_lines: list[str] = []
        total_matches = 0
        file_count = 0
        for line in limited:
            parsed = _extract_result_path_prefix(
                line,
                execution_root,
                output_mode="count",
            )
            if parsed is not None:
                file_path, split_idx = parsed
                count_str = line[split_idx + 1:]
                final_lines.append(
                    _to_relative_path(file_path, cwd) + ":" + count_str
                )
                try:
                    count = int(count_str)
                    total_matches += count
                    file_count += 1
                except ValueError:
                    pass
            else:
                final_lines.append(line)

        raw_content = "\n".join(final_lines) if final_lines else "No matches found"

        applied_offset = offset if offset > 0 else None
        limit_info = _format_limit_info(applied_limit, applied_offset)

        occ_word = "occurrence" if total_matches == 1 else "occurrences"
        file_word = "file" if file_count == 1 else "files"
        summary = (
            f"\n\nFound {total_matches} total {occ_word} "
            f"across {file_count} {file_word}."
        )
        if limit_info:
            summary += f" with pagination = {limit_info}"

        content = raw_content + summary

        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=content,
            metadata={
                "mode": "count",
                "num_files": file_count,
                "num_matches": total_matches,
                **({"applied_limit": applied_limit} if applied_limit else {}),
                **({"applied_offset": offset} if offset > 0 else {}),
            },
        )

    async def _format_files_mode(
        self,
        results: list[str],
        head_limit: int | None,
        offset: int,
        cwd: str,
        execution_root: str,
    ) -> ToolResult:
        """Format files_with_matches mode output (file paths sorted by mtime)."""
        # Get mtime for each file; failed stat entries sort last.
        mtime_pairs: list[tuple[str, float]] = []
        for fpath in results:
            absolute_path = _resolve_search_result_path(fpath, execution_root)
            try:
                st = os.stat(absolute_path)
                mtime_pairs.append((absolute_path, st.st_mtime))
            except OSError:
                mtime_pairs.append((absolute_path, 0.0))

        # Sort by mtime descending, filename ascending as tiebreaker
        mtime_pairs.sort(key=lambda pair: (-pair[1], pair[0]))

        sorted_paths = [p[0] for p in mtime_pairs]

        # Apply pagination
        limited, applied_limit = _apply_head_limit(sorted_paths, head_limit, offset)

        # Relativize paths
        relative_paths = [_to_relative_path(p, cwd) for p in limited]

        if not relative_paths:
            return ToolResult(
                status=ToolStatus.SUCCESS,
                content="No files found",
                metadata={"mode": "files_with_matches", "num_files": 0},
            )

        applied_offset = offset if offset > 0 else None
        limit_info = _format_limit_info(applied_limit, applied_offset)

        file_word = "file" if len(relative_paths) == 1 else "files"
        header = f"Found {len(relative_paths)} {file_word}"
        if limit_info:
            header += f" {limit_info}"

        content = header + "\n" + "\n".join(relative_paths)

        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=content,
            metadata={
                "mode": "files_with_matches",
                "num_files": len(relative_paths),
                "filenames": relative_paths,
                **({"applied_limit": applied_limit} if applied_limit else {}),
                **({"applied_offset": offset} if offset > 0 else {}),
            },
        )


# =====================================================================
# GlobTool
# =====================================================================

_GLOB_DESCRIPTION = """\
- Fast file pattern matching tool that works with any codebase size
- Supports glob patterns like "**/*.js" or "src/**/*.ts"
- Returns matching file paths sorted by modification time
- Use this tool when you need to find files by name patterns\
"""

_GLOB_PROMPT = _GLOB_DESCRIPTION


class GlobTool(BaseTool):
    """Glob-based file finder.

    Internally uses ``rg --files --glob <pattern> --sort=modified``.
    """

    _name = GLOB_TOOL_NAME
    _description = _GLOB_DESCRIPTION
    backend_type = BackendType.SHELL

    _is_read_only = True
    _is_concurrency_safe = True
    max_result_size_chars = 100_000
    search_hint = "find files by name pattern or wildcard"
    parameter_descriptions = {
        "pattern": "The glob pattern to match files against",
        "path": 'The directory to search in. If not specified, the current working directory will be used. IMPORTANT: Omit this field to use the default directory. DO NOT enter "undefined" or "null" - simply omit it for the default behavior. Must be a valid directory path if provided.',
    }

    def __init__(self, session: ShellSession | None = None):
        self._session = session
        self._current_context: ToolUseContext | None = None
        super().__init__()

    def get_prompt(self) -> str:
        return _GLOB_PROMPT

    def set_context(self, context: ToolUseContext) -> None:
        self._current_context = context

    # ------------------------------------------------------------------
    # Permission checks
    # ------------------------------------------------------------------
    async def check_permissions(self, input: dict, context: Any):
        from openspace.grounding.core.permissions import (
            check_read_permission_for_tool,
            deny_missing_permission_context,
        )

        perm_ctx = getattr(context, "permission_context", None)
        if perm_ctx is None:
            return deny_missing_permission_context(self._name)

        cwd = _get_cwd(self._session, context)
        absolute_path = _glob_permission_check_path(
            input.get("pattern") or "",
            input.get("path"),
            cwd,
        )
        return check_read_permission_for_tool(
            tool_name=self._name,
            input_path=absolute_path,
            context=perm_ctx,
        )

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------
    async def validate_input(self, input: dict, context: Any = None) -> str | None:
        """Validate path is a directory if provided.

        Checks path existence, directory type, UNC bypass, and cwd-relative
        suggestions.
        """
        pattern = input.get("pattern")
        if not pattern:
            return "Missing required parameter: pattern"

        cwd = _get_cwd(self._session, context or self._current_context)
        path = input.get("path")
        if not path and not os.path.isabs(pattern):
            return None

        absolute_path, _ = _resolve_glob_search(pattern, path, cwd)
        display_path = path or pattern

        # SECURITY: Skip filesystem operations for UNC paths
        if _is_unc_path(absolute_path):
            return None

        if not os.path.exists(absolute_path):
            cwd_suggestion = _suggest_path_under_cwd(absolute_path, cwd)
            message = (
                f"Directory does not exist: {display_path}. "
                f"{FILE_NOT_FOUND_CWD_NOTE} {cwd}."
            )
            if cwd_suggestion:
                message += f" Did you mean {cwd_suggestion}?"
            return message

        if not os.path.isdir(absolute_path):
            return f"Path is not a directory: {display_path}"

        return None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def _arun(
        self,
        pattern: str,
        path: str | None = None,
    ) -> ToolResult:
        """Execute glob file search using ripgrep --files."""
        start = time.time()
        cwd = _get_cwd(self._session, self._current_context)
        limit = DEFAULT_GLOB_LIMIT

        search_dir, search_pattern = _resolve_glob_search(pattern, path, cwd)
        permission_context = getattr(self._current_context, "permission_context", None)

        # Build rg args
        # --files: list files instead of searching content
        # --glob: filter by pattern
        # --sort=modified: sort by modification time (oldest first)
        # --no-ignore: don't respect .gitignore
        # --hidden: include hidden files
        args = [
            "--files",
            "--glob", search_pattern,
            "--sort=modified",
            "--no-ignore",
            "--hidden",
        ]

        _append_permission_ignore_globs(args, search_dir, permission_context)
        # This runtime has no plugin-cache path exclusions to add.

        search_backend = "ripgrep"
        try:
            all_paths = await _run_ripgrep(args, ".", cwd=search_dir)
        except FileNotFoundError:
            absolute_paths = _python_glob_files(
                search_dir,
                search_pattern,
                permission_context,
                self._name,
            )
            search_backend = "python_fallback"
        except TimeoutError as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                error=str(e),
            )
        except RuntimeError as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                error=str(e),
            )
        else:
            all_paths = _filter_file_results_by_permissions(
                all_paths,
                execution_root=search_dir,
                permission_context=permission_context,
                tool_name=self._name,
            )

            # Convert relative paths from rg to absolute
            absolute_paths = [
                _resolve_search_result_path(p, search_dir)
                for p in all_paths
            ]

        truncated = len(absolute_paths) > limit
        files = absolute_paths[:limit]

        # Relativize for token savings
        filenames = [_to_relative_path(f, cwd) for f in files]

        duration_ms = (time.time() - start) * 1000

        # Format output
        if not filenames:
            content = "No files found"
        else:
            parts = list(filenames)
            if truncated:
                parts.append(
                    "(Results are truncated. Consider using a more specific "
                    "path or pattern.)"
                )
            content = "\n".join(parts)

        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=content,
            metadata={
                "duration_ms": round(duration_ms, 1),
                "num_files": len(filenames),
                "filenames": filenames,
                "truncated": truncated,
                "search_backend": search_backend,
            },
        )


# =====================================================================
# ListDirTool
# =====================================================================

class ListDirTool(BaseTool):
    """List directory contents with direct filesystem APIs.

    Provides a compact, human-friendly view with name, size, mtime, and
    permissions. It is implemented directly instead of shelling out.
    """

    _name = LIST_DIR_TOOL_NAME
    _description = (
        "List the contents of a directory. "
        "Returns file names, sizes, and modification dates. "
        "Defaults to the current directory if no path is given."
    )
    backend_type = BackendType.SHELL

    _is_read_only = True
    _is_concurrency_safe = True

    def __init__(self, session: ShellSession | None = None):
        self._session = session
        self._current_context: ToolUseContext | None = None
        super().__init__()

    def set_context(self, context: ToolUseContext) -> None:
        self._current_context = context

    async def check_permissions(self, input: dict, context: Any):
        """Read-permission check on the listed directory."""
        from openspace.grounding.core.permissions import (
            check_read_permission_for_tool,
            deny_missing_permission_context,
        )

        perm_ctx = getattr(context, "permission_context", None)
        if perm_ctx is None:
            return deny_missing_permission_context(self._name)

        absolute_path = _resolve_list_dir_path(
            input.get("path"),
            _get_cwd(self._session, context),
        )
        return check_read_permission_for_tool(
            tool_name=self._name,
            input_path=absolute_path,
            context=perm_ctx,
        )

    async def _arun(self, path: str = ".") -> ToolResult:
        directory = _resolve_list_dir_path(
            path, _get_cwd(self._session, self._current_context)
        )
        permission_context = getattr(self._current_context, "permission_context", None)
        try:
            if not os.path.exists(directory):
                return ToolResult(
                    status=ToolStatus.ERROR,
                    content=f"Cannot list directory: {path}",
                )
            if not _has_read_permission(directory, self._name, permission_context):
                return ToolResult(
                    status=ToolStatus.ERROR,
                    content=f"Permission denied: {path}",
                )
            if not os.path.isdir(directory):
                return ToolResult(
                    status=ToolStatus.ERROR,
                    content=f"Path is not a directory: {path}",
                )

            entries: list[tuple[str, str]] = [
                (".", directory),
                ("..", os.path.dirname(directory) or directory),
            ]
            with os.scandir(directory) as scan:
                for entry in sorted(scan, key=lambda item: item.name.casefold()):
                    if not _has_read_permission(entry.path, self._name, permission_context):
                        continue
                    entries.append((entry.name, entry.path))

            lines: list[str] = []
            for name, entry_path in entries:
                if name == ".." and not _has_read_permission(
                    entry_path, self._name, permission_context
                ):
                    continue
                st = os.lstat(entry_path)
                display_name = name
                if stat.S_ISDIR(st.st_mode) and name not in (".", ".."):
                    display_name = f"{name}/"
                elif stat.S_ISLNK(st.st_mode):
                    display_name = f"{name} -> {os.readlink(entry_path)}"

                mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                lines.append(
                    f"{stat.filemode(st.st_mode)} {st.st_size:>10} {mtime} {display_name}"
                )

            return ToolResult(
                status=ToolStatus.SUCCESS,
                content="\n".join(lines),
            )
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"ls failed: {e}",
            )
