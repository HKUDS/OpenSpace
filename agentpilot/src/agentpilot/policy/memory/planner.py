"""Complete D1/D2/D3 ActionPlanner and updater for Textual Policy Learning."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from agentpilot.domain import (
    DirectionCandidate,
    DirectionSlate,
    EvidenceCutoff,
    ParentSlate,
    PlanningFailureEvent,
    PlanningRoundFailed,
    PlanningSlotAttempt,
    PolicyAction,
    PolicyDecisionPlan,
    PolicyMethod,
    PolicyObservation,
    SearchState,
    SlotAdherenceStatus,
    SlotKind,
    StructuredDirection,
    canonical_json,
    stable_id,
    utc_now,
)
from agentpilot.policy.proposer import (
    RepositoryMapProvider,
    RetryableProposalError,
    StructuredProposal,
    StructuredProposalRequest,
    StructuredProposer,
    VisibleAEvidenceProvider,
    validate_direction_targets,
)
from agentpilot.policy.proposer.base import ProposerDeferred
from agentpilot.search import (
    FailureCardSelection,
    FailureDirectionCard,
    FixedEvidenceSelection,
    canonical_direction_input,
    project_prompt_evidence,
)

from .barrier import BarrierExecutionResult, build_memory_barrier_facts
from .carrier import SingleFileMemoryCarrier
from .ranker import MemoryProposalReceipt
from .snapshot import MemorySnapshot

MEMORY_PROPOSAL_MAX_ATTEMPTS = 2


class ParentSlateBuilder(Protocol):
    """Build the fixed eligible D1 slate; it is not another learned policy."""

    def build(self, *, state: SearchState, decision_round_id: str) -> ParentSlate:
        ...


class EvidenceRetriever(Protocol):
    """Retrieve bounded carrier-neutral cards for one selected parent."""

    def retrieve(
        self, *, state: SearchState, selected_parent_id: str
    ) -> FixedEvidenceSelection:
        ...


class FailureCardRetriever(Protocol):
    """Retrieve batch-frozen carrier-neutral failures for the selected parent."""

    def retrieve(
        self, *, state: SearchState, selected_parent_id: str
    ) -> FailureCardSelection:
        ...


class MemoryCarrierUpdate(Protocol):
    """Complete structural view supplied by the application barrier.

    ``plans`` and ``execution_results`` are part of the contract because the
    summary must describe the selected direction and its execution, not just
    the labels carried by ``PolicyObservation``.
    """

    update_id: str
    state: SearchState
    carrier_snapshot: Mapping[str, Any]
    plans: tuple[PolicyDecisionPlan, ...]
    execution_results: tuple[BarrierExecutionResult, ...]
    observations: tuple[PolicyObservation, ...]


@dataclass(frozen=True)
class MemoryPlannedAction:
    """Structural match for the app PlannedAction without a policy→app import."""

    plan: PolicyDecisionPlan
    semantic_reservation: None = None

    @property
    def action(self) -> PolicyAction:
        return self.plan.action

    @property
    def reservation(self) -> None:
        return None


def _proposal_trace(proposal: StructuredProposal) -> dict[str, Any]:
    return {
        "proposal_id": proposal.proposal_id,
        "request_id": proposal.request_id,
        "direction_id": proposal.direction.direction_id,
        "provider": proposal.provider,
        "model": proposal.model,
        "metadata": dict(proposal.metadata),
    }


def _normalized_text(value: str) -> str:
    return " ".join(value.lower().split())


def _direction_history_signature(direction: StructuredDirection,) -> tuple[object, ...]:
    """Exact normalized identity for Textual's non-improving retry gate."""

    return (
        _normalized_text(direction.failure_mechanism),
        _normalized_text(direction.implementation_direction),
        direction.resolved_scope,
        tuple((item.path, _normalized_text(item.role)) for item in direction.new_paths),
    )


def _failure_history_signature(card: FailureDirectionCard,) -> tuple[object, ...]:
    return (
        _normalized_text(card.failure_mechanism),
        _normalized_text(card.implementation_direction),
        card.resolved_scope,
        tuple((item.path, _normalized_text(item.role)) for item in card.new_paths),
    )


