"""Information-weighted sampling and design-based evaluation estimators."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import ClassVar, Mapping, Optional, Sequence

from agentpilot.evaluation.paired import (
    FrozenPairedPlan,
    PairedEstimate,
    validated_block_delta_variances,
)
from agentpilot.evaluation.sampling import SamplingDraw

ABSOLUTE_HT_ESTIMATOR_IDENTITY = "horvitz-thompson-absolute"
PAIRED_HT_ESTIMATOR_IDENTITY = "horvitz-thompson-paired-delta-replicates-v2"
DESIGN_UNCERTAINTY_IDENTITY = "ht-exact-joint-replicate-correction-v2"


@dataclass(frozen=True)
class TaskPassPrior:
    """Beta posterior over a unit's control-side pass probability."""

    alpha: float = 1.0
    beta: float = 1.0
    n_obs: int = 0

    def __post_init__(self) -> None:
        if not math.isfinite(self.alpha) or self.alpha <= 0.0:
            raise ValueError("alpha must be finite and positive")
        if not math.isfinite(self.beta) or self.beta <= 0.0:
            raise ValueError("beta must be finite and positive")
        if isinstance(self.n_obs, bool) or not isinstance(self.n_obs, int):
            raise ValueError("n_obs must be a nonnegative integer")
        if self.n_obs < 0:
            raise ValueError("n_obs must be a nonnegative integer")

    @property
    def expected_discordance(self) -> float:
        """Return ``E[2 p (1-p)]`` under the Beta posterior."""

        total = self.alpha + self.beta
        return 2.0 * self.alpha * self.beta / (total * (total + 1.0))


UNINFORMED_DISCORDANCE = TaskPassPrior().expected_discordance


@dataclass(frozen=True)
class InformationWeighting:
    """Mix expected discordance with a positive uninformed floor."""

    enabled: bool = False
    floor: float = 0.2

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        if not math.isfinite(self.floor) or not 0.0 <= self.floor <= 1.0:
            raise ValueError("floor must be finite and between zero and one")

    def weight(self, prior: TaskPassPrior) -> float:
        return (
            1.0 - self.floor
        ) * prior.expected_discordance + self.floor * UNINFORMED_DISCORDANCE


def information_weights(
    unit_ids: Sequence[str],
    *,
    priors: Mapping[str, TaskPassPrior],
    config: InformationWeighting,
) -> dict[str, float]:
    """Build a complete positive weight vector for a finite population."""

    if len(set(unit_ids)) != len(unit_ids):
        raise ValueError("unit_ids must be unique")
    if any(not unit_id for unit_id in unit_ids):
        raise ValueError("unit_ids must be non-empty")
    if not config.enabled:
        return {unit_id: 1.0 for unit_id in unit_ids}
    default = TaskPassPrior()
    return {
        unit_id: config.weight(priors.get(unit_id, default)) for unit_id in unit_ids
    }


def horvitz_thompson_absolute_mean(
    observed_values: Mapping[str, float],
    propensity: Mapping[str, float],
    *,
    population_size: int,
) -> Optional[float]:
    """Estimate a finite-population, unit-uniform mean without ratio bias."""

    if not observed_values:
        return None
    if isinstance(population_size, bool) or not isinstance(population_size, int):
        raise ValueError("population_size must be a positive integer")
    if population_size <= 0:
        raise ValueError("population_size must be a positive integer")
    if len(observed_values) > population_size:
        raise ValueError("sample contains more units than the target population")
    total = 0.0
    for unit_id, raw_value in observed_values.items():
        inclusion_probability = float(propensity.get(unit_id, 0.0))
        if (
            not math.isfinite(inclusion_probability)
            or not 0.0 < inclusion_probability <= 1.0
        ):
            raise ValueError(
                f"unit {unit_id!r} was sampled with invalid propensity "
                f"{inclusion_probability!r}"
            )
        value = float(raw_value)
        if not math.isfinite(value):
            raise ValueError(f"unit {unit_id!r} has non-finite value {raw_value!r}")
        total += value / inclusion_probability
    estimate = total / population_size
    if not math.isfinite(estimate):
        raise ValueError("Horvitz-Thompson estimate is non-finite")
    return estimate


