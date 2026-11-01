"""Carrier-neutral search-state and parent-selection contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Mapping

from agentpilot.domain.common import deep_freeze, require_aware, require_text

_DOTTED_SYMBOL = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


def _repository_path(value: str, name: str) -> str:
    require_text(value, name)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{name} must be a safe relative POSIX path")
    return value


SYMBOL_KINDS = frozenset({"attribute", "class", "function"})


@dataclass(frozen=True)
class RepositorySymbol:
    """One targetable definition with the detail needed to reason about it.

    A bare dotted address tells a proposer where it may point but nothing about
    what lives there, so the signature, the first docstring line and the line
    span travel with the address. Attributes carry no signature.
    """

    name: str
    kind: str
    first_line: int
    last_line: int
    signature: str = ""
    summary: str = ""

    def __post_init__(self) -> None:
        require_text(self.name, "repository symbol name")
        if _DOTTED_SYMBOL.fullmatch(self.name) is None:
            raise ValueError("repository symbols must be dotted identifiers")
        if self.kind not in SYMBOL_KINDS:
            raise ValueError(f"repository symbol kind must be one of {sorted(SYMBOL_KINDS)}")
        for name in ("first_line", "last_line"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.last_line < self.first_line:
            raise ValueError("last_line must not precede first_line")
        for name, limit in (("signature", 512), ("summary", 200)):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"repository symbol {name} must be text")
            if len(value) > limit:
                raise ValueError(f"repository symbol {name} exceeds {limit} characters")
            if value.strip() != value or "\n" in value:
                raise ValueError(f"repository symbol {name} must be one trimmed line")
        if self.kind == "attribute" and self.signature:
            raise ValueError("an attribute symbol cannot carry a signature")

    def as_prompt_payload(self) -> Mapping[str, Any]:
        # Empty optional fields are dropped rather than declared: an
        # undocumented helper should not spend prompt budget saying so.
        payload: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "lines": [self.first_line, self.last_line],
        }
        if self.signature:
            payload["signature"] = self.signature
        if self.summary:
            payload["summary"] = self.summary
        return payload


@dataclass(frozen=True)
class RepositoryFileMap:
    """One regular file in the immutable parent inventory.

    ``symbols`` is empty for non-Python files, empty Python modules, and Python
    files with no targetable top-level definition.  ``byte_count`` is the
    immutable Git-blob size used to bound package/subsystem scope expansion.
    """

    path: str
    symbols: tuple[RepositorySymbol, ...]
    byte_count: int = 0

    def __post_init__(self) -> None:
        _repository_path(self.path, "repository file path")
        symbols = tuple(self.symbols)
        object.__setattr__(self, "symbols", symbols)
        if len(symbols) > 4_096:
            raise ValueError("repository file exceeds 4096 symbols")
        if any(not isinstance(item, RepositorySymbol) for item in symbols):
            raise TypeError("repository file symbols must be RepositorySymbol")
        names = tuple(item.name for item in symbols)
        if names != tuple(sorted(set(names))):
            raise ValueError("repository file symbols must be sorted and unique")
        if (
            isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count < 0
        ):
            raise ValueError("repository file byte_count must be non-negative")

    @property
    def symbol_names(self) -> tuple[str, ...]:
        """The grounding allowlist: exactly the addresses a target may name."""

        return tuple(item.name for item in self.symbols)


@dataclass(frozen=True)
class ParentRepositoryMap:
    """Replayable target allowlist extracted from one exact parent commit."""

    parent_id: str
    commit_sha: str
    tree_sha: str
    editable_roots: tuple[str, ...]
    files: tuple[RepositoryFileMap, ...]

    @classmethod
    def create(
        cls,
        *,
        parent_id: str,
        commit_sha: str,
        tree_sha: str,
        editable_roots: tuple[str, ...],
        files: tuple[RepositoryFileMap, ...],
    ) -> "ParentRepositoryMap":
        return cls(
            parent_id=parent_id,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            editable_roots=editable_roots,
            files=files,
        )

    def __post_init__(self) -> None:
        for name in ("parent_id", "commit_sha", "tree_sha"):
            require_text(getattr(self, name), name)
        roots = tuple(self.editable_roots)
        object.__setattr__(self, "editable_roots", roots)
        if not roots or roots != tuple(sorted(set(roots))):
            raise ValueError("editable_roots must be non-empty, sorted and unique")
        for root in roots:
            _repository_path(root, "editable root")
        files = tuple(self.files)
        object.__setattr__(self, "files", files)
        if not files or len(files) > 4_096:
            raise ValueError("repository map needs 1..4096 files")
        paths = tuple(item.path for item in files)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("repository map files must be sorted and unique")
        if any(
            not any(path == root or path.startswith(root + "/") for root in roots)
            for path in paths
        ):
            raise ValueError("repository map file lies outside editable_roots")
        if sum(len(item.symbols) for item in files) > 65_536:
            raise ValueError("repository map exceeds the symbol bound")

    def as_prompt_payload(self) -> Mapping[str, Any]:
        return deep_freeze(
            {
                "parent_id": self.parent_id,
                "commit_sha": self.commit_sha,
                "editable_roots": self.editable_roots,
                "files": [
                    {
                        "path": item.path,
                        "byte_count": item.byte_count,
                        "symbols": [
                            symbol.as_prompt_payload() for symbol in item.symbols
                        ],
                    }
                    for item in self.files
                ],
            }
        )


@dataclass(frozen=True)
class VisibleATaskEvidence:
    """Content-bearing evidence from one Visible-A development cell.

    The type is intentionally tier-specific.  Task identities, statements,
    answers, and execution text are development evidence and must never be
    constructed for Adaptive-B or Sealed-C cells.  Adaptive-B contributes only
    content-free outcome signals through ``ProgramEvidence``; Sealed-C never
    enters the search read model.

    Removing a caller-supplied tier closes the old footgun where an adapter
    could label a content-bearing record ``ADAPTIVE_B`` and still pass the
    shared contract.  Every value of this type is Visible-A by construction.
    """

    cell_id: str
    task_id: str
    title: str
    statement: str
    graded_answer: str | None = None
    observed_failure: str | None = None
    execution_excerpt: str | None = None

    def __post_init__(self) -> None:
        for name in ("cell_id", "task_id", "title", "statement"):
            require_text(getattr(self, name), name)
        for name, limit in (("title", 120), ("statement", 4_000)):
            if len(getattr(self, name)) > limit:
                raise ValueError(f"cell brief {name} exceeds {limit} characters")
        if self.title.strip() != self.title or "\n" in self.title:
            raise ValueError("cell brief title must be one trimmed line")
        if self.observed_failure is not None:
            require_text(self.observed_failure, "observed_failure")
            if len(self.observed_failure) > 120 or "\n" in self.observed_failure:
                raise ValueError("observed failure must be one short line")
        if self.execution_excerpt is not None:
            require_text(self.execution_excerpt, "execution_excerpt")
            if len(self.execution_excerpt) > 1_200:
                raise ValueError("execution excerpt exceeds 1200 characters")
        if self.graded_answer is None:
            return
        require_text(self.graded_answer, "graded_answer")
        if len(self.graded_answer) > 120 or "\n" in self.graded_answer:
            raise ValueError("graded answer must be one short line")

    def as_prompt_payload(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "evidence_ref": self.cell_id,
            "task_id": self.task_id,
            "title": self.title,
            "statement": self.statement,
            "tier": "visible_a",
        }
        if self.graded_answer is not None:
            payload["graded_answer"] = self.graded_answer
        if self.observed_failure is not None:
            payload["observed_failure"] = self.observed_failure
        if self.execution_excerpt is not None:
            payload["execution_excerpt"] = self.execution_excerpt
        return deep_freeze(payload)


@dataclass(frozen=True)
class EvidenceCutoff:
    """Latest completed fact visible to one frozen decision state."""

    available_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.available_at, "available_at")

    def allows(self, completed_at: datetime) -> bool:
        return require_aware(completed_at, "completed_at") <= self.available_at


@dataclass(frozen=True)
class CompatibilityKey:
    """Hard isolation key for transferable policy evidence."""

    objective_id: str
    evaluator_id: str
    metric_schema_id: str
    metric_direction: str
    environment_id: str

    def __post_init__(self) -> None:
        for name in (
            "objective_id",
            "evaluator_id",
            "metric_schema_id",
            "environment_id",
        ):
            require_text(getattr(self, name), name)
        if self.metric_direction not in {"maximize", "minimize"}:
            raise ValueError("metric_direction must be maximize or minimize")


@dataclass(frozen=True)
class StateDescriptor:
    """Descriptor blocks used only for neighborhood membership."""

    blocks: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.blocks, Mapping):
            raise ValueError("blocks must be a mapping")
        object.__setattr__(self, "blocks", deep_freeze(self.blocks))


@dataclass(frozen=True)
class SearchState:
    """Immutable, cutoff-safe input shared by D1, D2 and D3 in one batch."""

    state_id: str
    run_id: str
    batch_id: str
    cutoff: EvidenceCutoff
    compatibility: CompatibilityKey
    archive_seq: int
    parent_descriptor: StateDescriptor
    direction_descriptor: StateDescriptor
    budget: Mapping[str, float | int] = field(default_factory=dict)
    facts: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("state_id", "run_id", "batch_id"):
            require_text(getattr(self, name), name)
        if isinstance(self.archive_seq, bool) or self.archive_seq < 0:
            raise ValueError("archive_seq must be a non-negative integer")
        object.__setattr__(self, "budget", deep_freeze(self.budget))
        object.__setattr__(self, "facts", deep_freeze(self.facts))


@dataclass(frozen=True)
class ParentCandidate:
    parent_id: str
    commit_sha: str
    descriptor: StateDescriptor
    primary_value: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_text(self.parent_id, "parent_id")
        require_text(self.commit_sha, "commit_sha")
        object.__setattr__(self, "metadata", deep_freeze(self.metadata))


@dataclass(frozen=True)
class ParentSlate:
    slate_id: str
    decision_round_id: str
    state_id: str
    candidates: tuple[ParentCandidate, ...]

    def __post_init__(self) -> None:
        for name in ("slate_id", "decision_round_id", "state_id"):
            require_text(getattr(self, name), name)
        if not self.candidates:
            raise ValueError("parent slate must not be empty")
        identities = [item.parent_id for item in self.candidates]
        if len(identities) != len(set(identities)):
            raise ValueError("parent slate contains duplicate parent_id values")


@dataclass(frozen=True)
class DecisionTrace:
    """What a deterministic/tied selector can honestly report."""

    selected_id: str
    eligible_ids: tuple[str, ...]
    tie_ids: tuple[str, ...]
    seed: int | None
    conditional_probability: float
    indices: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_text(self.selected_id, "selected_id")
        if self.selected_id not in self.eligible_ids:
            raise ValueError("selected_id must belong to eligible_ids")
        if self.selected_id not in self.tie_ids:
            raise ValueError("selected_id must belong to tie_ids")
        if self.seed is not None and (
            isinstance(self.seed, bool) or not isinstance(self.seed, int)
        ):
            raise ValueError("seed must be an integer or None")
        if not 0.0 < self.conditional_probability <= 1.0:
            raise ValueError("conditional_probability must be in (0, 1]")
        object.__setattr__(self, "indices", deep_freeze(self.indices))


@dataclass(frozen=True)
class ParentSelection:
    selection_id: str
    slate_id: str
    decision_round_id: str
    state_id: str
    parent_id: str
    commit_sha: str
    trace: DecisionTrace

    def __post_init__(self) -> None:
        for name in (
            "selection_id",
            "slate_id",
            "decision_round_id",
            "state_id",
            "parent_id",
            "commit_sha",
        ):
            require_text(getattr(self, name), name)
