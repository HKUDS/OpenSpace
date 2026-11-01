"""Durable restricted-B admission for an existing research artifact.

The proposer sees only aggregate paired evidence.  The frozen B plan, task
coordinates, cell measurements and exact artifact binding stay in this
adapter's private journal.  A started cell without a durable result is never
executed a second time by recovery.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from agentpilot.domain import (
    ImplementationAdherence,
    PolicyAction,
    canonical_json,
    stable_id,
)
from agentpilot.evaluation import (
    HT_ESTIMATOR_CONTRACT,
    UNIFORM_ESTIMATOR_CONTRACT,
    AdmissionFate,
    CellMeasurement,
    FrozenActionEvaluationPlan,
    FrozenEvaluationBatch,
    FrozenInformationPriorSnapshot,
    HorvitzThompsonPairedEstimator,
    MeasurementStatus,
    PairedEvidence,
    PairedEvaluationEngine,
    UniformPairedEstimator,
    decide_admission,
)
from agentpilot.persistence.sqlite import _dumps, _loads

from .action_settings import ActionRuntimeSettings
from .structural_adherence import (
    ImmutableGitReaderConfig,
    StructuralImplementationAdherenceVerifier,
    SubprocessImmutableGitObjectReader,
)
from .wave import (
    WAVE_INFORMATION_WEIGHTING_CONTRACT,
    freeze_census_plan,
    freeze_sampled_wave_plan,
    frozen_task_statistics,
)


class AdmissionRecoveryRequired(RuntimeError):
    """An admission cell may have incurred cost but has no settled result."""


class AdmissionPreflightError(ValueError):
    """A candidate failed a local check before any restricted-B dispatch."""


class AdmissionExecutionError(RuntimeError):
    """Private B execution stopped without exposing private material."""


class AdmissionActionFactory(Protocol):
    def files(self, commit: str) -> tuple[str, ...]: ...

    def create(self, **arguments: Any) -> PolicyAction: ...


class AdherenceVerifier(Protocol):
    def verify(
        self,
        action: PolicyAction,
        *,
        parent_commit_sha: str,
        result_commit_sha: str,
    ) -> object: ...


AdmissionPlanFactory = Callable[[str, PolicyAction], FrozenEvaluationBatch]


def _snapshot(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    try:
        result = json.loads(canonical_json(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must contain finite JSON values") from error
    if not isinstance(result, dict):  # pragma: no cover - Mapping guarantees this
        raise TypeError(f"{name} must be a mapping")
    return result


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    if len(value.encode("utf-8")) > 512:
        raise ValueError(f"{name} is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _artifact(value: object, name: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        raw_value = value
    else:
        raw_value = {
            field: getattr(value, field)
            for field in (
                "id", "artifact_id", "commit_sha", "config", "base_id", "track_id",
                "metadata",
            )
            if hasattr(value, field)
        }
    raw = _snapshot(raw_value, name)
    artifact_id = raw.get("artifact_id", raw.get("id"))
    commit_sha = raw.get("commit_sha")
    result = {
        "artifact_id": _text(artifact_id, f"{name}.artifact_id"),
        "commit_sha": _text(commit_sha, f"{name}.commit_sha"),
        "config": raw.get("config"),
        "base_id": raw.get("base_id"),
        "track_id": raw.get("track_id"),
        "metadata": raw.get("metadata", {}),
    }
    if result["config"] is not None and not isinstance(result["config"], dict):
        raise TypeError(f"{name}.config must be an object or null")
    if not isinstance(result["metadata"], dict):
        raise TypeError(f"{name}.metadata must be an object")
    for field in ("base_id", "track_id"):
        if result[field] is not None:
            result[field] = _text(result[field], f"{name}.{field}")
    return result


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False,
            prefix=f".{path.name}.",
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    """Create one dispatch marker atomically across processes."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _atomic_typed(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False,
            prefix=f".{path.name}.",
        ) as stream:
            temporary = Path(stream.name)
            if not isinstance(value, FrozenEvaluationBatch):
                raise TypeError("typed admission journal needs FrozenEvaluationBatch")
            stream.write(_dumps({
                "prior_snapshot": value.prior_snapshot,
                "action_plan": value.action_plans[0],
            }))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _measurement_payload(value: CellMeasurement) -> dict[str, Any]:
    return {
        "cell_id": value.cell_id,
        "program_id": value.program_id,
        "status": value.status.value,
        "primary_value": value.primary_value,
        "constraints_pass": value.constraints_pass,
        "diagnostics": _snapshot(value.diagnostics, "measurement diagnostics"),
    }


