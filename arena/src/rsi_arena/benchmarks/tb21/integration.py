"""Terminal-Bench 2.1 trusted Arena integration entrypoint."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from rsi_arena.contracts import ScopeSpec
from rsi_arena.integration import (
    BenchmarkIntegration,
    IntegrationFactory,
    TrustedResolvedCampaignConfig,
)

from .campaign import TB2CampaignBenchmarkAdapter, TB2CampaignExecutionBackend
from .development import TB2DevelopmentAdapter


def create_tb2_integration_factory(
    *,
    development_adapter: TB2DevelopmentAdapter,
    campaign_adapter: TB2CampaignBenchmarkAdapter,
    execution_backend: TB2CampaignExecutionBackend,
    trusted_root: Path,
    scope: ScopeSpec,
    resource_identity: Mapping[str, object],
) -> IntegrationFactory:
    bound_backend = campaign_adapter.execution_backend
    if bound_backend is not execution_backend:
        raise ValueError("TB2 integration backend differs from its campaign adapter")

    def create(config: TrustedResolvedCampaignConfig) -> BenchmarkIntegration:
        return BenchmarkIntegration(
            benchmark_id="tb2",
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


__all__ = ["create_tb2_integration_factory"]
