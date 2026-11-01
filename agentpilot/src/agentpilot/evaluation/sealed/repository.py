"""Repository port and in-memory sealed-holdout state machine."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Callable, Mapping, MutableMapping, Protocol

from agentpilot.domain.common import require_aware, require_text
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
    SealedSlotState,
    SealedState,
    _new_evaluation_claim,
    make_sealed_slots,
)


class SealedRepository(Protocol):
    def add_manifest(self, manifest: SealedManifest) -> None: ...

    def get(self, run_id: str) -> SealedRecord: ...

    def authorize(self, authorization: SealedAuthorization) -> SealedRecord: ...

    def begin_evaluation(
        self,
        reservation: SealedEvaluationReservation,
        *,
        owner_token: str,
        now: datetime,
        ttl: timedelta,
    ) -> SealedRecord: ...

    def renew_evaluation_claim(
        self,
        claim: SealedEvaluationClaim,
        *,
        now: datetime,
        ttl: timedelta,
    ) -> SealedEvaluationClaim: ...

    def dispatch_slot(
        self,
        claim: SealedEvaluationClaim,
        *,
        slot_id: str,
        dispatched_at: datetime,
    ) -> tuple[SealedCostSlot, bool]: ...

    def settle_slot(
        self,
        claim: SealedEvaluationClaim,
        settlement: SealedSlotSettlement,
    ) -> SealedCostSlot: ...

    def record_evaluation(
        self,
        claim: SealedEvaluationClaim,
        receipt: SealedEvaluationReceipt,
        *,
        recorded_at: datetime,
    ) -> SealedRecord: ...

    def write_report(self, report: SealedReport) -> SealedRecord: ...


class InMemorySealedRepository:
    """Thread-safe reference port; formal runs need a durable implementation."""

    def __init__(
        self,
        records: Mapping[str, SealedRecord] | None = None,
        *,
        copy_records: bool = True,
        registration_guard: Callable[[str], bool] | None = None,
        authorization_guard: Callable[[SealedAuthorization], bool] | None = None,
    ) -> None:
        if (
            not copy_records
            and records is not None
            and not isinstance(records, MutableMapping)
        ):
            raise TypeError("shared sealed records must be a mutable mapping")
        seeded = dict(records or {}) if copy_records else records
        if seeded is None:
            seeded = {}
        for run_id, record in seeded.items():
            require_text(run_id, "sealed record run id")
            if not isinstance(record, SealedRecord):
                raise TypeError("seeded sealed records must be SealedRecord values")
            if record.manifest.run_id != run_id:
                raise ValueError("seeded sealed record key differs from manifest run")
        self._records: MutableMapping[str, SealedRecord] = seeded
        self._registration_guard = registration_guard
        self._authorization_guard = authorization_guard
        self._lock = threading.Lock()

    def add_manifest(self, manifest: SealedManifest) -> None:
        if not isinstance(manifest, SealedManifest):
            raise TypeError("manifest must be a SealedManifest")
        with self._lock:
            current = self._records.get(manifest.run_id)
            expected = SealedRecord(manifest)
            if current is None:
                if self._registration_guard is not None and self._registration_guard(
                    manifest.run_id
                ):
                    raise SealedConflict(
                        "sealed manifest must be registered before search"
                    )
                self._records[manifest.run_id] = expected
            elif current.manifest != manifest:
                raise SealedConflict("sealed manifest already differs")

    def get(self, run_id: str) -> SealedRecord:
        with self._lock:
            try:
                return self._records[run_id]
            except KeyError as exc:
                raise KeyError(f"no sealed manifest for run {run_id!r}") from exc

    def authorize(self, authorization: SealedAuthorization) -> SealedRecord:
        if not isinstance(authorization, SealedAuthorization):
            raise TypeError("authorization must be a SealedAuthorization")
        with self._lock:
            if self._authorization_guard is not None and not self._authorization_guard(
                authorization
            ):
                raise SealedConflict(
                    "sealed authorization lacks a durable terminal proof"
                )
            current = self._records[authorization.run_id]
            manifest = current.manifest
            if (
                manifest.run_id != authorization.run_id
                or manifest.manifest_id != authorization.manifest_id
                or manifest.plan_id != authorization.plan_id
                or manifest.plan_digest != authorization.plan_digest
                or manifest.max_cost_microusd != authorization.reserved_cost_microusd
            ):
                raise SealedConflict("authorization references another manifest/plan")
            desired = SealedRecord(manifest, SealedState.AUTHORIZED, authorization)
            if current.state is SealedState.SEALED:
                self._records[authorization.run_id] = desired
                return desired
            if (
                current.authorization is not None
                and current.authorization.authorization_id
                == authorization.authorization_id
            ):
                return current
            raise SealedConflict("sealed holdout was already authorized")

    def begin_evaluation(
        self,
        reservation: SealedEvaluationReservation,
        *,
        owner_token: str,
        now: datetime,
        ttl: timedelta,
    ) -> SealedRecord:
        if not isinstance(reservation, SealedEvaluationReservation):
            raise TypeError("reservation must be a SealedEvaluationReservation")
        require_text(owner_token, "owner_token")
        require_aware(now, "now")
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("ttl must be a positive timedelta")
        with self._lock:
            current = self._records[reservation.run_id]
            authorization = current.authorization
            manifest = current.manifest
            if authorization is None:
                raise SealedConflict("sealed evaluation has no authorization")
            coordinates = (
                reservation.authorization_id,
                reservation.run_id,
                reservation.plan_id,
                reservation.plan_digest,
                reservation.measurements_per_side,
            )
            expected = (
                authorization.authorization_id,
                authorization.run_id,
                authorization.plan_id,
                authorization.plan_digest,
                manifest.expected_measurements_per_side,
            )
            if coordinates != expected:
                raise SealedConflict("reservation differs from sealed plan")
            if reservation.reserved_at < authorization.authorized_at:
                raise SealedConflict("reservation predates authorization")
            if (
                reservation.total_reserved_microusd
                > authorization.reserved_cost_microusd
            ):
                raise SealedConflict("full sealed plan reservation exceeds budget")

            if current.state is SealedState.AUTHORIZED:
                claim = _new_evaluation_claim(
                    authorization=authorization,
                    reservation=reservation,
                    owner_token=owner_token,
                    fencing_token=1,
                    now=now,
                    ttl=ttl,
                )
                desired = SealedRecord(
                    manifest=manifest,
                    state=SealedState.EVALUATING,
                    authorization=authorization,
                    reservation=reservation,
                    evaluation_claim=claim,
                    cost_slots=make_sealed_slots(reservation),
                )
                self._records[reservation.run_id] = desired
                return desired
            if current.state is not SealedState.EVALUATING:
                raise SealedConflict("sealed evaluation is already complete")
            if (
                current.reservation is None
                or current.reservation.reservation_id != reservation.reservation_id
            ):
                raise SealedConflict("sealed evaluation reservation is write-once")
            assert current.evaluation_claim is not None
            claim = current.evaluation_claim
            if now < claim.renewed_at:
                raise SealedConflict("sealed evaluation clock regressed")
            if claim.is_live_at(now):
                if claim.owner_token != owner_token:
                    raise SealedConflict("sealed evaluation already has a live driver")
                return current
            takeover = _new_evaluation_claim(
                authorization=authorization,
                reservation=reservation,
                owner_token=owner_token,
                fencing_token=claim.fencing_token + 1,
                now=now,
                ttl=ttl,
            )
            desired = replace(current, evaluation_claim=takeover)
            self._records[reservation.run_id] = desired
            return desired

    def renew_evaluation_claim(
        self,
        claim: SealedEvaluationClaim,
        *,
        now: datetime,
        ttl: timedelta,
    ) -> SealedEvaluationClaim:
        if not isinstance(claim, SealedEvaluationClaim):
            raise TypeError("claim must be a SealedEvaluationClaim")
        with self._lock:
            current = self._records[claim.run_id]
            if current.state is not SealedState.EVALUATING:
                raise SealedConflict("sealed evaluation is not active")
            if current.evaluation_claim != claim:
                raise SealedConflict("sealed evaluation claim was superseded")
            renewed = claim.renew(now=now, ttl=ttl)
            self._records[claim.run_id] = replace(current, evaluation_claim=renewed)
            return renewed

    def dispatch_slot(
        self,
        claim: SealedEvaluationClaim,
        *,
        slot_id: str,
        dispatched_at: datetime,
    ) -> tuple[SealedCostSlot, bool]:
        if not isinstance(claim, SealedEvaluationClaim):
            raise TypeError("claim must be a SealedEvaluationClaim")
        require_text(slot_id, "slot_id")
        require_aware(dispatched_at, "dispatched_at")
        with self._lock:
            current = self._records[claim.run_id]
            if current.state is not SealedState.EVALUATING:
                raise SealedConflict("sealed evaluation is not active")
            if current.evaluation_claim != claim or not claim.is_live_at(dispatched_at):
                raise SealedConflict("sealed evaluation claim is not live")
            if (
                current.reservation is None
                or dispatched_at < current.reservation.reserved_at
                or current.authorization is None
                or dispatched_at < current.authorization.authorized_at
            ):
                raise SealedConflict(
                    "sealed dispatch predates reservation or authorization"
                )
            commitment = sum(
                (
                    slot.settlement.actual_cost_microusd
                    if slot.settlement is not None
                    else slot.reserved_microusd
                )
                for slot in current.cost_slots
            )
            assert current.authorization is not None
            slot_overspend = any(
                item.settlement is not None
                and item.settlement.actual_cost_microusd > item.reserved_microusd
                for item in current.cost_slots
            )
            if (
                slot_overspend
                or commitment > current.authorization.reserved_cost_microusd
            ):
                raise SealedConflict("sealed cost ledger exceeds budget")
            slots = list(current.cost_slots)
            for index, slot in enumerate(slots):
                if slot.slot_id != slot_id:
                    continue
                if slot.state is not SealedSlotState.RESERVED:
                    return slot, False
                dispatched = replace(
                    slot,
                    dispatch_fencing_token=claim.fencing_token,
                    state=SealedSlotState.DISPATCHED,
                    dispatched_at=dispatched_at,
                )
                slots[index] = dispatched
                self._records[claim.run_id] = replace(current, cost_slots=tuple(slots))
                return dispatched, True
            raise KeyError(f"unknown sealed cost slot {slot_id!r}")

    def settle_slot(
        self,
        claim: SealedEvaluationClaim,
        settlement: SealedSlotSettlement,
    ) -> SealedCostSlot:
        if not isinstance(claim, SealedEvaluationClaim):
            raise TypeError("claim must be a SealedEvaluationClaim")
        if not isinstance(settlement, SealedSlotSettlement):
            raise TypeError("settlement must be a SealedSlotSettlement")
        with self._lock:
            for run_id, current in self._records.items():
                slots = list(current.cost_slots)
                for index, slot in enumerate(slots):
                    if slot.slot_id != settlement.slot_id:
                        continue
                    if (
                        claim.run_id != run_id
                        or claim.authorization_id != slot.authorization_id
                        or claim.reservation_id != slot.reservation_id
                    ):
                        raise SealedConflict(
                            "sealed settlement claim has different coordinates"
                        )
                    if slot.state is SealedSlotState.RESERVED:
                        raise SealedConflict("sealed slot was not dispatched")
                    if settlement.dispatch_fencing_token != slot.dispatch_fencing_token:
                        raise SealedConflict(
                            "sealed settlement differs from dispatch fence"
                        )
                    if slot.state in {
                        SealedSlotState.SETTLED,
                        SealedSlotState.BLOCKED,
                    }:
                        if (
                            slot.settlement is not None
                            and slot.settlement.settlement_id
                            == settlement.settlement_id
                        ):
                            if (
                                slot.state is SealedSlotState.BLOCKED
                                and current.evaluation_claim == claim
                                and claim.is_live_at(settlement.settled_at)
                            ):
                                accepted = replace(slot, state=SealedSlotState.SETTLED)
                                slots[index] = accepted
                                self._records[run_id] = replace(
                                    current, cost_slots=tuple(slots)
                                )
                                return accepted
                            return slot
                        raise SealedConflict("sealed slot settlement is write-once")
                    current_claim = current.evaluation_claim
                    claim_is_current = current_claim == claim and claim.is_live_at(
                        settlement.settled_at
                    )
                    terminal_state = (
                        SealedSlotState.SETTLED
                        if claim_is_current
                        else SealedSlotState.BLOCKED
                    )
                    settled = replace(
                        slot,
                        state=terminal_state,
                        settlement=settlement,
                    )
                    slots[index] = settled
                    self._records[run_id] = replace(current, cost_slots=tuple(slots))
                    return settled
            raise KeyError(f"unknown sealed cost slot {settlement.slot_id!r}")

    def record_evaluation(
        self,
        claim: SealedEvaluationClaim,
        receipt: SealedEvaluationReceipt,
        *,
        recorded_at: datetime,
    ) -> SealedRecord:
        if not isinstance(claim, SealedEvaluationClaim):
            raise TypeError("claim must be a SealedEvaluationClaim")
        if not isinstance(receipt, SealedEvaluationReceipt):
            raise TypeError("receipt must be a SealedEvaluationReceipt")
        require_aware(recorded_at, "recorded_at")
        with self._lock:
            current = self._records[receipt.run_id]
            manifest = current.manifest
            authorization = current.authorization
            if authorization is None:
                raise SealedConflict("sealed evaluation has no authorization")
            if (
                current.state in {SealedState.EVALUATED, SealedState.REPORTED}
                and current.evaluation_claim == claim
                and current.evaluation == receipt
            ):
                return current
            if current.evaluation_claim != claim:
                raise SealedConflict("sealed evaluation claim was superseded")
            if not claim.is_live_at(recorded_at):
                raise SealedConflict("sealed evaluation claim is not live")
            expected_coordinates = (
                authorization.authorization_id,
                authorization.run_id,
                authorization.manifest_id,
                authorization.plan_id,
                authorization.plan_digest,
                authorization.root_program_id,
                authorization.champion_program_id,
            )
            actual_coordinates = (
                receipt.authorization_id,
                receipt.run_id,
                receipt.manifest_id,
                receipt.plan_id,
                receipt.plan_digest,
                receipt.root_program_id,
                receipt.champion_program_id,
            )
            if actual_coordinates != expected_coordinates:
                raise SealedConflict(
                    "evaluation receipt coordinates differ from authorization"
                )
            if (
                receipt.plan_digest != manifest.plan_digest
                or receipt.measured_blocks != manifest.expected_blocks
                or receipt.measurements_per_side
                != manifest.expected_measurements_per_side
            ):
                raise SealedConflict(
                    "sealed evaluation receipt does not cover the committed plan"
                )
            if current.reservation is None or not current.cost_slots:
                raise SealedConflict("sealed evaluation has no full-plan reservation")
            if any(
                slot.state is not SealedSlotState.SETTLED for slot in current.cost_slots
            ):
                raise SealedConflict("sealed evaluation has unsettled cost slots")
            settlement_ids = tuple(
                slot.settlement.settlement_id
                for slot in current.cost_slots
                if slot.settlement is not None
            )
            if receipt.settlement_ids != settlement_ids:
                raise SealedConflict(
                    "sealed evaluation receipt differs from cost settlements"
                )
            committed_cost = sum(
                slot.settlement.actual_cost_microusd
                for slot in current.cost_slots
                if slot.settlement is not None
            )
            if committed_cost > authorization.reserved_cost_microusd or any(
                slot.settlement is not None
                and slot.settlement.actual_cost_microusd > slot.reserved_microusd
                for slot in current.cost_slots
            ):
                raise SealedConflict("sealed evaluation exceeded its cost reservation")
            desired = SealedRecord(
                manifest=manifest,
                state=SealedState.EVALUATED,
                authorization=authorization,
                reservation=current.reservation,
                evaluation_claim=current.evaluation_claim,
                cost_slots=current.cost_slots,
                evaluation=receipt,
            )
            if current.state is SealedState.EVALUATING:
                self._records[receipt.run_id] = desired
                return desired
            if current.evaluation == receipt:
                return current
            raise SealedConflict("sealed evaluation receipt is write-once")

    def write_report(self, report: SealedReport) -> SealedRecord:
        if not isinstance(report, SealedReport):
            raise TypeError("report must be a SealedReport")
        with self._lock:
            current = self._records[report.run_id]
            manifest = current.manifest
            authorization = current.authorization
            evaluation = current.evaluation
            if authorization is None or evaluation is None:
                raise SealedConflict(
                    "sealed report has no completed evaluation receipt"
                )
            expected_coordinates = (
                authorization.authorization_id,
                evaluation.receipt_id,
                authorization.run_id,
                authorization.manifest_id,
                authorization.plan_id,
                authorization.plan_digest,
                authorization.root_program_id,
                authorization.champion_program_id,
                evaluation.summary_digest,
                evaluation.public_result,
            )
            actual_coordinates = (
                report.authorization_id,
                report.evaluation_receipt_id,
                report.run_id,
                report.manifest_id,
                report.plan_id,
                report.plan_digest,
                report.root_program_id,
                report.champion_program_id,
                report.summary_digest,
                report.payload,
            )
            if actual_coordinates != expected_coordinates:
                raise SealedConflict("report coordinates differ from authorization")
            if (
                authorization.plan_id != manifest.plan_id
                or authorization.plan_digest != manifest.plan_digest
            ):
                raise SealedConflict("authorization plan differs from manifest")
            if (
                report.measured_blocks != evaluation.measured_blocks
                or report.measurements_per_side != evaluation.measurements_per_side
            ):
                raise SealedConflict("sealed report differs from evaluation receipt")
            if report.completed_at < authorization.authorized_at:
                raise SealedConflict("sealed report predates its authorization")
            latest_settlement = max(
                slot.settlement.settled_at
                for slot in current.cost_slots
                if slot.settlement is not None
            )
            if report.completed_at < latest_settlement:
                raise SealedConflict("sealed report predates its cost settlements")
            ledger_cost = sum(
                slot.settlement.actual_cost_microusd
                for slot in current.cost_slots
                if slot.settlement is not None
            )
            if report.cost_microusd != ledger_cost:
                raise SealedConflict("sealed report cost differs from durable ledger")
            if ledger_cost > authorization.reserved_cost_microusd:
                raise SealedConflict("sealed report exceeds its cost authorization")
            desired = SealedRecord(
                manifest=manifest,
                state=SealedState.REPORTED,
                authorization=authorization,
                reservation=current.reservation,
                evaluation_claim=current.evaluation_claim,
                cost_slots=current.cost_slots,
                evaluation=evaluation,
                report=report,
            )
            if current.state is SealedState.EVALUATED:
                self._records[report.run_id] = desired
                return desired
            if current.report == report:
                return current
            raise SealedConflict("sealed report is write-once")


__all__ = ["InMemorySealedRepository", "SealedRepository"]
