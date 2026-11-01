"""Production session composition over the existing Arena run authorities.

The plural spine assembles the existing recorder, candidate gateway, campaign
controller, benchmark adapters, and development owners behind one run-scoped
API without introducing another ledger or backend layer.
"""

from __future__ import annotations

import json
import shutil
import stat
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from rsi_arena.campaign_evaluation import (
    CampaignController,
    campaign_evaluator_digest,
)
from rsi_arena.common import UsageLedger
from rsi_arena.contracts import canonical_digest
from rsi_arena.development import RunScopedDevelopmentOperations
from rsi_arena.dto import DevelopmentResult, bounded_opaque_id
from rsi_arena.evaluation_gateway import TrustedEvaluationGateway
from rsi_arena.integration import (
    BenchmarkIntegration,
    TrustedResolvedCampaignConfig,
)
from rsi_arena.manifests import SampleSlot
from rsi_arena.run_layout import (
    AttemptRecord,
    CampaignEvaluationResult,
    CampaignOccurrenceState,
    CandidateRecord,
    RunLayout,
    RunLayoutError,
    RunRecorder,
    RunValidationError,
    _campaign_run_freeze_from_manifest,
)
from rsi_arena.runtime_campaign import CampaignCellPlan, RuntimeCampaignError
from rsi_arena.service import (
    ArenaCandidateRejected,
    ArenaUnknownAttempt,
    NativeCandidateMaterialization,
    NativeCandidateReservation,
    NativeCandidateTerminal,
    project_campaign_result,
)

MaterializeRawCandidate = Callable[[bytes, Path], Mapping[str, object]]


