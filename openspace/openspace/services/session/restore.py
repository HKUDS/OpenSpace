"""Session discovery and resume restoration.

OpenSpace keeps the project-scoped append-only transcript from
``session_storage.py`` as the resume source of truth.  This module is the
runtime-facing layer: lightweight listing, cross-project decisions, transcript
deserialization, cost/metadata/worktree recovery, and TodoWrite state hydration.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import subprocess
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from openspace.services.memory.paths import find_project_root
from openspace.services.session.recovery import (
    deserialize_for_resume as conversation_deserialize_for_resume,
)
from openspace.persistence.file_history import copy_file_history_for_resume
from openspace.services.session.storage import (
    METADATA_FILENAME,
    TRANSCRIPT_FILENAME,
    SessionLoadResult,
    SessionStorage,
    get_projects_dir,
    get_sessions_dir,
)
from openspace.tools.todo_tool import (
    TODO_WRITE_TOOL_ALIAS,
    TODO_WRITE_TOOL_NAME,
    TodoItem,
    normalize_todos,
    validate_todo_payload,
)
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

DEFAULT_DISCOVERY_LIMIT = 50
DEFAULT_PAGE_SIZE = 20
TAIL_READ_BYTES = 64 * 1024


@dataclass(slots=True)
class CrossProjectResumeResult:
    is_cross_project: bool
    is_same_repo_worktree: bool = False
    project_path: str | None = None
    command: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "is_cross_project": self.is_cross_project,
            "isCrossProject": self.is_cross_project,
            "is_same_repo_worktree": self.is_same_repo_worktree,
            "isSameRepoWorktree": self.is_same_repo_worktree,
        }
        if self.project_path:
            payload["project_path"] = self.project_path
            payload["projectPath"] = self.project_path
        if self.command:
            payload["command"] = self.command
        return payload


@dataclass(slots=True)
class SessionSummary:
    session_id: str
    title: str
    first_prompt: str
    cwd: str
    project_root: str
    model: str | None
    created_at: str
    last_active_at: str
    message_count: int
    cost_usd: float | None
    tag: str | None
    mode: str
    agent_name: str | None
    agent_type: str | None
    session_dir: str
    transcript_path: str
    same_project: bool
    cross_project: bool
    cross_project_result: CrossProjectResumeResult = field(
        default_factory=lambda: CrossProjectResumeResult(False)
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "first_prompt": self.first_prompt,
            "preview": self.first_prompt,
            "cwd": self.cwd,
            "project_root": self.project_root,
            "project_path": self.project_root or self.cwd,
            "worktree_path": _worktree_path_from_summary(self),
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.last_active_at,
            "last_active_at": self.last_active_at,
            "message_count": self.message_count,
            "cost": self.cost_usd,
            "cost_usd": self.cost_usd,
            "tag": self.tag,
            "mode": self.mode,
            "agent_name": self.agent_name,
            "agent_type": self.agent_type,
            "session_dir": self.session_dir,
            "transcript_path": self.transcript_path,
            "same_project": self.same_project,
            "cross_project": self.cross_project,
            "cross_project_result": self.cross_project_result.to_dict(),
        }


@dataclass(slots=True)
class SessionDiscoveryResult:
    sessions: list[SessionSummary]
    total: int
    page: int
    page_size: int
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": [session.to_dict() for session in self.sessions],
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "has_more": self.has_more,
        }


@dataclass(slots=True)
class ResumeDeserializationResult:
    messages: list[dict[str, Any]]
    turn_interruption_state: dict[str, Any]
    inserted_sentinel: bool = False
    inserted_continuation: bool = False


@dataclass(slots=True)
class RestoredSession:
    session_id: str
    session_record: dict[str, Any]
    title: str | None
    mode: str | None
    metadata: dict[str, Any]
    runtime: dict[str, Any]
    messages: list[dict[str, Any]]
    cost: dict[str, Any] | None
    cost_total: float | None
    agent: dict[str, Any] | None
    standalone_agent_context: dict[str, Any] | None
    worktree: dict[str, Any] | None
    file_history_snapshots: list[dict[str, Any]]
    content_replacements: list[dict[str, Any]]
    transcript_segments: list[dict[str, Any]]
    todo_state: dict[str, list[dict[str, str]]]
    turn_interruption_state: dict[str, Any]
    session_dir: str
    transcript_path: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class CrossProjectResumeRequired(RuntimeError):
    """Raised when a session should be resumed from its original project."""

    def __init__(self, result: CrossProjectResumeResult) -> None:
        self.result = result
        super().__init__(
            result.command
            or f"Session belongs to a different project: {result.project_path}"
        )


async def discover_sessions(
    cwd: str | Path,
    *,
    limit: int = DEFAULT_DISCOVERY_LIMIT,
    page: int = 0,
    page_size: int = DEFAULT_PAGE_SIZE,
    all_projects: bool = False,
    same_repo: bool = True,
    config_home: str | Path | None = None,
) -> SessionDiscoveryResult:
    """Discover resumable sessions without loading full transcripts."""

    current_cwd = _resolve_path(cwd)
    current_project_root = str(find_project_root(current_cwd))
    worktree_paths = _git_worktree_paths(current_project_root)
    scan_all = bool(all_projects or same_repo)

    summaries_by_id: dict[str, SessionSummary] = {}
    for session_dir in _iter_session_dirs(
        current_project_root,
        config_home=config_home,
        all_projects=scan_all,
    ):
        summary = _summary_from_session_dir(
            session_dir,
            current_project_root=current_project_root,
            current_cwd=str(current_cwd),
            worktree_paths=worktree_paths,
        )
        if summary is None:
            continue

        include = all_projects or summary.same_project
        if not include and same_repo:
            include = summary.cross_project_result.is_same_repo_worktree
        if not include:
            continue

        existing = summaries_by_id.get(summary.session_id)
        if existing is None or _sort_timestamp(summary) >= _sort_timestamp(existing):
            summaries_by_id[summary.session_id] = summary

    summaries = sorted(
        summaries_by_id.values(),
        key=lambda item: (_sort_timestamp(item), item.session_id),
        reverse=True,
    )
    normalized_limit = max(0, int(limit or 0))
    if normalized_limit:
        summaries = summaries[:normalized_limit]

    normalized_page_size = max(1, int(page_size or DEFAULT_PAGE_SIZE))
    normalized_page = max(0, int(page or 0))
    start = normalized_page * normalized_page_size
    page_items = summaries[start : start + normalized_page_size]

    return SessionDiscoveryResult(
        sessions=page_items,
        total=len(summaries),
        page=normalized_page,
        page_size=normalized_page_size,
        has_more=start + normalized_page_size < len(summaries),
    )


async def restore_session(
    session_id: str,
    *,
    cwd: str | Path | None = None,
    fork: bool = False,
    allow_cross_project: bool = False,
    context: Any | None = None,
    config_home: str | Path | None = None,
) -> RestoredSession:
    """Restore a session from SessionStorage and hydrate runtime state."""

    current_cwd = _resolve_path(cwd or os.getcwd())
    current_project_root = str(find_project_root(current_cwd))

    if fork:
        source = SessionStorage.for_session(
            str(session_id),
            cwd=current_cwd,
            config_home=config_home,
            create=False,
        )
        loaded = source.load()
        metadata = dict(loaded.metadata)
        metadata["forked_from"] = str(session_id)
        metadata["cwd"] = str(current_cwd)
        metadata["project_root"] = current_project_root
        metadata["project_path"] = current_project_root
        storage = SessionStorage.create_new(
            cwd=current_cwd,
            model=_none_or_str(metadata.get("model")),
            config_home=config_home,
            metadata=metadata,
        )
        await storage.replace_messages(loaded.messages, metadata_patch=metadata)
        await copy_file_history_for_resume(source, storage, loaded.file_history_snapshots)
        loaded = storage.load()
        effective_session_id = storage.session_id
    else:
        storage = SessionStorage.for_session(
            str(session_id),
            cwd=current_cwd,
            config_home=config_home,
            create=False,
        )
        loaded = storage.load()
        effective_session_id = str(session_id)

    summary = _summary_from_session_dir(
        loaded.session_dir,
        current_project_root=current_project_root,
        current_cwd=str(current_cwd),
        worktree_paths=_git_worktree_paths(current_project_root),
    )
    cross_project = (
        summary.cross_project_result
        if summary is not None
        else check_cross_project_resume(
            _summary_from_loaded(loaded, current_project_root, str(current_cwd)),
            str(current_cwd),
            _git_worktree_paths(current_project_root),
        )
    )
    if (
        cross_project.is_cross_project
        and not cross_project.is_same_repo_worktree
        and not allow_cross_project
    ):
        raise CrossProjectResumeRequired(cross_project)

    deserialized = deserialize_messages_for_resume(loaded.messages)
    metadata = dict(loaded.metadata)
    metadata["session_id"] = effective_session_id
    metadata.setdefault("session_dir", str(loaded.session_dir))
    metadata.setdefault("transcript_path", str(loaded.transcript_path))

    runtime = dict(metadata.get("runtime") or {})
    runtime["session_id"] = effective_session_id
    if metadata.get("model"):
        runtime.setdefault("model", metadata.get("model"))

    cost_snapshot = _select_cost_snapshot(metadata, loaded)
    cost_total = _extract_cost_usd(cost_snapshot or runtime)
    if cost_total is not None:
        runtime["cost_usd"] = cost_total

    worktree = _restore_worktree_metadata(storage, metadata)
    worktree_data = _mapping(worktree)
    file_history = (
        _canonical_file_history_entries(metadata.get("file_history_snapshots"))
        or _canonical_file_history_entries(worktree_data.get("file_history_snapshots"))
        or _canonical_file_history_entries(loaded.file_history_snapshots)
    )
    content_replacements = (
        _dict_list(metadata.get("content_replacements"))
        or _dict_list(worktree_data.get("content_replacements"))
        or list(loaded.content_replacements)
    )
    if worktree is not None:
        if file_history and "file_history_snapshots" not in worktree:
            worktree["file_history_snapshots"] = file_history
        if content_replacements and "content_replacements" not in worktree:
            worktree["content_replacements"] = content_replacements

    todos = extract_todos_from_transcript(deserialized.messages)
    todo_key = _todo_key(context, effective_session_id)
    todo_state = {todo_key: todos}
    if context is not None:
        existing = getattr(context, "todo_state", None)
        if isinstance(existing, dict):
            existing[todo_key] = todos
        else:
            try:
                setattr(context, "todo_state", dict(todo_state))
            except Exception:
                pass

    runtime["todo_state"] = dict(todo_state)
    runtime["turn_interruption_state"] = dict(deserialized.turn_interruption_state)

    agent = _agent_payload(metadata)
    standalone_agent_context = _standalone_agent_context(metadata)
    record = _session_record(metadata, runtime, worktree, file_history, content_replacements)

    return RestoredSession(
        session_id=effective_session_id,
        session_record=record,
        title=_none_or_str(metadata.get("title")),
        mode=_none_or_str(metadata.get("mode")),
        metadata=dict(metadata.get("metadata") or {}),
        runtime=runtime,
        messages=deserialized.messages,
        cost=cost_snapshot,
        cost_total=cost_total,
        agent=agent,
        standalone_agent_context=standalone_agent_context,
        worktree=worktree,
        file_history_snapshots=file_history,
        content_replacements=content_replacements,
        transcript_segments=list(loaded.transcript_segments),
        todo_state=todo_state,
        turn_interruption_state=deserialized.turn_interruption_state,
        session_dir=str(loaded.session_dir),
        transcript_path=str(loaded.transcript_path),
    )


async def rewind_session(
    session_id: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    cwd: str | Path | None = None,
    config_home: str | Path | None = None,
    model: str | None = None,
    metadata_patch: Mapping[str, Any] | None = None,
    cost: Mapping[str, Any] | None = None,
    allow_cross_project: bool = True,
    context: Any | None = None,
) -> RestoredSession:
    """Replace a session transcript and restore the resulting canonical session."""

    storage = SessionStorage.for_session(
        str(session_id),
        cwd=cwd,
        config_home=config_home,
        create=True,
    )
    patch = dict(metadata_patch or {})
    patch["session_id"] = str(session_id)
    patch["last_task_id"] = None
    patch["last_status"] = "rewound"
    runtime = dict(patch.get("runtime") or {})
    runtime.pop("active_task_id", None)
    runtime["phase"] = "rewound"
    runtime["session_id"] = str(session_id)
    patch["runtime"] = runtime
    if cost is not None:
        patch["cost"] = dict(cost)

    normalized_messages = [
        dict(message) for message in messages if isinstance(message, Mapping)
    ]
    await storage.replace_messages(
        normalized_messages,
        model=model,
        metadata_patch=patch,
    )
    return await restore_session(
        str(session_id),
        cwd=cwd,
        allow_cross_project=allow_cross_project,
        context=context,
        config_home=config_home,
    )


def deserialize_messages_for_resume(
    messages: Sequence[Mapping[str, Any]],
) -> ResumeDeserializationResult:
    """Make persisted OpenAI-shaped messages safe for resume."""

    recovered = conversation_deserialize_for_resume(messages)

    return ResumeDeserializationResult(
        messages=recovered.messages,
        turn_interruption_state=recovered.turn_interruption_state,
        inserted_sentinel=recovered.inserted_sentinel,
        inserted_continuation=recovered.inserted_continuation,
    )


def extract_todos_from_transcript(
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Return the last valid TodoWrite payload from a transcript."""

    for message in reversed(list(messages)):
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        for payload in _iter_assistant_tool_payloads(message):
            name = _none_or_str(payload.get("name"))
            if name not in {TODO_WRITE_TOOL_NAME, TODO_WRITE_TOOL_ALIAS}:
                continue
            args = payload.get("input")
            if not isinstance(args, Mapping):
                return []
            validation_error = validate_todo_payload(args)
            if validation_error is not None:
                return []
            todos = normalize_todos(args.get("todos") or [])
            if todos and all(todo.status == "completed" for todo in todos):
                return []
            return [todo.to_dict() for todo in todos if isinstance(todo, TodoItem)]
    return []


