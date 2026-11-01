"""Benchmark-neutral parent/child paired evaluation orchestration."""

from __future__ import annotations

import math
import statistics
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from threading import Lock
from typing import Any, Callable, Mapping, Protocol, Sequence

from agentpilot.domain.common import deep_freeze, require_text, stable_id, utc_now
from agentpilot.domain.evaluation import (
    ChildProgramReadout,
    EvaluationValidity,
    EvidenceTier,
    OutcomeAttribution,
    PairedOutcome,
)
from agentpilot.domain.search import StateDescriptor
from agentpilot.evaluation.admission import (
    AdmissionFate,
    AdmissionRule,
    PairedEvidence,
    decide_admission,
)
from agentpilot.evaluation.sealed.contracts import (
    SealedAuthorization,
    SealedConflict,
    SealedCostSlot,
    SealedEvaluationClaim,
    SealedSlotSettlement,
    SealedSlotSide,
    SealedSlotState,
    require_keyed_commitment,
)
from agentpilot.evaluation.sealed.repository import SealedRepository
from agentpilot.evaluation.sealed.service import (
    SealedHoldoutController,
    record_sealed_evaluation,
    require_persisted_sealed_authorization,
)


class MeasurementStatus(str, Enum):
    VALID = "valid"
    UNMEASURED = "unmeasured"
    APPARATUS_FAILURE = "apparatus_failure"


@dataclass(frozen=True)
class EvaluationCell:
    """Opaque task coordinate shared by both sides of one comparison."""

    cell_id: str
    task_id: str
    block_id: str
    # Names the replicate; does not seed it. Whether a benchmark can make a
    # measurement reproducible is the benchmark's problem, and an agentic
    # benchmark generally cannot, so a recorded seed says nothing about how much
    # of a paired difference is noise.
    measurement_seed: int
    replicate: int = 0

    def __post_init__(self) -> None:
        for name in ("cell_id", "task_id", "block_id"):
            require_text(getattr(self, name), name)
        if (
            isinstance(self.measurement_seed, bool)
            or not isinstance(self.measurement_seed, int)
            or self.measurement_seed < 0
        ):
            raise ValueError("measurement_seed must be non-negative")
        if (
            isinstance(self.replicate, bool)
            or not isinstance(self.replicate, int)
            or self.replicate < 0
        ):
            raise ValueError("replicate must be non-negative")


@dataclass(frozen=True)
class FrozenPairedPlan:
    """A complete, immutable cell schedule chosen before either side runs."""

    plan_id: str
    tier: EvidenceTier
    cells: tuple[EvaluationCell, ...]
    target_population_size: int
    design_identity: Mapping[str, Any] = field(default_factory=dict)
    plan_commitment_id: str | None = None

    def __post_init__(self) -> None:
        require_text(self.plan_id, "plan_id")
        if not isinstance(self.tier, EvidenceTier):
            raise TypeError("tier must be an EvidenceTier")
        if self.tier is EvidenceTier.REALIZATION:
            raise ValueError("realization feedback cannot be a paired plan")
        object.__setattr__(self, "cells", tuple(self.cells))
        if not self.cells:
            raise ValueError("paired plan needs at least one cell")
        if any(not isinstance(cell, EvaluationCell) for cell in self.cells):
            raise TypeError("paired plan cells must be EvaluationCell values")
        identities = [cell.cell_id for cell in self.cells]
        if len(identities) != len(set(identities)):
            raise ValueError("paired plan contains duplicate cell ids")
        if (
            isinstance(self.target_population_size, bool)
            or not isinstance(self.target_population_size, int)
            or self.target_population_size < len({c.block_id for c in self.cells})
        ):
            raise ValueError("target population cannot be smaller than the sample")
        object.__setattr__(self, "design_identity", deep_freeze(self.design_identity))
        if self.tier is EvidenceTier.SEALED_C:
            if self.plan_commitment_id is None:
                raise ValueError("sealed C plan needs an external keyed commitment")
            require_keyed_commitment(self.plan_commitment_id, name="plan_commitment_id")
        else:
            if self.plan_commitment_id is not None:
                raise ValueError("external plan commitment is valid only for sealed C")


@dataclass(frozen=True)
class CellMeasurement:
    cell_id: str
    program_id: str
    status: MeasurementStatus
    primary_value: float | None
    constraints_pass: bool | None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_text(self.cell_id, "cell_id")
        require_text(self.program_id, "program_id")
        if not isinstance(self.status, MeasurementStatus):
            raise TypeError("status must be a MeasurementStatus")
        if self.constraints_pass is not None and not isinstance(
            self.constraints_pass, bool
        ):
            raise TypeError("constraints_pass must be bool or None")
        if self.status is MeasurementStatus.VALID:
            if self.primary_value is None or not math.isfinite(self.primary_value):
                raise ValueError("valid measurement needs a finite primary value")
        elif self.primary_value is not None:
            raise ValueError("unmeasured cells cannot carry a primary value")
        object.__setattr__(self, "diagnostics", deep_freeze(self.diagnostics))


class CellExecutor(Protocol):
    """Implemented by a benchmark package holding private task material."""

    def measure(self, *, program_id: str, cell: EvaluationCell) -> CellMeasurement: ...


class CancellableCellExecutor(Protocol):
    """A cell executor that can hand out an independently stoppable view.

    A public measurement is read-only, so abandoning one is always safe, but it
    is only cheap when the apparatus behind it can be told to stop. Stopping
    must be scoped to the caller that started the work: two actions in one
    batch measure the same parent program at the same time, so a runtime-wide
    stop would end a sibling's measurements too.
    """

    def cancellable(self) -> tuple[CellExecutor, Callable[[], None]]: ...


