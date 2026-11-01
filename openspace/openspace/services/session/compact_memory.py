"""Session-memory-backed compaction.

This module returns the same ``CompactionResult`` shape as the LLM compact
path, but uses the current session-memory notes as the summary and preserves a
recent verbatim suffix.  If any gate fails it returns ``None`` so the standard
LLM compact path remains the fallback.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from openspace.services.conversation.messages import (
    build_compact_boundary_message,
    build_compact_summary_message,
    extract_discovered_tool_names,
    get_message_uuid,
    is_compact_boundary_message,
)
from openspace.services.memory.session_memory import (
    find_message_cursor_index,
    get_last_summarized_message_id,
    get_session_memory_path,
    is_session_memory_empty,
    read_session_memory,
    set_last_summarized_message_id,
    truncate_session_memory_for_compact,
    wait_for_session_memory_extraction,
)

if TYPE_CHECKING:
    from openspace.services.conversation.compact import CompactionResult
    from openspace.services.tooling.hooks import HookRegistry
    from openspace.services.tooling.context import ToolUseContext

logger = logging.getLogger(__name__)

OPENSPACE_SESSION_MEMORY_COMPACT_ENABLED_ENV = (
    "OPENSPACE_SESSION_MEMORY_COMPACT_ENABLED"
)
OPENSPACE_DISABLE_SESSION_MEMORY_COMPACT_ENV = (
    "OPENSPACE_DISABLE_SESSION_MEMORY_COMPACT"
)
OPENSPACE_SM_COMPACT_MIN_TOKENS_ENV = "OPENSPACE_SM_COMPACT_MIN_TOKENS"
OPENSPACE_SM_COMPACT_MIN_TEXT_MESSAGES_ENV = (
    "OPENSPACE_SM_COMPACT_MIN_TEXT_MESSAGES"
)
OPENSPACE_SM_COMPACT_MAX_TOKENS_ENV = "OPENSPACE_SM_COMPACT_MAX_TOKENS"


@dataclass(frozen=True, slots=True)
class SessionMemoryCompactConfig:
    """OpenSpace session memory compaction thresholds."""

    min_tokens: int = 10_000
    min_text_block_messages: int = 5
    max_tokens: int = 40_000


DEFAULT_SM_COMPACT_CONFIG = SessionMemoryCompactConfig()
_sm_compact_config = DEFAULT_SM_COMPACT_CONFIG


def _env_truthy(value: str | None) -> bool:
    return value is not None and value.lower() in {"1", "true", "yes", "on"}


def _env_defined_falsy(value: str | None) -> bool:
    return value is not None and value.lower() in {"0", "false", "no", "off", ""}


def set_session_memory_compact_config(
    config: SessionMemoryCompactConfig | Mapping[str, int],
) -> None:
    global _sm_compact_config
    if isinstance(config, SessionMemoryCompactConfig):
        _sm_compact_config = config
        return
    current = _sm_compact_config
    _sm_compact_config = SessionMemoryCompactConfig(
        min_tokens=int(config.get("min_tokens", config.get("minTokens", current.min_tokens))),
        min_text_block_messages=int(
            config.get(
                "min_text_block_messages",
                config.get("minTextBlockMessages", current.min_text_block_messages),
            )
        ),
        max_tokens=int(config.get("max_tokens", config.get("maxTokens", current.max_tokens))),
    )


def get_session_memory_compact_config() -> SessionMemoryCompactConfig:
    return _sm_compact_config


def reset_session_memory_compact_config() -> None:
    global _sm_compact_config
    _sm_compact_config = DEFAULT_SM_COMPACT_CONFIG


def _config_from_env() -> SessionMemoryCompactConfig:
    return SessionMemoryCompactConfig(
        min_tokens=_parse_positive_int(
            os.environ.get(OPENSPACE_SM_COMPACT_MIN_TOKENS_ENV),
            _sm_compact_config.min_tokens,
        ),
        min_text_block_messages=_parse_positive_int(
            os.environ.get(OPENSPACE_SM_COMPACT_MIN_TEXT_MESSAGES_ENV),
            _sm_compact_config.min_text_block_messages,
        ),
        max_tokens=_parse_positive_int(
            os.environ.get(OPENSPACE_SM_COMPACT_MAX_TOKENS_ENV),
            _sm_compact_config.max_tokens,
        ),
    )


def _parse_positive_int(raw: str | None, default: int) -> int:
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def should_use_session_memory_compaction() -> bool:
    """Return whether session-memory compaction is enabled."""

    if _env_truthy(os.environ.get(OPENSPACE_DISABLE_SESSION_MEMORY_COMPACT_ENV)):
        return False
    enabled = os.environ.get(OPENSPACE_SESSION_MEMORY_COMPACT_ENABLED_ENV)
    if _env_defined_falsy(enabled):
        return False
    if _env_truthy(enabled):
        return True
    return True


def has_text_blocks(message: Mapping[str, Any]) -> bool:
    role = message.get("role")
    content = message.get("content")
    if role == "assistant":
        if isinstance(content, str):
            return bool(content)
        if isinstance(content, Sequence) and not isinstance(
            content,
            (str, bytes, bytearray),
        ):
            return any(
                isinstance(block, Mapping)
                and block.get("type") == "text"
                and bool(block.get("text"))
                for block in content
            )
    if role == "user":
        if isinstance(content, str):
            return bool(content)
        if isinstance(content, Sequence) and not isinstance(
            content,
            (str, bytes, bytearray),
        ):
            return any(
                isinstance(block, str)
                or (
                    isinstance(block, Mapping)
                    and block.get("type") == "text"
                    and bool(block.get("text"))
                )
                for block in content
            )
    return False


def _get_tool_result_ids(message: Mapping[str, Any]) -> list[str]:
    if message.get("role") == "tool":
        tool_call_id = message.get("tool_call_id")
        return [str(tool_call_id)] if tool_call_id else []
    if message.get("role") != "user":
        return []
    content = message.get("content")
    if not isinstance(content, Sequence) or isinstance(
        content,
        (str, bytes, bytearray),
    ):
        return []
    ids: list[str] = []
    for block in content:
        if isinstance(block, Mapping) and block.get("type") == "tool_result":
            tool_use_id = block.get("tool_use_id")
            if tool_use_id:
                ids.append(str(tool_use_id))
    return ids


def _get_tool_use_ids(message: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, Sequence) and not isinstance(
        tool_calls,
        (str, bytes, bytearray),
    ):
        for call in tool_calls:
            if isinstance(call, Mapping):
                tool_id = call.get("id")
                if tool_id:
                    ids.append(str(tool_id))
    content = message.get("content")
    if isinstance(content, Sequence) and not isinstance(
        content,
        (str, bytes, bytearray),
    ):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                block_id = block.get("id")
                if block_id:
                    ids.append(str(block_id))
    return ids


def _has_tool_use_with_ids(
    message: Mapping[str, Any],
    tool_use_ids: set[str],
) -> bool:
    return any(tool_id in tool_use_ids for tool_id in _get_tool_use_ids(message))


def _assistant_message_identity(message: Mapping[str, Any]) -> str | None:
    if message.get("role") != "assistant":
        return None
    value = message.get("id")
    if isinstance(value, str) and value:
        return value
    meta = message.get("_meta")
    if isinstance(meta, Mapping):
        for key in ("response_id", "id", "message_id"):
            value = meta.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def adjust_index_to_preserve_api_invariants(
    messages: Sequence[Mapping[str, Any]],
    start_index: int,
) -> int:
    """Avoid slicing through tool_use/tool_result pairs or split thinking."""

    if start_index <= 0 or start_index >= len(messages):
        return start_index

    adjusted_index = start_index

    all_tool_result_ids: list[str] = []
    for message in messages[start_index:]:
        all_tool_result_ids.extend(_get_tool_result_ids(message))

    if all_tool_result_ids:
        tool_use_ids_in_kept = set[str]()
        for message in messages[adjusted_index:]:
            tool_use_ids_in_kept.update(_get_tool_use_ids(message))

        needed = {
            tool_id
            for tool_id in all_tool_result_ids
            if tool_id not in tool_use_ids_in_kept
        }
        for i in range(adjusted_index - 1, -1, -1):
            if not needed:
                break
            message = messages[i]
            if _has_tool_use_with_ids(message, needed):
                adjusted_index = i
                needed.difference_update(_get_tool_use_ids(message))

    identities = {
        identity
        for identity in (
            _assistant_message_identity(message)
            for message in messages[adjusted_index:]
        )
        if identity
    }
    for i in range(adjusted_index - 1, -1, -1):
        identity = _assistant_message_identity(messages[i])
        if identity and identity in identities:
            adjusted_index = i

    return adjusted_index


def calculate_messages_to_keep_index(
    messages: Sequence[Mapping[str, Any]],
    last_summarized_index: int,
) -> int:
    if not messages:
        return 0

    from openspace.services.conversation.compact import estimate_message_tokens

    config = _config_from_env()
    start_index = (
        last_summarized_index + 1 if last_summarized_index >= 0 else len(messages)
    )
    total_tokens = 0
    text_block_count = 0
    for message in messages[start_index:]:
        total_tokens += estimate_message_tokens([message])
        if has_text_blocks(message):
            text_block_count += 1

    if total_tokens >= config.max_tokens:
        return adjust_index_to_preserve_api_invariants(messages, start_index)

    if (
        total_tokens >= config.min_tokens
        and text_block_count >= config.min_text_block_messages
    ):
        return adjust_index_to_preserve_api_invariants(messages, start_index)

    boundary_index = -1
    for index in range(len(messages) - 1, -1, -1):
        if is_compact_boundary_message(messages[index]):
            boundary_index = index
            break
    floor = boundary_index + 1 if boundary_index >= 0 else 0

    for i in range(start_index - 1, floor - 1, -1):
        message = messages[i]
        total_tokens += estimate_message_tokens([message])
        if has_text_blocks(message):
            text_block_count += 1
        start_index = i

        if total_tokens >= config.max_tokens:
            break
        if (
            total_tokens >= config.min_tokens
            and text_block_count >= config.min_text_block_messages
        ):
            break

    return adjust_index_to_preserve_api_invariants(messages, start_index)


def reset_last_summarized_after_compaction(context: "ToolUseContext | None") -> None:
    if context is not None:
        set_last_summarized_message_id(context, None)


async def try_session_memory_compaction(
    messages: list[dict[str, Any]],
    context: "ToolUseContext | None" = None,
    *,
    auto_compact_threshold: int | None = None,
    hook_registry: "HookRegistry | None" = None,
    model: str | None = None,
) -> "CompactionResult | None":
    if not should_use_session_memory_compaction():
        return None

    await wait_for_session_memory_extraction(context)

    session_id = getattr(context, "session_id", None) if context is not None else None
    cwd = getattr(context, "cwd", None) if context is not None else None
    session_dir = getattr(context, "session_dir", None) if context is not None else None
    if session_dir:
        memory_path = Path(session_dir).expanduser().resolve() / "session_memory.md"
        try:
            session_memory = memory_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            session_memory = None
        except OSError:
            logger.debug("Failed to read session memory for compact", exc_info=True)
            return None
    else:
        session_memory = read_session_memory(session_id, cwd=cwd)
        memory_path = get_session_memory_path(session_id, cwd=cwd)

    if not session_memory:
        await _emit_sm_compact_event(
            context,
            "session_memory_compact_skipped",
            {"reason": "no_session_memory"},
        )
        return None

    if is_session_memory_empty(session_memory):
        await _emit_sm_compact_event(
            context,
            "session_memory_compact_skipped",
            {"reason": "empty_template"},
        )
        return None

    last_summarized = (
        get_last_summarized_message_id(context) if context is not None else None
    )
    if last_summarized is not None:
        last_summarized_index = find_message_cursor_index(messages, last_summarized)
        if last_summarized_index == -1:
            await _emit_sm_compact_event(
                context,
                "session_memory_compact_skipped",
                {"reason": "summarized_id_not_found"},
            )
            return None
    else:
        last_summarized_index = len(messages) - 1
        await _emit_sm_compact_event(
            context,
            "session_memory_compact_resumed_session",
            {},
        )

    try:
        start_index = calculate_messages_to_keep_index(
            messages,
            last_summarized_index,
        )
        messages_to_keep = [
            dict(message)
            for message in messages[start_index:]
            if not is_compact_boundary_message(message)
        ]
        result = await _create_compaction_result_from_session_memory(
            messages=messages,
            session_memory=session_memory,
            messages_to_keep=messages_to_keep,
            memory_path=memory_path,
            context=context,
            hook_registry=hook_registry,
            model=model,
        )

        from openspace.services.conversation.compact import (
            build_post_compact_messages,
            estimate_message_tokens,
        )

        post_messages = build_post_compact_messages(result)
        post_token_count = estimate_message_tokens(post_messages, model=model)

        if (
            auto_compact_threshold is not None
            and post_token_count >= auto_compact_threshold
        ):
            await _emit_sm_compact_event(
                context,
                "session_memory_compact_skipped",
                {
                    "reason": "threshold_exceeded",
                    "post_compact_token_count": post_token_count,
                    "auto_compact_threshold": auto_compact_threshold,
                },
            )
            return None

        segment_data = await _write_session_memory_compact_segment(
            context,
            messages[:start_index],
        )
        compact_was_truncated = bool(result.compact_was_truncated)
        await _record_session_memory_compact_summary(
            context,
            result,
            segment_data=segment_data,
            memory_path=memory_path,
            was_truncated=compact_was_truncated,
        )

        result.post_compact_token_count = post_token_count
        result.true_post_compact_token_count = post_token_count
        result.compact_source = "session_memory_compact"
        result.compact_memory_path = str(memory_path)
        result.compact_was_truncated = compact_was_truncated
        await _emit_sm_compact_event(
            context,
            "session_memory_compact",
            {
                "memory_path": str(memory_path),
                "messages_kept": len(messages_to_keep),
                "start_index": start_index,
                "post_compact_token_count": post_token_count,
            },
        )
        reset_last_summarized_after_compaction(context)
        return result
    except Exception as exc:
        logger.debug("Session memory compaction failed: %s", exc, exc_info=True)
        await _emit_sm_compact_event(
            context,
            "session_memory_compact_skipped",
            {"reason": "error", "error": str(exc)},
        )
        return None


async def _create_compaction_result_from_session_memory(
    *,
    messages: list[dict[str, Any]],
    session_memory: str,
    messages_to_keep: list[dict[str, Any]],
    memory_path: Path,
    context: "ToolUseContext | None",
    hook_registry: "HookRegistry | None",
    model: str | None,
) -> "CompactionResult":
    from openspace.services.conversation.compact import (
        CompactionResult,
        estimate_message_tokens,
        get_compact_user_summary_message,
        token_count_from_last_api_response,
    )
    from openspace.services.conversation.attachments import create_post_compact_attachments
    from openspace.services.tooling.hooks import run_post_compact_hooks

    pre_compact_token_count = token_count_from_last_api_response(messages)
    pre_compact_discovered = extract_discovered_tool_names(messages)
    if context is not None:
        pre_compact_discovered.update(
            getattr(context, "discovered_tool_names", set()) or set()
        )

    boundary = build_compact_boundary_message(
        "auto",
        pre_compact_token_count,
        pre_compact_discovered_tools=sorted(pre_compact_discovered),
    )

    truncated, was_truncated = truncate_session_memory_for_compact(session_memory)
    transcript_path = _transcript_path_for_context(context)
    summary_content = get_compact_user_summary_message(
        truncated,
        True,
        str(transcript_path) if transcript_path is not None else None,
        True,
    )
    if was_truncated:
        summary_content += (
            "\n\nSome session memory sections were truncated for length. "
            f"The full session memory can be viewed at: {memory_path}"
        )

    summary_msg = build_compact_summary_message(
        summary_content,
        visible_in_transcript_only=True,
    )

    attachments: list[dict[str, Any]] = []
    if context is not None:
        attachments = await create_post_compact_attachments(
            context,
            effective_model=model,
            messages_to_keep=messages_to_keep,
            full_compact=False,
        )

    post_hook = await run_post_compact_hooks(
        hook_registry,
        {"trigger": "auto", "compact_summary": truncated, "source": "session_memory"},
        context,
    )
    post_tokens = estimate_message_tokens([summary_msg, *messages_to_keep, *attachments], model=model)
    return CompactionResult(
        boundary_marker=boundary,
        summary_messages=[summary_msg],
        attachments=attachments,
        hook_results=[],
        messages_to_keep=messages_to_keep if messages_to_keep else None,
        user_display_message=post_hook.user_display_message,
        pre_compact_token_count=pre_compact_token_count,
        post_compact_token_count=post_tokens,
        true_post_compact_token_count=post_tokens,
        compact_source="session_memory_compact",
        compact_memory_path=str(memory_path),
        compact_was_truncated=bool(was_truncated),
    )


def _transcript_path_for_context(context: "ToolUseContext | None") -> Path | None:
    if context is None:
        return None
    session_dir = getattr(context, "session_dir", None)
    if session_dir:
        candidate = Path(session_dir).expanduser().resolve()
        return candidate.with_suffix(".messages") if candidate.suffix else candidate.parent / f"{candidate.name}.messages"
    session_id = getattr(context, "session_id", None)
    if not session_id:
        return None
    from openspace.services.memory.session_memory import get_session_transcript_path

    return get_session_transcript_path(session_id, cwd=getattr(context, "cwd", None))


async def _write_session_memory_compact_segment(
    context: "ToolUseContext | None",
    messages: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if context is None or not messages:
        return None
    storage = getattr(context, "session_storage", None)
    write_segment = getattr(storage, "write_session_transcript_segment", None)
    if write_segment is None:
        return None
    try:
        result = write_segment(
            messages,
            reason="session_memory_compact",
            task_id=getattr(context, "task_id", None),
            parent_task_id=getattr(context, "parent_task_id", None),
            agent_id=getattr(context, "agent_id", None),
        )
        if hasattr(result, "__await__"):
            result = await result
        return dict(result) if isinstance(result, Mapping) else None
    except Exception:
        logger.debug("Failed to write session-memory compact segment", exc_info=True)
        return None


async def _record_session_memory_compact_summary(
    context: "ToolUseContext | None",
    result: "CompactionResult",
    *,
    segment_data: Mapping[str, Any] | None,
    memory_path: Path,
    was_truncated: bool,
) -> None:
    if context is None:
        return
    storage = getattr(context, "session_storage", None)
    record = getattr(storage, "record_compact_summary", None)
    if record is None:
        return
    summary_message = result.summary_messages[0] if result.summary_messages else {}
    segment_ref_id = None
    if isinstance(segment_data, Mapping):
        segment_id = segment_data.get("segment_id")
        if segment_id:
            segment_ref_id = f"transcript_segment:{storage.session_id}:{segment_id}"
    try:
        record_result = record(
            summary_message_uuid=get_message_uuid(summary_message),
            compact_source="session_memory_compact",
            segment_ref_id=segment_ref_id,
            memory_path=str(memory_path),
            was_truncated=was_truncated,
            task_id=getattr(context, "task_id", None),
            parent_task_id=getattr(context, "parent_task_id", None),
            agent_id=getattr(context, "agent_id", None),
        )
        if hasattr(record_result, "__await__"):
            await record_result
    except Exception:
        logger.debug("Failed to write session-memory compact summary ref", exc_info=True)


async def _emit_sm_compact_event(
    context: "ToolUseContext | None",
    event_type: str,
    data: dict[str, Any],
) -> None:
    if context is None:
        return
    try:
        await context.emit_event(event_type, data)
    except Exception:
        pass
