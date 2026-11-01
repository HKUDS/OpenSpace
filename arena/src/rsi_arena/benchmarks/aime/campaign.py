"""Method-neutral AIME campaign task resolution and execution."""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from pathlib import Path

from rsi_arena.benchmarks.harbor_runtime.agent_bridge import (
    CANDIDATE_RUNTIME_FAILURE_KEY,
    candidate_runtime_failure_from_reason,
)
from rsi_arena.contracts import CellBudget, TaskRef, canonical_digest
from rsi_arena.evaluation import (
    BenchAdapter,
    CandidateFailure,
    CellResult,
    UnmeasuredCell,
    bench_isolation_capabilities,
)
from rsi_arena.evaluation_gateway import (
    GatewayCandidate,
    GatewayCandidateRequired,
    TrustedEvaluationGateway,
)
from rsi_arena.manifests import SampleSlot
from rsi_arena.run_layout import (
    RunLayout,
    RunValidationError,
    TaskEvaluationOutcome,
    atomic_write_bytes,
)
from rsi_arena.runtime_campaign import (
    CampaignCellPlan,
    RuntimeCampaignError,
    runtime_realization_record,
)

_TRAJECTORY_SOURCE = Path("agent/trajectory.json")
_LOG = logging.getLogger(__name__)


def _publish_evaluation_task_trajectory(
    layout: RunLayout, trial_artifact_root: Path, *, task_id: str
) -> dict[str, str]:
    """Best-effort publish one raw evaluation trajectory in the trusted run."""

    try:
        payload = (Path(trial_artifact_root) / _TRAJECTORY_SOURCE).read_bytes()
    except OSError:
        _LOG.warning(
            "evaluation trajectory artifact dropped: task=%s stage=source-read",
            task_id,
        )
        return {}
    try:
        hex_digest = hashlib.sha256(payload).hexdigest()
        relative = Path("trajectory/raw/sha256") / f"{hex_digest}.bin"
        atomic_write_bytes(layout.root / relative, payload)
    except Exception:
        _LOG.warning(
            "evaluation trajectory artifact dropped: task=%s stage=durable-write",
            task_id,
        )
        return {}
    return {
        "trajectory_ref": relative.as_posix(),
        "trajectory_digest": f"sha256:{hex_digest}",
    }


def _scoped_tasks(tasks: Sequence[TaskRef]) -> Mapping[str, TaskRef]:
    scoped: dict[str, TaskRef] = {}
    for task in tasks:
        if task.benchmark_id and task.benchmark_id != "aime":
            raise ValueError("AIME adapter received another benchmark scope")
        item = (
            task
            if task.benchmark_id == "aime"
            else dataclasses.replace(task, benchmark_id="aime")
        )
        if not item.task_id or item.task_id in scoped:
            raise ValueError("AIME adapter task IDs must be non-empty and unique")
        scoped[item.task_id] = item
    if not scoped:
        raise ValueError("AIME adapter needs tasks")
    return scoped


def _candidate_entry(root: Path, entrypoint: str) -> Path:
    relative = Path(entrypoint)
    if relative.is_absolute() or ".." in relative.parts:
        raise RunValidationError("candidate benchmark entrypoint must be relative")
    checkout = root.resolve(strict=True)
    selected = (checkout / relative).resolve(strict=True)
    try:
        selected.relative_to(checkout)
    except ValueError as exc:
        raise RunValidationError(
            "candidate benchmark entrypoint escapes snapshot"
        ) from exc
    return selected


def _cost_status(observation: object) -> tuple[float | None, str]:
    cost = getattr(observation, "cost_usd", None)
    status = str(getattr(observation, "cost_status", "auto"))
    if status == "auto":
        status = "reported" if cost is not None else "missing"
    if status not in {"reported", "missing", "unverified"}:
        status = "unverified"
    if status == "reported" and (
        not isinstance(cost, (int, float))
        or isinstance(cost, bool)
        or not math.isfinite(float(cost))
        or float(cost) < 0.0
    ):
        status = "unverified"
    return (float(cost) if status == "reported" else None), status


