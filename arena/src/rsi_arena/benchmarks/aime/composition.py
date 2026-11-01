"""AIME-owned construction seam used by the kernel composition root."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from rsi_arena.contracts import CellBudget, ScopeSpec, canonical_digest
from rsi_arena.evaluation_gateway import TrustedEvaluationGateway
from rsi_arena.inner_agent_runtime import FrozenInnerAgentSelection
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

from .adapter import create_adapter
from .campaign import AimeCampaignBenchmarkAdapter
from .development import AimeDevelopmentAdapter
from .development_material import (
    CanonicalDevelopmentCorpusHandle,
    CanonicalDevelopmentMaterialRef,
)
from .integration import create_aime_integration_factory

_BENCHMARK_ID = "aime"
_IDENTITY_SCHEMA = "rsi-arena-aime-snapshot"


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    try:
        copied = json.loads(json.dumps(dict(value), allow_nan=False))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be JSON-compatible") from exc
    assert isinstance(copied, dict)
    return copied


def _reject_development_policy(config: Mapping[str, object]) -> None:
    forbidden = sorted({"baseline", "development_policy"} & set(config))
    if forbidden:
        raise ValueError(
            "AIME development policy belongs to the method, not benchmark config: "
            + ", ".join(forbidden)
        )


def _adapter_config(config: Mapping[str, object]) -> dict[str, object]:
    adapter = _mapping(config.get("adapter", {}), "AIME adapter")
    forbidden = sorted(
        {"agent_name", "agent_kwargs", "credential_value"} & set(adapter)
    )
    if forbidden:
        raise ValueError(
            "AIME adapter fields moved to the shared inner-agent/credential-name "
            "contract: " + ", ".join(forbidden)
        )
    credential_name = adapter.get("credential_env_name")
    if credential_name is not None and (
        not isinstance(credential_name, str)
        or not credential_name.isidentifier()
        or credential_name.upper() != credential_name
    ):
        raise ValueError("AIME credential_env_name is invalid")
    return adapter


def _authority(config: Mapping[str, object]) -> tuple[CampaignFreeze, CampaignCellPlan]:
    raw_freeze = config.get("campaign_freeze")
    if not isinstance(raw_freeze, Mapping):
        raise ValueError("AIME config requires campaign_freeze")
    cell_id = config.get("cell_id")
    if not isinstance(cell_id, str) or not cell_id.strip():
        raise ValueError("AIME config requires cell_id")
    freeze = CampaignFreeze.from_mapping(raw_freeze)
    plan = runtime_campaign_plan_from_freeze(
        freeze,
        cell_id=cell_id,
        benchmark_id=_BENCHMARK_ID,
    )
    return freeze, plan


def _field_material(
    task_root: Path,
    *,
    task_id: str,
    field: str,
) -> CanonicalDevelopmentMaterialRef:
    if field == "task_instruction":
        paths = (task_root / "instruction.md",)
    else:
        directory = (
            task_root
            / {
                "task_local_tests": "tests",
                "solution_or_gold": "solution",
            }[field]
        )
        paths = tuple(path for path in sorted(directory.rglob("*")) if path.is_file())
    if not paths or any(not path.is_file() for path in paths):
        raise RunValidationError("AIME development material is unavailable")
    records = [
        {
            "path": path.relative_to(task_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }
        for path in paths
    ]
    return CanonicalDevelopmentMaterialRef(
        task_id=task_id,
        field=field,
        status="present",
        content_ref=f"bench-snapshot://aime/{task_id}/{field}",
        content_digest="sha256:" + canonical_digest(records),
        file_count=len(paths),
        total_bytes=sum(path.stat().st_size for path in paths),
        source_paths=paths,
    )


def _direct_development_corpus(
    plan: CampaignCellPlan,
    tasks_root: Path,
    task_ids: tuple[str, ...],
) -> CanonicalDevelopmentCorpusHandle:
    fields = ("task_instruction", "task_local_tests", "solution_or_gold")
    materials = tuple(
        _field_material(tasks_root / task_id, task_id=task_id, field=field)
        for task_id in task_ids
        for field in fields
    )
    content = {
        "benchmark": _BENCHMARK_ID,
        "task_ids": list(task_ids),
        "materials": [material.content_digest for material in materials],
    }
    digest = "sha256:" + canonical_digest(content)
    return CanonicalDevelopmentCorpusHandle(
        benchmark=_BENCHMARK_ID,
        task_ids=task_ids,
        canonical_fields=fields,
        materials=materials,
        corpus_manifest_ref="experiment.json#benchmarks[name=aime].resolved.adapter",
        corpus_manifest_digest=digest,
        corpus_content_digest=digest,
        bench_evidence_spec_ref="experiment.json#benchmarks[name=aime].resolved.adapter",
        bench_evidence_spec_digest=digest,
        bench_identity_digest=digest,
        split_digest=plan.freeze_id,
        task_snapshot_digest=digest,
        content_storage="run-owned-aime-task-material",
        source_provenance_status=plan.source_reference,
    )


def _cell_budget(config: Mapping[str, object]) -> CellBudget:
    raw = config.get("cell_budget", {})
    if not isinstance(raw, Mapping):
        raise ValueError("AIME cell_budget must be a JSON object")
    return CellBudget(
        wall_seconds=raw.get("wall_seconds", CellBudget().wall_seconds),  # type: ignore[arg-type]
        max_cost_usd=raw.get("max_cost_usd", CellBudget().max_cost_usd),  # type: ignore[arg-type]
    )


@dataclass(frozen=True)
class FrozenAimeBenchmark:
    freeze: CampaignFreeze
    plan: CampaignCellPlan
    config: Mapping[str, object]
    control_root: Path
    inner_agent_selection: FrozenInnerAgentSelection

    benchmark_id = _BENCHMARK_ID

    @property
    def resolved_identity(self) -> Mapping[str, object]:
        payload = {
            "schema": _IDENTITY_SCHEMA,
            "benchmark_id": self.benchmark_id,
            "campaign_freeze": self.freeze.as_dict(),
            "cell_id": self.plan.cell_id,
            "config_digest": "sha256:"
            + canonical_digest(
                {
                    "config": dict(self.config),
                    "inner_agent_selection": dict(self.inner_agent_selection.identity),
                }
            ),
            "inner_agent_selection": dict(self.inner_agent_selection.identity),
        }
        return MappingProxyType(payload)

    def integration_factory(
        self,
        *,
        trusted_root: Path,
        scope: ScopeSpec,
        layout: RunLayout,
        gateway: TrustedEvaluationGateway,
        development_concurrency: int,
    ) -> IntegrationFactory:
        adapter_config = _adapter_config(self.config)
        adapter_config["agent_ref"] = self.inner_agent_selection.ref
        adapter_config["agent_config"] = dict(self.inner_agent_selection.config)
        runtime_root = self.control_root / "benchmark-runtime" / self.benchmark_id
        adapter_config.setdefault("tasks_root", str(runtime_root / "tasks"))
        adapter_config.setdefault("artifacts_root", str(runtime_root / "artifacts"))
        adapter_config.update(
            {
                "include_task_ids": list(self.plan.development_task_ids),
                "materialize_selected_only": True,
            }
        )
        bench = create_adapter(**adapter_config)  # type: ignore[arg-type]
        campaign = AimeCampaignBenchmarkAdapter(
            bench,
            trusted_gateway=gateway,
            layout=layout,
            candidate_entrypoint=str(self.config.get("candidate_entrypoint", ".")),
            cell_budget=_cell_budget(self.config),
            provider_name=str(adapter_config.get("provider_name", "unknown")),
            model_name=str(adapter_config.get("model", "unknown")),
        )
        tasks_root = (
            Path(str(adapter_config["tasks_root"])).expanduser().resolve(strict=True)
        )
        development = AimeDevelopmentAdapter(
            bench=bench,
            plan=self.plan,
            development_task_ids=self.plan.development_task_ids,
            layout=layout,
            gateway=gateway,
            corpus=_direct_development_corpus(
                self.plan, tasks_root, self.plan.development_task_ids
            ),
            cell_budget=_cell_budget(self.config),
            provider_name=str(adapter_config.get("provider_name", "unknown")),
            model_name=str(adapter_config.get("model", "unknown")),
            task_concurrency=development_concurrency,
        )
        return create_aime_integration_factory(
            development_adapter=development,
            campaign_adapter=campaign,
            execution_backend=bench,
            trusted_root=trusted_root,
            scope=scope,
            resource_identity={
                "kind": "aime-benchmark",
                "bench_identity": bench.identity().digest,
                "campaign_freeze": self.freeze.freeze_id,
            },
        )

    def held_out_evaluation_adapter(
        self,
        *,
        layout: RunLayout,
        gateway: TrustedEvaluationGateway,
    ) -> tuple[AimeCampaignBenchmarkAdapter, tuple[object, ...]]:
        """Construct the evaluator only inside the held-out capability."""

        held_out_task_ids = self.held_out_task_ids_for_evaluator()
        adapter_config = _adapter_config(self.config)
        adapter_config["agent_ref"] = self.inner_agent_selection.ref
        adapter_config["agent_config"] = dict(self.inner_agent_selection.config)
        if not adapter_config.get("pre_rendered_material_digest"):
            adapter_config["tasks_root"] = str(layout.root / "task-authority")
            adapter_config["materialize_selected_only"] = True
        adapter_config.update(
            {
                "artifacts_root": str(layout.root / "results" / "aime-artifacts"),
                "include_task_ids": list(held_out_task_ids),
            }
        )
        bench = create_adapter(**adapter_config)  # type: ignore[arg-type]
        campaign = AimeCampaignBenchmarkAdapter(
            bench,
            trusted_gateway=gateway,
            layout=layout,
            candidate_entrypoint=str(self.config.get("candidate_entrypoint", ".")),
            cell_budget=_cell_budget(self.config),
            provider_name=str(adapter_config.get("provider_name", "unknown")),
            model_name=str(adapter_config.get("model", "unknown")),
        )
        tasks = {task.task_id: task for task in bench.list_tasks()}
        return campaign, tuple(tasks[task_id] for task_id in held_out_task_ids)

    def held_out_task_ids_for_evaluator(self) -> tuple[str, ...]:
        """Resolve opaque held-out membership only inside evaluator-owned storage."""

        adapter_config = _adapter_config(self.config)
        adapter_config["agent_ref"] = self.inner_agent_selection.ref
        adapter_config["agent_config"] = dict(self.inner_agent_selection.config)
        evaluator_root = (
            self.control_root / "held-out-evaluator-catalog" / self.benchmark_id
        )
        if not adapter_config.get("pre_rendered_material_digest"):
            adapter_config["tasks_root"] = str(evaluator_root / "tasks")
            adapter_config["materialize_selected_only"] = False
        adapter_config.update(
            {
                "artifacts_root": str(evaluator_root / "artifacts"),
                "include_task_ids": [],
                "exclude_task_ids": [],
                "max_tasks": 0,
            }
        )
        bench = create_adapter(**adapter_config)  # type: ignore[arg-type]
        development = set(self.plan.development_task_ids)
        held_out = tuple(
            task.task_id
            for task in bench.list_tasks()
            if task.task_id not in development
        )
        return resolve_held_out_task_ids_for_evaluator(
            self.plan.dataset_boundary.held_out_set, held_out
        )


def freeze_aime_benchmark(
    config: Mapping[str, object],
    *,
    control_root: Path,
    inner_agent_selection: FrozenInnerAgentSelection,
) -> FrozenAimeBenchmark:
    copied = _mapping(config, "AIME config")
    _reject_development_policy(copied)
    adapter = _adapter_config(copied)
    if (
        inner_agent_selection.ref != "builtin:terminus-2"
        and not inner_agent_selection.is_python
    ):
        raise ValueError("AIME inner-agent selection is unsupported")
    if (
        inner_agent_selection.is_python
        and adapter.get("physical_isolation") is not True
    ):
        raise ValueError("AIME custom Python inner agents require physical_isolation")
    freeze, plan = _authority(copied)
    return FrozenAimeBenchmark(
        freeze,
        plan,
        MappingProxyType(copied),
        control_root,
        inner_agent_selection,
    )


def resume_aime_benchmark(
    config: Mapping[str, object],
    resolved_identity: Mapping[str, object],
    *,
    control_root: Path,
    inner_agent_selection: FrozenInnerAgentSelection,
) -> FrozenAimeBenchmark:
    package = freeze_aime_benchmark(
        config,
        control_root=control_root,
        inner_agent_selection=inner_agent_selection,
    )
    if dict(package.resolved_identity) != dict(resolved_identity):
        raise RunValidationError("stored AIME benchmark identity changed")
    return package


__all__ = [
    "FrozenAimeBenchmark",
    "freeze_aime_benchmark",
    "resume_aime_benchmark",
]
