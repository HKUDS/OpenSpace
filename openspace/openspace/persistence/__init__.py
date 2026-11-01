"""Public persistence API for OpenSpace.

``SessionStorage`` is the canonical and only session store.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from openspace.services.session.storage import (
    SessionLoadResult,
    SessionMetadata,
    SessionStorage,
    SessionSummary as StorageSessionSummary,
    TranscriptEntry,
    find_session_dir,
    get_project_dir,
    get_project_key,
    get_projects_dir,
    get_session_dir,
    get_sessions_dir,
    list_sessions,
    load_session,
)

from .file_history import (
    FileDiff,
    FileHistory,
    FileHistoryBackup,
    FileSnapshot,
    RevertResult,
    copy_file_history_for_resume,
)

SessionPrepare = Callable[[dict[str, Any]], Awaitable[str]]
SessionPersist = Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]]
SessionRestore = Callable[[str], Awaitable[dict[str, Any]]]
SessionFork = Callable[[str], Awaitable[dict[str, Any]]]
SessionRewind = Callable[[str, list[dict[str, Any]]], Awaitable[dict[str, Any]]]
SessionDiscover = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class RuntimeSessionAPI:
    """Persistence public API consumed by ``openspace.runtime.SessionRuntime``."""

    prepare_session: SessionPrepare | None = None
    persist_session: SessionPersist | None = None
    restore_session: SessionRestore | None = None
    fork_session: SessionFork | None = None
    rewind_session: SessionRewind | None = None
    discover_sessions: SessionDiscover | None = None

    @classmethod
    def from_runtime(cls, runtime: Any) -> "RuntimeSessionAPI":
        """Bridge an OpenSpaceRuntime into the persistence API surface."""

        return cls(
            prepare_session=getattr(runtime, "prepare_session", None),
            persist_session=getattr(runtime, "persist_session", None),
            restore_session=getattr(runtime, "restore_session", None),
            fork_session=getattr(runtime, "fork_session", None),
            rewind_session=getattr(runtime, "rewind_session", None),
            discover_sessions=getattr(runtime, "discover_sessions", None),
        )


__all__ = [
    "FileDiff",
    "FileHistory",
    "FileHistoryBackup",
    "FileSnapshot",
    "RevertResult",
    "RuntimeSessionAPI",
    "SessionLoadResult",
    "SessionMetadata",
    "SessionStorage",
    "StorageSessionSummary",
    "TranscriptEntry",
    "copy_file_history_for_resume",
    "find_session_dir",
    "get_project_dir",
    "get_project_key",
    "get_projects_dir",
    "get_session_dir",
    "get_sessions_dir",
    "list_sessions",
    "load_session",
]
