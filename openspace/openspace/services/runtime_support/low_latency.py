from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    name: str
    max_sync_startup_ms: int
    backend_scope: tuple[str, ...]
    default_active_tools: tuple[str, ...]
    hard_active_tool_limit: int
    allow_deferred_tools: bool = True
    enable_skill_listing: bool = True
    enable_turn0_skill_discovery: bool = True
    enable_turn0_llm_skill_selector: bool = False
    enable_skill_body_ranking: bool = False
    enable_lsp_sync_start: bool = False
    enable_scheduler_sync_start: bool = False
    enable_recording_summary_sync: bool = False
    memory_drain_timeout_s: float = 0.0
    post_execution_mode: str = "background"


_PROFILES: dict[str, CapabilityProfile] = {
    "interactive_fast": CapabilityProfile(
        name="interactive_fast",
        max_sync_startup_ms=1000,
        backend_scope=("shell", "meta"),
        default_active_tools=(
            "tool_search",
            "discover_skills",
            "ask_user_question",
            "brief",
        ),
        hard_active_tool_limit=12,
        enable_skill_listing=True,
        enable_turn0_skill_discovery=True,
        enable_turn0_llm_skill_selector=False,
        enable_skill_body_ranking=False,
        enable_lsp_sync_start=False,
        enable_scheduler_sync_start=False,
        enable_recording_summary_sync=False,
        memory_drain_timeout_s=0.0,
        post_execution_mode="background",
    ),
    "coding_task": CapabilityProfile(
        name="coding_task",
        max_sync_startup_ms=2500,
        backend_scope=("shell", "meta"),
        default_active_tools=(
            "tool_search",
            "discover_skills",
            "read",
            "read_file",
            "search",
            "grep",
            "glob",
            "edit",
            "write",
            "bash",
            "todo_read",
            "todo_write",
        ),
        hard_active_tool_limit=32,
        enable_turn0_llm_skill_selector=False,
    ),
    "agentic_task": CapabilityProfile(
        name="agentic_task",
        max_sync_startup_ms=3500,
        backend_scope=("shell", "meta"),
        default_active_tools=(
            "tool_search",
            "discover_skills",
            "read",
            "read_file",
            "search",
            "grep",
            "glob",
            "edit",
            "write",
            "bash",
            "todo_read",
            "todo_write",
            "Agent",
            "Task",
            "EnterPlanMode",
            "ExitPlanMode",
        ),
        hard_active_tool_limit=48,
        enable_turn0_llm_skill_selector=False,
    ),
    "batch_full": CapabilityProfile(
        name="batch_full",
        max_sync_startup_ms=0,
        backend_scope=("gui", "shell", "mcp", "web", "meta"),
        default_active_tools=(),
        hard_active_tool_limit=500,
        enable_turn0_llm_skill_selector=True,
        enable_skill_body_ranking=True,
        enable_lsp_sync_start=True,
        enable_scheduler_sync_start=True,
        enable_recording_summary_sync=True,
        memory_drain_timeout_s=3.0,
        post_execution_mode="inline",
    ),
}


def get_capability_profile(name: str | None) -> CapabilityProfile:
    key = str(name or "batch_full").strip() or "batch_full"
    return _PROFILES.get(key, _PROFILES["batch_full"])


def available_capability_profiles() -> tuple[str, ...]:
    return tuple(_PROFILES)


def new_correlation_id(
    *,
    platform: str,
    session_key: str,
    message_id: str | None = None,
) -> str:
    suffix = str(message_id or "").strip() or uuid.uuid4().hex[:12]
    return f"{platform}:{session_key}:{suffix}"


@dataclass(slots=True)
class LowLatencySpan:
    name: str
    started_at_ms: float
    ended_at_ms: float
    duration_ms: float
    metadata: dict[str, Any] = field(default_factory=dict)


