"""Single-file Policy Memory carrier spanning D1, D2 and D3."""

from __future__ import annotations

import fcntl
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ContextManager, Iterator, Mapping, Protocol, Sequence

from agentpilot.domain import (
    DirectionSelection,
    DirectionSlate,
    EvidenceCutoff,
    ParentSelection,
    ParentSlate,
    SearchState,
)
from agentpilot.policy.proposer.base import StructuredProposal
from agentpilot.search import PromptEvidence

from .barrier import MemoryBarrierFact
from .ranker import MemoryProposalReceipt, MemoryRanker, RankCompletion
from .snapshot import MemorySnapshot
from .summarizer import (
    MemorySummarizer,
    SummaryCompletion,
    atomic_replace_text,
    validate_memory_document,
)

_RUN_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MEMORY_UPDATE_LOCK = threading.RLock()


class MemoryUpdateGuard(Protocol):
    """Carrier-neutral ownership guard around a memory file commit.

    Production can bind this protocol to a durable run lease.  The policy
    package intentionally knows nothing about SQLite, unit-of-work objects, or
    application lifecycle types.
    """

    def hold(self, *, run_id: str, batch_id: str) -> ContextManager[None]: ...


class _UnfencedMemoryUpdateGuard:
    """Default used by isolated policy tests and non-durable embeddings."""

    @contextmanager
    def hold(self, *, run_id: str, batch_id: str) -> Iterator[None]:
        del run_id, batch_id
        yield


@contextmanager
def _exclusive_run_lock(path: Path) -> Iterator[None]:
    """Serialize one run's memory update across Linux processes.

    Lock the run directory instead of ``memory.md`` itself.  The memory file is
    atomically replaced, so locking its old inode would allow a second process
    to lock the new inode before the first process leaves its critical section.
    A directory lock also preserves the single-file carrier contract: no lock
    sidecar is created next to ``memory.md``.
    """

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path.parent, flags)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class MemoryGenerationContext:
    """D2 context tied to the same immutable snapshot used by D1 and D3."""

    run_id: str
    batch_id: str
    state_id: str
    parent_id: str
    content: str


