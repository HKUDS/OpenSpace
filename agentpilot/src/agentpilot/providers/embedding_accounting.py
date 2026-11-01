"""Independent embedding accounting against authoritative analytics rows."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.error import HTTPError
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
class EmbeddingAccountingRecord:
    """Authoritative accounting facts for one embedding response."""

    embedding_id: str
    model: str
    input_tokens: int
    total_tokens: int
    request_count: int
    total_cost: Decimal

    def __post_init__(self) -> None:
        for name in ("embedding_id", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"embedding accounting {name} must be non-empty")
        for name in ("input_tokens", "total_tokens", "request_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    "embedding accounting counts must be non-negative integers"
                )
        if not isinstance(self.total_cost, Decimal) or (
            not self.total_cost.is_finite() or self.total_cost < 0
        ):
            raise ValueError(
                "embedding accounting cost must be a finite non-negative Decimal"
            )


class EmbeddingAccountingLookup(Protocol):
    """Provider-scoped lookup for an already returned embedding identifier."""

    def lookup(self, embedding_id: str) -> EmbeddingAccountingRecord: ...


# Analytics rounds usage to millionths of a dollar, while the inline receipt
# carries the unrounded amount. Requiring equality would reject every embedding
# whose true cost is not already a multiple of that resolution, which for a
# few-hundred-token request is almost all of them.
ANALYTICS_COST_RESOLUTION = Decimal("0.000001")


def analytics_cost_agrees(inline_cost: Decimal, reported_cost: Decimal) -> bool:
    """Report whether two costs agree within the analytics row's resolution."""

    if not isinstance(inline_cost, Decimal) or not isinstance(
        reported_cost, Decimal
    ):
        raise TypeError("analytics cost comparison requires Decimal values")
    if not inline_cost.is_finite() or not reported_cost.is_finite():
        raise ValueError("analytics cost comparison requires finite values")
    return abs(inline_cost - reported_cost) <= ANALYTICS_COST_RESOLUTION


class EmbeddingAccountingReconciler:
    """Bind inline embedding tokens to a separate authoritative cost row."""

    def __init__(self, lookup: EmbeddingAccountingLookup):
        if not callable(getattr(lookup, "lookup", None)):
            raise TypeError("embedding accounting lookup must provide lookup()")
        self.lookup = lookup

    def reconcile(self, *, model: str, usage: ProviderUsage) -> ProviderUsage:
        embedding_id = usage.provider_request_id
        if (
            not isinstance(embedding_id, str)
            or not embedding_id
            or embedding_id != embedding_id.strip()
        ):
            raise ProviderAccountingError(
                "embedding reconciliation requires a returned embedding id"
            )
        if usage.output_tokens != 0:
            raise ProviderAccountingError(
                "embedding reconciliation requires zero output tokens"
            )

        record = self.lookup.lookup(embedding_id)
        if not isinstance(record, EmbeddingAccountingRecord):
            raise ProviderAccountingError(
                "embedding accounting lookup returned a malformed record"
            )
        if record.embedding_id != embedding_id:
            raise ProviderAccountingError("embedding accounting id mismatch")
        if not names_same_model(model, record.model):
            raise ProviderAccountingError("embedding accounting model mismatch")
        if record.input_tokens != usage.input_tokens:
            raise ProviderAccountingError("embedding accounting input token mismatch")
        if record.total_tokens != usage.input_tokens:
            raise ProviderAccountingError("embedding accounting total token mismatch")
        if record.request_count != 1:
            raise ProviderAccountingError(
                "embedding accounting must identify exactly one request"
            )
        inline_cost = (
            None if usage.cost_usd is None else Decimal(str(usage.cost_usd))
        )
        if inline_cost is not None and not analytics_cost_agrees(
            inline_cost, record.total_cost
        ):
            raise ProviderAccountingError("embedding accounting total cost mismatch")
        # The analytics row attests that the charge exists and how it was
        # measured; the inline receipt states it without rounding, so the ledger
        # keeps the unrounded amount once the row has confirmed it.
        settled_cost = record.total_cost if inline_cost is None else inline_cost
        cost_usd = float(settled_cost)
        if not math.isfinite(cost_usd) or Decimal(str(cost_usd)) != settled_cost:
            raise ProviderAccountingError(
                "embedding accounting cost cannot be represented safely"
            )
        return ProviderUsage(
            input_tokens=usage.input_tokens,
            output_tokens=0,
            cost_usd=cost_usd,
            accounting_status=AccountingStatus.ANALYTICS_RECONCILED,
            provider_request_id=embedding_id,
        )


