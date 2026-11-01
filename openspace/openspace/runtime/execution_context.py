from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from openspace.grounding.core.permissions import (
    get_permission_mode,
    set_session_permission_mode,
)
from openspace.services.lsp import get_lsp_server_manager
from openspace.utils.logging import Logger

from .execution_request import ExecutionRequest

logger = Logger.get_logger(__name__)

_TASK_WAIT_TIMEOUT = 660
_NORMAL_MEMORY_DRAIN_TIMEOUT_S = 3.0


class ExecutionContextManager:
    """Builds and mutates per-request execution context."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)

    async def wait_until_idle(self) -> None:
        if not self.state.running:
            return
        logger.info(
            "OpenSpace is busy - waiting up to %ds for the current task to finish...",
            _TASK_WAIT_TIMEOUT,
        )
        try:
            await asyncio.wait_for(self.state.task_done.wait(), timeout=_TASK_WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"OpenSpace is still running after waiting {_TASK_WAIT_TIMEOUT}s. "
                "Please try again later."
            )

    def record_interaction(self) -> None:
        try:
            from openspace.services.runtime_support.background import record_interaction

            record_interaction()
        except Exception:
            pass

    def memory_drain_timeout(self) -> float:
        configured = getattr(self.config, "memory_drain_timeout_s", None)
        if configured is None:
            return _NORMAL_MEMORY_DRAIN_TIMEOUT_S
        return max(0.0, float(configured))

    def build_initial_context(
        self,
        request: ExecutionRequest,
        task_id: str,
    ) -> dict[str, Any]:
        context = dict(request.context or {})
        if request.session_id and "session_id" not in context:
            context["session_id"] = request.session_id
        if request.metadata and "metadata" not in context:
            context["metadata"] = dict(request.metadata)
        if request.abort_event is not None:
            context["abort_event"] = request.abort_event
        context["task_id"] = task_id
        context["instruction"] = request.prompt
        return context

    def attach_runtime_context(
        self,
        execution_context: dict[str, Any],
    ) -> None:
        execution_context["cost_tracker"] = self.cost_tracker
        execution_context["lsp_manager"] = get_lsp_server_manager()
        execution_context["diagnostic_tracker"] = self.state.diagnostic_tracker
        if self.session_storage is not None:
            execution_context["session_storage"] = self.session_storage
            execution_context["session_dir"] = str(self.session_storage.session_dir)
            execution_context["tool_results_dir"] = str(
                self.session_storage.tool_results_dir
            )
            if self.file_history is not None:
                execution_context["file_history"] = self.file_history
        else:
            metadata = self.current_session_metadata
            session_dir = (
                str(metadata.get("session_dir", ""))
                if isinstance(metadata, dict)
                else ""
            )
            execution_context["session_dir"] = session_dir
            if isinstance(metadata, dict) and metadata.get("tool_results_dir"):
                execution_context["tool_results_dir"] = str(metadata["tool_results_dir"])
            elif session_dir:
                execution_context["tool_results_dir"] = str(
                    Path(session_dir).expanduser().resolve() / "tool-results"
                )

    def apply_config_defaults(
        self,
        execution_context: dict[str, Any],
    ) -> None:
        config = self.config
        execution_context.setdefault(
            "capability_profile",
            config.capability_profile,
        )
        execution_context.setdefault(
            "low_latency_enabled",
            config.low_latency_enabled,
        )
        execution_context.setdefault(
            "fast_tool_policy_enabled",
            config.fast_tool_policy_enabled,
        )
        execution_context.setdefault(
            "disable_fast_auto_preselection",
            config.disable_fast_auto_preselection,
        )
        execution_context.setdefault(
            "hard_active_tool_limit",
            config.hard_active_tool_limit,
        )
        if config.max_result_size_chars is not None:
            execution_context.setdefault(
                "max_result_size_chars",
                int(config.max_result_size_chars),
            )
        if config.max_tool_results_per_message_chars is not None:
            execution_context.setdefault(
                "max_tool_results_per_message_chars",
                int(config.max_tool_results_per_message_chars),
            )
        if config.active_tool_names is not None:
            execution_context.setdefault(
                "active_tool_names",
                list(config.active_tool_names),
            )
        if config.policy_deferred_tool_names is not None:
            execution_context.setdefault(
                "policy_deferred_tool_names",
                list(config.policy_deferred_tool_names),
            )
        if config.tool_retrieval_query is not None:
            execution_context.setdefault(
                "tool_retrieval_query",
                config.tool_retrieval_query,
            )
        execution_context.setdefault(
            "skills_disabled",
            bool(config.skills_disabled),
        )
        if config.memory_mode is not None:
            execution_context.setdefault("memory_mode", config.memory_mode)
        execution_context.setdefault(
            "tool_schema_cache_telemetry",
            config.tool_schema_cache_telemetry,
        )
        execution_context.setdefault(
            "skill_metadata_only_discovery",
            config.skill_metadata_only_discovery,
        )

    def apply_permission_mode(
        self,
        execution_context: dict[str, Any],
    ) -> None:
        if "permission_mode" in execution_context:
            return
        env_mode = os.environ.get("OPENSPACE_PERMISSION_MODE", "").strip()
        if env_mode:
            execution_context["permission_mode"] = env_mode
            return
        cwd = execution_context.get("workspace_dir") or getattr(
            self.config,
            "workspace_dir",
            None,
        )
        try:
            mode = get_permission_mode(str(cwd) if cwd else None)
        except Exception:
            logger.warning("Could not load permission mode; using default", exc_info=True)
            mode = "default"
        if isinstance(mode, str) and mode:
            execution_context["permission_mode"] = mode

    async def start_recording(self, task_id: str, task: str) -> None:
        recording_manager = self.state.recording_manager
        if not recording_manager:
            return
        if recording_manager.recording_status:
            await recording_manager.stop()
            logger.debug("Stopped previous recording session")

        recording_manager.task_id = task_id
        await recording_manager.start()
        await recording_manager.add_metadata("instruction", task)
        logger.info(f"Recording started: {task_id}")

    async def resolve_workspace(
        self,
        *,
        request: ExecutionRequest,
        task_id: str,
        execution_context: dict[str, Any],
    ) -> str:
        recording_workspace_dir = None
        recording_manager = self.state.recording_manager
        if recording_manager is not None:
            recording_workspace_dir = getattr(recording_manager, "trajectory_dir", None)
        resolution = self.workspace_runtime.resolve(
            request_workspace_dir=request.workspace_dir,
            context_workspace_dir=execution_context.get("workspace_dir"),
            recording_workspace_dir=recording_workspace_dir,
            task_id=task_id,
        )
        execution_context["workspace_dir"] = resolution.workspace_dir
        logger.info(f"Workspace: {resolution.workspace_dir}")
        return resolution.workspace_dir

    async def configure_workspace(self, workspace_dir: str) -> None:
        grounding_client = self.state.grounding_client
        if grounding_client is None:
            return
        try:
            await self.workspace_runtime.configure_shell_backend(
                grounding_client,
                str(workspace_dir),
            )
        except Exception:
            pass

    def resolve_max_iterations(self, request_max: int | None) -> int:
        if request_max is not None:
            return max(1, int(request_max))
        return int(self.config.grounding_max_iterations)

    def apply_final_permission_mode(
        self,
        result: dict[str, Any],
        execution_context: dict[str, Any],
    ) -> None:
        final_permission_mode = result.get("permission_mode")
        if isinstance(final_permission_mode, str) and final_permission_mode:
            cwd = execution_context.get("workspace_dir")
            try:
                set_session_permission_mode(
                    final_permission_mode,
                    str(cwd) if cwd else None,
                )
            except ValueError:
                logger.debug(
                    "Ignoring unsupported final permission mode %r",
                    final_permission_mode,
                )
