"""Carrier-neutral construction of the eligible parent slate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from agentpilot.domain import ParentCandidate, ParentSlate, SearchState, stable_id
from agentpilot.domain.common import require_aware


@dataclass(frozen=True)
class ParentRecord:
    """Archive visibility coordinates around a domain ParentCandidate."""

    candidate: ParentCandidate
    admitted_seq: int
    available_at: datetime
    searchable: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, ParentCandidate):
            raise TypeError("candidate must be ParentCandidate")
        if isinstance(self.admitted_seq, bool) or not isinstance(
            self.admitted_seq, int
        ):
            raise ValueError("admitted_seq must be an integer")
        if self.admitted_seq < 0:
            raise ValueError("admitted_seq must be non-negative")
        require_aware(self.available_at, "available_at")


class ParentSlateBuilder:
    """Apply fixed eligibility without learning a parent score."""

    def build(
        self,
        *,
        state: SearchState,
        decision_round_id: str,
        records: Iterable[ParentRecord],
    ) -> ParentSlate:
        by_parent: dict[str, ParentRecord] = {}
        for record in records:
            if not isinstance(record, ParentRecord):
                raise TypeError("parent slate records must be ParentRecord values")
            parent_id = record.candidate.parent_id
            previous = by_parent.get(parent_id)
            if previous is not None and previous != record:
                raise ValueError(f"parent id {parent_id!r} has semantic drift")
            by_parent[parent_id] = record

        candidates = tuple(
            sorted(
                (
                    record.candidate
                    for record in by_parent.values()
                    if record.searchable
                    and record.admitted_seq <= state.archive_seq
                    and state.cutoff.allows(record.available_at)
                ),
                key=lambda candidate: (candidate.commit_sha, candidate.parent_id,),
            )
        )
        slate_id = stable_id(
            "parent-slate",
            state.state_id,
            decision_round_id,
            [candidate.parent_id for candidate in candidates],
        )
        return ParentSlate(
            slate_id=slate_id,
            decision_round_id=decision_round_id,
            state_id=state.state_id,
            candidates=candidates,
        )
