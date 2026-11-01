"""TB2.1-owned development evidence request and pack contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from rsi_arena.contracts import canonical_digest


@dataclass(frozen=True)
class DevelopmentEpisode:
    """One fully visible TB2.1 development episode."""

    task_id: str
    task_statement: str = ""
    public_checks: str = ""
    expected_behavior: str = ""
    reference_solution: str = ""
    candidate_trajectory: str = ""
    candidate_output: str = ""
    verdict: str = ""


@dataclass(frozen=True)
class DevelopmentPack:
    """Frozen TB2.1 development material for its coding harness."""

    pack_id: str
    instructions: str = ""
    visible_criteria: str = ""
    episodes: tuple[DevelopmentEpisode, ...] = ()
    artifact_refs: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return canonical_digest(asdict(self))


@dataclass(frozen=True)
class RSIEvidenceRequest:
    """Stable coordinates supplied to the TB2.1 evidence provider."""

    experiment_id: str
    attempt_id: str
    selection_decision_id: str
    parent_commit_sha: str
    wave_id: str
    aggregate_feedback: dict = field(default_factory=dict)
    failure_summaries: tuple[str, ...] = ()
