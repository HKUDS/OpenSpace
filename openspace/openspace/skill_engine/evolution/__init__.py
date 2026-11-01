"""Evolution engine orchestration."""

from .admission import AdmissionResult, EvolutionAdmission
from .audit import EvidenceRefAccessError, EvolutionActionRecord, EvolutionAuditService
from .authoring import AuthoringResult, SkillEvolverAuthoringBackend, StagedSkillEdit
from .authoring_contract import (
    AuthoringIntentSpec,
    SkillAssertion,
    SkillAuthoringContract,
    SkillEvalPlan,
    SkillReplayTask,
)
from .backfill import (
    BackfillResult,
    EvidenceBackfill,
    backfill_recording,
    backfill_session,
    backfill_skill_store,
)
from .candidates import EvolutionCandidate, EvolutionCandidateStore
from .capture_semantic import CaptureContractSemanticReviewer
from .behavior_eval import (
    ContractEvalResult,
    ReplayEvalResult,
    RoutingEvalResult,
    SkillBehaviorEvalResult,
    SkillBehaviorEvaluator,
    SkillEvalAdapter,
    SubprocessSkillReplayRunner,
)
from .engine import (
    EvolutionCommitter,
    EvolutionEngine,
    EvolutionMutationOutcome,
    EvolutionRunResult,
)
from .job_completion import (
    EvolutionJobCompletion,
    completion_after_recovery,
    completion_from_outcome,
    outcome_has_committing_action,
    outcome_result_ref,
)
from .recovery import EvolutionRecovery, EvolutionRecoveryResult
from .validator import EvolutionValidator, ValidationResult

__all__ = [
    "AdmissionResult",
    "AuthoringResult",
    "BackfillResult",
    "AuthoringIntentSpec",
    "EvidenceBackfill",
    "backfill_recording",
    "backfill_session",
    "backfill_skill_store",
    "EvolutionActionRecord",
    "EvolutionAuditService",
    "EvidenceRefAccessError",
    "ContractEvalResult",
    "ReplayEvalResult",
    "RoutingEvalResult",
    "SkillAssertion",
    "SkillAuthoringContract",
    "SkillBehaviorEvalResult",
    "SkillBehaviorEvaluator",
    "SkillEvalAdapter",
    "SubprocessSkillReplayRunner",
    "SkillEvalPlan",
    "SkillReplayTask",
    "EvolutionCandidate",
    "EvolutionCandidateStore",
    "CaptureContractSemanticReviewer",
    "EvolutionCommitter",
    "EvolutionAdmission",
    "EvolutionEngine",
    "EvolutionMutationOutcome",
    "EvolutionJobCompletion",
    "EvolutionRecovery",
    "EvolutionRecoveryResult",
    "EvolutionRunResult",
    "EvolutionValidator",
    "SkillEvolverAuthoringBackend",
    "StagedSkillEdit",
    "ValidationResult",
    "completion_after_recovery",
    "completion_from_outcome",
    "outcome_has_committing_action",
    "outcome_result_ref",
]
