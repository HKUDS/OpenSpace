"""BashTool permission engine.

Main entry: :func:`bash_tool_has_permission` orchestrates rule matching,
sandbox auto-allow checks, compound-command handling, and path safety guards.

Runtime boundaries:

- LLM-based classifiers are not part of the local permission engine:
  ``classifyBashCommand``,
  ``getBashPromptAllowDescriptions`` / ``…AskDescriptions`` / ``…DenyDescriptions``,
  ``isClassifierPermissionsEnabled``, ``buildPendingClassifierCheck``,
  ``startSpeculativeClassifierCheck``, ``executeAsyncClassifierCheck``.
  Branches that would require those classifiers fall through to the local
  no-match result, typically ``ask``.
- ``auto`` / ``bubble`` mode auto-approval: we never enter those modes.
- ``yoloClassifier``.
- Tree-sitter AST paths are not required; compound commands use
  :func:`shell_parser.split_command_segments`.

Preserved:

- All exact + prefix + wildcard rule matching.
- All hard-deny SAFETY paths: dangerous ``rm``, block-device writes,
  sensitive-path traversal (delegated to :mod:`bash_path_validation`).
- Compound-command merging with deny > ask > allow > passthrough priority.
- ``cd`` + ``git`` bare-repo guard, multiple-``cd`` guard.
- Path constraint checks for the supported shell file-operation commands.
- ``is_bash_security_check_for_misparsing`` field on ``ask`` results for
  conservative parser-misread prompts.
"""
from __future__ import annotations

import re
from typing import (
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    Optional,
    Tuple,
)

from ..security.bash_injection import bash_command_passes_injection_gate
from ..security.shell_parser import (
    command_has_any_cd as _security_command_has_any_cd,
    command_has_any_git as _security_command_has_any_git,
    extract_output_redirections,
    is_normalized_cd_command as _security_is_normalized_cd_command,
    is_normalized_git_command as _security_is_normalized_git_command,
    split_command_segments,
)
from .bash_helpers import (
    CommandIdentityCheckers,
    check_command_operator_permissions,
)
from .bash_path_validation import check_path_constraints
from .types import (
    AddRulesUpdate,
    DecisionReasonMode,
    DecisionReasonOther,
    DecisionReasonRule,
    DecisionReasonSandboxOverride,
    DecisionReasonSubcommandResults,
    PermissionAllow,
    PermissionAsk,
    PermissionBehavior,
    PermissionDeny,
    PermissionMode,
    PermissionPassthrough,
    PermissionResult,
    PermissionRule,
    PermissionRuleValue,
    PermissionUpdate,
    ToolPermissionContext,
    format_rule_value,
    normalize_tool_name_for_rule,
    parse_rule_value,
)


__all__ = [
    # constants
    "MAX_SUBCOMMANDS_FOR_SECURITY_CHECK",
    "MAX_SUGGESTED_RULES_FOR_COMPOUND",
    "SAFE_ENV_VARS",
    "BINARY_HIJACK_VARS",
    "BARE_SHELL_PREFIXES",
    # rule parsing + matching
    "ShellPermissionRule",
    "ExactRule",
    "PrefixRule",
    "WildcardRule",
    "parse_permission_rule",
    "match_wildcard_pattern",
    "permission_rule_extract_prefix",
    # wrappers
    "strip_safe_wrappers",
    "strip_all_leading_env_vars",
    "strip_wrappers_from_argv",
    # suggestions
    "get_simple_command_prefix",
    "get_first_word_prefix",
    "suggestion_for_exact_command",
    "suggestion_for_prefix",
    # rule matchers
    "filter_rules_by_contents_matching_input",
    "matching_rules_for_input",
    # layered checks
    "bash_tool_check_exact_match_permission",
    "bash_tool_check_permission",
    "check_rule_based_permissions_for_bash",
    "check_sandbox_auto_allow",
    "check_compound_command_permissions",
    "check_legacy_misparsing",
    # main entry
    "bash_tool_has_permission",
    # command identity
    "is_normalized_git_command",
    "is_normalized_cd_command",
    "command_has_any_cd",
    "command_has_any_git",
]


# ════════════════════════════════════════════════════════════════════════
# §1  Constants
# ════════════════════════════════════════════════════════════════════════


# ``MAX_SUBCOMMANDS_FOR_SECURITY_CHECK`` — compound fan-out cap.
MAX_SUBCOMMANDS_FOR_SECURITY_CHECK = 50

# ``MAX_SUGGESTED_RULES_FOR_COMPOUND`` — cap on rule suggestions.
MAX_SUGGESTED_RULES_FOR_COMPOUND = 5

# ``BARE_SHELL_PREFIXES``. These must never be suggested as
# prefix rules because they'd allow arbitrary code via ``-c``.
BARE_SHELL_PREFIXES: frozenset[str] = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "fish",
        "csh",
        "tcsh",
        "ksh",
        "dash",
        "cmd",
        "powershell",
        "pwsh",
        # wrappers that exec their args as a command
        "env",
        "xargs",
        "nice",
        "stdbuf",
        "nohup",
        "timeout",
        "time",
        # privilege escalation
        "sudo",
        "doas",
        "pkexec",
    }
)


# ``SAFE_ENV_VARS`` — names safe to strip before rule matching.
SAFE_ENV_VARS: frozenset[str] = frozenset(
    {
        # Go
        "GOEXPERIMENT",
        "GOOS",
        "GOARCH",
        "CGO_ENABLED",
        "GO111MODULE",
        # Rust
        "RUST_BACKTRACE",
        "RUST_LOG",
        # Node
        "NODE_ENV",
        # Python
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        # Pytest
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "PYTEST_DEBUG",
        # API keys
        "ANTHROPIC_API_KEY",
        # Locale
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_TIME",
        "CHARSET",
        # Terminal / display
        "TERM",
        "COLORTERM",
        "NO_COLOR",
        "FORCE_COLOR",
        "TZ",
        # Colour configs
        "LS_COLORS",
        "LSCOLORS",
        "GREP_COLOR",
        "GREP_COLORS",
        "GCC_COLORS",
        # Formatting
        "TIME_STYLE",
        "BLOCK_SIZE",
        "BLOCKSIZE",
    }
)


