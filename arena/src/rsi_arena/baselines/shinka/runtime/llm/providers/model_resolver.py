from dataclasses import dataclass
from typing import Optional

from rsi_arena.baselines.shinka.runtime.local_openai_config import parse_local_openai_model
from .pricing import get_provider

_OPENROUTER_PREFIX = "openrouter/"
_HEADLESS_PREFIX = "headless/"


@dataclass(frozen=True)
class ResolvedModel:
    original_model_name: str
    api_model_name: str
    provider: str
    base_url: Optional[str] = None
    api_key_env_name: Optional[str] = None


def resolve_model_backend(model_name: str) -> ResolvedModel:
    """Resolve runtime backend info for known and dynamic model identifiers."""
    if model_name.startswith(_HEADLESS_PREFIX):
        api_model_name = model_name
        agent = (
            model_name.split(_HEADLESS_PREFIX, 1)[-1].split("?", 1)[0].split("@", 1)[0]
        )
        if not agent:
            raise ValueError("Headless model name is missing after 'headless/' prefix.")
        return ResolvedModel(
            original_model_name=model_name,
            api_model_name=api_model_name,
            provider="headless",
            base_url=None,
        )

    if model_name.startswith(_OPENROUTER_PREFIX):
        api_model_name = model_name.split(_OPENROUTER_PREFIX, 1)[-1]
        if not api_model_name:
            raise ValueError("OpenRouter model name is missing after 'openrouter/'.")
        return ResolvedModel(
            original_model_name=model_name,
            api_model_name=api_model_name,
            provider="openrouter",
            base_url=None,
        )

    provider = get_provider(model_name)
    if provider is not None:
        return ResolvedModel(
            original_model_name=model_name,
            api_model_name=model_name,
            provider=provider,
            base_url=None,
        )

    if model_name.startswith("azure-"):
        api_model_name = model_name.split("azure-", 1)[-1]
        if not api_model_name:
            raise ValueError("Azure model name is missing after 'azure-' prefix.")
        return ResolvedModel(
            original_model_name=model_name,
            api_model_name=api_model_name,
            provider="azure_openai",
            base_url=None,
        )

    local_match = parse_local_openai_model(model_name)
    if local_match:
        return ResolvedModel(
            original_model_name=model_name,
            api_model_name=local_match.api_model_name,
            provider="local_openai",
            base_url=local_match.base_url,
            api_key_env_name=local_match.api_key_env_name,
        )

    raise ValueError(
        f"Model '{model_name}' is not supported. "
        "Use a model from the refreshed pricing catalog, 'openrouter/<model>', "
        "or 'local/<model>@http(s)://host[:port]/v1'."
    )
