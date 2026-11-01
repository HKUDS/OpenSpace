"""Private durable development policy state for the shipped baselines.

This module is deliberately not an Arena extension point.  Codex, DSH,
OpenEvolve, and Shinka call it from their own launch/resume wiring to avoid
duplicating the small amount of JSON durability and deterministic sampling
code they share.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from rsi_arena.manifests import CampaignFreeze, SampleSlot

_SCHEMA = "shipped-baseline-development-state"
_POLICY_FIELD = "development_policy"


@dataclass(frozen=True)
class BenchmarkDevelopmentState:
    development_set_digest: str
    a_task_ids: tuple[str, ...]
    b_task_ids: tuple[str, ...]
    sampling: Mapping[str, object]
    base_seed: int


@dataclass(frozen=True)
class BaselineDevelopmentState:
    """One baseline-owned fixed A/B policy restored from durable state."""

    owner: str
    method: str
    cell_id: str
    benchmarks: Mapping[str, BenchmarkDevelopmentState]

    @property
    def benchmark_ids(self) -> tuple[str, ...]:
        return tuple(self.benchmarks)

    def fixed_a_task_ids_by_benchmark(
        self,
    ) -> Mapping[str, tuple[str, ...]]:
        """Project every fixed A membership in frozen campaign-cell order."""

        return MappingProxyType(
            {
                benchmark_id: benchmark.a_task_ids
                for benchmark_id, benchmark in self.benchmarks.items()
            }
        )

    def slots_for_attempt(
        self, attempt_id: str
    ) -> Mapping[str, tuple[SampleSlot, ...]]:
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("baseline attempt_id must be non-empty")
        realized: dict[str, tuple[SampleSlot, ...]] = {}
        for benchmark_id, benchmark in self.benchmarks.items():
            sample_size = int(benchmark.sampling["sample_size"])
            identity = (
                f"{benchmark.base_seed}\0{self.cell_id}:{benchmark_id}\0{attempt_id}"
            )
            seed = int.from_bytes(
                hashlib.sha256(identity.encode("utf-8")).digest()[:8],
                "big",
            )
            selected = tuple(
                random.Random(seed).sample(benchmark.b_task_ids, sample_size)
            )
            realized[benchmark_id] = tuple(
                SampleSlot(
                    sample_index=index,
                    task_id=task_id,
                    replicate=0,
                    measurement_seed=int.from_bytes(
                        hashlib.sha256(
                            f"{seed}\0{index}\0{task_id}".encode("utf-8")
                        ).digest()[:8],
                        "big",
                    ),
                )
                for index, task_id in enumerate(selected)
            )
        return MappingProxyType(realized)


def development_state_path(method_state_directory: Path, owner: str) -> Path:
    return Path(method_state_directory) / _owner(owner) / "development-state.json"


def initialize_development_state(
    *,
    method_state_directory: Path,
    owner: str,
    experiment: Mapping[str, object],
) -> BaselineDevelopmentState:
    """Validate the frozen baseline config and persist its policy exactly once."""

    path = development_state_path(method_state_directory, owner)
    if path.exists():
        raise RuntimeError(f"{owner} development state already exists")
    freeze, method, cell_id, benchmark_ids = _campaign_context(experiment)
    policy = _configured_policy(experiment)
    raw_benchmarks = _mapping(
        policy.get("benchmarks"),
        "baseline.config.development_policy.benchmarks",
    )
    if set(raw_benchmarks) != set(benchmark_ids):
        raise ValueError(
            "baseline development policy must match active cell benchmarks"
        )

    resolved: dict[str, BenchmarkDevelopmentState] = {}
    for benchmark_id in benchmark_ids:
        raw = _mapping(
            raw_benchmarks[benchmark_id],
            f"baseline development benchmark {benchmark_id}",
        )
        if set(raw) != {"a_task_ids", "b_task_ids", "sampling"}:
            raise ValueError(
                "baseline development benchmark requires A, B, and sampling"
            )
        boundary = freeze.dataset_boundaries[benchmark_id].development_set
        a_task_ids = _task_ids(raw["a_task_ids"], "A task IDs")
        b_task_ids = _task_ids(raw["b_task_ids"], "B task IDs")
        _validate_partition(
            boundary.ordered_task_ids,
            a_task_ids=a_task_ids,
            b_task_ids=b_task_ids,
        )
        sampling, base_seed = _resolve_sampling(raw["sampling"], b_task_ids=b_task_ids)
        resolved[benchmark_id] = BenchmarkDevelopmentState(
            development_set_digest=boundary.digest,
            a_task_ids=a_task_ids,
            b_task_ids=b_task_ids,
            sampling=MappingProxyType(sampling),
            base_seed=base_seed,
        )

    state = BaselineDevelopmentState(
        owner=_owner(owner),
        method=method,
        cell_id=cell_id,
        benchmarks=MappingProxyType(resolved),
    )
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    _atomic_write_json(path, _state_payload(state))
    return state


def load_development_state(
    *,
    method_state_directory: Path,
    owner: str,
    experiment: Mapping[str, object],
) -> BaselineDevelopmentState:
    """Restore only the baseline's durable record; never recompute its split."""

    path = development_state_path(method_state_directory, owner)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{owner} durable development state is unavailable") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"{owner} durable development state is invalid")
    state = _state_from_payload(payload, owner=owner)
    freeze, method, cell_id, benchmark_ids = _campaign_context(experiment)
    if state.method != method or state.cell_id != cell_id:
        raise RuntimeError(f"{owner} durable development state is misbound")
    state = _bind_benchmark_order(state, benchmark_ids, owner=owner)
    for benchmark_id, benchmark in state.benchmarks.items():
        boundary = freeze.dataset_boundaries[benchmark_id].development_set
        if benchmark.development_set_digest != boundary.digest:
            raise RuntimeError(f"{owner} durable development boundary digest changed")
        _validate_partition(
            boundary.ordered_task_ids,
            a_task_ids=benchmark.a_task_ids,
            b_task_ids=benchmark.b_task_ids,
        )
    return state


