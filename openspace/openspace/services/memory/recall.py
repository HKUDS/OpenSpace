"""Relevant memory recall and async prefetch.

Implementation notes:
- ``memdir/findRelevantMemories.ts`` (141 lines)
- ``utils/attachments.ts`` relevant-memory helpers
- ``query.ts`` ``startRelevantMemoryPrefetch`` consume path

OpenSpace keeps the OpenSpace data flow: scan memory headers, ask a lightweight
model to select up to five filenames, read selected files with strict caps,
then inject them as ``relevant_memories`` system-reminder attachments only
after the prefetch has settled.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from openspace.services.conversation.messages import get_user_message_text
from openspace.services.conversation.side_query import run_side_query
from openspace.services.tooling.context import ReadFileEntry, ToolUseContext
from openspace.utils.logging import Logger

from .memdir import get_auto_mem_path, is_auto_memory_enabled
from .memory_scan import MemoryHeader, format_memory_manifest, scan_memory_files

logger = Logger.get_logger(__name__)

SELECT_MEMORIES_SYSTEM_PROMPT = """You are selecting memories that will be useful to OpenSpace as it processes a user's query. You will be given the user's query and a list of available memory files with their filenames and descriptions.

Return a list of filenames for the memories that will clearly be useful to OpenSpace as it processes the user's query (up to 5). Only include memories that you are certain will be helpful based on their name and description.
- If you are unsure if a memory will be useful in processing the user's query, then do not include it in your list. Be selective and discerning.
- If there are no memories in the list that would clearly be useful, feel free to return an empty list.
- If a list of recently-used tools is provided, do not select memories that are usage reference or API documentation for those tools (OpenSpace is already exercising them). DO still select memories containing warnings, gotchas, or known issues about those tools - active use is exactly when those matter.
"""

MEMORY_RECALL_MODEL_ENV = "OPENSPACE_MEMORY_RECALL_MODEL"
MEMORY_RECALL_MAX_TOKENS_ENV = "OPENSPACE_MEMORY_RECALL_MAX_TOKENS"
MEMORY_RECALL_ENABLED_ENV = "OPENSPACE_MEMORY_RECALL_ENABLED"

MEMORY_RECALL_MAX_TOKENS = 256
MEMORY_RECALL_MAX_RESULTS = 5
MAX_MEMORY_LINES = 200
MAX_MEMORY_BYTES = 4096
MAX_SESSION_BYTES = 60 * 1024

Attachment = dict[str, Any]


@dataclass(frozen=True)
class RelevantMemory:
    """A selected memory file, matching OpenSpace ``RelevantMemory``."""

    path: Path
    mtime_ms: float

    @property
    def mtimeMs(self) -> float:
        """legacy-compatible camelCase alias."""

        return self.mtime_ms


@dataclass(frozen=True)
class SurfacedMemory:
    """A selected memory file after bounded content has been read."""

    path: Path
    content: str
    mtime_ms: float
    header: str
    limit: int | None = None

    def to_attachment_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": str(self.path),
            "content": self.content,
            "mtimeMs": self.mtime_ms,
            "header": self.header,
        }
        if self.limit is not None:
            payload["limit"] = self.limit
        return payload


@dataclass
class MemoryPrefetch:
    """Async prefetch handle consumed by the agent loop."""

    task: asyncio.Task[list[Attachment]]
    fired_at: float
    settled_at: float | None = None
    consumed_on_iteration: int = -1

    @property
    def promise(self) -> asyncio.Task[list[Attachment]]:
        """legacy-compatible field name."""

        return self.task

    def is_settled(self) -> bool:
        if self.settled_at is not None:
            return True
        if self.task.done():
            self.settled_at = time.time()
            return True
        return False

    def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()


async def find_relevant_memories(
    query: str,
    memory_dir: str | Path,
    *,
    llm_client: Any,
    abort_event: asyncio.Event | None = None,
    recent_tools: Sequence[str] | None = None,
    already_surfaced: set[str] | None = None,
    model: str | None = None,
    max_results: int = MEMORY_RECALL_MAX_RESULTS,
    max_tokens: int = MEMORY_RECALL_MAX_TOKENS,
    use_json_schema: bool = True,
) -> list[RelevantMemory]:
    """Select memory files relevant to *query*.

    Mirrors OpenSpace ``findRelevantMemories``:
    - filters ``already_surfaced`` before the selector call
    - returns ``[]`` for empty manifests, aborts, malformed model output, and
      selector failures
    - filters selected filenames against the scanned manifest
    """

    if abort_event is not None and abort_event.is_set():
        return []

    surfaced = {str(Path(path).expanduser().resolve()) for path in already_surfaced or set()}
    memories = [
        memory
        for memory in scan_memory_files(memory_dir)
        if str(memory.file_path.resolve()) not in surfaced
    ]
    if not memories:
        return []

    selected_filenames = await _select_relevant_memory_filenames(
        query=query,
        memories=memories,
        llm_client=llm_client,
        abort_event=abort_event,
        recent_tools=recent_tools or (),
        model=model,
        max_tokens=max_tokens,
        use_json_schema=use_json_schema,
    )
    if not selected_filenames:
        return []

    by_filename = {memory.filename: memory for memory in memories}
    selected: list[RelevantMemory] = []
    seen: set[str] = set()
    for filename in selected_filenames:
        if filename in seen:
            continue
        seen.add(filename)
        memory = by_filename.get(filename)
        if memory is None:
            continue
        selected.append(RelevantMemory(memory.file_path.resolve(), memory.mtime_ms))
        if len(selected) >= max_results:
            break
    return selected


async def get_relevant_memory_attachments(
    query: str,
    *,
    tool_use_context: ToolUseContext,
    llm_client: Any,
    memory_dirs: Sequence[str | Path] | None = None,
    recent_tools: Sequence[str] | None = None,
    already_surfaced: set[str] | None = None,
    model: str | None = None,
    max_results: int = MEMORY_RECALL_MAX_RESULTS,
) -> list[Attachment]:
    """Return ``relevant_memories`` attachments for a user query."""

    dirs = list(memory_dirs or [get_auto_mem_path(cwd=tool_use_context.cwd)])
    read_paths = set(tool_use_context.read_file_state.keys())
    surfaced = already_surfaced or set()

    all_results: list[RelevantMemory] = []
    for memory_dir in dirs:
        try:
            selected = await find_relevant_memories(
                query,
                memory_dir,
                llm_client=llm_client,
                abort_event=tool_use_context.abort_event,
                recent_tools=recent_tools,
                already_surfaced=surfaced,
                model=model,
                max_results=max_results,
            )
            all_results.extend(selected)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "Relevant memory selection failed for %s",
                memory_dir,
                exc_info=True,
            )

    selected_fresh: list[RelevantMemory] = []
    seen: set[str] = set()
    for memory in all_results:
        path = str(memory.path)
        if path in seen or path in read_paths or path in surfaced:
            continue
        seen.add(path)
        selected_fresh.append(memory)
        if len(selected_fresh) >= max_results:
            break

    surfaced_memories = read_memories_for_surfacing(selected_fresh)
    if not surfaced_memories:
        return []
    return [
        {
            "type": "relevant_memories",
            "memories": [memory.to_attachment_payload() for memory in surfaced_memories],
        }
    ]


def start_relevant_memory_prefetch(
    messages: Sequence[Mapping[str, Any]],
    tool_use_context: ToolUseContext,
    *,
    llm_client: Any | None = None,
    enabled: bool | None = None,
    model: str | None = None,
) -> MemoryPrefetch | None:
    """Start one non-blocking relevant-memory prefetch for the current turn."""

    if not _is_memory_recall_enabled(enabled):
        return None
    if not is_auto_memory_enabled():
        return None

    last_user_message = _find_last_real_user_message(messages)
    if last_user_message is None:
        return None

    query = get_user_message_text(last_user_message) or ""
    if not query or not re.search(r"\s", query.strip()):
        return None

    surfaced = collect_surfaced_memories(messages)
    if surfaced["total_bytes"] >= MAX_SESSION_BYTES:
        return None

    client = llm_client or tool_use_context.llm_client
    if client is None or not hasattr(client, "call_model"):
        return None

    recall_model = model or os.environ.get(MEMORY_RECALL_MODEL_ENV)
    fired_at = time.time()

    async def _run() -> list[Attachment]:
        try:
            return await get_relevant_memory_attachments(
                query,
                tool_use_context=tool_use_context,
                llm_client=client,
                recent_tools=collect_recent_successful_tools(messages, last_user_message),
                already_surfaced=surfaced["paths"],
                model=recall_model,
            )
        except asyncio.CancelledError:
            return []
        except Exception:
            logger.debug("Relevant memory prefetch failed", exc_info=True)
            return []

    task = asyncio.create_task(_run())
    prefetch = MemoryPrefetch(task=task, fired_at=fired_at)

    def _mark_settled(_task: asyncio.Task[list[Attachment]]) -> None:
        prefetch.settled_at = time.time()

    task.add_done_callback(_mark_settled)
    return prefetch


async def consume_relevant_memory_prefetch(
    prefetch: MemoryPrefetch | None,
    tool_use_context: ToolUseContext,
    *,
    iteration: int,
) -> list[dict[str, Any]]:
    """Consume a settled prefetch without blocking the agent loop."""

    if prefetch is None:
        return []
    if prefetch.consumed_on_iteration != -1:
        return []
    if not prefetch.is_settled():
        return []

    try:
        attachments = await prefetch.task
    except asyncio.CancelledError:
        return []
    except Exception:
        logger.debug("Relevant memory prefetch consume failed", exc_info=True)
        return []

    filtered = filter_duplicate_memory_attachments(
        attachments,
        tool_use_context.read_file_state,
    )
    messages = [create_attachment_message(attachment) for attachment in filtered]
    prefetch.consumed_on_iteration = iteration
    if messages:
        await tool_use_context.emit_event(
            "memory_prefetch_consumed",
            {
                "iteration": iteration,
                "attachment_count": len(messages),
                "memory_count": sum(
                    len((message.get("_meta", {}).get("attachment") or {}).get("memories") or [])
                    for message in messages
                ),
                "latency_ms": int(((prefetch.settled_at or time.time()) - prefetch.fired_at) * 1000),
            },
        )
    return messages


def filter_duplicate_memory_attachments(
    attachments: Sequence[Attachment],
    read_file_state: dict[str, ReadFileEntry],
) -> list[Attachment]:
    """Drop memories already present in read-file state, then mark survivors."""

    filtered_attachments: list[Attachment] = []
    for attachment in attachments:
        if attachment.get("type") != "relevant_memories":
            filtered_attachments.append(dict(attachment))
            continue

        filtered_memories: list[dict[str, Any]] = []
        for raw_memory in attachment.get("memories") or []:
            if not isinstance(raw_memory, Mapping):
                continue
            path = str(raw_memory.get("path") or "")
            if not path or path in read_file_state:
                continue
            filtered_memories.append(dict(raw_memory))

        for memory in filtered_memories:
            path = str(memory["path"])
            read_file_state[path] = ReadFileEntry(
                content=str(memory.get("content") or ""),
                timestamp=float(memory.get("mtimeMs") or 0),
                offset=None,
                limit=_coerce_optional_int(memory.get("limit")),
                is_partial_view=memory.get("limit") is not None,
            )

        if filtered_memories:
            filtered_attachments.append(
                {**dict(attachment), "memories": filtered_memories}
            )
    return filtered_attachments


def create_attachment_message(attachment: Mapping[str, Any]) -> dict[str, Any]:
    """Create a model message using the unified attachment formatter."""

    from openspace.services.conversation.attachments import create_attachment_message as create

    return create(attachment)


def collect_surfaced_memories(
    messages: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Collect paths and bytes from prior ``relevant_memories`` attachments."""

    paths: set[str] = set()
    total_bytes = 0
    for message in messages:
        meta = message.get("_meta")
        if not isinstance(meta, Mapping):
            continue
        attachment = meta.get("attachment")
        if not isinstance(attachment, Mapping):
            continue
        if attachment.get("type") != "relevant_memories":
            continue
        for memory in attachment.get("memories") or []:
            if not isinstance(memory, Mapping):
                continue
            path = memory.get("path")
            if path:
                paths.add(str(path))
            total_bytes += len(str(memory.get("content") or ""))
    return {"paths": paths, "total_bytes": total_bytes}


