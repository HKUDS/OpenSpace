"""Terminal-Bench 2.1 assembly for the shared Harbor adapter."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from rsi_arena.benchmarks.harbor_runtime.adapter import (
    HarborBench,
    PhysicalVerifierRuntime,
)
from rsi_arena.benchmarks.harbor_runtime.materials import (
    MaterialBoundaryError,
    resolve_task_dir,
    snapshot_task_materials,
)
from rsi_arena.benchmarks.harbor_runtime.verifier_settings import VerifierSettings
from rsi_arena.benchmarks.task_records import restrict_tasks
from rsi_arena.contracts import BenchIdentity, TaskRef, canonical_digest

from .harbor_backend import admit_offline_verifier, offline_verifier_launch_binding
from .evidence import DevelopmentEpisode, DevelopmentPack, RSIEvidenceRequest
from .offline_verifier import (
    OFFLINE_VERIFIER_IMPORT_PATH,
    OfflineClosureAuthority,
    load_offline_closure_authority,
    offline_verifier_launch_kwargs,
)
from .tasks import (
    RUN_OWNED_TB21_MANIFEST,
    load_run_owned_tb21_snapshot,
    load_tb21_task_snapshot,
    materialize_tb21_tasks,
    resolve_tb2_task_timeout_authority,
)


class _CharBudget:
    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def take(self, value: str, field_cap: int) -> str:
        if self.remaining <= 0 or not value:
            return ""
        limit = min(self.remaining, field_cap)
        result = (
            value
            if len(value) <= limit
            else value[:limit]
            if limit <= 24
            else value[: limit - 24] + "\n...[bounded/truncated]"
        )
        self.remaining -= len(result)
        return result


def _read_bounded(path: Path, limit: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            value = stream.read(limit + 1)
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return ""
    if len(value) <= limit:
        return value
    return (
        value[:limit]
        if limit <= 24
        else value[: limit - 24] + "\n...[bounded/truncated]"
    )


def _render_material_tree(root: Path, limit: int) -> str:
    if not root.is_dir() or limit <= 0:
        return ""
    chunks: list[str] = []
    remaining = limit
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.is_symlink():
            continue
        header = f"\n### {path.relative_to(root).as_posix()}\n"
        if len(header) >= remaining:
            break
        chunks.append(header)
        remaining -= len(header)
        body = _read_bounded(path, remaining)
        chunks.append(body)
        remaining -= len(body)
        if remaining <= 0:
            break
    return "".join(chunks).strip()


class TB2Bench(HarborBench):
    def __init__(
        self,
        *,
        development_field_max_chars: int = 12_000,
        development_pack_max_chars: int = 72_000,
        **kwargs: object,
    ) -> None:
        for name, value in (
            ("development_field_max_chars", development_field_max_chars),
            ("development_pack_max_chars", development_pack_max_chars),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if development_pack_max_chars < development_field_max_chars:
            raise ValueError(
                "development_pack_max_chars must cover one development field"
            )
        super().__init__(benchmark_id="tb2", **kwargs)
        self._development_field_max_chars = development_field_max_chars
        self._development_pack_max_chars = development_pack_max_chars

    def identity(self) -> BenchIdentity:
        agent_digest = canonical_digest(
            {
                **self._execution_identity_payload(),
                "pass_reward_threshold": self._pass_reward_threshold,
            }
        )
        return BenchIdentity(
            name="terminal-bench-2.1",
            judge_identity=(
                "tb21-container-verifier@"
                + canonical_digest(
                    {
                        "task_source": self._tasks_pin,
                        "task_list": self._tasks_digest,
                        "execution": agent_digest,
                    }
                )
            ),
        )

    def build_development_pack(
        self, tasks: tuple[TaskRef, ...], request: RSIEvidenceRequest
    ) -> DevelopmentPack:
        """Return bounded, integrity-checked material for visible TB2 tasks."""

        if (
            not tasks
            or any(not isinstance(task, TaskRef) for task in tasks)
            or len({task.task_id for task in tasks}) != len(tasks)
        ):
            raise ValueError("development tasks must be non-empty and unique")
        if not isinstance(request, RSIEvidenceRequest):
            raise TypeError("request must be an RSIEvidenceRequest")
        budget = _CharBudget(self._development_pack_max_chars)
        episodes: list[DevelopmentEpisode] = []
        manifest_ids: list[str] = []
        for task in tasks:
            task_dir = resolve_task_dir(self._tasks_root, task.task_id)
            manifest = self.evaluator_material_manifest(task)
            if snapshot_task_materials(task_dir) != manifest:
                raise MaterialBoundaryError(
                    "development task material moved after adapter creation"
                )
            cap = self._development_field_max_chars
            episodes.append(
                DevelopmentEpisode(
                    task_id=task.task_id,
                    task_statement=budget.take(
                        _read_bounded(task_dir / "instruction.md", cap), cap
                    ),
                    public_checks=budget.take(
                        _render_material_tree(task_dir / "tests", cap), cap
                    ),
                    expected_behavior=budget.take(
                        "Pass the task's trusted verifier while preserving the "
                        "requested environment behavior.",
                        cap,
                    ),
                    reference_solution=budget.take(
                        _render_material_tree(task_dir / "solution", cap), cap
                    ),
                )
            )
            manifest_ids.append(manifest.evaluator_digest)
        return DevelopmentPack(
            pack_id=canonical_digest(
                {
                    "kind": "tb21-harbor-development",
                    "experiment": request.experiment_id,
                    "tasks": [task.task_id for task in tasks],
                    "manifests": manifest_ids,
                    "field_max_chars": self._development_field_max_chars,
                    "pack_max_chars": self._development_pack_max_chars,
                }
            ),
            instructions=(
                "Visible development material. Trusted checks and reference "
                "solutions are available only for the frozen development set."
            ),
            episodes=tuple(episodes),
            artifact_refs=tuple(
                canonical_digest({"development_material": digest})
                for digest in manifest_ids
            ),
        )


def create_adapter(
    *,
    tasks_root: str,
    verifier_settings: Mapping[str, object],
    artifacts_root: str = "tb2-artifacts",
    levels: list[str] = (),
    include_task_ids: list[str] = (),
    exclude_task_ids: list[str] = (),
    max_tasks: int = 0,
    subset_seed: int = 0,
    model: str = "openrouter/qwen/qwen3.7-plus",
    agent_ref: str,
    agent_config: Mapping[str, object],
    reasoning_effort: str | None = None,
    reasoning_enabled: bool | None = None,
    temperature: float | None = None,
    env_passthrough: list[str] = (),
    docker_host: str = "",
    harbor_max_retries: int = 0,
    worker_timeout_seconds: float = 14_400,
    cleanup_grace_seconds: float = 30.0,
    uv_path: str = "uv",
    pass_reward_threshold: float = 1.0,
    development_field_max_chars: int = 12_000,
    development_pack_max_chars: int = 72_000,
    physical_isolation: bool = False,
    trusted_harbor_root: str = "",
    bridge_bwrap_path: str = "bwrap",
    artifact_codec: str = "",
    artifact_source: str = "",
    artifact_payload_name: str = "",
    offline_verifier_shared_root: str = "",
    offline_verifier_authority_path: str = "",
    provider_name: str | None = None,
    provider_base_url: str | None = None,
    provider_base_url_env_name: str | None = None,
    credential_env_name: str | None = None,
    verifier_env: Mapping[str, str] | None = None,
) -> TB2Bench:
    source_locator = Path(tasks_root).expanduser()
    repository_locator = (
        source_locator.parent if source_locator.name == "tasks" else source_locator
    )
    repository_root = repository_locator.resolve(strict=True)
    snapshot = (
        load_run_owned_tb21_snapshot(repository_root)
        if (repository_root / RUN_OWNED_TB21_MANIFEST).is_file()
        else load_tb21_task_snapshot(repository_root)
    )
    if not physical_isolation and shutil.which(uv_path) is None:
        raise OSError(f"uv not found at {uv_path!r} (required to sync checkouts)")
    tasks = restrict_tasks(
        list(snapshot.tasks),
        levels=tuple(levels),
        include_task_ids=tuple(include_task_ids),
        exclude_task_ids=tuple(exclude_task_ids),
        max_tasks=max_tasks,
        seed=subset_seed,
    )
    if not tasks:
        raise ValueError("restricted TB2.1 subset is empty; loosen the filters")
    artifact_root = Path(artifacts_root).expanduser().resolve()
    offline_root: Path | None = None
    offline_authority: OfflineClosureAuthority | None = None
    physical_verifier: PhysicalVerifierRuntime | None = None
    if physical_isolation:
        if not offline_verifier_shared_root:
            raise ValueError(
                "physical TB2 requires an explicit offline_verifier_shared_root"
            )
        offline_root = Path(offline_verifier_shared_root).expanduser().resolve()
        if not offline_verifier_authority_path:
            raise ValueError(
                "physical TB2 requires an explicit offline verifier authority path"
            )
        offline_authority = load_offline_closure_authority(
            snapshot,
            authority_path=Path(offline_verifier_authority_path).expanduser(),
        )
        assert offline_root is not None

        def launch_kwargs(**kwargs: object) -> Mapping[str, object]:
            assert offline_authority is not None
            return offline_verifier_launch_kwargs(
                authority=offline_authority,
                source_commit=snapshot.commit,
                shared_run_root=offline_root,
                **kwargs,
            )

        physical_verifier = PhysicalVerifierRuntime(
            import_path=OFFLINE_VERIFIER_IMPORT_PATH,
            source_commit=offline_authority.source_commit,
            tasks_tree=offline_authority.tasks_tree,
            task_ids=frozenset(offline_authority.tasks),
            resolve_timeout=resolve_tb2_task_timeout_authority,
            launch_kwargs=launch_kwargs,
            launch_binding=offline_verifier_launch_binding,
            admit=admit_offline_verifier,
        )
    materialization_parent = artifact_root / ".tb21-task-material"
    materialization_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    materialization_owner = Path(
        tempfile.mkdtemp(
            prefix=f"{snapshot.commit[:12]}-",
            dir=materialization_parent,
        )
    )
    try:
        materialized_tasks_root = materialize_tb21_tasks(
            snapshot,
            materialization_owner / "tasks",
            tuple(str(task["task_id"]) for task in tasks),
        )
        bench = TB2Bench(
            development_field_max_chars=development_field_max_chars,
            development_pack_max_chars=development_pack_max_chars,
            tasks=tasks,
            tasks_root=materialized_tasks_root,
            artifacts_root=artifact_root,
            model=model,
            agent_ref=agent_ref,
            agent_config=agent_config,
            reasoning_effort=reasoning_effort,
            reasoning_enabled=reasoning_enabled,
            temperature=temperature,
            env_passthrough=tuple(env_passthrough),
            docker_host=docker_host,
            harbor_max_retries=harbor_max_retries,
            worker_timeout_seconds=worker_timeout_seconds,
            cleanup_grace_seconds=cleanup_grace_seconds,
            uv_path=uv_path,
            pass_reward_threshold=pass_reward_threshold,
            physical_isolation=physical_isolation,
            trusted_harbor_root=(
                Path(os.path.expanduser(trusted_harbor_root))
                if trusted_harbor_root
                else None
            ),
            bridge_bwrap_path=bridge_bwrap_path,
            artifact_codec=artifact_codec or None,
            artifact_source=artifact_source or None,
            artifact_payload_name=artifact_payload_name or None,
            provider_name=provider_name,
            provider_base_url=provider_base_url,
            provider_base_url_env_name=provider_base_url_env_name,
            credential_env_name=credential_env_name,
            verifier_env=verifier_env,
            verifier_settings=VerifierSettings.from_mapping(verifier_settings),
            task_source_identity=snapshot.identity,
            physical_verifier=physical_verifier,
        )
    except BaseException:
        shutil.rmtree(materialization_owner, ignore_errors=True)
        raise
    bench._owned_task_material_root = materialization_owner
    return bench


__all__ = ["create_adapter"]
