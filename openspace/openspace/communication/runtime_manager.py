from __future__ import annotations

import asyncio
import contextlib
import inspect
import uuid
from time import monotonic
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

from openspace.utils.logging import Logger
from openspace.services.runtime_support.low_latency import SessionCapabilityState
from openspace.runtime import ExecutionRequest, ExecutionResult

from .config import CommunicationConfig
from .types import ChannelMessage, ChannelSession

if TYPE_CHECKING:
    from openspace.services.runtime_support.low_latency import LowLatencyProfiler
    from openspace.communication.session_store import SessionStore
    from openspace import OpenSpace

logger = Logger.get_logger(__name__)


OpenSpaceFactory = Callable[..., Awaitable["OpenSpace"]]
RuntimeEventHandler = Callable[[str, Dict[str, Any]], Any]


def _execution_result_to_channel_payload(result: ExecutionResult) -> Dict[str, Any]:
    return {
        "status": result.status,
        "response": result.text,
        "error": result.error,
        "task_id": result.task_id,
        "session_id": result.session_id,
        "execution_time": result.execution_time,
        "iterations": result.iterations,
        "tool_executions": list(result.tool_executions),
        "skills_used": list(result.skills_used),
        "evolved_skills": list(result.evolved_skills),
        "active_skills": list(result.active_skills),
        "permission_mode": result.permission_mode,
        "session_capability_state": result.session_capability_state,
    }


