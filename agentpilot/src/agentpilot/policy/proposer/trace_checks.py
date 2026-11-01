"""Inspect recorded tool requests without judging proposals or replaying a solver."""
from collections import Counter
from dataclasses import replace

from agentpilot.research.models import ResearchContentUnavailable

from agentpilot.domain import TraceEventKind, canonical_json
from agentpilot.search import trace_span_evidence
from agentpilot.search.supplemental_evidence import PROPOSER_SUPPLEMENTAL_MAX_TRACE_EVENTS


def check_trace(session, args):
    from .research_io import default_arguments

    args = default_arguments(args, ("ref", "start", "end"), {"name": None})
    if not isinstance(args["ref"], str):
        raise ValueError("ref must be a recorded execution reference")
    start, end, name = args["start"], args["end"], args["name"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise ValueError("Use event positions 0 <= start < end; end is exclusive")
    if name is not None and (not isinstance(name, str) or not 1 <= len(name) <= 128):
        raise ValueError("name is an optional exact recorded tool name")
    ref = session.references.resolve(args["ref"])
    ref = next((row.canonical_ref for row in session.evidence_catalog
                if row.ref == ref and row.canonical_ref in session.cases), ref)
    allowed = ({*session.primary_case_refs, *session.same_task_pass_refs}
               if session.phase == "diagnosis" else set(session.available_cases))
    if (ref not in allowed or session.cases[ref].trace_ref_id not in session.artifacts
            or session.trace_reads >= session.engine.budget.max_trace_reads):
        raise ResearchContentUnavailable("Trace is unavailable or its read allowance is exhausted")
    remaining = session.engine.budget.max_read_bytes - session.read_bytes
    if remaining < 512:
        raise ResearchContentUnavailable("Read byte budget is exhausted")
    trace = session.inputs.trace_store.get(session.cases[ref].trace_ref_id)
    if end > len(trace.events):
        raise ValueError(f"end exceeds the {len(trace.events)} retained events")
    requested_end = end
    end = min(end, start + PROPOSER_SUPPLEMENTAL_MAX_TRACE_EVENTS)
    following = ({"tool": "check_trace", "arguments": {**args, "start": end}}
                 if end < requested_end else None)
    events = trace.events[start:end]
    if [e.ordinal for e in events] != list(range(start, end)):
        raise ValueError("Requested interval does not have contiguous recorded positions")
    requests = [e for e in events if e.kind is TraceEventKind.TOOL_INTENT
                and (name is None or e.name == name)]
    counts = Counter(e.name for e in requests)
    shown_counts = list(counts.items())[:16]
    shown_requests = requests[:16]

    def view():
        return {
            "interval": [start, end], "name_filter": name,
            "remaining_events": requested_end - end,
            "recorded_request_count": len(requests),
            "present": True if requests else None if trace.partial else False,
            "count_is_exact": not trace.partial, "capture_partial": trace.partial,
            "counts": [{"name": k, "count": v} for k, v in shown_counts],
            "omitted_names": len(counts) - len(shown_counts),
            "requests_in_order": [{"event": e.ordinal, "name": e.name,
                "open": {"tool": "read_trace_content", "arguments": {
                    "ref": ref, "event_ordinal": e.ordinal}}} for e in shown_requests],
            "omitted_requests": len(requests) - len(shown_requests),
            "interpretation": "Recorded requests only, not proof of execution or independent verification. Partial capture gives lower bounds; absence is unknown.",
        }

    while len(canonical_json(view()).encode()) > min(8192, remaining):
        if shown_requests:
            shown_requests.pop()
        elif shown_counts:
            shown_counts.pop()
        else:
            raise ResearchContentUnavailable("Read byte budget cannot hold the fact result")
    original = session.inputs.trace_store.read_case_events(ref, start, end - start)
    display = replace(original, content=canonical_json(session.render(view())))
    scope = {"next_cursor": None, "summary_only": True,
             "checked_fields": ["kind", "name", "ordinal"], "capture_partial": trace.partial}
    result = session.record_read(
        "check_trace", ref, original, display, trace_span_evidence(session.cases[ref], original),
        session.case_context(ref), preview=True, scope=scope,
        extras={"next": following},
    )
    session.trace_reads += 1
    session.audit[-1]["checked_fields"] = scope["checked_fields"]
    return result
