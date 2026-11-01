"""Audit records and read-only audit service for skill evolution."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from openspace.skill_engine.evidence.redaction import redact_text
from openspace.skill_engine.signals.types import (
    STATUS_AGGREGATE_ONLY,
    TRIGGERABLE_EVIDENCE_STATUSES,
)

EVOLUTION_ACTION_STATUSES: frozenset[str] = frozenset(
    {
        "committing",
        "committed",
        "failed",
        "committed_reconciled",
        "failed_needs_review",
    }
)

_MAX_REF_READ_CHARS = 8_000
_DEFAULT_LIST_LIMIT = 100
_MAX_LIST_LIMIT = 500


@dataclass(frozen=True, slots=True)
class EvolutionActionRecord:
    action_id: str
    decision_id: str
    trigger_job_id: str
    authoring_id: str
    validation_id: str
    action_type: str
    commit_status: str
    skill_id: str | None
    parent_skill_ids: list[str]
    changed_files: list[str]
    evidence_refs: list[str]
    staging_dir: str
    active_target_dir: str
    backup_dir: str | None
    failure_reason: str | None
    created_at: str
    committed_at: str | None
    skill_record: Any | None = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("skill_record", None)
        return data


class EvidenceRefAccessError(RuntimeError):
    """Raised when an evidence ref exists but must not be read by the API."""

    def __init__(self, reason: str, *, status_code: int = 403) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


class EvolutionAuditService:
    """Query and rejection surface for evidence-backed evolution audit data."""

    def __init__(
        self,
        evidence_store: Any,
        skill_store: Any | None = None,
        *,
        candidate_store: Any | None = None,
    ) -> None:
        self.evidence_store = evidence_store
        self.skill_store = skill_store
        self.candidate_store = candidate_store
        self._ref_read_root_skill_ids: set[str] = set()

    def list_jobs(
        self,
        status: str | None = None,
        limit: int = _DEFAULT_LIST_LIMIT,
    ) -> list[dict[str, Any]]:
        capped_limit = _limit(limit)
        normalized_status = _none_if_all(status)
        with self._reader() as conn:
            if not _table_exists(conn, "trigger_jobs"):
                return []
            if normalized_status is None:
                rows = conn.execute(
                    """
                    SELECT * FROM trigger_jobs
                    ORDER BY created_at DESC, job_id
                    LIMIT ?
                    """,
                    (capped_limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM trigger_jobs
                    WHERE status=?
                    ORDER BY created_at DESC, job_id
                    LIMIT ?
                    """,
                    (normalized_status, capped_limit),
                ).fetchall()
            return [self._job_from_row(conn, row) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._reader() as conn:
            if not _table_exists(conn, "trigger_jobs"):
                return None
            row = conn.execute(
                "SELECT * FROM trigger_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            return self._job_from_row(conn, row) if row is not None else None

    def get_packet(self, packet_id: str) -> dict[str, Any] | None:
        load_packet = getattr(self.evidence_store, "load_packet", None)
        if callable(load_packet):
            packet = load_packet(packet_id)
            if packet is not None:
                return packet.to_dict()
        with self._reader() as conn:
            if not _table_exists(conn, "evidence_packets"):
                return None
            row = conn.execute(
                "SELECT packet_json FROM evidence_packets WHERE packet_id=?",
                (packet_id,),
            ).fetchone()
            if row is None:
                return None
            data = _json_object(row["packet_json"])
            return data or None

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self._reader() as conn:
            if not _table_exists(conn, "decision_rationales"):
                return None
            row = conn.execute(
                "SELECT * FROM decision_rationales WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if row is None:
                return None
            decision = _decision_from_row(conn, row)
            decision["admission"] = _latest_admission_for_decision(conn, decision_id)
            decision["candidate_ids"] = _ids(
                conn,
                "evolution_candidates",
                "candidate_id",
                "decision_id=?",
                (decision_id,),
            )
            decision["action_ids"] = _ids(
                conn,
                "evolution_actions",
                "action_id",
                "decision_id=?",
                (decision_id,),
            )
            return decision

    def list_candidates(
        self,
        status: str = "pending",
        limit: int = _DEFAULT_LIST_LIMIT,
    ) -> list[dict[str, Any]]:
        normalized_status = _none_if_all(status)
        store = self._candidate_store()
        candidates = store.list_candidates(
            status=normalized_status or "",
            limit=_limit(limit),
        )
        return [_to_dict(candidate) for candidate in candidates]

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        store = self._candidate_store()
        candidate = store.load_candidate(candidate_id)
        return _to_dict(candidate) if candidate is not None else None

    def list_review_items(self, limit: int = _DEFAULT_LIST_LIMIT) -> list[dict[str, Any]]:
        """Return the human-facing evolution review queue.

        Candidate, admission, and validation rows are inspect-only. Rejecting an
        audit candidate is a separate terminal operation and never re-enters the
        evolution engine.
        """

        capped_limit = _limit(limit)
        items: list[dict[str, Any]] = []
        try:
            pending_candidates = self.list_candidates(status="pending", limit=capped_limit)
        except Exception:
            pending_candidates = []
        for candidate in pending_candidates:
            candidate_id = str(candidate.get("candidate_id") or "")
            items.append(
                {
                    "item_id": f"candidate:{candidate_id}",
                    "item_type": "candidate",
                    "status": str(candidate.get("status") or "pending"),
                    "title": str(candidate.get("proposed_action") or "Candidate review"),
                    "summary": _candidate_review_summary(candidate),
                    "created_at": str(candidate.get("created_at") or ""),
                    "updated_at": str(candidate.get("updated_at") or candidate.get("created_at") or ""),
                    "candidate_id": candidate_id,
                    "decision_id": str(candidate.get("decision_id") or ""),
                    "admission_id": str(candidate.get("admission_id") or ""),
                    "packet_id": "",
                    "validation_id": "",
                    "action_kind": "inspect",
                    "approval_available": False,
                    "blocking_stage": "candidate",
                    "review_note": (
                        "Audit-only candidate. It can be inspected or rejected, "
                        "but it never auto-promotes into a skill."
                    ),
                }
            )

        with self._reader() as conn:
            if _table_exists(conn, "admission_results"):
                rows = conn.execute(
                    """
                    SELECT * FROM admission_results
                    WHERE outcome IN ('needs_human_review', 'human_review')
                    ORDER BY created_at DESC, admission_id
                    LIMIT ?
                    """,
                    (capped_limit,),
                ).fetchall()
                for row in rows:
                    admission_id = str(row["admission_id"] or "")
                    failures = _json_list(row["hard_failures_json"])
                    warnings = _json_list(row["warnings_json"])
                    items.append(
                        {
                            "item_id": f"admission:{admission_id}",
                            "item_type": "admission",
                            "status": str(row["outcome"] or ""),
                            "title": "Admission needs review",
                            "summary": _review_summary(failures, warnings),
                            "created_at": str(row["created_at"] or ""),
                            "updated_at": str(row["created_at"] or ""),
                            "candidate_id": "",
                            "decision_id": str(row["decision_id"] or ""),
                            "admission_id": admission_id,
                            "packet_id": str(row["packet_id"] or ""),
                            "validation_id": "",
                            "action_kind": "inspect",
                            "approval_available": False,
                            "blocking_stage": "admission",
                            "review_note": (
                                "Admission human review is inspect-only in the "
                                "dashboard. It cannot be overridden to direct; "
                                "add evidence or issue an explicit manual evolve "
                                "request to produce a new job."
                            ),
                        }
                    )

            if _table_exists(conn, "validation_results"):
                rows = conn.execute(
                    """
                    SELECT * FROM validation_results
                    WHERE outcome IN ('needs_human_review', 'human_review')
                    ORDER BY checked_at DESC, validation_id
                    LIMIT ?
                    """,
                    (capped_limit,),
                ).fetchall()
                for row in rows:
                    validation_id = str(row["validation_id"] or "")
                    failures = _json_list(row["deterministic_failures_json"])
                    warnings = _json_list(row["semantic_warnings_json"])
                    items.append(
                        {
                            "item_id": f"validation:{validation_id}",
                            "item_type": "validation",
                            "status": str(row["outcome"] or ""),
                            "title": "Validation needs review",
                            "summary": _review_summary(failures, warnings),
                            "created_at": str(row["checked_at"] or ""),
                            "updated_at": str(row["checked_at"] or ""),
                            "candidate_id": "",
                            "decision_id": str(row["decision_id"] or ""),
                            "admission_id": "",
                            "packet_id": str(row["packet_id"] or ""),
                            "validation_id": validation_id,
                            "action_kind": "inspect",
                            "approval_available": False,
                            "blocking_stage": "validation",
                            "review_note": (
                                "Validation human review is inspect-only in the "
                                "dashboard. It cannot override validator warnings "
                                "or commit staged edits; resolve the warning or "
                                "rerun through a deliberate manual evolution path."
                            ),
                        }
                    )

        items.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return items[:capped_limit]

    def list_quality_signals(
        self,
        *,
        subject_type: str | None = None,
        subject_id: str | None = None,
        actionability: str | None = None,
        not_triggerable: bool = False,
        limit: int = _DEFAULT_LIST_LIMIT,
    ) -> list[dict[str, Any]]:
        capped_limit = _limit(limit)
        with self._reader() as conn:
            if not _table_exists(conn, "quality_signal_index"):
                return []
            clauses: list[str] = []
            params: list[Any] = []
            if subject_type:
                clauses.append("subject_type=?")
                params.append(subject_type)
            if subject_id:
                clauses.append("subject_id=?")
                params.append(subject_id)
            if actionability:
                clauses.append("actionability=?")
                params.append(actionability)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            order_by = """
                ORDER BY COALESCE(signal_write_watermark, source_watermark, 0) DESC,
                         updated_at DESC,
                         signal_id
            """
            jobs = _quality_signal_jobs_by_signal_ref(conn)
            if not_triggerable:
                items: list[dict[str, Any]] = []
                offset = 0
                page_size = min(_MAX_LIST_LIMIT, max(capped_limit, _DEFAULT_LIST_LIMIT))
                while len(items) < capped_limit:
                    rows = conn.execute(
                        f"""
                        SELECT * FROM quality_signal_index
                        {where}
                        {order_by}
                        LIMIT ? OFFSET ?
                        """,
                        (*params, page_size, offset),
                    ).fetchall()
                    if not rows:
                        break
                    for row in rows:
                        item = self._quality_signal_audit_row(conn, row, jobs)
                        if item.get("not_triggerable_reason"):
                            items.append(item)
                            if len(items) >= capped_limit:
                                break
                    offset += len(rows)
                return items[:capped_limit]

            rows = conn.execute(
                f"""
                SELECT * FROM quality_signal_index
                {where}
                {order_by}
                LIMIT ?
                """,
                (*params, capped_limit),
            ).fetchall()
            items = [
                self._quality_signal_audit_row(conn, row, jobs)
                for row in rows
            ]
        return items[:capped_limit]

    def list_quality_signal_jobs(
        self,
        *,
        limit: int = _DEFAULT_LIST_LIMIT,
    ) -> list[dict[str, Any]]:
        capped_limit = _limit(limit)
        with self._reader() as conn:
            if not _table_exists(conn, "trigger_jobs"):
                return []
            rows = conn.execute(
                """
                SELECT * FROM trigger_jobs
                WHERE trigger_type='QUALITY_SIGNAL'
                ORDER BY created_at DESC, job_id
                LIMIT ?
                """,
                (capped_limit,),
            ).fetchall()
            items: list[dict[str, Any]] = []
            for row in rows:
                signal_ref_ids = _quality_signal_ref_ids_from_job_row(row)
                if signal_ref_ids and _table_exists(conn, "quality_signal_index"):
                    for signal_ref in signal_ref_ids:
                        signal_row = conn.execute(
                            """
                            SELECT * FROM quality_signal_index
                            WHERE ref_id=?
                            LIMIT 1
                            """,
                            (signal_ref,),
                        ).fetchone()
                        if signal_row is not None:
                            items.append(
                                self._quality_signal_audit_row(
                                    conn,
                                    signal_row,
                                    {signal_ref: [row]},
                                )
                            )
                            continue
                        items.append(_quality_signal_job_only_row(conn, row, signal_ref))
                else:
                    items.append(_quality_signal_job_only_row(conn, row, None))
        return items[:capped_limit]

    def reject_candidate(self, candidate_id: str, reason: str) -> dict[str, Any]:
        store = self._candidate_store()
        candidate = store.reject_candidate(candidate_id, reason or "manual reject")
        return _to_dict(candidate)

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        load_action = getattr(self.evidence_store, "load_action", None)
        action = load_action(action_id) if callable(load_action) else None
        if action is None:
            return None
        payload = _to_dict(action)
        load_validation = getattr(self.evidence_store, "load_validation", None)
        if callable(load_validation) and payload.get("validation_id"):
            validation = load_validation(str(payload["validation_id"]))
            payload["validation"] = _to_dict(validation) if validation is not None else None
        decision = self.get_decision(str(payload.get("decision_id") or ""))
        if decision is not None:
            payload["decision"] = decision
        failures = getattr(self.evidence_store, "list_action_failures", None)
        if callable(failures):
            payload["failures"] = failures(action_id)
        return payload

    def get_ref(
        self,
        ref_id: str,
        *,
        include_preview: bool = True,
    ) -> dict[str, Any] | None:
        get_ref = getattr(self.evidence_store, "get_ref", None)
        ref = get_ref(ref_id) if callable(get_ref) else None
        if ref is None:
            return None
        payload = ref.to_dict()
        if not include_preview or bool(payload.get("contains_secret")):
            payload["preview"] = ""
        return payload

    def read_ref(self, ref_id: str, max_chars: int = _MAX_REF_READ_CHARS) -> dict[str, Any]:
        get_ref = getattr(self.evidence_store, "get_ref", None)
        ref = get_ref(ref_id) if callable(get_ref) else None
        if ref is None:
            raise KeyError(ref_id)
        if bool(getattr(ref, "contains_secret", False)):
            raise EvidenceRefAccessError("contains_secret")

        cap = min(max(0, int(max_chars or _MAX_REF_READ_CHARS)), _MAX_REF_READ_CHARS)
        path = _path_from_uri(getattr(ref, "uri", None))
        source = "preview"
        original_length = len(getattr(ref, "preview", "") or "")
        text = getattr(ref, "preview", "") or ""
        if path is not None:
            resolved = path.expanduser()
            self._ensure_ref_read_roots(ref)
            if not self._path_read_allowed(resolved):
                raise EvidenceRefAccessError("outside_allowed_roots")
            if not resolved.is_file():
                raise EvidenceRefAccessError("missing_or_not_file", status_code=404)
            try:
                text = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise EvidenceRefAccessError(str(exc), status_code=404) from exc
            source = "file"
            original_length = len(text)

        redacted = redact_text(text)
        truncated = len(redacted) > cap
        content = redacted[:cap]
        return {
            "ref": self.get_ref(ref_id, include_preview=False),
            "ref_id": ref_id,
            "source": source,
            "content": content,
            "max_chars": cap,
            "original_length": original_length,
            "truncated": truncated,
        }

    def _reader(self) -> Any:
        reader = getattr(self.evidence_store, "_reader", None)
        if not callable(reader):
            raise RuntimeError("EvidenceStore reader is unavailable")
        return reader()

    def _candidate_store(self) -> Any:
        if self.candidate_store is not None:
            return self.candidate_store
        from openspace.skill_engine.evolution.candidates import EvolutionCandidateStore

        self.candidate_store = EvolutionCandidateStore(
            evidence_store=self.evidence_store,
        )
        return self.candidate_store

    def _path_read_allowed(self, path: Path) -> bool:
        checker = getattr(self.evidence_store, "_path_read_allowed", None)
        if callable(checker):
            try:
                return bool(checker(path))
            except Exception:
                return False
        return False

    def _ensure_ref_read_roots(self, ref: Any) -> None:
        """Allow a referenced skill file without scanning every skill at startup."""

        if self.skill_store is None:
            return
        add_root = getattr(self.evidence_store, "add_allowed_read_root", None)
        load_record = getattr(self.skill_store, "load_record", None)
        if not callable(add_root) or not callable(load_record):
            return
        for skill_id in _skill_ids_from_ref(ref):
            if skill_id in self._ref_read_root_skill_ids:
                continue
            try:
                record = load_record(skill_id)
            except Exception:
                continue
            record_path = getattr(record, "path", None)
            if not record_path:
                continue
            try:
                add_root(_skill_record_read_root(record_path))
            except Exception:
                continue
            self._ref_read_root_skill_ids.add(skill_id)

    def _job_from_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["reason_tags"] = _json_list(payload.pop("reason_tags_json", "[]"))
        payload["scope"] = _json_object(payload.pop("scope_json", "{}"))
        payload["profile_fallback"] = bool(payload.get("profile_fallback"))
        payload.update(_job_links(conn, str(payload.get("job_id") or "")))
        return payload

    def _quality_signal_audit_row(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        jobs_by_signal_ref: dict[str, list[sqlite3.Row]],
    ) -> dict[str, Any]:
        ref_id = str(row["ref_id"] or "")
        ref = self.get_ref(ref_id, include_preview=False)
        metadata = dict((ref or {}).get("metadata") or {})
        raw_backrefs = list((ref or {}).get("raw_backrefs") or [])
        job_rows = jobs_by_signal_ref.get(ref_id) or []
        job_row = job_rows[0] if job_rows else None
        admission = (
            _latest_admission_for_job(conn, str(job_row["job_id"] or ""))
            if job_row is not None
            else None
        )
        not_triggerable_reason = _quality_signal_not_triggerable_reason(
            signal_type=str(row["signal_type"] or metadata.get("signal_type") or ""),
            actionability=str(row["actionability"] or metadata.get("actionability") or ""),
            evidence_status=str(row["evidence_status"] or metadata.get("evidence_status") or ""),
            raw_backref_count=len(raw_backrefs),
            has_job=job_row is not None,
        )
        return {
            "signal_ref": ref_id,
            "signal_type": str(row["signal_type"] or metadata.get("signal_type") or ""),
            "subject_type": str(row["subject_type"] or metadata.get("subject_type") or ""),
            "subject_id": str(row["subject_id"] or metadata.get("subject_id") or ""),
            "tool_key": str(metadata.get("tool_key") or ""),
            "skill_id": str(metadata.get("skill_id") or ""),
            "actionability": str(row["actionability"] or metadata.get("actionability") or ""),
            "evidence_status": str(row["evidence_status"] or metadata.get("evidence_status") or ""),
            "merge_key": str(row["merge_key"] or metadata.get("merge_key") or ""),
            "raw_backref_count": len(raw_backrefs),
            "job_id": str(job_row["job_id"] or "") if job_row is not None else "",
            "job_status": str(job_row["status"] or "") if job_row is not None else "",
            "admission_status": str((admission or {}).get("outcome") or ""),
            "admission_hard_failures": list((admission or {}).get("hard_failures") or []),
            "admission_warnings": list((admission or {}).get("warnings") or []),
            "not_triggerable_reason": not_triggerable_reason,
        }


def _job_links(conn: sqlite3.Connection, job_id: str) -> dict[str, list[str]]:
    packet_ids = _ids(
        conn,
        "evidence_packets",
        "packet_id",
        "trigger_job_id=?",
        (job_id,),
    )
    decision_ids = _ids(
        conn,
        "decision_rationales",
        "decision_id",
        "trigger_job_id=?",
        (job_id,),
    )
    admission_ids = _ids_for_values(
        conn,
        table="admission_results",
        id_column="admission_id",
        match_column="decision_id",
        values=decision_ids,
    )
    candidate_ids = _ids_for_values(
        conn,
        table="evolution_candidates",
        id_column="candidate_id",
        match_column="decision_id",
        values=decision_ids,
    )
    action_ids = _ids(
        conn,
        "evolution_actions",
        "action_id",
        "trigger_job_id=?",
        (job_id,),
    )
    validation_ids = _ids_for_values(
        conn,
        table="validation_results",
        id_column="validation_id",
        match_column="decision_id",
        values=decision_ids,
    )
    return {
        "packet_ids": packet_ids,
        "decision_ids": decision_ids,
        "admission_ids": admission_ids,
        "candidate_ids": candidate_ids,
        "validation_ids": validation_ids,
        "action_ids": action_ids,
    }


def _quality_signal_jobs_by_signal_ref(
    conn: sqlite3.Connection,
) -> dict[str, list[sqlite3.Row]]:
    if not _table_exists(conn, "trigger_jobs"):
        return {}
    rows = conn.execute(
        """
        SELECT * FROM trigger_jobs
        WHERE trigger_type='QUALITY_SIGNAL'
        ORDER BY created_at DESC, job_id
        """
    ).fetchall()
    by_ref: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        for ref_id in _quality_signal_ref_ids_from_job_row(row):
            by_ref.setdefault(ref_id, []).append(row)
    return by_ref


def _quality_signal_ref_ids_from_job_row(row: sqlite3.Row) -> list[str]:
    scope = _json_object(row["scope_json"])
    refs = _str_values(scope.get("representative_execution_ids"))
    return [ref for ref in refs if ref.startswith("quality_signal:")]


def _latest_admission_for_job(
    conn: sqlite3.Connection,
    job_id: str,
) -> dict[str, Any] | None:
    decision_ids = _ids(
        conn,
        "decision_rationales",
        "decision_id",
        "trigger_job_id=?",
        (job_id,),
    )
    latest: dict[str, Any] | None = None
    for decision_id in decision_ids:
        admission = _latest_admission_for_decision(conn, decision_id)
        if admission is None:
            continue
        if latest is None or str(admission.get("created_at") or "") > str(latest.get("created_at") or ""):
            latest = admission
    return latest


def _quality_signal_job_only_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    signal_ref: str | None,
) -> dict[str, Any]:
    admission = _latest_admission_for_job(conn, str(row["job_id"] or ""))
    return {
        "signal_ref": signal_ref or "",
        "signal_type": str(row["reason"] or ""),
        "subject_type": "",
        "subject_id": "",
        "tool_key": "",
        "skill_id": "",
        "actionability": "",
        "evidence_status": "",
        "merge_key": "",
        "raw_backref_count": 0,
        "job_id": str(row["job_id"] or ""),
        "job_status": str(row["status"] or ""),
        "admission_status": str((admission or {}).get("outcome") or ""),
        "admission_hard_failures": list((admission or {}).get("hard_failures") or []),
        "admission_warnings": list((admission or {}).get("warnings") or []),
        "not_triggerable_reason": "",
    }


def _quality_signal_not_triggerable_reason(
    *,
    signal_type: str,
    actionability: str,
    evidence_status: str,
    raw_backref_count: int,
    has_job: bool,
) -> str:
    if (
        signal_type == "aggregate_without_incident"
        or evidence_status == STATUS_AGGREGATE_ONLY
    ):
        return "quality_signal_aggregate_only"
    if actionability != "trigger_review":
        return "quality_signal_not_trigger_review"
    if evidence_status not in TRIGGERABLE_EVIDENCE_STATUSES:
        return "quality_signal_incomplete"
    if raw_backref_count <= 0:
        return "missing_raw_backrefs"
    if not has_job:
        return "quality_signal_job_missing"
    return ""


def _decision_from_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> dict[str, Any]:
    decision_id = str(row["decision_id"])
    claims = []
    if _table_exists(conn, "decision_evidence_claims"):
        claim_rows = conn.execute(
            """
            SELECT claim, refs_json, confidence
            FROM decision_evidence_claims
            WHERE decision_id=?
            ORDER BY id
            """,
            (decision_id,),
        ).fetchall()
        claims = [
            {
                "claim": str(item["claim"] or ""),
                "refs": _json_list(item["refs_json"]),
                "confidence": str(item["confidence"] or "low"),
            }
            for item in claim_rows
        ]
    return {
        "decision_id": decision_id,
        "trigger_job_id": str(row["trigger_job_id"] or ""),
        "packet_id": str(row["packet_id"] or ""),
        "proposed_action": str(row["proposed_action"] or ""),
        "candidate_policy": str(row["candidate_policy"] or ""),
        "target_skill_ids": _json_list(row["target_skill_ids_json"]),
        "reason_summary": str(row["reason_summary"] or ""),
        "reason_tags": _json_list(row["reason_tags_json"]),
        "evidence_claims": claims,
        "confidence": float(row["confidence"] or 0.0),
        "risks": _json_list(row["risks_json"]),
        "source_analysis_id": _none_or_str(row["source_analysis_id"]),
        "noop_reason": _none_or_str(row["noop_reason"]),
        "analyzed_by": str(row["analyzed_by"] or ""),
        "created_at": str(row["created_at"] or ""),
    }


def _latest_admission_for_decision(
    conn: sqlite3.Connection,
    decision_id: str,
) -> dict[str, Any] | None:
    if not _table_exists(conn, "admission_results"):
        return None
    row = conn.execute(
        """
        SELECT * FROM admission_results
        WHERE decision_id=?
        ORDER BY created_at DESC, admission_id DESC
        LIMIT 1
        """,
        (decision_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "admission_id": str(row["admission_id"] or ""),
        "decision_id": str(row["decision_id"] or ""),
        "packet_id": str(row["packet_id"] or ""),
        "outcome": str(row["outcome"] or ""),
        "hard_failures": _json_list(row["hard_failures_json"]),
        "warnings": _json_list(row["warnings_json"]),
        "required_refs_checked": _json_list(row["required_refs_checked_json"]),
        "reviewed_by": str(row["reviewed_by"] or ""),
        "created_at": str(row["created_at"] or ""),
    }


def _ids(
    conn: sqlite3.Connection,
    table: str,
    id_column: str,
    where: str,
    params: tuple[Any, ...],
) -> list[str]:
    if not _table_exists(conn, table):
        return []
    rows = conn.execute(
        f"SELECT {id_column} FROM {table} WHERE {where} ORDER BY {id_column}",
        params,
    ).fetchall()
    return [str(row[id_column]) for row in rows if str(row[id_column])]


def _ids_for_values(
    conn: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    match_column: str,
    values: list[str],
) -> list[str]:
    if not values or not _table_exists(conn, table):
        return []
    placeholders = ",".join("?" for _ in values)
    rows = conn.execute(
        f"""
        SELECT {id_column} FROM {table}
        WHERE {match_column} IN ({placeholders})
        ORDER BY {id_column}
        """,
        tuple(values),
    ).fetchall()
    return [str(row[id_column]) for row in rows if str(row[id_column])]


def _candidate_review_summary(candidate: dict[str, Any]) -> str:
    needed = [str(item) for item in (candidate.get("needed_evidence") or []) if item]
    blocked = str(candidate.get("blocked_reason") or "").strip()
    if needed:
        return "needs evidence: " + ", ".join(needed[:3])
    if blocked:
        return blocked
    return "pending candidate review"


def _review_summary(primary: list[str], secondary: list[str]) -> str:
    values = [str(item) for item in [*primary, *secondary] if str(item or "").strip()]
    if not values:
        return "needs human review"
    return ", ".join(values[:3])


def _skill_ids_from_ref(ref: Any) -> list[str]:
    values: list[str] = []
    metadata = getattr(ref, "metadata", None)
    if isinstance(metadata, dict):
        for key in (
            "skill_id",
            "skill_ids",
            "target_skill_id",
            "target_skill_ids",
            "affected_skill_id",
            "affected_skill_ids",
            "parent_skill_id",
            "parent_skill_ids",
        ):
            values.extend(_str_values(metadata.get(key)))
    values.extend(_skill_ids_from_ref_token(getattr(ref, "ref_id", None)))
    for backref in _str_values(getattr(ref, "raw_backrefs", None)):
        values.extend(_skill_ids_from_ref_token(backref))
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        skill_id = value.strip()
        if not skill_id or skill_id in seen:
            continue
        seen.add(skill_id)
        result.append(skill_id)
    return result


def _skill_ids_from_ref_token(value: Any) -> list[str]:
    text = str(value or "")
    for prefix in ("skill_file:", "skill_record:", "skill_event:"):
        if text.startswith(prefix):
            skill_id = text.removeprefix(prefix).split(":", 1)[0].strip()
            return [skill_id] if skill_id else []
    return []


def _str_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            loaded = json.loads(value)
        except Exception:
            loaded = None
        if isinstance(loaded, list):
            return [str(item) for item in loaded if str(item)]
    return [str(value)] if str(value) else []


def _skill_record_read_root(path: Any) -> Path:
    resolved = Path(path).expanduser()
    if resolved.name == "SKILL.md" or resolved.suffix:
        return resolved.parent
    return resolved


def _path_from_uri(uri: Any) -> Path | None:
    if not uri:
        return None
    text = str(uri).split("#", 1)[0].strip()
    if not text:
        return None
    if text.startswith("file://"):
        from urllib.parse import unquote, urlparse

        parsed = urlparse(text)
        return Path(unquote(parsed.path))
    if "://" in text:
        return None
    return Path(text)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    method = getattr(value, "to_dict", None)
    if callable(method):
        return method()
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {"value": value}


def _limit(value: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = _DEFAULT_LIST_LIMIT
    return max(1, min(limit, _MAX_LIST_LIMIT))


def _none_if_all(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "all":
        return None
    return text


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        loaded = json.loads(str(value))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if not value:
        return []
    try:
        loaded = json.loads(str(value))
    except Exception:
        return []
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in loaded if str(item)]


def _none_or_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
