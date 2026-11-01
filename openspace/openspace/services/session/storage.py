"""Append-only session storage for OpenSpace.

Implementation notes:
- ``utils/sessionStorage.ts`` (5105 lines)
- ``bootstrap/state.ts`` session id / project-dir state

OpenSpace keeps provider-facing messages in OpenAI format and stores transcript
UUIDs in ``message["_meta"]["uuid"]``.  The JSONL entries below lift the UUID
and parent chain into storage-level fields so resume can reconstruct compacted
sessions without leaking storage metadata to LiteLLM providers.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import inspect
import json
import os
import re
import shutil
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from openspace.llm.types import TokenUsage
from openspace.services.memory.paths import (
    find_project_root,
    get_openspace_config_home_dir,
)
from openspace.services.conversation.messages import (
    clone_with_message_uuid,
    get_message_uuid,
    is_compact_boundary_message,
)
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

TRANSCRIPT_FILENAME = "transcript.jsonl"
METADATA_FILENAME = "metadata.json"
SEGMENTS_DIRNAME = "segments"
TOOL_RESULTS_DIRNAME = "tool-results"
MULTIMODAL_DIRNAME = "multimodal"
FILE_HISTORY_DIRNAME = "file-history"
LOCKS_DIRNAME = "locks"
MAX_TAIL_READ_BYTES = 64 * 1024
TRANSCRIPT_GENERATION_KEY = "transcript_generation"

SessionEntrySink = Callable[[str, dict[str, Any]], Awaitable[None] | None]


@dataclass(slots=True)
class SessionMetadata:
    session_id: str
    cwd: str
    project_root: str
    model: str | None = None
    created_at: str = ""
    last_active_at: str = ""
    title: str | None = None
    tag: str | None = None
    mode: str = "normal"
    agent_type: str | None = None
    agent_name: str | None = None
    worktree: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    migration: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any] | None,
        *,
        session_id: str,
        cwd: str,
        project_root: str,
    ) -> "SessionMetadata":
        raw = dict(data or {})
        now = _utc_now()
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
        runtime = raw.get("runtime") if isinstance(raw.get("runtime"), Mapping) else {}
        worktree = raw.get("worktree") if isinstance(raw.get("worktree"), Mapping) else {}
        cost = raw.get("cost") if isinstance(raw.get("cost"), Mapping) else {}
        migration = raw.get("migration") if isinstance(raw.get("migration"), Mapping) else {}
        return cls(
            session_id=str(raw.get("session_id") or session_id),
            cwd=str(raw.get("cwd") or raw.get("project_path") or cwd),
            project_root=str(raw.get("project_root") or raw.get("project_path") or project_root),
            model=_none_or_str(raw.get("model") or runtime.get("model")),
            created_at=str(raw.get("created_at") or now),
            last_active_at=str(
                raw.get("last_active_at")
                or raw.get("updated_at")
                or raw.get("created_at")
                or now
            ),
            title=_none_or_str(raw.get("title") or raw.get("name")),
            tag=_none_or_str(raw.get("tag")),
            mode=str(raw.get("mode") or metadata.get("mode") or "normal"),
            agent_type=_none_or_str(raw.get("agent_type")),
            agent_name=_none_or_str(raw.get("agent_name")),
            worktree=dict(worktree),
            cost=dict(cost),
            runtime=dict(runtime),
            migration=dict(migration),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 3,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "project_root": self.project_root,
            "project_path": self.project_root,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "updated_at": self.last_active_at,
            "title": self.title or "",
            "tag": self.tag,
            "mode": self.mode,
            "model": self.model,
            "agent_type": self.agent_type,
            "agent_name": self.agent_name,
            "worktree": dict(self.worktree),
            "cost": dict(self.cost),
            "runtime": dict(self.runtime),
            "migration": dict(self.migration),
        }


@dataclass(slots=True)
class TranscriptEntry:
    type: str
    session_id: str
    timestamp: str
    uuid: str | None = None
    parent_uuid: str | None = None
    logical_parent_uuid: str | None = None
    message: dict[str, Any] | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "type": self.type,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
        }
        if self.uuid is not None:
            result["uuid"] = self.uuid
        if self.parent_uuid is not None or self.type == "message":
            result["parent_uuid"] = self.parent_uuid
        if self.logical_parent_uuid is not None:
            result["logical_parent_uuid"] = self.logical_parent_uuid
        if self.message is not None:
            result["message"] = self.message
        if self.data:
            result["data"] = self.data
        return result


@dataclass(slots=True)
class SessionLoadResult:
    session_id: str
    session_dir: Path
    transcript_path: Path
    metadata: dict[str, Any]
    messages: list[dict[str, Any]]
    usage: list[dict[str, Any]] = field(default_factory=list)
    file_history_snapshots: list[dict[str, Any]] = field(default_factory=list)
    content_replacements: list[dict[str, Any]] = field(default_factory=list)
    transcript_segments: list[dict[str, Any]] = field(default_factory=list)
    current_generation: int = 0


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
    same_project: bool
    cross_project: bool

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def get_projects_dir(config_home: str | Path | None = None) -> Path:
    return get_openspace_config_home_dir(config_home) / "projects"


def get_project_key(cwd: str | Path) -> str:
    root = find_project_root(cwd)
    return _sanitize_path(str(root))


def get_project_dir(
    cwd: str | Path,
    config_home: str | Path | None = None,
) -> Path:
    return get_projects_dir(config_home) / get_project_key(cwd)


def get_sessions_dir(
    cwd: str | Path,
    config_home: str | Path | None = None,
) -> Path:
    return get_project_dir(cwd, config_home) / "sessions"


def get_session_dir(
    session_id: str,
    cwd: str | Path,
    config_home: str | Path | None = None,
) -> Path:
    return get_sessions_dir(cwd, config_home) / str(session_id)


def find_session_dir(
    session_id: str,
    *,
    cwd: str | Path | None = None,
    config_home: str | Path | None = None,
) -> Path | None:
    sid = str(session_id)
    if cwd is not None:
        candidate = get_session_dir(sid, cwd, config_home)
        if (candidate / TRANSCRIPT_FILENAME).exists() or (candidate / METADATA_FILENAME).exists():
            return candidate

    root = get_projects_dir(config_home)
    if not root.exists():
        return None
    for candidate in root.glob(f"*/sessions/{sid}"):
        if (candidate / TRANSCRIPT_FILENAME).exists() or (candidate / METADATA_FILENAME).exists():
            return candidate
    return None


class SessionStorage:
    """OpenSpace append-only transcript storage for one OS session."""

    def __init__(
        self,
        session_id: str | None = None,
        *,
        cwd: str | Path | None = None,
        model: str | None = None,
        config_home: str | Path | None = None,
        metadata: Mapping[str, Any] | None = None,
        session_dir: str | Path | None = None,
        create: bool = True,
    ) -> None:
        self.session_id = str(session_id or uuid.uuid4().hex)
        self.cwd = str(Path(cwd or os.getcwd()).expanduser().resolve())
        self.project_root = str(find_project_root(self.cwd))
        self.config_home = Path(config_home).expanduser().resolve() if config_home else None

        if session_dir is not None:
            self.session_dir = Path(session_dir).expanduser().resolve()
        else:
            existing = find_session_dir(
                self.session_id,
                cwd=self.cwd,
                config_home=self.config_home,
            )
            self.session_dir = existing or get_session_dir(
                self.session_id,
                self.project_root,
                self.config_home,
            )

        self.transcript_path = self.session_dir / TRANSCRIPT_FILENAME
        self.metadata_path = self.session_dir / METADATA_FILENAME
        self.segments_dir = self.session_dir / SEGMENTS_DIRNAME
        self.file_history_dir = self.session_dir / FILE_HISTORY_DIRNAME
        self.tool_results_dir = self.session_dir / TOOL_RESULTS_DIRNAME
        self.multimodal_dir = self.session_dir / MULTIMODAL_DIRNAME
        self.locks_dir = self.session_dir / LOCKS_DIRNAME
        self._write_lock = asyncio.Lock()
        self._seen_loaded = False
        self._seen_message_uuids: set[str] = set()
        self._last_parent_uuid: str | None = None
        self._entry_sink: SessionEntrySink | None = None

        loaded_metadata = _read_json_object(self.metadata_path)
        merged_metadata = {**(loaded_metadata or {}), **dict(metadata or {})}
        self.metadata = SessionMetadata.from_mapping(
            merged_metadata,
            session_id=self.session_id,
            cwd=self.cwd,
            project_root=self.project_root,
        )
        if model and not self.metadata.model:
            self.metadata.model = model
        self._metadata_cache = self.metadata.to_dict()
        self._current_generation = self._load_transcript_generation(merged_metadata)
        self._persist_transcript_generation(self._current_generation)

        if create:
            self._ensure_layout()
            self._flush_metadata_json()

    @property
    def current_generation(self) -> int:
        return self._current_generation

    @classmethod
    def create_new(
        cls,
        *,
        cwd: str | Path | None = None,
        model: str | None = None,
        config_home: str | Path | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "SessionStorage":
        return cls(
            uuid.uuid4().hex,
            cwd=cwd,
            model=model,
            config_home=config_home,
            metadata=metadata,
            create=True,
        )

    @classmethod
    def for_session(
        cls,
        session_id: str,
        *,
        cwd: str | Path | None = None,
        config_home: str | Path | None = None,
        create: bool = True,
    ) -> "SessionStorage":
        existing = find_session_dir(session_id, cwd=cwd, config_home=config_home)
        if existing is not None:
            metadata = _read_json_object(existing / METADATA_FILENAME) or {}
            storage_cwd = metadata.get("cwd") or metadata.get("project_root") or cwd
            return cls(
                session_id,
                cwd=storage_cwd,
                config_home=config_home,
                session_dir=existing,
                create=create,
            )
        return cls(session_id, cwd=cwd, config_home=config_home, create=create)

    async def save_turn(
        self,
        messages: Sequence[Mapping[str, Any]],
        usage: TokenUsage | Mapping[str, Any] | None = None,
        *,
        model: str | None = None,
        metadata_patch: Mapping[str, Any] | None = None,
    ) -> None:
        async with self._write_lock:
            self._ensure_layout()
            self._load_seen_index_if_needed()
            entries: list[dict[str, Any]] = []
            parent_uuid = self._last_parent_uuid

            for raw in messages:
                if not isinstance(raw, Mapping):
                    continue
                msg = clone_with_message_uuid(raw)
                msg_uuid = get_message_uuid(msg)
                if not msg_uuid:
                    continue

                if msg_uuid in self._seen_message_uuids:
                    if _chain_participant(msg):
                        parent_uuid = msg_uuid
                    continue

                is_boundary = is_compact_boundary_message(msg)
                entry = TranscriptEntry(
                    type="message",
                    session_id=self.session_id,
                    timestamp=_message_timestamp(msg),
                    uuid=msg_uuid,
                    parent_uuid=None if is_boundary else parent_uuid,
                    logical_parent_uuid=parent_uuid if is_boundary else None,
                    message=msg,
                    data={
                        TRANSCRIPT_GENERATION_KEY: self.current_generation,
                        "ref_id": self.transcript_message_ref_id(msg_uuid),
                    },
                )
                entries.append(entry.to_dict())
                self._seen_message_uuids.add(msg_uuid)
                if _chain_participant(msg):
                    parent_uuid = msg_uuid

            if usage is not None:
                entries.append(
                    TranscriptEntry(
                        type="usage",
                        session_id=self.session_id,
                        timestamp=_utc_now(),
                        data={
                            "usage": _usage_to_dict(usage),
                            "model": model or self.metadata.model,
                        },
                    ).to_dict()
                )

            if metadata_patch:
                self.update_metadata(metadata_patch, append=False)
                entries.append(self._metadata_entry())

            if entries:
                _append_jsonl(self.transcript_path, entries)
                await self._emit_entries(
                    entries,
                    metadata_patch=metadata_patch,
                )

            self._last_parent_uuid = parent_uuid
            self.metadata.last_active_at = _utc_now()
            if model:
                self.metadata.model = model
            self._metadata_cache.update(
                {
                    "last_active_at": self.metadata.last_active_at,
                    "updated_at": self.metadata.last_active_at,
                    "model": self.metadata.model,
                    "message_count": len(self._seen_message_uuids),
                }
            )
            self._flush_metadata_json()

    async def replace_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        usage: TokenUsage | Mapping[str, Any] | None = None,
        model: str | None = None,
        metadata_patch: Mapping[str, Any] | None = None,
    ) -> None:
        async with self._write_lock:
            self._ensure_layout()
            old_generation = self.current_generation
            preserved = self._preserve_transcript_generation_segment(old_generation)
            new_generation = self._next_transcript_generation()
            self._seen_loaded = True
            self._seen_message_uuids.clear()
            self._last_parent_uuid = None
            self.transcript_path.write_text("", encoding="utf-8")
            runtime_patch = (
                metadata_patch.get("runtime")
                if isinstance(metadata_patch, Mapping)
                else None
            )
            runtime_map = runtime_patch if isinstance(runtime_patch, Mapping) else {}
            task_id = None
            parent_task_id = None
            agent_id = None
            if isinstance(metadata_patch, Mapping):
                task_id = (
                    _none_or_str(runtime_map.get("active_task_id"))
                    or _none_or_str(metadata_patch.get("last_task_id"))
                )
                parent_task_id = (
                    _none_or_str(runtime_map.get("parent_task_id"))
                    or _none_or_str(metadata_patch.get("parent_task_id"))
                )
                agent_id = (
                    _none_or_str(runtime_map.get("agent_id"))
                    or _none_or_str(metadata_patch.get("agent_id"))
                )
            rewrite_data: dict[str, Any] = {
                "old_generation": old_generation,
                "new_generation": new_generation,
                "message_count": len([m for m in messages if isinstance(m, Mapping)]),
                "task_id": task_id,
                "parent_task_id": parent_task_id,
                "agent_id": agent_id,
            }
            if preserved is not None:
                rewrite_data["preserved_segment_path"] = str(preserved)
            _append_jsonl(
                self.transcript_path,
                [
                    TranscriptEntry(
                        type="transcript-rewrite",
                        session_id=self.session_id,
                        timestamp=_utc_now(),
                        data=rewrite_data,
                    ).to_dict(),
                    self._metadata_entry(),
                ],
            )
            await self._emit_entry(
                "transcript-rewrite",
                {
                    **rewrite_data,
                    "entry_type": "transcript-rewrite",
                },
            )
        await self.save_turn(
            messages,
            usage,
            model=model,
            metadata_patch=metadata_patch,
        )

    def update_metadata(
        self,
        patch: Mapping[str, Any],
        *,
        append: bool = True,
    ) -> None:
        for key, value in dict(patch).items():
            if key == "metadata" and isinstance(value, Mapping):
                runtime = self._metadata_cache.setdefault("metadata", {})
                if isinstance(runtime, dict):
                    runtime.update(dict(value))
                continue
            if key == "runtime" and isinstance(value, Mapping):
                runtime = self._metadata_cache.setdefault("runtime", {})
                if isinstance(runtime, dict):
                    runtime.update(dict(value))
                continue
            if key == "worktree" and isinstance(value, Mapping):
                worktree = self._metadata_cache.setdefault("worktree", {})
                if isinstance(worktree, dict):
                    worktree.update(dict(value))
                continue
            if key == "cost" and isinstance(value, Mapping):
                self._metadata_cache["cost"] = dict(value)
                self.metadata.cost = dict(value)
                continue
            self._metadata_cache[key] = copy.deepcopy(value)

        self._metadata_cache["session_id"] = self.session_id
        self._metadata_cache["last_active_at"] = _utc_now()
        self._metadata_cache["updated_at"] = self._metadata_cache["last_active_at"]
        self.metadata = SessionMetadata.from_mapping(
            self._metadata_cache,
            session_id=self.session_id,
            cwd=self.cwd,
            project_root=self.project_root,
        )
        self._flush_metadata_json()

        if append:
            _append_jsonl(self.transcript_path, [self._metadata_entry()])

    def reappend_session_metadata(self, *, refresh_tail: bool = True) -> None:
        self._ensure_layout()
        if refresh_tail:
            self._metadata_cache.update(_read_tail_metadata(self.transcript_path))
        self._metadata_cache["last_active_at"] = _utc_now()
        self._metadata_cache["updated_at"] = self._metadata_cache["last_active_at"]
        _append_jsonl(self.transcript_path, [self._metadata_entry()])
        self._flush_metadata_json()

    async def write_session_transcript_segment(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        reason: str = "compact",
        task_id: str | None = None,
        parent_task_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not messages:
            return None
        async with self._write_lock:
            try:
                self._ensure_layout()
                segment_id = uuid.uuid4().hex
                path = self.segments_dir / f"{segment_id}.jsonl"
                segment_messages = [
                    clone_with_message_uuid(m)
                    for m in messages
                    if isinstance(m, Mapping)
                ]
                _append_jsonl(path, segment_messages)
                head_uuid = get_message_uuid(segment_messages[0]) if segment_messages else None
                tail_uuid = get_message_uuid(segment_messages[-1]) if segment_messages else None
                data = {
                    "segment_id": segment_id,
                    "reason": reason,
                    "path": str(path),
                    "head_uuid": head_uuid,
                    "tail_uuid": tail_uuid,
                    "message_count": len(segment_messages),
                    "task_id": _none_or_str(task_id),
                    "parent_task_id": _none_or_str(parent_task_id),
                    "agent_id": _none_or_str(agent_id),
                }
                _append_jsonl(
                    self.transcript_path,
                    [
                        TranscriptEntry(
                            type="transcript-segment",
                            session_id=self.session_id,
                            timestamp=_utc_now(),
                            data=data,
                        ).to_dict()
                    ],
                )
                await self._emit_entry(
                    "transcript-segment",
                    {
                        "entry_type": "transcript-segment",
                        **data,
                    },
                )
                return data
            except Exception:
                logger.debug("Failed to write session transcript segment", exc_info=True)
                return None

    async def record_compact_summary(
        self,
        *,
        summary_message_uuid: str | None,
        compact_source: str,
        segment_ref_id: str | None = None,
        memory_path: str | None = None,
        was_truncated: bool = False,
        task_id: str | None = None,
        parent_task_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        await self.append_entry(
            "compact-summary",
            {
                "compact_source": str(compact_source),
                "summary_message_uuid": _none_or_str(summary_message_uuid),
                "segment_ref_id": _none_or_str(segment_ref_id),
                "memory_path": _none_or_str(memory_path),
                "was_truncated": bool(was_truncated),
                "task_id": _none_or_str(task_id),
                "parent_task_id": _none_or_str(parent_task_id),
                "agent_id": _none_or_str(agent_id),
            },
        )

    async def append_entry(self, entry_type: str, data: Mapping[str, Any]) -> None:
        async with self._write_lock:
            self._ensure_layout()
            _append_jsonl(
                self.transcript_path,
                [
                    TranscriptEntry(
                        type=entry_type,
                        session_id=self.session_id,
                        timestamp=_utc_now(),
                        data=dict(data),
                    ).to_dict()
                ],
            )
            await self._emit_entry(entry_type, {"entry_type": entry_type, **dict(data)})

    async def record_file_history_snapshot(
        self,
        message_uuid: str,
        snapshot: Mapping[str, Any],
        *,
        is_snapshot_update: bool = False,
        task_id: str | None = None,
        parent_task_id: str | None = None,
        agent_id: str | None = None,
        original_session_id: str | None = None,
        original_task_id: str | None = None,
        original_parent_task_id: str | None = None,
        original_agent_id: str | None = None,
    ) -> None:
        await self.append_entry(
            "file-history-snapshot",
            {
                "message_uuid": str(message_uuid),
                "snapshot": dict(snapshot),
                "is_snapshot_update": bool(is_snapshot_update),
                "task_id": _none_or_str(task_id),
                "parent_task_id": _none_or_str(parent_task_id),
                "agent_id": _none_or_str(agent_id),
                "original_session_id": _none_or_str(original_session_id),
                "original_task_id": _none_or_str(original_task_id),
                "original_parent_task_id": _none_or_str(original_parent_task_id),
                "original_agent_id": _none_or_str(original_agent_id),
            },
        )

    def load(self) -> SessionLoadResult:
        return load_session(
            self.session_id,
            cwd=self.cwd,
            config_home=self.config_home,
        )

    def fork(
        self,
        *,
        cwd: str | Path | None = None,
        metadata_patch: Mapping[str, Any] | None = None,
    ) -> "SessionStorage":
        loaded = self.load()
        metadata = dict(loaded.metadata)
        metadata["forked_from"] = self.session_id
        if metadata_patch:
            metadata.update(dict(metadata_patch))
        new_storage = SessionStorage.create_new(
            cwd=cwd or loaded.metadata.get("cwd") or self.cwd,
            model=metadata.get("model"),
            config_home=self.config_home,
            metadata=metadata,
        )
        asyncio.run(new_storage.replace_messages(loaded.messages, metadata_patch=metadata))
        return new_storage

    def delete(self) -> bool:
        if self.session_dir.exists():
            shutil.rmtree(self.session_dir)
            return True
        return False

    def _metadata_entry(self) -> dict[str, Any]:
        return TranscriptEntry(
            type="session-metadata",
            session_id=self.session_id,
            timestamp=_utc_now(),
            data=dict(self._metadata_cache),
        ).to_dict()

    def transcript_message_ref_id(self, message_uuid: str) -> str:
        return (
            f"transcript_message:{self.session_id}:"
            f"g{self.current_generation}:{message_uuid}"
        )

    def set_entry_sink(self, sink: SessionEntrySink | None) -> None:
        self._entry_sink = sink

    async def _emit_entries(
        self,
        entries: Sequence[Mapping[str, Any]],
        *,
        metadata_patch: Mapping[str, Any] | None = None,
    ) -> None:
        for index, entry in enumerate(entries):
            entry_type = str(entry.get("type") or "")
            if entry_type != "message":
                if entry_type in {
                    "transcript-rewrite",
                    "file-history-snapshot",
                    "content-replacement",
                    "compact-summary",
                    "transcript-segment",
                }:
                    data = entry.get("data")
                    await self._emit_entry(
                        entry_type,
                        {
                            "entry_type": entry_type,
                            **(dict(data) if isinstance(data, Mapping) else {}),
                        },
                    )
                continue
            await self._emit_entry(
                "message",
                self._message_entry_payload(
                    entry,
                    message_index=index,
                    metadata_patch=metadata_patch,
                ),
            )

    async def _emit_entry(self, entry_type: str, data: Mapping[str, Any]) -> None:
        sink = self._entry_sink
        if sink is None:
            return
        payload = {
            **dict(data),
            "entry_type": entry_type,
            "session_id": self.session_id,
            "session_dir": str(self.session_dir),
            "transcript_path": str(self.transcript_path),
            "tool_results_dir": str(self.tool_results_dir),
            "file_history_dir": str(self.file_history_dir),
            "transcript_generation": self.current_generation,
        }
        try:
            result = sink(entry_type, payload)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug("Session evidence entry sink failed for %s", entry_type, exc_info=True)

    def _message_entry_payload(
        self,
        entry: Mapping[str, Any],
        *,
        message_index: int,
        metadata_patch: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        message = entry.get("message")
        if not isinstance(message, Mapping):
            message = {}
        meta = message.get("_meta")
        meta_map = meta if isinstance(meta, Mapping) else {}
        tool_result_metadata = meta_map.get("tool_result_metadata")
        if not isinstance(tool_result_metadata, Mapping):
            tool_result_metadata = {}
        attachment = meta_map.get("attachment")
        attachment_map = attachment if isinstance(attachment, Mapping) else {}
        runtime = metadata_patch.get("runtime") if isinstance(metadata_patch, Mapping) else {}
        runtime_map = runtime if isinstance(runtime, Mapping) else {}
        task_id = None
        parent_task_id = None
        agent_id = None
        if isinstance(metadata_patch, Mapping):
            task_id = (
                _none_or_str(runtime_map.get("active_task_id"))
                or _none_or_str(metadata_patch.get("last_task_id"))
            )
            parent_task_id = (
                _none_or_str(runtime_map.get("parent_task_id"))
                or _none_or_str(metadata_patch.get("parent_task_id"))
            )
            agent_id = (
                _none_or_str(runtime_map.get("agent_id"))
                or _none_or_str(metadata_patch.get("agent_id"))
            )
        message_uuid = _none_or_str(entry.get("uuid")) or get_message_uuid(dict(message))
        return {
            "entry_type": "message",
            "ref_id": self.transcript_message_ref_id(message_uuid or "")
            if message_uuid
            else None,
            "message_uuid": message_uuid,
            "parent_uuid": _none_or_str(entry.get("parent_uuid")),
            "logical_parent_uuid": _none_or_str(entry.get("logical_parent_uuid")),
            "role": message.get("role"),
            "message_index": message_index,
            "task_id": task_id,
            "agent_id": _none_or_str(meta_map.get("agent_id")) or agent_id,
            "parent_task_id": _none_or_str(meta_map.get("parent_task_id")) or parent_task_id,
            "content_preview": _message_content_preview(message),
            "content_shape": _message_content_shape(message),
            "message_hash": _hash_json(message),
            "has_tool_result_metadata": bool(tool_result_metadata),
            "tool_result_metadata": dict(tool_result_metadata),
            "tool_call_id": message.get("tool_call_id") or meta_map.get("tool_call_id"),
            "tool_name": message.get("name") or meta_map.get("tool_name"),
            "backend": meta_map.get("backend"),
            "server_name": meta_map.get("server_name"),
            "attachment_type": attachment_map.get("type"),
            "memories": _attachment_memory_summaries(attachment_map),
        }

    def _ensure_layout(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.segments_dir.mkdir(parents=True, exist_ok=True)
        (self.file_history_dir / "backups").mkdir(parents=True, exist_ok=True)
        self.tool_results_dir.mkdir(parents=True, exist_ok=True)
        self.multimodal_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        if not self.transcript_path.exists():
            self.transcript_path.touch(mode=0o600)

    def _flush_metadata_json(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(self._metadata_cache)
        payload.setdefault("session_id", self.session_id)
        payload.setdefault("cwd", self.cwd)
        payload.setdefault("project_root", self.project_root)
        payload["session_dir"] = str(self.session_dir)
        payload["tool_results_dir"] = str(self.tool_results_dir)
        payload["transcript_path"] = str(self.transcript_path)
        payload[TRANSCRIPT_GENERATION_KEY] = self.current_generation
        runtime = payload.setdefault("runtime", {})
        if isinstance(runtime, dict):
            runtime[TRANSCRIPT_GENERATION_KEY] = self.current_generation
        self.metadata_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )

    def _load_transcript_generation(self, metadata: Mapping[str, Any] | None = None) -> int:
        sources: list[Any] = []
        if isinstance(metadata, Mapping):
            sources.append(metadata.get(TRANSCRIPT_GENERATION_KEY))
            runtime = metadata.get("runtime")
            if isinstance(runtime, Mapping):
                sources.append(runtime.get(TRANSCRIPT_GENERATION_KEY))
        for source in sources:
            try:
                if source is not None:
                    return max(0, int(source))
            except (TypeError, ValueError):
                continue
        return _infer_transcript_generation(self.transcript_path, self.segments_dir)

    def _persist_transcript_generation(self, generation: int) -> None:
        self._current_generation = max(0, int(generation))
        self._metadata_cache[TRANSCRIPT_GENERATION_KEY] = self._current_generation
        runtime = self._metadata_cache.setdefault("runtime", {})
        if isinstance(runtime, dict):
            runtime[TRANSCRIPT_GENERATION_KEY] = self._current_generation
        self.metadata.runtime = dict(runtime) if isinstance(runtime, dict) else {}

    def _preserve_transcript_generation_segment(self, generation: int) -> Path | None:
        entries = [
            entry
            for entry in _iter_jsonl(self.transcript_path)
            if entry.get("type") == "message" and isinstance(entry.get("message"), Mapping)
        ]
        if not entries:
            return None
        path = self.segments_dir / f"transcript-g{generation}-before-rewrite.jsonl"
        messages = [dict(entry["message"]) for entry in entries]
        try:
            path.write_text(
                "".join(
                    json.dumps(message, ensure_ascii=False, default=_json_default) + "\n"
                    for message in messages
                ),
                encoding="utf-8",
            )
            return path
        except Exception:
            logger.debug(
                "Failed to preserve transcript generation %s before rewrite",
                generation,
                exc_info=True,
            )
            return None

    def _next_transcript_generation(self) -> int:
        new_generation = self.current_generation + 1
        self._persist_transcript_generation(new_generation)
        self._flush_metadata_json()
        return new_generation

    def _load_seen_index_if_needed(self) -> None:
        if self._seen_loaded:
            return
        self._seen_loaded = True
        parent_uuid: str | None = None
        for entry in _iter_jsonl(self.transcript_path):
            if entry.get("type") != "message":
                continue
            msg_uuid = _none_or_str(entry.get("uuid"))
            if not msg_uuid:
                message = entry.get("message")
                if isinstance(message, Mapping):
                    msg_uuid = get_message_uuid(message)
            if not msg_uuid:
                continue
            self._seen_message_uuids.add(msg_uuid)
            message = entry.get("message")
            if isinstance(message, Mapping) and _chain_participant(message):
                parent_uuid = msg_uuid
        self._last_parent_uuid = parent_uuid
        try:
            loaded = load_session(
                self.session_id,
                cwd=self.cwd,
                config_home=self.config_home,
            )
        except Exception:
            logger.debug("Failed to rebuild compact-aware session parent index", exc_info=True)
            return
        for message in reversed(loaded.messages):
            if not _chain_participant(message):
                continue
            self._last_parent_uuid = get_message_uuid(message) or parent_uuid
            break


def load_session(
    session_id: str,
    *,
    cwd: str | Path | None = None,
    config_home: str | Path | None = None,
) -> SessionLoadResult:
    session_dir = find_session_dir(session_id, cwd=cwd, config_home=config_home)
    if session_dir is None:
        raise FileNotFoundError(f"Session {session_id} not found")
    transcript_path = session_dir / TRANSCRIPT_FILENAME
    metadata_path = session_dir / METADATA_FILENAME
    metadata = _read_json_object(metadata_path) or {"session_id": session_id}

    messages: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    parent_by_uuid: dict[str, str | None] = {}
    logical_parent_by_uuid: dict[str, str | None] = {}
    usage: list[dict[str, Any]] = []
    file_history: list[dict[str, Any]] = []
    content_replacements: list[dict[str, Any]] = []
    transcript_segments: list[dict[str, Any]] = []

    for entry in _iter_jsonl(transcript_path):
        etype = entry.get("type")
        if etype == "message":
            raw_msg = entry.get("message")
            if not isinstance(raw_msg, Mapping):
                continue
            msg = copy.deepcopy(dict(raw_msg))
            msg_uuid = _none_or_str(entry.get("uuid")) or get_message_uuid(msg)
            if not msg_uuid:
                msg_uuid = clone_with_message_uuid(msg)["_meta"]["uuid"]
            meta = msg.setdefault("_meta", {})
            if isinstance(meta, dict):
                meta["uuid"] = msg_uuid
            messages[msg_uuid] = msg
            parent_by_uuid[msg_uuid] = _none_or_str(entry.get("parent_uuid"))
            logical_parent_by_uuid[msg_uuid] = _none_or_str(entry.get("logical_parent_uuid"))
        elif etype == "session-metadata":
            data = entry.get("data")
            if isinstance(data, Mapping):
                metadata.update(dict(data))
        elif etype == "usage":
            data = entry.get("data")
            if isinstance(data, Mapping):
                usage.append(dict(data))
        elif etype == "file-history-snapshot":
            data = entry.get("data")
            if isinstance(data, Mapping):
                file_history.append(dict(data))
        elif etype == "content-replacement":
            data = entry.get("data")
            if isinstance(data, Mapping):
                content_replacements.append(dict(data))
        elif etype == "transcript-segment":
            data = entry.get("data")
            if isinstance(data, Mapping):
                transcript_segments.append(dict(data))
        elif etype == "transcript-rewrite":
            data = entry.get("data")
            if isinstance(data, Mapping):
                metadata[TRANSCRIPT_GENERATION_KEY] = data.get(
                    "new_generation",
                    metadata.get(TRANSCRIPT_GENERATION_KEY, 0),
                )

    _apply_preserved_segment_relinks(messages, parent_by_uuid)
    chain = _build_conversation_chain(messages, parent_by_uuid)
    current_generation = _metadata_generation(
        metadata,
        transcript_path,
        session_dir / SEGMENTS_DIRNAME,
    )
    metadata[TRANSCRIPT_GENERATION_KEY] = current_generation
    runtime = metadata.setdefault("runtime", {})
    if isinstance(runtime, dict):
        runtime[TRANSCRIPT_GENERATION_KEY] = current_generation

    return SessionLoadResult(
        session_id=str(session_id),
        session_dir=session_dir,
        transcript_path=transcript_path,
        metadata=dict(metadata),
        messages=chain,
        usage=usage,
        file_history_snapshots=file_history,
        content_replacements=content_replacements,
        transcript_segments=transcript_segments,
        current_generation=current_generation,
    )


def list_sessions(
    cwd: str | Path | None = None,
    *,
    config_home: str | Path | None = None,
    all_projects: bool = False,
    limit: int | None = None,
) -> list[SessionSummary]:
    current_root = str(find_project_root(cwd or os.getcwd()))
    roots: list[Path]
    if all_projects:
        projects_root = get_projects_dir(config_home)
        roots = [p / "sessions" for p in projects_root.glob("*") if p.is_dir()]
    else:
        roots = [get_sessions_dir(current_root, config_home)]

    summaries: list[SessionSummary] = []
    seen: set[str] = set()
    for sessions_root in roots:
        if not sessions_root.exists():
            continue
        for session_dir in sessions_root.iterdir():
            if not session_dir.is_dir() or session_dir.name in seen:
                continue
            metadata = _read_json_object(session_dir / METADATA_FILENAME) or {}
            transcript = session_dir / TRANSCRIPT_FILENAME
            if not metadata and not transcript.exists():
                continue
            session_id = str(metadata.get("session_id") or session_dir.name)
            seen.add(session_id)
            project_root = str(metadata.get("project_root") or metadata.get("project_path") or "")
            summaries.append(
                SessionSummary(
                    session_id=session_id,
                    title=str(metadata.get("title") or _first_prompt(transcript) or session_id),
                    first_prompt=_first_prompt(transcript),
                    cwd=str(metadata.get("cwd") or project_root),
                    project_root=project_root,
                    model=_none_or_str(metadata.get("model")),
                    created_at=str(metadata.get("created_at") or ""),
                    last_active_at=str(
                        metadata.get("last_active_at")
                        or metadata.get("updated_at")
                        or _mtime_iso(transcript)
                    ),
                    message_count=int(metadata.get("message_count") or _count_message_entries(transcript)),
                    cost_usd=_extract_cost_usd(metadata.get("cost") or metadata.get("runtime")),
                    tag=_none_or_str(metadata.get("tag")),
                    mode=str(metadata.get("mode") or "normal"),
                    agent_name=_none_or_str(metadata.get("agent_name")),
                    agent_type=_none_or_str(metadata.get("agent_type")),
                    session_dir=str(session_dir),
                    same_project=bool(project_root and project_root == current_root),
                    cross_project=bool(project_root and project_root != current_root),
                )
            )

    summaries.sort(key=lambda item: item.last_active_at or "", reverse=True)
    if limit is not None:
        summaries = summaries[: max(0, int(limit))]
    return summaries


def _apply_preserved_segment_relinks(
    messages: "OrderedDict[str, dict[str, Any]]",
    parent_by_uuid: dict[str, str | None],
) -> None:
    absolute_last_boundary_idx = -1
    last_seg_boundary_idx = -1
    last_seg: dict[str, Any] | None = None
    entry_index: dict[str, int] = {}

    for index, (msg_uuid, message) in enumerate(messages.items()):
        entry_index[msg_uuid] = index
        if not is_compact_boundary_message(message):
            continue
        absolute_last_boundary_idx = index
        meta = message.get("_meta")
        compact_meta = meta.get("compact_metadata") if isinstance(meta, Mapping) else None
        if isinstance(compact_meta, Mapping):
            seg = compact_meta.get("preserved_segment") or compact_meta.get("preservedSegment")
            if isinstance(seg, Mapping):
                last_seg = dict(seg)
                last_seg_boundary_idx = index

    if absolute_last_boundary_idx < 0:
        return

    preserved: set[str] = set()
    if last_seg and last_seg_boundary_idx == absolute_last_boundary_idx:
        head = _segment_value(last_seg, "head_uuid", "headUuid")
        tail = _segment_value(last_seg, "tail_uuid", "tailUuid")
        anchor = _segment_value(last_seg, "anchor_uuid", "anchorUuid")
        if head and tail and anchor:
            cur = tail
            seen: set[str] = set()
            reached_head = False
            while cur and cur not in seen and cur in messages:
                seen.add(cur)
                preserved.add(cur)
                if cur == head:
                    reached_head = True
                    break
                cur = parent_by_uuid.get(cur)
            if not reached_head:
                logger.warning(
                    "Malformed preserved segment in session transcript; "
                    "loading full history instead"
                )
                return
            parent_by_uuid[head] = anchor
            for msg_uuid, parent in list(parent_by_uuid.items()):
                if parent == anchor and msg_uuid != head:
                    parent_by_uuid[msg_uuid] = tail
            for msg_uuid in preserved:
                message = messages.get(msg_uuid)
                if not message or message.get("role") != "assistant":
                    continue
                meta = message.get("_meta")
                if isinstance(meta, dict):
                    meta.pop("usage", None)

    to_delete = [
        msg_uuid
        for msg_uuid, index in entry_index.items()
        if index < absolute_last_boundary_idx and msg_uuid not in preserved
    ]
    for msg_uuid in to_delete:
        messages.pop(msg_uuid, None)
        parent_by_uuid.pop(msg_uuid, None)


def _build_conversation_chain(
    messages: "OrderedDict[str, dict[str, Any]]",
    parent_by_uuid: dict[str, str | None],
) -> list[dict[str, Any]]:
    if not messages:
        return []
    parent_refs = {p for p in parent_by_uuid.values() if p}
    leaf: str | None = None
    for msg_uuid, message in reversed(messages.items()):
        if msg_uuid in parent_refs:
            continue
        if message.get("role") in {"user", "assistant"}:
            leaf = msg_uuid
            break
    if leaf is None:
        leaf = next(reversed(messages))

    chain_ids: list[str] = []
    seen: set[str] = set()
    cur: str | None = leaf
    while cur and cur in messages and cur not in seen:
        seen.add(cur)
        chain_ids.append(cur)
        cur = parent_by_uuid.get(cur)
    chain_ids.reverse()
    return [copy.deepcopy(messages[msg_uuid]) for msg_uuid in chain_ids if msg_uuid in messages]


def _chain_participant(message: Mapping[str, Any]) -> bool:
    meta = message.get("_meta")
    if isinstance(meta, Mapping) and meta.get("type") in {
        "progress",
        "system_api_error",
        "stop_hook_summary",
    }:
        return False
    return str(message.get("role") or "") in {"user", "assistant", "tool", "system"}


def _message_timestamp(message: Mapping[str, Any]) -> str:
    meta = message.get("_meta")
    if isinstance(meta, Mapping):
        ts = meta.get("timestamp")
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()
        if ts:
            return str(ts)
    return _utc_now()


def _append_jsonl(path: Path, entries: Sequence[Any]) -> None:
    if not entries:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False, default=_json_default) + "\n")


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Skipping corrupt JSONL line %s:%d", path, line_number)
                    continue
                if isinstance(value, dict):
                    entries.append(value)
    except OSError:
        logger.warning("Failed to read session transcript %s", path, exc_info=True)
    return entries


def _read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Failed to read session metadata %s", path, exc_info=True)
        return None
    return value if isinstance(value, dict) else None


def _read_tail_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - MAX_TAIL_READ_BYTES))
            tail = fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return {}
    metadata: dict[str, Any] = {}
    for line in tail.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("type") == "session-metadata":
            data = entry.get("data")
            if isinstance(data, Mapping):
                metadata.update(dict(data))
    return metadata


def _metadata_generation(
    metadata: Mapping[str, Any],
    transcript_path: Path,
    segments_dir: Path,
) -> int:
    for raw in (
        metadata.get(TRANSCRIPT_GENERATION_KEY),
        (metadata.get("runtime") or {}).get(TRANSCRIPT_GENERATION_KEY)
        if isinstance(metadata.get("runtime"), Mapping)
        else None,
    ):
        try:
            if raw is not None:
                return max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return _infer_transcript_generation(transcript_path, segments_dir)


def _infer_transcript_generation(transcript_path: Path, segments_dir: Path) -> int:
    generation = 0
    for entry in _iter_jsonl(transcript_path):
        data = entry.get("data")
        if not isinstance(data, Mapping):
            continue
        for key in (TRANSCRIPT_GENERATION_KEY, "new_generation"):
            try:
                if data.get(key) is not None:
                    generation = max(generation, int(data[key]))
            except (TypeError, ValueError):
                continue
    try:
        for path in segments_dir.glob("transcript-g*-before-rewrite.jsonl"):
            match = re.match(r"transcript-g(\d+)-before-rewrite\.jsonl$", path.name)
            if match:
                generation = max(generation, int(match.group(1)) + 1)
    except OSError:
        pass
    return generation


def _first_prompt(path: Path) -> str:
    for entry in _iter_jsonl(path):
        if entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()[:200]
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, Mapping) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
            text = "\n".join(parts).strip()
            if text:
                return text[:200]
    return ""


def _count_message_entries(path: Path) -> int:
    return sum(1 for entry in _iter_jsonl(path) if entry.get("type") == "message")


def _usage_to_dict(usage: TokenUsage | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(usage, TokenUsage):
        return dataclasses.asdict(usage)
    return copy.deepcopy(dict(usage))


def _json_default(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)


def _sanitize_path(name: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9]", "-", name)
    if len(sanitized) <= 200:
        return sanitized
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return f"{sanitized[:200]}-{digest}"


def _message_content_preview(message: Mapping[str, Any], max_chars: int = 500) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content[:max_chars]
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif block.get("type"):
                    parts.append(f"[{block.get('type')}]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)[:max_chars]
    if content is None:
        return ""
    return str(content)[:max_chars]


def _message_content_shape(message: Mapping[str, Any]) -> str:
    meta = message.get("_meta")
    if isinstance(meta, Mapping) and meta.get("type") == "tool_result":
        return "tool_result"
    content = message.get("content")
    if isinstance(content, str):
        return "text"
    if isinstance(content, list):
        if any(
            isinstance(block, Mapping)
            and block.get("type") in {"image", "image_url", "document"}
            for block in content
        ):
            return "attachment"
        return "blocks"
    return "text" if content is not None else "empty"


def _hash_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default).encode(
            "utf-8"
        )
    ).hexdigest()


def _attachment_memory_summaries(attachment: Mapping[str, Any]) -> list[dict[str, Any]]:
    memories = attachment.get("memories")
    if not isinstance(memories, list):
        if attachment.get("path"):
            memories = [attachment]
        else:
            return []
    result: list[dict[str, Any]] = []
    for memory in memories:
        if not isinstance(memory, Mapping):
            continue
        path = _none_or_str(memory.get("path"))
        if not path:
            continue
        result.append(
            {
                "path": path,
                "mtimeMs": memory.get("mtimeMs"),
                "header": memory.get("header"),
                "limit": memory.get("limit"),
            }
        )
    return result


def _none_or_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return ""


def _extract_cost_usd(value: Any) -> float | None:
    if not isinstance(value, Mapping):
        return None
    for key in ("total_cost_usd", "cost_usd", "total", "cost"):
        raw = value.get(key)
        if isinstance(raw, (int, float)):
            return float(raw)
    return None


def _segment_value(segment: Mapping[str, Any], snake: str, camel: str) -> str | None:
    return _none_or_str(segment.get(snake) or segment.get(camel))


__all__ = [
    "SessionMetadata",
    "TranscriptEntry",
    "SessionLoadResult",
    "SessionSummary",
    "SessionStorage",
    "get_projects_dir",
    "get_project_key",
    "get_project_dir",
    "get_sessions_dir",
    "get_session_dir",
    "find_session_dir",
    "load_session",
    "list_sessions",
]
