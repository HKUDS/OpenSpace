"""Shared TriggerJob completion decisions for evolution runners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class EvolutionJobCompletion:
    status: str
    result_ref: str | None = None
    error: str | None = None
    needs_recovery: bool = False


def completion_from_outcome(outcome: Any) -> EvolutionJobCompletion:
    """Map an EvolutionRunResult-like object to a TriggerJob terminal status."""

    result_ref = outcome_result_ref(outcome)
    needs_recovery = outcome_has_committing_action(outcome)
    status = "completed"
    error = None
    if str(getattr(outcome, "status", "") or "").startswith("failed"):
        status = "failed" if needs_recovery else "failed_retryable"
        errors = getattr(outcome, "errors", []) or []
        error = "; ".join(str(item) for item in errors)[:1000] or None
    return EvolutionJobCompletion(
        status=status,
        result_ref=result_ref,
        error=error,
        needs_recovery=needs_recovery,
    )


def completion_after_recovery(
    outcome: Any,
    recovered_actions: list[Any] | tuple[Any, ...] | None,
) -> EvolutionJobCompletion:
    """Map an outcome plus action reconciliation results to one job completion."""

    base = completion_from_outcome(outcome)
    if not base.needs_recovery:
        return base

    actions = list(recovered_actions or [])
    if not actions:
        error = base.error or "commit action needs recovery but no recovery result was produced"
        return EvolutionJobCompletion(
            status="failed" if base.status == "failed" else "failed_retryable",
            result_ref=base.result_ref,
            error=error[:1000],
            needs_recovery=True,
        )

    result_ref = action_result_ref(actions) or base.result_ref
    statuses = [str(_field_value(action, "commit_status") or "").lower() for action in actions]
    if statuses and all(status in {"committed", "committed_reconciled"} for status in statuses):
        return EvolutionJobCompletion(
            status="completed",
            result_ref=result_ref,
            error=None,
            needs_recovery=False,
        )

    error = recovery_error(actions) or base.error or "commit action recovery did not reconcile"
    if any(status == "committing" for status in statuses):
        status = "failed_retryable" if base.status != "failed" else "failed"
    else:
        status = "failed"
    return EvolutionJobCompletion(
        status=status,
        result_ref=result_ref,
        error=error[:1000],
        needs_recovery=status == "failed_retryable",
    )


def outcome_result_ref(outcome: Any) -> str | None:
    actions = list(getattr(outcome, "actions", []) or [])
    return action_result_ref(actions)


def action_result_ref(actions: list[Any] | tuple[Any, ...]) -> str | None:
    for action in reversed(list(actions)):
        action_id = _field_value(action, "action_id")
        if action_id:
            return f"evolution_action:{action_id}"
    return None


def outcome_has_committing_action(outcome: Any) -> bool:
    for action in getattr(outcome, "actions", []) or []:
        status = _field_value(action, "commit_status")
        if str(status or "").lower() == "committing":
            return True
    return False


def _field_value(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def recovery_error(actions: list[Any] | tuple[Any, ...]) -> str | None:
    messages: list[str] = []
    for action in actions:
        action_id = str(_field_value(action, "action_id") or "")
        status = str(_field_value(action, "commit_status") or "")
        reason = str(_field_value(action, "failure_reason") or "")
        if status.lower() in {"committed", "committed_reconciled"} and not reason:
            continue
        prefix = f"{action_id}: " if action_id else ""
        detail = reason or status or "unknown recovery status"
        messages.append(f"{prefix}{detail}")
    return "; ".join(messages)[:1000] or None
