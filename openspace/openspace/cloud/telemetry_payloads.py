"""Schema-aligned helpers for cloud telemetry payloads."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

MAX_CLOUD_REQUEST_ID_LENGTH = 128

_TASK_STATUSES = {"success", "partial_success", "failed", "cancelled"}
_SKILL_USE_STATUSES = {"success", "partial_success", "failed"}
_EVOLVE_STATUSES = {"ok", "partial", "failed", "cancelled"}
_REDACTION_LEVELS = {
    "raw",
    "hash_only",
    "redacted",
    "truncated_redacted",
    "abstract_only",
}
_REDACTION_PERFORMERS = {"none", "client", "server", "offline_pipeline"}


def short_cloud_request_id(kind: str, *parts: Any) -> str:
    """Return a deterministic OpenSpace request_id that fits the cloud limit."""

    clean_kind = _compact_token(kind, fallback="request")
    encoded = json.dumps([clean_kind, *parts], ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    request_id = f"openspace:{clean_kind}:{digest}"
    if len(request_id) <= MAX_CLOUD_REQUEST_ID_LENGTH:
        return request_id
    return f"openspace:req:{digest}"


def build_task_report_payload(
    *,
    request_id: str,
    occurred_at: str,
    status: str,
    task_id: str,
    trajectory_artifact_status: str,
    session_id: str | None = None,
    trajectory_detail_level: str = "redacted_detail",
    trajectory_artifact_ref: str | None = None,
    trajectory_artifact_format: str | None = None,
    extras: Mapping[str, Any] | None = None,
    redaction_level: str = "redacted",
    redaction_performed_by: str = "client",
    redaction_policy_version: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Build a payload accepted by POST /api/v2/telemetry/task-reported."""

    payload: dict[str, Any] = {
        "request_id": _bounded_request_id(request_id),
        "occurred_at": occurred_at,
        "status": normalize_task_status(status),
        "task_id": str(task_id),
        "trajectory_artifact_status": trajectory_artifact_status,
        "trajectory_detail_level": trajectory_detail_level,
        "redaction_level": _enum_value(redaction_level, _REDACTION_LEVELS, "redacted"),
        "redaction_performed_by": _enum_value(
            redaction_performed_by, _REDACTION_PERFORMERS, "client"
        ),
    }
    _put_optional(payload, "session_id", session_id)
    _put_optional(payload, "trajectory_artifact_ref", trajectory_artifact_ref)
    _put_optional(payload, "trajectory_artifact_format", trajectory_artifact_format)
    _put_optional(payload, "redaction_policy_version", redaction_policy_version)
    _put_optional(payload, "error_code", error_code)
    if extras:
        payload["extras"] = dict(extras)
    return payload


def build_skill_use_report_payload(
    *,
    request_id: str,
    occurred_at: str,
    status: str,
    task_id: str,
    cloud_skill_id: str,
    session_id: str | None = None,
    local_skill_id: str | None = None,
    duration_ms: int | None = None,
    failure_reason: str | None = None,
    error_code: str | None = None,
    extras: Mapping[str, Any] | None = None,
    redaction_level: str = "abstract_only",
    redaction_performed_by: str = "client",
    redaction_policy_version: str | None = None,
    quality_event_kind: str | None = None,
    quality_schema_version: str | None = None,
    denominator: str | None = None,
    skill_applied: bool | None = None,
    task_completed: bool | None = None,
    skill_phase_failed: bool | None = None,
    completed: bool | None = None,
    fallback: bool | None = None,
) -> dict[str, Any]:
    """Build a payload accepted by POST /api/v2/telemetry/skill-use-reported."""

    payload: dict[str, Any] = {
        "request_id": _bounded_request_id(request_id),
        "occurred_at": occurred_at,
        "status": _enum_value(status, _SKILL_USE_STATUSES, "failed"),
        "task_id": str(task_id),
        "cloud_skill_id": str(cloud_skill_id),
        "redaction_level": _enum_value(redaction_level, _REDACTION_LEVELS, "abstract_only"),
        "redaction_performed_by": _enum_value(
            redaction_performed_by, _REDACTION_PERFORMERS, "client"
        ),
    }
    _put_optional(payload, "session_id", session_id)
    _put_optional(payload, "local_skill_id", local_skill_id)
    _put_optional(payload, "duration_ms", duration_ms)
    _put_optional(payload, "failure_reason", failure_reason)
    _put_optional(payload, "error_code", error_code)
    _put_optional(payload, "redaction_policy_version", redaction_policy_version)
    _put_optional(payload, "quality_event_kind", quality_event_kind)
    _put_optional(payload, "quality_schema_version", quality_schema_version)
    _put_optional(payload, "denominator", denominator)
    _put_optional(payload, "skill_applied", skill_applied)
    _put_optional(payload, "task_completed", task_completed)
    _put_optional(payload, "skill_phase_failed", skill_phase_failed)
    _put_optional(payload, "completed", completed)
    _put_optional(payload, "fallback", fallback)
    if extras:
        payload["extras"] = dict(extras)
    return payload


