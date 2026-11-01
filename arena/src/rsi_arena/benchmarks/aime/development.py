"""AIME-owned adapter for the run-scoped Arena development surface."""

from __future__ import annotations

import dataclasses
import logging
import re
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rsi_arena.benchmarks.harbor_runtime.agent_bridge import (
    CANDIDATE_RUNTIME_FAILURE_KEY,
    AgentContextValidationError,
    validated_candidate_runtime_failure_marker,
)
from rsi_arena.benchmarks.harbor_runtime.trajectory import (
    HarvestedDevelopmentAgentTrajectory,
    harvest_development_agent_trajectory,
)
from rsi_arena.contracts import CellBudget, TaskRef
from rsi_arena.development import (
    DevelopmentArtifact,
    DevelopmentExecution,
    DevelopmentMaterial,
)
from rsi_arena.evaluation import BenchAdapter
from rsi_arena.evaluation_gateway import TrustedEvaluationGateway
from rsi_arena.run_layout import AppendOnlyJsonl, RunLayout, TaskEvaluationOutcome
from rsi_arena.runtime_campaign import CampaignCellPlan, RuntimeCampaignError

from .adapter import AimeBench
from .campaign import AimeCampaignBenchmarkAdapter
from .development_material import (
    CanonicalDevelopmentCorpusHandle,
    CanonicalDevelopmentMaterialRef,
)

_LOG = logging.getLogger(__name__)


