from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_LOCAL_MODEL_PATTERN = re.compile(r"^local/(?P<model>[^@]+)@(?P<url>https?://.+)$")
_ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_API_KEY_ENV_QUERY_PARAM = "api_key_env"


@dataclass(frozen=True)
class ResolvedLocalOpenAIModel:
    original_model_name: str
    api_model_name: str
    base_url: str
    api_key_env_name: Optional[str] = None


def parse_local_openai_model(
    model_name: str,
) -> Optional[ResolvedLocalOpenAIModel]:
    """Parse dynamic local OpenAI-compatible model identifiers."""
    local_match = _LOCAL_MODEL_PATTERN.match(model_name)
    if not local_match:
        return None

    api_model_name = local_match.group("model")
    raw_base_url = local_match.group("url")
    parsed = urlparse(raw_base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"Invalid local model URL '{raw_base_url}'. Expected http(s)://host[:port]/..."
        )

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    api_key_env_values = [
        value for key, value in query_pairs if key == _API_KEY_ENV_QUERY_PARAM
    ]
    if len(api_key_env_values) > 1:
        raise ValueError(
            "Local model URL may only specify one 'api_key_env' query parameter."
        )

    api_key_env_name = None
    if api_key_env_values:
        api_key_env_name = api_key_env_values[0].strip()
        if not api_key_env_name:
            raise ValueError("Local model URL 'api_key_env' must not be empty.")
        if not _ENV_VAR_NAME_PATTERN.match(api_key_env_name):
            raise ValueError(
                f"Invalid api_key_env '{api_key_env_name}'. "
                "Expected an environment variable name like CUSTOM_API_KEY."
            )

    sanitized_query_pairs = [
        (key, value) for key, value in query_pairs if key != _API_KEY_ENV_QUERY_PARAM
    ]
    base_url = urlunparse(
        parsed._replace(query=urlencode(sanitized_query_pairs, doseq=True))
    )

    return ResolvedLocalOpenAIModel(
        original_model_name=model_name,
        api_model_name=api_model_name,
        base_url=base_url,
        api_key_env_name=api_key_env_name,
    )


def resolve_local_openai_api_key(api_key_env_name: Optional[str] = None) -> str:
    """Resolve the API key for a local OpenAI-compatible backend."""
    if api_key_env_name:
        api_key = os.getenv(api_key_env_name, "").strip()
        if not api_key:
            raise ValueError(
                f"Local model api_key_env references '{api_key_env_name}', "
                f"but '{api_key_env_name}' is not set."
            )
        return api_key

    return os.getenv("LOCAL_OPENAI_API_KEY", "local")
