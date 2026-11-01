"""Tool execution pipeline.

Runs one tool use end to end: input normalization, validation, permission
checks, hooks, execution, error classification, result formatting, and optional
large-output persistence. The agent loop receives a flat ``ToolCallResult`` so
the runtime can keep observability in ``event_sink`` rather than yielding
streaming UI objects from this layer.

Bash ``_simulatedSedEdit`` remains model-hidden but is accepted internally after
permission preview. PermissionDenied hooks are best-effort: hook failures never
change a deny result.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import jsonschema

from openspace.grounding.core.tool.base import BaseTool, PermissionCheckResult
from openspace.grounding.core.types import ToolResult, ToolStatus
from openspace.services.conversation.messages import (
    CANCEL_MESSAGE,
    INTERRUPT_MESSAGE_FOR_TOOL_USE,
    build_tool_result_message,
    build_tool_result_stop_message,
    extract_discovered_tool_names,
)
from openspace.services.conversation.content_blocks import (
    content_has_multimodal_block,
    content_text_size,
    extract_text_from_content,
    make_text_block,
)
from openspace.services.tooling.results import maybe_persist_large_result
from openspace.services.tooling.context import ToolUseContext, active_skill_scope_payload
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# §1  Constants
# ═══════════════════════════════════════════════════════════════════════

TOOL_ERROR_MAX_CHARS: int = 10_000
"""Errors longer than this are head/tail truncated."""

TOOL_ERROR_HEAD_CHARS: int = 5_000
TOOL_ERROR_TAIL_CHARS: int = 5_000

EMPTY_RESULT_TEMPLATE = "({tool_name} completed with no output)"
"""Used when content is empty so the model does not treat it as a stop signal."""

# Tool name constants — used by normalize_tool_input, COMPACTABLE_TOOLS, etc.
# These are the canonical names used by the tool runtime.
BASH_TOOL_NAME = "bash"
FILE_READ_TOOL_NAME = "read"
FILE_EDIT_TOOL_NAME = "edit"
FILE_WRITE_TOOL_NAME = "write"
NOTEBOOK_EDIT_TOOL_NAME = "notebook_edit"
GREP_TOOL_NAME = "grep"
GLOB_TOOL_NAME = "glob"
WEB_SEARCH_TOOL_NAME = "web_search"
WEB_FETCH_TOOL_NAME = "web_fetch"
TASK_OUTPUT_TOOL_NAME = "TaskOutput"
EXIT_PLAN_MODE_TOOL_NAME = "ExitPlanMode"

# ToolSearch integration — populated in step 11.1
TOOL_SEARCH_TOOL_NAME = "tool_search"

LIST_DIR_TOOL_NAME = "ls"

PATH_ARGUMENT_TARGETS = {
    FILE_READ_TOOL_NAME: "file_path",
    FILE_EDIT_TOOL_NAME: "file_path",
    FILE_WRITE_TOOL_NAME: "file_path",
    GREP_TOOL_NAME: "path",
    GLOB_TOOL_NAME: "path",
    LIST_DIR_TOOL_NAME: "path",
}

PATH_ARGUMENT_ALIASES = {
    "file",
    "file_path",
    "filepath",
    "filename",
    "path",
}

# Invisible Unicode characters that LLMs sometimes inject into code strings.
# Implementation: normalizeFileEditInput in tools/FileEditTool/utils.ts
_INVISIBLE_CHARS_RE = re.compile(
    "[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u2062\u2063\u2064"
    "\u00ad\u034f\u061c\u180e\u2000-\u200a\u2028\u2029\u202a-\u202e"
    "\u2066-\u2069\ufff9-\ufffb]"
)


# ═══════════════════════════════════════════════════════════════════════
# §2  Data types
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class ToolCallResult:
    """Result of a single tool call execution.

    Implementation: the ``MessageUpdateLazy[]`` array returned by
    ``checkPermissionsAndCallTool``.  OS flattens the async generator
    into a collected result.

    Consumed by ``run_tools`` (step 5.2) and the agent loop (step 7.1).
    """

    tool_use_id: str
    tool_name: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    """Messages to append to the conversation (tool_result + hook messages)."""

    prevent_continuation: bool = False
    """If True, the agent loop should break after processing this result.
    Implementation: ``shouldPreventContinuation`` from pre/post hooks."""

    stop_reason: str | None = None
    """Reason for stopping continuation (from hooks)."""

    context_modifier: Callable[[ToolUseContext], ToolUseContext] | None = None
    """Optional modifier to apply to ToolUseContext after this call.
    Implementation: ``contextModifier`` in ``MessageUpdateLazy``."""


def tool_call_result_to_tool_result(result: ToolCallResult) -> ToolResult:
    """Convert pipeline output to the legacy/public ``ToolResult`` shape.

    Public non-agent entrypoints such as ``GroundingClient.invoke_tool()`` and
    ``BaseSession.call_tool()`` should still execute through ``run_tool_use()``;
    this helper is the single boundary back to Grounding's historical result
    object.
    """

    tool_message = None
    for message in result.messages:
        if not isinstance(message, dict):
            continue
        meta = message.get("_meta")
        if (
            message.get("role") == "tool"
            and isinstance(meta, dict)
            and meta.get("type") == "tool_result"
        ):
            tool_message = message
            break

    if tool_message is None:
        content = "\n".join(
            extract_text_from_content(message.get("content"))
            for message in result.messages
            if isinstance(message, dict)
        ).strip()
        error = content or "Tool execution produced no tool result message"
        return ToolResult(
            status=ToolStatus.ERROR,
            content=content,
            error=error,
            metadata={
                "tool": result.tool_name,
                "tool_call_id": result.tool_use_id,
            },
        )

    meta = tool_message.get("_meta") or {}
    raw_status = str(meta.get("status") or "").lower()
    is_success = raw_status == ToolStatus.SUCCESS.value
    content = tool_message.get("content", "")
    metadata = {}
    tool_result_metadata = meta.get("tool_result_metadata")
    if isinstance(tool_result_metadata, dict):
        metadata.update(tool_result_metadata)
    metadata["tool"] = result.tool_name
    metadata["tool_call_id"] = result.tool_use_id
    if raw_status and raw_status not in {
        ToolStatus.SUCCESS.value,
        ToolStatus.ERROR.value,
    }:
        metadata["pipeline_status"] = raw_status
    if meta.get("error_type"):
        metadata["error_type"] = meta["error_type"]

    execution_time = meta.get("execution_time")
    if execution_time is not None:
        try:
            execution_time = float(execution_time)
        except (TypeError, ValueError):
            execution_time = None

    error = None
    if not is_success:
        error_text = extract_text_from_content(content)
        error = error_text.removeprefix("Error: ").strip() or error_text

    return ToolResult(
        status=ToolStatus.SUCCESS if is_success else ToolStatus.ERROR,
        content=content,
        error=error,
        execution_time=execution_time,
        metadata=metadata,
    )


@dataclass(frozen=True)
class PermissionAskResolution:
    """Outcome of an interactive permission ask."""

    deny_message: dict[str, Any] | None = None
    updated_input: dict[str, Any] | None = None
    skip_permission_recheck: bool = False
    prevent_continuation: bool = False
    stop_reason: str | None = None


# ═══════════════════════════════════════════════════════════════════════
# §3  Error formatting
# ═══════════════════════════════════════════════════════════════════════

def _get_error_parts(error: BaseException) -> list[str]:
    """Extract displayable parts from an exception.

    Shell execution errors expose exit-code, interrupted, stderr, and stdout
    attributes. Generic exceptions fall back to their string message plus any
    stderr/stdout attributes they provide.
    """
    parts: list[str] = []

    # ShellError equivalent: exceptions with exit_code/stderr/stdout
    exit_code = getattr(error, "exit_code", None) or getattr(error, "returncode", None)
    if exit_code is not None:
        interrupted = getattr(error, "interrupted", False)
        if interrupted:
            parts.append(f"Exit code {exit_code}")
            parts.append(INTERRUPT_MESSAGE_FOR_TOOL_USE)
        else:
            parts.append(f"Exit code {exit_code}")

        stderr = getattr(error, "stderr", None)
        if stderr and isinstance(stderr, str):
            parts.append(stderr)
        stdout = getattr(error, "stdout", None)
        if stdout and isinstance(stdout, str):
            parts.append(stdout)
        return parts

    # Generic Error: message + optional stderr/stdout attributes
    msg = str(error)
    if msg:
        parts.append(msg)

    stderr = getattr(error, "stderr", None)
    if stderr and isinstance(stderr, str):
        parts.append(stderr)
    stdout = getattr(error, "stdout", None)
    if stdout and isinstance(stdout, str):
        parts.append(stdout)

    return parts


def format_tool_error(error: BaseException | str) -> str:
    """Format a tool execution error into a user-readable string.

    Truncation strategy: if the message exceeds
    ``TOOL_ERROR_MAX_CHARS``, keep the first 5000 and last 5000 chars
    with a ``[N characters truncated]`` indicator in the middle.
    """
    if isinstance(error, str):
        full_message = error
    else:
        if _is_abort_error(error):
            msg = str(error)
            return msg if msg else INTERRUPT_MESSAGE_FOR_TOOL_USE

        parts = _get_error_parts(error)
        full_message = "\n".join(p for p in parts if p).strip()

        if not full_message:
            full_message = "Command failed with no output"

    if len(full_message) <= TOOL_ERROR_MAX_CHARS:
        return full_message

    truncated_count = len(full_message) - TOOL_ERROR_MAX_CHARS
    head = full_message[:TOOL_ERROR_HEAD_CHARS]
    tail = full_message[-TOOL_ERROR_TAIL_CHARS:]
    return f"{head}\n... [{truncated_count} characters truncated] ...\n{tail}"


def format_validation_error(
    tool_name: str,
    validation_error: jsonschema.ValidationError,
) -> str:
    """Format a JSON Schema validation error into a user-readable message.

    Categorizes schema issues into:
      - missing required params  (``invalid_type`` + ``received undefined``)
      - unexpected params        (``unrecognized_keys``)
      - type mismatch            (``invalid_type`` + not undefined)

    jsonschema provides ``validator``, ``path``, and ``message`` on each error.
    """
    error_parts: list[str] = []

    # jsonschema may have sub-errors in context
    errors = list(validation_error.context) if validation_error.context else [validation_error]

    for err in errors:
        path_str = ".".join(str(p) for p in err.absolute_path) if err.absolute_path else "(root)"
        validator = err.validator

        if validator == "required":
            # Extract missing property names from message
            match = re.search(r"'(\w+)' is a required property", err.message)
            param = match.group(1) if match else path_str
            error_parts.append(f"The required parameter `{param}` is missing.")
        elif validator == "additionalProperties":
            match = re.search(r"Additional properties are not allowed \((.+) (?:was|were)", err.message)
            if match:
                params = match.group(1).replace("'", "`")
                error_parts.append(f"An unexpected parameter {params} was provided.")
            else:
                error_parts.append(f"An unexpected parameter was provided: {err.message}")
        elif validator == "type":
            error_parts.append(
                f"The parameter `{path_str}` has wrong type: {err.message}"
            )
        else:
            error_parts.append(f"Parameter `{path_str}`: {err.message}")

    if not error_parts:
        return f"{tool_name} input validation failed: {validation_error.message}"

    issue_word = "issue" if len(error_parts) == 1 else "issues"
    return (
        f"{tool_name} failed due to the following {issue_word}:\n"
        + "\n".join(error_parts)
    )


async def _validate_input_for_phase(
    tool: BaseTool,
    input_data: dict[str, Any],
    context: ToolUseContext,
    *,
    phase: str,
) -> str | None:
    """Run tool input validation for the requested permission phase."""
    if phase == "pre_permission":
        validator = getattr(tool, "pre_permission_validate_input", None)
        if validator is None:
            validator = tool.validate_input
    elif phase == "post_permission":
        validator = getattr(tool, "post_permission_validate_input", None)
        if validator is None:
            return None
    else:
        raise ValueError(f"Unknown validation phase: {phase}")

    return await validator(input_data, context)


def _build_input_validation_error_message(
    tool_use_id: str,
    tool_name: str,
    validation_error_msg: str,
) -> dict[str, Any]:
    """Produce the tool result message for custom input validation errors."""
    return {
        "role": "tool",
        "name": tool_name,
        "content": f"Error: {validation_error_msg}",
        "tool_call_id": tool_use_id,
        "_meta": {
            "type": "tool_result",
            "tool_name": tool_name,
            "tool_call_id": tool_use_id,
            "status": "error",
            "error_type": "input_validation_error",
            "timestamp": time.time(),
        },
    }


def classify_tool_error(error: BaseException | str) -> str:
    """Classify a tool error for telemetry / analytics.

    Checks exception attributes such as ``telemetry_message`` and ``errno`` when
    present, then falls back to exception type names.
    """
    if isinstance(error, str):
        return "StringError"

    telemetry_msg = getattr(error, "telemetry_message", None)
    if telemetry_msg and isinstance(telemetry_msg, str):
        return telemetry_msg[:200]

    errno_code = getattr(error, "errno", None) or getattr(error, "code", None)
    if errno_code and isinstance(errno_code, (str, int)):
        return f"Error:{errno_code}"

    err_name = type(error).__name__
    if err_name and err_name != "Exception" and err_name != "Error" and len(err_name) > 3:
        return err_name[:60]

    return "Error"


def _json_preview(value: Any, max_chars: int = 500) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)[:max_chars]
    except Exception:
        return str(value)[:max_chars]


def _is_abort_error(error: BaseException) -> bool:
    """Check if error is an abort/cancellation error."""
    return isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)) or (
        type(error).__name__ in ("AbortError", "CancelledError")
    )


def _resolve_tool_backend_value(tool: BaseTool) -> str | None:
    """Best-effort backend string for hook runtime state."""
    runtime_info = getattr(tool, "_runtime_info", None)
    runtime_backend = getattr(runtime_info, "backend", None)
    if hasattr(runtime_backend, "value"):
        return runtime_backend.value
    if isinstance(runtime_backend, str) and runtime_backend:
        return runtime_backend

    backend_type = getattr(tool, "backend_type", None)
    if hasattr(backend_type, "value"):
        return backend_type.value
    if isinstance(backend_type, str) and backend_type:
        return backend_type
    return None


def _is_tool_result_like(value: Any) -> bool:
    """Return whether a value looks like a ToolResult-compatible object."""
    return all(
        hasattr(value, attr)
        for attr in ("status", "content", "metadata", "error", "execution_time")
    )


def _coerce_hook_updated_tool_output(
    original_result: ToolResult,
    updated_output: Any,
) -> ToolResult:
    """Normalize hook-updated tool output back into a ToolResult."""
    if isinstance(updated_output, ToolResult) or _is_tool_result_like(updated_output):
        return updated_output

    return ToolResult(
        status=original_result.status,
        content=updated_output,
        error=original_result.error,
        execution_time=original_result.execution_time,
        metadata=original_result.metadata,
    )


def _is_last_tool_call_in_iteration(
    tool_use_id: str,
    assistant_message: dict[str, Any] | None,
) -> bool:
    """Return whether the tool call is the final tool call in the assistant turn."""
    if not assistant_message:
        return False

    tool_calls = assistant_message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return False

    last_tool_call = tool_calls[-1]
    if isinstance(last_tool_call, dict):
        return last_tool_call.get("id") == tool_use_id

    return getattr(last_tool_call, "id", None) == tool_use_id


def _build_tool_result_message_from_result(
    tool_use_id: str,
    tool_name: str,
    result: ToolResult,
) -> tuple[dict[str, Any], str, bool]:
    """Serialize a ToolResult into the transcript message format."""
    result_content_raw = result.content if result.content is not None else ""
    result_content_text = extract_text_from_content(result_content_raw)
    is_error = result.status == ToolStatus.ERROR

    if is_error:
        error_str = result.error or result_content_text or "Unknown error"
        error_text = (
            format_tool_error(error_str)
            if isinstance(error_str, str)
            else str(error_str)
        )
        if content_has_multimodal_block(result_content_raw):
            if isinstance(result_content_raw, list):
                result_content_raw = [make_text_block(f"Error: {error_text}")] + result_content_raw
            else:
                result_content_raw = [
                    make_text_block(f"Error: {error_text}"),
                    result_content_raw,
                ]
        else:
            result_content_raw = error_text
        result_content_text = extract_text_from_content(result_content_raw)

    if (
        not result_content_text.strip()
        and not content_has_multimodal_block(result_content_raw)
        and not is_error
    ):
        result_content_raw = EMPTY_RESULT_TEMPLATE.format(tool_name=tool_name)
        result_content_text = result_content_raw

    tool_result_msg = build_tool_result_message(
        tool_call_id=tool_use_id,
        tool_name=tool_name,
        result=ToolResult(
            status=result.status,
            content=result_content_raw,
            error=result.error if is_error else None,
            execution_time=result.execution_time,
            metadata=result.metadata,
        ),
    )
    return tool_result_msg, result_content_text, is_error


def _maybe_persist_tool_result_for_pipeline(
    tool: BaseTool,
    tool_use_id: str,
    tool_name: str,
    result: ToolResult,
    context: ToolUseContext,
) -> ToolResult:
    """Persist oversized tool output at the execution-pipeline boundary.

    OpenSpace persists after ``tool.call`` returns, where the real tool-use id is
    available.  Keeping this here avoids the old BaseTool-level fallback that
    had to use random file names outside the agent pipeline.
    """
    content = result.content if result.content is not None else ""
    if content_has_multimodal_block(content):
        return result
    content_text = extract_text_from_content(content)
    new_content, was_persisted, meta = maybe_persist_large_result(
        content=content_text,
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        max_result_size_chars=tool.max_result_size_chars,
        results_dir=getattr(context, "tool_results_dir", None),
    )
    if not was_persisted:
        return result

    return ToolResult(
        status=result.status,
        content=new_content,
        error=result.error,
        execution_time=result.execution_time,
        metadata={**(result.metadata or {}), **meta},
    )


def _get_tool_additional_messages(
    result: ToolResult,
    *,
    tool_use_id: str,
    tool_name: str,
) -> list[dict[str, Any]]:
    """Return OpenSpace extra messages produced by a tool result."""
    raw_messages = getattr(result, "additional_messages", None)
    if not isinstance(raw_messages, list):
        return []

    messages: list[dict[str, Any]] = []
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        msg = dict(raw)
        meta = dict(msg.get("_meta") or {})
        meta.update({
            "type": meta.get("type") or "tool_result_attachment",
            "tool_name": tool_name,
            "tool_call_id": tool_use_id,
        })
        msg["_meta"] = meta
        messages.append(msg)
    return messages


def _mark_dynamic_skill_paths_from_result(
    result: ToolResult,
    context: ToolUseContext,
) -> None:
    """Feed file-like tool result metadata into dynamic skill discovery."""

    if result.status != ToolStatus.SUCCESS:
        return
    marker = getattr(context, "mark_dynamic_skill_path", None)
    if not callable(marker):
        return
    metadata = result.metadata or {}
    raw_paths: list[Any] = []
    if metadata.get("file_path"):
        raw_paths.append(metadata.get("file_path"))
    filenames = metadata.get("filenames")
    if isinstance(filenames, Sequence) and not isinstance(
        filenames,
        (str, bytes, bytearray),
    ):
        raw_paths.extend(filenames)

    cwd = Path(getattr(context, "cwd", "") or ".")
    for raw_path in raw_paths:
        path_text = str(raw_path or "").strip()
        if not path_text:
            continue
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = cwd / path
        marker(str(path))


# ═══════════════════════════════════════════════════════════════════════
# §4  Tool lookup (Implementation: Tool.ts findToolByName)
# ═══════════════════════════════════════════════════════════════════════

def find_tool_by_name(
    tools: Sequence[BaseTool],
    name: str,
) -> BaseTool | None:
    """Find a tool by exact name or alias.

    Implementation: ``findToolByName(tools, name)`` in ``Tool.ts`` L348-360.
    OpenSpace also uses ``toolMatchesName`` which checks name + aliases.
    """
    for tool in tools:
        if tool.name == name:
            return tool
    # Alias fallback
    for tool in tools:
        if name in (tool.aliases or []):
            return tool
    return None


# ═══════════════════════════════════════════════════════════════════════
# §5  Deferred tool handling (Implementation: toolExecution.ts buildSchemaNotSentHint)
# ═══════════════════════════════════════════════════════════════════════

def build_schema_not_sent_hint(
    tool: BaseTool,
    messages: list[dict[str, Any]],
    tools: Sequence[BaseTool],
    *,
    deferred_tool_names: Iterable[str] | None = None,
) -> str | None:
    """Build a hint when a deferred tool's schema wasn't sent to the LLM.

    Implementation: ``buildSchemaNotSentHint(tool, messages, tools)`` in
    ``toolExecution.ts`` L577-597.

    OpenSpace checks:
    1. isToolSearchEnabledOptimistic() — tool search feature flag
    2. isToolSearchToolAvailable(tools) — ToolSearchTool in active tools
    3. isDeferredTool(tool) — tool is deferred
    4. extractDiscoveredToolNames(messages) — tool NOT already discovered

    OS live state is ``discovered_tool_names`` / metadata only. OpenSpace's
    Anthropic-only tool reference block is intentionally not parsed.
    """
    deferred_names = {str(name) for name in (deferred_tool_names or ())}
    if not tool.is_deferred and tool.name not in deferred_names:
        return None

    # Check if ToolSearchTool is available
    tool_search_available = any(t.name == TOOL_SEARCH_TOOL_NAME for t in tools)
    if not tool_search_available:
        return None

    if tool.name in extract_discovered_tool_names(messages):
        return None

    return (
        f"\n\nNote: The tool `{tool.name}` is available but its schema was not "
        f"included in the current prompt. To use it, first run "
        f"`{TOOL_SEARCH_TOOL_NAME}` with `select:{tool.name}` to load its "
        f"schema, then retry your call."
    )


def _check_deferred_tool_not_loaded(
    tool: BaseTool,
    active_tools: Sequence[BaseTool],
    deferred_tool_names: Iterable[str] | None = None,
) -> str | None:
    """Pipeline step 0: intercept calls to deferred tools whose schema
    was not sent to the LLM.

    Implementation: implicit in ``checkPermissionsAndCallTool`` — when the
    model calls a deferred tool, Zod validation fails and
    ``buildSchemaNotSentHint`` is appended.  OS makes this explicit as
    a preemptive check (DEC-003 / 04_module).

    Returns an error message if the tool should be intercepted, None otherwise.
    """
    deferred_names = {str(name) for name in (deferred_tool_names or ())}
    if not tool.is_deferred and tool.name not in deferred_names:
        return None

    # If the tool is in the active_tools list, its schema was sent
    active_names = {t.name for t in active_tools}
    if tool.name in active_names:
        return None

    hint_parts = [
        f"Tool '{tool.name}' is available but not yet loaded. "
        f"Use {TOOL_SEARCH_TOOL_NAME} to discover and load it first.",
    ]

    tool_search_available = TOOL_SEARCH_TOOL_NAME in active_names
    if tool_search_available:
        hint_parts.append(
            f"\nRun `{TOOL_SEARCH_TOOL_NAME}` with query `select:{tool.name}` "
            f"to load the tool schema."
        )

    return "\n".join(hint_parts)


def _build_deferred_not_loaded_result(
    *,
    tool_name: str,
    tool_use_id: str,
    content: str,
) -> ToolCallResult:
    """Return a tool result that tells the model to load a deferred schema."""

    return ToolCallResult(
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        messages=[{
            "role": "tool",
            "name": tool_name,
            "content": content,
            "tool_call_id": tool_use_id,
            "_meta": {
                "type": "tool_result",
                "tool_name": tool_name,
                "tool_call_id": tool_use_id,
                "status": "error",
                "error_type": "deferred_not_loaded",
                "timestamp": time.time(),
            },
        }],
    )


# ═══════════════════════════════════════════════════════════════════════
# §6  Input normalization (Implementation: utils/api.ts normalizeToolInput L566-718)
# ═══════════════════════════════════════════════════════════════════════

def normalize_tool_input(
    tool: BaseTool,
    input: dict[str, Any],
    context: ToolUseContext | None = None,
) -> dict[str, Any]:
    """Normalize tool input before execution.

    Implementation: ``normalizeToolInput(tool, input, agentId)`` in
    ``utils/api.ts`` L566-681.

    Per-tool normalization:
    - BashTool: strip ``cd $cwd && `` prefix, ``\\\\;`` → ``\\;``
    - FileEditTool: fix invisible Unicode chars in old_string/new_string
    - FileWriteTool: strip trailing whitespace for non-markdown files
    - TaskOutput: normalize legacy AgentOutputTool/BashOutputTool params
    - ExitPlanMode: inject local plan content and file path
    - Default: return input unchanged
    """
    tool_name = tool.name
    input = _normalize_path_argument_aliases(tool_name, input)

    if tool_name == EXIT_PLAN_MODE_TOOL_NAME:
        return _normalize_exit_plan_mode_input(input, context)
    elif tool_name == BASH_TOOL_NAME:
        return _normalize_bash_input(input, context)
    elif tool_name == FILE_EDIT_TOOL_NAME:
        return _normalize_file_edit_input(input)
    elif tool_name == FILE_WRITE_TOOL_NAME:
        return _normalize_file_write_input(input)
    elif tool_name == TASK_OUTPUT_TOOL_NAME:
        return _normalize_task_output_input(input)
    else:
        return input


def _normalize_path_argument_aliases(
    tool_name: str,
    input: dict[str, Any],
) -> dict[str, Any]:
    target = PATH_ARGUMENT_TARGETS.get(tool_name)
    if not target or target in input:
        return input

    alias_keys: list[str] = []
    for key, value in input.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        normalized = key.strip().lstrip("-/").replace("-", "_").lower()
        if normalized in PATH_ARGUMENT_ALIASES:
            alias_keys.append(key)

    if len(alias_keys) != 1:
        return input

    alias = alias_keys[0]
    result = dict(input)
    result[target] = result.pop(alias)
    return result


def normalize_tool_input_for_api(
    tool: BaseTool,
    input: dict[str, Any],
) -> dict[str, Any]:
    """Strip locally-injected fields before sending tool_use back to the API.

    Implementation: ``normalizeToolInputForAPI(tool, input)`` in
    ``utils/api.ts`` L683-718.

    OpenSpace strips:
    - ExitPlanModeV2: removes ``plan`` and ``planFilePath``
    - FileEditTool (legacy sessions): removes ``old_string``/``new_string``/
      ``replace_all`` when ``edits`` array is present

    """
    if tool.name == EXIT_PLAN_MODE_TOOL_NAME:
        return {
            key: value
            for key, value in input.items()
            if key not in {"plan", "planFilePath", "filePath"}
        }
    if tool.name == FILE_EDIT_TOOL_NAME and "edits" in input:
        return {
            key: value
            for key, value in input.items()
            if key not in {"old_string", "new_string", "replace_all"}
        }
    return input


def _normalize_exit_plan_mode_input(
    input: dict[str, Any],
    context: ToolUseContext | None = None,
) -> dict[str, Any]:
    from openspace.services.runtime_support.plan_mode import get_plan, get_plan_file_path

    result = dict(input)
    session_id = getattr(context, "session_id", None) if context else None
    agent_id = getattr(context, "agent_id", None) if context else None
    file_path = get_plan_file_path(session_id, agent_id)
    plan = get_plan(session_id, agent_id)
    if plan is not None and "plan" not in result:
        result["plan"] = plan
    result["planFilePath"] = str(file_path)
    return result


def _normalize_bash_input(
    input: dict[str, Any],
    context: ToolUseContext | None = None,
) -> dict[str, Any]:
    """Normalize BashTool input.

    Implementation: ``normalizeToolInput`` BashTool case (api.ts L589-631).

    1. Strip ``cd $cwd && `` prefix from command
    2. Replace ``\\\\;`` with ``\\;`` (find -exec compatibility)
    3. Pass through timeout, description, run_in_background
    """
    command = input.get("command", "")

    # Strip cd prefix (Implementation: normalizedCommand.replace(`cd ${cwd} && `, ''))
    if context and context.cwd:
        cwd = context.cwd
        prefix = f"cd {cwd} && "
        if command.startswith(prefix):
            command = command[len(prefix):]

    # Replace \\; with \; (Implementation: commonly needed for find -exec commands)
    command = command.replace("\\\\;", "\\;")

    result = {**input, "command": command}
    return result


def _normalize_task_output_input(input: dict[str, Any]) -> dict[str, Any]:
    """Normalize OpenSpace legacy AgentOutputTool/BashOutputTool parameters.

    Implementation: ``utils/api.ts`` L661-L676.
    """

    task_id = input.get("task_id") or input.get("agentId") or input.get("bash_id")
    timeout = input.get("timeout")
    wait_up_to = input.get("wait_up_to")
    if timeout is None and isinstance(wait_up_to, (int, float)):
        timeout = int(wait_up_to * 1000)
    return {
        "task_id": task_id or "",
        "block": input.get("block", True),
        "timeout": timeout if timeout is not None else 30_000,
    }


def _normalize_file_edit_input(input: dict[str, Any]) -> dict[str, Any]:
    """Normalize FileEditTool input.

    Implementation: ``normalizeFileEditInput`` in ``tools/FileEditTool/utils.ts``
    (L581-657) + invisible char stripping.

    Three normalization passes:
    1. Strip invisible Unicode characters from old_string / new_string
    2. Strip trailing whitespace from new_string (non-markdown files)
    3. If old_string not found in file, try desanitization fallback
    """
    result = dict(input)

    # Pass 1: invisible characters
    for key in ("old_string", "new_string"):
        value = result.get(key)
        if isinstance(value, str) and _INVISIBLE_CHARS_RE.search(value):
            cleaned = _INVISIBLE_CHARS_RE.sub("", value)
            if cleaned != value:
                logger.debug(
                    "Stripped %d invisible chars from %s",
                    len(value) - len(cleaned),
                    key,
                )
                result[key] = cleaned

    # Pass 2: trailing whitespace on new_string (Implementation: stripTrailingWhitespace)
    file_path = result.get("file_path", "")
    is_markdown = bool(
        isinstance(file_path, str)
        and re.search(r"\.(md|mdx)$", file_path, re.IGNORECASE)
    )
    new_string = result.get("new_string")
    if isinstance(new_string, str) and not is_markdown:
        stripped = "\n".join(line.rstrip() for line in new_string.split("\n"))
        if stripped != new_string:
            result["new_string"] = stripped

    # Pass 3: desanitization fallback (Implementation: DESANITIZATIONS in utils.ts L531-550)
    old_string = result.get("old_string")
    if isinstance(old_string, str) and isinstance(file_path, str) and file_path:
        try:
            from openspace.grounding.backends.shell.file_tools import (
                desanitize_match_string,
            )
            import os
            from pathlib import Path
            full_path = os.path.abspath(os.path.expanduser(file_path))
            if os.path.isfile(full_path):
                file_content = Path(full_path).read_text(
                    encoding="utf-8",
                    errors="replace",
                )
                if old_string not in file_content:
                    desanitized, applied = desanitize_match_string(old_string)
                    if applied and desanitized in file_content:
                        result["old_string"] = desanitized
                        ns = result.get("new_string", "")
                        if isinstance(ns, str):
                            for short, long in applied:
                                ns = ns.replace(short, long)
                            result["new_string"] = ns
        except Exception:
            pass

    return result


def _normalize_file_write_input(input: dict[str, Any]) -> dict[str, Any]:
    """Normalize FileWriteTool input — strip trailing whitespace.

    Implementation: ``normalizeToolInput`` FileWriteTool case (api.ts L653-665).

    Markdown uses trailing spaces for hard line breaks, so .md/.mdx files
    are exempt from stripping.
    """
    file_path = input.get("file_path", "")
    content = input.get("content", "")

    if not isinstance(content, str) or not isinstance(file_path, str):
        return input

    # Implementation: const isMarkdown = /\.(md|mdx)$/i.test(parsedInput.file_path)
    is_markdown = bool(re.search(r"\.(md|mdx)$", file_path, re.IGNORECASE))

    if is_markdown:
        return input

    # Strip trailing whitespace from each line
    stripped = "\n".join(line.rstrip() for line in content.split("\n"))
    if stripped != content:
        return {**input, "content": stripped}

    return input


# ═══════════════════════════════════════════════════════════════════════
# §7  Main pipeline — run_tool_use
# ═══════════════════════════════════════════════════════════════════════

async def run_tool_use(
    tool_call: dict[str, Any],
    tool_map: dict[str, BaseTool],
    context: ToolUseContext,
    *,
    assistant_message: dict[str, Any] | None = None,
) -> ToolCallResult:
    """Execute a single tool call through the runtime pipeline.

    Pipeline steps:
      0. Deferred tool interception
      1. Schema validation
      2. Custom ``validate_input``
      3. Input normalization
      4. Pre-tool hooks
      5. Permission resolution
      6. Tool execution
      7. Result processing
      8. Post-tool hooks

    Parameters
    ----------
    tool_call : dict
        OpenAI-format tool call: ``{id, type, function: {name, arguments}}``.
        ``arguments`` may be a provider JSON string or a dict; this function is
        the canonical parse/fallback point.
    tool_map : dict
        Map of tool names → BaseTool instances from ``ModelResponse.tool_map``.
    context : ToolUseContext
        Turn-scoped runtime context (messages, abort, hooks, etc.).
    assistant_message : dict, optional
        The full assistant message containing this tool_call.

    Returns
    -------
    ToolCallResult
        Contains messages to append and continuation control flags.
    """
    # ── Parse tool_call ──────────────────────────────────────────────
    tool_use_id = tool_call.get("id", "")
    func = tool_call.get("function", {})
    tool_name = func.get("name", "")
    tool_input: dict[str, Any] = func.get("arguments", {})
    if isinstance(tool_input, str):
        import json
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, TypeError):
            tool_input = {}

    start_time = time.time()
    processed_input_for_evidence: dict[str, Any] = dict(tool_input)
    pipeline_status = "error"
    pipeline_error_type: str | None = None
    pipeline_execution_time_ms: float | None = None
    pipeline_result_size_chars = 0
    pipeline_tool_result_metadata: dict[str, Any] = {}
    pipeline_message_meta: dict[str, Any] = {}
    pipeline_result_preview = ""
    pipeline_permission_status: str | None = None
    result_messages: list[dict[str, Any]] = []
    prevent_continuation = False
    stop_reason: str | None = None
    context_modifier: Callable | None = None
    pipeline_complete_emitted = False
    tool: BaseTool | None = None

    if os.environ.get("OPENSPACE_DEBUG_TOOL_CALLS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }:
        print(
            "OPENSPACE_DEBUG run_tool_use "
            f"tool={tool_name} id={tool_use_id} "
            f"permission_mode={getattr(context, 'permission_mode', None)} "
            f"input={_json_preview(tool_input)}",
            flush=True,
        )

    async def _complete(result: ToolCallResult) -> ToolCallResult:
        nonlocal pipeline_complete_emitted
        nonlocal pipeline_error_type
        nonlocal pipeline_execution_time_ms
        nonlocal pipeline_message_meta
        nonlocal pipeline_result_preview
        nonlocal pipeline_result_size_chars
        nonlocal pipeline_status
        nonlocal pipeline_tool_result_metadata
        nonlocal pipeline_permission_status
        if pipeline_complete_emitted:
            return result
        pipeline_complete_emitted = True

        messages_for_evidence = list(result.messages or [])
        if not pipeline_message_meta:
            for message in messages_for_evidence:
                meta = message.get("_meta") if isinstance(message, dict) else None
                if isinstance(meta, dict) and meta.get("type") == "tool_result":
                    pipeline_message_meta = dict(meta)
                    break

        if pipeline_message_meta:
            pipeline_status = str(
                pipeline_message_meta.get("status")
                or pipeline_status
                or ("error" if pipeline_error_type else "success")
            )
            if pipeline_message_meta.get("error_type"):
                pipeline_error_type = str(pipeline_message_meta.get("error_type"))
            metadata = pipeline_message_meta.get("tool_result_metadata")
            if isinstance(metadata, dict) and not pipeline_tool_result_metadata:
                pipeline_tool_result_metadata = dict(metadata)
            execution_time = pipeline_message_meta.get("execution_time")
            if pipeline_execution_time_ms is None and isinstance(
                execution_time,
                (int, float),
            ):
                pipeline_execution_time_ms = float(execution_time) * 1000

        if not pipeline_result_preview:
            for message in messages_for_evidence:
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                text = extract_text_from_content(content)
                if text:
                    pipeline_result_preview = text[:500]
                    break

        if pipeline_result_size_chars == 0:
            pipeline_result_size_chars = sum(
                content_text_size(message.get("content"))
                for message in messages_for_evidence
                if isinstance(message, dict)
            )

        total_duration_ms = (time.time() - start_time) * 1000
        try:
            complete_payload = {
                "session_id": getattr(context, "session_id", None),
                "task_id": getattr(context, "task_id", None),
                "agent_id": getattr(context, "agent_id", None),
                "parent_task_id": getattr(context, "parent_task_id", None),
                "current_iteration": getattr(context, "current_iteration", None),
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "backend": _resolve_tool_backend_value(tool) if tool is not None else None,
                "server_name": (
                    getattr(getattr(tool, "_runtime_info", None), "server_name", None)
                    if tool is not None
                    else None
                ) or "default",
                "status": pipeline_status,
                "error_type": pipeline_error_type,
                "permission_status": pipeline_permission_status,
                "execution_time_ms": pipeline_execution_time_ms,
                "total_duration_ms": total_duration_ms,
                "input_preview": _json_preview(processed_input_for_evidence),
                "result_size_chars": pipeline_result_size_chars,
                "result_preview": pipeline_result_preview,
                "tool_result_metadata": pipeline_tool_result_metadata,
                "message_meta": pipeline_message_meta,
                "message_count": len(messages_for_evidence),
                "prevent_continuation": result.prevent_continuation,
            }
            complete_payload.update(active_skill_scope_payload(context))
            await context.emit_event("tool_pipeline_complete", complete_payload)
        except Exception:
            logger.debug(
                "Failed to emit tool_pipeline_complete for %s/%s",
                tool_name,
                tool_use_id,
                exc_info=True,
            )
        await _record_pipeline_quality_outcome(
            context,
            tool=tool,
            tool_use_id=tool_use_id,
            status=pipeline_status,
            error_type=pipeline_error_type,
            permission_status=pipeline_permission_status,
            execution_time_ms=(
                pipeline_execution_time_ms
                if pipeline_execution_time_ms is not None
                else total_duration_ms
            ),
            result_preview=pipeline_result_preview,
        )
        return result

    # ── Find tool ────────────────────────────────────────────────────
    # Implementation: findToolByName(toolUseContext.options.tools, toolName)
    tool = tool_map.get(tool_name)

    if tool is None:
        # Alias fallback — Implementation: findToolByName(getAllBaseTools(), toolName)
        # with aliases?.includes(toolName) guard
        tool = find_tool_by_name(list(tool_map.values()), tool_name)

    if tool is None:
        # The LLM only receives active tool schemas, so a direct call to a
        # deferred tool usually misses ``tool_map``. Check the full tool
        # universe before returning a generic "No such tool" error.
        deferred_tool = find_tool_by_name(context.all_tools, tool_name)
        if deferred_tool is not None:
            deferred_msg = _check_deferred_tool_not_loaded(
                deferred_tool,
                context.tools,
                context.deferred_tool_names,
            )
            if deferred_msg is not None:
                logger.debug("Deferred tool call found outside active tool map: %s", tool_name)
                await context.emit_event("tool_deferred_intercepted", {
                    "tool_name": tool_name,
                    "tool_use_id": tool_use_id,
                })
                return await _complete(_build_deferred_not_loaded_result(
                    tool_name=tool_name,
                    tool_use_id=tool_use_id,
                    content=deferred_msg,
                ))

        # Tool not found — Implementation: yield error user message
        logger.warning("Tool not found: %s", tool_name)
        await context.emit_event("tool_error", {
            "tool_name": tool_name,
            "error": "no_such_tool",
            "tool_use_id": tool_use_id,
        })
        error_msg = (
            f"Error: No such tool: `{tool_name}`. "
            f"Available tools: {', '.join(sorted(tool_map.keys()))}"
        )
        return await _complete(ToolCallResult(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            messages=[{
                "role": "tool",
                "name": tool_name,
                "content": error_msg,
                "tool_call_id": tool_use_id,
                "_meta": {
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "tool_call_id": tool_use_id,
                    "status": "error",
                    "error_type": "no_such_tool",
                    "timestamp": time.time(),
                },
            }],
        ))

    # ── Check abort ──────────────────────────────────────────────────
    # Implementation: if (abortController.signal.aborted) → yield cancel message
    if context.is_aborted():
        logger.debug("Tool call aborted: %s/%s", tool_name, tool_use_id)
        await context.emit_event("tool_cancelled", {
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
        })
        return await _complete(ToolCallResult(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            messages=[build_tool_result_stop_message(tool_use_id, tool_name)],
        ))

    tool_input = _normalize_path_argument_aliases(tool.name, tool_input)
    processed_input_for_evidence = dict(tool_input)

    # ── Emit tool_start event ────────────────────────────────────────
    await context.emit_event("tool_start", {
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "tool_input": tool_input,
    })

    try:
        # ── Step 0: Deferred tool interception ───────────────────────
        # DEC-003 / 04_module: if tool is deferred and schema was not
        # sent, return a hint instead of executing.
        deferred_msg = _check_deferred_tool_not_loaded(
            tool,
            context.tools,
            context.deferred_tool_names,
        )
        if deferred_msg is not None:
            logger.debug("Deferred tool intercepted: %s", tool_name)
            await context.emit_event("tool_deferred_intercepted", {
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
            })
            return await _complete(_build_deferred_not_loaded_result(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                content=deferred_msg,
            ))

        # ── Step 1: Schema validation ────────────────────────────────
        # Implementation: tool.inputSchema.safeParse(input)
        schema_error = _validate_schema(tool, tool_input)
        if schema_error is not None:
            error_text = format_validation_error(tool_name, schema_error)
            # Implementation: buildSchemaNotSentHint — append hint for deferred tools
            hint = build_schema_not_sent_hint(tool, context.messages, context.tools)
            if hint:
                error_text += hint
            logger.debug("Schema validation failed for %s: %s", tool_name, error_text)
            await context.emit_event("tool_validation_error", {
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "error": error_text,
            })
            result_messages.append({
                "role": "tool",
                "name": tool_name,
                "content": f"Error: {error_text}",
                "tool_call_id": tool_use_id,
                "_meta": {
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "tool_call_id": tool_use_id,
                    "status": "error",
                    "error_type": "validation_error",
                    "timestamp": time.time(),
                },
            })
            return await _complete(ToolCallResult(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                messages=result_messages,
            ))

        # ── Step 2: Custom validate_input ────────────────────────────
        # Implementation: tool.validateInput?.(parsedInput.data, toolUseContext)
        validation_error_msg = await _validate_input_for_phase(
            tool,
            tool_input,
            context,
            phase="pre_permission",
        )
        if validation_error_msg is not None:
            logger.debug("validate_input failed for %s: %s", tool_name, validation_error_msg)
            await context.emit_event("tool_validation_error", {
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "error": validation_error_msg,
            })
            result_messages.append(
                _build_input_validation_error_message(
                    tool_use_id, tool_name, validation_error_msg
                )
            )
            return await _complete(ToolCallResult(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                messages=result_messages,
            ))

        # ── Step 3: Input normalization ──────────────────────────────
        # Implementation: normalizeToolInput(tool, input, agentId) in api.ts
        processed_input = normalize_tool_input(tool, tool_input, context)

        # ── Step 4: Pre-tool hooks ───────────────────────────────────
        # Implementation: for await (runPreToolUseHooks(...)) → process yields
        hook_registry = context.hook_registry
        hook_permission_result: dict[str, Any] | None = None
        pre_hook_base_input = processed_input
        should_stop_from_hooks = False

        if hook_registry:
            from openspace.services.tooling.hooks import run_pre_tool_use_hooks

            pre_hook_start = time.time()
            async for yield_item in run_pre_tool_use_hooks(
                hook_registry, tool_name, processed_input, tool_use_id, context,
            ):
                yield_type = yield_item.type

                if yield_type == "message" and yield_item.message:
                    # Hook messages may be dict or str; ensure dict for messages list
                    msg = yield_item.message
                    if isinstance(msg, dict):
                        result_messages.append(msg)

                elif yield_type == "hook_permission_result" and yield_item.hook_permission_result:
                    hook_permission_result = _merge_hook_permission_result(
                        hook_permission_result,
                        yield_item.hook_permission_result,
                    )
                    updated = hook_permission_result.get("updated_input")
                    if isinstance(updated, dict):
                        processed_input = updated
                    else:
                        processed_input = pre_hook_base_input

                elif yield_type == "hook_updated_input" and yield_item.updated_input:
                    processed_input = yield_item.updated_input
                    pre_hook_base_input = processed_input

                elif yield_type == "prevent_continuation":
                    prevent_continuation = True

                elif yield_type == "stop_reason" and yield_item.stop_reason:
                    stop_reason = yield_item.stop_reason

                elif yield_type == "stop":
                    should_stop_from_hooks = True
                    break

                elif yield_type == "additional_context" and yield_item.message:
                    msg = yield_item.message
                    if isinstance(msg, dict):
                        result_messages.append(msg)

            pre_hook_duration_ms = (time.time() - pre_hook_start) * 1000
            if pre_hook_duration_ms > 500:
                logger.debug(
                    "Pre-tool hooks for %s took %.0fms", tool_name, pre_hook_duration_ms
                )

        if should_stop_from_hooks:
            # Implementation: pre-hook returned stop → push stop tool_result → return
            stop_msg = build_tool_result_stop_message(tool_use_id, tool_name)
            if stop_reason:
                stop_msg["content"] = f"Error: {stop_reason}"
            result_messages.append(stop_msg)
            await context.emit_event("tool_hook_stopped", {
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "phase": "pre",
                "stop_reason": stop_reason,
            })
            return await _complete(ToolCallResult(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                messages=result_messages,
                prevent_continuation=prevent_continuation,
                stop_reason=stop_reason,
            ))

        # ── Step 5: Permission resolution ────────────────────────────
        # Implementation: resolveHookPermissionDecision(hookPermissionResult, tool,
        #     input, toolUseContext, canUseTool, assistantMessage, toolUseID)
        permission_decision = await _resolve_permissions(
            tool, processed_input, context,
            hook_permission_result=hook_permission_result,
        )
        pipeline_permission_status = permission_decision.behavior
        if os.environ.get("OPENSPACE_DEBUG_TOOL_CALLS", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }:
            print(
                "OPENSPACE_DEBUG permission_decision "
                f"tool={tool_name} behavior={permission_decision.behavior} "
                f"mode={getattr(context, 'permission_mode', None)}",
                flush=True,
            )

        ask_resolution: PermissionAskResolution | None = None
        if permission_decision.behavior != "allow":
            # Denied or needs user interaction
            if permission_decision.behavior == "deny":
                pipeline_permission_status = "denied"
                deny_content = permission_decision.message or CANCEL_MESSAGE
                logger.debug("Tool %s denied: %s", tool_name, deny_content)
                hook_says_retry = await _emit_permission_denied(
                    hook_registry,
                    tool_name,
                    processed_input,
                    deny_content,
                    tool_use_id,
                    context,
                )
                result_messages.append(
                    _build_tool_denied_message(tool_use_id, tool_name, deny_content)
                )
                if hook_says_retry:
                    result_messages.append(
                        _build_permission_denied_retry_message(tool_use_id, tool_name)
                    )
                return await _complete(ToolCallResult(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    messages=result_messages,
                ))

            # behavior == "ask" — delegate to permission engine / TUI.
            # Implementation: PermissionsLayer modal with rule-suggestions.  OS surfaces
            # ``suggestions`` (PermissionUpdate tuple) so the TUI can offer
            # "always allow" persistence (Q2 = localSettings).
            pipeline_permission_status = "asked"
            ask_resolution = await _handle_permission_ask(
                tool, processed_input, context,
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                message=permission_decision.message,
                suggestions=getattr(permission_decision, "suggestions", None),
                blocked_path=getattr(permission_decision, "blocked_path", None),
                decision_reason=getattr(permission_decision, "decision_reason", None),
            )
            if ask_resolution.deny_message is not None:
                pipeline_permission_status = "denied"
                # User denied
                await _record_skill_permission_denied_from_tool_execution(
                    tool_name,
                    processed_input,
                    context,
                    reason=_extract_permission_denied_reason(
                        ask_resolution.deny_message
                    ),
                )
                hook_says_retry = await _emit_permission_denied(
                    hook_registry,
                    tool_name,
                    processed_input,
                    _extract_permission_denied_reason(ask_resolution.deny_message),
                    tool_use_id,
                    context,
                )
                result_messages.append(ask_resolution.deny_message)
                if hook_says_retry:
                    result_messages.append(
                        _build_permission_denied_retry_message(tool_use_id, tool_name)
                    )
                return await _complete(ToolCallResult(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    messages=result_messages,
                    prevent_continuation=ask_resolution.prevent_continuation,
                    stop_reason=ask_resolution.stop_reason,
                ))
            # User allowed — continue execution
            pipeline_permission_status = "allowed_after_ask"
            await _record_skill_permission_granted_from_tool_execution(
                tool_name,
                processed_input,
                context,
                reason="permission ask allowed",
            )

        # Apply any updatedInput from permission resolution
        if ask_resolution is not None and ask_resolution.updated_input is not None:
            processed_input = ask_resolution.updated_input
            schema_error = _validate_schema(tool, processed_input)
            if schema_error is not None:
                error_text = format_validation_error(tool_name, schema_error)
                result_messages.append({
                    "role": "tool",
                    "name": tool_name,
                    "content": f"Error: {error_text}",
                    "tool_call_id": tool_use_id,
                    "_meta": {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "tool_call_id": tool_use_id,
                        "status": "error",
                        "error_type": "validation_error",
                        "timestamp": time.time(),
                    },
                })
                return await _complete(ToolCallResult(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    messages=result_messages,
                ))

            validation_error_msg = await _validate_input_for_phase(
                tool,
                processed_input,
                context,
                phase="pre_permission",
            )
            if validation_error_msg is not None:
                result_messages.append(
                    _build_input_validation_error_message(
                        tool_use_id, tool_name, validation_error_msg
                    )
                )
                return await _complete(ToolCallResult(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    messages=result_messages,
                ))

            processed_input = normalize_tool_input(tool, processed_input, context)

            if not ask_resolution.skip_permission_recheck:
                edited_hook_permission_result: dict[str, Any] | None = None
                edited_pre_hook_base_input = processed_input
                edited_should_stop_from_hooks = False
                if hook_registry:
                    from openspace.services.tooling.hooks import run_pre_tool_use_hooks

                    async for yield_item in run_pre_tool_use_hooks(
                        hook_registry, tool_name, processed_input, tool_use_id, context,
                    ):
                        yield_type = yield_item.type
                        if yield_type == "message" and yield_item.message:
                            msg = yield_item.message
                            if isinstance(msg, dict):
                                result_messages.append(msg)
                        elif yield_type == "hook_permission_result" and yield_item.hook_permission_result:
                            edited_hook_permission_result = _merge_hook_permission_result(
                                edited_hook_permission_result,
                                yield_item.hook_permission_result,
                            )
                            updated = edited_hook_permission_result.get("updated_input")
                            if isinstance(updated, dict):
                                processed_input = updated
                            else:
                                processed_input = edited_pre_hook_base_input
                        elif yield_type == "hook_updated_input" and yield_item.updated_input:
                            processed_input = yield_item.updated_input
                            edited_pre_hook_base_input = processed_input
                        elif yield_type == "prevent_continuation":
                            prevent_continuation = True
                        elif yield_type == "stop_reason" and yield_item.stop_reason:
                            stop_reason = yield_item.stop_reason
                        elif yield_type == "stop":
                            edited_should_stop_from_hooks = True
                            break
                        elif yield_type == "additional_context" and yield_item.message:
                            msg = yield_item.message
                            if isinstance(msg, dict):
                                result_messages.append(msg)

                if edited_should_stop_from_hooks:
                    stop_msg = build_tool_result_stop_message(tool_use_id, tool_name)
                    if stop_reason:
                        stop_msg["content"] = f"Error: {stop_reason}"
                    result_messages.append(stop_msg)
                    await context.emit_event("tool_hook_stopped", {
                        "tool_name": tool_name,
                        "tool_use_id": tool_use_id,
                        "phase": "pre",
                        "stop_reason": stop_reason,
                        "after_permission_edit": True,
                    })
                    return await _complete(ToolCallResult(
                        tool_use_id=tool_use_id,
                        tool_name=tool_name,
                        messages=result_messages,
                        prevent_continuation=prevent_continuation,
                        stop_reason=stop_reason,
                    ))

                rechecked_decision = await _resolve_permissions(
                    tool,
                    processed_input,
                    context,
                    hook_permission_result=edited_hook_permission_result,
                )
                if rechecked_decision.behavior == "deny":
                    pipeline_permission_status = "denied"
                    deny_content = rechecked_decision.message or CANCEL_MESSAGE
                    hook_says_retry = await _emit_permission_denied(
                        hook_registry,
                        tool_name,
                        processed_input,
                        deny_content,
                        tool_use_id,
                        context,
                    )
                    result_messages.append(
                        _build_tool_denied_message(tool_use_id, tool_name, deny_content)
                    )
                    if hook_says_retry:
                        result_messages.append(
                            _build_permission_denied_retry_message(tool_use_id, tool_name)
                        )
                    return await _complete(ToolCallResult(
                        tool_use_id=tool_use_id,
                        tool_name=tool_name,
                        messages=result_messages,
                    ))
                if rechecked_decision.behavior == "ask":
                    pipeline_permission_status = "asked_after_edit"
                    second_ask_resolution = await _handle_permission_ask(
                        tool,
                        processed_input,
                        context,
                        tool_use_id=tool_use_id,
                        tool_name=tool_name,
                        message=rechecked_decision.message,
                        suggestions=getattr(rechecked_decision, "suggestions", None),
                        blocked_path=getattr(rechecked_decision, "blocked_path", None),
                        decision_reason=getattr(rechecked_decision, "decision_reason", None),
                    )
                    if second_ask_resolution.deny_message is not None:
                        pipeline_permission_status = "denied"
                        hook_says_retry = await _emit_permission_denied(
                            hook_registry,
                            tool_name,
                            processed_input,
                            _extract_permission_denied_reason(
                                second_ask_resolution.deny_message
                            ),
                            tool_use_id,
                            context,
                        )
                        result_messages.append(second_ask_resolution.deny_message)
                        if hook_says_retry:
                            result_messages.append(
                                _build_permission_denied_retry_message(
                                    tool_use_id, tool_name
                                )
                            )
                        return await _complete(ToolCallResult(
                            tool_use_id=tool_use_id,
                            tool_name=tool_name,
                            messages=result_messages,
                        ))
                    pipeline_permission_status = "allowed_after_second_ask"
                    if second_ask_resolution.updated_input is not None:
                        deny_content = (
                            "Edited input still requires another permission edit; "
                            "refusing to execute without a fresh tool call."
                        )
                        hook_says_retry = await _emit_permission_denied(
                            hook_registry,
                            tool_name,
                            processed_input,
                            deny_content,
                            tool_use_id,
                            context,
                        )
                        result_messages.append(
                            _build_tool_denied_message(
                                tool_use_id,
                                tool_name,
                                deny_content,
                            )
                        )
                        if hook_says_retry:
                            result_messages.append(
                                _build_permission_denied_retry_message(
                                    tool_use_id, tool_name
                                )
                            )
                        return await _complete(ToolCallResult(
                            tool_use_id=tool_use_id,
                            tool_name=tool_name,
                            messages=result_messages,
                        ))

                if rechecked_decision.updated_input is not None:
                    processed_input = rechecked_decision.updated_input
        elif permission_decision.updated_input is not None:
            processed_input = permission_decision.updated_input

        post_permission_validation_error = await _validate_input_for_phase(
            tool,
            processed_input,
            context,
            phase="post_permission",
        )
        if post_permission_validation_error is not None:
            await context.emit_event("tool_validation_error", {
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "error": post_permission_validation_error,
            })
            result_messages.append(
                _build_input_validation_error_message(
                    tool_use_id, tool_name, post_permission_validation_error
                )
            )
            return await _complete(ToolCallResult(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                messages=result_messages,
            ))

        # ── Step 6: Tool execution ───────────────────────────────────
        # Implementation: await tool.call(callInput, {...toolUseContext}, canUseTool, ...)
        # Public BaseTool.invoke() routes into this runtime.  The runtime
        # boundary uses the private raw executor after permissions, hooks,
        # validation, persistence, and quality tracking are in place.  There
        # is intentionally no public arun() facade.
        # Inject ToolUseContext for tools that need it (e.g. FileEditTool
        # needs read_file_state for mtime check).
        if hasattr(tool, "set_context"):
            tool.set_context(context)
        set_tool_use = getattr(tool, "set_current_tool_use", None)
        if callable(set_tool_use):
            set_tool_use(tool_use_id=tool_use_id, tool_name=tool_name)
        processed_input_for_evidence = dict(processed_input)

        await context.emit_event("tool_executing", {
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
        })

        exec_start = time.time()
        try:
            result = await tool._execute_raw(**processed_input)
        except asyncio.CancelledError as exc:
            if not context.is_aborted():
                raise
            error_content = format_tool_error(exc)
            exec_duration_ms = (time.time() - exec_start) * 1000
            await context.emit_event("tool_cancelled", {
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "duration_ms": exec_duration_ms,
            })

            if hook_registry:
                from openspace.services.tooling.hooks import run_post_tool_use_failure_hooks

                async for hook_result in run_post_tool_use_failure_hooks(
                    hook_registry,
                    tool_name,
                    processed_input,
                    error_content,
                    tool_use_id,
                    is_interrupt=True,
                    context=context,
                ):
                    if hook_result.message and isinstance(hook_result.message, dict):
                        result_messages.append(hook_result.message)

            result_messages.append({
                "role": "tool",
                "name": tool_name,
                "content": f"Error: {error_content or CANCEL_MESSAGE}",
                "tool_call_id": tool_use_id,
                "_meta": {
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "tool_call_id": tool_use_id,
                    "status": "error",
                    "error_type": "interrupt",
                    "execution_time": exec_duration_ms / 1000,
                    "timestamp": time.time(),
                },
            })
            return await _complete(ToolCallResult(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                messages=result_messages,
                prevent_continuation=prevent_continuation,
                stop_reason=stop_reason,
            ))
        except Exception as exc:
            # Implementation: catch block in checkPermissionsAndCallTool
            error_content = format_tool_error(exc)
            error_class = classify_tool_error(exc)
            exec_duration_ms = (time.time() - exec_start) * 1000

            logger.debug(
                "Tool %s failed (%s) in %.0fms: %s",
                tool_name, error_class, exec_duration_ms,
                error_content[:200],
            )
            await context.emit_event("tool_error", {
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "error_class": error_class,
                "duration_ms": exec_duration_ms,
            })

            # Implementation: runPostToolUseFailureHooks
            if hook_registry:
                from openspace.services.tooling.hooks import run_post_tool_use_failure_hooks
                async for hook_result in run_post_tool_use_failure_hooks(
                    hook_registry,
                    tool_name,
                    processed_input,
                    error_content,
                    tool_use_id,
                    is_interrupt=_is_abort_error(exc),
                    context=context,
                ):
                    if hook_result.message and isinstance(hook_result.message, dict):
                        result_messages.append(hook_result.message)

            result_messages.append({
                "role": "tool",
                "name": tool_name,
                "content": f"Error: {error_content}",
                "tool_call_id": tool_use_id,
                "_meta": {
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "tool_call_id": tool_use_id,
                    "status": "error",
                    "error_type": error_class,
                    "execution_time": exec_duration_ms / 1000,
                    "timestamp": time.time(),
                },
            })
            return await _complete(ToolCallResult(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                messages=result_messages,
                prevent_continuation=prevent_continuation,
                stop_reason=stop_reason,
            ))

        exec_duration_ms = (time.time() - exec_start) * 1000

        # ── Step 7: Result processing ────────────────────────────────
        # Implementation: processPreMappedToolResultBlock → persist-to-disk + empty check
        tool_result_msg, result_content, is_error = _build_tool_result_message_from_result(
            tool_use_id,
            tool_name,
            result,
        )

        logger.debug(
            "Tool %s completed (%s) in %.0fms, result: %d text chars",
            tool_name,
            "error" if is_error else "success",
            exec_duration_ms,
            content_text_size(result.content),
        )
        await context.emit_event("tool_complete", {
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "status": "error" if is_error else "success",
            "duration_ms": exec_duration_ms,
            "result_size_chars": content_text_size(result.content),
            "has_multimodal_content": content_has_multimodal_block(result.content),
        })

        # ── Step 8: Post-tool hooks ──────────────────────────────────
        # Implementation: runPostToolUseHooks → collect messages, check preventContinuation
        if hook_registry:
            from openspace.services.tooling.hooks import (
                PostToolHookRuntimeState,
                run_post_tool_use_hooks,
            )

            backend = _resolve_tool_backend_value(tool)
            if not backend:
                raise RuntimeError(f"Unable to resolve backend for tool {tool_name}")

            post_tool_hook_state = PostToolHookRuntimeState(
                tool_call=tool_call,
                backend=backend,
                tool=tool,
                execution_time_ms=exec_duration_ms,
                is_last_tool_call_in_iteration=_is_last_tool_call_in_iteration(
                    tool_use_id,
                    assistant_message,
                ),
            )
            post_hook_start = time.time()
            async for hook_result in run_post_tool_use_hooks(
                hook_registry,
                tool_name,
                processed_input,
                result,
                tool_use_id,
                context,
                post_tool_hook_state=post_tool_hook_state,
            ):
                if hook_result.message and isinstance(hook_result.message, dict):
                    result_messages.append(hook_result.message)
                if hook_result.updated_tool_output is not None:
                    result = _coerce_hook_updated_tool_output(
                        result,
                        hook_result.updated_tool_output,
                    )
                if hook_result.prevent_continuation:
                    prevent_continuation = True
                    stop_reason = hook_result.stop_reason or stop_reason

            post_hook_duration_ms = (time.time() - post_hook_start) * 1000
            if post_hook_duration_ms > 500:
                logger.debug(
                    "Post-tool hooks for %s took %.0fms",
                    tool_name, post_hook_duration_ms,
                )

            tool_result_msg, result_content, is_error = _build_tool_result_message_from_result(
                tool_use_id,
                tool_name,
                result,
            )

        # Persist only after post-tool hooks have had a chance to inspect or
        # replace the raw result; the final message gets the real tool_use_id.
        result = _maybe_persist_tool_result_for_pipeline(
            tool, tool_use_id, tool_name, result, context,
        )
        _mark_dynamic_skill_paths_from_result(result, context)
        tool_result_msg, result_content, is_error = _build_tool_result_message_from_result(
            tool_use_id,
            tool_name,
            result,
        )
        pipeline_status = "error" if is_error else "success"
        pipeline_execution_time_ms = exec_duration_ms
        pipeline_result_size_chars = content_text_size(result.content)
        pipeline_tool_result_metadata = dict(result.metadata or {})
        pipeline_message_meta = dict(tool_result_msg.get("_meta") or {})
        pipeline_error_type = (
            str(pipeline_message_meta.get("error_type"))
            if pipeline_message_meta.get("error_type")
            else None
        )
        pipeline_result_preview = str(result_content or "")[:500]

        # Implementation: contextModifier — tool.call can return a context modifier
        # OS: tools can set this on the ToolResult metadata
        cm = getattr(result, "context_modifier", None)
        if cm is None:
            cm = (result.metadata or {}).get("context_modifier")
        if callable(cm):
            context_modifier = cm

        # Insert tool_result message before hook messages. The orchestration
        # layer later moves all follow-up attachment messages after every
        # tool_result in the assistant turn to preserve OpenAI tool pairing.
        result_messages.insert(0, tool_result_msg)
        additional_messages = _get_tool_additional_messages(
            result,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
        )
        if additional_messages:
            result_messages[1:1] = additional_messages

    except asyncio.CancelledError:
        if not context.is_aborted():
            raise
        logger.debug("Tool pipeline cancelled after abort: %s/%s", tool_name, tool_use_id)
        await context.emit_event("tool_cancelled", {
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
        })
        result_messages.append(build_tool_result_stop_message(tool_use_id, tool_name))

    except Exception as exc:
        # Implementation: outer try/catch in runToolUse — catch unexpected errors
        error_content = format_tool_error(exc)
        logger.error(
            "Unexpected error in tool execution pipeline for %s: %s",
            tool_name, error_content[:500],
            exc_info=True,
        )
        result_messages.append({
            "role": "tool",
            "name": tool_name,
            "content": f"Error: <tool_use_error>\nError calling tool {tool_name}: {error_content}\n</tool_use_error>",
            "tool_call_id": tool_use_id,
            "_meta": {
                "type": "tool_result",
                "tool_name": tool_name,
                "tool_call_id": tool_use_id,
                "status": "error",
                "error_type": "pipeline_error",
                "timestamp": time.time(),
            },
        })
        pipeline_status = "error"
        pipeline_error_type = "pipeline_error"

    return await _complete(ToolCallResult(
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        messages=result_messages,
        prevent_continuation=prevent_continuation,
        stop_reason=stop_reason,
        context_modifier=context_modifier,
    ))


# ═══════════════════════════════════════════════════════════════════════
# §8  Internal helpers
# ═══════════════════════════════════════════════════════════════════════

async def _record_pipeline_quality_outcome(
    context: ToolUseContext,
    *,
    tool: BaseTool | None,
    tool_use_id: str,
    status: str,
    error_type: str | None,
    permission_status: str | None,
    execution_time_ms: float,
    result_preview: str,
) -> None:
    """Record every terminal pipeline outcome once for quality accounting."""

    if tool is None or context.quality_manager is None:
        return

    recorded_ids = getattr(context, "quality_recorded_tool_use_ids", None)
    if isinstance(recorded_ids, set) and tool_use_id in recorded_ids:
        return

    qm = context.quality_manager
    record_outcome = getattr(qm, "record_outcome", None)
    if not callable(record_outcome):
        return

    normalized_status = str(status or "").lower()
    success = normalized_status == "success"
    error_message = None
    if not success:
        parts = [
            value
            for value in (
                error_type or normalized_status or "error",
                f"permission={permission_status}" if permission_status else "",
                result_preview[:400] if result_preview else "",
            )
            if value
        ]
        error_message = " | ".join(parts)[:500]

    try:
        quality_record = record_outcome(
            tool,
            success=success,
            execution_time_ms=float(execution_time_ms or 0.0),
            error_message=error_message,
        )
        if inspect.isawaitable(quality_record):
            quality_record = await quality_record
        if isinstance(recorded_ids, set):
            recorded_ids.add(tool_use_id)
        if quality_record is not None:
            try:
                from openspace.services.tooling.hooks import (
                    _emit_tool_quality_evidence,
                )

                await _emit_tool_quality_evidence(
                    context,
                    qm,
                    quality_record,
                    tool_use_id=tool_use_id,
                    execution_time_ms=float(execution_time_ms or 0.0),
                    source="pipeline_complete",
                )
            except Exception:
                logger.debug("Tool quality evidence emit failed", exc_info=True)
    except Exception:
        logger.warning(
            "Pipeline quality accounting failed for %s/%s",
            getattr(tool, "name", "unknown"),
            tool_use_id,
            exc_info=True,
        )


async def _emit_permission_denied(
    hook_registry: Any | None,
    tool_name: str,
    processed_input: dict[str, Any],
    deny_content: str,
    tool_use_id: str,
    context: ToolUseContext,
) -> bool:
    """Emit runtime and hook notifications for a terminal permission deny."""
    await context.emit_event("tool_permission_denied", {
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "reason": deny_content,
    })

    try:
        from openspace.services.tooling.hooks import run_permission_denied_hooks

        return await run_permission_denied_hooks(
            hook_registry,
            tool_name,
            processed_input,
            deny_content,
            tool_use_id,
            context,
        )
    except Exception:
        logger.warning(
            "PermissionDenied hooks failed for %s/%s",
            tool_name,
            tool_use_id,
            exc_info=True,
        )
        return False


async def _record_skill_permission_denied_from_tool_execution(
    tool_name: str,
    processed_input: dict[str, Any],
    context: ToolUseContext,
    *,
    reason: str,
) -> None:
    try:
        from openspace.skill_engine.protocol import (
            SKILL_TOOL_NAME,
            record_skill_permission_decision_for_context,
        )

        if tool_name != SKILL_TOOL_NAME:
            return
        await record_skill_permission_decision_for_context(
            context,
            processed_input,
            "permission_denied",
            source="tool_execution_permission",
            metadata={"reason": reason or "permission ask denied"},
        )
    except Exception:
        logger.debug("Skill permission deny event record failed", exc_info=True)


async def _record_skill_permission_granted_from_tool_execution(
    tool_name: str,
    processed_input: dict[str, Any],
    context: ToolUseContext,
    *,
    reason: str,
) -> None:
    try:
        from openspace.skill_engine.protocol import (
            SKILL_TOOL_NAME,
            record_skill_permission_decision_for_context,
        )

        if tool_name != SKILL_TOOL_NAME:
            return
        await record_skill_permission_decision_for_context(
            context,
            processed_input,
            "permission_granted",
            source="tool_execution_permission",
            metadata={"reason": reason or "permission ask allowed"},
        )
    except Exception:
        logger.debug("Skill permission grant event record failed", exc_info=True)


def _extract_permission_denied_reason(message: dict[str, Any]) -> str:
    """Return the deny reason stored in a tool result message."""
    content = message.get("content", "")
    if isinstance(content, str):
        return content.removeprefix("Error: ").strip()
    return extract_text_from_content(content).removeprefix("Error: ").strip()


def _validate_schema(
    tool: BaseTool,
    input: dict[str, Any],
) -> jsonschema.ValidationError | None:
    """Validate input against tool's JSON Schema.

    Implementation: ``tool.inputSchema.safeParse(input)`` (Zod validation).
    OS uses jsonschema.  Returns the ValidationError on failure, None on success.
    """
    schema = tool.schema.parameters if tool.schema else {}
    if not schema or not isinstance(schema, dict):
        return None

    # Only validate if schema has properties defined
    if "properties" not in schema and "type" not in schema:
        return None

    validation_input = dict(input)
    if getattr(tool, "name", "") == "bash":
        # OpenSpace keeps _simulatedSedEdit in BashTool's full internal input type but
        # omits it from the model-facing schema.  Permission UI can inject it
        # after preview approval, so schema validation must ignore it without
        # exposing it to the model.
        validation_input.pop("_simulatedSedEdit", None)

    try:
        jsonschema.validate(instance=validation_input, schema=schema)
        return None
    except jsonschema.ValidationError as ve:
        return ve


async def _resolve_permissions(
    tool: BaseTool,
    input: dict[str, Any],
    context: ToolUseContext,
    *,
    hook_permission_result: dict[str, Any] | None = None,
) -> PermissionCheckResult:
    """Resolve final permission decision.

    Resolution order:
    1. Hook's ``deny`` wins immediately.
    2. Hook's ``ask`` propagates (bypass-immune).
    3. Hook's ``allow`` still goes through the permission engine so settings
       deny/ask rules cannot be bypassed.
    4. Otherwise, call :func:`has_permissions_to_use_tool`.
    """
    from openspace.tool_runtime.permissions import (
        has_permissions_to_use_tool as _engine_has_perms,
    )
    from openspace.grounding.core.permissions.types import (
        PermissionAllow,
        PermissionAsk,
        PermissionDeny,
    )

    # 1. Hook hard decisions take precedence
    if hook_permission_result:
        behavior = hook_permission_result.get("behavior")
        if behavior == "deny":
            return PermissionDeny(
                message=hook_permission_result.get("message") or "Denied by hook",
                decision_reason=_hook_reason(hook_permission_result),
            )
        if behavior == "ask":
            return PermissionAsk(
                message=hook_permission_result.get("message") or f"Allow {tool.name}?",
                updated_input=hook_permission_result.get("updated_input"),
                decision_reason=_hook_reason(hook_permission_result),
            )
        if behavior == "allow":
            # Implementation: "hook allow doesn't bypass settings deny/ask"
            updated = hook_permission_result.get("updated_input")
            if _should_skip_permission_recheck_after_user_interaction(tool, updated):
                return PermissionAllow(
                    updated_input=dict(updated),
                    decision_reason=_hook_reason(hook_permission_result),
                )
            next_input = updated if updated is not None else input
            engine_decision = await _engine_has_perms(tool, next_input, context)
            if isinstance(engine_decision, PermissionAllow):
                # Merge hook's updated_input if engine didn't provide one
                if engine_decision.updated_input is None and updated is not None:
                    return PermissionAllow(
                        updated_input=updated,
                        decision_reason=engine_decision.decision_reason,
                    )
            return engine_decision

    # 2. No hook — run the engine
    return await _engine_has_perms(tool, input, context)


def _should_skip_permission_recheck_after_user_interaction(
    tool: BaseTool | None,
    updated_input: Any,
) -> bool:
    """Return True when an updated input already contains the user response.

    OpenSpace treats a PermissionRequest/PreToolUse hook ``allow`` with
    ``updatedInput`` for ``requiresUserInteraction`` tools as completed user
    interaction; the local UI is not shown again.  OS keeps that behavior
    guarded by an optional tool-level completeness check.
    """

    if tool is None or not getattr(tool, "requires_user_interaction", False):
        return False
    if not isinstance(updated_input, dict):
        return False
    checker = getattr(tool, "is_user_interaction_complete", None)
    if callable(checker):
        try:
            return bool(checker(updated_input))
        except Exception:
            logger.debug(
                "User interaction completeness check failed for %s",
                getattr(tool, "name", "tool"),
                exc_info=True,
            )
            return False
    return True


def _hook_reason(hook_result: dict[str, Any]):
    """Convert a hook payload into a :class:`DecisionReasonHook`."""
    from openspace.grounding.core.permissions.types import DecisionReasonHook

    return DecisionReasonHook(
        hook_name=hook_result.get("hook_name", "pre_tool_use"),
        hook_source=hook_result.get("hook_source"),
        reason=hook_result.get("reason"),
    )


def _merge_hook_permission_result(
    current: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Merge PreToolUse permission decisions with strict precedence.

    Multiple hooks can emit permission decisions for the same tool call. The
    effective ordering is ``deny > ask > allow``; a later permissive hook must
    not erase an earlier ask/deny decision.
    """

    if current is None:
        return dict(incoming)

    order = {"deny": 3, "ask": 2, "allow": 1}
    current_behavior = str(current.get("behavior") or "")
    incoming_behavior = str(incoming.get("behavior") or "")
    current_rank = order.get(current_behavior, 0)
    incoming_rank = order.get(incoming_behavior, 0)

    if incoming_rank > current_rank:
        return dict(incoming)

    merged = dict(current)
    if incoming_rank == current_rank and incoming_rank > 0:
        merged.update({k: v for k, v in incoming.items() if v is not None})
        return merged

    return merged


