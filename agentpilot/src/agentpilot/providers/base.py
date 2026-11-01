"""Provider-neutral completion and embedding contracts."""

# Persistent format and evaluation identifiers retain their historical values.

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from agentpilot.domain.common import canonical_json, deep_freeze


class AccountingStatus(str, Enum):
    # A CLI/subscription owns billing. No per-call dollar price is asserted.
    EXTERNAL = "external"
    PROVIDER_REPORTED = "provider_reported"
    GENERATION_RECONCILED = "generation_reconciled"
    ANALYTICS_RECONCILED = "analytics_reconciled"
    FROZEN_PRICE = "frozen_price"
    UNKNOWN = "unknown"


class ProviderError(RuntimeError):
    pass


class ProviderAccountingError(ProviderError):
    pass


class ProviderAccountingPendingError(ProviderAccountingError):
    """Authoritative accounting is not visible yet; retry lookup, not inference."""


class ProviderReceiptUnattested(ProviderAccountingError):
    """One dispatch produced billable evidence but no reconcilable identity.

    Dispatch may already have incurred cost, so this is terminal accounting
    quarantine rather than permission to issue the completion again.
    """


class ProviderAttemptSpent(ProviderError):
    """This call id will never yield content, and a fresh one is safe to use.

    Every subclass leaves the previous attempt fully accounted for, so a caller
    may reissue the same work under a new call id with no risk of paying twice.
    """


class ProviderCallRefused(ProviderAttemptSpent):
    """The provider rejected this dispatch without generating or billing.

    Adapters raise this instead of returning a receipt because a refusal has no
    receipt to lose: the provider stated that it generated nothing. The durable
    layer records the refusal and retracts the dispatch, which leaves the call
    id exactly as unspent as it was before the request left the process.
    """


class ProviderCompletionUnusable(ProviderAttemptSpent):
    """The dispatch was settled and billed but produced no usable content."""

    def __init__(self, detail: str, *, usage: ProviderUsage | None = None,
                 settled_text: str | None = None, finish_reason: str | None = None):
        super().__init__(detail)
        # Recovery must count failed generations too. Raw prefixes remain in
        # the durable receipt and are never executable assistant messages.
        self.usage = usage
        # Diagnostic material only, never an executable assistant message.
        self.settled_text = settled_text
        self.finish_reason = finish_reason


class ProviderCompletionTruncated(ProviderCompletionUnusable):
    """The provider stopped at its output limit before completing content."""


class ProviderLookupTransportError(ProviderError):
    """An accounting lookup never reached a parseable answer.

    Transports raise this with the HTTP status when the provider answered, and
    without one when the request never produced a response at all. Messages must
    stay free of request material: lookups carry authorization headers.
    """

    def __init__(self, detail: str, *, status: int | None = None):
        super().__init__(detail)
        self.status = status


_PENDING_LOOKUP_STATUSES = frozenset({404, 408, 409, 425, 429})


def lookup_failure_stays_pending(status: int | None) -> bool:
    """Decide whether a failed accounting lookup can still become visible.

    A request that never got an answer, a record the provider has not indexed
    yet, and throttling are all worth another lookup. An authorization or
    request-shape rejection is not: it will answer identically forever, so
    treating it as pending turns a configuration fault into endless polling.
    """

    if status is None:
        return True
    if status in _PENDING_LOOKUP_STATUSES:
        return True
    return 500 <= status <= 599


_SNAPSHOT_SUFFIX = re.compile(r"\d{4}(?:-?\d{2}){0,2}")


def names_same_model(requested: str, reported: str) -> bool:
    """Decide whether a provider named the model that was asked for.

    A requested name without a date is an alias, and providers answer it with
    the dated snapshot the alias currently points at: ``qwen/qwen3.5-9b``
    reconciles against ``qwen/qwen3.5-9b-20260310``. Demanding string equality
    would make every aliased model impossible to reconcile, while accepting any
    difference would hide the substitution this check exists to catch, so only a
    date appended to the requested name counts as the same model.
    """

    requested_text = requested.strip()
    reported_text = reported.strip()
    if not requested_text or not reported_text:
        return False
    if reported_text.casefold() == requested_text.casefold():
        return True
    prefix = f"{requested_text}-".casefold()
    if not reported_text.casefold().startswith(prefix):
        return False
    return _SNAPSHOT_SUFFIX.fullmatch(reported_text[len(prefix) :]) is not None


