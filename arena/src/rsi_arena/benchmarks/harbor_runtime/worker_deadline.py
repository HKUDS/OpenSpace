"""Shared inner-worker deadline semantics for Harbor bridges."""

from __future__ import annotations

import asyncio
from typing import Any


async def wait_for_worker_deadline(process: Any, *, timeout_seconds: float) -> bool:
    """Return whether a worker reached its measurement deadline.

    A deadline is a measured terminal state, not a connector exception. Callers
    freeze the worker, preserve bounded accounting/artifacts, and allow Harbor's
    verifier to grade the state that existed when the allowance ended.
    """

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError:
        return True
    return False


__all__ = ["wait_for_worker_deadline"]
