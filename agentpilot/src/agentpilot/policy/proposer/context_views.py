"""Compact navigation built from frozen inventories, without reading traces."""

from __future__ import annotations

import json

from agentpilot.domain import ScopeKind


def _label(value):
    return json.dumps(value, ensure_ascii=False)


def program_overview(repository, catalog, *, max_bytes=8192):
    """Use existing signatures/docstrings; never invent module responsibilities."""
    files = {
        row.target.path: row
        for row in catalog.scopes
        if row.target.kind is ScopeKind.FILE
    }
    definitions = {(row.file, row.symbol): row for row in catalog.targets}
    rows, issued = [], set()
    used = 0
    for file in repository.files:
        row = {"path": file.path, "ref": files[file.path].ref, "definitions": []}
        size = len((_label(file.path) + files[file.path].ref).encode()) + 80
        if used + size > max_bytes - 256:
            break
        used += size
        rows.append(row)
        issued.add(row["ref"])
    # Give each file a chance before expanding a long entry module.
    # Describe callable interfaces before large lists of constants/attributes.
    symbols = {
        file.path: sorted(
            file.symbols,
            key=lambda symbol: (
                symbol.kind != "function",
                symbol.kind == "attribute",
                symbol.first_line,
            ),
        )
        for file in repository.files
    }
    remaining = {row["path"]: iter(symbols[row["path"]]) for row in rows}
    while remaining:
        for row in rows:
            path = row["path"]
            if path not in remaining:
                continue
            symbol = next(remaining[path], None)
            if symbol is None:
                del remaining[path]
                continue
            target = definitions.get((path, symbol.name))
            if target is None:
                continue
            item = {
                "name": symbol.name,
                "ref": target.ref,
                "line": symbol.first_line,
                "signature": symbol.signature,
                "summary": symbol.summary,
            }
            size = len(json.dumps(item, ensure_ascii=False).encode()) + 16
            if used + size > max_bytes - 256:
                remaining.clear()
                break
            row["definitions"].append(item)
            issued.add(target.ref)
            used += size
    return {
        "files": rows,
        "omitted_files": len(repository.files) - len(rows),
        "omitted_definitions": sum(len(file.symbols) for file in repository.files)
        - sum(len(row["definitions"]) for row in rows),
    }, issued


def render_program_overview(view):
    lines = ["Program structure (source inventory; read bodies to establish behavior):"]
    for file in view["files"]:
        lines.append(f"{_label(file['path'])} — {file['ref']}")
        for definition in file["definitions"]:
            label = definition["signature"] or definition["name"]
            if definition["name"] not in label:
                label = definition["name"] + ": " + label
            lines.append(
                f"  {definition['ref']} line {definition['line']}: {label}"
                + (f" — {definition['summary']}" if definition["summary"] else "")
            )
    lines.append(
        f"Not shown: {view['omitted_files']} files, {view['omitted_definitions']} definitions. "
        "Use the package/file directory or search to continue. "
        "Signatures and docstrings are navigation, not proof of runtime behavior."
    )
    return "\n".join(lines)


