"""Session transcript and storage evidence adapter."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .memory_refs import canonical_path_hash, memory_ref_id
from .store import EvidenceStore
from .tool_adapter import ToolEvidenceAdapter
from .types import EvidenceEvent, ResourceRef


class SessionEvidenceAdapter:
    """Translate durable SessionStorage entries into evidence refs."""

    def __init__(self, store: EvidenceStore) -> None:
        self._store = store
        self._tool_adapter = ToolEvidenceAdapter(store)

    async def on_session_entry(
        self,
        entry_type: str,
        data: dict[str, Any],
    ) -> None:
        event = self.build_event(entry_type, data)
        if event is not None:
            self._store.ingest_event(event)

    def build_event(
        self,
        entry_type: str,
        data: Mapping[str, Any],
    ) -> EvidenceEvent | None:
        normalized = str(entry_type or data.get("entry_type") or "")
        if normalized == "message":
            return self._message_event(data)
        if normalized == "transcript-segment":
            return self._segment_event(data)
        if normalized == "transcript-rewrite":
            return self._rewrite_event(data)
        if normalized in {"compact-summary", "compact_summary"}:
            return self._compact_summary_event(data)
        if normalized == "file-history-snapshot":
            return self._file_history_event(data)
        if normalized == "content-replacement":
            return self._content_replacement_event(data)
        return None

    async def ingest_storage_delta(
        self,
        storage: Any,
        *,
        task_id: str | None = None,
        parent_task_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Best-effort checkpoint scan for transcript entries missed by callback."""

        transcript_path = Path(getattr(storage, "transcript_path", "") or "")
        if not transcript_path.is_file():
            return
        session_id = _none_or_str(getattr(storage, "session_id", None))
        if not session_id:
            return
        session_dir = _none_or_str(getattr(storage, "session_dir", None))
        tool_results_dir = _none_or_str(getattr(storage, "tool_results_dir", None))
        file_history_dir = _none_or_str(getattr(storage, "file_history_dir", None))
        generation = int(getattr(storage, "current_generation", 0) or 0)
        scoped_task_id = _none_or_str(task_id)
        scoped_parent_task_id = _none_or_str(parent_task_id)
        scoped_agent_id = _none_or_str(agent_id) or "primary"
        message_index = 0

        for entry in _iter_jsonl(transcript_path):
            entry_type = str(entry.get("type") or entry.get("entry_type") or "")
            data = entry.get("data") if isinstance(entry.get("data"), Mapping) else {}
            if entry_type == "session-metadata":
                scoped_task_id = (
                    _metadata_task_id(data)
                    or scoped_task_id
                    or _none_or_str(task_id)
                )
                scoped_parent_task_id = (
                    _metadata_parent_task_id(data)
                    or scoped_parent_task_id
                    or _none_or_str(parent_task_id)
                )
                scoped_agent_id = (
                    _metadata_agent_id(data)
                    or scoped_agent_id
                    or _none_or_str(agent_id)
                    or "primary"
                )
                generation = _metadata_generation(data, generation)
                continue
            if entry_type == "transcript-rewrite":
                generation = _safe_int(data.get("new_generation"), generation)

            payload = _payload_from_storage_entry(
                entry,
                session_id=session_id,
                session_dir=session_dir,
                transcript_path=str(transcript_path),
                tool_results_dir=tool_results_dir,
                file_history_dir=file_history_dir,
                transcript_generation=generation,
                task_id=scoped_task_id,
                parent_task_id=scoped_parent_task_id,
                agent_id=scoped_agent_id,
                message_index=message_index,
            )
            if payload is None:
                continue
            if entry_type == "message":
                message_index += 1
            await self.on_session_entry(entry_type, payload)

    def _message_event(self, data: Mapping[str, Any]) -> EvidenceEvent | None:
        session_id = _none_or_str(data.get("session_id"))
        message_uuid = _none_or_str(data.get("message_uuid"))
        if not session_id or not message_uuid:
            return None
        generation = int(data.get("transcript_generation") or 0)
        task_id = _none_or_str(data.get("task_id"))
        parent_task_id = _none_or_str(data.get("parent_task_id"))
        agent_id = _none_or_str(data.get("agent_id"))
        created_at = _utc_now()
        ref_id = (
            _none_or_str(data.get("ref_id"))
            or f"transcript_message:{session_id}:g{generation}:{message_uuid}"
        )
        transcript_path = _none_or_str(data.get("transcript_path"))
        uri = (
            f"{transcript_path}#generation={generation}&uuid={message_uuid}"
            if transcript_path
            else None
        )
        metadata = {
            "transcript_generation": generation,
            "logical_message_uuid": message_uuid,
            "rewrite_marker": data.get("rewrite_marker"),
            "parent_uuid": data.get("parent_uuid"),
            "logical_parent_uuid": data.get("logical_parent_uuid"),
            "role": data.get("role"),
            "message_index": data.get("message_index"),
            "has_tool_result_metadata": bool(data.get("has_tool_result_metadata")),
            "attachment_type": data.get("attachment_type"),
            "content_shape": data.get("content_shape"),
            "tool_call_id": data.get("tool_call_id"),
            "tool_name": data.get("tool_name"),
        }
        refs = [
            ResourceRef(
                ref_id=ref_id,
                ref_type="transcript_message",
                uri=uri,
                session_id=session_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
                agent_id=agent_id,
                producer="session_storage",
                created_at=created_at,
                reliability="persisted",
                role="primary",
                hash=_none_or_str(data.get("message_hash")),
                preview=str(data.get("content_preview") or "")[:500],
                metadata=metadata,
            )
        ]
        tool_result_ref = self._tool_result_from_message(data, created_at=created_at)
        if tool_result_ref is not None:
            refs.append(tool_result_ref)
        refs.extend(self._memory_refs_from_attachment(data, created_at=created_at))
        primary_refs, supporting_refs, derived_refs = _refs_by_event_role(refs)

        digest = _digest({"session_id": session_id, "ref_id": ref_id})
        return EvidenceEvent.create(
            event_id=f"evt_session_msg_{digest}",
            event_type="session_transcript_message",
            producer="session_storage",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            idempotency_key=f"session:message:{ref_id}",
            primary_refs=primary_refs,
            supporting_refs=supporting_refs,
            derived_refs=derived_refs,
            metadata={"entry_type": "message", "role": data.get("role")},
        )

    def _segment_event(self, data: Mapping[str, Any]) -> EvidenceEvent | None:
        session_id = _none_or_str(data.get("session_id"))
        segment_id = _none_or_str(data.get("segment_id"))
        path = _none_or_str(data.get("path"))
        if not session_id or not (segment_id or path):
            return None
        created_at = _utc_now()
        ref_id = f"transcript_segment:{session_id}:{segment_id or _digest(path)}"
        task_id = _none_or_str(data.get("task_id"))
        missing = bool(path and not Path(path).expanduser().is_file())
        ref = ResourceRef(
            ref_id=ref_id,
            ref_type="transcript_segment",
            uri=path,
            session_id=session_id,
            task_id=task_id,
            parent_task_id=_none_or_str(data.get("parent_task_id")),
            agent_id=_none_or_str(data.get("agent_id")),
            producer="session_storage",
            created_at=created_at,
            reliability="fallback" if missing else "persisted",
            role="supporting" if missing else "primary",
            hash=_file_hash(path),
            preview=(
                f"{data.get('message_count')} messages; "
                f"{data.get('head_uuid')}..{data.get('tail_uuid')}"
            ),
            metadata={
                "segment_id": segment_id,
                "reason": data.get("reason"),
                "head_uuid": data.get("head_uuid"),
                "tail_uuid": data.get("tail_uuid"),
                "message_count": data.get("message_count"),
                "missing": missing,
            },
        )
        primary_refs, supporting_refs, derived_refs = _refs_by_event_role([ref])
        return EvidenceEvent.create(
            event_id=f"evt_session_segment_{_digest(ref_id)}",
            event_type="session_transcript_segment",
            producer="session_storage",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            idempotency_key=f"session:segment:{ref_id}",
            primary_refs=primary_refs,
            supporting_refs=supporting_refs,
            derived_refs=derived_refs,
            metadata={"entry_type": "transcript-segment"},
        )

    def _rewrite_event(self, data: Mapping[str, Any]) -> EvidenceEvent | None:
        session_id = _none_or_str(data.get("session_id"))
        if not session_id:
            return None
        old_generation = data.get("old_generation")
        new_generation = data.get("new_generation")
        created_at = _utc_now()
        ref_id = f"transcript_rewrite:{session_id}:g{old_generation}:g{new_generation}"
        task_id = _none_or_str(data.get("task_id"))
        parent_task_id = _none_or_str(data.get("parent_task_id"))
        agent_id = _none_or_str(data.get("agent_id"))
        preserved = _none_or_str(data.get("preserved_segment_path"))
        preserved_ref_id = (
            "transcript_segment:"
            f"{session_id}:preserved-g{old_generation}:{_digest(preserved)[:12]}"
            if preserved
            else None
        )
        rewrite_ref = ResourceRef(
            ref_id=ref_id,
            ref_type="transcript_rewrite",
            uri=_none_or_str(data.get("transcript_path")),
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            producer="session_storage",
            created_at=created_at,
            reliability="persisted",
            role="supporting",
            preview=f"transcript generation {old_generation} -> {new_generation}",
            metadata={
                "old_generation": old_generation,
                "new_generation": new_generation,
                "message_count": data.get("message_count"),
                "preserved_segment_path": data.get("preserved_segment_path"),
            },
            raw_backrefs=[preserved_ref_id] if preserved_ref_id else [],
        )
        refs = [rewrite_ref]
        if preserved and preserved_ref_id:
            refs.append(
                ResourceRef(
                    ref_id=preserved_ref_id,
                    ref_type="transcript_segment",
                    uri=preserved,
                    session_id=session_id,
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                    agent_id=agent_id,
                    producer="session_storage",
                    created_at=created_at,
                    reliability="persisted",
                    role="supporting",
                    hash=_file_hash(preserved),
                    preview=f"preserved generation {old_generation}",
                    metadata={
                        "reason": "transcript_rewrite_preserve",
                        "old_generation": old_generation,
                    },
                )
            )
        primary_refs, supporting_refs, derived_refs = _refs_by_event_role(refs)
        return EvidenceEvent.create(
            event_id=f"evt_session_rewrite_{_digest(ref_id)}",
            event_type="session_transcript_rewrite",
            producer="session_storage",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            idempotency_key=f"session:rewrite:{ref_id}",
            primary_refs=primary_refs,
            supporting_refs=supporting_refs,
            derived_refs=derived_refs,
            metadata={"entry_type": "transcript-rewrite"},
        )

    def _compact_summary_event(self, data: Mapping[str, Any]) -> EvidenceEvent | None:
        session_id = _none_or_str(data.get("session_id"))
        summary_uuid = _none_or_str(data.get("summary_message_uuid"))
        if not session_id:
            return None
        created_at = _utc_now()
        task_id = _none_or_str(data.get("task_id"))
        segment_ref_id = _none_or_str(data.get("segment_ref_id"))
        raw_backrefs = [segment_ref_id] if segment_ref_id else []
        digest = _digest(
            {
                "session_id": session_id,
                "summary_uuid": summary_uuid,
                "segment_ref_id": segment_ref_id,
                "source": data.get("compact_source"),
            }
        )
        ref_id = f"compact_summary:{session_id}:{summary_uuid or digest}"
        transcript_path = _none_or_str(data.get("transcript_path"))
        ref = ResourceRef(
            ref_id=ref_id,
            ref_type="compact_summary",
            uri=(
                f"{transcript_path}#compact-summary:{summary_uuid}"
                if transcript_path and summary_uuid
                else transcript_path
            ),
            session_id=session_id,
            task_id=task_id,
            parent_task_id=_none_or_str(data.get("parent_task_id")),
            agent_id=_none_or_str(data.get("agent_id")),
            producer="session_storage",
            created_at=created_at,
            reliability="derived" if raw_backrefs else "summary_only",
            role="supporting",
            preview=f"compact summary from {data.get('compact_source') or 'unknown'}",
            metadata={
                "compact_source": data.get("compact_source"),
                "summary_message_uuid": summary_uuid,
                "segment_ref_id": segment_ref_id,
                "memory_path": data.get("memory_path"),
                "was_truncated": bool(data.get("was_truncated")),
                "missing_raw_backrefs": not bool(raw_backrefs),
            },
            raw_backrefs=raw_backrefs,
        )
        return EvidenceEvent.create(
            event_id=f"evt_compact_summary_{_digest(ref_id)}",
            event_type="session_compact_summary",
            producer="session_storage",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            idempotency_key=f"session:compact_summary:{ref_id}",
            supporting_refs=[ref],
            metadata={"entry_type": "compact-summary"},
        )

    def _file_history_event(self, data: Mapping[str, Any]) -> EvidenceEvent | None:
        session_id = _none_or_str(data.get("session_id"))
        message_uuid = _none_or_str(data.get("message_uuid"))
        if not session_id or not message_uuid:
            return None
        task_id = _none_or_str(data.get("task_id"))
        created_at = _utc_now()
        snapshot = data.get("snapshot")
        touched_files, read_files, written_files = _file_history_paths(snapshot)
        changed_paths: list[str] = []
        if isinstance(snapshot, Mapping):
            raw_paths = snapshot.get("paths") or snapshot.get("files") or []
            if isinstance(raw_paths, (list, tuple)):
                changed_paths = [str(path) for path in raw_paths if path]
        if not changed_paths:
            changed_paths = list(touched_files)
        ref_id = (
            f"file_history:{session_id}:{message_uuid}:"
            f"{_digest({'snapshot': snapshot, 'update': data.get('is_snapshot_update')})[:12]}"
        )
        ref = ResourceRef(
            ref_id=ref_id,
            ref_type="file_history",
            uri=(
                f"{_none_or_str(data.get('transcript_path'))}#file-history:{message_uuid}"
                if _none_or_str(data.get("transcript_path"))
                else _none_or_str(data.get("file_history_dir"))
            ),
            session_id=session_id,
            task_id=task_id,
            parent_task_id=_none_or_str(data.get("parent_task_id")),
            agent_id=_none_or_str(data.get("agent_id")),
            producer="session_storage",
            created_at=created_at,
            reliability="persisted",
            role="supporting",
            preview=f"file history snapshot for {message_uuid}",
            metadata={
                "message_uuid": message_uuid,
                "snapshot_id": (
                    snapshot.get("snapshot_id")
                    if isinstance(snapshot, Mapping)
                    else None
                ),
                "is_snapshot_update": bool(data.get("is_snapshot_update")),
                "changed_paths": changed_paths,
                "touched_files": touched_files,
                "read_files": read_files,
                "written_files": written_files,
                "file_history_dir": data.get("file_history_dir"),
                "original_session_id": data.get("original_session_id"),
                "original_task_id": data.get("original_task_id"),
                "original_parent_task_id": data.get("original_parent_task_id"),
                "original_agent_id": data.get("original_agent_id"),
            },
        )
        return EvidenceEvent.create(
            event_id=f"evt_file_history_{_digest(ref_id)}",
            event_type="session_file_history_snapshot",
            producer="session_storage",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            idempotency_key=f"session:file_history:{ref_id}",
            supporting_refs=[ref],
            metadata={"entry_type": "file-history-snapshot"},
        )

    def _content_replacement_event(self, data: Mapping[str, Any]) -> EvidenceEvent | None:
        session_id = _none_or_str(data.get("session_id"))
        if not session_id:
            return None
        created_at = _utc_now()
        task_id = _none_or_str(data.get("task_id"))
        ref_id = f"content_replacement:{session_id}:{_digest(data)}"
        ref = ResourceRef(
            ref_id=ref_id,
            ref_type="content_replacement",
            uri=_none_or_str(data.get("path")),
            session_id=session_id,
            task_id=task_id,
            producer="session_storage",
            created_at=created_at,
            reliability="persisted",
            role="supporting",
            preview=str(data.get("reason") or "content replacement")[:200],
            metadata={
                key: value
                for key, value in data.items()
                if key not in {"content", "message"}
            },
        )
        return EvidenceEvent.create(
            event_id=f"evt_content_repl_{_digest(ref_id)}",
            event_type="session_content_replacement",
            producer="session_storage",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            idempotency_key=f"session:content_replacement:{ref_id}",
            supporting_refs=[ref],
            metadata={"entry_type": "content-replacement"},
        )

    def _tool_result_from_message(
        self,
        data: Mapping[str, Any],
        *,
        created_at: str,
    ) -> ResourceRef | None:
        tool_result_metadata = data.get("tool_result_metadata")
        if not isinstance(tool_result_metadata, Mapping):
            return None
        tool_call_id = _none_or_str(data.get("tool_call_id"))
        tool_name = _none_or_str(data.get("tool_name"))
        if not tool_call_id or not tool_name:
            return None
        session_id = _none_or_str(data.get("session_id"))
        task_id = _none_or_str(data.get("task_id"))
        agent_id = _none_or_str(data.get("agent_id")) or "primary"
        backend = _none_or_str(data.get("backend")) or "unknown"
        server_name = _none_or_str(data.get("server_name")) or "default"
        raw_backref = f"tool_event:{session_id or 'none'}:{task_id or 'none'}:{agent_id}:{tool_call_id}"
        return self._tool_adapter.persisted_tool_result_ref(
            {
                "tool_result_metadata": tool_result_metadata,
                "current_iteration": data.get("current_iteration"),
            },
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            parent_task_id=_none_or_str(data.get("parent_task_id")),
            agent_id=agent_id,
            tool_use_id=tool_call_id,
            tool_name=tool_name,
            tool_key=f"{backend}:{server_name}:{tool_name}",
            raw_backref=raw_backref,
        )

    def _memory_refs_from_attachment(
        self,
        data: Mapping[str, Any],
        *,
        created_at: str,
    ) -> list[ResourceRef]:
        attachment_type = _none_or_str(data.get("attachment_type"))
        if attachment_type not in {"relevant_memories", "nested_memory"}:
            return []
        memories = data.get("memories")
        if not isinstance(memories, list):
            return []
        refs: list[ResourceRef] = []
        session_id = _none_or_str(data.get("session_id"))
        task_id = _none_or_str(data.get("task_id"))
        parent_task_id = _none_or_str(data.get("parent_task_id"))
        agent_id = _none_or_str(data.get("agent_id"))
        for memory in memories:
            if not isinstance(memory, Mapping):
                continue
            path = _none_or_str(memory.get("path"))
            if not path:
                continue
            source_kind = f"transcript_attachment:{attachment_type}"
            refs.append(
                ResourceRef(
                    ref_id=memory_ref_id(
                        source_kind,
                        session_id=session_id,
                        task_id=task_id,
                        agent_id=agent_id,
                        path=path,
                    ),
                    ref_type="memory_ref",
                    uri=path,
                    session_id=session_id,
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                    agent_id=agent_id,
                    producer="session_storage",
                    created_at=created_at,
                    reliability="persisted",
                    role="supporting",
                    hash=_file_hash(path),
                    preview=str(memory.get("header") or "")[:300],
                    metadata={
                        "attachment_type": attachment_type,
                        "path": path,
                        "mtime_ms": memory.get("mtimeMs"),
                        "limit": memory.get("limit"),
                        "memory_kind": _attachment_memory_kind(attachment_type),
                        "canonical_path_hash": canonical_path_hash(path),
                        "loaded_in_context": True,
                        "read_or_written_by_tool": False,
                        "source_event": "transcript_attachment",
                        "source_kind": source_kind,
                    },
                )
            )
        return refs