def _cancellable_view(
    executor: CellExecutor,
) -> tuple[CellExecutor, Callable[[], None]]:
    """Scope an executor for one caller, or fall back to joining its work."""

    factory = getattr(executor, "cancellable", None)
    if not callable(factory):
        return executor, lambda: None
    scoped, abort = factory()
    if not callable(getattr(scoped, "measure", None)) or not callable(abort):
        raise TypeError("cancellable() must return a cell executor and an abort call")
    return scoped, abort


@dataclass(frozen=True)
class SealedMeasurementEnvelope:
    """Opaque private-store receipt returned by the external C adapter."""

    execution_id: str
    result_identity: str
    actual_cost_microusd: int
    measurement: CellMeasurement

    def __post_init__(self) -> None:
        require_text(self.execution_id, "execution_id")
        require_text(self.result_identity, "result_identity")
        if (
            isinstance(self.actual_cost_microusd, bool)
            or not isinstance(self.actual_cost_microusd, int)
            or self.actual_cost_microusd < 0
        ):
            raise ValueError("actual_cost_microusd must be a non-negative integer")
        if not isinstance(self.measurement, CellMeasurement):
            raise TypeError("measurement must be a CellMeasurement")
        require_keyed_commitment(self.result_identity, name="result_identity")


class SealedCellExecutor(Protocol):
    """External private adapter with stable execute/recover semantics."""

    def execute_or_recover_sealed(
        self,
        *,
        program_id: str,
        cell: EvaluationCell,
        execution_id: str,
        max_cost_microusd: int,
    ) -> SealedMeasurementEnvelope: ...


@dataclass(frozen=True)
class PairedEstimate:
    primary_delta_mean: float
    primary_delta_standard_error: float | None
    effective_sample_size: float
    challenger_absolute_value: float
    estimator_identity: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in (
            "primary_delta_mean",
            "effective_sample_size",
            "challenger_absolute_value",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, value)
        if self.primary_delta_standard_error is not None:
            standard_error = float(self.primary_delta_standard_error)
            if not math.isfinite(standard_error) or standard_error < 0.0:
                raise ValueError(
                    "primary_delta_standard_error must be finite and non-negative"
                )
            object.__setattr__(self, "primary_delta_standard_error", standard_error)
        if self.effective_sample_size <= 0.0:
            raise ValueError("effective_sample_size must be positive")
        object.__setattr__(
            self, "estimator_identity", deep_freeze(self.estimator_identity)
        )


class PairedEstimator(Protocol):
    def estimate(
        self,
        *,
        block_deltas: Mapping[str, float],
        challenger_by_block: Mapping[str, float],
        plan: FrozenPairedPlan,
    ) -> PairedEstimate: ...


class UniformPairedEstimator:
    """Reference census/uniform estimator for complete paired blocks."""

    supports_replicate_variance = True

    def estimate(
        self,
        *,
        block_deltas: Mapping[str, float],
        challenger_by_block: Mapping[str, float],
        plan: FrozenPairedPlan,
        block_delta_variances: Mapping[str, float | None] | None = None,
    ) -> PairedEstimate:
        if set(block_deltas) != set(challenger_by_block):
            raise ValueError("delta and challenger blocks differ")
        plan_blocks = {cell.block_id for cell in plan.cells}
        if set(block_deltas) != plan_blocks:
            raise ValueError("uniform estimator inputs differ from paired plan blocks")
        if plan.target_population_size != len(plan_blocks):
            raise ValueError(
                "uniform estimator is census-only; sampled plans require HT"
            )
        values = list(block_deltas.values())
        if not values:
            raise ValueError("uniform estimator needs measured blocks")
        within = validated_block_delta_variances(block_deltas, block_delta_variances)
        known = [value for value in within.values() if value is not None]
        within_floor = sum(known) / len(values) ** 2 if known else None
        between_floor = (
            statistics.variance(values) / len(values) if len(values) > 1 else None
        )
        floors = [value for value in (between_floor, within_floor) if value is not None]
        standard_error = math.sqrt(max(floors)) if floors else None
        return PairedEstimate(
            primary_delta_mean=statistics.fmean(values),
            primary_delta_standard_error=standard_error,
            effective_sample_size=float(len(values)),
            challenger_absolute_value=statistics.fmean(challenger_by_block.values()),
            estimator_identity={
                "name": "uniform-complete-paired-replicates-v2",
                "between_variance_floor": between_floor,
                "within_variance_floor": within_floor,
                "within_variance_known_blocks": len(known),
                "within_variance_unknown_blocks": len(values) - len(known),
                "uncertainty_status": "unavailable"
                if standard_error is None
                else "replicate_and_between_floors"
                if len(known) == len(values)
                else "between_proxy_with_incomplete_replicates",
                "replicate_assumption": "independent_replicate_pairs",
                "design": dict(plan.design_identity),
            },
        )


def validated_block_delta_variances(
    block_deltas: Mapping[str, float],
    variances: Mapping[str, float | None] | None,
) -> dict[str, float | None]:
    """Validate variances of block means; an absent repeat is not zero noise."""
    if variances is None:
        return dict.fromkeys(block_deltas)
    if set(variances) != set(block_deltas):
        raise ValueError("within-block variances differ from measured blocks")
    result: dict[str, float | None] = {}
    for block, variance in variances.items():
        if variance is not None and (
            isinstance(variance, bool)
            or not isinstance(variance, (int, float))
            or not math.isfinite(variance)
            or variance < 0
        ):
            raise ValueError(
                "within-block mean variance must be finite and non-negative"
            )
        result[block] = None if variance is None else float(variance)
    return result


