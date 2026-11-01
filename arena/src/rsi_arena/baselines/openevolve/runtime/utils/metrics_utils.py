"""
Safe calculation utilities for metrics containing mixed types
"""

from typing import Any, Dict, List, Optional


def safe_numeric_average(metrics: Dict[str, Any]) -> float:
    """
    Calculate the average of numeric values in a metrics dictionary,
    safely ignoring non-numeric values like strings.

    Args:
        metrics: Dictionary of metric names to values

    Returns:
        Average of numeric values, or 0.0 if no numeric values found
    """
    if not metrics:
        return 0.0

    numeric_values = []
    for value in metrics.values():
        # bool is a subclass of int, but a flag is not a score. Averaging it in
        # would let e.g. {"error": 0.0, "timeout": True} score 0.5 instead of 0.0,
        # giving a program that timed out a mid-range fitness.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                # Convert to float and check if it's a valid number
                float_val = float(value)
                if not (float_val != float_val):  # Check for NaN (NaN != NaN is True)
                    numeric_values.append(float_val)
            except (ValueError, TypeError, OverflowError):
                # Skip invalid numeric values
                continue

    if not numeric_values:
        return 0.0

    return sum(numeric_values) / len(numeric_values)


def safe_numeric_sum(metrics: Dict[str, Any]) -> float:
    """
    Calculate the sum of numeric values in a metrics dictionary,
    safely ignoring non-numeric values like strings.

    Args:
        metrics: Dictionary of metric names to values

    Returns:
        Sum of numeric values, or 0.0 if no numeric values found
    """
    if not metrics:
        return 0.0

    numeric_sum = 0.0
    for value in metrics.values():
        if isinstance(value, (int, float)):
            try:
                # Convert to float and check if it's a valid number
                float_val = float(value)
                if not (float_val != float_val):  # Check for NaN (NaN != NaN is True)
                    numeric_sum += float_val
            except (ValueError, TypeError, OverflowError):
                # Skip invalid numeric values
                continue

    return numeric_sum


def get_fitness_score(
    metrics: Dict[str, Any], feature_dimensions: Optional[List[str]] = None
) -> float:
    """
    Calculate fitness score, excluding MAP-Elites feature dimensions

    This ensures that MAP-Elites features don't pollute the fitness calculation
    when combined_score is not available.

    Args:
        metrics: All metrics from evaluation
        feature_dimensions: List of MAP-Elites dimensions to exclude from fitness

    Returns:
        Fitness score (combined_score if available, otherwise average of non-feature metrics)
    """
    if not metrics:
        return 0.0

    # Always prefer combined_score if available
    if "combined_score" in metrics:
        try:
            return float(metrics["combined_score"])
        except (ValueError, TypeError):
            pass

    # Otherwise, average only non-feature metrics
    feature_dimensions = feature_dimensions or []
    fitness_metrics = {}

    for key, value in metrics.items():
        # Exclude MAP feature dimensions from fitness calculation
        if key not in feature_dimensions:
            # Booleans are flags, not scores - see the note in safe_numeric_average.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                try:
                    float_val = float(value)
                    if not (float_val != float_val):  # Check for NaN
                        fitness_metrics[key] = float_val
                except (ValueError, TypeError, OverflowError):
                    continue

    # If no non-feature metrics, fall back to all metrics (backward compatibility)
    if not fitness_metrics:
        return safe_numeric_average(metrics)

    return safe_numeric_average(fitness_metrics)


def format_feature_coordinates(metrics: Dict[str, Any], feature_dimensions: List[str]) -> str:
    """
    Format feature coordinates for display in prompts

    Args:
        metrics: All metrics from evaluation
        feature_dimensions: List of MAP-Elites feature dimensions

    Returns:
        Formatted string showing feature coordinates
    """
    feature_values = []
    for dim in feature_dimensions:
        if dim in metrics:
            value = metrics[dim]
            if isinstance(value, (int, float)):
                try:
                    float_val = float(value)
                    if not (float_val != float_val):  # Check for NaN
                        feature_values.append(f"{dim}={float_val:.2f}")
                except (ValueError, TypeError, OverflowError):
                    feature_values.append(f"{dim}={value}")
            else:
                feature_values.append(f"{dim}={value}")

    if not feature_values:  # No valid feature coordinates found will return empty string
        return ""

    return ", ".join(feature_values)
