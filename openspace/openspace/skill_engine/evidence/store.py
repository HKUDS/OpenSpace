"""SQLite EvidenceStore for evolution provenance."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generator

from openspace.utils.logging import Logger

from .redaction import contains_secret, redact_metadata, redact_text
from .types import (
    ALLOWED_REF_TYPES,
    ALLOWED_RELIABILITY,
    ALLOWED_ROLES,
    ALLOWED_SEVERITY,
    EvidenceEvent,
    EvidencePacket,
    EvidenceScope,
    ManifestView,
    ResourceRef,
)

logger = Logger.get_logger(__name__)

if TYPE_CHECKING:
    from openspace.skill_engine.evolution.admission import AdmissionResult
    from openspace.skill_engine.evolution.audit import EvolutionActionRecord
    from openspace.skill_engine.evolution.validator import ValidationResult


_DDL = """
CREATE TABLE IF NOT EXISTS evidence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    producer TEXT NOT NULL,
    created_at TEXT NOT NULL,
    session_id TEXT,
    task_id TEXT,
    parent_task_id TEXT,
    turn_id TEXT,
    agent_id TEXT,
    severity TEXT NOT NULL DEFAULT 'info',
    idempotency_key TEXT NOT NULL UNIQUE,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS resource_refs (
    ref_id TEXT PRIMARY KEY,
    ref_type TEXT NOT NULL,
    uri TEXT,
    session_id TEXT,
    task_id TEXT,
    parent_task_id TEXT,
    turn_id TEXT,
    agent_id TEXT,
    producer TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reliability TEXT NOT NULL,
    role TEXT NOT NULL,
    content_hash TEXT,
    preview TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    contains_secret INTEGER NOT NULL DEFAULT 0,
    first_event_id TEXT NOT NULL,
    last_event_id TEXT NOT NULL,
    first_seen_watermark INTEGER NOT NULL,
    last_seen_watermark INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_ref_observations (
    ref_id TEXT NOT NULL,
    watermark INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    ref_type TEXT NOT NULL,
    uri TEXT,
    session_id TEXT,
    task_id TEXT,
    parent_task_id TEXT,
    turn_id TEXT,
    agent_id TEXT,
    producer TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content_hash TEXT,
    preview TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    reliability TEXT NOT NULL,
    role TEXT NOT NULL,
    raw_backrefs_json TEXT NOT NULL DEFAULT '[]',
    contains_secret INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (ref_id, watermark)
);

CREATE TABLE IF NOT EXISTS resource_ref_links (
    derived_ref_id TEXT NOT NULL,
    raw_ref_id TEXT NOT NULL,
    link_type TEXT NOT NULL DEFAULT 'derived_from',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (derived_ref_id, raw_ref_id, link_type)
);

CREATE INDEX IF NOT EXISTS idx_resource_refs_scope_type_watermark
  ON resource_refs(session_id, task_id, ref_type, first_seen_watermark, last_seen_watermark);

CREATE INDEX IF NOT EXISTS idx_resource_refs_type_watermark
  ON resource_refs(ref_type, first_seen_watermark, last_seen_watermark);

CREATE INDEX IF NOT EXISTS idx_resource_refs_agent_task
  ON resource_refs(session_id, agent_id, task_id, ref_type);

CREATE INDEX IF NOT EXISTS idx_resource_refs_parent_task
  ON resource_refs(session_id, parent_task_id, ref_type, first_seen_watermark, last_seen_watermark);

CREATE INDEX IF NOT EXISTS idx_resource_ref_observations_ref_watermark
  ON resource_ref_observations(ref_id, watermark);

CREATE INDEX IF NOT EXISTS idx_evidence_events_scope_type_id
  ON evidence_events(session_id, task_id, event_type, id);

CREATE INDEX IF NOT EXISTS idx_resource_ref_links_raw
  ON resource_ref_links(raw_ref_id);

CREATE TABLE IF NOT EXISTS evidence_packets (
    packet_id TEXT PRIMARY KEY,
    trigger_job_id TEXT NOT NULL,
    packet_type TEXT NOT NULL,
    profile_name TEXT NOT NULL,
    subprofile TEXT NOT NULL,
    manifest_watermark INTEGER NOT NULL,
    build_status TEXT NOT NULL,
    redaction_status TEXT NOT NULL,
    missing_ref_types_json TEXT NOT NULL DEFAULT '[]',
    packet_json TEXT NOT NULL,
    packet_ref_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_packets_trigger
  ON evidence_packets(trigger_job_id, packet_type, manifest_watermark);

CREATE TABLE IF NOT EXISTS decision_rationales (
    decision_id TEXT PRIMARY KEY,
    trigger_job_id TEXT NOT NULL,
    packet_id TEXT NOT NULL,
    proposed_action TEXT NOT NULL,
    candidate_policy TEXT NOT NULL,
    target_skill_ids_json TEXT NOT NULL DEFAULT '[]',
    reason_summary TEXT NOT NULL DEFAULT '',
    reason_tags_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.0,
    risks_json TEXT NOT NULL DEFAULT '[]',
    source_analysis_id TEXT,
    noop_reason TEXT,
    analyzed_by TEXT NOT NULL DEFAULT '',
    proposal_contract_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decision_rationales_trigger
  ON decision_rationales(trigger_job_id, packet_id, created_at);

CREATE TABLE IF NOT EXISTS decision_evidence_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    claim TEXT NOT NULL,
    refs_json TEXT NOT NULL DEFAULT '[]',
    confidence TEXT NOT NULL DEFAULT 'low'
);

CREATE INDEX IF NOT EXISTS idx_decision_claims_decision
  ON decision_evidence_claims(decision_id);

CREATE TABLE IF NOT EXISTS admission_results (
    admission_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    packet_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    hard_failures_json TEXT NOT NULL DEFAULT '[]',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    required_refs_checked_json TEXT NOT NULL DEFAULT '[]',
    source_validation_passed INTEGER NOT NULL DEFAULT 0,
    reviewed_by TEXT NOT NULL DEFAULT 'rule',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_admission_results_decision
  ON admission_results(decision_id, packet_id, created_at);

CREATE TABLE IF NOT EXISTS validation_results (
    validation_id TEXT PRIMARY KEY,
    authoring_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    packet_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    deterministic_failures_json TEXT NOT NULL DEFAULT '[]',
    semantic_warnings_json TEXT NOT NULL DEFAULT '[]',
    changed_files_json TEXT NOT NULL DEFAULT '[]',
    provenance_refs_json TEXT NOT NULL DEFAULT '[]',
    checked_at TEXT NOT NULL,
    checked_by TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_validation_results_authoring
  ON validation_results(authoring_id, decision_id, checked_at);

CREATE TABLE IF NOT EXISTS behavior_eval_results (
    eval_id TEXT PRIMARY KEY,
    authoring_id TEXT NOT NULL,
    validation_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    packet_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    failures_json TEXT NOT NULL DEFAULT '[]',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    contract_eval_json TEXT NOT NULL DEFAULT '{}',
    routing_eval_json TEXT NOT NULL DEFAULT '{}',
    trigger_eval_json TEXT NOT NULL DEFAULT '{}',
    replay_eval_json TEXT NOT NULL DEFAULT '{}',
    contract_snapshot_json TEXT NOT NULL DEFAULT '{}',
    checked_at TEXT NOT NULL,
    checked_by TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_behavior_eval_results_authoring
  ON behavior_eval_results(authoring_id, validation_id, checked_at);

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
    rejection_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_candidates_status
  ON evolution_candidates(status, updated_at);

CREATE INDEX IF NOT EXISTS idx_candidates_admission
  ON evolution_candidates(admission_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_pending_merge
  ON evolution_candidates(merge_key)
  WHERE status='pending';

CREATE TABLE IF NOT EXISTS evolution_actions (
    action_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    trigger_job_id TEXT NOT NULL,
    authoring_id TEXT NOT NULL,
    validation_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    commit_status TEXT NOT NULL,
    skill_id TEXT,
    parent_skill_ids_json TEXT NOT NULL DEFAULT '[]',
    changed_files_json TEXT NOT NULL DEFAULT '[]',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    staging_dir TEXT NOT NULL,
    active_target_dir TEXT NOT NULL,
    backup_dir TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    committed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_evolution_actions_status
  ON evolution_actions(commit_status, created_at);

CREATE INDEX IF NOT EXISTS idx_evolution_actions_trigger
  ON evolution_actions(trigger_job_id, decision_id, created_at);

CREATE INDEX IF NOT EXISTS idx_evolution_actions_decision_status
  ON evolution_actions(decision_id, commit_status, committed_at);

CREATE TABLE IF NOT EXISTS evolution_action_failures (
    failure_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evolution_action_failures_action
  ON evolution_action_failures(action_id, created_at);
"""


class EvidenceStore:
    """Append-only evidence event log plus materialized ResourceRef index."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        allowed_read_roots: list[str | Path] | tuple[str | Path, ...] | None = None,
    ) -> None:
        if db_path is None:
            root = Path.home() / ".openspace"
            root.mkdir(parents=True, exist_ok=True)
            db_path = root / "evidence.db"
            logger.warning(
                "EvidenceStore constructed without explicit db_path; using "
                "global/dev storage at %s",
                db_path,
            )

        self._db_path = Path(db_path).expanduser().resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._mu = threading.Lock()
        self._closed = False
        self._conn = self._make_connection(read_only=False)
        self._allowed_read_roots = self._merge_allowed_read_roots(
            self._default_allowed_read_roots(),
            allowed_read_roots or (),
        )
        self._init_db()
        logger.debug("EvidenceStore ready at %s", self._db_path)

    @property
    def db_path(self) -> Path:
        return self._db_path

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
            self._ensure_column_locked("resource_ref_observations", "ref_type", "TEXT")
            self._ensure_column_locked("resource_ref_observations", "session_id", "TEXT")
            self._ensure_column_locked("resource_ref_observations", "task_id", "TEXT")
            self._ensure_column_locked("resource_ref_observations", "parent_task_id", "TEXT")
            self._ensure_column_locked("resource_ref_observations", "turn_id", "TEXT")
            self._ensure_column_locked("resource_ref_observations", "agent_id", "TEXT")
            self._ensure_column_locked("resource_ref_observations", "producer", "TEXT")
            self._ensure_column_locked("resource_ref_observations", "created_at", "TEXT")
            self._ensure_column_locked(
                "resource_ref_observations",
                "raw_backrefs_json",
                "TEXT DEFAULT '[]'",
            )
            self._ensure_column_locked(
                "behavior_eval_results",
                "contract_eval_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column_locked(
                "behavior_eval_results",
                "routing_eval_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column_locked(
                "decision_rationales",
                "proposal_contract_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column_locked(
                "admission_results",
                "source_validation_passed",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._conn.commit()

    def _ensure_column_locked(self, table: str, column: str, definition: str) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        if column in {str(row["name"]) for row in rows}:
            return
        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @contextmanager
    def _reader(self) -> Generator[sqlite3.Connection, None, None]:
        self._ensure_open()
        conn = self._make_connection(read_only=True)
        try:
            yield conn
        finally:
            conn.close()

    def ingest_event(self, event: EvidenceEvent) -> int:
        """Persist an EvidenceEvent and upsert all attached refs.

        Returns the manifest watermark, which is the durable
        ``evidence_events.id`` for the inserted event. Re-ingesting the same
        idempotency key returns the existing watermark without mutating refs.
        """

        self._validate_event(event)
        metadata = redact_metadata(event.metadata)
        with self._mu:
            self._ensure_open()
            watermark = self._ingest_event_locked(event, metadata)
            self._conn.commit()
            return watermark

    def _ingest_event_locked(
        self,
        event: EvidenceEvent,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        existing = self._conn.execute(
            "SELECT id FROM evidence_events WHERE idempotency_key = ?",
            (event.idempotency_key,),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])

        cursor = self._conn.execute(
            """
            INSERT INTO evidence_events (
                event_id, event_type, producer, created_at, session_id,
                task_id, parent_task_id, turn_id, agent_id, severity,
                idempotency_key, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.event_type,
                event.producer,
                event.created_at,
                event.session_id,
                event.task_id,
                event.parent_task_id,
                event.turn_id,
                event.agent_id,
                event.severity,
                event.idempotency_key,
                _json(metadata if metadata is not None else redact_metadata(event.metadata)),
            ),
        )
        watermark = int(cursor.lastrowid)
        for ref in event.primary_refs:
            self._upsert_ref_locked(
                ref,
                event=event,
                event_id=event.event_id,
                watermark=watermark,
                default_role="primary",
            )
        for ref in event.supporting_refs:
            self._upsert_ref_locked(
                ref,
                event=event,
                event_id=event.event_id,
                watermark=watermark,
                default_role="supporting",
            )
        for ref in event.derived_refs:
            self._upsert_ref_locked(
                ref,
                event=event,
                event_id=event.event_id,
                watermark=watermark,
                default_role="derived",
            )
        return watermark

    def upsert_ref(
        self,
        ref: ResourceRef,
        *,
        event_id: str,
        watermark: int,
    ) -> None:
        self._validate_ref(ref)
        with self._mu:
            self._ensure_open()
            self._upsert_ref_locked(
                ref,
                event=None,
                event_id=event_id,
                watermark=watermark,
                default_role=ref.role,
            )
            self._conn.commit()

    def freeze_view(
        self,
        scope: EvidenceScope,
        watermark: int | None = None,
    ) -> ManifestView:
        resolved_watermark = watermark if watermark is not None else self._latest_watermark()
        refs = self.query_refs(scope, watermark=resolved_watermark)
        return ManifestView(
            view_id=f"manifest_view:{uuid.uuid4().hex}",
            scope=scope,
            watermark=resolved_watermark,
            created_at=_utc_now(),
            refs=refs,
        )

    def latest_manifest_watermark(self) -> int:
        """Return the latest durable evidence manifest watermark."""

        return self._latest_watermark()

    def query_refs(
        self,
        scope: EvidenceScope,
        ref_types: list[str] | None = None,
        watermark: int | None = None,
    ) -> list[ResourceRef]:
        resolved_watermark = watermark if watermark is not None else self._latest_watermark()
        type_filter = set(ref_types or [])
        with self._reader() as conn:
            rows = conn.execute(
                """
                SELECT ref_id FROM resource_refs
                WHERE first_seen_watermark <= ?
                ORDER BY first_seen_watermark, ref_id
                """,
                (resolved_watermark,),
            ).fetchall()
            refs: list[ResourceRef] = []
            for row in rows:
                ref = self._get_ref_at_conn(
                    conn,
                    str(row["ref_id"]),
                    resolved_watermark,
                )
                if ref is None:
                    continue
                if type_filter and ref.ref_type not in type_filter:
                    continue
                if not _scope_matches(ref, scope):
                    continue
                refs.append(ref)
            return refs

    def get_ref(self, ref_id: str) -> ResourceRef | None:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM resource_refs WHERE ref_id = ?",
                (ref_id,),
            ).fetchone()
            if row is None:
                return None
            observation = conn.execute(
                """
                SELECT * FROM resource_ref_observations
                WHERE ref_id = ?
                ORDER BY watermark DESC
                LIMIT 1
                """,
                (ref_id,),
            ).fetchone()
            return self._row_to_ref(
                row,
                raw_backrefs=(
                    _raw_backrefs_from_observation(observation)
                    if observation is not None
                    else self._raw_backrefs(conn, ref_id)
                ),
                first_seen_watermark=int(row["first_seen_watermark"]),
                last_seen_watermark=(
                    int(observation["watermark"])
                    if observation is not None
                    else int(row["last_seen_watermark"])
                ),
            )

    def get_ref_at(self, ref_id: str, watermark: int) -> ResourceRef | None:
        with self._reader() as conn:
            return self._get_ref_at_conn(conn, ref_id, watermark)

    def read_ref_preview(self, ref_id: str, max_chars: int = 4000) -> str:
        ref = self.get_ref(ref_id)
        if ref is None:
            return ""
        text = ""
        path_text = (ref.uri or "").split("#", 1)[0]
        if path_text:
            try:
                path = Path(path_text).expanduser()
                if path.is_file() and self._path_read_allowed(path):
                    text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
        if not text:
            text = ref.preview
        return redact_text(text[: max(0, int(max_chars))])

    def persist_packet(self, packet: EvidencePacket) -> None:
        """Persist an EvidencePacket snapshot and index its derived packet ref."""

        selected_ref_ids = sorted(
            {
                ref.ref_id
                for refs in packet.selected_refs.values()
                for ref in refs
                if ref.ref_id
            }
        )
        created_at = _utc_now()
        packet_ref_id = f"packet:{packet.packet_id}"
        packet_ref = ResourceRef(
            ref_id=packet_ref_id,
            ref_type="evidence_packet_ref",
            session_id=packet.scope.session_id,
            task_id=packet.scope.task_id,
            producer="packet_builder",
            created_at=created_at,
            reliability="derived",
            role="derived",
            preview=(
                f"{packet.packet_type} packet {packet.profile_name}/"
                f"{packet.subprofile} status={packet.build_status}"
            ),
            metadata={
                "packet_id": packet.packet_id,
                "trigger_job_id": packet.trigger_job_id,
                "packet_type": packet.packet_type,
                "profile_name": packet.profile_name,
                "subprofile": packet.subprofile,
                "manifest_watermark": packet.manifest_watermark,
                "build_status": packet.build_status,
                "missing_ref_types": packet.missing_ref_types,
            },
            raw_backrefs=selected_ref_ids,
        )
        event = EvidenceEvent.create(
            event_id=f"evt_packet_{_digest(packet.packet_id)}",
            event_type="evidence_packet_built",
            producer="packet_builder",
            created_at=created_at,
            session_id=packet.scope.session_id,
            task_id=packet.scope.task_id,
            idempotency_key=f"evidence_packet:{packet.packet_id}",
            derived_refs=[packet_ref],
            metadata={
                "packet_id": packet.packet_id,
                "trigger_job_id": packet.trigger_job_id,
                "packet_type": packet.packet_type,
                "profile_name": packet.profile_name,
                "subprofile": packet.subprofile,
                "manifest_watermark": packet.manifest_watermark,
                "selected_ref_count": len(selected_ref_ids),
            },
        )
        self.ingest_event(event)

        packet_json = _json(packet.to_dict())
        now = _utc_now()
        with self._mu:
            self._ensure_open()
            existing = self._conn.execute(
                "SELECT created_at FROM evidence_packets WHERE packet_id=?",
                (packet.packet_id,),
            ).fetchone()
            self._conn.execute(
                """
                INSERT OR REPLACE INTO evidence_packets (
                    packet_id, trigger_job_id, packet_type, profile_name,
                    subprofile, manifest_watermark, build_status,
                    redaction_status, missing_ref_types_json, packet_json,
                    packet_ref_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    packet.packet_id,
                    packet.trigger_job_id,
                    packet.packet_type,
                    packet.profile_name,
                    packet.subprofile,
                    packet.manifest_watermark,
                    packet.build_status,
                    packet.redaction_status,
                    _json(packet.missing_ref_types),
                    packet_json,
                    packet_ref_id,
                    str(existing["created_at"]) if existing is not None else now,
                    now,
                ),
            )
            self._conn.commit()

    def load_packet(self, packet_id: str) -> EvidencePacket | None:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT packet_json FROM evidence_packets WHERE packet_id=?",
                (packet_id,),
            ).fetchone()
            if row is None:
                return None
            return EvidencePacket.from_mapping(_json_object(row["packet_json"]))

    def persist_decision(self, decision: Any, packet_id: str | None = None) -> None:
        """Persist a DecisionRationale row and index its derived resource ref."""

        decision_id = str(getattr(decision, "decision_id", "") or "")
        if not decision_id:
            raise ValueError("DecisionRationale.decision_id is required")
        trigger_job_id = str(getattr(decision, "trigger_job_id", "") or "")
        resolved_packet_id = str(packet_id or getattr(decision, "packet_id", "") or "")
        proposed_action = str(getattr(decision, "proposed_action", "") or "")
        candidate_policy = str(getattr(decision, "candidate_policy", "") or "")
        target_skill_ids = _str_list(getattr(decision, "target_skill_ids", []))
        reason_summary = str(getattr(decision, "reason_summary", "") or "")
        reason_tags = _str_list(getattr(decision, "reason_tags", []))
        confidence = _float_or_zero(getattr(decision, "confidence", 0.0))
        risks = _str_list(getattr(decision, "risks", []))
        source_analysis_id = _none_or_str(getattr(decision, "source_analysis_id", None))
        noop_reason = _none_or_str(getattr(decision, "noop_reason", None))
        analyzed_by = str(getattr(decision, "analyzed_by", "") or "")
        created_at = str(getattr(decision, "created_at", "") or _utc_now())
        local_category_path = str(getattr(decision, "local_category_path", "") or "")
        category = str(getattr(decision, "category", "") or "")
        proposal_contract = _json_object(
            getattr(decision, "proposal_contract", {})
        )
        claims = list(getattr(decision, "evidence_claims", []) or [])

        with self._mu:
            self._ensure_open()
            self._conn.execute(
                """
                INSERT OR REPLACE INTO decision_rationales (
                    decision_id, trigger_job_id, packet_id, proposed_action,
                    candidate_policy, target_skill_ids_json, reason_summary,
                    reason_tags_json, confidence, risks_json, source_analysis_id,
                    noop_reason, analyzed_by, proposal_contract_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    trigger_job_id,
                    resolved_packet_id,
                    proposed_action,
                    candidate_policy,
                    _json(target_skill_ids),
                    reason_summary,
                    _json(reason_tags),
                    confidence,
                    _json(risks),
                    source_analysis_id,
                    noop_reason,
                    analyzed_by,
                    _json(proposal_contract),
                    created_at,
                ),
            )
            self._conn.execute(
                "DELETE FROM decision_evidence_claims WHERE decision_id=?",
                (decision_id,),
            )
            for claim in claims:
                self._conn.execute(
                    """
                    INSERT INTO decision_evidence_claims (
                        decision_id, claim, refs_json, confidence
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        str(getattr(claim, "claim", "") or ""),
                        _json(_str_list(getattr(claim, "refs", []))),
                        str(getattr(claim, "confidence", "") or "low"),
                    ),
                )
            self._conn.commit()

        raw_backrefs = _decision_raw_backrefs(decision, resolved_packet_id)
        ref = ResourceRef(
            ref_id=f"decision:{decision_id}",
            ref_type="decision_rationale_ref",
            session_id=_packet_session_id(self, resolved_packet_id),
            task_id=_packet_task_id(self, resolved_packet_id),
            producer="decision_engine",
            created_at=created_at,
            reliability="derived",
            role="derived",
            preview=reason_summary[:500] or f"{proposed_action} decision",
            metadata={
                "decision_id": decision_id,
                "trigger_job_id": trigger_job_id,
                "packet_id": resolved_packet_id,
                "proposed_action": proposed_action,
                "candidate_policy": candidate_policy,
                "target_skill_ids": target_skill_ids,
                "reason_summary": reason_summary,
                "reason_tags": reason_tags,
                "confidence": confidence,
                "risks": risks,
                "source_analysis_id": source_analysis_id,
                "noop_reason": noop_reason,
                "analyzed_by": analyzed_by,
                "local_category_path": local_category_path,
                "category": category,
                "proposal_contract": proposal_contract,
            },
            raw_backrefs=raw_backrefs,
        )
        event = EvidenceEvent.create(
            event_id=f"evt_decision_{_digest(decision_id)}",
            event_type="decision_rationale_persisted",
            producer="decision_engine",
            created_at=created_at,
            session_id=ref.session_id,
            task_id=ref.task_id,
            idempotency_key=f"decision_rationale:{decision_id}",
            derived_refs=[ref],
            metadata={
                "decision_id": decision_id,
                "packet_id": resolved_packet_id,
                "proposed_action": proposed_action,
                "candidate_policy": candidate_policy,
                "noop_reason": noop_reason,
                "local_category_path": local_category_path,
            },
        )
        self.ingest_event(event)

    def persist_admission(self, result: "AdmissionResult") -> None:
        """Persist an AdmissionResult row and index its derived resource ref."""

        admission_id = str(getattr(result, "admission_id", "") or "")
        if not admission_id:
            raise ValueError("AdmissionResult.admission_id is required")
        decision_id = str(getattr(result, "decision_id", "") or "")
        packet_id = str(getattr(result, "packet_id", "") or "")
        outcome = str(getattr(result, "outcome", "") or "")
        hard_failures = _str_list(getattr(result, "hard_failures", []))
        warnings = _str_list(getattr(result, "warnings", []))
        required_refs_checked = _str_list(
            getattr(result, "required_refs_checked", [])
        )
        reviewed_by = str(getattr(result, "reviewed_by", "") or "rule")
        source_validation_passed = bool(
            getattr(result, "source_validation_passed", False)
        )
        created_at = str(getattr(result, "created_at", "") or _utc_now())

        with self._mu:
            self._ensure_open()
            self._conn.execute(
                """
                INSERT OR REPLACE INTO admission_results (
                    admission_id, decision_id, packet_id, outcome,
                    hard_failures_json, warnings_json,
                    required_refs_checked_json, source_validation_passed,
                    reviewed_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    admission_id,
                    decision_id,
                    packet_id,
                    outcome,
                    _json(hard_failures),
                    _json(warnings),
                    _json(required_refs_checked),
                    1 if source_validation_passed else 0,
                    reviewed_by,
                    created_at,
                ),
            )
            self._conn.commit()

        packet = self.load_packet(packet_id) if packet_id else None
        packet_ref_ids = (
            sorted(
                {
                    ref.ref_id
                    for refs in packet.selected_refs.values()
                    for ref in refs
                    if ref.ref_id
                }
            )
            if packet is not None
            else []
        )
        raw_backrefs = list(
            dict.fromkeys(
                [
                    f"decision:{decision_id}" if decision_id else "",
                    f"packet:{packet_id}" if packet_id else "",
                    *packet_ref_ids,
                    *required_refs_checked,
                ]
            )
        )
        raw_backrefs = [item for item in raw_backrefs if item]
        ref = ResourceRef(
            ref_id=f"admission:{admission_id}",
            ref_type="admission_result_ref",
            session_id=packet.scope.session_id if packet is not None else None,
            task_id=packet.scope.task_id if packet is not None else None,
            producer="admission",
            created_at=created_at,
            reliability="derived",
            role="derived",
            preview=(
                f"admission {outcome}"
                + (f" failures={','.join(hard_failures[:4])}" if hard_failures else "")
            )[:500],
            metadata={
                "admission_id": admission_id,
                "decision_id": decision_id,
                "packet_id": packet_id,
                "outcome": outcome,
                "hard_failures": hard_failures,
                "warnings": warnings,
                "required_refs_checked": required_refs_checked,
                "source_validation_passed": source_validation_passed,
                "reviewed_by": reviewed_by,
            },
            raw_backrefs=raw_backrefs,
        )
        event = EvidenceEvent.create(
            event_id=f"evt_admission_{_digest(admission_id)}",
            event_type="admission_result_persisted",
            producer="admission",
            created_at=created_at,
            session_id=ref.session_id,
            task_id=ref.task_id,
            idempotency_key=f"admission_result:{admission_id}",
            derived_refs=[ref],
            metadata={
                "admission_id": admission_id,
                "decision_id": decision_id,
                "packet_id": packet_id,
                "outcome": outcome,
                "source_validation_passed": source_validation_passed,
            },
        )
        self.ingest_event(event)

    def load_admission(self, admission_id: str) -> "AdmissionResult | None":
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM admission_results WHERE admission_id=?",
                (admission_id,),
            ).fetchone()
            if row is None:
                return None
            from openspace.skill_engine.evolution.admission import AdmissionResult

            return AdmissionResult(
                admission_id=str(row["admission_id"]),
                decision_id=str(row["decision_id"]),
                packet_id=str(row["packet_id"]),
                outcome=str(row["outcome"]),
                hard_failures=_json_list(row["hard_failures_json"]),
                warnings=_json_list(row["warnings_json"]),
                required_refs_checked=_json_list(
                    row["required_refs_checked_json"]
                ),
                source_validation_passed=bool(row["source_validation_passed"]),
                reviewed_by=str(row["reviewed_by"]),
                created_at=str(row["created_at"]),
            )

    def persist_validation(self, result: "ValidationResult") -> None:
        """Persist a ValidationResult row and index its derived resource ref."""

        validation_id = str(getattr(result, "validation_id", "") or "")
        if not validation_id:
            raise ValueError("ValidationResult.validation_id is required")
        authoring_id = str(getattr(result, "authoring_id", "") or "")
        decision_id = str(getattr(result, "decision_id", "") or "")
        packet_id = str(getattr(result, "packet_id", "") or "")
        outcome = str(getattr(result, "outcome", "") or "")
        deterministic_failures = _str_list(
            getattr(result, "deterministic_failures", [])
        )
        semantic_warnings = _str_list(getattr(result, "semantic_warnings", []))
        changed_files = _str_list(getattr(result, "changed_files", []))
        provenance_refs = _str_list(getattr(result, "provenance_refs", []))
        checked_at = str(getattr(result, "checked_at", "") or _utc_now())
        checked_by = str(getattr(result, "checked_by", "") or "validator")

        with self._mu:
            self._ensure_open()
            self._conn.execute(
                """
                INSERT OR REPLACE INTO validation_results (
                    validation_id, authoring_id, decision_id, packet_id, outcome,
                    deterministic_failures_json, semantic_warnings_json,
                    changed_files_json, provenance_refs_json, checked_at, checked_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validation_id,
                    authoring_id,
                    decision_id,
                    packet_id,
                    outcome,
                    _json(deterministic_failures),
                    _json(semantic_warnings),
                    _json(changed_files),
                    _json(provenance_refs),
                    checked_at,
                    checked_by,
                ),
            )
            self._conn.commit()

        packet = self.load_packet(packet_id) if packet_id else None
        packet_ref_ids = (
            sorted(
                {
                    ref.ref_id
                    for refs in packet.selected_refs.values()
                    for ref in refs
                    if ref.ref_id
                }
            )
            if packet is not None
            else []
        )
        admission_refs = [
            ref_id for ref_id in provenance_refs if ref_id.startswith("admission:")
        ]
        raw_backrefs = list(
            dict.fromkeys(
                [
                    f"authoring:{authoring_id}" if authoring_id else "",
                    f"decision:{decision_id}" if decision_id else "",
                    *admission_refs,
                    f"packet:{packet_id}" if packet_id else "",
                    *packet_ref_ids,
                    *provenance_refs,
                ]
            )
        )
        raw_backrefs = [item for item in raw_backrefs if item]
        ref = ResourceRef(
            ref_id=f"validation:{validation_id}",
            ref_type="validation_result_ref",
            session_id=packet.scope.session_id if packet is not None else None,
            task_id=packet.scope.task_id if packet is not None else None,
            producer=checked_by,
            created_at=checked_at,
            reliability="derived",
            role="derived",
            preview=(
                f"validation {outcome}"
                + (
                    f" failures={','.join(deterministic_failures[:4])}"
                    if deterministic_failures
                    else ""
                )
            )[:500],
            metadata={
                "validation_id": validation_id,
                "authoring_id": authoring_id,
                "decision_id": decision_id,
                "packet_id": packet_id,
                "outcome": outcome,
                "deterministic_failures": deterministic_failures,
                "semantic_warnings": semantic_warnings,
                "changed_files": changed_files,
                "provenance_refs": provenance_refs,
                "checked_by": checked_by,
            },
            raw_backrefs=raw_backrefs,
        )
        event = EvidenceEvent.create(
            event_id=f"evt_validation_{_digest(validation_id)}",
            event_type="validation_result_persisted",
            producer=checked_by,
            created_at=checked_at,
            session_id=ref.session_id,
            task_id=ref.task_id,
            idempotency_key=f"validation_result:{validation_id}",
            derived_refs=[ref],
            metadata={
                "validation_id": validation_id,
                "authoring_id": authoring_id,
                "decision_id": decision_id,
                "packet_id": packet_id,
                "outcome": outcome,
            },
        )
        self.ingest_event(event)

    def load_validation(self, validation_id: str) -> "ValidationResult | None":
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM validation_results WHERE validation_id=?",
                (validation_id,),
            ).fetchone()
            if row is None:
                return None
            from openspace.skill_engine.evolution.validator import ValidationResult

            return ValidationResult(
                validation_id=str(row["validation_id"]),
                authoring_id=str(row["authoring_id"]),
                decision_id=str(row["decision_id"]),
                packet_id=str(row["packet_id"]),
                outcome=str(row["outcome"]),
                deterministic_failures=_json_list(
                    row["deterministic_failures_json"]
                ),
                semantic_warnings=_json_list(row["semantic_warnings_json"]),
                changed_files=_json_list(row["changed_files_json"]),
                provenance_refs=_json_list(row["provenance_refs_json"]),
                checked_at=str(row["checked_at"]),
                checked_by=str(row["checked_by"]),
            )

    def persist_behavior_eval(self, result: Any) -> None:
        """Persist a behavior-evaluation gate result and index its ref."""

        eval_id = str(getattr(result, "eval_id", "") or "")
        if not eval_id:
            raise ValueError("SkillBehaviorEvalResult.eval_id is required")
        authoring_id = str(getattr(result, "authoring_id", "") or "")
        validation_id = str(getattr(result, "validation_id", "") or "")
        decision_id = str(getattr(result, "decision_id", "") or "")
        packet_id = str(getattr(result, "packet_id", "") or "")
        action_type = str(getattr(result, "action_type", "") or "")
        outcome = str(getattr(result, "outcome", "") or "")
        failures = _str_list(getattr(result, "failures", []))
        warnings = _str_list(getattr(result, "warnings", []))
        contract_eval = _to_dict(getattr(result, "contract_eval", None))
        routing_eval = _to_dict(getattr(result, "routing_eval", None))
        replay_eval = _to_dict(getattr(result, "replay_eval", None))
        contract_snapshot = _dict_or_empty(getattr(result, "contract_snapshot", {}))
        checked_at = str(getattr(result, "checked_at", "") or _utc_now())
        checked_by = str(getattr(result, "checked_by", "") or "behavior_eval")

        with self._mu:
            self._ensure_open()
            self._conn.execute(
                """
                INSERT OR REPLACE INTO behavior_eval_results (
                    eval_id, authoring_id, validation_id, decision_id, packet_id,
                    action_type, outcome, failures_json, warnings_json,
                    contract_eval_json, routing_eval_json, trigger_eval_json,
                    replay_eval_json, contract_snapshot_json, checked_at, checked_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    eval_id,
                    authoring_id,
                    validation_id,
                    decision_id,
                    packet_id,
                    action_type,
                    outcome,
                    _json(failures),
                    _json(warnings),
                    _json(contract_eval),
                    _json(routing_eval),
                    _json(routing_eval),
                    _json(replay_eval),
                    _json(contract_snapshot),
                    checked_at,
                    checked_by,
                ),
            )
            self._conn.commit()

        packet = self.load_packet(packet_id) if packet_id else None
        raw_backrefs = [
            item
            for item in dict.fromkeys(
                [
                    f"authoring:{authoring_id}" if authoring_id else "",
                    f"validation:{validation_id}" if validation_id else "",
                    f"decision:{decision_id}" if decision_id else "",
                    f"packet:{packet_id}" if packet_id else "",
                ]
            )
            if item
        ]
        ref = ResourceRef(
            ref_id=f"behavior_eval:{eval_id}",
            ref_type="behavior_eval_result_ref",
            session_id=packet.scope.session_id if packet is not None else None,
            task_id=packet.scope.task_id if packet is not None else None,
            producer=checked_by,
            created_at=checked_at,
            reliability="derived",
            role="derived",
            preview=(
                f"behavior_eval {outcome}"
                + (f" failures={','.join(failures[:4])}" if failures else "")
            )[:500],
            metadata={
                "eval_id": eval_id,
                "authoring_id": authoring_id,
                "validation_id": validation_id,
                "decision_id": decision_id,
                "packet_id": packet_id,
                "action_type": action_type,
                "outcome": outcome,
                "failures": failures,
                "warnings": warnings,
                "contract_eval": contract_eval,
                "routing_eval": routing_eval,
                "replay_eval": replay_eval,
            },
            raw_backrefs=raw_backrefs,
        )
        event = EvidenceEvent.create(
            event_id=f"evt_behavior_eval_{_digest(eval_id)}",
            event_type="behavior_eval_result_persisted",
            producer=checked_by,
            created_at=checked_at,
            session_id=ref.session_id,
            task_id=ref.task_id,
            idempotency_key=f"behavior_eval_result:{eval_id}",
            derived_refs=[ref],
            metadata={
                "eval_id": eval_id,
                "authoring_id": authoring_id,
                "validation_id": validation_id,
                "decision_id": decision_id,
                "packet_id": packet_id,
                "outcome": outcome,
            },
        )
        self.ingest_event(event)

    def load_behavior_eval(self, eval_id: str) -> Any:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM behavior_eval_results WHERE eval_id=?",
                (eval_id,),
            ).fetchone()
            if row is None:
                return None
            from openspace.skill_engine.evolution.behavior_eval import (
                SkillBehaviorEvalResult,
            )

            return SkillBehaviorEvalResult.from_mapping(
                {
                    "eval_id": row["eval_id"],
                    "authoring_id": row["authoring_id"],
                    "validation_id": row["validation_id"],
                    "decision_id": row["decision_id"],
                    "packet_id": row["packet_id"],
                    "action_type": row["action_type"],
                    "outcome": row["outcome"],
                    "failures": _json_list(row["failures_json"]),
                    "warnings": _json_list(row["warnings_json"]),
                    "contract_eval": _json_object(
                        _row_value(row, "contract_eval_json")
                    ),
                    "routing_eval": _json_object(
                        _row_value(row, "routing_eval_json")
                    ),
                    "trigger_eval": _json_object(row["trigger_eval_json"]),
                    "replay_eval": _json_object(row["replay_eval_json"]),
                    "contract_snapshot": _json_object(row["contract_snapshot_json"]),
                    "checked_at": row["checked_at"],
                    "checked_by": row["checked_by"],
                }
            )

    def begin_action(
        self,
        *,
        decision_id: str,
        trigger_job_id: str,
        authoring_id: str,
        validation_id: str,
        action_type: str,
        staging_dir: str,
        active_target_dir: str,
        skill_id: str | None = None,
        parent_skill_ids: list[str] | None = None,
        changed_files: list[str] | None = None,
        evidence_refs: list[str] | None = None,
        backup_dir: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        action_id: str | None = None,
        raw_backrefs: list[str] | None = None,
    ) -> EvolutionActionRecord:
        """Start an audited evolution commit before touching active disk."""

        EvolutionActionRecord = _evolution_action_record_cls()
        resolved_action_id = action_id or f"act_{uuid.uuid4().hex}"
        created_at = _utc_now()
        record = EvolutionActionRecord(
            action_id=resolved_action_id,
            decision_id=decision_id,
            trigger_job_id=trigger_job_id,
            authoring_id=authoring_id,
            validation_id=validation_id,
            action_type=action_type,
            commit_status="committing",
            skill_id=skill_id,
            parent_skill_ids=_str_list(parent_skill_ids),
            changed_files=_str_list(changed_files),
            evidence_refs=_str_list(evidence_refs),
            staging_dir=staging_dir,
            active_target_dir=active_target_dir,
            backup_dir=backup_dir,
            failure_reason=None,
            created_at=created_at,
            committed_at=None,
        )
        ref = _action_resource_ref(
            record,
            session_id=session_id,
            task_id=task_id,
            raw_backrefs=raw_backrefs,
        )
        event = EvidenceEvent.create(
            event_id=f"evt_action_begin_{_digest(resolved_action_id)}",
            event_type="evolution_action_status",
            producer="evolution_committer",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            idempotency_key=f"evolution_action_begin:{resolved_action_id}",
            derived_refs=[ref],
            metadata={
                "action_id": resolved_action_id,
                "commit_status": "committing",
                "action_type": action_type,
            },
        )
        self._validate_event(event)
        with self._mu:
            self._ensure_open()
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    """
                    INSERT INTO evolution_actions (
                        action_id, decision_id, trigger_job_id, authoring_id,
                        validation_id, action_type, commit_status, skill_id,
                        parent_skill_ids_json, changed_files_json,
                        evidence_refs_json, staging_dir, active_target_dir,
                        backup_dir, failure_reason, created_at, committed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _action_row_values(record),
                )
                self._ingest_event_locked(event, redact_metadata(event.metadata))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return record

    def finalize_action(
        self,
        action_id: str,
        *,
        status: str,
        skill_id: str | None = None,
        changed_files: list[str] | None = None,
        backup_dir: str | None = None,
        failure_reason: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        raw_backrefs: list[str] | None = None,
    ) -> EvolutionActionRecord:
        """Finalize an evolution action and append a new action observation."""

        if status not in _evolution_action_statuses():
            raise ValueError(f"Unsupported evolution action status: {status}")
        now = _utc_now()
        committed_at = now if status in {"committed", "committed_reconciled"} else None
        with self._mu:
            self._ensure_open()
            existing = self._conn.execute(
                "SELECT * FROM evolution_actions WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if existing is None:
                raise ValueError(f"Unknown evolution action: {action_id}")

            parent_skill_ids = _json_list(existing["parent_skill_ids_json"])
            evidence_refs = _json_list(existing["evidence_refs_json"])
            resolved_skill_id = skill_id if skill_id is not None else _none_or_str(existing["skill_id"])
            resolved_changed_files = (
                _str_list(changed_files)
                if changed_files is not None
                else _json_list(existing["changed_files_json"])
            )
            resolved_backup_dir = (
                backup_dir if backup_dir is not None else _none_or_str(existing["backup_dir"])
            )
            EvolutionActionRecord = _evolution_action_record_cls()
            record = EvolutionActionRecord(
                action_id=action_id,
                decision_id=str(existing["decision_id"]),
                trigger_job_id=str(existing["trigger_job_id"]),
                authoring_id=str(existing["authoring_id"]),
                validation_id=str(existing["validation_id"]),
                action_type=str(existing["action_type"]),
                commit_status=status,
                skill_id=resolved_skill_id,
                parent_skill_ids=parent_skill_ids,
                changed_files=resolved_changed_files,
                evidence_refs=evidence_refs,
                staging_dir=str(existing["staging_dir"]),
                active_target_dir=str(existing["active_target_dir"]),
                backup_dir=resolved_backup_dir,
                failure_reason=failure_reason,
                created_at=str(existing["created_at"]),
                committed_at=committed_at,
            )
            ref = _action_resource_ref(
                record,
                session_id=session_id,
                task_id=task_id,
                raw_backrefs=raw_backrefs,
            )
            event = EvidenceEvent.create(
                event_id=f"evt_action_finalize_{_digest([action_id, status, now])}",
                event_type="evolution_action_status",
                producer="evolution_committer",
                created_at=now,
                session_id=session_id,
                task_id=task_id,
                idempotency_key=f"evolution_action_finalize:{action_id}:{status}:{_digest(now)}",
                derived_refs=[ref],
                metadata={
                    "action_id": action_id,
                    "commit_status": status,
                    "failure_reason": failure_reason,
                },
            )
            self._validate_event(event)
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    """
                    UPDATE evolution_actions
                    SET commit_status=?,
                        skill_id=?,
                        changed_files_json=?,
                        backup_dir=?,
                        failure_reason=?,
                        committed_at=?
                    WHERE action_id=?
                    """,
                    (
                        status,
                        resolved_skill_id,
                        _json(resolved_changed_files),
                        resolved_backup_dir,
                        failure_reason,
                        committed_at,
                        action_id,
                    ),
                )
                self._ingest_event_locked(event, redact_metadata(event.metadata))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return record

    def record_action_failure(
        self,
        action_id: str,
        *,
        phase: str,
        status: str,
        error: str,
        details: dict[str, Any] | None = None,
    ) -> str:
        failure_id = f"actfail_{uuid.uuid4().hex}"
        with self._mu:
            self._ensure_open()
            self._conn.execute(
                """
                INSERT INTO evolution_action_failures (
                    failure_id, action_id, phase, status, error,
                    details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    failure_id,
                    action_id,
                    phase,
                    status,
                    error,
                    _json(details or {}),
                    _utc_now(),
                ),
            )
            self._conn.commit()
        return failure_id

    def load_action(self, action_id: str) -> EvolutionActionRecord | None:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM evolution_actions WHERE action_id=?",
                (action_id,),
            ).fetchone()
            return _action_from_row(row) if row is not None else None

    def load_committed_action_for_decision(
        self,
        decision_id: str,
    ) -> EvolutionActionRecord | None:
        """Return the latest durable commit for a retried decision."""

        with self._reader() as conn:
            row = conn.execute(
                """
                SELECT * FROM evolution_actions
                WHERE decision_id=?
                  AND commit_status IN ('committed', 'committed_reconciled')
                ORDER BY COALESCE(committed_at, created_at) DESC
                LIMIT 1
                """,
                (str(decision_id),),
            ).fetchone()
            return _action_from_row(row) if row is not None else None

    def list_actions(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[EvolutionActionRecord]:
        with self._reader() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM evolution_actions ORDER BY created_at LIMIT ?",
                    (int(limit),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM evolution_actions
                    WHERE commit_status=?
                    ORDER BY created_at
                    LIMIT ?
                    """,
                    (status, int(limit)),
                ).fetchall()
            return [_action_from_row(row) for row in rows]

    def list_action_failures(self, action_id: str) -> list[dict[str, Any]]:
        with self._reader() as conn:
            rows = conn.execute(
                """
                SELECT * FROM evolution_action_failures
                WHERE action_id=?
                ORDER BY created_at
                """,
                (action_id,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["details"] = _json_object(item.pop("details_json", "{}"))
                result.append(item)
            return result

    def create_or_merge_candidate(
        self,
        decision: Any,
        admission: "AdmissionResult",
        packet: EvidencePacket,
    ) -> Any:
        from openspace.skill_engine.evolution.candidates import EvolutionCandidateStore

        store = EvolutionCandidateStore(evidence_store=self)
        try:
            return store.create_or_merge(decision, admission, packet)
        finally:
            store.close()

    def load_candidate(self, candidate_id: str) -> Any:
        from openspace.skill_engine.evolution.candidates import EvolutionCandidateStore

        store = EvolutionCandidateStore(evidence_store=self)
        try:
            return store.load_candidate(candidate_id)
        finally:
            store.close()

    def list_candidates(
        self,
        status: str = "pending",
        limit: int = 100,
    ) -> list[Any]:
        from openspace.skill_engine.evolution.candidates import EvolutionCandidateStore

        store = EvolutionCandidateStore(evidence_store=self)
        try:
            return store.list_candidates(status=status, limit=limit)
        finally:
            store.close()

    def load_candidates_by_admission(self, admission_id: str) -> list[Any]:
        from openspace.skill_engine.evolution.candidates import EvolutionCandidateStore

        store = EvolutionCandidateStore(evidence_store=self)
        try:
            return store.load_candidates_by_admission(admission_id)
        finally:
            store.close()

    def update_candidate_status(
        self,
        candidate_id: str,
        status: str,
        *,
        promoted_action_id: str | None = None,
        rejection_reason: str | None = None,
    ) -> Any:
        from openspace.skill_engine.evolution.candidates import EvolutionCandidateStore

        store = EvolutionCandidateStore(evidence_store=self)
        try:
            return store.update_candidate_status(
                candidate_id,
                status,
                promoted_action_id=promoted_action_id,
                rejection_reason=rejection_reason,
            )
        finally:
            store.close()

    def persist_execution_analysis_ref(
        self,
        analysis: Any,
        packet: EvidencePacket,
        *,
        source_analysis_id: str | None = None,
    ) -> str:
        """Index a SkillStore ExecutionAnalysis as derived evidence."""

        task_id = str(getattr(analysis, "task_id", "") or packet.scope.task_id or "")
        analysis_ref_id = source_analysis_id or f"analysis:{task_id or packet.packet_id}"
        selected_ref_ids = sorted(
            {
                ref.ref_id
                for refs in packet.selected_refs.values()
                for ref in refs
                if ref.ref_id
            }
        )
        raw_backrefs = [f"packet:{packet.packet_id}", *selected_ref_ids]
        created_at = _utc_now()
        suggestions = [
            getattr(getattr(item, "evolution_type", None), "value", None)
            or str(getattr(item, "evolution_type", "") or "")
            for item in (getattr(analysis, "evolution_suggestions", []) or [])
        ]
        ref = ResourceRef(
            ref_id=analysis_ref_id,
            ref_type="execution_analysis",
            session_id=packet.scope.session_id,
            task_id=packet.scope.task_id or task_id,
            producer="execution_analyzer",
            created_at=created_at,
            reliability="derived",
            role="derived",
            preview=str(getattr(analysis, "execution_note", "") or "")[:500],
            metadata={
                "analysis_ref_id": analysis_ref_id,
                "task_id": task_id,
                "packet_id": packet.packet_id,
                "trigger_job_id": packet.trigger_job_id,
                "task_completed": bool(getattr(analysis, "task_completed", False)),
                "tool_issues": _str_list(getattr(analysis, "tool_issues", [])),
                "evolution_suggestion_types": [
                    item for item in suggestions if item
                ],
                "analyzed_by": str(getattr(analysis, "analyzed_by", "") or ""),
                "analyzed_at": str(getattr(analysis, "analyzed_at", "") or ""),
            },
            raw_backrefs=list(dict.fromkeys(raw_backrefs)),
        )
        event = EvidenceEvent.create(
            event_id=f"evt_analysis_ref_{_digest(analysis_ref_id)}",
            event_type="execution_analysis_persisted",
            producer="execution_analyzer",
            created_at=created_at,
            session_id=packet.scope.session_id,
            task_id=packet.scope.task_id or task_id,
            idempotency_key=f"execution_analysis_ref:{analysis_ref_id}",
            derived_refs=[ref],
            metadata={
                "analysis_ref_id": analysis_ref_id,
                "packet_id": packet.packet_id,
                "trigger_job_id": packet.trigger_job_id,
            },
        )
        self.ingest_event(event)
        return analysis_ref_id

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()
        except Exception:
            pass

    def _upsert_ref_locked(
        self,
        ref: ResourceRef,
        *,
        event: EvidenceEvent | None,
        event_id: str,
        watermark: int,
        default_role: str,
    ) -> None:
        normalized = self._normalize_ref(
            ref,
            event=event,
            watermark=watermark,
            default_role=default_role,
        )
        existing = self._conn.execute(
            "SELECT first_event_id, first_seen_watermark FROM resource_refs WHERE ref_id = ?",
            (normalized.ref_id,),
        ).fetchone()
        metadata_json = _json(normalized.metadata)
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO resource_refs (
                    ref_id, ref_type, uri, session_id, task_id, parent_task_id,
                    turn_id, agent_id, producer, created_at, reliability, role,
                    content_hash, preview, metadata_json, contains_secret,
                    first_event_id, last_event_id, first_seen_watermark,
                    last_seen_watermark
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _ref_row_values(
                    normalized,
                    first_event_id=event_id,
                    last_event_id=event_id,
                    first_seen_watermark=watermark,
                    last_seen_watermark=watermark,
                    metadata_json=metadata_json,
                ),
            )
            first_seen_watermark = watermark
        else:
            first_seen_watermark = int(existing["first_seen_watermark"])
            self._conn.execute(
                """
                UPDATE resource_refs
                SET ref_type = ?, uri = ?, session_id = ?, task_id = ?,
                    parent_task_id = ?, turn_id = ?, agent_id = ?,
                    producer = ?, created_at = ?, reliability = ?, role = ?,
                    content_hash = ?, preview = ?, metadata_json = ?,
                    contains_secret = ?, last_event_id = ?,
                    last_seen_watermark = ?
                WHERE ref_id = ?
                """,
                (
                    normalized.ref_type,
                    normalized.uri,
                    normalized.session_id,
                    normalized.task_id,
                    normalized.parent_task_id,
                    normalized.turn_id,
                    normalized.agent_id,
                    normalized.producer,
                    normalized.created_at,
                    normalized.reliability,
                    normalized.role,
                    normalized.hash,
                    normalized.preview,
                    metadata_json,
                    1 if normalized.contains_secret else 0,
                    event_id,
                    watermark,
                    normalized.ref_id,
                ),
            )

        self._conn.execute(
            """
            INSERT OR REPLACE INTO resource_ref_observations (
                ref_id, watermark, event_id, ref_type, uri, session_id, task_id,
                parent_task_id, turn_id, agent_id, producer, created_at,
                content_hash, preview, metadata_json, reliability, role,
                raw_backrefs_json, contains_secret, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized.ref_id,
                watermark,
                event_id,
                normalized.ref_type,
                normalized.uri,
                normalized.session_id,
                normalized.task_id,
                normalized.parent_task_id,
                normalized.turn_id,
                normalized.agent_id,
                normalized.producer,
                normalized.created_at,
                normalized.hash,
                normalized.preview,
                metadata_json,
                normalized.reliability,
                normalized.role,
                _json(normalized.raw_backrefs),
                1 if normalized.contains_secret else 0,
                _utc_now(),
            ),
        )
        for raw_ref_id in normalized.raw_backrefs:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO resource_ref_links (
                    derived_ref_id, raw_ref_id, link_type, metadata_json
                ) VALUES (?, ?, 'derived_from', '{}')
                """,
                (normalized.ref_id, raw_ref_id),
            )

    def _normalize_ref(
        self,
        ref: ResourceRef,
        *,
        event: EvidenceEvent | None,
        watermark: int,
        default_role: str,
    ) -> ResourceRef:
        self._validate_ref(ref)
        role = ref.role if ref.role in ALLOWED_ROLES else default_role
        metadata = redact_metadata(ref.metadata)
        if role == "derived" and not ref.raw_backrefs:
            role = "supporting"
            metadata = dict(metadata)
            metadata["derived_without_raw_backrefs"] = True
        preview = redact_text(ref.preview or "")
        uri = redact_text(ref.uri) if ref.uri else None
        secret = (
            bool(ref.contains_secret)
            or contains_secret(ref.preview)
            or contains_secret(ref.metadata)
            or contains_secret(ref.uri)
        )
        content_hash = ref.hash or _hash_payload(preview, metadata, uri)
        return replace(
            ref,
            uri=uri,
            session_id=ref.session_id if ref.session_id is not None else getattr(event, "session_id", None),
            task_id=ref.task_id if ref.task_id is not None else getattr(event, "task_id", None),
            parent_task_id=(
                ref.parent_task_id
                if ref.parent_task_id is not None
                else getattr(event, "parent_task_id", None)
            ),
            turn_id=ref.turn_id if ref.turn_id is not None else getattr(event, "turn_id", None),
            agent_id=ref.agent_id if ref.agent_id is not None else getattr(event, "agent_id", None),
            producer=ref.producer or getattr(event, "producer", "unknown"),
            created_at=ref.created_at or getattr(event, "created_at", "") or _utc_now(),
            reliability=(
                ref.reliability
                if ref.reliability in ALLOWED_RELIABILITY
                else "runtime"
            ),
            role=role,
            hash=content_hash,
            preview=preview,
            metadata=metadata,
            contains_secret=secret,
            first_seen_watermark=ref.first_seen_watermark or watermark,
            last_seen_watermark=watermark,
        )

    def _get_ref_at_conn(
        self,
        conn: sqlite3.Connection,
        ref_id: str,
        watermark: int,
    ) -> ResourceRef | None:
        row = conn.execute(
            "SELECT * FROM resource_refs WHERE ref_id = ? AND first_seen_watermark <= ?",
            (ref_id, watermark),
        ).fetchone()
        if row is None:
            return None
        observation = conn.execute(
            """
            SELECT * FROM resource_ref_observations
            WHERE ref_id = ? AND watermark <= ?
            ORDER BY watermark DESC
            LIMIT 1
            """,
            (ref_id, watermark),
        ).fetchone()
        if observation is None:
            return None
        return self._row_to_ref(
            row,
            observation=observation,
            raw_backrefs=_raw_backrefs_from_observation(observation),
            first_seen_watermark=int(row["first_seen_watermark"]),
            last_seen_watermark=int(observation["watermark"]),
        )

    def _row_to_ref(
        self,
        row: sqlite3.Row,
        *,
        observation: sqlite3.Row | None = None,
        raw_backrefs: list[str],
        first_seen_watermark: int,
        last_seen_watermark: int,
    ) -> ResourceRef:
        source = observation or row
        return ResourceRef(
            ref_id=str(row["ref_id"]),
            ref_type=str(_row_value(source, "ref_type") or ""),
            uri=_none_or_str(_row_value(source, "uri")),
            session_id=_none_or_str(_row_value(source, "session_id")),
            task_id=_none_or_str(_row_value(source, "task_id")),
            parent_task_id=_none_or_str(_row_value(source, "parent_task_id")),
            turn_id=_none_or_str(_row_value(source, "turn_id")),
            agent_id=_none_or_str(_row_value(source, "agent_id")),
            producer=str(_row_value(source, "producer") or "unknown"),
            created_at=str(_row_value(source, "created_at") or ""),
            reliability=str(_row_value(source, "reliability") or "runtime"),
            role=str(_row_value(source, "role") or "supporting"),
            hash=_none_or_str(_row_value(source, "content_hash")),
            preview=str(_row_value(source, "preview") or ""),
            metadata=_json_object(_row_value(source, "metadata_json")),
            raw_backrefs=raw_backrefs,
            contains_secret=bool(_row_value(source, "contains_secret")),
            first_seen_watermark=first_seen_watermark,
            last_seen_watermark=last_seen_watermark,
        )

    def _raw_backrefs(self, conn: sqlite3.Connection, ref_id: str) -> list[str]:
        rows = conn.execute(
            """
            SELECT raw_ref_id FROM resource_ref_links
            WHERE derived_ref_id = ?
            ORDER BY raw_ref_id
            """,
            (ref_id,),
        ).fetchall()
        return [str(row["raw_ref_id"]) for row in rows]

    def _latest_watermark(self) -> int:
        with self._reader() as conn:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) AS watermark FROM evidence_events").fetchone()
            return int(row["watermark"] if row is not None else 0)

    def _validate_event(self, event: EvidenceEvent) -> None:
        if not event.event_id:
            raise ValueError("EvidenceEvent.event_id is required")
        if not event.event_type:
            raise ValueError("EvidenceEvent.event_type is required")
        if not event.producer:
            raise ValueError("EvidenceEvent.producer is required")
        if event.severity not in ALLOWED_SEVERITY:
            raise ValueError(f"Unsupported EvidenceEvent.severity: {event.severity}")
        if not event.idempotency_key:
            raise ValueError("EvidenceEvent.idempotency_key is required")
        for ref in event.all_refs():
            self._validate_ref(ref)

    def _validate_ref(self, ref: ResourceRef) -> None:
        if not ref.ref_id:
            raise ValueError("ResourceRef.ref_id is required")
        if ref.ref_type not in ALLOWED_REF_TYPES:
            raise ValueError(f"Unsupported ResourceRef.ref_type: {ref.ref_type}")
        if ref.reliability not in ALLOWED_RELIABILITY:
            raise ValueError(f"Unsupported ResourceRef.reliability: {ref.reliability}")
        if ref.role not in ALLOWED_ROLES:
            raise ValueError(f"Unsupported ResourceRef.role: {ref.role}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("EvidenceStore is closed")

    def _default_allowed_read_roots(self) -> tuple[Path, ...]:
        db_parent = self._db_path.parent
        if db_parent.name == ".openspace":
            return (db_parent.parent.resolve(),)
        return (db_parent.resolve(),)

    @property
    def allowed_read_roots(self) -> tuple[Path, ...]:
        return self._allowed_read_roots

    def add_allowed_read_root(self, root: str | Path | None) -> None:
        if root is None:
            return
        self._allowed_read_roots = self._merge_allowed_read_roots(
            self._allowed_read_roots,
            (root,),
        )

    def add_allowed_read_roots(self, roots: list[str | Path] | tuple[str | Path, ...]) -> None:
        self._allowed_read_roots = self._merge_allowed_read_roots(
            self._allowed_read_roots,
            roots,
        )

    def _merge_allowed_read_roots(
        self,
        base: tuple[Path, ...],
        extra: list[str | Path] | tuple[str | Path, ...],
    ) -> tuple[Path, ...]:
        roots: list[Path] = []
        seen: set[str] = set()
        for item in [*base, *extra]:
            try:
                resolved = Path(item).expanduser().resolve()
            except (OSError, TypeError, ValueError):
                continue
            if _is_sensitive_path(resolved):
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            roots.append(resolved)
        return tuple(roots)

    def _path_read_allowed(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return False
        if _is_sensitive_path(resolved):
            return False
        return any(
            resolved == root or root in resolved.parents
            for root in self._allowed_read_roots
        )


def resolve_evolution_storage_root(
    *,
    explicit_root: str | Path | None = None,
    explicit_db_path: str | Path | None = None,
    session_storage: Any | None = None,
    skill_store: Any | None = None,
    workspace_dir: str | Path | None = None,
) -> Path:
    """Resolve the storage root without falling back to package PROJECT_ROOT."""

    if explicit_root:
        return Path(explicit_root).expanduser().resolve()

    if explicit_db_path:
        return _storage_root_from_db_path(Path(explicit_db_path).expanduser().resolve())

    if workspace_dir:
        return Path(workspace_dir).expanduser().resolve()

    project_root = getattr(session_storage, "project_root", None)
    if project_root:
        return Path(project_root).expanduser().resolve()

    cwd = getattr(session_storage, "cwd", None)
    if cwd:
        return Path(cwd).expanduser().resolve()

    session_dir = getattr(session_storage, "session_dir", None)
    if session_dir:
        path = Path(session_dir).expanduser().resolve()
        if path.parent.name == "sessions":
            return path.parent.parent
        return path.parent

    db_path = getattr(skill_store, "db_path", None)
    if db_path:
        return _storage_root_from_db_path(Path(db_path).expanduser().resolve())

    return Path(workspace_dir or Path.cwd()).expanduser().resolve()


def resolve_evidence_db_path(
    *,
    explicit_db_path: str | Path | None = None,
    storage_root: str | Path | None = None,
    session_storage: Any | None = None,
    skill_store: Any | None = None,
    workspace_dir: str | Path | None = None,
) -> Path:
    if explicit_db_path:
        return Path(explicit_db_path).expanduser().resolve()
    root = resolve_evolution_storage_root(
        explicit_root=storage_root,
        explicit_db_path=explicit_db_path,
        session_storage=session_storage,
        skill_store=skill_store,
        workspace_dir=workspace_dir,
    )
    return root / ".openspace" / "evidence.db"


def resolve_skill_store_db_path(
    *,
    explicit_db_path: str | Path | None = None,
    storage_root: str | Path | None = None,
    session_storage: Any | None = None,
    skill_store: Any | None = None,
    workspace_dir: str | Path | None = None,
) -> Path:
    if explicit_db_path:
        return Path(explicit_db_path).expanduser().resolve()
    root = resolve_evolution_storage_root(
        explicit_root=storage_root,
        session_storage=session_storage,
        skill_store=skill_store,
        workspace_dir=workspace_dir,
    )
    return root / ".openspace" / "openspace.db"


def _scope_matches(ref: ResourceRef, scope: EvidenceScope) -> bool:
    scoped_task_ids = {item for item in scope.source_task_ids if item}
    has_task_filter = bool(scope.task_id or scoped_task_ids)
    has_context_filter = bool(
        scope.session_id
        or scope.task_id
        or scoped_task_ids
        or scope.agent_ids
    )
    if scope.task_id:
        scoped_task_ids.add(scope.task_id)
    session_match = _scope_dimension_match(scope.session_id, ref.session_id)
    task_match = _scope_set_match(
        scoped_task_ids,
        {
            item
            for item in (
                ref.task_id,
                ref.parent_task_id,
                *_metadata_values(ref.metadata, "task_id", "task_ids"),
                *_metadata_values(ref.metadata, "parent_task_id", "parent_task_ids"),
            )
            if item
        },
    )
    agent_match = _scope_set_match(
        set(scope.agent_ids),
        {ref.agent_id} if ref.agent_id else set(),
    )

    if not (scope.skill_ids or scope.tool_keys):
        if scope.session_id and session_match is not True:
            return False
        if has_task_filter and task_match is not True:
            return False
        if scope.agent_ids and agent_match is not True:
            return False
        return True

    skill_values = _metadata_values(
        ref.metadata,
        "skill_id",
        "skill_ids",
        "affected_skill_id",
        "affected_skill_ids",
        "target_skill_id",
        "target_skill_ids",
    )
    tool_values = _metadata_values(ref.metadata, "tool_key", "tool_keys")
    skill_match = bool(scope.skill_ids and skill_values.intersection(scope.skill_ids))
    tool_match = bool(scope.tool_keys and tool_values.intersection(scope.tool_keys))
    target_match = skill_match or tool_match
    context_match = _same_task_or_session_context(
        session_match=session_match,
        task_match=task_match,
        agent_match=agent_match,
        has_task_filter=has_task_filter,
    )
    if _is_target_resource_ref(ref, skill_values, tool_values):
        if scope.skill_ids and skill_values and not skill_match and not tool_match:
            return False
        if scope.tool_keys and tool_values and not tool_match and not skill_match:
            return False

    if target_match:
        return (
            session_match is not False
            and task_match is not False
            and agent_match is not False
        )

    # skill_ids/tool_keys in a TriggerJob identify targets for packet
    # construction and ranking. They must not discard same task/session
    # context such as transcript/runtime refs or tool timeline refs that carry
    # different skill/tool metadata. Without a context filter, target scope is
    # hard to avoid pulling unrelated global refs into manual/metric packets.
    return bool(has_context_filter and context_match)


def _storage_root_from_db_path(db_path: Path) -> Path:
    db_parent = db_path.parent
    if db_parent.name == ".openspace":
        return db_parent.parent
    return db_parent


def _metadata_values(metadata: dict[str, Any], *keys: str) -> set[str]:
    values: set[str] = set()
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value:
            values.add(value)
        elif isinstance(value, (list, tuple, set)):
            values.update(str(item) for item in value if str(item))
    return values


def _is_target_resource_ref(
    ref: ResourceRef,
    skill_values: set[str],
    tool_values: set[str],
) -> bool:
    if ref.ref_type in {"skill_file", "skill_record"} and skill_values:
        return True
    if ref.ref_type in {"tool_quality_record", "tool_incident"} and tool_values:
        return True
    return False


def _scope_dimension_match(expected: str | None, actual: str | None) -> bool | None:
    if not expected:
        return True
    if not actual:
        return None
    return actual == expected


def _scope_set_match(expected: set[str], actual: set[str]) -> bool | None:
    if not expected:
        return True
    if not actual:
        return None
    return bool(expected.intersection(actual))


def _same_task_or_session_context(
    *,
    session_match: bool | None,
    task_match: bool | None,
    agent_match: bool | None,
    has_task_filter: bool,
) -> bool:
    if session_match is False or task_match is False or agent_match is False:
        return False
    if has_task_filter:
        return task_match is True
    return session_match is True


def _ref_row_values(
    ref: ResourceRef,
    *,
    first_event_id: str,
    last_event_id: str,
    first_seen_watermark: int,
    last_seen_watermark: int,
    metadata_json: str,
) -> tuple[Any, ...]:
    return (
        ref.ref_id,
        ref.ref_type,
        ref.uri,
        ref.session_id,
        ref.task_id,
        ref.parent_task_id,
        ref.turn_id,
        ref.agent_id,
        ref.producer,
        ref.created_at,
        ref.reliability,
        ref.role,
        ref.hash,
        ref.preview,
        metadata_json,
        1 if ref.contains_secret else 0,
        first_event_id,
        last_event_id,
        first_seen_watermark,
        last_seen_watermark,
    )


def _hash_payload(preview: str, metadata: Any, uri: str | None) -> str:
    payload = json.dumps(
        {"preview": preview, "metadata": metadata, "uri": uri},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item)]


def _to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        return dict(data) if isinstance(data, dict) else {}
    return {}


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _raw_backrefs_from_observation(observation: sqlite3.Row) -> list[str]:
    return _json_list(_row_value(observation, "raw_backrefs_json"))


def _row_value(row: sqlite3.Row, key: str) -> Any:
    if key not in row.keys():
        return None
    return row[key]


def _none_or_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return []


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _decision_raw_backrefs(decision: Any, packet_id: str) -> list[str]:
    refs: list[str] = []
    if packet_id:
        refs.append(f"packet:{packet_id}")
    source_analysis_id = _none_or_str(getattr(decision, "source_analysis_id", None))
    if source_analysis_id:
        refs.append(source_analysis_id)
    for claim in list(getattr(decision, "evidence_claims", []) or []):
        refs.extend(_str_list(getattr(claim, "refs", [])))
    return list(dict.fromkeys(refs))


def _packet_session_id(store: EvidenceStore, packet_id: str) -> str | None:
    packet = store.load_packet(packet_id) if packet_id else None
    return packet.scope.session_id if packet is not None else None


def _packet_task_id(store: EvidenceStore, packet_id: str) -> str | None:
    packet = store.load_packet(packet_id) if packet_id else None
    return packet.scope.task_id if packet is not None else None


def _action_resource_ref(
    record: EvolutionActionRecord,
    *,
    session_id: str | None,
    task_id: str | None,
    raw_backrefs: list[str] | None,
) -> ResourceRef:
    backrefs = list(
        dict.fromkeys(
            [
                f"decision:{record.decision_id}" if record.decision_id else "",
                f"authoring:{record.authoring_id}" if record.authoring_id else "",
                f"validation:{record.validation_id}" if record.validation_id else "",
                *record.evidence_refs,
                *(_str_list(raw_backrefs) if raw_backrefs is not None else []),
            ]
        )
    )
    backrefs = [item for item in backrefs if item]
    return ResourceRef(
        ref_id=f"evolution_action:{record.action_id}",
        ref_type="evolution_action_ref",
        uri=record.active_target_dir,
        session_id=session_id,
        task_id=task_id,
        producer="evolution_committer",
        created_at=record.committed_at or record.created_at,
        reliability="derived",
        role="derived",
        preview=(
            f"evolution action {record.action_type} {record.commit_status}"
            + (f": {record.failure_reason}" if record.failure_reason else "")
        )[:500],
        metadata=record.to_dict(),
        raw_backrefs=backrefs,
    )


def _action_row_values(record: EvolutionActionRecord) -> tuple[Any, ...]:
    return (
        record.action_id,
        record.decision_id,
        record.trigger_job_id,
        record.authoring_id,
        record.validation_id,
        record.action_type,
        record.commit_status,
        record.skill_id,
        _json(record.parent_skill_ids),
        _json(record.changed_files),
        _json(record.evidence_refs),
        record.staging_dir,
        record.active_target_dir,
        record.backup_dir,
        record.failure_reason,
        record.created_at,
        record.committed_at,
    )


def _action_from_row(row: sqlite3.Row) -> EvolutionActionRecord:
    EvolutionActionRecord = _evolution_action_record_cls()
    return EvolutionActionRecord(
        action_id=str(row["action_id"]),
        decision_id=str(row["decision_id"]),
        trigger_job_id=str(row["trigger_job_id"]),
        authoring_id=str(row["authoring_id"]),
        validation_id=str(row["validation_id"]),
        action_type=str(row["action_type"]),
        commit_status=str(row["commit_status"]),
        skill_id=_none_or_str(row["skill_id"]),
        parent_skill_ids=_json_list(row["parent_skill_ids_json"]),
        changed_files=_json_list(row["changed_files_json"]),
        evidence_refs=_json_list(row["evidence_refs_json"]),
        staging_dir=str(row["staging_dir"]),
        active_target_dir=str(row["active_target_dir"]),
        backup_dir=_none_or_str(row["backup_dir"]),
        failure_reason=_none_or_str(row["failure_reason"]),
        created_at=str(row["created_at"]),
        committed_at=_none_or_str(row["committed_at"]),
    )


def _evolution_action_record_cls() -> Any:
    from openspace.skill_engine.evolution.audit import EvolutionActionRecord

    return EvolutionActionRecord


def _evolution_action_statuses() -> frozenset[str]:
    from openspace.skill_engine.evolution.audit import EVOLUTION_ACTION_STATUSES

    return EVOLUTION_ACTION_STATUSES


def _is_sensitive_path(path: Path) -> bool:
    name = path.name.lower()
    return name == ".env" or name.startswith(".env.") or name.endswith(".env")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
