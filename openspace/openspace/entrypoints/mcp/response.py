from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from openspace.runtime import ExecutionResult


def _get_result_value(result: Dict[str, Any] | ExecutionResult, key: str, default: Any = None) -> Any:
    if isinstance(result, ExecutionResult):
        mapping = {
            "status": result.status,
            "response": result.text,
            "error": result.error,
            "task_id": result.task_id,
            "session_id": result.session_id,
            "execution_time": result.execution_time,
            "iterations": result.iterations,
            "tool_executions": list(result.tool_executions),
            "skills_used": list(result.skills_used),
            "evolved_skills": list(result.evolved_skills),
            "active_skills": list(result.active_skills),
        }
        return mapping.get(key, default)
    return result.get(key, default)


def format_task_result(result: Dict[str, Any] | ExecutionResult) -> Dict[str, Any]:
    """Format an OpenSpace execution result for the public MCP tool response."""
    tool_execs = _get_result_value(result, "tool_executions", [])
    tool_summary = [
        {
            "tool": te.get("tool_name", te.get("tool", "")),
            "status": te.get("status", ""),
            "error": te.get("error", "")[:200] if te.get("error") else None,
        }
        for te in tool_execs[:20]
    ]

    output: Dict[str, Any] = {
        "status": _get_result_value(result, "status", "unknown"),
        "response": _get_result_value(result, "response", ""),
        "execution_time": round(_get_result_value(result, "execution_time", 0), 2),
        "iterations": _get_result_value(result, "iterations", 0),
        "skills_used": _get_result_value(result, "skills_used", []),
        "task_id": _get_result_value(result, "task_id", ""),
        "session_id": _get_result_value(result, "session_id", ""),
        "tool_call_count": len(tool_execs),
        "tool_summary": tool_summary,
    }

    raw_evolved = _get_result_value(result, "evolved_skills", [])
    if raw_evolved:
        formatted_evolved = []
        for evolved in raw_evolved:
            if not isinstance(evolved, dict):
                continue
            skill_path = evolved.get("path", "")
            skill_dir = str(Path(skill_path).parent) if skill_path else ""
            formatted_evolved.append(
                {
                    "skill_dir": skill_dir,
                    "name": evolved.get("name", ""),
                    "origin": evolved.get("origin", ""),
                    "change_summary": evolved.get("change_summary", ""),
                    "upload_ready": bool(skill_dir),
                }
            )
        output["evolved_skills"] = formatted_evolved
        names = [item["name"] for item in formatted_evolved if item.get("upload_ready")]
        if names:
            output["action_required"] = (
                f"OpenSpace auto-evolved {len(names)} skill(s): {', '.join(names)}. "
                "Upload with the default private visibility unless the user explicitly "
                "asks to share publicly. "
                "Tell the user what you evolved and what you uploaded."
            )

    return output


def json_ok(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def json_error(error: Any, **extra: Any) -> str:
    to_payload = getattr(error, "to_payload", None)
    if callable(to_payload):
        payload = dict(to_payload())
        if "message" in payload and "error" not in payload:
            payload["error"] = payload["message"]
        payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)
    return json.dumps({"error": str(error), **extra}, ensure_ascii=False)


def format_transport_task_response(transport: str, result: Dict[str, Any]) -> str:
    """Return the tool payload shape shared by stdio, SSE, and streamable HTTP."""
    if transport not in {"stdio", "sse", "streamable-http"}:
        raise ValueError(f"Unsupported MCP transport: {transport}")
    return json_ok(format_task_result(result))


_format_task_result = format_task_result
_json_ok = json_ok
_json_error = json_error
