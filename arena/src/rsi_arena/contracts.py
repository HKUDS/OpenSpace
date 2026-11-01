"""Small immutable contracts shared by the Arena kernel and its extras."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# Canonical digests use sorted keys, reject non-finite floats, and normalize
# negative zero.  These semantics are shared by persisted identities.
def _normalise(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("NaN/inf are not allowed in canonical JSON")
        return 0.0 if value == 0.0 else value
    if isinstance(value, dict):
        return {
            str(key): _normalise(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def canonical_json(payload: Any) -> str:
    return json.dumps(
        _normalise(payload),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def canonical_digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class SampleOutcomeKind(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    TIMEOUT = "timeout"


class MetricRole(str, Enum):
    PRIMARY = "primary"
    CONSTRAINT = "constraint"
    AUXILIARY = "auxiliary"
    DESCRIPTOR = "descriptor"


class MetricDirection(str, Enum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


class EnvProfile(str, Enum):
    OFFLINE_DETERMINISTIC = "offline_deterministic"
    OFFLINE_STOCHASTIC = "offline_stochastic"
    ONLINE_LIVE = "online_live"


@dataclass(frozen=True)
class MetricSpec:
    name: str
    direction: str
    role: str
    unit: Optional[str] = None


@dataclass(frozen=True)
class MetricSchema:
    metrics: tuple[MetricSpec, ...]

    def __post_init__(self) -> None:
        primaries = [
            metric for metric in self.metrics if metric.role == MetricRole.PRIMARY.value
        ]
        if len(primaries) != 1:
            raise ValueError(
                "MetricSchema must declare exactly one primary metric, "
                f"got {len(primaries)}"
            )

    @property
    def primary(self) -> MetricSpec:
        return next(
            metric for metric in self.metrics if metric.role == MetricRole.PRIMARY.value
        )

    @property
    def constraints(self) -> tuple[MetricSpec, ...]:
        return tuple(
            metric
            for metric in self.metrics
            if metric.role == MetricRole.CONSTRAINT.value
        )


@dataclass(frozen=True)
class TaskRef:
    task_id: str
    cluster: str = "default"
    repeatable: bool = True
    est_cost_usd: float = 0.0
    meta: dict = field(default_factory=dict)
    benchmark_id: str = field(default="", kw_only=True)

    @property
    def canonical_identity(self) -> tuple[str, str]:
        """Return the cross-benchmark task identity."""

        return (self.benchmark_id, self.task_id)


@dataclass(frozen=True)
class CellBudget:
    wall_seconds: float = 300.0
    max_cost_usd: Optional[float] = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.wall_seconds, bool)
            or not isinstance(self.wall_seconds, (int, float))
            or not math.isfinite(float(self.wall_seconds))
            or float(self.wall_seconds) <= 0.0
        ):
            raise ValueError("cell wall budget must be positive and finite")
        if self.max_cost_usd is not None and (
            isinstance(self.max_cost_usd, bool)
            or not isinstance(self.max_cost_usd, (int, float))
            or not math.isfinite(float(self.max_cost_usd))
            or float(self.max_cost_usd) < 0.0
        ):
            raise ValueError("cell cost budget must be nonnegative and finite")


@dataclass(frozen=True)
class BenchIdentity:
    name: str
    judge_identity: str = "none"

    @property
    def digest(self) -> str:
        return canonical_digest({"name": self.name, "judge": self.judge_identity})


@dataclass(frozen=True)
class ScopeSpec:
    """Editable and protected glob patterns over repository-relative paths."""

    editable: tuple[str, ...]
    protected: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"editable": list(self.editable), "protected": list(self.protected)}

    @classmethod
    def from_dict(cls, payload: dict) -> "ScopeSpec":
        return cls(
            editable=tuple(payload.get("editable", ())),
            protected=tuple(payload.get("protected", ())),
        )
