"""
Query execution guard — ensures mutual exclusion for concurrent queries.
"""
from __future__ import annotations

import asyncio
import enum
from typing import Optional

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class QueryState(enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    RESERVED = "reserved"


class QueryGuard:
    """Ensures only one query runs at a time with optional reservation."""

    def __init__(self) -> None:
        self._state = QueryState.IDLE
        self._lock = asyncio.Lock()
        self._cancel_event = asyncio.Event()
        self._reserved_query: Optional[str] = None
        self._running_task: Optional[asyncio.Task] = None

    @property
    def state(self) -> QueryState:
        return self._state

    @property
    def is_idle(self) -> bool:
        return self._state == QueryState.IDLE

    @property
    def is_running(self) -> bool:
        return self._state == QueryState.RUNNING

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    async def try_start(self) -> bool:
        """Attempt to transition from IDLE to RUNNING.
        Returns True if the guard was acquired, False if already running."""
        async with self._lock:
            if self._state != QueryState.IDLE:
                logger.warning("QueryGuard: cannot start — state is %s", self._state.value)
                return False
            self._state = QueryState.RUNNING
            self._cancel_event.clear()
            logger.debug("QueryGuard: IDLE → RUNNING")
            return True

    def end(self) -> None:
        """Transition back to IDLE after query completes."""
        prev = self._state
        self._state = QueryState.IDLE
        self._running_task = None
        self._cancel_event.clear()
        logger.debug("QueryGuard: %s → IDLE", prev.value)

    def reserve(self, query: str) -> None:
        """Queue a query for execution when the current one finishes."""
        self._reserved_query = query
        self._state = QueryState.RESERVED
        logger.debug("QueryGuard: reserved query (%.60s...)", query)

    def take_reserved(self) -> Optional[str]:
        """Consume the reserved query, returning it or None."""
        q = self._reserved_query
        self._reserved_query = None
        if self._state == QueryState.RESERVED:
            self._state = QueryState.IDLE
        return q

    def cancel(self) -> None:
        """Signal cancellation to the running query."""
        if self._state == QueryState.RUNNING:
            self._cancel_event.set()
            logger.info("QueryGuard: cancel requested")
            if self._running_task and not self._running_task.done():
                self._running_task.cancel()

    def bind_task(self, task: asyncio.Task) -> None:
        """Bind the currently running asyncio.Task for cancellation support."""
        self._running_task = task

    async def wait_for_cancel(self) -> None:
        """Await until cancel() is called. Useful for cooperative cancellation."""
        await self._cancel_event.wait()

    def check_cancelled(self) -> None:
        """Raise asyncio.CancelledError if cancellation was requested."""
        if self._cancel_event.is_set():
            raise asyncio.CancelledError("Query cancelled via QueryGuard")
