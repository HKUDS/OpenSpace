"""Application wiring that makes the event-sourced UCB carrier recoverable."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from agentpilot.app.recovery import (
    CarrierUpdateRecoveryReader,
    PlanningFailureRecoveryReader,
)
from agentpilot.domain import EvidenceCutoff, PolicyMethod, SearchState
from agentpilot.policy.ucb import UCBActionPlannerAdapter, UCBPolicyUpdater


class RecoverableUCBRuntime:
    """Expose one object as both BatchLoop planner and carrier updater.

    UCB policy tables remain pure in-memory projections.  Before a new batch or
    a planning resume, this adapter deterministically replays every durable
    completed carrier event visible at the frozen cutoff.  A real persistence
    backend therefore does not need policy-specific UCB tables.
    """

    method = PolicyMethod.UCB

    def __init__(
        self,
        *,
        planner: UCBActionPlannerAdapter,
        updater: UCBPolicyUpdater,
        recovery_reader: CarrierUpdateRecoveryReader,
        planning_failure_reader: PlanningFailureRecoveryReader,
    ) -> None:
        self.planner = planner
        self.updater = updater
        self.recovery_reader = recovery_reader
        self.planning_failure_reader = planning_failure_reader

    def _recover(self, run_id: str, cutoff: EvidenceCutoff) -> None:
        batches = self.recovery_reader.list_for_run(run_id, method=PolicyMethod.UCB)
        visible = tuple(
            batch
            for batch in batches
            if batch.observations
            and all(cutoff.allows(item.available_at) for item in batch.observations)
        )
        failures = tuple(
            event
            for event in self.planning_failure_reader.list_for_run(
                run_id, method=PolicyMethod.UCB
            )
            if cutoff.allows(event.available_at)
        )

        def batch_available_at(batch: object) -> datetime:
            timestamps = tuple(
                item.reservation.dispatched_at
                for item in batch.semantic_reservations
                if item.reservation.dispatched_at is not None
            )
            if timestamps:
                return min(timestamps)
            return min(item.available_at for item in batch.observations)

        stream = [
            (batch_available_at(batch), "batch", batch.update_id, batch)
            for batch in visible
        ]
        stream.extend(
            (event.available_at, "failure", event.event_id, event) for event in failures
        )
        self.updater.reset_projection()
        for _, kind, _, item in sorted(stream, key=lambda row: row[:3]):
            if kind == "failure":
                self.updater.record_planning_failure(item)
            else:
                self.updater.update(item)

    @property
    def max_planning_attempts(self) -> int:
        return self.planner.max_planning_attempts

    def begin_batch(self, state: SearchState) -> object:
        self._recover(state.run_id, state.cutoff)
        return self.planner.begin_batch(state)

    def freeze_snapshot(self, snapshot: object) -> Mapping[str, Any]:
        return self.planner.freeze_snapshot(snapshot)

    def restore_snapshot(self, payload: Mapping[str, Any]) -> object:
        run_id = payload.get("run_id")
        cutoff_text = payload.get("cutoff")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("UCB snapshot has no run identity")
        if not isinstance(cutoff_text, str):
            raise ValueError("UCB snapshot has no evidence cutoff")
        self._recover(run_id, EvidenceCutoff(datetime.fromisoformat(cutoff_text)))
        return self.planner.restore_snapshot(payload)

    def plan_round(self, **kwargs: Any) -> object:
        state = kwargs.get("state")
        if not isinstance(state, SearchState):
            raise TypeError("UCB planning requires a typed SearchState")
        failures = tuple(
            event
            for event in self.planning_failure_reader.list_for_run(
                state.run_id, method=PolicyMethod.UCB
            )
            if state.cutoff.allows(event.available_at)
            or event.batch_id == state.batch_id
        )
        return self.planner.plan_round(
            **kwargs,
            prior_planning_failures=failures,
        )

    def update(self, batch: object) -> None:
        state = batch.state
        run_id = state.run_id
        cutoff = state.cutoff
        if not isinstance(run_id, str) or not isinstance(cutoff, EvidenceCutoff):
            raise TypeError("UCB update batch must carry a typed SearchState")
        # The current batch may already be MERGED in durable storage. Its own
        # observations are newer than its frozen cutoff, so recover only the
        # prior visible projection and then apply this barrier exactly once.
        self._recover(run_id, cutoff)
        self.updater.update(batch)


__all__ = ["RecoverableUCBRuntime"]
