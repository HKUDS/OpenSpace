"""Cloud credential and telemetry redaction helpers.

``redact_cloud_payload`` is intentionally conservative for auth/status output:
it removes credentials but preserves ordinary paths and status booleans.

Telemetry and task-trace upload use the stricter helpers below.  Those helpers
are schema-aware, remove local machine paths, summarize blocked raw fields, and
fail closed when high-confidence secrets remain.
"""

from __future__ import annotations

import re
from typing import Any
import json
import hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


REDACTED = "<redacted>"
REDACTED_PATH = "[REDACTED_PATH]"
REDACTED_FIELD = "[REDACTED_FIELD]"
REDACTION_POLICY_VERSION = "openspace-v2-redaction-1"

_SECRET_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "password",
    "authorization",
    "x_api_key",
    "x-api-key",
    "x_admin_key",
    "x-admin-key",
}

_NON_SECRET_STATUS_KEYS = {
    "api_key_preview",
    "api_key_saved",
    "has_access_token",
    "has_api_key",
}

_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9._-]{8,}")
_JSON_SECRET_RE = re.compile(
    r'("(?:(?:x[-_])?api[-_]?key|access[-_]?token|password|authorization|x[-_]?admin[-_]?key)"\s*:\s*)"[^"]*"',
    re.IGNORECASE,
)
_KV_SECRET_RE = re.compile(
    r"\b((?:(?:x[-_])?api[-_]?key|access[-_]?token|password|authorization|x[-_]?admin[-_]?key)\s*=\s*)[^\s,;&]+",
    re.IGNORECASE,
)
_COOKIE_RE = re.compile(r"\bCookie\s*:\s*[^\r\n]+", re.IGNORECASE)
_ENV_ASSIGN_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|COOKIE))=([^\s;&]+)"
)
_ABS_POSIX_PATH_RE = re.compile(r"(?<![\w:])/(?:Users|home|private|tmp|var|Volumes)/[^\s\"'<>]+")
_ABS_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s\"'<>]+")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d .()-]{7,}\d)(?!\d)")
_TRACEBACK_RE = re.compile(r'File "([^"]+)", line (\d+), in ([^\n]+)')
_ISO_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?"
)

_TELEMETRY_ALLOWED_KEYS = {
    "task_id",
    "session_id",
    "request_id",
    "occurred_at",
    "step_id",
    "step_index",
    "attempt",
    "action_type",
    "action_name",
    "status",
    "duration_ms",
    "error_code",
    "failure_reason",
    "quality_event_kind",
    "quality_schema_version",
    "denominator",
    "skill_applied",
    "task_completed",
    "skill_phase_failed",
    "completed",
    "fallback",
    "package_id",
    "package_path",
    "cloud_skill_id",
    "local_skill_id",
    "parent_cloud_skill_ids",
    "parent_local_skill_ids",
    "origin",
    "generation",
    "result_count",
    "content_hash",
    "manifest_hash",
    "projection_hash",
    "redacted_summary",
    "trajectory_detail_level",
    "trajectory_artifact_status",
    "trajectory_artifact_ref",
    "trajectory_artifact_format",
    "extras",
    "redaction_level",
    "redaction_performed_by",
    "redaction_policy_version",
    "usage_count",
    "audience",
    "bytes_served",
    "evolve_event_type",
}
_TELEMETRY_BLOCKED_KEYS = {
    "prompt",
    "messages",
    "messages_input",
    "messages_output",
    "transcript",
    "raw_args",
    "raw_input",
    "raw_output",
    "stdout",
    "stderr",
    "tool_output",
    "tool_result",
    "content",
    "file_content",
    "env",
    "environment",
    "traceback",
    "stack",
    "exception",
}


def _normalized_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def is_secret_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    if normalized in _NON_SECRET_STATUS_KEYS:
        return False
    return (
        normalized in _SECRET_KEYS
        or "api_key" in normalized
        or "apikey" in normalized
        or "password" in normalized
        or normalized.endswith("access_token")
    )


def redact_cloud_secret(value: Any) -> Any:
    """Redact credential-looking substrings from a scalar value."""

    if not isinstance(value, str):
        return value
    parsed = _try_parse_json(value)
    if parsed is not None:
        return json.dumps(redact_cloud_payload(parsed), ensure_ascii=False, sort_keys=True)
    text = _BEARER_RE.sub(f"Bearer {REDACTED}", value)
    text = _API_KEY_RE.sub(REDACTED, text)
    text = _JSON_SECRET_RE.sub(lambda match: f'{match.group(1)}"{REDACTED}"', text)
    text = _KV_SECRET_RE.sub(rf"\1{REDACTED}", text)
    return text


