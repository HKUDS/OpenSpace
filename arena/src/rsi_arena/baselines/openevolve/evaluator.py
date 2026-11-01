"""OpenEvolve evaluator implementation backed by one Arena session."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

from rsi_arena.baselines._development_state import BaselineDevelopmentState
from rsi_arena.dto import DevelopmentResult
from rsi_arena.runner import ArenaSession
from rsi_arena.service import (
    ArenaCandidateRejected,
    NativeCandidateMaterialization,
)

logger = logging.getLogger(__name__)

_MEDIA_TYPE = "text/plain; charset=utf-8"
_CANDIDATE_ID = re.compile(r"[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class _NativeContext:
    candidate_id: str
    program: Mapping[str, object]
    iteration: int
    target_island: int | None
    initial: bool


@dataclass(frozen=True)
class _TerminalTicket:
    attempt_id: str
    occurrence_key: str
    candidate_evaluation_id: str
    outcome: str
    score: float | None
    feedback: str
    artifacts: Mapping[str, str]
    a_results: tuple[tuple[str, str, str], ...]
    a_feedback: str


class OpenEvolveArenaEvaluator:
    """Implement OpenEvolve's native evaluator shape using Arena authority.

    The vendored search loop sees only ``evaluate_program(code, id)`` metrics,
    pending artifacts, native context binding, recovery, and one post-persist
    callback. Arena terminals and delivery tickets never leave this adapter.
    """

    def __init__(
        self,
        session: ArenaSession,
        *,
        candidate_path: str,
        development_state: BaselineDevelopmentState,
    ) -> None:
        if not isinstance(session, ArenaSession):
            raise TypeError("OpenEvolve evaluator requires an ArenaSession")
        if not isinstance(development_state, BaselineDevelopmentState):
            raise TypeError("OpenEvolve evaluator requires durable development state")
        relative = PurePosixPath(candidate_path)
        if (
            not candidate_path
            or relative.is_absolute()
            or ".." in relative.parts
            or "." in relative.parts
        ):
            raise ValueError(
                "OpenEvolve candidate path must be a contained relative path"
            )
        self.session = session
        self.development_state = development_state
        self.candidate_path = relative
        self.journal_directory = (
            session.method_state_directory / "openevolve-evaluator-journal"
        )
        self.journal_directory.mkdir(parents=True, exist_ok=True)
        _fsync_directory(self.journal_directory.parent)
        self._contexts: dict[str, _NativeContext] = {}
        self._tickets: dict[str, _TerminalTicket] = {}
        self._pending_artifacts: dict[str, dict[str, str]] = {}
        self._candidate_locks: dict[str, threading.Lock] = {}
        self._state_lock = threading.RLock()

    def bind_native_context(
        self,
        program: Mapping[str, object],
        *,
        iteration: int,
        target_island: int | None,
        initial: bool = False,
    ) -> None:
        """Bind native resume data without exposing Arena state to the loop."""

        plain = _plain_json(program)
        if not isinstance(plain, dict):
            raise TypeError("OpenEvolve native program context must be a mapping")
        candidate_id = plain.get("id")
        code = plain.get("code")
        if (
            not isinstance(candidate_id, str)
            or _CANDIDATE_ID.fullmatch(candidate_id) is None
        ):
            raise ValueError("OpenEvolve candidate ID must be a safe opaque ID")
        if not isinstance(code, str):
            raise TypeError("OpenEvolve native program code must be text")
        if (
            isinstance(iteration, bool)
            or not isinstance(iteration, int)
            or iteration < 0
        ):
            raise ValueError("OpenEvolve iteration must be a non-negative integer")
        if target_island is not None and (
            isinstance(target_island, bool)
            or not isinstance(target_island, int)
            or target_island < 0
        ):
            raise ValueError("OpenEvolve target island must be a non-negative integer")
        context = _NativeContext(
            candidate_id=candidate_id,
            program=plain,
            iteration=iteration,
            target_island=target_island,
            initial=bool(initial),
        )
        with self._state_lock:
            existing = self._contexts.get(candidate_id)
            if existing is not None and existing != context:
                raise RuntimeError("OpenEvolve native context changed for a candidate")
            self._contexts[candidate_id] = context
            self._candidate_locks.setdefault(candidate_id, threading.Lock())

    async def evaluate_program(
        self, program_code: str, program_id: str = ""
    ) -> dict[str, float] | None:
        """Return native metrics, or ``None`` for an inconclusive terminal."""

        if not isinstance(program_code, str):
            raise TypeError("OpenEvolve candidate must be text")
        with self._state_lock:
            context = self._contexts.get(program_id)
            candidate_lock = self._candidate_locks.get(program_id)
        if context is None or candidate_lock is None:
            raise RuntimeError("OpenEvolve evaluator has no bound native context")
        return await asyncio.to_thread(
            self._evaluate_program_sync, context, program_code, candidate_lock
        )

    def get_pending_artifacts(self, program_id: str) -> dict[str, str] | None:
        """Return the native artifacts produced by the last evaluation."""

        with self._state_lock:
            artifacts = self._pending_artifacts.pop(program_id, None)
        return dict(artifacts) if artifacts else None

    async def recover_pending_evaluations(self) -> list[dict[str, object]]:
        """Replay unacknowledged journals into native-shaped evaluation records."""

        pending: list[tuple[int, Path, dict[str, object]]] = []
        for path in sorted(self.journal_directory.glob("*.json")):
            if path.name == "completion-counter.json":
                continue
            payload = _read_json_object(path)
            if payload.get("acknowledged") is True:
                continue
            terminal = payload.get("terminal")
            rejection = payload.get("rejection")
            attempt_id = payload.get("attempt_id")
            if (
                not isinstance(terminal, Mapping)
                and not isinstance(rejection, Mapping)
                and isinstance(attempt_id, str)
                and self.session.spine.attempt(attempt_id) is None
                and self.session.service.attempt_cap_exhausted
            ):
                logger.info(
                    "Leaving cap-blocked OpenEvolve journal pending for %s",
                    payload.get("candidate_id"),
                )
                continue
            order = (
                self._completion_order(payload)
                if isinstance(terminal, Mapping) or isinstance(rejection, Mapping)
                else 2**63 - 1
            )
            pending.append((order, path, payload))

        recovered: list[tuple[int, dict[str, object]]] = []
        for _order, _path, payload in sorted(
            pending, key=lambda item: (item[0], item[1].name)
        ):
            context = self._context_from_payload(payload)
            self.bind_native_context(
                context.program,
                iteration=context.iteration,
                target_island=context.target_island,
                initial=context.initial,
            )
            metrics = await self.evaluate_program(
                str(context.program["code"]), context.candidate_id
            )
            durable = _read_json_object(self._journal_path(context.candidate_id))
            native_prepare = durable.get("native_prepare")
            if native_prepare is not None and not isinstance(native_prepare, Mapping):
                raise RuntimeError("OpenEvolve native prepare journal is malformed")
            recovered.append(
                (
                    self._completion_order(durable),
                    {
                        "program": dict(context.program),
                        "iteration": context.iteration,
                        "target_island": context.target_island,
                        "initial": context.initial,
                        "metrics": dict(metrics) if metrics is not None else None,
                        "artifacts": self.get_pending_artifacts(context.candidate_id),
                        "native_prepare": (
                            dict(native_prepare) if native_prepare is not None else None
                        ),
                    },
                )
            )
        recovered.sort(key=lambda item: item[0])
        return [entry for _order, entry in recovered]

    def prepare_native_persist(
        self,
        program_id: str,
        *,
        target_island: int,
        island_generation_before: int,
        last_migration_generation_before: int,
    ) -> dict[str, object]:
        """Freeze the native baseline used to recognize a complete commit."""

        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                target_island,
                island_generation_before,
                last_migration_generation_before,
            )
        ):
            raise ValueError("OpenEvolve native persist baseline is invalid")
        path = self._journal_path(program_id)
        payload = _read_json_object(path)
        ticket = self._ticket_from_payload(payload)
        if ticket.outcome != "measured":
            raise RuntimeError(
                "only measured OpenEvolve results enter native persistence"
            )
        expected = {
            "target_island": target_island,
            "island_generation_before": island_generation_before,
            "last_migration_generation_before": last_migration_generation_before,
        }
        existing = payload.get("native_prepare")
        if existing is not None and existing != expected:
            raise RuntimeError(
                "OpenEvolve native persist baseline changed during replay"
            )
        if existing is None:
            payload["native_prepare"] = expected
            _atomic_write_json(path, payload)
        return expected

    def on_native_persisted(
        self,
        program_id: str,
        *,
        admitted: bool,
        native_state_reference: str | None,
    ) -> None:
        """Verify native durability, then perform the one idempotent Arena ACK."""

        path = self._journal_path(program_id)
        payload = _read_json_object(path)
        rejection = payload.get("rejection")
        if isinstance(rejection, Mapping):
            if admitted or native_state_reference is not None:
                raise RuntimeError(
                    "OpenEvolve rejection cannot enter native persistence"
                )
            payload["acknowledged"] = True
            _atomic_write_json(path, payload)
            return
        ticket = self._ticket_from_payload(payload)
        if (ticket.outcome == "measured") != admitted:
            raise RuntimeError(
                "OpenEvolve native admission disagrees with Arena terminal"
            )
        if admitted:
            if (
                not isinstance(native_state_reference, str)
                or not native_state_reference
            ):
                raise RuntimeError(
                    "measured OpenEvolve result requires native DB reference"
                )
            self._verify_native_database(
                payload, ticket=ticket, reference=native_state_reference
            )
            reference = native_state_reference
        else:
            if native_state_reference is not None:
                raise RuntimeError(
                    "inconclusive OpenEvolve result cannot name native DB state"
                )
            reference = f"journal:{path.resolve(strict=True).as_posix()}"

        native_commit = payload.get("native_commit")
        expected_commit = {"admitted": admitted, "reference": reference}
        if native_commit is not None and native_commit != expected_commit:
            raise RuntimeError("OpenEvolve native commit changed during replay")
        if native_commit is None:
            payload["native_commit"] = expected_commit
            _atomic_write_json(path, payload)
        if payload.get("acknowledged") is True:
            return

        if ticket.a_results:
            try:
                self.session.record_native_development_delivery(
                    development_results={
                        benchmark_id: DevelopmentResult(
                            outcome,
                            development_evaluation_id,
                            None,
                        )
                        for benchmark_id, development_evaluation_id, outcome in (
                            ticket.a_results
                        )
                    },
                    native_request_id=ticket.occurrence_key,
                    rendered=ticket.a_feedback,
                    response_fields=("a_development_feedback",),
                    channel="openevolve_selected_parent_artifact",
                    recipient=f"OpenEvolve native state {reference}",
                )
            except Exception as exc:
                logger.warning("OpenEvolve A delivery record failed: %s", exc)
        self.session.record_native_evaluation_delivery(
            attempt_id=ticket.attempt_id,
            occurrence_key=ticket.occurrence_key,
            candidate_evaluation_id=ticket.candidate_evaluation_id,
            outcome=ticket.outcome,
        )
        payload = _read_json_object(path)
        payload["acknowledged"] = True
        _atomic_write_json(path, payload)

    def _evaluate_program_sync(
        self,
        context: _NativeContext,
        program_code: str,
        candidate_lock: threading.Lock,
    ) -> dict[str, float] | None:
        with candidate_lock:
            payload = self._ensure_pending_journal(context, program_code)
            if isinstance(payload.get("rejection"), Mapping):
                attempt_id = str(payload["attempt_id"])
                if self.session.spine.attempt(attempt_id) is not None:
                    self.session.seal_attempt(attempt_id)
                return None
            if "terminal" not in payload:
                try:
                    ticket = self._submit_to_arena(context, program_code)
                except ArenaCandidateRejected as exc:
                    payload["rejection"] = {
                        "completion_order": self._allocate_completion_order(),
                        "kind": exc.kind,
                    }
                    _atomic_write_json(
                        self._journal_path(context.candidate_id), payload
                    )
                    self.session.transition_attempt(str(payload["attempt_id"]), "skip")
                    return None
                completion_order = self._allocate_completion_order()
                payload["terminal"] = _ticket_payload(
                    ticket, completion_order=completion_order
                )
                _atomic_write_json(self._journal_path(context.candidate_id), payload)
            else:
                ticket = self._ticket_from_payload(payload)
            self._stage_native_result(context.candidate_id, ticket)
            if ticket.outcome == "inconclusive":
                return None
            return {"combined_score": float(ticket.score)}

    def _submit_to_arena(
        self, context: _NativeContext, program_code: str
    ) -> _TerminalTicket:
        occurrence_key = f"openevolve-{context.candidate_id}"
        attempt_id = f"attempt-openevolve-{context.candidate_id}"
        known_attempt = self.session.spine.attempt(attempt_id)
        if known_attempt is None:
            slots = self.development_state.slots_for_attempt(attempt_id)
            self.session.start_attempt(attempt_id, kind="single", benchmark_slots=slots)
        else:
            self.session.start_attempt(attempt_id)
        reservation = self.session.reserve_native_candidate(
            program_code,
            attempt_id=attempt_id,
            occurrence_key=occurrence_key,
            media_type=_MEDIA_TYPE,
            role="candidate",
        )
        materialized = self.session.materialize_native_candidate(
            reservation, materialize=self._materialize
        )

        a_results: tuple[tuple[str, str, str], ...] = ()
        a_feedback = ""
        if isinstance(materialized, NativeCandidateMaterialization):
            if reservation.terminal_result is None and self.session.developments:
                try:
                    development = self.session.run_development_tasks(
                        self.development_state.fixed_a_task_ids_by_benchmark()
                    )
                    projection = self.session.trusted_development_projection(
                        development
                    )
                    a_results = tuple(
                        (
                            benchmark_id,
                            result.development_evaluation_id,
                            result.outcome,
                        )
                        for benchmark_id, result in development.items()
                    )
                    a_feedback = _bounded_feedback(projection)
                except Exception as exc:
                    logger.warning("OpenEvolve A feedback unavailable: %s", exc)
            self.session.transition_attempt(attempt_id, "enter")
            terminal = self.session.evaluate_native_candidate(materialized)
        else:
            terminal = materialized
        self.session.seal_attempt(attempt_id)

        envelope = terminal.selection
        result = envelope.get("result")
        if envelope.get("state") != "TERMINAL" or not isinstance(result, Mapping):
            raise RuntimeError("Arena returned a malformed OpenEvolve terminal")
        outcome = str(result.get("outcome"))
        if outcome not in {"measured", "inconclusive"}:
            raise RuntimeError("Arena returned an unknown OpenEvolve outcome")
        score_value = result.get("aggregate_score")
        score: float | None = None
        if outcome == "measured":
            if (
                isinstance(score_value, bool)
                or not isinstance(score_value, (int, float))
                or not math.isfinite(float(score_value))
            ):
                raise RuntimeError("Arena measured terminal lacks a finite score")
            score = float(score_value)
        elif score_value is not None:
            raise RuntimeError(
                "Arena inconclusive terminal unexpectedly carries a score"
            )
        handle = result.get("candidate_evaluation_id")
        if not isinstance(handle, str) or not handle:
            raise RuntimeError("Arena terminal lacks a delivery handle")
        feedback = _bounded_feedback(
            {
                "outcome": outcome,
                "aggregate_score": score,
                "counts": result.get("counts"),
                "coverage": result.get("coverage"),
                "score_vector": result.get("score_vector"),
                "summary": result.get("summary"),
            }
        )
        artifacts: dict[str, str] = {}
        if a_feedback:
            artifacts["a_development_feedback"] = a_feedback
        if outcome == "measured":
            artifacts["evaluation_feedback"] = feedback
        return _TerminalTicket(
            attempt_id=attempt_id,
            occurrence_key=occurrence_key,
            candidate_evaluation_id=handle,
            outcome=outcome,
            score=score,
            feedback=feedback,
            artifacts=artifacts,
            a_results=a_results,
            a_feedback=a_feedback,
        )

    def _ensure_pending_journal(
        self, context: _NativeContext, program_code: str
    ) -> dict[str, object]:
        if program_code != context.program.get("code"):
            raise RuntimeError("OpenEvolve native code changed after context binding")
        path = self._journal_path(context.candidate_id)
        expected = {
            "candidate_id": context.candidate_id,
            "raw": program_code,
            "raw_sha256": hashlib.sha256(program_code.encode("utf-8")).hexdigest(),
            "iteration": context.iteration,
            "target_island": context.target_island,
            "occurrence_key": f"openevolve-{context.candidate_id}",
            "attempt_id": f"attempt-openevolve-{context.candidate_id}",
            "initial": context.initial,
            "program": dict(context.program),
        }
        if not path.exists():
            _atomic_write_json(path, expected)
            return expected
        payload = _read_json_object(path)
        for key, value in expected.items():
            if payload.get(key) != value:
                raise RuntimeError(f"OpenEvolve pending journal changed field {key}")
        return payload

    def _context_from_payload(self, payload: Mapping[str, object]) -> _NativeContext:
        program = payload.get("program")
        if not isinstance(program, Mapping):
            raise RuntimeError("OpenEvolve pending journal lacks native program")
        candidate_id = payload.get("candidate_id")
        iteration = payload.get("iteration")
        target_island = payload.get("target_island")
        initial = payload.get("initial")
        if not isinstance(candidate_id, str) or program.get("id") != candidate_id:
            raise RuntimeError(
                "OpenEvolve pending journal has inconsistent candidate ID"
            )
        if isinstance(iteration, bool) or not isinstance(iteration, int):
            raise RuntimeError("OpenEvolve pending journal has invalid iteration")
        if target_island is not None and (
            isinstance(target_island, bool) or not isinstance(target_island, int)
        ):
            raise RuntimeError("OpenEvolve pending journal has invalid island")
        if not isinstance(initial, bool):
            raise RuntimeError("OpenEvolve pending journal lacks initial marker")
        return _NativeContext(
            candidate_id=candidate_id,
            program=dict(program),
            iteration=iteration,
            target_island=target_island,
            initial=initial,
        )

    def _ticket_from_payload(self, payload: Mapping[str, object]) -> _TerminalTicket:
        terminal = payload.get("terminal")
        if not isinstance(terminal, Mapping):
            raise RuntimeError("OpenEvolve pending journal has no terminal")
        handle = terminal.get("candidate_evaluation_id")
        feedback = terminal.get("feedback")
        artifacts = terminal.get("artifacts", {})
        if not isinstance(handle, str) or not handle:
            raise RuntimeError("OpenEvolve pending journal has invalid delivery handle")
        if not isinstance(feedback, str) or not isinstance(artifacts, Mapping):
            raise RuntimeError("OpenEvolve pending journal feedback is malformed")
        raw_a_results = terminal.get("a_results", [])
        if not isinstance(raw_a_results, list):
            raise RuntimeError("OpenEvolve pending journal A results are malformed")
        a_results: list[tuple[str, str, str]] = []
        for raw_result in raw_a_results:
            if not isinstance(raw_result, Mapping) or set(raw_result) != {
                "benchmark_id",
                "development_evaluation_id",
                "outcome",
            }:
                raise RuntimeError("OpenEvolve pending journal A results are malformed")
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
                raise RuntimeError("OpenEvolve pending journal A results are malformed")
            a_results.append((benchmark_id, development_evaluation_id, str(a_outcome)))
        if len({item[0] for item in a_results}) != len(a_results):
            raise RuntimeError("OpenEvolve pending journal repeats an A benchmark")
        try:
            ticket = _TerminalTicket(
                attempt_id=str(payload["attempt_id"]),
                occurrence_key=str(payload["occurrence_key"]),
                candidate_evaluation_id=handle,
                outcome=str(terminal["outcome"]),
                score=(
                    float(terminal["score"])
                    if terminal.get("score") is not None
                    else None
                ),
                feedback=feedback,
                artifacts={str(key): str(value) for key, value in artifacts.items()},
                a_results=tuple(a_results),
                a_feedback=str(terminal.get("a_feedback", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "OpenEvolve pending journal terminal is malformed"
            ) from exc
        if ticket.outcome not in {"measured", "inconclusive"}:
            raise RuntimeError("OpenEvolve pending journal outcome is invalid")
        if terminal.get("attempt_id") != ticket.attempt_id:
            raise RuntimeError("OpenEvolve pending journal attempt is misbound")
        if ticket.outcome == "measured" and (
            ticket.score is None or not math.isfinite(ticket.score)
        ):
            raise RuntimeError("OpenEvolve measured journal lacks a score")
        if ticket.outcome == "inconclusive" and ticket.score is not None:
            raise RuntimeError("OpenEvolve inconclusive journal carries a score")
        return ticket

    def _stage_native_result(self, candidate_id: str, ticket: _TerminalTicket) -> None:
        with self._state_lock:
            self._tickets[candidate_id] = ticket
            if ticket.outcome == "measured" and ticket.artifacts:
                self._pending_artifacts[candidate_id] = dict(ticket.artifacts)

    def _allocate_completion_order(self) -> int:
        counter_path = self.journal_directory / "completion-counter.json"
        with self._state_lock:
            if counter_path.exists():
                counter = _read_json_object(counter_path).get("next")
                if (
                    isinstance(counter, bool)
                    or not isinstance(counter, int)
                    or counter < 1
                ):
                    raise RuntimeError("OpenEvolve completion counter is malformed")
            else:
                counter = 1
                for path in self.journal_directory.glob("*.json"):
                    if path == counter_path:
                        continue
                    payload = _read_json_object(path)
                    terminal = payload.get("terminal")
                    rejection = payload.get("rejection")
                    if isinstance(terminal, Mapping) or isinstance(rejection, Mapping):
                        counter = max(
                            counter,
                            self._completion_order(payload) + 1,
                        )
            _atomic_write_json(counter_path, {"next": counter + 1})
            return counter

    def _completion_order(self, payload: Mapping[str, object]) -> int:
        completion = payload.get("terminal")
        if not isinstance(completion, Mapping):
            completion = payload.get("rejection")
        if not isinstance(completion, Mapping):
            raise RuntimeError(
                "OpenEvolve pending journal has no terminal or rejection"
            )
        order = completion.get("completion_order")
        if isinstance(order, bool) or not isinstance(order, int) or order < 1:
            raise RuntimeError("OpenEvolve terminal completion order is malformed")
        return order

    def _verify_native_database(
        self,
        payload: Mapping[str, object],
        *,
        ticket: _TerminalTicket,
        reference: str,
    ) -> None:
        prefix = "database:"
        marker = "#program="
        if not reference.startswith(prefix) or marker not in reference:
            raise RuntimeError("OpenEvolve native DB reference is malformed")
        root_text, referenced_id = reference[len(prefix) :].rsplit(marker, 1)
        candidate_id = payload.get("candidate_id")
        if referenced_id != candidate_id:
            raise RuntimeError("OpenEvolve native DB reference names another candidate")
        root = Path(root_text).resolve(strict=True)
        program_path = (root / "programs" / f"{referenced_id}.json").resolve(
            strict=True
        )
        if program_path.parent != (root / "programs").resolve(strict=True):
            raise RuntimeError("OpenEvolve native DB program escaped its database")
        native = _read_json_object(program_path)
        program = payload.get("program")
        if not isinstance(program, Mapping):
            raise RuntimeError("OpenEvolve journal lost its native program")
        if native.get("id") != candidate_id or native.get("code") != program.get(
            "code"
        ):
            raise RuntimeError(
                "OpenEvolve native DB does not contain the journaled candidate"
            )
        metrics = native.get("metrics")
        if (
            not isinstance(metrics, Mapping)
            or metrics.get("combined_score") != ticket.score
        ):
            raise RuntimeError(
                "OpenEvolve native DB score disagrees with Arena terminal"
            )
        prepare = payload.get("native_prepare")
        if not isinstance(prepare, Mapping):
            raise RuntimeError("OpenEvolve journal lacks native persist baseline")
        target_island = prepare.get("target_island")
        generation_before = prepare.get("island_generation_before")
        migration_before = prepare.get("last_migration_generation_before")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (target_island, generation_before, migration_before)
        ):
            raise RuntimeError("OpenEvolve native persist baseline is malformed")
        metadata = _read_json_object(root / "metadata.json")
        generations = metadata.get("island_generations")
        migration = metadata.get("last_migration_generation")
        if (
            not isinstance(generations, list)
            or target_island >= len(generations)
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in generations
            )
            or isinstance(migration, bool)
            or not isinstance(migration, int)
            or migration < migration_before
        ):
            raise RuntimeError("OpenEvolve native generation metadata is malformed")
        if payload.get("initial") is True:
            islands = metadata.get("islands")
            if (
                not isinstance(islands, list)
                or target_island >= len(islands)
                or not isinstance(islands[target_island], list)
                or candidate_id not in islands[target_island]
                or metadata.get("best_program_id") != candidate_id
            ):
                raise RuntimeError("OpenEvolve initial native admission is incomplete")
        elif generations[target_island] != generation_before + 1:
            raise RuntimeError("OpenEvolve child native admission is incomplete")

    def _journal_path(self, candidate_id: str) -> Path:
        digest = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()
        return self.journal_directory / f"{digest}.json"

    def _materialize(self, raw: bytes, workspace: Path) -> Mapping[str, object]:
        root = workspace.resolve(strict=True)
        target = root.joinpath(*self.candidate_path.parts)
        parent = target.parent.resolve(strict=True)
        try:
            parent.relative_to(root)
        except ValueError as exc:
            raise ArenaCandidateRejected("candidate_path_escape") from exc
        if target.exists() or target.is_symlink():
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ArenaCandidateRejected("candidate_path_not_regular")
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        return {
            "candidate_path": self.candidate_path.as_posix(),
            "native_candidate": "OpenEvolve program text",
        }


def _ticket_payload(
    ticket: _TerminalTicket, *, completion_order: int
) -> dict[str, object]:
    return {
        "completion_order": completion_order,
        "attempt_id": ticket.attempt_id,
        "candidate_evaluation_id": ticket.candidate_evaluation_id,
        "outcome": ticket.outcome,
        "score": ticket.score,
        "feedback": ticket.feedback,
        "artifacts": dict(ticket.artifacts),
        "a_results": [
            {
                "benchmark_id": benchmark_id,
                "development_evaluation_id": development_evaluation_id,
                "outcome": outcome,
            }
            for benchmark_id, development_evaluation_id, outcome in ticket.a_results
        ],
        "a_feedback": ticket.a_feedback,
    }


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"OpenEvolve journal is unreadable: {path.name}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"OpenEvolve journal is not an object: {path.name}")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _bounded_feedback(payload: Mapping[str, object]) -> str:
    return json.dumps(
        _plain_json(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )[:8192]


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


__all__ = ["OpenEvolveArenaEvaluator"]