@dataclass(frozen=True)
class PairedHTEstimate:
    """Absolute parent/child HT means and their paired difference."""

    parent_mean: float
    child_mean: float
    delta_mean: float
    estimator_identity: str = PAIRED_HT_ESTIMATOR_IDENTITY


def horvitz_thompson_paired(
    parent_values: Mapping[str, float],
    child_values: Mapping[str, float],
    propensity: Mapping[str, float],
    *,
    population_size: int,
) -> Optional[PairedHTEstimate]:
    """Estimate paired parent/child means on one identical sampled unit set."""

    parent_ids = set(parent_values)
    child_ids = set(child_values)
    if parent_ids != child_ids:
        raise ValueError("paired HT requires identical parent and child unit ids")
    if not parent_ids:
        return None
    parent_mean = horvitz_thompson_absolute_mean(
        parent_values, propensity, population_size=population_size
    )
    child_mean = horvitz_thompson_absolute_mean(
        child_values, propensity, population_size=population_size
    )
    delta_values = {
        unit_id: float(child_values[unit_id]) - float(parent_values[unit_id])
        for unit_id in parent_values
    }
    delta_mean = horvitz_thompson_absolute_mean(
        delta_values, propensity, population_size=population_size
    )
    assert parent_mean is not None and child_mean is not None and delta_mean is not None
    return PairedHTEstimate(
        parent_mean=parent_mean,
        child_mean=child_mean,
        delta_mean=delta_mean,
    )


@dataclass(frozen=True)
class DesignUncertainty:
    """Exact-joint HT variance realization and conservative decision floors.

    The HT terms price only *which* units were drawn, so they vanish when a
    wave censuses its population: every inclusion probability reaches one and
    every ``1 - p`` factor becomes zero.  A census of noisy units is not a
    certain measurement, though, and an agentic benchmark re-measuring one unit
    routinely disagrees with itself, so ``dispersion_variance_floor`` prices the
    observed spread across units as well.  Without it a full-wave design reports
    a zero standard error, which silently turns a lower-bound decision rule into
    a bare sign test on the point estimate.
    """

    standard_error: float | None
    variance: float | None
    raw_ht_variance: float
    poisson_variance_floor: float
    dispersion_variance_floor: float
    effective_sample_size: float
    within_variance_correction: float | None = None
    corrected_ht_variance: float | None = None
    within_variance_known_blocks: int = 0
    within_variance_unknown_blocks: int = 0
    uncertainty_status: str = "between_proxy_with_incomplete_replicates"
    estimator_identity: str = DESIGN_UNCERTAINTY_IDENTITY


