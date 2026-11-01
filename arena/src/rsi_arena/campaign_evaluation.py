"""Shared campaign benchmark evaluation over the existing data plane.

The controller owns orchestration only.  CampaignFreeze remains the source of
split/sampling authority, RunRecorder remains the only candidate/evaluation
recorder, and benchmark adapters delegate execution to existing AIME
``BenchAdapter`` or SWE ``SweTaskCellRunner`` primitives.
"""

from __future__ import annotations

import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

from rsi_arena.common import UsageLedger
from rsi_arena.contracts import (
    TaskRef,
    canonical_digest,
)
from rsi_arena.evaluation_gateway import (
    GatewayCandidate,
    GatewayError,
    GatewayRejected,
    TrustedEvaluationGateway,
)
from rsi_arena.gitops import GitError
from rsi_arena.manifests import SampleSlot
from rsi_arena.run_layout import (
    AttemptRecord,
    CampaignEvaluationResult,
    CampaignOccurrenceState,
    CandidateRecord,
    RunLayout,
    RunRecorder,
    RunValidationError,
    TaskEvaluationOutcome,
    read_jsonl,
)
from rsi_arena.runtime_campaign import CampaignCellPlan


def _sum_observed(
    outcomes: Sequence[TaskEvaluationOutcome], field: str
) -> int | float | None:
    values = [getattr(outcome, field) for outcome in outcomes]
    if any(value is None for value in values):
        return None
    return sum(values)  # type: ignore[arg-type, return-value]


def _merge_retry_outcomes(
    outcomes: Sequence[TaskEvaluationOutcome],
) -> TaskEvaluationOutcome:
    """Charge every read while exposing only the final same-slot observation."""

    attempts = tuple(outcomes)
    if not attempts:
        raise RunValidationError("task evaluation produced no retry attempt")
    final = attempts[-1]
    if len(attempts) == 1:
        return final
    calls: list[dict[str, object]] = []
    for attempt in attempts:
        usage = attempt.provenance.get("usage")
        raw_calls = usage.get("calls") if isinstance(usage, Mapping) else None
        if raw_calls is None:
            continue
        if not isinstance(raw_calls, list) or not all(
            isinstance(call, Mapping) for call in raw_calls
        ):
            raise RunValidationError("retry usage calls are invalid")
        for raw_call in raw_calls:
            call = dict(raw_call)
            call["ordinal"] = len(calls) + 1
            calls.append(call)
    provenance = dict(final.provenance)
    usage = provenance.get("usage")
    if not isinstance(usage, Mapping):
        usage = {}
    merged_usage = dict(usage)
    merged_usage["calls"] = calls
    input_tokens = _sum_observed(attempts, "input_tokens")
    output_tokens = _sum_observed(attempts, "output_tokens")
    cache_tokens = _sum_observed(attempts, "cache_tokens")
    cost_usd = _sum_observed(attempts, "cost_usd")
    for name, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("cache_read_tokens", cache_tokens),
        ("cost_usd", cost_usd),
    ):
        merged_usage[name] = value
    provenance["usage"] = merged_usage
    provenance["arena_invalid_cell_attempts"] = len(attempts)
    input_status = "reported" if input_tokens is not None else "missing"
    output_status = "reported" if output_tokens is not None else "missing"
    cost_status = (
        "reported"
        if cost_usd is not None
        and all(outcome.cost_status == "reported" for outcome in attempts)
        else "unverified"
        if any(outcome.cost_status == "unverified" for outcome in attempts)
        else "missing"
    )
    return TaskEvaluationOutcome(
        outcome=final.outcome,
        score=final.score,
        wall_seconds=sum(float(outcome.wall_seconds or 0.0) for outcome in attempts),
        summary=final.summary,
        artifact_refs=final.artifact_refs,
        provenance=MappingProxyType(provenance),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=cache_tokens,
        token_status=(
            "reported"
            if input_tokens is not None and output_tokens is not None
            else "unverified"
            if input_tokens is not None or output_tokens is not None
            else "missing"
        ),
        input_token_status=input_status,
        output_token_status=output_status,
        cache_token_status=("reported" if cache_tokens is not None else "missing"),
        cost_usd=cost_usd if cost_status == "reported" else None,
        cost_status=cost_status,
        constraints_pass=final.constraints_pass,
    )


