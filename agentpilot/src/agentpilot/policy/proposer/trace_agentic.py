"""Bounded read-only research and proposal over frozen Visible-A evidence.

An optional investigation phase precedes the same final direction contract.
Trusted code owns evidence, refs and budgets; model notes are never verdicts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from agentpilot.app.model_harnesses import (
    STRUCTURED_PROPOSER_CONTRACT,
    STRUCTURED_PROPOSER_EVIDENCE_ALIAS_SCHEMA,
    STRUCTURED_PROPOSER_INITIAL_REPOSITORY_ENTRIES,
    STRUCTURED_PROPOSER_MAX_RESPONSE_BYTES,
    STRUCTURED_PROPOSER_TARGET_ALIAS_SCHEMA,
    DirectionAliasCatalog,
    DirectionTargetAlias,
    ProviderProposerHarness,
    StructuredProposalParseError,
    _alias_nonce,
    _assert_aliases_absent_from_untrusted_data,
    _evidence_catalog,
    _extend_evidence_catalog,
    _scope_prompt_payload,
    build_direction_alias_catalog,
    parse_trace_aware_direction,
)
from agentpilot.app.provider_calls import ProviderCallContext
from agentpilot.domain import (
    ArmSpec,
    EvidenceTier,
    PolicyMethod,
    ReadResult,
    ReadUnit,
    ScopeKind,
    TraceCaseIndex,
    TraceEvent,
    TraceEventKind,
    TraceEvidenceManifest,
    TraceSpanRef,
    canonical_json,
    stable_id,
)
from agentpilot.providers import CompletionRequest, ProviderAttemptSpent
from agentpilot.search import (
    PROPOSER_SUPPLEMENTAL_EVIDENCE_CONTRACT,
    PROPOSER_SUPPLEMENTAL_MAX_CONTENT_BYTES,
    PROPOSER_SUPPLEMENTAL_MAX_ENVELOPE_BYTES,
    PROPOSER_SUPPLEMENTAL_MAX_ITEMS,
    PROPOSER_SUPPLEMENTAL_MAX_SOURCE_LINES,
    PROPOSER_SUPPLEMENTAL_MAX_TOTAL_CONTENT_BYTES,
    PROPOSER_SUPPLEMENTAL_MAX_TRACE_EVENTS,
    FailureDirectionCard,
    ProposerSupplementalEvidence,
    SupplementalEvidenceError,
    TraceStore,
    project_prompt_evidence,
    source_span_evidence,
    supplemental_evidence_envelope,
    trace_case_evidence,
    trace_span_evidence,
)
from agentpilot.search.supplemental_evidence import trace_content_preview

from .base import (
    RetryableProposalError,
    StructuredProposal,
    StructuredProposalRequest,
)
from .presentation import PROPOSER_PRESENTATION_CONTRACT, ProposerReferences
from .trace_pages import trace_episode_index

TRACE_TOOL_LAYER_CONTRACT = "source-read-cursor"
TRACE_EVIDENCE_SELECTION_CONTRACT = "episode-navigation-source-brief-v3"
_MAX_READ_EVENTS = 64
_MAX_READ_ENTRIES = 16
_MAX_READ_LINES = 256
_MAX_NONTERMINAL_RESPONSE_BYTES = 4_096
INITIAL_FAILURE_CASES = 2
INITIAL_PASSING_CASES = 1
INITIAL_SPANS_PER_CASE = 3
INITIAL_TRACE_BYTES = 6 * 1024
INITIAL_SOURCE_BYTES = 12_000
INITIAL_INVESTIGATION_SOURCE_BYTES = 8_000
REJECTED_CASES_PER_ATTEMPT = 6
MAX_CASE_INDEX_ENTRIES = 64
MAX_CASE_INDEX_BYTES = 8 * 1024
MAX_INITIAL_PROMPT_BYTES = 40 * 1024
MAX_SESSION_CONTEXT_BYTES = 1024 * 1024
RESERVED_FINAL_RESPONSE_BYTES = 16 * 1024
RESERVED_FRAMING_SCHEMA_BYTES = 16 * 1024
MAX_RESOLVED_SCOPE_FILES = 32
MAX_RESOLVED_SCOPE_BYTES = 512 * 1024

# Mechanical error recognition, matched case-insensitively against normalized
# event content.
_ERROR_TOKENS: tuple[str, ...] = (
    "traceback (most recent call last)",
    "assertionerror",
    "attributeerror",
    "importerror",
    "indexerror",
    "keyerror",
    "modulenotfounderror",
    "nameerror",
    "syntaxerror",
    "typeerror",
    "valueerror",
    "zerodivisionerror",
    "exception:",
    "error:",
    "fatal:",
    "command not found",
    "no such file or directory",
    "permission denied",
    "timed out",
)


def _add_supplemental_record(
    records: dict[str, ProposerSupplementalEvidence],
    record: ProposerSupplementalEvidence,
) -> None:
    previous = records.get(record.evidence_ref)
    if previous is not None and canonical_json(previous) != canonical_json(record):
        raise TraceAgenticSessionFailed("TRACE_INTEGRITY_FAILED")
    records[record.evidence_ref] = record


class TraceEvidenceUnavailable(RetryableProposalError):
    """The frozen request has no valid failure trace to ground a direction."""


class TraceAgenticSessionFailed(RetryableProposalError):
    """A bounded session ended without one grounded terminal direction."""


class DefinitionReader(Protocol):
    """Read exact lines from one immutable T-ref definition, never an arbitrary path."""

    def read_definition(
        self,
        *,
        request: StructuredProposalRequest,
        target: DirectionTargetAlias,
        offset: int,
        count: int,
    ) -> ReadResult: ...


@dataclass(frozen=True)
class TraceAgenticInputs:
    manifests: tuple[TraceEvidenceManifest, ...]
    trace_store: TraceStore
    definition_reader: DefinitionReader
    #: Manifests published by rejected candidates that the frozen failure
    #: cards name.  They are readable evidence about prior failed attempts,
    #: never part of the parent's own evidence floor.
    negative_manifests: tuple[TraceEvidenceManifest, ...] = ()
    investigation_probe: object | None = None

    def __post_init__(self) -> None:
        manifests = tuple(self.manifests)
        object.__setattr__(self, "manifests", manifests)
        negatives = tuple(self.negative_manifests)
        object.__setattr__(self, "negative_manifests", negatives)
        if not manifests:
            raise TraceEvidenceUnavailable("TRACE_EVIDENCE_UNAVAILABLE")
        if any(
            not isinstance(item, TraceEvidenceManifest)
            for item in manifests + negatives
        ):
            raise TypeError("manifests must contain TraceEvidenceManifest values")
        manifest_ids = tuple(item.manifest_id for item in manifests)
        if manifest_ids != tuple(sorted(set(manifest_ids))):
            raise ValueError("trace manifests must be sorted and unique")
        negative_ids = tuple(item.manifest_id for item in negatives)
        if negative_ids != tuple(sorted(set(negative_ids))):
            raise ValueError("negative trace manifests must be sorted and unique")
        if set(manifest_ids) & set(negative_ids):
            raise ValueError("a manifest cannot be both parent and negative evidence")
        if not isinstance(self.trace_store, TraceStore):
            raise TypeError("trace_store must be TraceStore")
        if not callable(getattr(self.definition_reader, "read_definition", None)):
            raise TypeError("definition_reader must provide read_definition()")
        if self.investigation_probe is not None and not callable(getattr(self.investigation_probe, "run", None)):
            raise TypeError("investigation_probe must provide run()")


class TraceAgenticInputProvider(Protocol):
    def get(self, request: StructuredProposalRequest) -> TraceAgenticInputs: ...


class FrozenTraceManifestProvider(Protocol):
    def get(self, *, state: Any) -> tuple[TraceEvidenceManifest, ...]: ...


class BoundTraceAgenticInputProvider:
    """Combine a frozen manifest view with run-bound immutable readers."""

    def __init__(
        self,
        *,
        manifests: FrozenTraceManifestProvider,
        trace_store: TraceStore,
        definition_reader: DefinitionReader,
        investigation_probe: object | None = None,
    ) -> None:
        if not callable(getattr(manifests, "get", None)):
            raise TypeError("manifests must provide get()")
        if not isinstance(trace_store, TraceStore):
            raise TypeError("trace_store must be TraceStore")
        if not callable(getattr(definition_reader, "read_definition", None)):
            raise TypeError("definition_reader must provide read_definition()")
        self.manifests = manifests
        self.trace_store = trace_store
        self.definition_reader = definition_reader
        self.investigation_probe = investigation_probe

    def get(self, request: StructuredProposalRequest) -> TraceAgenticInputs:
        bound = self.manifests.get(state=request.state)
        values = tuple(
            sorted(
                (
                    manifest
                    for manifest in bound
                    if manifest.program_id == request.parent_selection.parent_id
                    and manifest.commit_sha == request.parent_selection.commit_sha
                ),
                key=lambda item: item.manifest_id,
            )
        )
        rejected_ids = {
            card.trace_manifest_id
            for card in request.failure_cards
            if card.trace_manifest_id is not None
        }
        negatives = tuple(
            sorted(
                (
                    manifest
                    for manifest in bound
                    if manifest.manifest_id in rejected_ids
                ),
                key=lambda item: item.manifest_id,
            )
        )
        return TraceAgenticInputs(
            manifests=values,
            trace_store=self.trace_store,
            definition_reader=self.definition_reader,
            negative_manifests=negatives,
            investigation_probe=self.investigation_probe,
        )


@dataclass(frozen=True)
class TraceAgenticBudget:
    max_proposer_turns: int
    max_trace_reads: int
    max_repository_reads: int
    max_read_bytes: int
    max_read_calls: int | None = None

    def __post_init__(self) -> None:
        for name, lower in (
            ("max_proposer_turns", 1),
            ("max_trace_reads", 0),
            ("max_repository_reads", 0),
            ("max_read_bytes", 0),
            ("max_read_calls", 0),
        ):
            value = getattr(self, name)
            if name == "max_read_calls" and value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < lower
            ):
                raise ValueError(f"{name} must be an integer >= {lower}")


def _is_passing(case: TraceCaseIndex) -> bool:
    return case.outcome.strip().lower() in {
        "pass",
        "passed",
        "passing",
        "success",
        "successful",
    }


def _failure_stratum(case: TraceCaseIndex) -> tuple[str, ...]:
    return (
        case.measurement_status,
        case.trusted_failure_label or "",
        *(case.normalized_error_signatures[:1]),
        *(case.repeated_action_signatures[:1]),
        case.final_artifact_state,
    )


def _select_cases(
    cases: tuple[TraceCaseIndex, ...], *, selection_index: int = 0
) -> tuple[TraceCaseIndex, ...]:
    failures = tuple(
        item
        for item in cases
        if item.trace_ref_id is not None and item.measurement_status == "valid"
        and not _is_passing(item)
    )
    passing = tuple(
        item for item in cases if item.trace_ref_id is not None
        and item.measurement_status == "valid" and _is_passing(item)
    )
    if not failures and not passing:
        raise TraceEvidenceUnavailable("TRACE_EVIDENCE_UNAVAILABLE")
    if failures:
        offset = selection_index % len(failures)
        failures = failures[offset:] + failures[:offset]
    if passing:
        offset = selection_index % len(passing)
        passing = passing[offset:] + passing[:offset]
    if not failures:
        # Passing A is evidence, not a reason to prevent further improvement.
        return passing[: INITIAL_FAILURE_CASES + INITIAL_PASSING_CASES]
    selected_failures: list[TraceCaseIndex] = []
    seen_strata: set[tuple[str, ...]] = set()
    for case in failures:
        stratum = _failure_stratum(case)
        if stratum in seen_strata and len(seen_strata) < INITIAL_FAILURE_CASES:
            continue
        selected_failures.append(case)
        seen_strata.add(stratum)
        if len(selected_failures) == INITIAL_FAILURE_CASES:
            break
    if len(selected_failures) < INITIAL_FAILURE_CASES:
        for case in failures:
            if case not in selected_failures:
                selected_failures.append(case)
            if len(selected_failures) == INITIAL_FAILURE_CASES:
                break
    return tuple(selected_failures) + passing[:INITIAL_PASSING_CASES]


def _window_starts(total: int, count: int, windows: int) -> tuple[int, ...]:
    if total <= count:
        return (0,)
    candidates = (0, max(0, (total - count) // 2), total - count)
    selected = list(dict.fromkeys(candidates))
    if windows > len(selected):
        for ordinal in range(1, windows + 1):
            start = round((total - count) * ordinal / (windows + 1))
            if start not in selected:
                selected.append(start)
    return tuple(sorted(selected[:windows]))


def _recognized_error(content: str) -> bool:
    lowered = content.lower()
    return any(token in lowered for token in _ERROR_TOKENS)


def _anchor_candidates(events: tuple[TraceEvent, ...]) -> tuple[int, ...]:
    """Rank observable landmarks by how much they explain a failure.

    Ordinal order is deliberately not the ranking. A dense agent trace has an
    observable landmark every few events, so ordering by ordinal selects the
    opening of the run and never reaches the failure.
    """

    errors = [event.ordinal for event in events if _recognized_error(event.content)]
    final_ordinals = [
        event.ordinal for event in events if event.kind is TraceEventKind.FINAL
    ]
    artifact_ordinals = [
        event.ordinal for event in events if event.kind is TraceEventKind.ARTIFACT
    ]
    finalization = (
        final_ordinals[-1]
        if final_ordinals
        else artifact_ordinals[-1]
        if artifact_ordinals
        else len(events)
    )
    ordered: list[int] = []
    if errors:
        ordered.append(errors[-1])
    if final_ordinals:
        ordered.append(final_ordinals[-1])
    elif artifact_ordinals:
        ordered.append(artifact_ordinals[-1])
    preceding_results = [
        event.ordinal
        for event in events
        if event.kind is TraceEventKind.TOOL_RESULT and event.ordinal < finalization
    ]
    if preceding_results:
        ordered.append(preceding_results[-1])
    ordered.extend(reversed(errors[:-1]))
    commands = [
        event.ordinal for event in events if event.kind is TraceEventKind.TOOL_INTENT
    ]
    if commands:
        ordered.append(commands[0])
    repeated: dict[tuple[str, str], list[int]] = {}
    for event in events:
        if event.kind is TraceEventKind.TOOL_INTENT:
            action = (event.name or "", event.content)
            repeated.setdefault(action, []).append(event.ordinal)
    recurring = sorted(
        (-len(ordinals), ordinals[0], ordinals[-1])
        for ordinals in repeated.values()
        if len(ordinals) > 1
    )
    if recurring:
        _, first, last = recurring[0]
        ordered.extend((first, last))
    return tuple(dict.fromkeys(ordered))


def _anchored_window_starts(
    *, events: tuple[TraceEvent, ...], count: int, windows: int
) -> tuple[int, ...]:
    total = len(events)
    if total <= count:
        return (0,)
    ceiling = total - count
    accepted: list[int] = []
    covered: set[int] = set()
    # A window earns its share of the trace budget only by showing mostly new
    # events.  Requiring strict disjointness instead would discard a landmark
    # that sits near another one, which is exactly where failures cluster.
    minimum_fresh = max(1, count // 2)

    def admit(start: int) -> None:
        clamped = min(max(0, start), ceiling)
        span = range(clamped, clamped + count)
        if sum(1 for ordinal in span if ordinal not in covered) < minimum_fresh:
            return
        accepted.append(clamped)
        covered.update(span)

    for ordinal in _anchor_candidates(events):
        if len(accepted) == windows:
            break
        # Centering keeps the command that produced a landmark inside its window.
        admit(ordinal - count // 2)
    for start in _window_starts(total, count, windows):
        if len(accepted) == windows:
            break
        admit(start)
    # Priority order, not ordinal order: the caller spends its byte budget on
    # window 0 first, so the decisive window has to come first.
    return tuple(accepted)


def _episode_ranges(events: tuple[TraceEvent, ...]) -> tuple[tuple[int, int], ...]:
    """Select actual command/result episodes, excluding repeated terminal panes."""
    episodes: list[tuple[int, int, bool]] = []
    seen: set[tuple[str, ...]] = set()
    for end, event in enumerate(events):
        if event.kind is not TraceEventKind.TOOL_RESULT:
            continue
        start = end
        while start > 0 and events[start - 1].kind is TraceEventKind.TOOL_INTENT:
            start -= 1
        commands = events[start:end]
        if not commands or all(item.name == "mark_task_complete" for item in commands):
            continue
        signature = (*tuple(item.content for item in commands), event.content)
        if signature in seen:
            continue
        seen.add(signature)
        if (
            start > 0
            and events[start - 1].kind in {TraceEventKind.MESSAGE, TraceEventKind.FINAL}
            and events[start - 1].actor.lower() in {"agent", "assistant"}
        ):
            start -= 1
        start = max(start, end + 1 - _MAX_READ_EVENTS)
        episodes.append(
            (start, end + 1, any(len(item.content) >= 256 for item in commands))
        )
    if episodes:
        # A late substantive computation explains the current state; pure
        # completion confirmations and terminal recordings never displace it.
        return tuple(
            (start, end)
            for start, end, _long_command in sorted(
                episodes, key=lambda item: (not item[2], -item[1])
            )
        )
    # Sparse/partial traces may have no result. Preserve their actual final
    # agent action instead of substituting the opening task/system prompt.
    eligible = [
        event.ordinal
        for event in events
        if event.kind is not TraceEventKind.TERMINAL
        and event.actor.lower() not in {"system", "user"}
    ]
    return tuple(
        (ordinal, ordinal + 1)
        for ordinal in reversed(eligible[-INITIAL_SPANS_PER_CASE:])
    )


def _render_source_blocks(blocks: list[dict[str, Any]], *, file_lines: bool = False) -> str:
    """Render source without double JSON escaping or ambiguous code fences."""
    sections: list[str] = []
    for block in blocks:
        content = block["content"]
        fence = "`" * max(
            3, 1 + max((len(run) for run in re.findall(r"`+", content)), default=0)
        )
        location = f"{block['path']}:{block['first_line']}-{block['last_line']}"
        symbol = "" if block["symbol"] is None else f" ({block['symbol']})"
        cursor = "end" if block["next_cursor"] is None else str(block["next_cursor"])
        continuation = (
            "read: " + canonical_json({"path": block["path"], "line": block["first_line"]})
            if file_lines else f"range=[{block['start']},{block['end']}) next_cursor={cursor}"
        )
        sections.append(
            f"{location}{symbol}\n"
            f"scope_ref={block['ref']} evidence_ref={block['evidence_ref']} "
            f"{continuation}\n"
            f"{fence}\n{content}\n{fence}"
        )
    return "\n\n".join(sections)


def _unique_json(value: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value:
        if key in result:
            raise ValueError("JSON object contains duplicate fields")
        result[key] = item
    return result


class TraceAgenticProposer(ProviderProposerHarness):
    """The only production proposer: bounded investigation followed by submit."""

    def __init__(
        self,
        *args: Any,
        inputs: TraceAgenticInputProvider,
        budget: TraceAgenticBudget,
        source_context: bool = True,
        investigation: bool = False,
        research_strategy: str = "focused",
        guide_enabled: bool = True,
        investigation_scope: str = "all_failures",
        group_repeated_tasks: bool = True,
        same_task_pass_examples: int = 1,
        max_context_bytes: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if not callable(getattr(inputs, "get", None)):
            raise TypeError("inputs must provide get()")
        if not isinstance(budget, TraceAgenticBudget):
            raise TypeError("budget must be TraceAgenticBudget")
        self.inputs = inputs
        self.budget = budget
        if not isinstance(source_context, bool):
            raise TypeError("source_context must be boolean")
        self.source_context = source_context
        if not isinstance(investigation, bool):
            raise TypeError("investigation must be boolean")
        if investigation and budget.max_proposer_turns < 4:
            raise ValueError("investigation needs at least four turns for diagnosis and design")
        self.investigation = investigation
        if not isinstance(research_strategy, str) or research_strategy not in {"focused", "interleaved", "mechanism", "counterexample"}:
            raise ValueError("unknown proposer research_strategy")
        if not investigation and research_strategy != "focused":
            raise ValueError("research_strategy requires investigation")
        self.research_strategy = research_strategy
        if not isinstance(guide_enabled, bool):
            raise TypeError("guide_enabled must be boolean")
        self.guide_enabled = guide_enabled
        if not isinstance(investigation_scope, str) or investigation_scope not in {"all_failures", "single_case"}:
            raise ValueError("investigation_scope must be all_failures or single_case")
        self.investigation_scope = investigation_scope
        if not isinstance(group_repeated_tasks, bool):
            raise TypeError("group_repeated_tasks must be boolean")
        if type(same_task_pass_examples) is not int or same_task_pass_examples < 0:
            raise ValueError("same_task_pass_examples must be a nonnegative integer")
        self.group_repeated_tasks = group_repeated_tasks
        self.same_task_pass_examples = same_task_pass_examples
        if max_context_bytes is not None and (
            isinstance(max_context_bytes, bool)
            or not isinstance(max_context_bytes, int)
            or max_context_bytes < 16384
        ):
            raise ValueError("max_context_bytes must be an integer >= 16384")
        self.max_context_bytes = max_context_bytes

    def _eligible_inputs(
        self, request: StructuredProposalRequest
    ) -> TraceAgenticInputs:
        inputs = self.inputs.get(request)
        for manifest in inputs.manifests:
            if (
                manifest.evidence_tier is not EvidenceTier.VISIBLE_A
                or manifest.compatibility != request.state.compatibility
                or not request.state.cutoff.allows(manifest.available_at)
                or manifest.program_id != request.parent_selection.parent_id
                or manifest.commit_sha != request.parent_selection.commit_sha
                or manifest.tree_sha != request.repository_map.tree_sha
            ):
                raise TraceAgenticSessionFailed("TRACE_INTEGRITY_FAILED")
            inputs.trace_store.register_manifest(manifest)
        # A failure card that names a manifest is a frozen promise: the bound
        # snapshot must contain it, and it must be the rejected candidate's
        # own Visible-A evidence, not a parent's.
        expected = {
            card.trace_manifest_id
            for card in request.failure_cards
            if card.trace_manifest_id is not None
        }
        found = {item.manifest_id for item in inputs.negative_manifests}
        if found != expected:
            raise TraceAgenticSessionFailed("TRACE_INTEGRITY_FAILED")
        for manifest in inputs.negative_manifests:
            if (
                manifest.evidence_tier is not EvidenceTier.VISIBLE_A
                or manifest.compatibility != request.state.compatibility
                or not request.state.cutoff.allows(manifest.available_at)
            ):
                raise TraceAgenticSessionFailed("TRACE_INTEGRITY_FAILED")
            inputs.trace_store.register_manifest(manifest)
        return inputs

    @staticmethod
    def _case_entry(case: TraceCaseIndex) -> dict[str, Any]:
        return {
            "case_ref": case.case_ref,
            "case_key": case.case_key,
            "trace_available": case.trace_ref_id is not None,
            "outcome": case.outcome,
            "measurement_status": case.measurement_status,
            "trusted_failure_label": case.trusted_failure_label,
            "normalized_error_signatures": list(case.normalized_error_signatures),
            "repeated_action_signatures": list(case.repeated_action_signatures),
            "tool_call_count": case.tool_call_count,
            "response_count": case.response_count,
            "final_artifact_state": case.final_artifact_state,
            "elapsed_seconds": case.elapsed_seconds,
        }

    def _initial_bundle(
        self,
        inputs: TraceAgenticInputs,
        failure_cards: tuple[FailureDirectionCard, ...],
        *,
        selection_index: int = 0,
    ) -> tuple[dict[str, Any], frozenset[str]]:
        cases = tuple(
            sorted(
                (
                    case
                    for manifest in inputs.manifests
                    for case in manifest.case_indexes
                    if case.measurement_status == "valid"
                ),
                key=lambda item: item.case_ref,
            )
        )
        selected = _select_cases(cases, selection_index=selection_index)
        rejected_attempts = self._rejected_attempts(inputs, failure_cards)
        # The first page rotates with the request. A larger task pool must not
        # exhaust the proposer before its first model call.
        ordered = (*selected, *(case for case in cases if case not in selected))
        case_payload: list[dict[str, Any]] = []
        index_bytes = len(canonical_json(rejected_attempts).encode("utf-8")) + 2
        for case in ordered[:MAX_CASE_INDEX_ENTRIES]:
            entry = self._case_entry(case)
            size = len(canonical_json(entry).encode("utf-8")) + 1
            if index_bytes + size > MAX_CASE_INDEX_BYTES:
                break
            case_payload.append(entry)
            index_bytes += size
        visible_cases = {item["case_ref"] for item in case_payload}
        if any(case.case_ref not in visible_cases for case in selected):
            raise TraceAgenticSessionFailed("TRACE_CASE_INDEX_TOO_LARGE")
        seen: set[str] = set(visible_cases)
        spans: list[dict[str, Any]] = []
        case_budget = INITIAL_TRACE_BYTES // len(selected)
        for case in selected:
            if case.trace_ref_id is None:
                raise TraceAgenticSessionFailed("TRACE_INTEGRITY_FAILED")
            artifact = inputs.trace_store.artifact(case.trace_ref_id)
            trace = inputs.trace_store.get(artifact.trace_ref_id)
            used_bytes = 0
            for start, end in _episode_ranges(trace.events)[:INITIAL_SPANS_PER_CASE]:
                remaining = case_budget - used_bytes
                if remaining < 1024:
                    break
                result = inputs.trace_store.read_case_events(
                    case.case_ref, start, end - start
                )
                preview, clipped = trace_content_preview(
                    result.content, max_bytes=remaining
                )
                span = TraceSpanRef.create(
                    trace_ref_id=case.trace_ref_id,
                    event_start=result.start,
                    event_end=result.end,
                )
                seen.add(span.evidence_ref)
                size = len(preview.encode("utf-8"))
                used_bytes += size
                spans.append(
                    {
                        "case_ref": case.case_ref,
                        "evidence_ref": span.evidence_ref,
                        "unit": result.unit.value,
                        "start": result.start,
                        "end": result.end,
                        "total": result.total,
                        "content": preview,
                        "truncated": result.truncated or clipped,
                        "preview": clipped,
                        "read_original_cursor": result.start,
                    }
                )
        if not spans:
            raise TraceEvidenceUnavailable("TRACE_EVIDENCE_UNAVAILABLE")
        for attempt in rejected_attempts:
            for entry in attempt["cases"]:
                seen.add(entry["case_ref"])
        return (
            {
                "selection_contract": TRACE_EVIDENCE_SELECTION_CONTRACT,
                "manifest_ids": [item.manifest_id for item in inputs.manifests],
                "case_index": case_payload,
                "omitted_case_count": len(cases) - len(case_payload),
                "initial_spans": spans,
                "rejected_attempts": rejected_attempts,
                **({"episode_navigation": [
                    trace_episode_index(inputs.trace_store, case.case_ref)
                    for case in selected
                ]} if getattr(self, "investigation", False) else {}),
            },
            frozenset(seen),
        )

    def _rejected_attempts(
        self,
        inputs: TraceAgenticInputs,
        failure_cards: tuple[FailureDirectionCard, ...],
    ) -> list[dict[str, Any]]:
        """List each rejected attempt's failing cases next to its failure card.

        The cases arrive as bare index entries, not initial spans: the fixed
        initial-trace budget stays with the parent's own evidence floor, and
        the model spends its read budget only on the rejected attempts it
        judges relevant.
        """

        by_id = {
            manifest.manifest_id: manifest for manifest in inputs.negative_manifests
        }
        attempts = []
        for card in failure_cards:
            if card.trace_manifest_id is None:
                continue
            failing = tuple(
                case
                for case in sorted(
                    by_id[card.trace_manifest_id].case_indexes,
                    key=lambda item: item.case_ref,
                )
                if not _is_passing(case)
            )[:REJECTED_CASES_PER_ATTEMPT]
            attempts.append(
                {
                    "failure_card_ref": card.evidence_ref,
                    "cases": [self._case_entry(case) for case in failing],
                }
            )
        return attempts

    @staticmethod
    def _replace_evidence_refs(
        value: Any, aliases: Mapping[str, str], *, field: str | None = None
    ) -> Any:
        if isinstance(value, dict):
            return {
                key: TraceAgenticProposer._replace_evidence_refs(
                    item, aliases, field=key
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                TraceAgenticProposer._replace_evidence_refs(item, aliases, field=field)
                for item in value
            ]
        if isinstance(value, str) and field in {
            "evidence_ref",
            "evidence_refs",
            "citation_ref",
            "task_evidence_ref",
        }:
            return aliases.get(value, value)
        return value

    @staticmethod
    def _union_schema(
        *,
        direction_schema: Mapping[str, Any],
        readable_refs: tuple[str, ...],
        allow_read: bool,
    ) -> Mapping[str, Any]:
        submit = {
            "type": "object",
            "properties": {
                "kind": {"const": "submit"},
                "direction": direction_schema,
            },
            "required": ["kind", "direction"],
            "additionalProperties": False,
        }
        if not allow_read:
            return submit
        read = {
            "type": "object",
            "properties": {
                "kind": {"const": "read"},
                "ref": {"type": "string", "enum": list(readable_refs)},
                "cursor": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "description": "null starts at zero; a supplied episode index start can jump directly to that event; next_cursor continues a page",
                },
            },
            "required": ["kind", "ref", "cursor"],
            "additionalProperties": False,
        }
        return {"oneOf": [read, submit]}

    @staticmethod
    def _catalog_read(
        *,
        catalog: DirectionAliasCatalog,
        ref: str,
        offset: int,
        count: int,
        presentation: ProposerReferences | None = None,
    ) -> tuple[ReadResult, tuple[str, ...]]:
        count = min(count, _MAX_READ_ENTRIES)
        scope = catalog.scope(ref)
        if scope.target.kind is ScopeKind.PACKAGE:
            entries = catalog.package_entries(ref)
            by_ref = {row["ref"]: row for row in _scope_prompt_payload(
                catalog, frozenset(item.ref for item in entries)
            )}
            rows = tuple(by_ref[item.ref] for item in entries)
            ref_kind = "package"
        elif scope.target.kind is ScopeKind.FILE:
            targets = catalog.file_targets(ref)
            rows = tuple(
                {
                    "ref": item.ref,
                    "kind": "definition",
                    "file": item.file,
                    "definition": item.symbol,
                    "definition_kind": item.kind,
                    "lines": list(item.lines),
                    **({"signature": item.signature} if item.signature else {}),
                    **({"summary": item.summary} if item.summary else {}),
                }
                for item in targets
            )
            ref_kind = "file"
        else:
            raise ValueError("definition refs are read through the source reader")
        total = len(rows)
        end = min(offset + count, total)
        if offset < 0 or (offset >= total and not (offset == 0 and total == 0)):
            raise ValueError("catalog read range is outside the frozen projection")
        content = canonical_json(
            rows[offset:end] if presentation is None else presentation.present(rows[offset:end])
        )
        return (
            ReadResult(
                ref=ref,
                ref_kind=ref_kind,
                start=offset,
                end=end,
                total=total,
                content=content,
            ),
            tuple(item["ref"] for item in rows[offset:end]),
        )

    def _dispatch(
        self,
        *,
        request: StructuredProposalRequest,
        session_id: str,
        turn: int,
        system: str,
        prompt: str,
        response_schema: Mapping[str, Any] | None,
        read_audit: tuple[Mapping[str, Any], ...] = (),
        phase: str = "proposal",
    ) -> Any:
        try:
            return self.completion.complete(
                CompletionRequest(
                    model=self.model,
                    prompt=prompt,
                    system=system,
                    max_output_tokens=self.max_output_tokens,
                    temperature=self.temperature,
                    disable_reasoning=self.disable_reasoning,
                    reasoning_effort=self.reasoning_effort,
                    response_schema=response_schema,
                    metadata={
                        "purpose": "trace-agentic-proposer",
                        "session_id": session_id,
                        "turn": turn,
                        "phase": phase,
                        "tool_layer_contract": TRACE_TOOL_LAYER_CONTRACT,
                        "proposer_contract": STRUCTURED_PROPOSER_CONTRACT,
                        "prior_read_audit": list(read_audit),
                    },
                ),
                context=ProviderCallContext(
                    call_id=stable_id("trace-proposer-turn", session_id, turn),
                    run_id=request.state.run_id,
                    batch_id=request.state.batch_id,
                    purpose="trace-agentic-proposer",
                ),
            )
        except ProviderAttemptSpent as exc:
            raise RetryableProposalError(str(exc)) from exc

    @staticmethod
    def _read_error(code: str, *, ref: str) -> dict[str, Any]:
        if code == 'TRACE_TOOL_BUDGET_EXHAUSTED':
            code = 'READ_UNAVAILABLE'
        return {"kind": "read_error", "code": code, "ref": ref}

    def _source_bundle(
        self,
        *,
        request: StructuredProposalRequest,
        inputs: TraceAgenticInputs,
        catalog: DirectionAliasCatalog,
        records: dict[str, ProposerSupplementalEvidence],
    ) -> list[dict[str, Any]]:
        source_method = (
            "execution_source_context"
            if self.investigation and self.research_strategy != "focused"
            else "initial_source_context"
        )
        factory = getattr(inputs.definition_reader, source_method, None)
        if factory is None and source_method == "execution_source_context":
            factory = getattr(inputs.definition_reader, "initial_source_context", None)
        if not self.source_context or not callable(factory):
            return []
        source_budget = INITIAL_INVESTIGATION_SOURCE_BYTES if self.investigation else INITIAL_SOURCE_BYTES
        blocks = factory(request=request, max_bytes=source_budget)
        rendered: list[dict[str, Any]] = []
        used_bytes = 0
        for block in blocks:
            result = block.result
            scope = catalog.scope(result.ref)
            if result.unit is not ReadUnit.LINE or scope.target.path != block.path:
                raise TraceAgenticSessionFailed("SOURCE_CONTEXT_INTEGRITY_FAILED")
            if block.symbol is None:
                if scope.target.kind is not ScopeKind.FILE:
                    raise TraceAgenticSessionFailed("SOURCE_CONTEXT_INTEGRITY_FAILED")
                first_line = result.start + 1
            else:
                target = catalog.definition(result.ref)
                if target.symbol != block.symbol or target.file != block.path:
                    raise TraceAgenticSessionFailed("SOURCE_CONTEXT_INTEGRITY_FAILED")
                first_line = target.lines[0] + result.start
            used_bytes += len(result.content.encode("utf-8"))
            if used_bytes > source_budget:
                raise TraceAgenticSessionFailed("SOURCE_CONTEXT_BUDGET_EXCEEDED")
            record = source_span_evidence(
                commit_sha=request.parent_selection.commit_sha,
                path=block.path,
                symbol=block.symbol or "__file__",
                result=result,
            )
            _add_supplemental_record(records, record)
            rendered.append(
                {
                    "ref": result.ref,
                    "path": block.path,
                    "symbol": block.symbol,
                    "start": result.start,
                    "end": result.end,
                    "first_line": first_line,
                    "last_line": first_line + result.end - result.start - 1,
                    "content": result.content,
                    "truncated": result.truncated,
                    "next_cursor": result.end if result.truncated else None,
                    "evidence_ref": record.evidence_ref,
                }
            )
        return rendered

    def _navigation(self, *, inputs, request, catalog, ref, count=4, max_bytes=900):
        reader = getattr(inputs.definition_reader, "read_navigation", None)
        if not self.investigation or not callable(reader):
            return None, ()
        try:
            payload = reader(request=request, ref=ref, count=count, max_bytes=max_bytes)
        except (KeyError, ValueError):
            return None, ()
        if not isinstance(payload, dict) or len(canonical_json(payload).encode("utf-8")) > max_bytes:
            raise TraceAgenticSessionFailed("TRACE_INTEGRITY_FAILED")
        refs = []
        for item in payload.get("entries", ()):
            target = item.get("declaration_ref") or item.get("ref")
            if target is not None:
                catalog.scope(target)  # Only actual entries in the frozen repository.
                refs.append(target)
        return payload, tuple(dict.fromkeys(refs))

    @staticmethod
    def _task_evidence(request, cell_ids):
        supplied = {item.cell_id for item in request.visible_a_evidence}
        return project_prompt_evidence(
            state=request.state, selected_parent=request.parent_selection,
            fixed_evidence=request.evidence_cards, failure_history=request.failure_cards,
            visible_a_evidence=request.visible_a_evidence,
            visible_a_cell_ids=tuple(dict.fromkeys(cell for cell in cell_ids if cell in supplied)),
        )

    @staticmethod
    def _implementation_packet(
        *,
        bundle,
        evidence,
        artifacts,
        cases_by_ref,
        records,
        direction,
        investigation_note,
        mechanism_test=None,
        trace_fragments=(),
        capture_status=None,
        counterexample_review=None,
    ):
        """Send actual cited reads and tentative research to the same worker."""
        expanded = bool(trace_fragments) or capture_status is not None
        refs = list(direction.evidence_refs)
        if investigation_note is not None:
            refs.extend(investigation_note["evidence_refs"])
        refs = list(dict.fromkeys(refs))
        selected_cases = {span["case_ref"] for span in bundle["initial_spans"]}
        if investigation_note is not None or expanded:
            for ref in refs:
                record = records.get(ref)
                case_ref = (
                    record.payload.get("case_ref", ref) if record is not None else ref
                )
                if case_ref in cases_by_ref:
                    selected_cases.add(case_ref)
        packet = {
            "case_facts_and_excerpts": {
                "case_index": [
                    item
                    for item in bundle["case_index"]
                    if item["case_ref"] in selected_cases
                ],
                "initial_spans": [] if expanded else bundle["initial_spans"],
            },
            "visible_a_tasks": [
                {
                    key: value
                    for key, value in item.as_prompt_payload().items()
                    if key != "execution_excerpt"
                }
                for item in evidence.visible_a_evidence
                if item.cell_id
                in {
                    artifacts[cases_by_ref[ref].trace_ref_id].cell_id
                    for ref in selected_cases
                }
            ],
        }
        if counterexample_review is not None:
            packet["counterexample_review"] = counterexample_review
        if mechanism_test is not None:
            packet["mechanism_test"] = mechanism_test
            packet[
                "mechanism_test_authority"
            ] = "Proposer predictions, not a passed behavioral test. Compare with the actual parent and changed code."
        if investigation_note is None and not expanded:
            return packet
        if investigation_note is not None:
            packet["investigation"] = investigation_note
            packet[
                "research_note_authority"
            ] = "Unverified model hypotheses; validate input interpretation and behavior."
        packet["cited_observations"] = []
        packet["omitted_cited_refs"] = []
        if expanded:
            statuses = {} if capture_status is None else capture_status
            if not isinstance(statuses, Mapping):
                raise TraceAgenticSessionFailed("TRACE_INTEGRITY_FAILED")
            case_rows = packet["case_facts_and_excerpts"]["case_index"]
            present = {item["case_ref"] for item in case_rows}
            case_rows = [
                *case_rows,
                *(
                    TraceAgenticProposer._case_entry(cases_by_ref[ref])
                    for ref in sorted(selected_cases - present)
                ),
            ]
            for row in case_rows:
                status = statuses.get(row["case_ref"])
                if status is not None and not isinstance(status, bool):
                    raise TraceAgenticSessionFailed("TRACE_INTEGRITY_FAILED")
            packet["case_facts_and_excerpts"]["case_index"] = [
                {**row, "capture_partial": statuses.get(row["case_ref"])}
                for row in case_rows
            ]
            # Only the controller's completed content reads enter this input.
            # Canonical span payloads can contain head/tail previews; do not
            # attempt to reconstruct unread middle text from those previews.
            windows = []
            seen_windows = {}
            for ref in refs:
                record = records.get(ref)
                if record is None or record.kind != "trace_span":
                    continue
                for fragment in trace_fragments:
                    if fragment.get("evidence_ref") != ref:
                        continue
                    case_ref = fragment.get("case_ref")
                    case = cases_by_ref.get(case_ref)
                    ordinal = fragment.get("event_ordinal")
                    field = fragment.get("field")
                    start, end, total = (
                        fragment.get(key) for key in ("start", "end", "total")
                    )
                    text = fragment.get("text")
                    if (
                        case is None
                        or case_ref != record.payload.get("case_ref")
                        or case.trace_ref_id != record.payload.get("trace_ref_id")
                        or isinstance(ordinal, bool)
                        or not isinstance(ordinal, int)
                        or not record.payload["start"]
                        <= ordinal
                        < record.payload["end"]
                        or not isinstance(field, str)
                        or field not in {"content", "keystrokes"}
                        or any(
                            isinstance(value, bool) or not isinstance(value, int)
                            for value in (start, end, total)
                        )
                        or not 0 <= start < end <= total
                        or not isinstance(text, str)
                        or len(text) != end - start
                        or fragment.get("origin", "verbatim_trace_field_fragment")
                        != "verbatim_trace_field_fragment"
                    ):
                        raise TraceAgenticSessionFailed("TRACE_INTEGRITY_FAILED")
                    identity = {
                        "case_ref": case_ref,
                        "event_ordinal": ordinal,
                        "field": field,
                        "start": start,
                        "end": end,
                        "total": total,
                        "evidence_ref": ref,
                    }
                    key = canonical_json(identity)
                    if key in seen_windows:
                        if seen_windows[key] != text:
                            raise TraceAgenticSessionFailed("TRACE_INTEGRITY_FAILED")
                        continue
                    seen_windows[key] = text
                    windows.append(
                        (
                            identity,
                            {
                                **identity,
                                "text": text,
                                "origin": "verbatim_trace_field_fragment",
                            },
                        )
                    )
            packet["trace_fragments"] = []
            # Reserve every omitted identity before admitting optional content.
            # This prevents a full packet from silently losing a window or task
            # merely because the omission metadata itself needed more space.
            packet["omitted_trace_fragments"] = [
                identity for identity, _window in windows
            ]
            initial = bundle["initial_spans"]
            initial_ids = [
                {key: span[key] for key in ("case_ref", "evidence_ref", "start", "end")}
                for span in initial
            ]
            packet["omitted_initial_spans"] = list(initial_ids)
            if len(canonical_json(packet).encode("utf-8")) > 32_768:
                raise TraceAgenticSessionFailed(
                    "IMPLEMENTATION_EVIDENCE_BUDGET_EXHAUSTED"
                )

            def admit(destination, omissions, identity, payload):
                position = omissions.index(identity)
                omissions.pop(position)
                destination.append(payload)
                if len(canonical_json(packet).encode("utf-8")) > 31_000:
                    destination.pop()
                    omissions.insert(position, identity)

            for identity, window in windows:
                admit(
                    packet["trace_fragments"],
                    packet["omitted_trace_fragments"],
                    identity,
                    window,
                )
            for identity, span in zip(initial_ids, initial):
                admit(
                    packet["case_facts_and_excerpts"]["initial_spans"],
                    packet["omitted_initial_spans"],
                    identity,
                    span,
                )
        # Final direction citations take priority. Research citations are useful
        # context but cannot replace or silently truncate a required task.
        for ref in refs:
            record = records.get(ref)
            if record is None:
                continue  # Task evidence is already in the bounded task packet.
            payload = record.as_metadata()
            case = cases_by_ref.get(payload.get("case_ref", ref))
            if case is not None and case.trace_ref_id in artifacts:
                payload["task_id"] = artifacts[case.trace_ref_id].task_id
                payload["cell_id"] = artifacts[case.trace_ref_id].cell_id
            packet["cited_observations"].append(payload)
            if len(canonical_json(packet).encode("utf-8")) > 31_000:
                packet["cited_observations"].pop()
                packet["omitted_cited_refs"].append(ref)
        if len(canonical_json(packet).encode("utf-8")) > 32_768:
            raise TraceAgenticSessionFailed("IMPLEMENTATION_EVIDENCE_BUDGET_EXHAUSTED")
        return packet

    def propose(self, request: StructuredProposalRequest) -> StructuredProposal:
        if self.investigation:
            from .native_research import propose_native
            return propose_native(self, request)
        inputs = self._eligible_inputs(request)
        rotation = (
            int(
                stable_id(
                    "case-rotation", request.state.run_id, request.decision_round_id
                ).rsplit("-", 1)[-1],
                16,
            )
            + request.candidate_ordinal
        )
        bundle, trace_seen = self._initial_bundle(
            inputs, request.failure_cards, selection_index=rotation
        )
        cases_by_ref: dict[str, TraceCaseIndex] = {}
        for manifest in (*inputs.manifests, *inputs.negative_manifests):
            for case in manifest.case_indexes:
                previous = cases_by_ref.get(case.case_ref)
                if previous is not None and canonical_json(previous) != canonical_json(
                    case
                ):
                    raise TraceAgenticSessionFailed("TRACE_INTEGRITY_FAILED")
                cases_by_ref[case.case_ref] = case
        supplemental_records: dict[str, ProposerSupplementalEvidence] = {}
        for case in cases_by_ref.values():
            _add_supplemental_record(supplemental_records, trace_case_evidence(case))
        for item in bundle["initial_spans"]:
            case = cases_by_ref[item["case_ref"]]
            record = trace_span_evidence(
                case,
                inputs.trace_store.read_case_events(
                    case.case_ref, item["start"], item["end"] - item["start"]
                ),
            )
            if record.evidence_ref != item["evidence_ref"]:
                raise TraceAgenticSessionFailed("TRACE_INTEGRITY_FAILED")
            _add_supplemental_record(supplemental_records, record)
        evidence = self._prompt_evidence(request)
        alias_catalog = build_direction_alias_catalog(request)
        source_bundle = self._source_bundle(
            request=request,
            inputs=inputs,
            catalog=alias_catalog,
            records=supplemental_records,
        )
        issued_repository_refs = set(
            alias_catalog.initial_repository_refs(request.repository_map.editable_roots)
        )
        issued_repository_refs.update(item["ref"] for item in source_bundle)
        issued_scope_refs = {
            ref for ref in issued_repository_refs if alias_catalog.scope(ref).selectable
        }
        seen: set[str] = set(evidence.evidence_refs) | set(trace_seen)
        seen.update(item["evidence_ref"] for item in source_bundle)
        evidence_catalog = _evidence_catalog(frozenset(seen), request.request_id)
        _assert_aliases_absent_from_untrusted_data(
            request=request,
            evidence=evidence,
            alias_catalog=alias_catalog,
            evidence_catalog=evidence_catalog,
        )
        aliases = {item.canonical_ref: item.ref for item in evidence_catalog}
        # A trace occurrence and its task statement use different identities.
        # Join them through trusted artifact coordinates, never by guessing from
        # mathematical text or from an opaque case-ref suffix.
        artifacts = {
            artifact.trace_ref_id: artifact
            for manifest in (*inputs.manifests, *inputs.negative_manifests)
            for artifact in manifest.artifact_refs
        }
        entries = [*bundle["case_index"], *bundle["initial_spans"]]
        entries.extend(case for attempt in bundle["rejected_attempts"] for case in attempt["cases"])
        for entry in entries:
            case = cases_by_ref[entry["case_ref"]]
            artifact = artifacts.get(case.trace_ref_id)
            if artifact is not None:
                entry["task_id"] = artifact.task_id
                if artifact.cell_id in evidence.evidence_refs:
                    entry["task_evidence_ref"] = artifact.cell_id
        for case in bundle["case_index"]:
            case["citation_ref"] = case["case_ref"]
        for attempt in bundle["rejected_attempts"]:
            for case in attempt["cases"]:
                case["citation_ref"] = case["case_ref"]
        references = ProposerReferences(_alias_nonce(request.request_id))
        rendered_bundle = references.present(self._replace_evidence_refs(bundle, aliases))
        base_prompt = self._prompt(
            request,
            evidence,
            alias_catalog,
            evidence_catalog,
            visible_scope_refs=frozenset(issued_repository_refs),
            trace_evidence_present=True,
        )
        base_prompt += (
            "\n\n# Persistent agent source (read-only parent snapshot)\n\n"
            + _render_source_blocks(references.present(self._replace_evidence_refs(source_bundle, aliases)))
            + "\n\nThe source above implements the persistent agent. Scripts and "
            "commands inside task traces are outputs of that agent on one task, "
            "not functions that necessarily exist in this repository. Improve the "
            "agent behavior that generates and executes those scripts. Source "
            "blocks are an entry point, not a restriction on other allowed edits. "
            "Shown templates exist in this parent; their runtime selection has "
            "not been established by the source packet."
            "\n\n# Frozen trace evidence floor\n\n"
            + canonical_json(rendered_bundle)
            + "\n\nReturn exactly one JSON object: either "
            '{"kind":"read","ref":...,"cursor":null} '
            "(use next_cursor to continue; files return source, packages list entries) "
            'or {"kind":"submit","direction":{...}}.'
        )
        if len(base_prompt.encode("utf-8")) > MAX_INITIAL_PROMPT_BYTES:
            raise TraceAgenticSessionFailed("TRACE_TOOL_BUDGET_EXHAUSTED")

        manifest_ids = tuple(item.manifest_id for item in inputs.manifests)
        negative_manifest_ids = tuple(
            item.manifest_id for item in inputs.negative_manifests
        )
        session_id = stable_id(
            "trace-agentic-session",
            PROPOSER_PRESENTATION_CONTRACT,
            base_prompt,
            self._system_prompt(request, guide_enabled=self.guide_enabled),
            request.request_id,
            request.state.state_id,
            request.parent_selection.commit_sha,
            request.repository_map,
            manifest_ids,
            negative_manifest_ids,
            self.budget,
            self.max_context_bytes or MAX_SESSION_CONTEXT_BYTES,
            self.source_context,
            self.guide_enabled,
            source_bundle,
            request.max_scope_refs,
            request.max_new_paths,
            request.max_resolved_scope_files,
            request.max_resolved_scope_bytes,
            getattr(getattr(self.completion, "provider", None), "provider_name", None),
            self.model,
            self.max_output_tokens,
            self.temperature,
            self.disable_reasoning,
            self.reasoning_effort,
            TRACE_TOOL_LAYER_CONTRACT,
            TRACE_EVIDENCE_SELECTION_CONTRACT,
            STRUCTURED_PROPOSER_INITIAL_REPOSITORY_ENTRIES,
            INITIAL_FAILURE_CASES,
            INITIAL_PASSING_CASES,
            INITIAL_SPANS_PER_CASE,
            INITIAL_TRACE_BYTES,
            MAX_CASE_INDEX_ENTRIES,
            MAX_CASE_INDEX_BYTES,
            MAX_INITIAL_PROMPT_BYTES,
            MAX_SESSION_CONTEXT_BYTES,
            RESERVED_FINAL_RESPONSE_BYTES,
            RESERVED_FRAMING_SCHEMA_BYTES,
            STRUCTURED_PROPOSER_CONTRACT,
            STRUCTURED_PROPOSER_TARGET_ALIAS_SCHEMA,
            STRUCTURED_PROPOSER_EVIDENCE_ALIAS_SCHEMA,
            PROPOSER_SUPPLEMENTAL_EVIDENCE_CONTRACT,
            PROPOSER_SUPPLEMENTAL_MAX_ITEMS,
            PROPOSER_SUPPLEMENTAL_MAX_CONTENT_BYTES,
            PROPOSER_SUPPLEMENTAL_MAX_TOTAL_CONTENT_BYTES,
            PROPOSER_SUPPLEMENTAL_MAX_ENVELOPE_BYTES,
            PROPOSER_SUPPLEMENTAL_MAX_TRACE_EVENTS,
            PROPOSER_SUPPLEMENTAL_MAX_SOURCE_LINES,
        )
        transcript: list[dict[str, Any]] = []
        read_audit: list[dict[str, Any]] = []
        delivered_ranges: dict[tuple[str, str], set[int]] = {}
        for item in source_bundle:
            delivered_ranges.setdefault(("source", item["path"]), set()).update(
                range(item["first_line"] - 1, item["last_line"])
            )
        trace_reads = repository_reads = read_bytes = 0
        input_tokens = output_tokens = 0
        cost_usd = 0.0
        read_disabled = False
        correction_used = False
        case_refs = {case["case_ref"] for case in bundle["case_index"]} | {
            case["case_ref"]
            for attempt in bundle["rejected_attempts"]
            for case in attempt["cases"]
        }
        root_refs = {item.ref for item in alias_catalog.roots}
        system = (
            self._system_prompt(request, guide_enabled=self.guide_enabled)
            + " Use the supplied read branch to understand the harness and investigate "
            "the proposed behavior; submit one hypothesis when the "
            "already-seen evidence is sufficient."
        )

        for turn in range(self.budget.max_proposer_turns):
            last_turn = correction_used or turn + 2 >= self.budget.max_proposer_turns
            allow_read = (
                not read_disabled
                and not last_turn
                and (self.budget.max_read_calls is None
                     or trace_reads + repository_reads < self.budget.max_read_calls)
                and (
                    trace_reads < self.budget.max_trace_reads
                    or repository_reads < self.budget.max_repository_reads
                )
            )
            evidence_catalog = _extend_evidence_catalog(
                evidence_catalog, frozenset(seen), request.request_id
            )
            aliases = {item.canonical_ref: item.ref for item in evidence_catalog}
            conversation = (
                base_prompt
                + "\n\n# Session transcript\n\n"
                + canonical_json(references.present(self._replace_evidence_refs(transcript, aliases)))
            )
            conversation += "\n\n# Turn boundary\n\n" + canonical_json(
                {
                    "read_available": allow_read,
                    "issued_selectable_scope_refs": references.present(sorted(issued_scope_refs)),
                }
            )
            context_bytes = (
                len(system.encode("utf-8"))
                + len(conversation.encode("utf-8"))
                + RESERVED_FINAL_RESPONSE_BYTES
            )
            if context_bytes > (self.max_context_bytes or MAX_SESSION_CONTEXT_BYTES):
                raise TraceAgenticSessionFailed("TRACE_TOOL_BUDGET_EXHAUSTED")
            grounding_catalog = tuple(
                item for item in evidence_catalog if item.canonical_ref in seen
            )
            direction_schema = self._response_schema(
                alias_catalog,
                grounding_catalog,
                max_scope_refs=request.max_scope_refs,
                max_new_paths=request.max_new_paths,
                arm_spec=request.arm_spec,
                allowed_scope_refs=frozenset(issued_scope_refs),
            )
            response_schema = (
                self._union_schema(
                    direction_schema=direction_schema,
                    readable_refs=tuple(
                        sorted(
                            (
                                case_refs
                                if trace_reads < self.budget.max_trace_reads
                                else set()
                            )
                            | (
                                issued_repository_refs
                                if repository_reads < self.budget.max_repository_reads
                                else set()
                            )
                        )
                    ),
                    allow_read=allow_read,
                )
                if self._supports_native_schema()
                else None
            )
            response_schema = references.present(response_schema)
            schema_bytes = (
                0
                if response_schema is None
                else len(canonical_json(response_schema).encode("utf-8"))
            )
            if (
                len(system.encode("utf-8")) + schema_bytes
                > RESERVED_FRAMING_SCHEMA_BYTES
            ):
                raise TraceAgenticSessionFailed("TRACE_TOOL_BUDGET_EXHAUSTED")
            if context_bytes + schema_bytes > (self.max_context_bytes or MAX_SESSION_CONTEXT_BYTES):
                raise TraceAgenticSessionFailed("TRACE_TOOL_BUDGET_EXHAUSTED")
            result = self._dispatch(
                request=request,
                session_id=session_id,
                turn=turn,
                system=system,
                prompt=conversation,
                response_schema=response_schema,
                read_audit=tuple(read_audit),
            )
            response = result.text
            input_tokens += result.usage.input_tokens
            output_tokens += result.usage.output_tokens
            cost_usd += result.usage.cost_usd
            if len(response.encode("utf-8")) > STRUCTURED_PROPOSER_MAX_RESPONSE_BYTES:
                raise StructuredProposalParseError(
                    "agentic proposer response exceeds the frozen byte budget"
                )
            try:
                output = references.arguments(json.loads(response, object_pairs_hook=_unique_json))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                if not correction_used:
                    correction_used = True
                    transcript.append(
                        {
                            "turn": turn,
                            "request": {
                                "kind": "invalid",
                                "invalid_response_id": stable_id(
                                    "invalid-proposer-response", response
                                ),
                            },
                            "observation": {
                                "kind": "schema_error",
                                "code": "DIRECTION_SCHEMA_INVALID",
                                "detail": "return one read or submit JSON object",
                            },
                        }
                    )
                    continue
                raise StructuredProposalParseError(
                    "agentic proposer returned invalid JSON"
                ) from exc
            if not isinstance(output, dict) or output.get("kind") not in {
                "read",
                "submit",
            }:
                if not correction_used:
                    correction_used = True
                    transcript.append(
                        {
                            "turn": turn,
                            "request": {"kind": "invalid"},
                            "observation": {
                                "kind": "schema_error",
                                "code": "DIRECTION_SCHEMA_INVALID",
                                "detail": "kind must be read or submit",
                            },
                        }
                    )
                    continue
                raise StructuredProposalParseError(
                    "agentic proposer must return one read or submit object"
                )
            if output["kind"] == "submit":
                if len(response.encode("utf-8")) > RESERVED_FINAL_RESPONSE_BYTES:
                    raise StructuredProposalParseError(
                        "terminal direction exceeds reserved_final_response_bytes"
                    )
                try:
                    if set(output) != {"kind", "direction"}:
                        raise StructuredProposalParseError(
                            "submit output has unknown or missing fields"
                        )
                    direction = parse_trace_aware_direction(
                        canonical_json(output["direction"]),
                        request=request,
                        allowed_evidence_refs=frozenset(seen),
                        allowed_scope_refs=frozenset(issued_scope_refs),
                        evidence_catalog=evidence_catalog,
                    )
                    grounding_refs = (
                        set(trace_seen)
                        | set(evidence.non_parent_evidence_refs)
                        | (seen - set(evidence.evidence_refs))
                    )
                    if not set(direction.evidence_refs) & grounding_refs:
                        raise StructuredProposalParseError("NO_GROUNDED_EVIDENCE")
                    proposal_id = stable_id("structured-proposal", session_id)
                    if request.method is PolicyMethod.MEMORY:
                        slate_id = stable_id(
                            "memory-direction-slate",
                            request.state.state_id,
                            request.decision_round_id,
                        )
                        candidate_id = stable_id(
                            "memory-direction-candidate",
                            request.request_id,
                            direction.direction_id,
                        )
                    else:
                        slate_id = stable_id(
                            "proposer-direction-slate",
                            request.state.state_id,
                            request.decision_round_id,
                        )
                        candidate_id = stable_id(
                            "proposer-direction-candidate",
                            request.request_id,
                            direction.direction_id,
                        )
                    try:
                        rank_evidence = supplemental_evidence_envelope(
                            request_id=request.request_id,
                            session_id=session_id,
                            proposal_id=proposal_id,
                            state_id=request.state.state_id,
                            parent_id=request.parent_selection.parent_id,
                            parent_commit_sha=request.parent_selection.commit_sha,
                            slate_id=slate_id,
                            candidate_id=candidate_id,
                            direction_id=direction.direction_id,
                            records=supplemental_records,
                            cited_refs=direction.evidence_refs,
                            prompt_evidence_refs=evidence.evidence_refs,
                        )
                    except SupplementalEvidenceError as exc:
                        raise StructuredProposalParseError(
                            f"supplemental evidence is invalid: {exc}"
                        ) from exc
                except StructuredProposalParseError as exc:
                    if correction_used:
                        raise
                    correction_used = True
                    transcript.append(
                        {
                            "turn": turn,
                            "request": {"kind": "invalid_submit"},
                            "observation": {
                                "kind": "schema_error",
                                "code": "DIRECTION_SCHEMA_INVALID",
                                "detail": str(exc)[:256],
                            },
                        }
                    )
                    continue
                return StructuredProposal(
                    proposal_id=proposal_id,
                    request_id=request.request_id,
                    direction=direction,
                    provider=result.provider,
                    model=result.model,
                    arm_spec=(
                        request.arm_spec
                        if request.arm_spec is not None
                        else (
                            ArmSpec(failure_mechanism=direction.failure_mechanism)
                            if request.method is PolicyMethod.UCB
                            else None
                        )
                    ),
                    metadata={
                        "session_id": session_id,
                        "manifest_ids": manifest_ids,
                        "negative_manifest_ids": negative_manifest_ids,
                        "turns": turn + 1,
                        "trace_reads": trace_reads,
                        "repository_reads": repository_reads,
                        "read_bytes": read_bytes,
                        "read_audit": list(read_audit),
                        "initial_source_blocks": len(source_bundle),
                        "initial_source_bytes": sum(
                            len(item["content"].encode("utf-8"))
                            for item in source_bundle
                        ),
                        "selected_case_refs": list(
                            dict.fromkeys(
                                item["case_ref"] for item in bundle["initial_spans"]
                            )
                        ),
                        "implementation_evidence": {
                            "case_facts_and_excerpts": {
                                "case_index": [
                                    item for item in bundle["case_index"]
                                    if item["case_ref"] in {
                                        span["case_ref"] for span in bundle["initial_spans"]
                                    }
                                ],
                                "initial_spans": bundle["initial_spans"],
                            },
                            "visible_a_tasks": [
                                {key: value for key, value in item.as_prompt_payload().items()
                                 if key != "execution_excerpt"}
                                for item in evidence.visible_a_evidence
                                if item.cell_id in {
                                    artifacts[cases_by_ref[span["case_ref"]].trace_ref_id].cell_id
                                    for span in bundle["initial_spans"]
                                }
                            ],
                        },
                        "supplemental_evidence": rank_evidence,
                        "provider_request_id": result.usage.provider_request_id,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cost_usd": cost_usd,
                    },
                )

            if not allow_read:
                raise TraceAgenticSessionFailed("TRACE_TOOL_BUDGET_EXHAUSTED")
            # Accept old persisted/scripted read messages as well as the new
            # cursor protocol. The model-facing schema only exposes cursors.
            if set(output) <= {"kind", "ref", "cursor"} and "ref" in output:
                ref = output["ref"]
                offset = output.get("cursor")
                offset = 0 if offset is None else offset
                count = 120
            elif set(output) == {"kind", "ref", "offset", "count"}:
                ref, offset, count = output["ref"], output["offset"], output["count"]
            else:
                raise StructuredProposalParseError(
                    "read output has unknown or missing fields"
                )
            if (
                not isinstance(ref, str)
                or isinstance(offset, bool)
                or not isinstance(offset, int)
                or offset < 0
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 1
            ):
                raise StructuredProposalParseError("read request fields are invalid")
            if "cursor" in output or "offset" not in output:
                count = 20 if ref in case_refs else 120
            observation: dict[str, Any]
            read_result: ReadResult | None = None
            evidence_ref: str | None = None
            issued_candidates: tuple[str, ...] = ()
            accepted_size = 0
            accepted_new_ref = False
            accepted_issued_refs: tuple[str, ...] = ()
            accepted_supplemental: ProposerSupplementalEvidence | None = None
            source_path: str | None = None
            source_symbol: str | None = None
            try:
                if (self.budget.max_read_calls is not None
                        and trace_reads + repository_reads >= self.budget.max_read_calls):
                    observation = self._read_error("TRACE_TOOL_BUDGET_EXHAUSTED", ref=ref)
                    read_disabled = True
                elif ref in case_refs:
                    if trace_reads >= self.budget.max_trace_reads:
                        observation = self._read_error(
                            "TRACE_TOOL_BUDGET_EXHAUSTED", ref=ref
                        )
                        read_disabled = True
                    else:
                        case = inputs.trace_store.case(ref)
                        total = inputs.trace_store.artifact(
                            case.trace_ref_id
                        ).event_count
                        count = min(count, _MAX_READ_EVENTS, max(0, total - offset))
                        read_result = inputs.trace_store.read_case_events(
                            ref, offset, count
                        )
                        evidence_ref = TraceSpanRef.create(
                            trace_ref_id=case.trace_ref_id,
                            event_start=read_result.start,
                            event_end=read_result.end,
                        ).evidence_ref
                        accepted_supplemental = trace_span_evidence(case, read_result)
                        if accepted_supplemental.evidence_ref != evidence_ref:
                            raise TraceAgenticSessionFailed("TRACE_INTEGRITY_FAILED")
                        trace_reads += 1
                        observation = {}
                elif ref in issued_repository_refs:
                    if repository_reads >= self.budget.max_repository_reads:
                        observation = self._read_error(
                            "TRACE_TOOL_BUDGET_EXHAUSTED", ref=ref
                        )
                        read_disabled = True
                    else:
                        scope = alias_catalog.scope(ref)
                        if scope.target.kind is ScopeKind.DEFINITION:
                            target = alias_catalog.definition(ref)
                            count = min(
                                count,
                                _MAX_READ_LINES,
                                max(0, target.lines[1] - target.lines[0] + 1 - offset),
                            )
                            read_result = inputs.definition_reader.read_definition(
                                request=request,
                                target=target,
                                offset=offset,
                                count=count,
                            )
                            source_path, source_symbol = target.file, target.symbol
                            if (
                                read_result.ref != ref
                                or read_result.unit is not ReadUnit.LINE
                            ):
                                raise TraceAgenticSessionFailed(
                                    "TRACE_INTEGRITY_FAILED"
                                )
                            evidence_ref = stable_id(
                                "source-span",
                                request.parent_selection.commit_sha,
                                target.file,
                                target.symbol,
                                read_result.start,
                                read_result.end,
                            )
                            accepted_supplemental = source_span_evidence(
                                commit_sha=request.parent_selection.commit_sha,
                                path=target.file,
                                symbol=target.symbol,
                                result=read_result,
                            )
                            if accepted_supplemental.evidence_ref != evidence_ref:
                                raise TraceAgenticSessionFailed(
                                    "TRACE_INTEGRITY_FAILED"
                                )
                        elif scope.target.kind is ScopeKind.FILE and callable(
                            getattr(inputs.definition_reader, "read_file", None)
                        ):
                            read_result = inputs.definition_reader.read_file(
                                request=request,
                                ref=ref,
                                path=scope.target.path,
                                offset=offset,
                                count=min(count, _MAX_READ_LINES),
                            )
                            if (
                                read_result.ref != ref
                                or read_result.unit is not ReadUnit.LINE
                            ):
                                raise TraceAgenticSessionFailed(
                                    "TRACE_INTEGRITY_FAILED"
                                )
                            source_path, source_symbol = scope.target.path, "__file__"
                            if read_result.end > read_result.start:
                                accepted_supplemental = source_span_evidence(
                                    commit_sha=request.parent_selection.commit_sha,
                                    path=source_path,
                                    symbol=source_symbol,
                                    result=read_result,
                                )
                                evidence_ref = accepted_supplemental.evidence_ref
                            issued_candidates = tuple(
                                item.ref
                                for item in alias_catalog.file_targets(ref)
                                if item.lines[0] <= read_result.end
                                and item.lines[1] > read_result.start
                            )[:16]
                        else:
                            read_result, issued_candidates = self._catalog_read(
                                catalog=alias_catalog,
                                ref=ref,
                                offset=offset,
                                count=count,
                                presentation=references,
                            )
                        repository_reads += 1
                        observation = {}
                elif ref in root_refs:
                    observation = self._read_error("REF_NOT_READABLE", ref=ref)
                else:
                    observation = self._read_error("REF_NOT_ISSUED", ref=ref)
            except (KeyError, ValueError) as exc:
                observation = {
                    **self._read_error("READ_RANGE_INVALID", ref=ref),
                    "detail": str(exc)[:256],
                }
            if read_result is not None:
                size = len(read_result.content.encode("utf-8"))
                if size > self.budget.max_read_bytes - read_bytes:
                    observation = self._read_error(
                        "TRACE_TOOL_BUDGET_EXHAUSTED", ref=ref
                    )
                    accepted_supplemental = None
                    read_disabled = True
                else:
                    read_bytes += size
                    accepted_size = size
                    if evidence_ref is not None:
                        accepted_new_ref = evidence_ref not in seen
                        seen.add(evidence_ref)
                    accepted_issued_refs = tuple(
                        item
                        for item in issued_candidates
                        if item not in issued_repository_refs
                    )
                    issued_repository_refs.update(accepted_issued_refs)
                    issued_scope_refs.update(
                        ref
                        for ref in accepted_issued_refs
                        if alias_catalog.scope(ref).selectable
                    )
                    observation = {
                        "kind": "read_result",
                        "ref": read_result.ref,
                        "ref_kind": read_result.ref_kind,
                        "unit": read_result.unit.value,
                        "start": read_result.start,
                        "end": read_result.end,
                        "total": read_result.total,
                        "content": read_result.content,
                        "truncated": read_result.truncated,
                        "next_cursor": read_result.end
                        if read_result.truncated
                        else None,
                    }
                    if source_path is not None:
                        observation["path"] = source_path
                        observation["symbol"] = source_symbol
                        observation["definitions"] = [
                            {
                                "ref": item.ref,
                                "symbol": item.symbol,
                                "lines": item.lines,
                            }
                            for item in alias_catalog.targets
                            if item.ref in issued_candidates
                        ]
                    if evidence_ref is not None:
                        observation["evidence_ref"] = evidence_ref
            if len(response.encode("utf-8")) > _MAX_NONTERMINAL_RESPONSE_BYTES:
                raise StructuredProposalParseError("read response object is too large")
            candidate_entry = {
                "turn": turn,
                "request": output,
                "observation": observation,
            }
            candidate_transcript = [*transcript, candidate_entry]
            future_catalog = _extend_evidence_catalog(
                evidence_catalog, frozenset(seen), request.request_id
            )
            future_aliases = {item.canonical_ref: item.ref for item in future_catalog}
            future_conversation = (
                base_prompt
                + "\n\n# Session transcript\n\n"
                + canonical_json(
                    references.present(self._replace_evidence_refs(candidate_transcript, future_aliases))
                )
                + "\n\n# Turn boundary\n\n"
                + canonical_json(
                    {
                        "read_available": False,
                        "issued_selectable_scope_refs": references.present(sorted(issued_scope_refs)),
                    }
                )
            )
            future_direction_schema = self._response_schema(
                alias_catalog,
                tuple(future_catalog),
                max_scope_refs=request.max_scope_refs,
                max_new_paths=request.max_new_paths,
                arm_spec=request.arm_spec,
                allowed_scope_refs=frozenset(issued_scope_refs),
            )
            future_submit_schema = references.present(self._union_schema(
                direction_schema=future_direction_schema,
                readable_refs=(),
                allow_read=False,
            ))
            future_bytes = (
                len(system.encode("utf-8"))
                + len(future_conversation.encode("utf-8"))
                + RESERVED_FINAL_RESPONSE_BYTES
                + (
                    len(canonical_json(future_submit_schema).encode("utf-8"))
                    if self._supports_native_schema()
                    else 0
                )
            )
            if future_bytes > (self.max_context_bytes or MAX_SESSION_CONTEXT_BYTES):
                if accepted_size:
                    read_bytes -= accepted_size
                if accepted_new_ref and evidence_ref is not None:
                    seen.remove(evidence_ref)
                for issued_ref in accepted_issued_refs:
                    issued_repository_refs.remove(issued_ref)
                    issued_scope_refs.discard(issued_ref)
                read_disabled = True
                candidate_entry = {
                    "turn": turn,
                    "request": output,
                    "observation": self._read_error(
                        "TRACE_TOOL_BUDGET_EXHAUSTED", ref=ref
                    ),
                }
            elif accepted_supplemental is not None:
                _add_supplemental_record(supplemental_records, accepted_supplemental)
            final_observation = candidate_entry["observation"]
            delivered = final_observation.get("kind") == "read_result"
            new_bytes = 0
            if delivered and read_result is not None:
                first_offset = read_result.start
                key = (ref, read_result.unit.value)
                if source_path is not None:
                    key = ("source", source_path)
                    if source_symbol != "__file__":
                        first_offset += alias_catalog.definition(ref).lines[0] - 1
                covered = delivered_ranges.setdefault(key, set())
                final_offset = first_offset + read_result.end - read_result.start
                pieces = read_result.content.splitlines(keepends=True)
                if read_result.unit is ReadUnit.LINE:
                    new_bytes = sum(
                        len(piece.encode("utf-8"))
                        for ordinal, piece in enumerate(pieces, start=first_offset)
                        if ordinal not in covered
                    )
                elif not set(range(first_offset, final_offset)) <= covered:
                    new_bytes = len(read_result.content.encode("utf-8"))
                covered.update(range(first_offset, final_offset))
            read_audit.append(
                {
                    "turn": turn,
                    "ref": ref,
                    "ref_kind": None if read_result is None else read_result.ref_kind,
                    "path": source_path,
                    "start": offset if read_result is None else read_result.start,
                    "end": None if read_result is None else read_result.end,
                    "result_code": "ok" if delivered else final_observation.get("code"),
                    "returned_bytes": accepted_size if delivered else 0,
                    "new_content_bytes": new_bytes,
                }
            )
            transcript.append(candidate_entry)

        raise TraceAgenticSessionFailed("DIRECTION_SCHEMA_INVALID")


__all__ = [
    "TRACE_EVIDENCE_SELECTION_CONTRACT",
    "TRACE_TOOL_LAYER_CONTRACT",
    "BoundTraceAgenticInputProvider",
    "DefinitionReader",
    "FrozenTraceManifestProvider",
    "TraceAgenticBudget",
    "TraceAgenticInputProvider",
    "TraceAgenticInputs",
    "TraceAgenticProposer",
    "TraceAgenticSessionFailed",
    "TraceEvidenceUnavailable",
]
