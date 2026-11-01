"""Provider-neutral structured-proposal boundary shared by policy carriers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from agentpilot.domain import (
    ArmSlot,
    ArmSpec,
    ParentRepositoryMap,
    ParentSelection,
    PolicyMethod,
    SearchState,
    StructuredDirection,
    VisibleATaskEvidence,
)
from agentpilot.domain.common import canonical_json, deep_freeze, require_text
from agentpilot.search import (
    FailureDirectionCard,
    FixedEvidenceCard,
    validate_visible_a_evidence_for_state,
)


class RetryableProposalError(ValueError):
    """A completed provider call whose structured payload may be regenerated.

    Transport, accounting, dangling-intent, identity and persistence failures
    must use their own exception types and must never inherit this class.
    """


class DraftDeliveryError(RetryableProposalError):
    """A supplied draft could not be delivered; it is not an evidence deferral.

    The exact raw draft remains in ``draft`` for durable audit/format repair.
    Callers should not concatenate this audit payload into worker evidence.
    """

    def __init__(self, *, draft: Mapping[str, Any], issues: tuple[Mapping[str, Any], ...]):
        self.draft = deep_freeze(draft)
        self.issues = tuple(deep_freeze(issue) for issue in issues)
        super().__init__(
            "draft_delivery_failed: "
            + "; ".join(str(issue.get("message", "invalid draft")) for issue in issues)
        )

    def as_payload(self) -> Mapping[str, Any]:
        return {"kind": "draft_delivery_failed", "draft": self.draft, "issues": self.issues}


class ProposerDeferred(ValueError):
    """An explicit lack of evidence ends this slot without a same-input repair."""

    def __init__(self, reason: str, missing_evidence: tuple[str, ...] | list[str] = ()):
        self.reason = require_text(reason, "deferred proposal reason").strip()
        if len(self.reason) > 900:
            raise ValueError("deferred proposal reason exceeds 900 characters")
        if not isinstance(missing_evidence, (list, tuple)) or len(missing_evidence) > 4:
            raise ValueError("deferred proposal needs at most four missing-evidence items")
        missing = tuple(require_text(item, "missing evidence").strip() for item in missing_evidence)
        if any(len(item) > 300 for item in missing):
            raise ValueError("missing-evidence item exceeds 300 characters")
        self.missing_evidence = missing
        super().__init__("proposer_deferred: " + canonical_json(self.as_payload()))

    def as_payload(self) -> dict[str, Any]:
        return {"reason": self.reason, "missing_evidence": list(self.missing_evidence)}


class ProposerWithdrawn(ProposerDeferred):
    """A researcher explicitly rejected a saved hypothesis after investigation.

    The compact receipt pointer travels with the outcome; the original draft
    stays in the durable provider transcript, outside downstream prompt evidence.
    """

    def __init__(self, reason: str, *, retained_draft: Mapping[str, Any]):
        self.retained_draft = deep_freeze(retained_draft)
        super().__init__(reason)

    def as_payload(self) -> dict[str, Any]:
        return {
            **super().as_payload(),
            "kind": "draft_withdrawn",
            "retained_draft": dict(self.retained_draft),
        }


@dataclass(frozen=True)
class StructuredProposalRequest:
    """One idempotent request to realize a single structured direction.

    Memory calls carry separately labelled environment constraints and frozen
    ``memory.md`` text in ``carrier_context``, with no source slot. UCB calls
    carry exactly one allocated source slot. Both carriers may use
    ``repair_feedback`` for a bounded structured-output retry.
    """

    request_id: str
    method: PolicyMethod
    state: SearchState
    parent_selection: ParentSelection
    decision_round_id: str
    candidate_ordinal: int
    max_scope_refs: int
    max_new_paths: int
    max_resolved_scope_files: int
    max_resolved_scope_bytes: int
    carrier_context: str
    repository_map: ParentRepositoryMap
    allowed_new_path_suffixes: tuple[str, ...] = (".py", ".pyi")
    environment_context: str = ""
    evidence_cards: tuple[FixedEvidenceCard, ...] = ()
    failure_cards: tuple[FailureDirectionCard, ...] = ()
    visible_a_evidence: tuple[VisibleATaskEvidence, ...] = ()
    source_slot: ArmSlot | None = None
    repair_feedback: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    arm_spec: ArmSpec | None = None

    def __post_init__(self) -> None:
        require_text(self.request_id, "request_id")
        require_text(self.decision_round_id, "decision_round_id")
        if (
            isinstance(self.candidate_ordinal, bool)
            or not isinstance(self.candidate_ordinal, int)
            or self.candidate_ordinal < 0
        ):
            raise ValueError("candidate_ordinal must be a non-negative integer")
        for name, upper in (
            ("max_scope_refs", 8),
            ("max_new_paths", 4),
            ("max_resolved_scope_files", 4_096),
            ("max_resolved_scope_bytes", 64 * 1024 * 1024),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= upper
            ):
                raise ValueError(f"{name} must be in [1, {upper}]")
        suffixes = tuple(self.allowed_new_path_suffixes)
        object.__setattr__(self, "allowed_new_path_suffixes", suffixes)
        if (
            not suffixes
            or suffixes != tuple(sorted(set(suffixes)))
            or any(
                not isinstance(suffix, str)
                or not suffix.startswith(".")
                or len(suffix) > 32
                for suffix in suffixes
            )
        ):
            raise ValueError(
                "allowed_new_path_suffixes must be sorted unique dotted suffixes"
            )
        if not isinstance(self.carrier_context, str):
            raise ValueError("carrier_context must be text")
        if not isinstance(self.environment_context, str):
            raise ValueError("environment_context must be text")
        if not isinstance(self.repository_map, ParentRepositoryMap):
            raise TypeError("repository_map must be a ParentRepositoryMap")
        if (
            self.repository_map.parent_id != self.parent_selection.parent_id
            or self.repository_map.commit_sha != self.parent_selection.commit_sha
        ):
            raise ValueError("repository_map belongs to another selected parent")
        if not isinstance(self.evidence_cards, tuple) or len(self.evidence_cards) > 2:
            raise ValueError("evidence_cards must be a bounded tuple")
        if any(not isinstance(item, FixedEvidenceCard) for item in self.evidence_cards):
            raise ValueError("evidence_cards must contain fixed evidence cards")
        sources = tuple(item.source for item in self.evidence_cards)
        if len(sources) != len(set(sources)) or any(
            item not in {"failure_complement", "top_score"} for item in sources
        ):
            raise ValueError("fixed evidence source labels are invalid or duplicated")
        if not isinstance(self.failure_cards, tuple) or len(self.failure_cards) > 3:
            raise ValueError("failure_cards must be a bounded tuple")
        if any(
            not isinstance(item, FailureDirectionCard) for item in self.failure_cards
        ):
            raise ValueError("failure_cards must contain typed failure cards")
        failure_refs = tuple(item.evidence_ref for item in self.failure_cards)
        if len(failure_refs) != len(set(failure_refs)):
            raise ValueError("failure_cards must not contain duplicates")
        if any(
            not isinstance(item, VisibleATaskEvidence)
            for item in self.visible_a_evidence
        ):
            raise TypeError(
                "visible_a_evidence must contain VisibleATaskEvidence values"
            )
        evidence_cell_ids = tuple(item.cell_id for item in self.visible_a_evidence)
        if len(evidence_cell_ids) != len(set(evidence_cell_ids)):
            raise ValueError("visible_a_evidence must not describe a cell twice")
        validate_visible_a_evidence_for_state(
            self.state, self.visible_a_evidence,
        )
        if self.parent_selection.state_id != self.state.state_id:
            raise ValueError("proposal parent selection belongs to another state")
        if self.parent_selection.decision_round_id != self.decision_round_id:
            raise ValueError("proposal parent selection belongs to another round")
        if self.method is PolicyMethod.MEMORY:
            if self.source_slot is not None:
                raise ValueError("Memory proposal request cannot carry a UCB slot")
            if self.arm_spec is not None:
                raise ValueError("Memory proposal request cannot carry a UCB arm spec")
        elif self.source_slot is None:
            raise ValueError("UCB proposal request requires one allocated slot")
        if self.arm_spec is not None and not isinstance(self.arm_spec, ArmSpec):
            raise TypeError("arm_spec must be ArmSpec or None")
        if self.repair_feedback is not None:
            require_text(self.repair_feedback, "repair_feedback")
        object.__setattr__(self, "metadata", deep_freeze(self.metadata))


@dataclass(frozen=True)
class StructuredProposal:
    """Typed proposer output with the provider identity needed for replay audit."""

    proposal_id: str
    request_id: str
    direction: StructuredDirection
    provider: str
    model: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    arm_spec: ArmSpec | None = None

    def __post_init__(self) -> None:
        for name in (
            "proposal_id",
            "request_id",
            "provider",
            "model",
        ):
            require_text(getattr(self, name), name)
        if self.arm_spec is not None and not isinstance(self.arm_spec, ArmSpec):
            raise TypeError("arm_spec must be ArmSpec or None")
        object.__setattr__(self, "metadata", deep_freeze(self.metadata))


class StructuredProposer(Protocol):
    """Realize one request, replaying the same result for the same request ID."""

    def propose(self, request: StructuredProposalRequest) -> StructuredProposal:
        """Return a validated direction idempotently by ``request.request_id``."""


class RepositoryMapProvider(Protocol):
    def get(
        self, *, state: SearchState, parent: ParentSelection
    ) -> ParentRepositoryMap:
        ...


class VisibleAEvidenceProvider(Protocol):
    def get(self, *, state: SearchState) -> tuple[VisibleATaskEvidence, ...]:
        ...


__all__ = [
    "VisibleAEvidenceProvider",
    "RetryableProposalError",
    "DraftDeliveryError",
    "ProposerDeferred",
    "RepositoryMapProvider",
    "StructuredProposal",
    "StructuredProposalRequest",
    "StructuredProposer",
]
