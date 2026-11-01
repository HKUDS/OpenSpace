"""Durable audit candidate store for evidence-backed skill evolution."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generator, Mapping

from openspace.skill_engine.evidence.types import (
    EvidenceEvent,
    EvidencePacket,
    ResourceRef,
)
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace.skill_engine.evidence.store import EvidenceStore

logger = Logger.get_logger(__name__)

_STATUSES = {"pending", "rejected", "promoted", "superseded"}
_RECURRENCES = {"single", "repeated", "user_explicit"}
_VOLATILE_TAGS = {
    "provisional_evolution_disabled",
    "admission_candidate",
    "fix_only_mode_non_fix",
    "low_confidence",
    "candidate",
}
_DDL = """
CREATE TABLE IF NOT EXISTS evolution_candidates (
    candidate_id TEXT PRIMARY KEY,
    proposed_action TEXT NOT NULL,
    status TEXT NOT NULL,
    admission_id TEXT NOT NULL,
    source_task_ids_json TEXT NOT NULL DEFAULT '[]',
    target_skill_ids_json TEXT NOT NULL DEFAULT '[]',
    decision_id TEXT NOT NULL,
    decision_snapshot_json TEXT NOT NULL DEFAULT '{}',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    similar_skill_ids_json TEXT NOT NULL DEFAULT '[]',
    recurrence TEXT NOT NULL DEFAULT 'single',
    recurrence_count INTEGER NOT NULL DEFAULT 1,
    merge_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    promoted_action_id TEXT,
    rejection_reason TEXT,
    last_recheck_result_json TEXT,
    blocked_reason TEXT,
    needed_evidence_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_candidates_status
  ON evolution_candidates(status, updated_at);

CREATE INDEX IF NOT EXISTS idx_candidates_admission
  ON evolution_candidates(admission_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_pending_merge
  ON evolution_candidates(merge_key)
  WHERE status='pending';
"""


@dataclass(frozen=True, slots=True)
class EvolutionCandidate:
    candidate_id: str
    proposed_action: str
    status: str
    admission_id: str
    source_task_ids: list[str]
    target_skill_ids: list[str]
    decision_id: str
    decision_snapshot: dict[str, Any]
    evidence_refs: list[str]
    similar_skill_ids: list[str]
    recurrence: str
    recurrence_count: int
    merge_key: str
    created_at: str
    updated_at: str
    promoted_action_id: str | None = None
    rejection_reason: str | None = None
    last_recheck_result: dict[str, Any] | None = None
    blocked_reason: str | None = None
    needed_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "EvolutionCandidate":
        return cls(
            candidate_id=str(data.get("candidate_id") or ""),
            proposed_action=str(data.get("proposed_action") or ""),
            status=_status(data.get("status")),
            admission_id=str(data.get("admission_id") or ""),
            source_task_ids=_str_list(data.get("source_task_ids")),
            target_skill_ids=_str_list(data.get("target_skill_ids")),
            decision_id=str(data.get("decision_id") or ""),
            decision_snapshot=_dict_or_empty(data.get("decision_snapshot")),
            evidence_refs=_str_list(data.get("evidence_refs")),
            similar_skill_ids=_str_list(data.get("similar_skill_ids")),
            recurrence=_recurrence(data.get("recurrence")),
            recurrence_count=max(1, _int_or_one(data.get("recurrence_count"))),
            merge_key=str(data.get("merge_key") or ""),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            promoted_action_id=_none_or_str(data.get("promoted_action_id")),
            rejection_reason=_none_or_str(data.get("rejection_reason")),
            last_recheck_result=(
                _dict_or_empty(data.get("last_recheck_result"))
                if data.get("last_recheck_result") is not None
                else None
            ),
            blocked_reason=_none_or_str(data.get("blocked_reason")),
            needed_evidence=_str_list(data.get("needed_evidence")),
        )


class EvolutionCandidateStore:
    """Long-lived audit store for proposals that admission did not commit."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        evidence_store: "EvidenceStore | None" = None,
    ) -> None:
        if evidence_store is not None:
            db_path = evidence_store.db_path
        if db_path is None:
            raise ValueError("EvolutionCandidateStore requires db_path or evidence_store")

        self.evidence_store = evidence_store
        self._db_path = Path(db_path).expanduser().resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._mu = threading.Lock()
        self._closed = False
        self._conn = self._make_connection(read_only=False)
        self._init_db()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def create_or_merge(
        self,
        decision: Any,
        admission: Any,
        packet: EvidencePacket | None = None,
        *,
        job: Any | None = None,
        reason: str | None = None,
    ) -> EvolutionCandidate:
        if packet is None:
            packet = self._load_packet_for_admission(admission)

        draft = self._candidate_from_inputs(
            decision=decision,
            admission=admission,
            packet=packet,
            job=job,
            reason=reason,
        )
        candidate = self._insert_or_merge(draft)
        self._upsert_candidate_ref(candidate, packet=packet)
        return candidate

    def load_candidate(self, candidate_id: str) -> EvolutionCandidate | None:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM evolution_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            return _row_to_candidate(row) if row is not None else None

    def find_by_admission(self, admission_id: str) -> list[EvolutionCandidate]:
        with self._reader() as conn:
            rows = conn.execute(
                """
                SELECT * FROM evolution_candidates
                WHERE admission_id=?
                ORDER BY created_at, candidate_id
                """,
                (admission_id,),
            ).fetchall()
            return [_row_to_candidate(row) for row in rows]

    def load_candidates_by_admission(
        self,
        admission_id: str,
    ) -> list[EvolutionCandidate]:
        return self.find_by_admission(admission_id)

    def list_candidates(
        self,
        status: str = "pending",
        limit: int = 100,
    ) -> list[EvolutionCandidate]:
        capped_limit = max(1, int(limit or 100))
        with self._reader() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT * FROM evolution_candidates
                    WHERE status=?
                    ORDER BY updated_at DESC, candidate_id
                    LIMIT ?
                    """,
                    (_status(status), capped_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM evolution_candidates
                    ORDER BY updated_at DESC, candidate_id
                    LIMIT ?
                    """,
                    (capped_limit,),
                ).fetchall()
            return [_row_to_candidate(row) for row in rows]

    def update_candidate_status(
        self,
        candidate_id: str,
        status: str,
        *,
        rejection_reason: str | None = None,
    ) -> EvolutionCandidate:
        normalized_status = _status(status)
        if normalized_status not in {"rejected", "superseded"}:
            raise ValueError("audit candidates can only be rejected or superseded")
        now = _utc_now()
        blocked_reason = None
        needed_evidence: list[str] = []
        with self._mu:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT * FROM evolution_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown evolution candidate: {candidate_id}")

            self._conn.execute(
                """
                UPDATE evolution_candidates
                SET status=?,
                    updated_at=?,
                    promoted_action_id=?,
                    rejection_reason=?,
                    blocked_reason=?,
                    needed_evidence_json=?
                WHERE candidate_id=?
                """,
                (
                    normalized_status,
                    now,
                    None,
                    rejection_reason if normalized_status == "rejected" else None,
                    blocked_reason,
                    _json(needed_evidence),
                    candidate_id,
                ),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM evolution_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if updated is None:
                raise RuntimeError("candidate update did not return a row")
            candidate = _row_to_candidate(updated)
        self._upsert_candidate_ref(candidate, packet=None)
        return candidate

    def reject_candidate(
        self,
        candidate_id: str,
        reason: str,
    ) -> EvolutionCandidate:
        return self.update_candidate_status(
            candidate_id,
            "rejected",
            rejection_reason=reason,
        )

    def close(self) -> None:
        with self._mu:
            if self._closed:
                return
            self._conn.commit()
            self._conn.close()
            self._closed = True

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
            _ensure_columns(
                self._conn,
                "evolution_candidates",
                {
                    "last_recheck_result_json": "TEXT",
                    "blocked_reason": "TEXT",
                    "needed_evidence_json": "TEXT NOT NULL DEFAULT '[]'",
                },
            )
            self._reconcile_recurrence_locked()
            self._conn.commit()

    def _reconcile_recurrence_locked(self) -> None:
        rows = self._conn.execute(
            "SELECT candidate_id, source_task_ids_json, recurrence, "
            "recurrence_count FROM evolution_candidates"
        ).fetchall()
        for row in rows:
            source_task_ids = _json_list(row["source_task_ids_json"])
            recurrence_count = max(1, len(set(source_task_ids)))
            recurrence = str(row["recurrence"] or "single")
            if recurrence != "user_explicit":
                recurrence = "repeated" if recurrence_count >= 2 else "single"
            if (
                recurrence_count == int(row["recurrence_count"] or 1)
                and recurrence == str(row["recurrence"] or "single")
            ):
                continue
            self._conn.execute(
                "UPDATE evolution_candidates SET recurrence=?, recurrence_count=? "
                "WHERE candidate_id=?",
                (recurrence, recurrence_count, row["candidate_id"]),
            )

    @contextmanager
    def _reader(self) -> Generator[sqlite3.Connection, None, None]:
        self._ensure_open()
        conn = self._make_connection(read_only=True)
        try:
            yield conn
        finally:
            conn.close()

    def _candidate_from_inputs(
        self,
        *,
        decision: Any,
        admission: Any,
        packet: EvidencePacket | None,
        job: Any | None,
        reason: str | None,
    ) -> EvolutionCandidate:
        decision_id = str(_attr(decision, "decision_id") or "")
        admission_id = str(_attr(admission, "admission_id") or "")
        if not decision_id:
            raise ValueError("DecisionRationale.decision_id is required")
        if not admission_id:
            raise ValueError("AdmissionResult.admission_id is required")

        proposed_action = _proposed_action(decision)
        target_skill_ids = _str_list(_attr(decision, "target_skill_ids"))
        evidence_refs = _evidence_refs(decision, admission, packet)
        source_task_ids = _source_task_ids(packet)
        if not source_task_ids:
            source_task_ids = _str_list(_attr(job, "source_task_ids"))
        decision_snapshot = _snapshot(decision)
        if packet is not None and packet.scope.session_id:
            decision_snapshot.setdefault("source_session_id", packet.scope.session_id)
        if reason:
            decision_snapshot.setdefault("candidate_reason", reason)
        similar_skill_ids = _str_list(_attr(decision, "similar_skill_ids"))
        merge_key = _merge_key(
            proposed_action=proposed_action,
            target_skill_ids=target_skill_ids,
            semantic_identity=_candidate_semantic_identity(decision),
            reason_tags=[
                *_str_list(_attr(decision, "reason_tags")),
                *_str_list(_attr(admission, "warnings")),
            ],
            packet=packet,
            evidence_refs=evidence_refs,
        )
        now = _utc_now()
        recurrence = (
            "user_explicit"
            if _is_user_explicit(decision, admission, packet, reason)
            else "single"
        )
        blocked_reason = _blocked_reason_from_inputs(
            reason=reason,
            admission=admission,
            decision=decision,
        )
        needed_evidence = _needed_evidence_from_inputs(
            reason=reason,
            admission=admission,
            decision=decision,
        )
        candidate_id = f"cand_{_digest({'merge_key': merge_key, 'admission_id': admission_id, 'nonce': uuid.uuid4().hex})[:20]}"
        return EvolutionCandidate(
            candidate_id=candidate_id,
            proposed_action=proposed_action,
            status="pending",
            admission_id=admission_id,
            source_task_ids=source_task_ids,
            target_skill_ids=target_skill_ids,
            decision_id=decision_id,
            decision_snapshot=decision_snapshot,
            evidence_refs=evidence_refs,
            similar_skill_ids=similar_skill_ids,
            recurrence=recurrence,
            recurrence_count=1,
            merge_key=merge_key,
            created_at=now,
            updated_at=now,
            blocked_reason=blocked_reason,
            needed_evidence=needed_evidence,
        )

    def _insert_or_merge(self, draft: EvolutionCandidate) -> EvolutionCandidate:
        with self._mu:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    """
                    SELECT * FROM evolution_candidates
                    WHERE merge_key=? AND status='pending'
                    LIMIT 1
                    """,
                    (draft.merge_key,),
                ).fetchone()
                if existing is None:
                    self._insert_locked(draft)
                    self._conn.commit()
                    row = self._conn.execute(
                        "SELECT * FROM evolution_candidates WHERE candidate_id=?",
                        (draft.candidate_id,),
                    ).fetchone()
                    if row is None:
                        raise RuntimeError("candidate insert did not return a row")
                    return _row_to_candidate(row)

                merged = self._merge_locked(_row_to_candidate(existing), draft)
                self._conn.commit()
                return merged
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return self._merge_after_conflict(draft)
            except Exception:
                self._conn.rollback()
                raise

    def _insert_locked(self, candidate: EvolutionCandidate) -> None:
        self._conn.execute(
            """
            INSERT INTO evolution_candidates (
                candidate_id, proposed_action, status, admission_id,
                source_task_ids_json, target_skill_ids_json, decision_id,
                decision_snapshot_json, evidence_refs_json,
                similar_skill_ids_json, recurrence, recurrence_count, merge_key,
                created_at, updated_at, promoted_action_id, rejection_reason,
                last_recheck_result_json, blocked_reason, needed_evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _candidate_row_values(candidate),
        )

    def _merge_locked(
        self,
        existing: EvolutionCandidate,
        draft: EvolutionCandidate,
    ) -> EvolutionCandidate:
        snapshot = _merged_snapshot(existing, draft)
        source_task_ids = _union(existing.source_task_ids, draft.source_task_ids)
        recurrence_count = max(1, len(source_task_ids))
        recurrence = _merged_recurrence(existing, draft, recurrence_count)
        now = _utc_now()
        merged = EvolutionCandidate(
            candidate_id=existing.candidate_id,
            proposed_action=existing.proposed_action,
            status=existing.status,
            admission_id=draft.admission_id,
            source_task_ids=source_task_ids,
            target_skill_ids=_union(existing.target_skill_ids, draft.target_skill_ids),
            decision_id=draft.decision_id,
            decision_snapshot=snapshot,
            evidence_refs=_union(existing.evidence_refs, draft.evidence_refs),
            similar_skill_ids=_union(existing.similar_skill_ids, draft.similar_skill_ids),
            recurrence=recurrence,
            recurrence_count=recurrence_count,
            merge_key=existing.merge_key,
            created_at=existing.created_at,
            updated_at=now,
            promoted_action_id=existing.promoted_action_id,
            rejection_reason=existing.rejection_reason,
            last_recheck_result=existing.last_recheck_result,
            blocked_reason=draft.blocked_reason,
            needed_evidence=draft.needed_evidence,
        )
        self._conn.execute(
            """
            UPDATE evolution_candidates
            SET admission_id=?,
                source_task_ids_json=?,
                target_skill_ids_json=?,
                decision_id=?,
                decision_snapshot_json=?,
                evidence_refs_json=?,
                similar_skill_ids_json=?,
                recurrence=?,
                recurrence_count=?,
                updated_at=?,
                promoted_action_id=?,
                rejection_reason=?,
                blocked_reason=?,
                needed_evidence_json=?
            WHERE candidate_id=?
            """,
            (
                merged.admission_id,
                _json(merged.source_task_ids),
                _json(merged.target_skill_ids),
                merged.decision_id,
                _json(merged.decision_snapshot),
                _json(merged.evidence_refs),
                _json(merged.similar_skill_ids),
                merged.recurrence,
                merged.recurrence_count,
                merged.updated_at,
                merged.promoted_action_id,
                merged.rejection_reason,
                merged.blocked_reason,
                _json(merged.needed_evidence),
                merged.candidate_id,
            ),
        )
        return merged

    def _merge_after_conflict(self, draft: EvolutionCandidate) -> EvolutionCandidate:
        with self._mu:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """
                    SELECT * FROM evolution_candidates
                    WHERE merge_key=? AND status='pending'
                    LIMIT 1
                    """,
                    (draft.merge_key,),
                ).fetchone()
                if row is None:
                    self._insert_locked(draft)
                    self._conn.commit()
                    return draft
                merged = self._merge_locked(_row_to_candidate(row), draft)
                self._conn.commit()
                return merged
            except Exception:
                self._conn.rollback()
                raise

    def _upsert_candidate_ref(
        self,
        candidate: EvolutionCandidate,
        *,
        packet: EvidencePacket | None,
    ) -> None:
        if self.evidence_store is None:
            return
        raw_backrefs = list(
            dict.fromkeys(
                [
                    f"decision:{candidate.decision_id}",
                    f"admission:{candidate.admission_id}",
                    *(
                        [f"packet:{packet.packet_id}"]
                        if packet is not None and packet.packet_id
                        else []
                    ),
                    *candidate.evidence_refs,
                ]
            )
        )
        raw_backrefs = [item for item in raw_backrefs if item]
        task_id = candidate.source_task_ids[0] if candidate.source_task_ids else None
        session_id = (
            packet.scope.session_id
            if packet is not None
            else _none_or_str(candidate.decision_snapshot.get("source_session_id"))
        )
        metadata = candidate.to_dict()
        metadata["primary_tool_keys"] = _tool_keys_from_refs(packet, candidate.evidence_refs)
        ref = ResourceRef(
            ref_id=f"candidate:{candidate.candidate_id}",
            ref_type="evolution_candidate_ref",
            session_id=session_id,
            task_id=task_id,
            producer="candidate_store",
            created_at=candidate.updated_at,
            reliability="derived",
            role="derived",
            preview=(
                f"{candidate.proposed_action} candidate {candidate.status} "
                f"recurrence={candidate.recurrence_count}"
            ),
            metadata=metadata,
            raw_backrefs=raw_backrefs,
        )
        event = EvidenceEvent.create(
            event_id=f"evt_candidate_{_digest({'candidate_id': candidate.candidate_id, 'updated_at': candidate.updated_at})}",
            event_type="evolution_candidate_persisted",
            producer="candidate_store",
            created_at=candidate.updated_at,
            session_id=ref.session_id,
            task_id=ref.task_id,
            idempotency_key=(
                f"evolution_candidate:{candidate.candidate_id}:{candidate.updated_at}"
            ),
            derived_refs=[ref],
            metadata={
                "candidate_id": candidate.candidate_id,
                "status": candidate.status,
                "admission_id": candidate.admission_id,
                "decision_id": candidate.decision_id,
            },
        )
        self.evidence_store.ingest_event(event)

    def _load_packet_for_admission(self, admission: Any) -> EvidencePacket | None:
        packet_id = str(_attr(admission, "packet_id") or "")
        if not packet_id or self.evidence_store is None:
            return None
        load_packet = getattr(self.evidence_store, "load_packet", None)
        if not callable(load_packet):
            return None
        try:
            return load_packet(packet_id)
        except Exception:
            logger.debug("Failed to load packet %s for candidate", packet_id, exc_info=True)
            return None

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("EvolutionCandidateStore is closed")


def _row_to_candidate(row: sqlite3.Row) -> EvolutionCandidate:
    return EvolutionCandidate(
        candidate_id=str(row["candidate_id"]),
        proposed_action=str(row["proposed_action"]),
        status=_status(row["status"]),
        admission_id=str(row["admission_id"]),
        source_task_ids=_json_list(row["source_task_ids_json"]),
        target_skill_ids=_json_list(row["target_skill_ids_json"]),
        decision_id=str(row["decision_id"]),
        decision_snapshot=_json_object(row["decision_snapshot_json"]),
        evidence_refs=_json_list(row["evidence_refs_json"]),
        similar_skill_ids=_json_list(row["similar_skill_ids_json"]),
        recurrence=_recurrence(row["recurrence"]),
        recurrence_count=max(1, int(row["recurrence_count"] or 1)),
        merge_key=str(row["merge_key"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        promoted_action_id=_none_or_str(row["promoted_action_id"]),
        rejection_reason=_none_or_str(row["rejection_reason"]),
        last_recheck_result=(
            _json_object(row["last_recheck_result_json"])
            if "last_recheck_result_json" in row.keys()
            and row["last_recheck_result_json"]
            else None
        ),
        blocked_reason=(
            _none_or_str(row["blocked_reason"])
            if "blocked_reason" in row.keys()
            else None
        ),
        needed_evidence=(
            _json_list(row["needed_evidence_json"])
            if "needed_evidence_json" in row.keys()
            else []
        ),
    )


def _ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: Mapping[str, str],
) -> None:
    existing = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, ddl in columns.items():
        if name in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _candidate_row_values(candidate: EvolutionCandidate) -> tuple[Any, ...]:
    return (
        candidate.candidate_id,
        candidate.proposed_action,
        candidate.status,
        candidate.admission_id,
        _json(candidate.source_task_ids),
        _json(candidate.target_skill_ids),
        candidate.decision_id,
        _json(candidate.decision_snapshot),
        _json(candidate.evidence_refs),
        _json(candidate.similar_skill_ids),
        candidate.recurrence,
        candidate.recurrence_count,
        candidate.merge_key,
        candidate.created_at,
        candidate.updated_at,
        candidate.promoted_action_id,
        candidate.rejection_reason,
        _json(candidate.last_recheck_result) if candidate.last_recheck_result else None,
        candidate.blocked_reason,
        _json(candidate.needed_evidence),
    )


def _evidence_refs(
    decision: Any,
    admission: Any,
    packet: EvidencePacket | None,
) -> list[str]:
    refs: list[str] = []
    for claim in list(_attr(decision, "evidence_claims") or []):
        refs.extend(_str_list(_attr(claim, "refs")))
    refs.extend(_str_list(_attr(admission, "required_refs_checked")))
    if not refs and packet is not None:
        refs.extend(
            ref.ref_id
            for group in packet.selected_refs.values()
            for ref in group
            if ref.ref_id
        )
    return list(dict.fromkeys(refs))


def _source_task_ids(packet: EvidencePacket | None) -> list[str]:
    if packet is None:
        return []
    ids = [packet.scope.task_id or "", *packet.scope.source_task_ids]
    for group in packet.selected_refs.values():
        ids.extend(ref.task_id or "" for ref in group)
    return [item for item in dict.fromkeys(ids) if item]


def _merge_key(
    *,
    proposed_action: str,
    target_skill_ids: list[str],
    semantic_identity: str,
    reason_tags: list[str],
    packet: EvidencePacket | None,
    evidence_refs: list[str],
) -> str:
    action = _normalize_token(proposed_action).upper()
    skills = ",".join(sorted(_normalize_token(item) for item in target_skill_ids))
    tags = ",".join(
        sorted(
            tag
            for tag in (_normalize_token(item).lower() for item in reason_tags)
            if tag and not _is_volatile_tag(tag)
        )
    )
    refs = ",".join(
        f"{key}:{count}" for key, count in sorted(_ref_type_histogram(packet, evidence_refs).items())
    )
    tools = ",".join(sorted(_normalize_token(item) for item in _tool_keys_from_refs(packet, evidence_refs)))
    parts = [
        action,
        f"skills={skills}",
        f"semantic={semantic_identity}",
        f"tags={tags}",
        f"refs={refs}",
    ]
    if tools:
        parts.append(f"tool={tools}")
    return "|".join(parts)


def _candidate_semantic_identity(decision: Any) -> str:
    summary = " ".join(str(_attr(decision, "reason_summary") or "").split()).lower()
    category_path = _normalize_token(_attr(decision, "local_category_path")).lower()
    if not summary and not category_path:
        return _normalize_token(_attr(decision, "decision_id"))
    return _digest({"summary": summary, "category_path": category_path})[:20]


def _ref_type_histogram(
    packet: EvidencePacket | None,
    evidence_refs: list[str],
) -> dict[str, int]:
    histogram: dict[str, int] = {}
    if packet is None:
        return histogram
    allowed = set(evidence_refs)
    for ref in _packet_refs(packet):
        if allowed and ref.ref_id not in allowed:
            continue
        histogram[ref.ref_type] = histogram.get(ref.ref_type, 0) + 1
    return histogram


def _tool_keys_from_refs(
    packet: EvidencePacket | None,
    evidence_refs: list[str],
) -> list[str]:
    if packet is None:
        return []
    allowed = set(evidence_refs)
    keys: list[str] = []
    for ref in _packet_refs(packet):
        if allowed and ref.ref_id not in allowed:
            continue
        for metadata_field in (
            "tool_key",
            "affected_tool_key",
            "tool_keys",
            "critical_tools",
        ):
            keys.extend(_str_list(ref.metadata.get(metadata_field)))
    return list(dict.fromkeys(item for item in keys if item))


def _packet_refs(packet: EvidencePacket) -> list[ResourceRef]:
    return [
        ref
        for group in packet.selected_refs.values()
        for ref in group
        if ref.ref_id
    ]


def _merged_snapshot(
    existing: EvolutionCandidate,
    draft: EvolutionCandidate,
) -> dict[str, Any]:
    snapshot = dict(draft.decision_snapshot)
    previous_decision_ids = _union(
        _snapshot_list(existing.decision_snapshot, "previous_decision_ids"),
        [existing.decision_id],
        _snapshot_list(existing.decision_snapshot, "decision_ids"),
    )
    decision_ids = _union(previous_decision_ids, [draft.decision_id])
    admission_ids = _union(
        _snapshot_list(existing.decision_snapshot, "admission_ids"),
        [existing.admission_id, draft.admission_id],
    )
    snapshot["previous_decision_ids"] = previous_decision_ids
    snapshot["decision_ids"] = decision_ids
    snapshot["admission_ids"] = admission_ids
    snapshot["last_merged_at"] = _utc_now()
    return snapshot


def _snapshot_list(snapshot: Mapping[str, Any], key: str) -> list[str]:
    return _str_list(snapshot.get(key))


def _merged_recurrence(
    existing: EvolutionCandidate,
    draft: EvolutionCandidate,
    recurrence_count: int,
) -> str:
    if existing.recurrence == "user_explicit" or draft.recurrence == "user_explicit":
        return "user_explicit"
    return "repeated" if recurrence_count >= 2 else "single"


def _is_user_explicit(
    decision: Any,
    admission: Any,
    packet: EvidencePacket | None,
    reason: str | None,
) -> bool:
    if packet is not None and packet.selected_refs.get("manual_request_ref"):
        return True
    if str(_attr(decision, "recurrence") or "").strip().lower() == "user_explicit":
        return True
    structured_values = {
        str(reason or "").strip().lower(),
        str(_attr(decision, "candidate_policy") or "").strip().lower(),
        *(
            str(tag).strip().lower()
            for tag in _str_list(_attr(decision, "reason_tags"))
        ),
        *(
            str(tag).strip().lower()
            for tag in _str_list(_attr(admission, "warnings"))
        ),
    }
    return bool(
        structured_values
        & {"manual", "user_explicit", "user_requested", "capture_requested"}
    )


def _snapshot(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        data = value.to_dict()
        return dict(data) if isinstance(data, Mapping) else {}
    if is_dataclass(value):
        data = asdict(value)
        return dict(data) if isinstance(data, Mapping) else {}
    if isinstance(value, Mapping):
        return dict(value)
    result: dict[str, Any] = {}
    for key in (
        "decision_id",
        "trigger_job_id",
        "proposed_action",
        "candidate_policy",
        "target_skill_ids",
        "reason_summary",
        "reason_tags",
        "confidence",
        "risks",
        "source_analysis_id",
        "noop_reason",
        "analyzed_by",
        "created_at",
    ):
        if hasattr(value, key):
            result[key] = getattr(value, key)
    return result


def _attr(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _blocked_reason_from_inputs(
    *,
    reason: str | None,
    admission: Any,
    decision: Any,
) -> str | None:
    reason_text = str(reason or "").strip()
    if reason_text == "fix_only_mode_non_fix":
        return "policy_blocked:fix_only_non_fix"
    if reason_text == "semantic_validation_failed":
        return "validation_failed:semantic"
    if reason_text == "validation_failed":
        return "validation_failed:deterministic"
    if reason_text == "missing_behavior_eval":
        return "validation_failed:missing_behavior_eval"
    if reason_text.startswith("behavior_eval_failed:"):
        return reason_text
    if reason_text == "behavior_eval_failed":
        return "behavior_eval_failed"
    outcome = str(_attr(admission, "outcome") or "").strip().lower()
    failures = _str_list(_attr(admission, "hard_failures"))
    warnings = _str_list(_attr(admission, "warnings"))
    risks = _str_list(_attr(decision, "risks"))
    if failures:
        return f"admission_blocked:{failures[0]}"
    if outcome == "candidate":
        return _candidate_warning_reason(warnings or risks) or "admission_candidate"
    if outcome in {"needs_human_review", "human_review"}:
        return "needs_human_review"
    return None


def _candidate_warning_reason(tags: list[str]) -> str | None:
    lowered = {str(tag).lower() for tag in tags}
    if "provisional_evolution_disabled" in lowered:
        return "policy_blocked:provisional_evolution_disabled"
    if "no_derived_divergence" in lowered:
        return "needs_more_evidence:derived_divergence"
    if "reusable_boundary_uncertain" in lowered:
        return "needs_more_evidence:reusable_boundary"
    if "workflow_trivial_or_uncertain" in lowered:
        return "needs_more_evidence:workflow_significance"
    if "low_signal_capture" in lowered:
        return "needs_more_evidence:stronger_capture_signal"
    if "fallback_only_capture_evidence" in lowered:
        return "needs_more_evidence:primary_execution_evidence"
    if "existing_skill_covers_workflow" in lowered:
        return "blocked_by_existing_skill"
    if "ephemeral_or_secret_dependent_capture" in lowered:
        return "blocked_by_ephemeral_or_secret_dependency"
    if tags:
        return f"admission_candidate:{_normalize_token(tags[0])}"
    return None


def _needed_evidence_from_inputs(
    *,
    reason: str | None,
    admission: Any,
    decision: Any,
) -> list[str]:
    reason_text = str(reason or "").strip()
    if reason_text == "fix_only_mode_non_fix":
        return []
    if reason_text == "semantic_validation_failed":
        return ["narrower_source_supported_capability_or_artifact_repair"]
    if reason_text == "validation_failed":
        return ["deterministic_validation_repair"]
    if reason_text == "missing_behavior_eval":
        return ["behavior_eval_result"]
    if reason_text.startswith("behavior_eval_failed:") or reason_text == "behavior_eval_failed":
        return _behavior_eval_needed_evidence(reason_text)
    tags = [
        *_str_list(_attr(admission, "hard_failures")),
        *_str_list(_attr(admission, "warnings")),
        *_str_list(_attr(decision, "risks")),
    ]
    needed: list[str] = []
    for tag in tags:
        text = str(tag).strip()
        lower = text.lower()
        if lower == "no_derived_divergence":
            needed.append("derived_divergence_evidence")
        elif lower in {"reusable_boundary_uncertain", "workflow_trivial_or_uncertain"}:
            needed.append("reusable_workflow_boundary_evidence")
        elif lower == "low_signal_capture":
            needed.append("stronger_successful_workflow_evidence")
        elif lower == "fallback_only_capture_evidence":
            needed.append("primary_runtime_or_transcript_evidence")
        elif lower.startswith("missing_ref:"):
            needed.append(text)
        elif lower.startswith("missing_"):
            needed.append(text)
    return list(dict.fromkeys(needed))


def _behavior_eval_needed_evidence(reason: str) -> list[str]:
    lower = str(reason or "").strip().lower()
    needed: list[str] = []
    if "missing_executable_eval_evidence" in lower:
        needed.append("executable_eval_evidence")
    if "missing_executable_eval_cases" in lower:
        needed.append("executable_eval_cases")
    if "replay_tasks_require_external_runner" in lower:
        needed.append("external_replay_runner")
    if "missing_required_replay_runner" in lower:
        needed.append("replay_runner")
    if "routing_" in lower:
        needed.append("routing_eval_repair")
    if "deterministic_assertion_failed" in lower:
        needed.append("deterministic_assertion_repair")
    if "candidate_score_regressed" in lower:
        needed.append("non_regressing_replay_score")
    if not needed:
        needed.append("behavior_eval_resolution")
    return list(dict.fromkeys(needed))


def _proposed_action(decision: Any) -> str:
    raw = (
        _attr(decision, "proposed_action")
        or _attr(decision, "action_type")
        or _attr(decision, "evolution_type")
        or ""
    )
    raw = getattr(raw, "value", raw)
    text = str(raw or "").strip()
    return text.upper() if text else "UNKNOWN"


def _status(value: Any) -> str:
    status = str(value or "pending").strip().lower()
    if status not in _STATUSES:
        raise ValueError(f"Unsupported evolution candidate status: {value}")
    return status


def _recurrence(value: Any) -> str:
    recurrence = str(value or "single").strip().lower()
    return recurrence if recurrence in _RECURRENCES else "single"


def _normalize_token(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^A-Za-z0-9:_.-]+", "", text)
    return text


def _is_volatile_tag(tag: str) -> bool:
    if tag in _VOLATILE_TAGS:
        return True
    if tag.startswith(("task:", "session:", "packet:", "decision:", "admission:", "job:")):
        return True
    return bool(re.search(r"[0-9a-f]{8,}", tag))


def _union(*groups: list[str]) -> list[str]:
    items: list[str] = []
    for group in groups:
        items.extend(_str_list(group))
    return list(dict.fromkeys(items))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_object(value: Any) -> dict[str, Any]:
    try:
        loaded = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return dict(loaded) if isinstance(loaded, Mapping) else {}


def _json_list(value: Any) -> list[str]:
    try:
        loaded = json.loads(str(value or "[]"))
    except Exception:
        return []
    return _str_list(loaded)


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return []


def _int_or_one(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _none_or_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
