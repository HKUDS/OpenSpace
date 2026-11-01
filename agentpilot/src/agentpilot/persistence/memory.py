"""Transactional in-memory persistence used by domain and app tests."""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from agentpilot.domain import (
    CostLedgerEntry,
    EffectivenessObservation,
    EvidenceTier,
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
    ProviderCallStatus,
    SearchState,
    SnapshotTraceBinding,
    TraceCaptureResult,
    TraceEvidenceManifest,
    WorkerResult,
    stable_id,
)
from agentpilot.domain.policy import ReservationState
from agentpilot.evaluation.frozen_plan import (
    FrozenActionEvaluationPlan,
    FrozenInformationPriorSnapshot,
)
from agentpilot.evaluation.sealed.contracts import (
    SealedAuthorization,
    SealedRecord,
    SealedState,
)
from agentpilot.evaluation.sealed.repository import InMemorySealedRepository
from agentpilot.persistence.protocols import (
    ArchiveTransition,
    BatchRecord,
    BatchStatus,
    LineageEdge,
)
from agentpilot.search.direction_history import (
    DirectionHistoryRecord,
    resolve_rejected_candidate_manifest_id,
)
from agentpilot.search.direction_semantic_index import PendingSemanticReservation
from agentpilot.search.read_model import (
    FrozenSearchInputRecord,
    SearchProgramRecord,
)

T = TypeVar("T")


class PersistenceConflict(RuntimeError):
    """An immutable identity was replayed with different content."""


def _put_immutable(table: dict[str, T], key: str, value: T) -> None:
    existing = table.get(key)
    if existing is None:
        table[key] = value
    elif existing != value:
        raise PersistenceConflict(f"immutable record drift for {key}")


@dataclass
class InMemoryDatabase:
    states: dict[str, SearchState] = field(default_factory=dict)
    batches: dict[str, BatchRecord] = field(default_factory=dict)
    actions: dict[str, PolicyAction] = field(default_factory=dict)
    plans: dict[str, PolicyDecisionPlan] = field(default_factory=dict)
    evaluation_priors: dict[str, FrozenInformationPriorSnapshot] = field(
        default_factory=dict
    )
    evaluation_plans: dict[str, FrozenActionEvaluationPlan] = field(
        default_factory=dict
    )
    planning_failures: dict[str, PlanningFailureEvent] = field(default_factory=dict)
    semantic_reservations: dict[str, PendingSemanticReservation] = field(
        default_factory=dict
    )
    workers: dict[str, WorkerResult] = field(default_factory=dict)
    outcomes: dict[str, PairedOutcome] = field(default_factory=dict)
    effectiveness: dict[str, EffectivenessObservation] = field(default_factory=dict)
    observations: dict[str, PolicyObservation] = field(default_factory=dict)
    feasibility: dict[str, FeasibilityObservation] = field(default_factory=dict)
    observation_batches: dict[str, str] = field(default_factory=dict)
    archive_transitions: dict[str, ArchiveTransition] = field(default_factory=dict)
    lineage_edges: dict[str, LineageEdge] = field(default_factory=dict)
    search_programs: dict[str, SearchProgramRecord] = field(default_factory=dict)
    search_input_snapshots: dict[str, FrozenSearchInputRecord] = field(
        default_factory=dict
    )
    trace_captures: dict[str, TraceCaptureResult] = field(default_factory=dict)
    trace_manifests: dict[str, TraceEvidenceManifest] = field(default_factory=dict)
    snapshot_trace_bindings: dict[str, SnapshotTraceBinding] = field(
        default_factory=dict
    )
    provider_call_intents: dict[str, ProviderCallIntent] = field(default_factory=dict)
    provider_call_dispatches: dict[str, ProviderCallDispatch] = field(
        default_factory=dict
    )
    provider_call_refusals: dict[str, ProviderCallRefusal] = field(
        default_factory=dict
    )
    provider_call_results: dict[str, ProviderCallResult] = field(default_factory=dict)
    cost_entries: dict[str, CostLedgerEntry] = field(default_factory=dict)
    sealed_records: dict[str, SealedRecord] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def _snapshot(self) -> dict[str, Any]:
        return {
            name: copy.deepcopy(getattr(self, name))
            for name in (
                "states",
                "batches",
                "actions",
                "plans",
                "evaluation_priors",
                "evaluation_plans",
                "planning_failures",
                "semantic_reservations",
                "workers",
                "outcomes",
                "effectiveness",
                "observations",
                "feasibility",
                "observation_batches",
                "archive_transitions",
                "lineage_edges",
                "search_programs",
                "search_input_snapshots",
                "trace_captures",
                "trace_manifests",
                "snapshot_trace_bindings",
                "provider_call_intents",
                "provider_call_dispatches",
                "provider_call_refusals",
                "provider_call_results",
                "cost_entries",
                "sealed_records",
            )
        }

    def _restore(self, snapshot: dict[str, Any]) -> None:
        for name, value in snapshot.items():
            setattr(self, name, value)


def _require_search_open(
    database: InMemoryDatabase,
    run_id: str,
    *,
    subject: str,
) -> None:
    sealed = database.sealed_records.get(run_id)
    if sealed is not None and sealed.state is not SealedState.SEALED:
        raise PersistenceConflict(f"sealed run cannot mutate {subject}")


class _StateRepository:
    def __init__(self, database: InMemoryDatabase):
        self.database = database

    def add(self, state: SearchState) -> None:
        existing = self.database.states.get(state.state_id)
        if existing is not None:
            _put_immutable(self.database.states, state.state_id, state)
            return
        _require_search_open(
            self.database,
            state.run_id,
            subject="search states",
        )
        _put_immutable(self.database.states, state.state_id, state)

    def get(self, state_id: str) -> SearchState:
        return self.database.states[state_id]


