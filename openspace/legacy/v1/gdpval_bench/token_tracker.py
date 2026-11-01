"""
Token Tracker — intercepts ALL litellm calls via CustomLogger to track token usage.

Captures every LLM call made by OpenSpace internals:
  - Agent main loop (grounding_agent)        → source="agent"
  - Skill selection (skill_registry)         → source="skill_select"
  - Post-execution analysis (execution_analyzer) → source="analyzer"
  - Skill evolution (skill_evolver)          → source="evolver"
  - Tool result summarization (llm/client.py) → source="summarizer"

Each call is tagged with a ``source`` label via ``set_call_source()``.
The TokenStats class tracks agent-only and overhead tokens separately.

Usage (serial mode — one task at a time):
    tracker = TokenTracker()
    tracker.start()
    # ... run OpenSpace task ...
    stats = tracker.stop()
    print(f"Agent tokens: {stats.agent_prompt_tokens}")

Usage (concurrent mode — multiple tasks in parallel):
    tracker = TokenTracker()
    tracker.install()        # install callback once

    async def worker(task_id):
        ctx = tracker.begin_task(task_id)
        try:
            await cs.execute(...)
        finally:
            stats = tracker.end_task(task_id, ctx)

    tracker.uninstall()      # remove callback when done

Source tagging (call from any component):
    from gdpval_bench.token_tracker import set_call_source
    token = set_call_source("analyzer")
    try:
        await llm_client.complete(...)
    finally:
        reset_call_source(token)
"""

from __future__ import annotations

import contextvars
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import litellm
from litellm.integrations.custom_logger import CustomLogger

# ── ContextVar for concurrent per-task routing ──
_current_task_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "gdpval_current_task_id", default=None
)

# ── ContextVar for call source tagging ──
# Valid sources: "agent", "skill_select", "analyzer", "evolver", "summarizer"
# Default "agent" so the main grounding loop needs no annotation.
CALL_SOURCE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "gdpval_call_source", default="agent"
)

AGENT_SOURCE = "agent"


def set_call_source(source: str) -> contextvars.Token:
    """Set the call source for the current async context. Returns a reset token."""
    return CALL_SOURCE.set(source)


def reset_call_source(token: contextvars.Token) -> None:
    """Reset call source to its previous value."""
    CALL_SOURCE.reset(token)


@contextmanager
def call_source_ctx(source: str):
    """Context manager for temporarily setting call source."""
    tok = CALL_SOURCE.set(source)
    try:
        yield
    finally:
        CALL_SOURCE.reset(tok)


@dataclass
class TokenStats:
    """Accumulated token usage statistics for one tracking window.

    Tracks both *total* usage (all calls) and *agent-only* usage
    (source == "agent") so benchmarks can compare apples-to-apples.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    cost_usd: float = 0.0
    wall_time_sec: float = 0.0

    # Agent-only counters (source == "agent")
    agent_prompt_tokens: int = 0
    agent_completion_tokens: int = 0
    agent_total_tokens: int = 0
    agent_llm_calls: int = 0

    # Per-call breakdown (for detailed analysis)
    call_details: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self, include_details: bool = False) -> Dict[str, Any]:
        d = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "llm_calls": self.llm_calls,
            "cost_usd": round(self.cost_usd, 6),
            "wall_time_sec": round(self.wall_time_sec, 2),
            "agent_prompt_tokens": self.agent_prompt_tokens,
            "agent_completion_tokens": self.agent_completion_tokens,
            "agent_total_tokens": self.agent_total_tokens,
            "agent_llm_calls": self.agent_llm_calls,
        }
        if include_details:
            d["call_details"] = self.call_details
        return d

    def reset(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.llm_calls = 0
        self.cost_usd = 0.0
        self.wall_time_sec = 0.0
        self.agent_prompt_tokens = 0
        self.agent_completion_tokens = 0
        self.agent_total_tokens = 0
        self.agent_llm_calls = 0
        self.call_details.clear()


def _accumulate(stats: TokenStats, prompt_tok: int, completion_tok: int,
                total_tok: int, cost: float, detail: Optional[Dict],
                source: str = "agent") -> None:
    """Add one LLM call's usage to a TokenStats object (caller holds lock)."""
    stats.prompt_tokens += prompt_tok
    stats.completion_tokens += completion_tok
    stats.total_tokens += total_tok
    stats.llm_calls += 1
    stats.cost_usd += cost
    if source == AGENT_SOURCE:
        stats.agent_prompt_tokens += prompt_tok
        stats.agent_completion_tokens += completion_tok
        stats.agent_total_tokens += total_tok
        stats.agent_llm_calls += 1
    if detail is not None:
        stats.call_details.append(detail)


class _TokenLoggerCallback(CustomLogger):
    """litellm CustomLogger that routes token usage to the TokenTracker."""

    def __init__(self, tracker: "TokenTracker"):
        super().__init__()
        self._tracker = tracker

    def log_success_event(self, kwargs: dict, response_obj: Any,
                          start_time: Any, end_time: Any) -> None:
        self._tracker._on_success(kwargs, response_obj, start_time, end_time)

    async def async_log_success_event(self, kwargs: dict, response_obj: Any,
                                      start_time: Any, end_time: Any) -> None:
        self._tracker._on_success(kwargs, response_obj, start_time, end_time)


