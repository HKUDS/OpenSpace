"""Call-source context shared by product runtime and benchmark tracking.

This module only tags the current async context with the logical source of an
LLM call.  It does not collect usage, emit telemetry events, or replace the
anonymous statistics/event telemetry under :mod:`openspace.utils.telemetry`.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator


AGENT_SOURCE = "agent"

# Default to the main grounding loop so product code does not need to annotate
# ordinary agent calls.  Benchmark token accounting can then count this bucket
# separately from skill/analysis overhead.
CALL_SOURCE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "openspace_call_source",
    default=AGENT_SOURCE,
)


def set_call_source(source: str) -> contextvars.Token:
    """Set the current call source and return a token for reset."""
    return CALL_SOURCE.set(source)


def reset_call_source(token: contextvars.Token) -> None:
    """Reset the current call source to the value before ``set_call_source``."""
    CALL_SOURCE.reset(token)


@contextmanager
def call_source_ctx(source: str) -> Iterator[None]:
    """Temporarily set the call source inside a ``with`` block."""
    token = set_call_source(source)
    try:
        yield
    finally:
        reset_call_source(token)


def get_call_source(default: str = AGENT_SOURCE) -> str:
    """Return the current call source label."""
    return CALL_SOURCE.get(default)


__all__ = [
    "AGENT_SOURCE",
    "CALL_SOURCE",
    "call_source_ctx",
    "get_call_source",
    "reset_call_source",
    "set_call_source",
]