class _BatchRepository:
    def __init__(self, database: InMemoryDatabase):
        self.database = database

    def add(self, batch: BatchRecord) -> None:
        self._require_search_open(batch.run_id)
        _put_immutable(self.database.batches, batch.batch_id, batch)

    def get(self, batch_id: str) -> BatchRecord:
        return self.database.batches[batch_id]

    def list_for_run(self, run_id: str) -> tuple[BatchRecord, ...]:
        return tuple(
            sorted(
                (
                    batch
                    for batch in self.database.batches.values()
                    if batch.run_id == run_id
                ),
                key=lambda batch: (batch.created_at, batch.batch_id),
            )
        )

    def replace(self, batch: BatchRecord) -> None:
        self._require_search_open(batch.run_id)
        current = self.database.batches.get(batch.batch_id)
        if current is None:
            raise KeyError(batch.batch_id)
        immutable = (
            "batch_id",
            "run_id",
            "state_id",
            "method",
            "expected_rounds",
            "created_at",
            "runtime_identity",
            "carrier_snapshot",
        )
        if any(getattr(batch, name) != getattr(current, name) for name in immutable):
            raise PersistenceConflict("batch immutable coordinates changed")
        append_only = (
            "action_ids",
            "dispatched_action_ids",
            "completed_action_ids",
        )
        for name in append_only:
            before = getattr(current, name)
            after = getattr(batch, name)
            if after[: len(before)] != before:
                raise PersistenceConflict(f"{name} are not append-only")
        allowed = {
            BatchStatus.DRAFTING: {
                BatchStatus.FROZEN,
                BatchStatus.PLANNING_EXHAUSTED,
                BatchStatus.INTERRUPTED,
                BatchStatus.FAILED,
            },
            BatchStatus.FROZEN: {
                BatchStatus.RUNNING,
                BatchStatus.INTERRUPTED,
                BatchStatus.FAILED,
            },
            BatchStatus.RUNNING: {
                BatchStatus.AT_BARRIER,
                BatchStatus.INTERRUPTED,
                BatchStatus.FAILED,
            },
            BatchStatus.AT_BARRIER: {
                BatchStatus.MERGED,
                BatchStatus.INTERRUPTED,
                BatchStatus.FAILED,
            },
            BatchStatus.MERGED: {
                BatchStatus.POLICY_UPDATED,
                BatchStatus.INTERRUPTED,
                BatchStatus.FAILED,
            },
            BatchStatus.POLICY_UPDATED: set(),
            BatchStatus.PLANNING_EXHAUSTED: set(),
            BatchStatus.INTERRUPTED: (
                {current.resume_from} if current.resume_from is not None else set()
            ),
            BatchStatus.FAILED: set(),
        }
        if (
            batch.status is not current.status
            and batch.status not in allowed[current.status]
        ):
            raise PersistenceConflict(
                "invalid batch transition "
                f"{current.status.value}->{batch.status.value}"
            )
        if (
            current.status is BatchStatus.DRAFTING
            and batch.status is BatchStatus.FROZEN
        ):
            priors = tuple(
                item
                for item in self.database.evaluation_priors.values()
                if item.batch_id == batch.batch_id
            )
            plans = tuple(
                item
                for item in self.database.evaluation_plans.values()
                if item.batch_id == batch.batch_id
            )
            if len(priors) != 1:
                raise PersistenceConflict(
                    "frozen batch needs exactly one evaluation prior snapshot"
                )
            plan_action_ids = tuple(item.action_id for item in plans)
            if (
                len(plan_action_ids) != len(set(plan_action_ids))
                or set(plan_action_ids) != set(batch.action_ids)
            ):
                raise PersistenceConflict(
                    "frozen batch needs exactly one evaluation plan per action"
                )
        self.database.batches[batch.batch_id] = batch

    def _require_search_open(self, run_id: str) -> None:
        _require_search_open(
            self.database,
            run_id,
            subject="search batches",
        )


class _ActionRepository:
    def __init__(self, database: InMemoryDatabase):
        self.database = database

    def add(self, action: PolicyAction) -> None:
        state = self.database.states.get(action.state_id)
        batch = self.database.batches.get(action.batch_id)
        if state is None or batch is None:
            raise PersistenceConflict("action needs a persisted state and batch")
        if (
            action.run_id != state.run_id
            or action.batch_id != state.batch_id
            or batch.run_id != action.run_id
            or batch.state_id != action.state_id
            or batch.method is not action.method
        ):
            raise PersistenceConflict("action coordinates differ from batch state")
        for existing in self.database.actions.values():
            if existing.action_id == action.action_id:
                continue
            if (
                existing.batch_id == action.batch_id
                and existing.decision_round_id == action.decision_round_id
            ):
                raise PersistenceConflict(
                    "decision round already has a selected action"
                )
        _put_immutable(self.database.actions, action.action_id, action)

    def get(self, action_id: str) -> PolicyAction:
        return self.database.actions[action_id]

    def list_for_batch(self, batch_id: str) -> tuple[PolicyAction, ...]:
        return tuple(
            sorted(
                (
                    action
                    for action in self.database.actions.values()
                    if action.batch_id == batch_id
                ),
                key=lambda action: (
                    action.decision_round_id,
                    action.action_id,
                ),
            )
        )


class _PlanRepository:
    def __init__(self, database: InMemoryDatabase):
        self.database = database

    def add(self, plan: PolicyDecisionPlan) -> None:
        action = self.database.actions.get(plan.action.action_id)
        if action is None:
            raise PersistenceConflict("decision plan action must be persisted first")
        if action != plan.action:
            raise PersistenceConflict("decision plan embeds a drifting action")
        batch = self.database.batches.get(action.batch_id)
        if batch is None or plan.method is not batch.method:
            raise PersistenceConflict("decision plan method differs from batch")
        for existing in self.database.plans.values():
            if existing.plan_id == plan.plan_id:
                continue
            if existing.action.action_id == action.action_id:
                raise PersistenceConflict("action already has a decision plan")
            if (
                existing.action.batch_id == action.batch_id
                and existing.action.decision_round_id == action.decision_round_id
            ):
                raise PersistenceConflict("decision round already has a plan")
        _put_immutable(self.database.plans, plan.plan_id, plan)

    def get(self, plan_id: str) -> PolicyDecisionPlan:
        return self.database.plans[plan_id]

    def list_for_batch(self, batch_id: str) -> tuple[PolicyDecisionPlan, ...]:
        return tuple(
            sorted(
                (
                    plan
                    for plan in self.database.plans.values()
                    if plan.action.batch_id == batch_id
                ),
                key=lambda plan: (
                    plan.action.decision_round_id,
                    plan.action.action_id,
                ),
            )
        )


