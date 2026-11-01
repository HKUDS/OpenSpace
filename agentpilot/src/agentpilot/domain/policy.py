"""Carrier-neutral D2/D3 and policy-action contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Mapping

from agentpilot.domain.common import (
    deep_freeze,
    require_aware,
    require_text,
    stable_id,
)
from agentpilot.domain.search import CompatibilityKey, ParentSelection, ParentSlate


OBSERVED_BEHAVIOR_MAX_CHARS = 400
FAILURE_MECHANISM_MAX_CHARS = 600
IMPLEMENTATION_DIRECTION_MAX_CHARS = 800
# Legacy wire/presentation limits above remain strict for legacy parsers.
# Native delivery may retain a longer bounded intent in the same typed fields.
OBSERVED_BEHAVIOR_STORAGE_MAX_CHARS = 1600
FAILURE_MECHANISM_STORAGE_MAX_CHARS = 2400
IMPLEMENTATION_DIRECTION_STORAGE_MAX_CHARS = 4800
NEW_PATH_ROLE_MAX_CHARS = 600
MAX_DECLARED_SCOPE_TARGETS = 8
MAX_NEW_PATHS = 4
_DOTTED_SYMBOL = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_REQUEST_LOCAL_ALIAS = re.compile(
    r"(?<![A-Za-z0-9_])[TEFPR][A-F0-9]{12}_\d{5}(?![A-Za-z0-9_])", re.IGNORECASE,
)


class TerminalPolicyUpdateError(RuntimeError):
    """A frozen policy update cannot make progress when replayed.

    Provider attempts used by a policy update have stable identities.  Once a
    bounded update has consumed every attempt without producing a valid result,
    replaying the batch can only reproduce the same failure.  Application
    lifecycle code treats this marker as terminal instead of entering a
    resume-without-progress loop.
    """


class PolicyMethod(str, Enum):
    UCB = "ucb"
    MEMORY = "memory"


class SlotKind(str, Enum):
    EXISTING = "existing"
    DISCOVER = "discover"


class DirectionMatchKind(str, Enum):
    REPEAT = "repeat"
    EXISTING = "existing"
    NEW = "new"
    PENDING_NEW = "pending_new"
    AMBIGUOUS = "ambiguous"


class SlotAdherenceStatus(str, Enum):
    ADHERENT = "adherent"
    MISMATCH = "mismatch"
    AMBIGUOUS = "ambiguous"
    HARD_REPEAT = "hard_repeat"
    GENERATION_FAILED = "generation_failed"


class ReservationState(str, Enum):
    PLANNED = "planned"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    RECLAIMED = "reclaimed"


@dataclass(frozen=True)
class PlanningSlotAttempt:
    """Carrier-neutral audit row for one proposer/semantic slot attempt."""

    request_id: str
    source_slot_id: str
    source_slot_kind: SlotKind
    source_arm_id: str | None
    slot_ordinal: int
    repair_ordinal: int
    status: SlotAdherenceStatus
    reason: str
    proposal_id: str | None = None
    direction_id: str | None = None
    match_kind: DirectionMatchKind | None = None
    matched_arm_id: str | None = None
    failure_mechanism: str | None = None
    implementation_direction: str | None = None
    resolved_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("request_id", "source_slot_id", "reason"):
            require_text(getattr(self, name), name)
        if not isinstance(self.source_slot_kind, SlotKind):
            raise TypeError("source_slot_kind must be SlotKind")
        if not isinstance(self.status, SlotAdherenceStatus):
            raise TypeError("status must be SlotAdherenceStatus")
        if self.match_kind is not None and not isinstance(
            self.match_kind, DirectionMatchKind
        ):
            raise TypeError("match_kind must be DirectionMatchKind or None")
        for name in ("slot_ordinal", "repair_ordinal"):
            value = getattr(self, name)
            if isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.source_slot_kind is SlotKind.EXISTING:
            require_text(self.source_arm_id or "", "source_arm_id")
        elif self.source_arm_id is not None:
            raise ValueError("DISCOVER planning attempt cannot carry source_arm_id")
        for name in ("proposal_id", "direction_id", "matched_arm_id"):
            value = getattr(self, name)
            if value is not None:
                require_text(value, name)
        if (self.proposal_id is None) is not (self.direction_id is None):
            raise ValueError("proposal_id and direction_id must be present together")
        summaries = (self.failure_mechanism, self.implementation_direction)
        if any(item is None for item in summaries) and any(
            item is not None for item in summaries
        ):
            raise ValueError("planning attempt direction summary is partial")
        targets = tuple(self.resolved_scope)
        object.__setattr__(self, "resolved_scope", targets)
        if self.failure_mechanism is None:
            if targets:
                raise ValueError("planning attempt targets have no direction summary")
        else:
            require_text(self.failure_mechanism, "failure_mechanism")
            require_text(
                self.implementation_direction or "", "implementation_direction"
            )
            if not targets:
                raise ValueError("planning attempt direction summary needs targets")
            for target in targets:
                _safe_repository_path(target, "resolved scope path")


@dataclass(frozen=True)
class PlanningFailureEvent:
    """Durable fact for a decision round that produced no selected action.

    Its stable identity deliberately excludes timestamps and trace contents.
    Each bounded planning attempt has its own identity and is never replayed.
    """

    event_id: str
    run_id: str
    batch_id: str
    state_id: str
    decision_round_id: str
    round_index: int
    planning_attempt_index: int
    method: PolicyMethod
    failure_kind: str
    slot_attempts: tuple[PlanningSlotAttempt, ...]
    available_at: datetime
    compatibility: CompatibilityKey

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "run_id",
            "batch_id",
            "state_id",
            "decision_round_id",
            "failure_kind",
        ):
            require_text(getattr(self, name), name)
        if isinstance(self.round_index, bool) or self.round_index < 0:
            raise ValueError("round_index must be non-negative")
        if (
            isinstance(self.planning_attempt_index, bool)
            or self.planning_attempt_index < 0
        ):
            raise ValueError("planning_attempt_index must be non-negative")
        if not isinstance(self.method, PolicyMethod):
            raise TypeError("method must be PolicyMethod")
        if not isinstance(self.compatibility, CompatibilityKey):
            raise TypeError("compatibility must be CompatibilityKey")
        object.__setattr__(self, "slot_attempts", tuple(self.slot_attempts))
        if not self.slot_attempts:
            raise ValueError("planning failure must preserve its slot attempts")
        if any(
            not isinstance(item, PlanningSlotAttempt) for item in self.slot_attempts
        ):
            raise TypeError("slot_attempts must contain PlanningSlotAttempt values")
        request_ids = tuple(item.request_id for item in self.slot_attempts)
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("planning failure has duplicate request ids")
        require_aware(self.available_at, "available_at")
        expected = stable_id(
            "planning-failure",
            self.batch_id,
            self.round_index,
            self.planning_attempt_index,
            self.method.value,
        )
        if self.event_id != expected:
            raise ValueError("planning failure event id is not stable")


class PlanningRoundFailed(RuntimeError):
    """Carrier-neutral control signal carrying a durable failure fact."""

    def __init__(self, event: PlanningFailureEvent, message: str | None = None) -> None:
        if not isinstance(event, PlanningFailureEvent):
            raise TypeError("planning failure requires a typed event")
        self.event = event
        super().__init__(message or event.failure_kind)


@dataclass(frozen=True)
class ArmSlot:
    slot_id: str
    kind: SlotKind
    ordinal: int
    source_arm_id: str | None = None

    def __post_init__(self) -> None:
        require_text(self.slot_id, "slot_id")
        if isinstance(self.ordinal, bool) or self.ordinal < 0:
            raise ValueError("slot ordinal must be non-negative")
        if self.kind is SlotKind.EXISTING:
            require_text(self.source_arm_id or "", "source_arm_id")
        elif self.source_arm_id is not None:
            raise ValueError("DISCOVER slot cannot carry source_arm_id")


@dataclass(frozen=True)
class ArmAllocation:
    allocation_id: str
    decision_round_id: str
    state_id: str
    parent_id: str
    slots: tuple[ArmSlot, ...]
    trace: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "allocation_id",
            "decision_round_id",
            "state_id",
            "parent_id",
        ):
            require_text(getattr(self, name), name)
        ids = [slot.slot_id for slot in self.slots]
        if len(ids) != len(set(ids)):
            raise ValueError("arm allocation contains duplicate slot ids")
        existing = [
            slot.source_arm_id for slot in self.slots if slot.kind is SlotKind.EXISTING
        ]
        if len(existing) != len(set(existing)):
            raise ValueError("an existing arm may receive at most one slot")
        object.__setattr__(self, "trace", deep_freeze(self.trace))


@dataclass(frozen=True)
class ArmSpec:
    """Non-persistent D2 identity for one innovation family.

    A :class:`StructuredDirection` is a D3 realization.  Keeping this smaller
    carrier-neutral value separate prevents the first realization from
    becoming the durable identity of its arm.
    """

    failure_mechanism: str

    def __post_init__(self) -> None:
        normalized = require_text(self.failure_mechanism, "failure_mechanism")
        if normalized != self.failure_mechanism:
            raise ValueError("failure_mechanism must be trimmed")
        if len(normalized) > FAILURE_MECHANISM_STORAGE_MAX_CHARS:
            raise ValueError(
                f"failure_mechanism exceeds {FAILURE_MECHANISM_STORAGE_MAX_CHARS} characters"
            )
        object.__setattr__(
            self, "failure_mechanism", normalized,
        )


class ScopeKind(str, Enum):
    """Granularity of one server-resolved direction boundary declaration."""

    DEFINITION = "definition"
    FILE = "file"
    PACKAGE = "package"


def _safe_repository_path(value: str, field_name: str) -> str:
    normalized = require_text(value, field_name)
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or not path.parts
        or path.as_posix() != normalized
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(character in normalized for character in "*?[]{}")
        or "\\" in normalized
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError(f"{field_name} must be a safe relative POSIX path")
    return normalized


def _bounded_direction_text(value: str, field_name: str, maximum: int) -> str:
    normalized = require_text(value, field_name)
    if normalized != value:
        raise ValueError(f"{field_name} must be trimmed")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} exceeds {maximum} characters")
    if _REQUEST_LOCAL_ALIAS.search(normalized):
        raise ValueError(f"{field_name} contains a request-local alias")
    return normalized


@dataclass(frozen=True)
class ScopeTarget:
    """One durable scope selection after a request-local T/F/P ref is resolved."""

    kind: ScopeKind
    path: str
    symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ScopeKind):
            raise TypeError("scope target kind must be a ScopeKind")
        object.__setattr__(
            self, "path", _safe_repository_path(self.path, "scope target path")
        )
        symbols = tuple(self.symbols)
        object.__setattr__(self, "symbols", symbols)
        if self.kind is ScopeKind.DEFINITION:
            if not symbols:
                raise ValueError("definition scope target needs at least one symbol")
            if symbols != tuple(sorted(set(symbols))):
                raise ValueError(
                    "definition scope target symbols must be sorted and unique"
                )
            if any(_DOTTED_SYMBOL.fullmatch(symbol) is None for symbol in symbols):
                raise ValueError("definition scope target has an invalid symbol")
        elif symbols:
            raise ValueError("file/package scope targets cannot carry symbols")


@dataclass(frozen=True)
class NewPath:
    """One trusted-root-relative creation request after alias resolution."""

    path: str
    role: str

    def __post_init__(self) -> None:
        path = _safe_repository_path(self.path, "new path")
        if any(
            part.startswith(".") or part == "__pycache__"
            for part in PurePosixPath(path).parts
        ):
            raise ValueError("new path cannot use hidden or cache path segments")
        object.__setattr__(self, "path", path)
        object.__setattr__(
            self,
            "role",
            _bounded_direction_text(
                self.role, "new path role", NEW_PATH_ROLE_MAX_CHARS
            ),
        )


@dataclass(frozen=True)
class StructuredDirection:
    """One grounded hypothesis and worker boundary over an immutable parent.

    Request-local aliases never enter this record. ``declared_scope`` preserves
    what the proposer selected, while ``resolved_scope`` is the server-expanded
    regular-file boundary. Scope is permissive inside that boundary: naming a
    definition does not require that exact definition to change.
    """

    direction_id: str
    observed_behavior: str
    failure_mechanism: str
    implementation_direction: str
    declared_scope: tuple[ScopeTarget, ...]
    new_paths: tuple[NewPath, ...]
    resolved_scope: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        require_text(self.direction_id, "direction_id")
        for name, maximum in (
            ("observed_behavior", OBSERVED_BEHAVIOR_STORAGE_MAX_CHARS),
            ("failure_mechanism", FAILURE_MECHANISM_STORAGE_MAX_CHARS),
            ("implementation_direction", IMPLEMENTATION_DIRECTION_STORAGE_MAX_CHARS),
        ):
            object.__setattr__(
                self, name, _bounded_direction_text(getattr(self, name), name, maximum),
            )

        scope = tuple(self.declared_scope)
        object.__setattr__(self, "declared_scope", scope)
        if not 1 <= len(scope) <= MAX_DECLARED_SCOPE_TARGETS:
            raise ValueError(
                f"declared_scope must contain 1..{MAX_DECLARED_SCOPE_TARGETS} targets"
            )
        if any(not isinstance(item, ScopeTarget) for item in scope):
            raise TypeError("declared_scope must contain ScopeTarget values")
        scope_keys = tuple((item.kind.value, item.path, item.symbols) for item in scope)
        if scope_keys != tuple(sorted(set(scope_keys))):
            raise ValueError("declared_scope must be canonical, sorted, and unique")

        new_paths = tuple(self.new_paths)
        object.__setattr__(self, "new_paths", new_paths)
        if len(new_paths) > MAX_NEW_PATHS:
            raise ValueError(f"new_paths cannot exceed {MAX_NEW_PATHS} entries")
        if any(not isinstance(item, NewPath) for item in new_paths):
            raise TypeError("new_paths must contain NewPath values")
        if tuple(item.path for item in new_paths) != tuple(
            sorted({item.path for item in new_paths})
        ):
            raise ValueError("new_paths must be canonical, path-sorted, and unique")

        resolved = tuple(self.resolved_scope)
        object.__setattr__(self, "resolved_scope", resolved)
        if not resolved or resolved != tuple(sorted(set(resolved))):
            raise ValueError("resolved_scope must be non-empty, sorted, and unique")
        for path in resolved:
            _safe_repository_path(path, "resolved scope path")
        if not {item.path for item in new_paths} <= set(resolved):
            raise ValueError("every new path must belong to resolved_scope")
        exact_scope_paths = {
            item.path for item in scope if item.kind is not ScopeKind.PACKAGE
        }
        if not exact_scope_paths <= set(resolved):
            raise ValueError(
                "definition/file scope targets must belong to resolved_scope"
            )

        evidence_refs = tuple(self.evidence_refs)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        if not evidence_refs or evidence_refs != tuple(sorted(set(evidence_refs))):
            raise ValueError("evidence_refs must be non-empty, sorted, and unique")
        for evidence_ref in evidence_refs:
            require_text(evidence_ref, "evidence_ref")

        expected = self.stable_id(
            observed_behavior=self.observed_behavior,
            failure_mechanism=self.failure_mechanism,
            implementation_direction=self.implementation_direction,
            declared_scope=scope,
            new_paths=new_paths,
            resolved_scope=resolved,
            evidence_refs=evidence_refs,
        )
        if self.direction_id != expected:
            raise ValueError("direction_id is not stable for the resolved direction")

    @staticmethod
    def stable_id(
        *,
        observed_behavior: str,
        failure_mechanism: str,
        implementation_direction: str,
        declared_scope: tuple[ScopeTarget, ...],
        new_paths: tuple[NewPath, ...],
        resolved_scope: tuple[str, ...],
        evidence_refs: tuple[str, ...],
    ) -> str:
        """Derive identity only from durable, already-resolved values."""

        return stable_id(
            "structured-direction",
            observed_behavior,
            failure_mechanism,
            implementation_direction,
            declared_scope,
            new_paths,
            resolved_scope,
            evidence_refs,
        )

    @classmethod
    def create(
        cls,
        *,
        observed_behavior: str,
        failure_mechanism: str,
        implementation_direction: str,
        declared_scope: tuple[ScopeTarget, ...],
        new_paths: tuple[NewPath, ...],
        resolved_scope: tuple[str, ...],
        evidence_refs: tuple[str, ...],
    ) -> "StructuredDirection":
        scope = tuple(
            sorted(
                declared_scope,
                key=lambda item: (item.kind.value, item.path, item.symbols),
            )
        )
        created = tuple(sorted(new_paths, key=lambda item: item.path))
        resolved = tuple(sorted(set(resolved_scope)))
        evidence = tuple(sorted(set(evidence_refs)))
        direction_id = cls.stable_id(
            observed_behavior=observed_behavior.strip(),
            failure_mechanism=failure_mechanism.strip(),
            implementation_direction=implementation_direction.strip(),
            declared_scope=scope,
            new_paths=created,
            resolved_scope=resolved,
            evidence_refs=evidence,
        )
        return cls(
            direction_id=direction_id,
            observed_behavior=observed_behavior.strip(),
            failure_mechanism=failure_mechanism.strip(),
            implementation_direction=implementation_direction.strip(),
            declared_scope=scope,
            new_paths=created,
            resolved_scope=resolved,
            evidence_refs=evidence,
        )


@dataclass(frozen=True)
class DirectionMatch:
    kind: DirectionMatchKind
    distance: float | None
    arm_id: str | None = None
    member_id: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.distance is not None and not 0.0 <= self.distance <= 2.0:
            raise ValueError("cosine distance must be in [0, 2]")
        if self.kind in {
            DirectionMatchKind.EXISTING,
            DirectionMatchKind.PENDING_NEW,
        }:
            require_text(self.arm_id or "", "arm_id")


@dataclass(frozen=True)
class SlotAdherence:
    status: SlotAdherenceStatus
    source_slot_id: str
    matched_arm_id: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        require_text(self.source_slot_id, "source_slot_id")

    @property
    def adherent(self) -> bool:
        return self.status is SlotAdherenceStatus.ADHERENT


@dataclass(frozen=True)
class DirectionCandidate:
    candidate_id: str
    source_slot: ArmSlot | None
    direction: StructuredDirection
    match: DirectionMatch | None
    adherence: SlotAdherence | None

    def __post_init__(self) -> None:
        require_text(self.candidate_id, "candidate_id")
        carrier_fields = (self.source_slot, self.match, self.adherence)
        if any(item is None for item in carrier_fields) and any(
            item is not None for item in carrier_fields
        ):
            raise ValueError(
                "direction candidate UCB slot/match/adherence must be "
                "all present or all absent"
            )
        if (
            self.source_slot is not None
            and self.adherence is not None
            and self.adherence.source_slot_id != self.source_slot.slot_id
        ):
            raise ValueError("slot adherence references a different slot")


@dataclass(frozen=True)
class DirectionSlate:
    slate_id: str
    decision_round_id: str
    state_id: str
    parent_id: str
    candidates: tuple[DirectionCandidate, ...]

    def __post_init__(self) -> None:
        for name in ("slate_id", "decision_round_id", "state_id", "parent_id"):
            require_text(getattr(self, name), name)
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("direction slate contains duplicate candidates")


@dataclass(frozen=True)
class DirectionSelection:
    selection_id: str
    slate_id: str
    candidate_id: str
    direction_id: str
    trace: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "selection_id",
            "slate_id",
            "candidate_id",
            "direction_id",
        ):
            require_text(getattr(self, name), name)
        object.__setattr__(self, "trace", deep_freeze(self.trace))


@dataclass(frozen=True)
class PendingReservation:
    reservation_id: str
    action_id: str
    batch_id: str
    decision_round_id: str
    arm_id: str
    direction_id: str
    state: ReservationState
    created_at: datetime
    source_slot_kind: SlotKind
    joined_existing_pending: bool = False
    dispatched_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in (
            "reservation_id",
            "action_id",
            "batch_id",
            "decision_round_id",
            "arm_id",
            "direction_id",
        ):
            require_text(getattr(self, name), name)
        require_aware(self.created_at, "created_at")
        if self.dispatched_at is not None:
            require_aware(self.dispatched_at, "dispatched_at")
            if self.dispatched_at < self.created_at:
                raise ValueError("reservation dispatch cannot precede creation")
        if self.completed_at is not None:
            require_aware(self.completed_at, "completed_at")
        if self.state in {
            ReservationState.PLANNED,
            ReservationState.RECLAIMED,
        }:
            if self.dispatched_at is not None or self.completed_at is not None:
                raise ValueError(
                    f"{self.state.value} reservation cannot carry " "execution times"
                )
        elif self.state is ReservationState.DISPATCHED:
            if self.dispatched_at is None or self.completed_at is not None:
                raise ValueError("dispatched reservation needs only dispatched_at")
        elif self.state is ReservationState.COMPLETED:
            if self.dispatched_at is None or self.completed_at is None:
                raise ValueError(
                    "completed reservation needs dispatch and completion times"
                )
            if self.completed_at < self.dispatched_at:
                raise ValueError("reservation completion cannot precede dispatch")


@dataclass(frozen=True)
class PolicyAction:
    action_id: str
    method: PolicyMethod
    run_id: str
    batch_id: str
    decision_round_id: str
    state_id: str
    parent_id: str
    parent_commit_sha: str
    direction: StructuredDirection
    source_slot: ArmSlot | None
    arm_id: str | None
    direction_match: DirectionMatch | None
    created_at: datetime
    d1_trace: Mapping[str, Any] = field(default_factory=dict)
    d2_trace: Mapping[str, Any] = field(default_factory=dict)
    d3_trace: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "action_id",
            "run_id",
            "batch_id",
            "decision_round_id",
            "state_id",
            "parent_id",
            "parent_commit_sha",
        ):
            require_text(getattr(self, name), name)
        if self.method is PolicyMethod.UCB:
            require_text(self.arm_id or "", "arm_id")
            if self.source_slot is None or self.direction_match is None:
                raise ValueError(
                    "UCB action needs typed slot and final direction match"
                )
            if self.direction_match.kind not in {
                DirectionMatchKind.EXISTING,
                DirectionMatchKind.NEW,
                DirectionMatchKind.PENDING_NEW,
            }:
                raise ValueError("UCB action needs a selectable final direction match")
            if self.direction_match.arm_id != self.arm_id:
                raise ValueError("UCB action arm and final direction match disagree")
            if self.source_slot.kind is SlotKind.EXISTING:
                if (
                    self.direction_match.kind is not DirectionMatchKind.EXISTING
                    or self.source_slot.source_arm_id != self.arm_id
                ):
                    raise ValueError(
                        "existing UCB slot must select its matched source arm"
                    )
            elif self.direction_match.kind not in {
                DirectionMatchKind.NEW,
                DirectionMatchKind.PENDING_NEW,
            }:
                raise ValueError(
                    "DISCOVER UCB slot must select a new or pending-new arm"
                )
        elif (
            self.arm_id is not None
            or self.source_slot is not None
            or self.direction_match is not None
        ):
            raise ValueError("Memory actions do not carry UCB slot/match/arm identity")
        require_aware(self.created_at, "created_at")
        object.__setattr__(self, "d1_trace", deep_freeze(self.d1_trace))
        object.__setattr__(self, "d2_trace", deep_freeze(self.d2_trace))
        object.__setattr__(self, "d3_trace", deep_freeze(self.d3_trace))


@dataclass(frozen=True)
class PolicyDecisionPlan:
    """Typed immutable D1/D2/Q/D3 trace for exactly one selected action."""

    plan_id: str
    method: PolicyMethod
    parent_slate: ParentSlate
    parent_selection: ParentSelection
    arm_allocation: ArmAllocation | None
    direction_slate: DirectionSlate
    direction_selection: DirectionSelection
    action: PolicyAction

    def __post_init__(self) -> None:
        require_text(self.plan_id, "plan_id")
        if self.action.method is not self.method:
            raise ValueError("decision plan/action method mismatch")
        if self.parent_selection.slate_id != self.parent_slate.slate_id:
            raise ValueError("parent selection does not reference parent slate")
        if self.direction_selection.slate_id != self.direction_slate.slate_id:
            raise ValueError("direction selection does not reference direction slate")
        if self.action.parent_id != self.parent_selection.parent_id:
            raise ValueError("action parent does not match D1 selection")
        selected_parents = tuple(
            candidate
            for candidate in self.parent_slate.candidates
            if candidate.parent_id == self.parent_selection.parent_id
            and candidate.commit_sha == self.parent_selection.commit_sha
        )
        if len(selected_parents) != 1:
            raise ValueError("parent selection is absent from its immutable slate")
        if self.action.parent_commit_sha != selected_parents[0].commit_sha:
            raise ValueError("action parent commit does not match D1 selection")
        if self.direction_slate.parent_id != self.action.parent_id:
            raise ValueError("direction slate belongs to another parent")
        if self.action.direction.direction_id != self.direction_selection.direction_id:
            raise ValueError("action direction does not match D3 selection")
        selected_directions = tuple(
            candidate
            for candidate in self.direction_slate.candidates
            if candidate.candidate_id == self.direction_selection.candidate_id
            and candidate.direction.direction_id
            == self.direction_selection.direction_id
        )
        if len(selected_directions) != 1:
            raise ValueError("direction selection is absent from its immutable slate")
        selected_direction = selected_directions[0]
        if selected_direction.direction != self.action.direction:
            raise ValueError("action direction differs from selected candidate")
        coordinates = {
            self.parent_slate.decision_round_id,
            self.parent_selection.decision_round_id,
            self.direction_slate.decision_round_id,
            self.action.decision_round_id,
        }
        if len(coordinates) != 1:
            raise ValueError("decision plan round coordinates disagree")
        state_ids = {
            self.parent_slate.state_id,
            self.parent_selection.state_id,
            self.direction_slate.state_id,
            self.action.state_id,
        }
        if len(state_ids) != 1:
            raise ValueError("decision plan state coordinates disagree")
        if self.method is PolicyMethod.UCB:
            if self.arm_allocation is None:
                raise ValueError("UCB decision plan needs typed arm allocation")
            if any(
                candidate.source_slot is None
                or candidate.match is None
                or candidate.adherence is None
                for candidate in self.direction_slate.candidates
            ):
                raise ValueError("UCB direction slate needs typed slot/match/adherence")
            if (
                self.arm_allocation.decision_round_id != self.action.decision_round_id
                or self.arm_allocation.state_id != self.action.state_id
                or self.arm_allocation.parent_id != self.action.parent_id
            ):
                raise ValueError("arm allocation coordinates disagree")
            if self.action.source_slot not in self.arm_allocation.slots:
                raise ValueError("selected UCB slot is absent from arm allocation")
            if (
                selected_direction.source_slot != self.action.source_slot
                or selected_direction.adherence is None
                or not selected_direction.adherence.adherent
            ):
                raise ValueError("selected UCB candidate is not slot-adherent")
        else:
            if self.arm_allocation is not None:
                raise ValueError("Memory decision plan cannot contain arm allocation")
            if any(
                candidate.source_slot is not None
                or candidate.match is not None
                or candidate.adherence is not None
                for candidate in self.direction_slate.candidates
            ):
                raise ValueError(
                    "Memory direction slate cannot contain UCB " "slot/match/adherence"
                )