def read_memories_for_surfacing(
    selected: Sequence[RelevantMemory],
) -> list[SurfacedMemory]:
    """Read selected files with OpenSpace's 200-line / 4096-byte caps."""

    results: list[SurfacedMemory] = []
    for memory in selected:
        try:
            content, line_count, total_lines, truncated_by_bytes = _read_file_limited(
                memory.path,
                max_lines=MAX_MEMORY_LINES,
                max_bytes=MAX_MEMORY_BYTES,
            )
        except (OSError, UnicodeDecodeError):
            continue

        truncated_by_lines = total_lines > MAX_MEMORY_LINES
        truncated = truncated_by_lines or truncated_by_bytes
        final_content = content
        if truncated:
            reason = (
                f"{MAX_MEMORY_BYTES} byte limit"
                if truncated_by_bytes
                else f"first {MAX_MEMORY_LINES} lines"
            )
            final_content += (
                f"\n\n> This memory file was truncated ({reason}). "
                f"Use the read tool to view the complete file at: {memory.path}"
            )

        results.append(
            SurfacedMemory(
                path=memory.path,
                content=final_content,
                mtime_ms=memory.mtime_ms,
                header=memory_header(memory.path, memory.mtime_ms),
                limit=line_count if truncated else None,
            )
        )
    return results


