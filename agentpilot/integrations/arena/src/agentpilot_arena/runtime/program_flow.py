"""Source-backed component, data and state navigation for the selected parent.

This is a static view. Call ordering within one definition is source ordering;
dynamic dispatch and code behind unresolved boundaries are not invented.
"""
from __future__ import annotations

import ast

from agentpilot.app.model_harnesses import build_direction_alias_catalog
from agentpilot.domain import canonical_json


def _text(node, limit=180):
    value = ast.unparse(node) if node is not None else "None"
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _local_nodes(root):
    def walk(node):
        if node is not root and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        yield node
        for child in ast.iter_child_nodes(node):
            yield from walk(child)
    return (root, *(node for statement in root.body for node in walk(statement)))


def _guards(node, parents):
    """Lexical branch context, excluding expressions that evaluate a guard."""
    under, evaluates = [], []
    while node in parents:
        parent = parents[node]
        if isinstance(parent, (ast.If, ast.While)):
            if node is parent.test:
                evaluates.append(_text(parent.test, 140))
            elif node in parent.orelse:
                under.append("else of " + _text(parent.test, 140))
            else:
                under.append(("while " if isinstance(parent, ast.While) else "if ") + _text(parent.test, 140))
        elif isinstance(parent, (ast.For, ast.AsyncFor)):
            if node is parent.iter:
                evaluates.append("iteration over " + _text(parent.iter, 140))
            else:
                under.append(("loop else: " if node in parent.orelse else "for ")
                             + _text(parent.target, 60) + " in " + _text(parent.iter, 100))
        elif isinstance(parent, ast.ExceptHandler):
            under.append("except " + _text(parent.type, 100))
        node = parent
    return list(reversed(under)), list(reversed(evaluates))


def _result_use(call, parents):
    """Only an entire RHS (optionally awaited) directly binds a call result."""
    node = call
    parent = parents.get(node)
    while isinstance(parent, ast.Await):
        node, parent = parent, parents.get(parent)
    if isinstance(parent, ast.Assign) and node is parent.value:
        return ", ".join(_text(t) for t in parent.targets), None
    if isinstance(parent, ast.AnnAssign) and node is parent.value:
        return _text(parent.target), None
    if isinstance(parent, ast.Return) and node is parent.value:
        return "return", None
    if isinstance(parent, (ast.Call, ast.Tuple, ast.List, ast.Dict, ast.BinOp, ast.BoolOp,
                           ast.Compare, ast.UnaryOp, ast.IfExp, ast.Subscript, ast.Attribute)):
        return None, _text(parent, 180)
    return None, None


def _storage_kind(destinations):
    attributes = [n for d in destinations for n in ast.walk(d)
                  if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)]
    if any(isinstance(n.value, ast.Name) and n.value.id in {"self", "cls"} for n in attributes):
        return "instance_attribute"
    return "object_attribute" if attributes else "local_data"


def _data_references(node):
    """Exact variable/attribute dependencies, including keyword argument values.

    Never promote ``self`` into a wildcard matching every instance attribute.
    Nested function bodies are excluded by the caller's local-node selection.
    """
    return {_text(n) for n in ast.walk(node) if isinstance(n, (ast.Name, ast.Attribute))
            and not (isinstance(n, ast.Name) and n.id in {"self", "cls"})}


def _call_inputs(calls, local=()):
    refs = set()
    for call in calls:
        for value in (*call.args, *(item.value for item in call.keywords)):
            refs.update(_data_references(value))
        if isinstance(call.func, ast.Attribute):
            refs.update(_data_references(call.func.value))
    # Follow definitions of inputs backwards through local assignments. This is
    # a syntactic dependency summary, not proof of a particular runtime value.
    changed = True
    while changed:
        before = len(refs)
        for node in local:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
                if any(_data_references(target) & refs for target in targets):
                    refs.update(_data_references(node.value))
        changed = len(refs) != before
    return refs


