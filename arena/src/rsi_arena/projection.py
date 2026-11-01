"""Public, read-only projections of canonical Arena runs.

This module is the sole presentation boundary for stored Arena experiments.
It validates the write-once experiment binding, replays the recorder without
opening a writer, and exposes only method-visible development and held-out
summary data.  Web frontends should consume this module instead of reading
Arena ledgers directly.
"""

from __future__ import annotations

import json
import math
import os
import selectors
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from rsi_arena.development import replay_delivered_development_results
from rsi_arena.experiment import EXPERIMENT_FILENAME, experiment_digest, read_experiment
from rsi_arena.held_out_evaluation import validate_held_out_publication
from rsi_arena.run_layout import (
    RUN_MANIFEST_SCHEMA,
    CandidateRecord,
    CanonicalRunReplay,
    RunLayout,
    RunValidationError,
    replay_run,
)

_RUN_AUTHORITY_SCHEMA = "rsi-arena-generic-command-authority"
_EXPERIMENT_BINDING_SCHEMA = "rsi-arena-experiment-binding"
_DIFF_MAX_BYTES = 400_000
_GIT_TIMEOUT_SECONDS = 30.0


class ArenaProjectionError(RuntimeError):
    """A stored run cannot form a safe public projection."""


@dataclass(frozen=True)
class ArenaRunAuthority:
    """Validated paths and identity from one write-once experiment."""

    control_root: Path
    layout_root: Path
    runtime_root: Path
    run_id: str
    workspace: Path
    cwd: Path
    candidate_root: Path
    trusted_candidate_root: Path
    trusted_candidate_commit: str
    trusted_candidate_tree: str


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ArenaProjectionError(f"{label} is not a regular file")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ArenaProjectionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArenaProjectionError(f"{label} is unreadable") from exc
    if not isinstance(payload, dict):
        raise ArenaProjectionError(f"{label} is not a JSON object")
    return payload


