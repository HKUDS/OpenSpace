"""Exceptions for the process sandbox runtime."""

from __future__ import annotations


class SandboxError(RuntimeError):
    """Base class for local process sandbox failures."""


class SandboxUnavailableError(SandboxError):
    """Sandbox was requested but cannot be applied on this platform/config."""


class SandboxDependencyError(SandboxUnavailableError):
    """Required sandbox dependency is missing or unusable."""


class SandboxPolicyError(SandboxError):
    """Sandbox policy could not be converted into a platform profile."""


__all__ = [
    "SandboxDependencyError",
    "SandboxError",
    "SandboxPolicyError",
    "SandboxUnavailableError",
]
