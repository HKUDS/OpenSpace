"""Provider-neutral reconciliation against authoritative generation records."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from agentpilot.providers.base import (
    AccountingStatus,
    ProviderAccountingError,
    ProviderAccountingPendingError,
    ProviderLookupTransportError,
    ProviderUsage,
    lookup_failure_stays_pending,
    names_same_model,
)


@dataclass(frozen=True)
class GenerationAccountingRecord:
    """The provider's authoritative accounting facts for one generation."""

    generation_id: str
    model: str
    input_tokens: int
    output_tokens: int
    total_cost: Decimal

    def __post_init__(self) -> None:
        for name in ("generation_id", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"generation accounting {name} must be non-empty")
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    "generation accounting token counts must be non-negative integers"
                )
        if not isinstance(self.total_cost, Decimal) or (
            not self.total_cost.is_finite() or self.total_cost < 0
        ):
            raise ValueError(
                "generation accounting cost must be a finite non-negative Decimal"
            )


class GenerationAccountingLookup(Protocol):
    """Provider-scoped lookup for an already known generation identifier."""

    def lookup(self, generation_id: str) -> GenerationAccountingRecord: ...


class GenerationAccountingReconciler:
    """Strictly compare inline usage with an authoritative provider record."""

    # OpenRouter's inline response may carry more decimal places than its
    # generation endpoint. Accept only sub-nanodollar representation drift;
    # the authoritative generation total remains the booked value.
    _COST_ABSOLUTE_TOLERANCE = Decimal("0.000000001")

    # The budget is configuration; the shape of the wait is not. Start short so
    # a row that is already indexed costs almost nothing, and cap the interval
    # so a long budget does not turn into one long blind sleep.
    _INITIAL_RETRY_SECONDS = 1.0
    _MAX_RETRY_SECONDS = 15.0

    def __init__(
        self,
        lookup: GenerationAccountingLookup,
        *,
        pending_grace_seconds: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(pending_grace_seconds, bool) or not isinstance(
            pending_grace_seconds, (int, float)
        ):
            raise ValueError("pending_grace_seconds must be a number")
        if not math.isfinite(pending_grace_seconds) or pending_grace_seconds < 0:
            raise ValueError("pending_grace_seconds must be finite and non-negative")
        self.lookup = lookup
        self.pending_grace_seconds = float(pending_grace_seconds)
        self._sleep = sleep
        self._monotonic = monotonic

    def _lookup_within_grace(self, generation_id: str) -> GenerationAccountingRecord:
        """Wait out a late provider row, never a wrong one.

        A pending lookup means the record is not indexed yet, so the same query
        can still succeed. Every other accounting failure answers identically
        forever and must stay immediate. Waiting here re-reads an existing
        receipt and never re-issues a call, so the guarantee that polling cannot
        produce a second model call still holds -- and it replaces a process
        restart that costs far more than the wait itself.
        """

        deadline = self._monotonic() + self.pending_grace_seconds
        delay = self._INITIAL_RETRY_SECONDS
        while True:
            try:
                return self.lookup.lookup(generation_id)
            except ProviderAccountingPendingError:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise
                self._sleep(min(delay, remaining))
                delay = min(delay * 2, self._MAX_RETRY_SECONDS)

    def reconcile(self, *, model: str, usage: ProviderUsage) -> ProviderUsage:
        generation_id = usage.provider_request_id
        if (
            not isinstance(generation_id, str)
            or not generation_id
            or generation_id != generation_id.strip()
        ):
            raise ProviderAccountingError(
                "generation reconciliation requires a returned generation id"
            )
        record = self._lookup_within_grace(generation_id)
        if record.generation_id != generation_id:
            raise ProviderAccountingError("generation accounting id mismatch")
        if not names_same_model(model, record.model):
            raise ProviderAccountingError("generation accounting model mismatch")
        if record.input_tokens != usage.input_tokens:
            raise ProviderAccountingError("generation accounting input token mismatch")
        if record.output_tokens != usage.output_tokens:
            raise ProviderAccountingError("generation accounting output token mismatch")
        if usage.cost_usd is not None:
            inline_cost = Decimal(str(usage.cost_usd))
            if abs(record.total_cost - inline_cost) > self._COST_ABSOLUTE_TOLERANCE:
                raise ProviderAccountingError(
                    "generation accounting total cost mismatch"
                )

        cost_usd = float(record.total_cost)
        if not math.isfinite(cost_usd) or Decimal(str(cost_usd)) != record.total_cost:
            raise ProviderAccountingError(
                "generation accounting cost cannot be represented safely"
            )

        return ProviderUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=cost_usd,
            accounting_status=AccountingStatus.GENERATION_RECONCILED,
            provider_request_id=generation_id,
        )


