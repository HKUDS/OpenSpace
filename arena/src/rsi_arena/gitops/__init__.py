"""Git worktrees, candidate commits, and the candidate scope gate."""

from rsi_arena.gitops.gate import GateResult, run_gate
from rsi_arena.gitops.repo import GitError, WorktreeManager

__all__ = [
    "GitError",
    "WorktreeManager",
    "GateResult",
    "run_gate",
]
