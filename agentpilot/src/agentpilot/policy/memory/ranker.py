"""Textual Policy Learning ranking conditioned on frozen Policy Memory."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agentpilot.domain import (
    DecisionTrace,
    DirectionCandidate,
    DirectionSelection,
    DirectionSlate,
    EvidenceTier,
    ParentSelection,
    ParentSlate,
    PolicyMethod,
    SearchState,
    stable_id,
)
from agentpilot.domain.common import canonical_json
from agentpilot.policy.proposer.base import StructuredProposal, StructuredProposalRequest
from agentpilot.providers import ProviderAttemptSpent
from agentpilot.search import (
    PROPOSER_SUPPLEMENTAL_EVIDENCE_KEY,
    PromptEvidence,
    ProposerSupplementalEvidence,
    decode_supplemental_evidence,
    project_prompt_evidence,
)

from .snapshot import MemorySnapshot
from .summarizer import validate_memory_document

MEMORY_RANK_CONTRACT = "indexed-top-one-ranking"
MEMORY_RANK_MAX_CANDIDATES = 64
MEMORY_RANK_MAX_PROMPT_BYTES = 65_536
MEMORY_RANK_MAX_RESPONSE_BYTES = 16_384
MEMORY_RANK_MAX_ATTEMPTS = 4

_AUDIT_ONLY_FIELDS = frozenset(
    {
        "action_id",
        "archive_seq",
        "available_at",
        "batch_id",
        "commit_sha",
        "compatibility",
        "cutoff",
        "direction_descriptor",
        "direction_id",
        "history_id",
        "parent_descriptor",
        "parent_id",
        "projection_id",
        "run_id",
        "source_action_id",
        "state_id",
        "task_id",
    }
)


class RankCompletion(Protocol):
    def complete(
        self,
        prompt: str,
        *,
        call_id: str,
        response_schema: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str: ...


class MemoryRankingError(ValueError):
    """A ranking response or its frozen inputs are invalid."""


@dataclass(frozen=True)
class MemoryProposalReceipt:
    """Planner-owned proof that a proposal belongs to this exact D2 slate."""

    receipt_id: str
    request_id: str
    proposal_id: str
    supplemental_evidence_id: str | None
    state_id: str
    slate_id: str
    decision_round_id: str
    parent_id: str
    parent_commit_sha: str
    candidate_id: str
    direction_id: str
    candidate_ordinal: int
    repair_ordinal: int

    def __post_init__(self) -> None:
        for name in (
            "receipt_id",
            "request_id",
            "proposal_id",
            "state_id",
            "slate_id",
            "decision_round_id",
            "parent_id",
            "parent_commit_sha",
            "candidate_id",
            "direction_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise MemoryRankingError(f"proposal receipt {name} must be text")
        if self.supplemental_evidence_id is not None and (
            not isinstance(self.supplemental_evidence_id, str)
            or not self.supplemental_evidence_id.strip()
        ):
            raise MemoryRankingError(
                "proposal receipt supplemental_evidence_id must be text or absent"
            )
        for name in ("candidate_ordinal", "repair_ordinal"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise MemoryRankingError(
                    f"proposal receipt {name} must be a non-negative integer"
                )
        expected = stable_id(
            "memory-proposal-receipt",
            self.request_id,
            self.proposal_id,
            self.supplemental_evidence_id,
            self.state_id,
            self.slate_id,
            self.decision_round_id,
            self.parent_id,
            self.parent_commit_sha,
            self.candidate_id,
            self.direction_id,
            self.candidate_ordinal,
            self.repair_ordinal,
        )
        if self.receipt_id != expected:
            raise MemoryRankingError("proposal receipt_id is not stable")

    def as_payload(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "request_id": self.request_id,
            "proposal_id": self.proposal_id,
            "supplemental_evidence_id": self.supplemental_evidence_id,
            "state_id": self.state_id,
            "slate_id": self.slate_id,
            "decision_round_id": self.decision_round_id,
            "parent_id": self.parent_id,
            "parent_commit_sha": self.parent_commit_sha,
            "candidate_id": self.candidate_id,
            "direction_id": self.direction_id,
            "candidate_ordinal": self.candidate_ordinal,
            "repair_ordinal": self.repair_ordinal,
        }

    @classmethod
    def create(
        cls,
        *,
        state: SearchState,
        slate: DirectionSlate,
        candidate: DirectionCandidate,
        request: StructuredProposalRequest,
        proposal: StructuredProposal,
        repair_ordinal: int,
    ) -> MemoryProposalReceipt:
        """Issue a receipt only after checking the planner's frozen request."""

        if request.method is not PolicyMethod.MEMORY or request.state != state:
            raise MemoryRankingError("proposal request belongs to another policy state")
        if slate.slate_id != stable_id(
            "memory-direction-slate", state.state_id, slate.decision_round_id
        ):
            raise MemoryRankingError("direction slate_id is not planner-owned")
        if (
            request.decision_round_id != slate.decision_round_id
            or request.parent_selection.parent_id != slate.parent_id
            or request.parent_selection.state_id != state.state_id
        ):
            raise MemoryRankingError("proposal request belongs to another D2 slate")
        if (
            isinstance(repair_ordinal, bool)
            or not isinstance(repair_ordinal, int)
            or repair_ordinal < 0
        ):
            raise MemoryRankingError("proposal repair ordinal is invalid")
        if request.metadata.get("repair_ordinal") != repair_ordinal:
            raise MemoryRankingError("proposal repair receipt differs from its request")
        expected_request_id = stable_id(
            "memory-proposal-request",
            state.run_id,
            state.batch_id,
            state.state_id,
            slate.decision_round_id,
            request.candidate_ordinal,
            repair_ordinal,
        )
        if request.request_id != expected_request_id:
            raise MemoryRankingError("proposal request_id is not planner-owned")
        if (
            proposal.request_id != request.request_id
            or proposal.direction != candidate.direction
            or candidate.candidate_id
            != stable_id(
                "memory-direction-candidate",
                request.request_id,
                proposal.direction.direction_id,
            )
            or candidate not in slate.candidates
        ):
            raise MemoryRankingError("proposal does not match its accepted candidate")
        raw_envelope = proposal.metadata.get(PROPOSER_SUPPLEMENTAL_EVIDENCE_KEY)
        if raw_envelope is not None and not isinstance(raw_envelope, Mapping):
            raise MemoryRankingError("proposal supplemental evidence is not a mapping")
        supplemental_evidence_id = (
            None if raw_envelope is None else raw_envelope.get("envelope_id")
        )
        if raw_envelope is not None and (
            not isinstance(supplemental_evidence_id, str)
            or not supplemental_evidence_id.strip()
        ):
            raise MemoryRankingError("proposal supplemental evidence has no identity")
        values = {
            "request_id": request.request_id,
            "proposal_id": proposal.proposal_id,
            "supplemental_evidence_id": supplemental_evidence_id,
            "state_id": state.state_id,
            "slate_id": slate.slate_id,
            "decision_round_id": slate.decision_round_id,
            "parent_id": slate.parent_id,
            "parent_commit_sha": request.parent_selection.commit_sha,
            "candidate_id": candidate.candidate_id,
            "direction_id": proposal.direction.direction_id,
            "candidate_ordinal": request.candidate_ordinal,
            "repair_ordinal": repair_ordinal,
        }
        receipt_id = stable_id(
            "memory-proposal-receipt",
            values["request_id"],
            values["proposal_id"],
            values["supplemental_evidence_id"],
            values["state_id"],
            values["slate_id"],
            values["decision_round_id"],
            values["parent_id"],
            values["parent_commit_sha"],
            values["candidate_id"],
            values["direction_id"],
            values["candidate_ordinal"],
            values["repair_ordinal"],
        )
        return cls(receipt_id=receipt_id, **values)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MemoryRankingError(f"ranking response duplicated JSON key {key!r}")
        result[key] = value
    return result