# ═══════════════════════════════════════════════════════════════════════
# TUI permission dialog — 4-option protocol
# ═══════════════════════════════════════════════════════════════════════
#
# When ``_handle_permission_ask`` runs, it emits a ``tool_permission_ask``
# event whose payload includes the 4 options the TUI should render:
#
#   option_id       | label               | effect
#   ----------------+---------------------+--------------------------------
#   allow_once      | "Allow once"        | proceed, no rule change
#   allow_always    | "Always allow"      | add allow rule(s) via loader
#                   |   (derived patterns)| to ``localSettings`` (Q2=A)
#   deny            | "Deny"              | stop, return tool_result error
#   provide_input   | "Provide custom"    | user edits ``tool_input`` and
#                   |                     | the tool is re-invoked via
#                   |                     | ``updated_input``
#
# A fully native TUI may respond with ``tool_permission_response`` carrying
# ``{option_id, tool_use_id, permission_ask_id?, selected_suggestion?,
# edited_input?}``.  The current Python bridge also supports this as a
# transitional shim by converting ``tool_permission_ask`` into prompt_request
# round trips before resolving the same pending future.  In both paths,
# malformed responses fail closed.


_PENDING_ASKS: dict[str, asyncio.Future] = {}
_PENDING_TOOL_USE_ID_BY_ASK_ID: dict[str, str] = {}
_PENDING_ASK_IDS_BY_TOOL_USE_ID: dict[str, set[str]] = {}
_ASK_TIMEOUT_SECONDS: float = 300.0  # 5-minute UX timeout