def horvitz_thompson_design_uncertainty(
    observed_values: Mapping[str, float],
    propensity: Mapping[str, float],
    joint_propensity: Mapping[tuple[str, str], float],
    *,
    population_size: int,
    block_delta_variances: Mapping[str, float | None] | None = None,
) -> DesignUncertainty:
    """Estimate design variance using exact first- and second-order inclusion."""

    if isinstance(population_size, bool) or not isinstance(population_size, int):
        raise ValueError("population_size must be a positive integer")
    if population_size <= 0:
        raise ValueError("population_size must be a positive integer")
    unit_ids = sorted(observed_values)
    if not unit_ids:
        raise ValueError("design uncertainty requires at least one unit")
    if len(unit_ids) > population_size:
        raise ValueError("sample contains more units than the target population")

    validated_propensity: dict[str, float] = {}
    validated_values: dict[str, float] = {}
    diagonal = 0.0
    raw_total_variance = 0.0
    for unit_id in unit_ids:
        inclusion_probability = float(propensity.get(unit_id, 0.0))
        value = float(observed_values[unit_id])
        if (
            not math.isfinite(inclusion_probability)
            or not 0.0 < inclusion_probability <= 1.0
        ):
            raise ValueError(
                f"unit {unit_id!r} has invalid propensity {inclusion_probability!r}"
            )
        if not math.isfinite(value):
            raise ValueError(f"unit {unit_id!r} has non-finite value {value!r}")
        validated_propensity[unit_id] = inclusion_probability
        validated_values[unit_id] = value
        term = (
            (1.0 - inclusion_probability)
            * value
            * value
            / (inclusion_probability * inclusion_probability)
        )
        diagonal += term
        raw_total_variance += term

    for index, first in enumerate(unit_ids):
        for second in unit_ids[index + 1 :]:
            pair = (first, second)
            joint = float(joint_propensity.get(pair, 0.0))
            if not math.isfinite(joint) or joint <= 0.0:
                raise ValueError(
                    f"sampled pair {pair!r} has no positive exact joint "
                    "inclusion probability"
                )
            first_probability = validated_propensity[first]
            second_probability = validated_propensity[second]
            raw_total_variance += 2.0 * (
                (joint - first_probability * second_probability)
                * validated_values[first]
                * validated_values[second]
                / (joint * first_probability * second_probability)
            )

    divisor = float(population_size * population_size)
    raw_variance = raw_total_variance / divisor
    poisson_floor = diagonal / divisor
    dispersion_floor = (
        statistics.variance(validated_values.values()) / len(unit_ids)
        if len(unit_ids) > 1
        else 0.0
    )
    within = validated_block_delta_variances(observed_values, block_delta_variances)
    known = {unit: value for unit, value in within.items() if value is not None}
    # The raw HT estimator applied to noisy task means already includes part
    # of their measurement variance. Two-stage HT adds sum(v_i / pi_i) / N^2,
    # not the whole conditional variance sum(v_i / pi_i^2) / N^2 again.
    correction = (
        sum(value / validated_propensity[unit] for unit, value in known.items())
        / divisor
        if known
        else None
    )
    corrected_ht_variance = raw_variance + (correction or 0.0)
    candidate_variance = max(
        0.0, corrected_ht_variance, poisson_floor, dispersion_floor
    )
    # One sampled task cannot estimate variation among unobserved tasks. A
    # one-task census can estimate its repeated-run uncertainty when repeated.
    uncertainty_available = len(unit_ids) > 1 or (
        population_size == 1 and len(known) == 1
    )
    variance = candidate_variance if uncertainty_available else None
    if not all(
        math.isfinite(value)
        for value in (raw_variance, poisson_floor, dispersion_floor, candidate_variance)
    ):
        raise ValueError("HT design variance is non-finite")
    inverse_weights = [1.0 / validated_propensity[unit_id] for unit_id in unit_ids]
    weight_total = sum(inverse_weights)
    effective_sample_size = (
        weight_total * weight_total / sum(weight * weight for weight in inverse_weights)
    )
    return DesignUncertainty(
        standard_error=math.sqrt(variance) if variance is not None else None,
        variance=variance,
        raw_ht_variance=raw_variance,
        poisson_variance_floor=poisson_floor,
        dispersion_variance_floor=dispersion_floor,
        effective_sample_size=effective_sample_size,
        within_variance_correction=correction,
        corrected_ht_variance=corrected_ht_variance,
        within_variance_known_blocks=len(known),
        within_variance_unknown_blocks=len(unit_ids) - len(known),
        uncertainty_status="unavailable"
        if variance is None
        else "two_stage_with_conservative_floors"
        if len(known) == len(unit_ids)
        else "between_proxy_with_incomplete_replicates",
    )


