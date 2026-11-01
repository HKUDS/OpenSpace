"""Opt-in OpenAI-compatible reference runner for BrowseComp-Plus.

Select this runner explicitly with
``python:rsi_arena.benchmarks.browsecomp_plus.openai_reference:run_agent``.
It is one benchmark-owned implementation of the existing ``runner_ref`` seam;
custom runners remain independent and unrestricted.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .usage import (
    USAGE_SIDECAR_ENV,
    ModelUsage,
    aggregate_model_usage,
    append_usage_sidecar,
    usage_from_openai_response,
)

REFERENCE_AGENT_REF = "python:browsecomp_agent:run"
DEFAULT_GLOBAL_COMPLETION_TOKENS = 20_000
DEFAULT_MAX_ITERATIONS = 100
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_INVALID_SEARCH_ARGUMENTS = 'invalid search arguments; retry with {"query":"..."}'
_FINAL_RESPONSE_KEYS = frozenset({"explanation", "exact_answer", "confidence"})


def _submit_answer_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": "submit_answer",
            "description": (
                "Submit the final BrowseComp answer. Call this exactly once, as the "
                "only tool call in the terminal response."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "explanation": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": r"^[^\r\n]+$",
                    },
                    "exact_answer": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": r"^[^\r\n]+$",
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 100,
                    },
                },
                "required": ["explanation", "exact_answer", "confidence"],
                "additionalProperties": False,
            },
        },
    }


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        value[key] = item
    return value


def _canonical_submitted_answer(arguments: object) -> str | None:
    if not isinstance(arguments, str) or not arguments:
        return None
    try:
        payload = json.loads(
            arguments,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_non_finite_json,
            object_pairs_hook=_unique_json_object,
        )
    except (InvalidOperation, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != _FINAL_RESPONSE_KEYS:
        return None
    explanation = payload["explanation"]
    exact_answer = payload["exact_answer"]
    confidence = payload["confidence"]
    if (
        not isinstance(explanation, str)
        or not explanation.strip()
        or "\r" in explanation
        or "\n" in explanation
        or not isinstance(exact_answer, str)
        or not exact_answer.strip()
        or "\r" in exact_answer
        or "\n" in exact_answer
        or not isinstance(confidence, Decimal)
        or not confidence.is_finite()
        or confidence < 0
        or confidence > 100
        or abs(confidence.as_tuple().exponent) > 100
    ):
        return None
    confidence_text = "0" if confidence == 0 else format(confidence, "f")
    if "." in confidence_text:
        confidence_text = confidence_text.rstrip("0").rstrip(".")
    return (
        f"Explanation: {explanation.strip()}\n"
        f"Exact Answer: {exact_answer.strip()}\n"
        f"Confidence: {confidence_text}%"
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _openai_compatible_client():
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "the BrowseComp OpenAI-compatible reference runner requires openai"
        ) from exc
    return OpenAI(
        api_key=_required_env("OPENROUTER_API_KEY"),
        base_url=os.environ.get("OPENROUTER_BASE_URL", _OPENROUTER_BASE_URL),
        timeout=300.0,
        max_retries=1,
    )


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _non_negative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _completion_kwargs(
    runtime: Mapping[str, object],
    *,
    default_max_tokens: int,
) -> dict[str, object]:
    model = runtime.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("BrowseComp reference runtime requires agent_model.model")
    max_tokens = _positive_integer(
        runtime.get("max_tokens", default_max_tokens),
        "BrowseComp reference runtime max_tokens",
    )
    effort = runtime.get("reasoning_effort", "high")
    if not isinstance(effort, str) or not effort.strip():
        raise ValueError("BrowseComp reference runtime reasoning_effort is invalid")
    temperature = runtime.get("temperature", 0.0)
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise TypeError("BrowseComp reference runtime temperature is invalid")
    return {
        "model": model.strip(),
        "temperature": float(temperature),
        "max_tokens": max_tokens,
        "extra_body": {"reasoning": {"effort": effort.strip()}},
    }


def _candidate_root(candidate_source: object) -> Path:
    checkout = getattr(candidate_source, "checkout_path", candidate_source)
    if not isinstance(checkout, (str, os.PathLike)):
        raise TypeError("candidate source has no checkout path")
    root = Path(checkout).resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError("candidate checkout is not a directory")
    return root


def _candidate_prompt(candidate_source: object) -> str:
    root = _candidate_root(candidate_source)
    prompt_path = (root / "inner_prompt.txt").resolve(strict=True)
    if root not in prompt_path.parents or not prompt_path.is_file():
        raise RuntimeError("candidate inner prompt escaped its checkout")
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise RuntimeError("candidate inner prompt is empty")
    return prompt


def _search_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search the frozen BrowseComp corpus.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "minLength": 1}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def _tool_call_record(call: object) -> dict[str, object]:
    function = call.function
    return {
        "id": call.id,
        "type": "function",
        "function": {
            "name": function.name,
            "arguments": function.arguments,
        },
    }


def _search_query(arguments: object) -> str | None:
    if not isinstance(arguments, str):
        return None
    try:
        payload = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        return None
    return query.strip()


def _tool_error(call_id: object, message: str) -> dict[str, object]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(
            {"error": message}, ensure_ascii=False, separators=(",", ":")
        ),
    }


def _inflight_retry_delay(exc: BaseException) -> float | None:
    if getattr(exc, "status_code", None) != 402:
        return None
    body = getattr(exc, "body", None)
    if not isinstance(body, Mapping):
        return None
    error = body.get("error")
    if not isinstance(error, Mapping):
        return None
    metadata = error.get("metadata")
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("reason") != "in_flight_budget_exhausted"
    ):
        return None
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {})
    retry_after = headers.get("Retry-After", "120")
    try:
        return max(1.0, min(float(retry_after), 120.0))
    except (TypeError, ValueError):
        return 120.0


def _http_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    return None


def _is_timeout(exc: BaseException) -> bool:
    error_type = type(exc)
    return isinstance(exc, TimeoutError) or (
        error_type.__name__ == "APITimeoutError"
        and error_type.__module__.partition(".")[0] == "openai"
    )


class _CompletionCallError(RuntimeError):
    def __init__(self, cause: Exception, *, retry_count: int) -> None:
        super().__init__("OpenAI-compatible completion failed")
        self.cause = cause
        self.http_status = _http_status(cause)
        self.retry_count = retry_count


def _create_completion_with_diagnostics(
    client: Any,
    *,
    request: Mapping[str, object],
    inflight_retry_attempts: int,
) -> tuple[object, int]:
    for attempt in range(inflight_retry_attempts + 1):
        try:
            return client.chat.completions.create(**dict(request)), attempt
        except Exception as exc:
            delay = _inflight_retry_delay(exc)
            if delay is None or attempt == inflight_retry_attempts:
                raise _CompletionCallError(exc, retry_count=attempt) from exc
            time.sleep(delay)
    raise AssertionError("unreachable")


def _create_completion(
    client: Any,
    *,
    request: Mapping[str, object],
    inflight_retry_attempts: int,
):
    try:
        response, _retry_count = _create_completion_with_diagnostics(
            client,
            request=request,
            inflight_retry_attempts=inflight_retry_attempts,
        )
    except _CompletionCallError as exc:
        raise exc.cause
    return response


def run_agent(*, candidate_source, agent_ref, model_runtime, config):
    """Build the explicit prompt-snapshot, search-only reference loop."""

    if agent_ref != REFERENCE_AGENT_REF:
        raise ValueError(
            f"BrowseComp reference runner requires agent_ref={REFERENCE_AGENT_REF!r}"
        )
    if not isinstance(model_runtime, Mapping):
        raise TypeError("BrowseComp reference model_runtime must be a mapping")
    if not isinstance(config, Mapping):
        raise TypeError("BrowseComp reference config must be a mapping")
    system_prompt = _candidate_prompt(candidate_source)
    frozen_runtime = dict(model_runtime)
    frozen_config = dict(config)

    def entrypoint(*, context, model_runtime, config):
        del model_runtime, config
        client = _openai_compatible_client()
        max_iterations = _positive_integer(
            frozen_config.get("max_iterations", DEFAULT_MAX_ITERATIONS),
            "BrowseComp reference max_iterations",
        )
        inflight_retry_attempts = _non_negative_integer(
            frozen_config.get("inflight_retry_attempts", 1),
            "BrowseComp reference inflight_retry_attempts",
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": context.instruction},
        ]
        kwargs = _completion_kwargs(
            frozen_runtime,
            default_max_tokens=DEFAULT_GLOBAL_COMPLETION_TOKENS,
        )
        global_max_tokens = int(kwargs["max_tokens"])
        completion_tokens = 0
        provider = str(frozen_runtime.get("provider", "openrouter"))
        model = str(kwargs["model"])
        usage_records = []

        def publish_usage(record: object, ordinal: int) -> None:
            if record is None:
                return
            sidecar = os.environ.get(USAGE_SIDECAR_ENV)
            if sidecar is None:
                return
            identity = json.dumps(
                {
                    "instruction": context.instruction,
                    "model": model,
                    "ordinal": ordinal,
                    "provider": provider,
                    "submit_answer_tool_sha256": hashlib.sha256(
                        json.dumps(
                            _submit_answer_tool(),
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "system_prompt_sha256": hashlib.sha256(
                        system_prompt.encode("utf-8")
                    ).hexdigest(),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            call_id = (
                "browsecomp-agent-"
                + hashlib.sha256(identity.encode("utf-8")).hexdigest()
            )
            append_usage_sidecar(sidecar, call_id=call_id, usage=record)

        def terminal(
            *,
            status: str,
            terminal_reason: str,
            result: list[dict[str, str]],
        ) -> dict[str, object]:
            payload: dict[str, object] = {
                "status": status,
                "terminal_reason": terminal_reason,
                "result": result,
            }
            aggregate = aggregate_model_usage(usage_records)
            if aggregate is not None:
                payload["usage"] = {
                    "calls": [record.as_dict() for record in usage_records]
                }
            return payload

        for _ in range(max_iterations):
            remaining_tokens = global_max_tokens - completion_tokens
            if remaining_tokens <= 0:
                return terminal(
                    status="failed",
                    terminal_reason="global_token_budget_exhausted",
                    result=[],
                )
            kwargs["max_tokens"] = remaining_tokens
            response = _create_completion(
                client,
                request={
                    "messages": messages,
                    "tools": [_search_tool(), _submit_answer_tool()],
                    "tool_choice": "auto",
                    **kwargs,
                },
                inflight_retry_attempts=inflight_retry_attempts,
            )
            usage = getattr(response, "usage", None)
            if usage is not None:
                completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
            usage_record = usage_from_openai_response(
                response,
                role="solver-agent",
                provider=provider,
                model=model,
            )
            if usage_record is None:
                usage_record = ModelUsage(
                    role="solver-agent",
                    provider=provider,
                    model=model,
                )
            usage_records.append(usage_record)
            publish_usage(usage_record, len(usage_records))
            choice = response.choices[0]
            message = choice.message
            calls = list(message.tool_calls or ())
            assistant: dict[str, Any] = {
                "role": "assistant",
                "content": message.content or "",
            }
            if calls:
                assistant["tool_calls"] = [_tool_call_record(call) for call in calls]
            messages.append(assistant)
            if calls:
                submissions = [
                    call
                    for call in calls
                    if getattr(call.function, "name", None) == "submit_answer"
                ]
                if submissions:
                    content = message.content
                    if len(calls) != 1 or (
                        content is not None
                        and (not isinstance(content, str) or bool(content.strip()))
                    ):
                        return terminal(
                            status="failed",
                            terminal_reason="invalid_structured_terminal",
                            result=[],
                        )
                    canonical = _canonical_submitted_answer(
                        submissions[0].function.arguments
                    )
                    if canonical is None:
                        return terminal(
                            status="failed",
                            terminal_reason="invalid_structured_terminal",
                            result=[],
                        )
                    return terminal(
                        status="completed",
                        terminal_reason="completed_submit_answer",
                        result=[{"type": "output_text", "output": canonical}],
                    )
                for call in calls:
                    call_id = call.id
                    function = call.function
                    if function.name != "search":
                        messages.append(
                            _tool_error(
                                call_id,
                                "unsupported tool; use search or submit_answer",
                            )
                        )
                        continue
                    query = _search_query(function.arguments)
                    if query is None:
                        messages.append(_tool_error(call_id, _INVALID_SEARCH_ARGUMENTS))
                        continue
                    results = context.environment.search(query)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json.dumps(
                                list(results),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        }
                    )
                continue
            if choice.finish_reason == "stop":
                return terminal(
                    status="failed",
                    terminal_reason="invalid_structured_terminal",
                    result=[],
                )
            content = message.content or ""
            result = [{"type": "output_text", "output": content}] if content else []
            finish_reason = str(choice.finish_reason or "unknown")
            return terminal(
                status=finish_reason if choice.finish_reason else "failed",
                terminal_reason=f"provider_finish_reason:{finish_reason}",
                result=result,
            )
        terminal_reason = (
            "global_token_budget_exhausted"
            if completion_tokens >= global_max_tokens
            else "max_iterations_exhausted"
        )
        return terminal(
            status="failed",
            terminal_reason=terminal_reason,
            result=[],
        )

    return entrypoint


def route_judge(*, prompt, provider, model, profile):
    """Route the optional reference judge through the same OpenRouter client."""

    if provider != "openrouter":
        raise ValueError("BrowseComp reference judge provider must be openrouter")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("BrowseComp reference judge prompt must be non-empty")
    if not isinstance(profile, Mapping):
        raise TypeError("BrowseComp reference judge profile must be a mapping")
    runtime = dict(profile)
    runtime["model"] = model
    kwargs = _completion_kwargs(runtime, default_max_tokens=2_048)
    inflight_retry_attempts = _non_negative_integer(
        runtime.get("inflight_retry_attempts", 1),
        "BrowseComp reference judge inflight_retry_attempts",
    )
    try:
        response, retry_count = _create_completion_with_diagnostics(
            _openai_compatible_client(),
            request={"messages": [{"role": "user", "content": prompt}], **kwargs},
            inflight_retry_attempts=inflight_retry_attempts,
        )
    except _CompletionCallError as exc:
        return {
            "text": None,
            "terminal_reason": (
                "timeout"
                if _is_timeout(exc.cause)
                else ("http_error" if exc.http_status is not None else "provider_error")
            ),
            "http_status": exc.http_status,
            "retry_count": exc.retry_count,
        }
    text = response.choices[0].message.content or ""
    terminal = {
        "text": text,
        "terminal_reason": "completed" if text else "empty_response",
        "http_status": None,
        "retry_count": retry_count,
    }
    usage = usage_from_openai_response(
        response,
        role="verifier/judge",
        provider=provider,
        model=model,
    )
    if usage is None:
        usage = ModelUsage(
            role="verifier/judge",
            provider=provider,
            model=model,
        )
    terminal["usage"] = usage.as_dict()
    return terminal


__all__ = [
    "DEFAULT_GLOBAL_COMPLETION_TOKENS",
    "DEFAULT_MAX_ITERATIONS",
    "REFERENCE_AGENT_REF",
    "route_judge",
    "run_agent",
]
