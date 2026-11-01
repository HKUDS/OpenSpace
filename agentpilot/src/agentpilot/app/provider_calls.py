"""Durable intent/result fencing around external completion providers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from agentpilot.domain import (
    CostLedgerEntry,
    ProviderCallDispatch,
    ProviderCallIntent,
    ProviderCallRefusal,
    ProviderCallResult,
    ProviderCallStatus,
    bounded_failure_detail,
    canonical_json,
    stable_id,
    utc_now,
)
from agentpilot.persistence import UnitOfWorkFactory
from agentpilot.providers import (
    AccountingStatus,
    CompletionProvider,
    CompletionRequest,
    CompletionResult,
    GenerationAccountingReconciler,
    ProviderAccountingPendingError,
    ProviderCallRefused,
    ProviderCompletionTruncated,
    ProviderCompletionUnusable,
    ProviderError,
    ProviderReceipt,
    ProviderReceiptUnattested,
    ProviderUsage,
)
from agentpilot.providers.base import NATIVE_MESSAGE_CONTRACT


class ProviderCallAttemptAlreadyFailed(ProviderError):
    """This dispatch has a terminal receipt and cannot be repeated."""


@dataclass(frozen=True)
class ProviderCallContext:
    call_id: str
    run_id: str
    batch_id: str
    purpose: str
    action_id: str | None = None


class DurableCompletion:
    """Persist call intent before network I/O and attested result afterwards."""

    def __init__(
        self,
        *,
        provider: CompletionProvider,
        unit_of_work: UnitOfWorkFactory,
        require_provider_reported_cost: bool = True,
        accounting_reconciler: GenerationAccountingReconciler | None = None,
        allow_pending_accounting: bool = False,
    ):
        self.provider = provider
        self.unit_of_work = unit_of_work
        self.require_provider_reported_cost = require_provider_reported_cost
        self.accounting_reconciler = accounting_reconciler
        self.allow_pending_accounting = allow_pending_accounting

    def recover(self, call_id: str) -> CompletionResult | None:
        """Consume an existing receipt only. Never dispatch a missing call."""
        with self.unit_of_work() as work:
            existing = work.usage.get_result(call_id)
            if existing is None:
                return None
            intent = work.usage.get_intent(call_id)
        return self._replay(intent, existing)

    def complete(
        self, request: CompletionRequest, *, context: ProviderCallContext
    ) -> CompletionResult:
        if context.purpose == "trace-agentic-proposer":
            # Raw trace/source observations and request-local aliases do not
            # belong in SQLite provider-intent rows.
            prompt_bytes = request.prompt.encode("utf-8")
            system_bytes = request.system.encode("utf-8")
            request_facts = {
                "prompt_persisted": False,
                "prompt_bytes": len(prompt_bytes),
                "system_bytes": len(system_bytes),
                "response_schema_present": request.response_schema is not None,
                "max_output_tokens": request.max_output_tokens,
                "temperature": request.temperature,
                "disable_reasoning": request.disable_reasoning,
                "metadata": request.metadata,
            }
        else:
            request_facts = {
                "prompt": request.prompt,
                "system": request.system,
                "max_output_tokens": request.max_output_tokens,
                "temperature": request.temperature,
                "disable_reasoning": request.disable_reasoning,
                "response_schema": request.response_schema,
                "metadata": request.metadata,
            }
        if request.reasoning_effort is not None:
            request_facts["reasoning_effort"] = request.reasoning_effort
        if request.messages is not None:
            native_payload = canonical_json(
                {
                    "contract": NATIVE_MESSAGE_CONTRACT,
                    "messages": request.messages,
                    "tools": request.tools,
                    "tool_choice": request.tool_choice,
                }
            ).encode("utf-8")
            request_facts = {
                **request_facts,
                "native_request_sha256": hashlib.sha256(native_payload).hexdigest(),
                "native_request_bytes": len(native_payload),
                "native_message_count": len(request.messages),
                "native_tool_count": len(request.tools or ()),
            }
        intent = ProviderCallIntent(
            call_id=context.call_id,
            run_id=context.run_id,
            batch_id=context.batch_id,
            provider=self.provider.provider_name,
            model=request.model,
            purpose=context.purpose,
            action_id=context.action_id,
            created_at=utc_now(),
            request_facts=request_facts,
        )
        with self.unit_of_work() as work:
            try:
                existing_intent = work.usage.get_intent(context.call_id)
            except KeyError:
                work.usage.add_intent(intent)
            else:
                # created_at is intentionally excluded from replay identity.
                if (
                    existing_intent.run_id != intent.run_id
                    or existing_intent.batch_id != intent.batch_id
                    or existing_intent.provider != intent.provider
                    or existing_intent.model != intent.model
                    or existing_intent.purpose != intent.purpose
                    or existing_intent.action_id != intent.action_id
                    or existing_intent.request_facts != intent.request_facts
                ):
                    raise ValueError("provider call intent identity drift")
            existing = work.usage.get_result(context.call_id)
            dispatched = work.usage.get_dispatch(context.call_id) is not None
        if existing is not None:
            return self._replay(intent, existing)
        if dispatched:
            # The request reached the transport and the process died before a
            # receipt landed. Nothing can prove the model did not run, so this
            # call id is quarantined rather than reissued.
            raise ProviderReceiptUnattested(
                "provider call was dispatched without a durable receipt; "
                "its cost cannot be established"
            )

        with self.unit_of_work() as work:
            work.usage.mark_dispatched(
                ProviderCallDispatch(
                    call_id=context.call_id,
                    dispatched_at=utc_now(),
                )
            )
        try:
            receipt = self.provider.complete(request)
        except ProviderCallRefused as exc:
            self._record_refusal(context.call_id, exc)
            raise
        if receipt.provider != intent.provider or receipt.model != intent.model:
            raise ValueError("completion provider returned mismatched identity")
        return self._record_receipt(intent, receipt)

    def _record_refusal(self, call_id: str, exc: ProviderCallRefused) -> None:
        with self.unit_of_work() as work:
            work.usage.record_refusal(
                ProviderCallRefusal(
                    call_id=call_id,
                    ordinal=len(work.usage.list_refusals(call_id)),
                    refused_at=utc_now(),
                    detail=str(exc),
                )
            )

    def _replay(
        self, intent: ProviderCallIntent, existing: ProviderCallResult
    ) -> CompletionResult:
        if existing.status is ProviderCallStatus.UNATTESTED:
            raise ProviderReceiptUnattested(
                f"provider call id has an unattested receipt: {existing.error_kind}"
            )
        if existing.status is ProviderCallStatus.FAILED:
            raise ProviderCallAttemptAlreadyFailed(
                f"provider call id failed reconciliation: {existing.error_kind}"
            )
        if existing.status is ProviderCallStatus.ACCOUNTING_PENDING:
            return self._settle_receipt(intent, existing)
        return self._attested_result(intent, existing)

    def _record_receipt(
        self, intent: ProviderCallIntent, receipt: ProviderReceipt
    ) -> CompletionResult:
        if receipt.settleable:
            pending = ProviderCallResult(
                call_id=intent.call_id,
                status=ProviderCallStatus.ACCOUNTING_PENDING,
                completed_at=utc_now(),
                input_tokens=receipt.input_tokens,
                output_tokens=receipt.output_tokens,
                cost_usd=receipt.cost_usd,
                accounting_status=receipt.accounting_status.value,
                provider_request_id=receipt.attestation_id,
                response_text=receipt.text,
                finish_reason=receipt.raw_finish_reason,
            )
            with self.unit_of_work() as work:
                work.usage.add_result(pending)
            return self._settle_receipt(intent, pending)
        assert receipt.defect is not None
        with self.unit_of_work() as work:
            work.usage.add_result(
                ProviderCallResult(
                    call_id=intent.call_id,
                    status=ProviderCallStatus.UNATTESTED,
                    completed_at=utc_now(),
                    input_tokens=receipt.input_tokens,
                    output_tokens=receipt.output_tokens,
                    cost_usd=None,
                    accounting_status=AccountingStatus.UNKNOWN.value,
                    provider_request_id=receipt.attestation_id,
                    response_text=receipt.text,
                    finish_reason=receipt.raw_finish_reason,
                    error_kind=receipt.defect,
                )
            )
        raise ProviderReceiptUnattested(receipt.defect)

    def _settle_receipt(
        self, intent: ProviderCallIntent, receipt: ProviderCallResult
    ) -> CompletionResult:
        if self.allow_pending_accounting and receipt.cost_usd is None:
            # The complete response is durable and validated below. Keep the
            # provider ledger ACCOUNTING_PENDING; do not invent a zero charge
            # or dispatch the same call again merely to obtain its price.
            return self._attested_result(intent, receipt)
        usage = ProviderUsage(
            input_tokens=receipt.input_tokens or 0,
            output_tokens=receipt.output_tokens or 0,
            cost_usd=receipt.cost_usd,
            accounting_status=AccountingStatus(receipt.accounting_status),
            provider_request_id=receipt.provider_request_id,
        )
        try:
            if self.accounting_reconciler is not None and not (
                usage.cost_usd is not None
                and usage.accounting_status is AccountingStatus.PROVIDER_REPORTED
            ):
                usage = self.accounting_reconciler.reconcile(
                    model=intent.model,
                    usage=usage,
                )
            self._validate_settled_usage(usage)
        except ProviderAccountingPendingError:
            raise
        except Exception as exc:
            failed = replace(
                receipt,
                status=ProviderCallStatus.FAILED,
                error_kind=type(exc).__name__,
                error_detail=bounded_failure_detail(exc),
            )
            with self.unit_of_work() as work:
                work.usage.transition_result(failed)
                if (
                    failed.cost_usd is not None
                    and failed.accounting_status != AccountingStatus.UNKNOWN.value
                ):
                    work.usage.add_cost(self._cost_entry(intent, failed))
            raise

        completed = replace(
            receipt,
            status=ProviderCallStatus.COMPLETED,
            cost_usd=usage.cost_usd,
            accounting_status=usage.accounting_status.value,
        )
        with self.unit_of_work() as work:
            work.usage.transition_result(completed)
            work.usage.add_cost(self._cost_entry(intent, completed))
        return self._attested_result(intent, completed)

    def _attested_result(
        self, intent: ProviderCallIntent, settled: ProviderCallResult
    ) -> CompletionResult:
        usage = ProviderUsage(
            input_tokens=settled.input_tokens or 0,
            output_tokens=settled.output_tokens or 0,
            cost_usd=settled.cost_usd,
            accounting_status=AccountingStatus(settled.accounting_status),
            provider_request_id=settled.provider_request_id,
        )
        finish_reason = (
            settled.finish_reason.strip().casefold()
            if isinstance(settled.finish_reason, str)
            else None
        )
        if finish_reason == "error":
            # An explicitly failed generation may still contain text or tool
            # call prefixes. Keep them in the settled receipt, not in executable
            # proposer history; a fresh attempt can use the bounded recovery.
            raise ProviderCompletionUnusable(
                "provider settled a dispatch with an explicit generation error "
                f"(finish_reason={settled.finish_reason!r})",
                usage=usage,
                settled_text=settled.response_text, finish_reason=finish_reason,
            )
        if finish_reason in {
            "length",
            "max_completion_tokens",
            "max_output_tokens",
            "max_tokens",
        }:
            # Structured callers cannot safely consume a prefix, even when the
            # provider happened to expose some final text.  The bill is already
            # booked and a fresh attempt id may ask for a smaller response.
            raise ProviderCompletionTruncated(
                "provider settled a dispatch at its output limit "
                f"(finish_reason={settled.finish_reason!r})",
                usage=usage,
                settled_text=settled.response_text, finish_reason=finish_reason,
            )
        if not settled.response_text:
            # The bill is booked, so a fresh call id can safely redo the work.
            raise ProviderCompletionUnusable(
                "provider settled a dispatch that produced no content "
                f"(finish_reason={settled.finish_reason!r})",
                usage=usage,
                settled_text=settled.response_text, finish_reason=finish_reason,
            )
        if "native_request_sha256" in intent.request_facts:
            try:
                envelope = json.loads(settled.response_text)
                message = envelope["message"]
                valid_message = (
                    envelope.get("contract") == NATIVE_MESSAGE_CONTRACT
                    and isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and (
                        bool(message.get("tool_calls"))
                        or (
                            bool(message.get("content", "").strip())
                            if isinstance(message.get("content"), str)
                            else bool(message.get("content"))
                        )
                    )
                )
            except (TypeError, ValueError, KeyError, AttributeError):
                valid_message = False
            if not valid_message:
                raise ProviderCompletionUnusable(
                    "provider settled a native dispatch without assistant content or tool calls",
                    usage=usage,
                    settled_text=settled.response_text, finish_reason=finish_reason,
                )
        return CompletionResult(
            text=settled.response_text,
            provider=intent.provider,
            model=intent.model,
            usage=usage,
            raw_finish_reason=settled.finish_reason,
        )

    def _validate_settled_usage(self, usage: ProviderUsage) -> None:
        if (
            usage.cost_usd is None
            or usage.accounting_status is AccountingStatus.UNKNOWN
            or not usage.provider_request_id
        ):
            raise ProviderReceiptUnattested(
                "durable completion requires attested usage and cost"
            )
        if self.require_provider_reported_cost and usage.accounting_status not in {
            AccountingStatus.PROVIDER_REPORTED,
            AccountingStatus.GENERATION_RECONCILED,
        }:
            raise ProviderReceiptUnattested(
                "formal durable completion requires provider-reported cost"
            )

    @staticmethod
    def _cost_entry(
        intent: ProviderCallIntent, result: ProviderCallResult
    ) -> CostLedgerEntry:
        assert result.cost_usd is not None
        return CostLedgerEntry(
            entry_id=stable_id("provider-cost", intent.call_id),
            call_id=intent.call_id,
            run_id=intent.run_id,
            batch_id=intent.batch_id,
            action_id=intent.action_id,
            purpose=intent.purpose,
            cost_usd=result.cost_usd,
            accounting_status=result.accounting_status,
            created_at=result.completed_at,
        )


__all__ = [
    "DurableCompletion",
    "ProviderCallAttemptAlreadyFailed",
    "ProviderCallContext",
]