def redact_cloud_payload(value: Any) -> Any:
    """Recursively redact cloud credentials from common JSON-like values."""

    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            redacted[key] = REDACTED if is_secret_key(key) and item else redact_cloud_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_cloud_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_cloud_payload(item) for item in value)
    return redact_cloud_secret(value)


def redact_upload_text(value: Any, *, workspace_root: str | Path | None = None) -> str:
    """Redact a free-text value for cloud telemetry/task-trace upload."""

    text = redact_cloud_secret("" if value is None else str(value))
    text = _COOKIE_RE.sub(f"Cookie: {REDACTED}", text)
    text = _ENV_ASSIGN_RE.sub(lambda match: f"{match.group(1)}={REDACTED}", text)
    text = _URL_RE.sub(_redact_url, text)
    text = _TRACEBACK_RE.sub(
        lambda match: f'File "{sanitize_upload_path(match.group(1), workspace_root=workspace_root)}", '
        f"line {match.group(2)}, in {match.group(3)}",
        text,
    )
    text = _ABS_WINDOWS_PATH_RE.sub(
        lambda match: sanitize_upload_path(match.group(0), workspace_root=workspace_root),
        text,
    )
    text = _ABS_POSIX_PATH_RE.sub(
        lambda match: sanitize_upload_path(match.group(0), workspace_root=workspace_root),
        text,
    )
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
    return text


def sanitize_upload_path(
    value: Any,
    *,
    workspace_root: str | Path | None = None,
) -> str:
    """Return a cloud-safe path reference.

    Workspace-relative paths are kept as ``$WORKSPACE/...``.  Other absolute
    paths are replaced with a stable hash so telemetry can deduplicate without
    leaking a local username or filesystem layout.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    if not _looks_absolute_path(text):
        return normalized
    if workspace_root:
        try:
            path = Path(text).expanduser().resolve()
            root = Path(workspace_root).expanduser().resolve()
            rel = path.relative_to(root)
            return "$WORKSPACE/" + rel.as_posix()
        except Exception:
            pass
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"path_hash:{digest}"


def redact_telemetry_payload(
    payload: dict[str, Any],
    *,
    workspace_root: str | Path | None = None,
    allow_extra_keys: bool = False,
) -> dict[str, Any]:
    """Return a Step-4 compliant telemetry payload.

    Unknown top-level keys are dropped by default.  Raw fields such as
    ``stdout``/``prompt``/``messages`` are never retained; a hash and short
    redacted summary are emitted instead.
    """

    if not isinstance(payload, dict):
        raise TypeError("telemetry payload must be a JSON object")
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        normalized_key = str(key)
        lowered = normalized_key.lower()
        if is_secret_key(normalized_key):
            continue
        if lowered in _TELEMETRY_BLOCKED_KEYS:
            redacted[f"{normalized_key}_redacted"] = _blocked_field_summary(
                value,
                workspace_root=workspace_root,
            )
            continue
        if normalized_key not in _TELEMETRY_ALLOWED_KEYS and not allow_extra_keys:
            continue
        redacted[normalized_key] = _redact_upload_value(
            value,
            workspace_root=workspace_root,
            key=normalized_key,
        )
    redacted.setdefault("redaction_level", "redacted")
    redacted.setdefault("redaction_performed_by", "client")
    redacted.setdefault("redaction_policy_version", REDACTION_POLICY_VERSION)
    return redacted


def redact_task_trace_value(
    value: Any,
    *,
    workspace_root: str | Path | None = None,
) -> Any:
    """Recursively redact arbitrary task-trace JSON-compatible data."""

    return _redact_upload_value(value, workspace_root=workspace_root)


def validate_upload_redaction(value: Any) -> list[str]:
    """Return high-confidence redaction failures that should block upload."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    failures: list[str] = []
    if _API_KEY_RE.search(encoded):
        failures.append("api_key")
    if _BEARER_RE.search(encoded):
        failures.append("bearer_token")
    if _COOKIE_RE.search(encoded):
        failures.append("cookie_header")
    if _secret_field_findings(value):
        failures.append("secret_field")
    for match in _KV_SECRET_RE.finditer(encoded):
        if not match.group(0).endswith(REDACTED):
            failures.append("secret_field")
    return sorted(set(failures))


