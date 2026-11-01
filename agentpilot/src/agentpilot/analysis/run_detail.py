"""Read-only entity-level projection of one run.

``RunReportBuilder`` answers "is this run improving".  This module answers "what
happened, and why did the policy choose it", which is what an inspection UI
needs: the archive lineage, the per-batch action timeline, the D1/D2/D3 decision
record behind a single action, and the evaluation that judged it.

Like the run report it consumes only canonical persistence ports, never a policy
implementation, and it derives no value that could feed back into the search
loop.  Facts the runtime does not persist are reported as ``None`` rather than
reconstructed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from agentpilot.domain import (
    ArmSlot,
    DirectionMatch,
    PolicyAction,
    PolicyDecisionPlan,
    PolicyMethod,
    StructuredDirection,
)
from agentpilot.persistence import BatchStatus, UnitOfWorkFactory
from agentpilot.search.read_model import SearchProgramRecord


@dataclass(frozen=True)
class ActorIdentity:
    """Persisted identity for a model-backed or external run actor.

    A worker runner is not necessarily a model.  Keeping the runner coordinates
    separate from ``provider``/``model`` prevents a UI from relabelling an
    adapter id as a model name when the environment did not persist one.
    """

    provider: str | None
    model: str | None
    runner_id: str | None
    scope_identity: str | None
    unavailable: tuple[str, ...]


@dataclass(frozen=True)
class ProposalDetail:
    """The selected proposer record retained in the carrier-neutral trace."""

    request_id: str | None
    proposal_id: str | None
    provider: str | None
    model: str | None
    candidate_count: int
    repair_feedback: str | None
    status: str | None
    reason: str | None


@dataclass(frozen=True)
class VisibleATaskEvidenceDetail:
    """Human-readable meaning of one visible development cell.

    Cell ids are useful join keys but are not useful labels.  The exact frozen
    search input already stores these briefs, so projecting them is both more
    faithful and safer than guessing a title from the id in the browser.
    """

    cell_id: str
    task_id: str
    title: str
    statement: str
    graded_answer: str | None
    observed_failure: str | None


@dataclass(frozen=True)
class RunMeta:
    """Run-wide constants a view needs once, not per row."""

    run_id: str
    method: PolicyMethod
    batch_count: int
    action_count: int
    objective: str
    evaluator: str
    metric_name: str
    metric_direction: str
    environment: str
    cell_ids: tuple[str, ...]
    visible_a_evidence: tuple[VisibleATaskEvidenceDetail, ...]
    search_budget: Mapping[str, float | int]
    proposer: ActorIdentity
    worker: ActorIdentity
    unavailable_fields: tuple[str, ...]


@dataclass(frozen=True)
class BatchDetail:
    batch_id: str
    label: str
    status: BatchStatus
    state_id: str
    created_at: datetime
    expected_rounds: int
    planned_action_ids: tuple[str, ...]
    dispatched_action_ids: tuple[str, ...]
    completed_action_ids: tuple[str, ...]
    cost_usd: float
    # UCB freezes its arm registry here; Memory freezes the whole document.
    arm_ids: tuple[str, ...]
    memory_content: str | None
    proposer: ActorIdentity
    worker: ActorIdentity


@dataclass(frozen=True)
class ProgramNode:
    """One admitted archive program, linked to the program it was born from."""

    program_id: str
    label: str
    archive_label: str
    parent_id: str
    commit_sha: str
    archive_seq: int
    primary_value: float | None
    cell_results: Mapping[str, bool | None]
    source_action_id: str | None
    source_batch_id: str | None
    parent_program_id: str | None
    parent_label: str | None
    available_at: datetime


@dataclass(frozen=True)
class DirectionText:
    direction_id: str
    failure_mechanism: str
    implementation_direction: str
    declared_scope: tuple[Mapping[str, Any], ...]
    new_paths: tuple[Mapping[str, str], ...]
    resolved_scope: tuple[str, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationDetail:
    """The B-tier measurement and the admission decision it supports.

    Admission and policy effectiveness are separate decisions and are kept as
    separate fields here so a view cannot present one as the other.
    """

    evidence_tier: str | None
    validity: str | None
    attribution: str | None
    signed_statistic: float | None
    primary_delta_mean: float | None
    primary_delta_lower: float | None
    constraints_pass: bool | None
    admission_fate: str | None
    admitted: bool | None
    child_commit_sha: str | None
    child_primary_value: float | None
    cell_results: Mapping[str, bool | None]
    cell_notes: Mapping[str, str]
    cell_diagnostics: Mapping[str, Mapping[str, Any]]
    completed_at: datetime | None


@dataclass(frozen=True)
class ExecutionDetail:
    worker_status: str | None
    adherence: str | None
    cost_usd: float | None
    result_commit_sha: str | None
    touched_files: tuple[str, ...]
    file_changes: tuple[Mapping[str, Any], ...]
    started_at: datetime | None
    completed_at: datetime | None
    duration_seconds: float | None
    rework_count: int | None
    harness_attempt_count: int | None
    harness_attempts: tuple[Mapping[str, Any], ...]
    diagnostic_ran: bool | None
    diagnostic_passed: bool | None
    diagnostic_command: tuple[str, ...]
    diagnostics: Mapping[str, Any]


@dataclass(frozen=True)
class ActionDetail:
    action_id: str
    batch_id: str
    decision_round_id: str
    ordinal: int
    attempt_number: int
    label: str
    parent_id: str
    parent_label: str
    parent_commit_sha: str
    created_at: datetime
    archive_seq: int | None
    archive_label: str
    lifecycle_stage: str
    direction: DirectionText
    proposer: ProposalDetail
    worker: ActorIdentity
    arm_id: str | None
    slot_id: str | None
    slot_kind: str | None
    match_kind: str | None
    match_distance: float | None
    effectiveness_label: str | None
    effectiveness_reason: str | None
    effectiveness_margin: float | None
    d1_trace: Mapping[str, Any]
    d2_trace: Mapping[str, Any]
    d3_trace: Mapping[str, Any]
    evaluation: EvaluationDetail
    execution: ExecutionDetail


@dataclass(frozen=True)
class ParentOption:
    """One D1 candidate with the index that ranked it."""

    parent_id: str
    label: str
    archive_seq: int | None
    commit_sha: str
    primary_value: float | None
    index: float | None
    eligible: bool
    tied: bool
    selected: bool


@dataclass(frozen=True)
class SlotOption:
    """One D2 generation slot, with the support index that won it a slot."""

    slot_id: str
    kind: str
    ordinal: int
    source_arm_id: str | None
    support_id: str | None
    index: float | None
    tied: bool


@dataclass(frozen=True)
class DirectionOption:
    """One D3 candidate: where it came from, how it matched, how it scored."""

    candidate_id: str
    direction: DirectionText
    slot_id: str | None
    slot_kind: str | None
    source_arm_id: str | None
    match_kind: str | None
    match_distance: float | None
    match_arm_id: str | None
    adherence_status: str | None
    adherence_reason: str | None
    index: float | None
    estimate: Mapping[str, Any]
    eligible: bool
    tied: bool
    selected: bool


@dataclass(frozen=True)
class DecisionDetail:
    """The full D1/D2/D3 record behind exactly one action."""

    action_id: str
    plan_id: str
    method: PolicyMethod
    parent_options: tuple[ParentOption, ...]
    parent_seed: int | None
    parent_conditional_probability: float | None
    slots: tuple[SlotOption, ...]
    allocation: Mapping[str, Any]
    direction_options: tuple[DirectionOption, ...]
    direction_seed: int | None
    direction_conditional_probability: float | None
    direction_ranking: Mapping[str, Any]


@dataclass(frozen=True)
class PlanningFailureDetail:
    batch_id: str
    decision_round_id: str
    round_index: int
    failure_kind: str
    attempts: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class EvidenceTierDetail:
    """How much evidence one visibility tier holds, and what it is for.

    The three tiers are not one record: A is the root seed's graded cells, B is
    the paired population the estimator declares, and C is a sealed commitment
    that never reveals its task list.  A size the run does not persist stays
    ``None`` so a view can say "not recorded" instead of inventing a number.
    """

    tier: str
    role: str
    cell_count: int | None
    source: str
    state: str | None


@dataclass(frozen=True)
class ProtocolDetail:
    """The evidence contract this run measured under."""

    tiers: tuple[EvidenceTierDetail, ...]
    estimator: str | None
    estimator_design: Mapping[str, Any]
    measured_actions: int
    sealed_state: str | None
    sealed_expected_blocks: int | None


@dataclass(frozen=True)
class RunDetail:
    meta: RunMeta
    protocol: ProtocolDetail
    batches: tuple[BatchDetail, ...]
    programs: tuple[ProgramNode, ...]
    actions: tuple[ActionDetail, ...]
    decisions: tuple[DecisionDetail, ...]
    planning_failures: tuple[PlanningFailureDetail, ...]


class RunDetailBuilder:
    """Project one run into the entities an inspection UI renders."""

    def __init__(self, unit_of_work: UnitOfWorkFactory):
        if not callable(unit_of_work):
            raise TypeError("unit_of_work must be callable")
        self._unit_of_work = unit_of_work

    def build(self, run_id: str) -> RunDetail:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be non-empty text")
        with self._unit_of_work() as work:
            batches = work.batches.list_for_run(run_id)
            programs = work.search_read_model.list_programs_for_run(run_id)
            costs = work.usage.list_costs_for_run(run_id)
            actions: list[PolicyAction] = []
            plans: list[PolicyDecisionPlan] = []
            failures = []
            snapshots = []
            for batch in batches:
                actions.extend(work.actions.list_for_batch(batch.batch_id))
                plans.extend(work.plans.list_for_batch(batch.batch_id))
                failures.extend(work.planning_failures.list_for_batch(batch.batch_id))
                snapshot = work.search_read_model.snapshot_for_batch(
                    run_id, batch.batch_id
                )
                if snapshot is not None:
                    snapshots.append(snapshot)
            effectiveness = {
                action.action_id: work.outcomes.effectiveness_for_action(
                    action.action_id
                )
                for action in actions
            }
            outcomes = {
                action.action_id: work.outcomes.outcome_for_action(action.action_id)
                for action in actions
            }
            workers = {
                action.action_id: work.outcomes.worker_for_action(action.action_id)
                for action in actions
            }
            transitions = {
                action.action_id: _transition_or_none(work.archive, action.action_id)
                for action in actions
            }
            sealed = _sealed_record(work, run_id)

        action_by_id = {action.action_id: action for action in actions}
        batch_by_id = {batch.batch_id: batch for batch in batches}
        cost_by_batch: dict[str, float] = {}
        for entry in costs:
            cost_by_batch[entry.batch_id] = (
                cost_by_batch.get(entry.batch_id, 0.0) + entry.cost_usd
            )

        program_nodes = _program_nodes(programs, action_by_id)
        program_by_parent = {item.parent_id: item for item in program_nodes}
        program_by_action = {
            item.source_action_id: item
            for item in program_nodes
            if item.source_action_id is not None
        }

        return RunDetail(
            meta=_meta(run_id, batches, actions, programs, snapshots),
            protocol=_protocol(programs, tuple(outcomes.values()), sealed),
            batches=tuple(
                _batch_detail(
                    batch,
                    ordinal=index,
                    cost_usd=cost_by_batch.get(batch.batch_id, 0.0),
                )
                for index, batch in enumerate(batches)
            ),
            programs=program_nodes,
            actions=tuple(
                _action_detail(
                    action,
                    ordinal=index,
                    batch=batch_by_id.get(action.batch_id),
                    parent_program=program_by_parent.get(action.parent_id),
                    archived_program=program_by_action.get(action.action_id),
                    effectiveness=effectiveness[action.action_id],
                    outcome=outcomes[action.action_id],
                    worker=workers[action.action_id],
                    transition=transitions[action.action_id],
                )
                for index, action in enumerate(actions)
            ),
            decisions=tuple(
                _decision_detail(plan, program_by_parent) for plan in plans
            ),
            planning_failures=tuple(_planning_failure(event) for event in failures),
        )


def _transition_or_none(archive: Any, action_id: str) -> Any:
    """Keep live, dispatched actions visible before archival finishes."""

    try:
        return archive.get_transition(action_id)
    except KeyError:
        return None


def _sealed_record(work: Any, run_id: str) -> Any:
    """Read the sealed C commitment when the run registered one."""

    try:
        return work.sealed.get(run_id)
    except KeyError:
        return None


def _protocol(
    programs: Sequence[SearchProgramRecord], outcomes: Sequence[Any], sealed: Any,
) -> ProtocolDetail:
    """Assemble the A/B/C contract from the three places that actually hold it."""

    root = next(
        (record for record in programs if record.source_action_id is None), None
    )
    visible_cells = len(root.evidence.cell_results) if root is not None else None

    measured = [outcome for outcome in outcomes if outcome is not None]
    paired_cells: set[str] = set()
    estimator: str | None = None
    design: Mapping[str, Any] = {}
    for outcome in measured:
        readout = outcome.child_readout
        if readout is not None:
            paired_cells.update(readout.cell_results)
        identity = outcome.estimator_identity
        estimator = estimator or _optional_text(identity.get("name"))
        candidate = identity.get("design")
        if isinstance(candidate, Mapping) and not design:
            design = dict(candidate)

    # The estimator declares the population it generalises to; the observed
    # union only covers cells this run happened to draw.
    declared = design.get("population")
    population = declared if isinstance(declared, int) else None

    sealed_state = sealed.state.value if sealed is not None else None
    expected_blocks = sealed.manifest.expected_blocks if sealed is not None else None

    tiers = (
        EvidenceTierDetail(
            tier="A",
            role="development",
            cell_count=visible_cells,
            source="root seed graded cells",
            state="visible" if visible_cells else None,
        ),
        EvidenceTierDetail(
            tier="B",
            role="paired measurement",
            cell_count=(
                population if population is not None else len(paired_cells) or None
            ),
            source=(
                "estimator population"
                if population is not None
                else "observed paired cells"
            ),
            state="measuring" if measured else None,
        ),
        EvidenceTierDetail(
            tier="C",
            role="sealed holdout",
            cell_count=expected_blocks if isinstance(expected_blocks, int) else None,
            source="sealed manifest" if sealed is not None else "not registered",
            state=sealed_state,
        ),
    )
    return ProtocolDetail(
        tiers=tiers,
        estimator=estimator,
        estimator_design=MappingProxyType(dict(design)),
        measured_actions=len(measured),
        sealed_state=sealed_state,
        sealed_expected_blocks=(
            expected_blocks if isinstance(expected_blocks, int) else None
        ),
    )


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _batch_actor_identities(batch: Any) -> tuple[ActorIdentity, ActorIdentity]:
    """Read actor identities from the frozen runtime, without adapter guesses."""

    if batch is None:
        proposer_provider = None
        proposer_model = None
        runner_id = None
        scope_identity = None
    else:
        try:
            run_identity = batch.runtime_identity["run_identity"]
            method_config = run_identity["policy"][batch.method.value]
            proposer_config = method_config["proposer"]
            runner = run_identity["action_runner"]
            proposer_provider = _optional_text(proposer_config["provider"])
            proposer_model = _optional_text(proposer_config["model"])
            runner_id = _optional_text(runner["runner_id"])
            scope_identity = _optional_text(runner["scope_identity"])
        except (KeyError, TypeError) as exc:
            raise ValueError(
                "batch runtime identity does not match the current contract"
            ) from exc
        if None in (proposer_provider, proposer_model, runner_id, scope_identity,):
            raise ValueError("batch runtime identity has an invalid actor identity")

    proposer = ActorIdentity(
        provider=proposer_provider,
        model=proposer_model,
        runner_id=None,
        scope_identity=None,
        unavailable=tuple(
            field
            for field, value in (
                ("provider", proposer_provider),
                ("model", proposer_model),
            )
            if value is None
        ),
    )

    worker = ActorIdentity(
        provider=None,
        model=None,
        runner_id=runner_id,
        scope_identity=scope_identity,
        unavailable=tuple(
            field
            for field, value in (
                ("provider", None),
                ("model", None),
                ("runner_id", runner_id),
                ("scope_identity", scope_identity),
            )
            if value is None
        ),
    )
    return proposer, worker


def _proposal_detail(action: PolicyAction) -> ProposalDetail:
    """Select the proposal record whose direction became this action."""

    trace = action.d2_trace
    raw_records = trace.get("generation_attempts")
    if not isinstance(raw_records, (list, tuple)):
        raw_records = trace.get("proposals")
    records = tuple(item for item in (raw_records or ()) if isinstance(item, Mapping))
    selected = next(
        (
            item
            for item in records
            if item.get("direction_id") == action.direction.direction_id
        ),
        None,
    )
    selected = selected or {}
    provider = _optional_text(selected.get("provider"))
    model = _optional_text(selected.get("model"))
    return ProposalDetail(
        request_id=_optional_text(selected.get("request_id")),
        proposal_id=_optional_text(selected.get("proposal_id")),
        provider=provider,
        model=model,
        candidate_count=len(records),
        repair_feedback=_optional_text(selected.get("repair_feedback")),
        status=_optional_text(selected.get("status")),
        reason=_optional_text(selected.get("reason")),
    )


def _meta(
    run_id: str,
    batches: Sequence[Any],
    actions: Sequence[PolicyAction],
    programs: Sequence[SearchProgramRecord],
    snapshots: Sequence[Any] = (),
) -> RunMeta:
    compatibility = programs[0].evidence.compatibility if programs else None
    cells: set[str] = set()
    for record in programs:
        cells.update(record.evidence.cell_results)
    evidence_by_cell_id: dict[str, VisibleATaskEvidenceDetail] = {}
    search_budget: Mapping[str, float | int] = MappingProxyType({})
    for snapshot in snapshots:
        search_budget = MappingProxyType(dict(snapshot.budget))
        for item in snapshot.visible_a_evidence:
            cells.add(item.cell_id)
            evidence_by_cell_id[item.cell_id] = VisibleATaskEvidenceDetail(
                cell_id=item.cell_id,
                task_id=item.task_id,
                title=item.title,
                statement=item.statement,
                graded_answer=item.graded_answer,
                observed_failure=item.observed_failure,
            )
    batch = batches[0] if batches else None
    proposer, worker = _batch_actor_identities(batch)
    for action in actions:
        selected = _proposal_detail(action)
        if selected.provider is not None or selected.model is not None:
            proposer = ActorIdentity(
                provider=selected.provider,
                model=selected.model,
                runner_id=None,
                scope_identity=None,
                unavailable=tuple(
                    field
                    for field, value in (
                        ("provider", selected.provider),
                        ("model", selected.model),
                    )
                    if value is None
                ),
            )
            break
    return RunMeta(
        run_id=run_id,
        method=batches[0].method if batches else PolicyMethod.MEMORY,
        batch_count=len(batches),
        action_count=len(actions),
        objective=compatibility.objective_id if compatibility is not None else "",
        evaluator=compatibility.evaluator_id if compatibility is not None else "",
        metric_name=(
            compatibility.metric_schema_id if compatibility is not None else ""
        ),
        metric_direction=(
            compatibility.metric_direction if compatibility is not None else "maximize"
        ),
        environment=(compatibility.environment_id if compatibility is not None else ""),
        cell_ids=tuple(sorted(cells)),
        visible_a_evidence=tuple(
            evidence_by_cell_id[cell_id] for cell_id in sorted(evidence_by_cell_id)
        ),
        search_budget=search_budget,
        proposer=proposer,
        worker=worker,
        unavailable_fields=(
            "worker.provider",
            "worker.model",
            "commit.subject",
            "commit.message",
            "execution.step_count",
            "execution.transcript",
        ),
    )


def _batch_detail(batch: Any, *, ordinal: int, cost_usd: float) -> BatchDetail:
    snapshot = batch.carrier_snapshot or {}
    content = snapshot.get("content")
    proposer, worker = _batch_actor_identities(batch)
    return BatchDetail(
        batch_id=batch.batch_id,
        label=f"Batch {ordinal + 1}",
        status=batch.status,
        state_id=batch.state_id,
        created_at=batch.created_at,
        expected_rounds=batch.expected_rounds,
        planned_action_ids=tuple(batch.action_ids),
        dispatched_action_ids=tuple(batch.dispatched_action_ids),
        completed_action_ids=tuple(batch.completed_action_ids),
        cost_usd=cost_usd,
        arm_ids=tuple(snapshot.get("arm_ids") or ()),
        memory_content=content if isinstance(content, str) else None,
        proposer=proposer,
        worker=worker,
    )


def _program_nodes(
    programs: Sequence[SearchProgramRecord], action_by_id: Mapping[str, PolicyAction],
) -> tuple[ProgramNode, ...]:
    """Link each admitted program to the program its action started from."""

    by_parent_id = {record.parent.candidate.parent_id: record for record in programs}
    nodes = []
    for record in programs:
        candidate = record.parent.candidate
        source = (
            action_by_id.get(record.source_action_id)
            if record.source_action_id is not None
            else None
        )
        parent_record = (
            by_parent_id.get(source.parent_id) if source is not None else None
        )
        # Warm-start archive parents intentionally have no *local* source action;
        # archive sequence zero, not source-action absence, is the root identity.
        root = record.parent.admitted_seq == 0
        archive_label = "Root" if root else f"Archive #{record.parent.admitted_seq}"
        if parent_record is None:
            parent_label = None
        elif parent_record.parent.admitted_seq == 0:
            parent_label = "Root"
        else:
            parent_label = f"Archive #{parent_record.parent.admitted_seq}"
        nodes.append(
            ProgramNode(
                program_id=record.record_id,
                label=(
                    "Root program"
                    if root
                    else f"Admitted program #{record.parent.admitted_seq}"
                ),
                archive_label=archive_label,
                parent_id=candidate.parent_id,
                commit_sha=candidate.commit_sha,
                archive_seq=record.parent.admitted_seq,
                primary_value=record.evidence.primary_value,
                cell_results=MappingProxyType(dict(record.evidence.cell_results)),
                source_action_id=record.source_action_id,
                source_batch_id=source.batch_id if source is not None else None,
                parent_program_id=(
                    parent_record.record_id if parent_record is not None else None
                ),
                parent_label=parent_label,
                available_at=record.parent.available_at,
            )
        )
    return tuple(sorted(nodes, key=lambda item: item.archive_seq))


def _direction_text(direction: StructuredDirection) -> DirectionText:
    return DirectionText(
        direction_id=direction.direction_id,
        failure_mechanism=direction.failure_mechanism,
        implementation_direction=direction.implementation_direction,
        declared_scope=tuple(
            MappingProxyType(
                {
                    "kind": target.kind.value,
                    "path": target.path,
                    "symbols": tuple(target.symbols),
                }
            )
            for target in direction.declared_scope
        ),
        new_paths=tuple(
            MappingProxyType({"path": item.path, "role": item.role})
            for item in direction.new_paths
        ),
        resolved_scope=tuple(direction.resolved_scope),
        evidence_refs=tuple(direction.evidence_refs),
    )


def _execution_detail(worker: Any) -> ExecutionDetail:
    diagnostics = dict(worker.diagnostics) if worker is not None else {}
    reworks = diagnostics.get("reworks")
    if isinstance(reworks, bool) or not isinstance(reworks, int) or reworks < 0:
        reworks = None
    harness_attempts = diagnostics.get("harness_attempts")
    harness_attempt_count = (
        len(harness_attempts) if isinstance(harness_attempts, (list, tuple)) else None
    )
    projected_harness_attempts = (
        tuple(
            MappingProxyType(dict(item))
            for item in harness_attempts
            if isinstance(item, Mapping)
        )
        if isinstance(harness_attempts, (list, tuple))
        else ()
    )
    diagnostic_ran = diagnostics.get("ran_after_edit")
    if not isinstance(diagnostic_ran, bool):
        diagnostic_ran = None
    diagnostic_passed = diagnostics.get("passed")
    if not isinstance(diagnostic_passed, bool):
        diagnostic_passed = None
    command = diagnostics.get("command")
    diagnostic_command = (
        tuple(item for item in command if isinstance(item, str))
        if isinstance(command, (list, tuple))
        else ()
    )
    started_at = worker.started_at if worker is not None else None
    completed_at = worker.completed_at if worker is not None else None
    return ExecutionDetail(
        worker_status=worker.status.value if worker is not None else None,
        adherence=worker.adherence.value if worker is not None else None,
        cost_usd=worker.cost_usd if worker is not None else None,
        result_commit_sha=worker.result_commit_sha if worker is not None else None,
        touched_files=tuple(worker.touched_files) if worker is not None else (),
        file_changes=(
            tuple(
                MappingProxyType(
                    {
                        "change": item.kind.value,
                        "file": item.file,
                        "symbols": tuple(item.symbols),
                        "role": item.role,
                        "from": item.from_file,
                    }
                )
                for item in worker.file_changes
            )
            if worker is not None
            else ()
        ),
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=(
            (completed_at - started_at).total_seconds()
            if started_at is not None and completed_at is not None
            else None
        ),
        rework_count=reworks,
        harness_attempt_count=harness_attempt_count,
        harness_attempts=projected_harness_attempts,
        diagnostic_ran=diagnostic_ran,
        diagnostic_passed=diagnostic_passed,
        diagnostic_command=diagnostic_command,
        diagnostics=MappingProxyType(diagnostics),
    )


def _action_detail(
    action: PolicyAction,
    *,
    ordinal: int,
    batch: Any,
    parent_program: ProgramNode | None,
    archived_program: ProgramNode | None,
    effectiveness: Any,
    outcome: Any,
    worker: Any,
    transition: Any,
) -> ActionDetail:
    readout = outcome.child_readout if outcome is not None else None
    match: DirectionMatch | None = action.direction_match
    slot: ArmSlot | None = action.source_slot
    _, worker_identity = _batch_actor_identities(batch)
    attestation = worker.execution_attestation if worker is not None else None
    attested_runner = (
        _optional_text(attestation.runner_id) if attestation is not None else None
    )
    if attested_runner is not None:
        worker_identity = ActorIdentity(
            provider=worker_identity.provider,
            model=worker_identity.model,
            runner_id=attested_runner,
            scope_identity=worker_identity.scope_identity,
            unavailable=tuple(
                field
                for field, value in (
                    ("provider", worker_identity.provider),
                    ("model", worker_identity.model),
                    ("runner_id", attested_runner),
                    ("scope_identity", worker_identity.scope_identity),
                )
                if value is None
            ),
        )
    parent_label = (
        parent_program.archive_label if parent_program else "Unarchived parent"
    )
    if archived_program is not None:
        archive_seq = archived_program.archive_seq
        archive_label = archived_program.archive_label
    elif transition is None:
        archive_seq = None
        archive_label = "Admission pending"
    elif transition.admitted:
        archive_seq = None
        archive_label = "Admitted; archive entry unavailable"
    else:
        archive_seq = None
        archive_label = "Not admitted"
    completed_ids = set(batch.completed_action_ids) if batch is not None else set()
    dispatched_ids = set(batch.dispatched_action_ids) if batch is not None else set()
    if (
        action.action_id in completed_ids
        or outcome is not None
        or worker is not None
        or transition is not None
    ):
        lifecycle_stage = "completed"
    elif action.action_id in dispatched_ids:
        lifecycle_stage = "running"
    else:
        lifecycle_stage = "planned"
    return ActionDetail(
        action_id=action.action_id,
        batch_id=action.batch_id,
        decision_round_id=action.decision_round_id,
        ordinal=ordinal,
        attempt_number=ordinal + 1,
        label=f"Attempt #{ordinal + 1}",
        parent_id=action.parent_id,
        parent_label=parent_label,
        parent_commit_sha=action.parent_commit_sha,
        created_at=action.created_at,
        archive_seq=archive_seq,
        archive_label=archive_label,
        lifecycle_stage=lifecycle_stage,
        direction=_direction_text(action.direction),
        proposer=_proposal_detail(action),
        worker=worker_identity,
        arm_id=action.arm_id,
        slot_id=slot.slot_id if slot is not None else None,
        slot_kind=slot.kind.value if slot is not None else None,
        match_kind=match.kind.value if match is not None else None,
        match_distance=match.distance if match is not None else None,
        effectiveness_label=(
            effectiveness.label.value if effectiveness is not None else None
        ),
        effectiveness_reason=(
            effectiveness.reason if effectiveness is not None else None
        ),
        effectiveness_margin=(
            effectiveness.margin if effectiveness is not None else None
        ),
        d1_trace=MappingProxyType(dict(action.d1_trace)),
        d2_trace=MappingProxyType(dict(action.d2_trace)),
        d3_trace=MappingProxyType(dict(action.d3_trace)),
        evaluation=EvaluationDetail(
            evidence_tier=(
                outcome.evidence_tier.value if outcome is not None else None
            ),
            validity=outcome.validity.value if outcome is not None else None,
            attribution=outcome.attribution.value if outcome is not None else None,
            signed_statistic=(
                outcome.signed_effectiveness_statistic if outcome is not None else None
            ),
            primary_delta_mean=(
                outcome.primary_delta_mean if outcome is not None else None
            ),
            primary_delta_lower=(
                outcome.primary_delta_lower if outcome is not None else None
            ),
            constraints_pass=(
                outcome.constraints_pass if outcome is not None else None
            ),
            admission_fate=outcome.admission_fate if outcome is not None else None,
            admitted=transition.admitted if transition is not None else None,
            child_commit_sha=(
                transition.child_commit_sha if transition is not None else None
            ),
            child_primary_value=(
                readout.absolute_primary_value if readout is not None else None
            ),
            cell_results=MappingProxyType(
                dict(readout.cell_results) if readout is not None else {}
            ),
            cell_notes=MappingProxyType(
                dict(readout.cell_notes) if readout is not None else {}
            ),
            cell_diagnostics=MappingProxyType(
                dict(outcome.cell_diagnostics) if outcome is not None else {}
            ),
            completed_at=outcome.completed_at if outcome is not None else None,
        ),
        execution=_execution_detail(worker),
    )


def _support_id(slot: ArmSlot, ordinal: int) -> str:
    """Rebuild the key D2 used for this slot inside its support index map."""

    if slot.source_arm_id is not None:
        return f"existing:{slot.source_arm_id}"
    return f"discover:{ordinal}"


def _decision_detail(
    plan: PolicyDecisionPlan,
    program_by_parent: Mapping[str, ProgramNode] | None = None,
) -> DecisionDetail:
    program_by_parent = program_by_parent or {}
    parent_trace = plan.parent_selection.trace
    indices = dict(parent_trace.indices)
    eligible = set(parent_trace.eligible_ids)
    ties = set(parent_trace.tie_ids)
    parent_options = tuple(
        ParentOption(
            parent_id=candidate.parent_id,
            label=(
                program_by_parent[candidate.parent_id].archive_label
                if candidate.parent_id in program_by_parent
                else "Unarchived parent"
            ),
            archive_seq=(
                program_by_parent[candidate.parent_id].archive_seq
                if candidate.parent_id in program_by_parent
                else None
            ),
            commit_sha=candidate.commit_sha,
            primary_value=candidate.primary_value,
            index=indices.get(candidate.parent_id),
            eligible=candidate.parent_id in eligible,
            tied=candidate.parent_id in ties,
            selected=candidate.parent_id == plan.parent_selection.parent_id,
        )
        for candidate in plan.parent_slate.candidates
    )

    allocation = plan.arm_allocation
    allocation_trace: Mapping[str, Any] = (
        allocation.trace if allocation is not None else {}
    )
    inner = allocation_trace.get("allocation")
    inner = inner if isinstance(inner, Mapping) else allocation_trace
    support_indices = inner.get("support_indices")
    support_indices = support_indices if isinstance(support_indices, Mapping) else {}
    tie_sets = inner.get("tie_sets") or ()
    tied_supports = {item for group in tie_sets for item in group}
    discover_ordinal = 0
    slots: list[SlotOption] = []
    for slot in allocation.slots if allocation is not None else ():
        if slot.source_arm_id is None:
            support = _support_id(slot, discover_ordinal)
            discover_ordinal += 1
        else:
            support = _support_id(slot, 0)
        slots.append(
            SlotOption(
                slot_id=slot.slot_id,
                kind=slot.kind.value,
                ordinal=slot.ordinal,
                source_arm_id=slot.source_arm_id,
                support_id=support,
                index=support_indices.get(support),
                tied=support in tied_supports,
            )
        )

    direction_trace = plan.direction_selection.trace
    direction_indices = direction_trace.get("indices")
    direction_indices = (
        direction_indices if isinstance(direction_indices, Mapping) else {}
    )
    estimates = direction_trace.get("estimates")
    estimates = estimates if isinstance(estimates, Mapping) else {}
    # Only a carrier that screens its slate publishes an eligibility set; when
    # the key is absent the carrier ran no such gate and every candidate stands.
    raw_eligible = direction_trace.get("eligible_candidate_ids")
    eligible_candidates = None if raw_eligible is None else set(raw_eligible)
    candidate_ties = set(direction_trace.get("tie_ids") or ())
    direction_options = tuple(
        DirectionOption(
            candidate_id=candidate.candidate_id,
            direction=_direction_text(candidate.direction),
            slot_id=(
                candidate.source_slot.slot_id
                if candidate.source_slot is not None
                else None
            ),
            slot_kind=(
                candidate.source_slot.kind.value
                if candidate.source_slot is not None
                else None
            ),
            source_arm_id=(
                candidate.source_slot.source_arm_id
                if candidate.source_slot is not None
                else None
            ),
            match_kind=(
                candidate.match.kind.value if candidate.match is not None else None
            ),
            match_distance=(
                candidate.match.distance if candidate.match is not None else None
            ),
            match_arm_id=(
                candidate.match.arm_id if candidate.match is not None else None
            ),
            adherence_status=(
                candidate.adherence.status.value
                if candidate.adherence is not None
                else None
            ),
            adherence_reason=(
                candidate.adherence.reason if candidate.adherence is not None else None
            ),
            index=direction_indices.get(candidate.candidate_id),
            estimate=MappingProxyType(
                dict(estimates.get(candidate.candidate_id) or {})
            ),
            eligible=(
                True
                if eligible_candidates is None
                else candidate.candidate_id in eligible_candidates
            ),
            tied=candidate.candidate_id in candidate_ties,
            selected=(candidate.candidate_id == plan.direction_selection.candidate_id),
        )
        for candidate in plan.direction_slate.candidates
    )

    # Memory ranks a slate instead of scoring it, so its ordering lives in the
    # trace rather than in per-candidate indices.
    ranking = {
        key: direction_trace.get(key)
        for key in ("ranking", "selection")
        if direction_trace.get(key) is not None
    }

    return DecisionDetail(
        action_id=plan.action.action_id,
        plan_id=plan.plan_id,
        method=plan.method,
        parent_options=parent_options,
        parent_seed=parent_trace.seed,
        parent_conditional_probability=(
            None
            if plan.method is PolicyMethod.MEMORY
            else parent_trace.conditional_probability
        ),
        slots=tuple(slots),
        allocation=MappingProxyType(
            {
                key: inner.get(key)
                for key in (
                    "cold_start",
                    "generation_capacity",
                    "discover_quota",
                    "selected_discover_count",
                    "discover_supported",
                    "seed",
                )
                if inner.get(key) is not None
            }
        ),
        direction_options=direction_options,
        direction_seed=direction_trace.get("seed"),
        direction_conditional_probability=direction_trace.get(
            "conditional_probability"
        ),
        direction_ranking=MappingProxyType(ranking),
    )


def _planning_failure(event: Any) -> PlanningFailureDetail:
    return PlanningFailureDetail(
        batch_id=event.batch_id,
        decision_round_id=event.decision_round_id,
        round_index=event.round_index,
        failure_kind=event.failure_kind,
        attempts=tuple(
            MappingProxyType(
                {
                    "source_slot_id": attempt.source_slot_id,
                    "source_slot_kind": attempt.source_slot_kind.value,
                    "source_arm_id": attempt.source_arm_id,
                    "slot_ordinal": attempt.slot_ordinal,
                    "repair_ordinal": attempt.repair_ordinal,
                    "status": attempt.status.value,
                    "reason": attempt.reason,
                    "match_kind": (
                        attempt.match_kind.value
                        if attempt.match_kind is not None
                        else None
                    ),
                    "matched_arm_id": attempt.matched_arm_id,
                }
            )
            for attempt in event.slot_attempts
        ),
    )


__all__ = [
    "ActionDetail",
    "BatchDetail",
    "DecisionDetail",
    "DirectionOption",
    "DirectionText",
    "EvaluationDetail",
    "ExecutionDetail",
    "ParentOption",
    "PlanningFailureDetail",
    "ProgramNode",
    "RunDetail",
    "RunDetailBuilder",
    "RunMeta",
    "SlotOption",
]
