"""Trusted, run-private artifact layout shared by baseline adapters.

This module is deliberately a small filesystem data plane.  It provides no
broker, database, daemon, signature protocol, evaluator, or search policy.
Native baseline loops keep ownership of generation, selection, feedback, and
archive semantics; the trusted caller uses :class:`RunRecorder` immediately
outside its evaluator boundary.

The evaluation-side layout never has a held-out capability.  Sealed held-out
material belongs to the separately authorized trusted evaluator.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Sequence

from rsi_arena.contracts import canonical_digest
from rsi_arena.manifests import (
    CampaignAggregateSpec,
    DatasetBoundary,
    ManifestValidationError,
    SampleSlot,
)

LAYOUT_SCHEMA = "rsi-arena-run-layout"
RUN_MANIFEST_SCHEMA = "rsi-arena-run-manifest"
CANDIDATE_SCHEMA = "rsi-arena-candidate"
ATTEMPT_SCHEMA = "rsi-arena-attempt"
EVALUATION_SCHEMA = "rsi-arena-attempt-evaluation"
BENCHMARK_EVALUATION_RESULT_SCHEMA = "rsi-arena-benchmark-result"
CAMPAIGN_EVALUATION_RESULT_SCHEMA = "rsi-arena-campaign-result"
RUN_RESULT_SCHEMA = "rsi-arena-run-result"
EVENT_SCHEMA = "rsi-arena-event"

JSONL_NAMES = (
    "attempts.jsonl",
    "candidates.jsonl",
    "evaluations.jsonl",
    "events.jsonl",
    "usage.jsonl",
)
MANIFEST_NAMES = (
    "run-manifest.json",
    "development-delivery-manifest.json",
)
EVALUATION_OPERATIONS = frozenset({"attempt-evaluation"})
TASK_OUTCOMES = frozenset({"measured", "inconclusive"})
ATTEMPT_KINDS = frozenset({"single", "paired"})
CANDIDATE_ROLES = frozenset({"candidate", "control", "challenger"})
GATE_DECISIONS = frozenset({"enter", "skip"})
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MAX_JSONL_RECORD_BYTES = 10 * 1024 * 1024
_HELD_OUT_EVENT_TYPES = frozenset(
    {
        "held_out_evaluation_started",
        "held_out_evaluation_terminal",
        "held_out_delivery_receipt",
        "held_out_unsealed",
    }
)
_HELD_OUT_ARTIFACT_PREFIXES = (
    "held-out-evaluation",
    "held-out-result",
    "held-out-trajectory",
    "sealed-held-out",
    "sealed_held_out",
)


class RunLayoutError(RuntimeError):
    """The trusted run layout cannot be created or safely used."""


class RunValidationError(RunLayoutError):
    """A run artifact violates the fail-closed schema or binding rules."""


class TruncatedJsonlError(RunValidationError):
    """A JSONL ledger lacks a complete, newline-terminated final record."""


class ConcurrentWriterError(RunLayoutError):
    """Another writer already owns an append-only ledger."""


class DuplicateCandidateEvaluation(RunLayoutError):
    """The gateway-canonical candidate already owns an evaluation lifecycle."""


class AttemptLimit(RunLayoutError):
    """The durable run lifecycle has exhausted its attempt cap."""


def new_run_id() -> str:
    """Return a time-sortable run identity with UUID entropy."""

    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return f"{stamp}Z-{uuid.uuid4().hex}"


def _segment(value: object, field: str) -> str:
    text = str(value)
    if text in {".", ".."} or _SEGMENT.fullmatch(text) is None:
        raise ValueError(f"invalid {field} path segment: {text!r}")
    return text


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Publish one trusted file with file and parent-directory durability."""

    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, mode)
    try:
        _write_all(descriptor, payload)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    else:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(payload, indent=2, sort_keys=True, default=str).encode()
    atomic_write_bytes(path, encoded + b"\n")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


class AppendOnlyJsonl:
    """One fsync'd JSON object per append, guarded by an exclusive writer lock.

    Each row is encoded before one ``O_APPEND`` write.  Readers reject a
    non-newline tail instead of silently dropping it.  We never auto-truncate:
    a damaged ledger remains audit evidence and the run fails closed.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        self._descriptor = os.open(self.path, flags, 0o600)
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._descriptor)
            raise ConcurrentWriterError(
                f"JSONL ledger already has a writer: {self.path}"
            ) from exc
        self._lock = threading.Lock()
        self._closed = False
        self._poisoned = False

    def append(self, payload: Mapping[str, object]) -> None:
        self.append_many((payload,))

    def append_many(self, payloads: Iterable[Mapping[str, object]]) -> None:
        """Append one lifecycle batch with one write and one fsync boundary."""

        records: list[bytes] = []
        for payload in payloads:
            records.append(_encode_jsonl_record(payload))
        if not records:
            return
        batch = b"".join(records)
        with self._lock:
            if self._closed:
                raise RunLayoutError("JSONL writer is closed")
            if self._poisoned:
                raise RunLayoutError("JSONL writer is poisoned by an earlier failure")
            try:
                written = os.write(self._descriptor, batch)
                if written != len(batch):
                    self._poisoned = True
                    raise OSError("partial JSONL append; ledger must fail closed")
                os.fsync(self._descriptor)
            except BaseException:
                self._poisoned = True
                raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            os.close(self._descriptor)
            self._closed = True

    def __enter__(self) -> "AppendOnlyJsonl":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _encode_jsonl_record(payload: Mapping[str, object]) -> bytes:
    """Encode one row under the single shared persisted-record boundary."""

    if not isinstance(payload, Mapping):
        raise TypeError("JSONL payload must be an object")
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str) + "\n"
    ).encode("utf-8")
    if len(encoded) > _MAX_JSONL_RECORD_BYTES:
        raise ValueError("JSONL record exceeds the 10 MiB boundary")
    return encoded


def read_jsonl(path: Path) -> list[dict]:
    """Read a strict ledger; a truncated tail is never treated as absent."""

    path = Path(path)
    if not path.exists():
        return []
    raw = path.read_bytes()
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise TruncatedJsonlError(f"truncated JSONL tail: {path}")
    rows: list[dict] = []
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        if not raw_line:
            raise RunValidationError(f"blank JSONL row at {path}:{line_number}")
        try:
            row = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunValidationError(
                f"invalid JSONL row at {path}:{line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise RunValidationError(
                f"JSONL row is not an object at {path}:{line_number}"
            )
        rows.append(row)
    return rows


def _load_json_object(path: Path, *, label: str) -> dict[str, object]:
    """Read one authoritative JSON object and reject partial/corrupt records."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunValidationError(f"{label} is unreadable") from exc
    if not isinstance(payload, dict):
        raise RunValidationError(f"{label} is not a JSON object")
    return payload


def _load_run_manifest(root: Path) -> dict[str, object]:
    """Read the run authority needed by focused recovery/read paths."""

    manifest = _load_json_object(Path(root) / "run-manifest.json", label="run manifest")
    _validate_evaluation_boundary(manifest)
    if (
        manifest.get("schema") != RUN_MANIFEST_SCHEMA
        or manifest.get("layout_schema") != LAYOUT_SCHEMA
    ):
        raise RunValidationError("unknown run/layout manifest schema")
    if (
        manifest.get("held_out_capability") is not False
        or manifest.get("sealed_held_out_reference") is not None
    ):
        raise RunValidationError(
            "evaluation run must not carry held-out capability/material"
        )
    if not isinstance(manifest.get("evaluator_digest"), str) or not manifest.get(
        "evaluator_digest"
    ):
        raise RunValidationError("run evaluator digest is missing")
    _campaign_run_freeze_from_manifest(manifest)
    return manifest


def _campaign_run_freeze_from_manifest(
    manifest: Mapping[str, object],
) -> dict[str, object]:
    """Validate and return the run's sole evaluation/resume freeze authority."""

    raw = manifest.get("campaign_run_freeze")
    if not isinstance(raw, Mapping):
        raise RunValidationError("campaign run freeze is missing")
    required = {
        "schema",
        "campaign_id",
        "campaign_freeze_id",
        "campaign_cell_id",
        "method",
        "run_kind",
        "evaluation_seed",
        "benchmark_ids",
        "benchmarks",
        "resume_identity",
    }
    optional = {"aggregate_policy"}
    if set(raw) - required - optional or required - set(raw):
        raise RunValidationError("campaign run freeze fields are invalid")
    benchmark_ids = raw.get("benchmark_ids")
    benchmarks = raw.get("benchmarks")
    if (
        raw.get("schema") != "rsi-arena-campaign-run-freeze"
        or not isinstance(benchmark_ids, list)
        or not benchmark_ids
        or any(not isinstance(item, str) or not item for item in benchmark_ids)
        or len(set(benchmark_ids)) != len(benchmark_ids)
        or not isinstance(benchmarks, Mapping)
        or set(benchmarks) != set(benchmark_ids)
    ):
        raise RunValidationError("campaign run freeze benchmark order is invalid")
    for benchmark_id in benchmark_ids:
        binding = benchmarks.get(benchmark_id)
        if not isinstance(binding, Mapping) or set(binding) != {"dataset_boundary"}:
            raise RunValidationError("campaign run freeze boundary is invalid")
        try:
            DatasetBoundary.from_mapping(binding["dataset_boundary"])
        except (KeyError, ManifestValidationError) as exc:
            raise RunValidationError("campaign run freeze boundary is invalid") from exc
    aggregate = raw.get("aggregate_policy")
    if "aggregate_policy" in raw:
        if not isinstance(aggregate, Mapping):
            raise RunValidationError("campaign aggregate policy is invalid")
        try:
            CampaignAggregateSpec.from_mapping(
                aggregate, field="campaign_run_freeze.aggregate_policy"
            )
        except ManifestValidationError as exc:
            raise RunValidationError(str(exc)) from exc
    identity = dict(raw)
    resume_identity = identity.pop("resume_identity")
    if resume_identity != "sha256:" + canonical_digest(identity):
        raise RunValidationError("campaign run freeze resume identity changed")
    if (
        manifest.get("runtime_campaign_schema") != "rsi-arena-campaign-run-freeze"
        or raw.get("campaign_id") != manifest.get("campaign_id")
        or raw.get("method") != manifest.get("method")
        or raw.get("run_kind") != manifest.get("run_kind")
        or str(raw.get("evaluation_seed")) != str(manifest.get("evaluation_seed"))
        or raw.get("campaign_cell_id") != manifest.get("benchmark")
    ):
        raise RunValidationError("campaign run freeze identity is invalid")
    return dict(raw)


def _assert_evaluation_artifact_name(name: str) -> None:
    lowered = str(name).lower()
    if lowered.startswith(_HELD_OUT_ARTIFACT_PREFIXES):
        raise RunValidationError(
            "evaluation run cannot publish a held-out artifact path"
        )


