"""Trusted one-time benchmark packaging for an RSI Arena run.

The package binds already-resolved campaign facts to benchmark-owned
development/evaluation adapters and their execution backend.  Production
composition passes these packages to the plural ``SessionSpine``.
"""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, TypeAlias

from rsi_arena.campaign_evaluation import CampaignBenchmarkAdapter
from rsi_arena.contracts import ScopeSpec
from rsi_arena.manifests import CampaignFreeze
from rsi_arena.runtime_campaign import CampaignCellPlan, RuntimeCampaignError

from .development import DevelopmentAdapter


def _identity_copy(value: Mapping[str, object], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{label} must be a non-empty mapping")
    try:
        copied = json.loads(
            json.dumps(
                dict(value),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must contain JSON identity facts") from exc
    return MappingProxyType(copied)


def _validate_campaign_binding(
    campaign_freeze: CampaignFreeze,
    plan: CampaignCellPlan,
    *,
    benchmark_id: str,
) -> None:
    cell = campaign_freeze.cells.get(plan.cell_id)
    boundary = campaign_freeze.dataset_boundaries.get(benchmark_id)
    source = campaign_freeze.benchmarks.get(benchmark_id)
    if cell is None or boundary is None or source is None:
        raise RuntimeCampaignError("resolved campaign escaped its CampaignFreeze")
    if (
        not campaign_freeze.held_out_sealed
        or plan.benchmark != benchmark_id
        or plan.campaign_id != campaign_freeze.campaign_id
        or plan.freeze_id != campaign_freeze.freeze_id
        or plan.source_reference != source.source_reference
        or plan.dataset_boundary != boundary
        or benchmark_id not in cell.benchmark_ids
    ):
        raise RuntimeCampaignError(
            "resolved campaign plan disagrees with CampaignFreeze"
        )


@dataclass(frozen=True)
class TrustedResolvedCampaignConfig:
    """The minimal resolved run facts missing from benchmark integration."""

    campaign_freeze: CampaignFreeze
    plan: CampaignCellPlan
    attempt_cap: int
    benchmark_concurrency: int
    invalid_cell_retries: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.campaign_freeze, CampaignFreeze):
            raise TypeError("resolved campaign freeze must be a CampaignFreeze")
        if not isinstance(self.plan, CampaignCellPlan):
            raise TypeError("resolved campaign plan must be a CampaignCellPlan")
        if (
            isinstance(self.attempt_cap, bool)
            or not isinstance(self.attempt_cap, int)
            or self.attempt_cap < 1
        ):
            raise ValueError("resolved attempt cap must be positive")
        if (
            isinstance(self.benchmark_concurrency, bool)
            or not isinstance(self.benchmark_concurrency, int)
            or self.benchmark_concurrency < 1
        ):
            raise ValueError("resolved benchmark concurrency must be positive")
        if (
            isinstance(self.invalid_cell_retries, bool)
            or not isinstance(self.invalid_cell_retries, int)
            or not 0 <= self.invalid_cell_retries <= 8
        ):
            raise ValueError("resolved invalid-cell retries must be in [0, 8]")
        _validate_campaign_binding(
            self.campaign_freeze,
            self.plan,
            benchmark_id=self.plan.benchmark,
        )


@dataclass(frozen=True)
class BenchmarkIntegration:
    """One trusted benchmark's resolved Arena package.

    This object does not discover plugins, create a run, choose sampling, or
    own a method's archive/selection behavior.  It only carries facts and
    objects that trusted composition has already resolved.
    """

    benchmark_id: str
    development_adapter: DevelopmentAdapter
    campaign_adapter: CampaignBenchmarkAdapter
    execution_backend: object
    trusted_root: Path
    scope: ScopeSpec
    campaign_freeze: CampaignFreeze
    plan: CampaignCellPlan
    resource_identity: Mapping[str, object]

    def __post_init__(self) -> None:
        benchmark_id = str(self.benchmark_id)
        if not benchmark_id:
            raise ValueError("benchmark integration needs an identity")
        if self.execution_backend is None:
            raise ValueError("benchmark integration needs its execution backend")
        if not isinstance(self.scope, ScopeSpec):
            raise TypeError("benchmark integration scope must be a ScopeSpec")

        supplied_root = Path(self.trusted_root).expanduser().absolute()
        try:
            root_info = supplied_root.lstat()
            trusted_root = supplied_root.resolve(strict=True)
        except OSError as exc:
            raise ValueError("benchmark trusted root is unavailable") from exc
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise ValueError("benchmark trusted root must be a real directory")

        freeze = self.campaign_freeze
        plan = self.plan
        _validate_campaign_binding(freeze, plan, benchmark_id=benchmark_id)
        boundary = freeze.dataset_boundaries[benchmark_id]
        if getattr(self.development_adapter, "benchmark_id", None) != benchmark_id:
            raise RuntimeCampaignError(
                "development adapter belongs to another benchmark"
            )
        if not set(self.development_adapter.frozen_task_ids).issubset(
            set(boundary.development_set.ordered_task_ids)
        ):
            raise RuntimeCampaignError(
                "development adapter escaped canonical development membership"
            )
        if getattr(self.campaign_adapter, "benchmark_id", None) != benchmark_id:
            raise RuntimeCampaignError(
                "evaluation adapter belongs to another benchmark"
            )
        evaluator_digest = getattr(self.campaign_adapter, "evaluator_digest", None)
        if not isinstance(evaluator_digest, str) or not evaluator_digest:
            raise ValueError("evaluation adapter needs a stable evaluator identity")

        object.__setattr__(self, "benchmark_id", benchmark_id)
        object.__setattr__(self, "trusted_root", trusted_root)
        object.__setattr__(
            self,
            "resource_identity",
            _identity_copy(self.resource_identity, "resource identity"),
        )


IntegrationFactory: TypeAlias = Callable[
    [TrustedResolvedCampaignConfig], BenchmarkIntegration
]


__all__ = [
    "BenchmarkIntegration",
    "IntegrationFactory",
    "TrustedResolvedCampaignConfig",
]
