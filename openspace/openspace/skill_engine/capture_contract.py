"""Shared deterministic checks for source-validated CAPTURED proposals."""

from __future__ import annotations

import re
from typing import Any, Mapping

from openspace.skill_engine.evidence import EvidencePacket, ResourceRef

_FALLBACK_REF_TYPES = {
    "background_task_result",
    "memory_ref",
    "recording_ref",
}
_PROCEDURE_REF_TYPES = {
    "file_history",
    "skill_event",
    "tool_event",
    "tool_result",
}
_VALIDATION_REF_TYPES = {
    "behavior_eval_result_ref",
    "tool_event",
    "tool_result",
}
_UNVERIFIED_CORRECTNESS_PATTERNS = (
    re.compile(
        r"\b(?:correctness|accuracy)\b[^.]{0,200}\b(?:not|never)\b"
        r"[^.]{0,100}\b(?:verified|validated|confirmed|checked)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:not|never)\b[^.]{0,100}\b(?:verified|validated|confirmed|checked)\b"
        r"[^.]{0,100}\b(?:correctness|accuracy)\b",
        re.IGNORECASE,
    ),
)


def normalize_capture_contract(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        value = value.to_dict()
    if not isinstance(value, Mapping):
        return {}
    return {
        "capability": str(value.get("capability") or "").strip(),
        "preconditions": _str_list(value.get("preconditions")),
        "procedure_refs": _str_list(value.get("procedure_refs")),
        "validation_refs": _str_list(value.get("validation_refs")),
        "validation_summary": str(value.get("validation_summary") or "").strip(),
        "limitations": _str_list(value.get("limitations")),
    }


def capture_contract_ref_ids(value: Any) -> list[str]:
    contract = normalize_capture_contract(value)
    return list(
        dict.fromkeys(
            [
                *_str_list(contract.get("procedure_refs")),
                *_str_list(contract.get("validation_refs")),
            ]
        )
    )


def capture_contract_failures(
    value: Any,
    packet: EvidencePacket,
    *,
    claimed_refs: list[str] | None = None,
) -> list[str]:
    contract = normalize_capture_contract(value)
    if not contract:
        return ["missing_capture_contract"]

    procedure_refs = _str_list(contract.get("procedure_refs"))
    validation_refs = _str_list(contract.get("validation_refs"))
    failures: list[str] = []
    if not contract["capability"]:
        failures.append("missing_capability")
    if not procedure_refs:
        failures.append("missing_procedure_refs")
    if not validation_refs:
        failures.append("missing_validation_refs")
    if not contract["validation_summary"]:
        failures.append("missing_validation_summary")
    if _disclaims_output_correctness(contract["limitations"]):
        failures.append("capture_contract_disclaims_output_correctness")
    if set(procedure_refs).intersection(validation_refs):
        failures.append("procedure_validation_refs_overlap")

    packet_refs = _packet_refs_by_id(packet)
    contract_refs = list(dict.fromkeys([*procedure_refs, *validation_refs]))
    if any(ref_id not in packet_refs for ref_id in contract_refs):
        failures.append("capture_contract_missing_refs")
    if claimed_refs is not None and not set(contract_refs).issubset(claimed_refs):
        failures.append("capture_contract_refs_not_claimed")

    procedure_evidence = [
        packet_refs[ref_id]
        for ref_id in procedure_refs
        if ref_id in packet_refs
    ]
    validation_evidence = [
        packet_refs[ref_id]
        for ref_id in validation_refs
        if ref_id in packet_refs
    ]
    procedure_observations = {
        _observation_key(ref) for ref in procedure_evidence
    }
    validation_observations = {
        _observation_key(ref) for ref in validation_evidence
    }
    if procedure_observations.intersection(validation_observations):
        failures.append("procedure_validation_observation_overlap")
    if procedure_evidence and all(
        ref.ref_type in _FALLBACK_REF_TYPES
        for ref in procedure_evidence
    ):
        failures.append("fallback_only_procedure_evidence")
    if procedure_evidence and not any(
        ref.ref_type in _PROCEDURE_REF_TYPES
        for ref in procedure_evidence
    ):
        failures.append("procedure_refs_lack_execution")
    if validation_evidence and not any(
        ref.ref_type in _VALIDATION_REF_TYPES
        for ref in validation_evidence
    ):
        failures.append("validation_refs_lack_observation")
    if any(_evidence_failed(ref, packet_refs) for ref in procedure_evidence):
        failures.append("capture_procedure_failed")
    if any(_evidence_failed(ref, packet_refs) for ref in validation_evidence):
        failures.append("capture_validation_failed")
    return list(dict.fromkeys(failures))


def _disclaims_output_correctness(limitations: list[str]) -> bool:
    return any(
        pattern.search(limitation)
        for limitation in limitations
        for pattern in _UNVERIFIED_CORRECTNESS_PATTERNS
    )


def format_capture_contract(value: Any) -> str:
    contract = normalize_capture_contract(value)
    if not contract:
        return ""
    lines = [
        "## Admitted Capture Contract",
        "Author exactly this source-validated capability. Do not add unsupported "
        "algorithms, thresholds, providers, postconditions, or compatibility claims.",
        "The cited packet evidence is complete for this authoring task. Do not inspect "
        "additional files or broaden the skill from uncited context.",
        f"- Capability: {contract['capability']}",
        f"- Preconditions: {_format_items(contract['preconditions'])}",
        f"- Procedure evidence refs: {_format_items(contract['procedure_refs'])}",
        f"- Validation evidence refs: {_format_items(contract['validation_refs'])}",
        f"- Validated postcondition: {contract['validation_summary']}",
        f"- Limitations: {_format_items(contract['limitations'])}",
    ]
    return "\n".join(lines)


def _packet_refs_by_id(packet: EvidencePacket) -> dict[str, ResourceRef]:
    return {
        ref.ref_id: ref
        for refs in packet.selected_refs.values()
        for ref in refs
        if ref.ref_id
    }


def _ref_failed(ref: ResourceRef) -> bool:
    metadata = ref.metadata
    success = metadata.get("success")
    if success is False:
        return True
    if isinstance(success, str) and success.strip().lower() in {
        "0",
        "false",
        "no",
    }:
        return True
    status = str(
        metadata.get("status") or metadata.get("outcome") or ""
    ).strip().lower()
    if status and status not in {
        "completed",
        "ok",
        "passed",
        "success",
        "succeeded",
    }:
        return True
    return bool(
        metadata.get("error_type")
        or metadata.get("error_message")
        or metadata.get("error_bucket")
        or str(metadata.get("permission_status") or "").lower()
        in {"blocked", "denied", "permission_denied", "rejected"}
    )


def _evidence_failed(
    ref: ResourceRef,
    packet_refs: dict[str, ResourceRef],
) -> bool:
    if _ref_failed(ref):
        return True
    return any(
        _ref_failed(packet_refs[backref])
        for backref in ref.raw_backrefs
        if backref in packet_refs
    )


def _observation_key(ref: ResourceRef) -> str:
    tool_use_id = str(ref.metadata.get("tool_use_id") or "").strip()
    if tool_use_id:
        return f"tool_use:{tool_use_id}"
    for backref in ref.raw_backrefs:
        if str(backref).startswith("tool_event:"):
            return f"tool_event:{backref}"
    return f"ref:{ref.ref_id}"


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return list(dict.fromkeys(str(item) for item in value if str(item)))
    return []


def _format_items(value: Any) -> str:
    items = _str_list(value)
    return "; ".join(items) if items else "(none stated)"
