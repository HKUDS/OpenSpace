"""Immutable contracts for the sealed-holdout lifecycle."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping

from agentpilot.domain.common import (
    deep_freeze,
    require_aware,
    require_text,
    stable_id,
)


class SealedState(str, Enum):
    SEALED = "sealed"
    AUTHORIZED = "authorized"
    EVALUATING = "evaluating"
    EVALUATED = "evaluated"
    REPORTED = "reported"


class SealedSlotState(str, Enum):
    RESERVED = "reserved"
    DISPATCHED = "dispatched"
    SETTLED = "settled"
    BLOCKED = "blocked"


class SealedSlotSide(str, Enum):
    CONTROL = "control"
    CHALLENGER = "challenger"


class SealedConflict(RuntimeError):
    """A one-shot sealed record was replayed with different content."""


def usd_to_microusd(value: float | int, *, name: str) -> int:
    """Convert public USD configuration to an exact durable integer."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and non-negative")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be finite and non-negative") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    microusd = amount * Decimal(1_000_000)
    if microusd != microusd.to_integral_value():
        raise ValueError(f"{name} supports at most six decimal places")
    return int(microusd)


def _require_microusd(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def require_keyed_commitment(value: str, *, name: str) -> str:
    require_text(value, name)
    if not value.startswith("hmac-sha256:") or len(value) != 76:
        raise ValueError(f"{name} must be an hmac-sha256 commitment")
    try:
        int(value[12:], 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be an hmac-sha256 commitment") from exc
    return value


@dataclass(frozen=True)
class SealedManifest:
    """Public commitment to a private C population, never its task list."""

    manifest_id: str
    run_id: str
    split_commitment_id: str
    evaluator_identity: str
    runtime_identity: str
    metric_protocol: str
    plan_id: str
    plan_digest: str
    expected_blocks: int
    expected_measurements_per_side: int
    max_cost_usd: float

    def __post_init__(self) -> None:
        for name in (
            "manifest_id",
            "run_id",
            "split_commitment_id",
            "evaluator_identity",
            "runtime_identity",
            "metric_protocol",
            "plan_id",
            "plan_digest",
        ):
            require_text(getattr(self, name), name)
        require_keyed_commitment(self.split_commitment_id, name="split_commitment_id")
        require_keyed_commitment(self.plan_digest, name="plan_digest")
        for name in ("expected_blocks", "expected_measurements_per_side"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        usd_to_microusd(self.max_cost_usd, name="max_cost_usd")

    @property
    def max_cost_microusd(self) -> int:
        return usd_to_microusd(self.max_cost_usd, name="max_cost_usd")


@dataclass(frozen=True)
class SealedAuthorization:
    authorization_id: str
    run_id: str
    manifest_id: str
    plan_id: str
    plan_digest: str
    root_program_id: str
    champion_program_id: str
    archive_cutoff: int
    reserved_cost_microusd: int
    authorized_at: datetime
    reason: str

    def __post_init__(self) -> None:
        for name in (
            "authorization_id",
            "run_id",
            "manifest_id",
            "plan_id",
            "plan_digest",
            "root_program_id",
            "champion_program_id",
            "reason",
        ):
            require_text(getattr(self, name), name)
        if self.root_program_id == self.champion_program_id:
            raise ValueError("sealed report needs distinct root and champion")
        if (
            isinstance(self.archive_cutoff, bool)
            or not isinstance(self.archive_cutoff, int)
            or self.archive_cutoff < 0
        ):
            raise ValueError("archive_cutoff must be non-negative")
        _require_microusd(self.reserved_cost_microusd, name="reserved_cost_microusd")
        require_aware(self.authorized_at, "authorized_at")
        expected = stable_id(
            "sealed-authorization",
            self.run_id,
            self.manifest_id,
            self.plan_id,
            self.plan_digest,
            self.root_program_id,
            self.champion_program_id,
            self.archive_cutoff,
            self.reserved_cost_microusd,
        )
        if self.authorization_id != expected:
            raise ValueError("sealed authorization id is not stable")


@dataclass(frozen=True)
class SealedEvaluationReservation:
    """Public, full-plan cost commitment created before any C I/O."""

    reservation_id: str
    authorization_id: str
    run_id: str
    plan_id: str
    plan_digest: str
    measurements_per_side: int
    max_cost_per_slot_microusd: int
    reserved_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "reservation_id",
            "authorization_id",
            "run_id",
            "plan_id",
            "plan_digest",
        ):
            require_text(getattr(self, name), name)
        if (
            isinstance(self.measurements_per_side, bool)
            or not isinstance(self.measurements_per_side, int)
            or self.measurements_per_side < 1
        ):
            raise ValueError("measurements_per_side must be a positive integer")
        _require_microusd(
            self.max_cost_per_slot_microusd,
            name="max_cost_per_slot_microusd",
        )
        require_aware(self.reserved_at, "reserved_at")
        expected = stable_id(
            "sealed-evaluation-reservation",
            self.authorization_id,
            self.run_id,
            self.plan_id,
            self.plan_digest,
            self.measurements_per_side,
            self.max_cost_per_slot_microusd,
        )
        if self.reservation_id != expected:
            raise ValueError("sealed reservation id is not stable")

    @property
    def total_reserved_microusd(self) -> int:
        return 2 * self.measurements_per_side * self.max_cost_per_slot_microusd


@dataclass(frozen=True)
class SealedEvaluationClaim:
    """Fenced single-driver claim for one recoverable sealed evaluation."""

    claim_id: str
    authorization_id: str
    run_id: str
    reservation_id: str
    owner_token: str
    fencing_token: int
    acquired_at: datetime
    renewed_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "claim_id",
            "authorization_id",
            "run_id",
            "reservation_id",
            "owner_token",
        ):
            require_text(getattr(self, name), name)
        if (
            isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token < 1
        ):
            raise ValueError("fencing_token must be a positive integer")
        for name in ("acquired_at", "renewed_at", "expires_at"):
            require_aware(getattr(self, name), name)
        if self.renewed_at < self.acquired_at:
            raise ValueError("claim renewal predates acquisition")
        if self.expires_at <= self.renewed_at:
            raise ValueError("claim must expire after its renewal")
        expected = stable_id(
            "sealed-evaluation-claim",
            self.authorization_id,
            self.run_id,
            self.reservation_id,
            self.owner_token,
            self.fencing_token,
        )
        if self.claim_id != expected:
            raise ValueError("sealed evaluation claim id is not stable")

    def is_live_at(self, now: datetime) -> bool:
        require_aware(now, "now")
        return self.renewed_at <= now < self.expires_at

    def renew(self, *, now: datetime, ttl: timedelta) -> "SealedEvaluationClaim":
        require_aware(now, "now")
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("ttl must be a positive timedelta")
        if now < self.renewed_at or not self.is_live_at(now):
            raise SealedConflict("sealed evaluation claim is not renewable")
        return SealedEvaluationClaim(
            claim_id=self.claim_id,
            authorization_id=self.authorization_id,
            run_id=self.run_id,
            reservation_id=self.reservation_id,
            owner_token=self.owner_token,
            fencing_token=self.fencing_token,
            acquired_at=self.acquired_at,
            renewed_at=now,
            expires_at=now + ttl,
        )


@dataclass(frozen=True)
class SealedSlotSettlement:
    settlement_id: str
    slot_id: str
    execution_id: str
    dispatch_fencing_token: int
    result_identity: str
    actual_cost_microusd: int
    settled_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "settlement_id",
            "slot_id",
            "execution_id",
            "result_identity",
        ):
            require_text(getattr(self, name), name)
        require_keyed_commitment(self.result_identity, name="result_identity")
        if (
            isinstance(self.dispatch_fencing_token, bool)
            or not isinstance(self.dispatch_fencing_token, int)
            or self.dispatch_fencing_token < 1
        ):
            raise ValueError("dispatch_fencing_token must be a positive integer")
        _require_microusd(self.actual_cost_microusd, name="actual_cost_microusd")
        require_aware(self.settled_at, "settled_at")
        expected = stable_id(
            "sealed-slot-settlement",
            self.slot_id,
            self.execution_id,
            self.dispatch_fencing_token,
            self.result_identity,
            self.actual_cost_microusd,
        )
        if self.settlement_id != expected:
            raise ValueError("sealed settlement id is not stable")


