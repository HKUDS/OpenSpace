"""Resolve policy access before dispatch, preserving explicit API credentials."""

from dataclasses import dataclass, replace

from .worker_settings import WorkerRuntimeSettings
from .worker_completion import worker_model


@dataclass(frozen=True)
class CompletionRoute:
    access: str
    provider: str
    model: str
    api_key_env: str | None = None

    def configure(self, config):
        return replace(config, provider=self.provider, model=self.model)


def resolve_completion_route(config, *, worker, providers, environment,
                             fallback_key_env, access="auto"):
    if access not in {"auto", "api", "worker"}:
        raise ValueError("completion access must be auto, api, or worker")
    binding = providers.by_name().get(config.provider) if providers is not None else None
    key_env = binding.api_key_env if binding else (
        fallback_key_env if config.provider == "openrouter" else None)
    key = environment.get(key_env) if key_env else None
    if access != "worker" and key:
        if not isinstance(key, str) or key != key.strip():
            raise ValueError(f"Invalid configured provider environment variable {key_env}")
        if config.provider != "openrouter":
            raise ValueError("This research entrypoint supports OpenRouter API calls; use worker access for a CLI provider")
        return CompletionRoute("api", config.provider, config.model, key_env)
    if access == "api":
        raise ValueError(f"Missing configured provider environment variable {key_env or config.provider}")
    if not isinstance(worker, WorkerRuntimeSettings):
        raise ValueError("No proposer API credential is available; configure an external [worker] to reuse its login")
    return CompletionRoute("worker", "worker:" + worker.backend, worker_model(worker))
