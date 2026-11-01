"""Shared path resolution for an explicitly selected OpenSpace workspace."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_workspace_dir(workspace_dir: str | Path | None = None) -> str | None:
    """Prefer an explicit workspace over the environment; keep legacy defaults otherwise."""
    value = workspace_dir or os.environ.get("OPENSPACE_WORKSPACE")
    if not value or not str(value).strip():
        return None
    return str(Path(value).expanduser().resolve())
