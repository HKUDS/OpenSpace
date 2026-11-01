from importlib import import_module as _import_module
from typing import TYPE_CHECKING as _TYPE_CHECKING

from .errors import (
    ModelNotAvailableError,
    PromptTooLongError,
)
from .effort import (
    EffortConfig,
    EffortLevel,
    get_effort_config,
    resolve_applied_effort,
)
from .thinking import (
    ThinkingConfig,
    build_thinking_request_params,
    get_thinking_config,
    supports_thinking,
)

if _TYPE_CHECKING:
    from .client import LLMClient as LLMClient

__all__ = [
    "LLMClient",
    "PromptTooLongError",
    "ModelNotAvailableError",
    "EffortConfig",
    "EffortLevel",
    "get_effort_config",
    "resolve_applied_effort",
    "ThinkingConfig",
    "build_thinking_request_params",
    "get_thinking_config",
    "supports_thinking",
]


def __getattr__(name: str):
    if name != "LLMClient":
        raise AttributeError(f"module 'openspace.llm' has no attribute '{name}'")

    value = _import_module("openspace.llm.client").LLMClient
    globals()[name] = value
    return value
