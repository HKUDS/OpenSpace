"""Bounded typed projection of one completed barrier into summary facts.

``PolicyObservation`` alone cannot describe *why* an attempt worked: it names an
opaque direction id and a label.  The barrier already carries the selected
``StructuredDirection`` and its execution facts, so the summary input joins
them here instead of asking the summarizer to learn from identities.

Worker-controlled prose never enters this projection. Direction scope remains
a proposed boundary, tree-observed file changes remain realization facts, and
paired-B remains a separate effectiveness observation. Missing tree-diff data
is recorded explicitly instead of being inferred from touched paths.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from agentpilot.domain import (
    EffectivenessLabel,
    EvaluationValidity,
    ImplementationAdherence,
    NewPath,
    OutcomeAttribution,
    PairedOutcome,
    PolicyDecisionPlan,
    PolicyMethod,
    PolicyObservation,
    ScopeTarget,
    WorkerResult,
)
from agentpilot.domain.common import deep_freeze, require_aware, require_text
from agentpilot.domain.realization import RealizedFileChange

MEMORY_FACT_MAX_TEXT_CHARS = 1_000
MEMORY_FACT_MAX_SCOPE_TARGETS = 8
MEMORY_FACT_MAX_CHANGES = 64
MEMORY_FACT_MAX_FILES = 64


class BarrierExecutionResult(Protocol):
    """Structural view of the application execution pair."""

    worker: WorkerResult
    outcome: PairedOutcome


def _bounded(text: str) -> str:
    if len(text) <= MEMORY_FACT_MAX_TEXT_CHARS:
        return text
    return text[: MEMORY_FACT_MAX_TEXT_CHARS - 1] + "…"


def _finite_or_none(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite when present")
    return float(value)


@dataclass(frozen=True)
class MemoryBarrierFact:
    """Hypothesis, realized child, and effectiveness kept as separate facts."""

    action_id: str
    parent_context: Mapping[str, object]
    observed_behavior: str
    failure_mechanism: str
    implementation_direction: str
    declared_scope: tuple[ScopeTarget, ...]
    new_paths: tuple[NewPath, ...]
    resolved_scope: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    implementation_adherence: ImplementationAdherence
    result_commit_sha: str | None
    realized_changes: tuple[RealizedFileChange, ...]
    unclassified_touched_files: tuple[str, ...]
    realization_classification_gap: str | None
    undeclared_file_count: int
    outcome_attribution: OutcomeAttribution
    evaluation_validity: EvaluationValidity
    effectiveness_label: EffectivenessLabel
    outcome_reason: str
    primary_delta_mean: float | None
    primary_delta_lower: float | None
    constraints_pass: bool | None
    available_at: datetime
    effectiveness_available_at: datetime
    #: Signed change in how often the child exhausted its resource budget rather
    #: than finishing.  A constraint veto reduces to one bit, so the magnitude is
    #: what separates "kept the answers but stalled everywhere" from "broke the
    #: answers".  ``None`` means no resource rule was applied.
    resource_regression: float | None = None

    def __post_init__(self) -> None:
        for name in (
            "action_id",
            "observed_behavior",
            "failure_mechanism",
            "implementation_direction",
            "outcome_reason",
        ):
            require_text(getattr(self, name), name)
        if not isinstance(self.parent_context, Mapping):
            raise TypeError("parent_context must be a mapping")
        object.__setattr__(self, "parent_context", deep_freeze(self.parent_context))
        scope = tuple(self.declared_scope)
        object.__setattr__(self, "declared_scope", scope)
        if not 1 <= len(scope) <= MEMORY_FACT_MAX_SCOPE_TARGETS:
            raise ValueError("memory fact has an invalid declared-scope count")
        if any(not isinstance(target, ScopeTarget) for target in scope):
            raise TypeError("memory fact declared_scope must be typed")
        paths = tuple(self.new_paths)
        object.__setattr__(self, "new_paths", paths)
        if any(not isinstance(path, NewPath) for path in paths):
            raise TypeError("memory fact new_paths must be typed")
        resolved = tuple(self.resolved_scope)
        object.__setattr__(self, "resolved_scope", resolved)
        if not resolved or resolved != tuple(sorted(set(resolved))):
            raise ValueError("memory fact resolved_scope is not canonical")
        evidence = tuple(self.evidence_refs)
        object.__setattr__(self, "evidence_refs", evidence)
        if evidence != tuple(sorted(set(evidence))):
            raise ValueError("memory fact evidence_refs are not canonical")
        changes = tuple(self.realized_changes)
        object.__setattr__(self, "realized_changes", changes)
        if len(changes) > MEMORY_FACT_MAX_CHANGES:
            raise ValueError("memory fact exceeded its realized-change bound")
        if any(not isinstance(change, RealizedFileChange) for change in changes):
            raise TypeError("memory fact realized_changes must be typed")
        if self.result_commit_sha is not None:
            require_text(self.result_commit_sha, "result_commit_sha")
        unclassified = tuple(self.unclassified_touched_files)
        object.__setattr__(self, "unclassified_touched_files", unclassified)
        if (
            len(unclassified) > MEMORY_FACT_MAX_FILES
            or unclassified != tuple(sorted(set(unclassified)))
        ):
            raise ValueError("memory fact unclassified files are not bounded/canonical")
        if self.realization_classification_gap is None:
            if unclassified:
                raise ValueError("unclassified files require a classification gap")
        else:
            require_text(
                self.realization_classification_gap,
                "realization_classification_gap",
            )
            if not unclassified:
                raise ValueError("classification gap requires unclassified files")
        if isinstance(self.undeclared_file_count, bool) or (
            self.undeclared_file_count < 0
        ):
            raise ValueError("undeclared_file_count must be a non-negative integer")
        for name in (
            "primary_delta_mean",
            "primary_delta_lower",
            "resource_regression",
        ):
            object.__setattr__(self, name, _finite_or_none(getattr(self, name), name))
        require_aware(self.available_at, "available_at")
        require_aware(self.effectiveness_available_at, "effectiveness_available_at")


def build_memory_barrier_fact(
    *,
    plan: PolicyDecisionPlan,
    worker: WorkerResult,
    outcome: PairedOutcome,
    observation: PolicyObservation,
) -> MemoryBarrierFact:
    """Join one Textual action with its completed execution and feedback."""

    action = plan.action
    if action.method is not PolicyMethod.MEMORY:
        raise ValueError("memory barrier fact requires a Memory action")
    if (
        worker.action_id != action.action_id
        or outcome.action_id != action.action_id
        or observation.action_id != action.action_id
    ):
        raise ValueError("memory barrier fact inputs describe different actions")
    if observation.parent_id != action.parent_id:
        raise ValueError("memory barrier fact parent coordinates differ")
    if observation.method is not PolicyMethod.MEMORY:
        raise ValueError("memory barrier observation belongs to another policy")
    if worker.adherence is not observation.implementation_adherence:
        raise ValueError("memory barrier implementation adherence differs")
    if outcome.validity is not observation.evaluation_validity:
        raise ValueError("memory barrier evaluation validity differs")
    if outcome.attribution is not observation.outcome_attribution:
        raise ValueError("memory barrier outcome attribution differs")
    if outcome.evidence_tier is not observation.evidence_tier:
        raise ValueError("memory barrier evidence tier differs")
    if observation.effectiveness.available_at != outcome.completed_at:
        raise ValueError("memory barrier effectiveness timestamp differs")
    if observation.available_at != observation.effectiveness.available_at:
        raise ValueError("memory barrier observation timestamps differ")
    if (
        observation.effectiveness.label is not EffectivenessLabel.SKIP
        and observation.effectiveness.signed_statistic
        != outcome.signed_effectiveness_statistic
    ):
        raise ValueError("memory barrier effectiveness statistic differs")
    observed_direction_id = observation.metadata.get("direction_id")
    if observed_direction_id is not None and (
        observed_direction_id != action.direction.direction_id
    ):
        raise ValueError("memory barrier observation names another direction")
    if (
        "admission_fate" in observation.metadata
        and observation.metadata["admission_fate"] != outcome.admission_fate
    ):
        raise ValueError("memory barrier admission outcome differs")
    direction = action.direction
    selected_parents = tuple(
        candidate
        for candidate in plan.parent_slate.candidates
        if candidate.parent_id == action.parent_id
    )
    if len(selected_parents) != 1:
        raise ValueError("memory barrier cannot resolve the selected parent")
    selected_parent = selected_parents[0]
    touched = set(worker.touched_files)
    declared_files = set(direction.resolved_scope)
    changes = tuple(worker.file_changes)
    classified_endpoints = {
        endpoint
        for change in changes
        for endpoint in (change.file, change.from_file)
        if endpoint is not None
    }
    unclassified = tuple(sorted(touched - classified_endpoints))
    return MemoryBarrierFact(
        action_id=action.action_id,
        parent_context={
            "descriptor": selected_parent.descriptor.blocks,
            "primary_value": selected_parent.primary_value,
        },
        observed_behavior=_bounded(direction.observed_behavior),
        failure_mechanism=_bounded(direction.failure_mechanism),
        implementation_direction=_bounded(direction.implementation_direction),
        declared_scope=direction.declared_scope,
        new_paths=direction.new_paths,
        resolved_scope=direction.resolved_scope,
        evidence_refs=direction.evidence_refs,
        implementation_adherence=observation.implementation_adherence,
        result_commit_sha=worker.result_commit_sha,
        realized_changes=changes,
        unclassified_touched_files=unclassified,
        realization_classification_gap=(
            "worker_result_missing_tree_diff" if unclassified else None
        ),
        undeclared_file_count=len(touched - declared_files),
        outcome_attribution=observation.outcome_attribution,
        evaluation_validity=observation.evaluation_validity,
        effectiveness_label=observation.effectiveness.label,
        outcome_reason=_bounded(observation.effectiveness.reason),
        primary_delta_mean=outcome.primary_delta_mean,
        primary_delta_lower=outcome.primary_delta_lower,
        resource_regression=outcome.resource_regression,
        constraints_pass=outcome.constraints_pass,
        available_at=observation.available_at,
        effectiveness_available_at=observation.effectiveness.available_at,
    )


def build_memory_barrier_facts(
    *,
    plans: tuple[PolicyDecisionPlan, ...],
    execution_results: tuple[BarrierExecutionResult, ...],
    observations: tuple[PolicyObservation, ...],
) -> tuple[MemoryBarrierFact, ...]:
    """Project a complete barrier without silently dropping any action."""

    if not plans:
        raise ValueError("Memory updater needs at least one decision plan")
    if len(plans) != len(execution_results) or len(plans) != len(observations):
        raise ValueError("Memory barrier inputs are not one-to-one")
    action_ids = tuple(plan.action.action_id for plan in plans)
    if len(action_ids) != len(set(action_ids)):
        raise ValueError("Memory barrier contains duplicate actions")
    observation_ids = tuple(item.observation_id for item in observations)
    if len(observation_ids) != len(set(observation_ids)):
        raise ValueError("Memory barrier contains duplicate observations")
    return tuple(
        build_memory_barrier_fact(
            plan=plan,
            worker=execution.worker,
            outcome=execution.outcome,
            observation=observation,
        )
        for plan, execution, observation in zip(plans, execution_results, observations)
    )


__all__ = [
    "MEMORY_FACT_MAX_CHANGES",
    "MEMORY_FACT_MAX_FILES",
    "MEMORY_FACT_MAX_SCOPE_TARGETS",
    "MEMORY_FACT_MAX_TEXT_CHARS",
    "BarrierExecutionResult",
    "MemoryBarrierFact",
    "build_memory_barrier_fact",
    "build_memory_barrier_facts",
]
