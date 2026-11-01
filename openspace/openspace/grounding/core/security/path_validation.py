"""Path-level data and helpers for bash permission checks.

The classifier and permission layers use these tables to extract file-system
targets from shell subcommands, identify read/write/create operations, strip
safe wrappers, expand ``~``, and detect dangerous removal targets.

The full context-aware permission decisions are implemented in
:mod:`openspace.grounding.core.permissions.bash_path_validation`; this module
stays stateless and parser-focused.
"""

from __future__ import annotations

import os
import os.path
import re
from typing import Any, Callable, Literal, Optional

from .shell_parser import try_parse_shell_command


FileOperationType = Literal["read", "write", "create"]


__all__ = [
    "FileOperationType",
    "PATH_EXTRACTORS",
    "COMMAND_OPERATION_TYPE",
    "SUPPORTED_PATH_COMMANDS",
    "NON_CREATING_WRITE_COMMANDS",
    "filter_out_flags",
    "parse_pattern_command",
    "expand_tilde",
    "is_dangerous_removal_path",
    "strip_wrappers_from_argv",
    "extract_write_paths_from_subcommand",
    "check_dangerous_removal_paths",
]


# ─────────────────────────────────────────────────────────────────────
#  expand_tilde & is_dangerous_removal_path  (utils/permissions/pathValidation.ts)
# ─────────────────────────────────────────────────────────────────────


def expand_tilde(path: str) -> str:
    """OpenSpace ``expandTilde`` (L80-89).  Only ``~`` and ``~/`` handled."""
    if path == "~" or path.startswith("~/"):
        return os.path.expanduser("~") + path[1:]
    # OpenSpace also handles ~\ on Windows; keep for parity (no-op on posix).
    if os.sep == "\\" and path.startswith("~\\"):
        return os.path.expanduser("~") + path[1:]
    return path


_WINDOWS_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:/?$")
_WINDOWS_DRIVE_CHILD_RE = re.compile(r"^[A-Za-z]:/[^/]+$")


def is_dangerous_removal_path(resolved_path: str) -> bool:
    """OpenSpace ``isDangerousRemovalPath`` (L331-367).

    Returns True for ``/``, ``~`` (resolved to ``$HOME``), direct
    children of ``/`` (``/tmp`` etc.), Windows drive roots, glob-all
    patterns, and ``C:\\X`` Windows drive children.
    """
    forward = re.sub(r"[\\/]+", "/", resolved_path)

    if forward == "*" or forward.endswith("/*"):
        return True

    normalized = forward if forward == "/" else re.sub(r"/$", "", forward)

    if normalized == "/":
        return True

    if _WINDOWS_DRIVE_ROOT_RE.match(normalized):
        return True

    home = re.sub(r"[\\/]+", "/", os.path.expanduser("~"))
    if normalized == home:
        return True

    # Direct child of /  (e.g. /tmp, /etc)
    parent = os.path.dirname(normalized)
    if parent == "/":
        return True

    if _WINDOWS_DRIVE_CHILD_RE.match(normalized):
        return True

    return False


# ─────────────────────────────────────────────────────────────────────
#  filter_out_flags — OpenSpace pathValidation.ts L126-139
# ─────────────────────────────────────────────────────────────────────


def filter_out_flags(args: list[str]) -> list[str]:
    """OpenSpace ``filterOutFlags``.  Handles the POSIX ``--`` delimiter."""
    result: list[str] = []
    after_dd = False
    for arg in args:
        if after_dd:
            result.append(arg)
        elif arg == "--":
            after_dd = True
        elif arg and not arg.startswith("-"):
            result.append(arg)
    return result


def parse_pattern_command(
    args: list[str],
    flags_with_args: set[str],
    defaults: Optional[list[str]] = None,
) -> list[str]:
    """OpenSpace ``parsePatternCommand`` (grep/rg/jq shape)."""
    paths: list[str] = []
    pattern_found = False
    after_dd = False

    defaults = defaults or []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg is None:
            i += 1
            continue

        if not after_dd and arg == "--":
            after_dd = True
            i += 1
            continue

        if not after_dd and arg.startswith("-"):
            flag = arg.split("=", 1)[0]
            if flag and flag in ("-e", "--regexp", "-f", "--file"):
                pattern_found = True
            if flag and flag in flags_with_args and "=" not in arg:
                i += 2
                continue
            i += 1
            continue

        if not pattern_found:
            pattern_found = True
            i += 1
            continue

        paths.append(arg)
        i += 1

    return paths if paths else defaults


