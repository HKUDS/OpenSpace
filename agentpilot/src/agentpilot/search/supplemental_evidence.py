"""Replay-stable evidence learned inside one trusted proposer session.

The ordinary :class:`PromptEvidence` projection is frozen before D2 starts.
An agentic proposer can subsequently read a trace or an immutable source span
and cite the resulting server-issued reference.  These records carry exactly
the bounded payload needed for D3 to rank such directions without treating a
direction's own untrusted citations as an evidence allowlist.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from agentpilot.domain import ReadResult, ReadUnit, TraceCaseIndex, TraceSpanRef
from agentpilot.domain.common import canonical_json, deep_freeze, require_text, stable_id

PROPOSER_SUPPLEMENTAL_EVIDENCE_CONTRACT = "bound-proposer-session-evidence"
PROPOSER_SUPPLEMENTAL_EVIDENCE_KEY = "supplemental_evidence"
PROPOSER_SUPPLEMENTAL_MAX_ITEMS = 64
PROPOSER_SUPPLEMENTAL_MAX_CONTENT_BYTES = 4_096
PROPOSER_SUPPLEMENTAL_MAX_TOTAL_CONTENT_BYTES = 16_384
PROPOSER_SUPPLEMENTAL_MAX_ENVELOPE_BYTES = 24_576
PROPOSER_SUPPLEMENTAL_MAX_TRACE_EVENTS = 64
PROPOSER_SUPPLEMENTAL_MAX_SOURCE_LINES = 256

_ENVELOPE_FIELDS = {
    "contract",
    "envelope_id",
    "request_id",
    "session_id",
    "proposal_id",
    "state_id",
    "parent_id",
    "parent_commit_sha",
    "slate_id",
    "candidate_id",
    "direction_id",
    "items",
}
_CASE_FIELDS = {
    "record_id",
    "kind",
    "evidence_ref",
    "case_key",
    "trace_ref_id",
    "outcome",
    "measurement_status",
    "trusted_failure_label",
    "normalized_error_signatures",
    "repeated_action_signatures",
    "tool_call_count",
    "response_count",
    "final_artifact_state",
    "elapsed_seconds",
}
_TRACE_SPAN_FIELDS = {
    "record_id",
    "kind",
    "evidence_ref",
    "case_ref",
    "case_key",
    "trace_ref_id",
    "unit",
    "start",
    "end",
    "total",
    "content",
    "truncated",
}
_SOURCE_SPAN_FIELDS = {
    "record_id",
    "kind",
    "evidence_ref",
    "commit_sha",
    "path",
    "symbol",
    "unit",
    "start",
    "end",
    "total",
    "content",
    "truncated",
}


class SupplementalEvidenceError(ValueError):
    """Trusted proposer evidence is malformed, unbound, or incomplete."""


_TRACE_OMISSION = "[... middle omitted; read the original trace for full content ...]"


def _head_tail_preview(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, _TRACE_OMISSION in value
    marker = "\n" + _TRACE_OMISSION + "\n"
    available = max_bytes - len(marker.encode("utf-8"))
    if available < 2:
        raise ValueError("trace preview budget cannot hold its omission marker")
    head = encoded[: available // 2].decode("utf-8", errors="ignore")
    tail = encoded[-(available - available // 2) :].decode("utf-8", errors="ignore")
    return head + marker + tail, True


def trace_content_preview(
    value: str, *, max_bytes: int = PROPOSER_SUPPLEMENTAL_MAX_CONTENT_BYTES
) -> tuple[str, bool]:
    """Bound verbatim event content fairly, preserving command/result tails.

    The event range and raw store remain unchanged. Initial displays may use a
    smaller budget, but citable trace spans always use this function's default
    budget on the original range. Re-previewing a preview is idempotent.
    """
    if not isinstance(value, str):
        raise TypeError("trace preview content must be text")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 128:
        raise ValueError("trace preview needs at least 128 bytes")
    if len(value.encode("utf-8")) <= max_bytes:
        return value, _TRACE_OMISSION in value
    try:
        events = json.loads(value)
    except (TypeError, ValueError):
        return _head_tail_preview(value, max_bytes)
    if (
        not isinstance(events, list)
        or not events
        or not all(
            isinstance(event, dict) and isinstance(event.get("content"), str)
            for event in events
        )
    ):
        return _head_tail_preview(value, max_bytes)
    metadata = canonical_json([{**event, "content": ""} for event in events])
    share = (max_bytes - len(metadata.encode("utf-8"))) // len(events)
    while share >= 128:
        preview = canonical_json(
            [
                {**event, "content": _head_tail_preview(event["content"], share)[0]}
                for event in events
            ]
        )
        if len(preview.encode("utf-8")) <= max_bytes:
            return preview, True
        # JSON escaping also consumes bytes, so reduce the per-event allowance
        # rather than cutting off the later event or its closing metadata.
        share = share * 3 // 4
    return _head_tail_preview(value, max_bytes)


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise SupplementalEvidenceError(f"{label} fields differ from the contract")


def _bounded_content(value: str) -> tuple[str, bool]:
    if not isinstance(value, str):
        raise TypeError("supplemental evidence content must be text")
    encoded = value.encode("utf-8")
    if len(encoded) <= PROPOSER_SUPPLEMENTAL_MAX_CONTENT_BYTES:
        return value, False
    clipped = encoded[:PROPOSER_SUPPLEMENTAL_MAX_CONTENT_BYTES]
    while True:
        try:
            return clipped.decode("utf-8"), True
        except UnicodeDecodeError:
            clipped = clipped[:-1]


def _safe_path(value: object) -> str:
    path = require_text(value, "source span path")  # type: ignore[arg-type]
    parsed = PurePosixPath(path)
    if (
        parsed.is_absolute()
        or parsed.as_posix() != path
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or "\\" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise SupplementalEvidenceError("source span path is not a safe POSIX path")
    return path


def _bounded_range(
    *, start: object, end: object, total: object, maximum_width: int, label: str
) -> tuple[int, int, int]:
    for value in (start, end, total):
        if isinstance(value, bool) or not isinstance(value, int):
            raise SupplementalEvidenceError(f"{label} coordinates must be integers")
    assert isinstance(start, int) and isinstance(end, int) and isinstance(total, int)
    if not 0 <= start < end <= total <= 1_000_000_000:
        raise SupplementalEvidenceError(f"{label} coordinates are outside their source")
    if end - start > maximum_width:
        raise SupplementalEvidenceError(f"{label} exceeds its coordinate bound")
    return start, end, total


@dataclass(frozen=True)
class ProposerSupplementalEvidence:
    """One validated citable record and its bounded ranker payload."""

    evidence_ref: str
    kind: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        require_text(self.evidence_ref, "supplemental evidence ref")
        if self.kind not in {"trace_case", "trace_span", "source_span"}:
            raise SupplementalEvidenceError("supplemental evidence kind is unsupported")
        object.__setattr__(self, "payload", deep_freeze(dict(self.payload)))

    def as_metadata(self) -> dict[str, Any]:
        record = {
            "kind": self.kind,
            "evidence_ref": self.evidence_ref,
            **dict(self.payload),
        }
        return {
            "record_id": stable_id("proposer-evidence-record", record),
            **record,
        }

    def as_prompt_payload(self) -> dict[str, Any]:
        payload = dict(self.payload)
        if self.kind == "trace_case":
            payload.pop("trace_ref_id")
        elif self.kind == "trace_span":
            payload.pop("trace_ref_id")
            payload.pop("case_ref")
        elif self.kind == "source_span":
            payload.pop("commit_sha")
        return {
            "evidence_ref": self.evidence_ref,
            "kind": self.kind,
            **payload,
        }


def trace_case_evidence(case: TraceCaseIndex) -> ProposerSupplementalEvidence:
    """Project one trusted case-index row with its stable-ref coordinates."""

    if not isinstance(case, TraceCaseIndex):
        raise TypeError("case must be a TraceCaseIndex")
    return ProposerSupplementalEvidence(
        evidence_ref=case.case_ref,
        kind="trace_case",
        payload={
            "case_key": case.case_key,
            "trace_ref_id": case.trace_ref_id,
            "outcome": case.outcome,
            "measurement_status": case.measurement_status,
            "trusted_failure_label": case.trusted_failure_label,
            "normalized_error_signatures": list(case.normalized_error_signatures),
            "repeated_action_signatures": list(case.repeated_action_signatures),
            "tool_call_count": case.tool_call_count,
            "response_count": case.response_count,
            "final_artifact_state": case.final_artifact_state,
            "elapsed_seconds": case.elapsed_seconds,
        },
    )


def trace_span_evidence(
    case: TraceCaseIndex, result: ReadResult
) -> ProposerSupplementalEvidence:
    """Bind a bounded trace read to both its case and stable trace occurrence."""

    if not isinstance(case, TraceCaseIndex) or not isinstance(result, ReadResult):
        raise TypeError("trace span evidence needs a typed case and read result")
    if result.ref != case.case_ref or result.unit is not ReadUnit.EVENT:
        raise ValueError("trace span read belongs to another case")
    start, end, _ = _bounded_range(
        start=result.start,
        end=result.end,
        total=result.total,
        maximum_width=PROPOSER_SUPPLEMENTAL_MAX_TRACE_EVENTS,
        label="trace span",
    )
    if case.trace_ref_id is None:
        raise ValueError("trace span case has no trace occurrence")
    ref = TraceSpanRef.create(
        trace_ref_id=case.trace_ref_id, event_start=start, event_end=end
    ).evidence_ref
    content, clipped = trace_content_preview(result.content)
    return ProposerSupplementalEvidence(
        evidence_ref=ref,
        kind="trace_span",
        payload={
            "case_ref": case.case_ref,
            "case_key": case.case_key,
            "trace_ref_id": case.trace_ref_id,
            "unit": ReadUnit.EVENT.value,
            "start": start,
            "end": end,
            "total": result.total,
            "content": content,
            "truncated": result.truncated or clipped,
        },
    )


def source_span_evidence(
    *, commit_sha: str, path: str, symbol: str, result: ReadResult
) -> ProposerSupplementalEvidence:
    """Bind a bounded immutable-source read to the selected parent commit."""

    commit = require_text(commit_sha, "source span commit")
    source_path = _safe_path(path)
    source_symbol = require_text(symbol, "source span symbol")
    if not isinstance(result, ReadResult) or result.unit is not ReadUnit.LINE:
        raise TypeError("source span evidence needs a line read result")
    start, end, _ = _bounded_range(
        start=result.start,
        end=result.end,
        total=result.total,
        maximum_width=PROPOSER_SUPPLEMENTAL_MAX_SOURCE_LINES,
        label="source span",
    )
    evidence_ref = stable_id(
        "source-span", commit, source_path, source_symbol, start, end
    )
    content, clipped = _bounded_content(result.content)
    return ProposerSupplementalEvidence(
        evidence_ref=evidence_ref,
        kind="source_span",
        payload={
            "commit_sha": commit,
            "path": source_path,
            "symbol": source_symbol,
            "unit": ReadUnit.LINE.value,
            "start": start,
            "end": end,
            "total": result.total,
            "content": content,
            "truncated": result.truncated or clipped,
        },
    )


def supplemental_evidence_envelope(
    *,
    request_id: str,
    session_id: str,
    proposal_id: str,
    state_id: str,
    parent_id: str,
    parent_commit_sha: str,
    slate_id: str,
    candidate_id: str,
    direction_id: str,
    records: Mapping[str, ProposerSupplementalEvidence],
    cited_refs: Sequence[str],
    prompt_evidence_refs: frozenset[str],
) -> dict[str, Any]:
    """Freeze only newly cited records; the base prompt is not duplicated."""

    supplemental_refs = tuple(sorted(set(cited_refs) - prompt_evidence_refs))
    if len(supplemental_refs) > PROPOSER_SUPPLEMENTAL_MAX_ITEMS:
        raise SupplementalEvidenceError("too many supplemental evidence refs")
    missing = set(supplemental_refs) - set(records)
    if missing:
        raise SupplementalEvidenceError(
            f"supplemental evidence payload is missing refs: {sorted(missing)}"
        )
    envelope_body = {
        "contract": PROPOSER_SUPPLEMENTAL_EVIDENCE_CONTRACT,
        "request_id": require_text(request_id, "request_id"),
        "session_id": require_text(session_id, "session_id"),
        "proposal_id": require_text(proposal_id, "proposal_id"),
        "state_id": require_text(state_id, "state_id"),
        "parent_id": require_text(parent_id, "parent_id"),
        "parent_commit_sha": require_text(parent_commit_sha, "parent_commit_sha"),
        "slate_id": require_text(slate_id, "slate_id"),
        "candidate_id": require_text(candidate_id, "candidate_id"),
        "direction_id": require_text(direction_id, "direction_id"),
        "items": [records[ref].as_metadata() for ref in supplemental_refs],
    }
    envelope = {
        "envelope_id": stable_id("proposer-evidence-envelope", envelope_body),
        **envelope_body,
    }
    # The decoder repeats this bound.  Checking here prevents durable proposer
    # metadata from becoming oversized in the first place.
    if (
        len(canonical_json(envelope).encode("utf-8"))
        > PROPOSER_SUPPLEMENTAL_MAX_ENVELOPE_BYTES
    ):
        raise SupplementalEvidenceError("supplemental evidence envelope is too large")
    decode_supplemental_evidence(
        {PROPOSER_SUPPLEMENTAL_EVIDENCE_KEY: envelope},
        request_id=request_id,
        session_id=session_id,
        proposal_id=proposal_id,
        state_id=state_id,
        parent_id=parent_id,
        parent_commit_sha=parent_commit_sha,
        slate_id=slate_id,
        candidate_id=candidate_id,
        direction_id=direction_id,
        required_refs=frozenset(supplemental_refs),
    )
    return envelope


def _decode_item(
    item: Mapping[str, Any], *, parent_commit_sha: str
) -> ProposerSupplementalEvidence:
    record_body = {key: value for key, value in item.items() if key != "record_id"}
    if item.get("record_id") != stable_id("proposer-evidence-record", record_body):
        raise SupplementalEvidenceError("supplemental evidence record_id is not stable")
    kind = item.get("kind")
    if kind == "trace_case":
        _exact_fields(item, _CASE_FIELDS, "trace-case evidence")
        case = TraceCaseIndex(
            case_ref=item["evidence_ref"],
            case_key=item["case_key"],
            trace_ref_id=item["trace_ref_id"],
            outcome=item["outcome"],
            measurement_status=item["measurement_status"],
            trusted_failure_label=item["trusted_failure_label"],
            normalized_error_signatures=tuple(item["normalized_error_signatures"]),
            repeated_action_signatures=tuple(item["repeated_action_signatures"]),
            tool_call_count=item["tool_call_count"],
            response_count=item["response_count"],
            final_artifact_state=item["final_artifact_state"],
            elapsed_seconds=item["elapsed_seconds"],
        )
        return trace_case_evidence(case)
    if kind == "trace_span":
        _exact_fields(item, _TRACE_SPAN_FIELDS, "trace-span evidence")
        if item["unit"] != ReadUnit.EVENT.value:
            raise SupplementalEvidenceError("trace span must use event coordinates")
        start, end, _total = _bounded_range(
            start=item["start"],
            end=item["end"],
            total=item["total"],
            maximum_width=PROPOSER_SUPPLEMENTAL_MAX_TRACE_EVENTS,
            label="trace span",
        )
        trace_ref_id = require_text(item["trace_ref_id"], "trace_ref_id")
        case_key = require_text(item["case_key"], "case_key")
        case_ref = require_text(item["case_ref"], "case_ref")
        if case_ref != stable_id("trace-case", case_key, trace_ref_id):
            raise SupplementalEvidenceError("trace span case_ref is not stable")
        span = TraceSpanRef(
            evidence_ref=item["evidence_ref"],
            trace_ref_id=trace_ref_id,
            event_start=start,
            event_end=end,
        )
        content = item["content"]
        if (
            not isinstance(content, str)
            or len(content.encode("utf-8")) > PROPOSER_SUPPLEMENTAL_MAX_CONTENT_BYTES
        ):
            raise SupplementalEvidenceError("trace span content exceeds its byte bound")
        if not isinstance(item["truncated"], bool):
            raise SupplementalEvidenceError("trace span truncated must be boolean")
        return ProposerSupplementalEvidence(
            evidence_ref=span.evidence_ref,
            kind="trace_span",
            payload={
                key: item[key]
                for key in _TRACE_SPAN_FIELDS - {"record_id", "kind", "evidence_ref"}
            },
        )
    if kind == "source_span":
        _exact_fields(item, _SOURCE_SPAN_FIELDS, "source-span evidence")
        if item["unit"] != ReadUnit.LINE.value:
            raise SupplementalEvidenceError("source span must use line coordinates")
        commit = require_text(item["commit_sha"], "source span commit")
        if commit != parent_commit_sha:
            raise SupplementalEvidenceError(
                "source span belongs to another parent commit"
            )
        path = _safe_path(item["path"])
        symbol = require_text(item["symbol"], "source span symbol")
        start, end, _total = _bounded_range(
            start=item["start"],
            end=item["end"],
            total=item["total"],
            maximum_width=PROPOSER_SUPPLEMENTAL_MAX_SOURCE_LINES,
            label="source span",
        )
        expected = stable_id("source-span", commit, path, symbol, start, end)
        if item["evidence_ref"] != expected:
            raise SupplementalEvidenceError("source span evidence_ref is not stable")
        content = item["content"]
        if (
            not isinstance(content, str)
            or len(content.encode("utf-8")) > PROPOSER_SUPPLEMENTAL_MAX_CONTENT_BYTES
        ):
            raise SupplementalEvidenceError(
                "source span content exceeds its byte bound"
            )
        if not isinstance(item["truncated"], bool):
            raise SupplementalEvidenceError("source span truncated must be boolean")
        return ProposerSupplementalEvidence(
            evidence_ref=expected,
            kind="source_span",
            payload={
                key: item[key]
                for key in _SOURCE_SPAN_FIELDS - {"record_id", "kind", "evidence_ref"}
            },
        )
    raise SupplementalEvidenceError("supplemental evidence kind is unsupported")


def decode_supplemental_evidence(
    metadata: Mapping[str, Any],
    *,
    request_id: str,
    session_id: str,
    proposal_id: str,
    state_id: str,
    parent_id: str,
    parent_commit_sha: str,
    slate_id: str,
    candidate_id: str,
    direction_id: str,
    required_refs: frozenset[str],
) -> tuple[ProposerSupplementalEvidence, ...]:
    """Validate the proposer-owned envelope against immutable D2 coordinates."""

    raw = metadata.get(PROPOSER_SUPPLEMENTAL_EVIDENCE_KEY)
    if raw is None:
        if required_refs:
            raise SupplementalEvidenceError("supplemental evidence envelope is missing")
        return ()
    if not isinstance(raw, Mapping):
        raise SupplementalEvidenceError(
            "supplemental evidence envelope must be a mapping"
        )
    _exact_fields(raw, _ENVELOPE_FIELDS, "supplemental evidence envelope")
    expected_bindings = {
        "contract": PROPOSER_SUPPLEMENTAL_EVIDENCE_CONTRACT,
        "request_id": request_id,
        "session_id": session_id,
        "proposal_id": proposal_id,
        "state_id": state_id,
        "parent_id": parent_id,
        "parent_commit_sha": parent_commit_sha,
        "slate_id": slate_id,
        "candidate_id": candidate_id,
        "direction_id": direction_id,
    }
    if any(raw[key] != value for key, value in expected_bindings.items()):
        raise SupplementalEvidenceError("supplemental evidence coordinates drifted")
    envelope_body = {key: value for key, value in raw.items() if key != "envelope_id"}
    if raw["envelope_id"] != stable_id("proposer-evidence-envelope", envelope_body):
        raise SupplementalEvidenceError(
            "supplemental evidence envelope_id is not stable"
        )
    if (
        len(canonical_json(raw).encode("utf-8"))
        > PROPOSER_SUPPLEMENTAL_MAX_ENVELOPE_BYTES
    ):
        raise SupplementalEvidenceError("supplemental evidence envelope is too large")
    items = raw["items"]
    if (
        not isinstance(items, (tuple, list))
        or len(items) > PROPOSER_SUPPLEMENTAL_MAX_ITEMS
    ):
        raise SupplementalEvidenceError("supplemental evidence items are not bounded")
    decoded: list[ProposerSupplementalEvidence] = []
    total_content_bytes = 0
    for item in items:
        if not isinstance(item, Mapping):
            raise SupplementalEvidenceError(
                "supplemental evidence item must be a mapping"
            )
        record = _decode_item(item, parent_commit_sha=parent_commit_sha)
        content = record.payload.get("content", "")
        total_content_bytes += len(content.encode("utf-8"))
        decoded.append(record)
    refs = tuple(item.evidence_ref for item in decoded)
    if refs != tuple(sorted(set(refs))):
        raise SupplementalEvidenceError(
            "supplemental evidence refs must be sorted and unique"
        )
    if set(refs) != set(required_refs):
        raise SupplementalEvidenceError(
            "supplemental evidence refs differ from cited refs"
        )
    if total_content_bytes > PROPOSER_SUPPLEMENTAL_MAX_TOTAL_CONTENT_BYTES:
        raise SupplementalEvidenceError(
            "supplemental evidence content exceeds its total bound"
        )
    return tuple(decoded)


__all__ = [
    "PROPOSER_SUPPLEMENTAL_EVIDENCE_CONTRACT",
    "PROPOSER_SUPPLEMENTAL_EVIDENCE_KEY",
    "PROPOSER_SUPPLEMENTAL_MAX_CONTENT_BYTES",
    "PROPOSER_SUPPLEMENTAL_MAX_ENVELOPE_BYTES",
    "PROPOSER_SUPPLEMENTAL_MAX_ITEMS",
    "PROPOSER_SUPPLEMENTAL_MAX_SOURCE_LINES",
    "PROPOSER_SUPPLEMENTAL_MAX_TOTAL_CONTENT_BYTES",
    "PROPOSER_SUPPLEMENTAL_MAX_TRACE_EVENTS",
    "ProposerSupplementalEvidence",
    "SupplementalEvidenceError",
    "decode_supplemental_evidence",
    "source_span_evidence",
    "supplemental_evidence_envelope",
    "trace_case_evidence",
    "trace_content_preview",
    "trace_span_evidence",
]
