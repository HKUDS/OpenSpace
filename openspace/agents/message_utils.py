from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set

from openspace.prompts import GroundingAgentPrompts
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

SUPPORTED_EXTERNAL_HISTORY_ROLES: Set[str] = {"user", "assistant"}
MAX_SINGLE_CONTENT_CHARS: int = 30_000
ITERATION_GUIDANCE_PREFIX: str = "[INTERNAL ORCHESTRATION NOTE]"


def cap_message_content(
    messages: List[Dict[str, Any]],
    max_chars: int = MAX_SINGLE_CONTENT_CHARS,
) -> List[Dict[str, Any]]:
    """Truncate oversized individual message contents in-place.

    Targets tool-result messages and assistant messages that can
    carry enormous file contents (read_file on large CSVs/scripts).
    System messages and the first user instruction are never touched.
    """
    trimmed = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= max_chars:
            continue
        if msg.get("role") == "system":
            continue
        original_len = len(content)
        msg["content"] = (
            content[: max_chars // 2]
            + f"\n\n... [truncated {original_len - max_chars:,} chars] ...\n\n"
            + content[-(max_chars // 2) :]
        )
        trimmed += 1
    if trimmed:
        logger.info(f"Capped {trimmed} oversized message(s) to {max_chars:,} chars each")
    return messages


def truncate_messages(
    messages: List[Dict[str, Any]],
    keep_recent: int = 8,
    max_tokens_estimate: int = 120_000,
    guidance_prefix: str = ITERATION_GUIDANCE_PREFIX,
) -> List[Dict[str, Any]]:
    """Truncate conversation history to fit within token budget.

    Preserves system messages and the first user instruction while
    keeping only the most recent conversation turns.
    """
    messages = cap_message_content(messages)

    if len(messages) <= keep_recent + 2:  # +2 for system and initial user
        return messages

    total_text = json.dumps(messages, ensure_ascii=False)
    estimated_tokens = len(total_text) // 4

    if estimated_tokens < max_tokens_estimate:
        return messages

    logger.info(
        f"Truncating message history: {len(messages)} messages, "
        f"~{estimated_tokens:,} tokens -> keeping recent {keep_recent} rounds"
    )

    system_messages: List[Dict[str, Any]] = []
    user_instruction: Optional[Dict[str, Any]] = None
    conversation_messages: List[Dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            system_messages.append(msg)
        elif role == "user" and user_instruction is None:
            user_instruction = msg
        else:
            conversation_messages.append(msg)

    recent_messages = (
        conversation_messages[-(keep_recent * 2) :] if conversation_messages else []
    )

    truncated = system_messages.copy()
    dropped = len(conversation_messages) - len(recent_messages)
    if dropped > 0:
        truncated.append(
            {
                "role": "system",
                "content": (
                    f"{guidance_prefix} {dropped} earlier messages were "
                    "truncated to save context. The original task instruction "
                    "is preserved below."
                ),
            }
        )
    if user_instruction:
        truncated.append(user_instruction)
    truncated.extend(recent_messages)

    logger.info(
        f"After truncation: {len(truncated)} messages, "
        f"~{len(json.dumps(truncated, ensure_ascii=False)) // 4:,} tokens (estimated)"
    )

    return truncated


def normalize_external_history(
    conversation_history: Any,
    supported_roles: Set[str] = SUPPORTED_EXTERNAL_HISTORY_ROLES,
) -> List[Dict[str, str]]:
    """Normalize external conversation history into ``{role, content}`` dicts."""
    if not isinstance(conversation_history, list):
        return []

    normalized: List[Dict[str, str]] = []
    for entry in conversation_history:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role", "")).strip().lower()
        if role not in supported_roles:
            continue

        content = entry.get("content")
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
                elif isinstance(item, str) and item.strip():
                    parts.append(item.strip())
            content = "\n".join(parts).strip()
        elif content is not None:
            content = str(content).strip()

        if not content:
            continue

        normalized.append({"role": role, "content": content})

    return normalized


def build_channel_context_message(channel_context: Any) -> Optional[str]:
    """Build a system message describing the communication channel context."""
    if not isinstance(channel_context, dict):
        return None

    lines = [
        "## Channel Context",
    ]

    platform = str(channel_context.get("platform", "")).strip()
    chat_type = str(channel_context.get("chat_type", "")).strip()
    chat_id = str(channel_context.get("chat_id", "")).strip()
    chat_name = str(channel_context.get("chat_name", "")).strip()
    thread_id = str(channel_context.get("thread_id", "")).strip()
    user_name = str(channel_context.get("user_name", "")).strip()
    user_id = str(channel_context.get("user_id", "")).strip()
    session_key = str(channel_context.get("session_key", "")).strip()
    message_id = str(channel_context.get("message_id", "")).strip()
    reply_to_message_id = str(channel_context.get("reply_to_message_id", "")).strip()
    reply_to_text = str(channel_context.get("reply_to_text", "")).strip()

    if platform:
        lines.append(f"- Platform: {platform}")
    if chat_type:
        lines.append(f"- Chat type: {chat_type}")
    if chat_id:
        lines.append(f"- Chat ID: {chat_id}")
    if chat_name:
        lines.append(f"- Chat name: {chat_name}")
    if thread_id:
        lines.append(f"- Thread ID: {thread_id}")
    if user_name:
        lines.append(f"- User: {user_name}")
    elif user_id:
        lines.append(f"- User ID: {user_id}")
    if session_key:
        lines.append(f"- Session key: {session_key}")
    if message_id:
        lines.append(f"- Message ID: {message_id}")
    if reply_to_message_id:
        lines.append(f"- Reply-to message ID: {reply_to_message_id}")
    if reply_to_text:
        lines.append(f"- Reply context: {reply_to_text[:500]}")

    lines.extend(
        [
            "",
            "## Chat Reply Policy",
            "- If the user is making simple conversation, answer directly in natural language.",
            "- Do not call tools for greetings, acknowledgements, thanks, or brief "
            "clarifications that can be answered from the current context.",
            f"- When you reply directly without tools, include "
            f"`{GroundingAgentPrompts.TASK_COMPLETE}` at the end of your response.",
        ]
    )

    attachments = channel_context.get("attachments")
    if isinstance(attachments, list) and attachments:
        lines.append("- Attachments:")
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            path = str(attachment.get("path", "")).strip()
            if not path:
                continue
            kind = str(attachment.get("kind", "file")).strip() or "file"
            name = str(attachment.get("name", "")).strip()
            label = f"{kind}: {path}"
            if name:
                label += f" ({name})"
            lines.append(f"  - {label}")

    if len(lines) == 1:
        return None

    return "\n".join(lines)
