"""SQLite outbox for redacted cloud telemetry events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Iterable

from openspace.config.constants import PROJECT_ROOT
from openspace.cloud.redaction import redact_telemetry_payload

_DDL = """
CREATE TABLE IF NOT EXISTS cloud_telemetry_outbox (
    request_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_redacted_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    PRIMARY KEY (request_id, payload_hash)
);
CREATE INDEX IF NOT EXISTS idx_cloud_telemetry_outbox_status
    ON cloud_telemetry_outbox(status, created_at);
"""


@dataclass(frozen=True)
class TelemetryOutboxRow:
    request_id: str
    endpoint: str
    payload_hash: str
    payload_redacted: dict[str, Any]
    status: str
    attempts: int
    last_error: str | None
    created_at: str
    sent_at: str | None


class CloudTelemetryOutbox:
    """Persist only redacted telemetry payloads for idempotent retry."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_dir = PROJECT_ROOT / ".openspace"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = db_dir / "openspace.db"
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._mu = threading.Lock()
        self._closed = False
        self._conn = self._make_connection(read_only=False)
        self._init_db()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "CloudTelemetryOutbox":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _make_connection(self, *, read_only: bool) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        if read_only:
            conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _reader(self) -> Generator[sqlite3.Connection, None, None]:
        self._ensure_open()
        conn = self._make_connection(read_only=True)
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._mu:
            self._conn.executescript(_DDL)
            self._conn.commit()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("CloudTelemetryOutbox is closed")

    def enqueue(
        self,
        *,
        endpoint: str,
        payload: dict[str, Any],
        request_id: str | None = None,
        workspace_root: str | Path | None = None,
    ) -> TelemetryOutboxRow:
        """Insert a redacted pending row or return the existing idempotent row."""

        self._ensure_open()
        if not endpoint.startswith("/"):
            endpoint = f"/{endpoint}"
        redacted = redact_telemetry_payload(payload, workspace_root=workspace_root)
        rid = str(request_id or redacted.get("request_id") or "").strip()
        if not rid:
            raise ValueError("request_id is required for cloud telemetry outbox")
        redacted["request_id"] = rid
        payload_json = _canonical_json(redacted)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        created_at = utc_now_iso()
        with self._mu:
            self._conn.execute(
                """
                INSERT INTO cloud_telemetry_outbox (
                    request_id,
                    endpoint,
                    payload_hash,
                    payload_redacted_json,
                    status,
                    attempts,
                    created_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?)
                ON CONFLICT(request_id, payload_hash) DO NOTHING
                """,
                (rid, endpoint, payload_hash, payload_json, created_at),
            )
            self._conn.commit()
        row = self.get(rid, payload_hash)
        if row is None:
            raise RuntimeError("failed to load queued cloud telemetry outbox row")
        return row

    def mark_sent(
        self,
        request_id: str,
        payload_hash: str,
        *,
        sent_at: str | None = None,
    ) -> TelemetryOutboxRow | None:
        self._ensure_open()
        with self._mu:
            self._conn.execute(
                """
                UPDATE cloud_telemetry_outbox
                SET status='sent', sent_at=?, last_error=NULL
                WHERE request_id=? AND payload_hash=?
                """,
                (sent_at or utc_now_iso(), request_id, payload_hash),
            )
            self._conn.commit()
        return self.get(request_id, payload_hash)

    def mark_failed(
        self,
        request_id: str,
        payload_hash: str,
        *,
        error: str,
    ) -> TelemetryOutboxRow | None:
        self._ensure_open()
        with self._mu:
            self._conn.execute(
                """
                UPDATE cloud_telemetry_outbox
                SET status='failed', attempts=attempts + 1, last_error=?
                WHERE request_id=? AND payload_hash=?
                """,
                (str(error)[:2000], request_id, payload_hash),
            )
            self._conn.commit()
        return self.get(request_id, payload_hash)

    def reset_for_retry(self, request_id: str, payload_hash: str) -> TelemetryOutboxRow | None:
        self._ensure_open()
        with self._mu:
            self._conn.execute(
                """
                UPDATE cloud_telemetry_outbox
                SET status='pending'
                WHERE request_id=? AND payload_hash=? AND status='failed'
                """,
                (request_id, payload_hash),
            )
            self._conn.commit()
        return self.get(request_id, payload_hash)

    def get(self, request_id: str, payload_hash: str) -> TelemetryOutboxRow | None:
        with self._reader() as conn:
            row = conn.execute(
                """
                SELECT * FROM cloud_telemetry_outbox
                WHERE request_id=? AND payload_hash=?
                """,
                (request_id, payload_hash),
            ).fetchone()
        return _row_to_outbox_row(row) if row else None

    def list_pending(self, *, limit: int = 100) -> list[TelemetryOutboxRow]:
        return self.list_by_status("pending", limit=limit)

    def list_by_status(
        self,
        status: str,
        *,
        limit: int = 100,
    ) -> list[TelemetryOutboxRow]:
        with self._reader() as conn:
            rows = conn.execute(
                """
                SELECT * FROM cloud_telemetry_outbox
                WHERE status=?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (status, int(limit)),
            ).fetchall()
        return [_row_to_outbox_row(row) for row in rows]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _row_to_outbox_row(row: sqlite3.Row) -> TelemetryOutboxRow:
    return TelemetryOutboxRow(
        request_id=row["request_id"],
        endpoint=row["endpoint"],
        payload_hash=row["payload_hash"],
        payload_redacted=json.loads(row["payload_redacted_json"]),
        status=row["status"],
        attempts=int(row["attempts"]),
        last_error=row["last_error"],
        created_at=row["created_at"],
        sent_at=row["sent_at"],
    )


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
