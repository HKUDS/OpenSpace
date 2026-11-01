"""Method-neutral Terminal-Bench 2.1 campaign adapter.

The adapter owns benchmark semantics only: it validates a TB2 projection of
the active ``CampaignFreeze``, preserves controller-realized ``SampleSlot``
order, binds every task to the verified official Git snapshot, and translates
one execution-backend verdict into the shared ``TaskEvaluationOutcome``.

Candidate freezing, candidate-evaluation slots, recording, aggregation, and
native feedback remain in the shared campaign controller/data plane. Sampling
realization and baseline search state belong to the calling method.
No Harbor or Docker backend is constructed here; a future backend can
implement the small protocol below.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from rsi_arena.benchmarks.task_records import to_task_refs
from rsi_arena.common import ROLES
from rsi_arena.contracts import TaskRef, canonical_digest
from rsi_arena.evaluation_gateway import GatewayCandidate
from rsi_arena.manifests import SampleSlot
from rsi_arena.run_layout import (
    EVALUATION_SCHEMA,
    RunValidationError,
    TaskEvaluationOutcome,
    _encode_jsonl_record,
    _validate_evaluation_boundary,
)
from rsi_arena.runtime_campaign import (
    CampaignCellPlan,
    RuntimeCampaignError,
    runtime_realization_record,
)

from .tasks import (
    TB21PinnedTaskMaterial,
    TB21SnapshotError,
    TB21TaskSnapshot,
    validate_tb21_task_snapshot,
)

_OUTCOME_SCHEMA = "tb21-method-neutral-task-outcome"
_ADAPTER_SCHEMA = "tb21-campaign-benchmark-adapter"
_TASK_CONTEXT_META_KEY = "_tb21_campaign_slot"
_TOKEN_STATUSES = frozenset({"reported", "recounted", "missing", "unverified"})
_COST_STATUSES = frozenset({"reported", "missing", "unverified"})
_INCONCLUSIVE_FAILURE_KINDS = frozenset(
    {"infra", "provider", "evaluator", "backend", "unknown"}
)
_EVALUATION_USAGE_ROLES = frozenset({"solver-agent", "verifier/judge"})
_BACKEND_PRIVATE_EVIDENCE_KEYS = frozenset(
    {
        "api_key",
        "credential",
        "credentials",
        "secret",
        "secrets",
        "password",
        "control_material",
    }
)


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _json_boundary_copy(value: object) -> object:
    """Return JSON-native evidence or reject backend-controlled values."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("TB2 backend evidence contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("TB2 backend evidence keys must be strings")
            copied[key] = _json_boundary_copy(child)
        return copied
    if isinstance(value, (list, tuple)):
        return [_json_boundary_copy(child) for child in value]
    raise ValueError("TB2 backend evidence is not JSON boundary-safe")