@dataclass
class SessionSpine:
    """One writer path over the existing durable Arena primitives.

    A session is bound to every benchmark child in one frozen campaign cell.
    ``open`` may publish that plural integration binding once; ``resume`` is
    read-only with respect to the run manifest and requires the binding.
    """

    configs: Mapping[str, TrustedResolvedCampaignConfig]
    integrations: Mapping[str, BenchmarkIntegration]
    layout: RunLayout
    recorder: RunRecorder
    gateway: TrustedEvaluationGateway
    controller: CampaignController
    developments: Mapping[str, RunScopedDevelopmentOperations]
    public_workspace: Path
    workspace: Path
    benchmark_ids: tuple[str, ...]
    attempt_cap: int
    development_concurrency: Mapping[str, int]
    evaluation_concurrency: int = 1
    invalid_cell_retries: int = 0
    _closed: bool = field(default=False, init=False, repr=False)
    _workspace_copy_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _submission_locks_guard: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _submission_locks: dict[str, threading.Lock] = field(
        default_factory=dict, init=False, repr=False
    )

    @classmethod
    def open(
        cls,
        configs: Mapping[str, TrustedResolvedCampaignConfig],
        integrations: Mapping[str, BenchmarkIntegration],
        *,
        layout: RunLayout,
        gateway: TrustedEvaluationGateway,
        development_concurrency: Mapping[str, int],
        usage_ledger: UsageLedger | None = None,
        candidate_root: Path | None = None,
    ) -> "SessionSpine":
        """Open the thin spine for a newly created, evaluator-bound layout."""

        return cls._compose(
            configs,
            integrations,
            layout=layout,
            gateway=gateway,
            development_concurrency=development_concurrency,
            usage_ledger=usage_ledger,
            candidate_root=candidate_root,
            fresh=True,
        )

    @classmethod
    def resume(
        cls,
        configs: Mapping[str, TrustedResolvedCampaignConfig],
        integrations: Mapping[str, BenchmarkIntegration],
        *,
        layout: RunLayout,
        gateway: TrustedEvaluationGateway,
        development_concurrency: Mapping[str, int],
        usage_ledger: UsageLedger | None = None,
        candidate_root: Path | None = None,
    ) -> "SessionSpine":
        """Resume the same thin spine from an already validated layout."""

        return cls._compose(
            configs,
            integrations,
            layout=layout,
            gateway=gateway,
            development_concurrency=development_concurrency,
            usage_ledger=usage_ledger,
            candidate_root=candidate_root,
            fresh=False,
        )

    @classmethod
    def _compose(
        cls,
        configs: Mapping[str, TrustedResolvedCampaignConfig],
        integrations: Mapping[str, BenchmarkIntegration],
        *,
        layout: RunLayout,
        gateway: TrustedEvaluationGateway,
        development_concurrency: Mapping[str, int],
        usage_ledger: UsageLedger | None,
        candidate_root: Path | None,
        fresh: bool,
    ) -> "SessionSpine":
        (
            benchmark_ids,
            frozen_configs,
            frozen_integrations,
            frozen_development_concurrency,
            attempt_cap,
            evaluation_concurrency,
            invalid_cell_retries,
        ) = _validate_plural_composition(
            configs,
            integrations,
            development_concurrency,
            gateway=gateway,
        )
        plans = {
            benchmark_id: frozen_integrations[benchmark_id].plan
            for benchmark_id in benchmark_ids
        }
        adapters = {
            benchmark_id: frozen_integrations[benchmark_id].campaign_adapter
            for benchmark_id in benchmark_ids
        }
        evaluator_digest = campaign_evaluator_digest(plans, adapters)
        _bind_integration_manifest(
            layout,
            frozen_configs,
            frozen_integrations,
            benchmark_ids,
            evaluator_digest,
            frozen_development_concurrency,
            attempt_cap,
            invalid_cell_retries,
            fresh=fresh,
        )

        recorder = RunRecorder(
            layout,
            evaluator_digest=evaluator_digest,
            benchmark_task_ids={
                benchmark_id: frozen_integrations[
                    benchmark_id
                ].plan.development_task_ids
                for benchmark_id in benchmark_ids
            },
            operation="attempt-evaluation",
        )
        try:
            controller = CampaignController(
                plans=plans,
                adapters=adapters,
                layout=layout,
                recorder=recorder,
                gateway=gateway,
                max_concurrency=evaluation_concurrency,
                invalid_cell_retries=invalid_cell_retries,
                usage_ledger=usage_ledger,
            )
            public_workspace = layout.assert_workspace_isolated(
                layout.method_root / "workspace"
            )
            workspace = layout.assert_workspace_isolated(
                candidate_root or public_workspace
            )
            try:
                workspace.relative_to(public_workspace)
            except ValueError as exc:
                raise RunLayoutError(
                    "session spine candidate root must be inside the method workspace"
                ) from exc
            namespaced = len(benchmark_ids) > 1
            developments = {
                benchmark_id: RunScopedDevelopmentOperations(
                    layout=layout,
                    adapter=frozen_integrations[benchmark_id].development_adapter,
                    workspace=public_workspace,
                    candidate_root=workspace,
                    benchmark_namespace=benchmark_id if namespaced else None,
                    usage_ledger=usage_ledger,
                )
                for benchmark_id in benchmark_ids
            }
        except BaseException:
            recorder.close()
            raise
        return cls(
            configs=frozen_configs,
            integrations=frozen_integrations,
            layout=layout,
            recorder=recorder,
            gateway=gateway,
            controller=controller,
            developments=MappingProxyType(developments),
            public_workspace=public_workspace,
            workspace=workspace,
            benchmark_ids=benchmark_ids,
            attempt_cap=attempt_cap,
            evaluation_concurrency=evaluation_concurrency,
            invalid_cell_retries=invalid_cell_retries,
            development_concurrency=frozen_development_concurrency,
        )

    @property
    def candidate_evaluations_used(self) -> int:
        return self.controller.candidate_evaluations_used

    @property
    def attempts_used(self) -> int:
        return self.controller.attempts_used

    @property
    def all_attempts_terminal(self) -> bool:
        return self.controller.all_attempts_terminal

    @property
    def campaign_aggregate_kind(self) -> str | None:
        return self.controller.campaign_aggregate_kind

    def attempt(self, attempt_id: str) -> AttemptRecord | None:
        self._require_open()
        return self.controller.attempt(bounded_opaque_id(attempt_id, "attempt_id"))

    def candidate_state(self, candidate_id: str) -> CampaignOccurrenceState:
        self._require_open()
        return self.controller.candidate_state(candidate_id)

    def result_for_occurrence(
        self, submission_identity: str
    ) -> CampaignOccurrenceState | None:
        self._require_open()
        return self.controller.result_for_occurrence(submission_identity)

    def campaign_terminal_results(self) -> tuple[CampaignEvaluationResult, ...]:
        self._require_open()
        return self.controller.campaign_terminal_results()

    @property
    def delivered_terminal_ids(self) -> frozenset[str]:
        self._require_open()
        return self.recorder.delivered_terminal_ids

    def record_evaluation_delivered(self, result: CampaignEvaluationResult) -> None:
        self._require_open()
        self.controller.record_evaluation_delivered(result)

    def run_development_tasks(
        self,
        benchmark_tasks: Mapping[str, Sequence[str]],
        *,
        candidate_workspace: Path | None = None,
    ) -> Mapping[str, DevelopmentResult]:
        """Run ordered development views at composition-frozen concurrency."""

        self._require_open()
        if not isinstance(benchmark_tasks, Mapping):
            raise RunValidationError("development tasks must be a benchmark mapping")
        unknown = set(benchmark_tasks) - set(self.benchmark_ids)
        if unknown:
            raise RunValidationError("development request names an unknown benchmark")
        selected = {
            benchmark_id: tuple(benchmark_tasks[benchmark_id])
            for benchmark_id in self.benchmark_ids
            if benchmark_id in benchmark_tasks and benchmark_tasks[benchmark_id]
        }
        selected_ids = tuple(selected)
        if not selected_ids:
            raise RunValidationError("development request selected no tasks")

        def run_one(benchmark_id: str) -> DevelopmentResult:
            operation = self.developments[benchmark_id]
            if candidate_workspace is None:
                return operation.run_set(selected[benchmark_id])
            return operation.run_set(
                selected[benchmark_id],
                candidate_workspace=candidate_workspace,
            )

        if len(selected_ids) == 1:
            benchmark_id = selected_ids[0]
            results = {benchmark_id: run_one(benchmark_id)}
        else:
            with ThreadPoolExecutor(
                max_workers=len(selected_ids),
                thread_name_prefix="arena-development-benchmark",
            ) as pool:
                futures = {
                    benchmark_id: pool.submit(run_one, benchmark_id)
                    for benchmark_id in selected_ids
                }
                results = {
                    benchmark_id: futures[benchmark_id].result()
                    for benchmark_id in selected_ids
                }
        return MappingProxyType(results)

    def start_attempt(
        self,
        attempt_id: str,
        *,
        max_attempts: int | None = None,
        kind: str | None = None,
        benchmark_slots: Mapping[str, Sequence[SampleSlot]] | None = None,
    ) -> AttemptRecord:
        """Durably start with explicit slots, or replay by ID only."""

        self._require_open()
        if max_attempts is not None and max_attempts != self.attempt_cap:
            raise RunValidationError("session spine attempt cap changed")
        bound_attempt_id = bounded_opaque_id(attempt_id, "attempt_id")
        if (
            benchmark_slots is None
            and self.controller.attempt(bound_attempt_id) is None
        ):
            raise ArenaUnknownAttempt
        return self.controller.start_attempt(
            bound_attempt_id,
            max_attempts=self.attempt_cap,
            kind=kind,
            benchmark_slots=benchmark_slots,
        )

    def transition_attempt(self, attempt_id: str, decision: str) -> AttemptRecord:
        self._require_open()
        return self.controller.transition_attempt(
            bounded_opaque_id(attempt_id, "attempt_id"), decision
        )

    def evaluate_candidate(
        self,
        candidate_workspace: Path,
        *,
        native_candidate_id: str,
        attempt_id: str,
        role: str,
        parent_candidate_id: str | None = None,
    ) -> CampaignEvaluationResult:
        """Campaign-operation spelling used by the existing service projection."""

        return self.submit_candidate(
            candidate_workspace,
            attempt_id=attempt_id,
            submission_identity=native_candidate_id,
            role=role,
            parent_candidate_id=parent_candidate_id,
        )

    def prepare_native_workspace(
        self,
        candidate_workspace: Path,
        *,
        occurrence_key: str,
        attempt_id: str,
        role: str,
        parent_candidate_id: str | None = None,
    ):
        self._require_open()
        return self.controller.prepare_native_workspace(
            candidate_workspace,
            occurrence_key=occurrence_key,
            attempt_id=attempt_id,
            role=role,
            parent_candidate_id=parent_candidate_id,
        )

    def evaluate_prepared_candidate(
        self,
        candidate: CandidateRecord,
        issued,
    ) -> CampaignEvaluationResult:
        self._require_open()
        return self.controller.evaluate_prepared_candidate(candidate, issued)

    def submit_candidate(
        self,
        candidate_workspace: Path,
        *,
        attempt_id: str,
        submission_identity: str,
        role: str,
        parent_candidate_id: str | None = None,
    ) -> CampaignEvaluationResult:
        """Freeze one Git snapshot durably, then evaluate its frozen slots."""

        self._require_open()
        attempt = bounded_opaque_id(attempt_id, "attempt_id")
        submission = bounded_opaque_id(submission_identity, "submission_identity")
        with self._submission_lock(submission):
            return self.controller.evaluate_candidate(
                Path(candidate_workspace),
                native_candidate_id=submission,
                attempt_id=attempt,
                role=role,
                parent_candidate_id=parent_candidate_id,
            )

    def evaluate_blob_candidate(
        self,
        payload: bytes | str,
        *,
        attempt_id: str,
        submission_identity: str,
        media_type: str,
        materialize: MaterializeRawCandidate,
        role: str,
        parent_candidate_id: str | None = None,
    ) -> CampaignEvaluationResult:
        """Materialize baseline-owned bytes, publish Git, admit, and evaluate.

        Raw bytes and rejection persistence remain caller-owned.  This method
        writes no proposal row and returns no proposal terminal projection.
        """
        reservation = self.reserve_native_candidate(
            payload,
            attempt_id=attempt_id,
            occurrence_key=submission_identity,
            media_type=media_type,
            role=role,
            parent_candidate_id=parent_candidate_id,
        )
        materialized = self.materialize_native_candidate(
            reservation,
            materialize=materialize,
        )
        if isinstance(materialized, NativeCandidateTerminal):
            return materialized.result
        return self.evaluate_native_candidate(materialized).result

    def reserve_native_candidate(
        self,
        payload: bytes | str,
        *,
        attempt_id: str,
        occurrence_key: str,
        media_type: str,
        role: str,
        parent_candidate_id: str | None = None,
    ) -> NativeCandidateReservation:
        """Bind baseline-owned bytes to an existing durable attempt."""

        self._require_open()
        attempt = bounded_opaque_id(attempt_id, "attempt_id")
        occurrence = bounded_opaque_id(occurrence_key, "occurrence_key")
        if not isinstance(media_type, str) or not media_type:
            raise ValueError("native candidate media type cannot be empty")
        if isinstance(payload, str):
            raw = payload.encode("utf-8")
        elif isinstance(payload, bytes):
            raw = payload
        else:
            raise TypeError("native candidate payload must be bytes or text")
        self.start_attempt(attempt)
        terminal = self._validate_raw_submission_contract(
            attempt_id=attempt,
            submission_identity=occurrence,
            role=role,
            parent_candidate_id=parent_candidate_id,
        )
        return NativeCandidateReservation(
            occurrence_key=occurrence,
            attempt_id=attempt,
            raw=raw,
            terminal_result=terminal,
            role=role,
            parent_candidate_id=parent_candidate_id,
        )

    def materialize_native_candidate(
        self,
        reservation: NativeCandidateReservation,
        materialize: MaterializeRawCandidate,
    ) -> NativeCandidateMaterialization | NativeCandidateTerminal:
        """Materialize and durably admit one canonical candidate."""

        self._require_open()
        if reservation.terminal_result is not None:
            return self._native_terminal(
                reservation,
                reservation.terminal_result,
                materialization_status="resumed_terminal",
            )
        workspace = self._isolated_raw_workspace(reservation.occurrence_key)
        try:
            try:
                provenance = dict(materialize(reservation.raw, workspace))
            except ArenaCandidateRejected:
                raise
            except Exception as exc:
                raise ArenaCandidateRejected("native_materialization_failed") from exc
            try:
                candidate, issued = self.prepare_native_workspace(
                    workspace,
                    occurrence_key=reservation.occurrence_key,
                    attempt_id=reservation.attempt_id,
                    role=reservation.role,
                    parent_candidate_id=reservation.parent_candidate_id,
                )
            except Exception as exc:
                state = self.controller.result_for_occurrence(
                    reservation.occurrence_key
                )
                attempt = self.controller.attempt(reservation.attempt_id)
                if (
                    state is None
                    and attempt is not None
                    and attempt.state in {"PENDING", "OPEN"}
                ):
                    raise ArenaCandidateRejected(
                        "canonical_snapshot_publication_failed"
                    ) from exc
                raise
            return NativeCandidateMaterialization(
                reservation,
                provenance,
                workspace,
                candidate,
                issued,
            )
        except BaseException:
            shutil.rmtree(workspace, ignore_errors=True)
            raise

    def evaluate_native_candidate(
        self,
        materialized: NativeCandidateMaterialization,
    ) -> NativeCandidateTerminal:
        """Evaluate one admitted native candidate through the shared controller."""

        self._require_open()
        reservation = materialized.reservation
        try:
            with self._submission_lock(reservation.occurrence_key):
                state = self.controller.result_for_occurrence(
                    reservation.occurrence_key
                )
                result = state.terminal_result if state is not None else None
                if result is None:
                    result = self.evaluate_prepared_candidate(
                        materialized.candidate,
                        materialized.issued,
                    )
                    status = "materialized"
                else:
                    status = "resumed_materialized"
            return self._native_terminal(
                reservation,
                result,
                materialization_status=status,
            )
        finally:
            self.discard_native_candidate_materialization(materialized)

    def discard_native_candidate_materialization(
        self, materialized: NativeCandidateMaterialization
    ) -> None:
        """Release an unevaluated temp checkout while retaining its durable freeze."""

        self._require_open()
        if not isinstance(materialized, NativeCandidateMaterialization):
            raise TypeError("native materialization is invalid")
        raw_workspace = Path(materialized.workspace)
        root = (self.layout.trusted_runtime_root / "native-workspaces").resolve(
            strict=True
        )
        workspace = raw_workspace.resolve(strict=False)
        if (
            workspace.parent.parent != root
            or not workspace.parent.name.startswith("submission-")
            or not workspace.name.startswith("materialization-")
        ):
            raise RunValidationError(
                "native materialization workspace escaped its owner"
            )
        try:
            workspace_info = raw_workspace.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RunValidationError(
                "native materialization workspace is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(workspace_info.st_mode)
            or raw_workspace.resolve(strict=True) != workspace
        ):
            raise RunValidationError(
                "native materialization workspace escaped its owner"
            )
        shutil.rmtree(workspace, ignore_errors=True)
        try:
            workspace.parent.rmdir()
        except OSError:
            pass

    @staticmethod
    def _native_terminal(
        reservation: NativeCandidateReservation,
        result: CampaignEvaluationResult,
        *,
        materialization_status: str,
    ) -> NativeCandidateTerminal:
        if (
            result.attempt_id != reservation.attempt_id
            or result.candidate_role != reservation.role
        ):
            raise RunValidationError("native terminal attempt binding is invalid")
        return NativeCandidateTerminal(
            reservation=reservation,
            result=result,
            selection=project_campaign_result(
                reservation.occurrence_key,
                result,
            ).as_dict(),
            materialization_status=materialization_status,
        )

    def seal_attempt(self, attempt_id: str) -> AttemptRecord:
        self._require_open()
        return self.controller.seal_attempt(bounded_opaque_id(attempt_id, "attempt_id"))

    def attempt_result(self, attempt_id: str) -> tuple[CampaignOccurrenceState, ...]:
        """Return recorder-validated candidate states in durable attempt order."""

        self._require_open()
        attempt = self.controller.attempt(bounded_opaque_id(attempt_id, "attempt_id"))
        if attempt is None:
            raise RunValidationError("attempt result names an unknown attempt")
        return tuple(
            self.controller.candidate_state(candidate_id)
            for candidate_id in attempt.candidate_ids
        )

    def close(self) -> None:
        if self._closed:
            return
        self.recorder.close()
        self._closed = True

    def __enter__(self) -> "SessionSpine":
        self._require_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("session spine is closed")

    def _submission_lock(self, submission_identity: str) -> threading.Lock:
        with self._submission_locks_guard:
            return self._submission_locks.setdefault(
                submission_identity, threading.Lock()
            )

    def _validate_raw_submission_contract(
        self,
        *,
        attempt_id: str,
        submission_identity: str,
        role: str,
        parent_candidate_id: str | None,
        snapshot_digest: str | None = None,
    ) -> CampaignEvaluationResult | None:
        self.recorder.validate_git_native_candidate_admission(
            native_candidate_id=submission_identity,
            attempt_id=attempt_id,
            role=role,
            parent_candidate_id=parent_candidate_id,
        )
        if parent_candidate_id is not None:
            self.recorder.candidate_by_id(parent_candidate_id)
        state = self.controller.result_for_occurrence(submission_identity)
        if state is None:
            return None
        candidate = self.recorder.candidate_by_id(state.candidate_id)
        if (
            state.attempt_id != attempt_id
            or candidate.role != role
            or candidate.parent_candidate_id != parent_candidate_id
            or (
                snapshot_digest is not None
                and candidate.snapshot_digest != snapshot_digest
            )
        ):
            raise RunValidationError("raw candidate replay changed identity")
        return state.terminal_result

    def _isolated_raw_workspace(self, submission_identity: str) -> Path:
        root = self.layout.trusted_runtime_root / "native-workspaces"
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        candidate_root = root / (
            "submission-"
            + canonical_digest({"submission_identity": submission_identity})[:20]
        )
        destination = candidate_root / f"materialization-{uuid.uuid4().hex}"
        staging = root / f".stage-{uuid.uuid4().hex}"
        with self._workspace_copy_lock:
            try:
                shutil.copytree(self.workspace, staging, symlinks=True)
                candidate_root.mkdir(mode=0o700, exist_ok=True)
                staging.replace(destination)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        return destination.resolve(strict=True)


