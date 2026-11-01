"""Execution, barrier, archive and carrier finalization services."""

from __future__ import annotations

from dataclasses import replace

from agentpilot.app.contracts import (
    ActionRunner,
    ArchiveTransitioner,
    BatchExecutionInterrupted,
    CarrierUpdater,
    ExecutionResult,
    ObservationBuilder,
)
from agentpilot.app.executor import LocalExecutor
from agentpilot.app.recovery import CarrierUpdateRecoveryReader
from agentpilot.domain import (
    EvidenceTier,
    EvaluationValidity,
    FeasibilityObservation,
    FeasibilityStage,
    ImplementationAdherence,
    OutcomeAttribution,
    PolicyAction,
    PolicyMethod,
    PolicyObservation,
    ReservationState,
    SearchState,
    WorkerExecutionStatus,
    stable_id,
    utc_now,
)
from agentpilot.evaluation import FrozenActionEvaluationPlan
from agentpilot.persistence import BatchStatus, UnitOfWork, UnitOfWorkFactory


class BatchExecutionFinalizer:
    """Execute missing actions and publish complete barrier-derived facts."""

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        runner: ActionRunner,
        observation_builder: ObservationBuilder,
        archive_transitioner: ArchiveTransitioner,
        carrier_updater: CarrierUpdater,
        worker_slots: int,
    ) -> None:
        self.unit_of_work = unit_of_work
        self.runner = runner
        self.observation_builder = observation_builder
        self.archive_transitioner = archive_transitioner
        self.carrier_updater = carrier_updater
        self.executor = LocalExecutor(worker_slots)
        self.recovery_reader = CarrierUpdateRecoveryReader(unit_of_work)

    def finish_execution(self, batch_id: str, state: SearchState) -> None:
        with self.unit_of_work() as work:
            batch = work.batches.get(batch_id)
            actions = work.actions.list_for_batch(batch_id)
            if set(batch.action_ids) != {action.action_id for action in actions}:
                raise RuntimeError("frozen batch action records are incomplete")
            for action in actions:
                self._require_frozen_evaluation_plan(work, action, state)
            if batch.status is BatchStatus.FROZEN:
                work.batches.replace(replace(batch, status=BatchStatus.RUNNING))
            missing = tuple(
                action
                for action in actions
                if work.outcomes.outcome_for_action(action.action_id) is None
                or work.outcomes.worker_for_action(action.action_id) is None
            )
        errors: dict[str, Exception] = {}
        for item in self.executor.run(
            missing,
            self.runner.execute,
            before_execute=self._mark_dispatch_started,
        ):
            action = item.item
            if item.error is not None:
                errors[action.action_id] = item.error
                continue
            if item.result is None:
                errors[action.action_id] = RuntimeError(
                    "executor returned no result"
                )
                continue
            try:
                self._persist_execution_result(batch_id, action, item.result)
            except Exception as exc:
                # Preserve every other successful slot even if one worker
                # returned a malformed or conflicting result.
                errors[action.action_id] = exc
        if errors:
            raise BatchExecutionInterrupted(errors)

        # Build all observations from durable raw results, then publish them
        # and the AT_BARRIER state in one transaction.
        observations: list[PolicyObservation] = []
        feasibility: list[FeasibilityObservation] = []
        with self.unit_of_work() as work:
            actions = work.actions.list_for_batch(batch_id)
            plans = {
                plan.action.action_id: plan
                for plan in work.plans.list_for_batch(batch_id)
            }
            if set(plans) != {action.action_id for action in actions}:
                raise RuntimeError("batch actions and typed plans disagree")
            for action in actions:
                worker = work.outcomes.worker_for_action(action.action_id)
                outcome = work.outcomes.outcome_for_action(action.action_id)
                if worker is None or outcome is None:
                    raise RuntimeError(
                        "batch barrier reached with missing raw result"
                    )
                observations.append(
                    self.observation_builder.build(
                        state=state,
                        plan=plans[action.action_id],
                        result=ExecutionResult(worker, outcome),
                    )
                )
                stages = [
                    FeasibilityStage.SCHEDULED,
                    FeasibilityStage.GROUNDED,
                ]
                realized = (
                    worker.status is WorkerExecutionStatus.COMPLETED
                    and worker.result_commit_sha is not None
                    and worker.adherence is ImplementationAdherence.FOLLOWED
                )
                if realized:
                    stages.append(FeasibilityStage.REALIZED)
                    attributable = (
                        outcome.attribution is OutcomeAttribution.ATTRIBUTABLE
                    )
                    if attributable:
                        stages.append(FeasibilityStage.ATTRIBUTABLE)
                        if (
                            outcome.evidence_tier is EvidenceTier.ADAPTIVE_B
                            and outcome.validity is EvaluationValidity.VALID
                            and outcome.signed_effectiveness_statistic is not None
                        ):
                            stages.append(FeasibilityStage.MEASURED)
                feasibility.append(
                    FeasibilityObservation(
                        observation_id=stable_id("feasibility", action.action_id),
                        action_id=action.action_id,
                        arm_id=action.arm_id,
                        failure_mechanism=action.direction.failure_mechanism,
                        reached_stages=tuple(stages),
                        cost_usd=worker.cost_usd,
                        available_at=outcome.completed_at,
                    )
                )
        with self.unit_of_work() as work:
            current = work.batches.get(batch_id)
            if set(current.completed_action_ids) != set(current.action_ids):
                raise RuntimeError(
                    "batch barrier reached with missing completed action"
                )
            for observation in observations:
                work.outcomes.add_effectiveness(observation.effectiveness)
                work.observations.add(observation)
            for item in feasibility:
                work.feasibility.add(item)
            work.batches.replace(
                replace(current, status=BatchStatus.AT_BARRIER)
            )

    def finish_merge(self, batch_id: str) -> None:
        with self.unit_of_work() as work:
            actions = work.actions.list_for_batch(batch_id)
            observations = {
                item.action_id: item
                for item in work.observations.list_for_batch(batch_id)
            }
            for action in actions:
                worker = work.outcomes.worker_for_action(action.action_id)
                outcome = work.outcomes.outcome_for_action(action.action_id)
                if worker is None or outcome is None:
                    raise RuntimeError(
                        "merge is missing a durable execution result"
                    )
                observation = observations[action.action_id]
                transition, edge = self.archive_transitioner.transition(
                    action=action,
                    result=ExecutionResult(worker, outcome),
                    observation=observation,
                )
                work.archive.add_transition(transition)
                if edge is not None:
                    work.archive.add_lineage(edge)
            current = work.batches.get(batch_id)
            work.batches.replace(replace(current, status=BatchStatus.MERGED))

    def finish_policy_update(
        self, batch_id: str, state: SearchState
    ) -> None:
        update = self.recovery_reader.get(batch_id)
        if update.state != state:
            raise RuntimeError("carrier recovery returned another SearchState")
        self.carrier_updater.update(update)
        with self.unit_of_work() as work:
            current = work.batches.get(batch_id)
            work.batches.replace(
                replace(current, status=BatchStatus.POLICY_UPDATED)
            )

    def _persist_execution_result(
        self, batch_id: str, action: PolicyAction, result: ExecutionResult
    ) -> None:
        if result.worker.action_id != action.action_id:
            raise ValueError("worker result references a different action")
        if result.outcome.action_id != action.action_id:
            raise ValueError("paired outcome references a different action")
        if result.outcome.evidence_tier not in {
            EvidenceTier.REALIZATION,
            EvidenceTier.ADAPTIVE_B,
        }:
            raise ValueError(
                "normal batch persistence accepts only realization or adaptive B outcomes"
            )
        with self.unit_of_work() as work:
            semantics = tuple(
                semantic
                for semantic in work.semantic_reservations.list_for_batch(
                    batch_id
                )
                if semantic.reservation.action_id == action.action_id
            )
            if action.method is PolicyMethod.UCB:
                if len(semantics) != 1:
                    raise RuntimeError(
                        "completed UCB action has no unique semantic reservation"
                    )
                reservation = semantics[0].reservation
                if (
                    reservation.state is not ReservationState.DISPATCHED
                    or reservation.dispatched_at is None
                ):
                    raise RuntimeError(
                        "completed action was never dispatch-fenced"
                    )
                if result.worker.started_at < reservation.dispatched_at:
                    raise ValueError("worker started before its dispatch fence")
            elif semantics:
                raise RuntimeError(
                    "Memory action unexpectedly has a reservation"
                )
            if result.outcome.completed_at < result.worker.completed_at:
                raise ValueError("evaluation completed before worker completion")
            work.outcomes.add_worker(result.worker)
            work.outcomes.add_outcome(result.outcome)
            for semantic in semantics:
                reservation = semantic.reservation
                if reservation.state is ReservationState.DISPATCHED:
                    work.semantic_reservations.replace(
                        replace(
                            semantic,
                            reservation=replace(
                                reservation,
                                state=ReservationState.COMPLETED,
                                completed_at=result.worker.completed_at,
                            ),
                        )
                    )
                elif reservation.state is not ReservationState.COMPLETED:
                    raise RuntimeError(
                        "completed action was never dispatch-fenced"
                    )
            current = work.batches.get(batch_id)
            if action.action_id not in current.completed_action_ids:
                work.batches.replace(
                    replace(
                        current,
                        completed_action_ids=(
                            *current.completed_action_ids,
                            action.action_id,
                        ),
                    )
                )

    def _mark_dispatch_started(self, action: PolicyAction) -> None:
        with self.unit_of_work() as work:
            current = work.batches.get(action.batch_id)
            state = work.states.get(action.state_id)
            self._require_frozen_evaluation_plan(work, action, state)
            if current.status is not BatchStatus.RUNNING:
                raise RuntimeError("action dispatch requires a running batch")
            if action.action_id not in current.action_ids:
                raise RuntimeError(
                    "dispatch action is absent from frozen batch"
                )
            semantics = [
                item
                for item in work.semantic_reservations.list_for_batch(
                    action.batch_id
                )
                if item.reservation.action_id == action.action_id
            ]
            if action.method is PolicyMethod.UCB:
                if len(semantics) != 1:
                    raise RuntimeError(
                        "UCB action has no unique semantic reservation"
                    )
                semantic = semantics[0]
                reservation = semantic.reservation
                if reservation.state is ReservationState.PLANNED:
                    dispatched_at = utc_now()
                    work.semantic_reservations.replace(
                        replace(
                            semantic,
                            reservation=replace(
                                reservation,
                                state=ReservationState.DISPATCHED,
                                dispatched_at=dispatched_at,
                            ),
                        )
                    )
                elif reservation.state is not ReservationState.DISPATCHED:
                    raise RuntimeError(
                        "reservation cannot be dispatched from current state"
                    )
            elif semantics:
                raise RuntimeError(
                    "Memory action unexpectedly has a reservation"
                )
            if action.action_id not in current.dispatched_action_ids:
                work.batches.replace(
                    replace(
                        current,
                        dispatched_action_ids=(
                            *current.dispatched_action_ids,
                            action.action_id,
                        ),
                    )
                )

    @staticmethod
    def _require_frozen_evaluation_plan(
        work: UnitOfWork,
        action: PolicyAction,
        state: SearchState,
    ) -> FrozenActionEvaluationPlan:
        plan = work.evaluation_plans.plan_for_action(action.action_id)
        if plan is None:
            raise RuntimeError(
                f"action {action.action_id!r} has no frozen evaluation plan"
            )
        prior = work.evaluation_priors.snapshot_for_batch(action.batch_id)
        if prior is None:
            raise RuntimeError(
                f"batch {action.batch_id!r} has no frozen evaluation prior"
            )
        expected = (
            action.run_id,
            action.batch_id,
            action.state_id,
            state.cutoff,
            prior.snapshot_id,
        )
        actual = (
            plan.run_id,
            plan.batch_id,
            plan.state_id,
            plan.cutoff,
            plan.prior_snapshot_id,
        )
        if actual != expected or (
            prior.run_id,
            prior.batch_id,
            prior.state_id,
            prior.cutoff,
            prior.compatibility,
        ) != (
            state.run_id,
            state.batch_id,
            state.state_id,
            state.cutoff,
            state.compatibility,
        ):
            raise RuntimeError(
                "frozen evaluation plan differs from its action SearchState"
            )
        return plan


__all__ = ["BatchExecutionFinalizer"]
