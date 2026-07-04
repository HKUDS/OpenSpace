import asyncio
import json
from datetime import datetime

import openspace.cloud.skill_quality_reporter as reporter_module
from openspace.cloud.config import (
    OPENSPACE_CLOUD_API_KEY_ENV,
    OPENSPACE_CLOUD_BASE_URL_ENV,
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
        OPENSPACE_CLOUD_BASE_URL_ENV,
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
            "RAW_EXECUTION_NOTE prompt messages transcript /tmp/private/file.diff "
            "raw_error token sk-private authorization Bearer secret redacted_preview sha256"
        ),
        tool_issues=[
            "RAW_TOOL_ISSUE traceback /home/user/project/file.py API_KEY=secret"
        ],
        skill_judgments=judgments
        or [
            SkillJudgment(
                skill_id="local-skill-1",
                skill_applied=True,
                note="RAW_SKILL_NOTE prompt diff token authorization",
            )
        ],
        skill_phase_failed_skill_ids=phase_failed_ids or [],
    )


QUALITY_FIELDS = {
    "quality_event_kind",
    "quality_schema_version",
    "denominator",
    "skill_applied",
    "task_completed",
    "skill_phase_failed",
    "completed",
    "fallback",
}


def _walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def _assert_private_analysis_text_absent(payload):
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).lower()
    for forbidden in (
        "raw_skill_note",
        "raw_execution_note",
        "raw_tool_issue",
        "prompt",
        "messages",
        "transcript",
        "file.diff",
        "/tmp/",
        "/home/",
        "traceback",
        "raw_error",
        "sk-private",
        "api_key",
        "bearer ",
        "authorization",
        "redacted_preview",
        "sha256",
    ):
        assert forbidden not in encoded
    forbidden_keys = {
        "note",
        "execution_note",
        "tool_issues",
        "prompt",
        "messages",
        "transcript",
        "path",
        "diff",
        "raw_error",
        "raw_diagnostic",
        "redacted_preview",
        "sha256",
        "api_key",
        "authorization",
        "token",
    }
    assert forbidden_keys.isdisjoint({key.lower() for key in _walk_keys(payload)})


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