def _campaign_context(
    experiment: Mapping[str, object],
) -> tuple[CampaignFreeze, str, str, tuple[str, ...]]:
    raw_benchmarks = experiment.get("benchmarks")
    if not isinstance(raw_benchmarks, list) or not raw_benchmarks:
        raise ValueError("stored benchmarks must be a non-empty array")
    authorities: list[tuple[str, CampaignFreeze, str]] = []
    for index, raw_benchmark in enumerate(raw_benchmarks):
        benchmark = _mapping(raw_benchmark, f"stored benchmarks[{index}]")
        benchmark_id = benchmark.get("name")
        if not isinstance(benchmark_id, str) or not benchmark_id:
            raise ValueError("stored benchmark name is invalid")
        resolved = _mapping(
            benchmark.get("resolved"), f"stored benchmarks[{index}].resolved"
        )
        identity = _mapping(
            resolved.get("adapter"), "stored benchmark adapter identity"
        )
        if identity.get("benchmark_id") != benchmark_id:
            raise ValueError("stored benchmark adapter identity changed")
        freeze = CampaignFreeze.from_mapping(
            _mapping(identity.get("campaign_freeze"), "stored campaign freeze")
        )
        cell_id = identity.get("cell_id")
        if not isinstance(cell_id, str) or not cell_id:
            raise ValueError("stored benchmark config lacks cell_id")
        authorities.append((benchmark_id, freeze, cell_id))
    benchmark_ids, freezes, cell_ids = zip(*authorities, strict=True)
    freeze = freezes[0]
    cell_id = cell_ids[0]
    if any(child != freeze for child in freezes) or any(
        child != cell_id for child in cell_ids
    ):
        raise ValueError("stored benchmark configs disagree on campaign authority")
    cell = freeze.cells.get(cell_id)
    if cell is None:
        raise ValueError("stored campaign freeze lacks the active cell")
    if set(benchmark_ids) != set(cell.benchmark_ids):
        raise ValueError("stored benchmarks do not match the active cell")
    return freeze, cell.method, cell_id, cell.benchmark_ids


def _configured_policy(experiment: Mapping[str, object]) -> Mapping[str, object]:
    baseline = _mapping(experiment.get("baseline"), "stored baseline")
    config = _mapping(baseline.get("config"), "stored baseline.config")
    policy = _mapping(
        config.get(_POLICY_FIELD),
        "stored baseline.config.development_policy",
    )
    if set(policy) != {"benchmarks"}:
        raise ValueError("baseline development policy requires benchmarks")
    return policy


def _resolve_sampling(
    value: object, *, b_task_ids: tuple[str, ...]
) -> tuple[dict[str, object], int]:
    raw = _mapping(value, "baseline sampling")
    if set(raw) != {"kind", "sample_size", "seed", "fixed_task_ids"}:
        raise ValueError(
            "baseline sampling requires kind, sample_size, seed, and fixed_task_ids"
        )
    kind = raw["kind"]
    sample_size = raw["sample_size"]
    base_seed = raw["seed"]
    fixed_task_ids = _task_ids(
        raw["fixed_task_ids"], "fixed task IDs", allow_empty=True
    )
    if (
        not isinstance(kind, str)
        or isinstance(sample_size, bool)
        or not isinstance(sample_size, int)
        or sample_size < 1
        or isinstance(base_seed, bool)
        or not isinstance(base_seed, int)
    ):
        raise ValueError("baseline sampling values are invalid")
    if kind != "random_subset" or fixed_task_ids or sample_size > len(b_task_ids):
        raise ValueError("shipped baseline random sampling is inconsistent")
    return (
        {
            "kind": kind,
            "sample_size": sample_size,
            "fixed_task_ids": fixed_task_ids,
        },
        base_seed,
    )


