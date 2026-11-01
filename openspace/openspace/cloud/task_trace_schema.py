"""Contracts and policy helpers for openspace_task_trace_v2 artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

TASK_TRACE_ARTIFACT_FORMAT = "openspace_task_trace_v2"
TASK_TRACE_SCHEMA_VERSION = "2.0"
TASK_TRACE_COLLECTION_SCOPES = {
    "local_only",
    "cloud_discovery_only",
    "cloud_import_only",
    "cloud_skill_used",
    "cloud_skill_attempted",
    "cloud_evolve",
}
TASK_TRACE_REQUIRED_FILES = (
    "manifest.json",
    "task_metadata.json",
    "messages.jsonl",
    "llm_calls.jsonl",
    "tool_calls.jsonl",
    "skill_invocations.jsonl",
    "redaction_report.json",
    "derived_summary.json",
)


@dataclass(frozen=True)
class TaskTraceArtifact:
    archive_path: Path
    request_id: str
    task_id: str
    session_id: str
    manifest: dict[str, Any]
    sha256: str
    size_bytes: int
    compression: str
    collection_scope: str
    collection_reason: str
    cloud_skill_ids: tuple[str, ...]
    package_ids: tuple[str, ...]
    upload_allowed: bool = True
    deny_findings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CloudTaskInvolvement:
    scope: str
    reason: str
    cloud_skill_ids: tuple[str, ...] = ()
    local_skill_ids: tuple[str, ...] = ()
    package_ids: tuple[str, ...] = ()

    @property
    def requires_task_trace_artifact(self) -> bool:
        return self.scope in {
            "cloud_skill_used",
            "cloud_skill_attempted",
            "cloud_evolve",
        }


def classify_cloud_task_involvement(
    *,
    cloud_skill_ids: list[str] | tuple[str, ...] = (),
    local_skill_ids: list[str] | tuple[str, ...] = (),
    package_ids: list[str] | tuple[str, ...] = (),
    cloud_discovery: bool = False,
    cloud_import: bool = False,
    cloud_evolve: bool = False,
    cloud_attempted: bool = False,
) -> CloudTaskInvolvement:
    """Classify upload policy for one local task."""

    cloud_ids = tuple(dict.fromkeys(str(item) for item in cloud_skill_ids if str(item)))
    local_ids = tuple(dict.fromkeys(str(item) for item in local_skill_ids if str(item)))
    pkg_ids = tuple(dict.fromkeys(str(item) for item in package_ids if str(item)))
    if cloud_evolve:
        return CloudTaskInvolvement(
            scope="cloud_evolve",
            reason="cloud_skill_evolved",
            cloud_skill_ids=cloud_ids,
            local_skill_ids=local_ids,
            package_ids=pkg_ids,
        )
    if cloud_ids and cloud_attempted:
        return CloudTaskInvolvement(
            scope="cloud_skill_attempted",
            reason="cloud_skill_failed",
            cloud_skill_ids=cloud_ids,
            local_skill_ids=local_ids,
            package_ids=pkg_ids,
        )
    if cloud_ids:
        return CloudTaskInvolvement(
            scope="cloud_skill_used",
            reason="cloud_skill_invoked",
            cloud_skill_ids=cloud_ids,
            local_skill_ids=local_ids,
            package_ids=pkg_ids,
        )
    if cloud_import:
        return CloudTaskInvolvement(scope="cloud_import_only", reason="cloud_skill_imported")
    if cloud_discovery:
        return CloudTaskInvolvement(scope="cloud_discovery_only", reason="cloud_search_only")
    return CloudTaskInvolvement(scope="local_only", reason="local_only")


def model_inventory_from_runtime(runtime: Any | None = None) -> dict[str, Any]:
    """Collect model names for runtime, worker/analyzer, and evolve agents."""

    if runtime is None:
        return {}
    config = getattr(runtime, "config", None)
    state = getattr(runtime, "state", None)
    analyzer = getattr(state, "execution_analyzer", None) if state is not None else None
    evolver = getattr(state, "skill_evolver", None) if state is not None else None
    return {
        "runtime_model": _text(getattr(config, "llm_model", None)),
        "worker_model": _text(
            getattr(config, "tool_retrieval_model", None)
            or getattr(config, "llm_model", None)
        ),
        "skill_selection_model": _text(getattr(config, "skill_registry_model", None)),
        "execution_analyzer_model": _text(
            getattr(config, "execution_analyzer_model", None)
            or getattr(analyzer, "_model", None)
        ),
        "skill_evolver_model": _text(
            getattr(config, "skill_evolver_model", None)
            or getattr(evolver, "_model", None)
        ),
    }


def model_inventory_from_mapping(data: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        return {}
    return {
        key: _text(data.get(key))
        for key in (
            "runtime_model",
            "worker_model",
            "skill_selection_model",
            "execution_analyzer_model",
            "skill_evolver_model",
        )
        if _text(data.get(key))
    }


def _text(value: Any) -> str:
    return str(value or "").strip()
