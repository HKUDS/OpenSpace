"""UI helpers for sandbox status, diagnostics, and violation text."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .types import SandboxDependencyIssue, SandboxViolation


_SANDBOX_VIOLATION_RE = re.compile(
    r"<sandbox_violations>[\s\S]*?</sandbox_violations>",
    re.MULTILINE,
)


def remove_sandbox_violation_tags(text: str) -> str:
    """OpenSpace ``removeSandboxViolationTags`` exact role in Python."""

    return _SANDBOX_VIOLATION_RE.sub("", text)


def build_sandbox_status(manager: Any) -> dict[str, Any]:
    """Return a JSON-serializable sandbox status snapshot for UI surfaces."""

    manager.refresh_config()
    runtime = manager.runtime_config()
    settings = runtime.settings
    policy = runtime.policy
    dependency_issues = manager.check_dependencies()
    dependency_errors = [
        issue.to_json() for issue in dependency_issues if issue.severity == "error"
    ]
    dependency_warnings = [
        issue.to_json() for issue in dependency_issues if issue.severity == "warning"
    ]
    linux_glob_warnings = list(manager.get_linux_glob_pattern_warnings())
    recent_violations = [
        _violation_to_json(violation)
        for violation in manager.get_violation_store().recent(limit=10)
    ]
    enabled_platforms = (
        list(settings.enabled_platforms)
        if settings.enabled_platforms is not None
        else None
    )
    sandboxing_enabled = manager.is_sandboxing_enabled()
    enabled_in_settings = manager.is_enabled_in_settings()

    payload: dict[str, Any] = {
        "status": "warn",
        "mode": _sandbox_mode(
            enabled_in_settings=enabled_in_settings,
            sandboxing_enabled=sandboxing_enabled,
            auto_allow=settings.auto_allow_bash_if_sandboxed,
        ),
        "platform": manager.platform,
        "supported_platform": manager.is_supported_platform(),
        "platform_enabled": manager.is_platform_in_enabled_list(),
        "enabled_in_settings": enabled_in_settings,
        "sandboxing_enabled": sandboxing_enabled,
        "fail_if_unavailable": settings.fail_if_unavailable,
        "auto_allow_bash_if_sandboxed": settings.auto_allow_bash_if_sandboxed,
        "allow_unsandboxed_commands": settings.allow_unsandboxed_commands,
        "enabled_platforms": enabled_platforms,
        "settings_locked": manager.are_settings_locked_by_policy(),
        "unavailable_reason": manager.get_unavailable_reason(),
        "dependency_errors": dependency_errors,
        "dependency_warnings": dependency_warnings,
        "linux_glob_warnings": linux_glob_warnings,
        "excluded_commands": list(settings.excluded_commands),
        "excluded_commands_count": len(settings.excluded_commands),
        "filesystem": {
            "denyRead": list(policy.deny_read),
            "allowRead": list(policy.allow_read),
            "allowWrite": list(policy.allow_write),
            "denyWrite": list(policy.deny_write),
        },
        "network": {
            "allowedDomains": list(policy.allowed_domains),
            "deniedDomains": list(policy.denied_domains),
            "allowNetwork": policy.allow_network,
            "allowUnixSockets": list(policy.allow_unix_sockets),
            "allowAllUnixSockets": policy.allow_all_unix_sockets,
            "allowLocalBinding": policy.allow_local_binding,
        },
        "ignore_violations": {
            key: list(values) for key, values in policy.ignore_violations.items()
        },
        "weaker_nested_sandbox": policy.enable_weaker_nested_sandbox,
        "weaker_network_isolation": policy.enable_weaker_network_isolation,
        "violation_count": len(manager.get_violation_store().recent()),
        "recent_violations": recent_violations,
        "recent_violation_count": len(recent_violations),
    }
    payload["status"] = sandbox_doctor_status(payload)
    return payload


def sandbox_doctor_status(payload: Mapping[str, Any]) -> str:
    """Classify a sandbox status snapshot as pass, warn, or fail."""

    enabled = bool(payload.get("enabled_in_settings"))
    active = bool(payload.get("sandboxing_enabled"))
    required = bool(payload.get("fail_if_unavailable"))
    has_errors = bool(payload.get("dependency_errors"))
    has_warnings = bool(payload.get("dependency_warnings")) or bool(
        payload.get("linux_glob_warnings")
    )
    platform_unavailable = not bool(payload.get("supported_platform")) or not bool(
        payload.get("platform_enabled")
    )

    if enabled and required and (platform_unavailable or has_errors or not active):
        return "fail"
    if not enabled:
        return "warn"
    if platform_unavailable or has_errors or not active:
        return "warn"
    if has_warnings:
        return "warn"
    return "pass"


def format_sandbox_status(payload: Mapping[str, Any]) -> str:
    """Render the OpenSpace Sandbox Settings/Config tabs as command text."""

    lines = [
        f"Sandbox status: {payload.get('status', 'warn')}",
        f"Mode: {_display_mode(payload)}",
        f"Enabled in settings: {_yes_no(payload.get('enabled_in_settings'))}",
        f"Sandbox active: {_yes_no(payload.get('sandboxing_enabled'))}",
        (
            "Platform: "
            f"{payload.get('platform', 'unknown')} "
            f"({_platform_state(payload)})"
        ),
        (
            "Policy: "
            f"failIfUnavailable={_bool_text(payload.get('fail_if_unavailable'))}, "
            "autoAllowBashIfSandboxed="
            f"{_bool_text(payload.get('auto_allow_bash_if_sandboxed'))}, "
            "allowUnsandboxedCommands="
            f"{_bool_text(payload.get('allow_unsandboxed_commands'))}"
        ),
        f"enabledPlatforms: {_format_enabled_platforms(payload.get('enabled_platforms'))}",
        f"Excluded commands: {int(payload.get('excluded_commands_count') or 0)}",
    ]

    reason = payload.get("unavailable_reason")
    if isinstance(reason, str) and reason:
        lines.append(f"Unavailable reason: {reason}")

    dependency_errors = _issue_lines(payload.get("dependency_errors"))
    dependency_warnings = _issue_lines(payload.get("dependency_warnings"))
    if dependency_errors or dependency_warnings:
        lines.append("")
        lines.append("Dependencies:")
        if dependency_errors:
            lines.extend(f"- error: {line}" for line in dependency_errors)
        if dependency_warnings:
            lines.extend(f"- warning: {line}" for line in dependency_warnings)

    linux_glob_warnings = _string_list(payload.get("linux_glob_warnings"))
    if linux_glob_warnings:
        lines.append("")
        lines.append("Linux glob warnings:")
        lines.extend(f"- {warning}" for warning in linux_glob_warnings)

    excluded_commands = _string_list(payload.get("excluded_commands"))
    if excluded_commands:
        lines.append("")
        lines.append("Excluded commands:")
        lines.extend(f"- {pattern}" for pattern in excluded_commands)

    filesystem = _mapping(payload.get("filesystem"))
    if filesystem:
        lines.append("")
        lines.append("Filesystem:")
        lines.append(f"- denyRead: {_count_and_sample(filesystem.get('denyRead'))}")
        lines.append(f"- allowRead: {_count_and_sample(filesystem.get('allowRead'))}")
        lines.append(f"- allowWrite: {_count_and_sample(filesystem.get('allowWrite'))}")
        lines.append(f"- denyWrite: {_count_and_sample(filesystem.get('denyWrite'))}")

    network = _mapping(payload.get("network"))
    if network:
        lines.append("")
        lines.append("Network:")
        lines.append(f"- allowNetwork: {_bool_text(network.get('allowNetwork'))}")
        lines.append(
            f"- allowedDomains: {_count_and_sample(network.get('allowedDomains'))}"
        )
        lines.append(
            f"- deniedDomains: {_count_and_sample(network.get('deniedDomains'))}"
        )
        lines.append(
            f"- allowUnixSockets: {_count_and_sample(network.get('allowUnixSockets'))}"
        )
        lines.append(
            f"- allowAllUnixSockets: {_bool_text(network.get('allowAllUnixSockets'))}"
        )
        lines.append(
            f"- allowLocalBinding: {_bool_text(network.get('allowLocalBinding'))}"
        )

    recent_violation_count = int(payload.get("recent_violation_count") or 0)
    if recent_violation_count:
        lines.append("")
        lines.append(format_sandbox_violations(payload.get("recent_violations")))

    lines.append("")
    lines.append(
        "Usage: /sandbox [status|doctor|enable [auto-allow|regular]|disable|"
        "toggle|exclude <pattern>|unexclude <pattern>|auto-allow <on|off>|"
        "allow-unsandboxed <on|off>]"
    )
    return "\n".join(line for line in lines if line is not None)


def format_sandbox_doctor(payload: Mapping[str, Any]) -> str:
    """Render dependency and policy diagnostics for `/sandbox doctor`."""

    lines = [
        f"Sandbox doctor: {payload.get('status', 'warn')}",
        f"Platform: {payload.get('platform', 'unknown')} ({_platform_state(payload)})",
        f"Enabled in settings: {_yes_no(payload.get('enabled_in_settings'))}",
        f"Sandbox active: {_yes_no(payload.get('sandboxing_enabled'))}",
        f"failIfUnavailable: {_bool_text(payload.get('fail_if_unavailable'))}",
        f"enabledPlatforms: {_format_enabled_platforms(payload.get('enabled_platforms'))}",
        f"autoAllowBashIfSandboxed: {_bool_text(payload.get('auto_allow_bash_if_sandboxed'))}",
        f"allowUnsandboxedCommands: {_bool_text(payload.get('allow_unsandboxed_commands'))}",
        f"Excluded command patterns: {int(payload.get('excluded_commands_count') or 0)}",
    ]

    reason = payload.get("unavailable_reason")
    if isinstance(reason, str) and reason:
        lines.append(f"Unavailable reason: {reason}")

    lines.append("")
    lines.append("Dependency errors:")
    errors = _issue_lines(payload.get("dependency_errors"))
    lines.extend(f"- {line}" for line in errors) if errors else lines.append("- none")

    lines.append("")
    lines.append("Dependency warnings:")
    warnings = _issue_lines(payload.get("dependency_warnings"))
    lines.extend(f"- {line}" for line in warnings) if warnings else lines.append("- none")

    linux_globs = _string_list(payload.get("linux_glob_warnings"))
    lines.append("")
    lines.append("Linux glob warnings:")
    lines.extend(f"- {item}" for item in linux_globs) if linux_globs else lines.append("- none")

    if payload.get("platform") == "windows":
        lines.append("")
        lines.append("Windows: process sandboxing is unsupported; no local OS sandbox is active.")

    recent_violation_count = int(payload.get("recent_violation_count") or 0)
    if recent_violation_count:
        lines.append("")
        lines.append(format_sandbox_violations(payload.get("recent_violations")))

    return "\n".join(lines)


def format_sandbox_violations(violations: Any) -> str:
    items = [item for item in _sequence(violations) if isinstance(item, Mapping)]
    if not items:
        return "Recent sandbox violations: none"
    lines = [f"Recent sandbox violations: {len(items)}"]
    for violation in items[-10:]:
        timestamp = _format_timestamp(violation.get("timestamp_ms"))
        command = str(violation.get("command") or "<unknown command>")
        operation = str(violation.get("operation") or "unknown")
        target = violation.get("path") or violation.get("domain") or ""
        message = str(violation.get("raw_message") or "Sandbox blocked this operation.")
        detail = f"{timestamp} {operation}"
        if target:
            detail += f" {target}"
        lines.append(f"- {detail}: {command} - {message}")
    return "\n".join(lines)


def _sandbox_mode(
    *,
    enabled_in_settings: bool,
    sandboxing_enabled: bool,
    auto_allow: bool,
) -> str:
    if not enabled_in_settings:
        return "disabled"
    if not sandboxing_enabled:
        return "unavailable"
    return "auto-allow" if auto_allow else "regular"


def _display_mode(payload: Mapping[str, Any]) -> str:
    mode = str(payload.get("mode") or "unknown")
    if mode == "auto-allow":
        return "sandboxed Bash with auto-allow"
    if mode == "regular":
        return "sandboxed Bash with regular permissions"
    return mode


def _platform_state(payload: Mapping[str, Any]) -> str:
    if not bool(payload.get("supported_platform")):
        return "unsupported"
    if not bool(payload.get("platform_enabled")):
        return "disabled by enabledPlatforms"
    return "supported"


def _format_enabled_platforms(value: Any) -> str:
    items = _string_list(value)
    return ", ".join(items) if items else "all supported platforms"


def _issue_lines(value: Any) -> list[str]:
    lines: list[str] = []
    for issue in _sequence(value):
        if isinstance(issue, SandboxDependencyIssue):
            text = issue.message
            if issue.remediation:
                text += f" ({issue.remediation})"
            lines.append(text)
            continue
        if not isinstance(issue, Mapping):
            continue
        message = str(issue.get("message") or issue.get("code") or "").strip()
        remediation = str(issue.get("remediation") or "").strip()
        if not message:
            continue
        if remediation:
            message += f" ({remediation})"
        lines.append(message)
    return lines


def _violation_to_json(violation: SandboxViolation | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(violation, SandboxViolation):
        return violation.to_json()
    return dict(violation)


def _format_timestamp(value: Any) -> str:
    try:
        seconds = float(value) / 1000
    except (TypeError, ValueError):
        return "unknown-time"
    return datetime.fromtimestamp(seconds).strftime("%H:%M:%S")


def _count_and_sample(value: Any, *, limit: int = 3) -> str:
    items = _string_list(value)
    if not items:
        return "0"
    sample = ", ".join(items[:limit])
    if len(items) > limit:
        sample += f", +{len(items) - limit} more"
    return f"{len(items)} ({sample})"


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in _sequence(value) if isinstance(item, (str, int, float))]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return []


def _yes_no(value: Any) -> str:
    return "yes" if bool(value) else "no"


def _bool_text(value: Any) -> str:
    return "true" if bool(value) else "false"


__all__ = [
    "build_sandbox_status",
    "format_sandbox_doctor",
    "format_sandbox_status",
    "format_sandbox_violations",
    "remove_sandbox_violation_tags",
    "sandbox_doctor_status",
]
