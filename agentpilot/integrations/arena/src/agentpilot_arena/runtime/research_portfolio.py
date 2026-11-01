"""Arena execution adapters for the lightweight research portfolio.

This module executes experiments; it does not decide which research track wins
or close sibling tracks.  Artifact and ticket persistence belongs to the core
portfolio store.  The adapters below keep the expensive boundaries explicit:
pure-Python probes, isolated Git patches, development-A measurements, worker
realization, and frozen admission are separate operations.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from agentpilot.domain import (
    PolicyAction,
    TraceCaseIndex,
    TraceEvidenceManifest,
    canonical_json,
    stable_id,
)
from agentpilot.evaluation import CellMeasurement, EvaluationCell, MeasurementStatus
from agentpilot.search import trace_case_evidence

from .action_connectors import (
    ScopeSecretCandidateValidator,
    ValidatedGitWorkspaceManager,
)
from .git_workspace import GitWorkspace


class PortfolioBackendError(RuntimeError):
    """A requested experiment cannot be represented by this Arena runtime."""


class MeasurementRecoveryRequired(PortfolioBackendError):
    """A cell started without a durable result and must be reconciled explicitly."""


class DevelopmentAccountingRequired(PortfolioBackendError):
    """Completed development cells do not contain settled cost facts."""


class ArtifactResolver(Protocol):
    def __call__(self, artifact_id: str) -> object:
        ...


class WorkerBuilder(Protocol):
    def build(
        self,
        *,
        ticket_id: str,
        base: Mapping[str, Any],
        instruction: str,
        files: tuple[str, ...],
        new_files: tuple[str, ...],
        research_evidence: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def estimate(self, arguments: Mapping[str, Any]) -> float:
        ...


class AdmissionEvaluator(Protocol):
    def admit(
        self, *, ticket_id: str, candidate: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def estimate(self, arguments: Mapping[str, Any]) -> float:
        ...


class ExperimentRunner(Protocol):
    def run(
        self,
        *,
        ticket_id: str,
        base: Mapping[str, Any],
        instruction: str,
        research_evidence: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def recover(
        self,
        *,
        ticket_id: str,
        base: Mapping[str, Any],
        instruction: str,
        research_evidence: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def estimate(self, arguments: Mapping[str, Any]) -> float:
        ...


PrototypeActionFactory = Callable[
    [str, Mapping[str, Any], GitWorkspace, tuple[str, ...]], PolicyAction
]


def _json_mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    try:
        result = json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain finite JSON values") from exc
    if not isinstance(result, dict):
        raise TypeError(f"{name} must be a mapping")
    return result


def _text(value: object, name: str, *, maximum: int = 65_536) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{name} exceeds its UTF-8 byte limit")
    return value


def _identifier(value: object, name: str) -> str:
    text = _text(value, name, maximum=256)
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError(f"{name} contains control characters")
    return text


def _path_list(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{name} must be a list of non-empty paths")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise ValueError(f"{name} contains duplicates")
    return result


def _non_negative_cost(value: object, name: str = "cost_usd") -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{name} must be finite and non-negative")
    return float(value)


def _artifact_snapshot(value: object, expected_id: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        raw = dict(value)
    else:
        raw = {
            name: getattr(value, name)
            for name in (
                "id",
                "artifact_id",
                "commit_sha",
                "config",
                "base_id",
                "track_id",
                "metadata",
            )
            if hasattr(value, name)
        }
    artifact_id = raw.get("artifact_id", raw.get("id", expected_id))
    if artifact_id != expected_id:
        raise ValueError("artifact resolver returned a different artifact identity")
    commit = raw.get("commit_sha")
    if not isinstance(commit, str) or not commit:
        raise ValueError("artifact has no commit_sha")
    config = raw.get("config")
    if config is not None and not isinstance(config, Mapping):
        raise TypeError("artifact config must be a mapping")
    metadata = raw.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise TypeError("artifact metadata must be a mapping")
    return {
        "id": expected_id,
        "artifact_id": expected_id,
        "commit_sha": commit,
        "config": None if config is None else _json_mapping(config, "artifact config"),
        "base_id": raw.get("base_id"),
        "track_id": raw.get("track_id"),
        "metadata": _json_mapping(metadata, "artifact metadata"),
    }


def _ticket_field(ticket: object, name: str) -> object:
    if isinstance(ticket, Mapping):
        return ticket.get(name)
    return getattr(ticket, name, None)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
            prefix=f".{path.name}.",
        ) as stream:
            temporary = Path(stream.name)
            json.dump(
                value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class GitPatchPrototypeMaterializer:
    """Apply one unified patch in an owned worktree and publish its validated commit."""

    def __init__(
        self,
        *,
        workspaces: ValidatedGitWorkspaceManager,
        validator: ScopeSecretCandidateValidator,
        action_factory: PrototypeActionFactory,
        journal_root: Path,
        max_patch_bytes: int = 262_144,
        namespace: str | None = None,
    ) -> None:
        if not isinstance(workspaces, ValidatedGitWorkspaceManager):
            raise TypeError("workspaces must be a ValidatedGitWorkspaceManager")
        if not isinstance(validator, ScopeSecretCandidateValidator):
            raise TypeError("validator must be a ScopeSecretCandidateValidator")
        if not callable(action_factory):
            raise TypeError("action_factory must be callable")
        if (
            isinstance(max_patch_bytes, bool)
            or not isinstance(max_patch_bytes, int)
            or max_patch_bytes < 1
        ):
            raise ValueError("max_patch_bytes must be positive")
        journal_root = Path(journal_root)
        if journal_root.exists() and journal_root.is_symlink():
            raise ValueError("prototype journal_root must not be a symlink")
        journal_root.mkdir(parents=True, exist_ok=True)
        self.workspaces = workspaces
        self.validator = validator
        self.action_factory = action_factory
        self.journal_root = journal_root.resolve()
        self.namespace = namespace or hashlib.sha256(str(self.journal_root).encode()).hexdigest()
        self.max_patch_bytes = max_patch_bytes

    def materialize(
        self, *, ticket_id: str, base: Mapping[str, Any], patch: str,
    ) -> Mapping[str, Any]:
        ticket_id = _identifier(ticket_id, 'ticket_id')
        journal = self.journal_root / hashlib.sha256(ticket_id.encode()).hexdigest()[:24]
        journal.mkdir(parents=True, exist_ok=True)
        with (journal / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._materialize(ticket_id=ticket_id, base=base, patch=patch)

    def _materialize(self, *, ticket_id, base, patch):
        ticket_id = _identifier(ticket_id, "ticket_id")
        base = _json_mapping(base, "base artifact")
        parent = _identifier(base.get("commit_sha"), "base commit_sha")
        raw_patch = _text(patch, "patch", maximum=self.max_patch_bytes).encode("utf-8")
        journal = (
            self.journal_root / hashlib.sha256(ticket_id.encode()).hexdigest()[:24]
        )
        request_path, started_path, result_path = (
            journal / "request.json",
            journal / "started.json",
            journal / "result.json",
        )
        request = {
            "ticket_id": ticket_id,
            "base": base,
            "patch_sha256": hashlib.sha256(raw_patch).hexdigest(),
            "patch": patch,
        }
        if request_path.exists():
            if json.loads(request_path.read_text(encoding="utf-8")) != request:
                raise PortfolioBackendError("prototype ticket input changed on replay")
            if result_path.exists():
                result = json.loads(result_path.read_text(encoding="utf-8"))
                if not isinstance(result, dict):
                    raise PortfolioBackendError("prototype result journal is invalid")
                return result
            if started_path.exists():
                raise MeasurementRecoveryRequired(
                    "prototype began without a durable result; inspect its owned worktree"
                )
        else:
            _atomic_json(request_path, request)
        _atomic_json(started_path, {"ticket_id": ticket_id})
        workspace = self.workspaces.create(
            parent_commit_sha=parent, action_id=f"portfolio-prototype:{self.namespace}:{ticket_id}",
        )
        try:
            completed = subprocess.run(
                ("git", "apply", "--index", "--whitespace=error-all", "-"),
                cwd=workspace.path,
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_SYSTEM": "/dev/null",
                    "GIT_TERMINAL_PROMPT": "0",
                },
                input=raw_patch,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                raise PortfolioBackendError(
                    f"prototype patch was rejected by git apply (exit {completed.returncode})"
                )
            staged = self.workspaces.stage(workspace)
            action = self.action_factory(ticket_id, base, workspace, staged)
            if not isinstance(action, PolicyAction):
                raise TypeError("prototype action_factory must return PolicyAction")
            if action.parent_commit_sha != parent:
                raise ValueError("prototype action parent differs from base artifact")
            self.validator.validate(action, workspace, staged)
            commit = self.workspaces.commit(
                workspace, message=f"AgentPilot research prototype {ticket_id}",
            )
            result = {
                "cost_usd": 0.0,
                "summary": {
                    "status": "materialized",
                    "changed_files": list(staged),
                    "evidence_tier": "prototype",
                },
                "artifact": {
                    "commit_sha": commit,
                    "config": base.get("config"),
                    "base_id": base.get("artifact_id"),
                    "metadata": {
                        "origin": "isolated_patch",
                        "ticket_id": ticket_id,
                        "changed_files": list(staged),
                    },
                },
            }
            _atomic_json(result_path, result)
            return result
        except Exception as exc:
            result = {
                "status": "failed",
                "cost_usd": 0.0,
                "error": type(exc).__name__,
                "summary": "Prototype patch did not pass materialization and source boundaries.",
            }
            _atomic_json(result_path, result)
            return result
        finally:
            self.workspaces.remove(workspace)

    def recover(
        self, *, ticket_id: str, base: Mapping[str, Any], patch: str
    ) -> Mapping[str, Any]:
        """Replay only a finished journal; an orphan requires operator recovery."""

        return self.materialize(ticket_id=ticket_id, base=base, patch=patch)


def _measurement_payload(measurement: CellMeasurement) -> dict[str, Any]:
    return {
        "cell_id": measurement.cell_id,
        "program_id": measurement.program_id,
        "status": measurement.status.value,
        "primary_value": measurement.primary_value,
        "constraints_pass": measurement.constraints_pass,
        "diagnostics": _json_mapping(
            measurement.diagnostics, "measurement diagnostics"
        ),
    }


def _measurement_from_payload(value: Mapping[str, Any]) -> CellMeasurement:
    return CellMeasurement(
        cell_id=str(value["cell_id"]),
        program_id=str(value["program_id"]),
        status=MeasurementStatus(str(value["status"])),
        primary_value=value.get("primary_value"),
        constraints_pass=value.get("constraints_pass"),
        diagnostics=_json_mapping(
            value.get("diagnostics", {}), "measurement diagnostics"
        ),
    )


def _measurement_cost(measurement: CellMeasurement) -> float | None:
    provider = measurement.diagnostics.get("provider_diagnostics")
    accounting = provider.get("accounting") if isinstance(provider, Mapping) else None
    value = accounting.get("cost_usd") if isinstance(accounting, Mapping) else None
    if value is None:
        return None
    return _non_negative_cost(value, "measurement provider cost")


class ArenaDevelopmentMeasurementAdapter:
    """Measure immutable artifacts on one shared, checkpointed Visible-A wave."""

    def __init__(
        self,
        *,
        executor: object,
        repository: Path,
        journal_root: Path,
        task_ids: tuple[str, ...],
        estimated_cost_per_cell_usd: float,
        max_replicates: int = 8,
        concurrency: int = 1,
        cost_reader: Callable[[tuple[str, ...]], float] | None = None,
        namespace: str | None = None,
        trace_artifact_resolver: Callable[[str], object] | None = None,
    ) -> None:
        if not (
            callable(getattr(executor, "measure", None))
            or callable(getattr(executor, "measure_candidate_set", None))
        ):
            raise TypeError(
                "executor must provide measure() or measure_candidate_set()"
            )
        repository = Path(repository).resolve()
        if not repository.is_dir():
            raise ValueError("repository must be an existing directory")
        journal_root = Path(journal_root)
        if journal_root.exists() and journal_root.is_symlink():
            raise ValueError("journal_root must not be a symlink")
        journal_root.mkdir(parents=True, exist_ok=True)
        if (
            not task_ids
            or len(task_ids) != len(set(task_ids))
            or any(not isinstance(item, str) or not item for item in task_ids)
        ):
            raise ValueError("task_ids must be a non-empty unique tuple")
        if (
            isinstance(max_replicates, bool)
            or not isinstance(max_replicates, int)
            or max_replicates < 1
        ):
            raise ValueError("max_replicates must be positive")
        if cost_reader is not None and not callable(cost_reader):
            raise TypeError("cost_reader must be callable")
        self.executor = executor
        self.repository = repository
        self.journal_root = journal_root.resolve()
        self.namespace = namespace or hashlib.sha256(str(self.journal_root).encode()).hexdigest()
        self.task_ids = task_ids
        self.estimated_cost_per_cell_usd = _non_negative_cost(
            estimated_cost_per_cell_usd, "estimated_cost_per_cell_usd"
        )
        self.max_replicates = max_replicates
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError('measurement concurrency must be positive')
        self.concurrency = concurrency
        self.cost_reader = cost_reader
        self.trace_artifact_resolver = trace_artifact_resolver

    def estimate(
        self, *, candidate_count: int, task_count: int, replicates: int
    ) -> float:
        return (
            self.estimated_cost_per_cell_usd
            * (candidate_count + 1)
            * task_count
            * replicates
        )

    @staticmethod
    def _seed(ticket_id: str, task_id: str, replicate: int, namespace: str = '') -> int:
        digest = hashlib.sha256(
            ((namespace + '\0' if namespace else '') + f"{ticket_id}\0{task_id}\0{replicate}").encode()
        ).digest()
        return int.from_bytes(digest[:8], "big")

    def _cells(
        self, ticket_id: str, tasks: tuple[str, ...], replicate: int, namespace: str = ''
    ) -> tuple[EvaluationCell, ...]:
        return tuple(
            EvaluationCell(
                cell_id=f"portfolio/{namespace + '/' if namespace else ''}{ticket_id}/{replicate}/{task_id}",
                task_id=task_id,
                block_id=task_id,
                measurement_seed=self._seed(ticket_id, task_id, replicate, namespace),
                replicate=replicate,
            )
            for task_id in tasks
        )

    def _journal_paths(
        self, ticket_id: str, program_id: str, cell: EvaluationCell
    ) -> tuple[Path, Path]:
        digest = hashlib.sha256(
            f"{program_id}\0{cell.cell_id}".encode("utf-8")
        ).hexdigest()
        root = self.journal_root / hashlib.sha256(ticket_id.encode()).hexdigest()[:24]
        return root / f"{digest}.request.json", root / f"{digest}.result.json"

    def _request(self, program_id: str, cell: EvaluationCell) -> dict[str, Any]:
        return {
            "program_id": program_id,
            "cell": {
                "cell_id": cell.cell_id,
                "task_id": cell.task_id,
                "block_id": cell.block_id,
                "measurement_seed": cell.measurement_seed,
                "replicate": cell.replicate,
            },
        }

    def _begin_or_replay(
        self, ticket_id: str, program_id: str, cell: EvaluationCell
    ) -> CellMeasurement | None:
        request_path, result_path = self._journal_paths(ticket_id, program_id, cell)
        request = self._request(program_id, cell)
        if request_path.exists():
            try:
                previous = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise PortfolioBackendError(
                    "development request journal is invalid"
                ) from exc
            if previous != request:
                raise PortfolioBackendError(
                    "development cell identity changed on replay"
                )
            if not result_path.exists():
                raise MeasurementRecoveryRequired(
                    f"development cell {cell.cell_id} started without a durable result"
                )
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise PortfolioBackendError(
                    "development result journal is invalid"
                ) from exc
            measurement = _measurement_from_payload(result)
            if (
                measurement.program_id != program_id
                or measurement.cell_id != cell.cell_id
            ):
                raise PortfolioBackendError(
                    "development result changed its coordinates"
                )
            return measurement
        if result_path.exists():
            raise PortfolioBackendError("development result has no request journal")
        _atomic_json(request_path, request)
        return None

    def _finish(
        self,
        ticket_id: str,
        program_id: str,
        cell: EvaluationCell,
        measurement: CellMeasurement,
    ) -> None:
        if measurement.program_id != program_id or measurement.cell_id != cell.cell_id:
            raise PortfolioBackendError("development executor changed cell coordinates")
        _request_path, result_path = self._journal_paths(ticket_id, program_id, cell)
        payload = _measurement_payload(measurement)
        capture = payload.get('diagnostics', {}).get('trace_capture')
        if isinstance(capture, dict) and capture.get('trace_ref_id') and self.trace_artifact_resolver is not None:
            from .research_worker import artifact_payload
            capture['artifact_ref'] = artifact_payload(self.trace_artifact_resolver(capture['trace_ref_id']))
        _atomic_json(result_path, payload)

    def _measure_program(
        self,
        *,
        ticket_id: str,
        artifact: Mapping[str, Any],
        cells: tuple[EvaluationCell, ...],
    ) -> tuple[CellMeasurement, ...]:
        if artifact.get("config") not in (None, {}):
            raise PortfolioBackendError(
                "this Arena development adapter cannot apply artifact config overrides"
            )
        program_id = _identifier(artifact.get("commit_sha"), "artifact commit_sha")
        replayed: dict[str, CellMeasurement] = {}
        pending: list[EvaluationCell] = []
        for cell in cells:
            old = self._begin_or_replay(ticket_id, program_id, cell)
            if old is None:
                pending.append(cell)
            else:
                replayed[cell.cell_id] = old
        plural = getattr(self.executor, "measure_candidate_set", None)
        if pending and callable(plural):
            measured = tuple(
                plural(
                    program_id=program_id,
                    cells=tuple(pending),
                    workspace=SimpleNamespace(path=self.repository),
                )
            )
            if len(measured) != len(pending):
                raise PortfolioBackendError(
                    "development executor changed cell cardinality"
                )
            for cell, result in zip(pending, measured, strict=True):
                self._finish(ticket_id, program_id, cell, result)
                replayed[cell.cell_id] = result
        elif pending:
            measure = getattr(self.executor, "measure")
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                futures = {pool.submit(measure, program_id=program_id, cell=cell): cell for cell in pending}
                for future in as_completed(futures):
                    cell, result = futures[future], future.result()
                    self._finish(ticket_id, program_id, cell, result)
                    replayed[cell.cell_id] = result
        return tuple(replayed[cell.cell_id] for cell in cells)

    def measure(
        self,
        *,
        ticket_id: str,
        control: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        tasks: Sequence[str] | None,
        replicates: int,
        namespace: str = '',
    ) -> Mapping[str, Any]:
        ticket_id = _identifier(ticket_id, "ticket_id")
        candidates = tuple(
            _json_mapping(item, "candidate artifact") for item in candidates
        )
        if not candidates:
            raise ValueError("measure needs at least one candidate")
        ids = tuple(item.get("artifact_id") for item in candidates)
        if any(not isinstance(item, str) or not item for item in ids) or len(
            set(ids)
        ) != len(ids):
            raise ValueError("measure candidate identities must be unique")
        selected = self.task_ids if tasks is None else tuple(tasks)
        if (
            not selected
            or len(selected) != len(set(selected))
            or not set(selected) <= set(self.task_ids)
        ):
            raise ValueError("measure tasks must be a unique Visible-A subset")
        if (
            isinstance(replicates, bool)
            or not isinstance(replicates, int)
            or not 1 <= replicates <= self.max_replicates
        ):
            raise ValueError(f"replicates must be in [1, {self.max_replicates}]")
        control = _json_mapping(control, "control artifact")
        if control.get("artifact_id") in ids:
            raise ValueError("measure candidates must differ from the shared control")
        all_results: dict[str, list[CellMeasurement]] = {
            str(control["artifact_id"]): []
        }
        all_results.update({str(item["artifact_id"]): [] for item in candidates})
        for replicate in range(replicates):
            cells = self._cells(ticket_id, selected, replicate, namespace)
            all_results[str(control["artifact_id"])].extend(
                self._measure_program(
                    ticket_id=ticket_id, artifact=control, cells=cells
                )
            )
            for candidate in candidates:
                all_results[str(candidate["artifact_id"])].extend(
                    self._measure_program(
                        ticket_id=ticket_id, artifact=candidate, cells=cells
                    )
                )

        flattened = tuple(item for values in all_results.values() for item in values)
        unique_measurements = tuple(
            {(item.program_id, item.cell_id): item for item in flattened}.values()
        )
        evaluation_ids = tuple(
            sorted(
                {
                    str(item.diagnostics["arena_development_evaluation_id"])
                    for item in unique_measurements
                    if isinstance(
                        item.diagnostics.get("arena_development_evaluation_id"), str
                    )
                }
            )
        )
        if self.cost_reader is not None:
            cost = _non_negative_cost(self.cost_reader(evaluation_ids))
        else:
            costs = tuple(_measurement_cost(item) for item in unique_measurements)
            if any(value is None for value in costs):
                raise DevelopmentAccountingRequired(
                    "development measurements need an explicit cost_reader or provider costs"
                )
            cost = sum(float(value) for value in costs if value is not None)

        control_id = str(control["artifact_id"])
        control_by_cell = {item.cell_id: item for item in all_results[control_id]}
        cell_by_id = {
            cell.cell_id: cell
            for replicate in range(replicates)
            for cell in self._cells(ticket_id, selected, replicate, namespace)
        }
        comparisons: dict[str, Any] = {}
        for candidate in candidates:
            candidate_id = str(candidate["artifact_id"])
            rows = []
            deltas: list[float] = []
            for item in all_results[candidate_id]:
                parent = control_by_cell[item.cell_id]
                comparable = (
                    parent.status is MeasurementStatus.VALID
                    and item.status is MeasurementStatus.VALID
                    and parent.primary_value is not None
                    and item.primary_value is not None
                )
                delta = (
                    float(item.primary_value) - float(parent.primary_value)
                    if comparable
                    else None
                )
                if delta is not None:
                    deltas.append(delta)
                rows.append(
                    {
                        "cell_id": item.cell_id,
                        "task_id": cell_by_id[item.cell_id].task_id,
                        "replicate": cell_by_id[item.cell_id].replicate,
                        "control": parent.primary_value,
                        "candidate": item.primary_value,
                        "delta": delta,
                        "control_status": parent.status.value,
                        "candidate_status": item.status.value,
                    }
                )
            comparisons[candidate_id] = {
                "cells": rows,
                "valid_pairs": len(deltas),
                "mean_delta": sum(deltas) / len(deltas) if deltas else None,
            }
        trace_cases = []
        trace_artifacts = []
        program_bindings: dict[str, list[tuple[str, str]]] = {}
        for role, artifact in (
            ("control", control),
            *(("candidate", item) for item in candidates),
        ):
            program_bindings.setdefault(str(artifact["commit_sha"]), []).append(
                (str(artifact["artifact_id"]), role)
            )
        for item in unique_measurements:
            capture = item.diagnostics.get("trace_capture")
            trace_ref = (
                capture.get("trace_ref_id") if isinstance(capture, Mapping) else None
            )
            if not isinstance(trace_ref, str) or not trace_ref:
                continue
            artifact_ref = capture.get('artifact_ref')
            if artifact_ref is None and self.trace_artifact_resolver is not None:
                from .research_worker import artifact_payload
                artifact_ref = artifact_payload(self.trace_artifact_resolver(trace_ref))
            if artifact_ref is not None:
                trace_artifacts.append(artifact_ref)
            bindings = program_bindings[item.program_id]
            roles = {role for _artifact_id, role in bindings}
            case_key = f"portfolio:{ticket_id}:{item.program_id}:{item.cell_id}"
            case = TraceCaseIndex.create(
                case_key=case_key,
                trace_ref_id=trace_ref,
                outcome="pass" if item.primary_value == 1.0 else "fail",
                measurement_status=item.status.value,
                final_artifact_state=str(
                    item.diagnostics.get("artifact_state")
                    or item.diagnostics.get("answer_state")
                    or item.diagnostics.get("agent_state")
                    or item.status.value
                ),
            )
            trace_cases.append(
                {
                    "case_ref": case.case_ref,
                    "case_key": case_key,
                    "artifact_id": bindings[0][0],
                    "artifact_ids": [artifact_id for artifact_id, _role in bindings],
                    "role": next(iter(roles)) if len(roles) == 1 else "shared",
                    "program_id": item.program_id,
                    "cell_id": item.cell_id,
                    "task_id": cell_by_id[item.cell_id].task_id,
                    "replicate": cell_by_id[item.cell_id].replicate,
                    "trace_ref_id": trace_ref,
                    "outcome": case.outcome,
                    "measurement_status": item.status.value,
                    "final_artifact_state": case.final_artifact_state,
                }
            )
        return {
            "cost_usd": cost,
            "summary": {
                "evidence_tier": "visible_a_development",
                "wave_id": ticket_id,
                "control": control_id,
                "tasks": list(selected),
                "replicates": replicates,
                "shared_control": True,
                "comparisons": comparisons,
                "note": "Development evidence only; it does not decide admission.",
            },
            "traces": [item["case_ref"] for item in trace_cases],
            "trace_cases": trace_cases,
            **({'trace_artifacts': trace_artifacts} if trace_artifacts else {}),
        }


class ArenaResearchPortfolioBackend:
    """Capability-driven bridge from persistent portfolio tickets to Arena."""

    def __init__(
        self,
        *,
        artifact_resolver: ArtifactResolver,
        default_base_id: str,
        python_probe: object | None = None,
        prototype: GitPatchPrototypeMaterializer | None = None,
        builder: WorkerBuilder | None = None,
        development: ArenaDevelopmentMeasurementAdapter | None = None,
        admission: AdmissionEvaluator | None = None,
        component_call: object | None = None,
        experiment_runner: ExperimentRunner | None = None,
        experiment_resolver: Callable | None = None,
    ) -> None:
        if not callable(artifact_resolver):
            raise TypeError("artifact_resolver must be callable")
        self.artifact_resolver = artifact_resolver
        self.default_base_id = _identifier(default_base_id, "default_base_id")
        if python_probe is not None and not callable(
            getattr(python_probe, "run", None)
        ):
            raise TypeError("python_probe must provide run()")
        if prototype is not None and not isinstance(
            prototype, GitPatchPrototypeMaterializer
        ):
            raise TypeError("prototype must be GitPatchPrototypeMaterializer")
        if builder is not None and not callable(getattr(builder, "build", None)):
            raise TypeError("builder must provide build()")
        if development is not None and not isinstance(
            development, ArenaDevelopmentMeasurementAdapter
        ):
            raise TypeError("development must be ArenaDevelopmentMeasurementAdapter")
        if admission is not None and not callable(getattr(admission, "admit", None)):
            raise TypeError("admission must provide admit()")
        if component_call is not None and not (
            callable(getattr(component_call, "prepare", None))
            and callable(getattr(component_call, "execute", None))
        ):
            raise TypeError("component_call must provide prepare() and execute()")
        if experiment_runner is not None and not (
            callable(getattr(experiment_runner, "run", None))
            and callable(getattr(experiment_runner, "estimate", None))
        ):
            raise TypeError("experiment_runner must provide run() and estimate()")
        self.python_probe = python_probe
        self.prototype = prototype
        self.builder = builder
        self.development = development
        self.admission = admission
        self.component_call = component_call
        self.experiment_runner = experiment_runner
        self.experiment_resolver = experiment_resolver

    def _artifact(self, artifact_id: object) -> dict[str, Any]:
        identity = _identifier(artifact_id, "artifact_id")
        return _artifact_snapshot(self.artifact_resolver(identity), identity)

    def capabilities(self) -> Mapping[str, Mapping[str, Any]]:
        result: dict[str, Mapping[str, Any]] = {}
        if self.experiment_runner is not None:
            result["experiment"] = {
                "description": "Delegate a research experiment to the coding worker and return its evidence.",
                "required": ["instruction"],
                "optional": ["base_artifact", "research_evidence"],
            }
        if self.python_probe is not None:
            result["python"] = {
                "description": "Run a local stdlib calculation in the isolated Python sandbox.",
                "required": ["code"],
                "optional": [],
            }
        if self.prototype is not None:
            result["prototype"] = {
                "description": "Apply a small patch to an isolated base artifact; no model or task run.",
                "required": ["patch"],
                "optional": ["base_artifact"],
            }
        if self.builder is not None:
            result["build"] = {
                "description": (
                    "Build a candidate, normally followed by restricted-B admission."
                    if self.admission is not None
                    else "Build a candidate; restricted-B admission is unavailable."
                ),
                "required": ["instruction"],
                "optional": [
                    "base_artifact",
                    "files",
                    "new_files",
                    "research_evidence",
                    "evaluate",
                    "from_experiment",
                    "experiment_handoff",
                ],
            }
        if self.development is not None:
            result["measure"] = {
                "description": "Compare existing artifacts with one shared control on development A.",
                "required": ["candidates"],
                "optional": ["control", "tasks", "replicates"],
            }
        if self.admission is not None:
            result["admit"] = {
                "description": "Evaluate one existing candidate with the frozen restricted-B admission plan.",
                "required": ["candidate"],
                "optional": ["control"],
            }
        if self.component_call is not None:
            result["call"] = {
                "description": "Call one configured program template with a constructed input.",
                "required": ["template"],
                "optional": ["variables", "input", "base_artifact"],
            }
        return result

    def prepare(self, kind: str, args: Mapping[str, Any]) -> Mapping[str, Any]:
        kind = _identifier(kind, "kind")
        if kind not in self.capabilities():
            raise PortfolioBackendError(f"experiment kind {kind!r} is unavailable")
        raw = _json_mapping(args, "experiment arguments")
        followup = None
        if kind == "experiment":
            allowed, required = (
                {"instruction", "base_artifact", "research_evidence"},
                {"instruction"},
            )
            base_id = raw.get("base_artifact", self.default_base_id)
            arguments = {
                "instruction": _text(raw.get("instruction"), "instruction"),
                "research_evidence": _json_mapping(
                    raw.get("research_evidence", {}), "research_evidence"
                ),
                "base": self._artifact(base_id),
                "artifact_id": base_id,
            }
            reservation = _non_negative_cost(
                self.experiment_runner.estimate(arguments),
                "experiment reservation",
            )
        elif kind == "call":
            prepared = _json_mapping(
                self.component_call.prepare(raw), "component call preparation"
            )
            arguments = _json_mapping(
                prepared.get("arguments"), "component call arguments"
            )
            reservation = _non_negative_cost(
                prepared.get("reservation_usd"), "component call reservation"
            )
            allowed, required = set(raw), set()
        elif kind == "python":
            allowed, required = {"code"}, {"code"}
            arguments = {"code": _text(raw.get("code"), "code")}
            reservation = 0.0
        elif kind == "prototype":
            allowed, required = {"patch", "base_artifact"}, {"patch"}
            base_id = raw.get("base_artifact", self.default_base_id)
            arguments = {
                "patch": _text(
                    raw.get("patch"), "patch", maximum=self.prototype.max_patch_bytes
                ),
                "base": self._artifact(base_id),
                "artifact_id": base_id,
            }
            reservation = 0.0
        elif kind == "build":
            allowed, required = (
                {
                    "instruction",
                    "base_artifact",
                    "files",
                    "new_files",
                    "research_evidence",
                    "evaluate",
                    "from_experiment",
                    "experiment_handoff",
                },
                {"instruction"},
            )
            arguments = {
                "instruction": _text(raw.get("instruction"), "instruction"),
                "files": list(_path_list(raw.get("files"), "files")),
                "new_files": list(_path_list(raw.get("new_files"), "new_files")),
                "research_evidence": _json_mapping(
                    raw.get("research_evidence", {}), "research_evidence"
                ),
                "base": self._artifact(raw.get("base_artifact", self.default_base_id)),
                "artifact_id": raw.get("base_artifact", self.default_base_id),
            }
            if raw.get('experiment_handoff') is not None:
                from .research_handoff import validate_handoff_tickets
                if self.experiment_resolver is None:
                    raise ValueError('This backend cannot resolve experiment material')
                handoff = validate_handoff_tickets(raw['experiment_handoff'], self.experiment_resolver,
                                                   build_base_commit_sha=arguments['base']['commit_sha'])
                if raw.get('from_experiment') != handoff['adopted_ticket']:
                    raise ValueError('from_experiment does not match the frozen experiment material')
                arguments['experiment_handoff'] = handoff
            elif raw.get('from_experiment') is not None:
                raise ValueError('from_experiment requires its frozen experiment material')
            reservation = _non_negative_cost(
                self.builder.estimate(arguments), "build reservation"
            )
            option = raw.get("evaluate", self.admission is not None)
            if type(option) is not bool:
                raise ValueError("evaluate must be true or false")
            if option:
                if self.admission is None:
                    raise ValueError("evaluate=true requires restricted-B admission support")
                admission_arguments = {
                    "control": self._artifact(self.default_base_id)
                }
                followup = {
                    "kind": "admit",
                    "arguments": admission_arguments,
                    "reservation_usd": _non_negative_cost(
                        self.admission.estimate(admission_arguments),
                        "admission reservation",
                    ),
                    "reserve_upfront": True,
                }
        elif kind == "measure":
            allowed, required = (
                {"candidates", "control", "tasks", "replicates",},
                {"candidates"},
            )
            candidate_ids = raw.get("candidates")
            if not isinstance(candidate_ids, (list, tuple)) or not candidate_ids:
                raise ValueError("candidates must be a non-empty list")
            candidates = [self._artifact(item) for item in candidate_ids]
            resolved_ids = [item["artifact_id"] for item in candidates]
            if len(resolved_ids) != len(set(resolved_ids)):
                raise ValueError("candidates contains duplicates")
            tasks = raw.get("tasks")
            if tasks is not None:
                tasks = list(_path_list(tasks, "tasks"))
                if not tasks or not set(tasks) <= set(self.development.task_ids):
                    raise ValueError("tasks must be a non-empty Visible-A subset")
            replicates = raw.get("replicates", 1)
            if (
                isinstance(replicates, bool)
                or not isinstance(replicates, int)
                or not 1 <= replicates <= self.development.max_replicates
            ):
                raise ValueError(
                    f"replicates must be in [1, {self.development.max_replicates}]"
                )
            control = self._artifact(raw.get("control", self.default_base_id))
            if control["artifact_id"] in resolved_ids:
                raise ValueError(
                    "measure candidates must differ from the shared control"
                )
            arguments = {
                "control": control,
                "candidates": candidates,
                "tasks": tasks,
                "replicates": replicates,
                "artifact_id": candidate_ids[0],
                "execution_namespace": getattr(self.development, 'namespace', ''),
            }
            task_count = len(self.development.task_ids if tasks is None else tasks)
            reservation = self.development.estimate(
                candidate_count=len(candidates),
                task_count=task_count,
                replicates=replicates,
            )
        else:
            allowed, required = {"candidate", "control"}, {"candidate"}
            candidate = self._artifact(raw.get("candidate"))
            arguments = {
                "candidate": candidate,
                "control": self._artifact(raw.get("control", self.default_base_id)),
                "artifact_id": raw.get("candidate"),
            }
            reservation = _non_negative_cost(
                self.admission.estimate(arguments), "admission reservation"
            )
        unknown = set(raw) - allowed
        missing = required - set(raw)
        if unknown:
            raise ValueError(f"unknown {kind} arguments: {sorted(unknown)}")
        if missing:
            raise ValueError(f"missing {kind} arguments: {sorted(missing)}")
        return {"arguments": arguments, "reservation_usd": reservation,
                **({'followup': followup} if followup is not None else {})}

    def execute(self, ticket: object) -> Mapping[str, Any]:
        ticket_id = _identifier(_ticket_field(ticket, "id"), "ticket.id")
        kind = _identifier(_ticket_field(ticket, "kind"), "ticket.kind")
        arguments = _json_mapping(
            _ticket_field(ticket, "arguments"), "ticket.arguments"
        )
        if kind == "experiment" and self.experiment_runner is not None:
            return self.experiment_runner.run(
                ticket_id=ticket_id,
                base=arguments["base"],
                instruction=arguments["instruction"],
                research_evidence=arguments["research_evidence"],
            )
        if kind == "python" and self.python_probe is not None:
            result = dict(
                self.python_probe.run(probe_id=ticket_id, code=arguments["code"])
            )
            return {
                "cost_usd": 0.0,
                "summary": {
                    "passed": result.get("passed"),
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                    "exit_code": result.get("exit_code"),
                    "evidence_tier": "local_probe",
                },
            }
        if kind == "call" and self.component_call is not None:
            return self.component_call.execute(ticket)
        if kind == "prototype" and self.prototype is not None:
            return self.prototype.materialize(
                ticket_id=ticket_id, base=arguments["base"], patch=arguments["patch"]
            )
        if kind == "build" and self.builder is not None:
            return self.builder.build(
                ticket_id=ticket_id,
                base=arguments["base"],
                instruction=arguments["instruction"],
                files=tuple(arguments["files"]),
                new_files=tuple(arguments["new_files"]),
                research_evidence=arguments["research_evidence"],
                **({'experiment_handoff': arguments['experiment_handoff']} if 'experiment_handoff' in arguments else {}),
            )
        if kind == "measure" and self.development is not None:
            return self.development.measure(
                ticket_id=ticket_id,
                control=arguments["control"],
                candidates=arguments["candidates"],
                tasks=arguments["tasks"],
                replicates=arguments["replicates"],
                namespace=arguments.get('execution_namespace', ''),
            )
        if kind == "admit" and self.admission is not None:
            return self.admission.admit(
                ticket_id=ticket_id,
                candidate=arguments["candidate"],
                control=arguments["control"],
            )
        raise PortfolioBackendError(f"ticket kind {kind!r} is unavailable")

    def recover(self, ticket: object) -> Mapping[str, Any]:
        """Resume only operations whose concrete adapter has durable receipts."""

        kind = _identifier(_ticket_field(ticket, "kind"), "ticket.kind")
        arguments = _json_mapping(
            _ticket_field(ticket, "arguments"), "ticket.arguments"
        )
        ticket_id = _identifier(_ticket_field(ticket, "id"), "ticket.id")
        if kind == "experiment" and self.experiment_runner is not None:
            recovery = getattr(self.experiment_runner, "recover", None)
            if not callable(recovery):
                raise MeasurementRecoveryRequired(
                    "worker experiment has no recovery adapter"
                )
            return recovery(
                ticket_id=ticket_id,
                base=arguments["base"],
                instruction=arguments["instruction"],
                research_evidence=arguments["research_evidence"],
            )
        if kind in {"python", "call", "measure"}:
            # Bwrap probes and DurableCompletion replay stable call identities;
            # development measurement replays per-cell results and refuses an
            # in-flight cell that lacks a receipt.
            return self.execute(ticket)
        if kind == "prototype" and self.prototype is not None:
            return self.prototype.recover(
                ticket_id=ticket_id, base=arguments["base"], patch=arguments["patch"]
            )
        if kind == "build" and self.builder is not None:
            recovery = getattr(self.builder, "recover", None)
            if not callable(recovery):
                raise MeasurementRecoveryRequired(
                    "worker build has no recovery adapter"
                )
            return recovery(
                ticket_id=ticket_id,
                base=arguments["base"],
                instruction=arguments["instruction"],
                files=tuple(arguments["files"]),
                new_files=tuple(arguments["new_files"]),
                research_evidence=arguments["research_evidence"],
                **({'experiment_handoff': arguments['experiment_handoff']} if 'experiment_handoff' in arguments else {}),
            )
        if kind == "admit" and self.admission is not None:
            recovery = getattr(self.admission, "recover", None)
            if not callable(recovery):
                raise MeasurementRecoveryRequired("admission has no recovery adapter")
            return recovery(ticket)
        raise PortfolioBackendError(f"ticket kind {kind!r} is unavailable")


def register_visible_result(reader: object, ticket: object) -> None:
    """Register only development-A captures from a completed portfolio ticket.

    The trace bodies and artifact records already live in the reader's shared
    ``TraceStore`` through the configured Visible-A sink.  This function adds
    immutable manifests and proposer-facing case indexes, then extends the
    frozen task briefs with the same public A task under the new cell identity.
    It deliberately ignores admission results.
    """

    if _ticket_field(ticket, 'kind') == 'experiment':
        from types import SimpleNamespace
        result = _ticket_field(ticket, 'result')
        for measured in result.get('development_measurements', ()) if isinstance(result, Mapping) else ():
            if isinstance(measured, Mapping) and isinstance(measured.get('result'), Mapping):
                register_visible_result(reader, SimpleNamespace(kind='measure', id=measured['measurement_id'], result=measured['result']))
        return
    if _ticket_field(ticket, "kind") != "measure":
        return
    result = _ticket_field(ticket, "result")
    if not isinstance(result, Mapping):
        return
    raw_cases = result.get("trace_cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        return
    inputs = getattr(reader, "inputs", None)
    trace_store = getattr(inputs, "trace_store", None)
    if trace_store is None or not callable(getattr(trace_store, "artifact", None)):
        raise TypeError("research reader has no trace store")
    from .research_trace_recovery import register_result_artifacts
    register_result_artifacts(trace_store, result)
    templates = tuple(getattr(inputs, "manifests", ()))
    if not templates:
        raise PortfolioBackendError(
            "development traces need a Visible-A manifest template"
        )
    template = templates[0]
    ticket_id = _identifier(_ticket_field(ticket, "id"), "ticket.id")
    grouped: dict[str, list[tuple[object, TraceCaseIndex]]] = {}
    supplied_evidence = tuple(
        getattr(getattr(reader, "request", None), "visible_a_evidence", ())
    )
    by_task = {item.task_id: item for item in supplied_evidence}
    experiment_cases = getattr(reader, "experiment_cases", None)
    if experiment_cases is None:
        experiment_cases = {}
        reader.experiment_cases = experiment_cases
    experiment_task_evidence = getattr(reader, "experiment_task_evidence", None)
    if experiment_task_evidence is None:
        experiment_task_evidence = {}
        reader.experiment_task_evidence = experiment_task_evidence
    for raw in raw_cases:
        if not isinstance(raw, Mapping):
            raise TypeError("trace_cases must contain mappings")
        trace_ref = _identifier(raw.get("trace_ref_id"), "trace_ref_id")
        artifact = trace_store.artifact(trace_ref)
        if artifact.evidence_tier.value != "visible_a":
            raise PortfolioBackendError("portfolio trace is not Visible-A evidence")
        for field in ("program_id", "cell_id", "task_id"):
            if raw.get(field) != getattr(artifact, field):
                raise PortfolioBackendError("portfolio trace coordinates changed")
        case = TraceCaseIndex.create(
            case_key=f"portfolio:{ticket_id}:{artifact.program_id}:{artifact.cell_id}",
            trace_ref_id=artifact.trace_ref_id,
            outcome=_identifier(raw.get("outcome"), "trace outcome"),
            measurement_status=_identifier(
                raw.get("measurement_status"), "measurement_status"
            ),
            final_artifact_state=_identifier(
                raw.get("final_artifact_state"), "final_artifact_state"
            ),
        )
        if raw.get("case_ref") not in (None, case.case_ref):
            raise PortfolioBackendError("portfolio trace case identity changed")
        grouped.setdefault(artifact.program_id, []).append((artifact, case))
        source = by_task.get(artifact.task_id)
        if source is not None:
            experiment_task_evidence[case.case_ref] = source
        artifact_ids = raw.get("artifact_ids", [raw.get("artifact_id")])
        if not isinstance(artifact_ids, list) or any(
            not isinstance(item, str) or not item for item in artifact_ids
        ):
            raise TypeError("portfolio trace artifact_ids are invalid")
        role = raw.get("role")
        if role not in {"control", "candidate", "shared"}:
            raise ValueError("portfolio trace role is invalid")
        experiment_cases[case.case_ref] = {
            "ticket_id": ticket_id,
            "artifact_id": artifact_ids[0],
            "artifact_ids": list(artifact_ids),
            "program_id": artifact.program_id,
            "role": role,
            "commit_sha": artifact.commit_sha,
            "task_id": artifact.task_id,
        }

    registered_cases = []
    for program_id, rows in grouped.items():
        artifacts = tuple(item[0] for item in rows)
        cases = tuple(item[1] for item in rows)
        coordinates = {(item.commit_sha, item.tree_sha) for item in artifacts}
        if len(coordinates) != 1:
            raise PortfolioBackendError(
                "one measured artifact spans multiple Git trees"
            )
        commit_sha, tree_sha = next(iter(coordinates))
        manifest = TraceEvidenceManifest.create(
            program_id=program_id,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            compatibility=template.compatibility,
            task_set_id=stable_id("portfolio-a-task-set", ticket_id),
            measurement_profile_id=stable_id(
                "portfolio-a-measurement", template.measurement_profile_id, ticket_id
            ),
            artifact_refs=artifacts,
            case_indexes=cases,
            available_at=max(item.available_at for item in artifacts),
        )
        trace_store.register_manifest(manifest)
        registered_cases.extend(cases)
        reader.artifacts.update({item.trace_ref_id: item for item in artifacts})

    reader.cases.update({item.case_ref: item for item in registered_cases})
    reader.available_cases.update({item.case_ref: item for item in registered_cases})
    reader.records.update(
        {item.case_ref: trace_case_evidence(item) for item in registered_cases}
    )
    # The completed result exposes case addresses, not unread trace bodies.
    # Issue those addresses so native tool enums can offer the new executions.
    reader.seen.update(item.case_ref for item in registered_cases)
    reader.case_catalog_paged = bool(set(reader.cases) - set(reader.available_cases))
    reader.refresh()


__all__ = [
    "AdmissionEvaluator",
    "ArenaDevelopmentMeasurementAdapter",
    "ArenaResearchPortfolioBackend",
    "DevelopmentAccountingRequired",
    "ExperimentRunner",
    "GitPatchPrototypeMaterializer",
    "MeasurementRecoveryRequired",
    "PortfolioBackendError",
    "PrototypeActionFactory",
    "WorkerBuilder",
    "register_visible_result",
]
