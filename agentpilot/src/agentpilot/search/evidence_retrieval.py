"""Fixed top-score and failure-complement evidence retrieval.

These channels expose completed search facts to every carrier.  They do not
form an action support, update learned state, or choose a policy arm.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Mapping

from agentpilot.domain import CompatibilityKey, EvidenceTier, SearchState
from agentpilot.domain.common import deep_freeze, require_aware, require_text


@dataclass(frozen=True)
class ProgramEvidence:
    parent_id: str
    commit_sha: str
    compatibility: CompatibilityKey
    archive_seq: int
    available_at: datetime
    primary_value: float | None
    evidence_tier: EvidenceTier
    cell_results: Mapping[str, bool | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_text(self.parent_id, "parent_id")
        require_text(self.commit_sha, "commit_sha")
        require_aware(self.available_at, "available_at")
        if not isinstance(self.evidence_tier, EvidenceTier):
            raise TypeError("evidence_tier must be an EvidenceTier")
        if self.evidence_tier is EvidenceTier.SEALED_C:
            raise ValueError("sealed C evidence cannot enter fixed retrieval")
        if isinstance(self.archive_seq, bool) or self.archive_seq < 0:
            raise ValueError("archive_seq must be non-negative")
        if self.primary_value is not None and not math.isfinite(self.primary_value):
            raise ValueError("primary_value must be finite when present")
        for cell_id, result in self.cell_results.items():
            require_text(str(cell_id), "cell id")
            if result is not True and result is not False and result is not None:
                raise ValueError("cell result must be true, false, or unknown")
        object.__setattr__(self, "cell_results", deep_freeze(self.cell_results))


@dataclass(frozen=True)
class FixedEvidenceCard:
    source: str
    parent_id: str
    commit_sha: str
    primary_value: float | None
    score: float | None
    evidence_cells: tuple[str, ...] = ()

    @property
    def evidence_ref(self) -> str:
        return f"evidence:{self.source}:{self.parent_id}:{self.commit_sha}"

    def as_prompt_payload(self) -> dict[str, object]:
        return {
            "evidence_ref": self.evidence_ref,
            "source": self.source,
            "parent_id": self.parent_id,
            "commit_sha": self.commit_sha,
            "primary_value": self.primary_value,
            "score": self.score,
            "evidence_cells": list(self.evidence_cells),
        }


@dataclass(frozen=True)
class FixedEvidenceSelection:
    selected_parent_id: str
    cards: tuple[FixedEvidenceCard, ...]


class FixedEvidenceRetriever:
    """Return at most one complement and one deterministic top program."""

    def retrieve(
        self,
        *,
        state: SearchState,
        selected_parent_id: str,
        records: Iterable[ProgramEvidence],
    ) -> FixedEvidenceSelection:
        require_text(selected_parent_id, "selected_parent_id")
        by_parent: dict[str, ProgramEvidence] = {}
        for record in records:
            if not isinstance(record, ProgramEvidence):
                raise TypeError("fixed evidence records must be ProgramEvidence")
            previous = by_parent.get(record.parent_id)
            if previous is not None and previous != record:
                raise ValueError(f"parent id {record.parent_id!r} has semantic drift")
            by_parent[record.parent_id] = record

        visible = {
            parent_id: record
            for parent_id, record in by_parent.items()
            if record.compatibility == state.compatibility
            and record.archive_seq <= state.archive_seq
            and state.cutoff.allows(record.available_at)
        }
        selected = visible.get(selected_parent_id)
        if selected is None:
            raise ValueError(
                "selected parent has no compatible cutoff-visible evidence"
            )

        cards: list[FixedEvidenceCard] = []
        used = {selected_parent_id}
        failed_cells = tuple(
            sorted(
                cell_id
                for cell_id, result in selected.cell_results.items()
                if result is False
            )
        )
        complement_rows = []
        if failed_cells:
            for record in visible.values():
                if record.parent_id in used:
                    continue
                successes = tuple(
                    cell_id
                    for cell_id in failed_cells
                    if record.cell_results.get(cell_id) is True
                )
                if not successes:
                    continue
                covered = sum(
                    record.cell_results.get(cell_id) in {True, False}
                    for cell_id in failed_cells
                )
                rate = len(successes) / covered if covered else 0.0
                complement_rows.append((len(successes), rate, record, successes))
        if complement_rows:
            complement_rows.sort(
                key=lambda row: (
                    -row[0],
                    -row[1],
                    # Break equally useful complements by score only within
                    # one measurement tier, preferring the selected tier.
                    row[2].evidence_tier is not selected.evidence_tier,
                    row[2].evidence_tier.value,
                    self._primary_sort_key(state, row[2]),
                    row[2].commit_sha,
                    row[2].parent_id,
                )
            )
            success_count, _rate, record, successes = complement_rows[0]
            cards.append(
                FixedEvidenceCard(
                    source="failure_complement",
                    parent_id=record.parent_id,
                    commit_sha=record.commit_sha,
                    primary_value=(
                        record.primary_value
                        if record.evidence_tier is selected.evidence_tier
                        else None
                    ),
                    score=float(success_count),
                    evidence_cells=tuple(sorted(successes)),
                )
            )
            used.add(record.parent_id)

        top_rows = [
            record
            for record in visible.values()
            if record.parent_id not in used
            and record.primary_value is not None
            and record.evidence_tier is selected.evidence_tier
        ]
        top_rows.sort(
            key=lambda record: (
                self._primary_sort_key(state, record),
                record.commit_sha,
                record.parent_id,
            )
        )
        if top_rows:
            record = top_rows[0]
            cards.append(
                FixedEvidenceCard(
                    source="top_score",
                    parent_id=record.parent_id,
                    commit_sha=record.commit_sha,
                    primary_value=record.primary_value,
                    score=record.primary_value,
                )
            )
        return FixedEvidenceSelection(
            selected_parent_id=selected_parent_id, cards=tuple(cards),
        )

    @staticmethod
    def _primary_sort_key(state: SearchState, record: ProgramEvidence) -> float:
        if record.primary_value is None:
            return math.inf
        if state.compatibility.metric_direction == "maximize":
            return -record.primary_value
        return record.primary_value
