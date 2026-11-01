"""D2 capacity-constrained allocation over existing arms and DISCOVER."""

from __future__ import annotations

from datetime import datetime

from agentpilot.domain.common import stable_id
from agentpilot.domain.policy import ArmAllocation, ArmSlot, SlotKind
from agentpilot.domain.search import SearchState
from agentpilot.policy.ucb.neighborhood import NeighborhoodKLUCB, seeded_top_k
from agentpilot.policy.ucb.observations import DirectionOpportunity, UCBTarget

_EXISTING_PREFIX = "existing:"
_DISCOVER_PREFIX = "discover:"


class ArmAllocator:
    """Reuse Direction-UCB evidence; no separate allocator learner exists."""

    def __init__(self, estimator: NeighborhoodKLUCB) -> None:
        self.estimator = estimator

    def allocate(
        self,
        *,
        state: SearchState,
        parent_id: str,
        decision_round_id: str,
        eligible_arm_ids: tuple[str, ...],
        cold_start: bool,
        generation_capacity: int,
        discover_quota: int,
        seed: int,
    ) -> ArmAllocation:
        if isinstance(generation_capacity, bool) or generation_capacity < 1:
            raise ValueError("generation_capacity must be positive")
        if (
            isinstance(discover_quota, bool)
            or discover_quota < 1
            or discover_quota > generation_capacity
        ):
            raise ValueError("discover_quota must be in [1, generation_capacity]")
        if len(eligible_arm_ids) != len(set(eligible_arm_ids)):
            raise ValueError("eligible arm ids must be unique")
        if any(not item for item in eligible_arm_ids):
            raise ValueError("eligible arm ids must be non-empty strings")
        if not isinstance(cold_start, bool):
            raise ValueError("cold_start must be boolean")
        if cold_start and eligible_arm_ids:
            raise ValueError("cold start cannot expose existing arms")

        values: dict[str, float] = {}
        arm_estimates: dict[str, dict] = {}
        for arm_id in eligible_arm_ids:
            estimate = self.estimator.estimate(
                target=UCBTarget.DIRECTION,
                compatibility=state.compatibility,
                descriptor=state.direction_descriptor,
                cutoff=state.cutoff,
                arm_id=arm_id,
            )
            key = f"{_EXISTING_PREFIX}{arm_id}"
            values[key] = estimate.index
            arm_estimates[arm_id] = {
                "index": estimate.index,
                "T": estimate.opportunities,
                "n": estimate.observations,
                "successes": estimate.successes,
                "level": estimate.level,
            }

        discover = self.estimator.estimate(
            target=UCBTarget.DISCOVER,
            compatibility=state.compatibility,
            descriptor=state.direction_descriptor,
            cutoff=state.cutoff,
            arm_id=None,
        )
        # DISCOVER is a quota, not another support item that an optimistic
        # existing arm may crowd out. Cold start fills the whole slate with
        # discovery; later rounds reserve the configured minimum and use the
        # strongest existing arms for the remaining slots.
        virtual_count = generation_capacity
        for ordinal in range(virtual_count):
            values[f"{_DISCOVER_PREFIX}{ordinal}"] = discover.index
        if cold_start:
            selected, tie_sets = seeded_top_k(
                {
                    key: value
                    for key, value in values.items()
                    if key.startswith(_DISCOVER_PREFIX)
                },
                k=generation_capacity,
                seed=seed,
                tie_tolerance=self.estimator.config.tie_tolerance,
            )
        else:
            existing_values = {
                key: value
                for key, value in values.items()
                if key.startswith(_EXISTING_PREFIX)
            }
            existing_count = min(
                len(existing_values), generation_capacity - discover_quota
            )
            if existing_count:
                selected_existing, existing_ties = seeded_top_k(
                    existing_values,
                    k=existing_count,
                    seed=seed,
                    tie_tolerance=self.estimator.config.tie_tolerance,
                )
            else:
                selected_existing, existing_ties = (), ()
            discover_count = generation_capacity - len(selected_existing)
            discover_values = {
                key: value
                for key, value in values.items()
                if key.startswith(_DISCOVER_PREFIX)
            }
            selected_discover, discover_ties = seeded_top_k(
                discover_values,
                k=discover_count,
                seed=seed ^ 0x5A17,
                tie_tolerance=self.estimator.config.tie_tolerance,
            )
            selected, ordering_ties = seeded_top_k(
                {
                    key: values[key]
                    for key in (*selected_existing, *selected_discover)
                },
                k=generation_capacity,
                seed=seed ^ 0xA51D,
                tie_tolerance=self.estimator.config.tie_tolerance,
            )
            tie_sets = (*existing_ties, *discover_ties, *ordering_ties)
        allocation_id = stable_id(
            "arm-allocation", state.state_id, decision_round_id, parent_id, seed
        )
        slots = []
        for rank, key in enumerate(selected):
            if key.startswith(_EXISTING_PREFIX):
                arm_id = key[len(_EXISTING_PREFIX) :]
                kind = SlotKind.EXISTING
            else:
                arm_id = None
                kind = SlotKind.DISCOVER
            slots.append(
                ArmSlot(
                    slot_id=stable_id("arm-slot", allocation_id, key),
                    kind=kind,
                    ordinal=rank,
                    source_arm_id=arm_id,
                )
            )
        return ArmAllocation(
            allocation_id=allocation_id,
            decision_round_id=decision_round_id,
            state_id=state.state_id,
            parent_id=parent_id,
            slots=tuple(slots),
            trace={
                "seed": seed,
                "generation_capacity": generation_capacity,
                "discover_quota": discover_quota,
                "cold_start": cold_start,
                "selected_discover_count": sum(
                    key.startswith(_DISCOVER_PREFIX) for key in selected
                ),
                "eligible_arm_ids": list(eligible_arm_ids),
                "discover_supported": True,
                "support_indices": dict(values),
                "selected_support_ids": list(selected),
                "tie_sets": [list(items) for items in tie_sets],
                "arm_estimates": arm_estimates,
                "discover_estimate": {
                    "index": discover.index,
                    "T": discover.opportunities,
                    "n": discover.observations,
                    "successes": discover.successes,
                    "level": discover.level,
                },
            },
        )

    @staticmethod
    def opportunity(
        allocation: ArmAllocation,
        state: SearchState,
        *,
        available_at: datetime,
    ) -> DirectionOpportunity:
        """Build the immutable pre-allocation support record.

        The support comes from the trace, not merely from allocated slots;
        otherwise non-selected existing arms would incorrectly stop accruing
        decision opportunities.
        """

        if allocation.state_id != state.state_id:
            raise ValueError("allocation and search state identities differ")
        raw_arms = allocation.trace.get("eligible_arm_ids")
        if not isinstance(raw_arms, (list, tuple)) or any(
            not isinstance(item, str) or not item for item in raw_arms
        ):
            raise ValueError("allocation trace lacks frozen eligible arm support")
        discover_supported = allocation.trace.get("discover_supported")
        if discover_supported is not True:
            raise ValueError("allocation trace must retain permanent DISCOVER support")
        return DirectionOpportunity(
            opportunity_id=stable_id(
                "direction-opportunity", allocation.decision_round_id
            ),
            decision_round_id=allocation.decision_round_id,
            eligible_arm_ids=tuple(raw_arms),
            discover_supported=True,
            compatibility=state.compatibility,
            descriptor=state.direction_descriptor,
            available_at=available_at,
        )
