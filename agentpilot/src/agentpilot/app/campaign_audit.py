"""Check a finished campaign against its acceptance shape from durable facts.

The supervisor decides what to do next; this module decides whether what already
happened counts. It reads only recorded facts, so it can be run long after the
processes exited and by someone who did not watch them run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from agentpilot.app.runtime_config import CampaignConfig
from agentpilot.domain import ProviderCallStatus
from agentpilot.persistence import BatchRecord, BatchStatus
from agentpilot.persistence.protocols import UnitOfWorkFactory


@dataclass(frozen=True)
class CampaignAudit:
    run_id: str
    completed_processes: int
    planning_exhausted_batches: int
    provider_calls: int
    total_cost_usd: float
    findings: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return not self.findings


def audit_campaign(
    unit_of_work: UnitOfWorkFactory,
    *,
    run_id: str,
    config: CampaignConfig,
) -> CampaignAudit:
    """Report every way a finished run departs from its acceptance shape."""

    with unit_of_work() as work:
        batches = tuple(work.batches.list_for_run(run_id))
        actions = {
            batch.batch_id: tuple(work.actions.list_for_batch(batch.batch_id))
            for batch in batches
        }
        observations = {
            batch.batch_id: tuple(work.observations.list_for_batch(batch.batch_id))
            for batch in batches
        }
        intents = tuple(work.usage.list_intents_for_run(run_id))
        dispatches = tuple(work.usage.list_dispatches_for_run(run_id))
        results = tuple(work.usage.list_results_for_run(run_id))
        costs = tuple(work.usage.list_costs_for_run(run_id))
        refusals = {
            intent.call_id: tuple(work.usage.list_refusals(intent.call_id))
            for intent in intents
        }

    findings: list[str] = []
    completed = tuple(
        batch for batch in batches if batch.status is BatchStatus.POLICY_UPDATED
    )
    exhausted = tuple(
        batch for batch in batches if batch.status is BatchStatus.PLANNING_EXHAUSTED
    )
    findings.extend(
        _process_findings(
            completed=completed,
            exhausted=exhausted,
            batches=batches,
            actions=actions,
            observations=observations,
            config=config,
        )
    )
    findings.extend(
        _accounting_findings(
            intents=intents,
            dispatches=dispatches,
            results=results,
            costs=costs,
            refusals=refusals,
        )
    )
    return CampaignAudit(
        run_id=run_id,
        completed_processes=len(completed),
        planning_exhausted_batches=len(exhausted),
        provider_calls=len(intents),
        total_cost_usd=round(sum(entry.cost_usd for entry in costs), 10),
        findings=tuple(findings),
    )


def _process_findings(
    *,
    completed: Sequence[BatchRecord],
    exhausted: Sequence[BatchRecord],
    batches: Sequence[BatchRecord],
    actions,
    observations,
    config: CampaignConfig,
) -> list[str]:
    findings: list[str] = []
    if len(completed) != config.total_processes:
        findings.append(
            f"the run completed {len(completed)} processes instead of "
            f"{config.total_processes}"
        )
    for batch in completed:
        expected = config.actions_per_process
        if batch.expected_rounds != expected:
            findings.append(
                f"{batch.batch_id} was planned for {batch.expected_rounds} "
                f"actions instead of {expected}"
            )
        counts = (
            len(batch.action_ids),
            len(batch.dispatched_action_ids),
            len(batch.completed_action_ids),
            len(actions[batch.batch_id]),
        )
        if set(counts) != {expected}:
            findings.append(
                f"{batch.batch_id} updated the policy with action counts "
                f"{counts} instead of {expected} throughout"
            )
        # One observation per action is what a barrier join consumes, so a
        # different count means the policy did not update exactly once.
        if len(observations[batch.batch_id]) != expected:
            findings.append(
                f"{batch.batch_id} carries "
                f"{len(observations[batch.batch_id])} observations instead of "
                f"{expected}"
            )
    for batch in exhausted:
        if batch.action_ids or batch.dispatched_action_ids:
            findings.append(
                f"{batch.batch_id} exhausted planning but still holds actions"
            )
        if observations[batch.batch_id]:
            findings.append(
                f"{batch.batch_id} exhausted planning but still holds rewards"
            )
    unresolved = tuple(
        batch
        for batch in batches
        if batch.status
        not in (BatchStatus.POLICY_UPDATED, BatchStatus.PLANNING_EXHAUSTED)
    )
    for batch in unresolved:
        findings.append(
            f"{batch.batch_id} is still {batch.status.value} rather than "
            "completed or exhausted"
        )
    return findings


def _accounting_findings(
    *, intents, dispatches, results, costs, refusals
) -> list[str]:
    findings: list[str] = []
    results_by_call = {item.call_id: item for item in results}
    costs_by_call: dict[str, list[object]] = {}
    for entry in costs:
        costs_by_call.setdefault(entry.call_id, []).append(entry)
    dispatched = {item.call_id for item in dispatches}
    for call_id in sorted(dispatched - set(results_by_call)):
        findings.append(f"{call_id} was dispatched without recording a receipt")
    for call_id, result in sorted(results_by_call.items()):
        if result.status is ProviderCallStatus.ACCOUNTING_PENDING:
            findings.append(f"{call_id} never finished reconciliation")
        elif result.status is ProviderCallStatus.UNATTESTED:
            findings.append(f"{call_id} holds a receipt that was never attested")
        elif result.status is ProviderCallStatus.FAILED:
            findings.append(
                f"{call_id} failed its accounting contract "
                f"({result.error_detail or result.error_kind or 'unknown reason'})"
            )
        elif result.status is ProviderCallStatus.COMPLETED:
            if not costs_by_call.get(call_id):
                findings.append(f"{call_id} settled without a cost ledger entry")
            if not result.response_text:
                # A billed dispatch with no content is what forces the caller to
                # pay again under a new call id, which is the redispatch the
                # campaign is required not to contain.
                findings.append(
                    f"{call_id} was billed for a completion with no content"
                )
        if len(costs_by_call.get(call_id, ())) > 1:
            findings.append(f"{call_id} was charged more than once")
    for call_id, recorded in sorted(refusals.items()):
        if recorded and costs_by_call.get(call_id):
            findings.append(f"{call_id} was refused and charged anyway")
    return findings


__all__ = ["CampaignAudit", "audit_campaign"]
