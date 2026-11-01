"""Runtime event adapter for EvidenceStore."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from openspace.utils.logging import Logger

from .memory_adapter import MemoryEvidenceAdapter, background_drain_event
from .session_adapter import SessionEvidenceAdapter
from .skill_adapter import SkillEvidenceAdapter
from .store import EvidenceStore
from .tool_adapter import ToolEvidenceAdapter
from .types import EvidenceEvent, EvidenceScope, ResourceRef

logger = Logger.get_logger(__name__)

_ANALYSIS_TRIGGER_SUPPRESSED_STATUSES = {
    "api_error",
    "llm_error",
    "model_error",
}
_ANALYSIS_TRIGGER_SUPPRESSED_TERMS = (
    "api error",
    "rate limit",
    "rate_limit",
    "429",
    "openrouterexception",
    "litellm.ratelimiterror",
)

_AGENT_EVIDENCE_EVENTS = {
    "agent_spawn",
    "agent_start",
    "agent_progress",
    "agent_task_update",
    "agent_complete",
    "agent_error",
    "agent_task_complete",
}


class RuntimeEvidenceAdapter:
    """Translate runtime bus facts into EvidenceEvent rows.

    This adapter is intentionally best-effort. Evidence writes must not make a
    user task fail; the store remains the durable source when ingestion
    succeeds.
    """

    def __init__(
        self,
        store: EvidenceStore,
        *,
        trigger_engine: Any | None = None,
    ) -> None:
        self._store = store
        self._trigger_engine = trigger_engine
        self._session_adapter = SessionEvidenceAdapter(store)
        self._tool_adapter = ToolEvidenceAdapter(store)
        self._skill_adapter = SkillEvidenceAdapter(store)
        self._memory_adapter = MemoryEvidenceAdapter(store)

    async def on_runtime_event(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            event = self._build_event(event_type, data)
            if event is None:
                return
            watermark = self._store.ingest_event(event)
            self._evaluate_trigger_checkpoint(event, data, watermark)
        except Exception:
            logger.debug(
                "Evidence ingest failed for runtime event %s",
                event_type,
                exc_info=True,
            )

    async def on_session_entry(self, entry_type: str, data: dict[str, Any]) -> None:
        try:
            await self._session_adapter.on_session_entry(entry_type, data)
        except Exception:
            logger.debug(
                "Evidence ingest failed for session entry %s",
                entry_type,
                exc_info=True,
            )

    async def ingest_session_storage_delta(
        self,
        storage: Any,
        *,
        task_id: str | None = None,
        parent_task_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        try:
            await self._session_adapter.ingest_storage_delta(
                storage,
                task_id=task_id,
                parent_task_id=parent_task_id,
                agent_id=agent_id,
            )
        except Exception:
            logger.debug("Evidence checkpoint scan failed", exc_info=True)

    async def ingest_skill_store_delta(
        self,
        skill_store: Any,
        *,
        task_id: str | None = None,
        turn_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 200,
    ) -> None:
        try:
            await self._skill_adapter.ingest_skill_store_delta(
                skill_store,
                task_id=task_id,
                turn_id=turn_id,
                agent_id=agent_id,
                limit=limit,
            )
        except Exception:
            logger.debug("Skill evidence checkpoint scan failed", exc_info=True)

    async def ingest_tool_quality_delta(
        self,
        quality_source: Any,
        *,
        limit: int = 20,
    ) -> None:
        try:
            await self._tool_adapter.ingest_quality_delta(
                quality_source,
                limit=limit,
            )
        except Exception:
            logger.debug("Tool quality evidence checkpoint scan failed", exc_info=True)

    async def on_skill_store_event(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            await self._skill_adapter.on_skill_store_event(event_type, data)
        except Exception:
            logger.debug(
                "Evidence ingest failed for skill event %s",
                event_type,
                exc_info=True,
            )

    def _build_event(
        self,
        event_type: str,
        data: Mapping[str, Any],
    ) -> EvidenceEvent | None:
        if event_type == "task_started":
            return self._task_started(data)
        if event_type == "task_finished_pre_persist":
            return self._task_finished_pre_persist(data)
        if event_type == "task_session_persisted":
            return self._task_session_persisted(data)
        if event_type == "background_drain":
            return background_drain_event(data)

        if event_type == "agent_event":
            event_name = str(data.get("event") or "")
            payload = data.get("payload")
            payload_map = payload if isinstance(payload, Mapping) else data
            if event_name in _AGENT_EVIDENCE_EVENTS:
                return self._agent_event(event_name, payload_map, envelope=data)
            return None

        if event_type in _AGENT_EVIDENCE_EVENTS:
            return self._agent_event(event_type, data, envelope=None)

        tool_event = self._tool_adapter.build_event(event_type, data)
        if tool_event is not None:
            return tool_event

        memory_event = self._memory_adapter.build_event(event_type, data)
        if memory_event is not None:
            return memory_event

        return None

    def _evaluate_trigger_checkpoint(
        self,
        event: EvidenceEvent,
        data: Mapping[str, Any],
        watermark: int,
    ) -> None:
        trigger_engine = self._trigger_engine
        if trigger_engine is None or event.event_type != "task_session_persisted":
            return
        if _should_suppress_analysis_checkpoint(data, event):
            return
        try:
            trigger_engine.evaluate_checkpoint(
                "task_session_persisted",
                EvidenceScope(
                    session_id=event.session_id,
                    task_id=event.task_id,
                    source_task_ids=tuple(_str_list(data.get("source_task_ids"))),
                    time_window=_pair_or_none(data.get("time_window")),
                    agent_ids=(),
                ),
                manifest_watermark=watermark,
            )
        except Exception:
            logger.debug(
                "Trigger checkpoint evaluation failed for %s",
                event.event_type,
                exc_info=True,
            )

    def _task_started(self, data: Mapping[str, Any]) -> EvidenceEvent:
        session_id = _none_or_str(data.get("session_id"))
        task_id = _none_or_str(data.get("task_id")) or "unknown"
        parent_task_id = _none_or_str(data.get("parent_task_id"))
        agent_id = _none_or_str(data.get("agent_id"))
        created_at = _utc_now()
        metadata = {
            "task_id": task_id,
            "session_id": session_id,
            "workspace_dir": data.get("workspace_dir"),
            "max_iterations": data.get("max_iterations"),
            "permission_mode": data.get("permission_mode"),
            "session_start_source": data.get("session_start_source"),
            "model": data.get("model"),
            "instruction_preview": str(data.get("instruction") or "")[:500],
        }
        ref = ResourceRef(
            ref_id=f"runtime_snapshot:{task_id}:start",
            ref_type="runtime_snapshot",
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            producer="runtime",
            created_at=created_at,
            reliability="runtime",
            role="primary",
            preview=f"task started {task_id}",
            metadata=metadata,
        )
        return EvidenceEvent.create(
            event_id=f"evt_task_started_{_digest(metadata)}",
            event_type="task_started",
            producer="runtime",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            idempotency_key=f"runtime:task_started:{session_id or ''}:{task_id}",
            primary_refs=[ref],
            metadata={"phase": "start", "model": data.get("model")},
        )

    def _task_finished_pre_persist(self, data: Mapping[str, Any]) -> EvidenceEvent:
        session_id = _none_or_str(data.get("session_id"))
        task_id = _none_or_str(data.get("task_id")) or "unknown"
        parent_task_id = _none_or_str(data.get("parent_task_id"))
        created_at = _utc_now()
        metadata = _runtime_finish_metadata(data)
        ref = ResourceRef(
            ref_id=f"runtime_snapshot:{task_id}:finish",
            ref_type="runtime_snapshot",
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            producer="runtime",
            created_at=created_at,
            reliability="runtime",
            role="primary",
            preview=f"task finished {task_id} status={metadata.get('status')}",
            metadata=metadata,
        )
        return EvidenceEvent.create(
            event_id=f"evt_task_finish_{_digest({'session_id': session_id, 'task_id': task_id})}",
            event_type="task_finished_pre_persist",
            producer="runtime",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            idempotency_key=f"runtime:task_finished_pre_persist:{session_id or ''}:{task_id}",
            primary_refs=[ref],
            metadata={"phase": "finish_pre_persist", "status": metadata.get("status")},
        )

    def _task_session_persisted(self, data: Mapping[str, Any]) -> EvidenceEvent:
        session_id = _none_or_str(data.get("session_id"))
        task_id = _none_or_str(data.get("task_id")) or "unknown"
        parent_task_id = _none_or_str(data.get("parent_task_id"))
        created_at = _utc_now()
        metadata = _runtime_finish_metadata(data)
        metadata["session_persisted"] = True
        primary = ResourceRef(
            ref_id=f"runtime_snapshot:{task_id}:session_persisted",
            ref_type="runtime_snapshot",
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            producer="runtime",
            created_at=created_at,
            reliability="runtime",
            role="primary",
            preview=f"task session persisted {task_id}",
            metadata=metadata,
        )
        supporting: list[ResourceRef] = []
        transcript_path = _none_or_str(data.get("transcript_path"))
        if transcript_path:
            supporting.append(
                ResourceRef(
                    ref_id=f"transcript_segment:{session_id or 'none'}:{task_id}:transcript",
                    ref_type="transcript_segment",
                    uri=transcript_path,
                    session_id=session_id,
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                    producer="runtime",
                    created_at=created_at,
                    reliability="persisted",
                    role="supporting",
                    metadata={
                        "session_dir": data.get("session_dir"),
                        "transcript_path": transcript_path,
                        "transcript_generation": data.get("transcript_generation"),
                    },
                )
            )
        file_history_dir = _none_or_str(data.get("file_history_dir"))
        if file_history_dir:
            supporting.append(
                ResourceRef(
                    ref_id=f"file_history:{session_id or 'none'}:{task_id}:dir",
                    ref_type="file_history",
                    uri=file_history_dir,
                    session_id=session_id,
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                    producer="runtime",
                    created_at=created_at,
                    reliability="persisted",
                    role="supporting",
                    metadata={"file_history_dir": file_history_dir},
                )
            )
        recording_dir = _none_or_str(data.get("recording_dir"))
        if recording_dir:
            supporting.append(
                ResourceRef(
                    ref_id=f"recording:{task_id}",
                    ref_type="recording_ref",
                    uri=recording_dir,
                    session_id=session_id,
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                    producer="runtime",
                    created_at=created_at,
                    reliability="fallback",
                    role="supporting",
                    preview=f"recording fallback for {task_id}",
                    metadata={"recording_dir": recording_dir},
                )
            )
        return EvidenceEvent.create(
            event_id=f"evt_task_persisted_{_digest({'session_id': session_id, 'task_id': task_id})}",
            event_type="task_session_persisted",
            producer="runtime",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            idempotency_key=f"runtime:task_session_persisted:{session_id or ''}:{task_id}",
            primary_refs=[primary],
            supporting_refs=supporting,
            metadata={"phase": "session_persisted", "status": metadata.get("status")},
        )

    def _agent_event(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        *,
        envelope: Mapping[str, Any] | None,
    ) -> EvidenceEvent:
        session_id = _first_str(payload, envelope, "session_id")
        task_id = _first_str(payload, envelope, "task_id")
        agent_id = _first_str(payload, envelope, "agent_id") or "unknown"
        parent_task_id = _first_str(payload, envelope, "parent_task_id")
        agent_type = _first_str(payload, envelope, "agent_type") or "unknown"
        status = _first_str(payload, envelope, "status") or _status_from_event(event_name)
        created_at = _utc_now()
        ref_id = (
            "agent_event:"
            f"{session_id or 'none'}:{task_id or agent_id}:{parent_task_id or 'root'}:"
            f"{agent_id}:{event_name}:{status}"
        )
        ref = ResourceRef(
            ref_id=ref_id,
            ref_type="agent_event",
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            producer="runtime",
            created_at=created_at,
            reliability="runtime",
            role="supporting",
            preview=f"{event_name} {agent_id} {status}",
            metadata={
                "session_id": session_id,
                "task_id": task_id,
                "parent_task_id": parent_task_id,
                "agent_id": agent_id,
                "agent_type": agent_type,
                "event_name": event_name,
                "status": status,
                "source": "runtime_event_bus",
            },
        )
        digest = hashlib.sha256(
            json.dumps(ref.metadata, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        return EvidenceEvent.create(
            event_id=f"evt_agent_{digest}",
            event_type="agent_event",
            producer="runtime",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            idempotency_key=(
                f"runtime:agent_event:{session_id or ''}:{task_id or ''}:"
                f"{parent_task_id or ''}:{agent_id}:{event_name}:{status}"
            ),
            supporting_refs=[ref],
            metadata={
                "event_name": event_name,
                "status": status,
                "agent_type": agent_type,
            },
        )


def _first_str(
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any] | None,
    key: str,
) -> str | None:
    for mapping in (primary, secondary):
        if not isinstance(mapping, Mapping):
            continue
        raw = mapping.get(key)
        if raw is None:
            continue
        text = str(raw)
        if text:
            return text
    return None


def _status_from_event(event_name: str) -> str:
    if event_name == "agent_spawn":
        return "running"
    if event_name in {"agent_task_complete", "agent_complete"}:
        return "completed"
    if event_name == "agent_error":
        return "failed"
    return "updated"


def _runtime_finish_metadata(data: Mapping[str, Any]) -> dict[str, Any]:
    final_response = (
        data.get("final_response_preview")
        or data.get("response_preview")
        or data.get("response")
        or ""
    )
    tool_executions = data.get("tool_executions")
    tool_summaries: list[dict[str, Any]] = []
    if isinstance(tool_executions, list):
        for item in tool_executions[:50]:
            if not isinstance(item, Mapping):
                continue
            tool_summaries.append(
                {
                    "tool": item.get("tool") or item.get("name") or item.get("tool_name"),
                    "status": item.get("status"),
                    "tool_use_id": item.get("tool_use_id") or item.get("id"),
                }
            )
    return {
        "status": data.get("status"),
        "stop_reason": data.get("stop_reason"),
        "execution_time": data.get("execution_time"),
        "iterations": data.get("iterations"),
        "tool_execution_count": data.get("tool_execution_count"),
        "tool_execution_summaries": tool_summaries,
        "active_skills": _str_list(data.get("active_skills")),
        "retrieved_tools_list": data.get("retrieved_tools_list") or [],
        "preselection_debug_info": data.get("preselection_debug_info"),
        "permission_mode": data.get("permission_mode"),
        "session_capability_state": data.get("session_capability_state"),
        "session_dir": data.get("session_dir"),
        "transcript_path": data.get("transcript_path"),
        "tool_results_dir": data.get("tool_results_dir"),
        "file_history_dir": data.get("file_history_dir"),
        "recording_dir": data.get("recording_dir"),
        "capture_skill_dir": data.get("capture_skill_dir"),
        "message_count": data.get("message_count"),
        "final_response_preview": str(final_response)[:500],
    }


def _should_suppress_analysis_checkpoint(
    data: Mapping[str, Any],
    event: EvidenceEvent,
) -> bool:
    metadata = event.metadata if isinstance(event.metadata, Mapping) else {}
    status = _lower_first_str(data, metadata, "status")
    stop_reason = _lower_first_str(data, metadata, "stop_reason")
    if (
        status in _ANALYSIS_TRIGGER_SUPPRESSED_STATUSES
        or stop_reason in _ANALYSIS_TRIGGER_SUPPRESSED_STATUSES
    ):
        return True
    text = " ".join(
        str(value or "")
        for value in (
            data.get("final_response_preview"),
            data.get("response"),
            data.get("error"),
            data.get("exception"),
            metadata.get("final_response_preview"),
            metadata.get("error"),
        )
    ).lower()
    return any(term in text for term in _ANALYSIS_TRIGGER_SUPPRESSED_TERMS)


def _lower_first_str(
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
    key: str,
) -> str:
    value = primary.get(key)
    if value is None:
        value = secondary.get(key)
    return str(value or "").strip().lower()


def _none_or_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if str(item)]


def _pair_or_none(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    left = _none_or_str(value[0])
    right = _none_or_str(value[1])
    if left is None or right is None:
        return None
    return (left, right)


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