class JsonAnalyticsTransport(Protocol):
    """Minimal injectable transport for management-key analytics calls."""

    def get_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Any: ...

    def post_json(
        self,
        *,
        url: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Any: ...


class _RejectRedirects(HTTPRedirectHandler):
    """Never forward a management credential to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibJsonAnalyticsTransport:
    """Bounded JSON transport dedicated to the Analytics API."""

    _MAX_RESPONSE_BYTES = 1_048_576

    def __init__(self) -> None:
        self._opener = build_opener(_RejectRedirects())

    def get_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Any:
        request = Request(url, headers=dict(headers), method="GET")
        return self._read_json(request, timeout_seconds=timeout_seconds)

    def post_json(
        self,
        *,
        url: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Any:
        body = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        request = Request(url, data=body, headers=dict(headers), method="POST")
        return self._read_json(request, timeout_seconds=timeout_seconds)

    def _read_json(self, request: Request, *, timeout_seconds: float) -> Any:
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                body = response.read(self._MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise ProviderLookupTransportError(
                "analytics lookup was answered with an error status",
                status=exc.code,
            ) from None
        except Exception:
            raise ProviderLookupTransportError(
                "analytics lookup did not reach the provider"
            ) from None
        if len(body) > self._MAX_RESPONSE_BYTES:
            raise ValueError("analytics response is too large")
        return json.loads(body.decode("utf-8"), parse_float=Decimal)


class OpenRouterEmbeddingAnalyticsLookup:
    """Strict per-embedding lookup through OpenRouter beta Analytics."""

    meta_endpoint = "https://openrouter.ai/api/v1/analytics/meta"
    query_endpoint = "https://openrouter.ai/api/v1/analytics/query"
    _REQUIRED_DIMENSIONS = frozenset({"generation_id", "model"})
    _REQUIRED_METRICS = frozenset(
        {"request_count", "tokens_prompt", "tokens_total", "total_usage"}
    )

    def __init__(
        self,
        *,
        management_key: str,
        model: str,
        transport: JsonAnalyticsTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not isinstance(management_key, str) or not management_key.strip():
            raise ValueError("OpenRouter management key must be non-empty")
        if not isinstance(model, str) or not model or model != model.strip():
            raise ValueError("OpenRouter embedding model must be non-empty")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("analytics lookup timeout must be finite and positive")
        self._management_key = management_key.strip()
        self._model = model
        self._transport = transport or UrllibJsonAnalyticsTransport()
        self._timeout_seconds = float(timeout_seconds)

    def lookup(self, embedding_id: str) -> EmbeddingAccountingRecord:
        if (
            not isinstance(embedding_id, str)
            or not embedding_id.startswith("gen-")
            or embedding_id != embedding_id.strip()
        ):
            raise ProviderAccountingError(
                "OpenRouter analytics lookup requires a gen-* id"
            )
        headers = {"Authorization": f"Bearer {self._management_key}"}
        self._validate_meta(headers)
        payload = {
            "metrics": sorted(self._REQUIRED_METRICS),
            "dimensions": ["generation_id", "model"],
            "filters": [
                {
                    "field": "generation_id",
                    "operator": "eq",
                    "value": embedding_id,
                }
            ],
            "limit": 2,
        }
        try:
            response = self._transport.post_json(
                url=self.query_endpoint,
                payload=payload,
                headers={**headers, "Content-Type": "application/json"},
                timeout_seconds=self._timeout_seconds,
            )
        except ProviderLookupTransportError as exc:
            if not lookup_failure_stays_pending(exc.status):
                raise ProviderAccountingError(
                    "OpenRouter rejected the embedding analytics query"
                ) from None
            raise ProviderAccountingPendingError(
                "OpenRouter embedding analytics query failed"
            ) from None
        except Exception:
            raise ProviderAccountingPendingError(
                "OpenRouter embedding analytics query failed"
            ) from None
        return self._parse_query(response, embedding_id=embedding_id)

    def _validate_meta(self, headers: Mapping[str, str]) -> None:
        try:
            payload = self._transport.get_json(
                url=self.meta_endpoint,
                headers=headers,
                timeout_seconds=self._timeout_seconds,
            )
        except ProviderLookupTransportError as exc:
            if not lookup_failure_stays_pending(exc.status):
                raise ProviderAccountingError(
                    "OpenRouter rejected the analytics metadata lookup"
                ) from None
            raise ProviderAccountingPendingError(
                "OpenRouter analytics metadata lookup failed"
            ) from None
        except Exception:
            raise ProviderAccountingPendingError(
                "OpenRouter analytics metadata lookup failed"
            ) from None
        try:
            data = self._data_mapping(payload)
            dimensions = self._named_entries(data, "dimensions")
            metrics = self._named_entries(data, "metrics")
            operators = self._named_entries(data, "operators")
            if not self._REQUIRED_DIMENSIONS.issubset(dimensions):
                raise ValueError
            if not self._REQUIRED_METRICS.issubset(metrics):
                raise ValueError
            if "eq" not in operators:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ProviderAccountingError(
                "OpenRouter analytics metadata lacks the required schema"
            ) from None

    def _parse_query(
        self, payload: Any, *, embedding_id: str
    ) -> EmbeddingAccountingRecord:
        try:
            outer = self._data_mapping(payload)
            rows = outer["data"]
            metadata = outer["metadata"]
            if not isinstance(rows, list) or not isinstance(metadata, Mapping):
                raise ValueError
            if metadata.get("truncated") is not False:
                raise ValueError
            row_count = metadata.get("row_count")
            if (
                isinstance(row_count, int)
                and not isinstance(row_count, bool)
                and row_count == 0
                and not rows
            ):
                raise ProviderAccountingPendingError(
                    "OpenRouter embedding analytics row is not available yet"
                )
            if (
                isinstance(row_count, bool)
                or not isinstance(row_count, int)
                or row_count != 1
                or len(rows) != 1
            ):
                raise ValueError
            row = rows[0]
            if not isinstance(row, Mapping) or set(row) != {
                "generation_id",
                "model",
                "request_count",
                "tokens_prompt",
                "tokens_total",
                "total_usage",
            }:
                raise ValueError
            returned_id = self._required_text(row, "generation_id")
            if returned_id != embedding_id:
                raise ValueError
            request_count = self._required_count(row, "request_count")
            if request_count != 1:
                raise ValueError
            returned_model = self._required_text(row, "model")
            if not names_same_model(self._model, returned_model):
                raise ValueError
            input_tokens = self._required_count(row, "tokens_prompt")
            raw_total_tokens = row["tokens_total"]
            total_tokens = (
                input_tokens
                if raw_total_tokens is None
                else self._required_count(row, "tokens_total")
            )
            return EmbeddingAccountingRecord(
                embedding_id=returned_id,
                model=self._model,
                input_tokens=input_tokens,
                total_tokens=total_tokens,
                request_count=request_count,
                total_cost=self._required_cost(row, "total_usage"),
            )
        except (KeyError, TypeError, ValueError):
            raise ProviderAccountingError(
                "OpenRouter embedding analytics response is malformed"
            ) from None

    @staticmethod
    def _data_mapping(payload: Any) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError
        data = payload["data"]
        if not isinstance(data, Mapping):
            raise ValueError
        return data

    @staticmethod
    def _named_entries(data: Mapping[str, Any], name: str) -> frozenset[str]:
        values = data[name]
        if not isinstance(values, list):
            raise ValueError
        result: set[str] = set()
        for value in values:
            if not isinstance(value, Mapping):
                raise ValueError
            item = value.get("name")
            if not isinstance(item, str) or not item or item != item.strip():
                raise ValueError
            result.add(item)
        return frozenset(result)

    @staticmethod
    def _required_text(data: Mapping[str, Any], name: str) -> str:
        value = data[name]
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError
        return value

    @staticmethod
    def _required_count(data: Mapping[str, Any], name: str) -> int:
        value = data[name]
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, int):
            result = value
        elif isinstance(value, str) and value.isascii() and value.isdigit():
            result = int(value)
        else:
            raise ValueError
        if result < 0:
            raise ValueError
        return result

    @staticmethod
    def _required_cost(data: Mapping[str, Any], name: str) -> Decimal:
        value = data[name]
        if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
            raise ValueError
        try:
            result = value if isinstance(value, Decimal) else Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError from exc
        if not result.is_finite() or result < 0:
            raise ValueError
        return result


__all__ = [
    "ANALYTICS_COST_RESOLUTION",
    "EmbeddingAccountingLookup",
    "EmbeddingAccountingRecord",
    "EmbeddingAccountingReconciler",
    "JsonAnalyticsTransport",
    "OpenRouterEmbeddingAnalyticsLookup",
    "UrllibJsonAnalyticsTransport",
    "analytics_cost_agrees",
]