def _file_hash(path_text: str | None) -> str | None:
    if not path_text:
        return None
    try:
        path = Path(path_text).expanduser()
        if not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return None


def _attachment_memory_kind(attachment_type: str | None) -> str:
    if attachment_type == "nested_memory":
        return "nested"
    if attachment_type == "relevant_memories":
        return "relevant"
    return "model_visible"


def _refs_by_event_role(
    refs: list[ResourceRef],
) -> tuple[list[ResourceRef], list[ResourceRef], list[ResourceRef]]:
    primary: list[ResourceRef] = []
    supporting: list[ResourceRef] = []
    derived: list[ResourceRef] = []
    for ref in refs:
        if ref.role == "primary":
            primary.append(ref)
        elif ref.role == "derived":
            derived.append(ref)
        else:
            supporting.append(ref)
    return primary, supporting, derived


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    entries.append(value)
    except OSError:
        return []
    return entries


def _payload_from_storage_entry(
    entry: Mapping[str, Any],
    *,
    session_id: str,
    session_dir: str | None,
    transcript_path: str,
    tool_results_dir: str | None,
    file_history_dir: str | None,
    transcript_generation: int,
    task_id: str | None,
    parent_task_id: str | None,
    agent_id: str | None,
    message_index: int,
) -> dict[str, Any] | None:
    entry_type = str(entry.get("type") or entry.get("entry_type") or "")
    data = entry.get("data") if isinstance(entry.get("data"), Mapping) else {}
    base = {
        "entry_type": entry_type,
        "session_id": session_id,
        "session_dir": session_dir,
        "transcript_path": transcript_path,
        "tool_results_dir": tool_results_dir,
        "file_history_dir": file_history_dir,
        "transcript_generation": _safe_int(
            data.get("transcript_generation"),
            transcript_generation,
        ),
        "task_id": _none_or_str(data.get("task_id")) or task_id,
        "parent_task_id": _none_or_str(data.get("parent_task_id")) or parent_task_id,
        "agent_id": _none_or_str(data.get("agent_id")) or agent_id or "primary",
    }
    if entry_type == "message":
        message = entry.get("message")
        if not isinstance(message, Mapping):
            return None
        message_uuid = _none_or_str(entry.get("uuid")) or _message_uuid(message)
        if not message_uuid:
            return None
        meta = message.get("_meta") if isinstance(message.get("_meta"), Mapping) else {}
        meta_map = meta if isinstance(meta, Mapping) else {}
        tool_result_metadata = meta_map.get("tool_result_metadata")
        attachment = meta_map.get("attachment")
        attachment_map = attachment if isinstance(attachment, Mapping) else {}
        generation = int(base["transcript_generation"] or 0)
        return {
            **base,
            "ref_id": (
                _none_or_str(data.get("ref_id"))
                or f"transcript_message:{session_id}:g{generation}:{message_uuid}"
            ),
            "message_uuid": message_uuid,
            "parent_uuid": _none_or_str(entry.get("parent_uuid")),
            "logical_parent_uuid": _none_or_str(entry.get("logical_parent_uuid")),
            "role": message.get("role"),
            "message_index": message_index,
            "content_preview": _message_content_preview(message),
            "content_shape": _message_content_shape(message),
            "message_hash": _sha256_json(message),
            "has_tool_result_metadata": isinstance(tool_result_metadata, Mapping),
            "tool_result_metadata": (
                dict(tool_result_metadata)
                if isinstance(tool_result_metadata, Mapping)
                else {}
            ),
            "tool_call_id": message.get("tool_call_id") or meta_map.get("tool_call_id"),
            "tool_name": message.get("name") or meta_map.get("tool_name"),
            "backend": meta_map.get("backend"),
            "server_name": meta_map.get("server_name"),
            "attachment_type": attachment_map.get("type"),
            "memories": _attachment_memory_summaries(attachment_map),
        }
    if entry_type in {
        "transcript-segment",
        "transcript-rewrite",
        "file-history-snapshot",
        "content-replacement",
        "compact-summary",
    }:
        return {**base, **dict(data)}
    return None


