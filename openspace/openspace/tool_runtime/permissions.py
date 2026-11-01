"""Tool-runtime permission facade.

Permission policy types still live under ``grounding.core.permissions``.  The
runtime pipeline imports them through this module so future phases can move the
implementation without touching execution code again.
"""

from __future__ import annotations

from openspace.grounding.core.permissions import (
    apply_permission_update,
    has_permissions_to_use_tool,
    persist_permission_updates,
)


def resolve_permission_ask(tool_use_id: str, response: dict[str, object]) -> bool:
    from openspace.tool_runtime.pipeline.execution import resolve_permission_ask as _impl

    return _impl(tool_use_id, response)


def reject_permission_ask(tool_use_id: str, reason: str) -> bool:
    from openspace.tool_runtime.pipeline.execution import reject_permission_ask as _impl

    return _impl(tool_use_id, reason)


def is_permission_ask_pending(tool_use_id: str) -> bool:
    from openspace.tool_runtime.pipeline.execution import is_permission_ask_pending as _impl

    return _impl(tool_use_id)


def pending_permission_ask_ids() -> tuple[str, ...]:
    from openspace.tool_runtime.pipeline.execution import pending_permission_ask_ids as _impl

    return _impl()


__all__ = [
    "apply_permission_update",
    "has_permissions_to_use_tool",
    "is_permission_ask_pending",
    "pending_permission_ask_ids",
    "persist_permission_updates",
    "reject_permission_ask",
    "resolve_permission_ask",
]
