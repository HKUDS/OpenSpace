"""Durable records used by the lightweight research portfolio."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


class ResearchBudgetUnavailable(ValueError):
    """A controller reservation cannot be made under its private limit."""

    presentation_code = 'BUDGET_LIMIT'


class ResearchContentUnavailable(ValueError):
    """A bounded reader cannot return the requested public content."""

    presentation_code = 'CONTENT_LIMIT'


@dataclass(frozen=True)
class Artifact:
    """An immutable snapshot of a base program or implemented candidate."""

    id: str
    commit_sha: str
    config: dict[str, Any]
    base_id: str | None
    track_id: str | None
    metadata: dict[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Track:
    """A persistent research line whose lifetime is independent of a session."""

    id: str
    label: str
    state: str
    reason: str | None
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Ticket:
    """One asynchronous experiment bound to a research track."""

    id: str
    track_id: str
    kind: str
    arguments: dict[str, Any]
    label: str
    status: str
    reservation_usd: float
    cost_usd: float | None
    result: dict[str, Any] | None
    artifact_id: str | None
    idempotency_key: str
    created_at: float
    started_at: float | None
    finished_at: float | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
