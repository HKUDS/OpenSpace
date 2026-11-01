"""Trusted held-out evaluation outside the method-facing Arena service."""

from __future__ import annotations

import fcntl
import json
import math
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, Protocol, Sequence

from rsi_arena.contracts import ScopeSpec, TaskRef, canonical_digest
from rsi_arena.evaluation_gateway import (
    GatewayCandidate,
    TrustedEvaluationGateway,
)
from rsi_arena.experiment import (
    experiment_digest,
    read_experiment,
)
from rsi_arena.run_layout import (
    CampaignEvaluationResult,
    CandidateRecord,
    RunLayout,
    RunValidationError,
    TaskEvaluationOutcome,
    atomic_write_json,
)

HELD_OUT_EVALUATION_DIRECTORY = "held-out-evaluation"
SELECTION_SCHEMA = "rsi-arena-held-out-selection"
AUTHORIZATION_SCHEMA = "rsi-arena-held-out-authorization"
TASK_SCHEMA = "rsi-arena-held-out-task"
ATTEMPT_START_SCHEMA = "rsi-arena-held-out-task-attempt"
BENCHMARK_REPORT_SCHEMA = "rsi-arena-held-out-benchmark-report"
REPORT_SCHEMA = "rsi-arena-held-out-report"


class HeldOutEvaluationError(RuntimeError):
    """A trusted selection or its held-out evaluation cannot be formed."""


class HeldOutBenchmarkPackage(Protocol):
    benchmark_id: str
    freeze: object

    def held_out_task_ids_for_evaluator(self) -> tuple[str, ...]: ...

    def held_out_evaluation_adapter(
        self,
        *,
        layout: RunLayout,
        gateway: TrustedEvaluationGateway,
    ) -> tuple[object, tuple[TaskRef, ...]]: ...


def _json_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunValidationError(f"{label} is unreadable") from exc
    if not isinstance(payload, dict):
        raise RunValidationError(f"{label} is not an object")
    return payload


def _write_once(path: Path, payload: Mapping[str, object], label: str) -> None:
    if path.exists():
        if _json_object(path, label) != dict(payload):
            raise RunValidationError(f"{label} changed")
        return
    atomic_write_json(path, payload)


@contextmanager
def _held_out_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def assert_evaluation_resume_allowed(run_root: str | os.PathLike[str]) -> None:
    """Reject evaluation resume after held-out authority has been frozen."""

    held_out_root = Path(run_root) / HELD_OUT_EVALUATION_DIRECTORY
    if (held_out_root / "selection.json").exists() or (
        held_out_root / "authorization.json"
    ).exists():
        raise RunValidationError(
            "evaluation cannot resume after held-out execution was frozen"
        )


def _selection(
    composition: object,
    *,
    candidate_id: str,
) -> tuple[CampaignEvaluationResult, CandidateRecord, str]:
    recorder = composition.spine.recorder
    if recorder.has_incomplete_campaign_evaluations:
        raise HeldOutEvaluationError("attempt evaluation is still in progress")
    terminals = tuple(recorder.campaign_terminal_results())

    def attempt_is_terminal(result: CampaignEvaluationResult) -> bool:
        attempt = recorder.attempt(result.attempt_id)
        if attempt is None or result.candidate_id not in attempt.candidate_ids:
            raise RunValidationError(
                "held-out candidate is misbound to its owning attempt"
            )
        return attempt.state == "TERMINAL"

    candidate = recorder.candidate_by_id(candidate_id)
    matches = tuple(
        result for result in terminals if result.candidate_id == candidate.candidate_id
    )
    if len(matches) != 1:
        raise HeldOutEvaluationError(
            "explicit held-out candidate has no unique terminal result"
        )
    selected = matches[0]
    if not attempt_is_terminal(selected):
        raise HeldOutEvaluationError(
            "explicit held-out candidate belongs to a nonterminal attempt"
        )
    if selected.candidate_evaluation_id not in recorder.delivered_terminal_ids:
        raise HeldOutEvaluationError(
            "explicit held-out candidate was not delivered to the method"
        )
    if candidate.role == "control":
        raise HeldOutEvaluationError(
            "paired control candidates cannot be selected for held-out evaluation"
        )
    return selected, candidate, "method-explicit"


