"""Carrier-neutral readers for rebuilding policy inputs from durable facts."""

from __future__ import annotations

from agentpilot.app.contracts import CarrierUpdateBatch, ExecutionResult
from agentpilot.domain import PlanningFailureEvent, PolicyMethod, stable_id
from agentpilot.persistence import BatchRecord, BatchStatus, UnitOfWorkFactory


class CarrierUpdateRecoveryReader:
    """Rebuild complete carrier events solely from durable batch facts."""

    _REPLAYABLE = {BatchStatus.MERGED, BatchStatus.POLICY_UPDATED}

    def __init__(self, unit_of_work: UnitOfWorkFactory):
        self.unit_of_work = unit_of_work

    def get(self, batch_id: str) -> CarrierUpdateBatch:
        with self.unit_of_work() as work:
            batch = work.batches.get(batch_id)
            if batch.status not in self._REPLAYABLE:
                raise ValueError("carrier update recovery requires a merged batch")
            return self._assemble(work, batch)

    def list_for_run(
        self, run_id: str, *, method: PolicyMethod | None = None,
    ) -> tuple[CarrierUpdateBatch, ...]:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be non-empty text")
        if method is not None and not isinstance(method, PolicyMethod):
            raise TypeError("method must be PolicyMethod or None")
        with self.unit_of_work() as work:
            batches = tuple(
                batch
                for batch in work.batches.list_for_run(run_id)
                if batch.status in self._REPLAYABLE
                and (method is None or batch.method is method)
            )
            return tuple(self._assemble(work, batch) for batch in batches)

    @staticmethod
    def _assemble(work: object, batch: BatchRecord) -> CarrierUpdateBatch:
        state = work.states.get(batch.state_id)
        unordered_plans = work.plans.list_for_batch(batch.batch_id)
        plans_by_action = {plan.action.action_id: plan for plan in unordered_plans}
        if len(plans_by_action) != len(unordered_plans) or set(plans_by_action) != set(
            batch.action_ids
        ):
            raise RuntimeError("durable batch plans disagree with action order")
        plans = tuple(plans_by_action[action_id] for action_id in batch.action_ids)
        action_ids = batch.action_ids
        if len(plans) != batch.expected_rounds or any(
            plan.method is not batch.method for plan in plans
        ):
            raise RuntimeError("durable batch plans are incomplete or drifting")
        semantic_by_action = {
            item.reservation.action_id: item
            for item in work.semantic_reservations.list_for_batch(batch.batch_id)
        }
        observation_by_action = {
            item.action_id: item
            for item in work.observations.list_for_batch(batch.batch_id)
        }
        if set(semantic_by_action) - set(action_ids):
            raise RuntimeError("batch has an orphan semantic reservation")
        if set(observation_by_action) != set(action_ids):
            raise RuntimeError("batch observations are incomplete")
        semantics = tuple(
            semantic_by_action[action_id]
            for action_id in action_ids
            if action_id in semantic_by_action
        )
        observations = tuple(
            observation_by_action[action_id] for action_id in action_ids
        )
        execution_results: list[ExecutionResult] = []
        for action_id in action_ids:
            worker = work.outcomes.worker_for_action(action_id)
            outcome = work.outcomes.outcome_for_action(action_id)
            if worker is None or outcome is None:
                raise RuntimeError("carrier recovery is missing an execution result")
            execution_results.append(ExecutionResult(worker, outcome))
        return CarrierUpdateBatch(
            update_id=stable_id("carrier-update", batch.batch_id),
            state=state,
            carrier_snapshot=batch.carrier_snapshot,
            plans=plans,
            semantic_reservations=semantics,
            execution_results=tuple(execution_results),
            observations=observations,
        )


class PlanningFailureRecoveryReader:
    """Read immutable pre-action failures without carrier-specific logic."""

    def __init__(self, unit_of_work: UnitOfWorkFactory):
        self.unit_of_work = unit_of_work

    def get(self, event_id: str) -> PlanningFailureEvent:
        with self.unit_of_work() as work:
            return work.planning_failures.get(event_id)

    def list_for_batch(self, batch_id: str) -> tuple[PlanningFailureEvent, ...]:
        with self.unit_of_work() as work:
            return work.planning_failures.list_for_batch(batch_id)

    def list_for_run(
        self, run_id: str, *, method: PolicyMethod | None = None,
    ) -> tuple[PlanningFailureEvent, ...]:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be non-empty text")
        if method is not None and not isinstance(method, PolicyMethod):
            raise TypeError("method must be PolicyMethod or None")
        with self.unit_of_work() as work:
            return tuple(
                event
                for event in work.planning_failures.list_for_run(run_id)
                if method is None or event.method is method
            )


__all__ = ["CarrierUpdateRecoveryReader", "PlanningFailureRecoveryReader"]
