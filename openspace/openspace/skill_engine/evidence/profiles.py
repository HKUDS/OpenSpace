"""Deterministic evidence packet profiles."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class TranscriptWindowPolicy:
    enabled: bool
    max_messages: int
    user_instruction_before: int
    user_instruction_after: int
    tool_before: int
    tool_after: int
    final_response_before: int
    final_response_after: int
    max_tool_anchors: int
    include_parent_chain: bool


@dataclass(frozen=True, slots=True)
class RepresentativeSamplingPolicy:
    enabled: bool
    ref_types: tuple[str, ...]
    max_groups: int
    max_per_group: int
    include_success_control: bool


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    max_selected_refs: int
    default_max_refs_per_type: int
    max_refs_per_type: dict[str, int]
    required_include_count: dict[str, int]
    transcript_window: TranscriptWindowPolicy
    representative_sampling: RepresentativeSamplingPolicy


@dataclass(frozen=True, slots=True)
class EvidenceProfile:
    name: str
    subprofile: str
    required_ref_types: tuple[str, ...]
    preferred_ref_types: tuple[str, ...]
    supporting_ref_types: tuple[str, ...]
    excluded_ref_types: tuple[str, ...]
    max_chars: int
    expansion_rules: dict[str, str]
    instructions: dict[str, str]
    selection_policy: SelectionPolicy


COMMON_INSTRUCTIONS: dict[str, str] = {
    "roles": (
        "Refs with role=primary are direct factual evidence. "
        "supporting/fallback refs provide context only."
    ),
    "large_outputs": (
        "Large tool outputs are represented by readable_paths. "
        "Do not treat previews as complete output."
    ),
    "compact_summary": (
        "Compact summaries are lossy. Factual claims must link back to "
        "transcript segment/message refs."
    ),
    "memory": (
        "Memory refs show background or visible constraints. They do not prove "
        "task success or skill defects."
    ),
    "recording": (
        "Recording refs are fallback/debug context, not the resume source of truth."
    ),
}

DEFAULT_TRANSCRIPT_WINDOW = TranscriptWindowPolicy(
    enabled=True,
    max_messages=16,
    user_instruction_before=0,
    user_instruction_after=4,
    tool_before=2,
    tool_after=2,
    final_response_before=3,
    final_response_after=0,
    max_tool_anchors=4,
    include_parent_chain=True,
)

NO_TRANSCRIPT_WINDOW = TranscriptWindowPolicy(
    enabled=False,
    max_messages=0,
    user_instruction_before=0,
    user_instruction_after=0,
    tool_before=0,
    tool_after=0,
    final_response_before=0,
    final_response_after=0,
    max_tool_anchors=0,
    include_parent_chain=False,
)

NO_REPRESENTATIVE_SAMPLING = RepresentativeSamplingPolicy(
    enabled=False,
    ref_types=(),
    max_groups=0,
    max_per_group=0,
    include_success_control=False,
)

BASE_SELECTION_POLICY = SelectionPolicy(
    max_selected_refs=80,
    default_max_refs_per_type=12,
    max_refs_per_type={
        "runtime_snapshot": 4,
        "transcript_message": 16,
        "tool_event": 24,
        "tool_result": 16,
        "skill_event": 16,
        "skill_record": 12,
        "skill_file": 12,
        "file_history": 8,
        "transcript_segment": 8,
        "compact_summary": 4,
        "memory_ref": 6,
        "recording_ref": 2,
        "background_task_result": 6,
        "manual_request_ref": 4,
        "tool_quality_record": 6,
        "tool_incident": 16,
        "quality_signal_ref": 8,
        "metric_window_ref": 4,
        "evolution_candidate_ref": 8,
        "decision_rationale_ref": 8,
    },
    required_include_count={
        "runtime_snapshot": 2,
        "transcript_message": 4,
        "skill_file": 12,
        "manual_request_ref": 2,
        "tool_incident": 2,
        "evolution_candidate_ref": 2,
        "decision_rationale_ref": 2,
    },
    transcript_window=DEFAULT_TRANSCRIPT_WINDOW,
    representative_sampling=NO_REPRESENTATIVE_SAMPLING,
)

QUALITY_SIGNAL_SELECTION_POLICY = replace(
    BASE_SELECTION_POLICY,
    transcript_window=replace(DEFAULT_TRANSCRIPT_WINDOW, max_messages=12),
)

ANALYSIS_EXPANSION_RULES: dict[str, str] = {
    "runtime_snapshot": "runtime_summary",
    "transcript_message": "task_window_message",
    "tool_event": "timeline",
    "tool_result": "preview_only",
    "skill_file": "frontmatter_preview",
    "compact_summary": "summary_with_backrefs",
    "recording_ref": "path_only",
    "memory_ref": "path_reason_only",
}

_BASE_PROFILES: dict[str, EvidenceProfile] = {
    "analysis_current_task": EvidenceProfile(
        name="analysis_current_task",
        subprofile="task_finished",
        required_ref_types=("runtime_snapshot", "transcript_message"),
        preferred_ref_types=(
            "tool_event",
            "tool_result",
            "quality_signal_ref",
            "skill_event",
            "skill_record",
            "skill_file",
            "file_history",
            "transcript_segment",
            "compact_summary",
        ),
        supporting_ref_types=(
            "recording_ref",
            "memory_ref",
            "background_task_result",
            "media_ref",
            "plan_ref",
            "manual_request_ref",
            "content_replacement",
        ),
        excluded_ref_types=(),
        max_chars=48_000,
        expansion_rules={
            **ANALYSIS_EXPANSION_RULES,
            "quality_signal_ref": "signal_summary",
        },
        instructions={
            **COMMON_INSTRUCTIONS,
            "quality_signal": (
                "quality_signal_ref captures derived quality observations for "
                "the current task. Treat it as context linked to raw refs, not "
                "as a standalone mutation command."
            ),
        },
        selection_policy=BASE_SELECTION_POLICY,
    ),
    "quality_signal": EvidenceProfile(
        name="quality_signal",
        subprofile="tool_failure_affects_skill",
        required_ref_types=(
            "quality_signal_ref",
            "tool_event",
            "skill_file",
            "skill_event",
        ),
        preferred_ref_types=(
            "tool_result",
            "tool_incident",
            "skill_record",
            "tool_quality_record",
            "execution_analysis",
            "transcript_message",
        ),
        supporting_ref_types=(
            "runtime_snapshot",
            "recording_ref",
            "memory_ref",
            "background_task_result",
        ),
        excluded_ref_types=(),
        max_chars=40_000,
        expansion_rules={
            **ANALYSIS_EXPANSION_RULES,
            "quality_signal_ref": "signal_summary",
            "tool_quality_record": "trigger_reason_only",
            "tool_incident": "incident_summary",
            "skill_file": "frontmatter_preview",
        },
        instructions={
            **COMMON_INSTRUCTIONS,
            "quality_signal": (
                "quality_signal_ref is derived evidence, not a decision or "
                "mutation command. Skill mutation still requires packet refs, "
                "DecisionRationale, Admission, validation, and commit."
            ),
            "quality_signal_raw_refs": (
                "Use the signal's raw_backrefs as representative evidence. "
                "Aggregate-only, conflicting, or observe-only evidence cannot "
                "justify a skill fix; actionable_partial can justify review "
                "when the current tool_event and skill refs support causality."
            ),
        },
        selection_policy=QUALITY_SIGNAL_SELECTION_POLICY,
    ),
    "manual_fix_or_derive": EvidenceProfile(
        name="manual_fix_or_derive",
        subprofile="fix",
        required_ref_types=("manual_request_ref", "skill_file"),
        preferred_ref_types=(
            "manual_request_ref",
            "runtime_snapshot",
            "transcript_message",
            "tool_event",
            "skill_event",
            "skill_record",
        ),
        supporting_ref_types=("tool_result", "file_history", "memory_ref", "recording_ref"),
        excluded_ref_types=(),
        max_chars=48_000,
        expansion_rules=ANALYSIS_EXPANSION_RULES,
        instructions={
            **COMMON_INSTRUCTIONS,
            "manual": (
                "Manual requests can be the primary trigger, but validator "
                "checks are still required before mutation."
            ),
        },
        selection_policy=BASE_SELECTION_POLICY,
    ),
    "manual_capture": EvidenceProfile(
        name="manual_capture",
        subprofile="capture",
        required_ref_types=("manual_request_ref",),
        preferred_ref_types=(
            "transcript_message",
            "tool_event",
            "tool_result",
            "file_history",
            "compact_summary",
            "memory_ref",
        ),
        supporting_ref_types=("runtime_snapshot", "recording_ref", "background_task_result"),
        excluded_ref_types=(),
        max_chars=44_000,
        expansion_rules=ANALYSIS_EXPANSION_RULES,
        instructions={
            **COMMON_INSTRUCTIONS,
            "manual_capture": (
                "Manual capture does not require a target skill file because "
                "the target skill may not exist yet."
            ),
        },
        selection_policy=BASE_SELECTION_POLICY,
    ),
}

_TRIGGER_DEFAULT_PROFILE: dict[str, str] = {
    "ANALYSIS": "analysis_current_task",
    "QUALITY_SIGNAL": "quality_signal",
    "MANUAL": "manual_capture",
}


def resolve_packet_profile(
    *,
    profile_name: str | None,
    subprofile: str | None,
    trigger_type: str | None = None,
) -> EvidenceProfile:
    name = str(profile_name or "").strip()
    if name not in _BASE_PROFILES:
        name = _TRIGGER_DEFAULT_PROFILE.get(str(trigger_type or "").strip(), "")
    if name not in _BASE_PROFILES:
        name = "analysis_current_task"
    profile = _BASE_PROFILES[name]
    resolved_subprofile = str(subprofile or profile.subprofile or "default").strip()
    if name == "manual_fix_or_derive" and resolved_subprofile not in {"fix", "derive"}:
        resolved_subprofile = "fix"
    return replace(profile, subprofile=resolved_subprofile or "default")


def profile_names() -> tuple[str, ...]:
    return tuple(sorted(_BASE_PROFILES))