class ProviderEmbeddingUnusable(ProviderError):
    """One embedding dispatch returned no usable rows and may still be billed.

    Whatever the provider did report about tokens and cost travels with the
    exception, so the durable layer can still book what it owes rather than
    quarantining a dispatch whose price is actually known.
    """

    def __init__(self, detail: str, *, usage: ProviderUsage | None = None):
        super().__init__(detail)
        self.usage = usage


NATIVE_MESSAGE_CONTRACT = "openthink.native-assistant-message.v1"


@dataclass(frozen=True)
class CompletionRequest:
    model: str
    prompt: str
    disable_reasoning: bool
    system: str = ""
    max_output_tokens: int = 8_192
    temperature: float = 0.3
    response_schema: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    messages: tuple[Mapping[str, Any], ...] | None = None
    tools: tuple[Mapping[str, Any], ...] | None = None
    tool_choice: str | Mapping[str, Any] | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("completion model must be non-empty")
        if not self.prompt and self.messages is None:
            raise ValueError("completion prompt must be non-empty")
        if self.messages is None:
            if self.tools is not None or self.tool_choice is not None:
                raise ValueError(
                    "native tools and tool_choice require explicit messages"
                )
        else:
            if self.prompt or self.system:
                raise ValueError(
                    "native messages replace prompt/system; leave both empty"
                )
            if not isinstance(self.messages, (tuple, list)) or not self.messages:
                raise ValueError("native messages must be a non-empty sequence")
            if any(
                not isinstance(message, Mapping)
                or message.get("role")
                not in {"system", "developer", "user", "assistant", "tool"}
                for message in self.messages
            ):
                raise ValueError("native messages need supported message roles")
            object.__setattr__(self, "messages", deep_freeze(self.messages))
            if self.tools is not None:
                if not isinstance(self.tools, (tuple, list)) or any(
                    not isinstance(tool, Mapping) or not tool for tool in self.tools
                ):
                    raise ValueError("native tools must contain tool mappings")
                object.__setattr__(self, "tools", deep_freeze(self.tools))
            if self.tool_choice is not None:
                if isinstance(self.tool_choice, str):
                    if self.tool_choice not in {"auto", "none", "required"}:
                        raise ValueError("native tool_choice is unsupported")
                elif not isinstance(self.tool_choice, Mapping) or not self.tool_choice:
                    raise ValueError("native tool_choice must be a string or mapping")
                object.__setattr__(self, "tool_choice", deep_freeze(self.tool_choice))
            # Fail locally on non-serializable native request data, before a
            # durable dispatch could reach a provider and become billable.
            canonical_json((self.messages, self.tools, self.tool_choice))
        if isinstance(self.max_output_tokens, bool) or self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if not math.isfinite(self.temperature) or self.temperature < 0.0:
            raise ValueError("temperature must be finite and non-negative")
        if not isinstance(self.disable_reasoning, bool):
            raise ValueError("disable_reasoning must be boolean")
        if self.reasoning_effort is not None:
            if not isinstance(self.reasoning_effort, str) or self.reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh", "max"}:
                raise ValueError("unsupported reasoning_effort")
            if self.disable_reasoning:
                raise ValueError("reasoning_effort conflicts with disable_reasoning")
        if self.response_schema is not None:
            if (
                not isinstance(self.response_schema, Mapping)
                or not self.response_schema
            ):
                raise ValueError("response_schema must be a non-empty mapping")
            object.__setattr__(
                self,
                "response_schema",
                deep_freeze(self.response_schema),
            )
        object.__setattr__(self, "metadata", deep_freeze(self.metadata))


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    accounting_status: AccountingStatus
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("provider token counts must be non-negative integers")
        if self.cost_usd is not None and (
            not math.isfinite(self.cost_usd) or self.cost_usd < 0.0
        ):
            raise ValueError("provider cost must be finite and non-negative")
        if (
            self.cost_usd is None
            and self.accounting_status not in {AccountingStatus.UNKNOWN, AccountingStatus.EXTERNAL}
        ):
            raise ValueError("known accounting status requires a numeric cost")
        if self.accounting_status is AccountingStatus.EXTERNAL and self.cost_usd is not None:
            raise ValueError("external accounting cannot assert a per-call cost")


@dataclass(frozen=True)
class CompletionResult:
    text: str
    provider: str
    model: str
    usage: ProviderUsage
    raw_finish_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("provider returned an empty completion")