def _apply_retry_cost_ceiling(
    outcome: TaskEvaluationOutcome,
    *,
    ceiling_usd: object,
) -> TaskEvaluationOutcome:
    attempts = outcome.provenance.get("arena_invalid_cell_attempts")
    if attempts is None or attempts == 1 or ceiling_usd is None:
        return outcome
    if (
        isinstance(ceiling_usd, bool)
        or not isinstance(ceiling_usd, (int, float))
        or not math.isfinite(float(ceiling_usd))
        or float(ceiling_usd) < 0.0
    ):
        raise RunValidationError("retry cost ceiling authority is invalid")
    accepted = (
        outcome.cost_status == "reported"
        and outcome.cost_usd is not None
        and float(outcome.cost_usd) <= float(ceiling_usd)
    )
    if accepted:
        return outcome
    provenance = dict(outcome.provenance)
    provenance["reason"] = "aggregate_retry_cost_unverified_or_exceeded"
    provenance["max_cost_usd"] = float(ceiling_usd)
    return replace(
        outcome,
        outcome="inconclusive",
        score=None,
        summary="aggregate_retry_cost_unverified_or_exceeded",
        provenance=MappingProxyType(provenance),
        constraints_pass=None,
    )


_RETRY_OUTCOME_FIELDS = frozenset(
    {
        "outcome",
        "score",
        "wall_seconds",
        "summary",
        "artifact_refs",
        "provenance",
        "input_tokens",
        "output_tokens",
        "cache_tokens",
        "token_status",
        "input_token_status",
        "output_token_status",
        "cache_token_status",
        "cost_usd",
        "cost_status",
        "constraints_pass",
    }
)


