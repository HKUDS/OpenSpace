from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

SUPPORTED_EXTERNAL_HISTORY_ROLES: Set[str] = {"user", "assistant"}
SKILL_ATTACHMENT_TYPES: Set[str] = {
    "skill_listing",
    "skill_discovery",
    "dynamic_skill",
    "skill_state",
    "invoked_skills",
    "invoked_skill_content",
}


def normalize_external_history(
    conversation_history: Any,
    supported_roles: Set[str] = SUPPORTED_EXTERNAL_HISTORY_ROLES,
    *,
    preserve_skill_attachments: bool = True,
) -> List[Dict[str, Any]]:
    """Normalize external conversation history into ``{role, content}`` dicts."""
    if not isinstance(conversation_history, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for entry in conversation_history:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role", "")).strip().lower()
        if role not in supported_roles:
            continue
        if not preserve_skill_attachments and _entry_has_skill_attachment(entry):
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

        message: Dict[str, Any] = {"role": role, "content": content}
        preserved_meta = _preserve_external_history_meta(entry, role)
        if preserved_meta is not None:
            message["_meta"] = preserved_meta
        normalized.append(message)

    return normalized


def _entry_has_skill_attachment(entry: Dict[str, Any]) -> bool:
    meta = entry.get("_meta")
    if not isinstance(meta, dict):
        return False
    attachment = meta.get("attachment")
    if not isinstance(attachment, dict):
        return False
    return str(attachment.get("type") or "") in SKILL_ATTACHMENT_TYPES


def _preserve_external_history_meta(
    entry: Dict[str, Any],
    role: str,
) -> Dict[str, Any] | None:
    """Preserve only trusted OS attachment metadata across process() calls.

    External channel history can contain arbitrary dicts, so we do not carry
    through general ``_meta``.  The relevant-memory prefetch path depends on
    the attachment metadata from a previous OpenSpace turn to avoid
    re-surfacing the same memory file after ``result["messages"]`` is passed
    back as ``conversation_history``.
    """

    if role != "user":
        return None
    meta = entry.get("_meta")
    if not isinstance(meta, dict):
        return None
    if meta.get("type") != "attachment":
        return None
    attachment = meta.get("attachment")
    if not isinstance(attachment, dict):
        return None
    attachment_type = attachment.get("type")
    if attachment_type == "nested_memory":
        path = attachment.get("path")
        content = attachment.get("content")
        if not isinstance(path, str) or not isinstance(content, dict):
            return None
        nested_content = {
            key: value
            for key, value in content.items()
            if key
            in {
                "path",
                "content",
                "source",
                "type",
                "priority",
                "parent",
                "globs",
                "contentDiffersFromDisk",
            }
        }
        if not isinstance(nested_content.get("path"), str) or not isinstance(
            nested_content.get("content"), str
        ):
            return None
        safe_attachment: dict[str, Any] = {
            "type": "nested_memory",
            "path": path,
            "content": nested_content,
        }
        if isinstance(attachment.get("displayPath"), str):
            safe_attachment["displayPath"] = attachment["displayPath"]
        if isinstance(attachment.get("triggerFilePath"), str):
            safe_attachment["triggerFilePath"] = attachment["triggerFilePath"]
        return {
            "type": "attachment",
            "attachment_type": "nested_memory",
            "attachment": safe_attachment,
        }

    if attachment_type in {
        "skill_listing",
        "skill_discovery",
        "dynamic_skill",
        "skill_state",
        "invoked_skills",
        "invoked_skill_content",
    }:
        safe_attachment = _sanitize_skill_attachment(attachment)
        if safe_attachment is None:
            return None
        return {
            "type": "attachment",
            "attachment_type": attachment_type,
            "attachment": safe_attachment,
        }

    if attachment_type != "relevant_memories":
        return None
    memories = attachment.get("memories")
    if not isinstance(memories, list):
        return None
    safe_memories: list[dict[str, Any]] = []
    for memory in memories:
        if not isinstance(memory, dict):
            continue
        path = memory.get("path")
        content = memory.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        safe_memory: dict[str, Any] = {
            "path": path,
            "content": content,
            "mtimeMs": memory.get("mtimeMs"),
        }
        if isinstance(memory.get("header"), str):
            safe_memory["header"] = memory["header"]
        if memory.get("limit") is not None:
            safe_memory["limit"] = memory.get("limit")
        safe_memories.append(safe_memory)
    if not safe_memories:
        return None
    return {
        "type": "attachment",
        "attachment_type": "relevant_memories",
        "attachment": {
            "type": "relevant_memories",
            "memories": safe_memories,
        },
    }


def _sanitize_skill_attachment(attachment: Dict[str, Any]) -> Dict[str, Any] | None:
    attachment_type = attachment.get("type")
    if not isinstance(attachment_type, str):
        return None

    if attachment_type == "skill_listing":
        return {
            "type": "skill_listing",
            "content": str(attachment.get("content") or ""),
            "skillCount": int(attachment.get("skillCount") or 0),
            "isInitial": bool(attachment.get("isInitial")),
            "skillNames": [
                str(name)
                for name in attachment.get("skillNames", [])
                if str(name).strip()
            ],
        }

    if attachment_type == "skill_discovery":
        skills: list[dict[str, Any]] = []
        for item in attachment.get("skills") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            skills.append({
                "name": name,
                "description": str(item.get("description") or ""),
                "skill_id": str(item.get("skill_id") or item.get("skillId") or ""),
                "source": str(item.get("source") or ""),
            })
        return {
            "type": "skill_discovery",
            "skills": skills,
            "signal": dict(attachment.get("signal") or {}),
            "source": str(attachment.get("source") or "openspace"),
        }

    if attachment_type == "dynamic_skill":
        return {
            "type": "dynamic_skill",
            "skillDir": str(attachment.get("skillDir") or ""),
            "displayPath": str(attachment.get("displayPath") or ""),
            "skillNames": [
                str(name)
                for name in attachment.get("skillNames", [])
                if str(name).strip()
            ],
        }

    if attachment_type == "skill_state":
        sent_by_agent: dict[str, list[str]] = {}
        raw_sent = attachment.get("sentSkillNamesByAgent")
        if isinstance(raw_sent, dict):
            for agent, names in raw_sent.items():
                if not isinstance(names, list):
                    continue
                safe_names = [str(name) for name in names if str(name).strip()]
                if safe_names:
                    sent_by_agent[str(agent)] = safe_names
        return {
            "type": "skill_state",
            "sentSkillNamesByAgent": sent_by_agent,
            "discoveredSkillNames": [
                str(name)
                for name in attachment.get("discoveredSkillNames", [])
                if str(name).strip()
            ],
            "sentDynamicSkillKeys": [
                str(key)
                for key in attachment.get("sentDynamicSkillKeys", [])
                if str(key).strip()
            ],
            "pathActivatedSkillNames": [
                str(name)
                for name in attachment.get("pathActivatedSkillNames", [])
                if str(name).strip()
            ],
        }

    if attachment_type == "invoked_skills":
        skills: list[dict[str, Any]] = []
        for item in attachment.get("skills") or []:
            if not isinstance(item, dict):
                continue
            safe = _sanitize_invoked_skill_item(item)
            if safe is not None:
                skills.append(safe)
        return {"type": "invoked_skills", "skills": skills}

    if attachment_type == "invoked_skill_content":
        safe = _sanitize_invoked_skill_item(attachment)
        if safe is None:
            return None
        return {"type": "invoked_skill_content", **safe}

    return None


def _sanitize_invoked_skill_item(item: Dict[str, Any]) -> Dict[str, Any] | None:
    name = str(item.get("name") or "").strip()
    content = str(item.get("content") or "")
    if not name or not content:
        return None
    return {
        "name": name,
        "skill_id": str(item.get("skill_id") or item.get("skillId") or ""),
        "path": str(item.get("path") or ""),
        "content": content,
        "agent_id": str(item.get("agent_id") or item.get("agentId") or ""),
        "allowed_tools": [
            str(tool)
            for tool in item.get("allowed_tools", item.get("allowedTools", []))
            if str(tool).strip()
        ],
        "model": str(item.get("model")).strip() if item.get("model") else None,
        "effort": str(item.get("effort")).strip() if item.get("effort") else None,
        "execution_context": str(
            item.get("execution_context") or item.get("executionContext") or "inline"
        ),
    }


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