@dataclass(frozen=True)
class ProviderReceipt:
    """Everything one dispatch came back with, before attestation is judged.

    An adapter must never raise on accounting grounds: the exception would
    discard the only evidence of what the provider generated and billed. It
    reports what arrived, including nothing, and the durable layer decides
    whether that is settleable, un-billed, or quarantine.
    """

    provider: str
    model: str
    attestation_id: str | None = None
    text: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    accounting_status: AccountingStatus = AccountingStatus.UNKNOWN
    raw_finish_reason: str | None = None
    defect: str | None = None

    def __post_init__(self) -> None:
        for name in ("provider", "model"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"provider receipt {name} must be non-empty")
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(
                    f"provider receipt {name} must be non-negative or None"
                )
        if (self.input_tokens is None) != (self.output_tokens is None):
            raise ValueError("provider receipt token counts must arrive together")
        if self.cost_usd is not None and (
            not math.isfinite(self.cost_usd) or self.cost_usd < 0.0
        ):
            raise ValueError("provider receipt cost must be finite and non-negative")
        if (self.cost_usd is None) != (
            self.accounting_status is AccountingStatus.UNKNOWN
        ):
            raise ValueError("provider receipt cost and accounting status disagree")
        if self.cost_usd is not None and self.input_tokens is None:
            raise ValueError("a costed provider receipt must carry token counts")
        if self.defect is not None and not self.defect.strip():
            raise ValueError("a provider receipt defect must describe what was wrong")
        if self.defect is None and (
            self.text is None
            or self.attestation_id is None
            or self.input_tokens is None
        ):
            raise ValueError("an intact provider receipt needs text, id, and tokens")

    @property
    def settleable(self) -> bool:
        """Whether an authoritative lookup can still price this dispatch."""

        return self.attestation_id is not None and self.input_tokens is not None


class CompletionProvider(Protocol):
    provider_name: str

    def complete(self, request: CompletionRequest) -> ProviderReceipt: ...


@dataclass(frozen=True)
class TokenPrice:
    input_per_token: float
    output_per_token: float

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.input_per_token)
            or not math.isfinite(self.output_per_token)
            or self.input_per_token < 0.0
            or self.output_per_token < 0.0
        ):
            raise ValueError("token prices must be finite and non-negative")


class PriceBook(Protocol):
    def price(self, provider: str, model: str) -> TokenPrice | None: ...


class FrozenPriceBook:
    def __init__(self, prices: Mapping[tuple[str, str], TokenPrice]):
        self._prices = dict(prices)

    def price(self, provider: str, model: str) -> TokenPrice | None:
        return self._prices.get((provider, model))


@dataclass(frozen=True)
class EmbeddingRequest:
    model: str
    texts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("embedding model must be non-empty")
        if not self.texts or any(
            not isinstance(text, str) or not text for text in self.texts
        ):
            raise ValueError("embedding request needs non-empty texts")


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    provider: str
    model: str
    usage: ProviderUsage

    def __post_init__(self) -> None:
        if not self.vectors or any(not vector for vector in self.vectors):
            raise ValueError("embedding provider returned empty vectors")
        dimension = len(self.vectors[0])
        if any(len(vector) != dimension for vector in self.vectors):
            raise ValueError("embedding vectors have inconsistent dimensions")
        if any(not math.isfinite(value) for vector in self.vectors for value in vector):
            raise ValueError("embedding vectors must be finite")


class EmbeddingProvider(Protocol):
    provider_name: str

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult: ...


__all__ = [
    "NATIVE_MESSAGE_CONTRACT",
    "AccountingStatus",
    "CompletionProvider",
    "CompletionRequest",
    "CompletionResult",
    "EmbeddingProvider",
    "EmbeddingRequest",
    "EmbeddingResult",
    "FrozenPriceBook",
    "PriceBook",
    "ProviderAccountingError",
    "ProviderAccountingPendingError",
    "ProviderAttemptSpent",
    "ProviderCallRefused",
    "ProviderCompletionTruncated",
    "ProviderCompletionUnusable",
    "ProviderEmbeddingUnusable",
    "ProviderError",
    "ProviderLookupTransportError",
    "ProviderReceipt",
    "ProviderReceiptUnattested",
    "ProviderUsage",
    "TokenPrice",
    "lookup_failure_stays_pending",
    "names_same_model",
]