@dataclass(frozen=True)
class PairedEvaluationSummary:
    plan_id: str
    tier: EvidenceTier
    control_program_id: str
    challenger_program_id: str
    estimate: PairedEstimate | None
    constraints_pass: bool | None
    measured_blocks: int
    expected_blocks: int
    missing_cell_ids: tuple[str, ...]
    cell_blocks: Mapping[str, str]
    measurements: tuple[tuple[CellMeasurement, CellMeasurement], ...]

    def __post_init__(self) -> None:
        require_text(self.plan_id, "plan_id")
        require_text(self.control_program_id, "control_program_id")
        require_text(self.challenger_program_id, "challenger_program_id")
        if self.control_program_id == self.challenger_program_id:
            raise ValueError("paired summary needs two distinct programs")
        if not isinstance(self.tier, EvidenceTier):
            raise TypeError("tier must be an EvidenceTier")
        if self.estimate is not None and not isinstance(self.estimate, PairedEstimate):
            raise TypeError("estimate must be a PairedEstimate or None")
        if self.constraints_pass is not None and not isinstance(
            self.constraints_pass, bool
        ):
            raise TypeError("constraints_pass must be bool or None")
        for name in ("measured_blocks", "expected_blocks"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.expected_blocks < 1:
            raise ValueError("expected_blocks must be positive")
        if self.measured_blocks > self.expected_blocks:
            raise ValueError("measured_blocks cannot exceed expected_blocks")

        measurements = tuple(self.measurements)
        if not measurements:
            raise ValueError("paired summary needs measurements")
        cell_ids: list[str] = []
        derived_missing: list[str] = []
        valid_challenger_constraints: list[bool | None] = []
        for pair in measurements:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise TypeError("measurements must contain control/challenger pairs")
            control, challenger = pair
            if not isinstance(control, CellMeasurement) or not isinstance(
                challenger, CellMeasurement
            ):
                raise TypeError("measurement pair values must be CellMeasurement")
            if control.cell_id != challenger.cell_id:
                raise ValueError("measurement pair references different cells")
            if control.program_id != self.control_program_id:
                raise ValueError("control measurement references another program")
            if challenger.program_id != self.challenger_program_id:
                raise ValueError("challenger measurement references another program")
            cell_ids.append(control.cell_id)
            if challenger.status is MeasurementStatus.VALID:
                valid_challenger_constraints.append(challenger.constraints_pass)
            if (
                control.status is not MeasurementStatus.VALID
                or challenger.status is not MeasurementStatus.VALID
            ):
                derived_missing.append(control.cell_id)
        if len(cell_ids) != len(set(cell_ids)):
            raise ValueError("paired summary contains duplicate cell ids")
        if not isinstance(self.cell_blocks, Mapping):
            raise TypeError("cell_blocks must be a mapping")
        cell_blocks = {
            require_text(cell_id, "cell block cell id"): require_text(
                block_id, "cell block id"
            )
            for cell_id, block_id in self.cell_blocks.items()
        }
        if set(cell_blocks) != set(cell_ids):
            raise ValueError("cell_blocks differ from measured cell ids")
        derived_expected_blocks = len(set(cell_blocks.values()))
        if self.expected_blocks != derived_expected_blocks:
            raise ValueError("expected_blocks differ from cell_blocks")
        missing = tuple(self.missing_cell_ids)
        if missing != tuple(derived_missing):
            raise ValueError("missing_cell_ids differ from non-valid measurements")
        derived_missing_blocks = {cell_blocks[cell_id] for cell_id in missing}
        if self.measured_blocks != self.expected_blocks - len(derived_missing_blocks):
            raise ValueError("measured_blocks differ from complete paired blocks")
        derived_constraints = PairedEvaluationEngine._constraints(
            valid_challenger_constraints
        )
        if self.constraints_pass is not derived_constraints:
            raise ValueError("constraints_pass differs from challenger measurements")
        if missing:
            if self.estimate is not None:
                raise ValueError("incomplete paired summary cannot carry an estimate")
            if self.measured_blocks >= self.expected_blocks:
                raise ValueError("incomplete summary cannot report full block coverage")
        else:
            if self.estimate is None:
                raise ValueError("complete paired summary needs an estimate")
            if self.measured_blocks != self.expected_blocks:
                raise ValueError("complete summary must report full block coverage")
        object.__setattr__(self, "measurements", measurements)
        object.__setattr__(self, "missing_cell_ids", missing)
        object.__setattr__(self, "cell_blocks", deep_freeze(cell_blocks))

    @property
    def coverage_complete(self) -> bool:
        return not self.missing_cell_ids


class ControlPrefetchHandle:
    """One-shot owner of public control measurements and their worker pool.

    The handle deliberately owns the pool rather than only the control futures.
    A later paired evaluation can therefore submit challenger cells to the same
    bounded executor.  Callers must close an unused handle; consuming it through
    :meth:`PairedEvaluationEngine.evaluate` closes it automatically.
    """

    def __init__(
        self,
        *,
        engine: PairedEvaluationEngine,
        control_program_id: str,
        plan: FrozenPairedPlan,
    ) -> None:
        self._engine = engine
        self.control_program_id = control_program_id
        self.plan = plan
        self._lock = Lock()
        self._closed = False
        self._consumed = False
        self._executor, self._abort = _cancellable_view(engine.executor)
        self._challenger_futures: dict[int, Future[CellMeasurement]] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=engine.measurement_concurrency,
            thread_name_prefix="paired-prefetch",
        )
        try:
            self._control_futures: dict[int, Future[CellMeasurement]] = {
                ordinal: self._pool.submit(
                    engine._measure_public,
                    program_id=control_program_id,
                    cell=cell,
                    executor=self._executor,
                )
                for ordinal, cell in enumerate(plan.cells)
            }
        except BaseException:
            self._abort()
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._closed = True
            raise

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _submit_challenger(
        self,
        *,
        engine: PairedEvaluationEngine,
        control_program_id: str,
        challenger_program_id: str,
        plan: FrozenPairedPlan,
    ) -> tuple[
        dict[int, Future[CellMeasurement]],
        dict[int, Future[CellMeasurement]],
    ]:
        with self._lock:
            if self._closed:
                raise RuntimeError("control prefetch handle is closed")
            if self._consumed:
                raise RuntimeError("control prefetch handle was already consumed")
            if engine is not self._engine:
                raise ValueError("control prefetch belongs to another evaluator")
            if control_program_id != self.control_program_id:
                raise ValueError("control prefetch belongs to another control program")
            if plan != self.plan:
                raise ValueError("control prefetch belongs to another frozen plan")
            self._consumed = True
            challenger_futures = {
                ordinal: self._pool.submit(
                    engine._measure_public,
                    program_id=challenger_program_id,
                    cell=cell,
                    executor=self._executor,
                )
                for ordinal, cell in enumerate(plan.cells)
            }
            self._challenger_futures = dict(challenger_futures)
            return dict(self._control_futures), challenger_futures

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            outstanding = any(
                not future.done()
                for future in (
                    *self._control_futures.values(),
                    *self._challenger_futures.values(),
                )
            )
        # Ask the apparatus to stop before joining. Cancelling futures only
        # reaches work still queued, and a pool sized for the plan starts every
        # cell at once, so an abandoned sweep would otherwise be joined for a
        # full cell timeout to produce readings nobody reads. The join still
        # happens, so no executor thread or external measurement outlives close.
        # None of this holds the state lock: it joins user-supplied code.
        if outstanding:
            self._abort()
        self._pool.shutdown(wait=True, cancel_futures=True)

    def __enter__(self) -> ControlPrefetchHandle:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class PairedEvaluationEngine:
    """Measure the same frozen cells on both sides and estimate only if complete.

    Complete-case deletion is intentionally forbidden.  If either side of a
    scheduled cell is missing, the engine returns typed incomplete evidence
    and does not let the estimator silently change the target population.

    ``measurement_concurrency`` overlaps public measurements only.  Sealed C
    stays strictly serial because its claim is threaded through one slot
    settlement at a time.

    ``invalid_cell_retries`` bounds how often a public cell is re-read after the
    apparatus fails.  One unreadable cell otherwise voids the whole comparison,
    because a summary with any missing cell carries no estimate.  Sealed C never
    retries: its exactly-once slots make a second reading unrepresentable.
    """

    def __init__(
        self,
        executor: CellExecutor,
        estimator: PairedEstimator,
        *,
        clock: Callable[[], datetime] | None = None,
        measurement_concurrency: int = 1,
        invalid_cell_retries: int = 0,
    ):
        if not callable(getattr(executor, "measure", None)) and not (
            callable(getattr(executor, "execute_or_recover_sealed", None))
        ):
            raise TypeError(
                "executor must provide measure() or execute_or_recover_sealed()"
            )
        if not callable(getattr(estimator, "estimate", None)):
            raise TypeError("estimator must provide estimate()")
        if (
            isinstance(measurement_concurrency, bool)
            or not isinstance(measurement_concurrency, int)
            or measurement_concurrency < 1
        ):
            raise ValueError("measurement_concurrency must be a positive integer")
        if (
            isinstance(invalid_cell_retries, bool)
            or not isinstance(invalid_cell_retries, int)
            or invalid_cell_retries < 0
        ):
            raise ValueError("invalid_cell_retries must be a non-negative integer")
        self.executor = executor
        self.estimator = estimator
        self.clock = clock or utc_now
        self.measurement_concurrency = measurement_concurrency
        self.invalid_cell_retries = invalid_cell_retries

    def start_control_prefetch(
        self, control_program_id: str, plan: FrozenPairedPlan
    ) -> ControlPrefetchHandle:
        """Start all public control cells before a challenger is available."""

        require_text(control_program_id, "control_program_id")
        if not isinstance(plan, FrozenPairedPlan):
            raise TypeError("plan must be a FrozenPairedPlan")
        if plan.tier is EvidenceTier.SEALED_C:
            raise ValueError("sealed C does not permit control prefetch")
        return ControlPrefetchHandle(
            engine=self,
            control_program_id=control_program_id,
            plan=plan,
        )

    def _measure_public(
        self,
        *,
        program_id: str,
        cell: EvaluationCell,
        executor: CellExecutor | None = None,
    ) -> CellMeasurement:
        """Read one public cell, re-reading only a failed apparatus.

        A retry repairs a missing reading; it never replaces one.  Only a
        non-valid status is re-read, so no measurement can be discarded for its
        value, and the cell's frozen measurement seed keeps every attempt the
        same experiment rather than a fresh draw.

        ``executor`` lets a prefetch read through its own stoppable view of the
        same apparatus without changing what is measured.
        """

        target = self.executor if executor is None else executor
        measurement = target.measure(program_id=program_id, cell=cell)
        for _ in range(self.invalid_cell_retries):
            if measurement.status is MeasurementStatus.VALID:
                break
            measurement = target.measure(program_id=program_id, cell=cell)
        return measurement

    def _measure_concurrently(
        self,
        *,
        control_program_id: str,
        challenger_program_id: str,
        plan: FrozenPairedPlan,
    ) -> dict[int, tuple[CellMeasurement, CellMeasurement]]:
        """Overlap public measurements and return them in plan order.

        A measurement depends only on its program id and its frozen cell, so
        overlapping cells cannot change any result.  Results are collected in
        plan order, which also means a partially failed sweep raises the same
        error it would have raised serially.
        """

        prefetch = self.start_control_prefetch(control_program_id, plan)
        try:
            return self._measure_with_prefetched_control(
                control_program_id=control_program_id,
                challenger_program_id=challenger_program_id,
                plan=plan,
                prefetched_control=prefetch,
            )
        finally:
            prefetch.close()

    def _measure_with_prefetched_control(
        self,
        *,
        control_program_id: str,
        challenger_program_id: str,
        plan: FrozenPairedPlan,
        prefetched_control: ControlPrefetchHandle,
    ) -> dict[int, tuple[CellMeasurement, CellMeasurement]]:
        controls, challengers = prefetched_control._submit_challenger(
            engine=self,
            control_program_id=control_program_id,
            challenger_program_id=challenger_program_id,
            plan=plan,
        )
        collected: dict[int, tuple[CellMeasurement, CellMeasurement]] = {}
        for ordinal in range(len(plan.cells)):
            # Resolve in plan order so failures retain the serial API's stable
            # error boundary. The shared executor still runs all submitted work.
            collected[ordinal] = (
                controls[ordinal].result(),
                challengers[ordinal].result(),
            )
        return {ordinal: collected[ordinal] for ordinal in range(len(plan.cells))}

    def evaluate(
        self,
        *,
        control_program_id: str,
        challenger_program_id: str,
        plan: FrozenPairedPlan,
        metric_direction: str,
        sealed_authorization: SealedAuthorization | None = None,
        sealed_repository: SealedRepository | None = None,
        sealed_owner_token: str | None = None,
        sealed_claim_ttl: timedelta | None = None,
        sealed_max_cost_per_measurement_usd: float | None = None,
        prefetched_control: ControlPrefetchHandle | None = None,
    ) -> PairedEvaluationSummary:
        if prefetched_control is not None and not isinstance(
            prefetched_control, ControlPrefetchHandle
        ):
            raise TypeError("prefetched_control must be a ControlPrefetchHandle")
        try:
            if (
                prefetched_control is not None
                and isinstance(plan, FrozenPairedPlan)
                and plan.tier is EvidenceTier.SEALED_C
            ):
                # Reject before opening or renewing any private-store claim.
                raise ValueError("sealed C does not permit control prefetch")
            return self._evaluate(
                control_program_id=control_program_id,
                challenger_program_id=challenger_program_id,
                plan=plan,
                metric_direction=metric_direction,
                sealed_authorization=sealed_authorization,
                sealed_repository=sealed_repository,
                sealed_owner_token=sealed_owner_token,
                sealed_claim_ttl=sealed_claim_ttl,
                sealed_max_cost_per_measurement_usd=(
                    sealed_max_cost_per_measurement_usd
                ),
                prefetched_control=prefetched_control,
            )
        finally:
            if prefetched_control is not None:
                prefetched_control.close()

    def _evaluate(
        self,
        *,
        control_program_id: str,
        challenger_program_id: str,
        plan: FrozenPairedPlan,
        metric_direction: str,
        sealed_authorization: SealedAuthorization | None,
        sealed_repository: SealedRepository | None,
        sealed_owner_token: str | None,
        sealed_claim_ttl: timedelta | None,
        sealed_max_cost_per_measurement_usd: float | None,
        prefetched_control: ControlPrefetchHandle | None,
    ) -> PairedEvaluationSummary:
        require_text(control_program_id, "control_program_id")
        require_text(challenger_program_id, "challenger_program_id")
        if control_program_id == challenger_program_id:
            raise ValueError("paired evaluation needs two distinct programs")
        if metric_direction not in {"maximize", "minimize"}:
            raise ValueError("metric_direction must be maximize or minimize")
        expected_blocks = len({cell.block_id for cell in plan.cells})
        sealed_claim: SealedEvaluationClaim | None = None
        sealed_slots: dict[tuple[int, SealedSlotSide], SealedCostSlot] = {}
        if plan.tier is EvidenceTier.SEALED_C:
            assert plan.plan_commitment_id is not None
            if not isinstance(sealed_authorization, SealedAuthorization):
                raise ValueError("sealed C needs a typed sealed authorization")
            if sealed_repository is None:
                raise ValueError("sealed C needs its durable authorization repository")
            if plan.target_population_size != expected_blocks:
                raise ValueError(
                    "sealed C plan must schedule its full target population"
                )
            if sealed_owner_token is None:
                raise ValueError("sealed C needs an evaluation owner token")
            if sealed_claim_ttl is None:
                raise ValueError("sealed C needs an evaluation claim TTL")
            if sealed_max_cost_per_measurement_usd is None:
                raise ValueError("sealed C needs a per-measurement cost cap")
            require_persisted_sealed_authorization(
                sealed_repository,
                sealed_authorization,
                plan_id=plan.plan_id,
                plan_digest=plan.plan_commitment_id,
                expected_blocks=expected_blocks,
                expected_measurements_per_side=len(plan.cells),
                root_program_id=control_program_id,
                champion_program_id=challenger_program_id,
            )
            record = SealedHoldoutController(sealed_repository).begin_evaluation(
                run_id=sealed_authorization.run_id,
                max_cost_per_measurement_usd=(sealed_max_cost_per_measurement_usd),
                owner_token=sealed_owner_token,
                now=self.clock(),
                ttl=sealed_claim_ttl,
            )
            sealed_claim = record.evaluation_claim
            if sealed_claim is None:
                raise ValueError("sealed repository did not persist a claim")
            sealed_slots = {
                (slot.ordinal, slot.side): slot for slot in record.cost_slots
            }
            require_persisted_sealed_authorization(
                sealed_repository,
                sealed_authorization,
                plan_id=plan.plan_id,
                plan_digest=plan.plan_commitment_id,
                expected_blocks=expected_blocks,
                expected_measurements_per_side=len(plan.cells),
                root_program_id=control_program_id,
                champion_program_id=challenger_program_id,
            )
        elif any(
            value is not None
            for value in (
                sealed_authorization,
                sealed_repository,
                sealed_owner_token,
                sealed_claim_ttl,
                sealed_max_cost_per_measurement_usd,
            )
        ):
            raise ValueError("sealed authorization is valid only for sealed C")

        measurements: list[tuple[CellMeasurement, CellMeasurement]] = []
        missing: list[str] = []
        missing_blocks: set[str] = set()
        control_by_block: dict[str, list[float]] = {}
        challenger_by_block: dict[str, list[float]] = {}
        challenger_constraints: list[bool | None] = []
        prefetched: dict[int, tuple[CellMeasurement, CellMeasurement]] | None = None
        if prefetched_control is not None:
            if plan.tier is EvidenceTier.SEALED_C:
                raise ValueError("sealed C does not permit control prefetch")
            prefetched = self._measure_with_prefetched_control(
                control_program_id=control_program_id,
                challenger_program_id=challenger_program_id,
                plan=plan,
                prefetched_control=prefetched_control,
            )
        elif (
            plan.tier is not EvidenceTier.SEALED_C and self.measurement_concurrency > 1
        ):
            prefetched = self._measure_concurrently(
                control_program_id=control_program_id,
                challenger_program_id=challenger_program_id,
                plan=plan,
            )
        for ordinal, cell in enumerate(plan.cells):
            if plan.tier is EvidenceTier.SEALED_C:
                assert sealed_repository is not None
                assert sealed_claim is not None
                control, sealed_claim = self._measure_sealed(
                    repository=sealed_repository,
                    claim=sealed_claim,
                    slot=sealed_slots[(ordinal, SealedSlotSide.CONTROL)],
                    program_id=control_program_id,
                    cell=cell,
                    claim_ttl=sealed_claim_ttl,
                )
                challenger, sealed_claim = self._measure_sealed(
                    repository=sealed_repository,
                    claim=sealed_claim,
                    slot=sealed_slots[(ordinal, SealedSlotSide.CHALLENGER)],
                    program_id=challenger_program_id,
                    cell=cell,
                    claim_ttl=sealed_claim_ttl,
                )
            elif prefetched is not None:
                control, challenger = prefetched[ordinal]
            else:
                control = self._measure_public(program_id=control_program_id, cell=cell)
                challenger = self._measure_public(
                    program_id=challenger_program_id, cell=cell
                )
            self._validate_measurement(control, control_program_id, cell)
            self._validate_measurement(challenger, challenger_program_id, cell)
            measurements.append((control, challenger))
            if challenger.status is MeasurementStatus.VALID:
                challenger_constraints.append(challenger.constraints_pass)
            if (
                control.status is not MeasurementStatus.VALID
                or challenger.status is not MeasurementStatus.VALID
            ):
                missing.append(cell.cell_id)
                missing_blocks.add(cell.block_id)
                continue
            control_by_block.setdefault(cell.block_id, []).append(
                float(control.primary_value)
            )
            challenger_by_block.setdefault(cell.block_id, []).append(
                float(challenger.primary_value)
            )

        complete_constraints = self._constraints(challenger_constraints)
        if missing:
            return PairedEvaluationSummary(
                plan.plan_id,
                plan.tier,
                control_program_id,
                challenger_program_id,
                None,
                complete_constraints,
                expected_blocks - len(missing_blocks),
                expected_blocks,
                tuple(missing),
                {cell.cell_id: cell.block_id for cell in plan.cells},
                tuple(measurements),
            )

        control_means = {
            block: statistics.fmean(values)
            for block, values in control_by_block.items()
        }
        challenger_means = {
            block: statistics.fmean(values)
            for block, values in challenger_by_block.items()
        }
        sign = 1.0 if metric_direction == "maximize" else -1.0
        deltas = {
            block: sign * (challenger_means[block] - control_means[block])
            for block in control_means
        }
        # Keep paired replicate differences before reducing them to one task
        # mean. The nominal measurement seed does not imply common randomness.
        block_cells = {
            block: [cell for cell in plan.cells if cell.block_id == block]
            for block in control_means
        }
        block_delta_variances: dict[str, float | None] = {}
        for block, controls in control_by_block.items():
            cells = block_cells[block]
            paired_deltas = [
                sign * (child - parent)
                for parent, child in zip(controls, challenger_by_block[block])
            ]
            genuine_repeats = (
                len(paired_deltas) > 1
                and len({cell.task_id for cell in cells}) == 1
                and len({cell.replicate for cell in cells}) == len(cells)
            )
            block_delta_variances[block] = (
                statistics.variance(paired_deltas) / len(paired_deltas)
                if genuine_repeats
                else None
            )
        estimate_kwargs: dict[str, Any] = {}
        if getattr(self.estimator, "supports_replicate_variance", False):
            estimate_kwargs["block_delta_variances"] = block_delta_variances
        estimate = self.estimator.estimate(
            block_deltas=deltas,
            challenger_by_block=challenger_means,
            plan=plan,
            **estimate_kwargs,
        )
        summary = PairedEvaluationSummary(
            plan.plan_id,
            plan.tier,
            control_program_id,
            challenger_program_id,
            estimate,
            complete_constraints,
            expected_blocks,
            expected_blocks,
            (),
            {cell.cell_id: cell.block_id for cell in plan.cells},
            tuple(measurements),
        )
        if plan.tier is EvidenceTier.SEALED_C:
            assert plan.plan_commitment_id is not None
            assert sealed_authorization is not None
            assert sealed_repository is not None
            assert summary.estimate is not None
            assert sealed_claim_ttl is not None
            recorded_at = self.clock()
            sealed_claim = sealed_repository.renew_evaluation_claim(
                sealed_claim,
                now=recorded_at,
                ttl=sealed_claim_ttl,
            )
            record_sealed_evaluation(
                sealed_repository,
                sealed_authorization,
                sealed_claim,
                plan_id=plan.plan_id,
                plan_digest=plan.plan_commitment_id,
                root_program_id=control_program_id,
                champion_program_id=challenger_program_id,
                measured_blocks=summary.measured_blocks,
                measurements_per_side=len(summary.measurements),
                public_result={
                    "primary_delta_mean": summary.estimate.primary_delta_mean,
                    "primary_delta_standard_error": (
                        summary.estimate.primary_delta_standard_error
                    ),
                    "effective_sample_size": (summary.estimate.effective_sample_size),
                    "challenger_absolute_value": (
                        summary.estimate.challenger_absolute_value
                    ),
                    "constraints_pass": summary.constraints_pass,
                    "estimator_protocol": self._sealed_estimator_protocol(),
                },
                recorded_at=recorded_at,
            )
        return summary

    def _sealed_estimator_protocol(self) -> str:
        estimator_type = type(self.estimator)
        coordinates = (estimator_type.__module__, estimator_type.__qualname__)
        if coordinates == (
            "agentpilot.evaluation.paired",
            "UniformPairedEstimator",
        ):
            return "uniform-complete-paired-replicates-v2"
        if coordinates == (
            "agentpilot.evaluation.information_weighting",
            "HorvitzThompsonPairedEstimator",
        ):
            return "horvitz-thompson-paired-delta-replicates-v2"
        raise ValueError("sealed C requires a framework-approved estimator")

    def _measure_sealed(
        self,
        *,
        repository: SealedRepository,
        claim: SealedEvaluationClaim,
        slot: SealedCostSlot,
        program_id: str,
        cell: EvaluationCell,
        claim_ttl: timedelta,
    ) -> tuple[CellMeasurement, SealedEvaluationClaim]:
        execute_or_recover = getattr(self.executor, "execute_or_recover_sealed", None)
        if not callable(execute_or_recover):
            raise TypeError(
                "sealed C executor must provide execute_or_recover_sealed()"
            )
        now = self.clock()
        claim = repository.renew_evaluation_claim(claim, now=now, ttl=claim_ttl)
        current = next(
            item
            for item in repository.get(claim.run_id).cost_slots
            if item.slot_id == slot.slot_id
        )
        if current.state is SealedSlotState.RESERVED:
            current, _fresh = repository.dispatch_slot(
                claim,
                slot_id=current.slot_id,
                dispatched_at=now,
            )
        envelope = execute_or_recover(
            program_id=program_id,
            cell=cell,
            execution_id=current.execution_id,
            max_cost_microusd=current.reserved_microusd,
        )
        completed_at = self.clock()
        if not isinstance(envelope, SealedMeasurementEnvelope):
            raise TypeError("sealed executor must return SealedMeasurementEnvelope")
        if envelope.execution_id != current.execution_id:
            raise ValueError("sealed envelope belongs to another execution")
        self._validate_measurement(envelope.measurement, program_id, cell)
        if current.dispatch_fencing_token is None:
            raise ValueError("sealed slot has no dispatch fence")
        settlement = SealedSlotSettlement(
            settlement_id=stable_id(
                "sealed-slot-settlement",
                current.slot_id,
                current.execution_id,
                current.dispatch_fencing_token,
                envelope.result_identity,
                envelope.actual_cost_microusd,
            ),
            slot_id=current.slot_id,
            execution_id=current.execution_id,
            dispatch_fencing_token=current.dispatch_fencing_token,
            result_identity=envelope.result_identity,
            actual_cost_microusd=envelope.actual_cost_microusd,
            settled_at=completed_at,
        )
        persisted = repository.settle_slot(claim, settlement)
        if (
            persisted.settlement is None
            or persisted.settlement.settlement_id != settlement.settlement_id
        ):
            raise ValueError("sealed settlement differs from executor envelope")
        if persisted.state is not SealedSlotState.SETTLED:
            raise SealedConflict(
                "sealed result arrived after its evaluation claim was superseded"
            )
        return envelope.measurement, claim

    @staticmethod
    def _validate_measurement(
        measurement: CellMeasurement,
        program_id: str,
        cell: EvaluationCell,
    ) -> None:
        if measurement.program_id != program_id:
            raise ValueError("cell measurement belongs to another program")
        if measurement.cell_id != cell.cell_id:
            raise ValueError("cell measurement belongs to another cell")

    @staticmethod
    def _constraints(values: Sequence[bool | None]) -> bool | None:
        if any(value is False for value in values):
            return False
        if not values or any(value is None for value in values):
            return None
        return True


