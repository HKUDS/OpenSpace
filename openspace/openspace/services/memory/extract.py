"""Background auto-memory extraction.

OpenSpace runs a small isolated tool loop through
``services.side_query.run_side_query`` to extract durable memory in the
background.

Implemented behavior:
- cursor based incremental extraction
- skip when the main agent already wrote auto memory
- per-N-turn throttling
- coalesced trailing run while extraction is in progress
- restricted tool gate for memory-related reads, searches, and writes
- best-effort fire-and-forget execution with drain support
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from openspace.grounding.core.tool.base import BaseTool
from openspace.services.memory.memdir import (
    ENTRYPOINT_NAME,
    ensure_memory_dir_exists,
    get_auto_mem_path,
    is_auto_mem_path,
    is_auto_memory_enabled,
    is_extract_mode_active,
)
from openspace.services.memory.daily_log import (
    MEMORY_LOG_TOOL_NAME,
    MemoryLogTool,
    build_extract_daily_log_prompt,
    extract_logged_entries,
    get_memory_mode,
)
from openspace.services.memory.memory_scan import format_memory_manifest, scan_memory_files
from openspace.services.memory.memory_types import (
    MEMORY_FRONTMATTER_EXAMPLE,
    TYPES_SECTION_COMBINED,
    TYPES_SECTION_INDIVIDUAL,
    WHAT_NOT_TO_SAVE_SECTION,
)
from openspace.services.memory.task_scope import maybe_memory_task_scope_key
from openspace.services.conversation.side_query import run_side_query
from openspace.services.tooling.context import ToolUseContext

logger = logging.getLogger(__name__)


FILE_READ_TOOL_NAME = "read"
GREP_TOOL_NAME = "grep"
GLOB_TOOL_NAME = "glob"
BASH_TOOL_NAME = "bash"
FILE_EDIT_TOOL_NAME = "edit"
FILE_WRITE_TOOL_NAME = "write"
MEMORY_READ_TOOL_NAME = "memory_read"
MEMORY_WRITE_TOOL_NAME = "memory_write"

OPENSPACE_EXTRACT_MEMORIES_ENABLED_ENV = "OPENSPACE_EXTRACT_MEMORIES_ENABLED"
OPENSPACE_DISABLE_EXTRACT_MEMORIES_ENV = "OPENSPACE_DISABLE_EXTRACT_MEMORIES"
OPENSPACE_EXTRACT_MEMORIES_EVERY_N_TURNS_ENV = "OPENSPACE_EXTRACT_MEMORIES_EVERY_N_TURNS"
OPENSPACE_EXTRACT_MEMORIES_SKIP_INDEX_ENV = "OPENSPACE_EXTRACT_MEMORIES_SKIP_INDEX"
OPENSPACE_MEMORY_EXTRACT_MODEL_ENV = "OPENSPACE_MEMORY_EXTRACT_MODEL"
OPENSPACE_EXTRACT_MEMORIES_ALLOW_NON_INTERACTIVE_ENV = (
    "OPENSPACE_EXTRACT_MEMORIES_ALLOW_NON_INTERACTIVE"
)

DEFAULT_MAX_EXTRACT_TURNS = 5

AppendSystemMessageFn = Callable[[dict[str, Any]], Awaitable[None] | None]
AutoMemCanUseToolFn = Callable[
    [BaseTool | None, Mapping[str, Any]],
    Awaitable[dict[str, Any]],
]


@dataclass(frozen=True)
class MemoryCursor:
    """Cursor for the last parent message processed by extraction.

    OpenSpace stores a UUID.  OpenSpace messages do not always carry UUIDs, so the
    cursor stores a stable fingerprint plus its occurrence number.  If the
    fingerprint cannot be found later, we fall back to "all messages", matching
    OpenSpace's compaction fallback.
    """

    fingerprint: str
    occurrence: int


@dataclass(slots=True)
class MemoryExtractionResult:
    """Result object for tests and observability.

    OpenSpace's public API resolves ``void``.  Returning this value in OS does not
    affect the fire-and-forget call path, but makes the behavior auditable.
    """

    ran: bool = False
    skipped_reason: str | None = None
    new_message_count: int = 0
    written_paths: list[str] = field(default_factory=list)
    memory_paths: list[str] = field(default_factory=list)
    turn_count: int = 0
    error: str | None = None
    duration_ms: float = 0.0


@dataclass(slots=True)
class MemoryExtractorRuntimeState:
    last_cursor: MemoryCursor | None = None
    in_progress: bool = False
    turns_since_last_extraction: int = 0
    pending_context: tuple[ToolUseContext, AppendSystemMessageFn | None] | None = None


def _env_truthy(value: str | None) -> bool:
    return value is not None and value.lower() in {"1", "true", "yes", "on"}


def _env_defined_falsy(value: str | None) -> bool:
    return value is not None and value.lower() in {"0", "false", "no", "off", ""}


def _extract_feature_enabled() -> bool:
    if _env_truthy(os.environ.get(OPENSPACE_DISABLE_EXTRACT_MEMORIES_ENV)):
        return False
    enabled = os.environ.get(OPENSPACE_EXTRACT_MEMORIES_ENABLED_ENV)
    # OpenSpace gates this behind GrowthBook with default ``false``.  OpenSpace has
    # no GrowthBook layer, so an explicit env/settings opt-in is the equivalent
    # feature decision.
    return _env_truthy(enabled)


def _extract_every_n_turns(default: int = 1) -> int:
    raw = os.environ.get(OPENSPACE_EXTRACT_MEMORIES_EVERY_N_TURNS_ENV)
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return max(1, parsed)


def _extract_skip_index() -> bool:
    return _env_truthy(os.environ.get(OPENSPACE_EXTRACT_MEMORIES_SKIP_INDEX_ENV))


def _extract_allow_non_interactive() -> bool:
    return _env_truthy(
        os.environ.get(OPENSPACE_EXTRACT_MEMORIES_ALLOW_NON_INTERACTIVE_ENV)
    )


def _opener(new_message_count: int, existing_memories: str) -> str:
    manifest = (
        "\n\n## Existing memory files\n\n"
        + existing_memories
        + "\n\nCheck this list before writing - update an existing file rather than creating a duplicate."
        if existing_memories
        else ""
    )
    return "\n".join(
        [
            (
                "You are now acting as the memory extraction subagent. "
                f"Analyze the most recent ~{new_message_count} messages above and use them to update your persistent memory systems."
            ),
            "",
            (
                f"Available tools: {FILE_READ_TOOL_NAME}, {GREP_TOOL_NAME}, {GLOB_TOOL_NAME}, "
                f"read-only {BASH_TOOL_NAME} (ls/find/cat/stat/wc/head/tail and similar), "
                f"and {FILE_EDIT_TOOL_NAME}/{FILE_WRITE_TOOL_NAME} for paths inside the memory directory only. "
                f"{MEMORY_READ_TOOL_NAME}/{MEMORY_WRITE_TOOL_NAME} are also available in OpenSpace and are already scoped to the memory directory. "
                f"{BASH_TOOL_NAME} rm is not permitted. All other tools - MCP, Agent, write-capable {BASH_TOOL_NAME}, etc - will be denied."
            ),
            "",
            (
                f"You have a limited turn budget. {FILE_EDIT_TOOL_NAME} requires a prior "
                f"{FILE_READ_TOOL_NAME} of the same file, so the efficient strategy is: "
                f"turn 1 - issue all {FILE_READ_TOOL_NAME} calls in parallel for every file you might update; "
                f"turn 2 - issue all {FILE_WRITE_TOOL_NAME}/{FILE_EDIT_TOOL_NAME} or {MEMORY_WRITE_TOOL_NAME} calls in parallel. "
                "Do not interleave reads and writes across multiple turns."
            ),
            "",
            (
                f"You MUST only use content from the last ~{new_message_count} messages to update your persistent memories. "
                "Do not waste any turns attempting to investigate or verify that content further - no grepping source files, "
                "no reading code to confirm a pattern exists, no git commands."
                + manifest
            ),
        ]
    )


def build_extract_auto_only_prompt(
    new_message_count: int,
    existing_memories: str,
    skip_index: bool = False,
) -> str:
    """Build OpenSpace's auto-only extraction prompt, adapted to OS tool names."""

    if skip_index:
        how_to_save = [
            "## How to save memories",
            "",
            "Write each memory to its own file (e.g., `user_role.md`, `feedback_testing.md`) using this frontmatter format:",
            "",
            *MEMORY_FRONTMATTER_EXAMPLE,
            "",
            "- Organize memory semantically by topic, not chronologically",
            "- Update or remove memories that turn out to be wrong or outdated",
            "- Do not write duplicate memories. First check if there is an existing memory you can update before writing a new one.",
        ]
    else:
        how_to_save = [
            "## How to save memories",
            "",
            "Saving a memory is a two-step process:",
            "",
            "**Step 1** - write the memory to its own file (e.g., `user_role.md`, `feedback_testing.md`) using this frontmatter format:",
            "",
            *MEMORY_FRONTMATTER_EXAMPLE,
            "",
            (
                f"**Step 2** - add a pointer to that file in `{ENTRYPOINT_NAME}`. "
                f"`{ENTRYPOINT_NAME}` is an index, not a memory - each entry should be one line, under ~150 characters: "
                "`- [Title](file.md) - one-line hook`. It has no frontmatter. Never write memory content directly into "
                f"`{ENTRYPOINT_NAME}`."
            ),
            "",
            f"- `{ENTRYPOINT_NAME}` is always loaded into your system prompt - lines after 200 will be truncated, so keep the index concise",
            "- Organize memory semantically by topic, not chronologically",
            "- Update or remove memories that turn out to be wrong or outdated",
            "- Do not write duplicate memories. First check if there is an existing memory you can update before writing a new one.",
        ]

    return "\n".join(
        [
            _opener(new_message_count, existing_memories),
            "",
            "If the user explicitly asks you to remember something, save it immediately as whichever type fits best. If they ask you to forget something, find and remove the relevant entry.",
            "",
            *TYPES_SECTION_INDIVIDUAL,
            *WHAT_NOT_TO_SAVE_SECTION,
            "",
            *how_to_save,
        ]
    )