def redaction_report_for_payload(value: Any) -> dict[str, Any]:
    failures = validate_upload_redaction(value)
    counts = _redaction_observability_counts(value)
    return {
        "policy_version": REDACTION_POLICY_VERSION,
        "redaction_level": "complete_redacted" if not failures else "redacted",
        "secret_redaction_count": counts["secret_redaction_count"],
        "path_redaction_count": counts["path_redaction_count"],
        "pii_redaction_count": counts["pii_redaction_count"],
        "blocked_fields_count": counts["blocked_fields_count"],
        "upload_allowed": not failures,
        "deny_findings": failures,
    }


def secret_preview(value: str | None) -> str:
    """Return a non-sensitive preview for status output."""

    if not value:
        return ""
    return REDACTED


def _try_parse_json(value: str) -> Any | None:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _redact_upload_value(
    value: Any,
    *,
    workspace_root: str | Path | None = None,
    key: str = "",
) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for item_key, item_value in value.items():
            item_key_text = str(item_key)
            if is_secret_key(item_key_text):
                redacted[item_key_text] = REDACTED
            elif item_key_text.lower() in _TELEMETRY_BLOCKED_KEYS:
                redacted[f"{item_key_text}_redacted"] = _blocked_field_summary(
                    item_value,
                    workspace_root=workspace_root,
                )
            else:
                redacted[item_key_text] = _redact_upload_value(
                    item_value,
                    workspace_root=workspace_root,
                    key=item_key_text,
                )
        return redacted
    if isinstance(value, list):
        return [
            _redact_upload_value(item, workspace_root=workspace_root, key=key)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _redact_upload_value(item, workspace_root=workspace_root, key=key)
            for item in value
        )
    if isinstance(value, str):
        if key == "occurred_at" and _is_iso_timestamp_like(value):
            return value
        if key.endswith("_path") or key in {"local_path", "workspace_ref", "path"}:
            return sanitize_upload_path(value, workspace_root=workspace_root)
        return redact_upload_text(value, workspace_root=workspace_root)
    return redact_cloud_payload(value)


def _is_iso_timestamp_like(value: str) -> bool:
    if not _ISO_TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _secret_field_findings(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if is_secret_key(key) and item not in ("", None, REDACTED):
                return True
            if _secret_field_findings(item):
                return True
        return False
    if isinstance(value, list):
        return any(_secret_field_findings(item) for item in value)
    if isinstance(value, tuple):
        return any(_secret_field_findings(item) for item in value)
    return False


def _redaction_observability_counts(value: Any) -> dict[str, int]:
    counts = {
        "secret_redaction_count": 0,
        "path_redaction_count": 0,
        "pii_redaction_count": 0,
        "blocked_fields_count": 0,
    }
    _count_redaction_markers(value, counts)
    return counts


def _count_redaction_markers(value: Any, counts: dict[str, int]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if key_text.endswith("_redacted") and _is_blocked_field_summary(item):
                counts["blocked_fields_count"] += 1
            if is_secret_key(key_text) and item == REDACTED:
                counts["secret_redaction_count"] += 1
                continue
            _count_redaction_markers(item, counts)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _count_redaction_markers(item, counts)
        return
    if not isinstance(value, str):
        return
    counts["secret_redaction_count"] += value.count(REDACTED)
    counts["path_redaction_count"] += value.count("$WORKSPACE/")
    counts["path_redaction_count"] += value.count("path_hash:")
    counts["path_redaction_count"] += value.count(REDACTED_PATH)
    counts["pii_redaction_count"] += value.count("[REDACTED_EMAIL]")
    counts["pii_redaction_count"] += value.count("[REDACTED_PHONE]")


def _is_blocked_field_summary(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("redaction") == REDACTED_FIELD
        and isinstance(value.get("sha256"), str)
    )


def _blocked_field_summary(
    value: Any,
    *,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    raw = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    redacted = redact_upload_text(raw, workspace_root=workspace_root)
    return {
        "redaction": REDACTED_FIELD,
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "redacted_preview": redacted[:500],
    }


def _looks_absolute_path(value: str) -> bool:
    return value.startswith("/") or bool(re.match(r"^[A-Za-z]:[\\/]", value))


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    try:
        parts = urlsplit(raw)
        path_hash = hashlib.sha256(parts.path.encode("utf-8")).hexdigest()[:16]
        safe_path = f"/path_hash:{path_hash}" if parts.path else ""
        return urlunsplit((parts.scheme, parts.netloc, safe_path, "", ""))
    except Exception:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"url_hash:{digest}"
