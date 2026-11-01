"""Cutoff-safe statistical catalog of dynamically created InnovationArms.

Semantic matching, pending identity assignment, and reservation transitions
belong exclusively to :class:`search.DirectionSemanticGate`.  This catalog
only retains already-dispatched arm metadata for later frozen UCB batches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agentpilot.domain.common import require_aware, require_text
from agentpilot.domain.policy import ArmSpec, StructuredDirection
from agentpilot.domain.search import CompatibilityKey, EvidenceCutoff
from agentpilot.search.direction_semantic_index import ArmAnchor


@dataclass(frozen=True)
class InnovationArm:
    arm_id: str
    compatibility: CompatibilityKey
    arm_spec: ArmSpec
    anchor_direction: StructuredDirection
    creation_action_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        require_text(self.arm_id, "arm_id")
        require_text(self.creation_action_id, "creation_action_id")
        require_aware(self.created_at, "created_at")
        if not isinstance(self.arm_spec, ArmSpec):
            raise TypeError("arm_spec must be ArmSpec")

    @classmethod
    def from_anchor(
        cls, anchor: ArmAnchor, direction: StructuredDirection,
    ) -> "InnovationArm":
        """Project an authoritative semantic anchor into statistical metadata."""

        if anchor.direction_id != direction.direction_id:
            raise ValueError("anchor and structured direction identities differ")
        return cls(
            arm_id=anchor.arm_id,
            compatibility=anchor.compatibility,
            arm_spec=ArmSpec(failure_mechanism=direction.failure_mechanism),
            anchor_direction=direction,
            creation_action_id=anchor.source_action_id,
            created_at=anchor.created_at,
        )


@dataclass(frozen=True)
class ArmRegistrySnapshot:
    compatibility: CompatibilityKey
    cutoff: EvidenceCutoff
    arms: tuple[InnovationArm, ...]

    @property
    def arm_ids(self) -> tuple[str, ...]:
        return tuple(item.arm_id for item in self.arms)


class InnovationArmRegistry:
    """Append-only arm metadata; deliberately has no semantic-index methods."""

    def __init__(self) -> None:
        self._arms: dict[str, InnovationArm] = {}

    def reset_projection(self) -> None:
        """Clear metadata that will be reconstructed from durable events."""

        self._arms.clear()

    def register_dispatched(
        self, anchor: ArmAnchor, direction: StructuredDirection,
    ) -> InnovationArm:
        """Register the arm after the gate materializes its dispatch anchor."""

        arm = InnovationArm.from_anchor(anchor, direction)
        previous = self._arms.get(arm.arm_id)
        if previous is not None and previous != arm:
            raise ValueError(f"arm {arm.arm_id!r} has immutable metadata drift")
        previous_action = next(
            (
                item
                for item in self._arms.values()
                if item.creation_action_id == arm.creation_action_id
            ),
            None,
        )
        if previous_action is not None and previous_action != arm:
            raise ValueError("one dispatched creation action cannot create two arms")
        self._arms[arm.arm_id] = arm
        return arm

    def register_existing(self, arm: InnovationArm) -> None:
        """Load exactly replayed durable metadata during process assembly."""

        previous = self._arms.get(arm.arm_id)
        if previous is not None and previous != arm:
            raise ValueError(f"arm {arm.arm_id!r} has immutable metadata drift")
        self._arms[arm.arm_id] = arm

    def snapshot(
        self, *, compatibility: CompatibilityKey, cutoff: EvidenceCutoff,
    ) -> ArmRegistrySnapshot:
        arms = tuple(
            sorted(
                (
                    item
                    for item in self._arms.values()
                    if item.compatibility == compatibility
                    and cutoff.allows(item.created_at)
                ),
                key=lambda item: item.arm_id,
            )
        )
        return ArmRegistrySnapshot(
            compatibility=compatibility, cutoff=cutoff, arms=arms,
        )

    def get_arm(self, arm_id: str) -> InnovationArm | None:
        require_text(arm_id, "arm_id")
        return self._arms.get(arm_id)

    def all(self) -> tuple[InnovationArm, ...]:
        return tuple(self._arms[key] for key in sorted(self._arms))