class AimeDevelopmentAdapter:
    """Expose canonical AIME development material and reuse its trusted task executor."""

    benchmark_id = "aime"

    def __init__(
        self,
        *,
        bench: BenchAdapter,
        plan: CampaignCellPlan,
        development_task_ids: tuple[str, ...],
        layout: RunLayout,
        gateway: TrustedEvaluationGateway,
        corpus: CanonicalDevelopmentCorpusHandle,
        cell_budget: CellBudget,
        provider_name: str = "unknown",
        model_name: str = "unknown",
        task_concurrency: int = 1,
    ) -> None:
        if (
            plan.benchmark != self.benchmark_id
            or corpus.benchmark != self.benchmark_id
            or corpus.task_ids != development_task_ids
            or not set(development_task_ids).issubset(plan.development_task_ids)
            or layout.benchmark != plan.cell_id
        ):
            raise RuntimeCampaignError("AIME development authority binding drifted")
        if (
            isinstance(task_concurrency, bool)
            or not isinstance(task_concurrency, int)
            or task_concurrency < 1
        ):
            raise ValueError("AIME Arena development task concurrency must be positive")
        tasks = {
            task.task_id: (
                task
                if task.benchmark_id == self.benchmark_id
                else dataclasses.replace(task, benchmark_id=self.benchmark_id)
            )
            for task in bench.list_tasks()
            if task.task_id in corpus.task_ids
        }
        if (
            tuple(task_id for task_id in corpus.task_ids if task_id in tasks)
            != corpus.task_ids
        ):
            raise RuntimeCampaignError("AIME Arena development tasks are unavailable")
        self._layout = layout
        self._gateway = gateway
        self._corpus = corpus
        self._tasks = tasks
        self._task_id_set = frozenset(corpus.task_ids)
        self._task_concurrency = task_concurrency
        self._trajectory_harvest_supported = isinstance(bench, AimeBench)
        self._executor = AimeCampaignBenchmarkAdapter(
            bench,
            trusted_gateway=gateway,
            layout=layout,
            cell_budget=cell_budget,
            provider_name=provider_name,
            model_name=model_name,
        )
        materials: dict[str, list[CanonicalDevelopmentMaterialRef]] = {
            task_id: [] for task_id in corpus.task_ids
        }
        for material in corpus.materials:
            if material.task_id not in materials:
                raise RuntimeCampaignError(
                    "AIME Arena development material escaped membership"
                )
            materials[material.task_id].append(material)
        if any(
            tuple(item.field for item in materials[task_id]) != corpus.canonical_fields
            for task_id in corpus.task_ids
        ):
            raise RuntimeCampaignError("AIME Arena development material fields drifted")
        self._materials = materials
        self._evidence_root = layout.root / "development"
        self._evidence_root.mkdir(parents=True, exist_ok=True)

    @property
    def frozen_task_ids(self) -> tuple[str, ...]:
        return self._corpus.task_ids

    @property
    def task_concurrency(self) -> int:
        """Frozen development-task worker ceiling consumed by this bench-owned adapter."""

        return self._task_concurrency

    def public_material(self, task_id: str) -> DevelopmentMaterial:
        self._authorize(task_id)
        fields: dict[str, object] = {}
        artifacts: list[DevelopmentArtifact] = []
        for material in self._materials[task_id]:
            paths: list[str] = []
            for source in material.source_paths:
                relative = _task_relative_path(source, task_id)
                artifact_path = f"{material.field}/{relative}"
                paths.append(artifact_path)
                artifacts.append(
                    DevelopmentArtifact(artifact_path, source.read_bytes())
                )
            fields[material.field] = {
                "status": material.status,
                "content_ref": material.content_ref,
                "content_digest": material.content_digest,
                "file_count": material.file_count,
                "total_bytes": material.total_bytes,
                "artifact_paths": paths,
            }
        return DevelopmentMaterial(
            task_id=task_id,
            task={
                "benchmark_id": self.benchmark_id,
                "task_id": task_id,
                "canonical_fields": list(self._corpus.canonical_fields),
                "fields": fields,
            },
            artifacts=tuple(artifacts),
        )

    def format_failed_task_brief(
        self, material: DevelopmentMaterial
    ) -> Mapping[str, object]:
        """Render only the public AIME problem and answer from frozen bytes."""

        if material.task_id not in self._task_id_set:
            raise RuntimeCampaignError("AIME failed-task material escaped membership")
        instruction = _public_artifact_text(material, "task_instruction/")
        solution = _public_artifact_text(material, "solution_or_gold/")
        match = re.search(
            r"echo\s+[\"']?([0-9]{1,3})[\"']?\s*>\s*/app/answer\.txt",
            solution,
        )
        answer = match.group(1) if match is not None else solution
        return {"instruction": instruction, "answer": answer}

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
                "AIME Arena development execution escaped membership"
            )
        started_at = time.time()
        started_monotonic = time.monotonic()
        public_trajectory: list[dict[str, object]] = [
            {
                "event": "development_evaluation_started",
                "stable_code_view_id": stable_code_view_id,
                "task_ids": list(task_ids),
            }
        ]
        trusted_trajectory: list[dict[str, object]] = [
            {
                "schema": "aime-arena-development-trajectory",
                "event": "development_evaluation_started",
                "development_evaluation_id": development_evaluation_id,
                "stable_code_view_id": stable_code_view_id,
                "task_ids": list(task_ids),
                "timestamp": started_at,
            }
        ]
        outcomes: dict[str, TaskEvaluationOutcome] = {}
        public_artifacts: list[DevelopmentArtifact] = []
        try:
            candidate = self._gateway.materialize(Path(stable_code_view))
            self._gateway.validate(candidate)

            def evaluate(
                task_id: str,
            ) -> tuple[
                str, TaskEvaluationOutcome, HarvestedDevelopmentAgentTrajectory | None
            ]:
                harvested: HarvestedDevelopmentAgentTrajectory | None = None

                def preserve(trial_artifact_root: Path) -> None:
                    nonlocal harvested
                    harvested = harvest_development_agent_trajectory(
                        task_id=task_id,
                        trial_artifact_root=trial_artifact_root,
                        durable_artifact_root=(
                            self._evidence_root
                            / "agent-trajectory-artifacts"
                            / development_evaluation_id
                        ),
                    )

                evaluate_kwargs = (
                    {"pre_cleanup_trial_hook": preserve}
                    if self._trajectory_harvest_supported
                    else {}
                )
                try:
                    outcome = self._executor.evaluate_task(
                        candidate,
                        self._authorize(task_id),
                        **evaluate_kwargs,
                    )
                except Exception as exc:  # noqa: BLE001 - one task is inconclusive
                    _LOG.warning(
                        "AIME development task was inconclusive: task=%s type=%s",
                        task_id,
                        type(exc).__name__,
                    )
                    outcome = TaskEvaluationOutcome(
                        outcome="inconclusive",
                        summary="trusted AIME development task evaluation failed",
                        provenance={
                            "development_agent_status": "inconclusive",
                            "exception_type": type(exc).__name__,
                        },
                    )
                return task_id, outcome, harvested

            with ThreadPoolExecutor(
                max_workers=min(self._task_concurrency, len(task_ids)),
                thread_name_prefix="aime-development-task",
            ) as pool:
                evaluated = tuple(pool.map(evaluate, task_ids))
            for task_id, outcome, harvested in evaluated:
                outcomes[task_id] = outcome
                public_result = _public_result(task_id, outcome)
                public_trajectory.append(
                    {
                        "event": "development_task_terminal",
                        **public_result,
                    }
                )
                agent_status = outcome.provenance.get("development_agent_status")
                if harvested is not None and agent_status in {
                    "completed",
                    "timeout",
                }:
                    public_event = harvested.public_event(agent_status)
                    if public_event is not None:
                        public_trajectory.append(dict(public_event))
                    public_artifacts.extend(harvested.artifacts)
        except Exception as exc:
            ended_at = time.time()
            wall_seconds = time.monotonic() - started_monotonic
            task_results = [
                {
                    "task_id": task_id,
                    "outcome": "inconclusive",
                    "score": None,
                    "summary": "trusted AIME development evaluation failed",
                }
                for task_id in task_ids
            ]
            public_trajectory.append(
                {"event": "development_evaluation_terminal", "outcome": "inconclusive"}
            )
            trusted_trajectory.append(
                {
                    "schema": "aime-arena-development-trajectory",
                    "event": "development_evaluation_terminal",
                    "development_evaluation_id": development_evaluation_id,
                    "outcome": "inconclusive",
                    "exception_type": type(exc).__name__,
                    "timestamp": ended_at,
                }
            )
            self._append_trajectory(
                development_evaluation_id, tuple(trusted_trajectory)
            )
            self._append(
                "evaluations.jsonl",
                (
                    {
                        "schema": "aime-arena-development-evaluation",
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
                        "summary": "trusted AIME development evaluation failed",
                        "exception_type": type(exc).__name__,
                        "started_at": started_at,
                        "ended_at": ended_at,
                        "wall_seconds": wall_seconds,
                    },
                ),
            )
            return DevelopmentExecution(
                outcome="inconclusive",
                result={
                    "benchmark_id": self.benchmark_id,
                    "task_ids": list(task_ids),
                    "outcome": "inconclusive",
                    "score": None,
                    "n_expected": len(task_ids),
                    "n_conclusive": 0,
                    "n_inconclusive": len(task_ids),
                    "coverage": 0.0,
                    "task_results": task_results,
                    "summary": "trusted AIME development evaluation was inconclusive",
                },
                usage={"cost_status": "missing", "tasks": []},
                trajectory=tuple(public_trajectory),
            )

        ended_at = time.time()
        wall_seconds = time.monotonic() - started_monotonic
        measured = [
            outcome for outcome in outcomes.values() if outcome.outcome == "measured"
        ]
        public_outcome = "measured" if measured else "inconclusive"
        task_results = [
            _public_result(task_id, outcomes[task_id]) for task_id in task_ids
        ]
        task_usage = [
            self._record_usage(development_evaluation_id, task_id, outcomes[task_id])
            for task_id in task_ids
        ]
        score = (
            sum(float(outcome.score) for outcome in measured) / len(measured)
            if measured
            else None
        )
        n_inconclusive = len(task_ids) - len(measured)
        coverage = len(measured) / len(task_ids)
        public_trajectory.append(
            {"event": "development_evaluation_terminal", "outcome": public_outcome}
        )
        trusted_trajectory.append(
            {
                "schema": "aime-arena-development-trajectory",
                "event": "development_evaluation_terminal",
                "development_evaluation_id": development_evaluation_id,
                "outcome": public_outcome,
                "timestamp": ended_at,
            }
        )
        self._append_trajectory(development_evaluation_id, tuple(trusted_trajectory))
        self._append(
            "evaluations.jsonl",
            (
                {
                    "schema": "aime-arena-development-evaluation",
                    "development_evaluation_id": development_evaluation_id,
                    "stable_code_view_id": stable_code_view_id,
                    "task_ids": list(task_ids),
                    "outcome": public_outcome,
                    "n_expected": len(task_ids),
                    "n_conclusive": len(measured),
                    "n_inconclusive": n_inconclusive,
                    "score": score,
                    "coverage": coverage,
                    "task_results": task_results,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "wall_seconds": wall_seconds,
                },
            ),
        )
        return DevelopmentExecution(
            outcome=public_outcome,
            result={
                "benchmark_id": self.benchmark_id,
                "task_ids": list(task_ids),
                "outcome": public_outcome,
                "score": score,
                "n_expected": len(task_ids),
                "n_conclusive": len(measured),
                "n_inconclusive": n_inconclusive,
                "coverage": coverage,
                "task_results": task_results,
            },
            usage={
                "cost_status": _aggregate_status(
                    tuple(outcome.cost_status for outcome in outcomes.values())
                ),
                "tasks": task_usage,
            },
            trajectory=tuple(public_trajectory),
            artifacts=tuple(public_artifacts),
        )

    def _authorize(self, task_id: str) -> TaskRef:
        if task_id not in self._task_id_set:
            raise RuntimeCampaignError(
                "task ID is not in frozen AIME development membership"
            )
        return self._tasks[task_id]

    def _record_usage(
        self,
        development_evaluation_id: str,
        task_id: str,
        outcome: TaskEvaluationOutcome,
    ) -> dict[str, object]:
        raw = outcome.provenance.get("usage")
        usage = raw if isinstance(raw, Mapping) else {}
        raw_calls = usage.get("calls", [])
        if not isinstance(raw_calls, list) or any(
            not isinstance(call, Mapping) for call in raw_calls
        ):
            raise RuntimeCampaignError("AIME provider call accounting is invalid")
        calls = [dict(call) for call in raw_calls]
        public: dict[str, object] = {
            "task_id": task_id,
            "role": "development_task_worker",
            "provider": str(usage.get("provider", "unknown")),
            "model": str(usage.get("model", "unknown")),
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "cache_read_tokens": usage.get("cache_read_tokens", outcome.cache_tokens),
            "input_token_status": outcome.input_token_status,
            "output_token_status": outcome.output_token_status,
            "cache_token_status": outcome.cache_token_status,
            "token_status": outcome.token_status,
            "provider_reported_cost": (
                outcome.cost_usd if outcome.cost_status == "reported" else None
            ),
            "cost_status": outcome.cost_status,
            "wall_seconds": float(outcome.wall_seconds or 0.0),
            "calls": calls,
        }
        return public

    def _append(self, name: str, rows: tuple[Mapping[str, object], ...]) -> None:
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