def build_extract_combined_prompt(
    new_message_count: int,
    existing_memories: str,
    skip_index: bool = False,
    *,
    team_memory_enabled: bool = False,
) -> str:
    """Build OpenSpace's combined auto+team prompt.

    Team memory is intentionally disabled in OpenSpace because it needs a
    backend/API surface (DEC-025).  The function keeps the OpenSpace return structure
    so later TeamMem work has a single place to connect.
    """

    if not team_memory_enabled:
        return build_extract_auto_only_prompt(
            new_message_count,
            existing_memories,
            skip_index,
        )

    if skip_index:
        how_to_save = [
            "## How to save memories",
            "",
            "Write each memory to its own file in the chosen directory (private or team, per the type's scope guidance) using this frontmatter format:",
            "",
            *MEMORY_FRONTMATTER_EXAMPLE,
            "",
            "- Organize memory semantically by topic, not chronologically",
            "- Update or remove memories that turn out to be wrong or outdated",
            "- Do not write duplicate memories. First check if there is an existing memory you can update before writing a new one.",
        ]
    else:
        how_to_save = [
            "## How to save memories",
            "",
            "Saving a memory is a two-step process:",
            "",
            "**Step 1** - write the memory to its own file in the chosen directory (private or team, per the type's scope guidance) using this frontmatter format:",
            "",
            *MEMORY_FRONTMATTER_EXAMPLE,
            "",
            "**Step 2** - add a pointer to that file in the same directory's `MEMORY.md`. Each directory (private and team) has its own `MEMORY.md` index - each entry should be one line, under ~150 characters: `- [Title](file.md) - one-line hook`. They have no frontmatter. Never write memory content directly into a `MEMORY.md`.",
            "",
            "- Both `MEMORY.md` indexes are loaded into your system prompt - lines after 200 will be truncated, so keep them concise",
            "- Organize memory semantically by topic, not chronologically",
            "- Update or remove memories that turn out to be wrong or outdated",
            "- Do not write duplicate memories. First check if there is an existing memory you can update before writing a new one.",
        ]

    return "\n".join(
        [
            _opener(new_message_count, existing_memories),
            "",
            "If the user explicitly asks you to remember something, save it immediately as whichever type fits best. If they ask you to forget something, find and remove the relevant entry.",
            "",
            *TYPES_SECTION_COMBINED,
            *WHAT_NOT_TO_SAVE_SECTION,
            "- You MUST avoid saving sensitive data within shared team memories. For example, never save API keys or user credentials.",
            "",
            *how_to_save,
        ]
    )


