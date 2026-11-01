"""Trigger policy for derived quality signal refs."""

from __future__ import annotations

from typing import Sequence

from openspace.skill_engine.evidence import EvidenceScope, ResourceRef
from openspace.skill_engine.triggers.store import TriggerStore
from openspace.skill_engine.triggers.types import TriggerJobSpec

from .types import (
    ACTION_TRIGGER_REVIEW,
    SIGNAL_AGGREGATE_WITHOUT_INCIDENT,
    TRIGGERABLE_EVIDENCE_STATUSES,
    stable_unique,
)


class QualitySignalTriggerPolicy:
    trigger_type = "QUALITY_SIGNAL"
    _OPEN_JOB_STATUSES = {"pending", "running", "failed_retryable"}

    def __init__(self, trigger_store: TriggerStore | None = None) -> None:
        self.trigger_store = trigger_store
        self.skipped_job_count = 0
        self.skipped_existing_job_count = 0

    def from_signal_refs(
        self,
        signal_refs: Sequence[ResourceRef],
        *,
        manifest_watermark: int,
    ) -> list[TriggerJobSpec]:
        del manifest_watermark
        self.skipped_job_count = 0
        self.skipped_existing_job_count = 0
        specs: list[TriggerJobSpec] = []
        for signal_ref in signal_refs:
            spec = self._spec_for_ref(signal_ref)
            if spec is None:
                self.skipped_job_count += 1
                continue
            if self._terminal_job_already_exists(spec):
                self.skipped_existing_job_count += 1
                continue
            specs.append(spec)
        return specs

    def _spec_for_ref(self, signal_ref: ResourceRef) -> TriggerJobSpec | None:
        if signal_ref.ref_type != "quality_signal_ref":
            return None
        metadata = signal_ref.metadata
        if metadata.get("actionability") != ACTION_TRIGGER_REVIEW:
            return None
        if metadata.get("evidence_status") not in TRIGGERABLE_EVIDENCE_STATUSES:
            return None

        signal_type = str(metadata.get("signal_type") or "").strip()
        if not signal_type or signal_type == SIGNAL_AGGREGATE_WITHOUT_INCIDENT:
            return None
        if not signal_ref.raw_backrefs:
            return None

        merge_key = str(metadata.get("merge_key") or "").strip()
        if not merge_key:
            return None

        skill_id = _none_or_str(metadata.get("skill_id"))
        tool_key = _none_or_str(metadata.get("tool_key"))
        skill_version = _none_or_str(metadata.get("skill_version"))
        representative_ids = stable_unique(
            (signal_ref.ref_id, *signal_ref.raw_backrefs)
        )
        reason_tags = _reason_tags(
            subject_type=_none_or_str(metadata.get("subject_type")),
            tool_key=tool_key,
            skill_id=skill_id,
        )

        return TriggerJobSpec(
            trigger_type=self.trigger_type,
            reason=signal_type,
            reason_tags=reason_tags,
            scope=EvidenceScope(
                session_id=signal_ref.session_id,
                task_id=signal_ref.task_id,
                agent_ids=(signal_ref.agent_id,) if signal_ref.agent_id else (),
                skill_ids=(skill_id,) if skill_id else (),
                tool_keys=(tool_key,) if tool_key else (),
                representative_execution_ids=representative_ids,
                source_task_ids=tuple(
                    item
                    for item in (signal_ref.task_id, signal_ref.parent_task_id)
                    if item
                ),
                time_window=_pair_or_none(metadata.get("time_window")),
            ),
            evidence_profile="quality_signal",
            subprofile=signal_type,
            idempotency_key=(
                f"quality_signal:{merge_key}:"
                f"{skill_version or 'no_skill_version'}"
            ),
        )

    def _terminal_job_already_exists(self, spec: TriggerJobSpec) -> bool:
        if self.trigger_store is None:
            return False
        get_by_key = getattr(self.trigger_store, "get_by_idempotency_key", None)
        if not callable(get_by_key):
            return False
        job = get_by_key(spec.idempotency_key)
        if job is None:
            return False
        return str(getattr(job, "status", "") or "") not in self._OPEN_JOB_STATUSES


def _reason_tags(
    *,
    subject_type: str | None,
    tool_key: str | None,
    skill_id: str | None,
) -> list[str]:
    tags: list[str] = []
    if subject_type:
        tags.append(f"subject:{subject_type}")
    if tool_key:
        tags.append(f"tool:{tool_key}")
    if skill_id:
        tags.append(f"skill:{skill_id}")
    return tags


def _none_or_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _pair_or_none(value: object) -> tuple[str, str] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    left = _none_or_str(value[0])
    right = _none_or_str(value[1])
    if left is None or right is None:
        return None
    return (left, right)
