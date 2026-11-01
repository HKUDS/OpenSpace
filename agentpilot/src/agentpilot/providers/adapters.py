"""Minimal direct completion adapters with explicit provider routing."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from agentpilot.domain.common import canonical_json
from agentpilot.providers.routing import ignored_providers
from agentpilot.providers.base import (
    NATIVE_MESSAGE_CONTRACT,
    AccountingStatus,
    CompletionRequest,
    EmbeddingRequest,
    EmbeddingResult,
    PriceBook,
    ProviderAccountingError,
    ProviderCallRefused,
    ProviderEmbeddingUnusable,
    ProviderReceipt,
    ProviderUsage,
    names_same_model,
)


def _field(value: Any, name: str, default=None):
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(name, default)
    result = getattr(value, name, default)
    if result is not default:
        return result
    extra = getattr(value, "model_extra", None)
    return extra.get(name, default) if isinstance(extra, dict) else default


def _optional_token_count(usage: Any, name: str) -> int | None:
    value = _field(usage, name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _native_json_value(value: Any) -> Any:
    """Preserve SDK message fields, including OpenRouter reasoning extensions."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {key: _native_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_native_json_value(item) for item in value]
    if callable(getattr(value, "model_dump", None)):
        return _native_json_value(value.model_dump(mode="json", exclude_unset=True))
    if isinstance(getattr(value, "__dict__", None), dict):
        return _native_json_value(vars(value))
    raise TypeError("native assistant message contains unsupported data")


def _native_message_text(message: Any) -> str | None:
    if message is None:
        return None
    return canonical_json(
        {
            "contract": NATIVE_MESSAGE_CONTRACT,
            "message": _native_json_value(message),
        }
    )


def _attestation_id(response: Any, usage: Any = None) -> str | None:
    value = (
        _field(usage, "generation_id")
        or _field(response, "generation_id")
        or _field(response, "response_id")
        or _field(response, "id")
    )
    return str(value).strip() if value is not None and str(value).strip() else None


def _check_model_identity(
    response: Any,
    expected: str,
    *,
    resolved_alias_ok: bool = False,
) -> None:
    """Check that the provider answered with the model that was requested.

    A router resolves a floating alias to the dated snapshot it currently points
    at, so its echo is only required to name the same model rather than to repeat
    the request byte for byte. A provider that serves exactly what it is asked
    for is held to the stricter rule, because there the difference would be a
    substitution.
    """

    actual = _field(response, "model") or _field(response, "model_version")
    actual_text = str(actual).strip() if actual is not None else ""
    matches = (
        names_same_model(expected, actual_text)
        if resolved_alias_ok
        else actual_text == expected
    )
    if not matches:
        raise ProviderAccountingError(
            f"provider response model identity does not match {expected!r}"
        )


def _identity_defect(
    response: Any,
    expected: str,
    *,
    resolved_alias_ok: bool = False,
) -> str | None:
    try:
        _check_model_identity(response, expected, resolved_alias_ok=resolved_alias_ok)
    except ProviderAccountingError as exc:
        return str(exc)
    return None


def _refusal(response: Any) -> str | None:
    """Describe an in-band provider rejection, which carries no completion.

    OpenAI-shape providers can answer a well-formed request with an error
    object instead of choices. That answer proves the model never ran, so the
    dispatch is un-billed and a fresh attempt is safe.
    """

    error = _field(response, "error")
    if error is None:
        return None
    code = _field(error, "code")
    message = _optional_text(_field(error, "message")) or "no message"
    prefix = (
        f"provider rejected the request (code={code!r})"
        if code
        else ("provider rejected the request")
    )
    return f"{prefix}: {message[:512]}"


def _transport_defect(exc: Exception) -> tuple[str, bool]:
    """Describe a failed transport and whether it proves nothing was billed.

    A status the provider chose to reject with means the model never ran. A
    timeout, a reset, or a server fault says nothing about whether generation
    happened, so those cannot be treated as free.
    """

    status = _field(exc, "status_code")
    if isinstance(status, bool) or not isinstance(status, int):
        status = _field(_field(exc, "response"), "status_code")
    detail = " ".join(str(exc).split())[:512] or type(exc).__name__
    if isinstance(status, int) and not isinstance(status, bool):
        rejected = 400 <= status < 500 and status != 408
        return f"provider transport returned {status}: {detail}", rejected
    return f"provider transport failed ({type(exc).__name__}): {detail}", False