# ``BINARY_HIJACK_VARS``. Stripping these would hide a load-path
# attack, so they must stay attached even in ``stripAllLeadingEnvVars``.
BINARY_HIJACK_VARS = re.compile(r"^(LD_|DYLD_|PATH$)")


# ``ENV_VAR_ASSIGN_RE``.
_ENV_VAR_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")

# — the "looks like a subcommand" regex: lowercase alphanum token.
_SUBCOMMAND_SHAPE_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


# ════════════════════════════════════════════════════════════════════════
# §2  ShellPermissionRule — parse / match (OpenSpace shellRuleMatching.ts)
# ════════════════════════════════════════════════════════════════════════


class ShellPermissionRule:
    """OpenSpace ``ShellPermissionRule`` (shellRuleMatching.ts L25-37) — tagged union."""

    __slots__ = ("type",)

    type: str

    def __init__(self, rule_type: str) -> None:
        self.type = rule_type


class ExactRule(ShellPermissionRule):
    __slots__ = ("command",)

    def __init__(self, command: str) -> None:
        super().__init__("exact")
        self.command = command


class PrefixRule(ShellPermissionRule):
    __slots__ = ("prefix",)

    def __init__(self, prefix: str) -> None:
        super().__init__("prefix")
        self.prefix = prefix


class WildcardRule(ShellPermissionRule):
    __slots__ = ("pattern",)

    def __init__(self, pattern: str) -> None:
        super().__init__("wildcard")
        self.pattern = pattern


# OpenSpace shellRuleMatching.ts L43-48.
def permission_rule_extract_prefix(rule_content: str) -> Optional[str]:
    """OpenSpace ``permissionRuleExtractPrefix`` — ``npm:*`` → ``"npm"`` / None."""
    m = re.match(r"^(.+):\*$", rule_content)
    return m.group(1) if m else None


def _has_wildcards(pattern: str) -> bool:
    """OpenSpace ``hasWildcards`` (shellRuleMatching.ts L54-78)."""
    if pattern.endswith(":*"):
        return False
    for i, ch in enumerate(pattern):
        if ch == "*":
            j = i - 1
            backslash_count = 0
            while j >= 0 and pattern[j] == "\\":
                backslash_count += 1
                j -= 1
            if backslash_count % 2 == 0:
                return True
    return False


# OpenSpace shellRuleMatching.ts L90-154 — Python port using translated regex.
_ESCAPED_STAR = "\x00STAR\x00"
_ESCAPED_BACKSLASH = "\x00BS\x00"
_REGEX_META = re.compile(r"([.+?^${}()|\[\]\\'\"])")


def match_wildcard_pattern(
    pattern: str, command: str, case_insensitive: bool = False
) -> bool:
    """OpenSpace ``matchWildcardPattern`` (shellRuleMatching.ts L90-154)."""
    trimmed = pattern.strip()

    processed: list[str] = []
    i = 0
    while i < len(trimmed):
        ch = trimmed[i]
        if ch == "\\" and i + 1 < len(trimmed):
            nxt = trimmed[i + 1]
            if nxt == "*":
                processed.append(_ESCAPED_STAR)
                i += 2
                continue
            if nxt == "\\":
                processed.append(_ESCAPED_BACKSLASH)
                i += 2
                continue
        processed.append(ch)
        i += 1
    proc_str = "".join(processed)

    escaped = _REGEX_META.sub(r"\\\1", proc_str)
    with_wildcards = re.sub(r"\*", ".*", escaped)

    regex_pattern = with_wildcards.replace(_ESCAPED_STAR, r"\*").replace(
        _ESCAPED_BACKSLASH, r"\\"
    )

    # Trailing " *" semantics.
    unescaped_star_count = proc_str.count("*")
    if regex_pattern.endswith(" .*") and unescaped_star_count == 1:
        regex_pattern = regex_pattern[:-3] + "( .*)?"

    flags = re.DOTALL
    if case_insensitive:
        flags |= re.IGNORECASE
    try:
        return re.fullmatch(regex_pattern, command, flags=flags) is not None
    except re.error:
        return False


# OpenSpace shellRuleMatching.ts L159-184.
def parse_permission_rule(rule_content: str) -> ShellPermissionRule:
    """OpenSpace ``parsePermissionRule``."""
    prefix = permission_rule_extract_prefix(rule_content)
    if prefix is not None:
        return PrefixRule(prefix=prefix)
    if _has_wildcards(rule_content):
        return WildcardRule(pattern=rule_content)
    return ExactRule(command=rule_content)


# ════════════════════════════════════════════════════════════════════════
# §3  strip_safe_wrappers
# ════════════════════════════════════════════════════════════════════════


# ``ENV_VAR_PATTERN`` — strict safe-value pattern.
_ENV_VAR_STRIP_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)=([A-Za-z0-9_./:\-]+)[ \t]+"
)


# ``SAFE_WRAPPER_PATTERNS``.
_SAFE_WRAPPER_PATTERNS: Tuple[re.Pattern[str], ...] = (
    # timeout with GNU flags.
    re.compile(
        r"^timeout[ \t]+"
        r"(?:(?:--(?:foreground|preserve-status|verbose)"
        r"|--(?:kill-after|signal)=[A-Za-z0-9_.+\-]+"
        r"|--(?:kill-after|signal)[ \t]+[A-Za-z0-9_.+\-]+"
        r"|-v"
        r"|-[ks][ \t]+[A-Za-z0-9_.+\-]+"
        r"|-[ks][A-Za-z0-9_.+\-]+)[ \t]+)*"
        r"(?:--[ \t]+)?"
        r"\d+(?:\.\d+)?[smhd]?[ \t]+"
    ),
    # time.
    re.compile(r"^time[ \t]+(?:--[ \t]+)?"),
    # nice — bare / -n N / -N.
    re.compile(r"^nice(?:[ \t]+-n[ \t]+-?\d+|[ \t]+-\d+)?[ \t]+(?:--[ \t]+)?"),
    # stdbuf short-fused only.
    re.compile(r"^stdbuf(?:[ \t]+-[ioe][LN0-9]+)+[ \t]+(?:--[ \t]+)?"),
    # nohup.
    re.compile(r"^nohup[ \t]+(?:--[ \t]+)?"),
)


def _strip_comment_lines(command: str) -> str:
    """OpenSpace ``stripCommentLines`` (L508-522)."""
    lines = command.split("\n")
    non_comment = [
        ln for ln in lines if ln.strip() and not ln.strip().startswith("#")
    ]
    if not non_comment:
        return command
    return "\n".join(non_comment)


