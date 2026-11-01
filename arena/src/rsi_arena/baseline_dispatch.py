"""CLI dispatch into the native launchers shipped for Arena baselines."""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .baseline_registry import BASELINE_REGISTRY, BaselineRegistration
from .experiment import JSONObject, read_experiment, resolve_experiment
from .manifests import CampaignFreeze


class BaselineDispatchError(ValueError):
    """The selected launcher disagrees with the frozen campaign method."""


def _campaign_method(experiment: Mapping[str, object]) -> tuple[JSONObject, str]:
    resolved = resolve_experiment(experiment)
    benchmarks = resolved["benchmarks"]
    assert isinstance(benchmarks, list)
    authority: tuple[str, str] | None = None
    method: str | None = None
    for index, benchmark in enumerate(benchmarks):
        assert isinstance(benchmark, dict)
        config = benchmark["config"]
        assert isinstance(config, dict)
        raw_freeze = config.get("campaign_freeze")
        cell_id = config.get("cell_id")
        if not isinstance(raw_freeze, Mapping) or not isinstance(cell_id, str):
            raise BaselineDispatchError(
                f"benchmarks[{index}].config requires campaign_freeze and cell_id"
            )
        freeze = CampaignFreeze.from_mapping(raw_freeze)
        cell = freeze.cells.get(cell_id)
        if cell is None:
            raise BaselineDispatchError(
                f"benchmarks[{index}] references an unknown campaign cell"
            )
        benchmark_name = benchmark["name"]
        assert isinstance(benchmark_name, str)
        if benchmark_name not in cell.benchmark_ids:
            raise BaselineDispatchError(
                f"benchmarks[{index}] is absent from its campaign cell"
            )
        current = (freeze.freeze_id, cell_id)
        if authority is None:
            authority = current
        elif current != authority:
            raise BaselineDispatchError(
                "benchmarks do not share one campaign freeze and active cell"
            )
        if method is None:
            method = cell.method
        elif cell.method != method:
            raise BaselineDispatchError("campaign benchmarks disagree on method")
    assert method is not None
    return resolved, method


def _registration(
    baseline: str,
    experiment: Mapping[str, object],
) -> tuple[JSONObject, BaselineRegistration]:
    resolved, method = _campaign_method(experiment)
    try:
        registration = BASELINE_REGISTRY[baseline]
    except KeyError as exc:
        raise BaselineDispatchError(f"unsupported native baseline: {baseline}") from exc
    if baseline != method:
        raise BaselineDispatchError(
            f"selected baseline {baseline!r} disagrees with campaign method {method!r}"
        )
    return resolved, registration


def _load_entrypoint(
    registration: BaselineRegistration, name: str
) -> Callable[..., Any]:
    entrypoint = getattr(importlib.import_module(registration.module), name)
    if not callable(entrypoint):  # pragma: no cover - registry is package-owned
        raise BaselineDispatchError("registered baseline entrypoint is not callable")
    return entrypoint


def launch_baseline(
    baseline: str,
    experiment: Mapping[str, object],
    *,
    run_root: str | os.PathLike[str],
    workspace_root: str | os.PathLike[str],
) -> Any:
    """Validate campaign ownership and invoke a native new-run entrypoint."""

    resolved, registration = _registration(baseline, experiment)
    return _load_entrypoint(registration, registration.launch)(
        resolved,
        run_root=run_root,
        workspace_root=workspace_root,
    )


def resume_baseline(
    baseline: str,
    *,
    run_root: str | os.PathLike[str],
) -> Any:
    """Validate the stored campaign method and invoke its native resume entrypoint."""

    experiment = read_experiment(Path(run_root))
    _resolved, registration = _registration(baseline, experiment)
    return _load_entrypoint(registration, registration.resume)(run_root=run_root)


__all__ = [
    "BaselineDispatchError",
    "launch_baseline",
    "resume_baseline",
]
