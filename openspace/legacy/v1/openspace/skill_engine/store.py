"""
Storage location: <project_root>/.openspace/openspace.db
Tables:
  skill_records          — SkillRecord main table
  skill_lineage_parents  — Lineage parent-child relationships (many-to-many)
  execution_analyses     — ExecutionAnalysis records (one per task)
  skill_judgments         — Per-skill judgments within an analysis
  skill_tool_deps        — Tool dependencies
  skill_tags             — Auxiliary tags
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from .patch import collect_skill_snapshot, compute_unified_diff
from .types import (
    EvolutionSuggestion,
    ExecutionAnalysis,
    SkillCategory,
    SkillJudgment,
    SkillLineage,
    SkillOrigin,
    SkillRecord,
    SkillVisibility,
)
from openspace.utils.logging import Logger
from openspace.config.constants import PROJECT_ROOT

logger = Logger.get_logger(__name__)


def _db_retry(
    max_retries: int = 5,
    initial_delay: float = 0.1,
    backoff: float = 2.0,
):
    """Retry on transient SQLite errors with exponential backoff.

    Catches ``OperationalError`` (e.g. "database is locked") and
    ``DatabaseError`` but NOT programming errors like ``InterfaceError``.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
                    if attempt == max_retries - 1:
                        logger.error(
                            f"DB {func.__name__} failed after "
                            f"{max_retries} retries: {exc}"
                        )
                        raise
                    logger.warning(
                        f"DB {func.__name__} retry {attempt + 1}"
                        f"/{max_retries}: {exc}"
                    )
                    time.sleep(delay)
                    delay *= backoff

        return wrapper

    return decorator


_DDL = """
CREATE TABLE IF NOT EXISTS skill_records (
    skill_id               TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    description            TEXT NOT NULL DEFAULT '',
    path                   TEXT NOT NULL DEFAULT '',
    is_active              INTEGER NOT NULL DEFAULT 1,
    category               TEXT NOT NULL DEFAULT 'workflow',
    visibility             TEXT NOT NULL DEFAULT 'private',
    creator_id             TEXT NOT NULL DEFAULT '',
    lineage_origin         TEXT NOT NULL DEFAULT 'imported',
    lineage_generation     INTEGER NOT NULL DEFAULT 0,
    lineage_source_task_id TEXT,
    lineage_change_summary TEXT NOT NULL DEFAULT '',
    lineage_content_diff   TEXT NOT NULL DEFAULT '',
    lineage_content_snapshot TEXT NOT NULL DEFAULT '{}',
    lineage_created_at     TEXT NOT NULL,
    lineage_created_by     TEXT NOT NULL DEFAULT '',
    total_selections       INTEGER NOT NULL DEFAULT 0,
    total_applied          INTEGER NOT NULL DEFAULT 0,
    total_completions      INTEGER NOT NULL DEFAULT 0,
    total_fallbacks        INTEGER NOT NULL DEFAULT 0,
    first_seen             TEXT NOT NULL,
    last_updated           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sr_category ON skill_records(category);
CREATE INDEX IF NOT EXISTS idx_sr_updated  ON skill_records(last_updated);
CREATE INDEX IF NOT EXISTS idx_sr_active   ON skill_records(is_active);
CREATE INDEX IF NOT EXISTS idx_sr_name     ON skill_records(name);

CREATE TABLE IF NOT EXISTS skill_lineage_parents (
    skill_id        TEXT NOT NULL
        REFERENCES skill_records(skill_id) ON DELETE CASCADE,
    parent_skill_id TEXT NOT NULL,
    PRIMARY KEY (skill_id, parent_skill_id)
);
CREATE INDEX IF NOT EXISTS idx_lp_parent
    ON skill_lineage_parents(parent_skill_id);

-- One row per task.  task_id is UNIQUE (at most one analysis per task).
CREATE TABLE IF NOT EXISTS execution_analyses (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                 TEXT NOT NULL UNIQUE,
    timestamp               TEXT NOT NULL,
    task_completed          INTEGER NOT NULL DEFAULT 0,
    execution_note          TEXT NOT NULL DEFAULT '',
    tool_issues             TEXT NOT NULL DEFAULT '[]',
    candidate_for_evolution INTEGER NOT NULL DEFAULT 0,
    evolution_processed_at  TEXT DEFAULT NULL,
    evolution_suggestions   TEXT NOT NULL DEFAULT '[]',
    analyzed_by             TEXT NOT NULL DEFAULT '',
    analyzed_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ea_task  ON execution_analyses(task_id);
CREATE INDEX IF NOT EXISTS idx_ea_ts    ON execution_analyses(timestamp);

-- Per-skill judgments within an analysis.
-- FK to execution_analyses.id (CASCADE delete).
-- skill_id is a plain TEXT — no FK to skill_records so that
-- historical judgments survive skill deletion.
CREATE TABLE IF NOT EXISTS skill_judgments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id    INTEGER NOT NULL
        REFERENCES execution_analyses(id) ON DELETE CASCADE,
    skill_id       TEXT NOT NULL,
    skill_applied  INTEGER NOT NULL DEFAULT 0,
    note           TEXT NOT NULL DEFAULT '',
    UNIQUE(analysis_id, skill_id)
);
CREATE INDEX IF NOT EXISTS idx_sj_skill    ON skill_judgments(skill_id);
CREATE INDEX IF NOT EXISTS idx_sj_analysis ON skill_judgments(analysis_id);

CREATE TABLE IF NOT EXISTS skill_tool_deps (
    skill_id TEXT NOT NULL
        REFERENCES skill_records(skill_id) ON DELETE CASCADE,
    tool_key TEXT NOT NULL,
    critical INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (skill_id, tool_key)
);
CREATE INDEX IF NOT EXISTS idx_td_tool ON skill_tool_deps(tool_key);

CREATE TABLE IF NOT EXISTS skill_tags (
    skill_id TEXT NOT NULL
        REFERENCES skill_records(skill_id) ON DELETE CASCADE,
    tag      TEXT NOT NULL,
    PRIMARY KEY (skill_id, tag)
);
"""