# ─────────────────────────────────────────────────────────────────────
#  Individual extractors
# ─────────────────────────────────────────────────────────────────────


def _extract_cd(args: list[str]) -> list[str]:
    return [os.path.expanduser("~")] if not args else [" ".join(args)]


def _extract_ls(args: list[str]) -> list[str]:
    paths = filter_out_flags(args)
    return paths if paths else ["."]


_FIND_PATH_FLAGS = {
    "-newer",
    "-anewer",
    "-cnewer",
    "-mnewer",
    "-samefile",
    "-path",
    "-wholename",
    "-ilname",
    "-lname",
    "-ipath",
    "-iwholename",
}
_FIND_NEWER_PATTERN = re.compile(r"^-newer[acmBt][acmtB]$")


def _extract_find(args: list[str]) -> list[str]:
    paths: list[str] = []
    found_non_global_flag = False
    after_dd = False

    i = 0
    while i < len(args):
        arg = args[i]
        if not arg:
            i += 1
            continue
        if after_dd:
            paths.append(arg)
            i += 1
            continue
        if arg == "--":
            after_dd = True
            i += 1
            continue
        if arg.startswith("-"):
            if arg in ("-H", "-L", "-P"):
                i += 1
                continue
            found_non_global_flag = True
            if arg in _FIND_PATH_FLAGS or _FIND_NEWER_PATTERN.match(arg):
                if i + 1 < len(args):
                    paths.append(args[i + 1])
                    i += 2
                    continue
            i += 1
            continue

        if not found_non_global_flag:
            paths.append(arg)
        i += 1

    return paths if paths else ["."]


def _extract_tr(args: list[str]) -> list[str]:
    has_delete = any(
        a == "-d"
        or a == "--delete"
        or (a.startswith("-") and "d" in a)
        for a in args
    )
    non_flags = filter_out_flags(args)
    return non_flags[1 if has_delete else 2:]


_GREP_FLAGS_WITH_ARGS = {
    "-e",
    "--regexp",
    "-f",
    "--file",
    "--exclude",
    "--include",
    "--exclude-dir",
    "--include-dir",
    "-m",
    "--max-count",
    "-A",
    "--after-context",
    "-B",
    "--before-context",
    "-C",
    "--context",
}


def _extract_grep(args: list[str]) -> list[str]:
    paths = parse_pattern_command(args, _GREP_FLAGS_WITH_ARGS)
    if not paths and any(a in ("-r", "-R", "--recursive") for a in args):
        return ["."]
    return paths


_RG_FLAGS_WITH_ARGS = {
    "-e",
    "--regexp",
    "-f",
    "--file",
    "-t",
    "--type",
    "-T",
    "--type-not",
    "-g",
    "--glob",
    "-m",
    "--max-count",
    "--max-depth",
    "-r",
    "--replace",
    "-A",
    "--after-context",
    "-B",
    "--before-context",
    "-C",
    "--context",
}


def _extract_rg(args: list[str]) -> list[str]:
    return parse_pattern_command(args, _RG_FLAGS_WITH_ARGS, ["."])


def _extract_sed(args: list[str]) -> list[str]:
    paths: list[str] = []
    skip_next = False
    script_found = False
    after_dd = False

    i = 0
    while i < len(args):
        if skip_next:
            skip_next = False
            i += 1
            continue

        arg = args[i]
        if not arg:
            i += 1
            continue

        if not after_dd and arg == "--":
            after_dd = True
            i += 1
            continue

        if not after_dd and arg.startswith("-"):
            if arg in ("-f", "--file"):
                if i + 1 < len(args):
                    paths.append(args[i + 1])
                    skip_next = True
                script_found = True
            elif arg in ("-e", "--expression"):
                skip_next = True
                script_found = True
            elif "e" in arg or "f" in arg:
                script_found = True
            i += 1
            continue

        if not script_found:
            script_found = True
            i += 1
            continue

        paths.append(arg)
        i += 1

    return paths


