"""Frozen semantic representation and append-only direction indexes.

The representation is deliberately narrow: it establishes direction identity,
nearest-arm membership, and repeat evidence.  It never predicts reward and it
has no online PCA, centroid update, merge, or LLM-judge path.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Protocol, Sequence, runtime_checkable

from agentpilot.domain import (
    ArmSpec,
    CompatibilityKey,
    PendingReservation,
    ReservationState,
    StructuredDirection,
)
from agentpilot.domain.common import canonical_json, require_aware, require_text

UCB_DUAL_SEMANTIC_INPUT_CONTRACT = "ucb-d2-arm-d3-scope-realization"


class SemanticIndexError(ValueError):
    """Base class for fail-closed semantic-index errors."""


class EncoderIdentityMismatch(SemanticIndexError):
    """An embedding or index belongs to a different frozen encoder."""


class EncoderOutputError(SemanticIndexError):
    """The frozen provider returned a malformed or drifting vector."""


class SemanticRecordConflict(SemanticIndexError):
    """A stable id was replayed with different immutable semantics."""


@dataclass(frozen=True)
class EmbeddingIdentity:
    encoder_id: str
    encoder_identity: str
    dimension: int
    distance: str = "cosine"

    def __post_init__(self) -> None:
        require_text(self.encoder_id, "encoder_id")
        require_text(self.encoder_identity, "encoder_identity")
        if (
            isinstance(self.dimension, bool)
            or not isinstance(self.dimension, int)
            or self.dimension < 1
        ):
            raise ValueError("embedding dimension must be positive")
        if self.distance != "cosine":
            raise ValueError("the frozen direction index only supports cosine distance")


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Provider-neutral embedding boundary."""

    def identity(self) -> EmbeddingIdentity:
        ...

    def embed(self, inputs: Sequence[str]) -> Sequence[Sequence[float]]:
        ...


@dataclass(frozen=True)
class EncodedDirection:
    canonical_input: str
    embedding: tuple[float, ...]
    embedding_identity: EmbeddingIdentity


@dataclass(frozen=True)
class DualEncodedDirection:
    """Separate D2 arm and D3 realization semantics for one proposal."""

    arm: EncodedDirection
    realization: EncodedDirection

    def __post_init__(self) -> None:
        if self.arm.embedding_identity != self.realization.embedding_identity:
            raise EncoderIdentityMismatch(
                "dual semantic encodings use different encoders"
            )


@dataclass(frozen=True)
class DirectionSemanticEnvelope:
    """Codec payload stored in an existing canonical-input column.

    The arm vector lives in this JSON envelope because the unchanged durable
    record has only one vector column; that column is reserved for D3 repeat
    detection.  Encoder identity remains in the record's existing typed field.
    """

    arm_canonical_input: str
    arm_embedding: tuple[float, ...]
    realization_canonical_input: str

    def __post_init__(self) -> None:
        require_text(self.arm_canonical_input, "arm_canonical_input")
        require_text(self.realization_canonical_input, "realization_canonical_input")
        for value, name in (
            (self.arm_canonical_input, "arm_canonical_input"),
            (self.realization_canonical_input, "realization_canonical_input"),
        ):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError) as exc:
                raise SemanticIndexError(f"{name} must contain JSON") from exc
            if canonical_json(parsed) != value:
                raise SemanticIndexError(f"{name} must contain canonical JSON")
        try:
            embedding = tuple(float(value) for value in self.arm_embedding)
        except (TypeError, ValueError) as exc:
            raise SemanticIndexError(
                "arm_embedding contains a non-numeric value"
            ) from exc
        if not embedding or any(not math.isfinite(value) for value in embedding):
            raise SemanticIndexError("arm_embedding must be a finite non-empty vector")
        if math.fsum(value * value for value in embedding) <= 0.0:
            raise SemanticIndexError("arm_embedding cannot be a zero vector")
        object.__setattr__(self, "arm_embedding", embedding)


