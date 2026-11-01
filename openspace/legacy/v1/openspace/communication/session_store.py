from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from openspace.utils.logging import Logger

from .types import ChannelAttachment, ChannelMessage, ChannelSession, ChannelSource

logger = Logger.get_logger(__name__)


class SessionStore:
    def __init__(
        self,
        sessions_dir: Path,
        *,
        workspace_root: Optional[Path] = None,
    ):
        self.sessions_dir = sessions_dir
        self.workspace_root = workspace_root
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        if self.workspace_root is not None:
            self.workspace_root.mkdir(parents=True, exist_ok=True)

    def get_or_create_session(self, source: ChannelSource) -> ChannelSession:
        session_key = build_session_key(source)
        session_dir = self.sessions_dir / session_key
        session_dir.mkdir(parents=True, exist_ok=True)

        metadata_path = session_dir / "session.json"
        transcript_path = session_dir / "transcript.jsonl"
        attachments_dir = session_dir / "attachments"
        workspace_dir = (
            self.workspace_root / session_key
            if self.workspace_root is not None
            else session_dir / "workspace"
        )
        attachments_dir.mkdir(parents=True, exist_ok=True)
        workspace_dir.mkdir(parents=True, exist_ok=True)

        now = _utcnow_iso()
        if metadata_path.exists():
            with open(metadata_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            session = ChannelSession.from_dict(data)
            session.source = source
            session.updated_at = now
        else:
            session = ChannelSession(
                session_key=session_key,
                source=source,
                session_dir=str(session_dir),
                workspace_dir=str(workspace_dir),
                attachments_dir=str(attachments_dir),
                transcript_path=str(transcript_path),
                metadata_path=str(metadata_path),
                created_at=now,
                updated_at=now,
            )

        self._write_session_metadata(session)
        return session

    def append_user_message(self, session: ChannelSession, message: ChannelMessage) -> None:
        self._append_transcript_entry(
            session,
            {
                "entry_id": uuid.uuid4().hex,
                "role": "user",
                "content": message.text,
                "platform_message_id": message.message_id,
                "reply_to_message_id": message.reply_to_message_id,
                "reply_to_text": message.reply_to_text,
                "mentions_bot": message.mentions_bot,
                "attachments": [attachment.to_context_dict() for attachment in message.attachments],
                "source": message.source.to_dict(),
                "metadata": message.metadata,
                "timestamp": message.received_at.isoformat(),
            },
        )

    def append_assistant_message(
        self,
        session: ChannelSession,
        *,
        content: str,
        platform_message_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._append_transcript_entry(
            session,
            {
                "entry_id": uuid.uuid4().hex,
                "role": "assistant",
                "content": content,
                "platform_message_id": platform_message_id,
                "metadata": metadata or {},
                "timestamp": _utcnow_iso(),
            },
        )

    def load_history(self, session: ChannelSession, max_turns: int) -> List[Dict[str, str]]:
        entries = self._read_transcript_entries(session)
        if not entries:
            return []

        selected: List[Dict[str, str]] = []
        user_messages = 0
        for entry in reversed(entries):
            role = entry.get("role")
            if role not in {"user", "assistant"}:
                continue
            if role == "assistant" and not _assistant_entry_visible_in_history(entry):
                continue
            content = str(entry.get("content", "")).strip()
            if not content:
                continue
            selected.append({"role": role, "content": content})
            if role == "user":
                user_messages += 1
                if user_messages >= max_turns:
                    break
        selected.reverse()
        return selected

    def is_reply_to_assistant(self, session: ChannelSession, message_id: Optional[str]) -> bool:
        if not message_id:
            return False
        for entry in reversed(self._read_transcript_entries(session)):
            if entry.get("platform_message_id") == message_id:
                return entry.get("role") == "assistant" and _assistant_entry_visible_in_history(entry)
        return False

    def list_sessions(self) -> List[ChannelSession]:
        sessions: List[ChannelSession] = []
        for metadata_path in sorted(self.sessions_dir.glob("*/session.json")):
            try:
                with open(metadata_path, "r", encoding="utf-8") as handle:
                    sessions.append(ChannelSession.from_dict(json.load(handle)))
            except Exception as exc:
                logger.warning("Failed to load session metadata %s: %s", metadata_path, exc)
        return sessions

    def _append_transcript_entry(self, session: ChannelSession, entry: Dict[str, Any]) -> None:
        transcript_path = Path(session.transcript_path)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        with open(transcript_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        session.updated_at = _utcnow_iso()
        self._write_session_metadata(session)

    def _write_session_metadata(self, session: ChannelSession) -> None:
        with open(session.metadata_path, "w", encoding="utf-8") as handle:
            json.dump(session.to_dict(), handle, ensure_ascii=False, indent=2)

    def _read_transcript_entries(self, session: ChannelSession) -> List[Dict[str, Any]]:
        transcript_path = Path(session.transcript_path)
        if not transcript_path.exists():
            return []
        entries: List[Dict[str, Any]] = []
        with open(transcript_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed transcript line in %s", transcript_path)
        return entries


def build_session_key(source: ChannelSource) -> str:
    parts = [source.platform.value, _sanitize(source.chat_id)]
    if source.thread_id:
        parts.append(_sanitize(source.thread_id))
    return "__".join(part for part in parts if part)


def _sanitize(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value).strip())
    value = value.strip("-._")
    return value or "unknown"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _assistant_entry_visible_in_history(entry: Dict[str, Any]) -> bool:
    metadata = entry.get("metadata")
    if not isinstance(metadata, dict):
        return True
    return metadata.get("send_success") is not False
