"""Carrier-neutral projection from one evaluated action to archive facts."""

from __future__ import annotations

from agentpilot.app.contracts import ExecutionResult, TerminalBatchError
from agentpilot.domain import (
    EvidenceTier,
    EvaluationValidity,
    ImplementationAdherence,
    OutcomeAttribution,
    PolicyAction,
    PolicyObservation,
    WorkerExecutionStatus,
)
from agentpilot.persistence import ArchiveTransition, LineageEdge


def require_admissible_child(result: ExecutionResult) -> None:
    """Reject an admitted fate that cannot produce a valid searchable child."""

    worker = result.worker
    outcome = result.outcome
    if outcome.evidence_tier is EvidenceTier.SEALED_C:
        raise TerminalBatchError("sealed C evidence cannot update the archive")
    child_commit = worker.result_commit_sha
    if worker.status is not WorkerExecutionStatus.COMPLETED:
        raise TerminalBatchError("admitted child requires a completed worker")
    if worker.adherence is not ImplementationAdherence.FOLLOWED:
        raise TerminalBatchError(
            "admitted child requires followed implementation adherence"
        )
    if not isinstance(child_commit, str) or not child_commit.strip():
        raise TerminalBatchError("admitted child requires a result commit")
    if outcome.child_readout is None:
        raise TerminalBatchError(
            "admitted child requires an explicit evaluator readout"
        )
    if outcome.validity is not EvaluationValidity.VALID:
        raise TerminalBatchError("admitted child requires a valid evaluation")
    if outcome.attribution is not OutcomeAttribution.ATTRIBUTABLE:
        raise TerminalBatchError("admitted child requires attributable evidence")
    if outcome.constraints_pass is not True:
        raise TerminalBatchError("admitted child requires passing constraints")


class CanonicalArchiveTransitioner:
    """Record every attempt and add lineage whenever a child commit exists."""

    def __init__(self, *, admitted_fate: str = "admitted") -> None:
        if not admitted_fate.strip():
            raise ValueError("admitted fate must be non-empty")
        self.admitted_fate = admitted_fate

    def transition(
        self,
        *,
        action: PolicyAction,
        result: ExecutionResult,
        observation: PolicyObservation,
    ) -> tuple[ArchiveTransition, LineageEdge | None]:
        if result.worker.action_id != action.action_id:
            raise TerminalBatchError(
                "worker result belongs to a different action"
            )
        if result.outcome.action_id != action.action_id:
            raise TerminalBatchError(
                "paired outcome belongs to a different action"
            )
        if observation.action_id != action.action_id:
            raise TerminalBatchError(
                "policy observation belongs to a different action"
            )
        if result.outcome.evidence_tier is EvidenceTier.SEALED_C:
            raise TerminalBatchError("sealed C evidence cannot update the archive")
        if observation.evidence_tier is not result.outcome.evidence_tier:
            raise TerminalBatchError(
                "policy observation and outcome evidence tiers differ"
            )
        child = result.worker.result_commit_sha
        requested_admission = result.outcome.admission_fate == self.admitted_fate
        adherence_veto = (
            requested_admission
            and result.worker.adherence is not ImplementationAdherence.FOLLOWED
        )
        admitted = requested_admission and not adherence_veto
        if admitted:
            require_admissible_child(result)
        payload = {
            "admission_fate": result.outcome.admission_fate,
            "evaluation_validity": result.outcome.validity.value,
            "outcome_attribution": result.outcome.attribution.value,
            "worker_status": result.worker.status.value,
            "implementation_adherence": result.worker.adherence.value,
        }
        if adherence_veto:
            payload["archive_veto"] = "implementation_adherence"
        transition = ArchiveTransition(
            action_id=action.action_id,
            parent_commit_sha=action.parent_commit_sha,
            child_commit_sha=child,
            admitted=admitted,
            effectiveness_label=observation.effectiveness.label.value,
            payload=payload,
        )
        edge = (
            LineageEdge(action.action_id, action.parent_commit_sha, child)
            if child is not None
            else None
        )
        return transition, edge


__all__ = ["CanonicalArchiveTransitioner", "require_admissible_child"]
