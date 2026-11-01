"""Build the immutable, cutoff-safe state shared by one search batch."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from agentpilot.domain import (
    CompatibilityKey,
    EvidenceTier,
    EvidenceCutoff,
    SearchState,
    StateDescriptor,
    stable_id,
)
from agentpilot.domain.common import (
    canonical_json,
    deep_freeze,
    require_aware,
    require_text,
)

@dataclass(frozen=True)
class SearchFact:
    """One completed fact eligible for a future SearchState.

    Facts become visible by ``available_at`` rather than by row insertion or
    attempt creation time.  This keeps a slow evaluation that finishes after
    the batch cutoff from leaking into the frozen policy state.
    """

    fact_id: str
    kind: str
    available_at: datetime
    evidence_tier: EvidenceTier
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_text(self.fact_id, "fact_id")
        require_text(self.kind, "kind")
        require_aware(self.available_at, "available_at")
        if not isinstance(self.evidence_tier, EvidenceTier):
            raise TypeError("evidence_tier must be an EvidenceTier")
        if self.evidence_tier is EvidenceTier.SEALED_C:
            raise ValueError("sealed C evidence cannot enter SearchState")
        if not isinstance(self.payload, Mapping):
            raise ValueError("fact payload must be a mapping")
        # Fail before a paid run when the payload cannot be serialized.
        canonical_json(dict(self.payload))
        object.__setattr__(self, "payload", deep_freeze(self.payload))


def cutoff_safe_facts(
    facts: Iterable[SearchFact], cutoff: EvidenceCutoff
) -> tuple[SearchFact, ...]:
    """Return a deterministic, duplicate-free view visible at ``cutoff``."""

    by_id: dict[str, SearchFact] = {}
    for fact in facts:
        if not isinstance(fact, SearchFact):
            raise TypeError("cutoff_safe_facts accepts SearchFact values")
        previous = by_id.get(fact.fact_id)
        if previous is not None and previous != fact:
            raise ValueError(f"fact id {fact.fact_id!r} has semantic drift")
        by_id[fact.fact_id] = fact
    visible = [fact for fact in by_id.values() if cutoff.allows(fact.available_at)]
    return tuple(
        sorted(visible, key=lambda fact: (fact.available_at, fact.kind, fact.fact_id),)
    )


def _freeze(value: Any) -> Any:
    """Deep-copy JSON-like state into immutable containers."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


class SearchStateBuilder:
    """Construct the sole D1/D2/D3 input before any policy call is made."""

    def build(
        self,
        *,
        run_id: str,
        batch_id: str,
        cutoff: EvidenceCutoff,
        compatibility: CompatibilityKey,
        archive_seq: int,
        parent_descriptor: StateDescriptor,
        direction_descriptor: StateDescriptor,
        budget: Mapping[str, float | int],
        facts: Iterable[SearchFact],
    ) -> SearchState:
        require_text(run_id, "run_id")
        require_text(batch_id, "batch_id")
        if not isinstance(cutoff, EvidenceCutoff):
            raise TypeError("cutoff must be EvidenceCutoff")
        if not isinstance(compatibility, CompatibilityKey):
            raise TypeError("compatibility must be CompatibilityKey")
        if isinstance(archive_seq, bool) or not isinstance(archive_seq, int):
            raise ValueError("archive_seq must be an integer")
        if archive_seq < 0:
            raise ValueError("archive_seq must be non-negative")
        if not isinstance(budget, Mapping):
            raise ValueError("budget must be a mapping")

        frozen_budget: dict[str, float | int] = {}
        for raw_name, raw_value in budget.items():
            name = require_text(str(raw_name), "budget key")
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                raise ValueError(f"budget {name!r} must be numeric")
            if not math.isfinite(float(raw_value)) or raw_value < 0:
                raise ValueError(f"budget {name!r} must be finite and non-negative")
            frozen_budget[name] = raw_value

        visible = cutoff_safe_facts(facts, cutoff)
        fact_payload = {
            "items": [
                {
                    "fact_id": fact.fact_id,
                    "kind": fact.kind,
                    "available_at": fact.available_at,
                    "evidence_tier": fact.evidence_tier.value,
                    "payload": dict(fact.payload),
                }
                for fact in visible
            ],
        }
        identity_payload = {
            "run_id": run_id,
            "batch_id": batch_id,
            "cutoff": cutoff,
            "compatibility": compatibility,
            "archive_seq": archive_seq,
            "parent_descriptor": parent_descriptor,
            "direction_descriptor": direction_descriptor,
            "budget": frozen_budget,
            "facts": fact_payload,
        }
        state_id = stable_id("search-state", identity_payload)
        return SearchState(
            state_id=state_id,
            run_id=run_id,
            batch_id=batch_id,
            cutoff=cutoff,
            compatibility=compatibility,
            archive_seq=archive_seq,
            parent_descriptor=parent_descriptor,
            direction_descriptor=direction_descriptor,
            budget=_freeze(frozen_budget),
            facts=_freeze(fact_payload),
        )
