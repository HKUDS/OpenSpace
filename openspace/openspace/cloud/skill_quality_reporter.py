"""Best-effort analyzer skill quality telemetry reporter."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from openspace.cloud.client import OpenSpaceClient
from openspace.cloud.config import (
    load_cloud_config,
    load_cloud_skill_quality_reporting_enabled,
)
from openspace.cloud.local_mapping import CloudLocalMappingStore
from openspace.cloud.redaction import REDACTION_POLICY_VERSION
from openspace.cloud.telemetry_outbox import CloudTelemetryOutbox
from openspace.cloud.telemetry_payloads import (
    build_skill_use_report_payload,
    short_cloud_request_id,
)
from openspace.config.constants import PROJECT_ROOT
from openspace.skill_engine.types import ExecutionAnalysis, SkillJudgment
from openspace.utils.logging import Logger


logger = Logger.get_logger(__name__)

QUALITY_EVENT_KIND = "skill_judgment"
QUALITY_SCHEMA_VERSION = "skill_quality_v1"
QUALITY_DENOMINATOR = "analyzer_judged_skill_use"
EXECUTOR_POLL_INTERVAL_SECONDS = 0.01


class CloudSkillQualityReporter:
    """Report persisted analyzer judgments as skill-use telemetry."""

    def __init__(
        self,
        *,
        client: OpenSpaceClient | None = None,
        mapping_store: CloudLocalMappingStore | None = None,
        outbox: CloudTelemetryOutbox | None = None,
        workspace_root: str | Path | None = None,
    ) -> None:
        self._client = client
        self._mapping_store = mapping_store
        self._outbox = outbox
        self._workspace_root = Path(workspace_root).resolve() if workspace_root else PROJECT_ROOT

    async def maybe_report_analysis(
        self,
        analysis: ExecutionAnalysis,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        executor: ThreadPoolExecutor | None = None
        result_future: ConcurrentFuture[dict[str, Any]] | None = None
        loop_future: asyncio.Future[Any] | None = None
        try:
            if not load_cloud_skill_quality_reporting_enabled():
                return _skill_quality_reporting_disabled_result()

            executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="openspace-skill-quality-reporter",
            )
            result_future = ConcurrentFuture()

            def run_sync_report() -> None:
                if not result_future.set_running_or_notify_cancel():
                    return
                try:
                    result_future.set_result(
                        self._maybe_report_analysis_sync(
                            analysis,
                            session_id=session_id,
                        )
                    )
                except BaseException as exc:
                    result_future.set_exception(exc)

            loop = asyncio.get_running_loop()
            loop_future = loop.run_in_executor(executor, run_sync_report)
            while not result_future.done():
                await asyncio.sleep(EXECUTOR_POLL_INTERVAL_SECONDS)
            if not loop_future.done():
                loop_future.cancel()
            return result_future.result()
        except Exception as exc:
            logger.warning(
                "Skill quality telemetry reporter failed for task %s: %s",
                getattr(analysis, "task_id", ""),
                exc,
            )
            return {
                "status": "skipped",
                "reason": "reporter_error",
                "error": type(exc).__name__,
            }
        finally:
            if loop_future is not None and not loop_future.done():
                loop_future.cancel()
            if executor is not None:
                work_finished = result_future is not None and result_future.done()
                executor.shutdown(wait=work_finished, cancel_futures=True)

    def _maybe_report_analysis_sync(
        self,
        analysis: ExecutionAnalysis,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        cfg = load_cloud_config()
        if not cfg.enabled or cfg.telemetry_mode != "outbox" or not cfg.api_key:
            return {"status": "skipped", "reason": "cloud_telemetry_disabled"}

        client = self._client or OpenSpaceClient(cfg, mapping_store=self._mapping_store)
        mapping_store = self._mapping_store or client._local_mapping_store()
        outbox = self._outbox or CloudTelemetryOutbox(mapping_store.db_path)

        outcomes: list[dict[str, Any]] = []
        for judgment in list(getattr(analysis, "skill_judgments", []) or []):
            local_skill_id = str(getattr(judgment, "skill_id", "") or "").strip()
            if not local_skill_id:
                outcomes.append({"status": "skipped", "reason": "missing_local_skill_id"})
                continue

            try:
                binding = mapping_store.get_binding_by_local(local_skill_id)
            except Exception as exc:
                outcomes.append(
                    {
                        "status": "failed",
                        "reason": "mapping_lookup_failed",
                        "local_skill_id": local_skill_id,
                        "error": type(exc).__name__,
                    }
                )
                continue
            cloud_skill_id = str(getattr(binding, "cloud_skill_id", "") or "").strip()
            if not cloud_skill_id:
                outcomes.append(
                    {
                        "status": "skipped",
                        "reason": "local_only",
                        "local_skill_id": local_skill_id,
                    }
                )
                continue

            payload = build_skill_quality_judgment_payload(
                analysis,
                judgment,
                cloud_skill_id=cloud_skill_id,
                session_id=session_id,
            )
            try:
                ack = client.report_telemetry("skill-use-reported", payload)
                outcomes.append(
                    {
                        "status": "reported",
                        "local_skill_id": local_skill_id,
                        "cloud_skill_id": cloud_skill_id,
                        "request_id": payload["request_id"],
                        "ack": ack,
                    }
                )
            except Exception as exc:
                try:
                    row = outbox.enqueue(
                        endpoint="/api/v2/telemetry/skill-use-reported",
                        payload=payload,
                        workspace_root=self._workspace_root,
                    )
                    outbox.mark_failed(
                        row.request_id,
                        row.payload_hash,
                        error=str(exc),
                    )
                    outcomes.append(
                        {
                            "status": "queued",
                            "reason": "report_failed",
                            "local_skill_id": local_skill_id,
                            "cloud_skill_id": cloud_skill_id,
                            "request_id": row.request_id,
                            "payload_hash": row.payload_hash,
                            "error": type(exc).__name__,
                        }
                    )
                except Exception as outbox_exc:
                    outcomes.append(
                        {
                            "status": "failed",
                            "reason": "outbox_enqueue_failed",
                            "local_skill_id": local_skill_id,
                            "cloud_skill_id": cloud_skill_id,
                            "request_id": payload["request_id"],
                            "error": type(outbox_exc).__name__,
                        }
                    )

        return _summarize_outcomes(outcomes)


def build_skill_quality_judgment_payload(
    analysis: ExecutionAnalysis,
    judgment: SkillJudgment,
    *,
    cloud_skill_id: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    local_skill_id = str(judgment.skill_id)
    skill_phase_failed_ids = set(getattr(analysis, "skill_phase_failed_skill_ids", []) or [])
    skill_applied = bool(judgment.skill_applied)
    task_completed = bool(analysis.task_completed)
    skill_phase_failed = local_skill_id in skill_phase_failed_ids
    completed = skill_applied and task_completed and not skill_phase_failed
    fallback = skill_phase_failed or not task_completed
    status = "success" if completed and not fallback else "failed"

    return build_skill_use_report_payload(
        request_id=short_cloud_request_id(
            "skill-quality-judgment",
            analysis.task_id,
            local_skill_id,
            cloud_skill_id,
        ),
        occurred_at=_analysis_timestamp_iso(analysis),
        status=status,
        task_id=analysis.task_id,
        cloud_skill_id=cloud_skill_id,
        session_id=session_id,
        local_skill_id=local_skill_id,
        duration_ms=None,
        failure_reason=None if status == "success" else "unknown",
        redaction_level="abstract_only",
        redaction_performed_by="client",
        redaction_policy_version=REDACTION_POLICY_VERSION,
        quality_event_kind=QUALITY_EVENT_KIND,
        quality_schema_version=QUALITY_SCHEMA_VERSION,
        denominator=QUALITY_DENOMINATOR,
        skill_applied=skill_applied,
        task_completed=task_completed,
        skill_phase_failed=skill_phase_failed,
        completed=completed,
        fallback=fallback,
    )


def _analysis_timestamp_iso(analysis: ExecutionAnalysis) -> str:
    timestamp = getattr(analysis, "timestamp", "")
    if hasattr(timestamp, "isoformat"):
        return timestamp.isoformat()
    return str(timestamp)


def _skill_quality_reporting_disabled_result() -> dict[str, Any]:
    return {"status": "skipped", "reason": "skill_quality_reporting_disabled"}


def _summarize_outcomes(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    if any(item.get("status") == "reported" for item in outcomes):
        status = "reported"
    elif any(item.get("status") == "queued" for item in outcomes):
        status = "queued"
    elif any(item.get("status") == "failed" for item in outcomes):
        status = "failed"
    else:
        status = "skipped"

    reason = None if outcomes else "no_skill_judgments"
    if status == "skipped" and outcomes:
        reason = "no_cloud_bound_skill_judgments"
    result: dict[str, Any] = {
        "status": status,
        "reported_count": sum(1 for item in outcomes if item.get("status") == "reported"),
        "queued_count": sum(1 for item in outcomes if item.get("status") == "queued"),
        "skipped_count": sum(1 for item in outcomes if item.get("status") == "skipped"),
        "failed_count": sum(1 for item in outcomes if item.get("status") == "failed"),
        "outcomes": outcomes,
    }
    if reason:
        result["reason"] = reason
    return result
