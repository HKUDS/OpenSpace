"""Shipped agentic baseline runner with baseline-owned A/B policy."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from rsi_arena.agentic import ArenaAgenticRunner
from rsi_arena.manifests import SampleSlot
from rsi_arena.service import ArenaService

from .boundary import AgentFacingArenaBoundary


class AgenticBaselineRunner(ArenaAgenticRunner):
    def __init__(
        self,
        service: ArenaService,
        *,
        fresh_attempt_slot_provider: Callable[[str], Mapping[str, Sequence[SampleSlot]]]
        | None = None,
        fixed_a_task_ids_by_benchmark: Mapping[str, Sequence[str]] | None = None,
        max_new_terminal_a_per_b_cycle: int | None = None,
    ) -> None:
        super().__init__(
            service,
            request_service=AgentFacingArenaBoundary(
                service,
                fresh_attempt_slot_provider=fresh_attempt_slot_provider,
                fixed_a_task_ids_by_benchmark=fixed_a_task_ids_by_benchmark,
                max_new_terminal_a_per_b_cycle=max_new_terminal_a_per_b_cycle,
            ),
        )


__all__ = ["AgenticBaselineRunner"]
