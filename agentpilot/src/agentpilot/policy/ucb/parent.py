"""D1 parent selection using frozen-neighborhood KL-UCB."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agentpilot.domain.common import stable_id
from agentpilot.domain.search import (
    DecisionTrace,
    ParentSelection,
    ParentSlate,
    SearchState,
)
from agentpilot.policy.ucb.neighborhood import (
    NeighborhoodEstimate,
    NeighborhoodKLUCB,
    seeded_argmax,
)
from agentpilot.policy.ucb.observations import ParentOpportunity, UCBTarget


@dataclass(frozen=True)
class ParentScore:
    parent_id: str
    estimate: NeighborhoodEstimate


class ParentUCB:
    def __init__(self, estimator: NeighborhoodKLUCB) -> None:
        self.estimator = estimator

    def score(self, slate: ParentSlate, state: SearchState) -> tuple[ParentScore, ...]:
        if slate.state_id != state.state_id:
            raise ValueError("parent slate and search state identities differ")
        return tuple(
            ParentScore(
                parent_id=candidate.parent_id,
                estimate=self.estimator.estimate(
                    target=UCBTarget.PARENT,
                    compatibility=state.compatibility,
                    descriptor=candidate.descriptor,
                    cutoff=state.cutoff,
                    arm_id=None,
                ),
            )
            for candidate in slate.candidates
        )

    def select(
        self,
        slate: ParentSlate,
        state: SearchState,
        *,
        seed: int,
    ) -> ParentSelection:
        scores = self.score(slate, state)
        values = {item.parent_id: item.estimate.index for item in scores}
        selected_id, ties = seeded_argmax(
            values,
            seed=seed,
            tie_tolerance=self.estimator.config.tie_tolerance,
        )
        candidate = next(
            item for item in slate.candidates if item.parent_id == selected_id
        )
        trace = DecisionTrace(
            selected_id=selected_id,
            eligible_ids=tuple(item.parent_id for item in slate.candidates),
            tie_ids=ties,
            seed=seed,
            conditional_probability=1.0,
            indices=values,
        )
        return ParentSelection(
            selection_id=stable_id(
                "parent-selection", slate.slate_id, slate.decision_round_id, seed
            ),
            slate_id=slate.slate_id,
            decision_round_id=slate.decision_round_id,
            state_id=state.state_id,
            parent_id=candidate.parent_id,
            commit_sha=candidate.commit_sha,
            trace=trace,
        )

    @staticmethod
    def opportunity(
        selection: ParentSelection,
        slate: ParentSlate,
        state: SearchState,
        *,
        available_at: datetime,
    ) -> ParentOpportunity:
        if (
            selection.slate_id != slate.slate_id
            or selection.decision_round_id != slate.decision_round_id
            or selection.state_id != state.state_id
        ):
            raise ValueError("parent selection opportunity coordinates differ")
        selected = next(
            (
                item
                for item in slate.candidates
                if item.parent_id == selection.parent_id
            ),
            None,
        )
        if selected is None:
            raise ValueError("selected parent is absent from its frozen slate")
        return ParentOpportunity(
            opportunity_id=stable_id("parent-opportunity", selection.decision_round_id),
            decision_round_id=selection.decision_round_id,
            selected_parent_id=selection.parent_id,
            eligible_parent_descriptors={
                item.parent_id: item.descriptor for item in slate.candidates
            },
            compatibility=state.compatibility,
            available_at=available_at,
        )
