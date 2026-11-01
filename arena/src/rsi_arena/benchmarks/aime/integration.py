"""AIME's single trusted Arena BenchmarkIntegration entrypoint."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from rsi_arena.contracts import ScopeSpec
from rsi_arena.evaluation import BenchAdapter
from rsi_arena.integration import (
    BenchmarkIntegration,
    IntegrationFactory,
    TrustedResolvedCampaignConfig,
)

from .campaign import AimeCampaignBenchmarkAdapter
from .development import AimeDevelopmentAdapter


def create_aime_integration_factory(
    *,
    development_adapter: AimeDevelopmentAdapter,
    campaign_adapter: AimeCampaignBenchmarkAdapter,
    execution_backend: BenchAdapter,
    trusted_root: Path,
    scope: ScopeSpec,
    resource_identity: Mapping[str, object],
) -> IntegrationFactory:
    bound_backend = campaign_adapter.execution_backend
    if bound_backend is not execution_backend:
        raise ValueError("AIME integration backend differs from its campaign adapter")

    def create(config: TrustedResolvedCampaignConfig) -> BenchmarkIntegration:
        return BenchmarkIntegration(
            benchmark_id="aime",
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


__all__ = ["create_aime_integration_factory"]
