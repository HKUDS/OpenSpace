"""Readable implementation addresses from declared types, never dynamic dispatch."""
from __future__ import annotations

import ast
from pathlib import PurePosixPath

from agentpilot.domain import canonical_json


def _local_nodes(root):
    def walk(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        yield node
        for child in ast.iter_child_nodes(node):
            yield from walk(child)
    return (node for statement in root.body for node in walk(statement))


def _types(annotation):
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            return _types(ast.parse(annotation.value, mode="eval").body)
        except SyntaxError:
            return set()
    if isinstance(annotation, ast.Name):
        return {annotation.id} - {"None", "Any"}
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _types(annotation.left) | _types(annotation.right)
    if isinstance(annotation, ast.Subscript):
        wrapper = ast.unparse(annotation.value).split(".")[-1]
        if wrapper in {"Optional", "Union"}:
            values = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
            return set().union(*(_types(value) for value in values))
        if wrapper == "Annotated" and isinstance(annotation.slice, ast.Tuple):
            return _types(annotation.slice.elts[0])
    return set()


def call_boundaries(reader, request, path, line=1, count=200, max_bytes=2048):
    visible = reader._visible_source_paths(request)
    if path not in visible:
        raise ValueError("call boundary path is outside visible parent source")
    modules = {}

    def module(filename):
        if filename not in modules:
            try:
                modules[filename] = ast.parse("\n".join(reader._source_lines(request.parent_selection.commit_sha, filename)))
            except SyntaxError:
                modules[filename] = None
        return modules[filename]

    tree = module(path)
    if tree is None:
        return []
    bindings = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.level:
                base = PurePosixPath(path).parent
                for _ in range(node.level - 1):
                    base = base.parent
                stem = str(base / node.module.replace(".", "/"))
                candidates = [p for p in visible if p in {stem + ".py", stem + "/__init__.py"}]
            else:
                stem = node.module.replace(".", "/")
                candidates = [p for p in visible if any(p == suffix or p.endswith("/" + suffix)
                              for suffix in (stem + ".py", stem + "/__init__.py"))]
            if len(candidates) == 1:
                for alias in node.names:
                    if alias.name != "*":
                        bindings[alias.asname or alias.name] = (candidates[0], alias.name)
        elif isinstance(node, ast.ClassDef):
            bindings[node.name] = (path, node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings.pop(target.id, None)

    parents = {child:parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    rows, seen = [], set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if function.end_lineno < line or function.lineno >= line + count:
            continue
        owner = parents.get(function)
        if owner is not tree and not isinstance(owner, ast.ClassDef):
            continue  # Local closures have a different binding scope.
        declared = {arg.arg:_types(arg.annotation) for arg in
                    (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)}
        if isinstance(owner, ast.ClassDef):
            for member in owner.body:
                annotations = ([member] if isinstance(member, ast.AnnAssign) else
                               _local_nodes(member) if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) else ())
                for annotation in annotations:
                    if not isinstance(annotation, ast.AnnAssign):
                        continue
                    target = annotation.target
                    if isinstance(member, ast.AnnAssign) and isinstance(target, ast.Name):
                        key = "self." + target.id
                    elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                        key = ast.unparse(target)
                    else:
                        continue
                    declared.setdefault(key, set()).update(_types(annotation.annotation))

        def local(node):
            if node is not function and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                return
            yield node
            for child in ast.iter_child_nodes(node):
                yield from local(child)

        for call in local(function):
            if not isinstance(call, ast.Call) or not line <= call.lineno < line + count or not isinstance(call.func, ast.Attribute):
                continue
            receiver = ast.unparse(call.func.value)
            for typename in sorted(declared.get(receiver, ())):
                address = bindings.get(typename)
                if address is None:
                    continue
                filename, classname = address
                target_module = module(filename)
                if target_module is None:
                    continue
                classes = [n for n in target_module.body if isinstance(n, ast.ClassDef) and n.name == classname]
                if len(classes) != 1:
                    continue
                methods = [n for n in classes[0].body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == call.func.attr]
                if len(methods) != 1:
                    continue
                method = methods[0]
                identity = (ast.unparse(call.func), filename, method.lineno)
                if identity in seen:
                    continue
                row = {"call":ast.unparse(call.func), "call_line":call.lineno,
                       "declared_type":typename, "path":filename, "symbol":classname + "." + method.name,
                       "line":method.lineno, "read_only":visible[filename],
                       "basis":"Declared type and source import; inspect implementation. Runtime dispatch may differ.",
                       "open":{"tool":"read", "arguments":{"path":filename, "line":method.lineno, "count":120}}}
                if len(canonical_json(rows + [row]).encode()) > max_bytes:
                    return rows
                rows.append(row)
                seen.add(identity)
    return rows
