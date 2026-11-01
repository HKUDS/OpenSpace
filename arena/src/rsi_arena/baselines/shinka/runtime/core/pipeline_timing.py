from __future__ import annotations

import math
import statistics
from typing import Any, Dict, Optional


def _duration_seconds(start_at: float, end_at: float) -> float:
    """Return a non-negative wall-clock duration in seconds."""
    return max(0.0, float(end_at) - float(start_at))


def _enrich_pipeline_breakdown(merged: Dict[str, Any]) -> Dict[str, Any]:
    """Add derived timing fields for queueing and accounted pipeline time."""
    merged["post_eval_queue_wait_seconds"] = _duration_seconds(
        merged["evaluation_finished_at"], merged["postprocess_started_at"]
    )
    merged["pipeline_accounted_seconds"] = (
        merged["sampling_seconds"]
        + merged["evaluation_seconds"]
        + merged["post_eval_queue_wait_seconds"]
        + merged["postprocess_seconds"]
    )
    merged["pipeline_unaccounted_seconds"] = max(
        0.0, merged["pipeline_seconds"] - merged["pipeline_accounted_seconds"]
    )
    return merged


def with_pipeline_timing(
    metadata: Optional[Dict[str, Any]],
    *,
    pipeline_started_at: float,
    sampling_started_at: float,
    sampling_finished_at: float,
    evaluation_started_at: float,
    evaluation_finished_at: float,
    postprocess_started_at: float,
    postprocess_finished_at: float,
) -> Dict[str, Any]:
    """Merge pipeline stage boundaries and durations into program metadata."""
    merged = dict(metadata or {})

    merged.update(
        {
            "pipeline_started_at": pipeline_started_at,
            "sampling_started_at": sampling_started_at,
            "sampling_finished_at": sampling_finished_at,
            "evaluation_started_at": evaluation_started_at,
            "evaluation_finished_at": evaluation_finished_at,
            "postprocess_started_at": postprocess_started_at,
            "postprocess_finished_at": postprocess_finished_at,
        }
    )

    merged["sampling_seconds"] = _duration_seconds(
        sampling_started_at, sampling_finished_at
    )
    merged["evaluation_seconds"] = _duration_seconds(
        evaluation_started_at, evaluation_finished_at
    )
    merged["postprocess_seconds"] = _duration_seconds(
        postprocess_started_at, postprocess_finished_at
    )
    merged["pipeline_seconds"] = _duration_seconds(
        pipeline_started_at, postprocess_finished_at
    )
    _enrich_pipeline_breakdown(merged)
    # Preserve legacy semantics used across the existing UI and examples.
    merged["compute_time"] = merged["evaluation_seconds"]
    return merged


def with_side_effect_timing(
    metadata: Optional[Dict[str, Any]],
    *,
    apply_started_at: float,
    apply_finished_at: float,
) -> Dict[str, Any]:
    """Merge side-effect timing fields into existing pipeline metadata."""
    merged = dict(metadata or {})
    merged["postprocess_apply_started_at"] = apply_started_at
    merged["postprocess_apply_finished_at"] = apply_finished_at
    merged["postprocess_apply_wait_seconds"] = _duration_seconds(
        float(merged.get("postprocess_finished_at", apply_started_at)),
        apply_started_at,
    )
    merged["postprocess_apply_seconds"] = _duration_seconds(
        apply_started_at, apply_finished_at
    )

    pipeline_started_at = merged.get("pipeline_started_at")
    if isinstance(pipeline_started_at, (int, float)):
        merged["end_to_end_with_side_effects_seconds"] = _duration_seconds(
            float(pipeline_started_at), apply_finished_at
        )
        merged["end_to_end_accounted_seconds"] = (
            float(merged.get("pipeline_accounted_seconds", 0.0))
            + merged["postprocess_apply_wait_seconds"]
            + merged["postprocess_apply_seconds"]
        )
        merged["end_to_end_unaccounted_seconds"] = max(
            0.0,
            merged["end_to_end_with_side_effects_seconds"]
            - merged["end_to_end_accounted_seconds"],
        )

    return merged


def summarize_timing_metadata(
    metadata_rows: list[Dict[str, Any]],
    metrics: list[str],
) -> Dict[str, Dict[str, float]]:
    """Return summary stats for requested numeric timing metrics."""
    summary: Dict[str, Dict[str, float]] = {}

    for metric in metrics:
        values = []
        for row in metadata_rows:
            value = row.get(metric)
            if isinstance(value, (int, float)) and math.isfinite(value):
                values.append(float(value))
        if not values:
            continue

        sorted_values = sorted(values)
        p90_index = max(0, math.ceil(0.9 * len(sorted_values)) - 1)
        summary[metric] = {
            "count": float(len(sorted_values)),
            "mean": float(statistics.fmean(sorted_values)),
            "median": float(statistics.median(sorted_values)),
            "p90": float(sorted_values[p90_index]),
            "max": float(sorted_values[-1]),
        }

    return summary
