"""Verifier runtime settings shared by Harbor benchmarks."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} field names must be strings")
    return value


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class VerifierSettings:
    """Verifier timeout multiplier and task-local environment adaptations."""

    timeout_multiplier: float
    task_adaptations: Mapping[str, Mapping[str, object]]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> VerifierSettings:
        if set(raw) != {"timeout_multiplier", "task_adaptations"}:
            raise ValueError(
                "verifier settings require timeout_multiplier and task_adaptations"
            )
        multiplier = raw["timeout_multiplier"]
        if (
            isinstance(multiplier, bool)
            or not isinstance(multiplier, (int, float))
            or not math.isfinite(float(multiplier))
            or float(multiplier) <= 0.0
        ):
            raise ValueError("verifier timeout multiplier must be positive and finite")

        raw_adaptations = _mapping(raw["task_adaptations"], "verifier task adaptations")
        adaptations: dict[str, Mapping[str, object]] = {}
        for raw_task_id, raw_adaptation in sorted(raw_adaptations.items()):
            task_id = _nonempty(raw_task_id, "verifier adaptation task ID")
            adaptation = _mapping(raw_adaptation, f"verifier adaptation {task_id!r}")
            if set(adaptation) != {"verifier_env"}:
                raise ValueError(
                    f"verifier adaptation {task_id!r} requires verifier_env"
                )
            raw_env = _mapping(
                adaptation["verifier_env"],
                f"verifier adaptation {task_id!r} environment",
            )
            verifier_env = {
                _nonempty(name, "verifier environment name"): _nonempty(
                    value, f"verifier environment {name}"
                )
                for name, value in sorted(raw_env.items())
            }
            adaptations[task_id] = MappingProxyType(
                {"verifier_env": MappingProxyType(verifier_env)}
            )
        return cls(
            timeout_multiplier=float(multiplier),
            task_adaptations=MappingProxyType(adaptations),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "timeout_multiplier": self.timeout_multiplier,
            "task_adaptations": {
                task_id: {"verifier_env": dict(adaptation["verifier_env"])}
                for task_id, adaptation in sorted(self.task_adaptations.items())
            },
        }

    def resolved_task_settings(self, task_id: str) -> dict[str, object]:
        adaptation = self.task_adaptations.get(task_id)
        return {
            "timeout_multiplier": self.timeout_multiplier,
            "task_adaptation": (
                None
                if adaptation is None
                else {"verifier_env": dict(adaptation["verifier_env"])}
            ),
        }