class _CostedAdapter:
    provider_name: str

    def __init__(
        self,
        client: Any,
        *,
        price_book: PriceBook | None = None,
    ):
        self.client = client
        self.price_book = price_book

    def _price(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        reported_cost: float | None,
    ) -> tuple[float | None, AccountingStatus, str | None]:
        """Price one dispatch without deciding whether it is acceptable."""

        if reported_cost is not None:
            if isinstance(reported_cost, bool):
                return (
                    None,
                    AccountingStatus.UNKNOWN,
                    "provider reported a boolean cost",
                )
            try:
                numeric_cost = float(reported_cost)
            except (TypeError, ValueError):
                return (
                    None,
                    AccountingStatus.UNKNOWN,
                    "provider reported a non-numeric cost",
                )
            if not math.isfinite(numeric_cost) or numeric_cost < 0.0:
                return (
                    None,
                    AccountingStatus.UNKNOWN,
                    "provider reported an invalid cost",
                )
            return numeric_cost, AccountingStatus.PROVIDER_REPORTED, None
        price = (
            self.price_book.price(self.provider_name, model)
            if self.price_book is not None
            else None
        )
        if price is not None:
            return (
                input_tokens * price.input_per_token
                + output_tokens * price.output_per_token,
                AccountingStatus.FROZEN_PRICE,
                None,
            )
        return (
            None,
            AccountingStatus.UNKNOWN,
            f"{self.provider_name}/{model} reported no cost and has no frozen price",
        )

    def _usage(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        reported_cost: float | None,
        request_id: str | None,
    ) -> ProviderUsage:
        """Report what the provider billed, including that it said nothing."""

        cost, status, _defect = self._price(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reported_cost=reported_cost,
        )
        return ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            accounting_status=status,
            provider_request_id=request_id,
        )

    def _receipt(
        self,
        *,
        model: str,
        text: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        reported_cost: float | None,
        attestation_id: str | None,
        finish_reason: Any,
        defects: tuple[str | None, ...],
    ) -> ProviderReceipt:
        cost: float | None = None
        status = AccountingStatus.UNKNOWN
        collected = [item for item in defects if item]
        if input_tokens is None or output_tokens is None:
            input_tokens = output_tokens = None
            collected.append("provider usage is missing token counts")
        else:
            cost, status, price_defect = self._price(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                reported_cost=reported_cost,
            )
            if price_defect is not None:
                collected.append(price_defect)
        if text is None:
            collected.append("provider returned no completion content")
        if attestation_id is None:
            collected.append("provider returned no generation id")
        return ProviderReceipt(
            provider=self.provider_name,
            model=model,
            attestation_id=attestation_id,
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            accounting_status=status,
            raw_finish_reason=_optional_text(finish_reason),
            defect="; ".join(collected) if collected else None,
        )


