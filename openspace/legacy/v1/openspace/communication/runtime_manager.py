from __future__ import annotations

import asyncio
import contextlib
import uuid
from time import monotonic
from typing import Any, Awaitable, Callable, Dict, Optional

from openspace.tool_layer import OpenSpace
from openspace.utils.logging import Logger

from .config import CommunicationConfig
from .types import ChannelMessage, ChannelSession

logger = Logger.get_logger(__name__)


OpenSpaceFactory = Callable[[ChannelSession], Awaitable[OpenSpace]]


class SessionRuntime:
    def __init__(self, session: ChannelSession, openspace_factory: OpenSpaceFactory):
        self.session = session
        self._openspace_factory = openspace_factory
        self._openspace: Optional[OpenSpace] = None
        self._lock = asyncio.Lock()
        self.last_used_monotonic = monotonic()

    @property
    def openspace(self) -> Optional[OpenSpace]:
        return self._openspace

    async def ensure_initialized(self) -> OpenSpace:
        if self._openspace is None:
            self._openspace = await self._openspace_factory(self.session)
        self.last_used_monotonic = monotonic()
        return self._openspace

    async def execute_turn(
        self,
        *,
        message: ChannelMessage,
        conversation_history: list[dict[str, str]],
        channel_context: dict[str, Any],
        max_iterations: Optional[int] = None,
    ) -> Dict[str, Any]:
        async with self._lock:
            openspace = await self.ensure_initialized()
            self.last_used_monotonic = monotonic()
            task_id = f"comm_{self.session.session_key}_{uuid.uuid4().hex[:10]}"
            result = await openspace.execute(
                task=message.text,
                context={
                    "conversation_history": conversation_history,
                    "channel_context": channel_context,
                    "session_key": self.session.session_key,
                },
                workspace_dir=self.session.workspace_dir,
                max_iterations=max_iterations,
                task_id=task_id,
            )
            self.last_used_monotonic = monotonic()
            return result

    async def close(self) -> None:
        if self._openspace is not None:
            await self._openspace.cleanup()
            self._openspace = None

    def is_idle(self, idle_ttl_seconds: int) -> bool:
        if self._lock.locked():
            return False
        return (monotonic() - self.last_used_monotonic) >= idle_ttl_seconds


class SessionRuntimeManager:
    def __init__(
        self,
        config: CommunicationConfig,
        openspace_factory: OpenSpaceFactory,
    ):
        self.config = config
        self._openspace_factory = openspace_factory
        self._runtimes: Dict[str, SessionRuntime] = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(config.sessions.max_parallel_sessions)
        self._eviction_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._eviction_task is None:
            self._eviction_task = asyncio.create_task(self._evict_idle_loop())

    async def stop(self) -> None:
        if self._eviction_task is not None:
            self._eviction_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._eviction_task
            self._eviction_task = None

        async with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        for runtime in runtimes:
            await runtime.close()

    async def execute_turn(
        self,
        *,
        session: ChannelSession,
        message: ChannelMessage,
        conversation_history: list[dict[str, str]],
        channel_context: dict[str, Any],
    ) -> Dict[str, Any]:
        runtime = await self._get_or_create_runtime(session)
        async with self._semaphore:
            return await runtime.execute_turn(
                message=message,
                conversation_history=conversation_history,
                channel_context=channel_context,
                max_iterations=self.config.agent.max_iterations,
            )

    async def status(self) -> Dict[str, Any]:
        async with self._lock:
            return {
                "active_runtimes": len(self._runtimes),
                "session_keys": sorted(self._runtimes.keys()),
            }

    async def _get_or_create_runtime(self, session: ChannelSession) -> SessionRuntime:
        async with self._lock:
            runtime = self._runtimes.get(session.session_key)
            if runtime is None:
                runtime = SessionRuntime(session, self._openspace_factory)
                self._runtimes[session.session_key] = runtime
            return runtime

    async def _evict_idle_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            stale_keys: list[str] = []
            async with self._lock:
                for session_key, runtime in self._runtimes.items():
                    if runtime.is_idle(self.config.sessions.idle_ttl_seconds):
                        stale_keys.append(session_key)
                runtimes = [self._runtimes.pop(key) for key in stale_keys]
            for runtime in runtimes:
                logger.info("Evicting idle communication runtime: %s", runtime.session.session_key)
                await runtime.close()
