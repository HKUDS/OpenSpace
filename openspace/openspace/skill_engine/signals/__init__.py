"""Quality signal contracts and storage."""

from .detector import (
    CHECKPOINT_TASK_SESSION_PERSISTED,
    QualitySignalDetector,
    stable_failure_signature,
)
from .policy import QualitySignalTriggerPolicy
from .reconciliation import (
    QualitySignalReconciler,
    QualitySignalReconciliationResult,
)
from .store import QualitySignalStore
from .types import (
    ACTION_MANUAL_REVIEW,
    ACTION_OBSERVE_ONLY,
    ACTION_RANKING_ONLY,
    ACTION_TRIGGER_REVIEW,
    SIGNAL_AGGREGATE_WITHOUT_INCIDENT,
    SIGNAL_SKILL_SELECTION_NOT_INVOKED,
    SIGNAL_TOOL_CALL_FAILED,
    SIGNAL_TOOL_FAILURE_AFFECTS_SKILL,
    SIGNAL_TOOL_SEMANTIC_ISSUE,
    STATUS_ACTIONABLE_PARTIAL,
    STATUS_AGGREGATE_ONLY,
    STATUS_AMBIGUOUS_SUBJECT,
    STATUS_CONFLICTING,
    STATUS_COMPLETE,
    STATUS_EXTERNAL_ONLY,
    STATUS_MISSING_RESULT,
    STATUS_MISSING_SKILL_CONTEXT,
    TRIGGERABLE_EVIDENCE_STATUSES,
    QualitySignal,
    QualitySignalWriteResult,
    quality_signal_from_ref,
    quality_signal_to_ref,
)

__all__ = [
    "ACTION_MANUAL_REVIEW",
    "ACTION_OBSERVE_ONLY",
    "ACTION_RANKING_ONLY",
    "ACTION_TRIGGER_REVIEW",
    "CHECKPOINT_TASK_SESSION_PERSISTED",
    "SIGNAL_AGGREGATE_WITHOUT_INCIDENT",
    "SIGNAL_SKILL_SELECTION_NOT_INVOKED",
    "SIGNAL_TOOL_CALL_FAILED",
    "SIGNAL_TOOL_FAILURE_AFFECTS_SKILL",
    "SIGNAL_TOOL_SEMANTIC_ISSUE",
    "STATUS_ACTIONABLE_PARTIAL",
    "STATUS_AGGREGATE_ONLY",
    "STATUS_AMBIGUOUS_SUBJECT",
    "STATUS_CONFLICTING",
    "STATUS_COMPLETE",
    "STATUS_EXTERNAL_ONLY",
    "STATUS_MISSING_RESULT",
    "STATUS_MISSING_SKILL_CONTEXT",
    "TRIGGERABLE_EVIDENCE_STATUSES",
    "QualitySignal",
    "QualitySignalDetector",
    "QualitySignalReconciler",
    "QualitySignalReconciliationResult",
    "QualitySignalStore",
    "QualitySignalTriggerPolicy",
    "QualitySignalWriteResult",
    "quality_signal_from_ref",
    "quality_signal_to_ref",
    "stable_failure_signature",
]
