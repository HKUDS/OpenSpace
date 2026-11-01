"""Evidence data contracts for skill evolution provenance."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


ALLOWED_REF_TYPES: frozenset[str] = frozenset(
    {
        "runtime_snapshot",
        "agent_event",
        "transcript_message",
        "transcript_segment",
        "compact_summary",
        "content_replacement",
        "transcript_rewrite",
        "tool_event",
        "tool_result",
        "file_history",
        "skill_record",
        "skill_file",
        "skill_event",
        "tool_quality_record",
        "tool_incident",
        "quality_signal_ref",
        "metric_window_ref",
        "execution_analysis",
        "recording_ref",
        "manual_request_ref",
        "evidence_packet_ref",
        "decision_rationale_ref",
        "admission_result_ref",
        "evolution_candidate_ref",
        "validation_result_ref",
        "behavior_eval_result_ref",
        "authoring_result_ref",
        "evolution_action_ref",
        "memory_ref",
        "background_task_result",
        "media_ref",
        "plan_ref",
    }
)

ALLOWED_RELIABILITY: frozenset[str] = frozenset(
    {"runtime", "persisted", "derived", "fallback", "summary_only"}
)
ALLOWED_ROLES: frozenset[str] = frozenset(
    {"primary", "supporting", "derived"}
)
ALLOWED_SEVERITY: frozenset[str] = frozenset({"info", "warning", "error"})


@dataclass(frozen=True, slots=True)
class ResourceRef:
    ref_id: str
    ref_type: str
    uri: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    parent_task_id: str | None = None
    turn_id: str | None = None
    agent_id: str | None = None
    producer: str = "unknown"
    created_at: str = ""
    reliability: str = "runtime"
    role: str = "supporting"
    hash: str | None = None
    preview: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_backrefs: list[str] = field(default_factory=list)
    contains_secret: bool = False
    first_seen_watermark: int | None = None
    last_seen_watermark: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ResourceRef":
        return cls(
            ref_id=str(data.get("ref_id") or ""),
            ref_type=str(data.get("ref_type") or ""),
            uri=_none_or_str(data.get("uri")),
            session_id=_none_or_str(data.get("session_id")),
            task_id=_none_or_str(data.get("task_id")),
            parent_task_id=_none_or_str(data.get("parent_task_id")),
            turn_id=_none_or_str(data.get("turn_id")),
            agent_id=_none_or_str(data.get("agent_id")),
            producer=str(data.get("producer") or "unknown"),
            created_at=str(data.get("created_at") or ""),
            reliability=str(data.get("reliability") or "runtime"),
            role=str(data.get("role") or "supporting"),
            hash=_none_or_str(data.get("hash") or data.get("content_hash")),
            preview=str(data.get("preview") or ""),
            metadata=_dict_or_empty(data.get("metadata")),
            raw_backrefs=_str_list(data.get("raw_backrefs")),
            contains_secret=bool(data.get("contains_secret", False)),
            first_seen_watermark=_none_or_int(data.get("first_seen_watermark")),
            last_seen_watermark=_none_or_int(data.get("last_seen_watermark")),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResourceRef":
        return cls.from_mapping(data)


@dataclass(frozen=True, slots=True)
class EvidenceEvent:
    event_id: str
    event_type: str
    producer: str
    created_at: str
    session_id: str | None = None
    task_id: str | None = None
    parent_task_id: str | None = None
    turn_id: str | None = None
    agent_id: str | None = None
    severity: str = "info"
    idempotency_key: str = ""
    primary_refs: list[ResourceRef] = field(default_factory=list)
    supporting_refs: list[ResourceRef] = field(default_factory=list)
    derived_refs: list[ResourceRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        producer: str,
        created_at: str,
        idempotency_key: str,
        event_id: str | None = None,
        **kwargs: Any,
    ) -> "EvidenceEvent":
        return cls(
            event_id=event_id or f"evt_{uuid.uuid4().hex}",
            event_type=event_type,
            producer=producer,
            created_at=created_at,
            idempotency_key=idempotency_key,
            **kwargs,
        )

    def all_refs(self) -> list[ResourceRef]:
        return [
            *self.primary_refs,
            *self.supporting_refs,
            *self.derived_refs,
        ]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["primary_refs"] = [ref.to_dict() for ref in self.primary_refs]
        data["supporting_refs"] = [ref.to_dict() for ref in self.supporting_refs]
        data["derived_refs"] = [ref.to_dict() for ref in self.derived_refs]
        return data

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "EvidenceEvent":
        return cls(
            event_id=str(data.get("event_id") or ""),
            event_type=str(data.get("event_type") or ""),
            producer=str(data.get("producer") or ""),
            created_at=str(data.get("created_at") or ""),
            session_id=_none_or_str(data.get("session_id")),
            task_id=_none_or_str(data.get("task_id")),
            parent_task_id=_none_or_str(data.get("parent_task_id")),
            turn_id=_none_or_str(data.get("turn_id")),
            agent_id=_none_or_str(data.get("agent_id")),
            severity=str(data.get("severity") or "info"),
            idempotency_key=str(data.get("idempotency_key") or ""),
            primary_refs=[
                ResourceRef.from_mapping(item)
                for item in _mapping_list(data.get("primary_refs"))
            ],
            supporting_refs=[
                ResourceRef.from_mapping(item)
                for item in _mapping_list(data.get("supporting_refs"))
            ],
            derived_refs=[
                ResourceRef.from_mapping(item)
                for item in _mapping_list(data.get("derived_refs"))
            ],
            metadata=_dict_or_empty(data.get("metadata")),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceEvent":
        return cls.from_mapping(data)


@dataclass(frozen=True, slots=True)
class EvidenceScope:
    session_id: str | None = None
    task_id: str | None = None
    turn_range: tuple[str, str] | None = None
    skill_ids: tuple[str, ...] = ()
    tool_keys: tuple[str, ...] = ()
    source_task_ids: tuple[str, ...] = ()
    representative_execution_ids: tuple[str, ...] = ()
    time_window: tuple[str, str] | None = None
    agent_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in list(data.items()):
            if isinstance(value, tuple):
                data[key] = list(value)
        return data

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "EvidenceScope":
        turn_range = data.get("turn_range")
        time_window = data.get("time_window")
        return cls(
            session_id=_none_or_str(data.get("session_id")),
            task_id=_none_or_str(data.get("task_id")),
            turn_range=_pair_or_none(turn_range),
            skill_ids=tuple(_str_list(data.get("skill_ids"))),
            tool_keys=tuple(_str_list(data.get("tool_keys"))),
            source_task_ids=tuple(_str_list(data.get("source_task_ids"))),
            representative_execution_ids=tuple(
                _str_list(data.get("representative_execution_ids"))
            ),
            time_window=_pair_or_none(time_window),
            agent_ids=tuple(_str_list(data.get("agent_ids"))),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceScope":
        return cls.from_mapping(data)


@dataclass(frozen=True, slots=True)
class ManifestView:
    view_id: str
    scope: EvidenceScope
    watermark: int
    created_at: str
    refs: list[ResourceRef]

    def to_dict(self) -> dict[str, Any]:
        return {
            "view_id": self.view_id,
            "scope": self.scope.to_dict(),
            "watermark": self.watermark,
            "created_at": self.created_at,
            "refs": [ref.to_dict() for ref in self.refs],
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ManifestView":
        scope_data = data.get("scope")
        scope = (
            EvidenceScope.from_mapping(scope_data)
            if isinstance(scope_data, Mapping)
            else EvidenceScope()
        )
        return cls(
            view_id=str(data.get("view_id") or ""),
            scope=scope,
            watermark=int(data.get("watermark") or 0),
            created_at=str(data.get("created_at") or ""),
            refs=[
                ResourceRef.from_mapping(item)
                for item in _mapping_list(data.get("refs"))
            ],
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ManifestView":
        return cls.from_mapping(data)


@dataclass(frozen=True, slots=True)
class EvidenceSnippet:
    ref_id: str
    text: str
    truncation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "EvidenceSnippet":
        return cls(
            ref_id=str(data.get("ref_id") or ""),
            text=str(data.get("text") or ""),
            truncation=str(data.get("truncation") or "none"),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceSnippet":
        return cls.from_mapping(data)


@dataclass(frozen=True, slots=True)
class ReadablePathRef:
    ref_id: str
    path: str
    purpose: str
    readable: bool
    missing_reason: str | None = None
    contains_secret: bool = False
    max_read_chars: int | None = None
    original_length: int | None = None
    content_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ReadablePathRef":
        return cls(
            ref_id=str(data.get("ref_id") or ""),
            path=str(data.get("path") or ""),
            purpose=str(data.get("purpose") or ""),
            readable=bool(data.get("readable", False)),
            missing_reason=_none_or_str(data.get("missing_reason")),
            contains_secret=bool(data.get("contains_secret", False)),
            max_read_chars=_none_or_int(data.get("max_read_chars")),
            original_length=_none_or_int(data.get("original_length")),
            content_hash=_none_or_str(data.get("content_hash")),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReadablePathRef":
        return cls.from_mapping(data)


@dataclass(frozen=True, slots=True)
class PacketBudget:
    max_chars: int
    used_chars: int
    omitted_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PacketBudget":
        return cls(
            max_chars=int(data.get("max_chars") or 0),
            used_chars=int(data.get("used_chars") or 0),
            omitted_refs=_str_list(data.get("omitted_refs")),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PacketBudget":
        return cls.from_mapping(data)


@dataclass(frozen=True, slots=True)
class EvidencePacket:
    packet_id: str
    trigger_job_id: str
    packet_type: str
    profile_name: str
    subprofile: str
    manifest_watermark: int
    scope: EvidenceScope
    selected_refs: dict[str, list[ResourceRef]]
    expanded_snippets: list[EvidenceSnippet]
    readable_paths: list[ReadablePathRef]
    instructions: dict[str, str]
    budget: PacketBudget
    redaction_status: str
    build_status: str
    missing_ref_types: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "trigger_job_id": self.trigger_job_id,
            "packet_type": self.packet_type,
            "profile_name": self.profile_name,
            "subprofile": self.subprofile,
            "manifest_watermark": self.manifest_watermark,
            "scope": self.scope.to_dict(),
            "selected_refs": {
                key: [ref.to_dict() for ref in refs]
                for key, refs in sorted(self.selected_refs.items())
            },
            "expanded_snippets": [
                snippet.to_dict() for snippet in self.expanded_snippets
            ],
            "readable_paths": [path.to_dict() for path in self.readable_paths],
            "instructions": dict(self.instructions),
            "budget": self.budget.to_dict(),
            "redaction_status": self.redaction_status,
            "build_status": self.build_status,
            "missing_ref_types": list(self.missing_ref_types),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "EvidencePacket":
        scope_data = data.get("scope")
        scope = (
            EvidenceScope.from_mapping(scope_data)
            if isinstance(scope_data, Mapping)
            else EvidenceScope()
        )
        selected_input = data.get("selected_refs")
        selected_refs: dict[str, list[ResourceRef]] = {}
        if isinstance(selected_input, Mapping):
            for key, items in selected_input.items():
                if not isinstance(items, list):
                    continue
                selected_refs[str(key)] = [
                    ResourceRef.from_mapping(item)
                    for item in items
                    if isinstance(item, Mapping)
                ]
        budget_input = data.get("budget")
        budget = (
            PacketBudget.from_mapping(budget_input)
            if isinstance(budget_input, Mapping)
            else PacketBudget(max_chars=0, used_chars=0)
        )
        return cls(
            packet_id=str(data.get("packet_id") or ""),
            trigger_job_id=str(data.get("trigger_job_id") or ""),
            packet_type=str(data.get("packet_type") or ""),
            profile_name=str(data.get("profile_name") or ""),
            subprofile=str(data.get("subprofile") or ""),
            manifest_watermark=int(data.get("manifest_watermark") or 0),
            scope=scope,
            selected_refs=selected_refs,
            expanded_snippets=[
                EvidenceSnippet.from_mapping(item)
                for item in _mapping_list(data.get("expanded_snippets"))
            ],
            readable_paths=[
                ReadablePathRef.from_mapping(item)
                for item in _mapping_list(data.get("readable_paths"))
            ],
            instructions=_dict_str_str(data.get("instructions")),
            budget=budget,
            redaction_status=str(data.get("redaction_status") or ""),
            build_status=str(data.get("build_status") or ""),
            missing_ref_types=_str_list(data.get("missing_ref_types")),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidencePacket":
        return cls.from_mapping(data)


@dataclass(frozen=True, slots=True)
class PacketBuildResult:
    status: str
    packet: EvidencePacket | None
    noop_reason: str | None
    missing_ref_types: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "packet": self.packet.to_dict() if self.packet is not None else None,
            "noop_reason": self.noop_reason,
            "missing_ref_types": list(self.missing_ref_types),
        }


def _none_or_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _none_or_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _dict_str_str(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if str(item)]


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _pair_or_none(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    first, second = str(value[0]), str(value[1])
    return (first, second)
