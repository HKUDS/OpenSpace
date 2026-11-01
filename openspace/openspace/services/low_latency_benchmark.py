from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from openspace.services.runtime_support.low_latency import aggregate_low_latency_spans


REQUIRED_BASELINE_SCENARIOS = (
    "simple_chat",
    "code_task",
    "scheduler_task",
    "skill_task",
)
REQUIRED_BASELINE_SKILL_COUNTS = (0, 50, 500)
REQUIRED_BASELINE_TOOL_COUNTS = (10, 100, 500)

DEFAULT_EVENT_LATENCY_THRESHOLDS_MS: tuple[Mapping[str, Any], ...] = (
    {
        "label": "feishu_first_visible_event_p95",
        "name": "first_visible_event",
        "platform": "feishu",
        "max_p95_ms": 1000.0,
    },
    {
        "label": "simple_chat_warm_first_model_request_p95",
        "name": "first_model_request",
        "benchmark_scenario": "simple_chat",
        "cold_runtime": False,
        "max_p95_ms": 2000.0,
    },
    {
        "label": "simple_chat_cold_first_model_request_p95",
        "name": "first_model_request",
        "benchmark_scenario": "simple_chat",
        "cold_runtime": True,
        "max_p95_ms": 4000.0,
    },
    {
        "label": "code_task_warm_first_model_request_p95",
        "name": "first_model_request",
        "benchmark_scenario": "code_task",
        "cold_runtime": False,
        "max_p95_ms": 3000.0,
    },
    {
        "label": "code_task_cold_first_model_request_p95",
        "name": "first_model_request",
        "benchmark_scenario": "code_task",
        "cold_runtime": True,
        "max_p95_ms": 5000.0,
    },
)


@dataclass(frozen=True, slots=True)
class LowLatencyBenchmarkReport:
    span_records: int
    scenarios_seen: tuple[str, ...]
    profiles_seen: tuple[str, ...]
    backend_scopes_seen: tuple[tuple[str, ...], ...]
    skill_counts_seen: tuple[int, ...]
    tool_counts_seen: tuple[int, ...]
    cold_runtime_samples: int
    warm_runtime_samples: int
    missing_required_scenarios: tuple[str, ...]
    missing_required_skill_counts: tuple[int, ...]
    missing_required_tool_counts: tuple[int, ...]
    span_aggregates: list[dict[str, Any]] = field(default_factory=list)
    scenario_aggregates: list[dict[str, Any]] = field(default_factory=list)
    cold_warm_aggregates: list[dict[str, Any]] = field(default_factory=list)
    event_latency_aggregates: list[dict[str, Any]] = field(default_factory=list)
    event_latency_threshold_results: list[dict[str, Any]] = field(default_factory=list)
    span_aggregates_truncated: bool = False
    scenario_aggregates_truncated: bool = False
    cold_warm_aggregates_truncated: bool = False
    event_latency_aggregates_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        coverage_passed = (
            self.cold_runtime_samples > 0
            and self.warm_runtime_samples > 0
            and not self.missing_required_scenarios
            and not self.missing_required_skill_counts
            and not self.missing_required_tool_counts
        )
        event_latency_thresholds_passed = all(
            result.get("passed") is True
            for result in self.event_latency_threshold_results
        )
        return {
            "span_records": self.span_records,
            "records_count": self.span_records,
            "scenarios_seen": list(self.scenarios_seen),
            "profiles_seen": list(self.profiles_seen),
            "backend_scopes_seen": [list(scope) for scope in self.backend_scopes_seen],
            "skill_counts_seen": list(self.skill_counts_seen),
            "tool_counts_seen": list(self.tool_counts_seen),
            "cold_runtime_samples": self.cold_runtime_samples,
            "warm_runtime_samples": self.warm_runtime_samples,
            "missing_required_scenarios": list(self.missing_required_scenarios),
            "missing_required_skill_counts": list(self.missing_required_skill_counts),
            "missing_required_tool_counts": list(self.missing_required_tool_counts),
            "coverage": {
                "has_cold_runtime": self.cold_runtime_samples > 0,
                "has_warm_runtime": self.warm_runtime_samples > 0,
                "scenarios_seen": list(self.scenarios_seen),
                "missing_required_scenarios": list(self.missing_required_scenarios),
                "skill_counts_seen": list(self.skill_counts_seen),
                "missing_required_skill_counts": list(
                    self.missing_required_skill_counts
                ),
                "tool_counts_seen": list(self.tool_counts_seen),
                "missing_required_tool_counts": list(
                    self.missing_required_tool_counts
                ),
                "passed": coverage_passed,
            },
            "coverage_passed": coverage_passed,
            "span_aggregates": list(self.span_aggregates),
            "scenario_aggregates": list(self.scenario_aggregates),
            "cold_warm_aggregates": list(self.cold_warm_aggregates),
            "event_latency_aggregates": list(self.event_latency_aggregates),
            "event_latency_threshold_results": list(
                self.event_latency_threshold_results
            ),
            "event_latency_thresholds_passed": event_latency_thresholds_passed,
            "acceptance_passed": (
                coverage_passed and event_latency_thresholds_passed
            ),
            "span_aggregates_truncated": self.span_aggregates_truncated,
            "scenario_aggregates_truncated": self.scenario_aggregates_truncated,
            "cold_warm_aggregates_truncated": self.cold_warm_aggregates_truncated,
            "event_latency_aggregates_truncated": (
                self.event_latency_aggregates_truncated
            ),
        }


