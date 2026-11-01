"""Shared proof for deriving final sealed coordinates from durable search facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from agentpilot.domain.common import require_aware, require_text
from agentpilot.persistence.protocols import BatchRecord, BatchStatus
from agentpilot.search.read_model import SearchProgramRecord

TERMINAL_BATCH_STATUSES = frozenset({BatchStatus.POLICY_UPDATED, BatchStatus.FAILED})


@dataclass(frozen=True)
class TerminalArchiveCoordinates:
    root_program_id: str
    champion_program_id: str
    archive_cutoff: int


def derive_terminal_archive_coordinates(
    *,
    run_id: str,
    batches: Iterable[BatchRecord],
    programs: Iterable[SearchProgramRecord],
    as_of: datetime,
) -> TerminalArchiveCoordinates:
    """Validate terminality and deterministically choose the durable champion."""

    require_text(run_id, "run_id")
    require_aware(as_of, "as_of")
    batches = tuple(batches)
    programs = tuple(programs)
    if not batches or any(batch.run_id != run_id for batch in batches):
        raise ValueError("sealed authorization needs durable run batches")
    unclosed = tuple(
        batch for batch in batches if batch.status not in TERMINAL_BATCH_STATUSES
    )
    if unclosed:
        coordinates = ", ".join(
            f"{batch.batch_id}:{batch.status.value}" for batch in unclosed
        )
        raise ValueError(
            "sealed authorization found non-terminal batches: " + coordinates
        )
    if not programs or any(program.run_id != run_id for program in programs):
        raise ValueError("sealed authorization needs durable run programs")
    latest_fact = max(
        [batch.created_at for batch in batches]
        + [program.parent.available_at for program in programs]
    )
    if as_of < latest_fact:
        raise ValueError("sealed authorization predates durable terminal facts")
    roots = tuple(item for item in programs if item.parent.admitted_seq == 0)
    if len(roots) != 1:
        raise ValueError("sealed authorization needs one durable root")
    candidates = tuple(
        item
        for item in programs
        if item.parent.searchable
        and item.parent.admitted_seq > 0
        and item.parent.candidate.primary_value is not None
    )
    if not candidates:
        raise ValueError("sealed run has no durable non-root champion")
    direction = candidates[0].evidence.compatibility.metric_direction
    if any(
        item.evidence.compatibility.metric_direction != direction for item in candidates
    ):
        raise ValueError("sealed champion candidates have mixed metric directions")
    sign = -1.0 if direction == "maximize" else 1.0
    champion = min(
        candidates,
        key=lambda item: (
            sign * float(item.parent.candidate.primary_value),
            item.parent.admitted_seq,
            item.parent.candidate.commit_sha,
            item.parent.candidate.parent_id,
        ),
    )
    return TerminalArchiveCoordinates(
        root_program_id=roots[0].parent.candidate.parent_id,
        champion_program_id=champion.parent.candidate.parent_id,
        archive_cutoff=max(item.parent.admitted_seq for item in programs),
    )


__all__ = [
    "TERMINAL_BATCH_STATUSES",
    "TerminalArchiveCoordinates",
    "derive_terminal_archive_coordinates",
]
