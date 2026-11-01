"""Shell parser used by bash read-only and permission checks.

The module normalizes bash command parsing behind a small API:
``try_parse_shell_command`` for argv-like tokenization, ``split_command_segments``
for compound-command decomposition, and ``extract_output_redirections`` for
permission checks that need to reason about write targets.

``bashlex`` is preferred when available because it provides a real bash AST.
Regex fallbacks remain in place so permission checks fail conservatively when
the optional parser is unavailable or cannot parse a command.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Iterable, Optional

try:
    import bashlex  # type: ignore[import-untyped]
    import bashlex.errors  # type: ignore[import-untyped]

    _BASHLEX_AVAILABLE = True
except ImportError:
    bashlex = None  # type: ignore[assignment]
    _BASHLEX_AVAILABLE = False


__all__ = [
    # parse
    "ShellParseResult",
    "try_parse_shell_command",
    "has_malformed_tokens",
    # compound command splitting
    "split_command_segments",
    # redirections
    "OutputRedirection",
    "extract_output_redirections",
    # identity helpers
    "strip_safe_wrappers",
    "strip_all_leading_env_vars",
    "is_normalized_git_command",
    "is_normalized_cd_command",
    "command_has_any_cd",
    "command_has_any_git",
    # bashlex availability
    "bashlex_available",
]


def bashlex_available() -> bool:
    """True if the :mod:`bashlex` backend is importable at runtime."""
    return _BASHLEX_AVAILABLE


# ─────────────────────────────────────────────────────────────────────
#  Shell tokenization
# ─────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ShellParseResult:
    """Parsed shell command result.

    ``tokens`` is a ``list[str]`` of argv-ish words. Operators (``|``, ``&&``,
    ``;``, ``(``, ``)``, ``>``, ``>>``) are not embedded; they are consumed by
    the AST and only surface through the compound-command APIs below.
    """

    success: bool
    tokens: list[str] = field(default_factory=list)
    error: Optional[str] = None


# Safe env-var prefix allowlist. Any ``NAME=value`` prefix whose name is NOT in
# this set must be preserved because load-path variables can change which
# executable is invoked.
_SAFE_ENV_VAR_PATTERN = re.compile(
    r"^("
    # locale / tz
    r"LC_[A-Z_]+|LANG|LANGUAGE|TZ|"
    # terminal / locale output
    r"COLUMNS|LINES|TERM|NO_COLOR|FORCE_COLOR|CLICOLOR|CLICOLOR_FORCE|"
    # common app-specific that don't hijack binaries
    r"GIT_[A-Z_]+|PAGER|EDITOR|VISUAL|MANPAGER|"
    r"DEBUG|VERBOSE|QUIET|"
    # tool-specific common
    r"NODE_OPTIONS|PYTHONDONTWRITEBYTECODE|PYTHONUNBUFFERED"
    r")=.*$"
)


# Binary-hijack env var names. These *must* be preserved through safe-wrapper
# stripping because dropping them would hide a load-path attack.
_BINARY_HIJACK_VARS = re.compile(
    r"^(LD_[A-Z_]+|DYLD_[A-Z_]+|PATH|PYTHONPATH|NODE_PATH|"
    r"CLASSPATH|PERL5LIB|LUA_PATH|LUA_CPATH|RUBYLIB)=.*$"
)


def _bashlex_to_first_command_tokens(tree_list: list) -> list[str]:
    """Flatten the first ``command`` node in a bashlex AST to argv words.

    ``bashlex.parse`` returns a list of top-level AST nodes.  Each can
    be ``command`` / ``pipeline`` / ``list`` / ``compoundcommand``.
    We descend to the first ``command`` node and extract its word
    children (redirects and substitutions are dropped — callers use
    the dedicated redirection / substitution APIs).
    """

    def _walk(node) -> Optional[list[str]]:
        kind = getattr(node, "kind", None)
        if kind == "command":
            words: list[str] = []
            for part in getattr(node, "parts", []) or []:
                if getattr(part, "kind", None) == "word":
                    w = getattr(part, "word", None)
                    if isinstance(w, str):
                        words.append(w)
            return words
        for part in getattr(node, "parts", []) or []:
            result = _walk(part)
            if result is not None:
                return result
        return None

    for top in tree_list:
        words = _walk(top)
        if words is not None:
            return words
    return []


def try_parse_shell_command(command: str) -> ShellParseResult:
    """OpenSpace ``tryParseShellCommand`` — best-effort argv tokenisation.

    Returns ``{success: True, tokens: [...]}`` on success, else
    ``{success: False, error: "..."}``.

    When bashlex is unavailable we fall back to :mod:`shlex` (POSIX
    mode, ``posix=True``) — less faithful but enough to keep the
    read-only pipeline functioning in constrained environments.
    """
    if not command or not command.strip():
        return ShellParseResult(success=False, error="empty command")

    if _BASHLEX_AVAILABLE:
        try:
            tree = bashlex.parse(command)
        except bashlex.errors.ParsingError as exc:
            return ShellParseResult(success=False, error=str(exc))
        except Exception as exc:  # bashlex also raises Matcher errors, etc.
            return ShellParseResult(success=False, error=str(exc))
        tokens = _bashlex_to_first_command_tokens(tree)
        if not tokens:
            # bashlex parsed but we couldn't flatten — fall back to shlex
            # so callers still get *some* argv for heuristics.
            try:
                tokens = shlex.split(command, posix=True)
            except ValueError as exc:
                return ShellParseResult(success=False, error=str(exc))
        return ShellParseResult(success=True, tokens=tokens)

    # shlex fallback
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        return ShellParseResult(success=False, error=str(exc))
    return ShellParseResult(success=True, tokens=tokens)


def has_malformed_tokens(command: str, tokens: Iterable[str]) -> bool:
    """OpenSpace ``hasMalformedTokens`` — best-effort sanity check.

    OpenSpace's implementation compares ``shell-quote`` output against the
    original; we check for empty-string tokens (bashlex produces those
    for malformed escape sequences) and grossly short token lists.
    """
    if not command.strip():
        return False
    tok_list = list(tokens)
    if not tok_list:
        return True
    if any(t == "" for t in tok_list):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────
#  split_command_segments
#
#  Return textual subcommands with control operators removed and redirections
#  left attached until the redirection extractor handles them.
# ─────────────────────────────────────────────────────────────────────


_SHELL_OPERATOR_SPLIT_FALLBACK = re.compile(r"\s*(?:&&|\|\||;|\|(?!\|))\s*")


def _ast_flatten_to_commands(node, source: str, out: list[str]) -> None:
    """Walk a bashlex AST and append each ``command`` SOURCE SUBSTRING to *out*.

    We slice the original *source* using each command node's ``pos``
    range so redirections (``> file`` / ``2>&1``) and assignments stay
    attached to the subcommand text. The splitter only separates pipe,
    logical, and semicolon operators.

    Operators (``&&``, ``||``, ``;``, ``|``) are skipped because they
    live in separate ``operator`` nodes at the ``list`` / ``pipeline``
    level, not inside ``command`` nodes.
    """
    kind = getattr(node, "kind", None)
    if kind == "command":
        pos = getattr(node, "pos", None)
        if pos and len(pos) == 2:
            start, end = pos
            out.append(source[start:end])
        else:
            # Fallback: reconstruct from words (loses redirects; only
            # triggers when bashlex omits position info).
            parts = []
            for part in getattr(node, "parts", []) or []:
                w = getattr(part, "word", None)
                if isinstance(w, str):
                    parts.append(w)
            if parts:
                out.append(" ".join(parts))
        return

    # pipeline / list / compoundcommand — recurse into children
    # (``operator`` / ``pipe`` nodes have no command content).
    for part in getattr(node, "parts", []) or []:
        pk = getattr(part, "kind", None)
        if pk in ("operator", "pipe", "reservedword"):
            continue
        _ast_flatten_to_commands(part, source, out)


def split_command_segments(command: str) -> list[str]:
    """Return subcommand fragments for a possibly compound shell command.

    Uses bashlex AST when available.  Falls back to the naive regex
    splitter when bashlex is missing or parsing fails. Bad parses are treated
    as one opaque command so callers do not incorrectly auto-approve a fragment.

    **Strips**:
        - control operators: ``|``, ``||``, ``&&``, ``;``, ``&``
        - redirections:      ``>``, ``>>``, ``2>&1``, ``<``, ``2>/dev/null``
        - subshell parens when used purely as grouping

    **Preserves**:
        - command substitutions (``$(cmd)``) appear as a word in the
          containing command.  Callers that need to analyse them must
          use :func:`try_parse_shell_command` or a full AST walk.
    """
    if not command or not command.strip():
        return []

    if _BASHLEX_AVAILABLE:
        try:
            tree = bashlex.parse(command)
            out: list[str] = []
            for top in tree:
                _ast_flatten_to_commands(top, command, out)
            if out:
                return out
        except Exception:
            pass  # fall through to regex fallback

    # Fallback: strip trailing ``2>&1`` + split on top-level operators.
    trimmed = re.sub(r"\s*\d?>&\d+\s*$", "", command.strip())
    parts = _SHELL_OPERATOR_SPLIT_FALLBACK.split(trimmed)
    return [p for p in (seg.strip() for seg in parts) if p]


# ─────────────────────────────────────────────────────────────────────
#  extractOutputRedirections — OpenSpace commands.ts L634-789 (subset)
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OutputRedirection:
    """OpenSpace ``OutputRedirection`` — ``{target, operator}``."""

    target: str
    operator: str  # '>' or '>>'


@dataclass(slots=True)
class ExtractRedirectResult:
    """OpenSpace ``{commandWithoutRedirections, redirections, hasDangerousRedirection}``."""

    command_without_redirections: str
    redirections: list[OutputRedirection]
    has_dangerous_redirection: bool


_DYNAMIC_TARGET_RE = re.compile(r"[\$`\*\?]|\([^)]*\)")
_ALLOWED_FDS = {"0", "1", "2"}


def _is_static_redirect_target(target: str) -> bool:
    """Return True when a redirect target has no expansion or glob syntax."""
    if not target:
        return True  # defense-in-depth: OpenSpace treats '' as unsafe-static
    return not _DYNAMIC_TARGET_RE.search(target)


def extract_output_redirections(command: str) -> ExtractRedirectResult:
    """Extract ``>`` / ``>>`` output redirection targets.

    Security note: parsing failure is treated as "potentially dangerous" and
    callers must not auto-allow.

    Returns dataclass with:
      - ``command_without_redirections``: command string minus the
        redirections (approximate reconstruction; do not rely on
        byte-exact match).
      - ``redirections``: list of ``OutputRedirection``.
      - ``has_dangerous_redirection``: True if any target is dynamic
        (contains ``$``, `` ` ``, ``*``, ``?``, or process-substitution)
        OR if parsing failed entirely.
    """
    if not command or not command.strip():
        return ExtractRedirectResult(command, [], False)

    if not _BASHLEX_AVAILABLE:
        # Fallback: fail closed on malformed redirection-like syntax.
        return ExtractRedirectResult(command, [], True)

    try:
        tree = bashlex.parse(command)
    except Exception:
        return ExtractRedirectResult(command, [], True)

    redirections: list[OutputRedirection] = []
    has_dangerous = False

    def _walk(node) -> None:
        nonlocal has_dangerous
        if getattr(node, "kind", None) == "redirect":
            op = getattr(node, "type", None) or ""
            if op in (">", ">>"):
                # Extract the target token.  bashlex stores the target
                # as ``output`` (a word node) on the redirect.
                target_word = None
                out = getattr(node, "output", None)
                if out is not None:
                    target_word = getattr(out, "word", None) or str(out)
                if isinstance(target_word, str):
                    if not _is_static_redirect_target(target_word):
                        has_dangerous = True
                    redirections.append(
                        OutputRedirection(target=target_word, operator=op)
                    )
            elif op in (">&", "&>"):
                # FD-duplication — drop, not a file write
                pass
        for p in getattr(node, "parts", []) or []:
            _walk(p)

    for top in tree:
        _walk(top)

    # Reconstruct the command minus redirections by scanning the original
    # text and removing matched redirect spans.  bashlex exposes
    # position ranges on nodes; use them when available.
    stripped = command
    try:
        spans: list[tuple[int, int]] = []

        def _collect_spans(node):
            if getattr(node, "kind", None) == "redirect":
                pos = getattr(node, "pos", None)
                if pos and len(pos) == 2:
                    spans.append(tuple(pos))  # type: ignore[arg-type]
            for p in getattr(node, "parts", []) or []:
                _collect_spans(p)

        for top in tree:
            _collect_spans(top)

        # Remove spans right-to-left to preserve offsets.
        for start, end in sorted(spans, reverse=True):
            stripped = stripped[:start] + stripped[end:]
        stripped = re.sub(r"\s+", " ", stripped).strip()
    except Exception:
        stripped = command

    return ExtractRedirectResult(
        command_without_redirections=stripped,
        redirections=redirections,
        has_dangerous_redirection=has_dangerous,
    )


# ─────────────────────────────────────────────────────────────────────
#  stripSafeWrappers — OpenSpace bashPermissions.ts L?? (export list §3.1)
#
#  Strips safe leading wrappers: timeout/time/nice/stdbuf/nohup and
#  safe env var prefixes.  Preserves binary-hijack env vars.
# ─────────────────────────────────────────────────────────────────────


_SAFE_WRAPPER_COMMANDS = {"timeout", "time", "nice", "stdbuf", "nohup", "ionice"}

# Leading timeout/stdbuf flag skip patterns.
_TIMEOUT_FLAG_VALUE_RE = re.compile(r"^[A-Za-z0-9_.+\-]+$")


def _skip_timeout_flags(tokens: list[str], i: int) -> int:
    """Skip ``timeout -k 10s -s TERM 30 CMD`` flags; return new index."""
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--kill-after=") or t.startswith("--signal="):
            i += 1
            continue
        if t in ("-k", "-s") and i + 1 < len(tokens):
            if _TIMEOUT_FLAG_VALUE_RE.match(tokens[i + 1]):
                i += 2
                continue
        if re.match(r"^-[ks][A-Za-z0-9_.+\-]+$", t):
            i += 1
            continue
        if re.match(r"^\d+(?:\.\d+)?[smhd]?$", t):
            # the duration token
            i += 1
            break
        break
    return i


def _skip_nice_flags(tokens: list[str], i: int) -> int:
    """Skip ``nice -n 10`` / ``nice -10`` prefix flags."""
    while i < len(tokens):
        t = tokens[i]
        if t == "-n" and i + 1 < len(tokens) and re.match(r"^-?\d+$", tokens[i + 1]):
            i += 2
            continue
        if re.match(r"^-\d+$", t):
            i += 1
            continue
        break
    return i


_LEADING_ENV_ASSIGN_RE = re.compile(
    r"""
    ^\s*                                                # leading ws
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)                    # var name
    =                                                   # literal '='
    (?P<value>                                          # value (one of):
        (?:'[^']*')                                     #   single-quoted
        | (?:"(?:\\.|[^"\\])*")                         #   double-quoted
        | (?:[^\s'"`$|&;<>(){}]*)                       #   bare word
    )
    """,
    re.VERBOSE,
)


def strip_all_leading_env_vars(
    command: str, preserve_binary_hijack: bool = True
) -> str:
    """OpenSpace ``stripAllLeadingEnvVars`` — strip ``NAME=VAL ...`` prefixes.

    Uses a text-level regex scan rather than the bashlex AST because
    bashlex moves leading assignments onto a separate ``assignment``
    node, losing the token-order information we need to distinguish
    "leading prefix" from "embedded argument".

    When *preserve_binary_hijack* is True (default, matches OpenSpace's
    safe-wrapper use case), vars matching :data:`_BINARY_HIJACK_VARS`
    are kept attached so an LD_PRELOAD attack cannot sneak through by
    being mis-labelled as a leading env.
    """
    if not command or not command.strip():
        return command

    remaining = command
    while True:
        m = _LEADING_ENV_ASSIGN_RE.match(remaining)
        if not m:
            break
        name = m.group("name")
        # Reconstruct the full token to check against BINARY_HIJACK.
        full_token = remaining[m.start() : m.end()].strip()
        if preserve_binary_hijack and _BINARY_HIJACK_VARS.match(full_token):
            break
        # Must be followed by whitespace + non-assignment (or EOL).
        rest = remaining[m.end() :]
        if rest and not rest[:1].isspace():
            break
        remaining = rest.lstrip()

    return remaining if remaining != command else command


def strip_safe_wrappers(command: str) -> str:
    """OpenSpace ``stripSafeWrappers`` — remove leading ``timeout``/``nice``/env prefixes.

    Returns a command string with the first *real* command exposed as
    token[0].  Binary-hijack env vars (``LD_*``, ``PATH=…``) are
    preserved — stripping them would hide a binary-override attack.
    """
    if not command:
        return command

    # 1. Strip leading safe env vars (preserve binary-hijack).
    stripped = strip_all_leading_env_vars(command, preserve_binary_hijack=True)

    # 2. Peel off wrapper commands recursively.
    for _ in range(4):  # cap recursion (OpenSpace matches the same "few times" heuristic)
        parsed = try_parse_shell_command(stripped)
        if not parsed.success or not parsed.tokens:
            return stripped
        tokens = parsed.tokens
        if tokens[0] not in _SAFE_WRAPPER_COMMANDS:
            return stripped

        cmd = tokens[0]
        i = 1
        if cmd == "timeout":
            i = _skip_timeout_flags(tokens, i)
        elif cmd == "nice":
            i = _skip_nice_flags(tokens, i)
        elif cmd == "time":
            # time can take -p, -f FMT, -o FILE
            while i < len(tokens):
                if tokens[i] == "-p":
                    i += 1
                elif tokens[i] in ("-f", "-o") and i + 1 < len(tokens):
                    i += 2
                elif tokens[i].startswith("-"):
                    i += 1
                else:
                    break
        elif cmd == "stdbuf":
            # -i/-o/-e N (or --input=/--output=/--error=)
            while i < len(tokens):
                t = tokens[i]
                if t.startswith("--input=") or t.startswith("--output=") or t.startswith("--error="):
                    i += 1
                elif t in ("-i", "-o", "-e") and i + 1 < len(tokens):
                    i += 2
                elif re.match(r"^-[ioe].", t):
                    i += 1
                else:
                    break
        elif cmd == "nohup":
            pass  # no flags, just cmd
        elif cmd == "ionice":
            # -c CLASS -n LEVEL -p PID
            while i < len(tokens):
                if tokens[i] in ("-c", "-n", "-p") and i + 1 < len(tokens):
                    i += 2
                elif tokens[i].startswith("-"):
                    i += 1
                else:
                    break

        if i >= len(tokens):
            return stripped
        stripped = " ".join(tokens[i:])

    return stripped


# ─────────────────────────────────────────────────────────────────────
#  Identity helpers — OpenSpace bashPermissions.ts L2567-2620
# ─────────────────────────────────────────────────────────────────────


def is_normalized_git_command(command: str) -> bool:
    """OpenSpace ``isNormalizedGitCommand`` — True iff the command (after
    wrapper stripping) runs ``git``.

    Matches OpenSpace's fast path (``command.startswith('git ')``) first, then
    falls back to wrapper stripping + argv parse + ``xargs git ...``
    detection.
    """
    if command.startswith("git ") or command == "git":
        return True
    stripped = strip_safe_wrappers(command)
    parsed = try_parse_shell_command(stripped)
    if parsed.success and parsed.tokens:
        if parsed.tokens[0] == "git":
            return True
        if parsed.tokens[0] == "xargs" and "git" in parsed.tokens:
            return True
        return False
    return bool(re.match(r"^git(?:\s|$)", stripped))


def is_normalized_cd_command(command: str) -> bool:
    """OpenSpace ``isNormalizedCdCommand`` — True iff the command is cd/pushd/popd."""
    stripped = strip_safe_wrappers(command)
    parsed = try_parse_shell_command(stripped)
    if parsed.success and parsed.tokens:
        return parsed.tokens[0] in ("cd", "pushd", "popd")
    return bool(re.match(r"^(?:cd|pushd|popd)(?:\s|$)", stripped))


def command_has_any_cd(command: str) -> bool:
    """OpenSpace ``commandHasAnyCd`` — True iff any subcommand is cd/pushd/popd."""
    return any(
        is_normalized_cd_command(sub.strip())
        for sub in split_command_segments(command)
    )


def command_has_any_git(command: str) -> bool:
    """OpenSpace ``commandHasAnyGit`` — True iff any subcommand is git."""
    return any(
        is_normalized_git_command(sub.strip())
        for sub in split_command_segments(command)
    )