def _message_uuid(message: Mapping[str, Any]) -> str | None:
    meta = message.get("_meta")
    if isinstance(meta, Mapping) and meta.get("uuid"):
        return str(meta["uuid"])
    value = message.get("uuid")
    return str(value) if value else None


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
    return "" if content is None else str(content)[:max_chars]


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


def _file_history_paths(snapshot: Any) -> tuple[list[str], list[str], list[str]]:
    if not isinstance(snapshot, Mapping):
        return [], [], []
    touched: set[str] = set()
    read_files: set[str] = set()
    written: set[str] = set()
    for key in ("paths", "files", "touched_files", "changed_paths"):
        raw = snapshot.get(key)
        if isinstance(raw, (list, tuple, set)):
            touched.update(str(path) for path in raw if path)
    for key in ("read_files", "readFiles"):
        raw = snapshot.get(key)
        if isinstance(raw, (list, tuple, set)):
            read_files.update(str(path) for path in raw if path)
    for key in ("written_files", "writtenFiles"):
        raw = snapshot.get(key)
        if isinstance(raw, (list, tuple, set)):
            written.update(str(path) for path in raw if path)
    backups = snapshot.get("tracked_file_backups")
    if isinstance(backups, Mapping):
        touched.update(str(path) for path in backups.keys() if path)
        written.update(str(path) for path in backups.keys() if path)
    return sorted(touched), sorted(read_files), sorted(written)


