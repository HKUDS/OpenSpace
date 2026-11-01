"""Paired-evaluation and policy-observation domain contracts."""

# Persistent format and evaluation identifiers retain their historical values.

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from agentpilot.domain.common import deep_freeze, require_aware, require_text
from agentpilot.domain.policy import DirectionMatch, PolicyMethod, SlotKind
from agentpilot.domain.realization import ImplementationAdherence
from agentpilot.domain.search import CompatibilityKey, StateDescriptor


class OutcomeAttribution(str, Enum):
    ATTRIBUTABLE = "attributable"
    NON_ATTRIBUTABLE = "non_attributable"
    UNKNOWN = "unknown"


class EvaluationValidity(str, Enum):
    VALID = "valid"
    INCONCLUSIVE = "inconclusive"
    MISSING = "missing"
    APPARATUS_FAILURE = "apparatus_failure"


class EffectivenessLabel(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    SKIP = "skip"


class EvidenceTier(str, Enum):
    """Evidence channel attached to an action outcome.

    C evidence is valid only for the separate final-report path.  It must
    never be converted into a normal policy observation or archive update.
    REALIZATION records a pre-evaluation implementation rejection and carries
    no benchmark measurement.
    """

    VISIBLE_A = "visible_a"
    REALIZATION = "realization"
    ADAPTIVE_B = "adaptive_b"
    SEALED_C = "sealed_c"


PROVIDER_TRIAL_DIAGNOSTICS_SCHEMA = "openthink.provider-trial"
PROVIDER_RELIABILITY_DIAGNOSTICS_SCHEMA = "openthink.provider-reliability"


_PROVIDER_DIAGNOSTIC_COUNTS = frozenset(
    {
        "logical_requests_started",
        "logical_requests_finished",
        "upstream_attempts_started",
        "upstream_attempts_finished",
        "responses_started",
        "responses_finished",
        "transport_failures",
        "terminal_transport_failures",
        "safe_retries",
        "open_requests",
        "open_without_response",
        "open_reading_response",
        "first_byte_samples",
    }
)
_PROVIDER_DIAGNOSTIC_OPTIONAL_DURATIONS = frozenset(
    {
        "oldest_open_request_seconds",
        "oldest_open_attempt_seconds",
        "first_byte_max_seconds",
        "last_progress_age_seconds",
        "last_transport_failure_age_seconds",
        "last_terminal_transport_failure_age_seconds",
    }
)
_PROVIDER_DIAGNOSTIC_DURATIONS = frozenset(
    {
        "total_upstream_wait_seconds",
        "terminal_transport_wait_seconds",
        "nonproductive_wait_seconds",
        "first_byte_total_seconds",
        "upstream_timeout_seconds",
    }
)


def _validate_provider_diagnostic(value: object) -> None:
    if value is None:
        return
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "schema",
            "worker_deadline_reached",
            "worker_timeout_seconds",
            "provider",
        }
        or value.get("schema") != PROVIDER_TRIAL_DIAGNOSTICS_SCHEMA
        or not isinstance(value.get("worker_deadline_reached"), bool)
    ):
        raise ValueError("provider diagnostic does not match the trusted envelope")
    worker_timeout = value["worker_timeout_seconds"]
    if (
        isinstance(worker_timeout, bool)
        or not isinstance(worker_timeout, (int, float))
        or not math.isfinite(float(worker_timeout))
        or not 0.0 < float(worker_timeout) <= 86_400.0
    ):
        raise ValueError("provider diagnostic worker timeout is invalid")
    provider = value["provider"]
    expected = (
        {"schema"}
        | set(_PROVIDER_DIAGNOSTIC_COUNTS)
        | set(_PROVIDER_DIAGNOSTIC_OPTIONAL_DURATIONS)
        | set(_PROVIDER_DIAGNOSTIC_DURATIONS)
    )
    if (
        not isinstance(provider, Mapping)
        or set(provider) != expected
        or provider.get("schema") != PROVIDER_RELIABILITY_DIAGNOSTICS_SCHEMA
    ):
        raise ValueError("provider diagnostic does not match the trusted schema")
    for name in _PROVIDER_DIAGNOSTIC_COUNTS:
        item = provider[name]
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 0 <= item <= 1_000_000
        ):
            raise ValueError(f"provider diagnostic {name} is invalid")
    for name in _PROVIDER_DIAGNOSTIC_OPTIONAL_DURATIONS:
        item = provider[name]
        if item is None:
            continue
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or not 0.0 <= float(item) <= 1_000_000_000.0
        ):
            raise ValueError(f"provider diagnostic {name} is invalid")
    for name in _PROVIDER_DIAGNOSTIC_DURATIONS:
        item = provider[name]
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or not 0.0 <= float(item) <= 1_000_000_000.0
        ):
            raise ValueError(f"provider diagnostic {name} is invalid")


