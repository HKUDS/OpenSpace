"""Cutoff-safe, durable evaluation plans frozen before batch dispatch.

These records belong to the trusted evaluation control plane.  In particular,
``FrozenPairedPlan`` and ``SamplingDraw`` may contain private adaptive-B task
identities and must never be projected into proposer or worker evidence.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from agentpilot.domain.common import deep_freeze, require_text, stable_id
from agentpilot.domain.evaluation import EvidenceTier
from agentpilot.domain.search import CompatibilityKey, EvidenceCutoff
from agentpilot.evaluation.paired import FrozenPairedPlan
from agentpilot.evaluation.sampling import SamplingDraw

HT_ESTIMATOR_CONTRACT = "horvitz-thompson-paired-delta-replicates-v2"
UNIFORM_ESTIMATOR_CONTRACT = "uniform-complete-paired-replicates-v2"
LEGACY_HT_ESTIMATOR_CONTRACT = "horvitz-thompson-paired-delta"
LEGACY_UNIFORM_ESTIMATOR_CONTRACT = "uniform-complete-paired"


@dataclass(frozen=True)
class FrozenInformationPriorSnapshot:
    """The complete information-weighting input visible at one state cutoff."""

    snapshot_id: str
    run_id: str
    batch_id: str
    state_id: str
    cutoff: EvidenceCutoff
    compatibility: CompatibilityKey
    source_scope_id: str
    task_counts: Mapping[str, tuple[int, int]]
    task_weights: Mapping[str, float]
    weighting_contract: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        batch_id: str,
        state_id: str,
        cutoff: EvidenceCutoff,
        compatibility: CompatibilityKey,
        source_scope_id: str,
        task_counts: Mapping[str, tuple[int, int]],
        task_weights: Mapping[str, float],
        weighting_contract: str,
    ) -> FrozenInformationPriorSnapshot:
        normalized_counts = {
            require_text(task_id, "task count id"): tuple(counts)
            for task_id, counts in task_counts.items()
        }
        normalized_weights = {
            require_text(task_id, "task weight id"): float(weight)
            for task_id, weight in task_weights.items()
        }
        coordinates = (
            run_id,
            batch_id,
            state_id,
            cutoff,
            compatibility,
            source_scope_id,
            normalized_counts,
            normalized_weights,
            weighting_contract,
        )
        return cls(
            snapshot_id=stable_id("evaluation-prior", *coordinates),
            run_id=run_id,
            batch_id=batch_id,
            state_id=state_id,
            cutoff=cutoff,
            compatibility=compatibility,
            source_scope_id=source_scope_id,
            task_counts=normalized_counts,
            task_weights=normalized_weights,
            weighting_contract=weighting_contract,
        )

    def __post_init__(self) -> None:
        for name in (
            "snapshot_id",
            "run_id",
            "batch_id",
            "state_id",
            "weighting_contract",
            "source_scope_id",
        ):
            require_text(getattr(self, name), name)
        if not isinstance(self.cutoff, EvidenceCutoff):
            raise TypeError("cutoff must be an EvidenceCutoff")
        if not isinstance(self.compatibility, CompatibilityKey):
            raise TypeError("compatibility must be a CompatibilityKey")
        if not isinstance(self.task_counts, Mapping):
            raise TypeError("task_counts must be a mapping")
        counts: dict[str, tuple[int, int]] = {}
        for raw_task_id, raw_counts in self.task_counts.items():
            task_id = require_text(raw_task_id, "task count id")
            if task_id in counts:
                raise ValueError("task_counts contains duplicate normalized ids")
            if (
                not isinstance(raw_counts, (tuple, list))
                or len(raw_counts) != 2
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in raw_counts
                )
            ):
                raise ValueError("task counts must be two non-negative integer counts")
            counts[task_id] = (raw_counts[0], raw_counts[1])
        if not isinstance(self.task_weights, Mapping):
            raise TypeError("task_weights must be a mapping")
        weights: dict[str, float] = {}
        for raw_task_id, raw_weight in self.task_weights.items():
            task_id = require_text(raw_task_id, "task weight id")
            if task_id in weights:
                raise ValueError("task_weights contains duplicate normalized ids")
            weight = float(raw_weight)
            if not math.isfinite(weight) or weight <= 0.0:
                raise ValueError("task weights must be finite and positive")
            weights[task_id] = weight
        object.__setattr__(self, "task_counts", deep_freeze(counts))
        object.__setattr__(self, "task_weights", deep_freeze(weights))
        expected = stable_id(
            "evaluation-prior",
            self.run_id,
            self.batch_id,
            self.state_id,
            self.cutoff,
            self.compatibility,
            self.source_scope_id,
            counts,
            weights,
            self.weighting_contract,
        )
        if self.snapshot_id != expected:
            raise ValueError("snapshot_id is not the stable prior content identity")


@dataclass(frozen=True)
class FrozenActionEvaluationPlan:
    """One action's exact adaptive-B draw, schedule and estimator contract."""

    record_id: str
    run_id: str
    batch_id: str
    state_id: str
    action_id: str
    cutoff: EvidenceCutoff
    plan: FrozenPairedPlan
    sampling_draw: SamplingDraw | None
    prior_snapshot_id: str
    estimator_contract: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        batch_id: str,
        state_id: str,
        action_id: str,
        cutoff: EvidenceCutoff,
        plan: FrozenPairedPlan,
        sampling_draw: SamplingDraw | None,
        prior_snapshot_id: str,
        estimator_contract: str,
    ) -> FrozenActionEvaluationPlan:
        if estimator_contract not in {
            HT_ESTIMATOR_CONTRACT,
            UNIFORM_ESTIMATOR_CONTRACT,
        }:
            raise ValueError(
                "new evaluation plans require a current estimator contract; legacy contracts are audit-only"
            )
        coordinates = (
            run_id,
            batch_id,
            state_id,
            action_id,
            cutoff,
            plan,
            sampling_draw,
            prior_snapshot_id,
            estimator_contract,
        )
        return cls(
            record_id=stable_id("action-evaluation-plan", *coordinates),
            run_id=run_id,
            batch_id=batch_id,
            state_id=state_id,
            action_id=action_id,
            cutoff=cutoff,
            plan=plan,
            sampling_draw=sampling_draw,
            prior_snapshot_id=prior_snapshot_id,
            estimator_contract=estimator_contract,
        )

    def __post_init__(self) -> None:
        for name in (
            "record_id",
            "run_id",
            "batch_id",
            "state_id",
            "action_id",
            "prior_snapshot_id",
            "estimator_contract",
        ):
            require_text(getattr(self, name), name)
        if not isinstance(self.cutoff, EvidenceCutoff):
            raise TypeError("cutoff must be an EvidenceCutoff")
        if not isinstance(self.plan, FrozenPairedPlan):
            raise TypeError("plan must be a FrozenPairedPlan")
        if self.plan.tier is not EvidenceTier.ADAPTIVE_B:
            raise ValueError("action evaluation plans must use adaptive B")
        if self.sampling_draw is None:
            # The constructor is also the persistence decoder. Historical
            # records stay readable, but creation and execution require v2.
            if self.estimator_contract not in {
                UNIFORM_ESTIMATOR_CONTRACT,
                LEGACY_UNIFORM_ESTIMATOR_CONTRACT,
            }:
                raise ValueError(
                    "a census plan must use the uniform estimator contract"
                )
            if self.plan.target_population_size != len(
                {cell.block_id for cell in self.plan.cells}
            ):
                raise ValueError(
                    "the uniform estimator contract requires a complete census"
                )
        else:
            if not isinstance(self.sampling_draw, SamplingDraw):
                raise TypeError("sampling_draw must be a SamplingDraw or None")
            if self.estimator_contract not in {
                HT_ESTIMATOR_CONTRACT,
                LEGACY_HT_ESTIMATOR_CONTRACT,
            }:
                raise ValueError(
                    "a sampled plan must use the Horvitz-Thompson contract"
                )
            plan_blocks = {cell.block_id for cell in self.plan.cells}
            if plan_blocks != set(self.sampling_draw.selected_ids):
                raise ValueError(
                    "paired plan blocks differ from the frozen sampling draw"
                )
            if self.plan.target_population_size != len(
                self.sampling_draw.marginal_inclusion
            ):
                raise ValueError(
                    "paired plan population differs from the sampling draw"
                )
        expected = stable_id(
            "action-evaluation-plan",
            self.run_id,
            self.batch_id,
            self.state_id,
            self.action_id,
            self.cutoff,
            self.plan,
            self.sampling_draw,
            self.prior_snapshot_id,
            self.estimator_contract,
        )
        if self.record_id != expected:
            raise ValueError("record_id is not the stable plan content identity")


