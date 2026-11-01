"""TOML authoring and resolved Arena experiment run records.

``experiment.toml`` is parsed once at the launch boundary.  The resolver and
all programmatic launch/session APIs accept only in-memory mappings, while the
write-once ``experiment.json`` remains the runtime and resume authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import TypeAlias

EXPERIMENT_FILENAME = "experiment.json"

JSONPrimitive: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONPrimitive | list["JSONValue"] | dict[str, "JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]

_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_EXPERIMENT_FIELDS = frozenset(
    {"arena", "baseline", "benchmark_runtime", "benchmarks", "execution", "source"}
)
_BASELINE_FIELDS = frozenset({"candidate_root", "command", "config", "cwd", "env"})
_BENCHMARK_FIELDS = frozenset({"config", "name", "resolved"})
_ARENA_FIELDS = frozenset(
    {
        "attempt_cap",
        "authority",
        "concurrency",
        "invalid_cell_retries",
        "wall_seconds",
    }
)
_EXECUTION_FIELDS = frozenset({"profile"})


class ExperimentDocumentError(ValueError):
    """The supplied config cannot form the resolved experiment authority."""


class ExperimentAlreadyExistsError(FileExistsError):
    """A write-once experiment record already occupies the run directory."""


def _json_value(value: object, *, label: str) -> JSONValue:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExperimentDocumentError(f"{label} contains a non-JSON number")
        return value
    if isinstance(value, Mapping):
        result: JSONObject = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ExperimentDocumentError(
                    f"{label} contains a non-string object key"
                )
            result[key] = _json_value(child, label=f"{label}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [
            _json_value(child, label=f"{label}[{index}]")
            for index, child in enumerate(value)
        ]
    raise ExperimentDocumentError(f"{label} contains a non-JSON value")


def _object(value: object, *, label: str) -> JSONObject:
    if not isinstance(value, Mapping):
        raise ExperimentDocumentError(f"{label} must be a JSON object")
    normalized = _json_value(value, label=label)
    assert isinstance(normalized, dict)
    return normalized


def load_experiment_toml(path: str | os.PathLike[str]) -> JSONObject:
    """Parse one human-authored TOML file into a JSON-compatible mapping."""

    try:
        with Path(path).open("rb") as source:
            decoded = tomllib.load(source)
    except OSError as exc:
        raise ExperimentDocumentError("experiment TOML is unreadable") from exc
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ExperimentDocumentError("experiment TOML is invalid") from exc
    return _object(decoded, label="experiment")


def _mapping_field(
    owner: JSONObject,
    name: str,
    *,
    label: str,
    default: Mapping[str, object] | None = None,
) -> JSONObject:
    if name not in owner:
        owner[name] = _object(default or {}, label=f"{label}.{name}")
    value = owner[name]
    if not isinstance(value, dict):
        raise ExperimentDocumentError(f"{label}.{name} must be a JSON object")
    return value


def _environment_references(value: JSONObject) -> None:
    for target_name, source_name in value.items():
        if _ENVIRONMENT_NAME.fullmatch(target_name) is None:
            raise ExperimentDocumentError(
                "baseline.env keys must be environment variable names"
            )
        if (
            not isinstance(source_name, str)
            or _ENVIRONMENT_NAME.fullmatch(source_name) is None
        ):
            raise ExperimentDocumentError(
                "baseline.env values must name source environment variables"
            )


def _require_exact_fields(
    value: Mapping[str, object], allowed: frozenset[str], *, label: str
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ExperimentDocumentError(
            f"{label} contains unknown fields: {', '.join(sorted(unknown))}"
        )


def resolve_experiment(source: Mapping[str, object]) -> JSONObject:
    """Return the canonical resolved shape without applying experiment policy.

    ``baseline.config``, each ``benchmarks[].config`` /
    ``benchmarks[].resolved``, and
    ``execution`` remain JSON objects with explicit structural fields. Values
    inside method and benchmark ``config`` objects remain opaque.
    Values such as model, temperature, reasoning effort, timeouts, caps, and
    concurrency are not interpreted here.
    """

    if not isinstance(source, Mapping):
        raise TypeError("experiment input must be an in-memory mapping")
    resolved = _object(source, label="experiment")
    _require_exact_fields(resolved, _EXPERIMENT_FIELDS, label="experiment")

    if "baseline" not in resolved:
        raise ExperimentDocumentError("experiment.baseline must be a JSON object")
    baseline = _mapping_field(resolved, "baseline", label="experiment")
    baseline.setdefault("cwd", ".")
    baseline.setdefault("candidate_root", ".")
    _mapping_field(baseline, "config", label="baseline")
    baseline_env = _mapping_field(baseline, "env", label="baseline")
    _require_exact_fields(baseline, _BASELINE_FIELDS, label="baseline")
    _environment_references(baseline_env)

    if "benchmark_runtime" not in resolved:
        raise ExperimentDocumentError(
            "experiment.benchmark_runtime must be a JSON object"
        )
    benchmark_runtime = _mapping_field(
        resolved, "benchmark_runtime", label="experiment"
    )
    if set(benchmark_runtime) != {"inner_agent"}:
        raise ExperimentDocumentError(
            "benchmark_runtime must contain exactly inner_agent"
        )
    inner_agent = _mapping_field(
        benchmark_runtime, "inner_agent", label="benchmark_runtime"
    )
    unknown_inner_agent_fields = set(inner_agent) - {"ref", "config", "resolved"}
    if unknown_inner_agent_fields:
        raise ExperimentDocumentError(
            "benchmark_runtime.inner_agent contains unknown fields: "
            + ", ".join(sorted(unknown_inner_agent_fields))
        )
    inner_agent_ref = inner_agent.get("ref")
    if not isinstance(inner_agent_ref, str) or not inner_agent_ref:
        raise ExperimentDocumentError(
            "benchmark_runtime.inner_agent.ref must be a non-empty string"
        )
    _mapping_field(inner_agent, "config", label="benchmark_runtime.inner_agent")
    _mapping_field(inner_agent, "resolved", label="benchmark_runtime.inner_agent")

    raw_benchmarks = resolved.get("benchmarks")
    if not isinstance(raw_benchmarks, list) or not raw_benchmarks:
        raise ExperimentDocumentError(
            "experiment.benchmarks must be a non-empty JSON array"
        )
    for index, raw_benchmark in enumerate(raw_benchmarks):
        if not isinstance(raw_benchmark, dict):
            raise ExperimentDocumentError(
                f"experiment.benchmarks[{index}] must be a JSON object"
            )
        benchmark_name = raw_benchmark.get("name")
        if not isinstance(benchmark_name, str) or not benchmark_name.strip():
            raise ExperimentDocumentError(
                f"experiment.benchmarks[{index}].name must be a non-empty string"
            )
        _mapping_field(
            raw_benchmark,
            "config",
            label=f"benchmarks[{index}]",
        )
        _mapping_field(
            raw_benchmark,
            "resolved",
            label=f"benchmarks[{index}]",
        )
        _require_exact_fields(
            raw_benchmark,
            _BENCHMARK_FIELDS,
            label=f"benchmarks[{index}]",
        )

    arena = _mapping_field(resolved, "arena", label="experiment")
    _require_exact_fields(arena, _ARENA_FIELDS, label="arena")
    execution = _mapping_field(resolved, "execution", label="experiment")
    _require_exact_fields(execution, _EXECUTION_FIELDS, label="execution")
    if "source" in resolved:
        _mapping_field(resolved, "source", label="experiment")
    return resolved


def canonical_experiment_bytes(source: Mapping[str, object]) -> bytes:
    """Resolve and serialize an experiment without a trailing newline."""

    normalized = resolve_experiment(source)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def experiment_digest(source: Mapping[str, object]) -> str:
    """Return the lowercase sha256 hex digest of canonical experiment bytes."""

    return hashlib.sha256(canonical_experiment_bytes(source)).hexdigest()


def _write_all(file_descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(file_descriptor, view)
        if written <= 0:  # pragma: no cover - the OS either writes or raises
            raise OSError("short write while storing the experiment record")
        view = view[written:]


def _fsync_directory(root: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor = os.open(root, flags)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def write_experiment(
    run_root: str | os.PathLike[str], source: Mapping[str, object]
) -> None:
    """Resolve and durably create the sole ``experiment.json`` authority."""

    resolved = resolve_experiment(source)
    canonical = canonical_experiment_bytes(resolved)

    root = Path(run_root).expanduser().absolute()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise NotADirectoryError(f"run root is not a directory: {root}")

    experiment_path = root / EXPERIMENT_FILENAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    created_paths: list[Path] = []
    descriptors: list[int] = []
    completed = False
    try:
        experiment_descriptor = os.open(experiment_path, flags, 0o600)
        descriptors.append(experiment_descriptor)
        created_paths.append(experiment_path)
        _write_all(experiment_descriptor, canonical)
        os.fsync(experiment_descriptor)
        _fsync_directory(root)
        completed = True
    except FileExistsError as exc:
        raise ExperimentAlreadyExistsError(
            f"experiment record already exists under {root}"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if not completed:
            # Remove only partials created by this failed call; never inspect
            # or unlink an existing record that this call did not create.
            for path in reversed(created_paths):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            _fsync_directory(root)


def _read_no_follow(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def read_experiment(run_root: str | os.PathLike[str]) -> JSONObject:
    """Read the resolved run authority without consulting an authoring source."""

    root = Path(run_root).expanduser().absolute()
    raw = _read_no_follow(root / EXPERIMENT_FILENAME)
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentDocumentError("stored experiment.json is invalid") from exc
    return resolve_experiment(decoded)


__all__ = [
    "EXPERIMENT_FILENAME",
    "ExperimentAlreadyExistsError",
    "ExperimentDocumentError",
    "JSONObject",
    "canonical_experiment_bytes",
    "experiment_digest",
    "load_experiment_toml",
    "read_experiment",
    "resolve_experiment",
    "write_experiment",
]
