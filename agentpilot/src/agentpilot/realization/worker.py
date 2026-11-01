"""Program realization with diagnostics only after a worktree edit."""

from __future__ import annotations

from pathlib import Path

from agentpilot.domain import (
    PolicyAction,
    WorkerExecutionStatus,
    WorkerResult,
    stable_id,
)
from agentpilot.realization.base import (
    AdherenceAssessor,
    CodingHarness,
    DiagnosticsRunner,
)


class ProgramRealizer:
    def __init__(
        self,
        *,
        harness: CodingHarness,
        adherence: AdherenceAssessor,
        diagnostics: DiagnosticsRunner | None = None,
        max_reworks: int = 0,
    ):
        if isinstance(max_reworks, bool) or max_reworks < 0:
            raise ValueError("max_reworks must be non-negative")
        self.harness = harness
        self.adherence = adherence
        self.diagnostics = diagnostics
        self.max_reworks = max_reworks

    def realize(self, action: PolicyAction, *, worktree: Path) -> WorkerResult:
        execution = self.harness.implement(action=action, worktree=worktree)
        first_started_at = execution.started_at
        total_cost_usd = execution.cost_usd
        touched_files = set(execution.touched_files)
        harness_attempts = [
            {
                "success": execution.success,
                "error_kind": execution.error_kind,
                "report": execution.report,
            }
        ]
        latest_diagnostic = None
        reworks = 0
        while execution.success and self.diagnostics is not None:
            latest_diagnostic = self.diagnostics.run(worktree=worktree)
            if latest_diagnostic.passed or reworks >= self.max_reworks:
                break
            execution = self.harness.implement(
                action=action,
                worktree=worktree,
                prior_execution=execution,
                diagnostics=latest_diagnostic,
            )
            total_cost_usd += execution.cost_usd
            touched_files.update(execution.touched_files)
            harness_attempts.append(
                {
                    "success": execution.success,
                    "error_kind": execution.error_kind,
                    "report": execution.report,
                }
            )
            reworks += 1

        completed = execution.success and (
            latest_diagnostic is None or latest_diagnostic.passed
        )
        status = (
            WorkerExecutionStatus.COMPLETED
            if completed
            else WorkerExecutionStatus.FAILED
        )
        diagnostics_payload = {
            "ran_after_edit": latest_diagnostic is not None,
            "reworks": reworks,
            "harness_attempts": harness_attempts,
        }
        if latest_diagnostic is not None:
            diagnostics_payload.update(
                {
                    "passed": latest_diagnostic.passed,
                    "command": list(latest_diagnostic.command),
                    "exit_code": latest_diagnostic.exit_code,
                    "timed_out": latest_diagnostic.timed_out,
                }
            )
        return WorkerResult(
            worker_result_id=stable_id("worker-result", action.action_id),
            action_id=action.action_id,
            status=status,
            adherence=self.adherence.assess(action=action, execution=execution),
            started_at=first_started_at,
            completed_at=execution.completed_at,
            result_commit_sha=execution.result_commit_sha,
            touched_files=tuple(sorted(touched_files)),
            diagnostics=diagnostics_payload,
            cost_usd=total_cost_usd,
        )


__all__ = ["ProgramRealizer"]
