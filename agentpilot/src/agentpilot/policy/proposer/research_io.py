"""Search and line reads over the same immutable, visible research inputs."""

from __future__ import annotations

import json

from agentpilot.domain import ReadResult, ScopeKind, canonical_json
from agentpilot.research.models import ResearchContentUnavailable
from agentpilot.search import source_span_evidence

from .tool_navigation import tool_call


def default_arguments(args, required, defaults):
    if (
        not isinstance(args, dict)
        or not set(required) <= set(args)
        or set(args) - (set(required) | set(defaults))
    ):
        raise ValueError("Required arguments: " + ", ".join(required))
    return {**defaults, **args}


def _allowance(session, scope):
    remaining = session.engine.budget.max_read_bytes - session.read_bytes
    if remaining < 512 or not session.shared_reads_available():
        raise ResearchContentUnavailable("Read allowance exhausted")
    if scope == "source":
        if (
            session.phase != "design"
            or session.repository_reads >= session.engine.budget.max_repository_reads
        ):
            raise ResearchContentUnavailable("Source read allowance is unavailable")
    elif session.trace_reads >= session.engine.budget.max_trace_reads:
        raise ResearchContentUnavailable("Trace read allowance is exhausted")
    return min(16384, remaining)


def list_cases(session, args):
    if session.phase != "design":
        raise ValueError("Case browsing opens with the design context")
    args = default_arguments(args, (), {"cursor": None, 'view': 'attempts'})
    if args['view'] not in {'attempts', 'tasks'}:
        raise ValueError('view must be attempts or tasks')
    if args['view'] == 'tasks':
        return list_task_requirements(session, args)
    cursor = args["cursor"]
    if cursor is None:
        cursor = 0
    if type(cursor) is not int or cursor < 0:
        raise ValueError("Invalid case cursor")
    maximum = _allowance(session, "trace")
    refs = sorted(
        ref
        for ref, case in session.cases.items()
        if session.investigation_scope == "all_failures"
        or case.measurement_status == "valid"
    )
    if cursor > len(refs):
        raise ValueError("Case cursor exceeds the eligible collection")
    rows, returned = [], []
    for ref in refs[cursor : cursor + 8]:
        case = session.cases[ref]
        artifact = session.artifacts.get(case.trace_ref_id)
        row = {
            "citation_ref": ref,
            "case_key": case.case_key,
            "task_id": artifact.task_id if artifact else None,
            "outcome": case.outcome,
            "trusted_failure_label": case.trusted_failure_label,
            "measurement_status": case.measurement_status,
            "trace_available": artifact is not None,
        }
        # Canonical addresses are longer than their rendered aliases; reserve
        # space for page metadata before issuing any new evidence addresses.
        if len(canonical_json(rows + [row]).encode()) + 256 > maximum:
            if not rows:
                raise ValueError("Remaining read bytes cannot fit a case entry")
            break
        rows.append(row)
        returned.append(ref)
    end = cursor + len(rows)
    session.seen.update(returned)
    result = session.render(
        {
            "cases": rows,
            "total": len(refs),
            "next_cursor": end if end < len(refs) else None,
        }
    )
    size = len(canonical_json(result).encode())
    assert size <= maximum
    session.available_cases.update({ref: session.cases[ref] for ref in returned})
    session.trace_reads += 1
    session.read_bytes += size
    session.audit.append(
        {
            "tool": "list_cases",
            "cursor": cursor,
            "returned_bytes": size,
            "cases": len(rows),
        }
    )
    return result


