"""Thin Arena evaluator injected into the vendored Shinka search loop."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import stat
import tempfile
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

from rsi_arena.baselines._development_state import BaselineDevelopmentState
from rsi_arena.dto import DevelopmentResult
from rsi_arena.runner import ArenaSession
from rsi_arena.service import (
    ArenaCandidateRejected,
    NativeCandidateMaterialization,
    NativeCandidateTerminal,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AReceipt:
    results: tuple[tuple[str, str, str], ...]
    aggregate: Mapping[str, object]


@dataclass(frozen=True)
class _Receipt:
    attempt_id: str
    occurrence_key: str
    candidate_evaluation_id: str
    outcome: str
    terminal: Mapping[str, object]
    a_receipt: _AReceipt | None
    acknowledged: bool = False


@dataclass(frozen=True)
class _Rejection:
    attempt_id: str
    occurrence_key: str
    raw_sha256: str
    kind: str


class _ReceiptJournal:
    """Small adapter-private durability boundary for Arena terminals."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._lock = threading.Lock()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    occurrence_key TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    candidate_evaluation_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    terminal_json TEXT NOT NULL,
                    a_receipt_json TEXT,
                    native_state_reference TEXT,
                    acknowledged INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rejections (
                    occurrence_key TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    raw_sha256 TEXT NOT NULL,
                    kind TEXT NOT NULL
                )
                """
            )

    def record(self, receipt: _Receipt) -> _Receipt:
        terminal_json = _canonical_json(receipt.terminal)
        a_json = (
            _canonical_json(
                {
                    "benchmark_results": [
                        {
                            "benchmark_id": benchmark_id,
                            "development_evaluation_id": evaluation_id,
                            "outcome": outcome,
                        }
                        for benchmark_id, evaluation_id, outcome in (
                            receipt.a_receipt.results
                        )
                    ],
                    "aggregate": dict(receipt.a_receipt.aggregate),
                }
            )
            if receipt.a_receipt is not None
            else None
        )
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO receipts (
                    occurrence_key, attempt_id, candidate_evaluation_id, outcome,
                    terminal_json, a_receipt_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.occurrence_key,
                    receipt.attempt_id,
                    receipt.candidate_evaluation_id,
                    receipt.outcome,
                    terminal_json,
                    a_json,
                ),
            )
            row = connection.execute(
                "SELECT * FROM receipts WHERE occurrence_key = ?",
                (receipt.occurrence_key,),
            ).fetchone()
        stored = _receipt_from_row(row)
        if (
            stored.attempt_id != receipt.attempt_id
            or stored.candidate_evaluation_id != receipt.candidate_evaluation_id
            or stored.outcome != receipt.outcome
            or _canonical_json(stored.terminal) != terminal_json
        ):
            raise RuntimeError("Shinka occurrence conflicts with its Arena receipt")
        return stored

    def get(self, occurrence_key: str) -> _Receipt | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM receipts WHERE occurrence_key = ?",
                (occurrence_key,),
            ).fetchone()
        return _receipt_from_row(row) if row is not None else None

    def record_rejection(self, rejection: _Rejection) -> _Rejection:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO rejections (
                    occurrence_key, attempt_id, raw_sha256, kind
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    rejection.occurrence_key,
                    rejection.attempt_id,
                    rejection.raw_sha256,
                    rejection.kind,
                ),
            )
            row = connection.execute(
                "SELECT * FROM rejections WHERE occurrence_key = ?",
                (rejection.occurrence_key,),
            ).fetchone()
        stored = _Rejection(
            attempt_id=str(row["attempt_id"]),
            occurrence_key=str(row["occurrence_key"]),
            raw_sha256=str(row["raw_sha256"]),
            kind=str(row["kind"]),
        )
        if stored != rejection:
            raise RuntimeError("Shinka occurrence conflicts with its rejection")
        return stored

    def get_rejection(self, occurrence_key: str) -> _Rejection | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM rejections WHERE occurrence_key = ?",
                (occurrence_key,),
            ).fetchone()
        if row is None:
            return None
        return _Rejection(
            attempt_id=str(row["attempt_id"]),
            occurrence_key=str(row["occurrence_key"]),
            raw_sha256=str(row["raw_sha256"]),
            kind=str(row["kind"]),
        )

    def rejections(self) -> list[_Rejection]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM rejections ORDER BY rowid"
            ).fetchall()
        return [
            _Rejection(
                attempt_id=str(row["attempt_id"]),
                occurrence_key=str(row["occurrence_key"]),
                raw_sha256=str(row["raw_sha256"]),
                kind=str(row["kind"]),
            )
            for row in rows
        ]

    def unacknowledged(self) -> list[_Receipt]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM receipts WHERE acknowledged = 0 ORDER BY rowid"
            ).fetchall()
        return [_receipt_from_row(row) for row in rows]

    def mark_acknowledged(
        self, occurrence_key: str, *, native_state_reference: str
    ) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE receipts
                SET acknowledged = 1, native_state_reference = ?
                WHERE occurrence_key = ?
                """,
                (native_state_reference, occurrence_key),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=60.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 60000")
        return connection


