"""Evidence-backed decision engine for skill evolution proposals."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from openspace.skill_engine.evidence import EvidencePacket
from openspace.skill_engine.signals.types import (
    STATUS_AGGREGATE_ONLY,
    TRIGGERABLE_EVIDENCE_STATUSES,
)
from openspace.skill_engine.types import ExecutionAnalysis
from openspace.utils.logging import Logger

from .analysis_adapter import (
    AnalyzerDecisionAdapter,
    packet_conflicts_with_analysis,
)
from .types import DecisionBundle, DecisionRationale, EvidenceClaim

logger = Logger.get_logger(__name__)

_FALLBACK_CAPTURE_REF_TYPES = {
    "background_task_result",
    "memory_ref",
    "recording_ref",
}


class DecisionEngine:
    """Run analyzer over an EvidencePacket and persist auditable decisions."""

    def __init__(
        self,
        analyzer: Any,
        evidence_store: Any,
    ) -> None:
        self.analyzer = analyzer
        self.evidence_store = evidence_store
        self.analysis_adapter = AnalyzerDecisionAdapter()

    async def decide(self, packet: EvidencePacket) -> DecisionBundle:
        if packet.packet_type != "analysis":
            decision = self.make_noop(
                packet,
                reason=f"unsupported_packet_type:{packet.packet_type}",
                tags=["unsupported_packet_type"],
            )
            self._persist_decisions(packet, [decision])
            return DecisionBundle(analysis=None, decisions=[decision], packet_id=packet.packet_id)

        quality_signal_failures = _quality_signal_packet_failures(packet)
        if quality_signal_failures:
            decision = self.make_noop(
                packet,
                reason=quality_signal_failures[0],
                tags=["quality_signal", *quality_signal_failures],
            )
            self._persist_decisions(packet, [decision])
            return DecisionBundle(analysis=None, decisions=[decision], packet_id=packet.packet_id)

        try:
            analyze = getattr(self.analyzer, "analyze_packet", None)
            if not callable(analyze):
                analysis = None
            else:
                analysis = await analyze(packet)
        except Exception as exc:
            logger.debug("DecisionEngine analyzer failed", exc_info=True)
            decision = self.make_noop(
                packet,
                reason="analysis_error",
                tags=["analysis_error"],
            )
            decision = replace(decision, risks=[str(exc)])
            self._persist_decisions(packet, [decision])
            return DecisionBundle(analysis=None, decisions=[decision], packet_id=packet.packet_id)

        source_analysis_id: str | None = None
        if analysis is not None:
            source_analysis_id = self._persist_analysis_ref(analysis, packet)

        if analysis is None:
            decisions = [
                self.make_noop(
                    packet,
                    reason="analysis_unavailable",
                    tags=["analysis_unavailable"],
                )
            ]
        else:
            conflicts = packet_conflicts_with_analysis(analysis, packet)
            if conflicts:
                decision = self.make_noop(
                    packet,
                    reason="analysis_conflicts_with_packet",
                    tags=["analysis_conflict", "packet_facts_preferred"],
                )
                decision = replace(
                    decision,
                    risks=list(dict.fromkeys([*decision.risks, *conflicts])),
                    source_analysis_id=source_analysis_id,
                    analyzed_by=analysis.analyzed_by or decision.analyzed_by,
                )
                decisions = [decision]
            else:
                decisions = self.from_analysis(analysis, packet)
                decisions = [
                    replace(decision, source_analysis_id=source_analysis_id)
                    for decision in decisions
                ]
                if not decisions:
                    decision = self.make_noop(
                        packet,
                        reason="no_evolution_suggestion",
                        tags=["no_evolution_suggestion"],
                    )
                    decisions = [
                        replace(
                            decision,
                            source_analysis_id=source_analysis_id,
                            analyzed_by=analysis.analyzed_by or decision.analyzed_by,
                        )
                    ]

        decisions = [self._ensure_valid(decision, packet) for decision in decisions]
        self._persist_decisions(packet, decisions)
        return DecisionBundle(analysis=analysis, decisions=decisions, packet_id=packet.packet_id)

    def from_analysis(
        self,
        analysis: ExecutionAnalysis,
        packet: EvidencePacket,
    ) -> list[DecisionRationale]:
        return self.analysis_adapter.from_analysis(
            analysis,
            packet,
            source_analysis_id=None,
        )

    def validate_decision_refs(
        self,
        decision: DecisionRationale,
        packet: EvidencePacket,
    ) -> list[str]:
        if decision.proposed_action == "NOOP":
            return []

        failures: list[str] = []
        selected_refs = _selected_ref_ids(packet)
        readable_refs = {item.ref_id for item in packet.readable_paths}
        valid_refs = selected_refs | readable_refs

        if not decision.evidence_claims:
            failures.append("no_evidence_claims")
        for idx, claim in enumerate(decision.evidence_claims):
            if not claim.refs:
                failures.append(f"claim_{idx}_missing_refs")
                continue
            missing = [ref for ref in claim.refs if ref not in valid_refs]
            for ref in missing:
                failures.append(f"missing_ref:{ref}")

        if decision.proposed_action == "FIX":
            if not decision.target_skill_ids:
                failures.append("fix_missing_target_skill")
            if not _has_skill_file_ref(packet, decision.target_skill_ids):
                failures.append("fix_missing_skill_file_ref")

        if decision.proposed_action == "CAPTURED":
            claim_refs = [ref for claim in decision.evidence_claims for ref in claim.refs]
            ref_types = _ref_types_for_ref_ids(packet, claim_refs)
            if ref_types and ref_types.issubset(_FALLBACK_CAPTURE_REF_TYPES):
                failures.append("captured_only_fallback_refs")

        return list(dict.fromkeys(failures))

    def make_noop(
        self,
        packet: EvidencePacket,
        reason: str,
        tags: list[str],
    ) -> DecisionRationale:
        refs = _primary_packet_refs(packet)[:8]
        claims = [
            EvidenceClaim(
                claim="packet was reviewed and no safe skill mutation was authorized",
                refs=refs,
                confidence="high" if refs else "low",
            )
        ] if refs else []
        payload = {
            "packet_id": packet.packet_id,
            "trigger_job_id": packet.trigger_job_id,
            "reason": reason,
            "tags": tags,
        }
        return DecisionRationale(
            decision_id=f"dec_{_digest(payload)}",
            trigger_job_id=packet.trigger_job_id,
            proposed_action="NOOP",
            candidate_policy="reject",
            target_skill_ids=[],
            reason_summary=f"No skill mutation admitted: {reason}",
            reason_tags=_noop_reason_tags(packet, tags),
            evidence_claims=claims,
            confidence=0.9,
            risks=[],
            source_analysis_id=None,
            noop_reason=reason,
            analyzed_by="decision_engine",
            created_at=_utc_now(),
        )

    def _ensure_valid(
        self,
        decision: DecisionRationale,
        packet: EvidencePacket,
    ) -> DecisionRationale:
        failures = self.validate_decision_refs(decision, packet)
        if not failures:
            return decision
        risks = list(dict.fromkeys([*decision.risks, *failures]))
        tags = list(dict.fromkeys([*decision.reason_tags, "invalid_decision_refs"]))
        return replace(
            decision,
            candidate_policy="reject",
            confidence=min(decision.confidence, 0.2),
            risks=risks,
            reason_tags=tags,
        )

    def _persist_analysis_ref(
        self,
        analysis: ExecutionAnalysis,
        packet: EvidencePacket,
    ) -> str | None:
        persist = getattr(self.evidence_store, "persist_execution_analysis_ref", None)
        if not callable(persist):
            return None
        try:
            return persist(analysis, packet)
        except Exception:
            logger.debug("Failed to persist execution analysis ref", exc_info=True)
            return None

    def _persist_decisions(
        self,
        packet: EvidencePacket,
        decisions: list[DecisionRationale],
    ) -> None:
        persist = getattr(self.evidence_store, "persist_decision", None)
        if not callable(persist):
            return
        for decision in decisions:
            try:
                persist(decision, packet_id=packet.packet_id)
            except TypeError:
                persist(decision)
            except Exception:
                logger.debug(
                    "Failed to persist decision rationale %s",
                    decision.decision_id,
                    exc_info=True,
                )


def _selected_ref_ids(packet: EvidencePacket) -> set[str]:
    return {
        ref.ref_id
        for refs in packet.selected_refs.values()
        for ref in refs
        if ref.ref_id
    }


def _primary_packet_refs(packet: EvidencePacket) -> list[str]:
    preferred = [
        "quality_signal_ref",
        "runtime_snapshot",
        "transcript_message",
        "tool_event",
        "tool_result",
        "manual_request_ref",
        "skill_file",
    ]
    refs: list[str] = []
    for ref_type in preferred:
        refs.extend(ref.ref_id for ref in packet.selected_refs.get(ref_type, []))
    if refs:
        return list(dict.fromkeys(refs))
    return sorted(_selected_ref_ids(packet))


def _noop_reason_tags(packet: EvidencePacket, tags: list[str]) -> list[str]:
    reason_tags = ["noop", *tags]
    if str(getattr(packet, "profile_name", "") or "") == "quality_signal":
        reason_tags.append("quality_signal")
        if not any(str(tag).startswith("attribution:") for tag in reason_tags):
            reason_tags.append("attribution:insufficient_evidence")
    return list(dict.fromkeys(reason_tags))


def _has_skill_file_ref(packet: EvidencePacket, target_skill_ids: list[str]) -> bool:
    target_set = set(target_skill_ids)
    for ref in packet.selected_refs.get("skill_file", []):
        skill_id = str(ref.metadata.get("skill_id") or "")
        if skill_id in target_set or any(target in ref.ref_id for target in target_set):
            return True
    return False


def _quality_signal_packet_failures(packet: EvidencePacket) -> list[str]:
    if str(getattr(packet, "profile_name", "") or "") != "quality_signal":
        return []
    refs = list(packet.selected_refs.get("quality_signal_ref") or [])
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
        elif evidence_status not in TRIGGERABLE_EVIDENCE_STATUSES:
            failures.append("quality_signal_incomplete")
        if signal_type == "aggregate_without_incident":
            failures.append("quality_signal_aggregate_only")
    return list(dict.fromkeys(failures))


def _ref_types_for_ref_ids(packet: EvidencePacket, ref_ids: list[str]) -> set[str]:
    wanted = set(ref_ids)
    return {
        ref.ref_type
        for refs in packet.selected_refs.values()
        for ref in refs
        if ref.ref_id in wanted
    }


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