class TokenTracker:
    """Global token tracker using litellm CustomLogger callback.

    Thread-safe. Supports two modes:
      - **Serial**: start() → execute one task → stop()
      - **Concurrent**: install() → begin_task()/end_task() per task → uninstall()

    In concurrent mode, a ``contextvars.ContextVar`` is used to route each
    litellm callback to the correct per-task stats bucket.  This works because
    asyncio executes callbacks in the context of the coroutine that triggered
    the LLM call.
    """

    def __init__(self, record_details: bool = True):
        self._serial_stats = TokenStats()       # serial mode accumulator
        self._per_task: Dict[str, TokenStats] = {}  # concurrent mode buckets
        self._per_task_start: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._start_time: Optional[float] = None
        self._record_details = record_details
        self._active = False
        self._installed = False
        self._callback = _TokenLoggerCallback(self)

    # ─── Callback management ───────────────────────────────────────

    def _install_callback(self) -> None:
        if not self._installed:
            # Use litellm.callbacks (CustomLogger instances)
            if not hasattr(litellm, 'callbacks') or litellm.callbacks is None:
                litellm.callbacks = []
            litellm.callbacks.append(self._callback)
            self._installed = True

    def _uninstall_callback(self) -> None:
        if self._installed:
            try:
                litellm.callbacks.remove(self._callback)
            except (ValueError, AttributeError):
                pass
            self._installed = False

    # ─── Serial mode (backward compat) ─────────────────────────────

    def start(self) -> None:
        """Start tracking (serial). Resets counters and installs callback."""
        with self._lock:
            self._serial_stats.reset()
            self._start_time = time.monotonic()
            self._active = True
        self._install_callback()

    def stop(self) -> TokenStats:
        """Stop tracking (serial) and return final stats."""
        with self._lock:
            self._active = False
            if self._start_time is not None:
                self._serial_stats.wall_time_sec = time.monotonic() - self._start_time
            snapshot = self._copy_stats(self._serial_stats)
        self._uninstall_callback()
        return snapshot

    def snapshot(self) -> TokenStats:
        """Get current serial stats without stopping."""
        with self._lock:
            s = self._copy_stats(self._serial_stats)
            if self._start_time is not None:
                s.wall_time_sec = time.monotonic() - self._start_time
            return s

    # ─── Concurrent mode ───────────────────────────────────────────

    def install(self) -> None:
        """Install persistent callback for concurrent tracking."""
        self._active = True
        self._install_callback()

    def uninstall(self) -> None:
        """Remove persistent callback after all concurrent tasks finish."""
        self._active = False
        self._uninstall_callback()

    def begin_task(self, task_id: str) -> contextvars.Token:
        """Start per-task tracking. Returns a ContextVar token for reset.

        Must be called *inside* the asyncio Task that will run the LLM calls.
        """
        with self._lock:
            self._per_task[task_id] = TokenStats()
            self._per_task_start[task_id] = time.monotonic()
        return _current_task_id.set(task_id)

    def end_task(self, task_id: str, ctx_token: contextvars.Token) -> TokenStats:
        """Stop per-task tracking, return its stats, reset ContextVar."""
        _current_task_id.reset(ctx_token)
        with self._lock:
            stats = self._per_task.pop(task_id, TokenStats())
            t0 = self._per_task_start.pop(task_id, None)
            if t0 is not None:
                stats.wall_time_sec = time.monotonic() - t0
            return self._copy_stats(stats)

    # ─── Internals ─────────────────────────────────────────────────

    @staticmethod
    def _copy_stats(src: TokenStats) -> TokenStats:
        return TokenStats(
            prompt_tokens=src.prompt_tokens,
            completion_tokens=src.completion_tokens,
            total_tokens=src.total_tokens,
            llm_calls=src.llm_calls,
            cost_usd=src.cost_usd,
            wall_time_sec=src.wall_time_sec,
            agent_prompt_tokens=src.agent_prompt_tokens,
            agent_completion_tokens=src.agent_completion_tokens,
            agent_total_tokens=src.agent_total_tokens,
            agent_llm_calls=src.agent_llm_calls,
            call_details=list(src.call_details),
        )

    def _on_success(self, kwargs: dict, completion_response: Any,
                    start_time: Any, end_time: Any) -> None:
        """Called by _TokenLoggerCallback after every successful LLM call."""
        if not self._active:
            return

        # ── Parse usage ──
        usage = getattr(completion_response, "usage", None)
        prompt_tok = completion_tok = total_tok = 0
        if usage:
            prompt_tok = getattr(usage, "prompt_tokens", 0) or 0
            completion_tok = getattr(usage, "completion_tokens", 0) or 0
            total_tok = getattr(usage, "total_tokens", 0) or 0
            if total_tok == 0:
                total_tok = prompt_tok + completion_tok

        cost = 0.0
        try:
            cost = litellm.completion_cost(completion_response=completion_response) or 0.0
        except Exception:
            pass

        model = kwargs.get("model", "unknown")
        source = CALL_SOURCE.get("agent")

        # Capture wall-clock timestamp for retroactive analysis
        ts: Optional[float] = None
        if end_time is not None:
            try:
                if hasattr(end_time, 'timestamp'):
                    ts = end_time.timestamp()
                else:
                    ts = float(end_time)
            except (TypeError, ValueError):
                pass

        detail: Optional[Dict] = None
        if self._record_details:
            detail = {
                "model": model,
                "source": source,
                "prompt_tokens": prompt_tok,
                "completion_tokens": completion_tok,
                "total_tokens": total_tok,
                "cost_usd": round(cost, 6),
            }
            if ts is not None:
                detail["timestamp"] = round(ts, 3)

        # ── Route to correct bucket ──
        with self._lock:
            task_id = _current_task_id.get(None)
            if task_id is not None and task_id in self._per_task:
                _accumulate(self._per_task[task_id],
                            prompt_tok, completion_tok, total_tok, cost, detail,
                            source)
            else:
                _accumulate(self._serial_stats,
                            prompt_tok, completion_tok, total_tok, cost, detail,
                            source)