def _bind_integration_manifest(
    layout: RunLayout,
    configs: Mapping[str, TrustedResolvedCampaignConfig],
    integrations: Mapping[str, BenchmarkIntegration],
    benchmark_ids: tuple[str, ...],
    evaluator_digest: str,
    development_concurrency: Mapping[str, int],
    attempt_cap: int,
    invalid_cell_retries: int,
    *,
    fresh: bool,
) -> None:
    path = layout.root / "run-manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunValidationError("session spine run identity is invalid") from exc
    first = integrations[benchmark_ids[0]]
    campaign_run_freeze = _campaign_run_freeze_from_manifest(manifest)
    aggregate = first.plan.aggregate.as_dict() if first.plan.aggregate else None
    if (
        manifest.get("campaign_id") != first.plan.campaign_id
        or manifest.get("run_scope_id") != first.plan.cell_id
        or manifest.get("method") != first.plan.method
        or manifest.get("run_kind") != first.plan.run_kind
        or campaign_run_freeze.get("campaign_freeze_id")
        != first.campaign_freeze.freeze_id
        or campaign_run_freeze.get("campaign_cell_id") != first.plan.cell_id
        or campaign_run_freeze.get("benchmark_ids") != list(benchmark_ids)
        or campaign_run_freeze.get("aggregate_policy") != aggregate
    ):
        raise RunValidationError("session spine run authority changed")
    existing_evaluator = manifest.get("evaluator_digest")
    changed = False
    if existing_evaluator != evaluator_digest:
        if not fresh or existing_evaluator is not None:
            raise RunValidationError("session spine evaluator identity changed")
        manifest["evaluator_digest"] = evaluator_digest
        changed = True
    binding = {
        "schema": "rsi-arena-benchmark-integrations-binding",
        "benchmark_ids": list(benchmark_ids),
        "campaign_run_freeze_identity": campaign_run_freeze["resume_identity"],
        "attempt_cap": attempt_cap,
        "invalid_cell_retries": invalid_cell_retries,
        "bindings": {
            benchmark_id: {
                "plan_identity_digest": "sha256:"
                + canonical_digest(_plan_identity(integrations[benchmark_id].plan)),
                "resource_identity_digest": "sha256:"
                + canonical_digest(dict(integrations[benchmark_id].resource_identity)),
                "benchmark_concurrency": configs[benchmark_id].benchmark_concurrency,
                "development_concurrency": development_concurrency[benchmark_id],
            }
            for benchmark_id in benchmark_ids
        },
    }
    if "arena_benchmark_integration" in manifest:
        raise RunValidationError(
            "session spine singular integration binding is invalid"
        )
    existing = manifest.get("arena_benchmark_integrations")
    if fresh:
        if existing is not None:
            raise RunValidationError(
                "session spine fresh open found an existing integration binding"
            )
        manifest["arena_benchmark_integrations"] = binding
        changed = True
    else:
        if existing is None:
            raise RunValidationError("session spine integration binding is missing")
        if existing != binding:
            raise RunValidationError("session spine integration identity changed")
    if changed:
        layout.write_manifest("run-manifest.json", manifest)


