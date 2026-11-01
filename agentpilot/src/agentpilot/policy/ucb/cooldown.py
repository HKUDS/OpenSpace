"""Opportunity-counted arm eligibility cooldowns, separate from reward."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from agentpilot.domain.common import require_text
from agentpilot.domain.search import CompatibilityKey
from agentpilot.policy.ucb.arms import ArmRegistrySnapshot


@dataclass(frozen=True)
class ArmCooldownConfig:
    max_slot_generation_failures: int
    slot_generation_cooldown_opportunities: int
    max_retryable_skips: int
    skip_cooldown_opportunities: int

    def __post_init__(self) -> None:
        for name in (
            "max_slot_generation_failures",
            "slot_generation_cooldown_opportunities",
            "max_retryable_skips",
            "skip_cooldown_opportunities",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class ArmEligibilityRound:
    decision_round_id: str
    compatibility: CompatibilityKey
    opportunity_seq: int
    generation_succeeded: Mapping[str, bool]
    selected_arm_id: str | None
    retryable_skip: bool

    def __post_init__(self) -> None:
        require_text(self.decision_round_id, "decision_round_id")
        if self.selected_arm_id is not None:
            require_text(self.selected_arm_id, "selected_arm_id")
        if isinstance(self.opportunity_seq, bool) or self.opportunity_seq < 1:
            raise ValueError("opportunity_seq must be positive")
        if any(
            not isinstance(arm_id, str) or not arm_id or not isinstance(succeeded, bool)
            for arm_id, succeeded in self.generation_succeeded.items()
        ):
            raise ValueError("generation status must map arm ids to booleans")


@dataclass(frozen=True)
class ArmCooldownWindow:
    arm_id: str
    reason: str
    triggered_at_seq: int
    excluded_through_seq: int


@dataclass(frozen=True)
class ArmEligibilityReport:
    compatibility: CompatibilityKey
    opportunity_seq: int
    base_arm_ids: tuple[str, ...]
    eligible_arm_ids: tuple[str, ...]
    exclusions: tuple[ArmCooldownWindow, ...]


class ArmEligibilityController:
    """Append completed rounds and project cooldown-only eligibility.

    A cooldown never writes a success/failure observation and never changes a
    KL-UCB index.  Expiry advances on completed compatible D2 opportunities,
    including rounds where a cooled arm is absent from support.
    """

    def __init__(self, config: ArmCooldownConfig) -> None:
        self.config = config
        self._rounds: dict[str, ArmEligibilityRound] = {}

    def reset_projection(self) -> None:
        """Clear completed rounds before ordered durable replay."""

        self._rounds.clear()

    def record_round(
        self,
        *,
        decision_round_id: str,
        compatibility: CompatibilityKey,
        generation_succeeded: Mapping[str, bool],
        selected_arm_id: str | None,
        retryable_skip: bool,
        replay_key: str | None = None,
    ) -> ArmEligibilityRound:
        require_text(decision_round_id, "decision_round_id")
        key = replay_key or decision_round_id
        require_text(key, "replay_key")
        if not isinstance(retryable_skip, bool):
            raise ValueError("retryable_skip must be boolean")
        if selected_arm_id is None:
            if retryable_skip:
                raise ValueError("planning failure cannot be a retryable skip")
            selected = None
        else:
            require_text(selected_arm_id, "selected_arm_id")
            selected = selected_arm_id
        previous = self._rounds.get(key)
        sequence = (
            previous.opportunity_seq
            if previous is not None
            else 1
            + sum(item.compatibility == compatibility for item in self._rounds.values())
        )
        record = ArmEligibilityRound(
            decision_round_id=decision_round_id,
            compatibility=compatibility,
            opportunity_seq=sequence,
            generation_succeeded=dict(generation_succeeded),
            selected_arm_id=selected,
            retryable_skip=retryable_skip,
        )
        if previous is not None:
            if previous != record:
                raise ValueError("eligibility round replay has immutable drift")
            return previous
        self._rounds[key] = record
        return record

    def get_round(self, decision_round_id: str) -> ArmEligibilityRound | None:
        return self._rounds.get(decision_round_id)

    def report(
        self, snapshot: ArmRegistrySnapshot, *, base_arm_ids: tuple[str, ...],
    ) -> ArmEligibilityReport:
        if len(base_arm_ids) != len(set(base_arm_ids)):
            raise ValueError("base arm ids must be unique")
        if not set(base_arm_ids) <= set(snapshot.arm_ids):
            raise ValueError("base eligibility must belong to the frozen arm snapshot")
        rows = sorted(
            (
                item
                for item in self._rounds.values()
                if item.compatibility == snapshot.compatibility
            ),
            key=lambda item: item.opportunity_seq,
        )
        next_seq = len(rows) + 1
        generation_failures: dict[str, int] = {}
        retryable_skips: dict[str, int] = {}
        windows: dict[tuple[str, str], ArmCooldownWindow] = {}
        for row in rows:
            for arm_id, succeeded in row.generation_succeeded.items():
                if succeeded:
                    generation_failures[arm_id] = 0
                    continue
                count = generation_failures.get(arm_id, 0) + 1
                if count >= self.config.max_slot_generation_failures:
                    windows[(arm_id, "slot_generation_failure")] = ArmCooldownWindow(
                        arm_id=arm_id,
                        reason="slot_generation_failure",
                        triggered_at_seq=row.opportunity_seq,
                        excluded_through_seq=(
                            row.opportunity_seq
                            + self.config.slot_generation_cooldown_opportunities
                        ),
                    )
                    count = 0
                generation_failures[arm_id] = count

            arm_id = row.selected_arm_id
            if arm_id is None:
                continue
            if row.retryable_skip:
                count = retryable_skips.get(arm_id, 0) + 1
                if count >= self.config.max_retryable_skips:
                    windows[(arm_id, "retryable_skip")] = ArmCooldownWindow(
                        arm_id=arm_id,
                        reason="retryable_skip",
                        triggered_at_seq=row.opportunity_seq,
                        excluded_through_seq=(
                            row.opportunity_seq
                            + self.config.skip_cooldown_opportunities
                        ),
                    )
                    count = 0
                retryable_skips[arm_id] = count
            else:
                retryable_skips[arm_id] = 0

        active = tuple(
            sorted(
                (
                    item
                    for item in windows.values()
                    if next_seq <= item.excluded_through_seq
                    and item.arm_id in base_arm_ids
                ),
                key=lambda item: (item.arm_id, item.reason),
            )
        )
        excluded_ids = {item.arm_id for item in active}
        return ArmEligibilityReport(
            compatibility=snapshot.compatibility,
            opportunity_seq=next_seq,
            base_arm_ids=base_arm_ids,
            eligible_arm_ids=tuple(
                arm_id for arm_id in base_arm_ids if arm_id not in excluded_ids
            ),
            exclusions=active,
        )

    def rounds(self) -> tuple[ArmEligibilityRound, ...]:
        return tuple(
            sorted(
                self._rounds.values(),
                key=lambda item: (
                    item.compatibility.objective_id,
                    item.opportunity_seq,
                    item.decision_round_id,
                ),
            )
        )


__all__ = [
    "ArmCooldownConfig",
    "ArmCooldownWindow",
    "ArmEligibilityController",
    "ArmEligibilityReport",
    "ArmEligibilityRound",
]