def memory_header(path: str | Path, mtime_ms: float) -> str:
    """Stable per-memory header used inside system-reminder attachments."""

    freshness = memory_freshness_text(mtime_ms)
    if freshness:
        return f"{freshness}\n\nMemory: {path}:"
    return f"Memory (saved {memory_age(mtime_ms)}): {path}:"


def memory_age_days(mtime_ms: float) -> int:
    return max(0, int((time.time() * 1000 - float(mtime_ms)) // 86_400_000))


def memory_age(mtime_ms: float) -> str:
    days = memory_age_days(mtime_ms)
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def memory_freshness_text(mtime_ms: float) -> str:
    days = memory_age_days(mtime_ms)
    if days <= 1:
        return ""
    return (
        f"This memory is {days} days old. "
        "Memories are point-in-time observations, not live state - "
        "claims about code behavior or file:line citations may be outdated. "
        "Verify against current code before asserting as fact."
    )


def collect_recent_successful_tools(
    messages: Sequence[Mapping[str, Any]],
    last_user_message: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return tools that succeeded since the previous real user boundary."""

    use_id_to_name: dict[str, str] = {}
    result_by_use_id: dict[str, bool] = {}

    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if _is_human_turn(message) and message is not last_user_message:
            break
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, Mapping):
                    continue
                tool_id = str(call.get("id") or "")
                function = call.get("function")
                name = (
                    function.get("name")
                    if isinstance(function, Mapping)
                    else call.get("name")
                )
                if tool_id and name:
                    use_id_to_name[tool_id] = str(name)
        elif message.get("role") == "tool":
            tool_id = str(message.get("tool_call_id") or "")
            if not tool_id:
                continue
            meta = message.get("_meta")
            errored = False
            if isinstance(meta, Mapping):
                errored = meta.get("status") in {"error", "cancelled", "denied"}
            else:
                content = message.get("content")
                errored = isinstance(content, str) and content.startswith("Error:")
            result_by_use_id[tool_id] = errored

    failed: set[str] = set()
    succeeded: set[str] = set()
    for tool_id, name in use_id_to_name.items():
        errored = result_by_use_id.get(tool_id)
        if errored is None:
            continue
        if errored:
            failed.add(name)
        else:
            succeeded.add(name)
    return tuple(name for name in succeeded if name not in failed)


async def _select_relevant_memory_filenames(
    *,
    query: str,
    memories: Sequence[MemoryHeader],
    llm_client: Any,
    abort_event: asyncio.Event | None,
    recent_tools: Sequence[str],
    model: str | None,
    max_tokens: int,
    use_json_schema: bool,
) -> list[str]:
    valid_filenames = {memory.filename for memory in memories}
    manifest = format_memory_manifest(list(memories))
    tools_section = (
        f"\n\nRecently used tools: {', '.join(recent_tools)}"
        if recent_tools
        else ""
    )
    selector_prompt = f"Query: {query}\n\nAvailable memories:\n{manifest}{tools_section}"
    response_format = _memory_selection_response_format() if use_json_schema else None

    try:
        response = await run_side_query(
            selector_prompt,
            tools=None,
            model=model,
            llm_client=llm_client,
            system=SELECT_MEMORIES_SYSTEM_PROMPT,
            max_tokens=_resolve_max_tokens(max_tokens),
            temperature=0,
            response_format=response_format,
            abort_event=abort_event,
            parent_abort_event=None,
            query_source="memdir_relevance",
            fork_label="memdir_relevance",
        )
    except asyncio.CancelledError:
        return []
    except Exception as exc:
        if abort_event is not None and abort_event.is_set():
            return []
        if use_json_schema:
            try:
                response = await run_side_query(
                    selector_prompt,
                    tools=None,
                    model=model,
                    llm_client=llm_client,
                    system=SELECT_MEMORIES_SYSTEM_PROMPT,
                    max_tokens=_resolve_max_tokens(max_tokens),
                    temperature=0,
                    abort_event=abort_event,
                    parent_abort_event=None,
                    query_source="memdir_relevance",
                    fork_label="memdir_relevance",
                )
            except Exception:
                logger.debug(
                    "[memdir] selectRelevantMemories failed: %s",
                    exc,
                    exc_info=True,
                )
                return []
        else:
            logger.debug(
                "[memdir] selectRelevantMemories failed: %s",
                exc,
                exc_info=True,
            )
            return []

    text = response.text
    if not text:
        return []

    parsed = _parse_selected_memories(text)
    return [filename for filename in parsed if filename in valid_filenames]


def _memory_selection_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "selected_memories",
            "schema": {
                "type": "object",
                "properties": {
                    "selected_memories": {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                },
                "required": ["selected_memories"],
                "additionalProperties": False,
            },
        },
    }


def _parse_selected_memories(text: str) -> list[str]:
    candidate = _strip_json_code_fence(text.strip())
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", candidate)
        if match is None:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []

    if isinstance(parsed, Mapping):
        selected = parsed.get("selected_memories")
    elif isinstance(parsed, list):
        selected = parsed
    else:
        selected = None
    if not isinstance(selected, list):
        return []
    return [str(item) for item in selected if isinstance(item, str)]


def _strip_json_code_fence(text: str) -> str:
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_response_text(response: Any) -> str:
    assistant_message = getattr(response, "assistant_message", None)
    if isinstance(assistant_message, Mapping):
        return _content_to_text(assistant_message.get("content"))
    if isinstance(response, Mapping):
        return _content_to_text(response.get("content"))
    content = getattr(response, "content", None)
    return _content_to_text(content)


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif "content" in block:
                    parts.append(str(block.get("content") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(part for part in parts if part)
    return str(content)


def _format_attachment_for_model(attachment: Mapping[str, Any]) -> str:
    if attachment.get("type") == "nested_memory":
        content = attachment.get("content")
        if isinstance(content, Mapping):
            path = str(content.get("path") or attachment.get("path") or "")
            body = str(content.get("content") or "")
        else:
            path = str(attachment.get("path") or "")
            body = str(content or "")
        return (
            "<system-reminder>\n"
            f"Contents of {path}:\n\n{body}"
            "\n</system-reminder>"
        )

    if attachment.get("type") != "relevant_memories":
        return f"<system-reminder>{json.dumps(dict(attachment), ensure_ascii=False, default=str)}</system-reminder>"

    blocks: list[str] = []
    for memory in attachment.get("memories") or []:
        if not isinstance(memory, Mapping):
            continue
        header = str(
            memory.get("header")
            or memory_header(str(memory.get("path") or ""), float(memory.get("mtimeMs") or 0))
        )
        content = str(memory.get("content") or "")
        blocks.append(f"{header}\n\n{content}")
    if not blocks:
        return "<system-reminder></system-reminder>"
    return "<system-reminder>\n" + "\n\n".join(blocks) + "\n</system-reminder>"


def _json_safe_attachment(attachment: Mapping[str, Any]) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, Mapping):
            return {str(k): convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v) for v in value]
        return value

    return convert(dict(attachment))


def _read_file_limited(
    path: Path,
    *,
    max_lines: int,
    max_bytes: int,
) -> tuple[str, int, int, bool]:
    data = path.read_bytes()
    total_lines = data.count(b"\n") + (1 if data else 0)
    selected_lines = data.splitlines(keepends=True)[:max_lines]
    limited = b"".join(selected_lines)
    truncated_by_bytes = len(limited) > max_bytes
    if truncated_by_bytes:
        limited = limited[:max_bytes]
    content = limited.decode("utf-8", errors="replace")
    line_count = limited.count(b"\n") + (1 if limited else 0)
    return content.rstrip("\n"), line_count, total_lines, truncated_by_bytes


def _find_last_real_user_message(
    messages: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        meta = message.get("_meta")
        if isinstance(meta, Mapping) and (
            meta.get("is_meta") is True
            or meta.get("type") in {"attachment", "agent_injection", "compact_summary"}
        ):
            continue
        return message
    return None


def _is_human_turn(message: Mapping[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    meta = message.get("_meta")
    if not isinstance(meta, Mapping):
        return True
    return not bool(
        meta.get("is_meta") is True
        or meta.get("type") in {"attachment", "agent_injection", "compact_summary"}
    )


def _is_memory_recall_enabled(enabled: bool | None) -> bool:
    if enabled is not None:
        return bool(enabled)
    env_value = os.environ.get(MEMORY_RECALL_ENABLED_ENV)
    if env_value is None:
        return True
    return env_value.lower() not in {"0", "false", "no", "off"}


def _resolve_max_tokens(default_value: int) -> int:
    raw = os.environ.get(MEMORY_RECALL_MAX_TOKENS_ENV)
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return default_value


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "MAX_MEMORY_BYTES",
    "MAX_MEMORY_LINES",
    "MAX_SESSION_BYTES",
    "MEMORY_RECALL_MAX_RESULTS",
    "MEMORY_RECALL_MAX_TOKENS",
    "MemoryPrefetch",
    "RelevantMemory",
    "SurfacedMemory",
    "collect_recent_successful_tools",
    "collect_surfaced_memories",
    "consume_relevant_memory_prefetch",
    "create_attachment_message",
    "filter_duplicate_memory_attachments",
    "find_relevant_memories",
    "get_relevant_memory_attachments",
    "memory_age",
    "memory_age_days",
    "memory_freshness_text",
    "memory_header",
    "read_memories_for_surfacing",
    "start_relevant_memory_prefetch",
]
