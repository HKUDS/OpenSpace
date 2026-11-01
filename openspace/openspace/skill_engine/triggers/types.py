"""Trigger job contracts for evidence-driven evolution."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from openspace.skill_engine.evidence import EvidenceScope


TRIGGER_TYPES: frozenset[str] = frozenset(
    {
        "ANALYSIS",
        "QUALITY_SIGNAL",
        "MANUAL",
    }
)

TRIGGER_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "running",
        "completed",
        "failed",
        "failed_retryable",
        "superseded",
        "rejected",
    }
)


@dataclass(frozen=True, slots=True)
class TriggerJob:
    job_id: str
    trigger_type: str
    reason: str
    reason_tags: list[str]
    status: str
    scope: EvidenceScope
    manifest_watermark: int
    evidence_profile: str
    subprofile: str
    profile_fallback: bool
    idempotency_key: str
    attempts: int
    locked_at: str | None
    locked_by: str | None
    created_at: str
    scheduled_at: str
    completed_at: str | None
    error: str | None
    result_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["scope"] = self.scope.to_dict()
        return data

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TriggerJob":
        scope_data = data.get("scope")
        scope = (
            EvidenceScope.from_mapping(scope_data)
            if isinstance(scope_data, Mapping)
            else EvidenceScope()
        )
        return cls(
            job_id=str(data.get("job_id") or ""),
            trigger_type=str(data.get("trigger_type") or ""),
            reason=str(data.get("reason") or ""),
            reason_tags=_str_list(data.get("reason_tags")),
            status=str(data.get("status") or "pending"),
            scope=scope,
            manifest_watermark=int(data.get("manifest_watermark") or 0),
            evidence_profile=str(data.get("evidence_profile") or ""),
            subprofile=str(data.get("subprofile") or ""),
            profile_fallback=bool(data.get("profile_fallback", False)),
            idempotency_key=str(data.get("idempotency_key") or ""),
            attempts=int(data.get("attempts") or 0),
            locked_at=_none_or_str(data.get("locked_at")),
            locked_by=_none_or_str(data.get("locked_by")),
            created_at=str(data.get("created_at") or ""),
            scheduled_at=str(data.get("scheduled_at") or ""),
            completed_at=_none_or_str(data.get("completed_at")),
            error=_none_or_str(data.get("error")),
            result_ref=_none_or_str(data.get("result_ref")),
        )


@dataclass(frozen=True, slots=True)
class TriggerJobSpec:
    trigger_type: str
    reason: str
    scope: EvidenceScope
    idempotency_key: str
    evidence_profile: str
    subprofile: str
    reason_tags: list[str] = field(default_factory=list)
    profile_fallback: bool = False
    scheduled_at: str | None = None


@dataclass(frozen=True, slots=True)
class ManualTriggerRequest:
    action: str
    reason: str = "user_requested"
    request_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    skill_ids: tuple[str, ...] = ()
    tool_keys: tuple[str, ...] = ()
    source_task_ids: tuple[str, ...] = ()
    representative_execution_ids: tuple[str, ...] = ()
    agent_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in list(data.items()):
            if isinstance(value, tuple):
                data[key] = list(value)
        return data

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ManualTriggerRequest":
        return cls(
            action=str(data.get("action") or ""),
            reason=str(data.get("reason") or "user_requested"),
            request_id=_none_or_str(data.get("request_id")),
            session_id=_none_or_str(data.get("session_id")),
            task_id=_none_or_str(data.get("task_id")),
            skill_ids=tuple(_str_list(data.get("skill_ids"))),
            tool_keys=tuple(_str_list(data.get("tool_keys"))),
            source_task_ids=tuple(_str_list(data.get("source_task_ids"))),
            representative_execution_ids=tuple(
                _str_list(data.get("representative_execution_ids"))
            ),
            agent_ids=tuple(_str_list(data.get("agent_ids"))),
            metadata=_dict_or_empty(data.get("metadata")),
        )


def _none_or_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return []


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}
