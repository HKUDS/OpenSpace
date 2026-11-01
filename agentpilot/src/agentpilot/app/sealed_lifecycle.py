"""Terminal run orchestration for benchmark-neutral sealed evaluation."""

from __future__ import annotations

from datetime import datetime, timedelta

from agentpilot.domain import stable_id
from agentpilot.evaluation.sealed.contracts import (
    SealedAuthorization,
    SealedConflict,
    SealedState,
)
from agentpilot.evaluation.sealed.service import SealedHoldoutController
from agentpilot.persistence import (
    RunBatchLeaseManager,
    RunLeaseLost,
    UnitOfWorkFactory,
)
from agentpilot.persistence.terminal_archive import (
    derive_terminal_archive_coordinates,
)


class SealedRunFinalizer:
    """Atomically freeze durable run coordinates before any C execution."""

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        lease_manager: RunBatchLeaseManager,
        *,
        lease_ttl: timedelta,
    ) -> None:
        if not callable(unit_of_work):
            raise TypeError("unit_of_work must be callable")
        if not isinstance(lease_ttl, timedelta) or lease_ttl <= timedelta(0):
            raise ValueError("lease_ttl must be a positive timedelta")
        self.unit_of_work = unit_of_work
        self.lease_manager = lease_manager
        self.lease_ttl = lease_ttl
        if not callable(getattr(unit_of_work, "fenced", None)):
            raise TypeError("sealed finalization requires a lease-fenced unit of work")

    def finalize_and_authorize(
        self,
        *,
        run_id: str,
        expected_champion_program_id: str | None = None,
        reason: str,
        owner_token: str,
        now: datetime,
    ) -> SealedAuthorization:
        """Latch terminal state using only durable batch/archive coordinates."""

        with self.unit_of_work() as work:
            existing = work.sealed.get(run_id)
            if existing.state is not SealedState.SEALED:
                authorization = existing.authorization
                if authorization is not None and (
                    expected_champion_program_id is None
                    or authorization.champion_program_id == expected_champion_program_id
                ):
                    return authorization
                raise SealedConflict("run was finalized with another champion")

        lease = self.lease_manager.acquire(
            run_id=run_id,
            batch_id=stable_id("sealed-finalization-lease", run_id),
            owner_token=owner_token,
            now=now,
            ttl=self.lease_ttl,
        )
        try:
            fenced = self.unit_of_work.fenced(lease, clock=lambda: now)
            with fenced() as work:
                record = work.sealed.get(run_id)
                if record.state is not SealedState.SEALED:
                    authorization = record.authorization
                    if authorization is not None and (
                        expected_champion_program_id is None
                        or authorization.champion_program_id
                        == expected_champion_program_id
                    ):
                        return authorization
                    raise SealedConflict("run was finalized with another champion")
                batches = work.batches.list_for_run(run_id)
                programs = work.search_read_model.list_programs_for_run(run_id)
                try:
                    coordinates = derive_terminal_archive_coordinates(
                        run_id=run_id,
                        batches=batches,
                        programs=programs,
                        as_of=now,
                    )
                except ValueError as exc:
                    raise SealedConflict(str(exc)) from exc
                champion_program_id = coordinates.champion_program_id
                if (
                    expected_champion_program_id is not None
                    and expected_champion_program_id != champion_program_id
                ):
                    raise SealedConflict(
                        "requested champion differs from durable best program"
                    )
                return SealedHoldoutController(work.sealed).authorize(
                    run_id=run_id,
                    root_program_id=coordinates.root_program_id,
                    champion_program_id=champion_program_id,
                    archive_cutoff=coordinates.archive_cutoff,
                    reason=reason,
                    now=now,
                )
        finally:
            try:
                self.lease_manager.release(lease, now=now)
            except RunLeaseLost:
                pass


__all__ = ["SealedRunFinalizer"]
