from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True, frozen=True)
class ExecutionRequest:
    """Normalized request passed into the runtime orchestration layer."""

    prompt: str
    context: Mapping[str, Any] = field(default_factory=dict)
    workspace_dir: str | Path | None = None
    session_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    max_iterations: int | None = None
    task_id: str | None = None
    capture_skill_dir: str | None = None
    resume: bool = False
    abort_event: asyncio.Event | None = None


@dataclass(slots=True, frozen=True)
class ExecutionResult:
    """Normalized result returned by runtime orchestration."""

    text: str = ""
    status: str = "unknown"
    error: Any = None
    task_id: str | None = None
    session_id: str | None = None
    execution_time: float = 0.0
    iterations: int = 0
    tool_executions: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    skills_used: Sequence[Any] = field(default_factory=tuple)
    evolved_skills: Sequence[Any] = field(default_factory=tuple)
    active_skills: Sequence[Any] = field(default_factory=tuple)
    permission_mode: str | None = None
    session_capability_state: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ExecutionResult":
        content = payload.get("content")
        if not isinstance(content, str):
            content = payload.get("response")
        text = content if isinstance(content, str) else str(content or "")
        status = payload.get("status")
        session_id = payload.get("session_id")
        task_id = payload.get("task_id")
        metadata = payload.get("metadata")
        tool_executions = payload.get("tool_executions")
        skills_used = payload.get("skills_used")
        evolved_skills = payload.get("evolved_skills")
        active_skills = payload.get("active_skills")
        permission_mode = payload.get("permission_mode")
        iterations = payload.get("iterations")
        execution_time = payload.get("execution_time")
        return cls(
            text=text,
            status=status if isinstance(status, str) else "unknown",
            error=payload.get("error"),
            task_id=task_id if isinstance(task_id, str) else None,
            session_id=session_id if isinstance(session_id, str) else None,
            execution_time=(
                float(execution_time)
                if isinstance(execution_time, (int, float))
                else 0.0
            ),
            iterations=iterations if isinstance(iterations, int) else 0,
            tool_executions=(
                tuple(tool_executions)
                if isinstance(tool_executions, Sequence)
                and not isinstance(tool_executions, (str, bytes))
                else ()
            ),
            skills_used=(
                tuple(skills_used)
                if isinstance(skills_used, Sequence)
                and not isinstance(skills_used, (str, bytes))
                else ()
            ),
            evolved_skills=(
                tuple(evolved_skills)
                if isinstance(evolved_skills, Sequence)
                and not isinstance(evolved_skills, (str, bytes))
                else ()
            ),
            active_skills=(
                tuple(active_skills)
                if isinstance(active_skills, Sequence)
                and not isinstance(active_skills, (str, bytes))
                else ()
            ),
            permission_mode=permission_mode if isinstance(permission_mode, str) else None,
            session_capability_state=payload.get("session_capability_state"),
            metadata=metadata if isinstance(metadata, Mapping) else {},
        )
