"""SWE task resolution and Arena outcome projection.

Candidate admission and durable lifecycle stay in Arena.  This extra freezes
SWE task identity, delegates one fresh task cell, and maps its trusted result
to the two Arena outcomes.  It never opens held-out material outside the
trusted evaluator.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from rsi_arena.benchmarks.harbor_runtime.materials import (
    resolve_task_dir,
    snapshot_task_materials,
)
from rsi_arena.benchmarks.swe.docker_runtime import (
    OfficialSweDockerError,
    OfficialSweTaskIdentity,
    SwePackagedRuntimeIdentity,
    inspect_official_swe_task,
)
from rsi_arena.contracts import TaskRef, canonical_digest
from rsi_arena.evaluation_gateway import GatewayCandidate
from rsi_arena.manifests import (
    SampleSlot,
)
from rsi_arena.run_layout import (
    RunValidationError,
    TaskEvaluationOutcome,
)
from rsi_arena.runtime_campaign import (
    CampaignCellPlan,
    RuntimeCampaignError,
    runtime_realization_record,
)


class SweBatchError(RuntimeError):
    """The frozen SWE task boundary failed closed."""


class SweTaskCellInfraError(RuntimeError):
    """Sanitized task-cell failure safe to place in neutral outcomes."""

    def __init__(
        self,
        *,
        cell_id: str,
        error_type: str,
        artifact_refs: Mapping[str, str] | None = None,
        provenance: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__("trusted SWE task cell reported an infrastructure error")
        self.cell_id = str(cell_id)
        self.error_type = str(error_type)
        self.artifact_refs = dict(artifact_refs or {})
        self.provenance = dict(provenance or {})
        if not self.cell_id or not self.error_type:
            raise ValueError("sanitized task-cell failure identity is empty")


@dataclass(frozen=True)
class TrustedSweTaskSpec:
    """Trusted controller-only description of one pinned SWE task package."""

    task_id: str
    task_dir: Path = field(repr=False)
    instruction: str = field(repr=False)
    repo: str
    base_commit: str
    source_image: str
    public_material_digest: str
    evaluator_material_digest: str = field(repr=False)
    static_evaluator_digest: str
    task_snapshot_digest: str
    split_digest: str
    operation: str
    official_task_identity: OfficialSweTaskIdentity = field(repr=False)

    def candidate_view(self) -> Mapping[str, object]:
        """Return only the task identity/instruction allowed into the cell."""

        return MappingProxyType(
            {
                "task_id": self.task_id,
                "instruction": self.instruction,
                "repo": self.repo,
                "base_commit": self.base_commit,
            }
        )


@dataclass(frozen=True)
class SweTaskMeasurement:
    """One trusted binary measurement returned by a SWE task cell."""

    task_id: str
    score: float
    wall_seconds: float
    cell_id: str
    artifact_refs: Mapping[str, str] = field(default_factory=dict)
    provenance: Mapping[str, object] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    token_status: str = "missing"
    cost_usd: float | None = None
    cost_status: str = "missing"

    def __post_init__(self) -> None:
        if not self.task_id or not self.cell_id:
            raise ValueError("SWE task measurement needs task and fresh-cell identity")
        if not math.isfinite(float(self.wall_seconds)) or self.wall_seconds < 0:
            raise ValueError("SWE task wall time must be finite and non-negative")
        if self.score not in {0, 1}:
            raise ValueError("SWE measurement must have a binary score")
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"invalid {name}")
        if self.token_status not in {"reported", "missing", "unverified"}:
            raise ValueError("invalid task token status")
        if self.token_status == "missing" and (self.input_tokens or self.output_tokens):
            raise ValueError("missing task tokens cannot carry counts")
        if self.cost_status not in {"reported", "missing", "unverified"}:
            raise ValueError("invalid task cost status")
        if self.cost_status == "reported":
            if (
                not isinstance(self.cost_usd, (int, float))
                or isinstance(self.cost_usd, bool)
                or not math.isfinite(float(self.cost_usd))
                or float(self.cost_usd) < 0
            ):
                raise ValueError("reported task cost is invalid")
        elif self.cost_usd is not None:
            raise ValueError("unknown task cost must be null")


class SweTaskCellRunner(Protocol):
    """Minimal shared logical task-cell interface."""

    @property
    def backend_digest(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def run_frozen_candidate_on_task(
        self,
        frozen_candidate: GatewayCandidate,
        trusted_task_spec: TrustedSweTaskSpec,
        *,
        pre_cleanup_trial_hook: Callable[[Path], None] | None = None,
    ) -> SweTaskMeasurement: ...


def _prepare_task(
    plan: CampaignCellPlan,
    task_id: str,
    *,
    snapshot_root: Path,
    operation: str,
) -> TrustedSweTaskSpec:
    if plan.benchmark != "swe":
        raise SweBatchError("task preparation requires a frozen SWE campaign cell")
    task_dir = resolve_task_dir(Path(snapshot_root), task_id)
    identity = inspect_official_swe_task(task_dir, expected_task_id=task_id)
    materials = snapshot_task_materials(task_dir)
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
    return TrustedSweTaskSpec(
        task_id=task_id,
        task_dir=task_dir,
        instruction=instruction,
        repo=identity.repo,
        base_commit=identity.base_commit,
        source_image=identity.source_image,
        public_material_digest=materials.public_digest,
        evaluator_material_digest=materials.evaluator_digest,
        static_evaluator_digest=identity.static_evaluator_digest,
        task_snapshot_digest=plan.freeze_id,
        split_digest=plan.freeze_id,
        operation=operation,
        official_task_identity=identity,
    )


def prepare_swe_development_task(
    plan: CampaignCellPlan,
    task_id: str,
    *,
    snapshot_root: Path,
) -> TrustedSweTaskSpec:
    """Prepare one task selected from frozen development membership."""

    selected = str(task_id)
    if plan.benchmark != "swe" or selected not in plan.development_task_ids:
        raise SweBatchError(
            "SWE development task is outside frozen development membership"
        )
    return _prepare_task(
        plan, selected, snapshot_root=Path(snapshot_root), operation="development"
    )


def prepare_swe_attempt_evaluation_tasks(
    plan: CampaignCellPlan,
    *,
    snapshot_root: Path,
    batch_task_ids: Sequence[str] | None = None,
) -> tuple[TrustedSweTaskSpec, ...]:
    """Prepare development metadata without selecting an attempt subset."""

    selected = (
        tuple(map(str, batch_task_ids))
        if batch_task_ids is not None
        else plan.development_task_ids
    )
    if (
        not selected
        or len(set(selected)) != len(selected)
        or any(task_id not in plan.development_task_ids for task_id in selected)
    ):
        raise RuntimeCampaignError(
            "SWE task metadata selection is outside frozen development"
        )
    return tuple(
        _prepare_task(
            plan,
            task_id,
            snapshot_root=Path(snapshot_root),
            operation="attempt-evaluation",
        )
        for task_id in selected
    )


def prepare_swe_held_out_tasks(
    plan: CampaignCellPlan,
    task_ids: Sequence[str],
    *,
    snapshot_root: Path,
) -> tuple[TrustedSweTaskSpec, ...]:
    """Prepare frozen held-out membership inside the trusted evaluator."""

    selected = tuple(map(str, task_ids))
    if not selected or len(set(selected)) != len(selected):
        raise SweBatchError("SWE held-out membership is empty or duplicated")
    return tuple(
        _prepare_task(
            plan,
            task_id,
            snapshot_root=Path(snapshot_root),
            operation="held-out",
        )
        for task_id in selected
    )


def swe_evaluator_digest(
    runner: SweTaskCellRunner,
    task_specs: Sequence[TrustedSweTaskSpec],
) -> str:
    """Bind task evaluators without planning per-task runtime content."""

    specs = tuple(task_specs)
    if not specs:
        raise ValueError("SWE evaluator identity requires tasks")
    return canonical_digest(
        {
            "schema": "swe-task-cell-evaluator",
            "backend_digest": runner.backend_digest,
            "ordered_tasks": [
                {
                    "task_id": spec.task_id,
                    "static_evaluator_digest": spec.static_evaluator_digest,
                    "public_material_digest": spec.public_material_digest,
                    "evaluator_material_digest": spec.evaluator_material_digest,
                }
                for spec in specs
            ],
        }
    )


def _validate_runtime_use_evidence(
    provenance: Mapping[str, object],
    spec: TrustedSweTaskSpec,
) -> SwePackagedRuntimeIdentity:
    if provenance.get("runtime_identity_recorded") is not True:
        raise OfficialSweDockerError("executed SWE task omitted runtime-use identity")
    raw = provenance.get("runtime_identity")
    if not isinstance(raw, Mapping):
        raise OfficialSweDockerError("executed SWE task omitted runtime-use identity")
    identity = SwePackagedRuntimeIdentity.from_dict(raw)
    if identity.source_image != spec.source_image:
        raise OfficialSweDockerError(
            "runtime-use identity is bound to another configured image"
        )

    source_export = provenance.get("source_export")
    if source_export is not None:
        if not isinstance(source_export, Mapping) or (
            source_export.get("schema") != "swe-source-export"
            or source_export.get("task_id") != spec.task_id
            or source_export.get("repo") != spec.repo
            or source_export.get("source_image") != identity.source_image
            or source_export.get("source_image_id") != identity.source_image_id
            or source_export.get("source_image_repo_digests")
            != list(identity.source_image_repo_digests)
            or source_export.get("runtime_image_ref") != identity.runtime_image_ref
            or source_export.get("repo_head") != identity.packaged_head
            or source_export.get("repo_tree") != identity.packaged_tree
            or source_export.get("runtime_identity_digest") != identity.digest
            or provenance.get("source_export_digest")
            != canonical_digest(dict(source_export))
        ):
            raise OfficialSweDockerError(
                "source export is misbound to runtime-use identity"
            )
    return identity


def _provenance_usage_outcome_fields(
    provenance: Mapping[str, object],
) -> dict[str, object]:
    """Preserve trusted task-worker usage even when a task has no outcome body."""

    raw = provenance.get("usage")
    usage = dict(raw) if isinstance(raw, Mapping) else {}

    def token(name: str, status_name: str) -> tuple[int | None, str]:
        value = usage.get(name)
        status = str(usage.get(status_name, "missing"))
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
            and status in {"reported", "recounted", "unverified"}
        ):
            return int(value), status
        return None, "missing" if status == "missing" else "unverified"

    input_tokens, input_status = token("input_tokens", "input_token_status")
    output_tokens, output_status = token("output_tokens", "output_token_status")
    cache_tokens, cache_status = token("cache_read_tokens", "cache_token_status")
    token_status = str(usage.get("token_status", "missing"))
    if token_status not in {"reported", "recounted", "missing", "unverified"}:
        token_status = "unverified"
    raw_cost = usage.get("provider_reported_cost")
    cost_status = str(usage.get("cost_status", "missing"))
    cost = (
        float(raw_cost)
        if cost_status == "reported"
        and isinstance(raw_cost, (int, float))
        and not isinstance(raw_cost, bool)
        and math.isfinite(float(raw_cost))
        and float(raw_cost) >= 0
        else None
    )
    if (
        cost_status == "reported"
        and cost is None
        or cost_status not in {"reported", "missing", "unverified"}
    ):
        cost_status = "unverified"
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_tokens": cache_tokens,
        "token_status": token_status,
        "input_token_status": input_status,
        "output_token_status": output_status,
        "cache_token_status": cache_status,
        "cost_usd": cost,
        "cost_status": cost_status,
    }


class SweCampaignBenchmarkAdapter:
    """Resolve persisted SWE slots and delegate one fresh trusted task cell."""

    benchmark_id = "swe"

    def __init__(
        self,
        runner: SweTaskCellRunner,
        task_specs: Sequence[TrustedSweTaskSpec],
    ) -> None:
        specs = tuple(task_specs)
        self._specs = {spec.task_id: spec for spec in specs}
        if not specs or len(self._specs) != len(specs):
            raise ValueError("SWE adapter needs unique task specs")
        self._runner = runner
        self._evaluator_digest = swe_evaluator_digest(runner, specs)
        self._cell_lock = threading.Lock()
        self._candidate_cells: dict[str, set[str]] = {}

    @property
    def evaluator_digest(self) -> str:
        return self._evaluator_digest

    @property
    def execution_backend(self) -> SweTaskCellRunner:
        return self._runner

    def resolve_tasks(
        self,
        plan: CampaignCellPlan,
        sample_slots: Sequence[SampleSlot],
    ) -> tuple[TaskRef, ...]:
        slots = tuple(sample_slots)
        if plan.benchmark != self.benchmark_id:
            raise RuntimeCampaignError("SWE adapter requires its frozen campaign plan")
        runtime_realization_record(plan, slots)
        try:
            specs = tuple(self._specs[slot.task_id] for slot in slots)
        except KeyError as exc:
            raise RunValidationError("SWE plan selected an unavailable task") from exc
        if any(
            spec.operation != "attempt-evaluation"
            or spec.task_snapshot_digest != plan.freeze_id
            or spec.split_digest != plan.freeze_id
            for spec in specs
        ):
            raise SweBatchError(
                "SWE task specs disagree with frozen development authority"
            )
        return tuple(
            TaskRef(task_id=spec.task_id, benchmark_id=self.benchmark_id)
            for spec in specs
        )

    def evaluate_task(
        self,
        candidate: GatewayCandidate,
        task: TaskRef,
    ) -> TaskEvaluationOutcome:
        if task.canonical_identity != (self.benchmark_id, task.task_id):
            raise RunValidationError("SWE task lost benchmark scope")
        spec = self._specs[task.task_id]
        if spec.operation not in {"attempt-evaluation", "held-out"}:
            raise RunValidationError("SWE task resolved unauthorized material")
        return self.evaluate_task_spec(candidate, spec)

    def evaluate_task_spec(
        self,
        candidate: GatewayCandidate,
        spec: TrustedSweTaskSpec,
        *,
        pre_cleanup_trial_hook: Callable[[Path], None] | None = None,
    ) -> TaskEvaluationOutcome:
        """Execute one trusted development/evaluation spec through the shared SWE cell projection."""

        if self._specs.get(spec.task_id) != spec or spec.operation not in {
            "development",
            "attempt-evaluation",
            "held-out",
        }:
            raise RunValidationError("SWE task spec is not adapter-authorized")
        started = time.monotonic()
        typed_failure: SweTaskCellInfraError | None = None
        try:
            hook_kwargs = (
                {"pre_cleanup_trial_hook": pre_cleanup_trial_hook}
                if pre_cleanup_trial_hook is not None
                else {}
            )
            outcome = self._runner.run_frozen_candidate_on_task(
                candidate, spec, **hook_kwargs
            )
        except OfficialSweDockerError:
            return TaskEvaluationOutcome(
                outcome="inconclusive",
                wall_seconds=time.monotonic() - started,
                summary="trusted SWE runtime resolution failed",
                provenance={
                    "runtime_identity_recorded": False,
                    "exception_detail_recorded": False,
                },
            )
        except SweTaskCellInfraError as exc:
            typed_failure = exc
        except Exception:
            return TaskEvaluationOutcome(
                outcome="inconclusive",
                wall_seconds=time.monotonic() - started,
                summary="trusted SWE backend raised an exception",
                provenance={
                    "exception_detail_recorded": False,
                },
            )
        if typed_failure is not None:
            failure = typed_failure
            duplicate_cell, provenance = self._register_cell(
                candidate, failure.cell_id, failure.provenance
            )
            invalid_runtime_evidence = False
            runtime_recorded = provenance.get("runtime_identity_recorded")
            if runtime_recorded is True:
                try:
                    _validate_runtime_use_evidence(provenance, spec)
                except OfficialSweDockerError:
                    invalid_runtime_evidence = True
            elif runtime_recorded is not False:
                invalid_runtime_evidence = True
            common = {
                "wall_seconds": time.monotonic() - started,
                "summary": (
                    "swe_task_cell_identity_reused"
                    if duplicate_cell
                    else (
                        "swe_runtime_use_evidence_missing"
                        if invalid_runtime_evidence
                        else failure.error_type
                    )
                ),
                "artifact_refs": failure.artifact_refs,
                "provenance": provenance,
                **_provenance_usage_outcome_fields(provenance),
            }
            return TaskEvaluationOutcome(outcome="inconclusive", **common)
        if (
            not isinstance(outcome, SweTaskMeasurement)
            or outcome.task_id != spec.task_id
        ):
            return TaskEvaluationOutcome(
                outcome="inconclusive",
                wall_seconds=time.monotonic() - started,
                summary="trusted SWE runner changed task identity",
            )
        duplicate_cell, provenance = self._register_cell(
            candidate, outcome.cell_id, outcome.provenance
        )
        try:
            _validate_runtime_use_evidence(provenance, spec)
        except OfficialSweDockerError:
            return TaskEvaluationOutcome(
                outcome="inconclusive",
                wall_seconds=outcome.wall_seconds,
                summary="required SWE runtime authority evidence is missing",
                artifact_refs=outcome.artifact_refs,
                provenance=provenance,
                **_provenance_usage_outcome_fields(provenance),
            )
        usage = provenance.get("usage")
        usage = dict(usage) if isinstance(usage, Mapping) else {}
        usage.setdefault(
            "token_source",
            "provider" if outcome.token_status == "reported" else "unknown",
        )
        usage.setdefault(
            "input_token_status",
            outcome.token_status if outcome.token_status != "missing" else "missing",
        )
        usage.setdefault(
            "output_token_status",
            outcome.token_status if outcome.token_status != "missing" else "missing",
        )
        usage.setdefault("cache_token_status", "missing")
        provenance["usage"] = usage
        raw_cache_tokens = usage.get("cache_read_tokens")
        cache_tokens = (
            int(raw_cache_tokens)
            if isinstance(raw_cache_tokens, int)
            and not isinstance(raw_cache_tokens, bool)
            and raw_cache_tokens >= 0
            else None
        )
        common = {
            "wall_seconds": outcome.wall_seconds,
            "artifact_refs": outcome.artifact_refs,
            "provenance": provenance,
            "input_tokens": (
                outcome.input_tokens if outcome.token_status == "reported" else None
            ),
            "output_tokens": (
                outcome.output_tokens if outcome.token_status == "reported" else None
            ),
            "token_status": outcome.token_status,
            "input_token_status": str(usage["input_token_status"]),
            "output_token_status": str(usage["output_token_status"]),
            "cache_tokens": cache_tokens,
            "cache_token_status": str(usage["cache_token_status"]),
            "cost_usd": outcome.cost_usd,
            "cost_status": outcome.cost_status,
        }
        if duplicate_cell:
            duplicate_common = {
                **common,
                "summary": "swe_task_cell_identity_reused",
            }
            return TaskEvaluationOutcome(
                outcome="inconclusive",
                **duplicate_common,
            )
        return TaskEvaluationOutcome(
            outcome="measured", score=float(outcome.score), **common
        )

    def _register_cell(
        self,
        candidate: GatewayCandidate,
        cell_id: str,
        provenance: Mapping[str, object],
    ) -> tuple[bool, dict[str, object]]:
        candidate_key = candidate.tree_sha
        with self._cell_lock:
            cells = self._candidate_cells.setdefault(candidate_key, set())
            duplicate_cell = cell_id in cells
            cells.add(cell_id)
        return duplicate_cell, {
            **provenance,
            "swe_task_cell_id": cell_id,
        }


__all__ = [
    "SweBatchError",
    "SweCampaignBenchmarkAdapter",
    "SweTaskCellInfraError",
    "SweTaskCellRunner",
    "SweTaskMeasurement",
    "TrustedSweTaskSpec",
    "prepare_swe_attempt_evaluation_tasks",
    "prepare_swe_development_task",
    "prepare_swe_held_out_tasks",
    "swe_evaluator_digest",
]
