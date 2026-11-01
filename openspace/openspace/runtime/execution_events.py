from __future__ import annotations

from typing import Any

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class ExecutionEventEmitter:
    """Emits task lifecycle events and runs the agent turn boundary."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)

    async def emit_start(
        self,
        *,
        task: str,
        task_id: str,
        session_id: str,
        max_iterations: int,
    ) -> None:
        await self.emit(
            "status_update",
            {
                "phase": "execution_start",
                "model": self.config.llm_model,
                "task_id": task_id,
                "session_id": session_id,
                "max_iterations": max_iterations,
                "cost_usd": self.cost_tracker.get_total(),
            },
        )
        await self.emit_runtime_event(
            "background_session_update",
            {
                "session_id": session_id,
                "title": task,
                "status": "running",
                "active_agent_id": "primary",
                "metadata": {
                    "task_id": task_id,
                    "model": self.config.llm_model,
                    "max_iterations": max_iterations,
                },
            },
        )
        await self.emit_runtime_event(
            "agent_spawn",
            {
                "session_id": session_id,
                "task_id": task_id,
                "parent_task_id": None,
                "agent_id": "primary",
                "agent_type": "root",
                "status": "running",
            },
        )
        await self.emit(
            "task_start",
            {
                "task_id": task_id,
                "title": task,
                "status": "execution_start",
            },
        )

    async def run_turns(
        self,
        *,
        task: str,
        task_id: str,
        session_id: str,
        max_iterations: int,
        execution_context: dict[str, Any],
    ) -> dict[str, Any]:
        logger.info(
            f"Executing with GroundingAgent "
            f"(max {max_iterations} iterations)..."
        )
        await self.emit(
            "task_progress",
            {
                "task_id": task_id,
                "title": task,
                "status": "tool_execution",
                "progress": "Running execution",
            },
        )
        await self.emit_runtime_event(
            "background_session_update",
            {
                "session_id": session_id,
                "title": task,
                "status": "running",
                "active_agent_id": "primary",
                "metadata": {
                    "task_id": task_id,
                    "phase": "tool_execution",
                    "progress": "Running execution",
                },
            },
        )
        await self.emit_runtime_event(
            "agent_task_update",
            {
                "session_id": session_id,
                "task_id": task_id,
                "parent_task_id": None,
                "agent_id": "primary",
                "agent_type": "root",
                "status": "running",
            },
        )
        execution_context["max_iterations"] = max_iterations
        return await self.turn_runner.run(execution_context)

    async def emit_complete(
        self,
        *,
        task: str,
        task_id: str,
        session_id: str,
        result: dict[str, Any],
        execution_time: float,
    ) -> None:
        status = result.get("status", "unknown")
        iterations = result.get("iterations", 0)
        tool_count = len(result.get("tool_executions", []))

        logger.info("=" * 60)
        if status == "success":
            logger.info(
                f"Task completed successfully! "
                f"({iterations} iterations, {tool_count} tool calls, "
                f"{execution_time:.2f}s)"
            )
            await self.emit(
                "task_complete",
                {
                    "task_id": task_id,
                    "status": "success",
                    "iterations": iterations,
                    "tool_calls": tool_count,
                    "execution_time": execution_time,
                },
            )
            await self.emit_runtime_event(
                "background_session_update",
                {
                    "session_id": session_id,
                    "title": task,
                    "status": "success",
                    "active_agent_id": "primary",
                    "metadata": {
                        "task_id": task_id,
                        "iterations": iterations,
                        "tool_calls": tool_count,
                        "execution_time": execution_time,
                    },
                },
            )
            await self.emit_runtime_event(
                "agent_task_complete",
                {
                    "session_id": session_id,
                    "task_id": task_id,
                    "parent_task_id": None,
                    "agent_id": "primary",
                    "agent_type": "root",
                    "status": "completed",
                },
            )
        elif status == "incomplete":
            logger.warning(
                f"Task incomplete after {iterations} iterations. "
                f"Consider increasing max_iterations."
            )
            await self.emit(
                "task_complete",
                {
                    "task_id": task_id,
                    "status": "incomplete",
                    "iterations": iterations,
                    "tool_calls": tool_count,
                    "execution_time": execution_time,
                },
            )
            await self.emit_runtime_event(
                "background_session_update",
                {
                    "session_id": session_id,
                    "title": task,
                    "status": "incomplete",
                    "active_agent_id": "primary",
                    "metadata": {
                        "task_id": task_id,
                        "iterations": iterations,
                        "tool_calls": tool_count,
                        "execution_time": execution_time,
                    },
                },
            )
            await self.emit_runtime_event(
                "agent_task_complete",
                {
                    "session_id": session_id,
                    "task_id": task_id,
                    "parent_task_id": None,
                    "agent_id": "primary",
                    "agent_type": "root",
                    "status": "incomplete",
                },
            )
        else:
            error_message = (
                result.get("error")
                or result.get("response")
                or result.get("warning")
                or result.get("status")
                or "Unknown error"
            )
            logger.error(f"Task failed: {error_message}")
            await self.emit(
                "task_error",
                {
                    "task_id": task_id,
                    "error": error_message,
                    "execution_time": execution_time,
                },
            )
            await self.emit_runtime_event(
                "background_session_update",
                {
                    "session_id": session_id,
                    "title": task,
                    "status": "error",
                    "active_agent_id": "primary",
                    "metadata": {
                        "task_id": task_id,
                        "error": error_message,
                        "execution_time": execution_time,
                    },
                },
            )
            await self.emit_runtime_event(
                "agent_task_complete",
                {
                    "session_id": session_id,
                    "task_id": task_id,
                    "parent_task_id": None,
                    "agent_id": "primary",
                    "agent_type": "root",
                    "status": "failed",
                },
            )
        logger.info("=" * 60)
