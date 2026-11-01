"""The single run-scoped Arena façade over development and campaign owners."""

from __future__ import annotations

import json
import math
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

from rsi_arena.campaign_evaluation import CampaignController
from rsi_arena.evaluation_gateway import GatewayCandidate
from rsi_arena.manifests import SampleSlot
from rsi_arena.run_layout import (
    AttemptLimit,
    AttemptRecord,
    CampaignEvaluationResult,
    CampaignOccurrenceState,
    CandidateRecord,
    RunLayout,
    RunLayoutError,
    RunValidationError,
)

from .development import DevelopmentOperations
from .dto import (
    ArenaBenchmarkScore,
    ArenaProtocolError,
    ArenaRequest,
    ArenaResponse,
    ArenaSubmitResult,
    DevelopmentBatchResult,
    DevelopmentResult,
    bounded_opaque_id,
)

_CANDIDATE_RUNTIME_FAILURE_KEY = "rsi_arena_candidate_runtime_failure"
_CANDIDATE_RUNTIME_MESSAGE_MAX_CHARS = 500
_PUBLIC_RUNTIME_FEEDBACK_MAX_CHARS = 1_200


def _candidate_runtime_marker_from_provenance(
    provenance: object,
) -> tuple[str, str | None] | None:
    """Read the narrow backend-validated marker without exposing tracebacks."""

    if not isinstance(provenance, Mapping):
        return None
    marker = provenance.get(_CANDIDATE_RUNTIME_FAILURE_KEY)
    if not isinstance(marker, Mapping) or set(marker) not in (
        {"kind", "exception_type"},
        {"kind", "exception_type", "message"},
    ):
        return None
    exception_type = marker.get("exception_type")
    if not (
        marker.get("kind") == "candidate_runtime_failure"
        and isinstance(exception_type, str)
        and exception_type.isidentifier()
        and len(exception_type) <= 128
    ):
        return None
    message = marker.get("message")
    if message is not None:
        if (
            not isinstance(message, str)
            or not message
            or len(message) > _CANDIDATE_RUNTIME_MESSAGE_MAX_CHARS
            or message != " ".join(message.split())
        ):
            return None
        if "Traceback (most recent call last):" in message:
            message = None
    return exception_type, message


def _candidate_runtime_feedback(result: CampaignEvaluationResult) -> str | None:
    failures: Counter[tuple[str, str | None]] = Counter()
    for child in result.benchmark_results:
        for outcome in child.task_outcomes:
            if outcome.get("outcome") != "measured" or outcome.get("score") != 0.0:
                continue
            marker = _candidate_runtime_marker_from_provenance(
                outcome.get("provenance")
            )
            if marker is not None:
                failures[marker] += 1
    total = sum(failures.values())
    if not total:
        return None

    prefix = (
        f"candidate runtime failure in {total}/{result.n_expected} task occurrences: "
    )
    details: list[str] = []
    omitted = 0
    for (exception_type, message), count in failures.items():
        detail = f"{count}x {exception_type}"
        if message is not None:
            detail = f"{detail}: {message}"
        candidate = prefix + "; ".join((*details, detail))
        if len(candidate) > _PUBLIC_RUNTIME_FEEDBACK_MAX_CHARS:
            omitted += 1
            continue
        details.append(detail)
    if omitted:
        suffix = f"; {omitted} additional distinct failure(s) omitted"
        if len(prefix + "; ".join(details) + suffix) <= (
            _PUBLIC_RUNTIME_FEEDBACK_MAX_CHARS
        ):
            details.append(f"{omitted} additional distinct failure(s) omitted")
    return prefix + "; ".join(details)


class CampaignSubmissionOperations(Protocol):
    """The subset already implemented by :class:`CampaignController`."""

    @property
    def layout(self) -> RunLayout: ...

    @property
    def benchmark_ids(self) -> tuple[str, ...]: ...

    @property
    def candidate_evaluations_used(self) -> int: ...

    @property
    def attempts_used(self) -> int: ...

    @property
    def all_attempts_terminal(self) -> bool: ...

    def start_attempt(
        self,
        attempt_id: str,
        *,
        max_attempts: int,
        kind: str | None = None,
        benchmark_slots: Mapping[str, Sequence[SampleSlot]] | None = None,
    ) -> AttemptRecord: ...

    def transition_attempt(self, attempt_id: str, decision: str) -> AttemptRecord: ...

    def seal_attempt(self, attempt_id: str) -> AttemptRecord: ...

    def attempt(self, attempt_id: str) -> AttemptRecord | None: ...

    def candidate_state(self, candidate_id: str) -> CampaignOccurrenceState: ...

    @property
    def campaign_aggregate_kind(self) -> str | None: ...

    @property
    def delivered_terminal_ids(self) -> frozenset[str]: ...

    def evaluate_candidate(
        self,
        candidate_workspace: Path,
        *,
        native_candidate_id: str,
        attempt_id: str,
        role: str,
        parent_candidate_id: str | None = None,
    ) -> CampaignEvaluationResult: ...

    def prepare_native_workspace(
        self,
        candidate_workspace: Path,
        *,
        occurrence_key: str,
        attempt_id: str,
        role: str,
        parent_candidate_id: str | None = None,
    ) -> tuple[CandidateRecord, GatewayCandidate]: ...

    def evaluate_prepared_candidate(
        self,
        candidate: CandidateRecord,
        issued: GatewayCandidate,
    ) -> CampaignEvaluationResult: ...

    def result_for_occurrence(
        self, occurrence_key: str
    ) -> CampaignOccurrenceState | None: ...

    def record_evaluation_delivered(self, result: CampaignEvaluationResult) -> None: ...

    def run_development_tasks(
        self,
        benchmark_tasks: Mapping[str, Sequence[str]],
        *,
        candidate_workspace: Path | None = None,
    ) -> Mapping[str, DevelopmentResult]: ...


