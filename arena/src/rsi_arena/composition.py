"""Kernel composition root for one generic command Arena run."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol

from rsi_arena.campaign_evaluation import campaign_evaluator_digest
from rsi_arena.common import UsageLedger, snapshot_worktree
from rsi_arena.contracts import ScopeSpec
from rsi_arena.evaluation_gateway import TrustedEvaluationGateway
from rsi_arena.manifests import CampaignFreeze
from rsi_arena.run_layout import RunLayout, RunValidationError
from rsi_arena.runtime_campaign import CampaignCellPlan
from rsi_arena.runtime_layout import create_multi_runtime_campaign_layout

from .agentic import ArenaAgenticRunner, TrustedAttemptBoundary
from .experiment import EXPERIMENT_FILENAME, JSONObject
from .integration import (
    BenchmarkIntegration,
    IntegrationFactory,
    TrustedResolvedCampaignConfig,
)
from .manifests import SampleSlot
from .service import ArenaService
from .session_spine import SessionSpine

_AUTHORITY_SCHEMA = "rsi-arena-generic-command-authority"
_MANIFEST_EXPERIMENT_SCHEMA = "rsi-arena-experiment-binding"
_BENCHMARK_IDENTITY_FIELD = "adapter"


class BenchmarkConstructionError(ValueError):
    """The kernel cannot construct the requested benchmark adapter."""


class FrozenBenchmarkPackage(Protocol):
    """The narrow benchmark-owned authority consumed by this composition."""

    benchmark_id: str
    freeze: CampaignFreeze
    plan: CampaignCellPlan
    resolved_identity: Mapping[str, object]

    def integration_factory(
        self,
        *,
        trusted_root: Path,
        scope: ScopeSpec,
        layout: RunLayout,
        gateway: TrustedEvaluationGateway,
        development_concurrency: int,
    ) -> IntegrationFactory: ...


@dataclass
class GenericArenaComposition:
    """One plural spine, service projection, UDS runner, and frozen workspace."""

    control_root: Path
    layout: RunLayout
    usage_ledger: UsageLedger
    spine: SessionSpine
    service: ArenaService
    integrations: Mapping[str, BenchmarkIntegration]
    workspace: Path
    cwd: Path
    candidate_root: Path
    arena_runner: ArenaAgenticRunner
    benchmark_packages: Mapping[str, FrozenBenchmarkPackage]
    _started: bool = False
    _closed: bool = False

    def bind_experiment(self, digest: str) -> None:
        """Bind the canonical experiment to the existing run authority once."""

        path = self.layout.root / "run-manifest.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunValidationError("generic run manifest is unavailable") from exc
        binding = {
            "schema": _MANIFEST_EXPERIMENT_SCHEMA,
            "path": str(self.control_root / EXPERIMENT_FILENAME),
            "sha256": digest,
            "workspace": str(self.workspace),
            "cwd": str(self.cwd),
            "candidate_root": str(self.candidate_root),
        }
        existing = manifest.get("arena_experiment")
        if existing not in (None, binding):
            raise RunValidationError("generic run experiment authority changed")
        if existing is None:
            manifest["arena_experiment"] = binding
            self.layout.write_manifest("run-manifest.json", manifest)

    def start_client(self) -> Mapping[str, str]:
        """Install and start the existing local UDS client/server boundary."""

        if self._closed:
            raise RuntimeError("generic Arena composition is closed")
        if self._started:
            raise RuntimeError("generic Arena client is already running")
        launch_root = (
            self.layout.method_root
            / "generic-command-launches"
            / f"launch-{uuid.uuid4().hex}"
        )
        bin_dir = launch_root / "bin"
        ipc_dir = launch_root / "ipc"
        bin_dir.mkdir(parents=True, mode=0o700)
        self.arena_runner.install(bin_dir, ipc_dir)
        self.arena_runner.start()
        self._started = True
        return self.arena_runner.client_environment

    @property
    def client_bin_dir(self) -> Path:
        command = self.arena_runner.installed_command
        if command is None:
            raise RuntimeError("generic Arena client is not installed")
        return command.parent

    def stop_client(self) -> None:
        if not self._started:
            return
        try:
            self.arena_runner.stop_admission()
        finally:
            self.arena_runner.stop()
            self._started = False

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.stop_client()
        finally:
            try:
                self.usage_ledger.close()
            finally:
                try:
                    self.spine.close()
                finally:
                    self._close_benchmark_packages()
        self._closed = True

    def _close_benchmark_packages(self) -> None:
        first_error: BaseException | None = None
        for package in reversed(tuple(self.benchmark_packages.values())):
            close = getattr(package, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


def compose_new_generic_run(
    resolved: JSONObject,
    benchmark_packages: Mapping[str, FrozenBenchmarkPackage],
    *,
    run_root: str | os.PathLike[str],
    workspace_source: str | os.PathLike[str],
    trusted_candidate_source: str | os.PathLike[str] | None = None,
) -> GenericArenaComposition:
    """Construct the supported adapter and freeze its new run-owned workspace."""

    control_root = Path(run_root).expanduser().absolute()
    control_root.mkdir(parents=True, exist_ok=True)
    control_root = control_root.resolve(strict=True)
    source = _source_workspace(workspace_source)
    if (
        source == control_root
        or source in control_root.parents
        or control_root in source.parents
    ):
        raise ValueError("workspace template and generic run root must be disjoint")
    packages = _ordered_benchmark_packages(resolved, benchmark_packages)
    _freeze_benchmark_identities(resolved, packages)
    plans = {benchmark_id: packages[benchmark_id].plan for benchmark_id in packages}
    first = next(iter(packages.values()))

    layout, _task_ids, _bindings = create_multi_runtime_campaign_layout(
        plans,
        runs_root=control_root / "run-tree",
        runtime_base=control_root / "runtime",
        campaign=first.plan.campaign_id,
        primary_evaluation_seed=0,
        evaluator_digest=None,
    )
    workspace = layout.method_root / "workspace"
    snapshot_worktree(source, workspace, allow_safe_symlinks=True)
    cwd, candidate_root = _command_paths(resolved, workspace)
    trusted_root = layout.trusted_runtime_root / "candidate-template"
    trusted_source = (
        candidate_root
        if trusted_candidate_source is None
        else _source_workspace(trusted_candidate_source)
    )
    trusted_source_tree: str | None = None
    if trusted_candidate_source is not None and (
        trusted_source == control_root
        or trusted_source in control_root.parents
        or control_root in trusted_source.parents
    ):
        raise ValueError("trusted candidate source and run root must be disjoint")
    if trusted_candidate_source is not None:
        _trusted_source_commit, trusted_source_tree = _standalone_trusted_git_identity(
            trusted_source
        )
    snapshot_worktree(
        trusted_source,
        trusted_root,
        allow_safe_symlinks=True,
    )
    _initialize_private_git_root(trusted_root)
    trusted_commit, trusted_tree = _private_git_identity(trusted_root)
    if trusted_source_tree is not None and trusted_tree != trusted_source_tree:
        raise RunValidationError(
            "trusted candidate snapshot differs from its clean source commit"
        )
    _freeze_run_authority(
        resolved,
        control_root=control_root,
        layout=layout,
        workspace=workspace,
        cwd=cwd,
        candidate_root=candidate_root,
        trusted_root=trusted_root,
        trusted_commit=trusted_commit,
        trusted_tree=trusted_tree,
    )
    return _compose_existing_owners(
        resolved,
        control_root=control_root,
        layout=layout,
        trusted_root=trusted_root,
        packages=packages,
        workspace=workspace,
        cwd=cwd,
        candidate_root=candidate_root,
        fresh=True,
    )


def compose_resumed_generic_run(
    resolved: JSONObject,
    benchmark_packages: Mapping[str, FrozenBenchmarkPackage],
    *,
    run_root: str | os.PathLike[str],
) -> GenericArenaComposition:
    """Restore the same run owners using only the stored experiment authority."""

    control_root = Path(run_root).expanduser().resolve(strict=True)
    authority = _stored_run_authority(resolved)
    if authority["control_root"] != str(control_root):
        raise RunValidationError("resume named another generic run root")
    layout_root = _stored_directory(authority["layout_root"], "layout root")
    runtime_root = _stored_directory(authority["runtime_root"], "runtime root")
    try:
        layout_root.relative_to(control_root)
        runtime_root.relative_to(control_root)
    except ValueError as exc:
        raise RunValidationError(
            "stored generic run paths escaped their authority"
        ) from exc
    layout = replace(RunLayout.open_existing(layout_root), runtime_root=runtime_root)
    if layout.run_id != authority["run_id"]:
        raise RunValidationError("stored generic run identity changed")
    workspace = _stored_directory(authority["workspace"], "workspace")
    if workspace != layout.method_root.resolve(strict=True) / "workspace":
        raise RunValidationError("stored generic workspace changed")
    cwd, candidate_root = _command_paths(resolved, workspace)
    if (
        str(cwd) != authority["cwd"]
        or str(candidate_root) != authority["candidate_root"]
    ):
        raise RunValidationError("stored generic command paths changed")

    packages = _ordered_benchmark_packages(resolved, benchmark_packages)
    trusted_root = _stored_directory(
        layout.trusted_runtime_root / "candidate-template", "trusted candidate root"
    )
    trusted_commit, trusted_tree = _private_git_identity(trusted_root)
    if (
        str(trusted_root) != authority["trusted_candidate_root"]
        or trusted_commit != authority["trusted_candidate_commit"]
        or trusted_tree != authority["trusted_candidate_tree"]
    ):
        raise RunValidationError("stored trusted candidate template changed")
    return _compose_existing_owners(
        resolved,
        control_root=control_root,
        layout=layout,
        trusted_root=trusted_root,
        packages=packages,
        workspace=workspace,
        cwd=cwd,
        candidate_root=candidate_root,
        fresh=False,
    )


def _freeze_benchmark_identities(
    resolved: JSONObject,
    packages: Mapping[str, FrozenBenchmarkPackage],
) -> None:
    raw_benchmarks = resolved["benchmarks"]
    assert isinstance(raw_benchmarks, list)
    for benchmark in raw_benchmarks:
        assert isinstance(benchmark, dict)
        benchmark_id = str(benchmark["name"])
        package = packages[benchmark_id]
        benchmark_resolved = benchmark["resolved"]
        assert isinstance(benchmark_resolved, dict)
        benchmark_resolved[_BENCHMARK_IDENTITY_FIELD] = dict(package.resolved_identity)


def _ordered_benchmark_packages(
    resolved: JSONObject,
    packages: Mapping[str, FrozenBenchmarkPackage],
) -> Mapping[str, FrozenBenchmarkPackage]:
    """Validate product-built packages and order them only from the cell freeze."""

    if not isinstance(packages, Mapping) or not packages:
        raise BenchmarkConstructionError(
            "benchmark packages must be a non-empty mapping"
        )
    raw_benchmarks = resolved.get("benchmarks")
    assert isinstance(raw_benchmarks, list)
    configured_names = tuple(str(child["name"]) for child in raw_benchmarks)
    if len(set(configured_names)) != len(configured_names):
        raise BenchmarkConstructionError("benchmark config repeats a benchmark name")
    if set(configured_names) != set(packages):
        raise BenchmarkConstructionError(
            "constructed benchmark packages do not match public config"
        )
    first = next(iter(packages.values()))
    freeze = first.freeze
    cell_id = first.plan.cell_id
    cell = freeze.cells.get(cell_id)
    if cell is None:
        raise RunValidationError("constructed benchmark package has no active cell")
    benchmark_ids = cell.benchmark_ids
    if set(packages) != set(benchmark_ids):
        raise BenchmarkConstructionError(
            "constructed benchmark IDs do not match active campaign cell"
        )
    ordered: dict[str, FrozenBenchmarkPackage] = {}
    for benchmark_id in benchmark_ids:
        package = packages[benchmark_id]
        if (
            package.benchmark_id != benchmark_id
            or package.freeze != freeze
            or package.plan.cell_id != cell_id
            or package.plan.benchmark != benchmark_id
        ):
            raise RunValidationError(
                "constructed benchmark packages changed campaign authority"
            )
        ordered[benchmark_id] = package
    return MappingProxyType(ordered)


def _compose_existing_owners(
    resolved: JSONObject,
    *,
    control_root: Path,
    layout: RunLayout,
    trusted_root: Path,
    packages: Mapping[str, FrozenBenchmarkPackage],
    workspace: Path,
    cwd: Path,
    candidate_root: Path,
    fresh: bool,
) -> GenericArenaComposition:
    scope = ScopeSpec(editable=("*",))
    gateway = TrustedEvaluationGateway(
        trusted_root=trusted_root,
        scope=scope,
        work_dir=layout.trusted_runtime_root / "gateway",
    )
    arena = resolved["arena"]
    assert isinstance(arena, dict)
    baseline = resolved["baseline"]
    assert isinstance(baseline, dict)
    baseline_config = baseline["config"]
    assert isinstance(baseline_config, dict)
    default_development_concurrency = baseline_config.get("development_concurrency", 1)
    if (
        isinstance(default_development_concurrency, bool)
        or not isinstance(default_development_concurrency, int)
        or default_development_concurrency < 1
    ):
        raise RunValidationError(
            "baseline.config.development_concurrency must be a positive integer"
        )
    raw_benchmarks = resolved["benchmarks"]
    assert isinstance(raw_benchmarks, list)
    configured_benchmarks = {
        str(benchmark["name"]): benchmark for benchmark in raw_benchmarks
    }
    development_concurrency: dict[str, int] = {}
    for benchmark_id in packages:
        benchmark = configured_benchmarks[benchmark_id]
        assert isinstance(benchmark, dict)
        benchmark_config = benchmark["config"]
        assert isinstance(benchmark_config, dict)
        concurrency = benchmark_config.get(
            "development_task_concurrency", default_development_concurrency
        )
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or concurrency < 1
        ):
            raise RunValidationError(
                f"benchmarks[{benchmark_id!r}].config."
                "development_task_concurrency must be a positive integer"
            )
        development_concurrency[benchmark_id] = concurrency
    benchmark_concurrency = arena.get("concurrency", 1)
    invalid_cell_retries = arena.get("invalid_cell_retries", 0)
    configs = {
        benchmark_id: TrustedResolvedCampaignConfig(
            campaign_freeze=package.freeze,
            plan=package.plan,
            attempt_cap=arena.get("attempt_cap", 1),  # type: ignore[arg-type]
            benchmark_concurrency=benchmark_concurrency,  # type: ignore[arg-type]
            invalid_cell_retries=invalid_cell_retries,  # type: ignore[arg-type]
        )
        for benchmark_id, package in packages.items()
    }
    integrations: dict[str, BenchmarkIntegration] = {}
    for benchmark_id, package in packages.items():
        integration_factory = package.integration_factory(
            trusted_root=trusted_root,
            scope=scope,
            layout=layout,
            gateway=gateway,
            development_concurrency=development_concurrency[benchmark_id],
        )
        integration = integration_factory(configs[benchmark_id])
        if not isinstance(integration, BenchmarkIntegration):
            raise TypeError("integration factory must return a BenchmarkIntegration")
        integrations[benchmark_id] = integration
    plans = {benchmark_id: packages[benchmark_id].plan for benchmark_id in packages}
    adapters = {
        benchmark_id: integrations[benchmark_id].campaign_adapter
        for benchmark_id in packages
    }
    evaluator_digest = campaign_evaluator_digest(
        plans,
        adapters,
    )
    layout.bind_evaluator(evaluator_digest)
    try:
        usage_ledger = UsageLedger(
            layout.root / "usage.jsonl",
            method=layout.method,
            run_id=layout.run_id,
        )
        spine_constructor = SessionSpine.open if fresh else SessionSpine.resume
        spine = spine_constructor(
            configs,
            integrations,
            layout=layout,
            gateway=gateway,
            development_concurrency={
                benchmark_id: development_concurrency[benchmark_id]
                for benchmark_id in packages
            },
            usage_ledger=usage_ledger,
            candidate_root=candidate_root,
        )
    except BaseException:
        if "usage_ledger" in locals():
            usage_ledger.close()
        raise
    try:
        service = ArenaService(
            layout=layout,
            developments=spine.developments,
            campaign_controller=spine,
            attempt_cap=spine.attempt_cap,
            candidate_root=candidate_root,
            development_evidence_roots={
                benchmark_id: spine.developments[benchmark_id].evidence_root
                for benchmark_id in spine.benchmark_ids
            },
            public_workspace=workspace,
        )
    except BaseException:
        try:
            usage_ledger.close()
        finally:
            spine.close()
        raise

    def trusted_slots(attempt_id: str) -> Mapping[str, tuple[SampleSlot, ...]]:
        return MappingProxyType(
            {
                benchmark_id: tuple(
                    SampleSlot(
                        sample_index=index,
                        task_id=task_id,
                        replicate=0,
                        measurement_seed=int.from_bytes(
                            hashlib.sha256(
                                f"{layout.run_id}\0{attempt_id}\0{benchmark_id}\0{task_id}".encode()
                            ).digest()[:8],
                            "big",
                        ),
                    )
                    for index, task_id in enumerate(
                        packages[benchmark_id].plan.development_task_ids
                    )
                )
                for benchmark_id in packages
            }
        )

    return GenericArenaComposition(
        control_root=control_root,
        layout=layout,
        usage_ledger=usage_ledger,
        spine=spine,
        service=service,
        integrations=MappingProxyType(integrations),
        workspace=workspace,
        cwd=cwd,
        candidate_root=candidate_root,
        arena_runner=ArenaAgenticRunner(
            service,
            request_service=TrustedAttemptBoundary(service, trusted_slots),
        ),
        benchmark_packages=packages,
    )


def _source_workspace(path: str | os.PathLike[str]) -> Path:
    try:
        workspace = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError("workspace template is unavailable") from exc
    if not workspace.is_dir():
        raise ValueError("workspace template must be a directory")
    return workspace


def _contained_directory(workspace: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a directory path")
    supplied = Path(value).expanduser()
    if not supplied.is_absolute():
        supplied = workspace / supplied
    try:
        resolved = supplied.resolve(strict=True)
        resolved.relative_to(workspace)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"{label} escaped or is absent from the authorized workspace"
        ) from exc
    if not resolved.is_dir():
        raise ValueError(f"{label} must be a directory")
    return resolved


def _command_paths(resolved: JSONObject, workspace: Path) -> tuple[Path, Path]:
    baseline = resolved["baseline"]
    assert isinstance(baseline, dict)
    return (
        _contained_directory(workspace, baseline["cwd"], "baseline.cwd"),
        _contained_directory(
            workspace, baseline["candidate_root"], "baseline.candidate_root"
        ),
    )


def _freeze_run_authority(
    resolved: JSONObject,
    *,
    control_root: Path,
    layout: RunLayout,
    workspace: Path,
    cwd: Path,
    candidate_root: Path,
    trusted_root: Path,
    trusted_commit: str,
    trusted_tree: str,
) -> None:
    arena = resolved["arena"]
    assert isinstance(arena, dict)
    arena["authority"] = {
        "schema": _AUTHORITY_SCHEMA,
        "control_root": str(control_root),
        "layout_root": str(layout.root),
        "runtime_root": str(layout.runtime_root),
        "run_id": layout.run_id,
        "workspace": str(workspace),
        "cwd": str(cwd),
        "candidate_root": str(candidate_root),
        "trusted_candidate_root": str(trusted_root),
        "trusted_candidate_commit": trusted_commit,
        "trusted_candidate_tree": trusted_tree,
    }


def _stored_run_authority(resolved: JSONObject) -> dict[str, str]:
    arena = resolved["arena"]
    assert isinstance(arena, dict)
    raw = arena.get("authority")
    fields = {
        "schema",
        "control_root",
        "layout_root",
        "runtime_root",
        "run_id",
        "workspace",
        "cwd",
        "candidate_root",
        "trusted_candidate_root",
        "trusted_candidate_commit",
        "trusted_candidate_tree",
    }
    if (
        not isinstance(raw, Mapping)
        or set(raw) != fields
        or raw.get("schema") != _AUTHORITY_SCHEMA
        or any(
            not isinstance(raw.get(field), str) or not raw.get(field)
            for field in fields
        )
    ):
        raise RunValidationError("stored generic run authority is invalid")
    return {field: str(raw[field]) for field in fields}


def _stored_directory(value: object, label: str) -> Path:
    try:
        path = Path(str(value)).resolve(strict=True)
    except OSError as exc:
        raise RunValidationError(f"stored {label} is unavailable") from exc
    if not path.is_dir():
        raise RunValidationError(f"stored {label} is not a directory")
    return path


def _initialize_private_git_root(root: Path) -> None:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "RSI Arena",
        "GIT_AUTHOR_EMAIL": "arena@example.invalid",
        "GIT_COMMITTER_NAME": "RSI Arena",
        "GIT_COMMITTER_EMAIL": "arena@example.invalid",
        "LC_ALL": "C",
    }
    for argv in (
        ("git", "init", "-q"),
        ("git", "add", "-A", "-f", "--", "."),
        ("git", "commit", "-q", "--allow-empty", "-m", "candidate template"),
    ):
        completed = subprocess.run(
            argv,
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError("could not construct the private candidate template")


def _private_git_identity(root: Path) -> tuple[str, str]:
    """Return an exact clean commit/tree identity for the private template."""

    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "LC_ALL": "C",
    }
    values: list[str] = []
    for arguments in (("rev-parse", "HEAD"), ("rev-parse", "HEAD^{tree}")):
        completed = subprocess.run(
            ("git", *arguments),
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        value = completed.stdout.strip().lower()
        if completed.returncode != 0 or len(value) not in {40, 64}:
            raise RunValidationError("trusted candidate Git identity is invalid")
        values.append(value)
    status = subprocess.run(
        ("git", "status", "--porcelain=v1", "--untracked-files=all", "--ignored"),
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0 or status.stdout:
        raise RunValidationError("trusted candidate template is not clean")
    return values[0], values[1]


def _standalone_trusted_git_identity(root: Path) -> tuple[str, str]:
    """Require an explicit trusted source to be one clean standalone Git root."""

    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "LC_ALL": "C",
    }
    completed = subprocess.run(
        ("git", "rev-parse", "--show-toplevel"),
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RunValidationError("trusted candidate source is not a Git repository")
    try:
        top = Path(completed.stdout.strip()).resolve(strict=True)
    except OSError as exc:
        raise RunValidationError("trusted candidate Git root is unavailable") from exc
    if top != root:
        raise RunValidationError("trusted candidate source must be its Git root")
    return _private_git_identity(root)


__all__ = [
    "BenchmarkConstructionError",
    "GenericArenaComposition",
    "compose_new_generic_run",
    "compose_resumed_generic_run",
]
