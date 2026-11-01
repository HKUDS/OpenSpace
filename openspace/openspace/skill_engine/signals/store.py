"""SQLite-backed quality signal index over EvidenceStore refs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Generator, Mapping, Sequence

from openspace.skill_engine.evidence import EvidenceEvent, EvidenceStore, ResourceRef

from .types import (
    ACTION_TRIGGER_REVIEW,
    ACTIONABILITIES,
    EVIDENCE_STATUSES,
    QUALITY_SIGNAL_METADATA_KEYS,
    SIGNAL_AGGREGATE_WITHOUT_INCIDENT,
    SIGNAL_TYPES,
    TRIGGERABLE_EVIDENCE_STATUSES,
    QualitySignal,
    QualitySignalWriteResult,
    choose_dominant_signal,
    quality_signal_from_ref,
    quality_signal_to_ref,
    stable_unique,
)


_DDL = """
CREATE TABLE IF NOT EXISTS quality_signal_index (
  signal_id TEXT PRIMARY KEY,
  merge_key TEXT NOT NULL UNIQUE,
  signal_type TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  actionability TEXT NOT NULL,
  evidence_status TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  source_watermark INTEGER,
  signal_write_watermark INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quality_signal_triggerable
  ON quality_signal_index(actionability, evidence_status, source_watermark);

CREATE INDEX IF NOT EXISTS idx_quality_signal_subject
  ON quality_signal_index(subject_type, subject_id);
"""

_FORBIDDEN_METADATA_KEYS = {
    "complete_result",
    "file_content",
    "full_result",
    "full_skill_file",
    "full_transcript",
    "raw_result",
    "result",
    "result_content",
    "skill_content",
    "skill_file_content",
    "stderr",
    "stdout",
    "tool_result",
    "transcript",
    "transcript_content",
}


class QualitySignalStore:
    def __init__(self, evidence_store: EvidenceStore) -> None:
        self.evidence_store = evidence_store
        self._db_path = evidence_store.db_path
        self._mu = threading.Lock()
        self._closed = False
        self._conn = self._make_connection(read_only=False)
        self._init_db()

    def close(self) -> None:
        with self._mu:
            if self._closed:
                return
            self._conn.commit()
            self._conn.close()
            self._closed = True

    def upsert_signal(self, signal: QualitySignal) -> QualitySignalWriteResult:
        with self._mu:
            self._ensure_open()
            existing = self._load_by_merge_key_locked(signal.merge_key)
            ref = self._build_ref(signal, existing=existing)
            event = _quality_signal_recorded_event(ref)

            write_watermark = self.evidence_store.ingest_event(event)
            self._upsert_index_locked(ref, signal_write_watermark=write_watermark)
            self._conn.commit()

        stored_ref = self.evidence_store.get_ref(ref.ref_id) or ref
        return QualitySignalWriteResult(refs=[stored_ref], write_watermark=write_watermark)

    def upsert_many(self, signals: Sequence[QualitySignal]) -> QualitySignalWriteResult:
        refs: list[ResourceRef] = []
        write_watermark = self.evidence_store.latest_manifest_watermark()
        for signal in signals:
            result = self.upsert_signal(signal)
            refs.extend(result.refs)
            write_watermark = max(write_watermark, result.write_watermark)
        return QualitySignalWriteResult(refs=refs, write_watermark=write_watermark)

    def load_by_merge_key(self, merge_key: str) -> ResourceRef | None:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT ref_id FROM quality_signal_index WHERE merge_key = ?",
                (merge_key,),
            ).fetchone()
        if row is None:
            return None
        return self.evidence_store.get_ref(str(row["ref_id"]))

    def list_triggerable_since(self, watermark: int) -> list[ResourceRef]:
        triggerable_statuses = tuple(sorted(TRIGGERABLE_EVIDENCE_STATUSES))
        placeholders = ",".join("?" for _ in triggerable_statuses)
        with self._reader() as conn:
            rows = conn.execute(
                f"""
                SELECT ref_id FROM quality_signal_index
                WHERE actionability = ?
                  AND evidence_status IN ({placeholders})
                  AND COALESCE(signal_write_watermark, source_watermark, 0) > ?
                ORDER BY COALESCE(signal_write_watermark, source_watermark, 0), signal_id
                """,
                (ACTION_TRIGGER_REVIEW, *triggerable_statuses, int(watermark)),
            ).fetchall()
        return self._refs_from_rows(rows)

    def list_by_subject(self, subject_type: str, subject_id: str) -> list[ResourceRef]:
        with self._reader() as conn:
            rows = conn.execute(
                """
                SELECT ref_id FROM quality_signal_index
                WHERE subject_type = ? AND subject_id = ?
                ORDER BY COALESCE(signal_write_watermark, source_watermark, 0), signal_id
                """,
                (subject_type, subject_id),
            ).fetchall()
        return self._refs_from_rows(rows)

    def _make_connection(self, *, read_only: bool) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if read_only:
            conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._mu:
            self._conn.executescript(_DDL)
            self._ensure_column_locked(
                "quality_signal_index",
                "signal_write_watermark",
                "INTEGER",
            )
            self._backfill_signal_write_watermark_locked()
            self._rebuild_triggerable_index_locked()
            self._conn.commit()

    def _ensure_column_locked(self, table: str, column: str, definition: str) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        if column in {str(row["name"]) for row in rows}:
            return
        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _backfill_signal_write_watermark_locked(self) -> None:
        self._conn.execute(
            """
            UPDATE quality_signal_index
            SET signal_write_watermark = (
                SELECT resource_refs.last_seen_watermark
                FROM resource_refs
                WHERE resource_refs.ref_id = quality_signal_index.ref_id
            )
            WHERE signal_write_watermark IS NULL
              AND EXISTS (
                SELECT 1 FROM resource_refs
                WHERE resource_refs.ref_id = quality_signal_index.ref_id
              )
            """
        )

    def _rebuild_triggerable_index_locked(self) -> None:
        self._conn.execute("DROP INDEX IF EXISTS idx_quality_signal_triggerable")
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_quality_signal_triggerable
              ON quality_signal_index(
                actionability,
                evidence_status,
                signal_write_watermark
              )
            """
        )

    @contextmanager
    def _reader(self) -> Generator[sqlite3.Connection, None, None]:
        self._ensure_open()
        conn = self._make_connection(read_only=True)
        try:
            yield conn
        finally:
            conn.close()

    def _load_by_merge_key_locked(self, merge_key: str) -> ResourceRef | None:
        row = self._conn.execute(
            "SELECT ref_id FROM quality_signal_index WHERE merge_key = ?",
            (merge_key,),
        ).fetchone()
        if row is None:
            return None
        return self.evidence_store.get_ref(str(row["ref_id"]))

    def _build_ref(
        self,
        signal: QualitySignal,
        *,
        existing: ResourceRef | None,
    ) -> ResourceRef:
        _validate_signal(signal, allow_empty_backrefs=existing is not None)
        if existing is None:
            return quality_signal_to_ref(signal)

        existing_signal = quality_signal_from_ref(existing)
        raw_backrefs = stable_unique((*existing.raw_backrefs, *signal.raw_backrefs))
        dominant = choose_dominant_signal(existing_signal, signal)
        if dominant is signal:
            metadata = dict(existing_signal.metadata)
            metadata.update(dict(signal.metadata))
            if (
                existing_signal.actionability != signal.actionability
                or existing_signal.evidence_status != signal.evidence_status
            ):
                metadata["previous_actionability"] = existing_signal.actionability
                metadata["previous_evidence_status"] = existing_signal.evidence_status
        else:
            metadata = dict(signal.metadata)
            metadata.update(dict(existing_signal.metadata))

        merged = replace(
            existing_signal,
            raw_backrefs=raw_backrefs,
            session_id=existing_signal.session_id or signal.session_id,
            task_id=existing_signal.task_id or signal.task_id,
            parent_task_id=existing_signal.parent_task_id or signal.parent_task_id,
            agent_id=existing_signal.agent_id or signal.agent_id,
            tool_key=existing_signal.tool_key or signal.tool_key,
            skill_id=existing_signal.skill_id or signal.skill_id,
            skill_version=existing_signal.skill_version or signal.skill_version,
            source_watermark=_max_optional_int(
                existing_signal.source_watermark,
                signal.source_watermark,
            ),
            actionability=dominant.actionability,
            evidence_status=dominant.evidence_status,
            missing_refs=dominant.missing_refs,
            metadata=metadata,
        )
        return quality_signal_to_ref(merged, raw_backrefs=raw_backrefs)

    def _upsert_index_locked(
        self,
        ref: ResourceRef,
        *,
        signal_write_watermark: int,
    ) -> None:
        metadata = ref.metadata
        signal_id = str(metadata.get("signal_id") or "")
        if not signal_id:
            raise ValueError("quality signal ref metadata missing signal_id")
        now = _utc_now()
        existing = self._conn.execute(
            "SELECT created_at FROM quality_signal_index WHERE signal_id = ?",
            (signal_id,),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO quality_signal_index (
                signal_id, merge_key, signal_type, subject_type, subject_id,
                actionability, evidence_status, ref_id, source_watermark,
                signal_write_watermark, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(signal_id) DO UPDATE SET
                merge_key = excluded.merge_key,
                signal_type = excluded.signal_type,
                subject_type = excluded.subject_type,
                subject_id = excluded.subject_id,
                actionability = excluded.actionability,
                evidence_status = excluded.evidence_status,
                ref_id = excluded.ref_id,
                source_watermark = excluded.source_watermark,
                signal_write_watermark = excluded.signal_write_watermark,
                updated_at = excluded.updated_at
            """,
            (
                signal_id,
                str(metadata.get("merge_key") or ""),
                str(metadata.get("signal_type") or ""),
                str(metadata.get("subject_type") or ""),
                str(metadata.get("subject_id") or ""),
                str(metadata.get("actionability") or ""),
                str(metadata.get("evidence_status") or ""),
                ref.ref_id,
                _none_or_int(metadata.get("source_watermark")),
                int(signal_write_watermark),
                str(existing["created_at"]) if existing is not None else now,
                now,
            ),
        )

    def _refs_from_rows(self, rows: Sequence[sqlite3.Row]) -> list[ResourceRef]:
        refs: list[ResourceRef] = []
        for row in rows:
            ref = self.evidence_store.get_ref(str(row["ref_id"]))
            if ref is not None and ref.ref_type == "quality_signal_ref":
                refs.append(ref)
        return refs

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("QualitySignalStore is closed")


def _quality_signal_recorded_event(ref: ResourceRef) -> EvidenceEvent:
    metadata = dict(ref.metadata)
    created_at = _utc_now()
    idempotency_payload = {
        "ref_id": ref.ref_id,
        "merge_key": metadata.get("merge_key"),
        "actionability": metadata.get("actionability"),
        "evidence_status": metadata.get("evidence_status"),
        "missing_refs": metadata.get("missing_refs"),
        "raw_backrefs": ref.raw_backrefs,
        "source_watermark": metadata.get("source_watermark"),
        "metadata": {
            key: value
            for key, value in metadata.items()
            if key not in QUALITY_SIGNAL_METADATA_KEYS
        },
    }
    return EvidenceEvent.create(
        event_id=f"evt_quality_signal_{_digest(idempotency_payload)}",
        event_type="quality_signal_recorded",
        producer="quality_signal_detector",
        created_at=created_at,
        session_id=ref.session_id,
        task_id=ref.task_id,
        parent_task_id=ref.parent_task_id,
        agent_id=ref.agent_id,
        idempotency_key=f"quality_signal:{_digest(idempotency_payload)}",
        derived_refs=[ref],
        metadata={
            "signal_id": metadata.get("signal_id"),
            "merge_key": metadata.get("merge_key"),
            "ref_id": ref.ref_id,
            "source_watermark": metadata.get("source_watermark"),
        },
    )


def _validate_signal(
    signal: QualitySignal,
    *,
    allow_empty_backrefs: bool = False,
) -> None:
    required = {
        "signal_id": signal.signal_id,
        "signal_type": signal.signal_type,
        "subject_type": signal.subject_type,
        "subject_id": signal.subject_id,
        "actionability": signal.actionability,
        "evidence_status": signal.evidence_status,
        "merge_key": signal.merge_key,
        "failure_signature": signal.failure_signature,
    }
    for field_name, value in required.items():
        if not str(value or "").strip():
            raise ValueError(f"QualitySignal.{field_name} is required")
    if signal.signal_type not in SIGNAL_TYPES:
        raise ValueError(f"Unsupported QualitySignal.signal_type: {signal.signal_type}")
    if signal.actionability not in ACTIONABILITIES:
        raise ValueError(f"Unsupported QualitySignal.actionability: {signal.actionability}")
    if signal.evidence_status not in EVIDENCE_STATUSES:
        raise ValueError(
            f"Unsupported QualitySignal.evidence_status: {signal.evidence_status}"
        )
    if (
        signal.actionability == ACTION_TRIGGER_REVIEW
        and signal.evidence_status not in TRIGGERABLE_EVIDENCE_STATUSES
    ):
        raise ValueError("trigger_review quality signals require triggerable evidence")
    if signal.signal_type == SIGNAL_AGGREGATE_WITHOUT_INCIDENT:
        if signal.actionability == ACTION_TRIGGER_REVIEW:
            raise ValueError("aggregate-only quality signals cannot trigger review")
    elif not signal.raw_backrefs and not allow_empty_backrefs:
        raise ValueError("non-aggregate quality signals require raw_backrefs")
    if signal.source_watermark is not None and signal.source_watermark < 0:
        raise ValueError("QualitySignal.source_watermark cannot be negative")
    _validate_metadata(signal.metadata)


def _validate_metadata(metadata: Mapping[str, Any]) -> None:
    for key, value in metadata.items():
        normalized = str(key).strip().lower()
        if normalized in QUALITY_SIGNAL_METADATA_KEYS:
            raise ValueError(f"QualitySignal.metadata cannot override {key}")
        if normalized in _FORBIDDEN_METADATA_KEYS:
            raise ValueError(f"QualitySignal.metadata cannot include full content key {key}")
        if isinstance(value, Mapping):
            _validate_metadata(value)


def _max_optional_int(first: int | None, second: int | None) -> int | None:
    values = [value for value in (first, second) if value is not None]
    return max(values) if values else None


def _none_or_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
