"""Stable JSON projection over the read-only analysis boundary."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from agentpilot.analysis import (
    ActionDetail,
    BatchDetail,
    DecisionDetail,
    DirectionText,
    PlanningFailureDetail,
    ProgramNode,
    RunDetailBuilder,
    RunReportBuilder,
)
from agentpilot.persistence import UnitOfWorkFactory


def _plain(value: Any) -> Any:
    """Render a frozen projection value as JSON-safe data."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


class RunViewAdapter:
    """Translate canonical records into a behavior-neutral UI payload."""

    def __init__(self, unit_of_work: UnitOfWorkFactory):
        if not callable(unit_of_work):
            raise TypeError("unit_of_work must be callable")
        self._reports = RunReportBuilder(unit_of_work)
        self._details = RunDetailBuilder(unit_of_work)

    def build(self, run_id: str) -> dict[str, Any]:
        report = self._reports.build(run_id)
        diagnostics = report.ucb_diagnostics
        return {
            "run_id": report.run_id,
            "summary": {
                "total_cost_usd": report.total_cost_usd,
                "total_actions": report.total_actions,
                "total_admitted": report.total_admitted,
            },
            "curve": [
                {
                    "batch_id": point.batch_id,
                    "method": point.method.value,
                    "status": point.status.value,
                    "planned_actions": point.planned_actions,
                    "dispatched_actions": point.dispatched_actions,
                    "completed_actions": point.completed_actions,
                    "success_count": point.success_count,
                    "failure_count": point.failure_count,
                    "skip_count": point.skip_count,
                    "admitted_count": point.admitted_count,
                    "batch_cost_usd": point.batch_cost_usd,
                    "cumulative_cost_usd": point.cumulative_cost_usd,
                    "cumulative_elapsed_seconds": (point.cumulative_elapsed_seconds),
                    "best_primary_value": point.best_primary_value,
                    "failure_reasons": dict(point.failure_reasons),
                }
                for point in report.curve
            ],
            "ucb_diagnostics": {
                "observed_selected_arm_count": (
                    diagnostics.observed_selected_arm_count
                ),
                "observed_selected_arm_sizes": dict(
                    diagnostics.observed_selected_arm_sizes
                ),
                "selected_match_counts": dict(diagnostics.selected_match_counts),
                "eligible_slate_match_counts": dict(
                    diagnostics.eligible_slate_match_counts
                ),
                "selected_existing_or_pending_match_rate": (
                    diagnostics.selected_existing_or_pending_match_rate
                ),
                "failed_round_attempt_status_counts": dict(
                    diagnostics.failed_round_attempt_status_counts
                ),
                "failed_round_attempt_match_counts": dict(
                    diagnostics.failed_round_attempt_match_counts
                ),
                "failed_round_attempt_count": diagnostics.failed_round_attempt_count,
                "failed_round_reject_count": diagnostics.failed_round_reject_count,
                "failed_round_ambiguous_count": (
                    diagnostics.failed_round_ambiguous_count
                ),
                "failed_round_reject_rate": diagnostics.failed_round_reject_rate,
                "failed_round_ambiguous_rate": (
                    diagnostics.failed_round_ambiguous_rate
                ),
                "allocated_slot_count": diagnostics.allocated_slot_count,
                "allocated_discover_slot_count": (
                    diagnostics.allocated_discover_slot_count
                ),
                "discover_selected_action_count": (
                    diagnostics.discover_selected_action_count
                ),
                "discover_dispatched_action_count": (
                    diagnostics.discover_dispatched_action_count
                ),
                "discover_final_new_dispatched_count": (
                    diagnostics.discover_final_new_dispatched_count
                ),
                "discover_selection_yield": diagnostics.discover_selection_yield,
                "discover_effectiveness_success_count": (
                    diagnostics.discover_effectiveness_success_count
                ),
                "discover_effectiveness_failure_count": (
                    diagnostics.discover_effectiveness_failure_count
                ),
                "discover_effectiveness_yield": (
                    diagnostics.discover_effectiveness_yield
                ),
                "eligible_slate_no_neighbor_count": (
                    diagnostics.eligible_slate_no_neighbor_count
                ),
                "eligible_slate_no_neighbor_rate": (
                    diagnostics.eligible_slate_no_neighbor_rate
                ),
                "exact_direction_retry_after_failure_count": (
                    diagnostics.exact_direction_retry_after_failure_count
                ),
                "exact_direction_retry_after_failure_rate": (
                    diagnostics.exact_direction_retry_after_failure_rate
                ),
                "within_arm_outcome_spread": [
                    {
                        "arm_id": item.arm_id,
                        "observation_count": item.observation_count,
                        "success_count": item.success_count,
                        "failure_count": item.failure_count,
                        "minimum_signed_statistic": item.minimum_signed_statistic,
                        "maximum_signed_statistic": item.maximum_signed_statistic,
                        "mean_signed_statistic": item.mean_signed_statistic,
                        "spread": item.spread,
                        "population_stddev": item.population_stddev,
                    }
                    for item in diagnostics.within_arm_outcome_spread
                ],
                "unavailable_metrics": dict(diagnostics.unavailable_metrics),
            },
        }

    def build_detail(self, run_id: str) -> dict[str, Any]:
        """Return the run view plus the entity-level projection an inspector needs."""

        detail = self._details.build(run_id)
        meta = detail.meta
        payload = self.build(run_id)
        payload["meta"] = {
            "run_id": meta.run_id,
            "method": meta.method.value,
            "batch_count": meta.batch_count,
            "action_count": meta.action_count,
            "objective": meta.objective,
            "evaluator": meta.evaluator,
            "metric_name": meta.metric_name,
            "metric_direction": meta.metric_direction,
            "environment": meta.environment,
            "cell_ids": list(meta.cell_ids),
            "visible_a_evidence": [
                {
                    "cell_id": item.cell_id,
                    "task_id": item.task_id,
                    "title": item.title,
                    "statement": item.statement,
                    "tier": "visible_a",
                    "graded_answer": item.graded_answer,
                    "observed_failure": item.observed_failure,
                }
                for item in meta.visible_a_evidence
            ],
            "search_budget": _plain(meta.search_budget),
            "proposer": _actor(meta.proposer),
            "worker": _actor(meta.worker),
            "unavailable_fields": list(meta.unavailable_fields),
        }
        protocol = detail.protocol
        payload["protocol"] = {
            "tiers": [
                {
                    "tier": tier.tier,
                    "role": tier.role,
                    "cell_count": tier.cell_count,
                    "source": tier.source,
                    "state": tier.state,
                }
                for tier in protocol.tiers
            ],
            "estimator": protocol.estimator,
            "estimator_design": _plain(protocol.estimator_design),
            "measured_actions": protocol.measured_actions,
            "sealed_state": protocol.sealed_state,
            "sealed_expected_blocks": protocol.sealed_expected_blocks,
        }
        payload["batches"] = [_batch(item) for item in detail.batches]
        payload["programs"] = [_program(item) for item in detail.programs]
        payload["actions"] = [_action(item) for item in detail.actions]
        payload["decisions"] = [_decision(item) for item in detail.decisions]
        payload["planning_failures"] = [
            _planning_failure(item) for item in detail.planning_failures
        ]
        return payload