def _plan_identity(plan: CampaignCellPlan) -> Mapping[str, object]:
    if not isinstance(plan, CampaignCellPlan):
        raise TypeError("session spine plan identity is invalid")
    aggregate = plan.aggregate
    return {
        "campaign_id": plan.campaign_id,
        "campaign_freeze_id": plan.freeze_id,
        "campaign_cell_id": plan.cell_id,
        "benchmark_id": plan.benchmark,
        "source_reference": plan.source_reference,
        "method": plan.method,
        "run_kind": plan.run_kind,
        "aggregate": aggregate.as_dict() if aggregate is not None else None,
        "dataset_boundary": plan.dataset_boundary.as_dict(),
    }


def _validate_plural_composition(
    configs: Mapping[str, TrustedResolvedCampaignConfig],
    integrations: Mapping[str, BenchmarkIntegration],
    development_concurrency: Mapping[str, int],
    *,
    gateway: TrustedEvaluationGateway,
) -> tuple[
    tuple[str, ...],
    Mapping[str, TrustedResolvedCampaignConfig],
    Mapping[str, BenchmarkIntegration],
    Mapping[str, int],
    int,
    int,
    int,
]:
    if not isinstance(configs, Mapping) or not configs:
        raise TypeError("session spine configs must be a non-empty mapping")
    if not isinstance(integrations, Mapping):
        raise TypeError("session spine integrations must be a mapping")
    if not isinstance(development_concurrency, Mapping):
        raise TypeError("development concurrency must be a mapping")
    first_config = next(iter(configs.values()))
    if not isinstance(first_config, TrustedResolvedCampaignConfig):
        raise TypeError("session spine config must be trusted and resolved")
    freeze = first_config.campaign_freeze
    cell_id = first_config.plan.cell_id
    cell = freeze.cells.get(cell_id)
    if cell is None:
        raise RuntimeCampaignError("session spine campaign cell is unavailable")
    benchmark_ids = cell.benchmark_ids
    supplied_orders = (
        tuple(configs),
        tuple(integrations),
        tuple(development_concurrency),
    )
    if any(order != benchmark_ids for order in supplied_orders):
        raise RunValidationError(
            "session spine mappings must exactly follow active campaign cell order"
        )

    campaign_identities: set[tuple[object, ...]] = set()
    frozen_configs: dict[str, TrustedResolvedCampaignConfig] = {}
    frozen_integrations: dict[str, BenchmarkIntegration] = {}
    frozen_development_concurrency: dict[str, int] = {}
    for benchmark_id in benchmark_ids:
        config = configs[benchmark_id]
        integration = integrations[benchmark_id]
        concurrency = development_concurrency[benchmark_id]
        if not isinstance(config, TrustedResolvedCampaignConfig):
            raise TypeError("session spine config must be trusted and resolved")
        if not isinstance(integration, BenchmarkIntegration):
            raise TypeError("session spine requires BenchmarkIntegration values")
        if (
            config.campaign_freeze != freeze
            or config.plan.cell_id != cell_id
            or config.plan.benchmark != benchmark_id
            or integration.benchmark_id != benchmark_id
            or integration.campaign_freeze != freeze
            or integration.plan != config.plan
        ):
            raise RuntimeCampaignError(
                "session spine child changed resolved campaign authority"
            )
        if (
            gateway.trusted_root != integration.trusted_root
            or gateway.scope != integration.scope
        ):
            raise RunValidationError("session spine gateway authority changed")
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or concurrency < 1
        ):
            raise ValueError("development concurrency must be positive")
        campaign_identities.add(
            (
                config.plan.campaign_id,
                config.plan.freeze_id,
                config.plan.cell_id,
                config.plan.method,
                config.plan.run_kind,
                config.plan.aggregate,
                config.attempt_cap,
                config.benchmark_concurrency,
                config.invalid_cell_retries,
            )
        )
        frozen_configs[benchmark_id] = config
        frozen_integrations[benchmark_id] = integration
        frozen_development_concurrency[benchmark_id] = concurrency
    if len(campaign_identities) != 1:
        raise RunValidationError(
            "session spine campaign-level identity changed across active cell"
        )
    attempt_cap = first_config.attempt_cap
    evaluation_concurrency = first_config.benchmark_concurrency
    invalid_cell_retries = first_config.invalid_cell_retries
    return (
        benchmark_ids,
        MappingProxyType(frozen_configs),
        MappingProxyType(frozen_integrations),
        MappingProxyType(frozen_development_concurrency),
        attempt_cap,
        evaluation_concurrency,
        invalid_cell_retries,
    )


__all__ = ["MaterializeRawCandidate", "SessionSpine"]
