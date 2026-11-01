"""TUI-facing background runtime adapter.

The manager binds a real background runtime/orchestrator and converts its
events into UI-friendly snapshots.
"""

from __future__ import annotations

import copy
import inspect
import time
import uuid
from typing import Any, Callable

from openspace.core.agent_runtime_events import (
    AgentRuntimeEvent,
    RUNTIME_AGENT_EVENT_TYPES,
    RUNTIME_EVENT_TYPES,
    coerce_runtime_event,
)
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

SnapshotCallback = Callable[[str, dict[str, Any]], Any]

SUPPORTED_BACKGROUND_RUNTIME_EVENTS = RUNTIME_EVENT_TYPES | {
    "agent_list",
    "agent_event",
    "agent_transcript",
    "status_update",
}

TASK_UPDATE_EVENT_STATUSES: dict[str, str] = {
    "agent_task_complete": "completed",
    "task_started": "running",
    "task_completed": "completed",
    "task_failed": "failed",
    "task_stopped": "killed",
}


class BackgroundRuntimeManager:
    """Track one background session and emit UI-friendly snapshots."""

    def __init__(
        self,
        emit_callback: SnapshotCallback | None = None,
        *,
        session_title: str = "Background session",
        primary_agent_id: str = "primary",
        primary_agent_name: str = "Primary agent",
    ) -> None:
        self._emit_callback = emit_callback
        self._runtime: Any | None = None
        self._session_title = session_title.strip() or "Background session"
        self._primary_agent_id = primary_agent_id.strip() or "primary"
        self._primary_agent_name = (
            primary_agent_name.strip() or "Primary agent"
        )

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

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_running(self) -> bool:
        return self._status == "running"

    @property
    def is_paused(self) -> bool:
        return self._status == "paused"

    @property
    def is_idle(self) -> bool:
        return self._status == "idle"

    @property
    def is_stopped(self) -> bool:
        return self._status == "stopped"

    def set_event_callback(self, callback: SnapshotCallback | None) -> None:
        self._emit_callback = callback

    def bind_runtime(self, runtime: Any) -> Any:
        """Attach a runtime/orchestrator that will receive delegated commands."""

        if runtime is None:
            raise RuntimeError("BackgroundRuntimeManager requires a bound runtime")
        self._runtime = runtime

        for attr in ("register_event_sink", "set_event_sink", "bind_event_sink"):
            sink = getattr(runtime, attr, None)
            if callable(sink):
                try:
                    sink(self.ingest_runtime_event)
                    break
                except Exception:
                    logger.exception("Failed to bind runtime event sink via %s", attr)
        return runtime

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

        result = await self._invoke_runtime("handle_agent_input", payload)
        self._apply_runtime_result(result)
        return self.snapshot()

    async def handle_background_control(
        self,
        data: dict[str, Any] | str | None,
    ) -> dict[str, Any]:
        payload = self._normalize_control(data)
        result = await self._invoke_runtime("handle_background_control", payload)
        self._apply_runtime_result(result)
        return self.snapshot()

    async def ingest_runtime_event(
        self,
        event_type: str | AgentRuntimeEvent | dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ingest a normalized runtime event emitted by a bound executor."""

        runtime_event = coerce_runtime_event(event_type, payload)
        etype = runtime_event.event_type
        event_payload = runtime_event.payload

        if etype not in SUPPORTED_BACKGROUND_RUNTIME_EVENTS:
            logger.debug("Ignoring non-background runtime event: %s", etype)
            return self.snapshot()

        if etype == "agent_list":
            self._sync_agent_list_from_snapshot(event_payload)
            await self._emit_state(
                None,
                include_agent_list=True,
                emit_agent_event=False,
                emit_transcript=False,
            )
            return self.snapshot()

        if etype == "agent_event":
            self._sync_events_from_snapshot(event_payload)
            await self._emit_state(
                None,
                include_agent_list=True,
                emit_agent_event=False,
                emit_transcript=False,
            )
            return self.snapshot()

        if etype == "agent_transcript":
            self._sync_transcript_from_snapshot(event_payload)
            await self._emit_state(
                None,
                include_agent_list=False,
                emit_agent_event=False,
                emit_transcript=False,
            )
            return self.snapshot()

        if etype == "status_update":
            event_payload.setdefault("session_id", self._session_id)
            await self._emit("status_update", event_payload)
            return self.snapshot()

        if etype == "background_session_update":
            self._sync_session_from_runtime(event_payload)
            event = self._append_event(
                etype,
                agent_id=str(
                    event_payload.get("active_agent_id")
                    or event_payload.get("agent_id")
                    or self._active_agent_id
                ),
                payload=copy.deepcopy(event_payload),
                source="runtime",
            )
            await self._emit_state(event, include_agent_list=True)
            return self.snapshot()

        if etype in TASK_UPDATE_EVENT_STATUSES:
            event_payload.setdefault("status", TASK_UPDATE_EVENT_STATUSES[etype])
            etype = "agent_task_update"

        if etype == "team_update":
            team_name = event_payload.get("team_name")
            if team_name:
                self._metadata["team_name"] = str(team_name)
            team_status = event_payload.get("status")
            if team_status:
                self._metadata["team_status"] = str(team_status)
            event = self._append_event(
                etype,
                agent_id=str(event_payload.get("agent_id") or self._primary_agent_id),
                payload=copy.deepcopy(event_payload),
                source="runtime",
            )
            await self._emit_state(event, include_agent_list=True)
            return self.snapshot()

        if etype == "todo_update":
            event = self._append_event(
                etype,
                agent_id=str(event_payload.get("agent_id") or self._primary_agent_id),
                payload=copy.deepcopy(event_payload),
                source="runtime",
            )
            await self._emit_state(event, include_agent_list=False)
            return self.snapshot()

        if etype in RUNTIME_AGENT_EVENT_TYPES:
            agent = self._upsert_agent_from_runtime(event_payload)
            if etype in {"agent_start", "agent_spawn"}:
                self._status = "running"
                self._active_agent_id = agent["agent_id"]
                self._last_control = {
                    "action": (
                        "runtime_agent_start"
                        if etype == "agent_start"
                        else "runtime_agent_spawn"
                    ),
                    "payload": copy.deepcopy(event_payload),
                }
            elif etype in {"agent_progress", "agent_task_update"}:
                self._status = "running"
                self._active_agent_id = agent["agent_id"]
            elif etype == "agent_output":
                agent["last_output_at"] = _utc_now()
            elif etype == "agent_error":
                agent["last_error_at"] = _utc_now()
            elif etype == "agent_complete":
                agent["last_completed_at"] = _utc_now()

            if etype in {"agent_output", "agent_error"}:
                self._append_transcript_from_runtime(runtime_event)

            event = self._append_event(
                etype,
                agent_id=agent["agent_id"],
                payload=copy.deepcopy(event_payload),
                source="runtime",
            )
            await self._emit_state(event, include_agent_list=True)
            return self.snapshot()

        logger.debug("Ignoring unsupported background runtime event: %s", etype)
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "session": self._session_snapshot(),
            "agents": self._agent_list_snapshot(),
            "events": copy.deepcopy(self._events),
            "transcript": copy.deepcopy(self._transcript),
        }

    async def _invoke_runtime(self, method_name: str, payload: dict[str, Any]) -> Any:
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("BackgroundRuntimeManager has no bound runtime")

        method = getattr(runtime, method_name, None)
        if method is None and hasattr(runtime, "dispatch"):
            if method_name == "handle_agent_input":
                method = lambda data: runtime.dispatch("agent_input", data)
            elif method_name == "handle_background_control":
                method = lambda data: runtime.dispatch("background_control", data)

        if method is None:
            raise RuntimeError(f"Bound runtime does not implement {method_name}")

        result = method(payload)
        if inspect.isawaitable(result):
            result = await result
        return result

    def _apply_runtime_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            return

        if "session" in result and isinstance(result["session"], dict):
            self._sync_session_from_runtime(result["session"])
        if "agents" in result and isinstance(result["agents"], dict):
            self._sync_agent_list_from_snapshot(result["agents"])
        if "events" in result and isinstance(result["events"], list):
            self._events = copy.deepcopy(result["events"])
        if "transcript" in result and isinstance(result["transcript"], list):
            self._transcript = copy.deepcopy(result["transcript"])

    def _reset_session_state(
        self,
        *,
        session_id: str | None = None,
        run_index: int | None = None,
    ) -> None:
        self._session_id = session_id or uuid.uuid4().hex
        self._run_index = run_index if run_index is not None else self._run_index + 1
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
            self._sync_agent_list_from_iterable(agents)

        self._last_control = copy.deepcopy(payload)

    def _sync_agent_list_from_snapshot(self, payload: dict[str, Any]) -> None:
        session_id = payload.get("session_id")
        if session_id and session_id != self._session_id:
            self._reset_session_state(
                session_id=str(session_id),
                run_index=_optional_int(payload.get("run_index")),
            )
        run_index = _optional_int(payload.get("run_index"))
        if run_index is not None:
            self._run_index = run_index
        status = payload.get("status")
        if isinstance(status, str) and status.strip():
            self._status = status.strip().lower()
        active_agent_id = payload.get("active_agent_id")
        if isinstance(active_agent_id, str) and active_agent_id.strip():
            self._active_agent_id = active_agent_id.strip()
        agents = payload.get("agents")
        if isinstance(agents, list):
            self._sync_agent_list_from_iterable(agents)

    def _sync_agent_list_from_iterable(self, agents: list[dict[str, Any]]) -> None:
        normalized: dict[str, dict[str, Any]] = {}
        for raw_agent in agents:
            if not isinstance(raw_agent, dict):
                continue
            agent = self._upsert_agent_from_runtime(raw_agent)
            normalized[agent["agent_id"]] = agent
        if self._primary_agent_id not in normalized:
            normalized[self._primary_agent_id] = self._make_agent(
                self._primary_agent_id,
                self._primary_agent_name,
            )
        self._agents = normalized

    def _sync_events_from_snapshot(self, payload: dict[str, Any]) -> None:
        events = payload.get("events")
        if isinstance(events, list):
            self._events = copy.deepcopy(events)
        latest_event = payload.get("latest_event")
        if isinstance(latest_event, dict):
            self._last_event_kind = str(
                latest_event.get("kind")
                or latest_event.get("event_type")
                or self._last_event_kind
            )

    def _sync_transcript_from_snapshot(self, payload: dict[str, Any]) -> None:
        transcript = payload.get("transcript")
        if isinstance(transcript, list):
            self._transcript = copy.deepcopy(transcript)

    def _append_transcript_from_runtime(
        self,
        runtime_event: AgentRuntimeEvent,
    ) -> dict[str, Any]:
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
            "role": str(
                payload.get("role")
                or ("system" if runtime_event.event_type == "agent_error" else "assistant")
            ),
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
            agent = self._make_agent(
                agent_id,
                agent_name,
                is_primary=agent_id == self._primary_agent_id,
            )
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

    def _ensure_agent(self, agent_id: str, agent_name: str) -> dict[str, Any]:
        agent = self._agents.get(agent_id)
        if agent is None:
            agent = self._make_agent(agent_id, agent_name, is_primary=False)
            self._agents[agent_id] = agent
        elif agent_name and agent["name"] != agent_name:
            agent["name"] = agent_name
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
            "kind": "primary" if is_primary else "auxiliary",
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

    def _append_event(
        self,
        kind: str,
        *,
        agent_id: str,
        payload: dict[str, Any],
        source: str = "runtime",
    ) -> dict[str, Any]:
        event = {
            "event_id": uuid.uuid4().hex,
            "kind": kind,
            "agent_id": agent_id,
            "session_id": self._session_id,
            "run_index": self._run_index,
            "timestamp": _utc_now(),
            "source": source,
            "payload": copy.deepcopy(payload),
        }
        self._events.append(event)
        agent = self._ensure_agent(agent_id, self._agents.get(agent_id, {}).get("name", agent_id))
        agent["event_count"] += 1
        agent["last_event_kind"] = kind
        self._last_event_kind = kind
        return event

    async def _emit_state(
        self,
        event: dict[str, Any] | None,
        *,
        include_agent_list: bool = False,
        emit_agent_event: bool = True,
        emit_transcript: bool = True,
    ) -> None:
        snapshots: list[tuple[str, dict[str, Any]]] = []
        if include_agent_list:
            snapshots.append(("agent_list", self._agent_list_snapshot()))
        if emit_agent_event and event is not None:
            snapshots.append(("agent_event", self._agent_event_snapshot(event)))
        if emit_transcript:
            snapshots.append(("agent_transcript", self._transcript_snapshot()))
        snapshots.append(("background_session_update", self._session_snapshot()))

        for event_type, payload in snapshots:
            await self._emit(event_type, payload)

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        callback = self._emit_callback
        if callback is None:
            return
        try:
            result = callback(event_type, copy.deepcopy(data))
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("Background runtime callback failed for %s", event_type)

    def _apply_runtime_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            return

        if "session" in result and isinstance(result["session"], dict):
            self._sync_session_from_runtime(result["session"])
        if "agents" in result and isinstance(result["agents"], dict):
            self._sync_agent_list_from_snapshot(result["agents"])
        if "events" in result and isinstance(result["events"], list):
            self._events = copy.deepcopy(result["events"])
        if "transcript" in result and isinstance(result["transcript"], list):
            self._transcript = copy.deepcopy(result["transcript"])

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

    def _agent_event_snapshot(self, event: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_id": self._session_id,
            "run_index": self._run_index,
            "event_count": len(self._events),
            "latest_event": copy.deepcopy(event),
            "events": copy.deepcopy(self._events),
        }

    def _transcript_snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self._session_id,
            "run_index": self._run_index,
            "transcript_count": len(self._transcript),
            "transcript": copy.deepcopy(self._transcript),
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