def build_low_latency_benchmark_report(
    records: Sequence[Mapping[str, Any]],
    *,
    required_scenarios: Sequence[str] = REQUIRED_BASELINE_SCENARIOS,
    required_skill_counts: Sequence[int] = REQUIRED_BASELINE_SKILL_COUNTS,
    required_tool_counts: Sequence[int] = REQUIRED_BASELINE_TOOL_COUNTS,
    event_latency_thresholds: Sequence[Mapping[str, Any]]
    | None = DEFAULT_EVENT_LATENCY_THRESHOLDS_MS,
    max_groups: int | None = None,
) -> LowLatencyBenchmarkReport:
    normalized = [_normalize_record(record) for record in records]
    scenarios_seen = tuple(sorted({record["benchmark_scenario"] for record in normalized}))
    profiles_seen = tuple(sorted({record["profile"] for record in normalized if record["profile"]}))
    backend_scopes_seen = tuple(
        sorted(
            {record["backend_scope"] for record in normalized},
            key=lambda scope: ",".join(scope),
        )
    )
    skill_counts_seen = tuple(
        sorted(
            {
                count
                for record in normalized
                if (count := record.get("skill_count")) is not None
            }
        )
    )
    tool_counts_seen = tuple(
        sorted(
            {
                count
                for record in normalized
                if (count := record.get("tool_count")) is not None
            }
        )
    )
    cold_runtime_samples = sum(
        1
        for record in normalized
        if record["name"] == "openspace.initialize"
        and record.get("cold_runtime") is True
    )
    warm_runtime_samples = sum(
        1
        for record in normalized
        if record["name"] == "openspace.initialize"
        and record.get("cold_runtime") is False
    )
    missing_required = tuple(
        scenario
        for scenario in required_scenarios
        if scenario not in scenarios_seen
    )
    missing_required_skill_counts = tuple(
        int(count)
        for count in required_skill_counts
        if int(count) not in skill_counts_seen
    )
    missing_required_tool_counts = tuple(
        int(count)
        for count in required_tool_counts
        if int(count) not in tool_counts_seen
    )
    all_span_aggregates = [
        aggregate.to_dict()
        for aggregate in aggregate_low_latency_spans(
            normalized,
            group_by=("name", "profile", "backend_scope"),
        )
    ]
    all_scenario_aggregates = [
        aggregate.to_dict()
        for aggregate in aggregate_low_latency_spans(
            normalized,
            group_by=("benchmark_scenario", "name", "profile", "backend_scope"),
        )
    ]
    cold_warm_records = [
        record
        for record in normalized
        if record.get("cold_runtime") is not None
    ]
    all_cold_warm_aggregates = [
        aggregate.to_dict()
        for aggregate in aggregate_low_latency_spans(
            cold_warm_records,
            group_by=(
                "benchmark_scenario",
                "name",
                "profile",
                "backend_scope",
                "cold_runtime",
            ),
        )
    ]
    all_event_latency_aggregates = [
        aggregate.to_dict()
        for aggregate in aggregate_low_latency_spans(
            _derive_event_latency_records(normalized),
            group_by=(
                "name",
                "platform",
                "benchmark_scenario",
                "profile",
                "backend_scope",
                "cold_runtime",
            ),
        )
    ]
    event_latency_threshold_results = _evaluate_event_latency_thresholds(
        all_event_latency_aggregates,
        event_latency_thresholds or (),
    )
    limit = None if max_groups is None else max(0, int(max_groups))
    span_aggregates = (
        all_span_aggregates
        if limit is None
        else all_span_aggregates[:limit]
    )
    scenario_aggregates = (
        all_scenario_aggregates
        if limit is None
        else all_scenario_aggregates[:limit]
    )
    cold_warm_aggregates = (
        all_cold_warm_aggregates
        if limit is None
        else all_cold_warm_aggregates[:limit]
    )
    event_latency_aggregates = (
        all_event_latency_aggregates
        if limit is None
        else all_event_latency_aggregates[:limit]
    )
    return LowLatencyBenchmarkReport(
        span_records=len(normalized),
        scenarios_seen=scenarios_seen,
        profiles_seen=profiles_seen,
        backend_scopes_seen=backend_scopes_seen,
        skill_counts_seen=skill_counts_seen,
        tool_counts_seen=tool_counts_seen,
        cold_runtime_samples=cold_runtime_samples,
        warm_runtime_samples=warm_runtime_samples,
        missing_required_scenarios=missing_required,
        missing_required_skill_counts=missing_required_skill_counts,
        missing_required_tool_counts=missing_required_tool_counts,
        span_aggregates=span_aggregates,
        scenario_aggregates=scenario_aggregates,
        cold_warm_aggregates=cold_warm_aggregates,
        event_latency_aggregates=event_latency_aggregates,
        event_latency_threshold_results=event_latency_threshold_results,
        span_aggregates_truncated=(
            limit is not None and len(all_span_aggregates) > limit
        ),
        scenario_aggregates_truncated=(
            limit is not None and len(all_scenario_aggregates) > limit
        ),
        cold_warm_aggregates_truncated=(
            limit is not None and len(all_cold_warm_aggregates) > limit
        ),
        event_latency_aggregates_truncated=(
            limit is not None and len(all_event_latency_aggregates) > limit
        ),
    )