class SingleFileMemoryCarrier:
    """Own exactly ``runs/<run-id>/memory.md`` for each adaptive run."""

    def __init__(
        self,
        runs_root: Path,
        *,
        rank_completion: RankCompletion,
        summary_completion: SummaryCompletion,
        max_chars: int = 8_192,
        update_guard: MemoryUpdateGuard | None = None,
    ) -> None:
        self.runs_root = Path(runs_root).resolve()
        self.ranker = MemoryRanker(rank_completion)
        self.summarizer = MemorySummarizer(summary_completion, max_chars=max_chars)
        self.max_chars = self.summarizer.max_chars
        self.update_guard = update_guard or _UnfencedMemoryUpdateGuard()
        if not callable(getattr(self.update_guard, "hold", None)):
            raise TypeError("update_guard must provide hold()")

    def _hold_update_guard(self, snapshot: MemorySnapshot) -> ContextManager[None]:
        return self.update_guard.hold(
            run_id=snapshot.run_id,
            batch_id=snapshot.batch_id,
        )

    def memory_path(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or _RUN_SEGMENT.fullmatch(run_id) is None:
            raise ValueError("run_id must be one safe path segment")
        return self.runs_root / run_id / "memory.md"

    def begin_batch(self, state: SearchState) -> MemorySnapshot:
        """Read the file once; all later batch calls use the returned value."""

        path = self.memory_path(state.run_id)
        if path.parent.is_symlink():
            raise ValueError("policy memory run directory must not be a symlink")
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ValueError("policy memory path is not a regular file")
            content = path.read_text(encoding="utf-8")
        else:
            # File initialization is also a write.  Recheck under the same
            # ownership guard used by barrier replacement so an expired owner
            # cannot create memory.md after a newer process takes over.
            snapshot = MemorySnapshot(
                run_id=state.run_id,
                batch_id=state.batch_id,
                state_id=state.state_id,
                path=path,
                content="",
            )
            with self._hold_update_guard(snapshot):
                if path.exists():
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("policy memory path is not a regular file")
                    content = path.read_text(encoding="utf-8")
                else:
                    atomic_replace_text(path, "")
                    content = ""
        if len(content) > self.max_chars:
            raise ValueError("policy memory exceeds the fixed character bound")
        validate_memory_document(content, max_chars=self.max_chars)
        return MemorySnapshot(
            run_id=state.run_id,
            batch_id=state.batch_id,
            state_id=state.state_id,
            path=path,
            content=content,
        )

    def freeze_snapshot(self, snapshot: MemorySnapshot) -> Mapping[str, Any]:
        """Serialize the exact frozen text without a digest or history record."""

        if snapshot.path != self.memory_path(snapshot.run_id):
            raise ValueError("memory snapshot path is outside this carrier")
        if len(snapshot.content) > self.max_chars:
            raise ValueError("policy memory exceeds the fixed character bound")
        return {
            "run_id": snapshot.run_id,
            "batch_id": snapshot.batch_id,
            "state_id": snapshot.state_id,
            "content": snapshot.content,
        }

    def restore_snapshot(self, payload: Mapping[str, Any]) -> MemorySnapshot:
        """Restore from the batch record without rereading the current file."""

        expected = {"run_id", "batch_id", "state_id", "content"}
        if not isinstance(payload, Mapping) or set(payload) != expected:
            raise ValueError("invalid policy memory snapshot payload")
        for name in ("run_id", "batch_id", "state_id"):
            if not isinstance(payload[name], str) or not payload[name].strip():
                raise ValueError(f"memory snapshot {name} must be non-empty text")
        content = payload["content"]
        if not isinstance(content, str):
            raise ValueError("memory snapshot content must be text")
        if len(content) > self.max_chars:
            raise ValueError("policy memory exceeds the fixed character bound")
        validate_memory_document(content, max_chars=self.max_chars)
        run_id = payload["run_id"]
        return MemorySnapshot(
            run_id=run_id,
            batch_id=payload["batch_id"],
            state_id=payload["state_id"],
            path=self.memory_path(run_id),
            content=content,
        )

    def rank_parents(
        self,
        snapshot: MemorySnapshot,
        state: SearchState,
        slate: ParentSlate,
    ) -> ParentSelection:
        return self.ranker.rank_parents(snapshot, state, slate)

    def generation_context(
        self,
        snapshot: MemorySnapshot,
        state: SearchState,
        *,
        parent_id: str,
    ) -> MemoryGenerationContext:
        if (
            snapshot.run_id != state.run_id
            or snapshot.batch_id != state.batch_id
            or snapshot.state_id != state.state_id
        ):
            raise ValueError("memory snapshot belongs to a different batch state")
        if not isinstance(parent_id, str) or not parent_id.strip():
            raise ValueError("parent_id must be non-empty")
        return MemoryGenerationContext(
            run_id=state.run_id,
            batch_id=state.batch_id,
            state_id=state.state_id,
            parent_id=parent_id.strip(),
            content=validate_memory_document(
                snapshot.content, max_chars=self.max_chars
            ),
        )

    def rank_directions(
        self,
        snapshot: MemorySnapshot,
        state: SearchState,
        slate: DirectionSlate,
        *,
        evidence: PromptEvidence,
        proposals: Sequence[StructuredProposal],
        receipts: Sequence[MemoryProposalReceipt],
    ) -> DirectionSelection:
        return self.ranker.rank_directions(
            snapshot,
            state,
            slate,
            evidence=evidence,
            proposals=proposals,
            receipts=receipts,
        )

    def complete_batch(
        self,
        snapshot: MemorySnapshot,
        facts: Sequence[MemoryBarrierFact],
        *,
        completed_cutoff: EvidenceCutoff,
        update_id: str,
    ) -> str:
        """At the batch barrier, replace the file with one bounded summary."""

        if snapshot.path != self.memory_path(snapshot.run_id):
            raise ValueError("memory snapshot path is outside this carrier")
        # Avoid a paid summary call when ownership has already been lost.  This
        # check is intentionally short; the external completion is not run
        # while holding a database transaction.
        with self._hold_update_guard(snapshot):
            pass
        # A crashed update may have replaced memory.md before the batch status
        # was committed.  Replaying that exact summary is harmless.  Any other
        # content means a later/stale batch already changed the single file and
        # must not be silently overwritten by this snapshot.
        with _MEMORY_UPDATE_LOCK:
            path = snapshot.path
            if path.parent.is_symlink():
                raise ValueError("policy memory run directory must not be a symlink")
            with _exclusive_run_lock(path):
                if path.is_symlink() or not path.is_file():
                    raise ValueError("policy memory path is not a regular file")
                current = path.read_text(encoding="utf-8")
                summary = self.summarizer.summarize(
                    snapshot,
                    facts,
                    completed_cutoff=completed_cutoff,
                    update_id=update_id,
                )
                # The final guard must remain held through os.replace.  A
                # SQLite-backed implementation keeps takeover behind this
                # critical section, giving a linear order without pretending
                # the file and database form one atomic resource transaction.
                with self._hold_update_guard(snapshot):
                    current = path.read_text(encoding="utf-8")
                    if current == summary:
                        return summary
                    if current != snapshot.content:
                        raise ValueError(
                            "policy memory changed since this batch snapshot"
                        )
                    atomic_replace_text(path, summary)
        return summary


__all__ = [
    "MemoryGenerationContext",
    "MemoryUpdateGuard",
    "SingleFileMemoryCarrier",
]
