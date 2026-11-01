"""One shared lock for every mutation of a Git worktree registry."""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

_LOCK_FILE_NAME = "rsi-arena-worktree-registry.lock"
_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[Path, threading.Lock] = {}


class WorktreeRegistryError(RuntimeError):
    """The repository-wide worktree registry cannot be locked safely."""


def _thread_lock(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(path, threading.Lock())


class WorktreeRegistryGuard:
    """Serialize ``git worktree add/remove`` by canonical Git common-dir.

    A repository, any of its linked worktrees, and independent experiment
    managers all mutate the same registry below ``git-common-dir``.  The module
    lock covers independent managers in one process; ``flock`` covers separate
    campaign processes.
    """

    def __init__(
        self,
        repository: str | Path,
        *,
        environment: Mapping[str, str],
    ) -> None:
        repo = Path(repository).expanduser().resolve(strict=True)
        completed = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(repo),
            env=dict(environment),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise WorktreeRegistryError("cannot resolve the Git common directory")
        raw = completed.stdout.strip()
        if not raw:
            raise WorktreeRegistryError("Git returned an empty common directory")
        common_input = Path(raw)
        common = (
            common_input if common_input.is_absolute() else repo / common_input
        ).resolve(strict=True)
        if not common.is_dir():
            raise WorktreeRegistryError("Git common directory is not a directory")
        self.common_dir = common
        self.lock_path = common / _LOCK_FILE_NAME
        self._thread_lock = _thread_lock(self.lock_path)

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Hold the shared registry lock for one complete Git mutation."""

        with self._thread_lock:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.lock_path, flags, 0o600)
            except OSError as exc:
                raise WorktreeRegistryError(
                    "cannot open the Git worktree registry lock"
                ) from exc
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise WorktreeRegistryError(
                        "Git worktree registry lock is not a regular file"
                    )
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)


__all__ = ["WorktreeRegistryError", "WorktreeRegistryGuard"]
