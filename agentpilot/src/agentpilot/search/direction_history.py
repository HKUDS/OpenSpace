"""Carrier-neutral completed-direction history and bounded failure retrieval.

The records in this module are projections of already durable action and
policy-observation facts.  They do not contain UCB arm state or policy-memory
content.  Application code freezes the records into the batch search-input
snapshot before either carrier is allowed to call a proposer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from agentpilot.domain import (
    CompatibilityKey,
    EffectivenessLabel,
    EvaluationValidity,
    ImplementationAdherence,
    OutcomeAttribution,
    PolicyAction,
    PolicyObservation,
    NewPath,
    ScopeKind,
    ScopeTarget,
    SearchState,
    StateDescriptor,
    StructuredDirection,
    stable_id,
)
from agentpilot.domain.common import require_aware, require_text


FAILURE_CARD_RETRIEVER_ID = "bounded-related-failures-scope"
FAILURE_CARD_MAX_CARDS = 3
FAILURE_CARD_MAX_TEXT_CHARS = 1_000


@dataclass(frozen=True)
class DirectionHistoryRecord:
    """One completed proposal/outcome join, independent of its carrier."""

    history_id: str
    run_id: str
    action_id: str
    parent_id: str
    direction: StructuredDirection
    compatibility: CompatibilityKey
    parent_descriptor: StateDescriptor
    direction_descriptor: StateDescriptor
    implementation_adherence: ImplementationAdherence
    outcome_attribution: OutcomeAttribution
    evaluation_validity: EvaluationValidity
    effectiveness_label: EffectivenessLabel
    signed_statistic: float | None
    margin: float
    outcome_reason: str
    #: The trace manifest the rejected candidate published while it was being
    #: measured, when one exists.  This is what lets the next proposer read how
    #: a failed attempt actually behaved instead of only its direction prose.
    trace_manifest_id: str | None
    available_at: datetime

    def __post_init__(self) -> None:
        for name in ("history_id", "run_id", "action_id", "parent_id"):
            require_text(getattr(self, name), name)
        require_text(self.outcome_reason, "outcome_reason")
        if self.trace_manifest_id is not None:
            require_text(self.trace_manifest_id, "trace_manifest_id")
        require_aware(self.available_at, "available_at")
        if not math.isfinite(self.margin):
            raise ValueError("history effectiveness margin must be finite")
        if self.effectiveness_label is EffectivenessLabel.SKIP:
            if self.signed_statistic is not None:
                raise ValueError("skip history cannot carry a signed statistic")
        elif self.signed_statistic is None or not math.isfinite(
            float(self.signed_statistic)
        ):
            raise ValueError("success/failure history needs a finite statistic")
        expected = stable_id("direction-history", self.run_id, self.action_id)
        if self.history_id != expected:
            raise ValueError("direction history id is not stable")

    @classmethod
    def from_completed(
        cls,
        action: PolicyAction,
        observation: PolicyObservation,
        *,
        trace_manifest_id: str | None,
    ) -> "DirectionHistoryRecord":
        """Project a selected direction and its completed observation.

        ``trace_manifest_id`` must come from
        :func:`resolve_rejected_candidate_manifest_id`, so every projection
        site derives the identical durable value and snapshot re-validation
        can reproduce it bit for bit.
        """

        if action.action_id != observation.action_id:
            raise ValueError("direction history action/observation mismatch")
        if (
            action.run_id == ""
            or action.parent_id != observation.parent_id
            or action.method is not observation.method
        ):
            raise ValueError("direction history coordinates drifted")
        observed_direction_id = observation.metadata.get("direction_id")
        if observed_direction_id is not None and (
            observed_direction_id != action.direction.direction_id
        ):
            raise ValueError("direction history observation names another direction")
        effectiveness = observation.effectiveness
        return cls(
            history_id=stable_id("direction-history", action.run_id, action.action_id),
            run_id=action.run_id,
            action_id=action.action_id,
            parent_id=action.parent_id,
            direction=action.direction,
            compatibility=observation.compatibility,
            parent_descriptor=observation.parent_descriptor,
            direction_descriptor=observation.direction_descriptor,
            implementation_adherence=observation.implementation_adherence,
            outcome_attribution=observation.outcome_attribution,
            evaluation_validity=observation.evaluation_validity,
            effectiveness_label=effectiveness.label,
            signed_statistic=effectiveness.signed_statistic,
            margin=effectiveness.margin,
            outcome_reason=effectiveness.reason,
            trace_manifest_id=trace_manifest_id,
            available_at=observation.available_at,
        )


def resolve_rejected_candidate_manifest_id(
    trace_evidence, observation: PolicyObservation
) -> str | None:
    """Resolve one measured candidate's manifest from durable facts only.

    The rejected candidate published its census manifest before the rejection
    observation completed, so every eligible manifest already existed at
    ``observation.available_at``.  Selecting the latest one by that durable
    order makes the resolution identical at freeze time and at snapshot
    re-validation, with no cutoff parameter to disagree about.
    """

    program_id = observation.metadata.get("candidate_program_id")
    if program_id is None:
        return None
    eligible = tuple(
        manifest
        for manifest in trace_evidence.list_manifests_for_program(program_id)
        if manifest.compatibility == observation.compatibility
        and manifest.available_at <= observation.available_at
    )
    if not eligible:
        return None
    return max(
        eligible, key=lambda item: (item.available_at, item.manifest_id)
    ).manifest_id


@dataclass(frozen=True)
class FailureDirectionCard:
    """Compact proposer evidence for one prior attempt that did not pay off.

    Two kinds of attempt teach a proposer something, and conflating them would
    lose one of them.  A followed direction that underperformed carries a
    verdict on the idea, so it arrives with its statistic.  A direction the
    worker did not implement as declared carries no verdict on the idea at all
    -- its measurement is a ``SKIP`` and has no statistic -- but it does say the
    direction was not realizable as written.  Suppressing the second kind is
    what lets a proposer re-issue an unimplementable direction indefinitely.
    """

    history_id: str
    action_id: str
    direction_id: str
    parent_id: str
    relation: str
    failure_mechanism: str
    implementation_direction: str
    declared_scope: tuple[ScopeTarget, ...]
    new_paths: tuple[NewPath, ...]
    resolved_scope: tuple[str, ...]
    signed_statistic: float | None
    margin: float
    attribution: OutcomeAttribution
    outcome_reason: str
    available_at: datetime
    adherence: ImplementationAdherence = ImplementationAdherence.FOLLOWED
    #: The rejected candidate's own trace manifest, when it published one.  The
    #: proposer session resolves this to readable cases, so the card is not
    #: just prose about a failure but a handle on the failure's behavior.
    trace_manifest_id: str | None = None

    @property
    def realized(self) -> bool:
        """Whether the measured program is the one this direction described."""

        return self.adherence is ImplementationAdherence.FOLLOWED

    def __post_init__(self) -> None:
        for name in (
            "history_id",
            "action_id",
            "direction_id",
            "parent_id",
            "failure_mechanism",
            "implementation_direction",
            "outcome_reason",
        ):
            require_text(getattr(self, name), name)
        if self.relation not in {
            "same_parent",
            "same_parent_state",
            "same_direction_state",
        }:
            raise ValueError("unsupported failure-card relation")
        if not isinstance(self.adherence, ImplementationAdherence):
            raise TypeError("adherence must be an ImplementationAdherence")
        # An unrealized attempt says nothing about the direction's merit, so it
        # must not present a verdict: no statistic, and no attribution claim
        # over an outcome that measured a different program.
        if self.realized:
            if self.attribution is not OutcomeAttribution.ATTRIBUTABLE:
                raise ValueError("a realized failure card must be attributable")
            if self.signed_statistic is None:
                raise ValueError("a realized failure card needs its statistic")
        elif self.signed_statistic is not None:
            raise ValueError("an unrealized failure card cannot carry a statistic")
        if self.signed_statistic is not None and not math.isfinite(
            self.signed_statistic
        ):
            raise ValueError("failure-card outcome values must be finite")
        if not math.isfinite(self.margin):
            raise ValueError("failure-card outcome values must be finite")
        declared_scope = tuple(self.declared_scope)
        new_paths = tuple(self.new_paths)
        resolved_scope = tuple(self.resolved_scope)
        object.__setattr__(self, "declared_scope", declared_scope)
        object.__setattr__(self, "new_paths", new_paths)
        object.__setattr__(self, "resolved_scope", resolved_scope)
        if not declared_scope or len(declared_scope) > 8:
            raise ValueError("failure card needs one to eight declared scope entries")
        if any(not isinstance(item, ScopeTarget) for item in declared_scope):
            raise TypeError(
                "failure card declared_scope must contain ScopeTarget values"
            )
        scope_keys = tuple(
            (item.kind.value, item.path, item.symbols) for item in declared_scope
        )
        if scope_keys != tuple(sorted(set(scope_keys))):
            raise ValueError("failure card declared_scope is not canonical")
        if len(new_paths) > 4:
            raise ValueError("failure card cannot contain more than four new paths")
        if any(not isinstance(item, NewPath) for item in new_paths):
            raise TypeError("failure card new_paths must contain NewPath values")
        if tuple(item.path for item in new_paths) != tuple(
            sorted({item.path for item in new_paths})
        ):
            raise ValueError("failure card new_paths are not canonical")
        if (
            not resolved_scope
            or len(resolved_scope) > 32
            or resolved_scope != tuple(sorted(set(resolved_scope)))
        ):
            raise ValueError("failure card needs one to 32 resolved scope paths")
        for path in resolved_scope:
            require_text(path, "resolved_scope path")
        if not {item.path for item in new_paths} <= set(resolved_scope):
            raise ValueError("failure card new paths must belong to resolved_scope")
        if self.trace_manifest_id is not None:
            require_text(self.trace_manifest_id, "trace_manifest_id")
        require_aware(self.available_at, "available_at")

    @property
    def evidence_ref(self) -> str:
        return f"failure:{self.history_id}"

    def as_prompt_payload(self) -> dict[str, object]:
        return {
            "evidence_ref": self.evidence_ref,
            "history_id": self.history_id,
            "action_id": self.action_id,
            "direction_id": self.direction_id,
            "parent_id": self.parent_id,
            "relation": self.relation,
            "failure_mechanism": self.failure_mechanism,
            "implementation_direction": self.implementation_direction,
            "declared_scope": [
                {
                    "kind": item.kind.value,
                    "path": item.path,
                    "symbols": list(item.symbols),
                }
                for item in self.declared_scope
            ],
            "new_paths": [
                {"path": item.path, "role": item.role} for item in self.new_paths
            ],
            "resolved_scope": list(self.resolved_scope),
            "signed_statistic": self.signed_statistic,
            "effectiveness_margin": self.margin,
            "attribution": self.attribution.value,
            "implementation_adherence": self.adherence.value,
            "outcome_reason": self.outcome_reason,
            "available_at": self.available_at,
        }


def _teaches_a_failure(record: DirectionHistoryRecord) -> bool:
    """Whether one completed attempt is a lesson for the next proposal.

    A followed attempt teaches through its verdict, so it needs a valid,
    attributable, scored failure.  An attempt whose implementation deviated
    from the declared targets teaches through the deviation itself, which is
    established by the adherence verdict alone: demanding an effectiveness
    statistic as well would silently drop every one of them, because deviation
    is classified as a skip and skips have no statistic.
    """

    if record.implementation_adherence is not ImplementationAdherence.FOLLOWED:
        return record.effectiveness_label is not EffectivenessLabel.SUCCESS
    return (
        record.effectiveness_label is EffectivenessLabel.FAILURE
        and record.outcome_attribution is OutcomeAttribution.ATTRIBUTABLE
        and record.evaluation_validity is EvaluationValidity.VALID
        and record.signed_statistic is not None
    )


@dataclass(frozen=True)
class FailureCardSelection:
    selected_parent_id: str
    retriever_id: str
    cards: tuple[FailureDirectionCard, ...]

    def __post_init__(self) -> None:
        require_text(self.selected_parent_id, "selected_parent_id")
        require_text(self.retriever_id, "retriever_id")
        cards = tuple(self.cards)
        object.__setattr__(self, "cards", cards)
        if len(cards) > 3:
            raise ValueError("failure-card selection exceeded its fixed bound")
        refs = tuple(card.evidence_ref for card in cards)
        if len(refs) != len(set(refs)):
            raise ValueError("failure-card selection contains duplicates")


class DirectionHistoryRetriever:
    """Select at most three related failures with no learned similarity score."""

    def __init__(
        self,
        *,
        max_cards: int = FAILURE_CARD_MAX_CARDS,
        max_text_chars: int = FAILURE_CARD_MAX_TEXT_CHARS,
    ) -> None:
        if isinstance(max_cards, bool) or not 1 <= max_cards <= 3:
            raise ValueError("max_cards must be in [1, 3]")
        if isinstance(max_text_chars, bool) or not 128 <= max_text_chars <= 2_000:
            raise ValueError("max_text_chars must be in [128, 2000]")
        self.max_cards = max_cards
        self.max_text_chars = max_text_chars

    def retrieve(
        self,
        *,
        state: SearchState,
        selected_parent_id: str,
        selected_parent_descriptor: StateDescriptor,
        records: Iterable[DirectionHistoryRecord],
    ) -> FailureCardSelection:
        require_text(selected_parent_id, "selected_parent_id")
        ranked: list[tuple[int, int, float, str, DirectionHistoryRecord, str]] = []
        seen: dict[str, DirectionHistoryRecord] = {}
        for record in records:
            if not isinstance(record, DirectionHistoryRecord):
                raise TypeError("direction history contains an untyped record")
            prior = seen.get(record.history_id)
            if prior is not None and prior != record:
                raise ValueError("direction history id has semantic drift")
            seen[record.history_id] = record
        for record in seen.values():
            if (
                record.run_id != state.run_id
                or record.compatibility != state.compatibility
                or not state.cutoff.allows(record.available_at)
            ):
                continue
            if not _teaches_a_failure(record):
                continue
            if record.parent_id == selected_parent_id:
                relation, relation_rank = "same_parent", 0
            elif record.parent_descriptor == selected_parent_descriptor:
                relation, relation_rank = "same_parent_state", 1
            elif record.direction_descriptor == state.direction_descriptor:
                relation, relation_rank = "same_direction_state", 2
            else:
                continue
            # Within one relation, a scored verdict on the idea outranks the
            # weaker news that an attempt was never realized, so the fixed
            # card budget is not spent on deviations while verdicts wait.
            ranked.append(
                (
                    relation_rank,
                    0
                    if record.implementation_adherence
                    is ImplementationAdherence.FOLLOWED
                    else 1,
                    -record.available_at.timestamp(),
                    record.history_id,
                    record,
                    relation,
                )
            )
        ranked.sort(key=lambda item: item[:4])
        cards = tuple(
            self._card(record, relation)
            for _, _, _, _, record, relation in ranked[: self.max_cards]
        )
        return FailureCardSelection(
            selected_parent_id=selected_parent_id,
            retriever_id=FAILURE_CARD_RETRIEVER_ID,
            cards=cards,
        )

    def _bounded(self, text: str) -> str:
        if len(text) <= self.max_text_chars:
            return text
        return text[: self.max_text_chars - 1] + "…"

    def _card(
        self, record: DirectionHistoryRecord, relation: str
    ) -> FailureDirectionCard:
        direction = record.direction
        created_paths = {item.path for item in direction.new_paths}
        required_paths = created_paths | {
            item.path
            for item in direction.declared_scope
            if item.kind is not ScopeKind.PACKAGE
        }
        existing_paths = tuple(
            path for path in direction.resolved_scope if path not in required_paths
        )
        compact_resolved_scope = tuple(
            sorted(required_paths | set(existing_paths[: 32 - len(required_paths)]))
        )
        realized = record.implementation_adherence is ImplementationAdherence.FOLLOWED
        return FailureDirectionCard(
            history_id=record.history_id,
            action_id=record.action_id,
            direction_id=direction.direction_id,
            parent_id=record.parent_id,
            relation=relation,
            failure_mechanism=self._bounded(direction.failure_mechanism),
            implementation_direction=self._bounded(direction.implementation_direction),
            declared_scope=direction.declared_scope,
            new_paths=direction.new_paths,
            resolved_scope=compact_resolved_scope,
            signed_statistic=(float(record.signed_statistic) if realized else None),
            margin=record.margin,
            attribution=record.outcome_attribution,
            adherence=record.implementation_adherence,
            outcome_reason=self._bounded(record.outcome_reason),
            available_at=record.available_at,
            trace_manifest_id=record.trace_manifest_id,
        )


__all__ = [
    "FAILURE_CARD_MAX_CARDS",
    "FAILURE_CARD_MAX_TEXT_CHARS",
    "FAILURE_CARD_RETRIEVER_ID",
    "DirectionHistoryRecord",
    "DirectionHistoryRetriever",
    "FailureCardSelection",
    "FailureDirectionCard",
    "resolve_rejected_candidate_manifest_id",
]