def _reject_backend_private_evidence_keys(value: object) -> None:
    """Reject explicit private/control fields without inspecting text values."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _BACKEND_PRIVATE_EVIDENCE_KEYS:
                raise ValueError("TB2 backend evidence contains a private field")
            _reject_backend_private_evidence_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_backend_private_evidence_keys(child)


def _nonnegative_integer(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"invalid TB2 backend {field_name}")
    return value


def _nonnegative_finite(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise ValueError(f"invalid TB2 backend {field_name}")
    return float(value)


def _directional_token_status(
    status: object,
    value: int | None,
    field_name: str,
) -> str:
    if not isinstance(status, str) or status not in _TOKEN_STATUSES:
        raise ValueError(f"invalid TB2 backend {field_name}")
    if status in {"reported", "recounted"} and value is None:
        raise ValueError(f"TB2 backend {field_name} lacks an observed count")
    if status == "missing" and value is not None:
        raise ValueError(f"TB2 backend {field_name} contradicts an observed count")
    return status


@dataclass(frozen=True)
class TB2CampaignTask:
    """One controller-realized task plus candidate-visible pinned Git blobs."""

    sample_slot: SampleSlot
    task_ref: TaskRef
    snapshot_digest: str
    source_commit: str
    repository_tree: str
    tasks_tree: str
    instruction: bytes
    task_manifest: bytes
    instruction_ref: str
    task_manifest_ref: str

    def __post_init__(self) -> None:
        if self.task_ref.canonical_identity != (
            "tb2",
            self.sample_slot.task_id,
        ):
            raise ValueError("TB2 execution task lost its sample-slot identity")


@dataclass(frozen=True)
class TB2TaskOutcome:
    """Backend-neutral terminal verdict for one exact sample slot.

    ``failed`` is reserved for candidate-caused, trustworthy zeroes.
    Infrastructure, provider, backend, and evaluator failures must use
    ``inconclusive`` and therefore cannot be converted to a score.
    """

    sample_slot: SampleSlot
    verdict: str
    failure_kind: str | None = None
    error_type: str | None = None
    wall_seconds: float | None = None
    artifact_refs: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    provenance: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_tokens: int | None = None
    token_status: str = "missing"
    input_token_status: str = "auto"
    output_token_status: str = "auto"
    cache_token_status: str = "missing"
    cost_usd: float | None = None
    cost_status: str = "missing"

    def __post_init__(self) -> None:
        if self.verdict not in {"passed", "failed", "inconclusive"}:
            raise ValueError(f"unknown TB2 task verdict: {self.verdict!r}")
        if self.verdict == "passed" and self.failure_kind is not None:
            raise ValueError("passed TB2 task cannot carry a failure kind")
        if self.verdict == "failed" and self.failure_kind != "candidate":
            raise ValueError("failed TB2 task must be candidate-caused")
        if (
            self.verdict == "inconclusive"
            and self.failure_kind not in _INCONCLUSIVE_FAILURE_KINDS
        ):
            raise ValueError(
                "inconclusive TB2 task needs an infra/provider/backend/evaluator "
                "classification"
            )
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.artifact_refs.items()
        ):
            raise ValueError("TB2 task artifact references must be strings")


class TB2CampaignExecutionBackend(Protocol):
    """Minimal execution seam shared by fake and future real TB2 backends.

    A backend with a long trajectory should be configured with the trusted
    run-private artifact sink by its assembler.  It must durably publish the
    complete artifact before returning a short run-relative ref/digest here;
    only a bounded summary belongs in inline provenance.  The adapter neither
    owns that sink nor exposes it to the candidate.
    """

    @property
    def backend_digest(self) -> str: ...

    def execute(
        self,
        candidate: GatewayCandidate,
        task: TB2CampaignTask,
    ) -> TB2TaskOutcome: ...


def bind_held_out_task_slots(tasks: Sequence[TaskRef]) -> tuple[TaskRef, ...]:
    """Bind frozen held-out order to adapter-consumed slot metadata."""

    bound: list[TaskRef] = []
    for sample_index, task in enumerate(tasks):
        meta = dict(task.meta)
        if _TASK_CONTEXT_META_KEY in meta:
            raise RunValidationError("TB2 task metadata uses a reserved field")
        meta[_TASK_CONTEXT_META_KEY] = SampleSlot(sample_index, task.task_id).as_dict()
        bound.append(replace(task, meta=meta))
    return tuple(bound)


class TB2CampaignBenchmarkAdapter:
    """CampaignFreeze-bound TB2 semantics over one injected task backend."""

    benchmark_id = "tb2"

    def __init__(
        self,
        snapshot: TB21TaskSnapshot,
        backend: TB2CampaignExecutionBackend,
    ) -> None:
        try:
            validate_tb21_task_snapshot(snapshot)
        except TB21SnapshotError as exc:
            raise RunValidationError(
                "TB2 campaign adapter requires a verified task snapshot"
            ) from exc
        backend_digest = getattr(backend, "backend_digest", None)
        if not isinstance(backend_digest, str) or not backend_digest.strip():
            raise ValueError("TB2 campaign backend needs a stable digest")
        records = tuple(snapshot.tasks)
        refs = tuple(to_task_refs(list(records), benchmark_id=self.benchmark_id))
        task_refs = {task.task_id: task for task in refs}
        materials = {item.task_id: item for item in snapshot.pinned_materials}
        if (
            len(task_refs) != len(refs)
            or len(materials) != len(snapshot.pinned_materials)
            or set(task_refs) != set(materials)
        ):
            raise RunValidationError(
                "verified TB2 snapshot has ambiguous task records/materials"
            )
        self._snapshot = snapshot
        self._backend = backend
        self._task_refs = MappingProxyType(task_refs)
        self._materials = MappingProxyType(materials)
        self._source_reference = f"terminal-bench-2.1@{snapshot.commit}"
        self._evaluator_digest = canonical_digest(
            {
                "schema": _ADAPTER_SCHEMA,
                "outcome_schema": _OUTCOME_SCHEMA,
                "benchmark_id": self.benchmark_id,
                "source_reference": self._source_reference,
                "snapshot_identity": snapshot.identity,
                "snapshot_digest": snapshot.snapshot_digest,
                "backend_digest": backend_digest,
            }
        )

    @property
    def evaluator_digest(self) -> str:
        return self._evaluator_digest

    @property
    def execution_backend(self) -> TB2CampaignExecutionBackend:
        return self._backend

    def resolve_tasks(
        self,
        plan: CampaignCellPlan,
        sample_slots: Sequence[SampleSlot],
    ) -> tuple[TaskRef, ...]:
        """Validate, but never realize or reorder, controller-owned slots."""

        slots = tuple(sample_slots)
        if plan.benchmark != self.benchmark_id:
            raise RuntimeCampaignError(
                "TB2 adapter requires its frozen TB2 campaign plan"
            )
        if plan.source_reference != self._source_reference:
            raise RuntimeCampaignError(
                "TB2 campaign source reference disagrees with the verified snapshot"
            )
        snapshot_ids = set(self._task_refs)
        if not set(plan.development_task_ids).issubset(snapshot_ids):
            raise RuntimeCampaignError(
                "TB2 development membership escapes the verified snapshot"
            )
        runtime_realization_record(plan, slots)
        resolved: list[TaskRef] = []
        for slot in slots:
            try:
                task = self._task_refs[slot.task_id]
            except KeyError as exc:
                raise RunValidationError(
                    "TB2 plan selected an unavailable verified task"
                ) from exc
            meta = dict(task.meta)
            if _TASK_CONTEXT_META_KEY in meta:
                raise RunValidationError("TB2 task metadata uses a reserved field")
            meta[_TASK_CONTEXT_META_KEY] = slot.as_dict()
            resolved.append(replace(task, meta=meta))
        return tuple(resolved)

    def evaluate_task(
        self,
        candidate: GatewayCandidate,
        task: TaskRef,
    ) -> TaskEvaluationOutcome:
        if task.canonical_identity != (self.benchmark_id, task.task_id):
            raise RunValidationError("TB2 task lost benchmark scope")
        slot = self._slot_from_task(task)
        try:
            material = self._materials[slot.task_id]
        except KeyError as exc:
            raise RunValidationError(
                "TB2 execution task is absent from pinned Git materials"
            ) from exc
        request = self._execution_task(slot, task, material)
        try:
            outcome = self._backend.execute(candidate, request)
        except Exception as exc:
            outcome = TB2TaskOutcome(
                sample_slot=slot,
                verdict="inconclusive",
                failure_kind="backend",
                error_type="tb2_backend_exception",
                provenance=MappingProxyType({"exception_type": type(exc).__name__}),
            )
        if not isinstance(outcome, TB2TaskOutcome):
            outcome = TB2TaskOutcome(
                sample_slot=slot,
                verdict="inconclusive",
                failure_kind="backend",
                error_type="tb2_backend_invalid_outcome",
            )
        if outcome.sample_slot != slot:
            outcome = TB2TaskOutcome(
                sample_slot=slot,
                verdict="inconclusive",
                failure_kind="backend",
                error_type="tb2_backend_changed_sample_identity",
            )
        try:
            outcome = self._validate_backend_outcome(outcome)
            shared_outcome = self._shared_outcome(request, outcome)
        except Exception:
            # Invalid backend evidence/usage is itself an inconclusive backend
            # result.  Source/sampling/task contracts were checked above this
            # backend-owned boundary, so they are not swallowed here.  Do not
            # leave the shared recorder with an open child.
            return self._sanitized_backend_outcome(request, slot)
        try:
            self._validate_terminal_record(request, shared_outcome)
        except ValueError as exc:
            if str(exc) != "JSONL record exceeds the 10 MiB boundary":
                return self._sanitized_backend_outcome(request, slot)
            # The backend payload was otherwise valid.  Drop only oversized
            # inline evidence while retaining task truth, accounting, and a
            # narrow safe artifact ref/digest summary.
            artifact_refs = self._overflow_artifact_refs(outcome.artifact_refs)
            if artifact_refs is None:
                return self._sanitized_backend_outcome(request, slot)
            compact_provenance: dict[str, object] = {}
            if "usage" in outcome.provenance:
                compact_provenance["usage"] = _json_boundary_copy(
                    outcome.provenance["usage"]
                )
            compact = replace(
                outcome,
                artifact_refs=artifact_refs,
                provenance=MappingProxyType(compact_provenance),
            )
            retained = self._shared_outcome(request, compact)
            try:
                self._validate_terminal_record(request, retained)
            except Exception:
                return self._sanitized_backend_outcome(request, slot)
            return retained
        return shared_outcome

    @staticmethod
    def _sanitized_backend_outcome(
        request: TB2CampaignTask,
        slot: SampleSlot,
    ) -> TaskEvaluationOutcome:
        return TB2CampaignBenchmarkAdapter._shared_outcome(
            request,
            TB2TaskOutcome(
                sample_slot=slot,
                verdict="inconclusive",
                failure_kind="backend",
                error_type="tb2_backend_invalid_outcome",
            ),
        )

    @staticmethod
    def _overflow_artifact_refs(
        artifact_refs: Mapping[str, str],
    ) -> Mapping[str, str] | None:
        """Accept empty refs or narrow run-relative ref/digest pairs."""

        retained: dict[str, str] = {}
        ref_names: set[str] = set()
        digest_names: set[str] = set()
        for key, value in artifact_refs.items():
            if key.endswith("_ref"):
                relative = Path(value)
                if (
                    not value
                    or value == "."
                    or "\x00" in value
                    or relative.is_absolute()
                    or ".." in relative.parts
                ):
                    return None
                retained[key] = value
                ref_names.add(key.removesuffix("_ref"))
            elif key.endswith("_digest"):
                prefix, separator, hex_digest = value.partition(":")
                if (
                    prefix != "sha256"
                    or separator != ":"
                    or len(hex_digest) != 64
                    or any(
                        character not in "0123456789abcdef" for character in hex_digest
                    )
                ):
                    return None
                retained[key] = value
                digest_names.add(key.removesuffix("_digest"))
            else:
                return None
        if ref_names != digest_names:
            return None
        return MappingProxyType(retained)

    @staticmethod
    def _validate_backend_outcome(outcome: TB2TaskOutcome) -> TB2TaskOutcome:
        """Validate every backend-owned field before any shared recorder write."""

        if outcome.verdict not in {"passed", "failed", "inconclusive"}:
            raise ValueError("TB2 backend verdict is invalid")
        if outcome.verdict == "passed" and outcome.failure_kind is not None:
            raise ValueError("passed TB2 backend verdict carries failure state")
        if outcome.verdict == "failed" and outcome.failure_kind != "candidate":
            raise ValueError("failed TB2 backend verdict is not candidate-caused")
        if (
            outcome.verdict == "inconclusive"
            and outcome.failure_kind not in _INCONCLUSIVE_FAILURE_KINDS
        ):
            raise ValueError("inconclusive TB2 backend verdict is unclassified")
        if outcome.error_type is not None and not isinstance(outcome.error_type, str):
            raise ValueError("TB2 backend error_type must be a string or null")
        if outcome.verdict == "passed" and outcome.error_type is not None:
            raise ValueError("passed TB2 backend verdict cannot carry an error")
        wall_seconds = _nonnegative_finite(outcome.wall_seconds, "wall_seconds")

        if not isinstance(outcome.artifact_refs, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in outcome.artifact_refs.items()
        ):
            raise ValueError("TB2 backend artifact references must be strings")
        copied_artifact_refs = dict(outcome.artifact_refs)
        _reject_backend_private_evidence_keys(copied_artifact_refs)
        _validate_evaluation_boundary(copied_artifact_refs)
        artifact_refs = MappingProxyType(copied_artifact_refs)

        if not isinstance(outcome.provenance, Mapping):
            raise ValueError("TB2 backend provenance must be an object")
        copied = _json_boundary_copy(outcome.provenance)
        if not isinstance(copied, dict):
            raise ValueError("TB2 backend provenance must be a JSON object")
        _reject_backend_private_evidence_keys(copied)
        _validate_evaluation_boundary(copied)
        if "tb2_task_identity" in copied:
            raise ValueError("TB2 backend used reserved task identity provenance")

        input_tokens = _nonnegative_integer(outcome.input_tokens, "input_tokens")
        output_tokens = _nonnegative_integer(outcome.output_tokens, "output_tokens")
        cache_tokens = _nonnegative_integer(outcome.cache_tokens, "cache_tokens")
        if not isinstance(outcome.token_status, str) or (
            outcome.token_status not in _TOKEN_STATUSES
        ):
            raise ValueError("TB2 backend token_status is invalid")
        token_status = outcome.token_status
        if token_status in {"reported", "recounted"} and (
            input_tokens is None or output_tokens is None
        ):
            raise ValueError("reported TB2 backend usage lacks directional counts")
        if token_status == "missing" and (
            input_tokens is not None or output_tokens is not None
        ):
            raise ValueError("missing TB2 backend usage carries token counts")

        input_status = outcome.input_token_status
        if input_status == "auto":
            input_status = token_status if input_tokens is not None else "missing"
        input_status = _directional_token_status(
            input_status, input_tokens, "input_token_status"
        )
        output_status = outcome.output_token_status
        if output_status == "auto":
            output_status = token_status if output_tokens is not None else "missing"
        output_status = _directional_token_status(
            output_status, output_tokens, "output_token_status"
        )
        cache_status = _directional_token_status(
            outcome.cache_token_status,
            cache_tokens,
            "cache_token_status",
        )

        cost_usd = _nonnegative_finite(outcome.cost_usd, "cost_usd")
        if not isinstance(outcome.cost_status, str) or (
            outcome.cost_status not in _COST_STATUSES
        ):
            raise ValueError("TB2 backend cost_status is invalid")
        if outcome.cost_status == "reported" and cost_usd is None:
            raise ValueError("reported TB2 backend cost is null")
        if outcome.cost_status != "reported" and cost_usd is not None:
            raise ValueError("unverified TB2 backend cost must be null")

        provenance = TB2CampaignBenchmarkAdapter._normalize_usage_provenance(
            copied,
            token_status=token_status,
            input_status=input_status,
            output_status=output_status,
            cache_status=cache_status,
            cache_tokens=cache_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_status=outcome.cost_status,
        )
        return replace(
            outcome,
            wall_seconds=wall_seconds,
            artifact_refs=artifact_refs,
            provenance=MappingProxyType(provenance),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_tokens=cache_tokens,
            token_status=token_status,
            input_token_status=input_status,
            output_token_status=output_status,
            cache_token_status=cache_status,
            cost_usd=cost_usd,
        )

    @staticmethod
    def _normalize_usage_provenance(
        provenance: dict[str, object],
        *,
        token_status: str,
        input_status: str,
        output_status: str,
        cache_status: str,
        cache_tokens: int | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_status: str,
    ) -> dict[str, object]:
        raw_usage = provenance.get("usage")
        if raw_usage is None:
            has_top_level_usage = (
                input_tokens is not None
                or output_tokens is not None
                or cache_tokens is not None
                or token_status != "missing"
                or input_status != "missing"
                or output_status != "missing"
                or cache_status != "missing"
                or cost_status != "missing"
            )
            if not has_top_level_usage:
                return provenance
            usage: dict[str, object] = {
                "role": "solver-agent",
                "provider": "unknown",
                "model": "unknown",
                "token_source": "unknown",
                "token_status": token_status,
                "input_token_status": input_status,
                "output_token_status": output_status,
                "cache_token_status": cache_status,
                "cache_status": (
                    "reported_undifferentiated"
                    if cache_tokens is not None
                    else "unknown"
                ),
                "cost_status": cost_status,
            }
            if input_tokens is not None:
                usage["input_tokens"] = input_tokens
            if output_tokens is not None:
                usage["output_tokens"] = output_tokens
            if cache_tokens is not None:
                usage["cache_tokens"] = cache_tokens
            provenance["usage"] = usage
            return provenance
        if not isinstance(raw_usage, dict):
            raise ValueError("TB2 backend usage provenance must be an object")
        usage = dict(raw_usage)
        role = usage.get("role", "solver-agent")
        if (
            not isinstance(role, str)
            or role not in ROLES
            or role not in _EVALUATION_USAGE_ROLES
        ):
            raise ValueError("TB2 backend usage role is invalid")
        for name in ("provider", "model"):
            if not isinstance(usage.get(name, "unknown"), str):
                raise ValueError(f"TB2 backend usage {name} is invalid")
        token_source = usage.get("token_source", "unknown")
        if not isinstance(token_source, str) or not token_source.strip():
            raise ValueError("TB2 backend usage token_source is invalid")
        if token_source == "local_recount" and token_status != "recounted":
            raise ValueError("TB2 backend local recount is mislabeled")
        if "token_status" in usage and usage["token_status"] != token_status:
            raise ValueError("TB2 backend usage token_status contradicts outcome")

        for name, expected in (
            ("input_token_status", input_status),
            ("output_token_status", output_status),
        ):
            observed = usage.get(name, expected)
            if observed != expected:
                raise ValueError(f"TB2 backend usage {name} contradicts outcome")
            usage[name] = expected

        cache_read_tokens = _nonnegative_integer(
            usage.get("cache_read_tokens"), "usage.cache_read_tokens"
        )
        if (
            cache_tokens is not None
            and cache_read_tokens is not None
            and cache_read_tokens != cache_tokens
        ):
            raise ValueError("TB2 backend cache token evidence contradicts outcome")
        effective_cache_tokens = (
            cache_tokens if cache_tokens is not None else cache_read_tokens
        )
        observed_cache_status = usage.get("cache_token_status", cache_status)
        observed_cache_status = _directional_token_status(
            observed_cache_status,
            effective_cache_tokens,
            "usage.cache_token_status",
        )
        if cache_tokens is not None and observed_cache_status != cache_status:
            raise ValueError("TB2 backend cache status contradicts outcome")
        usage["cache_token_status"] = observed_cache_status
        if not isinstance(usage.get("cache_status", "unknown"), str):
            raise ValueError("TB2 backend usage cache_status is invalid")
        usage.update(
            {
                "role": role,
                "provider": usage.get("provider", "unknown"),
                "model": usage.get("model", "unknown"),
                "token_source": token_source,
            }
        )
        provenance["usage"] = usage
        return provenance

    @staticmethod
    def _validate_terminal_record(
        task: TB2CampaignTask,
        outcome: TaskEvaluationOutcome,
    ) -> None:
        """Apply the shared JSONL cap to the eventual normalized terminal row."""

        benchmark_evaluation_id = "benchmark-eval-" + "0" * 32
        _encode_jsonl_record(
            {
                "schema": EVALUATION_SCHEMA,
                "lifecycle": "evaluation_terminal",
                "evaluation_kind": "task",
                "run_id": "20000101T000000Z-" + "0" * 32,
                "candidate_evaluation_id": "candidate-eval-" + "0" * 32,
                "benchmark_evaluation_id": benchmark_evaluation_id,
                "aggregate_evaluation_id": benchmark_evaluation_id,
                "parent_evaluation_id": benchmark_evaluation_id,
                "benchmark_id": "tb2",
                "evaluation_id": "task-eval-" + "0" * 32,
                "candidate_id": "cand-" + "0" * 32,
                "task_id": task.sample_slot.task_id,
                "sample_index": task.sample_slot.sample_index,
                "operation": "attempt-evaluation",
                "candidate_snapshot_digest": "sha256:" + "0" * 64,
                "evaluator_digest": "0" * 64,
                "outcome": outcome.outcome,
                "score": outcome.score,
                "started_at": 1.7976931348623157e308,
                "ended_at": 1.7976931348623157e308,
                "wall_seconds": outcome.wall_seconds,
                "summary": outcome.summary,
                "artifact_refs": dict(outcome.artifact_refs),
                "provenance": dict(outcome.provenance),
            }
        )

    @staticmethod
    def _slot_from_task(task: TaskRef) -> SampleSlot:
        raw = task.meta.get(_TASK_CONTEXT_META_KEY)
        if not isinstance(raw, Mapping):
            raise RunValidationError("TB2 task lacks controller sample-slot context")
        try:
            slot = SampleSlot.from_mapping(raw)
        except (TypeError, ValueError) as exc:
            raise RunValidationError(
                "TB2 task has invalid sample-slot context"
            ) from exc
        if slot.task_id != task.task_id:
            raise RunValidationError("TB2 task/sample-slot identity mismatch")
        return slot

    def _execution_task(
        self,
        slot: SampleSlot,
        task: TaskRef,
        material: TB21PinnedTaskMaterial,
    ) -> TB2CampaignTask:
        prefix = f"git+tb21://{self._snapshot.commit}/tasks/{slot.task_id}"
        return TB2CampaignTask(
            sample_slot=slot,
            task_ref=task,
            snapshot_digest=self._snapshot.snapshot_digest,
            source_commit=self._snapshot.commit,
            repository_tree=self._snapshot.repository_tree,
            tasks_tree=self._snapshot.tasks_tree,
            instruction=material.instruction,
            task_manifest=material.task_manifest,
            instruction_ref=prefix + "/instruction.md",
            task_manifest_ref=prefix + "/task.toml",
        )

    @staticmethod
    def _shared_outcome(
        task: TB2CampaignTask,
        outcome: TB2TaskOutcome,
    ) -> TaskEvaluationOutcome:
        provenance = dict(outcome.provenance)
        if "tb2_task_identity" in provenance:
            raise RunValidationError(
                "TB2 backend provenance uses a reserved identity field"
            )
        provenance["tb2_task_identity"] = {
            "schema": "tb21-campaign-task-identity",
            "task_id": task.sample_slot.task_id,
            "sample_index": task.sample_slot.sample_index,
            "snapshot_digest": task.snapshot_digest,
            "source_commit": task.source_commit,
            "repository_tree": task.repository_tree,
            "tasks_tree": task.tasks_tree,
            "instruction_ref": task.instruction_ref,
            "instruction_digest": _sha256(task.instruction),
            "task_manifest_ref": task.task_manifest_ref,
            "task_manifest_digest": _sha256(task.task_manifest),
        }
        common = {
            "wall_seconds": outcome.wall_seconds,
            "summary": outcome.error_type,
            "artifact_refs": MappingProxyType(dict(outcome.artifact_refs)),
            "provenance": MappingProxyType(provenance),
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "cache_tokens": outcome.cache_tokens,
            "token_status": outcome.token_status,
            "input_token_status": outcome.input_token_status,
            "output_token_status": outcome.output_token_status,
            "cache_token_status": outcome.cache_token_status,
            "cost_usd": outcome.cost_usd,
            "cost_status": outcome.cost_status,
        }
        if outcome.verdict == "passed":
            return TaskEvaluationOutcome(outcome="measured", score=1.0, **common)
        if outcome.verdict == "failed":
            return TaskEvaluationOutcome(
                outcome="measured",
                score=0.0,
                **common,
            )
        return TaskEvaluationOutcome(
            outcome="inconclusive",
            **common,
        )


__all__ = [
    "TB2CampaignBenchmarkAdapter",
    "TB2CampaignExecutionBackend",
    "TB2CampaignTask",
    "TB2TaskOutcome",
    "bind_held_out_task_slots",
]
