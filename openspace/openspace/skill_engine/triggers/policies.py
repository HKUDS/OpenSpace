"""Rule-based TriggerJob policies."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Protocol

from openspace.skill_engine.evidence import EvidenceEvent, EvidenceScope, EvidenceStore

from .types import ManualTriggerRequest, TriggerJobSpec


class TriggerPolicy(Protocol):
    trigger_type: str

    def on_event(
        self,
        event: EvidenceEvent,
        manifest_watermark: int,
    ) -> list[TriggerJobSpec]: ...

    def evaluate_checkpoint(
        self,
        name: str,
        scope: EvidenceScope,
        manifest_watermark: int,
    ) -> list[TriggerJobSpec]: ...

    def evaluate_window(
        self,
        now: datetime,
        manifest_watermark: int,
    ) -> list[TriggerJobSpec]: ...

    def from_manual_request(
        self,
        request: ManualTriggerRequest,
        manifest_watermark: int,
    ) -> list[TriggerJobSpec]: ...


class AnalysisTriggerPolicy:
    trigger_type = "ANALYSIS"

    def __init__(self, evidence_store: EvidenceStore | None = None) -> None:
        self._evidence_store = evidence_store

    def on_event(
        self,
        event: EvidenceEvent,
        manifest_watermark: int,
    ) -> list[TriggerJobSpec]:
        return []

    def evaluate_checkpoint(
        self,
        name: str,
        scope: EvidenceScope,
        manifest_watermark: int,
    ) -> list[TriggerJobSpec]:
        if name != "task_session_persisted":
            return []
        if not scope.session_id or not scope.task_id:
            return []

        source_task_ids = _source_task_ids_for_task_tree(
            self._evidence_store,
            scope,
            manifest_watermark,
        )
        analysis_scope = EvidenceScope(
            session_id=scope.session_id,
            task_id=scope.task_id,
            turn_range=scope.turn_range,
            skill_ids=scope.skill_ids,
            tool_keys=scope.tool_keys,
            source_task_ids=source_task_ids,
            representative_execution_ids=scope.representative_execution_ids,
            time_window=scope.time_window,
            agent_ids=(),  # empty means no agent restriction
        )
        profile = resolve_profile("ANALYSIS", "task_finished")
        return [
            TriggerJobSpec(
                trigger_type="ANALYSIS",
                reason="task_finished",
                reason_tags=["checkpoint:task_session_persisted"],
                scope=analysis_scope,
                evidence_profile=profile.evidence_profile,
                subprofile=profile.subprofile,
                profile_fallback=profile.profile_fallback,
                idempotency_key=(
                    "analysis:task_finished:"
                    f"{scope.session_id}:{scope.task_id}"
                ),
            )
        ]

    def evaluate_window(
        self,
        now: datetime,
        manifest_watermark: int,
    ) -> list[TriggerJobSpec]:
        return []

    def from_manual_request(
        self,
        request: ManualTriggerRequest,
        manifest_watermark: int,
    ) -> list[TriggerJobSpec]:
        return []


class ManualTriggerPolicy:
    trigger_type = "MANUAL"

    def on_event(
        self,
        event: EvidenceEvent,
        manifest_watermark: int,
    ) -> list[TriggerJobSpec]:
        return []

    def evaluate_checkpoint(
        self,
        name: str,
        scope: EvidenceScope,
        manifest_watermark: int,
    ) -> list[TriggerJobSpec]:
        return []

    def evaluate_window(
        self,
        now: datetime,
        manifest_watermark: int,
    ) -> list[TriggerJobSpec]:
        return []

    def from_manual_request(
        self,
        request: ManualTriggerRequest,
        manifest_watermark: int,
    ) -> list[TriggerJobSpec]:
        action = request.action.strip().lower()
        if not action:
            return []
        if action in {"fix", "derive"} and not request.skill_ids:
            return []
        reason = "manual_reanalysis" if action == "analyze" else request.reason
        profile = manual_profile_for_action(action, reason)
        scope = EvidenceScope(
            session_id=request.session_id,
            task_id=request.task_id,
            skill_ids=request.skill_ids,
            tool_keys=request.tool_keys,
            source_task_ids=request.source_task_ids,
            representative_execution_ids=request.representative_execution_ids,
            agent_ids=request.agent_ids,
        )
        request_key = request.request_id or _digest(request.to_dict())
        return [
            TriggerJobSpec(
                trigger_type="MANUAL",
                reason=reason,
                reason_tags=[f"manual_action:{action}"],
                scope=scope,
                evidence_profile=profile.evidence_profile,
                subprofile=profile.subprofile,
                profile_fallback=profile.profile_fallback,
                idempotency_key=f"manual:{action}:{request_key}",
            )
        ]


class _Profile:
    def __init__(
        self,
        evidence_profile: str,
        subprofile: str,
        profile_fallback: bool,
    ) -> None:
        self.evidence_profile = evidence_profile
        self.subprofile = subprofile
        self.profile_fallback = profile_fallback


def resolve_profile(trigger_type: str, reason: str) -> _Profile:
    known: dict[tuple[str, str], tuple[str, str]] = {
        ("ANALYSIS", "task_finished"): ("analysis_current_task", "task_finished"),
        ("QUALITY_SIGNAL", "tool_failure_affects_skill"): ("quality_signal", "tool_failure_affects_skill"),
        ("QUALITY_SIGNAL", "tool_semantic_issue"): ("quality_signal", "tool_semantic_issue"),
    }
    if (trigger_type, reason) in known:
        profile, subprofile = known[(trigger_type, reason)]
        return _Profile(profile, subprofile, False)
    defaults = {
        "ANALYSIS": "analysis_current_task",
        "QUALITY_SIGNAL": "quality_signal",
        "MANUAL": "manual",
    }
    return _Profile(defaults.get(trigger_type, "unknown"), "default", True)


def manual_profile_for_action(action: str, reason: str) -> _Profile:
    if action == "capture":
        return _Profile("manual_capture", "capture", False)
    if action in {"fix", "derive"}:
        return _Profile("manual_fix_or_derive", action, False)
    if action == "analyze":
        return _Profile("analysis_current_task", "manual_reanalysis", False)
    return resolve_profile("MANUAL", reason)


def default_policies(evidence_store: EvidenceStore | None = None) -> list[TriggerPolicy]:
    return [
        AnalysisTriggerPolicy(evidence_store),
        ManualTriggerPolicy(),
    ]


def _source_task_ids_for_task_tree(
    evidence_store: EvidenceStore | None,
    scope: EvidenceScope,
    manifest_watermark: int,
) -> tuple[str, ...]:
    ids = {item for item in scope.source_task_ids if item}
    if scope.task_id:
        ids.add(scope.task_id)
    if evidence_store is None or not scope.task_id:
        return tuple(sorted(ids))
    try:
        refs = evidence_store.query_refs(
            EvidenceScope(session_id=scope.session_id),
            watermark=manifest_watermark,
        )
    except Exception:
        return tuple(sorted(ids))
    changed = True
    while changed:
        changed = False
        for ref in refs:
            metadata_parent = ref.metadata.get("parent_task_id")
            if not ref.task_id:
                continue
            if (
                ref.task_id in ids
                or ref.parent_task_id in ids
                or metadata_parent in ids
            ) and ref.task_id not in ids:
                ids.add(ref.task_id)
                changed = True
    return tuple(sorted(ids))


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]
