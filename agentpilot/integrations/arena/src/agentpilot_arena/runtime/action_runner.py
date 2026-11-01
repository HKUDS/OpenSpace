"""Lease-fenced action transaction over injected harness/evaluator ports."""

# Persistent format and evaluation identifiers retain their historical values.

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from agentpilot.app import ExecutionResult
from agentpilot.domain import (
    ActionExecutionAttestation,
    ActionExecutionFence,
    ActionExecutionMode,
    EvaluationValidity,
    EvidenceTier,
    ImplementationAdherence,
    OutcomeAttribution,
    PairedOutcome,
    PolicyAction,
    RealizedFileChange,
    WorkerExecutionStatus,
    WorkerResult,
    stable_id,
    utc_now,
)
from agentpilot.providers import ProviderAccountingError
from agentpilot.realization import DiagnosticResult

from .action_store import (
    ActionClaim,
    ActionClaimMode,
    ActionRecordStatus,
    ActionRecoveryRequired,
    CommittedImplementation,
    DurableActionRecord,
    SQLiteActionStore,
)
from .errors import (
    CandidateBoundaryDiagnosticsFailed,
    CandidateBoundaryViolation,
    CandidateCommandDiagnosticsFailed,
    CandidateNoChangesError,
    VisibleACandidateRejected,
)
from .git_workspace import GitWorkspace
from .structural_adherence import StructuralImplementationAdherenceResult

_TERMINAL_REALIZATION_FAILURES = (
    CandidateBoundaryDiagnosticsFailed,
    CandidateBoundaryViolation,
    CandidateCommandDiagnosticsFailed,
    CandidateNoChangesError,
    VisibleACandidateRejected,
)

AGENTPILOT_ACTION_EXECUTION_POLICY_CONTRACT = "openthink-action-execution-policy"


class VisibleARejectionPolicy(str, Enum):
    """AgentPilot-owned disposition after conclusive Visible-A rejection."""

    BLOCK_AND_REPROPOSE = "block_and_repropose"
    CONTINUE_TO_B = "continue_to_b"


class WorkspaceManager(Protocol):
    def create(self, *, parent_commit_sha: str, action_id: str) -> GitWorkspace: ...

    def stage(self, workspace: GitWorkspace) -> tuple[str, ...]: ...

    def commit(self, workspace: GitWorkspace, *, message: str) -> str: ...

    def prepare_commit(
        self, workspace: GitWorkspace, *, message: str,
        prepared_commit_sha: str | None = None,
    ) -> str: ...

    def publish_commit(
        self, workspace: GitWorkspace, *, result_commit_sha: str
    ) -> None: ...

    def snapshot(self, workspace: GitWorkspace, *, message: str) -> str: ...

    def remove(self, workspace: GitWorkspace) -> None: ...


@dataclass(frozen=True, slots=True)
class HarnessResult:
    """Bounded harness transport result; adherence is verified after commit."""

    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    cost_usd: float | None = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.diagnostics, Mapping):
            raise TypeError("diagnostics must be a mapping")
        if self.cost_usd is None:
            accounting = self.diagnostics.get("accounting")
            if not isinstance(accounting, Mapping) or accounting.get("mode") != "external":
                raise ValueError("unreported harness cost requires explicit external accounting")
        elif isinstance(self.cost_usd, bool) or not math.isfinite(self.cost_usd) or self.cost_usd < 0:
            raise ValueError("harness cost must be finite and non-negative")


class _TerminalRealizationRejected(RuntimeError):
    """Carry bounded harness accounting out of a failed diagnostic loop."""

    def __init__(
        self,
        cause: RuntimeError,
        harness_result: HarnessResult,
        diagnostics_report: Mapping[str, Any],
    ) -> None:
        super().__init__(type(cause).__qualname__)
        self.cause = cause
        self.harness_result = harness_result
        self.diagnostics_report = dict(diagnostics_report)


class ActionHarness(Protocol):
    def run(
        self,
        action: PolicyAction,
        workspace: GitWorkspace,
        *,
        diagnostic: DiagnosticResult | None = None,
        rework_budget: int = 0,
    ) -> HarnessResult: ...


class WorktreeDiagnostics(Protocol):
    def run(self, *, worktree: Path) -> DiagnosticResult: ...


class CandidateValidator(Protocol):
    def validate(
        self,
        action: PolicyAction,
        workspace: GitWorkspace,
        staged_files: tuple[str, ...],
    ) -> None: ...

    def realized_file_changes(
        self,
        action: PolicyAction,
        workspace: GitWorkspace,
        *,
        parent_commit_sha: str,
        result_commit_sha: str,
    ) -> tuple[RealizedFileChange, ...]: ...


class CandidateAdherenceVerifier(Protocol):
    def verify(
        self,
        action: PolicyAction,
        *,
        parent_commit_sha: str,
        result_commit_sha: str,
    ) -> StructuralImplementationAdherenceResult: ...


class PairedEvaluator(Protocol):
    def evaluate(
        self,
        action: PolicyAction,
        result_commit_sha: str,
        workspace: GitWorkspace,
    ) -> PairedOutcome: ...


LiveFenceCheck = Callable[[ActionExecutionFence], object]
Clock = Callable[[], datetime]


