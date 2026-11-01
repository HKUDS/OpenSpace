"""Barrier-only projection of durable UCB actions and feedback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from agentpilot.domain.common import stable_id
from agentpilot.domain.evaluation import PolicyObservation
from agentpilot.domain.policy import (
    ArmSpec,
    DirectionMatchKind,
    PlanningFailureEvent,
    PolicyDecisionPlan,
    PolicyMethod,
    ReservationState,
    SlotAdherenceStatus,
    SlotKind,
    StructuredDirection,
)
from agentpilot.domain.realization import WorkerResult
from agentpilot.domain.search import EvidenceCutoff, SearchState
from agentpilot.policy.ucb.arm_allocator import ArmAllocator
from agentpilot.policy.ucb.arms import InnovationArm, InnovationArmRegistry
from agentpilot.policy.ucb.cooldown import ArmEligibilityController
from agentpilot.policy.ucb.direction import DirectionUCB
from agentpilot.policy.ucb.observations import (
    ArmCreationOpportunity,
    BernoulliObservation,
    DirectionOpportunity,
    ParentOpportunity,
    SelectedActionRecord,
    UCBHistory,
    UCBTarget,
    observations_from_policy,
)
from agentpilot.policy.ucb.parent import ParentUCB
from agentpilot.search.direction_semantic_index import (
    ArmAnchor,
    ArmAnchorIndex,
    ExecutedDirectionMember,
    ExecutedDirectionMemberIndex,
    FrozenDirectionEncoder,
    MemberEvidenceStatus,
    PendingSemanticReservation,
    canonical_arm_input,
    canonical_realization_input,
    decode_semantic_record,
)


class BarrierExecutionResult(Protocol):
    worker: WorkerResult
    outcome: object


class UCBBarrierBatch(Protocol):
    update_id: str
    state: SearchState
    carrier_snapshot: Mapping[str, Any]
    plans: tuple[PolicyDecisionPlan, ...]
    semantic_reservations: tuple[PendingSemanticReservation, ...]
    execution_results: tuple[BarrierExecutionResult, ...]
    observations: tuple[PolicyObservation, ...]


@dataclass(frozen=True)
class _Projection:
    parent_opportunity: ParentOpportunity
    direction_opportunity: DirectionOpportunity
    selected_action: SelectedActionRecord
    creation_transfer: ArmCreationOpportunity | None
    parent_observations: tuple[BernoulliObservation, ...]
    direction_observations: tuple[BernoulliObservation, ...]
    anchor: ArmAnchor | None
    arm: InnovationArm | None
    member: ExecutedDirectionMember


class UCBPolicyUpdater:
    """Apply a complete durable batch once, after the execution barrier.

    Parent and Direction histories are intentionally separate.  D2 and D3
    consume the latter; the parent learner may use different neighborhoods
    and formal parameters.  All conflicts are preflighted before append so
    the reference in-memory implementation has transaction-like behavior.
    """

    def __init__(
        self,
        *,
        parent_history: UCBHistory,
        direction_history: UCBHistory,
        arm_registry: InnovationArmRegistry,
        eligibility_controller: ArmEligibilityController,
        anchors: ArmAnchorIndex,
        members: ExecutedDirectionMemberIndex,
        encoder: FrozenDirectionEncoder,
    ) -> None:
        if parent_history is direction_history:
            raise ValueError("Parent and Direction UCB histories must be separate")
        if not isinstance(encoder, FrozenDirectionEncoder):
            raise TypeError("UCB updater requires a FrozenDirectionEncoder")
        if (
            anchors.embedding_identity != encoder.identity
            or members.embedding_identity != encoder.identity
        ):
            raise ValueError("UCB updater indexes and encoder identities differ")
        self.parent_history = parent_history
        self.direction_history = direction_history
        self.arm_registry = arm_registry
        self.eligibility_controller = eligibility_controller
        self.anchors = anchors
        self.members = members
        self.encoder = encoder
        self._applied: dict[str, UCBBarrierBatch] = {}
        self._applied_planning_failures: dict[str, PlanningFailureEvent] = {}

    def record_planning_failure(self, event: PlanningFailureEvent) -> None:
        """Project a no-action failure only into generation eligibility.

        This path intentionally never calls either UCB history and therefore
        cannot create a Bernoulli observation, Parent opportunity, Direction
        opportunity, selected-action row, member, anchor, or arm.
        """

        if event.method is not PolicyMethod.UCB:
            raise ValueError("UCB updater cannot consume another carrier failure")
        previous = self._applied_planning_failures.get(event.event_id)
        if previous is not None:
            if previous != event:
                raise ValueError("planning failure replay has immutable drift")
            return
        generation: dict[str, bool] = {}
        for attempt in event.slot_attempts:
            if attempt.source_slot_kind is not SlotKind.EXISTING:
                continue
            arm_id = attempt.source_arm_id
            if arm_id is None:
                raise ValueError("existing planning attempt has no source arm")
            generation[arm_id] = generation.get(arm_id, False) or (
                attempt.status is SlotAdherenceStatus.ADHERENT
            )
        if any(generation.values()):
            raise ValueError(
                "no-valid-direction event contains a successful existing slot"
            )
        self.eligibility_controller.record_round(
            decision_round_id=event.decision_round_id,
            compatibility=event.compatibility,
            generation_succeeded=generation,
            selected_arm_id=None,
            retryable_skip=False,
            replay_key=event.event_id,
        )
        self._applied_planning_failures[event.event_id] = event

    def replay_planning_failures(
        self, events: tuple[PlanningFailureEvent, ...]
    ) -> None:
        for event in events:
            self.record_planning_failure(event)

    def update(self, batch: UCBBarrierBatch) -> None:
        expected_id = stable_id("carrier-update", batch.state.batch_id)
        if batch.update_id != expected_id:
            raise ValueError("UCB update_id is not stable for the batch")
        previous = self._applied.get(batch.update_id)
        if previous is not None:
            if previous != batch:
                raise ValueError("UCB update replay changed immutable batch input")
            return
        projections = self._project(batch)
        self._preflight(projections)
        for item in projections:
            self.parent_history.record_parent_opportunity(item.parent_opportunity)
            self.parent_history.record_selected_action(item.selected_action)
            self.parent_history.record_observations(item.parent_observations)

            self.direction_history.record_direction_opportunity(
                item.direction_opportunity
            )
            self.direction_history.record_selected_action(item.selected_action)
            if item.creation_transfer is not None:
                self.direction_history.record_creation_transfer(item.creation_transfer)
            self.direction_history.record_observations(item.direction_observations)

            if item.anchor is not None:
                self.anchors.add(item.anchor)
                assert item.arm is not None
                self.arm_registry.register_existing(item.arm)
            self.members.add(item.member)
        for plan, item in zip(batch.plans, projections):
            assert plan.arm_allocation is not None
            successful_slots = {
                candidate.source_slot.slot_id
                for candidate in plan.direction_slate.candidates
                if candidate.source_slot is not None
            }
            generation = {
                slot.source_arm_id: slot.slot_id in successful_slots
                for slot in plan.arm_allocation.slots
                if slot.kind is SlotKind.EXISTING and slot.source_arm_id is not None
            }
            self.eligibility_controller.record_round(
                decision_round_id=plan.action.decision_round_id,
                compatibility=batch.state.compatibility,
                generation_succeeded=generation,
                selected_arm_id=plan.action.arm_id,
                retryable_skip=not (
                    item.parent_observations or item.direction_observations
                ),
            )
        self._applied[batch.update_id] = batch

    def replay(self, batches: tuple[UCBBarrierBatch, ...]) -> None:
        """Rebuild a fresh in-memory learner from durable completed events."""

        self.reset_projection()
        for batch in batches:
            self.update(batch)

    def reset_projection(self) -> None:
        """Discard all rebuildable UCB state before a cutoff-scoped replay."""

        self.parent_history.reset_projection()
        self.direction_history.reset_projection()
        self.arm_registry.reset_projection()
        self.eligibility_controller.reset_projection()
        self.anchors.reset_projection()
        self.members.reset_projection()
        self._applied.clear()
        self._applied_planning_failures.clear()

    def _project(self, batch: UCBBarrierBatch) -> tuple[_Projection, ...]:
        if not batch.plans:
            raise ValueError("UCB barrier update cannot be empty")
        lengths = {
            len(batch.plans),
            len(batch.semantic_reservations),
            len(batch.execution_results),
            len(batch.observations),
        }
        if len(lengths) != 1:
            raise ValueError("UCB barrier inputs are not one-to-one")
        rounds = [item.action.decision_round_id for item in batch.plans]
        if len(rounds) != len(set(rounds)):
            raise ValueError("UCB barrier contains duplicate decision rounds")

        result = []
        for plan, semantic, execution, observation in zip(
            batch.plans,
            batch.semantic_reservations,
            batch.execution_results,
            batch.observations,
        ):
            result.append(
                self._project_one(
                    state=batch.state,
                    plan=plan,
                    semantic=semantic,
                    worker=execution.worker,
                    observation=observation,
                )
            )
        new_arm_ids = {item.anchor.arm_id for item in result if item.anchor is not None}
        frozen_arm_ids = {item.arm_id for item in self.anchors.all()}
        for plan in batch.plans:
            action = plan.action
            assert action.direction_match is not None
            if action.direction_match.kind is DirectionMatchKind.PENDING_NEW:
                if action.arm_id not in new_arm_ids:
                    raise ValueError("pending-new action lacks its batch creator")
            elif action.direction_match.kind is DirectionMatchKind.EXISTING:
                if action.arm_id not in frozen_arm_ids:
                    raise ValueError("existing action lacks a frozen arm anchor")
        return tuple(result)

    def _project_one(
        self,
        *,
        state: SearchState,
        plan: PolicyDecisionPlan,
        semantic: PendingSemanticReservation,
        worker: WorkerResult,
        observation: PolicyObservation,
    ) -> _Projection:
        action = plan.action
        reservation = semantic.reservation
        if plan.method is not PolicyMethod.UCB or action.method is not PolicyMethod.UCB:
            raise ValueError("UCB updater received a non-UCB plan")
        if plan.arm_allocation is None or action.source_slot is None:
            raise ValueError("UCB barrier plan lacks its typed allocation/slot")
        if action.direction_match is None or action.arm_id is None:
            raise ValueError("UCB barrier action lacks its final semantic identity")
        if reservation.state is not ReservationState.COMPLETED:
            raise ValueError("UCB update requires a completed reservation")
        if reservation.dispatched_at is None or reservation.completed_at is None:
            raise ValueError("completed reservation lacks execution timestamps")
        expected = (
            action.action_id,
            action.batch_id,
            action.decision_round_id,
            action.arm_id,
            action.direction.direction_id,
            action.source_slot.kind,
        )
        actual = (
            reservation.action_id,
            reservation.batch_id,
            reservation.decision_round_id,
            reservation.arm_id,
            reservation.direction_id,
            reservation.source_slot_kind,
        )
        if expected != actual:
            raise ValueError("barrier reservation and action coordinates differ")
        if semantic.establishes_new_arm is not (
            action.direction_match.kind is DirectionMatchKind.NEW
        ) or reservation.joined_existing_pending is not (
            action.direction_match.kind is DirectionMatchKind.PENDING_NEW
        ):
            raise ValueError("barrier reservation and final match semantics differ")
        if (
            worker.action_id != action.action_id
            or observation.action_id != action.action_id
            or observation.method is not PolicyMethod.UCB
            or observation.parent_id != action.parent_id
            or observation.arm_id != action.arm_id
            or observation.source_slot_kind is not action.source_slot.kind
            or observation.direction_match != action.direction_match
        ):
            raise ValueError("barrier feedback and selected action coordinates differ")
        if (
            semantic.compatibility != state.compatibility
            or observation.compatibility != state.compatibility
        ):
            raise ValueError("barrier semantic/evidence compatibility drift")
        if observation.available_at < reservation.completed_at:
            raise ValueError("policy observation precedes worker completion")

        available_at = reservation.dispatched_at
        parent_opportunity = ParentUCB.opportunity(
            plan.parent_selection, plan.parent_slate, state, available_at=available_at,
        )
        direction_opportunity = ArmAllocator.opportunity(
            plan.arm_allocation, state, available_at=available_at,
        )
        if (
            observation.parent_descriptor
            != parent_opportunity.selected_parent_descriptor
            or observation.direction_descriptor != direction_opportunity.descriptor
        ):
            raise ValueError("barrier observation descriptor drift")
        selected_action = DirectionUCB.selected_action_record(
            plan.direction_selection,
            plan.direction_slate,
            action_id=action.action_id,
            available_at=available_at,
        )
        creation_transfer = None
        if action.source_slot.kind is SlotKind.DISCOVER:
            creation_transfer = ArmCreationOpportunity(
                opportunity_id=stable_id("arm-creation-opportunity", action.action_id),
                decision_round_id=action.decision_round_id,
                action_id=action.action_id,
                arm_id=action.arm_id,
                compatibility=state.compatibility,
                descriptor=state.direction_descriptor,
                available_at=available_at,
            )

        projected = observations_from_policy(
            observation, decision_round_id=action.decision_round_id,
        )
        parent_rows = tuple(
            item for item in projected if item.target is UCBTarget.PARENT
        )
        direction_rows = tuple(
            item for item in projected if item.target is not UCBTarget.PARENT
        )
        semantic_input, realization_embedding = self._project_semantics(
            action.direction, semantic
        )
        attributable = bool(projected)
        member = ExecutedDirectionMember(
            member_id=stable_id("direction-member", action.action_id),
            arm_id=action.arm_id,
            action_id=action.action_id,
            batch_id=action.batch_id,
            direction_id=action.direction.direction_id,
            canonical_input=semantic_input,
            embedding=realization_embedding,
            embedding_identity=semantic.embedding_identity,
            threshold_id=semantic.threshold_id,
            compatibility=semantic.compatibility,
            applicability=semantic.applicability,
            evidence_status=(
                MemberEvidenceStatus.ATTRIBUTABLE
                if attributable
                else MemberEvidenceStatus.SKIP
            ),
            dispatched_at=reservation.dispatched_at,
            completed_at=observation.available_at,
            skip_reason="" if attributable else observation.effectiveness.reason,
        )

        anchor = None
        arm = None
        if semantic.establishes_new_arm:
            if action.direction_match.kind is not DirectionMatchKind.NEW:
                raise ValueError("new-arm semantic payload lacks a NEW final match")
            anchor = ArmAnchor(
                arm_id=action.arm_id,
                direction_id=action.direction.direction_id,
                source_action_id=action.action_id,
                canonical_input=semantic_input,
                embedding=realization_embedding,
                embedding_identity=semantic.embedding_identity,
                threshold_id=semantic.threshold_id,
                compatibility=semantic.compatibility,
                created_at=reservation.dispatched_at,
            )
            arm = InnovationArm.from_anchor(anchor, action.direction)
        return _Projection(
            parent_opportunity=parent_opportunity,
            direction_opportunity=direction_opportunity,
            selected_action=selected_action,
            creation_transfer=creation_transfer,
            parent_observations=parent_rows,
            direction_observations=direction_rows,
            anchor=anchor,
            arm=arm,
            member=member,
        )

    def _project_semantics(
        self,
        direction: StructuredDirection,
        semantic: PendingSemanticReservation,
    ) -> tuple[str, tuple[float, ...]]:
        """Validate the current dual-semantic record."""

        if semantic.embedding_identity != self.encoder.identity:
            raise ValueError("barrier semantic encoder identity drift")
        arm_spec = ArmSpec(failure_mechanism=direction.failure_mechanism)
        dual = decode_semantic_record(
            canonical_input=semantic.canonical_input,
            embedding=semantic.embedding,
            embedding_identity=semantic.embedding_identity,
        )
        if (
            dual.arm.canonical_input != canonical_arm_input(arm_spec)
            or dual.realization.canonical_input
            != canonical_realization_input(direction)
        ):
            raise ValueError("barrier dual semantic direction drift")
        return semantic.canonical_input, dual.realization.embedding

    def _preflight(self, projections: tuple[_Projection, ...]) -> None:
        self._preflight_history(
            self.parent_history,
            parents=tuple(item.parent_opportunity for item in projections),
            directions=(),
            selected=tuple(item.selected_action for item in projections),
            creations=(),
            observations=tuple(
                row for item in projections for row in item.parent_observations
            ),
        )
        self._preflight_history(
            self.direction_history,
            parents=(),
            directions=tuple(item.direction_opportunity for item in projections),
            selected=tuple(item.selected_action for item in projections),
            creations=tuple(
                item.creation_transfer
                for item in projections
                if item.creation_transfer is not None
            ),
            observations=tuple(
                row for item in projections for row in item.direction_observations
            ),
        )
        anchors = {item.arm_id: item for item in self.anchors.all()}
        anchor_actions = {item.source_action_id: item for item in anchors.values()}
        members = {item.member_id: item for item in self.members.all()}
        member_actions = {item.action_id: item for item in members.values()}
        arms = {item.arm_id: item for item in self.arm_registry.all()}
        arm_actions = {item.creation_action_id: item for item in arms.values()}
        for item in projections:
            if item.anchor is not None:
                self._exact_or_absent(
                    anchors.get(item.anchor.arm_id), item.anchor, "arm anchor"
                )
                self._exact_or_absent(
                    anchor_actions.get(item.anchor.source_action_id),
                    item.anchor,
                    "anchor action",
                )
                assert item.arm is not None
                self._exact_or_absent(
                    arms.get(item.arm.arm_id), item.arm, "innovation arm"
                )
                self._exact_or_absent(
                    arm_actions.get(item.arm.creation_action_id),
                    item.arm,
                    "innovation arm creation action",
                )
            self._exact_or_absent(
                members.get(item.member.member_id), item.member, "direction member"
            )
            self._exact_or_absent(
                member_actions.get(item.member.action_id), item.member, "member action",
            )

    @staticmethod
    def _exact_or_absent(current: object | None, proposed: object, label: str) -> None:
        if current is not None and current != proposed:
            raise ValueError(f"{label} replay has immutable drift")

    @classmethod
    def _preflight_history(
        cls,
        history: UCBHistory,
        *,
        parents: tuple[ParentOpportunity, ...],
        directions: tuple[DirectionOpportunity, ...],
        selected: tuple[SelectedActionRecord, ...],
        creations: tuple[ArmCreationOpportunity, ...],
        observations: tuple[BernoulliObservation, ...],
    ) -> None:
        for record in parents:
            cls._exact_or_absent(
                history.parent_opportunity(record.decision_round_id),
                record,
                "parent opportunity",
            )
        for record in directions:
            cls._exact_or_absent(
                history.direction_opportunity(record.decision_round_id),
                record,
                "direction opportunity",
            )
        for record in selected:
            cls._exact_or_absent(
                history.selected_action(record.decision_round_id),
                record,
                "selected action",
            )
        for record in creations:
            cls._exact_or_absent(
                history.creation_transfer(record.decision_round_id),
                record,
                "creation transfer",
            )
        cutoff = EvidenceCutoff(datetime.max.replace(tzinfo=timezone.utc))
        existing = history.observations(cutoff)
        by_id = {item.observation_id: item for item in existing}
        by_slot = {(item.target, item.decision_round_id): item for item in existing}
        for record in observations:
            cls._exact_or_absent(
                by_id.get(record.observation_id), record, "UCB observation"
            )
            cls._exact_or_absent(
                by_slot.get((record.target, record.decision_round_id)),
                record,
                "UCB observation slot",
            )


__all__ = ["UCBBarrierBatch", "UCBPolicyUpdater"]
