from dataclasses import dataclass
import os
from typing import Any, Optional, Tuple

from rsi_arena.baselines.shinka.runtime.azure_openai_config import (
    azure_api_version,
    azure_openai_api_key,
    azure_resource_endpoint,
)
from rsi_arena.baselines.shinka.runtime.google_genai import _google_genai_timeout_ms, build_google_genai_client
from rsi_arena.baselines.shinka.runtime.local_openai_config import (
    parse_local_openai_model,
    resolve_local_openai_api_key,
)

from .providers.pricing import get_provider

TIMEOUT = 600
_OPENROUTER_PREFIX = "openrouter/"


@dataclass(frozen=True)
class ResolvedEmbeddingModel:
    original_model_name: str
    api_model_name: str
    provider: str
    base_url: Optional[str] = None
    api_key_env_name: Optional[str] = None


def resolve_embedding_backend(model_name: str) -> ResolvedEmbeddingModel:
    """Resolve runtime backend info for embedding model identifiers."""
    provider = get_provider(model_name)
    if provider == "azure":
        api_model_name = model_name.split("azure-", 1)[-1]
        return ResolvedEmbeddingModel(
            original_model_name=model_name,
            api_model_name=api_model_name,
            provider=provider,
            base_url=None,
        )
    if provider is not None:
        return ResolvedEmbeddingModel(
            original_model_name=model_name,
            api_model_name=model_name,
            provider=provider,
            base_url=None,
        )
    if model_name.startswith(_OPENROUTER_PREFIX):
        api_model_name = model_name.split(_OPENROUTER_PREFIX, 1)[-1]
        if not api_model_name:
            raise ValueError(
                "OpenRouter embedding model is missing after 'openrouter/'."
            )
        return ResolvedEmbeddingModel(
            original_model_name=model_name,
            api_model_name=api_model_name,
            provider="openrouter",
            base_url=None,
        )

    local_match = parse_local_openai_model(model_name)
    if local_match:
        return ResolvedEmbeddingModel(
            original_model_name=model_name,
            api_model_name=local_match.api_model_name,
            provider="local_openai",
            base_url=local_match.base_url,
            api_key_env_name=local_match.api_key_env_name,
        )

    raise ValueError(
        f"Embedding model {model_name} not supported. "
        "Use a model from the refreshed pricing catalog, 'openrouter/<model>', "
        "or 'local/<model>@http(s)://host[:port]/v1'."
    )


def get_client_embed(model_name: str) -> Tuple[Any, str]:
    """Get the client and model for the given embedding model name."""
    resolved = resolve_embedding_backend(model_name)
    provider = resolved.provider

    if provider == "openai":
        import openai

        client = openai.OpenAI(timeout=TIMEOUT)
    elif provider == "azure":
        import openai

        client = openai.AzureOpenAI(
            api_key=azure_openai_api_key(),
            api_version=azure_api_version(),
            azure_endpoint=azure_resource_endpoint(),
            timeout=TIMEOUT,
        )
    elif provider == "google":
        client = build_google_genai_client(timeout_ms=_google_genai_timeout_ms(TIMEOUT))
    elif provider == "openrouter":
        import openai

        client = openai.OpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url="https://openrouter.ai/api/v1",
            timeout=TIMEOUT,
        )
    elif provider == "local_openai":
        import openai

        client = openai.OpenAI(
            api_key=resolve_local_openai_api_key(resolved.api_key_env_name),
            base_url=resolved.base_url,
            timeout=TIMEOUT,
        )
    else:
        raise ValueError(f"Embedding model {model_name} not supported.")

    return client, resolved.api_model_name


def get_async_client_embed(model_name: str) -> Tuple[Any, str]:
    """Get the async client and model for the given embedding model name."""
    resolved = resolve_embedding_backend(model_name)
    provider = resolved.provider

    if provider == "openai":
        import openai

        client = openai.AsyncOpenAI()
    elif provider == "azure":
        import openai

        client = openai.AsyncAzureOpenAI(
            api_key=azure_openai_api_key(),
            api_version=azure_api_version(),
            azure_endpoint=azure_resource_endpoint(),
        )
    elif provider == "google":
        # Gemini doesn't have async client yet, will use thread pool in embedding.py
        client = build_google_genai_client(timeout_ms=_google_genai_timeout_ms(TIMEOUT))
    elif provider == "openrouter":
        import openai

        client = openai.AsyncOpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url="https://openrouter.ai/api/v1",
            timeout=TIMEOUT,
        )
    elif provider == "local_openai":
        import openai

        client = openai.AsyncOpenAI(
            api_key=resolve_local_openai_api_key(resolved.api_key_env_name),
            base_url=resolved.base_url,
            timeout=TIMEOUT,
        )
    else:
        raise ValueError(f"Embedding model {model_name} not supported.")

    return client, resolved.api_model_name
