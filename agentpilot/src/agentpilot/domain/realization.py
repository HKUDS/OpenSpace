"""Program-realization facts kept separate from outcome attribution."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Mapping

from agentpilot.domain.common import (
    deep_freeze,
    require_aware,
    require_text,
    stable_id,
)


class ImplementationAdherence(str, Enum):
    FOLLOWED = "followed"
    DEVIATED = "deviated"
    UNKNOWN = "unknown"


class WorkerExecutionStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    PROVIDER_FAILURE = "provider_failure"


class ActionExecutionMode(str, Enum):
    EXECUTED = "executed"
    REPLAYED = "replayed"


class RealizedFileChangeKind(str, Enum):
    """Tree-observed file change, independent of the proposed direction."""

    MODIFIED = "modified"
    ADDED = "added"
    REMOVED = "removed"
    RELOCATED = "relocated"


class FeasibilityStage(str, Enum):
    SCHEDULED = "scheduled"
    GROUNDED = "grounded_direction"
    REALIZED = "realized"
    ATTRIBUTABLE = "attributable"
    MEASURED = "measured"


@dataclass(frozen=True)
class FeasibilityObservation:
    """Record-only funnel progress; never an effectiveness reward."""

    observation_id: str
    action_id: str
    arm_id: str | None
    failure_mechanism: str
    reached_stages: tuple[FeasibilityStage, ...]
    cost_usd: float
    available_at: datetime

    def __post_init__(self) -> None:
        require_text(self.action_id, "action_id")
        if self.arm_id is not None:
            require_text(self.arm_id, "arm_id")
        require_text(self.failure_mechanism, "failure_mechanism")
        stages = tuple(self.reached_stages)
        object.__setattr__(self, "reached_stages", stages)
        order = tuple(FeasibilityStage)
        if not stages or stages != order[: len(stages)]:
            raise ValueError("feasibility stages must be a non-empty funnel prefix")
        if not math.isfinite(self.cost_usd) or self.cost_usd < 0.0:
            raise ValueError("feasibility cost must be finite and non-negative")
        require_aware(self.available_at, "available_at")
        if self.observation_id != stable_id("feasibility", self.action_id):
            raise ValueError("feasibility observation identity is not stable")


def _repository_path(value: str, name: str) -> str:
    normalized = require_text(value, name)
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or path.as_posix() != normalized
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in normalized
    ):
        raise ValueError(f"{name} must be a safe relative POSIX path")
    return normalized


@dataclass(frozen=True)
class RealizedFileChange:
    """One authoritative parent/child-tree change used by learning.

    The trusted Git reader classifies relocation only for an exact move. This
    record stores the resulting change, not Git's internal object identifiers.
    """

    kind: RealizedFileChangeKind
    file: str
    symbols: tuple[str, ...] = ()
    role: str | None = None
    from_file: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RealizedFileChangeKind):
            raise TypeError("realized file change kind is invalid")
        object.__setattr__(self, "file", _repository_path(self.file, "changed file"))
        symbols = tuple(self.symbols)
        object.__setattr__(self, "symbols", symbols)
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("realized change symbols must be sorted and unique")
        for symbol in symbols:
            require_text(symbol, "changed symbol")
        if self.role is not None:
            require_text(self.role, "added file role")
        if self.from_file is not None:
            object.__setattr__(
                self,
                "from_file",
                _repository_path(self.from_file, "relocation source"),
            )
        if self.kind is RealizedFileChangeKind.MODIFIED:
            if self.role is not None or self.from_file is not None:
                raise ValueError(
                    "modified change cannot carry role or relocation source"
                )
        elif self.kind is RealizedFileChangeKind.ADDED:
            if symbols or self.from_file is not None:
                raise ValueError("added change has invalid parent/symbol metadata")
        elif self.kind is RealizedFileChangeKind.REMOVED:
            if symbols or self.role is not None or self.from_file is not None:
                raise ValueError("removed change has invalid role/symbol metadata")
        else:
            if symbols or self.role is not None:
                raise ValueError("relocated change cannot carry role/symbol metadata")
            require_text(self.from_file or "", "relocation source")
            if self.from_file == self.file:
                raise ValueError("relocation source and destination must differ")


@dataclass(frozen=True)
class ActionExecutionFence:
    """Lease-bound authorization passed to an external mutation runner."""

    fence_id: str
    run_id: str
    batch_id: str
    action_id: str
    runner_id: str
    owner_token: str
    fencing_token: int
    parent_commit_sha: str
    scope_identity: str

    def __post_init__(self) -> None:
        for name in (
            "fence_id",
            "run_id",
            "batch_id",
            "action_id",
            "runner_id",
            "owner_token",
            "parent_commit_sha",
            "scope_identity",
        ):
            require_text(getattr(self, name), name)
        if isinstance(self.fencing_token, bool) or self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        expected = stable_id(
            "action-execution-fence",
            self.run_id,
            self.batch_id,
            self.action_id,
            self.runner_id,
            self.owner_token,
            self.fencing_token,
            self.parent_commit_sha,
            self.scope_identity,
        )
        if self.fence_id != expected:
            raise ValueError("action execution fence id is not stable")

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        batch_id: str,
        action_id: str,
        runner_id: str,
        owner_token: str,
        fencing_token: int,
        parent_commit_sha: str,
        scope_identity: str,
    ) -> "ActionExecutionFence":
        coordinates = (
            run_id,
            batch_id,
            action_id,
            runner_id,
            owner_token,
            fencing_token,
            parent_commit_sha,
            scope_identity,
        )
        return cls(
            stable_id("action-execution-fence", *coordinates), *coordinates
        )


@dataclass(frozen=True)
class ActionExecutionAttestation:
    """Runner evidence for a newly executed or durably replayed action."""

    fence_id: str
    runner_id: str
    worktree_id: str
    parent_commit_sha: str
    result_commit_sha: str | None
    mutation_fence_checked_at: datetime
    commit_fence_checked_at: datetime
    mode: ActionExecutionMode = ActionExecutionMode.EXECUTED
    original_fence_id: str | None = None
    replay_verified_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in (
            "fence_id",
            "runner_id",
            "worktree_id",
            "parent_commit_sha",
        ):
            require_text(getattr(self, name), name)
        if self.result_commit_sha is not None:
            require_text(self.result_commit_sha, "result_commit_sha")
        require_aware(
            self.mutation_fence_checked_at, "mutation_fence_checked_at"
        )
        require_aware(self.commit_fence_checked_at, "commit_fence_checked_at")
        if self.commit_fence_checked_at < self.mutation_fence_checked_at:
            raise ValueError("commit fence check precedes mutation fence check")
        if not isinstance(self.mode, ActionExecutionMode):
            raise TypeError("mode must be an ActionExecutionMode")
        if self.mode is ActionExecutionMode.EXECUTED:
            if (
                self.original_fence_id is not None
                or self.replay_verified_at is not None
            ):
                raise ValueError("executed attestation cannot carry replay provenance")
        else:
            require_text(self.original_fence_id or "", "original_fence_id")
            if self.replay_verified_at is None:
                raise ValueError("replayed attestation needs replay_verified_at")
            require_aware(self.replay_verified_at, "replay_verified_at")
            if self.replay_verified_at < self.commit_fence_checked_at:
                raise ValueError("replay verification precedes original commit check")

    def assert_matches(
        self, fence: ActionExecutionFence, result_commit_sha: str | None
    ) -> None:
        if (
            self.fence_id,
            self.runner_id,
            self.parent_commit_sha,
            self.result_commit_sha,
        ) != (
            fence.fence_id,
            fence.runner_id,
            fence.parent_commit_sha,
            result_commit_sha,
        ):
            raise ValueError("execution attestation differs from its fence/result")


@dataclass(frozen=True)
class WorkerResult:
    worker_result_id: str
    action_id: str
    status: WorkerExecutionStatus
    adherence: ImplementationAdherence
    started_at: datetime
    completed_at: datetime
    result_commit_sha: str | None = None
    touched_files: tuple[str, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    execution_attestation: ActionExecutionAttestation | None = None
    file_changes: tuple[RealizedFileChange, ...] = ()
    #: The anonymous Visible-A snapshot program that was actually measured, when
    #: candidate validation ran.  A rejected candidate has no result commit, so
    #: this identity is the only durable link to its published trace manifest.
    candidate_program_id: str | None = None

    def __post_init__(self) -> None:
        require_text(self.worker_result_id, "worker_result_id")
        require_text(self.action_id, "action_id")
        require_aware(self.started_at, "started_at")
        require_aware(self.completed_at, "completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("worker completion cannot precede start")
        if not math.isfinite(self.cost_usd) or self.cost_usd < 0.0:
            raise ValueError("worker cost must be finite and non-negative")
        changes = tuple(self.file_changes)
        object.__setattr__(self, "file_changes", changes)
        if any(not isinstance(item, RealizedFileChange) for item in changes):
            raise TypeError("file_changes must contain RealizedFileChange values")
        change_keys = tuple(
            (item.kind.value, item.from_file or "", item.file) for item in changes
        )
        if change_keys != tuple(sorted(set(change_keys))):
            raise ValueError("file_changes must be canonical, sorted, and unique")
        changed_endpoint_list = [
            endpoint
            for item in changes
            for endpoint in (item.file, item.from_file)
            if endpoint is not None
        ]
        if len(changed_endpoint_list) != len(set(changed_endpoint_list)):
            raise ValueError("a file may have only one authoritative final change")
        changed_endpoints = set(changed_endpoint_list)
        if not changed_endpoints <= set(self.touched_files):
            raise ValueError("file_changes must be covered by touched_files")
        if self.execution_attestation is not None:
            if not isinstance(
                self.execution_attestation, ActionExecutionAttestation
            ):
                raise TypeError(
                    "execution_attestation must be ActionExecutionAttestation"
                )
            if self.execution_attestation.result_commit_sha != (
                self.result_commit_sha
            ):
                raise ValueError("execution attestation result commit differs")
        if self.candidate_program_id is not None:
            require_text(self.candidate_program_id, "candidate_program_id")
        object.__setattr__(self, "diagnostics", deep_freeze(self.diagnostics))