def _metadata_task_id(data: Mapping[str, Any]) -> str | None:
    runtime = data.get("runtime") if isinstance(data.get("runtime"), Mapping) else {}
    runtime_map = runtime if isinstance(runtime, Mapping) else {}
    return _none_or_str(runtime_map.get("active_task_id")) or _none_or_str(
        data.get("last_task_id")
    )


def _metadata_parent_task_id(data: Mapping[str, Any]) -> str | None:
    runtime = data.get("runtime") if isinstance(data.get("runtime"), Mapping) else {}
    runtime_map = runtime if isinstance(runtime, Mapping) else {}
    return _none_or_str(runtime_map.get("parent_task_id")) or _none_or_str(
        data.get("parent_task_id")
    )


def _metadata_agent_id(data: Mapping[str, Any]) -> str | None:
    runtime = data.get("runtime") if isinstance(data.get("runtime"), Mapping) else {}
    runtime_map = runtime if isinstance(runtime, Mapping) else {}
    return _none_or_str(runtime_map.get("agent_id")) or _none_or_str(data.get("agent_id"))


def _metadata_generation(data: Mapping[str, Any], default: int) -> int:
    runtime = data.get("runtime") if isinstance(data.get("runtime"), Mapping) else {}
    runtime_map = runtime if isinstance(runtime, Mapping) else {}
    return _safe_int(
        data.get("transcript_generation"),
        _safe_int(runtime_map.get("transcript_generation"), default),
    )


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is not None:
            return max(0, int(value))
    except (TypeError, ValueError):
        pass
    return max(0, int(default))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]


def _none_or_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
