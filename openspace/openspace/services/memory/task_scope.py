"""Stable scope keys for background memory work."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


def _context_value(context: Any, key: str) -> Any:
    if isinstance(context, Mapping):
        return context.get(key)
    return getattr(context, key, None)


def _normalize_scope_path(value: Any) -> str:
    return str(Path(str(value)).expanduser().resolve())


def resolve_memory_task_scope_key(context: Any) -> str:
    """Return the stable scope key used to group memory background tasks."""

    if context is None:
        raise ValueError("Memory task scope requires a context.")

    session_id = _context_value(context, "session_id")
    if session_id:
        return f"session_id:{session_id}"

    session_dir = _context_value(context, "session_dir")
    if session_dir:
        return f"session_dir:{_normalize_scope_path(session_dir)}"

    cwd = _context_value(context, "cwd")
    if cwd:
        return f"cwd:{_normalize_scope_path(cwd)}"

    raise ValueError(
        "Memory task scope requires context.session_dir, context.session_id, or context.cwd."
    )


def maybe_memory_task_scope_key(context: Any | None) -> str | None:
    if context is None:
        return None
    try:
        return resolve_memory_task_scope_key(context)
    except ValueError:
        return None


__all__ = [
    "maybe_memory_task_scope_key",
    "resolve_memory_task_scope_key",
]
