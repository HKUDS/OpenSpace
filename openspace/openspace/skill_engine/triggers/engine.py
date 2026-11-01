"""TriggerEngine orchestration for rule-created TriggerJobs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from openspace.skill_engine.evidence import (
    EvidenceEvent,
    EvidenceScope,
    EvidenceStore,
    ResourceRef,
)
from openspace.utils.logging import Logger

from .policies import TriggerPolicy, default_policies
from .store import TriggerStore
from .types import ManualTriggerRequest, TriggerJob, TriggerJobSpec

logger = Logger.get_logger(__name__)


class TriggerEngine:
    """Create and persist TriggerJobs from deterministic policies."""

    def __init__(
        self,
        evidence_store: EvidenceStore,
        policies: list[TriggerPolicy] | None = None,
        *,
        trigger_store: TriggerStore | None = None,
    ) -> None:
        self.evidence_store = evidence_store
        self.store = trigger_store or TriggerStore(evidence_store=evidence_store)
        self.policies = policies if policies is not None else default_policies(evidence_store)

    def on_event(
        self,
        event: EvidenceEvent,
        watermark: int,
    ) -> list[TriggerJob]:
        jobs: list[TriggerJob] = []
        for policy in self.policies:
            try:
                specs = policy.on_event(event, watermark)
            except Exception as exc:
                self._record_policy_error(policy, "on_event", exc)
                continue
            jobs.extend(self._persist_specs(specs, watermark))
        return jobs

    def evaluate_checkpoint(
        self,
        name: str,
        scope: EvidenceScope,
        manifest_watermark: int | None = None,
    ) -> list[TriggerJob]:
        watermark = (
            int(manifest_watermark)
            if manifest_watermark is not None
            else self.store.latest_manifest_watermark()
        )
        jobs: list[TriggerJob] = []
        for policy in self.policies:
            try:
                specs = policy.evaluate_checkpoint(name, scope, watermark)
            except Exception as exc:
                self._record_policy_error(policy, "evaluate_checkpoint", exc)
                continue
            jobs.extend(self._persist_specs(specs, watermark))
        return jobs

    def evaluate_window(
        self,
        now: datetime,
        manifest_watermark: int | None = None,
    ) -> list[TriggerJob]:
        watermark = (
            int(manifest_watermark)
            if manifest_watermark is not None
            else self.store.latest_manifest_watermark()
        )
        jobs: list[TriggerJob] = []
        for policy in self.policies:
            try:
                specs = policy.evaluate_window(now, watermark)
            except Exception as exc:
                self._record_policy_error(policy, "evaluate_window", exc)
                continue
            jobs.extend(self._persist_specs(specs, watermark))
        return jobs

    def from_quality_signals(
        self,
        signal_refs: list[ResourceRef],
        *,
        manifest_watermark: int,
    ) -> list[TriggerJob]:
        from openspace.skill_engine.signals.policy import QualitySignalTriggerPolicy

        watermark = int(manifest_watermark)
        policy = QualitySignalTriggerPolicy(self.store)
        try:
            specs = policy.from_signal_refs(
                signal_refs,
                manifest_watermark=watermark,
            )
        except Exception as exc:
            self._record_policy_error(policy, "from_signal_refs", exc)
            return []
        return self._persist_specs(specs, watermark)

    def from_manual_request(
        self,
        request: ManualTriggerRequest,
    ) -> list[TriggerJob]:
        watermark = self._ingest_manual_request(request)
        jobs: list[TriggerJob] = []
        for policy in self.policies:
            try:
                specs = policy.from_manual_request(request, watermark)
            except Exception as exc:
                self._record_policy_error(policy, "from_manual_request", exc)
                continue
            jobs.extend(self._persist_specs(specs, watermark))
        return jobs

    def claim_next(
        self,
        limit: int = 1,
        worker_id: str | None = None,
        trigger_types: tuple[str, ...] | None = None,
        scope: EvidenceScope | None = None,
        claim_statuses: tuple[str, ...] | None = None,
    ) -> list[TriggerJob]:
        return self.store.claim_next(
            limit=limit,
            worker_id=worker_id,
            trigger_types=trigger_types,
            scope=scope,
            claim_statuses=claim_statuses,
        )

    def claim_jobs(
        self,
        job_ids: list[str],
        worker_id: str | None = None,
    ) -> list[TriggerJob]:
        return self.store.claim_jobs(job_ids, worker_id=worker_id)

    def complete(
        self,
        job_id: str,
        status: str,
        result_ref: str | None = None,
        error: str | None = None,
        clear_result_ref: bool = False,
    ) -> None:
        self.store.complete(
            job_id,
            status=status,
            result_ref=result_ref,
            error=error,
            clear_result_ref=clear_result_ref,
        )

    def recover_stale_jobs(self, timeout_s: float) -> int:
        return self.store.recover_stale_jobs(timeout_s=timeout_s)

    def load_checkpoint(self, checkpoint_name: str) -> str | None:
        return self.store.load_checkpoint(checkpoint_name)

    def save_checkpoint(self, checkpoint_name: str, checkpoint_value: Any) -> None:
        self.store.save_checkpoint(checkpoint_name, checkpoint_value)

    def close(self) -> None:
        self.store.close()

    def _persist_specs(
        self,
        specs: list[TriggerJobSpec],
        watermark: int,
    ) -> list[TriggerJob]:
        jobs: list[TriggerJob] = []
        for spec in specs:
            try:
                jobs.append(
                    self.store.create_job(
                        spec,
                        manifest_watermark=watermark,
                    )
                )
            except Exception:
                logger.debug("Failed to persist trigger job", exc_info=True)
        return jobs

    def _ingest_manual_request(self, request: ManualTriggerRequest) -> int:
        created_at = _utc_now()
        request_id = request.request_id or _digest(request.to_dict())
        ref = ResourceRef(
            ref_id=f"manual_request:{request_id}",
            ref_type="manual_request_ref",
            session_id=request.session_id,
            task_id=request.task_id,
            producer="trigger_engine",
            created_at=created_at,
            reliability="runtime",
            role="supporting",
            preview=f"manual {request.action}",
            metadata=request.to_dict(),
        )
        event = EvidenceEvent.create(
            event_id=f"evt_manual_request_{_digest(request.to_dict())}",
            event_type="manual_trigger_request",
            producer="trigger_engine",
            created_at=created_at,
            session_id=request.session_id,
            task_id=request.task_id,
            idempotency_key=f"manual_request:{request_id}",
            supporting_refs=[ref],
            metadata={"action": request.action, "reason": request.reason},
        )
        return self.evidence_store.ingest_event(event)

    def _record_policy_error(
        self,
        policy: TriggerPolicy,
        method: str,
        exc: BaseException,
    ) -> None:
        try:
            created_at = _utc_now()
            payload = {
                "policy": type(policy).__name__,
                "method": method,
                "error": str(exc),
            }
            event = EvidenceEvent.create(
                event_id=f"evt_trigger_policy_error_{_digest(payload)}",
                event_type="trigger_policy_error",
                producer="trigger_engine",
                created_at=created_at,
                severity="warning",
                idempotency_key=f"trigger_policy_error:{_digest(payload)}",
                metadata=payload,
            )
            self.evidence_store.ingest_event(event)
        except Exception:
            logger.debug("Failed to record trigger policy error", exc_info=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