def _permission_ask_id(context: ToolUseContext, tool_use_id: str) -> str:
    session_id = str(getattr(context, "session_id", None) or "no-session")
    agent_id = str(getattr(context, "agent_id", None) or "primary")
    return f"{session_id}:{agent_id}:{tool_use_id}"


def _register_pending_ask(
    *,
    ask_id: str,
    tool_use_id: str,
    future: asyncio.Future,
) -> None:
    _PENDING_ASKS[ask_id] = future
    _PENDING_TOOL_USE_ID_BY_ASK_ID[ask_id] = tool_use_id
    _PENDING_ASK_IDS_BY_TOOL_USE_ID.setdefault(tool_use_id, set()).add(ask_id)


def _drop_pending_ask(ask_id: str) -> None:
    tool_use_id = _PENDING_TOOL_USE_ID_BY_ASK_ID.pop(ask_id, None)
    _PENDING_ASKS.pop(ask_id, None)
    if tool_use_id is None:
        return
    ask_ids = _PENDING_ASK_IDS_BY_TOOL_USE_ID.get(tool_use_id)
    if ask_ids is None:
        return
    ask_ids.discard(ask_id)
    if not ask_ids:
        _PENDING_ASK_IDS_BY_TOOL_USE_ID.pop(tool_use_id, None)