_JQ_FLAGS_WITH_ARGS = {
    "-e",
    "--expression",
    "-f",
    "--from-file",
    "--arg",
    "--argjson",
    "--slurpfile",
    "--rawfile",
    "--args",
    "--jsonargs",
    "-L",
    "--library-path",
    "--indent",
    "--tab",
}


def _extract_jq(args: list[str]) -> list[str]:
    paths: list[str] = []
    filter_found = False
    after_dd = False

    i = 0
    while i < len(args):
        arg = args[i]
        if arg is None:
            i += 1
            continue

        if not after_dd and arg == "--":
            after_dd = True
            i += 1
            continue

        if not after_dd and arg.startswith("-"):
            flag = arg.split("=", 1)[0]
            if flag in ("-e", "--expression"):
                filter_found = True
            if flag in _JQ_FLAGS_WITH_ARGS and "=" not in arg:
                i += 2
                continue
            i += 1
            continue

        if not filter_found:
            filter_found = True
            i += 1
            continue

        paths.append(arg)
        i += 1

    return paths


def _extract_git(args: list[str]) -> list[str]:
    """OpenSpace git extractor (L491-508).

    Only ``git diff --no-index`` escapes the repo sandbox, so only that
    case returns real paths.
    """
    if args and args[0] == "diff" and "--no-index" in args:
        file_paths = filter_out_flags(list(args[1:]))
        return file_paths[:2]
    return []


_SimpleExtractor = Callable[[list[str]], list[str]]

PATH_EXTRACTORS: dict[str, _SimpleExtractor] = {
    "cd": _extract_cd,
    "ls": _extract_ls,
    "find": _extract_find,
    "mkdir": filter_out_flags,
    "touch": filter_out_flags,
    "rm": filter_out_flags,
    "rmdir": filter_out_flags,
    "mv": filter_out_flags,
    "cp": filter_out_flags,
    "cat": filter_out_flags,
    "head": filter_out_flags,
    "tail": filter_out_flags,
    "sort": filter_out_flags,
    "uniq": filter_out_flags,
    "wc": filter_out_flags,
    "cut": filter_out_flags,
    "paste": filter_out_flags,
    "column": filter_out_flags,
    "tr": _extract_tr,
    "file": filter_out_flags,
    "stat": filter_out_flags,
    "diff": filter_out_flags,
    "awk": filter_out_flags,
    "strings": filter_out_flags,
    "hexdump": filter_out_flags,
    "od": filter_out_flags,
    "base64": filter_out_flags,
    "nl": filter_out_flags,
    "grep": _extract_grep,
    "rg": _extract_rg,
    "sed": _extract_sed,
    "git": _extract_git,
    "jq": _extract_jq,
    "sha256sum": filter_out_flags,
    "sha1sum": filter_out_flags,
    "md5sum": filter_out_flags,
}


SUPPORTED_PATH_COMMANDS = tuple(PATH_EXTRACTORS.keys())


# ─────────────────────────────────────────────────────────────────────
#  COMMAND_OPERATION_TYPE
# ─────────────────────────────────────────────────────────────────────

COMMAND_OPERATION_TYPE: dict[str, FileOperationType] = {
    "cd": "read",
    "ls": "read",
    "find": "read",
    "mkdir": "create",
    "touch": "create",
    "rm": "write",
    "rmdir": "write",
    "mv": "write",
    "cp": "write",
    "cat": "read",
    "head": "read",
    "tail": "read",
    "sort": "read",
    "uniq": "read",
    "wc": "read",
    "cut": "read",
    "paste": "read",
    "column": "read",
    "tr": "read",
    "file": "read",
    "stat": "read",
    "diff": "read",
    "awk": "read",
    "strings": "read",
    "hexdump": "read",
    "od": "read",
    "base64": "read",
    "nl": "read",
    "grep": "read",
    "rg": "read",
    "sed": "write",
    "git": "read",
    "jq": "read",
    "sha256sum": "read",
    "sha1sum": "read",
    "md5sum": "read",
}


