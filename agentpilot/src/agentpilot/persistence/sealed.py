"""Thin transactional facades for benchmark-neutral sealed repositories."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from agentpilot.evaluation.sealed.contracts import (
    SealedAuthorization,
    SealedConflict,
    SealedCostSlot,
    SealedEvaluationClaim,
    SealedEvaluationReceipt,
    SealedEvaluationReservation,
    SealedManifest,
    SealedRecord,
    SealedReport,
    SealedSlotSettlement,
)
from agentpilot.persistence.protocols import UnitOfWorkFactory


class TransactionalSealedRepository:
    """Run each reference transition in one caller-supplied unit of work."""

    def __init__(self, unit_of_work: UnitOfWorkFactory):
        if not callable(unit_of_work):
            raise TypeError("unit_of_work must be callable")
        self.unit_of_work = unit_of_work

    def add_manifest(self, manifest: SealedManifest) -> None:
        with self.unit_of_work() as work:
            work.sealed.add_manifest(manifest)

    def get(self, run_id: str) -> SealedRecord:
        with self.unit_of_work() as work:
            return work.sealed.get(run_id)

    def authorize(self, authorization: SealedAuthorization) -> SealedRecord:
        raise SealedConflict("durable authorization requires app.SealedRunFinalizer")

    def begin_evaluation(
        self,
        reservation: SealedEvaluationReservation,
        *,
        owner_token: str,
        now: datetime,
        ttl: timedelta,
    ) -> SealedRecord:
        with self.unit_of_work() as work:
            return work.sealed.begin_evaluation(
                reservation,
                owner_token=owner_token,
                now=now,
                ttl=ttl,
            )

    def renew_evaluation_claim(
        self,
        claim: SealedEvaluationClaim,
        *,
        now: datetime,
        ttl: timedelta,
    ) -> SealedEvaluationClaim:
        with self.unit_of_work() as work:
            return work.sealed.renew_evaluation_claim(claim, now=now, ttl=ttl)

    def dispatch_slot(
        self,
        claim: SealedEvaluationClaim,
        *,
        slot_id: str,
        dispatched_at: datetime,
    ) -> tuple[SealedCostSlot, bool]:
        with self.unit_of_work() as work:
            return work.sealed.dispatch_slot(
                claim, slot_id=slot_id, dispatched_at=dispatched_at
            )

    def settle_slot(
        self,
        claim: SealedEvaluationClaim,
        settlement: SealedSlotSettlement,
    ) -> SealedCostSlot:
        with self.unit_of_work() as work:
            return work.sealed.settle_slot(claim, settlement)

    def record_evaluation(
        self,
        claim: SealedEvaluationClaim,
        receipt: SealedEvaluationReceipt,
        *,
        recorded_at: datetime,
    ) -> SealedRecord:
        with self.unit_of_work() as work:
            return work.sealed.record_evaluation(
                claim,
                receipt,
                recorded_at=recorded_at,
            )

    def write_report(self, report: SealedReport) -> SealedRecord:
        with self.unit_of_work() as work:
            return work.sealed.write_report(report)


class SQLiteSealedRepository(TransactionalSealedRepository):
    """Convenience facade backed by the main single-host SQLite UoW."""

    def __init__(self, database_path: str | Path, *, timeout: float = 30.0):
        from agentpilot.persistence.sqlite import SQLiteUnitOfWorkFactory

        super().__init__(SQLiteUnitOfWorkFactory(database_path, timeout=timeout))


__all__ = ["SQLiteSealedRepository", "TransactionalSealedRepository"]
