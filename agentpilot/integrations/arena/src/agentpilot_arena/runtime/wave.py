"""Pre-dispatch construction and execution-time resolution of B-wave plans.

Sampling belongs to the batch freeze barrier, never to action execution.  This
module therefore has two deliberately separate responsibilities:

* pure helpers turn cutoff-safe counts into a frozen prior and turn that prior
  into one durable action-plan record; and
* the evaluator-side resolver reads that already-persisted record and rebuilds
  its estimator without consulting live action outcomes.

Neither side of the paired Arena evaluation is allowed to draw a wave.
"""

# Persistent format and evaluation identifiers retain their historical values.

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol
from urllib.parse import quote, unquote

from agentpilot.domain import EvidenceTier, PolicyAction, SearchState
from agentpilot.evaluation import (
    HT_ESTIMATOR_CONTRACT,
    UNIFORM_ESTIMATOR_CONTRACT,
    EvaluationCell,
    FrozenActionEvaluationPlan,
    FrozenEvaluationBatch,
    FrozenInformationPriorSnapshot,
    FrozenPairedPlan,
    HorvitzThompsonPairedEstimator,
    InformationWeighting,
    SamplingUnit,
    TaskPassPrior,
    UniformPairedEstimator,
    build_measurement_cells,
    draw_stratified_sample,
    information_weights,
)
from agentpilot.evaluation.frozen_plan import (
    LEGACY_HT_ESTIMATOR_CONTRACT,
    LEGACY_UNIFORM_ESTIMATOR_CONTRACT,
)
from agentpilot.persistence import UnitOfWorkFactory
from rsi_arena import SampleSlot

WAVE_DESIGN_IDENTITY = "openthink-stratified-b-wave"
WAVE_INFORMATION_WEIGHTING_CONTRACT = "openthink-information-weighting"
WAVE_CELL_ID_CONTRACT = "openthink-b-cell-percent"
_CELL_ID_PREFIX = "b"


class FrozenPlanResolutionError(RuntimeError):
    """A trusted evaluator could not resolve one exact frozen action plan."""


def arena_sample_slots(plan: FrozenPairedPlan) -> tuple[SampleSlot, ...]:
    """Project one frozen AgentPilot wave onto Arena's exact slot identity."""

    if not isinstance(plan, FrozenPairedPlan):
        raise TypeError("plan must be a FrozenPairedPlan")
    return tuple(
        SampleSlot(
            sample_index=sample_index,
            task_id=cell.task_id,
            replicate=cell.replicate,
            measurement_seed=cell.measurement_seed,
        )
        for sample_index, cell in enumerate(plan.cells)
    )


def wave_cell_id(task_id: str, replicate: int) -> str:
    """Name a B measurement slot."""

    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task_id must be non-empty text")
    if isinstance(replicate, bool) or not isinstance(replicate, int) or replicate < 0:
        raise ValueError("replicate must be a non-negative integer")
    return f"{_CELL_ID_PREFIX}/{quote(task_id, safe='')}/{replicate}"


def wave_task_id(cell_id: str) -> str | None:
    """Recover the task a recorded B cell measured, or ``None``."""

    parts = cell_id.split("/")
    if len(parts) != 3 or parts[0] != _CELL_ID_PREFIX or not parts[1]:
        return None
    if not parts[2].isdigit() or str(int(parts[2])) != parts[2]:
        return None
    decoded = unquote(parts[1])
    if not decoded or quote(decoded, safe="") != parts[1]:
        return None
    return decoded