def _batch(item: BatchDetail) -> dict[str, Any]:
    return {
        "batch_id": item.batch_id,
        "label": item.label,
        "status": item.status.value,
        "state_id": item.state_id,
        "created_at": item.created_at.isoformat(),
        "expected_rounds": item.expected_rounds,
        "planned_action_ids": list(item.planned_action_ids),
        "dispatched_action_ids": list(item.dispatched_action_ids),
        "completed_action_ids": list(item.completed_action_ids),
        "cost_usd": item.cost_usd,
        "arm_ids": list(item.arm_ids),
        "memory_content": item.memory_content,
        "proposer": _actor(item.proposer),
        "worker": _actor(item.worker),
    }


def _program(item: ProgramNode) -> dict[str, Any]:
    return {
        "program_id": item.program_id,
        "label": item.label,
        "archive_label": item.archive_label,
        "parent_id": item.parent_id,
        "commit_sha": item.commit_sha,
        "archive_seq": item.archive_seq,
        "primary_value": item.primary_value,
        "cell_results": dict(item.cell_results),
        "source_action_id": item.source_action_id,
        "source_batch_id": item.source_batch_id,
        "parent_program_id": item.parent_program_id,
        "parent_label": item.parent_label,
        "available_at": item.available_at.isoformat(),
    }


def _direction(item: DirectionText) -> dict[str, Any]:
    return {
        "direction_id": item.direction_id,
        "failure_mechanism": item.failure_mechanism,
        "implementation_direction": item.implementation_direction,
        "declared_scope": _plain(item.declared_scope),
        "new_paths": _plain(item.new_paths),
        "resolved_scope": list(item.resolved_scope),
        "evidence_refs": list(item.evidence_refs),
    }


def _actor(item: Any) -> dict[str, Any]:
    return {
        "provider": item.provider,
        "model": item.model,
        "runner_id": item.runner_id,
        "scope_identity": item.scope_identity,
        "unavailable": list(item.unavailable),
    }


def _proposal(item: Any) -> dict[str, Any]:
    return {
        "request_id": item.request_id,
        "proposal_id": item.proposal_id,
        "provider": item.provider,
        "model": item.model,
        "candidate_count": item.candidate_count,
        "repair_feedback": item.repair_feedback,
        "status": item.status,
        "reason": item.reason,
    }