def _sanitize(value: Any, evidence_indexes: Mapping[str, int]) -> Any:
    if isinstance(value, Mapping):
        return {
            (
                evidence_indexes[key]
                if isinstance(key, str) and key in evidence_indexes
                else key
            ): _sanitize(item, evidence_indexes)
            for key, item in value.items()
            if key not in _AUDIT_ONLY_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, evidence_indexes) for item in value]
    if isinstance(value, str) and value in evidence_indexes:
        return evidence_indexes[value]
    return value


def _without_absolute_scores(value: Any) -> Any:
    """Keep evidence while withholding scores from different measurement tiers."""

    if isinstance(value, Mapping):
        return {
            key: _without_absolute_scores(item)
            for key, item in value.items()
            if key not in {"primary_value", "absolute_primary_value"}
            and not (key == "score" and value.get("source") == "top_score")
        }
    if isinstance(value, (list, tuple)):
        return [_without_absolute_scores(item) for item in value]
    return value


def _evidence_score_tiers(evidence: PromptEvidence) -> set[str]:
    rows = list(evidence.programs)
    if evidence.selected_parent is not None and evidence.selected_parent.program is not None:
        rows.append(evidence.selected_parent.program)
    return {row.evidence_tier.value for row in rows if row.primary_value is not None}