def _state_from_payload(
    payload: Mapping[str, object], *, owner: str
) -> BaselineDevelopmentState:
    if set(payload) != {"schema", "owner", "method", "cell_id", "benchmarks"}:
        raise RuntimeError(f"{owner} durable development state fields are invalid")
    if payload.get("schema") != _SCHEMA or payload.get("owner") != _owner(owner):
        raise RuntimeError(f"{owner} durable development state schema is invalid")
    method = payload.get("method")
    cell_id = payload.get("cell_id")
    if (
        not isinstance(method, str)
        or not method
        or not isinstance(cell_id, str)
        or not cell_id
    ):
        raise RuntimeError(f"{owner} durable development state identity is invalid")
    raw_benchmarks = _mapping(payload.get("benchmarks"), "durable benchmarks")
    if not raw_benchmarks:
        raise RuntimeError(f"{owner} durable development state has no benchmark")
    benchmarks: dict[str, BenchmarkDevelopmentState] = {}
    for benchmark_id, value in raw_benchmarks.items():
        if not isinstance(benchmark_id, str) or not benchmark_id:
            raise RuntimeError(f"{owner} durable benchmark ID is invalid")
        raw = _mapping(value, "durable benchmark state")
        if set(raw) != {
            "development_set_digest",
            "a_task_ids",
            "b_task_ids",
            "sampling",
            "base_seed",
        }:
            raise RuntimeError(f"{owner} durable benchmark fields are invalid")
        digest = raw["development_set_digest"]
        base_seed = raw["base_seed"]
        sampling = _mapping(raw["sampling"], "durable sampling")
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or isinstance(base_seed, bool)
            or not isinstance(base_seed, int)
            or set(sampling) != {"kind", "sample_size", "fixed_task_ids"}
        ):
            raise RuntimeError(f"{owner} durable benchmark state is invalid")
        a_task_ids = _task_ids(raw["a_task_ids"], "durable A task IDs")
        b_task_ids = _task_ids(raw["b_task_ids"], "durable B task IDs")
        resolved_sampling, resolved_seed = _resolve_sampling(
            {**dict(sampling), "seed": base_seed}, b_task_ids=b_task_ids
        )
        benchmarks[benchmark_id] = BenchmarkDevelopmentState(
            development_set_digest=digest,
            a_task_ids=a_task_ids,
            b_task_ids=b_task_ids,
            sampling=MappingProxyType(resolved_sampling),
            base_seed=resolved_seed,
        )
    return BaselineDevelopmentState(
        owner=_owner(owner),
        method=method,
        cell_id=cell_id,
        benchmarks=MappingProxyType(benchmarks),
    )


def _state_payload(state: BaselineDevelopmentState) -> dict[str, object]:
    return {
        "schema": _SCHEMA,
        "owner": state.owner,
        "method": state.method,
        "cell_id": state.cell_id,
        "benchmarks": {
            benchmark_id: {
                "development_set_digest": benchmark.development_set_digest,
                "a_task_ids": list(benchmark.a_task_ids),
                "b_task_ids": list(benchmark.b_task_ids),
                "sampling": {
                    "kind": benchmark.sampling["kind"],
                    "sample_size": benchmark.sampling["sample_size"],
                    "fixed_task_ids": list(benchmark.sampling["fixed_task_ids"]),
                },
                "base_seed": benchmark.base_seed,
            }
            for benchmark_id, benchmark in state.benchmarks.items()
        },
    }


def _bind_benchmark_order(
    state: BaselineDevelopmentState,
    benchmark_ids: Sequence[str],
    *,
    owner: str,
) -> BaselineDevelopmentState:
    ordered_ids = tuple(benchmark_ids)
    if set(state.benchmarks) != set(ordered_ids):
        raise RuntimeError(f"{owner} durable development state is misbound")
    return BaselineDevelopmentState(
        owner=state.owner,
        method=state.method,
        cell_id=state.cell_id,
        benchmarks=MappingProxyType(
            {
                benchmark_id: state.benchmarks[benchmark_id]
                for benchmark_id in ordered_ids
            }
        ),
    )


def _validate_partition(
    development_task_ids: Sequence[str],
    *,
    a_task_ids: tuple[str, ...],
    b_task_ids: tuple[str, ...],
) -> None:
    development = tuple(development_task_ids)
    if set(a_task_ids) & set(b_task_ids):
        raise ValueError("baseline A/B memberships must be disjoint")
    if set(a_task_ids) | set(b_task_ids) != set(development):
        raise ValueError("baseline A/B memberships must cover development")


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{field} must be a mapping")
    return value


def _task_ids(
    value: object, field: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field} must be an ordered sequence")
    task_ids = tuple(value)
    if (
        (not allow_empty and not task_ids)
        or any(not isinstance(task_id, str) or not task_id for task_id in task_ids)
        or len(set(task_ids)) != len(task_ids)
    ):
        raise ValueError(f"{field} are invalid")
    return task_ids


def _owner(value: str) -> str:
    if not isinstance(value, str) or not value or "/" in value or "\\" in value:
        raise ValueError("baseline development owner is invalid")
    return value


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "BaselineDevelopmentState",
    "BenchmarkDevelopmentState",
    "development_state_path",
    "initialize_development_state",
    "load_development_state",
]
