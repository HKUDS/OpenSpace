"""Small application contracts shared by lifecycle services and adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from agentpilot.domain import (
    EvaluationValidity,
    EvidenceTier,
    ImplementationAdherence,
    OutcomeAttribution,
    PairedOutcome,
    PendingReservation,
    PolicyAction,
    PolicyDecisionPlan,
    PolicyMethod,
    PolicyObservation,
    ReservationState,
    SearchState,
    WorkerExecutionStatus,
    WorkerResult,
    deep_freeze,
    stable_id,
)
from agentpilot.evaluation import FrozenEvaluationBatch
from agentpilot.persistence import ArchiveTransition, BatchRecord, LineageEdge
from agentpilot.search.direction_semantic_index import PendingSemanticReservation
from agentpilot.search.read_model import SearchProgramRecord


class BatchExecutionInterrupted(RuntimeError):
    """One or more external slots did not produce a durable result."""

    def __init__(self, errors: Mapping[str, Exception]):
        self.errors = dict(errors)
        # Name the failure type per action. Without it the aggregate hides the
        # only fact that tells a claim released for late accounting apart from
        # one released because the candidate never built.
        super().__init__(
            "batch execution interrupted for actions: "
            + ", ".join(
                f"{action_id} ({type(error).__qualname__})"
                for action_id, error in sorted(self.errors.items())
            )
        )


class TerminalBatchError(RuntimeError):
    """An immutable batch contract is invalid and retry cannot repair it."""


@dataclass(frozen=True)
class PlannedAction:
    plan: PolicyDecisionPlan
    semantic_reservation: PendingSemanticReservation | None = None

    @property
    def action(self) -> PolicyAction:
        return self.plan.action

    @property
    def reservation(self) -> PendingReservation | None:
        semantic = self.semantic_reservation
        return semantic.reservation if semantic is not None else None


@dataclass(frozen=True)
class ExecutionResult:
    worker: WorkerResult
    outcome: PairedOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.worker, WorkerResult):
            raise TypeError("worker must be a WorkerResult")
        if not isinstance(self.outcome, PairedOutcome):
            raise TypeError("outcome must be a PairedOutcome")
        if self.worker.action_id != self.outcome.action_id:
            raise ValueError("worker and outcome action ids differ")
        if self.outcome.completed_at < self.worker.completed_at:
            raise ValueError("outcome completed before worker")
        if self.outcome.evidence_tier is not EvidenceTier.REALIZATION:
            return
        failure_type = self.worker.diagnostics.get("failure_type")
        safe_failure_type = (
            isinstance(failure_type, str)
            and 0 < len(failure_type) <= 128
            and all(
                character.isalnum() or character in "._"
                for character in failure_type
            )
        )
        common_rejection_shape = (
            safe_failure_type
            and self.outcome.validity is EvaluationValidity.MISSING
            and self.outcome.attribution is OutcomeAttribution.NON_ATTRIBUTABLE
            and self.outcome.signed_effectiveness_statistic is None
            and self.outcome.primary_delta_mean is None
            and self.outcome.primary_delta_lower is None
            and self.outcome.resource_regression is None
            and self.outcome.constraints_pass is None
            and self.outcome.child_readout is None
            and not self.outcome.cell_diagnostics
            and self.outcome.completed_at == self.worker.completed_at
            and self.outcome.estimator_identity.get("status") == "not_evaluated"
            and self.outcome.estimator_identity.get("reason") == failure_type
        )
        precommit_rejection = (
            self.worker.status is WorkerExecutionStatus.FAILED
            and self.worker.adherence is ImplementationAdherence.DEVIATED
            and self.worker.result_commit_sha is None
            and not self.worker.touched_files
            and self.outcome.admission_fate == "implementation_rejected"
        )
        adherence_terminal_shapes = {
            ImplementationAdherence.DEVIATED: (
                "StructuralImplementationDeviation",
                "adherence_rejected",
            ),
            ImplementationAdherence.UNKNOWN: (
                "StructuralImplementationUnknown",
                "adherence_unknown",
            ),
        }
        expected_adherence_terminal = adherence_terminal_shapes.get(
            self.worker.adherence
        )
        adherence_terminal = (
            self.worker.status is WorkerExecutionStatus.COMPLETED
            and expected_adherence_terminal is not None
            and (failure_type, self.outcome.admission_fate)
            == expected_adherence_terminal
            and isinstance(self.worker.result_commit_sha, str)
            and bool(self.worker.result_commit_sha.strip())
            and bool(self.worker.touched_files)
        )
        if not common_rejection_shape or not (
            precommit_rejection or adherence_terminal
        ):
            raise ValueError(
                "realization outcome does not match the terminal rejection contract"
            )


@dataclass(frozen=True)
class CarrierUpdateBatch:
    """Complete durable barrier input; never projected from planner memory."""

    update_id: str
    state: SearchState
    carrier_snapshot: Mapping[str, Any]
    plans: tuple[PolicyDecisionPlan, ...]
    semantic_reservations: tuple[PendingSemanticReservation, ...]
    execution_results: tuple[ExecutionResult, ...]
    observations: tuple[PolicyObservation, ...]

    def __post_init__(self) -> None:
        for name in (
            "plans",
            "semantic_reservations",
            "execution_results",
            "observations",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if self.update_id != stable_id("carrier-update", self.state.batch_id):
            raise ValueError("carrier update id is not stable for the batch")
        if not self.plans:
            raise ValueError("carrier update needs at least one decision plan")
        object.__setattr__(
            self,
            "carrier_snapshot",
            deep_freeze(self.carrier_snapshot),
        )
        action_ids = tuple(plan.action.action_id for plan in self.plans)
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("carrier update contains duplicate planned actions")
        if any(
            plan.action.state_id != self.state.state_id
            or plan.action.batch_id != self.state.batch_id
            for plan in self.plans
        ):
            raise ValueError("carrier update plan coordinates drifted")
        result_ids = tuple(item.worker.action_id for item in self.execution_results)
        if result_ids != action_ids or any(
            item.outcome.action_id != item.worker.action_id
            for item in self.execution_results
        ):
            raise ValueError("carrier update execution results are incomplete")
        observation_ids = tuple(item.action_id for item in self.observations)
        if observation_ids != action_ids:
            raise ValueError("carrier update observations are incomplete")
        for plan, observation in zip(self.plans, self.observations):
            action = plan.action
            selected_parent = next(
                candidate
                for candidate in plan.parent_slate.candidates
                if candidate.parent_id == plan.parent_selection.parent_id
                and candidate.commit_sha == plan.parent_selection.commit_sha
            )
            expected_observation = (
                action.method,
                action.parent_id,
                action.arm_id,
                action.source_slot.kind if action.source_slot is not None else None,
                action.direction_match,
                self.state.compatibility,
                selected_parent.descriptor,
                self.state.direction_descriptor,
            )
            actual_observation = (
                observation.method,
                observation.parent_id,
                observation.arm_id,
                observation.source_slot_kind,
                observation.direction_match,
                observation.compatibility,
                observation.parent_descriptor,
                observation.direction_descriptor,
            )
            if actual_observation != expected_observation:
                raise ValueError(
                    "carrier update observation differs from its action/state"
                )
            if observation.available_at != observation.effectiveness.available_at:
                raise ValueError(
                    "carrier update observation/effectiveness time drifted"
                )
        reservation_ids = tuple(
            item.reservation.action_id for item in self.semantic_reservations
        )
        method = self.plans[0].method
        if any(plan.method is not method for plan in self.plans):
            raise ValueError("carrier update mixes policy methods")
        if method is PolicyMethod.UCB:
            if reservation_ids != action_ids:
                raise ValueError(
                    "UCB carrier update needs one ordered reservation per action"
                )
            if any(
                item.reservation.state is not ReservationState.COMPLETED
                for item in self.semantic_reservations
            ):
                raise ValueError("UCB carrier update has unfinished reservations")
        elif self.semantic_reservations:
            raise ValueError("Memory carrier update cannot carry reservations")


class SearchStateBuilder(Protocol):
    def build(self, *, run_id: str, batch_id: str) -> SearchState: ...


class ActionPlanner(Protocol):
    method: PolicyMethod
    # How many whole-slate replacements one logical round may spend before the
    # batch is declared planning-exhausted. This is an experiment choice, so it
    # arrives from configuration rather than from a constant in the lifecycle.
    max_planning_attempts: int

    def begin_batch(self, state: SearchState) -> object: ...

    def freeze_snapshot(self, snapshot: object) -> Mapping[str, Any]:
        """Return complete JSON-compatible state used by the batch."""

    def restore_snapshot(self, payload: Mapping[str, Any]) -> object:
        """Restore exactly the state returned by ``begin_batch``."""

    def plan_round(
        self,
        *,
        state: SearchState,
        carrier_snapshot: object,
        decision_round_id: str,
        round_index: int,
        planning_attempt_index: int,
        pending: tuple[PendingSemanticReservation, ...],
    ) -> PlannedAction: ...


class EvaluationPlanFreezer(Protocol):
    """Build every trusted adaptive-B plan from one frozen SearchState."""

    freezer_id: str
    source_scope_id: str

    def freeze_batch(
        self,
        *,
        state: SearchState,
        actions: tuple[PolicyAction, ...],
    ) -> FrozenEvaluationBatch:
        """Return a complete deterministic plan set without persisting it."""

        ...


class ActionRunner(Protocol):
    def execute(self, action: PolicyAction) -> ExecutionResult:
        """Execute idempotently by stable ``action.action_id``."""


class ObservationBuilder(Protocol):
    def build(
        self,
        *,
        state: SearchState,
        plan: PolicyDecisionPlan,
        result: ExecutionResult,
    ) -> PolicyObservation: ...


class ArchiveTransitioner(Protocol):
    def transition(
        self,
        *,
        action: PolicyAction,
        result: ExecutionResult,
        observation: PolicyObservation,
    ) -> tuple[ArchiveTransition, LineageEdge | None]: ...


class CarrierUpdater(Protocol):
    def update(self, batch: CarrierUpdateBatch) -> None:
        """Apply idempotently using ``batch.update_id`` as the replay key."""


class SearchReadModelMaterializer(Protocol):
    def materialize_batch(self, *, batch_id: str) -> tuple[SearchProgramRecord, ...]:
        """Project every admitted action using only durable evaluator readouts."""


@dataclass(frozen=True)
class BatchResult:
    batch: BatchRecord
    plans: tuple[PolicyDecisionPlan, ...]
    observations: tuple[PolicyObservation, ...]

    @property
    def actions(self) -> tuple[PolicyAction, ...]:
        return tuple(plan.action for plan in self.plans)


__all__ = [
    "ActionPlanner",
    "ActionRunner",
    "ArchiveTransitioner",
    "BatchExecutionInterrupted",
    "BatchResult",
    "CarrierUpdateBatch",
    "CarrierUpdater",
    "ExecutionResult",
    "EvaluationPlanFreezer",
    "ObservationBuilder",
    "PlannedAction",
    "SearchStateBuilder",
    "SearchReadModelMaterializer",
    "TerminalBatchError",
]