def _safe_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _contained_path(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ArenaProjectionError(f"Arena {label} authority is invalid")
    try:
        path = Path(value).expanduser().resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ArenaProjectionError(f"Arena {label} escaped its run root") from exc
    return path


def _trusted_git_identity(root: Path) -> tuple[str, str]:
    environment = _git_environment()
    values: list[str] = []
    for spec in ("HEAD", "HEAD^{tree}"):
        completed = subprocess.run(
            ("git", "rev-parse", spec),
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        value = completed.stdout.strip().lower()
        if completed.returncode != 0 or len(value) not in {40, 64}:
            raise ArenaProjectionError("trusted candidate Git identity is invalid")
        values.append(value)
    status = subprocess.run(
        ("git", "status", "--porcelain=v1", "--untracked-files=all", "--ignored"),
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if status.returncode != 0 or status.stdout:
        raise ArenaProjectionError("trusted candidate template is not clean")
    return values[0], values[1]


def _run_authority(
    control_root: Path, experiment: Mapping[str, object]
) -> ArenaRunAuthority:
    arena = experiment.get("arena")
    raw = arena.get("authority") if isinstance(arena, Mapping) else None
    fields = {
        "schema",
        "control_root",
        "layout_root",
        "runtime_root",
        "run_id",
        "workspace",
        "cwd",
        "candidate_root",
        "trusted_candidate_root",
        "trusted_candidate_commit",
        "trusted_candidate_tree",
    }
    if (
        not isinstance(raw, Mapping)
        or set(raw) != fields
        or raw.get("schema") != _RUN_AUTHORITY_SCHEMA
        or any(
            not isinstance(raw.get(field), str) or not raw.get(field)
            for field in fields
        )
    ):
        raise ArenaProjectionError("Arena run authority is invalid")
    try:
        recorded_control = Path(str(raw["control_root"])).resolve(strict=True)
    except OSError as exc:
        raise ArenaProjectionError("Arena control root is unavailable") from exc
    if recorded_control != control_root:
        raise ArenaProjectionError("Arena run root disagrees with its authority")
    layout_root = _contained_path(control_root, raw["layout_root"], "layout root")
    runtime_root = _contained_path(control_root, raw["runtime_root"], "runtime root")
    workspace = _contained_path(control_root, raw["workspace"], "workspace")
    cwd = _contained_path(control_root, raw["cwd"], "working directory")
    candidate_root = _contained_path(
        control_root, raw["candidate_root"], "candidate root"
    )
    trusted_candidate_root = _contained_path(
        control_root, raw["trusted_candidate_root"], "trusted candidate root"
    )
    for path, label in (
        (layout_root, "layout root"),
        (runtime_root, "runtime root"),
        (workspace, "workspace"),
        (cwd, "working directory"),
        (candidate_root, "candidate root"),
        (trusted_candidate_root, "trusted candidate root"),
    ):
        if not path.is_dir():
            raise ArenaProjectionError(f"Arena {label} is not a directory")
    trusted_commit, trusted_tree = _trusted_git_identity(trusted_candidate_root)
    if (
        raw["trusted_candidate_commit"] != trusted_commit
        or raw["trusted_candidate_tree"] != trusted_tree
    ):
        raise ArenaProjectionError("trusted candidate template identity changed")
    return ArenaRunAuthority(
        control_root=control_root,
        layout_root=layout_root,
        runtime_root=runtime_root,
        run_id=str(raw["run_id"]),
        workspace=workspace,
        cwd=cwd,
        candidate_root=candidate_root,
        trusted_candidate_root=trusted_candidate_root,
        trusted_candidate_commit=str(raw["trusted_candidate_commit"]),
        trusted_candidate_tree=str(raw["trusted_candidate_tree"]),
    )


def open_run_replay(
    control_root: str | os.PathLike[str],
) -> tuple[CanonicalRunReplay, Mapping[str, object], ArenaRunAuthority]:
    """Open one generic-command run through canonical, writer-free replay."""

    supplied = Path(control_root).expanduser().absolute()
    try:
        info = supplied.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArenaProjectionError("Arena run root must be a real directory")
        root = supplied.resolve(strict=True)
        experiment = read_experiment(root)
        authority = _run_authority(root, experiment)
        layout = RunLayout.open_existing(authority.layout_root)
        if layout.run_id != authority.run_id:
            raise ArenaProjectionError("Arena layout run identity changed")
        replay = replay_run(layout)
    except ArenaProjectionError:
        raise
    except (KeyError, OSError, RunValidationError, ValueError) as exc:
        raise ArenaProjectionError("Arena run authority is invalid") from exc

    manifest = replay.manifest
    expected_binding = {
        "schema": _EXPERIMENT_BINDING_SCHEMA,
        "path": str(root / EXPERIMENT_FILENAME),
        "sha256": experiment_digest(experiment),
        "workspace": str(authority.workspace),
        "cwd": str(authority.cwd),
        "candidate_root": str(authority.candidate_root),
    }
    if (
        manifest.get("schema") != RUN_MANIFEST_SCHEMA
        or manifest.get("arena_experiment") != expected_binding
    ):
        raise ArenaProjectionError("Arena experiment/layout binding is invalid")
    return replay, MappingProxyType(dict(experiment)), authority


def _usage_by_candidate(replay: CanonicalRunReplay) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in replay.usage:
        candidate_id = row.get("candidate_id")
        if isinstance(candidate_id, str) and candidate_id:
            grouped.setdefault(candidate_id, []).append(row)

    projected: dict[str, dict[str, Any]] = {}
    for candidate_id, rows in grouped.items():
        projected[candidate_id] = _usage_summary(rows)
    return projected


def _usage_by_development(replay: CanonicalRunReplay) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in replay.usage:
        if row.get("usage_scope") != "development":
            continue
        evaluation_id = row.get("evaluation_id")
        if isinstance(evaluation_id, str) and evaluation_id:
            grouped.setdefault(evaluation_id, []).append(row)
    return {
        evaluation_id: _usage_summary(rows) for evaluation_id, rows in grouped.items()
    }


def _usage_summary(rows: list[Mapping[str, object]]) -> dict[str, Any]:
    input_values = [row.get("input_tokens") for row in rows]
    output_values = [row.get("output_tokens") for row in rows]
    cost_values = [row.get("accounting_cost_usd") for row in rows]
    return {
        "input_tokens": (
            sum(int(value) for value in input_values)
            if input_values
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in input_values
            )
            else None
        ),
        "output_tokens": (
            sum(int(value) for value in output_values)
            if output_values
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in output_values
            )
            else None
        ),
        "cost_usd": (
            sum(float(value) for value in cost_values)
            if cost_values
            and all(_safe_number(value) is not None for value in cost_values)
            else None
        ),
        "calls": len(rows),
        "roles": sorted({str(row.get("role")) for row in rows if row.get("role")}),
    }


def _run_usage(replay: CanonicalRunReplay) -> dict[str, dict[str, Any]]:
    rows = list(replay.usage)
    development = [row for row in rows if row.get("usage_scope") == "development"]
    search = [row for row in rows if row.get("spend_kind") == "search"]
    candidate = [
        row
        for row in rows
        if row.get("spend_kind") == "evaluation"
        and row.get("usage_scope") != "development"
    ]
    return {
        "total": _usage_summary(rows),
        "search": _usage_summary(search),
        "development": _usage_summary(development),
        "candidate_evaluation": _usage_summary(candidate),
    }


class ArenaRunProjection:
    """Public read model for one stored Arena run."""

    def __init__(self, control_root: str | os.PathLike[str]):
        self.control_root = Path(control_root).expanduser().absolute()
        replay, _experiment, _authority = open_run_replay(self.control_root)
        self.run_id = replay.layout.run_id

    def _context(
        self,
    ) -> tuple[CanonicalRunReplay, Mapping[str, object], ArenaRunAuthority]:
        return open_run_replay(self.control_root)

    def catalogue_entry(self) -> dict[str, Any]:
        replay, _experiment, _authority = self._context()
        return self._catalogue_entry(replay)

    @classmethod
    def _catalogue_entry(cls, replay: CanonicalRunReplay) -> dict[str, Any]:
        freeze = replay.manifest.get("campaign_run_freeze")
        if not isinstance(freeze, Mapping) or not isinstance(
            freeze.get("benchmark_ids"), list
        ):
            raise ArenaProjectionError("Arena benchmark order is invalid")
        return {
            "run_id": replay.layout.run_id,
            "campaign": replay.layout.campaign,
            "method": replay.layout.method,
            "run_scope_id": replay.layout.benchmark,
            "benchmarks": list(freeze["benchmark_ids"]),
            "attempt_cap": cls._attempt_cap(replay.manifest),
            "attempts": len(replay.attempts),
            "candidates": len(replay.candidates),
            "terminal_candidates": len(replay.delivered_results),
            "aggregate_policy": (
                dict(freeze["aggregate_policy"])
                if isinstance(freeze.get("aggregate_policy"), Mapping)
                else None
            ),
        }

    def detail(self) -> dict[str, Any]:
        replay, _experiment, _authority = self._context()
        attempts = {attempt.attempt_id: attempt for attempt in replay.attempts}
        delivered = {result.candidate_id: result for result in replay.delivered_results}
        usage = _usage_by_candidate(replay)
        terminal_times = {
            str(row["candidate_id"]): row.get("timestamp")
            for row in replay.events
            if row.get("event_type") == "candidate_evaluation_terminal"
            and isinstance(row.get("candidate_id"), str)
        }
        started_times = {
            str(row["candidate_id"]): row.get("timestamp")
            for row in replay.events
            if row.get("event_type") == "candidate_evaluation_started"
            and isinstance(row.get("candidate_id"), str)
        }

        best: float | None = None
        candidate_rows: list[dict[str, Any]] = []
        for candidate in replay.candidates:
            result = delivered.get(candidate.candidate_id)
            vector: list[dict[str, Any]] = []
            tasks: list[dict[str, Any]] = []
            if result is not None:
                children = {
                    child.benchmark_id: child for child in result.benchmark_results
                }
                vector = [
                    {
                        "benchmark_id": benchmark_id,
                        "score": score,
                        "n_expected": children[benchmark_id].n_expected,
                        "n_conclusive": children[benchmark_id].n_conclusive,
                        "n_inconclusive": children[benchmark_id].n_inconclusive,
                        "coverage": children[benchmark_id].coverage,
                    }
                    for benchmark_id, score in result.score_vector
                ]
                tasks = [
                    {
                        "benchmark_id": child.benchmark_id,
                        "task_id": outcome.get("task_id"),
                        "replicate": outcome.get("replicate"),
                        "measurement_seed": outcome.get("measurement_seed"),
                        "outcome": outcome.get("outcome"),
                        "score": outcome.get("score"),
                        "summary": outcome.get("summary"),
                        "constraints_pass": outcome.get("constraints_pass"),
                    }
                    for child in result.benchmark_results
                    for outcome in child.task_outcomes
                ]
                if result.aggregate_score is not None:
                    best = (
                        float(result.aggregate_score)
                        if best is None
                        else max(best, float(result.aggregate_score))
                    )
            started = _safe_number(started_times.get(candidate.candidate_id))
            ended = _safe_number(terminal_times.get(candidate.candidate_id))
            candidate_rows.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "native_candidate_id": candidate.native_candidate_id,
                    "candidate_ordinal": candidate.iteration,
                    "attempt_id": candidate.attempt_id,
                    "attempt_index": attempts[candidate.attempt_id].attempt_index,
                    "role": candidate.role,
                    "parent_candidate_id": candidate.parent_candidate_id,
                    "outcome": result.outcome if result is not None else None,
                    "score_vector": vector,
                    "aggregate_policy": (
                        dict(result.aggregate_policy)
                        if result is not None and result.aggregate_policy is not None
                        else None
                    ),
                    "aggregate_score": result.aggregate_score
                    if result is not None
                    else None,
                    "best_so_far": best if result is not None else None,
                    "coverage": result.coverage if result is not None else None,
                    "wall_seconds": (
                        max(0.0, ended - started)
                        if started is not None and ended is not None
                        else None
                    ),
                    "usage": usage.get(candidate.candidate_id),
                    "tasks": tasks,
                    "snapshot": {
                        "digest": candidate.snapshot_digest,
                        "artifact_kind": candidate.artifact_kind,
                        "store_ref": candidate.store_ref,
                    },
                    "diff": self._diff_descriptor(candidate),
                }
            )

        return {
            "meta": self._catalogue_entry(replay),
            "usage": _run_usage(replay),
            "attempts": [
                {
                    "attempt_id": attempt.attempt_id,
                    "attempt_index": attempt.attempt_index,
                    "state": attempt.state,
                    "kind": attempt.kind,
                    "gate_decision": attempt.gate_decision,
                    "candidate_ids": list(attempt.candidate_ids),
                    "started_at": attempt.started_at,
                    "sealed_at": attempt.sealed_at,
                    "terminal_at": attempt.terminal_at,
                }
                for attempt in replay.attempts
            ],
            "candidate_evaluations": candidate_rows,
            "lineage_graph": {
                "nodes": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "ordinal": candidate.iteration,
                        "role": candidate.role,
                        "outcome": (
                            delivered[candidate.candidate_id].outcome
                            if candidate.candidate_id in delivered
                            else None
                        ),
                        "aggregate_score": (
                            delivered[candidate.candidate_id].aggregate_score
                            if candidate.candidate_id in delivered
                            else None
                        ),
                    }
                    for candidate in replay.candidates
                ],
                "edges": [
                    {
                        "source": candidate.parent_candidate_id,
                        "target": candidate.candidate_id,
                        "kind": "parent",
                    }
                    for candidate in replay.candidates
                    if candidate.parent_candidate_id is not None
                ],
            },
            "development": self._development(replay),
            "held_out": self._held_out(replay),
        }

    @staticmethod
    def _attempt_cap(manifest: Mapping[str, object]) -> int | None:
        binding = manifest.get("arena_benchmark_integrations")
        value = binding.get("attempt_cap") if isinstance(binding, Mapping) else None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _diff_descriptor(candidate: CandidateRecord) -> dict[str, Any]:
        bases = []
        if candidate.parent_candidate_id is not None:
            bases.append({"value": "parent", "label": "Parent candidate"})
        bases.append({"value": "empty", "label": "Empty tree"})
        return {
            "available": candidate.artifact_kind == "git_bundle",
            "default_base": "parent" if candidate.parent_candidate_id else "empty",
            "bases": bases,
        }

    @staticmethod
    def _development(replay: CanonicalRunReplay) -> list[dict[str, Any]]:
        try:
            delivered = replay_delivered_development_results(
                replay.layout,
                benchmark_task_ids=replay.benchmark_task_ids,
            )
        except RunValidationError as exc:
            raise ArenaProjectionError("Arena development delivery is invalid") from exc
        projected: list[dict[str, Any]] = []
        usage = _usage_by_development(replay)
        for raw in delivered:
            result = raw.get("result")
            if not isinstance(result, Mapping):
                raise ArenaProjectionError("Arena development result is invalid")
            outcome = result.get("outcome")
            score = _safe_number(result.get("score")) if outcome == "measured" else None
            projected.append(
                {
                    "development_evaluation_id": raw.get("development_evaluation_id"),
                    "benchmark_id": raw.get("benchmark_id"),
                    "outcome": outcome,
                    "usage": usage.get(str(raw.get("development_evaluation_id"))),
                    "score_vector": [
                        {
                            "benchmark_id": raw.get("benchmark_id"),
                            "score": score,
                            "n_expected": result.get("n_expected"),
                            "n_conclusive": result.get("n_conclusive"),
                            "n_inconclusive": result.get("n_inconclusive"),
                            "coverage": result.get("coverage"),
                        }
                    ],
                    "tasks": [
                        {
                            key: item.get(key)
                            for key in ("task_id", "outcome", "score", "summary")
                        }
                        for item in result.get("task_results", ())
                        if isinstance(item, Mapping)
                    ],
                }
            )
        return projected

    def _held_out(self, replay: CanonicalRunReplay) -> dict[str, Any] | None:
        root = self.control_root.resolve(strict=True) / "held-out-evaluation"
        report_path = root / "report.json"
        selection_path = root / "selection.json"
        if not report_path.exists() and not selection_path.exists():
            return None
        if not selection_path.is_file() or (
            report_path.exists() and not report_path.is_file()
        ):
            raise ArenaProjectionError("Arena held-out publication is incomplete")
        selection = _json_object(selection_path, "Arena held-out selection")
        report = (
            _json_object(report_path, "Arena held-out report")
            if report_path.is_file()
            else None
        )
        benchmark_ids = tuple(replay.benchmark_task_ids)
        try:
            validate_held_out_publication(
                selection, report, benchmark_ids=benchmark_ids
            )
            candidate = replay.candidate_by_id(str(selection.get("candidate_id")))
            attempt = replay.attempt_by_id(candidate.attempt_id)
        except RunValidationError as exc:
            raise ArenaProjectionError("Arena held-out publication is invalid") from exc
        results = tuple(
            result
            for result in replay.terminal_results
            if result.candidate_id == candidate.candidate_id
            and result.candidate_evaluation_id
            == selection.get("candidate_evaluation_id")
        )
        freeze = replay.manifest.get("campaign_run_freeze")
        expected_policy = (
            dict(results[0].aggregate_policy)
            if len(results) == 1 and results[0].aggregate_policy is not None
            else None
        )
        if (
            len(results) != 1
            or attempt.state != "TERMINAL"
            or candidate.candidate_id not in attempt.candidate_ids
            or not isinstance(freeze, Mapping)
            or selection.get("source_run_id") != replay.layout.run_id
            or selection.get("campaign_id") != replay.layout.campaign
            or selection.get("campaign_freeze_id") != freeze.get("campaign_freeze_id")
            or selection.get("attempt_id") != candidate.attempt_id
            or selection.get("candidate_snapshot_digest") != candidate.snapshot_digest
            or selection.get("candidate_artifact_digest") != candidate.artifact_digest
            or selection.get("candidate_store_ref") != candidate.store_ref
            or selection.get("evaluation_snapshot_ref") != candidate.store_ref
            or selection.get("evaluation_snapshot_digest") != candidate.artifact_digest
            or selection.get("evaluation_snapshot_tree_sha") != candidate.tree_sha
            or selection.get("evaluation_snapshot_pinned_ref") != candidate.pinned_ref
            or selection.get("aggregate_policy") != expected_policy
            or selection.get("aggregate_score") != results[0].aggregate_score
        ):
            raise ArenaProjectionError("Arena held-out selection binding is invalid")
        common = {
            "candidate_id": candidate.candidate_id,
            "selection_digest": selection["selection_digest"],
            # Held-out task terminals and identifiers never cross this boundary.
            "tasks": [],
        }
        if report is None:
            return {
                **common,
                "state": "IN_PROGRESS",
                "terminal": False,
                "outcome": None,
                "score_vector": [],
                "aggregate_policy": None,
                "aggregate_score": None,
                "counts": None,
                "coverage": None,
                "report_digest": None,
            }
        vector = report.get("score_vector")
        if not isinstance(vector, list) or any(
            not isinstance(item, Mapping) for item in vector
        ):
            raise ArenaProjectionError("Arena held-out score vector is invalid")
        aggregate_policy = report.get("aggregate_policy")
        return {
            **common,
            "state": "TERMINAL",
            "terminal": True,
            "outcome": report.get("outcome"),
            "score_vector": [
                {
                    key: item.get(key)
                    for key in (
                        "benchmark_id",
                        "score",
                        "n_expected",
                        "n_conclusive",
                        "n_inconclusive",
                        "coverage",
                    )
                }
                for item in vector
            ],
            "aggregate_policy": (
                dict(aggregate_policy)
                if isinstance(aggregate_policy, Mapping)
                else None
            ),
            "aggregate_score": (
                report.get("aggregate_score")
                if isinstance(aggregate_policy, Mapping)
                else None
            ),
            "counts": {
                "expected": report.get("n_expected"),
                "measured": report.get("n_conclusive"),
                "inconclusive": report.get("n_inconclusive"),
            },
            "coverage": report.get("coverage"),
            "report_digest": report.get("report_digest"),
        }

    def diff(self, candidate_id: str, base: str) -> dict[str, Any]:
        replay, _experiment, _authority = self._context()
        by_id = {candidate.candidate_id: candidate for candidate in replay.candidates}
        candidate = by_id.get(candidate_id)
        if candidate is None:
            raise ArenaProjectionError("unknown Arena candidate")
        parent: CandidateRecord | None = None
        if base == "parent":
            if candidate.parent_candidate_id is None:
                raise ArenaProjectionError("candidate has no parent")
            parent = by_id.get(candidate.parent_candidate_id)
            if parent is None:
                raise ArenaProjectionError("candidate parent is unavailable")
        elif base != "empty":
            raise ArenaProjectionError("unknown Arena diff base")

        with tempfile.TemporaryDirectory(prefix="rsi-arena-diff-") as temporary:
            repository = Path(temporary) / "projection.git"
            self._git("init", "--bare", "--quiet", str(repository))
            current_ref = "refs/projection/current"
            self._fetch_bundle(repository, replay.layout, candidate, current_ref)
            if parent is None:
                command = (
                    "-C",
                    str(repository),
                    "diff-tree",
                    "--root",
                    "--patch",
                    "--no-color",
                    "--no-ext-diff",
                    "--no-textconv",
                    current_ref,
                )
                label = "Empty tree"
            else:
                parent_ref = "refs/projection/parent"
                self._fetch_bundle(repository, replay.layout, parent, parent_ref)
                command = (
                    "-C",
                    str(repository),
                    "diff",
                    "--patch",
                    "--no-color",
                    "--no-ext-diff",
                    "--no-textconv",
                    f"{parent_ref}^{{tree}}",
                    f"{current_ref}^{{tree}}",
                )
                label = f"Parent candidate #{parent.iteration}"
            raw, truncated = self._bounded_git_output(*command)
        return {
            "candidate_id": candidate.candidate_id,
            "base": base,
            "base_label": label,
            "text": raw.decode("utf-8", errors="replace"),
            "truncated": truncated,
            "no_changes": not raw,
        }

    @classmethod
    def _fetch_bundle(
        cls, repository: Path, layout: RunLayout, candidate: CandidateRecord, ref: str
    ) -> None:
        if not candidate.pinned_ref:
            raise ArenaProjectionError("candidate bundle ref is unavailable")
        bundle = layout.resolve_store_ref(candidate.store_ref)
        cls._git(
            "-C",
            str(repository),
            "fetch",
            "--quiet",
            str(bundle),
            f"{candidate.pinned_ref}:{ref}",
        )

    @staticmethod
    def _git(*args: str) -> None:
        try:
            completed = subprocess.run(
                ("git", *args),
                check=False,
                capture_output=True,
                env=_git_environment(),
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArenaProjectionError("candidate diff command failed") from exc
        if completed.returncode != 0:
            raise ArenaProjectionError("candidate diff command failed")

    @staticmethod
    def _bounded_git_output(*args: str) -> tuple[bytes, bool]:
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                ("git", *args),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_git_environment(),
            )
            assert process.stdout is not None
            deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
            chunks: list[bytes] = []
            size = 0
            truncated = False
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(timeout=remaining):
                        raise subprocess.TimeoutExpired(
                            ("git", *args), _GIT_TIMEOUT_SECONDS
                        )
                    chunk = os.read(
                        process.stdout.fileno(),
                        min(64 * 1024, _DIFF_MAX_BYTES + 1 - size),
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > _DIFF_MAX_BYTES:
                        truncated = True
                        process.kill()
                        break
            return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            raw = b"".join(chunks)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if process is not None:
                process.kill()
                process.wait()
            raise ArenaProjectionError("candidate diff command failed") from exc
        if return_code != 0 and not truncated:
            raise ArenaProjectionError("candidate diff command failed")
        return raw[:_DIFF_MAX_BYTES], truncated


def _git_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "LC_ALL": "C",
    }


__all__ = [
    "ArenaProjectionError",
    "ArenaRunAuthority",
    "ArenaRunProjection",
    "open_run_replay",
]
