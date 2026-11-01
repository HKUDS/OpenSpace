"""Explicit external bindings required by the construction root."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from agentpilot.app.contracts import EvaluationPlanFreezer, ExecutionResult
from agentpilot.app.runtime_config import AppConfig
from agentpilot.app.search_read_model import DurableSearchInputSource
from agentpilot.domain import (
    ActionExecutionFence,
    ActionExecutionMode,
    EvidenceTier,
    ParentSelection,
    PolicyAction,
    SearchState,
    utc_now,
)
from agentpilot.evaluation import EffectivenessRule
from agentpilot.persistence import (
    RunBatchLease,
    RunBatchLeaseManager,
    RunLeaseLost,
    UnitOfWorkFactory,
)
from agentpilot.providers import (
    CompletionProvider,
    EmbeddingAccountingReconciler,
    EmbeddingProvider,
    GenerationAccountingReconciler,
)
from agentpilot.search import DirectionApplicability, SemanticCalibrationBundle


class AssemblyError(RuntimeError):
    """The configured runtime cannot be constructed without guessing."""


@dataclass(frozen=True)
class NamedNearStatePredicate:
    """Bind a configured identity to an executable predicate."""

    predicate_id: str
    predicate: Callable[[object, object, str], bool]

    def __post_init__(self) -> None:
        if not isinstance(self.predicate_id, str) or not self.predicate_id.strip():
            raise ValueError("predicate_id must be non-empty")
        if not callable(self.predicate):
            raise TypeError("predicate must be callable")

    def __call__(self, observed: object, query: object, level: str) -> bool:
        result = self.predicate(observed, query, level)
        if not isinstance(result, bool):
            raise TypeError("neighborhood predicate must return boolean")
        return result


@dataclass(frozen=True)
class NamedApplicabilityBuilder:
    """Bind a configured identity to its executable builder."""

    builder_id: str
    builder: Callable[[SearchState, ParentSelection], DirectionApplicability]

    def __post_init__(self) -> None:
        if not isinstance(self.builder_id, str) or not self.builder_id.strip():
            raise ValueError("builder_id must be non-empty")
        if not callable(self.builder):
            raise TypeError("builder must be callable")

    def __call__(
        self, state: SearchState, parent: ParentSelection
    ) -> DirectionApplicability:
        result = self.builder(state, parent)
        if not isinstance(result, DirectionApplicability):
            raise TypeError("applicability builder returned the wrong type")
        return result


class ProviderRegistry:
    """Provider instances keyed by the names frozen in policy config."""

    def __init__(
        self,
        *,
        completions: Mapping[str, CompletionProvider],
        embeddings: Mapping[str, EmbeddingProvider] | None = None,
        completion_accounting_reconcilers: (
            Mapping[str, GenerationAccountingReconciler] | None
        ) = None,
        embedding_accounting_reconcilers: (
            Mapping[str, EmbeddingAccountingReconciler] | None
        ) = None,
    ) -> None:
        self._completions = dict(completions)
        self._embeddings = dict(embeddings or {})
        self._completion_accounting_reconcilers = dict(
            completion_accounting_reconcilers or {}
        )
        self._embedding_accounting_reconcilers = dict(
            embedding_accounting_reconcilers or {}
        )
        self._validate(self._completions, "completion", "complete")
        self._validate(self._embeddings, "embedding", "embed")
        self._validate_accounting_reconcilers(
            self._completion_accounting_reconcilers,
            self._completions,
            GenerationAccountingReconciler,
            "completion",
        )
        self._validate_accounting_reconcilers(
            self._embedding_accounting_reconcilers,
            self._embeddings,
            EmbeddingAccountingReconciler,
            "embedding",
        )

    @staticmethod
    def _validate(providers: Mapping[str, object], kind: str, method: str) -> None:
        for name, provider in providers.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"{kind} provider key must be non-empty")
            if getattr(provider, "provider_name", None) != name:
                raise ValueError(f"{kind} provider key/name mismatch for {name!r}")
            if not callable(getattr(provider, method, None)):
                raise TypeError(f"{kind} provider {name!r} lacks {method}()")

    def completion(self, name: str) -> CompletionProvider:
        try:
            return self._completions[name]
        except KeyError as exc:
            raise AssemblyError(
                f"completion provider {name!r} is not registered"
            ) from exc

    def embedding(self, name: str) -> EmbeddingProvider:
        try:
            return self._embeddings[name]
        except KeyError as exc:
            raise AssemblyError(
                f"embedding provider {name!r} is not registered"
            ) from exc

    @staticmethod
    def _validate_accounting_reconcilers(
        reconcilers: Mapping[str, object],
        providers: Mapping[str, object],
        reconciler_type: type,
        kind: str,
    ) -> None:
        for name, reconciler in reconcilers.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    f"{kind} accounting reconciler provider key must be non-empty"
                )
            if name not in providers:
                raise ValueError(
                    f"{kind} accounting reconciler provider {name!r} "
                    f"has no registered {kind} provider"
                )
            if not isinstance(reconciler, reconciler_type):
                raise TypeError(
                    f"{kind} accounting reconciler for {name!r} has the wrong type"
                )

    def completion_accounting_reconciler(
        self, name: str
    ) -> GenerationAccountingReconciler | None:
        return self._completion_accounting_reconcilers.get(name)

    def embedding_accounting_reconciler(
        self, name: str
    ) -> EmbeddingAccountingReconciler | None:
        return self._embedding_accounting_reconcilers.get(name)


class FencedActionRunner(Protocol):
    def execute(
        self, action: PolicyAction, fence: ActionExecutionFence
    ) -> ExecutionResult: ...

    def recover_blocked_uncommitted(
        self,
        action: PolicyAction,
        fence: ActionExecutionFence,
        *,
        expected_failure_code: str,
    ) -> None: ...


def assert_action_execution_fence_live(
    fence: ActionExecutionFence,
    lease_manager: RunBatchLeaseManager,
) -> RunBatchLease:
    """Reusable authoritative check for external mutation/commit boundaries."""

    current = lease_manager.current(fence.run_id)
    if (
        current is None
        or current.batch_id != fence.batch_id
        or current.owner_token != fence.owner_token
        or current.fencing_token != fence.fencing_token
        or not current.is_live_at(utc_now())
    ):
        raise RunLeaseLost("action execution fence is no longer live")
    return current


@dataclass(frozen=True)
class ActionRunnerBinding:
    """Lease-fenced external mutation/commit boundary."""

    runner_id: str
    runner: FencedActionRunner
    lease: RunBatchLease
    lease_manager: RunBatchLeaseManager
    scope_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.runner_id, str) or not self.runner_id.strip():
            raise ValueError("runner_id must be non-empty")
        if not callable(getattr(self.runner, "execute", None)):
            raise TypeError("runner must provide execute()")
        if not isinstance(self.lease, RunBatchLease):
            raise TypeError("action runner binding needs a RunBatchLease")
        if not callable(getattr(self.lease_manager, "current", None)):
            raise TypeError("action runner binding needs a lease manager")
        if not isinstance(self.scope_identity, str) or not self.scope_identity.strip():
            raise ValueError("scope_identity must be non-empty")

    def _fence(self, action: PolicyAction) -> ActionExecutionFence:
        if (action.run_id, action.batch_id) != (
            self.lease.run_id,
            self.lease.batch_id,
        ):
            raise ValueError("action differs from the bound run lease")
        return ActionExecutionFence.create(
            run_id=action.run_id,
            batch_id=action.batch_id,
            action_id=action.action_id,
            runner_id=self.runner_id,
            owner_token=self.lease.owner_token,
            fencing_token=self.lease.fencing_token,
            parent_commit_sha=action.parent_commit_sha,
            scope_identity=self.scope_identity,
        )

    def recover_blocked_uncommitted(
        self,
        action: PolicyAction,
        *,
        expected_failure_code: str,
    ) -> None:
        recover = getattr(self.runner, "recover_blocked_uncommitted", None)
        if not callable(recover):
            raise TypeError("action runner provides no blocked recovery API")
        fence = self._fence(action)
        self.assert_live(fence)
        recover(
            action,
            fence,
            expected_failure_code=expected_failure_code,
        )
        self.assert_live(fence)

    def execute(self, action: PolicyAction) -> ExecutionResult:
        fence = self._fence(action)
        self.assert_live(fence)
        result = self.runner.execute(action, fence)
        current_lease = self.assert_live(fence)
        received_at = utc_now()
        if not isinstance(result, ExecutionResult):
            raise TypeError("action runner returned the wrong result type")
        if (
            result.worker.action_id != action.action_id
            or result.outcome.action_id != action.action_id
        ):
            raise ValueError("action runner result differs from action_id")
        if result.outcome.evidence_tier not in {
            EvidenceTier.REALIZATION,
            EvidenceTier.ADAPTIVE_B,
        }:
            raise ValueError(
                "normal action runner accepts only realization or adaptive B outcomes"
            )
        attestation = result.worker.execution_attestation
        if attestation is None:
            raise ValueError("action runner returned no execution attestation")
        attestation.assert_matches(fence, result.worker.result_commit_sha)
        if attestation.mutation_fence_checked_at > result.worker.started_at:
            raise ValueError("mutation fence check happened after worker start")
        if attestation.commit_fence_checked_at > result.worker.completed_at:
            raise ValueError("commit fence check happened after worker completion")
        if attestation.mode is ActionExecutionMode.EXECUTED:
            if (
                attestation.mutation_fence_checked_at < current_lease.acquired_at
                or attestation.commit_fence_checked_at < current_lease.acquired_at
            ):
                raise ValueError("execution attestation predates its active lease")
            if result.worker.completed_at > received_at:
                raise ValueError("worker completion is later than result receipt")
        elif attestation.replay_verified_at is None:
            raise ValueError("replayed action lacks durable replay verification")
        elif attestation.original_fence_id == fence.fence_id:
            raise ValueError("replayed action must reference an earlier fence")
        elif attestation.replay_verified_at < current_lease.acquired_at:
            raise ValueError("durable replay predates the active execution lease")
        elif attestation.replay_verified_at < result.worker.completed_at:
            raise ValueError("durable replay predates the stored worker result")
        elif attestation.replay_verified_at > received_at:
            raise ValueError("durable replay verification is later than receipt")
        return result

    def assert_live(self, fence: ActionExecutionFence) -> RunBatchLease:
        current = assert_action_execution_fence_live(fence, self.lease_manager)
        if not current.same_claim(self.lease):
            raise RunLeaseLost("action execution fence is no longer live")
        return current


@dataclass(frozen=True)
class BatchAssemblyContext:
    """Per-batch context supplied to the external environment adapter."""

    config: AppConfig
    unit_of_work: UnitOfWorkFactory
    run_id: str
    batch_id: str
    lease: RunBatchLease
    lease_manager: RunBatchLeaseManager


@dataclass(frozen=True)
class Environment:
    """Real benchmark/worktree boundaries that assembly cannot invent."""

    search_inputs: DurableSearchInputSource
    action_runner: ActionRunnerBinding
    evaluation_plan_freezer: EvaluationPlanFreezer
    effectiveness_rule: EffectivenessRule
    parent_neighborhood: NamedNearStatePredicate | None = None
    direction_neighborhood: NamedNearStatePredicate | None = None
    applicability_builder: NamedApplicabilityBuilder | None = None
    trace_store: object | None = None
    definition_reader: object | None = None
    investigation_probe: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.search_inputs, DurableSearchInputSource):
            raise TypeError("search_inputs must be the canonical durable source")
        if not isinstance(self.action_runner, ActionRunnerBinding):
            raise TypeError("action_runner must be ActionRunnerBinding")
        if not isinstance(
            getattr(self.evaluation_plan_freezer, "freezer_id", None), str
        ) or not self.evaluation_plan_freezer.freezer_id.strip():
            raise TypeError("evaluation_plan_freezer needs a non-empty freezer_id")
        if not isinstance(
            getattr(self.evaluation_plan_freezer, "source_scope_id", None), str
        ) or not self.evaluation_plan_freezer.source_scope_id.strip():
            raise TypeError(
                "evaluation_plan_freezer needs a non-empty source_scope_id"
            )
        if not callable(getattr(self.evaluation_plan_freezer, "freeze_batch", None)):
            raise TypeError("evaluation_plan_freezer must provide freeze_batch()")
        if not isinstance(self.effectiveness_rule, EffectivenessRule):
            raise TypeError("effectiveness_rule must be EffectivenessRule")
        if (self.trace_store is None) is not (self.definition_reader is None):
            raise TypeError(
                "trace_store and definition_reader must be configured together"
            )
        if self.trace_store is not None and not all(
            callable(getattr(self.trace_store, name, None))
            for name in ("register_manifest", "read_case_events")
        ):
            raise TypeError("trace_store lacks the immutable trace read boundary")
        if self.definition_reader is not None and not callable(
            getattr(self.definition_reader, "read_definition", None)
        ):
            raise TypeError("definition_reader must provide read_definition()")
        if self.investigation_probe is not None and not callable(
            getattr(self.investigation_probe, "run", None)
        ):
            raise TypeError("investigation_probe must provide run()")


class EnvironmentFactory(Protocol):
    def __call__(self, context: BatchAssemblyContext) -> Environment: ...


@dataclass(frozen=True)
class AssemblyDependencies:
    providers: ProviderRegistry
    environment_factory: EnvironmentFactory
    semantic_calibration_bundle: SemanticCalibrationBundle | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.providers, ProviderRegistry):
            raise TypeError("providers must be ProviderRegistry")
        if not callable(self.environment_factory):
            raise TypeError("environment_factory must be callable")
        if self.semantic_calibration_bundle is not None and not isinstance(
            self.semantic_calibration_bundle, SemanticCalibrationBundle
        ):
            raise TypeError(
                "semantic_calibration_bundle must be a SemanticCalibrationBundle"
            )


__all__ = [
    "ActionRunnerBinding",
    "assert_action_execution_fence_live",
    "AssemblyError",
    "BatchAssemblyContext",
    "FencedActionRunner",
    "NamedApplicabilityBuilder",
    "NamedNearStatePredicate",
    "ProviderRegistry",
    "AssemblyDependencies",
    "Environment",
    "EnvironmentFactory",
]