@dataclass(frozen=True)
class PairedDiagnosticRule:
    """Veto a challenger that buys its score with a resource regression.

    The primary metric prices only the graded artifact, so a change that keeps
    the same answer while tripling the work needed to reach it measures as
    neutral and is admitted on noise alone.  This rule reads one boolean-valued
    diagnostic that both sides already record and compares how often each side
    lands in its bad state, which stays a rate rather than an all-or-nothing
    per-cell veto: a control that occasionally exhausts its own budget is normal
    and must not fail every wave containing it.
    """

    diagnostic_key: str
    regressed_value: str
    max_rate_increase: float

    def __post_init__(self) -> None:
        require_text(self.diagnostic_key, "diagnostic_key")
        require_text(self.regressed_value, "regressed_value")
        if (
            not math.isfinite(self.max_rate_increase)
            or not 0.0 <= self.max_rate_increase <= 1.0
        ):
            raise ValueError("max_rate_increase must be finite and within [0, 1]")

    def rate_increase(
        self, measurements: Sequence[tuple[CellMeasurement, CellMeasurement]]
    ) -> float | None:
        """Return challenger minus control regressed-state rate, or None.

        ``None`` means the diagnostic was not reported on both sides for any
        cell, which is a missing reading rather than a passing comparison.
        """

        control_hits = challenger_hits = paired = 0
        for control, challenger in measurements:
            control_state = control.diagnostics.get(self.diagnostic_key)
            challenger_state = challenger.diagnostics.get(self.diagnostic_key)
            if control_state is None or challenger_state is None:
                continue
            paired += 1
            control_hits += control_state == self.regressed_value
            challenger_hits += challenger_state == self.regressed_value
        if not paired:
            return None
        return (challenger_hits - control_hits) / paired


