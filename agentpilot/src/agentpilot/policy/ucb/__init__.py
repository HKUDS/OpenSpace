"""UCB-based Policy Learning with neighborhood KL-UCB carrier primitives.

All behavior-shaping parameters are explicit construction inputs; this
package intentionally defines no formal-experiment defaults.
"""

from agentpilot.policy.ucb.arm_allocator import ArmAllocator
from agentpilot.policy.ucb.arms import (
    ArmRegistrySnapshot,
    InnovationArm,
    InnovationArmRegistry,
)
from agentpilot.policy.ucb.carrier import (
    DirectionApplicabilityBuilder,
    FailureCardRetriever,
    SlotGenerationAttempt,
    UCBPlanResult,
    UCBFrozenSnapshot,
    UCBPlannerConfig,
    UCBPolicyCarrier,
    derive_ucb_seed,
)
from agentpilot.policy.ucb.cooldown import (
    ArmCooldownConfig,
    ArmCooldownWindow,
    ArmEligibilityController,
    ArmEligibilityReport,
    ArmEligibilityRound,
)
from agentpilot.policy.ucb.direction import (
    DirectionUCB,
    NoValidDirection,
    classify_slot_adherence,
    generation_failed,
)
from agentpilot.policy.ucb.neighborhood import (
    NearStatePredicate,
    NeighborhoodConfig,
    NeighborhoodEstimate,
    NeighborhoodKLUCB,
    UCBHistoryView,
    bernoulli_kl,
    exploration_bound,
    kl_ucb_index,
    seeded_argmax,
    seeded_top_k,
)
from agentpilot.policy.ucb.observations import (
    ArmCreationOpportunity,
    BernoulliObservation,
    DirectionOpportunity,
    ParentOpportunity,
    SelectedActionRecord,
    UCBHistory,
    UCBTarget,
    observations_from_policy,
)
from agentpilot.policy.ucb.parent import ParentUCB
from agentpilot.policy.ucb.planner import (
    UCBActionPlannerAdapter,
    UCBAlgorithmIdentity,
    UCBPlannedAction,
    UCBRoundInputProvider,
    UCBRoundInputs,
)
from agentpilot.policy.ucb.updater import UCBBarrierBatch, UCBPolicyUpdater

__all__ = [
    "ArmAllocator",
    "ArmCreationOpportunity",
    "ArmCooldownConfig",
    "ArmCooldownWindow",
    "ArmEligibilityController",
    "ArmEligibilityReport",
    "ArmEligibilityRound",
    "ArmRegistrySnapshot",
    "BernoulliObservation",
    "DirectionOpportunity",
    "DirectionApplicabilityBuilder",
    "FailureCardRetriever",
    "DirectionUCB",
    "InnovationArm",
    "InnovationArmRegistry",
    "NearStatePredicate",
    "NeighborhoodConfig",
    "NeighborhoodEstimate",
    "NeighborhoodKLUCB",
    "NoValidDirection",
    "ParentOpportunity",
    "ParentUCB",
    "SelectedActionRecord",
    "SlotGenerationAttempt",
    "UCBHistory",
    "UCBHistoryView",
    "UCBBarrierBatch",
    "UCBActionPlannerAdapter",
    "UCBAlgorithmIdentity",
    "UCBPlanResult",
    "UCBFrozenSnapshot",
    "UCBPlannerConfig",
    "UCBPolicyCarrier",
    "derive_ucb_seed",
    "UCBPolicyUpdater",
    "UCBPlannedAction",
    "UCBRoundInputProvider",
    "UCBRoundInputs",
    "UCBTarget",
    "bernoulli_kl",
    "classify_slot_adherence",
    "exploration_bound",
    "generation_failed",
    "kl_ucb_index",
    "observations_from_policy",
    "seeded_argmax",
    "seeded_top_k",
]
