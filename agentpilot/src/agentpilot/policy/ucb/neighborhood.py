"""Frozen-neighborhood Bernoulli KL-UCB primitives."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Mapping, Protocol

from agentpilot.domain.search import (
    CompatibilityKey,
    EvidenceCutoff,
    StateDescriptor,
)
from agentpilot.policy.ucb.observations import (
    ArmCreationOpportunity,
    BernoulliObservation,
    DirectionOpportunity,
    ParentOpportunity,
    UCBTarget,
)


class NearStatePredicate(Protocol):
    """Run-frozen per-block neighborhood membership predicate."""

    def __call__(
        self,
        observed: StateDescriptor,
        query: StateDescriptor,
        level: str,
    ) -> bool: ...


class UCBHistoryView(Protocol):
    """Read boundary implemented by in-memory or durable policy history."""

    def observations(
        self, cutoff: EvidenceCutoff
    ) -> tuple[BernoulliObservation, ...]: ...

    def parent_opportunities(
        self, cutoff: EvidenceCutoff
    ) -> tuple[ParentOpportunity, ...]: ...

    def direction_opportunities(
        self, cutoff: EvidenceCutoff
    ) -> tuple[DirectionOpportunity, ...]: ...

    def creation_transfers(
        self, cutoff: EvidenceCutoff
    ) -> tuple[ArmCreationOpportunity, ...]: ...


@dataclass(frozen=True)
class NeighborhoodConfig:
    fallback_levels: tuple[str, ...]
    min_neighborhood_observations: int
    exploration_constant: float
    solver_tolerance: float
    solver_max_iterations: int
    tie_tolerance: float

    def __post_init__(self) -> None:
        if not self.fallback_levels or any(not item for item in self.fallback_levels):
            raise ValueError("fallback_levels must be explicitly non-empty")
        if len(set(self.fallback_levels)) != len(self.fallback_levels):
            raise ValueError("fallback levels must be unique")
        if (
            isinstance(self.min_neighborhood_observations, bool)
            or self.min_neighborhood_observations < 1
        ):
            raise ValueError("min_neighborhood_observations must be positive")
        if (
            not math.isfinite(self.exploration_constant)
            or self.exploration_constant < 0
        ):
            raise ValueError("exploration_constant must be finite and non-negative")
        if not math.isfinite(self.solver_tolerance) or not (
            0.0 < self.solver_tolerance < 1.0
        ):
            raise ValueError("solver_tolerance must be in (0, 1)")
        if (
            isinstance(self.solver_max_iterations, bool)
            or self.solver_max_iterations < 1
        ):
            raise ValueError("solver_max_iterations must be positive")
        if not math.isfinite(self.tie_tolerance) or self.tie_tolerance < 0.0:
            raise ValueError("tie_tolerance must be finite and non-negative")


@dataclass(frozen=True)
class NeighborhoodEstimate:
    target: UCBTarget
    arm_id: str | None
    level: str
    opportunities: int
    observations: int
    successes: int
    empirical_mean: float
    index: float
    observation_ids: tuple[str, ...]
    opportunity_round_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 0 <= self.successes <= self.observations <= self.opportunities:
            raise ValueError("estimate violates 0 <= successes <= n <= T")


def bernoulli_kl(p: float, q: float) -> float:
    if not 0.0 <= p <= 1.0 or not 0.0 <= q <= 1.0:
        raise ValueError("Bernoulli probabilities must be in [0, 1]")
    if p == q:
        return 0.0
    if p == 0.0:
        return math.inf if q == 1.0 else -math.log1p(-q)
    if p == 1.0:
        return math.inf if q == 0.0 else -math.log(q)
    if q in (0.0, 1.0):
        return math.inf
    return p * math.log(p / q) + (1.0 - p) * math.log((1.0 - p) / (1.0 - q))


def exploration_bound(opportunities: int, exploration_constant: float) -> float:
    if isinstance(opportunities, bool) or opportunities < 0:
        raise ValueError("opportunities must be non-negative")
    if not math.isfinite(exploration_constant) or exploration_constant < 0.0:
        raise ValueError("exploration_constant must be finite and non-negative")
    return math.log1p(opportunities) + exploration_constant * math.log1p(
        math.log1p(opportunities)
    )


def kl_ucb_index(
    *,
    successes: int,
    observations: int,
    opportunities: int,
    exploration_constant: float,
    tolerance: float,
    max_iterations: int,
) -> float:
    if any(
        isinstance(value, bool) for value in (successes, observations, opportunities)
    ):
        raise ValueError("KL-UCB counts must be integers")
    if not 0 <= successes <= observations <= opportunities:
        raise ValueError("KL-UCB requires 0 <= successes <= n <= T")
    if observations == 0:
        return 1.0
    if not 0.0 < tolerance < 1.0 or max_iterations < 1:
        raise ValueError("invalid KL solver controls")
    empirical = successes / observations
    if empirical == 1.0:
        return 1.0
    budget = exploration_bound(opportunities, exploration_constant) / observations
    low, high = empirical, 1.0
    for _ in range(max_iterations):
        if high - low <= tolerance:
            break
        middle = (low + high) / 2.0
        if bernoulli_kl(empirical, middle) <= budget:
            low = middle
        else:
            high = middle
    return max(empirical, min(1.0, low))


class NeighborhoodKLUCB:
    def __init__(
        self,
        history: UCBHistoryView,
        predicate: NearStatePredicate,
        config: NeighborhoodConfig,
    ) -> None:
        self.history = history
        self.predicate = predicate
        self.config = config

    def estimate(
        self,
        *,
        target: UCBTarget,
        compatibility: CompatibilityKey,
        descriptor: StateDescriptor,
        cutoff: EvidenceCutoff,
        arm_id: str | None,
    ) -> NeighborhoodEstimate:
        if target is UCBTarget.DIRECTION and not arm_id:
            raise ValueError("direction estimate requires arm_id")
        if target is not UCBTarget.DIRECTION and arm_id is not None:
            raise ValueError("only direction estimate accepts arm_id")

        visible = [
            item
            for item in self.history.observations(cutoff)
            if item.target is target
            and item.compatibility == compatibility
            and (target is not UCBTarget.DIRECTION or item.arm_id == arm_id)
        ]
        chosen_level = self.config.fallback_levels[-1]
        selected = []
        for level in self.config.fallback_levels:
            candidate = [
                item
                for item in visible
                if self.predicate(item.descriptor, descriptor, level)
            ]
            selected = candidate
            chosen_level = level
            if len(candidate) >= self.config.min_neighborhood_observations:
                break

        rounds: set[str] = set()
        if target is UCBTarget.PARENT:
            for item in self.history.parent_opportunities(cutoff):
                if item.compatibility == compatibility and any(
                    self.predicate(candidate, descriptor, chosen_level)
                    for candidate in item.eligible_parent_descriptors.values()
                ):
                    rounds.add(item.decision_round_id)
        elif target is UCBTarget.DISCOVER:
            for item in self.history.direction_opportunities(cutoff):
                if (
                    item.discover_supported
                    and item.compatibility == compatibility
                    and self.predicate(item.descriptor, descriptor, chosen_level)
                ):
                    rounds.add(item.decision_round_id)
        else:
            assert arm_id is not None
            for item in self.history.direction_opportunities(cutoff):
                if (
                    arm_id in item.eligible_arm_ids
                    and item.compatibility == compatibility
                    and self.predicate(item.descriptor, descriptor, chosen_level)
                ):
                    rounds.add(item.decision_round_id)
            for item in self.history.creation_transfers(cutoff):
                if (
                    item.arm_id == arm_id
                    and item.compatibility == compatibility
                    and self.predicate(item.descriptor, descriptor, chosen_level)
                ):
                    rounds.add(item.decision_round_id)

        n = len(selected)
        successes = sum(item.value for item in selected)
        opportunities = len(rounds)
        if n > opportunities:
            raise ValueError(
                "history violates T >= n in the selected compatible neighborhood"
            )
        empirical = successes / n if n else 0.0
        index = kl_ucb_index(
            successes=successes,
            observations=n,
            opportunities=opportunities,
            exploration_constant=self.config.exploration_constant,
            tolerance=self.config.solver_tolerance,
            max_iterations=self.config.solver_max_iterations,
        )
        return NeighborhoodEstimate(
            target=target,
            arm_id=arm_id,
            level=chosen_level,
            opportunities=opportunities,
            observations=n,
            successes=successes,
            empirical_mean=empirical,
            index=index,
            observation_ids=tuple(sorted(item.observation_id for item in selected)),
            opportunity_round_ids=tuple(sorted(rounds)),
        )


def seeded_argmax(
    values: Mapping[str, float], *, seed: int, tie_tolerance: float
) -> tuple[str, tuple[str, ...]]:
    if not values:
        raise ValueError("cannot select from empty values")
    if tie_tolerance < 0.0 or not math.isfinite(tie_tolerance):
        raise ValueError("tie_tolerance must be finite and non-negative")
    if any(not math.isfinite(value) for value in values.values()):
        raise ValueError("selection indices must be finite")
    maximum = max(values.values())
    ties = tuple(
        sorted(
            key
            for key, value in values.items()
            if math.isclose(value, maximum, rel_tol=0.0, abs_tol=tie_tolerance)
        )
    )
    return random.Random(seed).choice(ties), ties


def seeded_top_k(
    values: Mapping[str, float],
    *,
    k: int,
    seed: int,
    tie_tolerance: float,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    if isinstance(k, bool) or k < 1:
        raise ValueError("k must be positive")
    if not values:
        return (), ()
    remaining = dict(values)
    rng = random.Random(seed)
    selected: list[str] = []
    tie_sets: list[tuple[str, ...]] = []
    for _ in range(min(k, len(remaining))):
        maximum = max(remaining.values())
        ties = tuple(
            sorted(
                key
                for key, value in remaining.items()
                if math.isclose(value, maximum, rel_tol=0.0, abs_tol=tie_tolerance)
            )
        )
        chosen = rng.choice(ties)
        selected.append(chosen)
        tie_sets.append(ties)
        del remaining[chosen]
    return tuple(selected), tuple(tie_sets)
