"""In-memory background execution orchestrator.

This is the runtime-side counterpart to ``BackgroundRuntimeManager``:
it schedules background control and agent input work, maintains a compact
runtime snapshot, and emits normalized runtime events back to an attached
sink. It does not talk to the TUI directly.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import time
import uuid
from typing import Any, Callable

from openspace.core.agent_runtime_events import (
    AgentRuntimeEvent,
    RUNTIME_EVENT_TYPES,
    coerce_runtime_event,
)
from openspace.runtime import ExecutionRequest
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

RuntimeEventSink = Callable[[str, dict[str, Any]], Any]

BACKGROUND_SINK_EVENT_TYPES = RUNTIME_EVENT_TYPES | {
    "agent_list",
    "agent_event",
    "agent_transcript",
    "status_update",
}


class BackgroundOrchestrator:
    """Execution-layer background adapter backed by a real OpenSpace runtime."""

    def __init__(
        self,
        openspace: Any,
        *,
        session_title: str = "Background session",
        primary_agent_id: str = "primary",
        primary_agent_name: str = "Primary agent",
        event_sink: RuntimeEventSink | None = None,
    ) -> None:
        self._session_title = session_title.strip() or "Background session"
        self._primary_agent_id = primary_agent_id.strip() or "primary"
        self._primary_agent_name = (
            primary_agent_name.strip() or "Primary agent"
        )
        self._event_sink = event_sink
        self._openspace = None
        self._current_task: asyncio.Task | None = None
        self._has_bound_session_id = False

        self._session_id = uuid.uuid4().hex
        self._run_index = 0
        self._status = "idle"
        self._created_at = _utc_now()
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._metadata: dict[str, Any] = {}
        self._active_agent_id = self._primary_agent_id
        self._last_input: dict[str, Any] | None = None
        self._last_control: dict[str, Any] | None = None
        self._last_event_kind: str | None = None

        self._agents: dict[str, dict[str, Any]] = {
            self._primary_agent_id: self._make_agent(
                self._primary_agent_id,
                self._primary_agent_name,
            )
        }
        self._events: list[dict[str, Any]] = []
        self._transcript: list[dict[str, Any]] = []
        self._bound_event_runtime: Any | None = None
        self._bound_event_sink: RuntimeEventSink | None = None

        self.bind_runtime(openspace)

    def set_event_sink(self, sink: RuntimeEventSink | None) -> None:
        self._event_sink = sink

    def bind_runtime(self, openspace: Any | None) -> Any | None:
        """Attach a real OpenSpace executor for background runs."""
        if openspace is None:
            raise ValueError("BackgroundOrchestrator requires a bound OpenSpace runtime")
        self._unbind_runtime_event_sink()
        self._openspace = openspace

        register = getattr(openspace, "register_event_sink", None)
        if callable(register):
            sink = self._handle_executor_runtime_event
            register(sink)
            self._bound_event_runtime = openspace
            self._bound_event_sink = sink
        return openspace

    def _unbind_runtime_event_sink(self) -> None:
        runtime = self._bound_event_runtime
        sink = self._bound_event_sink
        self._bound_event_runtime = None
        self._bound_event_sink = None
        if runtime is None or sink is None:
            return
        unregister = getattr(runtime, "unregister_event_sink", None)
        if callable(unregister):
            try:
                unregister(sink)
            except Exception:
                logger.debug("Failed to unbind background runtime event sink", exc_info=True)

    async def dispatch(
        self,
        event_type: str,
        data: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        if event_type == "agent_input":
            return await self.handle_agent_input(data)
        if event_type == "background_control":
            return await self.handle_background_control(data)
        raise ValueError(f"Unsupported background runtime event type: {event_type}")

    async def handle_agent_input(
        self,
        data: dict[str, Any] | str | None,
    ) -> dict[str, Any]:
        payload = self._normalize_input(data)
        return await self._handle_real_agent_input(payload)

    async def _handle_real_agent_input(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self._status == "paused":
            raise RuntimeError("Cannot send agent input while the background session is paused")
        if self._current_task is not None and not self._current_task.done():
            raise RuntimeError("Background runtime is busy")
        if self._status != "running":
            await self.start_session()

        agent_id = str(payload.get("agent_id") or self._primary_agent_id)
        agent_name = str(
            payload.get("agent_name")
            or self._agents.get(agent_id, {}).get("name")
            or agent_id
        )
        self._active_agent_id = agent_id
        self._last_input = copy.deepcopy(payload)
        self._upsert_agent_from_runtime(
            {
                "agent_id": agent_id,
                "name": agent_name,
                "status": "queued",
                "task_id": payload.get("task_id"),
                "summary": payload.get("summary") or payload.get("title"),
                "metadata": payload.get("metadata"),
            }
        )
        await self._emit_runtime_event(
            "background_session_update",
            {
                **self._session_snapshot(),
                "status": "running",
                "active_agent_id": agent_id,
                "metadata": {
                    **copy.deepcopy(self._metadata),
                    "task_id": payload.get("task_id"),
                    "phase": "queued",
                },
            },
        )

        task = asyncio.create_task(
            self._run_bound_agent_input(
                payload,
                agent_id=agent_id,
                agent_name=agent_name,
            )
        )
        self._current_task = task
        task.add_done_callback(self._clear_current_task)
        return self.snapshot()

    async def handle_background_control(
        self,
        data: dict[str, Any] | str | None,
    ) -> dict[str, Any]:
        payload = self._normalize_control(data)
        action = payload["action"]

        if action == "start":
            await self.start_session(
                title=payload.get("title") if isinstance(payload.get("title"), str) else None,
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
            )
            return self.snapshot()

        if action == "reset":
            self._reset_session_state()
            await self._emit_runtime_event(
                "background_session_update",
                self._session_snapshot(),
            )
            return self.snapshot()

        if action == "pause":
            if self._status != "running":
                raise RuntimeError("Cannot pause a background session that is not running")
            self._status = "paused"
            self._touch_agents("paused")

        elif action == "resume":
            if self._status != "paused":
                raise RuntimeError("Cannot resume a background session that is not paused")
            self._status = "running"
            self._touch_agents("running", active_agent_id=self._active_agent_id)

        elif action == "stop":
            if self._status not in {"running", "paused"}:
                raise RuntimeError("Cannot stop a background session that is not active")
            if self._current_task is not None and not self._current_task.done():
                self._current_task.cancel()
            self._status = "stopped"
            self._finished_at = _utc_now()
            self._touch_agents("stopped")

        elif action == "focus":
            agent_id = str(payload.get("agent_id") or payload.get("target_agent_id") or "")
            if not agent_id:
                agent_id = self._active_agent_id
            self._active_agent_id = agent_id
            self._upsert_agent_from_runtime(
                {
                    "agent_id": agent_id,
                    "name": payload.get("agent_name") or agent_id,
                    "status": "running" if self._status == "running" else self._status,
                    "metadata": payload.get("metadata"),
                }
            )
            self._touch_agents(self._status, active_agent_id=agent_id)

        elif action in {"snapshot", "status"}:
            await self._emit_runtime_event("background_session_update", self._session_snapshot())
            return self.snapshot()

        else:
            raise ValueError(f"Unsupported background control action: {action}")

        self._last_control = copy.deepcopy(payload)
        await self._emit_runtime_event("background_session_update", self._session_snapshot())
        return self.snapshot()

    async def _run_bound_agent_input(
        self,
        payload: dict[str, Any],
        *,
        agent_id: str,
        agent_name: str,
    ) -> None:
        openspace = self._openspace
        if openspace is None:
            raise RuntimeError("BackgroundOrchestrator has no bound OpenSpace runtime")

        raw_content = self._extract_content(payload)
        instruction = raw_content if isinstance(raw_content, str) else str(raw_content)
        task_id = str(payload.get("task_id") or f"bg_{uuid.uuid4().hex[:12]}")
        context: dict[str, Any] = {
            "agent_id": agent_id,
            "background_session": True,
        }
        if self._has_bound_session_id:
            context["session_id"] = self._session_id

        suppress_bridge_dispatch = getattr(openspace, "suppress_bridge_dispatch", None)
        try:
            if callable(suppress_bridge_dispatch):
                async with suppress_bridge_dispatch():
                    await openspace.execute(
                        ExecutionRequest(
                            prompt=instruction,
                            context=context,
                            task_id=task_id,
                        )
                    )
            else:
                await openspace.execute(
                    ExecutionRequest(
                        prompt=instruction,
                        context=context,
                        task_id=task_id,
                    )
                )
        except asyncio.CancelledError:
            await self._emit_runtime_event(
                "background_session_update",
                {
                    **self._session_snapshot(),
                    "status": "cancelled",
                    "active_agent_id": agent_id,
                    "metadata": {
                        **copy.deepcopy(self._metadata),
                        "task_id": task_id,
                    },
                },
            )
            raise
        except Exception as exc:
            await self._emit_runtime_event(
                "agent_error",
                {
                    "agent_id": agent_id,
                    "name": agent_name,
                    "task_id": task_id,
                    "status": "error",
                    "content": str(exc),
                    "summary": str(exc)[:160],
                    "role": "system",
                },
            )
            await self._emit_runtime_event(
                "background_session_update",
                {
                    **self._session_snapshot(),
                    "status": "error",
                    "active_agent_id": agent_id,
                    "metadata": {
                        **copy.deepcopy(self._metadata),
                        "task_id": task_id,
                        "error": str(exc),
                    },
                },
            )

    async def _handle_executor_runtime_event(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if event_type not in BACKGROUND_SINK_EVENT_TYPES:
            return

        normalized_payload = copy.deepcopy(payload)
        session_id = normalized_payload.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            self._has_bound_session_id = True
        normalized_payload.setdefault("session_id", self._session_id)

        if event_type.startswith("agent_"):
            normalized_payload.setdefault(
                "agent_id",
                normalized_payload.get("agent_id") or self._active_agent_id,
            )
            normalized_payload.setdefault(
                "name",
                normalized_payload.get("name")
                or normalized_payload.get("agent_name")
                or self._agents.get(
                    str(normalized_payload.get("agent_id") or self._active_agent_id),
                    {},
                ).get("name")
                or self._primary_agent_name,
            )
        elif event_type == "background_session_update":
            normalized_payload.setdefault("title", self._session_title)
            normalized_payload.setdefault("active_agent_id", self._active_agent_id)

        await self._emit_runtime_event(event_type, normalized_payload)

    def _clear_current_task(self, task: asyncio.Task) -> None:
        if self._current_task is task:
            self._current_task = None

    async def start_session(
        self,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session_was_restarted = self._status == "stopped"
        if session_was_restarted:
            self._reset_session_state()

        if title is not None:
            self._session_title = title.strip() or self._session_title
        if metadata:
            self._metadata.update(copy.deepcopy(metadata))

        now = _utc_now()
        if self._run_index == 0:
            self._run_index = 1
            self._created_at = now
            self._started_at = now
        elif self._started_at is None:
            self._started_at = now

        self._status = "running"
        self._finished_at = None
        self._active_agent_id = self._primary_agent_id
        self._touch_agents("running", active_agent_id=self._primary_agent_id)

        await self._emit_runtime_event(
            "background_session_update",
            {
                **self._session_snapshot(),
                "status": "running",
                "started": True,
                "restarted": session_was_restarted,
            },
        )
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "session": self._session_snapshot(),
            "agents": self._agent_list_snapshot(),
            "events": copy.deepcopy(self._events),
            "transcript": copy.deepcopy(self._transcript),
        }

    async def _emit_runtime_event(
        self,
        event_type: str | AgentRuntimeEvent | dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> None:
        sink = self._event_sink
        if sink is None:
            return

        runtime_event = coerce_runtime_event(event_type, payload)
        self._append_runtime_record(runtime_event)

        try:
            result = sink(runtime_event.event_type, copy.deepcopy(runtime_event.payload))
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("Background runtime sink failed for %s", runtime_event.event_type)

    def _append_runtime_record(self, runtime_event: AgentRuntimeEvent) -> None:
        payload_session_id = runtime_event.payload.get("session_id")
        if isinstance(payload_session_id, str) and payload_session_id.strip():
            self._session_id = payload_session_id
            self._has_bound_session_id = True

        record = runtime_event.to_dict()
        record["event_id"] = uuid.uuid4().hex
        record["session_id"] = (
            payload_session_id if isinstance(payload_session_id, str) and payload_session_id.strip() else self._session_id
        )
        record["run_index"] = self._run_index
        record["timestamp"] = _utc_now()
        self._events.append(record)
        self._last_event_kind = runtime_event.event_type

        agent_id = runtime_event.agent_id or str(
            runtime_event.payload.get("agent_id") or self._active_agent_id
        )
        record["agent_id"] = agent_id
        agent_name = str(
            runtime_event.payload.get("name")
            or runtime_event.payload.get("agent_name")
            or self._agents.get(agent_id, {}).get("name")
            or agent_id
        )
        self._upsert_agent_from_runtime(
            {
                "agent_id": agent_id,
                "name": agent_name,
                "status": runtime_event.payload.get("status") or self._status,
                "model": runtime_event.payload.get("model"),
                "task_id": runtime_event.payload.get("task_id"),
                "summary": runtime_event.payload.get("summary"),
                "metadata": runtime_event.payload.get("metadata"),
            }
        )

        agent = self._agents[agent_id]
        agent["event_count"] += 1
        agent["last_event_kind"] = runtime_event.event_type
        if runtime_event.event_type == "agent_start":
            agent["status"] = "running"
            self._status = "running"
            self._active_agent_id = agent_id
        elif runtime_event.event_type == "agent_progress":
            agent["status"] = "running"
            self._active_agent_id = agent_id
        elif runtime_event.event_type == "agent_output":
            agent["status"] = "running"
            agent["last_output_at"] = _utc_now()
            self._append_transcript_from_runtime(runtime_event)
        elif runtime_event.event_type == "agent_error":
            agent["status"] = "error"
            agent["last_error_at"] = _utc_now()
            self._append_transcript_from_runtime(runtime_event)
        elif runtime_event.event_type == "agent_complete":
            agent["status"] = "completed"
            agent["last_completed_at"] = _utc_now()
        elif runtime_event.event_type == "background_session_update":
            self._sync_session_from_runtime(runtime_event.payload)

    def _reset_session_state(
        self,
        *,
        session_id: str | None = None,
        run_index: int | None = None,
    ) -> None:
        self._session_id = session_id or uuid.uuid4().hex
        self._run_index = run_index if run_index is not None else self._run_index + 1
        self._has_bound_session_id = False
        self._status = "idle"
        self._created_at = _utc_now()
        self._started_at = None
        self._finished_at = None
        self._metadata = {}
        self._active_agent_id = self._primary_agent_id
        self._last_input = None
        self._last_control = None
        self._last_event_kind = None
        self._events = []
        self._transcript = []
        self._agents = {
            self._primary_agent_id: self._make_agent(
                self._primary_agent_id,
                self._primary_agent_name,
            )
        }

    def _sync_session_from_runtime(self, payload: dict[str, Any]) -> None:
        session_id = payload.get("session_id")
        if session_id and session_id != self._session_id:
            self._reset_session_state(
                session_id=str(session_id),
                run_index=_optional_int(payload.get("run_index")),
            )

        run_index = _optional_int(payload.get("run_index"))
        if run_index is not None:
            self._run_index = run_index

        title = payload.get("title")
        if isinstance(title, str) and title.strip():
            self._session_title = title.strip()

        status = payload.get("status")
        if isinstance(status, str) and status.strip():
            self._status = status.strip().lower()

        created_at = payload.get("created_at")
        if isinstance(created_at, str) and created_at:
            self._created_at = created_at
        started_at = payload.get("started_at")
        if isinstance(started_at, str) and started_at:
            self._started_at = started_at
        finished_at = payload.get("finished_at")
        if isinstance(finished_at, str) and finished_at:
            self._finished_at = finished_at

        active_agent_id = payload.get("active_agent_id")
        if isinstance(active_agent_id, str) and active_agent_id.strip():
            self._active_agent_id = active_agent_id.strip()

        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            self._metadata.update(copy.deepcopy(metadata))

        agents = payload.get("agents")
        if isinstance(agents, list):
            for raw_agent in agents:
                if isinstance(raw_agent, dict):
                    self._upsert_agent_from_runtime(raw_agent)

        self._last_control = copy.deepcopy(payload)

    def _append_transcript_from_runtime(self, runtime_event: AgentRuntimeEvent) -> dict[str, Any]:
        payload = runtime_event.payload
        agent_id = runtime_event.agent_id or str(
            payload.get("agent_id") or self._active_agent_id
        )
        agent = self._agents.get(agent_id) or self._upsert_agent_from_runtime(
            {
                "agent_id": agent_id,
                "name": payload.get("name") or payload.get("agent_name") or agent_id,
            }
        )
        content = self._extract_content(payload)
        entry = {
            "entry_id": uuid.uuid4().hex,
            "kind": runtime_event.event_type,
            "role": str(payload.get("role") or ("assistant" if runtime_event.event_type != "agent_error" else "system")),
            "agent_id": agent_id,
            "agent_name": agent["name"],
            "content": copy.deepcopy(content),
            "payload": copy.deepcopy(payload),
            "timestamp": _utc_now(),
        }
        self._transcript.append(entry)
        return entry

    def _upsert_agent_from_runtime(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id") or payload.get("id") or self._primary_agent_id)
        agent_name = str(
            payload.get("name")
            or payload.get("agent_name")
            or self._agents.get(agent_id, {}).get("name")
            or agent_id
        )
        agent = self._agents.get(agent_id)
        if agent is None:
            agent = self._make_agent(agent_id, agent_name, is_primary=agent_id == self._primary_agent_id)
            self._agents[agent_id] = agent
        else:
            agent["name"] = agent_name

        for key in ("status", "model", "task_id", "summary"):
            value = payload.get(key)
            if value is not None:
                agent[key] = value

        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            agent["metadata"].update(copy.deepcopy(metadata))

        return agent

    def _make_agent(
        self,
        agent_id: str,
        agent_name: str,
        *,
        is_primary: bool = True,
    ) -> dict[str, Any]:
        return {
            "agent_id": agent_id,
            "name": agent_name,
            "kind": "runtime" if is_primary else "auxiliary",
            "status": "idle",
            "is_primary": is_primary,
            "input_count": 0,
            "event_count": 0,
            "last_event_kind": None,
            "last_input_at": None,
            "last_output_at": None,
            "last_error_at": None,
            "last_completed_at": None,
            "model": None,
            "task_id": None,
            "summary": None,
            "metadata": {},
        }

    def _touch_agents(self, status: str, *, active_agent_id: str | None = None) -> None:
        self._active_agent_id = active_agent_id or self._active_agent_id
        for agent_id, agent in self._agents.items():
            if agent_id == self._active_agent_id:
                agent["status"] = "active" if status == "running" else status
            elif agent.get("is_primary"):
                agent["status"] = "active" if status == "running" else status

    def _agent_list_snapshot(self) -> dict[str, Any]:
        agents = [copy.deepcopy(agent) for agent in self._agents.values()]
        agents.sort(key=lambda agent: (not agent.get("is_primary", False), agent["agent_id"]))
        return {
            "session_id": self._session_id,
            "run_index": self._run_index,
            "status": self._status,
            "primary_agent_id": self._primary_agent_id,
            "active_agent_id": self._active_agent_id,
            "agents": agents,
        }

    def _session_snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self._session_id,
            "run_index": self._run_index,
            "title": self._session_title,
            "status": self._status,
            "created_at": self._created_at,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "updated_at": _utc_now(),
            "primary_agent_id": self._primary_agent_id,
            "active_agent_id": self._active_agent_id,
            "agent_count": len(self._agents),
            "event_count": len(self._events),
            "transcript_count": len(self._transcript),
            "last_input": copy.deepcopy(self._last_input),
            "last_control": copy.deepcopy(self._last_control),
            "last_event_kind": self._last_event_kind,
            "metadata": copy.deepcopy(self._metadata),
        }

    @staticmethod
    def _normalize_input(data: dict[str, Any] | str | None) -> dict[str, Any]:
        if data is None:
            return {}
        if isinstance(data, str):
            return {"content": data}
        if isinstance(data, dict):
            return copy.deepcopy(data)
        raise TypeError(f"Unsupported agent input payload: {type(data)!r}")

    @staticmethod
    def _normalize_control(data: dict[str, Any] | str | None) -> dict[str, Any]:
        if data is None:
            raise ValueError("background_control requires an action")
        if isinstance(data, str):
            return {"action": data.strip().lower()}
        if not isinstance(data, dict):
            raise TypeError(f"Unsupported background control payload: {type(data)!r}")

        payload = copy.deepcopy(data)
        action = str(payload.get("action") or payload.get("command") or "").strip().lower()
        if not action:
            raise ValueError("background_control requires an action")
        payload["action"] = action
        return payload

    @staticmethod
    def _extract_content(payload: dict[str, Any]) -> Any:
        for key in ("content", "text", "message", "output", "response", "instruction"):
            if key in payload:
                return payload[key]
        return payload


def _optional_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