def selection_seed(base: int, action_id: str) -> int:
    """Derive one action's selection seed from its stable identity."""

    payload = json.dumps(
        {"action_id": action_id, "base": base, "method": WAVE_DESIGN_IDENTITY},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return int(hashlib.sha256(payload).hexdigest()[:16], 16) & ((1 << 63) - 1)


def fold_task_counts(
    observed: Mapping[str, tuple[int, int]],
    *,
    task_ids: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Fold cutoff-safe cell counts onto the configured task population."""

    allowed = frozenset(task_ids)
    folded: dict[str, tuple[int, int]] = {}
    for cell_id, raw_counts in observed.items():
        if (
            not isinstance(raw_counts, (tuple, list))
            or len(raw_counts) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in raw_counts
            )
        ):
            raise ValueError("observed cell counts must be non-negative pairs")
        task_id = wave_task_id(cell_id)
        if task_id is None or task_id not in allowed:
            continue
        passes, failures = raw_counts
        seen_passes, seen_failures = folded.get(task_id, (0, 0))
        folded[task_id] = (seen_passes + passes, seen_failures + failures)
    return folded


def task_priors(
    observed: Mapping[str, tuple[int, int]],
    *,
    task_ids: tuple[str, ...],
) -> dict[str, TaskPassPrior]:
    """Build Beta pass priors from an explicit, already-frozen observation set."""

    return {
        task_id: TaskPassPrior(
            alpha=1.0 + passes,
            beta=1.0 + failures,
            n_obs=passes + failures,
        )
        for task_id, (passes, failures) in fold_task_counts(
            observed, task_ids=task_ids
        ).items()
    }


class SamplingSettings(Protocol):
    scope_identity: str
    plan_id: str
    size: int
    replicates: int
    selection_seed_base: int
    units: tuple[SamplingUnit, ...]
    information_weighting: InformationWeighting

    @property
    def population_size(self) -> int: ...


class CutoffCellResultReader(Protocol):
    """Read only action outcomes visible at one explicit state cutoff."""

    source_scope_id: str

    def observed_cell_results(
        self, *, cutoff: datetime
    ) -> Mapping[str, tuple[int, int]]: ...


def frozen_task_statistics(
    *,
    settings: SamplingSettings,
    observed_cell_results: Mapping[str, tuple[int, int]],
) -> tuple[dict[str, tuple[int, int]], dict[str, float]]:
    """Compute counts and weights from one caller-supplied cutoff snapshot.

    There is intentionally no callback here.  The batch freezer must read the
    action journal with the SearchState cutoff first, then pass that immutable
    value into this function.
    """

    task_ids = tuple(unit.unit_id for unit in settings.units)
    counts = fold_task_counts(observed_cell_results, task_ids=task_ids)
    weighting = settings.information_weighting
    if weighting.enabled:
        priors = {
            task_id: TaskPassPrior(
                alpha=1.0 + passes,
                beta=1.0 + failures,
                n_obs=passes + failures,
            )
            for task_id, (passes, failures) in counts.items()
        }
        weights = information_weights(task_ids, priors=priors, config=weighting)
    else:
        weights = {task_id: 1.0 for task_id in task_ids}
    return counts, weights


def freeze_sampled_wave_plan(
    *,
    action: PolicyAction,
    settings: SamplingSettings,
    prior_snapshot: FrozenInformationPriorSnapshot,
) -> FrozenActionEvaluationPlan:
    """Build one durable sampled plan solely from a frozen prior snapshot."""

    if not isinstance(action, PolicyAction):
        raise TypeError("action must be an AgentPilot PolicyAction")
    if not isinstance(prior_snapshot, FrozenInformationPriorSnapshot):
        raise TypeError("prior_snapshot must be FrozenInformationPriorSnapshot")
    if (
        prior_snapshot.run_id,
        prior_snapshot.batch_id,
        prior_snapshot.state_id,
    ) != (action.run_id, action.batch_id, action.state_id):
        raise ValueError("prior snapshot differs from the action coordinates")
    if prior_snapshot.weighting_contract != WAVE_INFORMATION_WEIGHTING_CONTRACT:
        raise ValueError("prior snapshot uses a different weighting contract")
    task_ids = tuple(unit.unit_id for unit in settings.units)
    if set(prior_snapshot.task_weights) != set(task_ids):
        raise ValueError("prior snapshot weights differ from the B population")
    weighting = settings.information_weighting
    weights = dict(prior_snapshot.task_weights) if weighting.enabled else None
    seed = selection_seed(settings.selection_seed_base, action.action_id)
    draw = draw_stratified_sample(
        settings.units,
        size=settings.size,
        selection_seed=seed,
        weights=weights,
    )
    schedule = build_measurement_cells(
        draw.selected,
        scope_identity=settings.scope_identity,
        replicates=settings.replicates,
    )
    plan = FrozenPairedPlan(
        plan_id=f"{settings.plan_id}-{action.action_id}",
        tier=EvidenceTier.ADAPTIVE_B,
        cells=tuple(
            EvaluationCell(
                cell_id=wave_cell_id(slot.unit_id, slot.replicate_index),
                task_id=slot.unit_id,
                block_id=slot.unit_id,
                measurement_seed=slot.seed,
                replicate=slot.replicate_index,
            )
            for slot in schedule
        ),
        target_population_size=settings.population_size,
        design_identity={
            "name": WAVE_DESIGN_IDENTITY,
            "cell_id_contract": WAVE_CELL_ID_CONTRACT,
            "sampling": draw.sampling_method,
            "scope": settings.scope_identity,
            "size": settings.size,
            "population": settings.population_size,
            "replicates": settings.replicates,
            "selection_seed": seed,
            "prior_snapshot_id": prior_snapshot.snapshot_id,
            "information_weighting": {
                "enabled": weighting.enabled,
                "floor": weighting.floor,
            },
        },
    )
    return FrozenActionEvaluationPlan.create(
        run_id=action.run_id,
        batch_id=action.batch_id,
        state_id=action.state_id,
        action_id=action.action_id,
        cutoff=prior_snapshot.cutoff,
        plan=plan,
        sampling_draw=draw,
        prior_snapshot_id=prior_snapshot.snapshot_id,
        estimator_contract=HT_ESTIMATOR_CONTRACT,
    )


def freeze_census_plan(
    *,
    action: PolicyAction,
    plan: FrozenPairedPlan,
    prior_snapshot: FrozenInformationPriorSnapshot,
) -> FrozenActionEvaluationPlan:
    """Bind an existing adaptive-B census plan to one frozen action."""

    if not isinstance(action, PolicyAction):
        raise TypeError("action must be an AgentPilot PolicyAction")
    if not isinstance(plan, FrozenPairedPlan):
        raise TypeError("plan must be a FrozenPairedPlan")
    if plan.tier is not EvidenceTier.ADAPTIVE_B:
        raise ValueError("normal action evaluation requires adaptive B")
    if plan.target_population_size != len({cell.block_id for cell in plan.cells}):
        raise ValueError("uniform action plans must be a complete census")
    if (
        prior_snapshot.run_id,
        prior_snapshot.batch_id,
        prior_snapshot.state_id,
    ) != (action.run_id, action.batch_id, action.state_id):
        raise ValueError("prior snapshot differs from the action coordinates")
    if prior_snapshot.weighting_contract != WAVE_INFORMATION_WEIGHTING_CONTRACT:
        raise ValueError("prior snapshot uses a different weighting contract")
    return FrozenActionEvaluationPlan.create(
        run_id=action.run_id,
        batch_id=action.batch_id,
        state_id=action.state_id,
        action_id=action.action_id,
        cutoff=prior_snapshot.cutoff,
        plan=plan,
        sampling_draw=None,
        prior_snapshot_id=prior_snapshot.snapshot_id,
        estimator_contract=UNIFORM_ESTIMATOR_CONTRACT,
    )


@dataclass(frozen=True)
class WaveEvaluationPlanFreezer:
    """Freeze the complete Arena B design before any action is dispatched."""

    freezer_id: str
    result_reader: CutoffCellResultReader
    sampling: SamplingSettings | None = None
    census_plan: FrozenPairedPlan | None = None

    @property
    def source_scope_id(self) -> str:
        return self.result_reader.source_scope_id

    def __post_init__(self) -> None:
        if not isinstance(self.freezer_id, str) or not self.freezer_id.strip():
            raise ValueError("freezer_id must be non-empty")
        if not callable(getattr(self.result_reader, "observed_cell_results", None)):
            raise TypeError("result_reader must provide observed_cell_results()")
        if (
            not isinstance(getattr(self.result_reader, "source_scope_id", None), str)
            or not self.result_reader.source_scope_id.strip()
        ):
            raise TypeError("result_reader must provide a source_scope_id")
        if (self.sampling is None) == (self.census_plan is None):
            raise ValueError("freezer needs exactly one sampled or census design")
        if self.census_plan is not None:
            if not isinstance(self.census_plan, FrozenPairedPlan):
                raise TypeError("census_plan must be a FrozenPairedPlan")
            if self.census_plan.tier is not EvidenceTier.ADAPTIVE_B:
                raise ValueError("census_plan must use adaptive B")
            if any(
                wave_task_id(cell.cell_id) != cell.task_id
                for cell in self.census_plan.cells
            ):
                raise ValueError(
                    "census_plan cells must use the frozen B cell-id contract"
                )
            blocks = {cell.block_id for cell in self.census_plan.cells}
            if self.census_plan.target_population_size != len(blocks):
                raise ValueError("census_plan must cover its complete population")

    def freeze_batch(
        self,
        *,
        state: SearchState,
        actions: tuple[PolicyAction, ...],
    ) -> FrozenEvaluationBatch:
        if not isinstance(state, SearchState):
            raise TypeError("state must be an AgentPilot SearchState")
        actions = tuple(actions)
        if not actions or any(
            not isinstance(action, PolicyAction) for action in actions
        ):
            raise ValueError("freeze_batch needs PolicyAction values")
        action_ids = tuple(action.action_id for action in actions)
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("freeze_batch actions must be unique")
        expected = (state.run_id, state.batch_id, state.state_id)
        if any(
            (action.run_id, action.batch_id, action.state_id) != expected
            for action in actions
        ):
            raise ValueError("evaluation action differs from the frozen state")
        observed = dict(
            self.result_reader.observed_cell_results(cutoff=state.cutoff.available_at)
        )
        if self.sampling is not None:
            counts, weights = frozen_task_statistics(
                settings=self.sampling,
                observed_cell_results=observed,
            )
        else:
            assert self.census_plan is not None
            task_ids = tuple(sorted({cell.block_id for cell in self.census_plan.cells}))
            counts = fold_task_counts(observed, task_ids=task_ids)
            weights = {task_id: 1.0 for task_id in task_ids}
        prior = FrozenInformationPriorSnapshot.create(
            run_id=state.run_id,
            batch_id=state.batch_id,
            state_id=state.state_id,
            cutoff=state.cutoff,
            compatibility=state.compatibility,
            source_scope_id=self.result_reader.source_scope_id,
            task_counts=counts,
            task_weights=weights,
            weighting_contract=WAVE_INFORMATION_WEIGHTING_CONTRACT,
        )
        if self.sampling is not None:
            plans = tuple(
                freeze_sampled_wave_plan(
                    action=action,
                    settings=self.sampling,
                    prior_snapshot=prior,
                )
                for action in actions
            )
        else:
            assert self.census_plan is not None
            plans = tuple(
                freeze_census_plan(
                    action=action,
                    plan=self.census_plan,
                    prior_snapshot=prior,
                )
                for action in actions
            )
        return FrozenEvaluationBatch(prior_snapshot=prior, action_plans=plans)


@dataclass(frozen=True)
class ResolvedPairedPlan:
    """One durable plan record resolved with its exact estimator."""

    record_id: str
    plan: FrozenPairedPlan
    estimator: object

    def __post_init__(self) -> None:
        if not isinstance(self.record_id, str) or not self.record_id.strip():
            raise ValueError("record_id must be non-empty")
        if not isinstance(self.plan, FrozenPairedPlan):
            raise TypeError("plan must be a FrozenPairedPlan")
        if not callable(getattr(self.estimator, "estimate", None)):
            raise TypeError("estimator must provide estimate()")


class PairedPlanResolver(Protocol):
    """Resolve one pre-frozen plan record and its exact estimator."""

    def resolve(self, action: PolicyAction) -> ResolvedPairedPlan: ...


@dataclass(frozen=True)
class DurablePairedPlanResolver:
    """Read a plan record from the AgentPilot UoW and fail closed on drift."""

    unit_of_work: UnitOfWorkFactory

    def __post_init__(self) -> None:
        if not callable(self.unit_of_work):
            raise TypeError("unit_of_work must be callable")

    def resolve(self, action: PolicyAction) -> ResolvedPairedPlan:
        if not isinstance(action, PolicyAction):
            raise TypeError("action must be an AgentPilot PolicyAction")
        try:
            with self.unit_of_work() as work:
                record = work.evaluation_plans.plan_for_action(action.action_id)
                if record is None:
                    raise FrozenPlanResolutionError(
                        f"no frozen evaluation plan for action {action.action_id}"
                    )
                if isinstance(
                    record, FrozenActionEvaluationPlan
                ) and record.estimator_contract in {
                    LEGACY_HT_ESTIMATOR_CONTRACT,
                    LEGACY_UNIFORM_ESTIMATOR_CONTRACT,
                }:
                    raise FrozenPlanResolutionError(
                        "legacy estimator contract is read-only; freeze a new plan before execution"
                    )
                prior = work.evaluation_priors.get(record.prior_snapshot_id)
        except FrozenPlanResolutionError:
            raise
        except (KeyError, LookupError) as exc:
            raise FrozenPlanResolutionError(
                f"frozen evaluation plan dependencies are missing for "
                f"action {action.action_id}"
            ) from exc
        if not isinstance(record, FrozenActionEvaluationPlan):
            raise FrozenPlanResolutionError("evaluation-plan record has wrong type")
        if not isinstance(prior, FrozenInformationPriorSnapshot):
            raise FrozenPlanResolutionError("evaluation prior has wrong type")
        expected_action = (
            action.run_id,
            action.batch_id,
            action.state_id,
            action.action_id,
        )
        actual_action = (
            record.run_id,
            record.batch_id,
            record.state_id,
            record.action_id,
        )
        if actual_action != expected_action:
            raise FrozenPlanResolutionError(
                "frozen evaluation plan conflicts with the action coordinates"
            )
        expected_prior = (
            record.run_id,
            record.batch_id,
            record.state_id,
            record.cutoff,
            record.prior_snapshot_id,
        )
        actual_prior = (
            prior.run_id,
            prior.batch_id,
            prior.state_id,
            prior.cutoff,
            prior.snapshot_id,
        )
        if actual_prior != expected_prior:
            raise FrozenPlanResolutionError(
                "frozen evaluation plan conflicts with its prior snapshot"
            )
        plan = record.plan
        if plan.tier is not EvidenceTier.ADAPTIVE_B:
            raise FrozenPlanResolutionError("frozen action plan is not adaptive B")
        if record.estimator_contract == HT_ESTIMATOR_CONTRACT:
            if record.sampling_draw is None:
                raise FrozenPlanResolutionError(
                    "HT estimator contract is missing its frozen sampling draw"
                )
            estimator: object = HorvitzThompsonPairedEstimator(record.sampling_draw)
        elif record.estimator_contract == UNIFORM_ESTIMATOR_CONTRACT:
            if record.sampling_draw is not None:
                raise FrozenPlanResolutionError(
                    "uniform estimator contract cannot carry a sampling draw"
                )
            if plan.target_population_size != len(
                {cell.block_id for cell in plan.cells}
            ):
                raise FrozenPlanResolutionError(
                    "uniform estimator contract requires a census plan"
                )
            estimator = UniformPairedEstimator()
        else:
            raise FrozenPlanResolutionError(
                f"unsupported estimator contract {record.estimator_contract!r}"
            )
        return ResolvedPairedPlan(record.record_id, plan, estimator)


__all__ = [
    "arena_sample_slots",
    "HT_ESTIMATOR_CONTRACT",
    "UNIFORM_ESTIMATOR_CONTRACT",
    "WAVE_DESIGN_IDENTITY",
    "WAVE_CELL_ID_CONTRACT",
    "WAVE_INFORMATION_WEIGHTING_CONTRACT",
    "CutoffCellResultReader",
    "DurablePairedPlanResolver",
    "FrozenPlanResolutionError",
    "PairedPlanResolver",
    "ResolvedPairedPlan",
    "SamplingSettings",
    "WaveEvaluationPlanFreezer",
    "fold_task_counts",
    "freeze_census_plan",
    "freeze_sampled_wave_plan",
    "frozen_task_statistics",
    "selection_seed",
    "task_priors",
    "wave_cell_id",
    "wave_task_id",
]
