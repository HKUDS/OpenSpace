"""Shared read-only command data tables.

This module defines cross-shell command configurations used by BashTool and
future shell tool integrations:

- :data:`GIT_READ_ONLY_COMMANDS` — git subcommands
- :data:`GH_READ_ONLY_COMMANDS` — gh CLI subcommands
- :data:`DOCKER_READ_ONLY_COMMANDS` — docker logs / docker inspect
- :data:`RIPGREP_READ_ONLY_COMMANDS` — rg safe flags
- :data:`PYRIGHT_READ_ONLY_COMMANDS` — pyright (``respects_double_dash=False``)
- :data:`EXTERNAL_READONLY_COMMANDS` — ``docker ps`` / ``docker images``
  cross-shell read-only commands.

These tables are merged into the :data:`bash_classifier._COMMAND_ALLOWLIST`
via :func:`bash_classifier.register_shared_commands`.

Callbacks are module-level pure functions with no closure state. The inline
security comments document why particular flags are intentionally excluded.
"""

from __future__ import annotations

import re

from .readonly_commands import CommandConfig, FlagArgType


__all__ = [
    "GIT_READ_ONLY_COMMANDS",
    "GH_READ_ONLY_COMMANDS",
    "DOCKER_READ_ONLY_COMMANDS",
    "RIPGREP_READ_ONLY_COMMANDS",
    "PYRIGHT_READ_ONLY_COMMANDS",
    "EXTERNAL_READONLY_COMMANDS",
    "build_shared_allowlist",
]


# ─────────────────────────────────────────────────────────────────────
#  Shared git flag groups
# ─────────────────────────────────────────────────────────────────────

GIT_REF_SELECTION_FLAGS: dict[str, FlagArgType] = {
    "--all": "none",
    "--branches": "none",
    "--tags": "none",
    "--remotes": "none",
}

GIT_DATE_FILTER_FLAGS: dict[str, FlagArgType] = {
    "--since": "string",
    "--after": "string",
    "--until": "string",
    "--before": "string",
}

GIT_LOG_DISPLAY_FLAGS: dict[str, FlagArgType] = {
    "--oneline": "none",
    "--graph": "none",
    "--decorate": "none",
    "--no-decorate": "none",
    "--date": "string",
    "--relative-date": "none",
}

GIT_COUNT_FLAGS: dict[str, FlagArgType] = {
    "--max-count": "number",
    "-n": "number",
}

GIT_STAT_FLAGS: dict[str, FlagArgType] = {
    "--stat": "none",
    "--numstat": "none",
    "--shortstat": "none",
    "--name-only": "none",
    "--name-status": "none",
}

GIT_COLOR_FLAGS: dict[str, FlagArgType] = {
    "--color": "none",
    "--no-color": "none",
}

GIT_PATCH_FLAGS: dict[str, FlagArgType] = {
    "--patch": "none",
    "-p": "none",
    "--no-patch": "none",
    "--no-ext-diff": "none",
    "-s": "none",
}

GIT_AUTHOR_FILTER_FLAGS: dict[str, FlagArgType] = {
    "--author": "string",
    "--committer": "string",
    "--grep": "string",
}


# ─────────────────────────────────────────────────────────────────────
#  Git callbacks (OpenSpace inline arrow functions)
# ─────────────────────────────────────────────────────────────────────


def _git_reflog_callback(_raw: str, args: list[str]) -> bool:
    """OpenSpace ``git reflog`` callback (L283-303).

    Blocks ``git reflog expire|delete|exists`` — those write to
    ``.git/logs/**``.  Allows bare / ``show`` / positional ref names.
    """
    dangerous = {"expire", "delete", "exists"}
    for token in args:
        if not token or token.startswith("-"):
            continue
        if token in dangerous:
            return True
        # first positional is safe (show/HEAD/ref); subsequent are ref args
        return False
    return False


def _git_remote_show_callback(_raw: str, args: list[str]) -> bool:
    """OpenSpace ``git remote show`` callback (L478-487).

    Requires exactly one alphanumeric remote name (after stripping
    ``-n``).
    """
    positional = [a for a in args if a != "-n"]
    if len(positional) != 1:
        return True
    return not re.fullmatch(r"[a-zA-Z0-9_-]+", positional[0])


def _git_remote_callback(_raw: str, args: list[str]) -> bool:
    """OpenSpace ``git remote`` callback (L494-501) — only ``-v/--verbose`` allowed."""
    for a in args:
        if a not in ("-v", "--verbose"):
            return True
    return False