def _normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    return {
        "name": record.get("name"),
        "duration_ms": record.get("duration_ms"),
        "started_at_ms": _coerce_float(record.get("started_at_ms")),
        "ended_at_ms": _coerce_float(record.get("ended_at_ms")),
        "correlation_id": (
            record.get("correlation_id")
            or metadata.get("correlation_id")
        ),
        "turn_id": record.get("turn_id") or metadata.get("turn_id"),
        "platform": record.get("platform") or metadata.get("platform"),
        "session_key": record.get("session_key") or metadata.get("session_key"),
        "profile": record.get("profile") or metadata.get("profile"),
        "skill_count": _coerce_int_optional(
            record.get("skill_count")
            if record.get("skill_count") is not None
            else (
                record.get("skills_count")
                if record.get("skills_count") is not None
                else (
                    metadata.get("skill_count")
                    if metadata.get("skill_count") is not None
                    else metadata.get("skills_count")
                )
            )
        ),
        "tool_count": _coerce_int_optional(
            record.get("all_tools_count")
            if record.get("all_tools_count") is not None
            else (
                record.get("tool_count")
                if record.get("tool_count") is not None
                else (
                    record.get("total_tools_count")
                    if record.get("total_tools_count") is not None
                    else (
                        metadata.get("all_tools_count")
                        if metadata.get("all_tools_count") is not None
                        else (
                            metadata.get("tool_count")
                            if metadata.get("tool_count") is not None
                            else metadata.get("total_tools_count")
                        )
                    )
                )
            )
        ),
        "backend_scope": _normalize_backend_scope(
            record.get("backend_scope") or metadata.get("backend_scope")
        ),
        "benchmark_scenario": (
            str(
                record.get("benchmark_scenario")
                or record.get("low_latency_benchmark_scenario")
                or metadata.get("benchmark_scenario")
                or metadata.get("low_latency_benchmark_scenario")
                or "unspecified"
            ).strip()
            or "unspecified"
        ),
        "cold_runtime": _coerce_bool(
            record.get("cold_runtime")
            if record.get("cold_runtime") is not None
            else metadata.get("cold_runtime")
        ),
        "metadata": dict(metadata),
    }