def check_cross_project_resume(
    session: SessionSummary,
    current_cwd: str | Path,
    worktree_paths: Sequence[str] | None = None,
) -> CrossProjectResumeResult:
    """Return the cross-project resume decision for a session."""

    current_root = str(find_project_root(current_cwd))
    project_path = session.project_root or session.cwd
    if not project_path or _same_path(project_path, current_root) or _same_path(project_path, current_cwd):
        return CrossProjectResumeResult(False)

    normalized_worktrees = [str(Path(path).expanduser().resolve()) for path in worktree_paths or ()]
    try:
        resolved_project = str(Path(project_path).expanduser().resolve())
    except Exception:
        resolved_project = project_path
    for worktree in normalized_worktrees:
        if resolved_project == worktree or resolved_project.startswith(worktree + os.sep):
            return CrossProjectResumeResult(
                True,
                is_same_repo_worktree=True,
                project_path=project_path,
            )

    return CrossProjectResumeResult(
        True,
        is_same_repo_worktree=False,
        project_path=project_path,
        command=f"cd {shlex.quote(session.cwd or project_path)} && openspace --resume {shlex.quote(session.session_id)}",
    )


def _iter_session_dirs(
    cwd: str | Path,
    *,
    config_home: str | Path | None,
    all_projects: bool,
) -> list[Path]:
    if all_projects:
        projects_dir = get_projects_dir(config_home)
        if not projects_dir.exists():
            return []
        roots = [path / "sessions" for path in projects_dir.iterdir() if path.is_dir()]
    else:
        roots = [get_sessions_dir(cwd, config_home)]

    result: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for candidate in root.iterdir():
            if not candidate.is_dir():
                continue
            if (candidate / TRANSCRIPT_FILENAME).exists() or (candidate / METADATA_FILENAME).exists():
                result.append(candidate)
    return result


