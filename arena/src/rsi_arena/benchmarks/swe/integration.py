"""SWE's single trusted Arena BenchmarkIntegration entrypoint."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from rsi_arena.contracts import ScopeSpec
from rsi_arena.integration import (
    BenchmarkIntegration,
    IntegrationFactory,
    TrustedResolvedCampaignConfig,
)

from .campaign import SweCampaignBenchmarkAdapter, SweTaskCellRunner
from .development import SweDevelopmentAdapter


def create_swe_integration_factory(
    *,
    development_adapter: SweDevelopmentAdapter,
    campaign_adapter: SweCampaignBenchmarkAdapter,
    execution_backend: SweTaskCellRunner,
    trusted_root: Path,
    scope: ScopeSpec,
    resource_identity: Mapping[str, object],
) -> IntegrationFactory:
    bound_backend = campaign_adapter.execution_backend
    if bound_backend is not execution_backend:
        raise ValueError("SWE integration backend differs from its campaign adapter")

    def create(config: TrustedResolvedCampaignConfig) -> BenchmarkIntegration:
        return BenchmarkIntegration(
            benchmark_id="swe",
            development_adapter=development_adapter,
            campaign_adapter=campaign_adapter,
            execution_backend=bound_backend,
            trusted_root=trusted_root,
            scope=scope,
            campaign_freeze=config.campaign_freeze,
            plan=config.plan,
            resource_identity=resource_identity,
        )

    return create


__all__ = ["create_swe_integration_factory"]