class JsonGetTransport(Protocol):
    """Minimal injectable transport used by provider generation lookups."""

    def get_json(
        self,
        *,
        url: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Any: ...


class _RejectRedirects(HTTPRedirectHandler):
    """Keep authorization headers pinned to the configured provider origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibJsonGetTransport:
    """Small standard-library JSON GET transport for production assembly."""

    _MAX_RESPONSE_BYTES = 1_048_576

    def __init__(self) -> None:
        self._opener = build_opener(_RejectRedirects())

    def get_json(
        self,
        *,
        url: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Any:
        request = Request(
            f"{url}?{urlencode(query)}",
            headers=dict(headers),
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                body = response.read(self._MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise ProviderLookupTransportError(
                "generation lookup was answered with an error status",
                status=exc.code,
            ) from None
        except Exception:
            raise ProviderLookupTransportError(
                "generation lookup did not reach the provider"
            ) from None
        if len(body) > self._MAX_RESPONSE_BYTES:
            raise ValueError("generation lookup response is too large")
        return json.loads(body.decode("utf-8"), parse_float=Decimal)


class OpenRouterGenerationLookup:
    """OpenRouter ``GET /api/v1/generation`` accounting lookup."""

    endpoint = "https://openrouter.ai/api/v1/generation"

    def __init__(
        self,
        *,
        api_key: str,
        transport: JsonGetTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("OpenRouter API key must be non-empty")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("generation lookup timeout must be finite and positive")
        self._api_key = api_key.strip()
        self._transport = transport or UrllibJsonGetTransport()
        self._timeout_seconds = float(timeout_seconds)

    def lookup(self, generation_id: str) -> GenerationAccountingRecord:
        if (
            not isinstance(generation_id, str)
            or not generation_id
            or generation_id != generation_id.strip()
        ):
            raise ProviderAccountingError(
                "OpenRouter generation lookup requires a non-empty id"
            )
        try:
            payload = self._transport.get_json(
                url=self.endpoint,
                query={"id": generation_id},
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout_seconds=self._timeout_seconds,
            )
        except ProviderLookupTransportError as exc:
            # Do not relay transport messages: they can contain authorization
            # headers, request bodies, or other provider-sensitive material.
            if not lookup_failure_stays_pending(exc.status):
                raise ProviderAccountingError(
                    "OpenRouter rejected the generation accounting lookup"
                ) from None
            raise ProviderAccountingPendingError(
                "OpenRouter generation accounting is not available yet"
            ) from None
        except Exception:
            # An untyped transport failure carries no status, so it is treated
            # like a request that never got an answer.
            raise ProviderAccountingPendingError(
                "OpenRouter generation accounting is not available yet"
            ) from None

        try:
            if not isinstance(payload, Mapping):
                raise ValueError
            data = payload["data"]
            if not isinstance(data, Mapping):
                raise ValueError
            return GenerationAccountingRecord(
                generation_id=self._required_text(data, "id"),
                model=self._required_text(data, "model"),
                # Inline OpenRouter usage is reported with the selected model's
                # native tokenizer.  The generation endpoint also exposes
                # normalized ``tokens_*`` fields, which can differ materially
                # from that inline receipt.  Only the native counts reconcile
                # the same accounting fact.
                input_tokens=self._required_tokens(data, "native_tokens_prompt"),
                output_tokens=self._required_tokens(data, "native_tokens_completion"),
                total_cost=self._required_cost(data, "total_cost"),
            )
        except (KeyError, TypeError, ValueError):
            raise ProviderAccountingError(
                "OpenRouter generation accounting response is malformed"
            ) from None

    @staticmethod
    def _required_text(data: Mapping[str, Any], name: str) -> str:
        value = data[name]
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError
        return value

    @staticmethod
    def _required_tokens(data: Mapping[str, Any], name: str) -> int:
        value = data[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError
        return value

    @staticmethod
    def _required_cost(data: Mapping[str, Any], name: str) -> Decimal:
        value = data[name]
        if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
            raise ValueError
        result = value if isinstance(value, Decimal) else Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError
        return result


__all__ = [
    "GenerationAccountingLookup",
    "GenerationAccountingRecord",
    "GenerationAccountingReconciler",
    "JsonGetTransport",
    "OpenRouterGenerationLookup",
    "UrllibJsonGetTransport",
]
