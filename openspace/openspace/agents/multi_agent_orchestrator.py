from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from openspace.agents.agent_definitions import (
    AgentDefinition,
    AgentDefinitionRegistry,
    AgentSource,
)
from openspace.agents.agent_tool import run_agent
from openspace.agents.agent_tool_utils import (
    AgentToolResult,
    resolve_agent_tools,
)
from openspace.agents.coordinator import CoordinatorMode
from openspace.agents.task_manager import AgentTask, TaskManager, TaskType
from openspace.grounding.core.tool.base import BaseTool
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


@dataclass(slots=True)
class SpawnTeammateResult:
    """Provider-neutral OS equivalent of OpenSpace ``SpawnOutput``.

    OpenSpace returns pane/tmux identifiers because teammates may be separate CLI
    processes. OpenSpace 12.4 only supports in-process teammates, so the pane
    fields are fixed to ``"in-process"`` while task metadata lives in
    ``TaskManager``.
    """

    teammate_id: str
    agent_id: str
    name: str
    task_id: str
    team_name: str | None
    agent_type: str | None = None
    model: str | None = None
    color: str | None = None
    tmux_session_name: str = "in-process"
    tmux_window_name: str = "in-process"
    tmux_pane_id: str = "in-process"
    is_splitpane: bool = False
    plan_mode_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "teammate_id": self.teammate_id,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "model": self.model,
            "name": self.name,
            "color": self.color,
            "tmux_session_name": self.tmux_session_name,
            "tmux_window_name": self.tmux_window_name,
            "tmux_pane_id": self.tmux_pane_id,
            "team_name": self.team_name,
            "task_id": self.task_id,
            "is_splitpane": self.is_splitpane,
            "plan_mode_required": self.plan_mode_required,
        }


