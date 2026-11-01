"""Execution-grounded effectiveness classification for policy learning."""

from __future__ import annotations

import math
from dataclasses import dataclass

from agentpilot.domain import (
    EffectivenessLabel,
    EffectivenessObservation,
    EvidenceTier,
    EvaluationValidity,
    ImplementationAdherence,
    OutcomeAttribution,
    PairedOutcome,
    WorkerExecutionStatus,
    WorkerResult,
    stable_id,
)


@dataclass(frozen=True)
class EffectivenessRule:
    """Frozen Bernoulli target used by UCB and memory summaries."""

    margin: float
    statistic_identity: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.margin):
            raise ValueError("effectiveness margin must be finite")
        if self.statistic_identity not in {"point", "lower_bound"}:
            raise ValueError("statistic_identity must be point or lower_bound")


def higher_is_better_delta(
    *, child_value: float, parent_value: float, metric_direction: str
) -> float:
    """Normalize a raw paired metric difference to a higher-is-better sign."""

    if not math.isfinite(child_value) or not math.isfinite(parent_value):
        raise ValueError("paired metric values must be finite")
    if metric_direction == "maximize":
        return child_value - parent_value
    if metric_direction == "minimize":
        return parent_value - child_value
    raise ValueError("metric_direction must be maximize or minimize")


def _skip(
    action_id: str,
    outcome: PairedOutcome,
    rule: EffectivenessRule,
    reason: str,
) -> EffectivenessObservation:
    return EffectivenessObservation(
        observation_id=stable_id("effectiveness", action_id),
        action_id=action_id,
        label=EffectivenessLabel.SKIP,
        available_at=outcome.completed_at,
        reason=reason,
        signed_statistic=None,
        margin=rule.margin,
    )


def classify_effectiveness(
    worker: WorkerResult,
    outcome: PairedOutcome,
    rule: EffectivenessRule,
) -> EffectivenessObservation:
    """Classify one action using the frozen precedence from design doc 27.

    Measurement/apparatus validity is checked before implementation adherence,
    then outcome attribution, then constraints and the signed statistic.  A
    skip is not a zero reward and must never increment a Bernoulli observation
    count.
    """

    if worker.action_id != outcome.action_id:
        raise ValueError("worker and outcome reference different actions")
    if outcome.evidence_tier is EvidenceTier.SEALED_C:
        raise ValueError("sealed C evidence cannot become a policy reward")
    action_id = worker.action_id
    statistic = outcome.signed_effectiveness_statistic
    if outcome.evidence_tier is EvidenceTier.VISIBLE_A:
        return _skip(action_id, outcome, rule, "evidence:visible_a_not_effectiveness")

    if worker.status is not WorkerExecutionStatus.COMPLETED:
        failure_type = worker.diagnostics.get("failure_type")
        safe_failure_type = (
            failure_type
            if isinstance(failure_type, str)
            and 0 < len(failure_type) <= 128
            and all(
                character.isalnum() or character in "._"
                for character in failure_type
            )
            else None
        )
        reason = f"worker:{worker.status.value}"
        if safe_failure_type is not None:
            reason += f":{safe_failure_type}"
        return _skip(action_id, outcome, rule, reason)
    if (
        outcome.evidence_tier is EvidenceTier.REALIZATION
        and (
            (
                worker.adherence is ImplementationAdherence.DEVIATED
                and outcome.admission_fate == "adherence_rejected"
            )
            or (
                worker.adherence is ImplementationAdherence.UNKNOWN
                and outcome.admission_fate == "adherence_unknown"
            )
        )
    ):
        return _skip(
            action_id,
            outcome,
            rule,
            f"adherence:{worker.adherence.value}",
        )
    if outcome.validity is not EvaluationValidity.VALID:
        return _skip(action_id, outcome, rule, f"evaluation:{outcome.validity.value}")
    if worker.adherence is not ImplementationAdherence.FOLLOWED:
        return _skip(action_id, outcome, rule, f"adherence:{worker.adherence.value}")
    if outcome.attribution is not OutcomeAttribution.ATTRIBUTABLE:
        return _skip(
            action_id, outcome, rule, f"attribution:{outcome.attribution.value}"
        )
    if statistic is None or not math.isfinite(float(statistic)):
        return _skip(action_id, outcome, rule, "measurement:missing_or_non_finite")
    if outcome.constraints_pass is None:
        return _skip(action_id, outcome, rule, "measurement:constraint_unknown")

    label = (
        EffectivenessLabel.SUCCESS
        if outcome.constraints_pass and float(statistic) > rule.margin
        else EffectivenessLabel.FAILURE
    )
    reason = (
        "constraints_pass_and_statistic_above_margin"
        if label is EffectivenessLabel.SUCCESS
        else (
            "constraint_failure"
            if not outcome.constraints_pass
            else "statistic_not_above_margin"
        )
    )
    return EffectivenessObservation(
        observation_id=stable_id("effectiveness", action_id),
        action_id=action_id,
        label=label,
        available_at=outcome.completed_at,
        reason=reason,
        signed_statistic=float(statistic),
        margin=rule.margin,
    )


__all__ = [
    "EffectivenessRule",
    "classify_effectiveness",
    "higher_is_better_delta",
]