def _control_slice(body, local, calls, links):
    """Keep branch structure around actions, exits and their local dependencies.

    This is a source slice, not a reconstructed control-flow graph: omitted
    statements and shortened expressions remain explicit.
    """
    def names(node):
        return {_text(n) for n in ast.walk(node) if isinstance(n, (ast.Name, ast.Attribute))
                and not (isinstance(n, ast.Name) and n.id in {"self", "cls"})}

    seeds = {call for call in calls if
             links.get((call.lineno, _text(call.func, 240))) or
             isinstance(call.func, ast.Attribute) and call.func.attr in {"chat", "call", "parse_response", "send_keys"}}
    parents = {child:parent for parent in local for child in ast.iter_child_nodes(parent)}
    kept = set(seeds) | {n for n in local if isinstance(n, (ast.Return, ast.Break, ast.Continue))}
    wanted = set().union(*(names(n) for n in kept)) if kept else set()
    # The inputs to retained statements and branch guards define the slice.
    changed = True
    while changed:
        old = (len(kept), len(wanted))
        for n in tuple(kept):
            parent = parents.get(n)
            if parent is not None and parent not in kept:
                kept.add(parent)
                if isinstance(parent, (ast.If, ast.While)):
                    wanted.update(names(parent.test))
                elif isinstance(parent, (ast.For, ast.AsyncFor)):
                    wanted.update(names(parent.iter))
        for n in local:
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                destinations = n.targets if isinstance(n, ast.Assign) else (n.target,)
                if any(names(d) & wanted for d in destinations):
                    kept.add(n)
                    if n.value is not None:
                        wanted.update(names(n.value))
        changed = old != (len(kept), len(wanted))
    rows = []
    def emit(node, indent, text):
        rows.append({"line":node.lineno,"text":"  " * indent + text})
    def block(statements, indent):
        omitted = False
        for n in statements:
            if n not in kept:
                if not omitted:
                    emit(n, indent, "… [other statements omitted]")
                omitted = True
                continue
            omitted = False
            if isinstance(n, (ast.If, ast.While, ast.For, ast.AsyncFor)):
                if isinstance(n, (ast.If, ast.While)):
                    header = ("if " if isinstance(n, ast.If) else "while ") + _text(n.test, 160)
                else:
                    header = "for " + _text(n.target, 60) + " in " + _text(n.iter, 100)
                emit(n, indent, header + ":")
                block(n.body, indent + 1)
                if n.orelse:
                    emit(n.orelse[0], indent, "else:")
                    block(n.orelse, indent + 1)
            elif isinstance(n, (ast.Try, ast.TryStar)):
                emit(n, indent, "try:")
                block(n.body, indent + 1)
                for handler in n.handlers:
                    emit(handler, indent, "except " + _text(handler.type, 120) + ":")
                    block(handler.body, indent + 1)
                for label, statements in (("else:", n.orelse), ("finally:", n.finalbody)):
                    if statements:
                        emit(statements[0], indent, label)
                        block(statements, indent + 1)
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                emit(n, indent, "with " + ", ".join(_text(i.context_expr, 100) for i in n.items) + ":")
                block(n.body, indent + 1)
            else:
                emit(n, indent, _text(n, 240))
    block(body.body, 0)
    return rows


def select_entries(reader, request, targets):
    """Use configured symbols when available; otherwise explicitly offer hints."""
    configured = tuple(getattr(reader, "entrypoint_symbols", ()))
    if configured:
        by_name = {(t.file, t.symbol): t for t in targets.values()}
        try:
            return [by_name[(path, symbol)] for path, symbol in configured], "configured callable addresses, verified in this parent"
        except KeyError as exc:
            raise ValueError("configured callable entry is absent from selected parent") from exc
    paths = tuple(reader.entrypoint_paths)
    if paths:
        reader._entrypoint_files(request)
    groups = [[t for t in targets.values() if t.file == path] for path in paths] if paths else [list(targets.values())]
    entries = []
    for group in groups:
        named = [t for t in group if t.symbol.rsplit(".", 1)[-1] in {"run", "main"}]
        # Never silently discard other configured entry files or name matches.
        entries.extend(sorted(named or group, key=lambda t:(t.file, t.lines)) if named else
                       sorted(group, key=lambda t:(t.file, t.lines))[:1])
    return entries, ("configured files; callable selection is a name hint, not verified launcher binding" if paths else
                     "run/main name hints (or first callable fallback); launcher binding is unknown")


