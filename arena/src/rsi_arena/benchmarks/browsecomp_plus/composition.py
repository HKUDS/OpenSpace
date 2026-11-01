"""BrowseComp-Plus package-owned construction for Arena composition."""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from rsi_arena.contracts import ScopeSpec, TaskRef, canonical_digest
from rsi_arena.evaluation_gateway import TrustedEvaluationGateway
from rsi_arena.integration import IntegrationFactory
from rsi_arena.manifests import (
    CampaignFreeze,
    resolve_held_out_task_ids_for_evaluator,
)
from rsi_arena.run_layout import RunLayout, RunValidationError
from rsi_arena.runtime_campaign import (
    CampaignCellPlan,
    runtime_campaign_plan_from_freeze,
)

from .contracts import TrustedCatalog, TrustedTask
from .evaluator import BrowseCompPlusEvaluator, Judge
from .integration import create_browsecomp_plus_integration_factory
from .retrieval import BM25SearchBackend, Tokenizer
from .runtime import (
    BrowseCompPlusCampaignAdapter,
    BrowseCompPlusDevelopmentAdapter,
    BrowseCompPlusTaskExecutionBackend,
    InnerAgentCallableFactory,
)
from .search_transport import BrowseCompRetrievalService

_BENCHMARK_ID = "browsecomp_plus"
_IDENTITY_SCHEMA = "rsi-arena-browsecomp-plus-adapter"
BROWSECOMP_RESOURCE_SCHEMA = "rsi-arena-browsecomp-plus-resources"


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a JSON object")
    try:
        copied = json.loads(json.dumps(dict(value), allow_nan=False))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be finite JSON") from exc
    assert isinstance(copied, dict)
    return copied


def _task_identity(task: TrustedTask) -> dict[str, object]:
    return {
        "query_id": task.query_id,
        "query": task.query,
        "answer": task.answer,
        "evidence_docids": list(task.evidence_docids),
        "gold_docids": list(task.gold_docids),
        "negative_docids": list(task.negative_docids),
    }


def trusted_catalog_digest(catalog: TrustedCatalog) -> str:
    """Digest every trusted scoring field without publishing catalog material."""

    if not isinstance(catalog, TrustedCatalog):
        raise TypeError("BrowseComp-Plus requires a TrustedCatalog")
    return "sha256:" + canonical_digest(
        [_task_identity(task) for task in catalog.development + catalog.held_out]
    )


def _implementation_digest(value: object) -> str:
    target = (
        value if inspect.isfunction(value) or inspect.isclass(value) else type(value)
    )
    try:
        source = inspect.getsource(target)
    except (OSError, TypeError):
        source = f"{target.__module__}:{target.__qualname__}"
    return "sha256:" + canonical_digest(
        {
            "implementation": f"{target.__module__}:{target.__qualname__}",
            "source": source,
        }
    )


def _injected_resource_identity(
    *,
    catalog: TrustedCatalog,
    search_backend: BM25SearchBackend,
    tokenizer: Tokenizer,
    judge: Judge,
    inner_agent_factory: InnerAgentCallableFactory,
    model_runtime: Mapping[str, object] | None,
    agent_config: Mapping[str, object] | None,
) -> Mapping[str, object]:
    """Bind test/operator injection seams when no physical owner supplies identity."""

    return MappingProxyType(
        {
            "schema": BROWSECOMP_RESOURCE_SCHEMA,
            "catalog_sha256": trusted_catalog_digest(catalog),
            "search_backend_sha256": _implementation_digest(search_backend),
            "tokenizer_sha256": _implementation_digest(tokenizer),
            "judge_sha256": _implementation_digest(judge),
            "inner_agent_factory_sha256": _implementation_digest(inner_agent_factory),
            "model_runtime_sha256": "sha256:"
            + canonical_digest(dict(model_runtime or {})),
            "agent_config_sha256": "sha256:"
            + canonical_digest(dict(agent_config or {})),
        }
    )


def _authority(config: Mapping[str, object]) -> tuple[CampaignFreeze, CampaignCellPlan]:
    raw_freeze = config.get("campaign_freeze")
    if not isinstance(raw_freeze, Mapping):
        raise TypeError("BrowseComp-Plus config requires campaign_freeze")
    cell_id = config.get("cell_id")
    if not isinstance(cell_id, str) or not cell_id.strip():
        raise ValueError("BrowseComp-Plus config requires cell_id")
    freeze = CampaignFreeze.from_mapping(raw_freeze)
    plan = runtime_campaign_plan_from_freeze(
        freeze,
        cell_id=cell_id,
        benchmark_id=_BENCHMARK_ID,
    )
    return freeze, plan


