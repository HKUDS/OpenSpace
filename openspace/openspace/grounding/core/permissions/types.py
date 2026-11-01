"""Permission type definitions for the OpenSpace permission engine.

The public Python API uses snake_case while preserving the same branch
structure and field semantics used by persisted permission records. Where
wire data uses discriminated unions via
``type: 'allow' | 'ask' | 'deny' | 'passthrough'``, OS uses frozen
dataclasses that all subclass :class:`PermissionResultBase` and expose a
``behavior`` literal.

Naming notes:

- OpenSpace ``ruleBehavior``              → ``rule_behavior``
- OpenSpace ``ruleValue``                 → ``rule_value``
- OpenSpace ``toolName``                  → ``tool_name``
- OpenSpace ``ruleContent``               → ``rule_content``
- OpenSpace ``decisionReason``            → ``decision_reason``
- OpenSpace ``updatedInput``              → ``updated_input``
- OpenSpace ``blockedPath``               → ``blocked_path``
- OpenSpace ``pendingClassifierCheck``    → ``pending_classifier_check``
- OpenSpace ``userModified``              → ``user_modified``
- OpenSpace ``toolUseID``                 → ``tool_use_id``
- OpenSpace ``additionalWorkingDirectories`` → ``additional_working_directories``
- OpenSpace ``alwaysAllowRules``          → ``always_allow_rules``
- OpenSpace ``alwaysDenyRules``           → ``always_deny_rules``
- OpenSpace ``alwaysAskRules``            → ``always_ask_rules``
- OpenSpace ``isBypassPermissionsModeAvailable`` → ``is_bypass_permissions_mode_available``
- OpenSpace ``strippedDangerousRules``    → ``stripped_dangerous_rules``
- OpenSpace ``shouldAvoidPermissionPrompts`` → ``should_avoid_permission_prompts``
- OpenSpace ``awaitAutomatedChecksBeforeDialog`` → ``await_automated_checks_before_dialog``
- OpenSpace ``prePlanMode``               → ``pre_plan_mode``
- OpenSpace ``classifierApprovable``      → ``classifier_approvable``
- OpenSpace ``isBashSecurityCheckForMisparsing`` → ``is_bash_security_check_for_misparsing``
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    Literal,
    Mapping,
    Optional,
    Tuple,
    Union,
)


# ════════════════════════════════════════════════════════════════════════
# §1  Permission Modes
# ════════════════════════════════════════════════════════════════════════

# User-addressable modes exposed via CLI flag, settings.json defaultMode,
# or --permission-mode.
EXTERNAL_PERMISSION_MODES: Tuple[str, ...] = (
    "acceptEdits",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
)

ExternalPermissionMode = Literal[
    "acceptEdits", "bypassPermissions", "default", "dontAsk", "plan"
]

# Superset including transient internal modes. ``auto`` is kept as a valid
# enum value for persisted-record compatibility but is not user-addressable.
# ``bubble`` is an internal transitional mode used only inside the engine.
InternalPermissionMode = Literal[
    "acceptEdits",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
    "auto",
    "bubble",
]

PermissionMode = InternalPermissionMode

# Public mode tables intentionally exclude internal transient modes from
# user-addressable settings.
INTERNAL_PERMISSION_MODES: Tuple[str, ...] = EXTERNAL_PERMISSION_MODES
PERMISSION_MODES: Tuple[str, ...] = INTERNAL_PERMISSION_MODES


# ════════════════════════════════════════════════════════════════════════
# §2  Permission Behaviors
# ════════════════════════════════════════════════════════════════════════

PermissionBehavior = Literal["allow", "deny", "ask"]


# ════════════════════════════════════════════════════════════════════════
# §3  Permission Rule Sources
# ════════════════════════════════════════════════════════════════════════

# OpenSpace-supported permission rule sources. Order matters: later sources
# override earlier ones when merging into ``ToolPermissionContext``.
# - ``userSettings``    ~/.openspace/settings.json
# - ``projectSettings`` <cwd>/.openspace/settings.json  (tracked in git)
# - ``localSettings``   <cwd>/.openspace/settings.local.json  (not in git)
# - ``cliArg``          --allowedTools / --disallowedTools CLI flags
# - ``command``         emitted by slash-commands during runtime
# - ``session``         live-session "always allow" decisions
PermissionRuleSource = Literal[
    "userSettings",
    "projectSettings",
    "localSettings",
    "cliArg",
    "command",
    "session",
]

PERMISSION_RULE_SOURCES: Tuple[PermissionRuleSource, ...] = (
    "userSettings",
    "projectSettings",
    "localSettings",
    "cliArg",
    "command",
    "session",
)

# OpenSpace ``PermissionUpdateDestination`` — subset of rule sources that can be
# written at runtime.
PermissionUpdateDestination = Literal[
    "userSettings",
    "projectSettings",
    "localSettings",
    "session",
    "cliArg",
]


# ════════════════════════════════════════════════════════════════════════
# §4  Permission Rules
# ════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class PermissionRuleValue:
    """OpenSpace ``PermissionRuleValue``.

    ``rule_content`` semantics are tool-dependent:
    - ``bash``:  either an exact command (``git status``) or a prefix
      pattern (``npm:*``, ``git commit:*``).
    - ``read`` / ``edit`` / ``write``:  a file glob pattern (``**/*.md``,
      ``/tmp/**``) anchored to an :class:`AdditionalWorkingDirectory`
      where applicable.
    - Other tools:  tool-specific (see per-tool ``check_permissions``).
    ``rule_content=None`` means "match the tool regardless of input".
    """

    tool_name: str
    rule_content: Optional[str] = None


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """OpenSpace ``PermissionRule``."""

    source: PermissionRuleSource
    rule_behavior: PermissionBehavior
    rule_value: PermissionRuleValue


# ════════════════════════════════════════════════════════════════════════
# §5  Additional Working Directories
# ════════════════════════════════════════════════════════════════════════

WorkingDirectorySource = PermissionRuleSource


@dataclass(frozen=True, slots=True)
class AdditionalWorkingDirectory:
    """OpenSpace ``AdditionalWorkingDirectory``."""

    path: str
    source: WorkingDirectorySource


# ════════════════════════════════════════════════════════════════════════
# §6  Permission Updates (mutations to the rule store)
# ════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class AddRulesUpdate:
    """OpenSpace ``{ type: 'addRules' }``."""

    destination: PermissionUpdateDestination
    rules: Tuple[PermissionRuleValue, ...]
    behavior: PermissionBehavior
    type: Literal["addRules"] = "addRules"


@dataclass(frozen=True, slots=True)
class ReplaceRulesUpdate:
    """OpenSpace ``{ type: 'replaceRules' }``."""

    destination: PermissionUpdateDestination
    rules: Tuple[PermissionRuleValue, ...]
    behavior: PermissionBehavior
    type: Literal["replaceRules"] = "replaceRules"


@dataclass(frozen=True, slots=True)
class RemoveRulesUpdate:
    """OpenSpace ``{ type: 'removeRules' }``."""

    destination: PermissionUpdateDestination
    rules: Tuple[PermissionRuleValue, ...]
    behavior: PermissionBehavior
    type: Literal["removeRules"] = "removeRules"


@dataclass(frozen=True, slots=True)
class SetModeUpdate:
    """OpenSpace ``{ type: 'setMode' }``."""

    destination: PermissionUpdateDestination
    mode: ExternalPermissionMode
    type: Literal["setMode"] = "setMode"


@dataclass(frozen=True, slots=True)
class AddDirectoriesUpdate:
    """OpenSpace ``{ type: 'addDirectories' }``."""

    destination: PermissionUpdateDestination
    directories: Tuple[str, ...]
    type: Literal["addDirectories"] = "addDirectories"


@dataclass(frozen=True, slots=True)
class RemoveDirectoriesUpdate:
    """OpenSpace ``{ type: 'removeDirectories' }``."""

    destination: PermissionUpdateDestination
    directories: Tuple[str, ...]
    type: Literal["removeDirectories"] = "removeDirectories"


PermissionUpdate = Union[
    AddRulesUpdate,
    ReplaceRulesUpdate,
    RemoveRulesUpdate,
    SetModeUpdate,
    AddDirectoriesUpdate,
    RemoveDirectoriesUpdate,
]


# ════════════════════════════════════════════════════════════════════════
# §7  Decision Reasons (OpenSpace PermissionDecisionReason — 11 variants)
# ════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class DecisionReasonRule:
    """OpenSpace ``{ type: 'rule', rule }``.

    A specific :class:`PermissionRule` matched the tool+input.  The engine
    includes the concrete rule so the caller can surface source + behavior
    in the UI (e.g. "Allowed by project rule Bash(npm:*)").
    """

    rule: PermissionRule
    type: Literal["rule"] = "rule"


@dataclass(frozen=True, slots=True)
class DecisionReasonMode:
    """OpenSpace ``{ type: 'mode', mode }``.

    The active permission mode forced the decision (e.g. ``acceptEdits``
    auto-allowed a write; ``plan`` deny-by-default; ``bypassPermissions``
    auto-allowed a non-sensitive operation).
    """

    mode: PermissionMode
    type: Literal["mode"] = "mode"


@dataclass(frozen=True, slots=True)
class DecisionReasonSubcommandResults:
    """OpenSpace ``{ type: 'subcommandResults', reasons: Map<string, PermissionResult> }``.

    Used by BashTool when a compound command (``git add . && npm test``)
    decomposes into subcommands, each evaluated independently; the merged
    reason carries the per-subcommand result map.
    """

    reasons: Mapping[str, "PermissionResult"]
    type: Literal["subcommandResults"] = "subcommandResults"


@dataclass(frozen=True, slots=True)
class DecisionReasonPermissionPromptTool:
    """OpenSpace ``{ type: 'permissionPromptTool', permissionPromptToolName, toolResult }``.

    An external permission-prompt MCP tool produced the decision (e.g.
    a fleet policy server).  ``tool_result`` is the raw result payload.
    """

    permission_prompt_tool_name: str
    tool_result: Any
    type: Literal["permissionPromptTool"] = "permissionPromptTool"


@dataclass(frozen=True, slots=True)
class DecisionReasonHook:
    """OpenSpace ``{ type: 'hook', hookName, hookSource?, reason? }``.

    A ``pre_tool_use`` / ``permission_check`` hook produced the decision.
    """

    hook_name: str
    hook_source: Optional[str] = None
    reason: Optional[str] = None
    type: Literal["hook"] = "hook"


@dataclass(frozen=True, slots=True)
class DecisionReasonAsyncAgent:
    """OpenSpace ``{ type: 'asyncAgent', reason }``.

    The caller is an async subagent (spawned with ``Task`` tool) running
    without TUI access; in this context ``ask`` decisions auto-deny.
    """

    reason: str
    type: Literal["asyncAgent"] = "asyncAgent"


@dataclass(frozen=True, slots=True)
class DecisionReasonSandboxOverride:
    """OpenSpace ``{ type: 'sandboxOverride', reason }``.

    A sandbox auto-approve bypass (``excludedCommand``) or user bypass
    (``dangerouslyDisableSandbox``) produced the decision.
    """

    reason: Literal["excludedCommand", "dangerouslyDisableSandbox", "sandboxed"]
    type: Literal["sandboxOverride"] = "sandboxOverride"


@dataclass(frozen=True, slots=True)
class DecisionReasonClassifier:
    """OpenSpace ``{ type: 'classifier', classifier, reason }``.

    The local engine never sets this reason, but the variant is preserved
    when loading persisted decisions or legacy-compatible configs.
    """

    classifier: str
    reason: str
    type: Literal["classifier"] = "classifier"


@dataclass(frozen=True, slots=True)
class DecisionReasonWorkingDir:
    """OpenSpace ``{ type: 'workingDir', reason }``.

    A file-system tool decision based on working-directory membership
    (e.g. ``read`` on a path inside cwd → allow; outside → ask).
    """

    reason: str
    type: Literal["workingDir"] = "workingDir"


@dataclass(frozen=True, slots=True)
class DecisionReasonSafetyCheck:
    """OpenSpace ``{ type: 'safetyCheck', reason, classifierApprovable }``.

    A hardcoded safety rail blocked/asked the operation: sensitive path
    (.git/, .openspace/, shell configs), dangerous delete (rm /),
    Windows-path bypass attempt, etc.

    ``classifier_approvable`` mirrors Implementation: when True, OpenSpace's auto-mode
    classifier was allowed to override the safety check with context
    awareness.  OS ignores the field (auto-mode skipped).
    """

    reason: str
    classifier_approvable: bool = True
    type: Literal["safetyCheck"] = "safetyCheck"


@dataclass(frozen=True, slots=True)
class DecisionReasonOther:
    """OpenSpace ``{ type: 'other', reason }``."""

    reason: str
    type: Literal["other"] = "other"


PermissionDecisionReason = Union[
    DecisionReasonRule,
    DecisionReasonMode,
    DecisionReasonSubcommandResults,
    DecisionReasonPermissionPromptTool,
    DecisionReasonHook,
    DecisionReasonAsyncAgent,
    DecisionReasonSandboxOverride,
    DecisionReasonClassifier,
    DecisionReasonWorkingDir,
    DecisionReasonSafetyCheck,
    DecisionReasonOther,
]


# ════════════════════════════════════════════════════════════════════════
# §8  Pending Classifier Check (field only, not invoked)
# ════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class PendingClassifierCheck:
    """OpenSpace ``PendingClassifierCheck``.

    OpenSpace does not populate this field today. The type is kept so
    persisted decisions round-trip cleanly and future enablement does not
    require schema changes.
    """

    command: str
    cwd: str
    descriptions: Tuple[str, ...]


# ════════════════════════════════════════════════════════════════════════
# §9  Command Metadata (attached to Ask decisions for BashTool)
# ════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class PermissionCommandMetadata:
    """OpenSpace ``PermissionCommandMetadata``.

    Minimal command shape kept as a subset of ``Command`` to avoid import
    cycles.
    """

    name: str
    description: Optional[str] = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PermissionMetadata:
    """OpenSpace ``PermissionMetadata = { command } | undefined``."""

    command: Optional[PermissionCommandMetadata] = None


# ════════════════════════════════════════════════════════════════════════
# §10  Permission Results (Allow / Ask / Deny / Passthrough)
# ════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class PermissionAllow:
    """OpenSpace ``PermissionAllowDecision``.

    - ``updated_input``: the tool will receive this input instead of the
      original one.  BashTool uses this to strip ``sudo`` wrappers;
      FileEditTool uses it to normalize paths to absolute form.
    - ``user_modified``: True when the user edited the tool input in the
      permission dialog before accepting.  Consumed by
      ``insertFinalToolUseMetadata`` to annotate the transcript.
    - ``tool_use_id``: echoes the pending tool use id (for client-side
      correlation when permissions are resolved asynchronously).
    - ``accept_feedback``: a short user-provided note captured alongside
      the accept click (e.g. "approving because CI is offline").
    - ``content_blocks``: extra content (images) attached to feedback.
    """

    behavior: Literal["allow"] = "allow"
    updated_input: Optional[Dict[str, Any]] = None
    user_modified: bool = False
    decision_reason: Optional[PermissionDecisionReason] = None
    tool_use_id: Optional[str] = None
    accept_feedback: Optional[str] = None
    content_blocks: Optional[Tuple[Any, ...]] = None


@dataclass(frozen=True, slots=True)
class PermissionAsk:
    """OpenSpace ``PermissionAskDecision``.

    - ``message``: human-readable explanation shown in the prompt.
    - ``updated_input``: pre-normalized input the user will be asked to
      approve (different from the raw tool input).
    - ``suggestions``: :class:`PermissionUpdate` candidates the dialog
      surfaces as "always allow" / "always deny" buttons.  Typically
      3 items: exact rule, prefix rule, tool-wide rule.
    - ``blocked_path``: filesystem path that triggered a path-based ask
      (shown to the user; used by the "add directory" suggestion).
    - ``metadata``: tool-specific metadata (currently only
      ``command`` for BashTool).
    - ``is_bash_security_check_for_misparsing``: compatibility flag used
      to preserve older persisted ask decisions.
    - ``pending_classifier_check``: reserved classifier metadata.
    - ``content_blocks``: extra content when the user attaches images
      to the rejection message.
    """

    message: str
    behavior: Literal["ask"] = "ask"
    updated_input: Optional[Dict[str, Any]] = None
    decision_reason: Optional[PermissionDecisionReason] = None
    suggestions: Optional[Tuple[PermissionUpdate, ...]] = None
    blocked_path: Optional[str] = None
    metadata: Optional[PermissionMetadata] = None
    is_bash_security_check_for_misparsing: bool = False
    pending_classifier_check: Optional[PendingClassifierCheck] = None
    content_blocks: Optional[Tuple[Any, ...]] = None


@dataclass(frozen=True, slots=True)
class PermissionDeny:
    """OpenSpace ``PermissionDenyDecision``.

    Deny is always terminal (no ``updated_input``); the caller must
    surface the ``message`` to the model as a tool error.
    """

    message: str
    decision_reason: PermissionDecisionReason
    behavior: Literal["deny"] = "deny"
    tool_use_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class PermissionPassthrough:
    """OpenSpace ``PermissionResult`` ``passthrough`` variant.

    Used by intermediate layers to signal "I abstain; upstream should
    continue evaluating".  Example: a subcommand check that neither
    allows nor asks — the containing bash permission check then decides
    based on sibling subcommand results.
    """

    message: str
    behavior: Literal["passthrough"] = "passthrough"
    decision_reason: Optional[PermissionDecisionReason] = None
    suggestions: Optional[Tuple[PermissionUpdate, ...]] = None
    blocked_path: Optional[str] = None
    pending_classifier_check: Optional[PendingClassifierCheck] = None


# OpenSpace ``PermissionDecision = Allow | Ask | Deny``
PermissionDecision = Union[PermissionAllow, PermissionAsk, PermissionDeny]

# OpenSpace ``PermissionResult = PermissionDecision | Passthrough``
PermissionResult = Union[
    PermissionAllow,
    PermissionAsk,
    PermissionDeny,
    PermissionPassthrough,
]


# ════════════════════════════════════════════════════════════════════════
# §11  Permission result aliases
# ════════════════════════════════════════════════════════════════════════

# Kept as an alias of the :data:`PermissionResult` union for existing call sites
# that still use the shorter name in type annotations.
PermissionCheckResult = PermissionResult

# Sentinel allow result with no updated_input and no reason.
PERMISSION_ALLOW: PermissionAllow = PermissionAllow()


# ════════════════════════════════════════════════════════════════════════
# §12  Bash Classifier Metadata Types
# ════════════════════════════════════════════════════════════════════════


ClassifierConfidence = Literal["high", "medium", "low"]
ClassifierBehavior = Literal["deny", "ask", "allow"]


@dataclass(frozen=True, slots=True)
class ClassifierResult:
    """OpenSpace ``ClassifierResult``."""

    matches: bool
    confidence: ClassifierConfidence
    reason: str
    matched_description: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ClassifierUsage:
    """OpenSpace ``ClassifierUsage``."""

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int


@dataclass(frozen=True, slots=True)
class YoloClassifierResult:
    """OpenSpace ``YoloClassifierResult`` — schema only; OS never populates."""

    should_block: bool
    reason: str
    model: str
    thinking: Optional[str] = None
    unavailable: bool = False
    transcript_too_long: bool = False
    usage: Optional[ClassifierUsage] = None
    duration_ms: Optional[int] = None
    prompt_lengths: Optional[Dict[str, int]] = None
    error_dump_path: Optional[str] = None
    stage: Optional[Literal["fast", "thinking"]] = None
    stage1_usage: Optional[ClassifierUsage] = None
    stage1_duration_ms: Optional[int] = None
    stage1_request_id: Optional[str] = None
    stage1_msg_id: Optional[str] = None
    stage2_usage: Optional[ClassifierUsage] = None
    stage2_duration_ms: Optional[int] = None
    stage2_request_id: Optional[str] = None
    stage2_msg_id: Optional[str] = None


# ════════════════════════════════════════════════════════════════════════
# §13  Permission Explainer types
# ════════════════════════════════════════════════════════════════════════

RiskLevel = Literal["LOW", "MEDIUM", "HIGH"]


@dataclass(frozen=True, slots=True)
class PermissionExplanation:
    """OpenSpace ``PermissionExplanation``."""

    risk_level: RiskLevel
    explanation: str
    reasoning: str
    risk: str


# ════════════════════════════════════════════════════════════════════════
# §14  Tool Permission Context
# ════════════════════════════════════════════════════════════════════════

# OpenSpace ``ToolPermissionRulesBySource = { [source]: string[] }``.
# A mapping from rule source to the list of ``rule_content`` strings
# (formatted as ``toolName(ruleContent)`` when ruleContent is present).
ToolPermissionRulesBySource = Dict[PermissionRuleSource, Tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class ToolPermissionContext:
    """OpenSpace ``ToolPermissionContext``.

    The runtime permission bundle passed to every tool's
    ``check_permissions`` method.  Immutable within a turn; mutations
    (e.g. session ``always_allow``) materialise as a new context
    installed on the next turn.

    Fields:
    - ``mode``: the effective :data:`PermissionMode`.
    - ``additional_working_directories``: mapping from absolute path →
      :class:`AdditionalWorkingDirectory`.  Includes cwd implicitly.
    - ``always_allow_rules`` / ``always_deny_rules`` / ``always_ask_rules``:
      flat rule lists per source.  Stored as strings in OpenSpace's
      ``toolName(ruleContent)`` format for direct comparison with
      freshly-parsed rules.
    - ``is_bypass_permissions_mode_available``: user must have opted-in
      in settings.json to allow ``bypassPermissions`` mode.
    - ``stripped_dangerous_rules``: rules that were automatically
      stripped from the effective set because they would bypass a
      sensitive-path safety check.  Surfaced to the UI so the user
      knows why an "allow" rule didn't take effect.
    - ``should_avoid_permission_prompts``: when True (headless / CI),
      ``ask`` decisions auto-deny.
    - ``await_automated_checks_before_dialog``: reserved for async classifier
      integrations; currently false.
    - ``pre_plan_mode``: the mode to revert to when exiting plan mode.
    """

    mode: PermissionMode
    additional_working_directories: Mapping[str, AdditionalWorkingDirectory]
    always_allow_rules: ToolPermissionRulesBySource
    always_deny_rules: ToolPermissionRulesBySource
    always_ask_rules: ToolPermissionRulesBySource
    is_bypass_permissions_mode_available: bool = False
    stripped_dangerous_rules: Optional[ToolPermissionRulesBySource] = None
    should_avoid_permission_prompts: bool = False
    await_automated_checks_before_dialog: bool = False
    pre_plan_mode: Optional[PermissionMode] = None

    # --- convenience constructors --------------------------------------

    @classmethod
    def default(cls, cwd: str, mode: PermissionMode = "default") -> "ToolPermissionContext":
        """Construct a minimal context with only the cwd as a working dir."""
        empty_rules: ToolPermissionRulesBySource = {}
        return cls(
            mode=mode,
            additional_working_directories={
                cwd: AdditionalWorkingDirectory(path=cwd, source="session")
            },
            always_allow_rules=empty_rules,
            always_deny_rules=empty_rules,
            always_ask_rules=empty_rules,
        )

    def with_mode(self, mode: PermissionMode) -> "ToolPermissionContext":
        """Return a shallow copy with ``mode`` replaced."""
        return ToolPermissionContext(
            mode=mode,
            additional_working_directories=self.additional_working_directories,
            always_allow_rules=self.always_allow_rules,
            always_deny_rules=self.always_deny_rules,
            always_ask_rules=self.always_ask_rules,
            is_bypass_permissions_mode_available=self.is_bypass_permissions_mode_available,
            stripped_dangerous_rules=self.stripped_dangerous_rules,
            should_avoid_permission_prompts=self.should_avoid_permission_prompts,
            await_automated_checks_before_dialog=self.await_automated_checks_before_dialog,
            pre_plan_mode=self.pre_plan_mode,
        )


# ════════════════════════════════════════════════════════════════════════
# §15  Helpers — tool-name / rule-value formatting & matching
# ════════════════════════════════════════════════════════════════════════

# Canonical lowercase tool names.  Aliases accept PascalCase rule names from
# older configuration files.
_TOOL_RULE_ALIASES: Dict[str, str] = {
    "Bash": "bash",
    "Read": "read",
    "Edit": "edit",
    "Write": "write",
    "Grep": "grep",
    "Glob": "glob",
    "LS": "ls",
    "WebSearch": "web_search",
    "WebFetch": "web_fetch",
    "AskUserQuestion": "ask_user_question",
    # Internal tool aliases kept for rule round-tripping.
    "Task": "task",
    "TodoWrite": "todo_write",
    "NotebookEdit": "notebook_edit",
}


def normalize_tool_name_for_rule(tool_name: str) -> str:
    """Map PascalCase tool aliases to OpenSpace lowercase naming.

    Used when loading rules from settings.json that were authored against
    PascalCase naming (``Bash(npm:*)``) or migrated from an older OpenSpace install.
    Unknown names are returned unchanged (e.g. custom MCP tool names).
    """
    return _TOOL_RULE_ALIASES.get(tool_name, tool_name)


def parse_rule_value(raw: str) -> PermissionRuleValue:
    """Parse a ``"ToolName(ruleContent)"`` or ``"ToolName"`` rule string.

    Rule examples:
        ``Bash(npm:*)``               → ("bash", "npm:*")
        ``Bash(git commit:*)``        → ("bash", "git commit:*")
        ``Read(/tmp/**)``             → ("read", "/tmp/**")
        ``Bash``                       → ("bash", None)
        ``mcp__filesystem__read_file`` → ("mcp__filesystem__read_file", None)

    Malformed inputs (unbalanced parens, empty tool name) raise
    :class:`ValueError` — caller catches and drops the rule.
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("empty rule value")

    if "(" not in raw:
        tool_name = normalize_tool_name_for_rule(raw)
        return PermissionRuleValue(tool_name=tool_name, rule_content=None)

    if not raw.endswith(")"):
        raise ValueError(f"unbalanced parens in rule value: {raw!r}")

    open_idx = raw.index("(")
    if open_idx == 0:
        raise ValueError(f"empty tool name in rule value: {raw!r}")

    tool_name = normalize_tool_name_for_rule(raw[:open_idx])
    rule_content = raw[open_idx + 1 : -1]
    # Empty ruleContent is degenerate — treat as a wildcard tool-only rule.
    if rule_content == "":
        return PermissionRuleValue(tool_name=tool_name, rule_content=None)
    return PermissionRuleValue(tool_name=tool_name, rule_content=rule_content)


def format_rule_value(value: PermissionRuleValue) -> str:
    """Inverse of :func:`parse_rule_value`.

    ``(tool="bash", content="npm:*")`` → ``"bash(npm:*)"``
    ``(tool="bash", content=None)``    → ``"bash"``
    """
    if value.rule_content is None:
        return value.tool_name
    return f"{value.tool_name}({value.rule_content})"


def rule_matches_tool(rule: PermissionRuleValue, tool_name: str) -> bool:
    """True iff ``rule`` applies to a tool call with name ``tool_name``.

    Tool-name match alone — ``rule_content`` matching is tool-specific
    and implemented inside each ``check_permissions`` override
    (filesystem uses glob; bash uses exact/prefix; others exact).
    """
    return rule.tool_name == tool_name


def get_rule_behavior_description(behavior: PermissionBehavior) -> str:
    """OpenSpace ``getRuleBehaviorDescription`` (PermissionResult.ts).

    Used by the TUI dialog and the transcript annotator.
    """
    return {
        "allow": "allow",
        "deny": "deny",
        "ask": "ask",
    }[behavior]
