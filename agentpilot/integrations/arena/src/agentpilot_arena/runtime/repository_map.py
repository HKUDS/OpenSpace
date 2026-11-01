"""Immutable repository-map construction shared by AgentPilot methods."""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from agentpilot.app.model_harnesses import (
    DirectionTargetAlias,
    build_direction_alias_catalog,
)
from agentpilot.domain import ParentRepositoryMap, ReadResult, ScopeKind, canonical_json
from agentpilot.policy.proposer import StructuredProposalRequest
from agentpilot.search import ParentRecord

from .structural_adherence import (
    ImmutableGitReaderConfig,
    PythonParentRepositoryMapBuilder,
    SubprocessImmutableGitObjectReader,
)


class CandidateMapSettings(Protocol):
    git_executable: Path
    editable_roots: tuple[str, ...]
    max_files: int
    max_file_bytes: int
    git_timeout_seconds: float
    max_git_output_bytes: int
    read_only_roots: tuple[str, ...]


class ActionMapSettings(Protocol):
    program_repository: Path
    candidate: CandidateMapSettings


def repository_map_builder(
    action: ActionMapSettings,
) -> Callable[[ParentRecord], ParentRepositoryMap]:
    """Build a trusted source outline for the selected immutable parent."""

    candidate = action.candidate
    builder = PythonParentRepositoryMapBuilder(
        SubprocessImmutableGitObjectReader(
            ImmutableGitReaderConfig(
                repository=action.program_repository,
                git_executable=candidate.git_executable,
                timeout_seconds=candidate.git_timeout_seconds,
                max_output_bytes=candidate.max_git_output_bytes,
                max_blob_bytes=min(
                    candidate.max_file_bytes,
                    candidate.max_git_output_bytes,
                ),
            )
        ),
        editable_roots=candidate.editable_roots,
        max_files=candidate.max_files,
    )

    def build(parent: ParentRecord) -> ParentRepositoryMap:
        return builder.build(
            parent_id=parent.candidate.parent_id,
            commit_sha=parent.candidate.commit_sha,
        )

    return build


@dataclass(frozen=True)
class InitialSourceBlock:
    """Verbatim source with enough coordinates to expand it through ``read``.

    Definition offsets are relative to the named symbol; file offsets are
    absolute. These excerpts are an entry point, not an exhaustive program map
    or a restriction on what the proposer can inspect or modify.
    """

    path: str
    symbol: str | None
    result: ReadResult


_EXECUTION_METHODS = (
    "run",
    "_run_agent_loop",
    "_handle_llm_interaction",
    "_execute_commands",
)
_EXECUTION_CALLS = frozenset(
    {
        "_run_agent_loop",
        "_handle_llm_interaction",
        "_execute_commands",
        "_query_llm",
        "parse_response",
        "send_keys",
        "get_incremental_output",
        "_get_completion_confirmation_message",
        "_reset_per_run_state",
    }
)


