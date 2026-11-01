from __future__ import annotations

import asyncio
import os
import re
import traceback
import uuid
from pathlib import Path
from typing import Any

from openspace.utils.logging import Logger

from .execution_context import ExecutionContextManager
from .execution_events import ExecutionEventEmitter
from .execution_finalizer import ExecutionFinalizer
from .execution_request import ExecutionRequest, ExecutionResult
from .execution_scheduler import ExecutionScheduler

logger = Logger.get_logger(__name__)


def _normal_path(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return str(Path(text).expanduser())


def _first_env_skill_dir() -> str | None:
    raw = os.environ.get("OPENSPACE_HOST_SKILL_DIRS")
    if not raw:
        return None
    for item in re.split(rf"[,{re.escape(os.pathsep)}]", raw):
        path = _normal_path(item)
        if path:
            return path
    return None


class ExecutionLifecycle:
    """Owns the per-request execution lifecycle for OpenSpaceRuntime.

    The runtime remains the public owner of state and services. This class keeps
    the request orchestration readable and delegates context, scheduler, event,
    and finalization details to focused collaborators.
    """

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self.context_manager = ExecutionContextManager(runtime)
        self.scheduler = ExecutionScheduler(runtime)
        self.events = ExecutionEventEmitter(runtime)
        self.finalizer = ExecutionFinalizer(runtime)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)

    def _initial_capture_skill_dir(self, request: ExecutionRequest) -> str | None:
        for value in (
            request.capture_skill_dir,
            getattr(self.config, "capture_skill_dir", None),
            os.environ.get("OPENSPACE_CAPTURE_SKILL_DIR"),
            _first_env_skill_dir(),
        ):
            path = _normal_path(value)
            if path:
                return path
        return None

    def _default_capture_skill_dir(self, workspace_dir: Any) -> str:
        workspace = _normal_path(workspace_dir)
        if workspace is None:
            workspace = _normal_path(getattr(self.config, "workspace_dir", None))
        if workspace is None:
            workspace = os.getcwd()
        return str(Path(workspace).expanduser() / ".openspace" / "skills")

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        task = request.prompt

        if not self.state.initialized:
            raise RuntimeError(
                "OpenSpace not initialized. "
                "Call await initialize() before execute() or use async with."
            )

        await self.context_manager.wait_until_idle()

        logger.info("=" * 60)
        logger.info(f"Task: {task[:100]}...")
        logger.info("=" * 60)
        self.context_manager.record_interaction()

        self.state.running = True
        self.state.task_done.clear()

        loop = asyncio.get_event_loop()
        start_time = loop.time()
        task_id = request.task_id or f"task_{uuid.uuid4().hex[:12]}"
        logger.info(f"Task ID: {task_id}")

        result: dict[str, Any] = {}
        evolved_skills: list[dict[str, Any]] = []
        capture_skill_dir = self._initial_capture_skill_dir(request)
        execution_context: dict[str, Any] = {}
        execution_time = 0.0
        cancelled_exc: asyncio.CancelledError | None = None
        memory_drain_timeout = self.context_manager.memory_drain_timeout()

        try:
            execution_context = self.context_manager.build_initial_context(
                request,
                task_id,
            )
            low_latency_profiler = execution_context.get("low_latency_profiler")

            had_input_session_id = bool(execution_context.get("session_id"))
            session_id = await self.session_runtime.prepare(execution_context)
            if not self.current_session_id:
                self.current_session_id = session_id
            execution_context["session_id"] = session_id
            if "session_start_source" not in execution_context:
                if request.session_id or request.resume:
                    execution_context["session_start_source"] = "resume"
                elif not had_input_session_id:
                    execution_context["session_start_source"] = "startup"
            self.context_manager.attach_runtime_context(execution_context)

            if self.state.multi_agent is not None:
                self.state.multi_agent.inject_context(execution_context)

            if request.max_iterations is not None:
                execution_context["max_iterations"] = request.max_iterations
            if self.state.reasoning_effort:
                execution_context["reasoning_effort"] = self.state.reasoning_effort

            self.context_manager.apply_config_defaults(execution_context)
            self.context_manager.apply_permission_mode(execution_context)

            await self.context_manager.start_recording(task_id, task)
            workspace_dir = await self.context_manager.resolve_workspace(
                request=request,
                task_id=task_id,
                execution_context=execution_context,
            )
            if capture_skill_dir is None:
                capture_skill_dir = self._default_capture_skill_dir(workspace_dir)
            execution_context["capture_skill_dir"] = capture_skill_dir
            self.state.capture_skill_dir = capture_skill_dir
            self.scheduler.install_ensure(
                execution_context=execution_context,
                low_latency_profiler=low_latency_profiler,
                workspace_dir=workspace_dir,
            )

            await self.scheduler.maybe_start(
                task=task,
                execution_context=execution_context,
                low_latency_profiler=low_latency_profiler,
                workspace_dir=workspace_dir,
            )

            self.remember_memory_cleanup_context(execution_context)
            await self.context_manager.configure_workspace(workspace_dir)

            max_iterations = self.context_manager.resolve_max_iterations(
                request.max_iterations
            )
            await self.emit_runtime_event(
                "task_started",
                {
                    "task_id": task_id,
                    "parent_task_id": execution_context.get("parent_task_id"),
                    "session_id": session_id,
                    "agent_id": execution_context.get("agent_id") or "primary",
                    "instruction": request.prompt,
                    "workspace_dir": workspace_dir,
                    "max_iterations": max_iterations,
                    "permission_mode": execution_context.get("permission_mode"),
                    "session_start_source": execution_context.get(
                        "session_start_source"
                    ),
                    "model": self.config.llm_model,
                },
            )
            await self.events.emit_start(
                task=task,
                task_id=task_id,
                session_id=session_id,
                max_iterations=max_iterations,
            )

            result = await self.events.run_turns(
                task=task,
                task_id=task_id,
                session_id=session_id,
                max_iterations=max_iterations,
                execution_context=execution_context,
            )

            memory_drain_timeout = self.context_manager.memory_drain_timeout()
            await self.drain_memory_background_tasks(
                timeout_s=memory_drain_timeout,
                reason="agent_finished",
                context=execution_context,
            )

            execution_time = loop.time() - start_time
            self.context_manager.apply_final_permission_mode(
                result,
                execution_context,
            )
            await self.events.emit_complete(
                task=task,
                task_id=task_id,
                session_id=session_id,
                result=result,
                execution_time=execution_time,
            )

        except asyncio.CancelledError as exc:
            execution_time = loop.time() - start_time
            logger.warning("Task execution cancelled")
            await self.emit_runtime_event(
                "background_session_update",
                {
                    "session_id": self.current_session_id,
                    "title": task,
                    "status": "cancelled",
                    "active_agent_id": "primary",
                    "metadata": {
                        "task_id": task_id,
                        "execution_time": execution_time,
                    },
                },
            )
            result = {
                "status": "cancelled",
                "error": "Task execution cancelled",
                "response": "",
                "execution_time": execution_time,
                "task_id": task_id,
                "iterations": 0,
                "tool_executions": [],
            }
            cancelled_exc = exc

        except Exception as exc:
            execution_time = loop.time() - start_time
            tb = traceback.format_exc(limit=10)
            logger.error(f"Task execution failed: {exc}", exc_info=True)

            await self.emit(
                "task_error",
                {
                    "task_id": task_id,
                    "error": str(exc)[:500],
                    "execution_time": execution_time,
                },
            )
            await self.emit_runtime_event(
                "background_session_update",
                {
                    "session_id": self.current_session_id,
                    "title": task,
                    "status": "error",
                    "active_agent_id": "primary",
                    "metadata": {
                        "task_id": task_id,
                        "error": str(exc)[:500],
                        "execution_time": execution_time,
                    },
                },
            )

            result = {
                "status": "error",
                "error": str(exc),
                "traceback": tb,
                "response": f"Task execution error: {str(exc)}",
                "execution_time": execution_time,
                "task_id": task_id,
                "iterations": 0,
                "tool_executions": [],
            }

        finally:
            try:
                final_result = await self.finalizer.finalize(
                    task_id=task_id,
                    start_time=start_time,
                    execution_time=execution_time,
                    result=result,
                    execution_context=execution_context,
                    memory_drain_timeout=memory_drain_timeout,
                    evolved_skills=evolved_skills,
                    capture_skill_dir=capture_skill_dir,
                    cancelled_exc=cancelled_exc,
                )
            finally:
                self.state.running = False
                self.state.task_done.set()

        if cancelled_exc is not None:
            raise cancelled_exc
        return ExecutionResult.from_mapping(final_result)
