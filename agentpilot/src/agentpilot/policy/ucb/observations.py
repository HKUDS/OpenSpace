"""Typed opportunity and Bernoulli-feedback records for the UCB carrier.

The distinction between an opportunity and an observation is deliberate.
``T`` counts completed decisions at which an action was eligible, while
``n`` counts only selected actions with attributable, valid feedback.  The
in-memory history below is also the reference behavior for a future durable
repository implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable, Mapping

from agentpilot.domain.common import deep_freeze, require_aware, require_text, stable_id
from agentpilot.domain.evaluation import (
    EffectivenessLabel,
    EvaluationValidity,
    OutcomeAttribution,
    PolicyObservation,
)
from agentpilot.domain.policy import DirectionMatchKind, PolicyMethod, SlotKind
from agentpilot.domain.realization import ImplementationAdherence
from agentpilot.domain.search import CompatibilityKey, EvidenceCutoff, StateDescriptor


class UCBTarget(str, Enum):
    PARENT = "parent"
    DIRECTION = "direction"
    DISCOVER = "discover"


@dataclass(frozen=True)
class ParentOpportunity:
    """One completed D1 round with its entire frozen eligible slate.

    The selected descriptor supports the observation projection.  Every
    eligible descriptor supports opportunity counting, so a query can count
    the round even when a different parent was selected.
    """

    opportunity_id: str
    decision_round_id: str
    selected_parent_id: str
    eligible_parent_descriptors: Mapping[str, StateDescriptor]
    compatibility: CompatibilityKey
    available_at: datetime

    def __post_init__(self) -> None:
        for name in ("opportunity_id", "decision_round_id", "selected_parent_id"):
            require_text(getattr(self, name), name)
        require_aware(self.available_at, "available_at")
        if not isinstance(self.eligible_parent_descriptors, Mapping):
            raise ValueError("eligible parent descriptors must be a mapping")
        if not self.eligible_parent_descriptors:
            raise ValueError("parent opportunity needs a non-empty eligible slate")
        for parent_id, descriptor in self.eligible_parent_descriptors.items():
            require_text(parent_id, "eligible parent id")
            if not isinstance(descriptor, StateDescriptor):
                raise TypeError("eligible parent descriptor must be StateDescriptor")
        if self.selected_parent_id not in self.eligible_parent_descriptors:
            raise ValueError("selected parent must belong to the eligible slate")
        object.__setattr__(
            self,
            "eligible_parent_descriptors",
            deep_freeze(self.eligible_parent_descriptors),
        )

    @property
    def eligible_parent_ids(self) -> tuple[str, ...]:
        """Stable eligible identities retained for traces and diagnostics."""

        return tuple(self.eligible_parent_descriptors)

    @property
    def selected_parent_descriptor(self) -> StateDescriptor:
        """Descriptor of the selected parent used by its outcome observation."""

        return self.eligible_parent_descriptors[self.selected_parent_id]


@dataclass(frozen=True)
class DirectionOpportunity:
    """One completed D2 decision round.

    ``discover_supported`` is boolean rather than a virtual-slot count.  D3
    executes at most one action per decision round, so one round contributes
    at most one opportunity to the shared DISCOVER learner.
    """

    opportunity_id: str
    decision_round_id: str
    eligible_arm_ids: tuple[str, ...]
    discover_supported: bool
    compatibility: CompatibilityKey
    descriptor: StateDescriptor
    available_at: datetime

    def __post_init__(self) -> None:
        for name in ("opportunity_id", "decision_round_id"):
            require_text(getattr(self, name), name)
        require_aware(self.available_at, "available_at")
        if len(self.eligible_arm_ids) != len(set(self.eligible_arm_ids)):
            raise ValueError("eligible arm ids must be unique")
        if not self.eligible_arm_ids and not self.discover_supported:
            raise ValueError("direction opportunity has empty support")


@dataclass(frozen=True)
class ArmCreationOpportunity:
    """Creation transfer that gives a newly selected arm its first ``T``.

    A new arm cannot occur in the frozen pre-allocation support.  Persisting
    this record before its first feedback makes ``T >= n`` true even for an
    immediately failing first realization.
    """

    opportunity_id: str
    decision_round_id: str
    action_id: str
    arm_id: str
    compatibility: CompatibilityKey
    descriptor: StateDescriptor
    available_at: datetime

    def __post_init__(self) -> None:
        for name in ("opportunity_id", "decision_round_id", "action_id", "arm_id"):
            require_text(getattr(self, name), name)
        require_aware(self.available_at, "available_at")


@dataclass(frozen=True)
class SelectedActionRecord:
    """The sole D3-selected action for one completed decision round."""

    record_id: str
    decision_round_id: str
    action_id: str
    available_at: datetime

    def __post_init__(self) -> None:
        for name in ("record_id", "decision_round_id", "action_id"):
            require_text(getattr(self, name), name)
        require_aware(self.available_at, "available_at")


@dataclass(frozen=True)
class BernoulliObservation:
    observation_id: str
    action_id: str
    decision_round_id: str
    target: UCBTarget
    value: int
    parent_id: str
    arm_id: str | None
    source_slot_kind: SlotKind
    direction_match_kind: DirectionMatchKind
    compatibility: CompatibilityKey
    descriptor: StateDescriptor
    available_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "observation_id",
            "action_id",
            "decision_round_id",
            "parent_id",
        ):
            require_text(getattr(self, name), name)
        require_aware(self.available_at, "available_at")
        if self.value not in (0, 1) or isinstance(self.value, bool):
            raise ValueError("Bernoulli value must be integer 0 or 1")
        if self.target is UCBTarget.PARENT:
            if self.arm_id is not None:
                raise ValueError("parent observation cannot carry arm_id")
        else:
            require_text(self.arm_id or "", "arm_id")
        final_new = self.direction_match_kind in {
            DirectionMatchKind.NEW,
            DirectionMatchKind.PENDING_NEW,
        }
        if self.source_slot_kind is SlotKind.DISCOVER and not final_new:
            raise ValueError("DISCOVER source requires a final-new direction match")
        if self.source_slot_kind is SlotKind.EXISTING and (
            self.direction_match_kind is not DirectionMatchKind.EXISTING
        ):
            raise ValueError("existing source requires an existing-arm match")
        if self.target is UCBTarget.DISCOVER and not final_new:
            raise ValueError("DISCOVER feedback requires source-discover + final-new")

    @property
    def final_new(self) -> bool:
        return self.direction_match_kind in {
            DirectionMatchKind.NEW,
            DirectionMatchKind.PENDING_NEW,
        }


def observations_from_policy(
    observation: PolicyObservation, *, decision_round_id: str,
) -> tuple[BernoulliObservation, ...]:
    """Project one carrier-neutral observation into independent UCB tables.

    Skip feedback produces no Bernoulli row.  The explicit guards prevent a
    malformed caller from turning deviation, attribution failure, or missing
    evaluation into a synthetic failure.
    """

    require_text(decision_round_id, "decision_round_id")
    if observation.method is not PolicyMethod.UCB:
        raise ValueError("only UCB policy observations can update UCB history")
    assert observation.source_slot_kind is not None
    assert observation.direction_match is not None
    effect = observation.effectiveness
    if effect.label is EffectivenessLabel.SKIP:
        return ()
    if observation.implementation_adherence is not ImplementationAdherence.FOLLOWED:
        raise ValueError("non-followed implementation cannot update UCB")
    if observation.outcome_attribution is not OutcomeAttribution.ATTRIBUTABLE:
        raise ValueError("non-attributable outcome cannot update UCB")
    if observation.evaluation_validity is not EvaluationValidity.VALID:
        raise ValueError("invalid evaluation cannot update UCB")
    if not observation.arm_id:
        raise ValueError("UCB policy observation requires a concrete arm_id")
    match = observation.direction_match
    final_new = match.kind in {
        DirectionMatchKind.NEW,
        DirectionMatchKind.PENDING_NEW,
    }
    if observation.source_slot_kind is SlotKind.DISCOVER:
        slot_adherent = final_new
    else:
        slot_adherent = (
            match.kind is DirectionMatchKind.EXISTING
            and match.arm_id == observation.arm_id
        )
    if not slot_adherent:
        raise ValueError(
            "strict slot adherence requires existing->existing or DISCOVER->new"
        )
    if match.arm_id != observation.arm_id:
        raise ValueError("final semantic match and observation arm_id differ")

    value = 1 if effect.label is EffectivenessLabel.SUCCESS else 0
    common = {
        "action_id": observation.action_id,
        "decision_round_id": decision_round_id,
        "value": value,
        "parent_id": observation.parent_id,
        "source_slot_kind": observation.source_slot_kind,
        "direction_match_kind": match.kind,
        "compatibility": observation.compatibility,
        "available_at": observation.available_at,
    }
    result = [
        BernoulliObservation(
            observation_id=stable_id(
                "ucb-observation", observation.observation_id, UCBTarget.PARENT.value
            ),
            target=UCBTarget.PARENT,
            arm_id=None,
            descriptor=observation.parent_descriptor,
            **common,
        ),
        BernoulliObservation(
            observation_id=stable_id(
                "ucb-observation",
                observation.observation_id,
                UCBTarget.DIRECTION.value,
            ),
            target=UCBTarget.DIRECTION,
            arm_id=observation.arm_id,
            descriptor=observation.direction_descriptor,
            **common,
        ),
    ]
    if observation.source_slot_kind is SlotKind.DISCOVER and final_new:
        result.append(
            BernoulliObservation(
                observation_id=stable_id(
                    "ucb-observation",
                    observation.observation_id,
                    UCBTarget.DISCOVER.value,
                ),
                target=UCBTarget.DISCOVER,
                arm_id=observation.arm_id,
                descriptor=observation.direction_descriptor,
                **common,
            )
        )
    return tuple(result)


class UCBHistory:
    """Append-only, exactly-idempotent reference history.

    Production persistence can implement the same methods with database
    uniqueness constraints.  This class intentionally computes from records
    rather than maintaining mutable posterior counters.
    """

    def __init__(self) -> None:
        self._parent: dict[str, ParentOpportunity] = {}
        self._direction: dict[str, DirectionOpportunity] = {}
        self._creation: dict[str, ArmCreationOpportunity] = {}
        self._selected_actions: dict[str, SelectedActionRecord] = {}
        self._observations: dict[str, BernoulliObservation] = {}
        self._parent_rounds: dict[str, str] = {}
        self._direction_rounds: dict[str, str] = {}
        self._creation_rounds: dict[str, str] = {}
        self._selected_action_by_round: dict[str, str] = {}
        self._selected_record_by_round: dict[str, str] = {}
        self._observation_slot: dict[tuple[UCBTarget, str], str] = {}

    def reset_projection(self) -> None:
        """Clear the rebuildable view before replaying durable run events."""

        self._parent.clear()
        self._direction.clear()
        self._creation.clear()
        self._selected_actions.clear()
        self._observations.clear()
        self._parent_rounds.clear()
        self._direction_rounds.clear()
        self._creation_rounds.clear()
        self._selected_action_by_round.clear()
        self._selected_record_by_round.clear()
        self._observation_slot.clear()

    @staticmethod
    def _put_exact(store: dict[str, object], key: str, record: object) -> None:
        existing = store.get(key)
        if existing is not None and existing != record:
            raise ValueError(f"record {key!r} was replayed with semantic drift")
        store[key] = record

    def record_parent_opportunity(self, record: ParentOpportunity) -> None:
        prior_id = self._parent_rounds.get(record.decision_round_id)
        if prior_id is not None and prior_id != record.opportunity_id:
            raise ValueError("a D1 decision round has more than one opportunity")
        self._put_exact(self._parent, record.opportunity_id, record)
        self._parent_rounds[record.decision_round_id] = record.opportunity_id

    def record_direction_opportunity(self, record: DirectionOpportunity) -> None:
        prior_id = self._direction_rounds.get(record.decision_round_id)
        if prior_id is not None and prior_id != record.opportunity_id:
            raise ValueError("a D2 decision round has more than one opportunity")
        self._put_exact(self._direction, record.opportunity_id, record)
        self._direction_rounds[record.decision_round_id] = record.opportunity_id

    def record_creation_transfer(self, record: ArmCreationOpportunity) -> None:
        base = self.direction_opportunity(record.decision_round_id)
        if base is None:
            raise ValueError("creation transfer needs its frozen D2 opportunity")
        if not base.discover_supported:
            raise ValueError("creation transfer needs DISCOVER in frozen support")
        if record.arm_id in base.eligible_arm_ids:
            raise ValueError("creation transfer is only for an arm absent from support")
        if (
            record.compatibility != base.compatibility
            or record.descriptor != base.descriptor
            or record.available_at < base.available_at
        ):
            raise ValueError("creation transfer differs from its D2 opportunity")
        selected = self.selected_action(record.decision_round_id)
        if selected is None or selected.action_id != record.action_id:
            raise ValueError("creation transfer needs its selected action record")
        if record.available_at < selected.available_at:
            raise ValueError("creation transfer cannot precede action selection")
        prior_id = self._creation_rounds.get(record.decision_round_id)
        if prior_id is not None and prior_id != record.opportunity_id:
            raise ValueError("one decision round cannot create two selected arms")
        self._put_exact(self._creation, record.opportunity_id, record)
        self._creation_rounds[record.decision_round_id] = record.opportunity_id

    def record_selected_action(self, record: SelectedActionRecord) -> None:
        supports = tuple(
            item
            for item in (
                self.parent_opportunity(record.decision_round_id),
                self.direction_opportunity(record.decision_round_id),
            )
            if item is not None
        )
        if not supports:
            raise ValueError("selected action needs a frozen D1 or D2 opportunity")
        if any(record.available_at < item.available_at for item in supports):
            raise ValueError("action selection cannot precede its opportunity")
        prior_id = self._selected_action_by_round.get(record.decision_round_id)
        if prior_id is not None and prior_id != record.action_id:
            raise ValueError("a decision round cannot contain two selected actions")
        prior_record = self._selected_record_by_round.get(record.decision_round_id)
        if prior_record is not None and prior_record != record.record_id:
            raise ValueError(
                "a decision round has more than one selected action record"
            )
        self._put_exact(self._selected_actions, record.record_id, record)
        self._selected_action_by_round[record.decision_round_id] = record.action_id
        self._selected_record_by_round[record.decision_round_id] = record.record_id

    def record_observation(self, record: BernoulliObservation) -> None:
        selected = self.selected_action(record.decision_round_id)
        if selected is None or selected.action_id != record.action_id:
            raise ValueError("observation lacks its selected action record")
        if record.available_at < selected.available_at:
            raise ValueError("observation cannot precede action selection")

        if record.target is UCBTarget.PARENT:
            support = self.parent_opportunity(record.decision_round_id)
            if support is None or support.selected_parent_id != record.parent_id:
                raise ValueError("parent observation lacks its selected opportunity")
            support_descriptor = support.selected_parent_descriptor
        else:
            support = self.direction_opportunity(record.decision_round_id)
            if support is None:
                raise ValueError("direction observation lacks its D2 opportunity")
            support_descriptor = support.descriptor
            if record.target is UCBTarget.DISCOVER:
                if not support.discover_supported:
                    raise ValueError("DISCOVER observation lacks eligible support")
                transfer = self.creation_transfer(record.decision_round_id)
                if (
                    transfer is None
                    or transfer.action_id != record.action_id
                    or transfer.arm_id != record.arm_id
                ):
                    raise ValueError("DISCOVER observation lacks creation transfer")
            elif record.arm_id not in support.eligible_arm_ids:
                transfer = self.creation_transfer(record.decision_round_id)
                if (
                    transfer is None
                    or transfer.action_id != record.action_id
                    or transfer.arm_id != record.arm_id
                ):
                    raise ValueError("direction observation lacks eligible opportunity")

        if (
            record.compatibility != support.compatibility
            or record.descriptor != support_descriptor
            or record.available_at < support.available_at
        ):
            raise ValueError("observation differs from its opportunity coordinates")

        slot = (record.target, record.decision_round_id)
        prior_id = self._observation_slot.get(slot)
        if prior_id is not None and prior_id != record.observation_id:
            raise ValueError("learner received two observations for one decision round")
        self._put_exact(self._observations, record.observation_id, record)
        self._observation_slot[slot] = record.observation_id

    def record_observations(self, records: Iterable[BernoulliObservation]) -> None:
        for record in records:
            self.record_observation(record)

    def parent_opportunity(self, decision_round_id: str) -> ParentOpportunity | None:
        record_id = self._parent_rounds.get(decision_round_id)
        return self._parent.get(record_id) if record_id is not None else None

    def direction_opportunity(
        self, decision_round_id: str
    ) -> DirectionOpportunity | None:
        record_id = self._direction_rounds.get(decision_round_id)
        return self._direction.get(record_id) if record_id is not None else None

    def creation_transfer(
        self, decision_round_id: str
    ) -> ArmCreationOpportunity | None:
        record_id = self._creation_rounds.get(decision_round_id)
        return self._creation.get(record_id) if record_id is not None else None

    def selected_action(self, decision_round_id: str) -> SelectedActionRecord | None:
        record_id = self._selected_record_by_round.get(decision_round_id)
        if record_id is None:
            return None
        return self._selected_actions[record_id]

    def parent_opportunities(
        self, cutoff: EvidenceCutoff
    ) -> tuple[ParentOpportunity, ...]:
        return tuple(
            item for item in self._parent.values() if cutoff.allows(item.available_at)
        )

    def direction_opportunities(
        self, cutoff: EvidenceCutoff
    ) -> tuple[DirectionOpportunity, ...]:
        return tuple(
            item
            for item in self._direction.values()
            if cutoff.allows(item.available_at)
        )

    def creation_transfers(
        self, cutoff: EvidenceCutoff
    ) -> tuple[ArmCreationOpportunity, ...]:
        return tuple(
            item for item in self._creation.values() if cutoff.allows(item.available_at)
        )

    def observations(self, cutoff: EvidenceCutoff) -> tuple[BernoulliObservation, ...]:
        return tuple(
            item
            for item in self._observations.values()
            if cutoff.allows(item.available_at)
        )