def parse_complete_ranking(
    response: str,
    candidate_count: int,
    *,
    max_response_bytes: int = MEMORY_RANK_MAX_RESPONSE_BYTES,
) -> tuple[int, ...]:
    if isinstance(candidate_count, bool) or candidate_count < 1:
        raise MemoryRankingError("candidate_count must be positive")
    if not isinstance(response, str) or not response.strip():
        raise MemoryRankingError("rank completion returned no text")
    if len(response.encode("utf-8")) > max_response_bytes:
        raise MemoryRankingError("rank completion exceeded the response byte bound")
    try:
        payload = json.loads(response, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as exc:
        raise MemoryRankingError("rank completion must return one JSON object") from exc
    if not isinstance(payload, dict) or set(payload) != {"ranking"}:
        raise MemoryRankingError("rank completion must contain only ranking")
    ranking = payload["ranking"]
    if (
        not isinstance(ranking, list)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in ranking)
        or len(ranking) != candidate_count
        or len(ranking) != len(set(ranking))
        or set(ranking) != set(range(candidate_count))
    ):
        raise MemoryRankingError("ranking must contain every candidate index once")
    return tuple(ranking)


def _validate_batch(snapshot: MemorySnapshot, state: SearchState) -> None:
    if (
        snapshot.run_id != state.run_id
        or snapshot.batch_id != state.batch_id
        or snapshot.state_id != state.state_id
    ):
        raise MemoryRankingError("memory snapshot belongs to a different batch state")


def _response_schema(candidate_count: int) -> Mapping[str, Any]:
    indexes = list(range(candidate_count))
    return {
        "type": "object",
        "properties": {
            "ranking": {
                "type": "array",
                "minItems": candidate_count,
                "maxItems": candidate_count,
                "items": {"type": "integer", "enum": indexes},
            }
        },
        "required": ["ranking"],
        "additionalProperties": False,
    }


def _prompt(
    *,
    stage: str,
    snapshot: MemorySnapshot,
    context: Mapping[str, Any],
    evidence: Mapping[str, Any],
    candidates: list[dict[str, Any]],
    repair: bool = False,
) -> str:
    memory = validate_memory_document(
        snapshot.content, max_chars=max(len(snapshot.content), 1)
    )
    correction = (
        "The previous response was invalid. Return the corrected ranking.\n\n"
        if repair
        else ""
    )
    request_data = {
        "policy_memory": memory,
        "context": context,
        "evidence": evidence,
        "candidates": candidates,
    }
    return (
        "# Task\n\n"
        f"Rank every {stage} candidate from best to worst. The first candidate "
        "will be selected.\n\n"
        "# Rules\n\n"
        "- Use every candidate index exactly once.\n"
        "- Do not invent, omit, or repeat an index.\n"
        "- Memory may guide ranking but cannot change the objective or candidates.\n"
        "- Absolute scores and aggregate pass rates from different evidence tiers "
        "are not directly comparable. When primary_scores_comparable is false, "
        "scores are withheld; use the observed behavior and relevant memory.\n"
        "- Return one JSON object with only the ranking field.\n\n"
        f"{correction}"
        "# Request data\n\n"
        f"{canonical_json(request_data)}"
    )


