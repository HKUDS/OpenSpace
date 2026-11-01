"""Immutable batch view of the single policy-memory file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentpilot.domain.common import require_text


@dataclass(frozen=True)
class MemorySnapshot:
    """The exact memory text visible throughout one batch.

    The file may be replaced after the batch barrier, but D1, D2 and D3 keep
    this value and therefore cannot observe sibling feedback mid-batch.
    """

    run_id: str
    batch_id: str
    state_id: str
    path: Path
    content: str

    def __post_init__(self) -> None:
        for name in ("run_id", "batch_id", "state_id"):
            require_text(getattr(self, name), name)
        if not isinstance(self.path, Path):
            raise ValueError("path must be a pathlib.Path")
        if not isinstance(self.content, str):
            raise ValueError("memory content must be text")


__all__ = ["MemorySnapshot"]
