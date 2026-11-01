"""Strict provider assembly for AgentPilot methods.

The main AgentPilot package owns provider transports and accounting semantics.
This module only resolves name-only credential configuration against an
explicit host environment and constructs the corresponding main-package
``ProviderRegistry``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Mapping

from agentpilot.app import AppConfig, ProviderRegistry
from agentpilot.domain import PolicyMethod
from agentpilot.providers import (
    AnthropicAdapter,
    EmbeddingAccountingReconciler,
    GeminiAdapter,
    GenerationAccountingReconciler,
    OpenAIAdapter,
    OpenAIEmbeddingAdapter,
    OpenRouterAdapter,
    OpenRouterEmbeddingAdapter,
    OpenRouterEmbeddingAnalyticsLookup,
    OpenRouterGenerationLookup,
)

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_SUPPORTED_KINDS = frozenset({"openrouter", "openai", "anthropic", "gemini"})
_EMBEDDING_KINDS = frozenset({"openrouter", "openai"})
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
# An in-process wait for a late accounting row must stay far below the
# campaign's reconciliation deadline, or the batch would hold its lease past the
# point where the supervisor is entitled to quarantine the receipt.
_MAX_ACCOUNTING_GRACE_SECONDS = 300.0

ClientFactory = Callable[..., object]


class ProviderRuntimeError(RuntimeError):
    """Provider configuration or construction cannot safely continue."""


@dataclass(frozen=True, slots=True)
class ProviderCredentialBinding:
    """A canonical provider kind and environment-variable names only."""

    name: str
    kind: str
    api_key_env: str
    management_key_env: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderRuntimeSettings:
    providers: tuple[ProviderCredentialBinding, ...]
    accounting_lookup_grace_seconds: float = 0.0
    request_timeout_seconds: float = 1200.0

    def by_name(self) -> dict[str, ProviderCredentialBinding]:
        return {binding.name: binding for binding in self.providers}


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderRuntimeError(f"{field_name} must be a table")
    return value


def _only(value: Mapping[str, Any], allowed: set[str], field_name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ProviderRuntimeError(f"unknown {field_name} keys: {sorted(unknown)}")


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderRuntimeError(f"{field_name} must be non-empty text")
    if value != value.strip():
        raise ProviderRuntimeError(f"{field_name} must not contain outer whitespace")
    return value


def _grace_seconds(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderRuntimeError(f"{field_name} must be a number of seconds")
    seconds = float(value)
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        raise ProviderRuntimeError(f"{field_name} must be finite")
    if seconds < 0 or seconds > _MAX_ACCOUNTING_GRACE_SECONDS:
        raise ProviderRuntimeError(
            f"{field_name} must be between 0 and {_MAX_ACCOUNTING_GRACE_SECONDS}"
        )
    return seconds


def _env_name(value: Any, field_name: str) -> str:
    name = _text(value, field_name)
    if _ENV_NAME.fullmatch(name) is None:
        raise ProviderRuntimeError(
            f"{field_name} must be an uppercase environment-variable name"
        )
    return name


def parse_provider_runtime_settings(
    value: Mapping[str, Any],
) -> ProviderRuntimeSettings:
    """Parse a strict provider table that cannot contain credential values."""

    table = _mapping(value, "provider_runtime")
    _only(
        table,
        {"providers", "accounting_lookup_grace_seconds", "request_timeout_seconds"},
        "provider_runtime",
    )
    missing = {"providers"} - set(table)
    if missing:
        raise ProviderRuntimeError(f"missing provider_runtime keys: {sorted(missing)}")
    grace_seconds = _grace_seconds(
        table.get("accounting_lookup_grace_seconds", 0.0),
        "provider_runtime.accounting_lookup_grace_seconds",
    )
    timeout = table.get("request_timeout_seconds", 1200.0)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < timeout <= 86400
    ):
        raise ProviderRuntimeError(
            "provider_runtime.request_timeout_seconds must be in (0, 86400]"
        )
    raw_providers = _mapping(table["providers"], "provider_runtime.providers")
    if not raw_providers:
        raise ProviderRuntimeError("provider_runtime.providers must not be empty")

    bindings: list[ProviderCredentialBinding] = []
    for raw_name, raw_binding in raw_providers.items():
        name = _text(raw_name, "provider_runtime.providers provider name")
        binding = _mapping(
            raw_binding,
            f"provider_runtime.providers.{name}",
        )
        _only(
            binding,
            {"kind", "api_key_env", "management_key_env"},
            f"provider_runtime.providers.{name}",
        )
        missing_binding = {"kind", "api_key_env"} - set(binding)
        if missing_binding:
            raise ProviderRuntimeError(
                f"missing provider_runtime.providers.{name} keys: "
                f"{sorted(missing_binding)}"
            )
        kind = _text(binding["kind"], f"provider_runtime.providers.{name}.kind")
        if kind not in _SUPPORTED_KINDS:
            raise ProviderRuntimeError(f"provider kind {kind!r} is unsupported")
        # Main ProviderRegistry intentionally keys providers by provider_name.
        # Aliasing here would make durable call identities ambiguous.
        if name != kind:
            raise ProviderRuntimeError(
                f"provider name {name!r} must equal canonical kind {kind!r}"
            )
        api_key_env = _env_name(
            binding["api_key_env"],
            f"provider_runtime.providers.{name}.api_key_env",
        )
        management_key_env = None
        if "management_key_env" in binding:
            management_key_env = _env_name(
                binding["management_key_env"],
                f"provider_runtime.providers.{name}.management_key_env",
            )
            if kind != "openrouter":
                raise ProviderRuntimeError(
                    "management_key_env is supported only for openrouter"
                )
            if management_key_env == api_key_env:
                raise ProviderRuntimeError(
                    "OpenRouter inference and management environment names "
                    "must be distinct"
                )
        bindings.append(
            ProviderCredentialBinding(
                name=name,
                kind=kind,
                api_key_env=api_key_env,
                management_key_env=management_key_env,
            )
        )
    return ProviderRuntimeSettings(
        providers=tuple(sorted(bindings, key=lambda item: item.name)),
        accounting_lookup_grace_seconds=grace_seconds,
        request_timeout_seconds=float(timeout),
    )


def _required_provider_names(config: AppConfig) -> tuple[set[str], set[str]]:
    if config.policy.method is PolicyMethod.MEMORY:
        memory = config.policy.memory
        if memory is None:
            raise ProviderRuntimeError("Memory policy config is incomplete")
        return (
            {
                memory.proposer.provider,
                memory.ranker.provider,
                memory.summarizer.provider,
            },
            set(),
        )
    ucb = config.policy.ucb
    if ucb is None:
        raise ProviderRuntimeError("UCB policy config is incomplete")
    return {ucb.proposer.provider}, {ucb.semantic.provider}


def _credential(environment: Mapping[str, str], env_name: str, *, role: str) -> str:
    try:
        value = environment[env_name]
    except (KeyError, TypeError):
        raise ProviderRuntimeError(
            f"{role} credential environment variable {env_name!r} is missing"
        ) from None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProviderRuntimeError(
            f"{role} credential environment variable {env_name!r} is invalid"
        )
    return value


def _openrouter_client(*, api_key: str, timeout_seconds: float = 1200.0) -> object:
    try:
        from openai import OpenAI
    except (ImportError, ModuleNotFoundError):
        raise ProviderRuntimeError(
            "OpenRouter client SDK is unavailable; install the OpenAI optional extra"
        ) from None
    try:
        return OpenAI(api_key=api_key, base_url=_OPENROUTER_BASE_URL,
                      timeout=timeout_seconds, max_retries=0)
    except Exception:
        raise ProviderRuntimeError("OpenRouter client construction failed") from None


def _openai_client(*, api_key: str, timeout_seconds: float = 1200.0) -> object:
    try:
        from openai import OpenAI
    except (ImportError, ModuleNotFoundError):
        raise ProviderRuntimeError(
            "OpenAI client SDK is unavailable; install the OpenAI optional extra"
        ) from None
    try:
        return OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)
    except Exception:
        raise ProviderRuntimeError("OpenAI client construction failed") from None


def _anthropic_client(*, api_key: str, timeout_seconds: float = 1200.0) -> object:
    try:
        from anthropic import Anthropic
    except (ImportError, ModuleNotFoundError):
        raise ProviderRuntimeError(
            "Anthropic client SDK is unavailable; install the Anthropic optional extra"
        ) from None
    try:
        return Anthropic(api_key=api_key, timeout=timeout_seconds, max_retries=0)
    except Exception:
        raise ProviderRuntimeError("Anthropic client construction failed") from None


def _gemini_client(*, api_key: str, timeout_seconds: float = 1200.0) -> object:
    try:
        from google import genai
    except (ImportError, ModuleNotFoundError):
        raise ProviderRuntimeError(
            "Gemini client SDK is unavailable; install the Gemini optional extra"
        ) from None
    try:
        return genai.Client(api_key=api_key,
                            http_options={"timeout": int(timeout_seconds * 1000)})
    except Exception:
        raise ProviderRuntimeError("Gemini client construction failed") from None


def _default_client_factories(timeout_seconds: float = 1200.0) -> dict[str, ClientFactory]:
    factories = {
        "openrouter": _openrouter_client,
        "openai": _openai_client,
        "anthropic": _anthropic_client,
        "gemini": _gemini_client,
    }
    # A paid dispatch must correspond to exactly one SDK attempt. SDK retries
    # otherwise bypass the durable receipt and can silently double-spend.
    return {name: partial(factory, timeout_seconds=timeout_seconds)
            for name, factory in factories.items()}


def _construct_client(factory: ClientFactory, *, kind: str, api_key: str) -> object:
    if not callable(factory):
        raise ProviderRuntimeError(f"client factory for {kind!r} is not callable")
    try:
        return factory(api_key=api_key)
    except ProviderRuntimeError:
        raise
    except Exception:
        # A vendor constructor or injected factory can include the credential in
        # its exception. Never relay that detail across this boundary.
        raise ProviderRuntimeError(f"{kind} client construction failed") from None


def _completion_adapter(kind: str, client: object):
    # No price book is supplied. The main adapters therefore reject calls that
    # return neither provider-attested dollars nor another known cost source.
    if kind == "openrouter":
        # Some OpenRouter endpoints require reasoning even for structured
        # policy calls. The local schema parser remains the authority for the
        # returned proposal, ranking, or summary.
        return OpenRouterAdapter(client)
    if kind == "openai":
        return OpenAIAdapter(client)
    if kind == "anthropic":
        return AnthropicAdapter(client)
    if kind == "gemini":
        return GeminiAdapter(client)
    raise ProviderRuntimeError(f"provider kind {kind!r} is unsupported")


def _embedding_adapter(kind: str, client: object):
    if kind == "openrouter":
        # OpenRouter embedding responses need not include inline dollars. The
        # independent Analytics reconciler settles the durable receipt later.
        return OpenRouterEmbeddingAdapter(client)
    if kind == "openai":
        return OpenAIEmbeddingAdapter(client)
    raise ProviderRuntimeError(
        f"provider kind {kind!r} does not support semantic embeddings"
    )


def build_provider_registry(
    config: AppConfig,
    settings: ProviderRuntimeSettings,
    *,
    environment: Mapping[str, str],
    client_factories: Mapping[str, ClientFactory] | None = None,
    embeddings_only: bool = False,
) -> ProviderRegistry:
    """Build main-package providers without reading ambient process state.

    ``environment`` must be the explicit host mapping chosen by the CLI or
    caller. Credential values are never copied into config, identities, or
    diagnostics.
    """

    if not isinstance(config, AppConfig):
        raise TypeError("config must be an AgentPilot AppConfig")
    if not isinstance(settings, ProviderRuntimeSettings):
        raise TypeError("settings must be ProviderRuntimeSettings")
    if not isinstance(environment, Mapping):
        raise TypeError("environment must be an explicit mapping")
    completion_names, embedding_names = _required_provider_names(config)
    if embeddings_only:
        completion_names = set()
    required_names = completion_names | embedding_names
    bindings = settings.by_name()
    if embeddings_only:
        bindings = {name: binding for name, binding in bindings.items() if name in embedding_names}
    if set(bindings) != required_names:
        missing = sorted(required_names - set(bindings))
        unused = sorted(set(bindings) - required_names)
        detail = []
        if missing:
            detail.append(f"missing={missing}")
        if unused:
            detail.append(f"unused={unused}")
        raise ProviderRuntimeError(
            "provider bindings must exactly match policy providers: "
            + ", ".join(detail)
        )

    factories = _default_client_factories(settings.request_timeout_seconds)
    if client_factories is not None:
        if not isinstance(client_factories, Mapping):
            raise TypeError("client_factories must be a mapping")
        unknown_factories = set(client_factories) - _SUPPORTED_KINDS
        if unknown_factories:
            raise ProviderRuntimeError(
                f"unknown client factory kinds: {sorted(unknown_factories)}"
            )
        factories.update(client_factories)

    clients: dict[str, object] = {}
    inference_keys: dict[str, str] = {}
    management_keys: dict[str, str] = {}
    for name in sorted(required_names):
        binding = bindings[name]
        if name in embedding_names and binding.kind not in _EMBEDDING_KINDS:
            raise ProviderRuntimeError(
                f"provider kind {binding.kind!r} does not support semantic embeddings"
            )
        api_key = _credential(
            environment,
            binding.api_key_env,
            role=f"{binding.kind} inference",
        )
        inference_keys[name] = api_key
        if binding.management_key_env is not None:
            management_key = _credential(
                environment,
                binding.management_key_env,
                role=f"{binding.kind} management",
            )
            if management_key == api_key:
                raise ProviderRuntimeError(
                    "OpenRouter inference and management credentials must be distinct"
                )
            management_keys[name] = management_key
        if name in embedding_names and binding.kind == "openrouter":
            if binding.management_key_env is None:
                raise ProviderRuntimeError(
                    "OpenRouter semantic embeddings require management_key_env"
                )
        clients[name] = _construct_client(
            factories[binding.kind],
            kind=binding.kind,
            api_key=api_key,
        )

    completions = {
        name: _completion_adapter(bindings[name].kind, clients[name])
        for name in completion_names
    }
    embeddings = {
        name: _embedding_adapter(bindings[name].kind, clients[name])
        for name in embedding_names
    }
    completion_reconcilers = {}
    if "openrouter" in completion_names:
        completion_reconcilers["openrouter"] = GenerationAccountingReconciler(
            OpenRouterGenerationLookup(api_key=inference_keys["openrouter"]),
            pending_grace_seconds=settings.accounting_lookup_grace_seconds,
        )
    embedding_reconcilers = {}
    if "openrouter" in embedding_names:
        ucb = config.policy.ucb
        assert ucb is not None
        embedding_reconcilers["openrouter"] = EmbeddingAccountingReconciler(
            OpenRouterEmbeddingAnalyticsLookup(
                management_key=management_keys["openrouter"],
                model=ucb.semantic.model,
            )
        )

    return ProviderRegistry(
        completions=completions,
        embeddings=embeddings,
        completion_accounting_reconcilers=completion_reconcilers,
        embedding_accounting_reconcilers=embedding_reconcilers,
    )


__all__ = [
    "ProviderCredentialBinding",
    "ProviderRuntimeError",
    "ProviderRuntimeSettings",
    "build_provider_registry",
    "parse_provider_runtime_settings",
]