@dataclass(frozen=True)
class SealedCostSlot:
    """Opaque public ledger slot; private task membership stays external."""

    slot_id: str
    reservation_id: str
    authorization_id: str
    run_id: str
    plan_id: str
    plan_digest: str
    ordinal: int
    side: SealedSlotSide
    reserved_microusd: int
    execution_id: str
    dispatch_fencing_token: int | None = None
    state: SealedSlotState = SealedSlotState.RESERVED
    dispatched_at: datetime | None = None
    settlement: SealedSlotSettlement | None = None

    def __post_init__(self) -> None:
        for name in (
            "slot_id",
            "reservation_id",
            "authorization_id",
            "run_id",
            "plan_id",
            "plan_digest",
            "execution_id",
        ):
            require_text(getattr(self, name), name)
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise ValueError("ordinal must be an integer")
        if self.ordinal < 0:
            raise ValueError("ordinal must be non-negative")
        if not isinstance(self.side, SealedSlotSide):
            raise TypeError("side must be a SealedSlotSide")
        _require_microusd(self.reserved_microusd, name="reserved_microusd")
        if not isinstance(self.state, SealedSlotState):
            raise TypeError("state must be a SealedSlotState")
        expected_slot_id = stable_id(
            "sealed-cost-slot",
            self.reservation_id,
            self.authorization_id,
            self.run_id,
            self.plan_id,
            self.plan_digest,
            self.ordinal,
            self.side,
            self.reserved_microusd,
        )
        if self.slot_id != expected_slot_id:
            raise ValueError("sealed cost slot id is not stable")
        expected_execution_id = stable_id("sealed-execution", self.slot_id)
        if self.execution_id != expected_execution_id:
            raise ValueError("sealed execution id is not stable")
        if self.state is SealedSlotState.RESERVED:
            if (
                self.dispatch_fencing_token is not None
                or self.dispatched_at is not None
                or self.settlement is not None
            ):
                raise ValueError("reserved slot cannot contain dispatch facts")
        elif self.state is SealedSlotState.DISPATCHED:
            if (
                self.dispatch_fencing_token is None
                or self.dispatched_at is None
                or self.settlement is not None
            ):
                raise ValueError("dispatched slot needs only dispatched_at")
        elif (
            self.dispatch_fencing_token is None
            or self.dispatched_at is None
            or self.settlement is None
        ):
            raise ValueError("terminal slot needs dispatch and settlement facts")
        if self.dispatch_fencing_token is not None and (
            isinstance(self.dispatch_fencing_token, bool)
            or not isinstance(self.dispatch_fencing_token, int)
            or self.dispatch_fencing_token < 1
        ):
            raise ValueError("dispatch_fencing_token must be a positive integer")
        if self.dispatched_at is not None:
            require_aware(self.dispatched_at, "dispatched_at")
        if self.settlement is not None:
            if self.settlement.slot_id != self.slot_id:
                raise ValueError("settlement references another slot")
            if self.settlement.execution_id != self.execution_id:
                raise ValueError("settlement references another execution")
            if self.settlement.dispatch_fencing_token != self.dispatch_fencing_token:
                raise ValueError("settlement references another dispatch fence")
            if self.settlement.settled_at < self.dispatched_at:
                raise ValueError("settlement predates dispatch")