@dataclass(frozen=True)
class RunLayout:
    root: Path
    runtime_root: Path
    campaign: str
    method: str
    benchmark: str
    evaluation_seed: str
    run_id: str

    @property
    def method_root(self) -> Path:
        return self.runtime_root / "method"

    @property
    def trusted_runtime_root(self) -> Path:
        return self.runtime_root / "trusted"

    @classmethod
    def create(
        cls,
        runs_root: Path,
        *,
        campaign: str,
        method: str,
        benchmark: str,
        evaluation_seed: object,
        evaluator_digest: Optional[str],
        run_kind: str,
        runtime_base: Optional[Path] = None,
        run_manifest: Optional[Mapping[str, object]] = None,
        development_delivery_manifest: Optional[Mapping[str, object]] = None,
    ) -> "RunLayout":
        run_id = new_run_id()
        identity = {
            "campaign": _segment(campaign, "campaign"),
            "method": _segment(method, "method"),
            "benchmark": _segment(benchmark, "benchmark"),
            "evaluation_seed": _segment(evaluation_seed, "evaluation_seed"),
            "run_id": _segment(run_id, "run_id"),
        }
        runs_root = Path(runs_root).expanduser().absolute()
        runtime_base = (
            Path(
                runtime_base
                if runtime_base is not None
                else runs_root.parent / f".{runs_root.name}-runtime"
            )
            .expanduser()
            .absolute()
        )
        parts = tuple(identity.values())
        root = runs_root.joinpath(*parts)
        runtime_root = runtime_base.joinpath(*parts)
        if (
            root == runtime_root
            or root in runtime_root.parents
            or runtime_root in root.parents
        ):
            raise ValueError("official artifacts and mutable runtime must be disjoint")
        root.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        runtime_root.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        root.mkdir(mode=0o700)
        try:
            runtime_root.mkdir(mode=0o700)
        except BaseException:
            root.rmdir()
            raise
        layout = cls(root=root, runtime_root=runtime_root, **identity)
        for directory in (
            root / "candidates" / "git",
            root / "trajectory" / "raw",
            root / "snapshots",
            root / "results",
            layout.method_root / "workspace",
            layout.method_root / "home",
            layout.method_root / "tmp",
            layout.method_root / "cache",
            layout.method_root / "state",
            layout.method_root / "git",
            layout.trusted_runtime_root,
        ):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            directory.chmod(0o700)
        for name in JSONL_NAMES:
            descriptor = os.open(
                root / name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fsync(descriptor)
            os.close(descriptor)
        base = {
            "schema": RUN_MANIFEST_SCHEMA,
            "layout_schema": LAYOUT_SCHEMA,
            "run_id": layout.run_id,
            "campaign_id": layout.campaign,
            "method": layout.method,
            "benchmark": layout.benchmark,
            "evaluation_seed": layout.evaluation_seed,
            "run_kind": str(run_kind),
            "evaluator_digest": (
                str(evaluator_digest) if evaluator_digest is not None else None
            ),
            "evaluation_artifacts_only": True,
            "held_out_capability": False,
            "sealed_held_out_reference": None,
            "gradient_training_enabled": False,
            "model_weight_updates": False,
            "training_stack_identity": None,
            "created_at": time.time(),
        }
        base.update(dict(run_manifest or {}))
        # Identity and the held-out/no-training boundary are not caller-overridable.
        base.update(
            {
                "schema": RUN_MANIFEST_SCHEMA,
                "layout_schema": LAYOUT_SCHEMA,
                "run_id": layout.run_id,
                "campaign_id": layout.campaign,
                "method": layout.method,
                "benchmark": layout.benchmark,
                "evaluation_seed": layout.evaluation_seed,
                "run_kind": str(run_kind),
                "evaluator_digest": (
                    str(evaluator_digest) if evaluator_digest is not None else None
                ),
                "evaluation_artifacts_only": True,
                "held_out_capability": False,
                "sealed_held_out_reference": None,
                "gradient_training_enabled": False,
                "model_weight_updates": False,
                "training_stack_identity": None,
            }
        )
        _campaign_run_freeze_from_manifest(base)
        layout.write_manifest("run-manifest.json", base)
        development_payload = dict(
            development_delivery_manifest or _empty_development_manifest(layout)
        )
        development_payload.update({"run_id": layout.run_id, "method": layout.method})
        layout.write_manifest("development-delivery-manifest.json", development_payload)
        _fsync_directory(root)
        _fsync_directory(runtime_root)
        return layout

    @classmethod
    def open_existing(cls, root: Path) -> "RunLayout":
        """Open one trusted run directory without touching mutable runtime."""

        supplied = Path(root).expanduser().absolute()
        try:
            info = supplied.lstat()
        except FileNotFoundError as exc:
            raise RunValidationError("recovery run leaf does not exist") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RunValidationError("recovery target is not a real run directory")
        resolved = supplied.resolve(strict=True)
        manifest = _load_run_manifest(resolved)
        identity = {
            "campaign": _segment(manifest.get("campaign_id"), "campaign"),
            "method": _segment(manifest.get("method"), "method"),
            "benchmark": _segment(manifest.get("benchmark"), "benchmark"),
            "evaluation_seed": _segment(
                manifest.get("evaluation_seed"), "evaluation_seed"
            ),
            "run_id": _segment(manifest.get("run_id"), "run_id"),
        }
        return cls(
            root=resolved,
            runtime_root=resolved.parent / ".recovery-unused-runtime",
            **identity,
        )

    def bind_evaluator(self, evaluator_digest: str) -> None:
        """Bind evaluator identity once after trusted adapter construction."""

        digest = str(evaluator_digest)
        if not digest:
            raise ValueError("evaluator_digest is required")
        path = self.root / "run-manifest.json"
        manifest = json.loads(path.read_text())
        existing = manifest.get("evaluator_digest")
        if existing not in {None, digest}:
            raise RunValidationError("run evaluator is already bound differently")
        if existing is None:
            manifest["evaluator_digest"] = digest
            self.write_manifest("run-manifest.json", manifest)

    def write_manifest(self, name: str, payload: Mapping[str, object]) -> None:
        if name not in MANIFEST_NAMES:
            raise ValueError(f"unsupported run manifest: {name}")
        _validate_evaluation_boundary(payload)
        atomic_write_json(self.root / name, payload)

    def write_result(self, name: str, payload: Mapping[str, object]) -> Path:
        filename = _segment(name, "result name")
        _assert_evaluation_artifact_name(filename)
        if not filename.endswith(".json"):
            filename += ".json"
        _validate_evaluation_boundary(payload)
        path = self.root / "results" / filename
        atomic_write_json(path, payload)
        return path

    def resolve_store_ref(self, store_ref: str) -> Path:
        relative = Path(store_ref)
        if relative.is_absolute() or ".." in relative.parts:
            raise RunValidationError("store_ref must be run-relative")
        candidate = (self.root / relative).resolve(strict=True)
        root = self.root.resolve(strict=True)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise RunValidationError("store_ref escapes the trusted run") from exc
        return candidate

    def assert_workspace_isolated(self, workspace: Path) -> Path:
        workspace = Path(workspace).expanduser().resolve(strict=True)
        official = self.root.resolve(strict=True)
        if (
            workspace == official
            or official in workspace.parents
            or workspace in official.parents
        ):
            raise RunLayoutError("candidate workspace overlaps official artifacts")
        return workspace


def _empty_development_manifest(layout: RunLayout) -> dict:
    return {
        "schema": "campaign-development-delivery",
        "run_id": layout.run_id,
        "method": layout.method,
        "status": "not_recorded",
        "benchmarks": {
            layout.benchmark: _empty_development_benchmark_manifest(layout.benchmark)
        },
    }


def _empty_development_benchmark_manifest(
    benchmark_id: str,
    development_task_ids: Sequence[str] = (),
) -> dict[str, object]:
    return {
        "benchmark_id": str(benchmark_id),
        "overall_status": "not_recorded",
        "supported_fields": [],
        "delivered_fields": [],
        "delivery_channels": {},
        "rendered_artifact_digests": [],
        "status": {},
        "delivery_records": [],
        "development_mirror": None,
        "development_task_ids": list(map(str, development_task_ids)),
    }


def _development_task_ids_from_boundary(value: object) -> tuple[str, ...]:
    try:
        boundary = DatasetBoundary.from_mapping(value)
    except ManifestValidationError as exc:
        raise RunValidationError(str(exc)) from exc
    return boundary.development_set.ordered_task_ids


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: str
    native_candidate_id: Optional[str]
    artifact_kind: str
    snapshot_digest: str
    artifact_digest: str
    store_ref: str
    iteration: int
    attempt_id: str
    role: str
    parent_candidate_id: Optional[str] = None
    tree_sha: Optional[str] = None
    pinned_ref: Optional[str] = None
    gateway_commit_sha: Optional[str] = None
    gateway_root_commit_sha: Optional[str] = None
    artifact_commit_sha: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.attempt_id:
            raise ValueError("candidate attempt binding is empty")
        if self.role not in CANDIDATE_ROLES:
            raise ValueError("candidate role is invalid")
        if self.artifact_kind != "git_bundle":
            raise ValueError("candidate artifact kind must be git_bundle")
        if self.parent_candidate_id is not None and not self.parent_candidate_id:
            raise ValueError("candidate parent binding is empty")


@dataclass(frozen=True)
class AttemptRecord:
    """Recorder-owned snapshot of one run-scoped attempt lifecycle."""

    attempt_id: str
    attempt_index: int
    state: str
    kind: str
    gate_decision: str | None
    benchmark_slots: tuple[tuple[str, tuple[SampleSlot, ...]], ...]
    candidate_ids: tuple[str, ...]
    started_at: float
    sealed_at: Optional[float] = None
    terminal_at: Optional[float] = None

    def __post_init__(self) -> None:
        if self.kind not in ATTEMPT_KINDS:
            raise ValueError("attempt kind is invalid")
        if self.gate_decision not in GATE_DECISIONS | {None}:
            raise ValueError("attempt gate decision is invalid")

    def slots_for(self, benchmark_id: str) -> tuple[SampleSlot, ...]:
        matches = [
            slots for name, slots in self.benchmark_slots if name == benchmark_id
        ]
        if len(matches) != 1:
            raise RunValidationError(
                f"attempt has no unique benchmark slot binding for {benchmark_id!r}"
            )
        return matches[0]


@dataclass(frozen=True)
class EvaluationHandle:
    candidate: CandidateRecord
    evaluator_digest: str
    operation: str
    started_at: float
    attempt_id: str
    candidate_evaluation_id: str
    benchmark_evaluation_ids: tuple[tuple[str, str], ...]
    scoped_task_evaluation_ids: tuple[tuple[str, SampleSlot, str], ...]

    def __post_init__(self) -> None:
        if (
            not self.attempt_id
            or not self.candidate_evaluation_id
            or self.candidate.attempt_id != self.attempt_id
        ):
            raise ValueError("evaluation handle attempt binding is invalid")

    def benchmark_evaluation_id(self, benchmark_id: str) -> str:
        matches = [
            evaluation_id
            for child_benchmark, evaluation_id in self.benchmark_evaluation_ids
            if child_benchmark == benchmark_id
        ]
        if len(matches) != 1:
            raise RunValidationError(
                f"candidate evaluation has no unique benchmark {benchmark_id!r}"
            )
        return matches[0]

    def task_evaluation_id(self, benchmark_id: str, sample_index: int) -> str:
        matches = [
            evaluation_id
            for child_benchmark, slot, evaluation_id in self.scoped_task_evaluation_ids
            if child_benchmark == benchmark_id and slot.sample_index == sample_index
        ]
        if len(matches) != 1:
            raise RunValidationError(
                "candidate evaluation has no unique benchmark/task child"
            )
        return matches[0]


@dataclass(frozen=True)
class TaskEvaluationOutcome:
    outcome: str
    score: Optional[float] = None
    wall_seconds: Optional[float] = None
    summary: Optional[str] = None
    artifact_refs: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    provenance: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_tokens: Optional[int] = None
    token_status: str = "missing"
    input_token_status: str = "auto"
    output_token_status: str = "auto"
    cache_token_status: str = "missing"
    cost_usd: Optional[float] = None
    cost_status: str = "missing"
    constraints_pass: Optional[bool] = None

    def __post_init__(self) -> None:
        if self.outcome not in TASK_OUTCOMES:
            raise ValueError(f"unknown task outcome: {self.outcome!r}")
        if self.outcome == "measured" and (
            not isinstance(self.score, (int, float))
            or isinstance(self.score, bool)
            or not math.isfinite(float(self.score))
        ):
            raise ValueError("measured task outcome requires a finite score")
        if self.outcome == "inconclusive" and self.score is not None:
            raise ValueError("inconclusive task outcome cannot carry a score")
        if self.summary is not None and not isinstance(self.summary, str):
            raise ValueError("task outcome summary must be a string or null")
        if self.constraints_pass is not None and not isinstance(
            self.constraints_pass, bool
        ):
            raise ValueError("task outcome constraints_pass must be boolean or null")
        for field_name in ("input_tokens", "output_tokens", "cache_tokens"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"invalid task usage field {field_name}: {value!r}")
        for status_field, token_field in (
            ("input_token_status", "input_tokens"),
            ("output_token_status", "output_tokens"),
        ):
            status = getattr(self, status_field)
            value = getattr(self, token_field)
            if status == "auto":
                status = self.token_status if value is not None else "missing"
                object.__setattr__(self, status_field, status)
            if status not in {"reported", "recounted", "missing", "unverified"}:
                raise ValueError(
                    f"invalid task usage status {status_field}: {status!r}"
                )
            if status in {"reported", "recounted"} and value is None:
                raise ValueError(f"{status_field} requires an observed token count")


@dataclass(frozen=True)
class BenchmarkEvaluationResult:
    run_id: str
    evaluator_digest: str
    benchmark_id: str
    candidate_evaluation_id: str
    benchmark_evaluation_id: str
    candidate_id: str
    candidate_snapshot_digest: str
    sample_slots: tuple[SampleSlot, ...]
    task_outcomes: tuple[Mapping[str, object], ...]
    outcome: str
    n_expected: int
    n_conclusive: int
    n_inconclusive: int
    score: Optional[float]
    coverage: float
    result_ref: str
    attempt_id: str

    def __post_init__(self) -> None:
        if not self.attempt_id:
            raise ValueError("benchmark result attempt binding is empty")
        if self.outcome not in TASK_OUTCOMES:
            raise ValueError("benchmark result outcome is invalid")
        if (
            isinstance(self.n_expected, bool)
            or isinstance(self.n_conclusive, bool)
            or isinstance(self.n_inconclusive, bool)
            or not isinstance(self.n_expected, int)
            or not isinstance(self.n_conclusive, int)
            or not isinstance(self.n_inconclusive, int)
            or self.n_expected < 1
            or self.n_conclusive < 0
            or self.n_inconclusive < 0
            or self.n_conclusive + self.n_inconclusive != self.n_expected
            or len(self.sample_slots) != self.n_expected
            or len(self.task_outcomes) != self.n_expected
        ):
            raise ValueError("benchmark result counts are invalid")
        expected_coverage = self.n_conclusive / self.n_expected
        if (
            isinstance(self.coverage, bool)
            or not isinstance(self.coverage, (int, float))
            or not math.isclose(
                float(self.coverage), expected_coverage, rel_tol=0.0, abs_tol=1e-12
            )
        ):
            raise ValueError("benchmark result coverage is invalid")
        if self.n_conclusive:
            if self.outcome != "measured" or (
                not isinstance(self.score, (int, float))
                or isinstance(self.score, bool)
                or not math.isfinite(float(self.score))
            ):
                raise ValueError("conclusive benchmark result requires a score")
        elif self.outcome != "inconclusive" or self.score is not None:
            raise ValueError("zero-conclusive benchmark result must be inconclusive")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": BENCHMARK_EVALUATION_RESULT_SCHEMA,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "evaluator_digest": self.evaluator_digest,
            "benchmark_id": self.benchmark_id,
            "candidate_evaluation_id": self.candidate_evaluation_id,
            "benchmark_evaluation_id": self.benchmark_evaluation_id,
            "candidate_id": self.candidate_id,
            "candidate_snapshot_digest": self.candidate_snapshot_digest,
            "sample_slots": [slot.as_dict() for slot in self.sample_slots],
            "task_outcomes": [dict(outcome) for outcome in self.task_outcomes],
            "outcome": self.outcome,
            "n_expected": self.n_expected,
            "n_conclusive": self.n_conclusive,
            "n_inconclusive": self.n_inconclusive,
            "score": self.score,
            "coverage": self.coverage,
            "result_ref": self.result_ref,
        }


@dataclass(frozen=True)
class CampaignEvaluationResult:
    run_id: str
    evaluator_digest: str
    candidate_evaluation_id: str
    candidate_id: str
    candidate_role: str
    candidate_snapshot_digest: str
    benchmark_results: tuple[BenchmarkEvaluationResult, ...]
    outcome: str
    score_vector: tuple[tuple[str, Optional[float]], ...]
    aggregate_policy: Optional[Mapping[str, str]]
    aggregate_score: Optional[float]
    n_expected: int
    n_conclusive: int
    n_inconclusive: int
    coverage: float
    result_ref: str
    attempt_id: str

    def __post_init__(self) -> None:
        if self.outcome not in TASK_OUTCOMES or not self.benchmark_results:
            raise ValueError("campaign result outcome/vector is invalid")
        if self.candidate_role not in CANDIDATE_ROLES:
            raise ValueError("campaign result candidate role is invalid")
        expected_vector = tuple(
            (child.benchmark_id, child.score) for child in self.benchmark_results
        )
        if not self.attempt_id or any(
            child.attempt_id != self.attempt_id for child in self.benchmark_results
        ):
            raise ValueError("campaign result attempt binding is invalid")
        if self.score_vector != expected_vector:
            raise ValueError("campaign result score vector is invalid")
        expected_counts = (
            sum(child.n_expected for child in self.benchmark_results),
            sum(child.n_conclusive for child in self.benchmark_results),
            sum(child.n_inconclusive for child in self.benchmark_results),
        )
        if (
            self.n_expected,
            self.n_conclusive,
            self.n_inconclusive,
        ) != expected_counts or self.n_expected < 1:
            raise ValueError("campaign result counts are invalid")
        expected_coverage = self.n_conclusive / self.n_expected
        if (
            isinstance(self.coverage, bool)
            or not isinstance(self.coverage, (int, float))
            or not math.isclose(
                float(self.coverage), expected_coverage, rel_tol=0.0, abs_tol=1e-12
            )
        ):
            raise ValueError("campaign result coverage is invalid")
        child_scores = [
            float(child.score)
            for child in self.benchmark_results
            if child.score is not None
        ]
        if self.outcome != ("measured" if child_scores else "inconclusive"):
            raise ValueError("campaign result outcome is invalid")
        if self.aggregate_policy is None:
            if self.aggregate_score is not None:
                raise ValueError(
                    "campaign result cannot carry a scalar without aggregate policy"
                )
        else:
            try:
                policy = CampaignAggregateSpec.from_mapping(
                    self.aggregate_policy, field="campaign result aggregate_policy"
                )
            except ManifestValidationError as exc:
                raise ValueError(str(exc)) from exc
            expected_score = (
                sum(child_scores) / len(child_scores) if child_scores else None
            )
            if (
                policy.kind != "macro_mean_pass_rate"
                or (expected_score is None and self.aggregate_score is not None)
                or (
                    expected_score is not None
                    and (
                        not isinstance(self.aggregate_score, (int, float))
                        or isinstance(self.aggregate_score, bool)
                        or not math.isclose(
                            float(self.aggregate_score),
                            expected_score,
                            rel_tol=0.0,
                            abs_tol=1e-12,
                        )
                    )
                )
            ):
                raise ValueError("campaign aggregate score is invalid")

    def as_dict(self) -> dict[str, object]:
        by_benchmark = {
            result.benchmark_id: result for result in self.benchmark_results
        }
        payload: dict[str, object] = {
            "schema": CAMPAIGN_EVALUATION_RESULT_SCHEMA,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "evaluator_digest": self.evaluator_digest,
            "candidate_evaluation_id": self.candidate_evaluation_id,
            "candidate_id": self.candidate_id,
            "candidate_role": self.candidate_role,
            "candidate_snapshot_digest": self.candidate_snapshot_digest,
            "benchmark_results": [
                result.as_dict() for result in self.benchmark_results
            ],
            "outcome": self.outcome,
            "score_vector": [
                {
                    "benchmark_id": benchmark_id,
                    "score": score,
                    "n_expected": by_benchmark[benchmark_id].n_expected,
                    "n_conclusive": by_benchmark[benchmark_id].n_conclusive,
                    "n_inconclusive": by_benchmark[benchmark_id].n_inconclusive,
                    "coverage": by_benchmark[benchmark_id].coverage,
                }
                for benchmark_id, score in self.score_vector
            ],
            "aggregate_policy": (
                dict(self.aggregate_policy)
                if self.aggregate_policy is not None
                else None
            ),
            "aggregate_score": self.aggregate_score,
            "n_expected": self.n_expected,
            "n_conclusive": self.n_conclusive,
            "n_inconclusive": self.n_inconclusive,
            "coverage": self.coverage,
            "result_ref": self.result_ref,
        }
        return payload


@dataclass(frozen=True)
class CampaignOccurrenceState:
    """Read-only state for one caller-known durable campaign occurrence."""

    occurrence_key: str
    candidate_id: str
    candidate_evaluation_id: Optional[str]
    terminal_result: Optional[CampaignEvaluationResult]
    attempt_id: str
    role: str

    def __post_init__(self) -> None:
        if not self.attempt_id or (
            self.terminal_result is not None
            and self.terminal_result.attempt_id != self.attempt_id
        ):
            raise ValueError("occurrence attempt binding is invalid")
        if self.role not in CANDIDATE_ROLES:
            raise ValueError("occurrence role is invalid")

    @property
    def state(self) -> str:
        if self.terminal_result is None:
            return "IN_PROGRESS"
        return "TERMINAL"


