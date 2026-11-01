"""Offline acceptance checks for frozen direction-semantic thresholds.

This module measures labeled, precomputed embedding pairs.  It never fits a
threshold, calls a provider or LLM, refits a representation, or mutates a
semantic index.  Formal acceptance additionally requires an explicitly
attested target-model fixture; synthetic/unit fixtures remain diagnostic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from agentpilot.domain.common import require_text
from agentpilot.search.direction_semantic_index import (
    EmbeddingIdentity,
    SemanticThresholds,
    cosine_distance,
)


class SemanticPairLabel(str, Enum):
    """Frozen causal relationship between an anchor and one realization.

    Repeat means wording-only variation of the same mechanism and realization;
    same-arm means the same causal mechanism with a materially new realization;
    distinct means a different causal mechanism even when goals/files overlap.
    Parent/applicability comparability is intentionally labeled elsewhere.
    """

    REPEAT_PARAPHRASE = "repeat_paraphrase"
    SAME_ARM_NOVEL_REALIZATION = "same_arm_novel_realization"
    DISTINCT_MECHANISM = "distinct_mechanism"


class SemanticFixtureProvenance(str, Enum):
    """Whether a fixture is eligible to support a formal run."""

    UNIT_TEST = "unit_test"
    TARGET_MODEL_LABELED = "target_model_labeled"


class SemanticDistanceBand(str, Enum):
    REPEAT = "repeat"
    SAME_ARM = "same_arm"
    DISTINCT = "distinct"
    AMBIGUOUS = "ambiguous"
    UNMEASURABLE = "unmeasurable"


@dataclass(frozen=True)
class LabeledSemanticEmbedding:
    """One precomputed vector with its exact encoder and input identity."""

    item_id: str
    canonical_input: str
    embedding: tuple[float, ...]
    embedding_identity: EmbeddingIdentity

    def __post_init__(self) -> None:
        require_text(self.item_id, "item_id")
        require_text(self.canonical_input, "canonical_input")
        if not isinstance(self.embedding_identity, EmbeddingIdentity):
            raise TypeError("embedding_identity must be an EmbeddingIdentity")
        if isinstance(self.embedding, (str, bytes)):
            raise TypeError("embedding must be a numeric sequence")
        try:
            values = tuple(float(value) for value in self.embedding)
        except (TypeError, ValueError) as exc:
            raise ValueError("embedding contains a non-numeric value") from exc
        if len(values) != self.embedding_identity.dimension:
            raise ValueError("embedding dimension differs from encoder identity")
        if any(not math.isfinite(value) for value in values):
            raise ValueError("embedding contains NaN or infinity")
        if math.fsum(value * value for value in values) <= 0.0:
            raise ValueError("embedding must not be a zero vector")
        object.__setattr__(self, "embedding", values)


@dataclass(frozen=True)
class LabeledSemanticPair:
    case_id: str
    label: SemanticPairLabel
    anchor: LabeledSemanticEmbedding
    candidate: LabeledSemanticEmbedding
    annotation: str

    def __post_init__(self) -> None:
        require_text(self.case_id, "case_id")
        require_text(self.annotation, "annotation")
        if not isinstance(self.label, SemanticPairLabel):
            raise TypeError("label must be a SemanticPairLabel")
        if not isinstance(self.anchor, LabeledSemanticEmbedding) or not isinstance(
            self.candidate, LabeledSemanticEmbedding
        ):
            raise TypeError("semantic pair endpoints have the wrong type")
        if self.anchor.item_id == self.candidate.item_id:
            raise ValueError("semantic pair endpoints must have distinct item ids")


@dataclass(frozen=True)
class SemanticThresholdFixture:
    """Provider-neutral labeled dataset for one frozen semantic identity."""

    fixture_id: str
    provenance: SemanticFixtureProvenance
    embedding_identity: EmbeddingIdentity
    threshold_id: str
    cases: tuple[LabeledSemanticPair, ...]

    def __post_init__(self) -> None:
        require_text(self.fixture_id, "fixture_id")
        require_text(self.threshold_id, "threshold_id")
        if not isinstance(self.provenance, SemanticFixtureProvenance):
            raise TypeError("provenance must be a SemanticFixtureProvenance")
        if not isinstance(self.embedding_identity, EmbeddingIdentity):
            raise TypeError("embedding_identity must be an EmbeddingIdentity")
        cases = tuple(self.cases)
        if any(not isinstance(case, LabeledSemanticPair) for case in cases):
            raise TypeError("cases must contain LabeledSemanticPair values")
        case_ids = tuple(case.case_id for case in cases)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("semantic fixture contains duplicate case ids")
        pair_labels: dict[tuple[str, str], SemanticPairLabel] = {}
        items: dict[str, LabeledSemanticEmbedding] = {}
        for case in cases:
            for endpoint in (case.anchor, case.candidate):
                previous_endpoint = items.get(endpoint.item_id)
                if previous_endpoint is not None and previous_endpoint != endpoint:
                    raise ValueError(
                        "semantic fixture reuses an item id with different content"
                    )
                items[endpoint.item_id] = endpoint
            key = tuple(sorted((case.anchor.item_id, case.candidate.item_id)))
            previous = pair_labels.get(key)
            if previous is not None:
                if previous is not case.label:
                    raise ValueError("the same semantic pair has conflicting labels")
                raise ValueError("semantic fixture contains a duplicate pair")
            pair_labels[key] = case.label
        object.__setattr__(self, "cases", cases)


@dataclass(frozen=True)
class SemanticAcceptanceRequirements:
    """Frozen coverage gate; it does not derive or optimize thresholds."""

    minimum_cases_per_label: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_cases_per_label, bool)
            or not isinstance(self.minimum_cases_per_label, int)
            or self.minimum_cases_per_label < 1
        ):
            raise ValueError("minimum_cases_per_label must be positive")


@dataclass(frozen=True)
class SemanticCaseAcceptance:
    case_id: str
    label: SemanticPairLabel
    distance: float | None
    band: SemanticDistanceBand
    boundary_margin: float | None
    encoder_identity_passed: bool
    accepted: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SemanticClassAcceptance:
    label: SemanticPairLabel
    case_count: int
    minimum_distance: float | None
    maximum_distance: float | None
    coverage_passed: bool
    cases_passed: bool


@dataclass(frozen=True)
class SemanticThresholdAcceptanceReport:
    calibration_manifest_id: str | None
    calibration_split: str | None
    fixture_id: str
    encoder_identity: EmbeddingIdentity
    threshold_id: str
    tau_repeat: float
    tau_arm: float
    threshold_boundary_margin: float
    nearest_arm_tie_tolerance: float
    minimum_cases_per_label: int
    fixture_identity_passed: bool
    threshold_identity_passed: bool
    threshold_order_passed: bool
    class_separation_passed: bool
    diagnostic_passed: bool
    coverage_passed: bool
    provenance_passed: bool
    audit_manifest_passed: bool
    formal_accepted: bool
    cases: tuple[SemanticCaseAcceptance, ...]
    classes: tuple[SemanticClassAcceptance, ...]
    reasons: tuple[str, ...]


def _expected_band(label: SemanticPairLabel) -> SemanticDistanceBand:
    return {
        SemanticPairLabel.REPEAT_PARAPHRASE: SemanticDistanceBand.REPEAT,
        SemanticPairLabel.SAME_ARM_NOVEL_REALIZATION: (SemanticDistanceBand.SAME_ARM),
        SemanticPairLabel.DISTINCT_MECHANISM: SemanticDistanceBand.DISTINCT,
    }[label]


def _distance_band(
    distance: float,
    thresholds: SemanticThresholds,
) -> tuple[SemanticDistanceBand, float]:
    boundary_margin = thresholds.boundary_margin(distance)
    if thresholds.is_boundary_ambiguous(distance):
        return SemanticDistanceBand.AMBIGUOUS, boundary_margin
    if distance <= thresholds.tau_repeat:
        return SemanticDistanceBand.REPEAT, boundary_margin
    if distance <= thresholds.tau_arm:
        return SemanticDistanceBand.SAME_ARM, boundary_margin
    return SemanticDistanceBand.DISTINCT, boundary_margin


def _case_acceptance(
    case: LabeledSemanticPair,
    *,
    expected_identity: EmbeddingIdentity,
    thresholds: SemanticThresholds,
) -> SemanticCaseAcceptance:
    endpoints = (case.anchor, case.candidate)
    identity_passed = all(
        endpoint.embedding_identity == expected_identity for endpoint in endpoints
    )
    reasons = []
    if not identity_passed:
        reasons.append("embedding_identity_mismatch")
    try:
        distance = cosine_distance(
            case.anchor.embedding,
            case.candidate.embedding,
        )
    except ValueError:
        distance = None
        band = SemanticDistanceBand.UNMEASURABLE
        boundary_margin = None
        reasons.append("unmeasurable_cosine_distance")
    else:
        band, boundary_margin = _distance_band(distance, thresholds)
        if band is SemanticDistanceBand.AMBIGUOUS:
            reasons.append("inside_threshold_ambiguity_margin")
        elif band is not _expected_band(case.label):
            reasons.append("distance_band_differs_from_label")
    accepted = identity_passed and not reasons
    return SemanticCaseAcceptance(
        case_id=case.case_id,
        label=case.label,
        distance=distance,
        band=band,
        boundary_margin=boundary_margin,
        encoder_identity_passed=identity_passed,
        accepted=accepted,
        reasons=tuple(reasons),
    )


def evaluate_semantic_threshold_fixture(
    fixture: SemanticThresholdFixture,
    *,
    expected_identity: EmbeddingIdentity,
    thresholds: SemanticThresholds,
    requirements: SemanticAcceptanceRequirements,
) -> SemanticThresholdAcceptanceReport:
    """Measure one labeled fixture against fixed thresholds without fitting."""

    if not isinstance(fixture, SemanticThresholdFixture):
        raise TypeError("fixture must be a SemanticThresholdFixture")
    if not isinstance(expected_identity, EmbeddingIdentity):
        raise TypeError("expected_identity must be an EmbeddingIdentity")
    if not isinstance(thresholds, SemanticThresholds):
        raise TypeError("thresholds must be SemanticThresholds")
    if not isinstance(requirements, SemanticAcceptanceRequirements):
        raise TypeError("requirements must be SemanticAcceptanceRequirements")

    fixture_identity_passed = fixture.embedding_identity == expected_identity
    threshold_identity_passed = fixture.threshold_id == thresholds.threshold_id
    threshold_order_passed = 0.0 <= thresholds.tau_repeat < thresholds.tau_arm <= 2.0
    case_reports = tuple(
        _case_acceptance(
            case,
            expected_identity=expected_identity,
            thresholds=thresholds,
        )
        for case in fixture.cases
    )
    class_reports = []
    for label in SemanticPairLabel:
        selected = tuple(case for case in case_reports if case.label is label)
        distances = tuple(
            case.distance for case in selected if case.distance is not None
        )
        class_reports.append(
            SemanticClassAcceptance(
                label=label,
                case_count=len(selected),
                minimum_distance=min(distances) if distances else None,
                maximum_distance=max(distances) if distances else None,
                coverage_passed=(len(selected) >= requirements.minimum_cases_per_label),
                cases_passed=bool(selected) and all(case.accepted for case in selected),
            )
        )
    classes = tuple(class_reports)
    by_label = {item.label: item for item in classes}
    repeat = by_label[SemanticPairLabel.REPEAT_PARAPHRASE]
    same_arm = by_label[SemanticPairLabel.SAME_ARM_NOVEL_REALIZATION]
    distinct = by_label[SemanticPairLabel.DISTINCT_MECHANISM]
    class_separation_passed = (
        repeat.maximum_distance is not None
        and same_arm.minimum_distance is not None
        and same_arm.maximum_distance is not None
        and distinct.minimum_distance is not None
        and repeat.maximum_distance < same_arm.minimum_distance
        and same_arm.maximum_distance < distinct.minimum_distance
    )
    coverage_passed = all(item.coverage_passed for item in classes)
    provenance_passed = (
        fixture.provenance is SemanticFixtureProvenance.TARGET_MODEL_LABELED
    )
    diagnostic_passed = (
        fixture_identity_passed
        and threshold_identity_passed
        and threshold_order_passed
        and class_separation_passed
        and bool(case_reports)
        and all(case.accepted for case in case_reports)
    )
    # A bare in-memory fixture is useful for diagnostics but is not an
    # auditable formal artifact.  ``semantic_calibration`` upgrades this only
    # after strict manifest loading and acceptance-split checks.
    audit_manifest_passed = False
    formal_accepted = False
    reasons = []
    if not fixture_identity_passed:
        reasons.append("fixture_embedding_identity_mismatch")
    if not threshold_identity_passed:
        reasons.append("fixture_threshold_identity_mismatch")
    if not threshold_order_passed:
        reasons.append("invalid_threshold_order")
    if not class_separation_passed:
        reasons.append("labeled_classes_are_not_separated")
    if not coverage_passed:
        reasons.append("insufficient_labeled_case_coverage")
    if not provenance_passed:
        reasons.append("fixture_is_not_formal_target_model_data")
    else:
        reasons.append("auditable_calibration_manifest_required")
    if any(not case.accepted for case in case_reports):
        reasons.append("one_or_more_cases_failed")
    return SemanticThresholdAcceptanceReport(
        calibration_manifest_id=None,
        calibration_split=None,
        fixture_id=fixture.fixture_id,
        encoder_identity=expected_identity,
        threshold_id=thresholds.threshold_id,
        tau_repeat=thresholds.tau_repeat,
        tau_arm=thresholds.tau_arm,
        threshold_boundary_margin=thresholds.threshold_boundary_margin,
        nearest_arm_tie_tolerance=thresholds.nearest_arm_tie_tolerance,
        minimum_cases_per_label=requirements.minimum_cases_per_label,
        fixture_identity_passed=fixture_identity_passed,
        threshold_identity_passed=threshold_identity_passed,
        threshold_order_passed=threshold_order_passed,
        class_separation_passed=class_separation_passed,
        diagnostic_passed=diagnostic_passed,
        coverage_passed=coverage_passed,
        provenance_passed=provenance_passed,
        audit_manifest_passed=audit_manifest_passed,
        formal_accepted=formal_accepted,
        cases=case_reports,
        classes=classes,
        reasons=tuple(reasons),
    )


__all__ = [
    "LabeledSemanticEmbedding",
    "LabeledSemanticPair",
    "SemanticAcceptanceRequirements",
    "SemanticCaseAcceptance",
    "SemanticClassAcceptance",
    "SemanticDistanceBand",
    "SemanticFixtureProvenance",
    "SemanticPairLabel",
    "SemanticThresholdAcceptanceReport",
    "SemanticThresholdFixture",
    "evaluate_semantic_threshold_fixture",
]
