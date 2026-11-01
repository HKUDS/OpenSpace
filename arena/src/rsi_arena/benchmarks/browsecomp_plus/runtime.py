"""BrowseComp-Plus task runtime and thin Arena-facing adapters."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from rsi_arena.benchmarks.harbor_runtime.agent_bridge import (
    CANDIDATE_RUNTIME_FAILURE_KEY,
)
from rsi_arena.benchmarks.harbor_runtime.agent_worker import AgentExecutionContext
from rsi_arena.benchmarks.harbor_runtime.trajectory import (
    harvest_development_agent_trajectory,
)
from rsi_arena.campaign_evaluation import CampaignBenchmarkAdapter
from rsi_arena.contracts import TaskRef
from rsi_arena.development import (
    DevelopmentArtifact,
    DevelopmentExecution,
    DevelopmentMaterial,
)
from rsi_arena.evaluation_gateway import TrustedEvaluationGateway
from rsi_arena.manifests import SampleSlot
from rsi_arena.run_layout import RunValidationError, TaskEvaluationOutcome
from rsi_arena.runtime_campaign import (
    CampaignCellPlan,
    RuntimeCampaignError,
    assert_development_slots,
)

from .contracts import PublicTask, TrustedTask
from .evaluator import BrowseCompPlusEvaluator, TaskEvaluation
from .retrieval import RetrievalTrajectory
from .search_transport import (
    BrowseCompRetrievalService,
    BrowseCompSearchClient,
    SearchTransportError,
)
from .usage import (
    ModelUsage,
    PartialModelUsageError,
    aggregate_model_usage,
    model_usage_calls_from_mapping,
)

_BENCHMARK_ID = "browsecomp_plus"
_EVALUATOR_ID = "browsecomp-plus-task-evaluator"
_LOG = logging.getLogger(__name__)


class InnerAgentCallable(Protocol):
    """The existing shared ``context/model_runtime/config`` call shape."""

    def __call__(
        self,
        *,
        context: AgentExecutionContext,
        model_runtime: Mapping[str, object],
        config: Mapping[str, object],
    ) -> object: ...


class InnerAgentCallableFactory(Protocol):
    """Resolve that shared callable from the current frozen candidate source."""

    def __call__(self, candidate_source: object) -> InnerAgentCallable: ...


@dataclass(frozen=True)
class _TrustedInnerAgentTerminal:
    """Package-private runner terminal with an optional trusted Harbor trial."""

    candidate: object
    trial_artifact_root: Path | None = None
    candidate_runtime_failure: Mapping[str, str] | None = None
    model_usage: ModelUsage | None = None


class _InnerAgentTaskTimeout(RuntimeError):
    """The trusted outer task deadline expired before the runner returned."""


def _positive_timeout(value: object) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise ValueError("BrowseComp task wall timeout must be positive")
    return float(value)


def _json_mapping(value: Mapping[str, object], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    try:
        copied = json.loads(json.dumps(dict(value), allow_nan=False))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be finite JSON") from exc
    if not isinstance(copied, dict):  # pragma: no cover - guarded above
        raise TypeError(f"{label} must be a mapping")
    return MappingProxyType(copied)


class BrowseCompTaskEnvironment:
    """Candidate-visible environment: search only, backed by one UDS endpoint."""

    __slots__ = ("_client",)

    def __init__(self, client: BrowseCompSearchClient) -> None:
        self._client = client

    @property
    def search_endpoint(self) -> str:
        return self._client.endpoint

    def search(self, query: str) -> tuple[dict[str, str | None], ...]:
        return self._client.search(query)


@dataclass(frozen=True)
class BrowseCompTaskExecution:
    """Trusted terminal retaining the service-authored trajectory and auxiliaries."""

    evaluation: TaskEvaluation
    trajectory: RetrievalTrajectory
    search_endpoint: str
    final_output_text: str
    terminal_reason: str
    trial_artifact_root: Path | None = None
    candidate_runtime_failure: Mapping[str, str] | None = None
    agent_usage: tuple[ModelUsage, ...] = ()


class _BrowseCompTaskExecutionFailure(RuntimeError):
    """Carry a safe partial trace from an inner-runner exception."""

    def __init__(
        self,
        trajectory: RetrievalTrajectory,
        *,
        terminal_reason: str = "runner_exception",
        agent_usage: Sequence[ModelUsage] = (),
    ) -> None:
        super().__init__("trusted BrowseComp inner-agent failed")
        self.trajectory = trajectory
        self.terminal_reason = terminal_reason
        self.agent_usage = tuple(agent_usage)


def _candidate_projection(raw: object) -> Mapping[str, object]:
    """Drop every candidate-authored field except final status/result bytes."""

    if not isinstance(raw, Mapping):
        return MappingProxyType({"status": "failed", "result": []})
    status = raw.get("status")
    result = raw.get("result")
    if not isinstance(status, str):
        status = "failed"
    if not isinstance(result, Sequence) or isinstance(result, (str, bytes)):
        result = []
    copied: list[dict[str, str]] = []
    for item in result:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        output = item.get("output")
        if isinstance(item_type, str) and isinstance(output, str):
            copied.append({"type": item_type, "output": output})
    terminal_reason = raw.get("terminal_reason")
    if not isinstance(terminal_reason, str) or not terminal_reason.strip():
        terminal_reason = None
    return MappingProxyType(
        {
            "status": status,
            "result": copied,
            "terminal_reason": terminal_reason,
        }
    )


def _final_output_text(candidate: Mapping[str, object]) -> str:
    result = candidate.get("result")
    if not isinstance(result, Sequence) or isinstance(result, (str, bytes)):
        return ""
    for item in reversed(result):
        if (
            isinstance(item, Mapping)
            and item.get("type") == "output_text"
            and isinstance(item.get("output"), str)
        ):
            return item["output"]
    return ""


def _terminal_reason(candidate: Mapping[str, object]) -> str:
    supplied = candidate.get("terminal_reason")
    if isinstance(supplied, str) and supplied.strip():
        return supplied.strip()
    status = candidate.get("status")
    if status == "completed":
        return "completed"
    if status in {"length", "max_tokens", "content_filter"}:
        return f"provider_finish_reason:{status}"
    if status == "failed":
        return "runner_failed"
    return "runner_incomplete"


def _resolve_awaitable(value: object) -> object:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)  # type: ignore[arg-type]

    result: list[object] = []
    error: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(value))  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001  # pragma: no cover
            error.append(exc)

    import threading

    thread = threading.Thread(target=run, name="browsecomp-inner-agent-await")
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


class BrowseCompPlusTaskExecutionBackend:
    """One backend shared by development, attempt, and held-out evaluation."""

    def __init__(
        self,
        *,
        retrieval_service: BrowseCompRetrievalService,
        evaluator: BrowseCompPlusEvaluator,
        inner_agent_factory: InnerAgentCallableFactory,
        model_runtime: Mapping[str, object] | None = None,
        agent_config: Mapping[str, object] | None = None,
        development_agent_artifact_root: Path | None = None,
        task_wall_timeout_seconds: float | None = None,
    ) -> None:
        if not callable(inner_agent_factory):
            raise TypeError("inner_agent_factory must be callable")
        self._retrieval_service = retrieval_service
        self._evaluator = evaluator
        self._inner_agent_factory = inner_agent_factory
        self._model_runtime = _json_mapping(model_runtime or {}, "model runtime")
        self._agent_config = _json_mapping(agent_config or {}, "agent config")
        self._development_agent_artifact_root = (
            Path(development_agent_artifact_root)
            if development_agent_artifact_root is not None
            else None
        )
        self._task_wall_timeout_seconds = _positive_timeout(task_wall_timeout_seconds)

    @property
    def retrieval_service(self) -> BrowseCompRetrievalService:
        """Trusted owner access; this object is never placed in task context."""

        return self._retrieval_service

    def execute(
        self,
        candidate_source: object,
        task: TrustedTask,
    ) -> BrowseCompTaskExecution:
        if not isinstance(task, TrustedTask):
            raise TypeError("BrowseComp task execution requires a TrustedTask")
        session = self._retrieval_service.open_task_session()
        endpoint = str(session.endpoint)
        client = BrowseCompSearchClient(session.endpoint)
        environment = BrowseCompTaskEnvironment(client)
        context = AgentExecutionContext(
            benchmark_id=_BENCHMARK_ID,
            instruction=task.query,
            environment=environment,
        )
        failure: Exception | None = None
        raw_candidate: object = None
        try:
            factory_enforces_timeout = getattr(
                self._inner_agent_factory,
                "enforces_task_wall_timeout",
                False,
            )
            if self._task_wall_timeout_seconds is None or factory_enforces_timeout:
                entrypoint = self._inner_agent_factory(candidate_source)
                if not callable(entrypoint):
                    raise TypeError("inner-agent factory returned no callable")
                try:
                    inspect.signature(entrypoint).bind(
                        context=object(), model_runtime=object(), config=object()
                    )
                except (TypeError, ValueError) as exc:
                    raise TypeError(
                        "inner-agent callable must accept context, model_runtime, and config"
                    ) from exc
                raw_candidate = _resolve_awaitable(
                    entrypoint(
                        context=context,
                        model_runtime=self._model_runtime,
                        config=self._agent_config,
                    )
                )
            else:
                run_with_wall_timeout = getattr(
                    self._inner_agent_factory, "run_with_wall_timeout", None
                )
                if not callable(run_with_wall_timeout):
                    raise RuntimeError(
                        "BrowseComp inner-agent factory has no killable timeout boundary"
                    )
                try:
                    raw_candidate = run_with_wall_timeout(
                        candidate_source,
                        context=context,
                        model_runtime=self._model_runtime,
                        config=self._agent_config,
                        timeout_seconds=self._task_wall_timeout_seconds,
                    )
                except PartialModelUsageError:
                    raise
                except TimeoutError as exc:
                    raise _InnerAgentTaskTimeout(
                        "BrowseComp inner-agent exceeded its task wall timeout"
                    ) from exc
        except Exception as exc:  # noqa: BLE001 - runner failures are task outcomes
            failure = exc
        finally:
            try:
                trajectory = self._retrieval_service.freeze_task_session(session)
            except SearchTransportError as exc:
                if exc.trajectory is None:
                    raise
                raise _BrowseCompTaskExecutionFailure(
                    exc.trajectory,
                    terminal_reason="retrieval_failure",
                ) from exc
        if failure is not None:
            raise _BrowseCompTaskExecutionFailure(
                trajectory,
                terminal_reason=(
                    "task_wall_timeout"
                    if isinstance(
                        failure, (_InnerAgentTaskTimeout, PartialModelUsageError)
                    )
                    and "timeout" in str(failure).lower()
                    else "runner_exception"
                ),
                agent_usage=getattr(failure, "usage_calls", ()),
            ) from failure
        trial_artifact_root: Path | None = None
        candidate_runtime_failure: Mapping[str, str] | None = None
        model_usage: tuple[ModelUsage, ...] = ()
        if isinstance(raw_candidate, _TrustedInnerAgentTerminal):
            trial_artifact_root = raw_candidate.trial_artifact_root
            candidate_runtime_failure = raw_candidate.candidate_runtime_failure
            model_usage = (
                (raw_candidate.model_usage,)
                if raw_candidate.model_usage is not None
                else ()
            )
            raw_candidate = raw_candidate.candidate
        elif getattr(
            self._inner_agent_factory, "publishes_trusted_usage", False
        ) and isinstance(raw_candidate, Mapping):
            model_usage = model_usage_calls_from_mapping(raw_candidate.get("usage"))
        candidate = _candidate_projection(raw_candidate)
        terminal_reason = _terminal_reason(candidate)
        evaluation = self._evaluator.evaluate(
            task=task,
            candidate=candidate,
            trajectory=trajectory,
        )
        return BrowseCompTaskExecution(
            evaluation=evaluation,
            trajectory=trajectory,
            search_endpoint=endpoint,
            final_output_text=_final_output_text(candidate),
            terminal_reason=terminal_reason,
            trial_artifact_root=trial_artifact_root,
            candidate_runtime_failure=candidate_runtime_failure,
            agent_usage=model_usage,
        )

    def harvest_development_agent_trajectory(
        self,
        *,
        development_evaluation_id: str,
        task_id: str,
        execution: BrowseCompTaskExecution,
    ):
        source = execution.trial_artifact_root
        destination = self._development_agent_artifact_root
        if source is None or destination is None:
            return None
        try:
            return harvest_development_agent_trajectory(
                task_id=task_id,
                trial_artifact_root=source,
                durable_artifact_root=(destination / development_evaluation_id),
            )
        except Exception:  # noqa: BLE001 - trajectory publication is best effort
            _LOG.warning(
                "BrowseComp development agent trajectory harvest failed: task=%s",
                task_id,
            )
            return None

    def close(self) -> None:
        try:
            self._retrieval_service.close()
        finally:
            close = getattr(self._inner_agent_factory, "close", None)
            if callable(close):
                close()


def _trusted_runtime_summary(exc: BaseException) -> str:
    current: BaseException | None = exc
    while current is not None:
        reason = getattr(current, "reason", None)
        if isinstance(reason, str) and reason:
            return f"trusted BrowseComp setup failed ({reason})"
        current = current.__cause__
    return "trusted BrowseComp task runtime failed"


class BrowseCompPlusCampaignAdapter(CampaignBenchmarkAdapter):
    """Validate Arena-owned slots and delegate one task to the shared backend."""

    benchmark_id = _BENCHMARK_ID

    def __init__(
        self,
        *,
        plan: CampaignCellPlan,
        tasks: Mapping[str, TrustedTask],
        execution_backend: BrowseCompPlusTaskExecutionBackend,
        development_slots: bool = True,
        publish_retrieval_trajectory: bool = True,
        evaluator_digest: str | None = None,
    ) -> None:
        copied = dict(tasks)
        if (
            not copied
            or any(not isinstance(key, str) or not key for key in copied)
            or any(not isinstance(task, TrustedTask) for task in copied.values())
            or (
                development_slots
                and any(key != task.query_id for key, task in copied.items())
            )
            or len(copied) != len(set(copied))
        ):
            raise ValueError("BrowseComp campaign task authority is invalid")
        if development_slots and tuple(copied) != plan.development_task_ids:
            raise RuntimeCampaignError(
                "BrowseComp campaign tasks changed development membership"
            )
        self._plan = plan
        self._tasks = MappingProxyType(copied)
        self._backend = execution_backend
        self._development_slots = development_slots
        self._publish_retrieval_trajectory = publish_retrieval_trajectory
        self._evaluator_digest = evaluator_digest or _EVALUATOR_ID

    @property
    def evaluator_digest(self) -> str:
        return self._evaluator_digest

    @property
    def execution_backend(self) -> BrowseCompPlusTaskExecutionBackend:
        return self._backend

    def resolve_tasks(
        self,
        plan: CampaignCellPlan,
        sample_slots: Sequence[SampleSlot],
    ) -> tuple[TaskRef, ...]:
        if not self._development_slots or plan != self._plan:
            raise RuntimeCampaignError(
                "BrowseComp adapter requires its frozen development plan"
            )
        slots = assert_development_slots(plan, sample_slots)
        resolved: list[TaskRef] = []
        for slot in slots:
            if slot.task_id not in self._tasks:
                raise RunValidationError(
                    "BrowseComp attempt selected an unavailable development task"
                )
            resolved.append(
                TaskRef(
                    task_id=slot.task_id,
                    cluster=plan.dataset_boundary.development_set.task_clusters[
                        slot.task_id
                    ],
                    benchmark_id=self.benchmark_id,
                )
            )
        return tuple(resolved)

    def evaluate_task(
        self,
        candidate: object,
        task: TaskRef,
    ) -> TaskEvaluationOutcome:
        if task.canonical_identity != (self.benchmark_id, task.task_id):
            raise RunValidationError("BrowseComp task lost benchmark scope")
        trusted = self._tasks.get(task.task_id)
        if trusted is None:
            raise RunValidationError("BrowseComp task escaped trusted authority")
        started = time.monotonic()
        try:
            execution = self._backend.execute(candidate, trusted)
        except _BrowseCompTaskExecutionFailure as exc:
            provenance: dict[str, object] = {
                "browsecomp_plus_usage": {
                    "tool_call_count": exc.trajectory.tool_call_count,
                }
            }
            if self._publish_retrieval_trajectory:
                provenance["browsecomp_plus_search"] = exc.trajectory.as_record()
            outcome_usage: dict[str, object] = {}
            model_usage = aggregate_model_usage(exc.agent_usage)
            if model_usage is not None:
                calls = [call.as_dict() for call in exc.agent_usage]
                provenance["browsecomp_plus_usage"] = {
                    "tool_call_count": exc.trajectory.tool_call_count,
                    "model_calls": calls,
                }
                provenance["usage"] = {
                    "calls": calls,
                    **{
                        name: model_usage[name]
                        for name in (
                            "role",
                            "provider",
                            "model",
                            "token_source",
                        )
                    },
                }
                outcome_usage = {
                    "input_tokens": model_usage["input_tokens"],
                    "output_tokens": model_usage["output_tokens"],
                    "cache_tokens": model_usage["cache_read_tokens"],
                    "token_status": (
                        "reported"
                        if model_usage["input_tokens"] is not None
                        and model_usage["output_tokens"] is not None
                        else "unverified"
                        if model_usage["input_tokens"] is not None
                        or model_usage["output_tokens"] is not None
                        else "missing"
                    ),
                    "input_token_status": model_usage["input_token_status"],
                    "output_token_status": model_usage["output_token_status"],
                    "cache_token_status": model_usage["cache_token_status"],
                    "cost_usd": model_usage["cost_usd"],
                    "cost_status": model_usage["cost_status"],
                }
            return TaskEvaluationOutcome(
                outcome="inconclusive",
                wall_seconds=time.monotonic() - started,
                summary=_trusted_runtime_summary(exc),
                provenance=MappingProxyType(provenance),
                **outcome_usage,
            )
        except Exception as exc:  # noqa: BLE001 - trusted setup failures are inconclusive
            return TaskEvaluationOutcome(
                outcome="inconclusive",
                wall_seconds=time.monotonic() - started,
                summary=_trusted_runtime_summary(exc),
            )
        evaluation = execution.evaluation
        usage_calls = (*execution.agent_usage, evaluation.judge.usage)
        model_usage = aggregate_model_usage(usage_calls)
        provenance: dict[str, object] = {
            "browsecomp_plus_usage": {
                "tool_call_count": execution.trajectory.tool_call_count,
            }
        }
        outcome_usage: dict[str, object] = {}
        if model_usage is not None:
            provenance["browsecomp_plus_usage"] = {
                "tool_call_count": execution.trajectory.tool_call_count,
                "model_calls": model_usage["calls"],
            }
            provenance["usage"] = {
                name: model_usage[name]
                for name in (
                    "role",
                    "provider",
                    "model",
                    "token_source",
                    "input_token_status",
                    "output_token_status",
                    "cache_token_status",
                    "cache_status",
                    "cache_read_tokens",
                )
            }
            provenance["usage"]["calls"] = [
                call.as_dict() for call in usage_calls if call is not None
            ]
            input_tokens = model_usage["input_tokens"]
            output_tokens = model_usage["output_tokens"]
            cache_tokens = model_usage["cache_read_tokens"]
            cost_usd = model_usage["cost_usd"]
            token_status = (
                "reported"
                if input_tokens is not None and output_tokens is not None
                else "unverified"
                if input_tokens is not None or output_tokens is not None
                else "missing"
            )
            outcome_usage = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_tokens": cache_tokens,
                "token_status": token_status,
                "input_token_status": model_usage["input_token_status"],
                "output_token_status": model_usage["output_token_status"],
                "cache_token_status": model_usage["cache_token_status"],
                "cost_usd": cost_usd,
                "cost_status": model_usage["cost_status"],
            }
        if self._publish_retrieval_trajectory:
            provenance["browsecomp_plus_search"] = execution.trajectory.as_record()
        if execution.candidate_runtime_failure is not None:
            provenance[CANDIDATE_RUNTIME_FAILURE_KEY] = dict(
                execution.candidate_runtime_failure
            )
        if self._development_slots and evaluation.evaluation_signal is not None:
            provenance["browsecomp_plus_feedback"] = {
                "evaluation_signal": evaluation.evaluation_signal,
            }
        return TaskEvaluationOutcome(
            outcome=evaluation.outcome,
            score=(
                float(evaluation.score) if evaluation.outcome == "measured" else None
            ),
            wall_seconds=time.monotonic() - started,
            summary=(
                "BrowseComp answer measured"
                if evaluation.outcome == "measured"
                else "trusted BrowseComp judge was inconclusive"
            ),
            provenance=MappingProxyType(provenance),
            **outcome_usage,
        )


class BrowseCompPlusDevelopmentAdapter:
    """Public development material over the same task execution backend."""

    benchmark_id = _BENCHMARK_ID

    def __init__(
        self,
        *,
        plan: CampaignCellPlan,
        public_tasks: Sequence[PublicTask],
        trusted_tasks: Sequence[TrustedTask],
        execution_backend: BrowseCompPlusTaskExecutionBackend,
        gateway: TrustedEvaluationGateway,
        task_concurrency: int = 1,
        publish_retrieved_snippets: bool = False,
        publish_judge_text: bool = False,
    ) -> None:
        public = tuple(public_tasks)
        trusted = tuple(trusted_tasks)
        task_ids = tuple(task.query_id for task in public)
        if (
            plan.benchmark != self.benchmark_id
            or task_ids != plan.development_task_ids
            or tuple(task.public for task in trusted) != public
        ):
            raise RuntimeCampaignError(
                "BrowseComp development adapter authority is invalid"
            )
        if (
            isinstance(task_concurrency, bool)
            or not isinstance(task_concurrency, int)
            or task_concurrency < 1
        ):
            raise ValueError("BrowseComp development concurrency must be positive")
        if not isinstance(publish_retrieved_snippets, bool):
            raise TypeError("BrowseComp snippet publication flag must be a boolean")
        if not isinstance(publish_judge_text, bool):
            raise TypeError("BrowseComp judge-text publication flag must be a boolean")
        self._plan = plan
        self._public = MappingProxyType({task.query_id: task for task in public})
        self._trusted = MappingProxyType({task.query_id: task for task in trusted})
        self._backend = execution_backend
        self._gateway = gateway
        self._task_concurrency = task_concurrency
        self._publish_retrieved_snippets = publish_retrieved_snippets
        self._publish_judge_text = publish_judge_text

    @property
    def frozen_task_ids(self) -> tuple[str, ...]:
        return self._plan.development_task_ids

    @property
    def execution_backend(self) -> BrowseCompPlusTaskExecutionBackend:
        return self._backend

    def _authorize(self, task_id: str) -> TrustedTask:
        try:
            return self._trusted[task_id]
        except KeyError as exc:
            raise RuntimeCampaignError(
                "BrowseComp development request escaped frozen membership"
            ) from exc

    def public_material(self, task_id: str) -> DevelopmentMaterial:
        self._authorize(task_id)
        return DevelopmentMaterial(
            task_id=task_id,
            task=self._public[task_id].as_dict(),
        )

    def format_failed_task_brief(
        self, material: DevelopmentMaterial
    ) -> Mapping[str, object]:
        return MappingProxyType({"query": material.task["query"]})

    def execute(
        self,
        stable_code_view: Path,
        task_ids: tuple[str, ...],
        *,
        development_evaluation_id: str,
        stable_code_view_id: str,
    ) -> DevelopmentExecution:
        if (
            not task_ids
            or len(set(task_ids)) != len(task_ids)
            or not set(task_ids).issubset(self._trusted)
        ):
            raise RuntimeCampaignError(
                "BrowseComp development execution escaped frozen membership"
            )
        trajectory: list[dict[str, object]] = [
            {
                "event": "development_evaluation_started",
                "stable_code_view_id": stable_code_view_id,
                "task_ids": list(task_ids),
            }
        ]

        def terminal_inconclusive() -> DevelopmentExecution:
            task_results = [
                {
                    "task_id": task_id,
                    "outcome": "inconclusive",
                    "score": None,
                    "summary": "trusted BrowseComp development evaluation failed",
                }
                for task_id in task_ids
            ]
            trajectory.append(
                {
                    "event": "development_evaluation_terminal",
                    "development_evaluation_id": development_evaluation_id,
                    "outcome": "inconclusive",
                }
            )
            return DevelopmentExecution(
                outcome="inconclusive",
                result={
                    "schema": "browsecomp-plus-development-result",
                    "outcome": "inconclusive",
                    "task_results": task_results,
                    "summary": (
                        "trusted BrowseComp development evaluation was inconclusive"
                    ),
                },
                usage={
                    "schema": "browsecomp-plus-development-usage",
                    "tasks": [],
                },
                trajectory=tuple(trajectory),
            )

        try:
            candidate = self._gateway.materialize(Path(stable_code_view))
        except Exception:  # noqa: BLE001 - gateway owns its concrete error taxonomy
            return terminal_inconclusive()

        def evaluate(
            task_id: str,
        ) -> tuple[
            str,
            BrowseCompTaskExecution | None,
            RetrievalTrajectory,
            str,
            tuple[ModelUsage, ...],
        ]:
            try:
                execution = self._backend.execute(candidate, self._authorize(task_id))
                return (
                    task_id,
                    execution,
                    execution.trajectory,
                    execution.terminal_reason,
                    (),
                )
            except _BrowseCompTaskExecutionFailure as exc:
                return (
                    task_id,
                    None,
                    exc.trajectory,
                    exc.terminal_reason,
                    exc.agent_usage,
                )
            except Exception:  # noqa: BLE001 - trusted runtime failures are sanitized
                return (
                    task_id,
                    None,
                    RetrievalTrajectory((), 0),
                    "trusted_runtime_failure",
                    (),
                )

        try:
            with ThreadPoolExecutor(
                max_workers=min(self._task_concurrency, len(task_ids)),
                thread_name_prefix="browsecomp-development-task",
            ) as pool:
                executions = tuple(pool.map(evaluate, task_ids))
        except Exception:  # noqa: BLE001 - executor failures become one terminal
            return terminal_inconclusive()

        task_results: list[dict[str, object]] = []
        usage_tasks: list[dict[str, object]] = []
        artifacts: list[DevelopmentArtifact] = []
        for (
            task_id,
            execution,
            search_trajectory,
            terminal_reason,
            partial_usage,
        ) in executions:
            if execution is None:
                task_results.append(
                    {
                        "task_id": task_id,
                        "outcome": "inconclusive",
                        "score": None,
                        "metrics": {
                            "tool_call_count": search_trajectory.tool_call_count
                        },
                        "judge": {
                            "terminal_reason": "not_started",
                            "http_status": None,
                            "retry_count": 0,
                        },
                    }
                )
                final_output_text = ""
            else:
                public = execution.evaluation.as_public_dict()
                result = {
                    "task_id": task_id,
                    "outcome": public["outcome"],
                    "score": public["score"],
                    "metrics": public["metrics"],
                    "judge": execution.evaluation.development_judge_diagnostics(),
                }
                if "evaluation_signal" in public:
                    result["evaluation_signal"] = public["evaluation_signal"]
                if execution.candidate_runtime_failure is not None:
                    result["diagnostic"] = dict(execution.candidate_runtime_failure)
                task_results.append(result)
                final_output_text = execution.final_output_text
                harvested = self._backend.harvest_development_agent_trajectory(
                    development_evaluation_id=development_evaluation_id,
                    task_id=task_id,
                    execution=execution,
                )
                if harvested is not None:
                    artifacts.extend(harvested.artifacts)
                    event = harvested.public_event(
                        "timeout"
                        if "timeout" in execution.terminal_reason
                        else "completed"
                    )
                    if event is not None:
                        trajectory.append(dict(event))
                if self._publish_judge_text and execution.evaluation.judge.text:
                    artifacts.append(
                        DevelopmentArtifact(
                            path=f"{task_id}/judge-response.txt",
                            content=execution.evaluation.judge.text.encode("utf-8"),
                        )
                    )
            usage_task = {
                "task_id": task_id,
                "tool_call_count": search_trajectory.tool_call_count,
            }
            usage_tasks.append(usage_task)
            calls = (
                partial_usage
                if execution is None
                else (*execution.agent_usage, execution.evaluation.judge.usage)
            )
            model_usage = aggregate_model_usage(calls)
            if model_usage is not None:
                usage_task.update(dict(model_usage))
            trajectory.append(
                {
                    "event": "browsecomp_plus_search_trajectory",
                    "task_id": task_id,
                    "final_output_text": final_output_text,
                    "terminal_reason": terminal_reason,
                    **search_trajectory.as_development_record(),
                }
            )
            if self._publish_retrieved_snippets:
                artifact_record = {
                    **search_trajectory.snippets_artifact_record(),
                    "task_id": task_id,
                }
                artifacts.append(
                    DevelopmentArtifact(
                        path=f"{task_id}/retrieved-snippets.json",
                        content=(
                            json.dumps(
                                artifact_record,
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=False,
                                allow_nan=False,
                            ).encode("utf-8")
                            + b"\n"
                        ),
                    )
                )
        outcome = (
            "measured"
            if any(result["outcome"] == "measured" for result in task_results)
            else "inconclusive"
        )
        trajectory.append(
            {
                "event": "development_evaluation_terminal",
                "development_evaluation_id": development_evaluation_id,
                "outcome": outcome,
            }
        )
        return DevelopmentExecution(
            outcome=outcome,
            result={
                "schema": "browsecomp-plus-development-result",
                "outcome": outcome,
                "task_results": task_results,
            },
            usage={
                "schema": "browsecomp-plus-development-usage",
                "tasks": usage_tasks,
            },
            trajectory=tuple(trajectory),
            artifacts=tuple(artifacts),
        )


__all__ = [
    "BrowseCompPlusCampaignAdapter",
    "BrowseCompPlusDevelopmentAdapter",
    "BrowseCompPlusTaskExecutionBackend",
    "BrowseCompTaskEnvironment",
    "BrowseCompTaskExecution",
    "InnerAgentCallable",
    "InnerAgentCallableFactory",
]