class _EvaluationPriorRepository:
    def __init__(self, database: InMemoryDatabase):
        self.database = database

    def add(self, snapshot: FrozenInformationPriorSnapshot) -> None:
        state = self.database.states.get(snapshot.state_id)
        batch = self.database.batches.get(snapshot.batch_id)
        if state is None or batch is None:
            raise PersistenceConflict(
                "evaluation prior needs a persisted state and batch"
            )
        if batch.status is not BatchStatus.DRAFTING:
            raise PersistenceConflict(
                "evaluation prior can only be frozen while drafting"
            )
        expected = (
            state.run_id,
            state.batch_id,
            state.state_id,
            state.cutoff,
            state.compatibility,
        )
        actual = (
            snapshot.run_id,
            snapshot.batch_id,
            snapshot.state_id,
            snapshot.cutoff,
            snapshot.compatibility,
        )
        if actual != expected or batch.state_id != state.state_id:
            raise PersistenceConflict(
                "evaluation prior differs from the frozen SearchState"
            )
        for existing in self.database.evaluation_priors.values():
            if (
                existing.batch_id == snapshot.batch_id
                and existing.snapshot_id != snapshot.snapshot_id
            ):
                raise PersistenceConflict(
                    "batch already has a different evaluation prior snapshot"
                )
        _put_immutable(
            self.database.evaluation_priors,
            snapshot.snapshot_id,
            snapshot,
        )

    def get(self, snapshot_id: str) -> FrozenInformationPriorSnapshot:
        return self.database.evaluation_priors[snapshot_id]

    def snapshot_for_batch(
        self, batch_id: str
    ) -> FrozenInformationPriorSnapshot | None:
        matches = tuple(
            item
            for item in self.database.evaluation_priors.values()
            if item.batch_id == batch_id
        )
        if len(matches) > 1:
            raise PersistenceConflict(
                "batch has multiple evaluation prior snapshots"
            )
        return matches[0] if matches else None


class _ActionEvaluationPlanRepository:
    def __init__(self, database: InMemoryDatabase):
        self.database = database

    def add(self, plan: FrozenActionEvaluationPlan) -> None:
        action = self.database.actions.get(plan.action_id)
        state = self.database.states.get(plan.state_id)
        batch = self.database.batches.get(plan.batch_id)
        prior = self.database.evaluation_priors.get(plan.prior_snapshot_id)
        if action is None or state is None or batch is None or prior is None:
            raise PersistenceConflict(
                "evaluation plan needs its persisted action, state, batch, and prior"
            )
        if batch.status is not BatchStatus.DRAFTING:
            raise PersistenceConflict(
                "evaluation plan can only be frozen while drafting"
            )
        plan_coordinates = (
            plan.run_id,
            plan.batch_id,
            plan.state_id,
            plan.cutoff,
        )
        state_coordinates = (
            state.run_id,
            state.batch_id,
            state.state_id,
            state.cutoff,
        )
        action_coordinates = (
            action.run_id,
            action.batch_id,
            action.state_id,
            state.cutoff,
        )
        prior_coordinates = (
            prior.run_id,
            prior.batch_id,
            prior.state_id,
            prior.cutoff,
        )
        if (
            plan_coordinates != state_coordinates
            or action_coordinates != state_coordinates
            or prior_coordinates != state_coordinates
            or batch.state_id != state.state_id
        ):
            raise PersistenceConflict(
                "evaluation plan differs from the frozen action and SearchState"
            )
        for existing in self.database.evaluation_plans.values():
            if existing.record_id == plan.record_id:
                continue
            if existing.action_id == plan.action_id:
                raise PersistenceConflict(
                    "action already has a different evaluation plan"
                )
        _put_immutable(self.database.evaluation_plans, plan.record_id, plan)

    def get(self, record_id: str) -> FrozenActionEvaluationPlan:
        return self.database.evaluation_plans[record_id]

    def plan_for_action(
        self, action_id: str
    ) -> FrozenActionEvaluationPlan | None:
        matches = tuple(
            item
            for item in self.database.evaluation_plans.values()
            if item.action_id == action_id
        )
        if len(matches) > 1:
            raise PersistenceConflict("action has multiple evaluation plans")
        return matches[0] if matches else None

    def list_for_batch(
        self, batch_id: str
    ) -> tuple[FrozenActionEvaluationPlan, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.database.evaluation_plans.values()
                    if item.batch_id == batch_id
                ),
                key=lambda item: (
                    self.database.actions[item.action_id].decision_round_id,
                    item.action_id,
                ),
            )
        )


class _PlanningFailureRepository:
    def __init__(self, database: InMemoryDatabase):
        self.database = database

    def add(self, event: PlanningFailureEvent) -> None:
        batch = self.database.batches.get(event.batch_id)
        state = self.database.states.get(event.state_id)
        if batch is None or state is None:
            raise PersistenceConflict(
                "planning failure needs a persisted state and batch"
            )
        lifecycle = (
            batch.resume_from
            if batch.status is BatchStatus.INTERRUPTED
            else batch.status
        )
        if lifecycle is not BatchStatus.DRAFTING:
            raise PersistenceConflict(
                "planning failure can only be appended while drafting"
            )
        expected = (
            batch.run_id,
            batch.batch_id,
            batch.state_id,
            batch.method,
            state.compatibility,
        )
        actual = (
            event.run_id,
            event.batch_id,
            event.state_id,
            event.method,
            event.compatibility,
        )
        if actual != expected:
            raise PersistenceConflict(
                "planning failure coordinates differ from batch state"
            )
        if event.round_index >= batch.expected_rounds:
            raise PersistenceConflict("planning failure round exceeds batch bounds")
        for plan in self.database.plans.values():
            if (
                plan.action.batch_id == event.batch_id
                and plan.action.decision_round_id == event.decision_round_id
            ):
                raise PersistenceConflict(
                    "planning failure round already has a selected action"
                )
        for current in self.database.planning_failures.values():
            if current.event_id == event.event_id:
                continue
            if (
                current.batch_id == event.batch_id
                and current.decision_round_id == event.decision_round_id
            ):
                raise PersistenceConflict(
                    "decision round already has a planning failure"
                )
        _put_immutable(self.database.planning_failures, event.event_id, event)

    def get(self, event_id: str) -> PlanningFailureEvent:
        return self.database.planning_failures[event_id]

    def list_for_batch(self, batch_id: str) -> tuple[PlanningFailureEvent, ...]:
        return tuple(
            sorted(
                (
                    event
                    for event in self.database.planning_failures.values()
                    if event.batch_id == batch_id
                ),
                key=lambda event: (event.round_index, event.event_id),
            )
        )

    def list_for_run(self, run_id: str) -> tuple[PlanningFailureEvent, ...]:
        return tuple(
            sorted(
                (
                    event
                    for event in self.database.planning_failures.values()
                    if event.run_id == run_id
                ),
                key=lambda event: (
                    event.available_at,
                    event.batch_id,
                    event.round_index,
                    event.event_id,
                ),
            )
        )