def _live_ask_ids_for_tool_use_id(tool_use_id: str) -> list[str]:
    ask_ids = _PENDING_ASK_IDS_BY_TOOL_USE_ID.get(tool_use_id, set())
    return [
        ask_id
        for ask_id in sorted(ask_ids)
        if (future := _PENDING_ASKS.get(ask_id)) is not None and not future.done()
    ]


def _resolve_permission_ask_by_id(
    ask_id: str,
    response: dict[str, Any],
) -> bool:
    fut = _PENDING_ASKS.get(ask_id)
    if fut is None or fut.done():
        _drop_pending_ask(ask_id)
        return False
    _drop_pending_ask(ask_id)
    fut.set_result(response)
    return True


async def _emit_tool_permission_ask(
    context: ToolUseContext,
    payload: dict[str, Any],
) -> bool:
    """Emit a permission ask without silently masking delivery failure."""
    sink = getattr(context, "event_sink", None)
    if sink is None:
        return False

    try:
        result = sink("tool_permission_ask", payload)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.warning(
            "tool_permission_ask delivery failed for %s",
            payload.get("tool_use_id"),
            exc_info=True,
        )
        return False

    return True


async def _emit_tool_permission_cancel(
    context: ToolUseContext,
    tool_use_id: str,
    reason: str,
    permission_ask_id: str | None = None,
) -> None:
    """Best-effort notification that the ask future is no longer live."""
    sink = getattr(context, "event_sink", None)
    if sink is None:
        return

    try:
        result = sink(
            "tool_permission_cancel",
            {
                "tool_use_id": tool_use_id,
                "permission_ask_id": permission_ask_id,
                "reason": reason,
            },
        )
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.debug(
            "tool_permission_cancel delivery failed for %s",
            tool_use_id,
            exc_info=True,
        )