def render_program_flow(view):
    """A component graph followed by grounded data/state details and read locations."""
    components = view["components"]
    ids = {row["ref"]:f"M{i+1}" for i,row in enumerate(components)}
    lines = ["Program mechanism map — current parent source",
             f"parent={view['parent_id']} commit={view['commit_sha']} tree={view['tree_sha']}",
             "Nodes are callable components; arrows are source callsites, not a timeline or observed execution.",
             "This rule-based source navigation does not infer architectural modules such as memory or planning. Role labels are heuristic hints."]
    workflow = view.get("workflow_slice")
    if workflow:
        lines += ["\nControl and feedback — source slice of " + workflow["symbol"] + " " + workflow["ref"],
                  "Read location: " + workflow["path"],
                  "Selected by nested awaited-call count. Indentation preserves source branches; ellipses mark omitted statements or shortened expressions. This is not executable code or an observed runtime path.",
                  "```text"]
        lines += [f"L{row['line']} {row['text']}" for row in workflow["rows"]]
        lines += ["```"]
        if workflow["omitted_rows"]:
            lines.append(f"Slice tail not shown: {workflow['omitted_rows']} rows. Read the definition to continue.")
    if view.get("text_anchors"):
        lines += ["\nText on displayed call/return paths (source excerpts, not resolved runtime prompts):",
                  "Read the caller and text before inferring its role. Formatted source still has unresolved substitutions."]
        for anchor in view["text_anchors"]:
            lines += [f"  {anchor['caller']} {anchor['ref']} → {anchor['path']}:{anchor['line']} ({anchor['kind']})",
                      "  " + _label(anchor["source_excerpt"])
                      + (" [excerpt truncated]" if anchor["truncated"] else ""),
                      "  " + _label(anchor["open"])]
    if view.get("omitted_text_anchors"):
        lines.append(f"Additional text anchors not shown: {view['omitted_text_anchors']}.")
    lines += ["\nCallable navigation:", "```mermaid", "flowchart TD"]
    for row in components:
        lines.append(f"  {ids[row['ref']]}[{_label(row['role_label'] + ' / ' + row['symbol'])}]")
    seen = set()
    for edge in view["calls"]:
        pair = (edge["caller_ref"], edge["declaration_ref"])
        if pair not in seen and all(ref in ids for ref in pair):
            lines.append(f"  {ids[pair[0]]} -->|L{edge['line']}| {ids[pair[1]]}")
            seen.add(pair)
    lines.append("```")
    lines.append("Entry selection: " + view["selection_basis"])
    if view.get("boundary_reads"):
        lines += ["\nCalled implementations to inspect (declared types, not verified runtime dispatch):"]
        for hint in view["boundary_reads"]:
            lines.append(f"  {hint['call']} → {hint['symbol']} at {hint['path']}:{hint['line']}"
                         + (" [read-only]" if hint["read_only"] else "")
                         + "; " + _label(hint["open"]))
        if view.get("omitted_boundary_reads"):
            lines.append(f"  Additional boundary hints not shown: {view['omitted_boundary_reads']}.")
    if view.get("omitted_entry_count"):
        lines.append(f"Entry candidates not shown: {view['omitted_entry_count']}.")
    for row in components:
        lines += [f"\n{ids[row['ref']]} {row['role_label']} — {row['symbol']} — {row['ref']} at {row['path']}:{row['line']}-{row['end_line']}",
                  "  Inputs: " + row["signature"]]
        if row["declared_purpose"]:
            lines.append("  Source docstring: " + row["declared_purpose"])
        for operation in row["operations"]:
            label = f"  L{operation['line']} " + operation["arguments"]
            if operation["binds"]:
                label += " -> " + operation["binds"]
            if operation.get("feeds"):
                label += " [used inside " + operation["feeds"] + "]"
            if operation.get("evaluates"):
                label += " [evaluates condition: " + "; ".join(operation["evaluates"]) + "]"
            if operation["under"]:
                label += " [" + "; ".join(operation["under"]) + "]"
            target = operation.get("declaration_ref")
            label += " => " + target if target else " [callee implementation unresolved in this view]"
            lines.append(label)
        for state in row["state_writes"]:
            kind = {"instance_attribute":"Instance attribute", "object_attribute":"Object attribute",
                    "local_data":"Local data"}.get(state.get("kind"), "State")
            guard = " [" + "; ".join(state["under"]) + "]" if state.get("under") else ""
            lines.append(f"  {kind} L{state['line']}: {state['assignment']}" + guard)
        for branch in row["conditions"]:
            lines.append(f"  Branch L{branch['line']}: {branch['condition']}")
        for returned in row["returns"]:
            guard = " [" + "; ".join(returned["under"]) + "]" if returned.get("under") else ""
            lines.append(f"  Return L{returned['line']}: {returned['value']}" + guard)
        for control in row.get("control_exits", ()):
            lines.append(f"  Control L{control['line']}: {control['action']} [" + "; ".join(control["under"]) + "]")
        for transfer in row["iteration_transfers"]:
            guard = " [" + "; ".join(transfer["under"]) + "]" if transfer.get("under") else ""
            lines.append(f"  Loop data transfer L{transfer['line']}: {transfer['assignment']}" + guard)
        omitted = {k:row[k] for k in ("omitted_operations","omitted_state_writes","omitted_conditions","omitted_returns","omitted_iteration_transfers", "omitted_control_exits") if row.get(k)}
        if omitted:
            lines.append("  Additional source not expanded: " + _label(omitted))
    lines += [f"\nOmitted: {view['omitted_components']} callable bodies; {view['omitted_call_edges']} resolved callsite edges from displayed callers "
              f"({view.get('unexpanded_callee_edges', 0)} target unexpanded components).",
              "Instance attributes may be reset between runs; their lifecycle requires inspecting initialization/reset code.",
              view["authority"], "Use the module read locations to inspect full branches and dependencies before relying on omitted behavior."]
    return "\n".join(lines)


def execution_directory(session, *, max_bytes=16384):
    """Expose result/address metadata only; no task statement, answer or event body."""
    groups = list(session.task_groups)
    if session.inputs.negative_manifests:
        from .task_groups import task_groups

        groups += task_groups(session.inputs.negative_manifests, session.artifacts)
    lines = [
        "Recorded executions available for inspection:",
        (
            "Choose records relevant to the program behavior you are investigating. "
            "You may improve one behavior or a subset of tasks; no shared cause is required."
        ),
        (
            "Use read_trace(ref) for the task, decision index and first step. "
            "Follow returned next calls to continue and expand calls to recover omitted text."
        ),
        (
            "Repeated outcomes are observations, not a learning sequence. Invalid measurements "
            "are not wrong answers. A passing run does not certify every intermediate claim."
        ),
    ]
    used = len("\n".join(lines).encode())
    shown = []
    for group in groups:
        counts = group["counts"]
        header = (
            f"\nTask {_label(group['task_id'])}; program {_label(group['program_id'])}; "
            f"profile {_label(group['measurement_profile_id'])}; set {_label(group['task_set_id'])}: "
            f"{counts['pass']} passed, {counts['fail']} failed, {counts['invalid']} invalid"
        )
        for index, attempt in enumerate(group["attempts"]):
            ref = attempt["case_ref"]
            case = session.cases[ref]
            artifact = session.artifacts.get(case.trace_ref_id)
            # Issue only addresses actually displayed in this directory.
            prefix = f"  repeat={attempt['replicate']} {attempt['result']} "
            suffix = (
                f"; {artifact.event_count} events"
                if artifact
                else "; trace unavailable"
            )
            if attempt["failure_label"]:
                suffix += f"; {_label(attempt['failure_label'])}"
            heading = header + "\n" if index == 0 else ""
            addition = heading + prefix + ref + suffix
            if used + len(addition.encode()) + 200 > max_bytes:
                lines.append(
                    "\nMore executions are available through list_cases(cursor=null); "
                    "the directory does not limit the eligible evidence pool."
                )
                return "\n".join(lines), shown
            session.seen.add(ref)
            address = session.render({"citation_ref": ref})["citation_ref"]
            addition = heading + prefix + address + suffix
            shown.append(ref)
            lines.append(addition)
            used += len(addition.encode()) + 1
    return "\n".join(lines), shown
