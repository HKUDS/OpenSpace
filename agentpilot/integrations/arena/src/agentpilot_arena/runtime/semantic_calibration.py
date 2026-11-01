"""Strict semantic-calibration loading shared by Arena benchmark adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentpilot.app import AppConfig
from agentpilot.domain import PolicyMethod
from agentpilot.search import (
    EmbeddingIdentity,
    SemanticCalibrationBundle,
    SemanticCalibrationBundleReport,
    SemanticThresholds,
    evaluate_semantic_calibration_bundle,
    load_semantic_calibration_bundle,
    validate_formal_semantic_calibration_bundle,
)

from .errors import FormalRuntimeUnavailable


@dataclass(frozen=True, slots=True)
class FormalSemanticCalibration:
    """Canonical file, parsed bundle, and accepted offline report."""

    source: Path
    bundle: SemanticCalibrationBundle
    report: SemanticCalibrationBundleReport

    @property
    def provider(self) -> str:
        return self.report.encoder_identity.encoder_id.partition(":")[0]

    @property
    def model(self) -> str:
        return self.report.encoder_identity.encoder_id.partition(":")[2]


def _load_bundle(
    path: Path,
    *,
    benchmark: str,
) -> tuple[Path, SemanticCalibrationBundle]:
    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("semantic calibration path must be an absolute Path")
    try:
        source = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise FormalRuntimeUnavailable(
            f"{benchmark} semantic calibration bundle is unavailable"
        ) from exc
    if not source.is_file():
        raise FormalRuntimeUnavailable(
            f"{benchmark} semantic calibration bundle is not a regular file"
        )
    try:
        bundle = load_semantic_calibration_bundle(source)
    except (TypeError, ValueError) as exc:
        raise FormalRuntimeUnavailable(
            f"{benchmark} semantic calibration bundle is invalid: {exc}"
        ) from exc
    return source, bundle


def load_formal_semantic_calibration_authority(
    path: Path,
    *,
    benchmark: str,
) -> FormalSemanticCalibration:
    """Load one bundle and require its self-contained formal acceptance."""

    source, bundle = _load_bundle(path, benchmark=benchmark)
    try:
        report = evaluate_semantic_calibration_bundle(bundle)
    except (TypeError, ValueError) as exc:
        raise FormalRuntimeUnavailable(
            f"{benchmark} semantic calibration bundle is invalid: {exc}"
        ) from exc
    if not report.formal_accepted:
        detail = ", ".join(report.reasons) or "formal acceptance failed"
        raise FormalRuntimeUnavailable(
            f"{benchmark} semantic calibration bundle is invalid: "
            f"semantic calibration bundle rejected: {detail}"
        )
    return FormalSemanticCalibration(source=source, bundle=bundle, report=report)


def resolve_semantic_calibration_dependency(
    config: AppConfig,
    path: Path | None,
    *,
    benchmark: str,
) -> SemanticCalibrationBundle | None:
    """Resolve the exact optionality contract shared by Arena adapters."""

    if not isinstance(config, AppConfig):
        raise TypeError("config must be an AppConfig")
    if config.policy.method is not PolicyMethod.UCB:
        if path is not None:
            raise FormalRuntimeUnavailable(
                f"{benchmark} semantic calibration bundle is valid only for UCB"
            )
        return None
    if path is None:
        raise FormalRuntimeUnavailable(
            f"{benchmark} UCB requires a semantic calibration bundle"
        )
    if config.policy.ucb is None:
        raise ValueError("UCB policy config is incomplete")
    if not config.runtime.require_provider_reported_cost:
        raise FormalRuntimeUnavailable(f"{benchmark} UCB requires formal accounting")
    semantic = config.policy.ucb.semantic
    _source, bundle = _load_bundle(path, benchmark=benchmark)
    try:
        validate_formal_semantic_calibration_bundle(
            bundle,
            bundle_id=semantic.calibration_bundle_id,
            encoder_identity=EmbeddingIdentity(
                encoder_id=f"{semantic.provider}:{semantic.model}",
                encoder_identity=semantic.encoder_identity,
                dimension=semantic.dimension,
                distance=semantic.metric,
            ),
            thresholds=SemanticThresholds(
                threshold_id=semantic.threshold_id,
                tau_repeat=semantic.tau_repeat,
                tau_arm=semantic.tau_arm,
                threshold_boundary_margin=semantic.threshold_boundary_margin,
                nearest_arm_tie_tolerance=semantic.nearest_arm_tie_tolerance,
            ),
        )
    except (TypeError, ValueError) as exc:
        raise FormalRuntimeUnavailable(
            f"{benchmark} semantic calibration bundle is invalid: {exc}"
        ) from exc
    return bundle


__all__ = [
    "FormalSemanticCalibration",
    "load_formal_semantic_calibration_authority",
    "resolve_semantic_calibration_dependency",
]
