"""Composable D1/D2/D3 planner for UCB-based Policy Learning.

The planner owns policy selection and immutable audit records.  The injected
``DirectionSemanticGate`` remains the only component allowed to classify a
direction or assign a pending arm identity.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Mapping, Protocol, Sequence

from agentpilot.domain.common import (
    canonical_json,
    require_aware,
    require_text,
    stable_id,
)
from agentpilot.domain.policy import (
    ArmSlot,
    ArmSpec,
    DirectionCandidate,
    DirectionSlate,
    PolicyAction,
    PolicyDecisionPlan,
    PolicyMethod,
    SlotAdherence,
    StructuredDirection,
)
from agentpilot.domain.search import ParentSelection, ParentSlate, SearchState
from agentpilot.policy.proposer import (
    RepositoryMapProvider,
    RetryableProposalError,
    StructuredProposal,
    StructuredProposalRequest,
    StructuredProposer,
    VisibleAEvidenceProvider,
    validate_direction_targets,
)
from agentpilot.policy.proposer.base import ProposerDeferred
from agentpilot.policy.ucb.arm_allocator import ArmAllocator
from agentpilot.policy.ucb.arms import (
    ArmRegistrySnapshot,
    InnovationArm,
    InnovationArmRegistry,
)
from agentpilot.policy.ucb.cooldown import (
    ArmEligibilityController,
    ArmEligibilityReport,
)
from agentpilot.policy.ucb.direction import (
    DirectionUCB,
    NoValidDirection,
    generation_failed,
)
from agentpilot.policy.ucb.parent import ParentUCB
from agentpilot.search.direction_history import FailureCardSelection
from agentpilot.search.direction_semantic_index import (
    DirectionApplicability,
    PendingArmIdentityOverlay,
    PendingSemanticReservation,
    cosine_distance,
    decode_semantic_record,
)
from agentpilot.search.duplicate_gate import (
    DirectionSemanticGate,
    ReservedDirection,
    SemanticGateDecision,
)
from agentpilot.search.evidence_retrieval import (
    FixedEvidenceRetriever,
    FixedEvidenceSelection,
    ProgramEvidence,
)

UCB_GENERATION_CONTEXT_CONTRACT = "ucb-arm-spec-generation-context"


def _arm_spec_payload(arm_spec: ArmSpec | None) -> dict[str, str] | None:
    if arm_spec is None:
        return None
    return {
        "failure_mechanism": arm_spec.failure_mechanism,
    }


def derive_ucb_seed(seed: int, *coordinates: object) -> int:
    """Derive a stable sub-seed from frozen UCB decision coordinates."""

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("planning seed must be an integer")
    value = stable_id("ucb-derived-seed", seed, *coordinates)
    return uuid.UUID(value[-36:]).int % (2 ** 63)


@dataclass(frozen=True)
class UCBPlannerConfig:
    generation_capacity: int
    discover_quota: int
    max_slot_generation_attempts: int
    discover_exclusion_limit: int
    max_planning_attempts: int
    max_scope_refs: int
    max_new_paths: int
    max_resolved_scope_files: int
    max_resolved_scope_bytes: int

    def __post_init__(self) -> None:
        if isinstance(self.generation_capacity, bool) or self.generation_capacity < 1:
            raise ValueError("generation_capacity must be positive")
        if (
            isinstance(self.discover_exclusion_limit, bool)
            or self.discover_exclusion_limit < 1
        ):
            raise ValueError("discover_exclusion_limit must be positive")
        if (
            isinstance(self.max_planning_attempts, bool)
            or self.max_planning_attempts < 1
        ):
            raise ValueError("max_planning_attempts must be positive")
        if (
            isinstance(self.discover_quota, bool)
            or not 1 <= self.discover_quota <= self.generation_capacity
        ):
            raise ValueError("discover_quota must be in [1, generation_capacity]")
        if (
            isinstance(self.max_slot_generation_attempts, bool)
            or self.max_slot_generation_attempts < 1
        ):
            raise ValueError("max_slot_generation_attempts must be positive")
        for name, upper in (
            ("max_scope_refs", 8),
            ("max_new_paths", 4),
            ("max_resolved_scope_files", 4_096),
            ("max_resolved_scope_bytes", 64 * 1024 * 1024),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= upper
            ):
                raise ValueError(f"{name} must be in [1, {upper}]")


@dataclass(frozen=True)
class UCBFrozenSnapshot:
    state_id: str
    run_id: str
    batch_id: str
    arm_snapshot: ArmRegistrySnapshot
    eligibility: ArmEligibilityReport

    def __post_init__(self) -> None:
        for value, name in (
            (self.state_id, "state_id"),
            (self.run_id, "run_id"),
            (self.batch_id, "batch_id"),
        ):
            require_text(value, name)
        if self.eligibility.base_arm_ids != self.arm_snapshot.arm_ids:
            raise ValueError("frozen eligibility does not cover the arm snapshot")


class DirectionApplicabilityBuilder(Protocol):
    """Build repeat-comparability coordinates after D1 chooses the parent."""

    def __call__(
        self, state: SearchState, parent_selection: ParentSelection,
    ) -> DirectionApplicability:
        ...


class FailureCardRetriever(Protocol):
    """Read bounded failures from the batch-frozen carrier-neutral history."""

    def retrieve(
        self, *, state: SearchState, selected_parent_id: str
    ) -> FailureCardSelection:
        ...


@dataclass(frozen=True)
class SlotGenerationAttempt:
    """Auditable proposer/gate result, including rejected repair attempts."""

    request: StructuredProposalRequest
    proposal: StructuredProposal | None
    semantic_decision: SemanticGateDecision | None
    adherence: SlotAdherence

    @property
    def selectable(self) -> bool:
        return self.semantic_decision is not None and self.adherence.adherent

    def as_trace(self) -> dict[str, object]:
        decision = self.semantic_decision
        arm_spec = self.proposal.arm_spec if self.proposal else self.request.arm_spec
        return {
            "request_id": self.request.request_id,
            "proposal_id": self.proposal.proposal_id if self.proposal else None,
            "direction_id": (
                self.proposal.direction.direction_id if self.proposal else None
            ),
            "source_slot_id": self.request.source_slot.slot_id,
            "repair_feedback": self.request.repair_feedback,
            "status": self.adherence.status.value,
            "reason": self.adherence.reason,
            "match_kind": decision.match.kind.value if decision else None,
            "matched_arm_id": decision.match.arm_id if decision else None,
            "provider": self.proposal.provider if self.proposal else None,
            "model": self.proposal.model if self.proposal else None,
            "arm_spec": _arm_spec_payload(arm_spec),
            "context_contract": UCB_GENERATION_CONTEXT_CONTRACT,
        }


@dataclass(frozen=True)
class UCBPlanResult:
    """Complete plan plus the durable semantic reservation needed for resume."""

    plan: PolicyDecisionPlan
    semantic_reservation: PendingSemanticReservation
    reserved_direction: ReservedDirection
    arm_snapshot: ArmRegistrySnapshot
    eligibility: ArmEligibilityReport
    evidence_selection: FixedEvidenceSelection
    failure_selection: FailureCardSelection
    generation_attempts: tuple[SlotGenerationAttempt, ...]

    def __post_init__(self) -> None:
        reservation = self.semantic_reservation.reservation
        action = self.plan.action
        if self.reserved_direction.semantic_reservation != self.semantic_reservation:
            raise ValueError("reserved direction and semantic reservation differ")
        if (
            reservation.action_id != action.action_id
            or reservation.batch_id != action.batch_id
            or reservation.decision_round_id != action.decision_round_id
            or reservation.arm_id != action.arm_id
            or reservation.direction_id != action.direction.direction_id
            or reservation.source_slot_kind is not action.source_slot.kind
        ):
            raise ValueError(
                "semantic reservation and selected action coordinates differ"
            )


class UCBPolicyCarrier:
    """Plan one action with Parent UCB, ArmAllocator and Direction UCB."""

    def __init__(
        self,
        *,
        parent_policy: ParentUCB,
        arm_allocator: ArmAllocator,
        direction_policy: DirectionUCB,
        proposer: StructuredProposer,
        semantic_gate: DirectionSemanticGate,
        arm_registry: InnovationArmRegistry,
        eligibility_controller: ArmEligibilityController,
        applicability_builder: DirectionApplicabilityBuilder,
        evidence_retriever: FixedEvidenceRetriever,
        failure_card_retriever: FailureCardRetriever,
        repository_map_provider: RepositoryMapProvider,
        config: UCBPlannerConfig,
        visible_a_evidence_provider: VisibleAEvidenceProvider | None = None,
    ) -> None:
        if arm_allocator.estimator is not direction_policy.estimator:
            raise ValueError("D2 allocator and D3 must share the Direction estimator")
        if not callable(getattr(proposer, "propose", None)):
            raise TypeError("proposer must implement StructuredProposer")
        if not callable(applicability_builder):
            raise TypeError("applicability_builder must be callable")
        if not isinstance(evidence_retriever, FixedEvidenceRetriever):
            raise TypeError("evidence_retriever must be FixedEvidenceRetriever")
        if not callable(getattr(failure_card_retriever, "retrieve", None)):
            raise TypeError("failure_card_retriever must provide retrieve()")
        if not callable(getattr(repository_map_provider, "get", None)):
            raise TypeError("repository_map_provider must provide get()")
        if visible_a_evidence_provider is not None and not callable(
            getattr(visible_a_evidence_provider, "get", None)
        ):
            raise TypeError("visible_a_evidence_provider must provide get()")
        self.parent_policy = parent_policy
        self.arm_allocator = arm_allocator
        self.direction_policy = direction_policy
        self.proposer = proposer
        self.semantic_gate = semantic_gate
        self.arm_registry = arm_registry
        self.eligibility_controller = eligibility_controller
        self.applicability_builder = applicability_builder
        self.evidence_retriever = evidence_retriever
        self.failure_card_retriever = failure_card_retriever
        self.repository_map_provider = repository_map_provider
        self.visible_a_evidence_provider = visible_a_evidence_provider
        self.config = config

    @staticmethod
    def _derived_seed(seed: int, *coordinates: object) -> int:
        return derive_ucb_seed(seed, *coordinates)

    @staticmethod
    def _arm_spec(arm: InnovationArm) -> ArmSpec:
        return arm.arm_spec

    @staticmethod
    def _arm_summary(arm: InnovationArm) -> dict[str, object]:
        payload = _arm_spec_payload(UCBPolicyCarrier._arm_spec(arm))
        assert payload is not None
        return {
            **payload,
            "anchor_realization": UCBPolicyCarrier._direction_summary(
                arm.anchor_direction
            ),
        }

    @staticmethod
    def _direction_summary(direction: StructuredDirection) -> dict[str, object]:
        return {
            "implementation_direction": direction.implementation_direction[:1_024],
            "declared_scope": [
                {
                    "kind": target.kind.value,
                    "path": target.path,
                    "symbols": list(target.symbols),
                }
                for target in direction.declared_scope
            ],
            "new_paths": [
                {"path": target.path, "role": target.role}
                for target in direction.new_paths
            ],
            "resolved_scope": list(direction.resolved_scope),
        }

    def _executed_realization_summaries(self, arm_id: str) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        members = sorted(
            (
                item
                for item in self.semantic_gate.members.all()
                if item.arm_id == arm_id and item.repeat_blocking
            ),
            key=lambda item: (item.dispatched_at, item.member_id),
            reverse=True,
        )[: self.config.discover_exclusion_limit]
        for member in members:
            decoded = decode_semantic_record(
                canonical_input=member.canonical_input,
                embedding=member.embedding,
                embedding_identity=member.embedding_identity,
            )
            try:
                payload = json.loads(decoded.realization.canonical_input)
            except (TypeError, ValueError) as exc:
                raise ValueError("executed realization identity is not JSON") from exc
            implementation = payload.get("implementation_direction")
            resolved_scope = payload.get("resolved_scope")
            new_paths = payload.get("new_paths")
            if (
                not isinstance(implementation, str)
                or not isinstance(resolved_scope, list)
                or not isinstance(new_paths, list)
            ):
                raise ValueError("executed realization identity has invalid shape")
            rows.append(
                {
                    "implementation_direction": implementation[:1_024],
                    "resolved_scope": resolved_scope,
                    "new_paths": new_paths,
                }
            )
        return rows

    def _repair_feedback(
        self,
        adherence: SlotAdherence,
        arm_by_id: Mapping[str, InnovationArm],
        *,
        slot: ArmSlot,
        proposal: StructuredProposal | None,
    ) -> str:
        feedback: dict[str, object] = {
            "rejection": adherence.status.value,
            "reason": adherence.reason,
            "instruction": (
                "keep the fixed D2 family but materially change the D3 mechanism "
                "or target family; do not paraphrase the rejected realization"
                if slot.kind.value == "existing"
                else "change the D2 failure family and D3 realization so neither "
                "collides with an existing direction; do not paraphrase"
            ),
        }
        if proposal is not None:
            feedback["rejected_direction"] = {
                "failure_mechanism": proposal.direction.failure_mechanism,
                **self._direction_summary(proposal.direction),
            }
        # Naming the arm that was matched is the difference between feedback the
        # proposer can act on and a retry that collides with the same arm again.
        matched = arm_by_id.get(adherence.matched_arm_id or "")
        if matched is not None:
            feedback["collided_with"] = {
                "arm_id": matched.arm_id,
                **self._arm_summary(matched),
            }
        return canonical_json(feedback)

    def _generation_context(
        self,
        source_arm: InnovationArm | None,
        *,
        existing_arms: Sequence[InnovationArm],
        prior_rejected_directions: Sequence[Mapping[str, object]],
    ) -> str:
        rejected = [dict(item) for item in prior_rejected_directions]
        if source_arm is not None:
            if not isinstance(source_arm, InnovationArm):
                raise TypeError("source arm must be an InnovationArm")
            return canonical_json(
                {
                    "context_contract": UCB_GENERATION_CONTEXT_CONTRACT,
                    "fixed_d2_arm_spec": _arm_spec_payload(self._arm_spec(source_arm)),
                    "avoid_these_executed_realizations": (
                        self._executed_realization_summaries(source_arm.arm_id)
                        or [self._direction_summary(source_arm.anchor_direction)]
                    ),
                    "avoid_prior_rejected_directions": rejected,
                }
            )
        # A discover slot is rejected for colliding with an arm that already
        # exists, so it has to be told which arms those are. Without the
        # exclusion set the semantic gate is asking for something the proposer
        # cannot see, and a retry can only collide again.
        newest = sorted(
            existing_arms, key=lambda arm: (arm.created_at, arm.arm_id), reverse=True,
        )[: self.config.discover_exclusion_limit]
        return canonical_json(
            {
                "context_contract": UCB_GENERATION_CONTEXT_CONTRACT,
                "avoid_these_existing_directions": [
                    self._arm_summary(arm)
                    for arm in sorted(newest, key=lambda arm: arm.arm_id)
                ],
                "excluded_arm_count": len(existing_arms),
                "avoid_prior_rejected_directions": rejected,
            }
        )

    def restore_pending_semantics(
        self, records: Sequence[PendingSemanticReservation],
    ) -> None:
        """Restore a durable DRAFTING overlay exactly before planning resumes."""

        ordered = tuple(
            sorted(
                records,
                key=lambda item: (
                    item.reservation.created_at,
                    item.reservation.reservation_id,
                ),
            )
        )
        current = self.semantic_gate.pending
        # Durable persistence is the resume authority.  Replacing the whole
        # overlay also removes a process-local reservation created just before
        # a crash but never committed by the BatchLoop unit of work.
        self.semantic_gate.pending = PendingArmIdentityOverlay(
            batch_id=current.batch_id,
            embedding_identity=current.embedding_identity,
            threshold_id=current.threshold_id,
            reservations=ordered,
        )

    def begin_batch(self, state: SearchState) -> UCBFrozenSnapshot:
        arm_snapshot = self.arm_registry.snapshot(
            compatibility=state.compatibility, cutoff=state.cutoff,
        )
        return UCBFrozenSnapshot(
            state_id=state.state_id,
            run_id=state.run_id,
            batch_id=state.batch_id,
            arm_snapshot=arm_snapshot,
            eligibility=self.eligibility_controller.report(
                arm_snapshot, base_arm_ids=arm_snapshot.arm_ids,
            ),
        )

    def plan(
        self,
        *,
        state: SearchState,
        carrier_snapshot: UCBFrozenSnapshot,
        parent_slate: ParentSlate,
        eligible_arm_ids: tuple[str, ...],
        evidence_records: Sequence[ProgramEvidence],
        carrier_context: str,
        attempt_id: str,
        action_id: str,
        seed: int,
        planned_at: datetime,
        prior_rejected_directions: Sequence[Mapping[str, object]] = (),
    ) -> UCBPlanResult:
        require_text(attempt_id, "attempt_id")
        require_text(action_id, "action_id")
        require_aware(planned_at, "planned_at")
        if planned_at <= state.cutoff.available_at:
            raise ValueError(
                "planning records must be strictly after the frozen cutoff"
            )
        if parent_slate.state_id != state.state_id:
            raise ValueError("parent slate belongs to another search state")
        if self.semantic_gate.pending.batch_id != state.batch_id:
            raise ValueError("semantic pending overlay belongs to another batch")
        if (
            carrier_snapshot.state_id != state.state_id
            or carrier_snapshot.run_id != state.run_id
            or carrier_snapshot.batch_id != state.batch_id
            or carrier_snapshot.arm_snapshot.compatibility != state.compatibility
            or carrier_snapshot.arm_snapshot.cutoff != state.cutoff
        ):
            raise ValueError("UCB carrier snapshot belongs to another frozen state")
        arm_snapshot = carrier_snapshot.arm_snapshot
        if len(eligible_arm_ids) != len(set(eligible_arm_ids)):
            raise ValueError("eligible arm ids must be unique")
        frozen_ids = set(arm_snapshot.arm_ids)
        unknown = set(eligible_arm_ids) - frozen_ids
        if unknown:
            raise ValueError("eligible arms must belong to the frozen arm snapshot")
        anchor_ids = {
            item.arm_id
            for item in self.semantic_gate.anchors.all()
            if item.compatibility == state.compatibility
        }
        if not set(eligible_arm_ids) <= anchor_ids:
            raise ValueError("eligible arm lacks an authoritative semantic anchor")
        frozen_eligible = set(carrier_snapshot.eligibility.eligible_arm_ids)
        eligibility = ArmEligibilityReport(
            compatibility=state.compatibility,
            opportunity_seq=carrier_snapshot.eligibility.opportunity_seq,
            base_arm_ids=eligible_arm_ids,
            eligible_arm_ids=tuple(
                item for item in eligible_arm_ids if item in frozen_eligible
            ),
            exclusions=tuple(
                item
                for item in carrier_snapshot.eligibility.exclusions
                if item.arm_id in eligible_arm_ids
            ),
        )

        parent_selection = self.parent_policy.select(parent_slate, state, seed=seed)
        repository_map = self.repository_map_provider.get(
            state=state, parent=parent_selection
        )
        visible_a_evidence = (
            ()
            if self.visible_a_evidence_provider is None
            else tuple(self.visible_a_evidence_provider.get(state=state))
        )
        applicability = self.applicability_builder(state, parent_selection)
        if not isinstance(applicability, DirectionApplicability):
            raise TypeError("applicability_builder returned the wrong type")
        if applicability.parent_id != parent_selection.parent_id:
            raise ValueError("applicability must describe the selected parent")
        evidence_selection = self.evidence_retriever.retrieve(
            state=state,
            selected_parent_id=parent_selection.parent_id,
            records=evidence_records,
        )
        failure_selection = self.failure_card_retriever.retrieve(
            state=state, selected_parent_id=parent_selection.parent_id,
        )
        if failure_selection.selected_parent_id != parent_selection.parent_id:
            raise ValueError("failure cards belong to another selected parent")
        if len(failure_selection.cards) > 3:
            raise ValueError("failure-card retrieval exceeded its fixed bound")

        round_id = parent_slate.decision_round_id
        allocation = self.arm_allocator.allocate(
            state=state,
            parent_id=parent_selection.parent_id,
            decision_round_id=round_id,
            eligible_arm_ids=eligibility.eligible_arm_ids,
            cold_start=not arm_snapshot.arm_ids,
            generation_capacity=self.config.generation_capacity,
            discover_quota=self.config.discover_quota,
            seed=self._derived_seed(seed, "d2"),
        )

        arm_by_id = {item.arm_id: item for item in arm_snapshot.arms}
        attempts: list[SlotGenerationAttempt] = []
        candidates: list[DirectionCandidate] = []
        decisions: dict[str, SemanticGateDecision] = {}
        candidate_arm_specs: dict[str, ArmSpec] = {}
        accepted_realizations: list[tuple[str, SemanticGateDecision]] = []
        for slot in allocation.slots:
            source_arm = (
                arm_by_id[slot.source_arm_id]
                if slot.source_arm_id is not None
                else None
            )
            source_arm_spec = (
                self._arm_spec(source_arm) if source_arm is not None else None
            )
            repair_feedback = None
            for repair_ordinal in range(self.config.max_slot_generation_attempts):
                deferred = False
                request = StructuredProposalRequest(
                    request_id=stable_id(
                        "ucb-proposal-request", round_id, slot.slot_id, repair_ordinal,
                    ),
                    method=PolicyMethod.UCB,
                    state=state,
                    parent_selection=parent_selection,
                    decision_round_id=round_id,
                    candidate_ordinal=slot.ordinal,
                    max_scope_refs=self.config.max_scope_refs,
                    max_new_paths=self.config.max_new_paths,
                    max_resolved_scope_files=self.config.max_resolved_scope_files,
                    max_resolved_scope_bytes=self.config.max_resolved_scope_bytes,
                    carrier_context=self._generation_context(
                        source_arm,
                        existing_arms=arm_snapshot.arms,
                        prior_rejected_directions=prior_rejected_directions,
                    ),
                    repository_map=repository_map,
                    environment_context=carrier_context,
                    evidence_cards=evidence_selection.cards,
                    failure_cards=failure_selection.cards,
                    visible_a_evidence=visible_a_evidence,
                    source_slot=slot,
                    arm_spec=source_arm_spec,
                    repair_feedback=repair_feedback,
                    metadata={
                        "allocation_id": allocation.allocation_id,
                        "generation_context_contract": (
                            UCB_GENERATION_CONTEXT_CONTRACT
                        ),
                        "repair_ordinal": repair_ordinal,
                        "prior_rejection_count": len(prior_rejected_directions),
                        "prior_slate_directions": [
                            {
                                "failure_mechanism": item.direction.failure_mechanism[:200],
                                "implementation_direction": item.direction.implementation_direction[:400],
                            }
                            for item in candidates[-4:]
                        ],
                    },
                )
                try:
                    proposal = self.proposer.propose(request)
                    if proposal.request_id != request.request_id:
                        raise ValueError("proposer response references another request")
                    if request.arm_spec is not None:
                        if proposal.arm_spec not in (None, request.arm_spec):
                            raise RetryableProposalError(
                                "proposer changed the fixed D2 arm spec"
                            )
                        proposal_arm_spec = request.arm_spec
                    else:
                        proposal_arm_spec = proposal.arm_spec or ArmSpec(
                            failure_mechanism=proposal.direction.failure_mechanism
                        )
                        if (
                            proposal_arm_spec.failure_mechanism
                            != proposal.direction.failure_mechanism
                        ):
                            raise RetryableProposalError(
                                "DISCOVER proposal D2 differs from its direction"
                            )
                    if proposal.arm_spec is not proposal_arm_spec:
                        proposal = replace(proposal, arm_spec=proposal_arm_spec)
                    validate_direction_targets(proposal.direction, repository_map)
                    decision = self.semantic_gate.evaluate(
                        direction=proposal.direction,
                        arm_spec=proposal.arm_spec,
                        source_slot=slot,
                        compatibility=state.compatibility,
                        applicability=applicability,
                    )
                    adherence = decision.adherence
                    if adherence.adherent and accepted_realizations:
                        same_slate_neighbors = sorted(
                            (
                                (
                                    cosine_distance(
                                        decision.encoded.embedding,
                                        accepted.encoded.embedding,
                                    ),
                                    candidate_id,
                                )
                                for candidate_id, accepted in accepted_realizations
                            ),
                            key=lambda item: (item[0], item[1]),
                        )
                        distance, neighbor_id = same_slate_neighbors[0]
                        thresholds = self.semantic_gate.thresholds
                        if distance <= (
                            thresholds.tau_repeat + thresholds.threshold_boundary_margin
                        ):
                            boundary = thresholds.is_boundary_ambiguous(distance)
                            adherence = generation_failed(
                                slot,
                                (
                                    "ambiguous same-slate D3 repeat boundary"
                                    if boundary
                                    else "near-repeat of an accepted same-slate D3"
                                )
                                + f" candidate {neighbor_id} at distance {distance:.12g}",
                            )
                except ProposerDeferred as exc:
                    proposal = None
                    decision = None
                    adherence = generation_failed(slot, str(exc))
                    deferred = True
                except RetryableProposalError as exc:
                    proposal = None
                    decision = None
                    # Keep the violation detail: the repair prompt must name
                    # the offending targets, not only the exception class.
                    detail = " ".join(str(exc).split())[:1_024]
                    adherence = generation_failed(
                        slot,
                        f"retryable-proposal-error:{type(exc).__name__}"
                        + (f": {detail}" if detail else ""),
                    )
                attempt = SlotGenerationAttempt(
                    request=request,
                    proposal=proposal,
                    semantic_decision=decision,
                    adherence=adherence,
                )
                attempts.append(attempt)
                if deferred:
                    # Evidence is missing, not malformed: move to another slot
                    # without repeating this request or creating a task reward.
                    break
                if attempt.selectable:
                    assert proposal is not None
                    assert decision is not None
                    candidate_id = stable_id(
                        "direction-candidate", proposal.proposal_id, slot.slot_id
                    )
                    candidate = DirectionCandidate(
                        candidate_id=candidate_id,
                        source_slot=slot,
                        direction=proposal.direction,
                        match=decision.match,
                        adherence=decision.adherence,
                    )
                    candidates.append(candidate)
                    decisions[candidate_id] = decision
                    assert proposal.arm_spec is not None
                    candidate_arm_specs[candidate_id] = proposal.arm_spec
                    accepted_realizations.append((candidate_id, decision))
                    break
                repair_feedback = self._repair_feedback(
                    adherence, arm_by_id, slot=slot, proposal=proposal,
                )

        direction_slate = DirectionSlate(
            slate_id=stable_id(
                "direction-slate",
                allocation.allocation_id,
                tuple(item.candidate_id for item in candidates),
            ),
            decision_round_id=round_id,
            state_id=state.state_id,
            parent_id=parent_selection.parent_id,
            candidates=tuple(candidates),
        )
        try:
            direction_selection = self.direction_policy.select(
                direction_slate, state, seed=self._derived_seed(seed, "d3"),
            )
        except NoValidDirection as exc:
            raise NoValidDirection(str(exc), trace=tuple(attempts)) from exc

        selected = next(
            item
            for item in direction_slate.candidates
            if item.candidate_id == direction_selection.candidate_id
        )
        selected_decision = decisions[selected.candidate_id]
        implementation_evidence = next(
            item.proposal.metadata.get("implementation_evidence", {})
            for item in attempts
            if item.selectable and item.proposal is not None
            and item.proposal.direction.direction_id == selected.direction.direction_id
        )
        selected_arm_spec = candidate_arm_specs[selected.candidate_id]
        reserved = self.semantic_gate.reserve_selected(
            decision=selected_decision,
            direction=selected.direction,
            arm_spec=selected_arm_spec,
            source_slot=selected.source_slot,
            compatibility=state.compatibility,
            applicability=applicability,
            attempt_id=attempt_id,
            action_id=action_id,
            decision_round_id=round_id,
            created_at=planned_at,
        )
        reservation = reserved.semantic_reservation.reservation
        parent_candidate = next(
            item
            for item in parent_slate.candidates
            if item.parent_id == parent_selection.parent_id
        )
        d1 = parent_selection.trace
        action = PolicyAction(
            action_id=action_id,
            method=PolicyMethod.UCB,
            run_id=state.run_id,
            batch_id=state.batch_id,
            decision_round_id=round_id,
            state_id=state.state_id,
            parent_id=parent_selection.parent_id,
            parent_commit_sha=parent_candidate.commit_sha,
            direction=selected.direction,
            source_slot=selected.source_slot,
            arm_id=reservation.arm_id,
            direction_match=reserved.decision.match,
            created_at=planned_at,
            d1_trace={
                "selection_id": parent_selection.selection_id,
                "selected_id": d1.selected_id,
                "eligible_ids": list(d1.eligible_ids),
                "tie_ids": list(d1.tie_ids),
                "seed": d1.seed,
                "conditional_probability": d1.conditional_probability,
                "indices": dict(d1.indices),
            },
            d2_trace={
                **({"implementation_evidence": implementation_evidence} if implementation_evidence else {}),
                "allocation_id": allocation.allocation_id,
                "context_contract": UCB_GENERATION_CONTEXT_CONTRACT,
                "selected_arm_spec": _arm_spec_payload(selected_arm_spec),
                "allocation": dict(allocation.trace),
                "failure_history": {
                    "retriever_id": failure_selection.retriever_id,
                    "cards": [
                        card.as_prompt_payload() for card in failure_selection.cards
                    ],
                },
                "generation_attempts": [item.as_trace() for item in attempts],
            },
            d3_trace=dict(direction_selection.trace),
        )
        plan = PolicyDecisionPlan(
            plan_id=stable_id("ucb-policy-plan", action_id),
            method=PolicyMethod.UCB,
            parent_slate=parent_slate,
            parent_selection=parent_selection,
            arm_allocation=allocation,
            direction_slate=direction_slate,
            direction_selection=direction_selection,
            action=action,
        )
        return UCBPlanResult(
            plan=plan,
            semantic_reservation=reserved.semantic_reservation,
            reserved_direction=reserved,
            arm_snapshot=arm_snapshot,
            eligibility=eligibility,
            evidence_selection=evidence_selection,
            failure_selection=failure_selection,
            generation_attempts=tuple(attempts),
        )


__all__ = [
    "SlotGenerationAttempt",
    "DirectionApplicabilityBuilder",
    "FailureCardRetriever",
    "UCBPlanResult",
    "UCBFrozenSnapshot",
    "UCBPlannerConfig",
    "UCBPolicyCarrier",
]
