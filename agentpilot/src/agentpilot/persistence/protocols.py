"""Persistence ports for the batch loop.

The interfaces validate domain lifecycle only.  They intentionally know
nothing about KL-UCB, memory prompts, SQLAlchemy, or evaluator internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Protocol

from agentpilot.domain import (
    CostLedgerEntry,
    EffectivenessObservation,
    FeasibilityObservation,
    PairedOutcome,
    PlanningFailureEvent,
    PolicyAction,
    PolicyDecisionPlan,
    PolicyMethod,
    PolicyObservation,
    ProviderCallDispatch,
    ProviderCallIntent,
    ProviderCallRefusal,
    ProviderCallResult,
    SearchState,
    SnapshotTraceBinding,
    TraceCaptureResult,
    TraceEvidenceManifest,
    WorkerResult,
)
from agentpilot.domain.common import deep_freeze, require_aware, require_text
from agentpilot.evaluation.frozen_plan import (
    FrozenActionEvaluationPlan,
    FrozenInformationPriorSnapshot,
)
from agentpilot.evaluation.sealed.repository import SealedRepository
from agentpilot.search.direction_semantic_index import PendingSemanticReservation
from agentpilot.search.read_model import (
    FrozenSearchInputRecord,
    SearchProgramRecord,
)


class BatchStatus(str, Enum):
    DRAFTING = "drafting"
    FROZEN = "frozen"
    RUNNING = "running"
    AT_BARRIER = "at_barrier"
    MERGED = "merged"
    POLICY_UPDATED = "policy_updated"
    PLANNING_EXHAUSTED = "planning_exhausted"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


@dataclass(frozen=True)
class BatchRecord:
    batch_id: str
    run_id: str
    state_id: str
    method: PolicyMethod
    expected_rounds: int
    status: BatchStatus
    created_at: datetime
    action_ids: tuple[str, ...] = ()
    dispatched_action_ids: tuple[str, ...] = ()
    completed_action_ids: tuple[str, ...] = ()
    runtime_identity: Mapping[str, Any] = field(default_factory=dict)
    carrier_snapshot: Mapping[str, Any] = field(default_factory=dict)
    resume_from: BatchStatus | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        for name in ("batch_id", "run_id", "state_id"):
            require_text(getattr(self, name), name)
        require_aware(self.created_at, "created_at")
        if isinstance(self.expected_rounds, bool) or self.expected_rounds < 1:
            raise ValueError("expected_rounds must be positive")
        for name in (
            "action_ids",
            "dispatched_action_ids",
            "completed_action_ids",
        ):
            values = tuple(getattr(self, name))
            object.__setattr__(self, name, values)
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must not contain duplicates")
            for value in values:
                require_text(value, name)
        if len(self.action_ids) > self.expected_rounds:
            raise ValueError("action_ids exceed expected_rounds")
        if not set(self.dispatched_action_ids).issubset(self.action_ids):
            raise ValueError("dispatched action is absent from action_ids")
        if not set(self.completed_action_ids).issubset(self.dispatched_action_ids):
            raise ValueError("completed action was not dispatch-fenced")
        object.__setattr__(self, "runtime_identity", deep_freeze(self.runtime_identity))
        object.__setattr__(self, "carrier_snapshot", deep_freeze(self.carrier_snapshot))
        if self.status is BatchStatus.INTERRUPTED and self.resume_from is None:
            raise ValueError("interrupted batch must record resume_from")
        if self.status is not BatchStatus.INTERRUPTED and self.resume_from is not None:
            raise ValueError("resume_from is only valid for interrupted batches")
        lifecycle_status = (
            self.resume_from if self.status is BatchStatus.INTERRUPTED else self.status
        )
        if lifecycle_status is BatchStatus.PLANNING_EXHAUSTED:
            if (
                len(self.action_ids) >= self.expected_rounds
                or self.dispatched_action_ids
                or self.completed_action_ids
            ):
                raise ValueError(
                    "planning-exhausted batch must stop before dispatch with "
                    "an incomplete action set"
                )
        if (
            lifecycle_status
            in {
                BatchStatus.FROZEN,
                BatchStatus.RUNNING,
                BatchStatus.AT_BARRIER,
                BatchStatus.MERGED,
                BatchStatus.POLICY_UPDATED,
            }
            and len(self.action_ids) != self.expected_rounds
        ):
            raise ValueError("frozen batch must contain every planned action")
        if lifecycle_status in {BatchStatus.DRAFTING, BatchStatus.FROZEN} and (
            self.dispatched_action_ids or self.completed_action_ids
        ):
            raise ValueError("unstarted batch cannot contain execution facts")
        if lifecycle_status in {
            BatchStatus.AT_BARRIER,
            BatchStatus.MERGED,
            BatchStatus.POLICY_UPDATED,
        } and (
            set(self.dispatched_action_ids) != set(self.action_ids)
            or set(self.completed_action_ids) != set(self.action_ids)
        ):
            raise ValueError("barrier batch must have completed every action")


@dataclass(frozen=True)
class ArchiveTransition:
    action_id: str
    parent_commit_sha: str
    child_commit_sha: str | None
    admitted: bool
    effectiveness_label: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("action_id", "parent_commit_sha", "effectiveness_label"):
            require_text(getattr(self, name), name)
        if self.child_commit_sha is not None:
            require_text(self.child_commit_sha, "child_commit_sha")
        object.__setattr__(self, "payload", deep_freeze(self.payload))


@dataclass(frozen=True)
class LineageEdge:
    action_id: str
    parent_commit_sha: str
    child_commit_sha: str

    def __post_init__(self) -> None:
        for name in ("action_id", "parent_commit_sha", "child_commit_sha"):
            require_text(getattr(self, name), name)


class SearchStateRepository(Protocol):
    def add(self, state: SearchState) -> None: ...

    def get(self, state_id: str) -> SearchState: ...


class BatchRepository(Protocol):
    def add(self, batch: BatchRecord) -> None: ...

    def get(self, batch_id: str) -> BatchRecord: ...

    def list_for_run(self, run_id: str) -> tuple[BatchRecord, ...]: ...

    def replace(self, batch: BatchRecord) -> None: ...


class ActionTraceRepository(Protocol):
    def add(self, action: PolicyAction) -> None: ...

    def get(self, action_id: str) -> PolicyAction: ...

    def list_for_batch(self, batch_id: str) -> tuple[PolicyAction, ...]: ...


class DecisionPlanRepository(Protocol):
    def add(self, plan: PolicyDecisionPlan) -> None: ...

    def get(self, plan_id: str) -> PolicyDecisionPlan: ...

    def list_for_batch(self, batch_id: str) -> tuple[PolicyDecisionPlan, ...]: ...


class EvaluationPriorRepository(Protocol):
    def add(self, snapshot: FrozenInformationPriorSnapshot) -> None: ...

    def get(self, snapshot_id: str) -> FrozenInformationPriorSnapshot: ...

    def snapshot_for_batch(
        self, batch_id: str
    ) -> FrozenInformationPriorSnapshot | None: ...


class ActionEvaluationPlanRepository(Protocol):
    def add(self, plan: FrozenActionEvaluationPlan) -> None: ...

    def get(self, record_id: str) -> FrozenActionEvaluationPlan: ...

    def plan_for_action(
        self, action_id: str
    ) -> FrozenActionEvaluationPlan | None: ...

    def list_for_batch(
        self, batch_id: str
    ) -> tuple[FrozenActionEvaluationPlan, ...]: ...


class PlanningFailureRepository(Protocol):
    """Append-only failures emitted before a plan/action can exist."""

    def add(self, event: PlanningFailureEvent) -> None: ...

    def get(self, event_id: str) -> PlanningFailureEvent: ...

    def list_for_batch(self, batch_id: str) -> tuple[PlanningFailureEvent, ...]: ...

    def list_for_run(self, run_id: str) -> tuple[PlanningFailureEvent, ...]: ...


class SemanticReservationRepository(Protocol):
    """Durable, lossless same-batch semantic identity overlay."""

    def add(self, reservation: PendingSemanticReservation) -> None: ...

    def get(self, reservation_id: str) -> PendingSemanticReservation: ...

    def replace(self, reservation: PendingSemanticReservation) -> None: ...

    def list_for_batch(
        self, batch_id: str
    ) -> tuple[PendingSemanticReservation, ...]: ...


class OutcomeRepository(Protocol):
    def add_worker(self, result: WorkerResult) -> None: ...

    def add_outcome(self, outcome: PairedOutcome) -> None: ...

    def add_effectiveness(self, observation: EffectivenessObservation) -> None: ...

    def worker_for_action(self, action_id: str) -> WorkerResult | None: ...

    def outcome_for_action(self, action_id: str) -> PairedOutcome | None: ...

    def effectiveness_for_action(
        self, action_id: str
    ) -> EffectivenessObservation | None: ...


class PolicyObservationRepository(Protocol):
    def add(self, observation: PolicyObservation) -> None: ...

    def list_for_batch(self, batch_id: str) -> tuple[PolicyObservation, ...]: ...


class FeasibilityRepository(Protocol):
    def add(self, observation: FeasibilityObservation) -> None: ...

    def for_action(self, action_id: str) -> FeasibilityObservation | None: ...

    def list_for_batch(self, batch_id: str) -> tuple[FeasibilityObservation, ...]: ...


class ArchiveRepository(Protocol):
    def add_transition(self, transition: ArchiveTransition) -> None: ...

    def add_lineage(self, edge: LineageEdge) -> None: ...

    def get_transition(self, action_id: str) -> ArchiveTransition: ...

    def lineage_for_action(self, action_id: str) -> LineageEdge | None: ...


class SearchReadModelRepository(Protocol):
    def add_program(self, record: SearchProgramRecord) -> None: ...

    def get_program(self, record_id: str) -> SearchProgramRecord: ...

    def list_programs_for_run(self, run_id: str) -> tuple[SearchProgramRecord, ...]: ...

    def add_snapshot(self, record: FrozenSearchInputRecord) -> None: ...

    def snapshot_for_batch(
        self, run_id: str, batch_id: str
    ) -> FrozenSearchInputRecord | None: ...


class TraceEvidenceRepository(Protocol):
    """Durable metadata for immutable trace blobs stored outside SQLite."""

    def add_capture(self, result: TraceCaptureResult) -> None: ...

    def list_captures_for_program(
        self, program_id: str
    ) -> tuple[TraceCaptureResult, ...]: ...

    def add_manifest(self, manifest: TraceEvidenceManifest) -> None: ...

    def get_manifest(self, manifest_id: str) -> TraceEvidenceManifest: ...

    def list_manifests_for_program(
        self, program_id: str
    ) -> tuple[TraceEvidenceManifest, ...]: ...

    def bind_snapshot(self, binding: SnapshotTraceBinding) -> None: ...

    def binding_for_snapshot(self, snapshot_id: str) -> SnapshotTraceBinding | None: ...


class UsageRepository(Protocol):
    def add_intent(self, intent: ProviderCallIntent) -> None: ...

    def mark_dispatched(self, dispatch: ProviderCallDispatch) -> None: ...

    def get_dispatch(self, call_id: str) -> ProviderCallDispatch | None: ...

    def record_refusal(self, refusal: ProviderCallRefusal) -> None: ...

    def list_refusals(self, call_id: str) -> tuple[ProviderCallRefusal, ...]: ...

    def add_result(self, result: ProviderCallResult) -> None: ...

    def transition_result(self, result: ProviderCallResult) -> None: ...

    def add_cost(self, entry: CostLedgerEntry) -> None: ...

    def get_intent(self, call_id: str) -> ProviderCallIntent: ...

    def get_result(self, call_id: str) -> ProviderCallResult | None: ...

    def list_intents_for_run(self, run_id: str) -> tuple[ProviderCallIntent, ...]: ...

    def list_results_for_run(self, run_id: str) -> tuple[ProviderCallResult, ...]: ...

    def list_dispatches_for_run(
        self, run_id: str
    ) -> tuple[ProviderCallDispatch, ...]: ...

    def list_costs_for_run(self, run_id: str) -> tuple[CostLedgerEntry, ...]: ...


class UnitOfWork(Protocol):
    states: SearchStateRepository
    batches: BatchRepository
    actions: ActionTraceRepository
    plans: DecisionPlanRepository
    evaluation_priors: EvaluationPriorRepository
    evaluation_plans: ActionEvaluationPlanRepository
    planning_failures: PlanningFailureRepository
    semantic_reservations: SemanticReservationRepository
    outcomes: OutcomeRepository
    observations: PolicyObservationRepository
    feasibility: FeasibilityRepository
    archive: ArchiveRepository
    search_read_model: SearchReadModelRepository
    trace_evidence: TraceEvidenceRepository
    usage: UsageRepository
    sealed: SealedRepository

    def __enter__(self) -> "UnitOfWork": ...

    def __exit__(self, exc_type, exc, traceback) -> bool: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...


__all__ = [
    "ActionTraceRepository",
    "ActionEvaluationPlanRepository",
    "ArchiveRepository",
    "ArchiveTransition",
    "BatchRecord",
    "BatchRepository",
    "BatchStatus",
    "DecisionPlanRepository",
    "EvaluationPriorRepository",
    "FeasibilityRepository",
    "LineageEdge",
    "OutcomeRepository",
    "PlanningFailureRepository",
    "SemanticReservationRepository",
    "PolicyObservationRepository",
    "SearchStateRepository",
    "SearchReadModelRepository",
    "TraceEvidenceRepository",
    "UnitOfWork",
    "UnitOfWorkFactory",
    "UsageRepository",
]