def _build_tool_permission_interaction_payload(
    tool: BaseTool | None,
    input: dict[str, Any],
) -> dict[str, Any]:
    """Add structured interaction metadata for tools with dedicated prompts."""

    if tool is None:
        return {}
    tool_name = getattr(tool, "name", "")
    aliases = set(getattr(tool, "aliases", ()) or ())
    if tool_name != "ask_user_question" and "AskUserQuestion" not in aliases:
        return {}

    payload: dict[str, Any] = {
        "interaction": "ask_user_question",
        "requires_user_interaction": True,
        "questions": input.get("questions", []),
    }
    metadata = input.get("metadata")
    if isinstance(metadata, dict):
        payload["metadata"] = metadata
    return payload


_HIGH_RISK_BASH_RE = re.compile(
    r"(?is)"
    r"(\brm\s+[^|;&]*(-[^\s]*r|-R|--recursive|--no-preserve-root)\b"
    r"|\bsudo\b"
    r"|\bdd\s+"
    r"|\bmkfs(?:\.[a-z0-9]+)?\b"
    r"|\bchmod\s+-R\b"
    r"|\bchown\s+-R\b"
    r"|\bpkill\b|\bkillall\b"
    r"|\bshutdown\b|\breboot\b"
    r"|\bcurl\b[^|;&]*\|\s*(?:sh|bash)\b"
    r"|\bwget\b[^|;&]*\|\s*(?:sh|bash)\b)"
)
_LOW_RISK_BASH_RE = re.compile(
    r"(?is)^\s*("
    r"pwd\b|ls\b|cat\b|head\b|tail\b|wc\b|rg\b|grep\b|find\b|"
    r"git\s+(?:status|diff|log|show|branch)\b|"
    r"python(?:3)?\s+-m\s+pytest\b|pytest\b|npm\s+(?:test|run\s+test)\b"
    r")"
)


