"""Arena-native AgentPilot method runner using trusted paired attempts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentpilot.app.assembly import RuntimeFactory
from agentpilot.app.runtime_config import AppConfig
from rsi_arena import ArenaSession


@dataclass
class AgentPilotArenaMethodRunner:
    """Run or resume one AgentPilot batch inside an Arena native session."""

    config: AppConfig
    benchmark_id: str
    run_id: str
    batch_id: str
    rounds: int
    resume: bool = False
    last_result: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.config, AppConfig):
            raise TypeError("config must be AppConfig")
        if self.benchmark_id not in {"aime", "tb2"}:
            raise ValueError("AgentPilot Arena benchmark must be aime or tb2")
        for name in ("run_id", "batch_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be non-empty")
        if (
            isinstance(self.rounds, bool)
            or not isinstance(self.rounds, int)
            or self.rounds < 1
        ):
            raise ValueError("rounds must be positive")
        if not isinstance(self.resume, bool):
            raise TypeError("resume must be boolean")

    def run(self, session: ArenaSession) -> None:
        if not isinstance(session, ArenaSession):
            raise TypeError("AgentPilot method requires ArenaSession")
        if tuple(session.development_task_ids) != (self.benchmark_id,):
            raise ValueError(
                "AgentPilot Arena session must contain exactly the selected benchmark"
            )
        if self.benchmark_id == "aime":
            from .aime.factory import create_factory
        else:
            from .tb2.factory import create_factory

        dependencies = create_factory().create_dependencies(
            self.config,
            arena_session=session,
            arena_benchmark_id=self.benchmark_id,
        )
        factory = RuntimeFactory(self.config, dependencies)
        if self.resume:
            runtime = factory.build_for_resume(batch_id=self.batch_id)
            self.last_result = runtime.resume()
        else:
            runtime = factory.build(run_id=self.run_id, batch_id=self.batch_id)
            self.last_result = runtime.run(rounds=self.rounds)


__all__ = ["AgentPilotArenaMethodRunner"]
