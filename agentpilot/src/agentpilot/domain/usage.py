"""Durable provider-call and cost-ledger facts."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from agentpilot.domain.common import deep_freeze, require_aware, require_text


class ProviderCallStatus(str, Enum):
    """How far one provider dispatch got, and what its cost is owed to.

    Every value here is terminal for its call id: a result exists only once a
    dispatch produced something that has to be paid for or quarantined. Work
    that was provably never billed leaves a refusal instead of a result.
    """

    ACCOUNTING_PENDING = "accounting_pending"
    COMPLETED = "completed"
    UNATTESTED = "unattested"
    FAILED = "failed"


@dataclass(frozen=True)
class ProviderCallDispatch:
    """Durable proof that one intent was handed to the provider transport.

    Written immediately before the request leaves the process. An intent
    without this marker crashed before any network I/O, so redispatching it
    cannot double-bill; an intent with it may already have been charged.
    """

    call_id: str
    dispatched_at: datetime

    def __post_init__(self) -> None:
        require_text(self.call_id, "call_id")
        require_aware(self.dispatched_at, "dispatched_at")


@dataclass(frozen=True)
class ProviderCallRefusal:
    """Evidence that one dispatch was rejected without generating or billing.

    A refusal retracts its dispatch marker: the provider said it did no work,
    so the call id is exactly as unspent as it was before the request left.
    Refusals accumulate under one call id, which is what bounds how many times
    the same work may be handed over again.
    """

    call_id: str
    ordinal: int
    refused_at: datetime
    detail: str

    def __post_init__(self) -> None:
        require_text(self.call_id, "call_id")
        require_text(self.detail, "detail")
        require_aware(self.refused_at, "refused_at")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise ValueError("refusal ordinal must be an integer")
        if self.ordinal < 0:
            raise ValueError("refusal ordinal must be non-negative")


@dataclass(frozen=True)
class ProviderCallIntent:
    call_id: str
    run_id: str
    batch_id: str
    provider: str
    model: str
    purpose: str
    created_at: datetime
    action_id: str | None = None
    request_facts: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("call_id", "run_id", "batch_id", "provider", "model", "purpose"):
            require_text(getattr(self, name), name)
        require_aware(self.created_at, "created_at")
        object.__setattr__(self, "request_facts", deep_freeze(self.request_facts))


@dataclass(frozen=True)
class ProviderCallResult:
    call_id: str
    status: ProviderCallStatus
    completed_at: datetime
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    accounting_status: str
    provider_request_id: str | None = None
    response_text: str | None = None
    finish_reason: str | None = None
    error_kind: str | None = None
    # An exception class name says which contract broke but never which fact
    # broke it, and the operator reading this row afterwards cannot re-ask the
    # provider. The bounded sentence is the difference between diagnosing a
    # quarantine from the database and having to reproduce it.
    error_detail: str | None = None

    _MAX_ERROR_DETAIL = 512

    def __post_init__(self) -> None:
        require_text(self.call_id, "call_id")
        require_text(self.accounting_status, "accounting_status")
        require_aware(self.completed_at, "completed_at")
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")
        if self.cost_usd is not None and (
            not math.isfinite(self.cost_usd) or self.cost_usd < 0.0
        ):
            raise ValueError("provider call cost must be finite and non-negative")
        if self.status is ProviderCallStatus.ACCOUNTING_PENDING:
            if (
                self.input_tokens is None
                or self.output_tokens is None
                or not self.provider_request_id
                or self.error_kind is not None
            ):
                raise ValueError(
                    "accounting-pending provider call needs an immutable "
                    "response receipt"
                )
            if (self.cost_usd is None) != (self.accounting_status == "unknown"):
                raise ValueError(
                    "accounting-pending receipt cost/status are inconsistent"
                )
        elif self.status is ProviderCallStatus.COMPLETED:
            if (
                self.input_tokens is None
                or self.output_tokens is None
                or self.cost_usd is None
                or self.accounting_status == "unknown"
                or not self.provider_request_id
            ):
                raise ValueError(
                    "completed provider call needs attested usage and cost"
                )
            if self.error_kind is not None:
                raise ValueError("completed provider call cannot contain an error")
        elif not self.error_kind:
            raise ValueError("unsettled provider call needs error_kind")
        elif self.status is ProviderCallStatus.UNATTESTED and (
            self.cost_usd is not None or self.accounting_status != "unknown"
        ):
            raise ValueError("an unattested dispatch cannot claim a settled cost")
        if self.error_detail is not None:
            if not self.error_detail.strip():
                raise ValueError("error_detail must say something or be absent")
            if len(self.error_detail) > self._MAX_ERROR_DETAIL:
                raise ValueError("error_detail must be bounded")
            if self.error_kind is None:
                raise ValueError("error_detail without error_kind explains nothing")


def bounded_failure_detail(exc: BaseException) -> str:
    """Render one failure as a single bounded sentence fit for durable storage."""

    detail = " ".join(str(exc).split()) or type(exc).__name__
    limit = ProviderCallResult._MAX_ERROR_DETAIL
    return detail if len(detail) <= limit else detail[: limit - 1] + "…"


@dataclass(frozen=True)
class CostLedgerEntry:
    entry_id: str
    call_id: str
    run_id: str
    batch_id: str
    purpose: str
    cost_usd: float
    accounting_status: str
    created_at: datetime
    action_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "entry_id",
            "call_id",
            "run_id",
            "batch_id",
            "purpose",
            "accounting_status",
        ):
            require_text(getattr(self, name), name)
        require_aware(self.created_at, "created_at")
        if not math.isfinite(self.cost_usd) or self.cost_usd < 0.0:
            raise ValueError("ledger cost must be finite and non-negative")


__all__ = [
    "CostLedgerEntry",
    "ProviderCallDispatch",
    "ProviderCallIntent",
    "ProviderCallRefusal",
    "ProviderCallResult",
    "ProviderCallStatus",
    "bounded_failure_detail",
]
