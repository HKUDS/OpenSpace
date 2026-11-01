"""Tool execution evidence adapter."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .store import EvidenceStore
from .types import EvidenceEvent, ResourceRef

_MAX_HISTORY_INCIDENTS = 10
_MAX_HISTORY_INCIDENTS_PER_BUCKET = 2


class ToolEvidenceAdapter:
    """Translate final tool-pipeline facts into evidence refs."""

    def __init__(self, store: EvidenceStore) -> None:
        self._store = store

    def build_event(
        self,
        event_type: str,
        data: Mapping[str, Any],
    ) -> EvidenceEvent | None:
        if event_type == "tool_pipeline_complete":
            return self._tool_pipeline_complete(data)
        if event_type in {"tool_quality_recorded", "tool_quality_record"}:
            return self._tool_quality_recorded(data)
        return None

    def _tool_pipeline_complete(
        self,
        data: Mapping[str, Any],
    ) -> EvidenceEvent | None:
        session_id = _none_or_str(data.get("session_id"))
        task_id = _none_or_str(data.get("task_id"))
        parent_task_id = _none_or_str(data.get("parent_task_id"))
        agent_id = _none_or_str(data.get("agent_id")) or "primary"
        tool_use_id = _none_or_str(data.get("tool_use_id")) or "unknown"
        tool_name = _none_or_str(data.get("tool_name")) or "unknown"
        backend = _none_or_str(data.get("backend")) or "unknown"
        server_name = _none_or_str(data.get("server_name")) or "default"
        status = _status(data)
        created_at = _utc_now()
        tool_key = f"{backend}:{server_name}:{tool_name}"

        event_ref_id = (
            "tool_event:"
            f"{session_id or 'none'}:{task_id or 'none'}:{agent_id}:{tool_use_id}"
        )
        metadata = {
            "agent_id": agent_id,
            "parent_task_id": parent_task_id,
            "current_iteration": data.get("current_iteration"),
            "tool_use_id": tool_use_id,
            "tool_key": tool_key,
            "tool_name": tool_name,
            "backend": backend,
            "server_name": server_name,
            "status": status,
            "duration_ms": data.get("total_duration_ms"),
            "execution_time_ms": data.get("execution_time_ms"),
            "error_type": data.get("error_type"),
            "permission_status": data.get("permission_status"),
            "result_size_chars": data.get("result_size_chars"),
            "message_count": data.get("message_count"),
            "prevent_continuation": bool(data.get("prevent_continuation", False)),
        }
        metadata.update(_skill_scope_metadata(data))
        metadata.update(_tool_output_path_metadata(data))
        if data.get("input_preview"):
            metadata["input_preview"] = str(data.get("input_preview"))[:500]
        result_preview = _none_or_str(data.get("result_preview"))
        has_persisted_result = _persisted_path_from(data) is not None
        preview = f"{tool_name} {status}"
        if result_preview and not has_persisted_result:
            preview = f"{preview}: {result_preview[:300]}"

        refs = [
            ResourceRef(
                ref_id=event_ref_id,
                ref_type="tool_event",
                session_id=session_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
                agent_id=agent_id,
                producer="tool_runtime",
                created_at=created_at,
                reliability="runtime",
                role="primary",
                preview=preview,
                metadata=metadata,
                raw_backrefs=_active_skill_event_backrefs(data),
            )
        ]

        persisted_ref = self.persisted_tool_result_ref(
            data,
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            tool_key=tool_key,
            raw_backref=event_ref_id,
        )
        if persisted_ref is not None:
            refs.append(persisted_ref)
        output_file_ref = self._tool_output_file_ref(
            data,
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            tool_key=tool_key,
            raw_backref=event_ref_id,
        )
        if (
            output_file_ref is not None
            and all(ref.uri != output_file_ref.uri for ref in refs)
        ):
            refs.append(output_file_ref)
        primary_refs, supporting_refs, derived_refs = _refs_by_event_role(refs)

        digest = _digest(
            {
                "session_id": session_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "tool_use_id": tool_use_id,
                "status": status,
            }
        )
        return EvidenceEvent.create(
            event_id=f"evt_tool_{digest}",
            event_type="tool_pipeline_complete",
            producer="tool_runtime",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            idempotency_key=(
                "tool:pipeline_complete:"
                f"{session_id or ''}:{task_id or ''}:{agent_id}:{tool_use_id}"
            ),
            primary_refs=primary_refs,
            supporting_refs=supporting_refs,
            derived_refs=derived_refs,
            metadata={
                "tool_name": tool_name,
                "tool_key": tool_key,
                "status": status,
            },
        )

    async def ingest_quality_delta(self, quality_source: Any, *, limit: int = 20) -> None:
        """Backfill quality rows from a ToolQualityManager or QualityStore."""

        for payload in _quality_payloads_from_source(quality_source, limit=limit):
            event = self._tool_quality_recorded(payload)
            if event is not None:
                self._store.ingest_event(event)

    def persisted_tool_result_ref(
        self,
        data: Mapping[str, Any],
        *,
        created_at: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        parent_task_id: str | None = None,
        agent_id: str | None = None,
        tool_use_id: str | None = None,
        tool_name: str | None = None,
        tool_key: str | None = None,
        raw_backref: str | None = None,
    ) -> ResourceRef | None:
        tool_result_metadata = data.get("tool_result_metadata")
        if not isinstance(tool_result_metadata, Mapping):
            message_meta = data.get("message_meta")
            if isinstance(message_meta, Mapping):
                tool_result_metadata = message_meta.get("tool_result_metadata")
        if not isinstance(tool_result_metadata, Mapping):
            return None
        persisted_path = _none_or_str(tool_result_metadata.get("persisted_path"))
        if not persisted_path:
            return None

        path_hash = _digest(persisted_path)[:16]
        file_hash, preview, missing = _file_hash_preview_missing(persisted_path)
        identity_hash = (file_hash or path_hash)[:16]
        result_ref_id = (
            "tool_result:"
            f"{session_id or 'none'}:{task_id or 'none'}:"
            f"{agent_id or 'primary'}:{tool_use_id or 'unknown'}:{identity_hash}"
        )
        metadata = {
            "agent_id": agent_id or "primary",
            "parent_task_id": parent_task_id,
            "current_iteration": data.get("current_iteration"),
            "original_length": tool_result_metadata.get("original_length"),
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "tool_key": tool_key,
            "persisted_path": persisted_path,
            "persistence_source": tool_result_metadata.get("persistence_source"),
            "persisted_path_hash": path_hash,
            "missing": missing,
        }
        reliability = "fallback" if missing else "persisted"
        role = "supporting" if missing else "primary"
        return ResourceRef(
            ref_id=result_ref_id,
            ref_type="tool_result",
            uri=persisted_path,
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id or "primary",
            producer="tool_runtime",
            created_at=created_at or _utc_now(),
            reliability=reliability,
            role=role,
            hash=file_hash,
            preview=preview,
            metadata=metadata,
            raw_backrefs=[raw_backref] if raw_backref else [],
        )

    def _tool_output_file_ref(
        self,
        data: Mapping[str, Any],
        *,
        created_at: str,
        session_id: str | None,
        task_id: str | None,
        parent_task_id: str | None,
        agent_id: str | None,
        tool_use_id: str | None,
        tool_name: str | None,
        tool_key: str | None,
        raw_backref: str | None,
    ) -> ResourceRef | None:
        output_metadata = _tool_output_path_metadata(data)
        path = _none_or_str(
            output_metadata.get("background_output_path")
            or output_metadata.get("output_file_path")
            or output_metadata.get("persisted_output_path")
        )
        if not path:
            return None
        file_hash, preview, missing = _file_hash_preview_missing(path)
        path_hash = _digest(path)[:16]
        source = (
            "background_shell_output"
            if output_metadata.get("background_output_path")
            else "shell_output_file"
        )
        ref_id = (
            "tool_result:"
            f"{session_id or 'none'}:{task_id or 'none'}:"
            f"{agent_id or 'primary'}:{tool_use_id or 'unknown'}:"
            f"{source}:{(file_hash or path_hash)[:16]}"
        )
        metadata = {
            **output_metadata,
            "agent_id": agent_id or "primary",
            "parent_task_id": parent_task_id,
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "tool_key": tool_key,
            "persistence_source": source,
            "missing": missing,
        }
        return ResourceRef(
            ref_id=ref_id,
            ref_type="tool_result",
            uri=path,
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id or "primary",
            producer="tool_runtime",
            created_at=created_at,
            reliability="fallback" if missing else "persisted",
            role="supporting",
            hash=file_hash,
            preview=preview,
            metadata=metadata,
            raw_backrefs=[raw_backref] if raw_backref else [],
        )

    def _tool_quality_recorded(self, data: Mapping[str, Any]) -> EvidenceEvent | None:
        tool_key = _none_or_str(data.get("tool_key"))
        if not tool_key:
            backend = _none_or_str(data.get("backend")) or "unknown"
            server = _none_or_str(data.get("server") or data.get("server_name")) or "default"
            tool_name = _none_or_str(data.get("tool_name")) or "unknown"
            tool_key = f"{backend}:{server}:{tool_name}"
        created_at = _none_or_str(data.get("created_at")) or _utc_now()
        last_updated = _none_or_str(data.get("last_updated")) or created_at
        session_id = _none_or_str(data.get("session_id"))
        task_id = _none_or_str(data.get("task_id"))
        parent_task_id = _none_or_str(data.get("parent_task_id"))
        agent_id = _none_or_str(data.get("agent_id"))
        scope_metadata = _skill_scope_metadata(data)
        scope_backrefs = _active_skill_event_backrefs(data)
        record_metadata = {
            "tool_key": tool_key,
            "backend": data.get("backend"),
            "server": data.get("server") or data.get("server_name"),
            "tool_name": data.get("tool_name"),
            "total_calls": data.get("total_calls"),
            "success_count": data.get("success_count"),
            "recent_success_rate": data.get("recent_success_rate"),
            "last_updated": last_updated,
            "source": data.get("source") or "tool_quality",
        }
        record_metadata.update(scope_metadata)

        record_ref = ResourceRef(
            ref_id=f"tool_quality_record:{tool_key}:{_digest(last_updated)[:16]}",
            ref_type="tool_quality_record",
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            producer="tool_quality",
            created_at=created_at,
            reliability="persisted",
            role="supporting",
            preview=f"{tool_key} success_rate={data.get('recent_success_rate')}",
            metadata=record_metadata,
            raw_backrefs=scope_backrefs,
        )

        incident_refs = [
            _tool_incident_ref(
                tool_key,
                item,
                created_at=created_at,
                session_id=session_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
                agent_id=agent_id,
                scope_metadata=scope_metadata,
                raw_backrefs=scope_backrefs,
            )
            for item in _history_items(data)
        ]
        incident_refs = [ref for ref in incident_refs if ref is not None]

        digest = _digest(
            {
                "tool_key": tool_key,
                "last_updated": last_updated,
                "incident_refs": [ref.ref_id for ref in incident_refs],
            }
        )
        return EvidenceEvent.create(
            event_id=f"evt_tool_quality_{digest}",
            event_type="tool_quality_recorded",
            producer="tool_quality",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            idempotency_key=f"tool:quality:{tool_key}:{last_updated}:{digest}",
            supporting_refs=[record_ref, *incident_refs],
            metadata={
                "tool_key": tool_key,
                "recent_success_rate": data.get("recent_success_rate"),
            },
        )


def _status(data: Mapping[str, Any]) -> str:
    raw = _none_or_str(data.get("status"))
    if raw:
        return raw
    message_meta = data.get("message_meta")
    if isinstance(message_meta, Mapping):
        raw = _none_or_str(message_meta.get("status"))
        if raw:
            return raw
    return "error" if data.get("error_type") else "success"


def _tool_output_path_metadata(data: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for source in _tool_metadata_sources(data):
        for key in (
            "output_file_path",
            "background_output_path",
            "background_task_id",
            "background_task_type",
            "background_semantics",
            "persisted_output_path",
            "persisted_output_size",
        ):
            value = source.get(key)
            if value is not None and key not in metadata:
                metadata[key] = value
    return metadata


def _tool_metadata_sources(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    sources: list[Mapping[str, Any]] = []
    tool_result_metadata = data.get("tool_result_metadata")
    if isinstance(tool_result_metadata, Mapping):
        sources.append(tool_result_metadata)
    message_meta = data.get("message_meta")
    if isinstance(message_meta, Mapping):
        sources.append(message_meta)
        nested = message_meta.get("tool_result_metadata")
        if isinstance(nested, Mapping):
            sources.append(nested)
    return sources


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


def _persisted_path_from(data: Mapping[str, Any]) -> str | None:
    tool_result_metadata = data.get("tool_result_metadata")
    if not isinstance(tool_result_metadata, Mapping):
        message_meta = data.get("message_meta")
        if isinstance(message_meta, Mapping):
            tool_result_metadata = message_meta.get("tool_result_metadata")
    if not isinstance(tool_result_metadata, Mapping):
        return None
    return _none_or_str(tool_result_metadata.get("persisted_path"))


def _file_hash_preview_missing(path_text: str) -> tuple[str | None, str, bool]:
    try:
        path = Path(path_text).expanduser()
        if not path.is_file():
            return None, "", True
        digest = hashlib.sha256()
        preview_bytes = b""
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                if len(preview_bytes) < 2000:
                    preview_bytes += chunk[: 2000 - len(preview_bytes)]
                digest.update(chunk)
        return digest.hexdigest(), preview_bytes.decode("utf-8", errors="replace"), False
    except Exception:
        return None, "", True


def _history_items(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = data.get("history") or data.get("recent_executions")
    if isinstance(raw, Mapping):
        raw = [raw]
    items: list[Mapping[str, Any]] = []
    if isinstance(raw, (list, tuple)):
        for item in _representative_history_items(raw):
            if isinstance(item, Mapping):
                items.append(item)
    incident = data.get("incident")
    if isinstance(incident, Mapping):
        items.append(incident)
    return items


def _representative_history_items(raw: list[Any] | tuple[Any, ...]) -> list[Mapping[str, Any]]:
    items = [item for item in raw if isinstance(item, Mapping)]
    if len(items) <= _MAX_HISTORY_INCIDENTS:
        return items

    failures = [
        item
        for item in items
        if not bool(item.get("success"))
        or bool(item.get("llm_flagged"))
        or bool(_none_or_str(item.get("error_message") or item.get("error")))
    ]
    source = failures or items
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for item in source:
        grouped.setdefault(_history_error_bucket(item), []).append(item)

    selected: list[Mapping[str, Any]] = []
    for bucket in sorted(grouped):
        for item in grouped[bucket][:_MAX_HISTORY_INCIDENTS_PER_BUCKET]:
            selected.append(item)
            if len(selected) >= _MAX_HISTORY_INCIDENTS:
                return selected
    return selected


def _history_error_bucket(item: Mapping[str, Any]) -> str:
    text = _none_or_str(
        item.get("error_bucket")
        or item.get("failure_mode")
        or item.get("error_type")
        or item.get("error_message")
        or item.get("error")
    )
    return _error_bucket(text or "")


def _error_bucket(text: str) -> str:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return "unknown"
    if "selector" in normalized or "element not found" in normalized:
        return "selector_missing"
    if "timeout" in normalized or "timed out" in normalized:
        return "timeout"
    if (
        "auth" in normalized
        or "login" in normalized
        or "session expired" in normalized
        or "unauthorized" in normalized
        or "forbidden" in normalized
    ):
        return "auth_or_session"
    if "permission" in normalized or "denied" in normalized:
        return "permission_denied"
    if "network" in normalized or "connection" in normalized or "dns" in normalized:
        return "network"
    return "_".join(normalized.split()[:4])[:80] or "unknown"


def _tool_incident_ref(
    tool_key: str,
    item: Mapping[str, Any],
    *,
    created_at: str,
    session_id: str | None,
    task_id: str | None,
    parent_task_id: str | None,
    agent_id: str | None,
    scope_metadata: Mapping[str, Any] | None = None,
    raw_backrefs: list[str] | None = None,
) -> ResourceRef | None:
    success_raw = item.get("success")
    success = bool(success_raw) if success_raw is not None else False
    error_message = _none_or_str(item.get("error_message") or item.get("error"))
    if success and not error_message:
        return None
    timestamp = _none_or_str(item.get("timestamp")) or created_at
    error_bucket = _history_error_bucket(item)
    history_row_id = _none_or_str(item.get("id") or item.get("history_row_id"))
    tool_use_id = _none_or_str(
        item.get("tool_use_id")
        or item.get("tool_call_id")
        or item.get("call_id")
    )
    incident_id = _stable_tool_incident_id(
        tool_key,
        timestamp=timestamp,
        tool_use_id=tool_use_id,
        error_bucket=error_bucket,
    )
    metadata = {
        "tool_key": tool_key,
        "incident_id": incident_id,
        "history_row_id": history_row_id,
        "timestamp": timestamp,
        "tool_use_id": tool_use_id,
        "success": success,
        "execution_time_ms": item.get("execution_time_ms"),
        "error_message": str(error_message or "")[:500],
        "error_bucket": error_bucket,
        "failure_mode": item.get("failure_mode") or error_bucket,
        "source": item.get("source") or "tool_execution_history",
    }
    metadata.update(dict(scope_metadata or {}))
    return ResourceRef(
        ref_id=f"tool_incident:{tool_key}:{incident_id}",
        ref_type="tool_incident",
        session_id=session_id,
        task_id=task_id,
        parent_task_id=parent_task_id,
        agent_id=agent_id,
        producer="tool_quality",
        created_at=created_at,
        reliability="persisted",
        role="supporting",
        preview=f"{tool_key} failure {str(error_message or '')[:200]}",
        metadata=metadata,
        raw_backrefs=list(raw_backrefs or []),
    )


def _stable_tool_incident_id(
    tool_key: str,
    *,
    timestamp: str | None,
    tool_use_id: str | None,
    error_bucket: str,
) -> str:
    return _digest(
        {
            "tool_key": tool_key,
            "timestamp": timestamp or "",
            "tool_use_id": tool_use_id or "",
            "error_bucket": error_bucket,
        }
    )[:16]


def _quality_payloads_from_source(source: Any, *, limit: int) -> list[dict[str, Any]]:
    records = None
    store = None
    if hasattr(source, "load_all"):
        store = source
        try:
            records, _global_count = source.load_all()
        except Exception:
            records = None
    else:
        records = getattr(source, "_records", None)
        store = getattr(source, "_store", None)
    if not isinstance(records, Mapping):
        return []

    payloads: list[dict[str, Any]] = []
    for record in records.values():
        payload = _quality_record_payload(record)
        if payload is None:
            continue
        history: list[dict[str, Any]] = []
        if store is not None and hasattr(store, "load_recent_history"):
            try:
                history = list(store.load_recent_history(record.tool_key, limit=limit))
            except Exception:
                history = []
        if not history:
            history = [_execution_payload(item) for item in getattr(record, "recent_executions", [])[-limit:]]
        payload["history"] = [item for item in history if item]
        payloads.append(payload)
    return payloads


def _quality_record_payload(record: Any) -> dict[str, Any] | None:
    tool_key = _none_or_str(getattr(record, "tool_key", None))
    if not tool_key:
        return None
    return {
        "tool_key": tool_key,
        "backend": getattr(record, "backend", None),
        "server": getattr(record, "server", None),
        "tool_name": getattr(record, "tool_name", None),
        "total_calls": getattr(record, "total_calls", None),
        "success_count": getattr(record, "success_count", None),
        "recent_success_rate": getattr(record, "recent_success_rate", None),
        "last_updated": _isoformat_or_none(getattr(record, "last_updated", None)),
        "source": "quality_store_checkpoint",
    }


def _execution_payload(record: Any) -> dict[str, Any]:
    timestamp = getattr(record, "timestamp", None)
    return {
        "timestamp": _isoformat_or_none(timestamp),
        "success": getattr(record, "success", None),
        "execution_time_ms": getattr(record, "execution_time_ms", None),
        "error_message": getattr(record, "error_message", None),
    }


def _isoformat_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _skill_scope_metadata(data: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in (
        "skill_id",
        "active_skill_id",
        "skill_scope_id",
        "skill_invocation_scope_id",
        "skill_event_ref_id",
    ):
        value = _none_or_str(data.get(key))
        if value:
            metadata[key] = value

    for key in ("active_skill_ids", "skill_scope_ids", "skill_event_ref_ids"):
        values = _stable_strings(data.get(key))
        if values:
            metadata[key] = values

    scope_summaries = _scope_summaries(data.get("active_skill_scopes"))
    if scope_summaries:
        metadata["active_skill_scopes"] = scope_summaries
    return metadata


def _active_skill_event_backrefs(data: Mapping[str, Any]) -> list[str]:
    refs = _stable_strings(data.get("skill_event_ref_ids"))
    single = _none_or_str(data.get("skill_event_ref_id"))
    if single:
        refs.append(single)
    return _stable_strings(refs)


def _scope_summaries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    summaries: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        summary: dict[str, Any] = {}
        for source_key, target_key in (
            ("skill_id", "skill_id"),
            ("skill_scope_id", "skill_scope_id"),
            ("scope_id", "skill_scope_id"),
            ("name", "name"),
            ("execution_mode", "execution_mode"),
            ("invocation_tool_use_id", "invocation_tool_use_id"),
            ("skill_event_ref_id", "skill_event_ref_id"),
        ):
            value_text = _none_or_str(item.get(source_key))
            if value_text and target_key not in summary:
                summary[target_key] = value_text
        if summary.get("skill_id") and summary.get("skill_scope_id"):
            summaries.append(summary)
    return sorted(
        summaries,
        key=lambda item: (str(item.get("skill_scope_id")), str(item.get("skill_id"))),
    )


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