def test_disabled_quality_gate_skips_before_invalid_cloud_config(monkeypatch, tmp_path):
    _patch_host_env(monkeypatch)
    for key in (
        OPENSPACE_CLOUD_MODE_ENV,
        OPENSPACE_CLOUD_BASE_URL_ENV,
        OPENSPACE_CLOUD_TELEMETRY_MODE_ENV,
        OPENSPACE_CLOUD_API_KEY_ENV,
        OPENSPACE_CLOUD_SKILL_QUALITY_REPORTING_ENV,
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(OPENSPACE_CLOUD_MODE_ENV, "invalid-mode")
    monkeypatch.setenv(OPENSPACE_CLOUD_BASE_URL_ENV, "not-a-service-root")
    monkeypatch.setenv(OPENSPACE_CLOUD_TELEMETRY_MODE_ENV, "invalid-telemetry")
    monkeypatch.setenv(OPENSPACE_CLOUD_API_KEY_ENV, "test-key")

    def fail_client_construction(*args, **kwargs):
        raise AssertionError("OpenSpaceClient should not be constructed")

    def fail_config_load(*args, **kwargs):
        raise AssertionError("load_cloud_config should not be called")

    def fail_outbox_construction(*args, **kwargs):
        raise AssertionError("CloudTelemetryOutbox should not be constructed")

    def fail_executor_construction(*args, **kwargs):
        raise AssertionError("ThreadPoolExecutor should not be constructed")

    def fail_sync_body(*args, **kwargs):
        raise AssertionError("_maybe_report_analysis_sync should not be called")

    monkeypatch.setattr(reporter_module, "load_cloud_config", fail_config_load)
    monkeypatch.setattr(reporter_module, "OpenSpaceClient", fail_client_construction)
    monkeypatch.setattr(reporter_module, "CloudTelemetryOutbox", fail_outbox_construction)
    monkeypatch.setattr(reporter_module, "ThreadPoolExecutor", fail_executor_construction)
    reporter = CloudSkillQualityReporter(workspace_root=tmp_path)
    monkeypatch.setattr(reporter, "_maybe_report_analysis_sync", fail_sync_body)

    result = _run(reporter.maybe_report_analysis(_analysis()))

    assert result == {
        "status": "skipped",
        "reason": "skill_quality_reporting_disabled",
    }
    assert not list(tmp_path.rglob("*.db"))


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

    variants = [
        build_skill_quality_judgment_payload(
            _analysis(
                timestamp=datetime(2026, 1, 3, 0, 0, 0),
                judgments=[judgment],
            ),
            judgment,
            cloud_skill_id="cloud-skill-1",
            session_id="session-b",
        ),
        build_skill_quality_judgment_payload(
            _analysis(
                task_completed=False,
                judgments=[SkillJudgment("local-skill-1", False, "ignored")],
            ),
            SkillJudgment("local-skill-1", False, "ignored"),
            cloud_skill_id="cloud-skill-1",
            session_id="session-c",
        ),
        build_skill_quality_judgment_payload(
            _analysis(
                judgments=[judgment],
                phase_failed_ids=["local-skill-1"],
            ),
            judgment,
            cloud_skill_id="cloud-skill-1",
        ),
    ]
    assert variants[0]["occurred_at"] != payload["occurred_at"]
    assert variants[1]["status"] == "failed"
    assert variants[1]["failure_reason"] == "unknown"
    assert variants[2]["skill_phase_failed"] is True
    for variant in variants:
        assert variant["request_id"] == payload["request_id"]
        assert "duration_ms" not in variant


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
    assert set(payload) == {
        "request_id",
        "occurred_at",
        "status",
        "task_id",
        "cloud_skill_id",
        "redaction_level",
        "redaction_performed_by",
        "session_id",
        "local_skill_id",
        "redaction_policy_version",
        *QUALITY_FIELDS,
    }
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
    assert failed["failure_reason"] != "not_applicable"
    assert QUALITY_FIELDS.issubset(failed)
    assert not (QUALITY_FIELDS & set((failed.get("extras") or {}).keys()))

    _assert_private_analysis_text_absent(payload)
    _assert_private_analysis_text_absent(failed)


def test_payload_outbox_hash_stable_for_same_persisted_analysis(tmp_path):
    persisted = ExecutionAnalysis.from_dict(_analysis().to_dict())
    judgment = persisted.skill_judgments[0]
    payload = build_skill_quality_judgment_payload(
        persisted,
        judgment,
        cloud_skill_id="cloud-skill-1",
        session_id="session-a",
    )
    rebuilt = build_skill_quality_judgment_payload(
        persisted,
        judgment,
        cloud_skill_id="cloud-skill-1",
        session_id="session-a",
    )
    assert payload == rebuilt

    outbox = CloudTelemetryOutbox(tmp_path / "outbox.db")
    first = outbox.enqueue(
        endpoint="/api/v2/telemetry/skill-use-reported",
        payload=payload,
    )
    second = outbox.enqueue(
        endpoint="/api/v2/telemetry/skill-use-reported",
        payload=rebuilt,
    )

    assert first.request_id == second.request_id == payload["request_id"]
    assert first.payload_hash == second.payload_hash
    assert first.payload_redacted == second.payload_redacted
    assert len(outbox.list_pending()) == 1
    assert first.payload_redacted["occurred_at"] == persisted.timestamp.isoformat()
    assert "duration_ms" not in first.payload_redacted
    assert QUALITY_FIELDS.issubset(first.payload_redacted)
    assert not (QUALITY_FIELDS & set((first.payload_redacted.get("extras") or {}).keys()))
    _assert_private_analysis_text_absent(first.payload_redacted)


def test_occurred_at_redaction_bypass_requires_timestamp_shape(tmp_path):
    occurred_at = "2026-01-02T03:04:05.123456"
    raw_nested = (
        'call +1 415 555 0101; File "/tmp/private/raw_trace.py", line 9, '
        "in handler; token sk-private-token"
    )
    redacted = redact_telemetry_payload(
        {
            "request_id": "openspace:test:occurred-at",
            "occurred_at": occurred_at,
            "status": "success",
            "task_id": "task-1",
            "cloud_skill_id": "cloud-skill-1",
            "extras": {"occurred_at": raw_nested},
        },
        workspace_root=tmp_path,
    )

    assert redacted["occurred_at"] == occurred_at
    nested = redacted["extras"]["occurred_at"]
    assert nested != raw_nested
    assert "[REDACTED_PHONE]" in nested
    assert "<redacted>" in nested
    assert "path_hash:" in nested
    for forbidden in (
        "+1 415 555 0101",
        "/tmp/private",
        "raw_trace.py",
        "sk-private-token",
    ):
        assert forbidden not in nested


def test_status_mapping_ignores_free_text_partial_and_not_applicable_signals():
    success_judgment = SkillJudgment(
        "local-skill-1",
        True,
        "partial_success failed not_applicable raw note ignored",
    )
    success = build_skill_quality_judgment_payload(
        _analysis(
            task_completed=True,
            judgments=[success_judgment],
        ),
        success_judgment,
        cloud_skill_id="cloud-skill-1",
    )
    assert success["status"] == "success"
    assert "failure_reason" not in success

    failed_judgment = SkillJudgment(
        "local-skill-1",
        True,
        "partial_success success not_applicable ignored",
    )
    failed = build_skill_quality_judgment_payload(
        _analysis(
            task_completed=False,
            judgments=[failed_judgment],
        ),
        failed_judgment,
        cloud_skill_id="cloud-skill-1",
    )
    assert failed["status"] == "failed"
    assert failed["failure_reason"] == "unknown"
    assert {success["status"], failed["status"]} <= {"success", "failed"}


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
    _assert_private_analysis_text_absent(row.payload_redacted)


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
    assert len({row.payload_hash for row in failed_rows}) == 2


def test_multi_skill_failed_trajectory_reports_one_payload_per_cloud_bound_judgment(
    monkeypatch,
    tmp_path,
):
    _set_cloud_env(monkeypatch, quality=True)
    client = FakeClient()
    reporter = CloudSkillQualityReporter(
        client=client,
        mapping_store=FakeMappingStore(
            tmp_path,
            {
                "local-skill-1": SkillCloudBinding("local-skill-1", "cloud-skill-1"),
                "local-skill-2": SkillCloudBinding("local-skill-2", "cloud-skill-2"),
                "local-only": SkillCloudBinding("local-only", None),
            },
        ),
        outbox=CloudTelemetryOutbox(tmp_path / "outbox.db"),
    )
    result = _run(
        reporter.maybe_report_analysis(
            _analysis(
                task_completed=False,
                judgments=[
                    SkillJudgment("local-skill-1", True, "ignored success-ish note"),
                    SkillJudgment("local-skill-2", False, "ignored partial note"),
                    SkillJudgment("local-only", True, "ignored local note"),
                ],
                phase_failed_ids=["local-skill-2"],
            )
        )
    )

    assert result["status"] == "reported"
    assert result["reported_count"] == 2
    assert result["skipped_count"] == 1
    payloads = [payload for event, payload in client.calls if event == "skill-use-reported"]
    assert {payload["local_skill_id"] for payload in payloads} == {
        "local-skill-1",
        "local-skill-2",
    }
    for payload in payloads:
        assert payload["status"] == "failed"
        assert payload["failure_reason"] == "unknown"
        assert payload["task_completed"] is False
        assert payload["completed"] is False
        assert payload["fallback"] is True
        assert payload["denominator"] == QUALITY_DENOMINATOR
        _assert_private_analysis_text_absent(payload)
    by_local = {payload["local_skill_id"]: payload for payload in payloads}
    assert by_local["local-skill-1"]["skill_applied"] is True
    assert by_local["local-skill-1"]["skill_phase_failed"] is False
    assert by_local["local-skill-2"]["skill_applied"] is False
    assert by_local["local-skill-2"]["skill_phase_failed"] is True