def make_sealed_slots(
    reservation: SealedEvaluationReservation,
) -> tuple[SealedCostSlot, ...]:
    slots: list[SealedCostSlot] = []
    for ordinal in range(reservation.measurements_per_side):
        for side in (SealedSlotSide.CONTROL, SealedSlotSide.CHALLENGER):
            coordinates = (
                reservation.reservation_id,
                reservation.authorization_id,
                reservation.run_id,
                reservation.plan_id,
                reservation.plan_digest,
                ordinal,
                side,
                reservation.max_cost_per_slot_microusd,
            )
            slot_id = stable_id("sealed-cost-slot", *coordinates)
            slots.append(
                SealedCostSlot(
                    slot_id=slot_id,
                    reservation_id=reservation.reservation_id,
                    authorization_id=reservation.authorization_id,
                    run_id=reservation.run_id,
                    plan_id=reservation.plan_id,
                    plan_digest=reservation.plan_digest,
                    ordinal=ordinal,
                    side=side,
                    reserved_microusd=reservation.max_cost_per_slot_microusd,
                    execution_id=stable_id("sealed-execution", slot_id),
                )
            )
    return tuple(slots)


def _new_evaluation_claim(
    *,
    authorization: SealedAuthorization,
    reservation: SealedEvaluationReservation,
    owner_token: str,
    fencing_token: int,
    now: datetime,
    ttl: timedelta,
) -> SealedEvaluationClaim:
    claim_id = stable_id(
        "sealed-evaluation-claim",
        authorization.authorization_id,
        authorization.run_id,
        reservation.reservation_id,
        owner_token,
        fencing_token,
    )
    return SealedEvaluationClaim(
        claim_id=claim_id,
        authorization_id=authorization.authorization_id,
        run_id=authorization.run_id,
        reservation_id=reservation.reservation_id,
        owner_token=owner_token,
        fencing_token=fencing_token,
        acquired_at=now,
        renewed_at=now,
        expires_at=now + ttl,
    )