def _retry_json(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise RunValidationError("task retry evidence keys must be strings")
        return {key: _retry_json(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_retry_json(child) for child in value]
    return value


def _retry_outcome_payload(outcome: TaskEvaluationOutcome) -> dict[str, object]:
    return {
        "outcome": outcome.outcome,
        "score": outcome.score,
        "wall_seconds": outcome.wall_seconds,
        "summary": outcome.summary,
        "artifact_refs": _retry_json(outcome.artifact_refs),
        "provenance": _retry_json(outcome.provenance),
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "cache_tokens": outcome.cache_tokens,
        "token_status": outcome.token_status,
        "input_token_status": outcome.input_token_status,
        "output_token_status": outcome.output_token_status,
        "cache_token_status": outcome.cache_token_status,
        "cost_usd": outcome.cost_usd,
        "cost_status": outcome.cost_status,
        "constraints_pass": outcome.constraints_pass,
    }


def _retry_outcome_from_payload(value: object) -> TaskEvaluationOutcome:
    if not isinstance(value, Mapping) or set(value) != _RETRY_OUTCOME_FIELDS:
        raise RunValidationError("task retry outcome receipt is invalid")
    if not isinstance(value["artifact_refs"], Mapping) or not isinstance(
        value["provenance"], Mapping
    ):
        raise RunValidationError("task retry outcome receipt is invalid")
    try:
        return TaskEvaluationOutcome(
            outcome=value["outcome"],  # type: ignore[arg-type]
            score=value["score"],  # type: ignore[arg-type]
            wall_seconds=value["wall_seconds"],  # type: ignore[arg-type]
            summary=value["summary"],  # type: ignore[arg-type]
            artifact_refs=MappingProxyType(dict(value["artifact_refs"])),  # type: ignore[arg-type]
            provenance=MappingProxyType(dict(value["provenance"])),  # type: ignore[arg-type]
            input_tokens=value["input_tokens"],  # type: ignore[arg-type]
            output_tokens=value["output_tokens"],  # type: ignore[arg-type]
            cache_tokens=value["cache_tokens"],  # type: ignore[arg-type]
            token_status=value["token_status"],  # type: ignore[arg-type]
            input_token_status=value["input_token_status"],  # type: ignore[arg-type]
            output_token_status=value["output_token_status"],  # type: ignore[arg-type]
            cache_token_status=value["cache_token_status"],  # type: ignore[arg-type]
            cost_usd=value["cost_usd"],  # type: ignore[arg-type]
            cost_status=value["cost_status"],  # type: ignore[arg-type]
            constraints_pass=value["constraints_pass"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise RunValidationError("task retry outcome receipt is invalid") from exc


class CampaignBenchmarkAdapter(Protocol):
    """One benchmark child delegating to an existing execution backend."""

    @property
    def benchmark_id(self) -> str: ...

    @property
    def evaluator_digest(self) -> str: ...

    def resolve_tasks(
        self,
        plan: CampaignCellPlan,
        sample_slots: Sequence[SampleSlot],
    ) -> tuple[TaskRef, ...]: ...

    def evaluate_task(
        self, candidate: GatewayCandidate, task: TaskRef
    ) -> TaskEvaluationOutcome: ...


def campaign_evaluator_digest(
    plans: Mapping[str, CampaignCellPlan],
    adapters: Mapping[str, CampaignBenchmarkAdapter],
) -> str:
    benchmark_ids = tuple(plans)
    if tuple(adapters) != benchmark_ids:
        raise ValueError("campaign adapters must match child plans")
    return canonical_digest(
        {
            "schema": "rsi-arena-campaign-evaluator",
            "children": [
                {
                    "benchmark_id": benchmark_id,
                    "plan_freeze_id": plans[benchmark_id].freeze_id,
                    "adapter_evaluator_digest": adapters[benchmark_id].evaluator_digest,
                }
                for benchmark_id in benchmark_ids
            ],
        }
    )


class CampaignController:
    """One frozen candidate, one slot, and ordered benchmark/task children."""

    def __init__(
        self,
        *,
        plans: Mapping[str, CampaignCellPlan],
        adapters: Mapping[str, CampaignBenchmarkAdapter],
        layout: RunLayout,
        recorder: RunRecorder,
        gateway: TrustedEvaluationGateway,
        max_concurrency: int = 1,
        invalid_cell_retries: int = 0,
        usage_ledger: UsageLedger | None = None,
    ) -> None:
        benchmark_ids = tuple(plans)
        if tuple(adapters) != benchmark_ids:
            raise RunValidationError("campaign controller child bindings disagree")
        self._plans = {
            benchmark_id: plans[benchmark_id] for benchmark_id in benchmark_ids
        }
        self._adapters = {
            benchmark_id: adapters[benchmark_id] for benchmark_id in benchmark_ids
        }
        self._layout = layout
        self._recorder = recorder
        self._gateway = gateway
        self._delivery_lock = threading.Lock()
        self._usage_ledger = usage_ledger
        for adapter in self._adapters.values():
            bind_gateway = getattr(adapter, "bind_trusted_gateway", None)
            if callable(bind_gateway):
                bind_gateway(gateway)
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency < 1
        ):
            raise ValueError("campaign concurrency must be a positive integer")
        self._max_concurrency = max_concurrency
        if (
            isinstance(invalid_cell_retries, bool)
            or not isinstance(invalid_cell_retries, int)
            or not 0 <= invalid_cell_retries <= 8
        ):
            raise ValueError("invalid cell retries must be in [0, 8]")
        self._invalid_cell_retries = invalid_cell_retries
        self._task_slots = threading.BoundedSemaphore(max_concurrency)
        self._evaluator_digest = campaign_evaluator_digest(plans, adapters)
        if recorder.evaluator_digest != self._evaluator_digest:
            raise RunValidationError("campaign controller bindings disagree")
        if any(
            benchmark_id != plan.benchmark
            or plan.cell_id != next(iter(plans.values())).cell_id
            for benchmark_id, plan in plans.items()
        ):
            raise RunValidationError("campaign child plans are misbound")

    @property
    def layout(self) -> RunLayout:
        """The single run tree owned by this controller/recorder pair."""

        return self._layout

    @property
    def candidate_evaluations_used(self) -> int:
        """Expose the recorder-owned durable cap count without duplicating it."""

        return self._recorder.n_candidate_evaluations

    @property
    def attempts_used(self) -> int:
        return self._recorder.n_attempts

    @property
    def all_attempts_terminal(self) -> bool:
        return self._recorder.all_attempts_terminal

    def start_attempt(
        self,
        attempt_id: str,
        *,
        max_attempts: int,
        kind: str | None = None,
        benchmark_slots: Mapping[str, Sequence[SampleSlot]] | None = None,
    ) -> AttemptRecord:
        return self._recorder.start_attempt(
            attempt_id,
            max_attempts=max_attempts,
            kind=kind,
            benchmark_slots=benchmark_slots,
        )

    def transition_attempt(self, attempt_id: str, decision: str) -> AttemptRecord:
        return self._recorder.transition_attempt(attempt_id, decision)

    def seal_attempt(self, attempt_id: str) -> AttemptRecord:
        return self._recorder.seal_attempt(attempt_id)

    def attempt(self, attempt_id: str) -> AttemptRecord | None:
        return self._recorder.attempt(attempt_id)

    def candidate_state(self, candidate_id: str) -> CampaignOccurrenceState:
        candidate = self._recorder.candidate_by_id(candidate_id)
        occurrence_key = candidate.native_candidate_id or candidate.candidate_id
        return self._recorder.campaign_candidate_state(
            candidate, occurrence_key=occurrence_key
        )

    @property
    def campaign_aggregate_kind(self) -> str | None:
        """Expose only the recorder's frozen aggregate authorization."""

        return self._recorder.campaign_aggregate_kind

    def _record_leaf_usage(
        self,
        *,
        handle,
        benchmark_id: str,
        slot: SampleSlot,
        outcome: TaskEvaluationOutcome,
    ) -> None:
        evaluation_id = handle.task_evaluation_id(benchmark_id, slot.sample_index)
        existing = [
            row
            for row in read_jsonl(self._layout.root / "usage.jsonl")
            if row.get("evaluation_id") == evaluation_id
        ]
        expected_binding = {
            "run_id": self._layout.run_id,
            "method": self._layout.method,
            "attempt_id": handle.attempt_id,
            "candidate_id": handle.candidate.candidate_id,
            "benchmark_id": benchmark_id,
            "benchmark_evaluation_id": handle.benchmark_evaluation_id(benchmark_id),
            "task_id": slot.task_id,
            "replicate": slot.replicate,
            "measurement_seed": slot.measurement_seed,
            "evaluation_id": evaluation_id,
        }
        existing_by_call: dict[str, Mapping[str, object]] = {}
        for row in existing:
            call_id = row.get("call_id")
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in existing_by_call
            ):
                raise RunValidationError("duplicate leaf usage call binding")
            if any(
                row.get(field) != value for field, value in expected_binding.items()
            ):
                raise RunValidationError("leaf usage binding is invalid")
            existing_by_call[call_id] = row
        ledger = self._usage_ledger
        if ledger is None:
            ledger = UsageLedger(
                self._layout.root / "usage.jsonl",
                method=self._layout.method,
                run_id=self._layout.run_id,
            )
        owns_ledger = self._usage_ledger is None
        raw_usage = outcome.provenance.get("usage")
        usage = raw_usage if isinstance(raw_usage, Mapping) else {}
        raw_calls = usage.get("calls")
        calls: tuple[Mapping[str, object], ...]
        if not usage:
            calls = ()
        elif isinstance(raw_calls, list) and all(
            isinstance(call, Mapping) for call in raw_calls
        ):
            calls = tuple(raw_calls)
            required_call_fields = {
                "role",
                "provider",
                "model",
                "input_tokens",
                "output_tokens",
                "cost_usd",
            }
            allowed_call_fields = required_call_fields | {
                "cache_read_tokens",
                "cache_tokens",
                "ordinal",
                "status",
                "response_id",
                "generation_id",
            }
            if any(
                not required_call_fields.issubset(call)
                or not set(call).issubset(allowed_call_fields)
                for call in calls
            ):
                raise RunValidationError("leaf model-call usage fields are invalid")
        else:
            raise RunValidationError(
                "leaf usage must contain explicit model-call facts"
            )
        if not calls and any(
            usage.get(name) is not None
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_tokens",
                "provider_reported_cost",
                "cost_usd",
            )
        ):
            raise RunValidationError("leaf aggregate usage lacks model-call facts")
        try:
            expected_call_ids: set[str] = set()
            for ordinal, call in enumerate(calls, start=1):
                logical_ordinal = call.get("ordinal", ordinal)
                if (
                    isinstance(logical_ordinal, bool)
                    or not isinstance(logical_ordinal, int)
                    or logical_ordinal != ordinal
                ):
                    raise RunValidationError("leaf model-call ordinal is invalid")
                provider_call_status = call.get("status", "completed")
                if provider_call_status not in {
                    "completed",
                    "in_flight",
                    "response_oversized",
                    "transport_failed",
                }:
                    raise RunValidationError("leaf provider-call status is invalid")
                provider_id_fields: dict[str, str | None] = {}
                for source_name, target_name in (
                    ("response_id", "provider_response_id"),
                    ("generation_id", "provider_generation_id"),
                ):
                    identity = call.get(source_name)
                    if identity is not None and (
                        not isinstance(identity, str)
                        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", identity)
                        is None
                    ):
                        raise RunValidationError(
                            "leaf provider-call identity is invalid"
                        )
                    provider_id_fields[target_name] = identity
                call_id = f"{evaluation_id}:model-call:{ordinal}"
                expected_call_ids.add(call_id)
                role = str(call.get("role", usage.get("role", "solver-agent")))
                provider = str(call.get("provider", usage.get("provider", "unknown")))
                model = str(call.get("model", usage.get("model", "unknown")))
                if not provider.strip() or not model.strip():
                    raise RunValidationError(
                        "leaf model-call provider/model is invalid"
                    )
                input_tokens = call.get("input_tokens")
                output_tokens = call.get("output_tokens")
                cache_read_tokens = call.get(
                    "cache_read_tokens", call.get("cache_tokens")
                )
                cost_usd = call.get("cost_usd")
                for value in (input_tokens, output_tokens, cache_read_tokens):
                    if value is not None and (
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 0
                    ):
                        raise RunValidationError(
                            "leaf model-call token accounting is invalid"
                        )
                if cost_usd is not None and (
                    isinstance(cost_usd, bool)
                    or not isinstance(cost_usd, (int, float))
                    or not math.isfinite(float(cost_usd))
                    or float(cost_usd) < 0.0
                ):
                    raise RunValidationError(
                        "leaf model-call cost accounting is invalid"
                    )
                token_status = (
                    "reported"
                    if input_tokens is not None and output_tokens is not None
                    else "unverified"
                    if input_tokens is not None or output_tokens is not None
                    else "missing"
                )
                fields: dict[str, object] = {
                    **{
                        name: value
                        for name, value in expected_binding.items()
                        if name not in {"run_id", "method"}
                    },
                    "replicate": slot.replicate,
                    "measurement_seed": slot.measurement_seed,
                    "request_id": f"{slot.task_id}:{slot.replicate}",
                    "call_id": call_id,
                    "logical_call_ordinal": logical_ordinal,
                    "provider_response_id": provider_id_fields["provider_response_id"],
                    "provider_generation_id": provider_id_fields[
                        "provider_generation_id"
                    ],
                    "provider_call_status": provider_call_status,
                    "wall_seconds": (
                        float(outcome.wall_seconds or 0.0) if ordinal == 1 else 0.0
                    ),
                    "token_status": token_status,
                    "cost_usd": cost_usd,
                    "provider_reported_cost": cost_usd,
                    "cost_source": "provider" if cost_usd is not None else "unknown",
                    "cost_status": "reported" if cost_usd is not None else "missing",
                    "failure_status": (
                        None if outcome.outcome == "measured" else "inconclusive"
                    ),
                    "spend_kind": "evaluation",
                    "provider": provider,
                    "token_source": "provider"
                    if token_status != "missing"
                    else "unknown",
                    "input_token_status": (
                        "reported" if input_tokens is not None else "missing"
                    ),
                    "output_token_status": (
                        "reported" if output_tokens is not None else "missing"
                    ),
                    "cache_token_status": (
                        "reported" if cache_read_tokens is not None else "missing"
                    ),
                    "cache_status": (
                        "hit"
                        if isinstance(cache_read_tokens, int) and cache_read_tokens > 0
                        else "miss"
                        if cache_read_tokens is not None
                        else "unknown"
                    ),
                }
                if input_tokens is not None:
                    fields["tokens_in"] = input_tokens
                if output_tokens is not None:
                    fields["tokens_out"] = output_tokens
                if cache_read_tokens is not None:
                    fields["cache_read_tokens"] = cache_read_tokens
                existing_row = existing_by_call.get(call_id)
                if existing_row is not None:
                    comparable = {
                        "role": role,
                        "model": model,
                        **fields,
                    }
                    if any(
                        existing_row.get(name) != value
                        for name, value in comparable.items()
                    ):
                        raise RunValidationError("leaf usage call changed on replay")
                    continue
                ledger.record(role, model=model, **fields)
            if not set(existing_by_call).issubset(expected_call_ids):
                raise RunValidationError("leaf usage call set changed on replay")
        finally:
            if owns_ledger:
                ledger.close()

    def evaluate_candidate(
        self,
        candidate_workspace: Path,
        *,
        native_candidate_id: str,
        attempt_id: str,
        role: str,
        parent_candidate_id: str | None = None,
    ) -> CampaignEvaluationResult:
        bound_attempt_id = self._recorder.validate_git_native_candidate_admission(
            native_candidate_id=native_candidate_id,
            attempt_id=attempt_id,
            role=role,
            parent_candidate_id=parent_candidate_id,
        )
        handle = None

        def reserve_before_gate(
            staged: Path,
            gateway_commit_sha: str,
            tree_sha: str,
            gateway_root_commit_sha: str,
        ) -> None:
            nonlocal handle
            handle = self._recorder.freeze_and_resume_or_start_campaign_evaluation(
                staged,
                gateway_commit_sha=gateway_commit_sha,
                tree_sha=tree_sha,
                gateway_root_commit_sha=gateway_root_commit_sha,
                native_candidate_id=native_candidate_id,
                role=role,
                parent_candidate_id=parent_candidate_id,
                attempt_id=bound_attempt_id,
            )

        try:
            issued = self._gateway.materialize(
                Path(candidate_workspace), before_gate=reserve_before_gate
            )
        except GatewayRejected as exc:
            if handle is None:
                raise
            diagnostic = (
                exc.violations[0]
                if exc.violations
                else "candidate workspace failed trusted materialization"
            )[:500]
            return self._evaluate_handle(
                handle,
                issued=None,
                terminal_failure=TaskEvaluationOutcome(
                    outcome="measured",
                    score=0.0,
                    summary="candidate workspace was rejected",
                    provenance={
                        "candidate_boundary": "gateway_rejected",
                        "public_diagnostic": diagnostic,
                    },
                ),
            )
        except (GatewayError, GitError):
            if handle is None:
                raise
            return self._evaluate_handle(
                handle,
                issued=None,
                terminal_failure=TaskEvaluationOutcome(
                    outcome="inconclusive",
                    summary="trusted candidate gateway failed",
                    provenance={"trusted_boundary": "gateway_failure"},
                ),
            )
        self._gateway.validate(issued)
        if handle is None:
            raise RunValidationError("gateway omitted candidate occurrence reservation")
        return self._evaluate_handle(handle, issued=issued)

    def prepare_native_workspace(
        self,
        candidate_workspace: Path,
        *,
        occurrence_key: str,
        attempt_id: str,
        role: str,
        parent_candidate_id: str | None = None,
    ) -> tuple[CandidateRecord, GatewayCandidate]:
        """Publish the evaluated tree before admitting a native occurrence."""

        bound_attempt_id = self._recorder.validate_git_native_candidate_admission(
            native_candidate_id=occurrence_key,
            attempt_id=attempt_id,
            role=role,
            parent_candidate_id=parent_candidate_id,
        )
        issued = self._gateway.materialize(Path(candidate_workspace))
        self._gateway.validate(issued)
        candidate = self._recorder.freeze_git_candidate(
            issued.checkout_path,
            gateway_commit_sha=issued.commit_sha,
            tree_sha=issued.tree_sha,
            gateway_root_commit_sha=issued.root_commit_sha,
            native_candidate_id=occurrence_key,
            role=role,
            parent_candidate_id=parent_candidate_id,
            attempt_id=bound_attempt_id,
        )
        if candidate.snapshot_digest != f"git-tree-sha1:{issued.tree_sha}":
            raise RunValidationError("native occurrence changed canonical snapshot")
        self._gateway.validate(issued)
        return candidate, issued

    def evaluate_prepared_candidate(
        self,
        candidate: CandidateRecord,
        issued: GatewayCandidate,
    ) -> CampaignEvaluationResult:
        """Evaluate one already durable canonical native candidate."""

        self._gateway.validate(issued)
        if candidate.snapshot_digest != f"git-tree-sha1:{issued.tree_sha}":
            raise RunValidationError("prepared candidate snapshot is misbound")
        handle = self._recorder.resume_or_start_campaign_evaluation(candidate)
        return self._evaluate_handle(handle, issued=issued)

    def result_for_occurrence(
        self, occurrence_key: str
    ) -> CampaignOccurrenceState | None:
        """Read one caller-known occurrence from the recorder-owned lifecycle."""

        return self._recorder.campaign_occurrence_state(occurrence_key)

    def campaign_terminal_results(self) -> tuple[CampaignEvaluationResult, ...]:
        """Expose only recorder-validated durable campaign terminals."""

        return self._recorder.campaign_terminal_results()

    @property
    def delivered_terminal_ids(self) -> frozenset[str]:
        return self._recorder.delivered_terminal_ids

    def record_evaluation_delivered(self, result: CampaignEvaluationResult) -> None:
        """Record one minimal idempotent receipt for a delivered terminal."""

        if (
            result.run_id != self._layout.run_id
            or result.evaluator_digest != self._evaluator_digest
        ):
            raise RunValidationError("evaluation delivery result is misbound")
        with self._delivery_lock:
            self._recorder.record_delivery_receipt(result)

    def _retry_attempt_history(
        self,
        *,
        handle,
        benchmark_id: str,
        slot: SampleSlot,
    ) -> tuple[TaskEvaluationOutcome | None, ...]:
        """Recover append-only same-slot reads; ``None`` is an uncertain read."""

        task_evaluation_id = handle.task_evaluation_id(benchmark_id, slot.sample_index)
        binding = {
            "candidate_id": handle.candidate.candidate_id,
            "attempt_id": handle.attempt_id,
            "candidate_evaluation_id": handle.candidate_evaluation_id,
            "task_evaluation_id": task_evaluation_id,
            "benchmark_id": benchmark_id,
            **slot.as_dict(),
            "evaluator_digest": self._evaluator_digest,
        }
        rows = self._recorder.task_evaluation_attempt_events(task_evaluation_id)
        by_ordinal: dict[int, dict[str, Mapping[str, object]]] = {}
        for row in rows:
            if any(row.get(name) != expected for name, expected in binding.items()):
                raise RunValidationError("task retry receipt changed its frozen slot")
            ordinal = row.get("retry_ordinal")
            if (
                isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or not 0 <= ordinal <= self._invalid_cell_retries
            ):
                raise RunValidationError("task retry receipt ordinal is invalid")
            phase = (
                "started"
                if row["event_type"] == "task_evaluation_attempt_started"
                else "terminal"
            )
            phases = by_ordinal.setdefault(ordinal, {})
            if phase in phases:
                raise RunValidationError("task retry receipt phase is duplicated")
            phases[phase] = row
        if not by_ordinal:
            return ()
        ordinals = tuple(sorted(by_ordinal))
        if ordinals != tuple(range(ordinals[-1] + 1)):
            raise RunValidationError("task retry receipt sequence has a gap")
        recovered: list[TaskEvaluationOutcome | None] = []
        for ordinal in ordinals:
            phases = by_ordinal[ordinal]
            if "started" not in phases:
                raise RunValidationError("task retry terminal has no start")
            terminal = phases.get("terminal")
            recovered.append(
                None
                if terminal is None
                else _retry_outcome_from_payload(terminal.get("task_outcome"))
            )
        if any(outcome is None for outcome in recovered[:-1]):
            raise RunValidationError("task retry continued after an uncertain read")
        if any(
            outcome is not None and outcome.outcome == "measured"
            for outcome in recovered[:-1]
        ):
            raise RunValidationError("task retry continued after a measured read")
        return tuple(recovered)

    def _record_retry_phase(
        self,
        event_type: str,
        *,
        handle,
        benchmark_id: str,
        slot: SampleSlot,
        ordinal: int,
        outcome: TaskEvaluationOutcome | None = None,
    ) -> None:
        fields: dict[str, object] = {
            "candidate_id": handle.candidate.candidate_id,
            "candidate_evaluation_id": handle.candidate_evaluation_id,
            "task_evaluation_id": handle.task_evaluation_id(
                benchmark_id, slot.sample_index
            ),
            "benchmark_id": benchmark_id,
            **slot.as_dict(),
            "retry_ordinal": ordinal,
            "evaluator_digest": self._evaluator_digest,
        }
        if outcome is not None:
            fields["task_outcome"] = _retry_outcome_payload(outcome)
        self._recorder.event(event_type, **fields)

    def _evaluate_task_with_retries(
        self,
        *,
        handle,
        issued: GatewayCandidate,
        benchmark_id: str,
        slot: SampleSlot,
        task: TaskRef,
    ) -> TaskEvaluationOutcome:
        history = list(
            self._retry_attempt_history(
                handle=handle,
                benchmark_id=benchmark_id,
                slot=slot,
            )
        )
        interrupted_read = bool(history and history[-1] is None)
        if interrupted_read:
            interrupted = TaskEvaluationOutcome(
                outcome="inconclusive",
                summary="interrupted_task_evaluation_attempt",
                provenance=MappingProxyType(
                    {
                        "reason": "interrupted_task_evaluation_attempt",
                        "benchmark": benchmark_id,
                    }
                ),
            )
            self._record_retry_phase(
                "task_evaluation_attempt_terminal",
                handle=handle,
                benchmark_id=benchmark_id,
                slot=slot,
                ordinal=len(history) - 1,
                outcome=interrupted,
            )
            history[-1] = interrupted
        attempts = [
            outcome for outcome in history if isinstance(outcome, TaskEvaluationOutcome)
        ]
        if interrupted_read:
            # The adapter may have submitted a paid request before the process
            # died. Without a lower-level settlement receipt, consuming another
            # retry could duplicate that spend, so the slot closes inconclusive.
            return _apply_retry_cost_ceiling(
                _merge_retry_outcomes(attempts),
                ceiling_usd=getattr(
                    self._adapters[benchmark_id],
                    "cell_cost_acceptance_ceiling_usd",
                    None,
                ),
            )
        next_ordinal = len(history)
        for ordinal in range(next_ordinal, self._invalid_cell_retries + 1):
            if attempts and attempts[-1].outcome == "measured":
                break
            self._record_retry_phase(
                "task_evaluation_attempt_started",
                handle=handle,
                benchmark_id=benchmark_id,
                slot=slot,
                ordinal=ordinal,
            )
            self._gateway.validate(issued)
            with self._task_slots:
                outcome = self._adapters[benchmark_id].evaluate_task(issued, task)
            self._gateway.validate(issued)
            if not isinstance(outcome, TaskEvaluationOutcome):
                raise TypeError("campaign adapter returned an invalid task outcome")
            self._record_retry_phase(
                "task_evaluation_attempt_terminal",
                handle=handle,
                benchmark_id=benchmark_id,
                slot=slot,
                ordinal=ordinal,
                outcome=outcome,
            )
            attempts.append(outcome)
        return _apply_retry_cost_ceiling(
            _merge_retry_outcomes(attempts),
            ceiling_usd=getattr(
                self._adapters[benchmark_id],
                "cell_cost_acceptance_ceiling_usd",
                None,
            ),
        )

    def _evaluate_handle(
        self,
        handle,
        *,
        issued: GatewayCandidate | None,
        terminal_failure: TaskEvaluationOutcome | None = None,
    ) -> CampaignEvaluationResult:
        selected = {
            benchmark_id
            for benchmark_id, _slot, _evaluation_id in (
                handle.scoped_task_evaluation_ids
            )
        }
        benchmark_ids = tuple(
            benchmark_id for benchmark_id in self._plans if benchmark_id in selected
        )
        if not benchmark_ids or selected != set(benchmark_ids):
            raise RunValidationError("campaign evaluation benchmark subset is invalid")

        jobs: list[tuple[str, SampleSlot, TaskRef | None]] = []
        for benchmark_id in benchmark_ids:
            adapter = self._adapters[benchmark_id]
            slots = tuple(
                slot
                for child_benchmark, slot, _evaluation_id in (
                    handle.scoped_task_evaluation_ids
                )
                if child_benchmark == benchmark_id
            )
            tasks: tuple[TaskRef, ...] = ()
            if terminal_failure is None:
                tasks = adapter.resolve_tasks(self._plans[benchmark_id], slots)
                if tuple(task.canonical_identity for task in tasks) != tuple(
                    (benchmark_id, slot.task_id) for slot in slots
                ):
                    raise RunValidationError(
                        "campaign adapter changed persisted benchmark/task slots"
                    )
            task_by_index = (
                {
                    slot.sample_index: task
                    for slot, task in zip(slots, tasks, strict=True)
                }
                if terminal_failure is None
                else {}
            )
            for slot, _evaluation_id in self._recorder.missing_campaign_tasks(
                handle, benchmark_id
            ):
                jobs.append(
                    (
                        benchmark_id,
                        slot,
                        task_by_index.get(slot.sample_index),
                    )
                )

        def evaluate(
            benchmark_id: str,
            slot: SampleSlot,
            task: TaskRef | None,
        ) -> TaskEvaluationOutcome:
            if terminal_failure is not None:
                return terminal_failure
            if issued is None or task is None:
                raise RunValidationError("campaign task execution is unavailable")
            if getattr(self, "_invalid_cell_retries", 0) == 0:
                self._gateway.validate(issued)
                slots = getattr(self, "_task_slots", None)
                with slots if slots is not None else nullcontext():
                    outcome = self._adapters[benchmark_id].evaluate_task(issued, task)
                self._gateway.validate(issued)
                return outcome
            return self._evaluate_task_with_retries(
                handle=handle,
                issued=issued,
                benchmark_id=benchmark_id,
                slot=slot,
                task=task,
            )

        def persist(
            benchmark_id: str,
            slot: SampleSlot,
            outcome: TaskEvaluationOutcome,
        ) -> None:
            self._recorder.finish_task_evaluation(
                handle,
                benchmark_id,
                slot,
                outcome,
            )
            if terminal_failure is None:
                self._record_leaf_usage(
                    handle=handle,
                    benchmark_id=benchmark_id,
                    slot=slot,
                    outcome=outcome,
                )

        durable_outcome = getattr(self._recorder, "task_evaluation_outcome", None)
        if terminal_failure is None and callable(durable_outcome):
            for benchmark_id, slot, _evaluation_id in handle.scoped_task_evaluation_ids:
                durable = durable_outcome(handle, benchmark_id, slot)
                if durable is not None:
                    self._record_leaf_usage(
                        handle=handle,
                        benchmark_id=benchmark_id,
                        slot=slot,
                        outcome=durable,
                    )

        first_error: BaseException | None = None
        if self._max_concurrency == 1:
            for benchmark_id, slot, task in jobs:
                try:
                    persist(
                        benchmark_id,
                        slot,
                        evaluate(benchmark_id, slot, task),
                    )
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
        else:
            with ThreadPoolExecutor(
                max_workers=self._max_concurrency,
                thread_name_prefix="arena-task",
            ) as pool:
                futures = {
                    pool.submit(evaluate, benchmark_id, slot, task): (
                        benchmark_id,
                        slot,
                    )
                    for benchmark_id, slot, task in jobs
                }
                for future in as_completed(futures):
                    benchmark_id, slot = futures[future]
                    try:
                        persist(benchmark_id, slot, future.result())
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
        if first_error is not None:
            raise first_error

        benchmark_results = {
            benchmark_id: self._recorder.finish_benchmark_evaluation(
                handle, benchmark_id
            )
            for benchmark_id in benchmark_ids
        }
        return self._recorder.finish_campaign_evaluation(handle, benchmark_results)


__all__ = [
    "CampaignBenchmarkAdapter",
    "CampaignController",
    "campaign_evaluator_digest",
]