class ArenaUnknownAttempt(LookupError):
    """An attempt operation named no attempt owned by this run."""


class ArenaUnknownOccurrence(LookupError):
    """A result operation named no occurrence owned by this run."""


class ArenaCandidateRejected(ValueError):
    """A trusted native materializer rejected candidate-controlled bytes."""

    def __init__(self, kind: str = "native_candidate_invalid") -> None:
        self.kind = bounded_opaque_id(kind, "candidate rejection kind")
        super().__init__("native candidate could not be materialized")


@dataclass(frozen=True)
class NativeCandidateReservation:
    """One baseline-owned raw occurrence before Arena candidate admission."""

    occurrence_key: str
    attempt_id: str
    role: str
    raw: bytes
    terminal_result: CampaignEvaluationResult | None
    parent_candidate_id: str | None = None


@dataclass(frozen=True)
class NativeCandidateMaterialization:
    """A reserved occurrence with one durable canonical Arena candidate."""

    reservation: NativeCandidateReservation
    provenance: Mapping[str, object]
    workspace: Path
    candidate: CandidateRecord
    issued: GatewayCandidate


@dataclass(frozen=True)
class NativeCandidateTerminal:
    """Internal native evaluation terminal returned to a trusted MethodRunner."""

    reservation: NativeCandidateReservation
    result: CampaignEvaluationResult
    selection: Mapping[str, object]
    materialization_status: str


@dataclass
class _SubmitFlight:
    """One active public submit shared by concurrent identity retries."""

    completed: threading.Event
    candidate_workspace: Path
    role: str
    parent_candidate_id: str | None
    result: CampaignEvaluationResult | None = None
    error: BaseException | None = None


def _restored_evaluation_delivery_ids(
    campaign_controller: CampaignController | CampaignSubmissionOperations,
) -> set[str]:
    """Restore delivered terminal IDs from recorder-validated receipts."""

    delivered = campaign_controller.delivered_terminal_ids
    if not isinstance(delivered, frozenset) or any(
        not isinstance(terminal_id, str) or not terminal_id for terminal_id in delivered
    ):
        raise RunValidationError("evaluation delivery receipt index is invalid")
    return set(delivered)


def project_campaign_result(
    occurrence_key: str,
    result: CampaignEvaluationResult,
) -> ArenaSubmitResult:
    """Project the one canonical campaign terminal for public/native delivery."""

    scores = tuple(
        ArenaBenchmarkScore(
            benchmark_id=child.benchmark_id,
            score=child.score,
            n_expected=child.n_expected,
            n_conclusive=child.n_conclusive,
            n_inconclusive=child.n_inconclusive,
            coverage=child.coverage,
        )
        for child in result.benchmark_results
    )
    summary = (
        f"{result.n_conclusive}/{result.n_expected} expected task occurrences "
        "produced trusted measurements"
    )
    for child in result.benchmark_results:
        for outcome in child.task_outcomes:
            provenance = outcome.get("provenance")
            diagnostic = (
                provenance.get("public_diagnostic")
                if isinstance(provenance, Mapping)
                else None
            )
            if isinstance(diagnostic, str) and diagnostic:
                summary = f"{summary}; candidate workspace rejected: {diagnostic}"
                break
        else:
            continue
        break
    runtime_feedback = _candidate_runtime_feedback(result)
    if runtime_feedback is not None:
        summary = f"{summary}; {runtime_feedback}"
    return ArenaSubmitResult(
        outcome=result.outcome,
        attempt_id=result.attempt_id,
        occurrence_key=occurrence_key,
        candidate_id=result.candidate_id,
        candidate_evaluation_id=result.candidate_evaluation_id,
        role=result.candidate_role,
        score_vector=scores,
        aggregate_policy=result.aggregate_policy,
        aggregate_score=result.aggregate_score,
        summary=summary,
    )