def task_requirements(session):
    # Only the supplied Visible-A statements; no answers or observed outcomes.
    cached = getattr(session, '_task_requirement_rows', None)
    if cached is not None:
        return cached
    from datetime import datetime
    from agentpilot.search.prompt_evidence import PROGRAM_FACT_KINDS
    eligible = set()
    for fact in session.request.state.facts.get('items', ()):
        if fact.get('kind') not in PROGRAM_FACT_KINDS or fact.get('evidence_tier') != 'visible_a':
            continue
        available = fact.get('available_at')
        if isinstance(available, str):
            available = datetime.fromisoformat(available.replace('Z', '+00:00'))
        if isinstance(available, datetime) and session.request.state.cutoff.allows(available):
            eligible.update(fact['payload']['cell_results'])
    tasks = {}
    for brief in session.request.visible_a_evidence:
        if brief.cell_id not in eligible:
            continue
        tasks.setdefault(brief.task_id, {'task_id': brief.task_id, 'title': brief.title,
                                        'statement': brief.statement})
    # Older packets have no separate briefs. Recover only the recorded initial
    # user input from already-authorized A traces, never later actions/results or
    # a mutable current task directory. Keep the exact source and truncation flag.
    supplied = {brief.task_id for brief in session.request.visible_a_evidence}
    inputs = getattr(session, 'inputs', None)
    for manifest in getattr(inputs, 'manifests', ()):
        if not session.request.state.cutoff.allows(manifest.available_at):
            continue
        for artifact in manifest.artifact_refs:
            if (artifact.task_id in tasks or artifact.task_id in supplied
                    or artifact.evidence_tier.value != 'visible_a'
                    or not session.request.state.cutoff.allows(artifact.available_at)):
                continue
            events = inputs.trace_store.get(artifact.trace_ref_id).events
            if not events or events[0].actor != 'user' or events[0].kind.value != 'message':
                continue
            text = events[0].content
            if not text:
                continue
            origin = 'recorded_initial_user_input'
            # This is an exact delimiter in the Harbor prompt format, not a
            # semantic guess about an arbitrary agent's later explanation.
            if artifact.format in {'harbor-agent-trajectory', 'harbor-agent-trajectory-and-pane'} and '\nTask Description:\n' in text:
                task_text = text.split('\nTask Description:\n', 1)[1]
                if '\nCurrent terminal state:\n' in task_text:
                    text = task_text.rsplit('\nCurrent terminal state:\n', 1)[0].strip()
                    origin = 'recorded_harbor_task_description'
            tasks[artifact.task_id] = {'task_id': artifact.task_id, 'title': artifact.task_id,
                'statement': text[:4000], 'statement_complete': len(text) <= 4000,
                'origin': origin, 'source_trace_ref': artifact.trace_ref_id,
                'source_commit': artifact.commit_sha, 'source_event': 0}
    rows = [tasks[key] for key in sorted(tasks)]
    session._task_requirement_rows = rows
    return rows


def list_task_requirements(session, args):
    cursor = args['cursor'] if args['cursor'] is not None else 0
    tasks = task_requirements(session)
    if type(cursor) is not int or not 0 <= cursor <= len(tasks):
        raise ValueError('Use the returned task-directory cursor')
    maximum = _allowance(session, 'trace')
    rows = []
    for task in tasks[cursor:cursor + 8]:
        if len(canonical_json(rows + [task]).encode()) + 256 > maximum:
            if not rows:
                raise ResearchContentUnavailable('Remaining read bytes cannot fit a task statement')
            break
        rows.append(task)
    end = cursor + len(rows)
    result = {'tasks': rows, 'total': len(tasks), 'view': 'tasks',
              'next_cursor': end if end < len(tasks) else None}
    if not tasks:
        result['note'] = 'No task statements are available in the eligible briefs or captured initial user inputs; this is missing evidence, not an empty task distribution.'
    size = len(canonical_json(result).encode())
    session.trace_reads += 1
    session.read_bytes += size
    session.audit.append({'tool': 'list_cases', 'view': 'tasks', 'cursor': cursor, 'returned_bytes': size})
    return result


