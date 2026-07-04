"""Best-effort task trace artifact upload orchestration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping

from openspace.cloud.client import OpenSpaceClient
from openspace.cloud.config import load_cloud_config
from openspace.cloud.local_mapping import CloudLocalMappingStore
from openspace.cloud.redaction import REDACTION_POLICY_VERSION
from openspace.cloud.task_trace_exporter import TaskTraceExporter
from openspace.cloud.task_trace_schema import TaskTraceArtifact, model_inventory_from_runtime
from openspace.cloud.telemetry_outbox import CloudTelemetryOutbox, utc_now_iso
from openspace.cloud.telemetry_payloads import (
    build_task_report_payload,
    build_usage_report_payload,
    short_cloud_request_id,
)
from openspace.config.constants import PROJECT_ROOT


class CloudTaskTraceReporter:
    """Generate a redacted trace artifact, upload it, then report task closure."""

    def __init__(
        self,
        *,
        client: OpenSpaceClient | None = None,
        mapping_store: CloudLocalMappingStore | None = None,
        outbox: CloudTelemetryOutbox | None = None,
        artifact_dir: str | Path | None = None,
        workspace_root: str | Path | None = None,
    ) -> None:
        self._client = client
        self._mapping_store = mapping_store
        self._outbox = outbox
        self._artifact_dir = Path(
            artifact_dir or PROJECT_ROOT / ".openspace" / "cloud-task-traces"
        )
        self._workspace_root = Path(workspace_root).resolve() if workspace_root else PROJECT_ROOT

    async def maybe_report_execution(
        self,
        final_result: Mapping[str, Any],
        *,
        task_id: str,
        session_id: str,
        runtime: Any | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._maybe_report_execution_sync,
            final_result,
            task_id=task_id,
            session_id=session_id,
            runtime=runtime,
        )

    def _maybe_report_execution_sync(
        self,
        final_result: Mapping[str, Any],
        *,
        task_id: str,
        session_id: str,
        runtime: Any | None = None,
    ) -> dict[str, Any]:
        cfg = load_cloud_config()
        if not cfg.enabled or cfg.telemetry_mode != "outbox" or not cfg.api_key:
            return {"status": "skipped", "reason": "cloud_telemetry_disabled"}

        client = self._client or OpenSpaceClient(cfg, mapping_store=self._mapping_store)
        mapping_store = self._mapping_store or client._local_mapping_store()
        outbox = self._outbox or CloudTelemetryOutbox(mapping_store.db_path)
        exporter = TaskTraceExporter(
            self._artifact_dir,
            workspace_root=self._workspace_root,
        )
        artifact = exporter.from_execution_result(
            final_result,
            task_id=task_id,
            session_id=session_id,
            mapping_store=mapping_store,
            model_inventory=model_inventory_from_runtime(runtime),
        )
        if artifact is None:
            return {"status": "skipped", "reason": "local_only"}
        if not artifact.upload_allowed:
            usage_outcomes = self._report_usage_events(
                client,
                outbox,
                artifact,
                final_result,
            )
            row = outbox.enqueue(
                endpoint="/api/v2/telemetry/task-reported",
                payload=self._task_report_payload(
                    artifact,
                    final_result,
                    artifact_ref=None,
                    artifact_status="missing",
                    error_code="TASK_TRACE_REDACTION_BLOCKED",
                ),
                workspace_root=self._workspace_root,
            )
            return {
                "status": "queued",
                "reason": "redaction_blocked",
                "outbox_request_id": row.request_id,
                "deny_findings": list(artifact.deny_findings),
                "usage_report_outcomes": usage_outcomes,
            }

        try:
            upload = client.upload_task_trace_artifact(
                artifact.archive_path,
                request_id=artifact.request_id,
                task_id=artifact.task_id,
                session_id=artifact.session_id,
                manifest_json=artifact.manifest,
                artifact_sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                collection_scope=artifact.collection_scope,
                collection_reason=artifact.collection_reason,
                cloud_skill_ids=list(artifact.cloud_skill_ids),
                package_ids=list(artifact.package_ids),
                compression=artifact.compression,
            )
        except Exception as exc:
            row = outbox.enqueue(
                endpoint="/api/v2/telemetry/task-reported",
                payload=self._task_report_payload(
                    artifact,
                    final_result,
                    artifact_ref=None,
                    artifact_status="pending",
                    error_code=type(exc).__name__,
                ),
                workspace_root=self._workspace_root,
            )
            outbox.mark_failed(row.request_id, row.payload_hash, error=str(exc))
            usage_outcomes = self._report_usage_events(
                client,
                outbox,
                artifact,
                final_result,
            )
            return {
                "status": "queued",
                "reason": "upload_failed",
                "outbox_request_id": row.request_id,
                "error": str(exc),
                "usage_report_outcomes": usage_outcomes,
            }

        artifact_ref = str(upload.get("artifact_ref") or "")
        task_report = self._task_report_payload(
            artifact,
            final_result,
            artifact_ref=artifact_ref,
            artifact_status="ready" if artifact_ref else "pending",
        )
        try:
            ack = client.report_telemetry("task-reported", task_report)
        except Exception as exc:
            row = outbox.enqueue(
                endpoint="/api/v2/telemetry/task-reported",
                payload=task_report,
                workspace_root=self._workspace_root,
            )
            outbox.mark_failed(row.request_id, row.payload_hash, error=str(exc))
            usage_outcomes = self._report_usage_events(
                client,
                outbox,
                artifact,
                final_result,
            )
            return {
                "status": "queued",
                "reason": "task_report_failed",
                "artifact_ref": artifact_ref,
                "outbox_request_id": row.request_id,
                "error": str(exc),
                "usage_report_outcomes": usage_outcomes,
            }

        usage_outcomes = self._report_usage_events(
            client,
            outbox,
            artifact,
            final_result,
        )
        return {
            "status": "reported",
            "artifact_ref": artifact_ref,
            "task_report_ack": ack,
            "usage_report_outcomes": usage_outcomes,
        }

    def _report_usage_events(
        self,
        client: OpenSpaceClient,
        outbox: CloudTelemetryOutbox,
        artifact: TaskTraceArtifact,
        final_result: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        for payload in self._usage_report_payloads(artifact, final_result):
            cloud_skill_id = str(payload.get("cloud_skill_id") or "")
            try:
                ack = client.report_telemetry("usage-reported", payload)
                outcomes.append({
                    "status": "reported",
                    "cloud_skill_id": cloud_skill_id,
                    "ack": ack,
                })
            except Exception as exc:
                row = outbox.enqueue(
                    endpoint="/api/v2/telemetry/usage-reported",
                    payload=payload,
                    workspace_root=self._workspace_root,
                )
                outbox.mark_failed(row.request_id, row.payload_hash, error=str(exc))
                outcomes.append({
                    "status": "queued",
                    "cloud_skill_id": cloud_skill_id,
                    "outbox_request_id": row.request_id,
                    "error": str(exc),
                })
        return outcomes

    def _task_report_payload(
        self,
        artifact: TaskTraceArtifact,
        final_result: Mapping[str, Any],
        *,
        artifact_ref: str | None,
        artifact_status: str,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        payload = build_task_report_payload(
            request_id=short_cloud_request_id(
                "task-final",
                artifact.task_id,
                artifact.session_id,
                final_result.get("status") or "unknown",
                artifact.sha256,
            ),
            occurred_at=utc_now_iso(),
            status=str(final_result.get("status") or "unknown"),
            task_id=artifact.task_id,
            session_id=artifact.session_id,
            trajectory_detail_level="redacted_detail",
            trajectory_artifact_status=artifact_status,
            trajectory_artifact_format=(
                str(artifact.manifest.get("artifact_format") or "")
                if artifact_ref
                else None
            ),
            trajectory_artifact_ref=artifact_ref,
            error_code=error_code,
            extras={
                "cloud_involved": True,
                "collection_scope": artifact.collection_scope,
                "collection_reason": artifact.collection_reason,
                "cloud_skill_ids": list(artifact.cloud_skill_ids),
                "package_ids": list(artifact.package_ids),
                "task_trace_manifest_hash": _manifest_hash(artifact.manifest),
                "task_trace_archive_sha256": artifact.sha256,
            },
            redaction_level="redacted",
            redaction_performed_by="client",
            redaction_policy_version=REDACTION_POLICY_VERSION,
        )
        return payload

    def _usage_report_payloads(
        self,
        artifact: TaskTraceArtifact,
        final_result: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        occurred_at = utc_now_iso()
        status = "ok" if _is_success_status(final_result.get("status")) else "error"
        duration_ms = _duration_ms(final_result)
        error_code = None if status == "ok" else _short_error_code(final_result)
        payloads: list[dict[str, Any]] = []
        for cloud_skill_id in dict.fromkeys(artifact.cloud_skill_ids):
            payloads.append(
                build_usage_report_payload(
                    request_id=short_cloud_request_id(
                        "usage",
                        artifact.task_id,
                        artifact.session_id,
                        cloud_skill_id,
                        artifact.sha256,
                    ),
                    cloud_skill_id=cloud_skill_id,
                    occurred_at=occurred_at,
                    usage_count=1,
                    status=status,
                    duration_ms=duration_ms,
                    error_code=error_code,
                )
            )
        return payloads


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    import hashlib
    import json

    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_success_status(status: Any) -> bool:
    return str(status or "").strip().lower() in {"success", "completed", "complete", "ok"}


def _duration_ms(final_result: Mapping[str, Any]) -> int | None:
    direct = final_result.get("duration_ms")
    if isinstance(direct, (int, float)) and direct >= 0:
        return int(round(float(direct)))
    seconds = final_result.get("execution_time")
    if isinstance(seconds, (int, float)) and seconds >= 0:
        return int(round(float(seconds) * 1000))
    return None


def _short_error_code(final_result: Mapping[str, Any]) -> str | None:
    for key in ("error_code", "code"):
        value = final_result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:128]
    error = final_result.get("error")
    if isinstance(error, BaseException):
        return type(error).__name__[:128]
    if isinstance(error, str) and error.strip():
        return error.strip().splitlines()[0][:128]
    return None