def _task_relative_path(path: Path, task_id: str) -> str:
    parts = path.resolve(strict=True).parts
    positions = [index for index, part in enumerate(parts) if part == task_id]
    if not positions or positions[-1] == len(parts) - 1:
        raise RuntimeCampaignError("AIME canonical material path is misbound")
    return Path(*parts[positions[-1] + 1 :]).as_posix()


def _public_artifact_text(material: DevelopmentMaterial, prefix: str) -> str:
    chunks: list[str] = []
    for artifact in sorted(material.artifacts, key=lambda item: item.path):
        if not artifact.path.startswith(prefix):
            continue
        try:
            text = artifact.content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text.strip():
            chunks.append(text.strip())
    return "\n\n".join(chunks)


def _public_result(task_id: str, outcome: TaskEvaluationOutcome) -> dict[str, object]:
    result: dict[str, object] = {
        "task_id": task_id,
        "outcome": outcome.outcome,
        "score": outcome.score,
    }
    if outcome.summary is not None:
        result["summary"] = outcome.summary
    raw_diagnostic = outcome.provenance.get(CANDIDATE_RUNTIME_FAILURE_KEY)
    if raw_diagnostic is not None:
        try:
            diagnostic = validated_candidate_runtime_failure_marker(raw_diagnostic)
        except AgentContextValidationError:
            pass
        else:
            result["diagnostic"] = diagnostic
    return result


def _aggregate_status(statuses: tuple[str, ...]) -> str:
    if "unverified" in statuses:
        return "unverified"
    if "missing" in statuses:
        return "missing"
    return "reported"


__all__ = ["AimeDevelopmentAdapter"]
