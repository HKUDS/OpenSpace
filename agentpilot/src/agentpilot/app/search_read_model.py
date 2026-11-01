"""Durable materialization and loading of canonical search inputs."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Mapping

from agentpilot.app.archive import require_admissible_child
from agentpilot.app.contracts import ExecutionResult, TerminalBatchError
from agentpilot.app.inputs import SearchInputSnapshot, SearchInputSource
from agentpilot.domain import (
    CompatibilityKey,
    EvidenceCutoff,
    EvidenceTier,
    ParentCandidate,
    ParentRepositoryMap,
    StateDescriptor,
    SnapshotTraceBinding,
    TraceEvidenceManifest,
    VisibleATaskEvidence,
    stable_id,
    utc_now,
)
from agentpilot.domain.common import require_aware, require_text
from agentpilot.persistence import (
    BatchStatus,
    PersistenceConflict,
    SearchReadModelRepository,
    UnitOfWorkFactory,
)
from agentpilot.search import (
    DirectionHistoryRecord,
    FrozenSearchInputRecord,
    ParentRecord,
    ProgramEvidence,
    SearchFact,
    SearchProgramRecord,
    resolve_rejected_candidate_manifest_id,
)


TRACE_EVIDENCE_UNAVAILABLE = "TRACE_EVIDENCE_UNAVAILABLE"


class TraceEvidenceUnavailable(ValueError):
    """A frozen planning input contains no usable Visible-A parent trace."""

    code = TRACE_EVIDENCE_UNAVAILABLE

    def __init__(self, message: str = "no valid readable Visible-A parent trace exists") -> None:
        super().__init__(f"{self.code}: {message}")


class CanonicalSearchReadModelWriter:
    """Write explicit root and admitted-child evaluation readouts."""

    _MATERIALIZABLE = {BatchStatus.MERGED, BatchStatus.POLICY_UPDATED}

    def __init__(self, unit_of_work: UnitOfWorkFactory) -> None:
        if not callable(unit_of_work):
            raise TypeError("unit_of_work must be callable")
        self.unit_of_work = unit_of_work

    def seed_root(
        self,
        *,
        run_id: str,
        candidate: ParentCandidate,
        compatibility: CompatibilityKey,
        available_at: datetime,
        cell_results: Mapping[str, bool | None],
        cell_notes: Mapping[str, str] | None = None,
        cell_excerpts: Mapping[str, str] | None = None,
        fact_payload: Mapping[str, Any] | None = None,
        searchable: bool = True,
    ) -> SearchProgramRecord:
        require_text(run_id, "run_id")
        require_aware(available_at, "available_at")
        record = self._record(
            run_id=run_id,
            candidate=candidate,
            compatibility=compatibility,
            archive_seq=0,
            available_at=available_at,
            cell_results=cell_results,
            cell_notes=cell_notes,
            cell_excerpts=cell_excerpts,
            fact_payload=fact_payload,
            searchable=searchable,
            source_action_id=None,
            fact_kind="root_program",
            evidence_tier=EvidenceTier.VISIBLE_A,
        )
        with self.unit_of_work() as work:
            return self._store_program(work.search_read_model, record)

    def seed_warm_start(
        self,
        *,
        run_id: str,
        candidate: ParentCandidate,
        compatibility: CompatibilityKey,
        archive_seq: int,
        available_at: datetime,
        cell_results: Mapping[str, bool | None],
        cell_notes: Mapping[str, str] | None = None,
        fact_payload: Mapping[str, Any] | None = None,
    ) -> SearchProgramRecord:
        """Seed one externally admitted parent with explicit provenance.

        Warm-start parents are trusted operator-authored experiment inputs.  They
        intentionally have no local ``source_action_id``: that coordinate names
        actions in this run, while their originating action belongs to another
        immutable run and is retained in ``fact_payload`` instead.
        """

        require_text(run_id, "run_id")
        require_aware(available_at, "available_at")
        if isinstance(archive_seq, bool) or archive_seq < 1:
            raise ValueError("warm-start archive_seq must be positive")
        record = self._record(
            run_id=run_id,
            candidate=candidate,
            compatibility=compatibility,
            archive_seq=archive_seq,
            available_at=available_at,
            cell_results=cell_results,
            cell_notes=cell_notes,
            fact_payload=fact_payload,
            searchable=True,
            source_action_id=None,
            fact_kind="admitted_program",
            evidence_tier=EvidenceTier.ADAPTIVE_B,
        )
        with self.unit_of_work() as work:
            return self._store_program(work.search_read_model, record)

    def materialize_batch(
        self,
        *,
        batch_id: str,
    ) -> tuple[SearchProgramRecord, ...]:
        """Idempotently project every admitted action in durable action order."""

        require_text(batch_id, "batch_id")
        with self.unit_of_work() as work:
            batch = work.batches.get(batch_id)
            if batch.status not in self._MATERIALIZABLE:
                raise ValueError("child materialization requires a merged batch")
            records = []
            for action_id in batch.action_ids:
                try:
                    transition = work.archive.get_transition(action_id)
                except KeyError as exc:
                    raise TerminalBatchError(
                        "merged batch is missing an archive transition"
                    ) from exc
                if transition.admitted:
                    records.append(
                        self._materialize_admitted_child(
                            work,
                            batch=batch,
                            action_id=action_id,
                        )
                    )
            return tuple(records)

    def materialize_admitted_child(
        self,
        *,
        batch_id: str,
        action_id: str,
    ) -> SearchProgramRecord:
        """Project one admitted child from its persisted evaluator readout."""

        require_text(batch_id, "batch_id")
        require_text(action_id, "action_id")
        with self.unit_of_work() as work:
            batch = work.batches.get(batch_id)
            if batch.status not in self._MATERIALIZABLE:
                raise ValueError("child materialization requires a merged batch")
            return self._materialize_admitted_child(
                work,
                batch=batch,
                action_id=action_id,
            )

    def _materialize_admitted_child(
        self,
        work,
        *,
        batch,
        action_id: str,
    ) -> SearchProgramRecord:
        try:
            action = work.actions.get(action_id)
            state = work.states.get(batch.state_id)
            transition = work.archive.get_transition(action_id)
        except KeyError as exc:
            raise TerminalBatchError(
                "admitted child is missing durable coordinates"
            ) from exc
        if (
            action.batch_id != batch.batch_id
            or action.run_id != batch.run_id
            or action.state_id != batch.state_id
            or action.action_id not in batch.action_ids
            or action.action_id not in batch.completed_action_ids
        ):
            raise TerminalBatchError("action differs from its durable batch")
        lineage = work.archive.lineage_for_action(action_id)
        worker = work.outcomes.worker_for_action(action_id)
        outcome = work.outcomes.outcome_for_action(action_id)
        effectiveness = work.outcomes.effectiveness_for_action(action_id)
        if worker is None or outcome is None or effectiveness is None:
            raise TerminalBatchError("admitted child has incomplete evaluation facts")
        require_admissible_child(ExecutionResult(worker, outcome))
        readout = outcome.child_readout
        assert readout is not None
        child_commit = worker.result_commit_sha
        assert child_commit is not None
        if (
            transition.action_id != action.action_id
            or transition.parent_commit_sha != action.parent_commit_sha
            or transition.child_commit_sha != child_commit
            or not transition.admitted
            or transition.effectiveness_label != effectiveness.label.value
            or lineage is None
            or lineage.action_id != action.action_id
            or lineage.parent_commit_sha != action.parent_commit_sha
            or lineage.child_commit_sha != child_commit
            or worker.action_id != action.action_id
            or outcome.action_id != action.action_id
            or effectiveness.action_id != action.action_id
        ):
            raise TerminalBatchError(
                "admitted child differs from action/outcome/archive facts"
            )
        run_records = work.search_read_model.list_programs_for_run(batch.run_id)
        if not run_records or run_records[0].parent.admitted_seq != 0:
            raise ValueError("admitted child requires a durable root seed")
        existing = tuple(
            record
            for record in run_records
            if record.source_action_id == action.action_id
        )
        if len(existing) > 1:
            raise TerminalBatchError("source action has multiple search programs")
        archive_seq = (
            existing[0].parent.admitted_seq
            if existing
            else max(item.parent.admitted_seq for item in run_records) + 1
        )
        available_at = max(
            worker.completed_at,
            outcome.completed_at,
            effectiveness.available_at,
        )
        candidate = ParentCandidate(
            parent_id=readout.child_program_id,
            commit_sha=child_commit,
            descriptor=readout.descriptor,
            primary_value=readout.absolute_primary_value,
            metadata={
                "source_action_id": action.action_id,
                "source_parent_id": action.parent_id,
                "paired_outcome_id": outcome.outcome_id,
            },
        )
        record = self._record(
            run_id=batch.run_id,
            candidate=candidate,
            compatibility=state.compatibility,
            archive_seq=archive_seq,
            available_at=available_at,
            cell_results=readout.cell_results,
            cell_notes=readout.cell_notes,
            # ``outcome.cell_diagnostics`` is intentionally operator-only.
            # The compact cell note is the policy-safe timeout signal; the
            # structured diagnostic must not enter a SearchFact and therefore
            # cannot reach proposer prompt evidence.
            fact_payload={
                "paired_outcome_id": outcome.outcome_id,
                "admission_fate": outcome.admission_fate,
            },
            searchable=True,
            source_action_id=action.action_id,
            fact_kind="admitted_program",
            evidence_tier=EvidenceTier.ADAPTIVE_B,
        )
        try:
            return self._store_program(work.search_read_model, record)
        except PersistenceConflict as exc:
            raise TerminalBatchError(
                "admitted child conflicts with the durable search read model"
            ) from exc

    @staticmethod
    def _store_program(
        repository: SearchReadModelRepository, record: SearchProgramRecord
    ) -> SearchProgramRecord:
        """Preserve an immutable legacy row when only score provenance was added."""

        try:
            existing = repository.get_program(record.record_id)
        except KeyError:
            repository.add_program(record)
            return record
        if existing == record:
            return existing
        if "primary_evidence_tier" not in existing.parent.candidate.metadata:
            legacy_candidate = replace(
                record.parent.candidate,
                metadata={
                    key: value
                    for key, value in record.parent.candidate.metadata.items()
                    if key != "primary_evidence_tier"
                },
            )
            legacy_record = replace(
                record, parent=replace(record.parent, candidate=legacy_candidate)
            )
            if existing == legacy_record:
                return existing
        raise PersistenceConflict("search program replay differs from its immutable record")

    @staticmethod
    def _record(
        *,
        run_id: str,
        candidate: ParentCandidate,
        compatibility: CompatibilityKey,
        archive_seq: int,
        available_at: datetime,
        cell_results: Mapping[str, bool | None],
        fact_payload: Mapping[str, Any] | None,
        searchable: bool,
        source_action_id: str | None,
        fact_kind: str,
        evidence_tier: EvidenceTier,
        cell_notes: Mapping[str, str] | None = None,
        cell_excerpts: Mapping[str, str] | None = None,
    ) -> SearchProgramRecord:
        record_id = stable_id(
            "search-program",
            run_id,
            candidate.parent_id,
            candidate.commit_sha,
        )
        candidate = replace(
            candidate,
            metadata={
                **candidate.metadata,
                "primary_evidence_tier": evidence_tier.value,
            },
        )
        parent = ParentRecord(
            candidate=candidate,
            admitted_seq=archive_seq,
            available_at=available_at,
            searchable=searchable,
        )
        evidence = ProgramEvidence(
            parent_id=candidate.parent_id,
            commit_sha=candidate.commit_sha,
            compatibility=compatibility,
            archive_seq=archive_seq,
            available_at=available_at,
            primary_value=candidate.primary_value,
            evidence_tier=evidence_tier,
            cell_results=cell_results,
        )
        fact = SearchFact(
            fact_id=stable_id("search-program-fact", record_id),
            kind=fact_kind,
            available_at=available_at,
            evidence_tier=evidence_tier,
            payload={
                "record_id": record_id,
                "parent_id": candidate.parent_id,
                "commit_sha": candidate.commit_sha,
                "archive_seq": archive_seq,
                "primary_value": candidate.primary_value,
                "cell_results": dict(cell_results),
                "cell_notes": dict(cell_notes or {}),
                "cell_excerpts": dict(cell_excerpts or {}),
                "source_action_id": source_action_id,
                "readout": dict(fact_payload or {}),
            },
        )
        return SearchProgramRecord(
            record_id=record_id,
            run_id=run_id,
            parent=parent,
            evidence=evidence,
            fact=fact,
            source_action_id=source_action_id,
        )


class DurableSearchInputSource(SearchInputSource):
    """Freeze one cutoff-safe SQLite/UoW read-model view per batch."""

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        compatibility: CompatibilityKey,
        parent_descriptor: StateDescriptor,
        direction_descriptor: StateDescriptor,
        budget: Mapping[str, float | int],
        carrier_context: str = "",
        repository_map_builder: (
            Callable[[ParentRecord], ParentRepositoryMap] | None
        ) = None,
        visible_a_evidence_builder: (
            Callable[[tuple[str, ...]], tuple[VisibleATaskEvidence, ...]] | None
        ) = None,
        require_visible_a_trace: bool = False,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not callable(unit_of_work):
            raise TypeError("unit_of_work must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if repository_map_builder is not None and not callable(repository_map_builder):
            raise TypeError("repository_map_builder must be callable")
        if visible_a_evidence_builder is not None and not callable(
            visible_a_evidence_builder
        ):
            raise TypeError("visible_a_evidence_builder must be callable")
        self.unit_of_work = unit_of_work
        self.compatibility = compatibility
        self.parent_descriptor = parent_descriptor
        self.direction_descriptor = direction_descriptor
        self.budget = dict(budget)
        self.carrier_context = carrier_context
        self.repository_map_builder = repository_map_builder
        self.visible_a_evidence_builder = visible_a_evidence_builder
        if not isinstance(require_visible_a_trace, bool):
            raise TypeError("require_visible_a_trace must be boolean")
        self.require_visible_a_trace = require_visible_a_trace
        self.clock = clock

    def load(self, *, run_id: str, batch_id: str) -> SearchInputSnapshot:
        require_text(run_id, "run_id")
        require_text(batch_id, "batch_id")
        with self.unit_of_work() as work:
            self._require_prior_batches_closed(
                work,
                run_id=run_id,
                batch_id=batch_id,
            )
            frozen = work.search_read_model.snapshot_for_batch(run_id, batch_id)
            if frozen is None:
                frozen = self._freeze(work, run_id=run_id, batch_id=batch_id)
                records = tuple(
                    work.search_read_model.get_program(record_id)
                    for record_id in frozen.program_record_ids
                )
                manifests = self._eligible_trace_manifests(
                    work,
                    frozen=frozen,
                    records=records,
                )
                self._require_trace_evidence(frozen, manifests)
                work.search_read_model.add_snapshot(frozen)
                self._bind_trace_manifests(
                    work,
                    frozen=frozen,
                    manifests=manifests,
                )
            else:
                expected = (
                    self.compatibility,
                    self.parent_descriptor,
                    self.direction_descriptor,
                    self.budget,
                    self.carrier_context,
                )
                actual = (
                    frozen.compatibility,
                    frozen.parent_descriptor,
                    frozen.direction_descriptor,
                    frozen.budget,
                    frozen.carrier_context,
                )
                if actual != expected:
                    raise ValueError(
                        "durable batch snapshot differs from source config"
                    )
                records = tuple(
                    work.search_read_model.get_program(record_id)
                    for record_id in frozen.program_record_ids
                )
                manifests = self._bound_trace_manifests(
                    work,
                    frozen=frozen,
                    records=records,
                )
                self._require_trace_evidence(frozen, manifests)
        return SearchInputSnapshot(
            cutoff=frozen.cutoff,
            compatibility=frozen.compatibility,
            archive_seq=frozen.archive_seq,
            parent_descriptor=frozen.parent_descriptor,
            direction_descriptor=frozen.direction_descriptor,
            budget=frozen.budget,
            facts=tuple(record.fact for record in records),
            parent_records=tuple(record.parent for record in records),
            evidence_records=tuple(record.evidence for record in records),
            direction_history_records=frozen.direction_history_records,
            repository_maps=frozen.repository_maps,
            visible_a_evidence=frozen.visible_a_evidence,
            trace_manifests=manifests,
            carrier_context=frozen.carrier_context,
        )

    def _require_trace_evidence(
        self,
        frozen: FrozenSearchInputRecord,
        manifests: tuple[TraceEvidenceManifest, ...],
    ) -> None:
        """Require one valid readable case on a parent's own evidence floor.

        Rejected-candidate manifests describe attempts that no longer exist in
        the archive, so their cases cannot make a snapshot proposable. Passing
        cases remain useful evidence when a parent has solved its visible tasks.
        """

        if not self.require_visible_a_trace:
            return
        rejected = {
            record.trace_manifest_id
            for record in frozen.direction_history_records
            if record.trace_manifest_id is not None
        }
        parents = {
            (item.parent_id, item.commit_sha, item.tree_sha)
            for item in frozen.repository_maps
        }
        if not any(
            case.measurement_status == "valid" and case.trace_ref_id is not None
            for manifest in manifests
            if manifest.manifest_id not in rejected
            and manifest.evidence_tier is EvidenceTier.VISIBLE_A
            and manifest.compatibility == frozen.compatibility
            and frozen.cutoff.allows(manifest.available_at)
            and (manifest.program_id, manifest.commit_sha, manifest.tree_sha) in parents
            for case in manifest.case_indexes
        ):
            raise TraceEvidenceUnavailable()

    @staticmethod
    def _eligible_trace_manifests(
        work,
        *,
        frozen: FrozenSearchInputRecord,
        records: tuple[SearchProgramRecord, ...],
    ) -> tuple[TraceEvidenceManifest, ...]:
        """Select one latest exact, cutoff-visible manifest per frozen program,
        plus every rejected-candidate manifest named by the frozen history."""

        selected = []
        trees = {
            (item.parent_id, item.commit_sha): item.tree_sha
            for item in frozen.repository_maps
        }
        for record in records:
            candidate = record.parent.candidate
            eligible = tuple(
                manifest
                for manifest in work.trace_evidence.list_manifests_for_program(
                    candidate.parent_id
                )
                if manifest.compatibility == frozen.compatibility
                and manifest.commit_sha == candidate.commit_sha
                and manifest.tree_sha
                == trees.get((candidate.parent_id, candidate.commit_sha))
                and frozen.cutoff.allows(manifest.available_at)
            )
            if eligible:
                selected.append(
                    max(
                        eligible,
                        key=lambda item: (item.available_at, item.manifest_id),
                    )
                )
        rejected = {
            record.trace_manifest_id
            for record in frozen.direction_history_records
            if record.trace_manifest_id is not None
        } - {manifest.manifest_id for manifest in selected}
        # The frozen history named these manifests, so a missing one is a
        # durability failure, not an optional feature: get_manifest raises.
        selected.extend(
            work.trace_evidence.get_manifest(manifest_id)
            for manifest_id in sorted(rejected)
        )
        return tuple(sorted(selected, key=lambda item: item.manifest_id))

    @staticmethod
    def _bind_trace_manifests(
        work,
        *,
        frozen: FrozenSearchInputRecord,
        manifests: tuple[TraceEvidenceManifest, ...],
    ) -> None:
        current = work.trace_evidence.binding_for_snapshot(frozen.snapshot_id)
        if not manifests:
            if current is not None:
                raise ValueError("trace-free snapshot already has a trace binding")
            return
        expected = SnapshotTraceBinding.create(
            snapshot_id=frozen.snapshot_id,
            manifest_ids=tuple(item.manifest_id for item in manifests),
        )
        if current is None:
            work.trace_evidence.bind_snapshot(expected)
        elif current != expected:
            raise ValueError("durable snapshot trace binding has identity drift")

    @staticmethod
    def _bound_trace_manifests(
        work,
        *,
        frozen: FrozenSearchInputRecord,
        records: tuple[SearchProgramRecord, ...],
    ) -> tuple[TraceEvidenceManifest, ...]:
        binding = work.trace_evidence.binding_for_snapshot(frozen.snapshot_id)
        if binding is None:
            return ()
        manifests = tuple(
            work.trace_evidence.get_manifest(manifest_id)
            for manifest_id in binding.manifest_ids
        )
        coordinates = {
            (item.parent_id, item.commit_sha, item.tree_sha)
            for item in frozen.repository_maps
        }
        rejected = {
            record.trace_manifest_id
            for record in frozen.direction_history_records
            if record.trace_manifest_id is not None
        }
        if any(
            manifest.compatibility != frozen.compatibility
            or not frozen.cutoff.allows(manifest.available_at)
            or (
                (manifest.program_id, manifest.commit_sha, manifest.tree_sha)
                not in coordinates
                and manifest.manifest_id not in rejected
            )
            for manifest in manifests
        ):
            raise ValueError("durable snapshot trace binding is ineligible")
        return tuple(sorted(manifests, key=lambda item: item.manifest_id))

    @staticmethod
    def _require_prior_batches_closed(work, *, run_id: str, batch_id: str) -> None:
        unfinished = tuple(
            batch.batch_id
            for batch in work.batches.list_for_run(run_id)
            if batch.batch_id != batch_id
            and batch.status
            not in {
                BatchStatus.POLICY_UPDATED,
                BatchStatus.PLANNING_EXHAUSTED,
                BatchStatus.FAILED,
            }
        )
        if unfinished:
            raise ValueError(
                "new search snapshot is blocked by unfinished prior batches: "
                + ", ".join(unfinished)
            )

    def _freeze(self, work, *, run_id: str, batch_id: str):
        cutoff_at = self.clock()
        require_aware(cutoff_at, "search input cutoff")
        cutoff = EvidenceCutoff(cutoff_at)
        visible = tuple(
            record
            for record in work.search_read_model.list_programs_for_run(run_id)
            if record.evidence.compatibility == self.compatibility
            and cutoff.allows(record.parent.available_at)
        )
        if not visible or visible[0].parent.admitted_seq != 0:
            raise ValueError("search input source has no cutoff-visible root seed")
        archive_seq = max(record.parent.admitted_seq for record in visible)
        record_ids = tuple(record.record_id for record in visible)
        direction_history = self._direction_history(work, run_id=run_id, cutoff=cutoff)
        repository_maps = self._repository_maps(
            tuple(record.parent for record in visible)
        )
        visible_a_evidence = self._visible_a_evidence(
            tuple(record.evidence for record in visible)
        )
        snapshot_id = stable_id(
            "search-input-snapshot",
            run_id,
            batch_id,
            cutoff,
            self.compatibility,
            archive_seq,
            self.parent_descriptor,
            self.direction_descriptor,
            record_ids,
            direction_history,
            repository_maps,
            visible_a_evidence,
            self.budget,
            self.carrier_context,
        )
        return FrozenSearchInputRecord(
            snapshot_id=snapshot_id,
            run_id=run_id,
            batch_id=batch_id,
            cutoff=cutoff,
            compatibility=self.compatibility,
            archive_seq=archive_seq,
            parent_descriptor=self.parent_descriptor,
            direction_descriptor=self.direction_descriptor,
            program_record_ids=record_ids,
            direction_history_records=direction_history,
            repository_maps=repository_maps,
            visible_a_evidence=visible_a_evidence,
            budget=self.budget,
            carrier_context=self.carrier_context,
        )

    def _visible_a_evidence(
        self, evidence: tuple[ProgramEvidence, ...]
    ) -> tuple[VisibleATaskEvidence, ...]:
        """Describe only measured Visible-A cells, and nothing else."""

        if self.visible_a_evidence_builder is None:
            return ()
        cell_ids = tuple(
            sorted(
                {
                    cell_id
                    for item in evidence
                    if item.evidence_tier is EvidenceTier.VISIBLE_A
                    for cell_id in item.cell_results
                }
            )
        )
        if not cell_ids:
            return ()
        records = tuple(self.visible_a_evidence_builder(cell_ids))
        if any(not isinstance(item, VisibleATaskEvidence) for item in records):
            raise TypeError("Visible-A evidence builder returned the wrong type")
        described = {item.cell_id for item in records}
        if not described <= set(cell_ids):
            raise ValueError("Visible-A evidence builder described an unmeasured cell")
        return records

    def _repository_maps(
        self, parents: tuple[ParentRecord, ...]
    ) -> tuple[ParentRepositoryMap, ...]:
        if self.repository_map_builder is None:
            return ()
        maps = tuple(self.repository_map_builder(parent) for parent in parents)
        for parent, repository_map in zip(parents, maps, strict=True):
            candidate = parent.candidate
            if not isinstance(repository_map, ParentRepositoryMap):
                raise TypeError("repository map builder returned the wrong type")
            if (
                repository_map.parent_id != candidate.parent_id
                or repository_map.commit_sha != candidate.commit_sha
            ):
                raise ValueError("repository map differs from its frozen parent")
        return maps

    @staticmethod
    def _direction_history(work, *, run_id: str, cutoff: EvidenceCutoff):
        """Join only completed, cutoff-visible facts while freezing the batch."""

        records = []
        for batch in work.batches.list_for_run(run_id):
            for observation in work.observations.list_for_batch(batch.batch_id):
                if not cutoff.allows(observation.available_at):
                    continue
                try:
                    action = work.actions.get(observation.action_id)
                except KeyError as exc:
                    raise TerminalBatchError(
                        "direction history observation has no durable action"
                    ) from exc
                records.append(
                    DirectionHistoryRecord.from_completed(
                        action,
                        observation,
                        trace_manifest_id=resolve_rejected_candidate_manifest_id(
                            work.trace_evidence, observation
                        ),
                    )
                )
        return tuple(
            sorted(records, key=lambda item: (item.available_at, item.history_id))
        )


__all__ = [
    "CanonicalSearchReadModelWriter",
    "DurableSearchInputSource",
    "TRACE_EVIDENCE_UNAVAILABLE",
    "TraceEvidenceUnavailable",
]
