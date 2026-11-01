import backoff
import openai
import time
from rsi_arena.baselines.shinka.runtime.llm.constants import BACKOFF_MAX_TIME, BACKOFF_MAX_TRIES, BACKOFF_MAX_VALUE
from .pricing import calculate_cost, model_exists
from .result import QueryResult
import logging

logger = logging.getLogger(__name__)

MAX_TRIES = BACKOFF_MAX_TRIES
MAX_VALUE = BACKOFF_MAX_VALUE
MAX_TIME = BACKOFF_MAX_TIME


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _sequence(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return value
    return [value]


def _extract_response_text(response) -> str:
    output_text = _field(response, "output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output = _sequence(_field(response, "output"))
    for item in output:
        if _field(item, "type") != "message":
            continue
        for content in _sequence(_field(item, "content")):
            if _field(content, "type") != "output_text":
                continue
            text = _field(content, "text")
            if isinstance(text, str) and text.strip():
                return text

    output_types = [_field(item, "type", type(item).__name__) for item in output]
    status = _field(response, "status")
    incomplete = _field(response, "incomplete_details")
    incomplete_reason = _field(incomplete, "reason")
    raise ValueError(
        "OpenAI response contained no text output; "
        f"status={status}; output_types={output_types}; "
        f"incomplete_reason={incomplete_reason}"
    )


def _extract_reasoning_summary(response) -> str:
    summaries = []
    for item in _sequence(_field(response, "output")):
        for summary in _sequence(_field(item, "summary")):
            text = _field(summary, "text")
            if isinstance(text, str) and text.strip():
                summaries.append(text)
    return "\n".join(summaries)


def backoff_handler(details):
    exc = details.get("exception")
    if exc:
        retry_after = _retry_after_seconds(exc)
        if retry_after is not None and retry_after > details["wait"]:
            logger.warning(
                "OpenAI - server requested retry_after=%ss; sleeping before retry.",
                retry_after,
            )
            time.sleep(retry_after - details["wait"])
        logger.warning(
            f"OpenAI - Retry {details['tries']} due to error: {exc}. Waiting {details['wait']:0.1f}s..."
        )


def _retry_after_seconds(exc) -> int | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        value = headers.get("retry-after")
        if value:
            try:
                return int(float(value))
            except (TypeError, ValueError):
                pass
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        value = body.get("retry_after")
        if value is not None:
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None
    return None


def get_openai_costs(response, model):
    # Get token counts and costs
    in_tokens = response.usage.input_tokens
    try:
        thinking_tokens = response.usage.output_tokens_details.reasoning_tokens
    except Exception:
        thinking_tokens = 0
    all_out_tokens = response.usage.output_tokens
    out_tokens = response.usage.output_tokens - thinking_tokens

    # Get actual costs from OpenRouter API if available -- if not use OAI
    cost_details = getattr(response.usage, "cost_details", None)
    if cost_details:
        if isinstance(cost_details, dict):
            input_cost = float(cost_details.get("upstream_inference_input_cost", 0.0))
            output_cost = float(cost_details.get("upstream_inference_output_cost", 0.0))
        else:
            input_cost = float(
                getattr(cost_details, "upstream_inference_input_cost", 0.0) or 0.0
            )
            output_cost = float(
                getattr(cost_details, "upstream_inference_output_cost", 0.0) or 0.0
            )
    elif model_exists(model):
        input_cost, output_cost = calculate_cost(model, in_tokens, all_out_tokens)
    else:
        logger.warning(
            "Model '%s' has no pricing entry and response cost metadata is absent. "
            "Defaulting query cost to 0.",
            model,
        )
        input_cost, output_cost = 0.0, 0.0
    return {
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "thinking_tokens": thinking_tokens,
        "input_cost": input_cost,
        "output_cost": output_cost,
        "cost": input_cost + output_cost,
    }


@backoff.on_exception(
    backoff.expo,
    (
        openai.APIConnectionError,
        openai.APIStatusError,
        openai.RateLimitError,
        openai.APITimeoutError,
    ),
    max_tries=MAX_TRIES,
    max_value=MAX_VALUE,
    max_time=MAX_TIME,
    on_backoff=backoff_handler,
)
def query_openai(
    client,
    model,
    msg,
    system_msg,
    msg_history,
    output_model,
    model_posteriors=None,
    **kwargs,
) -> QueryResult:
    """Query OpenAI model."""
    new_msg_history = msg_history + [{"role": "user", "content": msg}]
    thought = ""
    if output_model is None:
        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system_msg},
                *new_msg_history,
            ],
            **kwargs,
        )
        content = _extract_response_text(response)
        thought = _extract_reasoning_summary(response)
        new_msg_history.append({"role": "assistant", "content": content})
    else:
        response = client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": system_msg},
                *new_msg_history,
            ],
            text_format=output_model,
            **kwargs,
        )
        content = response.output_parsed
        new_content = ""
        for i in content:
            new_content += i[0] + ":" + i[1] + "\n"
        new_msg_history.append({"role": "assistant", "content": new_content})

    # Get token counts and costs
    cost_results = get_openai_costs(response, model)

    # Collect all results
    result = QueryResult(
        content=content,
        msg=msg,
        system_msg=system_msg,
        new_msg_history=new_msg_history,
        model_name=model,
        kwargs=kwargs,
        **cost_results,
        thought=thought,
        model_posteriors=model_posteriors,
    )
    return result


@backoff.on_exception(
    backoff.expo,
    (
        openai.APIConnectionError,
        openai.APIStatusError,
        openai.RateLimitError,
        openai.APITimeoutError,
    ),
    max_tries=MAX_TRIES,
    max_value=MAX_VALUE,
    max_time=MAX_TIME,
    on_backoff=backoff_handler,
)
async def query_openai_async(
    client,
    model,
    msg,
    system_msg,
    msg_history,
    output_model,
    model_posteriors=None,
    **kwargs,
) -> QueryResult:
    """Query OpenAI model asynchronously."""
    new_msg_history = msg_history + [{"role": "user", "content": msg}]
    thought = ""
    if output_model is None:
        response = await client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system_msg},
                *new_msg_history,
            ],
            **kwargs,
        )
        content = _extract_response_text(response)
        thought = _extract_reasoning_summary(response)
        new_msg_history.append({"role": "assistant", "content": content})
    else:
        response = await client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": system_msg},
                *new_msg_history,
            ],
            text_format=output_model,
            **kwargs,
        )
        content = response.output_parsed
        new_content = ""
        for i in content:
            new_content += i[0] + ":" + i[1] + "\n"
        new_msg_history.append({"role": "assistant", "content": new_content})
    cost_results = get_openai_costs(response, model)
    result = QueryResult(
        content=content,
        msg=msg,
        system_msg=system_msg,
        new_msg_history=new_msg_history,
        model_name=model,
        kwargs=kwargs,
        **cost_results,
        thought=thought,
        model_posteriors=model_posteriors,
    )
    return result
