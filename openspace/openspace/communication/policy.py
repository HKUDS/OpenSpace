from __future__ import annotations

from typing import Any

from openspace.communication.types import ChannelMessage


def build_attachment_instruction(message: ChannelMessage) -> str:
    attachment_paths = ", ".join(attachment.path for attachment in message.attachments)
    return (
        "Please inspect the attached files and help the user based on their contents. "
        f"Attachment paths: {attachment_paths}"
    )


def is_authorized(message: ChannelMessage, platform_config: Any) -> bool:
    if getattr(platform_config, "allow_all_users", False):
        return True

    allowed_users = {
        entry.strip()
        for entry in getattr(platform_config, "allowed_users", [])
        if entry and entry.strip()
    }
    if not allowed_users:
        return False

    user_candidates = {
        candidate.strip()
        for candidate in (
            message.source.user_id,
            message.source.user_name,
            message.metadata.get("raw_user_id") if isinstance(message.metadata, dict) else None,
        )
        if candidate and candidate.strip()
    }
    if isinstance(message.metadata, dict):
        for candidate in message.metadata.get("auth_candidates", []) or []:
            if isinstance(candidate, str) and candidate.strip():
                user_candidates.add(candidate.strip())
    return bool(user_candidates & allowed_users)


def should_accept_message(
    message: ChannelMessage,
    platform_config: Any,
    reply_to_bot: bool,
) -> bool:
    if message.source.chat_type == "dm":
        return bool(getattr(platform_config, "allow_dm", True))
    if not getattr(platform_config, "allow_groups", True):
        return False

    group_policy = str(getattr(platform_config, "group_policy", "reply_or_mention"))
    if group_policy == "disabled":
        return False
    if group_policy == "all":
        return True
    if group_policy == "mention_only":
        return message.mentions_bot
    return message.mentions_bot or reply_to_bot
