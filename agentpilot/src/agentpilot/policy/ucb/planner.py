"""BatchLoop-compatible adapter for UCB-based Policy Learning."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from agentpilot.domain.common import canonical_json, deep_freeze, require_text, stable_id
from agentpilot.domain.policy import (
    PendingReservation,
    PlanningFailureEvent,
    PlanningRoundFailed,
    PlanningSlotAttempt,
    PolicyDecisionPlan,
    PolicyMethod,
)
from agentpilot.domain.search import (
    CompatibilityKey,
    EvidenceCutoff,
    ParentSlate,
    SearchState,
)
from agentpilot.policy.ucb.carrier import (
    SlotGenerationAttempt,
    UCBFrozenSnapshot,
    UCBPolicyCarrier,
    derive_ucb_seed,
)
from agentpilot.policy.ucb.direction import NoValidDirection
from agentpilot.search.direction_semantic_index import PendingSemanticReservation
from agentpilot.search.evidence_retrieval import ProgramEvidence


@dataclass(frozen=True)
class UCBAlgorithmIdentity:
    proposer_identity: Mapping[str, Any]
    applicability_builder_id: str
    evidence_retriever_id: str
    parent_neighborhood_predicate_id: str
    direction_neighborhood_predicate_id: str
    effectiveness_identity: Mapping[str, Any]
    semantic_calibration_bundle_id: str
    base_seed: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.applicability_builder_id, "applicability_builder_id"),
            (self.evidence_retriever_id, "evidence_retriever_id"),
            (
                self.parent_neighborhood_predicate_id,
                "parent_neighborhood_predicate_id",
            ),
            (
                self.direction_neighborhood_predicate_id,
                "direction_neighborhood_predicate_id",
            ),
            (self.semantic_calibration_bundle_id, "semantic_calibration_bundle_id",),
        ):
            require_text(value, name)
        for value, name in (
            (self.proposer_identity, "proposer_identity"),
            (self.effectiveness_identity, "effectiveness_identity"),
        ):
            if not value:
                raise ValueError(f"{name} must be explicitly non-empty")
            canonical_json(value)
        if isinstance(self.base_seed, bool) or not isinstance(self.base_seed, int):
            raise ValueError("base_seed must be an integer")
        object.__setattr__(
            self, "proposer_identity", deep_freeze(self.proposer_identity),
        )
        object.__setattr__(
            self, "effectiveness_identity", deep_freeze(self.effectiveness_identity),
        )


@dataclass(frozen=True)
class UCBRoundInputs:
    parent_slate: ParentSlate
    eligible_arm_ids: tuple[str, ...]
    evidence_records: tuple[ProgramEvidence, ...]
    carrier_context: str
    attempt_id: str
    action_id: str
    seed: int
    planned_at: datetime


class UCBRoundInputProvider(Protocol):
    def __call__(
        self,
        *,
        state: SearchState,
        carrier_snapshot: UCBFrozenSnapshot,
        decision_round_id: str,
        round_index: int,
    ) -> UCBRoundInputs:
        ...


@dataclass(frozen=True)
class UCBPlannedAction:
    plan: PolicyDecisionPlan
    semantic_reservation: PendingSemanticReservation

    @property
    def action(self):
        return self.plan.action

    @property
    def reservation(self) -> PendingReservation:
        return self.semantic_reservation.reservation


class UCBActionPlannerAdapter:
    """Freeze once per batch and restore only from durable semantic records."""

    method = PolicyMethod.UCB

    def __init__(
        self,
        carrier: UCBPolicyCarrier,
        input_provider: UCBRoundInputProvider,
        algorithm_identity: UCBAlgorithmIdentity,
    ) -> None:
        if not callable(input_provider):
            raise TypeError("input_provider must be callable")
        if not isinstance(algorithm_identity, UCBAlgorithmIdentity):
            raise TypeError("algorithm_identity must be UCBAlgorithmIdentity")
        self.carrier = carrier
        self.input_provider = input_provider
        self.algorithm_identity = algorithm_identity

    @property
    def max_planning_attempts(self) -> int:
        """How many whole-slate replacements one logical round may spend."""

        return self.carrier.config.max_planning_attempts

    @staticmethod
    def _neighborhood_config(config) -> Mapping[str, Any]:
        return {
            "fallback_levels": list(config.fallback_levels),
            "min_neighborhood_observations": (config.min_neighborhood_observations),
            "exploration_constant": config.exploration_constant,
            "solver_tolerance": config.solver_tolerance,
            "solver_max_iterations": config.solver_max_iterations,
            "tie_tolerance": config.tie_tolerance,
        }

    def _algorithm_payload(self) -> Mapping[str, Any]:
        encoder = self.carrier.semantic_gate.encoder.identity
        thresholds = self.carrier.semantic_gate.thresholds
        cooldown = self.carrier.eligibility_controller.config
        identity = self.algorithm_identity
        return {
            "proposer": dict(identity.proposer_identity),
            "applicability_builder_id": identity.applicability_builder_id,
            "evidence_retriever_id": identity.evidence_retriever_id,
            "semantic": {
                "encoder_id": encoder.encoder_id,
                "encoder_identity": encoder.encoder_identity,
                "dimension": encoder.dimension,
                "distance": encoder.distance,
                "threshold_id": thresholds.threshold_id,
                "tau_repeat": thresholds.tau_repeat,
                "tau_arm": thresholds.tau_arm,
                "threshold_boundary_margin": (thresholds.threshold_boundary_margin),
                "nearest_arm_tie_tolerance": (thresholds.nearest_arm_tie_tolerance),
                "calibration_bundle_id": identity.semantic_calibration_bundle_id,
            },
            "parent_neighborhood": {
                "predicate_id": identity.parent_neighborhood_predicate_id,
                "config": self._neighborhood_config(
                    self.carrier.parent_policy.estimator.config
                ),
            },
            "direction_neighborhood": {
                "predicate_id": identity.direction_neighborhood_predicate_id,
                "config": self._neighborhood_config(
                    self.carrier.direction_policy.estimator.config
                ),
            },
            "effectiveness": dict(identity.effectiveness_identity),
            "base_seed": identity.base_seed,
            "cooldown": {
                "max_slot_generation_failures": (cooldown.max_slot_generation_failures),
                "slot_generation_cooldown_opportunities": (
                    cooldown.slot_generation_cooldown_opportunities
                ),
                "max_retryable_skips": cooldown.max_retryable_skips,
                "skip_cooldown_opportunities": (cooldown.skip_cooldown_opportunities),
            },
        }

    def begin_batch(self, state: SearchState) -> UCBFrozenSnapshot:
        return self.carrier.begin_batch(state)

    def freeze_snapshot(self, snapshot: object) -> Mapping[str, Any]:
        if not isinstance(snapshot, UCBFrozenSnapshot):
            raise TypeError("UCB planner snapshot has the wrong type")
        compatibility = snapshot.arm_snapshot.compatibility
        cutoff = snapshot.arm_snapshot.cutoff
        config = self.carrier.config
        return {
            "state_id": snapshot.state_id,
            "run_id": snapshot.run_id,
            "batch_id": snapshot.batch_id,
            "compatibility": {
                "objective_id": compatibility.objective_id,
                "evaluator_id": compatibility.evaluator_id,
                "metric_schema_id": compatibility.metric_schema_id,
                "metric_direction": compatibility.metric_direction,
                "environment_id": compatibility.environment_id,
            },
            "cutoff": cutoff.available_at.isoformat(),
            "arm_ids": list(snapshot.arm_snapshot.arm_ids),
            "arms": json.loads(canonical_json(snapshot.arm_snapshot.arms)),
            "eligibility": {
                "opportunity_seq": snapshot.eligibility.opportunity_seq,
                "eligible_arm_ids": list(snapshot.eligibility.eligible_arm_ids),
                "exclusions": [
                    {
                        "arm_id": item.arm_id,
                        "reason": item.reason,
                        "triggered_at_seq": item.triggered_at_seq,
                        "excluded_through_seq": item.excluded_through_seq,
                    }
                    for item in snapshot.eligibility.exclusions
                ],
            },
            "planner_config": {
                "generation_capacity": config.generation_capacity,
                "discover_quota": config.discover_quota,
                "max_slot_generation_attempts": (config.max_slot_generation_attempts),
                "discover_exclusion_limit": config.discover_exclusion_limit,
                "max_planning_attempts": config.max_planning_attempts,
                "max_scope_refs": config.max_scope_refs,
                "max_new_paths": config.max_new_paths,
                "max_resolved_scope_files": config.max_resolved_scope_files,
                "max_resolved_scope_bytes": config.max_resolved_scope_bytes,
            },
            "algorithm_identity": self._algorithm_payload(),
        }

    def restore_snapshot(self, payload: Mapping[str, Any]) -> UCBFrozenSnapshot:
        raw_compatibility = payload["compatibility"]
        compatibility = CompatibilityKey(**dict(raw_compatibility))
        cutoff = EvidenceCutoff(datetime.fromisoformat(str(payload["cutoff"])))
        arm_snapshot = self.carrier.arm_registry.snapshot(
            compatibility=compatibility, cutoff=cutoff,
        )
        if tuple(payload["arm_ids"]) != arm_snapshot.arm_ids:
            raise ValueError("rebuilt UCB arm registry differs from frozen snapshot")
        rebuilt = UCBFrozenSnapshot(
            state_id=str(payload["state_id"]),
            run_id=str(payload["run_id"]),
            batch_id=str(payload["batch_id"]),
            arm_snapshot=arm_snapshot,
            eligibility=self.carrier.eligibility_controller.report(
                arm_snapshot, base_arm_ids=arm_snapshot.arm_ids,
            ),
        )
        if canonical_json(self.freeze_snapshot(rebuilt)) != canonical_json(payload):
            raise ValueError("rebuilt UCB carrier state differs from durable snapshot")
        return rebuilt

    def plan_round(
        self,
        *,
        state: SearchState,
        carrier_snapshot: object,
        decision_round_id: str,
        round_index: int,
        planning_attempt_index: int,
        pending: tuple[PendingSemanticReservation, ...],
        prior_planning_failures: tuple[PlanningFailureEvent, ...] = (),
    ) -> UCBPlannedAction:
        if not isinstance(carrier_snapshot, UCBFrozenSnapshot):
            raise TypeError("UCB planner snapshot has the wrong type")
        self.carrier.restore_pending_semantics(pending)
        inputs = self.input_provider(
            state=state,
            carrier_snapshot=carrier_snapshot,
            decision_round_id=decision_round_id,
            round_index=round_index,
        )
        if not isinstance(inputs, UCBRoundInputs):
            raise TypeError("UCB round input provider returned the wrong type")
        expected_seed = derive_ucb_seed(
            self.algorithm_identity.base_seed, "round", decision_round_id, round_index,
        )
        if inputs.seed != expected_seed:
            raise ValueError("round seed differs from frozen base-seed derivation")
        try:
            result = self.carrier.plan(
                state=state,
                carrier_snapshot=carrier_snapshot,
                parent_slate=inputs.parent_slate,
                eligible_arm_ids=inputs.eligible_arm_ids,
                evidence_records=inputs.evidence_records,
                carrier_context=inputs.carrier_context,
                attempt_id=inputs.attempt_id,
                action_id=inputs.action_id,
                seed=inputs.seed,
                planned_at=inputs.planned_at,
                prior_rejected_directions=self._rejected_direction_history(
                    prior_planning_failures
                ),
            )
        except NoValidDirection as exc:
            event = PlanningFailureEvent(
                event_id=stable_id(
                    "planning-failure",
                    state.batch_id,
                    round_index,
                    planning_attempt_index,
                    self.method.value,
                ),
                run_id=state.run_id,
                batch_id=state.batch_id,
                state_id=state.state_id,
                decision_round_id=decision_round_id,
                round_index=round_index,
                planning_attempt_index=planning_attempt_index,
                method=self.method,
                failure_kind="no_valid_direction",
                slot_attempts=tuple(self._planning_attempt(item) for item in exc.trace),
                available_at=inputs.planned_at,
                compatibility=state.compatibility,
            )
            raise PlanningRoundFailed(event, str(exc)) from exc
        return UCBPlannedAction(result.plan, result.semantic_reservation)

    @staticmethod
    def _rejected_direction_history(
        events: tuple[PlanningFailureEvent, ...],
    ) -> tuple[Mapping[str, object], ...]:
        rows: list[Mapping[str, object]] = []
        seen: set[str] = set()
        for event in sorted(
            events, key=lambda item: (item.available_at, item.event_id), reverse=True,
        ):
            for attempt in reversed(event.slot_attempts):
                if attempt.failure_mechanism is None:
                    continue
                payload = {
                    "failure_mechanism": attempt.failure_mechanism,
                    "implementation_direction": attempt.implementation_direction,
                    "resolved_scope": list(attempt.resolved_scope),
                    "rejection": attempt.status.value,
                    "reason": attempt.reason,
                }
                identity = canonical_json(payload)
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(payload)
                if len(rows) == 12:
                    return tuple(rows)
        return tuple(rows)

    @staticmethod
    def _planning_attempt(item: object) -> PlanningSlotAttempt:
        if not isinstance(item, SlotGenerationAttempt):
            raise TypeError("NoValidDirection carried an untyped UCB attempt")
        request = item.request
        repair_ordinal = request.metadata.get("repair_ordinal")
        if isinstance(repair_ordinal, bool) or not isinstance(repair_ordinal, int):
            raise ValueError("UCB attempt has no typed repair ordinal")
        decision = item.semantic_decision
        proposal = item.proposal
        return PlanningSlotAttempt(
            request_id=request.request_id,
            source_slot_id=request.source_slot.slot_id,
            source_slot_kind=request.source_slot.kind,
            source_arm_id=request.source_slot.source_arm_id,
            slot_ordinal=request.source_slot.ordinal,
            repair_ordinal=repair_ordinal,
            status=item.adherence.status,
            reason=item.adherence.reason,
            proposal_id=proposal.proposal_id if proposal is not None else None,
            direction_id=(
                proposal.direction.direction_id if proposal is not None else None
            ),
            match_kind=decision.match.kind if decision is not None else None,
            matched_arm_id=decision.match.arm_id if decision is not None else None,
            failure_mechanism=(
                proposal.direction.failure_mechanism if proposal is not None else None
            ),
            implementation_direction=(
                proposal.direction.implementation_direction
                if proposal is not None
                else None
            ),
            resolved_scope=(
                proposal.direction.resolved_scope if proposal is not None else ()
            ),
        )


__all__ = [
    "UCBAlgorithmIdentity",
    "UCBActionPlannerAdapter",
    "UCBPlannedAction",
    "UCBRoundInputProvider",
    "UCBRoundInputs",
]