def create_auto_mem_can_use_tool(memory_dir: str | Path) -> AutoMemCanUseToolFn:
    """Return the tool gate for auto-memory background agents."""

    root = Path(memory_dir).expanduser().resolve()

    async def can_use_tool(
        tool: BaseTool | None,
        input: Mapping[str, Any],
    ) -> dict[str, Any]:
        if tool is None:
            return _deny_auto_mem_tool(
                "unknown",
                "Tool is not available to the auto-memory extraction agent.",
            )

        name = tool.name
        data = dict(input)

        if name in {MEMORY_READ_TOOL_NAME, MEMORY_WRITE_TOOL_NAME}:
            return {"behavior": "allow", "updated_input": data}

        if name == MEMORY_LOG_TOOL_NAME:
            return {"behavior": "allow", "updated_input": data}

        if name in {FILE_READ_TOOL_NAME, GREP_TOOL_NAME, GLOB_TOOL_NAME}:
            return {"behavior": "allow", "updated_input": data}

        if name == BASH_TOOL_NAME:
            try:
                if tool.is_read_only(data):
                    return {"behavior": "allow", "updated_input": data}
            except Exception:
                pass
            return _deny_auto_mem_tool(
                name,
                (
                    "Only read-only shell commands are permitted in this context "
                    "(ls, find, grep, cat, stat, wc, head, tail, and similar)."
                ),
            )

        if name in {FILE_EDIT_TOOL_NAME, FILE_WRITE_TOOL_NAME}:
            file_path = data.get("file_path")
            if isinstance(file_path, str) and _path_is_inside(file_path, root):
                return {"behavior": "allow", "updated_input": data}
            return _deny_auto_mem_tool(
                name,
                f"{name} is only permitted for paths inside {root}.",
            )

        return _deny_auto_mem_tool(
            name,
            (
                f"Only {FILE_READ_TOOL_NAME}, {GREP_TOOL_NAME}, {GLOB_TOOL_NAME}, "
                f"read-only {BASH_TOOL_NAME}, {FILE_EDIT_TOOL_NAME}/{FILE_WRITE_TOOL_NAME} within {root}, "
                f"{MEMORY_READ_TOOL_NAME}/{MEMORY_WRITE_TOOL_NAME}, and {MEMORY_LOG_TOOL_NAME} are allowed."
            ),
        )

    return can_use_tool


