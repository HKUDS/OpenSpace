"""Durable SQLite implementation of the persistence ports.

SQLite is used as a local transactional envelope around the same validated
repositories as the in-memory reference implementation.  A unit of work takes
an immediate database write lock, restores a lossless domain snapshot, applies
the repository invariants, and commits the new snapshot atomically.  This is a
deliberately simple single-host backend; it favours replay correctness over
write throughput and does not contain policy-carrier algorithms.

The codec is an explicit, allow-listed JSON codec rather than ``pickle``.  A
database file is therefore data, not executable Python, and schema drift fails
closed instead of silently constructing unknown objects.
"""

from __future__ import annotations

import base64
import json
import math
import sqlite3
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from agentpilot.domain import (
    ActionExecutionAttestation,
    ActionExecutionFence,
    ActionExecutionMode,
    AdapterTraceCapability,
    ArmAllocation,
    ArmSlot,
    ChildProgramReadout,
    CompatibilityKey,
    CostLedgerEntry,
    DecisionTrace,
    DirectionCandidate,
    DirectionMatch,
    DirectionMatchKind,
    DirectionSelection,
    DirectionSlate,
    EffectivenessLabel,
    EffectivenessObservation,
    EvaluationValidity,
    EvidenceCutoff,
    EvidenceTier,
    FeasibilityObservation,
    FeasibilityStage,
    ImplementationAdherence,
    OutcomeAttribution,
    PairedOutcome,
    ParentCandidate,
    ParentRepositoryMap,
    ParentSelection,
    ParentSlate,
    PendingReservation,
    PlanningFailureEvent,
    PlanningSlotAttempt,
    PolicyAction,
    PolicyDecisionPlan,
    PolicyMethod,
    PolicyObservation,
    ProviderCallDispatch,
    ProviderCallIntent,
    ProviderCallRefusal,
    ProviderCallResult,
    ProviderCallStatus,
    RepositoryFileMap,
    RepositorySymbol,
    ReadResult,
    RealizedFileChange,
    RealizedFileChangeKind,
    ReservationState,
    SearchState,
    SnapshotTraceBinding,
    ScopeKind,
    ScopeTarget,
    SlotAdherence,
    SlotAdherenceStatus,
    SlotKind,
    StateDescriptor,
    StructuredDirection,
    NewPath,
    NormalizedTrace,
    TraceArtifactRef,
    TraceCaptureCoordinates,
    TraceCaptureRecord,
    TraceCaptureResult,
    TraceCaptureStatus,
    TraceCaseIndex,
    TraceEvidenceManifest,
    TraceEvent,
    TraceEventKind,
    TraceSpanRef,
    VisibleATaskEvidence,
    WorkerExecutionStatus,
    WorkerResult,
    stable_id,
)
from agentpilot.evaluation.frozen_plan import (
    FrozenActionEvaluationPlan,
    FrozenInformationPriorSnapshot,
)
from agentpilot.evaluation.paired import EvaluationCell, FrozenPairedPlan
from agentpilot.evaluation.sampling import SamplingDraw, SamplingUnit
from agentpilot.evaluation.sealed.contracts import (
    SealedAuthorization,
    SealedCostSlot,
    SealedEvaluationClaim,
    SealedEvaluationReceipt,
    SealedEvaluationReservation,
    SealedManifest,
    SealedRecord,
    SealedReport,
    SealedSlotSettlement,
    SealedSlotSide,
    SealedSlotState,
    SealedState,
)
from agentpilot.persistence.leases import (
    RunBatchLease,
    RunLeaseLost,
    RunLeaseUnavailable,
)
from agentpilot.persistence.memory import (
    InMemoryDatabase,
    InMemoryUnitOfWork,
    PersistenceConflict,
)
from agentpilot.persistence.protocols import (
    ArchiveTransition,
    BatchRecord,
    BatchStatus,
    LineageEdge,
)
from agentpilot.search.direction_history import DirectionHistoryRecord
from agentpilot.search.direction_semantic_index import (
    DirectionApplicability,
    EmbeddingIdentity,
    PendingSemanticReservation,
)
from agentpilot.search.evidence_retrieval import ProgramEvidence
from agentpilot.search.parent_candidates import ParentRecord
from agentpilot.search.read_model import (
    FrozenSearchInputRecord,
    SearchProgramRecord,
)
from agentpilot.search.state import SearchFact