class _SemanticReservationRepository:
    def __init__(self, database: InMemoryDatabase):
        self.database = database

    def add(self, semantic: PendingSemanticReservation) -> None:
        reservation = semantic.reservation
        action = self.database.actions.get(reservation.action_id)
        if action is None:
            raise PersistenceConflict(
                "semantic reservation action must be persisted first"
            )
        batch = self.database.batches.get(reservation.batch_id)
        if batch is None:
            raise PersistenceConflict("semantic reservation has no batch")
        if (
            action.method is not PolicyMethod.UCB
            or batch.method is not PolicyMethod.UCB
        ):
            raise PersistenceConflict("only UCB actions can carry reservations")
        if action.source_slot is None or action.direction_match is None:
            raise PersistenceConflict("reserved UCB action lacks typed match")
        expected = (
            action.action_id,
            action.batch_id,
            action.decision_round_id,
            action.arm_id,
            action.direction.direction_id,
            action.source_slot.kind,
            action.created_at,
        )
        actual = (
            reservation.action_id,
            reservation.batch_id,
            reservation.decision_round_id,
            reservation.arm_id,
            reservation.direction_id,
            reservation.source_slot_kind,
            reservation.created_at,
        )
        if actual != expected:
            raise PersistenceConflict(
                "semantic reservation coordinates differ from action"
            )
        for existing in self.database.semantic_reservations.values():
            other = existing.reservation
            if other.reservation_id == reservation.reservation_id:
                continue
            if other.action_id == reservation.action_id:
                raise PersistenceConflict("action already has a reservation")
            if other.decision_round_id == reservation.decision_round_id:
                raise PersistenceConflict("decision round already has a reservation")
        _put_immutable(
            self.database.semantic_reservations,
            reservation.reservation_id,
            semantic,
        )

    def get(self, reservation_id: str) -> PendingSemanticReservation:
        return self.database.semantic_reservations[reservation_id]

    def replace(self, semantic: PendingSemanticReservation) -> None:
        reservation = semantic.reservation
        current_semantic = self.database.semantic_reservations.get(
            reservation.reservation_id
        )
        current = current_semantic.reservation if current_semantic is not None else None
        if current is None:
            raise KeyError(reservation.reservation_id)
        immutable = (
            "reservation_id",
            "action_id",
            "batch_id",
            "decision_round_id",
            "arm_id",
            "direction_id",
            "created_at",
            "source_slot_kind",
            "joined_existing_pending",
        )
        if any(
            getattr(reservation, name) != getattr(current, name) for name in immutable
        ):
            raise PersistenceConflict("reservation immutable coordinates changed")
        semantic_immutable = (
            "canonical_input",
            "embedding",
            "embedding_identity",
            "threshold_id",
            "compatibility",
            "applicability",
            "establishes_new_arm",
        )
        if any(
            getattr(semantic, name) != getattr(current_semantic, name)
            for name in semantic_immutable
        ):
            raise PersistenceConflict("semantic reservation identity changed")
        if reservation.state is current.state:
            if semantic != current_semantic:
                raise PersistenceConflict(
                    "reservation lifecycle replay changed timestamps"
                )
            return
        allowed = {
            ReservationState.PLANNED: {
                ReservationState.DISPATCHED,
                ReservationState.RECLAIMED,
            },
            ReservationState.DISPATCHED: {ReservationState.COMPLETED},
            ReservationState.COMPLETED: set(),
            ReservationState.RECLAIMED: set(),
        }
        if reservation.state not in allowed[current.state]:
            raise PersistenceConflict(
                "invalid reservation transition "
                f"{current.state.value}->{reservation.state.value}"
            )
        if (
            current.state is ReservationState.DISPATCHED
            and reservation.dispatched_at != current.dispatched_at
        ):
            raise PersistenceConflict("reservation dispatch timestamp changed")
        self.database.semantic_reservations[reservation.reservation_id] = semantic

    def list_for_batch(self, batch_id: str) -> tuple[PendingSemanticReservation, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.database.semantic_reservations.values()
                    if item.reservation.batch_id == batch_id
                ),
                key=lambda item: (
                    item.reservation.decision_round_id,
                    item.reservation.reservation_id,
                ),
            )
        )


