"""Token budget continuation tracking.

Implementations:
  - query/tokenBudget.ts
  - utils/tokenBudget.ts

This module intentionally tracks *turn output tokens*, not context-window
tokens.  Context-window pressure remains owned by services.compact
auto-compact.  Token budget is the OpenSpace "+500k / spend 2m tokens" feature: when
the model naturally stops before spending the requested output budget, the loop
injects a meta user nudge and continues; when the budget is reached, or progress
has diminishing returns, the natural stop is allowed.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from typing import Literal

from openspace.llm.types import TokenUsage

COMPLETION_THRESHOLD = 0.9
DIMINISHING_THRESHOLD = 500

_SHORTHAND_START_RE = re.compile(r"^\s*\+(\d+(?:\.\d+)?)\s*(k|m|b)\b", re.I)
_SHORTHAND_END_RE = re.compile(r"\s\+(\d+(?:\.\d+)?)\s*(k|m|b)\s*[.!?]?\s*$", re.I)
_VERBOSE_RE = re.compile(
    r"\b(?:use|spend)\s+(\d+(?:\.\d+)?)\s*(k|m|b)\s*tokens?\b",
    re.I,
)
_VERBOSE_RE_G = re.compile(_VERBOSE_RE.pattern, re.I)

_MULTIPLIERS = {
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
}
_SLOTS_KW = {"slots": True} if sys.version_info >= (3, 10) else {}

TokenBudgetAction = Literal["continue", "stop"]


@dataclass(**_SLOTS_KW)
class TokenBudgetCompletionEvent:
    continuation_count: int
    pct: int
    turn_tokens: int
    budget: int
    diminishing_returns: bool
    duration_ms: int

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "continuationCount": self.continuation_count,
            "pct": self.pct,
            "turnTokens": self.turn_tokens,
            "budget": self.budget,
            "diminishingReturns": self.diminishing_returns,
            "durationMs": self.duration_ms,
            # snake_case mirrors the rest of OS runtime event payloads.
            "continuation_count": self.continuation_count,
            "turn_tokens": self.turn_tokens,
            "diminishing_returns": self.diminishing_returns,
            "duration_ms": self.duration_ms,
        }


@dataclass(**_SLOTS_KW)
class TokenBudgetDecision:
    action: TokenBudgetAction
    nudge_message: str | None = None
    continuation_count: int = 0
    pct: int = 0
    turn_tokens: int = 0
    budget: int = 0
    completion_event: TokenBudgetCompletionEvent | None = None


@dataclass(**_SLOTS_KW)
class BudgetTracker:
    continuation_count: int = 0
    last_delta_tokens: int = 0
    last_global_turn_tokens: int = 0
    started_at: float = field(default_factory=lambda: time.time() * 1000)
    total_output_tokens: int = 0

    def record_usage(self, usage: TokenUsage | None) -> None:
        if usage is None:
            return
        self.total_output_tokens += max(0, int(usage.output_tokens or 0))

    def check(
        self,
        *,
        agent_id: str | None,
        budget: int | None,
        global_turn_tokens: int | None = None,
    ) -> TokenBudgetDecision:
        return check_token_budget(
            self,
            agent_id,
            budget,
            self.total_output_tokens if global_turn_tokens is None else global_turn_tokens,
        )


def create_budget_tracker() -> BudgetTracker:
    return BudgetTracker()


def _parse_budget_match(value: str, suffix: str) -> int:
    return int(float(value) * _MULTIPLIERS[suffix.lower()])


def parse_token_budget(text: str) -> int | None:
    start_match = _SHORTHAND_START_RE.search(text)
    if start_match:
        return _parse_budget_match(start_match.group(1), start_match.group(2))

    end_match = _SHORTHAND_END_RE.search(text)
    if end_match:
        return _parse_budget_match(end_match.group(1), end_match.group(2))

    verbose_match = _VERBOSE_RE.search(text)
    if verbose_match:
        return _parse_budget_match(verbose_match.group(1), verbose_match.group(2))

    return None


def find_token_budget_positions(text: str) -> list[dict[str, int]]:
    positions: list[dict[str, int]] = []
    start_match = _SHORTHAND_START_RE.search(text)
    if start_match:
        offset = start_match.start() + len(start_match.group(0)) - len(start_match.group(0).lstrip())
        positions.append({"start": offset, "end": start_match.end()})

    end_match = _SHORTHAND_END_RE.search(text)
    if end_match:
        end_start = end_match.start() + 1
        already_covered = any(
            end_start >= pos["start"] and end_start < pos["end"]
            for pos in positions
        )
        if not already_covered:
            positions.append({"start": end_start, "end": end_match.end()})

    for match in _VERBOSE_RE_G.finditer(text):
        positions.append({"start": match.start(), "end": match.end()})

    return positions


def get_budget_continuation_message(pct: int, turn_tokens: int, budget: int) -> str:
    return (
        f"Stopped at {pct}% of token target "
        f"({turn_tokens:,} / {budget:,}). Keep working \u2014 do not summarize."
    )


def check_token_budget(
    tracker: BudgetTracker,
    agent_id: str | None,
    budget: int | None,
    global_turn_tokens: int,
) -> TokenBudgetDecision:
    if agent_id or budget is None or budget <= 0:
        return TokenBudgetDecision(action="stop")

    turn_tokens = max(0, int(global_turn_tokens))
    pct = round((turn_tokens / budget) * 100)
    delta_since_last_check = turn_tokens - tracker.last_global_turn_tokens

    is_diminishing = (
        tracker.continuation_count >= 3
        and delta_since_last_check < DIMINISHING_THRESHOLD
        and tracker.last_delta_tokens < DIMINISHING_THRESHOLD
    )

    if not is_diminishing and turn_tokens < budget * COMPLETION_THRESHOLD:
        tracker.continuation_count += 1
        tracker.last_delta_tokens = delta_since_last_check
        tracker.last_global_turn_tokens = turn_tokens
        return TokenBudgetDecision(
            action="continue",
            nudge_message=get_budget_continuation_message(pct, turn_tokens, budget),
            continuation_count=tracker.continuation_count,
            pct=pct,
            turn_tokens=turn_tokens,
            budget=budget,
        )

    if is_diminishing or tracker.continuation_count > 0:
        event = TokenBudgetCompletionEvent(
            continuation_count=tracker.continuation_count,
            pct=pct,
            turn_tokens=turn_tokens,
            budget=budget,
            diminishing_returns=is_diminishing,
            duration_ms=max(0, int(time.time() * 1000 - tracker.started_at)),
        )
        return TokenBudgetDecision(
            action="stop",
            continuation_count=tracker.continuation_count,
            pct=pct,
            turn_tokens=turn_tokens,
            budget=budget,
            completion_event=event,
        )

    return TokenBudgetDecision(action="stop")
