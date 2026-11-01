"""Budgeted trace pages with original content preserved for stable evidence."""

from __future__ import annotations

import json
import re
from dataclasses import replace

from agentpilot.domain import ReadResult, TraceEvent, TraceEventKind, canonical_json
from agentpilot.research.models import ResearchContentUnavailable
from agentpilot.search.supplemental_evidence import (
    PROPOSER_SUPPLEMENTAL_MAX_TRACE_EVENTS,
    trace_content_preview,
)
from agentpilot.search.trace_store import TraceStore
from .trace_provenance import event_provenance


def read_trace_page(
    store: TraceStore,
    ref: str,
    offset: int,
    count: int,
    max_bytes: int,
) -> tuple[ReadResult, ReadResult, bool]:
    """Return (original, display, preview) for a nonempty progressing page.

    Whole events are preferred and each display is at most min(8192, max_bytes)
    UTF-8 bytes. Only an individually oversized event is shown as an explicit
    head/tail preview. Callers must create citable evidence from ``original``;
    the display's event coordinates are unchanged and ``end`` is the next
    cursor. ``preview`` also marks omitted content on a final event page, where
    ReadResult.truncated alone would otherwise be false.
    """
    for value, name, minimum in (
        (offset, "offset", 0),
        (count, "count", 1),
        (max_bytes, "max_bytes", 512),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"trace page {name} must be an integer >= {minimum}")
    budget = min(8192, max_bytes)
    case = store.case(ref)
    if case.trace_ref_id is None:
        raise ValueError("trace case has no readable artifact")
    total = store.artifact(case.trace_ref_id).event_count
    if offset >= total:
        raise ValueError("trace page offset is outside the available events")
    count = min(count, total - offset, PROPOSER_SUPPLEMENTAL_MAX_TRACE_EVENTS)
    original = store.read_case_events(ref, offset, count)
    if len(original.content.encode("utf-8")) <= budget:
        return original, original, False

    original = store.read_case_events(ref, offset, 1)
    if len(original.content.encode("utf-8")) > budget:
        preview, clipped = trace_content_preview(original.content, max_bytes=budget)
        return original, replace(original, content=preview), clipped

    # Event-prefix sizes are monotone. Find the largest complete prefix that
    # fits instead of dropping later events or returning an empty page.
    lower, upper = 1, count - 1
    while lower < upper:
        middle = (lower + upper + 1) // 2
        candidate = store.read_case_events(ref, offset, middle)
        if len(candidate.content.encode("utf-8")) <= budget:
            original = candidate
            lower = middle
        else:
            upper = middle - 1
    return original, original, False


