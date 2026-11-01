"""Static native-baseline entrypoint registry shared with the staged CLI."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import NamedTuple


class BaselineRegistration(NamedTuple):
    module: str
    launch: str
    resume: str


BASELINE_REGISTRY: Mapping[str, BaselineRegistration] = MappingProxyType(
    {
        "codex": BaselineRegistration(
            "rsi_arena.baselines.codex",
            "run_new_codex",
            "resume_codex",
        ),
        "dsh": BaselineRegistration(
            "rsi_arena.baselines.dsh",
            "run_new_dsh",
            "resume_dsh",
        ),
        "openevolve": BaselineRegistration(
            "rsi_arena.baselines.openevolve",
            "launch_openevolve",
            "resume_openevolve",
        ),
        "shinkaevolve": BaselineRegistration(
            "rsi_arena.baselines.shinka",
            "launch_shinka",
            "resume_shinka",
        ),
    }
)


__all__ = ["BASELINE_REGISTRY", "BaselineRegistration"]