class DurableActionRunner:
    """Execute each stable action at most once without guessing recovery."""

    @staticmethod
    def _blocked_failure_code(stage: str, exc: BaseException) -> str:
        """Persist a safe failure class without storing exception prose.

        Exception messages may contain provider output or local paths, but the
        Python type is bounded trusted code metadata and is enough to tell a
        candidate-validation failure from a harness, lease, Git, or evaluator
        failure during later recovery.
        """

        if (
            not isinstance(stage, str)
            or not stage
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz_" for character in stage
            )
        ):
            raise ValueError("failure stage must be lowercase snake-case text")
        return f"action_execution_failed:{stage}:{type(exc).__qualname__}"

    def _realization_rejection(
        self,
        *,
        action: PolicyAction,
        fence: ActionExecutionFence,
        workspace: GitWorkspace,
        started_at: datetime,
        mutation_checked_at: datetime,
        failure_stage: str,
        exception: BaseException,
        harness_result: HarnessResult,
        diagnostics_report: Mapping[str, Any],
    ) -> ExecutionResult:
        """Close one deterministic pre-commit miss as non-reward feedback."""

        self.live_fence_check(fence)
        DurableActionRunner._close_before_b(self, action)
        completed_at = max(self.clock(), mutation_checked_at, started_at)
        failure_type = type(exception).__qualname__
        worker_diagnostics = dict(harness_result.diagnostics)
        worker_diagnostics.update(diagnostics_report)
        worker_diagnostics.update(
            {
                "disposition": "implementation_rejected",
                "failure_stage": failure_stage,
                "failure_type": failure_type,
            }
        )
        visible = diagnostics_report.get("visible_diagnostics")
        candidate_program_id = (
            visible.get("candidate_program_id")
            if isinstance(visible, Mapping)
            else None
        )
        worker = WorkerResult(
            worker_result_id=stable_id(
                "worker-result", action.action_id, "realization-rejected"
            ),
            action_id=action.action_id,
            status=WorkerExecutionStatus.FAILED,
            adherence=ImplementationAdherence.DEVIATED,
            started_at=started_at,
            completed_at=completed_at,
            result_commit_sha=None,
            touched_files=(),
            diagnostics=worker_diagnostics,
            cost_usd=harness_result.cost_usd,
            candidate_program_id=candidate_program_id,
            execution_attestation=ActionExecutionAttestation(
                fence_id=fence.fence_id,
                runner_id=fence.runner_id,
                worktree_id=workspace.worktree_id,
                parent_commit_sha=fence.parent_commit_sha,
                result_commit_sha=None,
                mutation_fence_checked_at=mutation_checked_at,
                commit_fence_checked_at=completed_at,
            ),
        )
        outcome = PairedOutcome(
            outcome_id=stable_id("realization-outcome", action.action_id, failure_type),
            action_id=action.action_id,
            evidence_tier=EvidenceTier.REALIZATION,
            evaluation_plan_record_id=None,
            validity=EvaluationValidity.MISSING,
            attribution=OutcomeAttribution.NON_ATTRIBUTABLE,
            signed_effectiveness_statistic=None,
            constraints_pass=None,
            completed_at=completed_at,
            admission_fate="implementation_rejected",
            estimator_identity={
                "status": "not_evaluated",
                "reason": failure_type,
            },
        )
        return ExecutionResult(worker, outcome)

    def __init__(
        self,
        *,
        runner_id: str,
        scope_identity: str,
        store: SQLiteActionStore,
        workspaces: WorkspaceManager,
        harness: ActionHarness,
        validator: CandidateValidator,
        adherence_verifier: CandidateAdherenceVerifier,
        evaluator: PairedEvaluator,
        live_fence_check: LiveFenceCheck,
        clock: Clock = utc_now,
        diagnostics: WorktreeDiagnostics | None = None,
        max_reworks: int = 0,
        visible_a_rejection_policy: VisibleARejectionPolicy = (
            VisibleARejectionPolicy.BLOCK_AND_REPROPOSE
        ),
    ) -> None:
        for name, value in (
            ("runner_id", runner_id),
            ("scope_identity", scope_identity),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if not isinstance(store, SQLiteActionStore):
            raise TypeError("store must be SQLiteActionStore")
        for name, value, method in (
            ("workspaces", workspaces, "create"),
            ("harness", harness, "run"),
            ("validator", validator, "validate"),
            ("adherence_verifier", adherence_verifier, "verify"),
            ("evaluator", evaluator, "evaluate"),
        ):
            if not callable(getattr(value, method, None)):
                raise TypeError(f"{name} must provide {method}()")
        if not callable(live_fence_check):
            raise TypeError("live_fence_check must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if diagnostics is not None and not any(
            callable(getattr(diagnostics, method, None))
            for method in ("run", "run_candidate")
        ):
            raise TypeError("diagnostics must provide run() or run_candidate()")
        if (
            isinstance(max_reworks, bool)
            or not isinstance(max_reworks, int)
            or max_reworks < 0
        ):
            raise ValueError("max_reworks must be a non-negative integer")
        if diagnostics is None and max_reworks:
            raise ValueError("max_reworks needs a diagnostics runner")
        if not isinstance(visible_a_rejection_policy, VisibleARejectionPolicy):
            raise TypeError(
                "visible_a_rejection_policy must be VisibleARejectionPolicy"
            )
        self.runner_id = runner_id.strip()
        self.scope_identity = scope_identity.strip()
        self.store = store
        self.workspaces = workspaces
        self.harness = harness
        self.validator = validator
        self.adherence_verifier = adherence_verifier
        self.evaluator = evaluator
        self.live_fence_check = live_fence_check
        self.clock = clock
        self.diagnostics = diagnostics
        self.max_reworks = max_reworks
        self.visible_a_rejection_policy = visible_a_rejection_policy

    def _record_visible_a_gate(self, action: PolicyAction, *, accepted: bool) -> None:
        if (
            accepted
            or self.visible_a_rejection_policy is VisibleARejectionPolicy.CONTINUE_TO_B
        ):
            return
        DurableActionRunner._close_before_b(self, action)

    def _close_before_b(self, action: PolicyAction) -> None:
        closer = getattr(getattr(self, "evaluator", None), "close_before_b", None)
        if callable(closer):
            closer(action)

    def _implement_with_bounded_rework(
        self,
        action: PolicyAction,
        workspace: GitWorkspace,
        *,
        set_failure_stage: Callable[[str], None],
    ) -> tuple[HarnessResult, dict[str, Any]]:
        """Edit, check, and repair at most `max_reworks` times.

        By default a candidate proceeds only after the final trusted diagnostic
        passes. A benchmark may explicitly treat a conclusive Visible-A score
        rejection as development feedback and continue to adaptive B after the
        bounded rework. Syntax, candidate-boundary, apparatus, provenance and
        every other failure remain pre-commit terminal failures.
        """

        set_failure_stage("harness_initial")
        harness_result = self.harness.run(action, workspace)
        if not isinstance(harness_result, HarnessResult):
            raise TypeError("action harness returned the wrong result type")
        if harness_result.cost_usd is None:
            raise ValueError("DurableActionRunner requires settled cost; use the research worker workflow for external billing")
        if self.diagnostics is None:
            return harness_result, {}

        cost_usd = harness_result.cost_usd
        attempts: list[dict[str, Any]] = []
        reworks = 0
        visible_a_soft_rejected = False
        diagnostic_failure: type[RuntimeError] = CandidateCommandDiagnosticsFailed
        while True:
            snapshot_program_id: str | None = None
            candidate_diagnostics = getattr(self.diagnostics, "run_candidate", None)
            set_failure_stage("candidate_stage")
            staged_files = self.workspaces.stage(workspace)
            if not staged_files:
                # A coding harness can exit successfully without changing the
                # checkout.  That is a repairable implementation miss, not an
                # ambiguous transaction failure: no candidate exists yet and
                # the configured rework budget can ask the same worker to make
                # the declared edit.  If the budget is exhausted, the normal
                # final validation below still fails closed.
                diagnostic = DiagnosticResult(
                    passed=False,
                    command=("rsi-arena-candidate-boundary",),
                    exit_code=1,
                    stderr=(
                        "candidate validation: no staged changes were produced; "
                        "modify the declared target before finishing"
                    ),
                )
                diagnostic_failure = CandidateNoChangesError
            elif callable(candidate_diagnostics):
                set_failure_stage("candidate_validation")
                try:
                    self.validator.validate(action, workspace, staged_files)
                except CandidateBoundaryViolation as exc:
                    # Scope/target mismatches are implementation misses, not
                    # ambiguous transaction failures. Feed the validator-owned
                    # bounded reason back to the same worker once, while still
                    # requiring the final staged tree to pass the exact boundary.
                    detail = " ".join(str(exc).split())[:1_024]
                    diagnostic = DiagnosticResult(
                        passed=False,
                        command=("rsi-arena-candidate-boundary",),
                        exit_code=1,
                        stderr=(
                            "candidate validation: "
                            + (detail or type(exc).__qualname__)
                            + "; revise the existing worktree so every change remains "
                            "inside the resolved scope, every declared new path "
                            "exists, and a pre-existing scope file changes"
                        ),
                    )
                    diagnostic_failure = CandidateBoundaryDiagnosticsFailed
                else:
                    set_failure_stage("candidate_snapshot")
                    snapshot_program_id = self.workspaces.snapshot(
                        workspace,
                        message=(
                            f"AgentPilot visible-A snapshot {action.action_id} "
                            f"rework {reworks}"
                        ),
                    )
                    set_failure_stage("visible_validation")
                    diagnostic = candidate_diagnostics(
                        action=action,
                        workspace=workspace,
                        program_id=snapshot_program_id,
                    )
            else:
                set_failure_stage("candidate_diagnostics")
                diagnostic = self.diagnostics.run(worktree=workspace.path)
                diagnostic_failure = CandidateCommandDiagnosticsFailed
            if not isinstance(diagnostic, DiagnosticResult):
                raise TypeError("diagnostics returned the wrong result type")
            if (
                callable(candidate_diagnostics)
                and staged_files
                and diagnostic.command != ("rsi-arena-candidate-boundary",)
            ):
                if diagnostic.command == ("rsi-arena-openthink-visible-a",):
                    diagnostic_failure = VisibleACandidateRejected
                else:
                    # Visible-A runs its local command/syntax boundary first
                    # and returns that result unchanged when it fails.
                    set_failure_stage("candidate_diagnostics")
                    diagnostic_failure = CandidateCommandDiagnosticsFailed
            attempts.append(
                {
                    "rework_ordinal": reworks,
                    "passed": diagnostic.passed,
                    "exit_code": diagnostic.exit_code,
                    "timed_out": diagnostic.timed_out,
                    "output_limit_exceeded": diagnostic.output_limit_exceeded,
                }
            )
            if (
                not diagnostic.passed
                and diagnostic_failure is VisibleACandidateRejected
                and self.visible_a_rejection_policy
                is VisibleARejectionPolicy.BLOCK_AND_REPROPOSE
            ):
                DurableActionRunner._record_visible_a_gate(self, action, accepted=False)
                cause = VisibleACandidateRejected(
                    "Visible-A semantic feedback requires a new action identity"
                )
                raise _TerminalRealizationRejected(
                    cause,
                    replace(harness_result, cost_usd=cost_usd),
                    {
                        "visible_diagnostics": {
                            "command": list(diagnostic.command),
                            "passed": False,
                            "timed_out": diagnostic.timed_out,
                            "output_limit_exceeded": (diagnostic.output_limit_exceeded),
                            "reworks_used": reworks,
                            "max_reworks": self.max_reworks,
                            "soft_rejected": False,
                            "disposition": "semantic_reproposal_required",
                            "attempts": attempts,
                            # The measured anonymous snapshot: the durable key
                            # to the trace manifest this census just published,
                            # which negative direction history attaches later.
                            "candidate_program_id": snapshot_program_id,
                        }
                    },
                )
            if diagnostic.passed:
                break
            if reworks >= self.max_reworks:
                if (
                    diagnostic_failure is VisibleACandidateRejected
                    and self.visible_a_rejection_policy
                    is VisibleARejectionPolicy.CONTINUE_TO_B
                ):
                    visible_a_soft_rejected = True
                    break
                cause = diagnostic_failure(
                    "candidate diagnostics still failed after bounded rework"
                )
                raise _TerminalRealizationRejected(
                    cause,
                    replace(harness_result, cost_usd=cost_usd),
                    {
                        "visible_diagnostics": {
                            "command": list(diagnostic.command),
                            "passed": False,
                            "timed_out": diagnostic.timed_out,
                            "output_limit_exceeded": (diagnostic.output_limit_exceeded),
                            "reworks_used": reworks,
                            "max_reworks": self.max_reworks,
                            "soft_rejected": False,
                            "disposition": "implementation_rejected",
                            "attempts": attempts,
                        }
                    },
                )
            reworks += 1
            set_failure_stage("harness_rework")
            harness_result = self.harness.run(
                action,
                workspace,
                diagnostic=diagnostic,
                rework_budget=self.max_reworks - reworks + 1,
            )
            if not isinstance(harness_result, HarnessResult):
                raise TypeError("action harness returned the wrong result type")
            cost_usd += harness_result.cost_usd

        if diagnostic.command == ("rsi-arena-openthink-visible-a",):
            DurableActionRunner._record_visible_a_gate(
                self, action, accepted=diagnostic.passed
            )

        summary = {
            "visible_diagnostics": {
                "command": list(diagnostic.command),
                "passed": diagnostic.passed,
                "timed_out": diagnostic.timed_out,
                "output_limit_exceeded": diagnostic.output_limit_exceeded,
                "reworks_used": reworks,
                "max_reworks": self.max_reworks,
                "soft_rejected": visible_a_soft_rejected,
                "disposition": (
                    "continued_to_adaptive_b" if visible_a_soft_rejected else "passed"
                ),
                "attempts": attempts,
            }
        }
        if snapshot_program_id is not None:
            # Keep the exact identity whose Visible-A manifest was published.
            # A new commit of the same tree would orphan that evidence when the
            # accepted child becomes a parent in the next search round.
            summary["visible_diagnostics"]["candidate_program_id"] = snapshot_program_id
        return replace(harness_result, cost_usd=cost_usd), summary

    def _validate_fence(
        self, action: PolicyAction, fence: ActionExecutionFence
    ) -> None:
        if not isinstance(action, PolicyAction):
            raise TypeError("action must be an AgentPilot PolicyAction")
        if not isinstance(fence, ActionExecutionFence):
            raise TypeError("fence must be an AgentPilot ActionExecutionFence")
        if (
            fence.action_id,
            fence.run_id,
            fence.batch_id,
            fence.parent_commit_sha,
            fence.runner_id,
            fence.scope_identity,
        ) != (
            action.action_id,
            action.run_id,
            action.batch_id,
            action.parent_commit_sha,
            self.runner_id,
            self.scope_identity,
        ):
            raise ValueError("action coordinates differ from execution fence")

    def execute(
        self, action: PolicyAction, fence: ActionExecutionFence
    ) -> ExecutionResult:
        self._validate_fence(action, fence)
        blocked = self.store.get(action.action_id)
        if (
            blocked is not None
            and blocked.status is ActionRecordStatus.BLOCKED
            and blocked.result_commit_sha is not None
            and blocked.failure_code is not None
            and self._postcommit_evaluation_failure(blocked.failure_code)
        ):
            return self._resume_postcommit_evaluation(action, fence, blocked)
        claim = self.store.claim(action_id=action.action_id, fence=fence)
        if claim.mode is ActionClaimMode.REPLAY:
            return self._replay(claim, fence, record=blocked, action=action)
        return self._execute_claim(action, fence, claim)

    @staticmethod
    def _postcommit_evaluation_failure(failure_code: str) -> bool:
        parts = failure_code.split(":", 2)
        return (
            len(parts) == 3
            and parts[0] == "action_execution_failed"
            and parts[1]
            in {
                "commit_publish",
                "candidate_revalidation",
                "accounting_finalize",
                "adherence",
                "realization_inspection",
                "evaluation",
                "result_finalize",
                "store_complete",
                "interrupted_postcommit",
            }
        )

    def _resume_postcommit_evaluation(
        self,
        action: PolicyAction,
        fence: ActionExecutionFence,
        record: DurableActionRecord,
    ) -> ExecutionResult:
        """Resume only the idempotent Arena half of an already committed action."""

        assert record.result_commit_sha is not None
        assert record.failure_code is not None
        implementation = record.committed_implementation
        if implementation is None:
            raise ActionRecoveryRequired(
                "post-commit recovery lacks committed implementation facts"
            )
        prior_context = record.claim_context
        if (
            prior_context.action_id,
            prior_context.run_id,
            prior_context.batch_id,
            prior_context.parent_commit_sha,
            prior_context.runner_id,
            prior_context.scope_identity,
        ) != (
            action.action_id,
            action.run_id,
            action.batch_id,
            action.parent_commit_sha,
            self.runner_id,
            self.scope_identity,
        ):
            raise ActionRecoveryRequired("post-commit recovery ownership changed")
        if record.worktree_id is None or record.worktree_path is None:
            raise ActionRecoveryRequired("post-commit recovery has no owned worktree")
        workspace = GitWorkspace(
            record.worktree_id,
            Path(record.worktree_path),
            action.parent_commit_sha,
        )
        if not workspace.path.is_dir():
            raise ActionRecoveryRequired("post-commit recovery worktree is unavailable")
        self.live_fence_check(fence)
        claim = self.store.reclaim_blocked_postcommit(
            action_id=action.action_id,
            expected_failure_code=record.failure_code,
            expected_result_commit_sha=record.result_commit_sha,
            fence=fence,
        )
        stage = "candidate_revalidation"
        try:
            self.live_fence_check(fence)
            staged_files = self.workspaces.stage(workspace)
            if staged_files != implementation.touched_files:
                raise ActionRecoveryRequired(
                    "post-commit recovery candidate paths changed"
                )
            self.validator.validate(action, workspace, staged_files)
            stage = "accounting_finalize"
            cleanup_recorded_action = getattr(
                self.harness, "cleanup_recorded_action", None
            )
            if callable(cleanup_recorded_action):
                cleanup_recorded_action(action)
            stage = "commit_publish"
            self.workspaces.publish_commit(
                workspace, result_commit_sha=record.result_commit_sha
            )
            stage = "adherence"
            adherence = self.adherence_verifier.verify(
                action,
                parent_commit_sha=action.parent_commit_sha,
                result_commit_sha=record.result_commit_sha,
            )
            if not isinstance(adherence, StructuralImplementationAdherenceResult):
                raise TypeError(
                    "candidate adherence verifier returned the wrong result type"
                )
            if (
                adherence.action_id,
                adherence.parent_commit_sha,
                adherence.result_commit_sha,
            ) != (
                action.action_id,
                action.parent_commit_sha,
                record.result_commit_sha,
            ):
                raise ValueError(
                    "candidate adherence result differs from action commits"
                )
            stage = "realization_inspection"
            file_changes = tuple(
                self.validator.realized_file_changes(
                    action,
                    workspace,
                    parent_commit_sha=action.parent_commit_sha,
                    result_commit_sha=record.result_commit_sha,
                )
            )
            worker_diagnostics = dict(implementation.diagnostics)
            implementation_completed_at = max(
                self.clock(),
                implementation.commit_fence_checked_at,
                implementation.started_at,
            )
            worker_diagnostics.update(
                {
                    "recovered_post_commit": True,
                    "previous_failure_code": record.failure_code,
                    "structural_implementation_adherence": (
                        adherence.as_worker_diagnostics()
                    ),
                }
            )
            adherence_failure_type: str | None = None
            adherence_fate: str | None = None
            if adherence.adherence is ImplementationAdherence.DEVIATED:
                adherence_failure_type = "StructuralImplementationDeviation"
                adherence_fate = "adherence_rejected"
            elif adherence.adherence is ImplementationAdherence.UNKNOWN:
                adherence_failure_type = "StructuralImplementationUnknown"
                adherence_fate = "adherence_unknown"
            if adherence_fate is not None:
                worker_diagnostics.update(
                    {
                        "disposition": adherence_fate,
                        "failure_stage": "adherence",
                        "failure_type": adherence_failure_type,
                    }
                )
                self._close_before_b(action)
                outcome = PairedOutcome(
                    outcome_id=stable_id(
                        "realization-outcome",
                        action.action_id,
                        adherence_failure_type,
                    ),
                    action_id=action.action_id,
                    evidence_tier=EvidenceTier.REALIZATION,
                    evaluation_plan_record_id=None,
                    validity=EvaluationValidity.MISSING,
                    attribution=OutcomeAttribution.NON_ATTRIBUTABLE,
                    signed_effectiveness_statistic=None,
                    constraints_pass=None,
                    completed_at=implementation_completed_at,
                    admission_fate=adherence_fate,
                    estimator_identity={
                        "status": "not_evaluated",
                        "reason": adherence_failure_type,
                    },
                )
            else:
                stage = "evaluation"
                outcome = self.evaluator.evaluate(
                    action,
                    record.result_commit_sha,
                    workspace,
                )
                if not isinstance(outcome, PairedOutcome):
                    raise TypeError("paired evaluator returned the wrong result type")
                if outcome.action_id != action.action_id:
                    raise ValueError("paired outcome differs from action_id")
                if outcome.evidence_tier is not EvidenceTier.ADAPTIVE_B:
                    raise ValueError("normal action execution accepts only adaptive B")
            checked_at = self.clock()
            recovered_under_new_fence = (
                implementation.execution_fence_id != fence.fence_id
            )
            attestation = ActionExecutionAttestation(
                fence_id=fence.fence_id,
                runner_id=fence.runner_id,
                worktree_id=workspace.worktree_id,
                parent_commit_sha=fence.parent_commit_sha,
                result_commit_sha=record.result_commit_sha,
                mutation_fence_checked_at=implementation.mutation_fence_checked_at,
                commit_fence_checked_at=implementation.commit_fence_checked_at,
                mode=(
                    ActionExecutionMode.REPLAYED
                    if recovered_under_new_fence
                    else ActionExecutionMode.EXECUTED
                ),
                original_fence_id=(
                    implementation.execution_fence_id
                    if recovered_under_new_fence
                    else None
                ),
                replay_verified_at=(checked_at if recovered_under_new_fence else None),
            )
            worker = WorkerResult(
                worker_result_id=stable_id(
                    "worker-result", action.action_id, record.result_commit_sha
                ),
                action_id=action.action_id,
                status=WorkerExecutionStatus.COMPLETED,
                adherence=adherence.adherence,
                started_at=implementation.started_at,
                completed_at=implementation_completed_at,
                result_commit_sha=record.result_commit_sha,
                touched_files=implementation.touched_files,
                file_changes=file_changes,
                diagnostics=worker_diagnostics,
                cost_usd=implementation.cost_usd,
                execution_attestation=attestation,
            )
            result = ExecutionResult(worker, outcome)
            stage = "store_complete"
            self.store.complete(claim, result)
        except BaseException as exc:
            self.store.block(
                claim,
                failure_code=self._blocked_failure_code(stage, exc),
                worktree_id=workspace.worktree_id,
                worktree_path=workspace.path,
            )
            raise
        self.workspaces.remove(workspace)
        return result

    def _replay(
        self,
        claim: ActionClaim,
        fence: ActionExecutionFence,
        *,
        record: DurableActionRecord | None,
        action: PolicyAction,
    ) -> ExecutionResult:
        result = claim.result
        if result is None:
            raise RuntimeError("replay claim has no durable result")
        attestation = result.worker.execution_attestation
        if attestation is None:
            raise RuntimeError("durable result has no execution attestation")
        self.live_fence_check(fence)
        cleanup_recorded_action = getattr(self.harness, "cleanup_recorded_action", None)
        if callable(cleanup_recorded_action):
            cleanup_recorded_action(action)
        if (
            record is not None
            and record.worktree_id is not None
            and record.worktree_path is not None
            and Path(record.worktree_path).exists()
        ):
            self.workspaces.remove(
                GitWorkspace(
                    record.worktree_id,
                    Path(record.worktree_path),
                    action.parent_commit_sha,
                )
            )
        if attestation.fence_id == fence.fence_id:
            return result
        verified_at = self.clock()
        replayed = replace(
            attestation,
            fence_id=fence.fence_id,
            runner_id=fence.runner_id,
            mode=ActionExecutionMode.REPLAYED,
            original_fence_id=(attestation.original_fence_id or attestation.fence_id),
            replay_verified_at=verified_at,
        )
        return replace(
            result,
            worker=replace(result.worker, execution_attestation=replayed),
        )

    def recover_blocked_uncommitted(
        self,
        action: PolicyAction,
        fence: ActionExecutionFence,
        *,
        expected_failure_code: str,
    ) -> None:
        """Clean and release one blocked pre-commit action under a new fence.

        The action ID, failure code, absent commit, a current live
        fence, owned worktree coordinates, and harness-private cleanup must all
        agree. This never recovers a post-commit action and never retries by
        itself; the caller may invoke ``execute`` only after it succeeds.
        """

        self._validate_fence(action, fence)
        self.live_fence_check(fence)
        record = self.store.get(action.action_id)
        if (
            record is None
            or record.status is not ActionRecordStatus.BLOCKED
            or record.result_commit_sha is not None
            or record.failure_code != expected_failure_code
        ):
            raise ActionRecoveryRequired(
                "blocked action is absent or differs from recovery preconditions"
            )
        if (record.worktree_id is None) is not (record.worktree_path is None):
            raise ActionRecoveryRequired("blocked action workspace coordinates drifted")
        validate_cleanup = getattr(
            self.harness, "validate_blocked_action_cleanup", None
        )
        if callable(validate_cleanup):
            validate_cleanup(action)
        if record.worktree_id is not None:
            assert record.worktree_path is not None
            path = Path(record.worktree_path)
            if path.exists() or path.is_symlink():
                self.workspaces.remove(
                    GitWorkspace(
                        record.worktree_id,
                        path,
                        action.parent_commit_sha,
                    )
                )
        cleanup = getattr(self.harness, "cleanup_blocked_action", None)
        if callable(cleanup):
            cleanup(action)
        self.store.release_blocked_uncommitted(
            action_id=action.action_id,
            expected_failure_code=expected_failure_code,
        )

    def _execute_claim(
        self,
        action: PolicyAction,
        fence: ActionExecutionFence,
        claim: ActionClaim,
    ) -> ExecutionResult:
        workspace: GitWorkspace | None = None
        try:
            self.live_fence_check(fence)
        except BaseException:
            self.store.release_unmutated(claim)
            raise
        failure_stage = "workspace_create"

        def set_failure_stage(value: str) -> None:
            nonlocal failure_stage
            failure_stage = value

        try:
            mutation_checked_at = self.clock()
            started_at = self.clock()
            workspace = self.workspaces.create(
                parent_commit_sha=action.parent_commit_sha,
                action_id=action.action_id,
            )
            set_failure_stage("workspace_attach")
            try:
                self.store.attach_workspace(
                    claim,
                    worktree_id=workspace.worktree_id,
                    worktree_path=workspace.path,
                )
            except BaseException as attach_error:
                # The journal does not yet know this deterministic worktree. It
                # must be removed before the failure is persisted, otherwise an
                # explicit pre-commit recovery would release the row but leave a
                # path that makes the retry collide with WorkspaceAlreadyExists.
                try:
                    self.workspaces.remove(workspace)
                except BaseException as cleanup_error:
                    attach_error.add_note(
                        "workspace cleanup also failed: "
                        f"{type(cleanup_error).__qualname__}"
                    )
                else:
                    workspace = None
                raise
            harness_result, diagnostics_report = self._implement_with_bounded_rework(
                action,
                workspace,
                set_failure_stage=set_failure_stage,
            )
            set_failure_stage("candidate_stage")
            staged_files = self.workspaces.stage(workspace)
            set_failure_stage("candidate_validation")
            self.validator.validate(action, workspace, staged_files)
            set_failure_stage("lease_before_commit")
            self.live_fence_check(fence)
            commit_checked_at = self.clock()
            set_failure_stage("commit_prepare")
            measured_commit = diagnostics_report.get("visible_diagnostics", {}).get(
                "candidate_program_id"
            )
            result_commit_sha = self.workspaces.prepare_commit(
                workspace,
                message=f"AgentPilot action {action.action_id}",
                **({"prepared_commit_sha": measured_commit} if measured_commit else {}),
            )
            set_failure_stage("commit_record")
            committed_diagnostics = dict(harness_result.diagnostics)
            committed_diagnostics.update(diagnostics_report)
            self.store.record_commit(
                claim,
                result_commit_sha=result_commit_sha,
                implementation=CommittedImplementation(
                    execution_fence_id=fence.fence_id,
                    started_at=started_at,
                    mutation_fence_checked_at=mutation_checked_at,
                    commit_fence_checked_at=commit_checked_at,
                    touched_files=staged_files,
                    diagnostics=committed_diagnostics,
                    cost_usd=harness_result.cost_usd,
                ),
            )
            set_failure_stage("accounting_finalize")
            cleanup_recorded_action = getattr(
                self.harness, "cleanup_recorded_action", None
            )
            if callable(cleanup_recorded_action):
                cleanup_recorded_action(action)
            set_failure_stage("commit_publish")
            self.workspaces.publish_commit(
                workspace, result_commit_sha=result_commit_sha
            )
            set_failure_stage("adherence")
            adherence_result = self.adherence_verifier.verify(
                action,
                parent_commit_sha=action.parent_commit_sha,
                result_commit_sha=result_commit_sha,
            )
            if not isinstance(
                adherence_result, StructuralImplementationAdherenceResult
            ):
                raise TypeError(
                    "candidate adherence verifier returned the wrong result type"
                )
            if (
                adherence_result.action_id,
                adherence_result.parent_commit_sha,
                adherence_result.result_commit_sha,
            ) != (
                action.action_id,
                action.parent_commit_sha,
                result_commit_sha,
            ):
                raise ValueError(
                    "candidate adherence result differs from action commits"
                )
            set_failure_stage("realization_inspection")
            file_changes = tuple(
                self.validator.realized_file_changes(
                    action,
                    workspace,
                    parent_commit_sha=action.parent_commit_sha,
                    result_commit_sha=result_commit_sha,
                )
            )
            implementation_completed_at = max(
                self.clock(), commit_checked_at, started_at
            )
            worker_diagnostics = dict(committed_diagnostics)
            worker_diagnostics["structural_implementation_adherence"] = (
                adherence_result.as_worker_diagnostics()
            )
            adherence_failure_type: str | None = None
            adherence_fate: str | None = None
            if adherence_result.adherence is ImplementationAdherence.DEVIATED:
                adherence_failure_type = "StructuralImplementationDeviation"
                adherence_fate = "adherence_rejected"
            elif adherence_result.adherence is ImplementationAdherence.UNKNOWN:
                adherence_failure_type = "StructuralImplementationUnknown"
                adherence_fate = "adherence_unknown"
            if adherence_fate is not None:
                self._close_before_b(action)
                worker_diagnostics.update(
                    {
                        "disposition": adherence_fate,
                        "failure_stage": "adherence",
                        "failure_type": adherence_failure_type,
                    }
                )
            worker = WorkerResult(
                worker_result_id=stable_id(
                    "worker-result", action.action_id, result_commit_sha
                ),
                action_id=action.action_id,
                status=WorkerExecutionStatus.COMPLETED,
                adherence=adherence_result.adherence,
                started_at=started_at,
                completed_at=implementation_completed_at,
                result_commit_sha=result_commit_sha,
                touched_files=staged_files,
                file_changes=file_changes,
                diagnostics=worker_diagnostics,
                cost_usd=harness_result.cost_usd,
                execution_attestation=ActionExecutionAttestation(
                    fence_id=fence.fence_id,
                    runner_id=fence.runner_id,
                    worktree_id=workspace.worktree_id,
                    parent_commit_sha=fence.parent_commit_sha,
                    result_commit_sha=result_commit_sha,
                    mutation_fence_checked_at=mutation_checked_at,
                    commit_fence_checked_at=commit_checked_at,
                ),
            )
            if adherence_fate is not None:
                assert adherence_failure_type is not None
                outcome = PairedOutcome(
                    outcome_id=stable_id(
                        "realization-outcome",
                        action.action_id,
                        adherence_failure_type,
                    ),
                    action_id=action.action_id,
                    evidence_tier=EvidenceTier.REALIZATION,
                    evaluation_plan_record_id=None,
                    validity=EvaluationValidity.MISSING,
                    attribution=OutcomeAttribution.NON_ATTRIBUTABLE,
                    signed_effectiveness_statistic=None,
                    constraints_pass=None,
                    completed_at=implementation_completed_at,
                    admission_fate=adherence_fate,
                    estimator_identity={
                        "status": "not_evaluated",
                        "reason": adherence_failure_type,
                    },
                )
                result = ExecutionResult(worker, outcome)
            else:
                set_failure_stage("evaluation")
                outcome = self.evaluator.evaluate(action, result_commit_sha, workspace)
                if not isinstance(outcome, PairedOutcome):
                    raise TypeError("paired evaluator returned the wrong result type")
                if outcome.action_id != action.action_id:
                    raise ValueError("paired outcome differs from action_id")
                if outcome.evidence_tier is not EvidenceTier.ADAPTIVE_B:
                    raise ValueError("normal action execution accepts only adaptive B")
                set_failure_stage("result_finalize")
                result = ExecutionResult(worker, outcome)
        except _TerminalRealizationRejected as rejected:
            assert workspace is not None
            result = self._realization_rejection(
                action=action,
                fence=fence,
                workspace=workspace,
                started_at=started_at,
                mutation_checked_at=mutation_checked_at,
                failure_stage=failure_stage,
                exception=rejected.cause,
                harness_result=rejected.harness_result,
                diagnostics_report=rejected.diagnostics_report,
            )
        except _TERMINAL_REALIZATION_FAILURES as exc:
            assert workspace is not None
            result = self._realization_rejection(
                action=action,
                fence=fence,
                workspace=workspace,
                started_at=started_at,
                mutation_checked_at=mutation_checked_at,
                failure_stage=failure_stage,
                exception=exc,
                harness_result=harness_result,
                diagnostics_report=diagnostics_report,
            )
        except BaseException as exc:
            if not self._reclaimed_for_retry(exc, claim, workspace):
                raw_failure_diagnostics = getattr(exc, "diagnostics", None)
                failure_diagnostics = (
                    dict(raw_failure_diagnostics)
                    if isinstance(raw_failure_diagnostics, Mapping)
                    else {}
                )
                failure_cost_usd = getattr(exc, "known_cost_usd", None)
                durable_accounting = getattr(
                    self.harness, "durable_action_accounting", None
                )
                if callable(durable_accounting):
                    try:
                        action_accounting = durable_accounting(action)
                    except Exception as accounting_exc:
                        failure_diagnostics = {
                            "disposition": "accounting_quarantine",
                            "reason": "durable_action_accounting_invalid",
                            "accounting_exception_type": type(
                                accounting_exc
                            ).__qualname__,
                            "original_exception_type": type(exc).__qualname__,
                        }
                        failure_cost_usd = None
                    else:
                        if isinstance(action_accounting, Mapping):
                            failure_diagnostics.update(action_accounting)
                            provider_accounting = action_accounting.get(
                                "provider_accounting"
                            )
                            if isinstance(provider_accounting, Mapping):
                                aggregate_cost = provider_accounting.get("cost_usd")
                                failure_cost_usd = (
                                    float(aggregate_cost)
                                    if isinstance(aggregate_cost, (int, float))
                                    and not isinstance(aggregate_cost, bool)
                                    else None
                                )
                self.store.block(
                    claim,
                    failure_code=self._blocked_failure_code(failure_stage, exc),
                    failure_diagnostics=(
                        failure_diagnostics if failure_diagnostics else None
                    ),
                    failure_cost_usd=(
                        float(failure_cost_usd)
                        if isinstance(failure_cost_usd, (int, float))
                        and not isinstance(failure_cost_usd, bool)
                        else None
                    ),
                    worktree_id=(None if workspace is None else workspace.worktree_id),
                    worktree_path=(None if workspace is None else workspace.path),
                )
            raise
        set_failure_stage("store_complete")
        try:
            self.store.complete(claim, result)
        except BaseException as exc:
            self.store.block(
                claim,
                failure_code=self._blocked_failure_code(failure_stage, exc),
                worktree_id=workspace.worktree_id,
                worktree_path=workspace.path,
            )
            raise
        if result.worker.result_commit_sha is None:
            cleanup_recorded_action = getattr(
                self.harness, "cleanup_recorded_action", None
            )
            if callable(cleanup_recorded_action):
                cleanup_recorded_action(action)
        assert workspace is not None
        self.workspaces.remove(workspace)
        return result

    def _reclaimed_for_retry(
        self,
        exc: BaseException,
        claim: ActionClaim,
        workspace: GitWorkspace | None,
    ) -> bool:
        """Give a claim back when the failure says nothing about the candidate.

        Blocking is the right default: a claim that failed somewhere unknown may
        have half-built a result, and re-running it blindly could bill twice or
        commit twice.  Unattested accounting is the one failure that carries no
        such doubt.  It is a transient state of our own ledger -- the receipt is
        stored and the authoritative row is merely late -- and it says nothing
        about the direction being implemented.  Left blocking, it would retire a
        whole action, and with it the second observation the process needs, for a
        condition that resolves itself in minutes.

        Reclaiming is still refused whenever anything durable may survive: the
        workspace has to be gone before the claim is dropped, and a claim that
        already recorded a commit is never reclaimed.
        """

        if not isinstance(exc, ProviderAccountingError):
            return False
        if workspace is None:
            try:
                self.store.release_unmutated(claim)
            except BaseException:
                return False
            return True
        record = self.store.get(claim.action_id)
        if record is None or record.result_commit_sha is not None:
            return False
        try:
            self.workspaces.remove(workspace)
            self.store.release_uncommitted(claim)
        except BaseException:
            return False
        return True


__all__ = [
    "AGENTPILOT_ACTION_EXECUTION_POLICY_CONTRACT",
    "ActionHarness",
    "CandidateAdherenceVerifier",
    "CandidateValidator",
    "DurableActionRunner",
    "HarnessResult",
    "LiveFenceCheck",
    "PairedEvaluator",
    "VisibleARejectionPolicy",
    "WorkspaceManager",
]