PERSISTENCE_CONTRACT = "trace-bounded-direction-negative-history"
_PERSISTENCE_CONTRACT_KEY = "persistence_contract"


class PersistenceContractMismatch(RuntimeError):
    """A database belongs to another exact persistence contract."""


def _assert_persistence_contract(connection: sqlite3.Connection) -> None:
    try:
        row = connection.execute(
            "SELECT value FROM persistence_metadata WHERE key = ?",
            (_PERSISTENCE_CONTRACT_KEY,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise PersistenceContractMismatch(
            "SQLite database has no persistence contract metadata"
        ) from exc
    if row is None or row[0] != PERSISTENCE_CONTRACT:
        observed = None if row is None else row[0]
        raise PersistenceContractMismatch(
            "SQLite persistence contract differs from this binary "
            f"({observed!r} != {PERSISTENCE_CONTRACT!r})"
        )


def _initialize_persistence_contract(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS persistence_metadata ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    row = connection.execute(
        "SELECT value FROM persistence_metadata WHERE key = ?",
        (_PERSISTENCE_CONTRACT_KEY,),
    ).fetchone()
    if row is not None:
        _assert_persistence_contract(connection)
        return
    record_count = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    sealed_count = connection.execute(
        "SELECT COUNT(*) FROM sealed_records"
    ).fetchone()[0]
    if record_count or sealed_count:
        raise PersistenceContractMismatch(
            "refusing to attach the new persistence contract to a non-empty "
            "database written under another persistence contract"
        )
    connection.execute(
        "INSERT INTO persistence_metadata(key, value) VALUES (?, ?)",
        (_PERSISTENCE_CONTRACT_KEY, PERSISTENCE_CONTRACT),
    )


_DATABASE_BUCKETS = (
    "states",
    "batches",
    "actions",
    "plans",
    "evaluation_priors",
    "evaluation_plans",
    "planning_failures",
    "semantic_reservations",
    "workers",
    "outcomes",
    "effectiveness",
    "observations",
    "feasibility",
    "observation_batches",
    "archive_transitions",
    "lineage_edges",
    "search_programs",
    "search_input_snapshots",
    "trace_captures",
    "trace_manifests",
    "snapshot_trace_bindings",
    "provider_call_intents",
    "provider_call_dispatches",
    "provider_call_refusals",
    "provider_call_results",
    "cost_entries",
)

_DATACLASS_TYPES = (
    ActionExecutionAttestation,
    ActionExecutionFence,
    AdapterTraceCapability,
    ArmAllocation,
    ArmSlot,
    ArchiveTransition,
    BatchRecord,
    ChildProgramReadout,
    CompatibilityKey,
    CostLedgerEntry,
    DecisionTrace,
    DirectionApplicability,
    DirectionHistoryRecord,
    DirectionCandidate,
    DirectionMatch,
    DirectionSelection,
    DirectionSlate,
    EffectivenessObservation,
    FeasibilityObservation,
    EmbeddingIdentity,
    EvidenceCutoff,
    NewPath,
    NormalizedTrace,
    LineageEdge,
    VisibleATaskEvidence,
    PairedOutcome,
    ParentCandidate,
    ParentRepositoryMap,
    ParentSelection,
    ParentSlate,
    PendingReservation,
    PendingSemanticReservation,
    PlanningFailureEvent,
    PlanningSlotAttempt,
    PolicyAction,
    PolicyDecisionPlan,
    PolicyObservation,
    ProviderCallDispatch,
    ProviderCallIntent,
    ProviderCallRefusal,
    ProviderCallResult,
    SearchState,
    SearchFact,
    SnapshotTraceBinding,
    SealedAuthorization,
    SealedCostSlot,
    SealedEvaluationClaim,
    SealedEvaluationReservation,
    SealedEvaluationReceipt,
    SealedManifest,
    SealedRecord,
    SealedReport,
    SealedSlotSettlement,
    ParentRecord,
    ProgramEvidence,
    RepositoryFileMap,
    RepositorySymbol,
    ReadResult,
    RealizedFileChange,
    SearchProgramRecord,
    FrozenSearchInputRecord,
    FrozenActionEvaluationPlan,
    FrozenInformationPriorSnapshot,
    FrozenPairedPlan,
    EvaluationCell,
    SamplingDraw,
    SamplingUnit,
    SlotAdherence,
    ScopeTarget,
    StateDescriptor,
    StructuredDirection,
    TraceArtifactRef,
    TraceCaptureCoordinates,
    TraceCaptureRecord,
    TraceCaptureResult,
    TraceCaseIndex,
    TraceEvidenceManifest,
    TraceEvent,
    TraceSpanRef,
    WorkerResult,
)

_ENUM_TYPES = (
    ActionExecutionMode,
    BatchStatus,
    DirectionMatchKind,
    EffectivenessLabel,
    EvidenceTier,
    FeasibilityStage,
    EvaluationValidity,
    ImplementationAdherence,
    OutcomeAttribution,
    PolicyMethod,
    ProviderCallStatus,
    ReservationState,
    RealizedFileChangeKind,
    ScopeKind,
    SealedState,
    SealedSlotSide,
    SealedSlotState,
    SlotAdherenceStatus,
    SlotKind,
    TraceCaptureStatus,
    TraceEventKind,
    WorkerExecutionStatus,
)


def _type_id(value: type) -> str:
    # Keep the persisted type identity stable across the public package rename.
    # Immutable records compare their encoded JSON, so read aliases alone are
    # insufficient: re-encoding an old record must preserve the same bytes.
    module = value.__module__
    if module == "agentpilot" or module.startswith("agentpilot."):
        module = "openthink" + module[len("agentpilot"):]
    return f"{module}:{value.__qualname__}"


_DATACLASS_REGISTRY = {_type_id(value): value for value in _DATACLASS_TYPES}
_ENUM_REGISTRY = {_type_id(value): value for value in _ENUM_TYPES}
_DATACLASS_REGISTRY.update(
    {f"{value.__module__}:{value.__qualname__}": value for value in _DATACLASS_TYPES}
)
_ENUM_REGISTRY.update(
    {f"{value.__module__}:{value.__qualname__}": value for value in _ENUM_TYPES}
)

_BUCKET_VALUE_TYPES: dict[str, type] = {
    "states": SearchState,
    "batches": BatchRecord,
    "actions": PolicyAction,
    "plans": PolicyDecisionPlan,
    "evaluation_priors": FrozenInformationPriorSnapshot,
    "evaluation_plans": FrozenActionEvaluationPlan,
    "planning_failures": PlanningFailureEvent,
    "semantic_reservations": PendingSemanticReservation,
    "workers": WorkerResult,
    "outcomes": PairedOutcome,
    "effectiveness": EffectivenessObservation,
    "observations": PolicyObservation,
    "feasibility": FeasibilityObservation,
    "observation_batches": str,
    "archive_transitions": ArchiveTransition,
    "lineage_edges": LineageEdge,
    "search_programs": SearchProgramRecord,
    "search_input_snapshots": FrozenSearchInputRecord,
    "trace_captures": TraceCaptureResult,
    "trace_manifests": TraceEvidenceManifest,
    "snapshot_trace_bindings": SnapshotTraceBinding,
    "provider_call_intents": ProviderCallIntent,
    "provider_call_dispatches": ProviderCallDispatch,
    "provider_call_refusals": ProviderCallRefusal,
    "provider_call_results": ProviderCallResult,
    "cost_entries": CostLedgerEntry,
}


def _terminal_authorization_matches(
    database: InMemoryDatabase,
    authorization: SealedAuthorization,
) -> bool:
    from agentpilot.persistence.terminal_archive import (
        derive_terminal_archive_coordinates,
    )

    batches = tuple(
        batch
        for batch in database.batches.values()
        if batch.run_id == authorization.run_id
    )
    programs = tuple(
        record
        for record in database.search_programs.values()
        if record.run_id == authorization.run_id
    )
    try:
        coordinates = derive_terminal_archive_coordinates(
            run_id=authorization.run_id,
            batches=batches,
            programs=programs,
            as_of=authorization.authorized_at,
        )
    except ValueError:
        return False
    return (
        coordinates.root_program_id == authorization.root_program_id
        and coordinates.champion_program_id == authorization.champion_program_id
        and coordinates.archive_cutoff == authorization.archive_cutoff
    )


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        kind = _type_id(type(value))
        if kind not in _ENUM_REGISTRY:
            raise TypeError(f"unsupported enum type: {kind}")
        return {"$": "enum", "type": kind, "value": value.value}
    if is_dataclass(value) and not isinstance(value, type):
        kind = _type_id(type(value))
        if kind not in _DATACLASS_REGISTRY:
            raise TypeError(f"unsupported dataclass type: {kind}")
        return {
            "$": "dataclass",
            "type": kind,
            "fields": [
                [field.name, _encode(getattr(value, field.name))]
                for field in fields(value)
            ],
        }
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TypeError("cannot persist a timezone-naive datetime")
        return {"$": "datetime", "value": value.isoformat()}
    if isinstance(value, Mapping):
        return {
            "$": "mapping",
            "items": [[_encode(key), _encode(item)] for key, item in value.items()],
        }
    if isinstance(value, tuple):
        return {"$": "tuple", "items": [_encode(item) for item in value]}
    if isinstance(value, frozenset):
        encoded = [_encode(item) for item in value]
        encoded.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return {"$": "frozenset", "items": encoded}
    if isinstance(value, bytes):
        return {
            "$": "bytes",
            "value": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            name = "nan"
        elif value > 0:
            name = "inf"
        else:
            name = "-inf"
        return {"$": "float", "value": name}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported durable value: {type(value).__name__}")


def _decode(value: Any) -> Any:
    if not isinstance(value, dict):
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        raise ValueError("invalid SQLite codec scalar")
    tag = value.get("$")
    if tag == "enum":
        enum_type = _ENUM_REGISTRY.get(value.get("type"))
        if enum_type is None:
            raise ValueError("unknown persisted enum type")
        return enum_type(value["value"])
    if tag == "dataclass":
        dataclass_type = _DATACLASS_REGISTRY.get(value.get("type"))
        if dataclass_type is None:
            raise ValueError("unknown persisted dataclass type")
        raw_fields = value.get("fields")
        if not isinstance(raw_fields, list):
            raise ValueError("invalid persisted dataclass fields")
        decoded_fields: dict[str, Any] = {}
        for pair in raw_fields:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or pair[0] in decoded_fields
            ):
                raise ValueError("invalid persisted dataclass field")
            decoded_fields[pair[0]] = _decode(pair[1])
        expected = {field.name for field in fields(dataclass_type)}
        if set(decoded_fields) - expected:
            raise ValueError("persisted dataclass fields differ from schema")
        missing = expected - set(decoded_fields)
        if missing:
            raise ValueError("persisted dataclass fields differ from schema")
        return dataclass_type(**decoded_fields)
    if tag == "datetime":
        timestamp = datetime.fromisoformat(value["value"])
        if timestamp.tzinfo is None:
            raise ValueError("persisted datetime is timezone-naive")
        return timestamp
    if tag == "mapping":
        pairs = value.get("items")
        if not isinstance(pairs, list):
            raise ValueError("invalid persisted mapping")
        result: dict[Any, Any] = {}
        for pair in pairs:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("invalid persisted mapping item")
            key = _decode(pair[0])
            if key in result:
                raise ValueError("persisted mapping contains duplicate keys")
            result[key] = _decode(pair[1])
        return result
    if tag == "tuple":
        return tuple(_decode(item) for item in value["items"])
    if tag == "frozenset":
        return frozenset(_decode(item) for item in value["items"])
    if tag == "bytes":
        return base64.b64decode(value["value"], validate=True)
    if tag == "float":
        non_finite = {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}
        return non_finite[value["value"]]
    raise ValueError("unknown persisted SQLite codec tag")


def _dumps(value: Any) -> str:
    return json.dumps(
        _encode(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _loads(payload: str) -> Any:
    return _decode(json.loads(payload))


class SQLiteUnitOfWork:
    """One serializable transaction over a durable SQLite database file."""

    def __init__(
        self,
        database_path: str,
        *,
        timeout: float = 30.0,
        lease: RunBatchLease | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.database_path = database_path
        self.timeout = timeout
        self.lease = lease
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._connection: sqlite3.Connection | None = None
        self._delegate: InMemoryUnitOfWork | None = None

    def __enter__(self) -> "SQLiteUnitOfWork":
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.timeout,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            _assert_persistence_contract(connection)
            transaction_now = self.clock()
            if self.lease is not None:
                _assert_lease_owned(connection, self.lease, now=transaction_now)
            database = self._load_database(connection)
            lease = self.lease
            delegate = InMemoryUnitOfWork(
                database,
                sealed_authorization_guard=lambda authorization: (
                    lease is not None
                    and lease.run_id == authorization.run_id
                    and lease.batch_id
                    == stable_id("sealed-finalization-lease", authorization.run_id)
                    and authorization.authorized_at == transaction_now
                    and _terminal_authorization_matches(database, authorization)
                ),
            )
            delegate.__enter__()
        except BaseException:
            connection.rollback()
            connection.close()
            raise
        self._connection = connection
        self._delegate = delegate
        self.states = delegate.states
        self.batches = delegate.batches
        self.actions = delegate.actions
        self.plans = delegate.plans
        self.evaluation_priors = delegate.evaluation_priors
        self.evaluation_plans = delegate.evaluation_plans
        self.planning_failures = delegate.planning_failures
        self.semantic_reservations = delegate.semantic_reservations
        self.outcomes = delegate.outcomes
        self.observations = delegate.observations
        self.feasibility = delegate.feasibility
        self.archive = delegate.archive
        self.search_read_model = delegate.search_read_model
        self.trace_evidence = delegate.trace_evidence
        self.usage = delegate.usage
        self.sealed = delegate.sealed
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        connection = self._connection
        delegate = self._delegate
        if connection is None or delegate is None:
            raise RuntimeError("SQLite unit of work was not entered")
        try:
            delegate.__exit__(exc_type, exc, traceback)
            if exc_type is None:
                self._persist_database(connection, delegate.database)
                connection.commit()
            else:
                connection.rollback()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._connection = None
            self._delegate = None
        return False

    @staticmethod
    def _load_database(connection: sqlite3.Connection) -> InMemoryDatabase:
        values: dict[str, dict[str, Any]] = {bucket: {} for bucket in _DATABASE_BUCKETS}
        rows = connection.execute(
            "SELECT bucket, record_key, payload FROM records"
        ).fetchall()
        for bucket, record_key, payload in rows:
            if bucket not in values:
                raise RuntimeError(f"unknown SQLite persistence bucket: {bucket}")
            decoded = _loads(payload)
            expected_type = _BUCKET_VALUE_TYPES[bucket]
            if not isinstance(decoded, expected_type):
                raise RuntimeError(f"invalid record type in SQLite bucket: {bucket}")
            values[bucket][record_key] = decoded
        sealed_records: dict[str, SealedRecord] = {}
        for run_id, payload in connection.execute(
            "SELECT run_id, payload FROM sealed_records"
        ).fetchall():
            decoded = _loads(payload)
            if not isinstance(decoded, SealedRecord):
                raise RuntimeError("invalid durable sealed record")
            if decoded.manifest.run_id != run_id:
                raise RuntimeError("sealed record key differs from manifest")
            sealed_records[run_id] = decoded
        return InMemoryDatabase(**values, sealed_records=sealed_records)

    @staticmethod
    def _persist_database(
        connection: sqlite3.Connection,
        database: InMemoryDatabase,
    ) -> None:
        rows: list[tuple[str, str, str]] = []
        for bucket in _DATABASE_BUCKETS:
            table = getattr(database, bucket)
            for record_key, value in table.items():
                if not isinstance(record_key, str) or not record_key:
                    raise TypeError("durable record keys must be non-empty strings")
                if not isinstance(value, _BUCKET_VALUE_TYPES[bucket]):
                    raise TypeError(f"invalid durable record type for bucket: {bucket}")
                rows.append((bucket, record_key, _dumps(value)))
        connection.execute("DELETE FROM records")
        connection.executemany(
            "INSERT INTO records(bucket, record_key, payload) " "VALUES (?, ?, ?)",
            rows,
        )
        connection.execute("DELETE FROM sealed_records")
        connection.executemany(
            "INSERT INTO sealed_records(run_id, payload) VALUES (?, ?)",
            (
                (run_id, _dumps(record))
                for run_id, record in database.sealed_records.items()
            ),
        )


class SQLiteUnitOfWorkFactory:
    """Create independent SQLite connections for one durable database file."""

    def __init__(self, database_path: str | Path, *, timeout: float = 30.0):
        if isinstance(timeout, bool) or timeout <= 0.0:
            raise ValueError("timeout must be positive")
        path = Path(database_path)
        if not str(path).strip():
            raise ValueError("database_path must be non-empty")
        if str(path) == ":memory:":
            raise ValueError(
                "SQLiteUnitOfWorkFactory requires a file for restart " "durability"
            )
        self.database_path = str(path.resolve())
        self.timeout = float(timeout)
        self._initialize()

    def _initialize(self) -> None:
        connection = sqlite3.connect(self.database_path, timeout=self.timeout)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS records ("
                "bucket TEXT NOT NULL, record_key TEXT NOT NULL, "
                "payload TEXT NOT NULL, PRIMARY KEY(bucket, record_key))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sealed_records ("
                "run_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            _initialize_persistence_contract(connection)
            _create_lease_tables(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def __call__(self) -> SQLiteUnitOfWork:
        return SQLiteUnitOfWork(self.database_path, timeout=self.timeout)

    def lease_manager(self) -> "SQLiteRunBatchLeaseManager":
        """Return an independent-connection manager for this database."""

        return SQLiteRunBatchLeaseManager(
            self.database_path,
            timeout=self.timeout,
        )

    def sealed_repository(self):
        """Return a transactional facade over this exact database."""

        from agentpilot.persistence.sealed import TransactionalSealedRepository

        return TransactionalSealedRepository(self)

    def fenced(
        self,
        lease: RunBatchLease,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> "SQLiteFencedUnitOfWorkFactory":
        """Bind every subsequent transaction to a live exact lease claim."""

        return SQLiteFencedUnitOfWorkFactory(self, lease=lease, clock=clock)


def _read_only_uri(database_path: str) -> str:
    return f"file:{quote(database_path, safe='/')}?mode=ro"


class SQLiteReadOnlyUnitOfWork(SQLiteUnitOfWork):
    """Load one SQLite snapshot without a write lock or persistence pass."""

    def __enter__(self) -> "SQLiteReadOnlyUnitOfWork":
        connection = sqlite3.connect(
            _read_only_uri(self.database_path),
            uri=True,
            timeout=self.timeout,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        try:
            connection.execute("BEGIN")
            _assert_persistence_contract(connection)
            database = self._load_database(connection)
            delegate = InMemoryUnitOfWork(database)
            delegate.__enter__()
        except BaseException:
            connection.rollback()
            connection.close()
            raise
        self._connection = connection
        self._delegate = delegate
        self.states = delegate.states
        self.batches = delegate.batches
        self.actions = delegate.actions
        self.plans = delegate.plans
        self.evaluation_priors = delegate.evaluation_priors
        self.evaluation_plans = delegate.evaluation_plans
        self.planning_failures = delegate.planning_failures
        self.semantic_reservations = delegate.semantic_reservations
        self.outcomes = delegate.outcomes
        self.observations = delegate.observations
        self.feasibility = delegate.feasibility
        self.archive = delegate.archive
        self.search_read_model = delegate.search_read_model
        self.trace_evidence = delegate.trace_evidence
        self.usage = delegate.usage
        self.sealed = delegate.sealed
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        connection = self._connection
        delegate = self._delegate
        if connection is None or delegate is None:
            raise RuntimeError("SQLite read-only unit of work was not entered")
        try:
            delegate.__exit__(exc_type, exc, traceback)
            connection.rollback()
        finally:
            connection.close()
            self._connection = None
            self._delegate = None
        return False


class SQLiteReadOnlyUnitOfWorkFactory:
    """Open an existing SQLite database through a read-only URI."""

    def __init__(self, database_path: str | Path, *, timeout: float = 30.0):
        if isinstance(timeout, bool) or timeout <= 0.0:
            raise ValueError("timeout must be positive")
        path = Path(database_path)
        if not str(path).strip() or str(path) == ":memory:":
            raise ValueError("read-only SQLite requires a durable database file")
        self.database_path = str(path.resolve())
        self.timeout = float(timeout)
        connection = sqlite3.connect(
            _read_only_uri(self.database_path),
            uri=True,
            timeout=self.timeout,
        )
        try:
            connection.execute("PRAGMA query_only = ON")
            _assert_persistence_contract(connection)
            connection.execute("SELECT 1 FROM records LIMIT 1").fetchone()
        finally:
            connection.close()

    def __call__(self) -> SQLiteReadOnlyUnitOfWork:
        return SQLiteReadOnlyUnitOfWork(
            self.database_path,
            timeout=self.timeout,
        )


def _create_lease_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS run_lease_fences ("
        "run_id TEXT PRIMARY KEY, fencing_token INTEGER NOT NULL "
        "CHECK(fencing_token > 0))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS run_batch_leases ("
        "run_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, "
        "owner_token TEXT NOT NULL, fencing_token INTEGER NOT NULL, "
        "acquired_at TEXT NOT NULL, renewed_at TEXT NOT NULL, "
        "expires_at TEXT NOT NULL, "
        "FOREIGN KEY(run_id) REFERENCES run_lease_fences(run_id))"
    )


def _timestamp(value: datetime, *, name: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _positive_ttl(ttl: timedelta) -> None:
    if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
        raise ValueError("ttl must be a positive timedelta")


def _lease_from_row(row: tuple[Any, ...] | None) -> RunBatchLease | None:
    if row is None:
        return None
    return RunBatchLease(
        run_id=row[0],
        batch_id=row[1],
        owner_token=row[2],
        fencing_token=row[3],
        acquired_at=datetime.fromisoformat(row[4]),
        renewed_at=datetime.fromisoformat(row[5]),
        expires_at=datetime.fromisoformat(row[6]),
    )


def _read_lease(
    connection: sqlite3.Connection,
    run_id: str,
) -> RunBatchLease | None:
    return _lease_from_row(
        connection.execute(
            "SELECT run_id, batch_id, owner_token, fencing_token, "
            "acquired_at, renewed_at, expires_at "
            "FROM run_batch_leases WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    )


def _assert_lease_owned(
    connection: sqlite3.Connection,
    lease: RunBatchLease,
    *,
    now: datetime,
) -> RunBatchLease:
    _timestamp(now, name="now")
    current = _read_lease(connection, lease.run_id)
    if current is None or not current.same_claim(lease):
        raise RunLeaseLost("run lease was released or superseded")
    if not current.is_live_at(now):
        raise RunLeaseLost("run lease expired")
    return current


class SQLiteRunBatchLeaseManager:
    """Atomic, process-safe per-run active-batch leases for SQLite."""

    def __init__(self, database_path: str | Path, *, timeout: float = 30.0):
        factory = SQLiteUnitOfWorkFactory(database_path, timeout=timeout)
        self.database_path = factory.database_path
        self.timeout = factory.timeout

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.timeout,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def acquire(
        self,
        *,
        run_id: str,
        batch_id: str,
        owner_token: str,
        now: datetime,
        ttl: timedelta,
    ) -> RunBatchLease:
        for name, value in (
            ("run_id", run_id),
            ("batch_id", batch_id),
            ("owner_token", owner_token),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        _timestamp(now, name="now")
        _positive_ttl(ttl)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            sealed_row = connection.execute(
                "SELECT payload FROM sealed_records WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if sealed_row is not None:
                sealed = _loads(sealed_row[0])
                if not isinstance(sealed, SealedRecord):
                    raise RuntimeError("invalid durable sealed record")
                if sealed.state is not SealedState.SEALED:
                    raise RunLeaseUnavailable(
                        f"run {run_id!r} is finalized for sealed evaluation"
                    )
            current = _read_lease(connection, run_id)
            if current is not None and current.is_live_at(now):
                if current.batch_id == batch_id and current.owner_token == owner_token:
                    connection.commit()
                    return current
                raise RunLeaseUnavailable(
                    f"run {run_id!r} already has a live batch driver"
                )
            fence_row = connection.execute(
                "SELECT fencing_token FROM run_lease_fences WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if fence_row is None:
                fencing_token = 1
                connection.execute(
                    "INSERT INTO run_lease_fences(run_id, fencing_token) "
                    "VALUES (?, ?)",
                    (run_id, fencing_token),
                )
            else:
                fencing_token = int(fence_row[0]) + 1
                connection.execute(
                    "UPDATE run_lease_fences SET fencing_token = ? " "WHERE run_id = ?",
                    (fencing_token, run_id),
                )
            lease = RunBatchLease(
                run_id=run_id,
                batch_id=batch_id,
                owner_token=owner_token,
                fencing_token=fencing_token,
                acquired_at=now,
                renewed_at=now,
                expires_at=now + ttl,
            )
            connection.execute(
                "INSERT INTO run_batch_leases("
                "run_id, batch_id, owner_token, fencing_token, acquired_at, "
                "renewed_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "batch_id = excluded.batch_id, "
                "owner_token = excluded.owner_token, "
                "fencing_token = excluded.fencing_token, "
                "acquired_at = excluded.acquired_at, "
                "renewed_at = excluded.renewed_at, "
                "expires_at = excluded.expires_at",
                (
                    lease.run_id,
                    lease.batch_id,
                    lease.owner_token,
                    lease.fencing_token,
                    _timestamp(lease.acquired_at, name="acquired_at"),
                    _timestamp(lease.renewed_at, name="renewed_at"),
                    _timestamp(lease.expires_at, name="expires_at"),
                ),
            )
            connection.commit()
            return lease
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew(
        self,
        lease: RunBatchLease,
        *,
        now: datetime,
        ttl: timedelta,
    ) -> RunBatchLease:
        _timestamp(now, name="now")
        _positive_ttl(ttl)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = _assert_lease_owned(connection, lease, now=now)
            if now < current.renewed_at:
                raise ValueError("now precedes the previous lease renewal")
            renewed = RunBatchLease(
                run_id=current.run_id,
                batch_id=current.batch_id,
                owner_token=current.owner_token,
                fencing_token=current.fencing_token,
                acquired_at=current.acquired_at,
                renewed_at=now,
                expires_at=max(current.expires_at, now + ttl),
            )
            connection.execute(
                "UPDATE run_batch_leases SET renewed_at = ?, expires_at = ? "
                "WHERE run_id = ?",
                (
                    _timestamp(renewed.renewed_at, name="renewed_at"),
                    _timestamp(renewed.expires_at, name="expires_at"),
                    renewed.run_id,
                ),
            )
            connection.commit()
            return renewed
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release(self, lease: RunBatchLease, *, now: datetime) -> None:
        _timestamp(now, name="now")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _assert_lease_owned(connection, lease, now=now)
            connection.execute(
                "DELETE FROM run_batch_leases WHERE run_id = ?",
                (lease.run_id,),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def current(self, run_id: str) -> RunBatchLease | None:
        """Read the persisted claim for audit; it may already be expired."""

        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be non-empty")
        connection = self._connect()
        try:
            return _read_lease(connection, run_id)
        finally:
            connection.close()


class SQLiteFencedUnitOfWorkFactory:
    """Unit-of-work factory that rejects stale lease owners before reads/writes."""

    def __init__(
        self,
        factory: SQLiteUnitOfWorkFactory,
        *,
        lease: RunBatchLease,
        clock: Callable[[], datetime] | None = None,
    ):
        self.factory = factory
        self.lease = lease
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self) -> SQLiteUnitOfWork:
        return SQLiteUnitOfWork(
            self.factory.database_path,
            timeout=self.factory.timeout,
            lease=self.lease,
            clock=self.clock,
        )


__all__ = [
    "PERSISTENCE_CONTRACT",
    "PersistenceContractMismatch",
    "PersistenceConflict",
    "SQLiteFencedUnitOfWorkFactory",
    "SQLiteReadOnlyUnitOfWork",
    "SQLiteReadOnlyUnitOfWorkFactory",
    "SQLiteRunBatchLeaseManager",
    "SQLiteUnitOfWork",
    "SQLiteUnitOfWorkFactory",
]