def _freeze_cell_diagnostics(
    diagnostics: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Mapping[str, Any]]:
    """Validate bounded operator-only timeout or apparatus diagnostics."""

    if not isinstance(diagnostics, Mapping):
        raise TypeError("cell_diagnostics must be a mapping")
    timeout_fields = {
        "side",
        "response_count",
        "first_response_latency_seconds",
        "last_terminal_activity_seconds",
        "answer_written_before_timeout",
        "provider_waiting_at_deadline",
        "provider_diagnostics",
    }
    apparatus_fields = {
        "kind",
        "side",
        "reason",
        "exception_type",
        "benchmark",
    }
    for diagnostic_id, diagnostic in diagnostics.items():
        require_text(diagnostic_id, "cell diagnostic id")
        if not isinstance(diagnostic, Mapping):
            raise TypeError("cell diagnostic must be a mapping")
        fields = set(diagnostic)
        side = diagnostic.get("side")
        if not isinstance(side, str) or side not in ("control", "challenger"):
            raise ValueError("cell diagnostic side is invalid")
        prefix, separator, cell_id = diagnostic_id.partition(":")
        if separator != ":" or prefix != side or not cell_id:
            raise ValueError("cell diagnostic id differs from its side")
        if fields == apparatus_fields:
            if diagnostic["kind"] != "apparatus_failure":
                raise ValueError("apparatus diagnostic has an invalid kind")
            for key in ("side", "reason", "exception_type", "benchmark"):
                require_text(diagnostic[key], f"cell diagnostic {key}")
            continue
        if fields != timeout_fields:
            raise ValueError(
                "cell diagnostic does not match the timeout schema or "
                "apparatus schema"
            )
        count = diagnostic["response_count"]
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= 1_000_000
        ):
            raise ValueError("cell diagnostic response_count is invalid")
        for name in (
            "first_response_latency_seconds",
            "last_terminal_activity_seconds",
        ):
            value = diagnostic[name]
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 172_800.0
            ):
                raise ValueError(f"cell diagnostic {name} is invalid")
        for name in (
            "answer_written_before_timeout",
            "provider_waiting_at_deadline",
        ):
            if not isinstance(diagnostic[name], bool):
                raise TypeError(f"cell diagnostic {name} must be boolean")
        _validate_provider_diagnostic(diagnostic["provider_diagnostics"])
    return deep_freeze(diagnostics)


@dataclass(frozen=True)
class ChildProgramReadout:
    """Explicit evaluator readout needed to make an admitted child searchable.

    ``absolute_primary_value`` is deliberately not a paired delta.  The
    evaluator must measure the child on the canonical primary scale and must
    provide the descriptor and per-cell results directly; the application
    layer is forbidden from reconstructing any of them from prior state.

    ``cell_notes`` says how a cell failed where the boolean only says that it
    did.  A short evaluator-owned label is enough to separate failures that ask
    for different repairs, and being a label rather than the measured output it
    cannot smuggle the graded result of a deciding cell into a later prompt.

    """

    child_program_id: str
    descriptor: StateDescriptor
    absolute_primary_value: float
    cell_results: Mapping[str, bool | None]
    cell_notes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_text(self.child_program_id, "child_program_id")
        if not isinstance(self.descriptor, StateDescriptor):
            raise TypeError("descriptor must be a StateDescriptor")
        if (
            isinstance(self.absolute_primary_value, bool)
            or not isinstance(self.absolute_primary_value, (int, float))
            or not math.isfinite(float(self.absolute_primary_value))
        ):
            raise ValueError("absolute_primary_value must be finite")
        object.__setattr__(
            self, "absolute_primary_value", float(self.absolute_primary_value)
        )
        if not isinstance(self.cell_results, Mapping):
            raise TypeError("cell_results must be a mapping")
        for cell_id, result in self.cell_results.items():
            require_text(cell_id, "cell_id")
            if result is not None and not isinstance(result, bool):
                raise TypeError("cell result must be bool or None")
        object.__setattr__(self, "cell_results", deep_freeze(self.cell_results))
        if not isinstance(self.cell_notes, Mapping):
            raise TypeError("cell_notes must be a mapping")
        for cell_id, note in self.cell_notes.items():
            require_text(cell_id, "cell_id")
            require_text(note, "cell note")
            if cell_id not in self.cell_results:
                raise ValueError("cell_notes describes an unmeasured cell")
            if len(note) > 64 or note.strip() != note or "\n" in note:
                raise ValueError("cell note must be one short trimmed label")
        object.__setattr__(self, "cell_notes", deep_freeze(self.cell_notes))