def _short_flag_bundle_includes_letter(token: str, letter: str) -> bool:
    """True if *token* is a short-flag bundle (``-li``) that contains *letter*."""
    return (
        len(token) > 2
        and token[0] == "-"
        and token[1] != "-"
        and "=" not in token
        and letter in token[1:]
    )


def _git_tag_callback(_raw: str, args: list[str]) -> bool:
    """OpenSpace ``git tag`` callback (L739-805).

    Blocks tag creation via positional arg.  Safe only when:
      - no positional args, OR
      - positional preceded by ``-l``/``--list``/``--contains``/etc.
    """
    flags_with_args = {
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
        "--sort",
        "--format",
        "-n",
    }
    i = 0
    seen_list_flag = False
    seen_dash_dash = False
    while i < len(args):
        token = args[i]
        if not token:
            i += 1
            continue
        if token == "--" and not seen_dash_dash:
            seen_dash_dash = True
            i += 1
            continue
        if not seen_dash_dash and token.startswith("-"):
            if token in ("--list", "-l"):
                seen_list_flag = True
            elif _short_flag_bundle_includes_letter(token, "l"):
                seen_list_flag = True
            if "=" in token:
                i += 1
            elif token in flags_with_args:
                i += 2
            else:
                i += 1
        else:
            if not seen_list_flag:
                return True
            i += 1
    return False


def _git_branch_callback(_raw: str, args: list[str]) -> bool:
    """OpenSpace ``git branch`` callback (L851-920).

    Blocks branch creation via positional arg.  Safe uses: no positional,
    ``-l <pattern>``, or ``--contains/--merged <ref>`` filtering.
    """
    flags_with_args = {
        "--contains",
        "--no-contains",
        "--points-at",
        "--sort",
    }
    flags_with_optional_args = {"--merged", "--no-merged"}
    i = 0
    last_flag = ""
    seen_list_flag = False
    seen_dash_dash = False
    while i < len(args):
        token = args[i]
        if not token:
            i += 1
            continue
        if token == "--" and not seen_dash_dash:
            seen_dash_dash = True
            last_flag = ""
            i += 1
            continue
        if not seen_dash_dash and token.startswith("-"):
            if token in ("--list", "-l"):
                seen_list_flag = True
            elif _short_flag_bundle_includes_letter(token, "l"):
                seen_list_flag = True
            if "=" in token:
                last_flag = token.split("=", 1)[0]
                i += 1
            elif token in flags_with_args:
                last_flag = token
                i += 2
            else:
                last_flag = token
                i += 1
        else:
            last_flag_has_optional = last_flag in flags_with_optional_args
            if not seen_list_flag and not last_flag_has_optional:
                return True
            i += 1
    return False


# ─────────────────────────────────────────────────────────────────────
#  GIT_READ_ONLY_COMMANDS — 23 subcommands
# ─────────────────────────────────────────────────────────────────────

