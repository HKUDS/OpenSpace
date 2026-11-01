"""AIME adapter identity with honest runtime capability declarations."""

from __future__ import annotations

from dataclasses import dataclass

from ..runtime.errors import FormalRuntimeUnavailable
from .catalog import AIME_2024_2025_SNAPSHOT_DIGEST, AIME_TASK_COUNT, AIME_YEARS
from .renderer import (
    ANSWER_ARTIFACT_PATH,
    BUILD_TIMEOUT_SEC,
    DEBIAN_SNAPSHOT,
    DEFAULT_AGENT_TIMEOUT_SEC,
    PYTHON_BASE_IMAGE,
    VERIFIER_TIMEOUT_SEC,
)


@dataclass(frozen=True, slots=True)
class AimeRuntimeCapabilities:
    public_catalog: bool = True
    deterministic_harbor_renderer: bool = True
    strict_integer_scorer: bool = True
    cell_executor: bool = False
    private_c_store: bool = False
    sealed_execute_or_recover: bool = False
    exactly_once_execution: bool = False
    actual_cost_microusd_attestation: bool = False
    action_fencing: bool = False
    environment_factory: bool = False

    @property
    def formal_runtime_ready(self) -> bool:
        return all(
            (
                self.cell_executor,
                self.private_c_store,
                self.sealed_execute_or_recover,
                self.exactly_once_execution,
                self.actual_cost_microusd_attestation,
                self.action_fencing,
                self.environment_factory,
            )
        )

    @property
    def missing_formal_capabilities(self) -> tuple[str, ...]:
        names = (
            "cell_executor",
            "private_c_store",
            "sealed_execute_or_recover",
            "exactly_once_execution",
            "actual_cost_microusd_attestation",
            "action_fencing",
            "environment_factory",
        )
        return tuple(name for name in names if not getattr(self, name))


@dataclass(frozen=True, slots=True)
class AimeProfile:
    profile_id: str
    snapshot_digest: str
    task_count: int
    years: tuple[int, ...]
    cluster_schema: str
    answer_artifact_path: str
    integer_answer_syntax: str
    python_base_image: str
    debian_snapshot: str
    agent_timeout_sec: float
    verifier_timeout_sec: float
    build_timeout_sec: float
    verifier_environment_mode: str
    allow_internet: bool
    capabilities: AimeRuntimeCapabilities

    @property
    def formal_runtime_ready(self) -> bool:
        return self.capabilities.formal_runtime_ready

    def assert_formal_runtime_ready(self) -> None:
        if not self.formal_runtime_ready:
            missing = ", ".join(self.capabilities.missing_formal_capabilities)
            raise FormalRuntimeUnavailable(
                "AIME formal  runtime is unavailable; missing real trusted "
                f"capabilities: {missing}"
            )


AIME_PROFILE = AimeProfile(
    profile_id="aime-2024-2025",
    snapshot_digest=AIME_2024_2025_SNAPSHOT_DIGEST,
    task_count=AIME_TASK_COUNT,
    years=AIME_YEARS,
    cluster_schema="aime-<year>/<easy|medium|hard>",
    answer_artifact_path=ANSWER_ARTIFACT_PATH,
    integer_answer_syntax="(?:0|[1-9][0-9]{0,2})(?:\\n)?",
    python_base_image=PYTHON_BASE_IMAGE,
    debian_snapshot=DEBIAN_SNAPSHOT,
    agent_timeout_sec=DEFAULT_AGENT_TIMEOUT_SEC,
    verifier_timeout_sec=VERIFIER_TIMEOUT_SEC,
    build_timeout_sec=BUILD_TIMEOUT_SEC,
    verifier_environment_mode="separate",
    allow_internet=False,
    capabilities=AimeRuntimeCapabilities(
        cell_executor=True,
        action_fencing=True,
        environment_factory=True,
    ),
)


__all__ = ["AIME_PROFILE", "AimeProfile", "AimeRuntimeCapabilities"]
