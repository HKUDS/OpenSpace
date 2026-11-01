from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class ChannelPlatform(str, Enum):
    WHATSAPP = "whatsapp"
    FEISHU = "feishu"


class AttachmentKind(str, Enum):
    IMAGE = "image"
    DOCUMENT = "document"
    FILE = "file"


@dataclass(slots=True)
class ChannelAttachment:
    kind: AttachmentKind
    path: str
    name: str = ""
    mime_type: str = ""
    size_bytes: Optional[int] = None
    source_url: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_context_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "path": self.path,
            "name": self.name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "source_url": self.source_url,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class ChannelSource:
    platform: ChannelPlatform
    chat_id: str
    chat_type: str = "dm"
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    chat_name: Optional[str] = None
    thread_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform.value,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "chat_name": self.chat_name,
            "thread_id": self.thread_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChannelSource":
        return cls(
            platform=ChannelPlatform(str(data["platform"])),
            chat_id=str(data["chat_id"]),
            chat_type=str(data.get("chat_type", "dm")),
            user_id=_optional_str(data.get("user_id")),
            user_name=_optional_str(data.get("user_name")),
            chat_name=_optional_str(data.get("chat_name")),
            thread_id=_optional_str(data.get("thread_id")),
        )


@dataclass(slots=True)
class ChannelMessage:
    source: ChannelSource
    text: str
    message_id: str
    attachments: List[ChannelAttachment] = field(default_factory=list)
    reply_to_message_id: Optional[str] = None
    reply_to_text: Optional[str] = None
    mentions_bot: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_channel_context(self, session_key: str) -> Dict[str, Any]:
        return {
            "platform": self.source.platform.value,
            "chat_id": self.source.chat_id,
            "chat_type": self.source.chat_type,
            "chat_name": self.source.chat_name,
            "thread_id": self.source.thread_id,
            "user_id": self.source.user_id,
            "user_name": self.source.user_name,
            "session_key": session_key,
            "message_id": self.message_id,
            "reply_to_message_id": self.reply_to_message_id,
            "reply_to_text": self.reply_to_text,
            "attachments": [attachment.to_context_dict() for attachment in self.attachments],
        }


@dataclass(slots=True)
class ChannelReply:
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SendResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: Any = None


@dataclass(slots=True)
class ChannelSession:
    session_key: str
    source: ChannelSource
    session_dir: str
    workspace_dir: str
    attachments_dir: str
    transcript_path: str
    metadata_path: str
    created_at: str
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_key": self.session_key,
            "source": self.source.to_dict(),
            "session_dir": self.session_dir,
            "workspace_dir": self.workspace_dir,
            "attachments_dir": self.attachments_dir,
            "transcript_path": self.transcript_path,
            "metadata_path": self.metadata_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChannelSession":
        return cls(
            session_key=str(data["session_key"]),
            source=ChannelSource.from_dict(data["source"]),
            session_dir=str(data["session_dir"]),
            workspace_dir=str(data["workspace_dir"]),
            attachments_dir=str(data["attachments_dir"]),
            transcript_path=str(data["transcript_path"]),
            metadata_path=str(data["metadata_path"]),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
        )


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None
