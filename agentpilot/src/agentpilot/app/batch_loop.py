"""Carrier-neutral, feedback-isolated and resumable batch coordinator."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from agentpilot.app.contracts import (
    ActionPlanner,
    ActionRunner,
    ArchiveTransitioner,
    BatchExecutionInterrupted,
    BatchResult,
    CarrierUpdateBatch,
    CarrierUpdater,
    EvaluationPlanFreezer,
    ExecutionResult,
    ObservationBuilder,
    PlannedAction,
    SearchReadModelMaterializer,
    SearchStateBuilder,
    TerminalBatchError,
)
from agentpilot.app.execution_finalization import BatchExecutionFinalizer
from agentpilot.app.planning_lifecycle import BatchPlanningLifecycle
from agentpilot.app.recovery import (
    CarrierUpdateRecoveryReader,
    PlanningFailureRecoveryReader,
)
from agentpilot.app.search_read_model import CanonicalSearchReadModelWriter
from agentpilot.domain import (
    SearchState,
    TerminalPolicyUpdateError,
    canonical_json,
    utc_now,
)
from agentpilot.persistence import BatchRecord, BatchStatus, UnitOfWorkFactory


class BatchLoop:
    """Orchestrate one durable batch through planning, execution and update."""

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        state_builder: SearchStateBuilder,
        planner: ActionPlanner,
        evaluation_plan_freezer: EvaluationPlanFreezer,
        runner: ActionRunner,
        observation_builder: ObservationBuilder,
        archive_transitioner: ArchiveTransitioner,
        carrier_updater: CarrierUpdater,
        worker_slots: int,
        search_materializer: SearchReadModelMaterializer | None = None,
        runtime_identity: Mapping[str, Any] | None = None,
    ):
        self.unit_of_work = unit_of_work
        self.state_builder = state_builder
        self.search_materializer = (
            search_materializer
            if search_materializer is not None
            else CanonicalSearchReadModelWriter(unit_of_work)
        )
        self.runtime_identity = dict(runtime_identity or {})
        canonical_json(self.runtime_identity)
        self.planning = BatchPlanningLifecycle(
            unit_of_work=unit_of_work,
            planner=planner,
            evaluation_plan_freezer=evaluation_plan_freezer,
        )
        self.finalization = BatchExecutionFinalizer(
            unit_of_work=unit_of_work,
            runner=runner,
            observation_builder=observation_builder,
            archive_transitioner=archive_transitioner,
            carrier_updater=carrier_updater,
            worker_slots=worker_slots,
        )

    @property
    def planner(self):
        return self.planning.planner

    @planner.setter
    def planner(self, value) -> None:
        self.planning.planner = value

    @property
    def runner(self):
        return self.finalization.runner

    @runner.setter
    def runner(self, value) -> None:
        self.finalization.runner = value

    @property
    def observation_builder(self):
        return self.finalization.observation_builder

    @observation_builder.setter
    def observation_builder(self, value) -> None:
        self.finalization.observation_builder = value

    @property
    def archive_transitioner(self):
        return self.finalization.archive_transitioner

    @archive_transitioner.setter
    def archive_transitioner(self, value) -> None:
        self.finalization.archive_transitioner = value

    @property
    def carrier_updater(self):
        return self.finalization.carrier_updater

    @carrier_updater.setter
    def carrier_updater(self, value) -> None:
        self.finalization.carrier_updater = value

    @property
    def executor(self):
        return self.finalization.executor

    @executor.setter
    def executor(self, value) -> None:
        self.finalization.executor = value

    @property
    def recovery_reader(self):
        return self.finalization.recovery_reader

    @recovery_reader.setter
    def recovery_reader(self, value) -> None:
        self.finalization.recovery_reader = value

    @property
    def planning_failure_reader(self):
        return self.planning.failure_reader

    @planning_failure_reader.setter
    def planning_failure_reader(self, value) -> None:
        self.planning.failure_reader = value

    def run(self, *, run_id: str, batch_id: str, rounds: int) -> BatchResult:
        if isinstance(rounds, bool) or rounds < 1:
            raise ValueError("rounds must be positive")
        state = self.state_builder.build(run_id=run_id, batch_id=batch_id)
        if state.run_id != run_id or state.batch_id != batch_id:
            raise ValueError("SearchStateBuilder returned mismatched coordinates")
        carrier_snapshot = self.planner.begin_batch(state)
        snapshot_payload = dict(self.planner.freeze_snapshot(carrier_snapshot))
        # Fail before writing when a carrier snapshot is opaque.
        canonical_json(snapshot_payload)
        batch = BatchRecord(
            batch_id=batch_id,
            run_id=run_id,
            state_id=state.state_id,
            method=self.planner.method,
            expected_rounds=rounds,
            status=BatchStatus.DRAFTING,
            created_at=utc_now(),
            runtime_identity=self.runtime_identity,
            carrier_snapshot=snapshot_payload,
        )
        with self.unit_of_work() as work:
            work.states.add(state)
            work.batches.add(batch)
        return self._continue(batch_id, state, carrier_snapshot)

    def resume(self, *, batch_id: str) -> BatchResult:
        """Resume planning/execution/merge/update from durable batch facts."""

        with self.unit_of_work() as work:
            batch = work.batches.get(batch_id)
            state = work.states.get(batch.state_id)
            durable_identity = dict(batch.runtime_identity)
            current_identity = dict(self.runtime_identity)
            if canonical_json(durable_identity) != canonical_json(current_identity):
                raise ValueError("runtime identity differs from the frozen batch")
            if batch.status is BatchStatus.POLICY_UPDATED:
                plans = work.plans.list_for_batch(batch_id)
                observations = work.observations.list_for_batch(batch_id)
                return BatchResult(batch, plans, observations)
            if batch.status is BatchStatus.FAILED:
                raise RuntimeError("failed batch is not resumable")
            if batch.status is BatchStatus.INTERRUPTED:
                restored_status = batch.resume_from
                if restored_status is None:
                    raise RuntimeError("interrupted batch has no recovery point")
                batch = replace(
                    batch,
                    status=restored_status,
                    resume_from=None,
                    error=None,
                )
                work.batches.replace(batch)
        if batch.method is not self.planner.method:
            raise ValueError("resume planner method differs from persisted batch")
        carrier_snapshot: object | None = None
        if batch.status in {
            BatchStatus.DRAFTING,
            BatchStatus.FROZEN,
            BatchStatus.RUNNING,
        }:
            carrier_snapshot = self.planner.restore_snapshot(batch.carrier_snapshot)
            if canonical_json(
                self.planner.freeze_snapshot(carrier_snapshot)
            ) != canonical_json(batch.carrier_snapshot):
                raise ValueError(
                    "restored carrier snapshot differs from persisted state"
                )
        return self._continue(batch_id, state, carrier_snapshot)

    def _continue(
        self,
        batch_id: str,
        state: SearchState,
        carrier_snapshot: object | None,
    ) -> BatchResult:
        try:
            with self.unit_of_work() as work:
                status = work.batches.get(batch_id).status
            if status is BatchStatus.DRAFTING:
                if carrier_snapshot is None:
                    raise RuntimeError(
                        "drafting resume is missing its carrier snapshot"
                    )
                self._finish_planning(batch_id, state, carrier_snapshot)
                with self.unit_of_work() as work:
                    status = work.batches.get(batch_id).status
                if status is BatchStatus.PLANNING_EXHAUSTED:
                    with self.unit_of_work() as work:
                        final = work.batches.get(batch_id)
                        plans = work.plans.list_for_batch(batch_id)
                    return BatchResult(final, plans, ())
            if status in {BatchStatus.FROZEN, BatchStatus.RUNNING}:
                self._finish_execution(batch_id, state)
                status = BatchStatus.AT_BARRIER
            if status is BatchStatus.AT_BARRIER:
                self._finish_merge(batch_id)
                status = BatchStatus.MERGED
            if status is BatchStatus.MERGED:
                self.search_materializer.materialize_batch(batch_id=batch_id)
                self._finish_policy_update(batch_id, state)
            with self.unit_of_work() as work:
                final = work.batches.get(batch_id)
                plans = work.plans.list_for_batch(batch_id)
                observations = work.observations.list_for_batch(batch_id)
            return BatchResult(final, plans, observations)
        except (TerminalBatchError, TerminalPolicyUpdateError) as exc:
            self._mark_failed(batch_id, exc)
            raise
        except BaseException as exc:
            self._mark_interrupted(batch_id, exc)
            raise

    def _finish_planning(
        self, batch_id: str, state: SearchState, carrier_snapshot: object
    ) -> None:
        self.planning.finish(batch_id, state, carrier_snapshot)

    def _finish_execution(self, batch_id: str, state: SearchState) -> None:
        self.finalization.finish_execution(batch_id, state)

    def _finish_merge(self, batch_id: str) -> None:
        self.finalization.finish_merge(batch_id)

    def _finish_policy_update(self, batch_id: str, state: SearchState) -> None:
        self.finalization.finish_policy_update(batch_id, state)

    def _mark_interrupted(self, batch_id: str, exc: BaseException) -> None:
        try:
            with self.unit_of_work() as work:
                current = work.batches.get(batch_id)
                if current.status in {
                    BatchStatus.POLICY_UPDATED,
                    BatchStatus.FAILED,
                    BatchStatus.INTERRUPTED,
                }:
                    return
                work.batches.replace(
                    replace(
                        current,
                        status=BatchStatus.INTERRUPTED,
                        resume_from=current.status,
                        error=type(exc).__name__,
                    )
                )
        except (KeyError, RuntimeError):
            return

    def _mark_failed(self, batch_id: str, exc: BaseException) -> None:
        try:
            with self.unit_of_work() as work:
                current = work.batches.get(batch_id)
                if current.status in {
                    BatchStatus.POLICY_UPDATED,
                    BatchStatus.FAILED,
                }:
                    return
                work.batches.replace(
                    replace(
                        current,
                        status=BatchStatus.FAILED,
                        resume_from=None,
                        error=type(exc).__name__,
                    )
                )
        except (KeyError, RuntimeError):
            return


__all__ = [
    "ActionPlanner",
    "ActionRunner",
    "ArchiveTransitioner",
    "BatchExecutionInterrupted",
    "BatchLoop",
    "BatchResult",
    "CarrierUpdater",
    "CarrierUpdateBatch",
    "CarrierUpdateRecoveryReader",
    "ExecutionResult",
    "EvaluationPlanFreezer",
    "ObservationBuilder",
    "PlannedAction",
    "PlanningFailureRecoveryReader",
    "SearchStateBuilder",
    "SearchReadModelMaterializer",
    "TerminalBatchError",
]