class _OutcomeRepository:
    def __init__(self, database: InMemoryDatabase):
        self.database = database

    def add_worker(self, result: WorkerResult) -> None:
        _put_immutable(self.database.workers, result.action_id, result)

    def add_outcome(self, outcome: PairedOutcome) -> None:
        action = self.database.actions.get(outcome.action_id)
        if action is None:
            raise PersistenceConflict("paired outcome has no persisted action")
        if outcome.evidence_tier is EvidenceTier.ADAPTIVE_B:
            plans = tuple(
                plan
                for plan in self.database.evaluation_plans.values()
                if plan.action_id == outcome.action_id
            )
            if len(plans) != 1:
                raise PersistenceConflict(
                    "adaptive-B outcome needs one frozen evaluation plan"
                )
            if outcome.evaluation_plan_record_id != plans[0].record_id:
                raise PersistenceConflict(
                    "adaptive-B outcome differs from its frozen evaluation plan"
                )
        _put_immutable(self.database.outcomes, outcome.action_id, outcome)

    def add_effectiveness(self, observation: EffectivenessObservation) -> None:
        _put_immutable(self.database.effectiveness, observation.action_id, observation)

    def worker_for_action(self, action_id: str) -> WorkerResult | None:
        return self.database.workers.get(action_id)

    def outcome_for_action(self, action_id: str) -> PairedOutcome | None:
        return self.database.outcomes.get(action_id)

    def effectiveness_for_action(
        self, action_id: str
    ) -> EffectivenessObservation | None:
        return self.database.effectiveness.get(action_id)


class _ObservationRepository:
    def __init__(self, database: InMemoryDatabase):
        self.database = database

    def add(self, observation: PolicyObservation) -> None:
        action = self.database.actions.get(observation.action_id)
        if action is None:
            raise PersistenceConflict("policy observation has no persisted action")
        batch = self.database.batches.get(action.batch_id)
        if batch is None or batch.status is not BatchStatus.RUNNING:
            raise PersistenceConflict(
                "policy observation must be published by a running " "batch barrier"
            )
        source_slot_kind = (
            action.source_slot.kind if action.source_slot is not None else None
        )
        expected = (
            action.method,
            action.parent_id,
            action.arm_id,
            source_slot_kind,
            action.direction_match,
        )
        actual = (
            observation.method,
            observation.parent_id,
            observation.arm_id,
            observation.source_slot_kind,
            observation.direction_match,
        )
        if actual != expected:
            raise PersistenceConflict(
                "policy observation coordinates differ from selected action"
            )
        state = self.database.states.get(action.state_id)
        plans = tuple(
            plan
            for plan in self.database.plans.values()
            if plan.action.action_id == action.action_id
        )
        if state is None or len(plans) != 1:
            raise PersistenceConflict(
                "policy observation needs one persisted typed plan"
            )
        plan = plans[0]
        selected = tuple(
            candidate
            for candidate in plan.parent_slate.candidates
            if candidate.parent_id == plan.parent_selection.parent_id
            and candidate.commit_sha == plan.parent_selection.commit_sha
        )
        if len(selected) != 1:
            raise PersistenceConflict(
                "typed plan has no unique selected parent candidate"
            )
        if (
            observation.compatibility != state.compatibility
            or observation.parent_descriptor != selected[0].descriptor
            or observation.direction_descriptor != state.direction_descriptor
            or observation.available_at != observation.effectiveness.available_at
        ):
            raise PersistenceConflict(
                "policy observation state/evidence coordinates drifted"
            )
        _put_immutable(
            self.database.observations, observation.observation_id, observation
        )
        existing_batch = self.database.observation_batches.get(
            observation.observation_id
        )
        if existing_batch is not None and existing_batch != action.batch_id:
            raise PersistenceConflict("policy observation batch identity drift")
        self.database.observation_batches[observation.observation_id] = action.batch_id

    def list_for_batch(self, batch_id: str) -> tuple[PolicyObservation, ...]:
        batch = self.database.batches.get(batch_id)
        if batch is None or batch.status not in {
            BatchStatus.AT_BARRIER,
            BatchStatus.MERGED,
            BatchStatus.POLICY_UPDATED,
            BatchStatus.INTERRUPTED,
        }:
            return ()
        if batch.status is BatchStatus.INTERRUPTED and batch.resume_from not in {
            BatchStatus.AT_BARRIER,
            BatchStatus.MERGED,
        }:
            return ()
        return tuple(
            sorted(
                (
                    self.database.observations[observation_id]
                    for observation_id, owner_batch in (
                        self.database.observation_batches.items()
                    )
                    if owner_batch == batch_id
                ),
                key=lambda item: (item.available_at, item.action_id),
            )
        )


class _FeasibilityRepository:
    def __init__(self, database: InMemoryDatabase):
        self.database = database

    def add(self, observation: FeasibilityObservation) -> None:
        if observation.action_id not in self.database.actions:
            raise PersistenceConflict("feasibility observation has no durable action")
        _put_immutable(
            self.database.feasibility,
            observation.action_id,
            observation,
        )

    def for_action(self, action_id: str) -> FeasibilityObservation | None:
        return self.database.feasibility.get(action_id)

    def list_for_batch(self, batch_id: str) -> tuple[FeasibilityObservation, ...]:
        action_ids = {
            action.action_id
            for action in self.database.actions.values()
            if action.batch_id == batch_id
        }
        return tuple(
            sorted(
                (
                    observation
                    for action_id, observation in self.database.feasibility.items()
                    if action_id in action_ids
                ),
                key=lambda item: (item.available_at, item.action_id),
            )
        )


class _ArchiveRepository:
    def __init__(self, database: InMemoryDatabase):
        self.database = database

    def add_transition(self, transition: ArchiveTransition) -> None:
        _put_immutable(
            self.database.archive_transitions, transition.action_id, transition
        )

    def add_lineage(self, edge: LineageEdge) -> None:
        _put_immutable(self.database.lineage_edges, edge.action_id, edge)

    def get_transition(self, action_id: str) -> ArchiveTransition:
        return self.database.archive_transitions[action_id]

    def lineage_for_action(self, action_id: str) -> LineageEdge | None:
        return self.database.lineage_edges.get(action_id)