class SkillStore:
    """SQLite persistence engine — Skill quality tracking and evolution ledger.

    Architecture:
        Write path: async method → asyncio.to_thread → _xxx_sync → self._mu lock → self._conn
        Read path: sync method → self._reader() → independent short connection (WAL parallel read)

    Lifecycle: ``__init__()`` → use → ``close()``
    Also supports async context manager:
        async with SkillStore() as store:
            await store.save_record(record)
            rec = store.load_record(skill_id)
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        if db_path is None:
            db_dir = PROJECT_ROOT / ".openspace"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = db_dir / "openspace.db"

        self._db_path = Path(db_path)
        self._mu = threading.Lock()
        self._closed = False

        # Crash recovery: clean up stale WAL/SHM from unclean shutdown
        self._cleanup_wal_on_startup()

        # Persistent write connection
        self._conn = self._make_connection(read_only=False)
        self._init_db()
        logger.debug(f"SkillStore ready at {self._db_path}")

    def _make_connection(self, *, read_only: bool) -> sqlite3.Connection:
        """Create a tuned SQLite connection.

        Write connection: ``check_same_thread=False`` for cross-thread
        usage via ``asyncio.to_thread()``.

        Read connection: ``query_only=ON`` pragma for safety.
        """
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-16000")  # 16 MB
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA foreign_keys=ON")
        if read_only:
            conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _reader(self) -> Generator[sqlite3.Connection, None, None]:
        """Open a temporary read-only connection.

        WAL mode allows concurrent readers and one writer.
        Each read operation gets its own connection so reads never
        block the event loop and never contend with the write lock.
        """
        self._ensure_open()
        conn = self._make_connection(read_only=True)
        try:
            yield conn
        finally:
            conn.close()

    def _cleanup_wal_on_startup(self) -> None:
        """Remove stale WAL/SHM left by unclean shutdown.

        If the main DB file is empty (0 bytes) but WAL/SHM companions
        exist, the database is unrecoverable — delete the companions
        so SQLite can start fresh.
        """
        if not self._db_path.exists():
            return
        wal = Path(f"{self._db_path}-wal")
        shm = Path(f"{self._db_path}-shm")
        if self._db_path.stat().st_size == 0 and (
            wal.exists() or shm.exists()
        ):
            logger.warning(
                "Empty DB with WAL/SHM — removing for crash recovery"
            )
            for f in (wal, shm):
                if f.exists():
                    f.unlink()

    @_db_retry()
    def _init_db(self) -> None:
        """Create tables if they don't exist (idempotent via IF NOT EXISTS)."""
        with self._mu:
            self._conn.executescript(_DDL)
            self._migrate_add_evolution_processed_at()
            self._conn.commit()

    def _migrate_add_evolution_processed_at(self) -> None:
        """Add evolution_processed_at column to existing DBs (idempotent)."""
        try:
            self._conn.execute(
                "ALTER TABLE execution_analyses "
                "ADD COLUMN evolution_processed_at TEXT DEFAULT NULL"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

    # Lifecycle
    def close(self) -> None:
        """Close the persistent connection. Subsequent ops will raise.

        Performs a WAL checkpoint before closing so that all committed
        data is flushed from the WAL file into the main ``.db`` file.
        This ensures external tools (DB browsers, backup scripts) see
        complete data without needing to understand SQLite WAL mode.
        """
        if self._closed:
            return
        self._closed = True
        try:
            # Flush WAL → main DB so external readers see all data
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()
        except Exception:
            pass
        logger.debug("SkillStore closed (WAL checkpointed)")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.close()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SkillStore is closed")

    # Write API (async, offloaded via asyncio.to_thread)
    async def save_record(self, record: SkillRecord) -> None:
        """Upsert a single :class:`SkillRecord`."""
        await asyncio.to_thread(self._save_record_sync, record)

    async def save_records(self, records: List[SkillRecord]) -> None:
        """Batch upsert in a single transaction."""
        await asyncio.to_thread(self._save_records_sync, records)

    async def sync_from_registry(
        self,
        discovered_skills: List[Any],
    ) -> int:
        """Ensure every discovered skill has an initial DB record.

        For each skill in *discovered_skills* (``SkillMeta`` objects
        from :meth:`SkillRegistry.discover`), if no record with the
        same ``skill_id`` already exists, a new :class:`SkillRecord` is
        created (``origin=IMPORTED``, ``generation=0``).

        Existing records (including evolved ones) are left untouched.

        Args:
            discovered_skills: List of ``SkillMeta`` objects.
        """
        return await asyncio.to_thread(
            self._sync_from_registry_sync, discovered_skills,
        )

    @_db_retry()
    def _sync_from_registry_sync(
        self, discovered_skills: List[Any],
    ) -> int:
        self._ensure_open()
        created = 0
        refreshed = 0
        with self._mu:
            self._conn.execute("BEGIN")
            try:
                # Fetch all existing records keyed by skill_id
                rows = self._conn.execute(
                    "SELECT skill_id, name, description, "
                    "lineage_content_snapshot "
                    "FROM skill_records"
                ).fetchall()
                existing: Dict[str, Any] = {r[0]: r for r in rows}

                # Also fetch all paths with an active record.
                # After FIX evolution the DB skill_id changes but the
                # filesystem path stays the same.  Matching by path
                # prevents creating a duplicate imported record on restart.
                path_rows = self._conn.execute(
                    "SELECT path FROM skill_records WHERE is_active=1"
                ).fetchall()
                existing_active_paths: set = {r[0] for r in path_rows}

                for meta in discovered_skills:
                    path_str = str(meta.path)
                    skill_dir = meta.path.parent

                    if meta.skill_id in existing:
                        # Refresh name/description if frontmatter changed,
                        # and backfill empty content_snapshot
                        row = existing[meta.skill_id]
                        updates: List[str] = []
                        params: list = []

                        if row["name"] != meta.name:
                            updates.append("name=?")
                            params.append(meta.name)
                        if row["description"] != meta.description:
                            updates.append("description=?")
                            params.append(meta.description)

                        raw_snap = row["lineage_content_snapshot"] or ""
                        if raw_snap in ("", "{}"):
                            try:
                                snap = collect_skill_snapshot(skill_dir)
                                if snap:
                                    updates.append("lineage_content_snapshot=?")
                                    params.append(json.dumps(snap, ensure_ascii=False))
                                    diff = "\n".join(
                                        compute_unified_diff("", text, filename=name)
                                        for name, text in sorted(snap.items())
                                        if compute_unified_diff("", text, filename=name)
                                    )
                                    if diff:
                                        updates.append("lineage_content_diff=?")
                                        params.append(diff)
                            except Exception as e:
                                logger.warning(
                                    f"sync_from_registry: snapshot backfill failed "
                                    f"for {meta.skill_id}: {e}"
                                )

                        if updates:
                            params.append(meta.skill_id)
                            self._conn.execute(
                                f"UPDATE skill_records SET {', '.join(updates)} "
                                f"WHERE skill_id=?",
                                params,
                            )
                            refreshed += 1
                        continue

                    # Path already covered by an evolved record
                    if path_str in existing_active_paths:
                        continue

                    # Snapshot the directory so this version can be restored later
                    snapshot: Dict[str, str] = {}
                    content_diff = ""
                    try:
                        snapshot = collect_skill_snapshot(skill_dir)
                        content_diff = "\n".join(
                            compute_unified_diff("", text, filename=name)
                            for name, text in sorted(snapshot.items())
                            if compute_unified_diff("", text, filename=name)
                        )
                    except Exception as e:
                        logger.warning(
                            f"sync_from_registry: failed to snapshot {skill_dir}: {e}"
                        )

                    record = SkillRecord(
                        skill_id=meta.skill_id,
                        name=meta.name,
                        description=meta.description,
                        path=path_str,
                        is_active=True,
                        lineage=SkillLineage(
                            origin=SkillOrigin.IMPORTED,
                            generation=0,
                            content_snapshot=snapshot,
                            content_diff=content_diff,
                        ),
                    )
                    self._upsert(record)
                    created += 1
                    logger.debug(
                        f"sync_from_registry: created {meta.name} [{meta.skill_id}]"
                    )

                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        if created or refreshed:
            logger.info(
                f"sync_from_registry: {created} new record(s) created, "
                f"{refreshed} refreshed, "
                f"{len(discovered_skills) - created - refreshed} unchanged"
            )
        return created

    async def record_analysis(self, analysis: ExecutionAnalysis) -> None:
        """Atomic observation: insert analysis + judgments + increment counters.

        1. INSERT a row in ``execution_analyses`` (one per task).
        2. INSERT rows in ``skill_judgments`` for each skill assessed.
        3. For each judgment, atomically increment the matching
           ``skill_records`` counters:
           - total_selections  += 1         (always)
           - total_applied     += 1         (if skill_applied)
           - total_completions += 1         (if applied and completed)
           - total_fallbacks   += 1         (if not applied and not completed)
           - last_updated = now
        """
        await asyncio.to_thread(self._record_analysis_sync, analysis)

    async def evolve_skill(
        self,
        new_record: SkillRecord,
        parent_skill_ids: List[str],
    ) -> None:
        """Atomic evolution: insert new version + deactivate old version.

        **FIXED** — Same-name skill fix:
          - ``new_record.name`` is the same as parent
          - ``new_record.path`` is the same as parent
          - parent is set to ``is_active=False``
          - ``new_record.is_active=True``

        **DERIVED** — New skill derived:
          - ``new_record.name`` is a new name
          - parent is kept ``is_active=True`` (it is still the latest version of its line)
          - ``new_record.is_active=True``

        In the same SQL transaction, guaranteed by ``self._mu``.

        Args:
        new_record : SkillRecord
            New version record, ``lineage.parent_skill_ids`` must be non-empty.
        parent_skill_ids : list[str]
            Parent skill_id list (FIXED exactly 1, DERIVED ≥ 1).
            For FIXED, parent is automatically deactivated.
        """
        await asyncio.to_thread(
            self._evolve_skill_sync, new_record, parent_skill_ids
        )

    async def deactivate_record(self, skill_id: str) -> bool:
        """Set a specific record's ``is_active`` to False."""
        return await asyncio.to_thread(self._deactivate_record_sync, skill_id)

    async def reactivate_record(self, skill_id: str) -> bool:
        """Set a specific record's ``is_active`` to True (revert / rollback)."""
        return await asyncio.to_thread(self._reactivate_record_sync, skill_id)

    async def delete_record(self, skill_id: str) -> bool:
        """Delete a skill and all related data (CASCADE)."""
        return await asyncio.to_thread(self._delete_record_sync, skill_id)

    # Sync write implementations (thread-safe via self._mu)
    @_db_retry()
    def _save_record_sync(self, record: SkillRecord) -> None:
        self._ensure_open()
        with self._mu:
            self._conn.execute("BEGIN")
            try:
                self._upsert(record)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @_db_retry()
    def _save_records_sync(self, records: List[SkillRecord]) -> None:
        self._ensure_open()
        with self._mu:
            self._conn.execute("BEGIN")
            try:
                for r in records:
                    self._upsert(r)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @_db_retry()
    def _record_analysis_sync(self, analysis: ExecutionAnalysis) -> None:
        """Persist an analysis and update skill quality counters.

        ``SkillJudgment.skill_id`` is the **true skill_id** (e.g.
        ``weather__imp_a1b2c3d4``), the same identifier used as the DB
        primary key.  The analysis LLM receives skill_ids in its prompt
        and outputs them verbatim.

        We update counters via ``WHERE skill_id = ?`` — exact match, no
        ambiguity.
        """
        self._ensure_open()
        with self._mu:
            self._conn.execute("BEGIN")
            try:
                analysis_id = self._insert_analysis(analysis)

                now_iso = datetime.now().isoformat()
                for j in analysis.skill_judgments:
                    applied = 1 if j.skill_applied else 0
                    completed = (
                        1
                        if (j.skill_applied and analysis.task_completed)
                        else 0
                    )
                    fallback = (
                        1
                        if (not j.skill_applied and not analysis.task_completed)
                        else 0
                    )
                    self._conn.execute(
                        """
                        UPDATE skill_records SET
                            total_selections  = total_selections + 1,
                            total_applied     = total_applied + ?,
                            total_completions = total_completions + ?,
                            total_fallbacks   = total_fallbacks + ?,
                            last_updated      = ?
                        WHERE skill_id = ?
                        """,
                        (applied, completed, fallback, now_iso, j.skill_id),
                    )

                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @_db_retry()
    def _evolve_skill_sync(
        self,
        new_record: SkillRecord,
        parent_skill_ids: List[str],
    ) -> None:
        """Atomic: insert new version + deactivate parents (for FIXED)."""
        self._ensure_open()
        with self._mu:
            self._conn.execute("BEGIN")
            try:
                # For FIXED: deactivate same-name parents
                if new_record.lineage.origin == SkillOrigin.FIXED:
                    for pid in parent_skill_ids:
                        self._conn.execute(
                            "UPDATE skill_records SET is_active=0, "
                            "last_updated=? WHERE skill_id=?",
                            (datetime.now().isoformat(), pid),
                        )

                # Ensure new record has parent refs set
                new_record.lineage.parent_skill_ids = list(parent_skill_ids)
                new_record.is_active = True

                self._upsert(new_record)
                self._conn.commit()

                origin = new_record.lineage.origin.value
                logger.info(
                    f"evolve_skill ({origin}): "
                    f"{new_record.name}@gen{new_record.lineage.generation} "
                    f"[{new_record.skill_id}] ← parents={parent_skill_ids}"
                )
            except Exception:
                self._conn.rollback()
                raise

    @_db_retry()
    def _deactivate_record_sync(self, skill_id: str) -> bool:
        self._ensure_open()
        with self._mu:
            cur = self._conn.execute(
                "UPDATE skill_records SET is_active=0, last_updated=? "
                "WHERE skill_id=?",
                (datetime.now().isoformat(), skill_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    @_db_retry()
    def _reactivate_record_sync(self, skill_id: str) -> bool:
        self._ensure_open()
        with self._mu:
            cur = self._conn.execute(
                "UPDATE skill_records SET is_active=1, last_updated=? "
                "WHERE skill_id=?",
                (datetime.now().isoformat(), skill_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    @_db_retry()
    def _delete_record_sync(self, skill_id: str) -> bool:
        self._ensure_open()
        with self._mu:
            # ON DELETE CASCADE automatically cleans up lineage_parents / deps / tags
            # skill_judgments are NOT cascade-deleted (no FK to skill_records)
            cur = self._conn.execute(
                "DELETE FROM skill_records WHERE skill_id=?", (skill_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    # Read API (sync, each call opens its own read-only conn)
    @_db_retry()
    def load_record(self, skill_id: str) -> Optional[SkillRecord]:
        """Load a single :class:`SkillRecord` by id."""
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM skill_records WHERE skill_id=?",
                (skill_id,),
            ).fetchone()
            return self._to_record(conn, row) if row else None

    @_db_retry()
    def load_all(
        self, *, active_only: bool = False
    ) -> Dict[str, SkillRecord]:
        """Load skill records, keyed by ``skill_id``.

        Args:
            active_only: If True, only return records with ``is_active=True``.
        """
        with self._reader() as conn:
            if active_only:
                rows = conn.execute(
                    "SELECT * FROM skill_records WHERE is_active=1"
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM skill_records").fetchall()
            result: Dict[str, SkillRecord] = {}
            for row in rows:
                rec = self._to_record(conn, row)
                result[rec.skill_id] = rec
            logger.info(f"Loaded {len(result)} skill records (active_only={active_only})")
            return result

    @_db_retry()
    def load_active(self) -> Dict[str, SkillRecord]:
        """Load only active skill records, keyed by ``skill_id``.

        Convenience wrapper for ``load_all(active_only=True)``.
        """
        return self.load_all(active_only=True)

    @_db_retry()
    def load_record_by_path(self, skill_dir: str) -> Optional[SkillRecord]:
        """Load the most recent active SkillRecord whose ``path`` is inside *skill_dir*.

        Used by ``upload_skill`` to retrieve pre-computed upload metadata
        (origin, parents, change_summary, etc.) from the DB when
        ``.upload_meta.json`` is missing.

        The match uses ``path LIKE '{skill_dir}%'`` so both
        ``/a/b/SKILL.md`` and ``/a/b/scenarios/x.md`` match ``/a/b``.
        Returns the newest active record (by ``last_updated DESC``).
        """
        normalized = skill_dir.rstrip("/")
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM skill_records "
                "WHERE path LIKE ? AND is_active=1 "
                "ORDER BY last_updated DESC LIMIT 1",
                (f"{normalized}%",),
            ).fetchone()
            return self._to_record(conn, row) if row else None

    @_db_retry()
    def get_versions(self, name: str) -> List[SkillRecord]:
        """Load all versions of a named skill (active + inactive), sorted by generation."""
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_records WHERE name=? "
                "ORDER BY lineage_generation ASC",
                (name,),
            ).fetchall()
            return [self._to_record(conn, r) for r in rows]

    @_db_retry()
    def load_by_category(
        self, category: SkillCategory, *, active_only: bool = True
    ) -> List[SkillRecord]:
        """Load skill records filtered by category.

        Args:
            active_only: If True (default), only return active records.
        """
        with self._reader() as conn:
            if active_only:
                rows = conn.execute(
                    "SELECT * FROM skill_records "
                    "WHERE category=? AND is_active=1",
                    (category.value,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM skill_records WHERE category=?",
                    (category.value,),
                ).fetchall()
            return [self._to_record(conn, r) for r in rows]

    @_db_retry()
    def load_analyses(
        self,
        skill_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[ExecutionAnalysis]:
        """Load recent analyses.

        Args:
            skill_id: True ``skill_id`` (e.g. ``weather__imp_a1b2c3d4``).
                ``skill_judgments.skill_id`` now stores the true skill_id,
                so filtering uses exact match.
                If None, return pure-execution analyses (no judgments).
        """
        with self._reader() as conn:
            if skill_id is not None:
                rows = conn.execute(
                    "SELECT ea.* FROM execution_analyses ea "
                    "JOIN skill_judgments sj ON ea.id = sj.analysis_id "
                    "WHERE sj.skill_id = ? "
                    "ORDER BY ea.timestamp DESC LIMIT ?",
                    (skill_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT ea.* FROM execution_analyses ea "
                    "LEFT JOIN skill_judgments sj ON ea.id = sj.analysis_id "
                    "WHERE sj.id IS NULL "
                    "ORDER BY ea.timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._to_analysis(conn, r) for r in reversed(rows)]

    @_db_retry()
    def load_analyses_for_task(
        self, task_id: str
    ) -> Optional[ExecutionAnalysis]:
        """Load the analysis for a specific task, or None."""
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM execution_analyses WHERE task_id=?",
                (task_id,),
            ).fetchone()
            return self._to_analysis(conn, row) if row else None

    @_db_retry()
    def load_all_analyses(self, limit: int = 200) -> List[ExecutionAnalysis]:
        """Load recent analyses across all tasks."""
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM execution_analyses "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._to_analysis(conn, r) for r in reversed(rows)]

    @_db_retry()
    def load_evolution_candidates(
        self, limit: int = 50, *, include_processed: bool = False,
    ) -> List[ExecutionAnalysis]:
        """Load analyses marked as evolution candidates.

        Args:
            limit: Max number of records.
            include_processed: If False (default), only return candidates
                whose suggestions have NOT been processed yet.
        """
        with self._reader() as conn:
            if include_processed:
                where = "WHERE candidate_for_evolution=1"
            else:
                where = (
                    "WHERE candidate_for_evolution=1 "
                    "AND evolution_processed_at IS NULL"
                )
            rows = conn.execute(
                f"SELECT * FROM execution_analyses {where} "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._to_analysis(conn, r) for r in reversed(rows)]

    @_db_retry()
    def mark_evolution_processed(self, task_id: str) -> None:
        """Mark an analysis's evolution suggestions as processed."""
        with self._mu:
            self._conn.execute(
                "UPDATE execution_analyses "
                "SET evolution_processed_at=? WHERE task_id=?",
                (datetime.now().isoformat(), task_id),
            )
            self._conn.commit()

    @_db_retry()
    def find_skills_by_tool(self, tool_key: str) -> List[str]:
        """
        Only returns active records — deactivated (superseded) versions
        are excluded so that Trigger 2 never re-processes old versions.
        """
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT sd.skill_id "
                "FROM skill_tool_deps sd "
                "JOIN skill_records sr ON sd.skill_id = sr.skill_id "
                "WHERE sd.tool_key=? AND sr.is_active=1",
                (tool_key,),
            ).fetchall()
            return [r["skill_id"] for r in rows]

    @_db_retry()
    def find_children(self, parent_skill_id: str) -> List[str]:
        """Find skill_ids derived from the given parent."""
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT skill_id FROM skill_lineage_parents "
                "WHERE parent_skill_id=?",
                (parent_skill_id,),
            ).fetchall()
            return [r["skill_id"] for r in rows]

    @_db_retry()
    def count(self, *, active_only: bool = False) -> int:
        """Total number of skill records."""
        with self._reader() as conn:
            if active_only:
                return conn.execute(
                    "SELECT COUNT(*) FROM skill_records WHERE is_active=1"
                ).fetchone()[0]
            return conn.execute(
                "SELECT COUNT(*) FROM skill_records"
            ).fetchone()[0]

    # Analytics / Summary
    @_db_retry()
    def get_summary(self, *, active_only: bool = True) -> List[Dict[str, Any]]:
        """Lightweight summary of skills (no analyses/deps loaded).

        Default filters to active skills only.
        """
        with self._reader() as conn:
            where = "WHERE is_active=1 " if active_only else ""
            rows = conn.execute(
                f"""
                SELECT skill_id, name, description, category, is_active,
                       visibility, creator_id,
                       lineage_origin, lineage_generation,
                       total_selections, total_applied,
                       total_completions, total_fallbacks,
                       first_seen, last_updated
                FROM skill_records
                {where}
                ORDER BY last_updated DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    @_db_retry()
    def get_stats(self, *, active_only: bool = True) -> Dict[str, Any]:
        """Aggregate statistics across skills."""
        with self._reader() as conn:
            where = " WHERE is_active=1" if active_only else ""
            total = conn.execute(
                f"SELECT COUNT(*) FROM skill_records{where}"
            ).fetchone()[0]

            by_category = {
                r["category"]: r["cnt"]
                for r in conn.execute(
                    f"SELECT category, COUNT(*) AS cnt "
                    f"FROM skill_records{where} GROUP BY category"
                ).fetchall()
            }
            by_origin = {
                r["lineage_origin"]: r["cnt"]
                for r in conn.execute(
                    f"SELECT lineage_origin, COUNT(*) AS cnt "
                    f"FROM skill_records{where} GROUP BY lineage_origin"
                ).fetchall()
            }
            n_analyses = conn.execute(
                "SELECT COUNT(*) FROM execution_analyses"
            ).fetchone()[0]
            n_candidates = conn.execute(
                "SELECT COUNT(*) FROM execution_analyses "
                "WHERE candidate_for_evolution=1"
            ).fetchone()[0]
            n_unprocessed_candidates = conn.execute(
                "SELECT COUNT(*) FROM execution_analyses "
                "WHERE candidate_for_evolution=1 "
                "AND evolution_processed_at IS NULL"
            ).fetchone()[0]
            agg = conn.execute(
                f"""
                SELECT SUM(total_selections)  AS sel,
                       SUM(total_applied)      AS app,
                       SUM(total_completions)  AS comp,
                       SUM(total_fallbacks)    AS fb
                FROM skill_records{where}
                """
            ).fetchone()

            # Also report total (including inactive) for context
            total_all = conn.execute(
                "SELECT COUNT(*) FROM skill_records"
            ).fetchone()[0]

            return {
                "total_skills": total,
                "total_skills_all": total_all,
                "by_category": by_category,
                "by_origin": by_origin,
                "total_analyses": n_analyses,
                "evolution_candidates": n_candidates,
                "unprocessed_evolution_candidates": n_unprocessed_candidates,
                "total_selections": agg["sel"] or 0,
                "total_applied": agg["app"] or 0,
                "total_completions": agg["comp"] or 0,
                "total_fallbacks": agg["fb"] or 0,
            }

    @_db_retry()
    def get_task_skill_summary(self, task_id: str) -> Dict[str, Any]:
        """Per-task summary: task-level fields + per-skill judgments.

        Useful for understanding how multiple skills contributed to a
        single task execution.

        Returns:
            dict: ``{"task_id", "task_completed", "execution_note",
                "tool_issues", "judgments": [{skill_id, skill_applied, note}],
                ...}`` or empty dict if the task has no analysis.
        """
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM execution_analyses WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if not row:
                return {}

            judgment_rows = conn.execute(
                "SELECT skill_id, skill_applied, note "
                "FROM skill_judgments WHERE analysis_id=?",
                (row["id"],),
            ).fetchall()

            try:
                evo_suggestions = json.loads(row["evolution_suggestions"] or "[]")
            except json.JSONDecodeError:
                evo_suggestions = []

            return {
                "task_id": row["task_id"],
                "timestamp": row["timestamp"],
                "task_completed": bool(row["task_completed"]),
                "execution_note": row["execution_note"],
                "tool_issues": json.loads(row["tool_issues"]),
                "candidate_for_evolution": bool(row["candidate_for_evolution"]),
                "evolution_suggestions": evo_suggestions,
                "analyzed_by": row["analyzed_by"],
                "judgments": [
                    {
                        "skill_id": jr["skill_id"],
                        "skill_applied": bool(jr["skill_applied"]),
                        "note": jr["note"],
                    }
                    for jr in judgment_rows
                ],
            }

    @_db_retry()
    def get_top_skills(
        self,
        n: int = 10,
        metric: str = "effective_rate",
        min_selections: int = 1,
        *,
        active_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """Top-N skills ranked by the chosen metric.

        Metrics:
            ``effective_rate``  — completions / selections
            ``applied_rate``    — applied / selections
            ``completion_rate`` — completions / applied
            ``total_selections``— raw count
        """
        rate_exprs = {
            "effective_rate": (
                "CAST(total_completions AS REAL) / total_selections"
            ),
            "applied_rate": (
                "CAST(total_applied AS REAL) / total_selections"
            ),
            "completion_rate": (
                "CASE WHEN total_applied > 0 "
                "THEN CAST(total_completions AS REAL) / total_applied "
                "ELSE 0.0 END"
            ),
            "total_selections": "total_selections",
        }
        expr = rate_exprs.get(metric, rate_exprs["effective_rate"])
        active_clause = " AND is_active=1" if active_only else ""

        with self._reader() as conn:
            rows = conn.execute(
                f"SELECT *, ({expr}) AS _rank "
                f"FROM skill_records "
                f"WHERE total_selections >= ?{active_clause} "
                f"ORDER BY _rank DESC LIMIT ?",
                (min_selections, n),
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d.pop("_rank", None)
                results.append(d)
            return results

    @_db_retry()
    def get_count_and_timestamp(
        self, *, active_only: bool = True
    ) -> Dict[str, Any]:
        """Skill count + newest ``last_updated`` for cheap change detection."""
        with self._reader() as conn:
            where = " WHERE is_active=1" if active_only else ""
            row = conn.execute(
                f"SELECT COUNT(*) AS cnt, MAX(last_updated) AS max_ts "
                f"FROM skill_records{where}"
            ).fetchone()
            return {
                "count": row["cnt"] if row else 0,
                "max_last_updated": row["max_ts"] if row else None,
            }

    # Lineage / Ancestry
    @_db_retry()
    def get_ancestry(
        self, skill_id: str, max_depth: int = 10
    ) -> List[SkillRecord]:
        """Walk up the lineage tree; returns ancestors oldest-first."""
        with self._reader() as conn:
            visited: set[str] = set()
            ancestors: List[SkillRecord] = []
            frontier = [skill_id]

            for _ in range(max_depth):
                next_frontier: List[str] = []
                for sid in frontier:
                    for pr in conn.execute(
                        "SELECT parent_skill_id "
                        "FROM skill_lineage_parents WHERE skill_id=?",
                        (sid,),
                    ).fetchall():
                        pid = pr["parent_skill_id"]
                        if pid in visited:
                            continue
                        visited.add(pid)
                        row = conn.execute(
                            "SELECT * FROM skill_records WHERE skill_id=?",
                            (pid,),
                        ).fetchone()
                        if row:
                            ancestors.append(self._to_record(conn, row))
                            next_frontier.append(pid)
                frontier = next_frontier
                if not frontier:
                    break

            ancestors.sort(key=lambda r: r.lineage.generation)
            return ancestors

    @_db_retry()
    def get_lineage_tree(
        self, skill_id: str, max_depth: int = 5
    ) -> Dict[str, Any]:
        """Build a JSON-friendly tree rooted at *skill_id* (downward)."""
        with self._reader() as conn:
            return self._subtree(conn, skill_id, max_depth, set())

    def _subtree(
        self,
        conn: sqlite3.Connection,
        sid: str,
        depth: int,
        visited: set,
    ) -> Dict[str, Any]:
        visited.add(sid)
        row = conn.execute(
            "SELECT skill_id, name, lineage_generation, lineage_origin, is_active "
            "FROM skill_records WHERE skill_id=?",
            (sid,),
        ).fetchone()
        node: Dict[str, Any] = {
            "skill_id": sid,
            "name": row["name"] if row else "?",
            "generation": row["lineage_generation"] if row else -1,
            "origin": row["lineage_origin"] if row else "unknown",
            "is_active": bool(row["is_active"]) if row else False,
            "children": [],
        }
        if depth <= 0:
            return node
        for cr in conn.execute(
            "SELECT skill_id FROM skill_lineage_parents "
            "WHERE parent_skill_id=?",
            (sid,),
        ).fetchall():
            cid = cr["skill_id"]
            if cid not in visited:
                node["children"].append(
                    self._subtree(conn, cid, depth - 1, visited)
                )
        return node

    # Maintenance
    def clear(self) -> None:
        """Delete all data (keeps schema)."""
        self._ensure_open()
        with self._mu:
            self._conn.execute("BEGIN")
            try:
                # CASCADE on skill_records cleans up: lineage_parents, tool_deps, tags
                self._conn.execute("DELETE FROM skill_records")
                # execution_analyses CASCADE cleans up skill_judgments
                self._conn.execute("DELETE FROM execution_analyses")
                self._conn.commit()
                logger.info("SkillStore cleared")
            except Exception:
                self._conn.rollback()
                raise

    def vacuum(self) -> None:
        """Compact the database file."""
        self._ensure_open()
        with self._mu:
            self._conn.execute("VACUUM")

    # Internal: Upsert / Insert / Deserialize
    def _upsert(self, record: SkillRecord) -> None:
        """Insert or update skill_records + sync related rows.

        Called within a transaction holding ``self._mu``.
        """
        lin = record.lineage
        # content_snapshot is Dict[str, str]; store as JSON text
        snapshot_json = json.dumps(
            lin.content_snapshot, ensure_ascii=False
        )
        self._conn.execute(
            """
            INSERT INTO skill_records (
                skill_id, name, description, path, is_active, category,
                visibility, creator_id,
                lineage_origin, lineage_generation,
                lineage_source_task_id, lineage_change_summary,
                lineage_content_diff, lineage_content_snapshot,
                lineage_created_at, lineage_created_by,
                total_selections, total_applied,
                total_completions, total_fallbacks,
                first_seen, last_updated
            ) VALUES (?,?,?,?,?,?, ?,?, ?,?, ?,?, ?,?, ?,?, ?,?,?,?, ?,?)
            ON CONFLICT(skill_id) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                path=excluded.path,
                is_active=excluded.is_active,
                category=excluded.category,
                visibility=excluded.visibility,
                creator_id=excluded.creator_id,
                lineage_origin=excluded.lineage_origin,
                lineage_generation=excluded.lineage_generation,
                lineage_source_task_id=excluded.lineage_source_task_id,
                lineage_change_summary=excluded.lineage_change_summary,
                lineage_content_diff=excluded.lineage_content_diff,
                lineage_content_snapshot=excluded.lineage_content_snapshot,
                lineage_created_at=excluded.lineage_created_at,
                lineage_created_by=excluded.lineage_created_by,
                total_selections=excluded.total_selections,
                total_applied=excluded.total_applied,
                total_completions=excluded.total_completions,
                total_fallbacks=excluded.total_fallbacks,
                last_updated=excluded.last_updated
            """,
            (
                record.skill_id,
                record.name,
                record.description,
                record.path,
                int(record.is_active),
                record.category.value,
                record.visibility.value,
                record.creator_id,
                lin.origin.value,
                lin.generation,
                lin.source_task_id,
                lin.change_summary,
                lin.content_diff,
                snapshot_json,
                lin.created_at.isoformat(),
                lin.created_by,
                record.total_selections,
                record.total_applied,
                record.total_completions,
                record.total_fallbacks,
                record.first_seen.isoformat(),
                record.last_updated.isoformat(),
            ),
        )

        # Sync lineage parents
        self._conn.execute(
            "DELETE FROM skill_lineage_parents WHERE skill_id=?",
            (record.skill_id,),
        )
        for pid in lin.parent_skill_ids:
            self._conn.execute(
                "INSERT INTO skill_lineage_parents"
                "(skill_id, parent_skill_id) VALUES(?,?)",
                (record.skill_id, pid),
            )

        # Sync tool dependencies
        self._conn.execute(
            "DELETE FROM skill_tool_deps WHERE skill_id=?",
            (record.skill_id,),
        )
        critical_set = set(record.critical_tools)
        for tk in record.tool_dependencies:
            self._conn.execute(
                "INSERT INTO skill_tool_deps"
                "(skill_id, tool_key, critical) VALUES(?,?,?)",
                (record.skill_id, tk, 1 if tk in critical_set else 0),
            )

        # Sync tags
        self._conn.execute(
            "DELETE FROM skill_tags WHERE skill_id=?",
            (record.skill_id,),
        )
        for tag in record.tags:
            self._conn.execute(
                "INSERT INTO skill_tags(skill_id, tag) VALUES(?,?)",
                (record.skill_id, tag),
            )

        # Sync analyses (insert only NEW ones, dedup by task_id)
        for a in record.recent_analyses:
            existing = self._conn.execute(
                "SELECT id FROM execution_analyses WHERE task_id=?",
                (a.task_id,),
            ).fetchone()
            if existing is None:
                self._insert_analysis(a)

    def _insert_analysis(self, a: ExecutionAnalysis) -> int:
        """Insert an execution_analyses row + its skill_judgments.

        Called within a transaction holding ``self._mu``.

        Returns:
            int: The ``execution_analyses.id`` of the newly inserted row.
        """
        cur = self._conn.execute(
            """
            INSERT INTO execution_analyses (
                task_id, timestamp,
                task_completed, execution_note,
                tool_issues, candidate_for_evolution,
                evolution_suggestions, analyzed_by, analyzed_at
            ) VALUES (?,?, ?,?, ?,?, ?,?,?)
            """,
            (
                a.task_id,
                a.timestamp.isoformat(),
                int(a.task_completed),
                a.execution_note,
                json.dumps(a.tool_issues, ensure_ascii=False),
                int(a.candidate_for_evolution),
                json.dumps(
                    [s.to_dict() for s in a.evolution_suggestions],
                    ensure_ascii=False,
                ),
                a.analyzed_by,
                a.analyzed_at.isoformat(),
            ),
        )
        analysis_id = cur.lastrowid

        for j in a.skill_judgments:
            self._conn.execute(
                "INSERT INTO skill_judgments "
                "(analysis_id, skill_id, skill_applied, note) "
                "VALUES (?,?,?,?)",
                (analysis_id, j.skill_id, int(j.skill_applied), j.note),
            )

        return analysis_id

    # Deserialization
    def _to_record(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> SkillRecord:
        """Deserialize a skill_records row + related rows → SkillRecord."""
        sid = row["skill_id"]

        parents = [
            r["parent_skill_id"]
            for r in conn.execute(
                "SELECT parent_skill_id "
                "FROM skill_lineage_parents WHERE skill_id=?",
                (sid,),
            ).fetchall()
        ]

        # Deserialize content_snapshot: stored as JSON dict
        # mapping relative file paths to their text content
        raw_snapshot = row["lineage_content_snapshot"] or "{}"
        snapshot: Dict[str, str] = json.loads(raw_snapshot)

        lineage = SkillLineage(
            origin=SkillOrigin(row["lineage_origin"]),
            generation=row["lineage_generation"],
            parent_skill_ids=parents,
            source_task_id=row["lineage_source_task_id"],
            change_summary=row["lineage_change_summary"],
            content_diff=row["lineage_content_diff"],
            content_snapshot=snapshot,
            created_at=datetime.fromisoformat(row["lineage_created_at"]),
            created_by=row["lineage_created_by"],
        )

        dep_rows = conn.execute(
            "SELECT tool_key, critical "
            "FROM skill_tool_deps WHERE skill_id=?",
            (sid,),
        ).fetchall()

        tag_rows = conn.execute(
            "SELECT tag FROM skill_tags WHERE skill_id=?", (sid,)
        ).fetchall()

        # Load recent analyses involving this skill (via skill_judgments).
        # skill_judgments.skill_id stores the true skill_id (same as DB PK).
        analysis_rows = conn.execute(
            "SELECT ea.* FROM execution_analyses ea "
            "JOIN skill_judgments sj ON ea.id = sj.analysis_id "
            "WHERE sj.skill_id = ? "
            "ORDER BY ea.timestamp DESC LIMIT ?",
            (sid, SkillRecord.MAX_RECENT),
        ).fetchall()

        return SkillRecord(
            skill_id=sid,
            name=row["name"],
            description=row["description"],
            path=row["path"],
            is_active=bool(row["is_active"]),
            category=SkillCategory(row["category"]),
            tags=[r["tag"] for r in tag_rows],
            visibility=(
                SkillVisibility(row["visibility"])
                if row["visibility"] else SkillVisibility.PRIVATE
            ),
            creator_id=row["creator_id"] or "",
            lineage=lineage,
            tool_dependencies=[r["tool_key"] for r in dep_rows],
            critical_tools=[
                r["tool_key"] for r in dep_rows if r["critical"]
            ],
            total_selections=row["total_selections"],
            total_applied=row["total_applied"],
            total_completions=row["total_completions"],
            total_fallbacks=row["total_fallbacks"],
            recent_analyses=[
                self._to_analysis(conn, r) for r in reversed(analysis_rows)
            ],
            first_seen=datetime.fromisoformat(row["first_seen"]),
            last_updated=datetime.fromisoformat(row["last_updated"]),
        )

    @staticmethod
    def _to_analysis(
        conn: sqlite3.Connection, row: sqlite3.Row
    ) -> ExecutionAnalysis:
        """Deserialize an execution_analyses row + judgments → ExecutionAnalysis."""
        analysis_id = row["id"]

        judgment_rows = conn.execute(
            "SELECT skill_id, skill_applied, note "
            "FROM skill_judgments WHERE analysis_id=?",
            (analysis_id,),
        ).fetchall()

        suggestions: list[EvolutionSuggestion] = []
        raw_suggestions = row["evolution_suggestions"]
        if raw_suggestions:
            try:
                suggestions = [
                    EvolutionSuggestion.from_dict(s)
                    for s in json.loads(raw_suggestions)
                ]
            except (json.JSONDecodeError, KeyError, ValueError):
                pass

        return ExecutionAnalysis(
            task_id=row["task_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            task_completed=bool(row["task_completed"]),
            execution_note=row["execution_note"],
            tool_issues=json.loads(row["tool_issues"]),
            skill_judgments=[
                SkillJudgment(
                    skill_id=jr["skill_id"],
                    skill_applied=bool(jr["skill_applied"]),
                    note=jr["note"],
                )
                for jr in judgment_rows
            ],
            evolution_suggestions=suggestions,
            analyzed_by=row["analyzed_by"],
            analyzed_at=datetime.fromisoformat(row["analyzed_at"]),
        )