def _benchmark_result_from_payload(
    payload: Mapping[str, object],
) -> BenchmarkEvaluationResult:
    try:
        slots = tuple(SampleSlot.from_mapping(item) for item in payload["sample_slots"])
        outcomes = tuple(
            MappingProxyType(dict(item)) for item in payload["task_outcomes"]
        )
        result = BenchmarkEvaluationResult(
            run_id=str(payload["run_id"]),
            attempt_id=str(payload["attempt_id"]),
            evaluator_digest=str(payload["evaluator_digest"]),
            benchmark_id=str(payload["benchmark_id"]),
            candidate_evaluation_id=str(payload["candidate_evaluation_id"]),
            benchmark_evaluation_id=str(payload["benchmark_evaluation_id"]),
            candidate_id=str(payload["candidate_id"]),
            candidate_snapshot_digest=str(payload["candidate_snapshot_digest"]),
            sample_slots=slots,
            task_outcomes=outcomes,
            outcome=str(payload["outcome"]),
            n_expected=int(payload["n_expected"]),
            n_conclusive=int(payload["n_conclusive"]),
            n_inconclusive=int(payload["n_inconclusive"]),
            score=(
                float(payload["score"]) if payload.get("score") is not None else None
            ),
            coverage=float(payload["coverage"]),
            result_ref=str(payload["result_ref"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RunValidationError("benchmark result is malformed") from exc
    if payload.get(
        "schema"
    ) != BENCHMARK_EVALUATION_RESULT_SCHEMA or result.as_dict() != dict(payload):
        raise RunValidationError("benchmark result is malformed")
    measured_scores: list[float] = []
    for slot, outcome in zip(result.sample_slots, result.task_outcomes, strict=True):
        if (
            outcome.get("sample_index") != slot.sample_index
            or outcome.get("task_id") != slot.task_id
            or outcome.get("replicate") != slot.replicate
            or outcome.get("measurement_seed") != slot.measurement_seed
            or outcome.get("outcome") not in TASK_OUTCOMES
            or outcome.get("constraints_pass") not in {None, True, False}
            or not isinstance(outcome.get("artifact_refs"), Mapping)
            or not isinstance(outcome.get("provenance"), Mapping)
        ):
            raise RunValidationError("benchmark result task outcome is misbound")
        score = outcome.get("score")
        if outcome.get("outcome") == "measured":
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not math.isfinite(float(score))
            ):
                raise RunValidationError("measured task result score is invalid")
            measured_scores.append(float(score))
        elif score is not None:
            raise RunValidationError("inconclusive task result carries a score")
    expected_score = (
        sum(measured_scores) / len(measured_scores) if measured_scores else None
    )
    if (
        result.n_conclusive != len(measured_scores)
        or result.n_inconclusive != result.n_expected - len(measured_scores)
        or result.outcome != ("measured" if measured_scores else "inconclusive")
        or result.score != expected_score
    ):
        raise RunValidationError("benchmark result aggregate is inconsistent")
    return result


def _campaign_result_from_payload(
    payload: Mapping[str, object],
) -> CampaignEvaluationResult:
    try:
        children = tuple(
            _benchmark_result_from_payload(item)
            for item in payload["benchmark_results"]
        )
        vector = tuple(
            (
                str(item["benchmark_id"]),
                float(item["score"]) if item.get("score") is not None else None,
            )
            for item in payload["score_vector"]
        )
        result = CampaignEvaluationResult(
            run_id=str(payload["run_id"]),
            attempt_id=str(payload["attempt_id"]),
            evaluator_digest=str(payload["evaluator_digest"]),
            candidate_evaluation_id=str(payload["candidate_evaluation_id"]),
            candidate_id=str(payload["candidate_id"]),
            candidate_role=str(payload["candidate_role"]),
            candidate_snapshot_digest=str(payload["candidate_snapshot_digest"]),
            benchmark_results=children,
            outcome=str(payload["outcome"]),
            score_vector=vector,
            aggregate_policy=(
                dict(payload["aggregate_policy"])
                if isinstance(payload.get("aggregate_policy"), Mapping)
                else None
            ),
            aggregate_score=(
                float(payload["aggregate_score"])
                if payload.get("aggregate_score") is not None
                else None
            ),
            n_expected=int(payload["n_expected"]),
            n_conclusive=int(payload["n_conclusive"]),
            n_inconclusive=int(payload["n_inconclusive"]),
            coverage=float(payload["coverage"]),
            result_ref=str(payload["result_ref"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RunValidationError("campaign result is malformed") from exc
    if payload.get(
        "schema"
    ) != CAMPAIGN_EVALUATION_RESULT_SCHEMA or result.as_dict() != dict(payload):
        raise RunValidationError("campaign result is malformed")
    if any(
        child.run_id != result.run_id
        or child.attempt_id != result.attempt_id
        or child.evaluator_digest != result.evaluator_digest
        or child.candidate_evaluation_id != result.candidate_evaluation_id
        or child.candidate_id != result.candidate_id
        or child.candidate_snapshot_digest != result.candidate_snapshot_digest
        for child in result.benchmark_results
    ):
        raise RunValidationError("campaign result child binding is invalid")
    return result


def _is_explicit_non_evaluation_usage(row: Mapping[str, object]) -> bool:
    """Recognize only usage shapes outside an attempt-evaluation lifecycle."""

    if row.get("attempt_id") is not None:
        return False
    if row.get("usage_scope") == "development":
        return (
            row.get("candidate_id") is None
            and row.get("benchmark_evaluation_id") is None
            and row.get("replicate") is None
            and row.get("measurement_seed") is None
            and isinstance(row.get("evaluation_id"), str)
            and bool(row.get("evaluation_id"))
            and isinstance(row.get("benchmark_id"), str)
            and bool(row.get("benchmark_id"))
            and isinstance(row.get("task_id"), str)
            and bool(row.get("task_id"))
        )
    if row.get("spend_kind") == "search":
        return all(
            row.get(field) is None
            for field in (
                "evaluation_id",
                "candidate_id",
                "benchmark_id",
                "benchmark_evaluation_id",
                "task_id",
                "replicate",
                "measurement_seed",
            )
        )
    return False


def _validate_explicit_usage_call_binding(row: Mapping[str, object]) -> None:
    """Validate identities intrinsic to one non-evaluation usage call."""

    call_id = str(row["call_id"])
    scope = row.get("usage_scope")
    if scope == "development":
        evaluation_id = str(row["evaluation_id"])
        benchmark_id = str(row["benchmark_id"])
        task_id = str(row["task_id"])
        prefix = f"{evaluation_id}:{benchmark_id}:{task_id}:model-call:"
        ordinal = call_id.removeprefix(prefix)
        if (
            not evaluation_id.startswith("development-evaluation-")
            or not call_id.startswith(prefix)
            or not ordinal.isdigit()
            or int(ordinal) < 1
            or row.get("request_id") != f"{evaluation_id}:{benchmark_id}:{task_id}"
            or row.get("spend_kind") != "evaluation"
        ):
            raise RunValidationError("development usage binding is invalid")
        return
    if scope == "message_leaf":
        if (
            row.get("message_id") != call_id
            or not isinstance(row.get("session_id"), str)
            or not row.get("session_id")
            or not isinstance(row.get("segment_id"), str)
            or not row.get("segment_id")
        ):
            raise RunValidationError("search message usage binding is invalid")
    expected_suffix = {
        "session_cumulative_cost_delta_leaf": ":session-cost-delta",
        "session_cumulative_cost_observation": ":session-cost-observation",
        "segment_observation": ":unobservable",
    }.get(scope)
    if expected_suffix is not None:
        segment_id = row.get("segment_id")
        if (
            not isinstance(row.get("session_id"), str)
            or not row.get("session_id")
            or not isinstance(segment_id, str)
            or not segment_id
            or call_id != f"{segment_id}{expected_suffix}"
        ):
            raise RunValidationError("search segment usage binding is invalid")


class RunRecorder:
    """Trusted-side candidate/evaluation recorder for one evaluation run."""

    def __init__(
        self,
        layout: RunLayout,
        *,
        evaluator_digest: str,
        benchmark_task_ids: Mapping[str, Iterable[str]],
        operation: str,
        _read_only: bool = False,
    ) -> None:
        self._read_only = bool(_read_only)
        self.layout = layout
        self.evaluator_digest = str(evaluator_digest)
        self.benchmark_task_ids = MappingProxyType(
            {
                str(benchmark_id): tuple(str(task_id) for task_id in values)
                for benchmark_id, values in benchmark_task_ids.items()
            }
        )
        self._benchmark_order = tuple(self.benchmark_task_ids)
        self.operation = str(operation)
        if not self.evaluator_digest:
            raise ValueError("recorder requires evaluator identity and tasks")
        if not self.benchmark_task_ids or any(
            not benchmark_id or not values or len(set(values)) != len(values)
            for benchmark_id, values in self.benchmark_task_ids.items()
        ):
            raise ValueError("campaign recorder requires non-empty scoped unique tasks")
        if self.operation not in EVALUATION_OPERATIONS:
            raise ValueError("recorder operation must be attempt-evaluation")
        manifest = _load_run_manifest(layout.root)
        if (
            manifest.get("run_id") != layout.run_id
            or manifest.get("campaign_id") != layout.campaign
            or manifest.get("method") != layout.method
            or manifest.get("benchmark") != layout.benchmark
            or str(manifest.get("evaluation_seed")) != layout.evaluation_seed
        ):
            raise RunValidationError("recorder run identity disagrees with manifest")
        if manifest.get("evaluator_digest") != self.evaluator_digest:
            raise RunValidationError("recorder evaluator digest disagrees with run")
        campaign_run_freeze = _campaign_run_freeze_from_manifest(manifest)
        raw_campaign_aggregate = campaign_run_freeze.get("aggregate_policy")
        if raw_campaign_aggregate is None:
            self._campaign_aggregate: CampaignAggregateSpec | None = None
        elif isinstance(raw_campaign_aggregate, Mapping):
            try:
                self._campaign_aggregate = CampaignAggregateSpec.from_mapping(
                    raw_campaign_aggregate,
                    field="campaign_run_freeze.aggregate_policy",
                )
            except ManifestValidationError as exc:
                raise RunValidationError(str(exc)) from exc
        else:
            raise RunValidationError("campaign aggregate binding is invalid")
        self._benchmark_authorizations: dict[str, tuple[str, ...]] = {}
        raw_bindings = campaign_run_freeze.get("benchmarks")
        if (
            not isinstance(raw_bindings, Mapping)
            or tuple(campaign_run_freeze.get("benchmark_ids", ()))
            != self._benchmark_order
            or set(map(str, raw_bindings)) != set(self._benchmark_order)
        ):
            raise RunValidationError(
                "campaign recorder disagrees with development boundaries"
            )
        for benchmark_id, expected_tasks in self.benchmark_task_ids.items():
            raw_binding = raw_bindings.get(benchmark_id)
            if not isinstance(raw_binding, Mapping):
                raise RunValidationError(
                    "development boundary benchmark binding is invalid"
                )
            development_ids = _development_task_ids_from_boundary(
                raw_binding.get("dataset_boundary")
            )
            if expected_tasks != development_ids:
                raise RunValidationError(
                    "benchmark tasks disagree with development membership"
                )
            self._benchmark_authorizations[benchmark_id] = development_ids
        self._manifest = dict(manifest)
        self._writers = (
            {}
            if self._read_only
            else {
                name: AppendOnlyJsonl(layout.root / name)
                for name in (
                    "attempts.jsonl",
                    "candidates.jsonl",
                    "evaluations.jsonl",
                    "events.jsonl",
                )
            }
        )
        self._lock = threading.RLock()
        self._attempt_started: dict[str, dict[str, object]] = {}
        self._attempt_gate: dict[str, dict[str, object]] = {}
        self._attempt_sealed: dict[str, dict[str, object]] = {}
        self._attempt_terminal: dict[str, dict[str, object]] = {}
        for row in read_jsonl(layout.root / "attempts.jsonl"):
            _validate_evaluation_boundary(row)
            if (
                row.get("schema") != ATTEMPT_SCHEMA
                or row.get("run_id") != layout.run_id
            ):
                self.close()
                raise RunValidationError("attempt schema/run binding mismatch")
            attempt_id = str(row.get("attempt_id", ""))
            lifecycle = str(row.get("lifecycle", ""))
            if not attempt_id:
                self.close()
                raise RunValidationError("attempt ID is empty")
            if lifecycle == "attempt_started":
                started_at = row.get("started_at")
                kind = row.get("kind")
                if (
                    attempt_id in self._attempt_started
                    or row.get("state") != "PENDING"
                    or kind not in ATTEMPT_KINDS
                    or isinstance(started_at, bool)
                    or not isinstance(started_at, (int, float))
                    or not math.isfinite(float(started_at))
                ):
                    self.close()
                    raise RunValidationError("attempt start is invalid")
                slots = self._attempt_slots_from_payload(row.get("benchmark_slots"))
                try:
                    self._validate_caller_selected_attempt_slots(slots)
                except RunValidationError:
                    self.close()
                    raise
                self._attempt_started[attempt_id] = dict(row)
            elif lifecycle == "gate_transition":
                started = self._attempt_started.get(attempt_id)
                transitioned_at = row.get("transitioned_at")
                decision = row.get("decision")
                if (
                    started is None
                    or attempt_id in self._attempt_gate
                    or decision not in GATE_DECISIONS
                    or row.get("state")
                    != ("OPEN" if decision == "enter" else "SKIPPED")
                    or isinstance(transitioned_at, bool)
                    or not isinstance(transitioned_at, (int, float))
                    or not math.isfinite(float(transitioned_at))
                    or row.get("started_at") != started.get("started_at")
                    or float(transitioned_at) < float(started["started_at"])
                ):
                    self.close()
                    raise RunValidationError("attempt gate transition is invalid")
                self._attempt_gate[attempt_id] = dict(row)
            elif lifecycle == "attempt_sealed":
                started = self._attempt_started.get(attempt_id)
                gate = self._attempt_gate.get(attempt_id)
                sealed_at = row.get("sealed_at")
                if (
                    started is None
                    or gate is None
                    or gate.get("decision") != "enter"
                    or row.get("decision") != "enter"
                    or attempt_id in self._attempt_sealed
                    or row.get("state") != "SEALED"
                    or isinstance(sealed_at, bool)
                    or not isinstance(sealed_at, (int, float))
                    or not math.isfinite(float(sealed_at))
                    or row.get("started_at") != started.get("started_at")
                    or float(sealed_at) < float(started["started_at"])
                ):
                    self.close()
                    raise RunValidationError("attempt seal is invalid")
                self._attempt_sealed[attempt_id] = dict(row)
            elif lifecycle == "attempt_terminal":
                sealed = self._attempt_sealed.get(attempt_id)
                gate = self._attempt_gate.get(attempt_id)
                terminal_at = row.get("terminal_at")
                if (
                    gate is None
                    or (sealed is None and gate.get("decision") != "skip")
                    or attempt_id in self._attempt_terminal
                    or row.get("state") != "TERMINAL"
                    or isinstance(terminal_at, bool)
                    or not isinstance(terminal_at, (int, float))
                    or not math.isfinite(float(terminal_at))
                    or row.get("started_at") != gate.get("started_at")
                    or row.get("decision") != gate.get("decision")
                    or row.get("sealed_at")
                    != (sealed.get("sealed_at") if sealed is not None else None)
                    or float(terminal_at)
                    < float(
                        sealed["sealed_at"]
                        if sealed is not None
                        else gate["transitioned_at"]
                    )
                ):
                    self.close()
                    raise RunValidationError("attempt terminal is invalid")
                self._attempt_terminal[attempt_id] = dict(row)
            else:
                self.close()
                raise RunValidationError("unknown attempt lifecycle")
        self._attempt_candidates: dict[str, list[str]] = {
            attempt_id: [] for attempt_id in self._attempt_started
        }
        candidate_rows = read_jsonl(layout.root / "candidates.jsonl")
        self._candidates: dict[str, CandidateRecord] = {}
        self._candidate_by_snapshot: dict[tuple[str, str], CandidateRecord] = {}
        occurrence_ordinals: set[int] = set()
        canonical_snapshots: set[tuple[str, str]] = set()
        snapshot_rows: dict[tuple[str, str], dict[str, object]] = {}
        native_git_ids: set[str] = set()
        for row in candidate_rows:
            _validate_evaluation_boundary(row)
            if (
                row.get("schema") != CANDIDATE_SCHEMA
                or row.get("run_id") != layout.run_id
            ):
                self.close()
                raise RunValidationError("candidate schema/run binding mismatch")
            try:
                record = _candidate_from_row(row)
            except (KeyError, TypeError, ValueError) as exc:
                self.close()
                raise RunValidationError("candidate record is malformed") from exc
            if record.attempt_id not in self._attempt_started:
                self.close()
                raise RunValidationError("candidate references an unknown attempt")
            if not record.candidate_id or record.candidate_id in self._candidates:
                self.close()
                raise RunValidationError("duplicate candidate ID")
            if record.iteration != len(self._candidates) + 1:
                self.close()
                raise RunValidationError("candidate ledger iteration order is invalid")
            if (
                record.parent_candidate_id is not None
                and record.parent_candidate_id not in self._candidates
            ):
                self.close()
                raise RunValidationError(
                    "candidate parent is not an earlier same-run candidate"
                )
            attempt_kind = str(self._attempt_started[record.attempt_id].get("kind", ""))
            prior_ids = self._attempt_candidates[record.attempt_id]
            if attempt_kind == "single":
                valid_role = (
                    record.role == "candidate"
                    and record.parent_candidate_id is None
                    and not prior_ids
                )
            else:
                prior = self._candidates.get(record.parent_candidate_id or "")
                valid_role = (
                    record.role == "control"
                    and record.parent_candidate_id is None
                    and not prior_ids
                ) or (
                    record.role == "challenger"
                    and len(prior_ids) == 1
                    and prior is not None
                    and prior.attempt_id == record.attempt_id
                    and prior.role == "control"
                )
            if not valid_role:
                self.close()
                raise RunValidationError("candidate role/parent binding is invalid")
            snapshot_key = (record.artifact_kind, record.snapshot_digest)
            occurrence_mode = (
                row.get("candidate_identity_mode") == "run_scoped_occurrence_ordinal"
            )
            if occurrence_mode:
                ordinal = row.get("candidate_occurrence_ordinal")
                if (
                    not isinstance(ordinal, int)
                    or isinstance(ordinal, bool)
                    or ordinal < 1
                    or ordinal in occurrence_ordinals
                    or record.iteration != ordinal
                    or record.candidate_id
                    != _occurrence_candidate_id(layout.run_id, ordinal)
                    or not row.get("candidate_identity_source")
                ):
                    self.close()
                    raise RunValidationError("candidate occurrence identity is invalid")
                occurrence_ordinals.add(ordinal)
            elif snapshot_key in canonical_snapshots:
                self.close()
                raise RunValidationError("duplicate candidate snapshot record")
            else:
                canonical_snapshots.add(snapshot_key)
            existing_snapshot = snapshot_rows.get(snapshot_key)
            if existing_snapshot is not None and any(
                row.get(field) != existing_snapshot.get(field)
                for field in ("artifact_digest", "store_ref")
            ):
                self.close()
                raise RunValidationError(
                    "candidate occurrences disagree on their CAS artifact"
                )
            _verify_candidate_artifact(layout, row)
            self._candidates[record.candidate_id] = record
            self._attempt_candidates[record.attempt_id].append(record.candidate_id)
            self._candidate_by_snapshot.setdefault(snapshot_key, record)
            snapshot_rows.setdefault(snapshot_key, row)
            native_id = record.native_candidate_id
            if not native_id or native_id in native_git_ids:
                self.close()
                raise RunValidationError("native Git candidate identity is not unique")
            native_git_ids.add(native_id)
        self._started: dict[str, dict] = {}
        self._terminal: set[str] = set()
        self._candidate_evaluation_candidates: dict[str, str] = {}
        self._evaluated_candidates: set[str] = set()
        self._terminal_rows: dict[str, dict] = {}
        for row in read_jsonl(layout.root / "evaluations.jsonl"):
            _validate_evaluation_boundary(row)
            if (
                row.get("schema") != EVALUATION_SCHEMA
                or row.get("run_id") != layout.run_id
                or row.get("operation") not in EVALUATION_OPERATIONS
            ):
                self.close()
                raise RunValidationError(
                    "attempt-evaluation schema/run/operation is invalid"
                )
            evaluation_id = str(row.get("evaluation_id", ""))
            candidate_id = str(row.get("candidate_id", ""))
            candidate = self._candidates.get(candidate_id)
            if not evaluation_id or candidate is None:
                self.close()
                raise RunValidationError("orphan evaluation has no candidate")
            if row.get("candidate_snapshot_digest") != candidate.snapshot_digest:
                self.close()
                raise RunValidationError("evaluation snapshot digest mismatch")
            if row.get("attempt_id") != candidate.attempt_id:
                self.close()
                raise RunValidationError("evaluation attempt binding mismatch")
            if row.get("evaluator_digest") != self.evaluator_digest:
                self.close()
                raise RunValidationError("evaluation evaluator digest mismatch")
            lifecycle = row.get("lifecycle")
            if lifecycle == "evaluation_started":
                if evaluation_id in self._started:
                    self.close()
                    raise RunValidationError("duplicate evaluation_started ID")
                if row.get("ended_at") is not None:
                    self.close()
                    raise RunValidationError("started evaluation already has ended_at")
                self._started[evaluation_id] = row
                candidate_evaluation_id = str(row.get("candidate_evaluation_id", ""))
                if not candidate_evaluation_id:
                    self.close()
                    raise RunValidationError("candidate evaluation ID is empty")
                previous = self._candidate_evaluation_candidates.setdefault(
                    candidate_evaluation_id, candidate_id
                )
                if previous != candidate_id:
                    self.close()
                    raise RunValidationError(
                        "candidate evaluation spans multiple candidates"
                    )
                self._evaluated_candidates.add(candidate_id)
            elif lifecycle == "evaluation_terminal":
                if evaluation_id in self._terminal:
                    self.close()
                    raise RunValidationError("duplicate evaluation terminal")
                self._terminal.add(evaluation_id)
                self._terminal_rows[evaluation_id] = row
            else:
                self.close()
                raise RunValidationError("unknown evaluation lifecycle")
        for evaluation_id, terminal in self._terminal_rows.items():
            started = self._started.get(evaluation_id)
            if started is None:
                self.close()
                raise RunValidationError("evaluation terminal has no start")
            for field_name in (
                "attempt_id",
                "candidate_evaluation_id",
                "benchmark_evaluation_id",
                "parent_evaluation_id",
                "benchmark_id",
                "evaluation_kind",
                "candidate_id",
                "sample_index",
                "task_id",
                "replicate",
                "measurement_seed",
                "operation",
                "candidate_snapshot_digest",
                "evaluator_digest",
                "started_at",
            ):
                if field_name in started and terminal.get(field_name) != started.get(
                    field_name
                ):
                    self.close()
                    raise RunValidationError(
                        f"evaluation terminal changed {field_name}"
                    )
            outcome = terminal.get("outcome")
            score = terminal.get("score")
            constraints_pass = terminal.get("constraints_pass")
            if outcome not in TASK_OUTCOMES or terminal.get("ended_at") is None:
                self.close()
                raise RunValidationError("evaluation terminal outcome is invalid")
            if outcome == "measured":
                if (
                    not isinstance(score, (int, float))
                    or isinstance(score, bool)
                    or not math.isfinite(float(score))
                ):
                    self.close()
                    raise RunValidationError("measured terminal score is invalid")
            elif score is not None:
                self.close()
                raise RunValidationError("inconclusive terminal carries a score")
            if constraints_pass is not None and not isinstance(constraints_pass, bool):
                self.close()
                raise RunValidationError("terminal constraints_pass is invalid")
            raw_usage = terminal.get("usage")
            usage_fields = {
                "input_tokens",
                "output_tokens",
                "cache_tokens",
                "token_status",
                "input_token_status",
                "output_token_status",
                "cache_token_status",
                "cost_usd",
                "cost_status",
            }
            if not isinstance(raw_usage, Mapping) or set(raw_usage) != usage_fields:
                self.close()
                raise RunValidationError("evaluation terminal usage is invalid")
            try:
                TaskEvaluationOutcome(
                    outcome=str(outcome),
                    score=score,
                    wall_seconds=terminal.get("wall_seconds"),
                    summary=terminal.get("summary"),
                    artifact_refs=MappingProxyType(
                        dict(terminal.get("artifact_refs") or {})
                    ),
                    provenance=MappingProxyType(dict(terminal.get("provenance") or {})),
                    input_tokens=raw_usage.get("input_tokens"),
                    output_tokens=raw_usage.get("output_tokens"),
                    cache_tokens=raw_usage.get("cache_tokens"),
                    token_status=str(raw_usage.get("token_status")),
                    input_token_status=str(raw_usage.get("input_token_status")),
                    output_token_status=str(raw_usage.get("output_token_status")),
                    cache_token_status=str(raw_usage.get("cache_token_status")),
                    cost_usd=raw_usage.get("cost_usd"),
                    cost_status=str(raw_usage.get("cost_status")),
                    constraints_pass=constraints_pass,
                )
            except (TypeError, ValueError) as exc:
                self.close()
                raise RunValidationError(
                    "evaluation terminal usage is invalid"
                ) from exc
        self._validate_loaded_evaluation_scopes()
        self._validate_loaded_usage()
        event_ids: set[str] = set()
        candidate_terminal_ids: set[str] = set()
        task_retry_phases: set[tuple[str, int, str]] = set()
        task_retry_terminal_outcomes: dict[tuple[str, int], str] = {}
        self._delivery_receipts: dict[str, dict[str, object]] = {}
        self._events: list[dict[str, object]] = []
        known_event_evaluations = set(self._candidate_evaluation_candidates)
        for row in read_jsonl(layout.root / "events.jsonl"):
            _validate_evaluation_boundary(row)
            if str(row.get("event_type", "")).lower() in _HELD_OUT_EVENT_TYPES:
                self.close()
                raise RunValidationError("held-out event found in evaluation artifacts")
            if row.get("schema") != EVENT_SCHEMA or row.get("run_id") != layout.run_id:
                self.close()
                raise RunValidationError("event schema/run binding mismatch")
            self._events.append(dict(row))
            event_id = str(row.get("event_id", ""))
            if not event_id or event_id in event_ids:
                self.close()
                raise RunValidationError("duplicate or empty event ID")
            event_ids.add(event_id)
            candidate_id = row.get("candidate_id")
            if candidate_id is not None and candidate_id not in self._candidates:
                self.close()
                raise RunValidationError("event references unknown candidate")
            if candidate_id is not None:
                candidate = self._candidates[str(candidate_id)]
                if row.get("attempt_id") != candidate.attempt_id:
                    self.close()
                    raise RunValidationError("candidate event attempt binding mismatch")
            evaluation_id = row.get("evaluation_id")
            if (
                evaluation_id is not None
                and str(evaluation_id) not in known_event_evaluations
            ):
                self.close()
                raise RunValidationError("event references unknown evaluation")
            event_type = row.get("event_type")
            if event_type in {
                "task_evaluation_attempt_started",
                "task_evaluation_attempt_terminal",
            }:
                task_evaluation_id = str(row.get("task_evaluation_id", ""))
                started = self._started.get(task_evaluation_id)
                retry_ordinal = row.get("retry_ordinal")
                phase = (
                    "started"
                    if event_type == "task_evaluation_attempt_started"
                    else "terminal"
                )
                retry_event_fields = {
                    "schema",
                    "event_id",
                    "timestamp",
                    "run_id",
                    "method",
                    "benchmark",
                    "event_type",
                    "candidate_id",
                    "attempt_id",
                    "candidate_evaluation_id",
                    "task_evaluation_id",
                    "benchmark_id",
                    "sample_index",
                    "task_id",
                    "replicate",
                    "measurement_seed",
                    "retry_ordinal",
                    "evaluator_digest",
                }
                if phase == "terminal":
                    retry_event_fields.add("task_outcome")
                phase_key = (task_evaluation_id, retry_ordinal, phase)
                expected = {
                    "candidate_id": started.get("candidate_id") if started else None,
                    "attempt_id": started.get("attempt_id") if started else None,
                    "candidate_evaluation_id": (
                        started.get("candidate_evaluation_id") if started else None
                    ),
                    "benchmark_id": started.get("benchmark_id") if started else None,
                    "sample_index": started.get("sample_index") if started else None,
                    "task_id": started.get("task_id") if started else None,
                    "replicate": started.get("replicate") if started else None,
                    "measurement_seed": (
                        started.get("measurement_seed") if started else None
                    ),
                    "evaluator_digest": self.evaluator_digest,
                }
                if (
                    started is None
                    or isinstance(retry_ordinal, bool)
                    or not isinstance(retry_ordinal, int)
                    or retry_ordinal < 0
                    or set(row) != retry_event_fields
                    or phase_key in task_retry_phases
                    or (
                        phase == "terminal"
                        and (task_evaluation_id, retry_ordinal, "started")
                        not in task_retry_phases
                    )
                    or (
                        phase == "started"
                        and retry_ordinal > 0
                        and task_retry_terminal_outcomes.get(
                            (task_evaluation_id, retry_ordinal - 1)
                        )
                        != "inconclusive"
                    )
                    or any(row.get(name) != value for name, value in expected.items())
                    or (phase == "started") != ("task_outcome" not in row)
                ):
                    self.close()
                    raise RunValidationError("task retry event is invalid")
                if phase == "terminal":
                    retry_outcome = row.get("task_outcome")
                    expected_outcome_fields = {
                        "outcome",
                        "score",
                        "wall_seconds",
                        "summary",
                        "artifact_refs",
                        "provenance",
                        "input_tokens",
                        "output_tokens",
                        "cache_tokens",
                        "token_status",
                        "input_token_status",
                        "output_token_status",
                        "cache_token_status",
                        "cost_usd",
                        "cost_status",
                        "constraints_pass",
                    }
                    if (
                        not isinstance(retry_outcome, Mapping)
                        or set(retry_outcome) != expected_outcome_fields
                        or not isinstance(retry_outcome.get("artifact_refs"), Mapping)
                        or not isinstance(retry_outcome.get("provenance"), Mapping)
                    ):
                        self.close()
                        raise RunValidationError("task retry outcome is invalid")
                    try:
                        validated_retry_outcome = TaskEvaluationOutcome(
                            outcome=retry_outcome["outcome"],  # type: ignore[arg-type]
                            score=retry_outcome["score"],  # type: ignore[arg-type]
                            wall_seconds=retry_outcome["wall_seconds"],  # type: ignore[arg-type]
                            summary=retry_outcome["summary"],  # type: ignore[arg-type]
                            artifact_refs=MappingProxyType(
                                dict(retry_outcome["artifact_refs"])  # type: ignore[arg-type]
                            ),
                            provenance=MappingProxyType(
                                dict(retry_outcome["provenance"])  # type: ignore[arg-type]
                            ),
                            input_tokens=retry_outcome["input_tokens"],  # type: ignore[arg-type]
                            output_tokens=retry_outcome["output_tokens"],  # type: ignore[arg-type]
                            cache_tokens=retry_outcome["cache_tokens"],  # type: ignore[arg-type]
                            token_status=retry_outcome["token_status"],  # type: ignore[arg-type]
                            input_token_status=retry_outcome["input_token_status"],  # type: ignore[arg-type]
                            output_token_status=retry_outcome["output_token_status"],  # type: ignore[arg-type]
                            cache_token_status=retry_outcome["cache_token_status"],  # type: ignore[arg-type]
                            cost_usd=retry_outcome["cost_usd"],  # type: ignore[arg-type]
                            cost_status=retry_outcome["cost_status"],  # type: ignore[arg-type]
                            constraints_pass=retry_outcome["constraints_pass"],  # type: ignore[arg-type]
                        )
                    except (TypeError, ValueError) as exc:
                        self.close()
                        raise RunValidationError(
                            "task retry outcome is invalid"
                        ) from exc
                    task_retry_terminal_outcomes[
                        (task_evaluation_id, retry_ordinal)
                    ] = validated_retry_outcome.outcome
                task_retry_phases.add(phase_key)
            if event_type == "candidate_evaluation_terminal":
                candidate_evaluation_id = str(row.get("candidate_evaluation_id", ""))
                if (
                    not candidate_evaluation_id
                    or candidate_evaluation_id in candidate_terminal_ids
                ):
                    self.close()
                    raise RunValidationError(
                        "candidate evaluation has duplicate terminals"
                    )
                bound_candidate_id = self._candidate_evaluation_candidates.get(
                    candidate_evaluation_id
                )
                bound_candidate = self._candidates.get(str(bound_candidate_id))
                if (
                    bound_candidate is None
                    or row.get("candidate_id") != bound_candidate.candidate_id
                    or row.get("snapshot_digest") != bound_candidate.snapshot_digest
                    or row.get("evaluator_digest") != self.evaluator_digest
                    or row.get("state") != "TERMINAL"
                    or row.get("outcome") not in TASK_OUTCOMES
                ):
                    self.close()
                    raise RunValidationError(
                        "candidate evaluation terminal is misbound"
                    )
                candidate_terminal_ids.add(candidate_evaluation_id)
            elif row.get("event_type") == "delivery_receipt":
                expected_fields = {
                    "schema",
                    "event_id",
                    "timestamp",
                    "run_id",
                    "event_type",
                    "terminal_id",
                    "result_digest",
                    "delivered",
                }
                terminal_id = str(row.get("terminal_id", ""))
                result_path = layout.root / "results" / f"campaign-{terminal_id}.json"
                if (
                    set(row) != expected_fields
                    or not terminal_id
                    or terminal_id not in candidate_terminal_ids
                    or terminal_id in self._delivery_receipts
                    or row.get("delivered") is not True
                    or not result_path.is_file()
                    or row.get("result_digest") != file_digest(result_path)
                ):
                    self.close()
                    raise RunValidationError("delivery receipt is invalid")
                self._delivery_receipts[terminal_id] = dict(row)
        for attempt_id in self._attempt_terminal:
            attempt = self._attempt_record(attempt_id)
            candidates = [
                self._candidates[candidate_id]
                for candidate_id in self._attempt_candidates[attempt_id]
            ]
            expected_roles = (
                ("candidate",)
                if attempt.kind == "single"
                else ("control", "challenger")
            )
            if attempt.gate_decision == "skip":
                roles_valid = not candidates
            else:
                roles_valid = (
                    tuple(candidate.role for candidate in candidates) == expected_roles
                )
            if not roles_valid:
                self.close()
                raise RunValidationError("terminal attempt candidate roles are invalid")
            if any(
                not self._candidate_is_terminal(candidate_id)
                for candidate_id in self._attempt_candidates[attempt_id]
            ):
                self.close()
                raise RunValidationError("terminal attempt owns nonterminal candidates")
        for candidate in self._candidates.values():
            if candidate.role != "challenger":
                continue
            parent = self._candidates.get(candidate.parent_candidate_id or "")
            if (
                parent is None
                or parent.attempt_id != candidate.attempt_id
                or parent.role != "control"
                or not self._candidate_is_terminal(parent.candidate_id)
            ):
                self.close()
                raise RunValidationError(
                    "challenger parent is not a terminal same-attempt control"
                )
        if not self._read_only:
            for attempt_id in (
                self._attempt_sealed.keys() - self._attempt_terminal.keys()
            ):
                self._maybe_terminal_attempt(attempt_id)
        self._next_iteration = 1 + max(
            (candidate.iteration for candidate in self._candidates.values()), default=0
        )

    def _validate_loaded_usage(self) -> None:
        """Fail closed on duplicate, orphaned, or misbound evaluation usage."""

        seen_calls: set[str] = set()
        self._usage_rows: list[dict[str, object]] = []
        for row in read_jsonl(self.layout.root / "usage.jsonl"):
            _validate_evaluation_boundary(row)
            if (
                row.get("run_id") != self.layout.run_id
                or row.get("method") != self.layout.method
            ):
                self.close()
                raise RunValidationError("usage run/method binding is invalid")
            call_id = row.get("call_id")
            if not isinstance(call_id, str) or not call_id or call_id in seen_calls:
                self.close()
                raise RunValidationError("usage call identity is invalid")
            seen_calls.add(call_id)
            raw_evaluation_id = row.get("evaluation_id")
            if raw_evaluation_id is None:
                if _is_explicit_non_evaluation_usage(row):
                    try:
                        _validate_explicit_usage_call_binding(row)
                    except RunValidationError:
                        self.close()
                        raise
                    self._usage_rows.append(dict(row))
                    continue
                self.close()
                raise RunValidationError("usage evaluation binding is invalid")
            evaluation_id = str(raw_evaluation_id)
            if not evaluation_id:
                self.close()
                raise RunValidationError("usage evaluation binding is invalid")
            started = self._started.get(evaluation_id)
            if started is None and _is_explicit_non_evaluation_usage(row):
                try:
                    _validate_explicit_usage_call_binding(row)
                except RunValidationError:
                    self.close()
                    raise
                self._usage_rows.append(dict(row))
                continue
            if started is None:
                self.close()
                raise RunValidationError("usage evaluation binding is invalid")
            expected = {
                "run_id": self.layout.run_id,
                "method": self.layout.method,
                "attempt_id": started.get("attempt_id"),
                "candidate_id": started.get("candidate_id"),
                "benchmark_id": started.get("benchmark_id", self.layout.benchmark),
                "benchmark_evaluation_id": started.get("benchmark_evaluation_id"),
                "task_id": started.get("task_id"),
                "evaluation_id": evaluation_id,
            }
            if any(row.get(field) != value for field, value in expected.items()):
                self.close()
                raise RunValidationError("usage authority binding is invalid")
            self._usage_rows.append(dict(row))

    def _validate_loaded_evaluation_scopes(self) -> None:
        """Validate persisted frozen children while rebuilding recorder indexes."""

        by_candidate_evaluation: dict[str, list[dict]] = {}
        for row in self._started.values():
            candidate_evaluation_id = str(row.get("candidate_evaluation_id", ""))
            by_candidate_evaluation.setdefault(candidate_evaluation_id, []).append(row)
        for candidate_evaluation_id, rows in by_candidate_evaluation.items():
            candidate_ids = {str(row.get("candidate_id", "")) for row in rows}
            if len(candidate_ids) != 1:
                raise RunValidationError("candidate evaluation mixes candidates")
            candidate = self._candidates[next(iter(candidate_ids))]
            attempt = self._attempt_record(candidate.attempt_id)
            attempt_benchmark_ids = tuple(
                benchmark_id for benchmark_id, _slots in attempt.benchmark_slots
            )
            observed_benchmarks = {str(row.get("benchmark_id", "")) for row in rows}
            if observed_benchmarks != set(attempt_benchmark_ids):
                raise RunValidationError(
                    "candidate evaluation benchmark set is invalid"
                )
            for benchmark_id in attempt_benchmark_ids:
                children = [
                    row for row in rows if row.get("benchmark_id") == benchmark_id
                ]
                parent_ids = {
                    str(row.get("benchmark_evaluation_id", "")) for row in children
                }
                if len(parent_ids) != 1 or not next(iter(parent_ids)):
                    raise RunValidationError("resumed benchmark parent is invalid")
                parent_id = next(iter(parent_ids))
                if any(
                    row.get("candidate_evaluation_id") != candidate_evaluation_id
                    or row.get("parent_evaluation_id") != parent_id
                    or row.get("evaluation_kind") != "task"
                    for row in children
                ):
                    raise RunValidationError(
                        "task evaluation parent linkage is invalid"
                    )
                self._validate_loaded_slots(
                    children,
                    expected=attempt.slots_for(benchmark_id),
                    eligible=self._benchmark_authorizations[benchmark_id],
                )

    @staticmethod
    def _validate_loaded_slots(
        rows: Iterable[Mapping[str, object]],
        *,
        expected: tuple[SampleSlot, ...],
        eligible: Iterable[str],
    ) -> None:
        try:
            slots = sorted(
                (
                    SampleSlot(
                        sample_index=row["sample_index"],  # type: ignore[arg-type]
                        task_id=row["task_id"],  # type: ignore[arg-type]
                        replicate=row["replicate"],  # type: ignore[arg-type]
                        measurement_seed=row["measurement_seed"],  # type: ignore[arg-type]
                    )
                    for row in rows
                ),
                key=lambda item: item.sample_index,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RunValidationError(
                "persisted evaluation task slots are invalid"
            ) from exc
        if (
            not slots
            or tuple(slot.sample_index for slot in slots) != tuple(range(len(slots)))
            or len({(slot.replicate, slot.task_id) for slot in slots}) != len(slots)
            or not {slot.task_id for slot in slots}.issubset(set(eligible))
        ):
            raise RunValidationError("persisted evaluation task slots are invalid")
        if expected is not None and tuple(slots) != tuple(expected):
            raise RunValidationError(
                "persisted evaluation task slots changed their frozen realization"
            )

    @staticmethod
    def _validate_attempt_slot_payload(value: object) -> None:
        if not isinstance(value, list) or not value:
            raise RunValidationError("attempt benchmark slots are invalid")
        benchmark_ids: list[str] = []
        for child in value:
            if not isinstance(child, Mapping) or set(child) != {
                "benchmark_id",
                "slots",
            }:
                raise RunValidationError("attempt benchmark binding is invalid")
            benchmark_id = child["benchmark_id"]
            raw_slots = child["slots"]
            if (
                not isinstance(benchmark_id, str)
                or not benchmark_id
                or benchmark_id in benchmark_ids
            ):
                raise RunValidationError("attempt benchmark identity is invalid")
            benchmark_ids.append(benchmark_id)
            if not isinstance(raw_slots, list) or not raw_slots:
                raise RunValidationError("attempt benchmark slots are empty")
            slots: list[SampleSlot] = []
            for raw in raw_slots:
                try:
                    slot = SampleSlot.from_mapping(raw)
                except (ManifestValidationError, TypeError):
                    raise RunValidationError("attempt benchmark slot is malformed")
                slots.append(slot)
            if tuple(slot.sample_index for slot in slots) != tuple(
                range(len(slots))
            ) or len({(slot.replicate, slot.task_id) for slot in slots}) != len(slots):
                raise RunValidationError("attempt benchmark slots are not canonical")

    @classmethod
    def _attempt_slots_from_payload(
        cls, value: object
    ) -> tuple[tuple[str, tuple[SampleSlot, ...]], ...]:
        cls._validate_attempt_slot_payload(value)
        assert isinstance(value, list)
        return tuple(
            (
                str(child["benchmark_id"]),
                tuple(SampleSlot.from_mapping(item) for item in child["slots"]),
            )
            for child in value
        )

    def _validate_caller_selected_attempt_slots(
        self,
        benchmark_slots: tuple[tuple[str, tuple[SampleSlot, ...]], ...],
    ) -> None:
        selected_benchmarks = tuple(
            benchmark_id for benchmark_id, _slots in benchmark_slots
        )
        expected_order = tuple(
            benchmark_id
            for benchmark_id in self._benchmark_order
            if benchmark_id in selected_benchmarks
        )
        if not selected_benchmarks or selected_benchmarks != expected_order:
            raise RunValidationError(
                "caller-selected benchmark keys/order are not an active subset"
            )
        for benchmark_id, slots in benchmark_slots:
            if not slots:
                raise RunValidationError(
                    "caller-selected attempt selected no benchmark slots"
                )
            if tuple(slot.sample_index for slot in slots) != tuple(
                range(len(slots))
            ) or len({(slot.replicate, slot.task_id) for slot in slots}) != len(slots):
                raise RunValidationError(
                    "caller-selected benchmark slots are not canonical"
                )
            membership = self.benchmark_task_ids[benchmark_id]
            if not {slot.task_id for slot in slots}.issubset(set(membership)):
                raise RunValidationError(
                    "caller-selected benchmark slots escape recorder membership"
                )
        if (
            self._campaign_aggregate is not None
            and selected_benchmarks != self._benchmark_order
        ):
            raise RunValidationError(
                "campaign aggregate policy requires every active benchmark"
            )

    def _freeze_caller_selected_attempt_slots(
        self, benchmark_slots: Mapping[str, Sequence[SampleSlot]]
    ) -> tuple[tuple[str, tuple[SampleSlot, ...]], ...]:
        if not isinstance(benchmark_slots, Mapping):
            raise RunValidationError(
                "caller-selected benchmark slots must be a mapping"
            )
        expected_benchmarks = self._benchmark_order
        unknown = set(benchmark_slots) - set(expected_benchmarks)
        if unknown:
            raise RunValidationError("caller-selected benchmark keys are not active")
        frozen: list[tuple[str, tuple[SampleSlot, ...]]] = []
        for benchmark_id in expected_benchmarks:
            if benchmark_id not in benchmark_slots:
                continue
            raw_slots = benchmark_slots[benchmark_id]
            if not isinstance(raw_slots, Sequence) or isinstance(
                raw_slots, (str, bytes, bytearray)
            ):
                raise RunValidationError(
                    "caller-selected benchmark slots must be a SampleSlot sequence"
                )
            if any(not isinstance(slot, SampleSlot) for slot in raw_slots):
                raise RunValidationError(
                    "caller-selected benchmark slots must contain SampleSlot values"
                )
            if not raw_slots:
                continue
            frozen.append(
                (
                    benchmark_id,
                    tuple(raw_slots),
                )
            )
        result = tuple(frozen)
        self._validate_caller_selected_attempt_slots(result)
        return result

    @staticmethod
    def _attempt_slot_payload(
        benchmark_slots: tuple[tuple[str, tuple[SampleSlot, ...]], ...],
    ) -> list[dict[str, object]]:
        return [
            {
                "benchmark_id": benchmark_id,
                "slots": [slot.as_dict() for slot in slots],
            }
            for benchmark_id, slots in benchmark_slots
        ]

    def _attempt_record(self, attempt_id: str) -> AttemptRecord:
        started = self._attempt_started.get(attempt_id)
        if started is None:
            raise RunValidationError("attempt is not recorder-owned")
        slots = self._attempt_slots_from_payload(started["benchmark_slots"])
        terminal = self._attempt_terminal.get(attempt_id)
        sealed = self._attempt_sealed.get(attempt_id)
        gate = self._attempt_gate.get(attempt_id)
        state = (
            "TERMINAL"
            if terminal is not None
            else "SEALED"
            if sealed is not None
            else "OPEN"
            if gate is not None and gate.get("decision") == "enter"
            else "PENDING"
        )
        return AttemptRecord(
            attempt_id=attempt_id,
            attempt_index=tuple(self._attempt_started).index(attempt_id) + 1,
            state=state,
            kind=str(started["kind"]),
            gate_decision=(str(gate["decision"]) if gate is not None else None),
            benchmark_slots=slots,
            candidate_ids=tuple(self._attempt_candidates.get(attempt_id, ())),
            started_at=float(started["started_at"]),
            sealed_at=(float(sealed["sealed_at"]) if sealed is not None else None),
            terminal_at=(
                float(terminal["terminal_at"]) if terminal is not None else None
            ),
        )

    def start_attempt(
        self,
        attempt_id: str,
        *,
        max_attempts: Optional[int] = None,
        kind: str | None = None,
        benchmark_slots: Mapping[str, Sequence[SampleSlot]] | None = None,
    ) -> AttemptRecord:
        """Durably consume one attempt slot and freeze its benchmark slots."""

        attempt_id = str(attempt_id)
        if not attempt_id:
            raise ValueError("attempt_id cannot be empty")
        if max_attempts is not None and (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise ValueError("max_attempts must be >= 1")
        with self._lock:
            if attempt_id in self._attempt_started:
                if benchmark_slots is None and kind is None:
                    return self._attempt_record(attempt_id)
                raise RunValidationError(
                    "existing attempt replay must not restate its authority"
                )
            if kind not in ATTEMPT_KINDS:
                raise RunValidationError("attempt.start requires a valid kind")
            if benchmark_slots is None:
                raise RunValidationError(
                    "attempt.start requires caller-selected benchmark slots"
                )
            caller_slots = self._freeze_caller_selected_attempt_slots(benchmark_slots)
            if max_attempts is not None and len(self._attempt_started) >= max_attempts:
                raise AttemptLimit("attempt limit reached")
            started_at = time.time()
            row: dict[str, object] = {
                "schema": ATTEMPT_SCHEMA,
                "lifecycle": "attempt_started",
                "run_id": self.layout.run_id,
                "attempt_id": attempt_id,
                "state": "PENDING",
                "kind": kind,
                "benchmark_slots": self._attempt_slot_payload(caller_slots),
                "started_at": started_at,
            }
            self._writers["attempts.jsonl"].append(row)
            self._attempt_started[attempt_id] = row
            self._attempt_candidates[attempt_id] = []
            return self._attempt_record(attempt_id)

    def transition_attempt(self, attempt_id: str, decision: str) -> AttemptRecord:
        """Persist the method-owned B gate decision exactly once."""

        if decision not in GATE_DECISIONS:
            raise ValueError("gate decision must be enter or skip")
        with self._lock:
            attempt = self._attempt_record(attempt_id)
            existing = self._attempt_gate.get(attempt_id)
            if existing is not None:
                if existing.get("decision") != decision:
                    raise RunValidationError("attempt gate decision changed")
                return attempt
            if attempt.state != "PENDING":
                raise RunValidationError("attempt gate transition is unavailable")
            transitioned_at = time.time()
            row: dict[str, object] = {
                "schema": ATTEMPT_SCHEMA,
                "lifecycle": "gate_transition",
                "run_id": self.layout.run_id,
                "attempt_id": attempt_id,
                "state": "OPEN" if decision == "enter" else "SKIPPED",
                "decision": decision,
                "started_at": attempt.started_at,
                "transitioned_at": transitioned_at,
            }
            self._writers["attempts.jsonl"].append(row)
            self._attempt_gate[attempt_id] = row
            if decision == "skip":
                terminal = {
                    "schema": ATTEMPT_SCHEMA,
                    "lifecycle": "attempt_terminal",
                    "run_id": self.layout.run_id,
                    "attempt_id": attempt_id,
                    "state": "TERMINAL",
                    "decision": decision,
                    "started_at": attempt.started_at,
                    "sealed_at": None,
                    "terminal_at": time.time(),
                }
                self._writers["attempts.jsonl"].append(terminal)
                self._attempt_terminal[attempt_id] = terminal
            return self._attempt_record(attempt_id)

    def attempt(self, attempt_id: str) -> Optional[AttemptRecord]:
        with self._lock:
            if attempt_id not in self._attempt_started:
                return None
            return self._attempt_record(attempt_id)

    @property
    def n_attempts(self) -> int:
        return len(self._attempt_started)

    @property
    def all_attempts_terminal(self) -> bool:
        return len(self._attempt_terminal) == len(self._attempt_started)

    def assert_attempt_accepts(
        self, attempt_id: str, *, existing_candidate_id: Optional[str] = None
    ) -> AttemptRecord:
        attempt = self._attempt_record(attempt_id)
        if (
            attempt.state != "OPEN"
            and existing_candidate_id not in attempt.candidate_ids
        ):
            raise RunValidationError("sealed attempt cannot accept a new candidate")
        return attempt

    def _validate_candidate_role(
        self,
        attempt_id: str,
        *,
        role: str,
        parent_candidate_id: str | None,
    ) -> None:
        attempt = self._attempt_record(attempt_id)
        if attempt.state not in {"PENDING", "OPEN"}:
            raise RunValidationError("attempt cannot accept a new candidate")
        candidates = [self._candidates[item] for item in attempt.candidate_ids]
        if attempt.kind == "single":
            valid = (
                role == "candidate" and parent_candidate_id is None and not candidates
            )
        elif role == "control":
            valid = parent_candidate_id is None and not candidates
        elif role == "challenger":
            control = candidates[0] if len(candidates) == 1 else None
            valid = (
                control is not None
                and control.role == "control"
                and parent_candidate_id == control.candidate_id
            )
        else:
            valid = False
        if not valid:
            raise RunValidationError("candidate role/parent violates attempt kind")

    def seal_attempt(self, attempt_id: str) -> AttemptRecord:
        """Linearize sealing against candidate admission and derive terminality."""

        with self._lock:
            attempt = self._attempt_record(attempt_id)
            if attempt.gate_decision != "enter":
                raise RunValidationError("only an entered attempt can be sealed")
            candidates = [self._candidates[item] for item in attempt.candidate_ids]
            expected_roles = (
                ("candidate",)
                if attempt.kind == "single"
                else ("control", "challenger")
            )
            if tuple(candidate.role for candidate in candidates) != expected_roles:
                raise RunValidationError(
                    "attempt cannot seal before its candidate roles are complete"
                )
            if attempt.state == "OPEN":
                sealed_at = time.time()
                row: dict[str, object] = {
                    "schema": ATTEMPT_SCHEMA,
                    "lifecycle": "attempt_sealed",
                    "run_id": self.layout.run_id,
                    "attempt_id": attempt_id,
                    "state": "SEALED",
                    "decision": "enter",
                    "started_at": attempt.started_at,
                    "sealed_at": sealed_at,
                }
                self._writers["attempts.jsonl"].append(row)
                self._attempt_sealed[attempt_id] = row
            self._maybe_terminal_attempt(attempt_id)
            return self._attempt_record(attempt_id)

    def _candidate_is_terminal(self, candidate_id: str) -> bool:
        return any(
            row.get("event_type") == "candidate_evaluation_terminal"
            and row.get("candidate_id") == candidate_id
            for row in read_jsonl(self.layout.root / "events.jsonl")
        )

    def _maybe_terminal_attempt(self, attempt_id: str) -> None:
        if (
            attempt_id not in self._attempt_sealed
            or attempt_id in self._attempt_terminal
        ):
            return
        candidates = self._attempt_candidates.get(attempt_id, [])
        if not candidates or not all(
            self._candidate_is_terminal(item) for item in candidates
        ):
            return
        sealed = self._attempt_sealed[attempt_id]
        row: dict[str, object] = {
            "schema": ATTEMPT_SCHEMA,
            "lifecycle": "attempt_terminal",
            "run_id": self.layout.run_id,
            "attempt_id": attempt_id,
            "state": "TERMINAL",
            "decision": "enter",
            "started_at": sealed["started_at"],
            "sealed_at": sealed["sealed_at"],
            "terminal_at": time.time(),
        }
        self._writers["attempts.jsonl"].append(row)
        self._attempt_terminal[attempt_id] = row

    @property
    def n_candidate_evaluations(self) -> int:
        return len(set(self._candidate_evaluation_candidates.values()))

    @property
    def campaign_aggregate_kind(self) -> str | None:
        """The optional scalar aggregate authorized by the campaign run freeze."""

        if self._campaign_aggregate is None:
            return None
        return self._campaign_aggregate.kind

    def _authorized_campaign_score(
        self, results: Sequence[BenchmarkEvaluationResult]
    ) -> float | None:
        measured = [
            float(result.score) for result in results if result.score is not None
        ]
        if not measured or self._campaign_aggregate is None:
            return None
        return sum(measured) / len(measured)

    def _campaign_aggregate_payload(self) -> Mapping[str, str] | None:
        if self._campaign_aggregate is None:
            return None
        return self._campaign_aggregate.as_dict()

    @property
    def n_candidates(self) -> int:
        """Number of durable candidate records (including explicit occurrences)."""

        return len(self._candidates)

    def candidate_by_id(self, candidate_id: str) -> CandidateRecord:
        """Return one recorder-validated candidate named by a trusted result."""

        try:
            return self._candidates[str(candidate_id)]
        except KeyError as exc:
            raise RunValidationError(
                "candidate result names an unknown candidate"
            ) from exc

    @property
    def has_incomplete_campaign_evaluations(self) -> bool:
        """Whether any admitted evaluation occurrence lacks its terminal."""

        for candidate_id in self._candidate_evaluation_candidates.values():
            candidate = self._candidates.get(candidate_id)
            if candidate is None:
                raise RunValidationError("campaign evaluation candidate is unavailable")
            if (
                self.campaign_candidate_state(
                    candidate, occurrence_key=candidate.candidate_id
                ).terminal_result
                is None
            ):
                return True
        return False

    def close(self) -> None:
        for writer in getattr(self, "_writers", {}).values():
            writer.close()

    def __enter__(self) -> "RunRecorder":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def freeze_git_candidate(
        self,
        repo: Path,
        *,
        gateway_commit_sha: str,
        tree_sha: str,
        gateway_root_commit_sha: str,
        native_candidate_id: str,
        role: str,
        parent_candidate_id: Optional[str] = None,
        caused_by_event_id: Optional[str] = None,
        attempt_id: str,
    ) -> CandidateRecord:
        if not isinstance(native_candidate_id, str) or not native_candidate_id:
            raise ValueError("native_candidate_id cannot be empty")
        repo = Path(repo).resolve(strict=True)
        commit = _git_output(repo, "rev-parse", f"{gateway_commit_sha}^{{commit}}")
        tree = _git_output(repo, "rev-parse", f"{commit}^{{tree}}")
        if commit != str(gateway_commit_sha) or tree != str(tree_sha):
            raise RunValidationError("candidate commit/tree identity mismatch")
        gateway_root = _git_output(
            repo, "rev-parse", f"{gateway_root_commit_sha}^{{commit}}"
        )
        candidate_row = _git_output(
            repo, "rev-list", "--parents", "-n", "1", commit
        ).split()
        if commit != gateway_root and candidate_row != [commit, gateway_root]:
            raise RunValidationError(
                "candidate commit is not directly based on the gateway root"
            )
        snapshot_digest = "git-tree-sha1:" + tree
        key = ("git_bundle", snapshot_digest)
        with self._lock:
            bound_attempt_id = self._require_attempt_id(attempt_id)
            existing_native = self._bind_native_candidate_occurrence(
                native_candidate_id=native_candidate_id,
                snapshot_digest=snapshot_digest,
                attempt_id=bound_attempt_id,
            )
            if existing_native is not None:
                if (
                    existing_native.parent_candidate_id != parent_candidate_id
                    or existing_native.role != role
                ):
                    raise RunValidationError(
                        "native Git candidate identity changed role or parent"
                    )
                return existing_native

            ordinal = self._next_iteration
            candidate_id = _occurrence_candidate_id(self.layout.run_id, ordinal)
            artifact_candidate_id = _candidate_id(self.layout.run_id, snapshot_digest)
            existing_artifact = self._candidate_by_snapshot.get(key)
            if existing_artifact is not None:
                path = self.layout.resolve_store_ref(existing_artifact.store_ref)
                _verify_candidate_artifact(
                    self.layout, _candidate_to_row(existing_artifact)
                )
                extra = {
                    "tree_sha": existing_artifact.tree_sha,
                    "pinned_ref": existing_artifact.pinned_ref,
                    "candidate_identity_mode": "run_scoped_occurrence_ordinal",
                    "candidate_identity_source": "native_candidate_id",
                    "candidate_occurrence_ordinal": ordinal,
                }
                for field in (
                    "gateway_commit_sha",
                    "gateway_root_commit_sha",
                    "artifact_commit_sha",
                ):
                    value = getattr(existing_artifact, field)
                    if value is not None:
                        extra[field] = value
                return self._record_candidate(
                    native_candidate_id=native_candidate_id,
                    snapshot_digest=snapshot_digest,
                    artifact_digest=existing_artifact.artifact_digest,
                    store_path=path,
                    role=role,
                    parent_candidate_id=parent_candidate_id,
                    caused_by_event_id=caused_by_event_id,
                    attempt_id=bound_attempt_id,
                    candidate_id=candidate_id,
                    extra=extra,
                )

            pinned_ref = f"refs/rsi-arena/candidates/{artifact_candidate_id}"
            path = (
                self.layout.root
                / "candidates"
                / "git"
                / f"{artifact_candidate_id}.bundle"
            )
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            artifact_commit = _parentless_artifact_commit(
                repo,
                tree,
                gateway_commit_sha=commit,
                gateway_root_commit_sha=gateway_root,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "update-ref",
                    pinned_ref,
                    artifact_commit,
                ],
                check=True,
                env=_git_env(),
            )
            try:
                try:
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(repo),
                            "bundle",
                            "create",
                            str(temporary),
                            pinned_ref,
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        env=_git_env(),
                    )
                except BaseException:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
                    raise
            finally:
                subprocess.run(
                    ["git", "-C", str(repo), "update-ref", "-d", pinned_ref],
                    check=True,
                    env=_git_env(),
                )
            descriptor = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if path.exists():
                # A crash may have published the immutable bundle just before
                # its candidate row.  Verify and reuse it; never overwrite a
                # previously published candidate artifact.
                _verify_git_bundle(
                    path,
                    pinned_ref,
                    artifact_commit,
                    tree,
                    require_parentless=True,
                    gateway_commit_sha=commit,
                    gateway_root_commit_sha=gateway_root,
                )
                temporary.unlink()
            else:
                os.replace(temporary, path)
                os.chmod(path, 0o600)
                _fsync_directory(path.parent)
            artifact_digest = file_digest(path)
            _verify_git_bundle(
                path,
                pinned_ref,
                artifact_commit,
                tree,
                require_parentless=True,
                gateway_commit_sha=commit,
                gateway_root_commit_sha=gateway_root,
            )
            return self._record_candidate(
                native_candidate_id=native_candidate_id,
                snapshot_digest=snapshot_digest,
                artifact_digest=artifact_digest,
                store_path=path,
                role=role,
                parent_candidate_id=parent_candidate_id,
                caused_by_event_id=caused_by_event_id,
                attempt_id=bound_attempt_id,
                candidate_id=candidate_id,
                extra={
                    "gateway_commit_sha": commit,
                    "gateway_root_commit_sha": gateway_root,
                    "artifact_commit_sha": artifact_commit,
                    "tree_sha": tree,
                    "pinned_ref": pinned_ref,
                    "candidate_identity_mode": "run_scoped_occurrence_ordinal",
                    "candidate_identity_source": "native_candidate_id",
                    "candidate_occurrence_ordinal": ordinal,
                },
            )

    def _bind_native_candidate_occurrence(
        self,
        *,
        native_candidate_id: str,
        snapshot_digest: str,
        attempt_id: str,
    ) -> Optional[CandidateRecord]:
        """Resolve an occurrence replay/conflict before admitting a new slot."""

        if not isinstance(native_candidate_id, str) or not native_candidate_id:
            raise ValueError("native_candidate_id cannot be empty")
        native_matches = self._native_candidate_occurrences(
            native_candidate_id=native_candidate_id,
        )
        if len(native_matches) > 1:
            raise RunValidationError("native Git candidate identity is not unique")
        if native_matches:
            existing = native_matches[0]
            if existing.snapshot_digest != snapshot_digest:
                raise RunValidationError(
                    "native Git candidate identity changed snapshot"
                )
            if existing.attempt_id != attempt_id:
                raise RunValidationError(
                    "native Git occurrence cannot move across attempts"
                )
            return existing
        self.assert_attempt_accepts(attempt_id)
        return None

    def _require_attempt_id(self, attempt_id: str) -> str:
        """Return one explicit recorder-owned attempt authority."""

        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("attempt_id cannot be empty")
        if attempt_id not in self._attempt_started:
            raise RunValidationError("attempt is not recorder-owned")
        return attempt_id

    def _native_candidate_occurrences(
        self, *, native_candidate_id: str
    ) -> list[CandidateRecord]:
        return [
            candidate
            for candidate in self._candidates.values()
            if candidate.native_candidate_id == native_candidate_id
        ]

    def validate_git_native_candidate_admission(
        self,
        *,
        native_candidate_id: str,
        attempt_id: str,
        role: str,
        parent_candidate_id: str | None,
    ) -> str:
        """Reject an unseen Git occurrence at cap before gateway materialisation.

        A known native identity must still reach the Git freeze path so that its
        snapshot is compared with the durable occurrence before any cap error.
        """

        if not isinstance(native_candidate_id, str) or not native_candidate_id:
            raise ValueError("native_candidate_id cannot be empty")
        with self._lock:
            bound_attempt_id = self._require_attempt_id(attempt_id)
            native_matches = self._native_candidate_occurrences(
                native_candidate_id=native_candidate_id,
            )
            if len(native_matches) > 1:
                raise RunValidationError("native Git candidate identity is not unique")
            if not native_matches:
                self._validate_candidate_role(
                    bound_attempt_id,
                    role=role,
                    parent_candidate_id=parent_candidate_id,
                )
            elif native_matches[0].attempt_id != bound_attempt_id:
                raise RunValidationError(
                    "native Git occurrence cannot move across attempts"
                )
            elif (
                native_matches[0].role != role
                or native_matches[0].parent_candidate_id != parent_candidate_id
            ):
                raise RunValidationError("native Git occurrence changed role or parent")
            return bound_attempt_id

    def freeze_and_resume_or_start_campaign_evaluation(
        self,
        repo: Path,
        *,
        gateway_commit_sha: str,
        tree_sha: str,
        gateway_root_commit_sha: str,
        native_candidate_id: str,
        role: str,
        parent_candidate_id: Optional[str] = None,
        caused_by_event_id: Optional[str] = None,
        attempt_id: str,
    ) -> EvaluationHandle:
        """Bind one Git occurrence before resuming or reserving its campaign."""

        with self._lock:
            candidate = self.freeze_git_candidate(
                repo,
                gateway_commit_sha=gateway_commit_sha,
                tree_sha=tree_sha,
                gateway_root_commit_sha=gateway_root_commit_sha,
                native_candidate_id=native_candidate_id,
                role=role,
                parent_candidate_id=parent_candidate_id,
                caused_by_event_id=caused_by_event_id,
                attempt_id=attempt_id,
            )
            return self.resume_or_start_campaign_evaluation(candidate)

    def _record_candidate(
        self,
        *,
        attempt_id: str,
        native_candidate_id: Optional[str],
        snapshot_digest: str,
        artifact_digest: str,
        store_path: Path,
        role: str,
        parent_candidate_id: Optional[str],
        caused_by_event_id: Optional[str],
        extra: Mapping[str, object],
        candidate_id: str,
    ) -> CandidateRecord:
        self._validate_candidate_role(
            attempt_id,
            role=role,
            parent_candidate_id=parent_candidate_id,
        )
        if candidate_id in self._candidates:
            raise RunValidationError("duplicate candidate ID")
        relative = store_path.relative_to(self.layout.root).as_posix()
        created_at = time.time()
        row = {
            "schema": CANDIDATE_SCHEMA,
            "run_id": self.layout.run_id,
            "attempt_id": attempt_id,
            "role": role,
            "candidate_id": candidate_id,
            "native_candidate_id": native_candidate_id,
            "iteration": self._next_iteration,
            "artifact_kind": "git_bundle",
            "snapshot_digest": snapshot_digest,
            "artifact_digest": artifact_digest,
            "store_ref": relative,
            "parent_candidate_id": parent_candidate_id,
            "caused_by_event_id": caused_by_event_id,
            "created_at": created_at,
            **extra,
        }
        self._writers["candidates.jsonl"].append(row)
        record = _candidate_from_row(row)
        self._candidates[candidate_id] = record
        self._attempt_candidates[attempt_id].append(candidate_id)
        self._candidate_by_snapshot.setdefault(("git_bundle", snapshot_digest), record)
        self._next_iteration += 1
        self.event(
            "candidate_frozen",
            attempt_id=attempt_id,
            candidate_id=candidate_id,
            iteration=record.iteration,
            snapshot_digest=snapshot_digest,
            caused_by_event_id=caused_by_event_id,
            status="durable",
        )
        return record

    def start_campaign_evaluation(
        self,
        candidate: CandidateRecord,
    ) -> EvaluationHandle:
        """Start one candidate slot containing all required benchmark children."""

        with self._lock:
            if self._attempt_record(candidate.attempt_id).state != "OPEN":
                raise RunValidationError(
                    "candidate evaluation requires an entered attempt"
                )
            known = self._candidates.get(candidate.candidate_id)
            if known != candidate:
                raise RunValidationError("evaluation candidate is not recorder-owned")
            if candidate.candidate_id in self._evaluated_candidates:
                raise DuplicateCandidateEvaluation(
                    "gateway-canonical candidate was already evaluated"
                )
            _verify_candidate_artifact(self.layout, _candidate_to_row(candidate))
            candidate_evaluation_id = "candidate-eval-" + uuid.uuid4().hex
            started_at = time.time()
            benchmark_ids: list[tuple[str, str]] = []
            scoped_tasks: list[tuple[str, int, str, str]] = []
            records: list[dict] = []
            attempt = self._attempt_record(candidate.attempt_id)
            selected_benchmark_ids = tuple(
                benchmark_id for benchmark_id, _slots in attempt.benchmark_slots
            )
            for benchmark_id in selected_benchmark_ids:
                slots = attempt.slots_for(benchmark_id)
                benchmark_evaluation_id = "benchmark-eval-" + uuid.uuid4().hex
                benchmark_ids.append((benchmark_id, benchmark_evaluation_id))
                for slot in slots:
                    task_evaluation_id = "task-eval-" + uuid.uuid4().hex
                    records.append(
                        {
                            "schema": EVALUATION_SCHEMA,
                            "lifecycle": "evaluation_started",
                            "evaluation_kind": "task",
                            "run_id": self.layout.run_id,
                            "attempt_id": candidate.attempt_id,
                            "candidate_evaluation_id": candidate_evaluation_id,
                            "benchmark_evaluation_id": benchmark_evaluation_id,
                            "parent_evaluation_id": benchmark_evaluation_id,
                            "benchmark_id": benchmark_id,
                            "evaluation_id": task_evaluation_id,
                            "candidate_id": candidate.candidate_id,
                            "task_id": slot.task_id,
                            "sample_index": slot.sample_index,
                            "replicate": slot.replicate,
                            "measurement_seed": slot.measurement_seed,
                            "operation": self.operation,
                            "candidate_snapshot_digest": candidate.snapshot_digest,
                            "evaluator_digest": self.evaluator_digest,
                            "outcome": None,
                            "score": None,
                            "started_at": started_at,
                            "ended_at": None,
                        }
                    )
                    scoped_tasks.append(
                        (
                            benchmark_id,
                            slot,
                            task_evaluation_id,
                        )
                    )
            if not records:
                raise RunValidationError(
                    "campaign evaluation selected no task children"
                )
            self._writers["evaluations.jsonl"].append_many(records)
            for row in records:
                self._started[str(row["evaluation_id"])] = row
            self._candidate_evaluation_candidates[candidate_evaluation_id] = (
                candidate.candidate_id
            )
            self._evaluated_candidates.add(candidate.candidate_id)
            self.event(
                "candidate_evaluation_started",
                attempt_id=candidate.attempt_id,
                candidate_id=candidate.candidate_id,
                iteration=candidate.iteration,
                candidate_evaluation_id=candidate_evaluation_id,
                snapshot_digest=candidate.snapshot_digest,
                evaluator_digest=self.evaluator_digest,
                benchmark_ids=list(selected_benchmark_ids),
                status="started",
            )
            return EvaluationHandle(
                candidate=candidate,
                evaluator_digest=self.evaluator_digest,
                operation=self.operation,
                started_at=started_at,
                candidate_evaluation_id=candidate_evaluation_id,
                benchmark_evaluation_ids=tuple(benchmark_ids),
                scoped_task_evaluation_ids=tuple(scoped_tasks),
                attempt_id=candidate.attempt_id,
            )

    def resume_campaign_evaluation(
        self, candidate: CandidateRecord
    ) -> Optional[EvaluationHandle]:
        """Read the durable campaign handle already bound to ``candidate``."""

        with self._lock:
            known = self._candidates.get(candidate.candidate_id)
            if known != candidate:
                raise RunValidationError("evaluation candidate is not recorder-owned")
            candidate_ids = {
                str(row.get("candidate_evaluation_id"))
                for row in self._started.values()
                if row.get("candidate_id") == candidate.candidate_id
                and row.get("candidate_evaluation_id")
            }
            if not candidate_ids:
                return None
            if len(candidate_ids) != 1:
                raise RunValidationError("candidate owns multiple campaign evaluations")
            candidate_evaluation_id = candidate_ids.pop()
            rows = [
                row
                for row in self._started.values()
                if row.get("candidate_evaluation_id") == candidate_evaluation_id
            ]
            benchmark_ids: list[tuple[str, str]] = []
            scoped_tasks: list[tuple[str, int, str, str]] = []
            attempt = self._attempt_record(candidate.attempt_id)
            selected_benchmark_ids = tuple(
                benchmark_id for benchmark_id, _slots in attempt.benchmark_slots
            )
            for benchmark_id in selected_benchmark_ids:
                children = [
                    row for row in rows if row.get("benchmark_id") == benchmark_id
                ]
                child_ids = {
                    str(row.get("benchmark_evaluation_id", "")) for row in children
                }
                if len(child_ids) != 1:
                    raise RunValidationError("resumed benchmark parent is invalid")
                benchmark_ids.append((benchmark_id, child_ids.pop()))
                for row in sorted(children, key=lambda item: int(item["sample_index"])):
                    scoped_tasks.append(
                        (
                            benchmark_id,
                            SampleSlot(
                                sample_index=row["sample_index"],
                                task_id=row["task_id"],
                                replicate=row["replicate"],
                                measurement_seed=row["measurement_seed"],
                            ),
                            str(row["evaluation_id"]),
                        )
                    )
            started_times = {float(row["started_at"]) for row in rows}
            if len(started_times) != 1:
                raise RunValidationError(
                    "resumed candidate children changed start time"
                )
            return EvaluationHandle(
                candidate=candidate,
                evaluator_digest=self.evaluator_digest,
                operation=self.operation,
                started_at=started_times.pop(),
                candidate_evaluation_id=candidate_evaluation_id,
                benchmark_evaluation_ids=tuple(benchmark_ids),
                scoped_task_evaluation_ids=tuple(scoped_tasks),
                attempt_id=candidate.attempt_id,
            )

    def resume_or_start_campaign_evaluation(
        self,
        candidate: CandidateRecord,
    ) -> EvaluationHandle:
        """Resume the durable draw/children, or consume one new candidate slot."""

        with self._lock:
            existing = self.resume_campaign_evaluation(candidate)
            if existing is not None:
                return existing
            return self.start_campaign_evaluation(candidate)

    def campaign_occurrence_state(
        self, native_candidate_id: str
    ) -> Optional[CampaignOccurrenceState]:
        """Recover one known Git occurrence without enumerating run history."""

        if not isinstance(native_candidate_id, str) or not native_candidate_id:
            raise ValueError("native_candidate_id cannot be empty")
        with self._lock:
            matches = self._native_candidate_occurrences(
                native_candidate_id=native_candidate_id,
            )
            if not matches:
                return None
            if len(matches) != 1:
                raise RunValidationError("native Git candidate identity is not unique")
            return self.campaign_candidate_state(
                matches[0], occurrence_key=native_candidate_id
            )

    def campaign_terminal_results(self) -> tuple[CampaignEvaluationResult, ...]:
        """Rebuild legal campaign terminals from the recorder's ownership index."""

        with self._lock:
            results: list[CampaignEvaluationResult] = []
            for (
                candidate_evaluation_id,
                candidate_id,
            ) in self._candidate_evaluation_candidates.items():
                candidate = self._candidates.get(candidate_id)
                if candidate is None:
                    raise RunValidationError(
                        "campaign evaluation candidate is not recorder-owned"
                    )
                state = self.campaign_candidate_state(
                    candidate, occurrence_key=candidate_id
                )
                if state.candidate_evaluation_id != candidate_evaluation_id:
                    raise RunValidationError(
                        "campaign evaluation ownership index is inconsistent"
                    )
                if state.terminal_result is not None:
                    results.append(state.terminal_result)
            return tuple(results)

    @property
    def delivered_terminal_ids(self) -> frozenset[str]:
        """Return terminal IDs whose canonical result has one durable receipt."""

        with self._lock:
            return frozenset(self._delivery_receipts)

    def record_delivery_receipt(self, result: CampaignEvaluationResult) -> None:
        """Append one minimal, idempotent delivery receipt for a terminal result."""

        terminal_id = result.candidate_evaluation_id
        with self._lock:
            candidate = self._candidates.get(result.candidate_id)
            if candidate is None:
                raise RunValidationError(
                    "delivery receipt references unknown candidate"
                )
            state = self.campaign_candidate_state(
                candidate,
                occurrence_key=candidate.native_candidate_id or candidate.candidate_id,
            )
            if state.terminal_result != result:
                raise RunValidationError("delivery receipt result is not canonical")
            result_path = self.layout.root / result.result_ref
            if not result_path.is_file():
                raise RunValidationError("delivery receipt result is unavailable")
            result_digest = file_digest(result_path)
            existing = self._delivery_receipts.get(terminal_id)
            if existing is not None:
                if (
                    existing.get("result_digest") != result_digest
                    or existing.get("delivered") is not True
                ):
                    raise RunValidationError("delivery receipt digest changed")
                return
            payload = {
                "schema": EVENT_SCHEMA,
                "event_id": "event-" + uuid.uuid4().hex,
                "timestamp": time.time(),
                "run_id": self.layout.run_id,
                "event_type": "delivery_receipt",
                "terminal_id": terminal_id,
                "result_digest": result_digest,
                "delivered": True,
            }
            _validate_evaluation_boundary(payload)
            self._writers["events.jsonl"].append(payload)
            self._delivery_receipts[terminal_id] = payload

    def _assert_benchmark_result_matches_durable_tasks(
        self, result: BenchmarkEvaluationResult
    ) -> None:
        """Bind one decoded benchmark aggregate to its authoritative leaves."""

        starts = {
            evaluation_id: row
            for evaluation_id, row in self._started.items()
            if row.get("benchmark_evaluation_id") == result.benchmark_evaluation_id
        }
        outcome_ids = tuple(
            str(outcome.get("evaluation_id", "")) for outcome in result.task_outcomes
        )
        if (
            not starts
            or len(set(outcome_ids)) != len(outcome_ids)
            or set(outcome_ids) != set(starts)
            or set(outcome_ids) - set(self._terminal_rows)
        ):
            raise RunValidationError(
                "benchmark result does not cover its durable task terminals"
            )
        ordered_starts = tuple(
            sorted(
                starts.values(),
                key=lambda row: int(row.get("sample_index", -1)),
            )
        )
        durable_slots = tuple(
            SampleSlot(
                sample_index=row["sample_index"],
                task_id=row["task_id"],
                replicate=row["replicate"],
                measurement_seed=row["measurement_seed"],
            )
            for row in ordered_starts
        )
        if result.sample_slots != durable_slots:
            raise RunValidationError(
                "benchmark result sample slots disagree with durable tasks"
            )
        for slot, outcome in zip(
            result.sample_slots, result.task_outcomes, strict=True
        ):
            evaluation_id = str(outcome.get("evaluation_id", ""))
            started = starts[evaluation_id]
            terminal = self._terminal_rows[evaluation_id]
            if (
                started.get("run_id") != result.run_id
                or started.get("attempt_id") != result.attempt_id
                or started.get("evaluator_digest") != result.evaluator_digest
                or started.get("candidate_id") != result.candidate_id
                or started.get("candidate_snapshot_digest")
                != result.candidate_snapshot_digest
                or started.get("sample_index") != slot.sample_index
                or started.get("task_id") != slot.task_id
                or started.get("replicate") != slot.replicate
                or started.get("measurement_seed") != slot.measurement_seed
            ):
                raise RunValidationError("benchmark result task identity is misbound")
            if (
                started.get("benchmark_id") != result.benchmark_id
                or started.get("candidate_evaluation_id")
                != result.candidate_evaluation_id
                or started.get("benchmark_evaluation_id")
                != result.benchmark_evaluation_id
            ):
                raise RunValidationError(
                    "benchmark result campaign task identity is misbound"
                )
            expected_outcome = {
                "evaluation_id": evaluation_id,
                **slot.as_dict(),
                "outcome": terminal.get("outcome"),
                "score": terminal.get("score"),
                "summary": terminal.get("summary"),
                "artifact_refs": dict(terminal.get("artifact_refs") or {}),
                "provenance": dict(terminal.get("provenance") or {}),
                "constraints_pass": terminal.get("constraints_pass"),
            }
            if any(
                outcome.get(field_name) != expected
                for field_name, expected in expected_outcome.items()
            ):
                raise RunValidationError(
                    "benchmark result disagrees with durable task terminal"
                )

    def campaign_candidate_state(
        self,
        candidate: CandidateRecord,
        *,
        occurrence_key: str,
    ) -> CampaignOccurrenceState:
        """Recover one recorder-owned campaign occurrence of any artifact kind."""

        if not isinstance(occurrence_key, str) or not occurrence_key:
            raise ValueError("occurrence_key cannot be empty")
        with self._lock:
            recorded = self._candidates.get(candidate.candidate_id)
            if recorded != candidate:
                raise RunValidationError("campaign candidate is not recorder-owned")
            handle = self.resume_campaign_evaluation(candidate)
            if handle is None:
                return CampaignOccurrenceState(
                    occurrence_key=occurrence_key,
                    candidate_id=candidate.candidate_id,
                    candidate_evaluation_id=None,
                    terminal_result=None,
                    attempt_id=candidate.attempt_id,
                    role=candidate.role,
                )
            terminals = [
                row
                for row in read_jsonl(self.layout.root / "events.jsonl")
                if row.get("event_type") == "candidate_evaluation_terminal"
                and row.get("candidate_evaluation_id") == handle.candidate_evaluation_id
            ]
            if len(terminals) > 1:
                raise RunValidationError("candidate evaluation has duplicate terminals")
            if not terminals:
                return CampaignOccurrenceState(
                    occurrence_key=occurrence_key,
                    candidate_id=candidate.candidate_id,
                    candidate_evaluation_id=handle.candidate_evaluation_id,
                    terminal_result=None,
                    attempt_id=candidate.attempt_id,
                    role=candidate.role,
                )
            terminal_state = str(terminals[0].get("state"))
            terminal_outcome = str(terminals[0].get("outcome"))
            expected_child_ids = [
                evaluation_id
                for _benchmark_id, evaluation_id in handle.benchmark_evaluation_ids
            ]
            if (
                terminal_state != "TERMINAL"
                or terminal_outcome not in TASK_OUTCOMES
                or terminals[0].get("candidate_id") != candidate.candidate_id
                or terminals[0].get("snapshot_digest") != candidate.snapshot_digest
                or terminals[0].get("evaluator_digest") != self.evaluator_digest
                or terminals[0].get("benchmark_evaluation_ids") != expected_child_ids
            ):
                raise RunValidationError(
                    "candidate evaluation terminal envelope is invalid"
                )
            children: list[BenchmarkEvaluationResult] = []
            for (
                benchmark_id,
                benchmark_evaluation_id,
            ) in handle.benchmark_evaluation_ids:
                path = (
                    self.layout.root
                    / "results"
                    / f"benchmark-{benchmark_evaluation_id}.json"
                )
                try:
                    payload = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError) as exc:
                    raise RunValidationError(
                        "terminal occurrence lacks a readable benchmark result"
                    ) from exc
                if not isinstance(payload, Mapping):
                    raise RunValidationError("benchmark result is malformed")
                child = _benchmark_result_from_payload(payload)
                if (
                    child.run_id != self.layout.run_id
                    or child.evaluator_digest != self.evaluator_digest
                    or child.benchmark_id != benchmark_id
                    or child.benchmark_evaluation_id != benchmark_evaluation_id
                    or child.candidate_evaluation_id != handle.candidate_evaluation_id
                    or child.candidate_id != candidate.candidate_id
                    or child.candidate_snapshot_digest != candidate.snapshot_digest
                    or child.result_ref
                    != (
                        Path("results") / f"benchmark-{benchmark_evaluation_id}.json"
                    ).as_posix()
                ):
                    raise RunValidationError(
                        "terminal occurrence benchmark result is misbound"
                    )
                self._assert_benchmark_result_matches_durable_tasks(child)
                children.append(child)
            path = (
                self.layout.root
                / "results"
                / f"campaign-{handle.candidate_evaluation_id}.json"
            )
            try:
                payload = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise RunValidationError(
                    "terminal occurrence lacks its campaign result"
                ) from exc
            if not isinstance(payload, Mapping):
                raise RunValidationError("campaign result is malformed")
            result = _campaign_result_from_payload(payload)
            if (
                result.run_id != self.layout.run_id
                or result.attempt_id != candidate.attempt_id
                or result.evaluator_digest != self.evaluator_digest
                or result.candidate_evaluation_id != handle.candidate_evaluation_id
                or result.candidate_id != candidate.candidate_id
                or result.candidate_role != candidate.role
                or result.candidate_snapshot_digest != candidate.snapshot_digest
                or result.benchmark_results != tuple(children)
                or result.outcome != terminal_outcome
                or result.aggregate_policy != self._campaign_aggregate_payload()
                or result.aggregate_score != self._authorized_campaign_score(children)
                or result.aggregate_score != terminals[0].get("aggregate_score")
                or result.result_ref
                != (
                    Path("results") / f"campaign-{handle.candidate_evaluation_id}.json"
                ).as_posix()
            ):
                raise RunValidationError(
                    "terminal occurrence campaign result is misbound"
                )
            return CampaignOccurrenceState(
                occurrence_key=occurrence_key,
                candidate_id=candidate.candidate_id,
                candidate_evaluation_id=handle.candidate_evaluation_id,
                terminal_result=result,
                attempt_id=candidate.attempt_id,
                role=candidate.role,
            )

    def missing_campaign_tasks(
        self, handle: EvaluationHandle, benchmark_id: str
    ) -> tuple[tuple[SampleSlot, str], ...]:
        """Return only unfinished leaf children in canonical sample order."""

        return tuple(
            (slot, evaluation_id)
            for child_benchmark, slot, evaluation_id in handle.scoped_task_evaluation_ids
            if child_benchmark == benchmark_id and evaluation_id not in self._terminal
        )

    def finish_task_evaluation(
        self,
        handle: EvaluationHandle,
        benchmark_id: str,
        slot: SampleSlot,
        outcome: TaskEvaluationOutcome,
    ) -> None:
        """Durably finish one leaf as soon as its worker returns."""

        evaluation_id = handle.task_evaluation_id(benchmark_id, slot.sample_index)
        with self._lock:
            if evaluation_id in self._terminal:
                return
            started = self._started.get(evaluation_id)
            if started is None:
                raise RunValidationError("task evaluation start is unavailable")
            expected_slot = SampleSlot(
                sample_index=started["sample_index"],
                task_id=started["task_id"],
                replicate=started["replicate"],
                measurement_seed=started["measurement_seed"],
            )
            if slot != expected_slot:
                raise RunValidationError("task evaluation slot changed")
            row = {
                **started,
                "lifecycle": "evaluation_terminal",
                "outcome": outcome.outcome,
                "score": outcome.score,
                "ended_at": time.time(),
                "wall_seconds": outcome.wall_seconds,
                "summary": outcome.summary,
                "artifact_refs": dict(outcome.artifact_refs),
                "provenance": dict(outcome.provenance),
                "constraints_pass": outcome.constraints_pass,
                "usage": {
                    "input_tokens": outcome.input_tokens,
                    "output_tokens": outcome.output_tokens,
                    "cache_tokens": outcome.cache_tokens,
                    "token_status": outcome.token_status,
                    "input_token_status": outcome.input_token_status,
                    "output_token_status": outcome.output_token_status,
                    "cache_token_status": outcome.cache_token_status,
                    "cost_usd": outcome.cost_usd,
                    "cost_status": outcome.cost_status,
                },
            }
            self._writers["evaluations.jsonl"].append(row)
            self._terminal.add(evaluation_id)
            self._terminal_rows[evaluation_id] = row

    def task_evaluation_outcome(
        self,
        handle: EvaluationHandle,
        benchmark_id: str,
        slot: SampleSlot,
    ) -> TaskEvaluationOutcome | None:
        """Decode one durable leaf terminal for usage reconciliation."""

        evaluation_id = handle.task_evaluation_id(benchmark_id, slot.sample_index)
        with self._lock:
            row = self._terminal_rows.get(evaluation_id)
            if row is None:
                return None
            raw_usage = row.get("usage")
            if not isinstance(raw_usage, Mapping) or set(raw_usage) != {
                "input_tokens",
                "output_tokens",
                "cache_tokens",
                "token_status",
                "input_token_status",
                "output_token_status",
                "cache_token_status",
                "cost_usd",
                "cost_status",
            }:
                raise RunValidationError("task terminal usage authority is invalid")
            try:
                return TaskEvaluationOutcome(
                    outcome=str(row["outcome"]),
                    score=row.get("score"),
                    wall_seconds=row.get("wall_seconds"),
                    summary=row.get("summary"),
                    artifact_refs=MappingProxyType(
                        dict(row.get("artifact_refs") or {})
                    ),
                    provenance=MappingProxyType(dict(row.get("provenance") or {})),
                    input_tokens=raw_usage.get("input_tokens"),
                    output_tokens=raw_usage.get("output_tokens"),
                    cache_tokens=raw_usage.get("cache_tokens"),
                    token_status=str(raw_usage.get("token_status")),
                    input_token_status=str(raw_usage.get("input_token_status")),
                    output_token_status=str(raw_usage.get("output_token_status")),
                    cache_token_status=str(raw_usage.get("cache_token_status")),
                    cost_usd=raw_usage.get("cost_usd"),
                    cost_status=str(raw_usage.get("cost_status")),
                    constraints_pass=row.get("constraints_pass"),
                )
            except (TypeError, ValueError) as exc:
                raise RunValidationError(
                    "task terminal usage authority is invalid"
                ) from exc

    def finish_benchmark_evaluation(
        self,
        handle: EvaluationHandle,
        benchmark_id: str,
    ) -> BenchmarkEvaluationResult:
        """Derive one benchmark result after every leaf is durable."""

        benchmark_evaluation_id = handle.benchmark_evaluation_id(benchmark_id)
        with self._lock:
            if self.missing_campaign_tasks(handle, benchmark_id):
                raise RunValidationError("benchmark evaluation remains incomplete")
            terminals_by_id = {
                str(row["evaluation_id"]): row
                for row in read_jsonl(self.layout.root / "evaluations.jsonl")
                if row.get("lifecycle") == "evaluation_terminal"
                and row.get("benchmark_evaluation_id") == benchmark_evaluation_id
            }
            ordered: list[dict[str, object]] = []
            slots: list[SampleSlot] = []
            for (
                child_benchmark,
                slot,
                evaluation_id,
            ) in handle.scoped_task_evaluation_ids:
                if child_benchmark != benchmark_id:
                    continue
                terminal = terminals_by_id.get(evaluation_id)
                if terminal is None:
                    raise RunValidationError("benchmark result lacks a task terminal")
                slots.append(slot)
                ordered.append(
                    {
                        "evaluation_id": evaluation_id,
                        **slot.as_dict(),
                        "outcome": terminal.get("outcome"),
                        "score": terminal.get("score"),
                        "summary": terminal.get("summary"),
                        "artifact_refs": dict(terminal.get("artifact_refs") or {}),
                        "provenance": dict(terminal.get("provenance") or {}),
                        "constraints_pass": terminal.get("constraints_pass"),
                    }
                )
            conclusive = [item for item in ordered if item["outcome"] == "measured"]
            n_expected = len(slots)
            n_conclusive = len(conclusive)
            n_inconclusive = n_expected - n_conclusive
            score = (
                sum(float(item["score"]) for item in conclusive) / n_conclusive
                if n_conclusive
                else None
            )
            result_ref = (
                Path("results") / f"benchmark-{benchmark_evaluation_id}.json"
            ).as_posix()
            result = BenchmarkEvaluationResult(
                run_id=self.layout.run_id,
                attempt_id=handle.attempt_id,
                evaluator_digest=self.evaluator_digest,
                benchmark_id=benchmark_id,
                candidate_evaluation_id=handle.candidate_evaluation_id,
                benchmark_evaluation_id=benchmark_evaluation_id,
                candidate_id=handle.candidate.candidate_id,
                candidate_snapshot_digest=handle.candidate.snapshot_digest,
                sample_slots=tuple(slots),
                task_outcomes=tuple(ordered),
                outcome="measured" if n_conclusive else "inconclusive",
                n_expected=n_expected,
                n_conclusive=n_conclusive,
                n_inconclusive=n_inconclusive,
                score=score,
                coverage=n_conclusive / n_expected,
                result_ref=result_ref,
            )
            path = self.layout.root / result_ref
            payload = result.as_dict()
            if path.exists():
                if json.loads(path.read_text()) != payload:
                    raise RunValidationError("benchmark result changed during resume")
            else:
                self.layout.write_result(path.name, payload)
            return result

    def finish_campaign_evaluation(
        self,
        handle: EvaluationHandle,
        benchmark_results: Mapping[str, BenchmarkEvaluationResult],
    ) -> CampaignEvaluationResult:
        """Persist the complete vector and only an explicitly authorized scalar."""

        expected_benchmark_ids = tuple(
            benchmark_id
            for benchmark_id, _evaluation_id in handle.benchmark_evaluation_ids
        )
        attempt = self._attempt_record(handle.attempt_id)
        durable_benchmark_ids = tuple(
            benchmark_id for benchmark_id, _slots in attempt.benchmark_slots
        )
        if (
            not expected_benchmark_ids
            or expected_benchmark_ids != durable_benchmark_ids
            or tuple(benchmark_results) != expected_benchmark_ids
        ):
            raise RunValidationError(
                "campaign result must contain its durable benchmark subset in order"
            )
        ordered = tuple(benchmark_results[key] for key in expected_benchmark_ids)
        for benchmark_id, result in zip(expected_benchmark_ids, ordered, strict=True):
            if (
                result.run_id != self.layout.run_id
                or result.attempt_id != handle.attempt_id
                or result.evaluator_digest != self.evaluator_digest
                or result.benchmark_id != benchmark_id
                or result.candidate_evaluation_id != handle.candidate_evaluation_id
                or result.benchmark_evaluation_id
                != handle.benchmark_evaluation_id(benchmark_id)
                or result.candidate_id != handle.candidate.candidate_id
                or result.candidate_snapshot_digest != handle.candidate.snapshot_digest
                or result.result_ref
                != (
                    Path("results") / f"benchmark-{result.benchmark_evaluation_id}.json"
                ).as_posix()
            ):
                raise RunValidationError("campaign benchmark result is misbound")
            result_path = self.layout.root / result.result_ref
            if (
                not result_path.is_file()
                or json.loads(result_path.read_text()) != result.as_dict()
            ):
                raise RunValidationError(
                    "campaign benchmark result is not its canonical child artifact"
                )
        score_vector = tuple((result.benchmark_id, result.score) for result in ordered)
        measured_scores = [
            float(score) for _benchmark_id, score in score_vector if score is not None
        ]
        aggregate_score = self._authorized_campaign_score(ordered)
        n_expected = sum(child.n_expected for child in ordered)
        n_conclusive = sum(child.n_conclusive for child in ordered)
        n_inconclusive = sum(child.n_inconclusive for child in ordered)
        result_ref = (
            Path("results") / f"campaign-{handle.candidate_evaluation_id}.json"
        ).as_posix()
        result = CampaignEvaluationResult(
            run_id=self.layout.run_id,
            attempt_id=handle.attempt_id,
            evaluator_digest=self.evaluator_digest,
            candidate_evaluation_id=handle.candidate_evaluation_id,
            candidate_id=handle.candidate.candidate_id,
            candidate_role=handle.candidate.role,
            candidate_snapshot_digest=handle.candidate.snapshot_digest,
            benchmark_results=ordered,
            outcome="measured" if measured_scores else "inconclusive",
            score_vector=score_vector,
            aggregate_policy=self._campaign_aggregate_payload(),
            aggregate_score=aggregate_score,
            n_expected=n_expected,
            n_conclusive=n_conclusive,
            n_inconclusive=n_inconclusive,
            coverage=n_conclusive / n_expected,
            result_ref=result_ref,
        )
        path = self.layout.root / result_ref
        payload = result.as_dict()
        if path.exists():
            if json.loads(path.read_text()) != payload:
                raise RunValidationError("campaign result changed during resume")
        else:
            self.layout.write_result(path.name, payload)
        self._finish_candidate_evaluation(handle, result=result)
        return result

    def _finish_candidate_evaluation(
        self,
        handle: EvaluationHandle,
        *,
        result: CampaignEvaluationResult,
    ) -> None:
        """Append the single durable terminal for one campaign candidate slot."""

        if result.candidate_evaluation_id != handle.candidate_evaluation_id:
            raise RunValidationError("candidate terminal result is misbound")
        task_ids = {
            evaluation_id
            for (
                _benchmark_id,
                _slot,
                evaluation_id,
            ) in handle.scoped_task_evaluation_ids
        }
        if not task_ids or not task_ids.issubset(self._terminal):
            raise RunValidationError(
                "candidate evaluation cannot terminate before every task child"
            )
        expected: dict[str, object] = {
            "attempt_id": handle.attempt_id,
            "candidate_id": handle.candidate.candidate_id,
            "candidate_evaluation_id": handle.candidate_evaluation_id,
            "snapshot_digest": handle.candidate.snapshot_digest,
            "evaluator_digest": self.evaluator_digest,
            "benchmark_evaluation_ids": [
                evaluation_id
                for _benchmark_id, evaluation_id in handle.benchmark_evaluation_ids
            ],
            "state": "TERMINAL",
            "outcome": result.outcome,
            "aggregate_score": result.aggregate_score,
        }
        existing = [
            row
            for row in read_jsonl(self.layout.root / "events.jsonl")
            if row.get("event_type") == "candidate_evaluation_terminal"
            and row.get("candidate_evaluation_id") == handle.candidate_evaluation_id
        ]
        if len(existing) > 1:
            raise RunValidationError("candidate evaluation has duplicate terminals")
        if existing:
            if any(existing[0].get(key) != value for key, value in expected.items()):
                raise RunValidationError("candidate evaluation terminal changed")
            return
        self.event("candidate_evaluation_terminal", **expected)
        self._maybe_terminal_attempt(handle.attempt_id)

    def event(self, event_type: str, **fields: object) -> str:
        if event_type == "delivery_receipt":
            raise RunValidationError(
                "delivery receipts require the canonical result writer"
            )
        if str(event_type).lower() in _HELD_OUT_EVENT_TYPES:
            raise RunValidationError("evaluation recorder cannot emit a held-out event")
        with self._lock:
            candidate_id = fields.get("candidate_id")
            if candidate_id is not None:
                candidate = self._candidates.get(str(candidate_id))
                if candidate is None:
                    raise RunValidationError("event references unknown candidate")
                supplied = fields.get("attempt_id")
                if supplied is not None and supplied != candidate.attempt_id:
                    raise RunValidationError("event attempt binding mismatch")
                fields["attempt_id"] = candidate.attempt_id
            elif fields.get("attempt_id") is not None:
                if str(fields["attempt_id"]) not in self._attempt_started:
                    raise RunValidationError("event references unknown attempt")
            payload = {
                "schema": EVENT_SCHEMA,
                "event_id": "event-" + uuid.uuid4().hex,
                "timestamp": time.time(),
                "run_id": self.layout.run_id,
                "method": self.layout.method,
                "benchmark": self.layout.benchmark,
                "event_type": str(event_type),
                **fields,
            }
            _validate_evaluation_boundary(payload)
            self._writers["events.jsonl"].append(payload)
            self._events.append(dict(payload))
            return str(payload["event_id"])

    def task_evaluation_attempt_events(
        self, task_evaluation_id: str
    ) -> tuple[Mapping[str, object], ...]:
        """Read retry receipts under the same lock used by append+fsync."""

        with self._lock:
            if task_evaluation_id not in self._started:
                raise RunValidationError("task retry names an unknown evaluation")
            return tuple(
                MappingProxyType(dict(row))
                for row in self._events
                if row.get("event_type")
                in {
                    "task_evaluation_attempt_started",
                    "task_evaluation_attempt_terminal",
                }
                and row.get("task_evaluation_id") == task_evaluation_id
            )


@dataclass(frozen=True)
class CanonicalRunReplay:
    """Immutable projection of the recorder's canonical, read-only replay."""

    layout: RunLayout
    manifest: Mapping[str, object]
    evaluator_digest: str
    benchmark_task_ids: Mapping[str, tuple[str, ...]]
    attempts: tuple[AttemptRecord, ...]
    candidates: tuple[CandidateRecord, ...]
    events: tuple[Mapping[str, object], ...]
    usage: tuple[Mapping[str, object], ...]
    terminal_results: tuple[CampaignEvaluationResult, ...]
    delivered_terminal_ids: frozenset[str]

    @property
    def delivered_results(self) -> tuple[CampaignEvaluationResult, ...]:
        return tuple(
            result
            for result in self.terminal_results
            if result.candidate_evaluation_id in self.delivered_terminal_ids
        )

    def candidate_by_id(self, candidate_id: str) -> CandidateRecord:
        matches = tuple(
            candidate
            for candidate in self.candidates
            if candidate.candidate_id == candidate_id
        )
        if len(matches) != 1:
            raise RunValidationError("replay has no unique canonical candidate")
        return matches[0]

    def attempt_by_id(self, attempt_id: str) -> AttemptRecord:
        matches = tuple(
            attempt for attempt in self.attempts if attempt.attempt_id == attempt_id
        )
        if len(matches) != 1:
            raise RunValidationError("replay has no unique canonical attempt")
        return matches[0]


def replay_run(layout: RunLayout) -> CanonicalRunReplay:
    """Replay one run through recorder validation without opening any writer.

    The manifest is the sole source of evaluator and development-task bindings.
    ``RunRecorder`` executes this same constructor path for writable resume; the
    private read-only switch only suppresses writer acquisition and terminal
    attempt repair.
    """

    manifest = _load_run_manifest(layout.root)
    freeze = _campaign_run_freeze_from_manifest(manifest)
    benchmark_ids = freeze["benchmark_ids"]
    raw_bindings = freeze["benchmarks"]
    assert isinstance(benchmark_ids, list)
    assert isinstance(raw_bindings, Mapping)
    benchmark_task_ids: dict[str, tuple[str, ...]] = {}
    for benchmark_id in benchmark_ids:
        binding = raw_bindings[benchmark_id]
        assert isinstance(binding, Mapping)
        benchmark_task_ids[benchmark_id] = _development_task_ids_from_boundary(
            binding["dataset_boundary"]
        )
    recorder = RunRecorder(
        layout,
        evaluator_digest=str(manifest["evaluator_digest"]),
        benchmark_task_ids=benchmark_task_ids,
        operation="attempt-evaluation",
        _read_only=True,
    )
    terminal_results = recorder.campaign_terminal_results()
    terminal_ids = {result.candidate_evaluation_id for result in terminal_results}
    if not recorder.delivered_terminal_ids.issubset(terminal_ids):
        raise RunValidationError("delivery receipt has no canonical campaign result")
    return CanonicalRunReplay(
        layout=layout,
        manifest=MappingProxyType(dict(recorder._manifest)),
        evaluator_digest=recorder.evaluator_digest,
        benchmark_task_ids=MappingProxyType(dict(recorder.benchmark_task_ids)),
        attempts=tuple(
            recorder._attempt_record(attempt_id)
            for attempt_id in recorder._attempt_started
        ),
        candidates=tuple(recorder._candidates.values()),
        events=tuple(MappingProxyType(dict(row)) for row in recorder._events),
        usage=tuple(MappingProxyType(dict(row)) for row in recorder._usage_rows),
        terminal_results=terminal_results,
        delivered_terminal_ids=recorder.delivered_terminal_ids,
    )


def _candidate_id(run_id: str, snapshot_digest: str) -> str:
    payload = f"{run_id}\0git_bundle\0{snapshot_digest}".encode()
    return "cand-" + hashlib.sha256(payload).hexdigest()[:32]


def _occurrence_candidate_id(run_id: str, ordinal: int) -> str:
    payload = f"{run_id}\0occurrence\0{ordinal}".encode()
    return f"cand-occ-{ordinal:08d}-" + hashlib.sha256(payload).hexdigest()[:16]


def _candidate_from_row(row: Mapping[str, object]) -> CandidateRecord:
    artifact_kind = str(row["artifact_kind"])
    if artifact_kind != "git_bundle":
        raise RunValidationError("candidate artifact kind must be git_bundle")
    gateway_commit_sha = str(row["gateway_commit_sha"])
    gateway_root_commit_sha = str(row["gateway_root_commit_sha"])
    artifact_commit_sha = str(row["artifact_commit_sha"])
    raw_parent_candidate_id = row.get("parent_candidate_id")
    if raw_parent_candidate_id is not None and (
        not isinstance(raw_parent_candidate_id, str) or not raw_parent_candidate_id
    ):
        raise RunValidationError("candidate parent binding is malformed")
    return CandidateRecord(
        candidate_id=str(row["candidate_id"]),
        native_candidate_id=(
            str(row["native_candidate_id"])
            if row.get("native_candidate_id") is not None
            else None
        ),
        artifact_kind=artifact_kind,
        snapshot_digest=str(row["snapshot_digest"]),
        artifact_digest=str(row["artifact_digest"]),
        store_ref=str(row["store_ref"]),
        iteration=int(row["iteration"]),
        parent_candidate_id=raw_parent_candidate_id,
        tree_sha=(str(row["tree_sha"]) if row.get("tree_sha") else None),
        pinned_ref=(str(row["pinned_ref"]) if row.get("pinned_ref") else None),
        gateway_commit_sha=gateway_commit_sha,
        gateway_root_commit_sha=gateway_root_commit_sha,
        artifact_commit_sha=artifact_commit_sha,
        attempt_id=str(row["attempt_id"]),
        role=str(row["role"]),
    )


def _candidate_to_row(candidate: CandidateRecord) -> dict:
    row = {
        "candidate_id": candidate.candidate_id,
        "attempt_id": candidate.attempt_id,
        "role": candidate.role,
        "native_candidate_id": candidate.native_candidate_id,
        "artifact_kind": candidate.artifact_kind,
        "snapshot_digest": candidate.snapshot_digest,
        "artifact_digest": candidate.artifact_digest,
        "store_ref": candidate.store_ref,
        "iteration": candidate.iteration,
        "parent_candidate_id": candidate.parent_candidate_id,
        "tree_sha": candidate.tree_sha,
        "pinned_ref": candidate.pinned_ref,
    }
    for field_name in (
        "gateway_commit_sha",
        "gateway_root_commit_sha",
        "artifact_commit_sha",
    ):
        value = getattr(candidate, field_name)
        if value is not None:
            row[field_name] = value
    return row


def _git_output(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    ).stdout.strip()


def _git_env() -> dict[str, str]:
    """Return a trusted Git environment without provider credentials."""

    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/dev/null",
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }


_ARTIFACT_COMMIT_MARKER = "rsi-arena durable candidate artifact"


def _artifact_commit_message(
    tree_sha: str, gateway_commit_sha: str, gateway_root_commit_sha: str
) -> str:
    return (
        f"{_ARTIFACT_COMMIT_MARKER}\n\n"
        f"gateway-commit: {gateway_commit_sha}\n"
        f"gateway-root-commit: {gateway_root_commit_sha}\n"
        f"tree: {tree_sha}"
    )


def _parentless_artifact_commit(
    repo: Path,
    tree_sha: str,
    *,
    gateway_commit_sha: str,
    gateway_root_commit_sha: str,
) -> str:
    """Create a deterministic, parentless commit for one admitted tree."""

    env = {
        **_git_env(),
        "GIT_AUTHOR_NAME": "rsi-arena-durable-recorder",
        "GIT_AUTHOR_EMAIL": "rsi-arena-durable-recorder@localhost",
        "GIT_COMMITTER_NAME": "rsi-arena-durable-recorder",
        "GIT_COMMITTER_EMAIL": "rsi-arena-durable-recorder@localhost",
        "GIT_AUTHOR_DATE": "1970-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "1970-01-01T00:00:00+00:00",
    }
    created = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "commit-tree",
            tree_sha,
            "-m",
            _artifact_commit_message(
                tree_sha, gateway_commit_sha, gateway_root_commit_sha
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    if _git_output(repo, "rev-parse", f"{created}^{{tree}}") != tree_sha:
        raise RunValidationError("artifact commit tree identity mismatch")
    if _git_output(repo, "rev-list", "--parents", "-n", "1", created).split() != [
        created
    ]:
        raise RunValidationError("artifact commit must be parentless")
    return created


def _verify_git_bundle(
    bundle: Path,
    pinned_ref: str,
    commit_sha: str,
    tree_sha: str,
    *,
    require_parentless: bool = False,
    gateway_commit_sha: str | None = None,
    gateway_root_commit_sha: str | None = None,
) -> None:
    with tempfile.TemporaryDirectory(prefix="rsi-arena-bundle-verify-") as raw:
        repository = Path(raw) / "verify.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", str(repository)],
            check=True,
            env=_git_env(),
        )
        verified = subprocess.run(
            ["git", "-C", str(repository), "bundle", "verify", str(bundle)],
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        if verified.returncode != 0:
            raise RunValidationError("candidate Git bundle failed verification")
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "fetch",
                "--quiet",
                str(bundle),
                f"{pinned_ref}:refs/verify/candidate",
            ],
            check=True,
            env=_git_env(),
        )
        observed_commit = _git_output(
            repository, "rev-parse", "refs/verify/candidate^{commit}"
        )
        observed_tree = _git_output(
            repository, "rev-parse", "refs/verify/candidate^{tree}"
        )
        if observed_commit != commit_sha or observed_tree != tree_sha:
            raise RunValidationError("bundle pinned ref does not match commit/tree")
        message = _git_output(repository, "show", "-s", "--format=%B", observed_commit)
        if require_parentless and _git_output(
            repository,
            "rev-list",
            "--parents",
            "-n",
            "1",
            observed_commit,
        ).split() != [observed_commit]:
            raise RunValidationError("bundle artifact commit is not parentless")
        if require_parentless:
            if gateway_commit_sha is None or gateway_root_commit_sha is None:
                raise RunValidationError("bundle artifact provenance is incomplete")
            if message != _artifact_commit_message(
                tree_sha, gateway_commit_sha, gateway_root_commit_sha
            ):
                raise RunValidationError("bundle artifact provenance mismatch")
        elif message.startswith(_ARTIFACT_COMMIT_MARKER):
            raise RunValidationError(
                "durable artifact commit is missing candidate provenance"
            )


def _verify_candidate_artifact(layout: RunLayout, row: Mapping[str, object]) -> None:
    path = layout.resolve_store_ref(str(row["store_ref"]))
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RunValidationError("candidate store_ref is not a regular file")
    if file_digest(path) != str(row["artifact_digest"]):
        raise RunValidationError("candidate artifact digest mismatch")
    kind = row.get("artifact_kind")
    if kind != "git_bundle":
        raise RunValidationError("candidate artifact kind must be git_bundle")
    tree = str(row.get("tree_sha") or "")
    pinned = str(row.get("pinned_ref") or "")
    if row.get("snapshot_digest") != "git-tree-sha1:" + tree:
        raise RunValidationError("Git snapshot/tree digest mismatch")
    if not pinned.startswith("refs/rsi-arena/candidates/"):
        raise RunValidationError("candidate-specific pinned ref is missing")
    try:
        gateway_commit = str(row["gateway_commit_sha"])
        gateway_root = str(row["gateway_root_commit_sha"])
        artifact_commit = str(row["artifact_commit_sha"])
    except KeyError as exc:
        raise RunValidationError("Git candidate provenance is incomplete") from exc
    if not all(
        re.fullmatch(r"[0-9a-f]{40}", value)
        for value in (gateway_commit, gateway_root, artifact_commit, tree)
    ):
        raise RunValidationError("Git candidate provenance is invalid")
    if artifact_commit == gateway_commit:
        raise RunValidationError("artifact commit must be distinct from gateway commit")
    _verify_git_bundle(
        path,
        pinned,
        artifact_commit,
        tree,
        require_parentless=True,
        gateway_commit_sha=gateway_commit,
        gateway_root_commit_sha=gateway_root,
    )


def _validate_evaluation_boundary(value: object) -> None:
    """Keep held-out capability and material outside evaluation artifacts."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered == "held_out_capability" and child is not False:
                raise RunValidationError(
                    "evaluation run cannot carry held-out capability"
                )
            if lowered == "sealed_held_out_reference" and child is not None:
                raise RunValidationError(
                    "evaluation run cannot bind sealed held-out artifacts"
                )
            if lowered == "operation" and str(child).lower() in {
                "held-out",
                "sealed",
                "held-out-evaluation",
            }:
                raise RunValidationError(
                    "held-out operation found in evaluation artifacts"
                )
            _validate_evaluation_boundary(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_evaluation_boundary(child)