class MemoryPolicyAdapter:
    """Use frozen Policy Memory for D1 ranking, D2 proposal and D3 ranking."""

    method = PolicyMethod.MEMORY

    def __init__(
        self,
        carrier: SingleFileMemoryCarrier,
        *,
        parent_slate_builder: ParentSlateBuilder,
        evidence_retriever: EvidenceRetriever,
        failure_card_retriever: FailureCardRetriever,
        repository_map_provider: RepositoryMapProvider,
        proposer: StructuredProposer,
        proposal_count: int,
        max_planning_attempts: int,
        max_scope_refs: int,
        max_new_paths: int,
        max_resolved_scope_files: int,
        max_resolved_scope_bytes: int,
        visible_a_evidence_provider: VisibleAEvidenceProvider | None = None,
        environment_context: str = "",
    ) -> None:
        if (
            isinstance(proposal_count, bool)
            or not isinstance(proposal_count, int)
            or proposal_count < 1
        ):
            raise ValueError("proposal_count must be a positive integer")
        if (
            isinstance(max_planning_attempts, bool)
            or not isinstance(max_planning_attempts, int)
            or max_planning_attempts < 1
        ):
            raise ValueError("max_planning_attempts must be a positive integer")
        if not isinstance(environment_context, str):
            raise ValueError("environment_context must be text")
        for name, value, upper in (
            ("max_scope_refs", max_scope_refs, 8),
            ("max_new_paths", max_new_paths, 4),
            ("max_resolved_scope_files", max_resolved_scope_files, 4_096),
            ("max_resolved_scope_bytes", max_resolved_scope_bytes, 64 * 1024 * 1024,),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= upper
            ):
                raise ValueError(f"{name} must be in [1, {upper}]")
        if not callable(getattr(parent_slate_builder, "build", None)):
            raise ValueError("parent_slate_builder must provide build()")
        if not callable(getattr(proposer, "propose", None)):
            raise ValueError("proposer must provide propose()")
        if not callable(getattr(evidence_retriever, "retrieve", None)):
            raise ValueError("evidence_retriever must provide retrieve()")
        if not callable(getattr(failure_card_retriever, "retrieve", None)):
            raise ValueError("failure_card_retriever must provide retrieve()")
        if not callable(getattr(repository_map_provider, "get", None)):
            raise ValueError("repository_map_provider must provide get()")
        if visible_a_evidence_provider is not None and not callable(
            getattr(visible_a_evidence_provider, "get", None)
        ):
            raise ValueError("visible_a_evidence_provider must provide get()")
        self.carrier = carrier
        self.parent_slate_builder = parent_slate_builder
        self.evidence_retriever = evidence_retriever
        self.failure_card_retriever = failure_card_retriever
        self.repository_map_provider = repository_map_provider
        self.visible_a_evidence_provider = visible_a_evidence_provider
        self.proposer = proposer
        self.proposal_count = proposal_count
        self.max_planning_attempts = max_planning_attempts
        self.environment_context = environment_context
        self.max_scope_refs = max_scope_refs
        self.max_new_paths = max_new_paths
        self.max_resolved_scope_files = max_resolved_scope_files
        self.max_resolved_scope_bytes = max_resolved_scope_bytes

    def begin_batch(self, state: SearchState) -> MemorySnapshot:
        return self.carrier.begin_batch(state)

    def freeze_snapshot(self, snapshot: object) -> Mapping[str, Any]:
        if not isinstance(snapshot, MemorySnapshot):
            raise ValueError("Memory planner received another carrier snapshot")
        return self.carrier.freeze_snapshot(snapshot)

    def restore_snapshot(self, payload: Mapping[str, Any]) -> MemorySnapshot:
        return self.carrier.restore_snapshot(payload)

    def plan_round(
        self,
        *,
        state: SearchState,
        carrier_snapshot: object,
        decision_round_id: str,
        round_index: int,
        planning_attempt_index: int,
        pending: tuple[object, ...],
    ) -> MemoryPlannedAction:
        if not isinstance(carrier_snapshot, MemorySnapshot):
            raise ValueError("Memory planner received another carrier snapshot")
        if pending:
            raise ValueError("Memory planning cannot consume UCB reservations")
        if (
            isinstance(round_index, bool)
            or not isinstance(round_index, int)
            or round_index < 0
        ):
            raise ValueError("round_index must be a non-negative integer")
        if (
            isinstance(planning_attempt_index, bool)
            or not isinstance(planning_attempt_index, int)
            or not 0 <= planning_attempt_index < self.max_planning_attempts
        ):
            raise ValueError("Memory planning attempt index is outside its budget")

        parent_slate = self.parent_slate_builder.build(
            state=state, decision_round_id=decision_round_id
        )
        if parent_slate.decision_round_id != decision_round_id:
            raise ValueError("parent slate builder returned another decision round")
        parent_selection = self.carrier.rank_parents(
            carrier_snapshot, state, parent_slate
        )
        repository_map = self.repository_map_provider.get(
            state=state, parent=parent_selection
        )
        visible_a_evidence = (
            ()
            if self.visible_a_evidence_provider is None
            else tuple(self.visible_a_evidence_provider.get(state=state))
        )
        generation_context = self.carrier.generation_context(
            carrier_snapshot, state, parent_id=parent_selection.parent_id,
        )
        evidence = self.evidence_retriever.retrieve(
            state=state, selected_parent_id=parent_selection.parent_id,
        )
        if evidence.selected_parent_id != parent_selection.parent_id:
            raise ValueError("fixed evidence belongs to another selected parent")
        if len(evidence.cards) > 2:
            raise ValueError("fixed evidence retrieval exceeded its bounded channels")
        failures = self.failure_card_retriever.retrieve(
            state=state, selected_parent_id=parent_selection.parent_id,
        )
        if failures.selected_parent_id != parent_selection.parent_id:
            raise ValueError("failure cards belong to another selected parent")
        if len(failures.cards) > 3:
            raise ValueError("failure-card retrieval exceeded its fixed bound")
        # D2 and D3 read the same bounded projection, so D3 can resolve every
        # evidence reference a proposed direction cites.
        prompt_evidence = project_prompt_evidence(
            state=state,
            selected_parent=parent_selection,
            fixed_evidence=evidence.cards,
            failure_history=failures.cards,
            visible_a_evidence=visible_a_evidence,
        )

        proposals: list[StructuredProposal] = []
        proposal_requests: list[StructuredProposalRequest] = []
        proposal_repair_ordinals: list[int] = []
        candidates: list[DirectionCandidate] = []
        # Keyed by request-independent content so a repeat is detected even
        # though each candidate ordinal uses its own request and direction id.
        seen_content: dict[str, str] = {}
        historical = {_failure_history_signature(card): card for card in failures.cards}
        planning_attempts: list[PlanningSlotAttempt] = []
        skipped: list[dict[str, Any]] = []
        for ordinal in range(self.proposal_count):
            accepted: StructuredProposal | None = None
            accepted_request: StructuredProposalRequest | None = None
            accepted_repair_ordinal: int | None = None
            repair_feedback: str | None = None
            deferred: ProposerDeferred | None = None
            for repair_ordinal in range(MEMORY_PROPOSAL_MAX_ATTEMPTS):
                request_id = stable_id(
                    "memory-proposal-request",
                    state.run_id,
                    state.batch_id,
                    state.state_id,
                    decision_round_id,
                    ordinal,
                    repair_ordinal,
                )
                request = StructuredProposalRequest(
                    request_id=request_id,
                    method=self.method,
                    state=state,
                    parent_selection=parent_selection,
                    decision_round_id=decision_round_id,
                    candidate_ordinal=ordinal,
                    max_scope_refs=self.max_scope_refs,
                    max_new_paths=self.max_new_paths,
                    max_resolved_scope_files=self.max_resolved_scope_files,
                    max_resolved_scope_bytes=self.max_resolved_scope_bytes,
                    carrier_context=generation_context.content,
                    repository_map=repository_map,
                    environment_context=self.environment_context,
                    evidence_cards=evidence.cards,
                    failure_cards=failures.cards,
                    visible_a_evidence=visible_a_evidence,
                    source_slot=None,
                    repair_feedback=repair_feedback,
                    metadata={
                        "round_index": round_index,
                        "planning_attempt_index": planning_attempt_index,
                        "repair_ordinal": repair_ordinal,
                        "prior_slate_directions": [
                            {
                                "failure_mechanism": item.direction.failure_mechanism[:200],
                                "implementation_direction": item.direction.implementation_direction[:400],
                            }
                            for item in proposals[-4:]
                        ],
                    },
                )
                try:
                    proposal = self.proposer.propose(request)
                    validate_direction_targets(proposal.direction, repository_map)
                except ProposerDeferred as exc:
                    deferred = exc
                    planning_attempts.append(self._proposal_attempt(
                        decision_round_id=decision_round_id, ordinal=ordinal,
                        repair_ordinal=repair_ordinal, request_id=request_id,
                        status=SlotAdherenceStatus.GENERATION_FAILED, reason=str(exc),
                    ))
                    break
                except RetryableProposalError as exc:
                    detail = " ".join(str(exc).split())[:1_024]
                    planning_attempts.append(
                        self._proposal_attempt(
                            decision_round_id=decision_round_id,
                            ordinal=ordinal,
                            repair_ordinal=repair_ordinal,
                            request_id=request_id,
                            status=SlotAdherenceStatus.GENERATION_FAILED,
                            reason=(
                                f"retryable-proposal-error:{type(exc).__name__}"
                                + (f": {detail}" if detail else "")
                            ),
                        )
                    )
                    if repair_ordinal + 1 == MEMORY_PROPOSAL_MAX_ATTEMPTS:
                        self._raise_planning_failure(
                            state=state,
                            decision_round_id=decision_round_id,
                            round_index=round_index,
                            planning_attempt_index=planning_attempt_index,
                            failure_kind="proposal_repair_exhausted",
                            attempts=planning_attempts,
                        )
                    repair_feedback = (
                        "generation_failed: retryable-proposal-error:"
                        f"{type(exc).__name__}"
                        + (f": {detail}" if detail else "")
                        + "; return one exact-schema, "
                        "policy-context-compliant direction"
                    )
                    continue
                if proposal.request_id != request.request_id:
                    raise ValueError("structured proposal references another request")
                content = canonical_direction_input(proposal.direction)
                duplicated = seen_content.get(content)
                if duplicated is not None:
                    planning_attempts.append(
                        self._proposal_attempt(
                            decision_round_id=decision_round_id,
                            ordinal=ordinal,
                            repair_ordinal=repair_ordinal,
                            request_id=request_id,
                            status=SlotAdherenceStatus.HARD_REPEAT,
                            reason=f"same-slate exact repeat of {duplicated}",
                            proposal=proposal,
                        )
                    )
                    repair_feedback = (
                        "duplicate_direction: this slate already realized an "
                        f"identical direction as candidate {duplicated}; propose a "
                        "materially different implementation mechanism or target "
                        "family, not a paraphrase"
                    )
                    continue
                prior = historical.get(_direction_history_signature(proposal.direction))
                if prior is not None:
                    planning_attempts.append(
                        self._proposal_attempt(
                            decision_round_id=decision_round_id,
                            ordinal=ordinal,
                            repair_ordinal=repair_ordinal,
                            request_id=request_id,
                            status=SlotAdherenceStatus.HARD_REPEAT,
                            reason=(
                                "exact repeat of non-improving direction "
                                f"{prior.direction_id}"
                            ),
                            proposal=proposal,
                        )
                    )
                    repair_feedback = canonical_json(
                        {
                            "rejection": "historical_non_improving_repeat",
                            "reason": prior.outcome_reason,
                            "rejected_direction": self._direction_summary(
                                proposal.direction
                            ),
                            "instruction": (
                                "change the D3 mechanism or target family; do not "
                                "paraphrase this non-improving direction"
                            ),
                        }
                    )
                    continue
                accepted = proposal
                accepted_request = request
                accepted_repair_ordinal = repair_ordinal
                break
            if accepted is None:
                # A repeat or an explicit defer shrinks the slate. Neither is
                # a measured task failure or a Bernoulli reward observation.
                skipped.append(
                    {
                        "candidate_ordinal": ordinal,
                        "reason": "proposer_deferred" if deferred else "duplicate_direction",
                        **({"defer": deferred.as_payload()} if deferred else {}),
                    }
                )
                continue
            candidate_id = stable_id(
                "memory-direction-candidate",
                accepted.request_id,
                accepted.direction.direction_id,
            )
            seen_content[canonical_direction_input(accepted.direction)] = candidate_id
            proposals.append(accepted)
            if accepted_request is None or accepted_repair_ordinal is None:
                raise RuntimeError("accepted Memory proposal has no request receipt")
            proposal_requests.append(accepted_request)
            proposal_repair_ordinals.append(accepted_repair_ordinal)
            candidates.append(
                DirectionCandidate(
                    candidate_id=candidate_id,
                    source_slot=None,
                    direction=accepted.direction,
                    match=None,
                    adherence=None,
                )
            )

        if not candidates:
            self._raise_planning_failure(
                state=state,
                decision_round_id=decision_round_id,
                round_index=round_index,
                planning_attempt_index=planning_attempt_index,
                failure_kind="no_distinct_direction",
                attempts=planning_attempts,
            )
        contents = [canonical_direction_input(item.direction) for item in candidates]
        if len(contents) != len(set(contents)):
            raise ValueError("Memory D2 slate contains a duplicate realized direction")
        direction_slate = DirectionSlate(
            slate_id=stable_id(
                "memory-direction-slate", state.state_id, decision_round_id
            ),
            decision_round_id=decision_round_id,
            state_id=state.state_id,
            parent_id=parent_selection.parent_id,
            candidates=tuple(candidates),
        )
        proposal_receipts = tuple(
            MemoryProposalReceipt.create(
                state=state,
                slate=direction_slate,
                candidate=candidate,
                request=request,
                proposal=proposal,
                repair_ordinal=repair_ordinal,
            )
            for candidate, request, proposal, repair_ordinal in zip(
                candidates,
                proposal_requests,
                proposals,
                proposal_repair_ordinals,
                strict=True,
            )
        )
        direction_selection = self.carrier.rank_directions(
            carrier_snapshot,
            state,
            direction_slate,
            evidence=prompt_evidence,
            proposals=proposals,
            receipts=proposal_receipts,
        )
        selected = next(
            item
            for item in direction_slate.candidates
            if item.candidate_id == direction_selection.candidate_id
        )
        implementation_evidence = next(
            item.metadata.get("implementation_evidence", {}) for item in proposals
            if item.direction.direction_id == selected.direction.direction_id
        )
        action_id = stable_id(
            "memory-action", state.run_id, state.batch_id, decision_round_id
        )
        action = PolicyAction(
            action_id=action_id,
            method=self.method,
            run_id=state.run_id,
            batch_id=state.batch_id,
            decision_round_id=decision_round_id,
            state_id=state.state_id,
            parent_id=parent_selection.parent_id,
            parent_commit_sha=parent_selection.commit_sha,
            direction=selected.direction,
            source_slot=None,
            arm_id=None,
            direction_match=None,
            created_at=utc_now(),
            d1_trace={
                "selection_id": parent_selection.selection_id,
                "selection": "top_one",
                "ranking": [
                    parent_id
                    for parent_id, _value in sorted(
                        parent_selection.trace.indices.items(),
                        key=lambda item: item[1],
                        reverse=True,
                    )
                ],
            },
            d2_trace={
                **({"implementation_evidence": implementation_evidence} if implementation_evidence else {}),
                "memory_injected": True,
                "snapshot_batch_id": carrier_snapshot.batch_id,
                "fixed_evidence": [
                    {
                        "source": card.source,
                        "parent_id": card.parent_id,
                        "commit_sha": card.commit_sha,
                        "primary_value": card.primary_value,
                        "score": card.score,
                        "evidence_cells": list(card.evidence_cells),
                    }
                    for card in evidence.cards
                ],
                "failure_history": {
                    "retriever_id": failures.retriever_id,
                    "cards": [card.as_prompt_payload() for card in failures.cards],
                },
                "proposals": [_proposal_trace(item) for item in proposals],
                "proposal_receipts": [
                    receipt.as_payload() for receipt in proposal_receipts
                ],
                "skipped_candidates": skipped,
            },
            d3_trace=dict(direction_selection.trace),
        )
        plan = PolicyDecisionPlan(
            plan_id=stable_id("memory-decision-plan", action_id),
            method=self.method,
            parent_slate=parent_slate,
            parent_selection=parent_selection,
            arm_allocation=None,
            direction_slate=direction_slate,
            direction_selection=direction_selection,
            action=action,
        )
        return MemoryPlannedAction(plan=plan)

    @staticmethod
    def _direction_summary(direction: StructuredDirection) -> dict[str, object]:
        return {
            "failure_mechanism": direction.failure_mechanism,
            "implementation_direction": direction.implementation_direction,
            "declared_scope": [
                {
                    "kind": item.kind.value,
                    "path": item.path,
                    "symbols": list(item.symbols),
                }
                for item in direction.declared_scope
            ],
            "new_paths": [
                {"path": item.path, "role": item.role} for item in direction.new_paths
            ],
            "resolved_scope": list(direction.resolved_scope),
        }

    @staticmethod
    def _proposal_attempt(
        *,
        decision_round_id: str,
        ordinal: int,
        repair_ordinal: int,
        request_id: str,
        status: SlotAdherenceStatus,
        reason: str,
        proposal: StructuredProposal | None = None,
    ) -> PlanningSlotAttempt:
        return PlanningSlotAttempt(
            request_id=request_id,
            source_slot_id=stable_id(
                "memory-proposal-slot", decision_round_id, ordinal
            ),
            source_slot_kind=SlotKind.DISCOVER,
            source_arm_id=None,
            slot_ordinal=ordinal,
            repair_ordinal=repair_ordinal,
            status=status,
            reason=reason,
            proposal_id=None if proposal is None else proposal.proposal_id,
            direction_id=(
                None if proposal is None else proposal.direction.direction_id
            ),
            failure_mechanism=(
                None if proposal is None else proposal.direction.failure_mechanism
            ),
            implementation_direction=(
                None
                if proposal is None
                else proposal.direction.implementation_direction
            ),
            resolved_scope=(
                () if proposal is None else proposal.direction.resolved_scope
            ),
        )

    def _raise_planning_failure(
        self,
        *,
        state: SearchState,
        decision_round_id: str,
        round_index: int,
        planning_attempt_index: int,
        failure_kind: str,
        attempts: list[PlanningSlotAttempt],
    ) -> None:
        if not attempts:
            raise RuntimeError("Memory planning failure has no proposer attempts")
        available_at = utc_now()
        if available_at <= state.cutoff.available_at:
            available_at = state.cutoff.available_at + timedelta(microseconds=1)
        event = PlanningFailureEvent(
            event_id=stable_id(
                "planning-failure",
                state.batch_id,
                round_index,
                planning_attempt_index,
                self.method.value,
            ),
            run_id=state.run_id,
            batch_id=state.batch_id,
            state_id=state.state_id,
            decision_round_id=decision_round_id,
            round_index=round_index,
            planning_attempt_index=planning_attempt_index,
            method=self.method,
            failure_kind=failure_kind,
            slot_attempts=tuple(attempts),
            available_at=available_at,
            compatibility=state.compatibility,
        )
        raise PlanningRoundFailed(event, failure_kind)

    def update(self, batch: MemoryCarrierUpdate) -> None:
        state = batch.state
        observations = batch.observations
        expected_id = stable_id("carrier-update", state.batch_id)
        if batch.update_id != expected_id:
            raise ValueError("Memory update_id is not stable for the batch")
        carrier_snapshot = self.restore_snapshot(batch.carrier_snapshot)
        if (
            carrier_snapshot.run_id != state.run_id
            or carrier_snapshot.batch_id != state.batch_id
            or carrier_snapshot.state_id != state.state_id
        ):
            raise ValueError("memory snapshot belongs to another batch state")
        if any(item.method is not PolicyMethod.MEMORY for item in observations):
            raise ValueError(
                "Memory updater cannot consume another carrier observation"
            )
        if not observations:
            raise ValueError("Memory updater needs completed batch observations")
        if any(plan.action.state_id != state.state_id for plan in batch.plans):
            raise ValueError("Memory update plan coordinates drifted")
        facts = build_memory_barrier_facts(
            plans=tuple(batch.plans),
            execution_results=tuple(batch.execution_results),
            observations=tuple(observations),
        )
        self.carrier.complete_batch(
            carrier_snapshot,
            facts,
            completed_cutoff=EvidenceCutoff(
                max(item.available_at for item in observations)
            ),
            update_id=batch.update_id,
        )


__all__ = [
    "EvidenceRetriever",
    "FailureCardRetriever",
    "MemoryCarrierUpdate",
    "MemoryPlannedAction",
    "MemoryPolicyAdapter",
    "ParentSlateBuilder",
]