class OpenAICompatibleAdapter(_CostedAdapter):
    """Shared transport for direct OpenAI and explicit OpenRouter clients."""

    def __init__(
        self,
        provider_name: str,
        client: Any,
        **kwargs,
    ):
        if provider_name not in {"openai", "openrouter"}:
            raise ValueError("OpenAI-compatible provider must be openai or openrouter")
        self.provider_name = provider_name
        super().__init__(client, **kwargs)

    def complete(self, request: CompletionRequest) -> ProviderReceipt:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
            "temperature": request.temperature,
        }
        extra_body: dict[str, Any] = {}
        if request.messages is not None:
            payload["messages"] = json.loads(canonical_json(request.messages))
            if self.provider_name == "openai":
                payload["parallel_tool_calls"] = False
            # OpenRouter filters require_parameters routes by model support.
            # Some tool-capable endpoints do not advertise parallel_tool_calls;
            # the research controller already validates and executes calls in order.
            if request.tools is not None:
                payload["tools"] = json.loads(canonical_json(request.tools))
            if request.tool_choice is not None:
                payload["tool_choice"] = json.loads(canonical_json(request.tool_choice))
            if self.provider_name == "openrouter" and request.tools:
                extra_body["provider"] = {"require_parameters": True}
        if request.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_completion",
                    "strict": True,
                    "schema": json.loads(canonical_json(request.response_schema)),
                },
            }
            if self.provider_name == "openrouter":
                # Do not silently route a structured request to an endpoint that
                # ignores JSON Schema.
                extra_body["provider"] = {"require_parameters": True}
        if self.provider_name == "openrouter":
            # The host Harbor relay enforces the same operator exclusion in
            # its standalone deployment. Apply it to core callers as well.
            extra_body.setdefault("provider", {})["ignore"] = list(ignored_providers(request.model))
            payload["max_tokens"] = request.max_output_tokens
            if request.reasoning_effort is not None:
                extra_body["reasoning"] = {"enabled": True, "effort": request.reasoning_effort}
            elif request.messages is not None:
                extra_body["reasoning"] = {"enabled": not request.disable_reasoning}
            elif request.disable_reasoning:
                # A thinking model may spend the whole cap on hidden reasoning and
                # return empty content, which no structured caller can use.
                extra_body["reasoning"] = {"enabled": False}
        else:
            payload["max_completion_tokens"] = request.max_output_tokens
            if request.reasoning_effort is not None:
                payload["reasoning_effort"] = request.reasoning_effort
        if extra_body:
            payload["extra_body"] = extra_body
        try:
            response = self.client.chat.completions.create(**payload)
        except Exception as exc:
            detail, rejected = _transport_defect(exc)
            if rejected:
                raise ProviderCallRefused(detail) from exc
            return ProviderReceipt(
                provider=self.provider_name,
                model=request.model,
                defect=detail,
            )
        refusal = _refusal(response)
        if refusal is not None:
            raise ProviderCallRefused(refusal)
        usage = _field(response, "usage")
        choices = _field(response, "choices", ())
        try:
            first = choices[0]
        except (IndexError, KeyError, TypeError):
            first = None
        native_defect = None
        if request.messages is not None:
            try:
                response_text = _native_message_text(_field(first, "message"))
            except (TypeError, ValueError):
                response_text = None
                native_defect = "provider returned a non-JSON native assistant message"
        else:
            response_text = _optional_text(_field(_field(first, "message"), "content"))
        return self._receipt(
            model=request.model,
            text=response_text,
            input_tokens=_optional_token_count(usage, "prompt_tokens"),
            output_tokens=_optional_token_count(usage, "completion_tokens"),
            reported_cost=(
                _field(usage, "cost")
                if _field(usage, "cost") is not None
                else _field(usage, "total_cost")
            ),
            attestation_id=_attestation_id(response, usage),
            finish_reason=_field(first, "finish_reason"),
            defects=(
                _identity_defect(
                    response,
                    request.model,
                    resolved_alias_ok=self.provider_name == "openrouter",
                ),
                None if first is not None else "provider returned no choices",
                native_defect,
            ),
        )


class AnthropicAdapter(_CostedAdapter):
    provider_name = "anthropic"

    def complete(self, request: CompletionRequest) -> ProviderReceipt:
        if request.reasoning_effort is not None:
            raise ProviderCallRefused("explicit reasoning effort is unsupported by the Anthropic adapter")
        if request.messages is not None:
            raise ProviderCallRefused(
                "native messages/tools are unsupported by the Anthropic adapter"
            )
        try:
            response = self.client.messages.create(
                model=request.model,
                system=request.system,
                messages=[{"role": "user", "content": request.prompt}],
                max_tokens=request.max_output_tokens,
                temperature=request.temperature,
            )
        except Exception as exc:
            detail, rejected = _transport_defect(exc)
            if rejected:
                raise ProviderCallRefused(detail) from exc
            return ProviderReceipt(
                provider=self.provider_name,
                model=request.model,
                defect=detail,
            )
        refusal = _refusal(response)
        if refusal is not None:
            raise ProviderCallRefused(refusal)
        blocks = _field(response, "content", ()) or ()
        try:
            text = "".join(
                str(_field(block, "text", ""))
                for block in blocks
                if _field(block, "type", "text") == "text"
            )
        except TypeError:
            text = ""
        usage = _field(response, "usage")
        return self._receipt(
            model=request.model,
            text=_optional_text(text),
            input_tokens=_optional_token_count(usage, "input_tokens"),
            output_tokens=_optional_token_count(usage, "output_tokens"),
            reported_cost=_field(usage, "cost_usd"),
            attestation_id=_attestation_id(response, usage),
            finish_reason=_field(response, "stop_reason"),
            defects=(_identity_defect(response, request.model),),
        )


class GeminiAdapter(_CostedAdapter):
    provider_name = "gemini"

    def complete(self, request: CompletionRequest) -> ProviderReceipt:
        if request.reasoning_effort is not None:
            raise ProviderCallRefused("explicit reasoning effort is unsupported by the Gemini adapter")
        if request.messages is not None:
            raise ProviderCallRefused(
                "native messages/tools are unsupported by the Gemini adapter"
            )
        try:
            response = self.client.models.generate_content(
                model=request.model,
                contents=request.prompt,
                config={
                    "system_instruction": request.system,
                    "temperature": request.temperature,
                    "max_output_tokens": request.max_output_tokens,
                },
            )
        except Exception as exc:
            detail, rejected = _transport_defect(exc)
            if rejected:
                raise ProviderCallRefused(detail) from exc
            return ProviderReceipt(
                provider=self.provider_name,
                model=request.model,
                defect=detail,
            )
        refusal = _refusal(response)
        if refusal is not None:
            raise ProviderCallRefused(refusal)
        usage = _field(response, "usage_metadata")
        return self._receipt(
            model=request.model,
            text=_optional_text(_field(response, "text")),
            input_tokens=_optional_token_count(usage, "prompt_token_count"),
            output_tokens=_optional_token_count(usage, "candidates_token_count"),
            reported_cost=_field(usage, "cost_usd"),
            attestation_id=_attestation_id(response, usage),
            finish_reason=_field(response, "finish_reason"),
            defects=(_identity_defect(response, request.model),),
        )