def _derive_event_latency_records(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    turn_starts: dict[str, float] = {}
    for record in records:
        if record.get("name") != "gateway.receive":
            continue
        turn_key = _turn_key(record)
        started_at_ms = _coerce_float(record.get("started_at_ms"))
        if turn_key is None or started_at_ms is None:
            continue
        existing = turn_starts.get(turn_key)
        if existing is None or started_at_ms < existing:
            turn_starts[turn_key] = started_at_ms

    derived: list[dict[str, Any]] = []
    for record in records:
        name = str(record.get("name") or "").strip()
        if name not in {
            "first_visible_event",
            "first_model_request",
            "llm.request_start",
            "llm.first_chunk",
            "reply.sent",
        }:
            continue
        turn_key = _turn_key(record)
        if turn_key is None:
            continue
        turn_start = turn_starts.get(turn_key)
        if turn_start is None:
            continue
        event_at_ms = _coerce_float(
            record.get("ended_at_ms") if name == "reply.sent" else record.get("started_at_ms")
        )
        if event_at_ms is None:
            continue
        derived.append(
            {
                **dict(record),
                "duration_ms": max(0.0, event_at_ms - turn_start),
                "metadata": {
                    **dict(record.get("metadata") or {}),
                    "latency_origin": "gateway.receive",
                },
            }
        )
    return derived


def _evaluate_event_latency_thresholds(
    aggregates: Sequence[Mapping[str, Any]],
    thresholds: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for threshold in thresholds:
        criteria = {
            str(key): value
            for key, value in threshold.items()
            if key not in {"label", "max_p95_ms"}
        }
        label = str(threshold.get("label") or _threshold_label(criteria))
        max_p95_ms = _coerce_float(threshold.get("max_p95_ms"))
        matches = [
            aggregate
            for aggregate in aggregates
            if _aggregate_matches_threshold(aggregate, criteria)
        ]
        observed_p95_ms = None
        if matches:
            observed_p95_ms = max(
                float(match.get("p95_ms") or 0.0)
                for match in matches
            )
        passed = (
            observed_p95_ms is not None
            and max_p95_ms is not None
            and observed_p95_ms <= max_p95_ms
        )
        status = "passed" if passed else "failed"
        if observed_p95_ms is None:
            status = "missing"
        results.append(
            {
                "label": label,
                "criteria": criteria,
                "max_p95_ms": max_p95_ms,
                "observed_p95_ms": observed_p95_ms,
                "matching_groups": len(matches),
                "passed": passed,
                "status": status,
            }
        )
    return results


def _aggregate_matches_threshold(
    aggregate: Mapping[str, Any],
    criteria: Mapping[str, Any],
) -> bool:
    group = aggregate.get("group")
    if not isinstance(group, Mapping):
        return False
    return all(
        _threshold_value_matches(group.get(key), expected)
        for key, expected in criteria.items()
    )


def _threshold_value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(actual, list):
        actual = tuple(actual)
    if isinstance(expected, list):
        expected = tuple(expected)
    return actual == expected


def _threshold_label(criteria: Mapping[str, Any]) -> str:
    return ",".join(f"{key}={value}" for key, value in sorted(criteria.items()))


def _turn_key(record: Mapping[str, Any]) -> str | None:
    correlation_id = str(record.get("correlation_id") or "").strip()
    if correlation_id:
        return correlation_id
    turn_id = str(record.get("turn_id") or "").strip()
    if turn_id:
        return turn_id
    return None


def _normalize_backend_scope(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    try:
        return tuple(str(item) for item in value if str(item))
    except TypeError:
        return (str(value),)


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _coerce_int_optional(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_EVENT_LATENCY_THRESHOLDS_MS",
    "LowLatencyBenchmarkReport",
    "REQUIRED_BASELINE_SCENARIOS",
    "REQUIRED_BASELINE_SKILL_COUNTS",
    "REQUIRED_BASELINE_TOOL_COUNTS",
    "build_low_latency_benchmark_report",
]