def _selection_payload(
    composition: object,
    result: CampaignEvaluationResult,
    candidate: CandidateRecord,
    selection_mode: str,
    evaluation_snapshot: Mapping[str, str],
) -> dict[str, object]:
    packages = composition.benchmark_packages
    first = next(iter(packages.values()))
    payload: dict[str, object] = {
        "schema": SELECTION_SCHEMA,
        "source_run_id": composition.layout.run_id,
        "campaign_id": composition.layout.campaign,
        "campaign_freeze_id": first.freeze.freeze_id,
        "candidate_id": candidate.candidate_id,
        "attempt_id": result.attempt_id,
        "candidate_evaluation_id": result.candidate_evaluation_id,
        "candidate_snapshot_digest": candidate.snapshot_digest,
        "candidate_artifact_digest": candidate.artifact_digest,
        "candidate_store_ref": candidate.store_ref,
        "selection_mode": selection_mode,
        "evaluation_snapshot_ref": evaluation_snapshot["ref"],
        "evaluation_snapshot_digest": evaluation_snapshot["digest"],
        "evaluation_snapshot_tree_sha": evaluation_snapshot["tree_sha"],
        "evaluation_snapshot_pinned_ref": evaluation_snapshot["pinned_ref"],
        "aggregate_policy": (
            dict(result.aggregate_policy)
            if result.aggregate_policy is not None
            else None
        ),
        "aggregate_score": result.aggregate_score,
    }
    payload["selection_digest"] = "sha256:" + canonical_digest(payload)
    return payload


def _evaluation_snapshot(
    composition: object, candidate: CandidateRecord
) -> Mapping[str, str]:
    if candidate.artifact_kind != "git_bundle":
        raise HeldOutEvaluationError("selection canonical snapshot is not a Git bundle")
    if not candidate.pinned_ref or not candidate.tree_sha:
        raise HeldOutEvaluationError("selection Git snapshot is incomplete")
    return {
        "ref": candidate.store_ref,
        "digest": candidate.artifact_digest,
        "tree_sha": candidate.tree_sha,
        "pinned_ref": candidate.pinned_ref,
    }


def _git(root: Path, *args: str) -> str:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "LC_ALL": "C",
    }
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return completed.stdout.strip()


