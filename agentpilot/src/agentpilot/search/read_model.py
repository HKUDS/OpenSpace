"""Canonical durable records behind the batch search-input read model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from agentpilot.domain import (
    CompatibilityKey,
    EvidenceCutoff,
    ParentRepositoryMap,
    StateDescriptor,
    VisibleATaskEvidence,
    deep_freeze,
    stable_id,
)
from agentpilot.domain.common import require_text
from agentpilot.search.direction_history import DirectionHistoryRecord
from agentpilot.search.evidence_retrieval import ProgramEvidence
from agentpilot.search.parent_candidates import ParentRecord
from agentpilot.search.state import SearchFact


@dataclass(frozen=True)
class SearchProgramRecord:
    """One admitted program projected into every canonical search view."""

    record_id: str
    run_id: str
    parent: ParentRecord
    evidence: ProgramEvidence
    fact: SearchFact
    source_action_id: str | None = None

    def __post_init__(self) -> None:
        require_text(self.record_id, "record_id")
        require_text(self.run_id, "run_id")
        if self.source_action_id is not None:
            require_text(self.source_action_id, "source_action_id")
        candidate = self.parent.candidate
        expected_id = stable_id(
            "search-program", self.run_id, candidate.parent_id, candidate.commit_sha,
        )
        if self.record_id != expected_id:
            raise ValueError("search program record id is not stable")
        if (
            self.evidence.parent_id != candidate.parent_id
            or self.evidence.commit_sha != candidate.commit_sha
            or self.evidence.archive_seq != self.parent.admitted_seq
            or self.evidence.available_at != self.parent.available_at
            or self.evidence.primary_value != candidate.primary_value
        ):
            raise ValueError("search program views have drifting coordinates")
        if self.fact.available_at != self.parent.available_at:
            raise ValueError("search program fact has a different visibility time")


@dataclass(frozen=True)
class FrozenSearchInputRecord:
    """Durable coordinates of the exact source snapshot for one batch."""

    snapshot_id: str
    run_id: str
    batch_id: str
    cutoff: EvidenceCutoff
    compatibility: CompatibilityKey
    archive_seq: int
    parent_descriptor: StateDescriptor
    direction_descriptor: StateDescriptor
    program_record_ids: tuple[str, ...]
    direction_history_records: tuple[DirectionHistoryRecord, ...] = ()
    repository_maps: tuple[ParentRepositoryMap, ...] = ()
    visible_a_evidence: tuple[VisibleATaskEvidence, ...] = ()
    budget: Mapping[str, float | int] = field(default_factory=dict)
    carrier_context: str = ""

    def __post_init__(self) -> None:
        require_text(self.snapshot_id, "snapshot_id")
        require_text(self.run_id, "run_id")
        require_text(self.batch_id, "batch_id")
        if isinstance(self.archive_seq, bool) or self.archive_seq < 0:
            raise ValueError("archive_seq must be non-negative")
        record_ids = tuple(self.program_record_ids)
        object.__setattr__(self, "program_record_ids", record_ids)
        if not record_ids:
            raise ValueError("frozen search input needs a root program")
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("frozen search input has duplicate programs")
        for record_id in record_ids:
            require_text(record_id, "program_record_id")
        history = tuple(self.direction_history_records)
        object.__setattr__(self, "direction_history_records", history)
        if any(not isinstance(item, DirectionHistoryRecord) for item in history):
            raise TypeError(
                "direction_history_records must contain DirectionHistoryRecord values"
            )
        history_ids = tuple(item.history_id for item in history)
        if len(history_ids) != len(set(history_ids)):
            raise ValueError("frozen search input has duplicate direction history")
        repository_maps = tuple(self.repository_maps)
        object.__setattr__(self, "repository_maps", repository_maps)
        if any(not isinstance(item, ParentRepositoryMap) for item in repository_maps):
            raise TypeError("repository_maps must contain ParentRepositoryMap values")
        map_coordinates = tuple(
            (item.parent_id, item.commit_sha) for item in repository_maps
        )
        if len(map_coordinates) != len(set(map_coordinates)):
            raise ValueError("frozen search input has duplicate repository maps")
        visible_a_evidence = tuple(self.visible_a_evidence)
        object.__setattr__(self, "visible_a_evidence", visible_a_evidence)
        if any(
            not isinstance(item, VisibleATaskEvidence)
            for item in visible_a_evidence
        ):
            raise TypeError(
                "visible_a_evidence must contain VisibleATaskEvidence values"
            )
        evidence_cell_ids = tuple(item.cell_id for item in visible_a_evidence)
        if len(evidence_cell_ids) != len(set(evidence_cell_ids)):
            raise ValueError("frozen search input describes a Visible-A cell twice")
        if not isinstance(self.carrier_context, str):
            raise TypeError("carrier_context must be text")
        object.__setattr__(self, "budget", deep_freeze(self.budget))
        expected_id = stable_id(
            "search-input-snapshot",
            self.run_id,
            self.batch_id,
            self.cutoff,
            self.compatibility,
            self.archive_seq,
            self.parent_descriptor,
            self.direction_descriptor,
            self.program_record_ids,
            self.direction_history_records,
            self.repository_maps,
            self.visible_a_evidence,
            self.budget,
            self.carrier_context,
        )
        if self.snapshot_id != expected_id:
            raise ValueError("frozen search input id is not stable")


__all__ = ["FrozenSearchInputRecord", "SearchProgramRecord"]