def strip_safe_wrappers(command: str) -> str:
    """OpenSpace ``stripSafeWrappers`` (L524-615).

    Two-phase fixed-point stripper:

    - Phase 1: strip leading safe env vars + comments.
    - Phase 2: strip leading wrapper commands + comments (NOT env vars
      — they're command arguments once a wrapper took over).
    """
    if not command:
        return command

    stripped = command
    previous: Optional[str] = None

    # Phase 1: env vars + comments.
    while stripped != previous:
        previous = stripped
        stripped = _strip_comment_lines(stripped)
        m = _ENV_VAR_STRIP_RE.match(stripped)
        if m:
            var_name = m.group(1)
            if var_name in SAFE_ENV_VARS:
                stripped = _ENV_VAR_STRIP_RE.sub("", stripped, count=1)

    # Phase 2: wrapper commands + comments (no env stripping).
    previous = None
    while stripped != previous:
        previous = stripped
        stripped = _strip_comment_lines(stripped)
        for pattern in _SAFE_WRAPPER_PATTERNS:
            stripped = pattern.sub("", stripped, count=1)

    return stripped.strip()


# ════════════════════════════════════════════════════════════════════════
# §4  strip_all_leading_env_vars
# ════════════════════════════════════════════════════════════════════════


# aggressive env-var stripping pattern.
_AGGRESSIVE_ENV_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?)\+?="
    r"""(?:'[^'\n\r]*'|"(?:\\.|[^"$`\\\n\r])*"|\\.|[^ \t\n\r$`;|&()<>\\'"])*"""
    r"[ \t]+"
)


def strip_all_leading_env_vars(
    command: str, blocklist: Optional[re.Pattern[str]] = None
) -> str:
    """OpenSpace ``stripAllLeadingEnvVars`` (L733-776).

    Aggressive env-var stripping for deny/ask rule matching. Stops when
    *blocklist* matches the variable name (pass ``BINARY_HIJACK_VARS``
    when callers want LD_PRELOAD etc. preserved).
    """
    stripped = command
    previous: Optional[str] = None

    while stripped != previous:
        previous = stripped
        stripped = _strip_comment_lines(stripped)
        m = _AGGRESSIVE_ENV_RE.match(stripped)
        if not m:
            continue
        if blocklist is not None and blocklist.search(m.group(1)):
            break
        stripped = stripped[m.end() :]

    return stripped.strip()


# ════════════════════════════════════════════════════════════════════════
# §5  strip_wrappers_from_argv
# ════════════════════════════════════════════════════════════════════════


# ``TIMEOUT_FLAG_VALUE_RE``.
_TIMEOUT_FLAG_VALUE_RE = re.compile(r"^[A-Za-z0-9_.+\-]+$")
_TIMEOUT_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_TIMEOUT_SIG_FUSED_RE = re.compile(r"^--(?:kill-after|signal)=[A-Za-z0-9_.+\-]+$")
_TIMEOUT_SHORT_FUSED_RE = re.compile(r"^-[ks][A-Za-z0-9_.+\-]+$")
_INT_RE = re.compile(r"^-?\d+$")


def _skip_timeout_flags(a: List[str]) -> int:
    """OpenSpace ``skipTimeoutFlags`` (L633-668)."""
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