# Commands that write but cannot create new files at new paths.
# OpenSpace defines this in readOnlyValidation.ts L1788 and uses it in
# extractWritePathsFromSubcommand (L1813-1815).
NON_CREATING_WRITE_COMMANDS = frozenset({"rm", "rmdir", "sed"})


# ─────────────────────────────────────────────────────────────────────
#  strip_wrappers_from_argv
# ─────────────────────────────────────────────────────────────────────


_TIMEOUT_FLAG_VALUE_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_TIMEOUT_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_TIMEOUT_SIG_FUSED_RE = re.compile(r"^--(?:kill-after|signal)=[A-Za-z0-9_.+-]+$")
_TIMEOUT_SHORT_FUSED_RE = re.compile(r"^-[ks][A-Za-z0-9_.+-]+$")
_STDBUF_SHORT_STD_RE = re.compile(r"^-[ioe]$")
_STDBUF_SHORT_FUSED_RE = re.compile(r"^-[ioe].")
_STDBUF_LONG_RE = re.compile(r"^--(input|output|error)=")
_INT_RE = re.compile(r"^-?\d+$")
_NICE_LEGACY_RE = re.compile(r"^-\d+$")


def _skip_timeout_flags(a: list[str]) -> int:
    """OpenSpace ``skipTimeoutFlags`` (L1183-1218)."""
    i = 1
    while i < len(a):
        arg = a[i]
        nxt = a[i + 1] if i + 1 < len(a) else None
        if arg in ("--foreground", "--preserve-status", "--verbose"):
            i += 1
        elif _TIMEOUT_SIG_FUSED_RE.match(arg):
            i += 1
        elif (
            arg in ("--kill-after", "--signal")
            and nxt is not None
            and _TIMEOUT_FLAG_VALUE_RE.match(nxt)
        ):
            i += 2
        elif arg == "--":
            i += 1
            break
        elif arg.startswith("--"):
            return -1
        elif arg == "-v":
            i += 1
        elif (
            arg in ("-k", "-s")
            and nxt is not None
            and _TIMEOUT_FLAG_VALUE_RE.match(nxt)
        ):
            i += 2
        elif _TIMEOUT_SHORT_FUSED_RE.match(arg):
            i += 1
        elif arg.startswith("-"):
            return -1
        else:
            break
    return i


def _skip_stdbuf_flags(a: list[str]) -> int:
    """OpenSpace ``skipStdbufFlags`` (L1225-1237)."""
    i = 1
    while i < len(a):
        arg = a[i]
        nxt = a[i + 1] if i + 1 < len(a) else None
        if _STDBUF_SHORT_STD_RE.match(arg) and nxt:
            i += 2
        elif _STDBUF_SHORT_FUSED_RE.match(arg):
            i += 1
        elif _STDBUF_LONG_RE.match(arg):
            i += 1
        elif arg.startswith("-"):
            return -1
        else:
            break
    return i if i > 1 and i < len(a) else -1


def _skip_env_flags(a: list[str]) -> int:
    """OpenSpace ``skipEnvFlags`` (L1244-1256)."""
    i = 1
    while i < len(a):
        arg = a[i]
        if "=" in arg and not arg.startswith("-"):
            i += 1
        elif arg in ("-i", "-0", "-v"):
            i += 1
        elif arg == "-u" and i + 1 < len(a):
            i += 2
        elif arg.startswith("-"):
            return -1
        else:
            break
    return i if i < len(a) else -1