class LowLatencyProfiler:
    """Best-effort per-turn span recorder.

    The profiler is intentionally side-effect-light: it records spans in memory
    and emits structured log lines. Callers can attach the serialized events to
    test results, health snapshots, or adapter metadata without changing the
    model prompt or transcript.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        correlation_id: str,
        session_key: str | None = None,
        profile: str | None = None,
        backend_scope: Sequence[str] | None = None,
        base_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.correlation_id = str(correlation_id)
        self.session_key = str(session_key) if session_key is not None else None
        self.profile = str(profile) if profile is not None else None
        self.backend_scope = tuple(str(item) for item in (backend_scope or ()))
        self.base_metadata = dict(base_metadata or {})
        self.events: list[LowLatencySpan] = []

    @contextmanager
    def span(self, name: str, **metadata: Any) -> Iterator[None]:
        if not self.enabled:
            yield
            return

        started = time.perf_counter() * 1000.0
        try:
            yield
        finally:
            ended = time.perf_counter() * 1000.0
            self.record(name, started_at_ms=started, ended_at_ms=ended, **metadata)

    def mark(self, name: str, **metadata: Any) -> None:
        if not self.enabled:
            return
        now = time.perf_counter() * 1000.0
        self.record(name, started_at_ms=now, ended_at_ms=now, **metadata)

    def record(
        self,
        name: str,
        *,
        started_at_ms: float,
        ended_at_ms: float,
        **metadata: Any,
    ) -> None:
        if not self.enabled:
            return
        merged = dict(self.base_metadata)
        merged.update(metadata)
        if self.profile is not None:
            merged.setdefault("profile", self.profile)
        if self.backend_scope:
            merged.setdefault("backend_scope", self.backend_scope)
        if self.session_key is not None:
            merged.setdefault("session_key", self.session_key)
        span = LowLatencySpan(
            name=str(name),
            started_at_ms=started_at_ms,
            ended_at_ms=ended_at_ms,
            duration_ms=max(0.0, ended_at_ms - started_at_ms),
            metadata=merged,
        )
        self.events.append(span)
        logger.info(
            "low_latency_span name=%s correlation_id=%s duration_ms=%.2f metadata=%s",
            span.name,
            self.correlation_id,
            span.duration_ms,
            span.metadata,
        )

    def as_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "name": event.name,
                "started_at_ms": event.started_at_ms,
                "ended_at_ms": event.ended_at_ms,
                "duration_ms": event.duration_ms,
                "metadata": dict(event.metadata),
            }
            for event in self.events
        ]


@dataclass(frozen=True, slots=True)
class LowLatencyMetricAggregate:
    group: dict[str, Any]
    count: int
    min_ms: float
    max_ms: float
    avg_ms: float
    p50_ms: float
    p90_ms: float
    p95_ms: float
    p99_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": dict(self.group),
            "count": self.count,
            "min_ms": self.min_ms,
            "max_ms": self.max_ms,
            "avg_ms": self.avg_ms,
            "p50_ms": self.p50_ms,
            "p90_ms": self.p90_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
        }


def aggregate_low_latency_spans(
    records: Sequence[Mapping[str, Any]],
    *,
    group_by: Sequence[str] = ("name", "session_key", "profile", "backend_scope"),
) -> list[LowLatencyMetricAggregate]:
    buckets: dict[tuple[Any, ...], list[float]] = {}
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        name = _optional_text(record.get("name"))
        if not name:
            continue
        try:
            duration = float(record.get("duration_ms"))
        except (TypeError, ValueError):
            continue
        if duration < 0:
            continue
        normalized = _normalize_metric_record(record)
        key = tuple(_metric_group_value(normalized.get(field)) for field in group_by)
        buckets.setdefault(key, []).append(duration)
        groups.setdefault(key, {field: normalized.get(field) for field in group_by})

    aggregates: list[LowLatencyMetricAggregate] = []
    for key, durations in buckets.items():
        ordered = sorted(durations)
        count = len(ordered)
        aggregates.append(
            LowLatencyMetricAggregate(
                group=groups[key],
                count=count,
                min_ms=ordered[0],
                max_ms=ordered[-1],
                avg_ms=sum(ordered) / count,
                p50_ms=_percentile(ordered, 50),
                p90_ms=_percentile(ordered, 90),
                p95_ms=_percentile(ordered, 95),
                p99_ms=_percentile(ordered, 99),
            )
        )
    aggregates.sort(key=lambda item: tuple(str(v) for v in item.group.values()))
    return aggregates


def _normalize_metric_record(record: Mapping[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    normalized = dict(metadata)
    for key, value in record.items():
        if key == "metadata":
            continue
        normalized[key] = value
    backend_scope = record.get("backend_scope", metadata.get("backend_scope", ()))
    normalized["name"] = record.get("name") or metadata.get("name")
    normalized["session_key"] = (
        record.get("session_key") or metadata.get("session_key")
    )
    normalized["profile"] = record.get("profile") or metadata.get("profile")
    normalized["backend_scope"] = _normalize_backend_scope(backend_scope)
    normalized["correlation_id"] = (
        record.get("correlation_id") or metadata.get("correlation_id")
    )
    return normalized


def _normalize_backend_scope(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    try:
        return tuple(str(item) for item in value if str(item))
    except TypeError:
        return (str(value),)


def _metric_group_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return value


def _percentile(sorted_values: Sequence[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * (float(percentile) / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return float(sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight)


@dataclass(frozen=True, slots=True)
class ActiveToolPolicy:
    profile: CapabilityProfile
    active_tool_names: frozenset[str]
    deferred_tool_names: frozenset[str]
    all_tool_names: frozenset[str]
    reason: str


@dataclass(frozen=True, slots=True)
class SessionCapabilityState:
    """Session-owned visibility state for low-latency turns.

    Transcript metadata remains a useful recovery source, but communication
    sessions need an explicit state object so history trimming and early
    feedback messages cannot change which deferred tools/skills are visible.
    """

    current_profile: str = "batch_full"
    discovered_tool_names: frozenset[str] = field(default_factory=frozenset)
    active_tool_names: frozenset[str] = field(default_factory=frozenset)
    deferred_tool_names: frozenset[str] = field(default_factory=frozenset)
    visible_skill_names: frozenset[str] = field(default_factory=frozenset)
    discovered_skill_names: frozenset[str] = field(default_factory=frozenset)
    active_skill_ids: frozenset[str] = field(default_factory=frozenset)
    last_intent_classification: str | None = None
    profile_upgrade_history: tuple[dict[str, Any], ...] = ()
    updated_turn_count: int = 0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "SessionCapabilityState":
        if isinstance(data, cls):
            return data
        if not data:
            return cls()
        profile = str(data.get("current_profile") or "batch_full").strip() or "batch_full"
        return cls(
            current_profile=get_capability_profile(profile).name,
            discovered_tool_names=_name_set(data.get("discovered_tool_names")),
            active_tool_names=_name_set(data.get("active_tool_names")),
            deferred_tool_names=_name_set(data.get("deferred_tool_names")),
            visible_skill_names=_name_set(data.get("visible_skill_names")),
            discovered_skill_names=_name_set(data.get("discovered_skill_names")),
            active_skill_ids=_name_set(data.get("active_skill_ids")),
            last_intent_classification=_optional_text(
                data.get("last_intent_classification")
            ),
            profile_upgrade_history=_profile_history(
                data.get("profile_upgrade_history")
            ),
            updated_turn_count=max(0, _coerce_int(data.get("updated_turn_count"))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_profile": self.current_profile,
            "discovered_tool_names": sorted(self.discovered_tool_names),
            "active_tool_names": sorted(self.active_tool_names),
            "deferred_tool_names": sorted(self.deferred_tool_names),
            "visible_skill_names": sorted(self.visible_skill_names),
            "discovered_skill_names": sorted(self.discovered_skill_names),
            "active_skill_ids": sorted(self.active_skill_ids),
            "last_intent_classification": self.last_intent_classification,
            "profile_upgrade_history": list(self.profile_upgrade_history),
            "updated_turn_count": self.updated_turn_count,
        }

    def merge_turn(
        self,
        *,
        profile_name: str | None = None,
        discovered_tool_names: Sequence[str] | set[str] | frozenset[str] = (),
        active_tool_names: Sequence[str] | set[str] | frozenset[str] = (),
        deferred_tool_names: Sequence[str] | set[str] | frozenset[str] = (),
        visible_skill_names: Sequence[str] | set[str] | frozenset[str] = (),
        discovered_skill_names: Sequence[str] | set[str] | frozenset[str] = (),
        active_skill_ids: Sequence[str] | set[str] | frozenset[str] = (),
        last_intent_classification: str | None = None,
        reason: str = "turn",
    ) -> "SessionCapabilityState":
        profile = get_capability_profile(profile_name or self.current_profile).name
        history = list(self.profile_upgrade_history)
        if profile != self.current_profile:
            history.append(
                {
                    "from": self.current_profile,
                    "to": profile,
                    "reason": str(reason or "turn"),
                }
            )
            history = history[-20:]
        return SessionCapabilityState(
            current_profile=profile,
            discovered_tool_names=frozenset(
                set(self.discovered_tool_names) | set(_name_set(discovered_tool_names))
            ),
            active_tool_names=_name_set(active_tool_names),
            deferred_tool_names=_name_set(deferred_tool_names),
            visible_skill_names=frozenset(
                set(self.visible_skill_names) | set(_name_set(visible_skill_names))
            ),
            discovered_skill_names=frozenset(
                set(self.discovered_skill_names)
                | set(_name_set(discovered_skill_names))
            ),
            active_skill_ids=_name_set(active_skill_ids),
            last_intent_classification=(
                _optional_text(last_intent_classification)
                or self.last_intent_classification
            ),
            profile_upgrade_history=tuple(history),
            updated_turn_count=self.updated_turn_count + 1,
        )


def _name_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        raw_values = [value]
    else:
        try:
            raw_values = list(value)
        except TypeError:
            raw_values = [value]
    return frozenset(
        text
        for item in raw_values
        if (text := str(item or "").strip())
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _profile_history(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    history: list[dict[str, Any]] = []
    for item in value[-20:]:
        if isinstance(item, Mapping):
            history.append(
                {
                    "from": _optional_text(item.get("from")),
                    "to": _optional_text(item.get("to")),
                    "reason": _optional_text(item.get("reason")) or "turn",
                }
            )
    return tuple(history)


_CODING_SIGNAL_WORDS = (
    "file",
    "path",
    "code",
    "traceback",
    "error",
    "exception",
    "test",
    "pytest",
    "implement",
    "modify",
    "fix",
    "edit",
    "write",
    "commit",
)

_AGENTIC_SIGNAL_WORDS = (
    "multi-step",
    "plan",
    "execute",
    "run",
    "create pr",
    "pull request",
    "subagent",
    "background",
)


def classify_capability_profile(
    instruction: str | None,
    *,
    default_profile: str = "interactive_fast",
) -> CapabilityProfile:
    """Cheap non-LLM profile classifier used before tool loading."""

    default = get_capability_profile(default_profile)
    if default.name == "batch_full":
        return default

    text = str(instruction or "").lower()
    if any(word in text for word in _AGENTIC_SIGNAL_WORDS):
        return get_capability_profile("agentic_task")
    if "```" in text or any(word in text for word in _CODING_SIGNAL_WORDS):
        return get_capability_profile("coding_task")
    return default


def build_active_tool_policy(
    *,
    profile_name: str | None,
    instruction: str | None,
    tools: Sequence[Any],
    hard_active_tool_limit: int | None = None,
) -> ActiveToolPolicy:
    profile = classify_capability_profile(
        instruction,
        default_profile=profile_name or "interactive_fast",
    )
    all_tool_names = tuple(
        name for tool in tools
        if (name := str(getattr(tool, "name", "") or "").strip())
    )
    all_tool_name_set = frozenset(all_tool_names)

    if profile.name == "batch_full":
        return ActiveToolPolicy(
            profile=profile,
            active_tool_names=all_tool_name_set,
            deferred_tool_names=frozenset(
                name
                for tool in tools
                if (name := str(getattr(tool, "name", "") or "").strip())
                and bool(getattr(tool, "is_deferred", False))
            ),
            all_tool_names=all_tool_name_set,
            reason="baseline_full",
        )

    limit = int(hard_active_tool_limit or profile.hard_active_tool_limit)
    limit = max(1, min(limit, profile.hard_active_tool_limit))
    requested = list(dict.fromkeys(profile.default_active_tools))
    reserve_tool_search = (
        "tool_search" in requested
        and "tool_search" not in all_tool_name_set
        and len(all_tool_name_set) > 1
    )
    tool_object_limit = max(1, limit - 1) if reserve_tool_search else limit

    active: list[str] = []
    tool_by_name = {
        str(getattr(tool, "name", "") or ""): tool
        for tool in tools
        if str(getattr(tool, "name", "") or "")
    }
    for name in requested:
        if name in tool_by_name and name not in active:
            active.append(name)
            if len(active) >= tool_object_limit:
                break

    if len(active) < tool_object_limit:
        for tool in tools:
            name = str(getattr(tool, "name", "") or "")
            if not name or name in active:
                continue
            if bool(getattr(tool, "always_load", False)):
                active.append(name)
                if len(active) >= tool_object_limit:
                    break

    active_set = frozenset(active)
    deferred = frozenset(name for name in all_tool_names if name not in active_set)
    return ActiveToolPolicy(
        profile=profile,
        active_tool_names=active_set,
        deferred_tool_names=deferred,
        all_tool_names=all_tool_name_set,
        reason="hard_allowlist",
    )
