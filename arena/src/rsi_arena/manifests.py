"""Dataset boundaries and campaign configuration.

The active campaign contract freezes only an ordered development set and an
opaque held-out descriptor for each benchmark.  A/B views and sampling are
caller-owned composition choices, never campaign topology.

Historical sealed-campaign validators and generic manifest digest helpers are
not part of this active owner.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from rsi_arena.contracts import TaskRef, canonical_digest

DATASET_BOUNDARY_SCHEMA = "dataset-boundary"
CAMPAIGN_CONFIG_SCHEMA = "campaign-config"
CAMPAIGN_FREEZE_SCHEMA = "campaign-freeze"
SUPPORTED_CAMPAIGN_AGGREGATES = frozenset({"macro_mean_pass_rate"})


class ManifestValidationError(ValueError):
    """Dataset/campaign input or freeze authority is invalid."""


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestValidationError(f"{field} must be an object")
    return value


def _exact_fields(raw: Mapping[object, object], expected: set[str], field: str) -> None:
    _fields(raw, expected, set(), field)


def _fields(
    raw: Mapping[object, object],
    required: set[str],
    optional: set[str],
    field: str,
) -> None:
    if any(not isinstance(key, str) for key in raw):
        raise ManifestValidationError(f"{field} field names must be strings")
    present = set(raw)
    missing = sorted(required - present)
    unknown = sorted(present - required - optional)
    if missing:
        raise ManifestValidationError(
            f"{field} requires explicit fields: " + ", ".join(missing)
        )
    if unknown:
        raise ManifestValidationError(
            f"{field} contains unknown fields: " + ", ".join(unknown)
        )


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{field} must be a string")
    result = value.strip()
    if not result:
        raise ManifestValidationError(f"{field} must be non-empty")
    return result


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestValidationError(f"{field} must be an integer")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestValidationError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ManifestValidationError(f"{field} must be finite")
    return result


def _explicit_task_ids(
    value: object, field: str, *, allow_empty: bool
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ManifestValidationError(f"{field} must be an explicit list")
    task_ids = tuple(_nonempty(item, field) for item in value)
    if not allow_empty and not task_ids:
        raise ManifestValidationError(f"{field} must not be empty")
    return task_ids


def _canonical_task_ids(
    task_ids: Sequence[str], field: str, *, sort: bool = False
) -> tuple[str, ...]:
    if isinstance(task_ids, (str, bytes)):
        raise ManifestValidationError(f"{field} must be a sequence")
    canonical = [_nonempty(task_id, f"{field} task_id") for task_id in task_ids]
    if not canonical:
        raise ManifestValidationError(f"{field} must not be empty")
    if len(set(canonical)) != len(canonical):
        raise ManifestValidationError(f"{field} contains duplicate/colliding task IDs")
    if sort:
        canonical.sort()
    return tuple(canonical)


def _canonical_task_catalog(
    tasks: Sequence[TaskRef], field: str
) -> tuple[tuple[str, ...], dict[str, str]]:
    listed = tuple(tasks)
    if not listed:
        raise ManifestValidationError(f"{field} must not be empty")
    task_ids: list[str] = []
    clusters: dict[str, str] = {}
    for task in listed:
        task_id = _nonempty(task.task_id, f"{field} task_id")
        if task_id in clusters:
            raise ManifestValidationError(
                f"{field} contains duplicate/colliding task IDs"
            )
        task_ids.append(task_id)
        clusters[task_id] = _nonempty(task.cluster, f"{field} cluster")
    return tuple(task_ids), clusters


def _validated_sha256_digest(value: object, field: str) -> str:
    digest = _nonempty(value, field)
    if not digest.startswith("sha256:"):
        raise ManifestValidationError(f"{field} must be a sha256 digest")
    digest_hex = digest.removeprefix("sha256:")
    if len(digest_hex) != 64 or any(
        character not in "0123456789abcdef" for character in digest_hex
    ):
        raise ManifestValidationError(f"{field} must be a sha256 digest")
    return digest


def _development_set_digest(
    ordered_task_ids: Sequence[str], task_clusters: Mapping[str, str]
) -> str:
    return "sha256:" + canonical_digest(
        {
            "ordered_task_ids": list(ordered_task_ids),
            "task_clusters": dict(task_clusters),
        }
    )


def _held_out_set_digest(opaque_identity: str, task_ids: Sequence[str]) -> str:
    return "sha256:" + canonical_digest(
        {
            "opaque_identity": opaque_identity,
            "task_ids": task_ids,
        }
    )


@dataclass(frozen=True)
class DevelopmentSetFreeze:
    """Immutable ordered development membership with cluster attribution."""

    ordered_task_ids: tuple[str, ...]
    task_clusters: Mapping[str, str]
    digest: str

    def __post_init__(self) -> None:
        task_ids = _canonical_task_ids(
            self.ordered_task_ids, "development ordered_task_ids"
        )
        clusters: dict[str, str] = {}
        raw_clusters = _mapping(self.task_clusters, "development task_clusters")
        for raw_task_id, raw_cluster in raw_clusters.items():
            task_id = _nonempty(raw_task_id, "development cluster task_id")
            if task_id in clusters:
                raise ManifestValidationError(
                    "development task_clusters contains duplicate/colliding task IDs"
                )
            clusters[task_id] = _nonempty(raw_cluster, "development task cluster")
        if set(clusters) != set(task_ids):
            raise ManifestValidationError(
                "development membership must equal its cluster catalog"
            )
        digest = _validated_sha256_digest(self.digest, "development digest")
        if digest != _development_set_digest(task_ids, clusters):
            raise ManifestValidationError(
                "development digest does not match ordered membership and clusters"
            )
        object.__setattr__(self, "ordered_task_ids", task_ids)
        object.__setattr__(self, "task_clusters", MappingProxyType(clusters))
        object.__setattr__(self, "digest", digest)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DevelopmentSetFreeze":
        _exact_fields(
            raw,
            {"ordered_task_ids", "task_clusters", "digest"},
            "development set freeze",
        )
        return cls(
            ordered_task_ids=_explicit_task_ids(
                raw["ordered_task_ids"],
                "development ordered_task_ids",
                allow_empty=False,
            ),
            task_clusters=_mapping(raw["task_clusters"], "development task_clusters"),
            digest=raw["digest"],
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "ordered_task_ids": list(self.ordered_task_ids),
            "task_clusters": {
                task_id: self.task_clusters[task_id]
                for task_id in self.ordered_task_ids
            },
            "digest": self.digest,
        }


@dataclass(frozen=True)
class HeldOutSetFreeze:
    """Opaque held-out descriptor; task membership is never persisted here."""

    opaque_identity: str
    digest: str
    count: int

    def __post_init__(self) -> None:
        identity = _nonempty(self.opaque_identity, "held-out opaque_identity")
        digest = _validated_sha256_digest(self.digest, "held-out digest")
        count = _integer(self.count, "held-out count")
        if count < 1:
            raise ManifestValidationError("held-out count must be positive")
        object.__setattr__(self, "opaque_identity", identity)
        object.__setattr__(self, "digest", digest)
        object.__setattr__(self, "count", count)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "HeldOutSetFreeze":
        _exact_fields(
            raw,
            {"opaque_identity", "digest", "count"},
            "held-out set freeze",
        )
        return cls(
            opaque_identity=raw["opaque_identity"],
            digest=raw["digest"],
            count=raw["count"],
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "opaque_identity": self.opaque_identity,
            "digest": self.digest,
            "count": self.count,
        }


@dataclass(frozen=True)
class DatasetBoundary:
    """Kernel dataset boundary with no split or sampling policy."""

    development_set: DevelopmentSetFreeze
    held_out_set: HeldOutSetFreeze

    def __post_init__(self) -> None:
        if not isinstance(self.development_set, DevelopmentSetFreeze):
            raise ManifestValidationError(
                "development_set must be a DevelopmentSetFreeze"
            )
        if not isinstance(self.held_out_set, HeldOutSetFreeze):
            raise ManifestValidationError("held_out_set must be a HeldOutSetFreeze")

    @classmethod
    def from_mapping(cls, raw: object) -> "DatasetBoundary":
        boundary = _mapping(raw, "dataset boundary")
        _exact_fields(
            boundary,
            {"schema", "development_set", "held_out_set"},
            "dataset boundary",
        )
        if boundary.get("schema") != DATASET_BOUNDARY_SCHEMA:
            raise ManifestValidationError("unexpected dataset boundary schema")
        return cls(
            development_set=DevelopmentSetFreeze.from_mapping(
                _mapping(boundary["development_set"], "development_set")
            ),
            held_out_set=HeldOutSetFreeze.from_mapping(
                _mapping(boundary["held_out_set"], "held_out_set")
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": DATASET_BOUNDARY_SCHEMA,
            "development_set": self.development_set.as_dict(),
            "held_out_set": self.held_out_set.as_dict(),
        }


def freeze_development_set(tasks: Sequence[TaskRef]) -> DevelopmentSetFreeze:
    """Freeze development tasks in their operator-selected order."""

    task_ids, clusters = _canonical_task_catalog(tasks, "development set")
    return DevelopmentSetFreeze(
        ordered_task_ids=task_ids,
        task_clusters=clusters,
        digest=_development_set_digest(task_ids, clusters),
    )


def freeze_held_out_set_for_evaluator(
    opaque_identity: str, task_ids: Sequence[str]
) -> HeldOutSetFreeze:
    """Build an opaque held-out descriptor from evaluator-only membership."""

    canonical_task_ids = _canonical_task_ids(
        task_ids, "evaluator held-out task IDs", sort=True
    )
    identity = _nonempty(opaque_identity, "held-out opaque_identity")
    return HeldOutSetFreeze(
        opaque_identity=identity,
        digest=_held_out_set_digest(identity, canonical_task_ids),
        count=len(canonical_task_ids),
    )


def resolve_held_out_task_ids_for_evaluator(
    freeze: HeldOutSetFreeze, task_ids: Sequence[str]
) -> tuple[str, ...]:
    """Verify and return held-out IDs supplied only inside the evaluator."""

    canonical_task_ids = _canonical_task_ids(
        task_ids, "evaluator held-out task IDs", sort=True
    )
    digest = _held_out_set_digest(freeze.opaque_identity, canonical_task_ids)
    if freeze.count != len(canonical_task_ids) or freeze.digest != digest:
        raise ManifestValidationError(
            "evaluator held-out task IDs do not match the opaque descriptor"
        )
    return canonical_task_ids


def validate_dataset_boundary_for_evaluator(
    boundary: DatasetBoundary,
    task_catalog: Sequence[TaskRef],
    *,
    held_out_task_ids: Sequence[str],
) -> None:
    """Validate disjoint cover using held-out material confined to the evaluator."""

    catalog_ids, catalog_clusters = _canonical_task_catalog(
        task_catalog, "evaluator task catalog"
    )
    resolved_held_out = resolve_held_out_task_ids_for_evaluator(
        boundary.held_out_set, held_out_task_ids
    )
    development_ids = boundary.development_set.ordered_task_ids
    if set(development_ids) & set(resolved_held_out):
        raise ManifestValidationError("development and held-out membership overlaps")
    if set(development_ids) | set(resolved_held_out) != set(catalog_ids):
        raise ManifestValidationError(
            "development and held-out membership must cover the task catalog"
        )
    for task_id in development_ids:
        if (
            catalog_clusters.get(task_id)
            != boundary.development_set.task_clusters[task_id]
        ):
            raise ManifestValidationError(
                "development clusters do not match the evaluator task catalog"
            )


@dataclass(frozen=True)
class CampaignAggregateSpec:
    """An optional scalar explicitly frozen for one campaign cell."""

    kind: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any], *, field: str
    ) -> "CampaignAggregateSpec":
        _exact_fields(raw, {"kind"}, field)
        kind = _nonempty(raw["kind"], f"{field}.kind")
        if kind not in SUPPORTED_CAMPAIGN_AGGREGATES:
            raise ManifestValidationError(f"unsupported campaign aggregate: {kind!r}")
        return cls(kind=kind)

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind}


@dataclass(frozen=True)
class BenchmarkSourceReference:
    benchmark: str
    source_reference: str
    task_catalog_ref: str

    @classmethod
    def from_mapping(
        cls, benchmark: str, raw: Mapping[str, Any]
    ) -> "BenchmarkSourceReference":
        required = {"source_reference", "task_catalog_ref"}
        _exact_fields(raw, required, f"benchmarks.{benchmark}")
        return cls(
            benchmark=_nonempty(benchmark, "benchmark"),
            source_reference=_nonempty(
                raw["source_reference"], f"benchmarks.{benchmark}.source_reference"
            ),
            task_catalog_ref=_nonempty(
                raw["task_catalog_ref"], f"benchmarks.{benchmark}.task_catalog_ref"
            ),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "source_reference": self.source_reference,
            "task_catalog_ref": self.task_catalog_ref,
        }


@dataclass(frozen=True)
class CampaignCell:
    cell_id: str
    method: str
    benchmarks: tuple[str, ...]
    aggregate: CampaignAggregateSpec | None = None

    def __post_init__(self) -> None:
        benchmark_ids = _canonical_task_ids(
            self.benchmarks, f"cell {self.cell_id!r} benchmarks", sort=False
        )
        if not benchmark_ids:
            raise ManifestValidationError(
                f"cell {self.cell_id!r} needs at least one benchmark"
            )
        object.__setattr__(self, "benchmarks", benchmark_ids)

    @property
    def benchmark_ids(self) -> tuple[str, ...]:
        """Ordered benchmark membership for this active cell."""

        return self.benchmarks

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "method": self.method,
            "benchmarks": list(self.benchmarks),
        }
        if self.aggregate is not None:
            payload["aggregate"] = self.aggregate.as_dict()
        return payload


@dataclass(frozen=True)
class CampaignConfig:
    campaign_id: str
    benchmarks: Mapping[str, BenchmarkSourceReference]
    cells: Mapping[str, CampaignCell]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CampaignConfig":
        _exact_fields(
            raw,
            {"schema", "campaign_id", "benchmarks", "cells"},
            "campaign config",
        )
        if raw.get("schema") != CAMPAIGN_CONFIG_SCHEMA:
            raise ManifestValidationError("unexpected campaign config schema")
        raw_benchmarks = _mapping(raw["benchmarks"], "benchmarks")
        if not raw_benchmarks:
            raise ManifestValidationError("campaign config needs a benchmark")
        benchmarks: dict[str, BenchmarkSourceReference] = {}
        for raw_benchmark, value in raw_benchmarks.items():
            benchmark = _nonempty(raw_benchmark, "benchmark key")
            benchmarks[benchmark] = BenchmarkSourceReference.from_mapping(
                benchmark, _mapping(value, f"benchmarks.{benchmark}")
            )
        raw_cells = _mapping(raw["cells"], "cells")
        if not raw_cells:
            raise ManifestValidationError("campaign config needs a cell")
        cells: dict[str, CampaignCell] = {}
        for raw_cell_id, value in raw_cells.items():
            cell_id = _nonempty(raw_cell_id, "cell id")
            cell = _mapping(value, f"cells.{cell_id}")
            _fields(
                cell,
                {"method", "benchmarks"},
                {"aggregate"},
                f"cells.{cell_id}",
            )
            raw_cell_benchmarks = _explicit_task_ids(
                cell["benchmarks"], f"cells.{cell_id}.benchmarks", allow_empty=False
            )
            cell_benchmarks = tuple(raw_cell_benchmarks)
            if len(set(cell_benchmarks)) != len(cell_benchmarks):
                raise ManifestValidationError(
                    f"cells.{cell_id}.benchmarks contains duplicates"
                )
            unknown = [item for item in cell_benchmarks if item not in benchmarks]
            if unknown:
                raise ManifestValidationError(
                    f"cells.{cell_id} references unknown benchmark {unknown[0]!r}"
                )
            cells[cell_id] = CampaignCell(
                cell_id=cell_id,
                method=_nonempty(cell["method"], f"cells.{cell_id}.method"),
                benchmarks=cell_benchmarks,
                aggregate=(
                    CampaignAggregateSpec.from_mapping(
                        _mapping(cell["aggregate"], f"cells.{cell_id}.aggregate"),
                        field=f"cells.{cell_id}.aggregate",
                    )
                    if "aggregate" in cell
                    else None
                ),
            )
        return cls(
            campaign_id=_nonempty(raw["campaign_id"], "campaign_id"),
            benchmarks=MappingProxyType(benchmarks),
            cells=MappingProxyType(cells),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": CAMPAIGN_CONFIG_SCHEMA,
            "campaign_id": self.campaign_id,
            "benchmarks": {
                key: value.as_dict() for key, value in sorted(self.benchmarks.items())
            },
            "cells": {
                key: value.as_dict() for key, value in sorted(self.cells.items())
            },
        }


@dataclass(frozen=True)
class SampleSlot:
    sample_index: int
    task_id: str
    replicate: int = 0
    measurement_seed: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.sample_index, bool)
            or not isinstance(self.sample_index, int)
            or self.sample_index < 0
        ):
            raise ManifestValidationError("sample_index must be a non-negative integer")
        _nonempty(self.task_id, "sample task_id")
        for field_name in ("replicate", "measurement_seed"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ManifestValidationError(
                    f"sample {field_name} must be a non-negative integer"
                )

    def as_dict(self) -> dict[str, object]:
        return {
            "sample_index": self.sample_index,
            "task_id": self.task_id,
            "replicate": self.replicate,
            "measurement_seed": self.measurement_seed,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SampleSlot":
        if not isinstance(value, Mapping) or set(value) != {
            "sample_index",
            "task_id",
            "replicate",
            "measurement_seed",
        }:
            raise ManifestValidationError("sample slot fields are invalid")
        return cls(
            sample_index=value["sample_index"],  # type: ignore[arg-type]
            task_id=value["task_id"],  # type: ignore[arg-type]
            replicate=value["replicate"],  # type: ignore[arg-type]
            measurement_seed=value["measurement_seed"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class CampaignFreeze:
    campaign_id: str
    benchmarks: Mapping[str, BenchmarkSourceReference]
    cells: Mapping[str, CampaignCell]
    dataset_boundaries: Mapping[str, DatasetBoundary]
    held_out_sealed: bool
    freeze_id: str

    @classmethod
    def create(
        cls,
        config: CampaignConfig,
        dataset_boundaries: Mapping[str, DatasetBoundary],
    ) -> "CampaignFreeze":
        if set(dataset_boundaries) != set(config.benchmarks):
            raise ManifestValidationError(
                "freeze requires exactly one dataset boundary per benchmark"
            )
        for benchmark, boundary in dataset_boundaries.items():
            if not isinstance(boundary, DatasetBoundary):
                raise ManifestValidationError(
                    f"dataset boundary is invalid: {benchmark}"
                )
        payload = {
            **config.as_dict(),
            "schema": CAMPAIGN_FREEZE_SCHEMA,
            "dataset_boundaries": {
                key: value.as_dict()
                for key, value in sorted(dataset_boundaries.items())
            },
            "held_out_sealed": True,
        }
        freeze_id = "sha256:" + canonical_digest(payload)
        return cls(
            campaign_id=config.campaign_id,
            benchmarks=config.benchmarks,
            cells=config.cells,
            dataset_boundaries=MappingProxyType(dict(dataset_boundaries)),
            held_out_sealed=True,
            freeze_id=freeze_id,
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CampaignFreeze":
        _exact_fields(
            raw,
            {
                "schema",
                "campaign_id",
                "benchmarks",
                "cells",
                "dataset_boundaries",
                "held_out_sealed",
                "freeze_id",
            },
            "campaign freeze",
        )
        if raw.get("schema") != CAMPAIGN_FREEZE_SCHEMA:
            raise ManifestValidationError("unexpected campaign freeze schema")
        if raw.get("held_out_sealed") is not True:
            raise ManifestValidationError("campaign freeze must seal held-out material")
        config_payload = {
            key: raw[key]
            for key in ("campaign_id", "benchmarks", "cells")
            if key in raw
        }
        config_payload["schema"] = CAMPAIGN_CONFIG_SCHEMA
        config = CampaignConfig.from_mapping(config_payload)
        raw_boundaries = _mapping(raw.get("dataset_boundaries"), "dataset_boundaries")
        boundaries: dict[str, DatasetBoundary] = {}
        for raw_key, value in raw_boundaries.items():
            key = _nonempty(raw_key, "dataset boundary key")
            boundaries[key] = DatasetBoundary.from_mapping(value)
        rebuilt = cls.create(config, boundaries)
        if raw.get("freeze_id") != rebuilt.freeze_id:
            raise ManifestValidationError("campaign freeze_id does not match content")
        return rebuilt

    def as_dict(self) -> dict[str, object]:
        return {
            **CampaignConfig(self.campaign_id, self.benchmarks, self.cells).as_dict(),
            "schema": CAMPAIGN_FREEZE_SCHEMA,
            "dataset_boundaries": {
                key: value.as_dict()
                for key, value in sorted(self.dataset_boundaries.items())
            },
            "held_out_sealed": self.held_out_sealed,
            "freeze_id": self.freeze_id,
        }


def load_campaign_config(path: str | Path) -> CampaignConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestValidationError(f"cannot load campaign config: {path}") from exc
    return CampaignConfig.from_mapping(_mapping(raw, "campaign config"))


def load_campaign_freeze(path: str | Path) -> CampaignFreeze:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestValidationError(f"cannot load campaign freeze: {path}") from exc
    return CampaignFreeze.from_mapping(_mapping(raw, "campaign freeze"))