class MultiAgentOrchestrator:
    """Session-scoped owner for OpenSpace multi-agent runtime state.

    OpenSpace's ``spawnMultiAgent.ts`` has three spawn paths: in-process teammate,
    split-pane teammate, and separate-window tmux teammate. OpenSpace's engine
    decision (DEC-016/017/018) is to keep the engine path in-process and route
    all lifetime state through a per-session ``TaskManager``.
    """

    def __init__(
        self,
        *,
        grounding_client: Any,
        llm_client: Any,
        event_sink: Callable[[str, dict[str, Any]], Any] | None = None,
        workspace_dir: str | Path | None = None,
        output_root: str | Path | None = None,
    ) -> None:
        self.grounding_client = grounding_client
        self.llm_client = llm_client
        self.event_sink = event_sink
        self.workspace_dir = Path(workspace_dir or ".").expanduser()
        self.output_root = Path(output_root).expanduser() if output_root else None
        self.agent_definitions = AgentDefinitionRegistry()
        self.coordinator = CoordinatorMode()
        self._task_managers: dict[str, TaskManager] = {}
        self._active_conversation_id: str | None = None

    def initialize(self) -> None:
        """Load project-local agent definitions once for this OpenSpace instance."""

        try:
            self.agent_definitions.load_custom_agents(
                self.workspace_dir / ".openspace" / "agents",
                source=AgentSource.PROJECT_SETTINGS,
            )
        except Exception:
            logger.debug("Failed to load project agent definitions", exc_info=True)

    def bind_agent(self, agent: Any) -> None:
        """Share this registry with ``GroundingAgent`` without global state."""

        try:
            agent._agent_definition_registry = self.agent_definitions
            agent._multi_agent_orchestrator = self
            agent._coordinator_mode = self.coordinator
        except Exception:
            logger.debug("Could not bind agent definition registry", exc_info=True)

    def set_event_sink(self, sink: Callable[[str, dict[str, Any]], Any] | None) -> None:
        self.event_sink = sink
        for manager in self._task_managers.values():
            manager.set_event_sink(sink)

    def activate_conversation(
        self,
        *,
        session_id: str | None,
        session_dir: str | Path | None = None,
    ) -> TaskManager:
        """Return the ``TaskManager`` for a session/conversation.

        A single OpenSpace process can serve several sessions over time. Keeping
        one manager per session prevents task IDs, output files, inboxes, and
        event sinks from leaking across users or conversations.
        """

        conversation_id = str(session_id or "default")
        manager = self._task_managers.get(conversation_id)
        if manager is None:
            manager = TaskManager(
                event_sink=self.event_sink,
                output_dir=self._task_output_dir(conversation_id, session_dir),
            )
            self._task_managers[conversation_id] = manager
        else:
            manager.set_event_sink(self.event_sink)
        self._active_conversation_id = conversation_id
        return manager

    def active_task_manager(self) -> TaskManager | None:
        """Return the currently active session TaskManager, if one exists."""

        if self._active_conversation_id is not None:
            manager = self._task_managers.get(self._active_conversation_id)
            if manager is not None:
                return manager
        if len(self._task_managers) == 1:
            return next(iter(self._task_managers.values()))
        return None

    async def background_all_foreground_tasks(self) -> list[str]:
        """Background foreground local tasks in the active session."""

        manager = self.active_task_manager()
        if manager is None:
            return []
        return await manager.background_all_foreground_tasks()

    def inject_context(self, context: dict[str, Any]) -> dict[str, Any]:
        """Attach session-scoped multi-agent state to an execution context."""

        manager = self.activate_conversation(
            session_id=context.get("session_id"),
            session_dir=context.get("session_dir"),
        )
        context["task_manager"] = manager
        context["multi_agent_orchestrator"] = self
        context["coordinator_mode"] = self.coordinator
        context.setdefault("coordinator_mode_enabled", self.coordinator.is_enabled(context))
        return context

    async def spawn_teammate(
        self,
        *,
        name: str,
        prompt: str,
        parent_context: Any,
        available_tools: Iterable[BaseTool],
        team_name: str | None = None,
        agent_type: str | None = None,
        model: str | None = None,
        description: str | None = None,
        plan_mode_required: bool = False,
    ) -> SpawnTeammateResult:
        """Spawn an in-process teammate via the active session ``TaskManager``.

        Teammates run inside the current OpenSpace process and share session
        state through the active ``TaskManager``.
        """

        if not name or not str(name).strip():
            raise ValueError("name is required for spawn operation")
        if not prompt or not str(prompt).strip():
            raise ValueError("prompt is required for spawn operation")

        selected = self._select_agent(agent_type)
        task_manager = self._manager_from_context(parent_context)
        sanitized_name = _sanitize_teammate_name(name)
        resolved_team = (
            team_name
            or getattr(parent_context, "team_name", None)
            or getattr(task_manager, "active_team_name", None)
        )
        teammate_id = _format_teammate_id(sanitized_name, resolved_team)
        resolved_model = model or selected.model or getattr(parent_context, "model", None)
        task_description = description or f"{sanitized_name}: {prompt[:50]}"
        resolved_tools = resolve_agent_tools(
            selected,
            list(available_tools),
            is_async=True,
            is_teammate=True,
        )

        async def runner(task: AgentTask) -> AgentToolResult:
            spawn_payload = {
                "agent_id": task.agent_id,
                "agent_type": selected.agent_type,
                "team_name": resolved_team,
                "description": task_description,
                "status": "running",
                "background": True,
                "task_id": task.id,
                "parent_task_id": getattr(parent_context, "task_id", None),
                "session_id": getattr(parent_context, "session_id", None),
            }
            await self._emit("agent_spawn", spawn_payload)
            await self._emit(
                "agent_event",
                {
                    "session_id": getattr(parent_context, "session_id", None),
                    "agent_id": task.agent_id,
                    "event": "agent_spawn",
                    "payload": spawn_payload,
                },
            )
            return await run_agent(
                agent_def=selected,
                prompt=prompt,
                filtered_tools=resolved_tools.resolved_tools,
                allowed_agent_types=resolved_tools.allowed_agent_types,
                parent_context=parent_context,
                parent_agent=None,
                grounding_client=self.grounding_client,
                llm_client=self.llm_client,
                resolved_model=str(resolved_model or ""),
                agent_id=task.agent_id,
                task_description=task_description,
                is_async_agent=True,
                abort_event=task.abort_event,
                message_source=task.inbox,
            )

        task = await task_manager.register_async_agent(
            runner=runner,
            prompt=prompt,
            description=task_description,
            agent_type=selected.agent_type,
            selected_agent=selected,
            model=str(resolved_model) if resolved_model else None,
            task_type=TaskType.IN_PROCESS_TEAMMATE,
            team_name=resolved_team,
            parent_task_id=getattr(parent_context, "task_id", None),
            parent_abort_event=getattr(parent_context, "abort_event", None),
            agent_id=teammate_id,
        )
        self._register_task_alias(task_manager, sanitized_name, task.id)
        if resolved_team:
            self._register_task_alias(task_manager, f"{sanitized_name}@{resolved_team}", task.id)

        return SpawnTeammateResult(
            teammate_id=teammate_id,
            agent_id=teammate_id,
            name=sanitized_name,
            task_id=task.id,
            team_name=resolved_team,
            agent_type=selected.agent_type,
            model=str(resolved_model) if resolved_model else None,
            color=_stable_color(teammate_id),
            plan_mode_required=plan_mode_required,
        )

    async def shutdown(self) -> None:
        for manager in list(self._task_managers.values()):
            await manager.stop_all()

    def _task_output_dir(
        self,
        conversation_id: str,
        session_dir: str | Path | None,
    ) -> Path | None:
        if session_dir is not None:
            return Path(session_dir) / "tasks"
        if self.output_root is not None:
            return self.output_root / conversation_id
        return None

    def _select_agent(self, agent_type: str | None) -> AgentDefinition:
        selected_type = agent_type or "general-purpose"
        agent = self.agent_definitions.get(selected_type)
        if agent is None:
            raise ValueError(f"Agent type '{selected_type}' not found")
        return agent

    def _manager_from_context(self, parent_context: Any) -> TaskManager:
        manager = getattr(parent_context, "task_manager", None)
        if isinstance(manager, TaskManager):
            return manager
        return self.activate_conversation(
            session_id=getattr(parent_context, "session_id", None),
            session_dir=getattr(parent_context, "session_dir", None),
        )

    def _register_task_alias(
        self,
        manager: TaskManager,
        alias: str,
        task_id: str,
    ) -> None:
        registrar = getattr(manager, "register_alias", None)
        if callable(registrar):
            registrar(alias, task_id)

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self.event_sink is None:
            return
        result = self.event_sink(event_type, data)
        if asyncio.iscoroutine(result):
            await result


def _sanitize_teammate_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name).strip()).strip("-")
    return cleaned or f"teammate-{int(time.time())}"


def _format_teammate_id(name: str, team_name: str | None) -> str:
    base = f"{name}@{team_name}" if team_name else name
    cleaned = re.sub(r"[^A-Za-z0-9_.@-]+", "-", base).strip("-")
    return f"t_{cleaned}"[:80]


def _stable_color(value: str) -> str:
    palette = ("blue", "green", "yellow", "magenta", "cyan", "red")
    return palette[sum(ord(ch) for ch in value) % len(palette)]


__all__ = [
    "MultiAgentOrchestrator",
    "SpawnTeammateResult",
]
