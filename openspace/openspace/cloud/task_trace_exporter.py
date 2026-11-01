"""Build redacted openspace_task_trace_v2 artifact archives."""

from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from openspace.cloud.local_mapping import CloudLocalMappingStore
from openspace.cloud.redaction import (
    REDACTION_POLICY_VERSION,
    redact_task_trace_value,
    redaction_report_for_payload,
    sanitize_upload_path,
)
from openspace.cloud.task_trace_schema import (
    TASK_TRACE_ARTIFACT_FORMAT,
    TASK_TRACE_REQUIRED_FILES,
    TASK_TRACE_SCHEMA_VERSION,
    TaskTraceArtifact,
    classify_cloud_task_involvement,
)
from openspace.cloud.telemetry_payloads import short_cloud_request_id


class TaskTraceExportError(RuntimeError):
    """Raised when a task trace artifact cannot be safely exported."""


@dataclass(frozen=True)
class TaskTraceExportInput:
    task_id: str
    session_id: str
    collection_scope: str
    collection_reason: str
    status: str = "unknown"
    entrypoint: str = "openspace"
    workspace_ref: str = ""
    cloud_skill_ids: tuple[str, ...] = ()
    package_ids: tuple[str, ...] = ()
    messages: tuple[Mapping[str, Any], ...] = ()
    llm_calls: tuple[Mapping[str, Any], ...] = ()
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    skill_invocations: tuple[Mapping[str, Any], ...] = ()
    package_skill_provenance: tuple[Mapping[str, Any], ...] = ()
    evolution_events: tuple[Mapping[str, Any], ...] = ()
    task_metadata: Mapping[str, Any] | None = None
    model_inventory: Mapping[str, Any] | None = None