def materialize_paired_outcome(
    *,
    action_id: str,
    evaluation_plan_record_id: str,
    summary: PairedEvaluationSummary,
    admission_rule: AdmissionRule,
    statistic_identity: str,
    attribution: OutcomeAttribution,
    completed_at: datetime,
    child_program_id: str,
    child_descriptor: StateDescriptor,
    child_cell_results: Mapping[str, bool | None],
    child_cell_notes: Mapping[str, str] | None = None,
    cell_diagnostics: Mapping[str, Mapping[str, Any]] | None = None,
    diagnostic_rule: PairedDiagnosticRule | None = None,
) -> PairedOutcome:
    """Convert generic paired evidence into the canonical domain outcome."""

    if not isinstance(summary, PairedEvaluationSummary):
        raise TypeError("summary must be a PairedEvaluationSummary")
    require_text(evaluation_plan_record_id, "evaluation_plan_record_id")
    if summary.tier is not EvidenceTier.ADAPTIVE_B:
        raise ValueError("normal paired outcomes must materialize frozen adaptive B")
    if not isinstance(attribution, OutcomeAttribution):
        raise TypeError("attribution must be an OutcomeAttribution")
    if child_program_id != summary.challenger_program_id:
        raise ValueError("child_program_id differs from evaluated challenger")
    expected_cell_ids = {
        challenger.cell_id for _control, challenger in summary.measurements
    }
    if set(child_cell_results) != expected_cell_ids:
        raise ValueError("child cell results differ from evaluated challenger cells")
    if any(
        result is not None and not isinstance(result, bool)
        for result in child_cell_results.values()
    ):
        raise TypeError("child cell results must be bool or None")
    if statistic_identity not in {"point", "lower_bound"}:
        raise ValueError("statistic_identity must be point or lower_bound")
    estimate = summary.estimate
    if estimate is None:
        nonvalid = tuple(
            measurement
            for pair in summary.measurements
            for measurement in pair
            if measurement.status is not MeasurementStatus.VALID
        )
        validity = (
            EvaluationValidity.APPARATUS_FAILURE
            if nonvalid
            and all(
                item.status is MeasurementStatus.APPARATUS_FAILURE for item in nonvalid
            )
            else EvaluationValidity.MISSING
        )
        return PairedOutcome(
            outcome_id=stable_id(
                "paired-outcome",
                action_id,
                evaluation_plan_record_id,
            ),
            action_id=action_id,
            evidence_tier=summary.tier,
            evaluation_plan_record_id=evaluation_plan_record_id,
            validity=validity,
            attribution=attribution,
            signed_effectiveness_statistic=None,
            constraints_pass=summary.constraints_pass,
            completed_at=completed_at,
            admission_fate=AdmissionFate.INCONCLUSIVE.value,
            estimator_identity={"status": "not_estimated"},
            cell_diagnostics=cell_diagnostics or {},
        )

    constraints_pass = summary.constraints_pass
    resource_regression: float | None = None
    if diagnostic_rule is not None and constraints_pass is not False:
        resource_regression = diagnostic_rule.rate_increase(summary.measurements)
        if resource_regression is None:
            constraints_pass = None
        elif resource_regression > diagnostic_rule.max_rate_increase:
            constraints_pass = False
    evidence = PairedEvidence(
        estimate=estimate.primary_delta_mean,
        standard_error=estimate.primary_delta_standard_error,
        effective_sample_size=estimate.effective_sample_size,
        measured_blocks=summary.measured_blocks,
        expected_blocks=summary.expected_blocks,
        constraints_pass=constraints_pass,
    )
    verdict = decide_admission(evidence, admission_rule)
    admission_fate = verdict.fate
    if attribution is not OutcomeAttribution.ATTRIBUTABLE:
        admission_fate = AdmissionFate.INCONCLUSIVE
    validity = (
        EvaluationValidity.VALID
        if constraints_pass is not None
        else EvaluationValidity.INCONCLUSIVE
    )
    signed_statistic = (
        estimate.primary_delta_mean
        if statistic_identity == "point"
        else verdict.conservative_lower
    )
    readout = (
        ChildProgramReadout(
            child_program_id=child_program_id,
            descriptor=child_descriptor,
            absolute_primary_value=estimate.challenger_absolute_value,
            cell_results=child_cell_results,
            cell_notes=child_cell_notes or {},
        )
        if attribution is OutcomeAttribution.ATTRIBUTABLE
        else None
    )
    return PairedOutcome(
        outcome_id=stable_id(
            "paired-outcome",
            action_id,
            evaluation_plan_record_id,
        ),
        action_id=action_id,
        evidence_tier=summary.tier,
        evaluation_plan_record_id=evaluation_plan_record_id,
        validity=validity,
        attribution=attribution,
        signed_effectiveness_statistic=signed_statistic,
        constraints_pass=constraints_pass,
        completed_at=completed_at,
        primary_delta_mean=estimate.primary_delta_mean,
        primary_delta_lower=verdict.conservative_lower,
        resource_regression=resource_regression,
        admission_fate=admission_fate.value,
        estimator_identity=estimate.estimator_identity,
        cell_diagnostics=cell_diagnostics or {},
        child_readout=readout,
    )


__all__ = [
    "CancellableCellExecutor",
    "CellExecutor",
    "CellMeasurement",
    "ControlPrefetchHandle",
    "EvaluationCell",
    "EvidenceTier",
    "FrozenPairedPlan",
    "MeasurementStatus",
    "materialize_paired_outcome",
    "PairedDiagnosticRule",
    "PairedEstimate",
    "PairedEstimator",
    "PairedEvaluationEngine",
    "PairedEvaluationSummary",
    "UniformPairedEstimator",
]
