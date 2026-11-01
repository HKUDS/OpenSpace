"""No-model operator tooling for BrowseComp-Plus experiment authority."""

from __future__ import annotations

import argparse
import hashlib
import importlib.resources
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

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

from .production import load_trusted_catalog

_BENCHMARK_ID = "browsecomp_plus"
_POLICY_MARKER = "# BROWSECOMP_DEVELOPMENT_POLICY"
_FREEZE_MARKER = "# BROWSECOMP_CAMPAIGN_FREEZE"
_REFERENCE_SCAFFOLD_DIRECTORY = "candidate_scaffold"
_REFERENCE_SCAFFOLD_FILES = ("inner_prompt.txt",)
_FINAL_RESPONSE_FIELDS = ("`explanation`", "`exact_answer`", "`confidence`")


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


def _campaign_freeze(
    *,
    catalog,
    campaign_id: str,
    cell_id: str,
    method: str,
    source_reference: str,
    task_catalog_reference: str,
    held_out_identity: str,
) -> CampaignFreeze:
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
    development = tuple(
        TaskRef(
            task.query_id,
            cluster="browsecomp-plus",
            benchmark_id=_BENCHMARK_ID,
        )
        for task in catalog.development
    )
    boundary = DatasetBoundary(
        development_set=freeze_development_set(development),
        held_out_set=freeze_held_out_set_for_evaluator(
            held_out_identity,
            tuple(task.query_id for task in catalog.held_out),
        ),
    )
    return CampaignFreeze.create(config, {_BENCHMARK_ID: boundary})


def _policy_toml(
    *,
    development_ids: tuple[str, ...],
    a_task_ids: tuple[str, ...],
    sample_size: int,
    sampling_seed: int,
) -> str:
    if not a_task_ids or len(a_task_ids) == len(development_ids):
        raise ValueError("A selection must leave at least one development task for B")
    if len(set(a_task_ids)) != len(a_task_ids) or not set(a_task_ids).issubset(
        development_ids
    ):
        raise ValueError("A task IDs must be unique members of the development set")
    b_task_ids = tuple(
        task_id for task_id in development_ids if task_id not in a_task_ids
    )
    if sample_size < 1 or sample_size > len(b_task_ids):
        raise ValueError("sample size must fit the generated B membership")
    lines = [
        "[baseline.config.development_policy]",
        "",
        "[baseline.config.development_policy.benchmarks]",
        "",
        "[baseline.config.development_policy.benchmarks.browsecomp_plus]",
        "a_task_ids = " + _array(a_task_ids)[0],
        *_array(a_task_ids)[1:],
        "b_task_ids = " + _array(b_task_ids)[0],
        *_array(b_task_ids)[1:],
        "",
        "[baseline.config.development_policy.benchmarks.browsecomp_plus.sampling]",
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
        "[benchmarks.config.campaign_freeze.benchmarks.browsecomp_plus]",
        f"source_reference = {_quote(source.source_reference)}",
        f"task_catalog_ref = {_quote(source.task_catalog_ref)}",
        "",
        "[benchmarks.config.campaign_freeze.cells]",
        "",
        f"[benchmarks.config.campaign_freeze.cells.{_quote(cell_id)}]",
        f"method = {_quote(freeze.cells[cell_id].method)}",
        'benchmarks = ["browsecomp_plus"]',
        'aggregate = { kind = "macro_mean_pass_rate" }',
        "",
        "[benchmarks.config.campaign_freeze.dataset_boundaries]",
        "",
        "[benchmarks.config.campaign_freeze.dataset_boundaries.browsecomp_plus]",
        'schema = "dataset-boundary"',
        "",
        "[benchmarks.config.campaign_freeze.dataset_boundaries.browsecomp_plus.development_set]",
        "ordered_task_ids = " + _array(development.ordered_task_ids)[0],
        *_array(development.ordered_task_ids)[1:],
        f"digest = {_quote(development.digest)}",
        "",
        "[benchmarks.config.campaign_freeze.dataset_boundaries.browsecomp_plus.development_set.task_clusters]",
        *(
            f"{_quote(task_id)} = {_quote(development.task_clusters[task_id])}"
            for task_id in development.ordered_task_ids
        ),
        "",
        "[benchmarks.config.campaign_freeze.dataset_boundaries.browsecomp_plus.held_out_set]",
        f"opaque_identity = {_quote(held_out.opaque_identity)}",
        f"digest = {_quote(held_out.digest)}",
        f"count = {held_out.count}",
    ]
    return "\n".join(lines)