@dataclass(frozen=True)
class SealedEvaluationReceipt:
    """Engine-produced binding between one complete C summary and its plan."""

    receipt_id: str
    authorization_id: str
    run_id: str
    manifest_id: str
    plan_id: str
    plan_digest: str
    root_program_id: str
    champion_program_id: str
    summary_digest: str
    measured_blocks: int
    measurements_per_side: int
    settlement_ids: tuple[str, ...]
    public_result: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in (
            "receipt_id",
            "authorization_id",
            "run_id",
            "manifest_id",
            "plan_id",
            "plan_digest",
            "root_program_id",
            "champion_program_id",
            "summary_digest",
        ):
            require_text(getattr(self, name), name)
        for name in ("measured_blocks", "measurements_per_side"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        settlement_ids = tuple(self.settlement_ids)
        object.__setattr__(self, "settlement_ids", settlement_ids)
        if len(settlement_ids) != 2 * self.measurements_per_side:
            raise ValueError("sealed receipt needs every paired settlement")
        if len(set(settlement_ids)) != len(settlement_ids):
            raise ValueError("sealed receipt has duplicate settlements")
        for settlement_id in settlement_ids:
            require_text(settlement_id, "settlement_id")
        expected = stable_id(
            "sealed-evaluation-receipt",
            self.authorization_id,
            self.run_id,
            self.manifest_id,
            self.plan_id,
            self.plan_digest,
            self.root_program_id,
            self.champion_program_id,
            self.summary_digest,
            self.measured_blocks,
            self.measurements_per_side,
            self.settlement_ids,
            self.public_result,
        )
        if self.receipt_id != expected:
            raise ValueError("sealed evaluation receipt id is not stable")
        if not isinstance(self.public_result, Mapping):
            raise TypeError("public_result must be a mapping")
        expected_keys = {
            "primary_delta_mean",
            "primary_delta_standard_error",
            "effective_sample_size",
            "challenger_absolute_value",
            "constraints_pass",
            "estimator_protocol",
        }
        if set(self.public_result) != expected_keys:
            raise ValueError("sealed public result does not match the allowlist")
        for name in (
            "primary_delta_mean",
            "primary_delta_standard_error",
            "effective_sample_size",
            "challenger_absolute_value",
        ):
            value = self.public_result[name]
            if (
                name == "primary_delta_standard_error"
                and value is None
                and self.public_result["estimator_protocol"]
                in {
                    "uniform-complete-paired-replicates-v2",
                    "horvitz-thompson-paired-delta-replicates-v2",
                }
            ):
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"sealed public result {name} must be finite")
        constraints_pass = self.public_result["constraints_pass"]
        if constraints_pass is not None and not isinstance(constraints_pass, bool):
            raise ValueError("sealed public constraints must be bool or None")
        if (
            self.public_result["primary_delta_standard_error"] is not None
            and self.public_result["primary_delta_standard_error"] < 0
        ):
            raise ValueError("sealed public standard error must be non-negative")
        effective_sample_size = self.public_result["effective_sample_size"]
        if not 0 < effective_sample_size <= self.measured_blocks:
            raise ValueError("sealed public effective sample size is invalid")
        protocol = self.public_result["estimator_protocol"]
        if protocol not in {
            "uniform-complete-paired",
            "horvitz-thompson-paired-delta",
            "uniform-complete-paired-replicates-v2",
            "horvitz-thompson-paired-delta-replicates-v2",
        }:
            raise ValueError("sealed estimator protocol is not allowlisted")
        expected_summary = stable_id(
            "sealed-public-summary",
            self.plan_digest,
            self.settlement_ids,
            self.public_result,
        )
        if self.summary_digest != expected_summary:
            raise ValueError("sealed public summary is not settlement-derived")
        object.__setattr__(self, "public_result", deep_freeze(self.public_result))