def _summary_from_session_dir(
    session_dir: Path,
    *,
    current_project_root: str,
    current_cwd: str,
    worktree_paths: Sequence[str],
) -> SessionSummary | None:
    metadata = _read_json_object(session_dir / METADATA_FILENAME) or {}
    transcript = session_dir / TRANSCRIPT_FILENAME
    if not metadata and not transcript.exists():
        return None

    session_id = str(metadata.get("session_id") or session_dir.name)
    cwd = str(metadata.get("cwd") or metadata.get("project_path") or metadata.get("project_root") or "")
    project_root = str(metadata.get("project_root") or metadata.get("project_path") or cwd)
    first_prompt = _first_prompt(transcript)
    last_active = str(
        metadata.get("last_active_at")
        or metadata.get("updated_at")
        or _mtime_iso(transcript)
        or ""
    )
    summary = SessionSummary(
        session_id=session_id,
        title=str(metadata.get("title") or metadata.get("name") or first_prompt or session_id),
        first_prompt=first_prompt,
        cwd=cwd,
        project_root=project_root,
        model=_none_or_str(metadata.get("model") or _mapping(metadata.get("runtime")).get("model")),
        created_at=str(metadata.get("created_at") or ""),
        last_active_at=last_active,
        message_count=int(metadata.get("message_count") or _count_messages(transcript)),
        cost_usd=_extract_cost_usd(metadata.get("cost") or metadata.get("runtime")),
        tag=_none_or_str(metadata.get("tag")),
        mode=str(metadata.get("mode") or "normal"),
        agent_name=_none_or_str(metadata.get("agent_name") or _mapping(metadata.get("agent")).get("name")),
        agent_type=_none_or_str(metadata.get("agent_type") or _mapping(metadata.get("agent")).get("type")),
        session_dir=str(session_dir),
        transcript_path=str(transcript),
        same_project=_same_path(project_root, current_project_root) or _same_path(cwd, current_cwd),
        cross_project=False,
    )
    cross = check_cross_project_resume(summary, current_cwd, worktree_paths)
    summary.cross_project_result = cross
    summary.cross_project = cross.is_cross_project
    summary.same_project = not cross.is_cross_project
    return summary


