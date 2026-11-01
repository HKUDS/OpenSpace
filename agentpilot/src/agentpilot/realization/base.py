"""Coding-harness boundary kept separate from proposal generation."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol

from agentpilot.domain import ImplementationAdherence, PolicyAction
from agentpilot.domain.common import deep_freeze, require_aware


@dataclass(frozen=True)
class HarnessExecution:
    started_at: datetime
    completed_at: datetime
    success: bool
    result_commit_sha: str | None
    touched_files: tuple[str, ...] = ()
    cost_usd: float = 0.0
    report: Mapping[str, Any] = field(default_factory=dict)
    error_kind: str | None = None

    def __post_init__(self) -> None:
        require_aware(self.started_at, "started_at")
        require_aware(self.completed_at, "completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("harness completion cannot precede start")
        if not math.isfinite(self.cost_usd) or self.cost_usd < 0.0:
            raise ValueError("harness cost must be finite and non-negative")
        object.__setattr__(self, "report", deep_freeze(self.report))


@dataclass(frozen=True)
class DiagnosticResult:
    passed: bool
    command: tuple[str, ...]
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    output_limit_exceeded: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise TypeError("diagnostic passed must be a boolean")
        if (
            not isinstance(self.command, tuple)
            or not self.command
            or any(
                not isinstance(item, str) or not item or "\x00" in item
                for item in self.command
            )
        ):
            raise ValueError("diagnostic command must be a non-empty text tuple")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise TypeError("diagnostic exit_code must be an integer or None")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("diagnostic output must be text")
        if not isinstance(self.timed_out, bool):
            raise TypeError("diagnostic timed_out must be a boolean")
        if not isinstance(self.output_limit_exceeded, bool):
            raise TypeError("diagnostic output_limit_exceeded must be a boolean")
        if (
            self.timed_out or self.output_limit_exceeded
        ) and self.exit_code is not None:
            raise ValueError("an interrupted diagnostic cannot have an exit code")
        if self.passed and (
            self.exit_code != 0 or self.timed_out or self.output_limit_exceeded
        ):
            raise ValueError("a passed diagnostic must have a clean zero exit")


class CodingHarness(Protocol):
    def implement(
        self,
        *,
        action: PolicyAction,
        worktree: Path,
        prior_execution: HarnessExecution | None = None,
        diagnostics: DiagnosticResult | None = None,
    ) -> HarnessExecution: ...


class DiagnosticsRunner(Protocol):
    def run(self, *, worktree: Path) -> DiagnosticResult: ...


class AdherenceAssessor(Protocol):
    def assess(
        self, *, action: PolicyAction, execution: HarnessExecution
    ) -> ImplementationAdherence: ...


__all__ = [
    "AdherenceAssessor",
    "CodingHarness",
    "DiagnosticResult",
    "DiagnosticsRunner",
    "HarnessExecution",
]
