"""
Storage location: <project_root>/.openspace/openspace.db
Tables:
  skill_records          — SkillRecord main table
  skill_lineage_parents  — Lineage parent-child relationships (many-to-many)
  execution_analyses     — ExecutionAnalysis records (one per task)
  skill_judgments         — Per-skill judgments within an analysis
  skill_trust_observations — Independent success/failure evidence for trust
  skill_tool_deps        — Tool dependencies
  skill_tags             — Auxiliary tags
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Generator, Iterable, List, Optional

from .patch import collect_skill_snapshot, compute_unified_diff
from .types import (
    EvolutionSuggestion,
    ExecutionAnalysis,
    SkillCategory,
    SkillJudgment,
    SkillLineage,
    SkillOrigin,
    SkillRecord,
    SkillTrustState,
    SkillVisibility,
)
from openspace.utils.logging import Logger
from openspace.config.constants import PROJECT_ROOT
from openspace.grounding.core.permissions.types import parse_rule_value

logger = Logger.get_logger(__name__)

_LIFECYCLE_EVENT_TYPES = (
    "listed",
    "discovered",
    "invoked",
    "applied",
    "completed",
    "fallback",
)

SkillEvidenceSink = Callable[[str, dict[str, Any]], Awaitable[None] | None]


def _normalize_tool_dependency_key(value: Any) -> Optional[str]:
    """Return the canonical quality-system key for a tool dependency."""

    if not isinstance(value, str):
        return None
    key = value.strip()
    if not key:
        return None

    parts = key.split(":", 2)
    if len(parts) == 3:
        backend, server, tool_name = (part.strip() for part in parts)
        if not backend or not tool_name:
            return None
        return f"{backend}:{server or 'default'}:{tool_name}"
    if len(parts) == 2:
        backend, tool_name = (part.strip() for part in parts)
        if not backend or not tool_name:
            return None
        return f"{backend}:default:{tool_name}"
    return None


def _skill_record_evidence_payload(
    record: SkillRecord,
    lifecycle_event: str,
    *,
    source: str = "skill_store",
) -> dict[str, Any]:
    lineage = record.lineage
    return {
        "skill_id": record.skill_id,
        "name": record.name,
        "description": record.description,
        "path": record.path,
        "is_active": bool(record.is_active),
        "enabled": bool(record.enabled),
        "trust_state": record.trust_state.value,
        "category": record.category.value,
        "tags": list(record.tags),
        "visibility": record.visibility.value,
        "creator_id": record.creator_id,
        "lineage_origin": lineage.origin.value,
        "lineage_generation": lineage.generation,
        "lineage_parent_skill_ids": list(lineage.parent_skill_ids),
        "lineage_source_task_id": lineage.source_task_id,
        "lineage_change_summary": lineage.change_summary,
        "lineage_evolution_action_id": lineage.evolution_action_id,
        "lineage_provenance_refs": list(lineage.provenance_refs),
        "lineage_created_at": lineage.created_at.isoformat(),
        "lineage_created_by": lineage.created_by,
        "tool_dependencies": list(record.tool_dependencies),
        "critical_tools": list(record.critical_tools),
        "total_selections": record.total_selections,
        "total_invocations": record.total_invocations,
        "total_applied": record.total_applied,
        "total_completions": record.total_completions,
        "total_fallbacks": record.total_fallbacks,
        "trust_successes": record.trust_successes,
        "trust_failures": record.trust_failures,
        "first_seen": record.first_seen.isoformat(),
        "last_updated": record.last_updated.isoformat(),
        "lifecycle_event": lifecycle_event,
        "source": source,
        "created_at": datetime.now().isoformat(),
    }


def _skill_meta_evidence_payload(
    meta: Any,
    lifecycle_event: str,
    *,
    path: str | None = None,
    source: str = "skill_store",
) -> dict[str, Any]:
    return {
        "skill_id": str(getattr(meta, "skill_id", "") or ""),
        "name": str(getattr(meta, "name", "") or ""),
        "description": str(getattr(meta, "description", "") or ""),
        "path": str(path or getattr(meta, "path", "") or ""),
        "allowed_tools": list(getattr(meta, "allowed_tools", []) or []),
        "tool_dependencies": _meta_allowed_tool_dependencies(meta),
        "lifecycle_event": lifecycle_event,
        "source": source,
        "created_at": datetime.now().isoformat(),
    }


def _tool_dependency_lookup_variants(tool_key: str) -> List[str]:
    """Return canonical and legacy variants for a tool dependency key."""

    variants: List[str] = []
    raw = tool_key.strip() if isinstance(tool_key, str) else ""
    canonical = _normalize_tool_dependency_key(raw)
    for key in (canonical, raw):
        if key and key not in variants:
            variants.append(key)

    if canonical:
        backend, server, tool_name = canonical.split(":", 2)
        if server == "default":
            legacy = f"{backend}:{tool_name}"
            if legacy not in variants:
                variants.append(legacy)
            if tool_name not in variants:
                variants.append(tool_name)
    return variants


def _tool_dependencies_from_allowed_tools(values: Iterable[Any]) -> List[str]:
    """Convert Skill Protocol allowed-tools rules into matchable dependency names."""

    deps: List[str] = []
    seen: set[str] = set()
    for value in values or []:
        raw = str(value or "").strip()
        if not raw:
            continue
        try:
            tool_name = parse_rule_value(raw).tool_name
        except Exception:
            tool_name = raw.split("(", 1)[0].strip()
        for candidate in (tool_name, raw):
            text = str(candidate or "").strip()
            if text and text not in seen:
                seen.add(text)
                deps.append(text)
    return deps


def _meta_allowed_tool_dependencies(meta: Any) -> List[str]:
    return _tool_dependencies_from_allowed_tools(
        getattr(meta, "allowed_tools", []) or []
    )


def _extract_tool_dependency_from_issue(issue: Any) -> Optional[str]:
    """Extract and canonicalize the leading tool key from an analysis issue."""

    if not isinstance(issue, str):
        return None
    if "—" in issue:
        key_part, _, _ = issue.partition("—")
    elif " - " in issue:
        key_part, _, _ = issue.partition(" - ")
    else:
        key_part = issue
    return _normalize_tool_dependency_key(key_part)


def _json_list_from_row(row: sqlite3.Row, key: str) -> list[str]:
    if key not in row.keys():
        return []
    try:
        loaded = json.loads(row[key] or "[]")
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in loaded if str(item)]


def _json_object_from_row(row: sqlite3.Row, key: str) -> dict[str, Any]:
    if key not in row.keys():
        return {}
    try:
        loaded = json.loads(row[key] or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}


def _category_for_meta_locked(conn: sqlite3.Connection, meta: Any) -> SkillCategory:
    """Resolve local/cloud classification for a discovered skill."""

    local_skill_id = str(getattr(meta, "skill_id", "") or "")
    if local_skill_id:
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='skill_local_classifications'"
            ).fetchone()
            if table is not None:
                row = conn.execute(
                    "SELECT category FROM skill_local_classifications "
                    "WHERE local_skill_id=?",
                    (local_skill_id,),
                ).fetchone()
                if row and row["category"]:
                    return SkillCategory(str(row["category"]))
        except Exception:
            pass
    try:
        from openspace.cloud.skill_classification import classify_skill_metadata

        classification = classify_skill_metadata(
            local_skill_id=local_skill_id,
            name=str(getattr(meta, "name", "") or ""),
            description=str(getattr(meta, "description", "") or ""),
            body="",
            allowed_tools=list(getattr(meta, "allowed_tools", []) or []),
            local_path=str(getattr(meta, "path", "") or ""),
            frontmatter=getattr(meta, "raw_frontmatter", {}) or {},
        )
        return SkillCategory(classification.category)
    except Exception:
        return SkillCategory.WORKFLOW


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
    enabled                INTEGER NOT NULL DEFAULT 1,
    trust_state            TEXT NOT NULL DEFAULT 'trusted',
    category               TEXT NOT NULL DEFAULT 'workflow',
    visibility             TEXT NOT NULL DEFAULT 'private',
    creator_id             TEXT NOT NULL DEFAULT '',
    lineage_origin         TEXT NOT NULL DEFAULT 'imported',
    lineage_revision_id    TEXT NOT NULL DEFAULT '',
    lineage_generation     INTEGER NOT NULL DEFAULT 0,
    lineage_parent_revision_ids_json TEXT NOT NULL DEFAULT '[]',
    lineage_source_task_id TEXT,
    lineage_change_summary TEXT NOT NULL DEFAULT '',
    lineage_content_hash   TEXT NOT NULL DEFAULT '',
    lineage_evolution_action_id TEXT,
    lineage_provenance_refs_json TEXT NOT NULL DEFAULT '[]',
    lineage_revision_metadata_json TEXT NOT NULL DEFAULT '{}',
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
    skill_phase_failed_skill_ids TEXT NOT NULL DEFAULT '[]',
    candidate_for_evolution INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS skill_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id    TEXT NOT NULL,
    skill_name  TEXT NOT NULL DEFAULT '',
    event_type  TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT '',
    task_id     TEXT NOT NULL DEFAULT '',
    turn_id     TEXT NOT NULL DEFAULT '',
    agent_id    TEXT NOT NULL DEFAULT '',
    query       TEXT NOT NULL DEFAULT '',
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_events_skill
    ON skill_events(skill_id, event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_skill_events_task
    ON skill_events(task_id);

CREATE TABLE IF NOT EXISTS skill_trust_observations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id           TEXT NOT NULL
        REFERENCES skill_records(skill_id) ON DELETE CASCADE,
    observation_id     TEXT NOT NULL,
    task_id            TEXT NOT NULL DEFAULT '',
    session_id         TEXT NOT NULL DEFAULT '',
    outcome            TEXT NOT NULL CHECK(outcome IN ('success', 'failure')),
    source             TEXT NOT NULL DEFAULT '',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    created_at         TEXT NOT NULL,
    UNIQUE(skill_id, observation_id)
);
CREATE INDEX IF NOT EXISTS idx_skill_trust_outcome
    ON skill_trust_observations(skill_id, outcome, id);
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

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        trust_promotion_min_independent_successes: int = 2,
    ) -> None:
        if db_path is None:
            db_dir = PROJECT_ROOT / ".openspace"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = db_dir / "openspace.db"

        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._mu = threading.Lock()
        self._closed = False
        self._evidence_sink: SkillEvidenceSink | None = None
        self.trust_promotion_min_independent_successes = max(
            1,
            int(trust_promotion_min_independent_successes or 2),
        )

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
            self._ensure_column_locked(
                "skill_records",
                "lineage_evolution_action_id",
                "TEXT",
            )
            self._ensure_column_locked(
                "skill_records",
                "enabled",
                "INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column_locked(
                "skill_records",
                "trust_state",
                "TEXT NOT NULL DEFAULT 'trusted'",
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sr_enabled "
                "ON skill_records(enabled)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sr_trust "
                "ON skill_records(trust_state)"
            )
            self._ensure_column_locked(
                "skill_records",
                "lineage_revision_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column_locked(
                "skill_records",
                "lineage_parent_revision_ids_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column_locked(
                "skill_records",
                "lineage_content_hash",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column_locked(
                "skill_records",
                "lineage_provenance_refs_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column_locked(
                "skill_records",
                "lineage_revision_metadata_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column_locked(
                "execution_analyses",
                "skill_phase_failed_skill_ids",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column_locked(
                "skill_events",
                "skill_name",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column_locked(
                "skill_events",
                "turn_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column_locked(
                "skill_events",
                "agent_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_skill_events_agent "
                "ON skill_events(agent_id, turn_id)"
            )
            self._conn.commit()

    def _ensure_column_locked(
        self,
        table: str,
        column: str,
        ddl: str,
    ) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

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

    def set_evidence_sink(self, sink: SkillEvidenceSink | None) -> None:
        self._evidence_sink = sink

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SkillStore is closed")

    # Write API (async, offloaded via asyncio.to_thread)
    async def save_record(self, record: SkillRecord) -> None:
        """Upsert a single :class:`SkillRecord`."""
        payload = await asyncio.to_thread(self._save_record_sync, record)
        await self._emit_evidence("skill_record", payload)

    async def save_records(self, records: List[SkillRecord]) -> None:
        """Batch upsert in a single transaction."""
        payloads = await asyncio.to_thread(self._save_records_sync, records)
        for payload in payloads or []:
            await self._emit_evidence("skill_record", payload)

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
        created, payloads = await asyncio.to_thread(
            self._sync_from_registry_sync, discovered_skills,
        )
        for payload in payloads or []:
            await self._emit_evidence("skill_record", payload)
        return created

    async def record_skill_selection(
        self,
        skill_ids: List[str],
        *,
        source: str = "",
        task_id: str = "",
        turn_id: str = "",
        agent_id: str = "",
        query: str = "",
    ) -> None:
        """Record factual skill selection/retrieval immediately.

        Selection is an observable event independent of post-run LLM analysis.
        Keeping this counter here means selection stats remain correct even when
        recording is disabled or the analyzer fails to produce a judgment.
        """
        rows = await asyncio.to_thread(
            self._record_skill_selection_sync,
            skill_ids,
            source,
            task_id,
            turn_id,
            agent_id,
            query,
        )
        for row in rows or []:
            await self._emit_evidence("skill_event", row)

    def record_skill_selection_now(
        self,
        skill_ids: List[str],
        *,
        source: str = "",
        task_id: str = "",
        turn_id: str = "",
        agent_id: str = "",
        query: str = "",
    ) -> None:
        """Synchronous variant for non-async prompt/attachment builders."""

        rows = self._record_skill_selection_sync(
            skill_ids,
            source,
            task_id,
            turn_id,
            agent_id,
            query,
        )
        for row in rows or []:
            self._emit_evidence_now("skill_event", row)

    async def record_skill_event(
        self,
        skill_id: str,
        event_type: str,
        *,
        source: str = "",
        task_id: str = "",
        turn_id: str = "",
        agent_id: str = "",
        skill_name: str = "",
        query: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any] | None:
        """Record a factual skill lifecycle event.

        New schema used by OpenSpace SkillTool accounting:
        listed/discovered/invoked/permission_granted/permission_denied/applied/
        completed/fallback/field_suggested/field_approved/field_rejected.
        """

        row = await asyncio.to_thread(
            self._record_skill_event_sync,
            skill_id,
            event_type,
            source,
            task_id,
            turn_id,
            agent_id,
            skill_name,
            query,
            metadata or {},
        )
        if row:
            await self._emit_evidence("skill_event", row)
        return row

    def record_skill_event_now(
        self,
        skill_id: str,
        event_type: str,
        *,
        source: str = "",
        task_id: str = "",
        turn_id: str = "",
        agent_id: str = "",
        skill_name: str = "",
        query: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any] | None:
        """Synchronous variant for non-async prompt/attachment builders."""

        row = self._record_skill_event_sync(
            skill_id,
            event_type,
            source,
            task_id,
            turn_id,
            agent_id,
            skill_name,
            query,
            metadata or {},
        )
        self._emit_evidence_now("skill_event", row)
        return row

    async def record_trust_observation(
        self,
        skill_id: str,
        observation_id: str,
        outcome: str,
        *,
        task_id: str = "",
        session_id: str = "",
        source: str = "",
        evidence_refs: Optional[Iterable[str]] = None,
    ) -> Optional[SkillRecord]:
        """Record one independent skill outcome and update its trust state."""

        result = await asyncio.to_thread(
            self._record_trust_observation_sync,
            skill_id,
            observation_id,
            outcome,
            task_id,
            session_id,
            source,
            list(evidence_refs or []),
        )
        for row in result.get("events", []):
            await self._emit_evidence("skill_event", row)
        payload = result.get("record_payload")
        if payload:
            await self._emit_evidence("skill_record", payload)
        return result.get("record")

    async def set_skill_enabled(self, skill_id: str, enabled: bool) -> bool:
        """Enable or disable a skill without changing revision activity."""

        changed, event_row, payload = await asyncio.to_thread(
            self._set_skill_enabled_sync,
            skill_id,
            bool(enabled),
        )
        if event_row:
            await self._emit_evidence("skill_event", event_row)
        if payload:
            await self._emit_evidence("skill_record", payload)
        return changed

    def is_skill_enabled(self, skill_id: str) -> bool:
        """Return whether a known skill is enabled; unknown skills fail open."""

        with self._reader() as conn:
            row = conn.execute(
                "SELECT enabled FROM skill_records WHERE skill_id=?",
                (skill_id,),
            ).fetchone()
            return True if row is None else bool(row["enabled"])

    async def _emit_evidence(self, event_type: str, payload: dict[str, Any]) -> None:
        sink = self._evidence_sink
        if sink is None or not payload:
            return
        try:
            result = sink(event_type, dict(payload))
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug("Skill evidence sink failed for %s", event_type, exc_info=True)

    def _emit_evidence_now(self, event_type: str, payload: dict[str, Any] | None) -> None:
        sink = self._evidence_sink
        if sink is None or not payload:
            return
        try:
            result = sink(event_type, dict(payload))
            if inspect.isawaitable(result):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()
                    return
                loop.create_task(result)
        except Exception:
            logger.debug("Skill evidence sink failed for %s", event_type, exc_info=True)

    @_db_retry()
    def _sync_from_registry_sync(
        self, discovered_skills: List[Any],
    ) -> tuple[int, list[dict[str, Any]]]:
        self._ensure_open()
        created = 0
        refreshed = 0
        evidence_payloads: list[dict[str, Any]] = []
        with self._mu:
            self._conn.execute("BEGIN")
            try:
                # Fetch all existing records keyed by skill_id
                rows = self._conn.execute(
                    "SELECT skill_id, name, description, path, category, "
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
                    allowed_tool_deps = _meta_allowed_tool_dependencies(meta)
                    classified_category = _category_for_meta_locked(self._conn, meta)

                    if meta.skill_id in existing:
                        # Refresh name/description if frontmatter changed,
                        # and backfill empty content_snapshot
                        row = existing[meta.skill_id]
                        updates: List[str] = []
                        params: list = []
                        dependency_refreshed = False

                        if row["name"] != meta.name:
                            updates.append("name=?")
                            params.append(meta.name)
                        if row["description"] != meta.description:
                            updates.append("description=?")
                            params.append(meta.description)
                        if row["category"] != classified_category.value:
                            updates.append("category=?")
                            params.append(classified_category.value)

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

                        if allowed_tool_deps:
                            existing_dep_rows = self._conn.execute(
                                "SELECT tool_key FROM skill_tool_deps WHERE skill_id=?",
                                (meta.skill_id,),
                            ).fetchall()
                            existing_deps = {
                                str(dep_row["tool_key"] or "")
                                for dep_row in existing_dep_rows
                            }
                            missing_deps = [
                                dep
                                for dep in allowed_tool_deps
                                if dep not in existing_deps
                            ]
                            for dep in missing_deps:
                                self._conn.execute(
                                    "INSERT INTO skill_tool_deps"
                                    "(skill_id, tool_key, critical) VALUES(?,?,?)",
                                    (meta.skill_id, dep, 0),
                                )
                            if missing_deps:
                                dependency_refreshed = True

                        if updates:
                            params.append(meta.skill_id)
                            self._conn.execute(
                                f"UPDATE skill_records SET {', '.join(updates)} "
                                f"WHERE skill_id=?",
                                params,
                            )
                        if updates or dependency_refreshed:
                            refreshed += 1
                            evidence_payloads.append(
                                _skill_meta_evidence_payload(
                                    meta,
                                    "registry_refreshed",
                                    path=row["path"] or path_str,
                                )
                            )
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
                        category=classified_category,
                        tool_dependencies=allowed_tool_deps,
                        lineage=SkillLineage(
                            origin=SkillOrigin.IMPORTED,
                            generation=0,
                            content_snapshot=snapshot,
                            content_diff=content_diff,
                        ),
                    )
                    self._upsert(record)
                    created += 1
                    evidence_payloads.append(
                        _skill_record_evidence_payload(
                            record,
                            "registry_created",
                        )
                    )
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
        return created, evidence_payloads

    @_db_retry()
    def _record_skill_selection_sync(
        self,
        skill_ids: List[str],
        source: str = "",
        task_id: str = "",
        turn_id: str = "",
        agent_id: str = "",
        query: str = "",
    ) -> list[dict[str, Any]]:
        self._ensure_open()
        selected_ids = [sid for sid in dict.fromkeys(skill_ids or []) if sid]
        if not selected_ids:
            return []

        emitted: list[dict[str, Any]] = []
        with self._mu:
            self._conn.execute("BEGIN")
            try:
                now_iso = datetime.now().isoformat()
                missing: List[str] = []
                for sid in selected_ids:
                    cur = self._conn.execute(
                        """
                        UPDATE skill_records SET
                            total_selections = total_selections + 1,
                            last_updated     = ?
                        WHERE skill_id = ?
                        """,
                        (now_iso, sid),
                    )
                    if cur.rowcount == 0:
                        missing.append(sid)
                    event_row = self._insert_skill_event_locked(
                        sid,
                        "selected",
                        source=source,
                        task_id=task_id,
                        turn_id=turn_id,
                        agent_id=agent_id,
                        query=query,
                        metadata={},
                    )
                    emitted.append(event_row)
                self._conn.commit()
                if missing:
                    logger.debug(
                        "record_skill_selection skipped unknown skill id(s) "
                        f"{missing} from source={source!r} task_id={task_id!r} "
                        f"query={query[:120]!r}"
                    )
            except Exception:
                self._conn.rollback()
                raise
        return emitted

    @_db_retry()
    def _record_skill_event_sync(
        self,
        skill_id: str,
        event_type: str,
        source: str = "",
        task_id: str = "",
        turn_id: str = "",
        agent_id: str = "",
        skill_name: str = "",
        query: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any] | None:
        self._ensure_open()
        if not skill_id or not event_type:
            return None
        with self._mu:
            row = self._insert_skill_event_locked(
                skill_id,
                event_type,
                source=source,
                task_id=task_id,
                turn_id=turn_id,
                agent_id=agent_id,
                skill_name=skill_name,
                query=query,
                metadata=metadata or {},
            )
            self._conn.commit()
            return row

    def _insert_skill_event_locked(
        self,
        skill_id: str,
        event_type: str,
        *,
        source: str = "",
        task_id: str = "",
        turn_id: str = "",
        agent_id: str = "",
        skill_name: str = "",
        query: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not skill_name:
            row = self._conn.execute(
                "SELECT name FROM skill_records WHERE skill_id = ?",
                (skill_id,),
            ).fetchone()
            if row is not None:
                skill_name = str(row["name"] or "")
        created_at = datetime.now().isoformat()
        cursor = self._conn.execute(
            """
            INSERT INTO skill_events
                (
                    skill_id, skill_name, event_type, source, task_id, turn_id,
                    agent_id, query, metadata, created_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_id,
                skill_name or "",
                event_type,
                source or "",
                task_id or "",
                turn_id or "",
                agent_id or "",
                query or "",
                json.dumps(metadata or {}, ensure_ascii=False),
                created_at,
            ),
        )
        row_id = int(cursor.lastrowid)
        return {
            "row_id": row_id,
            "skill_id": skill_id,
            "skill_name": skill_name or "",
            "event_type": event_type,
            "source": source or "",
            "task_id": task_id or "",
            "turn_id": turn_id or "",
            "agent_id": agent_id or "",
            "query": query or "",
            "metadata": metadata or {},
            "created_at": created_at,
        }

    @_db_retry()
    def _record_trust_observation_sync(
        self,
        skill_id: str,
        observation_id: str,
        outcome: str,
        task_id: str = "",
        session_id: str = "",
        source: str = "",
        evidence_refs: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        self._ensure_open()
        normalized_outcome = str(outcome or "").strip().lower()
        if normalized_outcome not in {"success", "failure"}:
            raise ValueError("trust observation outcome must be success or failure")
        normalized_observation_id = str(observation_id or "").strip()
        if not skill_id or not normalized_observation_id:
            raise ValueError("skill_id and observation_id are required")

        with self._mu:
            self._conn.execute("BEGIN")
            try:
                result = self._record_trust_observation_locked(
                    skill_id=skill_id,
                    observation_id=normalized_observation_id,
                    outcome=normalized_outcome,
                    task_id=task_id,
                    session_id=session_id,
                    source=source,
                    evidence_refs=evidence_refs or [],
                )
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def _record_trust_observation_locked(
        self,
        *,
        skill_id: str,
        observation_id: str,
        outcome: str,
        task_id: str,
        session_id: str,
        source: str,
        evidence_refs: List[str],
    ) -> Dict[str, Any]:
        record_row = self._conn.execute(
            "SELECT * FROM skill_records WHERE skill_id=?",
            (skill_id,),
        ).fetchone()
        if record_row is None:
            return {"record": None, "record_payload": None, "events": []}

        previous = self._conn.execute(
            "SELECT outcome FROM skill_trust_observations "
            "WHERE skill_id=? AND observation_id=?",
            (skill_id, observation_id),
        ).fetchone()
        previous_outcome = str(previous["outcome"] or "") if previous else ""
        normalized_refs = list(
            dict.fromkeys(str(ref).strip() for ref in evidence_refs if str(ref).strip())
        )
        now_iso = datetime.now().isoformat()
        self._conn.execute(
            """
            INSERT INTO skill_trust_observations (
                skill_id, observation_id, task_id, session_id, outcome,
                source, evidence_refs_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(skill_id, observation_id) DO UPDATE SET
                task_id=excluded.task_id,
                session_id=excluded.session_id,
                outcome=excluded.outcome,
                source=excluded.source,
                evidence_refs_json=excluded.evidence_refs_json,
                created_at=excluded.created_at
            """,
            (
                skill_id,
                observation_id,
                task_id or "",
                session_id or "",
                outcome,
                source or "",
                json.dumps(normalized_refs, ensure_ascii=False),
                now_iso,
            ),
        )

        counts = self._conn.execute(
            """
            SELECT
                SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) AS successes,
                SUM(CASE WHEN outcome='failure' THEN 1 ELSE 0 END) AS failures
            FROM skill_trust_observations WHERE skill_id=?
            """,
            (skill_id,),
        ).fetchone()
        successes = int(counts["successes"] or 0)
        failures = int(counts["failures"] or 0)
        last_failure_id = int(
            self._conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM skill_trust_observations "
                "WHERE skill_id=? AND outcome='failure'",
                (skill_id,),
            ).fetchone()[0]
            or 0
        )
        successes_since_failure = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM skill_trust_observations "
                "WHERE skill_id=? AND outcome='success' AND id>?",
                (skill_id, last_failure_id),
            ).fetchone()[0]
            or 0
        )

        previous_state = SkillTrustState(
            str(record_row["trust_state"] or SkillTrustState.TRUSTED.value)
        )
        next_state = previous_state
        if outcome == "failure":
            next_state = SkillTrustState.PROVISIONAL
        elif (
            previous_state == SkillTrustState.PROVISIONAL
            and successes_since_failure
            >= self.trust_promotion_min_independent_successes
        ):
            next_state = SkillTrustState.TRUSTED

        self._conn.execute(
            "UPDATE skill_records SET trust_state=?, last_updated=? WHERE skill_id=?",
            (next_state.value, now_iso, skill_id),
        )

        events: List[Dict[str, Any]] = []
        observation_changed = previous is None or previous_outcome != outcome
        event_metadata = {
            "observation_id": observation_id,
            "outcome": outcome,
            "session_id": session_id or "",
            "evidence_refs": normalized_refs,
            "trust_successes": successes,
            "trust_failures": failures,
            "successes_since_failure": successes_since_failure,
            "previous_trust_state": previous_state.value,
            "trust_state": next_state.value,
        }
        if observation_changed:
            events.append(
                self._insert_skill_event_locked(
                    skill_id,
                    "trust_observed",
                    source=source,
                    task_id=task_id,
                    metadata=event_metadata,
                )
            )
        if next_state != previous_state:
            transition = (
                "trust_promoted"
                if next_state == SkillTrustState.TRUSTED
                else "trust_demoted"
            )
            events.append(
                self._insert_skill_event_locked(
                    skill_id,
                    transition,
                    source=source,
                    task_id=task_id,
                    metadata=event_metadata,
                )
            )

        updated_row = self._conn.execute(
            "SELECT * FROM skill_records WHERE skill_id=?",
            (skill_id,),
        ).fetchone()
        record = self._to_record(self._conn, updated_row)
        return {
            "record": record,
            "record_payload": _skill_record_evidence_payload(
                record,
                "trust_updated",
            ),
            "events": events,
        }

    @_db_retry()
    def _set_skill_enabled_sync(
        self,
        skill_id: str,
        enabled: bool,
    ) -> tuple[bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        self._ensure_open()
        with self._mu:
            row = self._conn.execute(
                "SELECT enabled FROM skill_records WHERE skill_id=?",
                (skill_id,),
            ).fetchone()
            if row is None or bool(row["enabled"]) == enabled:
                return False, None, None
            now_iso = datetime.now().isoformat()
            self._conn.execute(
                "UPDATE skill_records SET enabled=?, last_updated=? WHERE skill_id=?",
                (int(enabled), now_iso, skill_id),
            )
            event_row = self._insert_skill_event_locked(
                skill_id,
                "enabled" if enabled else "disabled",
                source="skill_store",
                metadata={"enabled": enabled},
            )
            payload = self._skill_record_payload_from_db_locked(
                skill_id,
                "enabled" if enabled else "disabled",
            )
            self._conn.commit()
            return True, event_row, payload

    async def record_analysis(
        self,
        analysis: ExecutionAnalysis,
        observed_tool_keys: Optional[Iterable[str]] = None,
    ) -> None:
        """Atomic observation: insert analysis + judgments + increment counters.

        1. INSERT a row in ``execution_analyses`` (one per task).
        2. INSERT rows in ``skill_judgments`` for each skill assessed.
        3. For each judgment, atomically increment outcome counters:
           - total_applied     += 1         (if skill_applied)
           - total_completions += 1         (if applied and completed by this skill phase)
           - total_fallbacks   += 1         (if the selected skill did not complete the task)
           - last_updated = now

        ``total_selections`` is recorded at selection/retrieval time by
        ``record_skill_selection()``.  The analyzer may fail or omit a skill, so
        selection cannot safely be inferred from post-run judgments.

        ``observed_tool_keys`` comes from ``traj.jsonl``.  It is backfilled into
        ``skill_tool_deps`` together with LLM-reported ``tool_issues`` so tool
        degradation can find affected skills later.
        """
        trust_updates = await asyncio.to_thread(
            self._record_analysis_sync,
            analysis,
            list(observed_tool_keys or []),
        )
        for update in trust_updates or []:
            for row in update.get("events", []):
                await self._emit_evidence("skill_event", row)
            payload = update.get("record_payload")
            if payload:
                await self._emit_evidence("skill_record", payload)

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
        payloads = await asyncio.to_thread(
            self._evolve_skill_sync, new_record, parent_skill_ids
        )
        for payload in payloads or []:
            await self._emit_evidence("skill_record", payload)

    async def deactivate_record(self, skill_id: str) -> bool:
        """Set a specific record's ``is_active`` to False."""
        changed, payload = await asyncio.to_thread(self._deactivate_record_sync, skill_id)
        if payload:
            await self._emit_evidence("skill_record", payload)
        return changed

    async def reactivate_record(self, skill_id: str) -> bool:
        """Set a specific record's ``is_active`` to True (revert / rollback)."""
        changed, payload = await asyncio.to_thread(self._reactivate_record_sync, skill_id)
        if payload:
            await self._emit_evidence("skill_record", payload)
        return changed

    async def delete_record(self, skill_id: str) -> bool:
        """Delete a skill and all related data (CASCADE)."""
        changed, payload = await asyncio.to_thread(self._delete_record_sync, skill_id)
        if payload:
            await self._emit_evidence("skill_record", payload)
        return changed

    # Sync write implementations (thread-safe via self._mu)
    @_db_retry()
    def _save_record_sync(self, record: SkillRecord) -> dict[str, Any]:
        self._ensure_open()
        with self._mu:
            self._conn.execute("BEGIN")
            try:
                self._upsert(record)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return _skill_record_evidence_payload(record, "saved")

    @_db_retry()
    def _save_records_sync(self, records: List[SkillRecord]) -> list[dict[str, Any]]:
        self._ensure_open()
        payloads: list[dict[str, Any]] = []
        with self._mu:
            self._conn.execute("BEGIN")
            try:
                for r in records:
                    self._upsert(r)
                    payloads.append(_skill_record_evidence_payload(r, "saved"))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return payloads

    @_db_retry()
    def _record_analysis_sync(
        self,
        analysis: ExecutionAnalysis,
        observed_tool_keys: Iterable[str],
    ) -> List[Dict[str, Any]]:
        """Persist an analysis and update skill quality counters.

        ``SkillJudgment.skill_id`` is the **true skill_id** (e.g.
        ``weather__imp_a1b2c3d4``), the same identifier used as the DB
        primary key.  The analysis LLM receives skill_ids in its prompt
        and outputs them verbatim.

        We update counters via ``WHERE skill_id = ?`` — exact match, no
        ambiguity.
        """
        self._ensure_open()
        trust_updates: List[Dict[str, Any]] = []
        with self._mu:
            self._conn.execute("BEGIN")
            try:
                self._insert_analysis(analysis)

                now_iso = datetime.now().isoformat()
                phase_failed_ids = set(
                    getattr(analysis, "skill_phase_failed_skill_ids", []) or []
                )
                for j in analysis.skill_judgments:
                    applied = 1 if j.skill_applied else 0
                    completed = (
                        1
                        if (
                            j.skill_applied
                            and analysis.task_completed
                            and j.skill_id not in phase_failed_ids
                        )
                        else 0
                    )
                    fallback = (
                        1
                        if (
                            j.skill_id in phase_failed_ids
                            or not analysis.task_completed
                        )
                        else 0
                    )
                    self._conn.execute(
                        """
                        UPDATE skill_records SET
                            total_applied     = total_applied + ?,
                            total_completions = total_completions + ?,
                            total_fallbacks   = total_fallbacks + ?,
                            last_updated      = ?
                        WHERE skill_id = ?
                        """,
                        (applied, completed, fallback, now_iso, j.skill_id),
                    )
                    if applied:
                        self._insert_skill_event_locked(
                            j.skill_id,
                            "applied",
                            source="analyzer",
                            task_id=analysis.task_id,
                            metadata={"note": j.note},
                        )
                    if completed:
                        self._insert_skill_event_locked(
                            j.skill_id,
                            "completed",
                            source="analyzer",
                            task_id=analysis.task_id,
                            metadata={"note": j.note},
                        )
                    if fallback:
                        self._insert_skill_event_locked(
                            j.skill_id,
                            "fallback",
                            source="analyzer",
                            task_id=analysis.task_id,
                            metadata={
                                "skill_applied": bool(j.skill_applied),
                                "phase_failed": j.skill_id in phase_failed_ids,
                                "task_completed": bool(analysis.task_completed),
                            },
                        )
                    trust_outcome = ""
                    if completed:
                        trust_outcome = "success"
                    elif j.skill_id in phase_failed_ids:
                        trust_outcome = "failure"
                    if trust_outcome:
                        update = self._record_trust_observation_locked(
                            skill_id=j.skill_id,
                            observation_id=f"task:{analysis.task_id}",
                            outcome=trust_outcome,
                            task_id=analysis.task_id,
                            session_id="",
                            source="execution_analysis",
                            evidence_refs=[f"analysis:{analysis.task_id}"],
                        )
                        if update.get("record") is not None:
                            trust_updates.append(update)

                self._backfill_tool_deps_from_analysis_locked(
                    analysis,
                    observed_tool_keys,
                )

                self._conn.commit()
                return trust_updates
            except Exception:
                self._conn.rollback()
                raise

    def _backfill_tool_deps_from_analysis_locked(
        self,
        analysis: ExecutionAnalysis,
        observed_tool_keys: Iterable[str],
    ) -> None:
        """Attach observed and problematic tools to skills judged in analysis.

        The quality/degradation pipeline uses canonical
        ``backend:server:tool`` keys.  Trajectory logs and analysis output may
        still contain legacy ``backend:tool`` keys, so normalize on write.
        """

        deps: set[str] = set()
        for key in observed_tool_keys:
            normalized = _normalize_tool_dependency_key(key)
            if normalized:
                deps.add(normalized)
        for issue in analysis.tool_issues:
            normalized = _extract_tool_dependency_from_issue(issue)
            if normalized:
                deps.add(normalized)
        if not deps:
            return

        applied_skill_ids = [
            j.skill_id for j in analysis.skill_judgments
            if j.skill_id and j.skill_applied
        ]
        # Prefer applied skills, but keep signal for older/ambiguous analyses
        # that judged selected skills without marking any as applied.
        target_skill_ids = applied_skill_ids or [
            j.skill_id for j in analysis.skill_judgments if j.skill_id
        ]
        if not target_skill_ids:
            return

        for skill_id in dict.fromkeys(target_skill_ids):
            for dep in sorted(deps):
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO skill_tool_deps
                        (skill_id, tool_key, critical)
                    SELECT ?, ?, 0
                    WHERE EXISTS (
                        SELECT 1 FROM skill_records WHERE skill_id=?
                    )
                    """,
                    (skill_id, dep, skill_id),
                )

    @_db_retry()
    def _evolve_skill_sync(
        self,
        new_record: SkillRecord,
        parent_skill_ids: List[str],
    ) -> list[dict[str, Any]]:
        """Atomic: insert new version + deactivate parents (for FIXED)."""
        self._ensure_open()
        payloads: list[dict[str, Any]] = []
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
                        payload = self._skill_record_payload_from_db_locked(
                            pid,
                            "deactivated_for_evolution",
                        )
                        if payload:
                            payloads.append(payload)

                # Ensure new record has parent refs set
                new_record.lineage.parent_skill_ids = list(parent_skill_ids)
                new_record.is_active = True

                self._upsert(new_record)
                payloads.append(
                    _skill_record_evidence_payload(
                        new_record,
                        "evolved_created",
                    )
                )
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
        return payloads

    @_db_retry()
    def _deactivate_record_sync(self, skill_id: str) -> tuple[bool, dict[str, Any] | None]:
        self._ensure_open()
        with self._mu:
            cur = self._conn.execute(
                "UPDATE skill_records SET is_active=0, last_updated=? "
                "WHERE skill_id=?",
                (datetime.now().isoformat(), skill_id),
            )
            changed = cur.rowcount > 0
            payload = (
                self._skill_record_payload_from_db_locked(skill_id, "deactivated")
                if changed
                else None
            )
            self._conn.commit()
            return changed, payload

    @_db_retry()
    def _reactivate_record_sync(self, skill_id: str) -> tuple[bool, dict[str, Any] | None]:
        self._ensure_open()
        with self._mu:
            cur = self._conn.execute(
                "UPDATE skill_records SET is_active=1, last_updated=? "
                "WHERE skill_id=?",
                (datetime.now().isoformat(), skill_id),
            )
            changed = cur.rowcount > 0
            payload = (
                self._skill_record_payload_from_db_locked(skill_id, "reactivated")
                if changed
                else None
            )
            self._conn.commit()
            return changed, payload

    @_db_retry()
    def _delete_record_sync(self, skill_id: str) -> tuple[bool, dict[str, Any] | None]:
        self._ensure_open()
        with self._mu:
            payload = self._skill_record_payload_from_db_locked(skill_id, "deleted")
            # ON DELETE CASCADE automatically cleans up lineage_parents / deps / tags
            # skill_judgments are NOT cascade-deleted (no FK to skill_records)
            cur = self._conn.execute(
                "DELETE FROM skill_records WHERE skill_id=?", (skill_id,)
            )
            self._conn.commit()
            changed = cur.rowcount > 0
            return changed, payload if changed else None

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
    def load_skill_events(
        self,
        skill_id: Optional[str] = None,
        *,
        event_type: Optional[str] = None,
        task_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Load recent skill lifecycle events for diagnostics/tests."""

        clauses: list[str] = []
        params: list[Any] = []
        if skill_id:
            clauses.append("skill_id=?")
            params.append(skill_id)
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type)
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if turn_id:
            clauses.append("turn_id=?")
            params.append(turn_id)
        if agent_id:
            clauses.append("agent_id=?")
            params.append(agent_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._reader() as conn:
            rows = conn.execute(
                f"SELECT * FROM skill_events {where} "
                "ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
            events: list[dict[str, Any]] = []
            for row in reversed(rows):
                item = dict(row)
                try:
                    item["metadata"] = json.loads(item.get("metadata") or "{}")
                except Exception:
                    item["metadata"] = {}
                events.append(item)
            return events

    @_db_retry()
    def load_trust_observations(
        self,
        skill_id: str,
        *,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Load auditable trust observations for one stable skill id."""

        with self._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_trust_observations WHERE skill_id=? "
                "ORDER BY id DESC LIMIT ?",
                (skill_id, max(1, int(limit or 200))),
            ).fetchall()
            observations: List[Dict[str, Any]] = []
            for row in reversed(rows):
                item = dict(row)
                try:
                    item["evidence_refs"] = json.loads(
                        item.pop("evidence_refs_json", "[]") or "[]"
                    )
                except Exception:
                    item["evidence_refs"] = []
                observations.append(item)
            return observations

    @_db_retry()
    def load_evolution_candidates(
        self, limit: int = 50
    ) -> List[ExecutionAnalysis]:
        """Load analyses marked as evolution candidates."""
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM execution_analyses "
                "WHERE candidate_for_evolution=1 "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._to_analysis(conn, r) for r in reversed(rows)]

    @_db_retry()
    def find_skills_by_tool(self, tool_key: str) -> List[str]:
        """
        Only returns active records — deactivated (superseded) versions
        are excluded so that Trigger 2 never re-processes old versions.
        """
        variants = _tool_dependency_lookup_variants(tool_key)
        if not variants:
            return []
        placeholders = ",".join("?" for _ in variants)
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT sd.skill_id "
                "FROM skill_tool_deps sd "
                "JOIN skill_records sr ON sd.skill_id = sr.skill_id "
                f"WHERE sd.tool_key IN ({placeholders}) AND sr.is_active=1",
                tuple(variants),
            ).fetchall()
            return list(dict.fromkeys(r["skill_id"] for r in rows))

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
            where = "WHERE sr.is_active=1 " if active_only else ""
            rows = conn.execute(
                f"""
                SELECT sr.skill_id, sr.name, sr.description, sr.category, sr.is_active,
                       sr.enabled, sr.trust_state,
                       sr.visibility, sr.creator_id,
                       sr.lineage_origin, sr.lineage_generation,
                       sr.total_selections, sr.total_applied,
                       sr.total_completions, sr.total_fallbacks,
                       COALESCE(ev.total_listed_events, 0) AS total_listed_events,
                       COALESCE(ev.total_discovered_events, 0) AS total_discovered_events,
                       COALESCE(ev.total_invoked_events, 0) AS total_invoked_events,
                       COALESCE(ev.total_applied_events, 0) AS total_applied_events,
                       COALESCE(ev.total_completed_events, 0) AS total_completed_events,
                       COALESCE(ev.total_fallback_events, 0) AS total_fallback_events,
                       COALESCE(tr.trust_successes, 0) AS trust_successes,
                       COALESCE(tr.trust_failures, 0) AS trust_failures,
                       MAX(sr.total_selections, COALESCE(ev.total_invoked_events, 0))
                           AS total_uses,
                       sr.first_seen, sr.last_updated
                FROM skill_records sr
                LEFT JOIN (
                    SELECT skill_id,
                           SUM(CASE WHEN event_type='listed' THEN 1 ELSE 0 END)
                               AS total_listed_events,
                           SUM(CASE WHEN event_type='discovered' THEN 1 ELSE 0 END)
                               AS total_discovered_events,
                           SUM(CASE WHEN event_type='invoked' THEN 1 ELSE 0 END)
                               AS total_invoked_events,
                           SUM(CASE WHEN event_type='applied' THEN 1 ELSE 0 END)
                               AS total_applied_events,
                           SUM(CASE WHEN event_type='completed' THEN 1 ELSE 0 END)
                               AS total_completed_events,
                           SUM(CASE WHEN event_type='fallback' THEN 1 ELSE 0 END)
                               AS total_fallback_events
                    FROM skill_events
                    GROUP BY skill_id
                ) ev ON ev.skill_id = sr.skill_id
                LEFT JOIN (
                    SELECT skill_id,
                           SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END)
                               AS trust_successes,
                           SUM(CASE WHEN outcome='failure' THEN 1 ELSE 0 END)
                               AS trust_failures
                    FROM skill_trust_observations
                    GROUP BY skill_id
                ) tr ON tr.skill_id = sr.skill_id
                {where}
                ORDER BY sr.last_updated DESC
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
            by_trust_state = {
                r["trust_state"]: r["cnt"]
                for r in conn.execute(
                    f"SELECT trust_state, COUNT(*) AS cnt "
                    f"FROM skill_records{where} GROUP BY trust_state"
                ).fetchall()
            }
            n_analyses = conn.execute(
                "SELECT COUNT(*) FROM execution_analyses"
            ).fetchone()[0]
            n_candidates = conn.execute(
                "SELECT COUNT(*) FROM execution_analyses "
                "WHERE candidate_for_evolution=1"
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
            event_where = "WHERE sr.is_active=1" if active_only else ""
            lifecycle_counts = {
                event_type: 0 for event_type in _LIFECYCLE_EVENT_TYPES
            }
            event_rows = conn.execute(
                f"""
                SELECT e.event_type, COUNT(*) AS cnt
                FROM skill_events e
                LEFT JOIN skill_records sr ON sr.skill_id = e.skill_id
                {event_where}
                GROUP BY e.event_type
                """
            ).fetchall()
            for row in event_rows:
                event_type = str(row["event_type"] or "")
                if event_type in lifecycle_counts:
                    lifecycle_counts[event_type] = int(row["cnt"] or 0)
            total_uses = max(
                int(agg["sel"] or 0),
                lifecycle_counts["invoked"],
            )

            # Also report total (including inactive) for context
            total_all = conn.execute(
                "SELECT COUNT(*) FROM skill_records"
            ).fetchone()[0]

            return {
                "total_skills": total,
                "total_skills_all": total_all,
                "by_category": by_category,
                "by_origin": by_origin,
                "by_trust_state": by_trust_state,
                "total_analyses": n_analyses,
                "evolution_candidates": n_candidates,
                "total_selections": agg["sel"] or 0,
                "total_uses": total_uses,
                "total_applied": agg["app"] or 0,
                "total_completions": agg["comp"] or 0,
                "total_fallbacks": agg["fb"] or 0,
                "total_listed_events": lifecycle_counts["listed"],
                "total_discovered_events": lifecycle_counts["discovered"],
                "total_invoked_events": lifecycle_counts["invoked"],
                "total_applied_events": lifecycle_counts["applied"],
                "total_completed_events": lifecycle_counts["completed"],
                "total_fallback_events": lifecycle_counts["fallback"],
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
            ``effective_rate``  — completions / uses
            ``applied_rate``    — applied / uses
            ``completion_rate`` — completions / applied
            ``total_selections``— raw count
        """
        uses_expr = (
            "CASE WHEN total_selections > total_invocations "
            "THEN total_selections ELSE total_invocations END"
        )
        rate_exprs = {
            "effective_rate": (
                f"CASE WHEN {uses_expr} > 0 "
                f"THEN CAST(total_completions AS REAL) / {uses_expr} "
                "ELSE 0.0 END"
            ),
            "applied_rate": (
                f"CASE WHEN {uses_expr} > 0 "
                f"THEN CAST(total_applied AS REAL) / {uses_expr} "
                "ELSE 0.0 END"
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
                "WITH ranked_records AS ("
                "SELECT sr.*, COALESCE(inv.total_invocations, 0) AS total_invocations "
                "FROM skill_records sr "
                "LEFT JOIN ("
                "SELECT skill_id, COUNT(*) AS total_invocations "
                "FROM skill_events WHERE event_type='invoked' GROUP BY skill_id"
                ") inv ON inv.skill_id = sr.skill_id"
                ") "
                f"SELECT *, ({expr}) AS _rank "
                f"FROM ranked_records "
                f"WHERE {uses_expr} >= ?{active_clause} "
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
    def _skill_record_payload_from_db_locked(
        self,
        skill_id: str,
        lifecycle_event: str,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM skill_records WHERE skill_id=?",
            (skill_id,),
        ).fetchone()
        if row is None:
            return None
        return _skill_record_evidence_payload(
            self._to_record(self._conn, row),
            lifecycle_event,
        )

    def _upsert(self, record: SkillRecord) -> None:
        """Insert or update skill_records + sync related rows.

        Called within a transaction holding ``self._mu``.
        """
        lin = record.lineage
        # content_snapshot is Dict[str, str]; store as JSON text
        snapshot_json = json.dumps(
            lin.content_snapshot, ensure_ascii=False
        )
        provenance_refs_json = json.dumps(
            lin.provenance_refs, ensure_ascii=False
        )
        parent_revision_ids_json = json.dumps(
            lin.parent_revision_ids or lin.parent_skill_ids,
            ensure_ascii=False,
        )
        revision_metadata_json = json.dumps(
            lin.revision_metadata,
            ensure_ascii=False,
        )
        self._conn.execute(
            """
            INSERT INTO skill_records (
                skill_id, name, description, path, is_active, enabled,
                trust_state, category,
                visibility, creator_id,
                lineage_origin, lineage_revision_id, lineage_generation,
                lineage_parent_revision_ids_json, lineage_source_task_id,
                lineage_change_summary, lineage_content_hash,
                lineage_evolution_action_id, lineage_provenance_refs_json,
                lineage_revision_metadata_json, lineage_content_diff,
                lineage_content_snapshot,
                lineage_created_at, lineage_created_by,
                total_selections, total_applied,
                total_completions, total_fallbacks,
                first_seen, last_updated
            ) VALUES (?,?,?,?,?,?,?, ?, ?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?, ?,?,?,?, ?,?)
            ON CONFLICT(skill_id) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                path=excluded.path,
                is_active=excluded.is_active,
                enabled=excluded.enabled,
                trust_state=excluded.trust_state,
                category=excluded.category,
                visibility=excluded.visibility,
                creator_id=excluded.creator_id,
                lineage_origin=excluded.lineage_origin,
                lineage_revision_id=excluded.lineage_revision_id,
                lineage_generation=excluded.lineage_generation,
                lineage_parent_revision_ids_json=excluded.lineage_parent_revision_ids_json,
                lineage_source_task_id=excluded.lineage_source_task_id,
                lineage_change_summary=excluded.lineage_change_summary,
                lineage_content_hash=excluded.lineage_content_hash,
                lineage_evolution_action_id=excluded.lineage_evolution_action_id,
                lineage_provenance_refs_json=excluded.lineage_provenance_refs_json,
                lineage_revision_metadata_json=excluded.lineage_revision_metadata_json,
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
                int(record.enabled),
                record.trust_state.value,
                record.category.value,
                record.visibility.value,
                record.creator_id,
                lin.origin.value,
                lin.revision_id or record.skill_id,
                lin.generation,
                parent_revision_ids_json,
                lin.source_task_id,
                lin.change_summary,
                lin.content_hash,
                lin.evolution_action_id,
                provenance_refs_json,
                revision_metadata_json,
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
                tool_issues, skill_phase_failed_skill_ids, candidate_for_evolution,
                evolution_suggestions, analyzed_by, analyzed_at
            ) VALUES (?,?, ?,?, ?,?, ?,?, ?,?)
            """,
            (
                a.task_id,
                a.timestamp.isoformat(),
                int(a.task_completed),
                a.execution_note,
                json.dumps(a.tool_issues, ensure_ascii=False),
                json.dumps(a.skill_phase_failed_skill_ids, ensure_ascii=False),
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
            revision_id=(
                row["lineage_revision_id"]
                if "lineage_revision_id" in row.keys()
                else sid
            ) or sid,
            generation=row["lineage_generation"],
            parent_skill_ids=parents,
            parent_revision_ids=_json_list_from_row(
                row,
                "lineage_parent_revision_ids_json",
            ) or parents,
            source_task_id=row["lineage_source_task_id"],
            change_summary=row["lineage_change_summary"],
            content_hash=(
                row["lineage_content_hash"]
                if "lineage_content_hash" in row.keys()
                else ""
            ),
            content_diff=row["lineage_content_diff"],
            content_snapshot=snapshot,
            evolution_action_id=(
                row["lineage_evolution_action_id"]
                if "lineage_evolution_action_id" in row.keys()
                else None
            ),
            provenance_refs=_json_list_from_row(
                row,
                "lineage_provenance_refs_json",
            ),
            revision_metadata=_json_object_from_row(
                row,
                "lineage_revision_metadata_json",
            ),
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
        total_invocations = conn.execute(
            "SELECT COUNT(*) FROM skill_events "
            "WHERE skill_id=? AND event_type='invoked'",
            (sid,),
        ).fetchone()[0]
        trust_counts = conn.execute(
            """
            SELECT
                SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) AS successes,
                SUM(CASE WHEN outcome='failure' THEN 1 ELSE 0 END) AS failures
            FROM skill_trust_observations WHERE skill_id=?
            """,
            (sid,),
        ).fetchone()

        return SkillRecord(
            skill_id=sid,
            name=row["name"],
            description=row["description"],
            path=row["path"],
            is_active=bool(row["is_active"]),
            enabled=bool(row["enabled"]),
            trust_state=SkillTrustState(row["trust_state"]),
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
            total_invocations=total_invocations,
            total_applied=row["total_applied"],
            total_completions=row["total_completions"],
            total_fallbacks=row["total_fallbacks"],
            trust_successes=int(trust_counts["successes"] or 0),
            trust_failures=int(trust_counts["failures"] or 0),
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
        failed_skill_ids: list[str] = []
        if "skill_phase_failed_skill_ids" in row.keys():
            raw_failed = row["skill_phase_failed_skill_ids"] or "[]"
            try:
                loaded = json.loads(raw_failed)
                if isinstance(loaded, list):
                    failed_skill_ids = [str(sid) for sid in loaded if sid]
            except (json.JSONDecodeError, TypeError, ValueError):
                failed_skill_ids = []

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
            skill_phase_failed_skill_ids=failed_skill_ids,
            evolution_suggestions=suggestions,
            analyzed_by=row["analyzed_by"],
            analyzed_at=datetime.fromisoformat(row["analyzed_at"]),
        )
