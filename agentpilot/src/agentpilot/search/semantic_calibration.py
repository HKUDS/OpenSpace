"""Strict offline manifests for direction-semantic threshold calibration.

The loader consumes precomputed vectors only and never calls an embedding
provider.  ``strict-separated-gaps`` deterministically derives both taus
from the calibration split; it never optimizes on the acceptance split or
changes the pre-frozen boundary/tie tolerances.  A standalone manifest is always
diagnostic.  Formal acceptance is available only through a bundle that loads
and binds a target-model calibration split to a group-disjoint untouched
acceptance split.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from agentpilot.domain import NewPath, ScopeKind, ScopeTarget
from agentpilot.domain.common import canonical_json, require_text
from agentpilot.search.direction_semantic_index import (
    EmbeddingIdentity,
    SemanticThresholds,
    cosine_distance,
)
from agentpilot.search.semantic_threshold_acceptance import (
    LabeledSemanticEmbedding,
    LabeledSemanticPair,
    SemanticAcceptanceRequirements,
    SemanticFixtureProvenance,
    SemanticPairLabel,
    SemanticThresholdAcceptanceReport,
    SemanticThresholdFixture,
    evaluate_semantic_threshold_fixture,
)
from .semantic_research_calibration import (
    PROTOCOL as RESEARCH_THRESHOLD_SELECTION_PROTOCOL,
    ResearchCalibrationBundleReport,
    ResearchSplitReport,
    ResearchThresholdDerivation,
)

SEMANTIC_THRESHOLD_SELECTION_PROTOCOL = "strict-separated-gaps"


class SemanticCalibrationSplit(str, Enum):
    CALIBRATION = "calibration"
    ACCEPTANCE = "acceptance"


@dataclass(frozen=True)
class SemanticCalibrationManifest:
    """Auditable inputs for one offline threshold evaluation."""

    manifest_id: str
    split: SemanticCalibrationSplit
    fixture: SemanticThresholdFixture
    thresholds: SemanticThresholds
    requirements: SemanticAcceptanceRequirements
    annotators: tuple[str, ...]
    minimum_annotators: int
    adjudication_policy: str
    embedding_source: str
    threshold_selection_protocol: str
    selection_fixture_id: str | None
    case_group_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        require_text(self.manifest_id, "manifest_id")
        if not isinstance(self.split, SemanticCalibrationSplit):
            raise TypeError("split must be a SemanticCalibrationSplit")
        if not isinstance(self.fixture, SemanticThresholdFixture):
            raise TypeError("fixture must be a SemanticThresholdFixture")
        if not isinstance(self.thresholds, SemanticThresholds):
            raise TypeError("thresholds must be SemanticThresholds")
        if not isinstance(self.requirements, SemanticAcceptanceRequirements):
            raise TypeError("requirements must be SemanticAcceptanceRequirements")
        for name in (
            "adjudication_policy",
            "embedding_source",
            "threshold_selection_protocol",
        ):
            require_text(getattr(self, name), name)
        if self.threshold_selection_protocol not in {SEMANTIC_THRESHOLD_SELECTION_PROTOCOL, RESEARCH_THRESHOLD_SELECTION_PROTOCOL}:
            raise ValueError("unsupported semantic threshold selection protocol")
        annotators = tuple(require_text(item, "annotator") for item in self.annotators)
        if len(annotators) != len(set(annotators)):
            raise ValueError("semantic calibration annotators must be unique")
        if (
            isinstance(self.minimum_annotators, bool)
            or not isinstance(self.minimum_annotators, int)
            or self.minimum_annotators < 1
        ):
            raise ValueError("minimum_annotators must be positive")
        if len(annotators) < self.minimum_annotators:
            raise ValueError("semantic calibration has insufficient annotators")
        object.__setattr__(self, "annotators", annotators)
        if self.fixture.fixture_id != self.manifest_id:
            raise ValueError("fixture_id must equal its calibration manifest_id")
        if self.fixture.threshold_id != self.thresholds.threshold_id:
            raise ValueError("fixture threshold identity differs from the manifest")
        case_group_ids = tuple(
            require_text(item, "case_group_id") for item in self.case_group_ids
        )
        if len(case_group_ids) != len(self.fixture.cases):
            raise ValueError("every semantic calibration case needs one group id")
        object.__setattr__(self, "case_group_ids", case_group_ids)
        if self.split is SemanticCalibrationSplit.ACCEPTANCE:
            require_text(self.selection_fixture_id, "selection_fixture_id")
            if self.selection_fixture_id == self.manifest_id:
                raise ValueError(
                    "acceptance split must reference another selection fixture"
                )
        elif self.selection_fixture_id is not None:
            raise ValueError("calibration split cannot reference a selection fixture")

    @property
    def embedding_identity(self) -> EmbeddingIdentity:
        return self.fixture.embedding_identity

    @property
    def audit_eligible(self) -> bool:
        """A standalone manifest can never establish formal audit eligibility."""

        return False

    @property
    def group_ids(self) -> frozenset[str]:
        return frozenset(self.case_group_ids)

    @property
    def endpoint_item_ids(self) -> frozenset[str]:
        return frozenset(
            endpoint.item_id
            for case in self.fixture.cases
            for endpoint in (case.anchor, case.candidate)
        )

    @property
    def canonical_inputs(self) -> frozenset[str]:
        return frozenset(
            endpoint.canonical_input
            for case in self.fixture.cases
            for endpoint in (case.anchor, case.candidate)
        )


@dataclass(frozen=True)
class SemanticCalibrationBundle:
    """Loaded calibration and acceptance manifests with frozen audit binding."""

    bundle_id: str
    selection: SemanticCalibrationManifest
    acceptance: SemanticCalibrationManifest

    def __post_init__(self) -> None:
        require_text(self.bundle_id, "bundle_id")
        if not isinstance(
            self.selection, SemanticCalibrationManifest
        ) or not isinstance(self.acceptance, SemanticCalibrationManifest):
            raise TypeError("semantic calibration bundle members have the wrong type")
        if self.selection.split is not SemanticCalibrationSplit.CALIBRATION:
            raise ValueError("bundle selection member must be a calibration split")
        if self.acceptance.split is not SemanticCalibrationSplit.ACCEPTANCE:
            raise ValueError("bundle acceptance member must be an acceptance split")
        if self.acceptance.selection_fixture_id != self.selection.manifest_id:
            raise ValueError("acceptance split does not reference the loaded selection")

        bound_values = (
            (
                self.selection.embedding_identity,
                self.acceptance.embedding_identity,
                "embedding identity",
            ),
            (self.selection.thresholds, self.acceptance.thresholds, "thresholds"),
            (
                self.selection.requirements,
                self.acceptance.requirements,
                "acceptance requirements",
            ),
            (
                self.selection.threshold_selection_protocol,
                self.acceptance.threshold_selection_protocol,
                "threshold selection protocol",
            ),
            (
                self.selection.minimum_annotators,
                self.acceptance.minimum_annotators,
                "minimum annotators",
            ),
            (
                self.selection.adjudication_policy,
                self.acceptance.adjudication_policy,
                "adjudication policy",
            ),
        )
        for selected, accepted, name in bound_values:
            if selected != accepted:
                raise ValueError(f"bundle {name} differs across splits")

        if self.selection.group_ids & self.acceptance.group_ids:
            raise ValueError("calibration and acceptance groups must be disjoint")
        if self.selection.endpoint_item_ids & self.acceptance.endpoint_item_ids:
            raise ValueError("calibration and acceptance item ids must be disjoint")
        if self.selection.canonical_inputs & self.acceptance.canonical_inputs:
            raise ValueError("calibration and acceptance inputs must be disjoint")
        selection_case_ids = {case.case_id for case in self.selection.fixture.cases}
        acceptance_case_ids = {case.case_id for case in self.acceptance.fixture.cases}
        if selection_case_ids & acceptance_case_ids:
            raise ValueError("calibration and acceptance case ids must be disjoint")


@dataclass(frozen=True)
class SemanticThresholdDerivationReport:
    """Deterministic selection-only threshold derivation and binding report."""

    protocol: str
    selection_manifest_id: str
    repeat_maximum_distance: float | None
    same_arm_minimum_distance: float | None
    same_arm_maximum_distance: float | None
    distinct_minimum_distance: float | None
    first_strict_gap_passed: bool
    second_strict_gap_passed: bool
    derived_tau_repeat: float | None
    derived_tau_arm: float | None
    frozen_threshold_boundary_margin: float
    frozen_nearest_arm_tie_tolerance: float
    derived_thresholds_margin_compatible: bool
    configured_tau_repeat: float
    configured_tau_arm: float
    configured_taus_match: bool
    protocol_passed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SemanticCalibrationBundleReport:
    bundle_id: str
    selection_manifest_id: str
    acceptance_manifest_id: str
    encoder_identity: EmbeddingIdentity
    thresholds: SemanticThresholds
    binding_passed: bool
    group_disjoint_passed: bool
    threshold_derivation: SemanticThresholdDerivationReport
    selection_report: SemanticThresholdAcceptanceReport
    acceptance_report: SemanticThresholdAcceptanceReport
    formal_accepted: bool
    reasons: tuple[str, ...]


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _only(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{name} keys differ; missing={missing}, extra={extra}")


def _text(value: Mapping[str, Any], key: str, name: str) -> str:
    try:
        item = value[key]
    except KeyError as exc:
        raise ValueError(f"{name}.{key} is required") from exc
    return require_text(item, f"{name}.{key}")


def _number(value: Mapping[str, Any], key: str, name: str) -> float:
    try:
        item = value[key]
    except KeyError as exc:
        raise ValueError(f"{name}.{key} is required") from exc
    if isinstance(item, bool) or not isinstance(item, (int, float)):
        raise ValueError(f"{name}.{key} must be numeric")
    return float(item)


def _integer(value: Mapping[str, Any], key: str, name: str) -> int:
    try:
        item = value[key]
    except KeyError as exc:
        raise ValueError(f"{name}.{key} is required") from exc
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"{name}.{key} must be an integer")
    return item


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be an array")
    return value


def _identity(value: Any) -> EmbeddingIdentity:
    table = _mapping(value, "embedding_identity")
    _only(
        table,
        {"encoder_id", "encoder_identity", "dimension", "distance"},
        "embedding_identity",
    )
    return EmbeddingIdentity(
        encoder_id=_text(table, "encoder_id", "embedding_identity"),
        encoder_identity=_text(table, "encoder_identity", "embedding_identity"),
        dimension=_integer(table, "dimension", "embedding_identity"),
        distance=_text(table, "distance", "embedding_identity"),
    )


def _thresholds(value: Any) -> SemanticThresholds:
    table = _mapping(value, "thresholds")
    _only(
        table,
        {
            "threshold_id",
            "tau_repeat",
            "tau_arm",
            "threshold_boundary_margin",
            "nearest_arm_tie_tolerance",
        },
        "thresholds",
    )
    return SemanticThresholds(
        threshold_id=_text(table, "threshold_id", "thresholds"),
        tau_repeat=_number(table, "tau_repeat", "thresholds"),
        tau_arm=_number(table, "tau_arm", "thresholds"),
        threshold_boundary_margin=_number(
            table, "threshold_boundary_margin", "thresholds"
        ),
        nearest_arm_tie_tolerance=_number(
            table, "nearest_arm_tie_tolerance", "thresholds"
        ),
    )


def _embedding(
    value: Any,
    *,
    name: str,
    identity: EmbeddingIdentity,
) -> LabeledSemanticEmbedding:
    table = _mapping(value, name)
    _only(table, {"item_id", "canonical_input", "embedding"}, name)
    vector = tuple(
        _number({"value": item}, "value", f"{name}.embedding")
        for item in _sequence(table["embedding"], f"{name}.embedding")
    )
    canonical_input = _text(table, "canonical_input", name)
    _validate_canonical_input(canonical_input, f"{name}.canonical_input")
    return LabeledSemanticEmbedding(
        item_id=_text(table, "item_id", name),
        canonical_input=canonical_input,
        embedding=vector,
        embedding_identity=identity,
    )


def _validate_canonical_input(value: str, name: str) -> None:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be canonical JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{name} must encode a direction object")
    arm_fields = {"failure_mechanism"}
    realization_fields = {
        "implementation_direction",
        "resolved_scope",
        "new_paths",
    }
    direction_fields = arm_fields | realization_fields
    fields = frozenset(payload)
    if fields not in {
        frozenset(arm_fields),
        frozenset(realization_fields),
        frozenset(direction_fields),
    }:
        raise ValueError(f"{name} does not use the frozen direction fields")
    if "failure_mechanism" in payload:
        require_text(payload["failure_mechanism"], f"{name}.failure_mechanism")
    if realization_fields <= set(payload):
        require_text(
            payload["implementation_direction"],
            f"{name}.implementation_direction",
        )
        resolved = tuple(
            ScopeTarget(
                ScopeKind.FILE,
                require_text(item, f"{name}.resolved_scope[]"),
            ).path
            for item in _sequence(payload["resolved_scope"], f"{name}.resolved_scope")
        )
        if not resolved or resolved != tuple(sorted(set(resolved))):
            raise ValueError(f"{name}.resolved_scope must be sorted and unique")
        canonical_new_paths = []
        for index, item in enumerate(
            _sequence(payload["new_paths"], f"{name}.new_paths")
        ):
            item_name = f"{name}.new_paths[{index}]"
            table = _mapping(item, item_name)
            _only(table, {"path", "role"}, item_name)
            created = NewPath(
                path=_text(table, "path", item_name),
                role=_text(table, "role", item_name),
            )
            canonical_new_paths.append({"path": created.path, "role": created.role})
        canonical_new_paths.sort(key=lambda item: item["path"])
        if payload["new_paths"] != canonical_new_paths:
            raise ValueError(f"{name}.new_paths are not canonically ordered")
        if not {item["path"] for item in canonical_new_paths} <= set(resolved):
            raise ValueError(f"{name}.new_paths lie outside resolved_scope")
    if canonical_json(payload) != value:
        raise ValueError(f"{name} is not canonically serialized")


def parse_semantic_calibration_manifest(
    document: Mapping[str, Any],
) -> SemanticCalibrationManifest:
    """Parse one strict JSON-shaped manifest without provider access."""

    table = _mapping(document, "manifest")
    _only(
        table,
        {
            "manifest_id",
            "split",
            "provenance",
            "embedding_identity",
            "thresholds",
            "requirements",
            "annotators",
            "minimum_annotators",
            "adjudication_policy",
            "embedding_source",
            "threshold_selection_protocol",
            "selection_fixture_id",
            "cases",
        },
        "manifest",
    )
    identity = _identity(table["embedding_identity"])
    thresholds = _thresholds(table["thresholds"])
    requirements_table = _mapping(table["requirements"], "requirements")
    _only(requirements_table, {"minimum_cases_per_label"}, "requirements")
    requirements = SemanticAcceptanceRequirements(
        minimum_cases_per_label=_integer(
            requirements_table, "minimum_cases_per_label", "requirements"
        )
    )
    cases = []
    for index, raw_case in enumerate(_sequence(table["cases"], "cases")):
        name = f"cases[{index}]"
        case_table = _mapping(raw_case, name)
        _only(
            case_table,
            {
                "case_id",
                "group_id",
                "label",
                "annotation",
                "anchor",
                "candidate",
            },
            name,
        )
        try:
            label = SemanticPairLabel(_text(case_table, "label", name))
        except ValueError as exc:
            raise ValueError(f"{name}.label is unsupported") from exc
        cases.append(
            LabeledSemanticPair(
                case_id=_text(case_table, "case_id", name),
                label=label,
                anchor=_embedding(
                    case_table["anchor"], name=f"{name}.anchor", identity=identity
                ),
                candidate=_embedding(
                    case_table["candidate"],
                    name=f"{name}.candidate",
                    identity=identity,
                ),
                annotation=_text(case_table, "annotation", name),
            )
        )
    try:
        provenance = SemanticFixtureProvenance(_text(table, "provenance", "manifest"))
        split = SemanticCalibrationSplit(_text(table, "split", "manifest"))
    except ValueError as exc:
        raise ValueError("manifest provenance or split is unsupported") from exc
    manifest_id = _text(table, "manifest_id", "manifest")
    fixture = SemanticThresholdFixture(
        fixture_id=manifest_id,
        provenance=provenance,
        embedding_identity=identity,
        threshold_id=thresholds.threshold_id,
        cases=tuple(cases),
    )
    selection_fixture_id = table["selection_fixture_id"]
    if selection_fixture_id is not None:
        selection_fixture_id = require_text(
            selection_fixture_id, "manifest.selection_fixture_id"
        )
    return SemanticCalibrationManifest(
        manifest_id=manifest_id,
        split=split,
        fixture=fixture,
        thresholds=thresholds,
        requirements=requirements,
        annotators=tuple(
            require_text(item, "manifest.annotator")
            for item in _sequence(table["annotators"], "manifest.annotators")
        ),
        minimum_annotators=_integer(table, "minimum_annotators", "manifest"),
        adjudication_policy=_text(table, "adjudication_policy", "manifest"),
        embedding_source=_text(table, "embedding_source", "manifest"),
        threshold_selection_protocol=_text(
            table, "threshold_selection_protocol", "manifest"
        ),
        selection_fixture_id=selection_fixture_id,
        case_group_ids=tuple(
            _text(_mapping(raw_case, f"cases[{index}]"), "group_id", f"cases[{index}]")
            for index, raw_case in enumerate(_sequence(table["cases"], "cases"))
        ),
    )


def parse_semantic_calibration_bundle(
    document: Mapping[str, Any],
) -> SemanticCalibrationBundle:
    """Parse one strict pair of loaded selection and acceptance manifests."""

    table = _mapping(document, "bundle")
    _only(
        table,
        {"bundle_id", "selection", "acceptance"},
        "bundle",
    )
    return SemanticCalibrationBundle(
        bundle_id=_text(table, "bundle_id", "bundle"),
        selection=parse_semantic_calibration_manifest(
            _mapping(table["selection"], "bundle.selection")
        ),
        acceptance=parse_semantic_calibration_manifest(
            _mapping(table["acceptance"], "bundle.acceptance")
        ),
    )


def load_semantic_calibration_manifest(
    path: str | Path,
) -> SemanticCalibrationManifest:
    """Load one UTF-8 JSON manifest from disk without external calls."""

    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("cannot load semantic calibration manifest") from exc
    return parse_semantic_calibration_manifest(document)


def load_semantic_calibration_bundle(
    path: str | Path,
) -> SemanticCalibrationBundle:
    """Load one UTF-8 JSON calibration/acceptance bundle without external calls."""

    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("cannot load semantic calibration bundle") from exc
    return parse_semantic_calibration_bundle(document)


def validate_formal_semantic_calibration_bundle(
    bundle: SemanticCalibrationBundle,
    *,
    bundle_id: str,
    encoder_identity: EmbeddingIdentity,
    thresholds: SemanticThresholds,
) -> SemanticCalibrationBundleReport:
    """Require one accepted bundle to match the configured semantic policy."""

    if not isinstance(bundle, SemanticCalibrationBundle):
        raise TypeError("bundle must be a SemanticCalibrationBundle")
    if not isinstance(encoder_identity, EmbeddingIdentity):
        raise TypeError("encoder_identity must be an EmbeddingIdentity")
    if not isinstance(thresholds, SemanticThresholds):
        raise TypeError("thresholds must be SemanticThresholds")
    if bundle.bundle_id != require_text(bundle_id, "bundle_id"):
        raise ValueError("semantic calibration bundle id differs from policy config")
    report = evaluate_semantic_calibration_bundle(bundle)
    if not report.formal_accepted:
        detail = ", ".join(report.reasons) or "formal acceptance failed"
        raise ValueError(f"semantic calibration bundle rejected: {detail}")
    if report.encoder_identity != encoder_identity:
        raise ValueError(
            "semantic calibration encoder identity differs from policy config"
        )
    if report.thresholds != thresholds:
        raise ValueError("semantic calibration thresholds differ from policy config")
    return report


def _midpoint(left: float, right: float) -> float:
    return math.fsum((left, right)) / 2.0


def derive_semantic_thresholds(
    selection: SemanticCalibrationManifest,
) -> SemanticThresholdDerivationReport | ResearchThresholdDerivation:
    """Derive both taus from calibration distances without reading acceptance.

    The frozen boundary margin and nearest-arm tie tolerance are copied into the
    report and validated, never estimated from the labeled distances.
    """

    if not isinstance(selection, SemanticCalibrationManifest):
        raise TypeError("selection must be a SemanticCalibrationManifest")
    if selection.split is not SemanticCalibrationSplit.CALIBRATION:
        raise ValueError("threshold derivation requires a calibration split")
    if selection.threshold_selection_protocol == RESEARCH_THRESHOLD_SELECTION_PROTOCOL:
        from .semantic_research_calibration import derive
        return derive(selection)

    distances: dict[SemanticPairLabel, list[float]] = {
        label: [] for label in SemanticPairLabel
    }
    for case in selection.fixture.cases:
        distances[case.label].append(
            cosine_distance(case.anchor.embedding, case.candidate.embedding)
        )
    repeat = distances[SemanticPairLabel.REPEAT_PARAPHRASE]
    same_arm = distances[SemanticPairLabel.SAME_ARM_NOVEL_REALIZATION]
    distinct = distances[SemanticPairLabel.DISTINCT_MECHANISM]
    repeat_maximum = max(repeat) if repeat else None
    same_arm_minimum = min(same_arm) if same_arm else None
    same_arm_maximum = max(same_arm) if same_arm else None
    distinct_minimum = min(distinct) if distinct else None
    first_gap = (
        repeat_maximum is not None
        and same_arm_minimum is not None
        and repeat_maximum < same_arm_minimum
    )
    second_gap = (
        same_arm_maximum is not None
        and distinct_minimum is not None
        and same_arm_maximum < distinct_minimum
    )
    derived_tau_repeat = (
        _midpoint(repeat_maximum, same_arm_minimum) if first_gap else None
    )
    derived_tau_arm = (
        _midpoint(same_arm_maximum, distinct_minimum) if second_gap else None
    )

    margin_compatible = False
    if derived_tau_repeat is not None and derived_tau_arm is not None:
        try:
            SemanticThresholds(
                threshold_id=selection.thresholds.threshold_id,
                tau_repeat=derived_tau_repeat,
                tau_arm=derived_tau_arm,
                threshold_boundary_margin=(
                    selection.thresholds.threshold_boundary_margin
                ),
                nearest_arm_tie_tolerance=(
                    selection.thresholds.nearest_arm_tie_tolerance
                ),
            )
        except ValueError:
            pass
        else:
            margin_compatible = True
    configured_match = (
        derived_tau_repeat is not None
        and derived_tau_arm is not None
        and selection.thresholds.tau_repeat == derived_tau_repeat
        and selection.thresholds.tau_arm == derived_tau_arm
    )
    reasons = []
    if not repeat or not same_arm or not distinct:
        reasons.append("selection_split_missing_one_or_more_label_classes")
    if not first_gap:
        reasons.append("repeat_and_same_arm_classes_have_no_strict_gap")
    if not second_gap:
        reasons.append("same_arm_and_distinct_classes_have_no_strict_gap")
    if derived_tau_repeat is not None and derived_tau_arm is not None:
        if not margin_compatible:
            reasons.append(
                "selection_derived_taus_conflict_with_frozen_boundary_margin"
            )
        if not configured_match:
            reasons.append("configured_taus_differ_from_selection_derivation")
    protocol_passed = (
        first_gap and second_gap and margin_compatible and configured_match
    )
    return SemanticThresholdDerivationReport(
        protocol=selection.threshold_selection_protocol,
        selection_manifest_id=selection.manifest_id,
        repeat_maximum_distance=repeat_maximum,
        same_arm_minimum_distance=same_arm_minimum,
        same_arm_maximum_distance=same_arm_maximum,
        distinct_minimum_distance=distinct_minimum,
        first_strict_gap_passed=first_gap,
        second_strict_gap_passed=second_gap,
        derived_tau_repeat=derived_tau_repeat,
        derived_tau_arm=derived_tau_arm,
        frozen_threshold_boundary_margin=(
            selection.thresholds.threshold_boundary_margin
        ),
        frozen_nearest_arm_tie_tolerance=(
            selection.thresholds.nearest_arm_tie_tolerance
        ),
        derived_thresholds_margin_compatible=margin_compatible,
        configured_tau_repeat=selection.thresholds.tau_repeat,
        configured_tau_arm=selection.thresholds.tau_arm,
        configured_taus_match=configured_match,
        protocol_passed=protocol_passed,
        reasons=tuple(reasons),
    )


def evaluate_semantic_calibration_manifest(
    manifest: SemanticCalibrationManifest,
) -> SemanticThresholdAcceptanceReport | ResearchSplitReport:
    """Evaluate one split diagnostically; a standalone split is never formal."""

    if not isinstance(manifest, SemanticCalibrationManifest):
        raise TypeError("manifest must be a SemanticCalibrationManifest")
    if manifest.threshold_selection_protocol == RESEARCH_THRESHOLD_SELECTION_PROTOCOL:
        from .semantic_research_calibration import evaluate_split
        return evaluate_split(manifest)
    base = evaluate_semantic_threshold_fixture(
        manifest.fixture,
        expected_identity=manifest.embedding_identity,
        thresholds=manifest.thresholds,
        requirements=manifest.requirements,
    )
    reasons = [
        reason
        for reason in base.reasons
        if reason != "auditable_calibration_manifest_required"
    ]
    reasons.append("paired_calibration_bundle_required")
    return replace(
        base,
        calibration_manifest_id=manifest.manifest_id,
        calibration_split=manifest.split.value,
        audit_manifest_passed=False,
        formal_accepted=False,
        reasons=tuple(reasons),
    )


def evaluate_semantic_calibration_bundle(
    bundle: SemanticCalibrationBundle,
) -> SemanticCalibrationBundleReport | ResearchCalibrationBundleReport:
    """Evaluate both frozen splits after structural binding and disjointness checks."""

    if not isinstance(bundle, SemanticCalibrationBundle):
        raise TypeError("bundle must be a SemanticCalibrationBundle")
    if bundle.selection.threshold_selection_protocol == RESEARCH_THRESHOLD_SELECTION_PROTOCOL:
        from .semantic_research_calibration import evaluate_bundle
        return evaluate_bundle(bundle)
    threshold_derivation = derive_semantic_thresholds(bundle.selection)
    selection_report = evaluate_semantic_calibration_manifest(bundle.selection)
    standalone_acceptance = evaluate_semantic_calibration_manifest(bundle.acceptance)
    formal_accepted = all(
        (
            selection_report.diagnostic_passed,
            selection_report.coverage_passed,
            selection_report.provenance_passed,
            threshold_derivation.protocol_passed,
            standalone_acceptance.diagnostic_passed,
            standalone_acceptance.coverage_passed,
            standalone_acceptance.provenance_passed,
        )
    )
    reasons = []
    if not selection_report.diagnostic_passed:
        reasons.append("selection_split_failed_diagnostics")
    if not selection_report.coverage_passed:
        reasons.append("selection_split_failed_coverage")
    if not selection_report.provenance_passed:
        reasons.append("selection_split_is_not_formal_target_model_data")
    if not threshold_derivation.protocol_passed:
        reasons.extend(threshold_derivation.reasons)
    if not standalone_acceptance.diagnostic_passed:
        reasons.append("acceptance_split_failed_diagnostics")
    if not standalone_acceptance.coverage_passed:
        reasons.append("acceptance_split_failed_coverage")
    if not standalone_acceptance.provenance_passed:
        reasons.append("acceptance_split_is_not_formal_target_model_data")
    acceptance_reasons = tuple(
        reason
        for reason in standalone_acceptance.reasons
        if reason != "paired_calibration_bundle_required"
    )
    acceptance_report = replace(
        standalone_acceptance,
        audit_manifest_passed=threshold_derivation.protocol_passed,
        formal_accepted=formal_accepted,
        reasons=acceptance_reasons,
    )
    return SemanticCalibrationBundleReport(
        bundle_id=bundle.bundle_id,
        selection_manifest_id=bundle.selection.manifest_id,
        acceptance_manifest_id=bundle.acceptance.manifest_id,
        encoder_identity=bundle.acceptance.embedding_identity,
        thresholds=bundle.acceptance.thresholds,
        binding_passed=True,
        group_disjoint_passed=True,
        threshold_derivation=threshold_derivation,
        selection_report=selection_report,
        acceptance_report=acceptance_report,
        formal_accepted=formal_accepted,
        reasons=tuple(reasons),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agentpilot.search.semantic_calibration_cli",
        description=(
            "Evaluate a precomputed semantic manifest or paired formal bundle."
        ),
    )
    parser.add_argument("manifest")
    parser.add_argument(
        "--require-formal",
        action="store_true",
        help="return success only for a verified target-model paired bundle",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    threshold_derivation = None
    try:
        source = Path(arguments.manifest)
        document = json.loads(source.read_text(encoding="utf-8"))
        if isinstance(document, Mapping) and "bundle_id" in document:
            report = evaluate_semantic_calibration_bundle(
                parse_semantic_calibration_bundle(document)
            )
        else:
            manifest = parse_semantic_calibration_manifest(document)
            report = evaluate_semantic_calibration_manifest(manifest)
            if manifest.split is SemanticCalibrationSplit.CALIBRATION:
                threshold_derivation = derive_semantic_thresholds(manifest)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(
            canonical_json(
                {
                    "error": "invalid_semantic_calibration_manifest",
                    "message": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return 2
    if threshold_derivation is None:
        output = report
    else:
        output = json.loads(canonical_json(report))
        output["threshold_derivation"] = threshold_derivation
    print(canonical_json(output))
    if arguments.require_formal:
        passed = report.formal_accepted
    elif isinstance(report, (SemanticCalibrationBundleReport, ResearchCalibrationBundleReport)):
        passed = (
            report.threshold_derivation.protocol_passed
            and report.selection_report.diagnostic_passed
            and report.acceptance_report.diagnostic_passed
        )
    elif threshold_derivation is not None:
        passed = report.diagnostic_passed and threshold_derivation.protocol_passed
    else:
        passed = report.diagnostic_passed
    return 0 if passed else 1


__all__ = [
    "SEMANTIC_THRESHOLD_SELECTION_PROTOCOL",
    "SemanticCalibrationBundle",
    "SemanticCalibrationBundleReport",
    "SemanticCalibrationManifest",
    "SemanticCalibrationSplit",
    "SemanticThresholdDerivationReport",
    "derive_semantic_thresholds",
    "evaluate_semantic_calibration_bundle",
    "evaluate_semantic_calibration_manifest",
    "load_semantic_calibration_bundle",
    "load_semantic_calibration_manifest",
    "main",
    "parse_semantic_calibration_bundle",
    "parse_semantic_calibration_manifest",
    "validate_formal_semantic_calibration_bundle",
]