def _link_parent_indexes(value: Any, parent_indexes: Mapping[str, int]) -> Any:
    if isinstance(value, Mapping):
        result = {
            key: _link_parent_indexes(item, parent_indexes)
            for key, item in value.items()
        }
        parent_id = value.get("parent_id")
        if isinstance(parent_id, str) and parent_id in parent_indexes:
            result["candidate_index"] = parent_indexes[parent_id]
        return result
    if isinstance(value, list):
        return [_link_parent_indexes(item, parent_indexes) for item in value]
    return value


def _evidence_projection(
    evidence: PromptEvidence,
    *,
    parent_indexes: Mapping[str, int] | None = None,
    supplemental: Sequence[ProposerSupplementalEvidence] = (),
) -> tuple[Mapping[str, Any], Mapping[str, int]]:
    supplemental_refs = {item.evidence_ref for item in supplemental}
    if supplemental_refs & evidence.evidence_refs:
        raise MemoryRankingError("supplemental evidence duplicates prompt evidence")
    evidence_indexes = {
        ref: index
        for index, ref in enumerate(sorted(evidence.evidence_refs | supplemental_refs))
    }
    payload = json.loads(canonical_json(evidence.as_prompt_payload()))
    if supplemental:
        payload["proposer_session_evidence"] = [
            item.as_prompt_payload()
            for item in sorted(supplemental, key=lambda item: item.evidence_ref)
        ]
    linked = _link_parent_indexes(payload, parent_indexes or {})
    return _sanitize(linked, evidence_indexes), evidence_indexes


def _validated_supplemental_evidence(
    *,
    state: SearchState,
    slate: DirectionSlate,
    evidence: PromptEvidence,
    proposals: Sequence[StructuredProposal],
    receipts: Sequence[MemoryProposalReceipt],
) -> tuple[ProposerSupplementalEvidence, ...]:
    """Resolve only proposer-owned evidence bound to each slate candidate."""

    selected_parent = evidence.selected_parent
    if selected_parent is None or selected_parent.parent_id != slate.parent_id:
        raise MemoryRankingError("prompt evidence names another selected parent")
    if slate.slate_id != stable_id(
        "memory-direction-slate", state.state_id, slate.decision_round_id
    ):
        raise MemoryRankingError("direction slate_id is not planner-owned")
    if len(proposals) != len(slate.candidates) or len(receipts) != len(
        slate.candidates
    ):
        raise MemoryRankingError(
            "direction slate, proposals, and receipts differ in length"
        )
    resolved: dict[str, ProposerSupplementalEvidence] = {}
    seen_ordinals: set[int] = set()
    for candidate, proposal, receipt in zip(
        slate.candidates, proposals, receipts, strict=True
    ):
        if not isinstance(proposal, StructuredProposal):
            raise MemoryRankingError("direction proposal has the wrong type")
        if not isinstance(receipt, MemoryProposalReceipt):
            raise MemoryRankingError("direction proposal receipt has the wrong type")
        expected_candidate_id = stable_id(
            "memory-direction-candidate",
            proposal.request_id,
            proposal.direction.direction_id,
        )
        if (
            candidate.candidate_id != expected_candidate_id
            or candidate.direction != proposal.direction
            or receipt.request_id != proposal.request_id
            or receipt.proposal_id != proposal.proposal_id
            or receipt.state_id != state.state_id
            or receipt.slate_id != slate.slate_id
            or receipt.decision_round_id != slate.decision_round_id
            or receipt.parent_id != slate.parent_id
            or receipt.parent_commit_sha != selected_parent.commit_sha
            or receipt.candidate_id != candidate.candidate_id
            or receipt.direction_id != candidate.direction.direction_id
        ):
            raise MemoryRankingError(
                "direction proposal or receipt belongs to another candidate"
            )
        expected_request_id = stable_id(
            "memory-proposal-request",
            state.run_id,
            state.batch_id,
            state.state_id,
            slate.decision_round_id,
            receipt.candidate_ordinal,
            receipt.repair_ordinal,
        )
        if proposal.request_id != expected_request_id:
            raise MemoryRankingError("direction proposal request is not planner-owned")
        if receipt.candidate_ordinal in seen_ordinals:
            raise MemoryRankingError("direction proposal receipt reused an ordinal")
        seen_ordinals.add(receipt.candidate_ordinal)
        required = frozenset(candidate.direction.evidence_refs) - evidence.evidence_refs
        raw_envelope = proposal.metadata.get(PROPOSER_SUPPLEMENTAL_EVIDENCE_KEY)
        actual_evidence_id = (
            None
            if not isinstance(raw_envelope, Mapping)
            else raw_envelope.get("envelope_id")
        )
        if receipt.supplemental_evidence_id != actual_evidence_id:
            raise MemoryRankingError(
                "proposal supplemental evidence differs from its planner receipt"
            )
        session_id = proposal.metadata.get("session_id")
        if raw_envelope is not None:
            if not isinstance(session_id, str) or not session_id.strip():
                raise MemoryRankingError(
                    "supplemental evidence has no trusted proposer session"
                )
            if proposal.proposal_id != stable_id("structured-proposal", session_id):
                raise MemoryRankingError(
                    "supplemental evidence proposal_id is not session-bound"
                )
        elif required:
            raise MemoryRankingError("supplemental evidence envelope is missing")
        try:
            records = decode_supplemental_evidence(
                proposal.metadata,
                request_id=proposal.request_id,
                session_id=session_id if isinstance(session_id, str) else "absent",
                proposal_id=proposal.proposal_id,
                state_id=state.state_id,
                parent_id=slate.parent_id,
                parent_commit_sha=selected_parent.commit_sha,
                slate_id=slate.slate_id,
                candidate_id=candidate.candidate_id,
                direction_id=candidate.direction.direction_id,
                required_refs=required,
            )
        except (TypeError, ValueError) as exc:
            raise MemoryRankingError(
                f"invalid proposer supplemental evidence: {exc}"
            ) from exc
        for record in records:
            previous = resolved.get(record.evidence_ref)
            if previous is not None and canonical_json(previous) != canonical_json(
                record
            ):
                raise MemoryRankingError(
                    "proposer supplemental evidence conflicts across candidates"
                )
            resolved[record.evidence_ref] = record
    return tuple(resolved[ref] for ref in sorted(resolved))


