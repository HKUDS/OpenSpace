"""Explicit live canary for OpenRouter embedding Analytics reconciliation."""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agentpilot.domain import canonical_json
from agentpilot.providers.adapters import OpenRouterEmbeddingAdapter
from agentpilot.providers.base import (
    EmbeddingRequest,
    ProviderAccountingError,
    ProviderAccountingPendingError,
)
from agentpilot.providers.embedding_accounting import (
    EmbeddingAccountingReconciler,
    JsonAnalyticsTransport,
    OpenRouterEmbeddingAnalyticsLookup,
)

CANARY_REPORT_SCHEMA = "openrouter-embedding-accounting-canary"
_CANARY_INPUT = "AgentPilot public embedding accounting canary input."
_INFERENCE_KEY_ENV = "OPENROUTER_API_KEY"
_MANAGEMENT_KEY_ENV = "OPENROUTER_MANAGEMENT_KEY"


def _model_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise argparse.ArgumentTypeError(
            "model must be a non-empty printable OpenRouter model id"
        )
    return value


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if not 1 <= parsed <= 120:
        raise argparse.ArgumentTypeError("value must be between 1 and 120")
    return parsed


def _poll_interval(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("poll interval must be numeric") from exc
    if not math.isfinite(parsed) or not 0.1 <= parsed <= 60.0:
        raise argparse.ArgumentTypeError(
            "poll interval must be between 0.1 and 60 seconds"
        )
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agentpilot.providers.openrouter_embedding_canary",
        description=(
            "Send one fixed public embedding input, then verify its cost through "
            "the independent OpenRouter Analytics API."
        ),
    )
    parser.add_argument("--model", required=True, type=_model_name)
    parser.add_argument("--max-polls", type=_positive_int, default=12)
    parser.add_argument(
        "--poll-interval-seconds",
        type=_poll_interval,
        default=5.0,
    )
    return parser


def _default_client_factory(inference_key: str) -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - depends on local integration
        raise RuntimeError(
            "the OpenRouter canary requires the `openai` SDK; install it in "
            "the integration environment before running this command"
        ) from exc
    return OpenAI(
        api_key=inference_key,
        base_url="https://openrouter.ai/api/v1",
        max_retries=0,
        timeout=30.0,
    )


def _credential(
    environment: Mapping[str, str],
    name: str,
) -> str | None:
    value = environment.get(name)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        return None
    return value


def _report(status: str, **facts: Any) -> str:
    return canonical_json(
        {
            "schema": CANARY_REPORT_SCHEMA,
            "status": status,
            "formal_accepted": False,
            **facts,
        }
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    client_factory: Callable[[str], Any] | None = None,
    analytics_transport: JsonAnalyticsTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run one paid inference and bounded management-key reconciliation polls."""

    arguments = _parser().parse_args(argv)
    values = os.environ if environment is None else environment
    inference_key = _credential(values, _INFERENCE_KEY_ENV)
    management_key = _credential(values, _MANAGEMENT_KEY_ENV)
    if inference_key is None or management_key is None:
        print(
            _report(
                "configuration_error",
                error="required_environment_credentials_are_missing_or_invalid",
            ),
            file=sys.stderr,
        )
        return 2
    if inference_key == management_key:
        print(
            _report(
                "configuration_error",
                error="inference_and_management_credentials_must_be_distinct",
            ),
            file=sys.stderr,
        )
        return 2

    factory = client_factory or _default_client_factory
    try:
        client = factory(inference_key)
        result = OpenRouterEmbeddingAdapter(client).embed(
            EmbeddingRequest(
                model=arguments.model,
                texts=(_CANARY_INPUT,),
            )
        )
    except Exception:
        print(
            _report(
                "inference_failed",
                model=arguments.model,
                error="embedding_request_or_response_validation_failed",
            ),
            file=sys.stderr,
        )
        return 4

    request_id = result.usage.provider_request_id
    if (
        not isinstance(request_id, str)
        or not request_id.startswith("gen-")
        or request_id != request_id.strip()
    ):
        print(
            _report(
                "inference_failed",
                model=arguments.model,
                error="embedding_response_id_is_not_reconcilable",
            ),
            file=sys.stderr,
        )
        return 4

    lookup = OpenRouterEmbeddingAnalyticsLookup(
        management_key=management_key,
        model=arguments.model,
        transport=analytics_transport,
    )
    reconciler = EmbeddingAccountingReconciler(lookup)
    for attempt in range(1, arguments.max_polls + 1):
        try:
            usage = reconciler.reconcile(model=arguments.model, usage=result.usage)
        except ProviderAccountingPendingError:
            if attempt < arguments.max_polls:
                sleep(arguments.poll_interval_seconds)
                continue
            print(
                _report(
                    "analytics_not_ready",
                    model=arguments.model,
                    provider_request_id=request_id,
                    input_tokens=result.usage.input_tokens,
                    analytics_poll_attempts=attempt,
                    error="analytics_row_not_available_within_poll_budget",
                ),
                file=sys.stderr,
            )
            return 3
        except ProviderAccountingError:
            print(
                _report(
                    "reconciliation_failed",
                    model=arguments.model,
                    provider_request_id=request_id,
                    input_tokens=result.usage.input_tokens,
                    analytics_poll_attempts=attempt,
                    error="analytics_identity_usage_or_schema_validation_failed",
                ),
                file=sys.stderr,
            )
            return 4
        print(
            _report(
                "reconciled",
                model=arguments.model,
                provider_request_id=request_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                embedding_dimension=len(result.vectors[0]),
                cost_usd=usage.cost_usd,
                accounting_status=usage.accounting_status.value,
                analytics_poll_attempts=attempt,
            )
        )
        return 0
    raise AssertionError("bounded poll loop did not return")


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess use
    raise SystemExit(main())


__all__ = ["CANARY_REPORT_SCHEMA", "main"]