GIT_READ_ONLY_COMMANDS: dict[str, CommandConfig] = {
    "git diff": {
        "safeFlags": {
            **GIT_STAT_FLAGS,
            **GIT_COLOR_FLAGS,
            "--dirstat": "none",
            "--summary": "none",
            "--patch-with-stat": "none",
            "--word-diff": "none",
            "--word-diff-regex": "string",
            "--color-words": "none",
            "--no-renames": "none",
            "--no-ext-diff": "none",
            "--check": "none",
            "--ws-error-highlight": "string",
            "--full-index": "none",
            "--binary": "none",
            "--abbrev": "number",
            "--break-rewrites": "none",
            "--find-renames": "none",
            "--find-copies": "none",
            "--find-copies-harder": "none",
            "--irreversible-delete": "none",
            "--diff-algorithm": "string",
            "--histogram": "none",
            "--patience": "none",
            "--minimal": "none",
            "--ignore-space-at-eol": "none",
            "--ignore-space-change": "none",
            "--ignore-all-space": "none",
            "--ignore-blank-lines": "none",
            "--inter-hunk-context": "number",
            "--function-context": "none",
            "--exit-code": "none",
            "--quiet": "none",
            "--cached": "none",
            "--staged": "none",
            "--pickaxe-regex": "none",
            "--pickaxe-all": "none",
            "--no-index": "none",
            "--relative": "string",
            "--diff-filter": "string",
            "-p": "none",
            "-u": "none",
            "-s": "none",
            "-M": "none",
            "-C": "none",
            "-B": "none",
            "-D": "none",
            "-l": "none",
            # SECURITY: -S/-G/-O take REQUIRED string arguments.
            # documents the parser-differential attack that motivates this.
            "-S": "string",
            "-G": "string",
            "-O": "string",
            "-R": "none",
        },
    },
    "git log": {
        "safeFlags": {
            **GIT_LOG_DISPLAY_FLAGS,
            **GIT_REF_SELECTION_FLAGS,
            **GIT_DATE_FILTER_FLAGS,
            **GIT_COUNT_FLAGS,
            **GIT_STAT_FLAGS,
            **GIT_COLOR_FLAGS,
            **GIT_PATCH_FLAGS,
            **GIT_AUTHOR_FILTER_FLAGS,
            "--abbrev-commit": "none",
            "--full-history": "none",
            "--dense": "none",
            "--sparse": "none",
            "--simplify-merges": "none",
            "--ancestry-path": "none",
            "--source": "none",
            "--first-parent": "none",
            "--merges": "none",
            "--no-merges": "none",
            "--reverse": "none",
            "--walk-reflogs": "none",
            "--skip": "number",
            "--max-age": "number",
            "--min-age": "number",
            "--no-min-parents": "none",
            "--no-max-parents": "none",
            "--follow": "none",
            "--no-walk": "none",
            "--left-right": "none",
            "--cherry-mark": "none",
            "--cherry-pick": "none",
            "--boundary": "none",
            "--topo-order": "none",
            "--date-order": "none",
            "--author-date-order": "none",
            "--pretty": "string",
            "--format": "string",
            "--diff-filter": "string",
            "-S": "string",
            "-G": "string",
            "--pickaxe-regex": "none",
            "--pickaxe-all": "none",
        },
    },
    "git show": {
        "safeFlags": {
            **GIT_LOG_DISPLAY_FLAGS,
            **GIT_STAT_FLAGS,
            **GIT_COLOR_FLAGS,
            **GIT_PATCH_FLAGS,
            "--abbrev-commit": "none",
            "--word-diff": "none",
            "--word-diff-regex": "string",
            "--color-words": "none",
            "--pretty": "string",
            "--format": "string",
            "--first-parent": "none",
            "--raw": "none",
            "--diff-filter": "string",
            "-m": "none",
            "--quiet": "none",
        },
    },
    "git shortlog": {
        "safeFlags": {
            **GIT_REF_SELECTION_FLAGS,
            **GIT_DATE_FILTER_FLAGS,
            "-s": "none",
            "--summary": "none",
            "-n": "none",
            "--numbered": "none",
            "-e": "none",
            "--email": "none",
            "-c": "none",
            "--committer": "none",
            "--group": "string",
            "--format": "string",
            "--no-merges": "none",
            "--author": "string",
        },
    },
    "git reflog": {
        "safeFlags": {
            **GIT_LOG_DISPLAY_FLAGS,
            **GIT_REF_SELECTION_FLAGS,
            **GIT_DATE_FILTER_FLAGS,
            **GIT_COUNT_FLAGS,
            **GIT_AUTHOR_FILTER_FLAGS,
        },
        "additional_command_is_dangerous_callback": _git_reflog_callback,
    },
    "git stash list": {
        "safeFlags": {
            **GIT_LOG_DISPLAY_FLAGS,
            **GIT_REF_SELECTION_FLAGS,
            **GIT_COUNT_FLAGS,
        },
    },
    "git ls-remote": {
        "safeFlags": {
            "--branches": "none",
            "-b": "none",
            "--tags": "none",
            "-t": "none",
            "--heads": "none",
            "-h": "none",
            "--refs": "none",
            "--quiet": "none",
            "-q": "none",
            "--exit-code": "none",
            "--get-url": "none",
            "--symref": "none",
            "--sort": "string",
            # SECURITY: --server-option / -o intentionally excluded.
        },
    },
    "git status": {
        "safeFlags": {
            "--short": "none",
            "-s": "none",
            "--branch": "none",
            "-b": "none",
            "--porcelain": "none",
            "--long": "none",
            "--verbose": "none",
            "-v": "none",
            "--untracked-files": "string",
            "-u": "string",
            "--ignored": "none",
            "--ignore-submodules": "string",
            "--column": "none",
            "--no-column": "none",
            "--ahead-behind": "none",
            "--no-ahead-behind": "none",
            "--renames": "none",
            "--no-renames": "none",
            "--find-renames": "string",
            "-M": "string",
        },
    },
    "git blame": {
        "safeFlags": {
            **GIT_COLOR_FLAGS,
            "-L": "string",
            "--porcelain": "none",
            "-p": "none",
            "--line-porcelain": "none",
            "--incremental": "none",
            "--root": "none",
            "--show-stats": "none",
            "--show-name": "none",
            "--show-number": "none",
            "-n": "none",
            "--show-email": "none",
            "-e": "none",
            "-f": "none",
            "--date": "string",
            "-w": "none",
            "--ignore-rev": "string",
            "--ignore-revs-file": "string",
            "-M": "none",
            "-C": "none",
            "--score-debug": "none",
            "--abbrev": "number",
            "-s": "none",
            "-l": "none",
            "-t": "none",
        },
    },
    "git ls-files": {
        "safeFlags": {
            "--cached": "none",
            "-c": "none",
            "--deleted": "none",
            "-d": "none",
            "--modified": "none",
            "-m": "none",
            "--others": "none",
            "-o": "none",
            "--ignored": "none",
            "-i": "none",
            "--stage": "none",
            "-s": "none",
            "--killed": "none",
            "-k": "none",
            "--unmerged": "none",
            "-u": "none",
            "--directory": "none",
            "--no-empty-directory": "none",
            "--eol": "none",
            "--full-name": "none",
            "--abbrev": "number",
            "--debug": "none",
            "-z": "none",
            "-t": "none",
            "-v": "none",
            "-f": "none",
            "--exclude": "string",
            "-x": "string",
            "--exclude-from": "string",
            "-X": "string",
            "--exclude-per-directory": "string",
            "--exclude-standard": "none",
            "--error-unmatch": "none",
            "--recurse-submodules": "none",
        },
    },
    "git config --get": {
        "safeFlags": {
            "--local": "none",
            "--global": "none",
            "--system": "none",
            "--worktree": "none",
            "--default": "string",
            "--type": "string",
            "--bool": "none",
            "--int": "none",
            "--bool-or-int": "none",
            "--path": "none",
            "--expiry-date": "none",
            "-z": "none",
            "--null": "none",
            "--name-only": "none",
            "--show-origin": "none",
            "--show-scope": "none",
        },
    },
    # NOTE: 'git remote show' must come BEFORE 'git remote' so the longer
    # pattern is matched first.  The allowlist prefix matcher already sorts
    # multi-word patterns by length, so ordering in this dict doesn't matter
    # functionally, but we mirror OpenSpace for readability.
    "git remote show": {
        "safeFlags": {
            "-n": "none",
        },
        "additional_command_is_dangerous_callback": _git_remote_show_callback,
    },
    "git remote": {
        "safeFlags": {
            "-v": "none",
            "--verbose": "none",
        },
        "additional_command_is_dangerous_callback": _git_remote_callback,
    },
    "git merge-base": {
        "safeFlags": {
            "--is-ancestor": "none",
            "--fork-point": "none",
            "--octopus": "none",
            "--independent": "none",
            "--all": "none",
        },
    },
    "git rev-parse": {
        "safeFlags": {
            "--verify": "none",
            "--short": "string",
            "--abbrev-ref": "none",
            "--symbolic": "none",
            "--symbolic-full-name": "none",
            "--show-toplevel": "none",
            "--show-cdup": "none",
            "--show-prefix": "none",
            "--git-dir": "none",
            "--git-common-dir": "none",
            "--absolute-git-dir": "none",
            "--show-superproject-working-tree": "none",
            "--is-inside-work-tree": "none",
            "--is-inside-git-dir": "none",
            "--is-bare-repository": "none",
            "--is-shallow-repository": "none",
            "--is-shallow-update": "none",
            "--path-prefix": "none",
        },
    },
    "git rev-list": {
        "safeFlags": {
            **GIT_REF_SELECTION_FLAGS,
            **GIT_DATE_FILTER_FLAGS,
            **GIT_COUNT_FLAGS,
            **GIT_AUTHOR_FILTER_FLAGS,
            "--count": "none",
            "--reverse": "none",
            "--first-parent": "none",
            "--ancestry-path": "none",
            "--merges": "none",
            "--no-merges": "none",
            "--min-parents": "number",
            "--max-parents": "number",
            "--no-min-parents": "none",
            "--no-max-parents": "none",
            "--skip": "number",
            "--max-age": "number",
            "--min-age": "number",
            "--walk-reflogs": "none",
            "--oneline": "none",
            "--abbrev-commit": "none",
            "--pretty": "string",
            "--format": "string",
            "--abbrev": "number",
            "--full-history": "none",
            "--dense": "none",
            "--sparse": "none",
            "--source": "none",
            "--graph": "none",
        },
    },
    "git describe": {
        "safeFlags": {
            "--tags": "none",
            "--match": "string",
            "--exclude": "string",
            "--long": "none",
            "--abbrev": "number",
            "--always": "none",
            "--contains": "none",
            "--first-match": "none",
            "--exact-match": "none",
            "--candidates": "number",
            "--dirty": "none",
            "--broken": "none",
        },
    },
    "git cat-file": {
        # NOTE: --batch (without --check) is intentionally excluded — reads
        # arbitrary objects from stdin (L596-597).
        "safeFlags": {
            "-t": "none",
            "-s": "none",
            "-p": "none",
            "-e": "none",
            "--batch-check": "none",
            "--allow-undetermined-type": "none",
        },
    },
    "git for-each-ref": {
        "safeFlags": {
            "--format": "string",
            "--sort": "string",
            "--count": "number",
            "--contains": "string",
            "--no-contains": "string",
            "--merged": "string",
            "--no-merged": "string",
            "--points-at": "string",
        },
    },
    "git grep": {
        "safeFlags": {
            "-e": "string",
            "-E": "none",
            "--extended-regexp": "none",
            "-G": "none",
            "--basic-regexp": "none",
            "-F": "none",
            "--fixed-strings": "none",
            "-P": "none",
            "--perl-regexp": "none",
            "-i": "none",
            "--ignore-case": "none",
            "-v": "none",
            "--invert-match": "none",
            "-w": "none",
            "--word-regexp": "none",
            "-n": "none",
            "--line-number": "none",
            "-c": "none",
            "--count": "none",
            "-l": "none",
            "--files-with-matches": "none",
            "-L": "none",
            "--files-without-match": "none",
            "-h": "none",
            "-H": "none",
            "--heading": "none",
            "--break": "none",
            "--full-name": "none",
            "--color": "none",
            "--no-color": "none",
            "-o": "none",
            "--only-matching": "none",
            "-A": "number",
            "--after-context": "number",
            "-B": "number",
            "--before-context": "number",
            "-C": "number",
            "--context": "number",
            "--and": "none",
            "--or": "none",
            "--not": "none",
            "--max-depth": "number",
            "--untracked": "none",
            "--no-index": "none",
            "--recurse-submodules": "none",
            "--cached": "none",
            "--threads": "number",
            "-q": "none",
            "--quiet": "none",
        },
    },
    "git stash show": {
        "safeFlags": {
            **GIT_STAT_FLAGS,
            **GIT_COLOR_FLAGS,
            **GIT_PATCH_FLAGS,
            "--word-diff": "none",
            "--word-diff-regex": "string",
            "--diff-filter": "string",
            "--abbrev": "number",
        },
    },
    "git worktree list": {
        "safeFlags": {
            "--porcelain": "none",
            "-v": "none",
            "--verbose": "none",
            "--expire": "string",
        },
    },
    "git tag": {
        "safeFlags": {
            "-l": "none",
            "--list": "none",
            "-n": "number",
            "--contains": "string",
            "--no-contains": "string",
            "--merged": "string",
            "--no-merged": "string",
            "--sort": "string",
            "--format": "string",
            "--points-at": "string",
            "--column": "none",
            "--no-column": "none",
            "-i": "none",
            "--ignore-case": "none",
        },
        "additional_command_is_dangerous_callback": _git_tag_callback,
    },
    "git branch": {
        "safeFlags": {
            "-l": "none",
            "--list": "none",
            "-a": "none",
            "--all": "none",
            "-r": "none",
            "--remotes": "none",
            "-v": "none",
            "-vv": "none",
            "--verbose": "none",
            "--color": "none",
            "--no-color": "none",
            "--column": "none",
            "--no-column": "none",
            # See — --abbrev intentionally 'number' here.  The
            # callback below blocks the detached-arg form.
            "--abbrev": "number",
            "--no-abbrev": "none",
            "--contains": "string",
            "--no-contains": "string",
            "--merged": "none",
            "--no-merged": "none",
            "--points-at": "string",
            "--sort": "string",
            "--show-current": "none",
            "-i": "none",
            "--ignore-case": "none",
        },
        "additional_command_is_dangerous_callback": _git_branch_callback,
    },
}


