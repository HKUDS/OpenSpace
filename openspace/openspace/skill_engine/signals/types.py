"""Quality signal data contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from openspace.skill_engine.evidence import ResourceRef


ACTION_OBSERVE_ONLY = "observe_only"
ACTION_RANKING_ONLY = "ranking_only"
ACTION_MANUAL_REVIEW = "manual_review"
ACTION_TRIGGER_REVIEW = "trigger_review"

STATUS_COMPLETE = "complete"
STATUS_ACTIONABLE_PARTIAL = "actionable_partial"
STATUS_MISSING_RESULT = "missing_result"
STATUS_MISSING_SKILL_CONTEXT = "missing_skill_context"
STATUS_AGGREGATE_ONLY = "aggregate_only"
STATUS_AMBIGUOUS_SUBJECT = "ambiguous_subject"
STATUS_CONFLICTING = "conflicting"
STATUS_EXTERNAL_ONLY = "external_only"

SIGNAL_TOOL_CALL_FAILED = "tool_call_failed"
SIGNAL_TOOL_FAILURE_AFFECTS_SKILL = "tool_failure_affects_skill"
SIGNAL_AGGREGATE_WITHOUT_INCIDENT = "aggregate_without_incident"
SIGNAL_TOOL_SEMANTIC_ISSUE = "tool_semantic_issue"
SIGNAL_SKILL_SELECTION_NOT_INVOKED = "skill_selection_not_invoked"

ACTIONABILITIES: frozenset[str] = frozenset(
    {
        ACTION_OBSERVE_ONLY,
        ACTION_RANKING_ONLY,
        ACTION_MANUAL_REVIEW,
        ACTION_TRIGGER_REVIEW,
    }
)
EVIDENCE_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_COMPLETE,
        STATUS_ACTIONABLE_PARTIAL,
        STATUS_MISSING_RESULT,
        STATUS_MISSING_SKILL_CONTEXT,
        STATUS_AGGREGATE_ONLY,
        STATUS_AMBIGUOUS_SUBJECT,
        STATUS_CONFLICTING,
        STATUS_EXTERNAL_ONLY,
    }
)
TRIGGERABLE_EVIDENCE_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_COMPLETE,
        STATUS_ACTIONABLE_PARTIAL,
    }
)
SIGNAL_TYPES: frozenset[str] = frozenset(
    {
        SIGNAL_TOOL_CALL_FAILED,
        SIGNAL_TOOL_FAILURE_AFFECTS_SKILL,
        SIGNAL_AGGREGATE_WITHOUT_INCIDENT,
        SIGNAL_TOOL_SEMANTIC_ISSUE,
        SIGNAL_SKILL_SELECTION_NOT_INVOKED,
    }
)

QUALITY_SIGNAL_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "signal_id",
        "signal_type",
        "subject_type",
        "subject_id",
        "actionability",
        "evidence_status",
        "merge_key",
        "failure_signature",
        "tool_key",
        "skill_id",
        "skill_version",
        "source_watermark",
        "missing_refs",
    }
)


@dataclass(frozen=True, slots=True)
class QualitySignal:
    signal_id: str
    signal_type: str
    subject_type: str
    subject_id: str
    actionability: str
    evidence_status: str
    merge_key: str
    failure_signature: str
    raw_backrefs: tuple[str, ...]
    session_id: str | None = None
    task_id: str | None = None
    parent_task_id: str | None = None
    agent_id: str | None = None
    tool_key: str | None = None
    skill_id: str | None = None
    skill_version: str | None = None
    source_watermark: int | None = None
    missing_refs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QualitySignalWriteResult:
    refs: list[ResourceRef]
    write_watermark: int


_ACTIONABILITY_RANK: Mapping[str, int] = {
    ACTION_OBSERVE_ONLY: 10,
    ACTION_RANKING_ONLY: 20,
    ACTION_TRIGGER_REVIEW: 30,
    ACTION_MANUAL_REVIEW: 40,
}

_EVIDENCE_STATUS_RANK: Mapping[str, int] = {
    STATUS_AGGREGATE_ONLY: 10,
    STATUS_MISSING_RESULT: 40,
    STATUS_MISSING_SKILL_CONTEXT: 45,
    STATUS_ACTIONABLE_PARTIAL: 70,
    STATUS_COMPLETE: 80,
    STATUS_AMBIGUOUS_SUBJECT: 90,
    STATUS_EXTERNAL_ONLY: 100,
    STATUS_CONFLICTING: 110,
}


def quality_signal_precedence(signal: QualitySignal) -> tuple[int, int, int, int, int]:
    """Rank signals for the same merge key by evidence decisiveness."""

    return (
        _EVIDENCE_STATUS_RANK.get(signal.evidence_status, 0),
        _ACTIONABILITY_RANK.get(signal.actionability, 0),
        -len(signal.missing_refs),
        len(signal.raw_backrefs),
        signal.source_watermark or 0,
    )


def choose_dominant_signal(first: QualitySignal, second: QualitySignal) -> QualitySignal:
    """Return the stronger signal for a shared merge key."""

    return second if quality_signal_precedence(second) > quality_signal_precedence(first) else first


def quality_signal_ref_id(signal_id: str) -> str:
    return f"quality_signal:{signal_id}"


def quality_signal_to_ref(
    signal: QualitySignal,
    *,
    raw_backrefs: Sequence[str] | None = None,
    metadata: Mapping[str, Any] | None = None,
    preview: str | None = None,
) -> ResourceRef:
    """Map a quality signal to its derived evidence ResourceRef."""

    merged_metadata: dict[str, Any] = {
        "signal_id": signal.signal_id,
        "signal_type": signal.signal_type,
        "subject_type": signal.subject_type,
        "subject_id": signal.subject_id,
        "actionability": signal.actionability,
        "evidence_status": signal.evidence_status,
        "merge_key": signal.merge_key,
        "failure_signature": signal.failure_signature,
        "tool_key": signal.tool_key,
        "skill_id": signal.skill_id,
        "skill_version": signal.skill_version,
        "source_watermark": signal.source_watermark,
        "missing_refs": list(signal.missing_refs),
    }
    merged_metadata.update(dict(signal.metadata))
    if metadata is not None:
        merged_metadata.update(dict(metadata))

    return ResourceRef(
        ref_id=quality_signal_ref_id(signal.signal_id),
        ref_type="quality_signal_ref",
        session_id=signal.session_id,
        task_id=signal.task_id,
        parent_task_id=signal.parent_task_id,
        agent_id=signal.agent_id,
        producer="quality_signal_detector",
        reliability="derived",
        role="primary",
        preview=preview if preview is not None else _signal_preview(signal),
        raw_backrefs=list(stable_unique(raw_backrefs if raw_backrefs is not None else signal.raw_backrefs)),
        metadata=merged_metadata,
    )


def quality_signal_from_ref(ref: ResourceRef) -> QualitySignal:
    if ref.ref_type != "quality_signal_ref":
        raise ValueError(f"Expected quality_signal_ref, got {ref.ref_type}")
    metadata = dict(ref.metadata)
    signal_id = str(metadata.get("signal_id") or ref.ref_id.removeprefix("quality_signal:"))
    extra_metadata = {
        key: value
        for key, value in metadata.items()
        if key not in QUALITY_SIGNAL_METADATA_KEYS
    }
    return QualitySignal(
        signal_id=signal_id,
        signal_type=str(metadata.get("signal_type") or ""),
        subject_type=str(metadata.get("subject_type") or ""),
        subject_id=str(metadata.get("subject_id") or ""),
        actionability=str(metadata.get("actionability") or ""),
        evidence_status=str(metadata.get("evidence_status") or ""),
        merge_key=str(metadata.get("merge_key") or ""),
        failure_signature=str(metadata.get("failure_signature") or ""),
        raw_backrefs=tuple(stable_unique(ref.raw_backrefs)),
        session_id=ref.session_id,
        task_id=ref.task_id,
        parent_task_id=ref.parent_task_id,
        agent_id=ref.agent_id,
        tool_key=_none_or_str(metadata.get("tool_key")),
        skill_id=_none_or_str(metadata.get("skill_id")),
        skill_version=_none_or_str(metadata.get("skill_version")),
        source_watermark=_none_or_int(metadata.get("source_watermark")),
        missing_refs=tuple(stable_unique(_str_sequence(metadata.get("missing_refs")))),
        metadata=extra_metadata,
    )


def stable_unique(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


def _signal_preview(signal: QualitySignal) -> str:
    return (
        f"{signal.signal_type} for {signal.subject_type}:{signal.subject_id} "
        f"actionability={signal.actionability} status={signal.evidence_status}"
    )


def _none_or_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _none_or_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_sequence(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value if str(item or ""))
    return ()