class ImmutableGitDefinitionReader:
    """Read source from the selected immutable parent, never its worktree."""

    def __init__(
        self,
        reader: SubprocessImmutableGitObjectReader,
        *,
        entrypoint_paths: tuple[str, ...] = (),
        entrypoint_symbols: tuple[tuple[str, str], ...] = (),
        read_only_roots: tuple[str, ...] = (),
        max_read_only_files: int = 4096,
    ) -> None:
        if not isinstance(read_only_roots, tuple):
            raise ValueError("read_only_roots must be a tuple of repository paths")  # noqa: TRY004
        for root in read_only_roots:
            self._read_only_path(root)
        if len(read_only_roots) != len(set(read_only_roots)):
            raise ValueError("read_only_roots contains duplicates")
        if (
            isinstance(max_read_only_files, bool)
            or not isinstance(max_read_only_files, int)
            or not 1 <= max_read_only_files <= 4096
        ):
            raise ValueError("max_read_only_files must be in [1, 4096]")
        self.reader = reader
        self.entrypoint_paths = tuple(entrypoint_paths)
        self.entrypoint_symbols = tuple(entrypoint_symbols)
        for path, symbol in self.entrypoint_symbols:
            self._read_only_path(path)
            if not isinstance(symbol, str) or not symbol or any(not part.isidentifier() for part in symbol.split(".")):
                raise ValueError("entrypoint symbol must be a qualified Python name")
        if len(self.entrypoint_symbols) != len(set(self.entrypoint_symbols)):
            raise ValueError("entrypoint symbols contain duplicates")
        self.read_only_roots = read_only_roots
        self.max_read_only_files = max_read_only_files
        self._source_cache: dict[tuple[str, str], tuple[str, ...]] = {}
        self._read_only_path_cache: dict[str, tuple[str, ...]] = {}

    @staticmethod
    def _read_only_path(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("read-only path must be a normalized relative POSIX path")  # noqa: TRY004
        path = PurePosixPath(value)
        if (
            not path.parts
            or path.is_absolute()
            or str(path) != value
            or "\\" in value
            or any(ord(character) < 32 for character in value)
            or ".." in path.parts
            or any(
                part in {".git", ".env"} or part.startswith(".env.")
                for part in path.parts
            )
        ):
            raise ValueError("read-only path must be a normalized relative source path")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("read-only path must be UTF-8") from exc
        return value

    def _read_only_paths(self, request: StructuredProposalRequest) -> tuple[str, ...]:
        if request.repository_map.commit_sha != request.parent_selection.commit_sha:
            raise ValueError("repository map belongs to another selected parent")
        if not self.read_only_roots:
            return ()
        commit = request.parent_selection.commit_sha
        if commit not in self._read_only_path_cache:
            paths = self.reader.list_regular_paths(commit, self.read_only_roots)
            # Apply the count to the complete allowlist, not a requested subtree:
            # navigating smaller directories cannot evade this bound.
            if len(paths) > self.max_read_only_files:
                raise ValueError("read-only regular-file count exceeds its bound")
            accepted = []
            for path in paths:
                if not any(
                    path == root or path.startswith(root + "/")
                    for root in self.read_only_roots
                ):
                    raise ValueError("Git listing escaped read_only_roots")
                try:
                    self._read_only_path(path)
                except ValueError:
                    continue
                accepted.append(path)
            if len(self._read_only_path_cache) >= 8:
                self._read_only_path_cache.pop(next(iter(self._read_only_path_cache)))
            self._read_only_path_cache[commit] = tuple(accepted)
        # Editing addresses always come from the original map. An overlapping
        # read root cannot create alternate aliases for editable source.
        return tuple(
            path
            for path in self._read_only_path_cache[commit]
            if not any(
                path == root or path.startswith(root + "/")
                for root in request.repository_map.editable_roots
            )
        )

    def read_read_only(
        self,
        *,
        request: StructuredProposalRequest,
        path: str,
        offset: int = 0,
        count: int = 120,
        max_bytes: int = 4096,
    ) -> dict[str, object]:
        """Page allowlisted immutable source or immediate directory children.

        Natural paths are read addresses only, never editing refs. File cursors
        count lines; directory cursors count sorted immediate entries.
        """
        path = self._read_only_path(path)
        for value, name, lower, upper in (
            (offset, "offset", 0, 1_000_000_000),
            (count, "count", 1, 256),
            (max_bytes, "max_bytes", 512, 16_384),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not lower <= value <= upper
            ):
                raise ValueError(f"read-only {name} must be in [{lower}, {upper}]")
        if not any(
            path == root or path.startswith(root + "/") for root in self.read_only_roots
        ):
            raise ValueError("source path is outside read_only_roots")
        paths = self._read_only_paths(request)
        is_file = path in paths
        if is_file:
            values = self._source_lines(request.parent_selection.commit_sha, path)
        else:
            children = {}
            for candidate in paths:
                if candidate.startswith(path + "/"):
                    child, separator, _rest = candidate[len(path) + 1 :].partition("/")
                    children[path + "/" + child] = "directory" if separator else "file"
            if not children:
                raise ValueError("read-only path is absent or not regular source")
            values = tuple(
                {"path": child, "kind": kind, "read_only": True}
                for child, kind in sorted(children.items())
            )
        if offset >= len(values) and not (offset == 0 and not values):
            raise ValueError("read-only offset is outside the available content")
        key = "content" if is_file else "entries"
        result = {
            "read_only": True,
            "path": path,
            "kind": "file" if is_file else "directory",
            "unit": "line" if is_file else "entry",
            "start": offset,
            "end": offset,
            "total": len(values),
            "next_cursor": offset if offset < len(values) else None,
            key: "" if is_file else [],
        }
        if len(canonical_json(result).encode("utf-8")) > max_bytes:
            raise ValueError("read-only byte budget cannot hold its coordinates")
        for end in range(offset + 1, min(offset + count, len(values)) + 1):
            proposed = {
                **result,
                "end": end,
                "next_cursor": end if end < len(values) else None,
                key: "\n".join(values[offset:end])
                if is_file
                else list(values[offset:end]),
            }
            if len(canonical_json(proposed).encode("utf-8")) > max_bytes:
                break
            result = proposed
        if result["end"] == offset and offset < len(values):
            raise ValueError("read-only byte budget cannot hold the next item")
        return result

    def _visible_source_paths(
        self, request: StructuredProposalRequest
    ) -> dict[str, bool]:
        """Natural read addresses, with edit authority kept in the original map."""
        if request.repository_map.commit_sha != request.parent_selection.commit_sha:
            raise ValueError("repository map belongs to another selected parent")
        paths = {path: True for path in self._read_only_paths(request)}
        for item in request.repository_map.files:
            try:
                self._read_only_path(item.path)
            except ValueError:
                continue
            paths[item.path] = False
        return dict(sorted(paths.items()))

    @staticmethod
    def _bounded_integer(value: int, name: str, lower: int, upper: int) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not lower <= value <= upper
        ):
            raise ValueError(f"source {name} must be in [{lower}, {upper}]")

    def read_path(
        self,
        *,
        request: StructuredProposalRequest,
        path: str,
        line: int = 1,
        count: int = 120,
        max_bytes: int = 8192,
    ) -> dict[str, object]:
        """Read a visible immutable file without a pre-issued symbol address.

        ``line`` and ``next_line`` are one-based file line numbers. ``start`` /
        ``end`` retain the zero-based half-open coordinates used for source
        evidence. Only whole lines are returned; truncation never silently
        clips the middle of a source line.
        """
        path = self._read_only_path(path)
        self._bounded_integer(line, "line", 1, 1_000_000_000)
        self._bounded_integer(count, "count", 1, 256)
        self._bounded_integer(max_bytes, "max_bytes", 512, 16_384)
        visible = self._visible_source_paths(request)
        if path not in visible:
            raise ValueError("source path is outside the visible immutable files")
        lines = self._source_lines(request.parent_selection.commit_sha, path)
        offset = line - 1
        if offset >= len(lines) and not (offset == 0 and not lines):
            raise ValueError("source line is outside the available content")

        def page(end: int) -> dict[str, object]:
            return {
                "parent_commit": request.parent_selection.commit_sha,
                "path": path,
                "read_only": visible[path],
                "kind": "file",
                "unit": "line",
                "start": offset,
                "end": end,
                "total": len(lines),
                "line_start": line,
                "line_end": end,
                "next_line": end + 1 if end < len(lines) else None,
                "truncated": end < len(lines),
                "content": "\n".join(lines[offset:end]),
            }

        result = page(offset)
        if len(canonical_json(result).encode("utf-8")) > max_bytes:
            raise ValueError("source byte budget cannot hold its coordinates")
        for end in range(offset + 1, min(offset + count, len(lines)) + 1):
            candidate = page(end)
            if len(canonical_json(candidate).encode("utf-8")) > max_bytes:
                break
            result = candidate
        if result["end"] == offset and offset < len(lines):
            raise ValueError("source byte budget cannot hold the next whole line")
        return result

    def search(
        self,
        *,
        request: StructuredProposalRequest,
        query: str,
        path: str | None = None,
        cursor: dict[str, object] | None = None,
        max_results: int = 20,
        max_bytes: int = 8192,
    ) -> dict[str, object]:
        """Find literal, case-sensitive text in visible parent files.

        Results are navigation previews, not complete source evidence. Resume
        with ``next_cursor`` and the same query/path. A page scans at most 32
        files and 20,000 lines, so an absent match on a partial page does not
        establish absence. Invalid UTF-8 files are counted and skipped.
        """
        if (
            not isinstance(query, str)
            or not query
            or any(character in query for character in ("\n", "\r", "\x00"))
        ):
            raise ValueError("source query must be one nonempty literal line")
        try:
            query_bytes = len(query.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("source query must be UTF-8") from exc
        if query_bytes > 256:
            raise ValueError("source query exceeds 256 UTF-8 bytes")
        self._bounded_integer(max_results, "max_results", 1, 100)
        self._bounded_integer(max_bytes, "max_bytes", 512, 16_384)
        visible = self._visible_source_paths(request)
        if path is not None:
            path = self._read_only_path(path)
        paths = tuple(
            item
            for item in visible
            if path is None or item == path or item.startswith(path + "/")
        )
        if path is not None and not paths:
            raise ValueError("search path contains no visible immutable files")
        position, offset = 0, 0
        if cursor is not None:
            if not isinstance(cursor, dict) or set(cursor) != {"path", "line"}:
                raise ValueError("search cursor requires path and one-based line")
            if not isinstance(cursor["path"], str) or cursor["path"] not in paths:
                raise ValueError("search cursor path is outside this search scope")
            self._bounded_integer(cursor["line"], "cursor line", 1, 1_000_000_000)
            position, offset = paths.index(cursor["path"]), cursor["line"] - 1
        result = {
            "parent_commit": request.parent_selection.commit_sha,
            "query": query,
            "path": path,
            "matches": [],
            "next_cursor": None,
            "truncated": False,
            "scanned_files": 0,
            "scanned_lines": 0,
            "skipped_non_utf8_files": 0,
        }
        matches = result["matches"]
        while position < len(paths):
            current = paths[position]
            if result["scanned_files"] >= 32 or result["scanned_lines"] >= 20_000:
                result["next_cursor"] = {"path": current, "line": offset + 1}
                break
            try:
                lines = self._source_lines(request.parent_selection.commit_sha, current)
            except ValueError as exc:
                if str(exc) != "source is not UTF-8":
                    raise
                result["skipped_non_utf8_files"] += 1
                lines = ()
            result["scanned_files"] += 1
            if offset >= len(lines) and not (offset == 0 and not lines):
                raise ValueError("search cursor line is outside the source file")
            while offset < len(lines):
                if result["scanned_lines"] >= 20_000 or len(matches) >= max_results:
                    result["next_cursor"] = {"path": current, "line": offset + 1}
                    break
                text = lines[offset]
                found = text.find(query)
                result["scanned_lines"] += 1
                if found >= 0:
                    start = max(0, found - 80)
                    excerpt = text[start : max(found + len(query), start + 320)]
                    matches.append(
                        {
                            "path": current,
                            "line": offset + 1,
                            "read_only": visible[current],
                            "text": excerpt,
                            "text_truncated": excerpt != text,
                        }
                    )
                offset += 1
            if result["next_cursor"] is not None:
                break
            position += 1
            offset = 0
            if len(matches) >= max_results and position < len(paths):
                result["next_cursor"] = {"path": paths[position], "line": 1}
                break
        result["truncated"] = result["next_cursor"] is not None
        while len(canonical_json(result).encode("utf-8")) > max_bytes:
            if len(matches) > 1:
                removed = matches.pop()
                result["next_cursor"] = {
                    "path": removed["path"],
                    "line": removed["line"],
                }
                result["truncated"] = True
            elif matches and matches[0]["text"] != query:
                matches[0]["text"] = query
                matches[0]["text_truncated"] = True
            else:
                raise ValueError("search byte budget cannot hold the next result")
        return result

    def read_only_context(
        self, *, request: StructuredProposalRequest, max_bytes: int = 1500
    ) -> dict[str, object]:
        """Offer bounded dependency paths without adding source to the edit map."""
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 0
        ):
            raise ValueError("read-only context max_bytes must be non-negative")
        paths = self._read_only_paths(request)
        if not paths or not max_bytes:
            return {}
        roots = [
            root
            for root in self.read_only_roots
            if any(path == root or path.startswith(root + "/") for path in paths)
        ]
        imports = self._read_only_imports(request, paths)
        result = {
            "read_only": True,
            "roots": [],
            "imports": [],
            "omitted_root_count": len(roots),
            "omitted_import_count": len(imports),
            "note": "Read-only dependency source. Read these paths to inspect files or directories; they grant no edit or new-path authority. Import matches are syntactic navigation, not runtime resolution.",
        }
        if len(canonical_json(result).encode("utf-8")) > max_bytes:
            return {}
        for key, entries, omitted in (
            ("roots", roots, "omitted_root_count"),
            ("imports", imports, "omitted_import_count"),
        ):
            for entry in entries:
                proposed = {
                    **result,
                    key: [*result[key], entry],
                    omitted: result[omitted] - 1,
                }
                if len(canonical_json(proposed).encode("utf-8")) > max_bytes:
                    break
                result = proposed
        return result

    def _read_only_imports(
        self, request: StructuredProposalRequest, paths: tuple[str, ...]
    ) -> list[dict[str, object]]:
        files = self._entrypoint_files(request)
        # Dependencies may be imported by any current-parent component, including
        # newly split modules. Do not pin discovery to a historical entry filename.
        selected = tuple(dict.fromkeys(path for path, _ in self.entrypoint_symbols)) or self.entrypoint_paths or tuple(files)
        imports = []
        seen = set()
        for source_path in selected:
            if not source_path.endswith(".py"):
                continue
            try:
                module = ast.parse("\n".join(self._file_lines(request, source_path)))
            except (SyntaxError, ValueError):
                continue
            for node in module.body:
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif (
                    isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                ):
                    modules = [
                        node.module,
                        *(node.module + "." + alias.name for alias in node.names),
                    ]
                else:
                    continue
                for name in modules:
                    suffixes = (
                        name.replace(".", "/") + ".py",
                        name.replace(".", "/") + "/__init__.py",
                    )
                    for path in paths:
                        if path in seen or not any(
                            path == suffix or path.endswith("/" + suffix)
                            for suffix in suffixes
                        ):
                            continue
                        seen.add(path)
                        imports.append(
                            {
                                "path": path,
                                "kind": "file",
                                "read_only": True,
                                "from_path": source_path,
                                "line": node.lineno,
                            }
                        )
        return imports

    def _source_lines(self, commit_sha: str, path: str) -> tuple[str, ...]:
        # Only source is cached. Session-local aliases are generated afresh.
        key = (commit_sha, path)
        if key in self._source_cache:
            return self._source_cache[key]
        payload = self.reader.read_regular_blob(commit_sha, path)
        if payload is None:
            raise ValueError("source disappeared from the immutable parent")
        try:
            lines = tuple(payload.decode("utf-8").splitlines())
        except UnicodeDecodeError as exc:
            raise ValueError("source is not UTF-8") from exc
        if len(self._source_cache) >= 64:
            self._source_cache.pop(next(iter(self._source_cache)))
        self._source_cache[key] = lines
        return lines

    def _file_lines(
        self, request: StructuredProposalRequest, path: str
    ) -> tuple[str, ...]:
        if request.repository_map.commit_sha != request.parent_selection.commit_sha:
            raise ValueError("repository map belongs to another selected parent")
        if path not in {item.path for item in request.repository_map.files}:
            raise ValueError("source path is outside the immutable repository map")
        return self._source_lines(request.parent_selection.commit_sha, path)

    @staticmethod
    def _page(
        *, ref: str, ref_kind: str, lines: tuple[str, ...], offset: int, count: int
    ) -> ReadResult:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("source offset must be a non-negative integer")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("source count must be a positive integer")
        if offset >= len(lines) and not (offset == 0 and not lines):
            raise ValueError("source offset is outside the available lines")
        end = min(offset + count, len(lines))
        return ReadResult(
            ref=ref,
            ref_kind=ref_kind,
            start=offset,
            end=end,
            total=len(lines),
            content="\n".join(lines[offset:end]),
        )

    def read_file(
        self,
        *,
        request: StructuredProposalRequest,
        ref: str,
        path: str,
        offset: int = 0,
        count: int = 120,
    ) -> ReadResult:
        """Read any inventoried UTF-8 file, including templates and imports."""
        return self._page(
            ref=ref,
            ref_kind="source",
            lines=self._file_lines(request, path),
            offset=offset,
            count=count,
        )

    def read_definition(
        self,
        *,
        request: StructuredProposalRequest,
        target: DirectionTargetAlias,
        offset: int,
        count: int,
    ) -> ReadResult:
        file_lines = self._file_lines(request, target.file)
        first, last = target.lines
        file_map = next(
            item for item in request.repository_map.files if item.path == target.file
        )
        if not any(
            item.name == target.symbol
            and (item.first_line, item.last_line) == target.lines
            for item in file_map.symbols
        ):
            raise ValueError("definition is absent from the immutable repository map")
        if first < 1 or last < first or last > len(file_lines):
            raise ValueError("definition line coordinates differ from the source blob")
        return self._page(
            ref=target.ref,
            ref_kind="target",
            lines=file_lines[first - 1 : last],
            offset=offset,
            count=count,
        )

    def read_navigation(
        self,
        *,
        request: StructuredProposalRequest,
        ref: str,
        offset: int = 0,
        count: int = 8,
        max_bytes: int = 1500,
    ) -> dict[str, object]:
        """Return bounded navigation metadata, never inferred runtime call edges.

        P lists immediate files/packages, F lists definitions in source order,
        and T lists calls written directly in that definition. A declaration_ref
        is a same-file name lookup, not a claim about dynamic dispatch. Reading
        the referenced source still uses read_file/read_definition.
        """
        for value, name, lower, upper in (
            (offset, "offset", 0, 1_000_000_000),
            (count, "count", 1, 64),
            (max_bytes, "max_bytes", 512, 16_384),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not lower <= value <= upper
            ):
                raise ValueError(f"navigation {name} must be in [{lower}, {upper}]")
        if request.repository_map.commit_sha != request.parent_selection.commit_sha:
            raise ValueError("repository map belongs to another selected parent")
        catalog = build_direction_alias_catalog(request)
        scope = catalog.scope(ref)
        kind, path = scope.target.kind, scope.target.path
        entries: list[dict[str, object]] = []
        note = "Navigation metadata only; read source to inspect behavior."
        if kind is ScopeKind.PACKAGE:
            entries = [
                {
                    "ref": item.ref,
                    "path": item.target.path,
                    "kind": item.target.kind.value,
                }
                for item in catalog.package_entries(ref)
            ]
        elif kind is ScopeKind.FILE:
            entries = [
                {
                    "ref": item.ref,
                    "symbol": item.symbol,
                    "kind": item.kind,
                    "line": item.lines[0],
                    "end_line": item.lines[1],
                }
                for item in sorted(
                    catalog.file_targets(ref),
                    key=lambda item: (item.lines, item.symbol),
                )
            ]
        else:
            target = catalog.definition(ref)
            # Validate the frozen symbol coordinates before constructing a local
            # AST index. No filesystem or import execution is involved.
            self.read_definition(request=request, target=target, offset=0, count=1)
            source = "\n".join(self._file_lines(request, path))
            module = ast.parse(source)
            node = next(
                (
                    item
                    for item in ast.walk(module)
                    if isinstance(
                        item,
                        (
                            ast.FunctionDef,
                            ast.AsyncFunctionDef,
                            ast.ClassDef,
                            ast.Assign,
                            ast.AnnAssign,
                        ),
                    )
                    and item.lineno == target.lines[0]
                    and item.end_lineno == target.lines[1]
                ),
                None,
            )
            if node is None:
                raise ValueError("navigation definition differs from the immutable AST")
            declarations = {
                item.symbol: item for item in catalog.targets if item.file == path
            }
            calls: list[ast.Call] = []
            local_names = (
                {
                    argument.arg
                    for argument in ast.walk(node.args)
                    if isinstance(argument, ast.arg)
                }
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                else set()
            )

            def collect(current: ast.AST) -> None:
                # Calls in a nested function/method are not calls by its parent.
                if current is not node and isinstance(
                    current,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
                ):
                    if isinstance(
                        current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ):
                        local_names.add(current.name)
                    return
                if isinstance(current, ast.Name) and isinstance(current.ctx, ast.Store):
                    local_names.add(current.id)
                elif isinstance(current, (ast.Import, ast.ImportFrom)):
                    local_names.update(
                        alias.asname or alias.name.split(".")[0]
                        for alias in current.names
                    )
                if isinstance(current, ast.Call):
                    calls.append(current)
                for child in ast.iter_child_nodes(current):
                    collect(child)

            for statement in (
                node.body
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                )
                else (node,)
            ):
                collect(statement)
            member_class = target.symbol.rpartition(".")[0]
            if (
                member_class not in declarations
                or declarations[member_class].kind != "class"
            ):
                member_class = ""
            for call in sorted(calls, key=lambda item: (item.lineno, item.col_offset)):
                expression = ast.get_source_segment(source, call.func) or ast.unparse(
                    call.func
                )
                row: dict[str, object] = {
                    "expression": expression[:240],
                    "line": call.lineno,
                    "end_line": call.end_lineno,
                }
                if len(expression) > 240:
                    row["expression_truncated"] = True
                declaration = None
                if isinstance(call.func, ast.Name) and call.func.id not in local_names:
                    declaration = declarations.get(call.func.id)
                elif (
                    member_class
                    and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id in {"self", "cls"}
                ):
                    declaration = declarations.get(f"{member_class}.{call.func.attr}")
                if declaration is not None:
                    row["declaration_ref"] = declaration.ref
                entries.append(row)
            entries.sort(key=lambda row: "declaration_ref" not in row)
            note = (
                "Syntactic calls; same-file declaration links first, then source order, "
                "not importance or execution order. Links do not resolve dynamic dispatch. "
                "Nested bodies excluded."
            )
        if offset >= len(entries) and not (offset == 0 and not entries):
            raise ValueError("navigation offset is outside the available entries")
        result: dict[str, object] = {
            "ref": ref,
            "path": path,
            "kind": kind.value,
            "entries": [],
            "start": offset,
            "end": offset,
            "total": len(entries),
            "next_cursor": offset if offset < len(entries) else None,
            "note": note,
        }
        if len(canonical_json(result).encode("utf-8")) > max_bytes:
            raise ValueError("navigation byte budget cannot hold its coordinates")
        for entry in entries[offset : offset + count]:
            proposed = {
                **result,
                "entries": [*result["entries"], entry],
                "end": result["end"] + 1,
                "next_cursor": result["end"] + 1
                if result["end"] + 1 < len(entries)
                else None,
            }
            if len(canonical_json(proposed).encode("utf-8")) > max_bytes:
                break
            result = proposed
        if not result["entries"] and offset < len(entries):
            raise ValueError("navigation byte budget cannot hold the next entry")
        return result

    def _execution_functions(self, request: StructuredProposalRequest):
        catalog = build_direction_alias_catalog(request)
        modules = {}
        functions = []
        for name in _EXECUTION_METHODS:
            matches = sorted(
                (
                    item
                    for item in catalog.targets
                    if item.symbol == f"Terminus2.{name}"
                ),
                key=lambda item: (not item.file.endswith("/terminus_2.py"), item.file),
            )
            if not matches:
                continue
            target = matches[0]
            self.read_definition(request=request, target=target, offset=0, count=1)
            lines = self._file_lines(request, target.file)
            if target.file not in modules:
                modules[target.file] = ast.parse("\n".join(lines))
            node = next(
                (
                    item
                    for item in ast.walk(modules[target.file])
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.lineno == target.lines[0]
                    and item.end_lineno == target.lines[1]
                ),
                None,
            )
            if node is None:
                raise ValueError(
                    "execution entry differs from the immutable definition"
                )
            functions.append((target, node, lines))
        return functions

    def execution_source_context(
        self, *, request: StructuredProposalRequest, max_bytes: int = 8000
    ) -> tuple[InitialSourceBlock, ...]:
        """Show actual argument/result flow before imports or prompt templates.

        This is a source projection of existing Terminus entrypoints, not a
        dynamic execution trace or an editing whitelist. Separate blocks retain
        exact definition-relative coordinates so omitted source remains readable.
        """
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 0
        ):
            raise ValueError("execution source max_bytes must be non-negative")
        if not max_bytes:
            return ()
        if self.entrypoint_paths:
            return self.repository_source_context(request=request, max_bytes=max_bytes)
        functions = self._execution_functions(request)
        if not functions:
            return self.repository_source_context(request=request, max_bytes=max_bytes)
        queues = []
        for target, node, lines in functions:
            first, last = target.lines
            body = lines[first - 1 : last]
            if len(body) <= 64 and len("\n".join(body).encode()) <= min(
                4096, max_bytes // len(functions)
            ):
                ranges = [(first - 1, last)]
            else:
                # The signature itself can precede a large docstring/comment.
                ranges = [(first - 1, min(node.body[0].lineno - 1, first + 12))]
                parents = {
                    child: parent
                    for parent in ast.walk(node)
                    for child in ast.iter_child_nodes(parent)
                }
                deferred = (
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                    ast.Lambda,
                )

                def local_nodes(current, root=node, deferred_types=deferred):
                    if current is not root and isinstance(current, deferred_types):
                        return
                    yield current
                    for child in ast.iter_child_nodes(current):
                        yield from local_nodes(child)

                for item in local_nodes(node):
                    selected = False
                    if isinstance(item, ast.Call) and isinstance(
                        item.func, ast.Attribute
                    ):
                        selected = item.func.attr in _EXECUTION_CALLS or (
                            item.func.attr == "format"
                            and isinstance(item.func.value, ast.Attribute)
                            and item.func.value.attr == "_prompt_template"
                        )
                    elif isinstance(item, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                        targets = (
                            item.targets
                            if isinstance(item, ast.Assign)
                            else (item.target,)
                        )
                        selected = any(
                            isinstance(value, ast.Name)
                            and value.id
                            in {
                                "prompt",
                                "observation",
                                "commands",
                                "is_task_complete",
                                "was_pending_completion",
                            }
                            or isinstance(value, ast.Attribute)
                            and value.attr
                            in {
                                "_chat",
                                "_context",
                                "_pending_completion",
                                "_n_episodes",
                            }
                            for target_node in targets
                            for value in ast.walk(target_node)
                        )
                    elif isinstance(item, ast.Return):
                        selected = True
                    elif isinstance(item, ast.If) and any(
                        isinstance(value, ast.Name) and value.id == "is_task_complete"
                        for value in ast.walk(item.test)
                    ):
                        selected = any(
                            isinstance(value, ast.Return)
                            or isinstance(value, ast.Attribute)
                            and value.attr == "_pending_completion"
                            for value in ast.walk(item)
                        )
                    if not selected:
                        continue
                    statement = item
                    while not isinstance(statement, ast.stmt):
                        statement = parents[statement]
                    ranges.append((statement.lineno - 1, statement.end_lineno))
                    child = statement
                    parent = parents.get(child)
                    while parent is not None and parent is not node:
                        if isinstance(
                            parent, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try)
                        ):
                            ranges.append(
                                (parent.lineno - 1, parent.body[0].lineno - 1)
                            )
                            if isinstance(parent, ast.If) and child in parent.orelse:
                                ranges.append(
                                    (
                                        parent.body[-1].end_lineno,
                                        parent.orelse[0].lineno - 1,
                                    )
                                )
                        child, parent = parent, parents.get(parent)
                merged = []
                for start, end in sorted(ranges):
                    if start >= end:
                        continue
                    if merged and start <= merged[-1][1] + 1:
                        merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                    else:
                        merged.append((start, end))
                ranges = merged
            queues.append(
                (target, [(start - first + 1, end - start) for start, end in ranges])
            )

        blocks = []
        used = 0
        # Round-robin prevents one long loop from hiding the parser/executor.
        while any(queue for _target, queue in queues) and used < max_bytes:
            for target, queue in queues:
                if not queue or used >= max_bytes:
                    continue
                offset, count = queue.pop(0)
                result = self.read_definition(
                    request=request, target=target, offset=offset, count=min(count, 256)
                )
                selected, size = [], 0
                for line in result.content.splitlines():
                    addition = len(line.encode()) + bool(selected)
                    if size + addition > min(4096, max_bytes - used):
                        break
                    selected.append(line)
                    size += addition
                if not selected:
                    continue
                result = ReadResult(
                    result.ref,
                    result.ref_kind,
                    result.start,
                    result.start + len(selected),
                    result.total,
                    "\n".join(selected),
                )
                blocks.append(InitialSourceBlock(target.file, target.symbol, result))
                used += size
                consumed = result.end - offset
                if consumed < count:
                    queue.insert(0, (result.end, count - consumed))
        order = {
            f"Terminus2.{name}": index for index, name in enumerate(_EXECUTION_METHODS)
        }
        return tuple(
            sorted(blocks, key=lambda block: (order[block.symbol], block.result.start))
        )

    def execution_navigation(
        self, *, request: StructuredProposalRequest, max_bytes: int = 1500
    ) -> dict[str, object]:
        """Link real callsites between execution entrypoints and their helpers."""
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 512
        ):
            raise ValueError("execution navigation needs at least 512 bytes")
        if self.entrypoint_paths:
            files = self._entrypoint_files(request)
            result = {
                "path": self.entrypoint_paths[0] if len(self.entrypoint_paths) == 1 else None,
                "entry_refs": [files[path].ref for path in self.entrypoint_paths],
                "calls": [],
                "omitted_call_count": 0,
                "note": "Configured immutable file addresses for source navigation. Runtime selection and call edges are not resolved; these hints do not change the launcher.",
            }
            if len(canonical_json(result).encode()) > max_bytes:
                raise ValueError("execution navigation cannot fit its entry references")
            return result
        functions = self._execution_functions(request)
        entries = [target.ref for target, _node, _lines in functions]
        calls = []
        for target, _node, _lines in functions:
            navigation = self.read_navigation(
                request=request, ref=target.ref, count=64, max_bytes=16_384
            )
            for row in navigation["entries"]:
                expression = row["expression"]
                if (
                    expression.rsplit(".", 1)[-1] in _EXECUTION_CALLS
                    or expression == "self._prompt_template.format"
                ):
                    calls.append({"caller_ref": target.ref, "path": target.file, **row})
        core_refs = set(entries)
        calls.sort(
            key=lambda row: (
                0
                if row.get("declaration_ref") in core_refs
                else 1
                if row["expression"].rsplit(".", 1)[-1]
                in {"_query_llm", "parse_response", "send_keys"}
                else 2
            )
        )
        result = {
            "path": functions[0][0].file if functions else None,
            "entry_refs": entries,
            "calls": [],
            "omitted_call_count": len(calls),
            "note": "Syntactic callsites in the immutable parent. Declaration links are navigation, not dynamic execution proof; read source for conditions and data flow.",
        }
        if len(canonical_json(result).encode()) > max_bytes:
            raise ValueError("execution navigation cannot fit its entry references")
        for row in calls:
            candidate = {
                **result,
                "calls": [*result["calls"], row],
                "omitted_call_count": result["omitted_call_count"] - 1,
            }
            if len(canonical_json(candidate).encode()) > max_bytes:
                break
            result = candidate
        return result

    def program_flow_context(self, *, request: StructuredProposalRequest, max_bytes: int = 16384, max_components: int = 16):
        from .program_flow import program_flow_context
        return program_flow_context(self, request, max_bytes=max_bytes, max_components=max_components)

    def call_boundaries(self, *, request, path, line=1, count=200, max_bytes=2048):
        from .call_boundaries import call_boundaries
        return call_boundaries(self, request, path, line, count, max_bytes)

    def _entrypoint_files(self, request: StructuredProposalRequest):
        if request.repository_map.commit_sha != request.parent_selection.commit_sha:
            raise ValueError("repository map belongs to another selected parent")
        files = {
            item.target.path: item
            for item in build_direction_alias_catalog(request).scopes
            if item.target.kind is ScopeKind.FILE
        }
        if any(path not in files for path in self.entrypoint_paths):
            raise ValueError("entrypoint path is outside the immutable repository map")
        return files

    def repository_source_context(
        self, *, request: StructuredProposalRequest, max_bytes: int = 8000
    ) -> tuple[InitialSourceBlock, ...]:
        """Offer verbatim entry files for an unfamiliar harness, in any language.

        An adapter can supply known entrypoint paths. Otherwise ordinary project
        documentation and entry filenames are navigation leads, not assertions
        about which implementation executes. The remaining files stay readable.
        """
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
            raise ValueError("source max_bytes must be non-negative")
        files = self._entrypoint_files(request)
        if not max_bytes:
            return ()
        entry_names = {
            "__main__.py", "main.py", "agent.py", "main.ts", "index.ts",
            "main.js", "index.js", "main.go", "main.rs",
        }
        project_names = {"readme.md", "pyproject.toml", "package.json"}
        def priority(path):
            name = PurePosixPath(path).name
            return (
                0 if path in self.entrypoint_paths else
                1 if name.lower() in project_names else
                2 if name in entry_names else 3,
                len(PurePosixPath(path).parts), path,
            )

        blocks = []
        used = 0
        for path in sorted(files, key=priority):
            if used >= max_bytes or len(blocks) >= 4:
                break
            try:
                lines = self._file_lines(request, path)
            except ValueError as exc:
                if "UTF-8" in str(exc):
                    continue
                raise
            count = min(80, len(lines))
            allowance = min(2048, max_bytes - used)
            while count and len("\n".join(lines[:count]).encode()) > allowance:
                count -= 1
            if not count:
                continue
            result = self._page(
                ref=files[path].ref, ref_kind="source", lines=lines, offset=0, count=count
            )
            used += len(result.content.encode())
            blocks.append(InitialSourceBlock(path=path, symbol=None, result=result))
        return tuple(blocks)

    def initial_source_context(
        self, *, request: StructuredProposalRequest, max_bytes: int = 12_000
    ) -> tuple[InitialSourceBlock, ...]:
        """Offer small, source-backed entry points into Terminus 2's loop.

        Long functions are represented by labelled source spans around real
        calls, not rewritten pseudocode. A template shown here is a candidate
        template from the parent; its runtime selection is not established.
        """
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 0
        ):
            raise ValueError("initial source max_bytes must be non-negative")
        catalog = build_direction_alias_catalog(request)
        files = {
            item.target.path: item
            for item in catalog.scopes
            if item.target.kind is ScopeKind.FILE
        }
        main = next(
            (path for path in sorted(files) if path.endswith("/terminus_2.py")), None
        )
        if max_bytes == 0:
            return ()
        if main is None or self.entrypoint_paths:
            return self.repository_source_context(request=request, max_bytes=max_bytes)
        lines = self._file_lines(request, main)
        try:
            module = ast.parse("\n".join(lines))
        except (SyntaxError, ValueError):
            return ()
        targets = {item.symbol: item for item in catalog.targets if item.file == main}
        planned: list[tuple[str, DirectionTargetAlias | None, int, int]] = []

        imports = [
            node
            for node in module.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        if imports:
            planned.append(
                (
                    main,
                    None,
                    imports[0].lineno - 1,
                    min(
                        (imports[-1].end_lineno or imports[-1].lineno)
                        - imports[0].lineno
                        + 1,
                        64,
                    ),
                )
            )

        # Include a real prompt even though it has no Python definition alias.
        template = next(
            (
                path
                for path in sorted(files)
                if path.endswith(
                    (
                        "/templates/terminus-json-plain.txt",
                        "/templates/terminus-xml-plain.txt",
                    )
                )
            ),
            None,
        )
        if template is not None:
            planned.append((template, None, 0, 256))

        calls = {
            "_run_agent_loop",
            "_handle_llm_interaction",
            "_execute_commands",
            "_check_proactive_summarization",
            "_get_completion_confirmation_message",
            "format",
            "chat",
        }
        for name in (
            "run",
            "_run_agent_loop",
            "_handle_llm_interaction",
            "_execute_commands",
            "_query_llm",
        ):
            target = targets.get(f"Terminus2.{name}")
            if target is None:
                continue
            first, last = target.lines
            if last - first < 65:
                planned.append((main, target, 0, last - first + 1))
                continue
            node = next(
                (
                    item
                    for item in ast.walk(module)
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.lineno == first
                ),
                None,
            )
            if node is None:
                continue
            spans = [(first - 1, min(first + 10, last))]
            for item in ast.walk(node):
                if (
                    isinstance(item, ast.Call)
                    and isinstance(item.func, ast.Attribute)
                    and item.func.attr in calls
                ):
                    spans.append(
                        (
                            max(first - 1, item.lineno - 5),
                            min(last, (item.end_lineno or item.lineno) + 8),
                        )
                    )
            if name == "_run_agent_loop":
                spans.append((max(first - 1, last - 18), last))
            merged: list[tuple[int, int]] = []
            for start, end in sorted(spans):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:
                    merged.append((start, end))
            for start, end in merged:
                planned.append((main, target, start - first + 1, min(end - start, 256)))

        blocks: list[InitialSourceBlock] = []
        used = 0
        for path, target, offset, count in planned:
            if used >= max_bytes:
                break
            result = (
                self.read_definition(
                    request=request, target=target, offset=offset, count=count
                )
                if target is not None
                else self.read_file(
                    request=request,
                    ref=files[path].ref,
                    path=path,
                    offset=offset,
                    count=count,
                )
            )
            # Respect supplemental-evidence limits without splitting a UTF-8
            # character or inventing an ellipsis inside verbatim source.
            remaining = min(4096, max_bytes - used)
            selected: list[str] = []
            size = 0
            for line in result.content.splitlines():
                addition = len(line.encode("utf-8")) + bool(selected)
                if size + addition > remaining:
                    break
                selected.append(line)
                size += addition
            if not selected:
                continue
            result = ReadResult(
                ref=result.ref,
                ref_kind=result.ref_kind,
                start=result.start,
                end=result.start + len(selected),
                total=result.total,
                content="\n".join(selected),
            )
            blocks.append(
                InitialSourceBlock(path, target.symbol if target else None, result)
            )
            used += size
        return tuple(blocks)


def definition_reader(action: ActionMapSettings) -> ImmutableGitDefinitionReader:
    candidate = action.candidate
    return ImmutableGitDefinitionReader(
        SubprocessImmutableGitObjectReader(
            ImmutableGitReaderConfig(
                repository=action.program_repository,
                git_executable=candidate.git_executable,
                timeout_seconds=candidate.git_timeout_seconds,
                max_output_bytes=candidate.max_git_output_bytes,
                max_blob_bytes=min(
                    candidate.max_file_bytes,
                    candidate.max_git_output_bytes,
                ),
            )
        ),
        entrypoint_paths=getattr(candidate, "entrypoint_paths", ()),
        entrypoint_symbols=getattr(candidate, "entrypoint_symbols", ()),
        read_only_roots=getattr(candidate, "read_only_roots", ()),
        max_read_only_files=getattr(candidate, "max_files", 4096),
    )


__all__ = [
    "ImmutableGitDefinitionReader",
    "InitialSourceBlock",
    "definition_reader",
    "repository_map_builder",
]