def build_evolve_report_payload(
    *,
    request_id: str,
    occurred_at: str,
    status: str,
    evolve_event_type: str,
    task_id: str | None = None,
    session_id: str | None = None,
    extras: Mapping[str, Any] | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Build a payload accepted by POST /api/v2/telemetry/evolve-reported."""

    payload: dict[str, Any] = {
        "request_id": _bounded_request_id(request_id),
        "occurred_at": occurred_at,
        "status": normalize_evolve_status(status),
        "evolve_event_type": str(evolve_event_type),
        "redaction_level": "abstract_only",
    }
    _put_optional(payload, "task_id", task_id)
    _put_optional(payload, "session_id", session_id)
    _put_optional(payload, "error_code", error_code)
    if extras:
        payload["extras"] = dict(extras)
    return payload


def build_usage_report_payload(
    *,
    request_id: str,
    cloud_skill_id: str,
    occurred_at: str,
    usage_count: int = 1,
    status: str = "ok",
    audience: str | None = None,
    bytes_served: int | None = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Build a payload accepted by POST /api/v2/telemetry/usage-reported."""

    payload: dict[str, Any] = {
        "request_id": _bounded_request_id(request_id),
        "cloud_skill_id": str(cloud_skill_id),
        "occurred_at": occurred_at,
        "usage_count": max(int(usage_count), 1),
        "status": normalize_usage_status(status),
    }
    _put_optional(payload, "audience", audience)
    _put_optional(payload, "bytes_served", bytes_served)
    _put_optional(payload, "duration_ms", duration_ms)
    _put_optional(payload, "error_code", error_code)
    return payload


def normalize_task_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"ok", "completed", "complete", "succeeded"}:
        return "success"
    if normalized in {"partial", "partial_success"}:
        return "partial_success"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized in _TASK_STATUSES:
        return normalized
    return "failed"


def normalize_evolve_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"success", "succeeded", "completed", "complete"}:
        return "ok"
    if normalized in {"partial_success", "partial"}:
        return "partial"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized in _EVOLVE_STATUSES:
        return normalized
    return "failed"


def normalize_usage_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if not normalized:
        return "ok"
    if normalized in {"ok", "success", "succeeded", "completed", "complete"}:
        return "ok"
    return "error"


def _bounded_request_id(value: str) -> str:
    text = str(value or "").strip()
    if text and len(text) <= MAX_CLOUD_REQUEST_ID_LENGTH:
        return text
    return short_cloud_request_id("request", text)


def _compact_token(value: str, *, fallback: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(value))
    token = "-".join(part for part in token.split("-") if part)
    return (token or fallback)[:48]


def _enum_value(value: str, allowed: set[str], fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else fallback


def _put_optional(payload: dict[str, Any], key: str, value: Any) -> None:
    if value is not None and value != "":
        payload[key] = value