class _SearchReadModelRepository:
    def __init__(self, database: InMemoryDatabase):
        self.database = database

    def add_program(self, record: SearchProgramRecord) -> None:
        existing = self.database.search_programs.get(record.record_id)
        if existing is not None:
            _put_immutable(self.database.search_programs, record.record_id, record)
            return
        _require_search_open(
            self.database,
            record.run_id,
            subject="search programs",
        )
        run_records = tuple(
            current
            for current in self.database.search_programs.values()
            if current.run_id == record.run_id
        )
        if run_records:
            if any(
                current.evidence.compatibility != record.evidence.compatibility
                for current in run_records
            ):
                raise PersistenceConflict(
                    "search program compatibility differs within a run"
                )
            expected_seq = (
                max(current.parent.admitted_seq for current in run_records) + 1
            )
        else:
            expected_seq = 0
        if record.parent.admitted_seq != expected_seq:
            raise PersistenceConflict(
                "search program archive_seq is not the next run sequence"
            )
        candidate = record.parent.candidate
        for current in run_records:
            other = current.parent.candidate
            if other.parent_id == candidate.parent_id:
                raise PersistenceConflict("search parent identity already exists")
            if other.commit_sha == candidate.commit_sha:
                raise PersistenceConflict("search program commit already exists")
            if (
                record.source_action_id is not None
                and current.source_action_id == record.source_action_id
            ):
                raise PersistenceConflict(
                    "search program source action already materialized"
                )
        if record.source_action_id is not None:
            action = self.database.actions.get(record.source_action_id)
            worker = self.database.workers.get(record.source_action_id)
            outcome = self.database.outcomes.get(record.source_action_id)
            transition = self.database.archive_transitions.get(record.source_action_id)
            lineage = self.database.lineage_edges.get(record.source_action_id)
            if any(
                item is None for item in (action, worker, outcome, transition, lineage)
            ):
                raise PersistenceConflict(
                    "materialized child needs complete archive facts"
                )
            if (
                action.run_id != record.run_id
                or worker.result_commit_sha != candidate.commit_sha
                or not transition.admitted
                or transition.child_commit_sha != candidate.commit_sha
                or lineage.child_commit_sha != candidate.commit_sha
                or lineage.parent_commit_sha != action.parent_commit_sha
            ):
                raise PersistenceConflict(
                    "materialized child differs from archive transition"
                )
        _put_immutable(self.database.search_programs, record.record_id, record)

    def get_program(self, record_id: str) -> SearchProgramRecord:
        return self.database.search_programs[record_id]

    def list_programs_for_run(self, run_id: str) -> tuple[SearchProgramRecord, ...]:
        return tuple(
            sorted(
                (
                    record
                    for record in self.database.search_programs.values()
                    if record.run_id == run_id
                ),
                key=lambda record: (
                    record.parent.admitted_seq,
                    record.record_id,
                ),
            )
        )

    def add_snapshot(self, record: FrozenSearchInputRecord) -> None:
        existing = self.database.search_input_snapshots.get(record.snapshot_id)
        if existing is not None:
            _put_immutable(
                self.database.search_input_snapshots,
                record.snapshot_id,
                record,
            )
            return
        _require_search_open(
            self.database,
            record.run_id,
            subject="search snapshots",
        )
        for current in self.database.search_input_snapshots.values():
            if current.snapshot_id == record.snapshot_id:
                continue
            if current.run_id == record.run_id and current.batch_id == record.batch_id:
                raise PersistenceConflict(
                    "search input batch already has another snapshot"
                )
        selected = tuple(
            self.database.search_programs.get(record_id)
            for record_id in record.program_record_ids
        )
        if any(item is None for item in selected):
            raise PersistenceConflict("search snapshot references missing programs")
        if any(
            item.run_id != record.run_id
            or item.evidence.compatibility != record.compatibility
            or item.parent.admitted_seq > record.archive_seq
            or not record.cutoff.allows(item.parent.available_at)
            for item in selected
        ):
            raise PersistenceConflict("search snapshot contains an ineligible program")
        visible = tuple(
            item
            for item in self.list_programs_for_run(record.run_id)
            if item.evidence.compatibility == record.compatibility
            and record.cutoff.allows(item.parent.available_at)
        )
        if not visible:
            raise PersistenceConflict("search snapshot has no visible root")
        if record.archive_seq != max(item.parent.admitted_seq for item in visible):
            raise PersistenceConflict("search snapshot archive_seq is stale")
        expected_ids = tuple(item.record_id for item in visible)
        if record.program_record_ids != expected_ids:
            raise PersistenceConflict(
                "search snapshot does not freeze the full visible archive"
            )
        visible_history = []
        batches = tuple(
            sorted(
                (
                    batch
                    for batch in self.database.batches.values()
                    if batch.run_id == record.run_id
                ),
                key=lambda batch: (batch.created_at, batch.batch_id),
            )
        )
        for batch in batches:
            for observation in _ObservationRepository(self.database).list_for_batch(
                batch.batch_id
            ):
                if not record.cutoff.allows(observation.available_at):
                    continue
                action = self.database.actions.get(observation.action_id)
                if action is None:
                    raise PersistenceConflict(
                        "direction history observation has no durable action"
                    )
                visible_history.append(
                    DirectionHistoryRecord.from_completed(
                        action,
                        observation,
                        trace_manifest_id=resolve_rejected_candidate_manifest_id(
                            _TraceEvidenceRepository(self.database), observation
                        ),
                    )
                )
        expected_history = tuple(
            sorted(
                visible_history,
                key=lambda item: (item.available_at, item.history_id),
            )
        )
        if record.direction_history_records != expected_history:
            raise PersistenceConflict(
                "search snapshot does not freeze the full visible direction history"
            )
        _put_immutable(
            self.database.search_input_snapshots,
            record.snapshot_id,
            record,
        )

    def snapshot_for_batch(
        self, run_id: str, batch_id: str
    ) -> FrozenSearchInputRecord | None:
        matches = tuple(
            record
            for record in self.database.search_input_snapshots.values()
            if record.run_id == run_id and record.batch_id == batch_id
        )
        if len(matches) > 1:
            raise PersistenceConflict(
                "search input batch has multiple durable snapshots"
            )
        return matches[0] if matches else None