def _infer_tool_permission_risk_level(
    tool_name: str,
    input: dict[str, Any],
    *,
    blocked_path: str | None,
    decision_reason: dict[str, Any] | None,
) -> str:
    """Return the TUI risk badge for tool-level permission prompts."""

    if blocked_path:
        return "high"

    reason_type = decision_reason.get("type") if decision_reason else None
    if (
        reason_type == "safetyCheck"
        and decision_reason
        and decision_reason.get("classifier_approvable") is False
    ):
        return "high"

    if tool_name == BASH_TOOL_NAME:
        command = str(input.get("command") or "")
        if _HIGH_RISK_BASH_RE.search(command):
            return "high"
        if _LOW_RISK_BASH_RE.search(command) and not re.search(r"(?s)(^|[^>])>{1,2}[^>]", command):
            return "low"
        return "medium"

    if tool_name in {FILE_WRITE_TOOL_NAME, FILE_EDIT_TOOL_NAME, NOTEBOOK_EDIT_TOOL_NAME}:
        return "medium"

    return "medium"


def resolve_permission_ask(tool_use_id: str, response: dict[str, Any]) -> bool:
    """Called by TUI bridge to resolve a pending ask.

    ``response`` keys:
      - ``option_id``:          one of "allow_once"/"allow_always"/"deny"/"provide_input"
      - ``suggestion_index`` / ``selected_suggestion``: index into
                                 ``suggestions`` when option_id is
                                 "allow_always" (None → no persistence)
      - ``edited_input``:       dict when option_id is "provide_input"
    Returns True if a pending ask was found and resolved.
    """
    requested_ask_id = ""
    if isinstance(response, dict):
        requested_ask_id = str(
            response.get("permission_ask_id") or response.get("ask_id") or ""
        ).strip()

    if requested_ask_id:
        expected_tool_use_id = _PENDING_TOOL_USE_ID_BY_ASK_ID.get(requested_ask_id)
        if expected_tool_use_id is not None and expected_tool_use_id != tool_use_id:
            return False
        return _resolve_permission_ask_by_id(requested_ask_id, response)

    if tool_use_id in _PENDING_ASKS:
        return _resolve_permission_ask_by_id(tool_use_id, response)

    ask_ids = _live_ask_ids_for_tool_use_id(tool_use_id)
    if len(ask_ids) != 1:
        return False
    return _resolve_permission_ask_by_id(ask_ids[0], response)


