"""Deterministic structural implementation-adherence verification.

This module deliberately answers a narrow question: did the immutable result
commit make the source-level changes declared by a :class:`PolicyAction`?  It
does not consume benchmark outcomes and it does not prove that the resulting
implementation has the intended semantics.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from agentpilot.domain import (
    ImplementationAdherence,
    ParentRepositoryMap,
    PolicyAction,
    RepositoryFileMap,
    RepositorySymbol,
)

_IMMUTABLE_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_GIT_OBJECT = re.compile(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SYMBOL = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_REGULAR_BLOB_MODES = {b"100644", b"100755"}


class GitObjectInspectionError(RuntimeError):
    """An immutable Git object could not be inspected safely."""


class GitObjectInspectionTimeout(GitObjectInspectionError):
    """A bounded Git object inspection exceeded its deadline."""


class GitObjectOutputLimitExceeded(GitObjectInspectionError):
    """A Git object inspection exceeded its combined output allowance."""


class StructuralAdherenceDiagnosticCode(str, Enum):
    """Machine-readable structural verification diagnostics."""

    ACTION_PARENT_MISMATCH = "action_parent_mismatch"
    GIT_INSPECTION_FAILED = "git_inspection_failed"
    TOO_MANY_CHANGED_FILES = "too_many_changed_files"
    NO_IN_SCOPE_CHANGE = "no_in_scope_change"
    SCOPE_ESCAPED = "scope_escaped"
    NEW_PATH_ALREADY_EXISTED = "new_path_already_existed"
    NEW_PATH_MISSING = "new_path_missing"
    NEW_PATH_SYNTAX_UNREADABLE = "new_path_syntax_unreadable"
    NEW_PATH_ONLY_CHANGE = "new_path_only_change"


@dataclass(frozen=True, slots=True)
class StructuralAdherenceDiagnostic:
    """One bounded, typed reason for a structural adherence decision."""

    code: StructuralAdherenceDiagnosticCode
    message: str
    path: str | None = None
    symbol: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, StructuralAdherenceDiagnosticCode):
            raise TypeError("code must be StructuralAdherenceDiagnosticCode")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty text")
        if len(self.message) > 1_024:
            raise ValueError("message exceeds its intrinsic bound")
        for name in ("path", "symbol"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be non-empty text or None")
            if value is not None and len(value) > 1_024:
                raise ValueError(f"{name} exceeds its intrinsic bound")


@dataclass(frozen=True, slots=True)
class StructuralImplementationAdherenceResult:
    """Bounded structural evidence over two immutable commit identities."""

    action_id: str
    parent_commit_sha: str
    result_commit_sha: str
    adherence: ImplementationAdherence
    changed_files: tuple[str, ...]
    total_changed_files: int
    changed_files_truncated: bool
    diagnostics: tuple[StructuralAdherenceDiagnostic, ...]
    total_diagnostics: int
    diagnostics_truncated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id:
            raise ValueError("action_id must be non-empty")
        for name in ("parent_commit_sha", "result_commit_sha"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty")
        if not isinstance(self.adherence, ImplementationAdherence):
            raise TypeError("adherence must be ImplementationAdherence")
        if not isinstance(self.changed_files, tuple) or any(
            not isinstance(path, str) for path in self.changed_files
        ):
            raise TypeError("changed_files must be a tuple of paths")
        if self.total_changed_files < len(self.changed_files):
            raise ValueError("total_changed_files is inconsistent")
        if self.changed_files_truncated != (
            self.total_changed_files > len(self.changed_files)
        ):
            raise ValueError("changed_files_truncated is inconsistent")
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, StructuralAdherenceDiagnostic)
            for item in self.diagnostics
        ):
            raise TypeError("diagnostics must contain typed diagnostics")
        if self.total_diagnostics < len(self.diagnostics):
            raise ValueError("total_diagnostics is inconsistent")
        if self.diagnostics_truncated != (
            self.total_diagnostics > len(self.diagnostics)
        ):
            raise ValueError("diagnostics_truncated is inconsistent")

    def as_worker_diagnostics(self) -> dict[str, object]:
        """Return a bounded JSON-compatible payload for ``WorkerResult``."""

        return {
            "kind": "structural_implementation_adherence",
            "claim_scope": "structural_only_not_semantic",
            "parent_commit_sha": self.parent_commit_sha,
            "result_commit_sha": self.result_commit_sha,
            "changed_files": list(self.changed_files),
            "total_changed_files": self.total_changed_files,
            "changed_files_truncated": self.changed_files_truncated,
            "diagnostics": [
                {
                    "code": item.code.value,
                    "message": item.message,
                    "path": item.path,
                    "symbol": item.symbol,
                }
                for item in self.diagnostics
            ],
            "total_diagnostics": self.total_diagnostics,
            "diagnostics_truncated": self.diagnostics_truncated,
        }


@dataclass(frozen=True, slots=True)
class ImmutableGitReaderConfig:
    """Trusted local limits for subprocess-backed Git object reads."""

    repository: Path
    git_executable: Path = Path("/usr/bin/git")
    timeout_seconds: float = 10.0
    max_output_bytes: int = 2 * 1024 * 1024
    max_blob_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        repository = Path(self.repository).resolve(strict=True)
        executable = Path(self.git_executable).resolve(strict=True)
        if not repository.is_dir():
            raise ValueError("repository must be a directory")
        if not executable.is_file():
            raise ValueError("git_executable must be a file")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0 < float(self.timeout_seconds) <= 300
        ):
            raise ValueError("timeout_seconds must be in (0, 300]")
        for name in ("max_output_bytes", "max_blob_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1_024:
                raise ValueError(f"{name} must be an integer of at least 1024")
        if self.max_blob_bytes > self.max_output_bytes:
            raise ValueError("max_blob_bytes cannot exceed max_output_bytes")
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "git_executable", executable)
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


class ImmutableGitObjectReader(Protocol):
    """Read-only connector whose answers are derived from Git object IDs."""

    def resolve_commit(self, commit_sha: str) -> str: ...

    def resolve_tree(self, commit_sha: str) -> str: ...

    def changed_paths(
        self, parent_commit_sha: str, result_commit_sha: str
    ) -> tuple[str, ...]: ...

    def read_regular_blob(self, commit_sha: str, path: str) -> bytes | None: ...

    def list_regular_paths(
        self, commit_sha: str, roots: tuple[str, ...]
    ) -> tuple[str, ...]: ...


class SubprocessImmutableGitObjectReader:
    """Bounded connector for immutable commits, trees, and blobs.

    It never reads candidate content from the checkout.  The repository path is
    used only as the object-database location for Git plumbing commands.
    """

    def __init__(self, config: ImmutableGitReaderConfig) -> None:
        if not isinstance(config, ImmutableGitReaderConfig):
            raise TypeError("config must be ImmutableGitReaderConfig")
        self.config = config

    def _run(self, arguments: list[str]) -> bytes:
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        overflow = threading.Event()
        budget_lock = threading.Lock()
        remaining_budget = [self.config.max_output_bytes]

        def read_bounded(stream: object, destination: bytearray) -> None:
            while True:
                chunk = stream.read(64 * 1024)  # type: ignore[attr-defined]
                if not chunk:
                    return
                with budget_lock:
                    accepted = min(len(chunk), remaining_budget[0])
                    if accepted:
                        destination.extend(chunk[:accepted])
                        remaining_budget[0] -= accepted
                    if accepted != len(chunk):
                        overflow.set()

        try:
            process = subprocess.Popen(
                [str(self.config.git_executable), *arguments],
                cwd=str(self.config.repository),
                env={
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_SYSTEM": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise GitObjectInspectionError("cannot start configured Git") from exc
        assert process.stdout is not None
        assert process.stderr is not None
        threads = (
            threading.Thread(
                target=read_bounded,
                args=(process.stdout, stdout_buffer),
                daemon=True,
            ),
            threading.Thread(
                target=read_bounded,
                args=(process.stderr, stderr_buffer),
                daemon=True,
            ),
        )
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + self.config.timeout_seconds
        timed_out = False
        try:
            while process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    process.kill()
                    break
                if overflow.is_set():
                    process.kill()
                    break
                time.sleep(min(0.01, remaining))
            process.wait()
            for thread in threads:
                thread.join(timeout=1.0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()
            for thread in threads:
                thread.join(timeout=1.0)
        if timed_out:
            raise GitObjectInspectionTimeout("configured Git command timed out")
        if overflow.is_set():
            raise GitObjectOutputLimitExceeded(
                "configured Git command exceeded output bound"
            )
        if process.returncode != 0:
            operation = arguments[0] if arguments else "command"
            raise GitObjectInspectionError(
                f"git {operation} failed with exit code {process.returncode}"
            )
        return bytes(stdout_buffer)

    @staticmethod
    def _require_immutable_commit(commit_sha: str) -> str:
        if not isinstance(commit_sha, str) or not _IMMUTABLE_COMMIT.fullmatch(
            commit_sha
        ):
            raise GitObjectInspectionError(
                "commit identity must be a full lowercase Git object id"
            )
        return commit_sha

    def resolve_commit(self, commit_sha: str) -> str:
        requested = self._require_immutable_commit(commit_sha)
        resolved = (
            self._run(["rev-parse", "--verify", f"{requested}^{{commit}}"])
            .decode("ascii", errors="strict")
            .strip()
        )
        if resolved != requested:
            raise GitObjectInspectionError("commit did not resolve exactly")
        return resolved

    def resolve_tree(self, commit_sha: str) -> str:
        requested = self._require_immutable_commit(commit_sha)
        resolved = (
            self._run(["rev-parse", "--verify", f"{requested}^{{tree}}"])
            .decode("ascii", errors="strict")
            .strip()
        )
        if not _GIT_OBJECT.fullmatch(resolved.encode("ascii")):
            raise GitObjectInspectionError("commit tree did not resolve exactly")
        return resolved

    def changed_paths(
        self, parent_commit_sha: str, result_commit_sha: str
    ) -> tuple[str, ...]:
        parent = self._require_immutable_commit(parent_commit_sha)
        result = self._require_immutable_commit(result_commit_sha)
        raw = self._run(
            [
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                "--no-renames",
                parent,
                result,
                "--",
            ]
        )
        try:
            paths = tuple(item.decode("utf-8") for item in raw.split(b"\0") if item)
        except UnicodeDecodeError as exc:
            raise GitObjectInspectionError("changed Git paths must be UTF-8") from exc
        if len(paths) != len(set(paths)) or any(not path for path in paths):
            raise GitObjectInspectionError("changed Git paths are invalid")
        return tuple(sorted(paths))

    def read_regular_blob(self, commit_sha: str, path: str) -> bytes | None:
        commit = self._require_immutable_commit(commit_sha)
        if not isinstance(path, str) or not path or "\x00" in path:
            raise GitObjectInspectionError("Git path is invalid")
        listing = self._run(["ls-tree", "-z", commit, "--", path])
        if not listing:
            return None
        records = tuple(record for record in listing.split(b"\0") if record)
        if len(records) != 1 or b"\t" not in records[0]:
            raise GitObjectInspectionError("Git path has an ambiguous tree entry")
        metadata, encoded_path = records[0].split(b"\t", 1)
        fields = metadata.split()
        try:
            listed_path = encoded_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitObjectInspectionError("Git path must be UTF-8") from exc
        if len(fields) != 3 or listed_path != path:
            raise GitObjectInspectionError("Git tree entry differs from requested path")
        mode, object_type, object_id = fields
        if object_type != b"blob" or mode not in _REGULAR_BLOB_MODES:
            raise GitObjectInspectionError("Git path is not a regular source blob")
        if not _GIT_OBJECT.fullmatch(object_id):
            raise GitObjectInspectionError("Git blob identity is invalid")
        size_raw = self._run(["cat-file", "-s", object_id.decode("ascii")])
        try:
            size = int(size_raw.strip())
        except ValueError as exc:
            raise GitObjectInspectionError("Git blob size is invalid") from exc
        if size < 0 or size > self.config.max_blob_bytes:
            raise GitObjectInspectionError("Git blob exceeds its byte bound")
        content = self._run(["cat-file", "blob", object_id.decode("ascii")])
        if len(content) != size:
            raise GitObjectInspectionError("Git blob size is inconsistent")
        return content

    def list_regular_paths(
        self, commit_sha: str, roots: tuple[str, ...]
    ) -> tuple[str, ...]:
        commit = self._require_immutable_commit(commit_sha)
        if (
            not isinstance(roots, tuple)
            or not roots
            or any(
                not isinstance(root, str) or not root or "\x00" in root
                for root in roots
            )
        ):
            raise GitObjectInspectionError("Git roots must be a non-empty tuple")
        raw = self._run(["ls-tree", "-r", "-z", commit, "--", *roots])
        paths: list[str] = []
        for record in (item for item in raw.split(b"\0") if item):
            if b"\t" not in record:
                raise GitObjectInspectionError("Git tree record is malformed")
            metadata, encoded_path = record.split(b"\t", 1)
            fields = metadata.split()
            if len(fields) != 3:
                raise GitObjectInspectionError("Git tree metadata is malformed")
            mode, object_type, object_id = fields
            if object_type != b"blob" or mode not in _REGULAR_BLOB_MODES:
                continue
            if not _GIT_OBJECT.fullmatch(object_id):
                raise GitObjectInspectionError("Git tree blob identity is invalid")
            try:
                path = encoded_path.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GitObjectInspectionError("Git path must be UTF-8") from exc
            paths.append(path)
        if len(paths) != len(set(paths)) or any(not path for path in paths):
            raise GitObjectInspectionError("Git tree paths are invalid")
        return tuple(sorted(paths))


@dataclass(frozen=True, slots=True)
class StructuralAdherenceConfig:
    """Policy-neutral structural checks and evidence-size bounds."""

    python_suffixes: tuple[str, ...] = (".py", ".pyi")
    candidate_source_suffixes: tuple[str, ...] = (".py", ".pyi")
    max_changed_files: int = 1_024
    max_diagnostics: int = 64
    max_diagnostic_chars: int = 256

    def __post_init__(self) -> None:
        for name in ("python_suffixes", "candidate_source_suffixes"):
            values = getattr(self, name)
            if (
                not isinstance(values, tuple)
                or not values
                or any(
                    not isinstance(value, str)
                    or not value.startswith(".")
                    or len(value) > 32
                    for value in values
                )
                or len(values) != len(set(values))
            ):
                raise ValueError(f"{name} must be unique dotted suffixes")
        for name, upper in (
            ("max_changed_files", 100_000),
            ("max_diagnostics", 1_024),
            ("max_diagnostic_chars", 1_024),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                or value > upper
            ):
                raise ValueError(f"{name} must be in [1, {upper}]")


class _DiagnosticCollector:
    def __init__(self, config: StructuralAdherenceConfig) -> None:
        self.config = config
        self.items: list[StructuralAdherenceDiagnostic] = []
        self.total = 0
        self.unknown = False
        self.deviated = False

    def add(
        self,
        code: StructuralAdherenceDiagnosticCode,
        message: str,
        *,
        path: str | None = None,
        symbol: str | None = None,
        unknown: bool = False,
        informational: bool = False,
    ) -> None:
        self.total += 1
        self.unknown |= unknown
        self.deviated |= not unknown and not informational
        if len(self.items) >= self.config.max_diagnostics:
            return

        def bounded(value: str | None) -> str | None:
            if value is None:
                return None
            return value[: self.config.max_diagnostic_chars]

        self.items.append(
            StructuralAdherenceDiagnostic(
                code=code,
                message=bounded(message) or "structural verification issue",
                path=bounded(path),
                symbol=bounded(symbol),
            )
        )

    @property
    def adherence(self) -> ImplementationAdherence:
        if self.unknown:
            return ImplementationAdherence.UNKNOWN
        if self.deviated:
            return ImplementationAdherence.DEVIATED
        return ImplementationAdherence.FOLLOWED


def _definition_nodes(body: list[ast.stmt], name: str) -> tuple[ast.AST, ...]:
    found: list[ast.AST] = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                found.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            if any(
                isinstance(target, ast.Name) and target.id == name for target in targets
            ):
                found.append(node)
    return tuple(found)


def python_symbol_fingerprint(module: ast.Module, symbol: str) -> tuple[str, ...]:
    current: tuple[ast.AST, ...] = (module,)
    for segment in symbol.split("."):
        next_nodes: list[ast.AST] = []
        for node in current:
            body = getattr(node, "body", None)
            if isinstance(body, list):
                next_nodes.extend(_definition_nodes(body, segment))
        current = tuple(next_nodes)
        if not current:
            return ()
    return tuple(
        ast.dump(node, annotate_fields=True, include_attributes=False)
        for node in current
    )


def python_qualified_symbols(module: ast.Module) -> tuple[str, ...]:
    """Enumerate every dotted definition address accepted by the verifier."""

    found: set[str] = set()

    def visit(body: list[ast.stmt], prefix: tuple[str, ...]) -> None:
        names: set[str] = set()
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else (node.target,)
                )
                names.update(
                    target.id for target in targets if isinstance(target, ast.Name)
                )
        for name in sorted(names):
            address = (*prefix, name)
            found.add(".".join(address))
            for node in _definition_nodes(body, name):
                nested = getattr(node, "body", None)
                if isinstance(nested, list):
                    visit(nested, address)

    visit(module.body, ())
    return tuple(sorted(found))


@dataclass(frozen=True)
class PythonDefinition:
    """One targetable definition with its hover-level detail."""

    name: str
    kind: str
    first_line: int
    last_line: int
    signature: str = ""
    summary: str = ""


def _one_line(value: str, limit: int) -> str:
    collapsed = " ".join(value.split())
    return collapsed[:limit].strip()


def _docstring_summary(node: ast.AST) -> str:
    try:
        text = ast.get_docstring(node)  # type: ignore[arg-type]
    except TypeError:
        return ""
    if not text:
        return ""
    for line in text.splitlines():
        if line.strip():
            return _one_line(line, 200)
    return ""


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    keyword = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    try:
        arguments = ast.unparse(node.args)
        returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    except (AttributeError, ValueError):
        arguments, returns = "...", ""
    return _one_line(f"{keyword} {node.name}({arguments}){returns}", 512)


def _class_signature(node: ast.ClassDef) -> str:
    try:
        bases = [ast.unparse(base) for base in node.bases]
        bases += [f"{kw.arg}={ast.unparse(kw.value)}" for kw in node.keywords if kw.arg]
    except (AttributeError, ValueError):
        bases = []
    joined = ", ".join(bases)
    return _one_line(
        f"class {node.name}({joined})" if joined else f"class {node.name}", 512
    )


def python_definition_outline(module: ast.Module) -> tuple[PythonDefinition, ...]:
    """Enumerate the definitions a proposer may target, with their detail.

    This is a strict subset of :func:`python_qualified_symbols`, so any address
    offered here stays resolvable by the post-edit verifier. Assignments made
    inside a function body are excluded on purpose: a local variable is not an
    edit target, and enumerating them left over half of every emitted map
    unusable while consuming half of its prompt budget.
    """

    found: dict[str, PythonDefinition] = {}

    def end_line(node: ast.stmt) -> int:
        return getattr(node, "end_lineno", None) or node.lineno

    def visit(body: list[ast.stmt], prefix: tuple[str, ...], in_function: bool) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                address = ".".join((*prefix, node.name))
                found.setdefault(
                    address,
                    PythonDefinition(
                        name=address,
                        kind="function",
                        first_line=node.lineno,
                        last_line=end_line(node),
                        signature=_function_signature(node),
                        summary=_docstring_summary(node),
                    ),
                )
                visit(node.body, (*prefix, node.name), True)
            elif isinstance(node, ast.ClassDef):
                address = ".".join((*prefix, node.name))
                found.setdefault(
                    address,
                    PythonDefinition(
                        name=address,
                        kind="class",
                        first_line=node.lineno,
                        last_line=end_line(node),
                        signature=_class_signature(node),
                        summary=_docstring_summary(node),
                    ),
                )
                visit(node.body, (*prefix, node.name), in_function)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and not in_function:
                targets = (
                    node.targets if isinstance(node, ast.Assign) else (node.target,)
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        address = ".".join((*prefix, target.id))
                        found.setdefault(
                            address,
                            PythonDefinition(
                                name=address,
                                kind="attribute",
                                first_line=node.lineno,
                                last_line=end_line(node),
                            ),
                        )

    visit(module.body, (), False)
    return tuple(found[name] for name in sorted(found))


class PythonParentRepositoryMapBuilder:
    """Build one bounded repo map from the same AST truth used after execution."""

    def __init__(
        self,
        reader: ImmutableGitObjectReader,
        *,
        editable_roots: tuple[str, ...],
        max_files: int,
        max_symbols_per_file: int = 4_096,
        max_total_symbols: int = 65_536,
    ) -> None:
        for name in (
            "resolve_commit",
            "resolve_tree",
            "read_regular_blob",
            "list_regular_paths",
        ):
            if not callable(getattr(reader, name, None)):
                raise TypeError(f"reader must provide {name}()")
        if not editable_roots or tuple(sorted(set(editable_roots))) != editable_roots:
            raise ValueError("editable_roots must be non-empty, sorted and unique")
        for name, value, upper in (
            ("max_files", max_files, 4_096),
            ("max_symbols_per_file", max_symbols_per_file, 4_096),
            ("max_total_symbols", max_total_symbols, 65_536),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= upper
            ):
                raise ValueError(f"{name} must be in [1, {upper}]")
        self.reader = reader
        self.editable_roots = editable_roots
        self.max_files = max_files
        self.max_symbols_per_file = max_symbols_per_file
        self.max_total_symbols = max_total_symbols

    def build(self, *, parent_id: str, commit_sha: str) -> ParentRepositoryMap:
        resolved = self.reader.resolve_commit(commit_sha)
        if resolved != commit_sha:
            raise GitObjectInspectionError(
                "repo map reader changed the commit identity"
            )
        paths = self.reader.list_regular_paths(resolved, self.editable_roots)
        if not paths or len(paths) > self.max_files:
            raise GitObjectInspectionError(
                "editable regular-file count exceeds map bounds"
            )
        files: list[RepositoryFileMap] = []
        total_symbols = 0
        for path in paths:
            source = self.reader.read_regular_blob(resolved, path)
            if source is None:
                raise GitObjectInspectionError("repo map regular blob disappeared")
            definitions: tuple[PythonDefinition, ...] = ()
            if path.endswith((".py", ".pyi")):
                try:
                    module = ast.parse(source, filename=path, type_comments=True)
                except (SyntaxError, ValueError, UnicodeError):
                    # The inventory remains complete even when a Python blob has
                    # no trustworthy definition outline. It is still selectable
                    # through its F-ref, but no T-ref is minted from guessed AST.
                    definitions = ()
                else:
                    definitions = python_definition_outline(module)
            if len(definitions) > self.max_symbols_per_file:
                raise GitObjectInspectionError("Python symbol count exceeds map bounds")
            total_symbols += len(definitions)
            if total_symbols > self.max_total_symbols:
                raise GitObjectInspectionError("repo map total symbol bound exceeded")
            files.append(
                RepositoryFileMap(
                    path=path,
                    byte_count=len(source),
                    symbols=tuple(
                        RepositorySymbol(
                            name=item.name,
                            kind=item.kind,
                            first_line=item.first_line,
                            last_line=item.last_line,
                            signature=item.signature,
                            summary=item.summary,
                        )
                        for item in definitions
                    ),
                )
            )
        return ParentRepositoryMap.create(
            parent_id=parent_id,
            commit_sha=resolved,
            tree_sha=self.reader.resolve_tree(resolved),
            editable_roots=self.editable_roots,
            files=tuple(files),
        )


class StructuralImplementationAdherenceVerifier:
    """Verify declared structural edits without observing evaluation outcomes."""

    def __init__(
        self,
        reader: ImmutableGitObjectReader,
        config: StructuralAdherenceConfig = StructuralAdherenceConfig(),
    ) -> None:
        for name in ("resolve_commit", "changed_paths", "read_regular_blob"):
            if not callable(getattr(reader, name, None)):
                raise TypeError(f"reader must provide {name}()")
        if not isinstance(config, StructuralAdherenceConfig):
            raise TypeError("config must be StructuralAdherenceConfig")
        self.reader = reader
        self.config = config

    def _result(
        self,
        *,
        action: PolicyAction,
        parent_commit_sha: str,
        result_commit_sha: str,
        changed_files: tuple[str, ...],
        diagnostics: _DiagnosticCollector,
    ) -> StructuralImplementationAdherenceResult:
        visible_changed = changed_files[: self.config.max_changed_files]
        return StructuralImplementationAdherenceResult(
            action_id=action.action_id,
            parent_commit_sha=parent_commit_sha,
            result_commit_sha=result_commit_sha,
            adherence=diagnostics.adherence,
            changed_files=visible_changed,
            total_changed_files=len(changed_files),
            changed_files_truncated=len(changed_files) > len(visible_changed),
            diagnostics=tuple(diagnostics.items),
            total_diagnostics=diagnostics.total,
            diagnostics_truncated=diagnostics.total > len(diagnostics.items),
        )

    def verify(
        self,
        action: PolicyAction,
        *,
        parent_commit_sha: str,
        result_commit_sha: str,
    ) -> StructuralImplementationAdherenceResult:
        if not isinstance(action, PolicyAction):
            raise TypeError("action must be an AgentPilot PolicyAction")
        for name, value in (
            ("parent_commit_sha", parent_commit_sha),
            ("result_commit_sha", result_commit_sha),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty text")
        diagnostics = _DiagnosticCollector(self.config)
        if action.parent_commit_sha != parent_commit_sha:
            diagnostics.add(
                StructuralAdherenceDiagnosticCode.ACTION_PARENT_MISMATCH,
                "action parent differs from the supplied immutable parent commit",
                unknown=True,
            )
            return self._result(
                action=action,
                parent_commit_sha=parent_commit_sha,
                result_commit_sha=result_commit_sha,
                changed_files=(),
                diagnostics=diagnostics,
            )

        try:
            parent = self.reader.resolve_commit(parent_commit_sha)
            result = self.reader.resolve_commit(result_commit_sha)
            changed_files = self.reader.changed_paths(parent, result)
        except (GitObjectInspectionError, UnicodeError, ValueError) as exc:
            diagnostics.add(
                StructuralAdherenceDiagnosticCode.GIT_INSPECTION_FAILED,
                f"immutable Git inspection failed: {type(exc).__name__}",
                unknown=True,
            )
            return self._result(
                action=action,
                parent_commit_sha=parent_commit_sha,
                result_commit_sha=result_commit_sha,
                changed_files=(),
                diagnostics=diagnostics,
            )

        if parent != parent_commit_sha or result != result_commit_sha:
            diagnostics.add(
                StructuralAdherenceDiagnosticCode.GIT_INSPECTION_FAILED,
                "Git connector did not preserve exact immutable commit identities",
                unknown=True,
            )
            return self._result(
                action=action,
                parent_commit_sha=parent_commit_sha,
                result_commit_sha=result_commit_sha,
                changed_files=changed_files,
                diagnostics=diagnostics,
            )
        if len(changed_files) > self.config.max_changed_files:
            diagnostics.add(
                StructuralAdherenceDiagnosticCode.TOO_MANY_CHANGED_FILES,
                "changed file count exceeds the structural evidence bound",
                unknown=True,
            )

        changed_set = set(changed_files)
        resolved_scope = set(action.direction.resolved_scope)
        inside = changed_set & resolved_scope
        if not inside:
            diagnostics.add(
                StructuralAdherenceDiagnosticCode.NO_IN_SCOPE_CHANGE,
                "candidate made no regular-file change inside the resolved scope",
            )
        for path in sorted(changed_set - resolved_scope):
            diagnostics.add(
                StructuralAdherenceDiagnosticCode.SCOPE_ESCAPED,
                "candidate changed a path outside the resolved direction scope",
                path=path,
            )

        new_paths = {item.path: item for item in action.direction.new_paths}
        parent_existing_changed = False
        for path in sorted(inside):
            if path in new_paths:
                continue
            try:
                if self.reader.read_regular_blob(parent, path) is not None:
                    parent_existing_changed = True
            except (GitObjectInspectionError, UnicodeError, ValueError) as exc:
                diagnostics.add(
                    StructuralAdherenceDiagnosticCode.GIT_INSPECTION_FAILED,
                    f"parent scope inspection failed: {type(exc).__name__}",
                    path=path,
                    unknown=True,
                )

        for path in sorted(new_paths):
            try:
                parent_source = self.reader.read_regular_blob(parent, path)
                result_source = self.reader.read_regular_blob(result, path)
            except (GitObjectInspectionError, UnicodeError, ValueError) as exc:
                diagnostics.add(
                    StructuralAdherenceDiagnosticCode.GIT_INSPECTION_FAILED,
                    f"new path object inspection failed: {type(exc).__name__}",
                    path=path,
                    unknown=True,
                )
                continue
            if parent_source is not None:
                diagnostics.add(
                    StructuralAdherenceDiagnosticCode.NEW_PATH_ALREADY_EXISTED,
                    "declared new path already existed in the immutable parent",
                    path=path,
                )
            if result_source is None:
                diagnostics.add(
                    StructuralAdherenceDiagnosticCode.NEW_PATH_MISSING,
                    "declared new path is absent or not a regular result file",
                    path=path,
                )
                continue
            if not path.endswith(self.config.candidate_source_suffixes):
                diagnostics.add(
                    StructuralAdherenceDiagnosticCode.NEW_PATH_MISSING,
                    "declared new path has a suffix outside the candidate policy",
                    path=path,
                )
                continue
            if path.endswith(self.config.python_suffixes):
                try:
                    ast.parse(result_source, filename=path, type_comments=True)
                except (SyntaxError, ValueError, UnicodeError) as exc:
                    diagnostics.add(
                        StructuralAdherenceDiagnosticCode.NEW_PATH_SYNTAX_UNREADABLE,
                        f"new Python path is not parseable: {type(exc).__name__}",
                        path=path,
                    )

        if new_paths and not parent_existing_changed:
            diagnostics.add(
                StructuralAdherenceDiagnosticCode.NEW_PATH_ONLY_CHANGE,
                "a direction with new paths must also change one pre-existing "
                "regular file inside the resolved scope",
            )

        return self._result(
            action=action,
            parent_commit_sha=parent,
            result_commit_sha=result,
            changed_files=changed_files,
            diagnostics=diagnostics,
        )


__all__ = [
    "GitObjectInspectionError",
    "GitObjectInspectionTimeout",
    "GitObjectOutputLimitExceeded",
    "ImmutableGitObjectReader",
    "ImmutableGitReaderConfig",
    "PythonDefinition",
    "PythonParentRepositoryMapBuilder",
    "StructuralAdherenceConfig",
    "StructuralAdherenceDiagnostic",
    "StructuralAdherenceDiagnosticCode",
    "StructuralImplementationAdherenceResult",
    "StructuralImplementationAdherenceVerifier",
    "SubprocessImmutableGitObjectReader",
    "python_definition_outline",
    "python_qualified_symbols",
    "python_symbol_fingerprint",
]