@dataclass(frozen=True)
class PairedOutcome:
    outcome_id: str
    action_id: str
    evidence_tier: EvidenceTier
    evaluation_plan_record_id: str | None
    validity: EvaluationValidity
    attribution: OutcomeAttribution
    signed_effectiveness_statistic: float | None
    constraints_pass: bool | None
    completed_at: datetime
    primary_delta_mean: float | None = None
    primary_delta_lower: float | None = None
    #: Signed change in how often the child was stopped by its resource budget
    #: rather than by finishing, relative to the parent on the same cells.  A
    #: constraint veto reduces to one bit, so carrying the magnitude is what lets
    #: a reader tell "kept the answers but stalled everywhere" apart from "broke
    #: the answers".  ``None`` means no resource rule was applied.
    resource_regression: float | None = None
    admission_fate: str | None = None
    estimator_identity: Mapping[str, Any] = field(default_factory=dict)
    #: Operator-facing fixed-schema timeout summaries.  They live on the
    #: outcome rather than the child readout so apparatus failures remain
    #: auditable when no attributable/searchable child can be materialized.
    #: SearchFact construction intentionally omits this field.
    cell_diagnostics: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    child_readout: ChildProgramReadout | None = None

    def __post_init__(self) -> None:
        require_text(self.outcome_id, "outcome_id")
        require_text(self.action_id, "action_id")
        if not isinstance(self.evidence_tier, EvidenceTier):
            raise TypeError("evidence_tier must be an EvidenceTier")
        if self.evidence_tier is EvidenceTier.ADAPTIVE_B:
            require_text(
                self.evaluation_plan_record_id,
                "evaluation_plan_record_id",
            )
        elif self.evaluation_plan_record_id is not None:
            raise ValueError(
                "only adaptive-B outcomes may bind an evaluation plan"
            )
        if not isinstance(self.validity, EvaluationValidity):
            raise TypeError("validity must be an EvaluationValidity")
        if not isinstance(self.attribution, OutcomeAttribution):
            raise TypeError("attribution must be an OutcomeAttribution")
        require_aware(self.completed_at, "completed_at")
        for name in (
            "signed_effectiveness_statistic",
            "primary_delta_mean",
            "primary_delta_lower",
        ):
            value = getattr(self, name)
            if value is not None and not math.isfinite(float(value)):
                # Preserve the invalid input long enough for the classifier to
                # turn it into skip; consumers must never treat it as reward.
                continue
        object.__setattr__(
            self, "estimator_identity", deep_freeze(self.estimator_identity)
        )
        object.__setattr__(
            self,
            "cell_diagnostics",
            _freeze_cell_diagnostics(self.cell_diagnostics),
        )
        if self.child_readout is not None and not isinstance(
            self.child_readout, ChildProgramReadout
        ):
            raise TypeError("child_readout must be a ChildProgramReadout")


@dataclass(frozen=True)
class EffectivenessObservation:
    observation_id: str
    action_id: str
    label: EffectivenessLabel
    available_at: datetime
    reason: str
    signed_statistic: float | None = None
    margin: float = 0.0

    def __post_init__(self) -> None:
        require_text(self.observation_id, "observation_id")
        require_text(self.action_id, "action_id")
        require_text(self.reason, "reason")
        require_aware(self.available_at, "available_at")
        if self.label is not EffectivenessLabel.SKIP:
            if self.signed_statistic is None or not math.isfinite(
                self.signed_statistic
            ):
                raise ValueError("success/failure needs a finite signed statistic")


@dataclass(frozen=True)
class PolicyObservation:
    """Carrier-neutral join over state, selected action and valid feedback."""

    observation_id: str
    action_id: str
    evidence_tier: EvidenceTier
    method: PolicyMethod
    parent_id: str
    arm_id: str | None
    source_slot_kind: SlotKind | None
    direction_match: DirectionMatch | None
    compatibility: CompatibilityKey
    parent_descriptor: StateDescriptor
    direction_descriptor: StateDescriptor
    implementation_adherence: ImplementationAdherence
    outcome_attribution: OutcomeAttribution
    evaluation_validity: EvaluationValidity
    effectiveness: EffectivenessObservation
    available_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("observation_id", "action_id", "parent_id"):
            require_text(getattr(self, name), name)
        if not isinstance(self.evidence_tier, EvidenceTier):
            raise TypeError("evidence_tier must be an EvidenceTier")
        require_aware(self.available_at, "available_at")
        if self.evidence_tier is EvidenceTier.SEALED_C:
            raise ValueError("sealed C evidence cannot become a policy observation")
        if self.effectiveness.action_id != self.action_id:
            raise ValueError("effectiveness references a different action")
        if self.method is PolicyMethod.UCB:
            require_text(self.arm_id or "", "arm_id")
            if self.source_slot_kind is None or self.direction_match is None:
                raise ValueError("UCB observation needs typed slot and final match")
        elif (
            self.arm_id is not None
            or self.source_slot_kind is not None
            or self.direction_match is not None
        ):
            raise ValueError("Memory observation cannot carry UCB slot/match/arm")
        object.__setattr__(self, "metadata", deep_freeze(self.metadata))
