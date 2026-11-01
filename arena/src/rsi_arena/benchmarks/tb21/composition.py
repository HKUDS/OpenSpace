"""Terminal-Bench 2.1 construction seam used by the kernel composition root."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from rsi_arena.benchmarks.harbor_runtime.verifier_settings import VerifierSettings
from rsi_arena.benchmarks.task_records import to_task_refs
from rsi_arena.contracts import CellBudget, ScopeSpec, canonical_digest
from rsi_arena.evaluation_gateway import TrustedEvaluationGateway
from rsi_arena.inner_agent_runtime import FrozenInnerAgentSelection
from rsi_arena.integration import IntegrationFactory
from rsi_arena.manifests import (
    CampaignFreeze,
    resolve_held_out_task_ids_for_evaluator,
)
from rsi_arena.run_layout import RunLayout, RunValidationError, file_digest
from rsi_arena.runtime_campaign import (
    CampaignCellPlan,
    runtime_campaign_plan_from_freeze,
)

from .adapter import create_adapter
from .campaign import TB2CampaignBenchmarkAdapter, bind_held_out_task_slots
from .development import TB2DevelopmentAdapter
from .harbor_backend import TB21HarborExecutionBackend
from .integration import create_tb2_integration_factory
from .offline_verifier import load_offline_closure_authority
from .tasks import (
    RUN_OWNED_TB21_MANIFEST,
    TB21SnapshotError,
    TB21TaskSnapshot,
    load_run_owned_tb21_snapshot,
    load_tb21_task_snapshot,
    materialize_run_owned_tb21_snapshot,
)

_BENCHMARK_ID = "tb2"
_IDENTITY_SCHEMA = "rsi-arena-tb21-snapshot"


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
            "TB2.1 development policy belongs to the method, not benchmark config: "
            + ", ".join(forbidden)
        )


def _authority(config: Mapping[str, object]) -> tuple[CampaignFreeze, CampaignCellPlan]:
    raw_freeze = config.get("campaign_freeze")
    if not isinstance(raw_freeze, Mapping):
        raise ValueError("TB2.1 config requires campaign_freeze")
    cell_id = config.get("cell_id")
    if not isinstance(cell_id, str) or not cell_id.strip():
        raise ValueError("TB2.1 config requires cell_id")
    freeze = CampaignFreeze.from_mapping(raw_freeze)
    plan = runtime_campaign_plan_from_freeze(
        freeze,
        cell_id=cell_id,
        benchmark_id=_BENCHMARK_ID,
    )
    return freeze, plan


def _cell_budget(config: Mapping[str, object]) -> CellBudget:
    raw = config.get("cell_budget", {})
    if not isinstance(raw, Mapping):
        raise ValueError("TB2.1 cell_budget must be a JSON object")
    return CellBudget(
        wall_seconds=raw.get("wall_seconds", CellBudget().wall_seconds),  # type: ignore[arg-type]
        max_cost_usd=raw.get("max_cost_usd", CellBudget().max_cost_usd),  # type: ignore[arg-type]
    )


def _credential_name(config: Mapping[str, object]) -> str:
    name = config.get("credential_env_name")
    if not isinstance(name, str) or not name:
        raise ValueError("TB2.1 backend requires credential_env_name")
    return name


@dataclass(frozen=True)
class _TB21ModelRoute:
    """TB2-owned sole model/provider route for evaluation and development."""

    model_name: str
    provider_name: str
    provider_base_url: str
    provider_base_url_env_name: str
    credential_env_name: str
    reasoning_effort: str | None

    @classmethod
    def from_backend(cls, backend: Mapping[str, object]) -> _TB21ModelRoute:
        required = (
            "model_name",
            "provider_name",
            "provider_base_url",
            "provider_base_url_env_name",
            "credential_env_name",
        )
        if not all(
            isinstance(backend.get(name), str) and backend.get(name)
            for name in required
        ):
            raise ValueError(
                "TB2.1 backend model route requires " + ", ".join(required)
            )
        credential_name = _credential_name(backend)
        if (
            not credential_name.isidentifier()
            or credential_name.upper() != credential_name
        ):
            raise ValueError("TB2.1 credential_env_name is invalid")
        base_env = str(backend["provider_base_url_env_name"])
        if (
            not base_env.isidentifier()
            or base_env.upper() != base_env
            or base_env == credential_name
        ):
            raise ValueError("TB2.1 provider base URL environment name is invalid")
        effort = backend.get("reasoning_effort")
        if effort is not None and (not isinstance(effort, str) or not effort.strip()):
            raise ValueError("TB2.1 reasoning_effort must be a non-empty string")
        return cls(
            model_name=str(backend["model_name"]),
            provider_name=str(backend["provider_name"]),
            provider_base_url=str(backend["provider_base_url"]),
            provider_base_url_env_name=base_env,
            credential_env_name=credential_name,
            reasoning_effort=str(effort) if effort is not None else None,
        )

    def usage_identity(self) -> dict[str, str]:
        return {
            "provider": self.provider_name,
            "model": self.model_name,
            "authority": "development_task_worker",
        }

    def campaign_kwargs(self) -> dict[str, object]:
        return {
            "model_name": self.model_name,
            "provider_name": self.provider_name,
            "provider_base_url": self.provider_base_url,
            "provider_base_url_env_name": self.provider_base_url_env_name,
            "credential_env_name": self.credential_env_name,
            "reasoning_effort": self.reasoning_effort,
        }

    def development_kwargs(self) -> dict[str, object]:
        return {
            "model": self.model_name,
            "provider_name": self.provider_name,
            "provider_base_url": self.provider_base_url,
            "provider_base_url_env_name": self.provider_base_url_env_name,
            "credential_env_name": self.credential_env_name,
            "reasoning_effort": self.reasoning_effort,
        }


_REMOVED_DEVELOPMENT_ROUTE_FIELDS = frozenset(
    {
        "model",
        "model_name",
        "provider_name",
        "provider_base_url",
        "provider_base_url_env_name",
        "credential_env_name",
        "credential_value",
        "reasoning_effort",
        "reasoning_enabled",
        "temperature",
        "agent_name",
        "agent_kwargs",
        "agent_ref",
        "agent_config",
        "env_passthrough",
    }
)


def _backend_config(config: Mapping[str, object]) -> dict[str, object]:
    backend = _mapping(config.get("backend", {}), "TB2.1 backend")
    if "credential_value" in backend:
        raise ValueError("TB2.1 backend accepts only credential_env_name")
    docker_host = backend.get("docker_host")
    if not isinstance(docker_host, str) or not docker_host.strip():
        raise ValueError("TB2.1 backend requires explicit docker_host")
    _TB21ModelRoute.from_backend(backend)
    return backend


def _development_config(config: Mapping[str, object]) -> dict[str, object]:
    development = _mapping(
        config.get("development_adapter", {}),
        "TB2.1 development adapter",
    )
    forbidden = sorted(_REMOVED_DEVELOPMENT_ROUTE_FIELDS & set(development))
    if forbidden:
        raise ValueError(
            "TB2.1 development route is derived from backend; remove: "
            + ", ".join(forbidden)
        )
    if "usage_identity" in config:
        raise ValueError("TB2.1 usage_identity is derived from backend")
    return development


@dataclass(frozen=True)
class FrozenTB21Benchmark:
    freeze: CampaignFreeze
    plan: CampaignCellPlan
    config: Mapping[str, object]
    control_root: Path
    snapshot: TB21TaskSnapshot
    authority_relative: str
    authority_digest: str
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
                    "snapshot": self.snapshot.identity,
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
        backend = _backend_config(self.config)
        route = _TB21ModelRoute.from_backend(backend)
        snapshot = self.snapshot
        verifier = VerifierSettings.from_mapping(
            _mapping(
                backend.get(
                    "verifier_settings",
                    {"timeout_multiplier": 1.0, "task_adaptations": {}},
                ),
                "TB2.1 verifier_settings",
            )
        )
        harbor_root = backend.get("trusted_harbor_root")
        if not isinstance(harbor_root, str) or not harbor_root:
            raise ValueError("TB2.1 backend requires trusted_harbor_root")
        offline_path = backend.get("offline_verifier_authority_path")
        if not isinstance(offline_path, str) or not offline_path:
            raise ValueError("TB2.1 backend requires offline_verifier_authority_path")
        offline_authority = load_offline_closure_authority(
            snapshot, authority_path=Path(offline_path).expanduser()
        )
        campaign_backend = TB21HarborExecutionBackend(
            layout=layout,
            snapshot=snapshot,
            trusted_harbor_root=Path(harbor_root),
            docker_path=backend.get("docker_path", "docker"),  # type: ignore[arg-type]
            docker_host=str(backend.get("docker_host", "")),
            bwrap_path=backend.get("bwrap_path", "bwrap"),  # type: ignore[arg-type]
            agent_ref=self.inner_agent_selection.ref,
            agent_config=self.inner_agent_selection.config,
            **route.campaign_kwargs(),  # type: ignore[arg-type]
            worker_timeout_seconds=backend.get("worker_timeout_seconds", 14_400),  # type: ignore[arg-type]
            # The cell value remains a post-hoc acceptance threshold; this
            # physical broker mapping only stops after settled provider cost.
            settled_cost_stop_usd=_cell_budget(self.config).max_cost_usd,
            harbor_wall_timeout_seconds=backend.get(
                "harbor_wall_timeout_seconds", 18_000
            ),  # type: ignore[arg-type]
            cleanup_grace_seconds=backend.get("cleanup_grace_seconds", 30),  # type: ignore[arg-type]
            verifier_settings=verifier,
            verifier_env=(
                _mapping(backend["verifier_env"], "TB2.1 verifier_env")
                if "verifier_env" in backend
                else None
            ),
            pass_reward_threshold=backend.get("pass_reward_threshold", 1.0),  # type: ignore[arg-type]
            offline_closure_authority=offline_authority,
            offline_verifier_shared_root=(
                layout.trusted_runtime_root / "tb21-harbor-offline-verifier"
            ),
        )
        campaign = TB2CampaignBenchmarkAdapter(snapshot, campaign_backend)

        development_runtime = (
            self.control_root / "benchmark-runtime" / self.benchmark_id
        )
        bench_config = _development_config(self.config)
        bench_config.setdefault("tasks_root", str(snapshot.repository_root))
        bench_config.setdefault(
            "artifacts_root", str(development_runtime / "development-artifacts")
        )
        bench_config.setdefault("verifier_settings", verifier.as_dict())
        bench_config["include_task_ids"] = list(self.plan.development_task_ids)
        bench_config.update(
            {
                **route.development_kwargs(),
                "agent_ref": self.inner_agent_selection.ref,
                "agent_config": dict(self.inner_agent_selection.config),
                "env_passthrough": [],
                "physical_isolation": True,
                "trusted_harbor_root": harbor_root,
                "bridge_bwrap_path": backend.get("bwrap_path", "bwrap"),
                "docker_host": backend.get("docker_host", ""),
                "worker_timeout_seconds": backend.get("worker_timeout_seconds", 14_400),
                "cleanup_grace_seconds": backend.get("cleanup_grace_seconds", 30),
                "offline_verifier_authority_path": offline_path,
                "offline_verifier_shared_root": str(
                    development_runtime / "offline-verifier"
                ),
            }
        )
        bench_config.setdefault("artifact_codec", "native_state")
        bench = create_adapter(**bench_config)  # type: ignore[arg-type]
        development = TB2DevelopmentAdapter(
            bench=bench,
            plan=self.plan,
            development_task_ids=self.plan.development_task_ids,
            layout=layout,
            gateway=gateway,
            cell_budget=_cell_budget(self.config),
            usage_identity=route.usage_identity(),
            worker_timeout_seconds=backend.get("worker_timeout_seconds", 14_400),  # type: ignore[arg-type]
            cleanup_grace_seconds=backend.get("cleanup_grace_seconds", 30),  # type: ignore[arg-type]
            task_concurrency=development_concurrency,
            verifier_settings=verifier,
        )
        return create_tb2_integration_factory(
            development_adapter=development,
            campaign_adapter=campaign,
            execution_backend=campaign_backend,
            trusted_root=trusted_root,
            scope=scope,
            resource_identity={
                "kind": "terminal-bench-2.1",
                "snapshot_identity": snapshot.identity,
                "task_authority_sha256": self.authority_digest,
                "campaign_freeze": self.freeze.freeze_id,
            },
        )

    def held_out_evaluation_adapter(
        self,
        *,
        layout: RunLayout,
        gateway: TrustedEvaluationGateway,
    ) -> tuple[TB2CampaignBenchmarkAdapter, tuple[object, ...]]:
        """Materialize frozen held-out tasks only inside the trusted run."""

        backend = _backend_config(self.config)
        route = _TB21ModelRoute.from_backend(backend)
        repository = backend.get("task_repository")
        if not isinstance(repository, str) or not repository:
            raise ValueError("TB2.1 backend requires task_repository")
        held_out_task_ids = self.held_out_task_ids_for_evaluator()
        authority = layout.root / "task-authority"
        if (authority / RUN_OWNED_TB21_MANIFEST).exists():
            snapshot = load_run_owned_tb21_snapshot(authority)
        else:
            snapshot = materialize_run_owned_tb21_snapshot(
                load_tb21_task_snapshot(repository), authority, held_out_task_ids
            )
        if tuple(task.task_id for task in snapshot.tasks) != held_out_task_ids:
            raise RunValidationError("TB2.1 held-out task authority changed")

        verifier = VerifierSettings.from_mapping(
            _mapping(
                backend.get(
                    "verifier_settings",
                    {"timeout_multiplier": 1.0, "task_adaptations": {}},
                ),
                "TB2.1 verifier_settings",
            )
        )
        harbor_root = backend.get("trusted_harbor_root")
        if not isinstance(harbor_root, str) or not harbor_root:
            raise ValueError("TB2.1 backend requires trusted_harbor_root")
        offline_path = backend.get("offline_verifier_authority_path")
        if not isinstance(offline_path, str) or not offline_path:
            raise ValueError("TB2.1 backend requires offline_verifier_authority_path")
        offline_authority = load_offline_closure_authority(
            snapshot, authority_path=Path(offline_path).expanduser()
        )
        execution = TB21HarborExecutionBackend(
            layout=layout,
            snapshot=snapshot,
            trusted_harbor_root=Path(harbor_root),
            docker_path=backend.get("docker_path", "docker"),  # type: ignore[arg-type]
            docker_host=str(backend.get("docker_host", "")),
            bwrap_path=backend.get("bwrap_path", "bwrap"),  # type: ignore[arg-type]
            agent_ref=self.inner_agent_selection.ref,
            agent_config=self.inner_agent_selection.config,
            **route.campaign_kwargs(),  # type: ignore[arg-type]
            worker_timeout_seconds=backend.get("worker_timeout_seconds", 14_400),  # type: ignore[arg-type]
            harbor_wall_timeout_seconds=backend.get(
                "harbor_wall_timeout_seconds", 18_000
            ),  # type: ignore[arg-type]
            cleanup_grace_seconds=backend.get("cleanup_grace_seconds", 30),  # type: ignore[arg-type]
            verifier_settings=verifier,
            verifier_env=(
                _mapping(backend["verifier_env"], "TB2.1 verifier_env")
                if "verifier_env" in backend
                else None
            ),
            pass_reward_threshold=backend.get("pass_reward_threshold", 1.0),  # type: ignore[arg-type]
            offline_closure_authority=offline_authority,
            offline_verifier_shared_root=(
                layout.trusted_runtime_root / "tb21-harbor-offline-verifier"
            ),
        )
        adapter = TB2CampaignBenchmarkAdapter(snapshot, execution)
        refs = {task.task_id: task for task in to_task_refs(snapshot.tasks)}
        return adapter, bind_held_out_task_slots(
            tuple(refs[task_id] for task_id in held_out_task_ids)
        )

    def held_out_task_ids_for_evaluator(self) -> tuple[str, ...]:
        """Resolve opaque held-out membership from the evaluator-side repository."""

        backend = _backend_config(self.config)
        repository = backend.get("task_repository")
        if not isinstance(repository, str) or not repository:
            raise ValueError("TB2.1 backend requires task_repository")
        source = load_tb21_task_snapshot(repository)
        development = set(self.plan.development_task_ids)
        held_out = tuple(
            task.task_id for task in source.tasks if task.task_id not in development
        )
        return resolve_held_out_task_ids_for_evaluator(
            self.plan.dataset_boundary.held_out_set, held_out
        )


def freeze_tb21_benchmark(
    config: Mapping[str, object],
    *,
    control_root: Path,
    inner_agent_selection: FrozenInnerAgentSelection,
) -> FrozenTB21Benchmark:
    copied = _mapping(config, "TB2.1 config")
    _reject_development_policy(copied)
    freeze, plan = _authority(copied)
    backend = _backend_config(copied)
    _development_config(copied)
    repository = backend.get("task_repository")
    if not isinstance(repository, str) or not repository:
        raise ValueError("TB2.1 backend requires task_repository")
    source = load_tb21_task_snapshot(repository)
    task_ids = plan.development_task_ids
    authority = control_root / "benchmark-runtime" / _BENCHMARK_ID / "task-authority"
    snapshot = materialize_run_owned_tb21_snapshot(source, authority, task_ids)
    relative = authority.relative_to(control_root).as_posix()
    digest = file_digest(authority / RUN_OWNED_TB21_MANIFEST)
    return FrozenTB21Benchmark(
        freeze,
        plan,
        MappingProxyType(copied),
        control_root,
        snapshot,
        relative,
        digest,
        inner_agent_selection,
    )


def resume_tb21_benchmark(
    config: Mapping[str, object],
    resolved_identity: Mapping[str, object],
    *,
    control_root: Path,
    inner_agent_selection: FrozenInnerAgentSelection,
) -> FrozenTB21Benchmark:
    copied = _mapping(config, "TB2.1 config")
    _reject_development_policy(copied)
    _backend_config(copied)
    _development_config(copied)
    freeze, plan = _authority(copied)
    authority_record = resolved_identity.get("task_authority")
    if not isinstance(authority_record, Mapping):
        raise RunValidationError("stored TB2.1 task authority is unavailable")
    relative = authority_record.get("path")
    digest = authority_record.get("sha256")
    if not isinstance(relative, str) or not isinstance(digest, str):
        raise RunValidationError("stored TB2.1 task authority is invalid")
    authority = (control_root / relative).resolve(strict=True)
    try:
        authority.relative_to(control_root)
    except ValueError as exc:
        raise RunValidationError("stored TB2.1 task authority escaped the run") from exc
    if file_digest(authority / RUN_OWNED_TB21_MANIFEST) != digest:
        raise RunValidationError("stored TB2.1 task authority changed")
    try:
        snapshot = load_run_owned_tb21_snapshot(authority)
    except TB21SnapshotError as exc:
        raise RunValidationError("stored TB2.1 task authority changed") from exc
    package = FrozenTB21Benchmark(
        freeze,
        plan,
        MappingProxyType(copied),
        control_root,
        snapshot,
        relative,
        digest,
        inner_agent_selection,
    )
    if dict(package.resolved_identity) != dict(resolved_identity):
        raise RunValidationError("stored TB2.1 benchmark identity changed")
    return package


__all__ = [
    "FrozenTB21Benchmark",
    "freeze_tb21_benchmark",
    "resume_tb21_benchmark",
]
