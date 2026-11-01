"""Frozen local smoke AIME-style integration probe."""

# Persistent format and evaluation identifiers retain their historical values.

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from agentpilot_arena._canonical import canonical_digest

SMOKE_PROBE_TASK_ID = "aime-smoke-001"
SMOKE_PROBE_BENCHMARK = "aime-smoke"
SMOKE_PROBE_SCHEMA = "rsi-arena.openthink.aime-smoke-task"
SMOKE_PROBE_TASK_DIGEST = (
    "sha256:0c3f927a57e90f051901eacf3db404badd8b18e6e625a5ad3a5ca7faf01096c1"
)
SMOKE_PROBE_RESOURCE_DIGEST = (
    "sha256:7045328ea533d41e35971e497e5939495331fea49cb85f57320ada3b8576602c"
)
_EXPECTED_KEYS = {
    "task_id",
    "year",
    "contest",
    "number",
    "problem",
    "answer",
    "smoke_only",
    "synthetic",
    "formal_membership",
}


def load_smoke_probe_task() -> dict[str, Any]:
    resource = files("agentpilot_arena.aime").joinpath("data", "smoke_probe_task.json")
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("synthetic SMOKE_PROBE resource is unreadable") from exc
    if not isinstance(payload, list) or len(payload) != 1:
        raise ValueError("synthetic SMOKE_PROBE resource must contain exactly one task")
    task = payload[0]
    if not isinstance(task, dict) or set(task) != _EXPECTED_KEYS:
        raise ValueError("synthetic SMOKE_PROBE task schema is invalid")
    answer = task.get("answer")
    if (
        task.get("task_id") != SMOKE_PROBE_TASK_ID
        or task.get("smoke_only") is not True
        or task.get("synthetic") is not True
        or task.get("formal_membership") is not False
        or task.get("year") != 0
        or task.get("contest") != "SMOKE"
        or task.get("number") != 1
        or not isinstance(task.get("problem"), str)
        or not task["problem"].strip()
        or isinstance(answer, bool)
        or not isinstance(answer, int)
        or not 0 <= answer <= 999
    ):
        raise ValueError("synthetic SMOKE_PROBE task identity or content is invalid")
    if "sha256:" + canonical_digest(task) != SMOKE_PROBE_TASK_DIGEST:
        raise ValueError("synthetic SMOKE_PROBE task does not match its frozen digest")
    if "sha256:" + canonical_digest(payload) != SMOKE_PROBE_RESOURCE_DIGEST:
        raise ValueError(
            "synthetic SMOKE_PROBE resource does not match its frozen digest"
        )
    return dict(task)


def smoke_probe_identity() -> dict[str, object]:
    load_smoke_probe_task()
    return {
        "schema": SMOKE_PROBE_SCHEMA,
        "smoke_probe": True,
        "smoke_only": True,
        "formal_membership": False,
        "benchmark": SMOKE_PROBE_BENCHMARK,
        "task_id": SMOKE_PROBE_TASK_ID,
        "task_digest": SMOKE_PROBE_TASK_DIGEST,
        "resource_digest": SMOKE_PROBE_RESOURCE_DIGEST,
    }


__all__ = [
    "SMOKE_PROBE_BENCHMARK",
    "SMOKE_PROBE_RESOURCE_DIGEST",
    "SMOKE_PROBE_SCHEMA",
    "SMOKE_PROBE_TASK_DIGEST",
    "SMOKE_PROBE_TASK_ID",
    "load_smoke_probe_task",
    "smoke_probe_identity",
]
