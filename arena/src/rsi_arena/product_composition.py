"""Product-owned benchmark construction for the generic Arena composition."""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from rsi_arena.composition import (
    BenchmarkConstructionError,
    FrozenBenchmarkPackage,
    GenericArenaComposition,
)
from rsi_arena.composition import (
    compose_new_generic_run as compose_new_kernel_run,
)
from rsi_arena.composition import (
    compose_resumed_generic_run as compose_resumed_kernel_run,
)
from rsi_arena.experiment import JSONObject
from rsi_arena.inner_agent_runtime import (
    FrozenInnerAgentSelection,
    InnerAgentSelectionError,
    freeze_inner_agent_selection,
    validate_inner_agent_package_support,
)
from rsi_arena.run_layout import RunValidationError


@dataclass(frozen=True)
class BenchmarkRegistration:
    module: str
    freeze: str
    resume: str
    publication_policy: bool = False


BENCHMARK_REGISTRY: Mapping[str, BenchmarkRegistration] = MappingProxyType(
    {
        "aime": BenchmarkRegistration(
            "rsi_arena.benchmarks.aime.composition",
            "freeze_aime_benchmark",
            "resume_aime_benchmark",
        ),
        "tb2": BenchmarkRegistration(
            "rsi_arena.benchmarks.tb21.composition",
            "freeze_tb21_benchmark",
            "resume_tb21_benchmark",
        ),
        "swe": BenchmarkRegistration(
            "rsi_arena.benchmarks.swe.composition",
            "freeze_swe_benchmark",
            "resume_swe_benchmark",
        ),
        "browsecomp_plus": BenchmarkRegistration(
            "rsi_arena.benchmarks.browsecomp_plus.production",
            "freeze_product_browsecomp_plus_benchmark",
            "resume_product_browsecomp_plus_benchmark",
            publication_policy=True,
        ),
    }
)


def _construct_benchmark(
    benchmark: Mapping[str, object],
    *,
    control_root: Path,
    inner_agent_selection: FrozenInnerAgentSelection,
    publish_development_judge_text: bool,
    resumed: bool,
) -> FrozenBenchmarkPackage:
    name = str(benchmark["name"])
    registration = BENCHMARK_REGISTRY.get(name)
    if registration is None:
        raise BenchmarkConstructionError(
            f"unsupported benchmark at construction: {name!r}"
        )
    config = benchmark["config"]
    assert isinstance(config, dict)
    constructor = getattr(
        importlib.import_module(registration.module),
        registration.resume if resumed else registration.freeze,
    )
    keywords: dict[str, object] = {
        "control_root": control_root,
        "inner_agent_selection": inner_agent_selection,
    }
    if registration.publication_policy:
        keywords["publish_development_judge_text"] = publish_development_judge_text
    if not resumed:
        return constructor(config, **keywords)
    resolved = benchmark["resolved"]
    assert isinstance(resolved, dict)
    identity = resolved.get("adapter")
    if not isinstance(identity, Mapping):
        raise RunValidationError("stored benchmark identity is unavailable")
    return constructor(config, identity, **keywords)


def _packages(
    resolved: JSONObject,
    *,
    control_root: Path,
    resumed: bool,
    inner_agent_selection: FrozenInnerAgentSelection,
) -> Mapping[str, FrozenBenchmarkPackage]:
    raw_benchmarks = resolved["benchmarks"]
    assert isinstance(raw_benchmarks, list)
    baseline = resolved["baseline"]
    assert isinstance(baseline, dict)
    baseline_config = baseline["config"]
    assert isinstance(baseline_config, dict)
    # Method-owned publication policy is consumed before kernel composition.
    publish_development_judge_text = baseline_config.get(
        "publish_development_judge_text", False
    )
    if not isinstance(publish_development_judge_text, bool):
        raise RunValidationError(
            "baseline.config.publish_development_judge_text must be a boolean"
        )
    names: set[str] = set()
    for benchmark in raw_benchmarks:
        assert isinstance(benchmark, dict)
        name = benchmark["name"]
        assert isinstance(name, str)
        if name not in BENCHMARK_REGISTRY:
            raise BenchmarkConstructionError(
                f"unsupported benchmark at construction: {name!r}"
            )
        if name in names:
            raise BenchmarkConstructionError(f"duplicate benchmark package: {name!r}")
        names.add(name)

    packages: dict[str, FrozenBenchmarkPackage] = {}
    try:
        for benchmark in raw_benchmarks:
            assert isinstance(benchmark, dict)
            name = str(benchmark["name"])
            package = _construct_benchmark(
                benchmark,
                control_root=control_root,
                inner_agent_selection=inner_agent_selection,
                publish_development_judge_text=publish_development_judge_text,
                resumed=resumed,
            )
            if package.benchmark_id in packages:
                raise BenchmarkConstructionError(
                    f"duplicate benchmark package: {package.benchmark_id!r}"
                )
            packages[package.benchmark_id] = package
    except BaseException:
        _close_packages(packages)
        raise
    return MappingProxyType(packages)


