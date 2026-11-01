"""
SQLite-backed persistence for tool quality data. Shares the same database file as SkillStore.

Storage location (default):
    <project_root>/.openspace/openspace.db

Tables managed by this module:
    tool_quality_records   — one row per tool (aggregate stats)
    tool_execution_history — rolling window of per-call records
    tool_quality_meta      — key-value metadata (global_execution_count)
"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from .types import ToolQualityRecord, ExecutionRecord, DescriptionQuality
from openspace.utils.logging import Logger
from openspace.config.constants import PROJECT_ROOT

logger = Logger.get_logger(__name__)


_DDL = """
CREATE TABLE IF NOT EXISTS tool_quality_records (
    tool_key                TEXT PRIMARY KEY,
    backend                 TEXT NOT NULL,
    server                  TEXT NOT NULL DEFAULT 'default',
    tool_name               TEXT NOT NULL,
    total_calls             INTEGER NOT NULL DEFAULT 0,
    success_count           INTEGER NOT NULL DEFAULT 0,
    total_execution_time_ms REAL    NOT NULL DEFAULT 0.0,
    llm_flagged_count       INTEGER NOT NULL DEFAULT 0,
    description_hash        TEXT,
    desc_clarity            REAL,
    desc_completeness       REAL,
    desc_evaluated_at       TEXT,
    desc_reasoning          TEXT,
    first_seen              TEXT NOT NULL,
    last_updated            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tqr_backend ON tool_quality_records(backend);
CREATE INDEX IF NOT EXISTS idx_tqr_flagged ON tool_quality_records(llm_flagged_count);

CREATE TABLE IF NOT EXISTS tool_execution_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_key          TEXT NOT NULL
        REFERENCES tool_quality_records(tool_key) ON DELETE CASCADE,
    timestamp         TEXT NOT NULL,
    success           INTEGER NOT NULL,
    execution_time_ms REAL    NOT NULL DEFAULT 0.0,
    error_message     TEXT
);
CREATE INDEX IF NOT EXISTS idx_teh_key ON tool_execution_history(tool_key);
CREATE INDEX IF NOT EXISTS idx_teh_ts  ON tool_execution_history(timestamp);