def list_sources(session, args):
    args = default_arguments(args, (), {"path": None, "cursor": None})
    maximum = _allowance(session, "source")
    path, cursor = args["path"], args["cursor"]
    cursor = 0 if cursor is None else cursor
    if type(cursor) is not int or cursor < 0:
        raise ValueError("Use the cursor from the returned next call")
    if path is not None and (not isinstance(path, str) or not path):
        raise ValueError("path must be a source directory or file")
    files = {file.path: file for file in session.request.repository_map.files}
    roots = session.request.repository_map.editable_roots
    rows = []
    if path is None:
        rows = [
            {"path": root, "kind": "file" if root in files else "directory"}
            for root in roots
        ]
        rows += [
            {"path": root, "kind": "read_only"}
            for root in session.read_only_context.get("roots", ())
            if root not in roots
        ]
    elif path in files:
        rows = [
            {
                "name": symbol.name,
                "line": symbol.first_line,
                "summary": symbol.summary,
                "open": tool_call("read", path=path, line=symbol.first_line),
            }
            for symbol in files[path].symbols
        ]
    else:
        prefix = path + "/"
        children = {}
        for file in files:
            if file.startswith(prefix):
                suffix = file[len(prefix) :]
                child = prefix + suffix.split("/", 1)[0]
                children[child] = "directory" if "/" in suffix else "file"
        if children:
            rows = [
                {"path": child, "kind": kind}
                for child, kind in sorted(children.items())
            ]
        else:
            reader = getattr(session.inputs.definition_reader, "read_read_only", None)
            if not callable(reader):
                raise ValueError("Directory is outside the visible source inventory")
            result = reader(
                request=session.request,
                path=path,
                offset=cursor,
                count=16,
                max_bytes=maximum - 256,
            )
            if result["kind"] == "file":
                rows = [{"path": path, "kind": "file", "read_only": True}]
                cursor = 0
            else:
                rows = result["entries"]
                for row in rows:
                    row["open"] = tool_call(
                        "list_sources" if row["kind"] != "file" else "read",
                        path=row["path"],
                    )
                session.read_only_issued.update(row["path"] for row in rows)
                page = {
                    "path": path,
                    "entries": rows,
                    "next_cursor": result.get("next_cursor"),
                    "read_only": True,
                }
                return _source_directory_result(session, page, maximum)
    if cursor > len(rows):
        raise ValueError("Source directory cursor is outside the available entries")
    page = {"path": path, "entries": [], "total": len(rows), "next_cursor": None}
    for row in rows[cursor : cursor + 16]:
        if "path" in row:
            row["open"] = tool_call(
                "read" if row["kind"] == "file" else "list_sources", path=row["path"]
            )
        if (
            len(canonical_json({**page, "entries": [*page["entries"], row]}).encode())
            + 128
            > maximum
        ):
            if not page["entries"]:
                raise ResearchContentUnavailable("Read allowance cannot fit a source directory entry")
            break
        page["entries"].append(row)
    end = cursor + len(page["entries"])
    page["next_cursor"] = end if end < len(rows) else None
    return _source_directory_result(session, page, maximum)


def _source_directory_result(session, page, maximum):
    size = len(canonical_json(page).encode())
    if size > maximum:
        raise ResearchContentUnavailable("Source directory exceeds its read allowance")
    session.repository_reads += 1
    session.read_bytes += size
    session.audit.append(
        {"tool": "list_sources", "path": page["path"], "returned_bytes": size}
    )
    return page


def _file_page(session, args, maximum):
    reader = session.inputs.definition_reader
    if callable(getattr(reader, "read_path", None)):
        return reader.read_path(request=session.request, **args, max_bytes=maximum)
    path, line, count = args["path"], args["line"], args["count"]
    file = next(
        (
            row
            for row in session.catalog.scopes
            if row.target.kind is ScopeKind.FILE and row.target.path == path
        ),
        None,
    )
    if file is None:
        raise ValueError("source path is outside the visible immutable files")
    original = reader.read_file(
        request=session.request, ref=file.ref, path=path, offset=line - 1, count=count
    )
    if original.start != line - 1 or original.start > original.total:
        raise ValueError("Source reader returned different coordinates")
    lines, used = [], 0
    for text in original.content.splitlines():
        size = len(text.encode()) + bool(lines)
        if used + size > maximum:
            break
        lines.append(text)
        used += size
    if not lines and original.total:
        raise ValueError(
            "Source line is unavailable or does not fit the read allowance"
        )
    end = original.start + len(lines)
    return {
        "path": path,
        "parent_commit": session.request.parent_selection.commit_sha,
        "start": original.start,
        "end": end,
        "total": original.total,
        "content": "\n".join(lines),
        "line_start": line,
        "line_end": end,
        "next_line": end + 1 if end < original.total else None,
        "truncated": end < original.total,
        "read_only": False,
    }