def _measurement(value: Mapping[str, Any]) -> CellMeasurement:
    return CellMeasurement(
        cell_id=str(value["cell_id"]),
        program_id=str(value["program_id"]),
        status=MeasurementStatus(str(value["status"])),
        primary_value=value.get("primary_value"),
        constraints_pass=value.get("constraints_pass"),
        diagnostics=_snapshot(value.get("diagnostics", {}), "measurement diagnostics"),
    )


def _measurement_cost(value: CellMeasurement) -> float | None:
    provider = value.diagnostics.get("provider_diagnostics")
    accounting = provider.get("accounting") if isinstance(provider, Mapping) else None
    cost = accounting.get("cost_usd") if isinstance(accounting, Mapping) else None
    if cost is None:
        return None
    if (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(float(cost))
        or float(cost) < 0.0
    ):
        raise ValueError("measurement provider cost is invalid")
    return float(cost)


@dataclass
class _JournaledCellExecutor:
    executor: object
    ticket_id: str
    root: Path
    plan: object

    def __post_init__(self) -> None:
        cells = tuple(getattr(self.plan, "cells"))
        self._ordinals = {cell.cell_id: ordinal for ordinal, cell in enumerate(cells)}
        self._lock = threading.Lock()

    def _paths(self, program_id: str, cell: object) -> tuple[Path, Path]:
        ordinal = self._ordinals.get(getattr(cell, "cell_id", None))
        if ordinal is None:
            raise ValueError("cell is absent from the frozen admission plan")
        side = hashlib.sha256(program_id.encode("utf-8")).hexdigest()[:16]
        execution_id = f"{self.ticket_id}/cell-{ordinal}/{side}"
        digest = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.started.json", self.root / f"{digest}.result.json"

    def measure(self, *, program_id: str, cell: object) -> CellMeasurement:
        started, result = self._paths(program_id, cell)
        ordinal = self._ordinals[getattr(cell, "cell_id")]
        execution_id = f"{self.ticket_id}/cell-{ordinal}/" + hashlib.sha256(
            program_id.encode("utf-8")
        ).hexdigest()[:16]
        with self._lock:
            if result.exists():
                loaded = json.loads(result.read_text(encoding="utf-8"))
                measurement = _measurement(loaded["measurement"])
                if (
                    loaded.get("execution_id") != execution_id
                    or measurement.program_id != program_id
                    or measurement.cell_id != getattr(cell, "cell_id")
                ):
                    raise AdmissionRecoveryRequired("settled admission cell changed identity")
                return measurement
            if started.exists():
                raise AdmissionRecoveryRequired(
                    "an admission cell started without a durable result; it was not repeated"
                )
            try:
                _exclusive_json(
                    started,
                    {
                        "execution_id": execution_id,
                        "ticket_id": self.ticket_id,
                        "program_id": program_id,
                        "cell": {
                            "cell_id": getattr(cell, "cell_id"),
                            "task_id": getattr(cell, "task_id"),
                            "block_id": getattr(cell, "block_id"),
                            "measurement_seed": getattr(cell, "measurement_seed"),
                            "replicate": getattr(cell, "replicate"),
                        },
                    },
                )
            except FileExistsError as error:
                raise AdmissionRecoveryRequired(
                    "an admission cell is already dispatched; it was not repeated"
                ) from error
        measured = self.executor.measure(program_id=program_id, cell=cell)
        if not isinstance(measured, CellMeasurement):
            raise TypeError("admission cell executor must return CellMeasurement")
        if measured.program_id != program_id or measured.cell_id != getattr(cell, "cell_id"):
            raise ValueError("admission cell executor changed frozen coordinates")
        _atomic_json(
            result,
            {"execution_id": execution_id, "measurement": _measurement_payload(measured)},
        )
        return measured


