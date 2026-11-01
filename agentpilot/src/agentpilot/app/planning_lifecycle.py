"""Durable incremental planning lifecycle for one frozen batch."""

from __future__ import annotations

from dataclasses import replace

from agentpilot.app.contracts import (
    ActionPlanner,
    EvaluationPlanFreezer,
    PlannedAction,
)
from agentpilot.app.recovery import PlanningFailureRecoveryReader
from agentpilot.domain import (
    ArmSpec,
    DirectionMatchKind,
    PlanningFailureEvent,
    PlanningRoundFailed,
    PolicyDecisionPlan,
    PolicyMethod,
    ReservationState,
    SearchState,
    SlotKind,
    canonical_json,
)
from agentpilot.evaluation import FrozenEvaluationBatch
from agentpilot.persistence import BatchRecord, BatchStatus, UnitOfWorkFactory
from agentpilot.search.direction_semantic_index import (
    canonical_arm_input,
    canonical_realization_input,
    decode_semantic_record,
)


class BatchPlanningLifecycle:
    """Incrementally persist validated plans without mutating frozen inputs."""

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        planner: ActionPlanner,
        evaluation_plan_freezer: EvaluationPlanFreezer,
    ) -> None:
        self.unit_of_work = unit_of_work
        self.planner = planner
        self.evaluation_plan_freezer = evaluation_plan_freezer
        self.failure_reader = PlanningFailureRecoveryReader(unit_of_work)

    def finish(
        self, batch_id: str, state: SearchState, carrier_snapshot: object
    ) -> None:
        frozen_state = canonical_json(state)
        with self.unit_of_work() as work:
            batch = work.batches.get(batch_id)
            existing = work.plans.list_for_batch(batch_id)
        frozen_carrier = canonical_json(batch.carrier_snapshot)
        if len(existing) > batch.expected_rounds:
            raise RuntimeError("persisted plan count exceeds expected rounds")
        for round_index in range(len(existing), batch.expected_rounds):
            failures = tuple(
                event
                for event in self.failure_reader.list_for_batch(batch_id)
                if event.round_index == round_index
            )
            indices = tuple(event.planning_attempt_index for event in failures)
            if indices != tuple(range(len(indices))):
                raise RuntimeError(
                    "planning failure attempt sequence is not contiguous"
                )
            max_attempts = self.planner.max_planning_attempts
            item = None
            decision_round_id = ""
            for planning_attempt_index in range(len(failures), max_attempts):
                decision_round_id = f"{batch_id}:round:{round_index}"
                if planning_attempt_index:
                    decision_round_id += f":planning-attempt:{planning_attempt_index}"
                with self.unit_of_work() as work:
                    pending = work.semantic_reservations.list_for_batch(batch_id)
                try:
                    item = self.planner.plan_round(
                        state=state,
                        carrier_snapshot=carrier_snapshot,
                        decision_round_id=decision_round_id,
                        round_index=round_index,
                        planning_attempt_index=planning_attempt_index,
                        pending=pending,
                    )
                except PlanningRoundFailed as exc:
                    self._persist_planning_failure(
                        exc.event,
                        state=state,
                        batch=batch,
                        decision_round_id=decision_round_id,
                        round_index=round_index,
                        planning_attempt_index=planning_attempt_index,
                    )
                    continue
                break
            if item is None:
                self._mark_planning_exhausted(batch_id)
                return
            self._validate_plan(item, state, decision_round_id)
            if canonical_json(state) != frozen_state:
                raise RuntimeError("planner mutated the frozen SearchState")
            if (
                canonical_json(self.planner.freeze_snapshot(carrier_snapshot))
                != frozen_carrier
            ):
                raise RuntimeError("planner mutated the frozen carrier snapshot")
            with self.unit_of_work() as work:
                work.actions.add(item.action)
                work.plans.add(item.plan)
                if item.semantic_reservation is not None:
                    work.semantic_reservations.add(item.semantic_reservation)
                current = work.batches.get(batch_id)
                work.batches.replace(
                    replace(
                        current,
                        action_ids=(
                            *current.action_ids,
                            item.action.action_id,
                        ),
                    )
                )
        with self.unit_of_work() as work:
            current = work.batches.get(batch_id)
            actions = work.actions.list_for_batch(batch_id)
        if len(current.action_ids) != current.expected_rounds:
            raise RuntimeError(
                "batch planning did not produce one action per round"
            )
        if set(current.action_ids) != {action.action_id for action in actions}:
            raise RuntimeError("batch action records are incomplete before freeze")
        frozen_evaluation = self.evaluation_plan_freezer.freeze_batch(
            state=state,
            actions=actions,
        )
        self._validate_evaluation_freeze(
            frozen_evaluation,
            state=state,
            action_ids=current.action_ids,
        )
        # The prior, every per-action plan and the lifecycle transition commit
        # together.  A failure rolls back all of them, so dispatch can never
        # observe a partially frozen batch.
        with self.unit_of_work() as work:
            current = work.batches.get(batch_id)
            if current.status is not BatchStatus.DRAFTING:
                raise RuntimeError("evaluation plans can only freeze a draft batch")
            durable_action_ids = tuple(
                item.action_id for item in work.actions.list_for_batch(batch_id)
            )
            if len(durable_action_ids) != len(set(durable_action_ids)) or set(
                current.action_ids
            ) != set(durable_action_ids):
                raise RuntimeError("batch actions changed during evaluation freeze")
            work.evaluation_priors.add(frozen_evaluation.prior_snapshot)
            for plan in frozen_evaluation.action_plans:
                work.evaluation_plans.add(plan)
            work.batches.replace(replace(current, status=BatchStatus.FROZEN))

    def _validate_evaluation_freeze(
        self,
        frozen_evaluation: FrozenEvaluationBatch,
        *,
        state: SearchState,
        action_ids: tuple[str, ...],
    ) -> None:
        if not isinstance(frozen_evaluation, FrozenEvaluationBatch):
            raise TypeError(
                "evaluation plan freezer must return FrozenEvaluationBatch"
            )
        prior = frozen_evaluation.prior_snapshot
        if (
            prior.run_id,
            prior.batch_id,
            prior.state_id,
            prior.cutoff,
            prior.compatibility,
            prior.source_scope_id,
        ) != (
            state.run_id,
            state.batch_id,
            state.state_id,
            state.cutoff,
            state.compatibility,
            self.evaluation_plan_freezer.source_scope_id,
        ):
            raise ValueError(
                "evaluation prior differs from the frozen SearchState"
            )
        planned_ids = tuple(item.action_id for item in frozen_evaluation.action_plans)
        if len(planned_ids) != len(set(planned_ids)) or set(planned_ids) != set(
            action_ids
        ):
            raise ValueError(
                "evaluation freezer must return exactly one plan per action"
            )

    def _persist_planning_failure(
        self,
        event: PlanningFailureEvent,
        *,
        state: SearchState,
        batch: BatchRecord,
        decision_round_id: str,
        round_index: int,
        planning_attempt_index: int,
    ) -> None:
        expected = (
            state.run_id,
            state.batch_id,
            state.state_id,
            decision_round_id,
            round_index,
            planning_attempt_index,
            self.planner.method,
            state.compatibility,
        )
        actual = (
            event.run_id,
            event.batch_id,
            event.state_id,
            event.decision_round_id,
            event.round_index,
            event.planning_attempt_index,
            event.method,
            event.compatibility,
        )
        if actual != expected or event.available_at <= state.cutoff.available_at:
            raise ValueError("planning failure differs from the frozen round")
        if batch.batch_id != state.batch_id or batch.method is not event.method:
            raise ValueError("planning failure differs from its batch")
        with self.unit_of_work() as work:
            try:
                current = work.planning_failures.get(event.event_id)
            except KeyError:
                work.planning_failures.add(event)
                return
            # The first terminal trace is authoritative. A DRAFTING retry may
            # call the provider again, but cannot create another cooldown fact
            # for the same stable decision round.
            identity = (
                "event_id",
                "run_id",
                "batch_id",
                "state_id",
                "decision_round_id",
                "round_index",
                "planning_attempt_index",
                "method",
                "failure_kind",
                "compatibility",
            )
            if any(getattr(current, name) != getattr(event, name) for name in identity):
                raise ValueError("planning failure replay has identity drift")

    def _mark_planning_exhausted(self, batch_id: str) -> None:
        with self.unit_of_work() as work:
            for semantic in work.semantic_reservations.list_for_batch(batch_id):
                reservation = semantic.reservation
                if reservation.state is ReservationState.PLANNED:
                    work.semantic_reservations.replace(
                        replace(
                            semantic,
                            reservation=replace(
                                reservation,
                                state=ReservationState.RECLAIMED,
                            ),
                        )
                    )
                elif reservation.state is not ReservationState.RECLAIMED:
                    raise RuntimeError(
                        "planning exhaustion encountered a dispatched reservation"
                    )
            current = work.batches.get(batch_id)
            if current.dispatched_action_ids or current.completed_action_ids:
                raise RuntimeError("planning exhaustion occurred after dispatch")
            work.batches.replace(
                replace(
                    current,
                    status=BatchStatus.PLANNING_EXHAUSTED,
                    resume_from=None,
                    error="planning_exhausted",
                )
            )

    def _validate_plan(
        self, item: PlannedAction, state: SearchState, decision_round_id: str
    ) -> None:
        plan = item.plan
        if not isinstance(plan, PolicyDecisionPlan):
            raise TypeError("planner must return a typed PolicyDecisionPlan")
        action = plan.action
        if plan.method is not self.planner.method:
            raise ValueError("planner returned a plan for another carrier")
        if action.state_id != state.state_id or action.batch_id != state.batch_id:
            raise ValueError("planned action references a different frozen state")
        if action.decision_round_id != decision_round_id:
            raise ValueError("planner must return exactly one action for the round")
        semantic = item.semantic_reservation
        reservation = item.reservation
        if action.method is PolicyMethod.UCB:
            if semantic is None or reservation is None or action.source_slot is None:
                raise ValueError("UCB action must have a semantic reservation")
            if (
                reservation.action_id != action.action_id
                or reservation.batch_id != action.batch_id
                or reservation.arm_id != action.arm_id
                or reservation.direction_id != action.direction.direction_id
                or reservation.decision_round_id != decision_round_id
                or reservation.source_slot_kind is not action.source_slot.kind
                or reservation.state is not ReservationState.PLANNED
                or reservation.created_at != action.created_at
            ):
                raise ValueError("pending reservation does not match UCB action")
            match = action.direction_match
            if match is None or match.kind not in {
                DirectionMatchKind.EXISTING,
                DirectionMatchKind.NEW,
                DirectionMatchKind.PENDING_NEW,
            }:
                raise ValueError("selected UCB action has no assignable final match")
            if match.arm_id != action.arm_id:
                raise ValueError("final direction match and action arm disagree")
            if action.source_slot.kind is SlotKind.EXISTING:
                if (
                    match.kind is not DirectionMatchKind.EXISTING
                    or action.source_slot.source_arm_id != action.arm_id
                ):
                    raise ValueError("existing slot did not retain its source arm")
            elif match.kind not in {
                DirectionMatchKind.NEW,
                DirectionMatchKind.PENDING_NEW,
            }:
                raise ValueError("DISCOVER slot did not remain final-new")
            if reservation.joined_existing_pending is not (
                match.kind is DirectionMatchKind.PENDING_NEW
            ):
                raise ValueError("pending-join flag and final match disagree")
            if semantic.establishes_new_arm is not (
                match.kind is DirectionMatchKind.NEW
            ):
                raise ValueError("new-arm identity and final match disagree")
            if semantic.compatibility != state.compatibility:
                raise ValueError("semantic reservation compatibility drift")
            if semantic.applicability.parent_id != action.parent_id:
                raise ValueError("semantic applicability parent drift")
            dual = decode_semantic_record(
                canonical_input=semantic.canonical_input,
                embedding=semantic.embedding,
                embedding_identity=semantic.embedding_identity,
            )
            expected_arm = canonical_arm_input(
                ArmSpec(failure_mechanism=action.direction.failure_mechanism)
            )
            if (
                dual.arm.canonical_input != expected_arm
                or dual.realization.canonical_input
                != canonical_realization_input(action.direction)
            ):
                raise ValueError("semantic reservation direction drift")
        elif semantic is not None or reservation is not None:
            raise ValueError("Memory action cannot create an arm reservation")


__all__ = ["BatchPlanningLifecycle"]
