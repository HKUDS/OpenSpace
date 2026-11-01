"""Selection-only calibration for the two semantic decisions used by research.

Arm membership and implementation repetition use different encoder inputs.
This protocol tests each actual predicate. It permits conservative missed arm
matches while requiring zero false merges on both frozen splits.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math

from .direction_semantic_index import EmbeddingIdentity, SemanticThresholds, cosine_distance
from .semantic_threshold_acceptance import SemanticFixtureProvenance, SemanticPairLabel

PROTOCOL = 'conservative-dual-view-v1'
NOVEL_ARM_RECALL = .5


@dataclass(frozen=True)
class ResearchThresholdDerivation:
    protocol: str
    selection_manifest_id: str
    derived_tau_repeat: float | None
    derived_tau_arm: float | None
    minimum_novel_arm_recall: float
    frozen_threshold_boundary_margin: float
    configured_taus_match: bool
    protocol_passed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ResearchSplitReport:
    manifest_id: str
    split: str
    case_group_count: int
    coverage_passed: bool
    provenance_passed: bool
    repeat_true_positives: int
    repeat_positive_cases: int
    repeat_false_positives: int
    repeat_negative_cases: int
    arm_paraphrase_true_positives: int
    arm_paraphrase_cases: int
    arm_novel_true_positives: int
    arm_novel_cases: int
    arm_false_positives: int
    arm_negative_cases: int
    diagnostic_passed: bool
    reasons: tuple[str, ...]
    formal_accepted: bool = False


@dataclass(frozen=True)
class ResearchCalibrationBundleReport:
    bundle_id: str
    selection_manifest_id: str
    acceptance_manifest_id: str
    encoder_identity: EmbeddingIdentity
    thresholds: SemanticThresholds
    threshold_derivation: ResearchThresholdDerivation
    selection_report: ResearchSplitReport
    acceptance_report: ResearchSplitReport
    binding_passed: bool
    group_disjoint_passed: bool
    formal_accepted: bool
    reasons: tuple[str, ...]


def distances(manifest):
    result = {'arm': {}, 'realization': {}}
    for view in result:
        result[view] = {label: [] for label in SemanticPairLabel}
    for case in manifest.fixture.cases:
        def view(endpoint):
            keys = set(json.loads(endpoint.canonical_input))
            if keys == {'failure_mechanism'}:
                return 'arm'
            if keys == {'implementation_direction', 'resolved_scope', 'new_paths'}:
                return 'realization'
            raise ValueError('Research calibration must use the actual separate arm and realization inputs')
        space = view(case.anchor)
        if view(case.candidate) != space:
            raise ValueError('A calibration pair cannot mix encoder input views')
        if (case.anchor.embedding_identity != manifest.embedding_identity
                or case.candidate.embedding_identity != manifest.embedding_identity):
            raise ValueError('Calibration pair uses another encoder identity')
        result[space][case.label].append(cosine_distance(case.anchor.embedding, case.candidate.embedding))
    return result


def evaluate_split(manifest):
    d = distances(manifest)
    repeat, novel, distinct = SemanticPairLabel
    # Both views must carry every label, with the configured total coverage.
    minimum = math.ceil(manifest.requirements.minimum_cases_per_label / 2)
    coverage = all(len(d[view][label]) >= minimum for view in d for label in SemanticPairLabel)
    coverage = coverage and len(manifest.group_ids) >= minimum
    margin = manifest.thresholds.threshold_boundary_margin
    rcut = manifest.thresholds.tau_repeat - margin
    acut = manifest.thresholds.tau_arm - margin
    rpos, rneg = d['realization'][repeat], d['realization'][novel] + d['realization'][distinct]
    ap, an, ad = d['arm'][repeat], d['arm'][novel], d['arm'][distinct]
    tp = sum(v < rcut for v in rpos)
    rfp = sum(v < rcut for v in rneg)
    aptp = sum(v < acut for v in ap)
    antp = sum(v < acut for v in an)
    afp = sum(v < acut for v in ad)
    reasons = []
    if not coverage:
        reasons.append('insufficient_cases_or_groups_in_an_encoder_view')
    if not rpos or tp != len(rpos):
        reasons.append('missed_repeat_paraphrase')
    if rfp:
        reasons.append('false_implementation_repeat')
    if not ap or aptp != len(ap):
        reasons.append('missed_arm_paraphrase')
    if not an or antp < math.ceil(len(an) * NOVEL_ARM_RECALL):
        reasons.append('insufficient_novel_arm_recall')
    if afp:
        reasons.append('false_mechanism_merge')
    return ResearchSplitReport(manifest.manifest_id, manifest.split.value, len(manifest.group_ids),
        coverage, manifest.fixture.provenance is SemanticFixtureProvenance.TARGET_MODEL_LABELED,
        tp, len(rpos), rfp, len(rneg), aptp, len(ap), antp, len(an), afp, len(ad),
        not reasons, tuple(reasons))


def derive(manifest):
    if manifest.split.value != 'calibration':
        raise ValueError('Threshold selection must not read the acceptance split')
    d = distances(manifest)
    repeat, novel, _ = SemanticPairLabel
    rp, ap, an = d['realization'][repeat], d['arm'][repeat], sorted(d['arm'][novel])
    margin = manifest.thresholds.threshold_boundary_margin
    taus = (None, None)
    reasons = []
    if not rp or not ap or not an:
        reasons.append('selection_split_missing_required_positive_cases')
    else:
        # Use the smallest coverage radius with a fixed margin of clearance.
        # No acceptance distances, quantiles or errors participate in selection.
        taus = (max(rp) + 2 * margin,
                max(max(ap), an[math.ceil(len(an) * NOVEL_ARM_RECALL) - 1]) + 2 * margin)
    matched = taus == (manifest.thresholds.tau_repeat, manifest.thresholds.tau_arm)
    if taus[0] is not None:
        from dataclasses import replace
        try:
            chosen = replace(manifest.thresholds, tau_repeat=taus[0], tau_arm=taus[1])
            report = evaluate_split(replace(manifest, thresholds=chosen))
            reasons.extend(report.reasons)
        except ValueError:
            reasons.append('derived_thresholds_conflict_with_frozen_margin')
    if not matched:
        reasons.append('configured_taus_differ_from_selection_derivation')
    return ResearchThresholdDerivation(PROTOCOL, manifest.manifest_id, *taus, NOVEL_ARM_RECALL,
                                       margin, matched, not reasons, tuple(reasons))


def evaluate_bundle(bundle):
    derived = derive(bundle.selection)
    selection, acceptance = evaluate_split(bundle.selection), evaluate_split(bundle.acceptance)
    reasons = list(derived.reasons)
    if not selection.diagnostic_passed:
        reasons.append('selection_split_failed_dual_view_checks')
    if not acceptance.diagnostic_passed:
        reasons.append('acceptance_split_failed_dual_view_checks')
    if not selection.provenance_passed or not acceptance.provenance_passed:
        reasons.append('calibration_requires_measured_target_model_vectors')
    return ResearchCalibrationBundleReport(bundle.bundle_id, bundle.selection.manifest_id, bundle.acceptance.manifest_id,
        bundle.selection.embedding_identity, bundle.selection.thresholds, derived, selection, acceptance,
        True, True, not reasons, tuple(reasons))
