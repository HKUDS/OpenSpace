from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from agentpilot.domain import ProviderCallIntent, ProviderCallResult, ProviderCallStatus
from agentpilot.persistence.protocols import UnitOfWorkFactory
from agentpilot.providers import (
    AccountingStatus,
    EmbeddingAccountingLookup,
    EmbeddingAccountingRecord,
    ProviderAccountingPendingError,
    analytics_cost_agrees,
    names_same_model,
)

EMBEDDING_PURPOSE = "direction-semantic-embedding"


@dataclass(frozen=True)
class EmbeddingAccountingAudit:
    run_id: str
    embedding_calls: int
    confirmed_now: int
    confirmed_during_the_run: int
    awaiting_rows: tuple[str, ...]
    findings: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        """Every embedding charge has been matched to an authoritative row."""

        return not self.findings and not self.awaiting_rows


def audit_embedding_accounting(
    unit_of_work: UnitOfWorkFactory,
    *,
    run_id: str,
    lookup: EmbeddingAccountingLookup,
) -> EmbeddingAccountingAudit:
    """Match every embedding charge in one run against the analytics rows."""

    if not callable(getattr(lookup, "lookup", None)):
        raise TypeError("embedding accounting audit needs a lookup()")
    with unit_of_work() as work:
        intents = tuple(work.usage.list_intents_for_run(run_id))
        results = {
            result.call_id: result
            for result in work.usage.list_results_for_run(run_id)
        }

    embeddings = tuple(
        intent for intent in intents if intent.purpose == EMBEDDING_PURPOSE
    )
    findings: list[str] = []
    awaiting: list[str] = []
    confirmed_now = 0
    confirmed_during_the_run = 0

    for intent in sorted(embeddings, key=lambda item: item.call_id):
        result = results.get(intent.call_id)
        if result is None or result.status is not ProviderCallStatus.COMPLETED:
            findings.append(f"{intent.call_id} never settled its accounting")
            continue
        if result.accounting_status == AccountingStatus.ANALYTICS_RECONCILED.value:
            confirmed_during_the_run += 1
            continue
        if result.accounting_status != AccountingStatus.PROVIDER_REPORTED.value:
            findings.append(
                f"{intent.call_id} settled as {result.accounting_status}, "
                "which no analytics row can confirm"
            )
            continue
        attestation = result.provider_request_id
        if not attestation:
            findings.append(f"{intent.call_id} holds no id to confirm")
            continue
        try:
            record = lookup.lookup(attestation)
        except ProviderAccountingPendingError:
            awaiting.append(intent.call_id)
            continue
        except Exception as exc:
            findings.append(
                f"{intent.call_id} could not be looked up: {type(exc).__name__}"
            )
            continue
        if not isinstance(record, EmbeddingAccountingRecord):
            findings.append(f"{intent.call_id} returned a malformed row")
            continue
        mismatches = _row_mismatches(intent, result, record)
        if mismatches:
            findings.extend(f"{intent.call_id} {item}" for item in mismatches)
            continue
        confirmed_now += 1

    return EmbeddingAccountingAudit(
        run_id=run_id,
        embedding_calls=len(embeddings),
        confirmed_now=confirmed_now,
        confirmed_during_the_run=confirmed_during_the_run,
        awaiting_rows=tuple(awaiting),
        findings=tuple(findings),
    )


def _row_mismatches(
    intent: ProviderCallIntent,
    result: ProviderCallResult,
    record: EmbeddingAccountingRecord,
) -> tuple[str, ...]:
    mismatches: list[str] = []
    if record.embedding_id != result.provider_request_id:
        mismatches.append("was answered with another row's id")
    if not names_same_model(intent.model, record.model):
        mismatches.append(f"was billed as {record.model!r}")
    if record.request_count != 1:
        mismatches.append(f"covers {record.request_count} requests")
    if record.input_tokens != (result.input_tokens or 0):
        mismatches.append(
            f"was billed for {record.input_tokens} input tokens, "
            f"not {result.input_tokens or 0}"
        )
    if result.cost_usd is None:
        mismatches.append("settled without a cost")
        return tuple(mismatches)
    try:
        settled = Decimal(str(result.cost_usd))
    except InvalidOperation:
        mismatches.append("settled with a cost that cannot be compared")
        return tuple(mismatches)
    if not analytics_cost_agrees(settled, record.total_cost):
        mismatches.append(
            f"charged {settled} against a row of {record.total_cost}"
        )
    return tuple(mismatches)


__all__ = [
    "EMBEDDING_PURPOSE",
    "EmbeddingAccountingAudit",
    "audit_embedding_accounting",
]
