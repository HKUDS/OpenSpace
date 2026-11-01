from __future__ import annotations

import asyncio
from typing import Any

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class ExecutionFinalizer:
    """Stops recording, runs post-execution work, and persists the session."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)

    async def finalize(
        self,
        *,
        task_id: str,
        start_time: float,
        execution_time: float,
        result: dict[str, Any],
        execution_context: dict[str, Any],
        memory_drain_timeout: float,
        evolved_skills: list[dict[str, Any]],
        capture_skill_dir: str | None,
        cancelled_exc: asyncio.CancelledError | None,
    ) -> dict[str, Any]:
        recording_dir = None
        recording_manager = self.state.recording_manager
        if recording_manager and recording_manager.recording_status:
            recording_dir = recording_manager.trajectory_dir

        final_result = {
            **result,
            "task_id": task_id,
            "session_id": self.current_session_id,
            "execution_time": execution_time,
            "skills_used": result.get("active_skills", []),
            "evolved_skills": list(evolved_skills),
        }
        if capture_skill_dir:
            final_result["capture_skill_dir"] = capture_skill_dir
        if "session_capability_state" not in final_result:
            capability_state = execution_context.get("session_capability_state")
            if capability_state is not None:
                final_result["session_capability_state"] = capability_state

        try:
            await self._emit_task_finish_evidence(
                "task_finished_pre_persist",
                task_id=task_id,
                recording_dir=recording_dir,
                final_result=final_result,
                execution_context=execution_context,
                capture_skill_dir=capture_skill_dir,
            )
            await self.drain_memory_background_tasks(
                timeout_s=memory_drain_timeout,
                reason="pre_persist",
                context=execution_context,
            )
            await self.session_runtime.persist(final_result, execution_context)
            await self._scan_session_evidence_checkpoint(
                task_id=task_id,
                execution_context=execution_context,
            )
            await self._scan_skill_evidence_checkpoint(
                task_id=task_id,
                execution_context=execution_context,
            )
            await self._scan_tool_quality_evidence_checkpoint()
            await self._emit_task_finish_evidence(
                "task_session_persisted",
                task_id=task_id,
                recording_dir=recording_dir,
                final_result=final_result,
                execution_context=execution_context,
                capture_skill_dir=capture_skill_dir,
            )
            await self._scan_quality_signal_checkpoint(
                task_id=task_id,
                execution_context=execution_context,
            )

            post_execution_mode = self.post_execution_mode()
            if cancelled_exc is None and post_execution_mode == "inline":
                post_execution = self.run_post_execution_tasks(
                    task_id,
                    recording_dir,
                    result,
                    evolved_skills=evolved_skills,
                    capture_skill_dir=capture_skill_dir,
                )
                post_execution_timeout_s = self.post_execution_timeout_s()
                try:
                    if post_execution_timeout_s > 0:
                        await asyncio.wait_for(
                            post_execution,
                            timeout=post_execution_timeout_s,
                        )
                    else:
                        await post_execution
                except asyncio.TimeoutError:
                    logger.warning(
                        "Inline post-execution tasks timed out after %.2fs; "
                        "returning task result without waiting for more evolution",
                        post_execution_timeout_s,
                    )
                    final_result["post_execution_timed_out"] = True
                else:
                    final_result["evolved_skills"] = list(evolved_skills)

            if cancelled_exc is None and post_execution_mode == "background":
                self.schedule_post_execution_tasks(
                    task_id,
                    recording_dir,
                    result,
                    evolved_skills=evolved_skills,
                    capture_skill_dir=capture_skill_dir,
                )

            if cancelled_exc is None:
                await self._maybe_report_cloud_task_trace(
                    task_id=task_id,
                    final_result=final_result,
                    execution_context=execution_context,
                )

            return final_result
        finally:
            await self._stop_recording(
                recording_manager=recording_manager,
                task_id=task_id,
                start_time=start_time,
                result=result,
            )

    async def _stop_recording(
        self,
        *,
        recording_manager: Any,
        task_id: str,
        start_time: float,
        result: dict[str, Any],
    ) -> None:
        if not recording_manager or not recording_manager.recording_status:
            return
        try:
            exec_time = asyncio.get_event_loop().time() - start_time
            await recording_manager.save_execution_outcome(
                status=result.get("status", "unknown"),
                iterations=result.get("iterations", 0),
                execution_time=exec_time,
            )
        except Exception:
            pass

        try:
            await recording_manager.stop()
            logger.debug(f"Recording stopped: {task_id}")
        except Exception as exc:
            logger.warning(f"Failed to stop recording: {exc}")

    async def _maybe_report_cloud_task_trace(
        self,
        *,
        task_id: str,
        final_result: dict[str, Any],
        execution_context: dict[str, Any],
    ) -> None:
        try:
            from openspace.cloud.task_trace_reporter import CloudTaskTraceReporter

            workspace_root = (
                execution_context.get("cwd")
                or execution_context.get("original_cwd")
                or getattr(self.config, "project_root", None)
            )
            reporter = CloudTaskTraceReporter(workspace_root=workspace_root)
            outcome = await reporter.maybe_report_execution(
                final_result,
                task_id=task_id,
                session_id=str(self.current_session_id or ""),
                runtime=self._runtime,
            )
            if outcome.get("status") not in {"skipped", "reported"}:
                logger.debug("Cloud task trace reporter outcome: %s", outcome)
        except Exception:
            logger.debug("Cloud task trace reporting skipped", exc_info=True)

    async def _emit_task_finish_evidence(
        self,
        event_type: str,
        *,
        task_id: str,
        recording_dir: str | None,
        final_result: dict[str, Any],
        execution_context: dict[str, Any],
        capture_skill_dir: str | None = None,
    ) -> None:
        storage = self.session_storage
        messages = final_result.get("messages")
        if not isinstance(messages, list):
            messages = execution_context.get("conversation_history")
        message_count = len(messages) if isinstance(messages, list) else None
        response = final_result.get("response")
        payload = {
            "task_id": task_id,
            "parent_task_id": execution_context.get("parent_task_id"),
            "session_id": self.current_session_id,
            "execution_time": final_result.get("execution_time"),
            "status": final_result.get("status"),
            "stop_reason": final_result.get("stop_reason"),
            "iterations": final_result.get("iterations"),
            "tool_execution_count": len(final_result.get("tool_executions") or []),
            "tool_executions": final_result.get("tool_executions") or [],
            "active_skills": (
                final_result.get("active_skills")
                or final_result.get("skills_used")
                or []
            ),
            "retrieved_tools_list": final_result.get("retrieved_tools_list") or [],
            "preselection_debug_info": final_result.get("preselection_debug_info"),
            "permission_mode": final_result.get("permission_mode"),
            "session_capability_state": final_result.get("session_capability_state"),
            "recording_dir": recording_dir,
            "capture_skill_dir": capture_skill_dir or final_result.get("capture_skill_dir"),
            "message_count": message_count,
            "final_response_preview": str(response or "")[:500],
        }
        if storage is not None:
            payload.update(
                {
                    "session_dir": str(storage.session_dir),
                    "transcript_path": str(storage.transcript_path),
                    "tool_results_dir": str(storage.tool_results_dir),
                    "file_history_dir": str(storage.file_history_dir),
                    "transcript_generation": getattr(
                        storage,
                        "current_generation",
                        None,
                    ),
                }
            )
        try:
            await self.emit_runtime_event(event_type, payload)
        except Exception:
            logger.debug("Failed to emit evidence event %s", event_type, exc_info=True)

    async def _scan_session_evidence_checkpoint(
        self,
        *,
        task_id: str,
        execution_context: dict[str, Any],
    ) -> None:
        storage = self.session_storage
        adapter = getattr(self.state, "evidence_runtime_adapter", None)
        scan = getattr(adapter, "ingest_session_storage_delta", None)
        if storage is None or scan is None:
            return
        try:
            result = scan(
                storage,
                task_id=task_id,
                parent_task_id=execution_context.get("parent_task_id"),
                agent_id=execution_context.get("agent_id") or "primary",
            )
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.debug("Session evidence checkpoint scan failed", exc_info=True)

    async def _scan_skill_evidence_checkpoint(
        self,
        *,
        task_id: str,
        execution_context: dict[str, Any],
    ) -> None:
        skill_store = getattr(self.state, "skill_store", None)
        adapter = getattr(self.state, "evidence_runtime_adapter", None)
        scan = getattr(adapter, "ingest_skill_store_delta", None)
        if skill_store is None or scan is None:
            return
        try:
            result = scan(
                skill_store,
                task_id=task_id,
            )
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.debug("Skill evidence checkpoint scan failed", exc_info=True)

    async def _scan_tool_quality_evidence_checkpoint(self) -> None:
        adapter = getattr(self.state, "evidence_runtime_adapter", None)
        scan = getattr(adapter, "ingest_tool_quality_delta", None)
        if scan is None:
            return
        quality_manager = None
        grounding_client = getattr(self.state, "grounding_client", None)
        if grounding_client is not None:
            quality_manager = getattr(grounding_client, "quality_manager", None)
        if quality_manager is None:
            return
        try:
            result = scan(quality_manager)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.debug("Tool quality evidence checkpoint scan failed", exc_info=True)

    async def _scan_quality_signal_checkpoint(
        self,
        *,
        task_id: str,
        execution_context: dict[str, Any],
    ) -> None:
        if not self._quality_signal_detector_enabled():
            return

        evidence_store = getattr(self.state, "evidence_store", None)
        if evidence_store is None:
            return

        signal_store = None
        try:
            from openspace.skill_engine.evidence import EvidenceScope
            from openspace.skill_engine.signals import (
                CHECKPOINT_TASK_SESSION_PERSISTED,
                QualitySignalDetector,
                QualitySignalStore,
            )

            latest_watermark = getattr(evidence_store, "latest_manifest_watermark", None)
            if not callable(latest_watermark):
                await self._emit_quality_signal_warning(
                    task_id,
                    "evidence_store_missing_latest_manifest_watermark",
                )
                return

            scan_watermark = int(latest_watermark())
            detector = QualitySignalDetector(evidence_store)
            signals = detector.scan_checkpoint(
                checkpoint_name=CHECKPOINT_TASK_SESSION_PERSISTED,
                scope=EvidenceScope(
                    session_id=self.current_session_id,
                    task_id=task_id,
                    source_task_ids=tuple(
                        item
                        for item in (
                            task_id,
                            execution_context.get("parent_task_id"),
                        )
                        if item
                    ),
                ),
                manifest_watermark=scan_watermark,
            )
            signal_store = QualitySignalStore(evidence_store)
            # Trigger job creation/drain is owned by the runtime cutover path.
            signal_store.upsert_many(signals)
        except Exception as exc:
            logger.debug("Quality signal checkpoint scan failed", exc_info=True)
            await self._emit_quality_signal_warning(task_id, str(exc))
        finally:
            close = getattr(signal_store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.debug("Quality signal store close failed", exc_info=True)

    def _quality_signal_detector_enabled(self) -> bool:
        return bool(
            getattr(self.config, "quality_signal_detector_enabled", True)
        )

    async def _emit_quality_signal_warning(
        self,
        task_id: str,
        error: str,
    ) -> None:
        try:
            await self.emit_runtime_event(
                "quality_signal_checkpoint_warning",
                {
                    "session_id": self.current_session_id,
                    "task_id": task_id,
                    "error": error,
                },
            )
        except Exception:
            logger.debug("Failed to emit quality signal warning", exc_info=True)