def _deny_auto_mem_tool(tool_name: str, reason: str) -> dict[str, Any]:
    logger.debug("[autoMem] denied %s: %s", tool_name, reason)
    return {
        "behavior": "deny",
        "message": reason,
        "decision_reason": {"type": "other", "reason": reason},
    }


def _path_is_inside(file_path: str, root: Path) -> bool:
    try:
        candidate = Path(file_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate.resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


class MemoryExtractor:
    """Stateful background memory extractor.

    A single extractor instance mirrors OpenSpace's closure-scoped state created by
    ``initExtractMemories``.
    """

    def __init__(
        self,
        *,
        max_turns: int = DEFAULT_MAX_EXTRACT_TURNS,
        throttle_turns: int | None = None,
    ) -> None:
        self.max_turns = max(1, int(max_turns))
        self.throttle_turns = throttle_turns
        self._states: dict[str, MemoryExtractorRuntimeState] = {}
        self._default_state = MemoryExtractorRuntimeState()
        self._in_flight: set[asyncio.Task[MemoryExtractionResult]] = set()
        self._task_scope_keys: dict[asyncio.Task[MemoryExtractionResult], str | None] = {}

    @property
    def last_cursor(self) -> MemoryCursor | None:
        return self._default_state.last_cursor

    def _runtime_state(self, context: ToolUseContext) -> MemoryExtractorRuntimeState:
        scope_key = maybe_memory_task_scope_key(context)
        if scope_key is None:
            return self._default_state
        state = self._states.get(scope_key)
        if state is None:
            state = MemoryExtractorRuntimeState()
            self._states[scope_key] = state
        return state

    async def execute(
        self,
        context: ToolUseContext,
        append_system_message: AppendSystemMessageFn | None = None,
    ) -> MemoryExtractionResult:
        """Run extraction once, including trailing-run coalescing."""

        task = asyncio.current_task()
        if task is not None:
            self._in_flight.add(task)  # type: ignore[arg-type]
            self._task_scope_keys[task] = maybe_memory_task_scope_key(context)  # type: ignore[index]
        try:
            return await self._execute_impl(context, append_system_message)
        finally:
            if task is not None:
                self._in_flight.discard(task)  # type: ignore[arg-type]
                self._task_scope_keys.pop(task, None)

    def submit(
        self,
        context: ToolUseContext,
        append_system_message: AppendSystemMessageFn | None = None,
    ) -> asyncio.Task[MemoryExtractionResult]:
        """Schedule fire-and-forget extraction and track it for draining."""

        task = asyncio.create_task(self.execute(context, append_system_message))
        self._in_flight.add(task)
        self._task_scope_keys[task] = maybe_memory_task_scope_key(context)

        def _done(done: asyncio.Task[MemoryExtractionResult]) -> None:
            self._in_flight.discard(done)
            self._task_scope_keys.pop(done, None)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("Background memory extraction task failed", exc_info=True)

        task.add_done_callback(_done)
        return task

    async def drain(
        self,
        timeout_s: float = 60.0,
        *,
        context: Any | None = None,
        scope_key: str | None = None,
    ) -> int:
        """Wait for in-flight extractions with a soft timeout."""

        scope_key = scope_key or maybe_memory_task_scope_key(context)
        tasks = [
            task
            for task in self._in_flight
            if scope_key is None or self._task_scope_keys.get(task) == scope_key
        ]
        if not tasks:
            return 0
        done, _pending = await asyncio.wait(
            tasks,
            timeout=max(0.0, timeout_s),
        )
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("Pending memory extraction failed during drain", exc_info=True)
        return len(_pending)

    async def _execute_impl(
        self,
        context: ToolUseContext,
        append_system_message: AppendSystemMessageFn | None,
    ) -> MemoryExtractionResult:
        if _should_skip_context(context):
            await _emit_memory_extraction_skipped(context, "subagent")
            return MemoryExtractionResult(skipped_reason="subagent")

        feature_enabled = _extract_feature_enabled()
        non_interactive = not bool(getattr(context, "tui_available", False))
        if not is_extract_mode_active(
            feature_enabled=feature_enabled,
            non_interactive=non_interactive,
            allow_non_interactive=_extract_allow_non_interactive(),
        ):
            await _emit_memory_extraction_skipped(context, "disabled")
            return MemoryExtractionResult(skipped_reason="disabled")

        if not is_auto_memory_enabled():
            await _emit_memory_extraction_skipped(context, "auto_memory_disabled")
            return MemoryExtractionResult(skipped_reason="auto_memory_disabled")

        state = self._runtime_state(context)
        if state.in_progress:
            state.pending_context = (context, append_system_message)
            await context.emit_event("memory_extraction_coalesced", {})
            await _emit_memory_extraction_skipped(context, "coalesced")
            return MemoryExtractionResult(skipped_reason="coalesced")

        return await self._run_extraction_chain(context, append_system_message, state)

    async def _run_extraction_chain(
        self,
        context: ToolUseContext,
        append_system_message: AppendSystemMessageFn | None,
        state: MemoryExtractorRuntimeState,
        *,
        is_trailing_run: bool = False,
    ) -> MemoryExtractionResult:
        state.in_progress = True
        try:
            result = await self._run_extraction(
                context,
                append_system_message,
                state,
                is_trailing_run=is_trailing_run,
            )
        finally:
            state.in_progress = False

        trailing = state.pending_context
        state.pending_context = None
        if trailing is not None:
            await context.emit_event("memory_extraction_trailing_start", {})
            await self._run_extraction_chain(
                trailing[0],
                trailing[1],
                state,
                is_trailing_run=True,
            )
        return result

    async def _run_extraction(
        self,
        context: ToolUseContext,
        append_system_message: AppendSystemMessageFn | None,
        state: MemoryExtractorRuntimeState,
        *,
        is_trailing_run: bool,
    ) -> MemoryExtractionResult:
        start_time = time.time()
        messages = list(context.messages or [])
        memory_dir = get_auto_mem_path(cwd=context.cwd)
        new_message_count = count_model_visible_messages_since(
            messages,
            state.last_cursor,
        )

        if new_message_count <= 0:
            await _emit_memory_extraction_skipped(
                context,
                "no_new_messages",
                memory_dir=str(memory_dir),
                message_count=new_message_count,
            )
            return MemoryExtractionResult(
                skipped_reason="no_new_messages",
                new_message_count=0,
            )

        if has_memory_writes_since(messages, state.last_cursor, memory_dir=memory_dir):
            self._advance_cursor(messages, state)
            await context.emit_event(
                "memory_extraction_skipped",
                {
                    "task_id": _background_task_id(context, "extract_memories"),
                    "reason": "main_agent_memory_write",
                    "message_count": new_message_count,
                },
            )
            return MemoryExtractionResult(
                skipped_reason="main_agent_memory_write",
                new_message_count=new_message_count,
            )

        if not is_trailing_run:
            state.turns_since_last_extraction += 1
            required_turns = self.throttle_turns or _extract_every_n_turns()
            if state.turns_since_last_extraction < required_turns:
                await _emit_memory_extraction_skipped(
                    context,
                    "throttled",
                    memory_dir=str(memory_dir),
                    message_count=new_message_count,
                    required_turns=required_turns,
                    turns_since_last_extraction=state.turns_since_last_extraction,
                )
                return MemoryExtractionResult(
                    skipped_reason="throttled",
                    new_message_count=new_message_count,
                )
        state.turns_since_last_extraction = 0

        llm_client = context.llm_client
        if llm_client is None or not hasattr(llm_client, "call_model"):
            await _emit_memory_extraction_skipped(
                context,
                "missing_llm_client",
                memory_dir=str(memory_dir),
                message_count=new_message_count,
            )
            return MemoryExtractionResult(
                skipped_reason="missing_llm_client",
                new_message_count=new_message_count,
            )

        ensure_memory_dir_exists(memory_dir)
        existing_memories = format_memory_manifest(scan_memory_files(memory_dir))
        memory_mode = get_memory_mode(getattr(context, "memory_mode", None))
        daily_log_mode = memory_mode == "daily_log"
        if daily_log_mode:
            prompt = build_extract_daily_log_prompt(
                new_message_count,
                existing_memories,
            )
        else:
            prompt = build_extract_auto_only_prompt(
                new_message_count,
                existing_memories,
                skip_index=_extract_skip_index(),
            )
        tools = _select_auto_mem_tools(
            context,
            memory_dir,
            daily_log_mode=daily_log_mode,
        )
        if not tools:
            await _emit_memory_extraction_skipped(
                context,
                "missing_tools",
                memory_dir=str(memory_dir),
                message_count=new_message_count,
                memory_mode=memory_mode,
            )
            return MemoryExtractionResult(
                skipped_reason="missing_tools",
                new_message_count=new_message_count,
            )

        can_use_tool = create_auto_mem_can_use_tool(memory_dir)
        task_id = _background_task_id(context, "extract_memories")
        await context.emit_event(
            "memory_extraction_start",
            {
                "task_id": task_id,
                "message_count": new_message_count,
                "memory_mode": memory_mode,
                "memory_dir": str(memory_dir),
                "is_trailing_run": is_trailing_run,
            },
        )

        try:
            model_override = os.environ.get(OPENSPACE_MEMORY_EXTRACT_MODEL_ENV) or None
            side_result = await run_side_query(
                prompt,
                tools=tools,
                model=model_override,
                parent_context=context,
                llm_client=llm_client,
                messages=messages,
                max_turns=self.max_turns,
                can_use_tool=can_use_tool,
                query_source="extract_memories",
                fork_label="extract_memories",
                agent_type="extract_memories",
                denied_result_type="auto_mem_tool_denied",
                tui_available=False,
                is_async_agent=True,
            )
            result_messages = side_result.messages
            total_usage = side_result.total_usage
            turn_count = side_result.turn_count

            self._advance_cursor(messages, state)
            written_paths = extract_written_paths(result_messages, memory_dir=memory_dir)
            logged_entry_ids, logged_paths = extract_logged_entries(result_messages)
            if daily_log_mode:
                written_paths = logged_paths
            memory_paths = [
                path for path in written_paths if Path(path).name != ENTRYPOINT_NAME
            ]
            if daily_log_mode:
                memory_paths = []
            duration_ms = (time.time() - start_time) * 1000
            extraction_result = MemoryExtractionResult(
                ran=True,
                new_message_count=new_message_count,
                written_paths=written_paths,
                memory_paths=memory_paths,
                turn_count=turn_count,
                duration_ms=duration_ms,
            )
            await context.emit_event(
                "memory_extraction_complete",
                {
                    "task_id": task_id,
                    "message_count": new_message_count,
                    "turn_count": turn_count,
                    "files_written": len(written_paths),
                    "memories_saved": len(memory_paths),
                    "input_tokens": total_usage.input_tokens,
                    "output_tokens": total_usage.output_tokens,
                    "duration_ms": duration_ms,
                    "memory_mode": memory_mode,
                    "written_paths": written_paths,
                    "memory_paths": memory_paths,
                    "log_paths": logged_paths,
                    "logged_entry_ids": logged_entry_ids,
                    "logs_written": len(logged_paths),
                },
            )
            if memory_paths:
                await _append_memory_saved_message(
                    context,
                    memory_paths,
                    append_system_message,
                )
            return extraction_result
        except Exception as exc:
            duration_ms = (time.time() - start_time) * 1000
            logger.debug("[extractMemories] error: %s", exc, exc_info=True)
            await context.emit_event(
                "memory_extraction_error",
                {
                    "task_id": task_id,
                    "memory_dir": str(memory_dir),
                    "duration_ms": duration_ms,
                    "error": str(exc),
                },
            )
            return MemoryExtractionResult(
                ran=True,
                new_message_count=new_message_count,
                turn_count=turn_count,
                duration_ms=duration_ms,
                error=str(exc),
            )

    def _advance_cursor(
        self,
        messages: Sequence[Mapping[str, Any]],
        state: MemoryExtractorRuntimeState,
    ) -> None:
        if not messages:
            return
        state.last_cursor = _cursor_for_index(messages, len(messages) - 1)


def _should_skip_context(context: ToolUseContext) -> bool:
    if getattr(context, "is_async_agent", False):
        return True
    if getattr(context, "parent_task_id", None):
        return True
    if getattr(context, "agent_type", None) in {"extract_memories", "auto_dream"}:
        return True
    return False


def _background_task_id(context: ToolUseContext, key: str) -> str | None:
    task_ids = getattr(context, "background_task_ids", None)
    if isinstance(task_ids, dict):
        value = task_ids.get(key)
        return str(value) if value else None
    return None


async def _emit_memory_extraction_skipped(
    context: ToolUseContext,
    reason: str,
    **extra: Any,
) -> None:
    await context.emit_event(
        "memory_extraction_skipped",
        {
            "task_id": _background_task_id(context, "extract_memories"),
            "reason": reason,
            **extra,
        },
    )


def should_schedule_extract_memories(context: ToolUseContext) -> bool:
    """Cheap gate used by stop hooks before creating a background task."""

    if _should_skip_context(context):
        return False
    if not is_auto_memory_enabled():
        return False
    feature_enabled = _extract_feature_enabled()
    non_interactive = not bool(getattr(context, "tui_available", False))
    return is_extract_mode_active(
        feature_enabled=feature_enabled,
        non_interactive=non_interactive,
        allow_non_interactive=_extract_allow_non_interactive(),
    )


def _select_auto_mem_tools(
    context: ToolUseContext,
    memory_dir: str | Path,
    *,
    daily_log_mode: bool = False,
) -> list[BaseTool]:
    if daily_log_mode:
        allowed = {
            FILE_READ_TOOL_NAME,
            GREP_TOOL_NAME,
            GLOB_TOOL_NAME,
            BASH_TOOL_NAME,
            MEMORY_READ_TOOL_NAME,
            MEMORY_LOG_TOOL_NAME,
        }
    else:
        allowed = {
            FILE_READ_TOOL_NAME,
            GREP_TOOL_NAME,
            GLOB_TOOL_NAME,
            BASH_TOOL_NAME,
            FILE_EDIT_TOOL_NAME,
            FILE_WRITE_TOOL_NAME,
            MEMORY_READ_TOOL_NAME,
            MEMORY_WRITE_TOOL_NAME,
        }
    selected: list[BaseTool] = []
    seen: set[str] = set()
    for tool in [*(context.all_tools or []), *(context.tools or [])]:
        name = getattr(tool, "name", "")
        if name in allowed and name not in seen:
            selected.append(tool)
            seen.add(name)

    # OpenSpace-specific safe default: if the main tool set omitted the memory
    # helpers, use them as a scoped fallback.  OpenSpace lacks these dedicated tools;
    # OS added them in 15.5 to avoid fragile hand-written frontmatter/index
    # edits while preserving the same memdir storage shape.
    try:
        if daily_log_mode and MEMORY_LOG_TOOL_NAME not in seen:
            selected.append(MemoryLogTool())
            seen.add(MEMORY_LOG_TOOL_NAME)
        if not daily_log_mode and MEMORY_WRITE_TOOL_NAME not in seen:
            from openspace.tools.memory_tools import MemoryWriteTool

            selected.append(MemoryWriteTool())
            seen.add(MEMORY_WRITE_TOOL_NAME)
        if MEMORY_READ_TOOL_NAME not in seen:
            from openspace.tools.memory_tools import MemoryReadTool

            selected.append(MemoryReadTool())
            seen.add(MEMORY_READ_TOOL_NAME)
    except Exception:
        pass

    return selected


def _tool_call_name(tool_call: Mapping[str, Any]) -> str:
    function = tool_call.get("function")
    if isinstance(function, Mapping):
        name = function.get("name")
        if isinstance(name, str):
            return name
    name = tool_call.get("name")
    return name if isinstance(name, str) else ""


def _tool_call_input(tool_call: Mapping[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function")
    raw: Any = None
    if isinstance(function, Mapping):
        raw = function.get("arguments")
    elif "input" in tool_call:
        raw = tool_call.get("input")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _message_fingerprint(message: Mapping[str, Any]) -> str:
    meta = message.get("_meta")
    if isinstance(meta, Mapping):
        for key in ("uuid", "message_uuid", "id"):
            value = meta.get(key)
            if isinstance(value, str) and value:
                return f"meta:{key}:{value}"
    for key in ("uuid", "id"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return f"{key}:{value}"

    payload = {
        "role": message.get("role"),
        "content": message.get("content"),
        "tool_calls": message.get("tool_calls"),
        "tool_call_id": message.get("tool_call_id"),
        "name": message.get("name"),
    }
    raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cursor_for_index(
    messages: Sequence[Mapping[str, Any]],
    index: int,
) -> MemoryCursor:
    fingerprint = _message_fingerprint(messages[index])
    occurrence = 0
    for item in messages[: index + 1]:
        if _message_fingerprint(item) == fingerprint:
            occurrence += 1
    return MemoryCursor(fingerprint=fingerprint, occurrence=occurrence)


def _find_cursor_index(
    messages: Sequence[Mapping[str, Any]],
    cursor: MemoryCursor | None,
) -> int:
    if cursor is None:
        return -1
    seen = 0
    for index, message in enumerate(messages):
        if _message_fingerprint(message) == cursor.fingerprint:
            seen += 1
            if seen == cursor.occurrence:
                return index
    return -1


def _is_model_visible_message(message: Mapping[str, Any]) -> bool:
    return message.get("role") in {"user", "assistant"}


def count_model_visible_messages_since(
    messages: Sequence[Mapping[str, Any]],
    cursor: MemoryCursor | None,
) -> int:
    start = _find_cursor_index(messages, cursor)
    return sum(1 for message in messages[start + 1 :] if _is_model_visible_message(message))


def has_memory_writes_since(
    messages: Sequence[Mapping[str, Any]],
    cursor: MemoryCursor | None,
    *,
    memory_dir: str | Path | None = None,
) -> bool:
    start = _find_cursor_index(messages, cursor)
    for message in messages[start + 1 :]:
        if _message_contains_auto_memory_write(message, memory_dir=memory_dir):
            return True
    return False


def _message_contains_auto_memory_write(
    message: Mapping[str, Any],
    *,
    memory_dir: str | Path | None,
) -> bool:
    meta = message.get("_meta")
    if isinstance(meta, Mapping):
        if meta.get("tool_name") in {MEMORY_WRITE_TOOL_NAME, MEMORY_LOG_TOOL_NAME}:
            return True
        result_meta = meta.get("tool_result_metadata")
        if isinstance(result_meta, Mapping):
            if result_meta.get("type") in {"memory_write", "memory_log"}:
                return True
            path = result_meta.get("file_path") or result_meta.get("entrypoint_path")
            if isinstance(path, str) and _is_auto_memory_path(path, memory_dir):
                return True

    if message.get("role") == "assistant":
        for tool_call in _iter_assistant_tool_calls(message):
            tool_name = _tool_call_name(tool_call)
            if tool_name in {MEMORY_WRITE_TOOL_NAME, MEMORY_LOG_TOOL_NAME}:
                return True
            if tool_name in {FILE_EDIT_TOOL_NAME, FILE_WRITE_TOOL_NAME}:
                path = _tool_call_input(tool_call).get("file_path")
                if isinstance(path, str) and _is_auto_memory_path(path, memory_dir):
                    return True
    return False


def _iter_assistant_tool_calls(message: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if isinstance(call, Mapping):
                yield call
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                yield block


def _is_auto_memory_path(path: str, memory_dir: str | Path | None) -> bool:
    if memory_dir is not None:
        return _path_is_inside(path, Path(memory_dir).expanduser().resolve())
    return is_auto_mem_path(path)


def extract_written_paths(
    agent_messages: Sequence[Mapping[str, Any]],
    *,
    memory_dir: str | Path | None = None,
) -> list[str]:
    paths: list[str] = []
    for message in agent_messages:
        meta = message.get("_meta")
        if isinstance(meta, Mapping):
            result_meta = meta.get("tool_result_metadata")
            if isinstance(result_meta, Mapping):
                for key in ("file_path", "entrypoint_path"):
                    path = result_meta.get(key)
                    if isinstance(path, str) and _is_auto_memory_path(path, memory_dir):
                        paths.append(str(Path(path).expanduser().resolve()))

        if message.get("role") == "assistant":
            for tool_call in _iter_assistant_tool_calls(message):
                tool_name = _tool_call_name(tool_call)
                tool_input = _tool_call_input(tool_call)
                if tool_name in {FILE_EDIT_TOOL_NAME, FILE_WRITE_TOOL_NAME}:
                    path = tool_input.get("file_path")
                    if isinstance(path, str) and _is_auto_memory_path(path, memory_dir):
                        paths.append(str(Path(path).expanduser().resolve()))
                elif tool_name == MEMORY_WRITE_TOOL_NAME and memory_dir is not None:
                    filename = tool_input.get("filename")
                    title = tool_input.get("title") or "memory"
                    if isinstance(filename, str) and filename.strip():
                        candidate = Path(memory_dir) / filename
                    else:
                        slug = "".join(
                            ch.lower() if ch.isalnum() else "_"
                            for ch in str(title).strip()
                        ).strip("_")[:80] or "memory"
                        candidate = Path(memory_dir) / f"{slug}.md"
                    paths.append(str(candidate.expanduser().resolve()))

    return _uniq(paths)


def _uniq(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def create_memory_saved_message(memory_paths: Sequence[str]) -> dict[str, Any]:
    """Build a user-visible system message equivalent to OpenSpace memory saved note."""

    paths = [str(path) for path in memory_paths]
    if len(paths) == 1:
        content = f"Memory saved: {paths[0]}"
    else:
        rendered = "\n".join(f"- {path}" for path in paths)
        content = f"Memories saved:\n{rendered}"
    return {
        "role": "system",
        "content": content,
        "_meta": {
            "type": "memory_saved",
            "memory_paths": paths,
            "timestamp": time.time(),
        },
    }


async def _append_memory_saved_message(
    context: ToolUseContext,
    memory_paths: Sequence[str],
    append_system_message: AppendSystemMessageFn | None,
) -> None:
    message = create_memory_saved_message(memory_paths)
    if append_system_message is not None:
        result = append_system_message(message)
        if inspect.isawaitable(result):
            await result
    else:
        context.messages.append(message)
    await context.emit_event(
        "memory_saved",
        {"memory_paths": list(memory_paths), "message": message},
    )


_default_extractor: MemoryExtractor = MemoryExtractor()


def init_extract_memories(extractor: MemoryExtractor | None = None) -> MemoryExtractor:
    """Initialize global extraction state, matching OpenSpace ``initExtractMemories``."""

    global _default_extractor
    _default_extractor = extractor or MemoryExtractor()
    return _default_extractor


def get_memory_extractor() -> MemoryExtractor:
    return _default_extractor


async def execute_extract_memories(
    context: ToolUseContext,
    append_system_message: AppendSystemMessageFn | None = None,
) -> MemoryExtractionResult:
    """Run memory extraction.

    This no-ops until gates pass and is safe to schedule fire-and-forget.
    """

    return await _default_extractor.execute(context, append_system_message)


def submit_extract_memories(
    context: ToolUseContext,
    append_system_message: AppendSystemMessageFn | None = None,
) -> asyncio.Task[MemoryExtractionResult]:
    """Schedule background extraction and track it for draining."""

    return _default_extractor.submit(context, append_system_message)


async def drain_pending_extraction(
    timeout_s: float = 60.0,
    *,
    context: Any | None = None,
    scope_key: str | None = None,
) -> int:
    """Await all in-flight extraction tasks with a soft timeout."""

    return await _default_extractor.drain(
        timeout_s,
        context=context,
        scope_key=scope_key,
    )


__all__ = [
    "AutoMemCanUseToolFn",
    "MemoryCursor",
    "MemoryExtractionResult",
    "MemoryExtractor",
    "build_extract_auto_only_prompt",
    "build_extract_combined_prompt",
    "count_model_visible_messages_since",
    "create_auto_mem_can_use_tool",
    "create_memory_saved_message",
    "drain_pending_extraction",
    "execute_extract_memories",
    "extract_written_paths",
    "get_memory_extractor",
    "has_memory_writes_since",
    "init_extract_memories",
    "should_schedule_extract_memories",
    "submit_extract_memories",
]