class OpenAIAdapter(OpenAICompatibleAdapter):
    def __init__(self, client: Any, **kwargs):
        super().__init__("openai", client, **kwargs)


class OpenRouterAdapter(OpenAICompatibleAdapter):
    def __init__(self, client: Any, **kwargs):
        super().__init__("openrouter", client, **kwargs)


class OpenAICompatibleEmbeddingAdapter(_CostedAdapter):
    """OpenAI-shape embedding transport for direct OpenAI or OpenRouter."""

    def __init__(self, provider_name: str, client: Any, **kwargs):
        if provider_name not in {"openai", "openrouter"}:
            raise ValueError("embedding provider must be openai or openrouter")
        self.provider_name = provider_name
        super().__init__(client, **kwargs)

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        payload: dict[str, Any] = {
            "model": request.model,
            "input": list(request.texts),
            "encoding_format": "float",
        }
        if self.provider_name == "openrouter":
            payload["extra_body"] = {"provider": {"ignore": list(ignored_providers(request.model))}}
        try:
            response = self.client.embeddings.create(**payload)
        except Exception as exc:
            detail, rejected = _transport_defect(exc)
            if rejected:
                raise ProviderCallRefused(detail) from exc
            raise ProviderEmbeddingUnusable(detail) from exc
        refusal = _refusal(response)
        if refusal is not None:
            raise ProviderCallRefused(refusal)
        usage = _field(response, "usage")
        reported = self._usage(
            model=request.model,
            input_tokens=_optional_token_count(usage, "prompt_tokens") or 0,
            output_tokens=0,
            reported_cost=(
                _field(usage, "cost")
                if _field(usage, "cost") is not None
                else _field(usage, "total_cost")
            ),
            request_id=_attestation_id(response, usage),
        )
        try:
            _check_model_identity(
                response,
                request.model,
                resolved_alias_ok=self.provider_name == "openrouter",
            )
            raw_rows = tuple(_field(response, "data", ()))
            indexed_rows = []
            for row in raw_rows:
                index = _field(row, "index")
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or index < 0
                    or index >= len(request.texts)
                ):
                    raise ValueError("embedding response index is invalid")
                indexed_rows.append((index, row))
            indexed_rows.sort(key=lambda item: item[0])
            if tuple(index for index, _row in indexed_rows) != tuple(
                range(len(request.texts))
            ):
                raise ValueError(
                    "embedding response indices must be unique and contiguous"
                )
            rows = tuple(row for _index, row in indexed_rows)
            vectors = tuple(
                tuple(float(value) for value in _field(row, "embedding", ()))
                for row in rows
            )
            if len(vectors) != len(request.texts):
                raise ValueError("embedding response row count differs from request")
        except (AttributeError, TypeError, ValueError, ProviderAccountingError) as exc:
            # The dispatch may already have been billed, so whatever the
            # provider did say about tokens and cost has to survive the failure.
            raise ProviderEmbeddingUnusable(
                f"invalid {self.provider_name} embedding response: {exc}",
                usage=reported,
            ) from exc
        return EmbeddingResult(
            vectors=vectors,
            provider=self.provider_name,
            model=request.model,
            usage=reported,
        )


class OpenAIEmbeddingAdapter(OpenAICompatibleEmbeddingAdapter):
    def __init__(self, client: Any, **kwargs):
        super().__init__("openai", client, **kwargs)


class OpenRouterEmbeddingAdapter(OpenAICompatibleEmbeddingAdapter):
    def __init__(self, client: Any, **kwargs):
        super().__init__("openrouter", client, **kwargs)


__all__ = [
    "AnthropicAdapter",
    "GeminiAdapter",
    "OpenAIAdapter",
    "OpenAICompatibleAdapter",
    "OpenAICompatibleEmbeddingAdapter",
    "OpenAIEmbeddingAdapter",
    "OpenRouterAdapter",
    "OpenRouterEmbeddingAdapter",
]