class ArenaService:
    """Validate and route public development/evaluation for exactly one run."""

    def __init__(
        self,
        *,
        layout: RunLayout,
        developments: Mapping[str, DevelopmentOperations],
        campaign_controller: CampaignController | CampaignSubmissionOperations,
        attempt_cap: int,
        candidate_root: Path | None = None,
        development_evidence_roots: Mapping[str, Path],
        public_workspace: Path | None = None,
        public_development_evidence_roots: Mapping[str, Path] | None = None,
    ) -> None:
        if (
            isinstance(attempt_cap, bool)
            or not isinstance(attempt_cap, int)
            or attempt_cap < 1
        ):
            raise ValueError("Arena attempt cap must be positive")
        if campaign_controller.layout != layout:
            raise RunValidationError("Arena controller belongs to another run tree")
        benchmark_ids = tuple(campaign_controller.benchmark_ids)
        if (
            not benchmark_ids
            or tuple(developments) != benchmark_ids
            or tuple(development_evidence_roots) != benchmark_ids
        ):
            raise RunValidationError(
                "Arena development owners do not match the campaign benchmark order"
            )
        if any(owner.layout != layout for owner in developments.values()):
            raise RunValidationError("Arena development belongs to another run tree")
        if public_development_evidence_roots is not None and (
            tuple(public_development_evidence_roots) != benchmark_ids
        ):
            raise RunValidationError("Arena public development evidence order changed")
        self.layout = layout
        self.benchmark_ids = benchmark_ids
        self.developments = MappingProxyType(dict(developments))
        self.campaign_controller = campaign_controller
        self.attempt_cap = attempt_cap
        workspace = layout.assert_workspace_isolated(layout.method_root / "workspace")
        self.method_workspace = workspace
        self.workspace = layout.assert_workspace_isolated(candidate_root or workspace)
        try:
            self.workspace.relative_to(workspace)
        except ValueError as exc:
            raise RunLayoutError(
                "Arena candidate root must be contained by the workspace"
            ) from exc
        self.public_workspace = Path(public_workspace or self.method_workspace)
        if (
            not self.public_workspace.is_absolute()
            or ".." in self.public_workspace.parts
        ):
            raise RunLayoutError("Arena public workspace path is invalid")
        evidence_roots: dict[str, Path] = {}
        public_evidence_roots: dict[str, Path] = {}
        for benchmark_id in benchmark_ids:
            evidence_root = Path(development_evidence_roots[benchmark_id]).resolve(
                strict=True
            )
            public_root = Path(
                public_development_evidence_roots[benchmark_id]
                if public_development_evidence_roots is not None
                else evidence_root
            )
            if not public_root.is_absolute() or ".." in public_root.parts:
                raise RunLayoutError(
                    "Arena public development evidence path is invalid"
                )
            if not evidence_root.is_dir():
                raise RunLayoutError(
                    "Arena development evidence root is not a directory"
                )
            method_root = layout.method_root.resolve(strict=True)
            try:
                evidence_root.relative_to(method_root)
            except ValueError as exc:
                raise RunLayoutError(
                    "development evidence path is outside run-private method state"
                ) from exc
            if evidence_root == method_root:
                raise RunLayoutError(
                    "Arena development evidence root must be operation-scoped"
                )
            evidence_roots[benchmark_id] = evidence_root
            public_evidence_roots[benchmark_id] = public_root
        self.development_evidence_roots = MappingProxyType(evidence_roots)
        self.public_development_evidence_roots = MappingProxyType(public_evidence_roots)
        self.last_submission_result: dict[str, object] | None = None
        self._last_trusted_submission_result: dict[str, object] | None = None
        self._delivered_terminal_ids = _restored_evaluation_delivery_ids(
            campaign_controller
        )
        self._delivery_state_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._attempt_condition = threading.Condition()
        self._submit_flights_lock = threading.Lock()
        self._submit_flights: dict[tuple[str, str], _SubmitFlight] = {}

    def handle_request(self, request: ArenaRequest) -> ArenaResponse:
        """Compute one response without claiming that a transport delivered it."""

        try:
            payload = self._dispatch(request)
            if request.op == "context":
                return ArenaResponse.success(
                    request.request_id,
                    state="TERMINAL",
                    result=payload,
                )
            if set(payload) != {"state", "result"}:
                raise RunValidationError("Arena success envelope is invalid")
            state = payload.get("state")
            result = payload.get("result")
            return ArenaResponse.success(
                request.request_id,
                state=str(state),
                result=result if isinstance(result, Mapping) else None,
            )
        except ArenaProtocolError as exc:
            return ArenaResponse.failure(
                request.request_id,
                kind="protocol_error",
                summary=str(exc),
            )
        except AttemptLimit:
            return ArenaResponse.failure(
                request.request_id,
                kind="budget_exhausted",
                summary="attempt limit reached",
            )
        except ArenaUnknownAttempt:
            return ArenaResponse.failure(
                request.request_id,
                kind="unknown_attempt",
                summary="attempt ID is not known to this run",
            )
        except ArenaUnknownOccurrence:
            return ArenaResponse.failure(
                request.request_id,
                kind="unknown_occurrence",
                summary="occurrence key is not known to this run",
            )
        except RunValidationError:
            return ArenaResponse.failure(
                request.request_id,
                kind="identity_conflict",
                summary="request conflicts with durable run identity",
            )
        except Exception:
            return ArenaResponse.failure(
                request.request_id,
                kind="internal_error",
                summary="Arena operation failed",
            )

    def record_response_published(
        self, request: ArenaRequest, response: ArenaResponse
    ) -> None:
        """Record an exact terminal projection after its transport publishes it."""

        if request.op in {"development.run", "development.run_set"}:
            self._record_development_response_published(response)
            return

        if (
            request.op not in {"submit", "result"}
            or not response.ok
            or response.state != "TERMINAL"
            or response.result is None
        ):
            return
        occurrence_key = bounded_opaque_id(
            request.payload.get("occurrence_key"), "occurrence_key"
        )
        state = self.campaign_controller.result_for_occurrence(occurrence_key)
        result = state.terminal_result if state is not None else None
        if result is None:
            raise RunValidationError(
                "published candidate response lacks a terminal owner"
            )
        public = project_campaign_result(occurrence_key, result).as_dict()
        if {"state": response.state, "result": dict(response.result)} != public:
            raise RunValidationError("published candidate response changed")
        deliveries = [(occurrence_key, result, public)]
        for occurrence_key, result, public in deliveries:
            self._observe_campaign_result(result)
            record_delivery = getattr(
                self.campaign_controller, "record_evaluation_delivered", None
            )
            if not callable(record_delivery):
                raise RunValidationError("evaluation delivery lacks its recorder owner")
            record_delivery(result)
            with self._delivery_state_lock:
                self._delivered_terminal_ids.add(result.candidate_evaluation_id)
                self.last_submission_result = public

    def _record_development_response_published(self, response: ArenaResponse) -> None:
        if not response.ok or response.state != "TERMINAL" or response.result is None:
            return
        terminal = dict(response.result)
        public = {"state": response.state, "result": terminal}
        if set(terminal) != {"benchmark_results"}:
            raise RunValidationError("published development response fields changed")
        raw_results = terminal["benchmark_results"]
        if not isinstance(raw_results, list) or not raw_results:
            raise RunValidationError("published development response order changed")
        raw_benchmark_ids = tuple(
            str(raw_result.get("benchmark_id", ""))
            if isinstance(raw_result, Mapping)
            else ""
            for raw_result in raw_results
        )
        expected_ids = tuple(
            benchmark_id
            for benchmark_id in self.benchmark_ids
            if benchmark_id in raw_benchmark_ids
        )
        if raw_benchmark_ids != expected_ids:
            raise RunValidationError("published development response order changed")
        for benchmark_id, raw_result in zip(expected_ids, raw_results, strict=True):
            if (
                not isinstance(raw_result, Mapping)
                or set(raw_result)
                != {
                    "benchmark_id",
                    "outcome",
                    "development_evaluation_id",
                    "evidence_path",
                }
                or raw_result.get("benchmark_id") != benchmark_id
            ):
                raise RunValidationError(
                    "published development response benchmark binding changed"
                )
            raw_evidence = raw_result["evidence_path"]
            relative: Path | None = None
            if raw_evidence is not None:
                public_evidence = Path(str(raw_evidence))
                try:
                    relative = public_evidence.relative_to(
                        self.public_development_evidence_roots[benchmark_id]
                    )
                except ValueError as exc:
                    raise RunValidationError(
                        "published development response escaped its owner"
                    ) from exc
            trusted = DevelopmentResult(
                outcome=str(raw_result["outcome"]),
                development_evaluation_id=str(raw_result["development_evaluation_id"]),
                evidence_path=(
                    str(self.development_evidence_roots[benchmark_id] / relative)
                    if relative is not None
                    else None
                ),
            )
            if relative is not None and (
                not relative.parts
                or ".." in relative.parts
                or relative.name != trusted.development_evaluation_id
            ):
                raise RunValidationError(
                    "published development response changed evidence identity"
                )
            record_delivery = getattr(
                self.developments[benchmark_id],
                "record_public_response_delivered",
                None,
            )
            if callable(record_delivery):
                record_delivery(
                    trusted,
                    rendered=json.dumps(public, sort_keys=True, ensure_ascii=False),
                    response_fields=tuple(terminal),
                    channel="arena_client_response",
                    recipient="method_arena_client",
                )

    @property
    def terminal_response_delivered(self) -> bool:
        """Whether this service published any evaluation terminal response."""

        with self._delivery_state_lock:
            return bool(self._delivered_terminal_ids)

    @property
    def trusted_last_submission_result(self) -> Mapping[str, object] | None:
        """Latest evaluation terminal projection computed on the trusted side."""

        with self._state_lock:
            result = self._last_trusted_submission_result
            return dict(result) if result is not None else None

    @property
    def attempt_cap_exhausted(self) -> bool:
        """Whether the durable cap is consumed and every started attempt ended."""

        return (
            self.campaign_controller.attempts_used >= self.attempt_cap
            and self.campaign_controller.all_attempts_terminal
        )

    def start_attempt(
        self,
        attempt_id: str,
        *,
        kind: str | None = None,
        benchmark_slots: Mapping[str, Sequence[SampleSlot]] | None = None,
    ) -> AttemptRecord:
        bound_attempt_id = bounded_opaque_id(attempt_id, "attempt_id")
        if benchmark_slots is None:
            if self.campaign_controller.attempt(bound_attempt_id) is None:
                raise ArenaUnknownAttempt
        return self.campaign_controller.start_attempt(
            bound_attempt_id,
            max_attempts=self.attempt_cap,
            kind=kind,
            benchmark_slots=benchmark_slots,
        )

    def transition_attempt(self, attempt_id: str, decision: str) -> AttemptRecord:
        record = self.campaign_controller.transition_attempt(
            bounded_opaque_id(attempt_id, "attempt_id"), decision
        )
        with self._attempt_condition:
            self._attempt_condition.notify_all()
        return record

    def seal_attempt(self, attempt_id: str) -> AttemptRecord:
        record = self.campaign_controller.seal_attempt(
            bounded_opaque_id(attempt_id, "attempt_id")
        )
        with self._attempt_condition:
            self._attempt_condition.notify_all()
        return record

    def record_evaluation_delivered_for_occurrence(
        self,
        *,
        attempt_id: str,
        occurrence_key: str,
        candidate_evaluation_id: str,
        outcome: str,
    ) -> None:
        """Persist one idempotent method-native evaluation delivery receipt."""

        key = bounded_opaque_id(occurrence_key, "occurrence_key")
        attempt = bounded_opaque_id(attempt_id, "attempt_id")
        evaluation_id = bounded_opaque_id(
            candidate_evaluation_id, "candidate_evaluation_id"
        )
        operations = self.campaign_controller
        state = operations.result_for_occurrence(key)
        result = state.terminal_result if state is not None else None
        if (
            result is None
            or result.attempt_id != attempt
            or result.candidate_evaluation_id != evaluation_id
            or result.outcome != outcome
        ):
            raise RunValidationError("native evaluation delivery is misbound")
        self._observe_campaign_result(result)
        operations.record_evaluation_delivered(result)
        with self._delivery_state_lock:
            self._delivered_terminal_ids.add(evaluation_id)

    def _dispatch(self, request: ArenaRequest) -> dict[str, object]:
        if request.op == "context":
            self._exact_payload(request.payload, set())
            return self._context()
        if request.op == "development.run":
            benchmark_tasks = self._parse_development_tasks(
                request.payload, single=True
            )
            candidate_workspace = self._development_candidate_workspace(request.payload)
            return self._public_development_results(
                self.campaign_controller.run_development_tasks(
                    benchmark_tasks,
                    candidate_workspace=candidate_workspace,
                )
            )
        if request.op == "development.run_set":
            fields = set(request.payload)
            if fields in ({"all"}, {"all", "candidate_workspace"}):
                if request.payload["all"] is not True:
                    raise ArenaProtocolError("development.run_set all must be true")
                benchmark_tasks = MappingProxyType(
                    {
                        benchmark_id: self.developments[benchmark_id].frozen_task_ids
                        for benchmark_id in self.benchmark_ids
                    }
                )
            else:
                benchmark_tasks = self._parse_development_tasks(
                    request.payload, single=False
                )
            candidate_workspace = self._development_candidate_workspace(request.payload)
            return self._public_development_results(
                self.campaign_controller.run_development_tasks(
                    benchmark_tasks,
                    candidate_workspace=candidate_workspace,
                )
            )
        if request.op == "attempt.start":
            fields = set(request.payload)
            if fields not in (
                {"attempt_id"},
                {"attempt_id", "kind", "benchmark_slots"},
            ):
                raise ArenaProtocolError("operation payload fields are invalid")
            attempt_id = bounded_opaque_id(request.payload["attempt_id"], "attempt_id")
            benchmark_slots = (
                self._parse_benchmark_slots(request.payload["benchmark_slots"])
                if "benchmark_slots" in request.payload
                else None
            )
            kind = (
                bounded_opaque_id(request.payload["kind"], "attempt kind")
                if "kind" in request.payload
                else None
            )
            return self._public_attempt(
                self.start_attempt(
                    attempt_id,
                    kind=kind,
                    benchmark_slots=benchmark_slots,
                )
            )
        if request.op == "attempt.transition":
            self._exact_payload(request.payload, {"attempt_id", "decision"})
            attempt_id = bounded_opaque_id(request.payload["attempt_id"], "attempt_id")
            decision = bounded_opaque_id(request.payload["decision"], "gate decision")
            if self.campaign_controller.attempt(attempt_id) is None:
                raise ArenaUnknownAttempt
            return self._public_attempt(self.transition_attempt(attempt_id, decision))
        if request.op == "submit":
            required = {"attempt_id", "occurrence_key", "role"}
            optional = {"candidate_workspace", "parent_candidate_id"}
            if set(request.payload) - required - optional or required - set(
                request.payload
            ):
                raise ArenaProtocolError("operation payload fields are invalid")
            attempt_id = bounded_opaque_id(request.payload["attempt_id"], "attempt_id")
            attempt = self.campaign_controller.attempt(attempt_id)
            if attempt is None:
                raise ArenaUnknownAttempt
            if attempt.state != "OPEN":
                raise RunValidationError("submit requires an entered attempt")
            occurrence_key = bounded_opaque_id(
                request.payload["occurrence_key"], "occurrence_key"
            )
            role = bounded_opaque_id(request.payload["role"], "candidate role")
            candidate_workspace = (
                self._resolve_candidate_workspace(
                    request.payload["candidate_workspace"]
                )
                if "candidate_workspace" in request.payload
                else self.workspace
            )
            parent_candidate_id = (
                bounded_opaque_id(
                    request.payload["parent_candidate_id"],
                    "parent_candidate_id",
                )
                if "parent_candidate_id" in request.payload
                else None
            )
            if parent_candidate_id is not None:
                self.campaign_controller.candidate_state(parent_candidate_id)
            result = self._submit_joined(
                attempt_id,
                occurrence_key,
                candidate_workspace=candidate_workspace,
                role=role,
                parent_candidate_id=parent_candidate_id,
            )
            self._observe_campaign_result(result)
            public = project_campaign_result(occurrence_key, result).as_dict()
            with self._state_lock:
                self._last_trusted_submission_result = public
            with self._attempt_condition:
                self._attempt_condition.notify_all()
            return public
        if request.op == "result":
            self._exact_payload(request.payload, {"occurrence_key", "wait"})
            occurrence_key = bounded_opaque_id(
                request.payload["occurrence_key"], "occurrence_key"
            )
            if request.payload["wait"] is not True:
                raise ArenaProtocolError("result wait must be true")
            while True:
                state = self.campaign_controller.result_for_occurrence(occurrence_key)
                if state is None:
                    raise ArenaUnknownOccurrence
                if state.terminal_result is not None:
                    return self._public_occurrence_state(state)
                with self._attempt_condition:
                    self._attempt_condition.wait(timeout=0.1)
        if request.op == "attempt.seal":
            self._exact_payload(request.payload, {"attempt_id"})
            attempt_id = bounded_opaque_id(request.payload["attempt_id"], "attempt_id")
            if self.campaign_controller.attempt(attempt_id) is None:
                raise ArenaUnknownAttempt
            return self._public_attempt(self.seal_attempt(attempt_id))
        if request.op == "attempt.result":
            self._exact_payload(request.payload, {"attempt_id", "wait"})
            attempt_id = bounded_opaque_id(request.payload["attempt_id"], "attempt_id")
            wait = request.payload["wait"]
            if not isinstance(wait, bool):
                raise ArenaProtocolError("attempt.result wait must be boolean")
            while True:
                attempt = self.campaign_controller.attempt(attempt_id)
                if attempt is None:
                    raise ArenaUnknownAttempt
                if not wait or attempt.state == "TERMINAL":
                    return self._public_attempt(attempt)
                with self._attempt_condition:
                    self._attempt_condition.wait(timeout=0.1)
        raise ArenaProtocolError("unsupported Arena operation")

    def _submit_joined(
        self,
        attempt_id: str,
        occurrence_key: str,
        *,
        candidate_workspace: Path,
        role: str,
        parent_candidate_id: str | None,
    ) -> CampaignEvaluationResult:
        """Join concurrent retries for one public attempt/occurrence identity."""

        identity = (attempt_id, occurrence_key)
        with self._submit_flights_lock:
            flight = self._submit_flights.get(identity)
            leader = flight is None
            if flight is None:
                flight = _SubmitFlight(
                    threading.Event(),
                    candidate_workspace,
                    role,
                    parent_candidate_id,
                )
                self._submit_flights[identity] = flight
            elif (
                flight.candidate_workspace != candidate_workspace
                or flight.role != role
                or flight.parent_candidate_id != parent_candidate_id
            ):
                raise RunValidationError(
                    "concurrent submit retry changed candidate binding"
                )
        if not leader:
            flight.completed.wait()
            if flight.error is not None:
                raise flight.error
            if flight.result is None:
                raise RunValidationError(
                    "submit single-flight completed without result"
                )
            return flight.result
        try:
            result = self.campaign_controller.evaluate_candidate(
                candidate_workspace,
                native_candidate_id=occurrence_key,
                attempt_id=attempt_id,
                role=role,
                parent_candidate_id=parent_candidate_id,
            )
            flight.result = result
            return result
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            flight.completed.set()
            with self._submit_flights_lock:
                if self._submit_flights.get(identity) is flight:
                    del self._submit_flights[identity]

    @staticmethod
    def _exact_payload(payload, expected: set[str]) -> None:
        if set(payload) != expected:
            raise ArenaProtocolError("operation payload fields are invalid")

    def _parse_benchmark_slots(
        self,
        value: object,
    ) -> dict[str, tuple[SampleSlot, ...]]:
        if not isinstance(value, dict) or not value:
            raise ArenaProtocolError("benchmark_slots must be a non-empty object")
        parsed: dict[str, tuple[SampleSlot, ...]] = {}
        for raw_benchmark_id, raw_slots in value.items():
            benchmark_id = bounded_opaque_id(raw_benchmark_id, "benchmark id")
            if benchmark_id not in self.benchmark_ids:
                raise ArenaProtocolError("benchmark_slots names an unknown benchmark")
            if not isinstance(raw_slots, list):
                raise ArenaProtocolError("benchmark_slots values must be lists")
            if not raw_slots:
                continue
            slots: list[SampleSlot] = []
            for raw_slot in raw_slots:
                try:
                    slots.append(SampleSlot.from_mapping(raw_slot))
                except (TypeError, ValueError) as exc:
                    raise ArenaProtocolError("benchmark slot is invalid") from exc
            parsed[benchmark_id] = tuple(slots)
        result = {
            benchmark_id: parsed[benchmark_id]
            for benchmark_id in self.benchmark_ids
            if benchmark_id in parsed
        }
        if not result:
            raise ArenaProtocolError("benchmark_slots must select at least one task")
        return result

    def _parse_development_tasks(
        self,
        payload: Mapping[str, object],
        *,
        single: bool,
    ) -> Mapping[str, tuple[str, ...]]:
        required = {"benchmark_ids", "task_ids_by_benchmark"}
        if set(payload) - required - {"candidate_workspace"} or required - set(payload):
            raise ArenaProtocolError("operation payload fields are invalid")
        raw_benchmark_ids = payload["benchmark_ids"]
        raw_tasks = payload["task_ids_by_benchmark"]
        if not isinstance(raw_benchmark_ids, list) or not isinstance(
            raw_tasks, Mapping
        ):
            raise ArenaProtocolError("development benchmark IDs/tasks are invalid")
        benchmark_ids = tuple(
            bounded_opaque_id(benchmark_id, "benchmark_id")
            for benchmark_id in raw_benchmark_ids
        )
        if len(set(benchmark_ids)) != len(benchmark_ids) or set(raw_tasks) != set(
            benchmark_ids
        ):
            raise ArenaProtocolError(
                "development benchmark keys do not match their task mapping"
            )
        unknown = set(benchmark_ids) - set(self.benchmark_ids)
        if unknown:
            raise ArenaProtocolError("development request names an unknown benchmark")
        selected_by_benchmark: dict[str, tuple[str, ...]] = {}
        for benchmark_id in benchmark_ids:
            raw_value = raw_tasks[benchmark_id]
            if single:
                if not isinstance(raw_value, str):
                    raise ArenaProtocolError(
                        "development.run requires one task ID per benchmark"
                    )
                task_ids = (bounded_opaque_id(raw_value, "task_id"),)
            else:
                if not isinstance(raw_value, list):
                    raise ArenaProtocolError("development task mappings require lists")
                task_ids = tuple(
                    bounded_opaque_id(task_id, "task_id") for task_id in raw_value
                )
                if len(set(task_ids)) != len(task_ids):
                    raise ArenaProtocolError(
                        "development task IDs cannot contain duplicates"
                    )
            if task_ids:
                selected_by_benchmark[benchmark_id] = task_ids
        parsed = {
            benchmark_id: selected_by_benchmark[benchmark_id]
            for benchmark_id in self.benchmark_ids
            if benchmark_id in selected_by_benchmark
        }
        if not parsed:
            raise ArenaProtocolError("development request selected no tasks")
        return MappingProxyType(parsed)

    def _development_candidate_workspace(
        self, payload: Mapping[str, object]
    ) -> Path | None:
        if "candidate_workspace" not in payload:
            return None
        return self._resolve_candidate_workspace(payload["candidate_workspace"])

    def _resolve_candidate_workspace(self, value: object) -> Path:
        if not isinstance(value, str) or not value:
            raise ArenaProtocolError(
                "candidate_workspace must be a non-empty relative path"
            )
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ArenaProtocolError(
                "candidate_workspace must be a non-empty relative path"
            )
        try:
            candidate = (self.method_workspace / relative).resolve(strict=True)
            candidate.relative_to(self.method_workspace)
        except (OSError, ValueError) as exc:
            raise ArenaProtocolError(
                "candidate_workspace must remain inside the method workspace"
            ) from exc
        if not candidate.is_dir():
            raise ArenaProtocolError("candidate_workspace must name a directory")
        return self.layout.assert_workspace_isolated(candidate)

    def _context(self) -> dict[str, object]:
        used = self.campaign_controller.attempts_used
        result: dict[str, object] = {
            "run_id": self.layout.run_id,
            "campaign_id": self.layout.campaign,
            "method": self.layout.method,
            "run_scope_id": self.layout.benchmark,
            "benchmark_ids": list(self.benchmark_ids),
            "development_task_ids": {
                benchmark_id: list(self.developments[benchmark_id].frozen_task_ids)
                for benchmark_id in self.benchmark_ids
            },
            "workspace": str(self.public_workspace),
            "attempt_cap": self.attempt_cap,
            "remaining_attempts": max(0, self.attempt_cap - used),
        }
        result["development_evidence_roots"] = {
            benchmark_id: str(self.public_development_evidence_roots[benchmark_id])
            for benchmark_id in self.benchmark_ids
        }
        return result

    def _public_development_results(
        self, results: Mapping[str, DevelopmentResult]
    ) -> dict[str, object]:
        if not isinstance(results, Mapping) or not results:
            raise RunValidationError(
                "development result keys/order changed after session execution"
            )
        expected_ids = tuple(
            benchmark_id
            for benchmark_id in self.benchmark_ids
            if benchmark_id in results
        )
        if tuple(results) != expected_ids:
            raise RunValidationError(
                "development result keys/order changed after session execution"
            )
        public_results: dict[str, DevelopmentResult] = {}
        for benchmark_id in expected_ids:
            result = self.developments[benchmark_id].freeze_public_result(
                results[benchmark_id]
            )
            if result.evidence_path is None:
                public_results[benchmark_id] = DevelopmentResult(
                    outcome=result.outcome,
                    development_evaluation_id=(result.development_evaluation_id),
                    evidence_path=None,
                )
                continue
            evidence = Path(result.evidence_path).absolute()
            evidence_root = self.development_evidence_roots[benchmark_id]
            try:
                evidence.relative_to(evidence_root)
            except ValueError as exc:
                raise RunValidationError(
                    "development evidence escaped its run-private authority"
                ) from exc
            if evidence == evidence_root:
                raise RunValidationError("development result lacks operation evidence")
            relative = evidence.relative_to(evidence_root)
            public_evidence = (
                self.public_development_evidence_roots[benchmark_id] / relative
            )
            public_results[benchmark_id] = DevelopmentResult(
                outcome=result.outcome,
                development_evaluation_id=result.development_evaluation_id,
                evidence_path=str(public_evidence),
            )
        return DevelopmentBatchResult(public_results).as_dict()

    def _validate_campaign_authority(self, result: CampaignEvaluationResult) -> None:
        if result.run_id != self.layout.run_id or not result.attempt_id:
            raise RunValidationError("campaign result belongs to another run")
        result_benchmark_ids = tuple(
            child.benchmark_id for child in result.benchmark_results
        )
        expected_order = tuple(
            benchmark_id
            for benchmark_id in self.benchmark_ids
            if benchmark_id in result_benchmark_ids
        )
        attempt = self.campaign_controller.attempt(result.attempt_id)
        candidate_state = self.campaign_controller.candidate_state(result.candidate_id)
        durable_order = (
            tuple(benchmark_id for benchmark_id, _slots in attempt.benchmark_slots)
            if attempt is not None
            else ()
        )
        if (
            not result_benchmark_ids
            or candidate_state.role != result.candidate_role
            or result_benchmark_ids != expected_order
            or result_benchmark_ids != durable_order
        ):
            raise RunValidationError("campaign result benchmark subset is inconsistent")
        child_vector = tuple(
            (child.benchmark_id, child.score) for child in result.benchmark_results
        )
        if not result.score_vector or child_vector != result.score_vector:
            raise RunValidationError("campaign score vector is inconsistent")
        scores = [
            float(score)
            for _benchmark_id, score in result.score_vector
            if score is not None
        ]
        expected_score = (
            sum(scores) / len(scores)
            if scores and self.campaign_controller.campaign_aggregate_kind is not None
            else None
        )
        expected_policy = (
            {"kind": self.campaign_controller.campaign_aggregate_kind}
            if self.campaign_controller.campaign_aggregate_kind is not None
            else None
        )
        if result.aggregate_policy != expected_policy:
            raise RunValidationError("campaign aggregate policy is inconsistent")
        if result.aggregate_score != expected_score:
            raise RunValidationError("campaign aggregate score is inconsistent")
        if result.outcome != ("measured" if scores else "inconclusive"):
            raise RunValidationError("campaign aggregate outcome is inconsistent")
        expected_counts = (
            sum(child.n_expected for child in result.benchmark_results),
            sum(child.n_conclusive for child in result.benchmark_results),
            sum(child.n_inconclusive for child in result.benchmark_results),
        )
        if (
            result.n_expected,
            result.n_conclusive,
            result.n_inconclusive,
        ) != expected_counts or not math.isclose(
            result.coverage,
            result.n_conclusive / result.n_expected,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise RunValidationError("campaign aggregate counts are inconsistent")

    def _observe_campaign_result(self, result: CampaignEvaluationResult) -> None:
        self._validate_campaign_authority(result)

    def _public_attempt(self, attempt: AttemptRecord) -> dict[str, object]:
        n_terminal = 0
        for candidate_id in attempt.candidate_ids:
            state = self.campaign_controller.candidate_state(candidate_id)
            if state.attempt_id != attempt.attempt_id:
                raise RunValidationError("attempt candidate state is misbound")
            if state.terminal_result is not None:
                n_terminal += 1
        n_candidates = len(attempt.candidate_ids)
        result: dict[str, object] = {
            "attempt_id": attempt.attempt_id,
            "attempt_index": attempt.attempt_index,
            "attempt_state": attempt.state,
            "kind": attempt.kind,
            "gate_decision": attempt.gate_decision,
            "n_candidates": n_candidates,
            "n_in_progress": n_candidates - n_terminal,
            "n_terminal": n_terminal,
            "candidate_roles": [
                self.campaign_controller.candidate_state(candidate_id).role
                for candidate_id in attempt.candidate_ids
            ],
        }
        return {"state": attempt.state, "result": result}

    def _public_occurrence_state(
        self, state: CampaignOccurrenceState
    ) -> dict[str, object]:
        if state.terminal_result is not None:
            self._observe_campaign_result(state.terminal_result)
            public = project_campaign_result(
                state.occurrence_key, state.terminal_result
            ).as_dict()
            self._last_trusted_submission_result = public
            return public
        return {"state": "IN_PROGRESS", "result": None}
