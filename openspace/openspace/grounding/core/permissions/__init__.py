"""Permission subsystem for OpenSpace tool execution.

This package owns permission rule loading, rule-based decisions, filesystem
guards, and shell command permission checks.

Tool name convention: rules use lowercase tool names
(``bash``, ``read``, ``edit``, ``write``, ``grep``, ``glob``, ``ls``,
``web_search``, ``web_fetch``).  Older rules written with PascalCase
(``Bash``/``Read``/...) are normalized on load.
"""
from __future__ import annotations

from .engine import (
    has_permissions_to_use_tool,
    deny_missing_permission_context,
    check_rule_based_permissions,
    create_permission_request_message,
    get_allow_rules,
    get_deny_rules,
    get_ask_rules,
    get_deny_rule_for_tool,
    get_ask_rule_for_tool,
    tool_always_allowed_rule,
    apply_permission_rules_to_permission_context,
)
from .loader import (
    load_tool_permission_context,
    load_all_permission_rules_from_disk,
    apply_permission_update,
    persist_permission_updates,
    add_permission_rules_to_settings,
    delete_permission_rule_from_settings,
    get_session_store,
    get_cliarg_store,
)
from .snapshot import (
    build_permission_rules_snapshot,
    get_permission_mode,
    load_permission_context,
    set_session_permission_mode,
)
from .filesystem import (
    check_read_permission_for_tool,
    check_write_permission_for_tool,
    check_path_safety_for_auto_edit,
    is_openspace_settings_path,
    has_suspicious_windows_path_pattern,
    all_working_directories,
    path_in_working_path,
    path_in_allowed_working_path,
    register_internal_path_predicate,
    DANGEROUS_FILES,
    DANGEROUS_DIRECTORIES,
)
from .bash_permissions import bash_tool_has_permission
from .types import (
    # --- Enums / Literals ---
    PERMISSION_MODES,
    EXTERNAL_PERMISSION_MODES,
    INTERNAL_PERMISSION_MODES,
    PermissionMode,
    ExternalPermissionMode,
    InternalPermissionMode,
    PermissionBehavior,
    PermissionRuleSource,
    PermissionUpdateDestination,
    # --- Rules ---
    PermissionRuleValue,
    PermissionRule,
    PermissionUpdate,
    AdditionalWorkingDirectory,
    # --- Decision / Result ---
    PermissionCommandMetadata,
    PermissionAllow,
    PermissionAsk,
    PermissionDeny,
    PermissionPassthrough,
    PermissionDecision,
    PermissionResult,
    PermissionDecisionReason,
    PendingClassifierCheck,
    # --- Context ---
    ToolPermissionContext,
    ToolPermissionRulesBySource,
    # --- Classifier metadata types ---
    ClassifierResult,
    ClassifierBehavior,
    ClassifierUsage,
    YoloClassifierResult,
    RiskLevel,
    PermissionExplanation,
    # --- Helpers ---
    normalize_tool_name_for_rule,
    parse_rule_value,
    format_rule_value,
    rule_matches_tool,
    get_rule_behavior_description,
    # --- Backward-compat alias ---
    PermissionCheckResult,
    PERMISSION_ALLOW,
)

__all__ = [
    "PERMISSION_MODES",
    "EXTERNAL_PERMISSION_MODES",
    "INTERNAL_PERMISSION_MODES",
    "PermissionMode",
    "ExternalPermissionMode",
    "InternalPermissionMode",
    "PermissionBehavior",
    "PermissionRuleSource",
    "PermissionUpdateDestination",
    "PermissionRuleValue",
    "PermissionRule",
    "PermissionUpdate",
    "AdditionalWorkingDirectory",
    "PermissionCommandMetadata",
    "PermissionAllow",
    "PermissionAsk",
    "PermissionDeny",
    "PermissionPassthrough",
    "PermissionDecision",
    "PermissionResult",
    "PermissionDecisionReason",
    "PendingClassifierCheck",
    "ToolPermissionContext",
    "ToolPermissionRulesBySource",
    "ClassifierResult",
    "ClassifierBehavior",
    "ClassifierUsage",
    "YoloClassifierResult",
    "RiskLevel",
    "PermissionExplanation",
    "normalize_tool_name_for_rule",
    "parse_rule_value",
    "format_rule_value",
    "rule_matches_tool",
    "get_rule_behavior_description",
    "PermissionCheckResult",
    "PERMISSION_ALLOW",
    # --- Engine ---
    "has_permissions_to_use_tool",
    "deny_missing_permission_context",
    "check_rule_based_permissions",
    "create_permission_request_message",
    "get_allow_rules",
    "get_deny_rules",
    "get_ask_rules",
    "get_deny_rule_for_tool",
    "get_ask_rule_for_tool",
    "tool_always_allowed_rule",
    "apply_permission_rules_to_permission_context",
    # --- Loader ---
    "load_tool_permission_context",
    "load_all_permission_rules_from_disk",
    "apply_permission_update",
    "persist_permission_updates",
    "add_permission_rules_to_settings",
    "delete_permission_rule_from_settings",
    "get_session_store",
    "get_cliarg_store",
    "build_permission_rules_snapshot",
    "get_permission_mode",
    "load_permission_context",
    "set_session_permission_mode",
    # --- Filesystem ---
    "check_read_permission_for_tool",
    "check_write_permission_for_tool",
    "check_path_safety_for_auto_edit",
    "is_openspace_settings_path",
    "has_suspicious_windows_path_pattern",
    "all_working_directories",
    "path_in_working_path",
    "path_in_allowed_working_path",
    "register_internal_path_predicate",
    "DANGEROUS_FILES",
    "DANGEROUS_DIRECTORIES",
    # --- Bash ---
    "bash_tool_has_permission",
]
