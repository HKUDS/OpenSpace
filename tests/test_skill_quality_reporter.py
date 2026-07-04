import asyncio
import json
from datetime import datetime

from openspace.cloud.config import (
    OPENSPACE_CLOUD_API_KEY_ENV,
    OPENSPACE_CLOUD_MODE_ENV,
    OPENSPACE_CLOUD_SKILL_QUALITY_REPORTING_ENV,
    OPENSPACE_CLOUD_TELEMETRY_MODE_ENV,
)
from openspace.cloud.local_mapping import SkillCloudBinding
from openspace.cloud.redaction import redact_telemetry_payload
from openspace.cloud.skill_quality_reporter import (
    QUALITY_DENOMINATOR,
    QUALITY_EVENT_KIND,
    QUALITY_SCHEMA_VERSION,
    CloudSkillQualityReporter,
    build_skill_quality_judgment_payload,
)
from openspace.cloud.telemetry_outbox import CloudTelemetryOutbox
from openspace.cloud.telemetry_payloads import short_cloud_request_id
from openspace.skill_engine.types import ExecutionAnalysis, SkillJudgment


def _run(coro):
    return asyncio.run(coro)


def _patch_host_env(monkeypatch):
    import openspace.cloud.config as cloud_config

    monkeypatch.setattr(cloud_config, "load_runtime_env", lambda: None)
    monkeypatch.setattr(cloud_config, "read_host_mcp_env", lambda: {})


