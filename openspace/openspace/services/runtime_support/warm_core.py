from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from openspace.services.runtime_support.background import BackgroundSupervisor, get_background_supervisor


@dataclass(slots=True)
class WarmCore:
    """Process-level low-latency cache container.

    WarmCore intentionally does not own mutable per-session runtime objects.
    It only exposes shared immutable snapshots and explicit shared services
    that are safe by construction, such as the background supervisor.
    """

    config_snapshot: dict[str, Any] = field(default_factory=dict)
    model_capability_cache: dict[str, Any] = field(default_factory=dict)
    skill_metadata_cache: dict[str, Any] = field(default_factory=dict)
    background_supervisor: BackgroundSupervisor = field(
        default_factory=get_background_supervisor
    )
    created_at_ms: float = field(default_factory=lambda: time.time() * 1000.0)

    @classmethod
    def from_config(cls, config: Any) -> "WarmCore":
        return cls(config_snapshot=_snapshot_config(config))

    def snapshot(self) -> dict[str, Any]:
        return {
            "created_at_ms": self.created_at_ms,
            "config_snapshot": copy.deepcopy(self.config_snapshot),
            "model_capability_cache_keys": sorted(self.model_capability_cache.keys()),
            "skill_metadata_cache_keys": sorted(self.skill_metadata_cache.keys()),
            "background_supervisor": self.background_supervisor.status(),
        }


def _snapshot_config(config: Any) -> dict[str, Any]:
    if config is None:
        return {}
    model_dump = getattr(config, "model_dump", None)
    if callable(model_dump):
        try:
            return dict(model_dump(mode="python"))
        except TypeError:
            return dict(model_dump())
    if isinstance(config, Mapping):
        return dict(config)
    if hasattr(config, "__dict__"):
        return {
            key: copy.deepcopy(value)
            for key, value in vars(config).items()
            if not key.startswith("_")
        }
    return {"repr": repr(config)}


__all__ = ["WarmCore"]
