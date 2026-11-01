"""Durable ownership contracts for one active batch driver per run."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from agentpilot.domain.common import require_aware, require_text


class RunLeaseUnavailable(RuntimeError):
    """Another live owner already drives the requested run."""


class RunLeaseLost(RuntimeError):
    """A lease claim is expired, released, or superseded by a newer fence."""


@dataclass(frozen=True)
class RunBatchLease:
    """One time-bounded claim to drive a batch for a run.

    ``fencing_token`` is monotonically increasing per run and survives release.
    Callers must use the complete ``run_id/batch_id/owner_token/fencing_token``
    identity when renewing, releasing, or opening a fenced unit of work.
    """

    run_id: str
    batch_id: str
    owner_token: str
    fencing_token: int
    acquired_at: datetime
    renewed_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        for name in ("run_id", "batch_id", "owner_token"):
            require_text(getattr(self, name), name)
        if (
            isinstance(self.fencing_token, bool)
            or self.fencing_token < 1
        ):
            raise ValueError("fencing_token must be positive")
        for name in ("acquired_at", "renewed_at", "expires_at"):
            require_aware(getattr(self, name), name)
        if self.renewed_at < self.acquired_at:
            raise ValueError("renewed_at precedes acquired_at")
        if self.expires_at <= self.renewed_at:
            raise ValueError("expires_at must follow renewed_at")

    def same_claim(self, other: "RunBatchLease") -> bool:
        """Return whether two records carry the same fencing identity."""

        return (
            self.run_id,
            self.batch_id,
            self.owner_token,
            self.fencing_token,
        ) == (
            other.run_id,
            other.batch_id,
            other.owner_token,
            other.fencing_token,
        )

    def is_live_at(self, now: datetime) -> bool:
        """Return whether the claim has not reached its exclusive deadline."""

        require_aware(now, "now")
        return now < self.expires_at


class RunBatchLeaseManager(Protocol):
    """Atomic lease operations; all timestamps are caller-visible facts."""

    def acquire(
        self,
        *,
        run_id: str,
        batch_id: str,
        owner_token: str,
        now: datetime,
        ttl: timedelta,
    ) -> RunBatchLease:
        ...

    def renew(
        self,
        lease: RunBatchLease,
        *,
        now: datetime,
        ttl: timedelta,
    ) -> RunBatchLease:
        ...

    def release(self, lease: RunBatchLease, *, now: datetime) -> None:
        ...

    def current(self, run_id: str) -> RunBatchLease | None:
        """Return the persisted claim, which may already be expired."""

        ...


__all__ = [
    "RunBatchLease",
    "RunBatchLeaseManager",
    "RunLeaseLost",
    "RunLeaseUnavailable",
]