def _set_cloud_env(
    monkeypatch,
    *,
    mode="live",
    telemetry_mode="outbox",
    api_key="test-key",
    quality=True,
):
    _patch_host_env(monkeypatch)
    for key in (
        OPENSPACE_CLOUD_MODE_ENV,
        OPENSPACE_CLOUD_TELEMETRY_MODE_ENV,
        OPENSPACE_CLOUD_API_KEY_ENV,
        OPENSPACE_CLOUD_SKILL_QUALITY_REPORTING_ENV,
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(OPENSPACE_CLOUD_MODE_ENV, mode)
    monkeypatch.setenv(OPENSPACE_CLOUD_TELEMETRY_MODE_ENV, telemetry_mode)
    if api_key is not None:
        monkeypatch.setenv(OPENSPACE_CLOUD_API_KEY_ENV, api_key)
    if quality:
        monkeypatch.setenv(OPENSPACE_CLOUD_SKILL_QUALITY_REPORTING_ENV, "1")


class FakeClient:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def report_telemetry(self, event, payload):
        self.calls.append((event, payload))
        if self.fail:
            raise RuntimeError("server does not support quality fields yet")
        return {"ok": True}


class FakeMappingStore:
    def __init__(self, tmp_path, bindings):
        self.db_path = tmp_path / "mapping.db"
        self.bindings = bindings
        self.lookups = []

    def get_binding_by_local(self, local_skill_id):
        self.lookups.append(local_skill_id)
        return self.bindings.get(local_skill_id)


def _analysis(
    *,
    task_id="task-1",
    timestamp=None,
    task_completed=True,
    judgments=None,
    phase_failed_ids=None,
):
    return ExecutionAnalysis(
        task_id=task_id,
        timestamp=timestamp or datetime(2026, 1, 2, 3, 4, 5, 123456),
        task_completed=task_completed,
        execution_note=(
            "RAW_EXECUTION_NOTE prompt transcript /tmp/private/file.diff token hash "
            "redacted_preview"
        ),
        tool_issues=["RAW_TOOL_ISSUE traceback /home/user/project/file.py"],
        skill_judgments=judgments
        or [
            SkillJudgment(
                skill_id="local-skill-1",
                skill_applied=True,
                note="RAW_SKILL_NOTE prompt diff token",
            )
        ],
        skill_phase_failed_skill_ids=phase_failed_ids or [],
    )


def test_gates_default_disabled_and_enabled(monkeypatch, tmp_path):
    cases = [
        {"mode": "off", "telemetry_mode": "outbox", "api_key": "k", "quality": True},
        {"mode": "live", "telemetry_mode": "off", "api_key": "k", "quality": True},
        {"mode": "live", "telemetry_mode": "outbox", "api_key": None, "quality": True},
        {"mode": "live", "telemetry_mode": "outbox", "api_key": "k", "quality": False},
    ]
    for index, case in enumerate(cases):
        _set_cloud_env(monkeypatch, **case)
        client = FakeClient()
        reporter = CloudSkillQualityReporter(
            client=client,
            mapping_store=FakeMappingStore(
                tmp_path,
                {"local-skill-1": SkillCloudBinding("local-skill-1", "cloud-skill-1")},
            ),
            outbox=CloudTelemetryOutbox(tmp_path / f"{index}-outbox.db"),
        )
        result = _run(reporter.maybe_report_analysis(_analysis()))
        assert result["status"] == "skipped"
        assert client.calls == []

    _set_cloud_env(monkeypatch, quality=True)
    client = FakeClient()
    reporter = CloudSkillQualityReporter(
        client=client,
        mapping_store=FakeMappingStore(
            tmp_path,
            {"local-skill-1": SkillCloudBinding("local-skill-1", "cloud-skill-1")},
        ),
        outbox=CloudTelemetryOutbox(tmp_path / "enabled-outbox.db"),
    )
    result = _run(reporter.maybe_report_analysis(_analysis()))
    assert result["status"] == "reported"
    assert len(client.calls) == 1
    assert client.calls[0][0] == "skill-use-reported"


def test_local_only_and_missing_cloud_binding_skipped(monkeypatch, tmp_path):
    _set_cloud_env(monkeypatch, quality=True)
    client = FakeClient()
    reporter = CloudSkillQualityReporter(
        client=client,
        mapping_store=FakeMappingStore(
            tmp_path,
            {
                "missing-cloud": SkillCloudBinding("missing-cloud", None),
                "bound": SkillCloudBinding("bound", "cloud-bound"),
            },
        ),
        outbox=CloudTelemetryOutbox(tmp_path / "outbox.db"),
    )
    result = _run(
        reporter.maybe_report_analysis(
            _analysis(
                judgments=[
                    SkillJudgment("unbound", True, "do not upload"),
                    SkillJudgment("missing-cloud", True, "do not upload"),
                    SkillJudgment("bound", True, "upload"),
                ]
            )
        )
    )
    assert result["reported_count"] == 1
    assert result["skipped_count"] == 2
    assert len(client.calls) == 1
    payload = client.calls[0][1]
    assert payload["local_skill_id"] == "bound"
    assert payload["cloud_skill_id"] == "cloud-bound"


def test_request_id_exact_and_independent_of_mutable_fields():
    judgment = SkillJudgment("local-skill-1", True, "free text ignored")
    payload = build_skill_quality_judgment_payload(
        _analysis(judgments=[judgment]),
        judgment,
        cloud_skill_id="cloud-skill-1",
        session_id="session-a",
    )
    assert payload["request_id"] == short_cloud_request_id(
        "skill-quality-judgment",
        "task-1",
        "local-skill-1",
        "cloud-skill-1",
    )

    failed_payload = build_skill_quality_judgment_payload(
        _analysis(
            timestamp=datetime(2026, 1, 3, 0, 0, 0),
            task_completed=False,
            judgments=[SkillJudgment("local-skill-1", False, "ignored")],
        ),
        SkillJudgment("local-skill-1", False, "ignored"),
        cloud_skill_id="cloud-skill-1",
        session_id="session-b",
    )
    assert failed_payload["request_id"] == payload["request_id"]


def test_payload_fields_stability_status_and_privacy():
    judgment = SkillJudgment("local-skill-1", True, "RAW_SKILL_NOTE prompt diff")
    analysis = _analysis(judgments=[judgment])
    payload = build_skill_quality_judgment_payload(
        analysis,
        judgment,
        cloud_skill_id="cloud-skill-1",
        session_id="session-a",
    )
    assert payload == build_skill_quality_judgment_payload(
        analysis,
        judgment,
        cloud_skill_id="cloud-skill-1",
        session_id="session-a",
    )
    assert payload["occurred_at"] == analysis.timestamp.isoformat()
    assert "duration_ms" not in payload
    assert payload["status"] == "success"
    assert "failure_reason" not in payload
    assert payload["quality_event_kind"] == QUALITY_EVENT_KIND
    assert payload["quality_schema_version"] == QUALITY_SCHEMA_VERSION
    assert payload["denominator"] == QUALITY_DENOMINATOR
    assert payload["skill_applied"] is True
    assert payload["task_completed"] is True
    assert payload["skill_phase_failed"] is False
    assert payload["completed"] is True
    assert payload["fallback"] is False
    assert "extras" not in payload

    failed = build_skill_quality_judgment_payload(
        _analysis(
            task_completed=False,
            judgments=[SkillJudgment("local-skill-1", True, "partial_success ignored")],
        ),
        SkillJudgment("local-skill-1", True, "partial_success ignored"),
        cloud_skill_id="cloud-skill-1",
    )
    assert failed["status"] == "failed"
    assert failed["failure_reason"] == "unknown"
    assert failed["completed"] is False
    assert failed["fallback"] is True
    assert failed["status"] != "partial_success"

    encoded = json.dumps({**payload, **failed}, sort_keys=True)
    for forbidden in (
        "RAW_SKILL_NOTE",
        "RAW_EXECUTION_NOTE",
        "RAW_TOOL_ISSUE",
        "prompt",
        "transcript",
        "file.diff",
        "traceback",
        "token",
        "redacted_preview",
    ):
        assert forbidden not in encoded


def test_phase_failed_success_candidate_reports_failed_unknown():
    judgment = SkillJudgment("local-skill-1", True, "ignored")
    payload = build_skill_quality_judgment_payload(
        _analysis(judgments=[judgment], phase_failed_ids=["local-skill-1"]),
        judgment,
        cloud_skill_id="cloud-skill-1",
    )
    assert payload["status"] == "failed"
    assert payload["failure_reason"] == "unknown"
    assert payload["skill_phase_failed"] is True
    assert payload["completed"] is False
    assert payload["fallback"] is True


def test_outbox_redaction_preserves_quality_fields(monkeypatch, tmp_path):
    _set_cloud_env(monkeypatch, quality=True)
    judgment = SkillJudgment("local-skill-1", False, "RAW note")
    payload = build_skill_quality_judgment_payload(
        _analysis(task_completed=False, judgments=[judgment]),
        judgment,
        cloud_skill_id="cloud-skill-1",
    )
    redacted = redact_telemetry_payload(payload)
    for key in (
        "quality_event_kind",
        "quality_schema_version",
        "denominator",
        "skill_applied",
        "task_completed",
        "skill_phase_failed",
        "completed",
        "fallback",
    ):
        assert key in redacted

    outbox = CloudTelemetryOutbox(tmp_path / "outbox.db")
    row = outbox.enqueue(
        endpoint="/api/v2/telemetry/skill-use-reported",
        payload=payload,
    )
    assert row.payload_redacted["quality_event_kind"] == QUALITY_EVENT_KIND
    assert row.payload_redacted["denominator"] == QUALITY_DENOMINATOR
    assert row.payload_redacted["fallback"] is True


def test_repeated_report_uses_same_outbox_row_for_same_payload(monkeypatch, tmp_path):
    _set_cloud_env(monkeypatch, quality=True)
    client = FakeClient(fail=True)
    outbox = CloudTelemetryOutbox(tmp_path / "outbox.db")
    reporter = CloudSkillQualityReporter(
        client=client,
        mapping_store=FakeMappingStore(
            tmp_path,
            {"local-skill-1": SkillCloudBinding("local-skill-1", "cloud-skill-1")},
        ),
        outbox=outbox,
    )
    analysis = _analysis()
    first = _run(reporter.maybe_report_analysis(analysis, session_id="session-a"))
    second = _run(reporter.maybe_report_analysis(analysis, session_id="session-a"))
    assert first["queued_count"] == 1
    assert second["queued_count"] == 1
    failed_rows = outbox.list_by_status("failed")
    assert len(failed_rows) == 1

    third = _run(reporter.maybe_report_analysis(analysis, session_id="session-b"))
    assert third["queued_count"] == 1
    failed_rows = outbox.list_by_status("failed")
    assert len(failed_rows) == 2
    assert {row.request_id for row in failed_rows} == {
        short_cloud_request_id(
            "skill-quality-judgment",
            "task-1",
            "local-skill-1",
            "cloud-skill-1",
        )
    }