def canonical_arm_input(arm_spec: ArmSpec) -> str:
    """Serialize only the D2 innovation-family identity."""

    if not isinstance(arm_spec, ArmSpec):
        raise TypeError("arm_spec must be ArmSpec")
    return canonical_json({"failure_mechanism": arm_spec.failure_mechanism})


def canonical_realization_input(direction: StructuredDirection) -> str:
    """Serialize only the D3 realization identity used for repeat checks."""

    if not isinstance(direction, StructuredDirection):
        raise TypeError("direction must be StructuredDirection")
    return canonical_json(
        {
            "implementation_direction": direction.implementation_direction,
            "resolved_scope": list(direction.resolved_scope),
            "new_paths": [
                {"path": item.path, "role": item.role} for item in direction.new_paths
            ],
        }
    )


def canonical_direction_input(direction: StructuredDirection) -> str:
    """Serialize the Textual-policy direction identity."""

    if not isinstance(direction, StructuredDirection):
        raise TypeError("direction must be StructuredDirection")
    return canonical_json(
        {
            "failure_mechanism": direction.failure_mechanism,
            "implementation_direction": direction.implementation_direction,
            "resolved_scope": list(direction.resolved_scope),
            "new_paths": [
                {"path": item.path, "role": item.role} for item in direction.new_paths
            ],
        }
    )


def encode_direction_semantic_envelope(
    *, arm_encoded: EncodedDirection, realization_encoded: EncodedDirection,
) -> str:
    """Encode dual semantics into the unchanged durable text field."""

    if not isinstance(arm_encoded, EncodedDirection):
        raise TypeError("arm_encoded must be EncodedDirection")
    if not isinstance(realization_encoded, EncodedDirection):
        raise TypeError("realization_encoded must be EncodedDirection")
    if arm_encoded.embedding_identity != realization_encoded.embedding_identity:
        raise EncoderIdentityMismatch("dual semantic encodings use different encoders")
    envelope = DirectionSemanticEnvelope(
        arm_canonical_input=arm_encoded.canonical_input,
        arm_embedding=arm_encoded.embedding,
        realization_canonical_input=realization_encoded.canonical_input,
    )
    return canonical_json(
        {
            "arm": {
                "canonical_input": envelope.arm_canonical_input,
                "embedding": list(envelope.arm_embedding),
            },
            "realization": {"canonical_input": envelope.realization_canonical_input,},
        }
    )


def decode_direction_semantic_envelope(
    canonical_input: str,
) -> DirectionSemanticEnvelope:
    """Decode the current dual-semantic envelope, failing closed otherwise."""

    require_text(canonical_input, "canonical_input")
    try:
        document = json.loads(canonical_input)
    except (TypeError, ValueError) as exc:
        raise SemanticIndexError("canonical_input must contain JSON") from exc
    if not isinstance(document, dict):
        raise SemanticIndexError("semantic envelope must be a JSON object")
    if canonical_json(document) != canonical_input:
        raise SemanticIndexError("semantic envelope must contain canonical JSON")
    if set(document) != {"arm", "realization"}:
        raise SemanticIndexError("semantic envelope contains unknown or missing fields")
    arm = document.get("arm")
    realization = document.get("realization")
    if not isinstance(arm, dict) or set(arm) != {"canonical_input", "embedding"}:
        raise SemanticIndexError("semantic envelope arm payload is malformed")
    if not isinstance(realization, dict) or set(realization) != {"canonical_input"}:
        raise SemanticIndexError("semantic envelope realization payload is malformed")
    try:
        arm_embedding = tuple(arm["embedding"])
    except TypeError as exc:
        raise SemanticIndexError(
            "semantic envelope arm embedding is malformed"
        ) from exc
    return DirectionSemanticEnvelope(
        arm_canonical_input=arm["canonical_input"],
        arm_embedding=arm_embedding,
        realization_canonical_input=realization["canonical_input"],
    )


