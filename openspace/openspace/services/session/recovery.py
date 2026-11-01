"""Conversation recovery helpers for resume and transient API failures.

Implementation notes:
- ``utils/conversationRecovery.ts`` (597 lines)
- ``query.ts`` API-error recovery branches
- ``services/api/withRetry.ts`` retry exhaustion and ``CannotRetryError``

OpenSpace does not expose the checklist's historical
``recoverConversation`` / ``fixIncompleteToolResults`` /
``shouldRetryLastTurn`` names as separate runtime owners.  The real recovery
surface is resume deserialization with turn-interruption detection.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from openspace.llm.errors import (
    CannotRetryError,
    ModelNotAvailableError,
    PromptTooLongError,
    classify_api_error,
    is_abort_error,
)
from openspace.services.conversation.messages import (
    NO_RESPONSE_REQUESTED,
    SYNTHETIC_MODEL,
    ensure_tool_result_pairing,
    filter_whitespace_only_assistant_messages,
)

MISSING_TOOL_RESULT_CONTENT = "[Tool result missing due to interrupted execution]"
RESUME_CONTINUATION_CONTENT = "Continue from where you left off."
TERMINAL_COMMUNICATION_TOOL_NAMES = frozenset(
    {
        # OpenSpace BriefTool/prompt.ts
        "SendUserMessage",
        "Brief",
        # OpenSpace KAIROS-only SendUserFileTool.  OS does not currently expose this
        # tool, but recognizing the name preserves OpenSpace's completed-turn branch
        # if old or imported transcripts contain it.
        "SendUserFile",
        "send_user_file",
    }
)
TRANSIENT_RETRY_TAGS = frozenset(
    {
        "api_timeout",
        "connection_error",
        "rate_limit",
        "server_error",
        "server_overload",
        "repeated_overloaded",
    }
)


@dataclass(slots=True)
class ConversationRecoveryResult:
    messages: list[dict[str, Any]]
    should_retry: bool = False
    reason: str = ""
    inserted_synthetic_results: int = 0
    dropped_messages: int = 0
    turn_interruption_state: dict[str, Any] = field(
        default_factory=lambda: {"kind": "none"}
    )
    inserted_sentinel: bool = False
    inserted_continuation: bool = False


@dataclass(slots=True)
class _PairingFixResult:
    messages: list[dict[str, Any]]
    inserted_synthetic_results: int = 0
    dropped_messages: int = 0


def fix_incomplete_tool_results(
    messages: Sequence[Mapping[str, Any]],
    *,
    mode: Literal["synthesize", "drop"] = "synthesize",
) -> list[dict[str, Any]]:
    """Return API-valid messages by repairing incomplete tool result groups.

    ``mode="synthesize"`` mirrors the resume-safe path: historical tool calls
    receive synthetic error results so the transcript remains valid.
    ``mode="drop"`` is used before retrying a transient API failure: an
    incomplete assistant/tool group is removed so the model can regenerate it.
    """

    return _fix_incomplete_tool_results(messages, mode=mode).messages


def should_retry_last_turn(
    error: BaseException,
    messages: Sequence[Mapping[str, Any]],
) -> bool:
    """Return whether an API failure should get one loop-level retry."""

    last_physical = _last_non_progress_message(messages)
    if last_physical is not None and _is_assistant_api_error_message(last_physical):
        return False

    unwrapped = _unwrap_retry_error(error)
    if isinstance(unwrapped, PromptTooLongError):
        return False
    if isinstance(unwrapped, ModelNotAvailableError):
        return False
    if isinstance(unwrapped, (PermissionError, ValueError, TypeError)):
        if not _looks_like_tool_pairing_error(unwrapped):
            return False
    if isinstance(unwrapped, BaseException) and is_abort_error(unwrapped):
        return False
    if isinstance(unwrapped, (TimeoutError, asyncio.TimeoutError)):
        return True
    if _looks_like_tool_pairing_error(unwrapped):
        return True

    if isinstance(unwrapped, Exception):
        tag = classify_api_error(unwrapped)
        if tag in TRANSIENT_RETRY_TAGS:
            return True
        if tag in {"prompt_too_long", "invalid_model", "auth_error", "client_error"}:
            return False

    return False


def recover_conversation(
    messages: Sequence[Mapping[str, Any]],
    error: BaseException | None = None,
) -> ConversationRecoveryResult:
    """Recover a conversation after interruption or API failure.

    For transient API failures, the final incomplete assistant/tool group is
    dropped and the caller may retry once.  For resume or terminal failures,
    missing tool results are synthesized so the persisted transcript remains
    valid for future API calls.
    """

    cleaned = _strip_runtime_only_progress(messages)
    retry = should_retry_last_turn(error, cleaned) if error is not None else False

    if retry:
        cleaned = _remove_incomplete_final_assistant(cleaned)
        fixed = _fix_incomplete_tool_results(cleaned, mode="drop")
        return ConversationRecoveryResult(
            messages=fixed.messages,
            should_retry=True,
            reason="retry_last_turn",
            inserted_synthetic_results=fixed.inserted_synthetic_results,
            dropped_messages=fixed.dropped_messages,
        )

    fixed = _fix_incomplete_tool_results(cleaned, mode="synthesize")
    resumed = filter_whitespace_only_assistant_messages(fixed.messages)
    return ConversationRecoveryResult(
        messages=resumed,
        should_retry=False,
        reason="resume_safe",
        inserted_synthetic_results=fixed.inserted_synthetic_results,
        dropped_messages=fixed.dropped_messages,
    )


def deserialize_for_resume(
    messages: Sequence[Mapping[str, Any]],
) -> ConversationRecoveryResult:
    """Deserialize persisted messages and detect OpenSpace turn interruption."""

    fixed = _fix_incomplete_tool_results(messages, mode="synthesize")
    cleaned = filter_whitespace_only_assistant_messages(fixed.messages)

    turn_state: dict[str, Any] = {"kind": "none"}
    inserted_sentinel = False
    inserted_continuation = False

    last_idx = _last_turn_relevant_index(cleaned)
    if last_idx >= 0:
        last = cleaned[last_idx]
        role = str(last.get("role") or "")
        if _is_attachment_message(last):
            cleaned.append(_resume_continuation_message())
            inserted_continuation = True
            turn_state = {
                "kind": "interrupted_prompt",
                "message_index": len(cleaned) - 1,
            }
            cleaned.append(_synthetic_assistant_sentinel())
            inserted_sentinel = True
        elif role == "user" and not _is_meta_user_message(last):
            turn_state = {
                "kind": "interrupted_prompt",
                "message_index": last_idx,
            }
            cleaned.insert(last_idx + 1, _synthetic_assistant_sentinel())
            inserted_sentinel = True
        elif role == "tool" and not _is_terminal_tool_result(last, cleaned, last_idx):
            cleaned.append(_resume_continuation_message())
            inserted_continuation = True
            turn_state = {
                "kind": "interrupted_prompt",
                "message_index": len(cleaned) - 1,
            }

    return ConversationRecoveryResult(
        messages=cleaned,
        should_retry=False,
        reason="deserialize_for_resume",
        inserted_synthetic_results=fixed.inserted_synthetic_results,
        dropped_messages=fixed.dropped_messages,
        turn_interruption_state=turn_state,
        inserted_sentinel=inserted_sentinel,
        inserted_continuation=inserted_continuation,
    )


def _fix_incomplete_tool_results(
    messages: Sequence[Mapping[str, Any]],
    *,
    mode: Literal["synthesize", "drop"],
) -> _PairingFixResult:
    if mode not in {"synthesize", "drop"}:
        raise ValueError("mode must be 'synthesize' or 'drop'")

    source = [
        copy.deepcopy(dict(message))
        for message in messages
        if isinstance(message, Mapping)
    ]
    result: list[dict[str, Any]] = []
    seen_tool_call_ids: set[str] = set()
    seen_tool_result_ids: set[str] = set()
    inserted = 0
    dropped = 0

    i = 0
    while i < len(source):
        message = source[i]
        role = str(message.get("role") or "")

        if role == "tool":
            tool_call_id = _tool_result_id(message)
            if (
                not tool_call_id
                or tool_call_id not in seen_tool_call_ids
                or tool_call_id in seen_tool_result_ids
            ):
                dropped += 1
                i += 1
                continue
            seen_tool_result_ids.add(tool_call_id)
            result.append(message)
            i += 1
            continue

        if role != "assistant":
            result.append(message)
            i += 1
            continue

        tool_calls = _assistant_tool_calls(message)
        if not tool_calls:
            result.append(message)
            i += 1
            continue

        deduped_calls: list[Any] = []
        call_ids: list[str] = []
        call_names: dict[str, str] = {}
        local_seen: set[str] = set()
        for tool_call in tool_calls:
            call_id = _tool_call_id(tool_call)
            if not call_id:
                deduped_calls.append(tool_call)
                continue
            if call_id in seen_tool_call_ids or call_id in local_seen:
                dropped += 1
                continue
            local_seen.add(call_id)
            call_ids.append(call_id)
            call_name = _tool_call_name(tool_call)
            if call_name:
                call_names[call_id] = call_name
            deduped_calls.append(tool_call)

        assistant = copy.deepcopy(message)
        if len(deduped_calls) != len(tool_calls):
            if deduped_calls:
                assistant["tool_calls"] = deduped_calls
            else:
                assistant.pop("tool_calls", None)

        j = i + 1
        group_results: list[dict[str, Any]] = []
        group_result_ids: set[str] = set()
        while j < len(source) and source[j].get("role") == "tool":
            tool_message = source[j]
            tool_call_id = _tool_result_id(tool_message)
            if (
                not tool_call_id
                or tool_call_id not in local_seen
                or tool_call_id in group_result_ids
            ):
                dropped += 1
                j += 1
                continue
            group_result_ids.add(tool_call_id)
            group_results.append(copy.deepcopy(tool_message))
            j += 1

        missing_ids = [call_id for call_id in call_ids if call_id not in group_result_ids]
        if missing_ids and mode == "drop":
            dropped += 1 + len(group_results)
            i = j
            continue

        if deduped_calls:
            result.append(assistant)
            seen_tool_call_ids.update(call_ids)
        else:
            assistant.pop("tool_calls", None)
            result.append(assistant)

        for tool_message in group_results:
            tool_call_id = _tool_result_id(tool_message)
            if tool_call_id and tool_call_id not in seen_tool_result_ids:
                seen_tool_result_ids.add(tool_call_id)
                result.append(tool_message)
            else:
                dropped += 1

        if mode == "synthesize":
            for missing_id in missing_ids:
                inserted += 1
                seen_tool_result_ids.add(missing_id)
                result.append(
                    _synthetic_tool_result(
                        missing_id,
                        call_names.get(missing_id) or "unknown",
                    )
                )

        i = j

    paired = ensure_tool_result_pairing(result)
    return _PairingFixResult(
        messages=paired,
        inserted_synthetic_results=inserted,
        dropped_messages=dropped,
    )


def _strip_runtime_only_progress(
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        meta = message.get("_meta")
        meta_type = meta.get("type") if isinstance(meta, Mapping) else None
        if meta_type in {"progress", "system_api_error"}:
            continue
        cleaned.append(copy.deepcopy(dict(message)))
    return cleaned


def _remove_incomplete_final_assistant(
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    cleaned = [
        copy.deepcopy(dict(message))
        for message in messages
        if isinstance(message, Mapping)
    ]
    idx = _last_turn_relevant_index(cleaned)
    if idx < 0:
        return cleaned
    last = cleaned[idx]
    if last.get("role") != "assistant":
        return cleaned
    if _is_assistant_api_error_message(last):
        return cleaned
    meta = last.get("_meta")
    incomplete = isinstance(meta, Mapping) and bool(
        meta.get("incomplete")
        or meta.get("partial")
        or meta.get("streaming_partial")
        or meta.get("interrupted")
    )
    content = last.get("content")
    empty_content = (
        content is None
        or (isinstance(content, str) and not content.strip())
        or (isinstance(content, list) and len(content) == 0)
    )
    if incomplete or (empty_content and not last.get("tool_calls")):
        del cleaned[idx:]
    return cleaned


def _last_non_progress_message(
    messages: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "")
        meta = message.get("_meta")
        meta_type = meta.get("type") if isinstance(meta, Mapping) else None
        if role == "system" and meta_type in {"progress", "system_api_error"}:
            continue
        if meta_type in {"progress", "system_api_error"}:
            continue
        return dict(message)
    return None


def _last_turn_relevant_index(messages: Sequence[Mapping[str, Any]]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "")
        meta = message.get("_meta")
        meta_type = meta.get("type") if isinstance(meta, Mapping) else None
        if role == "system" and meta_type in {"progress", "system_api_error"}:
            continue
        if meta_type in {"progress", "system_api_error"}:
            continue
        if _is_assistant_api_error_message(message):
            continue
        return index
    return -1


def _is_assistant_api_error_message(message: Mapping[str, Any]) -> bool:
    if message.get("role") != "assistant":
        return False
    meta = message.get("_meta")
    return isinstance(meta, Mapping) and bool(meta.get("is_api_error_message"))


def _is_meta_user_message(message: Mapping[str, Any]) -> bool:
    meta = message.get("_meta")
    if isinstance(meta, Mapping):
        if meta.get("is_meta"):
            return True
        if meta.get("type") in {
            "compact_summary",
            "resume_continuation",
            "resume_no_response_sentinel",
        }:
            return True
    return str(message.get("content") or "") == NO_RESPONSE_REQUESTED


def _is_attachment_message(message: Mapping[str, Any]) -> bool:
    if message.get("role") == "attachment":
        return True
    meta = message.get("_meta")
    return isinstance(meta, Mapping) and meta.get("type") == "attachment"


def _is_terminal_tool_result(
    message: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    result_idx: int,
) -> bool:
    if message.get("role") != "tool":
        return False
    tool_call_id = _tool_result_id(message)
    if not tool_call_id:
        return False
    for index in range(result_idx - 1, -1, -1):
        candidate = messages[index]
        if not isinstance(candidate, Mapping) or candidate.get("role") != "assistant":
            continue
        for tool_call in _assistant_tool_calls(candidate):
            if _tool_call_id(tool_call) == tool_call_id:
                return (_tool_call_name(tool_call) or "") in TERMINAL_COMMUNICATION_TOOL_NAMES
    return False


def _synthetic_tool_result(tool_call_id: str, tool_name: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": tool_name,
        "content": MISSING_TOOL_RESULT_CONTENT,
        "_meta": {
            "type": "tool_result",
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "status": "error",
            "synthetic": True,
            "is_synthetic": True,
            "timestamp": time.time(),
        },
    }


def _synthetic_assistant_sentinel() -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": NO_RESPONSE_REQUESTED,
        "_meta": {
            "model": SYNTHETIC_MODEL,
            "type": "resume_no_response_sentinel",
            "timestamp": time.time(),
        },
    }


def _resume_continuation_message() -> dict[str, Any]:
    return {
        "role": "user",
        "content": RESUME_CONTINUATION_CONTENT,
        "_meta": {
            "type": "resume_continuation",
            "is_meta": True,
            "timestamp": time.time(),
        },
    }


def _assistant_tool_calls(message: Mapping[str, Any]) -> list[Any]:
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, (list, tuple)):
        return list(tool_calls)
    return []


def _tool_call_id(tool_call: Any) -> str | None:
    if isinstance(tool_call, Mapping):
        value = tool_call.get("id")
        return str(value) if value else None
    value = getattr(tool_call, "id", None)
    return str(value) if value else None


def _tool_call_name(tool_call: Any) -> str | None:
    if isinstance(tool_call, Mapping):
        function = tool_call.get("function")
        if isinstance(function, Mapping):
            name = function.get("name")
            return str(name) if name else None
        name = tool_call.get("name") or tool_call.get("tool_name")
        return str(name) if name else None
    function = getattr(tool_call, "function", None)
    if function is not None:
        name = getattr(function, "name", None)
        if name:
            return str(name)
    name = getattr(tool_call, "name", None) or getattr(tool_call, "tool_name", None)
    return str(name) if name else None


def _tool_result_id(message: Mapping[str, Any]) -> str | None:
    value = message.get("tool_call_id")
    return str(value) if value else None


def _unwrap_retry_error(error: BaseException) -> BaseException:
    if isinstance(error, CannotRetryError) and error.original_error is not None:
        return error.original_error
    original = getattr(error, "original_error", None)
    if isinstance(original, BaseException):
        return original
    return error


def _looks_like_tool_pairing_error(error: BaseException) -> bool:
    text = str(error).lower()
    return (
        ("tool_call_id" in text or "tool call" in text)
        and ("tool" in text)
        and (
            "result" in text
            or "response" in text
            or "messages with role" in text
            or "must be followed" in text
        )
    )


__all__ = [
    "ConversationRecoveryResult",
    "MISSING_TOOL_RESULT_CONTENT",
    "RESUME_CONTINUATION_CONTENT",
    "deserialize_for_resume",
    "fix_incomplete_tool_results",
    "recover_conversation",
    "should_retry_last_turn",
]