def read_path(session, args):
    args = default_arguments(args, ("path",), {"line": 1, "count": 120})
    if not isinstance(args["path"], str) or not args["path"]:
        raise ValueError("path must be a source file")
    for key, maximum in (("line", 1_000_000_000), ("count", 200)):
        if type(args[key]) is not int or not 1 <= args[key] <= maximum:
            raise ValueError(f"{key} must be in [1, {maximum}]")
    result = _file_page(session, args, _allowance(session, "source"))
    path = result["path"]
    original = ReadResult(
        path,
        "source",
        result["start"],
        result["end"],
        result["total"],
        result["content"],
    )
    record = (
        source_span_evidence(
            commit_sha=session.request.parent_selection.commit_sha,
            path=path,
            symbol="__file__",
            result=original,
        )
        if original.end > original.start
        else None
    )
    scopes = []
    for row in session.catalog.scopes:
        if row.target.path != path:
            continue
        if row.target.kind is ScopeKind.FILE:
            session.issued.add(row.ref)
            scopes.append(row.ref)
            for target in session.catalog.file_targets(row.ref):
                if target.lines[0] <= original.end and target.lines[1] > original.start:
                    session.issued.add(target.ref)
                    scopes.append(target.ref)
    session.repository_reads += 1
    boundaries = getattr(session.inputs.definition_reader, "call_boundaries", None)
    related = []
    if callable(boundaries) and result["end"] > result["start"]:
        available = min(2048, max(0, 16000 - len(canonical_json(result).encode())))
        if available >= 512:
            related = boundaries(request=session.request, path=path, line=result["start"] + 1,
                                 count=result["end"] - result["start"], max_bytes=available)
    return session.record_read(
        "read",
        path,
        original,
        original,
        record,
        {"path": path, "parent_commit": result["parent_commit"], "scope_refs": scopes},
        path=path,
        extras={"related_implementations": related, **{
            key: result[key]
            for key in (
                "read_only",
                "line_start",
                "line_end",
                "next_line",
                "truncated",
            )
        }},
    )


def _trace_search(session, query, cursor, maximum):
    from .trace_provenance import event_provenance
    if cursor is not None and (
        isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0
    ):
        raise ValueError("Invalid trace search cursor")
    allowed = (
        [session.focus.case_ref]
        if session.phase == "diagnosis"
        else sorted(session.available_cases)
    )
    matches, scanned, next_cursor, position = [], 0, None, 0
    for ref in allowed:
        case = session.cases[ref]
        if case.trace_ref_id is None:
            continue
        for event in session.inputs.trace_store.get(case.trace_ref_id).events:
            character_offset = 0
            for line, raw_line in enumerate(event.content.splitlines(keepends=True), 1):
                line_offset = character_offset
                character_offset += len(raw_line)
                text = raw_line.rstrip("\r\n")
                current = position
                position += 1
                if current < (cursor or 0):
                    continue
                if len(matches) >= 20 or scanned >= 20000:
                    next_cursor = current
                    break
                scanned += 1
                index = text.find(query)
                if index < 0:
                    continue
                start = max(0, index - 60)
                excerpt = text[start : start + 240]
                hit = {
                    "citation_ref": ref,
                    "event_ordinal": event.ordinal,
                    "provenance": event_provenance(event),
                    "line": line,
                    "text": excerpt,
                    "text_truncated": start > 0 or start + len(excerpt) < len(text),
                    "open": tool_call(
                        "read_trace_content",
                        ref=ref,
                        event_ordinal=event.ordinal,
                        offset=line_offset + start,
                    ),
                }
                if len(canonical_json(matches + [hit]).encode()) > maximum:
                    if not matches:
                        address = session.render({"citation_ref": ref})["citation_ref"]
                        raise ValueError(
                            f"Trace match does not fit the remaining page. Read {address} "
                            f"event_ordinal={event.ordinal} with read_trace_content instead."
                        )
                    next_cursor = current
                    break
                matches.append(hit)
            if next_cursor is not None:
                break
        if next_cursor is not None:
            break
    return {
        "matches": matches,
        "next_cursor": next_cursor,
        "truncated": next_cursor is not None,
        "scanned_lines": scanned,
    }