def _catalog(args: argparse.Namespace):
    return load_trusted_catalog(
        args.catalog,
        catalog_format=args.catalog_format,
    )


def _reference_scaffold_files() -> dict[str, bytes]:
    root = importlib.resources.files(__package__).joinpath(
        _REFERENCE_SCAFFOLD_DIRECTORY
    )
    files = {
        name: root.joinpath(name).read_bytes() for name in _REFERENCE_SCAFFOLD_FILES
    }
    prompt = files["inner_prompt.txt"].decode("utf-8")
    positions = tuple(prompt.find(field) for field in _FINAL_RESPONSE_FIELDS)
    if any(position < 0 for position in positions) or positions != tuple(
        sorted(positions)
    ):
        raise RuntimeError(
            "packaged BrowseComp reference scaffold does not require the evaluator "
            "output format"
        )
    if (
        "`submit_answer` function call" not in prompt
        or "0 through 100" not in prompt
        or "Do not add fields" not in prompt
        or "contain no line break" not in prompt
        or "sole tool call" not in prompt
        or "free text" not in prompt
    ):
        raise RuntimeError(
            "packaged BrowseComp reference scaffold has an incomplete output contract"
        )
    return files


def _prepare_candidate(args: argparse.Namespace) -> None:
    output = Path(args.output).expanduser().absolute()
    if output.exists():
        if not output.is_dir():
            raise ValueError("BrowseComp candidate output must be a directory")
        if any(output.iterdir()):
            raise FileExistsError("BrowseComp candidate output directory must be empty")
    else:
        output.mkdir(parents=True)
    for name, payload in _reference_scaffold_files().items():
        (output / name).write_bytes(payload)


def _render(args: argparse.Namespace) -> None:
    template = Path(args.template).read_text(encoding="utf-8")
    if template.count(_POLICY_MARKER) != 1 or template.count(_FREEZE_MARKER) != 1:
        raise ValueError("BrowseComp template markers are missing or duplicated")
    catalog = _catalog(args)
    catalog_path = Path(args.catalog).expanduser().resolve(strict=True)
    freeze = _campaign_freeze(
        catalog=catalog,
        campaign_id=args.campaign_id,
        cell_id=args.cell_id,
        method=args.method,
        source_reference=args.source_reference,
        task_catalog_reference=_file_sha256(catalog_path),
        held_out_identity=args.held_out_identity,
    )
    development_ids = tuple(task.query_id for task in catalog.development)
    rendered = template.replace(
        _POLICY_MARKER,
        _policy_toml(
            development_ids=development_ids,
            a_task_ids=tuple(args.a_task_id),
            sample_size=args.sample_size,
            sampling_seed=args.sampling_seed,
        ),
    ).replace(_FREEZE_MARKER, _freeze_toml(freeze, cell_id=args.cell_id))
    rendered = rendered.replace(
        'cell_id = "RENDERED_CELL_ID"', f"cell_id = {_quote(args.cell_id)}"
    )
    rendered = rendered.replace(
        'catalog_path = "/trusted/browsecomp-plus/catalog.jsonl"',
        f"catalog_path = {_quote(str(catalog_path))}",
    ).replace(
        'catalog_format = "official-obfuscated-jsonl"',
        f"catalog_format = {_quote(args.catalog_format)}",
    )
    resolved = resolve_experiment(load_experiment_toml_bytes(rendered.encode("utf-8")))
    _validate_resolved(
        resolved,
        catalog,
        catalog_path=catalog_path,
        catalog_format=args.catalog_format,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered.rstrip() + "\n", encoding="utf-8")


