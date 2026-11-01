"""Worktree lifecycle and candidate commits.

All git access is argv subprocess (no shell, scrubbed environment).  The
search unit is the commit: candidates are pinned under
``refs/rsi-arena/candidates/<sha>`` so they survive gc regardless of
admission outcome, and any candidate can be restored in a fresh directory
from ``(repo, commit_sha)`` alone.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from typing import Optional

CANDIDATE_REF_PREFIX = "refs/rsi-arena/candidates"

COMMITTER_NAME = "rsi-arena"
COMMITTER_EMAIL = "rsi-arena@localhost"


class GitError(Exception):
    pass


def _git(args: list[str], cwd: Path, check: bool = True) -> str:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        # Deterministic, config-independent behaviour.
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": COMMITTER_NAME,
        "GIT_AUTHOR_EMAIL": COMMITTER_EMAIL,
        "GIT_COMMITTER_NAME": COMMITTER_NAME,
        "GIT_COMMITTER_EMAIL": COMMITTER_EMAIL,
    }
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


class WorktreeManager:
    """Disposable detached worktrees over a single control repo."""

    def __init__(self, repo_path: Path, worktrees_root: Path):
        self.repo_path = Path(repo_path)
        self.worktrees_root = Path(worktrees_root)
        self.worktrees_root.mkdir(parents=True, exist_ok=True)

    def create(self, base_sha: str, name: Optional[str] = None) -> Path:
        path = self.worktrees_root / (name or f"wt-{uuid.uuid4().hex[:12]}")
        _git(
            ["worktree", "add", "--detach", str(path), base_sha],
            cwd=self.repo_path,
        )
        return path

    def remove(self, worktree: Path) -> None:
        _git(
            ["worktree", "remove", "--force", str(worktree)],
            cwd=self.repo_path,
            check=False,
        )

    # -- diff inspection (gate inputs) --------------------------------------

    def staged_files(self, worktree: Path) -> list[str]:
        # NUL framing preserves unusual path names so the gate can reject
        # them explicitly instead of accidentally splitting one path into
        # several apparently-safe records.
        out = _git(["diff", "--cached", "--name-only", "-z"], cwd=worktree)
        return [path for path in out.split("\0") if path]

    def staged_diff(self, worktree: Path) -> str:
        return _git(["diff", "--cached"], cwd=worktree)

    # -- candidate commit -----------------------------------------------------

    def commit_candidate(self, worktree: Path, message: str) -> Optional[str]:
        """Commit staged changes; returns the sha, or None when the tree is
        unchanged (fate=no_change upstream).  The commit is pinned under a
        candidate ref immediately so gc can never collect it."""
        check = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(worktree),
            capture_output=True,
        )
        if check.returncode == 0:
            return None
        _git(["commit", "--no-verify", "-m", message], cwd=worktree)
        sha = _git(["rev-parse", "HEAD"], cwd=worktree).strip()
        self.pin_candidate(sha)
        return sha

    def pin_candidate(self, sha: str) -> None:
        _git(
            ["update-ref", f"{CANDIDATE_REF_PREFIX}/{sha}", sha],
            cwd=self.repo_path,
        )

    # -- verifier checkout ------------------------------------------------------

    def checkout_commit(self, sha: str, dest: Path) -> Path:
        """Fresh full checkout of a candidate in a new directory, from
        (repo, commit_sha) alone.  Used by the verifier side."""
        dest = Path(dest)
        _git(["worktree", "add", "--detach", str(dest), sha], cwd=self.repo_path)
        return dest
