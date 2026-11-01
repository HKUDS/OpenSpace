"""Freeze durable search inputs before any carrier makes a decision.

The application owns data access; search owns deterministic construction; and
policy consumes only frozen domain values. A source must return the durable
snapshot stored for a batch, never a fresh view assembled independently for
each decision round.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping, Protocol

from agentpilot.domain import (
    CompatibilityKey,
    EvidenceCutoff,
    EvidenceTier,
    ParentRepositoryMap,
    ParentSelection,
    SearchState,
    StateDescriptor,
    TraceEvidenceManifest,
    VisibleATaskEvidence,
    deep_freeze,
    stable_id,
    utc_now,
)
from agentpilot.policy.ucb import (
    UCBFrozenSnapshot,
    UCBRoundInputs,
    derive_ucb_seed,
)
from agentpilot.search import (
    FAILURE_CARD_MAX_CARDS,
    FAILURE_CARD_MAX_TEXT_CHARS,
    DirectionHistoryRecord,
    DirectionHistoryRetriever,
    FailureCardSelection,
    FixedEvidenceRetriever,
    FixedEvidenceSelection,
    ParentRecord,
    ParentSlateBuilder,
    ProgramEvidence,
    SearchFact,
)
from agentpilot.search.state import SearchStateBuilder as DomainSearchStateBuilder


@dataclass(frozen=True)
class SearchInputSnapshot:
    """One immutable, batch-scoped source snapshot shared by D1/D2/D3."""

    cutoff: EvidenceCutoff
    compatibility: CompatibilityKey
    archive_seq: int
    parent_descriptor: StateDescriptor
    direction_descriptor: StateDescriptor
    budget: Mapping[str, float | int] = field(default_factory=dict)
    facts: tuple[SearchFact, ...] = ()
    parent_records: tuple[ParentRecord, ...] = ()
    evidence_records: tuple[ProgramEvidence, ...] = ()
    direction_history_records: tuple[DirectionHistoryRecord, ...] = ()
    repository_maps: tuple[ParentRepositoryMap, ...] = ()
    visible_a_evidence: tuple[VisibleATaskEvidence, ...] = ()
    trace_manifests: tuple[TraceEvidenceManifest, ...] = ()
    carrier_context: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "budget", deep_freeze(self.budget))
        for name in (
            "facts",
            "parent_records",
            "evidence_records",
            "direction_history_records",
            "repository_maps",
            "visible_a_evidence",
            "trace_manifests",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if any(not isinstance(item, SearchFact) for item in self.facts):
            raise TypeError("facts must contain SearchFact values")
        if any(not isinstance(item, ParentRecord) for item in self.parent_records):
            raise TypeError("parent_records must contain ParentRecord values")
        if any(not isinstance(item, ProgramEvidence) for item in self.evidence_records):
            raise TypeError("evidence_records must contain ProgramEvidence values")
        if any(
            not isinstance(item, DirectionHistoryRecord)
            for item in self.direction_history_records
        ):
            raise TypeError(
                "direction_history_records must contain DirectionHistoryRecord values"
            )
        if any(not isinstance(item, ParentRepositoryMap) for item in self.repository_maps):
            raise TypeError("repository_maps must contain ParentRepositoryMap values")
        map_coordinates = tuple(
            (item.parent_id, item.commit_sha, item.tree_sha)
            for item in self.repository_maps
        )
        if len(map_coordinates) != len(set(map_coordinates)):
            raise ValueError("repository_maps contain duplicate parent coordinates")
        parent_coordinates = {
            (item.candidate.parent_id, item.candidate.commit_sha)
            for item in self.parent_records
        }
        if not {(parent_id, commit_sha) for parent_id, commit_sha, _ in map_coordinates} <= parent_coordinates:
            raise ValueError("repository map belongs to a parent outside the snapshot")
        if any(
            not isinstance(item, VisibleATaskEvidence)
            for item in self.visible_a_evidence
        ):
            raise TypeError(
                "visible_a_evidence must contain VisibleATaskEvidence values"
            )
        evidence_cell_ids = tuple(
            item.cell_id for item in self.visible_a_evidence
        )
        if len(evidence_cell_ids) != len(set(evidence_cell_ids)):
            raise ValueError("visible_a_evidence describes a cell twice")
        if any(
            not isinstance(item, TraceEvidenceManifest)
            for item in self.trace_manifests
        ):
            raise TypeError(
                "trace_manifests must contain TraceEvidenceManifest values"
            )
        manifest_ids = tuple(item.manifest_id for item in self.trace_manifests)
        if manifest_ids != tuple(sorted(set(manifest_ids))):
            raise ValueError("trace_manifests must be sorted and unique")
        # A manifest belongs to this snapshot either as a frozen parent's own
        # evidence floor or as a rejected candidate named by direction history.
        rejected_manifest_ids = {
            record.trace_manifest_id
            for record in self.direction_history_records
            if record.trace_manifest_id is not None
        }
        for manifest in self.trace_manifests:
            if (
                manifest.compatibility != self.compatibility
                or not self.cutoff.allows(manifest.available_at)
                or (
                    (manifest.program_id, manifest.commit_sha, manifest.tree_sha)
                    not in set(map_coordinates)
                    and manifest.manifest_id not in rejected_manifest_ids
                )
            ):
                raise ValueError(
                    "trace manifest is incompatible with the frozen snapshot"
                )
        measured_visible_a_cells = {
            cell_id
            for record in self.evidence_records
            if record.evidence_tier is EvidenceTier.VISIBLE_A
            for cell_id in record.cell_results
        }
        if not set(evidence_cell_ids) <= measured_visible_a_cells:
            raise ValueError(
                "visible_a_evidence describes a cell outside Visible-A measurements"
            )
        if not isinstance(self.carrier_context, str):
            raise TypeError("carrier_context must be text")

    @property
    def snapshot_id(self) -> str:
        return stable_id(
            "search-input-snapshot",
            self.cutoff,
            self.compatibility,
            self.archive_seq,
            self.parent_descriptor,
            self.direction_descriptor,
            self.budget,
            self.facts,
            self.parent_records,
            self.evidence_records,
            self.direction_history_records,
            self.repository_maps,
            self.visible_a_evidence,
            self.trace_manifests,
            self.carrier_context,
        )


class SearchInputSource(Protocol):
    """Load the already-frozen durable snapshot for a stable batch identity."""

    def load(self, *, run_id: str, batch_id: str) -> SearchInputSnapshot:
        ...


class CanonicalSearchInputs:
    """Share one exact source snapshot across state, parent, and evidence views."""

    def __init__(
        self,
        source: SearchInputSource,
        state_builder: DomainSearchStateBuilder | None = None,
    ) -> None:
        if not callable(getattr(source, "load", None)):
            raise TypeError("source must provide load()")
        self.source = source
        self.state_builder = state_builder or DomainSearchStateBuilder()
        self._snapshots: dict[tuple[str, str], SearchInputSnapshot] = {}

    def snapshot(self, *, run_id: str, batch_id: str) -> SearchInputSnapshot:
        key = (run_id, batch_id)
        current = self._snapshots.get(key)
        if current is not None:
            return current
        loaded = self.source.load(run_id=run_id, batch_id=batch_id)
        if not isinstance(loaded, SearchInputSnapshot):
            raise TypeError("search input source returned the wrong type")
        self._snapshots[key] = loaded
        return loaded

    def state(self, *, run_id: str, batch_id: str) -> SearchState:
        snapshot = self.snapshot(run_id=run_id, batch_id=batch_id)
        identity_fact = SearchFact(
            fact_id=snapshot.snapshot_id,
            kind="search_input_snapshot",
            available_at=snapshot.cutoff.available_at,
            evidence_tier=EvidenceTier.VISIBLE_A,
            payload={"snapshot_id": snapshot.snapshot_id},
        )
        return self.state_builder.build(
            run_id=run_id,
            batch_id=batch_id,
            cutoff=snapshot.cutoff,
            compatibility=snapshot.compatibility,
            archive_seq=snapshot.archive_seq,
            parent_descriptor=snapshot.parent_descriptor,
            direction_descriptor=snapshot.direction_descriptor,
            budget=snapshot.budget,
            facts=(*snapshot.facts, identity_fact),
        )

    def for_state(self, state: SearchState) -> SearchInputSnapshot:
        snapshot = self.snapshot(run_id=state.run_id, batch_id=state.batch_id)
        rebuilt = self.state(run_id=state.run_id, batch_id=state.batch_id)
        if rebuilt.state_id != state.state_id:
            raise ValueError("search input snapshot differs from the frozen state")
        return snapshot


class CanonicalSearchStateBuilder:
    """BatchLoop adapter around the shared frozen search inputs."""

    def __init__(self, inputs: CanonicalSearchInputs) -> None:
        if not isinstance(inputs, CanonicalSearchInputs):
            raise TypeError("inputs must be CanonicalSearchInputs")
        self.inputs = inputs

    def build(self, *, run_id: str, batch_id: str) -> SearchState:
        return self.inputs.state(run_id=run_id, batch_id=batch_id)


class CanonicalParentSlateProvider:
    """Use one fixed archive eligibility rule for every policy carrier."""

    def __init__(
        self, inputs: CanonicalSearchInputs, builder: ParentSlateBuilder | None = None,
    ) -> None:
        if not isinstance(inputs, CanonicalSearchInputs):
            raise TypeError("inputs must be CanonicalSearchInputs")
        self.inputs = inputs
        self.builder = builder or ParentSlateBuilder()

    def build(self, *, state: SearchState, decision_round_id: str):
        snapshot = self.inputs.for_state(state)
        return self.builder.build(
            state=state,
            decision_round_id=decision_round_id,
            records=snapshot.parent_records,
        )


class CanonicalEvidenceProvider:
    """Use the same bounded top/complement evidence channels for all carriers."""

    def __init__(
        self,
        inputs: CanonicalSearchInputs,
        retriever: FixedEvidenceRetriever | None = None,
    ) -> None:
        if not isinstance(inputs, CanonicalSearchInputs):
            raise TypeError("inputs must be CanonicalSearchInputs")
        self.inputs = inputs
        self.retriever = retriever or FixedEvidenceRetriever()

    def records(self, *, state: SearchState) -> tuple[ProgramEvidence, ...]:
        return self.inputs.for_state(state).evidence_records

    def retrieve(
        self, *, state: SearchState, selected_parent_id: str
    ) -> FixedEvidenceSelection:
        return self.retriever.retrieve(
            state=state,
            selected_parent_id=selected_parent_id,
            records=self.records(state=state),
        )


class CanonicalRepositoryMapProvider:
    """Select the parent-specific repo map from the frozen batch snapshot."""

    def __init__(self, inputs: CanonicalSearchInputs) -> None:
        if not isinstance(inputs, CanonicalSearchInputs):
            raise TypeError("inputs must be CanonicalSearchInputs")
        self.inputs = inputs

    def get(
        self, *, state: SearchState, parent: ParentSelection
    ) -> ParentRepositoryMap:
        if parent.state_id != state.state_id:
            raise ValueError("parent selection belongs to another SearchState")
        snapshot = self.inputs.for_state(state)
        matches = tuple(
            item
            for item in snapshot.repository_maps
            if item.parent_id == parent.parent_id
            and item.commit_sha == parent.commit_sha
        )
        if len(matches) != 1:
            raise ValueError("selected parent has no unique frozen repository map")
        return matches[0]


class CanonicalVisibleAEvidenceProvider:
    """Read only frozen Visible-A content evidence for a search state."""

    def __init__(self, inputs: CanonicalSearchInputs) -> None:
        if not isinstance(inputs, CanonicalSearchInputs):
            raise TypeError("inputs must be CanonicalSearchInputs")
        self.inputs = inputs

    def get(self, *, state: SearchState) -> tuple[VisibleATaskEvidence, ...]:
        return self.inputs.for_state(state).visible_a_evidence


class CanonicalTraceEvidenceProvider:
    """Read only manifests bound to the state's frozen planning snapshot."""

    def __init__(self, inputs: CanonicalSearchInputs) -> None:
        if not isinstance(inputs, CanonicalSearchInputs):
            raise TypeError("inputs must be CanonicalSearchInputs")
        self.inputs = inputs

    def get(self, *, state: SearchState) -> tuple[TraceEvidenceManifest, ...]:
        return self.inputs.for_state(state).trace_manifests


