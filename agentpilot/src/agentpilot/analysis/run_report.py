"""Read-only on-policy run reporting.

The projection intentionally consumes only canonical persistence ports.  It
does not import a policy implementation, estimate counterfactual behavior, or
write derived values back into the search loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from types import MappingProxyType
from typing import Mapping

from agentpilot.domain import (
    DirectionMatchKind,
    EffectivenessLabel,
    EvidenceTier,
    PlanningFailureEvent,
    PolicyAction,
    PolicyDecisionPlan,
    PolicyMethod,
    PolicyObservation,
    SlotAdherenceStatus,
    SlotKind,
)
from agentpilot.persistence import BatchRecord, BatchStatus, UnitOfWorkFactory
from agentpilot.search.read_model import SearchProgramRecord


@dataclass(frozen=True)
class BatchCurvePoint:
    batch_id: str
    method: PolicyMethod
    status: BatchStatus
    planned_actions: int
    dispatched_actions: int
    completed_actions: int
    success_count: int
    failure_count: int
    skip_count: int
    admitted_count: int
    batch_cost_usd: float
    cumulative_cost_usd: float
    cumulative_elapsed_seconds: float
    # Adaptive-B only: a root's visible-A score is not a baseline for this curve.
    best_primary_value: float | None
    failure_reasons: Mapping[str, int]


@dataclass(frozen=True)
class WithinArmOutcomeSpread:
    """Observed attributable outcomes for one selected UCB arm.

    Skip observations have no reward statistic and are intentionally excluded
    from the numeric spread.  ``spread`` and ``population_stddev`` remain
    unavailable until at least two valid observations exist.
    """

    arm_id: str
    observation_count: int
    success_count: int
    failure_count: int
    minimum_signed_statistic: float | None
    maximum_signed_statistic: float | None
    mean_signed_statistic: float | None
    spread: float | None
    population_stddev: float | None


@dataclass(frozen=True)
class UCBRunDiagnostics:
    """Descriptive UCB diagnostics with deliberately narrow denominators.

    These values are projections of persisted facts, not policy scores.  Field
    names state their population (selected actions, eligible slates, or failed
    rounds) so a UI cannot silently present a partial population as a run-wide
    causal rate.
    """

    observed_selected_arm_count: int
    observed_selected_arm_sizes: Mapping[str, int]
    selected_match_counts: Mapping[str, int]
    eligible_slate_match_counts: Mapping[str, int]
    selected_existing_or_pending_match_rate: float | None
    failed_round_attempt_status_counts: Mapping[str, int]
    failed_round_attempt_match_counts: Mapping[str, int]
    failed_round_attempt_count: int
    failed_round_reject_count: int
    failed_round_ambiguous_count: int
    failed_round_reject_rate: float | None
    failed_round_ambiguous_rate: float | None
    allocated_slot_count: int
    allocated_discover_slot_count: int
    discover_selected_action_count: int
    discover_dispatched_action_count: int
    discover_final_new_dispatched_count: int
    discover_selection_yield: float | None
    discover_effectiveness_success_count: int
    discover_effectiveness_failure_count: int
    discover_effectiveness_yield: float | None
    eligible_slate_no_neighbor_count: int
    eligible_slate_no_neighbor_rate: float | None
    exact_direction_retry_after_failure_count: int
    exact_direction_retry_after_failure_rate: float | None
    within_arm_outcome_spread: tuple[WithinArmOutcomeSpread, ...]
    unavailable_metrics: Mapping[str, str]


@dataclass(frozen=True)
class RunReport:
    run_id: str
    curve: tuple[BatchCurvePoint, ...]
    total_cost_usd: float
    total_actions: int
    total_admitted: int
    ucb_diagnostics: UCBRunDiagnostics


class RunReportBuilder:
    """Build an on-policy curve without creating a behavior dependency."""

    def __init__(self, unit_of_work: UnitOfWorkFactory):
        self._unit_of_work = unit_of_work

    def build(self, run_id: str) -> RunReport:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be non-empty text")
        with self._unit_of_work() as work:
            batches = work.batches.list_for_run(run_id)
            costs = work.usage.list_costs_for_run(run_id)
            programs = work.search_read_model.list_programs_for_run(run_id)
            actions_by_batch = {
                batch.batch_id: work.actions.list_for_batch(batch.batch_id)
                for batch in batches
            }
            effectiveness = {
                action.action_id: work.outcomes.effectiveness_for_action(
                    action.action_id
                )
                for actions in actions_by_batch.values()
                for action in actions
            }
            outcomes = {
                action.action_id: work.outcomes.outcome_for_action(action.action_id)
                for actions in actions_by_batch.values()
                for action in actions
            }
            plans = tuple(
                plan
                for batch in batches
                for plan in work.plans.list_for_batch(batch.batch_id)
            )
            planning_failures = tuple(
                event
                for batch in batches
                for event in work.planning_failures.list_for_batch(batch.batch_id)
            )
            observations = tuple(
                observation
                for batch in batches
                for observation in work.observations.list_for_batch(batch.batch_id)
            )

        if not batches:
            return RunReport(
                run_id,
                (),
                0.0,
                0,
                0,
                _build_ucb_diagnostics((), (), (), (), ()),
            )

        cost_by_batch: dict[str, float] = {}
        for entry in costs:
            cost_by_batch[entry.batch_id] = (
                cost_by_batch.get(entry.batch_id, 0.0) + entry.cost_usd
            )

        direction = _metric_direction(programs)
        root_values = tuple(
            record.evidence.primary_value
            for record in programs
            if record.source_action_id is None
            and record.evidence.primary_value is not None
            and record.evidence.evidence_tier is EvidenceTier.ADAPTIVE_B
        )
        visible_values = list(root_values)
        program_by_action = {
            record.source_action_id: record
            for record in programs
            if record.source_action_id is not None
        }
        run_started_at = min(batch.created_at for batch in batches)
        cumulative_cost = 0.0
        cumulative_admitted = 0
        cumulative_elapsed = 0.0
        curve: list[BatchCurvePoint] = []

        for batch in batches:
            actions = actions_by_batch[batch.batch_id]
            labels = [
                item.label
                for action in actions
                if (item := effectiveness[action.action_id]) is not None
            ]
            reasons: dict[str, int] = {}
            for action in actions:
                item = effectiveness[action.action_id]
                if item is not None and item.label in {
                    EffectivenessLabel.FAILURE,
                    EffectivenessLabel.SKIP,
                }:
                    reasons[item.reason] = reasons.get(item.reason, 0) + 1

            admitted = 0
            completed_times: list[datetime] = []
            for action in actions:
                outcome = outcomes[action.action_id]
                if outcome is not None:
                    completed_times.append(outcome.completed_at)
                program = program_by_action.get(action.action_id)
                if program is not None:
                    admitted += 1
                    value = program.evidence.primary_value
                    if value is not None:
                        visible_values.append(value)

            batch_cost = cost_by_batch.get(batch.batch_id, 0.0)
            cumulative_cost += batch_cost
            cumulative_admitted += admitted
            observed_at = max(completed_times, default=batch.created_at)
            cumulative_elapsed = max(
                cumulative_elapsed,
                max(0.0, (observed_at - run_started_at).total_seconds()),
            )
            curve.append(
                BatchCurvePoint(
                    batch_id=batch.batch_id,
                    method=batch.method,
                    status=batch.status,
                    planned_actions=len(batch.action_ids),
                    dispatched_actions=len(batch.dispatched_action_ids),
                    completed_actions=len(batch.completed_action_ids),
                    success_count=labels.count(EffectivenessLabel.SUCCESS),
                    failure_count=labels.count(EffectivenessLabel.FAILURE),
                    skip_count=labels.count(EffectivenessLabel.SKIP),
                    admitted_count=admitted,
                    batch_cost_usd=batch_cost,
                    cumulative_cost_usd=cumulative_cost,
                    cumulative_elapsed_seconds=cumulative_elapsed,
                    best_primary_value=_best(visible_values, direction),
                    failure_reasons=MappingProxyType(dict(sorted(reasons.items()))),
                )
            )

        return RunReport(
            run_id=run_id,
            curve=tuple(curve),
            total_cost_usd=cumulative_cost,
            total_actions=sum(len(actions) for actions in actions_by_batch.values()),
            total_admitted=cumulative_admitted,
            ucb_diagnostics=_build_ucb_diagnostics(
                tuple(
                    action
                    for actions in actions_by_batch.values()
                    for action in actions
                ),
                plans,
                planning_failures,
                observations,
                tuple(batch for batch in batches),
            ),
        )


def _build_ucb_diagnostics(
    actions: tuple[PolicyAction, ...],
    plans: tuple[PolicyDecisionPlan, ...],
    planning_failures: tuple[PlanningFailureEvent, ...],
    observations: tuple[PolicyObservation, ...],
    batches: tuple[BatchRecord, ...],
) -> UCBRunDiagnostics:
    ucb_actions = tuple(
        sorted(
            (action for action in actions if action.method is PolicyMethod.UCB),
            key=lambda action: (action.created_at, action.action_id),
        )
    )
    arm_sizes: dict[str, int] = {}
    selected_matches: dict[str, int] = {}
    for action in ucb_actions:
        assert action.arm_id is not None
        assert action.direction_match is not None
        arm_sizes[action.arm_id] = arm_sizes.get(action.arm_id, 0) + 1
        _increment(selected_matches, action.direction_match.kind.value)

    eligible_matches: dict[str, int] = {}
    no_neighbor = 0
    eligible_candidate_count = 0
    unique_slots: dict[tuple[str, str], SlotKind] = {}
    for plan in plans:
        if plan.method is not PolicyMethod.UCB:
            continue
        for candidate in plan.direction_slate.candidates:
            if candidate.match is None:
                continue
            eligible_candidate_count += 1
            _increment(eligible_matches, candidate.match.kind.value)
            if (
                candidate.match.kind is DirectionMatchKind.NEW
                and candidate.match.distance is None
            ):
                no_neighbor += 1
        if plan.arm_allocation is not None:
            for slot in plan.arm_allocation.slots:
                unique_slots[(plan.action.decision_round_id, slot.slot_id)] = slot.kind

    failed_statuses: dict[str, int] = {}
    failed_matches: dict[str, int] = {}
    failed_attempt_count = 0
    failed_reject_count = 0
    failed_ambiguous_count = 0
    for event in planning_failures:
        for attempt in event.slot_attempts:
            failed_attempt_count += 1
            _increment(failed_statuses, attempt.status.value)
            if attempt.match_kind is not None:
                _increment(failed_matches, attempt.match_kind.value)
            if attempt.status is not SlotAdherenceStatus.ADHERENT:
                failed_reject_count += 1
            if (
                attempt.status is SlotAdherenceStatus.AMBIGUOUS
                or attempt.match_kind is DirectionMatchKind.AMBIGUOUS
            ):
                failed_ambiguous_count += 1
            unique_slots[(event.decision_round_id, attempt.source_slot_id)] = (
                attempt.source_slot_kind
            )

    dispatched_action_ids = {
        action_id
        for batch in batches
        for action_id in batch.dispatched_action_ids
    }
    discover_actions = tuple(
        action
        for action in ucb_actions
        if action.source_slot is not None
        and action.source_slot.kind is SlotKind.DISCOVER
    )
    discover_dispatched = tuple(
        action
        for action in discover_actions
        if action.action_id in dispatched_action_ids
    )
    discover_final_new_dispatched = tuple(
        action
        for action in discover_dispatched
        if action.direction_match is not None
        and action.direction_match.kind
        in {DirectionMatchKind.NEW, DirectionMatchKind.PENDING_NEW}
    )
    discover_slot_count = sum(
        kind is SlotKind.DISCOVER for kind in unique_slots.values()
    )

    ucb_observations = tuple(
        observation
        for observation in observations
        if observation.method is PolicyMethod.UCB
    )
    discover_effectiveness = tuple(
        observation.effectiveness.label
        for observation in ucb_observations
        if observation.source_slot_kind is SlotKind.DISCOVER
        and observation.direction_match is not None
        and observation.direction_match.kind
        in {DirectionMatchKind.NEW, DirectionMatchKind.PENDING_NEW}
        and observation.effectiveness.label
        in {EffectivenessLabel.SUCCESS, EffectivenessLabel.FAILURE}
    )
    discover_successes = discover_effectiveness.count(EffectivenessLabel.SUCCESS)
    discover_failures = discover_effectiveness.count(EffectivenessLabel.FAILURE)

    by_arm: dict[str, list[PolicyObservation]] = {}
    for observation in ucb_observations:
        if (
            observation.arm_id is not None
            and observation.effectiveness.label
            in {EffectivenessLabel.SUCCESS, EffectivenessLabel.FAILURE}
            and observation.effectiveness.signed_statistic is not None
        ):
            by_arm.setdefault(observation.arm_id, []).append(observation)
    spreads = tuple(
        _arm_spread(arm_id, tuple(by_arm[arm_id])) for arm_id in sorted(by_arm)
    )

    effectiveness_by_action = {
        observation.action_id: observation.effectiveness.label
        for observation in ucb_observations
    }
    previously_failed_directions: set[str] = set()
    repeated_after_failure = 0
    for action in ucb_actions:
        direction_id = action.direction.direction_id
        if direction_id in previously_failed_directions:
            repeated_after_failure += 1
        if effectiveness_by_action.get(action.action_id) is EffectivenessLabel.FAILURE:
            previously_failed_directions.add(direction_id)

    selected_match_denominator = sum(selected_matches.values())
    selected_matched = sum(
        selected_matches.get(kind.value, 0)
        for kind in (DirectionMatchKind.EXISTING, DirectionMatchKind.PENDING_NEW)
    )
    unavailable = MappingProxyType(
        {
            "all_attempt_match_ambiguous_reject_rates": (
                "successful planning records retain only repaired, eligible slate "
                "candidates; rejected repair attempts are canonical only for failed "
                "rounds"
            ),
            "run_wide_no_neighbor_rate": (
                "failed-round PlanningSlotAttempt does not persist match distance; "
                "the reported no-neighbor rate is scoped to eligible slates"
            ),
            "semantic_repeated_failed_direction_rate": (
                "the UnitOfWork read model does not expose executed semantic member "
                "embeddings or their member-to-action join; only exact direction_id "
                "retries are reported"
            ),
            "registry_arm_count_and_size": (
                "the canonical analysis ports expose selected actions, not a frozen "
                "arm-registry read model; observed selected-arm counts are reported"
            ),
        }
    )
    return UCBRunDiagnostics(
        observed_selected_arm_count=len(arm_sizes),
        observed_selected_arm_sizes=_frozen_counts(arm_sizes),
        selected_match_counts=_frozen_counts(selected_matches),
        eligible_slate_match_counts=_frozen_counts(eligible_matches),
        selected_existing_or_pending_match_rate=_ratio(
            selected_matched, selected_match_denominator,
        ),
        failed_round_attempt_status_counts=_frozen_counts(failed_statuses),
        failed_round_attempt_match_counts=_frozen_counts(failed_matches),
        failed_round_attempt_count=failed_attempt_count,
        failed_round_reject_count=failed_reject_count,
        failed_round_ambiguous_count=failed_ambiguous_count,
        failed_round_reject_rate=_ratio(failed_reject_count, failed_attempt_count),
        failed_round_ambiguous_rate=_ratio(
            failed_ambiguous_count, failed_attempt_count,
        ),
        allocated_slot_count=len(unique_slots),
        allocated_discover_slot_count=discover_slot_count,
        discover_selected_action_count=len(discover_actions),
        discover_dispatched_action_count=len(discover_dispatched),
        discover_final_new_dispatched_count=len(discover_final_new_dispatched),
        discover_selection_yield=_ratio(
            len(discover_actions), discover_slot_count,
        ),
        discover_effectiveness_success_count=discover_successes,
        discover_effectiveness_failure_count=discover_failures,
        discover_effectiveness_yield=_ratio(
            discover_successes, discover_successes + discover_failures,
        ),
        eligible_slate_no_neighbor_count=no_neighbor,
        eligible_slate_no_neighbor_rate=_ratio(
            no_neighbor, eligible_candidate_count,
        ),
        exact_direction_retry_after_failure_count=repeated_after_failure,
        exact_direction_retry_after_failure_rate=_ratio(
            repeated_after_failure, len(ucb_actions),
        ),
        within_arm_outcome_spread=spreads,
        unavailable_metrics=unavailable,
    )


def _arm_spread(
    arm_id: str, observations: tuple[PolicyObservation, ...]
) -> WithinArmOutcomeSpread:
    values = tuple(
        float(observation.effectiveness.signed_statistic)
        for observation in observations
        if observation.effectiveness.signed_statistic is not None
    )
    mean = sum(values) / len(values)
    enough = len(values) >= 2
    return WithinArmOutcomeSpread(
        arm_id=arm_id,
        observation_count=len(values),
        success_count=sum(
            observation.effectiveness.label is EffectivenessLabel.SUCCESS
            for observation in observations
        ),
        failure_count=sum(
            observation.effectiveness.label is EffectivenessLabel.FAILURE
            for observation in observations
        ),
        minimum_signed_statistic=min(values),
        maximum_signed_statistic=max(values),
        mean_signed_statistic=mean,
        spread=(max(values) - min(values)) if enough else None,
        population_stddev=(
            math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
            if enough
            else None
        ),
    )


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _frozen_counts(counts: Mapping[str, int]) -> Mapping[str, int]:
    return MappingProxyType(dict(sorted(counts.items())))


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _metric_direction(programs: tuple[SearchProgramRecord, ...]) -> str:
    directions = {
        record.evidence.compatibility.metric_direction for record in programs
    }
    if len(directions) > 1:
        raise ValueError("run report cannot mix metric directions")
    return next(iter(directions), "maximize")


def _best(values: list[float] | tuple[float, ...], direction: str) -> float | None:
    if not values:
        return None
    return max(values) if direction == "maximize" else min(values)


__all__ = [
    "BatchCurvePoint",
    "RunReport",
    "RunReportBuilder",
    "UCBRunDiagnostics",
    "WithinArmOutcomeSpread",
]
