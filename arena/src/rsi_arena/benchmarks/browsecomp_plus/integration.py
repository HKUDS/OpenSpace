"""BrowseComp-Plus single trusted ``BenchmarkIntegration`` entrypoint."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from rsi_arena.contracts import ScopeSpec
from rsi_arena.integration import (
    BenchmarkIntegration,
    IntegrationFactory,
    TrustedResolvedCampaignConfig,
)

from .runtime import (
    BrowseCompPlusCampaignAdapter,
    BrowseCompPlusDevelopmentAdapter,
    BrowseCompPlusTaskExecutionBackend,
)


def create_browsecomp_plus_integration_factory(
    *,
    development_adapter: BrowseCompPlusDevelopmentAdapter,
    campaign_adapter: BrowseCompPlusCampaignAdapter,
    execution_backend: BrowseCompPlusTaskExecutionBackend,
    trusted_root: Path,
    scope: ScopeSpec,
    resource_identity: Mapping[str, object],
) -> IntegrationFactory:
    if (
        development_adapter.execution_backend is not execution_backend
        or campaign_adapter.execution_backend is not execution_backend
    ):
        raise ValueError(
            "BrowseComp integration adapters must share one execution backend"
        )

    def create(config: TrustedResolvedCampaignConfig) -> BenchmarkIntegration:
        return BenchmarkIntegration(
            benchmark_id="browsecomp_plus",
            development_adapter=development_adapter,
            campaign_adapter=campaign_adapter,
            execution_backend=execution_backend,
            trusted_root=trusted_root,
            scope=scope,
            campaign_freeze=config.campaign_freeze,
            plan=config.plan,
            resource_identity=resource_identity,
        )

    return create


__all__ = ["create_browsecomp_plus_integration_factory"]
