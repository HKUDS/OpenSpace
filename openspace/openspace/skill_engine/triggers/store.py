"""SQLite persistence for TriggerJobs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator

from openspace.skill_engine.evidence import EvidenceScope, EvidenceStore
from openspace.utils.logging import Logger

from .types import TRIGGER_STATUSES, TRIGGER_TYPES, TriggerJob, TriggerJobSpec

logger = Logger.get_logger(__name__)

_OPEN_JOB_STATUSES = ("pending", "running", "failed_retryable")
_IDEMPOTENCY_RUN_SEPARATOR = "#run:"
_RETIRED_CANDIDATE_RECHECK_REASON = (
    "candidate recheck retired; evolution candidates are audit-only"
)


_DDL = """
CREATE TABLE IF NOT EXISTS trigger_jobs (
    job_id TEXT PRIMARY KEY,
    trigger_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    reason_tags_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    manifest_watermark INTEGER NOT NULL,
    evidence_profile TEXT NOT NULL,
    subprofile TEXT NOT NULL,
    profile_fallback INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT NOT NULL UNIQUE,
    attempts INTEGER NOT NULL DEFAULT 0,
    locked_at TEXT,
    locked_by TEXT,
    created_at TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT,
    result_ref TEXT
);

CREATE INDEX IF NOT EXISTS idx_trigger_jobs_status_sched
  ON trigger_jobs(status, scheduled_at);

CREATE INDEX IF NOT EXISTS idx_trigger_jobs_scope_task
  ON trigger_jobs(trigger_type, reason, status);

