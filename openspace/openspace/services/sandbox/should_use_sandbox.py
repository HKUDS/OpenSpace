"""Sandbox decision helpers for BashTool.

This module decides whether a shell command should run inside the process
sandbox.  It checks sandbox settings, unsupported platforms, explicit bypasses,
empty commands, and ``sandbox.excludedCommands`` with compound-command and
wrapper/env-prefix normalization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from openspace.grounding.core.permissions.bash_permissions import (
    BINARY_HIJACK_VARS,
    ExactRule,
    PrefixRule,
    WildcardRule,
    match_wildcard_pattern,
    parse_permission_rule,
    strip_all_leading_env_vars,
    strip_safe_wrappers,
)
from openspace.grounding.core.security.shell_parser import split_command_segments

from .manager import ProcessSandboxManager, get_process_sandbox_manager


SandboxDecisionReason = Literal[
    "enabled",
    "settings_disabled",
    "unsupported_platform",
    "platform_disabled",
    "dependency_error",
    "dangerously_disable_sandbox",
    "excluded_command",
    "empty_command",
    "remote_connector_unavailable",
]


@dataclass(slots=True)
class ShouldUseSandboxInput:
    command: str | None
    dangerously_disable_sandbox: bool = False
    cwd: str | None = None
    connector_kind: str = "local"


@dataclass(slots=True)
class SandboxDecision:
    should_sandbox: bool
    reason: SandboxDecisionReason
    bypassed: bool = False
    unavailable_reason: str | None = None


def should_use_sandbox(
    input: ShouldUseSandboxInput,
    *,
    sandbox_manager: ProcessSandboxManager | None = None,
) -> SandboxDecision:
    """Return the sandbox decision plus OpenSpace diagnostics."""

    manager = sandbox_manager or get_process_sandbox_manager(cwd=input.cwd)
    if not manager.is_enabled_in_settings():
        return SandboxDecision(False, "settings_disabled")

    if not manager.is_supported_platform():
        return SandboxDecision(
            False,
            "unsupported_platform",
            unavailable_reason=manager.get_unavailable_reason(),
        )

    if not manager.is_platform_in_enabled_list():
        return SandboxDecision(
            False,
            "platform_disabled",
            unavailable_reason=manager.get_unavailable_reason(),
        )

    if not manager.is_sandboxing_enabled():
        return SandboxDecision(
            False,
            "dependency_error",
            unavailable_reason=manager.get_unavailable_reason(),
        )

    if input.connector_kind != "local":
        return SandboxDecision(
            False,
            "remote_connector_unavailable",
            unavailable_reason=(
                "Process sandboxing is only available for the local shell connector."
            ),
        )

    if input.dangerously_disable_sandbox and manager.are_unsandboxed_commands_allowed():
        return SandboxDecision(
            False,
            "dangerously_disable_sandbox",
            bypassed=True,
        )

    if not input.command:
        return SandboxDecision(False, "empty_command")

    if contains_excluded_command(input.command, sandbox_manager=manager):
        return SandboxDecision(False, "excluded_command", bypassed=True)

    return SandboxDecision(True, "enabled")


def contains_excluded_command(
    command: str,
    *,
    sandbox_manager: ProcessSandboxManager | None = None,
) -> bool:
    """Return whether command matches OpenSpace ``sandbox.excludedCommands``."""

    manager = sandbox_manager or get_process_sandbox_manager()
    patterns = manager.get_excluded_commands()
    if not patterns:
        return False

    try:
        subcommands = split_command_segments(command)
    except Exception:
        subcommands = [command]
    if not subcommands:
        subcommands = [command]

    for subcommand in subcommands:
        candidates = _excluded_command_candidates(subcommand.strip())
        for pattern in patterns:
            rule = parse_permission_rule(pattern)
            for candidate in candidates:
                if isinstance(rule, PrefixRule):
                    if candidate == rule.prefix or candidate.startswith(rule.prefix + " "):
                        return True
                elif isinstance(rule, ExactRule):
                    if candidate == rule.command:
                        return True
                elif isinstance(rule, WildcardRule):
                    if match_wildcard_pattern(rule.pattern, candidate):
                        return True
    return False


def _excluded_command_candidates(command: str) -> list[str]:
    candidates = [command]
    seen = {command}
    start_idx = 0
    while start_idx < len(candidates):
        end_idx = len(candidates)
        for index in range(start_idx, end_idx):
            candidate = candidates[index]
            env_stripped = strip_all_leading_env_vars(candidate, BINARY_HIJACK_VARS)
            if env_stripped not in seen:
                candidates.append(env_stripped)
                seen.add(env_stripped)
            wrapper_stripped = strip_safe_wrappers(candidate)
            if wrapper_stripped not in seen:
                candidates.append(wrapper_stripped)
                seen.add(wrapper_stripped)
        start_idx = end_idx
    return candidates


__all__ = [
    "SandboxDecision",
    "SandboxDecisionReason",
    "ShouldUseSandboxInput",
    "contains_excluded_command",
    "should_use_sandbox",
]
