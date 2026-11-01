"""Decision rationale contracts for evidence-backed skill evolution."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from openspace.skill_engine.types import ExecutionAnalysis


@dataclass(frozen=True, slots=True)
class EvidenceClaim:
    claim: str
    refs: list[str] = field(default_factory=list)
    confidence: str = "low"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "EvidenceClaim":
        return cls(
            claim=str(data.get("claim") or ""),
            refs=_str_list(data.get("refs")),
            confidence=str(data.get("confidence") or "low"),
        )


@dataclass(frozen=True, slots=True)
class DecisionRationale:
    decision_id: str
    trigger_job_id: str
    proposed_action: str
    candidate_policy: str
    target_skill_ids: list[str]
    reason_summary: str
    reason_tags: list[str]
    evidence_claims: list[EvidenceClaim]
    confidence: float
    risks: list[str]
    source_analysis_id: str | None
    noop_reason: str | None
    analyzed_by: str
    created_at: str
    local_category_path: str = ""
    category: str = ""
    proposal_contract: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_claims"] = [claim.to_dict() for claim in self.evidence_claims]
        return data

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "DecisionRationale":
        return cls(
            decision_id=str(data.get("decision_id") or ""),
            trigger_job_id=str(data.get("trigger_job_id") or ""),
            proposed_action=str(data.get("proposed_action") or ""),
            candidate_policy=str(data.get("candidate_policy") or ""),
            target_skill_ids=_str_list(data.get("target_skill_ids")),
            reason_summary=str(data.get("reason_summary") or ""),
            reason_tags=_str_list(data.get("reason_tags")),
            evidence_claims=[
                EvidenceClaim.from_mapping(item)
                for item in _mapping_list(data.get("evidence_claims"))
            ],
            confidence=_float_or_zero(data.get("confidence")),
            risks=_str_list(data.get("risks")),
            source_analysis_id=_none_or_str(data.get("source_analysis_id")),
            noop_reason=_none_or_str(data.get("noop_reason")),
            analyzed_by=str(data.get("analyzed_by") or ""),
            created_at=str(data.get("created_at") or ""),
            local_category_path=str(data.get("local_category_path") or ""),
            category=str(data.get("category") or ""),
            proposal_contract=_mapping_or_empty(data.get("proposal_contract")),
        )


@dataclass(frozen=True, slots=True)
class DecisionBundle:
    analysis: ExecutionAnalysis | None
    decisions: list[DecisionRationale]
    packet_id: str

    def __iter__(self):
        return iter(self.decisions)

    def __len__(self) -> int:
        return len(self.decisions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis": self.analysis.to_dict() if self.analysis is not None else None,
            "decisions": [decision.to_dict() for decision in self.decisions],
            "packet_id": self.packet_id,
        }


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


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
