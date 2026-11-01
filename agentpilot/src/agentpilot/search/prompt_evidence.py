"""Bounded prompt projection of the frozen batch search state.

``SearchState`` is the complete typed policy identity of one batch: D1, D2 and
D3 must share it byte for byte, so it keeps every cutoff-safe fact.  A prompt
cannot afford the same shape.  The archive grows monotonically, so serialising
every projected program into every proposer and ranker call inflates cost
without adding decidable information, and it lets a direction cite a fact that
was never actually read.

This module keeps the two apart.  The state stays whole, while the prompt
receives a deterministic bounded projection of it.  The evidence allowlist is
derived from the same projection, so a direction can only reference evidence
the model was shown.  Program facts also get the kind-specific schema they
lack in ``SearchFact.payload``: a malformed program fact fails here instead of
silently degrading a prompt.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from itertools import zip_longest
from typing import Any

from agentpilot.domain import (
    CompatibilityKey,
    EvidenceCutoff,
    EvidenceTier,
    ParentSelection,
    SearchState,
    StateDescriptor,
    VisibleATaskEvidence,
)
from agentpilot.domain.common import deep_freeze, require_text
from agentpilot.search.direction_history import FailureDirectionCard
from agentpilot.search.evidence_retrieval import FixedEvidenceCard

PROMPT_EVIDENCE_PROJECTION_ID = "bounded-prompt-evidence"
PROMPT_EVIDENCE_MAX_PROGRAMS = 8
PROMPT_EVIDENCE_MAX_CELLS = 16
#: Enough to show a failure pattern rather than one anecdote, while keeping the
#: statements a minority of a prompt that must still carry the repo map.
PROMPT_EVIDENCE_MAX_VISIBLE_A_RECORDS = 6
PROMPT_EVIDENCE_MAX_VISIBLE_A_STATEMENT_CHARS = 1_200

#: Fact kinds written by the canonical search read model.  Anything else is
#: part of the state identity but is not program evidence a policy can cite.
PROGRAM_FACT_KINDS = frozenset({"root_program", "admitted_program"})


@dataclass(frozen=True)
class CellOutcomeSummary:
    """Per-cell results as counts plus a bounded list of failing cells."""

    passed: int
    failed: int
    unknown: int
    failed_cells: tuple[str, ...]
    omitted_failed_cells: int
    failure_modes: Mapping[str, str] = field(default_factory=dict)
    failure_excerpts: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("passed", "failed", "unknown", "omitted_failed_cells"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        cells = tuple(self.failed_cells)
        object.__setattr__(self, "failed_cells", cells)
        if len(cells) + self.omitted_failed_cells != self.failed:
            raise ValueError("failed-cell projection does not account for every cell")
        for name in ("failure_modes", "failure_excerpts"):
            mapping = getattr(self, name)
            if not isinstance(mapping, Mapping):
                raise TypeError(f"{name} must be a mapping")
            if not set(mapping) <= set(cells):
                raise ValueError(f"{name} describes a cell the prompt omits")
            object.__setattr__(self, name, dict(mapping))

    def as_prompt_payload(self) -> dict[str, Any]:
        # ``failure_excerpts`` is deliberately absent: the excerpt is bulk
        # text that belongs on the one cell brief describing that failure.
        # Repeating it here would multiply it across every projected program
        # row without adding a decidable fact.
        payload = {
            "passed": self.passed,
            "failed": self.failed,
            "unknown": self.unknown,
            "failed_cells": list(self.failed_cells),
            "omitted_failed_cells": self.omitted_failed_cells,
        }
        if self.failure_modes:
            payload["failure_modes"] = dict(sorted(self.failure_modes.items()))
        return payload


@dataclass(frozen=True)
class ProgramEvidenceRow:
    """One archive program as the prompt sees it, keyed by its citable ref."""

    evidence_ref: str
    kind: str
    parent_id: str
    commit_sha: str
    archive_seq: int
    primary_value: float | None
    source_action_id: str | None
    evidence_tier: EvidenceTier
    cells: CellOutcomeSummary

    def __post_init__(self) -> None:
        for name in ("evidence_ref", "kind", "parent_id", "commit_sha"):
            require_text(getattr(self, name), name)
        if isinstance(self.archive_seq, bool) or not isinstance(self.archive_seq, int):
            raise ValueError("archive_seq must be an integer")
        if self.archive_seq < 0:
            raise ValueError("archive_seq must be non-negative")
        if self.primary_value is not None and not math.isfinite(self.primary_value):
            raise ValueError("primary_value must be finite when present")
        if self.source_action_id is not None:
            require_text(self.source_action_id, "source_action_id")
        if not isinstance(self.evidence_tier, EvidenceTier):
            raise TypeError("evidence_tier must be an EvidenceTier")
        if self.evidence_tier is EvidenceTier.SEALED_C:
            raise ValueError("sealed C evidence cannot enter prompt evidence")

    def as_prompt_payload(self) -> dict[str, Any]:
        return {
            "evidence_ref": self.evidence_ref,
            "kind": self.kind,
            "parent_id": self.parent_id,
            "commit_sha": self.commit_sha,
            "archive_seq": self.archive_seq,
            "primary_value": self.primary_value,
            "source_action_id": self.source_action_id,
            "evidence_tier": self.evidence_tier.value,
            "cells": self.cells.as_prompt_payload(),
        }


@dataclass(frozen=True)
class SelectedParentEvidence:
    """The D1 result stated as evidence instead of only as coordinates."""

    parent_id: str
    commit_sha: str
    program: ProgramEvidenceRow | None

    def __post_init__(self) -> None:
        require_text(self.parent_id, "parent_id")
        require_text(self.commit_sha, "commit_sha")
        if self.program is not None and self.program.parent_id != self.parent_id:
            raise ValueError("selected-parent evidence names another program")

    def as_prompt_payload(self) -> dict[str, Any]:
        return {
            "evidence_ref": f"parent:{self.parent_id}",
            "parent_id": self.parent_id,
            "commit_sha": self.commit_sha,
            "program": (
                None if self.program is None else self.program.as_prompt_payload()
            ),
        }


@dataclass(frozen=True)
class PromptEvidence:
    """Everything a policy prompt may read about the frozen state."""

    projection_id: str
    run_id: str
    batch_id: str
    state_id: str
    archive_seq: int
    cutoff: EvidenceCutoff
    compatibility: CompatibilityKey
    parent_descriptor: StateDescriptor
    direction_descriptor: StateDescriptor
    budget: Mapping[str, float | int]
    selected_parent: SelectedParentEvidence | None
    programs: tuple[ProgramEvidenceRow, ...]
    omitted_program_count: int
    other_fact_kinds: tuple[str, ...]
    fixed_evidence: tuple[FixedEvidenceCard, ...]
    failure_history: tuple[FailureDirectionCard, ...]
    visible_a_evidence: tuple[VisibleATaskEvidence, ...] = ()
    selected_visible_a_cells: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("projection_id", "run_id", "batch_id", "state_id"):
            require_text(getattr(self, name), name)
        if isinstance(self.omitted_program_count, bool) or (
            self.omitted_program_count < 0
        ):
            raise ValueError("omitted_program_count must be a non-negative integer")
        object.__setattr__(self, "programs", tuple(self.programs))
        object.__setattr__(self, "other_fact_kinds", tuple(self.other_fact_kinds))
        object.__setattr__(self, "fixed_evidence", tuple(self.fixed_evidence))
        object.__setattr__(self, "failure_history", tuple(self.failure_history))
        object.__setattr__(self, "visible_a_evidence", tuple(self.visible_a_evidence))
        object.__setattr__(self, "budget", deep_freeze(dict(self.budget)))
        selected_cells = tuple(self.selected_visible_a_cells)
        object.__setattr__(self, "selected_visible_a_cells", selected_cells)
        if (
            len(set(selected_cells)) != len(selected_cells)
            or len(selected_cells) > PROMPT_EVIDENCE_MAX_VISIBLE_A_RECORDS
        ):
            raise ValueError(
                "selected Visible-A task context must be bounded and unique"
            )
        for cell_id in selected_cells:
            require_text(cell_id, "selected Visible-A cell id")
        if selected_cells and self.selected_parent is None:
            raise ValueError("selected task context requires a selected parent")
        refs = [row.evidence_ref for row in self.programs]
        if len(refs) != len(set(refs)):
            raise ValueError("prompt evidence projects a program twice")
        evidence_cell_ids = [item.cell_id for item in self.visible_a_evidence]
        if len(evidence_cell_ids) != len(set(evidence_cell_ids)):
            raise ValueError("prompt evidence describes a Visible-A cell twice")
        # A brief for a cell the prompt never names would be unciteable text,
        # and it would make the projection's cost stop tracking what it shows.
        if not set(evidence_cell_ids) <= self._named_cells():
            raise ValueError("prompt evidence describes an unnamed Visible-A cell")
        visible_a_cells = {
            cell_id
            for row in self.programs
            if row.evidence_tier is EvidenceTier.VISIBLE_A
            for cell_id in row.cells.failed_cells
        }
        # Explicit task IDs were validated against the frozen state's A task
        # scope. The selected parent's score may come from B; these task cards
        # do not turn that score into an A outcome or assert a pass/fail result.
        visible_a_cells.update(selected_cells)
        if (
            self.selected_parent is not None
            and self.selected_parent.program is not None
        ):
            row = self.selected_parent.program
            if row.evidence_tier is EvidenceTier.VISIBLE_A:
                visible_a_cells.update(row.cells.failed_cells)
        if not set(evidence_cell_ids) <= visible_a_cells:
            raise ValueError(
                "content evidence does not belong to a Visible-A program fact"
            )

    def _named_cells(self) -> set[str]:
        """Every individual cell this projection names in the prompt."""

        cells: set[str] = set()
        cells.update(self.selected_visible_a_cells)
        for row in self.programs:
            cells.update(row.cells.failed_cells)
        if (
            self.selected_parent is not None
            and self.selected_parent.program is not None
        ):
            cells.update(self.selected_parent.program.cells.failed_cells)
        for card in self.fixed_evidence:
            cells.update(card.evidence_cells)
        return cells

    @property
    def evidence_refs(self) -> frozenset[str]:
        """Return exactly the references this projection puts in a prompt.

        A named failing cell is the most specific fact this projection shows,
        so it is citable for the same reason its program is: the model read it
        here.  Omitting it would reject the sharpest available citation and
        push a direction back onto a vaguer parent-level ref.
        """

        refs = {row.evidence_ref for row in self.programs}
        refs.update(self._named_cells())
        if self.selected_parent is not None:
            refs.add(f"parent:{self.selected_parent.parent_id}")
            if self.selected_parent.program is not None:
                refs.add(self.selected_parent.program.evidence_ref)
        for card in self.fixed_evidence:
            refs.add(card.evidence_ref)
        refs.update(card.evidence_ref for card in self.failure_history)
        return frozenset(refs)

    @property
    def non_parent_evidence_refs(self) -> frozenset[str]:
        """Citable references that say more than "the parent I am editing".

        ``parent:<id>`` and the selected parent's own program fact both name the
        program the direction already modifies, so citing either asserts nothing
        about *why* a change should help.  Everything else -- another program, a
        named failing cell, a retrieved card -- is a fact the direction can be
        checked against, so a caller can require at least one when the
        projection offers any.
        """

        parent_level: set[str] = set()
        if self.selected_parent is not None:
            parent_level.add(f"parent:{self.selected_parent.parent_id}")
            if self.selected_parent.program is not None:
                parent_level.add(self.selected_parent.program.evidence_ref)
        return self.evidence_refs - parent_level

    def as_prompt_payload(self) -> dict[str, Any]:
        failure_counts: list[dict[str, object]] = []
        if (
            self.selected_parent is not None
            and self.selected_parent.program is not None
        ):
            counts: dict[str, int] = {}
            for mode in self.selected_parent.program.cells.failure_modes.values():
                counts[mode] = counts.get(mode, 0) + 1
            failure_counts = [
                {"mode": mode, "count": count} for mode, count in sorted(counts.items())
            ]
        return {
            "projection_id": self.projection_id,
            "run_id": self.run_id,
            "batch_id": self.batch_id,
            "state_id": self.state_id,
            "archive_seq": self.archive_seq,
            "cutoff": self.cutoff,
            "compatibility": self.compatibility,
            "parent_descriptor": self.parent_descriptor,
            "direction_descriptor": self.direction_descriptor,
            "budget": dict(self.budget),
            "selected_parent": (
                None
                if self.selected_parent is None
                else self.selected_parent.as_prompt_payload()
            ),
            "selected_parent_failure_counts": failure_counts,
            "archive_programs": [row.as_prompt_payload() for row in self.programs],
            "omitted_program_count": self.omitted_program_count,
            "other_fact_kinds": list(self.other_fact_kinds),
            "fixed_evidence": [
                card.as_prompt_payload() for card in self.fixed_evidence
            ],
            "bounded_failure_history": [
                card.as_prompt_payload() for card in self.failure_history
            ],
            "visible_a_failure_evidence": [
                item.as_prompt_payload() for item in self.visible_a_evidence
            ],
        }


def _cells(
    results: Mapping[str, Any],
    *,
    max_cells: int,
    notes: Mapping[str, Any] | None = None,
    excerpts: Mapping[str, Any] | None = None,
) -> CellOutcomeSummary:
    passed = failed = unknown = 0
    failing: list[str] = []
    for raw_id, result in results.items():
        cell_id = require_text(str(raw_id), "cell id")
        if result is True:
            passed += 1
        elif result is False:
            failed += 1
            failing.append(cell_id)
        elif result is None:
            unknown += 1
        else:
            raise ValueError("cell result must be true, false, or unknown")
    failing.sort()
    shown = tuple(failing[:max_cells])
    return CellOutcomeSummary(
        passed=passed,
        failed=failed,
        unknown=unknown,
        failed_cells=shown,
        omitted_failed_cells=max(len(failing) - max_cells, 0),
        failure_modes={
            cell_id: require_text(str(note), "cell note")
            for cell_id, note in (notes or {}).items()
            if cell_id in shown
        },
        failure_excerpts={
            cell_id: require_text(str(excerpt), "cell excerpt")
            for cell_id, excerpt in (excerpts or {}).items()
            if cell_id in shown
        },
    )


_REQUIRED_PROGRAM_KEYS = ("parent_id", "commit_sha", "archive_seq", "cell_results")


def _program_row(item: Mapping[str, Any], *, max_cells: int) -> ProgramEvidenceRow:
    payload = item.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("program fact must carry a mapping payload")
    missing = [key for key in _REQUIRED_PROGRAM_KEYS if key not in payload]
    if missing:
        raise ValueError(f"program fact payload is missing {missing}")
    results = payload["cell_results"]
    if not isinstance(results, Mapping):
        raise ValueError("program fact cell_results must be a mapping")
    primary_value = payload.get("primary_value")
    if primary_value is not None and not isinstance(primary_value, (int, float)):
        raise ValueError("program fact primary_value must be numeric or absent")
    source_action_id = payload.get("source_action_id")
    if source_action_id is not None and not isinstance(source_action_id, str):
        raise ValueError("program fact source_action_id must be a string or absent")
    notes = payload.get("cell_notes") or {}
    if not isinstance(notes, Mapping):
        raise ValueError("program fact cell_notes must be a mapping")
    excerpts = payload.get("cell_excerpts") or {}
    if not isinstance(excerpts, Mapping):
        raise ValueError("program fact cell_excerpts must be a mapping")
    try:
        evidence_tier = EvidenceTier(str(item["evidence_tier"]))
    except (KeyError, ValueError):
        raise ValueError("program fact must carry a valid evidence_tier") from None
    return ProgramEvidenceRow(
        evidence_ref=str(item["fact_id"]),
        kind=str(item["kind"]),
        parent_id=str(payload["parent_id"]),
        commit_sha=str(payload["commit_sha"]),
        archive_seq=payload["archive_seq"],
        primary_value=None if primary_value is None else float(primary_value),
        source_action_id=source_action_id,
        evidence_tier=evidence_tier,
        cells=_cells(results, max_cells=max_cells, notes=notes, excerpts=excerpts),
    )


def validate_visible_a_evidence_for_state(
    state: SearchState,
    evidence: Sequence[VisibleATaskEvidence],
) -> None:
    """Require every content-bearing record to name a measured Visible-A cell."""

    if not isinstance(state, SearchState):
        raise TypeError("state must be a SearchState")
    records = tuple(evidence)
    if any(not isinstance(item, VisibleATaskEvidence) for item in records):
        raise TypeError("evidence must contain VisibleATaskEvidence values")
    cell_ids = tuple(item.cell_id for item in records)
    if len(cell_ids) != len(set(cell_ids)):
        raise ValueError("Visible-A evidence describes a cell twice")
    if not records:
        return
    items = state.facts.get("items", ())
    if not isinstance(items, (tuple, list)):
        raise ValueError("search state facts must expose an items sequence")
    visible_a_cells: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("search state fact must be a mapping")
        if item.get("kind") not in PROGRAM_FACT_KINDS:
            continue
        try:
            tier = EvidenceTier(str(item["evidence_tier"]))
        except (KeyError, ValueError):
            raise ValueError("program fact must carry a valid evidence_tier") from None
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("program fact must carry a mapping payload")
        results = payload.get("cell_results")
        if not isinstance(results, Mapping):
            raise ValueError("program fact cell_results must be a mapping")
        if tier is EvidenceTier.VISIBLE_A:
            visible_a_cells.update(
                require_text(str(cell_id), "cell id") for cell_id in results
            )
    if not set(cell_ids) <= visible_a_cells:
        raise ValueError("content evidence is outside measured Visible-A state facts")


def project_prompt_evidence(
    *,
    state: SearchState,
    selected_parent: ParentSelection | None = None,
    fixed_evidence: Sequence[FixedEvidenceCard] = (),
    failure_history: Sequence[FailureDirectionCard] = (),
    visible_a_evidence: Sequence[VisibleATaskEvidence] = (),
    visible_a_cell_ids: tuple[str, ...] | None = None,
    max_programs: int = PROMPT_EVIDENCE_MAX_PROGRAMS,
    max_cells: int = PROMPT_EVIDENCE_MAX_CELLS,
    max_visible_a_evidence: int = PROMPT_EVIDENCE_MAX_VISIBLE_A_RECORDS,
) -> PromptEvidence:
    """Project one frozen state into a bounded, deterministic prompt view."""

    if not isinstance(state, SearchState):
        raise TypeError("state must be a SearchState")
    if isinstance(max_programs, bool) or max_programs < 1:
        raise ValueError("max_programs must be a positive integer")
    if isinstance(max_cells, bool) or max_cells < 1:
        raise ValueError("max_cells must be a positive integer")
    if isinstance(max_visible_a_evidence, bool) or max_visible_a_evidence < 0:
        raise ValueError("max_visible_a_evidence must be a non-negative integer")
    if selected_parent is not None and selected_parent.state_id != state.state_id:
        raise ValueError("selected parent belongs to another state")
    validate_visible_a_evidence_for_state(state, visible_a_evidence)
    selected_task_ids: tuple[str, ...] = ()
    if visible_a_cell_ids is not None:
        if not isinstance(visible_a_cell_ids, tuple):
            raise TypeError("visible_a_cell_ids must be a tuple or None")
        for cell_id in visible_a_cell_ids:
            require_text(cell_id, "selected Visible-A cell id")
        if len(set(visible_a_cell_ids)) != len(visible_a_cell_ids):
            raise ValueError("selected task context names a cell twice")
        if visible_a_cell_ids and selected_parent is None:
            raise ValueError("selected task context requires a selected parent")
        if not set(visible_a_cell_ids) <= {item.cell_id for item in visible_a_evidence}:
            raise ValueError(
                "selected task context has no validated Visible-A task brief"
            )
        # VisibleATaskEvidence has no independent compatibility or timestamp:
        # it inherits the A scope of this compatibility-bound frozen state.
        # Parent/commit ownership of selected trace IDs is checked by the trace
        # caller. In particular a child's B program fact need not contain A IDs.
        eligible: set[str] = set()
        for fact in state.facts.get("items", ()):
            if (
                fact.get("kind") not in PROGRAM_FACT_KINDS
                or fact.get("evidence_tier") != EvidenceTier.VISIBLE_A.value
            ):
                continue
            completed_at = fact.get("available_at")
            if isinstance(completed_at, str):
                completed_at = datetime.fromisoformat(
                    completed_at.replace("Z", "+00:00")
                )
            if isinstance(completed_at, datetime) and state.cutoff.allows(completed_at):
                eligible.update(fact["payload"]["cell_results"])
        if not set(visible_a_cell_ids) <= eligible:
            raise ValueError(
                "selected task context is outside the frozen Visible-A cutoff"
            )
        selected_task_ids = visible_a_cell_ids[
            : min(max_visible_a_evidence, PROMPT_EVIDENCE_MAX_VISIBLE_A_RECORDS)
        ]

    items = state.facts.get("items", ())
    if not isinstance(items, (tuple, list)):
        raise ValueError("search state facts must expose an items sequence")
    rows: list[ProgramEvidenceRow] = []
    other_kinds: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("search state fact must be a mapping")
        kind = str(item.get("kind", ""))
        if kind not in PROGRAM_FACT_KINDS:
            other_kinds.add(kind)
            continue
        rows.append(_program_row(item, max_cells=max_cells))

    selected_row: ProgramEvidenceRow | None = None
    if selected_parent is not None:
        matches = [row for row in rows if row.parent_id == selected_parent.parent_id]
        if len(matches) > 1:
            raise ValueError("selected parent has more than one program fact")
        if matches:
            selected_row = matches[0]
            if selected_row.commit_sha != selected_parent.commit_sha:
                raise ValueError("selected parent evidence names another commit")
            rows = [row for row in rows if row is not selected_row]

    maximize = state.compatibility.metric_direction == "maximize"

    def order(row: ProgramEvidenceRow) -> tuple[float, str, str]:
        if row.primary_value is None:
            ranked = math.inf
        else:
            ranked = -row.primary_value if maximize else row.primary_value
        return (ranked, row.commit_sha, row.parent_id)

    # Root A and admitted B readouts measure different task populations. Keep
    # score ordering within each tier, then alternate tiers before truncation so
    # a high score on one population cannot crowd out another population.
    by_tier: dict[EvidenceTier, list[ProgramEvidenceRow]] = {}
    for row in rows:
        by_tier.setdefault(row.evidence_tier, []).append(row)
    groups = [
        sorted(by_tier[tier], key=order)
        for tier in sorted(by_tier, key=lambda tier: tier.value)
    ]
    rows = [row for batch in zip_longest(*groups) for row in batch if row is not None]
    projection = PromptEvidence(
        projection_id=PROMPT_EVIDENCE_PROJECTION_ID,
        run_id=state.run_id,
        batch_id=state.batch_id,
        state_id=state.state_id,
        archive_seq=state.archive_seq,
        cutoff=state.cutoff,
        compatibility=state.compatibility,
        parent_descriptor=state.parent_descriptor,
        direction_descriptor=state.direction_descriptor,
        budget=dict(state.budget),
        selected_parent=(
            None
            if selected_parent is None
            else SelectedParentEvidence(
                parent_id=selected_parent.parent_id,
                commit_sha=selected_parent.commit_sha,
                program=selected_row,
            )
        ),
        programs=tuple(rows[:max_programs]),
        omitted_program_count=max(len(rows) - max_programs, 0),
        other_fact_kinds=tuple(sorted(other_kinds)),
        fixed_evidence=tuple(fixed_evidence),
        failure_history=tuple(failure_history),
        selected_visible_a_cells=selected_task_ids,
    )
    return replace(
        projection,
        visible_a_evidence=_visible_a_evidence_for(
            projection,
            visible_a_evidence,
            maximum=max_visible_a_evidence,
            selected_cell_ids=selected_task_ids
            if visible_a_cell_ids is not None
            else None,
        ),
    )


def _visible_a_evidence_for(
    projection: PromptEvidence,
    evidence: Sequence[VisibleATaskEvidence],
    *,
    maximum: int,
    selected_cell_ids: tuple[str, ...] | None = None,
) -> tuple[VisibleATaskEvidence, ...]:
    """Project named failing Visible-A cells, closest to the parent first.

    Which cells the projection names is decided above, so the briefs follow
    that decision rather than re-deriving it.  When the bound bites, the
    selected parent's own failures survive: those are the ones the direction
    under construction has to explain.

    The selected parent's failure mode for a cell is carried into that cell's
    brief.  Leaving it only in the program row would state the mechanism and
    the statement in two places keyed by an opaque ref, which asks the reader
    to perform a join before either fact becomes actionable.

    The parent's execution excerpt travels the same way and for the same
    reason: it is evidence about how *this* program failed *this* cell.  The
    brief constructor rejects an excerpt outside the visible tier, so an
    adaptive-tier excerpt smuggled into a program fact fails loudly here
    instead of reaching a prompt.
    """

    named = projection._named_cells()
    parent_program = (
        None
        if projection.selected_parent is None
        else projection.selected_parent.program
    )
    preferred = (
        frozenset()
        if parent_program is None
        else frozenset(parent_program.cells.failed_cells)
    )
    modes: Mapping[str, str] = (
        {} if parent_program is None else parent_program.cells.failure_modes
    )
    excerpts: Mapping[str, str] = (
        {} if parent_program is None else parent_program.cells.failure_excerpts
    )
    eligible = tuple(item for item in evidence if item.cell_id in named)
    groups: dict[str, list[VisibleATaskEvidence]] = {}
    for item in eligible:
        if item.cell_id not in preferred:
            continue
        groups.setdefault(modes.get(item.cell_id, "unspecified"), []).append(item)
    for group in groups.values():
        group.sort(key=lambda item: item.cell_id)

    selected: list[VisibleATaskEvidence] = []
    if selected_cell_ids is not None:
        by_id = {item.cell_id: item for item in eligible}
        selected = [by_id[cell_id] for cell_id in selected_cell_ids]
        # Explicit order follows the selected trace episodes, including success
        # controls. No archive failure ranking may crowd these cards out.
        groups = {}
        maximum = len(selected)
    # Round-robin across failure modes so the bounded detailed statements do
    # not turn an alphabetical cell-id prefix into a false dominant mechanism.
    mode_names = tuple(sorted(groups))
    offset = 0
    while len(selected) < maximum:
        appended = False
        for mode in mode_names:
            group = groups[mode]
            if offset < len(group):
                selected.append(group[offset])
                appended = True
                if len(selected) == maximum:
                    break
        if not appended:
            break
        offset += 1
    if len(selected) < maximum:
        selected.extend(
            sorted(
                (item for item in eligible if item.cell_id not in preferred),
                key=lambda item: item.cell_id,
            )[: maximum - len(selected)]
        )
    described: list[VisibleATaskEvidence] = []
    for item in selected:
        updates: dict[str, str] = {}
        if len(item.statement) > PROMPT_EVIDENCE_MAX_VISIBLE_A_STATEMENT_CHARS:
            updates["statement"] = (
                item.statement[: PROMPT_EVIDENCE_MAX_VISIBLE_A_STATEMENT_CHARS - 1]
                + "…"
            )
        if item.cell_id in modes:
            updates["observed_failure"] = modes[item.cell_id]
        if item.cell_id in excerpts:
            updates["execution_excerpt"] = excerpts[item.cell_id]
        described.append(replace(item, **updates) if updates else item)
    return tuple(described)


__all__ = [
    "PROGRAM_FACT_KINDS",
    "PROMPT_EVIDENCE_MAX_CELLS",
    "PROMPT_EVIDENCE_MAX_PROGRAMS",
    "PROMPT_EVIDENCE_MAX_VISIBLE_A_RECORDS",
    "PROMPT_EVIDENCE_MAX_VISIBLE_A_STATEMENT_CHARS",
    "PROMPT_EVIDENCE_PROJECTION_ID",
    "CellOutcomeSummary",
    "ProgramEvidenceRow",
    "PromptEvidence",
    "SelectedParentEvidence",
    "project_prompt_evidence",
    "validate_visible_a_evidence_for_state",
]
