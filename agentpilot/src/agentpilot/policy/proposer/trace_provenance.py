"""Interpret recorded roles without guessing origins from natural-language text."""
from agentpilot.domain import TraceEventKind

TRACE_INTERPRETATION = (
    "Executor statements are claims, not an independent diagnosis. Tool results may mix "
    "terminal output and harness feedback when the record does not distinguish them. "
    "Printed 'verified' text is not a correctness verdict. No origin is inferred from wording."
)


def event_provenance(event):
    actor = (event.actor or "").lower()
    if actor in {"system", "harness"}:
        origin = "recorded_system_or_harness_message"
    elif event.kind in {TraceEventKind.MESSAGE, TraceEventKind.FINAL} and actor in {"agent", "assistant"}:
        origin = "executor_statement"
    elif event.kind is TraceEventKind.TOOL_INTENT:
        origin = "recorded_action_request"
    elif event.kind is TraceEventKind.TERMINAL:
        origin = "terminal_capture"
    elif event.kind is TraceEventKind.TOOL_RESULT:
        origin = "tool_result_mixed_or_unspecified"
    elif event.kind is TraceEventKind.MESSAGE and actor == "user":
        origin = "recorded_input"
    else:
        origin = "unspecified"
    return {"event_ordinal":event.ordinal, "origin":origin}
