"""Small runtime helpers for permission mode and rule inspection."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .loader import load_tool_permission_context, persist_permission_updates
from .types import (
    EXTERNAL_PERMISSION_MODES,
    PermissionMode,
    SetModeUpdate,
    ToolPermissionContext,
)


def _cwd(cwd: str | None = None) -> str:
    return str(Path(cwd).expanduser() if cwd else Path.cwd())


def set_session_permission_mode(mode: str, cwd: str | None = None) -> None:
    """Set the live session permission mode through the new runtime store."""

    if mode not in EXTERNAL_PERMISSION_MODES:
        raise ValueError(f"Unsupported permission mode: {mode!r}")
    persist_permission_updates(
        (
            SetModeUpdate(
                destination="session",
                mode=mode,  # type: ignore[arg-type]
            ),
        ),
        _cwd(cwd),
    )


def load_permission_context(
    cwd: str | None = None,
    mode: PermissionMode | None = None,
) -> ToolPermissionContext:
    """Load the effective permission context for a workspace."""

    return load_tool_permission_context(_cwd(cwd), mode)


def get_permission_mode(
    cwd: str | None = None,
    mode: PermissionMode | None = None,
) -> str:
    """Return the effective permission mode for a workspace."""

    return str(load_permission_context(cwd, mode).mode)


def build_permission_rules_snapshot(
    cwd: str | None = None,
    mode: PermissionMode | None = None,
) -> dict[str, Any]:
    """Return a display-oriented snapshot of effective permission rules."""

    context = load_permission_context(cwd, mode)
    persistent_sources = {"userSettings", "projectSettings", "localSettings"}
    session_sources = {"session", "cliArg", "command"}
    result: dict[str, Any] = {
        "mode": {"current": context.mode},
        "session": {},
        "persistent": {},
        "allow": {},
        "deny": {},
        "ask": {},
        "rules_by_source": {},
        "additional_working_directories": {
            path: value.source
            for path, value in dict(context.additional_working_directories).items()
        },
    }

    for behavior, attr in (
        ("allow", "always_allow_rules"),
        ("deny", "always_deny_rules"),
        ("ask", "always_ask_rules"),
    ):
        rules_by_source: Mapping[str, tuple[str, ...]] = getattr(context, attr)
        behavior_rules: dict[str, str] = {}
        source_snapshot: dict[str, list[str]] = {}
        for source, rules in rules_by_source.items():
            source_snapshot[str(source)] = list(rules)
            for rule in rules:
                behavior_rules[str(rule)] = behavior
                if source in persistent_sources:
                    result["persistent"][str(rule)] = behavior
                elif source in session_sources:
                    result["session"][str(rule)] = behavior
        result[behavior] = behavior_rules
        result["rules_by_source"][behavior] = source_snapshot

    return result


__all__ = [
    "build_permission_rules_snapshot",
    "get_permission_mode",
    "load_permission_context",
    "set_session_permission_mode",
]