class _TraceEvidenceRepository:
    def __init__(self, database: InMemoryDatabase):
        self.database = database

    @staticmethod
    def _capture_id(result: TraceCaptureResult) -> str:
        return stable_id(
            "trace-capture",
            result.record.adapter_id,
            result.record.program_id,
            result.record.cell_id,
            result.record.status.value,
            result.record.trace_ref_id,
            result.record.reason,
        )

    def add_capture(self, result: TraceCaptureResult) -> None:
        if not isinstance(result, TraceCaptureResult):
            raise TypeError("trace capture repository needs TraceCaptureResult")
        _put_immutable(
            self.database.trace_captures,
            self._capture_id(result),
            result,
        )

    def list_captures_for_program(
        self, program_id: str
    ) -> tuple[TraceCaptureResult, ...]:
        return tuple(
            sorted(
                (
                    result
                    for result in self.database.trace_captures.values()
                    if result.record.program_id == program_id
                ),
                key=lambda result: (
                    result.record.cell_id,
                    result.record.status.value,
                    result.record.trace_ref_id or "",
                ),
            )
        )

    def add_manifest(self, manifest: TraceEvidenceManifest) -> None:
        if not isinstance(manifest, TraceEvidenceManifest):
            raise TypeError("trace manifest repository needs TraceEvidenceManifest")
        captured_artifacts = {
            result.record.trace_ref_id: result.artifact_ref
            for result in self.database.trace_captures.values()
            if result.record.program_id == manifest.program_id
            and result.record.trace_ref_id is not None
        }
        if any(
            captured_artifacts.get(item.trace_ref_id) != item
            for item in manifest.artifact_refs
        ):
            raise PersistenceConflict(
                "trace manifest artifact differs from its durable capture"
            )
        _put_immutable(
            self.database.trace_manifests,
            manifest.manifest_id,
            manifest,
        )

    def get_manifest(self, manifest_id: str) -> TraceEvidenceManifest:
        return self.database.trace_manifests[manifest_id]

    def list_manifests_for_program(
        self, program_id: str
    ) -> tuple[TraceEvidenceManifest, ...]:
        return tuple(
            sorted(
                (
                    manifest
                    for manifest in self.database.trace_manifests.values()
                    if manifest.program_id == program_id
                ),
                key=lambda manifest: (manifest.available_at, manifest.manifest_id),
            )
        )

    def bind_snapshot(self, binding: SnapshotTraceBinding) -> None:
        if not isinstance(binding, SnapshotTraceBinding):
            raise TypeError("trace binding repository needs SnapshotTraceBinding")
        snapshot = self.database.search_input_snapshots.get(binding.snapshot_id)
        if snapshot is None:
            raise PersistenceConflict("trace binding references a missing snapshot")
        manifests = tuple(
            self.database.trace_manifests.get(manifest_id)
            for manifest_id in binding.manifest_ids
        )
        if any(manifest is None for manifest in manifests):
            raise PersistenceConflict("trace binding references a missing manifest")
        programs = {
            record.parent.candidate.parent_id: record
            for record_id in snapshot.program_record_ids
            for record in (self.database.search_programs[record_id],)
        }
        trees = {
            (item.parent_id, item.commit_sha): item.tree_sha
            for item in snapshot.repository_maps
        }
        rejected = {
            record.trace_manifest_id
            for record in snapshot.direction_history_records
            if record.trace_manifest_id is not None
        }
        if any(
            manifest.compatibility != snapshot.compatibility
            or not snapshot.cutoff.allows(manifest.available_at)
            or (
                manifest.manifest_id not in rejected
                and (
                    manifest.program_id not in programs
                    or manifest.commit_sha
                    != programs[manifest.program_id].parent.candidate.commit_sha
                    or manifest.tree_sha
                    != trees.get((manifest.program_id, manifest.commit_sha))
                )
            )
            for manifest in manifests
        ):
            raise PersistenceConflict(
                "trace binding contains an ineligible or stale manifest"
            )
        _put_immutable(
            self.database.snapshot_trace_bindings,
            binding.snapshot_id,
            binding,
        )

    def binding_for_snapshot(self, snapshot_id: str) -> SnapshotTraceBinding | None:
        return self.database.snapshot_trace_bindings.get(snapshot_id)


