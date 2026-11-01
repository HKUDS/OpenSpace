"""Task-query normalization for system-side retrieval."""

from __future__ import annotations

from typing import Any, MutableMapping


_TERMINAL_BENCH_MARKER = "You are running inside a Terminal-Bench task container."
_TASK_MARKER = "\nTask:\n"


def derive_task_query(instruction: Any) -> str:
    """Return the user task text for system-side search/retrieval decisions."""
    text = str(instruction or "")
    if _TERMINAL_BENCH_MARKER in text and _TASK_MARKER in text:
        task_text = text.split(_TASK_MARKER, 1)[1].strip()
        if task_text:
            return task_text
    return text


def resolve_task_query(
    context: MutableMapping[str, Any],
    instruction: Any,
) -> str:
    """Resolve and store the semantic task query shared by retrieval paths."""
    for key in ("tool_retrieval_query", "task_query", "task_prompt"):
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            query = value.strip()
            break
    else:
        query = derive_task_query(instruction)

    context["task_query"] = query
    context["tool_retrieval_query"] = query
    return query

