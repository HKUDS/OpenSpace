"""Explicit construction root for the local reference runtime.

This module deliberately does not manufacture a benchmark environment or an
action executor.  Those boundaries perform the real worktree mutation and
paired evaluation, so callers must inject them through ``environment_factory``.
This module composes runtime lifecycle services from strict configuration and
delegates carrier-specific construction to ``carrier_assembly``.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Mapping

from agentpilot.app.archive import CanonicalArchiveTransitioner
from agentpilot.app.batch_loop import BatchLoop
from agentpilot.app.bindings import (
    ActionRunnerBinding,
    AssemblyDependencies,
    AssemblyError,
    BatchAssemblyContext,
    Environment,
    EnvironmentFactory,
    NamedApplicabilityBuilder,
    NamedNearStatePredicate,
    ProviderRegistry,
    assert_action_execution_fence_live,
)
from agentpilot.app.carrier_assembly import (
    FIXED_EVIDENCE_RETRIEVER_ID,
    CarrierAssembler,
)
from agentpilot.app.inputs import (
    CanonicalSearchInputs,
    CanonicalSearchStateBuilder,
)
from agentpilot.app.lease_runtime import BatchRuntime
from agentpilot.app.observations import CanonicalObservationBuilder
from agentpilot.app.runtime_config import AppConfig
from agentpilot.domain import canonical_json, utc_now
from agentpilot.persistence import (
    BatchStatus,
    RunBatchLease,
    RunLeaseLost,
    SQLiteUnitOfWorkFactory,
    UnitOfWorkFactory,
)

_UNCLOSED_BATCH_STATUSES = {
    BatchStatus.DRAFTING,
    BatchStatus.FROZEN,
    BatchStatus.RUNNING,
    BatchStatus.AT_BARRIER,
    BatchStatus.MERGED,
    BatchStatus.INTERRUPTED,
}


class RuntimeFactory:
    """Build a typed local runtime from strict config and real dependencies."""

    def __init__(self, config: AppConfig, dependencies: AssemblyDependencies) -> None:
        if not isinstance(config, AppConfig):
            raise TypeError("config must be AppConfig")
        if not isinstance(dependencies, AssemblyDependencies):
            raise TypeError("dependencies must be AssemblyDependencies")
        self.config = config
        self.dependencies = dependencies
        self._validate_storage_paths()
        self._base_unit_of_work = SQLiteUnitOfWorkFactory(
            config.runtime.database_path,
            timeout=config.runtime.sqlite_timeout_seconds,
        )
        self._lease_manager = self._base_unit_of_work.lease_manager()
        self._carrier_assembler = CarrierAssembler(
            config,
            dependencies.providers,
            semantic_calibration_bundle=(dependencies.semantic_calibration_bundle),
        )
        self._carrier_assembler.validate_registered_providers()

    def _validate_storage_paths(self) -> None:
        database = self.config.runtime.database_path
        runs_root = self.config.runtime.runs_root
        if not database.parent.exists() or not database.parent.is_dir():
            raise AssemblyError(
                f"SQLite parent directory does not exist: {database.parent}"
            )
        if database.exists() and not database.is_file():
            raise AssemblyError(f"SQLite path is not a regular file: {database}")
        if runs_root.exists() and not runs_root.is_dir():
            raise AssemblyError(f"runs_root is not a directory: {runs_root}")

    @staticmethod
    def _identity_text(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise AssemblyError(f"{name} must be non-empty")
        return value

    def _environment(
        self,
        run_id: str,
        batch_id: str,
        unit_of_work: UnitOfWorkFactory,
        lease: RunBatchLease,
    ) -> Environment:
        self._identity_text(run_id, "run_id")
        self._identity_text(batch_id, "batch_id")
        value = self.dependencies.environment_factory(
            BatchAssemblyContext(
                config=self.config,
                unit_of_work=unit_of_work,
                run_id=run_id,
                batch_id=batch_id,
                lease=lease,
                lease_manager=self._lease_manager,
            )
        )
        if not isinstance(value, Environment):
            raise AssemblyError("environment_factory must return Environment")
        if value.search_inputs.unit_of_work is not unit_of_work:
            raise AssemblyError(
                "durable search source must share the lease-fenced unit of work"
            )
        return value

    def _runtime_identity(self, environment: Environment) -> Mapping[str, object]:
        """Freeze algorithm and external adapter identity for exact resume."""

        source = environment.search_inputs
        run_identity = {
            "policy": self.config.policy,
            "carrier_behavior": self._carrier_assembler.behavior_identity(environment),
            "environment": {
                "factory": self.config.environment.factory,
                "identity": self.config.environment.identity,
            },
            "action_runner": {
                "runner_id": environment.action_runner.runner_id,
                "scope_identity": environment.action_runner.scope_identity,
            },
            "evaluation_plan_freezer": {
                "freezer_id": environment.evaluation_plan_freezer.freezer_id,
                "source_scope_id": (
                    environment.evaluation_plan_freezer.source_scope_id
                ),
            },
            "effectiveness_rule": environment.effectiveness_rule,
            "search_source": {
                "compatibility": source.compatibility,
            },
            "require_provider_reported_cost": (
                self.config.runtime.require_provider_reported_cost
            ),
        }
        payload = {
            "run_identity": run_identity,
            "search_input": {
                "parent_descriptor": source.parent_descriptor,
                "direction_descriptor": source.direction_descriptor,
                "budget": source.budget,
                "carrier_context": source.carrier_context,
            },
        }
        return json.loads(canonical_json(payload))

    def build(self, *, run_id: str, batch_id: str) -> BatchRuntime:
        return self._build_bound(run_id=run_id, batch_id=batch_id, resume_batch_id=None)

    def _build_bound(
        self,
        *,
        run_id: str,
        batch_id: str,
        resume_batch_id: str | None,
    ) -> BatchRuntime:
        self._identity_text(run_id, "run_id")
        self._identity_text(batch_id, "batch_id")
        lease = self._lease_manager.acquire(
            run_id=run_id,
            batch_id=batch_id,
            owner_token=str(uuid.uuid4()),
            now=utc_now(),
            ttl=timedelta(seconds=self.config.runtime.lease_ttl_seconds),
        )
        unit_of_work = self._base_unit_of_work.fenced(lease, clock=utc_now)
        try:
            self._validate_batch_closure(
                unit_of_work,
                run_id=run_id,
                batch_id=batch_id,
                resume_batch_id=resume_batch_id,
            )
            return self._build_with_lease(
                run_id=run_id,
                batch_id=batch_id,
                lease=lease,
                unit_of_work=unit_of_work,
            )
        except BaseException:
            try:
                self._lease_manager.release(lease, now=utc_now())
            except RunLeaseLost:
                pass
            raise

    @staticmethod
    def _validate_batch_closure(
        unit_of_work: UnitOfWorkFactory,
        *,
        run_id: str,
        batch_id: str,
        resume_batch_id: str | None,
    ) -> None:
        with unit_of_work() as work:
            batches = work.batches.list_for_run(run_id)
        if resume_batch_id is None and any(
            item.batch_id == batch_id for item in batches
        ):
            raise AssemblyError(
                f"batch {batch_id!r} already exists; use resume or a new id"
            )
        open_batches = tuple(
            item
            for item in batches
            if item.status in _UNCLOSED_BATCH_STATUSES
            and item.batch_id != resume_batch_id
        )
        if open_batches:
            identities = ", ".join(
                f"{item.batch_id}:{item.status.value}" for item in open_batches
            )
            raise AssemblyError(
                "run has an unclosed prior batch; resume it before starting "
                f"another batch ({identities})"
            )

    def _build_with_lease(
        self,
        *,
        run_id: str,
        batch_id: str,
        lease: RunBatchLease,
        unit_of_work: UnitOfWorkFactory,
    ) -> BatchRuntime:
        environment = self._environment(run_id, batch_id, unit_of_work, lease)
        runtime_identity = self._runtime_identity(environment)
        self._validate_run_identity(
            unit_of_work,
            run_id=run_id,
            runtime_identity=runtime_identity,
        )
        inputs = CanonicalSearchInputs(environment.search_inputs)
        state_builder = CanonicalSearchStateBuilder(inputs)
        planner, updater = self._carrier_assembler.assemble(
            run_id=run_id,
            batch_id=batch_id,
            environment=environment,
            inputs=inputs,
            unit_of_work=unit_of_work,
        )
        loop = BatchLoop(
            unit_of_work=unit_of_work,
            state_builder=state_builder,
            planner=planner,
            evaluation_plan_freezer=environment.evaluation_plan_freezer,
            runner=environment.action_runner,
            observation_builder=CanonicalObservationBuilder(
                environment.effectiveness_rule
            ),
            archive_transitioner=CanonicalArchiveTransitioner(),
            carrier_updater=updater,
            worker_slots=self.config.runtime.worker_slots,
            runtime_identity=runtime_identity,
        )
        return BatchRuntime(
            loop=loop,
            run_id=run_id,
            batch_id=batch_id,
            method=self.config.policy.method,
            lease_manager=self._lease_manager,
            lease=lease,
            lease_ttl=timedelta(seconds=self.config.runtime.lease_ttl_seconds),
            lease_renew_interval_seconds=(
                self.config.runtime.lease_renew_interval_seconds
            ),
        )

    @staticmethod
    def _validate_run_identity(
        unit_of_work: UnitOfWorkFactory,
        *,
        run_id: str,
        runtime_identity: Mapping[str, object],
    ) -> None:
        """Require one exact runtime identity throughout a run."""

        with unit_of_work() as work:
            batches = work.batches.list_for_run(run_id)
        if not isinstance(runtime_identity.get("run_identity"), Mapping):
            raise AssemblyError("configured runtime identity lacks run identity")

        drifted = []
        for batch in batches:
            if canonical_json(batch.runtime_identity.get("run_identity")) != (
                canonical_json(runtime_identity["run_identity"])
            ):
                drifted.append(batch.batch_id)
        if drifted:
            raise AssemblyError(
                "configured runtime identity differs from the durable run "
                f"identity ({', '.join(drifted)})"
            )

    def build_for_resume(self, *, batch_id: str) -> BatchRuntime:
        self._identity_text(batch_id, "batch_id")
        try:
            with self._base_unit_of_work() as work:
                batch = work.batches.get(batch_id)
        except KeyError as exc:
            raise AssemblyError(f"unknown durable batch: {batch_id}") from exc
        if batch.method is not self.config.policy.method:
            raise AssemblyError(
                "configured policy method differs from durable batch method"
            )
        if batch.status is BatchStatus.FAILED:
            raise AssemblyError("failed batch is not resumable")
        if batch.status is BatchStatus.PLANNING_EXHAUSTED:
            raise AssemblyError("planning-exhausted batch is not resumable")
        return self._build_bound(
            run_id=batch.run_id,
            batch_id=batch.batch_id,
            resume_batch_id=batch.batch_id,
        )


__all__ = [
    "ActionRunnerBinding",
    "assert_action_execution_fence_live",
    "AssemblyError",
    "BatchAssemblyContext",
    "FIXED_EVIDENCE_RETRIEVER_ID",
    "NamedApplicabilityBuilder",
    "NamedNearStatePredicate",
    "ProviderRegistry",
    "AssemblyDependencies",
    "BatchRuntime",
    "Environment",
    "EnvironmentFactory",
    "RuntimeFactory",
]