def strip_wrappers_from_argv(argv: list[str]) -> list[str]:
    """OpenSpace ``stripWrappersFromArgv`` (L1263-1303).

    Iteratively strips ``time``/``nohup``/``timeout``/``nice``/
    ``stdbuf``/``env`` prefix wrappers (with flag parsing).
    """
    a = list(argv)
    while True:
        if not a:
            return a
        head = a[0]
        if head in ("time", "nohup"):
            a = a[2:] if len(a) > 1 and a[1] == "--" else a[1:]
        elif head == "timeout":
            i = _skip_timeout_flags(a)
            if i < 0 or i >= len(a) or not _TIMEOUT_DURATION_RE.match(a[i]):
                return a
            a = a[i + 1:]
        elif head == "nice":
            if len(a) > 2 and a[1] == "-n" and _INT_RE.match(a[2]):
                a = a[4:] if len(a) > 3 and a[3] == "--" else a[3:]
            elif len(a) > 1 and _NICE_LEGACY_RE.match(a[1]):
                a = a[3:] if len(a) > 2 and a[2] == "--" else a[2:]
            else:
                a = a[2:] if len(a) > 1 and a[1] == "--" else a[1:]
        elif head == "stdbuf":
            i = _skip_stdbuf_flags(a)
            if i < 0:
                return a
            a = a[i:]
        elif head == "env":
            i = _skip_env_flags(a)
            if i < 0:
                return a
            a = a[i:]
        else:
            return a


# ─────────────────────────────────────────────────────────────────────
#  extract_write_paths_from_subcommand — OpenSpace readOnlyValidation.ts L1795-1823
# ─────────────────────────────────────────────────────────────────────


def extract_write_paths_from_subcommand(subcommand: str) -> list[str]:
    """OpenSpace ``extractWritePathsFromSubcommand``.

    Only commands with ``operationType`` ∈ ``{write, create}`` that
    aren't in :data:`NON_CREATING_WRITE_COMMANDS` are considered — the
    rest write in-place or delete existing files and don't create new
    files at attacker-chosen paths.

    The argv is stripped of leading wrappers so ``timeout 5 mkdir -p
    hooks`` still surfaces ``hooks`` as a write target.
    """
    parse_result = try_parse_shell_command(subcommand)
    if not parse_result.success:
        return []

    tokens = [t for t in parse_result.tokens if isinstance(t, str)]
    if not tokens:
        return []

    tokens = strip_wrappers_from_argv(tokens)
    if not tokens:
        return []

    base_cmd = tokens[0]
    if base_cmd not in COMMAND_OPERATION_TYPE:
        return []

    op = COMMAND_OPERATION_TYPE[base_cmd]
    if op not in ("write", "create") or base_cmd in NON_CREATING_WRITE_COMMANDS:
        return []

    extractor = PATH_EXTRACTORS.get(base_cmd)
    if extractor is None:
        return []

    return extractor(list(tokens[1:]))


# ─────────────────────────────────────────────────────────────────────
#  check_dangerous_removal_paths — OpenSpace pathValidation.ts L70-108
# ─────────────────────────────────────────────────────────────────────


_SURROUNDING_QUOTES_RE = re.compile(r"^['\"]|['\"]$")


def check_dangerous_removal_paths(
    command: Literal["rm", "rmdir"],
    args: list[str],
    cwd: str,
) -> dict[str, Any]:
    """OpenSpace ``checkDangerousRemovalPaths``.

    Returns a ``PermissionResult``-shaped dict (``behavior`` ∈
    ``{ask, passthrough}``) signalling whether any extracted target is
    dangerous (``/``, ``~``, ``/tmp`` etc.).  Symlinks are **not**
    resolved here — ``/tmp`` must be caught on macOS even though it
    symlinks to ``/private/tmp``.
    """
    extractor = PATH_EXTRACTORS.get(command)
    if extractor is None:
        return {
            "behavior": "passthrough",
            "message": f"No extractor for {command}",
        }

    for path in extractor(args):
        clean = expand_tilde(_SURROUNDING_QUOTES_RE.sub("", path))
        absolute = (
            clean
            if os.path.isabs(clean)
            else os.path.normpath(os.path.join(cwd, clean))
        )
        if is_dangerous_removal_path(absolute):
            return {
                "behavior": "ask",
                "message": (
                    f"Dangerous {command} operation detected: '{absolute}'\n\n"
                    "This command would remove a critical system directory. "
                    "This requires explicit approval and cannot be auto-allowed "
                    "by permission rules."
                ),
                "blockedPath": absolute,
            }

    return {
        "behavior": "passthrough",
        "message": f"No dangerous removals detected for {command} command",
    }