def _episode_spans(events: tuple[TraceEvent, ...]) -> tuple[tuple[int, int], ...]:
    """Use the same original coordinates for navigation and episode reads."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    end = 0
    for event in events:
        if event.kind in {TraceEventKind.MESSAGE, TraceEventKind.FINAL} and (
            event.actor or ""
        ).lower() in {"agent", "assistant"}:
            if start is not None:
                spans.append((start, end))
            start, end = event.ordinal, event.ordinal + 1
        elif start is not None and event.kind in {
            TraceEventKind.TOOL_INTENT,
            TraceEventKind.TOOL_RESULT,
        }:
            end = event.ordinal + 1
    if start is not None:
        spans.append((start, end))
    return tuple(spans)


def read_trace_episode(
    store: TraceStore, ref: str, offset: int, max_bytes: int
) -> tuple[ReadResult, ReadResult, bool, dict[str, object]]:
    """Read one indexed episode suffix, preserving its terminal result.

    Display clipping previews the whole event range; it never shortens a
    command/result episode just to fit bytes. Original content alone supplies
    citable evidence. A next cursor only continues this episode's tail when
    its 64-event limit requires another page, not later episodes or snapshots.
    ``complete`` means the requested suffix reached the scope end; callers
    can compare original.start with scope.start to see a mid-episode request.
    """
    for value, name, minimum in ((offset, "offset", 0), (max_bytes, "max_bytes", 512)):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"trace episode {name} must be an integer >= {minimum}")
    case = store.case(ref)
    if case.trace_ref_id is None:
        raise ValueError("trace case has no readable artifact")
    trace = store.get(case.trace_ref_id)
    if offset >= len(trace.events):
        raise ValueError("trace episode offset is outside the available events")
    episode = next(
        (
            (start, end)
            for start, end in _episode_spans(trace.events)
            if start <= offset < end
        ),
        None,
    )
    start, end = episode if episode is not None else (offset, offset + 1)
    original = store.read_case_events(
        ref, offset, min(end - offset, PROPOSER_SUPPLEMENTAL_MAX_TRACE_EVENTS)
    )
    content, preview = trace_content_preview(
        original.content, max_bytes=min(8192, max_bytes)
    )
    display = replace(original, content=content) if preview else original
    scope = {
        "kind": "episode" if episode is not None else "event",
        "start": start,
        "end": end,
        "complete": original.end == end,
        "next_cursor": original.end if original.end < end else None,
    }
    return original, display, preview, scope


def _bash_arguments(event: TraceEvent) -> dict[str, object] | None:
    """Decode only the recorded, unambiguous bash-command argument shape."""
    if event.kind is not TraceEventKind.TOOL_INTENT or event.name != "bash_command":
        return None

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate command argument")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("non-JSON command argument")

    try:
        value = json.loads(
            event.content,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("keystrokes"), str):
        return None
    try:
        canonical_json(value).encode("utf-8")
    except (ValueError, UnicodeError):
        return None
    return value


def read_trace_content(
    store: TraceStore,
    ref: str,
    event_ordinal: int,
    offset: int,
    max_bytes: int,
    field: str = "content",
) -> tuple[ReadResult, dict[str, object]]:
    """Read a reversible character slice at one canonical case/event location.

    ``content`` is the exact captured string. ``keystrokes`` is available only
    for a validated bash-command JSON object. Evidence must use the returned
    original event, never treat a decoded field as a new trace occurrence.
    """
    for value, name, minimum in (
        (event_ordinal, "event_ordinal", 0),
        (offset, "offset", 0),
        (max_bytes, "max_bytes", 512),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"trace content {name} must be an integer >= {minimum}")
    if not isinstance(field, str) or field not in {"content", "keystrokes"}:
        raise ValueError("trace content field must be content or keystrokes")
    case = store.case(ref)
    if case.trace_ref_id is None:
        raise ValueError("trace case has no readable artifact")
    trace = store.get(case.trace_ref_id)
    if event_ordinal >= len(trace.events):
        raise ValueError("trace content event is outside the available events")
    event = trace.events[event_ordinal]
    value = event.content
    if field == "keystrokes":
        arguments = _bash_arguments(event)
        if arguments is None:
            raise ValueError("trace event has no verified JSON keystrokes field")
        value = arguments["keystrokes"]
    if offset >= len(value):
        raise ValueError("trace content offset is outside the available characters")
    original = store.read_case_events(ref, event_ordinal, 1)
    budget = min(8192, max_bytes)

    def fragment(end):
        return {
            "case_ref": ref,
            "event_ordinal": event_ordinal,
            "event_elapsed_ms": event.elapsed_ms,
            "field": field,
            "unit": "character",
            "start": offset,
            "end": end,
            "total": len(value),
            "next_offset": end if end < len(value) else None,
            "text": value[offset:end],
            "capture_partial": trace.partial,
            "provenance": event_provenance(event),
        }

    # Every next character consumes at least one JSON byte; this also keeps
    # sizing work bounded when an event is much larger than the page budget.
    lower, upper = offset, min(len(value), offset + budget)
    while lower < upper:
        middle = (lower + upper + 1) // 2
        if len(canonical_json(fragment(middle)).encode("utf-8")) <= budget:
            lower = middle
        else:
            upper = middle - 1
    if lower == offset:
        raise ResearchContentUnavailable("trace content byte budget cannot hold the next character")
    return original, fragment(lower)


def _plain_display_preview(text: str, budget: int, marker: str) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text, False
    remaining = budget - len(marker.encode("utf-8"))
    if remaining < 0:
        raise ResearchContentUnavailable("trace display budget cannot hold its omission marker")
    head_bytes = remaining // 2
    tail_bytes = remaining - head_bytes
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore") if tail_bytes else ""
    return head + marker + tail, True


def _render_trace_view(headers, bodies, events, fields, budget):
    """Allocate space across every event before applying head/tail previews."""
    full = "\n\n".join(header + body for header, body in zip(headers, bodies))
    if len(full.encode("utf-8")) <= budget:
        return full, [False] * len(events)
    available = budget - sum(len(header.encode("utf-8")) for header in headers)
    available -= 2 * (len(events) - 1)
    sizes = [len(body.encode("utf-8")) for body in bodies]
    lower, upper = 0, max(sizes)
    while lower < upper:
        middle = (lower + upper + 1) // 2
        if sum(min(size, middle) for size in sizes) <= available:
            lower = middle
        else:
            upper = middle - 1
    if lower < 128:
        display, _ = _plain_display_preview(
            full,
            budget,
            "\n[display omitted; expand canonical events with read_trace_content]\n",
        )
        return display, [True] * len(events)
    displays, truncated = [], []
    for header, body, event, field in zip(headers, bodies, events, fields):
        content, clipped = _plain_display_preview(
            body,
            lower,
            f"\n[display omitted; read_trace_content event_ordinal={event.ordinal} field={field} offset=0]\n",
        )
        displays.append(header + content)
        truncated.append(clipped)
    return "\n\n".join(displays), truncated


def _fold_exact_command(
    output: str, command: str, command_ordinal: int, event_ordinal: int
):
    """Replace one exact contiguous repetition, retaining all differing lines."""
    expected = command.splitlines(keepends=True)
    actual = output.splitlines(keepends=True)
    if not expected:
        return output, False
    # Only the terminal's newline delimiter is ignored in line comparison;
    # spaces, carriage returns, and command text are otherwise preserved.
    expected = [line.removesuffix("\n") for line in expected]
    heredoc = bool(re.search(r"<<-?\s*['\"]?\w+", expected[0]))
    prompt = re.compile(r"^root@[\w.-]+:[^\r\n]*?[#$] ?")
    for start in range(len(actual) - len(expected) + 1):
        for index, line in enumerate(expected):
            observed = actual[start + index].removesuffix("\n")
            if observed == line:
                continue
            if index == 0:
                observed = prompt.sub("", observed, count=1)
            elif heredoc and observed.startswith("> "):
                observed = observed[2:]
            if observed != line:
                break
        else:
            marker = (
                f"[exact command repetition folded: event {command_ordinal}; "
                f"read_trace_content event_ordinal={event_ordinal} field=content offset=0]\n"
            )
            if len(marker.encode("utf-8")) >= len(
                "".join(actual[start : start + len(expected)]).encode("utf-8")
            ):
                continue
            return (
                "".join(actual[:start])
                + marker
                + "".join(actual[start + len(expected) :]),
                True,
            )
    return output, False


def read_trace_episode_view(
    store: TraceStore, ref: str, offset: int, max_bytes: int
) -> tuple[ReadResult, ReadResult, bool, dict[str, object], list[dict[str, object]]]:
    """Render a recorded episode while retaining its original citable range.

    Completeness of the event range, displayed content, and trace capture are
    separate properties. A later decision cursor never claims the intervening
    snapshots or later episodes were read.
    """
    original, _old_display, _old_preview, old_scope = read_trace_episode(
        store, ref, offset, max_bytes
    )
    case = store.case(ref)
    trace = store.get(case.trace_ref_id)
    events = trace.events[original.start : original.end]
    headers, bodies, fields, commands = [], [], [], []
    for event in events:
        decision = event.kind in {TraceEventKind.MESSAGE, TraceEventKind.FINAL} and (
            event.actor or ""
        ).lower() in {"agent", "assistant"}
        label = (
            "DECISION"
            if decision
            else {
                TraceEventKind.TOOL_INTENT: "COMMAND",
                TraceEventKind.TOOL_RESULT: "RESULT",
            }.get(event.kind, event.kind.value.upper())
        )
        metadata = " ".join(
            value
            for value in (
                f"actor={event.actor}" if event.actor else "",
                f"name={event.name}" if event.name else "",
                f"elapsed_ms={event.elapsed_ms}" if event.elapsed_ms is not None else "",
            )
            if value
        )
        headers.append(f"Event {event.ordinal} | {label} {metadata}\n")
        arguments = _bash_arguments(event)
        body, field, command = event.content, "content", None
        if arguments is not None:
            command = arguments["keystrokes"]
            parameters = {
                key: value for key, value in arguments.items() if key != "keystrokes"
            }
            body = (
                "parameters: " + canonical_json(parameters) + "\n" if parameters else ""
            ) + command
            field = "keystrokes"
        elif event.kind is TraceEventKind.TOOL_INTENT and event.name == "bash_command":
            command = event.content
        bodies.append(body)
        fields.append(field)
        commands.append(command)
    budget = min(8192, max_bytes)
    _initial_display, initial_truncated = _render_trace_view(
        headers, bodies, events, fields, budget
    )
    folded = [False] * len(events)
    for index, event in enumerate(events):
        if event.kind is not TraceEventKind.TOOL_RESULT:
            continue
        for previous in range(index - 1, -1, -1):
            if commands[previous] is None or initial_truncated[previous]:
                continue
            bodies[index], changed = _fold_exact_command(
                bodies[index],
                commands[previous],
                events[previous].ordinal,
                event.ordinal,
            )
            folded[index] |= changed
    content, truncated = _render_trace_view(headers, bodies, events, fields, budget)
    display_truncated, echo_folded = any(truncated), any(folded)
    preview = display_truncated or echo_folded
    scope = {
        "kind": old_scope["kind"],
        "event_provenance": [event_provenance(event) for event in events],
        "start": old_scope["start"],
        "end": old_scope["end"],
        "event_range_complete": old_scope["complete"],
        "content_complete": not preview,
        "display_truncated": display_truncated,
        "echo_folded": echo_folded,
        "capture_partial": trace.partial,
        "next_cursor": old_scope["next_cursor"],
        "next_episode_cursor": next(
            (
                start
                for start, _end in _episode_spans(trace.events)
                if start >= old_scope["end"]
            ),
            None,
        ),
    }
    detail_views = []
    for event, field, clipped, was_folded in zip(events, fields, truncated, folded):
        if not (clipped or was_folded or field == "keystrokes"):
            continue
        for available in (
            ("content", "keystrokes") if field == "keystrokes" else ("content",)
        ):
            text = (
                event.content
                if available == "content"
                else _bash_arguments(event)["keystrokes"]
            )
            if text:
                detail_views.append(
                    {
                        "event_ordinal": event.ordinal,
                        "field": available,
                        "offset": 0,
                        "total": len(text),
                        "display_truncated": clipped,
                        "echo_folded": was_folded,
                    }
                )
    return original, replace(original, content=content), preview, scope, detail_views


def read_trace_episode_views(store: TraceStore, ref: str, offsets, max_bytes: int):
    """Render an initial batch from one validated immutable trace snapshot.

    This is local to the initial batch, not a persistent cache. Later model
    reads still use the real store and its filesystem/integrity checks.
    """
    case = store.case(ref)
    if case.trace_ref_id is None:
        raise ValueError("trace case has no readable artifact")
    trace = store.get(case.trace_ref_id)

    class Snapshot:
        def case(self, candidate):
            if candidate != ref:
                raise ValueError("case is outside this trace snapshot")
            return case

        def get(self, trace_ref):
            if trace_ref != case.trace_ref_id:
                raise ValueError("trace is outside this snapshot")
            return trace

        def read_case_events(self, candidate, start, count):
            self.case(candidate)
            if type(start) is not int or type(count) is not int or start < 0 or count < 1:
                raise ValueError("invalid snapshot event range")
            end = min(start + count, len(trace.events))
            if start >= end:
                raise ValueError("snapshot event range is outside the trace")
            return ReadResult(ref=ref, ref_kind="case", start=start, end=end,
                              total=len(trace.events), content=canonical_json(trace.events[start:end]))

    snapshot = Snapshot()
    for offset in offsets:
        yield read_trace_episode_view(snapshot, ref, offset, max_bytes)


def _index_excerpt(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    if max_bytes < 3:
        return ""
    available = max_bytes - 3
    head = encoded[: available // 2].decode("utf-8", errors="ignore")
    tail_bytes = available - available // 2
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore") if tail_bytes else ""
    return head + "…" + tail


def _index_output(text: str) -> str:
    """Remove only blank lines and recognizable terminal framing/echo prompts."""
    lines: list[str] = []
    heredoc_end: str | None = None
    for line in text.splitlines():
        if not line.strip() or line.strip() in {
            "New Terminal Output:",
            "Current terminal state:",
        }:
            continue
        if re.match(r"^[A-Za-z_][\w.-]*@[\w.-]+:[^\r\n]*[#$](?:\s|$)", line):
            delimiter = re.search(r"<<-?\s*['\"]?(\w+)", line)
            heredoc_end = delimiter.group(1) if delimiter else None
            continue
        if heredoc_end is not None and (line.startswith("> ") or line == ">"):
            if line[1:].strip() == heredoc_end:
                heredoc_end = None
            continue
        heredoc_end = None
        lines.append(line)
    return "\n".join(lines)


def trace_episode_index(
    store: TraceStore, ref: str, max_bytes: int = 2200
) -> dict[str, object]:
    """Useful action/result excerpts, with explicit routes into omitted ranges.

    These are navigation, not a diagnosis. Never rank by a presumed failure
    mechanism or shorten every excerpt to a few meaningless characters.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 512:
        raise ValueError("trace episode index needs at least 512 bytes")
    case = store.case(ref)
    if case.trace_ref_id is None:
        raise ValueError("trace case has no readable artifact")
    trace = store.get(case.trace_ref_id)
    def action(events):
        requests = []
        for event in events:
            if event.kind is not TraceEventKind.TOOL_INTENT:
                continue
            text = event.content
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    text = next((data[key] for key in ("keystrokes", "command", "cmd")
                                 if isinstance(data.get(key), str)), "")
            except (TypeError, ValueError):
                pass
            first = next((line.strip() for line in text.splitlines() if line.strip()), "")
            requests.append((event.name or "unnamed tool") + (": " + first if first else ""))
        return " | ".join(requests) if requests else "No recorded tool request; decision: " + events[0].content

    def output(events):
        text = "\n".join(_index_output(event.content) for event in events
                         if event.kind is TraceEventKind.TOOL_RESULT)
        return "\n".join([line.strip() for line in text.splitlines() if line.strip()][-2:])

    episodes = [
        {
            "start": start,
            "end": end,
            "decision_excerpt": action(trace.events[start:end]),
            "output_excerpt": output(trace.events[start:end]),
        }
        for start, end in _episode_spans(trace.events)
    ]

    result: dict[str, object] = {
        "ref": ref,
        "episodes": [],
        "total_episodes": len(episodes),
        "omitted_count": 0,
        "note": "Navigation only: tool requests and result tails. start is a read cursor; end is exclusive; … marks omitted text.",
    }
    for excerpt_bytes in (128, 96, 64):
        entries = [
            {
                **episode,
                "decision_excerpt": _index_excerpt(
                    episode["decision_excerpt"], excerpt_bytes
                ),
                "output_excerpt": _index_excerpt(
                    episode["output_excerpt"], excerpt_bytes
                ),
            }
            for episode in episodes
        ]
        result["episodes"] = entries
        if len(canonical_json(result).encode()) <= max_bytes:
            return result
    # Preserve readable excerpts and a source-order spread, not a completion-
    # biased selection. Each skipped range retains an exact first read cursor.
    all_cursors = [e["start"] for e in episodes]
    keep_coordinates = len(canonical_json(all_cursors).encode()) <= max_bytes // 3
    for keep in range(min(len(episodes) - 1, max_bytes // 96), -1, -1):
        positions = ([round(i * (len(episodes) - 1) / (keep - 1)) for i in range(keep)]
                     if keep > 1 else [0] if keep else [])
        selected = set(positions)
        omitted, start = [], None
        for index, episode in enumerate(episodes):
            if index not in selected and start is None:
                start = episode["start"]
            if start is not None and (index in selected or index == len(episodes) - 1):
                omitted.append([start, episodes[index - 1]["end"] if index in selected else episode["end"]])
                start = None
        result.update(episodes=[entries[i] for i in positions],
                      omitted_count=len(episodes) - keep, omitted_ranges=omitted)
        # All coordinates fit cheaply for ordinary traces; large traces retain
        # skipped ranges instead of violating the view's byte budget.
        if keep_coordinates:
            result["all_episode_cursors"] = all_cursors
        if len(canonical_json(result).encode()) <= max_bytes:
            return result
    raise ResearchContentUnavailable("trace episode index budget cannot hold its metadata")


__all__ = [
    "read_trace_content",
    "read_trace_episode",
    "read_trace_episode_view",
    "read_trace_page",
    "trace_episode_index",
]
