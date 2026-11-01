"""Thin launcher seam for frozen multi-benchmark campaign boundaries."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from rsi_arena.contracts import canonical_digest
from rsi_arena.run_layout import (
    RunLayout,
    _empty_development_benchmark_manifest,
)
from rsi_arena.runtime_campaign import (
    CampaignCellPlan,
    RuntimeCampaignError,
)


def _boundary_binding(plan: CampaignCellPlan) -> dict[str, object]:
    return {
        "campaign_freeze_id": plan.freeze_id,
        "benchmark": plan.benchmark,
        "dataset_boundary": plan.dataset_boundary.as_dict(),
    }


def create_multi_runtime_campaign_layout(
    plans: Mapping[str, CampaignCellPlan],
    *,
    runs_root: str | Path,
    campaign: str,
    primary_evaluation_seed: int,
    evaluator_digest: str | None,
    runtime_base: str | Path | None = None,
) -> tuple[
    RunLayout,
    Mapping[str, tuple[str, ...]],
    Mapping[str, dict[str, object]],
]:
    """Create one cell-scoped run with benchmark-keyed development boundaries."""

    ordered = tuple(plans.items())
    if not ordered:
        raise RuntimeCampaignError("multi runtime needs benchmark child plans")
    if isinstance(primary_evaluation_seed, bool) or not isinstance(
        primary_evaluation_seed, int
    ):
        raise RuntimeCampaignError("primary evaluation seed must be an integer")
    first = ordered[0][1]
    if campaign != first.campaign_id:
        raise RuntimeCampaignError(
            "runtime campaign path identity disagrees with the frozen campaign"
        )
    identities = {
        (
            plan.campaign_id,
            plan.freeze_id,
            plan.cell_id,
            plan.method,
            plan.run_kind,
            plan.aggregate,
        )
        for _, plan in ordered
    }
    if len(identities) != 1 or any(
        benchmark_id != plan.benchmark for benchmark_id, plan in ordered
    ):
        raise RuntimeCampaignError(
            "multi runtime child plans do not share one campaign cell identity"
        )
    task_ids_by_benchmark = {
        benchmark_id: plan.development_task_ids for benchmark_id, plan in ordered
    }
    bindings = {benchmark_id: _boundary_binding(plan) for benchmark_id, plan in ordered}
    benchmark_ids = [benchmark_id for benchmark_id, _ in ordered]
    campaign_run_freeze: dict[str, object] = {
        "schema": "rsi-arena-campaign-run-freeze",
        "campaign_id": first.campaign_id,
        "campaign_freeze_id": first.freeze_id,
        "campaign_cell_id": first.cell_id,
        "method": first.method,
        "run_kind": first.run_kind,
        "evaluation_seed": primary_evaluation_seed,
        "benchmark_ids": benchmark_ids,
        "benchmarks": {
            benchmark_id: {
                "dataset_boundary": plan.dataset_boundary.as_dict(),
            }
            for benchmark_id, plan in ordered
        },
    }
    if first.aggregate is not None:
        campaign_run_freeze["aggregate_policy"] = first.aggregate.as_dict()
    campaign_run_freeze["resume_identity"] = "sha256:" + canonical_digest(
        campaign_run_freeze
    )
    run_manifest = {
        "runtime_campaign_schema": "rsi-arena-campaign-run-freeze",
        "run_scope_kind": "campaign_cell",
        "run_scope_id": first.cell_id,
        "campaign_run_freeze": campaign_run_freeze,
        "evaluation_seed": primary_evaluation_seed,
    }
    development_delivery_manifest = {
        "schema": "campaign-development-delivery",
        "status": "not_recorded",
        "benchmarks": {
            benchmark_id: _empty_development_benchmark_manifest(benchmark_id)
            for benchmark_id in benchmark_ids
        },
        "campaign_freeze_id": first.freeze_id,
    }
    layout = RunLayout.create(
        Path(runs_root),
        campaign=campaign,
        method=first.method,
        benchmark=first.cell_id,
        evaluation_seed=primary_evaluation_seed,
        evaluator_digest=evaluator_digest,
        run_kind=first.run_kind,
        runtime_base=Path(runtime_base) if runtime_base is not None else None,
        run_manifest=run_manifest,
        development_delivery_manifest=development_delivery_manifest,
    )
    return (
        layout,
        MappingProxyType(task_ids_by_benchmark),
        MappingProxyType(bindings),
    )