def _token_usage(metrics: object) -> dict[str, object]:
    raw = metrics if isinstance(metrics, Mapping) else {}
    result: dict[str, object] = {}
    statuses: dict[str, str] = {}
    for metric_name, value_name, status_name in (
        ("n_input_tokens", "input_tokens", "input_token_status"),
        ("n_output_tokens", "output_tokens", "output_token_status"),
        ("n_cache_tokens", "cache_read_tokens", "cache_token_status"),
    ):
        value = raw.get(metric_name)
        if value is None:
            status = "missing"
        elif (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0.0
            or not float(value).is_integer()
        ):
            status = "unverified"
        else:
            status = "reported"
            result[value_name] = int(value)
        statuses[status_name] = status
    if (
        statuses["input_token_status"] == "reported"
        and statuses["cache_token_status"] == "reported"
        and int(result["cache_read_tokens"]) > int(result["input_tokens"])
    ):
        statuses["cache_token_status"] = "unverified"
        result.pop("cache_read_tokens", None)
    primary = {statuses["input_token_status"], statuses["output_token_status"]}
    token_status = (
        "reported"
        if primary == {"reported"}
        else "missing"
        if primary == {"missing"}
        else "unverified"
    )
    return {
        **result,
        **statuses,
        "token_source": ("provider" if "reported" in statuses.values() else "unknown"),
        "token_status": token_status,
        "cache_status": (
            "unknown"
            if statuses["cache_token_status"] != "reported"
            else "hit"
            if int(result["cache_read_tokens"]) > 0
            else "miss"
        ),
        "provider": "unknown",
        "model": "unknown",
    }


def _adapter_evaluator_digest(
    bench: BenchAdapter,
    *,
    candidate_entrypoint: str,
    cell_budget: CellBudget,
    tasks: Mapping[str, TaskRef] | None = None,
) -> str:
    scoped_tasks = tasks if tasks is not None else _scoped_tasks(bench.list_tasks())
    return canonical_digest(
        {
            "schema": "aime-campaign-adapter",
            "bench": bench.identity().digest,
            "entrypoint": str(candidate_entrypoint),
            "tasks": list(scoped_tasks),
            "cell_budget": {
                "wall_seconds": cell_budget.wall_seconds,
                "max_cost_usd": cell_budget.max_cost_usd,
            },
        }
    )