def _summary_from_loaded(
    loaded: SessionLoadResult,
    current_project_root: str,
    current_cwd: str,
) -> SessionSummary:
    metadata = dict(loaded.metadata)
    return SessionSummary(
        session_id=loaded.session_id,
        title=str(metadata.get("title") or loaded.session_id),
        first_prompt="",
        cwd=str(metadata.get("cwd") or current_cwd),
        project_root=str(metadata.get("project_root") or metadata.get("project_path") or ""),
        model=_none_or_str(metadata.get("model")),
        created_at=str(metadata.get("created_at") or ""),
        last_active_at=str(metadata.get("last_active_at") or metadata.get("updated_at") or ""),
        message_count=len(loaded.messages),
        cost_usd=_extract_cost_usd(metadata.get("cost") or metadata.get("runtime")),
        tag=_none_or_str(metadata.get("tag")),
        mode=str(metadata.get("mode") or "normal"),
        agent_name=_none_or_str(metadata.get("agent_name")),
        agent_type=_none_or_str(metadata.get("agent_type")),
        session_dir=str(loaded.session_dir),
        transcript_path=str(loaded.transcript_path),
        same_project=_same_path(metadata.get("project_root"), current_project_root),
        cross_project=not _same_path(metadata.get("project_root"), current_project_root),
    )


