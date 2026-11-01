"""Ready-to-use follow-up calls over the existing evidence readers."""

from __future__ import annotations


def tool_call(name, **arguments):
    return {"tool": name, "arguments": arguments}


def read_actions(name, args, result):
    """Keep audit coordinates internal; give the model one continuation to copy."""
    shown = dict(result)
    if "error" in shown:
        return shown
    following = None
    if name == "read":
        path = shown.get("context", {}).get("path", args["path"])
        if shown.get("next_line") is not None:
            following = tool_call(
                "read", path=path, line=shown["next_line"], count=args.get("count", 120)
            )
        shown["lines"] = [shown.get("line_start"), shown.get("line_end")]
    elif name in {"list_cases", "list_sources", "search"}:
        if name == "search" and shown.get("scope") == "trace":
            from .trace_provenance import TRACE_INTERPRETATION
            shown["interpretation"] = TRACE_INTERPRETATION
        if shown.get("next_cursor") is not None:
            parameters = {**args, "cursor": shown["next_cursor"]}
            if name == "search":
                parameters["scope"] = shown["scope"]
            following = tool_call(name, **parameters)
    elif name == "read_trace":
        scope = shown.get("scope", {})
        from .trace_provenance import TRACE_INTERPRETATION
        shown["event_provenance"] = scope.get("event_provenance", [])
        shown["interpretation"] = TRACE_INTERPRETATION
        cursor = scope.get("next_cursor")
        if cursor is None:
            cursor = scope.get("next_episode_cursor")
        if cursor is not None:
            following = tool_call(name, ref=args["ref"], cursor=cursor)
        details = {}
        for item in shown.get("detail_views", []):
            if not (item.get("display_truncated") or item.get("echo_folded")):
                continue
            ordinal = item["event_ordinal"]
            if ordinal not in details or item["field"] == "keystrokes":
                details[ordinal] = tool_call(
                    "read_trace_content",
                    ref=args["ref"],
                    event_ordinal=ordinal,
                    field=item["field"],
                )
        if details:
            shown["expand"] = list(details.values())
        if shown.get("preview"):
            shown["note"] = (
                "Some text is omitted or folded. Use expand to inspect it; next moves through the execution."
            )
    elif name == "read_trace_content":
        from .trace_provenance import TRACE_INTERPRETATION
        shown["interpretation"] = TRACE_INTERPRETATION
        if shown.get("next_offset") is not None:
            following = tool_call(
                name,
                ref=args["ref"],
                event_ordinal=args["event_ordinal"],
                field=args.get("field", "content"),
                offset=shown["next_offset"],
            )
    elif name == "check_trace":
        following = shown.get("next")
    else:
        return shown
    shown["next"] = following
    # These raw coordinates remain in the durable read audit and evidence.
    for key in (
        "next_cursor",
        "next_line",
        "next_offset",
        "next_episode_cursor",
        "detail_views",
        "read_coverage",
        "coverage_note",
        "scope",
    ):
        shown.pop(key, None)
    if name == "read":
        for key in ("start", "end", "total", "line_start", "line_end"):
            shown.pop(key, None)
    context = shown.get("context")
    if isinstance(context, dict) and isinstance(context.get("case"), dict):
        case = context["case"]
        runtime = case.get("provider_calls")
        if isinstance(runtime, dict):
            compact = {"recorded_call_count": runtime.get("recorded_call_count")}
            ordinal = runtime.get("detail_event_ordinal")
            if ordinal is not None:
                compact["details"] = tool_call("read_trace_content", ref=case["citation_ref"], event_ordinal=ordinal)
            else:
                compact["note"] = "Call-level evidence unavailable; null is unknown."
            context = {**context, "case": {**case, "provider_calls": compact}}
            shown["context"] = context
    if isinstance(context, dict) and "episode_navigation" in context:
        navigation = context["episode_navigation"]
        shown["context"] = {
            **context,
            "episode_navigation": {
                "steps": [
                    {
                        "cursor": row["start"],
                        "action": row["decision_excerpt"],
                        "result_tail": row["output_excerpt"],
                    }
                    for row in navigation["episodes"]
                ],
                "omitted_steps": navigation["omitted_count"],
                **({"all_episode_cursors": navigation["all_episode_cursors"]}
                   if "all_episode_cursors" in navigation else {}),
                **({"omitted_ranges": navigation["omitted_ranges"]}
                   if navigation.get("omitted_ranges") else {}),
                "note": "Navigation only, not proof of execution. Read a step or omitted range using its first cursor.",
            },
        }
    return shown
