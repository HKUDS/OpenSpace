from typing import List, Union, Optional
import random
from .providers.pricing import (
    is_reasoning_model,
    has_fixed_temperature,
    requires_reasoning,
)
from .providers.model_resolver import resolve_model_backend
import logging

logger = logging.getLogger(__name__)

THINKING_TOKENS = {
    "min": 1024,
    "low": 2048,
    "medium": 4096,
    "high": 8192,
    "max": 16384,
}

NO_TEMPERATURE_MODELS = {
    "claude-opus-4-8",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "us.anthropic.claude-opus-4-8",
}


def sample_batch_kwargs(
    num_samples: int,
    model_names: Union[List[str], str] = "gpt-4o-mini-2024-07-18",
    temperatures: Union[List[float], float] = 0.0,
    max_tokens: Union[List[int], int] = 4096,
    reasoning_efforts: Union[List[str], str] = "",
    model_sample_probs: Optional[List[float]] = None,
    unique_filter: bool = False,
):
    """Sample a dictionary of kwargs for a given model."""
    all_kwargs = []
    attempts = 0
    max_attempts = num_samples * 10  # Prevent infinite loops

    while len(all_kwargs) < num_samples and attempts < max_attempts:
        kwargs_dict = sample_model_kwargs(
            model_names=model_names,
            temperatures=temperatures,
            max_tokens=max_tokens,
            reasoning_efforts=reasoning_efforts,
            model_sample_probs=model_sample_probs,
        )

        if unique_filter:
            if kwargs_dict not in all_kwargs:
                all_kwargs.append(kwargs_dict)
        else:
            all_kwargs.append(kwargs_dict)

        attempts += 1

    if len(all_kwargs) < num_samples:
        logger.info(
            f"Could not generate {num_samples} unique kwargs combinations "
            f"after {max_attempts} attempts"
        )
        logger.info(f"Returning {len(all_kwargs)} unique kwargs combinations.")

    return all_kwargs


def sample_model_kwargs(
    model_names: Union[List[str], str] = "gpt-4o-mini-2024-07-18",
    temperatures: Union[List[float], float] = 0.0,
    max_tokens: Union[List[int], int] = 4096,
    reasoning_efforts: Union[List[str], str] = "",
    model_sample_probs: Optional[List[float]] = None,
):
    """Sample a dictionary of kwargs for a given model."""
    # Make all inputs lists
    if isinstance(model_names, str):
        model_names = [model_names]
    if isinstance(temperatures, float):
        temperatures = [temperatures]
    if isinstance(max_tokens, int):
        max_tokens = [max_tokens]
    if isinstance(reasoning_efforts, str):
        reasoning_efforts = [reasoning_efforts]

    kwargs_dict = {}

    # 1. SAMPLE: model name
    if model_sample_probs is not None:
        if len(model_sample_probs) != len(model_names):
            raise ValueError(
                "model_sample_probs must have the same length as model_names"
            )
        if not abs(sum(model_sample_probs) - 1.0) < 1e-9:
            raise ValueError("model_sample_probs must sum to 1")
        kwargs_dict["model_name"] = random.choices(
            model_names, weights=model_sample_probs, k=1
        )[0]
    else:
        kwargs_dict["model_name"] = random.choice(model_names)

    model_name = kwargs_dict["model_name"]
    resolved_model = resolve_model_backend(model_name)
    api_model_name = resolved_model.api_model_name
    provider = resolved_model.provider

    if provider == "headless":
        return kwargs_dict

    # 2. SAMPLE: reasoning effort
    if is_reasoning_model(model_name):
        r_effort = random.choice(reasoning_efforts)
    else:
        r_effort = "disabled"

    # Some opennrouter models only support running with reasoning effort
    if requires_reasoning(model_name) and r_effort == "disabled":
        r_effort = "low"

    # 3. SAMPLE: temperature with possible reasoning restrictions
    include_temperature = api_model_name not in NO_TEMPERATURE_MODELS and not (
        provider in ("openai", "azure_openai") and is_reasoning_model(model_name)
    )
    if (
        include_temperature
        and has_fixed_temperature(model_name)
        and (
            r_effort != "disabled"
            or provider in ("openai", "openrouter", "azure_openai")
        )
    ):
        kwargs_dict["temperature"] = 1.0
    elif include_temperature:
        kwargs_dict["temperature"] = random.choice(temperatures)

    # 4.a) SET: max_output_tokens for OpenAI reasoning effort
    if provider in ("openai", "openrouter", "azure_openai") and is_reasoning_model(
        model_name
    ):
        kwargs_dict["max_output_tokens"] = random.choice(max_tokens)
        if r_effort == "disabled":
            kwargs_dict["reasoning"] = {"effort": None}
        elif r_effort == "min":
            kwargs_dict["reasoning"] = {"effort": "low"}
        elif r_effort == "max":
            kwargs_dict["reasoning"] = {"effort": "high"}
        else:
            kwargs_dict["reasoning"] = {"effort": r_effort}

        # 4.b.1) SET: auto-summarization for OpenAI reasoning effort
        if provider == "openai" and r_effort != "disabled":
            kwargs_dict["reasoning"]["summary"] = "auto"

    # 4.b) SET: max_tokens for Google reasoning effort
    elif provider == "google" and is_reasoning_model(model_name):
        kwargs_dict["max_tokens"] = random.choice(max_tokens)
        think_bool = r_effort != "disabled"
        if think_bool:
            t = THINKING_TOKENS[r_effort]
            thinking_tokens = t if t < kwargs_dict["max_tokens"] else 1024
            kwargs_dict["thinking_budget"] = thinking_tokens
        else:
            if api_model_name in ("gemini-2.5-pro", "gemini-3-pro-preview"):
                kwargs_dict["thinking_budget"] = 128
            else:
                kwargs_dict["thinking_budget"] = 0

    # 4.c) SET: max_tokens for Anthropic or Bedrock reasoning effort
    elif provider in ("anthropic", "bedrock") and is_reasoning_model(model_name):
        kwargs_dict["max_tokens"] = min(random.choice(max_tokens), 64000)
        think_bool = r_effort != "disabled"
        if think_bool:
            # filter thinking tokens to be smaller than max_tokens
            # not auto THINKING_TOKENS
            t = THINKING_TOKENS[r_effort]
            thinking_tokens = t if t < kwargs_dict["max_tokens"] else 1024
            # sample only from thinking tokens that are valid
            kwargs_dict["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_tokens,
            }

    # 4.d) SET: max_tokens and thinking mode for DeepSeek reasoning models
    elif provider == "deepseek" and is_reasoning_model(model_name):
        kwargs_dict["max_tokens"] = random.choice(max_tokens)
        if r_effort == "disabled":
            kwargs_dict["extra_body"] = {"thinking": {"type": "disabled"}}
        else:
            kwargs_dict["reasoning_effort"] = "max" if r_effort == "max" else "high"
            kwargs_dict["extra_body"] = {"thinking": {"type": "enabled"}}

    # 4.e) SET: max_tokens for all other models
    else:
        # Non-reasoning models or other providers
        if provider in ("anthropic", "bedrock", "deepseek", "local_openai"):
            kwargs_dict["max_tokens"] = random.choice(max_tokens)
        else:
            kwargs_dict["max_output_tokens"] = random.choice(max_tokens)

    return kwargs_dict
