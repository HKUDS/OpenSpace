"""Session memory for the current OpenSpace conversation.

Implementation notes:
- ``services/SessionMemory/sessionMemory.ts`` (495 lines)
- ``services/SessionMemory/sessionMemoryUtils.ts`` (207 lines)
- ``services/SessionMemory/prompts.ts`` (324 lines)

OpenSpace keeps the same state machine and markdown-note structure, but uses a
provider-neutral lightweight tool loop instead of OpenSpace's Anthropic-only
``runForkedAgent`` prompt-cache fork.  The subagent is restricted to editing
the single session memory file.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from openspace.grounding.core.tool.base import BaseTool
from openspace.services.conversation.messages import (
    has_tool_calls_in_last_assistant_turn,
)
from openspace.services.memory.paths import get_openspace_config_home_dir
from openspace.services.memory.task_scope import (
    maybe_memory_task_scope_key,
    resolve_memory_task_scope_key,
)
from openspace.services.conversation.side_query import run_side_query
from openspace.services.tooling.context import ReadFileEntry, ToolUseContext

logger = logging.getLogger(__name__)


SESSION_MEMORY_FILENAME = "session_memory.md"
SESSION_MEMORY_CONFIG_DIR = "session-memory"
SESSION_MEMORY_TEMPLATE_PATH = ("config", "template.md")
SESSION_MEMORY_PROMPT_PATH = ("config", "prompt.md")

OPENSPACE_SESSION_MEMORY_ENABLED_ENV = "OPENSPACE_SESSION_MEMORY_ENABLED"
OPENSPACE_DISABLE_SESSION_MEMORY_ENV = "OPENSPACE_DISABLE_SESSION_MEMORY"
OPENSPACE_SESSION_MEMORY_SESSIONS_DIR_ENV = "OPENSPACE_SESSION_MEMORY_SESSIONS_DIR"
OPENSPACE_MEMORY_SESSION_MODEL_ENV = "OPENSPACE_MEMORY_SESSION_MODEL"
OPENSPACE_REMOTE_ENV = "OPENSPACE_REMOTE"

EXTRACTION_WAIT_TIMEOUT_MS = 15_000
EXTRACTION_STALE_THRESHOLD_MS = 60_000
EXTRACTION_POLL_INTERVAL_MS = 1_000

MAX_SECTION_LENGTH = 2_000
MAX_TOTAL_SESSION_MEMORY_TOKENS = 12_000
DEFAULT_MAX_SESSION_MEMORY_TURNS = 5

FILE_EDIT_TOOL_NAME = "edit"


@dataclass(frozen=True, slots=True)
class SessionMemoryConfig:
    """OpenSpace ``SessionMemoryConfig`` threshold knobs."""

    minimum_message_tokens_to_init: int = 10_000
    minimum_tokens_between_update: int = 5_000
    tool_calls_between_updates: int = 3


DEFAULT_SESSION_MEMORY_CONFIG = SessionMemoryConfig()


@dataclass(frozen=True, slots=True)
class MessageCursor:
    """Stable cursor for an OpenSpace message.

    OpenSpace stores UUIDs.  OpenSpace messages can lack UUIDs, so we use a stable
    fingerprint plus occurrence number to distinguish repeated equal messages.
    """

    fingerprint: str
    occurrence: int


@dataclass(slots=True)
class SessionMemoryExtractionResult:
    """Auditable result for tests and runtime events."""

    ran: bool = False
    skipped_reason: str | None = None
    memory_path: str | None = None
    turn_count: int = 0
    edited: bool = False
    error: str | None = None
    duration_ms: float = 0.0


@dataclass(slots=True)
class SessionMemoryRuntimeState:
    """Per-session runtime cursors and threshold bookkeeping."""

    last_memory_message_cursor: MessageCursor | None = None
    last_summarized_message_cursor: MessageCursor | None = None
    extraction_started_at_ms: float | None = None
    tokens_at_last_extraction: int = 0
    initialized: bool = False


AppendSystemMessageFn = Callable[[dict[str, Any]], Awaitable[None] | None]
SessionMemoryCanUseToolFn = Callable[
    [BaseTool | None, Mapping[str, Any]],
    Awaitable[dict[str, Any]],
]

_session_memory_config = DEFAULT_SESSION_MEMORY_CONFIG
_session_memory_states: dict[str, SessionMemoryRuntimeState] = {}
_session_memory_singleton: "SessionMemory | None" = None


DEFAULT_SESSION_MEMORY_TEMPLATE = """
# Session Title
_A short and distinctive 5-10 word descriptive title for the session. Super info dense, no filler_

# Current State
_What is actively being worked on right now? Pending tasks not yet completed. Immediate next steps._

# Task specification
_What did the user ask to build? Any design decisions or other explanatory context_

# Files and Functions
_What are the important files? In short, what do they contain and why are they relevant?_

# Workflow
_What bash commands are usually run and in what order? How to interpret their output if not obvious?_

# Errors & Corrections
_Errors encountered and how they were fixed. What did the user correct? What approaches failed and should not be tried again?_

