"""Strict slot-adherence checks and D3 Direction UCB selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agentpilot.domain.common import stable_id
from agentpilot.domain.policy import (
    ArmSlot,
    DirectionCandidate,
    DirectionMatch,
    DirectionMatchKind,
    DirectionSelection,
    DirectionSlate,
    SlotAdherence,
    SlotAdherenceStatus,
    SlotKind,
)
from agentpilot.domain.search import SearchState
from agentpilot.policy.ucb.neighborhood import (
    NeighborhoodEstimate,
    NeighborhoodKLUCB,
    seeded_argmax,
)
from agentpilot.policy.ucb.observations import SelectedActionRecord, UCBTarget


class NoValidDirection(RuntimeError):
    """Typed termination: repair exhausted and no candidate may enter D3."""

    def __init__(self, message: str, *, trace: tuple[object, ...] = ()) -> None:
        super().__init__(message)
        self.trace = trace


def classify_slot_adherence(slot: ArmSlot, match: DirectionMatch) -> SlotAdherence:
    if match.kind is DirectionMatchKind.REPEAT:
        return SlotAdherence(
            status=SlotAdherenceStatus.HARD_REPEAT,
            source_slot_id=slot.slot_id,
            matched_arm_id=match.arm_id,
            reason=match.reason or "semantic repeat gate rejected the direction",
        )
    if match.kind is DirectionMatchKind.AMBIGUOUS:
        return SlotAdherence(
            status=SlotAdherenceStatus.AMBIGUOUS,
            source_slot_id=slot.slot_id,
            matched_arm_id=match.arm_id,
            reason=match.reason or "semantic assignment is ambiguous",
        )
    if slot.kind is SlotKind.EXISTING:
        adherent = (
            match.kind is DirectionMatchKind.EXISTING
            and match.arm_id == slot.source_arm_id
        )
        return SlotAdherence(
            status=(
                SlotAdherenceStatus.ADHERENT
                if adherent
                else SlotAdherenceStatus.MISMATCH
            ),
            source_slot_id=slot.slot_id,
            matched_arm_id=match.arm_id,
            reason=(
                "matched the allocated existing arm"
                if adherent
                else "existing-arm slot did not match its source arm"
            ),
        )
    adherent = match.kind in {
        DirectionMatchKind.NEW,
        DirectionMatchKind.PENDING_NEW,
    }
    return SlotAdherence(
        status=(
            SlotAdherenceStatus.ADHERENT if adherent else SlotAdherenceStatus.MISMATCH
        ),
        source_slot_id=slot.slot_id,
        matched_arm_id=match.arm_id,
        reason=(
            "direction remains new relative to the frozen registry"
            if adherent
            else "DISCOVER slot matched a frozen existing arm"
        ),
    )


def generation_failed(slot: ArmSlot, reason: str) -> SlotAdherence:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("generation failure needs a typed reason")
    return SlotAdherence(
        status=SlotAdherenceStatus.GENERATION_FAILED,
        source_slot_id=slot.slot_id,
        reason=reason.strip(),
    )


@dataclass(frozen=True)
class DirectionScore:
    candidate_id: str
    estimate: NeighborhoodEstimate


class DirectionUCB:
    def __init__(self, estimator: NeighborhoodKLUCB) -> None:
        self.estimator = estimator

    @staticmethod
    def _validated_candidates(
        slate: DirectionSlate,
    ) -> tuple[DirectionCandidate, ...]:
        adherent = []
        for candidate in slate.candidates:
            if (
                candidate.source_slot is None
                or candidate.match is None
                or candidate.adherence is None
            ):
                raise ValueError("DirectionUCB requires a UCB-typed direction slate")
            if candidate.adherence.source_slot_id != candidate.source_slot.slot_id:
                raise ValueError("candidate adherence references another source slot")
            if candidate.adherence.adherent:
                if candidate.source_slot.kind is SlotKind.EXISTING:
                    structurally_adherent = (
                        candidate.match.kind is DirectionMatchKind.EXISTING
                        and candidate.match.arm_id
                        == candidate.source_slot.source_arm_id
                    )
                else:
                    structurally_adherent = candidate.match.kind in {
                        DirectionMatchKind.NEW,
                        DirectionMatchKind.PENDING_NEW,
                    }
                if not structurally_adherent:
                    raise ValueError(
                        "candidate carries inconsistent slot adherence: "
                        "typed slot/match invariant failed"
                    )
                adherent.append(candidate)
        if not adherent:
            raise NoValidDirection("no-valid-direction: D3 slate is empty after repair")

        # Existing or pending arms get at most one realization in D3.  The
        # smallest candidate id is a fixed, model-independent contraction.
        kept: dict[str, DirectionCandidate] = {}
        for candidate in adherent:
            if candidate.source_slot.kind is SlotKind.EXISTING:
                group = f"existing:{candidate.match.arm_id}"
            elif candidate.match.kind is DirectionMatchKind.PENDING_NEW:
                group = f"pending:{candidate.match.arm_id}"
            else:
                group = f"discover:{candidate.candidate_id}"
            previous = kept.get(group)
            if previous is None or candidate.candidate_id < previous.candidate_id:
                kept[group] = candidate
        return tuple(sorted(kept.values(), key=lambda item: item.candidate_id))

    def score(
        self, slate: DirectionSlate, state: SearchState
    ) -> tuple[DirectionScore, ...]:
        if slate.state_id != state.state_id:
            raise ValueError("direction slate and search state identities differ")
        candidates = self._validated_candidates(slate)
        result = []
        for candidate in candidates:
            if candidate.source_slot.kind is SlotKind.DISCOVER:
                target, arm_id = UCBTarget.DISCOVER, None
            else:
                target, arm_id = UCBTarget.DIRECTION, candidate.match.arm_id
            result.append(
                DirectionScore(
                    candidate_id=candidate.candidate_id,
                    estimate=self.estimator.estimate(
                        target=target,
                        compatibility=state.compatibility,
                        descriptor=state.direction_descriptor,
                        cutoff=state.cutoff,
                        arm_id=arm_id,
                    ),
                )
            )
        return tuple(result)

    def select(
        self,
        slate: DirectionSlate,
        state: SearchState,
        *,
        seed: int,
    ) -> DirectionSelection:
        scores = self.score(slate, state)
        values = {item.candidate_id: item.estimate.index for item in scores}
        selected_id, ties = seeded_argmax(
            values,
            seed=seed,
            tie_tolerance=self.estimator.config.tie_tolerance,
        )
        candidate = next(
            item for item in slate.candidates if item.candidate_id == selected_id
        )
        return DirectionSelection(
            selection_id=stable_id(
                "direction-selection", slate.slate_id, slate.decision_round_id, seed
            ),
            slate_id=slate.slate_id,
            candidate_id=candidate.candidate_id,
            direction_id=candidate.direction.direction_id,
            trace={
                "decision_round_id": slate.decision_round_id,
                "eligible_candidate_ids": list(values),
                "tie_ids": list(ties),
                "seed": seed,
                "conditional_probability": 1.0,
                "indices": values,
                "estimates": {
                    item.candidate_id: {
                        "target": item.estimate.target.value,
                        "arm_id": item.estimate.arm_id,
                        "T": item.estimate.opportunities,
                        "n": item.estimate.observations,
                        "successes": item.estimate.successes,
                        "level": item.estimate.level,
                    }
                    for item in scores
                },
            },
        )

    @staticmethod
    def selected_action_record(
        selection: DirectionSelection,
        slate: DirectionSlate,
        *,
        action_id: str,
        available_at: datetime,
    ) -> SelectedActionRecord:
        if selection.slate_id != slate.slate_id:
            raise ValueError("direction selection belongs to another slate")
        if selection.candidate_id not in {
            item.candidate_id for item in slate.candidates
        }:
            raise ValueError("selected candidate is absent from its frozen slate")
        return SelectedActionRecord(
            record_id=stable_id("selected-action", action_id),
            decision_round_id=slate.decision_round_id,
            action_id=action_id,
            available_at=available_at,
        )
