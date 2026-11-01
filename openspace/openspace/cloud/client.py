"""OpenSpace cloud platform HTTP client.

All methods are **synchronous** (use ``urllib``).  In async contexts
(MCP server), wrap calls with ``asyncio.to_thread()``.

Provides both low-level HTTP operations and higher-level workflows:
  - v2 packages/skills: search, pull, bundle download, upload, telemetry
"""

from __future__ import annotations

import difflib
import io
import json
import logging
import os
import shutil
import time
import tempfile
import uuid
import urllib.parse
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from openspace.cloud.base import cloud_api_url
from openspace.cloud.config import CloudConfig, CloudConfigError, require_cloud_agent_key
from openspace.cloud.local_mapping import (
    CLOUD_SKILL_INFO_FILENAME,
    UPLOAD_META_FILENAME,
    CloudLocalMappingStore,
    CloudSkillBinding,
    compute_local_content_hash,
    ensure_local_skill_id,
    generate_local_skill_id,
    utc_now_iso,
    write_cloud_skill_info,
    write_local_skill_id,
)
from openspace.cloud.redaction import redact_cloud_secret, validate_upload_redaction
from openspace.cloud.redaction import REDACTION_POLICY_VERSION
from openspace.cloud.transport import (
    CloudRequest,
    CloudResponse,
    CloudTransport,
    UrllibCloudTransport,
)

logger = logging.getLogger("openspace.cloud")

SKILL_FILENAME = "SKILL.md"
SKILL_ID_FILENAME = ".skill_id"
V2_RECALL_SEARCH_MAX_LIMIT = 50

_TEXT_EXTENSIONS = frozenset({
    ".md", ".txt", ".yaml", ".yml", ".json", ".py", ".sh", ".toml",
    ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".xml", ".csv",
    ".ini", ".cfg", ".rst",
})


