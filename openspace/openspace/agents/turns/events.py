"""Runtime event payload builders for GroundingAgent turns."""

from __future__ import annotations

from typing import Any


def agent_start_payload(
    agent: Any,
    context: dict[str, Any],
    *,
    agent_id: str,
    instruction: str,
) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "name": agent.name,
        "status": "running",
        "model": getattr(agent._llm_client, "model", None),
        "task_id": context.get("task_id"),
        "session_id": context.get("session_id"),
        "summary": instruction[:160],
        "metadata": {
            "max_iterations": context.get(
                "max_iterations",
                agent._max_iterations,
            ),
        },
    }


def agent_progress_payload(
    agent: Any,
    context: dict[str, Any],
    *,
    agent_id: str,
    iteration: int,
    max_iterations: int,
) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "name": agent.name,
        "status": "running",
        "task_id": context.get("task_id"),
        "session_id": context.get("session_id"),
        "summary": f"Iteration {iteration}/{max_iterations}",
        "metadata": {"iteration": iteration, "max_iterations": max_iterations},
    }


def agent_output_payload(
    agent: Any,
    context: dict[str, Any],
    *,
    agent_id: str,
    content: Any,
    iteration: int,
    tool_calls_count: int,
) -> dict[str, Any]:
    text = content if isinstance(content, str) else str(content)
    return {
        "agent_id": agent_id,
        "name": agent.name,
        "status": "running",
        "task_id": context.get("task_id"),
        "session_id": context.get("session_id"),
        "content": text,
        "role": "assistant",
        "summary": text[:160],
        "metadata": {
            "iteration": iteration,
            "tool_calls_count": tool_calls_count,
        },
    }


def agent_error_payload(
    agent: Any,
    context: dict[str, Any],
    *,
    agent_id: str,
    error: Any,
) -> dict[str, Any]:
    text = str(error)
    return {
        "agent_id": agent_id,
        "name": agent.name,
        "status": "error",
        "task_id": context.get("task_id"),
        "session_id": context.get("session_id"),
        "content": text,
        "role": "system",
        "summary": text[:160],
    }


def agent_complete_payload(
    agent: Any,
    context: dict[str, Any],
    *,
    agent_id: str,
    result: dict[str, Any],
    iterations: int,
    tool_calls: int,
) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "name": agent.name,
        "status": result.get("status", "completed"),
        "task_id": context.get("task_id"),
        "session_id": context.get("session_id"),
        "summary": str(
            result.get("response") or result.get("status") or "completed"
        )[:160],
        "metadata": {"iterations": iterations, "tool_calls": tool_calls},
    }


def iteration_start_payload(iteration: int, max_iterations: int) -> dict[str, int]:
    return {"iteration": iteration, "max_iterations": max_iterations}


def status_update_payload(
    *,
    total_iterations: int,
    total_tool_calls: int,
) -> dict[str, Any]:
    return {
        "phase": "building_result",
        "total_iterations": total_iterations,
        "total_tool_calls": total_tool_calls,
    }


def token_warning_payload(
    *,
    token_count: int,
    model: str,
    token_warning: Any,
) -> dict[str, Any]:
    return {
        "token_count": token_count,
        "model": model,
        "percent_left": token_warning.percent_left,
        "is_above_warning_threshold": (
            token_warning.is_above_warning_threshold
        ),
        "is_above_error_threshold": token_warning.is_above_error_threshold,
        "is_above_auto_compact_threshold": (
            token_warning.is_above_auto_compact_threshold
        ),
        "is_at_blocking_limit": token_warning.is_at_blocking_limit,
    }
