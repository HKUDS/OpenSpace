"""Supervise a multi-process campaign from durable facts.

A shell loop can only see a process exit code, which says nothing about whether
a batch updated the policy, is waiting for authoritative accounting, or left a
dispatch unaccounted for. The supervisor therefore re-reads the database after
every child process and decides from the recorded facts.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Callable, Protocol, Sequence

from agentpilot.app.campaign import (
    CampaignDisposition,
    ReconciliationWindow,
    assess_batch,
)
from agentpilot.app.runtime_config import CampaignConfig
from agentpilot.domain import ProviderCallResult, ReservationState, utc_now
from agentpilot.persistence import BatchRecord, BatchStatus
from agentpilot.persistence.leases import RunBatchLeaseManager
from agentpilot.persistence.protocols import UnitOfWorkFactory


class BatchInvoker(Protocol):
    """Run one batch to whatever end it reaches, in its own process."""

    def start(self, *, batch_id: str, rounds: int) -> int: ...

    def resume(self, *, batch_id: str) -> int: ...


class CampaignStatus(str, Enum):
    COMPLETED = "completed"
    PLANNING_BUDGET_EXHAUSTED = "planning_budget_exhausted"
    ACCOUNTING_QUARANTINE = "accounting_quarantine"
    TERMINAL_FAILURE = "terminal_failure"
    STALLED = "stalled"


@dataclass(frozen=True)
class CampaignOutcome:
    status: CampaignStatus
    reason: str
    completed_processes: int
    batch_id: str | None = None


@dataclass(frozen=True)
class _BatchFacts:
    batches: tuple[BatchRecord, ...]
    intents: tuple[object, ...]
    dispatches: tuple[object, ...]
    results: tuple[ProviderCallResult, ...]


def planning_batch_id(*, process_ordinal: int, replacement: int) -> str:
    """Name a physical batch so its logical process stays readable in the data."""

    if process_ordinal < 1 or replacement < 1:
        raise ValueError("campaign batch ordinals start at one")
    return f"process-{process_ordinal:03d}-planning-{replacement:03d}"


class CampaignSupervisor:
    """Drive logical processes until the configured target is met or blocked."""

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        lease_manager: RunBatchLeaseManager,
        invoker: BatchInvoker,
        config: CampaignConfig,
        run_id: str,
        clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.unit_of_work = unit_of_work
        self.lease_manager = lease_manager
        self.invoker = invoker
        self.config = config
        self.run_id = run_id
        self.clock = clock
        self.sleep = sleep or (lambda _seconds: None)
        self.log = log or (lambda _message: None)

    def run(self) -> CampaignOutcome:
        # One invocation that leaves the durable facts exactly as they were is
        # normal; a stream of them means the campaign is looping, which no exit
        # code would reveal.
        stalled = 0
        progress: tuple[object, ...] | None = None
        while True:
            facts = self._facts()
            completed = tuple(
                batch
                for batch in facts.batches
                if batch.status is BatchStatus.POLICY_UPDATED
            )
            if len(completed) > self.config.total_processes:
                return CampaignOutcome(
                    CampaignStatus.TERMINAL_FAILURE,
                    "the run holds more completed processes than the campaign target",
                    len(completed),
                )
            if len(completed) == self.config.total_processes:
                return CampaignOutcome(
                    CampaignStatus.COMPLETED,
                    "every logical process updated the policy exactly once",
                    len(completed),
                )

            ordinal = len(completed) + 1
            physical = self._physical_batches(facts.batches, ordinal)
            if not physical:
                batch_id = planning_batch_id(
                    process_ordinal=ordinal, replacement=1
                )
                fingerprint = (batch_id, "unrecorded")
                stalled = stalled + 1 if progress == fingerprint else 0
                progress = fingerprint
                if stalled >= self.config.max_stalled_resumes:
                    return CampaignOutcome(
                        CampaignStatus.STALLED,
                        f"{batch_id} never recorded a batch across {stalled} "
                        "start attempts",
                        len(completed),
                        batch_id,
                    )
                self.log(
                    f"starting process {ordinal}/{self.config.total_processes} "
                    f"as {batch_id}"
                )
                self.invoker.start(
                    batch_id=batch_id, rounds=self.config.actions_per_process
                )
                terminal = self._invoker_terminal_outcome(
                    completed_processes=len(completed), batch_id=batch_id
                )
                if terminal is not None:
                    return terminal
                continue

            current = physical[-1]
            assessment = assess_batch(
                current,
                intents=facts.intents,
                dispatches=facts.dispatches,
                results=facts.results,
                reconciliation=ReconciliationWindow(
                    now=self.clock(),
                    deadline_seconds=self.config.reconciliation_deadline_seconds,
                ),
                lease_live=self._lease_live(),
            )
            disposition = assessment.disposition
            if disposition is CampaignDisposition.COMPLETE:
                stalled = 0
                progress = None
                continue
            if disposition is CampaignDisposition.WAIT_FOR_LEASE:
                self.sleep(self.config.poll_interval_seconds)
                continue
            if disposition is CampaignDisposition.AWAIT_RECONCILIATION:
                # Waiting for an authoritative accounting row is not a stalled
                # campaign, so it is bounded by the receipt's own reconciliation
                # deadline rather than by the resume budget. Resuming replays the
                # recorded receipt and retries only the lookup, so polling can
                # never issue a second model call.
                self.sleep(self.config.poll_interval_seconds)
                self.log(f"resuming {current.batch_id}: {assessment.reason}")
                self.invoker.resume(batch_id=current.batch_id)
                terminal = self._invoker_terminal_outcome(
                    completed_processes=len(completed), batch_id=current.batch_id
                )
                if terminal is not None:
                    return terminal
                continue
            if disposition is CampaignDisposition.RESUME:
                fingerprint = self._progress_fingerprint(current, facts)
                stalled = stalled + 1 if progress == fingerprint else 0
                progress = fingerprint
                if stalled >= self.config.max_stalled_resumes:
                    return CampaignOutcome(
                        CampaignStatus.STALLED,
                        f"{current.batch_id} stopped making durable progress "
                        f"across {stalled} resume attempts",
                        len(completed),
                        current.batch_id,
                    )
                self.log(f"resuming {current.batch_id}: {assessment.reason}")
                self.invoker.resume(batch_id=current.batch_id)
                terminal = self._invoker_terminal_outcome(
                    completed_processes=len(completed), batch_id=current.batch_id
                )
                if terminal is not None:
                    return terminal
                continue
            if disposition is CampaignDisposition.REPLACE_PLANNING_BATCH:
                if current.status is not BatchStatus.PLANNING_EXHAUSTED:
                    self._abandon_planning_batch(current, assessment.reason)
                replacement = len(physical) + 1
                if replacement > self.config.max_planning_batches_per_process:
                    return CampaignOutcome(
                        CampaignStatus.PLANNING_BUDGET_EXHAUSTED,
                        f"logical process {ordinal} used every planning "
                        "replacement without dispatching an action",
                        len(completed),
                        current.batch_id,
                    )
                batch_id = planning_batch_id(
                    process_ordinal=ordinal, replacement=replacement
                )
                stalled = 0
                progress = None
                self.log(
                    f"replacing exhausted planning batch {current.batch_id} "
                    f"with {batch_id}"
                )
                self.invoker.start(
                    batch_id=batch_id, rounds=self.config.actions_per_process
                )
                terminal = self._invoker_terminal_outcome(
                    completed_processes=len(completed), batch_id=batch_id
                )
                if terminal is not None:
                    return terminal
                continue
            status = (
                CampaignStatus.ACCOUNTING_QUARANTINE
                if disposition is CampaignDisposition.ACCOUNTING_QUARANTINE
                else CampaignStatus.TERMINAL_FAILURE
            )
            return CampaignOutcome(
                status, assessment.reason, len(completed), current.batch_id
            )

    def _invoker_terminal_outcome(
        self, *, completed_processes: int, batch_id: str
    ) -> CampaignOutcome | None:
        status = getattr(self.invoker, "terminal_status", None)
        if status is None:
            return None
        if not isinstance(status, CampaignStatus):
            return CampaignOutcome(
                CampaignStatus.TERMINAL_FAILURE,
                "batch invoker exposed an invalid terminal status",
                completed_processes,
                batch_id,
            )
        reason = getattr(self.invoker, "terminal_reason", None)
        if not isinstance(reason, str) or not reason:
            reason = "batch invoker reported a terminal condition"
        return CampaignOutcome(status, reason, completed_processes, batch_id)

    def _abandon_planning_batch(
        self, batch: BatchRecord, reason: str
    ) -> None:
        """Seal a provider-quarantined draft before starting its replacement.

        Unknown accounting remains in the provider ledger for audit.  Only the
        planning batch is abandoned, and only before any action dispatch, so no
        measured A/B work or policy update can be skipped.  Planned UCB semantic
        reservations are reclaimed in the same transaction.
        """

        with self.unit_of_work() as work:
            current = work.batches.get(batch.batch_id)
            lifecycle = (
                current.resume_from
                if current.status is BatchStatus.INTERRUPTED
                else current.status
            )
            if (
                lifecycle is not BatchStatus.DRAFTING
                or current.dispatched_action_ids
                or current.completed_action_ids
                or len(current.action_ids) >= current.expected_rounds
            ):
                raise RuntimeError(
                    "only an incomplete, undispatched drafting batch can be abandoned"
                )
            for semantic in work.semantic_reservations.list_for_batch(
                current.batch_id
            ):
                reservation = semantic.reservation
                if reservation.state is ReservationState.PLANNED:
                    work.semantic_reservations.replace(
                        replace(
                            semantic,
                            reservation=replace(
                                reservation,
                                state=ReservationState.RECLAIMED,
                            ),
                        )
                    )
                elif reservation.state is not ReservationState.RECLAIMED:
                    raise RuntimeError(
                        "provider-quarantined planning batch crossed dispatch"
                    )
            if current.status is BatchStatus.INTERRUPTED:
                current = replace(
                    current,
                    status=BatchStatus.DRAFTING,
                    resume_from=None,
                    error=None,
                )
                work.batches.replace(current)
            work.batches.replace(
                replace(
                    current,
                    status=BatchStatus.PLANNING_EXHAUSTED,
                    resume_from=None,
                    error="provider_quarantined_before_dispatch",
                )
            )

    def _facts(self) -> _BatchFacts:
        with self.unit_of_work() as work:
            return _BatchFacts(
                batches=tuple(work.batches.list_for_run(self.run_id)),
                intents=tuple(work.usage.list_intents_for_run(self.run_id)),
                dispatches=tuple(work.usage.list_dispatches_for_run(self.run_id)),
                results=tuple(work.usage.list_results_for_run(self.run_id)),
            )

    def _physical_batches(
        self, batches: Sequence[BatchRecord], ordinal: int
    ) -> tuple[BatchRecord, ...]:
        prefix = f"process-{ordinal:03d}-planning-"
        return tuple(
            sorted(
                (batch for batch in batches if batch.batch_id.startswith(prefix)),
                key=lambda batch: (batch.created_at, batch.batch_id),
            )
        )

    def _lease_live(self) -> bool:
        lease = self.lease_manager.current(self.run_id)
        return lease is not None and lease.is_live_at(self.clock())

    @staticmethod
    def _progress_fingerprint(
        batch: BatchRecord, facts: _BatchFacts
    ) -> tuple[object, ...]:
        return (
            batch.batch_id,
            batch.status,
            batch.resume_from,
            tuple(batch.completed_action_ids),
            tuple(batch.dispatched_action_ids),
            # A resume that only attests a late receipt adds no row and changes
            # no count, so counting results alone cannot tell it apart from a
            # resume that did nothing. Attestation is durable progress.
            tuple(
                sorted(
                    (result.call_id, result.accounting_status)
                    for result in facts.results
                )
            ),
        )


class SubprocessBatchInvoker:
    """Run each batch in a fresh process so a crash cannot poison the next one."""

    def __init__(
        self,
        *,
        config_path: str,
        run_id: str,
        env_file: str | None = None,
        executable: str | None = None,
        runner: Callable[[Sequence[str]], int] | None = None,
    ) -> None:
        self.config_path = config_path
        self.run_id = run_id
        self.env_file = env_file
        self.executable = executable or sys.executable
        self.runner = runner or self._spawn

    def start(self, *, batch_id: str, rounds: int) -> int:
        return self.runner(
            self._command(
                "run",
                batch_id,
                ("--run-id", self.run_id, "--rounds", str(rounds)),
            )
        )

    def resume(self, *, batch_id: str) -> int:
        return self.runner(self._command("resume", batch_id, ()))

    def _command(
        self, verb: str, batch_id: str, extra: Sequence[str]
    ) -> tuple[str, ...]:
        command = [
            self.executable,
            "-m",
            "agentpilot.app",
            verb,
            "--config",
            self.config_path,
            "--batch-id",
            batch_id,
        ]
        if self.env_file is not None:
            command.extend(("--env-file", self.env_file))
        command.extend(extra)
        return tuple(command)

    @staticmethod
    def _spawn(command: Sequence[str]) -> int:
        # Keep a long-running batch outside the campaign terminal's process
        # group.  Stopping the tmux campaign must not also interrupt a child
        # between its durable dispatch and receipt writes: that window is
        # intentionally fail-closed and would quarantine the whole run.  The
        # detached child retains the lease and finishes its current durable
        # transition; a later campaign invocation can then resume from facts.
        return subprocess.run(
            list(command),
            check=False,
            start_new_session=True,
        ).returncode


__all__ = [
    "BatchInvoker",
    "CampaignOutcome",
    "CampaignStatus",
    "CampaignSupervisor",
    "SubprocessBatchInvoker",
    "planning_batch_id",
]
