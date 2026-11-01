"""Rule-based EvidencePacket construction."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from openspace.utils.logging import Logger

from .profiles import EvidenceProfile, resolve_packet_profile
from .redaction import contains_secret, redact_text
from .types import (
    EvidencePacket,
    EvidenceScope,
    EvidenceSnippet,
    PacketBudget,
    PacketBuildResult,
    ReadablePathRef,
    ResourceRef,
)

logger = Logger.get_logger(__name__)

_PACKET_TYPES = {"analysis", "action", "validator"}
_RELIABILITY_RANK = {
    "persisted": 0,
    "runtime": 0,
    "derived": 1,
    "fallback": 2,
    "summary_only": 3,
}
_ROLE_RANK = {"primary": 0, "supporting": 1, "derived": 2}
_PATH_REF_TYPES = {
    "tool_result",
    "skill_file",
    "transcript_segment",
    "file_history",
    "recording_ref",
    "memory_ref",
    "media_ref",
    "background_task_result",
    "authoring_result_ref",
}
_REQUIRED_READABLE_PATH_TYPES = {"skill_file"}
_GENERIC_PREVIEW_CHARS = 2_000
_TOOL_RESULT_PREVIEW_CHARS = 2_000
_SKILL_ANALYSIS_PREVIEW_CHARS = 2_000
_SKILL_ACTION_MAX_CHARS = 16_000
_READABLE_MAX_CHARS = 200_000


class PacketBuilder:
    """Build deterministic packets from frozen EvidenceStore manifest views."""

    def __init__(self, evidence_store: Any) -> None:
        self.evidence_store = evidence_store

    def build_trigger_packet(self, job: Any) -> PacketBuildResult:
        return self._build_job_packet(job, packet_type="analysis")

    def build_analysis_packet(self, job: Any) -> PacketBuildResult:
        return self.build_trigger_packet(job)

    def build_action_packet(self, decision: Any) -> PacketBuildResult:
        source_packet = self._load_source_packet(decision)
        if source_packet is None:
            return PacketBuildResult(
                status="invalid_scope",
                packet=None,
                noop_reason="source_packet_not_found",
                missing_ref_types=[],
            )
        decision_id = _attr(decision, "decision_id") or _mapping_get(decision, "decision_id")
        if not decision_id:
            return PacketBuildResult(
                status="invalid_scope",
                packet=None,
                noop_reason="missing_decision_id",
                missing_ref_types=[],
            )
        trigger_job_id = (
            _attr(decision, "trigger_job_id")
            or _mapping_get(decision, "trigger_job_id")
            or source_packet.trigger_job_id
        )
        required_refs = [("decision_rationale_ref", f"decision:{decision_id}")]
        admission_id = _attr(decision, "admission_id") or _mapping_get(decision, "admission_id")
        if admission_id:
            required_refs.append(("admission_result_ref", f"admission:{admission_id}"))
        extra_refs, missing = self._load_required_refs(required_refs)
        if missing:
            return PacketBuildResult(
                status="insufficient_evidence",
                packet=None,
                noop_reason="required_refs_missing",
                missing_ref_types=missing,
            )
        return self._build_pinned_packet(
            source_packet,
            packet_type="action",
            trigger_job_id=trigger_job_id,
            subprofile=f"action:{source_packet.subprofile}",
            extra_refs=extra_refs,
        )

    def build_validator_packet(self, authoring: Any) -> PacketBuildResult:
        source_packet = self._load_source_packet(authoring)
        if source_packet is None:
            return PacketBuildResult(
                status="invalid_scope",
                packet=None,
                noop_reason="source_packet_not_found",
                missing_ref_types=[],
            )
        authoring_id = (
            _attr(authoring, "authoring_id")
            or _mapping_get(authoring, "authoring_id")
        )
        if not authoring_id:
            return PacketBuildResult(
                status="invalid_scope",
                packet=None,
                noop_reason="missing_authoring_id",
                missing_ref_types=[],
            )
        trigger_job_id = (
            _attr(authoring, "trigger_job_id")
            or _mapping_get(authoring, "trigger_job_id")
            or source_packet.trigger_job_id
        )
        extra_refs, missing = self._load_required_refs(
            [("authoring_result_ref", f"authoring:{authoring_id}")]
        )
        if missing:
            return PacketBuildResult(
                status="insufficient_evidence",
                packet=None,
                noop_reason="required_refs_missing",
                missing_ref_types=missing,
            )
        return self._build_pinned_packet(
            source_packet,
            packet_type="validator",
            trigger_job_id=trigger_job_id,
            subprofile=f"validator:{source_packet.subprofile}",
            extra_refs=extra_refs,
        )

    def _build_job_packet(self, job: Any, *, packet_type: str) -> PacketBuildResult:
        if packet_type not in _PACKET_TYPES:
            return PacketBuildResult(
                status="invalid_scope",
                packet=None,
                noop_reason=f"unsupported_packet_type:{packet_type}",
                missing_ref_types=[],
            )

        scope = _attr(job, "scope") or _mapping_get(job, "scope")
        if isinstance(scope, Mapping):
            scope = EvidenceScope.from_mapping(scope)
        if not isinstance(scope, EvidenceScope):
            return PacketBuildResult(
                status="invalid_scope",
                packet=None,
                noop_reason="missing_or_invalid_scope",
                missing_ref_types=[],
            )

        job_id = str(_attr(job, "job_id") or _mapping_get(job, "job_id") or "")
        if not job_id:
            return PacketBuildResult(
                status="invalid_scope",
                packet=None,
                noop_reason="missing_trigger_job_id",
                missing_ref_types=[],
            )

        base_watermark = int(
            _attr(job, "manifest_watermark")
            or _mapping_get(job, "manifest_watermark")
            or self._latest_watermark()
        )
        profile = resolve_packet_profile(
            profile_name=(
                _attr(job, "evidence_profile")
                or _mapping_get(job, "evidence_profile")
            ),
            subprofile=(
                _attr(job, "subprofile")
                or _mapping_get(job, "subprofile")
                or _attr(job, "reason")
                or _mapping_get(job, "reason")
            ),
            trigger_type=(
                _attr(job, "trigger_type")
                or _mapping_get(job, "trigger_type")
            ),
        )
        watermark = self._effective_job_watermark(
            job,
            profile=profile,
            base_watermark=base_watermark,
        )

        try:
            view = self.evidence_store.freeze_view(scope, watermark)
        except Exception as exc:
            logger.debug("Evidence packet scope freeze failed", exc_info=True)
            return PacketBuildResult(
                status="invalid_scope",
                packet=None,
                noop_reason=f"freeze_view_failed:{exc}",
                missing_ref_types=[],
            )

        frozen_refs = list(view.refs)
        refs = _dedupe_latest_transcript_generation(frozen_refs)
        pinned_refs = self._load_pinned_refs(
            scope,
            frozen_refs,
            watermark=watermark,
        )
        target_quality_signal_ref_ids = _quality_signal_scope_ref_ids(profile, scope)
        if profile.name == "quality_signal":
            refs = _filter_quality_signal_refs(refs, target_quality_signal_ref_ids)
            pinned_refs = _filter_quality_signal_refs(
                pinned_refs,
                target_quality_signal_ref_ids,
            )
        extra_refs, missing_extra_refs = self._load_extra_refs(job, frozen_refs)
        if missing_extra_refs:
            return PacketBuildResult(
                status="insufficient_evidence",
                packet=None,
                noop_reason="required_extra_refs_missing",
                missing_ref_types=missing_extra_refs,
            )
        selected_refs, omitted_ref_ids = self._select_refs(
            refs,
            scope=scope,
            profile=profile,
        )
        added_chain_ids = (
            self._include_transcript_parent_chains(selected_refs, refs)
            if profile.selection_policy.transcript_window.include_parent_chain
            else set()
        )
        for ref in (*pinned_refs, *extra_refs):
            _add_selected_ref(selected_refs, ref)
        forced_ref_ids = {ref.ref_id for ref in (*pinned_refs, *extra_refs) if ref.ref_id}
        omitted_ref_ids = [
            ref_id
            for ref_id in omitted_ref_ids
            if ref_id not in added_chain_ids and ref_id not in forced_ref_ids
        ]
        selected_refs = {
            key: sorted(value, key=_output_ref_sort_key)
            for key, value in sorted(selected_refs.items())
        }
        readable_paths = self._readable_paths(selected_refs)

        missing_ref_types = self._missing_required(profile, selected_refs)
        missing_ref_types.extend(
            self._missing_required_paths(profile, selected_refs, readable_paths)
        )
        missing_ref_types = sorted(dict.fromkeys(missing_ref_types))
        if missing_ref_types:
            return PacketBuildResult(
                status="insufficient_evidence",
                packet=None,
                noop_reason="required_refs_missing",
                missing_ref_types=missing_ref_types,
            )

        instructions = _packet_instructions(job, profile)
        snippets, budget, redaction_status = self._expand_snippets(
            selected_refs,
            readable_paths=readable_paths,
            profile=profile,
            packet_type=packet_type,
            initial_omitted_refs=omitted_ref_ids,
        )
        if redaction_status == "redaction_failed":
            return PacketBuildResult(
                status="redaction_failed",
                packet=None,
                noop_reason="redaction_failed",
                missing_ref_types=[],
            )

        packet = EvidencePacket(
            packet_id="",
            trigger_job_id=job_id,
            packet_type=packet_type,
            profile_name=profile.name,
            subprofile=profile.subprofile,
            manifest_watermark=watermark,
            scope=scope,
            selected_refs=selected_refs,
            expanded_snippets=snippets,
            readable_paths=readable_paths,
            instructions=instructions,
            budget=budget,
            redaction_status=redaction_status,
            build_status="ok",
            missing_ref_types=[],
        )
        packet_id = _packet_id(packet)
        packet = EvidencePacket(
            packet_id=packet_id,
            trigger_job_id=packet.trigger_job_id,
            packet_type=packet.packet_type,
            profile_name=packet.profile_name,
            subprofile=packet.subprofile,
            manifest_watermark=packet.manifest_watermark,
            scope=packet.scope,
            selected_refs=packet.selected_refs,
            expanded_snippets=packet.expanded_snippets,
            readable_paths=packet.readable_paths,
            instructions=packet.instructions,
            budget=packet.budget,
            redaction_status=packet.redaction_status,
            build_status=packet.build_status,
            missing_ref_types=packet.missing_ref_types,
        )
        self.evidence_store.persist_packet(packet)
        return PacketBuildResult(
            status="ok",
            packet=packet,
            noop_reason=None,
            missing_ref_types=[],
        )

    def _select_refs(
        self,
        refs: list[ResourceRef],
        *,
        scope: EvidenceScope,
        profile: EvidenceProfile,
    ) -> tuple[dict[str, list[ResourceRef]], list[str]]:
        policy = profile.selection_policy
        requested_types = _profile_ref_types(profile)
        type_rank = _type_rank(profile)
        ranked = sorted(
            (
                ref
                for ref in refs
                if ref.ref_type in requested_types
                and ref.ref_type not in set(profile.excluded_ref_types)
                and _memory_ref_allowed(ref)
            ),
            key=lambda ref: (
                type_rank.get(ref.ref_type, 99),
                _RELIABILITY_RANK.get(ref.reliability, 9),
                _ROLE_RANK.get(ref.role, 9),
                _proximity_rank(ref, scope),
                ref.first_seen_watermark or 0,
                ref.ref_id,
            ),
        )
        selected: dict[str, list[ResourceRef]] = {}
        seen: set[str] = set()
        omitted: list[str] = []

        def add_ref(ref: ResourceRef, *, required: bool = False) -> None:
            if ref.ref_id in seen:
                return
            type_limit = _max_refs_for_type(profile, ref.ref_type)
            current = selected.setdefault(ref.ref_type, [])
            if len(current) >= type_limit:
                omitted.append(ref.ref_id)
                return
            if len(seen) >= policy.max_selected_refs and not required:
                omitted.append(ref.ref_id)
                return
            seen.add(ref.ref_id)
            current.append(ref)

        self._select_transcript_windows(
            ranked,
            profile=profile,
            add_ref=add_ref,
        )
        sampled_types = self._select_representative_refs(
            ranked,
            profile=profile,
            add_ref=add_ref,
        )

        for requirement in profile.required_ref_types:
            for ref_type in _requirement_alternatives(requirement):
                candidates = [ref for ref in ranked if ref.ref_type == ref_type]
                if not candidates:
                    continue
                already_selected = bool(selected.get(ref_type))
                if not already_selected:
                    for ref in candidates[: _required_include_count(profile, ref_type)]:
                        add_ref(ref, required=True)
                break

        for ref in ranked:
            if ref.ref_type in sampled_types:
                if ref.ref_id not in seen:
                    omitted.append(ref.ref_id)
                continue
            add_ref(ref)
        selected_ids = {ref.ref_id for refs in selected.values() for ref in refs}
        for ref in ranked:
            if ref.ref_id not in selected_ids:
                omitted.append(ref.ref_id)
        return dict(sorted(selected.items())), sorted(dict.fromkeys(omitted))

    def _select_transcript_windows(
        self,
        ranked: list[ResourceRef],
        *,
        profile: EvidenceProfile,
        add_ref: Any,
    ) -> None:
        window = profile.selection_policy.transcript_window
        if not window.enabled or "transcript_message" not in _profile_ref_types(profile):
            return
        messages = sorted(
            [ref for ref in ranked if ref.ref_type == "transcript_message"],
            key=_transcript_sort_key,
        )
        if not messages:
            return

        anchors: list[tuple[int, int, int]] = []
        instruction_index = _find_user_instruction_index(messages)
        if instruction_index is not None:
            anchors.append(
                (
                    instruction_index,
                    window.user_instruction_before,
                    window.user_instruction_after,
                )
            )

        tool_use_ids = {
            item
            for ref in ranked
            if ref.ref_type in {"tool_event", "tool_result", "tool_incident"}
            for item in _tool_use_ids(ref)
        }
        tool_anchor_indexes: list[int] = []
        if tool_use_ids:
            for index, ref in enumerate(messages):
                if _message_tool_use_ids(ref).intersection(tool_use_ids):
                    tool_anchor_indexes.append(index)
                if len(tool_anchor_indexes) >= window.max_tool_anchors:
                    break
        for index in tool_anchor_indexes:
            anchors.append((index, window.tool_before, window.tool_after))

        final_index = _find_final_assistant_index(messages)
        if final_index is not None:
            anchors.append(
                (
                    final_index,
                    window.final_response_before,
                    window.final_response_after,
                )
            )

        added = 0
        added_ids: set[str] = set()
        for anchor_index, before, after in anchors:
            start = max(0, anchor_index - before)
            end = min(len(messages), anchor_index + after + 1)
            for ref in messages[start:end]:
                if ref.ref_id in added_ids:
                    continue
                if added >= window.max_messages:
                    return
                add_ref(ref)
                added_ids.add(ref.ref_id)
                added += 1

    def _select_representative_refs(
        self,
        ranked: list[ResourceRef],
        *,
        profile: EvidenceProfile,
        add_ref: Any,
    ) -> set[str]:
        sampling = profile.selection_policy.representative_sampling
        if not sampling.enabled:
            return set()

        sampled_types: set[str] = set()
        for ref_type in sampling.ref_types:
            candidates = [ref for ref in ranked if ref.ref_type == ref_type]
            if not candidates:
                continue
            sampled_types.add(ref_type)
            failures = [ref for ref in candidates if not _is_success_ref(ref)]
            success_refs = [ref for ref in candidates if _is_success_ref(ref)]
            sample_candidates = failures or candidates
            grouped: dict[tuple[str, ...], list[ResourceRef]] = {}
            for ref in sample_candidates:
                grouped.setdefault(_representative_signature(ref), []).append(ref)
            ordered_groups = sorted(
                grouped.items(),
                key=lambda item: (
                    _best_ref_rank(item[1]),
                    item[0],
                ),
            )
            for _, group_refs in ordered_groups[: sampling.max_groups]:
                for ref in group_refs[: sampling.max_per_group]:
                    add_ref(ref)
            if sampling.include_success_control and success_refs:
                add_ref(success_refs[0])
        return sampled_types

    def _readable_paths(
        self,
        selected_refs: dict[str, list[ResourceRef]],
    ) -> list[ReadablePathRef]:
        paths: list[ReadablePathRef] = []
        seen: set[tuple[str, str]] = set()
        for refs in selected_refs.values():
            for ref in refs:
                if ref.ref_type not in _PATH_REF_TYPES or not ref.uri:
                    continue
                path_text = _path_from_uri(ref.uri)
                if not path_text:
                    continue
                key = (ref.ref_id, path_text)
                if key in seen:
                    continue
                seen.add(key)
                paths.append(self._readable_path(ref, path_text))
        return sorted(paths, key=lambda item: (item.ref_id, item.path))

    def _readable_path(self, ref: ResourceRef, path_text: str) -> ReadablePathRef:
        path = Path(path_text).expanduser()
        contains_sensitive_path = _is_sensitive_path(path)
        path_secret = bool(ref.contains_secret) or contains_sensitive_path
        max_read_chars = _READABLE_MAX_CHARS
        missing_reason: str | None = None
        readable = False
        original_length: int | None = None
        content_hash = ref.hash

        try:
            exists = path.exists()
        except OSError:
            exists = False
        if not exists:
            missing_reason = "missing"
        elif not path.is_file():
            missing_reason = "not_file"
        elif path_secret:
            missing_reason = "contains_secret"
        elif not self._path_read_allowed(path):
            missing_reason = "outside_allowed_roots"
        elif _file_content_contains_secret(path):
            path_secret = True
            missing_reason = "contains_secret"
        else:
            try:
                stat = path.stat()
                original_length = int(stat.st_size)
                content_hash = content_hash or _file_hash(path)
                readable = True
            except OSError:
                missing_reason = "unreadable"

        if original_length is None:
            try:
                if exists and path.is_file():
                    original_length = int(path.stat().st_size)
            except OSError:
                original_length = None

        return ReadablePathRef(
            ref_id=ref.ref_id,
            path=str(path),
            purpose=_path_purpose(ref),
            readable=readable,
            missing_reason=missing_reason,
            contains_secret=path_secret,
            max_read_chars=max_read_chars,
            original_length=original_length,
            content_hash=content_hash,
        )

    def _path_read_allowed(self, path: Path) -> bool:
        method = getattr(self.evidence_store, "_path_read_allowed", None)
        if callable(method):
            try:
                return bool(method(path))
            except Exception:
                return False
        return True

    def _missing_required(
        self,
        profile: EvidenceProfile,
        selected_refs: dict[str, list[ResourceRef]],
    ) -> list[str]:
        missing: list[str] = []
        for requirement in profile.required_ref_types:
            alternatives = _requirement_alternatives(requirement)
            if not any(selected_refs.get(ref_type) for ref_type in alternatives):
                missing.append(requirement)
        return missing

    def _missing_required_paths(
        self,
        profile: EvidenceProfile,
        selected_refs: dict[str, list[ResourceRef]],
        readable_paths: list[ReadablePathRef],
    ) -> list[str]:
        by_ref_id = {item.ref_id: item for item in readable_paths}
        missing: list[str] = []
        for requirement in profile.required_ref_types:
            for ref_type in _requirement_alternatives(requirement):
                if ref_type not in _REQUIRED_READABLE_PATH_TYPES:
                    continue
                refs = selected_refs.get(ref_type) or []
                if not refs:
                    continue
                if not any(
                    by_ref_id.get(ref.ref_id) is not None
                    and bool(by_ref_id[ref.ref_id].readable)
                    for ref in refs
                ):
                    missing.append(f"{ref_type}:unreadable")
        return missing

    def _include_transcript_parent_chains(
        self,
        selected_refs: dict[str, list[ResourceRef]],
        all_refs: list[ResourceRef],
    ) -> set[str]:
        transcript_refs = [ref for ref in all_refs if ref.ref_type == "transcript_message"]
        if not transcript_refs or not selected_refs.get("transcript_message"):
            return set()
        by_logical_generation: dict[tuple[str, int], ResourceRef] = {}
        by_logical: dict[str, list[ResourceRef]] = {}
        for ref in transcript_refs:
            logical = _logical_message_uuid(ref)
            generation = _transcript_generation(ref)
            by_logical_generation[(logical, generation)] = ref
            by_logical.setdefault(logical, []).append(ref)
        for refs in by_logical.values():
            refs.sort(key=lambda item: (_transcript_generation(item), item.ref_id))

        existing = {ref.ref_id for ref in selected_refs.get("transcript_message", [])}
        added: set[str] = set()
        queue = list(selected_refs.get("transcript_message", []))
        depth = 0
        while queue and depth < 50:
            depth += 1
            ref = queue.pop(0)
            parent_uuid = _parent_message_uuid(ref)
            if not parent_uuid:
                continue
            generation = _transcript_generation(ref)
            parent = by_logical_generation.get((parent_uuid, generation))
            if parent is None:
                candidates = by_logical.get(parent_uuid) or []
                historical = [
                    item
                    for item in candidates
                    if _transcript_generation(item) <= generation
                ]
                parent = historical[-1] if historical else (candidates[-1] if candidates else None)
            if parent is None or parent.ref_id in existing:
                continue
            selected_refs.setdefault("transcript_message", []).append(parent)
            existing.add(parent.ref_id)
            added.add(parent.ref_id)
            queue.append(parent)
        return added

    def _expand_snippets(
        self,
        selected_refs: dict[str, list[ResourceRef]],
        *,
        readable_paths: list[ReadablePathRef],
        profile: EvidenceProfile,
        packet_type: str,
        initial_omitted_refs: list[str] | None = None,
    ) -> tuple[list[EvidenceSnippet], PacketBudget, str]:
        readable_by_ref = {item.ref_id: item for item in readable_paths}
        snippets: list[EvidenceSnippet] = []
        omitted: list[str] = list(initial_omitted_refs or [])
        used = 0
        redaction_status = "clean"
        max_chars = max(0, int(profile.max_chars))
        for ref in _iter_selected_refs(selected_refs):
            raw_text, truncation = _expand_ref_text(
                ref,
                profile=profile,
                packet_type=packet_type,
                readable=readable_by_ref.get(ref.ref_id),
            )
            text = redact_text(raw_text)
            if contains_secret(text):
                return [], PacketBudget(max_chars=max_chars, used_chars=0), "redaction_failed"
            if text != raw_text or ref.contains_secret:
                redaction_status = "redacted"
            if not text:
                continue
            remaining = max_chars - used
            if remaining <= 0:
                omitted.append(ref.ref_id)
                continue
            fitted, fit_truncation = _fit_text(text, remaining)
            if not fitted:
                omitted.append(ref.ref_id)
                continue
            snippets.append(
                EvidenceSnippet(
                    ref_id=ref.ref_id,
                    text=fitted,
                    truncation=fit_truncation if fit_truncation != "none" else truncation,
                )
            )
            used += len(fitted)
            if fit_truncation != "none":
                omitted.append(ref.ref_id)
        budget = PacketBudget(
            max_chars=max_chars,
            used_chars=used,
            omitted_refs=sorted(dict.fromkeys(omitted)),
        )
        return snippets, budget, redaction_status

    def _build_pinned_packet(
        self,
        source_packet: EvidencePacket,
        *,
        packet_type: str,
        trigger_job_id: str,
        subprofile: str,
        extra_refs: list[ResourceRef],
    ) -> PacketBuildResult:
        profile = resolve_packet_profile(
            profile_name=source_packet.profile_name,
            subprofile=subprofile,
            trigger_type=packet_type.upper(),
        )
        selected_refs = {
            ref_type: list(refs)
            for ref_type, refs in source_packet.selected_refs.items()
        }
        for ref in extra_refs:
            if not ref.ref_id:
                continue
            existing_ids = {
                item.ref_id for item in selected_refs.get(ref.ref_type, [])
            }
            if ref.ref_id not in existing_ids:
                selected_refs.setdefault(ref.ref_type, []).append(ref)
        packet_ref = self.evidence_store.get_ref(f"packet:{source_packet.packet_id}")
        if packet_ref is not None:
            selected_refs.setdefault(packet_ref.ref_type, []).append(packet_ref)
        selected_refs = {
            key: sorted(value, key=_output_ref_sort_key)
            for key, value in sorted(selected_refs.items())
        }
        readable_paths = self._readable_paths(selected_refs)
        snippets, budget, redaction_status = self._expand_snippets(
            selected_refs,
            readable_paths=readable_paths,
            profile=profile,
            packet_type=packet_type,
        )
        if redaction_status == "redaction_failed":
            return PacketBuildResult(
                status="redaction_failed",
                packet=None,
                noop_reason="redaction_failed",
                missing_ref_types=[],
            )
        watermark = max(source_packet.manifest_watermark, self._latest_watermark())
        packet = EvidencePacket(
            packet_id="",
            trigger_job_id=str(trigger_job_id),
            packet_type=packet_type,
            profile_name=profile.name,
            subprofile=profile.subprofile,
            manifest_watermark=watermark,
            scope=source_packet.scope,
            selected_refs=selected_refs,
            expanded_snippets=snippets,
            readable_paths=readable_paths,
            instructions=_pinned_packet_instructions(
                source_packet,
                profile,
            ),
            budget=budget,
            redaction_status=redaction_status,
            build_status="ok",
            missing_ref_types=[],
        )
        packet = EvidencePacket(
            packet_id=_packet_id(packet),
            trigger_job_id=packet.trigger_job_id,
            packet_type=packet.packet_type,
            profile_name=packet.profile_name,
            subprofile=packet.subprofile,
            manifest_watermark=packet.manifest_watermark,
            scope=packet.scope,
            selected_refs=packet.selected_refs,
            expanded_snippets=packet.expanded_snippets,
            readable_paths=packet.readable_paths,
            instructions=packet.instructions,
            budget=packet.budget,
            redaction_status=packet.redaction_status,
            build_status=packet.build_status,
            missing_ref_types=packet.missing_ref_types,
        )
        self.evidence_store.persist_packet(packet)
        return PacketBuildResult(
            status="ok",
            packet=packet,
            noop_reason=None,
            missing_ref_types=[],
        )

    def _load_source_packet(self, value: Any) -> EvidencePacket | None:
        packet = _attr(value, "packet") or _mapping_get(value, "packet")
        if isinstance(packet, EvidencePacket):
            return packet
        packet_id = (
            _attr(value, "packet_id")
            or _mapping_get(value, "packet_id")
            or _attr(value, "source_packet_id")
            or _mapping_get(value, "source_packet_id")
        )
        if not packet_id:
            return None
        loader = getattr(self.evidence_store, "load_packet", None)
        if not callable(loader):
            return None
        return loader(str(packet_id))

    def _load_required_refs(
        self,
        required_refs: list[tuple[str, str]],
    ) -> tuple[list[ResourceRef], list[str]]:
        refs: list[ResourceRef] = []
        missing: list[str] = []
        for ref_type, ref_id in required_refs:
            ref = self.evidence_store.get_ref(ref_id)
            if ref is None:
                missing.append(ref_type)
                continue
            refs.append(ref)
        return refs, sorted(dict.fromkeys(missing))

    def _load_extra_refs(
        self,
        job: Any,
        frozen_refs: list[ResourceRef],
    ) -> tuple[list[ResourceRef], list[str]]:
        refs: list[ResourceRef] = []
        missing: list[str] = []
        by_ref_id = {ref.ref_id: ref for ref in frozen_refs if ref.ref_id}
        for ref_id in _str_list(
            _attr(job, "required_extra_ref_ids")
            or _mapping_get(job, "required_extra_ref_ids")
        ):
            ref = by_ref_id.get(ref_id)
            if ref is None:
                missing.append(f"required_extra_ref:{ref_id}")
                continue
            refs.append(ref)
        return refs, sorted(dict.fromkeys(missing))

    def _load_pinned_refs(
        self,
        scope: EvidenceScope,
        frozen_refs: list[ResourceRef],
        *,
        watermark: int,
    ) -> list[ResourceRef]:
        by_ref_id = {ref.ref_id: ref for ref in frozen_refs if ref.ref_id}
        pinned_ids = _stable_str_list(scope.representative_execution_ids)
        loaded_refs: dict[str, ResourceRef] = {}
        scan_index = 0
        while scan_index < len(pinned_ids):
            ref_id = pinned_ids[scan_index]
            ref = self._load_ref_at_watermark(
                ref_id,
                watermark=watermark,
                fallback_refs=by_ref_id,
            )
            scan_index += 1
            if ref is not None:
                loaded_refs[ref_id] = ref
            if ref is None or ref.ref_type != "quality_signal_ref":
                continue
            for backref_id in _stable_str_list(ref.raw_backrefs):
                if backref_id not in pinned_ids:
                    pinned_ids.append(backref_id)
        return [loaded_refs[ref_id] for ref_id in pinned_ids if ref_id in loaded_refs]

    def _load_ref_at_watermark(
        self,
        ref_id: str,
        *,
        watermark: int,
        fallback_refs: dict[str, ResourceRef],
    ) -> ResourceRef | None:
        loader = getattr(self.evidence_store, "get_ref_at", None)
        if callable(loader):
            try:
                return loader(ref_id, watermark)
            except TypeError:
                try:
                    return loader(ref_id=ref_id, watermark=watermark)
                except Exception:
                    logger.debug("Pinned ref load failed: %s", ref_id, exc_info=True)
            except Exception:
                logger.debug("Pinned ref load failed: %s", ref_id, exc_info=True)
        return fallback_refs.get(ref_id)

    def _latest_watermark(self) -> int:
        method = getattr(self.evidence_store, "latest_manifest_watermark", None)
        if callable(method):
            try:
                return int(method())
            except Exception:
                return 0
        method = getattr(self.evidence_store, "_latest_watermark", None)
        if callable(method):
            try:
                return int(method())
            except Exception:
                return 0
        return 0

    def _effective_job_watermark(
        self,
        job: Any,
        *,
        profile: EvidenceProfile,
        base_watermark: int,
    ) -> int:
        if not _should_follow_latest_quality_refs(job, profile):
            return base_watermark
        latest = self._latest_watermark()
        return max(base_watermark, latest)


def _profile_ref_types(profile: EvidenceProfile) -> set[str]:
    result: set[str] = set()
    for item in (
        *profile.required_ref_types,
        *profile.preferred_ref_types,
        *profile.supporting_ref_types,
    ):
        result.update(_requirement_alternatives(item))
    return result


def _quality_signal_scope_ref_ids(
    profile: EvidenceProfile,
    scope: EvidenceScope,
) -> set[str]:
    if profile.name != "quality_signal":
        return set()
    return {
        item
        for item in _stable_str_list(scope.representative_execution_ids)
        if item.startswith("quality_signal:")
    }


def _filter_quality_signal_refs(
    refs: list[ResourceRef],
    target_ref_ids: set[str],
) -> list[ResourceRef]:
    return [
        ref
        for ref in refs
        if ref.ref_type != "quality_signal_ref" or ref.ref_id in target_ref_ids
    ]


def _type_rank(profile: EvidenceProfile) -> dict[str, int]:
    result: dict[str, int] = {}
    for rank, group in enumerate(
        (
            profile.required_ref_types,
            profile.preferred_ref_types,
            profile.supporting_ref_types,
        )
    ):
        for item in group:
            for ref_type in _requirement_alternatives(item):
                result.setdefault(ref_type, rank)
    return result


def _requirement_alternatives(requirement: str) -> tuple[str, ...]:
    return tuple(
        part.strip()
        for part in str(requirement).split("|")
        if part.strip()
    )


def _max_refs_for_type(profile: EvidenceProfile, ref_type: str) -> int:
    policy = profile.selection_policy
    return int(
        policy.max_refs_per_type.get(ref_type, policy.default_max_refs_per_type)
    )


def _required_include_count(profile: EvidenceProfile, ref_type: str) -> int:
    return int(profile.selection_policy.required_include_count.get(ref_type, 1))


def _best_ref_rank(refs: list[ResourceRef]) -> tuple[int, int, int, str]:
    best = sorted(
        refs,
        key=lambda ref: (
            _RELIABILITY_RANK.get(ref.reliability, 9),
            _ROLE_RANK.get(ref.role, 9),
            ref.first_seen_watermark or 0,
            ref.ref_id,
        ),
    )[0]
    return (
        _RELIABILITY_RANK.get(best.reliability, 9),
        _ROLE_RANK.get(best.role, 9),
        best.first_seen_watermark or 0,
        best.ref_id,
    )


def _output_ref_sort_key(ref: ResourceRef) -> tuple[Any, ...]:
    if ref.ref_type == "transcript_message":
        return _transcript_sort_key(ref)
    return (ref.ref_id,)


def _transcript_sort_key(ref: ResourceRef) -> tuple[int, int, str, str]:
    metadata = ref.metadata
    order = _first_int(
        metadata,
        "message_index",
        "sequence",
        "seq",
        "index",
        "turn_index",
    )
    if order is None:
        order = ref.first_seen_watermark or 0
    generation = _transcript_generation(ref)
    created_at = str(ref.created_at or metadata.get("created_at") or "")
    return (generation, order, created_at, ref.ref_id)


def _first_int(metadata: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = metadata.get(key)
        try:
            if value is not None and str(value) != "":
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _find_user_instruction_index(messages: list[ResourceRef]) -> int | None:
    for index, ref in enumerate(messages):
        metadata = ref.metadata
        if str(metadata.get("role") or "").lower() != "user":
            continue
        if (
            metadata.get("is_user_instruction")
            or metadata.get("message_kind") in {"user_instruction", "instruction"}
            or metadata.get("source") == "user_instruction"
        ):
            return index
    for index, ref in enumerate(messages):
        if str(ref.metadata.get("role") or "").lower() == "user":
            return index
    return None


def _find_final_assistant_index(messages: list[ResourceRef]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        ref = messages[index]
        metadata = ref.metadata
        if str(metadata.get("role") or "").lower() != "assistant":
            continue
        if (
            metadata.get("is_final_response")
            or metadata.get("final_response")
            or metadata.get("message_kind") in {"final_response", "final"}
        ):
            return index
    for index in range(len(messages) - 1, -1, -1):
        if str(messages[index].metadata.get("role") or "").lower() == "assistant":
            return index
    return None


def _tool_use_ids(ref: ResourceRef) -> set[str]:
    metadata = ref.metadata
    values = _metadata_values(
        metadata,
        "tool_use_id",
        "tool_use_ids",
        "tool_call_id",
        "tool_call_ids",
        "call_id",
    )
    if ref.ref_type == "tool_result" and ref.raw_backrefs:
        values.update(ref.raw_backrefs)
    return values


def _message_tool_use_ids(ref: ResourceRef) -> set[str]:
    return _metadata_values(
        ref.metadata,
        "tool_use_id",
        "tool_use_ids",
        "tool_call_id",
        "tool_call_ids",
        "call_id",
    )


def _is_success_ref(ref: ResourceRef) -> bool:
    metadata = ref.metadata
    status = str(
        metadata.get("status")
        or metadata.get("outcome")
        or metadata.get("result")
        or ""
    ).lower()
    return status in {"success", "ok", "passed"} and not metadata.get("error_type")


def _representative_signature(ref: ResourceRef) -> tuple[str, ...]:
    metadata = ref.metadata
    if ref.ref_type == "evolution_candidate_ref":
        return (
            ref.ref_type,
            _metadata_text(metadata, "target_skill_id", "skill_id"),
            _metadata_text(metadata, "proposed_action", "action"),
            _metadata_text(metadata, "reason_code", "failure_mode", "trigger_reason"),
            _metadata_text(metadata, "affected_tool_key", "tool_key"),
        )
    if ref.ref_type in {"tool_event", "tool_result", "tool_incident"}:
        return (
            ref.ref_type,
            _metadata_text(metadata, "tool_key", "tool_name"),
            _metadata_text(metadata, "status", "outcome"),
            _metadata_text(metadata, "error_type", "exception_type"),
            _metadata_text(
                metadata,
                "failure_mode",
                "error_bucket",
                "reason_code",
                "error_message",
            ),
            _metadata_text(metadata, "error_code", "exit_code", "normalized_error_code"),
        )
    return (
        ref.ref_type,
        _metadata_text(metadata, "skill_id", "target_skill_id"),
        _metadata_text(metadata, "reason_code", "failure_mode", "status"),
    )


def _metadata_text(metadata: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value) != "":
            return str(value)
    return "-"


def _proximity_rank(ref: ResourceRef, scope: EvidenceScope) -> tuple[int, int, int]:
    task_rank = 5
    source_tasks = {item for item in scope.source_task_ids if item}
    if scope.task_id and ref.task_id == scope.task_id:
        task_rank = 0
    elif scope.task_id and ref.parent_task_id == scope.task_id:
        task_rank = 1
    elif scope.task_id and ref.metadata.get("parent_task_id") == scope.task_id:
        task_rank = 2
    elif ref.task_id and ref.task_id in source_tasks:
        task_rank = 3
    elif not scope.task_id and not source_tasks:
        task_rank = 4

    skill_rank = 0
    if scope.skill_ids:
        skill_values = _metadata_values(ref.metadata, "skill_id", "skill_ids")
        skill_rank = 0 if skill_values.intersection(scope.skill_ids) else 1

    tool_rank = 0
    if scope.tool_keys:
        tool_values = _metadata_values(ref.metadata, "tool_key", "tool_keys")
        tool_rank = 0 if tool_values.intersection(scope.tool_keys) else 1

    return (task_rank, skill_rank, tool_rank)


def _metadata_values(metadata: Mapping[str, Any], *keys: str) -> set[str]:
    values: set[str] = set()
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value:
            values.add(value)
        elif isinstance(value, (list, tuple, set)):
            values.update(str(item) for item in value if str(item))
    return values


def _dedupe_latest_transcript_generation(refs: Iterable[ResourceRef]) -> list[ResourceRef]:
    non_transcript: list[ResourceRef] = []
    grouped: dict[str, ResourceRef] = {}
    for ref in refs:
        if ref.ref_type != "transcript_message":
            non_transcript.append(ref)
            continue
        logical_uuid = _logical_message_uuid(ref)
        current = grouped.get(logical_uuid)
        if current is None or _transcript_generation(ref) > _transcript_generation(current):
            grouped[logical_uuid] = ref
    return [*non_transcript, *grouped.values()]


def _transcript_generation(ref: ResourceRef) -> int:
    try:
        return int(ref.metadata.get("transcript_generation") or 0)
    except (TypeError, ValueError):
        return 0


def _logical_message_uuid(ref: ResourceRef) -> str:
    return str(
        ref.metadata.get("logical_message_uuid")
        or ref.metadata.get("message_uuid")
        or ref.ref_id
    )


def _parent_message_uuid(ref: ResourceRef) -> str:
    return str(
        ref.metadata.get("logical_parent_uuid")
        or ref.metadata.get("parent_uuid")
        or ""
    )


def _memory_ref_allowed(ref: ResourceRef) -> bool:
    if ref.ref_type != "memory_ref":
        return True
    metadata = ref.metadata
    return bool(
        metadata.get("loaded_in_context")
        or metadata.get("read_or_written_by_tool")
        or metadata.get("source_event") == "transcript_attachment"
        or metadata.get("memory_event_type") in {"memory_read", "memory_written"}
    )


def _iter_selected_refs(selected_refs: dict[str, list[ResourceRef]]) -> Iterable[ResourceRef]:
    for ref_type in sorted(selected_refs):
        yield from selected_refs[ref_type]


def _expand_ref_text(
    ref: ResourceRef,
    *,
    profile: EvidenceProfile,
    packet_type: str,
    readable: ReadablePathRef | None,
) -> tuple[str, str]:
    if ref.ref_type == "runtime_snapshot":
        return _runtime_snapshot_text(ref), "none"
    if ref.ref_type == "transcript_message":
        return _transcript_message_text(ref), "none"
    if ref.ref_type == "tool_event":
        return _tool_event_text(ref), "none"
    if ref.ref_type == "tool_result":
        return _tool_result_text(ref, readable), "preview_only"
    if ref.ref_type == "skill_file":
        return _skill_file_text(ref, packet_type=packet_type, readable=readable), (
            "head" if packet_type in {"action", "validator"} else "preview_only"
        )
    if ref.ref_type == "compact_summary":
        return _compact_summary_text(ref), "none"
    if ref.ref_type == "recording_ref":
        return _path_context_text(ref, "recording fallback"), "preview_only"
    if ref.ref_type == "memory_ref":
        return _memory_ref_text(ref), "preview_only"
    if ref.ref_type == "manual_request_ref":
        return _manual_request_text(ref), "none"
    if ref.ref_type in {"tool_quality_record", "tool_incident"}:
        return _metadata_summary_text(ref), "none"
    if ref.ref_type == "quality_signal_ref":
        return _quality_signal_text(ref), "none"
    if ref.ref_type in {"evolution_candidate_ref", "decision_rationale_ref"}:
        return _metadata_summary_text(ref), "none"
    preview = str(ref.preview or "")[:_GENERIC_PREVIEW_CHARS]
    if not preview:
        preview = json.dumps(ref.metadata, ensure_ascii=False, sort_keys=True, default=str)
    return f"{ref.ref_type} {ref.ref_id}\n{preview[:_GENERIC_PREVIEW_CHARS]}", "none"


def _runtime_snapshot_text(ref: ResourceRef) -> str:
    metadata = ref.metadata
    fields = {
        "status": metadata.get("status"),
        "instruction_preview": metadata.get("instruction_preview"),
        "stop_reason": metadata.get("stop_reason"),
        "iterations": metadata.get("iterations"),
        "active_skills": metadata.get("active_skills"),
        "tool_execution_count": metadata.get("tool_execution_count"),
        "session_persisted": metadata.get("session_persisted"),
        "final_response_preview": metadata.get("final_response_preview"),
    }
    return _section(ref, fields)


def _transcript_message_text(ref: ResourceRef) -> str:
    metadata = ref.metadata
    fields = {
        "role": metadata.get("role"),
        "generation": metadata.get("transcript_generation"),
        "logical_message_uuid": metadata.get("logical_message_uuid"),
        "parent_uuid": metadata.get("parent_uuid"),
        "logical_parent_uuid": metadata.get("logical_parent_uuid"),
        "rewrite_marker": metadata.get("rewrite_marker"),
        "tool_name": metadata.get("tool_name"),
        "tool_call_id": metadata.get("tool_call_id"),
        "preview": ref.preview,
    }
    return _section(ref, fields)


def _tool_event_text(ref: ResourceRef) -> str:
    metadata = ref.metadata
    fields = {
        "tool_key": metadata.get("tool_key"),
        "tool_name": metadata.get("tool_name"),
        "tool_use_id": metadata.get("tool_use_id"),
        "status": metadata.get("status"),
        "current_iteration": metadata.get("current_iteration"),
        "execution_time_ms": metadata.get("execution_time_ms"),
        "total_duration_ms": metadata.get("total_duration_ms"),
        "input_preview": metadata.get("input_preview"),
        "error_type": metadata.get("error_type"),
        "result_preview": metadata.get("result_preview") or ref.preview,
    }
    return _section(ref, fields)


def _tool_result_text(ref: ResourceRef, readable: ReadablePathRef | None) -> str:
    metadata = ref.metadata
    preview = str(ref.preview or "")[:_TOOL_RESULT_PREVIEW_CHARS]
    fields = {
        "tool_key": metadata.get("tool_key"),
        "tool_name": metadata.get("tool_name"),
        "tool_use_id": metadata.get("tool_use_id"),
        "original_length": metadata.get("original_length"),
        "persistence_source": metadata.get("persistence_source"),
        "missing": metadata.get("missing"),
        "readable_path": readable.path if readable is not None else _path_from_uri(ref.uri),
        "path_readable": readable.readable if readable is not None else None,
        "preview": preview,
    }
    return _section(ref, fields)


def _skill_file_text(
    ref: ResourceRef,
    *,
    packet_type: str,
    readable: ReadablePathRef | None,
) -> str:
    path_text = _path_from_uri(ref.uri)
    content = ""
    if (
        packet_type in {"action", "validator"}
        and readable is not None
        and readable.readable
        and path_text
    ):
        try:
            path = Path(path_text).expanduser()
            if path.is_file() and not _is_sensitive_path(path):
                content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            content = ""
    if not content:
        content = str(ref.preview or "")
    content = _frontmatter_and_preview(
        content,
        max_chars=(
            _SKILL_ACTION_MAX_CHARS
            if packet_type in {"action", "validator"}
            else _SKILL_ANALYSIS_PREVIEW_CHARS
        ),
    )
    fields = {
        "skill_id": ref.metadata.get("skill_id"),
        "path": path_text or ref.metadata.get("path"),
        "content": content,
    }
    return _section(ref, fields)


def _compact_summary_text(ref: ResourceRef) -> str:
    fields = {
        "summary": ref.preview,
        "raw_backrefs": ref.raw_backrefs,
        "missing_raw_backrefs": ref.metadata.get("missing_raw_backrefs"),
        "segment_ref_id": ref.metadata.get("segment_ref_id"),
    }
    return _section(ref, fields)


def _path_context_text(ref: ResourceRef, label: str) -> str:
    fields = {
        "label": label,
        "path": _path_from_uri(ref.uri),
        "reliability": ref.reliability,
        "preview": ref.preview,
    }
    return _section(ref, fields)


def _memory_ref_text(ref: ResourceRef) -> str:
    fields = {
        "path": _path_from_uri(ref.uri),
        "memory_kind": ref.metadata.get("memory_kind"),
        "source_event": ref.metadata.get("source_event"),
        "memory_event_type": ref.metadata.get("memory_event_type"),
        "loaded_in_context": ref.metadata.get("loaded_in_context"),
        "read_or_written_by_tool": ref.metadata.get("read_or_written_by_tool"),
        "reason": ref.preview,
    }
    return _section(ref, fields)


def _manual_request_text(ref: ResourceRef) -> str:
    fields = {
        "action": ref.metadata.get("action"),
        "reason": ref.metadata.get("reason"),
        "request_id": ref.metadata.get("request_id"),
        "skill_ids": ref.metadata.get("skill_ids"),
        "tool_keys": ref.metadata.get("tool_keys"),
        "preview": ref.preview,
    }
    return _section(ref, fields)


def _metadata_summary_text(ref: ResourceRef) -> str:
    return _section(
        ref,
        {
            "preview": ref.preview,
            "metadata": ref.metadata,
            "raw_backrefs": ref.raw_backrefs,
        },
    )


def _quality_signal_text(ref: ResourceRef) -> str:
    metadata = ref.metadata
    fields = {
        "signal_type": metadata.get("signal_type"),
        "subject_type": metadata.get("subject_type"),
        "subject_id": metadata.get("subject_id"),
        "actionability": metadata.get("actionability"),
        "evidence_status": metadata.get("evidence_status"),
        "policy_reason": metadata.get("policy_reason"),
        "summary": metadata.get("summary") or ref.preview,
        "tool_key": metadata.get("tool_key"),
        "tool_use_id": metadata.get("tool_use_id"),
        "skill_id": metadata.get("skill_id"),
        "skill_version": metadata.get("skill_version"),
        "source_watermark": metadata.get("source_watermark"),
        "signal_write_watermark": metadata.get("signal_write_watermark"),
        "raw_backrefs": ref.raw_backrefs,
        "missing_refs": metadata.get("missing_refs"),
    }
    return _section(ref, fields)


def _section(ref: ResourceRef, fields: Mapping[str, Any]) -> str:
    lines = [
        f"ref_id: {ref.ref_id}",
        f"ref_type: {ref.ref_type}",
        f"role: {ref.role}",
        f"reliability: {ref.reliability}",
    ]
    for key, value in fields.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _frontmatter_and_preview(text: str, *, max_chars: int) -> str:
    stripped = str(text or "")
    if not stripped:
        return ""
    if stripped.startswith("---"):
        end = stripped.find("\n---", 3)
        if end != -1:
            frontmatter = stripped[: end + 4].strip()
            rest = stripped[end + 4 :].strip()
            preview = rest[: max(0, max_chars - len(frontmatter) - 2)]
            return f"{frontmatter}\n\n{preview}".strip()
    return stripped[:max_chars]


def _fit_text(text: str, remaining: int) -> tuple[str, str]:
    if remaining <= 0:
        return "", "head"
    if len(text) <= remaining:
        return text, "none"
    if remaining < 80:
        return "", "head"
    suffix = "\n... [packet budget truncated] ..."
    keep = max(0, remaining - len(suffix))
    return f"{text[:keep]}{suffix}", "head"


def _packet_instructions(job: Any, profile: EvidenceProfile) -> dict[str, str]:
    instructions = dict(profile.instructions)
    profile_fallback = bool(
        _attr(job, "profile_fallback") or _mapping_get(job, "profile_fallback")
    )
    instructions["profile"] = f"{profile.name}/{profile.subprofile}"
    instructions["profile_fallback"] = "true" if profile_fallback else "false"
    instructions["packet_scope"] = (
        "Only selected refs and readable_paths are in scope for downstream "
        "analysis, authoring, and validation."
    )
    return instructions


def _pinned_packet_instructions(
    source_packet: EvidencePacket,
    profile: EvidenceProfile,
) -> dict[str, str]:
    instructions = dict(source_packet.instructions)
    for key, value in profile.instructions.items():
        instructions.setdefault(key, value)

    instructions["source_packet_id"] = source_packet.packet_id
    instructions["source_profile"] = (
        f"{source_packet.profile_name}/{source_packet.subprofile}"
    )
    instructions["profile"] = f"{profile.name}/{profile.subprofile}"
    instructions["packet_scope"] = (
        "This packet is pinned to the source packet plus explicit derived refs; "
        "it does not rescan the manifest."
    )

    return instructions


def _packet_id(packet: EvidencePacket) -> str:
    payload = packet.to_dict()
    payload["packet_id"] = ""
    return f"pkt_{_digest(payload)}"


def _path_from_uri(uri: str | None) -> str:
    if not uri:
        return ""
    return str(uri).split("#", 1)[0]


def _path_purpose(ref: ResourceRef) -> str:
    if ref.ref_type == "tool_result":
        return "full_tool_output"
    if ref.ref_type == "skill_file":
        return "skill_source"
    if ref.ref_type == "transcript_segment":
        return "transcript_segment"
    if ref.ref_type == "memory_ref":
        return "memory_context"
    if ref.ref_type == "recording_ref":
        return "recording_fallback"
    return ref.ref_type


def _is_sensitive_path(path: Path) -> bool:
    name = path.name.lower()
    return name == ".env" or name.startswith(".env.") or name.endswith(".env")


def _file_hash(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _file_content_contains_secret(path: Path, *, chunk_bytes: int = 64 * 1024) -> bool:
    tail = ""
    try:
        with path.open("rb") as handle:
            while True:
                data = handle.read(chunk_bytes)
                if not data:
                    break
                text = tail + data.decode("utf-8", errors="replace")
                if contains_secret(text):
                    return True
                tail = text[-512:]
    except OSError:
        return False
    return False


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _attr(value: Any, name: str) -> Any:
    return getattr(value, name, None)


def _mapping_get(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else None


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return []


def _stable_str_list(value: Any) -> list[str]:
    return list(dict.fromkeys(_str_list(value)))


def _should_follow_latest_quality_refs(job: Any, profile: EvidenceProfile) -> bool:
    if profile.name != "analysis_current_task" or profile.subprofile != "task_finished":
        return False
    trigger_type = str(
        _attr(job, "trigger_type") or _mapping_get(job, "trigger_type") or ""
    ).strip().upper()
    reason = str(_attr(job, "reason") or _mapping_get(job, "reason") or "").strip()
    return trigger_type == "ANALYSIS" and reason == "task_finished"


def _add_selected_ref(
    selected_refs: dict[str, list[ResourceRef]],
    ref: ResourceRef,
) -> None:
    if not ref.ref_id:
        return
    existing_ids = {item.ref_id for item in selected_refs.get(ref.ref_type, [])}
    if ref.ref_id not in existing_ids:
        selected_refs.setdefault(ref.ref_type, []).append(ref)