def _validate_catalog_authority(
    plan: CampaignCellPlan,
    catalog: TrustedCatalog,
) -> None:
    if not isinstance(catalog, TrustedCatalog):
        raise TypeError("BrowseComp-Plus requires a TrustedCatalog")
    public_ids = tuple(task.query_id for task in catalog.public.development)
    trusted_development_ids = tuple(task.query_id for task in catalog.development)
    held_out_ids = tuple(task.query_id for task in catalog.held_out)
    if (
        public_ids != plan.development_task_ids
        or trusted_development_ids != public_ids
        or set(public_ids) & set(held_out_ids)
    ):
        raise RunValidationError(
            "BrowseComp-Plus catalog disagrees with the public development boundary"
        )
    resolve_held_out_task_ids_for_evaluator(
        plan.dataset_boundary.held_out_set,
        held_out_ids,
    )


@dataclass(frozen=True)
class FrozenBrowseCompPlusBenchmark:
    """Trusted package; held-out membership has no public identity projection."""

    freeze: CampaignFreeze
    plan: CampaignCellPlan
    config: Mapping[str, object]
    catalog: TrustedCatalog
    execution_backend: BrowseCompPlusTaskExecutionBackend
    resource_identity: Mapping[str, object]
    publish_development_retrieved_snippets: bool = False
    publish_development_judge_text: bool = False

    benchmark_id = _BENCHMARK_ID

    def __post_init__(self) -> None:
        expected_plan = runtime_campaign_plan_from_freeze(
            self.freeze,
            cell_id=self.plan.cell_id,
            benchmark_id=self.benchmark_id,
        )
        if expected_plan != self.plan:
            raise RunValidationError(
                "BrowseComp-Plus package changed its CampaignFreeze projection"
            )
        if not isinstance(self.publish_development_retrieved_snippets, bool):
            raise TypeError("BrowseComp development snippet flag must be a boolean")
        if not isinstance(self.publish_development_judge_text, bool):
            raise TypeError("BrowseComp development judge-text flag must be a boolean")
        _validate_catalog_authority(self.plan, self.catalog)
        resources = _mapping(self.resource_identity, "BrowseComp resource identity")
        if resources.get("schema") != BROWSECOMP_RESOURCE_SCHEMA:
            raise RunValidationError("BrowseComp resource identity is invalid")
        object.__setattr__(self, "resource_identity", MappingProxyType(resources))

    @property
    def evaluator_digest(self) -> str:
        return canonical_digest(
            {
                "schema": "rsi-arena-browsecomp-plus-evaluator",
                "resources": dict(self.resource_identity),
            }
        )

    @property
    def resolved_identity(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "schema": _IDENTITY_SCHEMA,
                "benchmark_id": self.benchmark_id,
                "campaign_freeze": self.freeze.as_dict(),
                "cell_id": self.plan.cell_id,
                "resources": dict(self.resource_identity),
            }
        )

    def integration_factory(
        self,
        *,
        trusted_root: Path,
        scope: ScopeSpec,
        layout: RunLayout,
        gateway: TrustedEvaluationGateway,
        development_concurrency: int,
    ) -> IntegrationFactory:
        del layout
        development = BrowseCompPlusDevelopmentAdapter(
            plan=self.plan,
            public_tasks=self.catalog.public.development,
            trusted_tasks=self.catalog.development,
            execution_backend=self.execution_backend,
            gateway=gateway,
            task_concurrency=development_concurrency,
            publish_retrieved_snippets=(self.publish_development_retrieved_snippets),
            publish_judge_text=self.publish_development_judge_text,
        )
        campaign = BrowseCompPlusCampaignAdapter(
            plan=self.plan,
            tasks={task.query_id: task for task in self.catalog.development},
            execution_backend=self.execution_backend,
            evaluator_digest=self.evaluator_digest,
        )
        return create_browsecomp_plus_integration_factory(
            development_adapter=development,
            campaign_adapter=campaign,
            execution_backend=self.execution_backend,
            trusted_root=trusted_root,
            scope=scope,
            resource_identity=self.resource_identity,
        )

    def _verified_held_out(self) -> tuple[TrustedTask, ...]:
        actual_ids = tuple(task.query_id for task in self.catalog.held_out)
        resolve_held_out_task_ids_for_evaluator(
            self.plan.dataset_boundary.held_out_set,
            actual_ids,
        )
        return self.catalog.held_out

    def _held_out_aliases(self) -> tuple[str, ...]:
        aliases = tuple(
            f"opaque-held-out-occurrence-{index:04d}"
            for index in range(len(self.catalog.held_out))
        )
        if set(aliases) & set(self.plan.development_task_ids):
            raise RunValidationError(
                "BrowseComp held-out aliases collide with development"
            )
        return aliases

    def held_out_task_ids_for_evaluator(self) -> tuple[str, ...]:
        """Return report-safe occurrence aliases after private membership verification."""

        self._verified_held_out()
        return self._held_out_aliases()

    def held_out_evaluation_adapter(
        self,
        *,
        layout: RunLayout,
        gateway: TrustedEvaluationGateway,
    ) -> tuple[BrowseCompPlusCampaignAdapter, tuple[TaskRef, ...]]:
        del layout, gateway
        aliases = self._held_out_aliases()
        trusted_tasks = self._verified_held_out()
        private_tasks = dict(zip(aliases, trusted_tasks, strict=True))
        adapter = BrowseCompPlusCampaignAdapter(
            plan=self.plan,
            tasks=private_tasks,
            execution_backend=self.execution_backend,
            development_slots=False,
            publish_retrieval_trajectory=False,
            evaluator_digest=self.evaluator_digest,
        )
        return adapter, tuple(
            TaskRef(task_id=alias, benchmark_id=self.benchmark_id) for alias in aliases
        )

    def close(self) -> None:
        self.execution_backend.close()