def reject_permission_ask(tool_use_id: str, reason: str) -> bool:
    """Fail a pending permission ask closed with an explicit deny response."""

    response = {
        "option_id": "deny",
        "message": reason,
    }
    if tool_use_id in _PENDING_ASKS:
        return _resolve_permission_ask_by_id(tool_use_id, response)

    ask_ids = _live_ask_ids_for_tool_use_id(tool_use_id)
    resolved = False
    for ask_id in ask_ids:
        resolved = _resolve_permission_ask_by_id(ask_id, response) or resolved
    return resolved


def is_permission_ask_pending(tool_use_id: str) -> bool:
    """Return whether *tool_use_id* still has a live unresolved ask."""

    fut = _PENDING_ASKS.get(tool_use_id)
    if fut is not None and not fut.done():
        return True
    return bool(_live_ask_ids_for_tool_use_id(tool_use_id))


def pending_permission_ask_ids() -> tuple[str, ...]:
    """Return live unresolved ask ids in deterministic registry order."""

    tool_use_ids: list[str] = []
    seen: set[str] = set()
    for ask_id in sorted(_PENDING_ASKS):
        fut = _PENDING_ASKS.get(ask_id)
        if fut is None or fut.done():
            continue
        tool_use_id = _PENDING_TOOL_USE_ID_BY_ASK_ID.get(ask_id, ask_id)
        if tool_use_id in seen:
            continue
        seen.add(tool_use_id)
        tool_use_ids.append(tool_use_id)
    return tuple(tool_use_ids)


