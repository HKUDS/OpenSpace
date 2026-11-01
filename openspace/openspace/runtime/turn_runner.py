from __future__ import annotations

from collections.abc import Callable
from typing import Any


class TurnRunner:
    """Boundary for invoking the agent turn loop."""

    def __init__(
        self,
        agent: Any | None = None,
        agent_getter: Callable[[], Any | None] | None = None,
    ) -> None:
        self._agent = agent
        self._agent_getter = agent_getter

    @classmethod
    def from_runtime(cls, runtime: Any) -> "TurnRunner":
        return cls(agent_getter=lambda: getattr(runtime, "grounding_agent", None))

    @property
    def agent(self) -> Any | None:
        return self._agent

    @agent.setter
    def agent(self, value: Any | None) -> None:
        self._agent = value

    async def run(self, execution_context: dict[str, Any]) -> dict[str, Any]:
        agent = self._agent
        if agent is None and self._agent_getter is not None:
            agent = self._agent_getter()
        if agent is None:
            raise RuntimeError("Grounding agent is not configured")
        result = await agent.process(execution_context)
        if not isinstance(result, dict):
            raise RuntimeError("Grounding agent returned a non-mapping result")
        return result
