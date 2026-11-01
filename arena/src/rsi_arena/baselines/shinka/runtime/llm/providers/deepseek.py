import backoff
import openai
from rsi_arena.baselines.shinka.runtime.llm.constants import BACKOFF_MAX_TIME, BACKOFF_MAX_TRIES, BACKOFF_MAX_VALUE
from .pricing import calculate_cost
from .result import QueryResult
import logging

logger = logging.getLogger(__name__)


MAX_TRIES = BACKOFF_MAX_TRIES
MAX_VALUE = BACKOFF_MAX_VALUE
MAX_TIME = BACKOFF_MAX_TIME


def backoff_handler(details):
    exc = details.get("exception")
    if exc:
        logger.info(
            f"DeepSeek - Retry {details['tries']} due to error: {exc}. Waiting {details['wait']:0.1f}s..."
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
def query_deepseek(
    client,
    model,
    msg,
    system_msg,
    msg_history,
    output_model,
    model_posteriors=None,
    **kwargs,
) -> QueryResult:
    """Query DeepSeek model."""
    if output_model is not None:
        raise NotImplementedError("Structured output not supported for DeepSeek.")
    new_msg_history = msg_history + [{"role": "user", "content": msg}]
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_msg},
            *new_msg_history,
        ],
        **kwargs,
        n=1,
        stop=None,
    )
    content = response.choices[0].message.content
    try:
        thought = response.choices[0].message.reasoning_content
    except:
        thought = ""
    new_msg_history.append({"role": "assistant", "content": content})

    # Get token counts and costs
    in_tokens = response.usage.prompt_tokens
    all_out_tokens = response.usage.completion_tokens
    try:
        thinking_tokens = response.usage.completion_tokens_details.reasoning_tokens
    except Exception:
        thinking_tokens = 0
    out_tokens = all_out_tokens - thinking_tokens
    input_cost, output_cost = calculate_cost(model, in_tokens, all_out_tokens)

    # Collect all results
    result = QueryResult(
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
async def query_deepseek_async(
    client,
    model,
    msg,
    system_msg,
    msg_history,
    output_model,
    model_posteriors=None,
    **kwargs,
) -> QueryResult:
    """Query DeepSeek model asynchronously."""
    if output_model is not None:
        raise NotImplementedError("Structured output not supported for DeepSeek.")
    new_msg_history = msg_history + [{"role": "user", "content": msg}]
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_msg},
            *new_msg_history,
        ],
        **kwargs,
        n=1,
        stop=None,
    )
    content = response.choices[0].message.content
    try:
        thought = response.choices[0].message.reasoning_content
    except:
        thought = ""
    new_msg_history.append({"role": "assistant", "content": content})
    input_cost, output_cost = calculate_cost(
        model, response.usage.prompt_tokens, response.usage.completion_tokens
    )
    return QueryResult(
        content=content,
        msg=msg,
        system_msg=system_msg,
        new_msg_history=new_msg_history,
        model_name=model,
        kwargs=kwargs,
        input_tokens=response.usage.prompt_tokens,
        output_tokens=response.usage.completion_tokens,
        cost=input_cost + output_cost,
        input_cost=input_cost,
        output_cost=output_cost,
        thought=thought,
        model_posteriors=model_posteriors,
    )