CREATE TABLE IF NOT EXISTS trigger_checkpoints (
    checkpoint_name TEXT PRIMARY KEY,
    checkpoint_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class TriggerStore:
    """Durable, idempotent TriggerJob store."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        evidence_store: EvidenceStore | None = None,
        stale_max_attempts: int = 3,
    ) -> None:
        if evidence_store is not None:
            db_path = evidence_store.db_path
        if db_path is None:
            raise ValueError("TriggerStore requires db_path or evidence_store")
        self._db_path = Path(db_path).expanduser().resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._stale_max_attempts = stale_max_attempts
        self._mu = threading.Lock()
        self._closed = False
        self._conn = self._make_connection(read_only=False)
        self._init_db()

    @property
    def db_path(self) -> Path:
        return self._db_path

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
            self._retire_legacy_candidate_rechecks()
            self._conn.commit()

    def _retire_legacy_candidate_rechecks(self) -> None:
        now = _utc_now()
        self._conn.execute(
            """
            UPDATE trigger_jobs
            SET status='superseded',
                locked_at=NULL,
                locked_by=NULL,
                completed_at=?,
                error=CASE
                    WHEN error IS NULL OR error='' THEN ?
                    ELSE error || '; ' || ?
                END
            WHERE trigger_type='CANDIDATE_RECHECK'
              AND status IN ('pending', 'running', 'failed_retryable')
            """,
            (
                now,
                _RETIRED_CANDIDATE_RECHECK_REASON,
                _RETIRED_CANDIDATE_RECHECK_REASON,
            ),
        )

    @contextmanager
    def _reader(self) -> Generator[sqlite3.Connection, None, None]:
        self._ensure_open()
        conn = self._make_connection(read_only=True)
        try:
            yield conn
        finally:
            conn.close()

    def latest_manifest_watermark(self) -> int:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS watermark FROM evidence_events"
            ).fetchone()
            return int(row["watermark"] if row is not None else 0)

    def create_job(
        self,
        spec: TriggerJobSpec,
        *,
        manifest_watermark: int,
        status: str | None = None,
    ) -> TriggerJob:
        """Insert a job or return an existing open job for the same idempotency key."""

        now = _utc_now()
        job_status = status or "pending"
        profile_fallback = bool(spec.profile_fallback)
        evidence_profile = spec.evidence_profile
        subprofile = spec.subprofile
        error: str | None = None
        if spec.trigger_type not in TRIGGER_TYPES:
            job_status = "rejected"
            profile_fallback = True
            evidence_profile = evidence_profile or "unknown"
            subprofile = subprofile or "rejected"
            error = f"unknown trigger_type: {spec.trigger_type}"
        if job_status not in TRIGGER_STATUSES:
            raise ValueError(f"Unsupported trigger job status: {job_status}")

        scheduled_at = spec.scheduled_at or now
        with self._mu:
            self._ensure_open()
            existing = self._open_idempotent_job_locked(spec.idempotency_key)
            if existing is not None:
                return _row_to_job(existing)

            insert_idempotency_key = spec.idempotency_key
            if self._idempotency_key_exists_locked(spec.idempotency_key):
                insert_idempotency_key = self._next_idempotency_key_locked(
                    spec.idempotency_key,
                    now=now,
                    manifest_watermark=manifest_watermark,
                )
            job_id = f"trg_{_digest(insert_idempotency_key)}"

            self._conn.execute(
                """
                INSERT INTO trigger_jobs (
                    job_id, trigger_type, reason, reason_tags_json, status,
                    scope_json, manifest_watermark, evidence_profile, subprofile,
                    profile_fallback, idempotency_key, attempts, locked_at,
                    locked_by, created_at, scheduled_at, completed_at, error,
                    result_ref
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?, ?, NULL)
                """,
                (
                    job_id,
                    spec.trigger_type,
                    spec.reason,
                    _json(spec.reason_tags),
                    job_status,
                    _json(spec.scope.to_dict()),
                    int(manifest_watermark),
                    evidence_profile,
                    subprofile,
                    int(profile_fallback),
                    insert_idempotency_key,
                    now,
                    scheduled_at,
                    now if job_status in {"rejected", "failed"} else None,
                    error,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM trigger_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("trigger job insert did not return a row")
            return _row_to_job(row)

    def get_job(self, job_id: str) -> TriggerJob | None:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM trigger_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            return _row_to_job(row) if row is not None else None

    def get_by_idempotency_key(self, idempotency_key: str) -> TriggerJob | None:
        with self._reader() as conn:
            open_row = _open_idempotent_job(conn, idempotency_key)
            if open_row is not None:
                return _row_to_job(open_row)
            row = conn.execute(
                "SELECT * FROM trigger_jobs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            return _row_to_job(row) if row is not None else None

    def _open_idempotent_job_locked(self, idempotency_key: str) -> sqlite3.Row | None:
        return _open_idempotent_job(self._conn, idempotency_key)

    def _idempotency_key_exists_locked(self, idempotency_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM trigger_jobs WHERE idempotency_key=? LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        return row is not None

    def _next_idempotency_key_locked(
        self,
        idempotency_key: str,
        *,
        now: str,
        manifest_watermark: int,
    ) -> str:
        for attempt in range(1, 1000):
            candidate = (
                f"{idempotency_key}{_IDEMPOTENCY_RUN_SEPARATOR}"
                f"{_digest(_json({'base': idempotency_key, 'created_at': now, 'watermark': manifest_watermark, 'attempt': attempt}))[:12]}"
            )
            if not self._idempotency_key_exists_locked(candidate):
                return candidate
        raise RuntimeError("unable to allocate unique trigger idempotency run key")

    def list_jobs(self, *, status: str | None = None) -> list[TriggerJob]:
        with self._reader() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM trigger_jobs ORDER BY created_at, job_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM trigger_jobs WHERE status=? ORDER BY created_at, job_id",
                    (status,),
                ).fetchall()
            return [_row_to_job(row) for row in rows]

    def claim_next(
        self,
        *,
        limit: int = 1,
        worker_id: str | None = None,
        trigger_types: tuple[str, ...] | None = None,
        scope: EvidenceScope | None = None,
        claim_statuses: tuple[str, ...] | None = None,
    ) -> list[TriggerJob]:
        """Atomically lock pending retryable jobs for a worker."""

        if limit <= 0:
            return []
        worker = worker_id or "trigger-worker"
        now = _utc_now()
        requested_types = _normalize_trigger_types(trigger_types)
        if trigger_types and not requested_types:
            return []
        requested_statuses = _normalize_claim_statuses(claim_statuses)
        if claim_statuses and not requested_statuses:
            return []
        with self._mu:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                status_placeholders = ",".join("?" for _ in requested_statuses)
                clauses = [
                    f"status IN ({status_placeholders})",
                    "scheduled_at <= ?",
                ]
                params: list[Any] = [*requested_statuses, now]
                if requested_types:
                    placeholders = ",".join("?" for _ in requested_types)
                    clauses.append(f"trigger_type IN ({placeholders})")
                    params.extend(requested_types)
                rows = self._conn.execute(
                    f"""
                    SELECT job_id, scope_json FROM trigger_jobs
                    WHERE {' AND '.join(clauses)}
                    ORDER BY scheduled_at, created_at, job_id
                    """,
                    params,
                ).fetchall()
                job_ids = [
                    str(row["job_id"])
                    for row in rows
                    if _scope_matches(row["scope_json"], scope)
                ][: int(limit)]
                for job_id in job_ids:
                    self._conn.execute(
                        f"""
                        UPDATE trigger_jobs
                        SET status='running',
                            attempts=attempts + 1,
                            locked_at=?,
                            locked_by=?,
                            error=NULL
                        WHERE job_id=?
                          AND status IN ({status_placeholders})
                        """,
                        (now, worker, job_id, *requested_statuses),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        if not job_ids:
            return []
        with self._reader() as conn:
            placeholders = ",".join("?" for _ in job_ids)
            claimed = conn.execute(
                f"SELECT * FROM trigger_jobs WHERE job_id IN ({placeholders})",
                job_ids,
            ).fetchall()
            rows_by_id = {str(row["job_id"]): row for row in claimed}
            return [_row_to_job(rows_by_id[job_id]) for job_id in job_ids if job_id in rows_by_id]

    def claim_jobs(
        self,
        job_ids: list[str],
        *,
        worker_id: str | None = None,
    ) -> list[TriggerJob]:
        """Atomically lock specific pending retryable jobs for a worker."""

        requested = [str(job_id) for job_id in job_ids if str(job_id)]
        if not requested:
            return []
        worker = worker_id or "trigger-worker"
        now = _utc_now()
        with self._mu:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                placeholders = ",".join("?" for _ in requested)
                rows = self._conn.execute(
                    f"""
                    SELECT job_id FROM trigger_jobs
                    WHERE job_id IN ({placeholders})
                      AND status IN ('pending', 'failed_retryable')
                      AND scheduled_at <= ?
                    ORDER BY scheduled_at, created_at, job_id
                    """,
                    [*requested, now],
                ).fetchall()
                claimable = [str(row["job_id"]) for row in rows]
                for job_id in claimable:
                    self._conn.execute(
                        """
                        UPDATE trigger_jobs
                        SET status='running',
                            attempts=attempts + 1,
                            locked_at=?,
                            locked_by=?,
                            error=NULL
                        WHERE job_id=?
                          AND status IN ('pending', 'failed_retryable')
                        """,
                        (now, worker, job_id),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        if not claimable:
            return []
        with self._reader() as conn:
            placeholders = ",".join("?" for _ in claimable)
            claimed = conn.execute(
                f"SELECT * FROM trigger_jobs WHERE job_id IN ({placeholders})",
                claimable,
            ).fetchall()
            rows_by_id = {str(row["job_id"]): row for row in claimed}
            return [_row_to_job(rows_by_id[job_id]) for job_id in claimable if job_id in rows_by_id]

    def complete(
        self,
        job_id: str,
        *,
        status: str,
        result_ref: str | None = None,
        error: str | None = None,
        clear_result_ref: bool = False,
    ) -> None:
        if status not in TRIGGER_STATUSES:
            raise ValueError(f"Unsupported trigger job status: {status}")
        completed_at = _utc_now() if status in {
            "completed",
            "failed",
            "superseded",
            "rejected",
        } else None
        result_ref_sql = "result_ref"
        params: list[Any] = [status, completed_at, error]
        if clear_result_ref:
            result_ref_sql = "NULL"
        elif result_ref is not None:
            result_ref_sql = "?"
            params.append(result_ref)
        params.append(job_id)
        with self._mu:
            self._ensure_open()
            self._conn.execute(
                f"""
                UPDATE trigger_jobs
                SET status=?,
                    completed_at=?,
                    error=?,
                    result_ref={result_ref_sql},
                    locked_at=NULL,
                    locked_by=NULL
                WHERE job_id=?
                """,
                params,
            )
            self._conn.commit()

    def recover_stale_jobs(self, *, timeout_s: float) -> int:
        """Move stale running jobs back to retryable/failed state."""

        now_dt = datetime.now(timezone.utc)
        cutoff = (now_dt - timedelta(seconds=timeout_s)).isoformat()
        now = now_dt.isoformat()
        recovered = 0
        with self._mu:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    """
                    SELECT job_id, attempts FROM trigger_jobs
                    WHERE status='running'
                      AND locked_at IS NOT NULL
                      AND locked_at < ?
                    ORDER BY locked_at, job_id
                    """,
                    (cutoff,),
                ).fetchall()
                for row in rows:
                    attempts = int(row["attempts"] or 0) + 1
                    terminal = attempts >= self._stale_max_attempts
                    self._conn.execute(
                        """
                        UPDATE trigger_jobs
                        SET status=?,
                            attempts=?,
                            locked_at=NULL,
                            locked_by=NULL,
                            scheduled_at=?,
                            completed_at=?,
                            error=?
                        WHERE job_id=?
                        """,
                        (
                            "failed" if terminal else "failed_retryable",
                            attempts,
                            now,
                            now if terminal else None,
                            "stale running job recovered",
                            row["job_id"],
                        ),
                    )
                    recovered += 1
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return recovered

    def load_checkpoint(self, checkpoint_name: str) -> str | None:
        """Load a durable trigger scheduler checkpoint."""

        name = str(checkpoint_name or "").strip()
        if not name:
            return None
        with self._reader() as conn:
            row = conn.execute(
                "SELECT checkpoint_value FROM trigger_checkpoints WHERE checkpoint_name=?",
                (name,),
            ).fetchone()
            if row is None:
                return None
            return str(row["checkpoint_value"])

    def save_checkpoint(self, checkpoint_name: str, checkpoint_value: Any) -> None:
        """Persist a trigger scheduler checkpoint."""

        name = str(checkpoint_name or "").strip()
        if not name:
            raise ValueError("checkpoint_name is required")
        now = _utc_now()
        with self._mu:
            self._ensure_open()
            self._conn.execute(
                """
                INSERT INTO trigger_checkpoints (
                    checkpoint_name, checkpoint_value, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(checkpoint_name) DO UPDATE SET
                    checkpoint_value=excluded.checkpoint_value,
                    updated_at=excluded.updated_at
                """,
                (name, str(checkpoint_value), now),
            )
            self._conn.commit()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("TriggerStore is closed")


def _row_to_job(row: sqlite3.Row) -> TriggerJob:
    return TriggerJob(
        job_id=str(row["job_id"]),
        trigger_type=str(row["trigger_type"]),
        reason=str(row["reason"]),
        reason_tags=_json_list(row["reason_tags_json"]),
        status=str(row["status"]),
        scope=EvidenceScope.from_mapping(_json_object(row["scope_json"])),
        manifest_watermark=int(row["manifest_watermark"] or 0),
        evidence_profile=str(row["evidence_profile"] or ""),
        subprofile=str(row["subprofile"] or ""),
        profile_fallback=bool(row["profile_fallback"]),
        idempotency_key=str(row["idempotency_key"] or ""),
        attempts=int(row["attempts"] or 0),
        locked_at=_none_or_str(row["locked_at"]),
        locked_by=_none_or_str(row["locked_by"]),
        created_at=str(row["created_at"] or ""),
        scheduled_at=str(row["scheduled_at"] or ""),
        completed_at=_none_or_str(row["completed_at"]),
        error=_none_or_str(row["error"]),
        result_ref=_none_or_str(row["result_ref"]),
    )


def _open_idempotent_job(
    conn: sqlite3.Connection,
    idempotency_key: str,
) -> sqlite3.Row | None:
    key = str(idempotency_key)
    pattern = _like_escape(f"{key}{_IDEMPOTENCY_RUN_SEPARATOR}") + "%"
    placeholders = ",".join("?" for _ in _OPEN_JOB_STATUSES)
    return conn.execute(
        f"""
        SELECT *
        FROM trigger_jobs
        WHERE (idempotency_key=? OR idempotency_key LIKE ? ESCAPE '\\')
          AND status IN ({placeholders})
        ORDER BY created_at DESC, job_id DESC
        LIMIT 1
        """,
        (key, pattern, *_OPEN_JOB_STATUSES),
    ).fetchone()


def _like_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _normalize_trigger_types(value: tuple[str, ...] | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(
        item
        for item in dict.fromkeys(str(raw).strip().upper() for raw in value)
        if item in TRIGGER_TYPES
    )


def _normalize_claim_statuses(value: tuple[str, ...] | None) -> tuple[str, ...]:
    if not value:
        return ("pending", "failed_retryable")
    claimable = {"pending", "failed_retryable"}
    return tuple(
        item
        for item in dict.fromkeys(str(raw).strip().lower() for raw in value)
        if item in claimable
    )


def _scope_matches(scope_json: str, requested: EvidenceScope | None) -> bool:
    if requested is None:
        return True
    try:
        candidate = EvidenceScope.from_mapping(json.loads(scope_json or "{}"))
    except Exception:
        return False

    if requested.session_id and candidate.session_id != requested.session_id:
        return False
    if requested.task_id and candidate.task_id != requested.task_id:
        return False
    if requested.turn_range and candidate.turn_range != requested.turn_range:
        return False
    if requested.time_window and candidate.time_window != requested.time_window:
        return False
    for name in (
        "skill_ids",
        "tool_keys",
        "source_task_ids",
        "representative_execution_ids",
        "agent_ids",
    ):
        requested_values = set(getattr(requested, name, ()) or ())
        if requested_values and not requested_values.issubset(
            set(getattr(candidate, name, ()) or ())
        ):
            return False
    return True


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
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


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _none_or_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