@dataclass(frozen=True)
class HorvitzThompsonPairedEstimator:
    """Adapt a frozen :class:`SamplingDraw` to the paired-engine protocol.

    The benchmark adapter freezes the draw before executing either program.
    This estimator then rejects any plan or result whose block population no
    longer matches that draw, instead of silently estimating a different
    complete-case population.
    """

    draw: SamplingDraw
    supports_replicate_variance: ClassVar[bool] = True

    def estimate(
        self,
        *,
        block_deltas: Mapping[str, float],
        challenger_by_block: Mapping[str, float],
        plan: FrozenPairedPlan,
        block_delta_variances: Mapping[str, float | None] | None = None,
    ) -> PairedEstimate:
        plan_blocks = {cell.block_id for cell in plan.cells}
        selected_ids = set(self.draw.selected_ids)
        delta_ids = set(block_deltas)
        challenger_ids = set(challenger_by_block)
        if len(selected_ids) != len(self.draw.selected):
            raise ValueError("sampling draw contains duplicate selected unit ids")
        if plan_blocks != selected_ids:
            raise ValueError("paired plan blocks do not match the frozen sampling draw")
        if delta_ids != plan_blocks or challenger_ids != plan_blocks:
            raise ValueError(
                "paired estimator inputs do not match the frozen plan blocks"
            )

        population_size = len(self.draw.marginal_inclusion)
        if plan.target_population_size != population_size:
            raise ValueError(
                "paired plan target population size does not match the sampling draw"
            )
        if not selected_ids <= set(self.draw.marginal_inclusion):
            raise ValueError("sampling draw lacks marginal inclusion probabilities")

        expected_pairs = {
            (first, second)
            for index, first in enumerate(sorted(selected_ids))
            for second in sorted(selected_ids)[index + 1 :]
        }
        if not expected_pairs <= set(self.draw.selected_joint_inclusion):
            raise ValueError("sampling draw lacks exact joint inclusion probabilities")

        delta_mean = horvitz_thompson_absolute_mean(
            block_deltas,
            self.draw.marginal_inclusion,
            population_size=population_size,
        )
        challenger_mean = horvitz_thompson_absolute_mean(
            challenger_by_block,
            self.draw.marginal_inclusion,
            population_size=population_size,
        )
        uncertainty = horvitz_thompson_design_uncertainty(
            block_deltas,
            self.draw.marginal_inclusion,
            self.draw.selected_joint_inclusion,
            population_size=population_size,
            block_delta_variances=block_delta_variances,
        )
        assert delta_mean is not None and challenger_mean is not None
        return PairedEstimate(
            primary_delta_mean=delta_mean,
            primary_delta_standard_error=uncertainty.standard_error,
            effective_sample_size=uncertainty.effective_sample_size,
            challenger_absolute_value=challenger_mean,
            estimator_identity={
                "name": PAIRED_HT_ESTIMATOR_IDENTITY,
                "absolute": ABSOLUTE_HT_ESTIMATOR_IDENTITY,
                "uncertainty": DESIGN_UNCERTAINTY_IDENTITY,
                "raw_ht_variance": uncertainty.raw_ht_variance,
                "corrected_ht_variance": uncertainty.corrected_ht_variance,
                "within_variance_correction": uncertainty.within_variance_correction,
                "within_variance_known_blocks": uncertainty.within_variance_known_blocks,
                "within_variance_unknown_blocks": uncertainty.within_variance_unknown_blocks,
                "uncertainty_status": uncertainty.uncertainty_status,
                "replicate_assumption": "independent_replicate_pairs",
                "sampling": self.draw.sampling_method,
                "marginal": self.draw.marginal_method,
                "joint": self.draw.joint_method,
                "selection_seed": self.draw.selection_seed,
                "design": dict(plan.design_identity),
            },
        )


__all__ = [
    "ABSOLUTE_HT_ESTIMATOR_IDENTITY",
    "DESIGN_UNCERTAINTY_IDENTITY",
    "PAIRED_HT_ESTIMATOR_IDENTITY",
    "UNINFORMED_DISCORDANCE",
    "DesignUncertainty",
    "HorvitzThompsonPairedEstimator",
    "InformationWeighting",
    "PairedHTEstimate",
    "TaskPassPrior",
    "horvitz_thompson_absolute_mean",
    "horvitz_thompson_design_uncertainty",
    "horvitz_thompson_paired",
    "information_weights",
]
