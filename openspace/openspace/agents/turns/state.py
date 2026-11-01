"""Mutable state for a single GroundingAgent turn loop."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from openspace.agents.turns import compaction_controller, stop_policy
from openspace.llm.effort import resolve_applied_effort
from openspace.services.conversation.compact import AutoCompactTracking
from openspace.services.runtime_support.budget import BudgetTracker


@dataclass(slots=True)
class TurnState:
    """Per-turn counters and effective model selection.

    The effective model fields are intentionally task-local.  The loop must not
    mutate the shared LLM client when fallback handling switches models.
    """

    max_iterations: int
    effective_model: str
    effective_fallback_model: str | None
    base_reasoning_effort: Any = None
    effective_reasoning_effort: str | int | None = None
    current_turn_token_budget: int | None = None
    current_iteration: int = 0
    all_tool_results: list[dict[str, Any]] = field(default_factory=list)
    iteration_contexts: list[dict[str, Any]] = field(default_factory=list)
    consecutive_empty: int = 0
    max_consecutive_empty: int = stop_policy.MAX_CONSECUTIVE_EMPTY
    max_output_tokens_recovery_count: int = 0
    compact_tracking: AutoCompactTracking = field(default_factory=AutoCompactTracking)
    stop_reason_final: str | None = None
    conversation_recovery_retry_count: int = 0
    budget_tracker: BudgetTracker = field(default_factory=BudgetTracker)
    started_at_monotonic: float = field(default_factory=time.monotonic)
    bench_finalize_nudge_count: int = 0
    bench_finalize_nudge_iteration: int | None = None
    bench_finalize_nudge_monotonic: float | None = None
    bench_finalize_last_tool_iteration: int | None = None
    bench_finalize_last_tool_monotonic: float | None = None
    bench_visible_checker_failed: bool = False
    bench_visible_checker_failure_iteration: int | None = None
    bench_visible_checker_failure_command: str | None = None
    bench_visible_checker_failure_excerpt: str | None = None
    bench_visible_checker_failure_file_path: str | None = None
    bench_visible_checker_failure_file_sha256: str | None = None
    bench_visible_checker_pass_iteration: int | None = None
    force_tool_choice_next_call: bool = False

    @classmethod
    def from_agent_context(
        cls,
        agent: Any,
        context: dict[str, Any],
        instruction: str,
        tool_use_context: Any,
        *,
        max_iterations: int,
    ) -> "TurnState":
        effective_model = str(
            getattr(agent._llm_client, "model", "") or ""
        ) or "unknown"
        effective_fallback_model = getattr(agent._llm_client, "fallback_model", None)
        base_reasoning_effort = (
            context.get("reasoning_effort")
            if context.get("reasoning_effort") is not None
            else context.get("effort")
        )
        if tool_use_context.skill_model_override:
            effective_model = str(tool_use_context.skill_model_override)

        state = cls(
            max_iterations=max_iterations,
            effective_model=effective_model,
            effective_fallback_model=effective_fallback_model,
            base_reasoning_effort=base_reasoning_effort,
            current_turn_token_budget=compaction_controller.resolve_turn_token_budget(
                context,
                str(instruction),
            ),
        )
        state.effective_reasoning_effort = state.resolve_current_effort(
            tool_use_context
        )
        return state

    def begin_iteration(self) -> int:
        self.current_iteration += 1
        return self.current_iteration

    def resolve_current_effort(self, tool_use_context: Any) -> str | int | None:
        requested_effort = (
            tool_use_context.skill_effort_override
            if tool_use_context.skill_effort_override is not None
            else self.base_reasoning_effort
        )
        resolved = resolve_applied_effort(self.effective_model, requested_effort)
        return getattr(resolved, "value", resolved)

    def refresh_reasoning_effort(self, tool_use_context: Any) -> str | int | None:
        self.effective_reasoning_effort = self.resolve_current_effort(
            tool_use_context
        )
        return self.effective_reasoning_effort

    def switch_to_fallback(self, fallback_model: str) -> None:
        self.effective_model = fallback_model
        self.effective_fallback_model = None
        self.effective_reasoning_effort = None

    def reset_max_output_recovery(self) -> None:
        self.max_output_tokens_recovery_count = 0
