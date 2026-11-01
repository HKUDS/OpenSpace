"""Shared identifiers for memory evidence refs."""

from __future__ import annotations

import hashlib
from pathlib import Path


def memory_ref_id(
    source_kind: str,
    *,
    session_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    path: str,
) -> str:
    """Return an opaque, scope-aware id for a memory ResourceRef.

    Consumers should use ResourceRef columns and metadata for structured reads;
    this id is only a stable dedupe key.
    """

    return (
        "memory_ref:"
        f"{_ref_component(source_kind)}:"
        f"sid-{_ref_component(session_id)}:"
        f"tid-{_ref_component(task_id)}:"
        f"aid-{_ref_component(agent_id)}:"
        f"path-{canonical_path_hash(path)[:24]}"
    )


def canonical_path_hash(path_text: str) -> str:
    try:
        canonical = str(Path(path_text).expanduser().resolve(strict=False))
    except Exception:
        canonical = path_text
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ref_component(value: str | None) -> str:
    text = str(value) if value is not None else "none"
    if not text:
        text = "none"
    return "".join(char if char.isalnum() or char in {"_", "-", "."} else "_" for char in text)