def _select_cost_snapshot(
    metadata: Mapping[str, Any],
    loaded: SessionLoadResult,
) -> dict[str, Any] | None:
    cost = metadata.get("cost")
    if isinstance(cost, Mapping):
        return dict(cost)
    runtime = metadata.get("runtime")
    if isinstance(runtime, Mapping) and isinstance(runtime.get("cost"), Mapping):
        return dict(runtime["cost"])
    if not loaded.usage:
        return None
    return {
        "usage": list(loaded.usage),
        "total_cost": _extract_cost_usd(runtime) or 0.0,
    }


def _restore_worktree_metadata(
    storage: SessionStorage,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    worktree = metadata.get("worktree")
    if not isinstance(worktree, Mapping):
        return None
    restored = dict(worktree)
    worktree_path = (
        restored.get("worktree_path")
        or restored.get("worktreePath")
        or restored.get("workspace_dir")
        or restored.get("path")
    )
    if isinstance(worktree_path, str) and worktree_path:
        if Path(worktree_path).expanduser().exists():
            restored.setdefault("workspace_dir", worktree_path)
        else:
            logger.warning("Resume worktree path no longer exists: %s", worktree_path)
            try:
                storage.update_metadata({"worktree": None}, append=True)
            except Exception:
                logger.debug("Failed to clear stale worktree metadata", exc_info=True)
            return None
    return restored or None


def _session_record(
    metadata: Mapping[str, Any],
    runtime: Mapping[str, Any],
    worktree: Mapping[str, Any] | None,
    file_history: list[dict[str, Any]],
    content_replacements: list[dict[str, Any]],
) -> dict[str, Any]:
    project_path = str(metadata.get("project_root") or metadata.get("project_path") or metadata.get("cwd") or "")
    workspace_dir = (
        _mapping(worktree).get("workspace_dir")
        or _mapping(worktree).get("worktree_path")
        or metadata.get("cwd")
        or project_path
    )
    record = dict(metadata)
    record.update(
        {
            "session_id": metadata.get("session_id"),
            "project_path": project_path,
            "worktree_path": _mapping(worktree).get("worktree_path") or workspace_dir,
            "workspace_dir": workspace_dir,
            "runtime": dict(runtime),
            "worktree": dict(worktree or {}),
            "file_history_snapshots": list(file_history),
            "content_replacements": list(content_replacements),
        }
    )
    return record


def _iter_assistant_tool_payloads(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, SequenceABC) and not isinstance(tool_calls, (str, bytes)):
        for call in reversed(list(tool_calls)):
            if not isinstance(call, Mapping):
                continue
            name = _tool_call_name(call)
            args = _tool_call_arguments(call)
            payloads.append({"name": name, "input": args})

    content = message.get("content")
    if isinstance(content, SequenceABC) and not isinstance(content, (str, bytes)):
        for block in reversed(list(content)):
            if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                continue
            payloads.append(
                {
                    "name": _none_or_str(block.get("name")),
                    "input": block.get("input") if isinstance(block.get("input"), Mapping) else {},
                }
            )
    return payloads


def _tool_call_name(call: Mapping[str, Any]) -> str | None:
    function = call.get("function")
    if isinstance(function, Mapping):
        return _none_or_str(function.get("name"))
    return _none_or_str(call.get("name"))


def _tool_call_arguments(call: Mapping[str, Any]) -> dict[str, Any]:
    function = call.get("function")
    raw: Any
    if isinstance(function, Mapping):
        raw = function.get("arguments")
    else:
        raw = call.get("arguments") or call.get("input")
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _todo_key(context: Any | None, session_id: str) -> str:
    if context is not None:
        agent_id = getattr(context, "agent_id", None)
        if agent_id:
            return str(agent_id)
        context_session = getattr(context, "session_id", None)
        if context_session:
            return str(context_session)
    return str(session_id or "primary")


def _agent_payload(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    agent = metadata.get("agent")
    if isinstance(agent, Mapping) and agent:
        return dict(agent)
    agent_type = _none_or_str(metadata.get("agent_type"))
    agent_name = _none_or_str(metadata.get("agent_name"))
    if not agent_type and not agent_name:
        return None
    return {"type": agent_type, "name": agent_name}


def _standalone_agent_context(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    context = metadata.get("standalone_agent_context")
    if isinstance(context, Mapping) and context:
        return dict(context)
    name = _none_or_str(metadata.get("agent_name"))
    color = _none_or_str(metadata.get("agent_color"))
    if not name and not color:
        return None
    payload: dict[str, Any] = {"name": name or ""}
    if color and color != "default":
        payload["color"] = color
    return payload


def _git_worktree_paths(project_root: str) -> list[str]:
    root = Path(project_root).expanduser()
    if not root.exists():
        return [str(root)]
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except Exception:
        return [str(root.resolve())]
    if proc.returncode != 0:
        return [str(root.resolve())]
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            paths.append(str(Path(line[len("worktree ") :]).expanduser().resolve()))
    return paths or [str(root.resolve())]


def _first_prompt(path: Path) -> str:
    for message in _iter_transcript_messages(path, max_lines=300):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        text = _content_text(content)
        if text:
            return text[:240]
    return ""


def _iter_transcript_messages(path: Path, *, max_lines: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    messages: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if max_lines is not None and index >= max_lines:
                    break
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = raw.get("message") if isinstance(raw, Mapping) else None
                if isinstance(message, Mapping):
                    messages.append(dict(message))
                elif isinstance(raw, Mapping) and "role" in raw:
                    messages.append(dict(raw))
    except OSError:
        return []
    return messages


def _count_messages(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(raw, Mapping) and (
                    raw.get("type") == "message" or "role" in raw
                ):
                    count += 1
    except OSError:
        return 0
    return count


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, SequenceABC) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
        return "\n".join(part.strip() for part in parts if part and part.strip())
    return ""


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(raw) if isinstance(raw, Mapping) else None


def _mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return ""


def _sort_timestamp(summary: SessionSummary) -> float:
    try:
        return datetime.fromisoformat(summary.last_active_at).timestamp()
    except (TypeError, ValueError):
        try:
            return Path(summary.session_dir).stat().st_mtime
        except OSError:
            return 0.0


def _extract_cost_usd(raw: Any) -> float | None:
    if not isinstance(raw, Mapping):
        return None
    for key in ("total_cost", "totalCostUSD", "cost_usd", "cost"):
        value = raw.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _same_path(left: Any, right: Any) -> bool:
    if not left or not right:
        return False
    try:
        return Path(str(left)).expanduser().resolve() == Path(str(right)).expanduser().resolve()
    except Exception:
        return str(left) == str(right)


def _resolve_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, SequenceABC) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _canonical_file_history_entries(value: Any) -> list[dict[str, Any]]:
    return [
        item
        for item in _dict_list(value)
        if _is_canonical_file_history_snapshot(item)
    ]


def _is_canonical_file_history_snapshot(value: Mapping[str, Any]) -> bool:
    raw = value.get("snapshot") if isinstance(value.get("snapshot"), Mapping) else value
    if not isinstance(raw, Mapping):
        return False
    backups = raw.get("tracked_file_backups")
    return isinstance(backups, Mapping)


def _none_or_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _worktree_path_from_summary(summary: SessionSummary) -> str:
    return summary.cwd or summary.project_root


__all__ = [
    "CrossProjectResumeRequired",
    "CrossProjectResumeResult",
    "RestoredSession",
    "ResumeDeserializationResult",
    "SessionDiscoveryResult",
    "SessionSummary",
    "check_cross_project_resume",
    "deserialize_messages_for_resume",
    "discover_sessions",
    "extract_todos_from_transcript",
    "restore_session",
]
