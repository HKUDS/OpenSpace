"""Durable stable-action journal for external worktree execution."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from agentpilot.app import ExecutionResult
from agentpilot.domain import (
    ActionExecutionAttestation,
    ActionExecutionFence,
    ActionExecutionMode,
    ChildProgramReadout,
    EvaluationValidity,
    EvidenceTier,
    ImplementationAdherence,
    OutcomeAttribution,
    PairedOutcome,
    RealizedFileChange,
    RealizedFileChangeKind,
    StateDescriptor,
    WorkerExecutionStatus,
    WorkerResult,
    canonical_json,
    utc_now,
)


class ActionStoreError(RuntimeError):
    pass


class ActionClaimUnavailable(ActionStoreError):
    pass


class ActionRecoveryRequired(ActionStoreError):
    pass


class ActionStoreCorruption(ActionStoreError):
    pass


class ActionRecordStatus(str, Enum):
    CLAIMED = "claimed"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class ActionClaimMode(str, Enum):
    EXECUTE = "execute"
    REPLAY = "replay"


@dataclass(frozen=True, slots=True)
class ActionClaim:
    action_id: str
    claim_token: str | None
    mode: ActionClaimMode
    result: ExecutionResult | None = None


@dataclass(frozen=True, slots=True)
class DurableActionRecord:
    action_id: str
    status: ActionRecordStatus
    claim_token: str | None
    claimed_at: datetime
    worktree_id: str | None
    worktree_path: str | None
    result_commit_sha: str | None
    failure_code: str | None
    failure_diagnostics: Mapping[str, Any] | None
    failure_cost_usd: float | None
    result: ExecutionResult | None
    claim_context: ActionClaimContext
    committed_implementation: CommittedImplementation | None


@dataclass(frozen=True, slots=True)
class ActionClaimContext:
    """Exact lease/process owner of one executable journal phase."""

    fence_id: str
    action_id: str
    run_id: str
    batch_id: str
    runner_id: str
    owner_token: str
    fencing_token: int
    parent_commit_sha: str
    scope_identity: str
    process_id: int
    process_start_ticks: int

    @classmethod
    def current(cls, fence: ActionExecutionFence) -> ActionClaimContext:
        if not isinstance(fence, ActionExecutionFence):
            raise TypeError("claim context requires an ActionExecutionFence")
        process_id = os.getpid()
        return cls(
            fence_id=fence.fence_id,
            action_id=fence.action_id,
            run_id=fence.run_id,
            batch_id=fence.batch_id,
            runner_id=fence.runner_id,
            owner_token=fence.owner_token,
            fencing_token=fence.fencing_token,
            parent_commit_sha=fence.parent_commit_sha,
            scope_identity=fence.scope_identity,
            process_id=process_id,
            process_start_ticks=_process_start_ticks(process_id),
        )

    def __post_init__(self) -> None:
        fence = ActionExecutionFence(
            self.fence_id,
            self.run_id,
            self.batch_id,
            self.action_id,
            self.runner_id,
            self.owner_token,
            self.fencing_token,
            self.parent_commit_sha,
            self.scope_identity,
        )
        del fence
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (self.process_id, self.process_start_ticks)
        ):
            raise ValueError("claim process identity must be positive")


@dataclass(frozen=True, slots=True)
class CommittedImplementation:
    """Canonical pre-evaluation facts persisted with the prepared commit."""

    execution_fence_id: str
    started_at: datetime
    mutation_fence_checked_at: datetime
    commit_fence_checked_at: datetime
    touched_files: tuple[str, ...]
    diagnostics: Mapping[str, Any]
    cost_usd: float

    def __post_init__(self) -> None:
        _text(self.execution_fence_id, "execution_fence_id")
        for name in (
            "started_at",
            "mutation_fence_checked_at",
            "commit_fence_checked_at",
        ):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.mutation_fence_checked_at > self.started_at:
            raise ValueError("mutation fence check happened after worker start")
        if self.commit_fence_checked_at < self.mutation_fence_checked_at:
            raise ValueError("commit fence check precedes mutation fence check")
        if len(self.touched_files) != len(set(self.touched_files)) or any(
            not isinstance(path, str) or not path for path in self.touched_files
        ):
            raise ValueError("committed touched files are invalid")
        if not isinstance(self.diagnostics, Mapping):
            raise TypeError("committed diagnostics must be a mapping")
        if not math.isfinite(self.cost_usd) or self.cost_usd < 0.0:
            raise ValueError("committed cost must be finite and non-negative")


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value.strip()


def _timestamp(value: Any, field_name: str) -> datetime:
    text = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ActionStoreCorruption(f"invalid {field_name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ActionStoreCorruption(f"naive {field_name}")
    return parsed.astimezone(timezone.utc)


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ActionStoreCorruption(f"{field_name} must be an object")
    return value


def _only(value: Mapping[str, Any], names: set[str], field_name: str) -> None:
    if set(value) != names:
        raise ActionStoreCorruption(f"{field_name} has an invalid shape")


def _optional_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ActionStoreCorruption(f"{field_name} must be finite or null")
    return float(value)


def _process_start_ticks(process_id: int) -> int:
    """Read Linux's immutable-per-process start tick, resilient to PID reuse."""

    if (
        isinstance(process_id, bool)
        or not isinstance(process_id, int)
        or process_id < 1
    ):
        raise ValueError("process_id must be a positive integer")
    try:
        fields = Path(f"/proc/{process_id}/stat").read_text(encoding="ascii").split()
        value = int(fields[21])
    except (OSError, UnicodeError, ValueError, IndexError) as exc:
        raise RuntimeError("cannot establish action executor process identity") from exc
    if value < 1:
        raise RuntimeError("action executor process identity is invalid")
    return value