CREATE TABLE IF NOT EXISTS tool_quality_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class QualityStore:
    """SQLite-backed persistence for tool quality data.

    By default uses the same ``.db`` file as ``SkillStore``
    (``<project_root>/.openspace/openspace.db``).
    Each subsystem creates its own tables independently.
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_dir = PROJECT_ROOT / ".openspace"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = db_dir / "openspace.db"

        self._db_path = Path(db_path)
        self._mu = threading.Lock()

        self._conn = sqlite3.connect(
            str(self._db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.row_factory = sqlite3.Row

        self._init_tables()

        logger.debug(f"QualityStore ready (SQLite) at {self._db_path}")

    def _init_tables(self) -> None:
        with self._mu:
            self._conn.executescript(_DDL)
            self._conn.commit()

    def load_all(self) -> Tuple[Dict[str, ToolQualityRecord], int]:
        """Load all quality records and global execution count."""
        with self._mu:
            rows = self._conn.execute(
                "SELECT * FROM tool_quality_records"
            ).fetchall()

            records: Dict[str, ToolQualityRecord] = {}
            for row in rows:
                tool_key = row["tool_key"]
                record = ToolQualityRecord(
                    tool_key=tool_key,
                    backend=row["backend"],
                    server=row["server"],
                    tool_name=row["tool_name"],
                    total_calls=row["total_calls"],
                    success_count=row["success_count"],
                    total_execution_time_ms=row["total_execution_time_ms"],
                    llm_flagged_count=row["llm_flagged_count"],
                    description_hash=row["description_hash"],
                    first_seen=datetime.fromisoformat(row["first_seen"]),
                    last_updated=datetime.fromisoformat(row["last_updated"]),
                )

                # Description quality (all-or-nothing: clarity present → all present)
                if row["desc_clarity"] is not None:
                    record.description_quality = DescriptionQuality(
                        clarity=row["desc_clarity"],
                        completeness=row["desc_completeness"],
                        evaluated_at=datetime.fromisoformat(row["desc_evaluated_at"]),
                        reasoning=row["desc_reasoning"] or "",
                    )

                # Recent execution history (most recent N, restored chronologically)
                exec_rows = self._conn.execute(
                    "SELECT timestamp, success, execution_time_ms, error_message "
                    "FROM tool_execution_history "
                    "WHERE tool_key = ? ORDER BY id DESC LIMIT ?",
                    (tool_key, ToolQualityRecord.MAX_RECENT_EXECUTIONS),
                ).fetchall()
                record.recent_executions = [
                    ExecutionRecord(
                        timestamp=datetime.fromisoformat(er["timestamp"]),
                        success=bool(er["success"]),
                        execution_time_ms=er["execution_time_ms"],
                        error_message=er["error_message"],
                    )
                    for er in reversed(exec_rows)
                ]

                records[tool_key] = record

            # Global metadata
            meta_row = self._conn.execute(
                "SELECT value FROM tool_quality_meta "
                "WHERE key = 'global_execution_count'"
            ).fetchone()
            global_count = int(meta_row["value"]) if meta_row else 0

            logger.info(
                f"Loaded {len(records)} quality records from SQLite "
                f"(global_count={global_count})"
            )
            return records, global_count

    async def save_all(
        self,
        records: Dict[str, ToolQualityRecord],
        global_execution_count: int = 0,
    ) -> None:
        """Persist all records (bulk)."""
        self._save_all_sync(records, global_execution_count)

    async def save_record(
        self,
        record: ToolQualityRecord,
        all_records: Dict[str, ToolQualityRecord],
        global_execution_count: int = 0,
    ) -> None:
        """Persist a single record (incremental — much cheaper than save_all)."""
        with self._mu:
            try:
                self._upsert_record(record)
                self._conn.execute(
                    "INSERT OR REPLACE INTO tool_quality_meta "
                    "(key, value) VALUES (?, ?)",
                    ("global_execution_count", str(global_execution_count)),
                )
                self._conn.commit()
            except Exception as e:
                self._conn.rollback()
                logger.error(f"Failed to save record {record.tool_key}: {e}")

    def clear(self) -> None:
        """Delete all quality data."""
        with self._mu:
            self._conn.execute("DELETE FROM tool_execution_history")
            self._conn.execute("DELETE FROM tool_quality_records")
            self._conn.execute("DELETE FROM tool_quality_meta")
            self._conn.commit()
        logger.info("Quality data cleared")

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass

    def _save_all_sync(
        self,
        records: Dict[str, ToolQualityRecord],
        global_execution_count: int = 0,
    ) -> None:
        """Synchronous full save (used by async wrapper and migration)."""
        with self._mu:
            try:
                for record in records.values():
                    self._upsert_record(record)
                self._conn.execute(
                    "INSERT OR REPLACE INTO tool_quality_meta "
                    "(key, value) VALUES (?, ?)",
                    ("global_execution_count", str(global_execution_count)),
                )
                self._conn.commit()
            except Exception as e:
                self._conn.rollback()
                logger.error(f"Failed to bulk-save quality records: {e}")

    def _upsert_record(self, record: ToolQualityRecord) -> None:
        """Upsert one tool_quality_records row + its execution history.

        Caller MUST hold ``self._mu``.  Does NOT commit — caller manages
        the transaction boundary.
        """
        dq = record.description_quality
        self._conn.execute(
            """INSERT OR REPLACE INTO tool_quality_records
            (tool_key, backend, server, tool_name,
             total_calls, success_count, total_execution_time_ms,
             llm_flagged_count, description_hash,
             desc_clarity, desc_completeness, desc_evaluated_at, desc_reasoning,
             first_seen, last_updated)
            VALUES (?,?,?,?, ?,?,?, ?,?, ?,?,?,?, ?,?)""",
            (
                record.tool_key,
                record.backend,
                record.server,
                record.tool_name,
                record.total_calls,
                record.success_count,
                record.total_execution_time_ms,
                record.llm_flagged_count,
                record.description_hash,
                dq.clarity if dq else None,
                dq.completeness if dq else None,
                dq.evaluated_at.isoformat() if dq else None,
                dq.reasoning if dq else None,
                record.first_seen.isoformat(),
                record.last_updated.isoformat(),
            ),
        )

        # Sync execution history: delete + re-insert.
        # For ≤ MAX_RECENT_EXECUTIONS rows this is fast and avoids
        # complex diff logic between in-memory and DB state.
        self._conn.execute(
            "DELETE FROM tool_execution_history WHERE tool_key = ?",
            (record.tool_key,),
        )
        if record.recent_executions:
            self._conn.executemany(
                "INSERT INTO tool_execution_history "
                "(tool_key, timestamp, success, execution_time_ms, error_message) "
                "VALUES (?,?,?,?,?)",
                [
                    (
                        record.tool_key,
                        e.timestamp.isoformat(),
                        int(e.success),
                        e.execution_time_ms,
                        e.error_message,
                    )
                    for e in record.recent_executions
                ],
            )