class ArenaEvaluator:
    """Map one native candidate file to Shinka's evaluator result shape."""

    media_type = "text/x-python"

    def __init__(
        self,
        session: ArenaSession,
        *,
        candidate_path: str,
        development_state: BaselineDevelopmentState,
    ) -> None:
        if not isinstance(development_state, BaselineDevelopmentState):
            raise TypeError("Shinka evaluator requires durable development state")
        self.session = session
        self.development_state = development_state
        self.candidate_path = _candidate_relative_path(candidate_path)
        self._journal = _ReceiptJournal(
            session.method_state_directory / "shinka" / "arena-receipts.sqlite"
        )

    def submit(
        self, candidate_path: Path, *, occurrence_key: str
    ) -> dict[str, object] | None:
        try:
            raw = _read_candidate(Path(candidate_path))
            attempt_id = f"attempt-shinka-{occurrence_key}"
            raw_sha256 = hashlib.sha256(raw).hexdigest()
            rejection = self._journal.get_rejection(occurrence_key)
            known_attempt = self.session.spine.attempt(attempt_id)
            if rejection is not None:
                if (
                    rejection.attempt_id != attempt_id
                    or rejection.raw_sha256 != raw_sha256
                ):
                    raise RuntimeError("Shinka rejection replay changed identity")
                return None
            if known_attempt is None:
                slots = self.development_state.slots_for_attempt(attempt_id)
                self.session.start_attempt(
                    attempt_id, kind="single", benchmark_slots=slots
                )
            else:
                self.session.start_attempt(attempt_id)
            reservation = self.session.reserve_native_candidate(
                raw,
                attempt_id=attempt_id,
                occurrence_key=occurrence_key,
                media_type=self.media_type,
                role="candidate",
            )
            try:
                materialized = self.session.materialize_native_candidate(
                    reservation,
                    materialize=self._materialize,
                )
            except ArenaCandidateRejected as exc:
                self._journal.record_rejection(
                    _Rejection(
                        attempt_id=attempt_id,
                        occurrence_key=occurrence_key,
                        raw_sha256=raw_sha256,
                        kind=exc.kind,
                    )
                )
                self.session.transition_attempt(attempt_id, "skip")
                return None
            a_receipt: _AReceipt | None = None
            if isinstance(materialized, NativeCandidateMaterialization):
                if reservation.terminal_result is None:
                    a_receipt = self._run_a()
                self.session.transition_attempt(attempt_id, "enter")
                terminal = self.session.evaluate_native_candidate(materialized)
            else:
                terminal = materialized
            self.session.seal_attempt(attempt_id)
            selection = _terminal_selection(terminal)
            receipt = self._journal.record(
                _Receipt(
                    attempt_id=attempt_id,
                    occurrence_key=occurrence_key,
                    candidate_evaluation_id=terminal.result.candidate_evaluation_id,
                    outcome=terminal.result.outcome,
                    terminal={
                        **dict(selection),
                        "attempt_id": attempt_id,
                        "occurrence_key": occurrence_key,
                        "candidate_evaluation_id": terminal.result.candidate_evaluation_id,
                        "outcome": terminal.result.outcome,
                        "materialization_status": terminal.materialization_status,
                    },
                    a_receipt=a_receipt,
                )
            )
            if receipt.outcome == "inconclusive":
                # No native fitness row is admitted. The durable private receipt
                # is already sufficient to acknowledge this terminal.
                self._acknowledge(
                    receipt,
                    native_state_reference=f"non-admission:{occurrence_key}",
                )
                return None
            return _native_measured_result(
                selection,
                materialization_status=terminal.materialization_status,
                a_receipt=receipt.a_receipt,
            )
        except BaseException:
            raise

    def post_persist(self, occurrence_key: str, *, native_state_reference: str) -> None:
        """ACK a measured result after Shinka's native completion marker."""

        receipt = self._journal.get(occurrence_key)
        if receipt is None or receipt.outcome != "measured":
            raise RuntimeError("Shinka post-persist hook has no measured receipt")
        completed_program_id = _completed_native_programs(
            self.session.method_state_directory
            / "shinka"
            / "results"
            / "programs.sqlite"
        ).get(occurrence_key)
        if completed_program_id != native_state_reference:
            raise RuntimeError("Shinka native completion binding is invalid")
        self._acknowledge(
            receipt,
            native_state_reference=native_state_reference,
        )

    def reconcile_persisted(self, database: Path) -> None:
        """Replay rejection seals and persisted native receipt ACKs."""

        for rejection in self._journal.rejections():
            if self.session.spine.attempt(rejection.attempt_id) is not None:
                self.session.seal_attempt(rejection.attempt_id)
        completed = _completed_native_programs(Path(database))
        for receipt in self._journal.unacknowledged():
            if receipt.outcome == "inconclusive":
                native_reference = f"non-admission:{receipt.occurrence_key}"
            else:
                native_reference = completed.get(receipt.occurrence_key)
                if native_reference is None:
                    continue
            self._acknowledge(
                receipt,
                native_state_reference=native_reference,
            )

    def close(self) -> None:
        """The injected evaluator owns no scheduler lifecycle resources."""

    def _materialize(self, raw: bytes, destination: Path) -> Mapping[str, object]:
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArenaCandidateRejected("candidate_not_utf8") from exc
        root = Path(destination).resolve(strict=True)
        target = root.joinpath(*self.candidate_path.parts)
        try:
            target.relative_to(root)
            info = target.lstat()
        except (ValueError, OSError) as exc:
            raise ArenaCandidateRejected("candidate_path_unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ArenaCandidateRejected("candidate_path_invalid")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb", dir=target.parent, delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {
            "materializer": "vendored-shinka-candidate-file",
            "candidate_path": self.candidate_path.as_posix(),
        }

    def _run_a(self) -> _AReceipt | None:
        if not self.session.developments:
            return None
        try:
            result = self.session.run_development_tasks(
                self.development_state.fixed_a_task_ids_by_benchmark()
            )
            projection = self.session.trusted_development_projection(result)
            aggregate = _bounded_a_aggregate(projection)
            return _AReceipt(
                results=tuple(
                    (
                        benchmark_id,
                        child.development_evaluation_id,
                        child.outcome,
                    )
                    for benchmark_id, child in result.items()
                ),
                aggregate=aggregate,
            )
        except Exception:
            # A is independent development feedback and never gates B.
            return None

    def _acknowledge(
        self,
        receipt: _Receipt,
        *,
        native_state_reference: str,
    ) -> None:
        if receipt.acknowledged:
            return
        measured = receipt.outcome == "measured"
        self.session.record_native_evaluation_delivery(
            attempt_id=receipt.attempt_id,
            occurrence_key=receipt.occurrence_key,
            candidate_evaluation_id=receipt.candidate_evaluation_id,
            outcome=receipt.outcome,
        )
        if receipt.a_receipt is not None:
            try:
                self.session.record_native_development_delivery(
                    development_results={
                        benchmark_id: DevelopmentResult(
                            outcome,
                            development_evaluation_id,
                            None,
                        )
                        for benchmark_id, development_evaluation_id, outcome in (
                            receipt.a_receipt.results
                        )
                    },
                    native_request_id=receipt.occurrence_key,
                    rendered=_canonical_json(receipt.a_receipt.aggregate),
                    response_fields=(
                        ("metrics.text_feedback.a",)
                        if measured
                        else ("adapter_receipt.a",)
                    ),
                    channel=(
                        "shinka_program_sqlite"
                        if measured
                        else "shinka_receipt_journal"
                    ),
                    recipient=(
                        f"Shinka SQLite Program {native_state_reference}"
                        if measured
                        else f"Shinka non-admission {receipt.occurrence_key}"
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "Shinka auxiliary A feedback delivery failed for %s: %s",
                    receipt.occurrence_key,
                    exc,
                )
        self._journal.mark_acknowledged(
            receipt.occurrence_key,
            native_state_reference=native_state_reference,
        )


def _receipt_from_row(row: sqlite3.Row) -> _Receipt:
    try:
        attempt_id = str(row["attempt_id"])
        occurrence_key = str(row["occurrence_key"])
        evaluation_id = str(row["candidate_evaluation_id"])
        outcome = str(row["outcome"])
        terminal_payload = json.loads(row["terminal_json"])
        acknowledged_value = row["acknowledged"]
        a_payload = json.loads(row["a_receipt_json"]) if row["a_receipt_json"] else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Shinka Arena receipt is malformed") from exc
    if (
        not attempt_id
        or not occurrence_key
        or not evaluation_id
        or outcome not in {"measured", "inconclusive"}
        or acknowledged_value not in {0, 1}
        or not isinstance(terminal_payload, Mapping)
        or terminal_payload.get("attempt_id") != attempt_id
        or terminal_payload.get("occurrence_key") != occurrence_key
        or terminal_payload.get("candidate_evaluation_id") != evaluation_id
        or terminal_payload.get("outcome") != outcome
    ):
        raise RuntimeError("Shinka Arena receipt binding is invalid")
    if a_payload is None:
        a_receipt = None
    elif (
        isinstance(a_payload, Mapping)
        and set(a_payload) == {"benchmark_results", "aggregate"}
        and isinstance(a_payload.get("benchmark_results"), list)
        and bool(a_payload["benchmark_results"])
        and isinstance(a_payload.get("aggregate"), Mapping)
    ):
        a_results: list[tuple[str, str, str]] = []
        for raw_result in a_payload["benchmark_results"]:
            if not isinstance(raw_result, Mapping) or set(raw_result) != {
                "benchmark_id",
                "development_evaluation_id",
                "outcome",
            }:
                raise RuntimeError("Shinka auxiliary A receipt is malformed")
            benchmark_id = raw_result.get("benchmark_id")
            development_evaluation_id = raw_result.get("development_evaluation_id")
            a_outcome = raw_result.get("outcome")
            if (
                not isinstance(benchmark_id, str)
                or not benchmark_id
                or not isinstance(development_evaluation_id, str)
                or not development_evaluation_id
                or a_outcome not in {"measured", "inconclusive"}
            ):
                raise RuntimeError("Shinka auxiliary A receipt is malformed")
            a_results.append((benchmark_id, development_evaluation_id, str(a_outcome)))
        if len({item[0] for item in a_results}) != len(a_results):
            raise RuntimeError("Shinka auxiliary A receipt repeats a benchmark")
        a_receipt = _AReceipt(
            results=tuple(a_results),
            aggregate=dict(a_payload["aggregate"]),
        )
    else:
        raise RuntimeError("Shinka auxiliary A receipt is malformed")
    return _Receipt(
        attempt_id=attempt_id,
        occurrence_key=occurrence_key,
        candidate_evaluation_id=evaluation_id,
        outcome=outcome,
        terminal=dict(terminal_payload),
        a_receipt=a_receipt,
        acknowledged=bool(acknowledged_value),
    )


def _completed_native_programs(database: Path) -> dict[str, str]:
    if not database.is_file():
        return {}
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT id, json_extract(metadata, '$.source_job_id')
            FROM programs
            WHERE json_extract(metadata, '$.source_job_id') IS NOT NULL
              AND json_extract(metadata, '$.postprocess_side_effects_applied') = 1
              AND json_extract(metadata, '$._is_island_copy') IS NULL
            """
        ).fetchall()
    finally:
        connection.close()
    return {str(occurrence): str(program_id) for program_id, occurrence in rows}


def _terminal_selection(terminal: NativeCandidateTerminal) -> Mapping[str, object]:
    envelope = terminal.selection
    selection = envelope.get("result")
    if envelope.get("state") != "TERMINAL" or not isinstance(selection, Mapping):
        raise RuntimeError("Arena returned no terminal Shinka result")
    outcome = selection.get("outcome")
    if outcome not in {"measured", "inconclusive"}:
        raise RuntimeError("Arena returned an invalid Shinka outcome")
    return selection


def _native_measured_result(
    selection: Mapping[str, object],
    *,
    materialization_status: str,
    a_receipt: _AReceipt | None,
) -> dict[str, object]:
    score = selection.get("aggregate_score")
    if (
        not isinstance(score, (int, float))
        or isinstance(score, bool)
        or not math.isfinite(float(score))
    ):
        raise RuntimeError("measured Arena terminal lacks a Shinka fitness")
    numeric_score = float(score)
    correct = materialization_status != "candidate_rejected" and numeric_score > 0.0
    feedback: dict[str, object] = {
        "evaluation": _bounded_evaluation_feedback(selection)
    }
    if a_receipt is not None:
        feedback["a"] = dict(a_receipt.aggregate)
    return {
        "correct": {
            "correct": correct,
            "error": "" if correct else "measured candidate received zero fitness",
        },
        "metrics": {
            "combined_score": numeric_score,
            "public": {},
            "private": {},
            "text_feedback": _canonical_json(feedback),
        },
    }


def _candidate_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ValueError("Shinka candidate_path must be a non-empty relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("Shinka candidate_path must be a safe relative path")
    return path


def _read_candidate(path: Path) -> bytes:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("Shinka candidate is not a regular file")
    return path.read_bytes()


def _bounded_a_aggregate(projection: Mapping[str, object]) -> dict[str, object]:
    result = projection.get("result")
    if projection.get("state") != "TERMINAL" or not isinstance(result, Mapping):
        raise RuntimeError("Arena A operation is not terminal")
    benchmark_results = result.get("benchmark_results")
    if (
        not isinstance(benchmark_results, list)
        or not benchmark_results
        or any(not isinstance(child, Mapping) for child in benchmark_results)
    ):
        raise RuntimeError("Arena A operation lacks ordered benchmark results")
    return {
        "benchmark_results": [
            {
                key: child.get(key)
                for key in (
                    "benchmark_id",
                    "outcome",
                    "score",
                    "n_expected",
                    "n_conclusive",
                    "n_inconclusive",
                    "coverage",
                    "failed_task_briefs",
                )
            }
            for child in benchmark_results
        ]
    }


def _bounded_evaluation_feedback(selection: Mapping[str, object]) -> dict[str, object]:
    return {
        key: selection.get(key)
        for key in (
            "outcome",
            "aggregate_policy",
            "aggregate_score",
            "counts",
            "coverage",
            "score_vector",
            "summary",
        )
    }


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = ["ArenaEvaluator"]