class SessionRuntime:
    def __init__(
        self,
        session: ChannelSession,
        openspace_factory: OpenSpaceFactory,
        *,
        session_store: Optional["SessionStore"] = None,
        session_capability_state_enabled: bool = True,
    ):
        self.session = session
        self._openspace_factory = openspace_factory
        self._openspace: Optional[OpenSpace] = None
        self._lock = asyncio.Lock()
        self.last_used_monotonic = monotonic()
        self._session_store = session_store
        self._session_capability_state_enabled = session_capability_state_enabled
        self.capability_state = self._load_capability_state()

    @property
    def openspace(self) -> Optional[OpenSpace]:
        return self._openspace

    async def ensure_initialized(
        self,
        low_latency_profiler: Optional["LowLatencyProfiler"] = None,
    ) -> OpenSpace:
        if self._openspace is None:
            self._openspace = await self._call_openspace_factory(
                low_latency_profiler=low_latency_profiler,
            )
        self.last_used_monotonic = monotonic()
        return self._openspace

    async def _call_openspace_factory(
        self,
        *,
        low_latency_profiler: Optional["LowLatencyProfiler"] = None,
    ) -> OpenSpace:
        factory = self._openspace_factory
        try:
            signature = inspect.signature(factory)
            parameters = signature.parameters.values()
            accepts_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
            accepts_named_profiler = "low_latency_profiler" in signature.parameters
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
            ]
            if accepts_kwargs or accepts_named_profiler:
                return await factory(
                    self.session,
                    low_latency_profiler=low_latency_profiler,
                )
            if len(positional) >= 2:
                return await factory(self.session, low_latency_profiler)
        except (TypeError, ValueError):
            pass
        return await factory(self.session)

    async def execute_turn(
        self,
        *,
        message: ChannelMessage,
        conversation_history: list[dict[str, str]],
        channel_context: dict[str, Any],
        max_iterations: Optional[int] = None,
        low_latency_profiler: Optional["LowLatencyProfiler"] = None,
        runtime_event_handler: RuntimeEventHandler | None = None,
    ) -> Dict[str, Any]:
        async with self._lock:
            cold_runtime = self._openspace is None
            span = (
                low_latency_profiler.span(
                    "openspace.initialize",
                    cold_runtime=cold_runtime,
                )
                if low_latency_profiler is not None
                else contextlib.nullcontext()
            )
            with span:
                openspace = await self.ensure_initialized(low_latency_profiler)
            self.last_used_monotonic = monotonic()
            task_id = f"comm_{self.session.session_key}_{uuid.uuid4().hex[:10]}"
            execution_context: dict[str, Any] = {
                "conversation_history": conversation_history,
                "channel_context": channel_context,
                "session_key": self.session.session_key,
            }
            if self._session_capability_state_enabled:
                execution_context["session_capability_state"] = (
                    self.capability_state.to_dict()
                )
                execution_context["session_capability_state_enabled"] = True
                execution_context.setdefault(
                    "discovered_tool_names",
                    set(self.capability_state.discovered_tool_names),
                )
                execution_context.setdefault(
                    "discovered_skill_names",
                    set(self.capability_state.discovered_skill_names),
                )
                if self.capability_state.visible_skill_names:
                    execution_context.setdefault(
                        "sent_skill_names_by_agent",
                        {"primary": set(self.capability_state.visible_skill_names)},
                    )
            if low_latency_profiler is not None:
                execution_context["low_latency_profiler"] = low_latency_profiler
                execution_context["low_latency"] = {
                    "correlation_id": low_latency_profiler.correlation_id,
                    "profile": low_latency_profiler.profile,
                    "profiler_enabled": bool(low_latency_profiler.enabled),
                }
            if self.session.openspace_session_id:
                execution_context["session_id"] = self.session.openspace_session_id
            request = ExecutionRequest(
                prompt=message.text,
                context=execution_context,
                workspace_dir=self.session.workspace_dir,
                max_iterations=max_iterations,
                task_id=task_id,
            )

            async def _event_sink(event_type: str, data: Dict[str, Any]) -> None:
                if runtime_event_handler is None:
                    return
                result = runtime_event_handler(event_type, dict(data))
                if inspect.isawaitable(result):
                    await result

            register_event_sink = getattr(openspace, "register_event_sink", None)
            unregister_event_sink = getattr(openspace, "unregister_event_sink", None)
            should_register_events = (
                runtime_event_handler is not None
                and callable(register_event_sink)
                and callable(unregister_event_sink)
            )
            if should_register_events:
                register_event_sink(_event_sink)
            try:
                execution_result = await openspace.execute(request)
            finally:
                if should_register_events:
                    unregister_event_sink(_event_sink)
            result = _execution_result_to_channel_payload(execution_result)
            result_session_id = str(result.get("session_id", "")).strip()
            if result_session_id:
                self.session.openspace_session_id = result_session_id
            if self._session_capability_state_enabled:
                self._update_capability_state(result, execution_context)
            self.last_used_monotonic = monotonic()
            return result

    def _load_capability_state(self) -> SessionCapabilityState:
        if not self._session_capability_state_enabled:
            return SessionCapabilityState()
        if self._session_store is None:
            return SessionCapabilityState()
        return self._session_store.load_capability_state(self.session)

    def _update_capability_state(
        self,
        result: Dict[str, Any],
        execution_context: dict[str, Any],
    ) -> None:
        payload = result.get("session_capability_state")
        if payload is None:
            payload = execution_context.get("session_capability_state")
        self.capability_state = SessionCapabilityState.from_mapping(payload)
        result["session_capability_state"] = self.capability_state.to_dict()
        if self._session_store is not None:
            self._session_store.save_capability_state(
                self.session,
                self.capability_state,
            )

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
        *,
        session_store: Optional["SessionStore"] = None,
    ):
        self.config = config
        self._openspace_factory = openspace_factory
        self._session_store = session_store
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
        low_latency_profiler: Optional["LowLatencyProfiler"] = None,
        runtime_event_handler: RuntimeEventHandler | None = None,
    ) -> Dict[str, Any]:
        span = (
            low_latency_profiler.span("runtime.acquire")
            if low_latency_profiler is not None
            else contextlib.nullcontext()
        )
        with span:
            runtime = await self._get_or_create_runtime(session)
        async with self._semaphore:
            return await runtime.execute_turn(
                message=message,
                conversation_history=conversation_history,
                channel_context=channel_context,
                max_iterations=self.config.agent.max_iterations,
                low_latency_profiler=low_latency_profiler,
                runtime_event_handler=runtime_event_handler,
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
                runtime = SessionRuntime(
                    session,
                    self._openspace_factory,
                    session_store=self._session_store,
                    session_capability_state_enabled=(
                        self.config.low_latency.session_capability_state_enabled
                    ),
                )
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
