"""No-model operator tooling for a formal SWE-bench Verified campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from rsi_arena.benchmarks.harbor_runtime.execution import (
    HarborExecutionError,
    inspect_docker_image,
    resolve_immutable_docker_image,
)
from rsi_arena.contracts import TaskRef
from rsi_arena.experiment import load_experiment_toml, resolve_experiment
from rsi_arena.manifests import (
    CampaignConfig,
    CampaignFreeze,
    DatasetBoundary,
    freeze_development_set,
    freeze_held_out_set_for_evaluator,
    resolve_held_out_task_ids_for_evaluator,
)
from rsi_arena.runtime_campaign import runtime_campaign_plan_from_freeze

from .docker_runtime import (
    OfficialSweDockerError,
    OfficialSweTaskSource,
    inspect_official_swe_task,
    materialize_content_pinned_task_package,
)
from .harbor_backend import (
    SweHarborCellError,
    harbor_controller_runtime_identity,
    harbor_timeout_budget,
)
from .tasks import (
    DERIVATION_STAMP,
    DERIVED_TASK_TRANSFORM_IDENTITY,
    build_derived_task_tree,
    load_swe_tasks,
    tree_digest,
)

_BENCHMARK_ID = "swe"
_OFFICIAL_TASK_COUNT = 500
_POLICY_MARKER = "# SWE_DEVELOPMENT_POLICY"
_FREEZE_MARKER = "# SWE_CAMPAIGN_FREEZE"
_SNAPSHOT_PLACEHOLDER = "/trusted/swe-bench-verified/derived"
_HARBOR_PLACEHOLDER = "/trusted/harbor-7db020ba-runtime"
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class SweOperatorError(RuntimeError):
    """The local SWE production authority or runtime preflight is invalid."""


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _array(values: Sequence[str], *, indent: str = "  ") -> list[str]:
    return ["["] + [f"{indent}{_quote(value)}," for value in values] + ["]"]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _regular_tree_sha256(root: Path, label: str) -> str:
    try:
        resolved = root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise SweOperatorError(f"{label} is unavailable") from exc
    entries: list[dict[str, str]] = []
    for path in sorted(
        resolved.rglob("*"), key=lambda item: item.relative_to(resolved).as_posix()
    ):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SweOperatorError(f"{label} contains non-regular material")
        entries.append(
            {
                "path": path.relative_to(resolved).as_posix(),
                "sha256": _file_sha256(path),
            }
        )
    if not entries:
        raise SweOperatorError(f"{label} is empty")
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _regular_executable(value: str | os.PathLike[str], label: str) -> Path:
    locator = Path(value).expanduser()
    if not locator.is_absolute():
        found = shutil.which(str(locator))
        if found is None:
            raise SweOperatorError(f"{label} executable is unavailable")
        locator = Path(found)
    try:
        path = locator.resolve(strict=True)
        info = path.stat()
    except OSError as exc:
        raise SweOperatorError(f"{label} executable is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise SweOperatorError(f"{label} executable is unavailable")
    return path


def _snapshot_catalog(snapshot_root: Path) -> tuple[dict[str, object], ...]:
    root = snapshot_root.expanduser().resolve(strict=True)
    try:
        stamp = json.loads((root / DERIVATION_STAMP).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SweOperatorError("SWE snapshot lacks a valid derivation stamp") from exc
    try:
        rows = tuple(load_swe_tasks(root))
    except (OSError, ValueError) as exc:
        raise SweOperatorError("SWE snapshot task catalog is invalid") from exc
    task_ids = tuple(str(row["task_id"]) for row in rows)
    if len(rows) != _OFFICIAL_TASK_COUNT or len(set(task_ids)) != len(task_ids):
        raise SweOperatorError("formal SWE snapshot must contain exactly 500 tasks")
    if stamp.get("transform_identity") != DERIVED_TASK_TRANSFORM_IDENTITY:
        raise SweOperatorError("SWE snapshot uses an unsupported task transform")
    unexpected = tuple(
        child.name
        for child in root.iterdir()
        if child.name != DERIVATION_STAMP
        and (child.is_symlink() or not child.is_dir() or child.name not in task_ids)
    )
    if unexpected:
        raise SweOperatorError("SWE snapshot contains unexpected top-level entries")
    if stamp.get("task_ids") != sorted(task_ids):
        raise SweOperatorError("SWE derivation stamp and task catalog disagree")
    recorded_digest = stamp.get("content_digest")
    actual_digest = tree_digest(root, list(task_ids))
    if recorded_digest != actual_digest:
        raise SweOperatorError("SWE snapshot content changed after derivation")
    if any(row.get("agent_offline") is not True for row in rows):
        raise SweOperatorError("formal SWE tasks must make the agent phase offline")
    for task_id in task_ids:
        try:
            inspect_official_swe_task(root / task_id, expected_task_id=task_id)
        except (OSError, ValueError, OfficialSweDockerError) as exc:
            raise SweOperatorError(
                f"derived SWE task violates the official grader contract: {task_id}"
            ) from exc
    return rows


def _source_task_ids(source_root: Path) -> tuple[str, ...]:
    try:
        root = source_root.expanduser().resolve(strict=True)
        rows = tuple(load_swe_tasks(root))
    except (OSError, ValueError) as exc:
        raise SweOperatorError("official SWE source task catalog is invalid") from exc
    task_ids = tuple(str(row["task_id"]) for row in rows)
    if len(rows) != _OFFICIAL_TASK_COUNT or len(set(task_ids)) != len(task_ids):
        raise SweOperatorError("official SWE source must contain exactly 500 tasks")
    unexpected = tuple(
        child.name
        for child in root.iterdir()
        if child.is_symlink() or not child.is_dir() or child.name not in task_ids
    )
    if unexpected:
        raise SweOperatorError(
            "official SWE source contains unexpected top-level entries"
        )
    return tuple(sorted(task_ids))


def _task_map(rows: Sequence[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
    return {str(row["task_id"]): row for row in rows}


def _validate_development_selection(
    rows: Sequence[Mapping[str, object]],
    development_task_ids: Sequence[str],
    a_task_ids: Sequence[str],
    *,
    sample_size: int,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    catalog = _task_map(rows)
    development = tuple(development_task_ids)
    selected_a = tuple(a_task_ids)
    if len(development) < 2 or len(set(development)) != len(development):
        raise SweOperatorError(
            "SWE development selection must contain at least two unique tasks"
        )
    unknown_development = sorted(set(development) - set(catalog))
    if unknown_development:
        raise SweOperatorError(
            "SWE development selection contains unknown tasks: "
            + ", ".join(unknown_development)
        )
    if not selected_a or len(set(selected_a)) != len(selected_a):
        raise SweOperatorError("SWE A selection must contain unique tasks")
    if not set(selected_a).issubset(development):
        raise SweOperatorError("SWE A selection must be inside development membership")
    selected_b = tuple(task_id for task_id in development if task_id not in selected_a)
    if not selected_b:
        raise SweOperatorError("SWE A selection must leave development tasks for B")
    if sample_size < 1 or sample_size > len(selected_b):
        raise SweOperatorError("SWE B sample size must fit B membership")
    held_out = tuple(sorted(set(catalog) - set(development)))
    if not held_out:
        raise SweOperatorError("formal SWE selection must retain a held-out set")
    return development, selected_a, held_out


def _campaign_freeze(
    *,
    rows: Sequence[Mapping[str, object]],
    development_task_ids: Sequence[str],
    campaign_id: str,
    cell_id: str,
    method: str,
    source_reference: str,
    task_catalog_reference: str,
    held_out_identity: str,
) -> CampaignFreeze:
    catalog = _task_map(rows)
    development = tuple(
        TaskRef(
            task_id,
            cluster=f"{catalog[task_id]['category']}/{catalog[task_id]['level']}",
            benchmark_id=_BENCHMARK_ID,
        )
        for task_id in development_task_ids
    )
    held_out = tuple(sorted(set(catalog) - set(development_task_ids)))
    config = CampaignConfig.from_mapping(
        {
            "schema": "campaign-config",
            "campaign_id": campaign_id,
            "benchmarks": {
                _BENCHMARK_ID: {
                    "source_reference": source_reference,
                    "task_catalog_ref": task_catalog_reference,
                }
            },
            "cells": {
                cell_id: {
                    "method": method,
                    "benchmarks": [_BENCHMARK_ID],
                    "aggregate": {"kind": "macro_mean_pass_rate"},
                }
            },
        }
    )
    return CampaignFreeze.create(
        config,
        {
            _BENCHMARK_ID: DatasetBoundary(
                development_set=freeze_development_set(development),
                held_out_set=freeze_held_out_set_for_evaluator(
                    held_out_identity, held_out
                ),
            )
        },
    )


def _policy_toml(
    *,
    development_task_ids: Sequence[str],
    a_task_ids: Sequence[str],
    sample_size: int,
    sampling_seed: int,
) -> str:
    a_ids = tuple(a_task_ids)
    b_ids = tuple(task_id for task_id in development_task_ids if task_id not in a_ids)
    lines = [
        "[baseline.config.development_policy]",
        "",
        "[baseline.config.development_policy.benchmarks]",
        "",
        "[baseline.config.development_policy.benchmarks.swe]",
        "a_task_ids = " + _array(a_ids)[0],
        *_array(a_ids)[1:],
        "b_task_ids = " + _array(b_ids)[0],
        *_array(b_ids)[1:],
        "",
        "[baseline.config.development_policy.benchmarks.swe.sampling]",
        'kind = "random_subset"',
        f"sample_size = {sample_size}",
        f"seed = {sampling_seed}",
        "fixed_task_ids = []",
    ]
    return "\n".join(lines)


def _freeze_toml(freeze: CampaignFreeze, *, cell_id: str) -> str:
    boundary = freeze.dataset_boundaries[_BENCHMARK_ID]
    development = boundary.development_set
    held_out = boundary.held_out_set
    source = freeze.benchmarks[_BENCHMARK_ID]
    lines = [
        "[benchmarks.config.campaign_freeze]",
        'schema = "campaign-freeze"',
        f"campaign_id = {_quote(freeze.campaign_id)}",
        "held_out_sealed = true",
        f"freeze_id = {_quote(freeze.freeze_id)}",
        "",
        "[benchmarks.config.campaign_freeze.benchmarks]",
        "",
        "[benchmarks.config.campaign_freeze.benchmarks.swe]",
        f"source_reference = {_quote(source.source_reference)}",
        f"task_catalog_ref = {_quote(source.task_catalog_ref)}",
        "",
        "[benchmarks.config.campaign_freeze.cells]",
        "",
        f"[benchmarks.config.campaign_freeze.cells.{_quote(cell_id)}]",
        f"method = {_quote(freeze.cells[cell_id].method)}",
        'benchmarks = ["swe"]',
        'aggregate = { kind = "macro_mean_pass_rate" }',
        "",
        "[benchmarks.config.campaign_freeze.dataset_boundaries]",
        "",
        "[benchmarks.config.campaign_freeze.dataset_boundaries.swe]",
        'schema = "dataset-boundary"',
        "",
        "[benchmarks.config.campaign_freeze.dataset_boundaries.swe.development_set]",
        "ordered_task_ids = " + _array(development.ordered_task_ids)[0],
        *_array(development.ordered_task_ids)[1:],
        f"digest = {_quote(development.digest)}",
        "",
        "[benchmarks.config.campaign_freeze.dataset_boundaries.swe.development_set.task_clusters]",
        *(
            f"{_quote(task_id)} = {_quote(development.task_clusters[task_id])}"
            for task_id in development.ordered_task_ids
        ),
        "",
        "[benchmarks.config.campaign_freeze.dataset_boundaries.swe.held_out_set]",
        f"opaque_identity = {_quote(held_out.opaque_identity)}",
        f"digest = {_quote(held_out.digest)}",
        f"count = {held_out.count}",
    ]
    return "\n".join(lines)


def _load_toml_bytes(payload: bytes) -> Mapping[str, object]:
    import tomllib

    loaded = tomllib.loads(payload.decode("utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError("rendered SWE experiment is not a TOML table")
    return loaded


def _selected_swe_config(resolved: Mapping[str, object]) -> Mapping[str, object]:
    benchmarks = resolved.get("benchmarks")
    if not isinstance(benchmarks, list):
        raise SweOperatorError("experiment has no benchmark list")
    matches = [
        benchmark
        for benchmark in benchmarks
        if isinstance(benchmark, Mapping) and benchmark.get("name") == _BENCHMARK_ID
    ]
    if len(matches) != 1:
        raise SweOperatorError("experiment must contain exactly one SWE benchmark")
    config = matches[0].get("config")
    if not isinstance(config, Mapping):
        raise SweOperatorError("SWE benchmark config is missing")
    return config


def _validate_resolved(
    resolved: Mapping[str, object],
    rows: Sequence[Mapping[str, object]],
    *,
    snapshot_root: Path,
    require_runtime: bool = False,
) -> None:
    runtime = resolved.get("benchmark_runtime")
    inner = runtime.get("inner_agent") if isinstance(runtime, Mapping) else None
    if not isinstance(inner, Mapping) or inner.get("ref") != "builtin:terminus-2":
        raise SweOperatorError("formal SWE template requires builtin:terminus-2")
    config = _selected_swe_config(resolved)
    freeze_raw = config.get("campaign_freeze")
    cell_id = config.get("cell_id")
    backend = config.get("backend")
    if not isinstance(freeze_raw, Mapping) or not isinstance(cell_id, str):
        raise SweOperatorError("SWE campaign authority is missing")
    if not isinstance(backend, Mapping):
        raise SweOperatorError("SWE backend config is missing")
    configured_snapshot = backend.get("snapshot_root")
    if not isinstance(configured_snapshot, str):
        raise SweOperatorError("SWE snapshot_root is missing")
    try:
        configured_snapshot_path = (
            Path(configured_snapshot).expanduser().resolve(strict=True)
        )
    except OSError as exc:
        raise SweOperatorError("configured SWE snapshot is unavailable") from exc
    if configured_snapshot_path != snapshot_root:
        raise SweOperatorError("SWE snapshot config differs from validated data")
    freeze = CampaignFreeze.from_mapping(freeze_raw)
    plan = runtime_campaign_plan_from_freeze(
        freeze, cell_id=cell_id, benchmark_id=_BENCHMARK_ID
    )
    catalog_ids = tuple(str(row["task_id"]) for row in rows)
    development = plan.development_task_ids
    if not development or not set(development).issubset(catalog_ids):
        raise SweOperatorError("SWE development membership differs from the snapshot")
    held_out = tuple(sorted(set(catalog_ids) - set(development)))
    resolve_held_out_task_ids_for_evaluator(
        plan.dataset_boundary.held_out_set, held_out
    )
    if require_runtime:
        _validate_runtime_config(resolved, config, rows)


def _positive_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SweOperatorError(f"{label} must be positive")
    parsed = float(value)
    if parsed <= 0:
        raise SweOperatorError(f"{label} must be positive")
    return parsed


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SweOperatorError(f"{label} must be a positive integer")
    return value


def _exact_fields(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise SweOperatorError(f"{label} fields differ from the shipped contract")


def _validate_runtime_config(
    resolved: Mapping[str, object],
    config: Mapping[str, object],
    rows: Sequence[Mapping[str, object]],
) -> None:
    _exact_fields(
        config,
        {"backend", "campaign_freeze", "cell_id", "development_task_concurrency"},
        "SWE benchmark config",
    )
    backend = config["backend"]
    assert isinstance(backend, Mapping)
    _exact_fields(
        backend,
        {
            "bwrap_path",
            "candidate_harvest_grace_seconds",
            "cleanup_grace_seconds",
            "docker_path",
            "harbor_wall_timeout_seconds",
            "model",
            "snapshot_root",
            "trusted_harbor_root",
            "verifier_timeout_seconds",
            "worker_timeout_seconds",
        },
        "SWE backend",
    )
    model = backend["model"]
    if not isinstance(model, Mapping):
        raise SweOperatorError("SWE backend model config is missing")
    _exact_fields(
        model,
        {
            "api_base_url",
            "credential_delivery_env",
            "credential_source_env",
            "model_name",
            "model_revision",
            "provider_name",
        },
        "SWE backend model",
    )
    for field, value in model.items():
        if (
            not isinstance(value, str)
            or not value.strip()
            or value.startswith("REPLACE_")
        ):
            raise SweOperatorError(f"SWE backend model {field} is unresolved")
    source_env = str(model["credential_source_env"])
    delivery_env = str(model["credential_delivery_env"])
    if (
        _ENVIRONMENT_NAME.fullmatch(source_env) is None
        or _ENVIRONMENT_NAME.fullmatch(delivery_env) is None
    ):
        raise SweOperatorError("SWE provider credential environment name is invalid")
    if source_env not in os.environ or not os.environ[source_env]:
        raise SweOperatorError(f"SWE provider credential {source_env} is unavailable")
    harbor_root = Path(str(backend["trusted_harbor_root"])).expanduser()
    docker = _regular_executable(str(backend["docker_path"]), "Docker")
    try:
        harbor_controller_runtime_identity(harbor_root.resolve(strict=True), docker)
    except (OSError, SweHarborCellError) as exc:
        raise SweOperatorError("trusted Harbor checkout is unavailable") from exc
    _regular_executable(str(backend["bwrap_path"]), "bubblewrap")
    timeout_fields = {
        name: _positive_number(backend[name], f"SWE {name}")
        for name in (
            "worker_timeout_seconds",
            "candidate_harvest_grace_seconds",
            "verifier_timeout_seconds",
            "harbor_wall_timeout_seconds",
            "cleanup_grace_seconds",
        )
    }
    snapshot_root = (
        Path(str(backend["snapshot_root"])).expanduser().resolve(strict=True)
    )
    for row in rows:
        task_id = str(row["task_id"])
        harbor_timeout_budget(
            snapshot_root / task_id,
            worker_timeout_seconds=timeout_fields["worker_timeout_seconds"],
            candidate_harvest_grace_seconds=timeout_fields[
                "candidate_harvest_grace_seconds"
            ],
            verifier_timeout_seconds=timeout_fields["verifier_timeout_seconds"],
            harbor_wall_timeout_seconds=timeout_fields["harbor_wall_timeout_seconds"],
            cleanup_grace_seconds=timeout_fields["cleanup_grace_seconds"],
        )
    arena = resolved.get("arena")
    baseline = resolved.get("baseline")
    baseline_config = baseline.get("config") if isinstance(baseline, Mapping) else None
    if not isinstance(arena, Mapping) or not isinstance(baseline_config, Mapping):
        raise SweOperatorError("SWE formal runtime controls are missing")
    for field in ("attempt_cap", "concurrency"):
        _positive_integer(arena.get(field), f"arena.{field}")
    _positive_number(arena.get("wall_seconds"), "arena.wall_seconds")
    _positive_integer(
        config.get("development_task_concurrency"),
        "SWE development_task_concurrency",
    )
    _positive_integer(
        baseline_config.get("development_concurrency"),
        "baseline development_concurrency",
    )
    _positive_number(baseline_config.get("budget_usd"), "baseline budget_usd")
    outer_model = baseline_config.get("model")
    if (
        not isinstance(outer_model, str)
        or not outer_model.strip()
        or outer_model.startswith("REPLACE_")
    ):
        raise SweOperatorError("outer method model is unresolved")


def _prepare_snapshot(args: argparse.Namespace) -> None:
    source_root = Path(args.source_root).expanduser().resolve(strict=True)
    task_ids = _source_task_ids(source_root)
    prepared = build_derived_task_tree(source_root, args.snapshot_root, task_ids)
    _snapshot_catalog(prepared.root)
    print(
        json.dumps(
            {
                "snapshot_root": str(prepared.root),
                "task_count": len(task_ids),
                "source_digest": prepared.source_digest,
                "transform_digest": prepared.transform_digest,
                "content_digest": prepared.content_digest,
            },
            sort_keys=True,
        )
    )


def _render(args: argparse.Namespace) -> None:
    snapshot_root = Path(args.snapshot_root).expanduser().resolve(strict=True)
    rows = _snapshot_catalog(snapshot_root)
    development, selected_a, _held_out = _validate_development_selection(
        rows,
        args.development_task_id,
        args.a_task_id,
        sample_size=args.sample_size,
    )
    stamp = json.loads((snapshot_root / DERIVATION_STAMP).read_text(encoding="utf-8"))
    source_digest = str(stamp["source_digest"])
    freeze = _campaign_freeze(
        rows=rows,
        development_task_ids=development,
        campaign_id=args.campaign_id,
        cell_id=args.cell_id,
        method=args.method,
        source_reference="sha256:" + source_digest.removeprefix("sha256:"),
        task_catalog_reference="sha256:"
        + str(stamp["content_digest"]).removeprefix("sha256:"),
        held_out_identity=args.held_out_identity,
    )
    template = Path(args.template).read_text(encoding="utf-8")
    if template.count(_POLICY_MARKER) != 1 or template.count(_FREEZE_MARKER) != 1:
        raise SweOperatorError("SWE template markers are missing or duplicated")
    rendered = template.replace(
        _POLICY_MARKER,
        _policy_toml(
            development_task_ids=development,
            a_task_ids=selected_a,
            sample_size=args.sample_size,
            sampling_seed=args.sampling_seed,
        ),
    ).replace(_FREEZE_MARKER, _freeze_toml(freeze, cell_id=args.cell_id))
    rendered = rendered.replace(
        'cell_id = "RENDERED_CELL_ID"', f"cell_id = {_quote(args.cell_id)}"
    )
    rendered = rendered.replace(
        f"snapshot_root = {_quote(_SNAPSHOT_PLACEHOLDER)}",
        f"snapshot_root = {_quote(str(snapshot_root))}",
    )
    if args.harbor_root is not None:
        harbor_root = Path(args.harbor_root).expanduser().resolve(strict=True)
        rendered = rendered.replace(
            f"trusted_harbor_root = {_quote(_HARBOR_PLACEHOLDER)}",
            f"trusted_harbor_root = {_quote(str(harbor_root))}",
        )
    resolved = resolve_experiment(_load_toml_bytes(rendered.encode("utf-8")))
    _validate_resolved(resolved, rows, snapshot_root=snapshot_root)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered.rstrip() + "\n", encoding="utf-8")


def _validate(args: argparse.Namespace) -> None:
    snapshot_root = Path(args.snapshot_root).expanduser().resolve(strict=True)
    resolved = resolve_experiment(load_experiment_toml(args.experiment))
    _validate_resolved(
        resolved,
        _snapshot_catalog(snapshot_root),
        snapshot_root=snapshot_root,
        require_runtime=True,
    )


def _docker_run(
    docker: Path,
    *arguments: str,
    timeout: float,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    environment = {"PATH": os.environ.get("PATH", os.defpath)}
    if "DOCKER_HOST" in os.environ:
        environment["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
    if "DOCKER_CONFIG" in os.environ:
        environment["DOCKER_CONFIG"] = os.environ["DOCKER_CONFIG"]
    try:
        completed = subprocess.run(
            [str(docker), *arguments],
            check=False,
            text=True,
            capture_output=capture,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SweOperatorError("Docker preflight command failed") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:1000]
        raise SweOperatorError(f"Docker preflight command failed: {detail}")
    return completed


def _prepare_canary(args: argparse.Namespace) -> None:
    _positive_number(args.pull_timeout, "SWE image pull timeout")
    _positive_number(args.build_timeout, "SWE image build timeout")
    source_root = Path(args.source_root).expanduser().resolve(strict=True)
    task_ids = _source_task_ids(source_root)
    if args.task_id not in set(task_ids):
        raise SweOperatorError(
            "requested SWE canary task is outside official membership"
        )
    prepared = build_derived_task_tree(
        source_root, Path(args.canary_root), (args.task_id,)
    )
    task_dir = prepared.root / args.task_id
    identity = inspect_official_swe_task(task_dir, expected_task_id=args.task_id)
    docker = _regular_executable(args.docker_path, "Docker")
    bwrap = _regular_executable(args.bwrap_path, "bubblewrap")
    harbor_root = Path(args.harbor_root).expanduser().resolve(strict=True)
    candidate_root = (
        Path(args.candidate_root or harbor_root).expanduser().resolve(strict=True)
    )
    terminus_source = candidate_root / "src" / "harbor" / "agents" / "terminus_2"
    terminus_digest = _regular_tree_sha256(
        terminus_source, "candidate Terminus-2 source"
    )
    try:
        controller = harbor_controller_runtime_identity(harbor_root, docker)
    except SweHarborCellError as exc:
        raise SweOperatorError("trusted Harbor checkout is unavailable") from exc
    if args.pull_image:
        _docker_run(docker, "pull", identity.source_image, timeout=args.pull_timeout)
    try:
        image = resolve_immutable_docker_image(
            docker, identity.source_image, inspect=inspect_docker_image
        )
    except HarborExecutionError as exc:
        raise SweOperatorError(
            "SWE canary source image is unavailable; rerun with --pull-image"
        ) from exc
    budget = harbor_timeout_budget(
        task_dir,
        worker_timeout_seconds=args.worker_timeout_seconds,
        candidate_harvest_grace_seconds=args.candidate_harvest_grace_seconds,
        verifier_timeout_seconds=args.verifier_timeout_seconds,
        harbor_wall_timeout_seconds=args.harbor_wall_timeout_seconds,
        cleanup_grace_seconds=args.cleanup_grace_seconds,
    )
    source_export_record: dict[str, object]
    built_images: dict[str, object] = {}
    with tempfile.TemporaryDirectory(
        prefix=".rsi-arena-swe-canary-", dir=str(prepared.root.parent)
    ) as raw:
        work = Path(raw)
        source = OfficialSweTaskSource(
            task_dir,
            docker_path=docker,
            expected_task_id=args.task_id,
            expected_repo=identity.repo,
        )
        exported_repo = work / "official-source"
        exported = source.export_fresh_repo(exported_repo)
        source.revalidate_runtime_identity(exported, exported_repo=exported_repo)
        source_export_record = {
            **exported.binding(),
            "source_export_digest": exported.digest,
        }
        if args.build_images:
            pinned = work / "content-pinned-task"
            materialize_content_pinned_task_package(
                task_dir,
                pinned,
                runtime_image_ref=exported.runtime_image_ref,
            )
            tag_suffix = identity.public_material_digest[:24]
            targets = {
                "agent": (
                    pinned / "environment",
                    f"rsi-arena-swe-canary-agent:{tag_suffix}",
                ),
                "verifier": (
                    pinned / "tests",
                    f"rsi-arena-swe-canary-verifier:{tag_suffix}",
                ),
            }
            for role, (context, tag) in targets.items():
                _docker_run(
                    docker,
                    "build",
                    "--pull=false",
                    "--tag",
                    tag,
                    "--file",
                    str(context / "Dockerfile"),
                    str(context),
                    timeout=args.build_timeout,
                )
                built_images[role] = {
                    "tag": tag,
                    **inspect_docker_image(docker, tag).as_dict(),
                }
    receipt = {
        "schema": "rsi-arena-swe-canary-preflight",
        "task_id": args.task_id,
        "canary_root": str(prepared.root),
        "task_transform": prepared.transform_identity,
        "source_digest": prepared.source_digest,
        "transform_digest": prepared.transform_digest,
        "content_digest": prepared.content_digest,
        "task_identity": identity.__dict__,
        "source_image": image.as_dict(),
        "source_export": source_export_record,
        "built_images": built_images,
        "docker_executable_sha256": _file_sha256(docker),
        "bwrap_executable_sha256": _file_sha256(bwrap),
        "harbor_controller": controller,
        "inner_agent": "builtin:terminus-2",
        "candidate_terminus_source": {
            "root": str(terminus_source),
            "sha256": terminus_digest,
            "candidate_code_imported": False,
        },
        "timeout_budget": budget.as_dict(),
        "provider_call_performed": False,
    }
    payload = json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    if args.receipt is not None:
        receipt_path = Path(args.receipt)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(payload, encoding="utf-8")
    print(payload, end="")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rsi-arena-swe-operator")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare-snapshot")
    prepare.add_argument("--source-root", type=Path, required=True)
    prepare.add_argument("--snapshot-root", type=Path, required=True)

    render = commands.add_parser("render")
    render.add_argument("--template", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--snapshot-root", type=Path, required=True)
    render.add_argument("--harbor-root", type=Path)
    render.add_argument("--campaign-id", required=True)
    render.add_argument("--cell-id", required=True)
    render.add_argument("--method", required=True)
    render.add_argument("--held-out-identity", required=True)
    render.add_argument("--development-task-id", action="append", required=True)
    render.add_argument("--a-task-id", action="append", required=True)
    render.add_argument("--sample-size", type=int, required=True)
    render.add_argument("--sampling-seed", type=int, required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("--experiment", type=Path, required=True)
    validate.add_argument("--snapshot-root", type=Path, required=True)

    canary = commands.add_parser("prepare-canary")
    canary.add_argument("--source-root", type=Path, required=True)
    canary.add_argument("--canary-root", type=Path, required=True)
    canary.add_argument("--task-id", required=True)
    canary.add_argument("--harbor-root", type=Path, required=True)
    canary.add_argument("--candidate-root", type=Path)
    canary.add_argument("--docker-path", default="docker")
    canary.add_argument("--bwrap-path", default="bwrap")
    canary.add_argument("--pull-image", action="store_true")
    canary.add_argument("--pull-timeout", type=float, default=3600.0)
    canary.add_argument("--build-images", action="store_true")
    canary.add_argument("--build-timeout", type=float, default=3600.0)
    canary.add_argument("--worker-timeout-seconds", type=float, default=3000.0)
    canary.add_argument("--candidate-harvest-grace-seconds", type=float, default=60.0)
    canary.add_argument("--verifier-timeout-seconds", type=float, default=3000.0)
    canary.add_argument("--harbor-wall-timeout-seconds", type=float, default=14400.0)
    canary.add_argument("--cleanup-grace-seconds", type=float, default=30.0)
    canary.add_argument("--receipt", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prepare-snapshot":
        _prepare_snapshot(args)
    elif args.command == "render":
        _render(args)
    elif args.command == "validate":
        _validate(args)
    else:
        _prepare_canary(args)
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI
    raise SystemExit(main())
