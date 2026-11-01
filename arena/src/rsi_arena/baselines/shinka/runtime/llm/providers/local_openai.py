import backoff
import logging
import openai
from rsi_arena.baselines.shinka.runtime.llm.constants import BACKOFF_MAX_TIME, BACKOFF_MAX_TRIES, BACKOFF_MAX_VALUE

from .pricing import calculate_cost, model_exists
from .result import QueryResult

logger = logging.getLogger(__name__)

MAX_TRIES = BACKOFF_MAX_TRIES
MAX_VALUE = BACKOFF_MAX_VALUE
MAX_TIME = BACKOFF_MAX_TIME


def backoff_handler(details):
    exc = details.get("exception")
    if exc:
        logger.warning(
            f"Local OpenAI - Retry {details['tries']} due to error: {exc}. Waiting {details['wait']:0.1f}s..."
        )


def _extract_costs(model: str, in_tokens: int, all_out_tokens: int) -> tuple[float, float]:
    if model_exists(model):
        return calculate_cost(model, in_tokens, all_out_tokens)
    return 0.0, 0.0


def _extract_usage(response) -> tuple[int, int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0, 0
    in_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    all_out_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    completion_details = getattr(usage, "completion_tokens_details", None)
    thinking_tokens = 0
    if completion_details is not None:
        thinking_tokens = int(getattr(completion_details, "reasoning_tokens", 0) or 0)
    return in_tokens, all_out_tokens, thinking_tokens


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
def query_local_openai(
    client,
    model,
    msg,
    system_msg,
    msg_history,
    output_model,
    model_posteriors=None,
    **kwargs,
) -> QueryResult:
    if output_model is not None:
        raise NotImplementedError(
            "Structured output is not supported for local OpenAI-compatible backends."
        )

    new_msg_history = msg_history + [{"role": "user", "content": msg}]
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system_msg}, *new_msg_history],
        **kwargs,
        n=1,
    )
    content = response.choices[0].message.content or ""
    thought = getattr(response.choices[0].message, "reasoning_content", "") or ""
    new_msg_history.append({"role": "assistant", "content": content})

    in_tokens, all_out_tokens, thinking_tokens = _extract_usage(response)
    out_tokens = max(all_out_tokens - thinking_tokens, 0)
    input_cost, output_cost = _extract_costs(model, in_tokens, all_out_tokens)

    return QueryResult(
        content=content,
        msg=msg,
        system_msg=system_msg,
        new_msg_history=new_msg_history,
        model_name=model,
        kwargs=kwargs,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        thinking_tokens=thinking_tokens,
        cost=input_cost + output_cost,
        input_cost=input_cost,
        output_cost=output_cost,
        thought=thought,
        model_posteriors=model_posteriors,
    )


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
async def query_local_openai_async(
    client,
    model,
    msg,
    system_msg,
    msg_history,
    output_model,
    model_posteriors=None,
    **kwargs,
) -> QueryResult:
    if output_model is not None:
        raise NotImplementedError(
            "Structured output is not supported for local OpenAI-compatible backends."
        )

    new_msg_history = msg_history + [{"role": "user", "content": msg}]
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system_msg}, *new_msg_history],
        **kwargs,
        n=1,
    )
    content = response.choices[0].message.content or ""
    thought = getattr(response.choices[0].message, "reasoning_content", "") or ""
    new_msg_history.append({"role": "assistant", "content": content})

    in_tokens, all_out_tokens, thinking_tokens = _extract_usage(response)
    out_tokens = max(all_out_tokens - thinking_tokens, 0)
    input_cost, output_cost = _extract_costs(model, in_tokens, all_out_tokens)

    return QueryResult(
        content=content,
        msg=msg,
        system_msg=system_msg,
        new_msg_history=new_msg_history,
        model_name=model,
        kwargs=kwargs,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        thinking_tokens=thinking_tokens,
        cost=input_cost + output_cost,
        input_cost=input_cost,
        output_cost=output_cost,
        thought=thought,
        model_posteriors=model_posteriors,
    )