class TaskTraceExporter:
    """Write a redacted task trace directory and zip archive."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        workspace_root: str | Path | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None

    def export(self, data: TaskTraceExportInput) -> TaskTraceArtifact:
        if not data.task_id or not data.session_id:
            raise TaskTraceExportError("task_id and session_id are required")
        staging_parent = Path(
            tempfile.mkdtemp(prefix="openspace-task-trace-", dir=str(self.output_dir))
        )
        root = staging_parent / "task_trace_artifact"
        root.mkdir(parents=True)
        created_at = utc_now_iso()

        task_metadata = self._task_metadata(data, created_at=created_at)
        rows = {
            "messages.jsonl": self._message_rows(data.messages),
            "llm_calls.jsonl": self._generic_rows(data.llm_calls),
            "tool_calls.jsonl": self._generic_rows(data.tool_calls),
            "skill_invocations.jsonl": self._generic_rows(data.skill_invocations),
            "package_skill_provenance.jsonl": self._generic_rows(
                data.package_skill_provenance
            ),
            "evolution_events.jsonl": self._generic_rows(data.evolution_events),
        }
        _write_json(root / "task_metadata.json", task_metadata)
        for rel_path, file_rows in rows.items():
            _write_jsonl(root / rel_path, file_rows)

        derived_summary = {
            "task_id": data.task_id,
            "session_id": data.session_id,
            "status": data.status,
            "collection_scope": data.collection_scope,
            "collection_reason": data.collection_reason,
            "cloud_skill_count": len(data.cloud_skill_ids),
            "package_count": len(data.package_ids),
            "message_count": len(rows["messages.jsonl"]),
            "llm_call_count": len(rows["llm_calls.jsonl"]),
            "tool_call_count": len(rows["tool_calls.jsonl"]),
            "skill_invocation_count": len(rows["skill_invocations.jsonl"]),
            "model_inventory": redact_task_trace_value(
                dict(data.model_inventory or {}),
                workspace_root=self.workspace_root,
            ),
        }
        _write_json(root / "derived_summary.json", derived_summary)

        payload_for_report = {
            "task_metadata": task_metadata,
            "rows": rows,
            "derived_summary": derived_summary,
        }
        redaction_report = redaction_report_for_payload(payload_for_report)
        _write_json(root / "redaction_report.json", redaction_report)

        file_entries = self._file_entries(root)
        manifest = {
            "schema_version": TASK_TRACE_SCHEMA_VERSION,
            "artifact_format": TASK_TRACE_ARTIFACT_FORMAT,
            "task_id": data.task_id,
            "session_id": data.session_id,
            "collection_scope": data.collection_scope,
            "collection_reason": data.collection_reason,
            "created_at": created_at,
            "redaction_level": "complete_redacted",
            "redaction_policy_version": REDACTION_POLICY_VERSION,
            "compression": "zip",
            "cloud_skill_ids": list(data.cloud_skill_ids),
            "package_ids": list(data.package_ids),
            "files": file_entries,
        }
        _write_json(root / "manifest.json", manifest)
        _ensure_required_files(root)

        archive_stem = short_cloud_request_id(
            "task-trace-archive",
            data.task_id,
            data.session_id,
            created_at,
        ).replace(":", "-")
        archive_path = self.output_dir / f"{archive_stem}.zip"
        archive_path = _unique_path(archive_path)
        _zip_dir(root, archive_path)
        digest = _sha256_file(archive_path)
        request_id = short_cloud_request_id(
            "task-trace",
            data.task_id,
            data.session_id,
            digest,
        )
        return TaskTraceArtifact(
            archive_path=archive_path,
            request_id=request_id,
            task_id=data.task_id,
            session_id=data.session_id,
            manifest=manifest,
            sha256=digest,
            size_bytes=archive_path.stat().st_size,
            compression="zip",
            collection_scope=data.collection_scope,
            collection_reason=data.collection_reason,
            cloud_skill_ids=tuple(data.cloud_skill_ids),
            package_ids=tuple(data.package_ids),
            upload_allowed=bool(redaction_report.get("upload_allowed")),
            deny_findings=tuple(redaction_report.get("deny_findings") or ()),
        )

    def from_execution_result(
        self,
        final_result: Mapping[str, Any],
        *,
        task_id: str,
        session_id: str,
        mapping_store: CloudLocalMappingStore | None = None,
        model_inventory: Mapping[str, Any] | None = None,
    ) -> TaskTraceArtifact | None:
        """Create an artifact only when cloud involvement requires one."""

        local_skill_ids = _extract_local_skill_ids(final_result)
        cloud_bindings = _cloud_bindings_for_local_ids(mapping_store, local_skill_ids)
        cloud_skill_ids = tuple(
            binding.cloud_skill_id
            for binding in cloud_bindings
            if binding.cloud_skill_id
        )
        package_ids = tuple(
            item
            for binding in cloud_bindings
            for item in (binding.current_package_id, binding.package_id_at_pull)
            if item
        )
        cloud_evolve = any(
            _mapping_get(item, "source_cloud_skill_id")
            or _mapping_get(item, "cloud_skill_id")
            for item in final_result.get("evolved_skills") or []
            if isinstance(item, Mapping)
        )
        status = str(final_result.get("status") or "unknown")
        involvement = classify_cloud_task_involvement(
            cloud_skill_ids=list(cloud_skill_ids),
            local_skill_ids=local_skill_ids,
            package_ids=list(package_ids),
            cloud_evolve=cloud_evolve,
            cloud_attempted=status not in {"success", "completed", "ok"},
        )
        if not involvement.requires_task_trace_artifact:
            return None

        data = TaskTraceExportInput(
            task_id=task_id,
            session_id=session_id,
            collection_scope=involvement.scope,
            collection_reason=involvement.reason,
            status=status,
            workspace_ref=(
                sanitize_upload_path(self.workspace_root, workspace_root=self.workspace_root)
                if self.workspace_root
                else ""
            ),
            cloud_skill_ids=involvement.cloud_skill_ids,
            package_ids=involvement.package_ids,
            messages=tuple(_message_inputs(final_result)),
            tool_calls=tuple(_tool_call_inputs(final_result)),
            skill_invocations=tuple(
                _skill_invocation_rows(cloud_bindings, final_result=final_result)
            ),
            package_skill_provenance=tuple(
                _provenance_rows(cloud_bindings, workspace_root=self.workspace_root)
            ),
            evolution_events=tuple(_evolution_event_rows(final_result)),
            task_metadata={
                "iterations": final_result.get("iterations"),
                "stop_reason": final_result.get("stop_reason"),
                "execution_time": final_result.get("execution_time"),
            },
            model_inventory=model_inventory or {},
        )
        return self.export(data)

    def _task_metadata(
        self,
        data: TaskTraceExportInput,
        *,
        created_at: str,
    ) -> dict[str, Any]:
        metadata = {
            "task_id": data.task_id,
            "session_id": data.session_id,
            "agent_id": "",
            "started_at": "",
            "ended_at": created_at,
            "status": data.status,
            "entrypoint": data.entrypoint,
            "workspace_ref": data.workspace_ref,
            "collection_scope": data.collection_scope,
            "collection_reason": data.collection_reason,
            "model_inventory": dict(data.model_inventory or {}),
        }
        metadata.update(dict(data.task_metadata or {}))
        return redact_task_trace_value(metadata, workspace_root=self.workspace_root)

    def _message_rows(self, messages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            content = _extract_content_text(message)
            redacted = redact_task_trace_value(content, workspace_root=self.workspace_root)
            rows.append({
                "message_id": str(message.get("message_id") or f"msg_{index + 1:06d}"),
                "turn_id": str(message.get("turn_id") or ""),
                "role": str(message.get("role") or "unknown"),
                "created_at": str(message.get("created_at") or ""),
                "content_type": "text",
                "content": redacted,
                "content_sha256": hashlib.sha256(str(redacted).encode("utf-8")).hexdigest(),
                "redaction_applied": True,
                "source": str(message.get("source") or "execution_result"),
            })
        return rows

    def _generic_rows(self, rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            redact_task_trace_value(dict(row), workspace_root=self.workspace_root)
            for row in rows
            if isinstance(row, Mapping)
        ]

    def _file_entries(self, root: Path) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name == "manifest.json":
                continue
            rel = path.relative_to(root).as_posix()
            entries.append({
                "path": rel,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            })
        return entries


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")


def _ensure_required_files(root: Path) -> None:
    missing = [name for name in TASK_TRACE_REQUIRED_FILES if not (root / name).exists()]
    if missing:
        raise TaskTraceExportError(f"task trace missing required files: {missing}")


def _zip_dir(root: Path, archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, Path("task_trace_artifact") / path.relative_to(root))


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise TaskTraceExportError(f"could not allocate artifact path under {path.parent}")


def _extract_content_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return json.dumps(content, ensure_ascii=False, default=str)


def _message_inputs(final_result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = final_result.get("messages")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, Mapping)]
    response = final_result.get("response")
    if response:
        return [{"role": "assistant", "content": str(response), "source": "final_response"}]
    return []


def _tool_call_inputs(final_result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for index, item in enumerate(final_result.get("tool_executions") or []):
        if not isinstance(item, Mapping):
            continue
        rows.append({
            "tool_call_id": str(item.get("tool_use_id") or item.get("id") or f"tool_{index + 1:06d}"),
            "tool_name": str(item.get("tool_name") or item.get("name") or item.get("tool") or ""),
            "status": str(item.get("status") or ""),
            "duration_ms": item.get("duration_ms"),
            "input": item.get("input") or item.get("args") or {},
            "output": item.get("output") or item.get("result") or "",
        })
    return rows


def _extract_local_skill_ids(final_result: Mapping[str, Any]) -> list[str]:
    raw = (
        final_result.get("active_skill_ids")
        or final_result.get("active_skills")
        or final_result.get("skills_used")
        or []
    )
    if isinstance(raw, str):
        raw = [raw]
    return [str(item) for item in raw if str(item)]


def _cloud_bindings_for_local_ids(
    store: CloudLocalMappingStore | None,
    local_skill_ids: Iterable[str],
):
    if store is None:
        return []
    bindings = []
    for local_skill_id in local_skill_ids:
        try:
            binding = store.get_binding_by_local(local_skill_id)
        except Exception:
            binding = None
        if binding is not None and binding.cloud_skill_id:
            bindings.append(binding)
    return bindings


def _skill_invocation_rows(
    bindings: Iterable[Any],
    *,
    final_result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    status = str(final_result.get("status") or "")
    rows: list[dict[str, Any]] = []
    for index, binding in enumerate(bindings):
        rows.append({
            "skill_invocation_id": f"skill_use_{index + 1:06d}",
            "cloud_skill_id": binding.cloud_skill_id,
            "local_skill_id": binding.local_skill_id,
            "package_id": binding.current_package_id or binding.package_id_at_pull,
            "skill_name": "",
            "status": status or "unknown",
            "used_cloud_origin": True,
        })
    return rows


def _provenance_rows(
    bindings: Iterable[Any],
    *,
    workspace_root: Path | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for binding in bindings:
        rows.append({
            "cloud_skill_id": binding.cloud_skill_id,
            "local_skill_id": binding.local_skill_id,
            "package_id": binding.current_package_id or binding.package_id_at_pull,
            "package_path_at_pull": binding.package_path_at_pull,
            "local_path": sanitize_upload_path(binding.local_path, workspace_root=workspace_root),
            "manifest_hash": binding.manifest_hash,
            "absorbed_at": binding.last_pulled_at,
        })
    return rows


def _evolution_event_rows(final_result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for index, item in enumerate(final_result.get("evolved_skills") or []):
        if not isinstance(item, Mapping):
            continue
        rows.append({
            "evolve_event_id": str(item.get("evolve_event_id") or f"evolve_{index + 1:06d}"),
            "event_type": str(item.get("origin") or item.get("type") or "evolve"),
            "source_cloud_skill_id": item.get("source_cloud_skill_id"),
            "source_local_skill_id": item.get("source_local_skill_id"),
            "result_local_skill_id": item.get("local_skill_id") or item.get("skill_id"),
            "result_cloud_skill_id": item.get("cloud_skill_id"),
            "status": str(item.get("status") or "success"),
            "summary": item.get("summary") or item.get("change_summary") or "",
            "created_at": item.get("created_at") or "",
        })
    return rows


def _mapping_get(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else None
