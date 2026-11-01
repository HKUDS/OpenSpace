"""Deterministic quality signal detector."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any, Iterable, Mapping, Sequence

from openspace.skill_engine.evidence import EvidenceScope, EvidenceStore, ResourceRef

from .linkers import (
    EvidenceIndexes,
    SkillContextCandidate,
    build_evidence_indexes,
    has_representative_tool_evidence,
    latest_ref,
    metadata_values,
    ref_sort_key,
    ref_tool_key,
    related_tool_incidents,
    related_tool_results,
    resolve_skill_contexts,
    tool_use_key,
)
from .types import (
    ACTION_MANUAL_REVIEW,
    ACTION_OBSERVE_ONLY,
    ACTION_TRIGGER_REVIEW,
    SIGNAL_AGGREGATE_WITHOUT_INCIDENT,
    SIGNAL_TOOL_CALL_FAILED,
    SIGNAL_TOOL_FAILURE_AFFECTS_SKILL,
    STATUS_ACTIONABLE_PARTIAL,
    STATUS_AGGREGATE_ONLY,
    STATUS_AMBIGUOUS_SUBJECT,
    STATUS_CONFLICTING,
    STATUS_COMPLETE,
    STATUS_EXTERNAL_ONLY,
    STATUS_MISSING_SKILL_CONTEXT,
    QualitySignal,
    choose_dominant_signal,
    stable_unique,
)


CHECKPOINT_TASK_SESSION_PERSISTED = "task_session_persisted"

_BASE_REF_TYPES = [
    "tool_event",
    "tool_result",
    "tool_incident",
    "tool_quality_record",
    "skill_event",
    "skill_record",
    "skill_file",
    "execution_analysis",
    "transcript_message",
]
_SKILL_CONTEXT_REF_TYPES = ["skill_record", "skill_file"]
_TOOL_CONTEXT_REF_TYPES = ["tool_quality_record", "tool_incident"]


class QualitySignalDetector:
    def __init__(self, evidence_store: EvidenceStore) -> None:
        self.evidence_store = evidence_store
        self.warnings: list[str] = []

    def scan_checkpoint(
        self,
        *,
        checkpoint_name: str,
        scope: EvidenceScope,
        manifest_watermark: int,
    ) -> list[QualitySignal]:
        self.warnings = []
        if checkpoint_name != CHECKPOINT_TASK_SESSION_PERSISTED:
            return []

        refs = self._load_refs(scope, manifest_watermark)
        indexes = build_evidence_indexes(refs)
        signals: list[QualitySignal] = []

        for tool_event in indexes.failed_tool_events:
            tool_call_signal = self._tool_call_failed_signal(
                tool_event,
                indexes,
                manifest_watermark=manifest_watermark,
            )
            if tool_call_signal is not None:
                signals.append(tool_call_signal)
            skill_signal = self._tool_failure_affects_skill_signal(
                tool_event,
                indexes,
                manifest_watermark=manifest_watermark,
            )
            if skill_signal is not None:
                signals.append(skill_signal)

        signals.extend(
            self._aggregate_without_incident_signals(
                indexes,
                manifest_watermark=manifest_watermark,
            )
        )
        return signals

    def _load_refs(
        self,
        scope: EvidenceScope,
        manifest_watermark: int,
    ) -> list[ResourceRef]:
        refs = self.evidence_store.query_refs(
            scope,
            ref_types=list(_BASE_REF_TYPES),
            watermark=manifest_watermark,
        )
        indexes = build_evidence_indexes(refs)
        skill_ids = sorted(
            {
                *indexes.skill_events_by_skill_id,
                *[item for item in scope.skill_ids if item],
            }
        )
        tool_keys = sorted(
            {
                *[
                    ref_tool_key(ref)
                    for bucket in indexes.tool_events_by_use_id.values()
                    for ref in bucket
                    if ref_tool_key(ref)
                ],
                *[item for item in scope.tool_keys if item],
            }
        )

        extra_refs: list[ResourceRef] = []
        if skill_ids:
            extra_refs.extend(
                self.evidence_store.query_refs(
                    EvidenceScope(skill_ids=tuple(skill_ids)),
                    ref_types=list(_SKILL_CONTEXT_REF_TYPES),
                    watermark=manifest_watermark,
                )
            )
        if tool_keys:
            extra_refs.extend(
                ref
                for ref in self.evidence_store.query_refs(
                    EvidenceScope(
                        session_id=scope.session_id,
                        task_id=scope.task_id,
                        source_task_ids=scope.source_task_ids,
                        agent_ids=scope.agent_ids,
                        tool_keys=tuple(tool_keys),
                    ),
                    ref_types=list(_TOOL_CONTEXT_REF_TYPES),
                    watermark=manifest_watermark,
                )
                if _same_checkpoint_context(scope, ref)
            )

        by_ref_id = {ref.ref_id: ref for ref in [*refs, *extra_refs] if ref.ref_id}
        return sorted(by_ref_id.values(), key=ref_sort_key)

    def _tool_call_failed_signal(
        self,
        tool_event: ResourceRef,
        indexes: EvidenceIndexes,
        *,
        manifest_watermark: int,
    ) -> QualitySignal | None:
        tool_key = ref_tool_key(tool_event)
        if not tool_key:
            self.warnings.append(f"malformed_tool_event_missing_tool_key:{tool_event.ref_id}")
            return None
        results = related_tool_results(tool_event, indexes)
        incidents = related_tool_incidents(tool_event, indexes)
        raw_backrefs = stable_unique(
            [tool_event.ref_id, *[ref.ref_id for ref in results], *[ref.ref_id for ref in incidents]]
        )
        return self._signal(
            signal_type=SIGNAL_TOOL_CALL_FAILED,
            subject_type="tool",
            subject_id=tool_key,
            actionability=ACTION_OBSERVE_ONLY,
            evidence_status=STATUS_COMPLETE,
            failure_signature=stable_failure_signature(
                SIGNAL_TOOL_CALL_FAILED,
                tool_event=tool_event,
                result_refs=results,
            ),
            raw_backrefs=raw_backrefs,
            source_ref=tool_event,
            tool_key=tool_key,
            skill_id=None,
            skill_version=None,
            manifest_watermark=manifest_watermark,
            metadata=_summary_metadata(
                tool_event,
                result_refs=results,
                incident_refs=incidents,
                linkage="tool_event_failed",
            )
            | {"policy_reason": "observe_tool_event_failed"},
        )

    def _tool_failure_affects_skill_signal(
        self,
        tool_event: ResourceRef,
        indexes: EvidenceIndexes,
        *,
        manifest_watermark: int,
    ) -> QualitySignal | None:
        tool_key = ref_tool_key(tool_event)
        if not tool_key:
            return None

        all_results = related_tool_results(tool_event, indexes)
        results = _complete_results(all_results)
        incidents = related_tool_incidents(tool_event, indexes)
        contexts = resolve_skill_contexts(tool_event, indexes)
        supporting_refs = [*results, *incidents]

        if not contexts:
            return self._signal(
                signal_type=SIGNAL_TOOL_FAILURE_AFFECTS_SKILL,
                subject_type="tool",
                subject_id=tool_key,
                actionability=ACTION_OBSERVE_ONLY,
                evidence_status=STATUS_MISSING_SKILL_CONTEXT,
                failure_signature=stable_failure_signature(
                    SIGNAL_TOOL_FAILURE_AFFECTS_SKILL,
                    tool_event=tool_event,
                    result_refs=results,
                ),
                raw_backrefs=stable_unique(
                    [tool_event.ref_id, *[ref.ref_id for ref in supporting_refs]]
                ),
                source_ref=tool_event,
                tool_key=tool_key,
                skill_id=None,
                skill_version=None,
                manifest_watermark=manifest_watermark,
                missing_refs=("skill_event", "skill_file"),
                metadata=_summary_metadata(
                    tool_event,
                    result_refs=results,
                    incident_refs=incidents,
                    linkage="missing_skill_context",
                )
                | {"policy_reason": "missing_skill_context"},
            )

        if len(contexts) > 1:
            raw_backrefs = [
                tool_event.ref_id,
                *[ref.ref_id for ref in supporting_refs],
                *[context.skill_event.ref_id for context in contexts],
            ]
            return self._signal(
                signal_type=SIGNAL_TOOL_FAILURE_AFFECTS_SKILL,
                subject_type="tool",
                subject_id=tool_key,
                actionability=ACTION_MANUAL_REVIEW,
                evidence_status=STATUS_AMBIGUOUS_SUBJECT,
                failure_signature=stable_failure_signature(
                    SIGNAL_TOOL_FAILURE_AFFECTS_SKILL,
                    tool_event=tool_event,
                    result_refs=results,
                ),
                raw_backrefs=stable_unique(raw_backrefs),
                source_ref=tool_event,
                tool_key=tool_key,
                skill_id=None,
                skill_version=None,
                manifest_watermark=manifest_watermark,
                missing_refs=("unique_skill_context",),
                metadata={
                    **_summary_metadata(
                        tool_event,
                        result_refs=results,
                        incident_refs=incidents,
                        linkage="ambiguous_skill_context",
                    ),
                    "candidate_skill_ids": [context.skill_id for context in contexts],
                    "policy_reason": "ambiguous_skill_context",
                },
            )

        context = contexts[0]
        skill_version = _skill_version(context)
        conflict_reasons = _conflicting_evidence_reasons(
            result_refs=all_results,
            incident_refs=incidents,
        )
        if context.skill_file is None:
            status = STATUS_MISSING_SKILL_CONTEXT
            actionability = ACTION_OBSERVE_ONLY
            missing_refs = ("skill_file",)
            policy_reason = "missing_skill_file"
        elif conflict_reasons:
            status = STATUS_CONFLICTING
            actionability = ACTION_MANUAL_REVIEW
            missing_refs = ("consistent_tool_evidence",)
            policy_reason = "conflicting_tool_evidence"
        elif _is_permission_or_user_denied(tool_event):
            status = STATUS_EXTERNAL_ONLY
            actionability = ACTION_OBSERVE_ONLY
            missing_refs = ()
            policy_reason = "permission_denied_without_skill_evidence"
        elif supporting_refs:
            status = STATUS_COMPLETE
            actionability = ACTION_TRIGGER_REVIEW
            missing_refs = ()
            policy_reason = "complete_exact_failure_evidence"
        else:
            status = STATUS_ACTIONABLE_PARTIAL
            actionability = ACTION_TRIGGER_REVIEW
            missing_refs = ("tool_result", "tool_incident")
            policy_reason = "actionable_partial_failed_tool_event"

        raw_tool_refs = [*all_results, *incidents] if conflict_reasons else supporting_refs
        raw_backrefs = [
            tool_event.ref_id,
            *[ref.ref_id for ref in raw_tool_refs],
            context.skill_event.ref_id,
        ]
        if context.skill_record is not None:
            raw_backrefs.append(context.skill_record.ref_id)
        if context.skill_file is not None:
            raw_backrefs.append(context.skill_file.ref_id)

        return self._signal(
            signal_type=SIGNAL_TOOL_FAILURE_AFFECTS_SKILL,
            subject_type="tool_skill_relation",
            subject_id=f"{context.skill_id}:{tool_key}",
            actionability=actionability,
            evidence_status=status,
            failure_signature=stable_failure_signature(
                SIGNAL_TOOL_FAILURE_AFFECTS_SKILL,
                tool_event=tool_event,
                skill_id=context.skill_id,
                result_refs=results,
            ),
            raw_backrefs=stable_unique(raw_backrefs),
            source_ref=tool_event,
            tool_key=tool_key,
            skill_id=context.skill_id,
            skill_version=skill_version,
            manifest_watermark=manifest_watermark,
            missing_refs=missing_refs,
            metadata={
                **_summary_metadata(
                    tool_event,
                    result_refs=results,
                    incident_refs=incidents,
                    linkage=context.link_type,
                ),
                "skill_context_ref": context.skill_event.ref_id,
                "policy_reason": policy_reason,
                **({"conflict_reasons": conflict_reasons} if conflict_reasons else {}),
            },
        )

    def _aggregate_without_incident_signals(
        self,
        indexes: EvidenceIndexes,
        *,
        manifest_watermark: int,
    ) -> list[QualitySignal]:
        signals: list[QualitySignal] = []
        for tool_key, records in sorted(indexes.quality_records_by_tool_key.items()):
            if has_representative_tool_evidence(tool_key, indexes):
                continue
            record = latest_ref(records)
            if record is None:
                continue
            signals.append(
                self._signal(
                    signal_type=SIGNAL_AGGREGATE_WITHOUT_INCIDENT,
                    subject_type="tool",
                    subject_id=tool_key,
                    actionability=ACTION_OBSERVE_ONLY,
                    evidence_status=STATUS_AGGREGATE_ONLY,
                    failure_signature=stable_failure_signature(
                        SIGNAL_AGGREGATE_WITHOUT_INCIDENT,
                        tool_key=tool_key,
                    ),
                    raw_backrefs=(record.ref_id,),
                    source_ref=record,
                    tool_key=tool_key,
                    skill_id=None,
                    skill_version=None,
                    manifest_watermark=manifest_watermark,
                    metadata={
                        "summary": "aggregate quality record has no representative incident in scope",
                        "linkage": "aggregate_without_incident",
                        "policy_reason": "aggregate_without_incident",
                        "recent_success_rate": _safe_number(
                            record.metadata.get("recent_success_rate")
                        ),
                    },
                )
            )
        return signals

    def _signal(
        self,
        *,
        signal_type: str,
        subject_type: str,
        subject_id: str,
        actionability: str,
        evidence_status: str,
        failure_signature: str,
        raw_backrefs: Sequence[str],
        source_ref: ResourceRef,
        tool_key: str | None,
        skill_id: str | None,
        skill_version: str | None,
        manifest_watermark: int,
        missing_refs: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> QualitySignal:
        merge_key = (
            f"{subject_type}:{subject_id}:{signal_type}:"
            f"{failure_signature}:{skill_version or 'no_skill_version'}"
        )
        return QualitySignal(
            signal_id=_signal_id(merge_key, source_ref.ref_id),
            signal_type=signal_type,
            subject_type=subject_type,
            subject_id=subject_id,
            actionability=actionability,
            evidence_status=evidence_status,
            merge_key=merge_key,
            failure_signature=failure_signature,
            raw_backrefs=tuple(raw_backrefs),
            session_id=source_ref.session_id,
            task_id=source_ref.task_id,
            parent_task_id=source_ref.parent_task_id,
            agent_id=source_ref.agent_id,
            tool_key=tool_key,
            skill_id=skill_id,
            skill_version=skill_version,
            source_watermark=manifest_watermark,
            missing_refs=tuple(stable_unique(missing_refs)),
            metadata=dict(metadata or {}),
        )


def stable_failure_signature(
    signal_type: str,
    *,
    tool_event: ResourceRef | None = None,
    tool_key: str | None = None,
    skill_id: str | None = None,
    result_refs: Sequence[ResourceRef] = (),
) -> str:
    event_metadata = tool_event.metadata if tool_event is not None else {}
    resolved_tool_key = tool_key or (ref_tool_key(tool_event) if tool_event else "")
    payload = [
        signal_type,
        resolved_tool_key or "",
        skill_id or "",
        _normalized_error_type(event_metadata),
        _metadata_text(event_metadata, "permission_status"),
        _metadata_text(event_metadata, "exception_class", "exception_type"),
        _normalized_result_shape(result_refs),
    ]
    return _digest(payload)


def _summary_metadata(
    tool_event: ResourceRef,
    *,
    result_refs: Sequence[ResourceRef],
    incident_refs: Sequence[ResourceRef],
    linkage: str,
) -> dict[str, Any]:
    metadata = tool_event.metadata
    tool_key = ref_tool_key(tool_event)
    error_type = _normalized_error_type(metadata)
    result_shape = _normalized_result_shape(result_refs)
    summary_parts = [
        f"tool={tool_key or 'unknown'}",
        f"status={str(metadata.get('status') or 'unknown').lower()}",
    ]
    if error_type:
        summary_parts.append(f"error_type={error_type}")
    return _without_none(
        {
            "summary": " ".join(summary_parts),
            "linkage": linkage,
            "tool_use_id": tool_use_key(tool_event).tool_use_id or None,
            "error_type": error_type or None,
            "exception_class": _metadata_text(
                metadata,
                "exception_class",
                "exception_type",
            )
            or None,
            "permission_status": _metadata_text(metadata, "permission_status") or None,
            "result_shape": result_shape or None,
            "result_ref_count": len(result_refs),
            "incident_ref_count": len(incident_refs),
        }
    )


def _complete_results(refs: Sequence[ResourceRef]) -> list[ResourceRef]:
    return [
        ref
        for ref in refs
        if not bool(ref.metadata.get("missing"))
    ]


_SUCCESS_STATUSES_BY_REF_TYPE: Mapping[str, frozenset[str]] = {
    "tool_event": frozenset({"ok", "passed", "success", "succeeded"}),
    "tool_result": frozenset({"ok", "passed", "success", "succeeded"}),
    "tool_incident": frozenset(),
}


def _conflicting_evidence_reasons(
    *,
    result_refs: Sequence[ResourceRef],
    incident_refs: Sequence[ResourceRef],
) -> list[str]:
    reasons: list[str] = []
    for ref in sorted([*result_refs, *incident_refs], key=ref_sort_key):
        if _is_successish_ref(ref):
            reasons.append(f"{ref.ref_type}:{ref.ref_id}:success")
    return reasons


def _is_successish_ref(ref: ResourceRef) -> bool:
    metadata = ref.metadata
    success = metadata.get("success")
    if success is True or _truthy_text(success):
        return True
    status = str(metadata.get("status") or "").strip().lower()
    if status in _SUCCESS_STATUSES_BY_REF_TYPE.get(ref.ref_type, frozenset()):
        return True
    is_error = metadata.get("is_error")
    return is_error is False or _falsy_text(is_error)


def _is_permission_or_user_denied(ref: ResourceRef) -> bool:
    metadata = ref.metadata
    values = {
        str(metadata.get(key) or "").strip().lower()
        for key in (
            "status",
            "permission_status",
            "error_type",
            "error_bucket",
            "exception_class",
            "exception_type",
        )
    }
    values.discard("")
    if values.intersection({"blocked", "denied", "permission_denied", "rejected"}):
        return True
    text = " ".join(
        str(metadata.get(key) or "").strip().lower()
        for key in ("error_message", "message", "reason")
    )
    return "permission" in text and "denied" in text


def _truthy_text(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"}


def _falsy_text(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"0", "false", "no"}


def _skill_version(context: SkillContextCandidate) -> str | None:
    record = context.skill_record
    if record is not None:
        for key in ("skill_version", "version", "generation", "lineage_generation"):
            value = record.metadata.get(key)
            if value is not None and str(value) != "":
                return str(value)
    if context.skill_file is not None:
        return str(context.skill_file.hash or context.skill_file.ref_id)
    return None


def _normalized_error_type(metadata: Mapping[str, Any]) -> str:
    return _metadata_text(
        metadata,
        "error_type",
        "exception_type",
        "failure_mode",
        "error_bucket",
        "error_code",
        "exit_code",
    )


def _normalized_result_shape(result_refs: Sequence[ResourceRef]) -> str:
    shapes: list[str] = []
    for ref in sorted(result_refs, key=ref_sort_key):
        metadata = ref.metadata
        source = _metadata_text(metadata, "persistence_source")
        missing = "missing" if metadata.get("missing") else "present"
        length = _metadata_text(metadata, "original_length", "persisted_output_size")
        shapes.append(":".join(item for item in (source, missing, length) if item))
    return "|".join(shapes)


def _metadata_text(metadata: Mapping[str, Any], *keys: str) -> str:
    values = metadata_values(metadata, *keys)
    if not values:
        return ""
    return sorted(values)[0]


def _without_none(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _signal_id(merge_key: str, source_ref_id: str) -> str:
    return f"qsig_{_digest([merge_key, source_ref_id])}"


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _same_checkpoint_context(scope: EvidenceScope, ref: ResourceRef) -> bool:
    if scope.session_id and ref.session_id != scope.session_id:
        return False

    task_ids = {
        item
        for item in (
            scope.task_id,
            *scope.source_task_ids,
        )
        if item
    }
    if task_ids:
        ref_task_ids = {
            str(item)
            for item in (
                ref.task_id,
                ref.parent_task_id,
                ref.metadata.get("task_id"),
                ref.metadata.get("parent_task_id"),
            )
            if item
        }
        if not ref_task_ids.intersection(task_ids):
            return False

    if scope.agent_ids and ref.agent_id and ref.agent_id not in scope.agent_ids:
        return False
    return True


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def merge_signals_by_key(signals: Iterable[QualitySignal]) -> list[QualitySignal]:
    merged: dict[str, QualitySignal] = {}
    for signal in signals:
        existing = merged.get(signal.merge_key)
        if existing is None:
            merged[signal.merge_key] = signal
            continue
        dominant = choose_dominant_signal(existing, signal)
        merged[signal.merge_key] = replace(
            dominant,
            raw_backrefs=stable_unique((*existing.raw_backrefs, *signal.raw_backrefs)),
        )
    return list(merged.values())