def _action(item: ActionDetail) -> dict[str, Any]:
    evaluation = item.evaluation
    execution = item.execution
    return {
        "action_id": item.action_id,
        "batch_id": item.batch_id,
        "decision_round_id": item.decision_round_id,
        "ordinal": item.ordinal,
        "attempt_number": item.attempt_number,
        "label": item.label,
        "parent_id": item.parent_id,
        "parent_label": item.parent_label,
        "parent_commit_sha": item.parent_commit_sha,
        "created_at": item.created_at.isoformat(),
        "archive_seq": item.archive_seq,
        "archive_label": item.archive_label,
        "lifecycle_stage": item.lifecycle_stage,
        "direction": _direction(item.direction),
        "proposer": _proposal(item.proposer),
        "worker": _actor(item.worker),
        "arm_id": item.arm_id,
        "slot_id": item.slot_id,
        "slot_kind": item.slot_kind,
        "match_kind": item.match_kind,
        "match_distance": item.match_distance,
        "effectiveness_label": item.effectiveness_label,
        "effectiveness_reason": item.effectiveness_reason,
        "effectiveness_margin": item.effectiveness_margin,
        "decision_trace": {
            "d1": _plain(item.d1_trace),
            "d2": _plain(item.d2_trace),
            "d3": _plain(item.d3_trace),
        },
        "evaluation": {
            "evidence_tier": evaluation.evidence_tier,
            "validity": evaluation.validity,
            "attribution": evaluation.attribution,
            "signed_statistic": evaluation.signed_statistic,
            "primary_delta_mean": evaluation.primary_delta_mean,
            "primary_delta_lower": evaluation.primary_delta_lower,
            "constraints_pass": evaluation.constraints_pass,
            "admission_fate": evaluation.admission_fate,
            "admitted": evaluation.admitted,
            "child_commit_sha": evaluation.child_commit_sha,
            "child_primary_value": evaluation.child_primary_value,
            "cell_results": dict(evaluation.cell_results),
            "cell_notes": dict(evaluation.cell_notes),
            "cell_diagnostics": _plain(evaluation.cell_diagnostics),
            "completed_at": (
                evaluation.completed_at.isoformat()
                if evaluation.completed_at is not None
                else None
            ),
        },
        "execution": {
            "worker_status": execution.worker_status,
            "adherence": execution.adherence,
            "cost_usd": execution.cost_usd,
            "result_commit_sha": execution.result_commit_sha,
            "touched_files": list(execution.touched_files),
            "file_changes": [dict(item) for item in execution.file_changes],
            "commit": {
                "sha": execution.result_commit_sha,
                "touched_files": list(execution.touched_files),
                "file_changes": [dict(item) for item in execution.file_changes],
                "touched_file_count": len(execution.touched_files),
                "subject": None,
                "message": None,
            },
            "started_at": (
                execution.started_at.isoformat()
                if execution.started_at is not None
                else None
            ),
            "completed_at": (
                execution.completed_at.isoformat()
                if execution.completed_at is not None
                else None
            ),
            "duration_seconds": execution.duration_seconds,
            "rework_count": execution.rework_count,
            "harness_attempt_count": execution.harness_attempt_count,
            "harness_attempts": _plain(execution.harness_attempts),
            "diagnostic_ran": execution.diagnostic_ran,
            "diagnostic_passed": execution.diagnostic_passed,
            "diagnostic_command": list(execution.diagnostic_command),
            "diagnostics": _plain(execution.diagnostics),
        },
    }


def _decision(item: DecisionDetail) -> dict[str, Any]:
    return {
        "action_id": item.action_id,
        "plan_id": item.plan_id,
        "method": item.method.value,
        "parent_seed": item.parent_seed,
        "parent_conditional_probability": item.parent_conditional_probability,
        "parent_options": [
            {
                "parent_id": option.parent_id,
                "label": option.label,
                "archive_seq": option.archive_seq,
                "commit_sha": option.commit_sha,
                "primary_value": option.primary_value,
                "index": option.index,
                "eligible": option.eligible,
                "tied": option.tied,
                "selected": option.selected,
            }
            for option in item.parent_options
        ],
        "allocation": _plain(item.allocation),
        "slots": [
            {
                "slot_id": slot.slot_id,
                "kind": slot.kind,
                "ordinal": slot.ordinal,
                "source_arm_id": slot.source_arm_id,
                "support_id": slot.support_id,
                "index": slot.index,
                "tied": slot.tied,
            }
            for slot in item.slots
        ],
        "direction_seed": item.direction_seed,
        "direction_conditional_probability": item.direction_conditional_probability,
        "direction_ranking": _plain(item.direction_ranking),
        "direction_options": [
            {
                "candidate_id": option.candidate_id,
                "direction": _direction(option.direction),
                "slot_id": option.slot_id,
                "slot_kind": option.slot_kind,
                "source_arm_id": option.source_arm_id,
                "match_kind": option.match_kind,
                "match_distance": option.match_distance,
                "match_arm_id": option.match_arm_id,
                "adherence_status": option.adherence_status,
                "adherence_reason": option.adherence_reason,
                "index": option.index,
                "estimate": _plain(option.estimate),
                "eligible": option.eligible,
                "tied": option.tied,
                "selected": option.selected,
            }
            for option in item.direction_options
        ],
    }


def _planning_failure(item: PlanningFailureDetail) -> dict[str, Any]:
    return {
        "batch_id": item.batch_id,
        "decision_round_id": item.decision_round_id,
        "round_index": item.round_index,
        "failure_kind": item.failure_kind,
        "attempts": _plain(item.attempts),
    }


__all__ = ["RunViewAdapter"]