# Codebase and System Documentation
_What are the important system components? How do they work/fit together?_

# Learnings
_What has worked well? What has not? What to avoid? Do not duplicate items from other sections_

# Key results
_If the user asked a specific output such as an answer to a question, a table, or other document, repeat the exact result here_

# Worklog
_Step by step, what was attempted, done? Very terse summary for each step_
"""


def get_default_update_prompt() -> str:
    return f"""IMPORTANT: This message and these instructions are NOT part of the actual user conversation. Do NOT include any references to "note-taking", "session notes extraction", or these update instructions in the notes content.

Based on the user conversation above (EXCLUDING this note-taking instruction message as well as system prompt, OPENSPACE.md entries, or any past session summaries), update the session notes file.

The file {{{{notesPath}}}} has already been read for you. Here are its current contents:
<current_notes_content>
{{{{currentNotes}}}}
</current_notes_content>

Your ONLY task is to use the Edit tool to update the notes file, then stop. You can make multiple edits (update every section as needed) - make all Edit tool calls in parallel in a single message. Do not call any other tools.

CRITICAL RULES FOR EDITING:
- The file must maintain its exact structure with all sections, headers, and italic descriptions intact
-- NEVER modify, delete, or add section headers (the lines starting with '#' like # Task specification)
-- NEVER modify or delete the italic _section description_ lines (these are the lines in italics immediately following each header - they start and end with underscores)
-- The italic _section descriptions_ are TEMPLATE INSTRUCTIONS that must be preserved exactly as-is - they guide what content belongs in each section
-- ONLY update the actual content that appears BELOW the italic _section descriptions_ within each existing section
-- Do NOT add any new sections, summaries, or information outside the existing structure
- Do NOT reference this note-taking process or instructions anywhere in the notes
- It's OK to skip updating a section if there are no substantial new insights to add. Do not add filler content like "No info yet", just leave sections blank/unedited if appropriate.
- Write DETAILED, INFO-DENSE content for each section - include specifics like file paths, function names, error messages, exact commands, technical details, etc.
- For "Key results", include the complete, exact output the user requested (e.g., full table, full answer, etc.)
- Do not include information that's already in the OPENSPACE.md files included in the context
- Keep each section under ~{MAX_SECTION_LENGTH} tokens/words - if a section is approaching this limit, condense it by cycling out less important details while preserving the most critical information
- Focus on actionable, specific information that would help someone understand or recreate the work discussed in the conversation
- IMPORTANT: Always update "Current State" to reflect the most recent work - this is critical for continuity after compaction

Use the Edit tool with file_path: {{{{notesPath}}}}

STRUCTURE PRESERVATION REMINDER:
Each section has TWO parts that must be preserved exactly as they appear in the current file:
1. The section header (line starting with #)
2. The italic description line (the _italicized text_ immediately after the header - this is a template instruction)

You ONLY update the actual content that comes AFTER these two preserved lines. The italic description lines starting and ending with underscores are part of the template structure, NOT content to be edited or removed.

REMEMBER: Use the Edit tool in parallel and stop. Do not continue after the edits. Only include insights from the actual user conversation, never from these note-taking instructions. Do not delete or change section headers or italic _section descriptions_."""


def _env_truthy(value: str | None) -> bool:
    return value is not None and value.lower() in {"1", "true", "yes", "on"}


def _env_defined_falsy(value: str | None) -> bool:
    return value is not None and value.lower() in {"0", "false", "no", "off", ""}


def is_session_memory_enabled() -> bool:
    """Return whether session-memory extraction is enabled.

    Explicit env vars are the local configuration surface. The default is on
    because extraction is still guarded by the 10k/5k token thresholds.
    """

    if _env_truthy(os.environ.get(OPENSPACE_DISABLE_SESSION_MEMORY_ENV)):
        return False
    enabled = os.environ.get(OPENSPACE_SESSION_MEMORY_ENABLED_ENV)
    if _env_defined_falsy(enabled):
        return False
    if _env_truthy(enabled):
        return True
    return True


def get_session_memory_sessions_dir(sessions_dir: str | Path | None = None) -> Path:
    raw = sessions_dir or os.environ.get(OPENSPACE_SESSION_MEMORY_SESSIONS_DIR_ENV)
    if raw:
        return Path(raw).expanduser().resolve()
    return get_openspace_config_home_dir() / "sessions"


def _sanitize_session_id(session_id: str | None, *, cwd: str | Path | None = None) -> str:
    raw = str(session_id or "").strip()
    if raw and re.fullmatch(r"[A-Za-z0-9_.:-]+", raw):
        return raw
    fallback = raw or str(cwd or os.getcwd())
    return hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:32]


def get_session_memory_dir(
    session_id: str | None,
    *,
    cwd: str | Path | None = None,
    sessions_dir: str | Path | None = None,
) -> Path:
    """Return ``~/.openspace/sessions/<session_id>/`` for session memory."""

    return get_session_memory_sessions_dir(sessions_dir) / _sanitize_session_id(
        session_id,
        cwd=cwd,
    )


def get_session_memory_path(
    session_id: str | None,
    *,
    cwd: str | Path | None = None,
    sessions_dir: str | Path | None = None,
) -> Path:
    return get_session_memory_dir(
        session_id,
        cwd=cwd,
        sessions_dir=sessions_dir,
    ) / SESSION_MEMORY_FILENAME


def get_session_memory_path_for_context(context: ToolUseContext) -> Path:
    session_dir = getattr(context, "session_dir", None)
    if session_dir:
        return Path(session_dir).expanduser().resolve() / SESSION_MEMORY_FILENAME
    return get_session_memory_path(context.session_id, cwd=context.cwd)


def get_session_transcript_path(
    session_id: str | None,
    *,
    cwd: str | Path | None = None,
    sessions_dir: str | Path | None = None,
) -> Path:
    """Return the OS session transcript path when a session id is known."""

    sanitized = _sanitize_session_id(session_id, cwd=cwd)
    return get_session_memory_sessions_dir(sessions_dir) / f"{sanitized}.messages"


def load_session_memory_template() -> str:
    template_path = (
        get_openspace_config_home_dir()
        / SESSION_MEMORY_CONFIG_DIR
        / SESSION_MEMORY_TEMPLATE_PATH[0]
        / SESSION_MEMORY_TEMPLATE_PATH[1]
    )
    try:
        return template_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DEFAULT_SESSION_MEMORY_TEMPLATE
    except OSError:
        logger.debug("Failed to read session memory template", exc_info=True)
        return DEFAULT_SESSION_MEMORY_TEMPLATE


def load_session_memory_prompt() -> str:
    prompt_path = (
        get_openspace_config_home_dir()
        / SESSION_MEMORY_CONFIG_DIR
        / SESSION_MEMORY_PROMPT_PATH[0]
        / SESSION_MEMORY_PROMPT_PATH[1]
    )
    try:
        return prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return get_default_update_prompt()
    except OSError:
        logger.debug("Failed to read session memory prompt", exc_info=True)
        return get_default_update_prompt()


def _rough_token_count(text: str) -> int:
    return round(len(text) / 4)


def _analyze_section_sizes(content: str) -> dict[str, int]:
    sections: dict[str, int] = {}
    current_section = ""
    current_content: list[str] = []

    for line in content.split("\n"):
        if line.startswith("# "):
            if current_section and current_content:
                sections[current_section] = _rough_token_count(
                    "\n".join(current_content).strip()
                )
            current_section = line
            current_content = []
        else:
            current_content.append(line)

    if current_section and current_content:
        sections[current_section] = _rough_token_count(
            "\n".join(current_content).strip()
        )
    return sections


def _generate_section_reminders(
    section_sizes: Mapping[str, int],
    total_tokens: int,
) -> str:
    over_budget = total_tokens > MAX_TOTAL_SESSION_MEMORY_TOKENS
    oversized = sorted(
        (
            (section, tokens)
            for section, tokens in section_sizes.items()
            if tokens > MAX_SECTION_LENGTH
        ),
        key=lambda item: item[1],
        reverse=True,
    )

    if not oversized and not over_budget:
        return ""

    parts: list[str] = []
    if over_budget:
        parts.append(
            f"\n\nCRITICAL: The session memory file is currently ~{total_tokens} tokens, "
            f"which exceeds the maximum of {MAX_TOTAL_SESSION_MEMORY_TOKENS} tokens. "
            'You MUST condense the file to fit within this budget. Aggressively shorten oversized sections by removing less important details, merging related items, and summarizing older entries. Prioritize keeping "Current State" and "Errors & Corrections" accurate and detailed.'
        )

    if oversized:
        rendered = "\n".join(
            f'- "{section}" is ~{tokens} tokens (limit: {MAX_SECTION_LENGTH})'
            for section, tokens in oversized
        )
        parts.append(
            "\n\n"
            + (
                "Oversized sections to condense"
                if over_budget
                else "IMPORTANT: The following sections exceed the per-section limit and MUST be condensed"
            )
            + ":\n"
            + rendered
        )

    return "".join(parts)


def _substitute_variables(template: str, variables: Mapping[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return variables[key] if key in variables else match.group(0)

    return re.sub(r"\{\{(\w+)\}\}", repl, template)


def build_session_memory_update_prompt(current_notes: str, notes_path: str) -> str:
    prompt_template = load_session_memory_prompt()
    section_sizes = _analyze_section_sizes(current_notes)
    total_tokens = _rough_token_count(current_notes)
    section_reminders = _generate_section_reminders(section_sizes, total_tokens)
    base_prompt = _substitute_variables(
        prompt_template,
        {
            "currentNotes": current_notes,
            "notesPath": notes_path,
        },
    )
    return base_prompt + section_reminders


def is_session_memory_empty(content: str) -> bool:
    return content.strip() == load_session_memory_template().strip()


def truncate_session_memory_for_compact(content: str) -> tuple[str, bool]:
    lines = content.split("\n")
    max_chars_per_section = MAX_SECTION_LENGTH * 4
    output_lines: list[str] = []
    current_header = ""
    current_lines: list[str] = []
    was_truncated = False

    for line in lines:
        if line.startswith("# "):
            flushed, truncated = _flush_session_section(
                current_header,
                current_lines,
                max_chars_per_section,
            )
            output_lines.extend(flushed)
            was_truncated = was_truncated or truncated
            current_header = line
            current_lines = []
        else:
            current_lines.append(line)

    flushed, truncated = _flush_session_section(
        current_header,
        current_lines,
        max_chars_per_section,
    )
    output_lines.extend(flushed)
    was_truncated = was_truncated or truncated
    return "\n".join(output_lines), was_truncated


def _flush_session_section(
    section_header: str,
    section_lines: Sequence[str],
    max_chars_per_section: int,
) -> tuple[list[str], bool]:
    if not section_header:
        return list(section_lines), False

    section_content = "\n".join(section_lines)
    if len(section_content) <= max_chars_per_section:
        return [section_header, *section_lines], False

    char_count = 0
    kept = [section_header]
    for line in section_lines:
        if char_count + len(line) + 1 > max_chars_per_section:
            break
        kept.append(line)
        char_count += len(line) + 1
    kept.append("\n[... section truncated for length ...]")
    return kept, True


def get_session_memory_config() -> SessionMemoryConfig:
    return _session_memory_config


def set_session_memory_config(config: SessionMemoryConfig | Mapping[str, int]) -> None:
    global _session_memory_config
    if isinstance(config, SessionMemoryConfig):
        _session_memory_config = config
        return
    current = _session_memory_config
    _session_memory_config = SessionMemoryConfig(
        minimum_message_tokens_to_init=int(
            config.get(
                "minimum_message_tokens_to_init",
                config.get("minimumMessageTokensToInit", current.minimum_message_tokens_to_init),
            )
        ),
        minimum_tokens_between_update=int(
            config.get(
                "minimum_tokens_between_update",
                config.get("minimumTokensBetweenUpdate", current.minimum_tokens_between_update),
            )
        ),
        tool_calls_between_updates=int(
            config.get(
                "tool_calls_between_updates",
                config.get("toolCallsBetweenUpdates", current.tool_calls_between_updates),
            )
        ),
    )


def resolve_session_memory_state_key(context: Any) -> str:
    """Return the stable key used for this session's memory runtime state."""
    return resolve_memory_task_scope_key(context)


def get_session_memory_runtime_state(context: Any) -> SessionMemoryRuntimeState:
    key = resolve_session_memory_state_key(context)
    state = _session_memory_states.get(key)
    if state is None:
        state = SessionMemoryRuntimeState()
        _session_memory_states[key] = state
    return state


def mark_extraction_started(context: Any) -> None:
    get_session_memory_runtime_state(context).extraction_started_at_ms = (
        time.time() * 1000
    )


def mark_extraction_completed(
    context: Any,
    token_count: int | None = None,
) -> None:
    state = get_session_memory_runtime_state(context)
    state.extraction_started_at_ms = None
    if token_count is not None:
        state.tokens_at_last_extraction = int(token_count)


def _active_extraction_starts(context: Any | None) -> list[float]:
    if context is not None:
        started = get_session_memory_runtime_state(context).extraction_started_at_ms
        return [] if started is None else [started]
    return [
        started
        for state in _session_memory_states.values()
        if (started := state.extraction_started_at_ms) is not None
    ]


async def wait_for_session_memory_extraction(context: Any | None = None) -> None:
    """Wait for in-flight extraction, with OpenSpace's timeout and stale caps."""

    started = time.time() * 1000
    while starts := _active_extraction_starts(context):
        age = time.time() * 1000 - min(starts)
        if age > EXTRACTION_STALE_THRESHOLD_MS:
            return
        if time.time() * 1000 - started > EXTRACTION_WAIT_TIMEOUT_MS:
            return
        await asyncio.sleep(EXTRACTION_POLL_INTERVAL_MS / 1000)


def read_session_memory(
    session_id: str | None,
    *,
    cwd: str | Path | None = None,
    sessions_dir: str | Path | None = None,
) -> str | None:
    path = get_session_memory_path(session_id, cwd=cwd, sessions_dir=sessions_dir)
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.debug("Failed to read session memory", exc_info=True)
        return None
    return content


def write_session_memory(
    session_id: str | None,
    content: str,
    *,
    cwd: str | Path | None = None,
    sessions_dir: str | Path | None = None,
) -> Path:
    path = get_session_memory_path(session_id, cwd=cwd, sessions_dir=sessions_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def setup_session_memory_file(context: ToolUseContext) -> tuple[Path, str]:
    path = get_session_memory_path_for_context(context)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        path.write_text(load_session_memory_template(), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    current = path.read_text(encoding="utf-8")
    try:
        stat = path.stat()
        timestamp = stat.st_mtime
    except OSError:
        timestamp = time.time()
    context.read_file_state[str(path.resolve())] = ReadFileEntry(
        content=current,
        timestamp=timestamp,
    )
    return path, current


def has_met_initialization_threshold(current_token_count: int) -> bool:
    return (
        current_token_count
        >= _session_memory_config.minimum_message_tokens_to_init
    )


def has_met_update_threshold(context: Any, current_token_count: int) -> bool:
    state = get_session_memory_runtime_state(context)
    return (
        current_token_count - state.tokens_at_last_extraction
        >= _session_memory_config.minimum_tokens_between_update
    )


def get_tool_calls_between_updates() -> int:
    return _session_memory_config.tool_calls_between_updates


def get_last_summarized_message_id(context: Any) -> MessageCursor | None:
    return get_session_memory_runtime_state(context).last_summarized_message_cursor


def set_last_summarized_message_id(
    context: Any,
    cursor: MessageCursor | None,
) -> None:
    get_session_memory_runtime_state(context).last_summarized_message_cursor = cursor


def reset_session_memory_state() -> None:
    global _session_memory_config

    _session_memory_config = DEFAULT_SESSION_MEMORY_CONFIG
    _session_memory_states.clear()
    if _session_memory_singleton is not None:
        _session_memory_singleton._reset_runtime_state()


def message_cursor_for_index(
    messages: Sequence[Mapping[str, Any]],
    index: int,
) -> MessageCursor:
    fingerprint = _message_fingerprint(messages[index])
    occurrence = 0
    for item in messages[: index + 1]:
        if _message_fingerprint(item) == fingerprint:
            occurrence += 1
    return MessageCursor(fingerprint=fingerprint, occurrence=occurrence)


def find_message_cursor_index(
    messages: Sequence[Mapping[str, Any]],
    cursor: MessageCursor | None,
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


def _message_fingerprint(message: Mapping[str, Any]) -> str:
    meta = message.get("_meta")
    if isinstance(meta, Mapping):
        for key in ("uuid", "message_uuid", "id", "response_id"):
            value = meta.get(key)
            if isinstance(value, str) and value:
                return f"meta:{key}:{value}"
    for key in ("uuid", "id", "tool_call_id"):
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
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def count_tool_calls_since(
    messages: Sequence[Mapping[str, Any]],
    since_cursor: MessageCursor | None,
) -> int:
    start_index = find_message_cursor_index(messages, since_cursor)
    count = 0
    for message in messages[start_index + 1 :]:
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, Sequence) and not isinstance(
            tool_calls,
            (str, bytes, bytearray),
        ):
            count += len(tool_calls)
        content = message.get("content")
        if isinstance(content, Sequence) and not isinstance(
            content,
            (str, bytes, bytearray),
        ):
            count += sum(
                1
                for block in content
                if isinstance(block, Mapping) and block.get("type") == "tool_use"
            )
    return count


def should_extract_memory(
    messages: Sequence[Mapping[str, Any]],
    context: Any,
) -> bool:
    from openspace.services.conversation.compact import token_count_with_estimation

    state = get_session_memory_runtime_state(context)
    current_token_count = token_count_with_estimation(messages)
    if not state.initialized:
        if not has_met_initialization_threshold(current_token_count):
            return False
        state.initialized = True

    has_token_threshold = (
        current_token_count - state.tokens_at_last_extraction
        >= _session_memory_config.minimum_tokens_between_update
    )
    tool_calls_since = count_tool_calls_since(
        messages,
        state.last_memory_message_cursor,
    )
    has_tool_threshold = tool_calls_since >= get_tool_calls_between_updates()
    has_tool_calls_in_last_turn = has_tool_calls_in_last_assistant_turn(messages)

    should_extract = (has_token_threshold and has_tool_threshold) or (
        has_token_threshold and not has_tool_calls_in_last_turn
    )
    if should_extract and messages:
        state.last_memory_message_cursor = message_cursor_for_index(
            messages,
            len(messages) - 1,
        )
        return True
    return False


def should_schedule_session_memory(context: ToolUseContext) -> bool:
    if _should_skip_context(context):
        return False
    if not getattr(context, "session_id", None):
        return False
    if _env_truthy(os.environ.get(OPENSPACE_REMOTE_ENV)):
        return False
    if not is_session_memory_enabled():
        return False
    try:
        from openspace.services.conversation.compact import is_auto_compact_enabled

        if not is_auto_compact_enabled():
            return False
    except Exception:
        pass
    return True


def _should_skip_context(context: ToolUseContext) -> bool:
    if getattr(context, "is_async_agent", False):
        return True
    if getattr(context, "parent_task_id", None):
        return True
    if getattr(context, "agent_type", None) in {
        "extract_memories",
        "auto_dream",
        "session_memory",
    }:
        return True
    return False


def _background_task_id(context: ToolUseContext, key: str) -> str | None:
    task_ids = getattr(context, "background_task_ids", None)
    if isinstance(task_ids, dict):
        value = task_ids.get(key)
        return str(value) if value else None
    return None


async def _emit_session_memory_skipped(
    context: ToolUseContext,
    reason: str,
    **extra: Any,
) -> None:
    await context.emit_event(
        "session_memory_extraction_skipped",
        {
            "task_id": _background_task_id(context, "session_memory"),
            "reason": reason,
            **extra,
        },
    )


class SessionMemory:
    """Stateful session memory extractor.

    One singleton coordinates background tasks; runtime cursors are session-scoped.
    """

    def __init__(self, *, max_turns: int = DEFAULT_MAX_SESSION_MEMORY_TURNS) -> None:
        self.max_turns = max(1, int(max_turns))
        self._in_flight: set[asyncio.Task[SessionMemoryExtractionResult]] = set()
        self._task_scope_keys: dict[
            asyncio.Task[SessionMemoryExtractionResult],
            str | None,
        ] = {}
        self._in_progress_session_keys: set[str] = set()
        self._pending_contexts: dict[
            str,
            tuple[
                ToolUseContext,
                AppendSystemMessageFn | None,
                bool,
            ],
        ] = {}

    def _reset_runtime_state(self) -> None:
        self._in_progress_session_keys.clear()
        self._pending_contexts.clear()
        self._task_scope_keys.clear()

    async def extract(
        self,
        context: ToolUseContext,
        append_system_message: AppendSystemMessageFn | None = None,
        *,
        force: bool = False,
    ) -> SessionMemoryExtractionResult:
        task = asyncio.current_task()
        if task is not None:
            self._in_flight.add(task)  # type: ignore[arg-type]
            self._task_scope_keys[task] = maybe_memory_task_scope_key(context)  # type: ignore[index]
        try:
            return await self._extract_impl(context, append_system_message, force=force)
        finally:
            if task is not None:
                self._in_flight.discard(task)  # type: ignore[arg-type]
                self._task_scope_keys.pop(task, None)

    def submit(
        self,
        context: ToolUseContext,
        append_system_message: AppendSystemMessageFn | None = None,
        *,
        force: bool = False,
    ) -> asyncio.Task[SessionMemoryExtractionResult]:
        task = asyncio.create_task(
            self.extract(context, append_system_message, force=force)
        )
        self._in_flight.add(task)
        self._task_scope_keys[task] = maybe_memory_task_scope_key(context)

        def _done(done: asyncio.Task[SessionMemoryExtractionResult]) -> None:
            self._in_flight.discard(done)
            self._task_scope_keys.pop(done, None)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("Background session memory extraction failed", exc_info=True)

        task.add_done_callback(_done)
        return task

    async def drain(
        self,
        timeout_s: float = 60.0,
        *,
        context: Any | None = None,
        scope_key: str | None = None,
    ) -> int:
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
                logger.debug("Session memory extraction failed during drain", exc_info=True)
        return len(_pending)

    async def _extract_impl(
        self,
        context: ToolUseContext,
        append_system_message: AppendSystemMessageFn | None,
        *,
        force: bool,
    ) -> SessionMemoryExtractionResult:
        if not should_schedule_session_memory(context):
            await _emit_session_memory_skipped(context, "disabled")
            return SessionMemoryExtractionResult(skipped_reason="disabled")

        if not force and not should_extract_memory(
            list(context.messages or []),
            context,
        ):
            await _emit_session_memory_skipped(context, "threshold")
            return SessionMemoryExtractionResult(skipped_reason="threshold")

        session_key = resolve_session_memory_state_key(context)
        if session_key in self._in_progress_session_keys:
            self._pending_contexts[session_key] = (
                context,
                append_system_message,
                force,
            )
            await context.emit_event("session_memory_extraction_coalesced", {})
            await _emit_session_memory_skipped(context, "coalesced")
            return SessionMemoryExtractionResult(skipped_reason="coalesced")

        return await self._run_extraction_chain(
            context,
            append_system_message,
            force=force,
            session_key=session_key,
        )

    async def _run_extraction_chain(
        self,
        context: ToolUseContext,
        append_system_message: AppendSystemMessageFn | None,
        *,
        force: bool,
        session_key: str,
    ) -> SessionMemoryExtractionResult:
        self._in_progress_session_keys.add(session_key)
        try:
            result = await self._run_extraction(context, append_system_message)
            while trailing := self._pending_contexts.pop(session_key, None):
                await context.emit_event("session_memory_extraction_trailing_start", {})
                await self._run_extraction(trailing[0], trailing[1])
            return result
        finally:
            self._in_progress_session_keys.discard(session_key)

    async def _run_extraction(
        self,
        context: ToolUseContext,
        append_system_message: AppendSystemMessageFn | None,
    ) -> SessionMemoryExtractionResult:
        start = time.time()
        mark_extraction_started(context)
        memory_path: Path | None = None
        turn_count = 0
        completed_token_count: int | None = None
        try:
            llm_client = context.llm_client
            if llm_client is None or not hasattr(llm_client, "call_model"):
                await _emit_session_memory_skipped(context, "missing_llm_client")
                return SessionMemoryExtractionResult(skipped_reason="missing_llm_client")

            setup_context = _build_session_memory_context(
                parent=context,
                tools=[],
                messages=list(context.messages or []),
            )
            memory_path, current_memory = setup_session_memory_file(setup_context)
            prompt = build_session_memory_update_prompt(
                current_memory,
                str(memory_path),
            )

            tools = _select_session_memory_tools(context)
            if not tools:
                await _emit_session_memory_skipped(
                    context,
                    "missing_tools",
                    memory_path=str(memory_path),
                )
                return SessionMemoryExtractionResult(
                    skipped_reason="missing_tools",
                    memory_path=str(memory_path),
                )

            gate = create_memory_file_can_use_tool(memory_path)
            read_file_state = dict(context.read_file_state)
            read_file_state[str(memory_path.resolve())] = (
                setup_context.read_file_state[str(memory_path.resolve())]
            )
            model_override = os.environ.get(OPENSPACE_MEMORY_SESSION_MODEL_ENV) or None
            side_result = await run_side_query(
                prompt,
                tools=tools,
                model=model_override,
                parent_context=context,
                llm_client=llm_client,
                messages=list(context.messages or []),
                max_turns=self.max_turns,
                can_use_tool=gate,
                query_source="session_memory",
                fork_label="session_memory",
                agent_type="session_memory",
                denied_result_type="session_memory_tool_denied",
                read_file_state=read_file_state,
                tui_available=False,
                is_async_agent=True,
            )
            turn_count = side_result.turn_count
            total_usage = side_result.total_usage
            result_messages = side_result.messages

            current_tokens = _token_count_with_estimation_safe(context.messages or [])
            completed_token_count = current_tokens
            _update_last_summarized_message_id_if_safe(context, context.messages or [])
            edited = _session_memory_was_edited(result_messages, memory_path)
            duration_ms = (time.time() - start) * 1000
            await context.emit_event(
                "session_memory_extraction_complete",
                {
                    "task_id": _background_task_id(context, "session_memory"),
                    "memory_path": str(memory_path),
                    "turn_count": turn_count,
                    "edited": edited,
                    "input_tokens": total_usage.input_tokens,
                    "output_tokens": total_usage.output_tokens,
                    "duration_ms": duration_ms,
                },
            )
            await _append_session_memory_message(
                context,
                memory_path,
                append_system_message,
                edited=edited,
            )
            return SessionMemoryExtractionResult(
                ran=True,
                memory_path=str(memory_path),
                turn_count=turn_count,
                edited=edited,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            duration_ms = (time.time() - start) * 1000
            logger.debug("Session memory extraction error: %s", exc, exc_info=True)
            await context.emit_event(
                "session_memory_extraction_error",
                {
                    "task_id": _background_task_id(context, "session_memory"),
                    "memory_path": str(memory_path) if memory_path else None,
                    "error": str(exc),
                    "duration_ms": duration_ms,
                },
            )
            return SessionMemoryExtractionResult(
                ran=True,
                memory_path=str(memory_path) if memory_path else None,
                turn_count=turn_count,
                duration_ms=duration_ms,
                error=str(exc),
            )
        finally:
            mark_extraction_completed(context, completed_token_count)


def get_session_memory() -> SessionMemory:
    global _session_memory_singleton
    if _session_memory_singleton is None:
        _session_memory_singleton = SessionMemory()
    return _session_memory_singleton


def init_session_memory() -> SessionMemory:
    """Return the singleton for parity with OpenSpace's synchronous initializer."""

    return get_session_memory()


async def extract_session_memory(
    context: ToolUseContext,
    append_system_message: AppendSystemMessageFn | None = None,
    *,
    force: bool = False,
) -> SessionMemoryExtractionResult:
    return await get_session_memory().extract(
        context,
        append_system_message,
        force=force,
    )


def submit_session_memory_extraction(
    context: ToolUseContext,
    append_system_message: AppendSystemMessageFn | None = None,
    *,
    force: bool = False,
) -> asyncio.Task[SessionMemoryExtractionResult]:
    return get_session_memory().submit(
        context,
        append_system_message,
        force=force,
    )


async def drain_pending_session_memory(
    timeout_s: float = 60.0,
    *,
    context: Any | None = None,
    scope_key: str | None = None,
) -> int:
    return await get_session_memory().drain(
        timeout_s,
        context=context,
        scope_key=scope_key,
    )


async def manually_extract_session_memory(
    messages: Sequence[Mapping[str, Any]],
    context: ToolUseContext,
) -> SessionMemoryExtractionResult:
    context.replace_messages([dict(message) for message in messages])
    return await extract_session_memory(context, force=True)


def create_memory_file_can_use_tool(
    memory_path: str | Path,
) -> SessionMemoryCanUseToolFn:
    allowed_path = Path(memory_path).expanduser().resolve()

    async def can_use_tool(
        tool: BaseTool | None,
        input: Mapping[str, Any],
    ) -> dict[str, Any]:
        if tool is None:
            return _deny_session_memory_tool(
                "unknown",
                "Tool is not available to the session-memory extraction agent.",
            )
        name = getattr(tool, "name", "")
        data = dict(input)
        if name != FILE_EDIT_TOOL_NAME:
            return _deny_session_memory_tool(
                name or "unknown",
                f"only {FILE_EDIT_TOOL_NAME} on {allowed_path} is allowed",
            )
        file_path = data.get("file_path")
        if isinstance(file_path, str):
            try:
                candidate = Path(file_path).expanduser()
                if not candidate.is_absolute():
                    candidate = allowed_path.parent / candidate
                if candidate.resolve() == allowed_path:
                    return {"behavior": "allow", "updated_input": data}
            except OSError:
                pass
        return _deny_session_memory_tool(
            name,
            f"only {FILE_EDIT_TOOL_NAME} on {allowed_path} is allowed",
        )

    return can_use_tool


def _deny_session_memory_tool(tool_name: str, reason: str) -> dict[str, Any]:
    logger.debug("[sessionMemory] denied %s: %s", tool_name, reason)
    return {
        "behavior": "deny",
        "message": reason,
        "decision_reason": {"type": "other", "reason": reason},
    }


def _select_session_memory_tools(context: ToolUseContext) -> list[BaseTool]:
    selected: list[BaseTool] = []
    seen: set[str] = set()
    for tool in [*(context.all_tools or []), *(context.tools or [])]:
        name = getattr(tool, "name", "")
        if name == FILE_EDIT_TOOL_NAME and name not in seen:
            selected.append(tool)
            seen.add(name)

    if FILE_EDIT_TOOL_NAME not in seen:
        try:
            from openspace.grounding.backends.shell.file_tools import FileEditTool

            selected.append(FileEditTool())
        except Exception:
            logger.debug("Could not instantiate FileEditTool for session memory", exc_info=True)
    return selected


def _build_session_memory_context(
    *,
    parent: ToolUseContext,
    tools: list[BaseTool],
    messages: list[dict[str, Any]],
) -> ToolUseContext:
    return ToolUseContext(
        tools=list(tools),
        all_tools=list(tools),
        model=parent.model,
        llm_client=parent.llm_client,
        cwd=parent.cwd,
        original_cwd=parent.original_cwd,
        agent_id=f"{parent.agent_id}:session_memory",
        agent_type="session_memory",
        max_result_size_chars=parent.max_result_size_chars,
        abort_event=parent.abort_event,
        messages=messages,
        read_file_state=dict(parent.read_file_state),
        tool_results_token_count=0,
        permission_engine=parent.permission_engine,
        permission_mode=parent.permission_mode,
        permission_context=parent.permission_context,
        hook_registry=parent.hook_registry,
        tui_available=False,
        is_async_agent=True,
        event_sink=parent.event_sink,
        recording_manager=None,
        quality_manager=None,
        parent_task_id=parent.parent_task_id,
        task_description=parent.task_description,
        current_iteration=0,
        max_iterations=DEFAULT_MAX_SESSION_MEMORY_TURNS,
        session_id=parent.session_id,
        session_dir=parent.session_dir,
        tool_results_dir=parent.tool_results_dir,
    )


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


def _token_count_with_estimation_safe(messages: Sequence[Mapping[str, Any]]) -> int:
    try:
        from openspace.services.conversation.compact import token_count_with_estimation

        return token_count_with_estimation(messages)
    except Exception:
        return sum(_rough_token_count(str(message.get("content", ""))) for message in messages)


def _update_last_summarized_message_id_if_safe(
    context: Any,
    messages: Sequence[Mapping[str, Any]],
) -> None:
    if not messages:
        return
    if has_tool_calls_in_last_assistant_turn(messages):
        return
    set_last_summarized_message_id(
        context,
        message_cursor_for_index(messages, len(messages) - 1)
    )


def _session_memory_was_edited(
    messages: Sequence[Mapping[str, Any]],
    memory_path: Path | None,
) -> bool:
    if memory_path is None:
        return False
    target = str(memory_path.expanduser().resolve())
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if _tool_call_name(call) != FILE_EDIT_TOOL_NAME:
                continue
            file_path = _tool_call_input(call).get("file_path")
            if isinstance(file_path, str):
                try:
                    if str(Path(file_path).expanduser().resolve()) == target:
                        return True
                except OSError:
                    continue
    return False


async def _append_session_memory_message(
    context: ToolUseContext,
    memory_path: Path,
    append_system_message: AppendSystemMessageFn | None,
    *,
    edited: bool,
) -> None:
    message = {
        "role": "system",
        "content": (
            f"Session memory updated: {memory_path}"
            if edited
            else f"Session memory checked: {memory_path}"
        ),
        "_meta": {
            "type": "session_memory_updated" if edited else "session_memory_checked",
            "memory_path": str(memory_path),
            "timestamp": time.time(),
        },
    }
    if append_system_message is not None:
        result = append_system_message(message)
        if inspect.isawaitable(result):
            await result
    else:
        await context.emit_event(
            "session_memory_updated" if edited else "session_memory_checked",
            {"memory_path": str(memory_path), "edited": edited},
        )