class AimeCampaignBenchmarkAdapter:
    """Resolve persisted AIME slots and execute one trusted task at a time."""

    benchmark_id = "aime"

    def __init__(
        self,
        bench: BenchAdapter,
        *,
        layout: RunLayout,
        trusted_gateway: TrustedEvaluationGateway | None = None,
        candidate_entrypoint: str = ".",
        cell_budget: CellBudget = CellBudget(),
        defer_trusted_gateway_binding: bool = False,
        provider_name: str = "unknown",
        model_name: str = "unknown",
    ) -> None:
        self._bench = bench
        self._tasks = _scoped_tasks(bench.list_tasks())
        self._gateway = trusted_gateway
        self._layout = layout
        self._entrypoint = str(candidate_entrypoint)
        self._budget = cell_budget
        self._provider_name = str(provider_name)
        self._model_name = str(model_name)
        self._primary = bench.metric_schema().primary.name
        self._required_metrics = {self._primary} | {
            spec.name for spec in bench.metric_schema().constraints
        }
        self._isolation = bench_isolation_capabilities(bench)
        if (
            self._isolation.fresh_candidate_worker
            and trusted_gateway is None
            and not defer_trusted_gateway_binding
        ):
            raise ValueError("fresh AIME evaluation requires its issuing gateway")
        self._evaluator_digest = _adapter_evaluator_digest(
            bench,
            candidate_entrypoint=self._entrypoint,
            cell_budget=self._budget,
            tasks=self._tasks,
        )

    def bind_trusted_gateway(self, gateway: TrustedEvaluationGateway) -> None:
        """Bind the controller's issuing gateway before any fresh task runs."""

        if self._gateway is not None and self._gateway is not gateway:
            raise RunValidationError("AIME adapter is already bound to another gateway")
        self._gateway = gateway

    @property
    def evaluator_digest(self) -> str:
        return self._evaluator_digest

    @property
    def execution_backend(self) -> BenchAdapter:
        return self._bench

    @property
    def cell_cost_acceptance_ceiling_usd(self) -> float | None:
        """The aggregate same-slot ceiling, including apparatus retries."""

        return self._budget.max_cost_usd

    def resolve_tasks(
        self,
        plan: CampaignCellPlan,
        sample_slots: Sequence[SampleSlot],
    ) -> tuple[TaskRef, ...]:
        slots = tuple(sample_slots)
        if plan.benchmark != self.benchmark_id:
            raise RuntimeCampaignError(
                "AIME adapter requires its frozen AIME campaign plan"
            )
        runtime_realization_record(plan, slots)
        try:
            return tuple(self._tasks[slot.task_id] for slot in slots)
        except KeyError as exc:
            raise RunValidationError("AIME plan selected an unavailable task") from exc

    def evaluate_task(
        self,
        candidate: GatewayCandidate,
        task: TaskRef,
        *,
        pre_cleanup_trial_hook: Callable[[Path], None] | None = None,
    ) -> TaskEvaluationOutcome:
        if task.canonical_identity != (self.benchmark_id, task.task_id):
            raise RunValidationError("AIME task lost benchmark scope")
        trajectory_refs: dict[str, str] = {}
        trial_hook = pre_cleanup_trial_hook
        if trial_hook is None:

            def preserve_b_trajectory(trial_artifact_root: Path) -> None:
                trajectory_refs.update(
                    _publish_evaluation_task_trajectory(
                        self._layout, trial_artifact_root, task_id=task.task_id
                    )
                )

            trial_hook = preserve_b_trajectory
        started = time.monotonic()
        fresh = nullcontext(candidate.checkout_path)
        if self._isolation.fresh_candidate_worker:
            if not isinstance(candidate, GatewayCandidate):
                raise GatewayCandidateRequired(
                    "fresh AIME evaluation refuses an ordinary directory"
                )
            if self._gateway is None:
                raise GatewayCandidateRequired(
                    "fresh AIME evaluation requires its issuing gateway"
                )
            self._gateway.validate(candidate)
            fresh = self._gateway.fresh_checkout(candidate)
        elif isinstance(candidate, GatewayCandidate) and self._gateway is not None:
            self._gateway.validate(candidate)
        try:
            with fresh as checkout:
                try:
                    entrypoint = _candidate_entry(Path(checkout), self._entrypoint)
                except (OSError, RunValidationError):
                    return TaskEvaluationOutcome(
                        outcome="measured",
                        score=0.0,
                        wall_seconds=time.monotonic() - started,
                        summary="candidate entrypoint is invalid",
                    )
                try:
                    observation = self._bench.run_cell(
                        entrypoint,
                        task,
                        0,
                        self._budget,
                        pre_cleanup_trial_hook=trial_hook,
                    )
                except Exception:
                    return TaskEvaluationOutcome(
                        outcome="inconclusive",
                        wall_seconds=time.monotonic() - started,
                        summary="trusted AIME backend raised an exception",
                        artifact_refs=trajectory_refs,
                    )
        except Exception:
            return TaskEvaluationOutcome(
                outcome="inconclusive",
                wall_seconds=time.monotonic() - started,
                summary="trusted fresh checkout failed",
                artifact_refs=trajectory_refs,
            )
        wall_seconds = max(
            time.monotonic() - started,
            float(getattr(observation, "duration_s", 0.0)),
        )
        metrics = getattr(observation, "raw_metrics", {}) or {}
        cost, cost_status = _cost_status(observation)
        token_usage = {
            **_token_usage(metrics),
            "provider": self._provider_name,
            "model": self._model_name,
            "role": "solver-agent",
        }
        token_usage["calls"] = [
            {
                "role": "solver-agent",
                "provider": self._provider_name,
                "model": self._model_name,
                **dict(call),
            }
            for call in getattr(observation, "provider_calls", ())
        ]
        provenance: dict[str, object] = {
            "usage": token_usage,
            **dict(getattr(observation, "diagnostics", {})),
        }
        timeout_observed = metrics.get("agent_timeout_observed")
        if (
            isinstance(timeout_observed, (int, float))
            and not isinstance(timeout_observed, bool)
            and float(timeout_observed) in {0.0, 1.0}
        ):
            timed_out = float(timeout_observed) == 1.0
            provenance["agent_state"] = "timed_out" if timed_out else "completed"
            provenance["answer_state"] = (
                "parsed"
                if provenance.get("artifact_state") == "present"
                else "unparsed"
            )
            if timed_out:
                provenance["timeout_diagnostics"] = {
                    "response_count": len(token_usage["calls"]),
                    "first_response_latency_seconds": None,
                    "last_terminal_activity_seconds": None,
                    "answer_written_before_timeout": (
                        provenance.get("artifact_state") == "present"
                    ),
                    "provider_waiting_at_deadline": None,
                    "provider_diagnostics": None,
                }
            if pre_cleanup_trial_hook is not None:
                provenance["development_agent_status"] = (
                    "timeout" if timed_out else "completed"
                )
        common = {
            "wall_seconds": wall_seconds,
            "input_tokens": token_usage.get("input_tokens"),
            "output_tokens": token_usage.get("output_tokens"),
            "cache_tokens": token_usage.get("cache_read_tokens"),
            "token_status": token_usage["token_status"],
            "input_token_status": token_usage["input_token_status"],
            "output_token_status": token_usage["output_token_status"],
            "cache_token_status": token_usage["cache_token_status"],
            "cost_usd": cost,
            "cost_status": cost_status,
            "provenance": provenance,
            "artifact_refs": trajectory_refs,
        }
        values_valid = (
            isinstance(metrics, Mapping)
            and self._required_metrics.issubset(metrics)
            and all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                for value in metrics.values()
            )
        )
        if isinstance(observation, CandidateFailure) and values_valid:
            candidate_diagnostic = candidate_runtime_failure_from_reason(
                observation.reason
            )
            candidate_common = common
            summary = str(observation.reason)
            if candidate_diagnostic is not None:
                candidate_common = {
                    **common,
                    "provenance": {
                        **common["provenance"],
                        CANDIDATE_RUNTIME_FAILURE_KEY: candidate_diagnostic,
                    },
                }
                summary = "candidate runtime failed"
            return TaskEvaluationOutcome(
                outcome="measured",
                score=0.0,
                summary=summary,
                **candidate_common,
            )
        score = metrics.get(self._primary) if isinstance(metrics, Mapping) else None
        if isinstance(observation, CellResult) and values_valid:
            return TaskEvaluationOutcome(
                outcome="measured", score=float(score), **common
            )
        reason = (
            observation.reason
            if isinstance(observation, UnmeasuredCell)
            else "invalid_aime_observation"
        )
        failure_provenance = dict(common["provenance"])
        if isinstance(observation, UnmeasuredCell):
            failure_provenance.update(
                {
                    "reason": str(observation.reason)[:128],
                    "benchmark": self.benchmark_id,
                }
            )
            exception_type = observation.diagnostics.get("exception_type")
            if isinstance(exception_type, str) and exception_type:
                failure_provenance["exception_type"] = exception_type[:128]
        return TaskEvaluationOutcome(
            outcome="inconclusive",
            summary=str(reason),
            **{**common, "provenance": failure_provenance},
        )


__all__ = ["AimeCampaignBenchmarkAdapter"]
