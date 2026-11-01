"""Rule-based admission gates for evidence-backed skill evolution."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from openspace.skill_engine.capture_contract import (
    capture_contract_failures,
    capture_contract_ref_ids,
)
from openspace.skill_engine.evidence import EvidencePacket, ResourceRef
from openspace.skill_engine.signals.types import (
    STATUS_ACTIONABLE_PARTIAL,
    STATUS_AGGREGATE_ONLY,
    TRIGGERABLE_EVIDENCE_STATUSES,
)
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

_OUTCOMES = {"direct", "candidate", "reject", "noop", "needs_human_review"}
_ENVIRONMENT_FAILURE_TERMS = (
    "api key",
    "apikey",
    "openai_api_key",
    "anthropic_api_key",
    "missing key",
    "unauthorized",
    "authentication",
    "auth failed",
    "401",
    "403",
    "network outage",
    "network error",
    "dns",
    "connection refused",
    "connection reset",
    "service unavailable",
    "external service",
    "sandbox",
    "permission mode",
)
_EPHEMERAL_CAPTURE_TERMS = (
    "api key",
    "apikey",
    "_api_key",
    "secret",
    "token",
    "password",
    "credential",
    "one-time",
    "one time",
    "single-use",
    "temporary url",
    "signed url",
    "presigned",
    "session-specific",
    "temporary environment",
    "/tmp/",
    "/private/tmp",
)
_SCRATCHPAD_EPHEMERAL_CAPTURE_TERMS = (
    "/tmp/",
    "/private/tmp",
)
_CONTEXT_EPHEMERAL_CAPTURE_TERMS = tuple(
    term
    for term in _EPHEMERAL_CAPTURE_TERMS
    if term not in _SCRATCHPAD_EPHEMERAL_CAPTURE_TERMS
)
_DERIVED_DIVERGENCE_TERMS = (
    "divergence",
    "diverge",
    "subscenario",
    "sub-scenario",
    "specialize",
    "specialized",
    "stable sub",
    "scope too broad",
    "too broad",
    "too wide",
    "different tool",
    "different tools",
    "tool combination",
    "cannot stay clear",
    "separate workflow",
)
_BUGFIX_TERMS = ("bug", "fix", "broken", "failure", "failed", "repair")
_PERMISSION_BYPASS_TERMS = ("bypass", "circumvent", "evade")
_PERMISSION_CONTROL_TERMS = (
    "permission",
    "approval",
    "authorization",
    "access control",
    "security control",
)
_QUALITY_SIGNAL_EXTERNAL_ATTRIBUTIONS = {
    "attribution:permission",
    "attribution:environment",
    "attribution:tool_external",
}


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    admission_id: str
    decision_id: str
    packet_id: str
    outcome: str
    hard_failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    required_refs_checked: list[str] = field(default_factory=list)
    source_validation_passed: bool = False
    reviewed_by: str = "rule"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AdmissionResult":
        outcome = str(data.get("outcome") or "reject").strip().lower()
        if outcome not in _OUTCOMES:
            outcome = "reject"
        return cls(
            admission_id=str(data.get("admission_id") or ""),
            decision_id=str(data.get("decision_id") or ""),
            packet_id=str(data.get("packet_id") or ""),
            outcome=outcome,
            hard_failures=_str_list(data.get("hard_failures")),
            warnings=_str_list(data.get("warnings")),
            required_refs_checked=_str_list(data.get("required_refs_checked")),
            source_validation_passed=bool(
                data.get("source_validation_passed", False)
            ),
            reviewed_by=str(data.get("reviewed_by") or "rule"),
            created_at=str(data.get("created_at") or ""),
        )


class EvolutionAdmission:
    """Deterministic hard gate between decision proposals and authoring."""

    def __init__(
        self,
        *,
        evidence_store: Any | None = None,
        skill_store: Any | None = None,
        registry: Any | None = None,
        allow_single_observation_capture: bool = True,
    ) -> None:
        self.evidence_store = evidence_store
        self.skill_store = skill_store
        self.registry = registry
        self.allow_single_observation_capture = bool(
            allow_single_observation_capture
        )

    def admit(
        self,
        decision: Any,
        packet: EvidencePacket,
        job: Any | None = None,
    ) -> AdmissionResult:
        try:
            result = self._admit(decision, packet, job)
        except Exception as exc:
            logger.debug("Evolution admission failed", exc_info=True)
            result = self._result(
                decision,
                packet,
                outcome="reject",
                hard_failures=["admission_error"],
                warnings=[str(exc)[:500]],
                required_refs_checked=_claim_ref_ids(decision),
            )
        self._persist(result)
        return result

    def _admit(
        self,
        decision: Any,
        packet: EvidencePacket,
        job: Any | None = None,
    ) -> AdmissionResult:
        action = _action(decision)
        checked = _claim_ref_ids(decision)
        hard_failures = self._base_hard_failures(decision, packet, job)
        warnings: list[str] = []

        if action == "NOOP":
            warnings.extend(_str_list(_attr(decision, "reason_tags")))
            noop_reason = _none_or_str(_attr(decision, "noop_reason"))
            if noop_reason:
                warnings.append(noop_reason)
            return self._result(
                decision,
                packet,
                outcome="noop",
                hard_failures=hard_failures,
                warnings=warnings,
                required_refs_checked=checked,
            )

        if _candidate_policy(decision) == "reject":
            hard_failures.append("decision_policy_reject")
            hard_failures.extend(_str_list(_attr(decision, "risks")))

        if hard_failures:
            return self._result(
                decision,
                packet,
                outcome="reject",
                hard_failures=hard_failures,
                warnings=warnings,
                required_refs_checked=checked,
            )

        if action == "FIX":
            return self._admit_fix(decision, packet, checked, job=job)
        if action == "DERIVED":
            return self._admit_derived(decision, packet, checked)
        if action == "CAPTURED":
            return self._admit_captured(decision, packet, checked)

        return self._result(
            decision,
            packet,
            outcome="reject",
            hard_failures=["unsupported_action"],
            required_refs_checked=checked,
        )

    def _base_hard_failures(
        self,
        decision: Any,
        packet: EvidencePacket,
        job: Any | None,
    ) -> list[str]:
        failures: list[str] = []
        claims = list(_attr(decision, "evidence_claims") or [])
        if not claims:
            failures.append("no_evidence_claims")
        valid_refs = _packet_ref_ids(packet)
        missing_refs: list[str] = []
        for index, claim in enumerate(claims):
            refs = _str_list(_attr(claim, "refs"))
            if not refs:
                failures.append(f"claim_{index}_missing_refs")
                continue
            missing_refs.extend(ref_id for ref_id in refs if ref_id not in valid_refs)
        if missing_refs:
            failures.append("missing_refs")
            failures.extend(f"missing_ref:{ref_id}" for ref_id in missing_refs)
        failures.extend(_quality_signal_packet_failures(packet, job))
        return list(dict.fromkeys(failures))

    def _admit_fix(
        self,
        decision: Any,
        packet: EvidencePacket,
        checked: list[str],
        *,
        job: Any | None = None,
    ) -> AdmissionResult:
        target_skill_ids = _target_skill_ids(decision)
        hard_failures: list[str] = []
        warnings: list[str] = []

        if not target_skill_ids:
            hard_failures.append("missing_target_skill")
        for skill_id in target_skill_ids:
            if not self._skill_exists(skill_id, packet):
                hard_failures.append("unknown_target_skill")
                hard_failures.append(f"unknown_target_skill:{skill_id}")

        skill_file_refs = _skill_file_refs(packet, target_skill_ids)
        lifecycle_refs = _skill_lifecycle_refs(packet, target_skill_ids)
        manual_fix_refs = _manual_fix_request_refs(packet)
        failure_refs = _failure_or_friction_refs(packet)
        target_state_refs = _target_skill_state_refs(packet, target_skill_ids)
        tool_quality_refs = list(_refs(packet, "tool_quality_record"))
        tool_incident_refs = list(_refs(packet, "tool_incident"))
        quality_signal_refs = _quality_signal_refs(packet)
        tool_dependency_warning = _tool_quality_dependency_warning(
            packet,
            target_skill_ids,
            [*tool_quality_refs, *tool_incident_refs],
        )
        tool_dependency_confirmed = (
            bool(tool_quality_refs or tool_incident_refs)
            and tool_dependency_warning is None
        )

        checked.extend(ref.ref_id for ref in skill_file_refs)
        checked.extend(ref.ref_id for ref in lifecycle_refs)
        checked.extend(ref.ref_id for ref in manual_fix_refs)
        checked.extend(ref.ref_id for ref in failure_refs)
        checked.extend(ref.ref_id for ref in target_state_refs)
        checked.extend(ref.ref_id for ref in tool_quality_refs)
        checked.extend(ref.ref_id for ref in tool_incident_refs)
        checked.extend(ref.ref_id for ref in quality_signal_refs)

        if not skill_file_refs:
            hard_failures.append("missing_skill_file_ref")
        if not lifecycle_refs and not manual_fix_refs and not tool_dependency_confirmed:
            hard_failures.append("missing_skill_lifecycle_ref")
        if not failure_refs and not manual_fix_refs:
            hard_failures.append("missing_failure_evidence")
        if (
            tool_quality_refs
            and not tool_incident_refs
            and not _is_quality_signal_context(packet, None)
        ):
            hard_failures.append("tool_quality_aggregate_without_incident")
        hard_failures.extend(
            _quality_signal_fix_failures(
                decision,
                packet,
                target_skill_ids=target_skill_ids,
            )
        )

        if hard_failures:
            return self._result(
                decision,
                packet,
                outcome="reject",
                hard_failures=hard_failures,
                warnings=warnings,
                required_refs_checked=checked,
            )

        environment_refs = [ref for ref in failure_refs if _is_environment_failure(ref)]
        non_environment_refs = [ref for ref in failure_refs if ref not in environment_refs]
        if environment_refs and not non_environment_refs:
            warnings.append("environment_failure")
            warnings.extend(_environment_reason_tags(environment_refs))
            return self._result(
                decision,
                packet,
                outcome="noop",
                warnings=warnings,
                required_refs_checked=checked,
            )

        if tool_dependency_warning:
            warnings.append(tool_dependency_warning)
            return self._result(
                decision,
                packet,
                outcome="candidate",
                warnings=warnings,
                required_refs_checked=checked,
            )

        if not manual_fix_refs and not _fix_causality_plausible(decision, packet, failure_refs):
            warnings.append("causality_uncertain")
            return self._result(
                decision,
                packet,
                outcome="candidate",
                warnings=warnings,
                required_refs_checked=checked,
            )

        return self._result(
            decision,
            packet,
            outcome="direct",
            warnings=warnings,
            required_refs_checked=checked,
        )

    def _admit_derived(
        self,
        decision: Any,
        packet: EvidencePacket,
        checked: list[str],
    ) -> AdmissionResult:
        target_skill_ids = _target_skill_ids(decision)
        hard_failures: list[str] = []
        warnings: list[str] = []

        if not target_skill_ids:
            hard_failures.append("missing_parent_skill")
        for skill_id in target_skill_ids:
            if not self._skill_exists(skill_id, packet):
                hard_failures.append("unknown_parent_skill")
                hard_failures.append(f"unknown_parent_skill:{skill_id}")

        skill_file_refs = _skill_file_refs(packet, target_skill_ids)
        checked.extend(ref.ref_id for ref in skill_file_refs)
        if not skill_file_refs:
            hard_failures.append("missing_parent_skill_file_ref")
        if hard_failures:
            return self._result(
                decision,
                packet,
                outcome="reject",
                hard_failures=hard_failures,
                required_refs_checked=checked,
            )

        text = _decision_text(decision)
        has_divergence = _contains_any(text, _DERIVED_DIVERGENCE_TERMS)
        user_explicit = _is_user_explicit(decision, packet)
        provisional_allowed = self.allow_single_observation_capture or user_explicit
        if not has_divergence:
            warnings.append("no_derived_divergence")
        if _contains_any(text, _BUGFIX_TERMS) and not has_divergence:
            warnings.append("bugfix_should_be_fix")
        if not provisional_allowed:
            warnings.append("provisional_evolution_disabled")
        elif not user_explicit:
            warnings.append("single_observation_allowed")

        outcome = "candidate"
        if has_divergence and provisional_allowed:
            outcome = "direct"
        return self._result(
            decision,
            packet,
            outcome=outcome,
            warnings=warnings,
            required_refs_checked=checked,
        )

    def _admit_captured(
        self,
        decision: Any,
        packet: EvidencePacket,
        checked: list[str],
    ) -> AdmissionResult:
        warnings: list[str] = []
        contract_failures = capture_contract_failures(
            _attr(decision, "proposal_contract"),
            packet,
            claimed_refs=_claim_ref_ids(decision),
        )
        if contract_failures:
            return self._result(
                decision,
                packet,
                outcome="noop",
                warnings=contract_failures,
                required_refs_checked=checked,
            )

        if _captures_permission_bypass(decision):
            return self._result(
                decision,
                packet,
                outcome="noop",
                warnings=["permission_bypass_capture"],
                required_refs_checked=checked,
            )

        if _existing_skill_covers(decision):
            return self._result(
                decision,
                packet,
                outcome="noop",
                warnings=["existing_skill_covers_workflow"],
                required_refs_checked=checked,
            )

        if _capture_depends_on_ephemeral_context(packet, decision):
            return self._result(
                decision,
                packet,
                outcome="noop",
                warnings=["ephemeral_or_secret_dependent_capture"],
                required_refs_checked=checked,
            )

        user_explicit = _is_user_explicit(decision, packet)
        provisional_allowed = self.allow_single_observation_capture or user_explicit
        if not provisional_allowed:
            warnings.append("provisional_evolution_disabled")
        elif not user_explicit:
            warnings.append("single_observation_allowed")

        outcome = "candidate"
        if provisional_allowed:
            outcome = "direct"
        return self._result(
            decision,
            packet,
            outcome=outcome,
            warnings=warnings,
            required_refs_checked=checked,
            source_validation_passed=True,
        )

    def _skill_exists(self, skill_id: str, packet: EvidencePacket) -> bool:
        if not skill_id:
            return False
        for ref_type in ("skill_file", "skill_record", "skill_event"):
            for ref in _refs(packet, ref_type):
                values = _metadata_values(ref.metadata, "skill_id", "skill_ids")
                if skill_id in values or skill_id in ref.ref_id:
                    return True

        for source in (self.skill_store, self.registry):
            if source is None:
                continue
            for method_name in ("load_record", "get_skill", "get", "load"):
                method = getattr(source, method_name, None)
                if not callable(method):
                    continue
                try:
                    if method(skill_id) is not None:
                        return True
                except Exception:
                    logger.debug("Skill lookup failed for %s", skill_id, exc_info=True)
                    break
        return False

    def _result(
        self,
        decision: Any,
        packet: EvidencePacket,
        *,
        outcome: str,
        hard_failures: list[str] | None = None,
        warnings: list[str] | None = None,
        required_refs_checked: list[str] | None = None,
        source_validation_passed: bool = False,
    ) -> AdmissionResult:
        normalized_outcome = outcome if outcome in _OUTCOMES else "reject"
        failure_tags = list(dict.fromkeys(_str_list(hard_failures)))
        warning_tags = list(dict.fromkeys(_str_list(warnings)))
        refs_checked = list(dict.fromkeys(_str_list(required_refs_checked)))
        decision_id = str(_attr(decision, "decision_id") or "")
        packet_id = str(getattr(packet, "packet_id", "") or "")
        created_at = _utc_now()
        admission_id = "adm_" + _digest(
            {
                "decision_id": decision_id,
                "packet_id": packet_id,
                "outcome": normalized_outcome,
                "hard_failures": failure_tags,
                "warnings": warning_tags,
                "refs": refs_checked,
                "source_validation_passed": bool(source_validation_passed),
            }
        )[:16]
        return AdmissionResult(
            admission_id=admission_id,
            decision_id=decision_id,
            packet_id=packet_id,
            outcome=normalized_outcome,
            hard_failures=failure_tags,
            warnings=warning_tags,
            required_refs_checked=refs_checked,
            source_validation_passed=bool(source_validation_passed),
            reviewed_by="rule",
            created_at=created_at,
        )

    def _persist(self, result: AdmissionResult) -> None:
        persist = getattr(self.evidence_store, "persist_admission", None)
        if not callable(persist):
            return
        try:
            persist(result)
        except Exception:
            logger.debug(
                "Failed to persist admission result %s",
                result.admission_id,
                exc_info=True,
            )


def _action(decision: Any) -> str:
    raw = (
        _attr(decision, "proposed_action")
        or _attr(decision, "action_type")
        or _attr(decision, "evolution_type")
        or ""
    )
    return str(getattr(raw, "value", raw) or "").strip().upper()


def _candidate_policy(decision: Any) -> str:
    return str(_attr(decision, "candidate_policy") or "").strip().lower()


def _target_skill_ids(decision: Any) -> list[str]:
    return _str_list(_attr(decision, "target_skill_ids") or _attr(decision, "target_skills"))


def _claim_ref_ids(decision: Any) -> list[str]:
    refs: list[str] = []
    for claim in list(_attr(decision, "evidence_claims") or []):
        refs.extend(_str_list(_attr(claim, "refs")))
    return list(dict.fromkeys(refs))


def _packet_ref_ids(packet: EvidencePacket) -> set[str]:
    refs = {
        ref.ref_id
        for group in packet.selected_refs.values()
        for ref in group
        if ref.ref_id
    }
    refs.update(path.ref_id for path in packet.readable_paths if path.ref_id)
    return refs


def _refs(packet: EvidencePacket, ref_type: str) -> list[ResourceRef]:
    return list(packet.selected_refs.get(ref_type) or [])


def _quality_signal_refs(packet: EvidencePacket) -> list[ResourceRef]:
    return _refs(packet, "quality_signal_ref")


def _is_quality_signal_context(
    packet: EvidencePacket,
    job: Any | None,
) -> bool:
    trigger_type = str(_attr(job, "trigger_type") or "").strip().upper()
    if trigger_type == "QUALITY_SIGNAL":
        return True
    if str(getattr(packet, "profile_name", "") or "") == "quality_signal":
        return True
    return False


def _quality_signal_packet_failures(
    packet: EvidencePacket,
    job: Any | None,
) -> list[str]:
    if not _is_quality_signal_context(packet, job):
        return []
    refs = _quality_signal_refs(packet)
    if not refs:
        return ["missing_quality_signal_ref"]

    failures: list[str] = []
    for ref in refs:
        metadata = ref.metadata
        actionability = str(metadata.get("actionability") or "").strip()
        evidence_status = str(metadata.get("evidence_status") or "").strip()
        signal_type = str(metadata.get("signal_type") or "").strip()
        if actionability != "trigger_review":
            failures.append("quality_signal_not_trigger_review")
        if evidence_status == STATUS_AGGREGATE_ONLY:
            failures.append("quality_signal_aggregate_only")
            failures.append("aggregate_only_quality_source")
        elif evidence_status not in TRIGGERABLE_EVIDENCE_STATUSES:
            failures.append("quality_signal_incomplete")
        if signal_type == "aggregate_without_incident":
            failures.append("quality_signal_aggregate_only")
            failures.append("aggregate_only_quality_source")
    return list(dict.fromkeys(failures))


def _quality_signal_fix_failures(
    decision: Any,
    packet: EvidencePacket,
    *,
    target_skill_ids: list[str],
) -> list[str]:
    if not _is_quality_signal_context(packet, None):
        return []

    failures: list[str] = []
    reason_tags = {tag.lower() for tag in _str_list(_attr(decision, "reason_tags"))}
    if reason_tags.intersection(_QUALITY_SIGNAL_EXTERNAL_ATTRIBUTIONS):
        failures.append("attribution_external_only")

    has_tool_event = bool(_refs(packet, "tool_event"))
    has_exact_tool_evidence = bool(
        _refs(packet, "tool_result") or _refs(packet, "tool_incident")
    )
    if not has_tool_event or (
        not has_exact_tool_evidence
        and not _has_actionable_partial_quality_signal(
            packet,
            target_skill_ids=target_skill_ids,
        )
    ):
        failures.append("missing_representative_tool_evidence")

    target_set = {item for item in target_skill_ids if item}
    signal_skill_ids = _quality_signal_skill_ids(packet)
    if target_set and signal_skill_ids and not target_set.intersection(signal_skill_ids):
        failures.append("quality_signal_target_skill_mismatch")

    return list(dict.fromkeys(failures))


def _has_actionable_partial_quality_signal(
    packet: EvidencePacket,
    *,
    target_skill_ids: list[str],
) -> bool:
    failed_tool_event_ids = {
        ref.ref_id
        for ref in _refs(packet, "tool_event")
        if _is_failed_tool_event_ref(ref)
    }
    skill_event_ids = {ref.ref_id for ref in _refs(packet, "skill_event")}
    skill_file_ids = {ref.ref_id for ref in _refs(packet, "skill_file")}
    target_set = {item for item in target_skill_ids if item}
    for ref in _quality_signal_refs(packet):
        actionability = str(ref.metadata.get("actionability") or "").strip()
        evidence_status = str(ref.metadata.get("evidence_status") or "").strip()
        if actionability != "trigger_review" or evidence_status != STATUS_ACTIONABLE_PARTIAL:
            continue
        backrefs = {str(item) for item in (ref.raw_backrefs or []) if item}
        if not failed_tool_event_ids.intersection(backrefs):
            continue
        if not skill_event_ids.intersection(backrefs):
            continue
        if not skill_file_ids.intersection(backrefs):
            continue
        if target_set and not _quality_signal_ref_skill_ids(ref).intersection(target_set):
            continue
        return True
    return False


def _is_failed_tool_event_ref(ref: ResourceRef) -> bool:
    if ref.ref_type != "tool_event":
        return False
    metadata = ref.metadata
    success = metadata.get("success")
    if success is False:
        return True
    if isinstance(success, str) and success.strip().lower() in {"0", "false", "no"}:
        return True
    status = str(metadata.get("status") or "").strip().lower()
    if status and status not in {"ok", "passed", "success", "succeeded"}:
        return True
    permission_status = str(metadata.get("permission_status") or "").strip().lower()
    return permission_status in {"blocked", "denied", "permission_denied", "rejected"}


def _quality_signal_skill_ids(packet: EvidencePacket) -> set[str]:
    skill_ids: set[str] = set()
    for ref in _quality_signal_refs(packet):
        skill_ids.update(_quality_signal_ref_skill_ids(ref))
    return {item for item in skill_ids if item}


def _quality_signal_ref_skill_ids(ref: ResourceRef) -> set[str]:
    metadata = ref.metadata
    skill_ids = set(_metadata_values(metadata, "skill_id", "skill_ids"))
    subject_type = str(metadata.get("subject_type") or "")
    subject_id = str(metadata.get("subject_id") or "")
    if subject_type == "tool_skill_relation" and subject_id:
        skill_ids.add(subject_id.split(":", 1)[0])
    return {item for item in skill_ids if item}


def _skill_file_refs(packet: EvidencePacket, skill_ids: list[str]) -> list[ResourceRef]:
    target_set = set(skill_ids)
    return [
        ref
        for ref in _refs(packet, "skill_file")
        if not target_set
        or _metadata_values(ref.metadata, "skill_id", "skill_ids").intersection(target_set)
        or any(skill_id in ref.ref_id for skill_id in target_set)
    ]


def _skill_lifecycle_refs(packet: EvidencePacket, skill_ids: list[str]) -> list[ResourceRef]:
    target_set = set(skill_ids)
    allowed = {"selected", "invoked", "applied"}
    refs: list[ResourceRef] = []
    for ref_type in ("skill_event", "skill_record"):
        for ref in _refs(packet, ref_type):
            values = _metadata_values(ref.metadata, "skill_id", "skill_ids")
            if target_set and not values.intersection(target_set) and not any(
                skill_id in ref.ref_id for skill_id in target_set
            ):
                continue
            lifecycle = str(
                ref.metadata.get("event_type")
                or ref.metadata.get("lifecycle_event")
                or ref.metadata.get("status")
                or ""
            ).strip().lower()
            nested = ref.metadata.get("metadata")
            if not lifecycle and isinstance(nested, Mapping):
                lifecycle = str(
                    nested.get("event_type") or nested.get("lifecycle_event") or ""
                ).strip().lower()
            if lifecycle in allowed:
                refs.append(ref)
    return refs


def _manual_fix_request_refs(packet: EvidencePacket) -> list[ResourceRef]:
    refs = list(_refs(packet, "manual_request_ref"))
    if not refs:
        return []
    if (
        str(getattr(packet, "profile_name", "") or "") == "manual_fix_or_derive"
        and str(getattr(packet, "subprofile", "") or "") == "fix"
    ):
        return refs
    selected: list[ResourceRef] = []
    for ref in refs:
        action = str(ref.metadata.get("action") or "").strip().lower()
        if action in {"fix", "repair"}:
            selected.append(ref)
    return selected


def _target_skill_state_refs(
    packet: EvidencePacket,
    skill_ids: list[str],
) -> list[ResourceRef]:
    target_set = set(skill_ids)
    refs: list[ResourceRef] = []
    for ref in _refs(packet, "skill_record"):
        values = _metadata_values(ref.metadata, "skill_id", "skill_ids")
        if (
            not target_set
            or values.intersection(target_set)
            or any(skill_id in ref.ref_id for skill_id in target_set)
        ):
            refs.append(ref)
    return refs


def _failure_or_friction_refs(packet: EvidencePacket) -> list[ResourceRef]:
    refs: list[ResourceRef] = []
    for ref in _refs(packet, "runtime_snapshot"):
        metadata = ref.metadata
        status = str(metadata.get("status") or "").strip().lower()
        stop_reason = str(metadata.get("stop_reason") or "").strip().lower()
        if status and status not in {"success", "completed", "ok", "passed"}:
            refs.append(ref)
        elif stop_reason in {"max_iterations", "error", "cancelled", "incomplete"}:
            refs.append(ref)
    refs.extend(_tool_failure_refs(packet))
    for ref in _refs(packet, "skill_event"):
        lifecycle = str(ref.metadata.get("event_type") or ref.metadata.get("status") or "").lower()
        if lifecycle in {"fallback", "failed", "error", "permission_denied"}:
            refs.append(ref)
    for ref in _refs(packet, "transcript_message"):
        text = _ref_text(ref).lower()
        if any(term in text for term in ("correction", "not what i asked", "wrong", "failed")):
            refs.append(ref)
    return _dedupe_refs(refs)


def _tool_failure_refs(packet: EvidencePacket) -> list[ResourceRef]:
    refs: list[ResourceRef] = []
    for ref_type in ("tool_event", "tool_result", "tool_incident"):
        for ref in _refs(packet, ref_type):
            metadata = ref.metadata
            status = str(
                metadata.get("status")
                or metadata.get("outcome")
                or metadata.get("result")
                or ""
            ).strip().lower()
            success = metadata.get("success")
            if ref_type == "tool_incident":
                refs.append(ref)
            elif status and status not in {"success", "ok", "completed", "passed"}:
                refs.append(ref)
            elif success is False:
                refs.append(ref)
            elif (
                metadata.get("error_type")
                or metadata.get("error_message")
                or metadata.get("error_bucket")
                or metadata.get("permission_status") in {"denied", "rejected"}
            ):
                refs.append(ref)
    return _dedupe_refs(refs)


def _is_environment_failure(ref: ResourceRef) -> bool:
    return _contains_any(_ref_text(ref), _ENVIRONMENT_FAILURE_TERMS)


def _environment_reason_tags(refs: list[ResourceRef]) -> list[str]:
    tags: list[str] = []
    text = "\n".join(_ref_text(ref).lower() for ref in refs)
    if "api key" in text or "apikey" in text or "_api_key" in text:
        tags.append("api_key")
    if "network" in text or "dns" in text or "connection" in text:
        tags.append("network")
    if "sandbox" in text or "permission mode" in text:
        tags.append("sandbox_or_permission_mode")
    if "external service" in text or "service unavailable" in text:
        tags.append("external_service")
    return tags or ["environment"]


def _fix_causality_plausible(
    decision: Any,
    packet: EvidencePacket,
    failure_refs: list[ResourceRef],
) -> bool:
    if not failure_refs:
        return False
    text = _decision_text(decision).lower()
    if any(term in text for term in ("no_causality", "unrelated_to_skill", "external_only")):
        return False
    if any(term in text for term in ("skill", "instruction", "step", "workflow", "fix", "repair")):
        return True
    return bool(_skill_lifecycle_refs(packet, _target_skill_ids(decision)))


def _tool_quality_dependency_warning(
    packet: EvidencePacket,
    target_skill_ids: list[str],
    tool_refs: list[ResourceRef],
) -> str | None:
    if not tool_refs:
        return None
    tool_variants = _tool_identity_variants_from_refs(tool_refs)
    if not tool_variants:
        return "tool_dependency_uncertain"

    dependency_variants = _target_skill_dependency_variants(packet, target_skill_ids)
    if dependency_variants and dependency_variants.intersection(tool_variants):
        return None
    if dependency_variants:
        return "tool_dependency_mismatch"
    if _target_skill_text_mentions_tool(packet, target_skill_ids, tool_variants):
        return None
    return "tool_dependency_uncertain"


def _target_skill_dependency_variants(
    packet: EvidencePacket,
    target_skill_ids: list[str],
) -> set[str]:
    variants: set[str] = set()
    for ref in _target_skill_state_refs(packet, target_skill_ids):
        for value in _metadata_tool_values(ref.metadata):
            variants.update(_tool_key_variants(value))
    return variants


def _metadata_tool_values(metadata: Mapping[str, Any]) -> set[str]:
    keys = {
        "tool_dependency",
        "tool_dependencies",
        "tool_key",
        "tool_keys",
        "critical_tool",
        "critical_tools",
        "allowed-tools",
        "allowed_tools",
    }
    values: set[str] = set()
    for key, value in metadata.items():
        if key in keys:
            values.update(_split_tool_values(value))
        if isinstance(value, Mapping):
            values.update(_metadata_tool_values(value))
    return values


def _split_tool_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return set()
        parts = {
            item.strip()
            for item in _split_tool_value_tokens(raw)
            if item.strip()
        }
        parts.add(raw)
        return parts
    if isinstance(value, Mapping):
        return _metadata_tool_values(value)
    if isinstance(value, (list, tuple, set)):
        values: set[str] = set()
        for item in value:
            values.update(_split_tool_values(item))
        return values
    text = str(value).strip()
    return {text} if text else set()


def _split_tool_value_tokens(value: str) -> list[str]:
    return re.split(r"[\s,;\n]+", value)


def _tool_identity_variants_from_refs(refs: list[ResourceRef]) -> set[str]:
    variants: set[str] = set()
    for ref in refs:
        metadata = ref.metadata
        raw_key = metadata.get("tool_key")
        if raw_key:
            variants.update(_tool_key_variants(raw_key))
        tool_name = metadata.get("tool_name")
        backend = metadata.get("backend")
        server = metadata.get("server") or metadata.get("server_name")
        if tool_name:
            variants.update(_tool_key_variants(tool_name))
            if backend:
                variants.update(
                    _tool_key_variants(
                        f"{backend}:{server or 'default'}:{tool_name}"
                    )
                )
        variants.update(_tool_key_variants(_tool_key_from_ref_id(ref.ref_id)))
    return {item for item in variants if item}


def _tool_key_from_ref_id(ref_id: str) -> str:
    parts = str(ref_id or "").split(":")
    if len(parts) >= 4 and parts[0] in {"tool_quality_record", "tool_incident"}:
        return ":".join(parts[1:4])
    return ""


def _tool_key_variants(value: Any) -> set[str]:
    text = str(value or "").strip().lower()
    if not text:
        return set()
    variants = {text}
    parts = [part for part in text.split(":") if part]
    if len(parts) >= 3:
        backend, server, tool_name = parts[0], parts[1], parts[2]
        variants.add(f"{backend}:{server}:{tool_name}")
        variants.add(f"{backend}:{tool_name}")
        variants.add(tool_name)
    elif len(parts) == 2:
        backend, tool_name = parts
        variants.add(f"{backend}:default:{tool_name}")
        variants.add(f"{backend}:{tool_name}")
        variants.add(tool_name)
    elif len(parts) == 1:
        variants.add(parts[0])
    return variants


def _target_skill_text_mentions_tool(
    packet: EvidencePacket,
    target_skill_ids: list[str],
    tool_variants: set[str],
) -> bool:
    names = {variant.split(":")[-1] for variant in tool_variants if variant}
    names.update(tool_variants)
    names = {name for name in names if len(name) >= 3}
    if not names:
        return False
    text_parts: list[str] = []
    for ref in _skill_file_refs(packet, target_skill_ids):
        text_parts.append(_ref_text(ref).lower())
    for ref in _target_skill_state_refs(packet, target_skill_ids):
        text_parts.append(_ref_text(ref).lower())
    text = "\n".join(text_parts)
    return any(name in text for name in names)


def _capture_depends_on_ephemeral_context(
    packet: EvidencePacket,
    decision: Any,
) -> bool:
    cited_ref_ids = set(
        capture_contract_ref_ids(_attr(decision, "proposal_contract"))
    )
    if cited_ref_ids:
        cited_refs = [
            ref
            for refs in packet.selected_refs.values()
            for ref in refs
            if ref.ref_id in cited_ref_ids
        ]
        return any(
            bool(getattr(ref, "contains_secret", False))
            or _contains_ephemeral_term(
                _ref_text(ref),
                _CONTEXT_EPHEMERAL_CAPTURE_TERMS,
            )
            for ref in cited_refs
        )

    # Legacy decisions without a source-validation contract retain the
    # conservative packet-wide fallback.
    if _contains_ephemeral_term(
        _decision_text(decision),
        _EPHEMERAL_CAPTURE_TERMS,
    ):
        return True

    manual_text = "\n".join(_ref_text(ref) for ref in _refs(packet, "manual_request_ref"))
    if _contains_ephemeral_term(manual_text, _EPHEMERAL_CAPTURE_TERMS):
        return True

    parts: list[str] = []
    for ref_type in (
        "runtime_snapshot",
        "transcript_message",
        "tool_event",
        "tool_result",
        "file_history",
    ):
        for ref in _refs(packet, ref_type):
            if getattr(ref, "contains_secret", False):
                return True
            parts.append(_ref_text(ref))
    return _contains_ephemeral_term(
        "\n".join(parts),
        _CONTEXT_EPHEMERAL_CAPTURE_TERMS,
    )


def _captures_permission_bypass(decision: Any) -> bool:
    contract = _attr(decision, "proposal_contract")
    contract_parts: list[str] = []
    if isinstance(contract, Mapping):
        contract_parts.append(str(contract.get("capability") or ""))
        contract_parts.extend(_str_list(contract.get("preconditions")))
        contract_parts.extend(_str_list(contract.get("limitations")))
    text = "\n".join([_decision_text(decision), *contract_parts]).lower()
    return bool(
        any(term in text for term in _PERMISSION_BYPASS_TERMS)
        and any(term in text for term in _PERMISSION_CONTROL_TERMS)
    )


def _is_user_explicit(decision: Any, packet: EvidencePacket) -> bool:
    if _refs(packet, "manual_request_ref"):
        return True
    recurrence = str(_attr(decision, "recurrence") or "").lower()
    if recurrence == "user_explicit":
        return True
    tags = {
        str(tag).strip().lower()
        for tag in _str_list(_attr(decision, "reason_tags"))
    }
    return bool(tags & {"user_explicit", "user_requested", "manual"})


def _existing_skill_covers(decision: Any) -> bool:
    text = _decision_text(decision).lower()
    return "existing_skill_covers" in text or "covered_by_existing_skill" in text


def _dedupe_refs(refs: list[ResourceRef]) -> list[ResourceRef]:
    seen: set[str] = set()
    result: list[ResourceRef] = []
    for ref in refs:
        key = ref.ref_id
        if key in seen:
            continue
        seen.add(key)
        result.append(ref)
    return result


def _decision_text(decision: Any) -> str:
    parts: list[str] = []
    for name in ("reason_summary", "noop_reason"):
        value = _attr(decision, name)
        if value:
            parts.append(str(value))
    parts.extend(_str_list(_attr(decision, "reason_tags")))
    parts.extend(_str_list(_attr(decision, "risks")))
    for claim in list(_attr(decision, "evidence_claims") or []):
        value = _attr(claim, "claim")
        if value:
            parts.append(str(value))
    return "\n".join(parts)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    normalized = str(text or "").lower()
    return any(term in normalized for term in terms)


def _contains_ephemeral_term(text: str, terms: tuple[str, ...]) -> bool:
    normalized = str(text or "").lower()
    pluralizable = {
        "api key",
        "apikey",
        "_api_key",
        "secret",
        "token",
        "password",
        "credential",
    }
    for term in terms:
        if term.startswith("/"):
            if term in normalized:
                return True
            continue
        suffix = "s?" if term in pluralizable else ""
        pattern = rf"(?<![a-z0-9_]){re.escape(term)}{suffix}(?![a-z0-9_])"
        if re.search(pattern, normalized):
            return True
    return False


def _ref_text(ref: ResourceRef) -> str:
    return "\n".join(
        [
            str(ref.preview or ""),
            json.dumps(ref.metadata, sort_keys=True, default=str),
        ]
    )


def _metadata_values(metadata: Mapping[str, Any], *keys: str) -> set[str]:
    values: set[str] = set()
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value:
            values.add(value)
        elif isinstance(value, (list, tuple, set)):
            values.update(str(item) for item in value if str(item))
    nested = metadata.get("metadata")
    if isinstance(nested, Mapping):
        values.update(_metadata_values(nested, *keys))
    return values


def _attr(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


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


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
