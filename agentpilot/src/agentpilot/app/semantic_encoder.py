"""Assembly adapter from paid embedding providers to the frozen search index."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import replace

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
    EmbeddingAccountingReconciler,
    EmbeddingProvider,
    EmbeddingRequest,
    EmbeddingResult,
    ProviderAccountingError,
    ProviderAccountingPendingError,
    ProviderCallRefused,
    ProviderEmbeddingUnusable,
    ProviderUsage,
)
from agentpilot.search import EmbeddingIdentity


class ProviderDirectionEmbeddingBackend:
    """Bind model identity and dimension once and expose the search protocol."""

    def __init__(
        self,
        *,
        provider: EmbeddingProvider,
        model: str,
        encoder_identity: str,
        dimension: int,
        unit_of_work: UnitOfWorkFactory,
        run_id: str,
        batch_id: str,
        require_provider_reported_cost: bool = True,
        accounting_reconciler: EmbeddingAccountingReconciler | None = None,
    ) -> None:
        if not model.strip() or not encoder_identity.strip():
            raise ValueError("embedding model identity must be non-empty")
        if isinstance(dimension, bool) or dimension < 1:
            raise ValueError("embedding dimension must be positive")
        if not run_id.strip() or not batch_id.strip():
            raise ValueError("embedding run/batch identity must be non-empty")
        if accounting_reconciler is not None and not isinstance(
            accounting_reconciler, EmbeddingAccountingReconciler
        ):
            raise TypeError(
                "embedding backend requires an EmbeddingAccountingReconciler"
            )
        self.provider = provider
        self.model = model
        self.unit_of_work = unit_of_work
        self.run_id = run_id
        self.batch_id = batch_id
        self.require_provider_reported_cost = require_provider_reported_cost
        self.accounting_reconciler = accounting_reconciler
        self._identity = EmbeddingIdentity(
            encoder_id=f"{provider.provider_name}:{model}",
            encoder_identity=encoder_identity,
            dimension=dimension,
        )

    def identity(self) -> EmbeddingIdentity:
        return self._identity

    def embed(self, inputs: Sequence[str]) -> Sequence[Sequence[float]]:
        request = EmbeddingRequest(
            model=self.model,
            texts=tuple(inputs),
        )
        call_id = stable_id(
            "direction-embedding-call",
            self.run_id,
            self.batch_id,
            self._identity,
            request.texts,
        )
        intent = ProviderCallIntent(
            call_id=call_id,
            run_id=self.run_id,
            batch_id=self.batch_id,
            provider=self.provider.provider_name,
            model=self.model,
            purpose="direction-semantic-embedding",
            created_at=utc_now(),
            request_facts={
                "texts": request.texts,
                "encoder_identity": self._identity.encoder_identity,
                "dimension": self._identity.dimension,
            },
        )
        with self.unit_of_work() as work:
            try:
                existing_intent = work.usage.get_intent(call_id)
            except KeyError:
                work.usage.add_intent(intent)
            else:
                if (
                    existing_intent.run_id != intent.run_id
                    or existing_intent.batch_id != intent.batch_id
                    or existing_intent.provider != intent.provider
                    or existing_intent.model != intent.model
                    or existing_intent.purpose != intent.purpose
                    or existing_intent.action_id != intent.action_id
                    or existing_intent.request_facts != intent.request_facts
                ):
                    raise ValueError("embedding call intent identity drift")
            existing = work.usage.get_result(call_id)
            dispatched = work.usage.get_dispatch(call_id) is not None
        if existing is not None:
            if existing.status is ProviderCallStatus.ACCOUNTING_PENDING:
                return self._settle_receipt(existing, expected_rows=len(request.texts))
            if existing.status is not ProviderCallStatus.COMPLETED:
                raise RuntimeError(
                    "embedding call id already records a terminal attempt: "
                    f"{existing.status.value}"
                )
            return self._parse_vectors(
                existing.response_text or "", expected_rows=len(request.texts)
            )
        if dispatched:
            raise ProviderAccountingError(
                "embedding call was dispatched without a durable receipt; "
                "its cost cannot be established"
            )
        with self.unit_of_work() as work:
            work.usage.mark_dispatched(
                ProviderCallDispatch(call_id=call_id, dispatched_at=utc_now())
            )

        try:
            result = self.provider.embed(request)
        except ProviderCallRefused as exc:
            # An embedding call id is derived from its own inputs, so there is
            # no fresh id to move to. A refusal retracts the dispatch and leaves
            # this id reusable, which is the only way a resume can ever get past
            # a provider that rejected one request.
            with self.unit_of_work() as work:
                work.usage.record_refusal(
                    ProviderCallRefusal(
                        call_id=call_id,
                        ordinal=len(work.usage.list_refusals(call_id)),
                        refused_at=utc_now(),
                        detail=str(exc),
                    )
                )
            raise
        except ProviderEmbeddingUnusable as exc:
            self._record_pre_receipt_failure(call_id, None, exc, usage=exc.usage)
            raise

        try:
            if (
                result.provider != self.provider.provider_name
                or result.model != self.model
            ):
                raise ValueError("embedding provider returned mismatched identity")
            vectors = self._validate_vectors(
                result.vectors, expected_rows=len(request.texts)
            )
            request_id = result.usage.provider_request_id
            if (
                not isinstance(request_id, str)
                or not request_id
                or request_id != request_id.strip()
            ):
                raise ProviderAccountingError(
                    "direction embeddings require a provider response id"
                )
            if (
                self.provider.provider_name == "openrouter"
                and self.accounting_reconciler is not None
                and not request_id.startswith("gen-")
            ):
                raise ProviderAccountingError(
                    "OpenRouter embedding reconciliation requires a gen-* id"
                )
            if result.usage.output_tokens != 0:
                raise ProviderAccountingError(
                    "direction embedding usage must have zero output tokens"
                )
        except Exception as exc:
            self._record_pre_receipt_failure(call_id, result, exc)
            raise

        receipt = ProviderCallResult(
            call_id=call_id,
            status=ProviderCallStatus.ACCOUNTING_PENDING,
            completed_at=utc_now(),
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cost_usd=result.usage.cost_usd,
            accounting_status=result.usage.accounting_status.value,
            provider_request_id=result.usage.provider_request_id,
            response_text=canonical_json({"vectors": vectors}),
        )
        with self.unit_of_work() as work:
            work.usage.add_result(receipt)
        return self._settle_receipt(receipt, expected_rows=len(request.texts))

    def _settle_receipt(
        self,
        receipt: ProviderCallResult,
        *,
        expected_rows: int,
    ) -> tuple[tuple[float, ...], ...]:
        try:
            vectors = self._parse_vectors(
                receipt.response_text or "", expected_rows=expected_rows
            )
            usage = ProviderUsage(
                input_tokens=receipt.input_tokens or 0,
                output_tokens=receipt.output_tokens or 0,
                cost_usd=receipt.cost_usd,
                accounting_status=AccountingStatus(receipt.accounting_status),
                provider_request_id=receipt.provider_request_id,
            )
            if self.accounting_reconciler is not None:
                try:
                    usage = self.accounting_reconciler.reconcile(
                        model=self.model,
                        usage=usage,
                    )
                except ProviderAccountingPendingError:
                    if (
                        usage.cost_usd is None
                        or usage.accounting_status is AccountingStatus.UNKNOWN
                    ):
                        # Only the row can price this receipt, so waiting for
                        # the row is the only way it can ever settle.
                        raise
                    # The row trails the response by minutes and the search
                    # cannot choose a direction until this embedding settles.
                    # The inline receipt already states the charge and carries
                    # the id that proves it, so the ledger takes that now and
                    # `reconcile-embeddings` confirms it against the
                    # authoritative row once the row exists.
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
                    work.usage.add_cost(
                        CostLedgerEntry(
                            entry_id=stable_id("provider-cost", receipt.call_id),
                            call_id=receipt.call_id,
                            run_id=self.run_id,
                            batch_id=self.batch_id,
                            purpose="direction-semantic-embedding",
                            cost_usd=failed.cost_usd,
                            accounting_status=failed.accounting_status,
                            created_at=utc_now(),
                        )
                    )
            raise

        completed = replace(
            receipt,
            status=ProviderCallStatus.COMPLETED,
            cost_usd=usage.cost_usd,
            accounting_status=usage.accounting_status.value,
        )
        assert usage.cost_usd is not None
        entry = CostLedgerEntry(
            entry_id=stable_id("provider-cost", receipt.call_id),
            call_id=receipt.call_id,
            run_id=self.run_id,
            batch_id=self.batch_id,
            purpose="direction-semantic-embedding",
            cost_usd=usage.cost_usd,
            accounting_status=usage.accounting_status.value,
            created_at=utc_now(),
        )
        with self.unit_of_work() as work:
            work.usage.transition_result(completed)
            work.usage.add_cost(entry)
        return vectors

    def _validate_settled_usage(self, usage: ProviderUsage) -> None:
        if (
            usage.cost_usd is None
            or usage.accounting_status is AccountingStatus.UNKNOWN
            or not usage.provider_request_id
        ):
            raise ProviderAccountingError(
                "direction embeddings require attested usage and cost"
            )
        if self.require_provider_reported_cost and usage.accounting_status not in {
            AccountingStatus.PROVIDER_REPORTED,
            AccountingStatus.ANALYTICS_RECONCILED,
        }:
            raise ProviderAccountingError(
                "formal direction embeddings require provider-reported cost"
            )

    def _record_pre_receipt_failure(
        self,
        call_id: str,
        result: EmbeddingResult | None,
        exc: Exception,
        *,
        usage: ProviderUsage | None = None,
    ) -> None:
        if usage is None:
            usage = result.usage if result is not None else None
        known_cost = (
            usage is not None
            and usage.cost_usd is not None
            and usage.accounting_status is not AccountingStatus.UNKNOWN
            and bool(usage.provider_request_id)
        )
        failed = ProviderCallResult(
            call_id=call_id,
            status=(
                ProviderCallStatus.FAILED
                if known_cost
                else ProviderCallStatus.UNATTESTED
            ),
            completed_at=utc_now(),
            input_tokens=usage.input_tokens if known_cost else None,
            output_tokens=usage.output_tokens if known_cost else None,
            cost_usd=usage.cost_usd if known_cost else None,
            accounting_status=(
                usage.accounting_status.value
                if known_cost
                else AccountingStatus.UNKNOWN.value
            ),
            provider_request_id=(usage.provider_request_id if known_cost else None),
            error_kind=type(exc).__name__,
            error_detail=bounded_failure_detail(exc),
        )
        with self.unit_of_work() as work:
            work.usage.add_result(failed)
            if known_cost:
                assert usage is not None and usage.cost_usd is not None
                work.usage.add_cost(
                    CostLedgerEntry(
                        entry_id=stable_id("provider-cost", call_id),
                        call_id=call_id,
                        run_id=self.run_id,
                        batch_id=self.batch_id,
                        purpose="direction-semantic-embedding",
                        cost_usd=usage.cost_usd,
                        accounting_status=usage.accounting_status.value,
                        created_at=failed.completed_at,
                    )
                )

    def _validate_vectors(
        self, vectors: Sequence[Sequence[float]], *, expected_rows: int
    ) -> tuple[tuple[float, ...], ...]:
        if len(vectors) != expected_rows:
            raise ValueError("embedding provider returned the wrong row count")
        normalized = tuple(tuple(float(value) for value in row) for row in vectors)
        if any(len(row) != self._identity.dimension for row in normalized):
            raise ValueError("embedding provider returned the wrong dimension")
        if any(not math.isfinite(value) for row in normalized for value in row):
            raise ValueError("embedding provider returned a non-finite vector")
        if any(math.fsum(value * value for value in row) <= 0.0 for row in normalized):
            raise ValueError("embedding provider returned a zero vector")
        return normalized

    def _parse_vectors(
        self, response_text: str, *, expected_rows: int
    ) -> tuple[tuple[float, ...], ...]:
        try:
            payload = json.loads(response_text)
            if not isinstance(payload, dict) or set(payload) != {"vectors"}:
                raise ValueError
            vectors = payload["vectors"]
            if not isinstance(vectors, list):
                raise ValueError
            return self._validate_vectors(vectors, expected_rows=expected_rows)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("persisted embedding result is malformed") from exc


__all__ = ["ProviderDirectionEmbeddingBackend"]