def strip_wrappers_from_argv(argv: List[str]) -> List[str]:
    """OpenSpace ``stripWrappersFromArgv`` (L678-701).

    Narrower than the canonical pathValidation.ts copy — strips only
    ``time`` / ``nohup`` / ``timeout`` / ``nice``. Kept for parity with
    OpenSpace's two-copy layout (see ). New callers should prefer
    :func:`bash_path_validation.strip_wrappers_from_argv`.
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
            a = a[i + 1 :]
        elif (
            head == "nice"
            and len(a) > 2
            and a[1] == "-n"
            and _INT_RE.match(a[2])
        ):
            a = a[4:] if len(a) > 3 and a[3] == "--" else a[3:]
        else:
            return a


# ════════════════════════════════════════════════════════════════════════
# §6  Suggestion builders
# ════════════════════════════════════════════════════════════════════════


def get_simple_command_prefix(command: str) -> Optional[str]:
    """OpenSpace ``getSimpleCommandPrefix`` (L161-188)."""
    tokens = [t for t in command.strip().split() if t]
    if not tokens:
        return None

    i = 0
    while i < len(tokens) and _ENV_VAR_ASSIGN_RE.match(tokens[i]):
        var_name = tokens[i].split("=", 1)[0]
        if var_name not in SAFE_ENV_VARS:
            return None
        i += 1

    remaining = tokens[i:]
    if len(remaining) < 2:
        return None
    subcmd = remaining[1]
    if not _SUBCOMMAND_SHAPE_RE.match(subcmd):
        return None
    return " ".join(remaining[:2])


def get_first_word_prefix(command: str) -> Optional[str]:
    """OpenSpace ``getFirstWordPrefix`` (L243-264)."""
    tokens = [t for t in command.strip().split() if t]

    i = 0
    while i < len(tokens) and _ENV_VAR_ASSIGN_RE.match(tokens[i]):
        var_name = tokens[i].split("=", 1)[0]
        if var_name not in SAFE_ENV_VARS:
            return None
        i += 1

    if i >= len(tokens):
        return None
    cmd = tokens[i]
    if not _SUBCOMMAND_SHAPE_RE.match(cmd):
        return None
    if cmd in BARE_SHELL_PREFIXES:
        return None
    return cmd


def _extract_prefix_before_heredoc(command: str) -> Optional[str]:
    """OpenSpace ``extractPrefixBeforeHeredoc`` (L307-337)."""
    if "<<" not in command:
        return None
    idx = command.index("<<")
    if idx <= 0:
        return None
    before = command[:idx].strip()
    if not before:
        return None
    prefix = get_simple_command_prefix(before)
    if prefix:
        return prefix
    tokens = [t for t in before.split() if t]
    i = 0
    while i < len(tokens) and _ENV_VAR_ASSIGN_RE.match(tokens[i]):
        var_name = tokens[i].split("=", 1)[0]
        if var_name not in SAFE_ENV_VARS:
            return None
        i += 1
    if i >= len(tokens):
        return None
    return " ".join(tokens[i : i + 2]) or None


_BASH_TOOL_NAME = "bash"


def suggestion_for_prefix(prefix: str) -> Tuple[PermissionUpdate, ...]:
    """OpenSpace ``suggestionForPrefix`` (L339-341)."""
    return (
        AddRulesUpdate(
            destination="localSettings",
            rules=(
                PermissionRuleValue(
                    tool_name=_BASH_TOOL_NAME,
                    rule_content=f"{prefix}:*",
                ),
            ),
            behavior="allow",
        ),
    )


def suggestion_for_exact_command(command: str) -> Tuple[PermissionUpdate, ...]:
    """OpenSpace ``suggestionForExactCommand`` (L266-295)."""
    heredoc_prefix = _extract_prefix_before_heredoc(command)
    if heredoc_prefix:
        return suggestion_for_prefix(heredoc_prefix)

    if "\n" in command:
        first_line = command.split("\n", 1)[0].strip()
        if first_line:
            return suggestion_for_prefix(first_line)

    prefix = get_simple_command_prefix(command)
    if prefix:
        return suggestion_for_prefix(prefix)

    return (
        AddRulesUpdate(
            destination="localSettings",
            rules=(
                PermissionRuleValue(
                    tool_name=_BASH_TOOL_NAME,
                    rule_content=command,
                ),
            ),
            behavior="allow",
        ),
    )


# ════════════════════════════════════════════════════════════════════════
# §7  Rule-content matchers
# ════════════════════════════════════════════════════════════════════════


MatchMode = Literal["exact", "prefix"]


def _collect_rules_for_tool(
    context: ToolPermissionContext,
    tool_name: str,
    behavior: PermissionBehavior,
) -> Dict[str, PermissionRule]:
    """OpenSpace ``getRuleByContentsForTool`` — returns a ``{rule_content:
    PermissionRule}`` map for *tool_name* + *behavior* merged across
    rule sources (OpenSpace precedence is inherent in storage order).
    """
    source_map = {
        "allow": context.always_allow_rules,
        "deny": context.always_deny_rules,
        "ask": context.always_ask_rules,
    }[behavior]

    out: Dict[str, PermissionRule] = {}
    for source, rule_contents in source_map.items():
        for raw in rule_contents:
            try:
                rv = parse_rule_value(raw)
            except ValueError:
                continue
            if normalize_tool_name_for_rule(rv.tool_name) != tool_name:
                continue
            if rv.rule_content is None:
                # Tool-wide rules don't participate in content matching.
                continue
            if rv.rule_content in out:
                continue  # first-wins per source order
            out[rv.rule_content] = PermissionRule(
                source=source,
                rule_behavior=behavior,
                rule_value=rv,
            )
    return out


def filter_rules_by_contents_matching_input(
    rules: Mapping[str, PermissionRule],
    command: str,
    match_mode: MatchMode = "prefix",
    strip_all_env_vars: bool = False,
    skip_compound_check: bool = False,
) -> List[PermissionRule]:
    """OpenSpace ``filterRulesByContentsMatchingInput`` (L778-935).

    Produces the set of commands to test (original, redirection-stripped,
    wrapper-stripped, env-stripped when *strip_all_env_vars*) and walks
    each rule under the precedence prescribed by its type (exact vs
    prefix vs wildcard) and the ``match_mode`` (exact rules only match
    exactly; prefix rules only match after splitting; wildcards never
    match when mode=``exact``).
    """
    command = command.strip()
    redir = extract_output_redirections(command)
    command_no_redir = redir.command_without_redirections or command

    if match_mode == "exact":
        commands_for_matching = [command, command_no_redir]
    else:
        commands_for_matching = [command_no_redir]

    # Add wrapper-stripped variants.
    commands_to_try: List[str] = []
    for c in commands_for_matching:
        commands_to_try.append(c)
        stripped = strip_safe_wrappers(c)
        if stripped != c and stripped not in commands_to_try:
            commands_to_try.append(stripped)

    # Aggressive env strip for deny/ask rules.
    if strip_all_env_vars:
        seen = set(commands_to_try)
        start_idx = 0
        while start_idx < len(commands_to_try):
            end_idx = len(commands_to_try)
            for i in range(start_idx, end_idx):
                c = commands_to_try[i]
                env_stripped = strip_all_leading_env_vars(c)
                if env_stripped not in seen:
                    commands_to_try.append(env_stripped)
                    seen.add(env_stripped)
                wrapper_stripped = strip_safe_wrappers(c)
                if wrapper_stripped not in seen:
                    commands_to_try.append(wrapper_stripped)
                    seen.add(wrapper_stripped)
            start_idx = end_idx

    # Precompute compound-command status for the prefix-allow guard
    #.
    is_compound: Dict[str, bool] = {}
    if match_mode == "prefix" and not skip_compound_check:
        for c in commands_to_try:
            if c not in is_compound:
                is_compound[c] = len(split_command_segments(c)) > 1

    matched: List[PermissionRule] = []
    for rule_content, rule in rules.items():
        parsed = parse_permission_rule(rule_content)
        for cmd_to_match in commands_to_try:
            if _rule_matches_command(parsed, cmd_to_match, match_mode, is_compound):
                matched.append(rule)
                break
    return matched


def _rule_matches_command(
    rule: ShellPermissionRule,
    command: str,
    match_mode: MatchMode,
    is_compound: Dict[str, bool],
) -> bool:
    """OpenSpace rule-type switch (L874-932)."""
    if isinstance(rule, ExactRule):
        return rule.command == command

    if isinstance(rule, PrefixRule):
        if match_mode == "exact":
            return rule.prefix == command
        if is_compound.get(command, False):
            return False
        if command == rule.prefix:
            return True
        if command.startswith(rule.prefix + " "):
            return True
        xargs_prefix = "xargs " + rule.prefix
        if command == xargs_prefix:
            return True
        return command.startswith(xargs_prefix + " ")

    if isinstance(rule, WildcardRule):
        if match_mode == "exact":
            return False
        if is_compound.get(command, False):
            return False
        return match_wildcard_pattern(rule.pattern, command)

    return False


class MatchingRulesResult:
    """OpenSpace ``matchingRulesForInput`` return shape (L981-985)."""

    __slots__ = (
        "matching_deny_rules",
        "matching_ask_rules",
        "matching_allow_rules",
    )

    def __init__(
        self,
        matching_deny_rules: List[PermissionRule],
        matching_ask_rules: List[PermissionRule],
        matching_allow_rules: List[PermissionRule],
    ) -> None:
        self.matching_deny_rules = matching_deny_rules
        self.matching_ask_rules = matching_ask_rules
        self.matching_allow_rules = matching_allow_rules


def matching_rules_for_input(
    tool_name: str,
    command: str,
    match_mode: MatchMode,
    context: ToolPermissionContext,
    skip_compound_check: bool = False,
) -> MatchingRulesResult:
    """OpenSpace ``matchingRulesForInput`` (L937-986).

    Task signature adaptation: OpenSpace takes ``(input, ctx, matchMode)``; the
    OS signature collapses ``input.command`` into the ``command`` arg and
    makes *tool_name* explicit so the engine can reuse this entry point
    for both ``bash`` and ``Bash`` rule storage (settings.json is often
    authored with OpenSpace naming).
    """
    deny_rules = _collect_rules_for_tool(context, tool_name, "deny")
    matching_deny = filter_rules_by_contents_matching_input(
        deny_rules,
        command,
        match_mode=match_mode,
        strip_all_env_vars=True,
        skip_compound_check=True,
    )

    ask_rules = _collect_rules_for_tool(context, tool_name, "ask")
    matching_ask = filter_rules_by_contents_matching_input(
        ask_rules,
        command,
        match_mode=match_mode,
        strip_all_env_vars=True,
        skip_compound_check=True,
    )

    allow_rules = _collect_rules_for_tool(context, tool_name, "allow")
    matching_allow = filter_rules_by_contents_matching_input(
        allow_rules,
        command,
        match_mode=match_mode,
        strip_all_env_vars=False,
        skip_compound_check=skip_compound_check,
    )

    return MatchingRulesResult(
        matching_deny_rules=matching_deny,
        matching_ask_rules=matching_ask,
        matching_allow_rules=matching_allow,
    )


# ════════════════════════════════════════════════════════════════════════
# §8  bash_tool_check_exact_match_permission
# ════════════════════════════════════════════════════════════════════════


def _bash_deny_message(command: str) -> str:
    return f"Permission to use bash with command {command} has been denied."


def _bash_ask_message() -> str:
    return "OpenSpace needs permission to run a bash command"


def _result_for_rule(
    command: str,
    rule: PermissionRule,
) -> PermissionResult:
    if rule.rule_behavior == "deny":
        return PermissionDeny(
            message=_bash_deny_message(command),
            decision_reason=DecisionReasonRule(rule=rule),
        )
    if rule.rule_behavior == "ask":
        return PermissionAsk(
            message=_bash_ask_message(),
            decision_reason=DecisionReasonRule(rule=rule),
        )
    return PermissionAllow(
        updated_input={"command": command},
        decision_reason=DecisionReasonRule(rule=rule),
    )


def bash_tool_check_exact_match_permission(
    command: str,
    context: ToolPermissionContext,
    tool_name: str = "bash",
) -> PermissionResult:
    """OpenSpace ``bashToolCheckExactMatchPermission`` (L991-1048)."""
    command = command.strip()
    matches = matching_rules_for_input(tool_name, command, "exact", context)

    if matches.matching_deny_rules:
        return _result_for_rule(command, matches.matching_deny_rules[0])

    if matches.matching_ask_rules:
        return _result_for_rule(command, matches.matching_ask_rules[0])

    if matches.matching_allow_rules:
        return _result_for_rule(command, matches.matching_allow_rules[0])

    reason = DecisionReasonOther(reason="This command requires approval")
    return PermissionPassthrough(
        message=_bash_ask_message(),
        decision_reason=reason,
        suggestions=suggestion_for_exact_command(command),
    )


# ════════════════════════════════════════════════════════════════════════
# §9  bash_tool_check_permission
# ════════════════════════════════════════════════════════════════════════


def bash_tool_check_permission(
    command: str,
    cwd: str,
    context: ToolPermissionContext,
    compound_command_has_cd: bool = False,
    original_cwd: Optional[str] = None,
    tool_name: str = "bash",
) -> PermissionResult:
    """OpenSpace ``bashToolCheckPermission`` (L1050-1178).

    Decision order:

    1. Exact match → deny/ask short-circuit.
    2. Prefix match → deny → ask.
    3. Path constraint check (34 commands).
    4. Exact-allow from step 1 → allow.
    5. Prefix allow rule → allow.
    6. Mode-based decision (:func:`_check_permission_mode`).
    7. Read-only classifier (:func:`bash_classifier.check_read_only_constraints`) → allow.
    8. Passthrough with exact-command suggestion.
    """
    command = command.strip()

    # §1 — exact
    exact = bash_tool_check_exact_match_permission(command, context, tool_name)
    if isinstance(exact, (PermissionDeny, PermissionAsk)):
        return exact

    # §2 — prefix
    prefix_matches = matching_rules_for_input(tool_name, command, "prefix", context)
    if prefix_matches.matching_deny_rules:
        return _result_for_rule(command, prefix_matches.matching_deny_rules[0])
    if prefix_matches.matching_ask_rules:
        return _result_for_rule(command, prefix_matches.matching_ask_rules[0])

    # §3 — path constraints
    path_result = check_path_constraints(
        command, cwd, context, compound_command_has_cd
    )
    if not isinstance(path_result, PermissionPassthrough):
        return path_result

    # §4 — exact-allow from §1
    if isinstance(exact, PermissionAllow):
        return exact

    # §5 — prefix-allow
    if prefix_matches.matching_allow_rules:
        return _result_for_rule(command, prefix_matches.matching_allow_rules[0])

    # §6 — mode-based
    mode_result = _check_permission_mode(command, context)
    if not isinstance(mode_result, PermissionPassthrough):
        return mode_result

    # §7 — read-only classifier.
    from ..security.bash_classifier import check_read_only_constraints

    ro = check_read_only_constraints(
        command,
        compound_command_has_cd=compound_command_has_cd,
        cwd=cwd,
        original_cwd=original_cwd,
    )
    if ro.get("behavior") == "allow":
        return PermissionAllow(
            updated_input={"command": command},
            decision_reason=DecisionReasonOther(
                reason="Read-only command is allowed"
            ),
        )

    # §8 — passthrough with exact suggestion.
    reason = DecisionReasonOther(reason="This command requires approval")
    return PermissionPassthrough(
        message=_bash_ask_message(),
        decision_reason=reason,
        suggestions=suggestion_for_exact_command(command),
    )


def check_rule_based_permissions_for_bash(
    command: str,
    subcommands: List[str],
    context: ToolPermissionContext,
    cwd: str,
    compound_command_has_cd: bool = False,
    original_cwd: Optional[str] = None,
    tool_name: str = "bash",
) -> Optional[PermissionResult]:
    """Task-spec entry wrapping :func:`bash_tool_check_permission`.

    Runs :func:`bash_tool_check_permission` on *command* (not each
    subcommand) and returns the first concrete (non-passthrough)
    decision, or ``None`` if the caller should continue the pipeline.

    The *subcommands* argument is kept for call-site parity with OpenSpace (it
    pre-computes ``splitCommandSegments`` output for compound merging)
    but is not consulted here — :func:`bash_tool_check_permission`
    re-derives it internally via :func:`check_path_constraints`.
    """
    result = bash_tool_check_permission(
        command,
        cwd,
        context,
        compound_command_has_cd=compound_command_has_cd,
        original_cwd=original_cwd,
        tool_name=tool_name,
    )
    if isinstance(result, PermissionPassthrough):
        return None
    return result


# ════════════════════════════════════════════════════════════════════════
# §10  Mode-based check (OpenSpace modeValidation.ts ``checkPermissionMode``)
# ════════════════════════════════════════════════════════════════════════


def _check_permission_mode(
    command: str,
    context: ToolPermissionContext,
) -> PermissionResult:
    """OpenSpace ``checkPermissionMode`` (modeValidation.ts).

    Bash never auto-allows under ``acceptEdits`` (OpenSpace behaviour — the mode
    only lifts the ask for file-modifying tools). ``bypassPermissions``
    auto-allows if the escape hatch is available. ``plan`` denies by
    default.
    """
    mode = context.mode

    if mode == "bypassPermissions":
        if context.is_bypass_permissions_mode_available:
            return PermissionAllow(
                updated_input={"command": command},
                decision_reason=DecisionReasonMode(mode=mode),
            )
        return PermissionPassthrough(
            message="bypassPermissions not available; fall through"
        )

    if mode == "plan":
        return PermissionDeny(
            message="Plan mode forbids bash execution.",
            decision_reason=DecisionReasonMode(mode=mode),
        )

    # default / acceptEdits / dontAsk / auto / bubble → passthrough.
    return PermissionPassthrough(message="Mode not decisive for bash")


# ════════════════════════════════════════════════════════════════════════
# §11  check_sandbox_auto_allow
# ════════════════════════════════════════════════════════════════════════


def check_sandbox_auto_allow(
    command: str,
    cwd: str,
    context: ToolPermissionContext,
    tool_name: str = "bash",
    connector_kind: str = "local",
    *,
    dangerously_disable_sandbox: bool = False,
) -> Optional[PermissionResult]:
    """OpenSpace ``checkSandboxAutoAllow`` (L1270-1359).

    Only returns allow when the process sandbox is enabled and this command
    will actually be wrapped. Explicit deny/ask rules still win.
    """
    from openspace.services.sandbox import get_process_sandbox_manager
    from openspace.services.sandbox.should_use_sandbox import (
        ShouldUseSandboxInput,
        should_use_sandbox,
    )

    manager = get_process_sandbox_manager(cwd=cwd)
    if not manager.is_sandboxing_enabled():
        return None
    if not manager.is_auto_allow_bash_if_sandboxed_enabled():
        return None
    decision = should_use_sandbox(
        ShouldUseSandboxInput(
            command=command,
            dangerously_disable_sandbox=dangerously_disable_sandbox,
            cwd=cwd,
            connector_kind=connector_kind,
        ),
        sandbox_manager=manager,
    )
    if not decision.should_sandbox:
        return None

    command = command.strip()
    full_matches = matching_rules_for_input(tool_name, command, "prefix", context)
    if full_matches.matching_deny_rules:
        return PermissionDeny(
            message=_bash_deny_message(command),
            decision_reason=DecisionReasonRule(rule=full_matches.matching_deny_rules[0]),
        )

    subcommands = split_command_segments(command)
    if len(subcommands) > 1:
        first_ask_rule: PermissionRule | None = None
        for subcommand in subcommands:
            sub_matches = matching_rules_for_input(
                tool_name,
                subcommand,
                "prefix",
                context,
            )
            if sub_matches.matching_deny_rules:
                return PermissionDeny(
                    message=_bash_deny_message(command),
                    decision_reason=DecisionReasonRule(
                        rule=sub_matches.matching_deny_rules[0]
                    ),
                )
            if first_ask_rule is None and sub_matches.matching_ask_rules:
                first_ask_rule = sub_matches.matching_ask_rules[0]
        if first_ask_rule is not None:
            return PermissionAsk(
                message=_bash_ask_message(),
                decision_reason=DecisionReasonRule(rule=first_ask_rule),
            )

    if full_matches.matching_ask_rules:
        return PermissionAsk(
            message=_bash_ask_message(),
            decision_reason=DecisionReasonRule(rule=full_matches.matching_ask_rules[0]),
        )

    return PermissionAllow(
        updated_input={"command": command},
        decision_reason=DecisionReasonSandboxOverride(reason="sandboxed"),
    )


# ════════════════════════════════════════════════════════════════════════
# §12  check_legacy_misparsing
# ════════════════════════════════════════════════════════════════════════


def check_legacy_misparsing(command: str, cwd: str) -> Optional[PermissionResult]:
    """Ask for approval when shell injection patterns could confuse parsing.

    Returns:

    - ``None`` if the command is parsed cleanly (continue pipeline).
    - :class:`PermissionAsk` with ``is_bash_security_check_for_misparsing=True``
      if the conservative injection gate flags it.
    """
    del cwd
    safety = bash_command_passes_injection_gate(command)
    if safety.get("behavior") == "allow":
        return None

    msg = safety.get("message") or (
        "Command contains patterns that could pose security risks "
        "and requires approval"
    )
    return PermissionAsk(
        message=msg,
        decision_reason=DecisionReasonOther(reason=msg),
        is_bash_security_check_for_misparsing=True,
    )


# ════════════════════════════════════════════════════════════════════════
# §13  check_compound_command_permissions
# ════════════════════════════════════════════════════════════════════════


def _extract_rules_from_updates(
    updates: Optional[Iterable[PermissionUpdate]],
) -> List[PermissionRuleValue]:
    """OpenSpace ``extractRules`` (PermissionUpdate.ts)."""
    out: List[PermissionRuleValue] = []
    if not updates:
        return out
    for u in updates:
        if isinstance(u, AddRulesUpdate):
            out.extend(u.rules)
    return out


def check_compound_command_permissions(
    command: str,
    subcommands: List[str],
    context: ToolPermissionContext,
    cwd: str,
    compound_command_has_cd: bool = False,
    original_cwd: Optional[str] = None,
    tool_name: str = "bash",
) -> PermissionResult:
    """compound merge loop.

    For each subcommand:

    1. Run :func:`bash_tool_check_permission`.
    2. Collect results.
    3. Merge by precedence: deny > ask > allow > passthrough.
    4. If ask/passthrough dominates, aggregate suggestions per
       subcommand (capped at :data:`MAX_SUGGESTED_RULES_FOR_COMPOUND`).
    """
    subcommand_results: Dict[str, PermissionResult] = {}
    for sub in subcommands:
        sub = sub.strip()
        if not sub:
            continue
        subcommand_results[sub] = bash_tool_check_permission(
            sub,
            cwd,
            context,
            compound_command_has_cd=compound_command_has_cd,
            original_cwd=original_cwd,
            tool_name=tool_name,
        )

    # §Deny wins.
    for _, result in subcommand_results.items():
        if isinstance(result, PermissionDeny):
            return PermissionDeny(
                message=_bash_deny_message(command),
                decision_reason=DecisionReasonSubcommandResults(
                    reasons=dict(subcommand_results)
                ),
            )

    # §All-allowed.
    if subcommand_results and all(
        isinstance(r, PermissionAllow) for r in subcommand_results.values()
    ):
        return PermissionAllow(
            updated_input={"command": command},
            decision_reason=DecisionReasonSubcommandResults(
                reasons=dict(subcommand_results)
            ),
        )

    # §Collect suggestions from ask/passthrough.
    collected: Dict[str, PermissionRuleValue] = {}
    for sub, result in subcommand_results.items():
        if isinstance(result, PermissionAllow):
            continue
        suggestions_attr = getattr(result, "suggestions", None)
        rules = _extract_rules_from_updates(suggestions_attr)
        for rv in rules:
            collected[format_rule_value(rv)] = rv

        # OpenSpace GH#28784: synthesize an exact-Bash rule for ask subcommands
        # that carried no suggestions (safety-check asks).
        if (
            isinstance(result, PermissionAsk)
            and not rules
            and not isinstance(result.decision_reason, DecisionReasonRule)
        ):
            for rv in _extract_rules_from_updates(
                suggestion_for_exact_command(sub)
            ):
                collected[format_rule_value(rv)] = rv

    capped = list(collected.values())[:MAX_SUGGESTED_RULES_FOR_COMPOUND]
    suggested_updates: Optional[Tuple[PermissionUpdate, ...]] = (
        (
            AddRulesUpdate(
                destination="localSettings",
                rules=tuple(capped),
                behavior="allow",
            ),
        )
        if capped
        else None
    )

    has_ask = any(
        isinstance(r, PermissionAsk) for r in subcommand_results.values()
    )

    if has_ask:
        return PermissionAsk(
            message=_bash_ask_message(),
            decision_reason=DecisionReasonSubcommandResults(
                reasons=dict(subcommand_results)
            ),
            suggestions=suggested_updates,
        )

    return PermissionPassthrough(
        message=_bash_ask_message(),
        decision_reason=DecisionReasonSubcommandResults(
            reasons=dict(subcommand_results)
        ),
        suggestions=suggested_updates,
    )


# ════════════════════════════════════════════════════════════════════════
# §14  Command identity helpers
# ════════════════════════════════════════════════════════════════════════


def is_normalized_git_command(command: str) -> bool:
    """OpenSpace ``isNormalizedGitCommand`` (L2567-2588). Delegates to
    :func:`core.security.shell_parser.is_normalized_git_command`.
    """
    return _security_is_normalized_git_command(command)


def is_normalized_cd_command(command: str) -> bool:
    """OpenSpace ``isNormalizedCdCommand`` (L2603-2611)."""
    return _security_is_normalized_cd_command(command)


def command_has_any_cd(command: str) -> bool:
    """OpenSpace ``commandHasAnyCd`` (L2617-2621)."""
    return _security_command_has_any_cd(command)


def command_has_any_git(command: str) -> bool:
    """OpenSpace ``commandHasAnyGit``. Mirror of :func:`command_has_any_cd`."""
    return _security_command_has_any_git(command)


# ════════════════════════════════════════════════════════════════════════
# §15  Main entry: bash_tool_has_permission
# ════════════════════════════════════════════════════════════════════════


async def bash_tool_has_permission(
    command: str,
    cwd: str,
    description: Optional[str],
    context: ToolPermissionContext,
    tool_name: str = "bash",
    connector_kind: str = "local",
    *,
    original_cwd: Optional[str] = None,
    dangerously_disable_sandbox: bool = False,
) -> PermissionResult:
    """OpenSpace ``bashToolHasPermission`` (L1663-2557) — main entry point.

    Decision pipeline (see module docstring for full OpenSpace cross-reference):

    1. Strip safe wrappers off the raw command (used for prefix matching
       by downstream ``filterRulesByContentsMatchingInput``).
    2. Sandbox auto-allow when the process sandbox will wrap the command.
    3. Split into subcommands via ``splitCommandSegments``.
    4. Exact-match deny/ask/allow — deny short-circuits.
    5. Operator check (``check_command_operator_permissions``) for pipes.
    6. Shell injection misparsing check.
    7. Multiple-``cd`` guard + compound ``cd`` + ``git`` guard.
    8. Fan-out cap (:data:`MAX_SUBCOMMANDS_FOR_SECURITY_CHECK`).
    9. Per-subcommand :func:`bash_tool_check_permission` merge.
    10. Path constraint check on the compound command.
    11. Compound merge via :func:`check_compound_command_permissions`.
    12. Fall through to ``ask`` with suggestion.

    Notes:

    - The signature returns a coroutine to match OpenSpace's async contract;
      every branch completes synchronously in OS (no LLM calls).
    - *description* is currently unused but kept for future use by
      classifier integrations.
    """
    del description  # reserved for future classifier wiring
    command = command.strip()
    mode: PermissionMode = context.mode

    # §0 — empty command short-circuit (defence).
    if not command:
        return PermissionAsk(
            message="Empty command cannot be executed.",
            decision_reason=DecisionReasonOther(reason="Empty command"),
        )

    # §1 — passthrough bypass mode.
    # Exact-match deny is evaluated first to ensure explicit user denies
    # still win (mirrors ).
    exact_match_result = bash_tool_check_exact_match_permission(
        command, context, tool_name
    )
    if isinstance(exact_match_result, PermissionDeny):
        return exact_match_result

    # §2 — sandbox auto-allow.
    sandbox = check_sandbox_auto_allow(
        command,
        cwd,
        context,
        tool_name,
        connector_kind,
        dangerously_disable_sandbox=dangerously_disable_sandbox,
    )
    if sandbox is not None and not isinstance(sandbox, PermissionPassthrough):
        return sandbox

    # §3 — operator / pipe handling.
    #      Sole place where we need the recursion hook.
    checkers = CommandIdentityCheckers(
        is_normalized_cd_command=is_normalized_cd_command,
        is_normalized_git_command=is_normalized_git_command,
    )

    async def _recurse(
        sub_command: str,
        sub_cwd: str,
        _sub_description: Optional[str],
        sub_context: ToolPermissionContext,
    ) -> PermissionResult:
        return await bash_tool_has_permission(
            sub_command,
            sub_cwd,
            None,
            sub_context,
            tool_name,
            connector_kind=connector_kind,
            original_cwd=original_cwd,
            dangerously_disable_sandbox=dangerously_disable_sandbox,
        )

    operator_result = await check_command_operator_permissions(
        command=command,
        recurse=_recurse,
        checkers=checkers,
        context=context,
        cwd=cwd,
    )
    if isinstance(operator_result, PermissionDeny):
        return operator_result
    if isinstance(operator_result, PermissionAsk):
        # Validate the full command's redirections on top of the pipe's
        # ask — the pipe path already stripped them per-segment.
        return operator_result
    if isinstance(operator_result, PermissionAllow):
        # Validate redirections + per-path constraints on the full
        # command even when pipe-segments allowed
        path_on_full = check_path_constraints(
            command,
            cwd,
            context,
            compound_command_has_cd=command_has_any_cd(command),
        )
        if not isinstance(path_on_full, PermissionPassthrough):
            return path_on_full
        return operator_result
    # passthrough → continue

    # §4 — legacy misparsing check.
    misparse = check_legacy_misparsing(command, cwd)
    if misparse is not None:
        # : an exact-allow overrides misparse-ask.
        if isinstance(exact_match_result, PermissionAllow):
            return exact_match_result
        return misparse

    # §5 — subcommand splitting + guards.
    subcommands_raw = split_command_segments(command) or [command]
    subcommands = [
        sub for sub in subcommands_raw if sub.strip() and sub.strip() != f"cd {cwd}"
    ] or subcommands_raw

    cd_subcommands = [s for s in subcommands if is_normalized_cd_command(s.strip())]
    if len(cd_subcommands) > 1:
        reason = (
            "Multiple directory changes in one command "
            "require approval for clarity"
        )
        return PermissionAsk(
            message=reason,
            decision_reason=DecisionReasonOther(reason=reason),
        )

    compound_has_cd = len(cd_subcommands) > 0

    if compound_has_cd:
        has_git = any(
            is_normalized_git_command(s.strip()) for s in subcommands
        )
        if has_git:
            reason = (
                "Compound commands with cd and git require approval "
                "to prevent bare repository attacks"
            )
            return PermissionAsk(
                message=reason,
                decision_reason=DecisionReasonOther(reason=reason),
            )

    # §6 — fan-out cap.
    if len(subcommands) > MAX_SUBCOMMANDS_FOR_SECURITY_CHECK:
        reason = (
            f"Command splits into {len(subcommands)} subcommands, "
            "too many to safety-check individually"
        )
        return PermissionAsk(
            message=reason,
            decision_reason=DecisionReasonOther(reason=reason),
        )

    # §7 — full-command path constraints.
    #      Must run on the ORIGINAL command so output redirections are
    #      validated (splitCommand strips them before subcommand checks).
    path_result = check_path_constraints(
        command, cwd, context, compound_command_has_cd=compound_has_cd
    )
    if isinstance(path_result, PermissionDeny):
        return path_result

    # §8 — single-subcommand fast path.
    if len(subcommands) == 1:
        result = bash_tool_check_permission(
            subcommands[0].strip(),
            cwd,
            context,
            compound_command_has_cd=compound_has_cd,
            original_cwd=original_cwd,
            tool_name=tool_name,
        )
        if isinstance(result, PermissionPassthrough):
            # No classifier follow-up is available here; fall through to ask
            # with whatever suggestions the passthrough carries.
            return PermissionAsk(
                message=result.message,
                decision_reason=result.decision_reason,
                suggestions=result.suggestions,
                blocked_path=result.blocked_path,
            )
        if isinstance(path_result, PermissionAsk) and isinstance(
            result, PermissionAllow
        ):
            # path-constraint ask shouldn't be silently swallowed.
            return path_result
        return result

    # §9 — multi-subcommand merge.
    compound = check_compound_command_permissions(
        command,
        [s.strip() for s in subcommands],
        context,
        cwd,
        compound_command_has_cd=compound_has_cd,
        original_cwd=original_cwd,
        tool_name=tool_name,
    )

    # If the full-command path check returned an ask and no subcommand
    # asked by itself, surface it.
    if isinstance(path_result, PermissionAsk):
        if isinstance(compound, (PermissionAllow,)):
            return path_result
        if isinstance(compound, PermissionPassthrough):
            return path_result

    if isinstance(compound, PermissionPassthrough):
        # — convert passthrough to ask (no classifier).
        return PermissionAsk(
            message=compound.message,
            decision_reason=compound.decision_reason,
            suggestions=compound.suggestions,
        )

    return compound
