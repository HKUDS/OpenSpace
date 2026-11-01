"""Memory and background-housekeeping evidence adapter."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .memory_refs import canonical_path_hash, memory_ref_id
from .store import EvidenceStore
from .types import EvidenceEvent, ResourceRef


_MEMORY_EVENT_TYPES = {
    "memory_written",
    "memory_prefetch_consumed",
    "nested_memory_consumed",
    "memory_saved",
    "memory_logged",
    "session_memory_extraction_coalesced",
    "session_memory_extraction_trailing_start",
    "session_memory_extraction_start",
    "session_memory_extraction_complete",
    "session_memory_extraction_error",
    "session_memory_extraction_skipped",
    "session_memory_updated",
    "session_memory_checked",
    "memory_extraction_coalesced",
    "memory_extraction_start",
    "memory_extraction_complete",
    "memory_extraction_error",
    "memory_extraction_skipped",
    "memory_extraction_trailing_start",
    "auto_dream_start",
    "auto_dream_progress",
    "auto_dream_complete",
    "auto_dream_cancelled",
    "auto_dream_error",
    "auto_dream_skipped",
    "manual_dream_start",
    "manual_dream_progress",
    "manual_dream_complete",
    "manual_dream_cancelled",
    "manual_dream_error",
    "manual_dream_skipped",
}


class MemoryEvidenceAdapter:
    """Translate memory visibility/write/background facts into evidence."""

    def __init__(self, store: EvidenceStore) -> None:
        self._store = store

    def build_event(
        self,
        event_type: str,
        data: Mapping[str, Any],
    ) -> EvidenceEvent | None:
        if event_type not in _MEMORY_EVENT_TYPES:
            return None
        session_id = _none_or_str(data.get("session_id"))
        task_id = _none_or_str(data.get("task_id"))
        parent_task_id = _none_or_str(data.get("parent_task_id"))
        agent_id = _none_or_str(data.get("agent_id"))
        created_at = _utc_now()
        refs = _memory_refs(event_type, data, created_at=created_at)
        background_ref = _background_task_ref(event_type, data, created_at=created_at)
        if background_ref is not None:
            refs.append(background_ref)
        if not refs:
            if not _allow_synthetic_memory_ref(event_type):
                return None
            refs = [
                ResourceRef(
                    ref_id=f"memory_ref:{event_type}:{_digest(data)}",
                    ref_type="memory_ref",
                    session_id=session_id,
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                    agent_id=agent_id,
                    producer="memory",
                    created_at=created_at,
                    reliability="derived",
                    role="supporting",
                    preview=event_type,
                    metadata=_metadata_without_content(data, event_type=event_type),
                )
            ]
        return EvidenceEvent.create(
            event_id=f"evt_memory_{_digest({'type': event_type, 'data': data})}",
            event_type=event_type,
            producer="memory",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            idempotency_key=(
                "memory:event:"
                f"{event_type}:{session_id or ''}:{task_id or ''}:{_digest(data)}"
            ),
            supporting_refs=refs,
            metadata={
                "memory_event_type": event_type,
                "background_status": _background_status(event_type),
            },
        )


def background_drain_event(data: Mapping[str, Any]) -> EvidenceEvent:
    created_at = _utc_now()
    session_id = _none_or_str(data.get("session_id"))
    task_id = _none_or_str(data.get("task_id"))
    reason = _none_or_str(data.get("reason")) or "unknown"
    ref = ResourceRef(
        ref_id=f"background_task_result:{session_id or 'none'}:{task_id or 'none'}:{reason}:{_digest(data)[:12]}",
        ref_type="background_task_result",
        session_id=session_id,
        task_id=task_id,
        producer="runtime",
        created_at=created_at,
        reliability="runtime",
        role="supporting",
        preview=f"background drain {reason}",
        metadata={
            "reason": reason,
            "timeout_s": data.get("timeout_s"),
            "pending_count": data.get("pending_count"),
            "timed_out": bool(data.get("timed_out")),
            "session_memory_pending": data.get("session_memory_pending"),
            "memory_extraction_pending": (
                data.get("memory_extraction_pending")
                if "memory_extraction_pending" in data
                else data.get("extraction_pending")
            ),
            "auto_dream_pending": data.get("auto_dream_pending"),
            "tracked_pending": data.get("tracked_pending"),
        },
    )
    return EvidenceEvent.create(
        event_id=f"evt_background_drain_{_digest(ref.ref_id)}",
        event_type="background_drain",
        producer="runtime",
        created_at=created_at,
        session_id=session_id,
        task_id=task_id,
        idempotency_key=f"runtime:background_drain:{ref.ref_id}",
        supporting_refs=[ref],
        metadata={"reason": reason},
    )


def _memory_refs(
    event_type: str,
    data: Mapping[str, Any],
    *,
    created_at: str,
) -> list[ResourceRef]:
    paths: list[str] = []
    for key in ("file_path", "entrypoint_path", "memory_dir", "memory_path"):
        value = _none_or_str(data.get(key))
        if value:
            paths.append(value)
    for key in (
        "files_touched",
        "paths",
        "memory_paths",
        "written_paths",
        "log_paths",
    ):
        raw = data.get(key)
        if isinstance(raw, (list, tuple, set)):
            paths.extend(str(item) for item in raw if item)
    refs: list[ResourceRef] = []
    session_id = _none_or_str(data.get("session_id"))
    task_id = _none_or_str(data.get("task_id"))
    parent_task_id = _none_or_str(data.get("parent_task_id"))
    agent_id = _none_or_str(data.get("agent_id"))
    for path in dict.fromkeys(paths):
        source_kind = _memory_ref_source_kind(event_type)
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
                producer="memory",
                created_at=created_at,
                reliability="persisted" if Path(path).expanduser().exists() else "runtime",
                role="supporting",
                hash=_file_hash(path),
                preview=f"{event_type}: {Path(path).name}",
                metadata=_metadata_without_content(
                    data,
                    event_type=event_type,
                    path=path,
                    source_kind=source_kind,
                ),
            )
        )
    return refs


def _background_task_ref(
    event_type: str,
    data: Mapping[str, Any],
    *,
    created_at: str,
) -> ResourceRef | None:
    if not _is_background_memory_event(event_type):
        return None
    session_id = _none_or_str(data.get("session_id"))
    task_id = _none_or_str(data.get("task_id"))
    parent_task_id = _none_or_str(data.get("parent_task_id"))
    agent_id = _none_or_str(data.get("agent_id"))
    status = _background_status(event_type)
    task_kind = _background_task_kind(event_type)
    ref_key = _digest(
        {
            "event_type": event_type,
            "session_id": session_id,
            "task_id": task_id,
            "status": status,
            "data": _metadata_without_content(data, event_type=event_type),
        }
    )[:16]
    return ResourceRef(
        ref_id=(
            "background_task_result:"
            f"{session_id or 'none'}:{task_id or 'none'}:{task_kind}:{ref_key}"
        ),
        ref_type="background_task_result",
        session_id=session_id,
        task_id=task_id,
        parent_task_id=parent_task_id,
        agent_id=agent_id,
        producer="memory",
        created_at=created_at,
        reliability="runtime",
        role="supporting",
        preview=f"{task_kind} {status}",
        metadata={
            "task_kind": task_kind,
            "status": status,
            "source_event": event_type,
            "session_id": session_id,
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "agent_id": agent_id,
            "reason": data.get("reason") or data.get("skipped_reason"),
            "duration_ms": data.get("duration_ms"),
            "turn_count": data.get("turn_count"),
            "message_count": data.get("message_count"),
            "files_written": data.get("files_written"),
            "files_touched": data.get("files_touched"),
            "memory_mode": data.get("memory_mode"),
            "error": str(data.get("error") or "")[:500],
        },
    )


def _is_background_memory_event(event_type: str) -> bool:
    return event_type.startswith(
        (
            "session_memory_extraction",
            "memory_extraction",
            "auto_dream",
            "manual_dream",
        )
    )


def _background_task_kind(event_type: str) -> str:
    if event_type.startswith("session_memory"):
        return "session_memory"
    if event_type.startswith("memory_extraction"):
        return "memory_extraction"
    if event_type.startswith("manual_dream"):
        return "manual_dream"
    if event_type.startswith("auto_dream"):
        return "auto_dream"
    return "memory"


def _background_status(event_type: str) -> str | None:
    suffix = event_type.rsplit("_", 1)[-1]
    if suffix in {"start", "progress", "complete", "cancelled", "error", "skipped"}:
        return {
            "start": "running",
            "progress": "running",
            "complete": "complete",
            "cancelled": "cancelled",
            "error": "error",
            "skipped": "skipped",
        }[suffix]
    if event_type.endswith("_coalesced"):
        return "skipped"
    if event_type.endswith("_trailing_start"):
        return "running"
    return None


def _file_hash(path_text: str) -> str | None:
    try:
        path = Path(path_text).expanduser()
        if not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return None


def _metadata_without_content(
    data: Mapping[str, Any],
    *,
    event_type: str,
    path: str | None = None,
    source_kind: str | None = None,
) -> dict[str, Any]:
    excluded = {"content", "messages", "prompt", "response"}
    metadata = {key: value for key, value in data.items() if key not in excluded}
    metadata["memory_event_type"] = event_type
    metadata["memory_kind"] = _memory_kind(event_type, data, path=path)
    metadata["loaded_in_context"] = _loaded_in_context(event_type, data)
    metadata["read_or_written_by_tool"] = _read_or_written_by_tool(event_type, data)
    metadata["source_event"] = event_type
    metadata["source_kind"] = source_kind or _memory_ref_source_kind(event_type)
    if path is not None:
        metadata["path"] = path
        metadata["canonical_path_hash"] = canonical_path_hash(path)
    return metadata


def _memory_ref_source_kind(event_type: str) -> str:
    return event_type


def _memory_kind(
    event_type: str,
    data: Mapping[str, Any],
    *,
    path: str | None,
) -> str:
    explicit = _none_or_str(data.get("memory_kind") or data.get("memory_type"))
    if explicit:
        return explicit
    if event_type.startswith("session_memory"):
        return "session"
    if event_type in {"memory_logged"} or (path and "/logs/" in path):
        return "daily_log"
    if event_type.startswith("auto_dream") or event_type.startswith("manual_dream"):
        return "dream"
    if event_type == "nested_memory_consumed":
        return "nested"
    if event_type == "memory_prefetch_consumed":
        return "relevant"
    if _none_or_str(data.get("memory_mode")) == "daily_log":
        return "daily_log"
    return "memory"


def _loaded_in_context(event_type: str, data: Mapping[str, Any]) -> bool:
    if "loaded_in_context" in data:
        return bool(data.get("loaded_in_context"))
    return event_type in {"memory_prefetch_consumed", "nested_memory_consumed"}


def _read_or_written_by_tool(event_type: str, data: Mapping[str, Any]) -> bool:
    if "read_or_written_by_tool" in data:
        return bool(data.get("read_or_written_by_tool"))
    return event_type in {"memory_written", "memory_logged"}


def _allow_synthetic_memory_ref(event_type: str) -> bool:
    return event_type not in {"memory_prefetch_consumed", "nested_memory_consumed"}


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
