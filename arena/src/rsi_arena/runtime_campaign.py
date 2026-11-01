"""Evaluation-facing projection of one explicit :class:`CampaignFreeze` cell."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from rsi_arena.manifests import (
    CampaignAggregateSpec,
    CampaignFreeze,
    DatasetBoundary,
    ManifestValidationError,
    SampleSlot,
    load_campaign_freeze,
)


class RuntimeCampaignError(ValueError):
    """A campaign freeze or cell projection is missing or inconsistent."""


@dataclass(frozen=True)
class CampaignCellPlan:
    """Immutable development-boundary projection for one evaluation cell."""

    campaign_id: str
    freeze_id: str
    cell_id: str
    benchmark: str
    source_reference: str
    method: str
    run_kind: str
    dataset_boundary: DatasetBoundary
    aggregate: CampaignAggregateSpec | None = None

    @property
    def development_task_ids(self) -> tuple[str, ...]:
        """The only task membership authorized by the kernel."""

        return self.dataset_boundary.development_set.ordered_task_ids


def _freeze_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_dir():
        path = path / "campaign-freeze.json"
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeCampaignError("campaign freeze is unavailable") from exc


def _cell_projection(
    freeze: CampaignFreeze, *, cell_id: str, benchmark_id: str, run_kind: str
) -> CampaignCellPlan:
    cell = freeze.cells.get(cell_id)
    if cell is None:
        raise RuntimeCampaignError(f"campaign freeze has no cell {cell_id!r}")
    if benchmark_id not in cell.benchmark_ids:
        raise RuntimeCampaignError(
            f"campaign cell {cell_id!r} has no benchmark {benchmark_id!r}"
        )
    boundary = freeze.dataset_boundaries[benchmark_id]
    source = freeze.benchmarks[benchmark_id]
    return CampaignCellPlan(
        campaign_id=freeze.campaign_id,
        freeze_id=freeze.freeze_id,
        cell_id=cell_id,
        benchmark=benchmark_id,
        source_reference=source.source_reference,
        method=cell.method,
        run_kind=run_kind,
        dataset_boundary=boundary,
        aggregate=cell.aggregate,
    )


def runtime_campaign_plan_from_freeze(
    freeze: CampaignFreeze,
    *,
    cell_id: str,
    benchmark_id: str,
    run_kind: str = "generic-command",
) -> CampaignCellPlan:
    """Project one benchmark child from an in-memory boundary authority."""

    if not isinstance(freeze, CampaignFreeze):
        raise TypeError("freeze must be a CampaignFreeze")
    if not isinstance(cell_id, str) or not cell_id.strip():
        raise RuntimeCampaignError("cell_id must be a non-empty string")
    if not isinstance(benchmark_id, str) or not benchmark_id.strip():
        raise RuntimeCampaignError("benchmark_id must be a non-empty string")
    if not isinstance(run_kind, str) or not run_kind.strip():
        raise RuntimeCampaignError("run_kind must be a non-empty string")
    benchmark_id = benchmark_id.strip()
    if benchmark_id not in freeze.dataset_boundaries:
        raise RuntimeCampaignError(f"campaign freeze has no benchmark {benchmark_id!r}")
    return _cell_projection(
        freeze,
        cell_id=cell_id.strip(),
        benchmark_id=benchmark_id,
        run_kind=run_kind.strip(),
    )


def load_runtime_campaign_plan(
    freeze_ref: str | Path,
    cell_id: str,
    run_kind: str,
    *,
    benchmark_id: str | None = None,
) -> CampaignCellPlan:
    """Load one child projection from the active multi-benchmark authority."""

    plans = load_runtime_campaign_plans(freeze_ref, cell_id, run_kind)
    if benchmark_id is not None:
        try:
            return plans[str(benchmark_id)]
        except KeyError as exc:
            raise RuntimeCampaignError(
                f"campaign cell {cell_id!r} has no benchmark {benchmark_id!r}"
            ) from exc
    if len(plans) != 1:
        raise RuntimeCampaignError(
            "multi-benchmark cell requires an explicit benchmark_id or multi loader"
        )
    return next(iter(plans.values()))


def load_runtime_campaign_plans(
    freeze_ref: str | Path,
    cell_id: str,
    run_kind: str,
) -> Mapping[str, CampaignCellPlan]:
    """Load ordered single-benchmark child plans for one campaign cell."""

    if not isinstance(cell_id, str) or not cell_id.strip():
        raise RuntimeCampaignError("cell_id must be a non-empty string")
    if not isinstance(run_kind, str) or not run_kind.strip():
        raise RuntimeCampaignError("run_kind must be a non-empty string")
    cell_id = cell_id.strip()
    run_kind = run_kind.strip()
    try:
        freeze = load_campaign_freeze(_freeze_path(freeze_ref))
    except ManifestValidationError as exc:
        raise RuntimeCampaignError(str(exc)) from exc
    cell = freeze.cells.get(cell_id)
    if cell is None:
        raise RuntimeCampaignError(f"campaign freeze has no cell {cell_id!r}")
    return MappingProxyType(
        {
            benchmark_id: _cell_projection(
                freeze,
                cell_id=cell_id,
                benchmark_id=benchmark_id,
                run_kind=run_kind,
            )
            for benchmark_id in cell.benchmark_ids
        }
    )


def assert_development_slots(
    plan: CampaignCellPlan, slots: Sequence[SampleSlot]
) -> tuple[SampleSlot, ...]:
    """Validate caller-selected slots against authoritative development membership."""

    realized = tuple(slots)
    if not realized:
        raise RuntimeCampaignError("attempt selected no development sample slots")
    if any(not isinstance(slot, SampleSlot) for slot in realized):
        raise RuntimeCampaignError("attempt slots must contain SampleSlot values")
    if tuple(slot.sample_index for slot in realized) != tuple(range(len(realized))):
        raise RuntimeCampaignError(
            "attempt sample indexes must be contiguous and ordered"
        )
    eligible = set(plan.development_task_ids)
    if any(slot.task_id not in eligible for slot in realized):
        raise RuntimeCampaignError(
            "attempt selected a task outside frozen development membership"
        )
    if len({slot.task_id for slot in realized}) != len(realized):
        raise RuntimeCampaignError("attempt sampling cannot repeat task IDs")
    return realized


def runtime_realization_record(
    plan: CampaignCellPlan,
    slots: Sequence[SampleSlot],
) -> dict[str, object]:
    """Persist one caller-owned selection without turning it into kernel policy."""

    realized = assert_development_slots(plan, slots)
    return {
        "schema": "development-subset-realization",
        "campaign_id": plan.campaign_id,
        "freeze_id": plan.freeze_id,
        "cell_id": plan.cell_id,
        "benchmark": plan.benchmark,
        "method": plan.method,
        "development_set_digest": plan.dataset_boundary.development_set.digest,
        "benchmark_slots": [slot.as_dict() for slot in realized],
    }


def runtime_plan_provenance(
    plan: CampaignCellPlan, slots: Sequence[SampleSlot]
) -> dict[str, object]:
    realization = runtime_realization_record(plan, slots)
    return {
        "campaign_id": plan.campaign_id,
        "campaign_freeze_id": plan.freeze_id,
        "campaign_cell_id": plan.cell_id,
        "source_reference": plan.source_reference,
        "dataset_boundary": plan.dataset_boundary.as_dict(),
        "sample_slots": list(realization["benchmark_slots"]),
    }
