"""Canonical observable-trace evidence contracts.

The records in this module deliberately separate three concerns:

* ``NormalizedTrace`` is the exact observable payload;
* ``TraceArtifactRef`` identifies one stored measured occurrence; and
* ``TraceEvidenceManifest`` freezes the complete set of occurrences and case
  facts exposed to one search snapshot.

Adapters extract observable events and remove hidden reasoning before calling
``normalize_observable_trace``.  The normalizer then provides the last shared,
deterministic boundary: newline normalization, secret replacement, bounds,
contiguous ordinals and canonical JSON encoding.
"""

# Persistent format and evaluation identifiers retain their historical values.

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from agentpilot.domain.common import (
    canonical_json,
    require_aware,
    require_text,
    stable_id,
)
from agentpilot.domain.evaluation import EvidenceTier
from agentpilot.domain.search import CompatibilityKey


NORMALIZED_TRACE_SCHEMA = "openthink.normalized-observable-trace"
TRACE_MAX_EVENTS = 100_000
TRACE_MAX_CONTENT_CHARS = 1_000_000
TRACE_MAX_ACTOR_CHARS = 128
TRACE_MAX_NAME_CHARS = 512
TRACE_MAX_ELAPSED_MS = 7 * 24 * 60 * 60 * 1_000
TRACE_MAX_SIGNATURES = 256
TRACE_MAX_SIGNATURE_CHARS = 512
TRACE_MAX_REASON_CHARS = 1_000
TRACE_MAX_CASE_INDEX_ENTRIES = 100_000
TRACE_MAX_CASE_INDEX_BYTES = 16 * 1024 * 1024

REDACTION_MARKER = "[REDACTED]"
"""The one replacement text every adapter and the normalizer must use.

Two redaction passes over the same secret must produce identical bytes, so
adapters that pre-redact before ``normalize_observable_trace`` import this
marker instead of inventing their own.
"""