def load_experiment_toml_bytes(payload: bytes) -> Mapping[str, object]:
    import tomllib

    loaded = tomllib.loads(payload.decode("utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError("rendered experiment is not a TOML table")
    return loaded


def _validate_resolved(
    resolved: Mapping[str, object],
    catalog,
    *,
    catalog_path: Path,
    catalog_format: str,
) -> None:
    benchmarks = resolved.get("benchmarks")
    if not isinstance(benchmarks, list):
        raise TypeError("experiment has no benchmark list")
    matches = [
        benchmark
        for benchmark in benchmarks
        if isinstance(benchmark, Mapping) and benchmark.get("name") == _BENCHMARK_ID
    ]
    if len(matches) != 1:
        raise ValueError("experiment must contain exactly one BrowseComp benchmark")
    config = matches[0].get("config")
    if not isinstance(config, Mapping):
        raise TypeError("BrowseComp benchmark config is missing")
    freeze_raw = config.get("campaign_freeze")
    cell_id = config.get("cell_id")
    if not isinstance(freeze_raw, Mapping) or not isinstance(cell_id, str):
        raise TypeError("BrowseComp campaign authority is missing")
    trusted_data = config.get("trusted_data")
    if not isinstance(trusted_data, Mapping):
        raise TypeError("BrowseComp trusted data config is missing")
    configured_catalog = trusted_data.get("catalog_path")
    if (
        not isinstance(configured_catalog, str)
        or Path(configured_catalog).expanduser().resolve(strict=True) != catalog_path
        or trusted_data.get("catalog_format") != catalog_format
    ):
        raise ValueError("BrowseComp catalog config differs from validated data")
    freeze = CampaignFreeze.from_mapping(freeze_raw)
    plan = runtime_campaign_plan_from_freeze(
        freeze,
        cell_id=cell_id,
        benchmark_id=_BENCHMARK_ID,
    )
    development_ids = tuple(task.query_id for task in catalog.development)
    if plan.development_task_ids != development_ids:
        raise ValueError("BrowseComp development membership differs from the catalog")
    resolve_held_out_task_ids_for_evaluator(
        plan.dataset_boundary.held_out_set,
        tuple(task.query_id for task in catalog.held_out),
    )


def _validate(args: argparse.Namespace) -> None:
    resolved = resolve_experiment(load_experiment_toml(args.experiment))
    catalog_path = Path(args.catalog).expanduser().resolve(strict=True)
    _validate_resolved(
        resolved,
        _catalog(args),
        catalog_path=catalog_path,
        catalog_format=args.catalog_format,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="browsecomp-plus-operator")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("render", "validate"):
        command = commands.add_parser(name)
        command.add_argument("--catalog", type=Path, required=True)
        command.add_argument(
            "--catalog-format",
            choices=("official-obfuscated-jsonl", "official-decrypted-jsonl"),
            required=True,
        )
    render = commands.choices["render"]
    render.add_argument("--template", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--campaign-id", required=True)
    render.add_argument("--cell-id", required=True)
    render.add_argument("--method", required=True)
    render.add_argument("--source-reference", required=True)
    render.add_argument("--held-out-identity", required=True)
    render.add_argument("--a-task-id", action="append", required=True)
    render.add_argument("--sample-size", type=int, required=True)
    render.add_argument("--sampling-seed", type=int, required=True)
    validate = commands.choices["validate"]
    validate.add_argument("--experiment", type=Path, required=True)
    prepare_candidate = commands.add_parser("prepare-candidate")
    prepare_candidate.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "render":
        _render(args)
    elif args.command == "validate":
        _validate(args)
    else:
        _prepare_candidate(args)
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI
    raise SystemExit(main())