@dataclass(frozen=True)
class SealedReport:
    report_id: str
    authorization_id: str
    evaluation_receipt_id: str
    run_id: str
    manifest_id: str
    plan_id: str
    plan_digest: str
    root_program_id: str
    champion_program_id: str
    summary_digest: str
    measured_blocks: int
    measurements_per_side: int
    completed_at: datetime
    cost_microusd: int
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "report_id",
            "authorization_id",
            "evaluation_receipt_id",
            "run_id",
            "manifest_id",
            "plan_id",
            "plan_digest",
            "root_program_id",
            "champion_program_id",
            "summary_digest",
        ):
            require_text(getattr(self, name), name)
        for name in ("measured_blocks", "measurements_per_side"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        require_aware(self.completed_at, "completed_at")
        _require_microusd(self.cost_microusd, name="cost_microusd")
        expected = stable_id(
            "sealed-report",
            self.authorization_id,
            self.evaluation_receipt_id,
            self.summary_digest,
            self.payload,
        )
        if self.report_id != expected:
            raise ValueError("sealed report id is not stable")
        object.__setattr__(self, "payload", deep_freeze(self.payload))

    @property
    def cost_usd(self) -> float:
        return self.cost_microusd / 1_000_000


@dataclass(frozen=True)
class SealedRecord:
    manifest: SealedManifest
    state: SealedState = SealedState.SEALED
    authorization: SealedAuthorization | None = None
    reservation: SealedEvaluationReservation | None = None
    evaluation_claim: SealedEvaluationClaim | None = None
    cost_slots: tuple[SealedCostSlot, ...] = ()
    evaluation: SealedEvaluationReceipt | None = None
    report: SealedReport | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, SealedManifest):
            raise TypeError("sealed record needs a SealedManifest")
        if not isinstance(self.state, SealedState):
            raise TypeError("state must be a SealedState")
        if self.authorization is not None and not isinstance(
            self.authorization, SealedAuthorization
        ):
            raise TypeError("authorization must be a SealedAuthorization")
        if self.reservation is not None and not isinstance(
            self.reservation, SealedEvaluationReservation
        ):
            raise TypeError("reservation must be a SealedEvaluationReservation")
        if self.evaluation_claim is not None and not isinstance(
            self.evaluation_claim, SealedEvaluationClaim
        ):
            raise TypeError("evaluation_claim must be a SealedEvaluationClaim")
        slots = tuple(self.cost_slots)
        object.__setattr__(self, "cost_slots", slots)
        if any(not isinstance(slot, SealedCostSlot) for slot in slots):
            raise TypeError("cost_slots must contain SealedCostSlot values")
        if len({slot.slot_id for slot in slots}) != len(slots):
            raise ValueError("sealed record has duplicate cost slots")
        if self.evaluation is not None and not isinstance(
            self.evaluation, SealedEvaluationReceipt
        ):
            raise TypeError("evaluation must be a SealedEvaluationReceipt")
        if self.report is not None and not isinstance(self.report, SealedReport):
            raise TypeError("report must be a SealedReport")
        if self.state is SealedState.SEALED and (
            self.authorization is not None
            or self.reservation is not None
            or self.evaluation_claim is not None
            or self.cost_slots
            or self.evaluation is not None
            or self.report is not None
        ):
            raise ValueError("sealed record cannot contain authorization/report")
        if self.state is SealedState.AUTHORIZED and (
            self.authorization is None
            or self.reservation is not None
            or self.evaluation_claim is not None
            or self.cost_slots
            or self.evaluation is not None
            or self.report is not None
        ):
            raise ValueError("authorized record needs only an authorization")
        if self.state is SealedState.EVALUATING and (
            self.authorization is None
            or self.reservation is None
            or self.evaluation_claim is None
            or not self.cost_slots
            or self.evaluation is not None
            or self.report is not None
        ):
            raise ValueError("evaluating record needs authorization/reservation/slots")
        if self.state is SealedState.EVALUATED and (
            self.authorization is None
            or self.reservation is None
            or self.evaluation_claim is None
            or not self.cost_slots
            or self.evaluation is None
            or self.report is not None
        ):
            raise ValueError("evaluated record needs authorization and receipt")
        if self.state is SealedState.REPORTED and (
            self.authorization is None
            or self.reservation is None
            or self.evaluation_claim is None
            or not self.cost_slots
            or self.evaluation is None
            or self.report is None
        ):
            raise ValueError("reported record needs authorization/receipt/report")
        if self.reservation is not None:
            expected_slots = make_sealed_slots(self.reservation)
            expected_coordinates = tuple(
                (slot.slot_id, slot.ordinal, slot.side, slot.reserved_microusd)
                for slot in expected_slots
            )
            actual_coordinates = tuple(
                (slot.slot_id, slot.ordinal, slot.side, slot.reserved_microusd)
                for slot in slots
            )
            if actual_coordinates != expected_coordinates:
                raise ValueError("sealed cost slots differ from full reservation")
            if self.authorization is None:
                raise ValueError("reservation references another authorization")
            reservation_coordinates = (
                self.reservation.authorization_id,
                self.reservation.run_id,
                self.reservation.plan_id,
                self.reservation.plan_digest,
                self.reservation.measurements_per_side,
            )
            expected_reservation_coordinates = (
                self.authorization.authorization_id,
                self.authorization.run_id,
                self.authorization.plan_id,
                self.authorization.plan_digest,
                self.manifest.expected_measurements_per_side,
            )
            if reservation_coordinates != expected_reservation_coordinates:
                raise ValueError("reservation differs from sealed coordinates")
            if self.reservation.reserved_at < self.authorization.authorized_at:
                raise ValueError("reservation predates sealed authorization")
            if (
                self.reservation.total_reserved_microusd
                > self.authorization.reserved_cost_microusd
            ):
                raise ValueError("reservation exceeds sealed authorization")
            if self.evaluation_claim is None or (
                self.evaluation_claim.reservation_id != self.reservation.reservation_id
            ):
                raise ValueError("evaluation claim references another reservation")
            assert self.authorization is not None
            claim_coordinates = (
                self.evaluation_claim.authorization_id,
                self.evaluation_claim.run_id,
                self.evaluation_claim.reservation_id,
            )
            expected_claim_coordinates = (
                self.authorization.authorization_id,
                self.authorization.run_id,
                self.reservation.reservation_id,
            )
            if claim_coordinates != expected_claim_coordinates:
                raise ValueError("evaluation claim differs from sealed coordinates")
            if self.evaluation_claim.acquired_at < self.reservation.reserved_at:
                raise ValueError("evaluation claim predates sealed reservation")
        if self.state in {SealedState.EVALUATED, SealedState.REPORTED} and any(
            slot.state is not SealedSlotState.SETTLED for slot in slots
        ):
            raise ValueError("evaluated sealed record needs every slot settled")
        if self.state in {SealedState.EVALUATED, SealedState.REPORTED}:
            assert self.authorization is not None
            if any(
                slot.settlement is not None
                and slot.settlement.actual_cost_microusd > slot.reserved_microusd
                for slot in slots
            ):
                raise ValueError("evaluated sealed record exceeds a slot cap")
            actual_cost = sum(
                slot.settlement.actual_cost_microusd
                for slot in slots
                if slot.settlement is not None
            )
            if actual_cost > self.authorization.reserved_cost_microusd:
                raise ValueError("evaluated sealed record exceeds authorization")
        if self.authorization is not None:
            authorization_coordinates = (
                self.authorization.run_id,
                self.authorization.manifest_id,
                self.authorization.plan_id,
                self.authorization.plan_digest,
                self.authorization.reserved_cost_microusd,
            )
            manifest_coordinates = (
                self.manifest.run_id,
                self.manifest.manifest_id,
                self.manifest.plan_id,
                self.manifest.plan_digest,
                self.manifest.max_cost_microusd,
            )
            if authorization_coordinates != manifest_coordinates:
                raise ValueError("authorization differs from sealed manifest")
        if self.evaluation is not None:
            assert self.authorization is not None
            evaluation_coordinates = (
                self.evaluation.authorization_id,
                self.evaluation.run_id,
                self.evaluation.manifest_id,
                self.evaluation.plan_id,
                self.evaluation.plan_digest,
                self.evaluation.root_program_id,
                self.evaluation.champion_program_id,
            )
            authorization_coordinates = (
                self.authorization.authorization_id,
                self.authorization.run_id,
                self.authorization.manifest_id,
                self.authorization.plan_id,
                self.authorization.plan_digest,
                self.authorization.root_program_id,
                self.authorization.champion_program_id,
            )
            if evaluation_coordinates != authorization_coordinates:
                raise ValueError("evaluation differs from sealed authorization")
            if (
                self.evaluation.measured_blocks != self.manifest.expected_blocks
                or self.evaluation.measurements_per_side
                != self.manifest.expected_measurements_per_side
            ):
                raise ValueError("evaluation differs from sealed census")
            settlement_ids = tuple(
                slot.settlement.settlement_id
                for slot in slots
                if slot.settlement is not None
            )
            if self.evaluation.settlement_ids != settlement_ids:
                raise ValueError("evaluation differs from sealed settlements")
        if self.report is not None:
            assert self.authorization is not None
            assert self.evaluation is not None
            report_coordinates = (
                self.report.authorization_id,
                self.report.evaluation_receipt_id,
                self.report.run_id,
                self.report.manifest_id,
                self.report.plan_id,
                self.report.plan_digest,
                self.report.root_program_id,
                self.report.champion_program_id,
                self.report.summary_digest,
                self.report.measured_blocks,
                self.report.measurements_per_side,
                self.report.payload,
            )
            evaluation_coordinates = (
                self.authorization.authorization_id,
                self.evaluation.receipt_id,
                self.authorization.run_id,
                self.authorization.manifest_id,
                self.authorization.plan_id,
                self.authorization.plan_digest,
                self.authorization.root_program_id,
                self.authorization.champion_program_id,
                self.evaluation.summary_digest,
                self.evaluation.measured_blocks,
                self.evaluation.measurements_per_side,
                self.evaluation.public_result,
            )
            if report_coordinates != evaluation_coordinates:
                raise ValueError("report differs from sealed evaluation")
            ledger_cost = sum(
                slot.settlement.actual_cost_microusd
                for slot in slots
                if slot.settlement is not None
            )
            if self.report.cost_microusd != ledger_cost:
                raise ValueError("report cost differs from sealed ledger")
            latest_settlement = max(
                slot.settlement.settled_at
                for slot in slots
                if slot.settlement is not None
            )
            if self.report.completed_at < latest_settlement:
                raise ValueError("report predates its sealed settlements")


__all__ = [
    "SealedAuthorization",
    "SealedConflict",
    "SealedCostSlot",
    "SealedEvaluationClaim",
    "SealedEvaluationReservation",
    "SealedEvaluationReceipt",
    "SealedManifest",
    "SealedRecord",
    "SealedReport",
    "SealedSlotSettlement",
    "SealedSlotSide",
    "SealedSlotState",
    "SealedState",
    "make_sealed_slots",
    "require_keyed_commitment",
    "usd_to_microusd",
]
