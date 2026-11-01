"""Canonical projection from execution facts to policy-visible observations."""

from __future__ import annotations

from dataclasses import dataclass

from agentpilot.app.contracts import ExecutionResult
from agentpilot.domain import (
    PolicyDecisionPlan,
    PolicyObservation,
    SearchState,
    stable_id,
)
from agentpilot.evaluation import EffectivenessRule, classify_effectiveness


@dataclass(frozen=True)
class CanonicalObservationBuilder:
    """Build carrier-neutral feedback consumed after a barrier."""

    effectiveness_rule: EffectivenessRule

    def build(
        self, *, state: SearchState, plan: PolicyDecisionPlan, result: ExecutionResult,
    ) -> PolicyObservation:
        action = plan.action
        if action.state_id != state.state_id:
            raise ValueError("action and SearchState identities differ")
        if result.worker.action_id != action.action_id:
            raise ValueError("worker result belongs to a different action")
        if result.outcome.action_id != action.action_id:
            raise ValueError("paired outcome belongs to a different action")
        effectiveness = classify_effectiveness(
            result.worker, result.outcome, self.effectiveness_rule,
        )
        source_slot_kind = (
            action.source_slot.kind if action.source_slot is not None else None
        )
        selected_parents = tuple(
            candidate
            for candidate in plan.parent_slate.candidates
            if candidate.parent_id == plan.parent_selection.parent_id
            and candidate.commit_sha == plan.parent_selection.commit_sha
        )
        if len(selected_parents) != 1:
            raise ValueError(
                "typed plan does not identify one selected parent candidate"
            )
        parent_descriptor = selected_parents[0].descriptor
        metadata: dict[str, object] = {
            "direction_id": action.direction.direction_id,
            "admission_fate": result.outcome.admission_fate,
        }
        if result.worker.candidate_program_id is not None:
            # The measured anonymous Visible-A snapshot.  Direction history
            # resolves it to that candidate's published trace manifest, so the
            # next proposer can read how the rejected attempt actually behaved.
            metadata["candidate_program_id"] = result.worker.candidate_program_id
        return PolicyObservation(
            observation_id=stable_id("policy-observation", action.action_id),
            action_id=action.action_id,
            evidence_tier=result.outcome.evidence_tier,
            method=action.method,
            parent_id=action.parent_id,
            arm_id=action.arm_id,
            source_slot_kind=source_slot_kind,
            direction_match=action.direction_match,
            compatibility=state.compatibility,
            parent_descriptor=parent_descriptor,
            direction_descriptor=state.direction_descriptor,
            implementation_adherence=result.worker.adherence,
            outcome_attribution=result.outcome.attribution,
            evaluation_validity=result.outcome.validity,
            effectiveness=effectiveness,
            available_at=effectiveness.available_at,
            metadata=metadata,
        )


__all__ = ["CanonicalObservationBuilder"]
