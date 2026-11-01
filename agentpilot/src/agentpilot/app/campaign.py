"""Durable campaign decisions derived from batch and provider-call facts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Sequence

from agentpilot.domain import (
    ProviderCallDispatch,
    ProviderCallIntent,
    ProviderCallResult,
    ProviderCallStatus,
)
from agentpilot.domain.common import require_aware
from agentpilot.persistence import BatchRecord, BatchStatus


class CampaignDisposition(str, Enum):
    COMPLETE = "complete"
    RESUME = "resume"
    AWAIT_RECONCILIATION = "await_reconciliation"
    WAIT_FOR_LEASE = "wait_for_lease"
    REPLACE_PLANNING_BATCH = "replace_planning_batch"
    ACCOUNTING_QUARANTINE = "accounting_quarantine"
    TERMINAL_FAILURE = "terminal_failure"


@dataclass(frozen=True)
class CampaignAssessment:
    disposition: CampaignDisposition
    reason: str


@dataclass(frozen=True)
class ReconciliationWindow:
    """How long a receipt may wait for authoritative accounting.

    Polling a lookup forever is indistinguishable from a stuck campaign, so a
    receipt that never becomes reconcilable has to convert into quarantine at a
    stated deadline instead.
    """

    now: datetime
    deadline_seconds: float

    def __post_init__(self) -> None:
        require_aware(self.now, "now")
        if (
            isinstance(self.deadline_seconds, bool)
            or not isinstance(self.deadline_seconds, (int, float))
            or not math.isfinite(self.deadline_seconds)
            or self.deadline_seconds <= 0
        ):
            raise ValueError(
                "reconciliation deadline must be finite and positive"
            )

    def expired(self, observed_at: datetime) -> bool:
        require_aware(observed_at, "observed_at")
        return self.now - observed_at > timedelta(seconds=self.deadline_seconds)


def assess_batch(
    batch: BatchRecord,
    *,
    intents: Sequence[ProviderCallIntent],
    dispatches: Sequence[ProviderCallDispatch],
    results: Sequence[ProviderCallResult],
    reconciliation: ReconciliationWindow,
    lease_live: bool = False,
) -> CampaignAssessment:
    """Classify one physical batch without trusting a process exit code."""

    if not isinstance(batch, BatchRecord):
        raise TypeError("campaign assessment requires a BatchRecord")
    if not isinstance(reconciliation, ReconciliationWindow):
        raise TypeError("campaign assessment requires a ReconciliationWindow")
    if not isinstance(lease_live, bool):
        raise TypeError("lease_live must be boolean")
    typed_intents = tuple(intents)
    typed_dispatches = tuple(dispatches)
    typed_results = tuple(results)
    if any(not isinstance(item, ProviderCallIntent) for item in typed_intents):
        raise TypeError("intents must contain ProviderCallIntent values")
    if any(not isinstance(item, ProviderCallDispatch) for item in typed_dispatches):
        raise TypeError("dispatches must contain ProviderCallDispatch values")
    if any(not isinstance(item, ProviderCallResult) for item in typed_results):
        raise TypeError("results must contain ProviderCallResult values")
    if lease_live:
        return CampaignAssessment(
            CampaignDisposition.WAIT_FOR_LEASE,
            "the batch still has a live execution lease",
        )

    owned = {
        intent.call_id
        for intent in typed_intents
        if intent.batch_id == batch.batch_id
    }
    results_by_call = {
        item.call_id: item for item in typed_results if item.call_id in owned
    }
    dispatched = {
        item.call_id for item in typed_dispatches if item.call_id in owned
    }
    # An intent whose dispatch marker is absent never reached the transport, or
    # was refused and retracted. Either way nothing was billed and the work can
    # be handed over again, so it is not evidence of an unaccounted charge.
    unaccounted = tuple(
        call_id
        for call_id in sorted(dispatched)
        if call_id not in results_by_call
    )
    unattested = tuple(
        result.call_id
        for result in results_by_call.values()
        if result.status is ProviderCallStatus.UNATTESTED
        or (
            result.status is ProviderCallStatus.FAILED
            and result.accounting_status == "unknown"
        )
    )
    lifecycle_status = (
        batch.resume_from if batch.status is BatchStatus.INTERRUPTED else batch.status
    )
    replaceable_planning_failure = (
        lifecycle_status is BatchStatus.DRAFTING
        and not batch.dispatched_action_ids
        and not batch.completed_action_ids
        and len(batch.action_ids) < batch.expected_rounds
    )
    if unaccounted and replaceable_planning_failure:
        return CampaignAssessment(
            CampaignDisposition.REPLACE_PLANNING_BATCH,
            "an unaccounted provider dispatch stopped planning before action dispatch",
        )
    if unaccounted:
        return CampaignAssessment(
            CampaignDisposition.ACCOUNTING_QUARANTINE,
            "a dispatched provider call never recorded a receipt",
        )
    if unattested and replaceable_planning_failure:
        return CampaignAssessment(
            CampaignDisposition.REPLACE_PLANNING_BATCH,
            "an unattested provider receipt stopped planning before action dispatch",
        )
    if unattested:
        return CampaignAssessment(
            CampaignDisposition.ACCOUNTING_QUARANTINE,
            "a provider receipt has no reconcilable accounting identity",
        )
    if batch.status is BatchStatus.FAILED:
        return CampaignAssessment(
            CampaignDisposition.TERMINAL_FAILURE,
            batch.error or "batch failed an immutable contract",
        )
    if any(
        result.status is ProviderCallStatus.FAILED
        for result in results_by_call.values()
    ):
        return CampaignAssessment(
            CampaignDisposition.TERMINAL_FAILURE,
            "a settled provider call failed its accounting contract",
        )
    pending = tuple(
        result
        for result in results_by_call.values()
        if result.status is ProviderCallStatus.ACCOUNTING_PENDING
    )
    if pending:
        oldest = min(result.completed_at for result in pending)
        if reconciliation.expired(oldest):
            return CampaignAssessment(
                CampaignDisposition.ACCOUNTING_QUARANTINE,
                "a provider receipt outlived its reconciliation deadline",
            )
        return CampaignAssessment(
            CampaignDisposition.AWAIT_RECONCILIATION,
            "a provider receipt is waiting for authoritative accounting",
        )
    if batch.status is BatchStatus.POLICY_UPDATED:
        if len(batch.completed_action_ids) != batch.expected_rounds:
            return CampaignAssessment(
                CampaignDisposition.TERMINAL_FAILURE,
                "policy-updated batch has incomplete actions",
            )
        return CampaignAssessment(
            CampaignDisposition.COMPLETE,
            "batch completed its policy update",
        )
    if batch.status is BatchStatus.PLANNING_EXHAUSTED:
        return CampaignAssessment(
            CampaignDisposition.REPLACE_PLANNING_BATCH,
            "bounded planning attempts were exhausted before dispatch",
        )
    return CampaignAssessment(
        CampaignDisposition.RESUME,
        "batch has durable progress and no terminal failure",
    )


__all__ = [
    "CampaignAssessment",
    "CampaignDisposition",
    "ReconciliationWindow",
    "assess_batch",
]
