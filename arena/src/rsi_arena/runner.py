"""Small benchmark-neutral session contract for native baseline extras."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Callable, Mapping, Protocol, Sequence

from rsi_arena.client import ArenaClient

if TYPE_CHECKING:
    from rsi_arena.manifests import SampleSlot

from .development import NativeDevelopmentOperations
from .dto import DevelopmentResult
from .service import (
    ArenaService,
    NativeCandidateMaterialization,
    NativeCandidateReservation,
    NativeCandidateTerminal,
)
from .session_spine import SessionSpine


class MethodRunner(Protocol):
    """One native baseline integration, shared across supported benchmarks."""

    def run(self, session: "ArenaSession") -> None: ...


@dataclass(frozen=True)
class ArenaSession:
    """Run-scoped authorities supplied to a native method integration.

    The plural spine owns candidate lifecycle routing; its existing recorder and
    controller remain the durable slot, cap, and official-score writers.
    """

    service: ArenaService
    spine: SessionSpine
    client: ArenaClient
    developments: Mapping[str, NativeDevelopmentOperations]
    candidate_workspace: Path
    prompt_context_path: Path
    method_state_directory: Path
    authority_environment: Mapping[str, str]
    usage_sink: object
    trajectory_sink: object
    event_sink: object

    def __post_init__(self) -> None:
        layout = self.service.layout
        if (
            self.spine.layout != layout
            or self.service.campaign_controller is not self.spine
            or tuple(self.developments) != self.spine.benchmark_ids
            or any(
                self.developments[benchmark_id]
                is not self.spine.developments[benchmark_id]
                for benchmark_id in self.spine.benchmark_ids
            )
        ):
            raise ValueError("Arena session service is not bound to its SessionSpine")
        workspace = layout.assert_workspace_isolated(self.candidate_workspace)
        if workspace != self.service.workspace:
            raise ValueError("Arena session workspace differs from its service")
        method_root = layout.method_root.resolve(strict=True)
        state = Path(self.method_state_directory).resolve(strict=True)
        context = Path(self.prompt_context_path).resolve(strict=True)
        for path, label in ((state, "method state"), (context, "prompt context")):
            try:
                path.relative_to(method_root)
            except ValueError as exc:
                raise ValueError(
                    f"Arena session {label} escaped method runtime"
                ) from exc
        object.__setattr__(self, "candidate_workspace", workspace)
        object.__setattr__(self, "prompt_context_path", context)
        object.__setattr__(self, "method_state_directory", state)
        object.__setattr__(
            self, "developments", MappingProxyType(dict(self.developments))
        )
        object.__setattr__(
            self,
            "authority_environment",
            MappingProxyType(dict(self.authority_environment)),
        )

    def reserve_native_candidate(
        self,
        payload: bytes | str,
        *,
        attempt_id: str,
        occurrence_key: str,
        media_type: str,
        role: str,
        parent_candidate_id: str | None = None,
    ) -> NativeCandidateReservation:
        """Reserve one raw native evaluation occurrence before method parsing."""

        return self.spine.reserve_native_candidate(
            payload,
            attempt_id=attempt_id,
            occurrence_key=occurrence_key,
            media_type=media_type,
            role=role,
            parent_candidate_id=parent_candidate_id,
        )

    @property
    def development_task_ids(self) -> Mapping[str, tuple[str, ...]]:
        """The development owners' frozen membership in campaign-cell order."""

        return MappingProxyType(
            {
                benchmark_id: self.developments[benchmark_id].frozen_task_ids
                for benchmark_id in self.spine.benchmark_ids
            }
        )

    @property
    def evaluation_concurrency(self) -> int:
        """The one Arena-owned task cap shared across concurrent B sides."""

        return self.spine.evaluation_concurrency

    @property
    def invalid_cell_retries(self) -> int:
        """The frozen Arena-owned public-cell apparatus retry budget."""

        return self.spine.invalid_cell_retries

    def start_attempt(
        self,
        attempt_id: str,
        *,
        kind: str | None = None,
        benchmark_slots: Mapping[str, Sequence[SampleSlot]] | None = None,
    ):
        """Start explicitly, or replay an attempt already durable in Arena."""

        if benchmark_slots is None and kind is None:
            return self.spine.start_attempt(attempt_id)
        return self.spine.start_attempt(
            attempt_id,
            kind=kind,
            benchmark_slots=benchmark_slots,
        )

    def transition_attempt(self, attempt_id: str, decision: str):
        """Persist the method's gate decision before any evaluation starts."""

        return self.spine.transition_attempt(attempt_id, decision)

    def seal_attempt(self, attempt_id: str):
        return self.spine.seal_attempt(attempt_id)

    def materialize_native_candidate(
        self,
        reservation: NativeCandidateReservation,
        *,
        materialize: Callable[[bytes, Path], Mapping[str, object]],
    ) -> NativeCandidateMaterialization | NativeCandidateTerminal:
        return self.spine.materialize_native_candidate(reservation, materialize)

    def evaluate_native_candidate(
        self, materialized: NativeCandidateMaterialization
    ) -> NativeCandidateTerminal:
        return self.spine.evaluate_native_candidate(materialized)

    def discard_native_candidate_materialization(
        self, materialized: NativeCandidateMaterialization
    ) -> None:
        self.spine.discard_native_candidate_materialization(materialized)

    def candidate_state(self, candidate_id: str):
        return self.spine.candidate_state(candidate_id)

    def attempt_result(self, attempt_id: str):
        return self.spine.attempt_result(attempt_id)

    @staticmethod
    def task_outcomes(
        terminal: NativeCandidateTerminal,
    ) -> Mapping[str, tuple[Mapping[str, object], ...]]:
        """Expose trusted leaf outcomes, including constraints and wave slots."""

        return MappingProxyType(
            {
                child.benchmark_id: child.task_outcomes
                for child in terminal.result.benchmark_results
            }
        )

    def run_development_all(self) -> Mapping[str, DevelopmentResult]:
        return self.spine.run_development_tasks(
            {
                benchmark_id: self.spine.developments[benchmark_id].frozen_task_ids
                for benchmark_id in self.spine.benchmark_ids
            }
        )

    def run_development_tasks(
        self,
        task_ids_by_benchmark: Mapping[str, Sequence[str]],
        *,
        candidate_workspace: Path | None = None,
    ) -> Mapping[str, DevelopmentResult]:
        """Run an ordered non-empty method-selected benchmark subset."""

        return self.spine.run_development_tasks(
            task_ids_by_benchmark,
            candidate_workspace=candidate_workspace,
        )

    def evaluate_native_development_candidate(
        self,
        payload: bytes | str,
        *,
        materialize: Callable[[bytes, Path], Mapping[str, object]],
        task_ids_by_benchmark: Mapping[str, Sequence[str]],
    ) -> Mapping[str, Mapping[str, object]]:
        """Materialize raw method output and return trusted development facts.

        The temporary candidate lives below the Arena-owned method workspace;
        external method worktrees are never handed directly to a benchmark.
        Each benchmark projection is read from its immutable publication.
        """

        if isinstance(payload, str):
            raw = payload.encode("utf-8")
        elif isinstance(payload, bytes):
            raw = payload
        else:
            raise TypeError("native development payload must be bytes or text")
        if not callable(materialize):
            raise TypeError("native development materializer must be callable")
        root = self.candidate_workspace / ".rsi-arena" / "native-development"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="candidate-", dir=root) as temporary:
            candidate = Path(temporary).resolve(strict=True)
            provenance = materialize(raw, candidate)
            if not isinstance(provenance, Mapping):
                raise TypeError(
                    "native development materializer returned no provenance"
                )
            results = {
                benchmark_id: self.developments[benchmark_id].run_trusted_native_set(
                    task_ids,
                    candidate_workspace=candidate,
                )
                for benchmark_id, task_ids in task_ids_by_benchmark.items()
            }
            projections: dict[str, Mapping[str, object]] = {}
            for benchmark_id, result in results.items():
                operation = self.developments[benchmark_id]
                projections[benchmark_id] = MappingProxyType(
                    {
                        **dict(operation.trusted_native_projection(result)),
                        "trace_artifacts": operation.trusted_native_trace_artifacts(
                            result
                        ),
                    }
                )
            return MappingProxyType(projections)

    def trusted_development_projection(
        self, results: Mapping[str, DevelopmentResult]
    ) -> Mapping[str, object]:
        expected = tuple(
            benchmark_id
            for benchmark_id in self.spine.benchmark_ids
            if benchmark_id in results
        )
        if not expected or tuple(results) != expected:
            raise RuntimeError(
                "Arena development result order differs from the session"
            )
        benchmark_results: list[dict[str, object]] = []
        for benchmark_id in expected:
            projection = self.developments[
                benchmark_id
            ].trusted_native_feedback_projection(results[benchmark_id])
            result = projection.get("result")
            if projection.get("state") != "TERMINAL" or not isinstance(result, Mapping):
                raise RuntimeError("Arena development projection is not terminal")
            benchmark_results.append(
                {
                    **dict(result),
                    "benchmark_id": benchmark_id,
                    "development_evaluation_id": projection[
                        "development_evaluation_id"
                    ],
                }
            )
        return MappingProxyType(
            {
                "state": "TERMINAL",
                "result": {"benchmark_results": benchmark_results},
            }
        )

    def record_native_development_delivery(
        self,
        *,
        development_results: Mapping[str, DevelopmentResult],
        native_request_id: str,
        rendered: str,
        response_fields: tuple[str, ...],
        channel: str,
        recipient: str,
    ) -> None:
        expected = tuple(
            benchmark_id
            for benchmark_id in self.spine.benchmark_ids
            if benchmark_id in development_results
        )
        if not expected or tuple(development_results) != expected:
            raise RuntimeError(
                "Arena development delivery order differs from the session"
            )
        for benchmark_id in expected:
            result = development_results[benchmark_id]
            self.developments[benchmark_id].record_native_aggregate_delivery(
                development_evaluation_id=result.development_evaluation_id,
                development_outcome=result.outcome,
                native_request_id=native_request_id,
                rendered=rendered,
                response_fields=response_fields,
                channel=channel,
                recipient=recipient,
            )

    def record_native_evaluation_delivery(
        self,
        *,
        attempt_id: str,
        occurrence_key: str,
        candidate_evaluation_id: str,
        outcome: str,
    ) -> None:
        self.service.record_evaluation_delivered_for_occurrence(
            attempt_id=attempt_id,
            occurrence_key=occurrence_key,
            candidate_evaluation_id=candidate_evaluation_id,
            outcome=outcome,
        )


__all__ = ["ArenaSession", "MethodRunner"]