def _validated_embedding(
    values: Sequence[float], identity: EmbeddingIdentity
) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)):
        raise EncoderOutputError("embedding must be a numeric sequence")
    try:
        embedding = tuple(float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise EncoderOutputError("embedding contains a non-numeric value") from exc
    if len(embedding) != identity.dimension:
        raise EncoderOutputError(
            f"embedding dimension {len(embedding)} != {identity.dimension}"
        )
    if any(not math.isfinite(value) for value in embedding):
        raise EncoderOutputError("embedding contains NaN or infinity")
    if math.fsum(value * value for value in embedding) <= 0.0:
        raise EncoderOutputError("zero vectors have undefined cosine distance")
    return embedding


def cosine_distance(left: Sequence[float], right: Sequence[float]) -> float:
    """Compute bounded cosine distance without producing a value score."""

    if len(left) != len(right) or not left:
        raise SemanticIndexError("cosine vectors must have the same non-zero dimension")
    if any(not math.isfinite(float(value)) for value in (*left, *right)):
        raise SemanticIndexError("cosine vectors must be finite")
    left_norm = math.sqrt(math.fsum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(math.fsum(float(value) ** 2 for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        raise SemanticIndexError("cosine distance is undefined for a zero vector")
    dot = math.fsum(float(a) * float(b) for a, b in zip(left, right))
    similarity = max(-1.0, min(1.0, dot / (left_norm * right_norm)))
    return max(0.0, min(2.0, 1.0 - similarity))


class FrozenDirectionEncoder:
    """Validate provider identity and detect within-process encoder drift."""

    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        expected_identity: EmbeddingIdentity | None = None,
    ) -> None:
        if not isinstance(provider, EmbeddingProvider):
            raise TypeError("provider does not satisfy EmbeddingProvider")
        identity = provider.identity()
        if not isinstance(identity, EmbeddingIdentity):
            raise TypeError("embedding provider identity has the wrong type")
        if expected_identity is not None and identity != expected_identity:
            raise EncoderIdentityMismatch("embedding provider identity changed")
        self._provider = provider
        self.identity = identity
        self._cache: dict[str, tuple[float, ...]] = {}

    def _encode_inputs(self, inputs: Sequence[str]) -> tuple[EncodedDirection, ...]:
        semantic_inputs = tuple(
            require_text(value, "semantic_input") for value in inputs
        )
        missing = tuple(
            dict.fromkeys(
                value for value in semantic_inputs if value not in self._cache
            )
        )
        if missing:
            rows = self._provider.embed(missing)
            if len(rows) != len(missing):
                raise EncoderOutputError(
                    "embedding provider returned the wrong number of rows"
                )
            validated = tuple(_validated_embedding(row, self.identity) for row in rows)
            self._cache.update(zip(missing, validated))
        return tuple(
            EncodedDirection(
                canonical_input=semantic_input,
                embedding=self._cache[semantic_input],
                embedding_identity=self.identity,
            )
            for semantic_input in semantic_inputs
        )

    def encode_arm(self, arm_spec: ArmSpec) -> EncodedDirection:
        return self._encode_inputs((canonical_arm_input(arm_spec),))[0]

    def encode_realization(self, direction: StructuredDirection) -> EncodedDirection:
        return self._encode_inputs((canonical_realization_input(direction),))[0]

    def encode_dual(
        self, arm_spec: ArmSpec, direction: StructuredDirection,
    ) -> DualEncodedDirection:
        arm, realization = self._encode_inputs(
            (canonical_arm_input(arm_spec), canonical_realization_input(direction),)
        )
        return DualEncodedDirection(arm=arm, realization=realization)


def decode_semantic_record(
    *,
    canonical_input: str,
    embedding: Sequence[float],
    embedding_identity: EmbeddingIdentity,
) -> DualEncodedDirection:
    """Project one current durable record into D2 and D3 encodings."""

    persisted = _validated_embedding(embedding, embedding_identity)
    envelope = decode_direction_semantic_envelope(canonical_input)
    arm = EncodedDirection(
        canonical_input=envelope.arm_canonical_input,
        embedding=_validated_embedding(envelope.arm_embedding, embedding_identity),
        embedding_identity=embedding_identity,
    )
    realization = EncodedDirection(
        canonical_input=envelope.realization_canonical_input,
        embedding=persisted,
        embedding_identity=embedding_identity,
    )
    return DualEncodedDirection(arm=arm, realization=realization)


@dataclass(frozen=True)
class SemanticThresholds:
    threshold_id: str
    tau_repeat: float
    tau_arm: float
    threshold_boundary_margin: float
    nearest_arm_tie_tolerance: float

    def __post_init__(self) -> None:
        require_text(self.threshold_id, "threshold_id")
        values = (
            self.tau_repeat,
            self.tau_arm,
            self.threshold_boundary_margin,
            self.nearest_arm_tie_tolerance,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in values
        ):
            raise ValueError("semantic thresholds must be numeric")
        if any(not math.isfinite(value) for value in values):
            raise ValueError("semantic thresholds must be finite")
        if not 0.0 <= self.tau_repeat < self.tau_arm <= 2.0:
            raise ValueError(
                "semantic thresholds require 0 <= tau_repeat < tau_arm <= 2"
            )
        if self.threshold_boundary_margin < 0.0:
            raise ValueError("threshold_boundary_margin must be non-negative")
        if self.nearest_arm_tie_tolerance < 0.0:
            raise ValueError("nearest_arm_tie_tolerance must be non-negative")
        if self.threshold_boundary_margin >= self.tau_repeat:
            raise ValueError(
                "threshold boundary margin leaves no repeat acceptance band"
            )
        if self.tau_arm + self.threshold_boundary_margin >= 2.0:
            raise ValueError(
                "threshold boundary margin leaves no distinct acceptance band"
            )
        if (
            self.tau_repeat + self.threshold_boundary_margin
            >= self.tau_arm - self.threshold_boundary_margin
        ):
            raise ValueError(
                "threshold boundary margins leave no same-arm acceptance band"
            )

    def boundary_margin(self, distance: float) -> float:
        """Return distance to the nearest frozen identity threshold."""

        if (
            isinstance(distance, bool)
            or not isinstance(distance, (int, float))
            or not math.isfinite(distance)
            or not 0.0 <= distance <= 2.0
        ):
            raise ValueError(
                "semantic distance must be finite and between zero and two"
            )
        return min(abs(distance - self.tau_repeat), abs(distance - self.tau_arm),)

    def is_boundary_ambiguous(self, distance: float) -> bool:
        """Apply the same threshold safety band in calibration and runtime."""

        return self.boundary_margin(distance) <= self.threshold_boundary_margin


@dataclass(frozen=True)
class DirectionApplicability:
    """Coordinates used only to decide whether a near-repeat is comparable."""

    parent_id: str
    applicability_key: str

    def __post_init__(self) -> None:
        require_text(self.parent_id, "parent_id")
        require_text(self.applicability_key, "applicability_key")

    def comparable(self, other: "DirectionApplicability") -> bool:
        return (
            self.parent_id == other.parent_id
            or self.applicability_key == other.applicability_key
        )


@dataclass(frozen=True)
class ArmAnchor:
    arm_id: str
    direction_id: str
    source_action_id: str
    canonical_input: str
    embedding: tuple[float, ...]
    embedding_identity: EmbeddingIdentity
    threshold_id: str
    compatibility: CompatibilityKey
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "arm_id",
            "direction_id",
            "source_action_id",
            "canonical_input",
        ):
            require_text(getattr(self, name), name)
        require_text(self.threshold_id, "threshold_id")
        require_aware(self.created_at, "created_at")
        object.__setattr__(
            self,
            "embedding",
            _validated_embedding(self.embedding, self.embedding_identity),
        )


class MemberEvidenceStatus(str, Enum):
    ATTRIBUTABLE = "attributable"
    SKIP = "skip"


@dataclass(frozen=True)
class ExecutedDirectionMember:
    """One selected and dispatched realization, including retryable skips."""

    member_id: str
    arm_id: str
    action_id: str
    batch_id: str
    direction_id: str
    canonical_input: str
    embedding: tuple[float, ...]
    embedding_identity: EmbeddingIdentity
    threshold_id: str
    compatibility: CompatibilityKey
    applicability: DirectionApplicability
    evidence_status: MemberEvidenceStatus
    dispatched_at: datetime
    completed_at: datetime
    skip_reason: str = ""

    def __post_init__(self) -> None:
        for name in (
            "member_id",
            "arm_id",
            "action_id",
            "batch_id",
            "direction_id",
            "canonical_input",
        ):
            require_text(getattr(self, name), name)
        require_text(self.threshold_id, "threshold_id")
        require_aware(self.dispatched_at, "dispatched_at")
        require_aware(self.completed_at, "completed_at")
        if self.completed_at < self.dispatched_at:
            raise ValueError("member completion cannot precede dispatch")
        object.__setattr__(
            self,
            "embedding",
            _validated_embedding(self.embedding, self.embedding_identity),
        )

    @property
    def repeat_blocking(self) -> bool:
        return self.evidence_status is MemberEvidenceStatus.ATTRIBUTABLE

    @property
    def retryable(self) -> bool:
        return self.evidence_status is MemberEvidenceStatus.SKIP


@dataclass(frozen=True)
class PendingSemanticReservation:
    reservation: PendingReservation
    canonical_input: str
    embedding: tuple[float, ...]
    embedding_identity: EmbeddingIdentity
    threshold_id: str
    compatibility: CompatibilityKey
    applicability: DirectionApplicability
    establishes_new_arm: bool

    def __post_init__(self) -> None:
        require_text(self.canonical_input, "canonical_input")
        require_text(self.threshold_id, "threshold_id")
        object.__setattr__(
            self,
            "embedding",
            _validated_embedding(self.embedding, self.embedding_identity),
        )

    @property
    def active(self) -> bool:
        return self.reservation.state is not ReservationState.RECLAIMED


class _FrozenIndex:
    def __init__(
        self, *, embedding_identity: EmbeddingIdentity, threshold_id: str,
    ) -> None:
        self.embedding_identity = embedding_identity
        self.threshold_id = require_text(threshold_id, "threshold_id")

    def _validate_record_identity(
        self, identity: EmbeddingIdentity, threshold_id: str
    ) -> None:
        if identity != self.embedding_identity:
            raise EncoderIdentityMismatch("semantic record encoder identity differs")
        if threshold_id != self.threshold_id:
            raise EncoderIdentityMismatch("semantic record threshold identity differs")


class ArmAnchorIndex(_FrozenIndex):
    """Immutable anchors used only for cross-batch arm assignment."""

    def __init__(
        self,
        *,
        embedding_identity: EmbeddingIdentity,
        threshold_id: str,
        anchors: Sequence[ArmAnchor] = (),
    ) -> None:
        super().__init__(
            embedding_identity=embedding_identity, threshold_id=threshold_id,
        )
        self._anchors: dict[str, ArmAnchor] = {}
        for anchor in anchors:
            self.add(anchor)

    def reset_projection(self) -> None:
        """Clear anchors while retaining the frozen semantic identity."""

        self._anchors.clear()

    def add(self, anchor: ArmAnchor) -> None:
        if not isinstance(anchor, ArmAnchor):
            raise TypeError("anchor must be ArmAnchor")
        self._validate_record_identity(anchor.embedding_identity, anchor.threshold_id)
        previous = self._anchors.get(anchor.arm_id)
        if previous is not None and previous != anchor:
            raise SemanticRecordConflict(f"arm {anchor.arm_id!r} anchor is immutable")
        self._anchors[anchor.arm_id] = anchor

    def all(self) -> tuple[ArmAnchor, ...]:
        return tuple(self._anchors[key] for key in sorted(self._anchors))

    def neighbors(
        self,
        embedding: Sequence[float],
        compatibility: CompatibilityKey,
        *,
        max_distance: float,
    ) -> tuple[tuple[float, ArmAnchor], ...]:
        query = _validated_embedding(embedding, self.embedding_identity)
        rows = (
            (
                cosine_distance(
                    query,
                    decode_semantic_record(
                        canonical_input=anchor.canonical_input,
                        embedding=anchor.embedding,
                        embedding_identity=anchor.embedding_identity,
                    ).arm.embedding,
                ),
                anchor,
            )
            for anchor in self._anchors.values()
            if anchor.compatibility == compatibility
        )
        return tuple(
            sorted(
                (row for row in rows if row[0] <= max_distance),
                key=lambda row: (row[0], row[1].arm_id),
            )
        )


class ExecutedDirectionMemberIndex(_FrozenIndex):
    """All dispatched realizations.

    Only attributable rows hard-block repeats.
    """

    def __init__(
        self,
        *,
        embedding_identity: EmbeddingIdentity,
        threshold_id: str,
        members: Sequence[ExecutedDirectionMember] = (),
    ) -> None:
        super().__init__(
            embedding_identity=embedding_identity, threshold_id=threshold_id,
        )
        self._members: dict[str, ExecutedDirectionMember] = {}
        for member in members:
            self.add(member)

    def reset_projection(self) -> None:
        """Clear members while retaining the frozen semantic identity."""

        self._members.clear()

    def add(self, member: ExecutedDirectionMember) -> None:
        if not isinstance(member, ExecutedDirectionMember):
            raise TypeError("member must be ExecutedDirectionMember")
        self._validate_record_identity(member.embedding_identity, member.threshold_id)
        previous = self._members.get(member.member_id)
        if previous is not None and previous != member:
            raise SemanticRecordConflict(
                f"member {member.member_id!r} has semantic drift"
            )
        previous_action = next(
            (
                item
                for item in self._members.values()
                if item.action_id == member.action_id
            ),
            None,
        )
        if previous_action is not None and previous_action != member:
            raise SemanticRecordConflict(
                f"action {member.action_id!r} already has a dispatched member"
            )
        self._members[member.member_id] = member

    def all(self) -> tuple[ExecutedDirectionMember, ...]:
        return tuple(self._members[key] for key in sorted(self._members))

    def repeat_neighbors(
        self,
        embedding: Sequence[float],
        compatibility: CompatibilityKey,
        applicability: DirectionApplicability,
        *,
        tau_repeat: float,
    ) -> tuple[tuple[float, ExecutedDirectionMember], ...]:
        query = _validated_embedding(embedding, self.embedding_identity)
        rows = (
            (
                cosine_distance(
                    query,
                    decode_semantic_record(
                        canonical_input=member.canonical_input,
                        embedding=member.embedding,
                        embedding_identity=member.embedding_identity,
                    ).realization.embedding,
                ),
                member,
            )
            for member in self._members.values()
            if member.compatibility == compatibility
            and member.repeat_blocking
            and applicability.comparable(member.applicability)
        )
        return tuple(
            sorted(
                (row for row in rows if row[0] <= tau_repeat),
                key=lambda row: (row[0], row[1].member_id),
            )
        )


class PendingArmIdentityOverlay(_FrozenIndex):
    """Batch-local reservations excluded from frozen UCB estimates."""

    _TRANSITIONS = {
        ReservationState.PLANNED: {
            ReservationState.DISPATCHED,
            ReservationState.RECLAIMED,
        },
        ReservationState.DISPATCHED: {ReservationState.COMPLETED},
        ReservationState.COMPLETED: set(),
        ReservationState.RECLAIMED: set(),
    }

    def __init__(
        self,
        *,
        batch_id: str,
        embedding_identity: EmbeddingIdentity,
        threshold_id: str,
        reservations: Sequence[PendingSemanticReservation] = (),
    ) -> None:
        super().__init__(
            embedding_identity=embedding_identity, threshold_id=threshold_id,
        )
        self.batch_id = require_text(batch_id, "batch_id")
        self._reservations: dict[str, PendingSemanticReservation] = {}
        for reservation in reservations:
            self.add(reservation)

    def add(self, semantic: PendingSemanticReservation) -> None:
        if not isinstance(semantic, PendingSemanticReservation):
            raise TypeError("reservation must be PendingSemanticReservation")
        self._validate_record_identity(
            semantic.embedding_identity, semantic.threshold_id
        )
        if semantic.reservation.batch_id != self.batch_id:
            raise SemanticRecordConflict("pending reservation belongs to another batch")
        key = semantic.reservation.reservation_id
        previous = self._reservations.get(key)
        if previous is not None and previous != semantic:
            raise SemanticRecordConflict(f"reservation {key!r} has semantic drift")
        previous_action = next(
            (
                item
                for item in self._reservations.values()
                if item.reservation.action_id == semantic.reservation.action_id
            ),
            None,
        )
        if previous_action is not None and previous_action != semantic:
            raise SemanticRecordConflict(
                f"action {semantic.reservation.action_id!r} "
                "already has a reservation"
            )
        self._reservations[key] = semantic

    def transition(
        self,
        reservation_id: str,
        state: ReservationState,
        *,
        transitioned_at: datetime | None = None,
    ) -> PendingSemanticReservation:
        current = self._reservations.get(reservation_id)
        if current is None:
            raise KeyError(reservation_id)
        if state is current.reservation.state:
            return current
        if state not in self._TRANSITIONS[current.reservation.state]:
            raise SemanticRecordConflict(
                "illegal reservation transition "
                f"{current.reservation.state} -> {state}"
            )
        if state is ReservationState.DISPATCHED:
            require_aware(transitioned_at, "transitioned_at")
            reservation = replace(
                current.reservation, state=state, dispatched_at=transitioned_at,
            )
        elif state is ReservationState.COMPLETED:
            require_aware(transitioned_at, "transitioned_at")
            reservation = replace(
                current.reservation, state=state, completed_at=transitioned_at,
            )
        else:
            if transitioned_at is not None:
                raise SemanticRecordConflict(
                    "reclaimed reservation does not carry an execution time"
                )
            reservation = replace(current.reservation, state=state)
        updated = replace(current, reservation=reservation)
        self._reservations[reservation_id] = updated
        return updated

    def all(self) -> tuple[PendingSemanticReservation, ...]:
        return tuple(self._reservations[key] for key in sorted(self._reservations))

    def repeat_neighbors(
        self,
        embedding: Sequence[float],
        compatibility: CompatibilityKey,
        applicability: DirectionApplicability,
        *,
        tau_repeat: float,
    ) -> tuple[tuple[float, PendingSemanticReservation], ...]:
        query = _validated_embedding(embedding, self.embedding_identity)
        rows = (
            (
                cosine_distance(
                    query,
                    decode_semantic_record(
                        canonical_input=semantic.canonical_input,
                        embedding=semantic.embedding,
                        embedding_identity=semantic.embedding_identity,
                    ).realization.embedding,
                ),
                semantic,
            )
            for semantic in self._reservations.values()
            if semantic.active
            and semantic.compatibility == compatibility
            and applicability.comparable(semantic.applicability)
        )
        return tuple(
            sorted(
                (row for row in rows if row[0] <= tau_repeat),
                key=lambda row: (row[0], row[1].reservation.reservation_id),
            )
        )

    def new_arm_neighbors(
        self,
        embedding: Sequence[float],
        compatibility: CompatibilityKey,
        *,
        tau_arm: float,
    ) -> tuple[tuple[float, PendingSemanticReservation], ...]:
        query = _validated_embedding(embedding, self.embedding_identity)
        rows = (
            (
                cosine_distance(
                    query,
                    decode_semantic_record(
                        canonical_input=semantic.canonical_input,
                        embedding=semantic.embedding,
                        embedding_identity=semantic.embedding_identity,
                    ).arm.embedding,
                ),
                semantic,
            )
            for semantic in self._reservations.values()
            if semantic.active
            and semantic.establishes_new_arm
            and semantic.compatibility == compatibility
        )
        return tuple(
            sorted(
                (row for row in rows if row[0] <= tau_arm),
                key=lambda row: (row[0], row[1].reservation.reservation_id),
            )
        )
