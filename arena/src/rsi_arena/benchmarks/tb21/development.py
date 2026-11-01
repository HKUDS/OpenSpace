"""Benchmark-owned Terminal-Bench 2 development adapter.

The shared Arena owner supplies one operation-local stable code view.  This
adapter resolves frozen TB2 authority, runs the existing TB2 evaluator lower
half, owns the complete trusted development evidence ledgers, and returns only the
benchmark-owned neutral projection for the method-visible mirror.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from rsi_arena.benchmarks.harbor_runtime.trajectory import (
    HarvestedDevelopmentAgentTrajectory,
    harvest_development_agent_trajectory,
)
from rsi_arena.benchmarks.harbor_runtime.verifier_settings import VerifierSettings
from rsi_arena.contracts import CellBudget, TaskRef
from rsi_arena.development import (
    DevelopmentArtifact,
    DevelopmentExecution,
    DevelopmentMaterial,
)
from rsi_arena.evaluation import CandidateFailure, CellResult, UnmeasuredCell
from rsi_arena.evaluation_gateway import TrustedEvaluationGateway
from rsi_arena.run_layout import AppendOnlyJsonl, RunLayout
from rsi_arena.runtime_campaign import CampaignCellPlan, RuntimeCampaignError

from .adapter import TB2Bench
from .evidence import RSIEvidenceRequest
from .tasks import TB2TaskTimeoutAuthority, resolve_tb2_task_timeout_authority

_LOG = logging.getLogger(__name__)

_PUBLIC_METRICS = frozenset(
    {
        "reward",
        "harness_boot",
        "duration_s",
        "n_input_tokens",
        "n_cache_tokens",
        "n_output_tokens",
        "agent_timeout_observed",
    }
)


def _public_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


class TB2DevelopmentAdapter:
    """One method-independent TB2 implementation of ``DevelopmentAdapter``."""

    benchmark_id = "tb2"

    def __init__(
        self,
        *,
        bench: TB2Bench,
        plan: CampaignCellPlan,
        development_task_ids: tuple[str, ...],
        layout: RunLayout,
        gateway: TrustedEvaluationGateway,
        cell_budget: CellBudget,
        usage_identity: Mapping[str, object],
        worker_timeout_seconds: float,
        cleanup_grace_seconds: float,
        task_concurrency: int = 1,
        verifier_settings: VerifierSettings,
    ) -> None:
        if plan.benchmark != self.benchmark_id:
            raise RuntimeCampaignError("TB2 development adapter requires a TB2 plan")
        delivery = json.loads(
            (layout.root / "development-delivery-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        benchmark_delivery = delivery.get("benchmarks", {}).get(self.benchmark_id)
        if (
            layout.run_id == ""
            or delivery.get("schema") != "campaign-development-delivery"
            or delivery.get("campaign_freeze_id") != plan.freeze_id
            or not isinstance(benchmark_delivery, Mapping)
            or benchmark_delivery.get("benchmark_id") != self.benchmark_id
        ):
            raise RuntimeCampaignError(
                "TB2 development adapter run authority disagrees"
            )
        tasks = {task.task_id: task for task in bench.list_tasks()}
        if (
            not development_task_ids
            or not set(development_task_ids).issubset(plan.development_task_ids)
            or not set(development_task_ids).issubset(tasks)
        ):
            raise RuntimeCampaignError("TB2 development membership is unavailable")
        provider = str(usage_identity.get("provider", "")).strip()
        model = str(usage_identity.get("model", "")).strip()
        authority = str(usage_identity.get("authority", "")).strip()
        if not provider or not model or not authority:
            raise ValueError("TB2 development usage authority is incomplete")
        if (
            isinstance(task_concurrency, bool)
            or not isinstance(task_concurrency, int)
            or task_concurrency < 1
        ):
            raise ValueError("TB2 Arena development task concurrency must be positive")
        self._bench = bench
        self._plan = plan
        self._layout = layout
        self._gateway = gateway
        self._cell_budget = cell_budget
        self._usage_identity = {
            "provider": provider,
            "model": model,
            "authority": authority,
        }
        self._task_concurrency = task_concurrency
        self._trajectory_harvest_supported = isinstance(bench, TB2Bench)
        if not isinstance(verifier_settings, VerifierSettings):
            raise TypeError("verifier_settings must be VerifierSettings")
        self._verifier_settings = verifier_settings
        self._tasks = tasks
        self._development_task_ids = development_task_ids
        self._task_id_set = frozenset(development_task_ids)
        self._task_timeouts: Mapping[str, TB2TaskTimeoutAuthority] = MappingProxyType(
            {
                task_id: resolve_tb2_task_timeout_authority(
                    {**tasks[task_id].meta, "task_id": task_id},
                    worker_timeout_seconds=worker_timeout_seconds,
                    outer_timeout_seconds=cell_budget.wall_seconds,
                    cleanup_grace_seconds=cleanup_grace_seconds,
                    verifier_timeout_multiplier=(
                        self._verifier_settings.timeout_multiplier
                    ),
                )
                for task_id in development_task_ids
            }
        )
        self._evidence_root = layout.root / "development"
        self._evidence_root.mkdir(parents=True, mode=0o700, exist_ok=True)

    @property
    def frozen_task_ids(self) -> tuple[str, ...]:
        return self._development_task_ids

    @property
    def task_concurrency(self) -> int:
        """Frozen development-task worker ceiling consumed by this bench-owned adapter."""

        return self._task_concurrency

    def public_material(self, task_id: str) -> DevelopmentMaterial:
        task = self._authorize(task_id)
        request = RSIEvidenceRequest(
            experiment_id=self._layout.run_id,
            attempt_id=(
                "development-material-"
                + hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
            ),
            selection_decision_id="tb2-frozen-development-membership",
            parent_commit_sha="operation-local-code-view",
            wave_id="development",
        )
        pack = self._bench.build_development_pack((task,), request)
        if len(pack.episodes) != 1 or pack.episodes[0].task_id != task_id:
            raise RuntimeError("TB2 development material changed frozen task identity")
        episode = pack.episodes[0]
        return DevelopmentMaterial(
            task_id=task_id,
            task={
                "instruction": episode.task_statement,
                "task_local_tests": episode.public_checks,
                "expected_behavior": episode.expected_behavior,
                "solution_or_gold": episode.reference_solution,
            },
        )

    def format_failed_task_brief(
        self, material: DevelopmentMaterial
    ) -> Mapping[str, object]:
        """Render only fields already approved as public TB2 material."""

        if material.task_id not in self._task_id_set:
            raise RuntimeCampaignError("TB2 failed-task material escaped membership")
        task = material.task
        return {
            "instruction": _public_text(task.get("instruction")),
            "public_checks": _public_text(task.get("task_local_tests")),
            "expected_behavior": _public_text(task.get("expected_behavior")),
        }

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
            or not set(task_ids).issubset(self._task_id_set)
        ):
            raise RuntimeCampaignError(
                "TB2 development execution escaped frozen membership"
            )
        tasks = tuple(self._authorize(task_id) for task_id in task_ids)
        started_at = time.time()
        monotonic_started = time.monotonic()
        started = {
            "schema": "tb21-development-trajectory",
            "event": "development_evaluation_started",
            "development_evaluation_id": development_evaluation_id,
            "stable_code_view_id": stable_code_view_id,
            "task_ids": list(task_ids),
            "timestamp": started_at,
        }
        self._append_trajectory(development_evaluation_id, (started,))
        results: list[dict[str, object]] = []
        public_events: list[dict[str, object]] = [
            {
                "event": "development_evaluation_started",
                "stable_code_view_id": stable_code_view_id,
                "task_ids": list(task_ids),
            }
        ]
        public_artifacts: list[DevelopmentArtifact] = []
        try:
            candidate = self._gateway.materialize(Path(stable_code_view))

            def evaluate(
                indexed_task: tuple[int, TaskRef],
            ) -> tuple[dict[str, object], HarvestedDevelopmentAgentTrajectory | None]:
                index, task = indexed_task
                task_started = time.monotonic()
                timeout_authority = self._task_timeouts[task.task_id]
                harvested: HarvestedDevelopmentAgentTrajectory | None = None

                def preserve(trial_artifact_root: Path) -> None:
                    nonlocal harvested
                    harvested = harvest_development_agent_trajectory(
                        task_id=task.task_id,
                        trial_artifact_root=trial_artifact_root,
                        durable_artifact_root=(
                            self._evidence_root
                            / "agent-trajectory-artifacts"
                            / development_evaluation_id
                        ),
                    )

                run_kwargs = (
                    {"pre_cleanup_trial_hook": preserve}
                    if self._trajectory_harvest_supported
                    else {}
                )
                try:
                    observation = self._bench.run_cell(
                        candidate.checkout_path,
                        task,
                        seed=index,
                        budget=self._cell_budget,
                        timeout_authority=timeout_authority,
                        **run_kwargs,
                    )
                except Exception as exc:  # noqa: BLE001 - one task is inconclusive
                    _LOG.warning(
                        "TB2 development task was inconclusive: task=%s type=%s",
                        task.task_id,
                        type(exc).__name__,
                    )
                    observation = UnmeasuredCell(
                        reason="trusted TB2 development task evaluation failed",
                        diagnostics={"exception_type": type(exc).__name__},
                        duration_s=time.monotonic() - task_started,
                        cost_usd=None,
                        cost_status="missing",
                    )
                return (
                    self._observation(
                        task.task_id,
                        observation,
                        timeout_authority=timeout_authority,
                        resolved_verifier_settings=(
                            self._verifier_settings.resolved_task_settings(task.task_id)
                        ),
                    ),
                    harvested,
                )

            with ThreadPoolExecutor(
                max_workers=min(self._task_concurrency, len(tasks)),
                thread_name_prefix="tb2-development-task",
            ) as pool:
                evaluated = tuple(pool.map(evaluate, enumerate(tasks)))
            for task, (result, harvested) in zip(tasks, evaluated):
                results.append(result)
                public_events.append(
                    {
                        "event": "development_task_terminal",
                        "task_id": task.task_id,
                        "outcome": result["outcome"],
                        "score": result["score"],
                        "metric": result["metric"],
                    }
                )
                agent_status = _trusted_agent_status(result)
                if harvested is not None and agent_status is not None:
                    public_event = harvested.public_event(agent_status)
                    if public_event is not None:
                        public_events.append(dict(public_event))
                    public_artifacts.extend(harvested.artifacts)
        except Exception as exc:
            ended_at = time.time()
            wall_seconds = time.monotonic() - monotonic_started
            task_results = [
                {
                    "task_id": task_id,
                    "outcome": "inconclusive",
                    "score": None,
                    "summary": "trusted TB2 development evaluation failed",
                }
                for task_id in task_ids
            ]
            terminal = {
                "schema": "tb21-development-evaluation",
                "development_evaluation_id": development_evaluation_id,
                "stable_code_view_id": stable_code_view_id,
                "task_ids": list(task_ids),
                "outcome": "inconclusive",
                "n_expected": len(task_ids),
                "n_conclusive": 0,
                "n_inconclusive": len(task_ids),
                "score": None,
                "coverage": 0.0,
                "task_results": task_results,
                "failure_kind": "trusted_tb2_development_callback_exception",
                "exception_type": type(exc).__name__,
                "started_at": started_at,
                "ended_at": ended_at,
                "wall_seconds": wall_seconds,
            }
            trusted_terminal = {
                "schema": "tb21-development-trajectory",
                "event": "development_evaluation_terminal",
                "development_evaluation_id": development_evaluation_id,
                "outcome": "inconclusive",
                "timestamp": ended_at,
            }
            self._append("evaluations.jsonl", (terminal,))
            self._append_trajectory(development_evaluation_id, (trusted_terminal,))
            public_events.append(
                {"event": "development_evaluation_terminal", "outcome": "inconclusive"}
            )
            return DevelopmentExecution(
                outcome="inconclusive",
                result={
                    "benchmark_id": self.benchmark_id,
                    "task_ids": list(task_ids),
                    "outcome": "inconclusive",
                    "n_expected": len(task_ids),
                    "n_conclusive": 0,
                    "n_inconclusive": len(task_ids),
                    "score": None,
                    "coverage": 0.0,
                    "task_results": task_results,
                    "failure_summary": {
                        "kind": "infrastructure",
                        "summary": "trusted TB2 development evaluation was inconclusive",
                    },
                },
                usage={"tasks": [], "cost_status": "missing"},
                trajectory=tuple(public_events),
            )

        ended_at = time.time()
        wall_seconds = time.monotonic() - monotonic_started
        n_passed = sum(
            item["outcome"] == "measured" and item["score"] == 1.0 for item in results
        )
        n_inconclusive = sum(item["outcome"] == "inconclusive" for item in results)
        n_conclusive = sum(item["outcome"] == "measured" for item in results)
        outcome = "measured" if n_conclusive else "inconclusive"
        pass_rate = n_passed / n_conclusive if n_conclusive else None
        coverage = n_conclusive / len(results)
        terminal = {
            "schema": "tb21-development-evaluation",
            "development_evaluation_id": development_evaluation_id,
            "stable_code_view_id": stable_code_view_id,
            "task_ids": list(task_ids),
            "outcome": outcome,
            "n_expected": len(results),
            "n_conclusive": n_conclusive,
            "n_inconclusive": n_inconclusive,
            "score": pass_rate,
            "coverage": coverage,
            "task_results": results,
            "started_at": started_at,
            "ended_at": ended_at,
            "wall_seconds": wall_seconds,
        }
        trusted_terminal = {
            "schema": "tb21-development-trajectory",
            "event": "development_evaluation_terminal",
            "development_evaluation_id": development_evaluation_id,
            "outcome": outcome,
            "timestamp": ended_at,
        }
        usage = tuple(
            self._usage_row(development_evaluation_id, item) for item in results
        )
        public_results = [self._public_observation(item) for item in results]
        self._append("evaluations.jsonl", (terminal,))
        self._append_trajectory(development_evaluation_id, (trusted_terminal,))
        self._append("usage.jsonl", usage)
        public_events.append(
            {"event": "development_evaluation_terminal", "outcome": outcome}
        )
        return DevelopmentExecution(
            outcome=outcome,
            result={
                "benchmark_id": self.benchmark_id,
                "task_ids": list(task_ids),
                "outcome": outcome,
                "n_expected": len(results),
                "n_conclusive": n_conclusive,
                "n_inconclusive": n_inconclusive,
                "score": pass_rate,
                "coverage": coverage,
                "task_results": public_results,
            },
            usage={
                "authority": self._usage_identity["authority"],
                "cost_status": self._aggregate_status(results, "cost_status"),
                "tasks": [self._public_usage(item) for item in usage],
            },
            trajectory=tuple(public_events),
            artifacts=tuple(public_artifacts),
        )

    def _authorize(self, task_id: str) -> TaskRef:
        if not task_id or task_id not in self._task_id_set:
            raise RuntimeCampaignError(
                "task ID is not in frozen TB2 development membership"
            )
        return self._tasks[task_id]

    def _append(self, name: str, rows: tuple[Mapping[str, object], ...]) -> None:
        if not rows:
            return
        writer = AppendOnlyJsonl(self._evidence_root / name)
        try:
            writer.append_many(rows)
        finally:
            writer.close()

    def _append_trajectory(
        self,
        development_evaluation_id: str,
        rows: tuple[Mapping[str, object], ...],
    ) -> None:
        try:
            self._append("trajectory.jsonl", rows)
        except OSError:
            _LOG.warning(
                "development trajectory JSONL dropped: evaluation=%s stage=%s",
                development_evaluation_id,
                "trusted-append",
            )

    @staticmethod
    def _observation(
        task_id: str,
        observation: object,
        *,
        timeout_authority: TB2TaskTimeoutAuthority | None = None,
        resolved_verifier_settings: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        base: dict[str, object] = {
            "task_id": task_id,
            "wall_seconds": float(getattr(observation, "duration_s", 0.0)),
            "provider_reported_cost": getattr(observation, "cost_usd", None),
            "cost_status": str(getattr(observation, "cost_status", "missing")),
            "provider_calls": [
                dict(call) for call in getattr(observation, "provider_calls", ())
            ],
        }
        if timeout_authority is not None:
            resolved_timeouts = {
                "agent_deadline_seconds": (timeout_authority.agent_deadline_seconds),
                "outer_deadline_seconds": timeout_authority.outer_deadline_seconds,
            }
            resolved_timeouts["verifier_deadline_seconds"] = (
                timeout_authority.verifier_deadline_seconds
            )
            base["resolved_timeouts"] = resolved_timeouts
        if resolved_verifier_settings is not None:
            base["resolved_verifier_settings"] = dict(resolved_verifier_settings)
        if isinstance(observation, CellResult):
            base.update(
                {
                    "outcome": "measured",
                    "score": 1.0 if observation.outcome == "pass" else 0.0,
                    "metric": {
                        key: value
                        for key, value in observation.raw_metrics.items()
                        if key in _PUBLIC_METRICS
                    },
                }
            )
        elif isinstance(observation, CandidateFailure):
            base.update(
                {
                    "outcome": "measured",
                    "score": 0.0,
                    "metric": {
                        key: value
                        for key, value in observation.raw_metrics.items()
                        if key in _PUBLIC_METRICS
                    },
                }
            )
        elif isinstance(observation, UnmeasuredCell):
            base.update(
                {
                    "outcome": "inconclusive",
                    "score": None,
                    "summary": observation.reason,
                    "metric": {},
                }
            )
        else:
            raise RuntimeError("TB2 development bench returned an invalid observation")
        return base

    def _usage_row(
        self,
        development_evaluation_id: str,
        result: Mapping[str, object],
    ) -> dict[str, object]:
        metrics = result.get("metric")
        metrics = metrics if isinstance(metrics, Mapping) else {}

        def count(name: str) -> int | None:
            value = metrics.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric = float(value)
                if numeric >= 0 and numeric.is_integer():
                    return int(numeric)
            return None

        tokens_in = count("n_input_tokens")
        tokens_out = count("n_output_tokens")
        cache_tokens = count("n_cache_tokens")
        calls = [
            {
                "role": "solver-agent",
                **self._usage_identity,
                **dict(call),
            }
            for call in result.get("provider_calls", ())
            if isinstance(call, Mapping)
        ]
        return {
            "schema": "tb21-development-usage",
            "development_evaluation_id": development_evaluation_id,
            "task_id": result["task_id"],
            "role": "development_task_worker",
            **self._usage_identity,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "cache_tokens": cache_tokens,
            "input_token_status": "reported" if tokens_in is not None else "missing",
            "output_token_status": (
                "reported" if tokens_out is not None else "missing"
            ),
            "cache_token_status": (
                "reported" if cache_tokens is not None else "missing"
            ),
            "provider_reported_cost": result.get("provider_reported_cost"),
            "cost_status": result.get("cost_status", "missing"),
            "wall_seconds": result.get("wall_seconds", 0.0),
            "timestamp": time.time(),
            "calls": calls,
        }

    @staticmethod
    def _public_observation(result: Mapping[str, object]) -> dict[str, object]:
        public = dict(result)
        public.pop("provider_calls", None)
        if public.get("outcome") == "inconclusive":
            public["summary"] = "trusted TB2 development evaluation was inconclusive"
        return public

    @staticmethod
    def _public_usage(row: Mapping[str, object]) -> dict[str, object]:
        return {
            key: row[key]
            for key in (
                "task_id",
                "role",
                "provider",
                "model",
                "authority",
                "input_tokens",
                "output_tokens",
                "cache_tokens",
                "input_token_status",
                "output_token_status",
                "cache_token_status",
                "provider_reported_cost",
                "cost_status",
                "wall_seconds",
                "calls",
            )
        }

    @staticmethod
    def _aggregate_status(results: list[dict[str, object]], field: str) -> str:
        values = {str(item.get(field, "missing")) for item in results}
        if "unverified" in values:
            return "unverified"
        if "missing" in values:
            return "missing"
        return "reported"


def _trusted_agent_status(
    result: Mapping[str, object],
) -> Literal["completed", "timeout"] | None:
    """Project only the typed timeout state from a conclusive observation."""

    if result.get("outcome") != "measured":
        return None
    metrics = result.get("metric")
    if not isinstance(metrics, Mapping):
        return None
    observed = metrics.get("agent_timeout_observed")
    if (
        not isinstance(observed, (int, float))
        or isinstance(observed, bool)
        or float(observed) not in {0.0, 1.0}
    ):
        return None
    return "timeout" if float(observed) == 1.0 else "completed"


__all__ = ["TB2DevelopmentAdapter"]