async def _handle_permission_ask(
    tool: BaseTool,
    input: dict[str, Any],
    context: ToolUseContext,
    *,
    tool_use_id: str,
    tool_name: str,
    message: str | None = None,
    suggestions: tuple[Any, ...] | None = None,
    blocked_path: str | None = None,
    decision_reason: Any = None,
) -> PermissionAskResolution:
    """Prompt the user for an ``ask`` decision and persist "always allow"
    rules when chosen.

    Implementation: ``canUseTool`` when decision is ``ask`` (PermissionRequest
    flow in ``PermissionsLayer`` React component).

    Returns:
      - ``deny_message`` populated when the user denied or timed out —
        the caller appends this and short-circuits execution.
      - ``updated_input`` populated when the user edited the tool input.

    When ``option_id == "allow_always"`` and the tool result carried
    ``suggestions``, the selected suggestion is persisted via
    :func:`openspace.grounding.core.permissions.persist_permission_updates`
    to ``.openspace/settings.local.json`` (Q2 = A).
    """
    from openspace.tool_runtime.permissions import (
        persist_permission_updates,
        apply_permission_update,
    )
    from openspace.grounding.core.permissions.types import (
        AddRulesUpdate,
        PermissionRuleValue,
    )

    hook_resolution = await _run_permission_request_hooks(
        input,
        context,
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        tool=tool,
        suggestions=suggestions,
    )
    if hook_resolution is not None:
        return hook_resolution

    if (
        not getattr(context, "tui_available", True)
        or getattr(context, "event_sink", None) is None
    ):
        logger.info(
            "tool_permission_ask for %s in headless mode -> auto-deny", tool_name
        )
        return PermissionAskResolution(
            deny_message=_build_tool_denied_message(
                tool_use_id,
                tool_name,
                "No interactive TUI available (headless mode); add an allow rule "
                "in .openspace/settings.local.json or re-run with "
                "--permission-mode acceptEdits/bypassPermissions.",
            )
        )

    # Assemble the 4-option payload.
    options: list[dict[str, Any]] = [
        {"option_id": "allow_once", "label": "Allow once"},
    ]
    if suggestions:
        for idx, sug in enumerate(suggestions):
            # OpenSpace formats the suggestion label from the rule values.
            label = _format_suggestion_label(sug)
            options.append(
                {
                    "option_id": "allow_always",
                    "suggestion_index": idx,
                    "label": f"Always allow: {label}",
                }
            )
    else:
        # Fallback — offer a tool-wide always-allow when no suggestions.
        options.append(
            {
                "option_id": "allow_always",
                "suggestion_index": None,
                "label": f"Always allow: {tool_name}",
            }
        )
    options.append({"option_id": "deny", "label": "Deny"})
    options.append({"option_id": "provide_input", "label": "Edit input and retry"})

    # Register a pending ask and wait for TUI response.
    ask_id = _permission_ask_id(context, tool_use_id)
    existing = _PENDING_ASKS.get(ask_id)
    if existing is not None and not existing.done():
        return PermissionAskResolution(
            deny_message=_build_tool_denied_message(
                tool_use_id,
                tool_name,
                "Duplicate permission prompt id is already pending.",
            )
        )

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_ask(ask_id=ask_id, tool_use_id=tool_use_id, future=fut)
    try:
        serialized_decision_reason = _serialize_decision_reason(decision_reason)
        payload = {
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "permission_ask_id": ask_id,
            "message": message or f"Allow {tool_name}?",
            "description": message or f"Allow {tool_name}?",
            "tool_input": input,
            "blocked_path": blocked_path,
            "request_kind": "tool",
            "risk_level": _infer_tool_permission_risk_level(
                tool_name,
                input,
                blocked_path=blocked_path,
                decision_reason=serialized_decision_reason,
            ),
            "options": options,
            "decision_reason": serialized_decision_reason,
        }
        payload.update(_build_tool_permission_interaction_payload(tool, input))
        if not await _emit_tool_permission_ask(context, payload):
            return PermissionAskResolution(
                deny_message=_build_tool_denied_message(
                    tool_use_id,
                    tool_name,
                    "Interactive permission prompt unavailable because the permission event could not be delivered.",
                )
            )
        response = await asyncio.wait_for(fut, timeout=_ASK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        timeout_message = (
            f"Permission prompt timed out after {_ASK_TIMEOUT_SECONDS:.0f}s."
        )
        await _emit_tool_permission_cancel(
            context,
            tool_use_id,
            timeout_message,
            ask_id,
        )
        return PermissionAskResolution(
            deny_message=_build_tool_denied_message(
                tool_use_id,
                tool_name,
                timeout_message,
            )
        )
    finally:
        if _PENDING_ASKS.get(ask_id) is fut:
            _drop_pending_ask(ask_id)

    # Fail-closed parsing of the bridge response. An old TUI, a malformed
    # payload, or even an empty ``{}`` must NEVER silently fall through to
    # "allow_once" — that would be a privilege escalation for protected tools.
    if not isinstance(response, dict):
        logger.warning(
            "tool_permission_ask for %s returned non-dict response (%r) -> deny",
            tool_name,
            type(response).__name__,
        )
        return PermissionAskResolution(
            deny_message=_build_tool_denied_message(
                tool_use_id,
                tool_name,
                "Permission response was malformed (expected JSON object).",
            )
        )

    option_id = response.get("option_id")
    _VALID_OPTION_IDS = {"allow_once", "allow_always", "deny", "provide_input"}
    if option_id not in _VALID_OPTION_IDS:
        logger.warning(
            "tool_permission_ask for %s returned unrecognized option_id=%r -> deny",
            tool_name,
            option_id,
        )
        return PermissionAskResolution(
            deny_message=_build_tool_denied_message(
                tool_use_id,
                tool_name,
                f"Permission response had an unrecognized option_id={option_id!r}; "
                "refusing to proceed.",
            )
        )

    if option_id == "deny":
        deny_reason = str(response.get("message") or "Denied by user.").strip()
        return PermissionAskResolution(
            deny_message=_build_tool_denied_message(
                tool_use_id,
                tool_name,
                deny_reason or "Denied by user.",
            )
        )

    if option_id == "allow_once":
        updated = response.get("updated_input")
        if updated is not None and not isinstance(updated, dict):
            return PermissionAskResolution(
                deny_message=_build_tool_denied_message(
                    tool_use_id,
                    tool_name,
                    "Permission response updated_input must be a JSON object.",
                )
            )

        candidate_input = updated if isinstance(updated, dict) else input
        if getattr(tool, "requires_user_interaction", False):
            checker = getattr(tool, "is_user_interaction_complete", None)
            try:
                complete = bool(checker(candidate_input)) if callable(checker) else True
            except Exception:
                complete = False
            if not complete:
                return PermissionAskResolution(
                    deny_message=_build_tool_denied_message(
                        tool_use_id,
                        tool_name,
                        "Interactive permission response did not include complete user answers.",
                    )
                )

        return PermissionAskResolution(
            updated_input=dict(updated) if isinstance(updated, dict) else None,
            skip_permission_recheck=_should_skip_permission_recheck_after_user_interaction(
                tool,
                candidate_input,
            ),
        )

    if option_id == "allow_always" and getattr(tool, "requires_user_interaction", False):
        return PermissionAskResolution(
            deny_message=_build_tool_denied_message(
                tool_use_id,
                tool_name,
                "Interactive tools cannot be always-allowed without a user answer.",
            )
        )

    if option_id == "allow_always":
        updates: tuple[Any, ...] = ()
        sug_idx = response.get("suggestion_index", response.get("selected_suggestion"))
        if suggestions:
            # Suggestions were offered: require a valid index. Do NOT degrade
            # to a tool-wide allow rule — that would be broader than what the
            # user was actually presented with in the TUI.
            if (
                not isinstance(sug_idx, int)
                or isinstance(sug_idx, bool)
                or not (0 <= sug_idx < len(suggestions))
            ):
                logger.warning(
                    "tool_permission_ask for %s: allow_always with invalid "
                    "suggestion_index=%r (have %d suggestions) -> deny",
                    tool_name,
                    sug_idx,
                    len(suggestions),
                )
                return PermissionAskResolution(
                    deny_message=_build_tool_denied_message(
                        tool_use_id,
                        tool_name,
                        "Permission response selected 'allow_always' but "
                        f"suggestion_index={sug_idx!r} is invalid.",
                    )
                )
            updates = (suggestions[sug_idx],)
        else:
            # No suggestions were offered; the TUI only showed a tool-wide
            # "Always allow: <tool>" button, so tool-wide persistence is the
            # expected outcome here.
            if sug_idx is not None:
                logger.warning(
                    "tool_permission_ask for %s: allow_always returned "
                    "unexpected suggestion_index=%r with no suggestions -> deny",
                    tool_name,
                    sug_idx,
                )
                return PermissionAskResolution(
                    deny_message=_build_tool_denied_message(
                        tool_use_id,
                        tool_name,
                        "Permission response selected 'allow_always' with "
                        f"unexpected suggestion_index={sug_idx!r}.",
                    )
                )
            updates = (
                AddRulesUpdate(
                    destination="localSettings",
                    rules=(PermissionRuleValue(tool_name=tool_name),),
                    behavior="allow",
                ),
            )

        for update in updates:
            try:
                if context.permission_context is not None:
                    context.permission_context = apply_permission_update(
                        update, context.cwd, context.permission_context
                    )
                # persist_permission_updates is sync disk I/O — offload.
                await asyncio.to_thread(
                    persist_permission_updates, (update,), context.cwd
                )
                logger.info(
                    "Persisted always-allow rule for %s from user prompt",
                    tool_name,
                )
            except Exception as e:  # pragma: no cover — IO errors
                logger.warning(
                    "Failed to persist always-allow rule: %s", e
                )
        return PermissionAskResolution()

    if option_id == "provide_input":
        edited = response.get("edited_input")
        if isinstance(edited, dict):
            return PermissionAskResolution(updated_input=dict(edited))
        return PermissionAskResolution(
            deny_message=_build_tool_denied_message(
                tool_use_id,
                tool_name,
                "Edited input must be a JSON object.",
            )
        )

    # option_id == "allow_once" is handled above. The branch below is retained
    # for defensive exhaustiveness if _VALID_OPTION_IDS changes.
    return PermissionAskResolution()


async def _run_permission_request_hooks(
    input: dict[str, Any],
    context: ToolUseContext,
    *,
    tool_use_id: str,
    tool_name: str,
    tool: BaseTool | None = None,
    suggestions: tuple[Any, ...] | None = None,
) -> PermissionAskResolution | None:
    """Run OpenSpace PermissionRequest hooks before the interactive ask UI.

    Hooks can allow or deny the pending request. Returning ``ask`` or no
    decision falls through to the existing OS prompt path.
    """

    hook_registry = getattr(context, "hook_registry", None)
    if hook_registry is None:
        return None

    from openspace.services.tooling.hooks import HookEvent

    if not hook_registry.has_hook_for_event(HookEvent.PERMISSION_REQUEST, tool_name):
        return None

    serialized_suggestions = [
        _serialize_permission_update_for_hook(item)
        for item in (suggestions or ())
    ]
    async for agg in hook_registry.execute_hooks(
        HookEvent.PERMISSION_REQUEST,
        tool_name,
        hook_kwargs={
            "tool_name": tool_name,
            "tool_input": input,
            "tool_use_id": tool_use_id,
            "permission_suggestions": serialized_suggestions,
            "context": context,
        },
        context=context,
        abort_event=getattr(context, "abort_event", None),
    ):
        if agg.blocking_error:
            return PermissionAskResolution(
                deny_message=_build_tool_denied_message(
                    tool_use_id,
                    tool_name,
                    agg.blocking_error.blocking_error,
                ),
                prevent_continuation=agg.prevent_continuation,
                stop_reason=agg.stop_reason,
            )
        if agg.prevent_continuation:
            return PermissionAskResolution(
                deny_message=_build_tool_denied_message(
                    tool_use_id,
                    tool_name,
                    agg.stop_reason or "Permission request stopped by hook.",
                ),
                prevent_continuation=True,
                stop_reason=agg.stop_reason,
            )
        if agg.permission_behavior == "allow":
            if agg.updated_permissions:
                await _apply_permission_updates_from_hook(
                    agg.updated_permissions,
                    context,
                    tool_name=tool_name,
                )
            updated_input = (
                dict(agg.updated_input)
                if isinstance(agg.updated_input, dict)
                else None
            )
            return PermissionAskResolution(
                updated_input=updated_input,
                skip_permission_recheck=_should_skip_permission_recheck_after_user_interaction(
                    tool,
                    updated_input,
                ),
            )
        if agg.permission_behavior == "deny":
            return PermissionAskResolution(
                deny_message=_build_tool_denied_message(
                    tool_use_id,
                    tool_name,
                    agg.hook_permission_decision_reason
                    or "Permission request denied by hook.",
                ),
                prevent_continuation=agg.prevent_continuation,
                stop_reason=agg.stop_reason,
            )
        if agg.permission_behavior == "ask":
            continue
    return None


async def _apply_permission_updates_from_hook(
    updates_payload: list[Any],
    context: ToolUseContext,
    *,
    tool_name: str,
) -> None:
    from openspace.tool_runtime.permissions import (
        apply_permission_update,
        persist_permission_updates,
    )

    updates = _permission_updates_from_hook_payload(updates_payload)
    if not updates:
        return
    for update in updates:
        try:
            if context.permission_context is not None:
                context.permission_context = apply_permission_update(
                    update, context.cwd, context.permission_context
                )
            await asyncio.to_thread(persist_permission_updates, (update,), context.cwd)
            logger.info("Applied PermissionRequest hook update for %s", tool_name)
        except Exception as exc:
            logger.warning("Failed to apply PermissionRequest hook update: %s", exc)


def _permission_updates_from_hook_payload(items: list[Any]) -> tuple[Any, ...]:
    from openspace.grounding.core.permissions.types import (
        AddDirectoriesUpdate,
        AddRulesUpdate,
        RemoveDirectoriesUpdate,
        RemoveRulesUpdate,
        ReplaceRulesUpdate,
        SetModeUpdate,
        parse_rule_value,
    )

    parsed: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        update_type = item.get("type")
        destination = str(item.get("destination") or "localSettings")
        try:
            if update_type in {"addRules", "replaceRules", "removeRules"}:
                rules = tuple(parse_rule_value(str(raw)) for raw in item.get("rules") or [])
                behavior = str(item.get("behavior") or "allow")
                if update_type == "addRules":
                    parsed.append(AddRulesUpdate(destination, rules, behavior))  # type: ignore[arg-type]
                elif update_type == "replaceRules":
                    parsed.append(ReplaceRulesUpdate(destination, rules, behavior))  # type: ignore[arg-type]
                else:
                    parsed.append(RemoveRulesUpdate(destination, rules, behavior))  # type: ignore[arg-type]
            elif update_type == "setMode":
                parsed.append(SetModeUpdate(destination, str(item.get("mode") or "default")))  # type: ignore[arg-type]
            elif update_type == "addDirectories":
                parsed.append(
                    AddDirectoriesUpdate(
                        destination,
                        tuple(str(path) for path in item.get("directories") or ()),
                    )
                )
            elif update_type == "removeDirectories":
                parsed.append(
                    RemoveDirectoriesUpdate(
                        destination,
                        tuple(str(path) for path in item.get("directories") or ()),
                    )
                )
        except Exception as exc:
            logger.warning("Ignoring malformed PermissionRequest hook update: %s", exc)
    return tuple(parsed)


def _serialize_permission_update_for_hook(update: Any) -> dict[str, Any] | str:
    """Best-effort PermissionUpdate JSON for hook payloads."""

    from openspace.grounding.core.permissions.types import (
        AddDirectoriesUpdate,
        AddRulesUpdate,
        RemoveDirectoriesUpdate,
        RemoveRulesUpdate,
        ReplaceRulesUpdate,
        SetModeUpdate,
        format_rule_value,
    )

    if isinstance(update, (AddRulesUpdate, ReplaceRulesUpdate, RemoveRulesUpdate)):
        return {
            "type": update.type,
            "destination": update.destination,
            "behavior": update.behavior,
            "rules": [format_rule_value(rule) for rule in update.rules],
        }
    if isinstance(update, (AddDirectoriesUpdate, RemoveDirectoriesUpdate)):
        return {
            "type": update.type,
            "destination": update.destination,
            "directories": list(update.directories),
        }
    if isinstance(update, SetModeUpdate):
        return {
            "type": update.type,
            "destination": update.destination,
            "mode": update.mode,
        }
    return repr(update)


def _format_suggestion_label(suggestion: Any) -> str:
    """Best-effort human label for a PermissionUpdate suggestion."""
    from openspace.grounding.core.permissions.types import (
        AddRulesUpdate,
        AddDirectoriesUpdate,
        format_rule_value,
    )

    if isinstance(suggestion, AddRulesUpdate):
        rules = ", ".join(format_rule_value(r) for r in suggestion.rules)
        return f"{rules} ({suggestion.behavior})"
    if isinstance(suggestion, AddDirectoriesUpdate):
        return "+dir " + ", ".join(suggestion.directories)
    return repr(suggestion)


def _serialize_decision_reason(reason: Any) -> dict[str, Any] | None:
    """Serialize a :class:`PermissionDecisionReason` for the event sink."""
    if reason is None:
        return None
    t = getattr(reason, "type", None)
    out: dict[str, Any] = {"type": t}
    for attr in ("reason", "mode", "classifier_approvable", "hook_name"):
        if hasattr(reason, attr):
            out[attr] = getattr(reason, attr)
    return out


def _build_tool_denied_message(
    tool_use_id: str, tool_name: str, reason: str
) -> dict[str, Any]:
    """Produce the tool result message shown to the model after a deny."""
    return {
        "role": "tool",
        "name": tool_name,
        "content": f"Error: {reason}",
        "tool_call_id": tool_use_id,
        "_meta": {
            "type": "tool_result",
            "tool_name": tool_name,
            "tool_call_id": tool_use_id,
            "status": "denied",
            "error_type": "permission_denied",
            "timestamp": time.time(),
        },
    }


def _build_permission_denied_retry_message(
    tool_use_id: str, tool_name: str
) -> dict[str, Any]:
    """Build the model-visible retry hint for PermissionDenied hooks."""
    return {
        "role": "user",
        "content": (
            "The PermissionDenied hook indicated this command is now approved. "
            "You may retry it if you would like."
        ),
        "_meta": {
            "type": "permission_denied_retry",
            "hook_event": "PermissionDenied",
            "tool_name": tool_name,
            "tool_call_id": tool_use_id,
            "is_meta": True,
            "timestamp": time.time(),
        },
    }


# ═══════════════════════════════════════════════════════════════════════
# §9  Import guard for asyncio (used by _is_abort_error)
# ═══════════════════════════════════════════════════════════════════════
import asyncio  # noqa: E402 — needed for CancelledError check