def search(session, args):
    source_available = (
        session.phase == "design"
        and session.repository_reads < session.engine.budget.max_repository_reads
        and callable(getattr(session.inputs.definition_reader, "search", None))
    )
    args = default_arguments(
        args,
        ("query",),
        {
            "scope": "source" if source_available else "trace",
            "path": None,
            "cursor": None,
        },
    )
    query, scope, path = args["query"], args["scope"], args["path"]
    if not isinstance(query, str) or not 1 <= len(query.encode()) <= 256:
        raise ValueError("query must contain 1..256 UTF-8 bytes")
    if not isinstance(scope, str) or scope not in {"source", "trace"}:
        raise ValueError("scope must be source or trace")
    maximum = _allowance(session, scope)
    if maximum < 1024:
        raise ValueError(
            "Search needs at least 1024 remaining bytes including navigation metadata; use a direct read"
        )
    cursor = args["cursor"]
    if cursor is not None:
        if not isinstance(cursor, str):
            raise ValueError("Use the returned opaque cursor string")
        cursor = json.loads(cursor)
    hit_budget = max(512, (maximum - 1024) // 2)
    if scope == "source":
        result = session.inputs.definition_reader.search(
            request=session.request,
            query=query,
            path=path,
            cursor=cursor,
            max_results=20,
            max_bytes=hit_budget,
        )
    else:
        if path is not None:
            raise ValueError("path must be null for visible trace search")
        result = _trace_search(session, query, cursor, hit_budget)
    result = {
        **result,
        "scope": scope,
        "query": query,
        "note": "Navigation matches only. Read surrounding source or trace events for evidence.",
    }
    if result.get("next_cursor") is not None:
        result["next_cursor"] = canonical_json(result["next_cursor"])
    for hit in result["matches"]:
        if scope == "source":
            hit["open"] = tool_call(
                "read", path=hit["path"], line=max(1, hit["line"] - 4), count=20
            )
        else:
            ref = hit["citation_ref"]
            session.seen.add(ref)
    size = len(canonical_json(session.render(result)).encode())
    # Search metadata is returned as the body, so charge all of it. Allow for
    # the small navigation wrapper when asking the backend for its page.
    if size > maximum:
        raise ValueError(
            "Search result exceeds its page allowance; narrow the source path or query"
        )
    session.read_bytes += size
    if scope == "source":
        session.repository_reads += 1
    else:
        session.trace_reads += 1
    session.audit.append(
        {
            "tool": "search",
            "scope": scope,
            "query": query,
            "path": path,
            "returned_bytes": size,
            "preview": True,
            "matches": len(result["matches"]),
        }
    )
    if (
        scope == "source"
        and len(result["matches"]) == 1
        and not result.get("next_cursor")
    ):
        # No choice between matches is required: open nearby code in this turn.
        # Search and reading remain separately charged and separately audited.
        hit = result["matches"][0]
        try:
            opened = read_path(session, hit["open"]["arguments"])
        except ValueError:
            pass  # The match remains usable when the next read cannot fit.
        else:
            result.update(
                {
                    key: opened[key]
                    for key in (
                        "content",
                        "evidence_ref",
                        "context",
                        "line_start",
                        "line_end",
                        "related_implementations",
                    )
                    if key in opened
                }
            )
            result["note"] = (
                "One source match: nearby code was read automatically. Other search snippets are navigation only."
            )
            if opened.get("next_line") is not None:
                result["expand"] = [
                    tool_call("read", path=hit["path"], line=opened["next_line"])
                ]
    return session.render(result)
