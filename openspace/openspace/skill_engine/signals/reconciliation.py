"""Reconciliation worker for quality signal evidence and trigger jobs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from openspace.skill_engine.evidence import (
    EvidenceEvent,
    EvidenceScope,
    EvidenceStore,
    ResourceRef,
)

from .detector import (
    CHECKPOINT_TASK_SESSION_PERSISTED,
    QualitySignalDetector,
    stable_failure_signature,
)
from .store import QualitySignalStore
from .types import (
    ACTION_OBSERVE_ONLY,
    SIGNAL_AGGREGATE_WITHOUT_INCIDENT,
    STATUS_AGGREGATE_ONLY,
    QualitySignal,
    choose_dominant_signal,
    stable_unique,
)


@dataclass(frozen=True, slots=True)
class QualitySignalReconciliationResult:
    scanned_ref_count: int
    created_signal_count: int
    updated_signal_count: int
    created_job_count: int
    skipped_job_count: int
    metric_window_ref: str | None


class QualitySignalReconciler:
    def __init__(
        self,
        evidence_store: EvidenceStore,
        *,
        signal_store: QualitySignalStore | None = None,
        trigger_engine: Any | None = None,
        detector: QualitySignalDetector | None = None,
        enabled: bool = True,
    ) -> None:
        self.evidence_store = evidence_store
        self.signal_store = signal_store or QualitySignalStore(evidence_store)
        self.trigger_engine = trigger_engine
        self.detector = detector or QualitySignalDetector(evidence_store)
        self.enabled = enabled

    def scan_window(
        self,
        *,
        since_watermark: int,
        until_watermark: int,
    ) -> QualitySignalReconciliationResult:
        since = max(0, int(since_watermark))
        until = max(since, int(until_watermark))
        scanned_ref_count = 0
        created_signal_count = 0
        updated_signal_count = 0
        created_job_count = 0
        skipped_job_count = 0
        status = "success"
        error: str | None = None

        try:
            if not self.enabled:
                status = "disabled"
            else:
                window_refs = self._window_refs(since, until)
                scanned_ref_count = len(window_refs)
                signals = self._scan_groups(window_refs, until)
                signal_refs: list[ResourceRef] = []
                write_watermark = self.evidence_store.latest_manifest_watermark()
                for signal in signals:
                    existing = self.signal_store.load_by_merge_key(signal.merge_key)
                    result = self.signal_store.upsert_signal(signal)
                    signal_refs.extend(result.refs)
                    write_watermark = max(write_watermark, result.write_watermark)
                    if existing is None:
                        created_signal_count += 1
                    else:
                        updated_signal_count += 1

                trigger_refs = _dedupe_refs(
                    [
                        *signal_refs,
                        *self._triggerable_refs(since, until),
                    ]
                )
                jobs = self._create_jobs(
                    trigger_refs,
                    manifest_watermark=max(until, write_watermark),
                )
                created_job_count = len(jobs)
                skipped_job_count = max(0, len(trigger_refs) - created_job_count)
        except Exception as exc:
            status = "failed"
            error = str(exc)

        metric_ref = self._write_metric_window(
            since_watermark=since,
            until_watermark=until,
            status=status,
            error=error,
            scanned_ref_count=scanned_ref_count,
            created_signal_count=created_signal_count,
            updated_signal_count=updated_signal_count,
            created_job_count=created_job_count,
            skipped_job_count=skipped_job_count,
        )
        return QualitySignalReconciliationResult(
            scanned_ref_count=scanned_ref_count,
            created_signal_count=created_signal_count,
            updated_signal_count=updated_signal_count,
            created_job_count=created_job_count,
            skipped_job_count=skipped_job_count,
            metric_window_ref=metric_ref,
        )

    def backfill_window(
        self,
        *,
        since_watermark: int,
        until_watermark: int,
        backfill_version: str = "v1",
    ) -> QualitySignalReconciliationResult:
        since = max(0, int(since_watermark))
        until = max(since, int(until_watermark))
        scanned_ref_count = 0
        created_signal_count = 0
        updated_signal_count = 0
        status = "success"
        error: str | None = None

        try:
            if not self.enabled:
                status = "disabled"
            else:
                window_refs = self._window_refs(since, until)
                scanned_ref_count = len(window_refs)
                signals = self._backfill_signals(
                    window_refs,
                    until,
                    backfill_version=backfill_version,
                )
                for signal in signals:
                    existing = self.signal_store.load_by_merge_key(signal.merge_key)
                    self.signal_store.upsert_signal(signal)
                    if existing is None:
                        created_signal_count += 1
                    else:
                        updated_signal_count += 1
        except Exception as exc:
            status = "failed"
            error = str(exc)

        metric_ref = self._write_metric_window(
            since_watermark=since,
            until_watermark=until,
            status=status,
            error=error,
            scanned_ref_count=scanned_ref_count,
            created_signal_count=created_signal_count,
            updated_signal_count=updated_signal_count,
            created_job_count=0,
            skipped_job_count=0,
            window_type="quality_signal_backfill",
            backfill_version=str(backfill_version or "v1"),
        )
        return QualitySignalReconciliationResult(
            scanned_ref_count=scanned_ref_count,
            created_signal_count=created_signal_count,
            updated_signal_count=updated_signal_count,
            created_job_count=0,
            skipped_job_count=0,
            metric_window_ref=metric_ref,
        )

    def _window_refs(self, since: int, until: int) -> list[ResourceRef]:
        refs = self.evidence_store.query_refs(
            EvidenceScope(),
            ref_types=list(_WINDOW_REF_TYPES),
            watermark=until,
        )
        return [
            ref
            for ref in refs
            if _ref_in_window(ref, since=since, until=until)
        ]

    def _scan_groups(
        self,
        refs: list[ResourceRef],
        until_watermark: int,
    ) -> list[QualitySignal]:
        groups = sorted(
            {
                (ref.session_id, ref.task_id)
                for ref in refs
                if ref.session_id and ref.task_id
            }
        )
        by_merge_key: dict[str, QualitySignal] = {}
        for session_id, task_id in groups:
            for signal in self.detector.scan_checkpoint(
                checkpoint_name=CHECKPOINT_TASK_SESSION_PERSISTED,
                scope=EvidenceScope(session_id=session_id, task_id=task_id),
                manifest_watermark=until_watermark,
            ):
                by_merge_key[signal.merge_key] = _dominant_signal(
                    by_merge_key.get(signal.merge_key),
                    signal,
                )
        return list(by_merge_key.values())

    def _backfill_signals(
        self,
        refs: list[ResourceRef],
        until_watermark: int,
        *,
        backfill_version: str,
    ) -> list[QualitySignal]:
        by_merge_key = {
            signal.merge_key: signal
            for signal in self._scan_groups(refs, until_watermark)
        }
        for signal in _aggregate_backfill_signals(
            refs,
            until_watermark=until_watermark,
            backfill_version=backfill_version,
        ):
            by_merge_key[signal.merge_key] = _dominant_signal(
                by_merge_key.get(signal.merge_key),
                signal,
            )
        return list(by_merge_key.values())

    def _create_jobs(
        self,
        refs: list[ResourceRef],
        *,
        manifest_watermark: int,
    ) -> list[Any]:
        if not refs or self.trigger_engine is None:
            return []
        from_quality_signals = getattr(self.trigger_engine, "from_quality_signals", None)
        if not callable(from_quality_signals):
            return []
        return list(
            from_quality_signals(
                refs,
                manifest_watermark=manifest_watermark,
            )
            or []
        )

    def _triggerable_refs(self, since: int, until: int) -> list[ResourceRef]:
        refs = self.signal_store.list_triggerable_since(since)
        return [
            ref
            for ref in refs
            if _ref_write_watermark(ref) <= until
        ]

    def _write_metric_window(
        self,
        *,
        since_watermark: int,
        until_watermark: int,
        status: str,
        error: str | None,
        scanned_ref_count: int,
        created_signal_count: int,
        updated_signal_count: int,
        created_job_count: int,
        skipped_job_count: int,
        window_type: str = "quality_signal_reconciliation",
        backfill_version: str | None = None,
    ) -> str | None:
        created_at = _utc_now()
        metadata = {
            "window_type": window_type,
            "since_watermark": since_watermark,
            "until_watermark": until_watermark,
            "status": status,
            "error": error,
            "scanned_ref_count": scanned_ref_count,
            "created_signal_count": created_signal_count,
            "updated_signal_count": updated_signal_count,
            "created_job_count": created_job_count,
            "skipped_job_count": skipped_job_count,
        }
        if backfill_version is not None:
            metadata["backfill_version"] = backfill_version
        digest = _digest({"created_at": created_at, **metadata})
        ref = ResourceRef(
            ref_id=f"metric_window:{window_type}:{digest}",
            ref_type="metric_window_ref",
            producer="quality_signal_reconciler",
            created_at=created_at,
            reliability="derived",
            role="supporting",
            preview=(
                f"{window_type} "
                f"{since_watermark}..{until_watermark} status={status}"
            ),
            metadata=metadata,
        )
        event = EvidenceEvent.create(
            event_id=f"evt_{window_type}_{digest}",
            event_type=f"{window_type}_window",
            producer="quality_signal_reconciler",
            created_at=created_at,
            idempotency_key=f"{window_type}:{digest}",
            supporting_refs=[ref],
            metadata=metadata,
        )
        try:
            self.evidence_store.ingest_event(event)
        except Exception:
            return None
        return ref.ref_id


_WINDOW_REF_TYPES: tuple[str, ...] = (
    "tool_event",
    "tool_result",
    "tool_incident",
    "tool_quality_record",
    "skill_event",
    "skill_record",
    "skill_file",
    "execution_analysis",
    "transcript_message",
)


def _ref_in_window(ref: ResourceRef, *, since: int, until: int) -> bool:
    first = int(ref.first_seen_watermark or 0)
    last = int(ref.last_seen_watermark or first)
    return since < first <= until or since < last <= until


def _ref_write_watermark(ref: ResourceRef) -> int:
    return int(ref.last_seen_watermark or ref.first_seen_watermark or 0)


def _dedupe_refs(refs: list[ResourceRef]) -> list[ResourceRef]:
    seen: set[str] = set()
    result: list[ResourceRef] = []
    for ref in refs:
        if not ref.ref_id or ref.ref_id in seen:
            continue
        seen.add(ref.ref_id)
        result.append(ref)
    return result


def _aggregate_backfill_signals(
    refs: list[ResourceRef],
    *,
    until_watermark: int,
    backfill_version: str,
) -> list[QualitySignal]:
    quality_refs = [
        ref for ref in refs if ref.ref_type == "tool_quality_record"
    ]
    signals: list[QualitySignal] = []
    for ref in quality_refs:
        tool_key = _tool_key_from_quality_ref(ref)
        if not tool_key:
            continue
        if _has_representative_tool_evidence(refs, tool_key):
            continue
        failure_signature = stable_failure_signature(
            SIGNAL_AGGREGATE_WITHOUT_INCIDENT,
            tool_key=tool_key,
        )
        merge_key = (
            "tool:"
            f"{tool_key}:{SIGNAL_AGGREGATE_WITHOUT_INCIDENT}:"
            f"{failure_signature}:no_skill_version"
        )
        signals.append(
            QualitySignal(
                signal_id=f"qsig_backfill_{_digest([merge_key, ref.ref_id])}",
                signal_type=SIGNAL_AGGREGATE_WITHOUT_INCIDENT,
                subject_type="tool",
                subject_id=tool_key,
                actionability=ACTION_OBSERVE_ONLY,
                evidence_status=STATUS_AGGREGATE_ONLY,
                merge_key=merge_key,
                failure_signature=failure_signature,
                raw_backrefs=tuple(stable_unique((ref.ref_id,))),
                session_id=ref.session_id,
                task_id=ref.task_id,
                parent_task_id=ref.parent_task_id,
                agent_id=ref.agent_id,
                tool_key=tool_key,
                skill_id=None,
                skill_version=None,
                source_watermark=until_watermark,
                missing_refs=("tool_incident",),
                metadata={
                    "summary": (
                        "backfilled aggregate quality record has no "
                        "representative incident"
                    ),
                    "linkage": "aggregate_without_incident",
                    "window_type": "quality_signal_backfill",
                    "backfill_version": str(backfill_version or "v1"),
                },
            )
        )
    return signals


def _dominant_signal(
    existing: QualitySignal | None,
    incoming: QualitySignal,
) -> QualitySignal:
    if existing is None:
        return incoming
    return choose_dominant_signal(existing, incoming)


def _tool_key_from_quality_ref(ref: ResourceRef) -> str:
    metadata = ref.metadata
    tool_key = str(metadata.get("tool_key") or "").strip()
    if tool_key:
        return tool_key
    ref_id = str(ref.ref_id or "")
    prefix = "tool_quality_record:"
    if not ref_id.startswith(prefix):
        return ""
    remainder = ref_id.removeprefix(prefix)
    parts = remainder.split(":")
    if len(parts) >= 3:
        return ":".join(parts[:3])
    return remainder


def _has_representative_tool_evidence(
    refs: list[ResourceRef],
    tool_key: str,
) -> bool:
    for ref in refs:
        if ref.ref_type not in {"tool_event", "tool_result", "tool_incident"}:
            continue
        metadata_tool_key = str(ref.metadata.get("tool_key") or "").strip()
        if metadata_tool_key == tool_key:
            return True
        if tool_key and tool_key in str(ref.ref_id or ""):
            return True
    return False


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