@dataclass(frozen=True)
class FrozenEvaluationBatch:
    """Complete output of one pre-dispatch evaluation-plan freeze."""

    prior_snapshot: FrozenInformationPriorSnapshot
    action_plans: tuple[FrozenActionEvaluationPlan, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.prior_snapshot, FrozenInformationPriorSnapshot):
            raise TypeError("prior_snapshot has the wrong type")
        plans = tuple(self.action_plans)
        object.__setattr__(self, "action_plans", plans)
        if not plans:
            raise ValueError("frozen evaluation batch needs action plans")
        if any(not isinstance(item, FrozenActionEvaluationPlan) for item in plans):
            raise TypeError("action_plans contain the wrong type")
        action_ids = tuple(item.action_id for item in plans)
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("frozen evaluation batch has duplicate actions")
        prior = self.prior_snapshot
        expected_coordinates = (
            prior.run_id,
            prior.batch_id,
            prior.state_id,
            prior.cutoff,
            prior.snapshot_id,
        )
        for item in plans:
            actual_coordinates = (
                item.run_id,
                item.batch_id,
                item.state_id,
                item.cutoff,
                item.prior_snapshot_id,
            )
            if actual_coordinates != expected_coordinates:
                raise ValueError(
                    "action evaluation plan differs from its prior snapshot"
                )


__all__ = [
    "HT_ESTIMATOR_CONTRACT",
    "UNIFORM_ESTIMATOR_CONTRACT",
    "FrozenActionEvaluationPlan",
    "FrozenEvaluationBatch",
    "FrozenInformationPriorSnapshot",
]
