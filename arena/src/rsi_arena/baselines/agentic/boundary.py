"""Shipped agentic baseline A/B request policy boundary."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence

from rsi_arena.development import replay_delivered_development_results
from rsi_arena.dto import (
    ArenaProtocolError,
    ArenaRequest,
    ArenaResponse,
    bounded_opaque_id,
)
from rsi_arena.manifests import SampleSlot
from rsi_arena.run_layout import RunValidationError, replay_run
from rsi_arena.service import ArenaService


class AgentFacingArenaBoundary:
    """Restrict only the agent-owned UDS view of the public Arena service."""

    def __init__(
        self,
        service: ArenaService,
        *,
        fresh_attempt_slot_provider: Callable[[str], Mapping[str, Sequence[SampleSlot]]]
        | None,
        fixed_a_task_ids_by_benchmark: Mapping[str, Sequence[str]] | None,
        max_new_terminal_a_per_b_cycle: int | None,
    ) -> None:
        if max_new_terminal_a_per_b_cycle is not None and (
            isinstance(max_new_terminal_a_per_b_cycle, bool)
            or not isinstance(max_new_terminal_a_per_b_cycle, int)
            or max_new_terminal_a_per_b_cycle <= 0
        ):
            raise ValueError(
                "agentic max_new_terminal_a_per_b_cycle must be a positive integer"
            )
        self._service = service
        self.session_spine = service.campaign_controller
        self._fresh_attempt_slot_provider = fresh_attempt_slot_provider
        if fixed_a_task_ids_by_benchmark is None:
            self._fixed_a_task_ids_by_benchmark = None
        else:
            if tuple(fixed_a_task_ids_by_benchmark) != service.benchmark_ids:
                raise ValueError("agentic fixed A task IDs are invalid")
            fixed_a: dict[str, tuple[str, ...]] = {}
            for benchmark_id in service.benchmark_ids:
                task_ids = tuple(fixed_a_task_ids_by_benchmark[benchmark_id])
                if (
                    not task_ids
                    or any(
                        not isinstance(task_id, str) or not task_id
                        for task_id in task_ids
                    )
                    or len(set(task_ids)) != len(task_ids)
                ):
                    raise ValueError("agentic fixed A task IDs are invalid")
                fixed_a[benchmark_id] = task_ids
            self._fixed_a_task_ids_by_benchmark = fixed_a
        self._max_new_terminal_a_per_b_cycle = max_new_terminal_a_per_b_cycle
        self._pending_a_response: ArenaResponse | None = None
        (
            self._new_terminal_a_count,
            self._active_attempt_id,
        ) = self._restore_cycle_state()
        self._a_inflight = False
        self._state_lock = threading.RLock()
        self.layout = service.layout

    def _restore_cycle_state(self) -> tuple[int, str | None]:
        """Rebuild the A-before-B cadence from canonical delivery and attempt facts."""

        if self._max_new_terminal_a_per_b_cycle is None:
            return 0, None
        replay = replay_run(self._service.layout)
        active = tuple(
            attempt for attempt in replay.attempts if attempt.state != "TERMINAL"
        )
        if len(active) > 1 or any(attempt.kind != "single" for attempt in active):
            raise RunValidationError("agentic active attempt state is invalid")
        benchmark_tasks = {
            benchmark_id: self._service.developments[benchmark_id].frozen_task_ids
            for benchmark_id in self._service.benchmark_ids
        }
        delivered = replay_delivered_development_results(
            self._service.layout,
            benchmark_task_ids=benchmark_tasks,
        )
        terminal_b_times = tuple(
            attempt.terminal_at
            for attempt in replay.attempts
            if attempt.state == "TERMINAL"
            and attempt.gate_decision == "enter"
            and attempt.terminal_at is not None
        )
        last_terminal_b = max(terminal_b_times, default=float("-inf"))
        counts = Counter(
            str(result.get("benchmark_id"))
            for result in delivered
            if result.get("benchmark_id") in benchmark_tasks
            and isinstance(result.get("delivery_timestamp"), (int, float))
            and not isinstance(result.get("delivery_timestamp"), bool)
            and float(result["delivery_timestamp"]) > last_terminal_b
        )
        completed_development_cycles = (
            min(counts.get(benchmark_id, 0) for benchmark_id in benchmark_tasks)
            if benchmark_tasks
            else 0
        )
        pending_development = completed_development_cycles
        if pending_development > self._max_new_terminal_a_per_b_cycle:
            raise RunValidationError("agentic A-before-B cadence is invalid")
        return pending_development, active[0].attempt_id if active else None

    def handle_request(self, request: ArenaRequest) -> ArenaResponse:
        fresh_attempt_id: str | None = None
        response: ArenaResponse | None = None
        service_request = request
        if request.op == "development.run":
            return self._reject_partial_a(request)
        if request.op == "development.run_set" and dict(request.payload) != {
            "all": True
        }:
            return self._reject_partial_a(request)
        if request.op == "attempt.start" and set(request.payload) != {"attempt_id"}:
            return ArenaResponse.failure(
                request.request_id,
                kind="protocol_error",
                summary="agentic attempt.start accepts only attempt_id",
            )
        if request.op == "submit" and set(request.payload) != {
            "attempt_id",
            "occurrence_key",
            "role",
        }:
            return ArenaResponse.failure(
                request.request_id,
                kind="protocol_error",
                summary=("agentic submit accepts only attempt_id and occurrence_key"),
            )
        if request.op == "submit" and request.payload.get("role") != "candidate":
            return ArenaResponse.failure(
                request.request_id,
                kind="protocol_error",
                summary="agentic submit role must be candidate",
            )
        if request.op == "attempt.transition" and (
            set(request.payload) != {"attempt_id", "decision"}
            or request.payload.get("decision") != "enter"
        ):
            return ArenaResponse.failure(
                request.request_id,
                kind="protocol_error",
                summary="agentic gate transition must enter the active attempt",
            )
        is_a_run = request.op == "development.run_set"
        if is_a_run and self._fixed_a_task_ids_by_benchmark is not None:
            service_request = ArenaRequest(
                request.request_id,
                request.op,
                {
                    "benchmark_ids": list(self._fixed_a_task_ids_by_benchmark),
                    "task_ids_by_benchmark": {
                        benchmark_id: list(task_ids)
                        for benchmark_id, task_ids in (
                            self._fixed_a_task_ids_by_benchmark.items()
                        )
                    },
                },
            )
        with self._state_lock:
            if is_a_run and self._pending_a_response is not None:
                pending = self._pending_a_response
                assert pending.result is not None
                return ArenaResponse.success(
                    request.request_id,
                    state=str(pending.state),
                    result=pending.result,
                )
            if is_a_run:
                rejected = self._reject_fresh_a(request)
                if rejected is not None:
                    return rejected
                if self._a_inflight:
                    return ArenaResponse.failure(
                        request.request_id,
                        kind="protocol_error",
                        summary="agentic A evaluation is already active",
                    )
                self._a_inflight = True
            if request.op in {
                "attempt.start",
                "attempt.transition",
                "submit",
                "attempt.seal",
                "attempt.result",
            }:
                attempt_id = self._request_attempt_id(request)
                if attempt_id is not None:
                    rejected, fresh = self._check_attempt_request(request, attempt_id)
                    if rejected is not None:
                        if is_a_run:
                            self._a_inflight = False
                        return rejected
                    if fresh:
                        fresh_attempt_id = attempt_id
                        self._active_attempt_id = attempt_id
                    if request.op == "attempt.start":
                        if not self._known_attempt(attempt_id):
                            try:
                                service_request = self._fresh_attempt_start_request(
                                    request,
                                    attempt_id,
                                )
                            except Exception:
                                if self._active_attempt_id == attempt_id:
                                    self._active_attempt_id = None
                                return ArenaResponse.failure(
                                    request.request_id,
                                    kind="internal_error",
                                    summary="trusted attempt slot selection failed",
                                )
                        # Keep the trusted selection and durable first-start atomic
                        # with respect to other agent-facing start intents.  The
                        # kernel remains the sole durable admission authority.
                        response = self._service.handle_request(service_request)
        try:
            if response is None:
                response = self._service.handle_request(service_request)
        finally:
            if is_a_run:
                with self._state_lock:
                    self._a_inflight = False
        with self._state_lock:
            if (
                fresh_attempt_id is not None
                and not self._known_attempt(fresh_attempt_id)
                and self._active_attempt_id == fresh_attempt_id
            ):
                self._active_attempt_id = None
            if is_a_run and self._is_terminal_a_response(response):
                self._pending_a_response = response
        return response

    def _fresh_attempt_start_request(
        self,
        request: ArenaRequest,
        attempt_id: str,
    ) -> ArenaRequest:
        provider = self._fresh_attempt_slot_provider
        if provider is None:
            raise RuntimeError("fresh attempt slot provider is unavailable")
        selected = provider(attempt_id)
        if not isinstance(selected, Mapping) or not selected:
            raise TypeError("fresh attempt slot provider returned no slots")
        return ArenaRequest(
            request.request_id,
            request.op,
            {
                "attempt_id": attempt_id,
                "kind": "single",
                "benchmark_slots": {
                    benchmark_id: [slot.as_dict() for slot in slots]
                    for benchmark_id, slots in selected.items()
                },
            },
        )

    def record_response_published(
        self,
        request: ArenaRequest,
        response: ArenaResponse,
    ) -> None:
        self._service.record_response_published(request, response)
        with self._state_lock:
            pending = self._pending_a_response
            if (
                request.op == "development.run_set"
                and pending is not None
                and pending.state == response.state
                and pending.result == response.result
            ):
                if self._max_new_terminal_a_per_b_cycle is not None:
                    self._new_terminal_a_count += 1
                self._pending_a_response = None
            if self._max_new_terminal_a_per_b_cycle is None:
                return
            attempt_id = self._request_attempt_id(request)
            if (
                request.op in {"attempt.seal", "attempt.result"}
                and attempt_id is not None
                and attempt_id == self._active_attempt_id
                and self._is_terminal_attempt_response(response, attempt_id)
            ):
                self._new_terminal_a_count = 0
                self._active_attempt_id = None

    def _reject_fresh_a(self, request: ArenaRequest) -> ArenaResponse | None:
        maximum = self._max_new_terminal_a_per_b_cycle
        if maximum is None:
            return None
        if self._active_attempt_id is not None:
            summary = "agentic A is unavailable while an attempt is active"
        elif self._service.attempt_cap_exhausted:
            summary = "agentic A is unavailable after the attempt cap is exhausted"
        elif self._new_terminal_a_count >= maximum:
            summary = "agentic A-per-B cycle reached its configured terminal A limit"
        else:
            return None
        return ArenaResponse.failure(
            request.request_id,
            kind="protocol_error",
            summary=summary,
        )

    def _check_attempt_request(
        self,
        request: ArenaRequest,
        attempt_id: str,
    ) -> tuple[ArenaResponse | None, bool]:
        if self._max_new_terminal_a_per_b_cycle is None:
            return None, False
        known = self.session_spine.attempt(attempt_id)
        if known is not None and self._active_attempt_id is None:
            if known.state != "TERMINAL":
                self._active_attempt_id = attempt_id
            return None, False
        if self._active_attempt_id is not None:
            if attempt_id == self._active_attempt_id:
                return None, False
            return (
                ArenaResponse.failure(
                    request.request_id,
                    kind="protocol_error",
                    summary="agentic recovery must use the active attempt ID",
                ),
                False,
            )
        if request.op != "attempt.start":
            return (
                ArenaResponse.failure(
                    request.request_id,
                    kind="protocol_error",
                    summary="agentic submit requires attempt.start",
                ),
                False,
            )
        if self._service.attempt_cap_exhausted:
            return (
                ArenaResponse.failure(
                    request.request_id,
                    kind="protocol_error",
                    summary="agentic attempt cap is exhausted",
                ),
                False,
            )
        if self._new_terminal_a_count < 1:
            return (
                ArenaResponse.failure(
                    request.request_id,
                    kind="protocol_error",
                    summary="agentic B requires a newly published terminal A",
                ),
                False,
            )
        return None, True

    def _known_attempt(self, attempt_id: str) -> bool:
        return self.session_spine.attempt(attempt_id) is not None

    @staticmethod
    def _request_attempt_id(request: ArenaRequest) -> str | None:
        expected = {
            "submit": {"attempt_id", "occurrence_key", "role"},
            "attempt.transition": {"attempt_id", "decision"},
            "attempt.seal": {"attempt_id"},
            "attempt.result": {"attempt_id", "wait"},
        }.get(request.op)
        fields = set(request.payload)
        if request.op == "attempt.start":
            if fields != {"attempt_id"}:
                return None
        elif expected is None or fields != expected:
            return None
        if request.op == "attempt.result" and not isinstance(
            request.payload.get("wait"), bool
        ):
            return None
        try:
            return bounded_opaque_id(
                request.payload.get("attempt_id"),
                "attempt_id",
            )
        except ArenaProtocolError:
            return None

    @staticmethod
    def _is_terminal_a_response(response: ArenaResponse) -> bool:
        return (
            response.ok and response.state == "TERMINAL" and response.result is not None
        )

    @staticmethod
    def _is_terminal_attempt_response(
        response: ArenaResponse,
        attempt_id: str,
    ) -> bool:
        return (
            response.ok
            and response.state == "TERMINAL"
            and response.result is not None
            and response.result.get("attempt_id") == attempt_id
        )

    @staticmethod
    def _reject_partial_a(request: ArenaRequest) -> ArenaResponse:
        return ArenaResponse.failure(
            request.request_id,
            kind="protocol_error",
            summary="agentic A permits only the complete frozen membership",
        )


__all__ = ["AgentFacingArenaBoundary"]
