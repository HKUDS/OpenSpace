"""SQLite persistence for asynchronous research tracks and experiments."""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models import Artifact, ResearchBudgetUnavailable, Ticket, Track


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object(value: Mapping[str, Any] | None, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping")
    # Encoding here both validates persistence and takes a detached snapshot.
    loaded = json.loads(_json(dict(value)))
    if not isinstance(loaded, dict):  # pragma: no cover - guarded by Mapping above
        raise TypeError(f"{field} must encode to an object")
    return loaded


def _money(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return result


def external_worker_accounting(result: Mapping[str, Any]) -> bool:
    accounting = result.get("worker_accounting")
    return (isinstance(accounting, Mapping)
            and accounting.get("mode") == "external"
            and accounting.get("actual_cost_usd") is None
            and accounting.get("budget_policy") == "retain_reservation")


def _completion_cost(value, result):
    if value is None and external_worker_accounting(result):
        return None
    return _money(value, "cost_usd")


def _same_cost(left, right):
    if left is None or right is None:
        return left is right
    return math.isclose(left, right)


class ResearchStore:
    """A process-safe durable store with transactional budget accounting."""

    def __init__(self, path: str | Path, budget_usd: float | None = None, *, budget_mode: str | None = None):
        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        requested_budget = None if budget_usd is None else _money(budget_usd, "budget_usd")
        self._initialize(requested_budget)
        if budget_mode is not None:
            self.set_budget_mode(budget_mode)

    def set_budget_mode(self, mode: str) -> None:
        if mode not in {'reserved', 'actual'}:
            raise ValueError('budget_mode must be reserved or actual')
        with self._connect() as connection:
            connection.execute("INSERT INTO settings VALUES ('budget_mode',?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json", (_json(mode),))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self, requested_budget: float | None) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS counters (
                    namespace TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    commit_sha TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    base_id TEXT REFERENCES artifacts(id),
                    track_id TEXT REFERENCES tracks(id),
                    metadata_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tracks (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('active', 'dropped')),
                    reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY,
                    track_id TEXT NOT NULL REFERENCES tracks(id),
                    kind TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'running', 'succeeded', 'failed', 'accounting_unknown')
                    ),
                    reservation_usd REAL NOT NULL,
                    cost_usd REAL,
                    result_json TEXT,
                    artifact_id TEXT REFERENCES artifacts(id),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS tickets_status_idx ON tickets(status, created_at);
                CREATE INDEX IF NOT EXISTS tickets_track_idx ON tickets(track_id, created_at);
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_id TEXT NOT NULL REFERENCES tracks(id),
                    text TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_costs (
                    key TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('reserved', 'settled', 'accounting_unknown')),
                    reservation_usd REAL NOT NULL,
                    cost_usd REAL,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_billing (
                    key TEXT PRIMARY KEY REFERENCES external_costs(key)
                );
                CREATE TABLE IF NOT EXISTS documents (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS policy_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS followups (
                    parent_ticket TEXT PRIMARY KEY REFERENCES tickets(id),
                    plan_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    child_ticket TEXT REFERENCES tickets(id),
                    error TEXT,
                    reserved_upfront INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                note_columns = {r['name'] for r in connection.execute('PRAGMA table_info(notes)')}
                if 'context_json' not in note_columns:
                    connection.execute("ALTER TABLE notes ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'")
                if 'idempotency_key' not in note_columns:
                    connection.execute('ALTER TABLE notes ADD COLUMN idempotency_key TEXT')
                connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS notes_idempotency ON notes(idempotency_key)')
                columns = {r['name'] for r in connection.execute('PRAGMA table_info(tickets)')}
                if 'requested_track' not in columns:
                    connection.execute('ALTER TABLE tickets ADD COLUMN requested_track TEXT')
                followup_columns = {
                    r["name"] for r in connection.execute("PRAGMA table_info(followups)")
                }
                if "reserved_upfront" not in followup_columns:
                    connection.execute(
                        "ALTER TABLE followups ADD COLUMN reserved_upfront "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                connection.execute("INSERT OR IGNORE INTO settings VALUES ('namespace', ?)", (_json(uuid.uuid4().hex),))
                row = connection.execute(
                    "SELECT value_json FROM settings WHERE key = 'budget_usd'"
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO settings(key, value_json) VALUES ('budget_usd', ?)",
                        (_json(requested_budget),),
                    )
                elif requested_budget is not None:
                    existing = json.loads(row["value_json"])
                    if existing is None:
                        committed = self._budget_row(connection)
                        used = committed["spent_usd"] + committed["reserved_usd"]
                        if requested_budget + 1e-12 < used:
                            raise ValueError(
                                f"new budget ${requested_budget:.6f} is below already "
                                f"committed cost ${used:.6f}"
                            )
                        connection.execute(
                            "UPDATE settings SET value_json = ? WHERE key = 'budget_usd'",
                            (_json(requested_budget),),
                        )
                    elif not math.isclose(float(existing), requested_budget):
                        raise ValueError(
                            f"store budget is already {existing!r}, not {requested_budget!r}"
                        )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @property
    def namespace(self) -> str:
        with self._connect() as connection:
            return json.loads(connection.execute("SELECT value_json FROM settings WHERE key='namespace'").fetchone()[0])

    def put_document(self, key: str, value: Any) -> str:
        """Append immutable audit material; checkpoints keep only its address."""
        encoded = _json(value)
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('INSERT OR IGNORE INTO documents VALUES (?, ?)', (key, encoded))
            stored = connection.execute('SELECT value_json FROM documents WHERE key=?', (key,)).fetchone()[0]
            if stored != encoded:
                raise ValueError('Immutable research document changed')
        return key

    def load_document(self, key: str, default=None):
        with self._connect() as connection:
            row = connection.execute('SELECT value_json FROM documents WHERE key=?', (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def policy_event(self, event_id: str, kind: str, value: Mapping[str, Any]):
        """Append an exactly-once policy fact, rejecting replay with drift."""
        encoded = _json(_object(value, "policy event"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM policy_events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                connection.execute("INSERT INTO policy_events(event_id, kind, value_json) VALUES (?, ?, ?)",
                                   (event_id, kind, encoded))
                row = connection.execute("SELECT * FROM policy_events WHERE event_id=?", (event_id,)).fetchone()
            if row["kind"] != kind or row["value_json"] != encoded:
                raise ValueError("Policy event replay changed its frozen meaning")
        return {"sequence": row["sequence"], "event_id": event_id, "kind": kind, "value": json.loads(encoded)}

    def policy_events(self, kind=None):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM policy_events" + (" WHERE kind=?" if kind else "") + " ORDER BY sequence",
                (kind,) if kind else (),
            ).fetchall()
        return [{"sequence": row["sequence"], "event_id": row["event_id"],
                 "kind": row["kind"], "value": json.loads(row["value_json"])} for row in rows]

    @classmethod
    def _insert_ticket(cls, connection, kind, arguments, reservation, key, track, label, now, requested_track=None):
        ticket_id = cls._next_id(connection, 'ticket', 'X')
        connection.execute('''INSERT INTO tickets(
            id, track_id, kind, arguments_json, label, status, reservation_usd,
            idempotency_key, created_at, requested_track)
            VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)''',
            (ticket_id, track, kind, _json(arguments), label, reservation, key, now, requested_track))
        return ticket_id

    @staticmethod
    def _next_id(connection: sqlite3.Connection, namespace: str, prefix: str) -> str:
        row = connection.execute(
            "SELECT value FROM counters WHERE namespace = ?", (namespace,)
        ).fetchone()
        value = 1 if row is None else int(row["value"]) + 1
        if row is None:
            connection.execute(
                "INSERT INTO counters(namespace, value) VALUES (?, ?)", (namespace, value)
            )
        else:
            connection.execute(
                "UPDATE counters SET value = ? WHERE namespace = ?", (value, namespace)
            )
        return f"{prefix}{value}"

    @staticmethod
    def _artifact(row: sqlite3.Row) -> Artifact:
        return Artifact(
            id=row["id"], commit_sha=row["commit_sha"], config=json.loads(row["config_json"]),
            base_id=row["base_id"], track_id=row["track_id"],
            metadata=json.loads(row["metadata_json"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _track(row: sqlite3.Row) -> Track:
        return Track(
            id=row["id"], label=row["label"], state=row["state"], reason=row["reason"],
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _ticket(row: sqlite3.Row) -> Ticket:
        return Ticket(
            id=row["id"], track_id=row["track_id"], kind=row["kind"],
            arguments=json.loads(row["arguments_json"]), label=row["label"],
            status=row["status"], reservation_usd=float(row["reservation_usd"]),
            cost_usd=None if row["cost_usd"] is None else float(row["cost_usd"]),
            result=None if row["result_json"] is None else json.loads(row["result_json"]),
            artifact_id=row["artifact_id"], idempotency_key=row["idempotency_key"],
            created_at=float(row["created_at"]),
            started_at=None if row["started_at"] is None else float(row["started_at"]),
            finished_at=None if row["finished_at"] is None else float(row["finished_at"]),
            error=row["error"],
        )

    @staticmethod
    def _budget_row(connection: sqlite3.Connection) -> dict[str, Any]:
        limit = json.loads(connection.execute(
            "SELECT value_json FROM settings WHERE key = 'budget_usd'"
        ).fetchone()["value_json"])
        ticket = connection.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN cost_usd IS NOT NULL THEN cost_usd ELSE 0 END), 0) spent,
              COALESCE(SUM(CASE WHEN status IN ('queued','running','accounting_unknown')
                                THEN reservation_usd ELSE 0 END), 0) reserved,
              COALESCE(SUM(CASE WHEN status = 'accounting_unknown' THEN 1 ELSE 0 END), 0) unknown,
              COALESCE(SUM(CASE WHEN status IN ('succeeded','failed') AND cost_usd IS NULL
                                THEN reservation_usd ELSE 0 END), 0) external_allocation
            FROM tickets
            """
        ).fetchone()
        external = connection.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN cost_usd IS NOT NULL THEN cost_usd ELSE 0 END), 0) spent,
              COALESCE(SUM(CASE WHEN state IN ('reserved','accounting_unknown') AND billing.key IS NULL
                                THEN reservation_usd ELSE 0 END), 0) reserved,
              COALESCE(SUM(CASE WHEN state = 'accounting_unknown' THEN 1 ELSE 0 END), 0) unknown,
              COALESCE(SUM(CASE WHEN billing.key IS NOT NULL THEN reservation_usd ELSE 0 END), 0) external_allocation
            FROM external_costs LEFT JOIN external_billing billing USING (key)
            """
        ).fetchone()
        followup_reserved = sum(
            float(json.loads(row["plan_json"])["reservation_usd"])
            for row in connection.execute(
                "SELECT plan_json FROM followups "
                "WHERE reserved_upfront = 1 AND status IN ('waiting', 'blocked')"
            )
        )
        spent = float(ticket["spent"]) + float(external["spent"])
        reserved = (
            float(ticket["reserved"])
            + float(external["reserved"])
            + followup_reserved
        )
        mode_row = connection.execute("SELECT value_json FROM settings WHERE key='budget_mode'").fetchone()
        mode = json.loads(mode_row['value_json']) if mode_row else 'reserved'
        external_allocation = float(ticket["external_allocation"]) + float(external["external_allocation"])
        blocking_reserved = (reserved if mode == 'reserved' else 0.) + external_allocation
        return {
            "limit_usd": limit,
            "spent_usd": spent,
            "reserved_usd": blocking_reserved,
            "estimated_pending_usd": reserved,
            "externally_billed_reservation_usd": external_allocation,
            "budget_mode": mode,
            "available_usd": None if limit is None else float(limit) - spent - blocking_reserved,
            "accounting_unknown": bool(int(ticket["unknown"]) + int(external["unknown"])),
        }

    @classmethod
    def _require_budget(cls, connection: sqlite3.Connection, amount: float) -> None:
        budget = cls._budget_row(connection)
        available = budget["available_usd"]
        blocked = (amount > available + 1e-12 if budget['budget_mode'] == 'reserved'
                   else amount > 0 and available <= 1e-12) if available is not None else False
        if blocked:
            raise ResearchBudgetUnavailable(
                f"insufficient research budget: requested ${amount:.6f}, "
                f"available ${max(0.0, available):.6f}"
            )

    def create_track(self, label: str = "") -> Track:
        if not isinstance(label, str):
            raise TypeError("label must be a string")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                track_id = self._next_id(connection, "track", "T")
                connection.execute(
                    "INSERT INTO tracks VALUES (?, ?, 'active', NULL, ?, ?)",
                    (track_id, label, now, now),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_track(track_id)

    def get_track(self, track_id: str) -> Track:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown track {track_id}")
        return self._track(row)

    def tracks(self) -> list[Track]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM tracks ORDER BY created_at, id").fetchall()
        return [self._track(row) for row in rows]

    def register_base(
        self,
        commit_sha: str,
        config: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Artifact:
        if not isinstance(commit_sha, str) or not commit_sha.strip():
            raise ValueError("commit_sha must be a non-empty string")
        frozen_config = _object(config, "config")
        frozen_metadata = _object(metadata, "metadata")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                artifact_id = self._next_id(connection, "base", "P")
                connection.execute(
                    "INSERT INTO artifacts VALUES (?, ?, ?, NULL, NULL, ?, ?)",
                    (artifact_id, commit_sha, _json(frozen_config), _json(frozen_metadata), now),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.artifact(artifact_id)

    def artifact(self, artifact_id: str) -> Artifact:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown artifact {artifact_id}")
        return self._artifact(row)

    def artifacts(self) -> list[Artifact]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM artifacts ORDER BY created_at, id").fetchall()
        return [self._artifact(row) for row in rows]

    def submit(
        self,
        kind: str,
        arguments: Mapping[str, Any],
        reservation_usd: float,
        idempotency_key: str,
        track_id: str | None = None,
        label: str = "",
        followup: Mapping[str, Any] | None = None,
    ) -> Ticket:
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("kind must be a non-empty string")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string")
        if not isinstance(label, str):
            raise TypeError("label must be a string")
        frozen_arguments = _object(arguments, "arguments")
        frozen_followup = None if followup is None else _object(followup, 'followup')
        followup_reservation = 0.0
        reserve_followup_upfront = False
        if frozen_followup is not None:
            allowed_followup = {
                "kind", "arguments", "reservation_usd", "reserve_upfront"
            }
            if kind != "build" or not set(frozen_followup) <= allowed_followup or not {
                "kind", "arguments", "reservation_usd"
            } <= set(frozen_followup):
                raise ValueError("Build followup plan is invalid")
            reserve_followup_upfront = frozen_followup.pop("reserve_upfront", False)
            if type(reserve_followup_upfront) is not bool:
                raise ValueError("followup reserve_upfront must be true or false")
            if frozen_followup["kind"] not in {"measure", "admit"}:
                raise ValueError("Build followup must be measure or admit")
            if frozen_followup["kind"] == "measure" and reserve_followup_upfront:
                raise ValueError("Legacy development followups cannot reserve upfront")
            if frozen_followup["kind"] == "admit" and not reserve_followup_upfront:
                raise ValueError("Admission followups must reserve upfront")
            _object(frozen_followup['arguments'], 'followup arguments')
            followup_reservation = _money(
                frozen_followup['reservation_usd'], 'followup reservation'
            )
        reservation = _money(reservation_usd, "reservation_usd")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM tickets WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing is not None:
                    ticket = self._ticket(existing)
                    same_track = (existing['requested_track'] == track_id or
                                  existing['requested_track'] is None and track_id == ticket.track_id)
                    planned = connection.execute(
                        "SELECT plan_json, reserved_upfront FROM followups "
                        "WHERE parent_ticket=?", (ticket.id,)
                    ).fetchone()
                    same_followup = (
                        planned is None and frozen_followup is None
                        or planned is not None
                        and json.loads(planned["plan_json"]) == frozen_followup
                        and bool(planned["reserved_upfront"])
                        == reserve_followup_upfront
                    )
                    if not (
                        ticket.kind == kind
                        and ticket.arguments == frozen_arguments
                        and math.isclose(ticket.reservation_usd, reservation)
                        and same_track
                        and same_followup
                    ):
                        raise ValueError(f"idempotency key {idempotency_key!r} has another request")
                    connection.commit()
                    return ticket
                self._require_budget(
                    connection,
                    reservation
                    + (followup_reservation if reserve_followup_upfront else 0.0),
                )
                selected_track = None if track_id == 'new' else track_id
                target_artifact = frozen_arguments.get("artifact_id")
                if selected_track is None and track_id != 'new' and isinstance(target_artifact, str):
                    owner = connection.execute(
                        "SELECT track_id FROM artifacts WHERE id = ?", (target_artifact,)
                    ).fetchone()
                    if owner is None:
                        raise KeyError(f"unknown artifact {target_artifact}")
                    selected_track = owner["track_id"]
                if selected_track is None:
                    selected_track = self._next_id(connection, "track", "T")
                    connection.execute(
                        "INSERT INTO tracks VALUES (?, ?, 'active', NULL, ?, ?)",
                        (selected_track, label, now, now),
                    )
                track = connection.execute(
                    "SELECT state FROM tracks WHERE id = ?", (selected_track,)
                ).fetchone()
                if track is None:
                    raise KeyError(f"unknown track {selected_track}")
                if track["state"] != "active":
                    raise ValueError(f"track {selected_track} is dropped")
                ticket_id = self._insert_ticket(connection, kind, frozen_arguments, reservation,
                                                idempotency_key, selected_track, label, now, track_id)
                if frozen_followup is not None:
                    connection.execute(
                        "INSERT INTO followups(parent_ticket, plan_json, status, "
                        "child_ticket, error, reserved_upfront) "
                        "VALUES (?, ?, 'waiting', NULL, NULL, ?)",
                        (ticket_id, _json(frozen_followup), int(reserve_followup_upfront)),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_ticket(ticket_id)

    def activate_followups(self) -> list[Ticket]:
        """Atomically enqueue each frozen post-build plan once."""
        created = []
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute("SELECT * FROM followups WHERE status IN ('waiting','blocked')").fetchall()
            for row in rows:
                parent = self._ticket(connection.execute('SELECT * FROM tickets WHERE id=?', (row['parent_ticket'],)).fetchone())
                track = connection.execute('SELECT state FROM tracks WHERE id=?', (parent.track_id,)).fetchone()[0]
                if parent.status == 'failed' or track == 'dropped' or parent.status == 'succeeded' and parent.artifact_id is None:
                    connection.execute("UPDATE followups SET status='skipped', error=? WHERE parent_ticket=?",
                                       ('Build did not produce a candidate or the track was dropped.', parent.id))
                    continue
                if parent.status != 'succeeded':
                    continue
                plan = json.loads(row['plan_json'])
                if not row["reserved_upfront"]:
                    try:
                        self._require_budget(connection, plan['reservation_usd'])
                    except ValueError as error:
                        connection.execute("UPDATE followups SET status='blocked', error=? WHERE parent_ticket=?", (str(error), parent.id))
                        continue
                artifact = self._artifact(connection.execute('SELECT * FROM artifacts WHERE id=?', (parent.artifact_id,)).fetchone())
                candidate = {key: value for key, value in artifact.to_dict().items() if key != 'created_at'}
                if plan["kind"] == "measure":
                    args = {
                        **plan["arguments"],
                        "candidates": [{**candidate, "artifact_id": artifact.id}],
                        "artifact_id": artifact.id,
                    }
                    label = "A evaluation after " + parent.id
                else:
                    args = {
                        **plan["arguments"],
                        "candidate": {**candidate, "artifact_id": artifact.id},
                        "artifact_id": artifact.id,
                    }
                    label = "B admission after " + parent.id
                child = self._insert_ticket(
                    connection, plan["kind"], args, plan["reservation_usd"],
                    "after-build:" + parent.id, parent.track_id, label,
                    time.time(), parent.track_id,
                )
                connection.execute("UPDATE followups SET status='enqueued', child_ticket=?, error=NULL WHERE parent_ticket=?", (child, parent.id))
                created.append(child)
        return [self.get_ticket(identity) for identity in created]

    def held_build_notifications(self) -> set[str]:
        """Return successful builds still waiting for their automatic B result."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT f.parent_ticket
                FROM followups f
                JOIN tickets parent ON parent.id = f.parent_ticket
                LEFT JOIN tickets child ON child.id = f.child_ticket
                WHERE f.reserved_upfront = 1
                  AND json_extract(f.plan_json, '$.kind') = 'admit'
                  AND parent.status = 'succeeded'
                  AND (
                    f.status = 'waiting'
                    OR (f.status = 'enqueued' AND child.status IN ('queued', 'running'))
                  )
                """
            ).fetchall()
        return {str(row["parent_ticket"]) for row in rows}

    def followups(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute('''SELECT f.*, t.status AS child_status FROM followups f
                LEFT JOIN tickets t ON t.id=f.child_ticket ORDER BY f.rowid''').fetchall()
        result = []
        for row in rows:
            plan = json.loads(row["plan_json"])
            result.append({
                "build_ticket": row["parent_ticket"],
                "ticket": row["child_ticket"],
                "kind": plan["kind"],
                "status": row["child_status"] or row["status"],
                "error": row["error"],
                "estimated_cost_usd": plan["reservation_usd"],
            })
        return result

    def get_ticket(self, ticket_id: str) -> Ticket:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown ticket {ticket_id}")
        return self._ticket(row)

    def ticket_by_idempotency(self, key: str) -> Ticket | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tickets WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return None if row is None else self._ticket(row)

    def tickets(self, status: str | None = None, track_id: str | None = None) -> list[Ticket]:
        clauses: list[str] = []
        values: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            values.append(status)
        if track_id is not None:
            clauses.append("track_id = ?")
            values.append(track_id)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tickets" + where + " ORDER BY created_at, id", values
            ).fetchall()
        return [self._ticket(row) for row in rows]

    def claim(self, ticket_id: str) -> Ticket | None:
        """Atomically claim a queued ticket for one executor."""
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                changed = connection.execute(
                    """
                    UPDATE tickets SET status = 'running', started_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (now, ticket_id),
                ).rowcount
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_ticket(ticket_id) if changed else None

    def complete(self, ticket_id: str, result: Mapping[str, Any], cost_usd: float | None) -> Ticket:
        frozen_result = _object(result, "result")
        actual_cost = _completion_cost(cost_usd, frozen_result)
        artifact_spec = frozen_result.pop("artifact", None)
        if artifact_spec is not None and not isinstance(artifact_spec, dict):
            raise TypeError("result.artifact must be an object")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown ticket {ticket_id}")
                current = self._ticket(row)
                if actual_cost is None and current.kind not in {"build", "experiment"}:
                    raise ValueError("external accounting is only supported for worker tickets")
                if current.status == "succeeded":
                    if artifact_spec is not None:
                        if current.artifact_id is None:
                            raise ValueError(f"ticket {ticket_id} already completed without an artifact")
                        stored_artifact = connection.execute(
                            "SELECT * FROM artifacts WHERE id = ?", (current.artifact_id,)
                        ).fetchone()
                        requested_artifact = {
                            "commit_sha": artifact_spec.get("commit_sha"),
                            "config": _object(artifact_spec.get("config"), "result.artifact.config"),
                            "base_id": artifact_spec.get("base_id"),
                            "metadata": _object(
                                artifact_spec.get("metadata"), "result.artifact.metadata"
                            ),
                        }
                        persisted_artifact = self._artifact(stored_artifact)
                        if requested_artifact != {
                            "commit_sha": persisted_artifact.commit_sha,
                            "config": persisted_artifact.config,
                            "base_id": persisted_artifact.base_id,
                            "metadata": persisted_artifact.metadata,
                        }:
                            raise ValueError(f"ticket {ticket_id} already has another artifact")
                    expected = dict(frozen_result)
                    if current.artifact_id is not None:
                        expected["artifact_id"] = current.artifact_id
                    if current.result != expected or not _same_cost(current.cost_usd, actual_cost):
                        raise ValueError(f"ticket {ticket_id} already has another completion")
                    connection.commit()
                    return current
                if current.status not in {"running", "accounting_unknown"}:
                    raise ValueError(f"ticket {ticket_id} cannot complete from {current.status}")
                artifact_id = None
                if artifact_spec is not None:
                    commit_sha = artifact_spec.get("commit_sha")
                    if not isinstance(commit_sha, str) or not commit_sha.strip():
                        raise ValueError("result.artifact.commit_sha must be a non-empty string")
                    config = _object(artifact_spec.get("config"), "result.artifact.config")
                    metadata = _object(artifact_spec.get("metadata"), "result.artifact.metadata")
                    base_id = artifact_spec.get("base_id")
                    if base_id is not None:
                        exists = connection.execute(
                            "SELECT 1 FROM artifacts WHERE id = ?", (base_id,)
                        ).fetchone()
                        if exists is None:
                            raise KeyError(f"unknown base artifact {base_id}")
                    artifact_id = self._next_id(connection, "candidate", "C")
                    connection.execute(
                        "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (artifact_id, commit_sha, _json(config), base_id, current.track_id,
                         _json(metadata), now),
                    )
                    frozen_result["artifact_id"] = artifact_id
                connection.execute(
                    """
                    UPDATE tickets SET status = 'succeeded', cost_usd = ?, result_json = ?,
                      artifact_id = ?, finished_at = ?, error = NULL WHERE id = ?
                    """,
                    (actual_cost, _json(frozen_result), artifact_id, now, ticket_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_ticket(ticket_id)

    def mark_failed(
        self,
        ticket_id: str,
        error: str,
        cost_usd: float | None = 0.0,
        result: Mapping[str, Any] | None = None,
    ) -> Ticket:
        frozen_result = _object(result, "result")
        actual_cost = _completion_cost(cost_usd, frozen_result)
        if not isinstance(error, str) or not error:
            raise ValueError("error must be a non-empty string")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown ticket {ticket_id}")
                ticket = self._ticket(row)
                if actual_cost is None and ticket.kind not in {"build", "experiment"}:
                    raise ValueError("external accounting is only supported for worker tickets")
                if ticket.status == "failed":
                    if (ticket.error != error or ticket.result != frozen_result
                            or not _same_cost(ticket.cost_usd, actual_cost)):
                        raise ValueError(f"ticket {ticket_id} already has another failure")
                    connection.commit()
                    return ticket
                if ticket.status not in {"queued", "running", "accounting_unknown"}:
                    raise ValueError(f"ticket {ticket_id} cannot fail from {ticket.status}")
                connection.execute(
                    """
                    UPDATE tickets SET status = 'failed', cost_usd = ?, result_json = ?,
                      finished_at = ?, error = ?
                    WHERE id = ?
                    """,
                    (actual_cost, _json(frozen_result), now, error, ticket_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_ticket(ticket_id)

    def mark_accounting_unknown(self, ticket_id: str, error: str = "") -> Ticket:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown ticket {ticket_id}")
                if row["status"] not in {"queued", "running", "accounting_unknown"}:
                    raise ValueError(f"ticket {ticket_id} cannot become accounting_unknown")
                connection.execute(
                    """
                    UPDATE tickets SET status = 'accounting_unknown', error = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (error or None, now, ticket_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_ticket(ticket_id)

    def note(self, track_id: str, text: str, *, context=None, idempotency_key=None) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("note text must be non-empty")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                encoded = _json(context or {})
                if idempotency_key is not None:
                    prior = connection.execute('SELECT * FROM notes WHERE idempotency_key=?', (idempotency_key,)).fetchone()
                    if prior is not None:
                        if (prior['text'] != text or prior['context_json'] != encoded
                                or track_id not in ('new', prior['track_id'])):
                            raise ValueError('Research note replay changed its content or track')
                        connection.commit()
                        return {**dict(prior), 'context': json.loads(prior['context_json'])}
                if track_id == 'new':
                    track_id = self._next_id(connection, 'track', 'T')
                    connection.execute("INSERT INTO tracks VALUES (?, ?, 'active', NULL, ?, ?)",
                                       (track_id, text.strip().splitlines()[0][:160], now, now))
                track = connection.execute("SELECT 1 FROM tracks WHERE id = ?", (track_id,)).fetchone()
                if track is None:
                    raise KeyError(f"unknown track {track_id}")
                cursor = connection.execute(
                    "INSERT INTO notes(track_id, text, created_at, context_json, idempotency_key) VALUES (?, ?, ?, ?, ?)",
                    (track_id, text, now, encoded, idempotency_key),
                )
                note_id = int(cursor.lastrowid)
                connection.execute("UPDATE tracks SET updated_at = ? WHERE id = ?", (now, track_id))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {"id": note_id, "track_id": track_id, "text": text, "created_at": now, 'context': context or {}}

    def research_lines(self):
        """One latest judgment per track, including tracks older than the board window."""
        from .context import research_lines
        with self._connect() as connection:
            tracks = [dict(r) for r in connection.execute('SELECT * FROM tracks ORDER BY created_at,id')]
            notes = [dict(r) for r in connection.execute('SELECT * FROM notes WHERE id IN (SELECT max(id) FROM notes GROUP BY track_id)')]
            tickets = [dict(r) for r in connection.execute("SELECT * FROM tickets WHERE kind IN ('experiment','build') ORDER BY created_at,id")]
        return research_lines(tracks, notes, tickets)

    def drop(self, track_id: str, reason: str) -> Track:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("drop reason must be non-empty")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT state, reason FROM tracks WHERE id = ?", (track_id,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown track {track_id}")
                if row["state"] == "dropped" and row["reason"] != reason:
                    raise ValueError(f"track {track_id} was already dropped for another reason")
                connection.execute(
                    "UPDATE tracks SET state = 'dropped', reason = ?, updated_at = ? WHERE id = ?",
                    (reason, now, track_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_track(track_id)

    def checkpoint(self, key: str, value: Any) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("checkpoint key must be non-empty")
        frozen = json.loads(_json(value))
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO checkpoints(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
                                               updated_at = excluded.updated_at
                """,
                (key, _json(frozen), now),
            )

    def load_checkpoint(self, key: str, default: Any = None) -> Any:
        with self._connect() as connection:
            row = connection.execute("SELECT value_json FROM checkpoints WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row["value_json"])

    def reserve_external(self, key: str, reservation_usd: float, label: str = "") -> dict[str, Any]:
        reservation = _money(reservation_usd, "reservation_usd")
        if not isinstance(key, str) or not key:
            raise ValueError("external cost key must be non-empty")
        if not isinstance(label, str):
            raise TypeError("label must be a string")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM external_costs WHERE key = ?", (key,)).fetchone()
                if row is None:
                    self._require_budget(connection, reservation)
                    connection.execute(
                        "INSERT INTO external_costs VALUES (?, ?, 'reserved', ?, NULL, NULL, ?, ?)",
                        (key, label, reservation, now, now),
                    )
                elif not (row["label"] == label and math.isclose(float(row["reservation_usd"]), reservation)):
                    raise ValueError(f"external cost key {key!r} has another reservation")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self._external_cost(key)

    def settle_external(self, key: str, cost_usd: float) -> dict[str, Any]:
        cost = _money(cost_usd, "cost_usd")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM external_costs WHERE key = ?", (key,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown external cost {key}")
                if connection.execute("SELECT 1 FROM external_billing WHERE key = ?", (key,)).fetchone():
                    raise ValueError("externally billed CLI calls cannot be assigned an API price")
                if row["state"] == "settled" and not math.isclose(float(row["cost_usd"]), cost):
                    raise ValueError(f"external cost {key!r} already has another settlement")
                connection.execute(
                    "UPDATE external_costs SET state = 'settled', cost_usd = ?, error = NULL, updated_at = ? WHERE key = ?",
                    (cost, now, key),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self._external_cost(key)

    def mark_external_billed(self, key: str) -> dict[str, Any]:
        """Retain a CLI call's allowance without inventing an invoice or pending lookup."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT state FROM external_costs WHERE key = ?", (key,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown external cost {key}")
                if row["state"] == "settled":
                    raise ValueError("settled API calls cannot become externally billed")
                connection.execute("INSERT OR IGNORE INTO external_billing VALUES (?)", (key,))
                connection.execute(
                    "UPDATE external_costs SET state = 'reserved', error = NULL, updated_at = ? WHERE key = ?",
                    (time.time(), key),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self._external_cost(key)

    def mark_external_unknown(self, key: str, error: str = "") -> dict[str, Any]:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT state FROM external_costs WHERE key = ?", (key,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown external cost {key}")
                if row["state"] == "settled":
                    raise ValueError(f"external cost {key!r} is already settled")
                if connection.execute("SELECT 1 FROM external_billing WHERE key = ?", (key,)).fetchone():
                    raise ValueError(f"external cost {key!r} is already externally billed")
                connection.execute(
                    "UPDATE external_costs SET state = 'accounting_unknown', error = ?, updated_at = ? WHERE key = ?",
                    (error or None, now, key),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self._external_cost(key)

    def admissions(self) -> list[dict[str, Any]]:
        """Return actual admission outcomes bound to their frozen ticket artifacts."""
        rows: list[dict[str, Any]] = []
        for ticket in self.tickets():
            if ticket.kind != "admit" or ticket.status != "succeeded" or ticket.result is None:
                continue
            candidate = ticket.arguments.get("candidate")
            control = ticket.arguments.get("control")
            candidate_id = (
                candidate.get("artifact_id") if isinstance(candidate, Mapping)
                else ticket.arguments.get("artifact_id")
            )
            control_id = control.get("artifact_id") if isinstance(control, Mapping) else None
            if not isinstance(candidate_id, str) or not isinstance(control_id, str):
                continue
            rows.append({
                "ticket_id": ticket.id,
                "track_id": ticket.track_id,
                "artifact_id": candidate_id,
                "control_id": control_id,
                "fate": ticket.result.get("admission_fate", ticket.result.get("admission")),
                "accepted": (
                    ticket.result.get("accepted") is True
                    and ticket.result.get("evidence_tier") == "adaptive_b"
                    and ticket.result.get(
                        "admission_fate", ticket.result.get("admission")
                    ) == "admitted"
                ),
                "cost_usd": ticket.cost_usd,
                "finished_at": ticket.finished_at,
            })
        return rows

    def accepted_artifacts(self) -> list[Artifact]:
        """Resolve candidates accepted by completed configured admission tickets."""
        accepted: list[Artifact] = []
        seen: set[str] = set()
        for row in self.admissions():
            artifact_id = row["artifact_id"]
            if not row["accepted"] or artifact_id in seen:
                continue
            accepted.append(self.artifact(artifact_id))
            seen.add(artifact_id)
        return accepted

    def _external_cost(self, key: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM external_costs WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return dict(row)

    def board(self) -> dict[str, Any]:
        with self._connect() as connection:
            budget = self._budget_row(connection)
            tracks = connection.execute("SELECT * FROM tracks ORDER BY created_at, id").fetchall()
            tickets = connection.execute("SELECT * FROM tickets ORDER BY created_at, id").fetchall()
            artifacts = connection.execute(
                "SELECT id, commit_sha, base_id, track_id FROM artifacts ORDER BY created_at, id"
            ).fetchall()
            notes = connection.execute(
                "SELECT id, track_id, text, created_at FROM notes ORDER BY id DESC LIMIT 20"
            ).fetchall()
        return {
            "budget": budget,
            "tracks": [self._track(row).to_dict() for row in tracks],
            "tickets": [
                {
                    "id": row["id"], "track_id": row["track_id"], "kind": row["kind"],
                    "label": row["label"], "status": row["status"],
                    "reservation_usd": float(row["reservation_usd"]),
                    "cost_usd": None if row["cost_usd"] is None else float(row["cost_usd"]),
                    "artifact_id": row["artifact_id"], "error": row["error"],
                }
                for row in tickets
            ],
            "artifacts": [dict(row) for row in artifacts],
            "accepted": self.admissions(),
            "followups": self.followups(),
            "notes": [dict(row) for row in reversed(notes)],
        }
