"""Verifier dependency-cache environment wiring for Harbor benchmarks."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

VERIFIER_DEPENDENCY_CACHE_RUNTIME_KEY = "verifier_dependency_cache"
VERIFIER_DEPENDENCY_CACHE_ENV_NAMES = frozenset(
    {
        "NO_PROXY",
        "PIP_INDEX_URL",
        "PIP_TRUSTED_HOST",
        "UV_INDEX_URL",
        "UV_INSECURE_HOST",
        "UV_INSTALLER_GHE_BASE_URL",
        "UV_PYTHON_INSTALL_MIRROR",
        "http_proxy",
        "no_proxy",
    }
)


def verifier_dependency_cache_env(value: object) -> Mapping[str, str]:
    """Resolve the exact trusted runtime mapping used by both Harbor paths."""

    if not isinstance(value, Mapping):
        raise ValueError("verifier dependency cache environment is not an object")
    if not value:
        return MappingProxyType({})
    if set(value) != set(VERIFIER_DEPENDENCY_CACHE_ENV_NAMES):
        raise ValueError("verifier dependency cache environment fields are not exact")
    resolved = {str(key): value[key] for key in sorted(value)}
    if any(not isinstance(item, str) or not item.strip() for item in resolved.values()):
        raise ValueError("verifier dependency cache values must be non-empty strings")
    if resolved["NO_PROXY"] != resolved["no_proxy"]:
        raise ValueError("NO_PROXY and no_proxy must describe the same cache bypasses")
    return MappingProxyType(resolved)


def harbor_verifier_env_args(
    verifier_env: Mapping[str, str],
    *,
    task_env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Build one deterministic repeated ``--verifier-env`` argument sequence."""

    resolved = (
        dict(verifier_dependency_cache_env(verifier_env))
        if verifier_env
        else {}
    )
    resolved.update(task_env or {})
    return tuple(
        part
        for name, value in sorted(resolved.items())
        for part in ("--verifier-env", f"{name}={value}")
    )


__all__ = [
    "VERIFIER_DEPENDENCY_CACHE_ENV_NAMES",
    "VERIFIER_DEPENDENCY_CACHE_RUNTIME_KEY",
    "harbor_verifier_env_args",
    "verifier_dependency_cache_env",
]