def _rank_values(ranking: Sequence[str]) -> dict[str, float]:
    size = len(ranking)
    return {
        candidate_id: float(size - index) for index, candidate_id in enumerate(ranking)
    }


class MemoryRanker:
    """Rank a frozen slate and deterministically select its first candidate."""

    def __init__(self, completion: RankCompletion):
        if not callable(getattr(completion, "complete", None)):
            raise TypeError("rank completion must provide durable complete()")
        self._completion = completion

    def rank_research(self, *, snapshot, slate_id, stage, candidate_ids, candidates, evidence, objective):
        """Rank immutable research choices with the same bounded Textual contract."""
        return self._complete_ranking(stage=stage, snapshot=snapshot, slate_id=slate_id,
            candidate_ids=candidate_ids, candidates=candidates, evidence=evidence,
            context={"primary_scores_comparable": False, "setting": "research investment",
                     "optimization": objective,
                     "observations_are_not_causal_proofs": True})

    def _complete_ranking(
        self,
        *,
        stage: str,
        snapshot: MemorySnapshot,
        slate_id: str,
        candidate_ids: tuple[str, ...],
        context: Mapping[str, Any],
        evidence: Mapping[str, Any],
        candidates: list[dict[str, Any]],
    ) -> tuple[str, ...]:
        if not candidate_ids or len(candidate_ids) > MEMORY_RANK_MAX_CANDIDATES:
            raise MemoryRankingError(
                f"ranking candidate count must be in [1, {MEMORY_RANK_MAX_CANDIDATES}]"
            )
        if len(candidate_ids) == 1:
            return candidate_ids
        schema = _response_schema(len(candidate_ids))
        last_error: MemoryRankingError | None = None
        for attempt in range(MEMORY_RANK_MAX_ATTEMPTS):
            prompt = _prompt(
                stage=stage,
                snapshot=snapshot,
                context=context,
                evidence=evidence,
                candidates=candidates,
                repair=attempt > 0,
            )
            prompt_bytes = len(prompt.encode("utf-8"))
            if prompt_bytes > MEMORY_RANK_MAX_PROMPT_BYTES:
                raise MemoryRankingError(
                    "ranking prompt exceeds the frozen byte budget "
                    f"({prompt_bytes} > {MEMORY_RANK_MAX_PROMPT_BYTES})"
                )
            call_id = stable_id(
                f"memory-{stage}-ranking",
                MEMORY_RANK_CONTRACT,
                slate_id,
                attempt,
            )
            try:
                response = self._completion.complete(
                    prompt,
                    call_id=call_id,
                    response_schema=schema,
                    metadata={
                        "contract": MEMORY_RANK_CONTRACT,
                        "stage": stage,
                        "attempt": attempt,
                    },
                )
            except ProviderAttemptSpent as exc:
                last_error = MemoryRankingError(str(exc))
                continue
            try:
                indexes = parse_complete_ranking(response, len(candidate_ids))
            except MemoryRankingError as exc:
                last_error = exc
                continue
            return tuple(candidate_ids[index] for index in indexes)
        assert last_error is not None
        raise last_error

    def rank_parents(
        self,
        snapshot: MemorySnapshot,
        state: SearchState,
        slate: ParentSlate,
    ) -> ParentSelection:
        _validate_batch(snapshot, state)
        if slate.state_id != state.state_id:
            raise MemoryRankingError("parent slate belongs to a different state")
        candidate_ids = tuple(item.parent_id for item in slate.candidates)
        prompt_evidence = project_prompt_evidence(state=state)
        candidate_tiers = tuple(
            item.metadata.get("primary_evidence_tier") for item in slate.candidates
        )
        known_tiers = {EvidenceTier.VISIBLE_A.value, EvidenceTier.ADAPTIVE_B.value}
        scores_comparable = (
            all(isinstance(tier, str) and tier in known_tiers for tier in candidate_tiers)
            and len(set(candidate_tiers) | _evidence_score_tiers(prompt_evidence)) == 1
        )
        context = {
            "budget": dict(state.budget),
            "descriptor": state.parent_descriptor.blocks,
            "omitted_program_count": prompt_evidence.omitted_program_count,
            "optimization": f"{state.compatibility.metric_direction} primary_value",
            "primary_scores_comparable": scores_comparable,
        }
        candidates = [
            {
                "index": index,
                "primary_value": item.primary_value,
                "primary_evidence_tier": candidate_tiers[index],
                "descriptor": item.descriptor.blocks,
            }
            for index, item in enumerate(slate.candidates)
        ]
        evidence, _ = _evidence_projection(
            prompt_evidence,
            parent_indexes={
                parent_id: index for index, parent_id in enumerate(candidate_ids)
            },
        )
        if not scores_comparable:
            context = _without_absolute_scores(context)
            candidates = _without_absolute_scores(candidates)
            evidence = _without_absolute_scores(evidence)
        ranking = self._complete_ranking(
            stage="parent",
            snapshot=snapshot,
            slate_id=slate.slate_id,
            candidate_ids=candidate_ids,
            context=context,
            evidence=evidence,
            candidates=candidates,
        )
        selected_id = ranking[0]
        selected = next(
            item for item in slate.candidates if item.parent_id == selected_id
        )
        return ParentSelection(
            selection_id=stable_id(
                "memory-parent-selection", slate.slate_id, selected_id
            ),
            slate_id=slate.slate_id,
            decision_round_id=slate.decision_round_id,
            state_id=state.state_id,
            parent_id=selected.parent_id,
            commit_sha=selected.commit_sha,
            trace=DecisionTrace(
                selected_id=selected_id,
                eligible_ids=candidate_ids,
                tie_ids=(selected_id,),
                seed=None,
                conditional_probability=1.0,
                indices=_rank_values(ranking),
            ),
        )

    def rank_directions(
        self,
        snapshot: MemorySnapshot,
        state: SearchState,
        slate: DirectionSlate,
        *,
        evidence: PromptEvidence,
        proposals: Sequence[StructuredProposal],
        receipts: Sequence[MemoryProposalReceipt],
    ) -> DirectionSelection:
        _validate_batch(snapshot, state)
        if slate.state_id != state.state_id or evidence.state_id != state.state_id:
            raise MemoryRankingError("direction inputs belong to another state")
        selected_parent = evidence.selected_parent
        if selected_parent is None or selected_parent.parent_id != slate.parent_id:
            raise MemoryRankingError("prompt evidence names another selected parent")
        candidate_ids = tuple(item.candidate_id for item in slate.candidates)
        if not candidate_ids:
            raise MemoryRankingError("direction slate must not be empty")
        supplemental = _validated_supplemental_evidence(
            state=state,
            slate=slate,
            evidence=evidence,
            proposals=proposals,
            receipts=receipts,
        )
        allowed_refs = evidence.evidence_refs | {
            item.evidence_ref for item in supplemental
        }
        cited = {
            ref for item in slate.candidates for ref in item.direction.evidence_refs
        }
        unresolved = cited - allowed_refs
        if unresolved:
            raise MemoryRankingError(
                f"direction slate cites unknown evidence: {sorted(unresolved)}"
            )
        if len(slate.candidates) == 1:
            selected = slate.candidates[0]
            return DirectionSelection(
                selection_id=stable_id(
                    "memory-direction-selection", slate.slate_id, selected.candidate_id
                ),
                slate_id=slate.slate_id,
                candidate_id=selected.candidate_id,
                direction_id=selected.direction.direction_id,
                trace={
                    "method": "memory",
                    "selection": "only_candidate",
                    "ranking": [selected.candidate_id],
                },
            )
        evidence_payload, evidence_indexes = _evidence_projection(
            evidence, supplemental=supplemental
        )
        candidates = [
            {
                "index": index,
                "direction": {
                    "mechanism": item.direction.failure_mechanism,
                    "recommended_change": item.direction.implementation_direction,
                    "declared_scope": [
                        {
                            "kind": target.kind.value,
                            "path": target.path,
                            "symbols": list(target.symbols),
                        }
                        for target in item.direction.declared_scope
                    ],
                    "new_paths": [
                        {"path": target.path, "role": target.role}
                        for target in item.direction.new_paths
                    ],
                    "resolved_scope": list(item.direction.resolved_scope),
                    "evidence_indexes": [
                        evidence_indexes[ref] for ref in item.direction.evidence_refs
                    ],
                },
            }
            for index, item in enumerate(slate.candidates)
        ]
        context = {
            "budget": dict(state.budget),
            "descriptor": state.direction_descriptor.blocks,
            "omitted_program_count": evidence.omitted_program_count,
            "optimization": f"{state.compatibility.metric_direction} primary_value",
            "primary_scores_comparable": len(_evidence_score_tiers(evidence)) == 1,
        }
        if not context["primary_scores_comparable"]:
            context = _without_absolute_scores(context)
            evidence_payload = _without_absolute_scores(evidence_payload)
        ranking = self._complete_ranking(
            stage="direction",
            snapshot=snapshot,
            slate_id=slate.slate_id,
            candidate_ids=candidate_ids,
            context=context,
            evidence=evidence_payload,
            candidates=candidates,
        )
        selected_id = ranking[0]
        selected = next(
            item for item in slate.candidates if item.candidate_id == selected_id
        )
        return DirectionSelection(
            selection_id=stable_id(
                "memory-direction-selection", slate.slate_id, selected_id
            ),
            slate_id=slate.slate_id,
            candidate_id=selected.candidate_id,
            direction_id=selected.direction.direction_id,
            trace={
                "method": "memory",
                "selection": "top_one",
                "ranking": list(ranking),
            },
        )


__all__ = [
    "MEMORY_RANK_CONTRACT",
    "MEMORY_RANK_MAX_ATTEMPTS",
    "MEMORY_RANK_MAX_CANDIDATES",
    "MEMORY_RANK_MAX_PROMPT_BYTES",
    "MEMORY_RANK_MAX_RESPONSE_BYTES",
    "MemoryProposalReceipt",
    "MemoryRanker",
    "MemoryRankingError",
    "RankCompletion",
    "parse_complete_ranking",
]
