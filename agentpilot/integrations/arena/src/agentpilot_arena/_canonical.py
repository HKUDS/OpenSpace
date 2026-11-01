"""Dependency-free canonical JSON identities used by benchmark assets."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Mapping


def _normalize(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NaN and infinity are not allowed in canonical JSON")
        return 0.0 if value == 0.0 else value
    if is_dataclass(value) and not isinstance(value, type):
        return _normalize(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _normalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, Enum):
        return _normalize(value.value)
    return value


def canonical_json(payload: Any) -> str:
    return json.dumps(
        _normalize(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = ["canonical_digest", "canonical_json"]
