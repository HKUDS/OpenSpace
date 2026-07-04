import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openspace.cloud.task_trace_reporter as reporter_module
from openspace.cloud.config import CloudConfig
from openspace.cloud.task_trace_reporter import CloudTaskTraceReporter
from openspace.cloud.task_trace_schema import TaskTraceArtifact
from openspace.cloud.telemetry_outbox import CloudTelemetryOutbox


def _run(coro):
    return asyncio.run(coro)


@dataclass
class FakeExporter:
    artifact_dir: Path
    workspace_root: Path

    artifact: TaskTraceArtifact | None = None

    def from_execution_result(self, *args, **kwargs):
        return self.artifact


class FakeClient:
    def __init__(
        self,
        *,
        artifact_ref: str = "artifact-ref-1",
        upload_error: Exception | None = None,
        task_report_error: Exception | None = None,
    ) -> None:
        self.artifact_ref = artifact_ref
        self.upload_error = upload_error
        self.task_report_error = task_report_error
        self.uploads: list[dict[str, Any]] = []
        self.reports: list[tuple[str, dict[str, Any]]] = []

    def upload_task_trace_artifact(self, archive_path, **kwargs):
        self.uploads.append({"archive_path": archive_path, **kwargs})
        if self.upload_error is not None:
            raise self.upload_error
        return {"artifact_ref": self.artifact_ref}

    def report_telemetry(self, event, payload):
        self.reports.append((event, payload))
        if event == "task-reported" and self.task_report_error is not None:
            raise self.task_report_error
        return {"ok": True}


class FakeMappingStore:
    def __init__(self, db_path):
        self.db_path = db_path


def _patch_cloud_config(monkeypatch):
    monkeypatch.setattr(
        reporter_module,
        "load_cloud_config",
        lambda: CloudConfig(
            mode="live",
            base_url="https://example.invalid",
            api_key="test-key",
            telemetry_mode="outbox",
        ),
    )


def _patch_exporter(monkeypatch, artifact):
    def make_exporter(artifact_dir, *, workspace_root):
        return FakeExporter(Path(artifact_dir), Path(workspace_root), artifact)

    monkeypatch.setattr(reporter_module, "TaskTraceExporter", make_exporter)


def _artifact(tmp_path, *, request_id="artifact-upload-request-1"):
    archive_path = tmp_path / "trace.tar.gz"
    archive_path.write_bytes(b"redacted trace")
    return TaskTraceArtifact(
        archive_path=archive_path,
        request_id=request_id,
        task_id="task-1",
        session_id="session-1",
        manifest={"artifact_format": "openspace_task_trace_v2"},
        sha256="a" * 64,
        size_bytes=archive_path.stat().st_size,
        compression="gzip",
        collection_scope="cloud_skill_used",
        collection_reason="cloud_skill_invoked",
        cloud_skill_ids=("cloud-skill-1",),
        package_ids=("pkg-1",),
    )


def _reporter(tmp_path, client, outbox):
    return CloudTaskTraceReporter(
        client=client,
        mapping_store=FakeMappingStore(tmp_path / "mapping.db"),
        outbox=outbox,
        artifact_dir=tmp_path / "artifacts",
        workspace_root=tmp_path,
    )


def test_upload_failure_queues_pending_task_report(monkeypatch, tmp_path):
    _patch_cloud_config(monkeypatch)
    _patch_exporter(monkeypatch, _artifact(tmp_path))
    outbox = CloudTelemetryOutbox(tmp_path / "outbox.db")
    client = FakeClient(upload_error=RuntimeError("upload unavailable"))
    reporter = _reporter(tmp_path, client, outbox)

    result = _run(
        reporter.maybe_report_execution(
            {"status": "success"},
            task_id="task-1",
            session_id="session-1",
        )
    )

    rows = outbox.list_by_status("failed")
    assert result["status"] == "queued"
    assert result["reason"] == "upload_failed"
    assert len(rows) == 1
    payload = rows[0].payload_redacted
    assert payload["trajectory_artifact_status"] == "pending"
    assert "trajectory_artifact_ref" not in payload
    assert payload["error_code"] == "RuntimeError"
    assert [call for call in client.reports if call[0] == "task-reported"] == []


def test_task_report_failure_queues_attempted_ready_payload(monkeypatch, tmp_path):
    _patch_cloud_config(monkeypatch)
    artifact_ref = "task-trace-artifact-ref"
    _patch_exporter(monkeypatch, _artifact(tmp_path))
    outbox = CloudTelemetryOutbox(tmp_path / "outbox.db")
    client = FakeClient(
        artifact_ref=artifact_ref,
        task_report_error=RuntimeError("task report rejected"),
    )
    reporter = _reporter(tmp_path, client, outbox)

    result = _run(
        reporter.maybe_report_execution(
            {"status": "success"},
            task_id="task-1",
            session_id="session-1",
        )
    )

    rows = outbox.list_by_status("failed")
    assert result["status"] == "queued"
    assert result["reason"] == "task_report_failed"
    assert result["artifact_ref"] == artifact_ref
    assert len(rows) == 1
    task_report_calls = [call for call in client.reports if call[0] == "task-reported"]
    assert len(task_report_calls) == 1
    event, attempted_payload = task_report_calls[0]
    payload = rows[0].payload_redacted
    assert event == "task-reported"
    assert payload == attempted_payload
    assert payload["request_id"] == attempted_payload["request_id"]
    assert payload["occurred_at"] == attempted_payload["occurred_at"]
    assert payload["trajectory_artifact_status"] == "ready"
    assert payload["trajectory_artifact_ref"] == artifact_ref
    assert payload["trajectory_artifact_format"] == "openspace_task_trace_v2"
    assert "error_code" not in payload
