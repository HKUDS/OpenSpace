"""Skill lifecycle evidence adapter."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .store import EvidenceStore
from .types import EvidenceEvent, ResourceRef


class SkillEvidenceAdapter:
    """Translate SkillStore lifecycle facts into evidence refs."""

    def __init__(self, store: EvidenceStore) -> None:
        self._store = store

    async def on_skill_store_event(
        self,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        event = self.build_event(event_type, data)
        if event is not None:
            self._store.ingest_event(event)

    async def ingest_skill_store_delta(
        self,
        skill_store: Any,
        *,
        task_id: str | None = None,
        turn_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 200,
    ) -> None:
        """Backfill persisted skill lifecycle rows after session persist."""

        load_skill_events = getattr(skill_store, "load_skill_events", None)
        if not callable(load_skill_events):
            return
        rows = load_skill_events(
            task_id=task_id,
            turn_id=turn_id,
            agent_id=agent_id,
            limit=limit,
        )
        for row in rows or []:
            if isinstance(row, Mapping):
                await self.on_skill_store_event("skill_event", dict(row))

    def build_event(
        self,
        event_type: str,
        data: Mapping[str, Any],
    ) -> EvidenceEvent | None:
        if event_type == "skill_event":
            return self._skill_event(data)
        if event_type == "skill_record":
            return self._skill_record_event(data)
        return None

    def _skill_event(self, data: Mapping[str, Any]) -> EvidenceEvent | None:
        skill_id = _none_or_str(data.get("skill_id"))
        lifecycle_type = _none_or_str(data.get("event_type"))
        if not skill_id or not lifecycle_type:
            return None
        row_id = _none_or_str(data.get("row_id")) or _digest(data)[:12]
        created_at = _none_or_str(data.get("created_at")) or _utc_now()
        task_id = _none_or_str(data.get("task_id"))
        turn_id = _none_or_str(data.get("turn_id"))
        agent_id = _none_or_str(data.get("agent_id"))
        event_metadata = (
            dict(data.get("metadata"))
            if isinstance(data.get("metadata"), Mapping)
            else {}
        )
        session_id = _none_or_str(data.get("session_id") or event_metadata.get("session_id"))
        metadata = {
            "skill_id": skill_id,
            "skill_name": data.get("skill_name"),
            "event_type": lifecycle_type,
            "source": data.get("source"),
            "session_id": session_id,
            "task_id": task_id,
            "turn_id": turn_id,
            "agent_id": agent_id,
            "query": data.get("query"),
            "metadata": event_metadata,
        }
        metadata.update(_skill_event_link_metadata(event_metadata))
        ref = ResourceRef(
            ref_id=f"skill_event:{skill_id}:{lifecycle_type}:{row_id}",
            ref_type="skill_event",
            session_id=session_id,
            task_id=task_id,
            turn_id=turn_id,
            agent_id=agent_id,
            producer="skill_store",
            created_at=created_at,
            reliability="persisted",
            role="supporting",
            preview=f"{skill_id} {lifecycle_type}",
            metadata=metadata,
            raw_backrefs=_skill_event_raw_backrefs(data, event_metadata),
        )
        return EvidenceEvent.create(
            event_id=f"evt_skill_event_{_digest(ref.ref_id)}",
            event_type="skill_lifecycle_event",
            producer="skill_store",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            turn_id=turn_id,
            agent_id=agent_id,
            idempotency_key=f"skill:event:{ref.ref_id}",
            supporting_refs=[ref],
            metadata={"skill_id": skill_id, "event_type": lifecycle_type},
        )

    def _skill_record_event(self, data: Mapping[str, Any]) -> EvidenceEvent | None:
        skill_id = _none_or_str(data.get("skill_id"))
        if not skill_id:
            return None
        created_at = _none_or_str(data.get("created_at")) or _utc_now()
        record_hash = _digest(data)
        metadata = {
            key: value
            for key, value in data.items()
            if key not in {"content_snapshot"}
        }
        if "generation" not in metadata and "lineage_generation" in metadata:
            metadata["generation"] = metadata.get("lineage_generation")
        record_ref = ResourceRef(
            ref_id=f"skill_record:{skill_id}:{record_hash[:16]}",
            ref_type="skill_record",
            uri=_none_or_str(data.get("path")),
            producer="skill_store",
            created_at=created_at,
            reliability="persisted",
            role="supporting",
            preview=str(data.get("description") or data.get("name") or skill_id)[:300],
            metadata=metadata,
        )
        refs = [record_ref]
        skill_file_ref = _skill_file_ref(data, created_at=created_at)
        if skill_file_ref is not None:
            refs.append(skill_file_ref)
        return EvidenceEvent.create(
            event_id=f"evt_skill_record_{_digest(record_ref.ref_id)}",
            event_type="skill_record_snapshot",
            producer="skill_store",
            created_at=created_at,
            idempotency_key=f"skill:record:{record_ref.ref_id}",
            supporting_refs=refs,
            metadata={"skill_id": skill_id},
        )


def _skill_file_ref(data: Mapping[str, Any], *, created_at: str) -> ResourceRef | None:
    skill_id = _none_or_str(data.get("skill_id"))
    path_text = _none_or_str(data.get("path"))
    if not skill_id or not path_text:
        return None
    path = Path(path_text).expanduser()
    file_path = path if path.name == "SKILL.md" else path / "SKILL.md"
    if not file_path.exists():
        return None
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    preview = "\n".join(content.splitlines()[:20])[:800]
    return ResourceRef(
        ref_id=f"skill_file:{skill_id}:{file_hash[:16]}",
        ref_type="skill_file",
        uri=str(file_path),
        producer="skill_store",
        created_at=created_at,
        reliability="persisted",
        role="supporting",
        hash=file_hash,
        preview=preview,
        metadata={"skill_id": skill_id, "path": str(file_path)},
    )


def _skill_event_link_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "skill_scope_id",
        "skill_invocation_scope_id",
        "invocation_scope_id",
        "invocation_tool_use_id",
        "invocation_tool_event_ref_id",
        "skill_event_ref_id",
    ):
        value = _none_or_str(metadata.get(key))
        if value:
            result[key] = value
    allowed_tools = _stable_strings(metadata.get("allowed_tools"))
    if allowed_tools:
        result["allowed_tools"] = allowed_tools
    return result


def _skill_event_raw_backrefs(
    data: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> list[str]:
    refs = [
        *_stable_strings(data.get("raw_backrefs")),
        *_stable_strings(metadata.get("raw_backrefs")),
        *_stable_strings(metadata.get("raw_backref")),
    ]
    invocation_ref = _none_or_str(metadata.get("invocation_tool_event_ref_id"))
    if invocation_ref:
        refs.append(invocation_ref)
    return _stable_strings(refs)


def _stable_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, Mapping):
        candidates = list(value.values())
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        candidates = [value]
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


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