class CloudError(Exception):
    """Raised when a cloud API call fails."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        body: str = "",
        *,
        code: str | None = None,
        kind: str | None = None,
        retryable: bool | None = None,
        field_errors: Any = None,
        suggested_action: str | None = None,
        request_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ):
        redacted_message = redact_cloud_secret(message)
        super().__init__(redacted_message)
        self.message = redacted_message
        self.status_code = status_code
        self.body = redact_cloud_secret(body)
        self.code = code
        self.kind = kind or _classify_cloud_error_kind(status_code)
        self.retryable = bool(retryable) if retryable is not None else status_code >= 500
        self.field_errors = field_errors
        self.suggested_action = suggested_action
        self.request_id = request_id
        self.details = dict(details or {})

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "status": "error",
            "message": self.message,
        }
        if self.code:
            payload["code"] = self.code
        if self.kind:
            payload["kind"] = self.kind
        if self.status_code:
            payload["status_code"] = self.status_code
        if self.retryable:
            payload["retryable"] = True
        if self.field_errors:
            payload["field_errors"] = self.field_errors
        if self.suggested_action:
            payload["suggested_action"] = self.suggested_action
        if self.request_id:
            payload["request_id"] = self.request_id
        if self.details:
            payload["details"] = self.details
        return payload


def _classify_cloud_error_kind(status_code: int) -> str:
    if status_code in (401, 403):
        return "auth"
    if status_code == 409:
        return "conflict"
    if status_code == 422:
        return "validation"
    if status_code >= 500:
        return "server"
    if status_code == 400:
        return "bad_request"
    if status_code:
        return "http"
    return "client"


def _structured_error_from_response(response: CloudResponse) -> CloudError:
    body = response.body.decode("utf-8", errors="replace")
    payload: Any = None
    if body:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None
    parsed = payload if isinstance(payload, dict) else {}
    error_obj = parsed.get("error")
    if isinstance(error_obj, dict):
        parsed = {**parsed, **error_obj}
    code = _first_text(
        parsed.get("code"),
        parsed.get("error_code"),
        parsed.get("error"),
        parsed.get("type"),
    )
    message = _cloud_error_message(response.status_code, parsed, body)
    retryable = parsed.get("retryable")
    if not isinstance(retryable, bool):
        retryable = response.status_code >= 500 or code == "PACKAGE_PROJECTION_NOT_READY"
    field_errors = parsed.get("field_errors")
    if field_errors is None and response.status_code == 422:
        field_errors = parsed.get("errors") or parsed.get("detail")
    return CloudError(
        message,
        status_code=response.status_code,
        body=body,
        code=code,
        kind=_classify_cloud_error_kind(response.status_code),
        retryable=retryable,
        field_errors=field_errors,
        suggested_action=_first_text(parsed.get("suggested_action"), parsed.get("action")),
        request_id=_first_text(
            parsed.get("request_id"),
            parsed.get("trace_id"),
            response.headers.get("X-Request-ID"),
            response.headers.get("x-request-id"),
        ),
        details={
            key: value
            for key, value in parsed.items()
            if key
            not in {
                "code",
                "error_code",
                "error",
                "type",
                "message",
                "detail",
                "errors",
                "field_errors",
                "retryable",
                "suggested_action",
                "action",
                "request_id",
                "trace_id",
            }
        },
    )


def _cloud_error_message(status_code: int, payload: Mapping[str, Any], body: str) -> str:
    explicit = _first_text(payload.get("message"), payload.get("detail"))
    code = _first_text(payload.get("code"), payload.get("error_code"), payload.get("error"))
    if explicit:
        return f"HTTP {status_code} {code}: {explicit}" if code else f"HTTP {status_code}: {explicit}"
    if code:
        return f"HTTP {status_code}: {code}"
    return f"HTTP {status_code}: {body[:500]}"


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return redact_cloud_secret(value.strip())
    return None


def is_package_projection_not_ready(error: BaseException) -> bool:
    return isinstance(error, CloudError) and error.code == "PACKAGE_PROJECTION_NOT_READY"


def _cloud_config_error_to_cloud_error(error: CloudConfigError) -> CloudError:
    message = str(error)
    if "OPENSPACE_CLOUD_API_KEY" in message:
        return CloudError(
            message,
            code="CLOUD_API_KEY_REQUIRED",
            kind="auth",
            retryable=False,
            suggested_action="call cloud_auth_flow(action='bootstrap_agent_key') or set OPENSPACE_CLOUD_API_KEY",
            details={
                "required_env": "OPENSPACE_CLOUD_API_KEY",
                "registration_tool": "cloud_auth_flow",
            },
        )
    if "OPENSPACE_CLOUD_MODE" in message:
        return CloudError(
            message,
            code="CLOUD_DISABLED",
            kind="auth",
            retryable=False,
            suggested_action="set OPENSPACE_CLOUD_MODE=live before using cloud tools",
            details={"required_env": "OPENSPACE_CLOUD_MODE"},
        )
    return CloudError(
        message,
        code="CLOUD_CONFIG_INVALID",
        kind="validation",
        retryable=False,
        suggested_action="fix OPENSPACE_CLOUD_* configuration before using cloud tools",
    )


class OpenSpaceClient:
    """HTTP client for the OpenSpace cloud API.

    Args:
        config: Strict cloud runtime config.
        transport: Optional HTTP transport for tests.
    """

    _DEFAULT_UA = "OpenSpace-Client/1.0"

    def __init__(
        self,
        config: CloudConfig,
        transport: CloudTransport | None = None,
        mapping_store: CloudLocalMappingStore | None = None,
    ):
        try:
            config = require_cloud_agent_key(config)
        except CloudConfigError as exc:
            raise _cloud_config_error_to_cloud_error(exc) from exc
        self._config = config
        self._transport = transport or UrllibCloudTransport()
        self._mapping_store = mapping_store
        self._headers = {
            "User-Agent": self._DEFAULT_UA,
            "X-API-Key": config.api_key,
        }

    def _local_mapping_store(self) -> CloudLocalMappingStore:
        if self._mapping_store is None:
            self._mapping_store = CloudLocalMappingStore()
        return self._mapping_store

    def _request_api(
        self,
        version: str,
        method: str,
        path: str,
        *,
        body: Optional[bytes] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: int = 30,
    ) -> tuple[int, bytes]:
        """Execute a versioned API request. Returns ``(status_code, body)``."""
        headers = {**self._headers}
        if extra_headers:
            headers.update(extra_headers)
        request = CloudRequest(
            method=method,
            url=cloud_api_url(self._config.base_url, version, path),
            headers=headers,
            body=body,
            timeout=timeout,
        )
        response = self._transport.send(request)
        self._raise_for_status(response)
        return response.status_code, response.body

    @staticmethod
    def _raise_for_status(response: CloudResponse) -> None:
        if 200 <= response.status_code < 300:
            return
        raise _structured_error_from_response(response)

    def _get_v2_json(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        query = self._encode_query(params)
        full_path = f"{path}?{query}" if query else path
        _, data = self._request_api("v2", "GET", full_path, timeout=timeout)
        return json.loads(data.decode("utf-8"))

    def _post_v2_json(
        self,
        path: str,
        payload: Dict[str, Any],
        *,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        _, data = self._request_api(
            "v2",
            "POST",
            path,
            body=body,
            extra_headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        return json.loads(data.decode("utf-8"))

    @staticmethod
    def _encode_query(params: Optional[Dict[str, Any]]) -> str:
        if not params:
            return ""
        clean: Dict[str, Any] = {
            key: value
            for key, value in params.items()
            if value is not None and value != ""
        }
        return urllib.parse.urlencode(clean)

    @staticmethod
    def _quote_id(value: str) -> str:
        return urllib.parse.quote(str(value), safe="")

    def smoke(self) -> Dict[str, Any]:
        """GET /api/v2/smoke — validate the active v2 credential."""
        return self._get_v2_json("/smoke", timeout=15)

    def get_package_domain_index(self) -> Dict[str, Any]:
        """GET /api/v2/packages/domain-index."""
        return self._get_v2_json("/packages/domain-index", timeout=30)

    def get_package_subtree_for_upload(
        self,
        sub_domain_package_id: str,
        *,
        snapshot_version: str | None = None,
    ) -> Dict[str, Any]:
        """GET /api/v2/packages/{sub_domain_package_id}/subtree-for-upload."""
        return self._get_v2_json(
            f"/packages/{self._quote_id(sub_domain_package_id)}/subtree-for-upload",
            params={"snapshot_version": snapshot_version},
            timeout=30,
        )

    def get_packages_root(self) -> Dict[str, Any]:
        """GET /api/v2/packages/root."""
        return self._get_v2_json("/packages/root", timeout=30)

    def get_package_children(
        self,
        package_id: str,
        *,
        audience: str = "requester_visible",
        child_packages_cursor: str | None = None,
        child_packages_limit: int = 50,
        skills_cursor: str | None = None,
        skills_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /api/v2/packages/{package_id}/children."""
        return self._get_v2_json(
            f"/packages/{self._quote_id(package_id)}/children",
            params={
                "audience": audience,
                "child_packages_cursor": child_packages_cursor,
                "child_packages_limit": child_packages_limit,
                "skills_cursor": skills_cursor,
                "skills_limit": skills_limit,
            },
            timeout=30,
        )

    def recall_packages(
        self,
        *,
        query: str = "",
        audience: str = "requester_visible",
        limit: int = 10,
    ) -> Dict[str, Any]:
        """GET /api/v2/recall — read-only package recall without search_id."""
        return self._get_v2_json(
            "/recall",
            params={
                "query": query,
                "audience": audience,
                "limit": min(max(limit, 1), V2_RECALL_SEARCH_MAX_LIMIT),
            },
            timeout=30,
        )

    def search_packages(
        self,
        *,
        query: str,
        audience: str = "requester_visible",
        limit: int = 10,
        request_id: str | None = None,
        task_id: str | None = None,
    ) -> Dict[str, Any]:
        """POST /api/v2/recall/search — telemetry-covered package search."""
        payload: Dict[str, Any] = {
            "request_id": request_id or f"recall-{uuid.uuid4().hex}",
            "query": query,
            "audience": audience,
            "limit": min(max(limit, 1), V2_RECALL_SEARCH_MAX_LIMIT),
        }
        if task_id:
            payload["task_id"] = task_id
        return self._post_v2_json("/recall/search", payload, timeout=30)

    def pull_package(
        self,
        package_id: str,
        *,
        audience: str = "requester_visible",
    ) -> Dict[str, Any]:
        """GET /api/v2/packages/{package_id}/pull."""
        result = self._get_v2_json(
            f"/packages/{self._quote_id(package_id)}/pull",
            params={"audience": audience},
            timeout=30,
        )
        self._cache_package_pull(result)
        return result

    def pull_packages(
        self,
        *,
        package_ids: List[str],
        search_id: str,
        request_id: str | None = None,
        audience: str = "requester_visible",
    ) -> Dict[str, Any]:
        """POST /api/v2/packages/pull — telemetry-covered package selection."""
        if not package_ids:
            raise CloudError("package_ids must not be empty")
        payload = {
            "request_id": request_id or f"package-pull-{uuid.uuid4().hex}",
            "search_id": search_id,
            "package_ids": package_ids[:50],
            "audience": audience,
        }
        result = self._post_v2_json("/packages/pull", payload, timeout=60)
        for pull in result.get("pulls") or []:
            if isinstance(pull, dict):
                self._cache_package_pull(pull)
        return result

    def search_skills(
        self,
        *,
        query: str,
        package_id: str | None = None,
        audience: str = "requester_visible",
        limit: int = 10,
        artifact_filter: str = "all",
        request_id: str | None = None,
    ) -> Dict[str, Any]:
        """POST /api/v2/skills/search — skill-first lexical search."""
        payload: Dict[str, Any] = {
            "request_id": request_id or f"skill-search-{uuid.uuid4().hex}",
            "query": query,
            "audience": audience,
            "limit": min(max(limit, 1), V2_RECALL_SEARCH_MAX_LIMIT),
            "artifact_filter": artifact_filter,
        }
        if package_id:
            payload["package_id"] = package_id
        return self._post_v2_json("/skills/search", payload, timeout=30)

    def fetch_cloud_skill(self, cloud_skill_id: str) -> Dict[str, Any]:
        """GET /api/v2/skills/{cloud_skill_id}."""
        return self._get_v2_json(
            f"/skills/{self._quote_id(cloud_skill_id)}",
            timeout=30,
        )

    def download_skill_bundle(
        self,
        cloud_skill_id: str,
        *,
        audience: str = "requester_visible",
    ) -> bytes:
        """GET /api/v2/skills/{cloud_skill_id}/bundle."""
        query = self._encode_query({"audience": audience})
        path = f"/skills/{self._quote_id(cloud_skill_id)}/bundle"
        if query:
            path = f"{path}?{query}"
        _, data = self._request_api("v2", "GET", path, timeout=120)
        return data

    def download_package_bundle(
        self,
        package_id: str,
        *,
        audience: str = "requester_visible",
    ) -> bytes:
        """GET /api/v2/packages/{package_id}/bundle."""
        query = self._encode_query({"audience": audience})
        path = f"/packages/{self._quote_id(package_id)}/bundle"
        if query:
            path = f"{path}?{query}"
        _, data = self._request_api("v2", "GET", path, timeout=180)
        return data

    def pull_package_with_projection_status(
        self,
        package_id: str,
        *,
        audience: str = "requester_visible",
        attempts: int = 1,
        backoff_seconds: float = 0.0,
    ) -> Dict[str, Any]:
        """Pull a package, returning pending status while projection catches up."""

        last_error: CloudError | None = None
        total_attempts = max(int(attempts), 1)
        for attempt in range(1, total_attempts + 1):
            try:
                return {
                    "status": "ready",
                    "package_id": package_id,
                    "attempts": attempt,
                    "package": self.pull_package(package_id, audience=audience),
                }
            except CloudError as exc:
                if not is_package_projection_not_ready(exc):
                    raise
                last_error = exc
                if attempt < total_attempts and backoff_seconds > 0:
                    time.sleep(backoff_seconds * attempt)
        return self._package_projection_pending_payload(
            package_id,
            attempts=total_attempts,
            error=last_error,
        )

    def download_package_bundle_with_projection_status(
        self,
        package_id: str,
        *,
        audience: str = "requester_visible",
        attempts: int = 1,
        backoff_seconds: float = 0.0,
    ) -> Dict[str, Any]:
        """Download a package bundle, returning pending status if projection is not ready."""

        last_error: CloudError | None = None
        total_attempts = max(int(attempts), 1)
        for attempt in range(1, total_attempts + 1):
            try:
                return {
                    "status": "ready",
                    "package_id": package_id,
                    "attempts": attempt,
                    "bundle": self.download_package_bundle(package_id, audience=audience),
                }
            except CloudError as exc:
                if not is_package_projection_not_ready(exc):
                    raise
                last_error = exc
                if attempt < total_attempts and backoff_seconds > 0:
                    time.sleep(backoff_seconds * attempt)
        return self._package_projection_pending_payload(
            package_id,
            attempts=total_attempts,
            error=last_error,
        )

    @staticmethod
    def _package_projection_pending_payload(
        package_id: str,
        *,
        attempts: int,
        error: CloudError | None,
    ) -> Dict[str, Any]:
        return {
            "status": "pending",
            "reason": "package_projection_not_ready",
            "package_id": package_id,
            "retryable": True,
            "attempts": attempts,
            "suggested_action": "retry_package_projection_or_use_skill_detail_bundle",
            "error": error.to_payload() if error else {},
        }

    def report_telemetry(self, event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST one of the v2 telemetry report endpoints.

        ``event`` is the endpoint stem, for example ``"task-reported"`` or
        ``"skill-use-reported"``.
        """
        allowed = {
            "action-step-reported",
            "task-reported",
            "skill-use-reported",
            "evolve-reported",
            "usage-reported",
        }
        if event not in allowed:
            raise CloudError(f"Unsupported telemetry event: {event}")
        return self._post_v2_json(f"/telemetry/{event}", payload, timeout=30)

    def upload_task_trace_artifact(
        self,
        archive_path: str | Path,
        *,
        request_id: str,
        task_id: str,
        session_id: str,
        manifest_json: Mapping[str, Any],
        artifact_sha256: str,
        size_bytes: int,
        collection_scope: str,
        collection_reason: str,
        cloud_skill_ids: list[str] | None = None,
        package_ids: list[str] | None = None,
        redaction_level: str = "complete_redacted",
        redaction_policy_version: str = REDACTION_POLICY_VERSION,
        compression: str = "zip",
        schema_version: str = "2.0",
    ) -> Dict[str, Any]:
        """POST /api/v2/telemetry/task-trace-artifacts."""

        archive = Path(archive_path)
        if not archive.exists() or not archive.is_file():
            raise CloudError(f"task trace archive not found: {archive}")
        fields: dict[str, Any] = {
            "request_id": request_id,
            "task_id": task_id,
            "session_id": session_id,
            "artifact_format": "openspace_task_trace_v2",
            "schema_version": schema_version,
            "collection_scope": collection_scope,
            "collection_reason": collection_reason,
            "cloud_skill_ids": json.dumps(cloud_skill_ids or [], sort_keys=True),
            "package_ids": json.dumps(package_ids or [], sort_keys=True),
            "redaction_level": redaction_level,
            "redaction_policy_version": redaction_policy_version,
            "compression": compression,
            "artifact_sha256": artifact_sha256,
            "size_bytes": int(size_bytes),
            "manifest_json": json.dumps(manifest_json, ensure_ascii=False, sort_keys=True),
        }
        boundary, body = self._multipart_file_upload_body(
            fields,
            file_field="archive",
            file_path=archive,
            content_type="application/zip" if compression == "zip" else "application/octet-stream",
        )
        _, resp_data = self._request_api(
            "v2",
            "POST",
            "/telemetry/task-trace-artifacts",
            body=body,
            extra_headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=300,
        )
        return json.loads(resp_data.decode("utf-8"))

    def search_cloud_skills(
        self,
        *,
        query: str,
        limit: int = 20,
        audience: str = "requester_visible",
        task_id: str | None = None,
    ) -> List[Dict[str, Any]]:
        """Search v2 cloud skills and return concrete skill rows.

        This is intentionally skill-first only. Package recall/projection remains
        available through explicit package-browsing APIs, but it is not used as
        an implicit cloud skill search fallback.
        """
        skill_limit = min(max(limit, 1), V2_RECALL_SEARCH_MAX_LIMIT)

        try:
            skill_search = self.search_skills(
                query=query,
                audience=audience,
                limit=skill_limit,
                artifact_filter="downloadable_only",
            )
            return self._skill_first_search_rows(skill_search, skill_limit)
        except CloudError as exc:
            if exc.status_code in (401, 403):
                raise
            logger.info(
                "search_cloud_skills: skill-first search failed without package fallback: %s",
                exc,
            )
            return []

    def _skill_first_search_rows(
        self,
        payload: Dict[str, Any],
        limit: int,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        seen: set[str] = set()
        root_package_id = str(payload.get("root_package_id") or "")
        root_package_path = str(payload.get("root_package_path") or "")
        skill_search_id = str(payload.get("skill_search_id") or "")
        requested_mode = str(payload.get("requested_mode") or "")
        served_mode = str(payload.get("served_mode") or "")
        semantic_status = str(payload.get("semantic_status") or "")
        fallback_reason = str(payload.get("fallback_reason") or "")

        for index, candidate in enumerate(payload.get("results") or []):
            if not isinstance(candidate, dict):
                continue
            skill_id = str(candidate.get("cloud_skill_id") or "")
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)

            try:
                detail = self.fetch_cloud_skill(skill_id)
            except CloudError:
                detail = {}
            metadata = detail.get("authored_metadata")
            if not isinstance(metadata, dict):
                metadata = {}

            name = (
                detail.get("title")
                or metadata.get("name")
                or candidate.get("title")
                or candidate.get("skill_name")
                or skill_id
            )
            description = (
                detail.get("summary")
                or metadata.get("description")
                or candidate.get("summary")
                or candidate.get("snippet")
                or ""
            )
            effective_visibility = (
                detail.get("effective_visibility")
                or candidate.get("effective_visibility")
                or candidate.get("access_mode")
                or "public"
            )
            package_id = (
                detail.get("package_id")
                or candidate.get("package_id")
                or root_package_id
            )
            package_path = (
                detail.get("package_path")
                or candidate.get("package_path")
                or root_package_path
            )
            raw_score = candidate.get("score")
            raw_rank = candidate.get("rank")
            if isinstance(raw_score, (int, float)):
                search_rank = float(raw_score)
            elif isinstance(raw_rank, int) and raw_rank > 0:
                search_rank = 1.0 / raw_rank
            else:
                search_rank = 1.0 / (index + 1)

            rows.append({
                "cloud_skill_id": skill_id,
                "name": name,
                "description": description,
                "visibility": str(effective_visibility),
                "effective_visibility": effective_visibility,
                "package_id": package_id,
                "package_path": package_path,
                "tags": metadata.get("tags") or [],
                "origin": metadata.get("origin_type") or metadata.get("origin") or "",
                "created_by": metadata.get("created_by") or "",
                "search_rank": search_rank,
                "source_api": "v2/skills/search",
                "snippet": candidate.get("snippet") or "",
                "skill_search_id": skill_search_id,
                "match_mode": candidate.get("match_mode", ""),
                "served_mode": candidate.get("served_mode", served_mode),
                "requested_mode": requested_mode,
                "semantic_status": candidate.get("semantic_status", semantic_status),
                "fallback_reason": candidate.get("fallback_reason") or fallback_reason,
                "artifact_state": detail.get("artifact_state") or candidate.get("artifact_state", ""),
                "downloadable": detail.get("downloadable", candidate.get("downloadable")),
                "metadata_only": detail.get("metadata_only", candidate.get("metadata_only")),
            })
            if len(rows) >= limit:
                break
        return rows

    def upload_skill_v2(
        self,
        skill_dir: Path,
        *,
        local_skill_store_db_path: str | Path | None = None,
        visibility: str = "private",
        origin: str = "imported",
        parent_cloud_skill_ids: Optional[List[str]] = None,
        requested_package_id: str | None = None,
        requested_parent_package_id: str | None = None,
        requested_new_package_segment: str | None = None,
        snapshot_version_used: str | None = None,
        owner_agent_id: str | None = None,
        submitted_skill_id: str | None = None,
        content_diff: str | None = None,
    ) -> Dict[str, Any]:
        """Upload a local skill through POST /api/v2/skills/upload.

        v2 combines artifact upload and package placement in one multipart
        request. Package creation happens by sending
        ``requested_parent_package_id`` plus ``requested_new_package_segment``.
        Local trust is verified before any cloud request and is not included in
        the multipart payload.
        """
        from openspace.skill_engine.skill_utils import parse_frontmatter
        from openspace.cloud.upload_trust import (
            require_trusted_skill_for_upload_db,
            resolve_upload_skill_store_db,
        )

        skill_path = Path(skill_dir)
        trust_db_path = resolve_upload_skill_store_db(
            skill_path,
            explicit_db_path=local_skill_store_db_path,
        )
        require_trusted_skill_for_upload_db(
            skill_path,
            db_path=trust_db_path,
        )
        skill_file = skill_path / SKILL_FILENAME
        if not skill_file.exists():
            raise CloudError(f"SKILL.md not found in {skill_dir}")

        content = skill_file.read_text(encoding="utf-8")
        fm = parse_frontmatter(content)
        name = fm.get("name", skill_path.name)
        description = fm.get("description", "")
        if not name:
            raise CloudError("SKILL.md frontmatter missing 'name' field")

        origin_type = self._normalize_v2_origin(origin)
        parents = parent_cloud_skill_ids or []
        self._validate_v2_origin_parents(origin_type, parents)
        local_skill_id = submitted_skill_id or ensure_local_skill_id(skill_path, skill_name=str(name))
        if visibility not in ("public", "private"):
            raise CloudError("visibility must be 'public' or 'private'")
        if requested_package_id and (requested_parent_package_id or requested_new_package_segment):
            raise CloudError(
                "Use either requested_package_id or requested_parent_package_id "
                "+ requested_new_package_segment, not both"
            )
        if bool(requested_parent_package_id) != bool(requested_new_package_segment):
            raise CloudError(
                "requested_parent_package_id and requested_new_package_segment "
                "must be provided together"
            )
        if origin_type != "fix" and not requested_package_id and not requested_parent_package_id:
            raise CloudError(
                "v2 non-fix uploads require package placement: provide "
                "requested_package_id or requested_parent_package_id plus "
                "requested_new_package_segment"
            )

        file_paths = self._collect_files(skill_path)
        if not file_paths:
            raise CloudError("No files found in skill directory")
        self._validate_skill_upload_redaction(skill_path, file_paths)

        if content_diff is None:
            content_diff = self._compute_v2_content_diff(
                skill_path,
                visibility,
                parents,
            )

        form_fields: Dict[str, Any] = {
            "submitted_skill_id": local_skill_id,
            "origin_type": origin_type,
            "requested_visibility": visibility,
            "owner_agent_id": owner_agent_id,
            "requested_package_id": requested_package_id,
            "requested_parent_package_id": requested_parent_package_id,
            "requested_new_package_segment": requested_new_package_segment,
            "snapshot_version_used": snapshot_version_used,
            "requested_parent_cloud_skill_ids": parents or None,
            "content_diff": content_diff,
        }
        boundary, body = self._multipart_skill_upload_body(skill_path, form_fields, file_paths)
        _, resp_data = self._request_api(
            "v2",
            "POST",
            "/skills/upload",
            body=body,
            extra_headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=180,
        )
        result = json.loads(resp_data.decode("utf-8"))
        cloud_skill_id = result.get("cloud_skill_id")
        if cloud_skill_id:
            now = utc_now_iso()
            binding = CloudSkillBinding(
                local_skill_id=local_skill_id,
                cloud_skill_id=str(cloud_skill_id),
                local_path=str(skill_path),
                current_package_id=result.get("package_id") or requested_package_id,
                current_package_path=result.get("package_path"),
                source_cloud_skill_id=parents[0] if len(parents) == 1 else None,
                manifest_hash=result.get("manifest_hash"),
                local_content_hash=compute_local_content_hash(skill_path),
                sync_state="uploaded",
                last_pushed_at=now,
            )
            self._local_mapping_store().upsert_binding(binding)
            write_cloud_skill_info(skill_path, binding)
        return {
            "status": "success",
            "api_version": "v2",
            "name": name,
            "description": description,
            "file_count": len(file_paths),
            **result,
        }

    def import_skill(
        self,
        cloud_skill_id: str,
        target_dir: Path,
        *,
        audience: str = "requester_visible",
        local_category: str | None = None,
        local_category_path: str | None = None,
    ) -> Dict[str, Any]:
        """Download a v2 cloud skill and extract it locally."""
        return self.import_cloud_skill(
            cloud_skill_id,
            target_dir,
            audience=audience,
            local_category=local_category,
            local_category_path=local_category_path,
        )

    def import_cloud_skill(
        self,
        cloud_skill_id: str,
        target_dir: Path,
        *,
        audience: str = "requester_visible",
        local_category: str | None = None,
        local_category_path: str | None = None,
    ) -> Dict[str, Any]:
        """Download a v2 cloud skill bundle and extract it locally."""
        logger.info(f"import_cloud_skill: fetching metadata for {cloud_skill_id}")
        store = self._local_mapping_store()
        skill_data = self.fetch_cloud_skill(cloud_skill_id)
        metadata = skill_data.get("authored_metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        skill_name = (
            skill_data.get("title")
            or metadata.get("name")
            or cloud_skill_id
        )
        skill_name = self._safe_skill_dir_name(str(skill_name), cloud_skill_id)
        target_root = target_dir.resolve()
        target_root.mkdir(parents=True, exist_ok=True)
        existing_binding = store.get_binding_by_cloud(cloud_skill_id)
        if existing_binding:
            existing_dir = Path(existing_binding.local_path)
            if existing_dir.exists() and (existing_dir / SKILL_FILENAME).exists():
                classification = self._classify_imported_skill(
                    store,
                    existing_dir,
                    local_skill_id=existing_binding.local_skill_id,
                    cloud_package_path=(
                        skill_data.get("package_path")
                        or existing_binding.current_package_path
                        or existing_binding.package_path_at_pull
                    ),
                    local_category=local_category,
                    local_category_path=local_category_path,
                    origin="imported",
                )
                materialized_dir = self._materialize_classified_skill(
                    existing_dir,
                    classification,
                    skills_root=existing_dir.parent,
                )
                if materialized_dir != existing_dir:
                    existing_binding = replace(
                        existing_binding,
                        local_path=str(materialized_dir),
                        local_content_hash=compute_local_content_hash(materialized_dir),
                    )
                    store.upsert_binding(existing_binding)
                    write_cloud_skill_info(materialized_dir, existing_binding)
                    existing_dir = materialized_dir
                return {
                    "status": "already_exists",
                    "api_version": "v2",
                    "skill_id": existing_binding.local_skill_id,
                    "local_skill_id": existing_binding.local_skill_id,
                    "cloud_skill_id": cloud_skill_id,
                    "name": skill_name,
                    "description": skill_data.get("summary") or metadata.get("description", ""),
                    "package_id": skill_data.get("package_id") or existing_binding.current_package_id,
                    "package_path": skill_data.get("package_path") or existing_binding.current_package_path,
                    "cloud_package_path": skill_data.get("package_path") or existing_binding.current_package_path,
                    "local_path": str(existing_dir),
                    "local_category_path": (
                        classification.get("local_category_path")
                        if isinstance(classification, dict)
                        else None
                    ),
                    "classification": classification,
                }
            local_skill_id = existing_binding.local_skill_id
        else:
            local_skill_id = generate_local_skill_id(skill_name)

        skill_dir = self._target_dir_for_cloud_skill(
            target_root,
            skill_name,
            cloud_skill_id,
            (
                existing_binding.local_path
                if existing_binding
                and Path(existing_binding.local_path).exists()
                else ""
            ),
        )
        if not skill_dir.is_relative_to(target_root):
            raise CloudError(f"Skill name {skill_name!r} escapes target directory")

        if skill_dir.exists():
            raise CloudError(f"Target skill directory already exists: {skill_dir}")

        logger.info(f"import_cloud_skill: downloading bundle for {cloud_skill_id}")
        zip_data = self.download_skill_bundle(cloud_skill_id, audience=audience)

        with tempfile.TemporaryDirectory(
            prefix=".openspace-cloud-skill-",
            dir=str(target_root),
        ) as tmp:
            staging_dir = Path(tmp)
            self._extract_zip(zip_data, staging_dir)
            skill_root = self._find_extracted_skill_root(staging_dir)
            if skill_root is None:
                raise CloudError("Downloaded skill bundle does not contain SKILL.md")
            self._copy_extracted_skill_tree(skill_root, skill_dir)

        write_local_skill_id(skill_dir, local_skill_id)
        classification = self._classify_imported_skill(
            store,
            skill_dir,
            local_skill_id=local_skill_id,
            cloud_package_path=skill_data.get("package_path"),
            local_category=local_category,
            local_category_path=local_category_path,
            origin="imported",
        )
        skill_dir = self._materialize_classified_skill(
            skill_dir,
            classification,
            skills_root=target_root,
        )
        now = utc_now_iso()
        binding = CloudSkillBinding(
            local_skill_id=local_skill_id,
            cloud_skill_id=cloud_skill_id,
            local_path=str(skill_dir),
            package_id_at_pull=skill_data.get("package_id"),
            package_path_at_pull=skill_data.get("package_path"),
            package_snapshot_version_at_pull=skill_data.get("snapshot_version"),
            current_package_id=skill_data.get("package_id"),
            current_package_path=skill_data.get("package_path"),
            manifest_hash=skill_data.get("manifest_hash"),
            local_content_hash=compute_local_content_hash(skill_dir),
            sync_state="clean",
            last_pulled_at=now,
        )
        store.upsert_binding(binding)
        write_cloud_skill_info(skill_dir, binding)

        logger.info(
            f"import_cloud_skill: {skill_name} [{cloud_skill_id}] -> {skill_dir} "
            f"({len(self._collect_files(skill_dir))} files)"
        )

        return {
            "status": "success",
            "api_version": "v2",
            "skill_id": local_skill_id,
            "local_skill_id": local_skill_id,
            "cloud_skill_id": cloud_skill_id,
            "name": skill_name,
            "description": skill_data.get("summary") or metadata.get("description", ""),
            "package_id": skill_data.get("package_id"),
            "package_path": skill_data.get("package_path"),
            "cloud_package_path": skill_data.get("package_path"),
            "local_path": str(skill_dir),
            "local_category_path": (
                classification.get("local_category_path")
                if isinstance(classification, dict)
                else None
            ),
            "classification": classification,
            "files": [str(path.relative_to(skill_dir)) for path in self._collect_files(skill_dir)],
        }

    def import_package_bundle(
        self,
        package_id: str,
        target_dir: Path,
        *,
        audience: str = "requester_visible",
    ) -> Dict[str, Any]:
        """Download a v2 package subtree bundle and extract it locally."""
        logger.info(f"import_package_bundle: fetching package projection for {package_id}")
        pull_status = self.pull_package_with_projection_status(
            package_id,
            audience=audience,
            attempts=3,
            backoff_seconds=1.0,
        )
        if pull_status.get("status") == "pending":
            return {
                "status": "pending",
                "api_version": "v2",
                "package_id": package_id,
                "local_path": "",
                **pull_status,
            }
        package_data = pull_status["package"]
        package_path = str(package_data.get("root_package_path") or package_id)
        package_name = self._safe_skill_dir_name(package_path.rstrip("/").split("/")[-1], package_id)
        package_dir = (target_dir / package_name).resolve()
        if not package_dir.is_relative_to(target_dir.resolve()):
            raise CloudError(f"Package name {package_name!r} escapes target directory")

        logger.info(f"import_package_bundle: downloading bundle for {package_id}")
        bundle_status = self.download_package_bundle_with_projection_status(
            package_id,
            audience=audience,
            attempts=3,
            backoff_seconds=1.0,
        )
        if bundle_status.get("status") == "pending":
            return {
                "status": "pending",
                "api_version": "v2",
                "package_id": package_id,
                "package_path": package_path,
                "local_path": "",
                **bundle_status,
            }
        zip_data = bundle_status["bundle"]
        package_dir.mkdir(parents=True, exist_ok=True)
        extracted = self._extract_zip(zip_data, package_dir)
        imported_skills = self._bind_imported_package_skills(
            package_dir,
            package_id=package_id,
            package_path=package_path,
            package_data=package_data,
        )
        (package_dir / ".cloud_package.json").write_text(
            json.dumps(
                {
                    "api_version": "v2",
                    "package_id": package_id,
                    "package_path": package_path,
                    "audience": package_data.get("audience"),
                    "projection_hash": package_data.get("projection_hash"),
                    "skill_count": len(package_data.get("skills") or []),
                    "imported_skill_count": len(imported_skills),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "status": "success",
            "api_version": "v2",
            "package_id": package_id,
            "package_path": package_path,
            "local_path": str(package_dir),
            "imported_skills": imported_skills,
            "files": extracted,
        }

    def _classify_imported_skill(
        self,
        store: CloudLocalMappingStore,
        skill_dir: Path,
        *,
        local_skill_id: str,
        cloud_package_path: Any,
        local_category: str | None = None,
        local_category_path: str | None = None,
        origin: str,
    ) -> dict[str, Any]:
        try:
            from openspace.cloud.skill_classification import (
                classify_skill_dir,
                persist_skill_classification,
            )

            classification = classify_skill_dir(
                skill_dir,
                local_skill_id=local_skill_id,
                cloud_package_path=(
                    str(cloud_package_path) if cloud_package_path else None
                ),
                local_category=local_category,
                local_category_path=local_category_path,
                origin=origin,
            )
            return persist_skill_classification(store, classification).to_payload()
        except Exception as exc:
            logger.debug("cloud skill local classification skipped: %s", exc)
            return {}

    @staticmethod
    def _materialize_classified_skill(
        skill_dir: Path,
        classification: Mapping[str, Any],
        *,
        skills_root: Path,
    ) -> Path:
        if not classification.get("local_category_path"):
            return skill_dir
        try:
            from openspace.cloud.skill_classification import materialize_skill_category_tree

            return materialize_skill_category_tree(
                skill_dir,
                classification,
                skills_root=skills_root,
            )
        except Exception as exc:
            logger.debug("cloud skill local category tree materialization skipped: %s", exc)
            return skill_dir

    def _bind_imported_package_skills(
        self,
        package_dir: Path,
        *,
        package_id: str,
        package_path: str,
        package_data: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        skills_root = package_dir / "skills"
        if not skills_root.is_dir():
            return []
        store = self._local_mapping_store()
        now = utc_now_iso()
        imported: list[dict[str, Any]] = []
        detail_by_cloud_id = {
            str(item.get("cloud_skill_id") or ""): item
            for item in package_data.get("skills") or []
            if isinstance(item, dict)
        }
        for skill_root in sorted(skills_root.iterdir()):
            if not skill_root.is_dir() or not (skill_root / SKILL_FILENAME).exists():
                continue
            cloud_skill_id = skill_root.name
            detail = detail_by_cloud_id.get(cloud_skill_id, {})
            existing = store.get_binding_by_cloud(cloud_skill_id)
            local_skill_id = (
                existing.local_skill_id
                if existing is not None and existing.local_skill_id
                else generate_local_skill_id(skill_root.name)
            )
            write_local_skill_id(skill_root, local_skill_id)
            binding = CloudSkillBinding(
                local_skill_id=local_skill_id,
                cloud_skill_id=cloud_skill_id,
                local_path=str(skill_root),
                package_id_at_pull=str(detail.get("package_id") or package_id),
                package_path_at_pull=str(detail.get("package_path") or package_path),
                package_snapshot_version_at_pull=(
                    str(detail.get("snapshot_version"))
                    if detail.get("snapshot_version") is not None
                    else None
                ),
                current_package_id=str(detail.get("package_id") or package_id),
                current_package_path=str(detail.get("package_path") or package_path),
                manifest_hash=detail.get("manifest_hash"),
                local_content_hash=compute_local_content_hash(skill_root),
                sync_state="clean",
                last_pulled_at=now,
            )
            store.upsert_binding(binding)
            write_cloud_skill_info(skill_root, binding)
            classification = self._classify_imported_skill(
                store,
                skill_root,
                local_skill_id=local_skill_id,
                cloud_package_path=detail.get("package_path") or package_path,
                origin="imported",
            )
            imported.append({
                "local_skill_id": local_skill_id,
                "cloud_skill_id": cloud_skill_id,
                "package_id": binding.current_package_id,
                "package_path": binding.current_package_path,
                "local_path": str(skill_root),
                "classification": classification,
            })
        return imported

    @staticmethod
    def _safe_skill_dir_name(name: str, fallback: str) -> str:
        if "/" in name or "\\" in name or name.startswith(".") or not name.strip():
            return fallback
        return name.strip()

    @staticmethod
    def _target_dir_for_cloud_skill(
        target_root: Path,
        skill_name: str,
        cloud_skill_id: str,
        bound_local_path: str = "",
    ) -> Path:
        if bound_local_path:
            return Path(bound_local_path).resolve()
        preferred = (target_root / skill_name).resolve()
        if not preferred.exists():
            return preferred
        suffix = uuid.uuid5(uuid.NAMESPACE_URL, cloud_skill_id).hex[:8]
        return (target_root / f"{skill_name}__cloud_{suffix}").resolve()

    def _cache_package_pull(self, pull: Dict[str, Any]) -> None:
        package_id = str(pull.get("root_package_id") or pull.get("package_id") or "")
        if not package_id:
            return
        self._local_mapping_store().upsert_package_cache(
            package_id=package_id,
            package_path=str(pull.get("root_package_path") or pull.get("package_path") or ""),
            projection_hash=pull.get("projection_hash"),
            serving_epoch=(
                str(pull["serving_epoch"])
                if pull.get("serving_epoch") is not None
                else None
            ),
            source_epoch=(
                str(pull["source_epoch"])
                if pull.get("source_epoch") is not None
                else None
            ),
            last_pulled_at=utc_now_iso(),
        )

    @staticmethod
    def _normalize_v2_origin(origin: str) -> str:
        mapping = {
            "imported": "imported",
            "captured": "capture",
            "capture": "capture",
            "derived": "derive",
            "derive": "derive",
            "fixed": "fix",
            "fix": "fix",
        }
        normalized = mapping.get(origin)
        if not normalized:
            raise CloudError(
                "origin must be imported, captured/capture, derived/derive, or fixed/fix"
            )
        return normalized

    @staticmethod
    def _validate_v2_origin_parents(origin_type: str, parents: List[str]) -> None:
        if origin_type in ("imported", "capture") and parents:
            raise CloudError(f"origin_type='{origin_type}' must not have parent skill IDs")
        if origin_type == "derive" and not parents:
            raise CloudError("origin_type='derive' requires at least 1 parent cloud skill ID")
        if origin_type == "fix" and len(parents) != 1:
            raise CloudError("origin_type='fix' requires exactly 1 parent cloud skill ID")

    def _compute_v2_content_diff(
        self,
        skill_dir: Path,
        api_visibility: str,
        parents: List[str],
    ) -> Optional[str]:
        if api_visibility != "public":
            return None

        cur_files = self._collect_text_files(skill_dir)

        if len(parents) == 1:
            try:
                anc_zip = self.download_skill_bundle(parents[0])
                anc_files = self._extract_zip_text_files(anc_zip)
                diff = self._unified_diff(anc_files, cur_files)
                if diff:
                    logger.info(f"Computed v2 diff vs ancestor {parents[0]}")
                    return diff
            except Exception as e:
                logger.warning(f"v2 diff computation failed: {e}")
            return None

        if not parents:
            return self._unified_diff({}, cur_files)

        return None

    @classmethod
    def _multipart_skill_upload_body(
        cls,
        skill_dir: Path,
        form_fields: Dict[str, Any],
        file_paths: List[Path],
    ) -> tuple[str, bytes]:
        boundary = f"----OpenSpaceV2Upload{os.urandom(8).hex()}"
        parts: list[bytes] = []
        for name, value in form_fields.items():
            cls._append_form_field(parts, boundary, name, value)
        for path in file_paths:
            cls._append_file_field(parts, boundary, skill_dir, path)
        parts.append(f"--{boundary}--\r\n".encode())
        return boundary, b"".join(parts)

    @classmethod
    def _multipart_file_upload_body(
        cls,
        form_fields: Dict[str, Any],
        *,
        file_field: str,
        file_path: Path,
        content_type: str = "application/octet-stream",
    ) -> tuple[str, bytes]:
        boundary = f"----OpenSpaceV2Upload{os.urandom(8).hex()}"
        parts: list[bytes] = []
        for name, value in form_fields.items():
            cls._append_form_field(parts, boundary, name, value)
        safe_filename = file_path.name.replace('"', "%22")
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{safe_filename}"\r\n'.encode()
        )
        parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        parts.append(file_path.read_bytes())
        parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return boundary, b"".join(parts)

    @staticmethod
    def _append_form_field(
        parts: list[bytes],
        boundary: str,
        name: str,
        value: Any,
    ) -> None:
        if value is None:
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                OpenSpaceClient._append_form_field(parts, boundary, name, item)
            return
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")

    @staticmethod
    def _append_file_field(
        parts: list[bytes],
        boundary: str,
        skill_dir: Path,
        path: Path,
    ) -> None:
        rel_path = str(path.relative_to(skill_dir)).replace("\\", "/")
        safe_filename = rel_path.replace('"', "%22")
        ctype = "text/plain" if path.suffix in _TEXT_EXTENSIONS else "application/octet-stream"
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="files"; '
            f'filename="{safe_filename}"\r\n'.encode()
        )
        parts.append(f"Content-Type: {ctype}\r\n\r\n".encode())
        parts.append(path.read_bytes())
        parts.append(b"\r\n")

    @staticmethod
    def _collect_files(skill_dir: Path) -> List[Path]:
        """Collect all files in skill directory (skip .skill_id sidecar)."""
        return [
            p for p in sorted(skill_dir.rglob("*"))
            if p.is_file()
            and p.name not in {SKILL_ID_FILENAME, CLOUD_SKILL_INFO_FILENAME, UPLOAD_META_FILENAME}
        ]

    @staticmethod
    def _collect_text_files(skill_dir: Path) -> Dict[str, str]:
        """Collect text files as ``{relative_path: content}``."""
        files: Dict[str, str] = {}
        for p in sorted(skill_dir.rglob("*")):
            if p.is_file() and p.name not in {
                SKILL_ID_FILENAME,
                CLOUD_SKILL_INFO_FILENAME,
                UPLOAD_META_FILENAME,
            }:
                rel = str(p.relative_to(skill_dir))
                try:
                    files[rel] = p.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    pass
        return files

    @staticmethod
    def _validate_skill_upload_redaction(skill_dir: Path, file_paths: List[Path]) -> None:
        """Fail closed when a direct skill upload still contains secrets."""

        from openspace.skill_engine.evidence.redaction import contains_secret

        findings: list[dict[str, Any]] = []
        for path in file_paths:
            if path.suffix.lower() not in _TEXT_EXTENSIONS and path.name != SKILL_FILENAME:
                continue
            rel_path = str(path.relative_to(skill_dir)).replace("\\", "/")
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            file_findings = set(validate_upload_redaction(content))
            if contains_secret(content):
                file_findings.add("secret_in_content")
            if file_findings:
                findings.append({
                    "path": rel_path,
                    "findings": sorted(file_findings),
                })
        if not findings:
            return
        raise CloudError(
            "Skill upload blocked: secret-like content was found in skill files. "
            "Remove or redact the secret before uploading.",
            code="SKILL_UPLOAD_REDACTION_BLOCKED",
            kind="validation",
            retryable=False,
            suggested_action="remove_or_redact_skill_secrets_then_retry_upload_skill",
            details={
                "files": findings,
                "redaction_policy_version": REDACTION_POLICY_VERSION,
            },
        )

    @staticmethod
    def _extract_zip(zip_data: bytes, target_dir: Path) -> List[str]:
        """Extract zip bytes to target directory with path traversal protection."""
        extracted: List[str] = []
        resolved_target = target_dir.resolve()
        try:
            with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    clean_name = Path(info.filename).as_posix()
                    if clean_name.startswith("..") or clean_name.startswith("/"):
                        raise CloudError(f"Downloaded artifact contains unsafe path: {info.filename}")
                    target_path = (target_dir / clean_name).resolve()
                    if not target_path.is_relative_to(resolved_target):
                        raise CloudError(f"Downloaded artifact contains unsafe path: {info.filename}")
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_bytes(zf.read(info))
                    extracted.append(clean_name)
        except zipfile.BadZipFile:
            raise CloudError("Downloaded artifact is not a valid zip file")
        return extracted

    @staticmethod
    def _find_extracted_skill_root(staging_dir: Path) -> Path | None:
        direct = staging_dir / SKILL_FILENAME
        if direct.exists():
            return staging_dir
        matches = [path.parent for path in staging_dir.rglob(SKILL_FILENAME)]
        if not matches:
            return None
        if len(matches) > 1:
            raise CloudError("Downloaded skill bundle contains multiple SKILL.md files")
        return matches[0]

    @staticmethod
    def _copy_extracted_skill_tree(skill_root: Path, skill_dir: Path) -> None:
        def ignore(_directory: str, names: list[str]) -> set[str]:
            return {
                name for name in names
                if name in {
                    "skill_bundle.json",
                    "bundle.json",
                    "metadata",
                    SKILL_ID_FILENAME,
                    CLOUD_SKILL_INFO_FILENAME,
                    UPLOAD_META_FILENAME,
                }
            }

        shutil.copytree(skill_root, skill_dir, ignore=ignore)

    @staticmethod
    def _extract_zip_text_files(zip_data: bytes) -> Dict[str, str]:
        """Extract text files from zip as ``{filename: content}``."""
        files: Dict[str, str] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                for info in zf.infolist():
                    if info.is_dir() or info.filename in {
                        SKILL_ID_FILENAME,
                        CLOUD_SKILL_INFO_FILENAME,
                        UPLOAD_META_FILENAME,
                    }:
                        continue
                    try:
                        files[info.filename] = zf.read(info).decode("utf-8")
                    except (UnicodeDecodeError, KeyError):
                        pass
        except zipfile.BadZipFile:
            pass
        return files

    @staticmethod
    def _unified_diff(old_files: Dict[str, str], new_files: Dict[str, str]) -> Optional[str]:
        """Compute combined unified diff between two file snapshots."""
        all_names = sorted(set(old_files) | set(new_files))
        parts: List[str] = []
        for fname in all_names:
            old = old_files.get(fname, "")
            new = new_files.get(fname, "")
            d = "".join(difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{fname}",
                tofile=f"b/{fname}",
                n=3,
            ))
            if d:
                parts.append(d)
        return "\n".join(parts) if parts else None