class FrozenBAdmissionEvaluator:
    """Evaluate one existing candidate on one privately frozen adaptive-B plan."""

    def __init__(
        self,
        *,
        runtime: ActionRuntimeSettings,
        request: object,
        executor: object,
        root: Path,
        action_factory: AdmissionActionFactory,
        plan_factory: AdmissionPlanFactory | None = None,
        adherence_verifier: AdherenceVerifier | None = None,
        artifact_resolver: Callable[[str], object] | None = None,
        estimated_cost_per_cell_usd: float | None = None,
        concurrency: int = 1,
    ) -> None:
        if not callable(getattr(executor, "measure", None)):
            raise TypeError("executor must provide measure()")
        if not callable(getattr(action_factory, "create", None)) or not callable(
            getattr(action_factory, "files", None)
        ):
            raise TypeError("action_factory must provide files() and create()")
        if not hasattr(request, "state"):
            raise TypeError("request must provide the frozen proposer state")
        if (getattr(runtime, "sampling", None) is None) == (
            getattr(runtime, "paired_plan", None) is None
        ):
            raise ValueError("runtime needs exactly one sampled or census admission design")
        private_root = Path(root).expanduser()
        if not private_root.is_absolute():
            raise ValueError("admission root must be absolute")
        if private_root.exists() and private_root.is_symlink():
            raise ValueError("admission root must not be a symlink")
        private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_root.chmod(0o700)
        if plan_factory is not None and not callable(plan_factory):
            raise TypeError("plan_factory must be callable")
        if artifact_resolver is not None and not callable(artifact_resolver):
            raise TypeError("artifact_resolver must be callable")
        if (
            estimated_cost_per_cell_usd is None
            or isinstance(estimated_cost_per_cell_usd, bool)
            or not isinstance(estimated_cost_per_cell_usd, (int, float))
            or not math.isfinite(float(estimated_cost_per_cell_usd))
            or float(estimated_cost_per_cell_usd) <= 0.0
        ):
            raise ValueError("estimated_cost_per_cell_usd must be finite and positive")
        if adherence_verifier is None:
            candidate = runtime.candidate
            adherence_verifier = StructuralImplementationAdherenceVerifier(
                SubprocessImmutableGitObjectReader(
                    ImmutableGitReaderConfig(
                        repository=runtime.program_repository,
                        git_executable=candidate.git_executable,
                        timeout_seconds=candidate.git_timeout_seconds,
                        max_output_bytes=candidate.max_git_output_bytes,
                        max_blob_bytes=min(candidate.max_file_bytes, candidate.max_git_output_bytes),
                    )
                )
            )
        if not callable(getattr(adherence_verifier, "verify", None)):
            raise TypeError("adherence_verifier must provide verify()")
        self.runtime = runtime
        self.request = request
        self.executor = executor
        self.root = private_root.resolve()
        self.action_factory = action_factory
        self.plan_factory = plan_factory or self._default_plan
        self.adherence_verifier = adherence_verifier
        self.artifact_resolver = artifact_resolver
        self.estimated_cost_per_cell_usd = float(estimated_cost_per_cell_usd)
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError('measurement concurrency must be positive')
        self.concurrency = concurrency

    def _folder(self, ticket_id: str) -> Path:
        identity = hashlib.sha256(ticket_id.encode("utf-8")).hexdigest()[:32]
        folder = self.root / identity
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        return folder

    def _action(
        self, ticket_id: str, candidate: Mapping[str, Any], control: Mapping[str, Any]
    ) -> PolicyAction:
        metadata = candidate["metadata"]
        if metadata.get("scope_validated") is False:
            raise AdmissionPreflightError("candidate scope validation failed")
        if metadata.get("diagnostics_passed") is False:
            raise AdmissionPreflightError("candidate smoke diagnostics failed")
        existing = set(self.action_factory.files(control["commit_sha"]))
        candidate_files = set(self.action_factory.files(candidate["commit_sha"]))
        new_files = tuple(sorted(candidate_files - existing))
        instruction = metadata.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            instruction = "Evaluate the supplied implemented research candidate."
        action = self.action_factory.create(
            ticket_id=ticket_id,
            base={**control, "id": control["artifact_id"]},
            instruction=instruction,
            # Empty means the factory expands the complete editable control scope.
            # This covers a cumulative C2-vs-P diff rather than trusting only the
            # most recent build ticket's changed_files metadata.
            files=(),
            new_files=new_files,
            research_evidence={"candidate_artifact": candidate["artifact_id"]},
        )
        if not isinstance(action, PolicyAction):
            raise TypeError("action_factory.create must return PolicyAction")
        state = self.request.state
        return replace(
            action,
            run_id=state.run_id,
            batch_id=state.batch_id,
            state_id=state.state_id,
        )

    def _default_plan(self, ticket_id: str, action: PolicyAction) -> FrozenEvaluationBatch:
        state = self.request.state
        sampling = self.runtime.sampling
        if sampling is not None:
            counts, weights = frozen_task_statistics(
                settings=sampling, observed_cell_results={}
            )
        else:
            plan = self.runtime.paired_plan
            task_ids = tuple(sorted({cell.block_id for cell in plan.cells}))
            counts, weights = {}, {task_id: 1.0 for task_id in task_ids}
        prior = FrozenInformationPriorSnapshot.create(
            run_id=state.run_id,
            batch_id=state.batch_id,
            state_id=state.state_id,
            cutoff=state.cutoff,
            compatibility=state.compatibility,
            source_scope_id=stable_id(
                "portfolio-admission-prior", self.runtime.scope_identity, ticket_id
            ),
            task_counts=counts,
            task_weights=weights,
            weighting_contract=WAVE_INFORMATION_WEIGHTING_CONTRACT,
        )
        record = (
            freeze_sampled_wave_plan(action=action, settings=sampling, prior_snapshot=prior)
            if sampling is not None
            else freeze_census_plan(
                action=action, plan=self.runtime.paired_plan, prior_snapshot=prior
            )
        )
        return FrozenEvaluationBatch(prior, (record,))

    def _validate_binding(
        self, candidate: Mapping[str, Any], control: Mapping[str, Any]
    ) -> None:
        if candidate["artifact_id"] == control["artifact_id"]:
            raise AdmissionPreflightError("candidate and control artifacts are identical")
        if candidate["commit_sha"] == control["commit_sha"]:
            raise AdmissionPreflightError("candidate and control commits are identical")
        if candidate["config"] not in (None, {}) or control["config"] not in (None, {}):
            raise AdmissionPreflightError(
                "the configured cell executor cannot apply artifact config overrides"
            )

    def _load_or_freeze(
        self,
        *,
        ticket_id: str,
        candidate: Mapping[str, Any],
        control: Mapping[str, Any],
        folder: Path,
    ) -> FrozenEvaluationBatch:
        binding = {"ticket_id": ticket_id, "candidate": candidate, "control": control}
        binding_path = folder / "binding.private.json"
        if binding_path.exists():
            previous = json.loads(binding_path.read_text(encoding="utf-8"))
            if previous != binding:
                raise AdmissionRecoveryRequired("admission ticket artifact binding changed")
        else:
            _atomic_json(binding_path, binding)

        frozen_path = folder / "frozen-plan.private.json"
        if frozen_path.exists():
            saved = _loads(frozen_path.read_text(encoding="utf-8"))
            if not isinstance(saved, Mapping):
                raise AdmissionRecoveryRequired("frozen admission plan is unreadable")
            try:
                batch = FrozenEvaluationBatch(
                    saved["prior_snapshot"], (saved["action_plan"],)
                )
            except (KeyError, TypeError, ValueError) as error:
                raise AdmissionRecoveryRequired(
                    "frozen admission plan is unreadable"
                ) from error
            return batch
        cells_root = folder / "cells"
        if cells_root.exists() and any(cells_root.iterdir()):
            raise AdmissionRecoveryRequired("admission cells exist without a frozen plan")

        action = self._action(ticket_id, candidate, control)
        adherence = self.adherence_verifier.verify(
            action,
            parent_commit_sha=control["commit_sha"],
            result_commit_sha=candidate["commit_sha"],
        )
        if getattr(adherence, "adherence", None) is not ImplementationAdherence.FOLLOWED:
            raise AdmissionPreflightError("candidate failed structural adherence")
        batch = self.plan_factory(ticket_id, action)
        if not isinstance(batch, FrozenEvaluationBatch) or len(batch.action_plans) != 1:
            raise TypeError("plan_factory must return one FrozenEvaluationBatch action plan")
        record = batch.action_plans[0]
        if record.action_id != action.action_id:
            raise ValueError("frozen admission plan belongs to another action")
        _atomic_json(
            folder / "preflight.private.json",
            {
                "ticket_id": ticket_id,
                "action_id": action.action_id,
                "adherence": adherence.as_worker_diagnostics(),
                "prior_task_counts": len(batch.prior_snapshot.task_counts),
            },
        )
        _atomic_typed(frozen_path, batch)
        return batch

    @staticmethod
    def _estimator(record: FrozenActionEvaluationPlan) -> object:
        if record.estimator_contract == HT_ESTIMATOR_CONTRACT:
            if record.sampling_draw is None:
                raise ValueError("HT admission plan lacks its frozen sampling draw")
            return HorvitzThompsonPairedEstimator(record.sampling_draw)
        if record.estimator_contract == UNIFORM_ESTIMATOR_CONTRACT:
            if record.sampling_draw is not None:
                raise ValueError("uniform admission plan carries a sampling draw")
            return UniformPairedEstimator()
        raise ValueError("admission plan uses an unsupported estimator contract")

    def estimate(self, arguments: Mapping[str, Any]) -> float:
        _snapshot(arguments, "admission arguments")
        if self.runtime.sampling is not None:
            cells = self.runtime.sampling.size * self.runtime.sampling.replicates
        else:
            cells = len(self.runtime.paired_plan.cells)
        return 2.0 * cells * self.estimated_cost_per_cell_usd

    def admit(
        self,
        *,
        ticket_id: str,
        candidate: Mapping[str, Any],
        control: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ticket_id = _text(ticket_id, "ticket_id")
        candidate = _artifact(candidate, "candidate")
        control = _artifact(control, "control")
        if self.artifact_resolver is not None:
            for supplied, name in ((candidate, "candidate"), (control, "control")):
                resolved = _artifact(
                    self.artifact_resolver(supplied["artifact_id"]), name
                )
                if resolved != supplied:
                    raise AdmissionRecoveryRequired(
                        f"{name} artifact changed after ticket preparation"
                    )
        folder = self._folder(ticket_id)
        result_path = folder / "result.public.json"
        try:
            self._validate_binding(candidate, control)
            batch = self._load_or_freeze(
                ticket_id=ticket_id, candidate=candidate, control=control, folder=folder
            )
        except AdmissionPreflightError as error:
            result = {
                "status": "failed",
                "cost_usd": 0.0,
                "error": str(error),
                "summary": "Candidate failed local admission checks before restricted-B execution.",
            }
            _atomic_json(result_path, result)
            return result
        except AdmissionRecoveryRequired:
            raise
        except Exception as error:
            _atomic_json(
                folder / "plan-error.private.json",
                {"exception_type": type(error).__name__, "message": str(error)},
            )
            raise AdmissionExecutionError(
                "restricted-B plan could not be prepared; inspect the private admission journal"
            ) from error
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise AdmissionRecoveryRequired("public admission result is unreadable")
            return result

        record = batch.action_plans[0]
        executor = _JournaledCellExecutor(
            self.executor, ticket_id, folder / "cells", record.plan
        )
        try:
            summary = PairedEvaluationEngine(
                executor, self._estimator(record), measurement_concurrency=self.concurrency,
                invalid_cell_retries=0,
            ).evaluate(
                control_program_id=control["commit_sha"],
                challenger_program_id=candidate["commit_sha"],
                plan=record.plan,
                metric_direction=self.runtime.metric_direction,
            )
        except AdmissionRecoveryRequired:
            raise
        except Exception as error:
            _atomic_json(
                folder / "execution-error.private.json",
                {"exception_type": type(error).__name__, "message": str(error)},
            )
            raise AdmissionExecutionError(
                "restricted-B execution did not settle; inspect the private admission journal"
            ) from error
        constraints = summary.constraints_pass
        resource_regression = None
        if self.runtime.diagnostic_rule is not None and constraints is not False:
            resource_regression = self.runtime.diagnostic_rule.rate_increase(summary.measurements)
            if resource_regression is None:
                constraints = None
            elif resource_regression > self.runtime.diagnostic_rule.max_rate_increase:
                constraints = False
        estimate = summary.estimate
        evidence = PairedEvidence(
            estimate=None if estimate is None else estimate.primary_delta_mean,
            standard_error=(
                None if estimate is None else estimate.primary_delta_standard_error
            ),
            effective_sample_size=(
                None if estimate is None else estimate.effective_sample_size
            ),
            measured_blocks=summary.measured_blocks,
            expected_blocks=summary.expected_blocks,
            constraints_pass=constraints,
        )
        verdict = decide_admission(evidence, self.runtime.admission_rule)
        costs = [
            _measurement_cost(measurement)
            for pair in summary.measurements
            for measurement in pair
        ]
        cost = None if any(item is None for item in costs) else sum(
            float(item) for item in costs if item is not None
        )
        result = {
            "cost_usd": cost,
            "summary": "Restricted-B paired admission completed.",
            "admission": verdict.fate.value,
            "admission_fate": verdict.fate.value,
            "accepted": verdict.fate is AdmissionFate.ADMITTED,
            "evidence_tier": "adaptive_b",
            "estimate": None if estimate is None else {
                "primary_delta_mean": estimate.primary_delta_mean,
                "primary_delta_standard_error": estimate.primary_delta_standard_error,
                "effective_sample_size": estimate.effective_sample_size,
                "challenger_absolute_value": estimate.challenger_absolute_value,
            },
            "coverage": {
                "measured_blocks": summary.measured_blocks,
                "expected_blocks": summary.expected_blocks,
                "complete": summary.measured_blocks == summary.expected_blocks,
            },
            "constraints_pass": constraints,
            "conservative_lower": verdict.conservative_lower,
            "reason": verdict.reason,
            "resource_regression": resource_regression,
            "policy_evidence": {
                "control_commit": control["commit_sha"],
                "candidate_commit": candidate["commit_sha"],
                "implementation_adherence": "followed",
                "evaluation_validity": "valid" if (
                    summary.measured_blocks == summary.expected_blocks and estimate is not None
                    and constraints is not None
                    and len(summary.measurements) == len(record.plan.cells)
                    and all(measurement.status is MeasurementStatus.VALID
                            for pair in summary.measurements for measurement in pair)
                ) else "inconclusive",
            },
        }
        _atomic_json(result_path, result)
        return result

    def recover(self, ticket: object) -> Mapping[str, Any]:
        arguments = ticket.get("arguments") if isinstance(ticket, Mapping) else getattr(
            ticket, "arguments", None
        )
        ticket_id = ticket.get("id") if isinstance(ticket, Mapping) else getattr(
            ticket, "id", None
        )
        if not isinstance(arguments, Mapping):
            raise TypeError("admission recovery ticket needs arguments")
        return self.admit(
            ticket_id=ticket_id,
            candidate=arguments["candidate"],
            control=arguments["control"],
        )


__all__ = [
    "AdmissionPlanFactory",
    "AdmissionExecutionError",
    "AdmissionPreflightError",
    "AdmissionRecoveryRequired",
    "FrozenBAdmissionEvaluator",
    "PortfolioAdmissionEvaluator",
]


PortfolioAdmissionEvaluator = FrozenBAdmissionEvaluator
