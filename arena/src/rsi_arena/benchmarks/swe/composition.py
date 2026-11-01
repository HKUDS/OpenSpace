"""SWE-bench Verified construction seam used by the kernel composition root."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from rsi_arena.benchmarks.harbor_runtime.materials import resolve_task_dir
from rsi_arena.contracts import ScopeSpec, TaskRef, canonical_digest
from rsi_arena.evaluation_gateway import TrustedEvaluationGateway
from rsi_arena.inner_agent_runtime import FrozenInnerAgentSelection
from rsi_arena.integration import IntegrationFactory
from rsi_arena.manifests import (
    CampaignFreeze,
    validate_dataset_boundary_for_evaluator,
)
from rsi_arena.run_layout import (
    RunLayout,
    RunValidationError,
    atomic_write_json,
    file_digest,
)
from rsi_arena.runtime_campaign import (
    CampaignCellPlan,
    runtime_campaign_plan_from_freeze,
)

from .campaign import (
    SweCampaignBenchmarkAdapter,
    prepare_swe_attempt_evaluation_tasks,
    prepare_swe_development_task,
    prepare_swe_held_out_tasks,
)
from .development import SweDevelopmentAdapter
from .harbor_backend import (
    HarborSweTaskCellRunner,
    _require_supported_swe_inner_agent_config,
)
from .integration import create_swe_integration_factory
from .runtime import HarborModelDelivery
from .tasks import _copy_regular_tree, load_swe_tasks, tree_digest

_BENCHMARK_ID = "swe"
_IDENTITY_SCHEMA = "rsi-arena-swe-snapshot"
_AUTHORITY_MANIFEST = "task-snapshot.json"


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
            "SWE development policy belongs to the method, not benchmark config: "
            + ", ".join(forbidden)
        )


def _authority(config: Mapping[str, object]) -> tuple[CampaignFreeze, CampaignCellPlan]:
    raw_freeze = config.get("campaign_freeze")
    if not isinstance(raw_freeze, Mapping):
        raise ValueError("SWE config requires campaign_freeze")
    cell_id = config.get("cell_id")
    if not isinstance(cell_id, str) or not cell_id.strip():
        raise ValueError("SWE config requires cell_id")
    freeze = CampaignFreeze.from_mapping(raw_freeze)
    plan = runtime_campaign_plan_from_freeze(
        freeze,
        cell_id=cell_id,
        benchmark_id=_BENCHMARK_ID,
    )
    return freeze, plan


def _model(config: Mapping[str, object]) -> HarborModelDelivery:
    model = _mapping(config.get("model", {}), "SWE model")
    return HarborModelDelivery.provider(
        model_name=str(model.get("model_name", "")),
        model_revision=str(model.get("model_revision", "")),
        api_base_url=str(model.get("api_base_url", "")),
        provider_name=str(model.get("provider_name", "")),
        credential_source_env=str(model.get("credential_source_env", "")),
        credential_delivery_env=str(model.get("credential_delivery_env", "")),
    )


@dataclass(frozen=True)
class FrozenSweBenchmark:
    freeze: CampaignFreeze
    plan: CampaignCellPlan
    config: Mapping[str, object]
    control_root: Path
    snapshot_root: Path
    authority_relative: str
    authority_digest: str
    task_tree_digest: str
    inner_agent_selection: FrozenInnerAgentSelection

    benchmark_id = _BENCHMARK_ID

    @property
    def resolved_identity(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "schema": _IDENTITY_SCHEMA,
                "benchmark_id": self.benchmark_id,
                "campaign_freeze": self.freeze.as_dict(),
                "cell_id": self.plan.cell_id,
                "config_digest": "sha256:"
                + canonical_digest(
                    {
                        "config": dict(self.config),
                        "inner_agent_selection": dict(
                            self.inner_agent_selection.identity
                        ),
                    }
                ),
                "inner_agent_selection": dict(self.inner_agent_selection.identity),
                "task_authority": {
                    "path": self.authority_relative,
                    "sha256": self.authority_digest,
                    "task_tree_digest": self.task_tree_digest,
                },
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
        backend = _mapping(self.config.get("backend", {}), "SWE backend")
        harbor_root_value = backend.get("trusted_harbor_root")
        if not isinstance(harbor_root_value, str) or not harbor_root_value:
            raise ValueError("SWE backend requires trusted_harbor_root")
        snapshot_root = self.snapshot_root
        model = _model(backend)
        runner = HarborSweTaskCellRunner(
            layout=layout,
            trusted_harbor_root=Path(harbor_root_value),
            docker_path=backend.get("docker_path", "docker"),  # type: ignore[arg-type]
            bwrap_path=backend.get("bwrap_path", "bwrap"),  # type: ignore[arg-type]
            agent_ref=self.inner_agent_selection.ref,
            agent_config=self.inner_agent_selection.config,
            model=model,
            worker_timeout_seconds=backend.get("worker_timeout_seconds", 1800),  # type: ignore[arg-type]
            candidate_harvest_grace_seconds=backend.get(  # type: ignore[arg-type]
                "candidate_harvest_grace_seconds", 60
            ),
            verifier_timeout_seconds=backend.get(  # type: ignore[arg-type]
                "verifier_timeout_seconds", 1800
            ),
            harbor_wall_timeout_seconds=backend.get(
                "harbor_wall_timeout_seconds", 8400
            ),  # type: ignore[arg-type]
            cleanup_grace_seconds=backend.get("cleanup_grace_seconds", 30),  # type: ignore[arg-type]
        )
        evaluation_specs = prepare_swe_attempt_evaluation_tasks(
            self.plan, snapshot_root=snapshot_root
        )
        development_specs = tuple(
            prepare_swe_development_task(
                self.plan, task_id, snapshot_root=snapshot_root
            )
            for task_id in self.plan.development_task_ids
        )
        for spec in evaluation_specs:
            runner.resolve_task_timeout_budget(spec)
        campaign = SweCampaignBenchmarkAdapter(runner, evaluation_specs)
        development = SweDevelopmentAdapter(
            plan=self.plan,
            development_task_ids=self.plan.development_task_ids,
            layout=layout,
            gateway=gateway,
            task_specs=development_specs,
            task_runner=runner,
            task_concurrency=development_concurrency,
        )
        return create_swe_integration_factory(
            development_adapter=development,
            campaign_adapter=campaign,
            execution_backend=runner,
            trusted_root=trusted_root,
            scope=scope,
            resource_identity={
                "kind": "swe-bench-verified",
                "task_snapshot": self.freeze.freeze_id,
                "task_authority_sha256": self.authority_digest,
                "model_delivery": model.delivery_identity(),
            },
        )

    def held_out_evaluation_adapter(
        self,
        *,
        layout: RunLayout,
        gateway: TrustedEvaluationGateway,
    ) -> tuple[SweCampaignBenchmarkAdapter, tuple[object, ...]]:
        """Copy frozen held-out tasks only into the trusted evaluator."""

        backend = _mapping(self.config.get("backend", {}), "SWE backend")
        harbor_root = backend.get("trusted_harbor_root")
        source_value = backend.get("snapshot_root")
        if not isinstance(harbor_root, str) or not harbor_root:
            raise ValueError("SWE backend requires trusted_harbor_root")
        if not isinstance(source_value, str) or not source_value:
            raise ValueError("SWE backend requires snapshot_root")
        source = Path(source_value).expanduser().resolve(strict=True)
        held_out_task_ids = self.held_out_task_ids_for_evaluator()
        tasks_root = layout.root / "task-authority" / "tasks"
        if not tasks_root.exists():
            for task_id in held_out_task_ids:
                _copy_regular_tree(
                    resolve_task_dir(source, task_id), tasks_root / task_id
                )
        if tree_digest(tasks_root, list(held_out_task_ids)) != tree_digest(
            source, list(held_out_task_ids)
        ):
            raise RunValidationError("stored SWE held-out task authority changed")
        runner = HarborSweTaskCellRunner(
            layout=layout,
            trusted_harbor_root=Path(harbor_root),
            docker_path=backend.get("docker_path", "docker"),  # type: ignore[arg-type]
            bwrap_path=backend.get("bwrap_path", "bwrap"),  # type: ignore[arg-type]
            agent_ref=self.inner_agent_selection.ref,
            agent_config=self.inner_agent_selection.config,
            model=_model(backend),
            worker_timeout_seconds=backend.get("worker_timeout_seconds", 1800),  # type: ignore[arg-type]
            candidate_harvest_grace_seconds=backend.get(  # type: ignore[arg-type]
                "candidate_harvest_grace_seconds", 60
            ),
            verifier_timeout_seconds=backend.get(  # type: ignore[arg-type]
                "verifier_timeout_seconds", 1800
            ),
            harbor_wall_timeout_seconds=backend.get(
                "harbor_wall_timeout_seconds", 8400
            ),  # type: ignore[arg-type]
            cleanup_grace_seconds=backend.get("cleanup_grace_seconds", 30),  # type: ignore[arg-type]
        )
        specs = prepare_swe_held_out_tasks(
            self.plan, held_out_task_ids, snapshot_root=tasks_root
        )
        for spec in specs:
            runner.resolve_task_timeout_budget(spec)
        adapter = SweCampaignBenchmarkAdapter(runner, specs)
        return adapter, tuple(
            TaskRef(task_id=task_id, benchmark_id=self.benchmark_id)
            for task_id in held_out_task_ids
        )

    def held_out_task_ids_for_evaluator(self) -> tuple[str, ...]:
        """Resolve opaque held-out membership from the trusted SWE snapshot."""

        backend = _mapping(self.config.get("backend", {}), "SWE backend")
        source_value = backend.get("snapshot_root")
        if not isinstance(source_value, str) or not source_value:
            raise ValueError("SWE backend requires snapshot_root")
        source = Path(source_value).expanduser().resolve(strict=True)
        rows = load_swe_tasks(source)
        catalog = tuple(
            TaskRef(
                task_id=str(row["task_id"]),
                cluster=f"{row['category']}/{row['level']}",
                benchmark_id=self.benchmark_id,
            )
            for row in rows
        )
        development = set(self.plan.development_task_ids)
        held_out = tuple(
            task.task_id for task in catalog if task.task_id not in development
        )
        validate_dataset_boundary_for_evaluator(
            self.plan.dataset_boundary,
            catalog,
            held_out_task_ids=held_out,
        )
        return tuple(sorted(held_out))


def freeze_swe_benchmark(
    config: Mapping[str, object],
    *,
    control_root: Path,
    inner_agent_selection: FrozenInnerAgentSelection,
) -> FrozenSweBenchmark:
    copied = _mapping(config, "SWE config")
    _reject_development_policy(copied)
    freeze, plan = _authority(copied)
    backend = _mapping(copied.get("backend", {}), "SWE backend")
    if (
        inner_agent_selection.ref
        not in {"builtin:mini-swe-agent", "builtin:terminus-2"}
        and not inner_agent_selection.is_python
    ):
        raise ValueError("SWE inner-agent selection is unsupported")
    _require_supported_swe_inner_agent_config(
        inner_agent_selection.ref, inner_agent_selection.config
    )
    _model(backend)
    source_value = backend.get("snapshot_root")
    if not isinstance(source_value, str) or not source_value:
        raise ValueError("SWE backend requires snapshot_root")
    source = Path(source_value).expanduser().resolve(strict=True)
    task_ids = plan.development_task_ids
    authority = control_root / "benchmark-runtime" / _BENCHMARK_ID / "task-authority"
    tasks_root = authority / "tasks"
    if authority.exists() or authority.is_symlink():
        raise ValueError("run-owned SWE task authority already exists")
    for task_id in task_ids:
        _copy_regular_tree(resolve_task_dir(source, task_id), tasks_root / task_id)
    digest = tree_digest(tasks_root, list(task_ids))
    atomic_write_json(
        authority / _AUTHORITY_MANIFEST,
        {
            "schema": "rsi-arena-run-owned-swe",
            "task_ids": list(task_ids),
            "task_tree_digest": digest,
        },
    )
    relative = authority.relative_to(control_root).as_posix()
    return FrozenSweBenchmark(
        freeze,
        plan,
        MappingProxyType(copied),
        control_root,
        tasks_root,
        relative,
        file_digest(authority / _AUTHORITY_MANIFEST),
        digest,
        inner_agent_selection,
    )


def resume_swe_benchmark(
    config: Mapping[str, object],
    resolved_identity: Mapping[str, object],
    *,
    control_root: Path,
    inner_agent_selection: FrozenInnerAgentSelection,
) -> FrozenSweBenchmark:
    copied = _mapping(config, "SWE config")
    _reject_development_policy(copied)
    if (
        inner_agent_selection.ref
        not in {"builtin:mini-swe-agent", "builtin:terminus-2"}
        and not inner_agent_selection.is_python
    ):
        raise ValueError("SWE inner-agent selection is unsupported")
    _require_supported_swe_inner_agent_config(
        inner_agent_selection.ref, inner_agent_selection.config
    )
    backend = _mapping(copied.get("backend", {}), "SWE backend")
    _model(backend)
    freeze, plan = _authority(copied)
    record = resolved_identity.get("task_authority")
    if not isinstance(record, Mapping):
        raise RunValidationError("stored SWE task authority is unavailable")
    relative = record.get("path")
    manifest_digest = record.get("sha256")
    if not isinstance(relative, str) or not isinstance(manifest_digest, str):
        raise RunValidationError("stored SWE task authority is invalid")
    authority = (control_root / relative).resolve(strict=True)
    try:
        authority.relative_to(control_root)
    except ValueError as exc:
        raise RunValidationError("stored SWE task authority escaped the run") from exc
    manifest_path = authority / _AUTHORITY_MANIFEST
    if file_digest(manifest_path) != manifest_digest:
        raise RunValidationError("stored SWE task authority changed")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunValidationError("stored SWE task authority is invalid") from exc
    task_ids = plan.development_task_ids
    tasks_root = authority / "tasks"
    try:
        current_tree_digest = tree_digest(tasks_root, list(task_ids))
    except (OSError, ValueError) as exc:
        raise RunValidationError("stored SWE task authority changed") from exc
    if manifest != {
        "schema": "rsi-arena-run-owned-swe",
        "task_ids": list(task_ids),
        "task_tree_digest": current_tree_digest,
    }:
        raise RunValidationError("stored SWE task authority changed")
    package = FrozenSweBenchmark(
        freeze,
        plan,
        MappingProxyType(copied),
        control_root,
        tasks_root,
        relative,
        manifest_digest,
        current_tree_digest,
        inner_agent_selection,
    )
    if dict(package.resolved_identity) != dict(resolved_identity):
        raise RunValidationError("stored SWE benchmark identity changed")
    return package


__all__ = [
    "FrozenSweBenchmark",
    "freeze_swe_benchmark",
    "resume_swe_benchmark",
]