def _close_packages(packages: Mapping[str, object]) -> None:
    """Best-effort cleanup used only while a composition is not yet returned."""

    for package in reversed(tuple(packages.values())):
        close = getattr(package, "close", None)
        if callable(close):
            try:
                close()
            except BaseException:
                pass


def _active_benchmark_ids(
    resolved: JSONObject,
) -> tuple[str, ...]:
    """Return ordered benchmark IDs while enforcing the cell-wide authority."""

    raw_benchmarks = resolved["benchmarks"]
    assert isinstance(raw_benchmarks, list)
    benchmark_ids: list[str] = []
    for benchmark in raw_benchmarks:
        assert isinstance(benchmark, dict)
        benchmark_id = str(benchmark["name"])
        config = benchmark["config"]
        assert isinstance(config, dict)
        if "inner_agent" in config:
            raise InnerAgentSelectionError(
                "inner_agent is valid only at benchmark_runtime.inner_agent; "
                f"benchmarks[{benchmark_id!r}].config cannot define it"
            )
        benchmark_ids.append(benchmark_id)
    return tuple(benchmark_ids)


def compose_new_generic_run(
    resolved: JSONObject,
    *,
    run_root: str | os.PathLike[str],
    workspace_source: str | os.PathLike[str],
    trusted_candidate_source: str | os.PathLike[str] | None = None,
) -> GenericArenaComposition:
    benchmark_ids = _active_benchmark_ids(resolved)
    control_root = Path(run_root).expanduser().absolute()
    inner_agent_selection = freeze_inner_agent_selection(resolved, resumed=False)
    validate_inner_agent_package_support(inner_agent_selection, benchmark_ids)
    control_root.mkdir(parents=True, exist_ok=True)
    control_root = control_root.resolve(strict=True)
    packages = _packages(
        resolved,
        control_root=control_root,
        resumed=False,
        inner_agent_selection=inner_agent_selection,
    )
    try:
        return compose_new_kernel_run(
            resolved,
            packages,
            run_root=control_root,
            workspace_source=workspace_source,
            trusted_candidate_source=trusted_candidate_source,
        )
    except BaseException:
        _close_packages(packages)
        raise


def compose_resumed_generic_run(
    resolved: JSONObject,
    *,
    run_root: str | os.PathLike[str],
) -> GenericArenaComposition:
    benchmark_ids = _active_benchmark_ids(resolved)
    control_root = Path(run_root).expanduser().resolve(strict=True)
    inner_agent_selection = freeze_inner_agent_selection(resolved, resumed=True)
    validate_inner_agent_package_support(inner_agent_selection, benchmark_ids)
    packages = _packages(
        resolved,
        control_root=control_root,
        resumed=True,
        inner_agent_selection=inner_agent_selection,
    )
    try:
        return compose_resumed_kernel_run(
            resolved,
            packages,
            run_root=control_root,
        )
    except BaseException:
        _close_packages(packages)
        raise


__all__ = [
    "BENCHMARK_REGISTRY",
    "BenchmarkRegistration",
    "compose_new_generic_run",
    "compose_resumed_generic_run",
]