def _bounded_text(
    value: str, name: str, *, limit: int, empty: bool = False, one_line: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    if not empty:
        require_text(value, name)
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    if "\r" in value:
        raise ValueError(f"{name} must use normalized newlines")
    if one_line and ("\n" in value or value.strip() != value):
        raise ValueError(f"{name} must be one trimmed line")
    return value


def _optional_line(value: str | None, name: str, *, limit: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, name, limit=limit, one_line=True)


def _non_negative_int(value: int, name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be in [0, {maximum}]")
    return value


def _normalize_newlines(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def ordered_secret_values(secret_values: Iterable[str]) -> tuple[str, ...]:
    """Order secrets deterministically: longest first, ties lexicographic.

    Longest-first keeps overlapping secrets stable; the lexicographic tie
    break removes the set-iteration nondeterminism adapters would otherwise
    reintroduce when they pre-redact.
    """

    secrets = []
    for value in secret_values:
        if not isinstance(value, str):
            raise TypeError("secret_values must contain strings")
        if value:
            secrets.append(value)
    return tuple(sorted(set(secrets), key=lambda item: (-len(item), item)))


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    if not isinstance(value, str):
        raise TypeError("trace field must be text")
    for secret in secrets:
        value = value.replace(secret, REDACTION_MARKER)
    return value


class TraceEventKind(str, Enum):
    MESSAGE = "message"
    TOOL_INTENT = "tool_intent"
    TOOL_RESULT = "tool_result"
    TERMINAL = "terminal"
    ARTIFACT = "artifact"
    FINAL = "final"


@dataclass(frozen=True)
class TraceEvent:
    ordinal: int
    kind: TraceEventKind
    actor: str | None
    name: str | None
    content: str
    elapsed_ms: int | None

    def __post_init__(self) -> None:
        _non_negative_int(self.ordinal, "trace event ordinal", maximum=TRACE_MAX_EVENTS)
        if not isinstance(self.kind, TraceEventKind):
            raise TypeError("trace event kind must be TraceEventKind")
        _optional_line(
            self.actor, "trace event actor", limit=TRACE_MAX_ACTOR_CHARS,
        )
        _optional_line(
            self.name, "trace event name", limit=TRACE_MAX_NAME_CHARS,
        )
        _bounded_text(
            self.content,
            "trace event content",
            limit=TRACE_MAX_CONTENT_CHARS,
            empty=True,
        )
        if self.elapsed_ms is not None:
            _non_negative_int(
                self.elapsed_ms, "trace event elapsed_ms", maximum=TRACE_MAX_ELAPSED_MS,
            )


@dataclass(frozen=True)
class NormalizedTrace:
    schema: str
    trace_contract: str
    partial: bool
    events: tuple[TraceEvent, ...]

    def __post_init__(self) -> None:
        if self.schema != NORMALIZED_TRACE_SCHEMA:
            raise ValueError("normalized trace schema is unsupported")
        _bounded_text(
            self.trace_contract, "trace_contract", limit=256, one_line=True,
        )
        if not isinstance(self.partial, bool):
            raise TypeError("normalized trace partial must be boolean")
        events = tuple(self.events)
        object.__setattr__(self, "events", events)
        if not events or len(events) > TRACE_MAX_EVENTS:
            raise ValueError(f"normalized trace needs 1..{TRACE_MAX_EVENTS} events")
        if any(not isinstance(event, TraceEvent) for event in events):
            raise TypeError("normalized trace events must be TraceEvent values")
        if tuple(event.ordinal for event in events) != tuple(range(len(events))):
            raise ValueError(
                "normalized trace event ordinals must be contiguous from zero"
            )

    def canonical_bytes(self) -> bytes:
        """Return the sole byte representation eligible for storage."""

        return canonical_json(self).encode("utf-8")


def normalize_observable_trace(
    *,
    trace_contract: str,
    events: Iterable[TraceEvent | Mapping[str, object]],
    partial: bool = False,
    secret_values: Iterable[str] = (),
) -> NormalizedTrace:
    """Normalize adapter-extracted observable events deterministically.

    The adapter owns extraction and must never pass hidden reasoning fields.
    Mapping inputs use the exact six-field ``TraceEvent`` schema; extra fields
    are rejected so adapter-native metadata cannot silently affect identity.
    Secrets are replaced longest-first, which makes overlapping values stable.
    """

    ordered_secrets = ordered_secret_values(secret_values)
    normalized: list[TraceEvent] = []
    expected_fields = {"ordinal", "kind", "actor", "name", "content", "elapsed_ms"}
    for item in events:
        if isinstance(item, TraceEvent):
            values: Mapping[str, object] = {
                "ordinal": item.ordinal,
                "kind": item.kind,
                "actor": item.actor,
                "name": item.name,
                "content": item.content,
                "elapsed_ms": item.elapsed_ms,
            }
        elif isinstance(item, Mapping):
            if set(item) != expected_fields:
                raise ValueError("trace event fields differ from the canonical schema")
            values = item
        else:
            raise TypeError("events must contain TraceEvent values or mappings")
        kind = values["kind"]
        try:
            normalized_kind = (
                kind if isinstance(kind, TraceEventKind) else TraceEventKind(kind)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("trace event kind is unsupported") from exc
        actor = values["actor"]
        name = values["name"]
        content = values["content"]
        if actor is not None:
            actor = _normalize_newlines(
                _redact(actor, ordered_secrets), "trace event actor"
            )
        if name is not None:
            name = _normalize_newlines(
                _redact(name, ordered_secrets), "trace event name"
            )
        content = _normalize_newlines(
            _redact(content, ordered_secrets), "trace event content"
        )
        normalized.append(
            TraceEvent(
                ordinal=values["ordinal"],  # type: ignore[arg-type]
                kind=normalized_kind,
                actor=actor,
                name=name,
                content=content,
                elapsed_ms=values["elapsed_ms"],  # type: ignore[arg-type]
            )
        )
    return NormalizedTrace(
        schema=NORMALIZED_TRACE_SCHEMA,
        trace_contract=trace_contract,
        partial=partial,
        events=tuple(normalized),
    )


def decode_normalized_trace(payload: bytes) -> NormalizedTrace:
    """Decode and re-canonicalize an immutable trace payload."""

    if not isinstance(payload, bytes):
        raise TypeError("normalized trace payload must be bytes")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("normalized trace payload is not UTF-8 JSON") from exc
    if not isinstance(document, Mapping) or set(document) != {
        "schema",
        "trace_contract",
        "partial",
        "events",
    }:
        raise ValueError("normalized trace payload fields differ from the schema")
    raw_events = document["events"]
    if not isinstance(raw_events, list):
        raise ValueError("normalized trace events must be an array")
    trace = normalize_observable_trace(
        trace_contract=document["trace_contract"],  # type: ignore[arg-type]
        partial=document["partial"],  # type: ignore[arg-type]
        events=raw_events,
    )
    if trace.schema != document["schema"] or trace.canonical_bytes() != payload:
        raise ValueError("normalized trace payload is not canonically encoded")
    return trace


def summarize_trace_actions(trace: NormalizedTrace) -> tuple[int, tuple[str, ...]]:
    """Compute the mechanical tool-action facts for the trusted case index.

    Returns the exact tool-intent event count and the sorted signatures of
    actions the run issued more than once. Both values derive only from the
    immutable normalized payload, so every holder of the same trace computes
    the same facts, and the proposer never has to infer them from spans.
    """

    if not isinstance(trace, NormalizedTrace):
        raise TypeError("trace must be NormalizedTrace")
    tool_calls = 0
    occurrences: dict[str, int] = {}
    for event in trace.events:
        if event.kind is not TraceEventKind.TOOL_INTENT:
            continue
        tool_calls += 1
        collapsed = " ".join(f"{event.name or ''} {event.content}".split())
        signature = collapsed[:TRACE_MAX_SIGNATURE_CHARS].strip()
        if signature:
            occurrences[signature] = occurrences.get(signature, 0) + 1
    repeated = tuple(
        sorted(
            signature
            for signature, count in occurrences.items()
            if count > 1
        )
    )[:TRACE_MAX_SIGNATURES]
    return tool_calls, repeated


class TraceCaptureStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    MISSING = "missing"
    INVALID = "invalid"


@dataclass(frozen=True)
class AdapterTraceCapability:
    adapter_id: str
    format: str
    supports_complete: bool
    supports_timeout_partial: bool
    trace_contract: str

    def __post_init__(self) -> None:
        for name in ("adapter_id", "format", "trace_contract"):
            _bounded_text(getattr(self, name), name, limit=256, one_line=True)
        for name in ("supports_complete", "supports_timeout_partial"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        if not (self.supports_complete or self.supports_timeout_partial):
            raise ValueError(
                "adapter trace capability must support at least one capture"
            )


@dataclass(frozen=True)
class TraceCaptureCoordinates:
    program_id: str
    commit_sha: str
    tree_sha: str
    cell_id: str
    task_id: str
    replicate: int
    available_at: datetime

    def __post_init__(self) -> None:
        for name in ("program_id", "commit_sha", "tree_sha", "cell_id", "task_id"):
            _bounded_text(getattr(self, name), name, limit=512, one_line=True)
        _non_negative_int(self.replicate, "replicate", maximum=1_000_000)
        require_aware(self.available_at, "available_at")


@dataclass(frozen=True)
class TraceCaptureRecord:
    adapter_id: str
    program_id: str
    cell_id: str
    status: TraceCaptureStatus
    trace_ref_id: str | None
    reason: str | None
    tool_call_count: int = 0
    repeated_action_signatures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("adapter_id", "program_id", "cell_id"):
            _bounded_text(getattr(self, name), name, limit=512, one_line=True)
        if not isinstance(self.status, TraceCaptureStatus):
            raise TypeError("capture status must be TraceCaptureStatus")
        if self.status in {TraceCaptureStatus.COMPLETE, TraceCaptureStatus.PARTIAL}:
            require_text(self.trace_ref_id, "trace_ref_id")  # type: ignore[arg-type]
        elif self.trace_ref_id is not None:
            raise ValueError("missing/invalid capture cannot carry trace_ref_id")
        if self.reason is not None:
            _bounded_text(
                self.reason,
                "capture reason",
                limit=TRACE_MAX_REASON_CHARS,
                one_line=True,
            )
        if self.status in {TraceCaptureStatus.MISSING, TraceCaptureStatus.INVALID}:
            require_text(self.reason, "capture reason")  # type: ignore[arg-type]
        _non_negative_int(
            self.tool_call_count, "tool_call_count", maximum=TRACE_MAX_EVENTS,
        )
        object.__setattr__(
            self,
            "repeated_action_signatures",
            _signatures(self.repeated_action_signatures, "repeated_action_signatures"),
        )
        if self.trace_ref_id is None and (
            self.tool_call_count or self.repeated_action_signatures
        ):
            raise ValueError("missing/invalid capture cannot carry action facts")


@dataclass(frozen=True)
class TraceArtifactRef:
    trace_ref_id: str
    program_id: str
    commit_sha: str
    tree_sha: str
    cell_id: str
    task_id: str
    replicate: int
    evidence_tier: EvidenceTier
    format: str
    byte_count: int
    event_count: int
    trace_contract: str
    available_at: datetime

    @classmethod
    def create(
        cls,
        *,
        coordinates: TraceCaptureCoordinates,
        format: str,
        byte_count: int,
        event_count: int,
        trace_contract: str,
    ) -> "TraceArtifactRef":
        trace_ref_id = stable_id(
            "trace-ref",
            coordinates.program_id,
            coordinates.commit_sha,
            coordinates.tree_sha,
            coordinates.cell_id,
            coordinates.task_id,
            coordinates.replicate,
            trace_contract,
        )
        return cls(
            trace_ref_id=trace_ref_id,
            program_id=coordinates.program_id,
            commit_sha=coordinates.commit_sha,
            tree_sha=coordinates.tree_sha,
            cell_id=coordinates.cell_id,
            task_id=coordinates.task_id,
            replicate=coordinates.replicate,
            evidence_tier=EvidenceTier.VISIBLE_A,
            format=format,
            byte_count=byte_count,
            event_count=event_count,
            trace_contract=trace_contract,
            available_at=coordinates.available_at,
        )

    def __post_init__(self) -> None:
        for name in (
            "trace_ref_id",
            "program_id",
            "commit_sha",
            "tree_sha",
            "cell_id",
            "task_id",
            "format",
            "trace_contract",
        ):
            _bounded_text(getattr(self, name), name, limit=512, one_line=True)
        _non_negative_int(self.replicate, "replicate", maximum=1_000_000)
        if self.evidence_tier is not EvidenceTier.VISIBLE_A:
            raise ValueError("trace artifacts may contain only Visible-A evidence")
        _non_negative_int(self.byte_count, "byte_count", maximum=1_000_000_000)
        if self.byte_count == 0:
            raise ValueError("trace artifact byte_count must be positive")
        _non_negative_int(self.event_count, "event_count", maximum=TRACE_MAX_EVENTS)
        if self.event_count == 0:
            raise ValueError("trace artifact event_count must be positive")
        require_aware(self.available_at, "available_at")
        expected = stable_id(
            "trace-ref",
            self.program_id,
            self.commit_sha,
            self.tree_sha,
            self.cell_id,
            self.task_id,
            self.replicate,
            self.trace_contract,
        )
        if self.trace_ref_id != expected:
            raise ValueError(
                "trace_ref_id is not stable for its occurrence coordinates"
            )


@dataclass(frozen=True)
class TraceCaptureResult:
    record: TraceCaptureRecord
    artifact_ref: TraceArtifactRef | None

    def __post_init__(self) -> None:
        if not isinstance(self.record, TraceCaptureRecord):
            raise TypeError("capture result record must be TraceCaptureRecord")
        if self.artifact_ref is None:
            if self.record.trace_ref_id is not None:
                raise ValueError("capture result is missing its artifact ref")
            return
        if not isinstance(self.artifact_ref, TraceArtifactRef):
            raise TypeError("capture result artifact_ref must be TraceArtifactRef")
        if (
            self.record.trace_ref_id != self.artifact_ref.trace_ref_id
            or self.record.program_id != self.artifact_ref.program_id
            or self.record.cell_id != self.artifact_ref.cell_id
        ):
            raise ValueError("capture result record and artifact coordinates differ")

    def as_diagnostics(self) -> Mapping[str, object]:
        artifact = self.artifact_ref
        return {
            "adapter_id": self.record.adapter_id,
            "status": self.record.status.value,
            "trace_ref_id": self.record.trace_ref_id,
            "reason": self.record.reason,
            "byte_count": None if artifact is None else artifact.byte_count,
            "event_count": None if artifact is None else artifact.event_count,
        }


@runtime_checkable
class VisibleATraceSink(Protocol):
    """Run-bound, Visible-A-only capture seam injected into Arena adapters."""

    def capture(
        self,
        coordinates: TraceCaptureCoordinates,
        trace: NormalizedTrace,
        *,
        partial_reason: str | None = None,
    ) -> TraceCaptureResult:
        ...

    def unavailable(
        self,
        coordinates: TraceCaptureCoordinates,
        *,
        status: TraceCaptureStatus,
        reason: str,
    ) -> TraceCaptureResult:
        ...


@dataclass(frozen=True)
class TraceSpanRef:
    evidence_ref: str
    trace_ref_id: str
    event_start: int
    event_end: int

    @classmethod
    def create(
        cls, *, trace_ref_id: str, event_start: int, event_end: int
    ) -> "TraceSpanRef":
        return cls(
            evidence_ref=stable_id("trace-span", trace_ref_id, event_start, event_end),
            trace_ref_id=trace_ref_id,
            event_start=event_start,
            event_end=event_end,
        )

    def __post_init__(self) -> None:
        require_text(self.trace_ref_id, "trace_ref_id")
        _non_negative_int(self.event_start, "event_start", maximum=TRACE_MAX_EVENTS)
        _non_negative_int(self.event_end, "event_end", maximum=TRACE_MAX_EVENTS)
        if self.event_end <= self.event_start:
            raise ValueError("trace span must be a non-empty half-open event range")
        expected = stable_id(
            "trace-span", self.trace_ref_id, self.event_start, self.event_end
        )
        if self.evidence_ref != expected:
            raise ValueError("trace span evidence_ref is not stable")


def _signatures(values: Sequence[str], name: str) -> tuple[str, ...]:
    result = tuple(values)
    if len(result) > TRACE_MAX_SIGNATURES:
        raise ValueError(f"{name} exceeds {TRACE_MAX_SIGNATURES} entries")
    if result != tuple(sorted(set(result))):
        raise ValueError(f"{name} must be sorted and unique")
    for value in result:
        _bounded_text(value, name, limit=TRACE_MAX_SIGNATURE_CHARS, one_line=True)
    return result


@dataclass(frozen=True)
class TraceCaseIndex:
    case_ref: str
    case_key: str
    trace_ref_id: str | None
    outcome: str
    measurement_status: str
    trusted_failure_label: str | None
    normalized_error_signatures: tuple[str, ...]
    repeated_action_signatures: tuple[str, ...]
    tool_call_count: int
    response_count: int
    final_artifact_state: str
    elapsed_seconds: float | None

    @classmethod
    def create(
        cls,
        *,
        case_key: str,
        trace_ref_id: str | None,
        outcome: str,
        measurement_status: str,
        trusted_failure_label: str | None = None,
        normalized_error_signatures: tuple[str, ...] = (),
        repeated_action_signatures: tuple[str, ...] = (),
        tool_call_count: int = 0,
        response_count: int = 0,
        final_artifact_state: str,
        elapsed_seconds: float | None = None,
    ) -> "TraceCaseIndex":
        return cls(
            case_ref=stable_id("trace-case", case_key, trace_ref_id),
            case_key=case_key,
            trace_ref_id=trace_ref_id,
            outcome=outcome,
            measurement_status=measurement_status,
            trusted_failure_label=trusted_failure_label,
            normalized_error_signatures=normalized_error_signatures,
            repeated_action_signatures=repeated_action_signatures,
            tool_call_count=tool_call_count,
            response_count=response_count,
            final_artifact_state=final_artifact_state,
            elapsed_seconds=elapsed_seconds,
        )

    def __post_init__(self) -> None:
        require_text(self.case_key, "case_key")
        if self.trace_ref_id is not None:
            require_text(self.trace_ref_id, "trace_ref_id")
        if self.case_ref != stable_id("trace-case", self.case_key, self.trace_ref_id):
            raise ValueError("case_ref is not stable")
        for name in ("outcome", "measurement_status", "final_artifact_state"):
            _bounded_text(getattr(self, name), name, limit=512, one_line=True)
        _optional_line(
            self.trusted_failure_label, "trusted_failure_label", limit=512,
        )
        object.__setattr__(
            self,
            "normalized_error_signatures",
            _signatures(
                self.normalized_error_signatures, "normalized_error_signatures"
            ),
        )
        object.__setattr__(
            self,
            "repeated_action_signatures",
            _signatures(self.repeated_action_signatures, "repeated_action_signatures"),
        )
        _non_negative_int(self.tool_call_count, "tool_call_count", maximum=1_000_000)
        _non_negative_int(self.response_count, "response_count", maximum=1_000_000)
        if self.elapsed_seconds is not None:
            if (
                isinstance(self.elapsed_seconds, bool)
                or not isinstance(self.elapsed_seconds, (int, float))
                or not math.isfinite(float(self.elapsed_seconds))
                or not 0.0
                <= float(self.elapsed_seconds)
                <= TRACE_MAX_ELAPSED_MS / 1_000
            ):
                raise ValueError("elapsed_seconds must be finite and bounded")


@dataclass(frozen=True)
class TraceEvidenceManifest:
    manifest_id: str
    program_id: str
    commit_sha: str
    tree_sha: str
    compatibility: CompatibilityKey
    evidence_tier: EvidenceTier
    task_set_id: str
    measurement_profile_id: str
    artifact_refs: tuple[TraceArtifactRef, ...]
    case_indexes: tuple[TraceCaseIndex, ...]
    available_at: datetime

    @classmethod
    def create(
        cls,
        *,
        program_id: str,
        commit_sha: str,
        tree_sha: str,
        compatibility: CompatibilityKey,
        task_set_id: str,
        measurement_profile_id: str,
        artifact_refs: tuple[TraceArtifactRef, ...],
        case_indexes: tuple[TraceCaseIndex, ...],
        available_at: datetime,
    ) -> "TraceEvidenceManifest":
        artifacts = tuple(sorted(artifact_refs, key=lambda item: item.trace_ref_id))
        cases = tuple(sorted(case_indexes, key=lambda item: item.case_ref))
        coordinates = (
            program_id,
            commit_sha,
            tree_sha,
            compatibility,
            EvidenceTier.VISIBLE_A,
            task_set_id,
            measurement_profile_id,
            available_at,
        )
        return cls(
            manifest_id=stable_id("trace-manifest", *coordinates),
            program_id=program_id,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            compatibility=compatibility,
            evidence_tier=EvidenceTier.VISIBLE_A,
            task_set_id=task_set_id,
            measurement_profile_id=measurement_profile_id,
            artifact_refs=artifacts,
            case_indexes=cases,
            available_at=available_at,
        )

    def __post_init__(self) -> None:
        for name in (
            "program_id",
            "commit_sha",
            "tree_sha",
            "task_set_id",
            "measurement_profile_id",
        ):
            _bounded_text(getattr(self, name), name, limit=512, one_line=True)
        if not isinstance(self.compatibility, CompatibilityKey):
            raise TypeError("manifest compatibility must be CompatibilityKey")
        if self.evidence_tier is not EvidenceTier.VISIBLE_A:
            raise ValueError(
                "trace evidence manifests may contain only Visible-A evidence"
            )
        require_aware(self.available_at, "available_at")
        artifacts = tuple(self.artifact_refs)
        cases = tuple(self.case_indexes)
        object.__setattr__(self, "artifact_refs", artifacts)
        object.__setattr__(self, "case_indexes", cases)
        if not artifacts or not cases:
            raise ValueError("trace evidence manifest needs artifacts and case indexes")
        if artifacts != tuple(sorted(artifacts, key=lambda item: item.trace_ref_id)):
            raise ValueError("manifest artifact refs must be canonically ordered")
        if cases != tuple(sorted(cases, key=lambda item: item.case_ref)):
            raise ValueError("manifest case indexes must be canonically ordered")
        if len(cases) > TRACE_MAX_CASE_INDEX_ENTRIES:
            raise ValueError("trace case index exceeds its entry bound")
        if any(not isinstance(item, TraceArtifactRef) for item in artifacts):
            raise TypeError("artifact_refs must contain TraceArtifactRef values")
        if any(not isinstance(item, TraceCaseIndex) for item in cases):
            raise TypeError("case_indexes must contain TraceCaseIndex values")
        trace_ids = tuple(item.trace_ref_id for item in artifacts)
        case_ids = tuple(item.case_ref for item in cases)
        if len(trace_ids) != len(set(trace_ids)):
            raise ValueError("manifest contains duplicate trace occurrences")
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("manifest contains duplicate cases")
        trace_id_set = set(trace_ids)
        if {
            item.trace_ref_id for item in cases if item.trace_ref_id is not None
        } != trace_id_set:
            raise ValueError(
                "manifest artifacts and case indexes must cover the same traces"
            )
        for artifact in artifacts:
            if (
                artifact.program_id != self.program_id
                or artifact.commit_sha != self.commit_sha
                or artifact.tree_sha != self.tree_sha
                or artifact.evidence_tier is not self.evidence_tier
                or artifact.available_at > self.available_at
            ):
                raise ValueError("manifest artifact coordinates drifted")
        if len(canonical_json(cases).encode("utf-8")) > TRACE_MAX_CASE_INDEX_BYTES:
            raise ValueError("trace case index exceeds its byte bound")
        expected = stable_id(
            "trace-manifest",
            self.program_id,
            self.commit_sha,
            self.tree_sha,
            self.compatibility,
            self.evidence_tier,
            self.task_set_id,
            self.measurement_profile_id,
            self.available_at,
        )
        if self.manifest_id != expected:
            raise ValueError("manifest_id is not stable for its coordinates")


@dataclass(frozen=True)
class SnapshotTraceBinding:
    """Immutable association between one frozen search snapshot and manifests."""

    snapshot_id: str
    manifest_ids: tuple[str, ...]

    @classmethod
    def create(
        cls, *, snapshot_id: str, manifest_ids: tuple[str, ...]
    ) -> "SnapshotTraceBinding":
        return cls(
            snapshot_id=snapshot_id,
            manifest_ids=tuple(sorted(set(manifest_ids))),
        )

    def __post_init__(self) -> None:
        require_text(self.snapshot_id, "snapshot_id")
        manifest_ids = tuple(self.manifest_ids)
        object.__setattr__(self, "manifest_ids", manifest_ids)
        if not manifest_ids or manifest_ids != tuple(sorted(set(manifest_ids))):
            raise ValueError("manifest_ids must be non-empty, sorted, and unique")
        for manifest_id in manifest_ids:
            require_text(manifest_id, "manifest_id")


class ReadUnit(str, Enum):
    EVENT = "event"
    ENTRY = "entry"
    LINE = "line"


@dataclass(frozen=True)
class ReadResult:
    ref: str
    ref_kind: str
    start: int
    end: int
    total: int
    content: str

    @property
    def unit(self) -> ReadUnit:
        """The addressing unit implied by the ref kind."""

        if self.ref_kind == "case":
            return ReadUnit.EVENT
        if self.ref_kind in {"package", "file"}:
            return ReadUnit.ENTRY
        return ReadUnit.LINE

    @property
    def truncated(self) -> bool:
        return self.end < self.total

    def __post_init__(self) -> None:
        require_text(self.ref, "read ref")
        if self.ref_kind not in {"case", "package", "file", "target", "source"}:
            raise ValueError("read ref_kind is unsupported")
        for name in ("start", "end", "total"):
            _non_negative_int(getattr(self, name), name, maximum=1_000_000_000)
        if self.start > self.end or self.end > self.total:
            raise ValueError("read result range is outside the underlying content")
        if not isinstance(self.content, str):
            raise TypeError("read result content must be text")


__all__ = [
    "NORMALIZED_TRACE_SCHEMA",
    "REDACTION_MARKER",
    "TRACE_MAX_CASE_INDEX_BYTES",
    "TRACE_MAX_CASE_INDEX_ENTRIES",
    "AdapterTraceCapability",
    "NormalizedTrace",
    "ReadResult",
    "ReadUnit",
    "SnapshotTraceBinding",
    "TraceArtifactRef",
    "TraceCaptureCoordinates",
    "TraceCaptureRecord",
    "TraceCaptureResult",
    "TraceCaptureStatus",
    "TraceCaseIndex",
    "TraceEvidenceManifest",
    "TraceEvent",
    "TraceEventKind",
    "TraceSpanRef",
    "VisibleATraceSink",
    "decode_normalized_trace",
    "normalize_observable_trace",
    "ordered_secret_values",
    "summarize_trace_actions",
]