# ─────────────────────────────────────────────────────────────────────
#  gh callback — shared across all gh subcommands
# ─────────────────────────────────────────────────────────────────────


def _gh_is_dangerous_callback(_raw: str, args: list[str]) -> bool:
    """OpenSpace ``ghIsDangerousCallback`` (L944-982).

    Rejects any token (or flag-value after ``=``) that contains ``://``,
    ``@``, or 2+ slashes (3-segment ``HOST/OWNER/REPO`` spec — normal gh
    usage is 2-segment ``OWNER/REPO``).
    """
    for token in args:
        if not token:
            continue
        value = token
        if token.startswith("-"):
            eq_idx = token.find("=")
            if eq_idx == -1:
                continue
            value = token[eq_idx + 1 :]
            if not value:
                continue
        if (
            "/" not in value
            and "://" not in value
            and "@" not in value
        ):
            continue
        if "://" in value:
            return True
        if "@" in value:
            return True
        slash_count = value.count("/")
        if slash_count >= 2:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────
#  GH_READ_ONLY_COMMANDS — 21 subcommands
# ─────────────────────────────────────────────────────────────────────

GH_READ_ONLY_COMMANDS: dict[str, CommandConfig] = {
    "gh pr view": {
        "safeFlags": {
            "--json": "string",
            "--comments": "none",
            "--repo": "string",
            "-R": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh pr list": {
        "safeFlags": {
            "--state": "string",
            "-s": "string",
            "--author": "string",
            "--assignee": "string",
            "--label": "string",
            "--limit": "number",
            "-L": "number",
            "--base": "string",
            "--head": "string",
            "--search": "string",
            "--json": "string",
            "--draft": "none",
            "--app": "string",
            "--repo": "string",
            "-R": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh pr diff": {
        "safeFlags": {
            "--color": "string",
            "--name-only": "none",
            "--patch": "none",
            "--repo": "string",
            "-R": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh pr checks": {
        "safeFlags": {
            "--watch": "none",
            "--required": "none",
            "--fail-fast": "none",
            "--json": "string",
            "--interval": "number",
            "--repo": "string",
            "-R": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh issue view": {
        "safeFlags": {
            "--json": "string",
            "--comments": "none",
            "--repo": "string",
            "-R": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh issue list": {
        "safeFlags": {
            "--state": "string",
            "-s": "string",
            "--assignee": "string",
            "--author": "string",
            "--label": "string",
            "--limit": "number",
            "-L": "number",
            "--milestone": "string",
            "--search": "string",
            "--json": "string",
            "--app": "string",
            "--repo": "string",
            "-R": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh repo view": {
        # gh repo view uses a positional argument, not --repo/-R
        "safeFlags": {
            "--json": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh run list": {
        "safeFlags": {
            "--branch": "string",
            "-b": "string",
            "--status": "string",
            "-s": "string",
            "--workflow": "string",
            "-w": "string",  # --workflow here, NOT --web
            "--limit": "number",
            "-L": "number",
            "--json": "string",
            "--repo": "string",
            "-R": "string",
            "--event": "string",
            "-e": "string",
            "--user": "string",
            "-u": "string",
            "--created": "string",
            "--commit": "string",
            "-c": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh run view": {
        "safeFlags": {
            "--log": "none",
            "--log-failed": "none",
            "--exit-status": "none",
            "--verbose": "none",
            "-v": "none",  # --verbose here, NOT --web
            "--json": "string",
            "--repo": "string",
            "-R": "string",
            "--job": "string",
            "-j": "string",
            "--attempt": "number",
            "-a": "number",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh auth status": {
        # --show-token/-t intentionally excluded
        "safeFlags": {
            "--active": "none",
            "-a": "none",
            "--hostname": "string",
            "-h": "string",
            "--json": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh pr status": {
        "safeFlags": {
            "--conflict-status": "none",
            "-c": "none",
            "--json": "string",
            "--repo": "string",
            "-R": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh issue status": {
        "safeFlags": {
            "--json": "string",
            "--repo": "string",
            "-R": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh release list": {
        "safeFlags": {
            "--exclude-drafts": "none",
            "--exclude-pre-releases": "none",
            "--json": "string",
            "--limit": "number",
            "-L": "number",
            "--order": "string",
            "-O": "string",
            "--repo": "string",
            "-R": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh release view": {
        "safeFlags": {
            "--json": "string",
            "--repo": "string",
            "-R": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh workflow list": {
        "safeFlags": {
            "--all": "none",
            "-a": "none",
            "--json": "string",
            "--limit": "number",
            "-L": "number",
            "--repo": "string",
            "-R": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh workflow view": {
        "safeFlags": {
            "--ref": "string",
            "-r": "string",
            "--yaml": "none",
            "-y": "none",
            "--repo": "string",
            "-R": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh label list": {
        "safeFlags": {
            "--json": "string",
            "--limit": "number",
            "-L": "number",
            "--order": "string",
            "--search": "string",
            "-S": "string",
            "--sort": "string",
            "--repo": "string",
            "-R": "string",
        },
        "additional_command_is_dangerous_callback": _gh_is_dangerous_callback,
    },
    "gh search repos": {
        "safeFlags": {
            "--archived": "none",
            "--created": "string",
            "--followers": "string",
            "--forks": "string",
            "--good-first-issues": "string",
            "--help-wanted-issues": "string",
            "--include-forks": "string",
            "--json": "string",
            "--language": "string",
            "--license": "string",
            "--limit": "number",
            "-L": "number",
            "--match": "string",
            "--number-topics": "string",
            "--order": "string",
            "--owner": "string",
            "--size": "string",
            "--sort": "string",
            "--stars": "string",
            "--topic": "string",
            "--updated": "string",
            "--visibility": "string",
        },
    },
    "gh search issues": {
        "safeFlags": {
            "--app": "string",
            "--assignee": "string",
            "--author": "string",
            "--closed": "string",
            "--commenter": "string",
            "--comments": "string",
            "--created": "string",
            "--include-prs": "none",
            "--interactions": "string",
            "--involves": "string",
            "--json": "string",
            "--label": "string",
            "--language": "string",
            "--limit": "number",
            "-L": "number",
            "--locked": "none",
            "--match": "string",
            "--mentions": "string",
            "--milestone": "string",
            "--no-assignee": "none",
            "--no-label": "none",
            "--no-milestone": "none",
            "--no-project": "none",
            "--order": "string",
            "--owner": "string",
            "--project": "string",
            "--reactions": "string",
            "--repo": "string",
            "-R": "string",
            "--sort": "string",
            "--state": "string",
            "--team-mentions": "string",
            "--updated": "string",
            "--visibility": "string",
        },
    },
    "gh search prs": {
        "safeFlags": {
            "--app": "string",
            "--assignee": "string",
            "--author": "string",
            "--base": "string",
            "-B": "string",
            "--checks": "string",
            "--closed": "string",
            "--commenter": "string",
            "--comments": "string",
            "--created": "string",
            "--draft": "none",
            "--head": "string",
            "-H": "string",
            "--interactions": "string",
            "--involves": "string",
            "--json": "string",
            "--label": "string",
            "--language": "string",
            "--limit": "number",
            "-L": "number",
            "--locked": "none",
            "--match": "string",
            "--mentions": "string",
            "--merged": "none",
            "--merged-at": "string",
            "--milestone": "string",
            "--no-assignee": "none",
            "--no-label": "none",
            "--no-milestone": "none",
            "--no-project": "none",
            "--order": "string",
            "--owner": "string",
            "--project": "string",
            "--reactions": "string",
            "--repo": "string",
            "-R": "string",
            "--review": "string",
            "--review-requested": "string",
            "--reviewed-by": "string",
            "--sort": "string",
            "--state": "string",
            "--team-mentions": "string",
            "--updated": "string",
            "--visibility": "string",
        },
    },
    "gh search commits": {
        "safeFlags": {
            "--author": "string",
            "--author-date": "string",
            "--author-email": "string",
            "--author-name": "string",
            "--committer": "string",
            "--committer-date": "string",
            "--committer-email": "string",
            "--committer-name": "string",
            "--hash": "string",
            "--json": "string",
            "--limit": "number",
            "-L": "number",
            "--merge": "none",
            "--order": "string",
            "--owner": "string",
            "--parent": "string",
            "--repo": "string",
            "-R": "string",
            "--sort": "string",
            "--tree": "string",
            "--visibility": "string",
        },
    },
    "gh search code": {
        "safeFlags": {
            "--extension": "string",
            "--filename": "string",
            "--json": "string",
            "--language": "string",
            "--limit": "number",
            "-L": "number",
            "--match": "string",
            "--owner": "string",
            "--repo": "string",
            "-R": "string",
            "--size": "string",
        },
    },
}


# ─────────────────────────────────────────────────────────────────────
#  DOCKER_READ_ONLY_COMMANDS
# ─────────────────────────────────────────────────────────────────────

DOCKER_READ_ONLY_COMMANDS: dict[str, CommandConfig] = {
    "docker logs": {
        "safeFlags": {
            "--follow": "none",
            "-f": "none",
            "--tail": "string",
            "-n": "string",
            "--timestamps": "none",
            "-t": "none",
            "--since": "string",
            "--until": "string",
            "--details": "none",
        },
    },
    "docker inspect": {
        "safeFlags": {
            "--format": "string",
            "-f": "string",
            "--type": "string",
            "--size": "none",
            "-s": "none",
        },
    },
}


# ─────────────────────────────────────────────────────────────────────
#  RIPGREP_READ_ONLY_COMMANDS
# ─────────────────────────────────────────────────────────────────────

RIPGREP_READ_ONLY_COMMANDS: dict[str, CommandConfig] = {
    "rg": {
        "safeFlags": {
            # Pattern flags
            "-e": "string",
            "--regexp": "string",
            "-f": "string",
            # Common search options
            "-i": "none",
            "--ignore-case": "none",
            "-S": "none",
            "--smart-case": "none",
            "-F": "none",
            "--fixed-strings": "none",
            "-w": "none",
            "--word-regexp": "none",
            "-v": "none",
            "--invert-match": "none",
            # Output
            "-c": "none",
            "--count": "none",
            "-l": "none",
            "--files-with-matches": "none",
            "--files-without-match": "none",
            "-n": "none",
            "--line-number": "none",
            "-o": "none",
            "--only-matching": "none",
            "-A": "number",
            "--after-context": "number",
            "-B": "number",
            "--before-context": "number",
            "-C": "number",
            "--context": "number",
            "-H": "none",
            "-h": "none",
            "--heading": "none",
            "--no-heading": "none",
            "-q": "none",
            "--quiet": "none",
            "--column": "none",
            # File filtering
            "-g": "string",
            "--glob": "string",
            "-t": "string",
            "--type": "string",
            "-T": "string",
            "--type-not": "string",
            "--type-list": "none",
            "--hidden": "none",
            "--no-ignore": "none",
            "-u": "none",
            # Common options
            "-m": "number",
            "--max-count": "number",
            "-d": "number",
            "--max-depth": "number",
            "-a": "none",
            "--text": "none",
            "-z": "none",
            "-L": "none",
            "--follow": "none",
            # Display
            "--color": "string",
            "--json": "none",
            "--stats": "none",
            # Help and version
            "--help": "none",
            "--version": "none",
            "--debug": "none",
            # Special
            "--": "none",
        },
    },
}


# ─────────────────────────────────────────────────────────────────────
#  PYRIGHT_READ_ONLY_COMMANDS
# ─────────────────────────────────────────────────────────────────────


def _pyright_is_dangerous_callback(_raw: str, args: list[str]) -> bool:
    """OpenSpace pyright callback (L1523-1529) — reject ``--watch`` / ``-w``."""
    return any(t == "--watch" or t == "-w" for t in args)


PYRIGHT_READ_ONLY_COMMANDS: dict[str, CommandConfig] = {
    "pyright": {
        # pyright treats `--` as a file path, NOT end-of-options.
        "respects_double_dash": False,
        "safeFlags": {
            "--outputjson": "none",
            "--project": "string",
            "-p": "string",
            "--pythonversion": "string",
            "--pythonplatform": "string",
            "--typeshedpath": "string",
            "--venvpath": "string",
            "--level": "string",
            "--stats": "none",
            "--verbose": "none",
            "--version": "none",
            "--dependencies": "none",
            "--warnings": "none",
        },
        "additional_command_is_dangerous_callback": _pyright_is_dangerous_callback,
    },
}


# ─────────────────────────────────────────────────────────────────────
#  EXTERNAL_READONLY_COMMANDS
# ─────────────────────────────────────────────────────────────────────

# Cross-shell commands that work identically in bash and PowerShell.
# Purely informational — both entries are already present in
# ``readonly_commands.READONLY_COMMANDS`` (the READONLY_COMMANDS regex
# fallback already covers them).
EXTERNAL_READONLY_COMMANDS: tuple[str, ...] = (
    "docker ps",
    "docker images",
)


# ─────────────────────────────────────────────────────────────────────
#  Aggregation helper
# ─────────────────────────────────────────────────────────────────────


def build_shared_allowlist() -> dict[str, CommandConfig]:
    """Merge the five shared tables into a single dict.

    Called from :func:`bash_classifier.register_shared_commands` at module
    init time.  Returns a fresh dict so mutations do not leak back to
    module-level constants.
    """
    merged: dict[str, CommandConfig] = {}
    merged.update(GIT_READ_ONLY_COMMANDS)
    merged.update(GH_READ_ONLY_COMMANDS)
    merged.update(DOCKER_READ_ONLY_COMMANDS)
    merged.update(RIPGREP_READ_ONLY_COMMANDS)
    merged.update(PYRIGHT_READ_ONLY_COMMANDS)
    return merged