def _restore_selection(
    composition: object,
    selection: Mapping[str, object],
    destination: Path,
) -> None:
    if destination.exists():
        observed = _git(destination, "rev-parse", "HEAD^{tree}")
        if selection["evaluation_snapshot_tree_sha"] != observed:
            raise RunValidationError("materialized held-out selection changed")
        return
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".selection-source-", dir=destination.parent)
    )
    try:
        _git(staging, "init", "-q")
        bundle = composition.layout.resolve_store_ref(
            str(selection["evaluation_snapshot_ref"])
        )
        _git(
            staging,
            "fetch",
            "-q",
            str(bundle),
            str(selection["evaluation_snapshot_pinned_ref"]),
        )
        _git(staging, "checkout", "-q", "--detach", "FETCH_HEAD")
        if (
            _git(staging, "rev-parse", "HEAD^{tree}")
            != selection["evaluation_snapshot_tree_sha"]
        ):
            raise RunValidationError("materialized held-out selection has another tree")
        os.rename(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _held_out_layout(composition: object, root: Path, benchmark_id: str) -> RunLayout:
    runtime = root / "runtime"
    for path in (
        root / "results",
        root / "trajectory" / "raw",
        runtime / "method",
        runtime / "trusted",
    ):
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    return RunLayout(
        root=root,
        runtime_root=runtime,
        campaign=composition.layout.campaign,
        method="trusted-held-out-evaluation",
        benchmark=benchmark_id,
        evaluation_seed="held-out",
        run_id=f"held-out-{composition.layout.run_id}",
    )


def _outcome_payload(
    selection: Mapping[str, object],
    benchmark_id: str,
    task_id: str,
    index: int,
    attempt: int,
    outcome: TaskEvaluationOutcome,
) -> dict[str, object]:
    return {
        "schema": TASK_SCHEMA,
        "selection_digest": selection["selection_digest"],
        "candidate_id": selection["candidate_id"],
        "attempt_id": selection["attempt_id"],
        "candidate_snapshot_digest": selection["candidate_snapshot_digest"],
        "benchmark_id": benchmark_id,
        "sample_index": index,
        "task_id": task_id,
        "attempt": attempt,
        "outcome": outcome.outcome,
        "score": outcome.score,
        "summary": outcome.summary,
        "artifact_refs": dict(outcome.artifact_refs),
        "provenance": dict(outcome.provenance),
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


def _load_outcome(
    path: Path,
    *,
    selection: Mapping[str, object],
    benchmark_id: str,
    task_id: str,
    index: int,
    attempt: int,
) -> dict[str, object]:
    payload = _json_object(path, "held-out task terminal")
    if (
        payload.get("schema") != TASK_SCHEMA
        or payload.get("selection_digest") != selection.get("selection_digest")
        or payload.get("candidate_id") != selection.get("candidate_id")
        or payload.get("attempt_id") != selection.get("attempt_id")
        or payload.get("candidate_snapshot_digest")
        != selection.get("candidate_snapshot_digest")
        or payload.get("benchmark_id") != benchmark_id
        or payload.get("sample_index") != index
        or payload.get("task_id") != task_id
        or payload.get("attempt") != attempt
        or payload.get("outcome") not in {"measured", "inconclusive"}
        or (
            payload.get("outcome") == "measured"
            and (
                isinstance(payload.get("score"), bool)
                or not isinstance(payload.get("score"), (int, float))
                or not math.isfinite(float(payload["score"]))
            )
        )
        or (
            payload.get("outcome") == "inconclusive"
            and payload.get("score") is not None
        )
    ):
        raise RunValidationError("held-out task terminal is misbound")
    return payload


def _attempt_start_payload(
    selection: Mapping[str, object],
    benchmark_id: str,
    task_id: str,
    index: int,
    attempt: int,
) -> dict[str, object]:
    return {
        "schema": ATTEMPT_START_SCHEMA,
        "selection_digest": selection["selection_digest"],
        "candidate_id": selection["candidate_id"],
        "attempt_id": selection["attempt_id"],
        "candidate_snapshot_digest": selection["candidate_snapshot_digest"],
        "benchmark_id": benchmark_id,
        "sample_index": index,
        "task_id": task_id,
        "attempt": attempt,
    }


def _task_attempts(
    attempts_root: Path,
    *,
    selection: Mapping[str, object],
    benchmark_id: str,
    task_id: str,
    index: int,
) -> list[tuple[int, dict[str, object] | None]]:
    task_root = attempts_root / f"{index:04d}"
    if not task_root.exists():
        return []
    records: list[tuple[int, dict[str, object] | None]] = []
    attempt = 1
    while True:
        start_path = task_root / f"attempt-{attempt:04d}-start.json"
        if not start_path.exists():
            break
        expected = _attempt_start_payload(
            selection, benchmark_id, task_id, index, attempt
        )
        if _json_object(start_path, "held-out task attempt") != expected:
            raise RunValidationError("held-out task attempt is misbound")
        terminal_path = task_root / f"attempt-{attempt:04d}-terminal.json"
        terminal = (
            _load_outcome(
                terminal_path,
                selection=selection,
                benchmark_id=benchmark_id,
                task_id=task_id,
                index=index,
                attempt=attempt,
            )
            if terminal_path.exists()
            else None
        )
        if terminal is None and any(
            task_root.glob(f"attempt-{attempt + 1:04d}-*.json")
        ):
            raise RunValidationError("held-out task attempts are not sequential")
        records.append((attempt, terminal))
        attempt += 1
    known = {
        path.name
        for ordinal, terminal in records
        for path in (
            task_root / f"attempt-{ordinal:04d}-start.json",
            *(
                (task_root / f"attempt-{ordinal:04d}-terminal.json",)
                if terminal is not None
                else ()
            ),
        )
    }
    if {path.name for path in task_root.glob("attempt-*.json")} != known:
        raise RunValidationError("held-out task attempts are not sequential")
    return records


def _evaluate_task_attempt(
    *,
    attempts_root: Path,
    selection: Mapping[str, object],
    benchmark_id: str,
    adapter: object,
    issued: GatewayCandidate,
    task: TaskRef,
    index: int,
    retry_inconclusive: bool,
) -> Mapping[str, object]:
    task_root = attempts_root / f"{index:04d}"
    task_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    records = _task_attempts(
        attempts_root,
        selection=selection,
        benchmark_id=benchmark_id,
        task_id=task.task_id,
        index=index,
    )
    measured = [
        terminal
        for _attempt, terminal in records
        if terminal is not None and terminal["outcome"] == "measured"
    ]
    if measured:
        return measured[0]
    pending = next(
        ((attempt, terminal) for attempt, terminal in records if terminal is None),
        None,
    )
    if pending is not None:
        attempt = pending[0]
    elif not records:
        attempt = 1
    elif retry_inconclusive:
        attempt = records[-1][0] + 1
    else:
        terminal = records[-1][1]
        assert terminal is not None
        return terminal

    start_path = task_root / f"attempt-{attempt:04d}-start.json"
    _write_once(
        start_path,
        _attempt_start_payload(selection, benchmark_id, task.task_id, index, attempt),
        "held-out task attempt",
    )
    try:
        terminal_outcome = adapter.evaluate_task(issued, task)
        if not isinstance(terminal_outcome, TaskEvaluationOutcome):
            raise TypeError("held-out adapter returned no task terminal")
    except Exception:
        terminal_outcome = TaskEvaluationOutcome(
            outcome="inconclusive",
            summary="trusted held-out evaluation failed",
        )
    terminal_path = task_root / f"attempt-{attempt:04d}-terminal.json"
    _write_once(
        terminal_path,
        _outcome_payload(
            selection,
            benchmark_id,
            task.task_id,
            index,
            attempt,
            terminal_outcome,
        ),
        "held-out task terminal",
    )
    return _load_outcome(
        terminal_path,
        selection=selection,
        benchmark_id=benchmark_id,
        task_id=task.task_id,
        index=index,
        attempt=attempt,
    )


def _report(
    selection: Mapping[str, object],
    benchmark_id: str,
    outcomes: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    scores = [
        float(outcome["score"])
        for outcome in outcomes
        if outcome["outcome"] == "measured"
    ]
    n_expected = len(outcomes)
    n_conclusive = len(scores)
    payload: dict[str, object] = {
        "schema": BENCHMARK_REPORT_SCHEMA,
        "selection_digest": selection["selection_digest"],
        "candidate_id": selection["candidate_id"],
        "attempt_id": selection["attempt_id"],
        "candidate_snapshot_digest": selection["candidate_snapshot_digest"],
        "benchmark_id": benchmark_id,
        "outcome": "measured" if scores else "inconclusive",
        "score": sum(scores) / len(scores) if scores else None,
        "n_expected": n_expected,
        "n_conclusive": n_conclusive,
        "n_inconclusive": n_expected - n_conclusive,
        "coverage": n_conclusive / n_expected,
        "task_terminals": [
            {
                "sample_index": outcome["sample_index"],
                "task_id": outcome["task_id"],
                "attempt": outcome["attempt"],
                "outcome": outcome["outcome"],
                "score": outcome["score"],
            }
            for outcome in outcomes
        ],
    }
    payload["report_digest"] = "sha256:" + canonical_digest(payload)
    return payload


def _campaign_report(
    selection: Mapping[str, object],
    benchmark_reports: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    score_vector = [
        {
            "benchmark_id": report["benchmark_id"],
            "score": report["score"],
            "n_expected": report["n_expected"],
            "n_conclusive": report["n_conclusive"],
            "n_inconclusive": report["n_inconclusive"],
            "coverage": report["coverage"],
        }
        for report in benchmark_reports
    ]
    measured_scores = [
        float(report["score"])
        for report in benchmark_reports
        if report["score"] is not None
    ]
    aggregate_policy = selection["aggregate_policy"]
    aggregate_score = (
        sum(measured_scores) / len(measured_scores)
        if aggregate_policy is not None and measured_scores
        else None
    )
    n_expected = sum(int(report["n_expected"]) for report in benchmark_reports)
    n_conclusive = sum(int(report["n_conclusive"]) for report in benchmark_reports)
    payload: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "selection_digest": selection["selection_digest"],
        "candidate_id": selection["candidate_id"],
        "attempt_id": selection["attempt_id"],
        "candidate_snapshot_digest": selection["candidate_snapshot_digest"],
        "outcome": "measured" if measured_scores else "inconclusive",
        "score_vector": score_vector,
        "aggregate_policy": aggregate_policy,
        "aggregate_score": aggregate_score,
        "n_expected": n_expected,
        "n_conclusive": n_conclusive,
        "n_inconclusive": n_expected - n_conclusive,
        "coverage": n_conclusive / n_expected,
        "benchmark_results": [dict(report) for report in benchmark_reports],
        "task_terminals": [
            {
                "benchmark_id": report["benchmark_id"],
                **dict(terminal),
            }
            for report in benchmark_reports
            for terminal in report["task_terminals"]
        ],
    }
    payload["report_digest"] = "sha256:" + canonical_digest(payload)
    return payload


def validate_held_out_publication(
    selection: Mapping[str, object],
    report: Mapping[str, object] | None,
    *,
    benchmark_ids: Sequence[str],
) -> None:
    """Validate canonical held-out publication bytes without exposing tasks."""

    selection_fields = {
        "schema",
        "source_run_id",
        "campaign_id",
        "campaign_freeze_id",
        "candidate_id",
        "attempt_id",
        "candidate_evaluation_id",
        "candidate_snapshot_digest",
        "candidate_artifact_digest",
        "candidate_store_ref",
        "selection_mode",
        "evaluation_snapshot_ref",
        "evaluation_snapshot_digest",
        "evaluation_snapshot_tree_sha",
        "evaluation_snapshot_pinned_ref",
        "aggregate_policy",
        "aggregate_score",
        "selection_digest",
    }
    selection_input = dict(selection)
    selection_digest = selection_input.pop("selection_digest", None)
    if (
        set(selection) != selection_fields
        or selection.get("schema") != SELECTION_SCHEMA
        or selection.get("selection_mode") != "method-explicit"
        or selection_digest != "sha256:" + canonical_digest(selection_input)
    ):
        raise RunValidationError("held-out selection is invalid")
    if report is None:
        return
    raw_benchmarks = report.get("benchmark_results")
    if not isinstance(raw_benchmarks, list) or len(raw_benchmarks) != len(
        benchmark_ids
    ):
        raise RunValidationError("held-out benchmark reports are invalid")
    rebuilt: list[Mapping[str, object]] = []
    for benchmark_id, raw in zip(benchmark_ids, raw_benchmarks, strict=True):
        if not isinstance(raw, Mapping) or raw.get("benchmark_id") != benchmark_id:
            raise RunValidationError("held-out benchmark report is misbound")
        terminals = raw.get("task_terminals")
        if not isinstance(terminals, list) or any(
            not isinstance(item, Mapping) for item in terminals
        ):
            raise RunValidationError("held-out task terminals are invalid")
        expected = _report(selection, benchmark_id, terminals)
        if dict(raw) != expected:
            raise RunValidationError("held-out benchmark report is noncanonical")
        rebuilt.append(raw)
    if dict(report) != _campaign_report(selection, rebuilt):
        raise RunValidationError("held-out campaign report is noncanonical")


def run_held_out_evaluation_for_composition(
    composition: object,
    *,
    candidate_id: str,
    retry_inconclusive: bool = False,
) -> Mapping[str, object]:
    """Freeze one authorized canonical candidate and run held-out tasks."""

    if not isinstance(retry_inconclusive, bool):
        raise TypeError("retry_inconclusive must be boolean")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise TypeError("candidate_id must be a non-empty string")
    # Fail before creating the held-out lock/root.  Selection is repeated under
    # the lock so concurrent terminal publications cannot weaken the boundary.
    _selection(composition, candidate_id=candidate_id)
    held_out_root = Path(composition.control_root) / HELD_OUT_EVALUATION_DIRECTORY
    with _held_out_lock(held_out_root):
        result, candidate, selection_mode = _selection(
            composition, candidate_id=candidate_id
        )
        evaluation_snapshot = _evaluation_snapshot(composition, candidate)
        selection = _selection_payload(
            composition,
            result,
            candidate,
            selection_mode,
            evaluation_snapshot,
        )
        selection_path = held_out_root / "selection.json"
        _write_once(selection_path, selection, "frozen held-out selection")

        benchmark_reports: list[Mapping[str, object]] = []
        for benchmark_id, raw_package in composition.benchmark_packages.items():
            package: HeldOutBenchmarkPackage = raw_package
            held_out_task_ids = package.held_out_task_ids_for_evaluator()
            if not held_out_task_ids:
                raise HeldOutEvaluationError(
                    f"benchmark {benchmark_id!r} has no held-out occurrences"
                )
            benchmark_root = held_out_root / "benchmarks" / benchmark_id
            layout = _held_out_layout(composition, benchmark_root, benchmark_id)
            selection_source = layout.trusted_runtime_root / "selection-source"
            _restore_selection(composition, selection, selection_source)
            gateway = TrustedEvaluationGateway(
                trusted_root=selection_source,
                scope=ScopeSpec(editable=("*",)),
                work_dir=layout.trusted_runtime_root / "gateway",
            )
            issued: GatewayCandidate = gateway.materialize(selection_source)
            if issued.tree_sha != selection["evaluation_snapshot_tree_sha"]:
                raise RunValidationError(
                    "held-out gateway changed the frozen selection"
                )

            adapter, tasks = package.held_out_evaluation_adapter(
                layout=layout,
                gateway=gateway,
            )
            if tuple(task.task_id for task in tasks) != held_out_task_ids or any(
                task.benchmark_id != benchmark_id for task in tasks
            ):
                raise RunValidationError("held-out adapter changed frozen membership")

            authorization = {
                "schema": AUTHORIZATION_SCHEMA,
                "selection_digest": selection["selection_digest"],
                "campaign_freeze_id": selection["campaign_freeze_id"],
                "benchmark_id": benchmark_id,
                "held_out_task_ids": list(held_out_task_ids),
            }
            _write_once(
                benchmark_root / "authorization.json",
                authorization,
                f"{benchmark_id} held-out authorization",
            )

            attempts_root = benchmark_root / "task-attempts"
            attempts_root.mkdir(mode=0o700, exist_ok=True)
            outcomes: list[Mapping[str, object]] = []
            for index, task in enumerate(tasks):
                outcomes.append(
                    _evaluate_task_attempt(
                        attempts_root=attempts_root,
                        selection=selection,
                        benchmark_id=benchmark_id,
                        adapter=adapter,
                        issued=issued,
                        task=task,
                        index=index,
                        retry_inconclusive=retry_inconclusive,
                    )
                )
            child_report = _report(selection, benchmark_id, outcomes)
            atomic_write_json(benchmark_root / "report.json", child_report)
            benchmark_reports.append(child_report)
        report = _campaign_report(selection, benchmark_reports)
        atomic_write_json(held_out_root / "report.json", report)
        return report


def run_held_out_evaluation(
    *,
    run_root: str | os.PathLike[str],
    candidate_id: str,
    retry_inconclusive: bool = False,
) -> Mapping[str, object]:
    """Experimenter entrypoint for one completed run's held-out evaluation."""

    from .product_composition import compose_resumed_generic_run

    resolved = read_experiment(run_root)
    composition = compose_resumed_generic_run(resolved, run_root=run_root)
    try:
        composition.bind_experiment(experiment_digest(resolved))
        return run_held_out_evaluation_for_composition(
            composition,
            candidate_id=candidate_id,
            retry_inconclusive=retry_inconclusive,
        )
    finally:
        composition.close()


__all__ = [
    "HELD_OUT_EVALUATION_DIRECTORY",
    "HeldOutEvaluationError",
    "assert_evaluation_resume_allowed",
    "run_held_out_evaluation",
]
