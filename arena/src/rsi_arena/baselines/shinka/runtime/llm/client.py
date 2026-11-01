from typing import Any, Tuple
import os
from rsi_arena.baselines.shinka.runtime.azure_openai_config import azure_openai_api_key, azure_v1_base_url
from rsi_arena.baselines.shinka.runtime.google_genai import _google_genai_timeout_ms, build_google_genai_client
from rsi_arena.baselines.shinka.runtime.local_openai_config import resolve_local_openai_api_key
from .constants import OPENAI_MAX_RETRIES, TIMEOUT
from .providers.model_resolver import resolve_model_backend

def get_client_llm(
    model_name: str, structured_output: bool = False
) -> Tuple[Any, str, str]:
    """Get the client and model for the given model name.

    Args:
        model_name (str): The name of the model to get the client.

    Raises:
        ValueError: If the model is not supported.

    Returns:
        Tuple[Any, str, str]: (client, API model name, resolved provider).
    """
    resolved = resolve_model_backend(model_name)
    provider = resolved.provider
    api_model_name = resolved.api_model_name

    if provider == "anthropic":
        import anthropic

        client = anthropic.Anthropic(timeout=TIMEOUT)
        if structured_output:
            import instructor

            client = instructor.from_anthropic(
                client, mode=instructor.mode.Mode.ANTHROPIC_JSON
            )
    elif provider == "bedrock":
        import anthropic

        client = anthropic.AnthropicBedrock(
            aws_access_key=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            aws_region=os.getenv("AWS_REGION_NAME"),
            timeout=TIMEOUT,
        )
        if structured_output:
            import instructor

            client = instructor.from_anthropic(
                client, mode=instructor.mode.Mode.ANTHROPIC_JSON
            )
    elif provider == "openai":
        import openai

        client = openai.OpenAI(timeout=TIMEOUT, max_retries=OPENAI_MAX_RETRIES)
        if structured_output:
            import instructor

            client = instructor.from_openai(client, mode=instructor.Mode.TOOLS_STRICT)
    elif provider == "azure_openai":
        import openai

        client = openai.OpenAI(
            api_key=azure_openai_api_key(),
            base_url=azure_v1_base_url(),
            timeout=TIMEOUT,
            max_retries=OPENAI_MAX_RETRIES,
        )
        if structured_output:
            import instructor

            client = instructor.from_openai(client, mode=instructor.Mode.TOOLS_STRICT)
    elif provider == "deepseek":
        import openai

        client = openai.OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com",
            timeout=TIMEOUT,
            max_retries=OPENAI_MAX_RETRIES,
        )
        if structured_output:
            import instructor

            client = instructor.from_openai(client, mode=instructor.Mode.MD_JSON)
    elif provider == "google":
        client = build_google_genai_client(timeout_ms=_google_genai_timeout_ms(TIMEOUT))
        if structured_output:
            import instructor

            client = instructor.from_openai(
                client,
                mode=instructor.Mode.GEMINI_JSON,
            )
    elif provider == "openrouter":
        import openai

        client = openai.OpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url="https://openrouter.ai/api/v1",
            timeout=TIMEOUT,
            max_retries=OPENAI_MAX_RETRIES,
        )
        if structured_output:
            import instructor

            client = instructor.from_openai(client, mode=instructor.Mode.MD_JSON)
    elif provider == "local_openai":
        import openai

        client = openai.OpenAI(
            api_key=resolve_local_openai_api_key(resolved.api_key_env_name),
            base_url=resolved.base_url,
            timeout=TIMEOUT,
            max_retries=OPENAI_MAX_RETRIES,
        )
    elif provider == "headless":
        client = None
    else:
        raise ValueError(f"Model {model_name} not supported.")

    return client, api_model_name, provider


def get_async_client_llm(
    model_name: str, structured_output: bool = False
) -> Tuple[Any, str, str]:
    """Get the async client and model for the given model name.

    Args:
        model_name (str): The name of the model to get the client.

    Raises:
        ValueError: If the model is not supported.

    Returns:
        Tuple[Any, str, str]: (async client, API model name, resolved provider).
    """
    resolved = resolve_model_backend(model_name)
    provider = resolved.provider
    api_model_name = resolved.api_model_name

    if provider == "anthropic":
        import anthropic

        client = anthropic.AsyncAnthropic(timeout=TIMEOUT)
        if structured_output:
            import instructor

            client = instructor.from_anthropic(
                client, mode=instructor.mode.Mode.ANTHROPIC_JSON
            )
    elif provider == "bedrock":
        import anthropic

        client = anthropic.AsyncAnthropicBedrock(
            aws_access_key=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            aws_region=os.getenv("AWS_REGION_NAME"),
            timeout=TIMEOUT,
        )
        if structured_output:
            import instructor

            client = instructor.from_anthropic(
                client, mode=instructor.mode.Mode.ANTHROPIC_JSON
            )
    elif provider == "openai":
        import openai

        client = openai.AsyncOpenAI(timeout=TIMEOUT, max_retries=OPENAI_MAX_RETRIES)
        if structured_output:
            import instructor

            client = instructor.from_openai(client, mode=instructor.Mode.TOOLS_STRICT)
    elif provider == "azure_openai":
        import openai

        client = openai.AsyncOpenAI(
            api_key=azure_openai_api_key(),
            base_url=azure_v1_base_url(),
            timeout=TIMEOUT,
            max_retries=OPENAI_MAX_RETRIES,
        )
        if structured_output:
            import instructor

            client = instructor.from_openai(client, mode=instructor.Mode.TOOLS_STRICT)
    elif provider == "deepseek":
        import openai

        client = openai.AsyncOpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com",
            timeout=TIMEOUT,
            max_retries=OPENAI_MAX_RETRIES,
        )
        if structured_output:
            import instructor

            client = instructor.from_openai(client, mode=instructor.Mode.MD_JSON)
    elif provider == "google":
        client = build_google_genai_client(timeout_ms=_google_genai_timeout_ms(TIMEOUT))
        if structured_output:
            raise ValueError("Gemini does not support structured output.")
    elif provider == "openrouter":
        import openai

        client = openai.AsyncOpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url="https://openrouter.ai/api/v1",
            timeout=TIMEOUT,
            max_retries=OPENAI_MAX_RETRIES,
        )
        if structured_output:
            import instructor

            client = instructor.from_openai(client, mode=instructor.Mode.MD_JSON)
    elif provider == "local_openai":
        import openai

        client = openai.AsyncOpenAI(
            api_key=resolve_local_openai_api_key(resolved.api_key_env_name),
            base_url=resolved.base_url,
            timeout=TIMEOUT,
            max_retries=OPENAI_MAX_RETRIES,
        )
    elif provider == "headless":
        client = None
    else:
        raise ValueError(f"Model {model_name} not supported.")

    return client, api_model_name, provider