class _UsageRepository:
    def __init__(self, database: InMemoryDatabase):
        self.database = database

    def add_intent(self, intent: ProviderCallIntent) -> None:
        _put_immutable(self.database.provider_call_intents, intent.call_id, intent)

    def mark_dispatched(self, dispatch: ProviderCallDispatch) -> None:
        if dispatch.call_id not in self.database.provider_call_intents:
            raise PersistenceConflict("provider call dispatch has no durable intent")
        _put_immutable(
            self.database.provider_call_dispatches,
            dispatch.call_id,
            dispatch,
        )

    def get_dispatch(self, call_id: str) -> ProviderCallDispatch | None:
        return self.database.provider_call_dispatches.get(call_id)

    def record_refusal(self, refusal: ProviderCallRefusal) -> None:
        """Append refusal evidence and retract the dispatch it invalidates."""

        if refusal.call_id not in self.database.provider_call_intents:
            raise PersistenceConflict("provider call refusal has no durable intent")
        if refusal.call_id in self.database.provider_call_results:
            raise PersistenceConflict(
                "a provider call with a durable result cannot be refused"
            )
        if refusal.ordinal != len(self.list_refusals(refusal.call_id)):
            raise PersistenceConflict(
                "provider call refusals must be appended in order"
            )
        _put_immutable(
            self.database.provider_call_refusals,
            f"{refusal.call_id}:{refusal.ordinal}",
            refusal,
        )
        self.database.provider_call_dispatches.pop(refusal.call_id, None)

    def list_refusals(self, call_id: str) -> tuple[ProviderCallRefusal, ...]:
        return tuple(
            sorted(
                (
                    refusal
                    for refusal in self.database.provider_call_refusals.values()
                    if refusal.call_id == call_id
                ),
                key=lambda refusal: refusal.ordinal,
            )
        )

    def add_result(self, result: ProviderCallResult) -> None:
        if result.call_id not in self.database.provider_call_intents:
            raise PersistenceConflict("provider call result has no durable intent")
        if result.call_id not in self.database.provider_call_dispatches:
            raise PersistenceConflict("provider call result has no durable dispatch")
        _put_immutable(self.database.provider_call_results, result.call_id, result)

    def transition_result(self, result: ProviderCallResult) -> None:
        existing = self.database.provider_call_results.get(result.call_id)
        if existing is None:
            raise PersistenceConflict("provider call transition has no durable receipt")
        if existing.status is not ProviderCallStatus.ACCOUNTING_PENDING:
            raise PersistenceConflict(
                "only an accounting-pending provider call can transition"
            )
        if result.status not in {
            ProviderCallStatus.COMPLETED,
            ProviderCallStatus.FAILED,
        }:
            raise PersistenceConflict(
                "accounting-pending provider call must transition to terminal"
            )
        for name in (
            "call_id",
            "completed_at",
            "input_tokens",
            "output_tokens",
            "provider_request_id",
            "response_text",
            "finish_reason",
        ):
            if getattr(existing, name) != getattr(result, name):
                raise PersistenceConflict(
                    "provider response receipt changed during accounting transition"
                )
        if result.status is ProviderCallStatus.FAILED and (
            result.cost_usd != existing.cost_usd
            or result.accounting_status != existing.accounting_status
        ):
            raise PersistenceConflict(
                "failed accounting transition changed unverified accounting facts"
            )
        self.database.provider_call_results[result.call_id] = result

    def add_cost(self, entry: CostLedgerEntry) -> None:
        result = self.database.provider_call_results.get(entry.call_id)
        if result is None:
            raise PersistenceConflict("cost entry has no provider call result")
        if result.status is ProviderCallStatus.ACCOUNTING_PENDING:
            raise PersistenceConflict(
                "accounting-pending call cannot enter cost ledger"
            )
        intent = self.database.provider_call_intents[entry.call_id]
        if (
            entry.run_id != intent.run_id
            or entry.batch_id != intent.batch_id
            or entry.action_id != intent.action_id
            or entry.purpose != intent.purpose
        ):
            raise PersistenceConflict(
                "cost entry ownership differs from provider call intent"
            )
        if (
            result.cost_usd is None
            or entry.cost_usd != result.cost_usd
            or entry.accounting_status != result.accounting_status
        ):
            raise PersistenceConflict(
                "cost entry differs from provider call result accounting"
            )
        _put_immutable(self.database.cost_entries, entry.entry_id, entry)

    def get_intent(self, call_id: str) -> ProviderCallIntent:
        return self.database.provider_call_intents[call_id]

    def get_result(self, call_id: str) -> ProviderCallResult | None:
        return self.database.provider_call_results.get(call_id)

    def list_intents_for_run(self, run_id: str) -> tuple[ProviderCallIntent, ...]:
        call_ids = {
            intent.call_id
            for intent in self.database.provider_call_intents.values()
            if intent.run_id == run_id
        }
        return tuple(
            sorted(
                (self.database.provider_call_intents[call_id] for call_id in call_ids),
                key=lambda item: (item.created_at, item.call_id),
            )
        )

    def list_results_for_run(self, run_id: str) -> tuple[ProviderCallResult, ...]:
        call_ids = {
            intent.call_id
            for intent in self.database.provider_call_intents.values()
            if intent.run_id == run_id
        }
        return tuple(
            sorted(
                (
                    self.database.provider_call_results[call_id]
                    for call_id in call_ids
                    if call_id in self.database.provider_call_results
                ),
                key=lambda item: (item.completed_at, item.call_id),
            )
        )

    def list_dispatches_for_run(self, run_id: str) -> tuple[ProviderCallDispatch, ...]:
        call_ids = {
            intent.call_id
            for intent in self.database.provider_call_intents.values()
            if intent.run_id == run_id
        }
        return tuple(
            sorted(
                (
                    self.database.provider_call_dispatches[call_id]
                    for call_id in call_ids
                    if call_id in self.database.provider_call_dispatches
                ),
                key=lambda item: (item.dispatched_at, item.call_id),
            )
        )

    def list_costs_for_run(self, run_id: str) -> tuple[CostLedgerEntry, ...]:
        return tuple(
            sorted(
                (
                    entry
                    for entry in self.database.cost_entries.values()
                    if entry.run_id == run_id
                ),
                key=lambda entry: (entry.created_at, entry.entry_id),
            )
        )


class InMemoryUnitOfWork:
    """One coarse transaction, adequate for deterministic contract tests."""

    def __init__(
        self,
        database: InMemoryDatabase,
        *,
        sealed_authorization_guard: Callable[[SealedAuthorization], bool] | None = None,
    ):
        self.database = database
        self.states = _StateRepository(database)
        self.batches = _BatchRepository(database)
        self.actions = _ActionRepository(database)
        self.plans = _PlanRepository(database)
        self.evaluation_priors = _EvaluationPriorRepository(database)
        self.evaluation_plans = _ActionEvaluationPlanRepository(database)
        self.planning_failures = _PlanningFailureRepository(database)
        self.semantic_reservations = _SemanticReservationRepository(database)
        self.outcomes = _OutcomeRepository(database)
        self.observations = _ObservationRepository(database)
        self.feasibility = _FeasibilityRepository(database)
        self.archive = _ArchiveRepository(database)
        self.search_read_model = _SearchReadModelRepository(database)
        self.trace_evidence = _TraceEvidenceRepository(database)
        self.usage = _UsageRepository(database)
        self.sealed = InMemorySealedRepository(
            database.sealed_records,
            copy_records=False,
            registration_guard=lambda run_id: any(
                item.run_id == run_id
                for table in (
                    database.batches,
                    database.states,
                    database.search_programs,
                )
                for item in table.values()
            ),
            authorization_guard=(
                sealed_authorization_guard or (lambda _authorization: False)
            ),
        )
        self._snapshot: dict[str, Any] | None = None

    def __enter__(self) -> "InMemoryUnitOfWork":
        self.database.lock.acquire()
        self._snapshot = self.database._snapshot()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if exc_type is not None and self._snapshot is not None:
                self.database._restore(self._snapshot)
        finally:
            self._snapshot = None
            self.database.lock.release()
        return False


class InMemoryUnitOfWorkFactory:
    def __init__(self, database: InMemoryDatabase | None = None):
        self.database = database or InMemoryDatabase()

    def __call__(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self.database)


__all__ = [
    "InMemoryDatabase",
    "InMemoryUnitOfWork",
    "InMemoryUnitOfWorkFactory",
    "PersistenceConflict",
]
