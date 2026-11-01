import backoff
import anthropic
from rsi_arena.baselines.shinka.runtime.llm.constants import BACKOFF_MAX_TIME, BACKOFF_MAX_TRIES, BACKOFF_MAX_VALUE
from .pricing import calculate_cost
from .result import QueryResult
import logging

logger = logging.getLogger(__name__)


MAX_TRIES = BACKOFF_MAX_TRIES
MAX_VALUE = BACKOFF_MAX_VALUE
MAX_TIME = BACKOFF_MAX_TIME


def get_anthropic_costs(response, model):
    """Get the costs for the given response and model."""
    # Get token counts and costs
    input_tokens = response.usage.input_tokens
    all_out_tokens = response.usage.output_tokens
    # Unclear how to get thinking tokens from Anthropic
    thinking_tokens = 0
    input_cost, output_cost = calculate_cost(model, input_tokens, all_out_tokens)
    return {
        "input_tokens": input_tokens,
        "output_tokens": all_out_tokens,
        "thinking_tokens": thinking_tokens,
        "input_cost": input_cost,
        "output_cost": output_cost,
        "cost": input_cost + output_cost,
    }


def backoff_handler(details):
    exc = details.get("exception")
    if exc:
        logger.info(
            f"Anthropic - Retry {details['tries']} due to error: {exc}. Waiting {details['wait']:0.1f}s..."
        )


@backoff.on_exception(
    backoff.expo,
    (
        anthropic.APIConnectionError,
        anthropic.APIStatusError,
        anthropic.RateLimitError,
        anthropic.APITimeoutError,
    ),
    max_tries=MAX_TRIES,
    max_value=MAX_VALUE,
    max_time=MAX_TIME,
    on_backoff=backoff_handler,
)
def query_anthropic(
    client,
    model,
    msg,
    system_msg,
    msg_history,
    output_model,
    model_posteriors=None,
    **kwargs,
) -> QueryResult:
    """Query Anthropic/Bedrock model."""
    new_msg_history = msg_history + [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": msg,
                }
            ],
        }
    ]
    if output_model is None:
        response = client.messages.create(
            model=model,
            system=system_msg,
            messages=new_msg_history,
            **kwargs,
        )
        # Separate thinking from non-thinking content
        if len(response.content) == 1:
            thought = ""
            content = response.content[0].text
        else:
            thought = response.content[0].thinking
            content = response.content[1].text
    else:
        raise NotImplementedError("Structured output not supported for Anthropic.")
    new_msg_history.append(
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": content,
                }
            ],
        }
    )
    cost_results = get_anthropic_costs(response, model)
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
        anthropic.APIConnectionError,
        anthropic.APIStatusError,
        anthropic.RateLimitError,
        anthropic.APITimeoutError,
    ),
    max_tries=MAX_TRIES,
    max_value=MAX_VALUE,
    max_time=MAX_TIME,
    on_backoff=backoff_handler,
)
async def query_anthropic_async(
    client,
    model,
    msg,
    system_msg,
    msg_history,
    output_model,
    model_posteriors=None,
    **kwargs,
) -> QueryResult:
    """Query Anthropic/Bedrock model asynchronously."""
    new_msg_history = msg_history + [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": msg,
                }
            ],
        }
    ]
    if output_model is None:
        response = await client.messages.create(
            model=model,
            system=system_msg,
            messages=new_msg_history,
            **kwargs,
        )
        # Separate thinking from non-thinking content
        if len(response.content) == 1:
            thought = ""
            content = response.content[0].text
        else:
            thought = response.content[0].thinking
            content = response.content[1].text
    else:
        raise NotImplementedError("Structured output not supported for Anthropic.")
    new_msg_history.append(
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": content,
                }
            ],
        }
    )
    input_cost, output_cost = calculate_cost(
        model, response.usage.input_tokens, response.usage.output_tokens
    )
    result = QueryResult(
        content=content,
        msg=msg,
        system_msg=system_msg,
        new_msg_history=new_msg_history,
        model_name=model,
        kwargs=kwargs,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cost=input_cost + output_cost,
        input_cost=input_cost,
        output_cost=output_cost,
        thought=thought,
        model_posteriors=model_posteriors,
    )
    return result
