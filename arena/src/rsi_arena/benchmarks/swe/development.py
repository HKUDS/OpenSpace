"""Benchmark-owned SWE development adapter for the shared Arena owner."""

from __future__ import annotations

import stat
import time
from collections.abc import Mapping, Sequence
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
from rsi_arena.development import (
    DevelopmentArtifact,
    DevelopmentExecution,
    DevelopmentMaterial,
)
from rsi_arena.evaluation_gateway import TrustedEvaluationGateway
from rsi_arena.run_layout import AppendOnlyJsonl, RunLayout, TaskEvaluationOutcome
from rsi_arena.runtime_campaign import CampaignCellPlan, RuntimeCampaignError

from .campaign import (
    SweCampaignBenchmarkAdapter,
    SweTaskCellRunner,
    TrustedSweTaskSpec,
)


class SweDevelopmentAdapter:
    """Expose frozen SWE development material and reuse the existing task-cell backend."""

    benchmark_id = "swe"

    def __init__(
        self,
        *,
        plan: CampaignCellPlan,
        development_task_ids: tuple[str, ...],
        layout: RunLayout,
        gateway: TrustedEvaluationGateway,
        task_specs: Sequence[TrustedSweTaskSpec],
        task_runner: SweTaskCellRunner,
        task_concurrency: int = 1,
    ) -> None:
        specs = tuple(task_specs)
        if (
            plan.benchmark != self.benchmark_id
            or tuple(spec.task_id for spec in specs) != development_task_ids
            or not set(development_task_ids).issubset(plan.development_task_ids)
            or any(
                spec.operation != "development"
                or spec.task_snapshot_digest != plan.freeze_id
                or spec.split_digest != plan.freeze_id
                for spec in specs
            )
        ):
            raise RuntimeCampaignError("SWE development adapter authority is invalid")
        if (
            isinstance(task_concurrency, bool)
            or not isinstance(task_concurrency, int)
            or task_concurrency < 1
        ):
            raise ValueError("SWE development task concurrency must be positive")
        self._development_task_ids = development_task_ids
        self._layout = layout
        self._gateway = gateway
        self._specs = {spec.task_id: spec for spec in specs}
        self._adapter = SweCampaignBenchmarkAdapter(task_runner, specs)
        self._model_name = task_runner.model_name
        self._task_concurrency = task_concurrency
        self._trusted_root = layout.root / "development" / "swe-trusted"
        self._trusted_root.mkdir(parents=True, mode=0o700, exist_ok=True)

    @property
    def frozen_task_ids(self) -> tuple[str, ...]:
        return self._development_task_ids

    @property
    def task_concurrency(self) -> int:
        """Frozen development-task worker ceiling for this adapter."""

        return self._task_concurrency

    def public_material(self, task_id: str) -> DevelopmentMaterial:
        spec = self._authorize(task_id)
        artifacts: list[DevelopmentArtifact] = []
        for root_name in ("tests", "solution"):
            root = spec.task_dir / root_name
            if root.is_file():
                artifacts.append(
                    DevelopmentArtifact(root_name, self._regular_bytes(root))
                )
            elif root.is_dir():
                for path in sorted(
                    root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
                ):
                    if path.is_dir():
                        continue
                    relative = path.relative_to(spec.task_dir).as_posix()
                    artifacts.append(
                        DevelopmentArtifact(relative, self._regular_bytes(path))
                    )
        return DevelopmentMaterial(
            task_id=task_id,
            task={
                "instruction": spec.instruction,
                "repo": spec.repo,
                "base_commit": spec.base_commit,
                "task_local_tests_path": "artifacts/tests",
                "solution_or_gold_path": "artifacts/solution",
                "public_material_digest": spec.public_material_digest,
            },
            artifacts=tuple(artifacts),
        )

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
            or not set(task_ids).issubset(self._specs)
        ):
            raise RuntimeCampaignError(
                "SWE development execution escaped frozen membership"
            )
        started_at = time.time()
        started = time.monotonic()
        public_events: list[dict[str, object]] = [
            {
                "event": "development_evaluation_started",
                "stable_code_view_id": stable_code_view_id,
                "task_ids": list(task_ids),
            }
        ]
        public_artifacts: list[DevelopmentArtifact] = []
        harvested_by_task: dict[
            str, tuple[HarvestedDevelopmentAgentTrajectory, str]
        ] = {}
        try:
            candidate = self._gateway.materialize(Path(stable_code_view))

            def evaluate(
                task_id: str,
            ) -> tuple[
                str,
                TaskEvaluationOutcome,
                HarvestedDevelopmentAgentTrajectory | None,
            ]:
                harvested: HarvestedDevelopmentAgentTrajectory | None = None

                def preserve(trial_artifact_root: Path) -> None:
                    nonlocal harvested
                    harvested = harvest_development_agent_trajectory(
                        task_id=task_id,
                        trial_artifact_root=trial_artifact_root,
                        durable_artifact_root=(
                            self._trusted_root
                            / "agent-trajectory-artifacts"
                            / development_evaluation_id
                        ),
                    )

                return (
                    task_id,
                    self._adapter.evaluate_task_spec(
                        candidate,
                        self._authorize(task_id),
                        pre_cleanup_trial_hook=preserve,
                    ),
                    harvested,
                )

            with ThreadPoolExecutor(
                max_workers=min(self._task_concurrency, len(task_ids)),
                thread_name_prefix="swe-development-task",
            ) as pool:
                outcomes = tuple(pool.map(evaluate, task_ids))
        except Exception:
            outcomes = ()
            task_results = [
                {
                    "task_id": task_id,
                    "outcome": "inconclusive",
                    "score": None,
                    "summary": "trusted SWE development evaluation failed",
                }
                for task_id in task_ids
            ]
            usage_tasks: list[dict[str, object]] = []
            failure = "trusted SWE development evaluation was inconclusive"
        else:
            task_results = [
                self._public_outcome(task_id, outcome)
                for task_id, outcome, _harvested in outcomes
            ]
            usage_tasks = [
                self._public_usage(task_id, outcome)
                for task_id, outcome, _harvested in outcomes
            ]
            for task_id, outcome, harvested in outcomes:
                agent_status = outcome.provenance.get("development_agent_status")
                if harvested is not None and agent_status in {
                    "completed",
                    "timeout",
                }:
                    harvested_by_task[task_id] = (harvested, str(agent_status))
            failure = (
                None
                if any(
                    outcome.outcome == "measured"
                    for _task_id, outcome, _harvested in outcomes
                )
                else "all SWE development tasks were inconclusive"
            )
        for task_result in task_results:
            public_events.append({"event": "development_task_terminal", **task_result})
            harvested_item = harvested_by_task.get(str(task_result["task_id"]))
            if harvested_item is not None:
                harvested, agent_status = harvested_item
                public_event = harvested.public_event(
                    "timeout" if agent_status == "timeout" else "completed"
                )
                if public_event is not None:
                    public_events.append(dict(public_event))
                public_artifacts.extend(harvested.artifacts)
        measured_scores = [
            float(task_result["score"])
            for task_result in task_results
            if task_result["outcome"] == "measured"
        ]
        public_outcome = "measured" if measured_scores else "inconclusive"
        n_inconclusive = len(task_ids) - len(measured_scores)
        score = sum(measured_scores) / len(measured_scores) if measured_scores else None
        wall_seconds = time.monotonic() - started
        public_events.append(
            {"event": "development_evaluation_terminal", "outcome": public_outcome}
        )
        trusted = {
            "schema": "swe-development-evaluation",
            "run_id": self._layout.run_id,
            "development_evaluation_id": development_evaluation_id,
            "stable_code_view_id": stable_code_view_id,
            "task_ids": list(task_ids),
            "outcome": public_outcome,
            "score": score,
            "n_expected": len(task_ids),
            "n_conclusive": len(measured_scores),
            "n_inconclusive": n_inconclusive,
            "coverage": len(measured_scores) / len(task_ids),
            "task_results": task_results,
            "started_at": started_at,
            "ended_at": time.time(),
            "wall_seconds": wall_seconds,
        }
        self._append("evaluations.jsonl", trusted)
        for row in usage_tasks:
            self._append(
                "usage.jsonl",
                {
                    "schema": "swe-development-usage",
                    "run_id": self._layout.run_id,
                    "development_evaluation_id": development_evaluation_id,
                    **row,
                },
            )
        result: dict[str, object] = {
            "benchmark_id": self.benchmark_id,
            "outcome": public_outcome,
            "score": score,
            "n_expected": len(task_ids),
            "n_conclusive": len(measured_scores),
            "n_inconclusive": n_inconclusive,
            "coverage": len(measured_scores) / len(task_ids),
            "task_results": task_results,
        }
        if failure is not None:
            result["summary"] = failure
        return DevelopmentExecution(
            outcome=public_outcome,
            result=result,
            usage={
                "model": self._model_name,
                "tasks": usage_tasks,
                "cost_status": self._aggregate_cost_status(usage_tasks),
            },
            trajectory=tuple(public_events),
            artifacts=tuple(public_artifacts),
        )

    def _authorize(self, task_id: str) -> TrustedSweTaskSpec:
        try:
            return self._specs[task_id]
        except KeyError as exc:
            raise RuntimeCampaignError(
                "SWE development task is outside frozen membership"
            ) from exc

    def _append(self, name: str, row: Mapping[str, object]) -> None:
        writer = AppendOnlyJsonl(self._trusted_root / name)
        try:
            writer.append(row)
        finally:
            writer.close()

    @staticmethod
    def _regular_bytes(path: Path) -> bytes:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeCampaignError("SWE development public material is not regular")
        return path.read_bytes()

    @staticmethod
    def _public_outcome(task_id: str, outcome) -> dict[str, object]:
        result = {
            "task_id": task_id,
            "outcome": outcome.outcome,
            "score": outcome.score if outcome.outcome == "measured" else None,
            "summary": outcome.summary,
            "wall_seconds": outcome.wall_seconds,
        }
        raw_diagnostic = outcome.provenance.get(CANDIDATE_RUNTIME_FAILURE_KEY)
        if raw_diagnostic is not None:
            try:
                diagnostic = validated_candidate_runtime_failure_marker(raw_diagnostic)
            except AgentContextValidationError:
                pass
            else:
                result["diagnostic"] = diagnostic
        return result

    def _public_usage(self, task_id: str, outcome) -> dict[str, object]:
        usage = outcome.provenance.get("usage")
        usage = dict(usage) if isinstance(usage, Mapping) else {}
        raw_calls = usage.get("calls", [])
        if not isinstance(raw_calls, list) or any(
            not isinstance(call, Mapping) for call in raw_calls
        ):
            raise RuntimeCampaignError("SWE provider call accounting is invalid")
        provider = str(usage.get("provider", "unknown"))
        model = str(usage.get("model", self._model_name))
        return {
            "task_id": task_id,
            "role": "development_task_worker",
            "provider": provider,
            "model": model,
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "cache_tokens": outcome.cache_tokens,
            "input_token_status": outcome.input_token_status,
            "output_token_status": outcome.output_token_status,
            "cache_token_status": outcome.cache_token_status,
            "provider_reported_cost": (
                outcome.cost_usd if outcome.cost_status == "reported" else None
            ),
            "cost_status": outcome.cost_status,
            "wall_seconds": outcome.wall_seconds,
            "calls": [
                {"provider": provider, "model": model, **dict(call)}
                for call in raw_calls
            ],
        }

    @staticmethod
    def _aggregate_cost_status(rows: Sequence[Mapping[str, object]]) -> str:
        statuses = {str(row.get("cost_status", "missing")) for row in rows}
        if not statuses or "missing" in statuses:
            return "missing"
        if "unverified" in statuses:
            return "unverified"
        return "reported"


__all__ = ["SweDevelopmentAdapter"]
