"""Dependency-free primitives shared by the AgentPilot domain packages."""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

_ID_NAMESPACE = uuid.UUID("92c6e6ba-0893-4e73-a8cb-d831d279634b")


def require_text(value: str, field_name: str) -> str:
    """Return a stripped non-empty string or fail at the domain boundary."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def require_aware(value: datetime, field_name: str) -> datetime:
    """Require a timezone-aware timestamp.

    Cutoff comparisons with naive datetimes are process-locale dependent and
    therefore unsafe for replay.
    """

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FrozenMapping(Mapping):
    """Small recursively immutable mapping used by frozen domain records."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping | None = None):
        self._values = {
            key: deep_freeze(value) for key, value in dict(values or {}).items()
        }

    def __getitem__(self, key):
        return self._values[key]

    def __iter__(self) -> Iterator:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"FrozenMapping({self._values!r})"

    def __deepcopy__(self, memo):
        return self


def deep_freeze(value: Any) -> Any:
    if isinstance(value, FrozenMapping):
        return value
    if isinstance(value, Mapping):
        return FrozenMapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(deep_freeze(item) for item in value)
    return value


def _canonical(value: Any) -> Any:
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        require_aware(value, "datetime")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical data cannot contain NaN or infinity")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Serialize domain facts deterministically without importing ``core``."""

    return json.dumps(
        _canonical(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def stable_id(kind: str, *coordinates: object) -> str:
    """Build a replay-stable, human-prefixed UUID from immutable coordinates."""

    prefix = require_text(kind, "kind")
    if not coordinates:
        raise ValueError("stable_id needs at least one coordinate")
    payload = canonical_json([prefix, *coordinates])
    return f"{prefix}-{uuid.uuid5(_ID_NAMESPACE, payload)}"