def freeze_browsecomp_plus_benchmark(
    config: Mapping[str, object],
    *,
    control_root: Path,
    catalog: TrustedCatalog,
    search_backend: BM25SearchBackend,
    tokenizer: Tokenizer,
    judge: Judge,
    inner_agent_factory: InnerAgentCallableFactory,
    model_runtime: Mapping[str, object] | None = None,
    agent_config: Mapping[str, object] | None = None,
    retrieval_concurrency: int = 1,
    publish_development_judge_text: bool = False,
    retrieval_socket_parent: Path | None = None,
    resource_identity: Mapping[str, object] | None = None,
    task_wall_timeout_seconds: float | None = None,
) -> FrozenBrowseCompPlusBenchmark:
    """Assemble injected physical owners around the existing campaign authority."""

    copied = _mapping(config, "BrowseComp-Plus config")
    if not isinstance(publish_development_judge_text, bool):
        raise TypeError("BrowseComp development judge-text flag must be a boolean")
    freeze, plan = _authority(copied)
    _validate_catalog_authority(plan, catalog)
    retrieval = copied.get("retrieval", {})
    if not isinstance(retrieval, Mapping):
        raise TypeError("BrowseComp retrieval must be a JSON object")
    publish_development_retrieved_snippets = retrieval.get(
        "publish_development_snippets", False
    )
    if not isinstance(publish_development_retrieved_snippets, bool):
        raise TypeError(
            "BrowseComp retrieval.publish_development_snippets must be a boolean"
        )
    service = BrowseCompRetrievalService(
        root=Path(control_root) / "bc-search",
        backend=search_backend,
        tokenizer=tokenizer,
        retrieval_concurrency=retrieval_concurrency,
        socket_parent=retrieval_socket_parent,
    )
    try:
        resources = (
            _injected_resource_identity(
                catalog=catalog,
                search_backend=search_backend,
                tokenizer=tokenizer,
                judge=judge,
                inner_agent_factory=inner_agent_factory,
                model_runtime=model_runtime,
                agent_config=agent_config,
            )
            if resource_identity is None
            else MappingProxyType(
                _mapping(resource_identity, "BrowseComp resource identity")
            )
        )
        backend = BrowseCompPlusTaskExecutionBackend(
            retrieval_service=service,
            evaluator=BrowseCompPlusEvaluator(judge),
            inner_agent_factory=inner_agent_factory,
            model_runtime=model_runtime,
            agent_config=agent_config,
            development_agent_artifact_root=(
                Path(control_root)
                / "benchmark-runtime"
                / _BENCHMARK_ID
                / "development-agent-trajectories"
            ),
            task_wall_timeout_seconds=task_wall_timeout_seconds,
        )
        return FrozenBrowseCompPlusBenchmark(
            freeze=freeze,
            plan=plan,
            config=MappingProxyType(copied),
            catalog=catalog,
            execution_backend=backend,
            resource_identity=resources,
            publish_development_retrieved_snippets=(
                publish_development_retrieved_snippets
            ),
            publish_development_judge_text=publish_development_judge_text,
        )
    except BaseException:
        service.close()
        raise


def resume_browsecomp_plus_benchmark(
    config: Mapping[str, object],
    resolved_identity: Mapping[str, object],
    **kwargs: object,
) -> FrozenBrowseCompPlusBenchmark:
    package = freeze_browsecomp_plus_benchmark(config, **kwargs)  # type: ignore[arg-type]
    if dict(package.resolved_identity) != dict(resolved_identity):
        package.close()
        raise RunValidationError("stored BrowseComp-Plus benchmark identity changed")
    return package


__all__ = [
    "BROWSECOMP_RESOURCE_SCHEMA",
    "FrozenBrowseCompPlusBenchmark",
    "freeze_browsecomp_plus_benchmark",
    "resume_browsecomp_plus_benchmark",
    "trusted_catalog_digest",
]
