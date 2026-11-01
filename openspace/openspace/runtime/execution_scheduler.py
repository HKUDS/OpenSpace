from __future__ import annotations

from typing import Any

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class ExecutionScheduler:
    """Coordinates scheduler startup and lazy scheduler injection per request."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)

    def install_ensure(
        self,
        *,
        execution_context: dict[str, Any],
        low_latency_profiler: Any,
        workspace_dir: str,
    ) -> None:
        async def ensure_scheduler(context: Any | None = None) -> Any:
            return await self.ensure_for_context(
                execution_context=execution_context,
                low_latency_profiler=low_latency_profiler,
                workspace_dir=workspace_dir,
                context=context,
            )

        execution_context["ensure_scheduler"] = ensure_scheduler

    async def ensure_for_context(
        self,
        *,
        execution_context: dict[str, Any],
        low_latency_profiler: Any,
        workspace_dir: str,
        context: Any | None = None,
    ) -> Any:
        scheduler_span = (
            low_latency_profiler.span("scheduler.ensure")
            if low_latency_profiler is not None
            else None
        )
        task_manager = (
            getattr(context, "task_manager", None)
            if context is not None and getattr(context, "task_manager", None) is not None
            else execution_context.get("task_manager")
        )
        if scheduler_span is not None:
            with scheduler_span:
                scheduler = await self.ensure_scheduler(
                    workspace_dir,
                    task_manager=task_manager,
                )
        else:
            scheduler = await self.ensure_scheduler(
                workspace_dir,
                task_manager=task_manager,
            )

        execution_context["scheduler"] = scheduler
        execution_context["notification_service"] = scheduler.notification_service
        execution_context["approval_service"] = scheduler.approval_service
        if context is not None:
            try:
                context.scheduler = scheduler
                context.notification_service = scheduler.notification_service
                context.approval_service = scheduler.approval_service
            except Exception:
                pass
        if low_latency_profiler is not None:
            marker = getattr(low_latency_profiler, "mark", None)
            if callable(marker):
                marker("scheduler.initialize_started", phase="execute")
        logger.info("scheduler.initialize_started")
        return scheduler

    async def maybe_start(
        self,
        *,
        task: str,
        execution_context: dict[str, Any],
        low_latency_profiler: Any,
        workspace_dir: str,
    ) -> None:
        should_start = self.should_start_scheduler_for_execute(task, execution_context)
        if not should_start:
            should_start = self.workspace_has_enabled_schedules(workspace_dir)
        if should_start:
            ensure_scheduler = execution_context.get("ensure_scheduler")
            if callable(ensure_scheduler):
                await ensure_scheduler()
            else:
                await self.ensure_for_context(
                    execution_context=execution_context,
                    low_latency_profiler=low_latency_profiler,
                    workspace_dir=workspace_dir,
                )
            return

        if low_latency_profiler is not None:
            marker = getattr(low_latency_profiler, "mark", None)
            if callable(marker):
                marker(
                    "scheduler.initialize_skipped_by_profile",
                    phase="execute",
                    profile=self.config.capability_profile,
                )
        logger.info("scheduler.initialize_skipped_by_profile")