def process_identity_is_live(context: ActionClaimContext) -> bool:
    """Return whether the exact recorded process (including start tick) lives."""

    if not isinstance(context, ActionClaimContext):
        raise TypeError("context must be an ActionClaimContext")
    try:
        return _process_start_ticks(context.process_id) == context.process_start_ticks
    except RuntimeError:
        return False


def _claim_context_payload(context: ActionClaimContext) -> Mapping[str, Any]:
    return {
        "fence_id": context.fence_id,
        "action_id": context.action_id,
        "run_id": context.run_id,
        "batch_id": context.batch_id,
        "runner_id": context.runner_id,
        "owner_token": context.owner_token,
        "fencing_token": context.fencing_token,
        "parent_commit_sha": context.parent_commit_sha,
        "scope_identity": context.scope_identity,
        "process_id": context.process_id,
        "process_start_ticks": context.process_start_ticks,
    }


def _decode_claim_context(value: object) -> ActionClaimContext:
    raw = _mapping(value, "claim_context")
    fields = {
        "fence_id",
        "action_id",
        "run_id",
        "batch_id",
        "runner_id",
        "owner_token",
        "fencing_token",
        "parent_commit_sha",
        "scope_identity",
        "process_id",
        "process_start_ticks",
    }
    _only(raw, fields, "claim_context")
    try:
        return ActionClaimContext(**raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ActionStoreCorruption("claim context is invalid") from exc


def _implementation_payload(value: CommittedImplementation) -> Mapping[str, Any]:
    return {
        "execution_fence_id": value.execution_fence_id,
        "started_at": value.started_at,
        "mutation_fence_checked_at": value.mutation_fence_checked_at,
        "commit_fence_checked_at": value.commit_fence_checked_at,
        "touched_files": value.touched_files,
        "diagnostics": value.diagnostics,
        "cost_usd": value.cost_usd,
    }


def _decode_implementation(value: object) -> CommittedImplementation:
    raw = _mapping(value, "committed_implementation")
    _only(
        raw,
        {
            "started_at",
            "execution_fence_id",
            "mutation_fence_checked_at",
            "commit_fence_checked_at",
            "touched_files",
            "diagnostics",
            "cost_usd",
        },
        "committed_implementation",
    )
    touched = raw["touched_files"]
    if not isinstance(touched, list) or any(
        not isinstance(item, str) for item in touched
    ):
        raise ActionStoreCorruption("committed touched files are invalid")
    cost = raw["cost_usd"]
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        raise ActionStoreCorruption("committed cost is invalid")
    try:
        return CommittedImplementation(
            execution_fence_id=_text(
                raw["execution_fence_id"], "implementation execution_fence_id"
            ),
            started_at=_timestamp(raw["started_at"], "implementation started_at"),
            mutation_fence_checked_at=_timestamp(
                raw["mutation_fence_checked_at"], "mutation_fence_checked_at"
            ),
            commit_fence_checked_at=_timestamp(
                raw["commit_fence_checked_at"], "commit_fence_checked_at"
            ),
            touched_files=tuple(touched),
            diagnostics=_mapping(raw["diagnostics"], "committed diagnostics"),
            cost_usd=float(cost),
        )
    except (TypeError, ValueError) as exc:
        raise ActionStoreCorruption("committed implementation is invalid") from exc


def _result_payload(result: ExecutionResult) -> Mapping[str, Any]:
    worker = result.worker
    outcome = result.outcome
    attestation = worker.execution_attestation
    child = outcome.child_readout
    return {
        "worker": {
            "worker_result_id": worker.worker_result_id,
            "action_id": worker.action_id,
            "status": worker.status.value,
            "adherence": worker.adherence.value,
            "started_at": worker.started_at,
            "completed_at": worker.completed_at,
            "result_commit_sha": worker.result_commit_sha,
            "touched_files": worker.touched_files,
            "file_changes": tuple(
                {
                    "kind": item.kind.value,
                    "file": item.file,
                    "symbols": item.symbols,
                    "role": item.role,
                    "from_file": item.from_file,
                }
                for item in worker.file_changes
            ),
            "diagnostics": worker.diagnostics,
            "cost_usd": worker.cost_usd,
            "execution_attestation": (
                None
                if attestation is None
                else {
                    "fence_id": attestation.fence_id,
                    "runner_id": attestation.runner_id,
                    "worktree_id": attestation.worktree_id,
                    "parent_commit_sha": attestation.parent_commit_sha,
                    "result_commit_sha": attestation.result_commit_sha,
                    "mutation_fence_checked_at": (
                        attestation.mutation_fence_checked_at
                    ),
                    "commit_fence_checked_at": attestation.commit_fence_checked_at,
                    "mode": attestation.mode.value,
                    "original_fence_id": attestation.original_fence_id,
                    "replay_verified_at": attestation.replay_verified_at,
                }
            ),
        },
        "outcome": {
            "outcome_id": outcome.outcome_id,
            "action_id": outcome.action_id,
            "evidence_tier": outcome.evidence_tier.value,
            "evaluation_plan_record_id": outcome.evaluation_plan_record_id,
            "validity": outcome.validity.value,
            "attribution": outcome.attribution.value,
            "signed_effectiveness_statistic": (outcome.signed_effectiveness_statistic),
            "constraints_pass": outcome.constraints_pass,
            "completed_at": outcome.completed_at,
            "primary_delta_mean": outcome.primary_delta_mean,
            "primary_delta_lower": outcome.primary_delta_lower,
            "admission_fate": outcome.admission_fate,
            "estimator_identity": outcome.estimator_identity,
            "child_readout": (
                None
                if child is None
                else {
                    "child_program_id": child.child_program_id,
                    "descriptor": {
                        "blocks": child.descriptor.blocks,
                    },
                    "absolute_primary_value": child.absolute_primary_value,
                    "cell_results": child.cell_results,
                    "cell_notes": child.cell_notes,
                }
            ),
            "cell_diagnostics": outcome.cell_diagnostics,
        },
    }


def _decode_attestation(value: Any) -> ActionExecutionAttestation | None:
    if value is None:
        return None
    table = _mapping(value, "execution_attestation")
    names = {
        "fence_id",
        "runner_id",
        "worktree_id",
        "parent_commit_sha",
        "result_commit_sha",
        "mutation_fence_checked_at",
        "commit_fence_checked_at",
        "mode",
        "original_fence_id",
        "replay_verified_at",
    }
    _only(table, names, "execution_attestation")
    replay_at = table["replay_verified_at"]
    return ActionExecutionAttestation(
        fence_id=_text(table["fence_id"], "fence_id"),
        runner_id=_text(table["runner_id"], "runner_id"),
        worktree_id=_text(table["worktree_id"], "worktree_id"),
        parent_commit_sha=_text(table["parent_commit_sha"], "parent_commit_sha"),
        result_commit_sha=table["result_commit_sha"],
        mutation_fence_checked_at=_timestamp(
            table["mutation_fence_checked_at"], "mutation_fence_checked_at"
        ),
        commit_fence_checked_at=_timestamp(
            table["commit_fence_checked_at"], "commit_fence_checked_at"
        ),
        mode=ActionExecutionMode(table["mode"]),
        original_fence_id=table["original_fence_id"],
        replay_verified_at=(
            None if replay_at is None else _timestamp(replay_at, "replay_verified_at")
        ),
    )


def _decode_child(value: Any) -> ChildProgramReadout | None:
    if value is None:
        return None
    table = _mapping(value, "child_readout")
    required = {
        "child_program_id",
        "descriptor",
        "absolute_primary_value",
        "cell_results",
    }
    allowed = required | {"cell_notes"}
    if not required.issubset(table) or set(table) - allowed:
        raise ActionStoreCorruption("child_readout has an invalid shape")
    descriptor = _mapping(table["descriptor"], "child descriptor")
    _only(descriptor, {"blocks"}, "child descriptor")
    value_float = _optional_float(
        table["absolute_primary_value"], "absolute_primary_value"
    )
    if value_float is None:
        raise ActionStoreCorruption("absolute_primary_value cannot be null")
    return ChildProgramReadout(
        child_program_id=_text(table["child_program_id"], "child_program_id"),
        descriptor=StateDescriptor(
            blocks=_mapping(descriptor["blocks"], "descriptor blocks"),
        ),
        absolute_primary_value=value_float,
        cell_results=_mapping(table["cell_results"], "cell_results"),
        cell_notes=_mapping(table.get("cell_notes", {}), "cell_notes"),
    )


def _decode_result(payload: str) -> ExecutionResult:
    try:
        raw = json.loads(payload)
        table = _mapping(raw, "execution result")
        _only(table, {"worker", "outcome"}, "execution result")
        worker = _mapping(table["worker"], "worker")
        _only(
            worker,
            {
                "worker_result_id",
                "action_id",
                "status",
                "adherence",
                "started_at",
                "completed_at",
                "result_commit_sha",
                "touched_files",
                "file_changes",
                "diagnostics",
                "cost_usd",
                "execution_attestation",
            },
            "worker",
        )
        touched = worker["touched_files"]
        if not isinstance(touched, list) or any(
            not isinstance(path, str) or not path for path in touched
        ):
            raise ActionStoreCorruption("touched_files has an invalid shape")
        raw_changes = worker["file_changes"]
        if not isinstance(raw_changes, list):
            raise ActionStoreCorruption("file_changes must be a list")
        file_changes: list[RealizedFileChange] = []
        for raw_change in raw_changes:
            change = _mapping(raw_change, "file change")
            _only(
                change,
                {
                    "kind",
                    "file",
                    "symbols",
                    "role",
                    "from_file",
                },
                "file change",
            )
            symbols = change["symbols"]
            if not isinstance(symbols, list) or any(
                not isinstance(symbol, str) or not symbol for symbol in symbols
            ):
                raise ActionStoreCorruption("file change symbols are invalid")
            for name in ("role", "from_file"):
                value = change[name]
                if value is not None and (not isinstance(value, str) or not value):
                    raise ActionStoreCorruption(f"file change {name} is invalid")
            file_changes.append(
                RealizedFileChange(
                    kind=RealizedFileChangeKind(change["kind"]),
                    file=_text(change["file"], "changed file"),
                    symbols=tuple(symbols),
                    role=change["role"],
                    from_file=change["from_file"],
                )
            )
        cost = _optional_float(worker["cost_usd"], "cost_usd")
        if cost is None:
            raise ActionStoreCorruption("cost_usd cannot be null")
        worker_result = WorkerResult(
            worker_result_id=_text(worker["worker_result_id"], "worker_result_id"),
            action_id=_text(worker["action_id"], "worker action_id"),
            status=WorkerExecutionStatus(worker["status"]),
            adherence=ImplementationAdherence(worker["adherence"]),
            started_at=_timestamp(worker["started_at"], "worker started_at"),
            completed_at=_timestamp(worker["completed_at"], "worker completed_at"),
            result_commit_sha=worker["result_commit_sha"],
            touched_files=tuple(touched),
            file_changes=tuple(file_changes),
            diagnostics=_mapping(worker["diagnostics"], "worker diagnostics"),
            cost_usd=cost,
            execution_attestation=_decode_attestation(worker["execution_attestation"]),
        )
        outcome = _mapping(table["outcome"], "outcome")
        required_outcome = {
            "outcome_id",
            "action_id",
            "evidence_tier",
            "evaluation_plan_record_id",
            "validity",
            "attribution",
            "signed_effectiveness_statistic",
            "constraints_pass",
            "completed_at",
            "primary_delta_mean",
            "primary_delta_lower",
            "admission_fate",
            "estimator_identity",
            "child_readout",
        }
        allowed_outcome = required_outcome | {"cell_diagnostics"}
        if not required_outcome.issubset(outcome) or set(outcome) - allowed_outcome:
            raise ActionStoreCorruption("outcome has an invalid shape")
        constraints = outcome["constraints_pass"]
        if constraints is not None and not isinstance(constraints, bool):
            raise ActionStoreCorruption("constraints_pass must be boolean or null")
        paired = PairedOutcome(
            outcome_id=_text(outcome["outcome_id"], "outcome_id"),
            action_id=_text(outcome["action_id"], "outcome action_id"),
            evidence_tier=EvidenceTier(outcome["evidence_tier"]),
            evaluation_plan_record_id=outcome["evaluation_plan_record_id"],
            validity=EvaluationValidity(outcome["validity"]),
            attribution=OutcomeAttribution(outcome["attribution"]),
            signed_effectiveness_statistic=_optional_float(
                outcome["signed_effectiveness_statistic"],
                "signed_effectiveness_statistic",
            ),
            constraints_pass=constraints,
            completed_at=_timestamp(outcome["completed_at"], "outcome completed_at"),
            primary_delta_mean=_optional_float(
                outcome["primary_delta_mean"], "primary_delta_mean"
            ),
            primary_delta_lower=_optional_float(
                outcome["primary_delta_lower"], "primary_delta_lower"
            ),
            admission_fate=outcome["admission_fate"],
            estimator_identity=_mapping(
                outcome["estimator_identity"], "estimator_identity"
            ),
            cell_diagnostics=_mapping(
                outcome.get("cell_diagnostics", {}), "cell_diagnostics"
            ),
            child_readout=_decode_child(outcome["child_readout"]),
        )
        return ExecutionResult(worker_result, paired)
    except ActionStoreCorruption:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ActionStoreCorruption("invalid durable execution result") from exc


class SQLiteActionStore:
    """Atomic claim/result journal; interrupted claims never auto-reexecute."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        source_scope_id: str,
        timeout: float = 30.0,
    ):
        if isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("timeout must be positive")
        path = Path(database_path)
        if str(path) == ":memory:":
            raise ValueError("action store requires a durable file")
        path = path.expanduser().resolve()
        if not path.parent.is_dir():
            raise ValueError("action store parent directory does not exist")
        self.database_path = str(path)
        self.source_scope_id = _text(source_scope_id, "source_scope_id")
        self.timeout = float(timeout)
        self._initialize()

    @classmethod
    def open_for_recovery(
        cls,
        database_path: str | Path,
        *,
        timeout: float = 30.0,
    ) -> SQLiteActionStore:
        """Open an already-scoped journal for explicit operator recovery."""

        path = Path(database_path).expanduser().resolve()
        try:
            connection = sqlite3.connect(
                f"file:{path}?mode=ro",
                uri=True,
                timeout=timeout,
            )
            try:
                row = connection.execute(
                    "SELECT value FROM action_store_metadata "
                    "WHERE key = 'source_scope_id'"
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise ActionStoreCorruption(
                "cannot read action-store source scope"
            ) from exc
        if row is None:
            raise ActionStoreCorruption("action store has no immutable source scope")
        return cls(
            path, source_scope_id=_text(row[0], "source_scope_id"), timeout=timeout
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.timeout,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS action_executions ("
                "action_id TEXT PRIMARY KEY, "
                "status TEXT NOT NULL CHECK(status IN "
                "('claimed', 'completed', 'blocked')), "
                "claim_token TEXT, claimed_at TEXT NOT NULL, "
                "claim_context_json TEXT NOT NULL, "
                "worktree_id TEXT, worktree_path TEXT, result_commit_sha TEXT, "
                "implementation_json TEXT, failure_code TEXT, "
                "failure_diagnostics_json TEXT, failure_cost_usd REAL, "
                "result_json TEXT, "
                "completed_at TEXT, "
                "CHECK((result_commit_sha IS NULL AND implementation_json IS NULL) "
                "OR (result_commit_sha IS NOT NULL AND implementation_json IS NOT NULL)), "
                "CHECK("
                "(status = 'claimed' AND claim_token IS NOT NULL "
                "AND failure_code IS NULL AND failure_diagnostics_json IS NULL "
                "AND failure_cost_usd IS NULL AND result_json IS NULL "
                "AND completed_at IS NULL) OR "
                "(status = 'completed' AND claim_token IS NULL "
                "AND failure_code IS NULL AND failure_diagnostics_json IS NULL "
                "AND failure_cost_usd IS NULL AND result_json IS NOT NULL "
                "AND completed_at IS NOT NULL) OR "
                "(status = 'blocked' AND claim_token IS NULL "
                "AND failure_code IS NOT NULL AND result_json IS NULL "
                "AND completed_at IS NOT NULL)))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS action_store_metadata ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT value FROM action_store_metadata WHERE key = 'source_scope_id'"
            ).fetchone()
            if row is None:
                count = connection.execute(
                    "SELECT COUNT(*) FROM action_executions"
                ).fetchone()[0]
                if count:
                    raise ActionStoreCorruption(
                        "existing action rows have no immutable source scope"
                    )
                connection.execute(
                    "INSERT INTO action_store_metadata(key, value) VALUES (?, ?)",
                    ("source_scope_id", self.source_scope_id),
                )
            elif row[0] != self.source_scope_id:
                raise ActionStoreCorruption(
                    "action store belongs to a different source scope"
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate_action_id(action_id: str) -> None:
        _text(action_id, "action_id")

    def claim(self, *, action_id: str, fence: ActionExecutionFence) -> ActionClaim:
        self._validate_action_id(action_id)
        context = ActionClaimContext.current(fence)
        if context.action_id != action_id:
            raise ValueError("action claim differs from execution fence")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, result_json, claim_context_json, "
                "implementation_json, result_commit_sha "
                "FROM action_executions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if row is None:
                token = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO action_executions("
                    "action_id, status, claim_token, claimed_at, claim_context_json"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        action_id,
                        ActionRecordStatus.CLAIMED.value,
                        token,
                        utc_now().isoformat(),
                        canonical_json(_claim_context_payload(context)),
                    ),
                )
                connection.commit()
                return ActionClaim(
                    action_id,
                    token,
                    ActionClaimMode.EXECUTE,
                )
            (
                status_text,
                result_json,
                raw_context,
                raw_implementation,
                recorded_commit,
            ) = row
            status = ActionRecordStatus(status_text)
            if status is ActionRecordStatus.COMPLETED:
                if not isinstance(result_json, str):
                    raise ActionStoreCorruption(
                        "completed action has no durable result"
                    )
                try:
                    stored_context = _decode_claim_context(json.loads(raw_context))
                    stored_implementation = (
                        None
                        if raw_implementation is None
                        else _decode_implementation(json.loads(raw_implementation))
                    )
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ActionStoreCorruption(
                        "completed action recovery state is invalid"
                    ) from exc
                result = _decode_result(result_json)
                if (
                    stored_context.action_id != action_id
                    or result.worker.result_commit_sha != recorded_commit
                    or (recorded_commit is None) != (stored_implementation is None)
                ):
                    raise ActionStoreCorruption("completed action state is misbound")
                connection.commit()
                return ActionClaim(
                    action_id,
                    None,
                    ActionClaimMode.REPLAY,
                    result,
                )
            if status is ActionRecordStatus.BLOCKED:
                raise ActionRecoveryRequired(
                    "action is blocked pending explicit recovery"
                )
            raise ActionClaimUnavailable("action already has an active claim")
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _claim_token(claim: ActionClaim) -> str:
        if claim.mode is not ActionClaimMode.EXECUTE or claim.claim_token is None:
            raise ValueError("operation requires an executable claim")
        return claim.claim_token

    def _update_claim(
        self, claim: ActionClaim, sql: str, parameters: tuple[Any, ...]
    ) -> None:
        token = self._claim_token(claim)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                sql,
                (*parameters, claim.action_id, token),
            )
            if cursor.rowcount != 1:
                raise ActionClaimUnavailable(
                    "action claim was completed, blocked, or superseded"
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def attach_workspace(
        self, claim: ActionClaim, *, worktree_id: str, worktree_path: str | Path
    ) -> None:
        identity = _text(worktree_id, "worktree_id")
        path = Path(worktree_path).expanduser()
        if not path.is_absolute():
            raise ValueError("worktree_path must be absolute")
        self._update_claim(
            claim,
            "UPDATE action_executions SET worktree_id = ?, worktree_path = ? "
            "WHERE action_id = ? AND claim_token = ? "
            "AND status = 'claimed' AND worktree_id IS NULL",
            (identity, str(path)),
        )

    def release_unmutated(self, claim: ActionClaim) -> None:
        """Drop only a claim that has not acquired any workspace or commit."""

        token = self._claim_token(claim)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM action_executions WHERE action_id = ? "
                "AND claim_token = ? "
                "AND status = 'claimed' AND worktree_id IS NULL "
                "AND worktree_path IS NULL AND result_commit_sha IS NULL",
                (claim.action_id, token),
            )
            if cursor.rowcount != 1:
                raise ActionClaimUnavailable(
                    "action claim has crossed the mutation boundary"
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def release_uncommitted(self, claim: ActionClaim) -> None:
        """Drop a claim whose workspace is gone and which never committed.

        Separate from ``release_unmutated`` because that one guards the mutation
        boundary by requiring no workspace at all.  A claim that took a
        workspace and then failed for a reason unrelated to the candidate has
        still produced nothing to keep, so it may be reclaimed -- but only once
        the caller has actually removed the workspace, and never after a commit,
        since a commit is a result someone may already have read.
        """

        token = self._claim_token(claim)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM action_executions WHERE action_id = ? "
                "AND claim_token = ? "
                "AND status = 'claimed' AND result_commit_sha IS NULL",
                (claim.action_id, token),
            )
            if cursor.rowcount != 1:
                raise ActionClaimUnavailable(
                    "action claim already committed or is no longer held"
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def release_stale_claimed_uncommitted(
        self,
        *,
        action_id: str,
        expected_claim_token: str,
        expected_claimed_at: datetime,
        expected_worktree_id: str | None,
        expected_worktree_path: str | Path | None,
    ) -> None:
        """Release one externally interrupted claim after operator cleanup.

        This is an exact compare-and-delete primitive, not an automatic lease
        expiry policy.  The caller must first establish that the old executor
        and all of its child processes/containers are gone, validate and remove
        the recorded worktree, and clean any harness-private state.  Matching
        the original token, timestamp, and workspace coordinates keeps
        a concurrent or newly changed claim from being released accidentally.
        A claim that crossed the commit boundary is never recoverable here.
        """

        self._validate_action_id(action_id)
        token = _text(expected_claim_token, "expected_claim_token")
        if not isinstance(expected_claimed_at, datetime):
            raise TypeError("expected_claimed_at must be a datetime")
        if (
            expected_claimed_at.tzinfo is None
            or expected_claimed_at.utcoffset() is None
        ):
            raise ValueError("expected_claimed_at must be timezone-aware")
        claimed_at = expected_claimed_at.astimezone(timezone.utc).isoformat()
        if (expected_worktree_id is None) is not (expected_worktree_path is None):
            raise ValueError(
                "expected workspace coordinates must be both set or absent"
            )
        worktree_id = (
            None
            if expected_worktree_id is None
            else _text(expected_worktree_id, "expected_worktree_id")
        )
        worktree_path: str | None = None
        if expected_worktree_path is not None:
            candidate = Path(expected_worktree_path).expanduser()
            if not candidate.is_absolute():
                raise ValueError("expected_worktree_path must be absolute")
            worktree_path = str(candidate)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM action_executions WHERE action_id = ? "
                "AND status = 'claimed' AND claim_token = ? AND claimed_at = ? "
                "AND result_commit_sha IS NULL "
                "AND worktree_id IS ? AND worktree_path IS ?",
                (
                    action_id,
                    token,
                    claimed_at,
                    worktree_id,
                    worktree_path,
                ),
            )
            if cursor.rowcount != 1:
                raise ActionRecoveryRequired(
                    "claimed action token, timestamp, workspace, or commit "
                    "precondition changed"
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def release_blocked_uncommitted(
        self,
        *,
        action_id: str,
        expected_failure_code: str,
    ) -> None:
        """Release one explicitly recovered blocked action before any commit.

        This is deliberately not automatic. The caller must first establish a
        new live run fence and clean the recorded worktree/private harness
        artifacts. Exact coordinates and the observed failure code prevent an
        operator from releasing a different or newly changed blocked record.
        A recorded commit is never recoverable through this path.
        """

        self._validate_action_id(action_id)
        failure = _text(expected_failure_code, "expected_failure_code")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT failure_diagnostics_json FROM action_executions "
                "WHERE action_id = ? AND status = 'blocked' "
                "AND failure_code = ? AND result_commit_sha IS NULL",
                (action_id, failure),
            ).fetchone()
            if row is not None and row[0] is not None:
                try:
                    diagnostics = _mapping(json.loads(row[0]), "failure_diagnostics")
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ActionStoreCorruption(
                        "blocked failure diagnostics are invalid"
                    ) from exc
                if diagnostics.get("disposition") == "accounting_quarantine":
                    raise ActionRecoveryRequired(
                        "accounting quarantine forbids action re-execution"
                    )
            cursor = connection.execute(
                "DELETE FROM action_executions WHERE action_id = ? "
                "AND status = 'blocked' "
                "AND failure_code = ? AND result_commit_sha IS NULL",
                (action_id, failure),
            )
            if cursor.rowcount != 1:
                raise ActionRecoveryRequired(
                    "blocked action coordinates, failure code, or commit "
                    "precondition changed"
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def record_commit(
        self,
        claim: ActionClaim,
        *,
        result_commit_sha: str,
        implementation: CommittedImplementation,
    ) -> None:
        commit = _text(result_commit_sha, "result_commit_sha")
        if not isinstance(implementation, CommittedImplementation):
            raise TypeError("implementation must be CommittedImplementation")
        self._update_claim(
            claim,
            "UPDATE action_executions SET result_commit_sha = ?, "
            "implementation_json = ? "
            "WHERE action_id = ? AND claim_token = ? "
            "AND status = 'claimed' AND result_commit_sha IS NULL",
            (commit, canonical_json(_implementation_payload(implementation))),
        )

    def reclaim_blocked_postcommit(
        self,
        *,
        action_id: str,
        expected_failure_code: str,
        expected_result_commit_sha: str,
        fence: ActionExecutionFence,
    ) -> ActionClaim:
        """Reclaim one exact committed action for idempotent Arena replay."""

        self._validate_action_id(action_id)
        failure = _text(expected_failure_code, "expected_failure_code")
        commit = _text(expected_result_commit_sha, "expected_result_commit_sha")
        context = ActionClaimContext.current(fence)
        if context.action_id != action_id:
            raise ValueError("post-commit reclaim differs from execution fence")
        token = str(uuid.uuid4())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE action_executions SET status = 'claimed', "
                "claim_token = ?, claimed_at = ?, failure_code = NULL, "
                "failure_diagnostics_json = NULL, failure_cost_usd = NULL, "
                "completed_at = NULL, claim_context_json = ? "
                "WHERE action_id = ? AND status = 'blocked' "
                "AND failure_code = ? AND result_commit_sha = ? "
                "AND worktree_id IS NOT NULL AND worktree_path IS NOT NULL",
                (
                    token,
                    utc_now().isoformat(),
                    canonical_json(_claim_context_payload(context)),
                    action_id,
                    failure,
                    commit,
                ),
            )
            if cursor.rowcount != 1:
                raise ActionRecoveryRequired("post-commit recovery coordinates changed")
            connection.commit()
            return ActionClaim(action_id, token, ActionClaimMode.EXECUTE)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def block_stale_claimed_postcommit(
        self,
        *,
        record: DurableActionRecord,
        failure_code: str,
    ) -> None:
        """Fence one dead executor's exact committed claim for safe replay."""

        if (
            not isinstance(record, DurableActionRecord)
            or record.status is not ActionRecordStatus.CLAIMED
            or record.claim_token is None
            or record.result_commit_sha is None
            or record.committed_implementation is None
        ):
            raise ValueError("record must be an active committed action")
        code = _text(failure_code, "failure_code")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE action_executions SET status = 'blocked', "
                "claim_token = NULL, failure_code = ?, completed_at = ? "
                "WHERE action_id = ? AND status = 'claimed' "
                "AND claim_token = ? AND claimed_at = ? "
                "AND claim_context_json = ? AND worktree_id IS ? "
                "AND worktree_path IS ? AND result_commit_sha = ? "
                "AND implementation_json = ?",
                (
                    code,
                    utc_now().isoformat(),
                    record.action_id,
                    record.claim_token,
                    record.claimed_at.isoformat(),
                    canonical_json(_claim_context_payload(record.claim_context)),
                    record.worktree_id,
                    record.worktree_path,
                    record.result_commit_sha,
                    canonical_json(
                        _implementation_payload(record.committed_implementation)
                    ),
                ),
            )
            if cursor.rowcount != 1:
                raise ActionRecoveryRequired(
                    "committed action ownership or recovery coordinates changed"
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def block_stale_claimed_uncommitted_accounting(
        self,
        *,
        record: DurableActionRecord,
        failure_code: str,
        failure_diagnostics: Mapping[str, Any],
        failure_cost_usd: float | None,
    ) -> None:
        """Fence a dead pre-commit executor whose provider journal forbids replay."""

        if (
            not isinstance(record, DurableActionRecord)
            or record.status is not ActionRecordStatus.CLAIMED
            or record.claim_token is None
            or record.result_commit_sha is not None
            or record.committed_implementation is not None
        ):
            raise ValueError("record must be an active uncommitted action")
        code = _text(failure_code, "failure_code")
        if not isinstance(failure_diagnostics, Mapping):
            raise TypeError("failure_diagnostics must be a mapping")
        if failure_cost_usd is not None and (
            isinstance(failure_cost_usd, bool)
            or not isinstance(failure_cost_usd, (int, float))
            or not math.isfinite(float(failure_cost_usd))
            or failure_cost_usd < 0
        ):
            raise ValueError("failure_cost_usd must be finite and non-negative")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE action_executions SET status = 'blocked', "
                "claim_token = NULL, failure_code = ?, "
                "failure_diagnostics_json = ?, failure_cost_usd = ?, "
                "completed_at = ? WHERE action_id = ? AND status = 'claimed' "
                "AND claim_token = ? AND claimed_at = ? "
                "AND claim_context_json = ? AND worktree_id IS ? "
                "AND worktree_path IS ? AND result_commit_sha IS NULL "
                "AND implementation_json IS NULL",
                (
                    code,
                    canonical_json(dict(failure_diagnostics)),
                    None if failure_cost_usd is None else float(failure_cost_usd),
                    utc_now().isoformat(),
                    record.action_id,
                    record.claim_token,
                    record.claimed_at.isoformat(),
                    canonical_json(_claim_context_payload(record.claim_context)),
                    record.worktree_id,
                    record.worktree_path,
                ),
            )
            if cursor.rowcount != 1:
                raise ActionRecoveryRequired(
                    "uncommitted action ownership or recovery coordinates changed"
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def complete(self, claim: ActionClaim, result: ExecutionResult) -> None:
        if not isinstance(result, ExecutionResult):
            raise TypeError("result must be an AgentPilot ExecutionResult")
        if (
            result.worker.action_id != claim.action_id
            or result.outcome.action_id != claim.action_id
        ):
            raise ValueError("execution result differs from claimed action")
        attestation = result.worker.execution_attestation
        if attestation is None:
            raise ValueError("durable original result needs an execution attestation")
        commit = result.worker.result_commit_sha
        completed_candidate = (
            result.worker.status is WorkerExecutionStatus.COMPLETED
            and result.outcome.evidence_tier is EvidenceTier.ADAPTIVE_B
            and commit is not None
        )
        rejected_realization = (
            result.worker.status is WorkerExecutionStatus.FAILED
            and result.outcome.evidence_tier is EvidenceTier.REALIZATION
            and commit is None
        )
        terminal_adherence = (
            result.worker.status is WorkerExecutionStatus.COMPLETED
            and result.worker.adherence
            in {
                ImplementationAdherence.DEVIATED,
                ImplementationAdherence.UNKNOWN,
            }
            and result.outcome.evidence_tier is EvidenceTier.REALIZATION
            and result.outcome.admission_fate
            in {"adherence_rejected", "adherence_unknown"}
            and commit is not None
        )
        if not (completed_candidate or rejected_realization or terminal_adherence):
            raise ValueError(
                "completed action must be an evaluated candidate or a terminal "
                "realization rejection"
            )
        payload = canonical_json(_result_payload(result))
        commit_predicate = (
            "result_commit_sha = ?"
            if commit is not None
            else "result_commit_sha IS NULL"
        )
        parameters: tuple[Any, ...] = (
            (payload, utc_now().isoformat(), commit)
            if commit is not None
            else (payload, utc_now().isoformat())
        )
        self._update_claim(
            claim,
            "UPDATE action_executions SET status = 'completed', "
            "claim_token = NULL, result_json = ?, completed_at = ? "
            f"WHERE status = 'claimed' AND {commit_predicate} "
            "AND action_id = ? AND claim_token = ?",
            parameters,
        )

    def block(
        self,
        claim: ActionClaim,
        *,
        failure_code: str,
        failure_diagnostics: Mapping[str, Any] | None = None,
        failure_cost_usd: float | None = None,
        worktree_id: str | None = None,
        worktree_path: str | Path | None = None,
    ) -> None:
        code = _text(failure_code, "failure_code")
        if failure_diagnostics is not None and not isinstance(
            failure_diagnostics, Mapping
        ):
            raise TypeError("failure_diagnostics must be a mapping or None")
        if failure_cost_usd is not None and (
            isinstance(failure_cost_usd, bool)
            or not isinstance(failure_cost_usd, (int, float))
            or not math.isfinite(float(failure_cost_usd))
            or float(failure_cost_usd) < 0
        ):
            raise ValueError("failure_cost_usd must be finite and non-negative")
        diagnostics_json = (
            None
            if failure_diagnostics is None
            else canonical_json(dict(failure_diagnostics))
        )
        if (worktree_id is None) is not (worktree_path is None):
            raise ValueError("blocked workspace coordinates must be both set or absent")
        identity = None if worktree_id is None else _text(worktree_id, "worktree_id")
        path: str | None = None
        if worktree_path is not None:
            candidate = Path(worktree_path).expanduser()
            if not candidate.is_absolute():
                raise ValueError("worktree_path must be absolute")
            path = str(candidate)
        self._update_claim(
            claim,
            "UPDATE action_executions SET status = 'blocked', "
            "claim_token = NULL, failure_code = ?, failure_diagnostics_json = ?, "
            "failure_cost_usd = ?, completed_at = ?, "
            "worktree_id = COALESCE(worktree_id, ?), "
            "worktree_path = COALESCE(worktree_path, ?) "
            "WHERE action_id = ? AND claim_token = ? "
            "AND status = 'claimed'",
            (
                code,
                diagnostics_json,
                None if failure_cost_usd is None else float(failure_cost_usd),
                utc_now().isoformat(),
                identity,
                path,
            ),
        )

    def get(self, action_id: str) -> DurableActionRecord | None:
        identity = _text(action_id, "action_id")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT status, claim_token, claimed_at, worktree_id, "
                "worktree_path, result_commit_sha, failure_code, "
                "failure_diagnostics_json, failure_cost_usd, result_json, "
                "claim_context_json, implementation_json "
                "FROM action_executions WHERE action_id = ?",
                (identity,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        (
            status,
            token,
            claimed_at,
            worktree_id,
            path,
            commit,
            failure,
            raw_failure_diagnostics,
            failure_cost,
            payload,
            raw_claim_context,
            raw_implementation,
        ) = row
        try:
            claim_context = _decode_claim_context(json.loads(raw_claim_context))
            implementation = (
                None
                if raw_implementation is None
                else _decode_implementation(json.loads(raw_implementation))
            )
        except (TypeError, json.JSONDecodeError) as exc:
            raise ActionStoreCorruption("action recovery state is invalid") from exc
        if claim_context.action_id != identity:
            raise ActionStoreCorruption("action claim context is misbound")
        try:
            failure_diagnostics = (
                None
                if raw_failure_diagnostics is None
                else _mapping(
                    json.loads(raw_failure_diagnostics), "failure_diagnostics"
                )
            )
        except (TypeError, json.JSONDecodeError) as exc:
            raise ActionStoreCorruption("failure diagnostics are invalid") from exc
        normalized_failure_cost = _optional_float(failure_cost, "failure_cost_usd")
        if normalized_failure_cost is not None and normalized_failure_cost < 0:
            raise ActionStoreCorruption("failure_cost_usd is negative")
        return DurableActionRecord(
            action_id=identity,
            status=ActionRecordStatus(status),
            claim_token=token,
            claimed_at=_timestamp(claimed_at, "claimed_at"),
            worktree_id=worktree_id,
            worktree_path=path,
            result_commit_sha=commit,
            failure_code=failure,
            failure_diagnostics=failure_diagnostics,
            failure_cost_usd=normalized_failure_cost,
            result=None if payload is None else _decode_result(payload),
            claim_context=claim_context,
            committed_implementation=implementation,
        )

    def observed_cell_results(self, *, cutoff: datetime) -> dict[str, tuple[int, int]]:
        """Tally cell outcomes that were complete at the frozen cutoff.

        This is evidence about the tasks, not about any one candidate: a task
        every candidate passes and a task every candidate fails both separate
        nothing, whoever was measured.  Unmeasured cells carry no information
        and are skipped.  Requiring the caller's SearchState cutoff prevents a
        batch planner from observing results produced by a sibling action after
        that batch was frozen.
        """

        if not isinstance(cutoff, datetime):
            raise TypeError("cutoff must be a datetime")
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("cutoff must be timezone-aware")
        frozen_cutoff = cutoff.astimezone(timezone.utc)

        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT result_json FROM action_executions "
                "WHERE status = 'completed' AND result_json IS NOT NULL "
                "AND completed_at <= ?",
                (frozen_cutoff.isoformat(),),
            ).fetchall()
        finally:
            connection.close()
        tally: dict[str, tuple[int, int]] = {}
        for (payload,) in rows:
            outcome = _decode_result(payload).outcome
            if outcome.completed_at > frozen_cutoff:
                continue
            child = outcome.child_readout
            if child is None:
                continue
            for task_id, passed in child.cell_results.items():
                if passed is None:
                    continue
                passes, failures = tally.get(task_id, (0, 0))
                tally[task_id] = (
                    (passes + 1, failures) if passed else (passes, failures + 1)
                )
        return tally


__all__ = [
    "ActionClaim",
    "ActionClaimContext",
    "ActionClaimMode",
    "ActionClaimUnavailable",
    "ActionRecordStatus",
    "ActionRecoveryRequired",
    "ActionStoreCorruption",
    "ActionStoreError",
    "CommittedImplementation",
    "DurableActionRecord",
    "SQLiteActionStore",
    "process_identity_is_live",
]
