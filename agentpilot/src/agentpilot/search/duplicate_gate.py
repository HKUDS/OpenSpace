"""Authoritative post-proposer duplicate and arm-assignment gate."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from agentpilot.domain import (
    ArmSpec,
    ArmSlot,
    CompatibilityKey,
    DirectionMatch,
    DirectionMatchKind,
    PendingReservation,
    ReservationState,
    SlotAdherence,
    SlotAdherenceStatus,
    SlotKind,
    StructuredDirection,
    stable_id,
)
from agentpilot.domain.common import require_aware, require_text
from agentpilot.search.direction_semantic_index import (
    ArmAnchor,
    ArmAnchorIndex,
    DirectionApplicability,
    EncodedDirection,
    ExecutedDirectionMemberIndex,
    FrozenDirectionEncoder,
    PendingArmIdentityOverlay,
    PendingSemanticReservation,
    SemanticIndexError,
    SemanticThresholds,
    canonical_arm_input,
    canonical_realization_input,
    encode_direction_semantic_envelope,
)


@dataclass(frozen=True)
class SemanticGateDecision:
    arm_encoded: EncodedDirection
    encoded: EncodedDirection
    source_slot: ArmSlot
    compatibility: CompatibilityKey
    applicability: DirectionApplicability
    match: DirectionMatch
    adherence: SlotAdherence

    @property
    def selectable(self) -> bool:
        return self.adherence.status is SlotAdherenceStatus.ADHERENT


@dataclass(frozen=True)
class ReservedDirection:
    decision: SemanticGateDecision
    semantic_reservation: PendingSemanticReservation


class DirectionSemanticGate:
    """Use frozen distances for identity and repeats, never for reward."""

    def __init__(
        self,
        *,
        encoder: FrozenDirectionEncoder,
        thresholds: SemanticThresholds,
        anchors: ArmAnchorIndex,
        members: ExecutedDirectionMemberIndex,
        pending: PendingArmIdentityOverlay,
    ) -> None:
        if not isinstance(encoder, FrozenDirectionEncoder):
            raise TypeError("encoder must be FrozenDirectionEncoder")
        if not isinstance(thresholds, SemanticThresholds):
            raise TypeError("thresholds must be SemanticThresholds")
        expected_identity = encoder.identity
        for index in (anchors, members, pending):
            if index.embedding_identity != expected_identity:
                raise SemanticIndexError("semantic index encoder identity mismatch")
            if index.threshold_id != thresholds.threshold_id:
                raise SemanticIndexError("semantic index threshold identity mismatch")
        self.encoder = encoder
        self.thresholds = thresholds
        self.anchors = anchors
        self.members = members
        self.pending = pending

    def evaluate(
        self,
        *,
        arm_spec: ArmSpec,
        direction: StructuredDirection,
        source_slot: ArmSlot,
        compatibility: CompatibilityKey,
        applicability: DirectionApplicability,
    ) -> SemanticGateDecision:
        if not isinstance(arm_spec, ArmSpec):
            raise TypeError("arm_spec must be ArmSpec")
        dual = self.encoder.encode_dual(arm_spec, direction)
        arm_encoded = dual.arm
        encoded = dual.realization

        executed_repeats = self.members.repeat_neighbors(
            encoded.embedding,
            compatibility,
            applicability,
            tau_repeat=(
                self.thresholds.tau_repeat + self.thresholds.threshold_boundary_margin
            ),
        )
        pending_repeats = self.pending.repeat_neighbors(
            encoded.embedding,
            compatibility,
            applicability,
            tau_repeat=(
                self.thresholds.tau_repeat + self.thresholds.threshold_boundary_margin
            ),
        )
        repeat_rows = [
            (distance, member.arm_id, member.member_id, "executed_member")
            for distance, member in executed_repeats
        ] + [
            (
                distance,
                semantic.reservation.arm_id,
                semantic.reservation.reservation_id,
                "pending_reservation",
            )
            for distance, semantic in pending_repeats
        ]
        repeat_rows.sort(key=lambda row: (row[0], row[1], row[2]))
        if repeat_rows:
            distance, arm_id, member_id, source = repeat_rows[0]
            tied_arms = {
                row[1]
                for row in repeat_rows
                if abs(row[0] - distance) <= self.thresholds.nearest_arm_tie_tolerance
            }
            if self.thresholds.is_boundary_ambiguous(distance):
                match = DirectionMatch(
                    kind=DirectionMatchKind.AMBIGUOUS,
                    distance=distance,
                    reason="nearest repeat is inside the repeat threshold margin",
                )
            elif len(tied_arms) > 1:
                match = DirectionMatch(
                    kind=DirectionMatchKind.AMBIGUOUS,
                    distance=distance,
                    reason=("equidistant repeat evidence belongs to multiple arms"),
                )
            else:
                match = DirectionMatch(
                    kind=DirectionMatchKind.REPEAT,
                    distance=distance,
                    arm_id=arm_id,
                    member_id=member_id,
                    reason=f"near-repeat of {source}",
                )
            return SemanticGateDecision(
                arm_encoded=arm_encoded,
                encoded=encoded,
                source_slot=source_slot,
                compatibility=compatibility,
                applicability=applicability,
                match=match,
                adherence=self._slot_adherence(source_slot, match),
            )

        # An EXISTING slot is an instruction to generate a fresh D3 within its
        # already-selected D2 family.  A novel realization must never be used
        # to infer a different arm after allocation.
        if source_slot.kind is SlotKind.EXISTING:
            match = DirectionMatch(
                kind=DirectionMatchKind.EXISTING,
                distance=None,
                arm_id=source_slot.source_arm_id,
                reason="existing slot retains its allocated source arm",
            )
            return SemanticGateDecision(
                arm_encoded=arm_encoded,
                encoded=encoded,
                source_slot=source_slot,
                compatibility=compatibility,
                applicability=applicability,
                match=match,
                adherence=self._slot_adherence(source_slot, match),
            )

        assignment_rows = [
            (distance, anchor.arm_id, "anchor", anchor)
            for distance, anchor in self.anchors.neighbors(
                arm_encoded.embedding,
                compatibility,
                max_distance=2.0,
            )
        ] + [
            (
                distance,
                semantic.reservation.arm_id,
                "pending_new",
                semantic,
            )
            for distance, semantic in self.pending.new_arm_neighbors(
                arm_encoded.embedding,
                compatibility,
                tau_arm=2.0,
            )
        ]
        assignment_rows.sort(key=lambda row: (row[0], row[1], row[2]))
        if not assignment_rows:
            match = DirectionMatch(
                kind=DirectionMatchKind.NEW,
                distance=None,
                reason=("outside tau_arm of every compatible " "frozen/pending anchor"),
            )
        else:
            distance, arm_id, source, _record = assignment_rows[0]
            tied_arms = {
                row[1]
                for row in assignment_rows
                if abs(row[0] - distance) <= self.thresholds.nearest_arm_tie_tolerance
            }
            if self.thresholds.is_boundary_ambiguous(distance):
                match = DirectionMatch(
                    kind=DirectionMatchKind.AMBIGUOUS,
                    distance=distance,
                    reason="nearest anchor is inside the arm threshold margin",
                )
            elif len(tied_arms) > 1:
                match = DirectionMatch(
                    kind=DirectionMatchKind.AMBIGUOUS,
                    distance=distance,
                    reason="nearest semantic anchors are ambiguous",
                )
            elif distance > self.thresholds.tau_arm:
                match = DirectionMatch(
                    kind=DirectionMatchKind.NEW,
                    distance=distance,
                    reason=f"nearest {source} is outside tau_arm",
                )
            elif source == "pending_new":
                match = DirectionMatch(
                    kind=DirectionMatchKind.PENDING_NEW,
                    distance=distance,
                    arm_id=arm_id,
                    reason="matched a same-batch pending-new arm",
                )
            else:
                match = DirectionMatch(
                    kind=DirectionMatchKind.EXISTING,
                    distance=distance,
                    arm_id=arm_id,
                    reason="matched an immutable arm anchor",
                )
        return SemanticGateDecision(
            arm_encoded=arm_encoded,
            encoded=encoded,
            source_slot=source_slot,
            compatibility=compatibility,
            applicability=applicability,
            match=match,
            adherence=self._slot_adherence(source_slot, match),
        )

    def reserve_selected(
        self,
        *,
        decision: SemanticGateDecision,
        arm_spec: ArmSpec,
        direction: StructuredDirection,
        source_slot: ArmSlot,
        compatibility: CompatibilityKey,
        applicability: DirectionApplicability,
        attempt_id: str,
        action_id: str,
        decision_round_id: str,
        created_at: datetime,
    ) -> ReservedDirection:
        """Assign stable identity and append the pre-dispatch overlay."""

        if not decision.selectable:
            raise SemanticIndexError("only an adherent direction can be reserved")
        if decision.arm_encoded.canonical_input != canonical_arm_input(arm_spec):
            raise SemanticIndexError("gate decision belongs to a different arm spec")
        if decision.encoded.canonical_input != canonical_realization_input(direction):
            raise SemanticIndexError("gate decision belongs to a different realization")
        if source_slot != decision.source_slot:
            raise SemanticIndexError("gate decision belongs to a different source slot")
        if compatibility != decision.compatibility:
            raise SemanticIndexError("gate decision belongs to different compatibility")
        if applicability != decision.applicability:
            raise SemanticIndexError("gate decision belongs to different applicability")
        for value, name in (
            (attempt_id, "attempt_id"),
            (action_id, "action_id"),
            (decision_round_id, "decision_round_id"),
        ):
            require_text(value, name)
        require_aware(created_at, "created_at")

        match = decision.match
        establishes_new_arm = match.kind is DirectionMatchKind.NEW
        if establishes_new_arm:
            arm_id = stable_id("innovation-arm", attempt_id)
            assigned_match = replace(match, arm_id=arm_id)
        elif match.kind in {
            DirectionMatchKind.EXISTING,
            DirectionMatchKind.PENDING_NEW,
        }:
            arm_id = require_text(match.arm_id or "", "matched arm_id")
            assigned_match = match
        else:
            raise SemanticIndexError(f"cannot reserve match kind {match.kind.value}")

        reservation = PendingReservation(
            reservation_id=stable_id("pending-reservation", action_id),
            action_id=action_id,
            batch_id=self.pending.batch_id,
            decision_round_id=decision_round_id,
            arm_id=arm_id,
            direction_id=direction.direction_id,
            state=ReservationState.PLANNED,
            created_at=created_at,
            source_slot_kind=source_slot.kind,
            joined_existing_pending=(match.kind is DirectionMatchKind.PENDING_NEW),
        )
        semantic = PendingSemanticReservation(
            reservation=reservation,
            canonical_input=encode_direction_semantic_envelope(
                arm_encoded=decision.arm_encoded,
                realization_encoded=decision.encoded,
            ),
            embedding=decision.encoded.embedding,
            embedding_identity=decision.encoded.embedding_identity,
            threshold_id=self.thresholds.threshold_id,
            compatibility=compatibility,
            applicability=applicability,
            establishes_new_arm=establishes_new_arm,
        )
        self.pending.add(semantic)
        return ReservedDirection(
            decision=replace(decision, match=assigned_match),
            semantic_reservation=semantic,
        )

    def anchor_from_reservation(
        self,
        reserved: ReservedDirection,
        *,
        created_at: datetime,
    ) -> ArmAnchor:
        """Materialize the anchor established by the first new action."""

        semantic = reserved.semantic_reservation
        if not semantic.establishes_new_arm:
            raise SemanticIndexError(
                "only a new-arm reservation can establish an anchor"
            )
        require_aware(created_at, "created_at")
        return ArmAnchor(
            arm_id=semantic.reservation.arm_id,
            direction_id=semantic.reservation.direction_id,
            source_action_id=semantic.reservation.action_id,
            canonical_input=semantic.canonical_input,
            embedding=semantic.embedding,
            embedding_identity=semantic.embedding_identity,
            threshold_id=semantic.threshold_id,
            compatibility=semantic.compatibility,
            created_at=created_at,
        )

    @staticmethod
    def _slot_adherence(slot: ArmSlot, match: DirectionMatch) -> SlotAdherence:
        if match.kind is DirectionMatchKind.REPEAT:
            status = SlotAdherenceStatus.HARD_REPEAT
            reason = match.reason
        elif match.kind is DirectionMatchKind.AMBIGUOUS:
            status = SlotAdherenceStatus.AMBIGUOUS
            reason = match.reason
        elif slot.kind is SlotKind.EXISTING:
            if (
                match.kind is DirectionMatchKind.EXISTING
                and match.arm_id == slot.source_arm_id
            ):
                status = SlotAdherenceStatus.ADHERENT
                reason = "existing slot matched its source arm"
            else:
                status = SlotAdherenceStatus.MISMATCH
                reason = "existing slot did not match its source arm"
        elif match.kind in {
            DirectionMatchKind.NEW,
            DirectionMatchKind.PENDING_NEW,
        }:
            status = SlotAdherenceStatus.ADHERENT
            reason = "discover slot remained final-new"
        else:
            status = SlotAdherenceStatus.MISMATCH
            reason = "discover slot matched a pre-batch existing arm"
        return SlotAdherence(
            status=status,
            source_slot_id=slot.slot_id,
            matched_arm_id=match.arm_id,
            reason=reason,
        )
