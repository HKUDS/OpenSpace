"""Benchmark evaluation contracts shared by the Arena kernel and extras."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional, Protocol, Union, runtime_checkable

from rsi_arena.contracts import (
    BenchIdentity,
    CellBudget,
    MetricSchema,
    SampleOutcomeKind,
    TaskRef,
)


@dataclass(frozen=True)
class CellResult:
    """A complete, canonical measurement produced by a bench.

    ``CellResult`` is deliberately *not* the generic error channel.  A result
    is measured only when it contains the primary and every constraint metric;
    adapters that obtained no valid score return :class:`UnmeasuredCell`
    instead.
    """

    outcome: str  # SampleOutcomeKind value
    raw_metrics: dict = field(default_factory=dict)
    duration_s: float = 0.0
    cost_usd: Optional[float] = 0.0
    cost_status: str = "auto"
    provider_calls: tuple[Mapping[str, object], ...] = ()
    diagnostics: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        if self.outcome not in {
            SampleOutcomeKind.PASS.value,
            SampleOutcomeKind.FAIL.value,
            SampleOutcomeKind.TIMEOUT.value,
        }:
            raise ValueError("cell result outcome is not supported")
        _validate_observation_cost(self)
        _freeze_provider_calls(self)
        object.__setattr__(
            self, "diagnostics", MappingProxyType(dict(self.diagnostics))
        )


@dataclass(frozen=True)
class CandidateFailure:
    """Optional, explicit attribution of a candidate-controlled failure.

    Generic/black-box adapters never need to construct this type: anything
    without a valid score safely becomes :class:`UnmeasuredCell`.  An adapter
    may use it only when a trusted outer supervisor observed a violation of a
    candidate-controlled contract (for example, a timed-out candidate process
    or malformed candidate-owned artifact).  The adapter supplies a complete
    metric vector because metric scales are bench-specific; the evaluator
    validates it exactly like any other measurement.
    """

    reason: str
    raw_metrics: dict
    outcome: str = SampleOutcomeKind.FAIL.value
    duration_s: float = 0.0
    cost_usd: Optional[float] = 0.0
    cost_status: str = "auto"
    provider_calls: tuple[Mapping[str, object], ...] = ()
    diagnostics: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        _validate_observation_cost(self)
        _freeze_provider_calls(self)
        object.__setattr__(
            self, "diagnostics", MappingProxyType(dict(self.diagnostics))
        )


@dataclass(frozen=True)
class UnmeasuredCell:
    """A bench invocation that produced no usable candidate measurement.

    This is the universal safe default for unavailable, malformed, or
    unattributed results.  The evaluator retries it, records diagnostics and
    cost outside canonical observation slots, and eventually returns an
    INCONCLUSIVE ticket if the slot remains unmeasured.
    """

    reason: str = "unmeasured"
    diagnostics: dict = field(default_factory=dict)
    duration_s: float = 0.0
    cost_usd: Optional[float] = 0.0
    cost_status: str = "auto"
    provider_calls: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        _validate_observation_cost(self)
        _freeze_provider_calls(self)


CellObservation = Union[CellResult, CandidateFailure, UnmeasuredCell]


def _freeze_provider_calls(observation: object) -> None:
    raw_calls = tuple(getattr(observation, "provider_calls"))
    if any(not isinstance(call, Mapping) for call in raw_calls):
        raise TypeError("provider_calls must contain mappings")
    object.__setattr__(
        observation,
        "provider_calls",
        tuple(MappingProxyType(dict(call)) for call in raw_calls),
    )


def _validate_observation_cost(observation: object) -> None:
    """Keep an absent provider cost distinct from an explicit zero."""

    cost = getattr(observation, "cost_usd")
    status = str(getattr(observation, "cost_status"))
    if status == "auto":
        status = "reported" if cost is not None else "missing"
        object.__setattr__(observation, "cost_status", status)
    if status not in {"reported", "missing", "unverified"}:
        raise ValueError("invalid observation cost_status")
    if status == "reported":
        if (
            not isinstance(cost, (int, float))
            or isinstance(cost, bool)
            or not math.isfinite(float(cost))
            or float(cost) < 0.0
        ):
            raise ValueError("reported observation cost is invalid")
    elif cost is not None:
        raise ValueError("non-reported observation cost must be null")


@dataclass(frozen=True)
class BenchIsolationCapabilities:
    """Auditable claims made by a concrete bench adapter.

    These flags deliberately distinguish cheap, useful fail-closed guards
    from a real security boundary.  In particular, validating the freshness
    and internal consistency of a result file does *not* make the process
    which wrote it trusted.  ``hermetic_candidate_fs`` and
    ``trusted_verifier_process`` therefore default to false and require a
    backend capability-probe digest before an adapter may claim either one.
    """

    task_material_manifest: bool = False
    result_freshness_validation: bool = False
    result_consistency_validation: bool = False
    manifest_redaction_canary: bool = False
    development_material_hook: bool = False
    # Hygiene only: moving HOME/TMPDIR does not hide explicitly passed API
    # credentials or constrain the host process's filesystem/network.
    run_private_home: bool = False
    # The bench can execute one candidate side for one cell in a disposable
    # worker.  When this is true LocalEvaluator also gives every cell/side a
    # distinct checkout and removes it as soon as that invocation returns, so
    # repository state cannot cross observation slots.  This is a strong claim:
    # the bench still owns process/container freshness and must substantiate it
    # with a backend probe digest.
    fresh_candidate_worker: bool = False
    candidate_secret_isolation: bool = False
    candidate_host_egress_isolation: bool = False
    trusted_verifier_process: bool = False
    hermetic_candidate_fs: bool = False
    # The verifier consumes only a bounded data artifact and never imports,
    # installs, or executes candidate-controlled code.  This separates a
    # fresh scoring process from the stronger delayed-payload property: SWE
    # has the former, while an integer-answer verifier has both.
    non_executable_artifact_verifier: bool = False
    backend_probe_digest: str | None = None

    def __post_init__(self) -> None:
        strong_claim = any(
            (
                self.candidate_secret_isolation,
                self.candidate_host_egress_isolation,
                self.trusted_verifier_process,
                self.hermetic_candidate_fs,
                self.fresh_candidate_worker,
                self.non_executable_artifact_verifier,
            )
        )
        if strong_claim and not (
            isinstance(self.backend_probe_digest, str)
            and self.backend_probe_digest.strip()
        ):
            raise ValueError(
                "strong bench isolation claims require a backend capability "
                "probe digest"
            )

    def as_public_dict(self) -> dict:
        """No paths, secrets or private-material digests enter this view."""
        return {
            "task_material_manifest": self.task_material_manifest,
            "result_freshness_validation": self.result_freshness_validation,
            "result_consistency_validation": self.result_consistency_validation,
            "manifest_redaction_canary": self.manifest_redaction_canary,
            "development_material_hook": self.development_material_hook,
            "run_private_home": self.run_private_home,
            "fresh_candidate_worker": self.fresh_candidate_worker,
            "candidate_secret_isolation": self.candidate_secret_isolation,
            "candidate_host_egress_isolation": self.candidate_host_egress_isolation,
            "trusted_verifier_process": self.trusted_verifier_process,
            "hermetic_candidate_fs": self.hermetic_candidate_fs,
            "non_executable_artifact_verifier": (self.non_executable_artifact_verifier),
            "backend_probe_digest": self.backend_probe_digest,
        }


@dataclass(frozen=True)
class TaskMaterialManifest:
    """Evaluator-side content identity for one task package.

    ``evaluator_digest`` covers the complete package, including tests and
    solutions.  ``redaction_canary`` is a one-way marker derived from the
    evaluator-only portion: its presence in a candidate payload proves that a
    private manifest was serialized across the boundary.  Candidate-facing
    code must use :meth:`candidate_view`, which intentionally contains
    neither field.
    """

    public_digest: str
    evaluator_digest: str
    redaction_canary: str
    public_file_count: int
    evaluator_only_file_count: int

    def candidate_view(self) -> dict:
        return {
            "public_digest": self.public_digest,
            "public_file_count": self.public_file_count,
        }


def bench_isolation_capabilities(adapter) -> BenchIsolationCapabilities:
    """Return a typed, conservative capability declaration.

    Adapters predating this protocol receive the all-false declaration; a
    class name or config string containing ``sandbox`` is never evidence.
    """
    provider = getattr(adapter, "isolation_capabilities", None)
    if provider is None:
        return BenchIsolationCapabilities()
    value = provider()
    if not isinstance(value, BenchIsolationCapabilities):
        raise TypeError(
            "bench isolation_capabilities() must return BenchIsolationCapabilities"
        )
    return value


@runtime_checkable
class BenchAdapter(Protocol):
    """Runs inside the evaluator domain; the search side never holds one."""

    def identity(self) -> BenchIdentity: ...

    def metric_schema(self) -> MetricSchema: ...

    def list_tasks(self) -> list[TaskRef]: ...

    def run_cell(
        self, checkout_path: Path, task: TaskRef, seed: int, budget: CellBudget
    ) -> CellObservation: ...

    def environment_profile(self) -> str: ...  # EnvProfile value