def _role_label(local, calls):
    """Multiple syntax-derived hints; none establishes a module's whole role."""
    names = {call.func.attr for call in calls if isinstance(call.func,ast.Attribute)}
    labels = []
    if "send_keys" in names:
        labels.append("Terminal actions")
    if "parse_response" in names:
        labels.append("Response parsing")
    if "chat" in names or "call" in names:
        labels.append("Model-call boundary hint")
    if "format" in names and any(isinstance(call.func,ast.Name) and call.func.id=="Chat" for call in calls):
        labels.append("Chat/input construction hint")
    if any(isinstance(n,(ast.For,ast.While,ast.AsyncFor)) for n in local) and any(
        isinstance(n,ast.Await) for n in local
    ):
        labels.append("Awaited iteration")
    if any(isinstance(n,ast.ExceptHandler) for n in local):
        labels.append("Exception branches")
    return "; ".join(labels) or "Callable source"


def _source_text_candidates(reader, request, nodes, modules):
    """Short source literals on displayed call/return paths, not resolved prompts."""
    files = {f.path for f in request.repository_map.files}
    groups = []
    for component in nodes:
        tree = modules[component["path"]]
        body = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.lineno == component["line"] and n.end_lineno == component["end_line"])
        local = _local_nodes(body)
        parents = {child: parent for parent in local for child in ast.iter_child_nodes(parent)}
        calls = [n for n in local if isinstance(n, ast.Call)]
        wanted = _call_inputs(calls, local)
        for node in local:
            if isinstance(node, ast.Return) and node.value is not None:
                wanted.update(_data_references(node.value))
        source = "\n".join(reader._file_lines(request, component["path"]))
        candidates = []
        for node in local:
            if not (isinstance(node, ast.JoinedStr) or isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            parent = parents.get(node)
            if isinstance(parent, (ast.Expr, ast.JoinedStr)):
                continue  # Docstrings/standalone prose, and duplicate f-string pieces.
            ancestor = node
            relevant = False
            while ancestor in parents:
                ancestor = parents[ancestor]
                if isinstance(ancestor, (ast.Call, ast.Return)):
                    relevant = True
                    break
                if isinstance(ancestor, (ast.Assign, ast.AnnAssign)):
                    destinations = ancestor.targets if isinstance(ancestor, ast.Assign) else (ancestor.target,)
                    relevant = any(_data_references(d) & wanted for d in destinations)
                    break
            if not relevant:
                continue
            literal = node.value if isinstance(node, ast.Constant) else None
            matches = []
            if (isinstance(literal, str) and literal.endswith((".txt", ".md", ".jinja", ".jinja2", ".j2"))
                    and ".." not in literal.split("/")):
                matches = sorted(p for p in files if p == literal or p.endswith("/" + literal))
            text = ast.get_source_segment(source, node) or ""
            path, line, kind = component["path"], node.lineno, "formatted_source" if literal is None else "source_literal"
            file_tail_omitted = False
            if len(matches) == 1:
                path, line, kind = matches[0], 1, "referenced_text_file"
                file_lines = reader._file_lines(request, path)
                text = "\n".join(file_lines[:12])
                file_tail_omitted = len(file_lines) > 12
            elif literal is not None and len(literal) < 40 and "\n" not in literal:
                continue
            preview = text.encode()[:320].decode("utf-8", errors="ignore")
            candidates.append({"ref": component["ref"], "caller": component["symbol"],
                "path": path, "line": line, "kind": kind, "source_excerpt": preview,
                "truncated": preview != text or file_tail_omitted,
                "open": {"tool": "read", "arguments": {"path": path, "line": line, "count": 80}}})
        groups.append(candidates)
    # Give each displayed callable a turn, without prioritizing completion text.
    return [group[i] for i in range(max((len(g) for g in groups), default=0))
            for group in groups if i < len(group)]


def program_flow_context(reader, request, *, max_bytes=16384, max_components=16):
    if type(max_bytes) is not int or max_bytes < 1024:
        raise ValueError("program flow requires at least1024 bytes")
    if type(max_components) is not int or not 1 <= max_components <= 128:
        raise ValueError("program flow component limit must be in [1, 128]")
    if request.repository_map.commit_sha != request.parent_selection.commit_sha:
        raise ValueError("program flow map belongs to another parent")
    catalog = build_direction_alias_catalog(request)
    targets = {t.ref:t for t in catalog.targets if t.kind == "function"}
    entries, basis = select_entries(reader, request, targets)
    modules, nodes, edges, selected, queue = {}, [], [], set(), [t.ref for t in entries]
    priorities = {t.ref:(0, 0, 0) for t in entries}
    visited = set()
    pending_edges = []
    workflow = None
    workflow_rank = 0
    class_conditions = {}
    mutating_helpers = set()
    while queue and len(nodes) < max_components:
        ref = queue.pop(0)
        if ref in visited:
            continue
        visited.add(ref)
        target = targets[ref]
        # read_definition verifies the declaration against the frozen map.
        reader.read_definition(request=request, target=target, offset=0, count=1)
        if target.file not in modules:
            modules[target.file] = ast.parse("\n".join(reader._file_lines(request, target.file)))
            for owner in ast.walk(modules[target.file]):
                if not isinstance(owner, ast.ClassDef):
                    continue
                refs = set()
                for member in owner.body:
                    if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for n in _local_nodes(member):
                        if isinstance(n, (ast.If, ast.While)):
                            refs.update(value for value in _data_references(n.test) if value.startswith("self."))
                        if isinstance(n, (ast.Assign, ast.AnnAssign)):
                            destinations = n.targets if isinstance(n, ast.Assign) else (n.target,)
                            if _storage_kind(destinations) == "instance_attribute":
                                mutating_helpers.add((target.file, member.lineno))
                class_conditions[(target.file, owner.name)] = refs
        body = next(n for n in ast.walk(modules[target.file])
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.lineno == target.lines[0] and n.end_lineno == target.lines[1])
        local = _local_nodes(body)
        shared_conditions = class_conditions.get((target.file, target.symbol.rpartition(".")[0]), set())
        parents = {child:parent for parent in local for child in ast.iter_child_nodes(parent)}
        calls = sorted((n for n in local if isinstance(n,ast.Call)), key=lambda n:(n.lineno,n.col_offset))
        # Reuse the reader's conservative same-file declaration resolver.
        links, offset = {}, 0
        while True:
            navigation = reader.read_navigation(request=request, ref=ref, offset=offset, count=64, max_bytes=16384)
            links.update({(r["line"], r["expression"]):r.get("declaration_ref") for r in navigation["entries"]})
            if navigation["next_cursor"] is None:
                break
            offset = navigation["next_cursor"]
        loop_rank = max((sum(isinstance(n, ast.Await) for n in _local_nodes(loop))
                         for loop in local if isinstance(loop, (ast.For, ast.While, ast.AsyncFor))), default=0)
        if loop_rank > workflow_rank:
            workflow = {"ref":ref,"path":target.file,"symbol":target.symbol,
                        "rows":_control_slice(body, local, calls, links)}
            workflow_rank = loop_rank
        operations = []
        for call in calls:
            expression = _text(call.func,240)
            declaration = links.get((call.lineno,expression))
            if expression.startswith(("self.logger.", "logger.", "logging.")):
                continue
            conditions, evaluates = _guards(call, parents)
            binding, feeds = _result_use(call, parents)
            row = {"line":call.lineno,"expression":expression,
                   "arguments":_text(call,220),"binds":binding,"under":conditions}
            if evaluates:
                row["evaluates"] = evaluates
            if feeds:
                row["feeds"] = feeds
            if isinstance(parents.get(call), ast.Await):
                row["awaited"] = True
            if declaration:
                row["declaration_ref"] = declaration
            pending_edges.append({"caller_ref":ref, "path":target.file, **row})
            operations.append(row)
            if declaration in targets and declaration not in visited:
                branch_cost = sum(4 if c.startswith("except ") else 1 for c in conditions
                                  if not c.startswith(("for ", "while ")))
                entry_state_call = ref in {entry.ref for entry in entries} and (
                    targets[declaration].file, targets[declaration].lines[0]) in mutating_helpers
                helper_cost = 0 if isinstance(parents.get(call), ast.Await) or binding == "return" or entry_state_call else 1
                old = priorities[ref]
                priority = (old[0] + branch_cost, old[1] + helper_cost, old[2] + 1)
                priorities[declaration] = min(priorities.get(declaration, priority), priority)
                if declaration not in queue:
                    queue.append(declaration)
        # Prefer inter-component transfers over constructors used for logging,
        # but preserve source order among the displayed operations.
        boundary_calls = {"parse_response", "send_keys", "get_incremental_output", "chat", "call"}
        def operation_priority(o):
            return (
            bool(o.get("feeds")),
            0 if o.get("awaited") or o["expression"].split(".")[-1] in boundary_calls else
            1 if o.get("declaration_ref") in targets else
            1 if o["expression"].split(".")[-1] in boundary_calls else
            2 if o["binds"] else 3, o["line"])
        picked_ops = sorted(operations, key=operation_priority)[:8]
        picked_ops.sort(key=lambda o:o["line"])
        branch_names = {_text(v) for n in local if isinstance(n,(ast.If,ast.While))
                        for v in ast.walk(n.test) if isinstance(v,(ast.Name,ast.Attribute))}
        exit_names = {_text(v) for n in local if isinstance(n,(ast.If,ast.While))
                      and any(isinstance(x,(ast.Return,ast.Continue,ast.Break)) and x in local for x in ast.walk(n))
                      for v in ast.walk(n.test) if isinstance(v,(ast.Name,ast.Attribute))}
        # Carry the branch's local snapshot dependency back to stored state,
        # e.g. was_pending = self.pending, so both True and False writes appear.
        for node in local:
            if isinstance(node,(ast.Assign,ast.AnnAssign)):
                destinations = node.targets if isinstance(node,ast.Assign) else (node.target,)
                if isinstance(node.value,(ast.Name,ast.Attribute)) and any(
                    _text(v) in exit_names for d in destinations for v in ast.walk(d)
                    if isinstance(v,(ast.Name,ast.Attribute))):
                    exit_names.update(_text(v) for v in ast.walk(node.value)
                                      if isinstance(v,(ast.Name,ast.Attribute)))
        data_names = _call_inputs(calls, local)
        direct_inputs = _call_inputs(calls)
        transfer_inputs = _call_inputs([call for call in calls
            if links.get((call.lineno, _text(call.func, 240))) in targets
            or isinstance(parents.get(call), ast.Await)])
        parameters = {a.arg for a in (*body.args.posonlyargs, *body.args.args, *body.args.kwonlyargs)} - {"self", "cls"}
        is_entry = ref in {entry.ref for entry in entries}
        state_writes = []
        for node in local:
            if isinstance(node,(ast.Assign,ast.AnnAssign,ast.AugAssign)):
                destinations = node.targets if isinstance(node,ast.Assign) else (node.target,)
                names = [t for dest in destinations for t in ast.walk(dest)
                         if isinstance(t,ast.Attribute) and isinstance(t.ctx,ast.Store)]
                named = [_text(t) for dest in destinations for t in ast.walk(dest)
                         if isinstance(t,(ast.Name,ast.Attribute)) and isinstance(t.ctx,ast.Store)]
                if names or any(name in data_names or name in branch_names for name in named):
                    input_binding = _storage_kind(destinations) == "instance_attribute" and (
                        any(name in data_names for name in named)
                        or is_entry and isinstance(node.value, ast.Name) and node.value.id in parameters
                    )
                    state_writes.append({"line":node.lineno,"assignment":_text(node,200),
                        "kind":_storage_kind(destinations), "under":_guards(node, parents)[0],
                        "priority":-1 if any(name in branch_names or name in exit_names or name in shared_conditions for name in named)
                                    and isinstance(node.value,ast.Constant) and isinstance(node.value.value,bool) else
                                   0 if input_binding else
                                   0 if any(name in exit_names for name in named) else
                                   0 if any(name in shared_conditions for name in named) else
                                   1 if any(name in transfer_inputs for name in named) else
                                   2 if any(name in direct_inputs for name in named) else
                                   1 if any(name in data_names for name in named) and isinstance(node.value,(ast.Name,ast.Attribute)) else
                                   2 if any(name in data_names or name in branch_names for name in named) else 3})
        picked_state = sorted(state_writes,key=lambda row:(row["priority"],row["line"]))[:8]
        picked_state = [{k:v for k,v in row.items() if k!="priority"} for row in sorted(picked_state,key=lambda row:row["line"])]
        controls = [{"line":n.lineno,"condition":_text(n.test,140),
                     "priority":0 if any(isinstance(v,(ast.Return,ast.Continue,ast.Break)) and v in local for v in ast.walk(n)) else 1}
                    for n in local if isinstance(n,(ast.If,ast.While))]
        picked_controls = sorted(controls,key=lambda row:(row["priority"],row["line"]))[:8]
        picked_controls = [{k:v for k,v in row.items() if k!="priority"} for row in sorted(picked_controls,key=lambda row:row["line"])]
        returns = [{"line":n.lineno,"value":_text(n.value,160),"under":_guards(n, parents)[0]}
                   for n in local if isinstance(n,ast.Return)]
        exits = [{"line":n.lineno,"action":type(n).__name__.lower(),"under":_guards(n, parents)[0]}
                 for n in local if isinstance(n,(ast.Return,ast.Break,ast.Continue))]
        transfers = []
        for n in local:
            if not isinstance(n,ast.Assign) or not isinstance(n.value,ast.Name) or not all(isinstance(t,ast.Name) for t in n.targets):
                continue
            parent = parents.get(n)
            while parent is not None and not isinstance(parent,(ast.For,ast.While,ast.AsyncFor)):
                parent = parents.get(parent)
            if parent is not None:
                transfers.append({"line":n.lineno,"assignment":_text(n,180),"under":_guards(n, parents)[0]})
        info = {"ref":ref,"path":target.file,"symbol":target.symbol,"line":target.lines[0],
                "role_label":_role_label(local,calls),
                "end_line":target.lines[1],"signature":_text(body.args,240),
                "declared_purpose":(ast.get_docstring(body) or "").split("\n",1)[0][:240],
                "operations":picked_ops, "omitted_operations":max(0,len(operations)-len(picked_ops)),
                "state_writes":picked_state,"omitted_state_writes":max(0,len(state_writes)-len(picked_state)),
                "conditions":picked_controls,"omitted_conditions":max(0,len(controls)-len(picked_controls)),
                "iteration_transfers":transfers[-4:],"omitted_iteration_transfers":max(0,len(transfers)-4),
                "returns":returns[:4],"omitted_returns":max(0,len(returns)-4)}
        info["control_exits"] = exits[:8]
        info["omitted_control_exits"] = max(0,len(exits)-8)
        # Share the overview across components instead of letting one large
        # recovery function consume the normal request path's entire allowance.
        fields = ("operations", "state_writes", "conditions", "returns", "control_exits", "iteration_transfers")
        node_limit = max(850, min(1500, (max_bytes - 1800) // 10))
        minimum = {"operations":3, "state_writes":4 if is_entry else 2, "conditions":1, "returns":1,
                   "control_exits":1, "iteration_transfers":1}
        while len(canonical_json(info).encode()) > node_limit:
            reducible = [key for key in fields if len(info[key]) > minimum[key]]
            if not reducible:
                break
            field = max(reducible, key=lambda key:len(canonical_json(info[key])))
            if field == "operations":
                info[field].remove(max(info[field], key=operation_priority))
            elif field == "state_writes":
                state_priority = {r["line"]:r["priority"] for r in state_writes}
                info[field].remove(max(info[field], key=lambda r:(state_priority[r["line"]],r["line"])))
            elif field == "iteration_transfers":
                info[field].pop(0)
            else:
                info[field].pop()
            info["omitted_" + field] += 1
        reserve = min(8500 if workflow else 4096, max_bytes // 2)
        if len(canonical_json([*nodes,info]).encode()) > max_bytes - reserve:
            queue.sort(key=lambda r:priorities[r])
            continue
        nodes.append(info)
        selected.add(ref)
        # Walk structural controllers before miscellaneous helper bodies while
        # retaining every call address as an expandable boundary.
        queue.sort(key=lambda r:priorities[r])
    for edge in pending_edges:
        if edge["caller_ref"] not in selected:
            continue
        if edge.get("declaration_ref") in targets:
            edges.append(edge)
    result = {"schema":"source-mechanism-map-v3", "parent_id":request.repository_map.parent_id,
              "commit_sha":request.parent_selection.commit_sha,"tree_sha":request.repository_map.tree_sha,
              "entry_refs":[t.ref for t in entries if t.ref in selected],"selection_basis":basis,
              "omitted_entry_count":sum(t.ref not in selected for t in entries),
              "components":nodes,"calls":[],"omitted_components":max(0,len(targets)-len(nodes)),
              "total_resolved_call_edges":len(edges), "omitted_call_edges":len(edges),
              "unexpanded_callee_edges":sum(e["declaration_ref"] not in selected for e in edges),
              "authority":"Rule-based source facts from this parent; role labels are heuristic navigation hints, not architectural modules. Docstrings state intent. Unresolved calls remain boundaries; dynamic dispatch and actual execution are not inferred."}
    boundary_reader = getattr(reader, "call_boundaries", None)
    if callable(boundary_reader) and max_bytes >= 8192:
        boundaries = {}
        for node in nodes:
            for hint in boundary_reader(request=request, path=node["path"], line=node["line"],
                                        count=node["end_line"]-node["line"]+1, max_bytes=4096):
                boundaries.setdefault((hint["path"],hint["symbol"]), hint)
        # Surface direct model/tool boundaries ahead of incidental helper calls.
        ordered = sorted(boundaries.values(), key=lambda h:(
            h["symbol"].split(".")[-1] not in {"chat", "send_keys", "call"}, h["path"],h["line"]))
        result["boundary_reads"] = []
        result["omitted_boundary_reads"] = len(ordered)
        for hint in ordered:
            # The initial renderer states this basis once for the whole table.
            hint = {key:value for key,value in hint.items() if key != "basis"}
            if len(canonical_json(result["boundary_reads"] + [hint]).encode()) > 2048:
                break
            result["boundary_reads"].append(hint)
            result["omitted_boundary_reads"] -= 1
    if workflow and workflow["ref"] in selected:
        rows = workflow["rows"]
        result["workflow_slice"] = {**workflow, "rows":[],"omitted_rows":len(rows)}
        # Keep the whole control structure when it fits; otherwise state the
        # missing tail explicitly rather than printing an apparently complete flow.
        for row in rows:
            candidate = {**result, "workflow_slice":{**result["workflow_slice"],
                "rows":[*result["workflow_slice"]["rows"],row],
                "omitted_rows":result["workflow_slice"]["omitted_rows"]-1}}
            if len(canonical_json(candidate).encode()) > max_bytes - min(1800, max_bytes // 8):
                break
            result = candidate
    if max_bytes >= 4096:
        anchors = _source_text_candidates(reader, request, nodes, modules)
        result["text_anchors"] = []
        result["omitted_text_anchors"] = len(anchors)
        for anchor in anchors:
            candidate = {**result, "text_anchors": [*result["text_anchors"], anchor],
                         "omitted_text_anchors": result["omitted_text_anchors"] - 1}
            if (len(canonical_json(candidate["text_anchors"]).encode()) > 2048
                    or len(canonical_json(candidate).encode()) > max_bytes - min(500, max_bytes // 8)):
                break
            result = candidate
    for edge in edges:
        if edge["declaration_ref"] not in selected:
            continue
        address = {key:edge[key] for key in ("caller_ref", "declaration_ref", "path", "line")}
        candidate = {**result,"calls":[*result["calls"],address],"omitted_call_edges":result["omitted_call_edges"]-1}
        if len(canonical_json(candidate).encode()) > max_bytes:
            break
        result = candidate
    return result
