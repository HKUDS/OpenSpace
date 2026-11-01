"""Benchmark-neutral archive admission from paired evaluation evidence.

The benchmark adapter measures cells.  This module owns the stable decision
rule applied to those measurements, so a benchmark cannot silently redefine
what ``admitted`` means.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class AdmissionFate(str, Enum):
    ADMITTED = "admitted"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class AdmissionRule:
    """Frozen conservative rule for a higher-is-better paired estimate.

    ``estimate`` and ``standard_error`` may come from an ordinary census or a
    design-based estimator.  The rule deliberately does not know which
    benchmark produced them.

    ``margin`` is how much regression to tolerate, so raising it loosens this
    gate and a non-negative value can never demand an improvement: the
    strictest admission expressible here is "did not regress", which a neutral
    child satisfies.  Requiring a real gain is the separate job of
    ``EffectivenessRule``, whose own ``margin`` is a floor the statistic must
    strictly exceed.  The two fields share a name and read in opposite
    directions, so a value moved from one to the other inverts its meaning.
    """

    margin: float = 0.0
    uncertainty_multiplier: float = 1.0
    prior_strength: float = 2.0
    min_blocks: int = 2

    def __post_init__(self) -> None:
        for name in ("margin", "uncertainty_multiplier", "prior_strength"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
        if isinstance(self.min_blocks, bool) or self.min_blocks < 1:
            raise ValueError("min_blocks must be positive")


@dataclass(frozen=True)
class PairedEvidence:
    """Complete decision input produced by a paired estimator."""

    estimate: float | None
    standard_error: float | None
    effective_sample_size: float | None
    measured_blocks: int
    expected_blocks: int
    constraints_pass: bool | None

    def __post_init__(self) -> None:
        if isinstance(self.measured_blocks, bool) or self.measured_blocks < 0:
            raise ValueError("measured_blocks must be non-negative")
        if isinstance(self.expected_blocks, bool) or self.expected_blocks < 1:
            raise ValueError("expected_blocks must be positive")
        if self.measured_blocks > self.expected_blocks:
            raise ValueError("measured_blocks cannot exceed expected_blocks")
        for name in ("estimate", "standard_error", "effective_sample_size"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite when present")
        if self.standard_error is not None and self.standard_error < 0.0:
            raise ValueError("standard_error must be non-negative")
        if (
            self.effective_sample_size is not None
            and self.effective_sample_size <= 0.0
        ):
            raise ValueError("effective_sample_size must be positive")

    @property
    def coverage_complete(self) -> bool:
        return self.measured_blocks == self.expected_blocks


@dataclass(frozen=True)
class AdmissionVerdict:
    fate: AdmissionFate
    conservative_lower: float | None
    reason: str


def conservative_lower_bound(
    evidence: PairedEvidence, rule: AdmissionRule
) -> float:
    """Return the frozen shrinkage-and-uncertainty decision statistic."""

    if (
        evidence.estimate is None
        or evidence.standard_error is None
        or evidence.effective_sample_size is None
    ):
        raise ValueError("complete numeric evidence is required")
    effective_n = evidence.effective_sample_size
    shrunk = evidence.estimate * effective_n / (
        effective_n + rule.prior_strength
    )
    return shrunk - rule.uncertainty_multiplier * evidence.standard_error


def decide_admission(
    evidence: PairedEvidence, rule: AdmissionRule
) -> AdmissionVerdict:
    """Apply coverage checks, compute the statistic, then apply constraint veto."""

    if evidence.constraints_pass is None:
        return AdmissionVerdict(
            AdmissionFate.INCONCLUSIVE, None, "constraint_status_missing"
        )
    if evidence.constraints_pass is False:
        lower = None
        if (
            evidence.coverage_complete
            and evidence.measured_blocks >= rule.min_blocks
            and evidence.estimate is not None
            and evidence.standard_error is not None
            and evidence.effective_sample_size is not None
        ):
            lower = conservative_lower_bound(evidence, rule)
        return AdmissionVerdict(
            AdmissionFate.REJECTED, lower, "hard_constraints_failed"
        )
    if not evidence.coverage_complete:
        return AdmissionVerdict(
            AdmissionFate.INCONCLUSIVE, None, "paired_coverage_incomplete"
        )
    if evidence.measured_blocks < rule.min_blocks:
        return AdmissionVerdict(
            AdmissionFate.INCONCLUSIVE, None, "insufficient_paired_blocks"
        )
    if (
        evidence.estimate is None
        or evidence.standard_error is None
        or evidence.effective_sample_size is None
    ):
        return AdmissionVerdict(
            AdmissionFate.INCONCLUSIVE, None, "paired_estimate_missing"
        )
    lower = conservative_lower_bound(evidence, rule)
    if lower >= -rule.margin:
        return AdmissionVerdict(
            AdmissionFate.ADMITTED, lower, "lower_bound_meets_margin"
        )
    return AdmissionVerdict(
        AdmissionFate.REJECTED, lower, "lower_bound_below_margin"
    )


__all__ = [
    "AdmissionFate",
    "AdmissionRule",
    "AdmissionVerdict",
    "PairedEvidence",
    "conservative_lower_bound",
    "decide_admission",
]
