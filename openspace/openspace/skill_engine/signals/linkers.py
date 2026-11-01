"""Deterministic evidence linkers for quality signals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from openspace.grounding.core.permissions.types import (
    normalize_tool_name_for_rule,
    parse_rule_value,
)
from openspace.skill_engine.evidence import ResourceRef


ACTIVE_SKILL_EVENT_TYPES: frozenset[str] = frozenset({"invoked", "applied"})
FAILED_TOOL_STATUSES: frozenset[str] = frozenset(
    {
        "blocked",
        "cancelled",
        "canceled",
        "denied",
        "error",
        "failed",
        "failure",
        "permission_denied",
        "rejected",
        "timeout",
        "timed_out",
    }
)


@dataclass(frozen=True, slots=True)
class ToolUseKey:
    session_id: str
    task_id: str
    agent_id: str
    tool_use_id: str


@dataclass(frozen=True, slots=True)
class SkillContextCandidate:
    skill_id: str
    skill_event: ResourceRef
    skill_record: ResourceRef | None
    skill_file: ResourceRef | None
    link_type: str


@dataclass(frozen=True, slots=True)
class EvidenceIndexes:
    refs: tuple[ResourceRef, ...]
    tool_events_by_use_id: dict[ToolUseKey, list[ResourceRef]]
    tool_results_by_use_id: dict[ToolUseKey, list[ResourceRef]]
    tool_incidents_by_tool_key: dict[str, list[ResourceRef]]
    quality_records_by_tool_key: dict[str, list[ResourceRef]]
    skill_events_by_skill_id: dict[str, list[ResourceRef]]
    skill_files_by_skill_id: dict[str, list[ResourceRef]]
    skill_records_by_skill_id: dict[str, list[ResourceRef]]

    @property
    def failed_tool_events(self) -> list[ResourceRef]:
        refs: list[ResourceRef] = []
        for bucket in self.tool_events_by_use_id.values():
            refs.extend(ref for ref in bucket if is_failed_tool_event(ref))
        return sorted(refs, key=ref_sort_key)


def build_evidence_indexes(refs: Iterable[ResourceRef]) -> EvidenceIndexes:
    tool_events_by_use_id: dict[ToolUseKey, list[ResourceRef]] = {}
    tool_results_by_use_id: dict[ToolUseKey, list[ResourceRef]] = {}
    tool_incidents_by_tool_key: dict[str, list[ResourceRef]] = {}
    quality_records_by_tool_key: dict[str, list[ResourceRef]] = {}
    skill_events_by_skill_id: dict[str, list[ResourceRef]] = {}
    skill_files_by_skill_id: dict[str, list[ResourceRef]] = {}
    skill_records_by_skill_id: dict[str, list[ResourceRef]] = {}
    ordered_refs = tuple(sorted(refs, key=ref_sort_key))

    for ref in ordered_refs:
        if ref.ref_type == "tool_event":
            key = tool_use_key(ref)
            if key.tool_use_id:
                tool_events_by_use_id.setdefault(key, []).append(ref)
        elif ref.ref_type == "tool_result":
            key = tool_use_key(ref)
            if key.tool_use_id:
                tool_results_by_use_id.setdefault(key, []).append(ref)
        elif ref.ref_type == "tool_incident":
            tool_key = ref_tool_key(ref)
            if tool_key:
                tool_incidents_by_tool_key.setdefault(tool_key, []).append(ref)
        elif ref.ref_type == "tool_quality_record":
            tool_key = ref_tool_key(ref)
            if tool_key:
                quality_records_by_tool_key.setdefault(tool_key, []).append(ref)
        elif ref.ref_type == "skill_event":
            skill_id = ref_skill_id(ref)
            if skill_id:
                skill_events_by_skill_id.setdefault(skill_id, []).append(ref)
        elif ref.ref_type == "skill_file":
            skill_id = ref_skill_id(ref)
            if skill_id:
                skill_files_by_skill_id.setdefault(skill_id, []).append(ref)
        elif ref.ref_type == "skill_record":
            skill_id = ref_skill_id(ref)
            if skill_id:
                skill_records_by_skill_id.setdefault(skill_id, []).append(ref)

    return EvidenceIndexes(
        refs=ordered_refs,
        tool_events_by_use_id=tool_events_by_use_id,
        tool_results_by_use_id=tool_results_by_use_id,
        tool_incidents_by_tool_key=tool_incidents_by_tool_key,
        quality_records_by_tool_key=quality_records_by_tool_key,
        skill_events_by_skill_id=skill_events_by_skill_id,
        skill_files_by_skill_id=skill_files_by_skill_id,
        skill_records_by_skill_id=skill_records_by_skill_id,
    )


def resolve_skill_contexts(
    tool_event: ResourceRef,
    indexes: EvidenceIndexes,
) -> list[SkillContextCandidate]:
    tool_key = ref_tool_key(tool_event)
    if not tool_key:
        return []

    candidates: list[SkillContextCandidate] = []
    seen: set[str] = set()
    for skill_id, events in indexes.skill_events_by_skill_id.items():
        active_events = [
            ref
            for ref in events
            if skill_event_type(ref) in ACTIVE_SKILL_EVENT_TYPES
            and same_run_window(tool_event, ref)
        ]
        for skill_event in sorted(active_events, key=ref_sort_key):
            link_type = _skill_link_type(tool_event, skill_event, indexes)
            if not link_type or skill_id in seen:
                continue
            seen.add(skill_id)
            candidates.append(
                SkillContextCandidate(
                    skill_id=skill_id,
                    skill_event=skill_event,
                    skill_record=latest_ref(indexes.skill_records_by_skill_id.get(skill_id, [])),
                    skill_file=latest_ref(indexes.skill_files_by_skill_id.get(skill_id, [])),
                    link_type=link_type,
                )
            )
    return sorted(candidates, key=lambda item: (item.skill_id, item.link_type))


def related_tool_results(
    tool_event: ResourceRef,
    indexes: EvidenceIndexes,
) -> list[ResourceRef]:
    key = tool_use_key(tool_event)
    refs = indexes.tool_results_by_use_id.get(key, [])
    return sorted(refs, key=ref_sort_key)


def related_tool_incidents(
    tool_event: ResourceRef,
    indexes: EvidenceIndexes,
) -> list[ResourceRef]:
    tool_key = ref_tool_key(tool_event)
    if not tool_key:
        return []
    refs = [
        ref
        for ref in indexes.tool_incidents_by_tool_key.get(tool_key, [])
        if same_run_window(tool_event, ref)
        and same_tool_attempt(tool_event, ref)
    ]
    return sorted(refs, key=ref_sort_key)


def has_representative_tool_evidence(
    tool_key: str,
    indexes: EvidenceIndexes,
) -> bool:
    if indexes.tool_incidents_by_tool_key.get(tool_key):
        return True
    return any(
        ref_tool_key(ref) == tool_key
        for bucket in indexes.tool_events_by_use_id.values()
        for ref in bucket
    )


def is_failed_tool_event(ref: ResourceRef) -> bool:
    status = str(ref.metadata.get("status") or "").strip().lower()
    if ref.ref_type != "tool_event":
        return False
    if status in FAILED_TOOL_STATUSES:
        return True
    permission_status = str(ref.metadata.get("permission_status") or "").strip().lower()
    return permission_status in {"blocked", "denied", "permission_denied"}


def ref_tool_key(ref: ResourceRef) -> str:
    return _first_text(ref.metadata, "tool_key", "tool_name")


def ref_skill_id(ref: ResourceRef) -> str:
    return _first_text(
        ref.metadata,
        "skill_id",
        "target_skill_id",
        "affected_skill_id",
    )


def skill_event_type(ref: ResourceRef) -> str:
    return _first_text(ref.metadata, "event_type", "lifecycle_event").lower()


def tool_use_key(ref: ResourceRef) -> ToolUseKey:
    return ToolUseKey(
        session_id=str(ref.session_id or ""),
        task_id=str(ref.task_id or ref.metadata.get("task_id") or ""),
        agent_id=str(ref.agent_id or ref.metadata.get("agent_id") or ""),
        tool_use_id=_first_text(
            ref.metadata,
            "tool_use_id",
            "tool_call_id",
            "call_id",
        ),
    )


def same_run_window(left: ResourceRef, right: ResourceRef) -> bool:
    for left_value, right_value in (
        (left.session_id, right.session_id),
        (
            left.task_id or left.metadata.get("task_id"),
            right.task_id or right.metadata.get("task_id"),
        ),
    ):
        if left_value and not right_value:
            return False
        if left_value and right_value and str(left_value) != str(right_value):
            return False
    if left.agent_id and right.agent_id and str(left.agent_id) != str(right.agent_id):
        return False
    return True


def same_tool_attempt(tool_event: ResourceRef, ref: ResourceRef) -> bool:
    event_key = tool_use_key(tool_event)
    ref_key = tool_use_key(ref)
    if event_key.tool_use_id:
        return (
            ref_key.tool_use_id == event_key.tool_use_id
            or tool_event.ref_id in set(ref.raw_backrefs or [])
        )
    return tool_event.ref_id in set(ref.raw_backrefs or [])


def latest_ref(refs: Sequence[ResourceRef]) -> ResourceRef | None:
    if not refs:
        return None
    return sorted(refs, key=ref_sort_key)[-1]


def ref_sort_key(ref: ResourceRef) -> tuple[int, str]:
    return (int(ref.last_seen_watermark or ref.first_seen_watermark or 0), ref.ref_id)


def metadata_values(metadata: Mapping[str, Any], *keys: str) -> set[str]:
    values: set[str] = set()
    for key in keys:
        value = metadata.get(key)
        values.update(_string_values(value))
    nested = metadata.get("metadata")
    if isinstance(nested, Mapping):
        for key in keys:
            values.update(_string_values(nested.get(key)))
    return values


def _skill_link_type(
    tool_event: ResourceRef,
    skill_event: ResourceRef,
    indexes: EvidenceIndexes,
) -> str:
    skill_id = ref_skill_id(skill_event)
    tool_key = ref_tool_key(tool_event)
    tool_use_id = tool_use_key(tool_event).tool_use_id

    record = latest_ref(indexes.skill_records_by_skill_id.get(skill_id, []))
    if record is not None:
        dependency_values = metadata_values(
            record.metadata,
            "tool_dependencies",
            "critical_tools",
            "required_tools",
            "allowed_tools",
            "allowed-tools",
            "allowedTools",
        )
        if _tool_matches_any_dependency(tool_key, dependency_values):
            return "skill_dependency"

    explicit_skill_ids = metadata_values(
        tool_event.metadata,
        "skill_id",
        "skill_ids",
        "active_skill_id",
        "active_skill_ids",
        "invoked_skill_id",
        "invoked_skill_ids",
    )
    if skill_id in explicit_skill_ids or skill_id in set(tool_event.raw_backrefs):
        return "explicit_invocation_scope"

    tool_backrefs = set(tool_event.raw_backrefs)
    tool_backrefs.update(
        metadata_values(
            tool_event.metadata,
            "skill_event_ref_id",
            "skill_event_ref_ids",
            "skill_event_ref",
            "skill_event_refs",
            "raw_backrefs",
        )
    )
    if skill_event.ref_id in tool_backrefs:
        return "explicit_invocation_scope"

    if _scope_ids(tool_event.metadata).intersection(_scope_ids(skill_event.metadata)):
        return "explicit_invocation_scope"

    skill_tool_use_ids = metadata_values(
        skill_event.metadata,
        "tool_use_id",
        "tool_use_ids",
        "tool_call_id",
        "tool_call_ids",
    )
    if tool_use_id and tool_use_id in skill_tool_use_ids:
        return "skill_event_tool_use"

    skill_backrefs = set(skill_event.raw_backrefs)
    skill_backrefs.update(
        metadata_values(
            skill_event.metadata,
            "tool_event_ref",
            "tool_event_refs",
            "tool_ref",
            "tool_refs",
            "raw_backrefs",
        )
    )
    if tool_event.ref_id in skill_backrefs or (tool_use_id and tool_use_id in skill_backrefs):
        return "skill_event_backref"

    return ""


def _tool_matches_any_dependency(tool_key: str, dependencies: Iterable[str]) -> bool:
    tool_variants = _tool_identity_variants(tool_key)
    if not tool_variants:
        return False
    for dependency in dependencies:
        if tool_variants.intersection(_tool_identity_variants(dependency)):
            return True
    return False


def _tool_identity_variants(value: Any) -> set[str]:
    raw = str(value or "").strip()
    if not raw:
        return set()

    candidates: set[str] = {raw}
    try:
        parsed = parse_rule_value(raw).tool_name
        if parsed:
            candidates.add(parsed)
    except Exception:
        head = raw.split("(", 1)[0].strip()
        if head:
            candidates.add(normalize_tool_name_for_rule(head))

    variants: set[str] = set()
    for candidate in list(candidates):
        candidate = str(candidate or "").strip()
        if not candidate:
            continue
        variants.add(_identity_token(candidate))
        _add_colon_variants(candidate, variants)
        _add_mcp_separator_variants(candidate, variants)
    return {item for item in variants if item}


def _add_colon_variants(value: str, variants: set[str]) -> None:
    parts = [part.strip() for part in value.split(":") if part.strip()]
    if len(parts) == 3:
        backend, server, tool_name = parts
        variants.add(_identity_token(f"{backend}:{server}:{tool_name}"))
        variants.add(_identity_token(f"{backend}:{tool_name}"))
        variants.add(_identity_token(tool_name))
        if backend.lower() == "mcp":
            variants.add(_identity_token(f"mcp__{server}__{tool_name}"))
            variants.add(_identity_token(f"{server}__{tool_name}"))
            variants.add(_identity_token(f"{server}:{tool_name}"))
    elif len(parts) == 2:
        first, second = parts
        variants.add(_identity_token(f"{first}:{second}"))
        variants.add(_identity_token(second))


def _add_mcp_separator_variants(value: str, variants: set[str]) -> None:
    parts = [part.strip() for part in value.split("__") if part.strip()]
    if len(parts) >= 3 and parts[0].lower() == "mcp":
        server = parts[1]
        tool_name = "__".join(parts[2:])
        variants.add(_identity_token(f"mcp:{server}:{tool_name}"))
        variants.add(_identity_token(f"{server}:{tool_name}"))
        variants.add(_identity_token(f"{server}__{tool_name}"))
        variants.add(_identity_token(tool_name))
    elif len(parts) >= 2:
        tool_name = parts[-1]
        server = parts[-2]
        variants.add(_identity_token(f"{server}:{tool_name}"))
        variants.add(_identity_token(tool_name))


def _identity_token(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _scope_ids(metadata: Mapping[str, Any]) -> set[str]:
    return metadata_values(
        metadata,
        "skill_invocation_scope_id",
        "invocation_scope_id",
        "skill_scope_id",
        "scope_id",
    )


def _first_text(metadata: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value) != "":
            return str(value)
    return ""


def _string_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, Mapping):
        result: set[str] = set()
        for item in value.values():
            result.update(_string_values(item))
        return result
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value if str(item)}
    return {str(value)} if str(value) else set()