class CanonicalDirectionHistoryProvider:
    """Retrieve only bounded cards from the already-frozen batch history."""

    def __init__(
        self,
        inputs: CanonicalSearchInputs,
        retriever: DirectionHistoryRetriever | None = None,
    ) -> None:
        if not isinstance(inputs, CanonicalSearchInputs):
            raise TypeError("inputs must be CanonicalSearchInputs")
        self.inputs = inputs
        self.retriever = retriever or DirectionHistoryRetriever(
            max_cards=FAILURE_CARD_MAX_CARDS,
            max_text_chars=FAILURE_CARD_MAX_TEXT_CHARS,
        )

    def retrieve(
        self, *, state: SearchState, selected_parent_id: str
    ) -> FailureCardSelection:
        snapshot = self.inputs.for_state(state)
        matches = tuple(
            record.candidate.descriptor
            for record in snapshot.parent_records
            if record.candidate.parent_id == selected_parent_id
        )
        if len(matches) != 1:
            raise ValueError("selected parent is absent from the frozen archive")
        return self.retriever.retrieve(
            state=state,
            selected_parent_id=selected_parent_id,
            selected_parent_descriptor=matches[0],
            records=snapshot.direction_history_records,
        )


class CanonicalUCBRoundInputProvider:
    """Build deterministic UCB round coordinates from shared frozen inputs."""

    def __init__(
        self,
        *,
        inputs: CanonicalSearchInputs,
        parents: CanonicalParentSlateProvider,
        evidence: CanonicalEvidenceProvider,
        base_seed: int,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if isinstance(base_seed, bool) or not isinstance(base_seed, int):
            raise ValueError("base_seed must be an integer")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.inputs = inputs
        self.parents = parents
        self.evidence = evidence
        self.base_seed = base_seed
        self.clock = clock

    def __call__(
        self,
        *,
        state: SearchState,
        carrier_snapshot: UCBFrozenSnapshot,
        decision_round_id: str,
        round_index: int,
    ) -> UCBRoundInputs:
        if not isinstance(carrier_snapshot, UCBFrozenSnapshot):
            raise TypeError("carrier_snapshot must be UCBFrozenSnapshot")
        if carrier_snapshot.state_id != state.state_id:
            raise ValueError("UCB snapshot belongs to another search state")
        snapshot = self.inputs.for_state(state)
        planned_at = self.clock()
        if not isinstance(planned_at, datetime) or planned_at.tzinfo is None:
            raise ValueError("clock must return an aware datetime")
        if planned_at <= state.cutoff.available_at:
            raise ValueError("planning clock must be strictly after the state cutoff")
        parent_slate = self.parents.build(
            state=state, decision_round_id=decision_round_id,
        )
        return UCBRoundInputs(
            parent_slate=parent_slate,
            eligible_arm_ids=carrier_snapshot.eligibility.eligible_arm_ids,
            evidence_records=self.evidence.records(state=state),
            carrier_context=snapshot.carrier_context,
            attempt_id=stable_id(
                "ucb-attempt", state.run_id, state.batch_id, decision_round_id,
            ),
            action_id=stable_id(
                "ucb-action", state.run_id, state.batch_id, decision_round_id,
            ),
            seed=derive_ucb_seed(
                self.base_seed, "round", decision_round_id, round_index,
            ),
            planned_at=planned_at,
        )


__all__ = [
    "CanonicalDirectionHistoryProvider",
    "CanonicalEvidenceProvider",
    "CanonicalParentSlateProvider",
    "CanonicalRepositoryMapProvider",
    "CanonicalSearchInputs",
    "CanonicalSearchStateBuilder",
    "CanonicalTraceEvidenceProvider",
    "CanonicalUCBRoundInputProvider",
    "CanonicalVisibleAEvidenceProvider",
    "SearchInputSnapshot",
    "SearchInputSource",
]
